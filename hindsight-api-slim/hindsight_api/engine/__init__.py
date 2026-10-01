"""
Memory Engine - Core implementation of the memory system.

This package contains all the implementation details of the memory engine:
- MemoryEngine: Main class for memory operations
- Utility modules: embedding_utils, link_utils, bank_utils
- Supporting modules: embeddings, cross_encoder, entity_resolver, etc.

Public exports resolve on access. Importing a parser in a spawned OCR process
must not initialize the application, provider configuration or local ML stack.
"""

from typing import TYPE_CHECKING

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "MemoryEngine": (".memory_engine", "MemoryEngine"),
    "acquire_with_retry": (".db_utils", "acquire_with_retry"),
    "Embeddings": (".embeddings", "Embeddings"),
    "LocalSTEmbeddings": (".embeddings", "LocalSTEmbeddings"),
    "RemoteTEIEmbeddings": (".embeddings", "RemoteTEIEmbeddings"),
    "CrossEncoderModel": (".cross_encoder", "CrossEncoderModel"),
    "LocalSTCrossEncoder": (".cross_encoder", "LocalSTCrossEncoder"),
    "RemoteTEICrossEncoder": (".cross_encoder", "RemoteTEICrossEncoder"),
    "SearchTrace": (".search.trace", "SearchTrace"),
    "SearchTracer": (".search.tracer", "SearchTracer"),
    "QueryInfo": (".search.trace", "QueryInfo"),
    "EntryPoint": (".search.trace", "EntryPoint"),
    "NodeVisit": (".search.trace", "NodeVisit"),
    "WeightComponents": (".search.trace", "WeightComponents"),
    "SearchSummary": (".search.trace", "SearchSummary"),
    "SearchPhaseMetrics": (".search.trace", "SearchPhaseMetrics"),
    "LLMConfig": (".llm_wrapper", "LLMConfig"),
    "RecallResult": (".response_models", "RecallResult"),
    "ReflectResult": (".response_models", "ReflectResult"),
    "MemoryFact": (".response_models", "MemoryFact"),
    "fq_table": (".memory_engine", "fq_table"),
    "get_current_schema": (".memory_engine", "get_current_schema"),
    "validate_sql_schema": (".memory_engine", "validate_sql_schema"),
    "UnqualifiedTableError": (".memory_engine", "UnqualifiedTableError"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    """Resolve and cache public exports, preserving their original identities."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    from importlib import import_module

    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:  # pragma: no cover
    from .cross_encoder import CrossEncoderModel, LocalSTCrossEncoder, RemoteTEICrossEncoder  # noqa: F401
    from .db_utils import acquire_with_retry  # noqa: F401
    from .embeddings import Embeddings, LocalSTEmbeddings, RemoteTEIEmbeddings  # noqa: F401
    from .llm_wrapper import LLMConfig  # noqa: F401
    from .memory_engine import (  # noqa: F401
        MemoryEngine,
        UnqualifiedTableError,
        fq_table,
        get_current_schema,
        validate_sql_schema,
    )
    from .response_models import MemoryFact, RecallResult, ReflectResult  # noqa: F401
    from .search.trace import (  # noqa: F401
        EntryPoint,
        NodeVisit,
        QueryInfo,
        SearchPhaseMetrics,
        SearchSummary,
        SearchTrace,
        WeightComponents,
    )
    from .search.tracer import SearchTracer  # noqa: F401
