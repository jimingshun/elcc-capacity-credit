"""Typer command-line entry point for the capacity credit toolkit.

P1--P5 use the CLI as the formal entry point. T09 adds the four P1
commands: ``project import``, ``validate``, ``scenario build`` and
``run adequacy``. The CLI stays thin: it defines commands and
parameters, calls public application services, converts results to
terminal output and maps stable application errors to exit codes
(0 success, 2 missing required args via Typer, 1 application/contract
failure). Ordinary commands catch ``Exception`` only; ``BaseException``
is reserved for the underlying atomic-cleanup code with a reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from capacity_credit.application.p1_workflow import (
    P1WorkflowError,
    import_project,
    run_adequacy_pipeline,
    scenario_build,
    validate_project,
)

app = typer.Typer(
    name="elcc-tool",
    help="ELCC可靠容量测算工具（Capacity Credit Assessment Toolkit）",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """ELCC可靠容量测算工具（Capacity Credit Assessment Toolkit）"""


def _fail(error: BaseException) -> None:
    if isinstance(error, P1WorkflowError):
        typer.echo(f"ERROR {error.code}: {error.message}")
    else:
        typer.echo(f"ERROR P1_UNEXPECTED: {error}")
    raise typer.Exit(code=1)


@app.command("serve")
def serve(
    workspace: Annotated[
        str,
        typer.Option(
            ...,
            "--workspace",
            help="工作区路径（显式必填；不从环境变量、固定路径或当前目录自动发现）",
        ),
    ],
    port: Annotated[
        int, typer.Option("--port", help="本机服务端口（1-65535，默认8600）")
    ] = 8600,
) -> None:
    """启动Web版工作台（仅绑定127.0.0.1）。

    与 `ui` 命令同一个 WorkbenchService 门面、同一个工作区：浏览器
    访问 http://127.0.0.1:<port>/ 即可使用。工作区无效以退出码1明确
    失败；参数错误退出码2。当前接通成本域（切片①）；可靠性数据链与
    运行视图按切片②③接入。
    """
    if not (1 <= port <= 65535):
        typer.echo(
            f"ERROR SERVE_PORT_INVALID: --port 必须在1-65535范围内，收到 {port}"
        )
        raise typer.Exit(code=2)
    if not workspace.strip():
        typer.echo(
            "ERROR SERVE_WORKSPACE_BLANK: --workspace 不能为空：工作区必须"
            "显式提供，不会回退到当前目录。"
        )
        raise typer.Exit(code=2)
    from capacity_credit.application.workbench_service import (
        WorkbenchError,
        WorkbenchService,
    )

    try:
        service = WorkbenchService(workspace)
    except WorkbenchError as exc:
        typer.echo(f"ERROR SERVE_WORKSPACE_INVALID（{exc.kind}）：{exc.message}")
        raise typer.Exit(code=1) from None
    from capacity_credit.webapp import serve as run_webapp

    run_webapp(service.workspace_root, port)


@app.command()
def version() -> None:
    """Print the installed package version."""
    from capacity_credit import __version__

    typer.echo(__version__)


project_app = typer.Typer(help="项目导入与数据版本管理")
app.add_typer(project_app, name="project")


@project_app.command("import")
def project_import(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    dataset_id: Annotated[str, typer.Option(..., "--dataset-id", help="数据集ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    grade: Annotated[str, typer.Option("--grade", help="数据等级（默认教学G0）")] = "G0_teaching",
) -> None:
    """导入并自动校验项目（不自动批准/冻结）"""
    try:
        result = import_project(
            project_dir=project_dir,
            dataset_id=dataset_id,
            data_version_id=data_version_id,
            created_by=created_by,
            grade=grade,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"import_batch_id={result['import_batch_id']}")
    typer.echo(f"data_version_id={result['data_version_id']}")
    typer.echo(f"validation_status={result['validation_status']}")


@app.command()
def validate(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
) -> None:
    """自动校验（只产生passed/failed，不批准不冻结）"""
    try:
        result = validate_project(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"validation_status={result['validation_status']}")
    typer.echo(f"issue_count={result['issue_count']}")


scenario_app = typer.Typer(help="场景库管理")
app.add_typer(scenario_app, name="scenario")


@scenario_app.command("build")
def scenario_build_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    root_seed: Annotated[int, typer.Option(..., "--root-seed", help="随机根种子")],
    timezone: Annotated[str, typer.Option("--timezone", help="时区")] = "Asia/Shanghai",
    samples: Annotated[int, typer.Option("--samples", help="样本数（默认30）")] = 30,
    weight_set_id: Annotated[
        str, typer.Option("--weight-set-id", help="气象年权重集（默认w1）")
    ] = "w1",
) -> None:
    """构建可复现场景库（要求已审批并冻结）"""
    try:
        result = scenario_build(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            root_seed=root_seed,
            timezone=timezone,
            samples=samples,
            weight_set_id=weight_set_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"scenario_bank_id={result['scenario_bank_id']}")


run_app = typer.Typer(help="运行管理")
app.add_typer(run_app, name="run")


@run_app.command("adequacy")
def run_adequacy_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    scenario_bank_id: Annotated[str, typer.Option(..., "--scenario-bank-id", help="场景库ID")],
    request_id: Annotated[str, typer.Option(..., "--request-id", help="请求ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    root_seed: Annotated[int, typer.Option(..., "--root-seed", help="随机根种子")],
    primary_risk_metric: Annotated[
        str, typer.Option("--primary-risk-metric", help="主风险指标")
    ] = "lole_days_per_year",
    weight_set_id: Annotated[
        str, typer.Option("--weight-set-id", help="气象年权重集（默认w1）")
    ] = "w1",
    scenario_id: Annotated[
        str | None, typer.Option("--scenario-id", help="新能源场景ID（多场景数据必须显式选择）")
    ] = None,
    software_version: Annotated[str, typer.Option("--software-version", help="软件版本")] = "dev",
    software_commit: Annotated[str, typer.Option("--software-commit", help="软件提交")] = "dev",
) -> None:
    """执行充裕度运行并生成method_result/artifact_manifest"""
    try:
        projection = run_adequacy_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            scenario_bank_id=scenario_bank_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            root_seed=root_seed,
            primary_risk_metric=primary_risk_metric,
            weight_set_id=weight_set_id,
            scenario_id=scenario_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"run_id={projection['run_id']}")
    typer.echo(f"result_id={projection.get('result_id') or 'null'}")
    typer.echo(f"artifact_manifest_id={(projection.get('artifact_manifest_ids') or ['null'])[0]}")
    typer.echo(f"sample_count={projection.get('sample_count')}")
    typer.echo(f"risk_convergence={projection.get('risk_convergence')}")
    typer.echo(f"result_status={projection.get('result_status')}")
    typer.echo(f"lifecycle_status={projection.get('lifecycle_status')}")
    if projection.get("result_id") is None:
        typer.echo("ERROR P1_NO_SAMPLES: no method_result was produced (zero samples processed)")
        raise typer.Exit(code=1)


@run_app.command("elcc")
def run_elcc_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    config_path: Annotated[Path, typer.Option(..., "--config", help="ELCC应用配置YAML路径")],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    request_id: Annotated[str, typer.Option(..., "--request-id", help="请求ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    software_version: Annotated[str, typer.Option("--software-version", help="软件版本")] = "dev",
    software_commit: Annotated[str, typer.Option("--software-commit", help="软件提交")] = "dev",
) -> None:
    """执行ELCC端到端运行（读取严格版本化配置并调用RunService）"""
    try:
        from pydantic import ValidationError

        from capacity_credit.application.elcc_workflow import (
            P1_ELCC_INVALID_CONFIG,
            run_elcc_pipeline,
        )
        from capacity_credit.contracts.elcc_config import (
            ElccApplicationConfig,
        )

        if not config_path.is_file():
            typer.echo("ERROR P1_CONFIG_MISSING: ELCC config file not found")
            raise typer.Exit(code=1)
        try:
            app_config = ElccApplicationConfig.from_yaml(config_path.read_text(encoding="utf-8"))
        except (ValidationError, TypeError, ValueError) as exc:
            typer.echo(f"ERROR {P1_ELCC_INVALID_CONFIG}: invalid ELCC config: {exc}")
            raise typer.Exit(code=1)
        projection = run_elcc_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=app_config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"run_id={projection['run_id']}")
    typer.echo(f"result_id={projection.get('result_id') or 'null'}")
    typer.echo(f"artifact_manifest_id={(projection.get('artifact_manifest_ids') or ['null'])[0]}")
    typer.echo(f"sample_count={projection.get('sample_count')}")
    typer.echo(f"risk_convergence={projection.get('risk_convergence')}")
    typer.echo(f"result_status={projection.get('result_status')}")
    typer.echo(f"lifecycle_status={projection.get('lifecycle_status')}")
    typer.echo(f"accredited_capacity_mw={projection.get('accredited_capacity_mw')}")
    if projection.get("result_id") is None:
        typer.echo("ERROR P1_NO_SAMPLES: no method_result was produced (zero samples processed)")
        raise typer.Exit(code=1)


@run_app.command("efc")
def run_efc_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    config_path: Annotated[Path, typer.Option(..., "--config", help="EFC应用配置YAML路径")],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    request_id: Annotated[str, typer.Option(..., "--request-id", help="请求ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    software_version: Annotated[str, typer.Option("--software-version", help="软件版本")] = "dev",
    software_commit: Annotated[str, typer.Option("--software-commit", help="软件提交")] = "dev",
) -> None:
    """执行EFC端到端运行（读取严格版本化配置并调用RunService）"""
    try:
        from pydantic import ValidationError

        from capacity_credit.application.efc_workflow import (
            P1_EFC_INVALID_CONFIG,
            run_efc_pipeline,
        )
        from capacity_credit.contracts.efc_config import EfcApplicationConfig

        if not config_path.is_file():
            typer.echo("ERROR P1_CONFIG_MISSING: EFC config file not found")
            raise typer.Exit(code=1)
        try:
            app_config = EfcApplicationConfig.from_yaml(config_path.read_text(encoding="utf-8"))
        except (ValidationError, TypeError, ValueError) as exc:
            typer.echo(f"ERROR {P1_EFC_INVALID_CONFIG}: invalid EFC config: {exc}")
            raise typer.Exit(code=1)
        projection = run_efc_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=app_config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"run_id={projection['run_id']}")
    typer.echo(f"result_id={projection.get('result_id') or 'null'}")
    typer.echo(f"artifact_manifest_id={(projection.get('artifact_manifest_ids') or ['null'])[0]}")
    typer.echo(f"sample_count={projection.get('sample_count')}")
    typer.echo(f"risk_convergence={projection.get('risk_convergence')}")
    typer.echo(f"result_status={projection.get('result_status')}")
    typer.echo(f"lifecycle_status={projection.get('lifecycle_status')}")
    typer.echo(f"accredited_capacity_mw={projection.get('accredited_capacity_mw')}")
    if projection.get("result_id") is None:
        typer.echo("ERROR P1_NO_SAMPLES: no method_result was produced (zero samples processed)")
        raise typer.Exit(code=1)


@run_app.command("mri")
def run_mri_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    config_path: Annotated[
        Path, typer.Option(..., "--config", help="MRI应用配置YAML路径 (run-config 1.1)")
    ],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    request_id: Annotated[str, typer.Option(..., "--request-id", help="请求ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    software_version: Annotated[str, typer.Option("--software-version", help="软件版本")] = "dev",
    software_commit: Annotated[str, typer.Option("--software-commit", help="软件提交")] = "dev",
) -> None:
    """执行MRI端到端运行（读取run-config 1.1并调用RunService）"""
    try:
        from capacity_credit.application.mri_workflow import (
            P1_MRI_INVALID_CONFIG,
            MriApplicationConfig,
            run_mri_pipeline,
        )

        if not config_path.is_file():
            typer.echo("ERROR P1_CONFIG_MISSING: MRI config file not found")
            raise typer.Exit(code=1)
        try:
            app_config = MriApplicationConfig.from_yaml(config_path.read_text(encoding="utf-8"))
        except (TypeError, ValueError) as exc:
            typer.echo(f"ERROR {P1_MRI_INVALID_CONFIG}: invalid MRI config: {exc}")
            raise typer.Exit(code=1)
        projection = run_mri_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=app_config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    _echo_run_projection(projection)


@run_app.command("rmri")
def run_rmri_cmd(
    project_dir: Annotated[Path, typer.Option(..., "--project-dir", "-d", help="项目目录")],
    config_path: Annotated[
        Path, typer.Option(..., "--config", help="rMRI应用配置YAML路径 (run-config 1.1)")
    ],
    import_batch_id: Annotated[str, typer.Option(..., "--import-batch-id", help="导入批次ID")],
    data_version_id: Annotated[str, typer.Option(..., "--data-version-id", help="数据版本ID")],
    request_id: Annotated[str, typer.Option(..., "--request-id", help="请求ID")],
    created_by: Annotated[str, typer.Option(..., "--created-by", help="创建人")],
    software_version: Annotated[str, typer.Option("--software-version", help="软件版本")] = "dev",
    software_commit: Annotated[str, typer.Option("--software-commit", help="软件提交")] = "dev",
) -> None:
    """执行rMRI端到端运行（读取run-config 1.1并调用RunService）"""
    try:
        from capacity_credit.application.mri_workflow import (
            P1_MRI_INVALID_CONFIG,
            MriApplicationConfig,
            run_mri_pipeline,
        )

        if not config_path.is_file():
            typer.echo("ERROR P1_CONFIG_MISSING: rMRI config file not found")
            raise typer.Exit(code=1)
        try:
            app_config = MriApplicationConfig.from_yaml(config_path.read_text(encoding="utf-8"))
        except (TypeError, ValueError) as exc:
            typer.echo(f"ERROR {P1_MRI_INVALID_CONFIG}: invalid rMRI config: {exc}")
            raise typer.Exit(code=1)
        projection = run_mri_pipeline(
            project_dir=project_dir,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=app_config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    _echo_run_projection(projection)


def _echo_run_projection(projection: dict[str, Any]) -> None:
    typer.echo(f"run_id={projection['run_id']}")
    typer.echo(f"result_id={projection.get('result_id') or 'null'}")
    typer.echo(f"artifact_manifest_id={(projection.get('artifact_manifest_ids') or ['null'])[0]}")
    typer.echo(f"sample_count={projection.get('sample_count')}")
    typer.echo(f"risk_convergence={projection.get('risk_convergence')}")
    typer.echo(f"result_status={projection.get('result_status')}")
    typer.echo(f"lifecycle_status={projection.get('lifecycle_status')}")
    typer.echo(f"accredited_capacity_mw={projection.get('accredited_capacity_mw')}")
    if projection.get("result_id") is None:
        typer.echo("ERROR P1_NO_SAMPLES: no method_result was produced (zero samples processed)")
        raise typer.Exit(code=1)


report_app = typer.Typer(help="报告导出（T24-A 轻量 Excel/HTML 报告层）")
app.add_typer(report_app, name="report")


@report_app.command("export")
def report_export(
    input_dir: Annotated[Path, typer.Option(..., "--input-dir", help="T23-A 机器可读结果目录")],
    output_dir: Annotated[Path, typer.Option(..., "--output-dir", help="报告输出目录")],
) -> None:
    """Generate the full Excel + HTML + manifest report set from an
    already-completed T23-A result directory (no compute engine)."""
    from capacity_credit.reporting.export_service import export_report
    from capacity_credit.reporting.report_model import ReportExportError

    try:
        result = export_report(input_dir, output_dir)
    except ReportExportError as exc:
        typer.echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"report_id={result['report_id']}")
    typer.echo(f"result_status={result['result_status']}")
    typer.echo(f"is_diagnostic={result['is_diagnostic']}")
    typer.echo(f"replayed={result['replayed']}")
    typer.echo(f"output_dir={result['output_dir']}")


bundle_app = typer.Typer(
    help="复现包（T24-C）：verify_bundle 为纯校验包（不运行计算）；rerun_bundle 具备完整复现条件，可通过既有正式工作流复算"
)
report_app.add_typer(bundle_app, name="bundle")


@bundle_app.command("create")
def report_bundle_create(
    input_dir: Annotated[
        Path,
        typer.Option(
            ...,
            "--input-dir",
            help="完整成果根目录（machine-results/ + report.xlsx/html/manifest，可选 input/ 快照）",
        ),
    ],
    output_dir: Annotated[
        Path, typer.Option(..., "--output-dir", help="bundle 输出目录（必须为空或不存在）")
    ],
    bundle_type: Annotated[
        str,
        typer.Option(
            ...,
            "--bundle-type",
            help="verify_bundle 或 rerun_bundle（rerun_bundle 需要完整复现条件）",
        ),
    ],
    confirm: Annotated[
        bool, typer.Option("--confirm", help="创建 rerun_bundle 的显式确认")
    ] = False,
    confirm_by: Annotated[
        str, typer.Option("--confirm-by", help="确认人标识（rerun_bundle 必需）")
    ] = "",
    rerun_command: Annotated[
        str | None,
        typer.Option("--rerun-command", help="可复制执行的正式复算命令（rerun_bundle 必需）"),
    ] = None,
    confidentiality: Annotated[
        str,
        typer.Option(
            "--confidentiality", help="数据保密等级：internal/restricted/public_sanitized"
        ),
    ] = "internal",
    dependency_lock: Annotated[
        Path | None,
        typer.Option(
            "--dependency-lock",
            help="经验证的依赖锁文件（rerun_bundle 必需；复制进包并记录 SHA-256）",
        ),
    ] = None,
) -> None:
    """Create a reproduction bundle. verify_bundle: pure inspection
    package (machine results + Excel/HTML reports + manifest). rerun_bundle:
    additionally carries the input snapshot, seed/samples reconstruction,
    formal command, dependency identity and explicit confirmation - it can
    be rerun through the EXISTING formal workflow only. No ELCC/EFC/
    reliability computation happens at create time."""
    from capacity_credit.reporting.bundles import BundleError, create_bundle

    try:
        result = create_bundle(
            input_dir,
            output_dir,
            bundle_type,
            confirm=confirm,
            confirm_by=confirm_by,
            rerun_command=rerun_command,
            confidentiality=confidentiality,
            dependency_lock=dependency_lock,
        )
    except BundleError as exc:
        typer.echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"bundle_type={result['bundle_type']}")
    typer.echo(f"run_id={result['run_id']}")
    typer.echo(f"result_id={result['result_id']}")
    typer.echo(f"rerun_eligible={result['rerun_eligible']}")
    typer.echo(f"file_count={result['file_count']}")
    typer.echo(f"output_dir={result['output_dir']}")


@bundle_app.command("verify")
def report_bundle_verify(
    bundle_dir: Annotated[Path, typer.Option(..., "--bundle-dir", help="bundle 目录")],
) -> None:
    """Verify a reproduction bundle WITHOUT running any computation:
    manifest structure, exact file set, path safety, sizes, SHA-256,
    run/result identity against the internal machine manifests, schema
    and bundle type."""
    from capacity_credit.reporting.bundles import BundleError, verify_bundle

    try:
        result = verify_bundle(bundle_dir)
    except BundleError as exc:
        typer.echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"ok={result['ok']}")
    typer.echo(f"bundle_type={result['bundle_type']}")
    typer.echo(f"run_id={result['run_id']}")
    typer.echo(f"result_id={result['result_id']}")
    typer.echo(f"file_count={result['file_count']}")
    typer.echo(f"bundle_dir={result['bundle_dir']}")


@bundle_app.command("rerun")
def report_bundle_rerun(
    bundle_dir: Annotated[Path, typer.Option(..., "--bundle-dir", help="rerun_bundle 目录")],
    output_dir: Annotated[
        Path, typer.Option(..., "--output-dir", help="新的隔离复算输出目录（必须为空或不存在）")
    ],
) -> None:
    """Rerun a rerun_bundle through the EXISTING formal demo workflow.
    The bundle is verified first and must declare rerun eligibility;
    output goes to a NEW isolated directory and existing different
    results are never overwritten."""
    from capacity_credit.reporting.bundles import BundleError, rerun_bundle

    try:
        result = rerun_bundle(bundle_dir, output_dir)
    except BundleError as exc:
        typer.echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"run_id={result['run_id']}")
    typer.echo(f"result_id={result['result_id']}")
    typer.echo(f"output_dir={result['output_dir']}")
    typer.echo(f"source_bundle={result['source_bundle']}")


demo_app = typer.Typer(help="T24-B 业务示范（教学场景，薄编排，全部走正式生产路径）")
app.add_typer(demo_app, name="demo")

worker_app = typer.Typer(
    help="后台任务（T25）：一次性独立worker子进程+原子文件控制面；start后父进程可退出"
)
app.add_typer(worker_app, name="worker")


def _worker_error(exc: Exception) -> None:
    code = getattr(exc, "code", "WORKER_ERROR")
    typer.echo(f"ERROR {code}: {exc}")
    raise typer.Exit(code=1) from exc


@worker_app.command("start")
def worker_start(
    task: Annotated[str, typer.Option(..., "--task", help="任务类型：demo 或 adequacy")],
    jobs_dir: Annotated[Path, typer.Option(..., "--jobs-dir", help="作业状态目录")],
    requested_by: Annotated[str, typer.Option("--requested-by", help="操作人标识")] = "cli",
    config: Annotated[
        Path | None, typer.Option("--config", help="demo任务：教学配置YAML路径")
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", help="demo任务：成果输出目录")
    ] = None,
    param: Annotated[
        list[str] | None, typer.Option("--param", help="adequacy任务：KEY VALUE（可重复）")
    ] = None,
) -> None:
    """Create a job, spawn the DETACHED one-shot worker subprocess and
    return immediately (the parent process may exit; the worker keeps
    running). Structured business parameters only - never a shell
    command."""
    from capacity_credit.application import worker as worker_mod

    try:
        if task == "demo":
            if config is None or output_dir is None:
                typer.echo(
                    "ERROR WORKER_TASK_INVALID: demo task requires --config and --output-dir"
                )
                raise typer.Exit(code=1)
            params = {"config": str(config), "output_dir": str(output_dir)}
        elif task == "adequacy":
            params = {}
            for item in param or []:
                key, _, value = item.partition("=")
                if not key or not _:
                    typer.echo("ERROR WORKER_TASK_INVALID: --param expects KEY=VALUE")
                    raise typer.Exit(code=1)
                params[key] = value
        else:
            typer.echo(f"ERROR WORKER_TASK_INVALID: unknown task {task!r}")
            raise typer.Exit(code=1)
        job = worker_mod.create_job(jobs_dir, task=task, params=params, requested_by=requested_by)
        launched = worker_mod.launch(jobs_dir, job["job_id"])
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _worker_error(exc)
        return
    typer.echo(f"job_id={launched['job_id']}")
    typer.echo(f"request_id={launched['request_id']}")
    typer.echo(f"pid={launched.get('pid')}")
    typer.echo(f"state={launched.get('state')}")
    typer.echo(f"status_dir={Path(jobs_dir) / launched['job_id']}")


@worker_app.command("status")
def worker_status_cmd(
    jobs_dir: Annotated[Path, typer.Option(..., "--jobs-dir", help="作业状态目录")],
    job_id: Annotated[str, typer.Option(..., "--job-id", help="作业ID")],
) -> None:
    """PURE read-only status: job record, RunLock probe, control
    requests, progress projection, checkpoint presence. Never acquires
    a lock, starts a process, sends a request or writes any file."""
    from capacity_credit.application.worker import WorkerError, worker_status

    try:
        result = worker_status(jobs_dir, job_id)
    except WorkerError as exc:
        _worker_error(exc)
        return
    job = result["job"]
    typer.echo(f"job_id={job['job_id']}")
    typer.echo(f"state={job.get('state')}")
    typer.echo(f"task={job.get('task')}")
    typer.echo(f"run_id={job.get('run_id')}")
    typer.echo(f"lock_held={result.get('lock_held')}")
    progress = result.get("progress") or {}
    typer.echo(f"phase={progress.get('phase')}")
    typer.echo(f"current_subtask={progress.get('current_subtask')}")
    typer.echo(f"lifecycle_status={progress.get('lifecycle_status')}")
    typer.echo(f"diagnostic_code={progress.get('diagnostic_code')}")
    typer.echo(f"checkpoint_present={result.get('checkpoint_present')}")


@worker_app.command("stop")
def worker_stop_cmd(
    jobs_dir: Annotated[Path, typer.Option(..., "--jobs-dir", help="作业状态目录")],
    job_id: Annotated[str, typer.Option(..., "--job-id", help="作业ID")],
    requested_by: Annotated[str, typer.Option("--requested-by", help="操作人标识")] = "cli",
) -> None:
    """Create an IDEMPOTENT stop_safely control request; the worker
    honours it at the next SAFE boundary (adequacy batch boundary after
    the checkpoint; demo stage boundary before the ELCC compute)."""
    from capacity_credit.application.worker import stop_job

    try:
        result = stop_job(jobs_dir, job_id, requested_by=requested_by)
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _worker_error(exc)
        return
    typer.echo(f"job_id={result['job_id']}")
    typer.echo(f"request_id={result.get('request_id')}")
    typer.echo(f"request_status={result.get('request_status')}")


@worker_app.command("resume")
def worker_resume_cmd(
    jobs_dir: Annotated[Path, typer.Option(..., "--jobs-dir", help="作业状态目录")],
    job_id: Annotated[str, typer.Option(..., "--job-id", help="被中断作业ID")],
    requested_by: Annotated[str, typer.Option("--requested-by", help="操作人标识")] = "cli",
) -> None:
    """Resume a worker-interrupted adequacy run from its checkpoint:
    only a RUNNING run with NO live OS-lock owner and a matching
    checkpoint; completed/failed/cancelled are stably refused."""
    from capacity_credit.application.worker import WorkerError, resume_job

    try:
        record = resume_job(jobs_dir, job_id, requested_by=requested_by)
        launched = worker_resume_launch(jobs_dir, record["job_id"])
    except WorkerError as exc:
        _worker_error(exc)
        return
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _worker_error(exc)
        return
    typer.echo(f"job_id={launched['job_id']}")
    typer.echo(f"request_id={launched['request_id']}")
    typer.echo(f"pid={launched.get('pid')}")
    typer.echo(f"run_id={launched.get('run_id')}")


def worker_resume_launch(jobs_dir: Path, job_id: str) -> dict[str, Any]:
    from capacity_credit.application.worker import launch

    return launch(jobs_dir, job_id)


@worker_app.command("recover-terminal")
def worker_recover_terminal_cmd(
    jobs_dir: Annotated[Path, typer.Option(..., "--jobs-dir", help="作业状态目录")],
    job_id: Annotated[str, typer.Option(..., "--job-id", help="作业ID")],
) -> None:
    """Replay the FROZEN terminal intent of a job whose terminal publish
    failed: completes the remaining request settlement, terminal progress
    and the pinned terminal job state under the same control gate - never
    re-runs the runner, never re-chooses the target state."""
    from capacity_credit.application.worker import recover_terminal_job

    try:
        result = recover_terminal_job(jobs_dir, job_id)
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _worker_error(exc)
        return
    typer.echo(f"job_id={result['job_id']}")
    typer.echo(f"state={result['state']}")
    typer.echo(f"recovered={result['recovered']}")
    typer.echo(f"code={result['code']}")


@demo_app.command("run")
def demo_run(
    config: Annotated[Path, typer.Option(..., "--config", help="示范配置YAML路径")],
    output_dir: Annotated[Path, typer.Option(..., "--output-dir", help="成果物输出目录")],
) -> None:
    """Run a T24-B business demonstration end-to-end (synthetic teaching
    input -> formal import/freeze/scenario -> run_elcc_pipeline ->
    T23-A machine outputs -> T24-A report -> business analysis)."""
    from capacity_credit.demo.business_demo import DemoError, run_demo

    try:
        result = run_demo(config, output_dir)
    except DemoError as exc:
        typer.echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - CLI error mapping
        _fail(exc)
        return
    typer.echo(f"run_id={result['run_id']}")
    typer.echo(f"result_id={result['result_id'] or 'null'}")
    typer.echo(f"use_boundary_code={result['use_boundary_code']}")
    typer.echo(f"demo_replayed={result['demo_replayed']}")
    typer.echo(f"run_replayed={result['run_replayed']}")
    typer.echo(f"report_replayed={result['report_replayed']}")
    typer.echo(f"run_reused={result['run_reused']}")
    typer.echo(f"report_reused={result['report_reused']}")
    typer.echo(f"accredited_capacity_mw={result['accredited_capacity_mw']}")
    typer.echo(f"capacity_credit_fraction={result['capacity_credit_fraction']}")
    typer.echo(f"output_dir={result['output_dir']}")


if __name__ == "__main__":
    app()
