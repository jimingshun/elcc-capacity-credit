# capacity_credit包

## 目录用途
ELCC、EFC、MRI/rMRI工具的唯一正式Python实现（含成本三方法与 web 服务层）。

## 包含什么
`application`、`benchmark`、`contracts`、`costs`、`data`、`demo`、`domain`、`engine`、`regression`、`reporting`、`resources`、`scenarios`、`solvers`、`ui`（Streamlit 兜底）、`cli.py`、`webapp.py`＋`webapp_static/`（正式 Web 服务门面与前端页面）。

## 不应放什么
第二套状态机、重复Schema、测试私有入口、用户工作区或PRAS运行时。

## 主要输入
标准数据、冻结配置、场景库和公共应用服务请求。

## 主要输出
版本化机器结果、运行制品、报告视图和审计信息。

## 关键入口文件或命令
`cli.py`是命令入口；业务和UI只调用`application/`公共服务。

## 与其他目录的关系
各一级模块README说明局部职责；跨层约束以架构契约为准。

## 修改后影响哪些测试
按[测试治理入口](../../docs/testing/README.md)和影响映射选择。

## Git提交规则
先最小反例、后最小实现；共享抽象只保留一套。

## 保留、归档和清理规则
不在包内保存运行产物、历史副本或弃用模块备份。
