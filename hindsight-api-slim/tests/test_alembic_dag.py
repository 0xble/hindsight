"""Graph-level sanity checks for the Alembic migration DAG.

These tests do not touch a database; they only parse the revision files on
disk, so they are cheap to run in CI and catch DAG accidents (divergent
heads, unreachable revisions) at merge time instead of at deploy time.
"""

import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    script_location = Path(__file__).parent.parent / "hindsight_api" / "alembic"
    cfg.set_main_option("script_location", str(script_location))
    return ScriptDirectory.from_config(cfg)


def test_one_upstream_head_plus_designated_fork_index_head() -> None:
    """Reject accidental heads while preserving the fork index's separate lineage.

    An unexpected head means a branch was added without a merge revision, which
    makes ``alembic upgrade head`` (singular) ambiguous and forces the next
    migration author to orphan whichever head they don't pick as parent.
    v0.5.3 shipped in exactly that state; this test would have caught it.

    The maintained fork's normalized-observation index has an intentional second
    head. The runtime upgrades plural ``heads``, and keeping this revision lineage
    preserves compatibility with already-installed databases. Apart from that
    designated fork head, upstream must still have exactly one head.
    """
    script = _script_directory()
    heads = script.get_heads()
    assert "e6f7a8b9c0d1" in heads, "the fork normalized-observation index head must remain in the migration DAG"
    upstream_heads = set(heads) - {"e6f7a8b9c0d1"}
    assert len(upstream_heads) == 1, (
        f"Alembic has unexpected heads ({heads}); expected one upstream head "
        "plus the designated fork normalized-observation index head."
    )


def test_single_base() -> None:
    """The DAG must have exactly one base (the initial schema).

    Multiple bases mean disconnected migration trees, which can only happen
    through manual file edits.
    """
    script = _script_directory()
    bases = script.get_bases()
    assert len(bases) == 1, f"Alembic has {len(bases)} bases ({bases}); expected exactly 1."


@pytest.mark.parametrize("target_schema", [None, "offline_tenant"])
def test_normalized_observation_index_emits_offline_sql(target_schema: str | None) -> None:
    """Render the actual fork revision without a connection or catalog reads."""
    script = _script_directory()
    cfg = Config()
    if target_schema:
        cfg.set_main_option("target_schema", target_schema)
    output = io.StringIO()
    migration = script.get_revision("e6f7a8b9c0d1").module
    with EnvironmentContext(cfg, script, as_sql=True, output_buffer=output) as env:
        env.configure(dialect_name="postgresql")
        with env.begin_transaction(), Operations.context(env.get_context()):
            migration.upgrade()
            migration.downgrade()
    sql = output.getvalue()
    prefix = f'"{target_schema}".' if target_schema else ""
    assert f"ON {prefix}memory_units" in sql
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_units_observation_norm_text_md5" in sql
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {prefix}idx_memory_units_observation_norm_text_md5" in sql
    assert "SELECT NOT i.indisvalid" not in sql
