"""Sample cost catalog (night-5 N2).

THE machine / province / zone / technology sample tables, transcribed
from the reviewed v1.2 Excel template (系数表 sheet). Every value is a
TEMPLATE SAMPLE pending verification - the catalog is never presented
as calibrated provincial standards or industry conventions. The
template's spare zone-slot filler values ("135") are deliberately NOT
copied: the tool seeds only the legitimate 全省 default rows.
"""

from __future__ import annotations

from typing import Any

# ① per-machine base static cost (元/kW) and capacity (MW). The v1.2
# template removed the double machine markup: K_machine is uniformly 1
# (the machine difference lives in the base cost itself).
_MACHINES: tuple[dict[str, Any], ...] = (
    {
        "machine_id": "2x1000MW超超临界",
        "k_machine": 1.0,
        "capacity_mw": 2000,
        "base_cost_per_kw": 3750.0,
        "basis": "原模板3750元/kW示例；项目投资口径待核实",
    },
    {
        "machine_id": "2x660MW超超临界",
        "k_machine": 1.0,
        "capacity_mw": 1320,
        "base_cost_per_kw": 3950.0,
        "basis": "原模板3950元/kW示例；原1.05重复加成已移除",
    },
    {
        "machine_id": "2x350MW超临界",
        "k_machine": 1.0,
        "capacity_mw": 700,
        "base_cost_per_kw": 4300.0,
        "basis": "原模板4300元/kW示例；原1.15重复加成已移除",
    },
)

# ② provincial coefficients (three sample tiers; all pending verification)
_PROVINCES: tuple[tuple[str, float], ...] = (
    ("安徽", 1.00),
    ("福建", 1.01),
    ("江西", 0.99),
    ("浙江", 1.00),
    ("北京", 1.05),
    ("天津", 1.05),
    ("河北", 1.05),
    ("上海", 1.05),
    ("江苏", 1.05),
    ("广东", 1.05),
    ("山东", 1.05),
    ("海南", 1.05),
    ("山西", 1.00),
    ("河南", 1.00),
    ("湖北", 1.00),
    ("湖南", 1.00),
    ("黑龙江", 1.00),
    ("吉林", 1.00),
    ("辽宁", 1.00),
    ("重庆", 1.00),
    ("四川", 1.00),
    ("陕西", 1.00),
    ("广西", 1.00),
    ("贵州", 1.00),
    ("云南", 1.00),
    ("内蒙古", 0.95),
    ("甘肃", 0.95),
    ("青海", 0.95),
    ("宁夏", 0.95),
    ("新疆", 0.95),
    ("西藏", 0.95),
)

# ③ zone mapping: every province gets its 全省=1 default row. Custom
# zones are a user extension (the template's 41 slots); unknown or
# duplicate keys are a hard refusal in the calculation, never a
# fallback.
_ZONE_DEFAULT = 1.0

# ④ technology correction toggles: K_technology = 1 + Σ(enabled × delta)
TECH_TOGGLES: dict[str, float] = {
    "二次再热": 0.04,
    "间接空冷": 0.06,
    "供热改造": 0.03,
}

_CATALOG_DISCLAIMER = (
    "模板示例系数（待核实），不是已校准省级标准或行业惯例"
)


def machine_catalog() -> dict[str, dict[str, Any]]:
    """The machine sample table keyed by machine id (fresh dicts)."""
    return {m["machine_id"]: dict(m) for m in _MACHINES}


def province_coefficients() -> dict[str, float]:
    """The provincial sample coefficients keyed by province name."""
    return dict(_PROVINCES)


def zone_coefficients() -> dict[str, float]:
    """The "省份|分区" zone mapping (the seeded 全省 defaults; fresh
    dict)."""
    return {
        f"{province}|全省": _ZONE_DEFAULT for province, _ in _PROVINCES
    }


def catalog_disclaimer() -> str:
    """The honest sample-data disclaimer carried into results/reports."""
    return _CATALOG_DISCLAIMER


__all__ = [
    "TECH_TOGGLES",
    "catalog_disclaimer",
    "machine_catalog",
    "province_coefficients",
    "zone_coefficients",
]
