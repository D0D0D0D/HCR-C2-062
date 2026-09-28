"""AgentCore Platform v1.0 — HCR-C2-062: ModeRouterNode (inner graph entry)

docs/02_design.md Data Flow (inner graph): mode is already resolved by the
OUTER PreProcessNode and carried in the envelope
(state["validated_input"] = JSON {"mode", "tenant_id", "payload_ref",
"free_text"}) — this node restores it onto the INNER
DeviationWorkflowState so route_mode() can read it. This is the inner
graph's sole restoration point (mirrors BusinessDiningAndEntertainmentPlannerAgent's context_restore_node
pattern).
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_MODES = ("ingest", "invoke", "feedback")


class ModeRouterNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        raw = state.get("user_input") or state.get("validated_input", "")
        try:
            envelope = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            envelope = {}

        mode = envelope.get("mode")
        if mode not in _VALID_MODES:
            emit_trace_event("mode_router_invalid_mode", {"mode": mode}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"ModeRouterNode: invalid mode '{mode}'"]}

        emit_trace_event("mode_router_restored", {"mode": mode}, state)

        return {
            "mode": mode,
            "tenant_id": envelope.get("tenant_id", ""),
            "payload_ref": envelope.get("payload_ref", ""),
            "validated_input": envelope.get("free_text", ""),
            "_ingest_record_kind": envelope.get("_ingest_record_kind", "deviation"),
            "status": AgentStatus.SUCCESS.value,
        }
