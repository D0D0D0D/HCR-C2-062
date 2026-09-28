"""HCR-C2-062 — PreProcessNode unit tests.

Covers: TC-08 (trust gate — must call node(state), not execute() directly),
mode routing, tenant fail-closed resolution, S-2 channel gate (mode-blind).
"""

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode
from src.services.kb_service import TransientPayloadStore


def _state(**overrides) -> dict:
    state = {
        "user_input": "",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "caller_id": "",
        "correlation_id": "test-corr",
        "session_id": "test-session",
        "thread_id": "test-thread",
        "trace_id": "test-trace",
        "hitl_allowed": True,
        "node_history": [],
        "error_log": [],
    }
    state.update(overrides)
    return state


class TestPreProcessNodeTrustGate:
    def test_insufficient_trust_denied_via_dunder_call(self):
        """TC-08: must call node(state) via __call__, not execute() directly."""
        node = PreProcessNode()
        state = _state(
            caller_trust_level=TrustLevel.ANONYMOUS.value,
            input_context={"mode": "invoke", "company_id": "A", "site_id": "B"},
        )
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_sufficient_trust_allowed(self):
        node = PreProcessNode()
        state = _state(input_context={"mode": "invoke", "company_id": "ACME", "site_id": "SITE1", "free_text": "test"})
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestPreProcessNodeModeRouting:
    def test_valid_mode_accepted(self):
        node = PreProcessNode()
        for mode in ("ingest", "invoke", "feedback"):
            state = _state(input_context={"mode": mode, "company_id": "ACME", "site_id": "SITE1"})
            result = node(state)
            assert result["status"] == AgentStatus.SUCCESS.value
            assert result["mode"] == mode

    def test_invalid_mode_rejected(self):
        node = PreProcessNode()
        state = _state(input_context={"mode": "not_a_real_mode", "company_id": "ACME", "site_id": "SITE1"})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestPreProcessNodeTenantFailClosed:
    def test_missing_company_id_fails_closed(self):
        node = PreProcessNode()
        state = _state(input_context={"mode": "invoke", "site_id": "SITE1"})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_missing_site_id_fails_closed(self):
        node = PreProcessNode()
        state = _state(input_context={"mode": "invoke", "company_id": "ACME"})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_valid_tenant_resolved(self):
        node = PreProcessNode()
        state = _state(input_context={"mode": "invoke", "company_id": "ACME", "site_id": "SITE1"})
        result = node(state)
        assert result["tenant_id"] == "ACME:SITE1"


class TestPreProcessNodeS2Gate:
    def test_s2_gate_does_not_read_mode(self):
        """docs/02_design.md 🔴: S-2 gate must not branch on state['mode'] —
        it does not exist yet at gate time. Verify passing a state with NO
        mode key at all does not raise/crash the gate (mode-blind by design)."""
        node = PreProcessNode()
        state = {"input_context": {}, "correlation_id": "c1"}
        gated = node._extra_security_gate_input(state)
        assert gated is not None  # no crash — gate is mode-blind

    def test_pdf_payload_rejected(self):
        store = TransientPayloadStore()
        ref = store.put_bytes(b"%PDF-1.4 fake pdf content")
        node = PreProcessNode(payload_store=store)
        state = _state(
            payload_ref=ref, input_context={"mode": "ingest", "company_id": "A", "site_id": "B", "payload_ref": ref}
        )
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_valid_csv_payload_accepted(self):
        store = TransientPayloadStore()
        csv_bytes = "a,b\n1,2\n".encode()
        ref = store.put_bytes(csv_bytes)
        node = PreProcessNode(payload_store=store)
        state = _state(input_context={"mode": "ingest", "company_id": "A", "site_id": "B", "payload_ref": ref})
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # sanitized version replaces raw bytes in the store
        assert store.get_sanitized(ref) is not None

    def test_oversized_payload_rejected(self):
        store = TransientPayloadStore()
        csv_bytes = ("a,b\n" + "1,2\n" * 10000).encode()
        ref = store.put_bytes(csv_bytes)
        node = PreProcessNode(payload_store=store, max_file_bytes=10)
        state = _state(input_context={"mode": "ingest", "company_id": "A", "site_id": "B", "payload_ref": ref})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestPreProcessNodeRequiredTrustLevel:
    def test_required_trust_level_is_valid_enum(self):
        assert PreProcessNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )
