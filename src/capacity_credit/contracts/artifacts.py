"""Artifact manifest contract (application service contract V0.2 §7).

T19 round-2 version boundary (D):

- ``artifact-manifest-1.0`` keeps its ORIGINAL semantics: ``result_id`` is
  REQUIRED and non-null; the historical ArtifactType set is unchanged and
  historical 1.0 results read exactly as before.
- ``artifact-manifest-1.1`` is the VERSIONED coordination manifest used by
  portfolio / sensitivity coordination runs: it adds machine-distinguishable
  fields ``manifest_kind`` (result | coordination) and ``run_type``.
  A coordination manifest requires run_type=portfolio|sensitivity,
  result_id=null, at least one ``analysis_summary`` artifact and no
  json_result entry; a result manifest requires result_id non-null and
  exactly one json_result entry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capacity_credit.contracts.enums import (
    ArtifactType,
    ConfidentialityLevel,
    RedactionStatus,
)

_SHA256_PATTERN = r"[0-9a-fA-F]{64}"


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("must be timezone-aware ISO 8601")
    return v


class ArtifactEntry(BaseModel):
    """Single artifact record inside the manifest."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: ArtifactType
    relative_path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    media_type: str
    producer_service: str
    schema_or_template_version: str


# The HISTORICAL artifact-manifest 1.0 type set (application service
# contract V0.2 §7.1). T19 added ``analysis_summary`` for coordination
# manifests; it is deliberately NOT a valid 1.0 type. Version-level
# validation lives in the 1.0 model AND the generated 1.0 JSON Schema so
# a global enum extension can never silently pollute the historical
# schema.
ARTIFACT_TYPES_1_0 = frozenset(
    {
        "json_result",
        "parquet_result",
        "excel_report",
        "html_report",
        "figure",
        "verify_bundle",
        "rerun_bundle",
        "search_trace",
    }
)


class ArtifactManifest(BaseModel):
    """artifact-manifest 1.0: result-bound manifest (application service
    contract V0.2 §7.1). ``result_id`` is REQUIRED and non-null, and only
    the historical 1.0 ArtifactEntry types are allowed (analysis_summary
    and future coordination types are rejected at the model level)."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: Literal["1.0"]
    artifact_manifest_id: uuid.UUID
    run_id: uuid.UUID
    source_run_id: uuid.UUID | None = None
    result_id: uuid.UUID
    # Q-D1：报告映射契约升 1.1（risk_events 增
    # duration_hours）；工件清单如实携带源映射版本（1.0/1.1 均合法）
    report_mapping_version: Literal["1.0", "1.1"]
    confidentiality_level: ConfidentialityLevel
    redaction_status: RedactionStatus
    generated_by: str
    generated_at: datetime
    artifacts: list[ArtifactEntry] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @model_validator(mode="after")
    def _validate_1_0_artifact_types(self) -> Self:
        for entry in self.artifacts:
            if entry.artifact_type.value not in ARTIFACT_TYPES_1_0:
                raise ValueError(
                    f"artifact-manifest 1.0 must not carry artifact_type "
                    f"{entry.artifact_type.value!r} (only {sorted(ARTIFACT_TYPES_1_0)})"
                )
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> ArtifactManifest:
        return cls.model_validate_json(text)


_COORDINATION_RUN_TYPES = frozenset({"portfolio", "sensitivity"})


class ArtifactManifest11(BaseModel):
    """artifact-manifest 1.1: versioned manifest with an explicit
    ``manifest_kind``. A COORDINATION manifest (portfolio / sensitivity
    run with no MethodResult) carries ``result_id=null`` and only
    ``analysis_summary`` / ``search_trace`` artifacts; a RESULT manifest
    carries ``result_id`` and exactly one json_result entry."""

    model_config = ConfigDict(extra="forbid")

    artifact_schema_version: Literal["1.1"]
    manifest_kind: Literal["result", "coordination"]
    run_type: str
    artifact_manifest_id: uuid.UUID
    run_id: uuid.UUID
    source_run_id: uuid.UUID | None = None
    result_id: uuid.UUID | None = None
    report_mapping_version: Literal["1.0", "1.1"]
    confidentiality_level: ConfidentialityLevel
    redaction_status: RedactionStatus
    generated_by: str
    generated_at: datetime
    artifacts: list[ArtifactEntry] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @model_validator(mode="after")
    def _validate_manifest_kind(self) -> Self:
        json_results = [e for e in self.artifacts if e.artifact_type is ArtifactType.JSON_RESULT]
        analysis_summaries = [
            e for e in self.artifacts if e.artifact_type is ArtifactType.ANALYSIS_SUMMARY
        ]
        if self.manifest_kind == "coordination":
            if self.run_type not in _COORDINATION_RUN_TYPES:
                raise ValueError(
                    "a coordination manifest run_type must be portfolio or sensitivity, "
                    f"got {self.run_type!r}"
                )
            if self.result_id is not None:
                raise ValueError("a coordination manifest must have result_id=null")
            if not analysis_summaries:
                raise ValueError(
                    "a coordination manifest requires at least one analysis_summary artifact"
                )
            if json_results:
                raise ValueError(
                    "a coordination manifest must not carry a json_result (no MethodResult)"
                )
        else:  # result
            if self.result_id is None:
                raise ValueError("a result manifest requires a non-null result_id")
            if len(json_results) != 1:
                raise ValueError(
                    "a result manifest requires exactly one json_result artifact entry"
                )
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> ArtifactManifest11:
        return cls.model_validate_json(text)


def load_artifact_manifest(payload: dict[str, Any]) -> ArtifactManifest | ArtifactManifest11:
    """Explicit version dispatch for artifact manifest documents: 1.0 uses
    the historical result-bound model, 1.1 uses the versioned model. The
    two semantics are never collapsed into one loose model."""
    version = payload.get("artifact_schema_version")
    if version == "1.1":
        return ArtifactManifest11.model_validate(payload)
    return ArtifactManifest.model_validate(payload)


__all__ = [
    "ARTIFACT_TYPES_1_0",
    "ArtifactEntry",
    "ArtifactManifest",
    "ArtifactManifest11",
    "load_artifact_manifest",
]
