"""AgentCore Platform v1.0 — HCR-C2-062: HitlReviewNode (inner, invoke Stage 3→4 boundary)

docs/02_design.md HITL 設計:
  発火点: 内側 workflow graph の hitl_review ノード（Stage 3 の後）。報告書
  セクション単位のドラフトを QA 担当に提示し、修正指示を受けて refine するループ。

  🔴 interrupt() は必ず state.get("hitl_allowed", True) でガードする。
  hitl_allowed=False（親 GraphNode からの自動実行）では
  人間レビューを飛ばして先へ進む。

  HitlStatus の 3 分岐をすべて処理する:
    APPROVED  -> ドラフト確定 -> Stage 4 へ進む（そのまま）
    CORRECTED -> 人間の修正を反映して refine（無視すると修正が黙って捨てられる）
    REJECTED  -> Stage 4 をスキップしドラフト破棄を明示して終了

  resume 応答は無加工で state に入れない — PII サニタイザを先に通す
  （scrub-first。先行テンプレートの実測パターンを踏襲）。

  hitl_draft に生成済みドラフトを保存し、resume 時の LLM 再実行を防ぐ
  （冪等性ガード）。
"""

from __future__ import annotations

from typing import Any, ClassVar

from langgraph.types import interrupt

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.pii_sanitizer import sanitize_worker_pii


class HitlReviewNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        draft = state.get("investigation_draft", {})

        # 🔴 mandatory guard — hitl_allowed=False (automated GraphNode-driven
        # invocation) skips human review and proceeds straight through.
        if not state.get("hitl_allowed", True):
            emit_trace_event("hitl_review_skipped", {"reason": "hitl_allowed=False"}, state)
            return {"status": AgentStatus.SUCCESS.value}

        # Idempotency guard: if hitl_draft already holds this exact draft, we
        # are on a resume pass and must not re-trigger a fresh interrupt cycle
        # without state — but LangGraph replays the node from the top on
        # resume, so interrupt() itself is what returns the human's answer
        # (it does not raise a second time on resume).
        raw_feedback = interrupt(
            {
                "prompt": "逸脱調査報告書ドラフトをご確認ください（承認 / 修正 / 却下）。",
                "draft": draft,
                "requires_human_review": state.get("requires_human_review", False),
            }
        )

        # ── everything below runs only on resume ────────────────────────
        sanitized_feedback, pii_count = self._sanitize_feedback(raw_feedback)
        if pii_count:
            emit_trace_event("hitl_response_sanitized", {"count": pii_count}, state)

        hitl_status = HitlStatus.from_feedback(sanitized_feedback)

        emit_trace_event("hitl_review_round", {"hitl_status": str(hitl_status)}, state)

        result: dict[str, Any] = {
            "hitl_draft": draft,
            "hitl_feedback": sanitized_feedback,
            "hitl_status": hitl_status,
            "status": AgentStatus.SUCCESS.value,
        }

        if hitl_status == HitlStatus.REJECTED:
            result["status"] = AgentStatus.CANCELLED.value
            result["error_log"] = ["QA reviewer rejected the investigation draft — CAPA drafting skipped"]
            return result

        if hitl_status == HitlStatus.CORRECTED:
            correction = None
            if isinstance(sanitized_feedback, dict):
                correction = sanitized_feedback.get("correction") or sanitized_feedback.get("corrected_draft")
            if correction:
                merged_draft = dict(draft)
                if isinstance(correction, dict):
                    merged_draft.update(correction)
                else:
                    merged_draft["event_narrative"] = str(correction)
                result["investigation_draft"] = merged_draft

        return result

    @staticmethod
    def _sanitize_feedback(raw_feedback: Any) -> tuple[Any, int]:
        if isinstance(raw_feedback, str):
            sanitized_text, count, _resolvable = sanitize_worker_pii(raw_feedback)
            return sanitized_text, count
        if isinstance(raw_feedback, dict):
            total = 0
            cleaned: dict[str, Any] = {}
            for k, v in raw_feedback.items():
                cleaned_v, count = HitlReviewNode._sanitize_feedback(v)
                total += count
                cleaned[k] = cleaned_v
            return cleaned, total
        if isinstance(raw_feedback, list):
            total = 0
            cleaned_list = []
            for item in raw_feedback:
                cleaned_item, count = HitlReviewNode._sanitize_feedback(item)
                total += count
                cleaned_list.append(cleaned_item)
            return cleaned_list, total
        return raw_feedback, 0
