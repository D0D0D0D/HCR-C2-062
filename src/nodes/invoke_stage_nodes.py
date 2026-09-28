"""AgentCore Platform v1.0 — HCR-C2-062: inner invoke-branch stage nodes.

docs/02_design.md "invoke の 4 ステージ" — Stage1(決定論構造化) →
Stage2(決定論検索・テナント限定) → Stage3(LLM調査ドラフト) →
[hitl_review] → Stage4(LLM+決定論 CAPA + 決定論再発リスク)。

Each stage is a thin FunctionNode wrapping src/services/investigation_pipeline.py
(pure/testable business logic) — nodes themselves only shuttle state and
emit S-4 audit events.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.base_llm import BaseLLM
from shared.utils.audit_logger import emit_trace_event

from src.services.investigation_pipeline import (
    stage1_structure_incident,
    stage2_search_similar,
    stage3_investigation_draft,
    stage4_capa_draft,
    stage4_recurrence_flags,
)
from src.services.kb_service import KBService, TransientPayloadStore
from src.services.record_parser import parse_docx_text_heuristic


class Stage1StructureNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, payload_store: TransientPayloadStore | None = None) -> None:
        super().__init__()
        self._payload_store = payload_store or TransientPayloadStore()

    def execute(self, state: AgentState) -> dict:
        payload_ref = state.get("payload_ref")
        sanitized = self._payload_store.get_sanitized(payload_ref) if payload_ref else None

        record: dict = {}
        if sanitized is not None:
            rows = getattr(sanitized, "sanitized_rows", None) or []
            text = getattr(sanitized, "sanitized_text", None)
            if rows:
                record = dict(rows[0])
            elif text:
                parsed = parse_docx_text_heuristic(text)
                record = parsed.fields if parsed.accepted else {}

        if not record:
            # No file payload — fall back to free_text-derived minimal record
            # (invoke callers may submit a plain-text incident description).
            record = {"event_summary": state.get("validated_input", "")}

        structured = stage1_structure_incident(record)
        emit_trace_event("stage1_structured", {"element_count": len(structured)}, state)

        return {
            "structured_incident": structured,
            "_stage1_record": record,
            "status": AgentStatus.SUCCESS.value,
        }


class Stage2SearchNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: KBService | None = None, top_k: int = 8) -> None:
        super().__init__()
        self._kb_service = kb_service
        self._top_k = top_k

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        tenant_id = state.get("tenant_id", "")
        if not tenant_id:
            return {"status": AgentStatus.ERROR.value, "error_log": ["Stage2SearchNode: tenant_id missing (fail closed)"]}

        record = state.get("_stage1_record", {})
        if self._kb_service is None:
            search_result = {"similar_deviations": [], "capa_effectiveness": [], "retrieved_records": []}
            degradation = "KBService not wired — search skipped (graceful degradation)"
        else:
            search_result = stage2_search_similar(
                self._kb_service,
                tenant_id=tenant_id,
                deviation_category=record.get("deviation_category"),
                process_type=record.get("process_type"),
                root_cause_class=record.get("root_cause_class"),
                top_k=self._top_k,
            )
            degradation = None if search_result["similar_deviations"] else "no similar past deviations found in KB (cold start)"

        emit_trace_event(
            "stage2_searched",
            {"tenant_id": tenant_id, "similar_count": len(search_result["similar_deviations"])},
            state,
        )

        # 🔴 S-3 Provenance gate source of truth: retrieved_records is the
        # UNION of Stage 1's own structured incident elements (so Stage 3
        # may legitimately cite the incident's own source_ids, e.g. its own
        # event_summary) AND Stage 2's KB matches (docs/02_design.md
        # "retrieved_records — Stage 1/2 が返した実レコード"). Building this
        # union here (rather than in merge_output) keeps the inner graph's
        # get_output() a straight passthrough.
        structured_incident = state.get("structured_incident", [])
        retrieved_records = list(structured_incident) + list(search_result["retrieved_records"])

        result = {**search_result, "retrieved_records": retrieved_records, "status": AgentStatus.SUCCESS.value}
        if degradation:
            result["degradation_reason"] = degradation
        return result


class Stage3DraftNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: BaseLLM | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        structured_incident = state.get("structured_incident", [])
        similar_deviations = state.get("similar_deviations", [])

        stage3_result = stage3_investigation_draft(structured_incident, similar_deviations, self._llm)

        emit_trace_event(
            "stage3_drafted",
            {"cited_count": len(stage3_result["cited_source_ids"]), "degraded": bool(stage3_result.get("degradation_reason"))},
            state,
        )

        result: dict = {
            "investigation_draft": stage3_result["investigation_draft"],
            "evidence_map": stage3_result["evidence_map"],
            "cited_source_ids": stage3_result["cited_source_ids"],
            "status": AgentStatus.SUCCESS.value,
        }
        if stage3_result.get("degradation_reason"):
            existing = state.get("degradation_reason", "")
            result["degradation_reason"] = (
                f"{existing}; {stage3_result['degradation_reason']}" if existing else stage3_result["degradation_reason"]
            )
        # requires_human_review: any evidence_map claim lacking source_ids
        # forces HITL routing (docs/02_design.md claim→source schema table).
        unattributed = any(not claim.get("source_ids") for claim in stage3_result["evidence_map"])
        result["requires_human_review"] = unattributed
        return result


class Stage4CapaNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: BaseLLM | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        capa_effectiveness = state.get("capa_effectiveness", [])
        recurrence_flags = stage4_recurrence_flags(capa_effectiveness)  # deterministic, LLM never judges this

        capa_result = stage4_capa_draft(
            structured_incident=state.get("structured_incident", []),
            capa_effectiveness=capa_effectiveness,
            recurrence_flags=recurrence_flags,
            llm=self._llm,
        )

        emit_trace_event("stage4_capa_drafted", {"recurrence_flag_count": len(recurrence_flags)}, state)

        evidence_map = list(state.get("evidence_map", [])) + capa_result["evidence_map_delta"]
        cited_source_ids = list(state.get("cited_source_ids", [])) + capa_result["cited_source_ids_delta"]

        output = {
            "investigation_report": state.get("investigation_draft", {}),
            "capa_plan": capa_result["capa_draft"],
            "evidence_map": evidence_map,
            "cited_source_ids": cited_source_ids,
            "recurrence_flags": recurrence_flags,
            "missing_data_flags": state.get("missing_data_flags", []),
        }

        return {
            "capa_draft": capa_result["capa_draft"],
            "recurrence_flags": recurrence_flags,
            "evidence_map": evidence_map,
            "cited_source_ids": cited_source_ids,
            "output": output,
            "gate_kind": "draft",  # S-3 Provenance gate path declaration
            "status": AgentStatus.SUCCESS.value,
        }
