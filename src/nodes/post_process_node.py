"""AgentCore Platform v1.0 — HCR-C2-062: PostProcessNode

docs/02_design.md S-3 設計 第2層（品質断定ガード）:
  層1（構造）: 影響評価欄に品質判定/出荷可否フィールドを一切定義しない
  （schema テストで assert — この node はそれを保証する側であって、フィールド
  を追加してはならない）。
  層2（lexicon）: quality_verdict_gate.scan_and_sanitize_fields() で自由文
  中の断定表現を検知・文単位で実置換。全文該当なら AgentStatus.ERROR。

  PII 再スキャン（③ S-3 出力再スキャン — docs/02_design.md 従業員 PII
  サニタイズ 4点適用箇所のうちの1つ）。

  degradation_reason の畳み込み（縮退動作 — 先行テンプレートと同じパターン）:
  framework の get_output() はカスタム欄を透過しないため、formatted_output
  へ明示的に畳み込む。
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.pii_sanitizer import sanitize_worker_pii
from src.services.quality_verdict_gate import load_lexicon, scan_and_sanitize_fields

# Fields that never enter the output schema — asserting these here is a
# structural (layer-1) guarantee, not a lexicon check. See
# config/report_templates/deviation_investigation.yaml section 6
# prohibited_fields.
_PROHIBITED_OUTPUT_FIELDS = frozenset({"quality_verdict", "shippability", "release_decision", "pass_fail"})


class PostProcessNode(FunctionNode):
    """Output shaping + S-3 layer-2 quality-verdict gate + PII rescan."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("post_process_passthrough_error", {}, state)
            return {"formatted_output": {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])}}

        gate_kind = state.get("gate_kind")

        if gate_kind in ("ingest", "feedback"):
            payload = state.get("ingest_summary") if gate_kind == "ingest" else state.get("feedback_summary")
            emit_trace_event("post_process_non_draft", {"gate_kind": gate_kind}, state)
            return {
                "formatted_output": {"gate_kind": gate_kind, "summary": payload or {}, "status": AgentStatus.SUCCESS.value},
                "status": AgentStatus.SUCCESS.value,
            }

        result = state.get("result") or {}

        # ── layer 1 structural guard: prohibited fields never leak through ──
        found_prohibited = self._find_prohibited_fields(result)
        if found_prohibited:
            emit_trace_event("post_process_prohibited_field_detected", {"fields": sorted(found_prohibited)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: prohibited quality-verdict field(s) present: {sorted(found_prohibited)}"],
            }

        # ── layer 2: lexicon-based assertive-statement neutralization ──
        lexicon = load_lexicon()
        sanitized_result, all_safe, detected = scan_and_sanitize_fields(result, lexicon)
        if not all_safe:
            emit_trace_event("post_process_quality_verdict_all_flagged", {"categories": detected}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "PostProcessNode: S-3 quality-verdict gate — entire text flagged as assertive, no factual content survives"
                ],
            }
        if detected:
            emit_trace_event("post_process_quality_verdict_neutralized", {"categories": detected}, state)

        # ── S-3 output re-scan: worker-name/employee-number PII (④ …③ point) ──
        sanitized_result, pii_removed = self._rescan_pii(sanitized_result)
        if pii_removed:
            emit_trace_event("post_process_pii_rescan_removed", {"count": pii_removed}, state)

        formatted_output: dict = {
            "gate_kind": "draft",
            "report": sanitized_result,
            "recurrence_flags": state.get("recurrence_flags", []),
            "missing_data_flags": state.get("missing_data_flags", []),
            "requires_human_review": state.get("requires_human_review", False),
            "status": AgentStatus.SUCCESS.value,
        }

        # degradation_reason folding — framework's get_output() does not
        # surface custom fields transparently; fold it into formatted_output
        # explicitly (same pattern as earlier templates).
        degradation_reason = state.get("degradation_reason")
        if degradation_reason:
            formatted_output["degradation_reason"] = degradation_reason

        emit_trace_event("post_process_complete", {"requires_human_review": formatted_output["requires_human_review"]}, state)

        return {"formatted_output": formatted_output, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _find_prohibited_fields(value) -> set[str]:
        found: set[str] = set()

        def _walk(v):
            if isinstance(v, dict):
                for k, vv in v.items():
                    if k in _PROHIBITED_OUTPUT_FIELDS:
                        found.add(k)
                    _walk(vv)
            elif isinstance(v, list):
                for item in v:
                    _walk(item)

        _walk(value)
        return found

    @staticmethod
    def _rescan_pii(value):
        total = 0

        def _walk(v):
            nonlocal total
            if isinstance(v, str):
                sanitized, count, resolvable = sanitize_worker_pii(v)
                total += count
                return sanitized if resolvable else v
            if isinstance(v, dict):
                return {k: _walk(vv) for k, vv in v.items()}
            if isinstance(v, list):
                return [_walk(item) for item in v]
            return v

        return _walk(value), total
