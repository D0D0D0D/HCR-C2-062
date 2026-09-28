"""HCR-C2-062 — invoke 4-stage pipeline unit tests (src/services/investigation_pipeline.py).

docs/02_design.md "invoke の 4 ステージ": Stage 1/2 は決定論、Stage 3/4 の
LLM 有無で分岐、再発リスクフラグは決定論算出（LLM 判定禁止）。
"""

from __future__ import annotations

from src.services.investigation_pipeline import (
    stage1_structure_incident,
    stage2_search_similar,
    stage3_investigation_draft,
    stage4_capa_draft,
    stage4_recurrence_flags,
)
from src.services.kb_service import KBService


class TestStage1StructureIncident:
    def test_every_element_gets_source_id(self):
        record = {"source_record_id": "DEV-1", "occurred_on": "2026-01-01", "event_summary": "test event"}
        elements = stage1_structure_incident(record)
        assert all("source_id" in e for e in elements)
        assert all(e["source_id"] for e in elements)

    def test_empty_values_skipped(self):
        record = {"source_record_id": "DEV-1", "occurred_on": None, "event_summary": ""}
        elements = stage1_structure_incident(record)
        kinds = {e["kind"] for e in elements}
        assert "occurred_on" not in kinds
        assert "event_summary" not in kinds

    def test_defaults_to_unclassified_kind_present(self):
        record = {"source_record_id": "DEV-1", "deviation_category": "unclassified"}
        elements = stage1_structure_incident(record)
        cat_elem = next(e for e in elements if e["kind"] == "deviation_category")
        assert cat_elem["value"] == "unclassified"


class TestStage2SearchSimilar:
    def test_tenant_scoped_search_returns_records(self, fake_db):
        fake_db.set_fetch_many(
            "FROM deviation_cases",
            [
                {
                    "source_id": "dev_1",
                    "deviation_category": "process",
                    "process_type": "cleaning",
                    "root_cause_class": "method",
                }
            ],
        )
        kb = KBService(db=fake_db)
        result = stage2_search_similar(kb, "ACME:SITE1", "process", "cleaning", "method")
        assert len(result["similar_deviations"]) == 1
        assert result["retrieved_records"]

    def test_no_matches_returns_empty_not_error(self, fake_db):
        kb = KBService(db=fake_db)
        result = stage2_search_similar(kb, "ACME:SITE1", None, None, None)
        assert result["similar_deviations"] == []
        assert result["retrieved_records"] == []


class TestStage3InvestigationDraft:
    def test_no_llm_deterministic_fallback(self):
        structured = [{"source_id": "inc_1", "kind": "event_summary", "value": "something happened"}]
        result = stage3_investigation_draft(structured, [], llm=None)
        assert result["degradation_reason"] is not None
        assert "inc_1" in result["cited_source_ids"]
        # Layer-1 structural guarantee: no quality-verdict-like field ever
        # appears in the draft produced here.
        assert "quality_verdict" not in result["investigation_draft"]
        assert "shippability" not in result["investigation_draft"]

    def test_with_llm_uses_response_content(self, fake_llm):
        structured = [{"source_id": "inc_1", "kind": "event_summary", "value": "something happened"}]
        result = stage3_investigation_draft(structured, [], llm=fake_llm)
        assert result["degradation_reason"] is None
        assert result["investigation_draft"]["event_narrative"]

    def test_llm_failure_falls_back_deterministically(self):
        class BrokenLLM:
            def complete(self, messages):
                raise RuntimeError("LLM unavailable")

        structured = [{"source_id": "inc_1", "kind": "event_summary", "value": "test"}]
        result = stage3_investigation_draft(structured, [], llm=BrokenLLM())
        assert result["degradation_reason"] is not None
        assert "LLM call failed" in result["degradation_reason"]

    def test_llm_cannot_cite_unknown_ids(self, fake_llm):
        """The response parser only accepts ids that are actually present
        in structured_incident/similar_deviations — a fabricated id embedded
        in free-text content must not be picked up as 'cited'."""
        structured = [{"source_id": "inc_real", "kind": "event_summary", "value": "real event"}]

        class FabricatingLLM:
            def complete(self, messages):
                return {"content": "citing inc_fabricated which does not exist", "tool_calls": [], "model": "fake"}

        result = stage3_investigation_draft(structured, [], llm=FabricatingLLM())
        assert "inc_fabricated" not in result["cited_source_ids"]


class TestStage4RecurrenceFlags:
    def test_recurrence_without_effective_capa_is_elevated_risk(self):
        capa_eff = [{"deviation_id": "dev_1", "recurrence_observed": True, "effective": False}]
        flags = stage4_recurrence_flags(capa_eff)
        assert flags[0]["risk_level"] == "elevated"

    def test_recurrence_with_effective_capa_is_moderate_risk(self):
        capa_eff = [
            {"deviation_id": "dev_1", "recurrence_observed": True, "effective": False},
            {"deviation_id": "dev_1", "recurrence_observed": False, "effective": True},
        ]
        flags = stage4_recurrence_flags(capa_eff)
        assert flags[0]["risk_level"] == "moderate"

    def test_no_recurrence_is_low_risk(self):
        capa_eff = [{"deviation_id": "dev_1", "recurrence_observed": False, "effective": True}]
        flags = stage4_recurrence_flags(capa_eff)
        assert flags[0]["risk_level"] == "low"

    def test_empty_input_returns_empty(self):
        assert stage4_recurrence_flags([]) == []

    def test_is_purely_deterministic_no_llm_param(self):
        """Contract check: this function accepts no llm parameter at all —
        recurrence risk must never be an LLM judgment (docs/02_design.md)."""
        import inspect

        sig = inspect.signature(stage4_recurrence_flags)
        assert "llm" not in sig.parameters


class TestStage4CapaDraft:
    def test_no_llm_still_produces_draft(self):
        recurrence_flags = [{"deviation_id": "dev_1", "risk_level": "low"}]
        result = stage4_capa_draft([], [], recurrence_flags, llm=None)
        assert "corrective_action" in result["capa_draft"]
        assert "preventive_action" in result["capa_draft"]

    def test_capa_ids_cited_when_present(self):
        capa_eff = [{"source_id": "capa_1", "action_class": "procedure_revision", "effective": True}]
        result = stage4_capa_draft([], capa_eff, [], llm=None)
        assert "capa_1" in result["cited_source_ids_delta"]
