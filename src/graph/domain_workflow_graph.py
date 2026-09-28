"""AgentCore Platform v1.0 — HCR-C2-062: DeviationWorkflowGraph (inner graph)

docs/02_design.md Data Flow — 内側 workflow graph:

                      ┌── mode == "ingest"   → parse → sanitize → taxonomy_tag → kb_upsert ─────┐
    START → mode_router ──┼── mode == "invoke"   → stage1 → stage2 → stage3 → [hitl_review] → stage4 ┼→ END
                      └── mode == "feedback" → validate → kb_update_effectiveness ──────────────┘
                                                     ↑
                                        status == error → END（fail fast）

Inherits BaseGraph directly (docs/02_design.md Composition Pattern —
GraphNode wraps this inner graph in the outer `main` slot). route() /
get_output() are annotated with the inner DeviationWorkflowState schema,
NOT AgentState (Business Travel Hotel & Plan Recommendation Agent class of bug — bare AgentState annotation causes
LangGraph's projection to drop custom fields like `mode`, silently
collapsing every branch onto the default route).
"""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.feedback_node import FeedbackValidateNode, KbUpdateEffectivenessNode
from src.nodes.hitl_review_node import HitlReviewNode
from src.nodes.ingest_nodes import IngestParseNode, IngestSanitizeNode, KbUpsertNode, TaxonomyTagNode
from src.nodes.invoke_stage_nodes import Stage1StructureNode, Stage2SearchNode, Stage3DraftNode, Stage4CapaNode
from src.nodes.mode_router_node import ModeRouterNode
from src.schemas.state import DeviationWorkflowState


class DeviationWorkflowGraph(BaseGraph):
    """Inner domain workflow — 3-way mode dispatch + invoke 4-stage
    pipeline + HITL review loop between Stage 3 and Stage 4."""

    @property
    def name(self) -> str:
        return "deviation_workflow"

    @property
    def state_schema(self) -> type:
        return DeviationWorkflowState

    def _validate_config(self) -> None:
        # llm/service injection is done via constructor args on individual
        # nodes, wired in register_nodes() from self.config (forwarded by
        # DeviationWorkflowGraphNode._parent_config()).
        pass

    def register_nodes(self) -> None:
        # No super() call — BaseGraph.register_nodes() is abstract.
        llm = self.config.get("llm")
        kb_service = self.config.get("kb_service")
        payload_store = self.config.get("payload_store")
        top_k = self.config.get("top_k_similar_deviations", 8)

        self._nodes["mode_router"] = ModeRouterNode()

        # ingest branch
        self._nodes["parse"] = IngestParseNode(payload_store=payload_store)
        self._nodes["sanitize"] = IngestSanitizeNode()
        self._nodes["taxonomy_tag"] = TaxonomyTagNode()
        self._nodes["kb_upsert"] = KbUpsertNode(kb_service=kb_service)

        # invoke branch
        self._nodes["stage1"] = Stage1StructureNode(payload_store=payload_store)
        self._nodes["stage2"] = Stage2SearchNode(kb_service=kb_service, top_k=top_k)
        self._nodes["stage3"] = Stage3DraftNode(llm=llm)
        self._nodes["hitl_review"] = HitlReviewNode()
        self._nodes["stage4"] = Stage4CapaNode(llm=llm)

        # feedback branch
        self._nodes["validate_feedback"] = FeedbackValidateNode()
        self._nodes["kb_update_effectiveness"] = KbUpdateEffectivenessNode(kb_service=kb_service)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "mode_router")
        self._sg.add_conditional_edges(
            "mode_router",
            self.route_mode,
            {
                "ingest": "parse",
                "invoke": "stage1",
                "feedback": "validate_feedback",
                "error": END,
            },
        )

        # ingest branch — linear, fail-fast on error (each node passes
        # through if status is already error; final edge always reaches END)
        self._sg.add_edge("parse", "sanitize")
        self._sg.add_edge("sanitize", "taxonomy_tag")
        self._sg.add_edge("taxonomy_tag", "kb_upsert")
        self._sg.add_edge("kb_upsert", END)

        # invoke branch — 4 stages with an HITL review gate between 3 and 4
        self._sg.add_edge("stage1", "stage2")
        self._sg.add_edge("stage2", "stage3")
        self._sg.add_edge("stage3", "hitl_review")
        self._sg.add_conditional_edges(
            "hitl_review",
            self.route_after_review,
            {
                "proceed": "stage4",
                "stop": END,
            },
        )
        self._sg.add_edge("stage4", END)

        # feedback branch
        self._sg.add_edge("validate_feedback", "kb_update_effectiveness")
        self._sg.add_edge("kb_update_effectiveness", END)

    def route(self, state: DeviationWorkflowState) -> str:
        """Required by BaseGraph ABC. Delegates to route_mode — this graph
        has two distinct conditional-edge sites (route_mode, route_after_review),
        each registered directly via add_conditional_edges()."""
        return self.route_mode(state)

    # ------------------------------------------------------------------
    # Conditional routes — both annotated with DeviationWorkflowState
    # (NOT AgentState — see module docstring).
    # ------------------------------------------------------------------
    def route_mode(self, state: DeviationWorkflowState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "error"
        mode = state.get("mode", "invoke")
        if mode in ("ingest", "invoke", "feedback"):
            return mode
        return "error"

    def route_after_review(self, state: DeviationWorkflowState) -> str:
        """Reads state["status"] exclusively (set by HitlReviewNode):
        CANCELLED (REJECTED feedback) or ERROR -> stop (skip Stage 4).
        Otherwise (SUCCESS — APPROVED/CORRECTED, or hitl_allowed=False
        skip-through) -> proceed to Stage 4."""
        status = state.get("status")
        if status in (AgentStatus.CANCELLED.value, AgentStatus.ERROR.value):
            return "stop"
        return "proceed"

    def get_output(self, state: DeviationWorkflowState) -> dict:
        """Shape sub_result for DeviationWorkflowGraphNode.merge_output()."""
        return {
            "status": state.get("status"),
            "mode": state.get("mode"),
            "gate_kind": state.get("gate_kind"),
            "output": state.get("output"),
            "evidence_map": state.get("evidence_map"),
            "cited_source_ids": state.get("cited_source_ids"),
            # retrieved_records already carries the full union (Stage 1
            # incident elements + Stage 2 similar_deviations + Stage 2
            # capa_effectiveness) — built once in Stage2SearchNode/Stage4CapaNode.
            "retrieved_records": state.get("retrieved_records", []),
            "recurrence_flags": state.get("recurrence_flags"),
            "missing_data_flags": state.get("missing_data_flags"),
            "requires_human_review": state.get("requires_human_review"),
            "degradation_reason": state.get("degradation_reason"),
            "ingest_summary": state.get("ingest_summary"),
            "feedback_summary": state.get("feedback_summary"),
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "hitl_status": state.get("hitl_status"),
            "error_code": state.get("error_code"),
            "error_type": state.get("error_type"),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
