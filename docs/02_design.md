# HCR-C2-062 — Design Specification

> GMP Manufacturing Deviation & CAPA Report Drafting Agent（逸脱履歴想起・蓄積型）

---

## Position in AgentCore Architecture

- **Agent Class**: `GmpDeviationCapaGraph`（`src/graph/graph.py`）
- **L1 Base**: **AgentBaseGraph**（L1 直継承。`AutonomousBaseGraph` ではない — 自律ループを持たない固定パイプライン）
- **Three-Layer Separation**:
  - **State**: flat TypedDict composition（`AgentState` を継承。プリミティブと JSON 直列化可能な型のみ）
  - **Node**: L1 inheritance（Template Method: `execute(self, state) -> dict` のみ override）
  - **Graph**: composition（`register_nodes()` で 3 スロットを埋める。`add_edges()` は override しない）

> **旧 Level 2 base 継承モデルからの移行注記**: 本テンプレートは旧 "Level 2" 基底エージェント
> （RAGAgent / DocGenerationAgent 等）を継承しない。L1 フレームワーク（`AgentBaseGraph`）を直接継承し、
> ドメインの複雑さは `main` スロットの `GraphNode` が包む内側 workflow graph に閉じ込める。

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| `initialize` | schema_version / session_id / trust_level | — | — | `InitializeNode`（framework 既定） |
| `pre_process` | モードルーティング・入力検証・**テナント fail-closed**・**S-2 入力チャネルゲート** | `user_input` | `mode` / `tenant_id` / `validated_input` / `payload_ref` | `PreProcessNode(FunctionNode)` |
| `main` | 内側 workflow graph（ingest / invoke 4 stage / feedback）の実行・**S-3 Provenance 検証** | `validated_input` | `result` / `evidence_map` / `gate_kind` ほか | `DeviationWorkflowGraphNode(GraphNode)` |
| `post_process` | 出力整形・**S-3 品質断定ガード（第 2 層）**・PII 再スキャン・縮退理由の畳み込み | `result` | `formatted_output` | `PostProcessNode(FunctionNode)` |
| `finalize` | response_metadata / total_time_ms | — | — | `FinalizeNode`（framework 既定） |

### 実装時のクラス名整合（MR-B）

| ファイル | クラス |
|---|---|
| `src/graph/graph.py` | `GmpDeviationCapaGraph(AgentBaseGraph)` |
| `src/graph/domain_workflow_graph.py` | `DeviationWorkflowGraph(BaseGraph)`（内側） |
| `src/nodes/main_node.py` | `DeviationWorkflowGraphNode(GraphNode)` |
| `src/nodes/pre_process_node.py` | `PreProcessNode(FunctionNode)` |
| `src/nodes/post_process_node.py` | `PostProcessNode(FunctionNode)` |
| `cli.py` | `MarketplaceGraph(GmpDeviationCapaGraph)` — AGENTIC STAR Marketplace read-only entry adapter |

`config/agent.yaml` の `class:` は `GmpDeviationCapaGraph`、`base_type:` は `AgentBaseGraph`。

`cli.py` の Marketplace 経路は素の文字列を `free_text` として受け、`mode="invoke"` に固定する。
`company_id` / `site_id` は呼び出し入力ではなく AGENTIC STAR Admin の deployment env から解決し、runner が渡す
`conversation_history` を含む `input_context` は State にマージしない。Marketplace は resume channel を
持たないため `InvocationContext` を `hitl_allowed=False` に置換し、`AWAITING_HUMAN` を外へ返さない。

> **AgentRegistry エントリポイント整合**: `config/agent.yaml` の
> `module: src.graph` / `class: GmpDeviationCapaGraph` と、`src/graph/graph.py` が実際に export する
> クラス名・`src/api/server.py` の import は**常に一致していなければならない**（不一致だと registry が
> 実在しないエントリポイントを解決しようとする）。MR-A 時点で seed の `Graph` を
> `GmpDeviationCapaGraph` へリネーム済み（`src/api/server.py` の import も同時更新）。
> 内側 graph・ノードの実装は MR-B。

### Data Flow — 外側 backbone（固定・変更しない）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                    ↓ (RETRY, max 3)
                                 pre_process
```

`add_edges()` / `route()` は override しない（framework 既定）。`route()` は `main` の後に
`state["status"]`（`AgentStatus` の**値**）を読む。HITL 時は `hitl_status` と `hitl_feedback` の
**両方**が揃ったときのみ HITL ルーティングが発火する（フレームワークの HITL ルーティング契約）。

---

## 3 つの動作モード

| Mode | エンドポイント | trust | 処理 |
|---|---|---|---|
| `ingest` | `/ingest` | `VERIFIED_EXTERNAL`（DB 書込） | 過去の逸脱記録・CAPA 実施記録・有効性確認結果を構造化 → サニタイズ → KB へ INSERT |
| `invoke` | `/invoke` | `VERIFIED_EXTERNAL` | 新規逸脱のインシデント記録 → 4 stage パイプライン → 報告書・CAPA ドラフト |
| `feedback` | `/feedback` | `VERIFIED_EXTERNAL`（DB 書込） | CAPA 実施結果・有効性確認結果・再発有無を KB へ反映 |

**全モードが同期 request-response**（非同期バッチジョブは持たない）。モードは `pre_process.execute()` 内で
`validated_input` から確定し、**業務ロジックの分岐にのみ使う。セキュリティ判定には使わない**（後述 S-2）。

### Data Flow — 内側 workflow graph（`src/graph/domain_workflow_graph.py`）

```
                      ┌── mode == "ingest"   → parse → sanitize → taxonomy_tag → kb_upsert ─────┐
START → mode_router ──┼── mode == "invoke"   → stage1 → stage2 → stage3 → [hitl_review] → stage4 ┼→ END
                      └── mode == "feedback" → validate → kb_update_effectiveness ──────────────┘
                                                     ↑
                                        status == error → END（fail fast）
```

内側 graph の `route()` / `get_output()` は**自スキーマ（`DeviationWorkflowState`）で型注釈する**。
`AgentState` で注釈すると射影でカスタムフィールドが消える（Business Travel Hotel & Plan Recommendation Agent 実測）。

### invoke の 4 ステージ

| Stage | 種別 | 処理 | 出力 |
|---|---|---|---|
| **Stage 1** | 決定論（LLM 不使用） | インシデント記録を構造化（事象・工程・設備・時系列・関係ロット）。**全要素に `source_id` を付与** | `structured_incident`（`source_id` 付き要素の配列） |
| **Stage 2** | 検索（決定論・**テナント内限定**） | 類似過去逸脱（逸脱カテゴリ × 工程種別 × 5M1E 根本原因分類）＋当該類型への過去 CAPA と有効性実績を KB 照合 | `similar_deviations` / `capa_effectiveness` / `retrieved_case_ids` |
| **Stage 3** | LLM | 調査報告書ドラフト生成（事象記述・時系列整理・5 Whys 支援による RCA **仮説**の構造化）。影響評価は**「過去類似事例の事実想起＋要確認事項の列挙」に限定**し断定しない | `investigation_draft` / `evidence_map` / `cited_source_ids` |
| **Stage 4** | LLM + 決定論 | CAPA 計画ドラフト（過去の同型 CAPA の有効性実績を根拠添付）＋**再発リスクフラグ（決定論算出）**＋根拠マップ統合＋欠落データフラグ | `capa_draft` / `recurrence_flags` / `missing_data_flags` |

> **再発リスクフラグは決定論で算出する**（LLM に判定させない）。Stage 2 が返した同型逸脱の
> `recurrence_observed` 件数と、その CAPA の `effective` 実績から機械的に導く。LLM が担うのは
> その事実の**言語化**のみ。品質に直結する判断を生成に委ねない。

---

## 🔴 v1 の入力スコープ（Design 入場条件 #2 の一部）

| 入力形式 | v1 | 理由 |
|---|---|---|
| **CSV / Excel（.xlsx）の QMS 構造化エクスポート** | ✅ **v1 対象** | 逸脱記録は GMP 上の定型様式（逸脱番号・分類・工程・RCA・CAPA）であり、主要 QMS（TrackWise / Veeva Vault QMS / MasterControl）はレポート出力機能を持つ。**構造化された状態で受け取るのが最も欠落・誤読が少ない** |
| **Word（.docx）のテキスト抽出** | ✅ **v1 対象** | 調査報告書本体が Word 様式のサイトが実在する。`.docx` は ZIP + XML でありテキスト抽出は決定論で可能（マクロは実行しない・埋め込みオブジェクトは読まない） |
| **PDF（テキストレイヤあり）** | ⚠️ **v1.1 候補（要ユーザー判断）** | 抽出自体は可能だが `/JavaScript` `/OpenAction` 等の攻撃面を持ち込む（入力チャネルのサニタイゼーション方針）。受理するなら S-2 で拒否が必須 |
| **PDF（スキャン画像）・紙** | 🚫 **スコープ外** | OCR が必要。OCR 誤読が逸脱記録の数値・ロット番号を壊すと品質リスクに直結する。**受け付けない＝攻撃面も誤読リスクも構造的に持たない** |

> **設計原則（SoilInvestigationNeighbouringCaseReferenceAgent で確立）**: 受理しない形式は攻撃面を持たない。
> v1 は「顧客が QMS からエクスポートできる構造化データ」に絞り、OCR 領域には踏み込まない。
> KB がゼロでも GMP 記載要素テンプレート準拠のドラフト＋`source_id` 引用は動作する（グレースフルデグレード）。

---

## Design 入場条件 #1 — 逸脱分類体系と KB スキーマ

### 逸脱分類体系（初期セット）

再利用の軸は **逸脱カテゴリ × 工程種別 × 5M1E 根本原因分類 × CAPA 型と有効性実績**。
これは**同一テナント（自社サイト）内の検索インデックス**であって、CAPA を無関係な工程へ移植する
一般化ではない。品目・ロット固有の詳細はメタデータとして分離格納する。

| 軸 | 初期値（`config/taxonomy.yaml`） |
|---|---|
| `deviation_category` | `process`（製造工程逸脱） / `equipment`（設備・機器） / `material`（原材料・資材） / `testing`（試験・検査） / `utility`（環境・ユーティリティ） / `documentation`（文書・記録） / `training`（教育訓練） / `cleaning`（清掃・洗浄・交叉汚染） |
| `process_type` | `weighing`（秤量） / `mixing_granulation`（混合・造粒） / `compression_filling`（打錠・充填） / `packaging_labeling`（包装・表示） / `sterilization`（滅菌） / `aseptic`（無菌操作） / `storage_dispatch`（保管・出庫） / `cleaning`（洗浄） / `utility`（用水・空調） |
| `root_cause_class`（5M1E） | `man` / `machine` / `material` / `method` / `measurement` / `environment` |
| `severity_class` | `critical` / `major` / `minor`（GMP 慣行） |
| `capa_type` | `corrective`（是正） / `preventive`（予防） |

分類体系は `config/taxonomy.yaml` に外出しし、**顧客サイトごとに追加・改称できる**（コード変更不要）。
`schema_owner` = 顧客QA 部門。未知の値は `unclassified` に落として `missing_data_flags` に載せる
（推測で埋めない）。

### KB スキーマ（サニタイズ後構造化データのみ）

#### `deviation_cases`

| Field | Type | 備考 |
|---|---|---|
| `deviation_id` | str | KB 内 ID（テナント内一意） |
| `tenant_id` | str | **`{company_id}:{site_id}`**（会社 × 製造サイト） |
| `source_record_id` | str | **元 QMS レコード ID（非改ざん参照）** — 正本との整合追跡用。本エージェントは QMS を書き換えない |
| `occurred_on` | date | 発生**日**（時刻は持たない — 再識別リスク低減） |
| `deviation_category` / `process_type` / `root_cause_class` / `severity_class` | str | 分類体系（上表） |
| `equipment_class` | str | 設備**クラス**（個体識別子ではなく型式・区分） |
| `product_class` | str | 剤形・製品**区分**（品目名ではない） |
| `lot_ref` | str \| null | **既定では格納しない**（`null`）。格納は顧客の統治契約による（#5） |
| `event_summary` | str | **サニタイズ済み**の事象記述（作業者氏名 → 役割表記） |
| `rca_summary` | str | サニタイズ済みの根本原因記述 |
| `recurrence_of` | str \| null | 同型逸脱の再発なら元 `deviation_id` |
| `ingested_at` | datetime | |

#### `capa_records`

| Field | Type | 備考 |
|---|---|---|
| `capa_id` / `tenant_id` | str | |
| `deviation_id` | str | FK → `deviation_cases` |
| `capa_type` | str | `corrective` / `preventive` |
| `action_class` | str | 処置の型（`procedure_revision` / `equipment_repair` / `retraining` / `spec_change` / `monitoring_added` …） |
| `action_summary` | str | サニタイズ済み記述 |
| `owner_role` | str | **役割**（氏名ではない。例:「充填工程責任者」） |
| `implemented_on` | date \| null | |

#### `effectiveness_checks`

| Field | Type | 備考 |
|---|---|---|
| `check_id` / `tenant_id` | str | |
| `capa_id` | str | FK → `capa_records` |
| `checked_on` | date | |
| `effective` | bool \| null | 有効性確認の結果（`null` = 未確認） |
| `recurrence_observed` | bool \| null | 同型逸脱の再発が観測されたか |
| `evidence_note` | str | サニタイズ済み |
| `source` | str | `ingest` / `feedback` |
| `operator_edited` | bool | **改善シグナル** — 人間が修正したか（`feedback` 由来なら true） |

> **原文ファイルは保存しない。** 原本は顧客 QMS にある（正本）。本エージェントの KB は
> **正本性を持たない派生コピー**であり、サニタイズ後の構造化データのみを持つ。

---

## Design 入場条件 #2 — `/ingest` の最小データ要件とパース方式

### 最小データ要件

| 記録種別 | 必須フィールド | 欠落時 |
|---|---|---|
| 逸脱記録 | 逸脱番号（`source_record_id`）・発生日・事象記述 | **reject**（この 3 つが無ければ KB レコードとして成立しない） |
| 逸脱記録（推奨） | 分類・工程・RCA・重大度 | `unclassified` + `missing_data_flags` に記録して INSERT（検索精度は落ちるが蓄積は進む） |
| CAPA 記録 | CAPA 番号・対応する逸脱番号・処置内容 | **reject** |
| 有効性確認記録 | 対応する CAPA 番号・確認日 | **reject** |

### パース方式（`src/services/record_parser.py`）

| 形式 | 方式 |
|---|---|
| CSV | `csv` 標準ライブラリ。**列マッピングは `config/ingest_mapping.yaml`** で顧客ごとに定義（QMS ごとにヘッダ名が異なるため）。数式インジェクション中和は S-2 |
| Excel（.xlsx） | `openpyxl`（`read_only=True`・**数式は評価せず文字列として読む**）。マクロ有効ブック（.xlsm）は **reject** |
| Word（.docx） | ZIP + `word/document.xml` のテキスト抽出（**`defusedxml`** でパース）。マクロ・埋め込みオブジェクト・外部参照は読まない |
| PDF | ⚠️ v1.1 候補（🔴 判断項目）。受理する場合は `/JavaScript` `/JS` `/OpenAction` を含むファイルを **reject** |

**LLM は構造化の補助にのみ使う**（自由記述からの分類推定）。必須フィールドの抽出は決定論で行い、
LLM が失敗しても必須フィールドが取れていれば INSERT は成立する。

---

## Design 入場条件 #4 — GMP 記載要素テンプレート（`config/report_templates/`）

`deviation_investigation.yaml` — 逸脱調査報告書の必須構成要素（PIC/S GMP・GMP 省令の要求に対応）:

| セクション | 内容 | 生成方針 |
|---|---|---|
| 1. 逸脱の識別 | 逸脱番号・発生日時・発見者の**役割**・発生工程 | Stage 1（決定論） |
| 2. 事象の記述 | 何が起きたか・時系列 | Stage 3（LLM・`source_id` 引用必須） |
| 3. 影響を受けた対象 | 製品区分・工程・設備クラス | Stage 1 |
| 4. 応急措置 | 即時に取った措置 | Stage 3（入力になければ `missing_data_flags`） |
| 5. 調査・根本原因分析 | 5 Whys / 5M1E による RCA **仮説** | Stage 3（**仮説として提示・断定しない**） |
| 6. 影響評価 | **過去類似事例の事実想起＋要確認事項リスト**のみ | Stage 3（**品質判定は生成しない**） |
| 7. 類似逸脱・再発リスク | 同型逸脱の履歴・CAPA 有効性実績 | Stage 2 + Stage 4（決定論算出） |
| 8. 根拠マップ | 記述 → `source_id` / 過去逸脱 ID の対応表 | Stage 3/4（必須構造） |

`capa_plan.yaml` — CAPA 計画の必須構成要素: 是正処置 / 予防処置 / 実施責任**役割** / 期限 /
有効性確認の方法と基準 / 過去の同型 CAPA の有効性実績（根拠付き）。

**セクション 6（影響評価）には品質判定・出荷可否に相当するフィールドを一切定義しない**（S-3 第 1 層）。

---

## State Definition

`src/schemas/state.py` — `AgentState` を継承した flat TypedDict。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` | ✅ |
| `tenant_id` | `str` | `{company_id}:{site_id}` — **fail-closed で解決** | ✅ |
| `validated_input` | `str` | 内側 graph への JSON 直列化入力 | ✅ |
| `payload_ref` | `str` | **生の逸脱記録本文への不透明参照**（本文は State に載せない） | ✅ |
| `gate_kind` | `str` | **S-3 の経路宣言**（`ingest` / `draft` / `feedback`） | ✅ |
| `result` | `dict` | 報告書・CAPA ドラフト | invoke |
| `evidence_map` | `list[dict]` | 記述 → 根拠 の対応表（S-3 の照合対象） | invoke |
| `cited_source_ids` | `list[str]` | LLM が引用した ID 集合 | invoke |
| `retrieved_records` | `list[dict]` | Stage 1/2 が返した**実レコード**（`source_id` + 照合用属性。**S-3 照合の正本**）。生ナラティブ本文は載せない | invoke |
| `recurrence_flags` | `list[dict]` | 再発リスクフラグ（決定論算出） | invoke |
| `missing_data_flags` | `list[str]` | 欠落データ | — |
| `requires_human_review` | `bool` | 根拠対応が不完全な主張がある場合 `true` | — |
| `degradation_reason` | `str` | 縮退理由（KB ゼロ・LLM 不在等） | — |
| `formatted_output` | `dict` | post_process の最終出力 | — |

**State Constraints（必須）:**
- flat TypedDict のみ（プリミティブ + JSON 直列化可能な型）。ADR-005 の禁止型は使わない
- **JWT / API キー / 認証情報を State に入れない**（checkpoint は DB に永続化される）
- `InvocationContext` は **`InvocationContext.from_state(state)`** で取得する。
  **直接構築（`InvocationContext(...)`）は禁止**（`NullProvider` が束縛され `ctx.secrets.require()` が常に raise する）
- **逸脱記録・報告書の生テキストを State に置かない** — `payload_ref`（TTL 付き transient store の
  不透明参照）で間接化する（SubcontractorQuotationDisputeHistory `TransientPayloadStore` パターン）。実体解決は service 層 /
  API アダプタのみが行う
- `status` は必ず **`AgentStatus.<X>.value`**（文字列）で返す（bare Enum を返さない）
- **HITL 系フィールド（`hitl_draft` / `hitl_feedback` / `hitl_status` / `hitl_metadata` / `hitl_count` /
  `hitl_allowed` / `subgraph_thread_id`）は `AgentState` から継承する。本テンプレートで再宣言しない**
  （フレームワークが許可する HITL 7 フィールド）。書き込んでよいのは **`hitl_draft` のみ**
  （冪等性ガード。`hitl_count` / `hitl_allowed` は framework / Gateway 専用で**テンプレートから書かない**）

---

## Framework Utilization

### Shared Components Used

- [x] `InvocationContext`（`from_state()` 経由。correlation_id / session_id / permissions / **tenant 解決の入力**）
- [x] `SecurityViolationError`
- [x] S-2: `_extra_security_gate_input()` — 入力チャネルのサニタイズ
- [x] S-3: `_extra_security_gate_output()` — **Provenance 検証（GraphNode）** + **品質断定ガード（PostProcessNode）**
- [x] S-4: `emit_trace_event()`（`shared.utils.audit_logger`）— 各 `execute()` 内で最低 1 回のドメインイベント
- [x] `ctx.secrets.require()` — DB 接続情報等（`os.environ` は使わない）

---

## S-1 設計 — Trust Level

| Node | `required_trust_level` | 根拠 |
|---|---|---|
| `PreProcessNode` | `VERIFIED_EXTERNAL` | 外部から逸脱記録ファイルを受け取る |
| `DeviationWorkflowGraphNode`（main） | `VERIFIED_EXTERNAL` | LLM を呼び、**`ingest` / `feedback` では KB に書き込む** |
| `PostProcessNode` | `VERIFIED_EXTERNAL` | 調査報告書ドラフトを出力する |

- 全 `FunctionNode` サブクラスは **class body で `required_trust_level` を明示宣言**する
  （暗黙の `ANONYMOUS` 継承は不可 — `gate-trust-level-check` / `scripts/check_trust_level.py`）
- **`GraphNode` は既定が `ANONYMOUS`** のため明示宣言が必須（SubcontractorQuotationDisputeHistory で実バグ検出）
- 有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ（他の値は import 時 AttributeError）

---

## S-2 設計 — 入力チャネル・サニタイゼーション

入力チャネル・パターンの **strict** 区分に該当する。
本エージェントは**ファイルアップロードチャネル**（QMS エクスポート）を持つ。

### 🔴 実行順制約 — チャネル判定を `mode` に依存させてはならない

framework の実行順は以下であり、`mode` を書くのは `execute()` である:

```
S-1 → S-4 node_start → S-2 (_extra_security_gate_input) → execute() → S-3 → S-4 node_complete
                        ↑ ゲートはここで走る               ↑ mode はここで初めて書かれる
```

> 🔴 **したがって「S-2 で `mode == "ingest"` なら XX を検査する」という設計は成立しない。**
> ゲート実行時点で `mode` は state に存在せず、分岐が偽になって**検査が丸ごとスキップされる**
> （SoilInvestigationNeighbouringCaseReferenceAgent で実機確認済みの fail-open 実バグ）。

### 正しい設計 — 入力の中身から判別する（`mode` 非依存・fail closed・allowlist）

`PreProcessNode._extra_security_gate_input()` は **state の `mode` を読まない**。生の入力そのものを見て判定する。
共有サニタイザは `src/services/sanitizer.py` に集約する。

| チャネル | 脅威 | 対策 |
|---|---|---|
| **CSV** | 数式インジェクション（`=` `+` `-` `@` 始まりのセルが表計算ソフトで数式として解釈される） | 該当セルを `'` でプレフィックスして中和 |
| **Excel（.xlsx）** | 同上 + マクロ | `openpyxl` は数式を評価しない設定で読む。**.xlsm（マクロ有効）は reject** |
| **Word（.docx）** | XML 外部実体（XXE）・埋め込みオブジェクト | **`defusedxml`**（`forbid_entities=True` / `forbid_external=True`）。マクロ・埋め込みオブジェクトは読まない |
| **PDF**（受理する場合） | `/JavaScript` `/JS` `/OpenAction` | 検出したら **reject**（除去ではなく拒否 — 逸脱記録の完全性を毀損しないため） |
| **未知の形式** | — | 🚫 **allowlist で reject**（素通りさせない） |
| **巨大ペイロード** | checkpoint 肥大・DoS | 1 ファイル・1 バッチのサイズ上限（`config/config.yaml`） |
| **自由記述フィールド** | プロンプトインジェクション（事象記述・備考欄が Stage 3 の LLM プロンプトに入る） | untrusted として扱い、**指示文とデータを分離**。長さ上限を設ける |

**形式判定は入力の中身（マジックバイト / MIME スニッフィング）から行う**。
クライアント申告の `Content-Type` やファイル拡張子は信用しない。

### 従業員 PII サニタイズ（人事機微への配慮）

逸脱記録には**作業者氏名・従業員番号**が含まれる。これらを **KB に残さない**。

- **役割表記への置換**が既定動作（例:「山田太郎」→「充填工程担当者A」）。同一記録内で同一人物には
  同一の役割トークンを割り当て、記述の一貫性を保つ
- **解決不能なら reject**（fail closed）
- ⚠️ **SDK の `pii_detector` は日本語氏名を検知しない**（日本語直後の `\b` が失敗する — CorporateGroupTripPlannerAgent 実測）。
  **`detect_pii` 単体に依存してはならない。** `src/services/pii_sanitizer.py` に日本語補完サニタイザを実装し、
  **① S-2 入力時 ② KB 書込直前 ③ S-3 出力再スキャン ④ HITL resume 応答**の 4 点で適用する

`FunctionNode._security_gate_input()` は `@final` — **override 禁止**（class 定義時に `TypeError`）。
拡張は `_extra_security_gate_input()` フックのみ。

---

## S-3 設計 — 二重ゲート（**critical**）

### 第 1 層 — Provenance ID 機械検証（`DeviationWorkflowGraphNode._extra_security_gate_output()`）

**LLM が引用した過去逸脱 ID・CAPA ID・`source_id` が実在し、かつ主張が実レコードと整合するかを機械照合する。**

#### 実装位置は **subgraph 境界**（`PostProcessNode` に置いてはならない）

`GraphNode.merge_output()` が outer state へ書き戻すキーしか post_process には届かない。
照合の**正本である `retrieved_case_ids`（Stage 2 の実在事例）**を outer state に昇格させない限り、
post_process では検証できない。

`BaseNode.__call__()` は `result = self._security_gate_output(result)` を**無条件に実行**し、
`GraphNode._security_gate_output()` は既定スキャンこそ no-op だが **`_extra_security_gate_output()` へ委譲する**
（framework 1.0.0 実機検証済み）。したがって `GraphNode` サブクラスが `_extra_*` を override すれば確実に実行される。

#### 🚫 fail open の NG 例（写経しないこと）

```python
# 🚫 NG — 経路を「キーの有無」で推測し、欠落を空リストで握りつぶしている
def merge_output(self, state, sub_result):
    return {"result": ...,
            "cited_source_ids":   sub_result.get("cited_source_ids", []),    # 🚫 欠落→[] に化ける
            "retrieved_case_ids": sub_result.get("retrieved_case_ids", [])}

def _extra_security_gate_output(self, result):
    if "cited_source_ids" not in result:   # 🚫 欠落を「検査不要」と解釈している
        return result
```

**なぜ NG か**: Stage 3 がフィールドを落としただけで Provenance 検証が丸ごと無効化される
（バグでも攻撃でも同じ結果になる）。**欠落を「検査不要」と解釈してはならない。**

#### ✅ 正しい実装 — 経路を明示宣言（`gate_kind`）し、宣言が無ければ error に倒す

```python
def merge_output(self, state, sub_result):
    mode = state.get("mode")
    if mode == "ingest":
        return {"gate_kind": "ingest",
                "ingest_summary": sub_result.get("ingest_summary", {}),
                "status": sub_result.get("status")}
    if mode == "feedback":
        return {"gate_kind": "feedback",
                "feedback_summary": sub_result.get("feedback_summary", {}),
                "status": sub_result.get("status")}
    return {                                        # invoke = ドラフト生成経路
        "gate_kind": "draft",                       # ← 経路を明示宣言
        "result":            sub_result.get("output"),
        "status":            sub_result.get("status"),
        "evidence_map":      sub_result.get("evidence_map"),        # ← .get(…, []) で握りつぶさない
        "cited_source_ids":  sub_result.get("cited_source_ids"),    # ← 欠落は None のまま渡す
        # 🔴 照合の正本そのものを昇格する（ID だけでは属性照合ができない）
        "retrieved_records": sub_result.get("retrieved_records"),
        "recurrence_flags":  sub_result.get("recurrence_flags"),
    }

def _extra_security_gate_output(self, result: dict) -> dict:
    # 上流エラーのパススルー（error は業務結果を持たない）
    if result.get("status") == AgentStatus.ERROR.value:
        return result

    kind = result.get("gate_kind")
    if kind in ("ingest", "feedback"):
        return result                      # LLM 出力も引用も存在しない（正当な適用外）

    if kind != "draft":                    # ← 宣言が無い/未知 = 想定外の経路 → fail closed
        emit_trace_event("provenance_gate_error", {"reason": "missing gate_kind"}, result)
        raise SecurityViolationError("S-3 gate: gate_kind is not declared")

    cited    = result.get("cited_source_ids")
    records  = result.get("retrieved_records")     # [{"source_id": ..., <属性...>}, ...]
    evidence = result.get("evidence_map")
    if cited is None or records is None or evidence is None:   # 🔴 欠落は異常であって「検査不要」ではない
        emit_trace_event("provenance_gate_error",
                         {"reason": "citation fields missing on draft path"}, result)
        raise SecurityViolationError(
            "S-3 gate: draft output must carry evidence_map, cited_source_ids, retrieved_records")

    by_id = {r["source_id"]: r for r in records}   # 照合の正本（Stage 1/2 が返した実レコード）

    # (1) 捏造 ID の遮断 — 引用 ID が Stage 1/2 の出力に実在するか
    unknown = set(cited) - set(by_id)
    if unknown:
        emit_trace_event("provenance_violation", {"unknown_ids": sorted(unknown)}, result)
        raise SecurityViolationError(f"LLM cited non-existent ids: {sorted(unknown)}")

    # (2) 「実在するが無関係な source_id」の遮断 — 主張が引用元レコードの属性と一致するか
    for claim in evidence:
        for sid, asserted in claim["asserted_attrs"].items():
            actual = by_id.get(sid)
            if actual is None:                     # evidence_map 側の引用も実在必須（fail closed）
                emit_trace_event("provenance_violation", {"unknown_ids": [sid]}, result)
                raise SecurityViolationError(f"evidence_map cites non-existent id: {sid}")
            mismatch = {k: (v, actual.get(k)) for k, v in asserted.items() if actual.get(k) != v}
            if mismatch:
                emit_trace_event("provenance_attr_mismatch",
                                 {"source_id": sid, "mismatch": mismatch}, result)
                raise SecurityViolationError(
                    f"claim {claim['claim_id']} misattributes {sid}: {mismatch}")
    return result
```

> 🔴 **照合の正本は ID の集合ではなくレコード実体である。**
> `retrieved_case_ids`（ID だけ）を昇格すると「ID の実在」しか検証できず、
> **「実在 ID を引用しつつ内容を改ざんする」誤帰属を素通りさせる**（レビュー High 指摘）。
> **`retrieved_records`（Stage 1/2 が返した実レコードの構造化配列）を昇格し、
> ゲートはそれを唯一の正本として照合する**。S-3 のためだけに KB を再フェッチしない
> （検索結果と照合対象がずれる二重取得を避ける）。
>
> レコードは**サニタイズ済みの照合用属性のみ**を載せる（`source_id` / `deviation_category` /
> `process_type` / `root_cause_class` / `action_class` / `effective` / `recurrence_observed` …）。
> 生ナラティブ本文は載せない（State 肥大と機微データ露出の回避）。

#### claim → source 対応スキーマ（レビュー指摘 High #1 への対応）

「ID が実在する」だけでは根拠性は保証されない。**主張が当該根拠に実際に支持されているか**を機械検証するため、
Stage 3/4 の出力は `evidence_map` を**必須構造**として持つ:

```json
{
  "claim_id": "C-07",
  "claim_text": "同型の充填工程逸脱（DEV-2024-031）では、洗浄手順の改訂により再発が止まっている",
  "source_ids": ["DEV-2024-031", "CAPA-2024-018"],
  "asserted_attrs": {
    "DEV-2024-031": {"process_type": "compression_filling", "root_cause_class": "method"},
    "CAPA-2024-018": {"action_class": "procedure_revision", "effective": true}
  }
}
```

| 検証 | 不一致時の挙動 |
|---|---|
| 引用 ID が Stage 1/2 出力に**実在しない**（捏造） | 🔴 **fail-closed `raise`** |
| 引用 ID は実在するが、主張が言う**属性が実レコードと不一致**（＝「実在するが無関係な `source_id`」） | 🔴 **fail-closed `raise`**（誤帰属） |
| 数値・ロット番号・工程属性が引用元レコードの該当フィールドと不一致 | 🔴 **fail-closed `raise`** |
| 報告書本文の主張に `evidence_map` エントリが**存在しない**（根拠なしの記述） | 🟡 `requires_human_review = true` を付与し、**HITL レビューへ強制ルーティング**（出力はするが人間確認を必須にする） |

> **役割分担の明文化**: Provenance 検証の守備範囲は「捏造 ID・誤帰属の機械遮断」である。
> **RCA 解釈そのものの正しさは検証できない** — それは根拠マップ経由の人間（QA 担当・QA 責任者）レビューが担保する。

### 第 2 層 — 品質断定ガード（構造 → lexicon の順）

| 層 | 実装 | 内容 |
|---|---|---|
| **層 1（構造）** | **出力スキーマ** | 影響評価欄は「**過去類似事例の事実＋要確認事項リスト**」のみ。**品質判定・出荷可否に相当するフィールド（bool / 数値フラグを含む）を出力スキーマに一切定義しない**。禁止フィールド名の不在を schema テストで assert する（言い換えによる迂回が原理的に不可能） |
| **層 2（lexicon）** | `PostProcessNode._extra_security_gate_output()` + `src/services/quality_verdict_gate.py` | 自由文中の断定表現（「品質への影響なし」「出荷可能」「規格適合」等）を lexicon で検知。**NFKC + casefold 正規化 → 文単位分割 → 該当文を非断定形へ実置換**（例:「過去類似事例では〜（要QA 責任者確認）」）。**全文が該当する場合は `AgentStatus.ERROR.value`**（fail closed） |

- lexicon は `config/quality_verdict_lexicon.yaml`。**`schema_owner` = 顧客QA 部門**・版管理付き
  （SubcontractorQuotationDisputeHistory `legal_output_lexicon` 方式の転用）
- 「テンプレに追記するだけ」は過去のレビュー Critical で捕まった前例がある — **文単位の実置換を実装する**

`FunctionNode._security_gate_output()` は `@final` — **override 禁止**。

---

## テナント統制（fail-closed・検索段まで貫通）

| 箇所 | 挙動 |
|---|---|
| `PreProcessNode.execute()` | `InvocationContext.from_state(state)` から `company_id` / `site_id` を解決 → `tenant_id`。**解決不能なら `status = AgentStatus.ERROR.value`**（fail closed） |
| Stage 2 KB 検索 | 全クエリに `tenant_id` フィルタを**必ず**適用。**既定テナントへのフォールバック禁止・クロステナント学習なし** |
| `ingest` / `feedback` の INSERT/UPDATE | `tenant_id` 必須。他テナントの行は書き換えられない |

PB テストで **2 テナント分の KB をシードし、テナント A のクエリがテナント B の逸脱記録を一切返さないこと**を assert する。

---

## Composition Pattern

- **Pattern**: **GraphNode（subgraph）** — `main` スロットに `DeviationWorkflowGraphNode` を配置
- **Composition target**: `src/graph/domain_workflow_graph.py` の `DeviationWorkflowGraph`（内側・`BaseGraph` 継承）
- **Error propagation strategy**: **`propagate`**（既定）— 内側のエラーは再送出し fail fast。
  GMP 逸脱調査は**誤った部分結果を返すより失敗させるべき**領域である
- **`propagate_hitl`**: **`True`** — HITL は内側 workflow graph の draft レビューで発火するため、
  外側の呼び出し元（QA 担当）まで伝播させる必要がある

### GraphNode の 3 つの `@abstractmethod`

| Method | 実装 |
|---|---|
| `get_subgraph()` | **`__init__` でキャッシュした内側 graph を返す**。毎 `execute()` で新規生成すると HITL resume が無限ループする（BusinessDiningAndEntertainmentPlannerAgent 実測） |
| `extract_input(state)` | `state["validated_input"]`（`mode` / `tenant_id` / `payload_ref` を含めて JSON 直列化した**文字列**）。`BaseGraph.invoke()` は単一 str 入力のため |
| `merge_output(state, sub_result)` | **実行されたモードのフィールドだけを返す**（`None` 埋め禁止）。`gate_kind` を明示宣言（上記 S-3） |

`error_strategy` / `propagate_hitl` / `required_trust_level` は **class body で明示宣言**する。
`InvocationContext` を `GraphNode` 内で手動構築しない（context 伝播は `GraphNode.execute()` が自動で行う）。

---

## HITL 設計（D6 `interrupt()`）

### 設定（`config/config.yaml` = ランタイム / `config/agent.yaml` = 静的マニフェスト）

```yaml
# config/config.yaml — ランタイムパラメータ（マニフェストとの分離契約）
memory_enabled: true      # MANDATORY — checkpointer が無いと interrupt() の pause/resume が成立しない
hitl:
  enabled: true
  max_hitl: 12            # 安全弁（壁打ち回数の上限であって、対話を縛る目的ではない）
```

`config/agent.yaml` にも **トップレベル `hitl.enabled: true`** を宣言する。
scaffold の PB-7 スタブ（`tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py`）と
HITL レビューが `config/agent.yaml` のトップレベル `hitl.enabled` を読むため
（`max_hitl` 等の**チューナブルは `config.yaml` 側のみ**に置き、二重管理しない）。

> ⚠️ **`enabled` フラグだけが 2 ファイルに現れる**（`agent.yaml` = 能力宣言 / `config.yaml` = ランタイム）。
> 片方だけ変更するドリフト（マニフェストは HITL 有効なのに実行時は無効、またはその逆）を防ぐため、
> **`tests/unit/test_config_consistency.py` で 2 ファイルの `hitl.enabled` 一致を assert する**（MR-B）。
> なお `config/agent.yaml` にランタイムを置く運用（既リリースの先行テンプレートは `agent.config:` 配下に
> 全部入れている）と、マニフェスト／ランタイムの分離契約、および PB-7 スタブの読み取り位置（agent.yaml トップレベル）は
> 現状ずれている。**本テンプレートは「宣言 = agent.yaml / チューナブル = config.yaml」で両方を満たす**が、
> scaffold 側の是正は `request:coe` 候補として記録する。

### 挙動

- **発火点**: 内側 workflow graph の `hitl_review` ノード（Stage 3 の後）。報告書セクション単位のドラフトを
  QA 担当に提示し、修正指示を受けて refine するループ
- 🔴 **`interrupt()` は必ず `state.get("hitl_allowed", True)` でガードする**。
  無条件 `interrupt()` は **blocking defect** — `hitl_allowed=False`（親 `GraphNode` からの自動実行）で
  本番デッドロックする。`hitl_allowed=False` のときは人間レビューを飛ばして先へ進む
- **`hitl_draft`** に生成済みドラフトを保存し、resume 時の LLM 再実行を防ぐ（冪等性ガード）
- **`HitlStatus` の 3 分岐をすべて処理する**:

| `HitlStatus` | route 先 | 本テンプレートの処理 |
|---|---|---|
| `APPROVED` | `post_process` | ドラフト確定 → S-3 ゲート → 出力 |
| `CORRECTED` | `post_process` | **人間の修正を反映して refine** してから確定（無視すると修正が黙って捨てられる） |
| `REJECTED` | `finalize` | post_process をスキップし、ドラフト破棄を明示して終了 |

- **resume 応答は無加工で state に入れない** — PII サニタイザを**先に**通す（scrub-first。CorporateGroupTripPlannerAgent 実測）
- 🔴 **inner / outer の checkpointer は別インスタンス**にする。同一 saver を共有すると checkpoint が
  相互汚染し resume が収束しない（BusinessDiningAndEntertainmentPlannerAgent 実測）。inner は `DeviationWorkflowGraphNode.__init__` で
  専用 saver を新規生成し、**`compile(checkpointer=...)` を明示的に呼ぶ**（1.0.0 は自動生成しない）
- **`hitl_count` はテンプレートから書かない**（framework 専用フィールド）
- **最終文責はQA 責任者**（GMP 省令の責任体制と整合）

### API 認可境界（2026-07-14 のレビュー指摘 F-01/F-02 対応）

グラフ内部の `hitl_allowed` ガード（上記）だけでは不十分——**呼び出し元がその値をどう決めるか**の
経路自体が壊れていれば、正規の内部ロジックごと迂回される。この節はその外側 API アダプタ層の契約を定める。

- 🔴 **`hitl_allowed` は caller が指定できてはならない（F-01）**。`src/api/server.py` の `InvokeRequest`
  は `hitl_allowed` フィールドを**持たない**。値は `_stg_smoke_mode()`（`STG_MOCK_MODE` 環境変数、
  デプロイ時にのみ立つフラグ）から一意に決まる。認証済みの通常 caller がリクエストボディに
  `"hitl_allowed": false` を混入させても pydantic が未知フィールドとして無視するため、
  Stage 3→4 の QA 人間レビューをボディ操作で無効化する経路は存在しない
  （回帰ガード: `tests/integration/test_stg_invoke_payload.py::test_invoke_with_caller_supplied_hitl_allowed_false_still_suspends`）
- **`STG_MOCK_MODE`** は STG 検証の smoke 評価専用（`scripts/stg_invoke_evidence.py` が要求する
  「初回 `/invoke` で `status: success`」を、人間レビュー不在のまま満たすための唯一の抜け道）。
  CI の `deploy-stg` ジョブが job レベル変数として立てる（`variables: STG_MOCK_MODE:
  "true"` — GitLab がジョブシェルに自動 export し、同シェル内で起動する `uvicorn` サブプロセスにも
  継承される）。本番デプロイでは **絶対に立てない**（`docs/07_operation_guide.md` 参照）
- 🔴 **`/resume` は入口認証 + テナント所有権検証を必ず通る（F-02）**。修正前は `Request` すら
  受け取らず認可ゼロだった——`thread_id` を知っている匿名 caller が他人の QA 承認/却下/修正を
  注入できた。修正後の契約:
  1. `_resolve_trust()`（`/invoke` と同じ入口認証）。`INVOKE_AUTH_TOKEN` 設定時、トークン欠落/不正 → **401**
  2. `ResumeRequest` に `company_id`/`site_id` を必須化。checkpoint の `tenant_id`
     （PreProcessNode が `"{company_id}:{site_id}"` で書き込む値）と突合し、不一致 → **403**、
     thread_id 不明 → **404**
  3. checkpoint 読み出しは `agent._compiled.get_state(config)`（framework の `AgentBaseGraph.abort()`
     が使う公式 LangGraph API と同一パターン。実機の `agenticstar-agentcore==1.0.0` wheel で確認済み）
  4. 🔴 **`company_id`/`site_id` に `:` を含む値は 403 で拒否**（レビュー起因の追加防御）。
     `tenant_id` は `PreProcessNode` が非エスケープの `f"{company_id}:{site_id}"` で組み立てるため、
     `company_id="ACME:LINE", site_id="12"` は正規テナント `company_id="ACME", site_id="LINE:12"` の
     `tenant_id`（`"ACME:LINE:12"`）と文字列衝突する。コロン拒否でこの曖昧結合の悪用経路を閉じる
  - 回帰ガード: `tests/integration/test_resume_authorization.py`（401 / 403（テナント不一致・コロン衝突）/
    404 / 正規テナント成功の 5 系統）

---

## Design 入場条件 #5 — 派生 KB のデータ統治契約

| 項目 | 契約 |
|---|---|
| **正本性** | 記録の正本（System of Record）は**顧客 QMS**。本エージェントの KB は検索支援用の**正本性を持たない派生コピー**。原文ファイルは保持しない |
| **許可フィールド** | 上記 KB スキーマに列挙したフィールドのみ |
| **禁止フィールド** | 作業者氏名・従業員番号・個人を特定し得る自由記述・原文ファイル本体・認証情報。**品質判定/出荷可否フィールド**も持たない |
| **テナントキー** | `tenant_id = {company_id}:{site_id}`。全テーブルに必須・全クエリでフィルタ |
| **元 QMS への参照** | `source_record_id` を**非改ざん参照**として保持（本エージェントは QMS を書き換えない） |
| **保持期間・訂正・削除** | 顧客 QA が指定。KB レコードは `source_record_id` 単位で削除可能な API を持つ |
| **再識別リスク対処** | 発生**日**までの分解能（時刻なし）・設備は**クラス**・製品は**区分**・ロット番号は既定で非格納・自由記述はサニタイズ後の要約のみ。「ロット × 工程時刻 × 設備 × 自由記述」の組合せによる個人特定を構造的に困難にする |
| **監査** | `ingest` / `feedback` の全書込で `emit_trace_event()`（誰が・どのテナントに・どのレコードを） |
| **導入前提（正直な明記）** | 本エージェントは GxP 記録を直接生成・保管するシステムではないが、**顧客 QA のサプライヤー評価・データインテグリティ評価の対象になり得る**。「バリデーション不要」とは主張しない |

---

## LLM 注入シーム

- `src/graph/graph.py` が `config.get("llm")` で LLM クライアントを取り出し、ノードへ**コンストラクタ注入**する
- **テンプレート内にモデル ID（`claude-...` 等）や具体クライアント実装を書かない**
- LLM 不在時は **deterministic fallback**（テンプレート準拠のドラフト骨子＋`source_id` 引用のみ）で
  `status = AgentStatus.SUCCESS.value` + `degradation_reason` を返す（KB ゼロ時も同様のグレースフルデグレード）

## Import Isolation Confirmation

- [x] Level 0（`agenticstar` SDK）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレートを import しない（正本パターンは**写経**する）

## Test Design（`docs/03_test_spec.md` で詳細化）

| 種別 | 主要項目 |
|---|---|
| TC | TC-01〜TC-08。**TC-08 は `node(state)` で呼ぶ**（`node.execute(state)` の直呼びは S-1 をバイパスして偽の緑になる = blocking defect） |
| PB-1〜PB-6 | 全テンプレ必須 |
| **PB-7** | **HITL 有効のため必須**（auto-waive されない）。`interrupt()` → `GraphInterrupt` 伝播 / **`hitl_allowed=False` ガードで interrupt が発火しない**こと |
| PB（ドメイン） | ① 従業員 PII 混入入力（CSV/Excel/Word 各形式）→ 役割表記置換 or reject ② **KB 全レコードに氏名・従業員番号パターンが存在しない** ③ State/checkpoint に生テキスト非格納（参照 ID のみ） ④ テナント検索分離（A のクエリが B を返さない・解決不能は ERROR） ⑤ **出力スキーマに品質判定フィールドが存在しない** ⑥ 断定表現の lexicon 検知・文単位置換 ⑦ **Provenance: 捏造 ID → raise** ⑧ **「実在するが無関係な `source_id`」（属性不一致）→ raise** ⑨ **`gate_kind` 欠落・引用フィールド欠落 → raise**（fail closed） ⑩ invoke → feedback の E2E 配線（記録した有効性が次の invoke で hit する） |
| unit | `test_config_consistency.py` — `config/agent.yaml` と `config/config.yaml` の `hitl.enabled` が一致すること（ドリフト検知） |

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプライン。自律的な次アクション決定は不要 |
| Composition pattern | Standalone | GraphNode（subgraph） | **GraphNode** | 3 モード × invoke 4 stage を 3 スロットに収めるため、ドメインの複雑さを内側 graph に閉じ込める（Cat 2 標準） |
| Error propagation | propagate | handle | **propagate** | GMP 逸脱調査は誤った部分結果より失敗が正しい |
| S-3 Provenance の実装位置 | PostProcessNode | GraphNode 境界 | **GraphNode 境界** | 照合の正本（Stage 2 出力）が post_process に届かない |
| 品質断定の防止 | lexicon のみ | 構造（スキーマ）+ lexicon | **構造 + lexicon** | lexicon 単層は言い換えで迂回可能（レビュー指摘）。判定フィールド自体を持たない構造制約が最も強い |
| 再発リスクフラグ | LLM 判定 | 決定論算出 | **決定論算出** | 品質に直結する判断を LLM の生成に委ねない。LLM は言語化のみ |
| v1 入力形式 | PDF 含む全形式 | CSV/Excel/Word（構造化エクスポート） | **CSV/Excel/Word**（PDF は 🔴 判断項目） | 受理しない形式は攻撃面を持たない。OCR 誤読は品質リスクに直結 |
| ロット番号の KB 格納 | 格納する | 既定で非格納 | **既定で非格納** | 再識別リスク低減。必要なら顧客の統治契約に従って有効化 |

---

*設計着手時の未決事項のうち、本ドキュメントで解決したものは上記のとおり。PoC 検証計画は導入時に決めるべき事項として残る。*
