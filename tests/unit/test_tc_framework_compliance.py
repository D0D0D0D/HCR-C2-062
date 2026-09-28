"""HCR-C2-062 — TC-01/02/04/06/07 framework compliance tests
(framework compliance TC set; TC-03/05/08 are covered elsewhere: TC-03 by the credential-scan
CI, TC-05 by the emit_trace_event assertions embedded across the node test
files, TC-08 by tests/unit/test_pre_process_node.py's node(state) __call__
tests + PB-6/PB-7)."""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class TestTC01StateContractEnforcement:
    """TC-01: State is a flat TypedDict — no Pydantic/dataclass fields."""

    def test_outer_state_is_plain_typed_dict(self):
        from src.schemas.state import State

        # TypedDict classes are plain dict subclasses at runtime — no
        # Pydantic BaseModel / dataclass machinery attached.
        assert not hasattr(State, "model_fields")  # would exist on a Pydantic BaseModel
        assert not hasattr(State, "__dataclass_fields__")

    def test_inner_state_is_plain_typed_dict(self):
        from src.schemas.state import DeviationWorkflowState

        assert not hasattr(DeviationWorkflowState, "model_fields")
        assert not hasattr(DeviationWorkflowState, "__dataclass_fields__")

    def test_outer_state_instance_is_a_dict(self):
        from src.schemas.state import State

        instance: State = {"user_input": "x", "status": "success"}  # type: ignore[assignment]
        assert isinstance(instance, dict)


class TestTC02SecurityViolationErrorFires:
    """TC-02: SecurityViolationError raised on invalid/malicious input."""

    def test_provenance_gate_raises_security_violation_error(self):
        from src.nodes.main_node import DeviationWorkflowGraphNode

        node = DeviationWorkflowGraphNode()
        bad_result = {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "cited_source_ids": ["src_fabricated"],
            "retrieved_records": [],
            "evidence_map": [],
        }
        with pytest.raises(SecurityViolationError):
            node._extra_security_gate_output(bad_result)


class TestTC04InvocationContextViaConfigurableOnly:
    """TC-04: InvocationContext is obtained via InvocationContext.from_state(),
    never constructed directly inside node execute() bodies (a framework
    prohibition — direct construction binds NullProvider, secrets.require()
    always raises)."""

    def test_pre_process_node_uses_from_state_not_direct_construction(self):
        import ast
        import inspect

        from src.nodes import pre_process_node

        source = inspect.getsource(pre_process_node)
        tree = ast.parse(source)

        direct_constructions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InvocationContext":
                direct_constructions.append(node.lineno)

        assert not direct_constructions, (
            f"PreProcessNode must use InvocationContext.from_state(state), "
            f"not direct InvocationContext(...) construction at lines {direct_constructions}"
        )

    def test_main_node_does_not_construct_invocation_context_manually(self):
        """GraphNode subclasses must not construct InvocationContext manually
        — GraphNode.execute() handles propagation automatically."""
        import ast
        import inspect

        from src.nodes import main_node

        source = inspect.getsource(main_node)
        tree = ast.parse(source)

        direct_constructions = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InvocationContext"
        ]
        assert not direct_constructions


class TestTC06S2GateNonBypassable:
    """TC-06: FunctionNode._security_gate_input() is @final — overriding it
    directly raises TypeError at class definition."""

    def test_overriding_security_gate_input_raises_type_error(self):
        with pytest.raises(TypeError):

            class BadNode(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_input(self, state):  # noqa: ANN001 — intentional violation
                    return state

                def execute(self, state):
                    return {}


class TestTC07S3GateNonBypassable:
    """TC-07: FunctionNode._security_gate_output() is @final — overriding it
    directly raises TypeError at class definition."""

    def test_overriding_security_gate_output_raises_type_error(self):
        with pytest.raises(TypeError):

            class BadNode(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_output(self, result):  # noqa: ANN001 — intentional violation
                    return result

                def execute(self, state):
                    return {}

    def test_all_function_node_subclasses_use_extra_hook_pattern(self):
        """Domain FunctionNode subclasses extend via _extra_security_gate_*
        hooks only — never redefine the @final methods (which would have
        already failed at class-definition time if attempted)."""
        from src.nodes.feedback_node import FeedbackValidateNode, KbUpdateEffectivenessNode
        from src.nodes.hitl_review_node import HitlReviewNode
        from src.nodes.ingest_nodes import IngestParseNode, IngestSanitizeNode, KbUpsertNode, TaxonomyTagNode
        from src.nodes.invoke_stage_nodes import Stage1StructureNode, Stage2SearchNode, Stage3DraftNode, Stage4CapaNode
        from src.nodes.mode_router_node import ModeRouterNode
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        function_node_classes = [
            FeedbackValidateNode,
            KbUpdateEffectivenessNode,
            HitlReviewNode,
            IngestParseNode,
            IngestSanitizeNode,
            KbUpsertNode,
            TaxonomyTagNode,
            Stage1StructureNode,
            Stage2SearchNode,
            Stage3DraftNode,
            Stage4CapaNode,
            ModeRouterNode,
            PostProcessNode,
            PreProcessNode,
        ]
        for cls in function_node_classes:
            assert "_security_gate_input" not in cls.__dict__
            assert "_security_gate_output" not in cls.__dict__


class TestTrustLevelDeclaredExplicitly:
    """All FunctionNode subclasses declare required_trust_level explicitly
    (implicit ANONYMOUS inheritance is not acceptable)."""

    def test_all_nodes_declare_required_trust_level_in_class_body(self):
        from src.nodes.feedback_node import FeedbackValidateNode, KbUpdateEffectivenessNode
        from src.nodes.hitl_review_node import HitlReviewNode
        from src.nodes.ingest_nodes import IngestParseNode, IngestSanitizeNode, KbUpsertNode, TaxonomyTagNode
        from src.nodes.invoke_stage_nodes import Stage1StructureNode, Stage2SearchNode, Stage3DraftNode, Stage4CapaNode
        from src.nodes.main_node import DeviationWorkflowGraphNode
        from src.nodes.mode_router_node import ModeRouterNode
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        all_node_classes = [
            FeedbackValidateNode,
            KbUpdateEffectivenessNode,
            HitlReviewNode,
            IngestParseNode,
            IngestSanitizeNode,
            KbUpsertNode,
            TaxonomyTagNode,
            Stage1StructureNode,
            Stage2SearchNode,
            Stage3DraftNode,
            Stage4CapaNode,
            DeviationWorkflowGraphNode,
            ModeRouterNode,
            PostProcessNode,
            PreProcessNode,
        ]
        valid_values = {TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL}
        for cls in all_node_classes:
            assert (
                "required_trust_level" in cls.__dict__
            ), f"{cls.__name__} must declare required_trust_level explicitly"
            assert cls.required_trust_level in valid_values, f"{cls.__name__} has an invalid TrustLevel value"
