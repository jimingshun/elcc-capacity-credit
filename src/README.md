# src目录

## 目录用途
保存正式可安装Python包，不承担测试夹具或运行结果职责。

## 包含什么
[capacity_credit](capacity_credit/README.md)及安装产生的本地元数据目录。

## 不应放什么
测试代码、真实数据、报告成果、缓存、临时脚本或复制的外部求解器。

## 主要输入
P0主责设计、软件实施计划和版本化契约。

## 主要输出
CLI、应用服务、可靠性内核、容量信用求解和报告能力。

## 关键入口文件或命令
`capacity_credit/cli.py`及`elcc-tool`命令（正式 UI＝`elcc-tool serve`/`python -m capacity_credit.cli serve`）。

## 与其他目录的关系
`tests/`证明行为，`schemas/`保存机器合同，`docs/`定义边界。

## 修改后影响哪些测试
必须先用`tools/test_governance.py select`选择受影响节点。

## Git提交规则
生产代码与最小反例同提交；不混入生成结果或无关格式化。

## 保留、归档和清理规则
`capacity_credit.egg-info`是本地安装元数据，不作为业务成果。
