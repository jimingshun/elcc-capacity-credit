"""Controlled storage SOC initialization configuration (T14).

Strictly typed, schema-exported configuration for the two methodology
§6.3 initialization modes (``weather_cycle_warmup`` /
``stochastic_sample_burnin``) plus the S-13 explicit-initial-SOC escape
hatch. The config is embedded as ``storage_soc`` inside the ELCC method
config, so it enters run_config.yaml, the run_config_hash, create_run
idempotency parameters and the run-config JSON Schema.

Burn-in parameters are PROVISIONAL by design: they are read from this
controlled config (never hardcoded as policy constants), carry an
explicit ``provisional`` status and a ``pending_benchmark`` marker, and
must never be presented as benchmark-locked values. The ELCC method
convergence parameters (e.g. ``recent_batch_stability_tolerance``) are
NOT a SOC burn-in configuration and must never be reused as the SOC
stability threshold.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from capacity_credit.contracts.enums import StorageSocInitializationMode

STORAGE_SOC_CONFIG_SCHEMA_VERSION: Literal["1.0"] = "1.0"
BURNIN_CONFIG_SCHEMA_VERSION: Literal["1.0"] = "1.0"


def _finite_nonneg_number(value: object, field: str) -> float:
    """Strict numeric gate: bool / strings / NaN / +-inf / negatives are
    rejected (pseudo-numbers never bypass the configuration)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a real number, got {value!r}")  # noqa: TRY004
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    if number < 0.0:
        raise ValueError(f"{field} must be non-negative, got {value!r}")
    return number


class StorageBurninConfig(BaseModel):
    """Stochastic sample burn-in parameters (provisional / pending
    benchmark - never policy constants). Stability is judged by the
    LENGTH comparison of the same batch and stream (see
    storage_chronology.compare_burnin_lengths); the two tolerances are
    part of this controlled config and enter the schema and hashes."""

    model_config = ConfigDict(extra="forbid")

    burnin_config_schema_version: Literal["1.0"] = BURNIN_CONFIG_SCHEMA_VERSION
    burnin_hours: int
    length_stability_soc_tolerance_mwh: float
    length_stability_eue_tolerance_mwh: float
    provisional_status: Literal["provisional"] = "provisional"
    benchmark_status: Literal["pending_benchmark"] = "pending_benchmark"

    @field_validator("burnin_hours", mode="before")
    @classmethod
    def _hours(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"burnin_hours must be an integer, got {value!r}")  # noqa: TRY004
        if value <= 0:
            raise ValueError("burnin_hours must be positive")
        return value

    @field_validator(
        "length_stability_soc_tolerance_mwh",
        "length_stability_eue_tolerance_mwh",
        mode="before",
    )
    @classmethod
    def _tolerance(cls, value: object, info: Any) -> object:
        return _finite_nonneg_number(value, info.field_name or "tolerance")


class StorageEndOfCycleRechargeConfig(BaseModel):
    """W2/B6-2: the end-of-cycle recharge window (方法说明第三章(一)
    周期末回充约束, provisional). Disabled by default - enabling it
    floors each unit's discharge inside the final ``window_hours`` of
    every charge cycle at ``target_soc_fraction x energy_capacity``
    (energy for the next cycle; no charging is ever invented)."""

    model_config = ConfigDict(extra="forbid")

    end_of_cycle_schema_version: Literal["1.0"] = "1.0"
    enabled: bool = False
    window_hours: int = 4
    target_soc_fraction: float = 0.8
    provisional_status: Literal["provisional"] = "provisional"

    @field_validator("window_hours", mode="before")
    @classmethod
    def _window(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8760:
            raise ValueError("window_hours must be an integer in [1, 8760]")
        return value

    @field_validator("target_soc_fraction", mode="before")
    @classmethod
    def _target(cls, value: object) -> object:
        number = _finite_nonneg_number(value, "target_soc_fraction")
        if number <= 0.0 or number > 1.0:
            raise ValueError("target_soc_fraction must lie in (0, 1]")
        return number


class StorageSocConfig(BaseModel):
    """Typed storage SOC initialization configuration (methodology V0.4
    §6.3). Exactly one initialization mode; the burn-in section is
    required for stochastic_sample_burnin and forbidden otherwise; the
    S-13 explicit-initial-SOC escape hatch is only for specific tests or
    explicit user configuration and never becomes the default warm-up."""

    model_config = ConfigDict(extra="forbid")

    storage_soc_config_schema_version: Literal["1.0"] = STORAGE_SOC_CONFIG_SCHEMA_VERSION
    initialization_mode: StorageSocInitializationMode
    warmup_max_cycles: int = 5
    burnin: StorageBurninConfig | None = None
    explicit_initial_soc_mwh: dict[str, float] | None = None
    # W2/B6-2: optional end-of-cycle recharge window (None = disabled =
    # legacy behavior); enters run_config hashes with the storage_soc
    # section
    end_of_cycle_recharge: StorageEndOfCycleRechargeConfig | None = None

    @field_validator("warmup_max_cycles", mode="before")
    @classmethod
    def _warmup_cycles(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"warmup_max_cycles must be an integer, got {value!r}")  # noqa: TRY004
        if not 1 <= value <= 5:
            raise ValueError(
                "warmup_max_cycles must lie in [1, 5] (methodology V0.4 "
                "§6.3: at most five warm-up cycles)"
            )
        return value

    @field_validator("explicit_initial_soc_mwh", mode="before")
    @classmethod
    def _explicit_soc(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("explicit_initial_soc_mwh must be a mapping")  # noqa: TRY004
        for storage_id, soc in value.items():
            if not isinstance(storage_id, str) or not storage_id.strip():
                raise ValueError("explicit_initial_soc_mwh keys must be non-empty strings")
            _finite_nonneg_number(soc, f"explicit_initial_soc_mwh[{storage_id!r}]")
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> Self:
        if self.initialization_mode is StorageSocInitializationMode.NOT_APPLICABLE:
            raise ValueError(
                "storage_soc.initialization_mode must be weather_cycle_warmup "
                "or stochastic_sample_burnin (never not_applicable)"
            )
        if self.initialization_mode is StorageSocInitializationMode.STOCHASTIC_SAMPLE_BURNIN:
            if self.burnin is None:
                raise ValueError("stochastic_sample_burnin requires the burnin section")
        elif self.burnin is not None:
            raise ValueError("the burnin section is only valid for stochastic_sample_burnin")
        return self


__all__ = [
    "BURNIN_CONFIG_SCHEMA_VERSION",
    "STORAGE_SOC_CONFIG_SCHEMA_VERSION",
    "StorageBurninConfig",
    "StorageSocConfig",
]
