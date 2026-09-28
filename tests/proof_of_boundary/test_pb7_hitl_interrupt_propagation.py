# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# HCR-C2-062: config/config.yaml has hitl.enabled: true, so PB-7 is
# mandatory. These assertions use the actual compiled outer graph. Calling
# HitlReviewNode directly is not a valid proof because langgraph interrupt()
# only works inside a runnable graph context.

from __future__ import annotations

import pathlib
import warnings

import pytest

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)


def _build_graph():
    from langgraph.checkpoint.memory import InMemorySaver

    from src.graph.graph import GmpDeviationCapaGraph

    graph = GmpDeviationCapaGraph(config={"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 12}})
    graph.compile(checkpointer=InMemorySaver())
    return graph


def _ctx(thread_id: str, *, hitl_allowed: bool = True):
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    return InvocationContext(
        session_id=thread_id,
        thread_id=thread_id,
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=hitl_allowed,
    )


def _invoke_context() -> dict:
    return {
        "mode": "invoke",
        "company_id": "ACME",
        "site_id": "SITE1",
        "free_text": "filling line anomaly",
    }


def test_pb7_hitl_interrupt_propagates() -> None:
    graph = _build_graph()
    result = graph.invoke("incident occurred", ctx=_ctx("pb7-propagate"), input_context=_invoke_context())
    assert result["status"] == "awaiting_human"
    assert result.get("thread_id") == "pb7-propagate"
    assert result["status"] != "error"


def test_pb7_hitl_allowed_false_skips_interrupt_in_real_graph() -> None:
    graph = _build_graph()
    result = graph.invoke(
        "incident occurred",
        ctx=_ctx("pb7-skip", hitl_allowed=False),
        input_context=_invoke_context(),
    )
    assert result["status"] == "success"
    assert result["status"] != "awaiting_human"
    assert result["output"]["gate_kind"] == "draft"
    assert result["output"]["report"] is not None


def test_pb7_resume_reaches_success() -> None:
    graph = _build_graph()
    graph.invoke("incident occurred", ctx=_ctx("pb7-resume"), input_context=_invoke_context())
    resumed = graph.resume(thread_id="pb7-resume", feedback="approve")
    assert resumed["status"] == "success"
    assert resumed["output"]["gate_kind"] == "draft"
