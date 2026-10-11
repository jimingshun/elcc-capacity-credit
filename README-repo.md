# ELCC容量信用工具

目标：打开本机界面，输入数据，选择充裕度、ELCC或EFC，查看真实计算进展，取得结果与分析报告。

**当前阶段（2026-09-20）：单主干 `codex/elcc-v1`＋短命主题线模型**（[09-19 整改计划](docs/plans/2026-09-19-git-branch-consolidation-plan.md) §1 五层裁决）；历史分支已归档为 `archive/*` tag（16 个），唯 t26a 两个原语候选分支保留且未合并（不能根据提交标题或历史绿灯判定已通过独立语义复评）。**正式 UI＝web 服务**：`.venv\Scripts\python -m capacity_credit.cli serve --workspace "<业务工作区>" --port 8632`，浏览器访问 http://127.0.0.1:8632（代码默认端口 8600，正式实例显式指定）；Streamlit 兜底壳层已删除（2026-09-23），Web 服务是唯一界面入口。最新交付：ELCC/EFC 方法对齐整改（评审通过）、四模块统一项目模型与六步流程（P-A~P-G）、标定页五段式（M2）、季节缩放（SL-01）、气象年 W1 覆盖投影、P0 research_point 豁免（3d05627，ELCC/EFC 项目标定全通）、P-G HTTP 验收 20/20。**当前路线与任务状态见[核心豁免工单与适配路线图](docs/plans/2026-09-20-core-exemption-work-order.md)**（A 核心豁免已完成；B 文档梳理、C UI 适配进行中；D 结构债、E 再授权门待放行）；V1 计划 §5.8—§5.11 均为历史交接。储能作 ELCC/EFC 候选仍 BLOCKED_SCOPE；P5 仍 BLOCKED、P6 未验收；结算（P-E）与市场出清内核未实施（再授权门）。

本文件是唯一当前总入口。不要从历史T阶段、候选分支旧README或归档中的“下一步”自行恢复开发。

## 当前执行入口

- 本机启动（正式）：`python -m capacity_credit.cli serve --workspace "<工作区路径>" --port 8632`（工作区必须显式给出；仅绑定 127.0.0.1）。已提供：项目工作台（11 类项目创建/类型锁/一项目一正式结果）、数据页（模板下载/导入/冻结/场景库）、成本三方法（固定成本/Gross CONE/Net CONE）、充裕度/ELCC/EFC 真实测算、六步 ELCC/EFC 模块（含五段式基准标定）、结果与报告（Excel/HTML/PDF 打印）、跨项目聚合表。
- [核心豁免工单与适配路线图](docs/plans/2026-09-20-core-exemption-work-order.md)：**当前唯一活跃任务文档**（A→E 路线、裁决记录、任务 C/D/E 范围）。
- [文档梳理报告](docs/plans/2026-09-20-docs-reorg-survey-report.md)：全仓文档现状判定与裁决记录（R1—R8）。
- [ELCC-V1实施计划](docs/plans/2026-09-06-elcc-v1-implementation-plan.md)：V1 范围与验收矩阵的历史依据（T1—T5 已完结；§5.8—§5.11 全部为历史交接记录，文件内最新为 §5.11，内容止于 2026-09-15；旧"唯一当前交接"声明作废）。
- [V1应用架构D1](docs/architecture/2026-09-06-elcc-v1-application-architecture.md)：已确认架构（§1-§10 原则现行；第 11 节为已交付的 T1-A 历史设计记录）。
- [分支与恢复映射](docs/plans/2026-08-30-phase-branch-baseline-map.md)：唯一分支登记（含 09-19/09-20 三轮归档记录）。
- [目录地图](DIRECTORY_MAP.md)：源码、业务工作区与归档位置。
- [旧T01—T27封存索引](docs/archive/implementation/T01-T27/README.md)：旧实施计划与逐项进度盘点，仅作追溯；2026-09 夜间工单与双分支计划见[归档目录](docs/archive/plans/2026-09-night-orders/README.md)。

## V1需要交付什么

1. 本机启动与工作区/项目选择。
2. 模板数据导入、少量参数填写、错误定位与口径确认。
3. 充裕度/ELCC/EFC选择及真实阶段、耗时展示。
4. 结果、图表、Excel/HTML分析报告及历史结果打开。
5. 简明使用说明与完整用户流程验收。

一次一个任务。后台常驻、恢复、并发、高级批量研究和便携发行后置；执行方式仍须V1-T1证明，不预先宣称简化方案已绕开现有生命周期问题。

## 保持有效的规范

| 职责 | 唯一依据 |
|---|---|
| 公式、风险和方法结果 | [方法学](docs/methodology/2026-08-03-capacity-credit-methodology-design.md) |
| 字段、单位、时间轴、来源、数据等级 | [数据契约](docs/data/2026-08-03-input-data-contract-and-dictionary.md) |
| 审批、冻结、状态、锁、身份与产物 | [应用服务契约](docs/architecture/2026-08-04-application-service-and-run-artifact-contract.md) |
| 界面状态及服务边界 | [界面设计](docs/ui/2026-08-03-local-tool-interface-and-workflow-design.md)，全量页面范围由V1计划限定 |
| 项目类型、六步流程与验收矩阵 | [四模块统一设计](docs/plans/2026-09-16-unified-project-model-and-flow-design.md)及[验收矩阵](docs/plans/2026-09-16-four-module-acceptance-matrix-and-dev-prompt.md) |
| 派生数据与标定口径（DR/CAL） | [确认清单](docs/plans/2026-09-16-derived-data-and-calibration-confirmation-checklist.md) |
| 测试目录、影响选择 | [测试治理](docs/testing/README.md)与[验证方案](docs/validation/2026-08-03-test-and-validation-design.md) |
| 业务成果、数据与发布边界 | [业务治理](docs/plans/2026-08-28-business-outcomes-and-project-governance-design.md)、[核心产品范围](docs/architecture/2026-08-29-core-product-scope-and-deferred-pras-storage-validation.md) |
| 结果与报告 | [报告模块](src/capacity_credit/reporting/README.md) |

旧完整产品规划和延期后台架构只在各自适用范围内保留，不是新的V1全量开发前置。遇到实际公式/字段/状态冲突，应定位主责规范，不在界面另造实现。

## 验证状态与限制

- 旧T24、T25关闭事实保留；T23-A已有通过证据，T23整体仍受P5约束。
- P5仍BLOCKED，P6未验收；P-03/PRAS储能对照及其他开放项未因重编号关闭。
- Python储能保持experimental/provisional。界面可用、教学出数、Git基线均不证明正式发布或真实省级适用。
- [P5机器事实源](docs/validation/p5-validation-evidence.json)不由人工文字覆盖；[P5报告](docs/validation/p5-validation-report.md)、[P6报告](docs/validation/p6-validation-report.md)保留既有证据。
- 已知红测试基线见[known-red-baseline.json](docs/testing/known-red-baseline.json)（2026-09-21 实测销账后 24 项；golden 族 9 项仍红＝生产文件哈希漂移，再生成属已批准的独立清理项目）。
- 最近授权范围内的开发测试与真实浏览器/HTTP验收见各工作令与[验证目录](docs/validation/README.md)；未运行P-01/02/03、PRAS、统计、性能、完整核心回归、真实EFC重放及P6长门。历史证据从[档案索引](docs/archive/INDEX.md)按需读取。

## 工作与交付位置

源码：D:/AImodels/elcc-capacity-credit-tool。
输入、运行和用户报告：D:/AImodels/elcc-capacity-credit-workspace。
Git bundle及历史原始证据：D:/AImodels/elcc-capacity-credit-archive。

开发入口与执行限制见[AGENTS](AGENTS.md)。当前为文档梳理与 UI 适配阶段：活跃任务以[核心豁免工单](docs/plans/2026-09-20-core-exemption-work-order.md)的 A→E 路线为准；T1—T5、各夜历史交接由Git历史与归档目录保存，全部历史线已合入主干并归档为 tag（t26a 两候选除外）。
