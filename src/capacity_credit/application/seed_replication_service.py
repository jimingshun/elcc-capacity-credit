"""MR-03 (A3): seed replication - the cross-seed robustness gate.

方法说明第五章(三)与第八章风险5: a single-seed ELCC/EFC point value is
NOT review-grade; the final result must be a point ± 95% CI derived from
N seed-replicated full searches (default 20, provisional), with an IQR
gate on the credit fraction (1.5 percentage points, provisional).

Structure (adjudicated revision doc, MR-03):

- ONE pure statistics core (:func:`summarize_seed_replication`) - the
  single home of the gate math; nothing else re-derives median/CI/IQR
  for replication groups;
- ONE orchestration entry (:func:`run_seed_replication_for_elcc`) that
  re-runs the REAL production ELCC pipeline once per seed - only the
  root seed (and the scenario bank it implies) changes; every other
  input stays the frozen base configuration;
- the summary artifact (``seed_replication_summary.json``) follows the
  run-dir unregistered-evidence pattern and is what the formal-gate
  check and the report layer consume. The formal MethodResult JSON is
  never rewritten (immutable publication) - its ``lower_ci/upper_ci``
  slots are filled by a future pre-publication flow; until then the
  CI travels with this artifact and the report layer.

Teaching/research chains are exempt by adjudication: they keep the
honest ``*_CI_NOT_AVAILABLE`` diagnostics instead.
"""

from __future__ import annotations

import math
from typing import Any

_VALID_POINT_STATUSES = frozenset({"valid", "valid_with_warning"})


class SeedReplicationError(ValueError):
    """Stable seed-replication error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_valid_group(group: dict[str, Any]) -> bool:
    status = str(group.get("result_status") or "")
    return status in _VALID_POINT_STATUSES and group.get("estimate_mw") is not None


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile on an ascending list (numpy 'linear'
    semantics without a numpy import at module scope)."""
    if not sorted_values:
        raise ValueError("quantile of an empty list")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return float(sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction)


def summarize_seed_replication(
    groups: list[dict[str, Any]],
    *,
    credit_iqr_max_pp: float = 1.5,
    min_valid_ratio: float = 0.8,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """MR-03.2: the ONE gate computation over per-seed groups.

    Only ``valid | valid_with_warning`` groups with a non-null point
    estimate count (a not_converged seed is recorded and excluded, never
    interpolated). The reported point is the MEDIAN of the per-seed
    estimates; the 95% CI is Student-t on the seed distribution; the
    gate requires >= ceil(min_valid_ratio * N) valid groups AND a credit
    fraction IQR within ``credit_iqr_max_pp`` percentage points."""
    if not groups:
        raise SeedReplicationError(
            "SEED_REPLICATION_NO_GROUPS",
            "seed replication requires at least one group",
        )
    expected = len(groups)
    valid = [g for g in groups if _is_valid_group(g)]
    estimates = sorted(float(g["estimate_mw"]) for g in valid)
    fractions = sorted(
        float(g["credit_fraction"])
        for g in valid
        if g.get("credit_fraction") is not None
    )
    n = len(estimates)
    median = _quantile(estimates, 0.5) if estimates else None
    mean = (sum(estimates) / n) if estimates else None
    sd = None
    ci_lower = None
    ci_upper = None
    ci_method = None
    if n >= 2:
        variance = sum((v - mean) ** 2 for v in estimates) / (n - 1)
        sd = math.sqrt(variance)
        try:
            from scipy import stats as sp_stats

            critical = float(sp_stats.t.ppf(1.0 - (1.0 - confidence_level) / 2.0, df=n - 1))
            ci_method = "student_t"
        except Exception:  # noqa: BLE001 - scipy missing: honest normal approx
            critical = 1.96
            ci_method = "normal_approximation"
        half_width = critical * sd / math.sqrt(n)
        ci_lower = mean - half_width
        ci_upper = mean + half_width
    iqr_pp = None
    if len(fractions) >= 2:
        iqr_pp = (_quantile(fractions, 0.75) - _quantile(fractions, 0.25)) * 100.0

    reasons: list[str] = []
    min_valid = math.ceil(float(min_valid_ratio) * expected)
    if n < min_valid:
        reasons.append(
            f"有效组数 {n} 少于下限 {min_valid}（共 {expected} 组，"
            f"要求 ≥ {min_valid_ratio:.0%} 收敛）"
        )
    if iqr_pp is None:
        if valid:
            reasons.append("信用分数不可用（缺冻结铭牌分母），IQR 门限无法判定")
    elif iqr_pp > float(credit_iqr_max_pp):
        reasons.append(
            f"信用分数 IQR {iqr_pp:.2f} 个百分点超过门限 {credit_iqr_max_pp}（方法说明第五章(三)）"
        )
    return {
        "seed_replication_schema_version": "1.0",
        "group_count": expected,
        "valid_count": n,
        "invalid_count": expected - n,
        "estimate_mw": {
            "median": median,
            "mean": mean,
            "sd": sd,
            "ci95_lower": ci_lower,
            "ci95_upper": ci_upper,
            "ci_method": ci_method,
        },
        "credit_fraction_iqr_pp": iqr_pp,
        "gate": {
            "passed": not reasons,
            "reasons": reasons,
            "min_valid_ratio": float(min_valid_ratio),
            "credit_iqr_max_pp": float(credit_iqr_max_pp),
        },
        "provisional_status": "provisional",
        "groups": list(groups),
        "attribution_note": (
            "各组为完整独立搜索（仅 root_seed 不同，其余输入冻结一致）；"
            "not_converged 组如实记录并剔除，不插值；搜索 bracket 永不冒充统计 CI。"
        ),
    }


def gate_failed_code(summary: dict[str, Any]) -> str:
    """The stable gate verdict for consumers (report layer, publish
    gate): ``seed_replication_passed`` | ``seed_replication_failed``."""
    return (
        "seed_replication_passed"
        if summary.get("gate", {}).get("passed")
        else "seed_replication_failed"
    )


def run_seed_replication_batch(
    *,
    workspace_root: Any,
    batch_ref: str,
    project_dir: Any,
    import_batch_id: str,
    data_version_id: str,
    base_app_config: dict[str, Any],
    target_run_dir: Any,
    samples: int = 30,
    created_by: str = "seed_replication",
    software_version: str = "0.1.0",
    software_commit: str = "dev",
) -> dict[str, Any]:
    """F3: execute ONE seed_replication research batch end to end.

    The batch's points carry ``params.seed`` - one point per
    replication group. The orchestrator runs ALL groups (only the root
    seed changes), the summary is written through to the reviewed
    formal run's directory (the bind gate + report block consume it),
    and every point is finalized with its OWN group status (a
    not_converged group stays honest evidence on the ledger, never
    interpolated). Points must be in 待派生/失败 (the DR-01 forward-only
    ledger); anything else refuses.
    """
    from capacity_credit.application import research_batch_service

    manifest = research_batch_service.read_batch(workspace_root, batch_ref)
    if manifest.get("kind") != "seed_replication":
        raise SeedReplicationError(
            "SEED_REPLICATION_BATCH_KIND_MISMATCH",
            f"批次 {batch_ref} 类型为 {manifest.get('kind')!r}，不是 seed_replication",
        )
    points = manifest.get("points") or []
    seeds: list[int] = []
    for point in points:
        seed = (point.get("params") or {}).get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise SeedReplicationError(
                "SEED_REPLICATION_INVALID_SEED",
                f"点位 {point.get('point_id')} 缺少整数 params.seed",
            )
        if str(point.get("status")) not in ("待派生", "失败"):
            raise SeedReplicationError(
                "SEED_REPLICATION_POINT_STATE",
                f"点位 {point.get('point_id')} 状态为 {point.get('status')}，"
                "只有待派生/失败可执行",
            )
        seeds.append(int(seed))
    if not seeds:
        raise SeedReplicationError(
            "SEED_REPLICATION_NO_SEEDS", "批次没有任何点位（params.seed）"
        )
    summary = run_seed_replication_for_elcc(
        project_dir=project_dir,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        base_app_config=base_app_config,
        seeds=seeds,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        samples=samples,
        target_run_dir=target_run_dir,
    )
    by_seed = {g["seed"]: g for g in summary.get("groups", [])}
    for point in points:
        seed = int((point.get("params") or {}).get("seed"))
        group = by_seed.get(seed, {})
        status = group.get("result_status") or "not_converged"
        diagnosis = (
            f"seed={seed} 状态 {status}；门 "
            f"{'通过' if summary.get('gate', {}).get('passed') else '未通过'}"
        )
        target_state = (
            "完成" if status in ("valid", "valid_with_warning") else "失败"
        )
        for intermediate in ("待确认", "待运行"):
            research_batch_service.update_point(
                workspace_root, batch_ref, str(point.get("point_id")),
                status=intermediate,
            )
        research_batch_service.update_point(
            workspace_root,
            batch_ref,
            str(point.get("point_id")),
            status=target_state,
            diagnosis=diagnosis,
            extra={"metric_value": group.get("estimate_mw"), "root_seed": seed},
        )
    return summary


__all__ = [
    "SeedReplicationError",
    "gate_failed_code",
    "run_seed_replication_for_elcc",
    "summarize_seed_replication",
]


def run_seed_replication_for_elcc(
    *,
    project_dir: Any,
    import_batch_id: str,
    data_version_id: str,
    base_app_config: dict[str, Any],
    seeds: list[int],
    created_by: str = "seed_replication",
    software_version: str = "0.1.0",
    software_commit: str = "dev",
    credit_iqr_max_pp: float = 1.5,
    min_valid_ratio: float = 0.8,
    weight_set_id: str = "w1",
    samples: int = 30,
    target_run_dir: Any = None,
) -> dict[str, Any]:
    """MR-03.1: re-run the REAL production ELCC pipeline once per seed.

    Only the root seed (and the scenario bank it implies) changes; every
    other input stays the frozen base configuration verbatim. Groups run
    in TEACHING mode (the formal result under review is already bound to
    the project; replication groups are robustness evidence, not new
    formal results). Returns the gate summary per
    :func:`summarize_seed_replication`.

    ``target_run_dir`` (optional): when given, the summary is ALSO
    written into that (formal) run's directory via
    ``write_seed_replication_summary`` - the bind gate and the report
    review block consume exactly that file. A FAILED gate is written
    too (honest evidence; the gate stays failed on read)."""
    import copy
    import uuid

    from capacity_credit.application.elcc_workflow import run_elcc_pipeline
    from capacity_credit.application.p1_workflow import scenario_build
    from capacity_credit.application.result_service import read_method_result
    from capacity_credit.contracts.elcc_config import ElccApplicationConfig

    if not seeds:
        raise SeedReplicationError(
            "SEED_REPLICATION_NO_SEEDS", "seed replication requires a non-empty seed list"
        )
    base_config = dict(copy.deepcopy(base_app_config))
    base_config.setdefault("run_config", {})
    base_config["run_config"]["run_mode"] = "teaching"

    groups: list[dict[str, Any]] = []
    for seed in seeds:
        built = scenario_build(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            root_seed=int(seed),
            timezone="Asia/Shanghai",
            samples=int(samples),
            request_id=f"seedrep-scn-{seed}-{uuid.uuid4().hex[:8]}",
        )
        config = copy.deepcopy(base_config)
        config["run_config"]["random_seed"] = int(seed)
        config["run_config"]["scenario_bank_id"] = built["scenario_bank_id"]
        projection = run_elcc_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=f"seedrep-run-{seed}-{uuid.uuid4().hex[:8]}",
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=ElccApplicationConfig.model_validate(config),
        )
        run_dir = project_dir / "runs" / str(projection["run_id"])
        result = read_method_result(run_dir / "method_result.json")
        groups.append(
            {
                "seed": int(seed),
                "run_id": str(projection["run_id"]),
                "result_status": result.result_status.value,
                "risk_convergence": result.risk_convergence.value,
                "search_convergence": result.search_convergence.value,
                "estimate_mw": result.accredited_capacity_mw,
                "credit_fraction": result.capacity_credit_fraction,
                "reference_capacity_mw": result.reference_capacity_mw,
            }
        )
    summary = summarize_seed_replication(
        groups,
        credit_iqr_max_pp=credit_iqr_max_pp,
        min_valid_ratio=min_valid_ratio,
    )
    if target_run_dir is not None:
        from capacity_credit.application.result_service import (
            write_seed_replication_summary,
        )

        write_seed_replication_summary(target_run_dir, summary)
    return summary
