"""Fail closed at configurable code boundaries before handing them private input."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..private_reads import PrivateReadUnsupportedError, is_private_read

if TYPE_CHECKING:
    from .memory_engine import MemoryEngine


def require_private_component(component: object, allowed: tuple[type, ...]) -> None:
    """Exact types, not opt-in attributes or subclasses that can override a sink."""
    if is_private_read() and type(component) not in allowed:
        raise PrivateReadUnsupportedError("Configured component does not support private reads")


def check_private_read_extensions(memory: MemoryEngine) -> None:
    """Run before alias resolution/auth, including direct engine calls.

    An arbitrary extension may write files, spawn a subprocess, or enqueue work
    outside ContextVars. Never skip its auth/visibility hook to make a read work.
    Unsupported deployments reject the entire read instead.
    """
    if not is_private_read():
        return
    from ..config_resolver import ConfigResolver
    from ..extensions.builtin.tenant import ApiKeyTenantExtension, DefaultTenantExtension
    from .memory_engine import MemoryEngine

    require_private_component(memory, (MemoryEngine,))
    if getattr(memory, "_initialized", False) is not True:
        raise PrivateReadUnsupportedError("Private reads require an initialized engine")
    require_private_component(getattr(memory, "_config_resolver", None), (ConfigResolver,))
    require_private_component(
        memory._config_resolver.tenant_extension, (ApiKeyTenantExtension, DefaultTenantExtension, type(None))
    )
    require_private_component(memory._tenant_extension, (ApiKeyTenantExtension, DefaultTenantExtension))
    if memory._operation_validator is not None:
        raise PrivateReadUnsupportedError("Operation extensions do not support private reads")


def check_private_read_retrieval(memory: MemoryEngine) -> None:
    """Contain injectable retrieval implementations, including failover members."""
    if not is_private_read():
        return
    from .cross_encoder import (
        AlibabaCloudCrossEncoder,
        CohereCrossEncoder,
        FlashRankCrossEncoder,
        GoogleCrossEncoder,
        JinaMLXCrossEncoder,
        LiteLLMCrossEncoder,
        LocalSTCrossEncoder,
        MultiCrossEncoder,
        RemoteTEICrossEncoder,
        RRFPassthroughCrossEncoder,
        SiliconFlowCrossEncoder,
        ZeroEntropyCrossEncoder,
    )
    from .embeddings import (
        CohereEmbeddings,
        GeminiEmbeddings,
        LiteLLMEmbeddings,
        LocalSTEmbeddings,
        OnnxEmbeddings,
        OpenAIEmbeddings,
        RemoteTEIEmbeddings,
        ZeroEntropyEmbeddings,
    )
    from .query_analyzer import DateparserQueryAnalyzer, TransformerQueryAnalyzer
    from .search.reranking import CrossEncoderReranker

    require_private_component(
        memory.embeddings,
        (
            CohereEmbeddings,
            GeminiEmbeddings,
            LiteLLMEmbeddings,
            LocalSTEmbeddings,
            OnnxEmbeddings,
            OpenAIEmbeddings,
            RemoteTEIEmbeddings,
            ZeroEntropyEmbeddings,
        ),
    )
    require_private_component(memory.query_analyzer, (DateparserQueryAnalyzer, TransformerQueryAnalyzer))
    require_private_component(memory._cross_encoder_reranker, (CrossEncoderReranker,))
    encoder = memory._cross_encoder_reranker.cross_encoder
    supported = (
        AlibabaCloudCrossEncoder,
        CohereCrossEncoder,
        FlashRankCrossEncoder,
        GoogleCrossEncoder,
        JinaMLXCrossEncoder,
        LiteLLMCrossEncoder,
        LocalSTCrossEncoder,
        RemoteTEICrossEncoder,
        RRFPassthroughCrossEncoder,
        SiliconFlowCrossEncoder,
        ZeroEntropyCrossEncoder,
    )
    if type(encoder) is MultiCrossEncoder:
        for member in encoder._members:
            require_private_component(member, supported)
    else:
        require_private_component(encoder, supported)


def check_private_provider_name(provider: str) -> None:
    """Reject unsafe aliases even when they share a native SDK implementation."""
    if is_private_read() and provider not in {
        "openai",
        "openai-responses",
        "anthropic",
        "gemini",
        "fireworks",
        "minimax",
        "groq",
        "ollama",
        "lmstudio",
        "openai-compatible",
        "mock",
        "none",
        "nous",
    }:
        raise PrivateReadUnsupportedError("Configured LLM provider does not support private reads")


def check_private_llm_config(config: object) -> None:
    """Validate the native wrapper and every failover member before selection."""
    if not is_private_read():
        return
    from .llm_wrapper import LLMProvider
    from .multi_llm import MultiLLMProvider

    require_private_component(config, (LLMProvider, MultiLLMProvider))
    if isinstance(config, MultiLLMProvider):
        for member in config.members:
            check_private_llm_config(member)
    elif isinstance(config, LLMProvider):
        check_private_provider_name(config.provider)
        check_private_llm(config._provider_impl)


def check_private_llm(implementation: object) -> None:
    """Only source-owned in-process providers with the shared diagnostic owners.

    SDK callback routers, CLI/subprocess agents and credential-refreshing OAuth
    wrappers are intentionally unsupported, not silently routed elsewhere.
    """
    if not is_private_read():
        return
    from .providers import (
        AnthropicLLM,
        FireworksLLM,
        GeminiLLM,
        MockLLM,
        NoneLLM,
        OpenAICompatibleLLM,
        OpenAIResponsesLLM,
    )

    require_private_component(
        implementation,
        (AnthropicLLM, FireworksLLM, GeminiLLM, MockLLM, NoneLLM, OpenAICompatibleLLM, OpenAIResponsesLLM),
    )
