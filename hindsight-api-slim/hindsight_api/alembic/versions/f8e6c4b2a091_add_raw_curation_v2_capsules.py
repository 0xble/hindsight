"""PostgreSQL-only durable bounded curation capsules and indexed identity pins.

Revision ID: f8e6c4b2a091
Revises: e6f7a8b9c0d1
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "f8e6c4b2a091"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    schema = _pg_schema_prefix()
    op.execute(f"CREATE UNIQUE INDEX idx_entities_bank_id_id ON {schema}entities(bank_id,id)")
    op.execute(f"""CREATE TABLE {schema}curation_batches (
        bank_id text NOT NULL REFERENCES {schema}banks(bank_id) ON DELETE CASCADE,
        batch_id text NOT NULL,
        manifest_revision text NOT NULL,
        status text NOT NULL CHECK(status IN ('applied','reverted')),
        capsule jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(bank_id,batch_id),
        CHECK (octet_length(capsule::text) <= 33554432)
    )""")
    op.execute(f"""CREATE TABLE {schema}curation_entity_pins (
        bank_id text NOT NULL,
        batch_id text NOT NULL,
        entity_id uuid NOT NULL,
        PRIMARY KEY(bank_id,batch_id,entity_id),
        FOREIGN KEY(bank_id,batch_id) REFERENCES {schema}curation_batches(bank_id,batch_id) ON DELETE CASCADE,
        FOREIGN KEY(bank_id,entity_id) REFERENCES {schema}entities(bank_id,id) ON DELETE RESTRICT
    )""")
    op.execute(f"CREATE INDEX idx_curation_entity_pins_entity ON {schema}curation_entity_pins(entity_id)")
    op.execute(f"CREATE INDEX idx_curation_batches_active ON {schema}curation_batches(bank_id) WHERE status='applied'")


def _pg_downgrade() -> None:
    schema = _pg_schema_prefix()
    # A downgrade must not discard active recovery evidence.
    op.execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM {schema}curation_batches WHERE status='applied') THEN
        RAISE EXCEPTION 'Active curation capsules prevent downgrade'; END IF; END $$""")
    op.execute(f"DROP TABLE {schema}curation_entity_pins")
    op.execute(f"DROP TABLE {schema}curation_batches")
    op.execute(f"DROP INDEX {schema}idx_entities_bank_id_id")


def upgrade() -> None:
    # V2 is deliberately unsupported on Oracle; no unused Oracle tables.
    run_for_dialect(pg=_pg_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade)
