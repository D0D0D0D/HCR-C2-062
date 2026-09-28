"""AgentCore Platform v1.0 — HCR-C2-062: inner feedback-branch nodes.

docs/02_design.md Data Flow (inner graph), feedback branch:
    validate → kb_update_effectiveness

Records CAPA implementation results / effectiveness-check outcomes /
recurrence observations back into effectiveness_checks
(operator_edited=True — the improvement-signal field, docs/02_design.md
KB スキーマ). free-text evidence_note goes through the same worker-PII
sanitizer as ingest (② KB書込直前 application point).
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.kb_service import CapaNotFoundError, KBService
from src.services.pii_sanitizer import sanitize_worker_pii


class FeedbackValidateNode(FunctionNode):
    """Validates the minimum feedback payload (capa_id + checked_on) before
    any KB write (docs/02_design.md 最小データ要件 — 有効性確認記録)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        import json

        raw = state.get("validated_input", "")
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            payload = {}

        capa_id = payload.get("capa_id")
        checked_on = payload.get("checked_on")
        if not capa_id or not checked_on:
            emit_trace_event("feedback_validate_rejected", {"reason": "missing capa_id/checked_on"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["FeedbackValidateNode: capa_id and checked_on are required — reject"],
            }

        evidence_note = payload.get("evidence_note", "")
        if evidence_note:
            sanitized_note, _count, resolvable = sanitize_worker_pii(str(evidence_note))
            if not resolvable:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["FeedbackValidateNode: evidence_note PII unresolvable (fail closed)"],
                }
            payload["evidence_note"] = sanitized_note

        emit_trace_event("feedback_validated", {"capa_id": capa_id}, state)
        return {"_feedback_payload": payload, "status": AgentStatus.SUCCESS.value}


class KbUpdateEffectivenessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: KBService | None = None) -> None:
        super().__init__()
        self._kb_service = kb_service

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        tenant_id = state.get("tenant_id", "")
        if not tenant_id:
            return {"status": AgentStatus.ERROR.value, "error_log": ["KbUpdateEffectivenessNode: tenant_id missing (fail closed)"]}
        if self._kb_service is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["KbUpdateEffectivenessNode: KBService not wired (server startup misconfiguration)"],
            }

        payload = state.get("_feedback_payload", {})
        try:
            result = self._kb_service.update_effectiveness(
                tenant_id=tenant_id,
                capa_id=payload["capa_id"],
                effective=payload.get("effective"),
                recurrence_observed=payload.get("recurrence_observed"),
                evidence_note=payload.get("evidence_note"),
            )
        except CapaNotFoundError:
            # The UPDATE matched no row — unknown capa_id, or one belonging to
            # another tenant. Never report this as recorded: CAPA effectiveness
            # is part of the GMP audit record, and a silent no-op would tell the
            # QA operator the outcome was captured when nothing was written.
            emit_trace_event(
                "kb_effectiveness_update_missed",
                {"tenant_id": tenant_id, "capa_id": payload.get("capa_id")},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "KbUpdateEffectivenessNode: no effectiveness_checks row for the given capa_id "
                    "in this tenant — nothing was recorded"
                ],
            }

        emit_trace_event("kb_effectiveness_updated", {"tenant_id": tenant_id, "capa_id": payload["capa_id"]}, state)

        return {
            "feedback_summary": {
                "capa_id": payload["capa_id"],
                "recorded": result.get("recorded", True),
                "rows_updated": result.get("rows_updated"),
            },
            "gate_kind": "feedback",  # S-3 Provenance gate path declaration
            "status": AgentStatus.SUCCESS.value,
        }
