"""HCR-C2-062 — domain-specific proof-of-boundary tests.

docs/02_design.md "PB（ドメイン）" list — items ①②③④⑤⑥⑦⑧⑨:
  ① worker-PII input (CSV/Excel/Word) -> role-token replacement or reject
  ② KB records never contain a worker name / employee-number pattern
  ③ State/checkpoint never carries raw narrative text (payload_ref only)
  ④ tenant search isolation (A's query never returns B's records)
  ⑤ output schema has no quality-verdict field
  ⑥ assertive-statement lexicon detection + sentence-level replacement
  ⑦⑧⑨ S-3 Provenance gate fail-closed variants (covered in
      tests/unit/test_main_node.py::TestS3ProvenanceGate — cross-referenced
      here rather than duplicated)
Item ⑩ (invoke->feedback E2E wiring) is covered in
tests/integration/test_graph_e2e.py::TestInvokeToFeedbackWiring.
"""

from __future__ import annotations

import json
import re

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import GmpDeviationCapaGraph
from src.services.kb_service import KBService, TransientPayloadStore

_NAME_LIKE_PATTERN = re.compile(r"山田太郎|鈴木花子")
_EMPLOYEE_NO_PATTERN = re.compile(r"EMP\d{4,}")


def _compiled_graph(payload_store=None, kb_service=None):
    payload_store = payload_store or TransientPayloadStore()
    graph = GmpDeviationCapaGraph(
        config={
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 12},
            "payload_store": payload_store,
            "kb_service": kb_service,
        }
    )
    graph.compile(checkpointer=InMemorySaver())
    return graph, payload_store


def _ctx(thread_id: str) -> InvocationContext:
    return InvocationContext(session_id=thread_id, thread_id=thread_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


class TestPB_WorkerPiiIngestSanitization:
    """① worker-PII input -> role-token replacement (never reaches the KB)."""

    def test_csv_worker_name_sanitized_before_kb_write(self, fake_db):
        kb = KBService(db=fake_db)
        store = TransientPayloadStore()
        csv_bytes = (
            "Deviation Number,Date Occurred,Event Description\n" "DEV-1,2026-01-01,山田太郎さんが確認し異常を発見した\n"
        ).encode()
        ref = store.put_bytes(csv_bytes)
        graph, _ = _compiled_graph(payload_store=store, kb_service=kb)

        result = graph.invoke(
            "ingest",
            ctx=_ctx("pb-pii-1"),
            input_context={
                "mode": "ingest",
                "company_id": "ACME",
                "site_id": "SITE1",
                "_ingest_record_kind": "deviation",
                "payload_ref": ref,
            },
        )
        assert result["status"] == "success"

        # ② KB rows must never contain the raw name pattern.
        for _query, params in fake_db.executed:
            if params is None:
                continue
            for value in params.values():
                if isinstance(value, str):
                    assert not _NAME_LIKE_PATTERN.search(value), f"worker name leaked into KB write: {value!r}"


class TestPB_StateNeverCarriesRawNarrative:
    """③ State/checkpoint never carries the raw payload — only payload_ref."""

    def test_outer_state_result_has_no_raw_bytes_reference(self):
        store = TransientPayloadStore()
        csv_bytes = "Deviation Number,Date Occurred,Event Description\nDEV-1,2026-01-01,test event\n".encode()
        ref = store.put_bytes(csv_bytes)
        graph, _ = _compiled_graph(payload_store=store)

        result = graph.invoke(
            "ingest",
            ctx=_ctx("pb-state-1"),
            input_context={"mode": "ingest", "company_id": "ACME", "site_id": "SITE1", "payload_ref": ref},
        )
        serialized = json.dumps(result, default=str)
        assert "Deviation Number" not in serialized  # raw CSV header text never leaks into output
        assert csv_bytes.decode() not in serialized


class TestPB_TenantSearchIsolation:
    """④ tenant search isolation — 2-tenant KB seed, A's query never returns B."""

    def test_two_tenant_seed_isolated_search(self):
        class TwoTenantFakeDB:
            def __init__(self):
                self._by_tenant = {
                    "ACME:SITE1": [
                        {
                            "source_id": "dev_acme",
                            "deviation_category": "process",
                            "process_type": "cleaning",
                            "root_cause_class": "method",
                        }
                    ],
                    "OTHER:SITE9": [
                        {
                            "source_id": "dev_other",
                            "deviation_category": "process",
                            "process_type": "cleaning",
                            "root_cause_class": "method",
                        }
                    ],
                }

            def execute(self, q, params=None):
                return None

            def fetch_many(self, q, params=None):
                tenant = (params or {}).get("tenant_id", "")
                return self._by_tenant.get(tenant, [])

            def fetch_one(self, q, params=None):
                return None

        kb = KBService(db=TwoTenantFakeDB())
        result_a = kb.search_similar_deviations("ACME:SITE1", "process", "cleaning", "method")
        result_b = kb.search_similar_deviations("OTHER:SITE9", "process", "cleaning", "method")

        assert [r["source_id"] for r in result_a] == ["dev_acme"]
        assert [r["source_id"] for r in result_b] == ["dev_other"]
        assert "dev_other" not in [r["source_id"] for r in result_a]
        assert "dev_acme" not in [r["source_id"] for r in result_b]

    def test_unresolvable_tenant_is_error_not_fallback(self):
        graph, _ = _compiled_graph()
        result = graph.invoke(
            "incident",
            ctx=_ctx("pb-tenant-fail"),
            input_context={"mode": "invoke", "free_text": "test"},  # no company_id/site_id
        )
        assert result["status"] == "error"


class TestPB_OutputSchemaNoQualityVerdictField:
    """⑤ output schema never defines a quality-verdict/shippability field —
    structural (layer-1) guarantee, checked at the PostProcessNode level."""

    def test_report_templates_declare_no_verdict_fields(self):
        import pathlib

        import yaml

        template_path = (
            pathlib.Path(__file__).parents[2] / "config" / "report_templates" / "deviation_investigation.yaml"
        )
        data = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        all_fields: list[str] = []
        for section in data.get("sections", []):
            all_fields.extend(section.get("fields", []))
        prohibited = {"quality_verdict", "shippability", "release_decision", "pass_fail"}
        assert not (prohibited & set(all_fields))

    def test_post_process_rejects_prohibited_field_if_injected(self):
        from framework.schemas.agent_status import AgentStatus

        from src.nodes.post_process_node import PostProcessNode

        node = PostProcessNode()
        state = {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "result": {"section_6": {"quality_verdict": "acceptable"}},
            "node_history": [],
            "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestPB_AssertiveLexiconDetection:
    """⑥ assertive-statement lexicon detection + sentence-level replacement."""

    def test_shippability_assertion_neutralized_end_to_end(self):
        from framework.schemas.agent_status import AgentStatus

        from src.nodes.post_process_node import PostProcessNode

        node = PostProcessNode()
        state = {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "result": {"impact": "類似逸脱の記録がある。出荷可能と判断する。"},
            "node_history": [],
            "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "出荷可能" not in result["formatted_output"]["report"]["impact"]
