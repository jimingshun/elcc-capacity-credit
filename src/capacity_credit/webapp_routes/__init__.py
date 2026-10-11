"""Web 路由表（任务 D 工作 2，2026-09-23）：webapp.py 的处理函数注册处。

每个域模块在项目/数据/运行/批次/成本/结果一个文件，处理函数是模块级
函数 ``fn(h, qs, body)``——``h`` 是 webapp._Handler 实例（复用其
_service/_json/_bytes/_read_json 与 workspace），``qs`` 是查询串，
``body`` 是 POST 的已解析 JSON（GET 恒为 None）。

历史根治面：旧巨型 ``_api`` 函数里的函数级 ``from ... import`` 会遮蔽
整个共享函数的同名绑定（两次真实事故：预览/发起全失效）。本包所有导入
一律模块级，从结构上消灭该现场。
"""

from __future__ import annotations

from typing import Any, Callable

# (method, path, handler)；注册即入表，顺序即文档
ROUTES: list[tuple[str, str, Callable[..., Any]]] = []
# (method, path_prefix, handler)：前缀路由（如 /api/check/<kind> 的分级检查）
PREFIX_ROUTES: list[tuple[str, str, Callable[..., Any]]] = []


def route(method: str, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(h, qs, body)`` as the handler for (method, path)."""

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES.append((method, path, fn))
        return fn

    return _deco


def route_prefix(
    method: str, path_prefix: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(h, qs, body)`` for a path PREFIX (e.g. graded checks
    under /api/check/<kind>). Exact routes win over prefixes."""

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        PREFIX_ROUTES.append((method, path_prefix, fn))
        return fn

    return _deco


# 域模块导入即注册（新增接口＝在对应域文件加函数，webapp.py 零改动）
from capacity_credit.webapp_routes import (  # noqa: E402,F401
    batches,
    cost,
    data,
    market,
    projects,
    results,
    runs,
    settlement,
)

__all__ = ["PREFIX_ROUTES", "ROUTES", "route", "route_prefix"]
