"""Typed, bounded opt-in PostgreSQL curation protocol.

Opaque server revisions keep JSON number serialization out of client CAS. V1 and
ordinary curation retain their existing behavior. Storage lives in memories/pg.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue, SkipJsonSchema
from pydantic_core import to_jsonable_python
from typing_extensions import TypeAliasType

# Internal PostgreSQL snapshots must not route numeric JSONB through binary
# floats (or Pydantic's JSON mode, which turns Decimal into quoted strings).
LosslessJsonValue = TypeAliasType(
    "LosslessJsonValue",
    "dict[str, LosslessJsonValue] | list[LosslessJsonValue] | str | int | float | Decimal | bool | None",
)

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BatchId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
MAX_BYTES = 8 * 1024 * 1024
MAX_OBSERVATIONS = 200
MAX_PEERS = 500
MAX_ENTITIES = 2000
MAX_LINKS = 4000
MAX_HISTORY = 2000


class CurationBatchConflict(ValueError):
    """No mutation committed. The capsule remains available after a conflict."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurationFactType(str, Enum):
    WORLD = "world"
    EXPERIENCE = "experience"


def _nullable_primitive_types(schema: JsonSchemaValue) -> None:
    # Both forms are valid OpenAPI 3.1. The pinned Python/Go generators cannot
    # handle anyOf(string, null) inside an object with additionalProperties=false.
    # Use a primitive type array, without weakening the strict runtime model.
    for field in schema["properties"].values():
        branches = field.get("anyOf")
        if branches and len(branches) == 2 and branches[1] == {"type": "null"} and branches[0].get("type") == "string":
            field.pop("anyOf")
            field.update(branches[0])
            field["type"] = ["string", "null"]
            # Compatibility annotation for the pinned 3.0-era generators.
            # The type array remains the actual 3.1 validation contract.
            field["nullable"] = True


class CurationFields(StrictModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_nullable_primitive_types)

    text: str | SkipJsonSchema[None] = Field(
        default=None, description="Nonblank replacement text, at most 100000 characters"
    )
    context: str | None = Field(
        default=None, description="Replacement context, at most 100000 characters, or null to clear"
    )
    fact_type: CurationFactType | SkipJsonSchema[None] = None
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None

    @model_serializer(mode="wrap")
    def preserve_patch_presence(self, handler: SerializerFunctionWrapHandler) -> dict[str, JsonValue | datetime]:
        # Presence is correction intent: omission leaves a field unchanged,
        # while explicit null clears it. Preserve that distinction in nested
        # manifest hashes and durable capsules, not only the HTTP request.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}

    @field_validator("occurred_start", "occurred_end")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Occurrence dates must have an explicit timezone")
        return value

    @model_validator(mode="after")
    def nonempty(self) -> CurationFields:
        if not self.model_fields_set:
            raise ValueError("A correction must include at least one field")
        if "text" in self.model_fields_set and (self.text is None or not self.text.strip()):
            raise ValueError("Correction text must be nonblank")
        if (
            self.text is not None
            and len(self.text) > 100_000
            or self.context is not None
            and len(self.context) > 100_000
        ):
            raise ValueError("Correction text/context exceeds 100000 characters")
        if "fact_type" in self.model_fields_set and self.fact_type is None:
            raise ValueError("fact_type cannot be null")
        if (
            self.occurred_start is not None
            and self.occurred_end is not None
            and self.occurred_start > self.occurred_end
        ):
            raise ValueError("Occurrence start must not follow end")
        return self


class CurationTargetRevision(StrictModel):
    memory_id: UUID
    memory_revision: Hash
    source_revision: Hash


class CurationChange(CurationTargetRevision):
    action: Literal["invalidate", "correct"]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]
    fields: CurationFields | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def action_fields(self) -> CurationChange:
        if (self.action == "correct") != (self.fields is not None):
            raise ValueError("Only a correction requires fields")
        return self


class CurationPreviewRequest(StrictModel):
    protocol: Literal["raw-curation-v2"]
    memory_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)]

    @field_validator("memory_ids")
    @classmethod
    def unique_ids(cls, values: list[UUID]) -> list[UUID]:
        if len(set(values)) != len(values):
            raise ValueError("Duplicate targets")
        return values


class CurationApplyRequest(StrictModel):
    protocol: Literal["raw-curation-v2"]
    expected_closure_revision: Hash
    changes: Annotated[list[CurationChange], Field(min_length=1, max_length=50)]

    @field_validator("changes")
    @classmethod
    def unique_changes(cls, values: list[CurationChange]) -> list[CurationChange]:
        if len({v.memory_id for v in values}) != len(values):
            raise ValueError("Duplicate targets")
        return values


class CurationRevertRequest(StrictModel):
    protocol: Literal["raw-curation-v2"]
    expected_receipt_revision: Hash


class CurationInventory(StrictModel):
    targets: int
    observations: int
    peers: int
    entities: int
    links: int
    history_rows: int
    snapshot_bytes: int
    source_bytes: int


class CurationPreview(StrictModel):
    protocol: Literal["raw-curation-v2"] = "raw-curation-v2"
    closure_revision: Hash
    targets: list[CurationTargetRevision]
    inventory: CurationInventory


class MaintenanceDebt(StrictModel):
    consolidation: bool = True
    graph: bool = True
    model_refresh: bool = True
    memory_ids: list[UUID]


class CurationReceipt(StrictModel):
    protocol: Literal["raw-curation-v2"] = "raw-curation-v2"
    bank_id: str
    batch_id: BatchId
    manifest_revision: Hash
    receipt_revision: Hash
    status: Literal["applied", "reverted"]
    inventory: CurationInventory
    maintenance_debt: MaintenanceDebt


class ColumnSnapshot(StrictModel):
    name: str
    type: str
    generated: bool


class TableSnapshot(StrictModel):
    # Keys come from the live catalog, not a hand-maintained schema. The schema
    # fingerprint gates reconstruction, including new columns and vector dims.
    columns: list[ColumnSnapshot]
    rows: list[dict[str, LosslessJsonValue]]


class ClosureScope(StrictModel):
    targets: list[UUID]
    affected: list[UUID]
    peers: list[UUID]
    entities: list[UUID]


class CurationSnapshot(StrictModel):
    scope: ClosureScope
    schema_revision: str
    memories: TableSnapshot
    archives: TableSnapshot
    postings: TableSnapshot
    links: TableSnapshot
    entities: TableSnapshot
    cooccurrences: TableSnapshot
    history: TableSnapshot
    source_revisions: dict[str, str]
    source_bytes: int
    dependencies: list[dict[str, LosslessJsonValue]]


class PreparedCorrection(StrictModel):
    memory_id: UUID
    text: str
    context: str | None
    fact_type: CurationFactType
    occurred_start: datetime | None
    occurred_end: datetime | None
    event_date: datetime | None
    embedding: str


class SourceRevision(StrictModel):
    revision: Hash
    document_id: str
    chunk_id: str
    document_bytes: int
    chunk_bytes: int


class BatchCapsule(StrictModel):
    manifest: CurationApplyRequest
    before: CurationSnapshot
    after: CurationSnapshot
    receipt: CurationReceipt
    applied_receipt_revision: Hash


def canonical_bytes(value: BaseModel | LosslessJsonValue | list[dict[str, LosslessJsonValue]]) -> bytes:
    def encode(item: Any) -> str:
        if isinstance(item, BaseModel):
            return encode(item.model_dump(mode="python"))
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Nonfinite JSON number")
            # Decimal.__str__ is exact and independent of the decimal context.
            # Emit a JSON number, not a string, without a float conversion.
            return str(item)
        if isinstance(item, dict):
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in sorted(item)) + "}"
        if isinstance(item, list):
            return "[" + ",".join(encode(element) for element in item) + "]"
        return json.dumps(to_jsonable_python(item), ensure_ascii=False, allow_nan=False)

    return encode(value).encode()


def revision(value: BaseModel | LosslessJsonValue) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def snapshot_revision(snapshot: CurationSnapshot) -> str:
    value = snapshot.model_dump(mode="python")
    # Shared identities may gain unrelated references while a batch is applied.
    # Undo owns only its posting delta, never these other writers' counters.
    for entity in value["entities"]["rows"]:
        for key in ("mention_count", "first_seen", "last_seen"):
            entity.pop(key, None)
    for pair in value["cooccurrences"]["rows"]:
        for key in ("cooccurrence_count", "last_cooccurred"):
            pair.pop(key, None)
    return revision(value)
