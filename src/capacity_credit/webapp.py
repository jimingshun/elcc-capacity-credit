"""Slice-① web serve layer: a localhost-only HTTP front door over the
SAME WorkbenchService facade the Streamlit shell uses.

Hard boundaries:

- stdlib ``ThreadingHTTPServer`` only - no new dependency, bound to
  127.0.0.1, the workspace is EXPLICIT (never auto-discovered);
- this layer FORWARDS: every business call goes to the existing facade
  (projects, unified result index, cost submissions, projections,
  report export/downloads); nothing here computes, reads run files or
  writes business state beyond what the facade does;
- the cost payload assembly is the SHARED boundary in
  application.cost_payload (the Streamlit form layer delegates to the
  same builders) - one percent->ratio conversion, one defaults table;
- errors are honest: a facade refusal becomes HTTP 400 with
  ``{kind, message}`` for the page to render inline - never a fake
  success, never a swallowed failure;
- reliability-domain pages are NOT wired in this slice (slice ②③):
  the frontend states that honestly instead of faking entries.

D-2 路由拆分（任务 D 工作 2，2026-09-23）：全部 /api/* 接口经
``webapp_routes`` 路由表按域注册分发，本文件只剩 HTTP 门面（静态页、
响应助手、分发与 404）；处理函数均为各域模块级函数（导入模块级，
根治旧巨型 if 链的函数内 from-import 遮蔽 bug 类）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from capacity_credit.application.import_service import ServiceError
from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.ui import runtime as ui_runtime

# D-2 路由拆分：路由表（模块级，一次装配；各域处理函数见 webapp_routes/）
from capacity_credit.webapp_routes import PREFIX_ROUTES as _WEB_PREFIX_ROUTES
from capacity_credit.webapp_routes import ROUTES as _WEB_ROUTES

_WEB_ROUTE_MAP = {(m, p): fn for m, p, fn in _WEB_ROUTES}
_WEB_PREFIX_ROUTE_LIST = sorted(
    _WEB_PREFIX_ROUTES, key=lambda item: len(item[1]), reverse=True
)

_STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "ElccWebServe/0.1"
    workspace: Path = Path(".")

    def log_message(self, *args: Any) -> None:  # noqa: D102 - quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        self._route("POST")

    # ---- response helpers -------------------------------------------------
    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(
        self, data: bytes, content_type: str, filename: str
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        safe = filename.replace('"', "")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{safe}"'
        )
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    # ---- routing ----------------------------------------------------------
    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._serve_index()
            if method == "GET" and path.startswith("/assets/"):
                return self._serve_asset(path)
            if path.startswith("/api/"):
                return self._api(method, path, qs)
            self._json(404, {"kind": "not_found", "message": f"未知路径 {path}"})
        except WorkbenchError as exc:
            self._json(400, {"kind": exc.kind, "message": exc.message})
        except ServiceError as exc:
            # the data-chain facade delegates to ImportService, whose
            # business refusals (unknown batch, invalid state) surface
            # here - translated to the SAME honest 400 shape
            self._json(400, {"kind": exc.code, "message": exc.message})
        except (ValueError, TypeError) as exc:
            self._json(400, {"kind": "invalid_input", "message": str(exc)})

    def _serve_index(self) -> None:
        index = _STATIC_DIR / "index.html"
        if not index.is_file():
            return self._json(
                500, {"kind": "server", "message": "前端文件缺失"}
            )
        # INLINE render (never an attachment): Content-Disposition must
        # stay off, or the browser downloads the page instead of showing
        # the workbench.
        body = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # the SPA loads its code ONCE, so a stale cached index hides a
        # deployed update behind a plain refresh - never cache it
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    _ASSET_TYPES = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }

    def _serve_asset(self, path: str) -> None:
        """D-1（2026-09-24）：index.html 拆分后的静态资产只读供给。
        解析必须落在 assets 目录内（拒绝穿越）；类型按后缀白名单；
        no-store 与 index 同纪律（SPA 代码只加载一次，缓存会让
        部署后的更新不可见）。未知文件与非白名单后缀一律 404。"""
        assets_root = (_STATIC_DIR / "assets").resolve()
        target = (assets_root / path[len("/assets/") :]).resolve()
        if target != assets_root and assets_root not in target.parents:
            return self._json(
                400, {"kind": "invalid_input", "message": "非法资产路径"}
            )
        ctype = self._ASSET_TYPES.get(target.suffix.lower())
        if not target.is_file() or ctype is None:
            return self._json(
                404, {"kind": "not_found", "message": f"未知资产 {path}"}
            )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- API --------------------------------------------------------------
    def _service(self) -> Any:
        return ui_runtime.get_workbench_service(self.workspace)

    def _api(self, method: str, path: str, qs: dict[str, list[str]]) -> None:
        # D-2 路由拆分（2026-09-23 用户审核确认）：全部接口经
        # webapp_routes 路由表分发（处理函数为各域文件的模块级函数）。
        # POST 的 body 只在命中路由表时才读取，并统一代读传入处理函数
        # （处理函数不再各自 _read_json，互不抢读）。
        handler = _WEB_ROUTE_MAP.get((method, path))
        if handler is None:
            # 前缀路由（如 /api/check/<kind>）：精确路由优先
            for pmethod, prefix, fn in _WEB_PREFIX_ROUTE_LIST:
                if method == pmethod and path.startswith(prefix):
                    handler = fn
                    break
        if handler is not None:
            body = self._read_json() if method == "POST" else None
            return handler(self, qs, body)
        self._json(404, {"kind": "not_found", "message": f"未知API {path}"})


def make_server(workspace_root: Path | str, port: int) -> ThreadingHTTPServer:
    """Bind the localhost-only server (port 0 picks a free one for
    tests); the handler class carries the resolved workspace."""
    handler = type("BoundHandler", (_Handler,), {})
    handler.workspace = Path(workspace_root).resolve()
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(workspace_root: Path | str, port: int = 8600) -> None:
    """Run the web front door in-process (the CLI `serve` command lands
    here; no execv, no second interpreter)."""
    httpd = make_server(workspace_root, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"容量补偿（市场）参数测算及仿真工具 · Web入口 {url}")
    print(f"工作区：{Path(workspace_root).resolve()}")
    print("仅本机可访问（127.0.0.1）。按 Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


__all__ = ["make_server", "serve"]
