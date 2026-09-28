"""HCR-C2-062 — POST /resume authorization tests (review finding F-02).

Before the F-02 fix, `/resume` took no `Request` and ran neither the
entry-point auth (`_resolve_trust()`) nor any tenant-ownership check: any
caller who merely learned a `thread_id` could inject an approve / reject /
correction decision into someone else's suspended QA review. This file
proves the fix end-to-end through the real FastAPI app (`src.api.server`),
against the actual outer-graph checkpointer -- not mocks -- because the
authorization boundary being tested IS the wiring between the HTTP layer,
`_resolve_trust()`, and the checkpoint's `tenant_id` field.

Covers (the minimum the review specified):
  - resume without a token -> 401 (entry-point auth)
  - resume from a different tenant -> 403 (tenant-ownership check)
  - resume with the correct reviewer's tenant -> success (regression guard
    that the fix doesn't break the legitimate path)
  - resume against an unknown thread_id -> 404
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-resume-token")
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    from fastapi.testclient import TestClient

    from src.api.server import app

    return TestClient(app)


def _auth_headers(token: str = "test-resume-token") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _suspend_session(client, thread_id: str, company_id: str = "ACME-CORP", site_id: str = "SITE-A") -> dict:
    """Drive a real /invoke call to a HITL suspend and return its response body."""
    resp = client.post(
        "/invoke",
        json={
            "input": "filling line temperature deviation detected",
            "session_id": thread_id,
            "thread_id": thread_id,
            "company_id": company_id,
            "site_id": site_id,
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "awaiting_human", f"fixture precondition failed: {body}"
    return body


class TestResumeEntryPointAuth:
    def test_resume_without_token_is_denied(self, client):
        """No INVOKE_AUTH_TOKEN header supplied -> 401, mirroring /invoke's
        own entry-point auth (F-02 requirement 1)."""
        thread_id = "resume-auth-401"
        _suspend_session(client, thread_id)
        resp = client.post(
            "/resume",
            json={
                "thread_id": thread_id,
                "feedback": "approve",
                "company_id": "ACME-CORP",
                "site_id": "SITE-A",
            },
        )
        assert resp.status_code == 401

    def test_resume_with_wrong_token_is_denied(self, client):
        thread_id = "resume-auth-401-badtoken"
        _suspend_session(client, thread_id)
        resp = client.post(
            "/resume",
            json={
                "thread_id": thread_id,
                "feedback": "approve",
                "company_id": "ACME-CORP",
                "site_id": "SITE-A",
            },
            headers=_auth_headers("wrong-token"),
        )
        assert resp.status_code == 401


class TestResumeTenantOwnership:
    def test_resume_from_different_tenant_is_forbidden(self, client):
        """Caller is authenticated (valid Bearer token) but supplies a
        company_id/site_id that does not match the checkpoint's tenant_id
        -> 403 (F-02 requirement 2: the core cross-tenant HITL hijack this
        fix closes)."""
        thread_id = "resume-tenant-403"
        _suspend_session(client, thread_id, company_id="ACME-CORP", site_id="SITE-A")
        resp = client.post(
            "/resume",
            json={
                "thread_id": thread_id,
                "feedback": "approve",
                "company_id": "OTHER-CORP",
                "site_id": "SITE-Z",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_resume_unknown_thread_id_is_not_found(self, client):
        resp = client.post(
            "/resume",
            json={
                "thread_id": "no-such-session-ever",
                "feedback": "approve",
                "company_id": "ACME-CORP",
                "site_id": "SITE-A",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 404

    def test_resume_rejects_colon_in_tenant_fields(self, client):
        """Delimiter-collision guard (review, post-F-02 fix):
        PreProcessNode builds tenant_id as an unescaped
        f"{company_id}:{site_id}" string. Without a colon ban, a caller
        could forge company_id="ACME:LINE", site_id="12" to collide with a
        legitimate tenant_id "ACME:LINE:12" actually owned by
        company_id="ACME", site_id="LINE:12" -- and hijack that session.
        Colons must be rejected outright (403), never treated as a
        tenant-matching wildcard."""
        thread_id = "resume-colon-collision"
        _suspend_session(client, thread_id, company_id="ACME", site_id="LINE:12")
        resp = client.post(
            "/resume",
            json={
                "thread_id": thread_id,
                "feedback": "approve",
                "company_id": "ACME:LINE",
                "site_id": "12",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_resume_with_correct_tenant_succeeds(self, client):
        """Regression guard: the legitimate reviewer (same tenant as the
        suspended checkpoint) must still be able to resume normally."""
        thread_id = "resume-tenant-success"
        _suspend_session(client, thread_id, company_id="ACME-CORP", site_id="SITE-A")
        resp = client.post(
            "/resume",
            json={
                "thread_id": thread_id,
                "feedback": "approve",
                "company_id": "ACME-CORP",
                "site_id": "SITE-A",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json().get("status") == "success"
