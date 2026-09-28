"""HCR-C2-062 — PostProcessNode unit tests.

Covers S-3 layer-1 (prohibited fields) + layer-2 (lexicon) quality-verdict
gate, PII re-scan, degradation_reason folding, upstream error passthrough.
"""

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode
from tests.conftest import base_state


class TestPostProcessNodeErrorPassthrough:
    def test_upstream_error_passthrough(self):
        node = PostProcessNode()
        state = base_state(status=AgentStatus.ERROR.value, error_log=["upstream failure"])
        result = node.execute(state)
        assert result["formatted_output"]["status"] == AgentStatus.ERROR.value


class TestPostProcessNodeNonDraftPaths:
    def test_ingest_gate_kind_passthrough(self):
        node = PostProcessNode()
        state = base_state(status=AgentStatus.SUCCESS.value, gate_kind="ingest", ingest_summary={"count": 1})
        result = node.execute(state)
        assert result["formatted_output"]["gate_kind"] == "ingest"
        assert result["formatted_output"]["summary"] == {"count": 1}

    def test_feedback_gate_kind_passthrough(self):
        node = PostProcessNode()
        state = base_state(status=AgentStatus.SUCCESS.value, gate_kind="feedback", feedback_summary={"recorded": True})
        result = node.execute(state)
        assert result["formatted_output"]["gate_kind"] == "feedback"


class TestPostProcessNodeLayer1StructuralGuard:
    def test_prohibited_quality_verdict_field_detected(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"impact_assessment": {"quality_verdict": "pass"}},
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "prohibited" in result["error_log"][0].lower()

    def test_shippability_field_detected(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value, gate_kind="draft", result={"section": {"shippability": True}}
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_clean_output_without_prohibited_fields_passes(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"event_narrative": "類似逸脱DEV-001が過去に記録されている。"},
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestPostProcessNodeLayer2LexiconGuard:
    def test_assertive_statement_neutralized(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"event_narrative": "類似逸脱の記録がある。本ロットは出荷可能である。"},
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "出荷可能" not in result["formatted_output"]["report"]["event_narrative"]

    def test_entirely_assertive_text_fails_closed(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"event_narrative": "本ロットは出荷可能である。"},
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestPostProcessNodePiiRescan:
    def test_worker_name_rescanned_and_masked(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"event_narrative": "山田太郎さんが対応した。"},
        )
        result = node.execute(state)
        assert "山田太郎" not in result["formatted_output"]["report"]["event_narrative"]


class TestPostProcessNodeDegradationFolding:
    def test_degradation_reason_folded_into_formatted_output(self):
        node = PostProcessNode()
        state = base_state(
            status=AgentStatus.SUCCESS.value,
            gate_kind="draft",
            result={"event_narrative": "test"},
            degradation_reason="KB empty (cold start)",
        )
        result = node.execute(state)
        assert result["formatted_output"]["degradation_reason"] == "KB empty (cold start)"

    def test_no_degradation_reason_when_absent(self):
        node = PostProcessNode()
        state = base_state(status=AgentStatus.SUCCESS.value, gate_kind="draft", result={"event_narrative": "test"})
        result = node.execute(state)
        assert "degradation_reason" not in result["formatted_output"]


class TestPostProcessNodeRequiredTrustLevel:
    def test_required_trust_level_declared(self):
        assert PostProcessNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )
