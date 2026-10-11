# reporting模块

## 目录用途
将正式机器结果映射为共享报告视图、Excel、HTML和后续业务分析。

## 包含什么
唯一字段映射、机器产物、ReportView、导出和模板。

## 不应放什么
风险重算、第二套显示语义、测试夹具读取或手工补造结论。

## 主要输入
T23机器结果、正式运行身份和报告配置。

## 主要输出
机器可读产物、Excel、HTML、导出清单、复现包（bundles.py）和后续固定业务成果包。

## 关键入口文件或命令

从已完成的 T23-A 机器结果目录生成 Excel/HTML 与导出清单（正式 CLI，Windows PowerShell）：

```powershell
.\ .venv\Scripts\elcc-tool.exe report export `
  --input-dir "D:\AImodels\elcc-capacity-credit-workspace\<项目>\machine-results\<run_id>" `
  --output-dir "D:\AImodels\elcc-capacity-credit-workspace\<项目>\reports\<run_id>"
```

输出：report.xlsx、report.html、report_export_manifest.json。

复现包（T24-C，bundles.py 唯一共享实现）：`report bundle create/verify/rerun`。
verify_bundle 为纯校验包（不运行计算）；rerun_bundle 要求完整复现条件并只调用既有正式工作流。
Excel/HTML 报告含增强图表（风险对比/容量价值/搜索轨迹），数据来自同一 ReportView。
约束：输入必须是完整、已完成的 T23-A 机器结果目录；报告导出不重新运行 ELCC/EFC/MRI
计算；项目正式/教学成果进入仓库外 workspace；Git 仓库中的 tmp/output 不得作为正式
业务成果目录。T24-B Demo 和 T26 UI 消费本层公共模型。

## 与其他目录的关系
只消费正式结果；业务解释不得反向修改结果。 总体模块地图见[包说明](../README.md)。

## 修改后影响哪些测试
报告映射、机器输出、成果包、重放、空值和业务验收测试。 具体节点由[测试影响映射](../../../docs/testing/README.md)选择。

## Git提交规则
生产变更必须带所属领域最小反例；不混入无关格式化、生成结果或平行实现。

## 保留、归档和清理规则
本目录不保存运行实例、缓存、历史副本或用户成果。
