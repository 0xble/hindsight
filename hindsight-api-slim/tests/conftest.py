"""
Pytest configuration and shared fixtures.
"""

import asyncio
import importlib.util
import inspect
import os
import socket
import sys
from collections.abc import Iterator
from functools import wraps
from pathlib import Path

import filelock
import pytest
import pytest_asyncio
from dotenv import load_dotenv

# Load the stdlib-only guard by path so --confcutdir/direct API runs are safe too.
_guard_spec = importlib.util.spec_from_file_location(
    "hindsight_test_db_guard", Path(__file__).resolve().parents[2] / "scripts/ci/test_db_guard.py"
)
assert _guard_spec is not None and _guard_spec.loader is not None
_db_guard = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(_db_guard)


def _check_test_database_safety(url: str | None = None) -> None:
    # Runtime refusals must be ordinary test/fixture errors. pytest.exit is a
    # BaseException that tears down xdist workers instead of reporting the cause.
    _db_guard.check_test_database_environment()
    _db_guard.assert_safe_database_url(url)


def _check_test_session_database_safety(url: str | None = None) -> None:
    try:
        _check_test_database_safety(url)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None


_check_test_session_database_safety()

# Force torch to initialize exactly once, in the main thread, at conftest import
# time — before any fixture spins up an event loop or sentence-transformers'
# thread pools. torch's C-level `_add_docstr(_has_torch_function, ...)` in
# torch/overrides.py is not re-entrancy-safe: when the first `import torch`
# happens lazily from inside concurrent/async code (e.g.
# embeddings.initialize() -> sentence_transformers -> transformers -> torch, or
# cross_encoder's ThreadPoolExecutor), torch/overrides.py can execute twice and
# raise "RuntimeError: function '_has_torch_function' already has a docstring",
# failing collection of every test on the pytest-xdist shard. Importing it here
# (single-threaded, before any concurrency) makes that registration happen once
# per worker process. Guarded so slim/no-torch environments still collect.
try:
    import sentence_transformers  # noqa: F401
    import torch  # noqa: F401  # eager one-time init; see comment above

    # Same class of problem, different torch module. transformers' lazy loader
    # imports `torch._inductor.test_operators` while resolving classes such as
    # AutoModelForSequenceClassification / GenerationMixin (exercised by the
    # cross-encoder / reranker tests). That module registers an `_inductor_test`
    # TORCH_LIBRARY namespace at module-body level, and under pytest-xdist its
    # body can execute twice, raising "Only a single TORCH_LIBRARY can be used
    # to register the namespace _inductor_test". The failure surfaces on
    # whichever shard runs the reranker tests, masked by transformers as a
    # misleading "sentence-transformers is required for LocalSTEmbeddings"
    # ImportError. Seed it once here so the later lazy import is a sys.modules
    # cache hit and the body never re-executes.
    import torch._inductor.test_operators  # noqa: F401  # see comment above

    # Seed the rest of the native embedding/reranker stack the same way, and for
    # the same reason. transformers and safetensors/tokenizers ship PyO3/Rust
    # and C extensions whose module bodies are not safe to execute twice
    # (safetensors raises "PyO3 modules ... may only be initialized once per
    # interpreter process"). When these are first imported lazily from inside a
    # fixture's event loop / sentence-transformers' thread pools, or re-executed
    # by transformers' lazy-loader retry path, the second init aborts and — like
    # the torch cases above — is re-raised as a misleading
    # "sentence-transformers is required" ImportError on the reranker shard.
    # Importing the whole chain here (single-threaded, at collection time) puts
    # every submodule in sys.modules so later imports are cache hits.
    import transformers  # noqa: F401  # seeds safetensors/tokenizers once
except ImportError:
    pass

from hindsight_api import LLMConfig, LocalSTEmbeddings, MemoryEngine, RequestContext
from hindsight_api.engine.cross_encoder import LocalSTCrossEncoder
from hindsight_api.engine.query_analyzer import DateparserQueryAnalyzer
from hindsight_api.engine.task_backend import SyncTaskBackend
from hindsight_api.pg0 import EmbeddedPostgres
from hindsight_api.tracing import unregister_span_recorder


async def _teardown_memory_engine(mem: MemoryEngine) -> None:
    """Tear down a test MemoryEngine, guaranteeing its span recorder is unregistered.

    LLM-trace recorders live in a process-global registry; ``MemoryEngine.close()`` is
    the only thing that removes the engine's recorder from it. If close() is skipped
    (pool already closing) or raises before that step, the recorder leaks and a later
    test's LLM calls get recorded into the shared DB — the flaky
    test_llm_trace::test_disabled_writes_no_rows (#2229). Unregister unconditionally;
    it's a no-op when close() already did it.
    """
    try:
        if mem._pool and not mem._pool._closing:
            await mem.close()
    except Exception:
        pass
    finally:
        unregister_span_recorder(mem._llm_recorder)


@pytest_asyncio.fixture
async def _close_aiohttp_sessions():
    """Close the aiohttp sessions a test's clients opened, on the test's own loop.

    Providers open a session per loop lazily and have no close hook, so without this
    each async test's loop ends with open sessions and aiohttp logs "Unclosed client
    session" for every one of them.
    """
    from hindsight_api.engine.aiohttp_session import close_loop_sessions

    yield
    await close_loop_sessions()


def pytest_collection_modifyitems(config, items):
    # Only async tests: an async autouse fixture would give every sync test a loop too.
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "obj", None)):
            item.fixturenames.append("_close_aiohttp_sessions")


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Let a test's ``monkeypatch.setenv`` actually reach the code under test.

    ``HindsightConfig`` is built once and cached for the process, and every
    ``HINDSIGHT_API_*`` value is now read off it rather than from ``os.environ`` at
    the point of use. Without this, a test that sets an environment variable and
    then calls the code would be read against whatever config the *first* test in
    this xdist worker happened to build — the value would silently not apply, and
    which tests noticed would depend on file ordering.

    Clearing on the way out as well keeps a config built from one test's patched
    environment from outliving it.
    """
    from hindsight_api.config import clear_config_cache

    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture(autouse=True)
def _cleanup_leaked_span_recorders():
    """Fail-safe for the process-global LLM-trace recorder registry (#2229).

    ``MemoryEngine.__init__`` registers its recorder in the shared registry, and
    only ``close()`` removes it. Tests that construct an engine directly (without
    ``_teardown_memory_engine``/``close()``) leak an *enabled* recorder; a later
    test's LLM calls then get recorded into the shared DB, flaking
    ``test_llm_trace::test_disabled_writes_no_rows`` (it observes rows for its
    bank even though its own recorder is disabled). ``_teardown_memory_engine``
    guards the fixtures; this guards everything else by dropping any recorder a
    test added to the registry.
    """
    from hindsight_api.tracing import get_span_recorder

    recorders = get_span_recorder()._recorders
    # Strong references compared by identity, not a set of id()s. An id is only
    # unique while its object is alive: a recorder registered and dropped during
    # the test could be collected, and CPython would hand the same address to the
    # *next* recorder — which then matched `before` and was left in the registry.
    # That is how #2229 kept flaking after the first fix, as a leaked enabled
    # recorder writing rows for a later test's bank. Holding the objects also
    # keeps them alive, so no address can be recycled underneath the comparison.
    before = list(recorders)
    yield
    for recorder in list(recorders):
        if not any(recorder is known for known in before):
            recorders.remove(recorder)


@pytest.fixture(autouse=True)
def _restore_global_metrics_collector():
    """Fail-safe for the process-global metrics collector (#3780).

    ``create_metrics_collector()`` swaps the module-global collector in
    ``hindsight_api.metrics`` for a real ``MetricsCollector``. The API lifespan
    now restores it on shutdown, but a test that starts the app and never runs
    shutdown (or calls ``create_metrics_collector()`` itself) still leaves the
    real collector installed for every test that follows in the same xdist
    worker. ``NoOpMetricsCollector`` ignores its arguments while the real one
    compares them, so provider tests that pass a bare ``MagicMock`` usage object
    then blow up with "'>' not supported between instances of 'MagicMock' and
    'int'" — in whichever files the worker happened to be given, which is why
    the failure count moved every time someone added a test.
    """
    from hindsight_api import metrics as metrics_module

    before = metrics_module.get_metrics_collector()
    yield
    metrics_module.reset_metrics_collector(before)


# Default pg0 instance configuration for tests
DEFAULT_PG0_INSTANCE_NAME = "hindsight-test"
DEFAULT_PG0_PORT = int(os.environ.get("HINDSIGHT_TEST_PG_PORT", "5557"))

# Keep the background MaintenanceLoop from auto-starting during tests. In
# production it sweeps retention and re-schedules consolidation, but its timers
# would race shared-pg0 test data (e.g. delete llm_requests/audit_log rows a test
# just inserted). Disabling the reconcile interval, the mental-model refresh tick
# and llm-trace retention — with audit retention already off by default — leaves
# no job enabled, so the loop never starts. Tests that exercise it call
# MaintenanceLoop methods (_run_reconcile / _run_scheduled_mm_refresh /
# _purge_expired) directly.
#
# Every job added to the loop must be switched off here too: one job left on is
# enough to start the loop for the whole suite, which reintroduces exactly the
# races the others are disabled to avoid.
os.environ.setdefault("HINDSIGHT_API_CONSOLIDATION_RECONCILE_INTERVAL_SECONDS", "0")
os.environ.setdefault("HINDSIGHT_API_MENTAL_MODEL_REFRESH_TICK_SECONDS", "0")
os.environ.setdefault("HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS", "-1")


# Load environment variables from .env at the start of test session
def pytest_configure(config):
    """Load environment variables before running tests."""
    # Reject inherited URLs even if the workspace .env would replace them.
    _check_test_session_database_safety()
    # Look for .env in the workspace root (two levels up from tests dir)
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        # override=True keeps the workspace .env authoritative for the test
        # session, matching the precedence hindsight_api used to apply at import
        # time (removed in #2961 so library imports are side-effect-free).
        load_dotenv(env_file, override=True)
        _check_test_session_database_safety()
    else:
        print(f"Warning: {env_file} not found, tests may fail without proper configuration")

    from hindsight_api import config as api_config

    # Tests constructing engines/configs directly must not reuse the live bare-pg0
    # default. Do not set the API env var: db_url must still choose its pg0 fixture.
    _check_test_session_database_safety(api_config.DEFAULT_DATABASE_URL)
    _install_database_connection_guards(config)


def _install_database_connection_guards(config: pytest.Config) -> None:
    """Catch URLs resolved after startup, including libpq's native socket path."""
    import psycopg2

    from hindsight_api import config as api_config, migrations

    patches = pytest.MonkeyPatch()
    active = True

    def cleanup():
        nonlocal active
        active = False
        patches.undo()
        api_config.clear_config_cache()

    config.add_cleanup(cleanup)
    patches.setattr(api_config, "DEFAULT_DATABASE_URL", f"pg0://{DEFAULT_PG0_INSTANCE_NAME}:{DEFAULT_PG0_PORT}")
    api_config.clear_config_cache()
    original_get_pg0 = EmbeddedPostgres._get_pg0
    original_ensure_running = EmbeddedPostgres.ensure_running
    original_connect = psycopg2.connect
    original_initialize = MemoryEngine.initialize

    @wraps(original_initialize)
    async def safe_initialize(engine):
        # A default/explicit URL can reach Alembic/libpq before asyncpg ever
        # creates a socket. Refuse it before model init or startup migrations.
        _check_test_database_safety(engine.db_url)
        config = api_config.get_config()
        _db_guard.assert_safe_database_url(config.read_database_url, source="read_database_url")
        _db_guard.assert_safe_database_url(config.migration_database_url, source="migration_database_url")
        return await original_initialize(engine)

    def guard_migration(entry):
        signature = inspect.signature(entry)

        @wraps(entry)
        def safe_migration(*args, **kwargs):
            # Isolated migration children do not inherit Python hooks. Validate
            # both endpoints in the parent before native SQL or child dispatch.
            arguments = signature.bind(*args, **kwargs).arguments
            _check_test_database_safety(arguments.get("database_url"))
            _db_guard.assert_safe_database_url(arguments.get("migration_database_url"), source="migration_database_url")
            return entry(*args, **kwargs)

        return safe_migration

    patches.setattr(MemoryEngine, "initialize", safe_initialize)
    for name in (
        "run_migrations",
        "run_migrations_for_schemas",
        "ensure_embedding_dimension",
        "ensure_vector_extension",
        "ensure_text_search_extension",
    ):
        patches.setattr(migrations, name, guard_migration(getattr(migrations, name)))

    def safe_get_pg0(instance):
        # Explicit bare pg0 bypasses DEFAULT_DATABASE_URL. Redirect before pg0
        # looks up the persistent live instance's receipt or checks its health.
        if instance.name == "hindsight" and instance.port is None:
            instance.name = DEFAULT_PG0_INSTANCE_NAME
            instance.port = DEFAULT_PG0_PORT
        elif instance.port is None:
            # Dedicated migration/unit fixtures also construct named pg0 with
            # no port. Never let those fall back to pg0's low-port allocator.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                instance.port = reservation.getsockname()[1]
        if instance.port is not None:
            _check_test_database_safety(f"pg0://test:{instance.port}")
        return original_get_pg0(instance)

    async def safe_ensure_running(instance):
        url = await original_ensure_running(instance)
        _check_test_database_safety(url)
        return url

    def safe_connect(dsn=None, *args, **kwargs):
        # parse_dsn is offline. psycopg2 uses native libpq, so Python's socket
        # audit hook alone does not protect migrations or SQLAlchemy connections.
        params = psycopg2.extensions.parse_dsn(dsn) if dsn else {}
        params.update(kwargs)
        ports = params.get("port") or os.environ.get("PGPORT", "5432")
        _check_test_database_safety(f"postgresql://test/db?port={ports}")
        return original_connect(dsn, *args, **kwargs)

    patches.setattr(EmbeddedPostgres, "_get_pg0", safe_get_pg0)
    patches.setattr(EmbeddedPostgres, "ensure_running", safe_ensure_running)
    patches.setattr(psycopg2, "connect", safe_connect)

    def socket_guard(event, args):
        # asyncpg pools and any late/default URL reach this boundary before the
        # OS connect. Audit hooks are process-wide; disable it when pytest ends.
        if event == "socket.connect" and active:
            address = args[1]
            if isinstance(address, tuple):
                _check_test_database_safety(f"postgresql://test:{address[1]}/db")
            elif isinstance(address, (str, bytes)):
                # Unix-domain sockets have no (host, port) tuple. asyncpg uses
                # <socket directory>/.s.PGSQL.<port>; bytes paths are valid too.
                name = os.fsdecode(address).rsplit("/", 1)[-1]
                if name.startswith(".s.PGSQL."):
                    _check_test_database_safety(f"postgresql://test/db?port={name.removeprefix('.s.PGSQL.')}")

    sys.addaudithook(socket_guard)


@pytest.fixture(scope="session")
def db_url():
    """
    Provide a PostgreSQL connection URL for tests.

    If HINDSIGHT_API_DATABASE_URL is set, use it directly.
    Otherwise, return None to indicate pg0 should be used (managed by pg0_instance fixture).
    """
    url = os.getenv("HINDSIGHT_API_DATABASE_URL")
    _check_test_database_safety(url)
    return url


@pytest.fixture(scope="session")
def pg0_db_url(db_url, tmp_path_factory, worker_id) -> Iterator[str]:
    """
    Session-scoped fixture that ensures pg0 is running, migrations are applied,
    and returns the database URL.

    If HINDSIGHT_API_DATABASE_URL is a plain postgresql:// URL, uses it directly.
    If HINDSIGHT_API_DATABASE_URL is a pg0:// URL, resolves it to a real URL first.
    Serial runs retain the requested instance and port. Each xdist worker instead
    owns a uniquely named pg0 instance on an available port, stopped at teardown.
    Sharing memory_units across the whole offline suite accumulates per-bank HNSW
    indexes and lets one worker's index DDL block every other's retain/recall. That
    made otherwise short append regressions exceed the 300-second test timeout.
    """
    # Also validate explicit fixture overrides before migrations or pg0 startup.
    _check_test_database_safety(db_url)

    from hindsight_api.pg0 import parse_pg0_url as _parse_pg0_url

    # Determine pg0 instance name/port from db_url (if it's a pg0:// URL) or use defaults
    if db_url and not _parse_pg0_url(db_url).is_pg0:
        # Plain postgresql:// URL - use it directly but still run migrations
        from hindsight_api.migrations import run_migrations

        run_migrations(db_url)
        yield db_url
        return

    if db_url:
        _parsed = _parse_pg0_url(db_url)
        pg0_instance_name = _parsed.instance_name or DEFAULT_PG0_INSTANCE_NAME
        pg0_instance_port = _parsed.port or DEFAULT_PG0_PORT
    else:
        pg0_instance_name = DEFAULT_PG0_INSTANCE_NAME
        pg0_instance_port = DEFAULT_PG0_PORT

    # Get shared temp dir for coordination between xdist workers
    if worker_id == "master":
        # Running without xdist (-n 0 or no -n flag)
        root_tmp_dir = tmp_path_factory.getbasetemp()
    else:
        # Running with xdist - use parent dir shared by all workers
        root_tmp_dir = tmp_path_factory.getbasetemp().parent

    owns_instance = worker_id != "master"
    if owns_instance:
        # The pytest session directory also separates concurrent runs/worktrees.
        pg0_instance_name = f"{pg0_instance_name}-{root_tmp_dir.name}-{worker_id}"

    pg0 = None

    # Serial runs can reuse their instance. Worker names also distinguish
    # parallel sessions, so their setup receipts are never shared.
    lock_file = root_tmp_dir / f"pg0_setup_{pg0_instance_name}.lock"
    url_file = root_tmp_dir / f"pg0_url_{pg0_instance_name}.txt"

    # Run migrations - uses PostgreSQL advisory lock internally,
    # so safe to call from multiple workers (only one will actually run migrations)
    from hindsight_api.migrations import run_migrations

    try:
        with filelock.FileLock(str(lock_file)):
            if url_file.exists() and not owns_instance:
                url = url_file.read_text().strip()
            else:
                # Select an OS-assigned ephemeral port and keep selection/start
                # under the same cross-session lock. pg0's implicit allocator
                # walks from 5432 and can pick protected 5436 on hosted workers.
                startup_lock = root_tmp_dir.parent / "hindsight_pg0_start.lock"
                with filelock.FileLock(str(startup_lock)):
                    if owns_instance:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                            reservation.bind(("127.0.0.1", 0))
                            pg0_instance_port = reservation.getsockname()[1]
                    _check_test_database_safety(f"pg0://{pg0_instance_name}:{pg0_instance_port}")
                    pg0 = EmbeddedPostgres(
                        name=pg0_instance_name,
                        port=pg0_instance_port,
                        config={"max_connections": "300"},
                    )
                    loop = asyncio.new_event_loop()
                    try:
                        url = loop.run_until_complete(pg0.ensure_running())
                    finally:
                        loop.close()
                url_file.write_text(url)

        # Reused pg0 receipts and resolved URLs must pass before any DB operation.
        _check_test_database_safety(url)
        run_migrations(url)

        # Serial instances persist between runs. Worker-owned instances are new,
        # but use the same cleanup boundary before any test starts.
        cleanup_lock = root_tmp_dir / f"pg0_cleanup_{pg0_instance_name}.lock"
        cleanup_done = root_tmp_dir / f"pg0_cleanup_{pg0_instance_name}.done"
        with filelock.FileLock(str(cleanup_lock)):
            if not cleanup_done.exists():
                _cleanup_stale_test_data(url)
                cleanup_done.write_text("done")
        yield url
    finally:
        if owns_instance and pg0 is not None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(pg0.stop())
            finally:
                loop.close()


def _cleanup_stale_test_data(db_url: str) -> None:
    """Drop all per-bank vector indexes and test data from previous sessions.

    pg0 persists between test runs, so per-bank HNSW indexes accumulate
    (3 per bank × thousands of test banks = tens of thousands of indexes).
    This eventually causes 'out of shared memory' errors because PostgreSQL
    tracks all indexes in shared lock tables.
    """
    import asyncpg

    async def _do_cleanup():
        conn = await asyncpg.connect(db_url)
        try:
            idx_rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'idx_mu_emb_%'"
            )
            if idx_rows:
                for row in idx_rows:
                    await conn.execute(f'DROP INDEX IF EXISTS public."{row["indexname"]}"')

            # Truncate test data in dependency order
            for table in [
                "entity_cooccurrences",
                "unit_entities",
                "memory_links",
                "entities",
                "memory_units",
                "chunks",
                "documents",
                "mental_models",
                "directives",
                "async_operations",
                "audit_log",
                "webhooks",
                "file_storage",
                "banks",
            ]:
                try:
                    await conn.execute(f"TRUNCATE {table} CASCADE")
                except Exception:
                    pass  # Table may not exist yet
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_do_cleanup())
    finally:
        loop.close()


@pytest.fixture(scope="session")
def _oracle_admin_dsn():
    """
    Parse ORACLE_TEST_DSN into admin connection parameters.

    Accepts either URL format (oracle://user:pass@host:port/service) or
    bare DSN (host:port/service) with separate ORACLE_TEST_USER/PASSWORD env vars.
    Skips the entire test session if ORACLE_TEST_DSN is not set.
    """
    from urllib.parse import urlparse

    dsn = os.getenv("ORACLE_TEST_DSN")
    if not dsn:
        pytest.skip("ORACLE_TEST_DSN not set — skipping Oracle tests")

    parsed = urlparse(dsn)
    if parsed.scheme in ("oracle", "oracle+oracledb"):
        host = parsed.hostname or "localhost"
        port = parsed.port or 1521
        service = parsed.path.lstrip("/") if parsed.path else "FREEPDB1"
        return {
            "user": parsed.username or "SYSTEM",
            "password": parsed.password or "oracle",
            "dsn": f"{host}:{port}/{service}",
        }
    else:
        return {
            "user": os.getenv("ORACLE_TEST_USER", "SYSTEM"),
            "password": os.getenv("ORACLE_TEST_PASSWORD", "oracle"),
            "dsn": dsn,
        }


@pytest.fixture(scope="session")
def oracle_db_url(_oracle_admin_dsn):
    """
    Bootstrap a dedicated Oracle test user with an ASSM tablespace and return
    a connection URL for that user.

    Oracle 23ai requires VECTOR columns to be in an Automatic Segment Space
    Management (ASSM) tablespace. The default SYSTEM tablespace is not ASSM,
    so connecting as SYSTEM directly would cause ORA-43853 during migrations.

    This fixture creates a ``HINDSIGHT_TEST`` user (idempotent) with the USERS
    tablespace (which is ASSM on Oracle Free/XE) and returns a URL that the
    ``oracle_memory`` fixture and ``run_migrations()`` can use directly.
    """
    try:
        import oracledb
    except ImportError:
        pytest.skip("oracledb not installed — skipping Oracle tests")

    oracledb.defaults.fetch_lobs = False

    admin_user = _oracle_admin_dsn["user"]
    admin_pass = _oracle_admin_dsn["password"]
    bare_dsn = _oracle_admin_dsn["dsn"]

    test_user = "HINDSIGHT_TEST"
    test_pass = "hindsight_test"

    conn = oracledb.connect(user=admin_user, password=admin_pass, dsn=bare_dsn)
    cursor = conn.cursor()
    try:
        # Create test user (idempotent — skip if already exists)
        try:
            cursor.execute(
                f'CREATE USER {test_user} IDENTIFIED BY "{test_pass}" DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS'
            )
        except oracledb.DatabaseError as e:
            code = getattr(e.args[0], "code", None)
            if code == 1920:
                # ORA-01920: user name conflicts with another user or role name
                pass
            elif code == 1031:
                # ORA-01031: we are not an admin. CI provisions the user with a
                # privileged account before pytest runs and then points
                # ORACLE_TEST_DSN at that same unprivileged user, so this bootstrap
                # cannot (and need not) create it. Assume it exists — if it does
                # not, run_migrations below fails with a plain login error.
                pass
            else:
                raise

        # Grant required privileges (idempotent)
        for grant in [
            f"GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO {test_user}",
            f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW TO {test_user}",
            f"GRANT CTXAPP TO {test_user}",
        ]:
            try:
                cursor.execute(grant)
            except oracledb.DatabaseError:
                pass

        # Grant UTL_MATCH for fuzzy entity matching (may not be available)
        try:
            cursor.execute(f"GRANT EXECUTE ON UTL_MATCH TO {test_user}")
        except oracledb.DatabaseError:
            pass

        conn.commit()
    finally:
        cursor.close()
        conn.close()

    # Return URL-format DSN for the test user
    url = f"oracle://{test_user}:{test_pass}@{bare_dsn}"

    # Run idempotent migrations once at session scope (mirrors PG's pg0_db_url).
    # This avoids re-running DDL checks on every function-scoped test.
    from hindsight_api.migrations import run_migrations

    run_migrations(url)

    return url


@pytest_asyncio.fixture(scope="function")
async def oracle_memory(oracle_db_url, embeddings, cross_encoder, query_analyzer):
    """
    Provide a MemoryEngine backed by Oracle 23ai for each test.

    Mirrors the PG `memory` fixture but uses the Oracle backend.
    Migrations are run once at session scope in the `oracle_db_url` fixture.
    """
    from hindsight_api.config import clear_config_cache

    # Temporarily set the database backend env var so the global config
    # (used by fq_table / _is_oracle) returns "oracle".
    old_backend = os.environ.get("HINDSIGHT_API_DATABASE_BACKEND")
    os.environ["HINDSIGHT_API_DATABASE_BACKEND"] = "oracle"
    clear_config_cache()

    try:
        mem = MemoryEngine(
            db_url=oracle_db_url,
            # Note: conftest loads ../.env with override=True at session start, so
            # these defaults only apply if no .env file is found. .env is authoritative.
            memory_llm_provider=os.getenv("HINDSIGHT_API_LLM_PROVIDER", "openai"),
            memory_llm_api_key=os.getenv("HINDSIGHT_API_LLM_API_KEY"),
            memory_llm_model=os.getenv("HINDSIGHT_API_LLM_MODEL", "gpt-4o-mini"),
            memory_llm_base_url=os.getenv("HINDSIGHT_API_LLM_BASE_URL") or None,
            embeddings=embeddings,
            cross_encoder=cross_encoder,
            query_analyzer=query_analyzer,
            pool_min_size=1,
            pool_max_size=15,
            run_migrations=False,  # Already ran above
            task_backend=SyncTaskBackend(),
        )
        await mem.initialize()
        yield mem
        await _teardown_memory_engine(mem)
    finally:
        # Restore original env var and clear config cache
        if old_backend is None:
            os.environ.pop("HINDSIGHT_API_DATABASE_BACKEND", None)
        else:
            os.environ["HINDSIGHT_API_DATABASE_BACKEND"] = old_backend
        clear_config_cache()


@pytest.fixture(scope="function")
def request_context():
    """Provide a default RequestContext for tests."""
    return RequestContext()


@pytest.fixture(scope="session")
def llm_config():
    """
    Provide LLM configuration for tests.
    This can be used by tests that need to call LLM directly without memory system.
    """
    return LLMConfig.from_env()


def _skip_without_local_ml(what: str) -> None:
    """Skip rather than error when the local ML stack is not installed.

    The ``local-ml`` extra (sentence-transformers, transformers, torch) is optional: a
    deployment using TEI/OpenAI/Cohere for embeddings and reranking never installs it.

    Without this, every DB-backed test collapses into an ImportError from deep inside
    fixture setup ("sentence-transformers is required for LocalSTEmbeddings"), which
    reads as 1495 broken tests rather than one absent optional dependency.
    """
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip(
            f"local ML stack not installed; {what} fixture needs the 'local-ml' extra "
            "(pip install 'hindsight-api-slim[local-ml]')",
            allow_module_level=False,
        )


@pytest.fixture(scope="session")
def embeddings(tmp_path_factory, worker_id):
    """
    Session-scoped embeddings fixture with filelock to prevent race conditions.

    When pytest-xdist runs multiple workers in parallel, they all try to load
    models from the HuggingFace cache simultaneously, which can cause race
    conditions and meta tensor errors. We use a filelock to serialize model
    initialization across workers.
    """
    # Get shared temp dir for coordination between xdist workers
    if worker_id == "master":
        root_tmp_dir = tmp_path_factory.getbasetemp()
    else:
        root_tmp_dir = tmp_path_factory.getbasetemp().parent

    lock_file = root_tmp_dir / "embeddings_init.lock"

    _skip_without_local_ml("embeddings")
    emb = LocalSTEmbeddings()

    # Serialize model initialization across workers
    with filelock.FileLock(str(lock_file)):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(emb.initialize())
        finally:
            loop.close()

    return emb


@pytest.fixture(scope="session")
def cross_encoder(tmp_path_factory, worker_id):
    """
    Session-scoped cross-encoder fixture with filelock to prevent race conditions.

    When pytest-xdist runs multiple workers in parallel, they all try to load
    models from the HuggingFace cache simultaneously, which can cause race
    conditions and meta tensor errors. We use a filelock to serialize model
    initialization across workers.
    """
    # Get shared temp dir for coordination between xdist workers
    if worker_id == "master":
        root_tmp_dir = tmp_path_factory.getbasetemp()
    else:
        root_tmp_dir = tmp_path_factory.getbasetemp().parent

    lock_file = root_tmp_dir / "cross_encoder_init.lock"

    _skip_without_local_ml("cross_encoder")
    ce = LocalSTCrossEncoder()

    # Serialize model initialization across workers
    with filelock.FileLock(str(lock_file)):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(ce.initialize())
        finally:
            loop.close()

    return ce


@pytest.fixture(scope="session")
def query_analyzer():
    return DateparserQueryAnalyzer()


@pytest_asyncio.fixture(scope="function")
async def memory(pg0_db_url, embeddings, cross_encoder, query_analyzer):
    """
    Provide a MemoryEngine instance using a mock LLM for deterministic tests.

    The mock LLM returns canned facts derived from input text, allowing the
    full retain → recall → reflect pipeline to work without real LLM calls.
    This makes core tests fast, deterministic, and free from LLM flakiness.

    Tests that need real LLM output quality should use `memory_real_llm` instead.
    """
    mem = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider="mock",
        memory_llm_api_key="",
        memory_llm_model="mock",
        embeddings=embeddings,
        cross_encoder=cross_encoder,
        query_analyzer=query_analyzer,
        pool_min_size=1,
        pool_max_size=15,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
    )
    await mem.initialize()
    yield mem
    await _teardown_memory_engine(mem)


@pytest_asyncio.fixture(scope="function")
async def memory_real_llm(pg0_db_url, embeddings, cross_encoder, query_analyzer):
    """
    Provide a MemoryEngine instance using a real LLM provider.

    Use this fixture ONLY for tests that assert on LLM output quality
    (fact extraction accuracy, language preservation, consolidation decisions, etc.).
    These tests are non-deterministic and should be marked with @pytest.mark.hs_llm_core
    (or @pytest.mark.hs_llm_mat for provider matrix acceptance tests).
    """
    mem = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider=os.getenv("HINDSIGHT_API_LLM_PROVIDER", "groq"),
        memory_llm_api_key=os.getenv("HINDSIGHT_API_LLM_API_KEY"),
        memory_llm_model=os.getenv("HINDSIGHT_API_LLM_MODEL", "openai/gpt-oss-120b"),
        memory_llm_base_url=os.getenv("HINDSIGHT_API_LLM_BASE_URL") or None,
        embeddings=embeddings,
        cross_encoder=cross_encoder,
        query_analyzer=query_analyzer,
        pool_min_size=1,
        pool_max_size=15,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
    )
    await mem.initialize()
    yield mem
    await _teardown_memory_engine(mem)


@pytest_asyncio.fixture(scope="function")
async def memory_no_llm_verify(pg0_db_url, embeddings, cross_encoder, query_analyzer):
    """
    Provide a MemoryEngine instance that skips LLM connection verification.

    This fixture is useful for tests that override the LLM configuration
    after initialization (e.g., to test specific providers).
    """
    mem = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider="mock",  # Use mock provider as placeholder
        memory_llm_api_key="",
        memory_llm_model="mock",
        embeddings=embeddings,
        cross_encoder=cross_encoder,
        query_analyzer=query_analyzer,
        pool_min_size=1,
        pool_max_size=15,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
        skip_llm_verification=True,  # Skip verification - will be overridden by test
    )
    await mem.initialize()
    yield mem
    await _teardown_memory_engine(mem)


@pytest_asyncio.fixture
async def api_client(memory):
    """General-purpose HTTP test client over the `memory` fixture's app.

    Use for any integration test that exercises the FastAPI surface without
    needing audit-logging side effects. See `audit_api_client` for the
    audit-enabled variant.
    """
    import httpx

    from hindsight_api.api import create_app

    app = create_app(memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def stub_refresh_has_sources(monkeypatch, memory) -> None:
    """Tell a mental-model refresh that its bank holds something to read.

    A refresh whose scope is empty skips the reflect loop outright (#3875): running
    the agent over nothing is its worst case, not a cheap one. Tests that stub
    ``reflect_async`` almost always do so on a bank with no memories, where that
    short-circuit would pre-empt the stub instead of the test exercising it — so any
    test that fakes retrieval has to say the bank is not empty. Tests that are about
    the short-circuit itself let the real check run (``TestRefreshSkipsEmptyScope``).

    Answered on the sibling-documents leg, which is the one that runs when no memory
    is in scope: that is the state these tests are in, and it needs no fake timestamps
    to line up against a delta window.
    """

    async def _has_document(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(memory, "_bank_has_readable_document", _has_document)


def enable_audit_default(memory, enabled: bool) -> None:
    """Set the deployment-wide default for the hierarchical ``audit_log_enabled``.

    ``audit_log_enabled`` resolves through env -> tenant -> bank, and the
    ConfigResolver snapshots the global layer at construction time. Tests that
    want "auditing on by default" therefore have to update that snapshot;
    flipping ``AuditLogger._enabled`` alone only covers actions with no bank in
    scope. Per-bank overrides are set with ``resolver.update_bank_config``.
    """
    from dataclasses import replace

    resolver = memory._config_resolver
    resolver._global_config = replace(resolver._global_config, audit_log_enabled=enabled)
