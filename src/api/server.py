"""AgentCore Platform v1.0 — HCR-C2-062: standalone HTTP entry point

docs/02_design.md 3 つの動作モード:
  POST /ingest   — mode="ingest"   (VERIFIED_EXTERNAL, DB write)
  POST /invoke   — mode="invoke"   (VERIFIED_EXTERNAL)
  POST /feedback — mode="feedback" (VERIFIED_EXTERNAL, DB write)
  POST /resume   — HITL resume (entry-point auth + tenant-ownership check,
                   then agent.resume() delegation — F-02 fix)

mode の単一決定点: mode の決定権は API アダプタのみ（input_context["mode"]
に固定値をセット）。PreProcessNode は enum 検証 + テナント解決に徹する。

payload_ref の受け渡し: AgentBaseGraph.invoke()'s initial state only ever
seeds `user_input`/`input_context` (+ framework bookkeeping) — there is no
top-level `payload_ref` kwarg. This adapter therefore deposits raw file
bytes into TransientPayloadStore BEFORE invocation and passes the resulting
ref through `input_context["payload_ref"]`; PreProcessNode reads it from
there on the first pass (see src/nodes/pre_process_node.py S-2 gate note).

server 起動時に wire_services()（LLM/KB クライアントの実配線）を明示実行
— 未配線だと KBService は db=None のまま渡り、Stage2/ingest/feedback は
「KB 空」として振る舞う（設計上正当な graceful degradation。本番で DSN
secret が用意されているのに未配線なのは misconfiguration）。

Entry points are adapters only — no business logic here.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import GmpDeviationCapaGraph
from src.services.kb_service import KBService, TransientPayloadStore

app = FastAPI(title="HCR-C2-062 GmpDeviationCapaGraph")

# Checkpointer for the OUTER graph only. STG is InMemorySaver (per
# docs/02_design.md HITL 設計); the inner DeviationWorkflowGraphNode always
# builds its own dedicated checkpointer instance — see src/nodes/main_node.py
# "CRITICAL" note on why sharing one checkpointer object between outer and
# inner graphs under the same thread_id silently corrupts both checkpoints.
_checkpointer = InMemorySaver()

# Shared transient payload store — same instance for the outer PreProcessNode
# S-2 gate and the inner graph's parse/stage1 nodes (both must resolve the
# SAME payload_ref).
_payload_store = TransientPayloadStore()


def _load_runtime_config() -> dict[str, Any]:
    try:
        with open("config/config.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


_runtime_cfg = _load_runtime_config()


def wire_services() -> KBService:
    """Wire real service clients at server startup.

    docs/02_design.md S-3 Secret Isolation: any DB DSN must be read via
    ctx.secrets.require() at the point of use (per-request, inside an
    InvocationContext), never os.environ directly — so it cannot be
    resolved here at module-import time regardless.

    v1 has no declared `requires.secrets` DB DSN in config/agent.yaml, so
    this always wires KBService with db=None — a SUPPORTED configuration
    (graceful degradation / KB cold-start, not a crash): Stage 2 search
    returns empty results, and ingest/feedback DB writes raise explicitly
    if attempted without a real DB (a genuine misconfiguration signal,
    distinct from "KB has no matching rows yet"). A customer deployment
    that needs real KB persistence adds a DSN secret to `requires.secrets`
    and extends this function to construct a `PostgresClient`/`MySQLClient`
    from it inside a per-request `bound_secrets()` context.
    """
    return KBService(db=None)


_kb_service = wire_services()

agent = GmpDeviationCapaGraph(
    config={
        "kb_service": _kb_service,
        "payload_store": _payload_store,
        "top_k_similar_deviations": _runtime_cfg.get("retrieval", {}).get("top_k_similar_deviations", 8),
        "ingest": _runtime_cfg.get("ingest", {}),
        "memory_enabled": _runtime_cfg.get("memory_enabled", True),
        "hitl": _runtime_cfg.get("hitl", {"enabled": True, "max_hitl": 12}),
    }
)
agent.compile(checkpointer=_checkpointer)
agent.provision_secrets(secrets_factory(namespace="hcr-c2-062", agent_name="GmpDeviationCapaGraph"))


class InvokeRequest(BaseModel):
    """POST /invoke — mode="invoke" (single-entry design; mode is fixed by
    this route, never inferred from `input`).

    NOTE (review finding F-01, High/merge-blocker): this model
    intentionally does NOT expose a `hitl_allowed` field. An authenticated
    caller MUST NOT be able to disable the mandatory Stage 3->4 QA human
    review by setting a body flag -- pydantic silently drops unknown input
    fields by default, so a legacy caller that still POSTs
    `"hitl_allowed": false` has that value ignored, not honored. Whether
    HITL is skipped is decided exclusively by `_stg_smoke_mode()` below, a
    deployment-level flag the caller cannot influence.
    """

    input: str
    session_id: str = ""
    thread_id: str = ""
    company_id: str
    site_id: str
    file_bytes_b64: str | None = None


class IngestRequest(BaseModel):
    """POST /ingest — mode="ingest". file_bytes_b64 carries the raw QMS
    export (CSV/xlsx/docx) — deposited into TransientPayloadStore BEFORE
    graph invocation so the raw bytes never enter State (docs/02_design.md
    payload_ref pattern)."""

    input: str = "ingest"
    session_id: str = ""
    thread_id: str = ""
    company_id: str
    site_id: str
    record_kind: str = "deviation"  # "deviation" / "capa" / "effectiveness"
    file_bytes_b64: str | None = None


class FeedbackRequest(BaseModel):
    """POST /feedback — mode="feedback"."""

    input: str = ""
    session_id: str = ""
    thread_id: str = ""
    company_id: str
    site_id: str
    capa_id: str
    checked_on: str
    effective: bool | None = None
    recurrence_observed: bool | None = None
    evidence_note: str | None = None


class ResumeRequest(BaseModel):
    """POST /resume.

    NOTE (review finding F-02, High/merge-blocker): `company_id` /
    `site_id` are REQUIRED, not optional convenience fields -- they are the
    tenant-ownership proof checked against the suspended checkpoint's
    `tenant_id` (see `_authorize_resume_tenant()`). Without them, any caller
    who merely learns a `thread_id` (e.g. from a log line, a shared Slack
    thread) could inject an approve/reject/correction decision into someone
    else's QA review.
    """

    thread_id: str
    feedback: str | dict
    company_id: str
    site_id: str


def _stg_smoke_mode() -> bool:
    """Deployment-level flag (NOT caller-controlled).

    Set only by the `deploy-stg` CI job (CI `deploy-stg:
    variables: STG_MOCK_MODE: "true"`, inherited by the job shell that then
    launches `uvicorn` in the background) or `deploy/local-stg.yml`
    rehearsal compose, for the smoke-test run only -- where no human
    reviewer exists to answer `interrupt()` and the evidence script
    (`scripts/stg_invoke_evidence.py`) requires a terminal `status:
    "success"` on the very first `/invoke`.

    This is a process-environment flag read once per request, exactly like
    `INVOKE_AUTH_TOKEN` (the entry-point exception to the secrets rule)
    -- it authorizes a deployment posture, not a per-request caller
    credential, and it is never accepted from the request body
    (see `InvokeRequest` docstring, F-01).
    """
    return os.environ.get("STG_MOCK_MODE", "").lower() == "true"


def _resolve_trust(request: Request) -> TrustLevel:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone/staging caller auth:
    # deployment-level caller credential read from process env by design —
    # this authenticates the caller BEFORE any InvocationContext exists, so
    # ctx.secrets cannot apply (the entry-point exception to the secrets rule).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    return trust


def _build_ctx(request: Request, session_id: str, thread_id: str, hitl_allowed: bool = True) -> InvocationContext:
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        thread_id=thread_id or session_id or str(uuid4()),
        caller_trust_level=_resolve_trust(request),
        caller_id=getattr(request.state, "caller_id", ""),
        hitl_allowed=hitl_allowed,
    )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    with bound_secrets(agent._secrets_provider):
        # hitl_allowed is decided SOLELY by the deployment-level STG_MOCK_MODE
        # flag -- never by caller input (F-01; see InvokeRequest / _stg_smoke_mode
        # docstrings). A normal authenticated caller always gets hitl_allowed=True.
        ctx = _build_ctx(request, req.session_id, req.thread_id, hitl_allowed=not _stg_smoke_mode())
        input_context: dict[str, Any] = {
            "mode": "invoke",
            "company_id": req.company_id,
            "site_id": req.site_id,
            "free_text": req.input,
        }
        if req.file_bytes_b64:
            input_context["payload_ref"] = _payload_store.put_bytes(base64.b64decode(req.file_bytes_b64))
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    with bound_secrets(agent._secrets_provider):
        ctx = _build_ctx(request, req.session_id, req.thread_id)
        input_context: dict[str, Any] = {
            "mode": "ingest",
            "company_id": req.company_id,
            "site_id": req.site_id,
            "_ingest_record_kind": req.record_kind,
        }
        if req.file_bytes_b64:
            input_context["payload_ref"] = _payload_store.put_bytes(base64.b64decode(req.file_bytes_b64))
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    with bound_secrets(agent._secrets_provider):
        ctx = _build_ctx(request, req.session_id, req.thread_id)
        feedback_payload = {
            "capa_id": req.capa_id,
            "checked_on": req.checked_on,
            "effective": req.effective,
            "recurrence_observed": req.recurrence_observed,
            "evidence_note": req.evidence_note,
        }
        return agent.invoke(
            json.dumps(feedback_payload, default=str),
            ctx=ctx,
            input_context={
                "mode": "feedback",
                "company_id": req.company_id,
                "site_id": req.site_id,
                "free_text": json.dumps(feedback_payload, default=str),
            },
        )


def _authorize_resume_tenant(thread_id: str, company_id: str, site_id: str) -> None:
    """Verify the resuming caller owns the suspended checkpoint (F-02).

    Reads the checkpointed state via the same LangGraph `get_state()` API the
    framework itself uses in `AgentBaseGraph.abort()` (verified against the
    installed `agenticstar-agentcore==1.0.0` wheel) -- there is no
    higher-level accessor for "peek at a suspended session's state" on
    `BaseGraph`, so this reaches through `agent._compiled` deliberately,
    mirroring the framework's own internal pattern rather than inventing one.

    Raises HTTPException(404) if no checkpoint exists for thread_id, or
    HTTPException(403) if the resuming caller's tenant does not match the
    checkpoint's `tenant_id` (`"{company_id}:{site_id}"`, set by
    PreProcessNode — src/nodes/pre_process_node.py).

    Delimiter-collision guard (review, post-F-02): PreProcessNode
    builds tenant_id as an unescaped f"{company_id}:{site_id}" string, so
    without this check a caller could forge company_id="ACME:LINE",
    site_id="12" to produce the same tenant_id string as the legitimate
    owner company_id="ACME", site_id="LINE:12" and hijack the resume. Reject
    any company_id/site_id containing ":" outright -- PreProcessNode never
    accepts colons in practice (tenant identifiers are alphanumeric site/
    company codes), so this is not a functional restriction, only a closure
    of the ambiguous-concatenation exploit path.
    """
    if ":" in company_id or ":" in site_id:
        raise HTTPException(status_code=403, detail="company_id/site_id must not contain ':'.")
    agent._ensure_compiled()
    config = agent._thread_config(thread_id)
    snapshot = agent._compiled.get_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No HITL checkpoint found for thread_id={thread_id!r}")
    checkpoint_tenant_id = snapshot.values.get("tenant_id", "")
    requested_tenant_id = f"{company_id}:{site_id}"
    if not checkpoint_tenant_id or checkpoint_tenant_id != requested_tenant_id:
        raise HTTPException(status_code=403, detail="Caller tenant does not own this HITL session.")


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request):
    """POST /resume — F-02 fix (review finding, High/merge-blocker).

    Before this fix, /resume took no Request and never ran entry-point auth
    or tenant ownership checks: any caller who merely knew a thread_id could
    inject a QA approve/reject/correction decision into someone else's
    suspended review. Now: (1) the same entry-point auth as /invoke
    (`_resolve_trust()` -- 401 on missing/invalid INVOKE_AUTH_TOKEN when one
    is configured), then (2) tenant-ownership verification against the
    checkpoint's tenant_id (403 on mismatch, 404 if thread_id is unknown).
    """
    with bound_secrets(agent._secrets_provider):
        _resolve_trust(request)  # 401 raised internally on invalid/missing token
        _authorize_resume_tenant(req.thread_id, req.company_id, req.site_id)
        try:
            return agent.resume(thread_id=req.thread_id, feedback=req.feedback)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="HITL checkpoint not found.") from exc


@app.get("/health")
def health():
    return {"status": "ok", "agent": "HCR-C2-062"}
