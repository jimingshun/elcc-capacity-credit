"""Dataset manifest contract (data contract V0.3 §3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from capacity_credit.contracts.enums import (
    Calendar,
    DataGrade,
    IntervalLabel,
    LoadBasis,
    SystemBoundary,
    TimeResolution,
)


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("must be timezone-aware ISO 8601")
    return v


class DatasetManifest(BaseModel):
    """Standard dataset manifest (``dataset_manifest.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0-draft.3"]
    project_id: str
    dataset_id: str
    project_name: str
    region_ids: list[str] = Field(min_length=1)
    system_boundary: SystemBoundary
    timezone: str
    time_resolution: TimeResolution
    interval_label: IntervalLabel
    calendar: Calendar
    weather_years: list[int]
    declared_data_grade: DataGrade
    power_unit: Literal["MW"]
    energy_unit: Literal["MWh"]
    load_basis: LoadBasis
    created_at: datetime
    created_by: str
    source_ledger_file: str
    tables: dict[str, str]

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @field_validator("region_ids")
    @classmethod
    def _regions_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("region_ids must be unique")
        return v

    @field_validator("weather_years")
    @classmethod
    def _weather_years_unique(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("weather_years must be unique")
        return v

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )

    @classmethod
    def from_yaml(cls, text: str) -> DatasetManifest:
        import yaml

        return cls.model_validate(yaml.safe_load(text))

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> DatasetManifest:
        return cls.model_validate_json(text)
