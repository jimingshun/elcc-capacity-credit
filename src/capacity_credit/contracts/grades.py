"""Grade evidence contract (T09, review round 4).

The immutable grade-evidence anchor written into every ImportBatch: the
three authoritative fields with exact shape (``extra="forbid"``),
``DataGrade`` enum values (never free strings) and the relation
``effective <= declared <= computed_max`` enforced by the model itself.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from capacity_credit.contracts.enums import DataGrade

_GRADE_ORDER = list(DataGrade)


class GradeEvidence(BaseModel):
    """Exact-shape grade evidence (declared / computed_max / effective)."""

    model_config = ConfigDict(extra="forbid")

    declared_data_grade: DataGrade
    computed_max_data_grade: DataGrade
    effective_data_grade: DataGrade

    @model_validator(mode="after")
    def _validate_relation(self) -> Self:
        declared = _GRADE_ORDER.index(self.declared_data_grade)
        computed_max = _GRADE_ORDER.index(self.computed_max_data_grade)
        effective = _GRADE_ORDER.index(self.effective_data_grade)
        if declared > computed_max:
            raise ValueError(
                "declared_data_grade must not exceed computed_max_data_grade"
            )
        if effective > declared:
            raise ValueError(
                "effective_data_grade must not exceed declared_data_grade"
            )
        return self

    def to_plain(self) -> dict[str, str]:
        """Plain string mapping for staging/frozen manifests."""
        return {
            "declared_data_grade": self.declared_data_grade.value,
            "computed_max_data_grade": self.computed_max_data_grade.value,
            "effective_data_grade": self.effective_data_grade.value,
        }


__all__ = ["GradeEvidence"]
