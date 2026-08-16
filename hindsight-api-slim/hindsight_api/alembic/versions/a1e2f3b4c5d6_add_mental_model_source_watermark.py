"""Add last_refreshed_source_watermark column to mental_models.

``last_refreshed_at`` has carried two unrelated meanings: the wall-clock time of
the most recent refresh AND the source-data watermark (the newest in-scope
memory visible at that refresh). Staleness/"due for refresh" keys off the
watermark, while time-based client schedulers key off the wall-clock time. When
a model's source memories are static, the conflated column never advances even
though refreshes keep rewriting content, so wall-clock schedulers re-refresh
forever.

This splits the two meanings out. ``last_refreshed_source_watermark`` becomes
the dedicated source-data watermark; ``last_refreshed_at`` reverts to a true
wall-clock timestamp. The new column is backfilled from the current
``last_refreshed_at`` for every existing row — because today ``last_refreshed_at``
already holds the watermark, this preserves staleness behaviour across the
migration so nothing mass-refreshes or mass-stops. Nullable so consumers fall
back to ``last_refreshed_at`` (COALESCE) for any row not yet stamped.

Revision ID: a1e2f3b4c5d6
Revises: c4f7a91b2d38
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "a1e2f3b4c5d6"
down_revision: str | Sequence[str] | None = "c4f7a91b2d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    """Schema-qualifier for raw SQL on PG (multi-tenant search_path)."""
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    schema = _pg_schema_prefix()
    op.execute(
        f"""
        ALTER TABLE {schema}mental_models
        ADD COLUMN IF NOT EXISTS last_refreshed_source_watermark TIMESTAMP WITH TIME ZONE
        """
    )
    # Backfill from the current last_refreshed_at: it presently holds the
    # watermark, so copying it preserves each model's staleness decision across
    # the cutover. Only stamp rows still NULL so a re-run is idempotent.
    op.execute(
        f"""
        UPDATE {schema}mental_models
        SET last_refreshed_source_watermark = last_refreshed_at
        WHERE last_refreshed_source_watermark IS NULL
        """
    )


def _pg_downgrade() -> None:
    schema = _pg_schema_prefix()
    op.execute(f"ALTER TABLE {schema}mental_models DROP COLUMN IF EXISTS last_refreshed_source_watermark")


def _oracle_upgrade() -> None:
    # Oracle has no ADD COLUMN IF NOT EXISTS; guard on the data dictionary so a
    # re-run doesn't fail with ORA-01430 (column already exists).
    op.get_bind().exec_driver_sql(
        """
        DECLARE
            n NUMBER;
        BEGIN
            SELECT COUNT(*) INTO n FROM user_tab_columns
            WHERE table_name = 'MENTAL_MODELS'
              AND column_name = 'LAST_REFRESHED_SOURCE_WATERMARK';
            IF n = 0 THEN
                EXECUTE IMMEDIATE
                    'ALTER TABLE mental_models ADD (last_refreshed_source_watermark TIMESTAMP WITH TIME ZONE)';
            END IF;
        END;
        """
    )
    op.get_bind().exec_driver_sql(
        "UPDATE mental_models SET last_refreshed_source_watermark = last_refreshed_at "
        "WHERE last_refreshed_source_watermark IS NULL"
    )


def _oracle_downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE mental_models DROP COLUMN last_refreshed_source_watermark")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
