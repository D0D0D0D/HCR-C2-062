"""HCR-C2-062 — static manifest and runtime config consistency guards."""

from __future__ import annotations

import pathlib

import yaml

_REPO_ROOT = pathlib.Path(__file__).parents[2]


def _load_yaml(relpath: str) -> dict:
    path = _REPO_ROOT / relpath
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestHitlEnabledConsistency:
    def test_agent_yaml_does_not_carry_runtime_hitl_tunables(self):
        """config/agent.yaml is AgentRegistry static metadata only."""
        agent_yaml = _load_yaml("config/agent.yaml")
        assert "hitl" not in agent_yaml

    def test_config_yaml_declares_hitl_enabled(self):
        """PB-7 applicability is driven by runtime config/config.yaml."""
        config_yaml = _load_yaml("config/config.yaml")
        assert config_yaml.get("hitl", {}).get("enabled") is True

    def test_memory_enabled_required_when_hitl_enabled(self):
        config_yaml = _load_yaml("config/config.yaml")
        if config_yaml.get("hitl", {}).get("enabled", False):
            assert (
                config_yaml.get("memory_enabled") is True
            ), "hitl.enabled=true requires memory_enabled=true (checkpointer mandatory, ADR-016 D5)"

    def test_cat_consistency_across_sources(self):
        agent_yaml = _load_yaml("config/agent.yaml")
        assert agent_yaml.get("category") == "Cat 2"
        assert agent_yaml.get("base_type") == "AgentBaseGraph"
        assert agent_yaml.get("class") == "src.graph.graph.GmpDeviationCapaGraph"

        readme_path = _REPO_ROOT / "README.md"
        if readme_path.exists():
            readme_text = readme_path.read_text(encoding="utf-8")
            assert "Cat 2" in readme_text, "README.md must declare **Category**: Cat 2"
