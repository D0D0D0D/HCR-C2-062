"""HCR-C2-062 — DeviationWorkflowGraphNode (main slot GraphNode) unit tests.

Covers: S-3 Provenance gate (critical), merge_output gate_kind declaration,
required_trust_level explicit declaration (GraphNode defaults to ANONYMOUS),
subgraph caching.
"""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import DeviationWorkflowGraphNode


class TestRequiredTrustLevel:
    def test_explicit_declaration_not_anonymous_default(self):
        """docs/02_design.md: GraphNode defaults to ANONYMOUS — must be
        explicitly overridden (SubcontractorQuotationDisputeHistory real-bug precedent)."""
        assert DeviationWorkflowGraphNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_is_valid_enum_value(self):
        assert DeviationWorkflowGraphNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )


class TestSubgraphCaching:
    def test_get_subgraph_returns_same_instance(self):
        node = DeviationWorkflowGraphNode()
        first = node.get_subgraph()
        second = node.get_subgraph()
        assert first is second


class TestMergeOutputGateKindDeclaration:
    def test_ingest_mode_declares_ingest_gate_kind(self):
        node = DeviationWorkflowGraphNode()
        state = {"mode": "ingest"}
        sub_result = {"status": AgentStatus.SUCCESS.value, "ingest_summary": {"count": 1}}
        delta = node.merge_output(state, sub_result)
        assert delta["gate_kind"] == "ingest"

    def test_feedback_mode_declares_feedback_gate_kind(self):
        node = DeviationWorkflowGraphNode()
        state = {"mode": "feedback"}
        sub_result = {"status": AgentStatus.SUCCESS.value, "feedback_summary": {"recorded": True}}
        delta = node.merge_output(state, sub_result)
        assert delta["gate_kind"] == "feedback"

    def test_invoke_mode_declares_draft_gate_kind(self):
        node = DeviationWorkflowGraphNode()
        state = {"mode": "invoke"}
        sub_result = {
            "status": AgentStatus.SUCCESS.value,
            "output": {},
            "evidence_map": [],
            "cited_source_ids": [],
            "retrieved_records": [],
            "recurrence_flags": [],
        }
        delta = node.merge_output(state, sub_result)
        assert delta["gate_kind"] == "draft"

    def test_none_fill_prohibited_for_absent_optional_fields(self):
        """merge_output must not force-fill fields the sub_result did not
        provide (docs/02_design.md '実行されたモードのフィールドだけを返す')."""
        node = DeviationWorkflowGraphNode()
        state = {"mode": "ingest"}
        sub_result = {"status": AgentStatus.SUCCESS.value, "ingest_summary": {}}
        delta = node.merge_output(state, sub_result)
        assert "degradation_reason" not in delta
        assert "hitl_draft" not in delta


class TestS3ProvenanceGate:
    """docs/02_design.md S-3 設計 第1層 — critical."""

    def _draft_result(self, **overrides) -> dict:
        result = {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "cited_source_ids": ["src_1"],
            "retrieved_records": [{"source_id": "src_1", "deviation_category": "process"}],
            "evidence_map": [
                {
                    "claim_id": "C-01",
                    "source_ids": ["src_1"],
                    "asserted_attrs": {"src_1": {"deviation_category": "process"}},
                }
            ],
        }
        result.update(overrides)
        return result

    def test_valid_citations_pass(self):
        node = DeviationWorkflowGraphNode()
        result = node._extra_security_gate_output(self._draft_result())
        assert result["cited_source_ids"] == ["src_1"]

    def test_fabricated_id_raises(self):
        """🔴 捏造 ID の遮断."""
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(cited_source_ids=["src_1", "src_FABRICATED"])
        with pytest.raises(SecurityViolationError, match="non-existent ids"):
            node._extra_security_gate_output(bad)

    def test_real_but_unrelated_source_id_raises(self):
        """🔴 実在するが無関係な source_id（属性不一致）の遮断."""
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(
            evidence_map=[
                {
                    "claim_id": "C-01",
                    "source_ids": ["src_1"],
                    # actual record has deviation_category=process, claim asserts equipment
                    "asserted_attrs": {"src_1": {"deviation_category": "equipment"}},
                }
            ]
        )
        with pytest.raises(SecurityViolationError, match="misattributes"):
            node._extra_security_gate_output(bad)

    def test_missing_gate_kind_raises_fail_closed(self):
        """🔴 gate_kind 欠落 -> fail closed (検査不要と解釈しない)."""
        node = DeviationWorkflowGraphNode()
        result = {"status": AgentStatus.SUCCESS.value}  # no gate_kind at all
        with pytest.raises(SecurityViolationError, match="gate_kind is not declared"):
            node._extra_security_gate_output(result)

    def test_unknown_gate_kind_raises_fail_closed(self):
        node = DeviationWorkflowGraphNode()
        result = {"status": AgentStatus.SUCCESS.value, "gate_kind": "unknown_path"}
        with pytest.raises(SecurityViolationError, match="gate_kind is not declared"):
            node._extra_security_gate_output(result)

    def test_missing_citation_fields_on_draft_path_raises(self):
        """🔴 draft 経路で evidence_map/cited_source_ids/retrieved_records の
        いずれかが None -> raise（.get(…, []) で握りつぶさない）。"""
        node = DeviationWorkflowGraphNode()
        result = {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "cited_source_ids": None,  # explicitly absent, not []
            "retrieved_records": [{"source_id": "src_1"}],
            "evidence_map": [],
        }
        with pytest.raises(SecurityViolationError, match="must carry evidence_map"):
            node._extra_security_gate_output(result)

    def test_cited_source_ids_rejects_non_string_before_membership(self):
        """LLM-origin values must be type-checked before set membership."""
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(cited_source_ids=[{"raw": "not-hashable"}])
        with pytest.raises(SecurityViolationError, match="cited_source_ids"):
            node._extra_security_gate_output(bad)

    def test_evidence_map_rejects_non_dict_claim_before_get(self):
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(evidence_map=["not-a-dict"])
        with pytest.raises(SecurityViolationError, match="evidence_map"):
            node._extra_security_gate_output(bad)

    def test_asserted_attrs_rejects_non_dict_without_raw_value_leak(self):
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(evidence_map=[{"claim_id": "C-01", "source_ids": ["src_1"], "asserted_attrs": "raw"}])
        with pytest.raises(SecurityViolationError, match="asserted_attrs"):
            node._extra_security_gate_output(bad)

    def test_evidence_map_citing_nonexistent_id_raises(self):
        node = DeviationWorkflowGraphNode()
        bad = self._draft_result(
            evidence_map=[{"claim_id": "C-01", "source_ids": ["src_ghost"], "asserted_attrs": {"src_ghost": {}}}]
        )
        with pytest.raises(SecurityViolationError, match="non-existent id"):
            node._extra_security_gate_output(bad)

    def test_ingest_gate_kind_skips_provenance_check(self):
        """ingest/feedback paths have no LLM citations — not applicable."""
        node = DeviationWorkflowGraphNode()
        result = {"status": AgentStatus.SUCCESS.value, "gate_kind": "ingest", "ingest_summary": {}}
        gated = node._extra_security_gate_output(result)
        assert gated == result

    def test_upstream_error_passes_through_without_gate_kind(self):
        """Upstream status=error must pass through even with no gate_kind —
        error carries no business result to validate."""
        node = DeviationWorkflowGraphNode()
        result = {"status": AgentStatus.ERROR.value, "error_log": ["some upstream failure"]}
        gated = node._extra_security_gate_output(result)
        assert gated == result


class TestErrorStrategy:
    def test_error_strategy_is_handle_for_stable_projection(self):
        assert DeviationWorkflowGraphNode.error_strategy == "handle"

    def test_known_failure_codes_declared(self):
        assert "payload_ref_unresolved" in DeviationWorkflowGraphNode.known_failure_codes
        assert "subgraph_execution_failed" in DeviationWorkflowGraphNode.known_failure_codes

    def test_on_subgraph_error_maps_known_failure_without_raw_log(self):
        class FakeSubgraphError(Exception):
            error_log = ["IngestParseNode: no sanitized payload found for payload_ref SECRET-SENTINEL"]

        node = DeviationWorkflowGraphNode()
        result = node.on_subgraph_error({}, FakeSubgraphError())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_code"] == "payload_ref_unresolved"
        assert result["error_type"] == "inner_workflow_error"
        assert "SECRET-SENTINEL" not in json.dumps(result, default=str)

    def test_propagate_hitl_is_true(self):
        assert DeviationWorkflowGraphNode.propagate_hitl is True
