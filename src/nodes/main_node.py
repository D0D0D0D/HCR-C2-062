"""AgentCore Platform v1.0 — HCR-C2-062: DeviationWorkflowGraphNode (main slot, GraphNode)

docs/02_design.md Composition Pattern:
  Pattern: GraphNode (subgraph). Composition target: inner
  DeviationWorkflowGraph (src/graph/domain_workflow_graph.py).
  error_strategy = "handle" — inner workflow errors are projected to stable
  error_code values without leaking raw inner error_log text. propagate_hitl
  = True — HITL は内側 workflow graph の draft レビューで発火するため、
  外側の呼び出し元（QA担当）まで伝播させる必要がある。

docs/02_design.md S-3 設計 第1層 — Provenance ID 機械検証:
  実装位置は subgraph 境界（このノード）。merge_output() が gate_kind を
  明示宣言し、_extra_security_gate_output() がそれを唯一の正本として
  照合する。欠落を「検査不要」と解釈せず fail closed で raise する。

HITL checkpointer (docs/02_design.md HITL 設計):
  🔴 inner / outer の checkpointer は別インスタンス（BusinessDiningAndEntertainmentPlannerAgent 実測 —
  同一 saver 共有は checkpoint 相互汚染を起こし resume が収束しない）。
  この GraphNode の __init__ が専用 InMemorySaver を新規生成し、明示的に
  compile(checkpointer=...) を呼ぶ。

  get_subgraph() は __init__ でキャッシュした内側 graph を返す — 毎
  execute() で新規生成すると HITL resume が無限ループする
  （BusinessDiningAndEntertainmentPlannerAgent 実測）。
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver
from shared.utils.audit_logger import emit_trace_event

_ERROR_STRATEGY = "handle"
_GENERIC_SUBGRAPH_ERROR_CODE = "subgraph_execution_failed"
_SUBGRAPH_ERROR_TYPE = "inner_workflow_error"
_KNOWN_FAILURE_CLASSIFIERS: tuple[tuple[str, str], ...] = (
    ("no sanitized payload found", "payload_ref_unresolved"),
    ("no records could be parsed", "ingest_payload_parse_failed"),
    ("unresolvable PII", "regulated_pii_unresolvable"),
    ("tenant_id missing", "tenant_scope_missing"),
    ("KBService not wired", "kb_service_not_configured"),
    ("capa_id and checked_on are required", "feedback_payload_invalid"),
    ("evidence_note PII unresolvable", "regulated_pii_unresolvable"),
    ("no effectiveness_checks row", "feedback_target_not_found"),
)
_KNOWN_FAILURE_CODES = frozenset(code for _needle, code in _KNOWN_FAILURE_CLASSIFIERS) | {_GENERIC_SUBGRAPH_ERROR_CODE}


class DeviationWorkflowGraphNode(GraphNode):
    error_strategy: ClassVar[str] = _ERROR_STRATEGY
    known_failure_codes: ClassVar[frozenset[str]] = _KNOWN_FAILURE_CODES
    propagate_hitl: ClassVar[bool] = True
    # docs/02_design.md S-1: GraphNode defaults to ANONYMOUS — must declare
    # explicitly (SubcontractorQuotationDisputeHistory real-bug precedent).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        llm: Any | None = None,
        kb_service: Any | None = None,
        payload_store: Any | None = None,
        top_k_similar_deviations: int = 8,
    ) -> None:
        super().__init__()
        self._llm = llm
        self._kb_service = kb_service
        self._payload_store = payload_store
        self._top_k = top_k_similar_deviations

        # Mandatory subgraph cache — built once, NEVER rebuilt per execute().
        # Always its own dedicated checkpointer instance (never the outer
        # graph's checkpointer — see module docstring "CRITICAL").
        from src.graph.domain_workflow_graph import DeviationWorkflowGraph

        self._checkpointer = InMemorySaver()
        self._subgraph = DeviationWorkflowGraph(config=self._parent_config())
        self._subgraph.compile(checkpointer=self._checkpointer)

    def get_subgraph(self) -> Any:
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        # validated_input already IS the JSON envelope built by the outer
        # PreProcessNode ({"mode","tenant_id","payload_ref","free_text"});
        # pass it straight through as the subgraph's user_input string.
        value = state.get("validated_input", "")
        return value if isinstance(value, str) else ""

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Declares gate_kind explicitly per mode — the S-3 gate's routing
        key. NEVER infer the path from key presence (docs/02_design.md 🚫
        NG example) — sub_result's own gate_kind (set by the inner graph's
        terminal node for that branch) is trusted here since it is
        confirmed to travel through get_output() unmodified.
        """
        mode = state.get("mode")
        status = sub_result.get("status")

        if mode == "ingest":
            ingest_delta: dict[str, Any] = {
                "gate_kind": "ingest",
                "ingest_summary": sub_result.get("ingest_summary", {}),
                "status": status,
                "error_log": sub_result.get("error_log", []) if sub_result.get("error_log") else [],
            }
            self._copy_error_fields(ingest_delta, sub_result)
            return ingest_delta
        if mode == "feedback":
            feedback_delta: dict[str, Any] = {
                "gate_kind": "feedback",
                "feedback_summary": sub_result.get("feedback_summary", {}),
                "status": status,
                "error_log": sub_result.get("error_log", []) if sub_result.get("error_log") else [],
            }
            self._copy_error_fields(feedback_delta, sub_result)
            return feedback_delta

        # invoke = draft-generation path
        delta: dict[str, Any] = {
            "gate_kind": "draft",
            "result": sub_result.get("output"),
            "status": status,
            "evidence_map": sub_result.get("evidence_map"),
            "cited_source_ids": sub_result.get("cited_source_ids"),
            # 🔴 promote the record ENTITIES themselves (not just ids) — the
            # S-3 gate's sole source of truth (docs/02_design.md).
            "retrieved_records": sub_result.get("retrieved_records"),
            "recurrence_flags": sub_result.get("recurrence_flags"),
            "missing_data_flags": sub_result.get("missing_data_flags"),
            "requires_human_review": sub_result.get("requires_human_review"),
        }
        if sub_result.get("degradation_reason"):
            delta["degradation_reason"] = sub_result.get("degradation_reason")
        if sub_result.get("error_log"):
            delta["error_log"] = sub_result.get("error_log")
        self._copy_error_fields(delta, sub_result)
        # HITL fields — only include when present (None-fill prohibited).
        if sub_result.get("hitl_draft") is not None:
            delta["hitl_draft"] = sub_result.get("hitl_draft")
        if sub_result.get("hitl_feedback") is not None:
            delta["hitl_feedback"] = sub_result.get("hitl_feedback")
        if sub_result.get("hitl_status") is not None:
            delta["hitl_status"] = sub_result.get("hitl_status")
        return delta

    # ------------------------------------------------------------------
    # S-3 — Provenance ID machine verification (critical, subgraph boundary)
    # ------------------------------------------------------------------
    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        # Upstream error passthrough — error carries no business result.
        if result.get("status") == AgentStatus.ERROR.value:
            return result

        kind = result.get("gate_kind")
        if kind in ("ingest", "feedback"):
            return result  # no LLM output, no citations — not applicable

        if kind != "draft":
            emit_trace_event("provenance_gate_error", {"reason": "missing gate_kind"}, result)
            raise SecurityViolationError("S-3 gate: gate_kind is not declared")

        cited = result.get("cited_source_ids")
        records = result.get("retrieved_records")
        evidence = result.get("evidence_map")
        if cited is None or records is None or evidence is None:
            emit_trace_event(
                "provenance_gate_error",
                {"reason": "citation fields missing on draft path"},
                result,
            )
            raise SecurityViolationError(
                "S-3 gate: draft output must carry evidence_map, cited_source_ids, retrieved_records"
            )

        if not isinstance(cited, list) or not all(isinstance(sid, str) and sid for sid in cited):
            emit_trace_event("provenance_gate_error", {"reason": "cited_source_ids type"}, result)
            raise SecurityViolationError("S-3 gate: cited_source_ids must be a list of non-empty strings")
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            emit_trace_event("provenance_gate_error", {"reason": "retrieved_records type"}, result)
            raise SecurityViolationError("S-3 gate: retrieved_records must be a list of dicts")
        if not isinstance(evidence, list) or not all(isinstance(claim, dict) for claim in evidence):
            emit_trace_event("provenance_gate_error", {"reason": "evidence_map type"}, result)
            raise SecurityViolationError("S-3 gate: evidence_map must be a list of dicts")

        by_id = {r["source_id"]: r for r in records if isinstance(r.get("source_id"), str) and r.get("source_id")}

        # (1) fabricated-id rejection — cited ids must exist in Stage 1/2 output
        unknown = set(cited) - set(by_id)
        if unknown:
            emit_trace_event("provenance_violation", {"unknown_id_count": len(unknown)}, result)
            raise SecurityViolationError("LLM cited non-existent ids")

        # (2) "real-but-unrelated source_id" rejection — asserted attrs must
        # match the actual retrieved record's attrs.
        for claim in evidence:
            asserted_attrs = claim.get("asserted_attrs", {})
            if not isinstance(asserted_attrs, dict):
                emit_trace_event("provenance_gate_error", {"reason": "asserted_attrs type"}, result)
                raise SecurityViolationError("S-3 gate: asserted_attrs must be a dict")
            for sid, asserted in asserted_attrs.items():
                if not isinstance(sid, str) or not isinstance(asserted, dict):
                    emit_trace_event("provenance_gate_error", {"reason": "asserted_attrs entry type"}, result)
                    raise SecurityViolationError("S-3 gate: asserted_attrs entries must map string ids to dicts")
                actual = by_id.get(sid)
                if actual is None:
                    emit_trace_event("provenance_violation", {"unknown_id_count": 1}, result)
                    raise SecurityViolationError("evidence_map cites non-existent id")
                mismatch = {k: (v, actual.get(k)) for k, v in asserted.items() if actual.get(k) != v}
                if mismatch:
                    emit_trace_event(
                        "provenance_attr_mismatch",
                        {"source_id": sid, "mismatch_keys": sorted(str(k) for k in mismatch)},
                        result,
                    )
                    raise SecurityViolationError("claim misattributes cited source")
        return result

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict[str, Any]:
        error_code = self._classify_subgraph_error(error)
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": error_code,
            "error_type": _SUBGRAPH_ERROR_TYPE,
            "error_log": [f"DeviationWorkflowGraphNode: inner workflow failed ({error_code})"],
        }

    @staticmethod
    def _copy_error_fields(delta: dict[str, Any], sub_result: dict[str, Any]) -> None:
        for key in ("error_code", "error_type"):
            value = sub_result.get(key)
            if value:
                delta[key] = value

    @staticmethod
    def _classify_subgraph_error(error: Exception) -> str:
        raw_logs = getattr(error, "error_log", None)
        messages = raw_logs if isinstance(raw_logs, list) else []
        if not messages:
            messages = [type(error).__name__]
        joined = "\n".join(str(item) for item in messages)
        for needle, code in _KNOWN_FAILURE_CLASSIFIERS:
            if needle in joined:
                return code
        return _GENERIC_SUBGRAPH_ERROR_CODE

    def _parent_config(self) -> dict[str, Any]:
        """Forward llm/service injection seams to the inner graph so tests
        can substitute fakes without touching node code. memory_enabled/hitl
        MUST be forwarded — BaseGraph.invoke()/resume() only attach a
        checkpointer-aware RunnableConfig when self.config declares
        memory_enabled or hitl.enabled."""
        return {
            "llm": self._llm,
            "kb_service": self._kb_service,
            "payload_store": self._payload_store,
            "top_k_similar_deviations": self._top_k,
            "memory_enabled": True,
            "hitl": {"enabled": True},
        }
