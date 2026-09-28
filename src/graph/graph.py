"""AgentCore Platform v1.0 — HCR-C2-062: GmpDeviationCapaGraph

docs/02_design.md Position in AgentCore Architecture:
  Agent Class: GmpDeviationCapaGraph (this file)
  L1 Base: AgentBaseGraph (L1-direct inheritance; AutonomousBaseGraph unused
  — fixed pipeline, no autonomous think/act loop)

Outer backbone (fixed — add_edges() NOT overridden):
    START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                               v (RETRY, max 3)
                                            pre_process

Domain complexity (3-mode dispatch + invoke 4-stage pipeline + HITL review)
is hidden inside the `main` slot's GraphNode (DeviationWorkflowGraphNode)
wrapping the inner DeviationWorkflowGraph (src/graph/domain_workflow_graph.py).

LLM injection seam (docs/02_design.md "LLM 注入シーム"):
  This file pulls `config.get("llm")` and passes it down by constructor
  injection — no model ID / concrete client implementation is ever written
  in src/. LLM absence triggers deterministic fallback inside
  src/services/investigation_pipeline.py, not here.
"""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import DeviationWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class GmpDeviationCapaGraph(AgentBaseGraph):
    """Outer graph — 5-node fixed backbone; domain complexity is hidden in
    the `main` slot's GraphNode (DeviationWorkflowGraphNode) wrapping the
    inner DeviationWorkflowGraph.

    Constructor accepts optional injection seams via `config` so server.py
    and tests can wire fakes without reaching into node internals:

        config = {
            "llm": <BaseLLM instance>,
            "kb_service": <KBService instance>,
            "payload_store": <TransientPayloadStore instance>,
            "top_k_similar_deviations": 8,
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 12},
        }

    AgentRegistry entry point — must stay in sync with config/agent.yaml
    (`module: src.graph` / `class: GmpDeviationCapaGraph`).
    """

    @property
    def name(self) -> str:
        return "GMP Manufacturing Deviation & CAPA Report Drafting Agent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode (required first)

        llm = self.config.get("llm")
        kb_service = self.config.get("kb_service")
        payload_store = self.config.get("payload_store")
        top_k = self.config.get("top_k_similar_deviations", 8)
        ingest_cfg = self.config.get("ingest", {})
        max_file_bytes = ingest_cfg.get("max_file_bytes", 10_485_760)
        max_free_text_chars = ingest_cfg.get("max_free_text_chars", 20_000)

        self._nodes["pre_process"] = PreProcessNode(
            payload_store=payload_store,
            max_file_bytes=max_file_bytes,
            max_free_text_chars=max_free_text_chars,
        )
        # NOTE: DeviationWorkflowGraphNode intentionally does NOT accept the
        # outer graph's checkpointer — it always builds its own dedicated
        # InMemorySaver for the inner subgraph (see src/nodes/main_node.py
        # module docstring "CRITICAL" note on distinct checkpointer instances).
        self._nodes["main"] = DeviationWorkflowGraphNode(
            llm=llm,
            kb_service=kb_service,
            payload_store=payload_store,
            top_k_similar_deviations=top_k,
        )
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def get_output(self, state: State) -> dict[str, Any]:
        """Caller-facing projection.

        AgentBaseGraph.get_output() intentionally exposes only the standard
        envelope. This template also needs stable error fields to survive the
        inner workflow GraphNode boundary; raw error text stays in
        state["error_log"] and is de-duplicated before projection.
        """
        output = {
            "output": state.get("formatted_output") or state.get("result"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
        if state.get("error_code"):
            output["error_code"] = state.get("error_code")
        if state.get("error_type"):
            output["error_type"] = state.get("error_type")
        error_log = state.get("error_log", [])
        if error_log:
            output["error_log"] = list(dict.fromkeys(str(item) for item in error_log))
        return output
