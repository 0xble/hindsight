"""Stable typed details exposed by async operation status responses."""

from typing import Literal

from pydantic import BaseModel, Field

from .parsers.ocr_quality import OcrQualityReason


class FileConvertRetainOperationDetails(BaseModel):
    """A deterministic terminal outcome from a file conversion operation."""

    operation_type: Literal["file_convert_retain"] = Field(
        default="file_convert_retain",
        description="Discriminator: which operation type this detail describes.",
    )
    failure_class: Literal["low_quality_ocr"] = Field(
        description="Stable failure class callers may use to decide whether the source artifact is retryable.",
    )
    failure_reason: OcrQualityReason = Field(
        description="The OCR quality gate that rejected the converted image text.",
    )
