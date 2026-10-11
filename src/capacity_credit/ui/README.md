# capacity_credit.ui

Web 后端共用的桥接与投影小零件（2026-09-23 起）。Streamlit 业务壳层
（app/forms/style/views 等）已按用户裁决删除，界面唯一入口是 web 服务
（`cli serve` ＋ `webapp_static/`）。

| 模块 | 职责 |
|---|---|
| `viewmodel.py` | 纯投影层：服务事实 → 页面模型/文案；无 Streamlit、无 IO 副作用（webapp_routes 与静态页共用） |
| `runtime.py` | 共享运行时/服务缓存（webapp 与各域处理函数经它取 WorkbenchService/LocalRuntime） |

保留纪律：本包不得 import streamlit（静态断言钉住）、不得 import
engine/solvers/resources；界面语义改动去 `webapp_static/`，本包只做事实投影。
本期不挪位不改名（爆炸半径控制）。
