"""Import batch contract (application service contract V0.2 §2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from capacity_credit.contracts.enums import (
    DataVersionStatus,
    ImportReviewStatus,
    ValidationStatus,
)
from capacity_credit.contracts.grades import GradeEvidence

_SHA256_PATTERN = r"[0-9a-fA-F]{64}"


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("must be timezone-aware ISO 8601")
    return v


class ImportBatch(BaseModel):
    """Import batch record with review and data-version state."""

    model_config = ConfigDict(extra="forbid")

    import_schema_version: Literal["1.0"]
    import_batch_id: uuid.UUID
    dataset_id: str
    data_version_id: str
    data_version_hash: str = Field(pattern=_SHA256_PATTERN)
    data_version_status: DataVersionStatus
    validation_status: ValidationStatus
    import_review_status: ImportReviewStatus
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None
    approved_data_version_id: str | None = None
    superseded_by_import_batch_id: uuid.UUID | None = None
    created_by: str
    created_at: datetime
    grades: GradeEvidence

    @field_validator("created_at", "reviewed_at")
    @classmethod
    def _timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return _ensure_aware(v)

    @model_validator(mode="after")
    def _validate_approval(self) -> Self:
        if self.import_review_status is ImportReviewStatus.APPROVED:
            if (
                self.reviewed_by is None
                or self.reviewed_at is None
                or self.review_comment is None
                or self.approved_data_version_id is None
            ):
                raise ValueError(
                    "approved import review requires reviewed_by, reviewed_at, "
                    "review_comment and approved_data_version_id"
                )
            if self.approved_data_version_id != self.data_version_id:
                raise ValueError(
                    "approved_data_version_id must equal data_version_id"
                )
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> ImportBatch:
        return cls.model_validate_json(text)
