"""AGENTIC STAR Marketplace entry adapter for HCR-C2-062."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # pragma: no cover - Marketplace extra is absent in the local 1.0.1 wheel
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-Marketplace wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import GmpDeviationCapaGraph

_NAMESPACE = "hcr"
_AGENT_NAME = "HCR-C2-062"
_PROVIDER_AGENT_NAME = "hcr_c2_062"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_BY_KEY = {
    "company_id": "STG_DEFAULT_COMPANY_ID",
    "site_id": "STG_DEFAULT_SITE_ID",
}
_SCOPE_ENV_KEYS = tuple(_SCOPE_ENV_BY_KEY.values())
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent Pod's declared process environment."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        return super().get(key, default)


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without normalising bad input."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a non-empty string without whitespace")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build the complete tenant scope from deployment-owned values."""

    return {state_key: _require_clean(provider, env_name) for state_key, env_name in _SCOPE_ENV_BY_KEY.items()}


class MarketplaceAzureLLM(BaseLLM):  # type: ignore[misc]
    """Create a fresh Azure client for every operation and retain no credentials."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        # Azure support is an optional SDK extra and is the sole lazy import.
        # Framework/security imports stay eager because the runner dispatches
        # graph.invoke() through asyncio.to_thread().
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        provider = current_secrets()
        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
                "azure_endpoint": _require_azure_endpoint(provider),
                "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "MarketplaceAzureLLM":
        return type(self)(tools=tuple(tools))


class MarketplaceGraph(GmpDeviationCapaGraph):
    """Marketplace-only read boundary, tenant scope, HITL, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Nodes must therefore
        # capture this credential-free per-call facade during compile().
        marketplace_config = dict(config or {})
        marketplace_config["llm"] = MarketplaceAzureLLM()
        retrieval = marketplace_config.get("retrieval", {})
        if isinstance(retrieval, dict):
            marketplace_config.setdefault(
                "top_k_similar_deviations",
                retrieval.get("top_k_similar_deviations", 8),
            )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is untrusted free text outside this AG's
        # S-2 scan. Rebuild State input_context exclusively from adapter-owned
        # scope plus the runner's one plain-string message.
        del input_context
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        marketplace_input_context = {
            "mode": "invoke",
            **scope,
            "free_text": user_input,
        }

        # Marketplace has no resume channel. Preserve caller/trace identity but
        # force the authenticated Marketplace trust level and suppress HITL.
        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=marketplace_input_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's broader/dotenv-capable provider. Startup succeeds
        # only when every allowlisted Azure and tenant-scope value is clean.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
