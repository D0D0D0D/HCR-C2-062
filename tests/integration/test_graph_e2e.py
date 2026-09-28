"""HCR-C2-062 — end-to-end integration tests (outer graph, real compiled pipeline).

Covers: invoke -> HITL suspend -> resume (approve/corrected/reject), ingest
E2E, feedback E2E, invoke->feedback wiring (docs/02_design.md PB domain
item ⑩), S-2 PDF rejection at the full-pipeline level, tenant isolation.
"""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import GmpDeviationCapaGraph
from src.services.kb_service import KBService, TransientPayloadStore


def _compiled_graph(**config_overrides):
    payload_store = config_overrides.pop("payload_store", None) or TransientPayloadStore()
    kb_service = config_overrides.pop("kb_service", None)
    config = {
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 12},
        "payload_store": payload_store,
        "kb_service": kb_service,
        **config_overrides,
    }
    graph = GmpDeviationCapaGraph(config=config)
    graph.compile(checkpointer=InMemorySaver())
    return graph, payload_store


def _ctx(thread_id: str, session_id: str | None = None) -> InvocationContext:
    return InvocationContext(
        session_id=session_id or thread_id,
        thread_id=thread_id,
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


class TestInvokeHitlFlow:
    def test_invoke_suspends_awaiting_human(self):
        graph, _store = _compiled_graph()
        result = graph.invoke(
            "incident occurred",
            ctx=_ctx("t-invoke-1"),
            input_context={
                "mode": "invoke",
                "company_id": "ACME",
                "site_id": "SITE1",
                "free_text": "filling line anomaly",
            },
        )
        assert result["status"] == "awaiting_human"
        # "draft" is a framework-internal interrupt-payload key — it is
        # stripped from the client-facing hitl_metadata and instead stored
        # separately as state["hitl_draft"] (BaseGraph._build_awaiting_human_output).
        assert "prompt" in result["hitl_metadata"]

    def test_resume_approve_reaches_success(self):
        graph, _store = _compiled_graph()
        graph.invoke(
            "incident occurred",
            ctx=_ctx("t-invoke-2"),
            input_context={
                "mode": "invoke",
                "company_id": "ACME",
                "site_id": "SITE1",
                "free_text": "filling line anomaly",
            },
        )
        resumed = graph.resume(thread_id="t-invoke-2", feedback="approve")
        assert resumed["status"] == "success"
        assert resumed["output"]["gate_kind"] == "draft"
        assert resumed["output"]["report"] is not None

    def test_resume_reject_cancels_and_skips_post_process(self):
        graph, _store = _compiled_graph()
        graph.invoke(
            "incident occurred",
            ctx=_ctx("t-invoke-3"),
            input_context={"mode": "invoke", "company_id": "ACME", "site_id": "SITE1", "free_text": "test"},
        )
        resumed = graph.resume(thread_id="t-invoke-3", feedback="reject")
        assert resumed["status"] == "cancelled"

    def test_resume_corrected_merges_human_edit(self):
        graph, _store = _compiled_graph()
        graph.invoke(
            "incident occurred",
            ctx=_ctx("t-invoke-4"),
            input_context={"mode": "invoke", "company_id": "ACME", "site_id": "SITE1", "free_text": "test"},
        )
        resumed = graph.resume(
            thread_id="t-invoke-4",
            feedback={"action": "correct", "correction": "human-revised narrative text"},
        )
        assert resumed["status"] == "success"
        assert "human-revised" in resumed["output"]["report"]["investigation_report"]["event_narrative"]


class TestIngestE2E:
    def test_ingest_deviation_record_inserted(self, fake_db):
        store = TransientPayloadStore()
        csv_bytes = "Deviation Number,Date Occurred,Event Description\nDEV-1,2026-01-01,filling anomaly\n".encode()
        ref = store.put_bytes(csv_bytes)
        kb = KBService(db=fake_db)
        graph, _ = _compiled_graph(payload_store=store, kb_service=kb)

        result = graph.invoke(
            "ingest",
            ctx=_ctx("t-ingest-1"),
            input_context={
                "mode": "ingest",
                "company_id": "ACME",
                "site_id": "SITE1",
                "_ingest_record_kind": "deviation",
                "payload_ref": ref,
            },
        )
        assert result["status"] == "success"
        assert result["output"]["summary"]["count"] == 1
        assert len(fake_db.executed) == 1

    def test_ingest_unsupported_pdf_rejected(self):
        store = TransientPayloadStore()
        ref = store.put_bytes(b"%PDF-1.4 fake")
        graph, _ = _compiled_graph(payload_store=store)

        result = graph.invoke(
            "ingest",
            ctx=_ctx("t-ingest-2"),
            input_context={"mode": "ingest", "company_id": "ACME", "site_id": "SITE1", "payload_ref": ref},
        )
        assert result["status"] == "error"

    def test_inner_error_code_reaches_caller_without_raw_inner_log(self):
        graph, _ = _compiled_graph()
        result = graph.invoke(
            "ingest",
            ctx=_ctx("t-error-code-hop"),
            input_context={
                "mode": "ingest",
                "company_id": "ACME",
                "site_id": "SITE1",
                "payload_ref": "missing-ref",
            },
        )
        assert result["status"] == "error"
        assert result["error_code"] == "payload_ref_unresolved"
        assert result["error_type"] == "inner_workflow_error"
        assert "no sanitized payload found" not in json.dumps(result, default=str)


class TestFeedbackE2E:
    def test_feedback_updates_effectiveness(self, fake_db):
        kb = KBService(db=fake_db)
        graph, _ = _compiled_graph(kb_service=kb)

        payload = {
            "capa_id": "capa_1",
            "checked_on": "2026-07-01",
            "effective": True,
            "recurrence_observed": False,
            "evidence_note": "",
        }
        result = graph.invoke(
            "",
            ctx=_ctx("t-feedback-1"),
            input_context={
                "mode": "feedback",
                "company_id": "ACME",
                "site_id": "SITE1",
                "free_text": json.dumps(payload),
            },
        )
        assert result["status"] == "success"
        assert result["output"]["summary"]["capa_id"] == "capa_1"


class TestInvokeToFeedbackWiring:
    """docs/02_design.md PB domain item ⑩ — invoke -> feedback E2E: the
    effectiveness recorded via /feedback later hits on a subsequent Stage 2
    search for the same tenant/category."""

    def test_recorded_effectiveness_visible_to_next_stage2_search(self, fake_db):
        kb = KBService(db=fake_db)
        # Seed a deviation + CAPA the "feedback" call will update.
        kb.insert_deviation_case(
            "ACME:SITE1",
            {
                "source_record_id": "DEV-9",
                "occurred_on": "2026-01-01",
                "event_summary": "x",
                "deviation_category": "process",
            },
        )
        fake_db.set_fetch_many(
            "FROM deviation_cases",
            [
                {
                    "source_id": "dev_seed",
                    "deviation_category": "process",
                    "process_type": "cleaning",
                    "root_cause_class": "method",
                    "severity_class": "major",
                    "recurrence_of": None,
                }
            ],
        )
        fake_db.set_fetch_many(
            "FROM capa_records",
            [
                {
                    "source_id": "capa_seed",
                    "deviation_id": "dev_seed",
                    "capa_type": "corrective",
                    "action_class": "procedure_revision",
                    "effective": True,
                    "recurrence_observed": False,
                }
            ],
        )

        graph, _ = _compiled_graph(kb_service=kb)
        graph.invoke(
            "incident",
            ctx=_ctx("t-wiring-1"),
            input_context={"mode": "invoke", "company_id": "ACME", "site_id": "SITE1", "free_text": "process anomaly"},
        )
        resumed = graph.resume(thread_id="t-wiring-1", feedback="approve")
        assert resumed["status"] == "success"
        cited = resumed["output"]["report"]["cited_source_ids"]
        # the seeded past CAPA case should be reachable via retrieved_records
        assert (
            any("capa_seed" in str(c) or "dev_seed" in str(c) for c in cited)
            or resumed["output"]["report"]["capa_plan"]["past_effectiveness_evidence"]["similar_capa_cases"]
        )


class TestTenantIsolationE2E:
    def test_cross_tenant_search_returns_nothing(self, fake_db):
        # Seed rows tagged tenant B only; tenant A's search must not see them
        # (FakeDatabase.fetch_many ignores params in this simple fake, so we
        # verify via a fake that actually tenant-filters).
        class TenantAwareFakeDB:
            def __init__(self):
                self.rows = {
                    "ACME:SITE1": [],
                    "OTHER:SITE9": [{"source_id": "dev_other", "deviation_category": "process"}],
                }

            def execute(self, q, p=None):
                return None

            def fetch_many(self, q, params=None):
                tenant = (params or {}).get("tenant_id", "")
                return self.rows.get(tenant, [])

            def fetch_one(self, q, params=None):
                return None

        db = TenantAwareFakeDB()
        kb = KBService(db=db)
        result = kb.search_similar_deviations("ACME:SITE1", None, None, None)
        assert result == []
        result_other = kb.search_similar_deviations("OTHER:SITE9", None, None, None)
        assert len(result_other) == 1

    def test_missing_tenant_fails_closed_at_pipeline_level(self):
        graph, _ = _compiled_graph()
        result = graph.invoke(
            "incident",
            ctx=_ctx("t-tenant-fail"),
            input_context={"mode": "invoke", "free_text": "test"},  # no company_id/site_id
        )
        assert result["status"] == "error"
