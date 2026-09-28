"""HCR-C2-062 — deploy/invoke_payload.json contract test.

docs/07_operation_guide.md "STG Sign-off Record": the Stage 5 evidence
script (scripts/stg_invoke_evidence.py) asserts `body.get("status") ==
"success"` on the FIRST /invoke response. Because this template is
HITL-enabled, a normal /invoke call suspends to "awaiting_human" on the
first pass by design -- so the smoke-test SERVER PROCESS must run with
STG_MOCK_MODE=true (a deployment-level flag, never a caller-supplied body
field -- see the F-01 fix in src/api/server.py
`_stg_smoke_mode()`) to reach a terminal "success" state without a human
resuming the session.

This test guards against a future edit to deploy/invoke_payload.json (or
src/api/server.py's hitl_allowed wiring) silently reintroducing the
"awaiting_human != success" STG evidence failure, AND against the F-01
regression of caller-controlled hitl_allowed reappearing in the request
body.
"""

from __future__ import annotations

import json
import pathlib

import pytest


@pytest.fixture
def stg_client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-stg-token")
    monkeypatch.setenv("STG_MOCK_MODE", "true")
    from fastapi.testclient import TestClient

    from src.api.server import app

    return TestClient(app)


def _payload() -> dict:
    path = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestStgInvokePayloadReachesSuccess:
    def test_payload_does_not_declare_caller_controlled_hitl_allowed(self):
        """F-01 regression guard: the smoke payload must NOT carry a caller
        hitl_allowed flag — HITL-skip is decided only by the server process's
        STG_MOCK_MODE env var, never by request body content."""
        payload = _payload()
        assert "hitl_allowed" not in payload

    def test_payload_declares_tenant_fields(self):
        payload = _payload()
        assert payload.get("company_id")
        assert payload.get("site_id")

    def test_invoke_with_standard_payload_reaches_success(self, stg_client):
        """Simulates exactly what scripts/stg_invoke_evidence.py does against
        a server process booted with STG_MOCK_MODE=true: POST /invoke with
        the standard payload + Bearer token, assert status == "success"
        (the evidence script's agent_status_success assertion)."""
        payload = _payload()
        resp = stg_client.post("/invoke", json=payload, headers={"Authorization": "Bearer test-stg-token"})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "success", (
            f"STG evidence script requires status=='success' on the first /invoke — got {body.get('status')!r}. "
            "If HITL now suspends unconditionally, either fix STG_MOCK_MODE propagation or escalate "
            "(this assertion protects the Stage 5 gate)."
        )

    def test_invoke_with_caller_supplied_hitl_allowed_false_still_suspends(self, monkeypatch):
        """F-01 fix verification (review finding, High/merge-blocker):
        a normal authenticated caller (STG_MOCK_MODE unset — i.e. NOT the
        deploy-stg smoke run) POSTing hitl_allowed:false in the body MUST
        NOT be able to disable the mandatory QA human review. pydantic
        silently drops the unknown field, and hitl_allowed is decided
        exclusively by the deployment-level STG_MOCK_MODE flag, so the
        session must still suspend to awaiting_human."""
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-stg-token")
        monkeypatch.delenv("STG_MOCK_MODE", raising=False)
        from fastapi.testclient import TestClient

        from src.api.server import app

        client = TestClient(app)
        payload = {**_payload(), "hitl_allowed": False, "thread_id": "f01-regression-guard"}
        resp = client.post("/invoke", json=payload, headers={"Authorization": "Bearer test-stg-token"})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "awaiting_human", (
            "F-01 regression: a caller-supplied hitl_allowed=false in the request body must be "
            f"ignored (HITL must still fire) — got status={body.get('status')!r}"
        )

    def test_invoke_without_auth_token_env_is_anonymous_and_denied(self, monkeypatch):
        """Sanity check on the OTHER branch of the STG runbook, section 5: when
        INVOKE_AUTH_TOKEN is unset, callers stay ANONYMOUS and are correctly
        denied by the S-1 gate (this template's nodes require
        VERIFIED_EXTERNAL) -- confirms the auth model doc is accurate, not
        aspirational."""
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        from fastapi.testclient import TestClient

        from src.api.server import app

        client = TestClient(app)
        payload = _payload()
        resp = client.post("/invoke", json=payload)
        assert resp.status_code == 200
        assert resp.json().get("status") == "error"
