"""Conversion-task policy and failure safety, with storage as the observable boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Connection(_Transaction):
    def __init__(self):
        self.execute = AsyncMock()

    def transaction(self):
        return _Transaction()


class _Backend:
    async def acquire(self):
        return _Connection()

    async def release(self, connection):
        return None


@pytest.fixture
def conversion_engine():
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._file_storage = SimpleNamespace(retrieve=AsyncMock(return_value=b"original"), delete=AsyncMock())
    engine._parser_registry = SimpleNamespace(
        convert_with_fallback=AsyncMock(return_value=SimpleNamespace(content="converted", parser_name="parser"))
    )
    engine._operation_validator = None
    engine._task_backend = SimpleNamespace(submit_task=AsyncMock())
    engine._get_backend = AsyncMock(return_value=_Backend())
    engine._config_resolver = SimpleNamespace(resolve_full_config=AsyncMock())
    return engine


def _task():
    return {
        "bank_id": "synthetic-preservation-bank",
        "storage_key": "banks/synthetic-preservation-bank/original.txt",
        "document_id": "source-document",
        "original_filename": "original.txt",
        "content_type": "text/plain",
        "parser": ["parser"],
        "_tenant_id": "synthetic-tenant",
        "_api_key_id": "synthetic-key-id",
        "_retry_count": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_original", [False, True])
async def test_conversion_resolves_policy_in_task_context_and_keeps_file_association(
    conversion_engine, delete_original
):
    engine = conversion_engine
    engine._config_resolver.resolve_full_config.return_value = SimpleNamespace(file_delete_after_retain=delete_original)
    await engine._handle_file_convert_retain(_task())
    args, kwargs = engine._config_resolver.resolve_full_config.call_args
    assert args[0] == _task()["bank_id"]
    context = args[1]
    assert context.internal and context.user_initiated
    assert context.tenant_id == "synthetic-tenant"
    assert context.api_key_id == "synthetic-key-id"
    assert context.retry_count == 2
    assert kwargs == {"cached": False, "fail_closed": True}
    downstream = engine._task_backend.submit_task.call_args.args[0]
    assert downstream["contents"][0]["document_id"] == "source-document"
    assert downstream["_file_metadata"]["file_storage_key"] == _task()["storage_key"]
    assert downstream["_tenant_id"] == "synthetic-tenant"
    if delete_original:
        engine._file_storage.delete.assert_awaited_once_with(_task()["storage_key"])
    else:
        engine._file_storage.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config", "conversion", "submission"])
async def test_conversion_failures_never_delete_originals(conversion_engine, failure):
    engine = conversion_engine
    engine._config_resolver.resolve_full_config.return_value = SimpleNamespace(file_delete_after_retain=True)
    if failure == "config":
        engine._config_resolver.resolve_full_config.side_effect = RuntimeError("config unavailable")
    elif failure == "conversion":
        engine._parser_registry.convert_with_fallback.side_effect = RuntimeError("parse failed")
    else:
        engine._task_backend.submit_task.side_effect = RuntimeError("submission failed")
    with pytest.raises(RuntimeError):
        await engine._handle_file_convert_retain(_task())
    engine._file_storage.delete.assert_not_awaited()
    if failure != "submission":
        engine._task_backend.submit_task.assert_not_awaited()


class _ConfigReadConnection(_Transaction):
    def __init__(self, failure=None, config=None):
        self.failure = failure
        self.config = config

    async def __aenter__(self):
        if self.failure == "acquire":
            raise RuntimeError("pool unavailable")
        return self

    async def fetchrow(self, query, bank_id):
        if self.failure == "query":
            raise RuntimeError("config query unavailable")
        if self.failure == "missing":
            return None
        return {"config": self.config}


class _ConfigReadBackend:
    def __init__(self, failure=None, config=None):
        self.connection = _ConfigReadConnection(failure, config)

    def acquire(self):
        return self.connection


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["acquire", "query", "missing"])
async def test_real_resolver_failure_preserves_original_before_conversion(conversion_engine, failure):
    """The real config loader must not turn a failed lookup into process True."""
    from hindsight_api.config_resolver import ConfigResolver, ConfigUnavailableError

    engine = conversion_engine
    engine._config_resolver = ConfigResolver(backend=_ConfigReadBackend(failure=failure))
    with pytest.raises(ConfigUnavailableError):
        await engine._handle_file_convert_retain(_task())
    engine._file_storage.retrieve.assert_not_awaited()
    engine._parser_registry.convert_with_fallback.assert_not_awaited()
    engine._task_backend.submit_task.assert_not_awaited()
    engine._get_backend.assert_not_awaited()
    engine._file_storage.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [None, {}, {"file_delete_after_retain": None}])
async def test_real_resolver_existing_empty_policy_still_inherits(config):
    from hindsight_api.config import _get_raw_config
    from hindsight_api.config_resolver import ConfigResolver

    resolver = ConfigResolver(backend=_ConfigReadBackend(config=config))
    resolved = await resolver.resolve_full_config(_task()["bank_id"], fail_closed=True)
    assert resolved.file_delete_after_retain is _get_raw_config().file_delete_after_retain


@pytest.mark.asyncio
@pytest.mark.parametrize("process_policy", [True, False])
@pytest.mark.parametrize("tenant_key", ["file_delete_after_retain", "HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN"])
@pytest.mark.parametrize("bank_config", [{}, {"file_delete_after_retain": None}])
@pytest.mark.parametrize("fail_closed", [True, False])
async def test_tenant_null_preservation_policy_inherits_process(process_policy, tenant_key, bank_config, fail_closed):
    from dataclasses import replace

    from hindsight_api.config_resolver import ConfigResolver
    from hindsight_api.models import RequestContext

    tenant = SimpleNamespace(get_tenant_config=AsyncMock(return_value={tenant_key: None}))
    resolver = ConfigResolver(backend=_ConfigReadBackend(config=bank_config), tenant_extension=tenant)
    process_config = resolver._global_config
    resolver._global_config = replace(process_config, file_delete_after_retain=process_policy)
    context = RequestContext(internal=True, tenant_id="preservation-tenant")

    resolved = await resolver.resolve_full_config(_task()["bank_id"], context, cached=False, fail_closed=fail_closed)

    assert resolved.file_delete_after_retain is process_policy
    assert tenant.get_tenant_config.return_value == {tenant_key: None}


@pytest.mark.asyncio
@pytest.mark.parametrize("process_policy", [True, False])
async def test_conversion_tenant_null_uses_inherited_process_policy(conversion_engine, process_policy):
    from dataclasses import replace

    from hindsight_api.config_resolver import ConfigResolver

    tenant = SimpleNamespace(get_tenant_config=AsyncMock(return_value={"file_delete_after_retain": None}))
    resolver = ConfigResolver(backend=_ConfigReadBackend(config={}), tenant_extension=tenant)
    resolver._global_config = replace(resolver._global_config, file_delete_after_retain=process_policy)
    conversion_engine._config_resolver = resolver

    await conversion_engine._handle_file_convert_retain(_task())

    conversion_engine._task_backend.submit_task.assert_awaited_once()
    if process_policy:
        conversion_engine._file_storage.delete.assert_awaited_once_with(_task()["storage_key"])
    else:
        conversion_engine._file_storage.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_closed_resolution_bypasses_cached_policy(conversion_engine):
    from hindsight_api.config_resolver import ConfigResolver, ConfigUnavailableError

    backend = _ConfigReadBackend(config={"file_delete_after_retain": False})
    resolver = ConfigResolver(backend=backend)
    assert (await resolver.resolve_full_config(_task()["bank_id"])).file_delete_after_retain is False
    backend.connection.failure = "query"
    # cached=True is the ordinary resolver default. A destructive consumer must
    # prove current policy rather than accept the cached value on read failure.
    with pytest.raises(ConfigUnavailableError):
        await resolver.resolve_full_config(_task()["bank_id"], fail_closed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["acquire", "query", "missing"])
async def test_ordinary_resolver_still_uses_existing_fallback(failure):
    from hindsight_api.config import _get_raw_config
    from hindsight_api.config_resolver import ConfigResolver

    resolver = ConfigResolver(backend=_ConfigReadBackend(failure=failure))
    resolved = await resolver.resolve_full_config(_task()["bank_id"], cached=False)
    assert resolved.file_delete_after_retain is _get_raw_config().file_delete_after_retain


@pytest.mark.asyncio
async def test_strict_pre_create_resolution_uses_defaults_only_for_missing_bank():
    from hindsight_api.config import _get_raw_config
    from hindsight_api.config_resolver import ConfigResolver

    resolver = ConfigResolver(backend=_ConfigReadBackend(failure="missing"))
    resolved = await resolver.resolve_full_config(
        _task()["bank_id"], cached=False, fail_closed=True, allow_missing_bank=True
    )
    assert resolved.file_delete_after_retain is _get_raw_config().file_delete_after_retain


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["acquire", "query"])
async def test_strict_pre_create_resolution_still_fails_closed_on_read_errors(failure):
    from hindsight_api.config_resolver import ConfigResolver, ConfigUnavailableError

    resolver = ConfigResolver(backend=_ConfigReadBackend(failure=failure))
    with pytest.raises(ConfigUnavailableError):
        await resolver.resolve_full_config(_task()["bank_id"], cached=False, fail_closed=True, allow_missing_bank=True)


@pytest.mark.asyncio
async def test_real_tenant_resolution_failure_preserves_original(conversion_engine):
    from hindsight_api.config_resolver import ConfigResolver, ConfigUnavailableError

    tenant = SimpleNamespace(get_tenant_config=AsyncMock(side_effect=RuntimeError("tenant config unavailable")))
    engine = conversion_engine
    resolver = ConfigResolver(backend=_ConfigReadBackend(config={}), tenant_extension=tenant)
    engine._config_resolver = resolver
    with pytest.raises(ConfigUnavailableError):
        await engine._handle_file_convert_retain(_task())
    engine._file_storage.retrieve.assert_not_awaited()
    engine._task_backend.submit_task.assert_not_awaited()
    engine._file_storage.delete.assert_not_awaited()
    # Only the strict worker path changes. Other callers keep best-effort tenant
    # resolution, including the inherited process value when the hook fails.
    ordinary = await resolver.resolve_full_config(
        _task()["bank_id"], tenant.get_tenant_config.call_args.args[0], cached=False
    )
    assert ordinary.file_delete_after_retain is resolver._global_config.file_delete_after_retain


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", 0, 1, {}, []])
@pytest.mark.parametrize("source", ["bank", "tenant"])
async def test_real_resolver_rejects_malformed_preservation_policy_before_deletion(conversion_engine, value, source):
    from hindsight_api.config_resolver import ConfigResolver, ConfigUnavailableError

    overrides = {"HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN": value}
    backend = _ConfigReadBackend(config=overrides if source == "bank" else {})
    tenant = SimpleNamespace(get_tenant_config=AsyncMock(return_value=overrides)) if source == "tenant" else None
    engine = conversion_engine
    engine._config_resolver = ConfigResolver(backend=backend, tenant_extension=tenant)
    with pytest.raises(ConfigUnavailableError, match="file_delete_after_retain must be a boolean or null"):
        await engine._handle_file_convert_retain(_task())
    engine._file_storage.retrieve.assert_not_awaited()
    engine._parser_registry.convert_with_fallback.assert_not_awaited()
    engine._task_backend.submit_task.assert_not_awaited()
    engine._file_storage.delete.assert_not_awaited()
