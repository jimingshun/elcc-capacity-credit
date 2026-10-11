# application模块

## 目录用途
编排导入、审批/冻结、场景、运行、锁、恢复、协调、控制请求和制品发布等正式用例。

## 包含什么
公共应用服务、持久化边界、审计和运行协调实现。

T25新增：`worker.py`（一次性独立后台worker子进程 + 作业记录/进度投影/停止恢复编排，复用RunService/RunLock/检查点，不建立第二套生命周期或锁）与`contracts/control_requests.py`（原子控制请求唯一合同，Schema见`schemas/control-request-1.0.json`）。控制请求持久化唯一入口为`run_service.ControlRequestStore`。

## 不应放什么
求解公式副本、UI组件、测试专用捷径或第二套状态机。

## 主要输入
契约对象、标准数据、运行请求和公共领域服务。

## 主要输出
不可变运行记录、机器结果、制品、状态和诊断。

## 关键入口文件或命令
CLI、后续Demo和UI必须从本层公共接口进入。

## 与其他目录的关系
调用contracts/data/scenarios/resources/engine/solvers/reporting，不反向依赖UI。 总体模块地图见[包说明](../README.md)。

## 修改后影响哪些测试
工作流、运行生命周期、锁、恢复、资源运行和正式入口集成测试。 具体节点由[测试影响映射](../../../docs/testing/README.md)选择。

## Git提交规则
生产变更必须带所属领域最小反例；不混入无关格式化、生成结果或平行实现。

## 保留、归档和清理规则
本目录不保存运行实例、缓存、历史副本或用户成果。
