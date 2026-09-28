"""AgentCore Platform v1.0 — HCR-C2-062: PreProcessNode

docs/02_design.md S-2 設計 / テナント統制:
  - Mode routing (ingest/invoke/feedback) + tenant fail-closed resolution.
  - S-2 input-channel gate (_extra_security_gate_input) does NOT read
    `mode` — mode is written by execute(), so at gate time it does not yet
    exist in state; branching on it is fail-open (SoilInvestigationNeighbouringCaseReferenceAgent). Channel is
    instead sniffed from the raw payload content.
  - Raw payload never enters State — the API adapter deposits it into a
    TransientPayloadStore before graph invocation; only `payload_ref`
    (opaque reference) travels through State/checkpoint (see
    src/services/kb_service.py TransientPayloadStore).
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.kb_service import TransientPayloadStore
from src.services.sanitizer import sanitize_input, truncate_free_text

_VALID_MODES = ("ingest", "invoke", "feedback")


class PreProcessNode(FunctionNode):
    """Mode routing + tenant resolution (fail-closed) + S-2 input gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        payload_store: TransientPayloadStore | None = None,
        max_file_bytes: int = 10_485_760,
        max_free_text_chars: int = 20_000,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store or TransientPayloadStore()
        # Config values are constructor-injected (config/config.yaml's
        # ingest.max_file_bytes / ingest.max_free_text_chars) — nodes have
        # no direct access to graph.config, so graph.py reads config.yaml
        # and passes these through (docs/02_design.md config split contract).
        self._max_file_bytes = max_file_bytes
        self._max_free_text_chars = max_free_text_chars

    # ------------------------------------------------------------------
    # S-2 — input-channel gate. MUST NOT branch on `mode` (not yet written).
    # ------------------------------------------------------------------
    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        # payload_ref does not exist as a top-level state key yet on the
        # FIRST pass through this graph — AgentBaseGraph.invoke() only seeds
        # `user_input`/`input_context`/framework bookkeeping into initial
        # state; `payload_ref` is written by THIS node's own execute(),
        # which has not run yet when the S-2 gate fires (S-2 runs BEFORE
        # execute() — see docs/02_design.md 実行順制約). The API adapter
        # therefore places payload_ref inside input_context, and this gate
        # reads it from there. On a RETRY re-entry (pre_process re-executed
        # after a prior pass), the top-level state["payload_ref"] set by the
        # earlier execute() is also honored so retries do not need the
        # adapter to resupply input_context.
        input_context = state.get("input_context", {}) or {}
        payload_ref = state.get("payload_ref") or input_context.get("payload_ref")
        raw_bytes = self._payload_store.get_bytes(payload_ref) if payload_ref else None

        # No file payload (e.g. pure structured-JSON invoke request without
        # an attached QMS export) -> nothing to sniff, pass through.
        if raw_bytes is None:
            return state

        result = sanitize_input(raw_bytes, max_file_bytes=self._max_file_bytes)
        if not result.accepted:
            state = dict(state)
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = list(state.get("error_log", [])) + [
                f"PreProcessNode S-2: input rejected — {result.reject_reason}"
            ]
            return state

        # Stash the sanitized decode back into the transient store under the
        # SAME ref so downstream nodes read the sanitized version, never the
        # raw bytes.
        self._payload_store.put_sanitized(payload_ref, result)
        return state

    # ------------------------------------------------------------------
    def execute(self, state: AgentState) -> dict:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {}) or {}

        if state.get("status") == AgentStatus.ERROR.value:
            # S-2 already rejected — nothing more to do.
            return {}

        mode = input_context.get("mode") or _infer_mode(user_input)
        if mode not in _VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unknown mode '{mode}' (expected one of {_VALID_MODES})"],
            }

        # ── tenant fail-closed resolution ────────────────────────────────
        ctx = InvocationContext.from_state(state)
        company_id = input_context.get("company_id", "")
        site_id = input_context.get("site_id", "")
        if not company_id or not site_id:
            emit_trace_event(
                "tenant_resolution_failed",
                {"reason": "missing company_id/site_id", "correlation_id": ctx.correlation_id},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: tenant resolution failed (company_id/site_id missing) — fail closed"],
            }
        tenant_id = f"{company_id}:{site_id}"

        raw_text = input_context.get("free_text", "")
        truncated_text, was_truncated = truncate_free_text(raw_text, self._max_free_text_chars)

        # Same lookup order as the S-2 gate: top-level state (RETRY re-entry)
        # first, then input_context (the adapter's first-pass channel).
        payload_ref = state.get("payload_ref") or input_context.get("payload_ref")
        if not payload_ref and input_context.get("raw_payload"):
            # Direct/test callers that pass raw bytes through input_context
            # instead of pre-depositing into the payload store.
            raw = input_context.get("raw_payload") or b""
            payload_ref = self._payload_store.put_bytes(raw)

        envelope = {
            "mode": mode,
            "tenant_id": tenant_id,
            "_ingest_record_kind": input_context.get("_ingest_record_kind", "deviation"),
            "payload_ref": payload_ref or "",
            "free_text": truncated_text,
        }

        emit_trace_event(
            "pre_process_validated",
            {"mode": mode, "tenant_id": tenant_id, "free_text_truncated": was_truncated},
            state,
        )

        return {
            "mode": mode,
            "tenant_id": tenant_id,
            "payload_ref": payload_ref or "",
            "validated_input": json.dumps(envelope, ensure_ascii=False, default=str),
            "status": AgentStatus.SUCCESS.value,
        }


def _infer_mode(user_input: str) -> str | None:
    """Fallback mode inference from a plain user_input string (e.g. direct
    /invoke callers that pass mode inline as 'mode:invoke ...')."""
    if not user_input:
        return None
    lowered = user_input.strip().lower()
    for candidate in _VALID_MODES:
        if lowered.startswith(candidate):
            return candidate
    return None
