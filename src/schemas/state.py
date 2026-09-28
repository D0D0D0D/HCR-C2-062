"""AgentCore Platform v1.0 — HCR-C2-062 GmpDeviationCapaGraph"""

# docs/02_design.md > State Definition:
#   Outer State: flat TypedDict (AgentState subtype). HITL 7 fields are
#   inherited from AgentState — NOT redeclared here (only hitl_draft is
#   writable by this template; hitl_count/hitl_allowed are framework/
#   Gateway-only, per the framework's HITL state schema).
#
#   Raw deviation-record / report text is NEVER stored here — payload_ref is
#   an opaque reference into a TTL-bounded transient store (SubcontractorQuotationDisputeHistory
#   TransientPayloadStore pattern); only the API adapter / service layer
#   resolves it.
#
#   retrieved_records is the S-3 Provenance gate's single source of truth
#   (structured, sanitized match attributes only — never raw narrative
#   text). See DeviationWorkflowGraphNode._extra_security_gate_output().

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer agent state — GmpDeviationCapaGraph.

    Add only fields specific to this agent below. All shared fields
    (user_input, status, session_id, node_history, error_log, hitl_*, etc.)
    are inherited from AgentState.
    """

    # ── mode / tenant / input plumbing ──────────────────────────────────
    mode: str  # "ingest" / "invoke" / "feedback" — set by PreProcessNode
    tenant_id: str  # "{company_id}:{site_id}" — resolved fail-closed
    validated_input: str  # JSON envelope handed to the inner workflow graph
    payload_ref: str  # opaque reference to raw record body (transient store)

    # ── S-3 provenance gate — path declaration (fail-closed) ────────────
    gate_kind: str  # "ingest" / "feedback" / "draft" — declared by merge_output()

    # ── stable error projection ─────────────────────────────────────────
    error_code: str  # caller-visible stable code, never raw provider text
    error_type: str  # broad class for logs / operation triage

    # ── invoke-mode outputs ──────────────────────────────────────────────
    result: dict  # investigation report + CAPA draft (pre S-3 second layer)
    evidence_map: list  # claim_id -> source_ids / asserted_attrs (S-3 input)
    cited_source_ids: list  # LLM-cited past-deviation / CAPA ids
    retrieved_records: list  # Stage 1/2 real records — S-3 match source of truth
    recurrence_flags: list  # deterministic recurrence-risk flags
    missing_data_flags: list  # fields that could not be extracted/classified
    requires_human_review: bool  # unattributed claim present -> force HITL routing
    degradation_reason: str  # graceful-degradation note (KB empty / LLM absent)

    # ── ingest/feedback-mode outputs (merge_output's non-draft branches) ──
    ingest_summary: dict  # KbUpsertNode result, surfaced via gate_kind="ingest"
    feedback_summary: dict  # KbUpdateEffectivenessNode result, gate_kind="feedback"

    # ── final shaped output ──────────────────────────────────────────────
    formatted_output: dict  # PostProcessNode's final shaped payload


class DeviationWorkflowState(AgentState):
    """Inner workflow-graph state (src/graph/domain_workflow_graph.py).

    docs/02_design.md: route() / get_output() of the inner graph MUST be
    annotated with this schema, NOT the bare framework AgentState — the
    bare schema does not declare these custom fields and annotating with it
    risks the custom fields being treated as type-invisible during
    projection (the Business Travel Hotel & Plan Recommendation Agent class of bug where mode_router routes onto
    the wrong branch because `mode` silently disappears).
    """

    mode: str
    tenant_id: str
    validated_input: str
    payload_ref: str

    # ingest branch
    ingest_summary: dict
    _ingest_record_kind: str  # internal — parse/sanitize/taxonomy_tag/kb_upsert handoff
    _parsed_records: list  # internal — record dicts between parse and kb_upsert

    # invoke branch — Stage 1-4
    structured_incident: list
    similar_deviations: list
    capa_effectiveness: list
    retrieved_records: list
    investigation_draft: dict
    evidence_map: list
    cited_source_ids: list
    capa_draft: dict
    recurrence_flags: list
    missing_data_flags: list
    requires_human_review: bool
    degradation_reason: str
    output: dict
    gate_kind: str
    error_code: str
    error_type: str
    _stage1_record: dict  # internal — stage1 -> stage2 handoff (raw record before structuring)

    # feedback branch
    feedback_summary: dict
    _feedback_payload: dict  # internal — validate_feedback -> kb_update_effectiveness handoff
