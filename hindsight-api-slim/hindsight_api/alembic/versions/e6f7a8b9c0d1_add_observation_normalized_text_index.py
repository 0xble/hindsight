"""Index normalized observation text for exact consolidation reconciliation.

Revision ID: e6f7a8b9c0d1
Revises: a7c2e9f41b60
Create Date: 2026-09-28

The serialized lane apply path compares prepared CREATE texts with committed
observations using the same whitespace normalization as the Python guard. A
fixed-size hash expression index keeps that probe selective even when
observation text exceeds the btree tuple limit. The reader confirms normalized
text after the hash lookup to guard against collisions.
"""

from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import text

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "a7c2e9f41b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_INDEX_NAME = "idx_memory_units_observation_norm_text"
_INDEX_NAME = "idx_memory_units_observation_norm_text_md5"
_NORM_EXPR = (
    "btrim(regexp_replace(text, "
    "E'[\\\\x09-\\\\x0d\\\\x1c-\\\\x20\\\\x85\\\\xa0\\\\x1680"
    "\\\\x2000-\\\\x200a\\\\x2028\\\\x2029\\\\x202f\\\\x205f\\\\x3000]+', ' ', 'g'))"
)


def _get_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    schema = _get_schema_prefix()
    bind = op.get_bind()
    target_schema = context.config.get_main_option("target_schema") or None
    # The largest table can be written throughout the build. Recover an invalid
    # index left by an interrupted concurrent attempt before IF NOT EXISTS.
    with op.get_context().autocommit_block():
        # Only clean up the old index name in pre-release test databases.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}{_OLD_INDEX_NAME}")
        leftover_invalid = bind.execute(
            text(
                "SELECT NOT i.indisvalid FROM pg_class c "
                "JOIN pg_index i ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = :index_name "
                "AND n.nspname = COALESCE(:target_schema, current_schema())"
            ),
            {"index_name": _INDEX_NAME, "target_schema": target_schema},
        ).scalar()
        if leftover_invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}{_INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON {schema}memory_units "
            f"(bank_id, md5({_NORM_EXPR})) WHERE fact_type = 'observation'"
        )


def _pg_downgrade() -> None:
    schema = _get_schema_prefix()
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}{_INDEX_NAME}")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade)
