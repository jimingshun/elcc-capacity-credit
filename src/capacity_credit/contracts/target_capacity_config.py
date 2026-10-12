"""Target-capacity application configuration contract (Step 2 Stage A).

需求类设计 §3（主责口径）的瘦合同：来源充裕度项目引用（血缘）、绝对目标
（LOLE/EUE 二选一，目标值>0）、求解口径单选、搜索区间与容差。

与 EFC 合同的关系：正式 ``RunConfig`` 不携带 method_config（本方法不走
elcc/efc 的 method_config 联合类型）；类型化配置整体冻结为运行目录内的
``target_capacity_config.json``，其规范化哈希经 create_run 的
application_config_hash 入身份。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_credit.contracts.enums import (
    AllocationType,
    CapacityValueType,
    ChronologyMode,
    RiskMetric,
    RunMode,
    RunType,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.runs import (
    RunConfig,
    _check_allocation,
    _check_capacity_value,
    _check_chronology,
    _check_mri_fields,
)

_TARGET_METRICS = (RiskMetric.LOLE_DAYS_PER_YEAR, RiskMetric.EUE_MWH_PER_YEAR)


class TargetCapacityRunSettings(BaseModel):
    """DRAFT run-level settings of the target-capacity application input.

    NOT a persistence contract: ``to_run_config()`` on the application
    config builds the FORMAL frozen ``RunConfig`` (run-config 1.0 shape,
    method_config=None - the typed application payload is frozen
    separately as target_capacity_config.json)."""

    model_config = ConfigDict(extra="forbid")

    run_config_schema_version: Literal["1.0"]
    run_mode: RunMode
    run_type: RunType
    method_profile: str
    primary_risk_metric: RiskMetric
    weather_year_weight_set_id: str
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    )
    soc_year_boundary_mode: SocYearBoundaryMode = (
        SocYearBoundaryMode.NOT_APPLICABLE
    )
    capacity_value_type: CapacityValueType = CapacityValueType.AVERAGE
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType = AllocationType.NONE
    random_seed: int
    scenario_bank_id: str | None = None
    scenario_id: str | None = None

    @model_validator(mode="after")
    def _validate_draft(self) -> Self:
        if self.run_type is not RunType.DEMAND_TARGET_CAPACITY:
            raise ValueError(
                "run_type must be 'demand_target_capacity' for a "
                "target-capacity application config"
            )
        if self.primary_risk_metric not in _TARGET_METRICS:
            raise ValueError(
                "primary_risk_metric must be LOLE or EUE for target capacity "
                "（需求类设计 §3.1：目标指标二选一）"
            )
        _check_chronology(self.chronology_mode, self.soc_year_boundary_mode)
        _check_mri_fields(self.run_type, None, None)
        _check_capacity_value(self.capacity_value_type, self.baseline_portfolio_version)
        _check_allocation(self.allocation_type, None)
        return self

class TargetCapacitySourceRef(BaseModel):
    """The COMPLETED adequacy run this search stands on（血缘引用）."""

    model_config = ConfigDict(extra="forbid")

    project_ref: str = Field(min_length=1)
    run_ref: str = Field(min_length=1)


class TargetCapacityTargetSpec(BaseModel):
    """The absolute risk target（需求类设计 §3.1）."""

    model_config = ConfigDict(extra="forbid")

    metric: RiskMetric
    value: float = Field(gt=0.0)
    target_year: int


class TargetCapacitySearchSpec(BaseModel):
    """Search bounds and stopping rule（需求类设计 §3.1）.

    两轴互斥（Q-D4  2026-10-10）：MW 轴＝调整完美容量口径；
    λ 轴＝调整负荷口径（负荷缩放系数，无量纲）。口径决定哪组必填、
    另一组必须缺席（应用配置级校验，见 TargetCapacityApplicationConfig）。
    """

    model_config = ConfigDict(extra="forbid")

    lower_bound_mw: float | None = Field(default=None, ge=0.0)
    upper_bound_mw: float | None = Field(default=None, gt=0.0)
    capacity_tolerance_mw: float | None = Field(default=None, gt=0.0)
    # Q-D4：调整负荷口径的 λ 轴（无量纲负荷缩放系数）
    lower_bound_load_scale: float | None = Field(default=None, gt=0.0)
    upper_bound_load_scale: float | None = Field(default=None, gt=0.0)
    load_scale_tolerance: float | None = Field(default=None, gt=0.0)
    max_iterations: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        mw_axis = (
            self.lower_bound_mw is not None
            or self.upper_bound_mw is not None
            or self.capacity_tolerance_mw is not None
        )
        load_axis = (
            self.lower_bound_load_scale is not None
            or self.upper_bound_load_scale is not None
            or self.load_scale_tolerance is not None
        )
        if mw_axis and load_axis:
            raise ValueError(
                "search axis must be exclusive: MW 轴（调整完美容量）与"
                " λ 轴（调整负荷）不得同时出现"
            )
        if mw_axis and None in (
            self.lower_bound_mw, self.upper_bound_mw, self.capacity_tolerance_mw
        ):
            raise ValueError("MW 轴三字段（下界/上界/容差）须同时给出")
        if mw_axis and self.upper_bound_mw <= self.lower_bound_mw:
            raise ValueError("upper_bound_mw must be > lower_bound_mw")
        if load_axis and None in (
            self.lower_bound_load_scale,
            self.upper_bound_load_scale,
            self.load_scale_tolerance,
        ):
            raise ValueError("λ 轴三字段（下界/上界/容差）须同时给出")
        if (
            load_axis
            and self.upper_bound_load_scale <= self.lower_bound_load_scale
        ):
            raise ValueError(
                "upper_bound_load_scale must be > lower_bound_load_scale"
            )
        if not mw_axis and not load_axis:
            raise ValueError("search requires either the MW axis or the λ axis")
        return self


class TargetCapacityApplicationConfig(BaseModel):
    """Versioned target-capacity application configuration (schema 1.0)."""

    model_config = ConfigDict(extra="forbid")

    target_capacity_config_schema_version: Literal["1.0"] = "1.0"
    run_config: TargetCapacityRunSettings
    source: TargetCapacitySourceRef
    target: TargetCapacityTargetSpec
    search: TargetCapacitySearchSpec
    # 求解口径单选（强制显式，无默认——提示词9.2/需求类设计 §3.1）。
    # Q-D4：adjust_load 开放——供给冻结、原生负荷曲线
    # 保形缩放（λ 轴搜索）；与 adjust_perfect_capacity 并列、不可互比。
    caliber: Literal["adjust_perfect_capacity", "adjust_load"]

    @model_validator(mode="after")
    def _validate_metric_agreement(self) -> Self:
        if self.target.metric is not self.run_config.primary_risk_metric:
            raise ValueError(
                "target.metric must equal run_config.primary_risk_metric "
                "（搜索判据与运行主指标必须是同一个风险量）"
            )
        # 口径与搜索轴一致（Q-D4）：MW 轴↔完美容量；λ 轴↔调整负荷
        mw_axis = self.search.lower_bound_mw is not None
        load_axis = self.search.lower_bound_load_scale is not None
        if self.caliber == "adjust_perfect_capacity" and not mw_axis:
            raise ValueError(
                "caliber=adjust_perfect_capacity requires the MW search axis"
                "（lower_bound_mw/upper_bound_mw/capacity_tolerance_mw）"
            )
        if self.caliber == "adjust_load" and not load_axis:
            raise ValueError(
                "caliber=adjust_load requires the λ search axis"
                "（lower_bound_load_scale/upper_bound_load_scale/"
                "load_scale_tolerance）"
            )
        return self

    @classmethod
    def from_json(cls, text: str) -> TargetCapacityApplicationConfig:
        return cls.model_validate_json(text)

    def to_json(self) -> str:
        return self.model_dump_json()

    def to_run_config(self) -> RunConfig:
        """Build the FORMAL frozen RunConfig (1.0 shape, method_config=None)."""
        return RunConfig.model_validate(self.run_config.model_dump(mode="json"))


__all__ = [
    "TargetCapacityApplicationConfig",
    "TargetCapacityRunSettings",
    "TargetCapacitySearchSpec",
    "TargetCapacitySourceRef",
    "TargetCapacityTargetSpec",
]
