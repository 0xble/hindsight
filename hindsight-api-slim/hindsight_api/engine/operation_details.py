"""Stable typed details exposed by async operation status responses."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .parsers.ocr_quality import OcrQualityReason


class FileConvertRetainOperationDetails(BaseModel):
    """A deterministic terminal outcome from a file conversion operation."""

    operation_type: Literal["file_convert_retain"] = Field(
        default="file_convert_retain",
        description="Discriminator: which operation type this detail describes.",
    )
    failure_class: Literal["low_quality_ocr", "no_extractable_text"] = Field(
        description="Stable failure class callers may use to decide whether the source artifact is retryable.",
    )
    failure_reason: OcrQualityReason | Literal["empty_content"] = Field(
        description="The OCR rejection reason, or empty_content when every parser extracted no text.",
    )
    parsers: list[str] | None = Field(
        default=None,
        description="Ordered parser chain tried when no extractable text was found.",
    )

    @model_validator(mode="after")
    def validate_failure(self) -> "FileConvertRetainOperationDetails":
        if self.failure_class == "no_extractable_text":
            if self.failure_reason != "empty_content" or not self.parsers:
                raise ValueError("No-text failure requires empty_content and a nonempty parser chain")
        elif self.failure_reason == "empty_content" or self.parsers is not None:
            raise ValueError("OCR failure requires an OCR reason and no parser chain")
        return self
