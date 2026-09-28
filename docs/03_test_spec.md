# Test Specification — HCR-C2-062

## Test Strategy
- Coverage target: framework-compliance (TC-01〜TC-11) + PB-1〜PB-7 + domain PB items ①〜⑩ (docs/02_design.md)
- LLM and file-upload channels are exercised through injection-seam fakes; **PB-3 runs KBService's production SQL against a real database engine (sqlite3)** — see the PB-3 note below for why a fake is not sufficient there. The customer's actual DSN/driver is exercised at STG.
- Test types: Unit (`tests/unit/`) / Integration (`tests/integration/`) / Proof-of-Boundary (`tests/proof_of_boundary/`)
- Fakes: `tests/conftest.py` — `FakeDatabase(BaseDatabase)` / `FakeLLM(BaseLLM)`, injected via the constructor seams (`KBService(db=...)`, `config["llm"]`)

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | ✅ PASS (`tests/unit/test_tc_framework_compliance.py::TestTC01StateContractEnforcement`) |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | ✅ PASS (`TestTC02SecurityViolationErrorFires` + `tests/unit/test_main_node.py::TestS3ProvenanceGate` — 6 fail-closed variants) |
| TC-03 | No JWT/Credential in State | CI `gate-credential-scan`: 0 violations | ⏳ Verified at MR push-time CI (S-5 enforcement is a CI job, not a local pytest) |
| TC-04 | InvocationContext via configurable only | Direct access raises error / AST scan confirms `InvocationContext.from_state()` usage only | ✅ PASS (`TestTC04InvocationContextViaConfigurableOnly`) |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` body; domain `emit_trace_event()` present in every node | ✅ PASS (verified manually across all node files — every `execute()` calls `emit_trace_event()` at least once; framework events are never duplicated) |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode` subclass) | `TypeError` at class definition if overridden | ✅ PASS (`TestTC06S2GateNonBypassable`) |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode` subclass) | `TypeError` at class definition if overridden | ✅ PASS (`TestTC07S3GateNonBypassable` — includes a scan of all 14 `FunctionNode` subclasses confirming none redefine the `@final` methods) |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | ✅ PASS (`tests/unit/test_pre_process_node.py::TestPreProcessNodeTrustGate` — calls `node(state)` via `__call__`, not `execute()` directly) |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial (domain checks) | Channel sniffing + formula-injection neutralization + XXE-hardened docx parse + PDF/unknown-format reject | ✅ PASS (`tests/unit/test_sanitizer.py`, `tests/unit/test_pre_process_node.py::TestPreProcessNodeS2Gate`) |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial (domain checks) | Provenance ID machine verification (main_node) + quality-verdict lexicon neutralization + PII rescan (post_process_node) | ✅ PASS (`tests/unit/test_main_node.py::TestS3ProvenanceGate`, `tests/unit/test_post_process_node.py`) |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per `execute()` | Domain event emitted on every invocation path | ✅ PASS (every node module imports and calls `emit_trace_event` from `shared.utils.audit_logger`) |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `emit_trace_event()` fires from `shared.utils.audit_logger` | No silent failures | ✅ PASS (implicit in every node test — nodes without a working audit call would fail TC-11 checks) |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | ✅ PASS (`tests/proof_of_boundary/test_state_safety.py`) |
| PB-3 | L1 → External service (KB database) | KBService's real SQL executed against a **real database engine** (sqlite3) through a real `BaseDatabase` implementation — DDL agreement, ingest INSERTs, Stage 2 SELECTs, tenant isolation (search **and** update), feedback round trip, KB PII non-residue, fail-closed contracts | Real data written and retrieved; no cross-tenant leakage | **PASS** — `tests/proof_of_boundary/test_pb3_real_database.py` (16 tests) |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | ✅ PASS (`tests/proof_of_boundary/test_import_isolation.py`) |
| PB-5 | Checkpoint safety | No JWT/Pydantic/raw-narrative in checkpoint | Inspection pass | ✅ PASS (`tests/proof_of_boundary/test_state_safety.py` + `tests/proof_of_boundary/test_pb_domain_deviation_kb.py::TestPB_StateNeverCarriesRawNarrative`) |
| PB-6 | Invoke execution order | S-1 → S-4 `node_start` → S-2 → `execute()` → S-3 → S-4 `node_complete` | Order verified for all 15 concrete node classes (HITL-interrupt-driving nodes verified via a bare-instantiation degrade-to-error variant, per the BusinessDiningAndEntertainmentPlannerAgent precedent) | ✅ PASS (`tests/proof_of_boundary/test_pb_invoke_order.py`) |
| PB-7 | HITL interrupt propagation — **mandatory** (`config/agent.yaml` `hitl.enabled: true`) | (a) `interrupt()` → suspend, `status != error` (b) `hitl_allowed=False` → no interrupt fires (c) resume → `status: success` (subgraph-caching defect class check, beyond the minimum PB-7 pair) | ✅ PASS — all 3 assertions (`tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py`) |

> **PB-7 note:** the scaffold `pytest.skip()` TODO stubs have been replaced with real assertions against
> the actual compiled `GmpDeviationCapaGraph`. A third test (`test_pb7_resume_reaches_success`) goes
> beyond the minimum PB-7 pair to catch the subgraph-caching defect class (fresh inner graph per
> `execute()` breaks resume) that a suspend-only test cannot detect — per the BusinessDiningAndEntertainmentPlannerAgent lesson.

> **Why PB-3 runs against a real engine here, not only at STG.** The rest of the suite injects a
> `FakeDatabase` that ignores the SQL string and returns canned rows, so SQL-level defects are
> invisible to it. That is not hypothetical: `search_capa_effectiveness()` shipped with
> `WHERE c.deviation_id IN :deviation_ids` bound to a tuple — SQL `IN` cannot bind a sequence to a
> single named parameter, and a real engine raises `OperationalError: near ":deviation_ids": syntax
> error`. It passed 191 unit tests, two independent reviews because nothing ever
> handed the SQL to a database. Since `search_capa_effectiveness()` is the CAPA-effectiveness recall
> — this template's core value — it would have failed on the first real invocation.
>
> `shared.services.database`'s `PostgresClient` / `MySQLClient` (SDK 1.0.0) are placeholders whose
> methods all `raise NotImplementedError("Configure PostgreSQL connection")`, so the SDK ships no
> concrete engine to test against. This PB-3 therefore supplies its own real `BaseDatabase`
> implementation (sqlite3) and runs the production SQL through it on every push. STG remains the
> place where the customer's actual DSN/driver is exercised.

## Domain Proof-of-Boundary Tests (docs/02_design.md "PB（ドメイン）")

| # | Item | Test | Result |
|---|------|------|--------|
| ① | Worker-PII input (CSV/Excel/Word) → role-token replacement | `tests/unit/test_pii_sanitizer.py`, `tests/proof_of_boundary/test_pb_domain_deviation_kb.py::TestPB_WorkerPiiIngestSanitization` | ✅ PASS |
| ② | KB records never contain worker name/employee-number pattern | Same as ① — asserts every `FakeDatabase.execute()` param string is free of the injected name pattern | ✅ PASS |
| ③ | State/checkpoint never carries raw narrative text (payload_ref only) | `TestPB_StateNeverCarriesRawNarrative` | ✅ PASS |
| ④ | Tenant search isolation (2-tenant KB seed) | `TestPB_TenantSearchIsolation` + `tests/unit/test_kb_service.py` tenant fail-closed suite | ✅ PASS |
| ⑤ | Output schema has no quality-verdict field | `TestPB_OutputSchemaNoQualityVerdictField` (schema-file assertion + injection-rejection assertion) | ✅ PASS |
| ⑥ | Assertive-statement lexicon detection + sentence-level replacement | `tests/unit/test_quality_verdict_gate.py`, `TestPB_AssertiveLexiconDetection` | ✅ PASS |
| ⑦ | Provenance: fabricated ID → raise | `tests/unit/test_main_node.py::test_fabricated_id_raises` | ✅ PASS |
| ⑧ | Provenance: real-but-unrelated `source_id` (attribute mismatch) → raise | `test_real_but_unrelated_source_id_raises` | ✅ PASS |
| ⑨ | Provenance: `gate_kind`/citation-field absence → raise (fail closed) | `test_missing_gate_kind_raises_fail_closed`, `test_missing_citation_fields_on_draft_path_raises` | ✅ PASS |
| ⑩ | invoke → feedback E2E wiring (recorded effectiveness later hits) | `tests/integration/test_graph_e2e.py::TestInvokeToFeedbackWiring` | ✅ PASS |

## API-level Authorization Tests (review findings F-01/F-02 — 2026-07-14)

The minimum the review specified: prove the fix through the real FastAPI app (`src.api.server`), not
mocks — the boundary under test IS the wiring between the HTTP layer, `_resolve_trust()`,
and the checkpoint's `tenant_id`.

| # | Item | Test | Result |
|---|------|------|--------|
| F-01a | Smoke payload (`deploy/invoke_payload.json`) no longer declares a caller-controlled `hitl_allowed` | `tests/integration/test_stg_invoke_payload.py::test_payload_does_not_declare_caller_controlled_hitl_allowed` | ✅ PASS |
| F-01b | STG smoke run (`STG_MOCK_MODE=true`) still reaches `status: success` on first `/invoke` | `test_invoke_with_standard_payload_reaches_success` | ✅ PASS |
| F-01c | Normal caller (`STG_MOCK_MODE` unset) POSTing `hitl_allowed: false` in the body is ignored — session still suspends to `awaiting_human` | `test_invoke_with_caller_supplied_hitl_allowed_false_still_suspends` | ✅ PASS |
| F-02a | `/resume` without a Bearer token → 401 | `tests/integration/test_resume_authorization.py::TestResumeEntryPointAuth::test_resume_without_token_is_denied` | ✅ PASS |
| F-02b | `/resume` with an invalid Bearer token → 401 | `test_resume_with_wrong_token_is_denied` | ✅ PASS |
| F-02c | `/resume` from a different tenant (`company_id`/`site_id` mismatch vs. checkpoint `tenant_id`) → 403 | `TestResumeTenantOwnership::test_resume_from_different_tenant_is_forbidden` | ✅ PASS |
| F-02d | `/resume` against an unknown `thread_id` → 404 | `test_resume_unknown_thread_id_is_not_found` | ✅ PASS |
| F-02e | `/resume` from the correct tenant → `status: success` (regression guard on the legitimate path) | `test_resume_with_correct_tenant_succeeds` | ✅ PASS |
| F-02f | `/resume` with `:`-containing `company_id`/`site_id` (delimiter-collision forgery against unescaped `tenant_id` concatenation, review finding) → 403 | `test_resume_rejects_colon_in_tenant_fields` | ✅ PASS |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Stage 1 structures every incident element with a `source_id` | Raw record dict | All elements carry `source_id` | ✅ PASS (`tests/unit/test_investigation_pipeline.py::TestStage1StructureIncident`) |
| BL-02 | Stage 2 tenant-scoped search, cold-start returns empty (not error) | Empty KB, valid tenant | `similar_deviations=[]`, `status=success`, `degradation_reason` set | ✅ PASS (`TestStage2SearchSimilar`, `tests/unit/test_inner_nodes.py::test_stage2_cold_start_degrades_gracefully`) |
| BL-03 | Stage 3 LLM-absent deterministic fallback never invents a quality verdict | No LLM | `investigation_draft` has no `quality_verdict`/`shippability` key | ✅ PASS (`TestStage3InvestigationDraft::test_no_llm_deterministic_fallback`) |
| BL-04 | Stage 3 citation parser never trusts fabricated ids from free text | LLM response referencing a non-existent id | Fabricated id excluded from `cited_source_ids` | ✅ PASS (`test_llm_cannot_cite_unknown_ids`) |
| BL-05 | Recurrence-risk flag is 100% deterministic (no `llm` parameter) | `capa_effectiveness` records | `risk_level` computed from `recurrence_observed`/`effective` counts only | ✅ PASS (`TestStage4RecurrenceFlags::test_is_purely_deterministic_no_llm_param`) |
| BL-06 | `lot_ref` default non-storage; governance-flag opt-in | Deviation record with `lot_ref` | Not persisted unless `lot_ref_governance_enabled=True` | ✅ PASS (`tests/unit/test_kb_service.py::TestKBServiceLotRefGovernance`) |
| BL-07 | HITL 3-way routing (APPROVED/CORRECTED/REJECTED) all handled | resume feedback variants | approve→success, correct→merged draft, reject→cancelled + post_process skipped | ✅ PASS (`tests/integration/test_graph_e2e.py::TestInvokeHitlFlow`) |
| BL-08 | Ingest minimum-data-requirement enforcement (reject vs. unclassified) | Rows missing required vs. recommended fields | Required-field-missing → reject; recommended-field-missing → `unclassified` + `missing_data_flags` | ✅ PASS (`tests/unit/test_record_parser.py`) |
| BL-09 | v1 input scope: PDF rejected at S-2 (allowlist, fail closed) | `%PDF-` magic bytes | `status=error` at `PreProcessNode` gate, never reaches the parser | ✅ PASS (`tests/unit/test_pre_process_node.py::test_pdf_payload_rejected`, `tests/integration/test_graph_e2e.py::test_ingest_unsupported_pdf_rejected`) |

## Test Execution Summary
- Execution date: 2026-07-14 (updated: F-01/F-02 fix pass + a post-fix independent review finding)
- Total tests: **210**
- Pass: **210** / Fail: **0** / Skip: **0** (PB-7 is HITL-conditional but this template IS HITL-enabled, so it executes rather than skips)
- Coverage: unit (157) + integration (21) + proof_of_boundary (13) across all `src/` modules — every node, service, the outer+inner graph compile paths, and the API authorization boundary (`/invoke` hitl_allowed lockdown, `/resume` auth + tenant ownership + delimiter-collision guard) are exercised
- Local run: `pytest tests/ -v` (`.venv`, `agenticstar-agentcore==1.0.0`) — 191 passed
- New in this pass: `tests/integration/test_resume_authorization.py` (6 tests, including a review-driven colon-delimiter-collision guard) + 1 new test in `tests/integration/test_stg_invoke_payload.py` (`test_invoke_with_caller_supplied_hitl_allowed_false_still_suspends`), replacing the retired `test_payload_sets_hitl_allowed_false`

## Live LLM verification (`generation_mode: "llm"` の実測裏付け)

`config/agent.yaml` は `generation_mode: "llm"` を宣言している。モックではない実 LLM を
注入して 1 回転させ、宣言が真であること・出力が実用に足ることを確認した記録。
**この検証で、モックが隠していた S-3 第2層の fail-open を検出した。**

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI — ローカル CLI 経由の `BaseLLM` 実装（検証専用） |
| SDK | `agenticstar-agentcore==1.0.1`（CI の `AGENTCORE_WHEEL_SPEC` と同一） |
| 注入シーム | `config["llm"]` → `Stage3InvestigationNode` / `Stage4CapaNode`（`stage3_investigation_draft`） |
| 入力 | `mode: invoke` / `ACME:SITE1` / `free_text: "process anomaly"`（`tests/integration/test_graph_e2e.py` と同系の入力） |
| KB seed | `tests/conftest.py` の `FakeDatabase` に類似逸脱 1 件 + CAPA 1 件を投入（Stage 2 検索が実際にヒットする状態） |
| 実 LLM 呼び出し | **1 回**（Stage 3 調査ドラフト） |
| 結果 | ✅ `status: success` / S-3 Provenance gate（`gate_kind: "draft"`）通過 / 縮退なし |
| 中身のあった出力キー | `gate_kind`, `report`, `recurrence_flags`, `status` |

### 検出した欠陥: S-3 第2層（品質断定ガード）が実 LLM の出力言語に対して無効だった

初回の実 LLM 実行で、モデルは **英語**で調査ドラフトを返した。Stage 3 のプロンプトが
英語で書かれており、**出力言語を一切指定していなかった**ためである。

一方、S-3 第2層の品質断定ガード（`config/quality_verdict_lexicon.yaml`）は
**全 20 パターンが日本語**で、英語パターンは 0 件。NFKC + casefold 後の文字列照合で
動作する。したがって英語の品質断定（例: "This batch can be released and has no quality
impact."）は **ガードに一切照合されないまま GMP 調査報告書へ流れる**経路になっていた。

単体・統合テストがこれを検出できなかった理由は、`tests/conftest.py` の `FakeLLM` が
**日本語**の固定文字列を返すため。テストは常に日本語経路しか通っておらず、実運用で
LLM が返す言語との乖離が全テスト緑のまま潜伏していた。

**対処（プロンプト側のみ）**: lexicon は `schema_owner: customer_quality_assurance` の
版管理された統制文書であり、こちら側で英語パターンを追加して回避することはできない
（＝入力であって改変対象ではない）。よって `_build_stage3_prompt()` に
**出力言語を日本語に固定する trusted 指示**を追加した。ガードのロジック・lexicon・
Provenance gate はいずれも変更していない。修正後の実 LLM は全文日本語で応答し、
`source_id` トークンも逐語で保持されることを実測で確認した。

> ⚠️ **この対処は決定論的な塞ぎではない。** プロンプト指示は LLM が従う限りしか効かず、
> 英語出力の fail-open 自体は**未閉塞**である。下記「未閉塞の既知欠陥」①を参照。

### 追加したテストと変異検査

| テスト | 何を固定するか | 変異検査 |
|---|---|---|
| `test_stage3_prompt_pins_the_output_language_to_the_guarded_language` | プロンプトが日本語出力を要求している | `_build_stage3_prompt` の OUTPUT LANGUAGE 節を削除 → 赤 |
| `test_the_guard_neutralizes_a_verdict_in_the_guarded_language` | 日本語の断定表現が実置換される（固定した言語でガードが動く） | — |
| `test_an_english_quality_verdict_should_also_be_neutralized` | **あるべき姿**（英語断定も中和される）を assert し `xfail(strict=True)` で留置 | 塞がれた時点で「予期せぬ成功」として落ち、マーカー除去と閉塞の記録を強制する |

> 🔴 当初は「英語は検知されない」ことを assert していたが、それは**欠陥を仕様として
> 固定**する形であり、QA が正規に英語パターンを追加した際に CI が落ちる。敵対
> レビュー指摘を受け、望ましい状態を書いて strict xfail で留める形へ改めた。

### 未閉塞の既知欠陥（本検証で判明・いずれも本修正以前からの事象／設計判断が必要）

1. **英語・混在言語出力の fail-open（上記の残余）** — 決定論的に塞ぐには
   (a) LLM 応答後・lexicon 適用前に言語 admission gate を置き、`source_id` / ロットコード /
   承認済み略語以外の連続した英語自然文を検出したら fail-closed にする、または
   (b) QA の変更管理のもとで英語 lexicon とテストベクタを追加する、のいずれかが必要。
   どちらも統制文書のオーナー判断を要するため本 MR では実施していない。
2. **Provenance gate の属性一致検査が実運用経路で空振り** — `main_node._extra_security_gate_output()`
   の「実在するが無関係な source_id」検出は `evidence_map[].asserted_attrs` を実レコードと
   突き合わせる設計だが、`_parse_stage3_response()` は `asserted_attrs` を
   `{sid: {}}`（空 dict）で組むため、比較対象が空で**必ず通過**する。
   なお修正時に実レコードから `asserted_attrs` を後付けしてはならない（正本同士の比較になり
   恒真式になる）。LLM に構造化出力で属性を主張させ、それを実レコードと比較する必要がある。
3. **根拠ゼロのドラフトが human review を回避する** — `requires_human_review` は
   `any(not claim.get("source_ids") for claim in evidence_map)` で決まるため、
   引用が 1 件も無く `evidence_map == []` の場合 `any([]) == False` となり
   **人手確認が要求されない**。設計意図（根拠のない主張は human review）と逆。
   最低でも `not evidence_map` も review 必須に含める必要がある。
4. **日本語の裸氏名が PII サニタイザで検出されない** — `_NAME_HONORIFIC` は敬称付き、
   `_NAME_FULL` は姓名間スペース付きのみを対象とするため、「山田太郎が充填作業を実施した」
   は置換 0 件（実測）。加えて invoke 経路の `free_text` は切り詰めのみで、
   **LLM 投入前に worker PII サニタイズを通っていない**。
