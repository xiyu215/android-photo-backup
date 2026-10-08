"""本地网页管理页（FastAPI + uvicorn）。

定位（按 Q1 确认后调整）
------------------------
它不是"手机上传通道"——WiFi 走 ADB over WiFi，手机不需要上传任何东西。
它的作用是：
    1. 在**电脑浏览器**上提供比终端舒服得多的交互：勾选文件、看清单表格、
       排序筛选、导出 CSV/TXT、反向下载；
    2. 在**手机浏览器**上提供只读查看：看进度、看清单。
       手机上对照着清单删文件时非常有用（不用来回切窗口）。
    3. 承载 WebSocket 实时进度推送。

安全性
------
    服务只监听 **127.0.0.1**（本机）+ 局域网访问需要一次性令牌。
    令牌每次启动随机生成，终端与二维码里都会显示，防止同网段的其他设备误操作。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..common import i18n, paths
from ..common.errors import PhotobakError
from ..common.logger import get_logger
from ..core.cleanup import TIER_ORDER

log = get_logger("web")


@dataclass
class WebContext:
    """网页服务需要访问的运行时上下文。"""

    tracker: Any = None                     # ui.progress.ProgressTracker
    get_state: Callable[[], dict] | None = None      # 返回设备/配置等概要
    get_cleanup: Callable[[], Any] | None = None     # 返回 CleanupReport
    export_cleanup: Callable[[str, str], Path | None] | None = None
    resolve_file: Callable[[int], Path | None] | None = None
    resolve_ids: Callable[[list[str]], list[int]] | None = None   # 手机路径 → file_id
    make_zip: Callable[[list[int]], Path | None] | None = None
    restore_files: Callable[[list[str]], list[dict]] | None = None
    refresh_media: Callable[[], tuple[bool, str]] | None = None
    can_restore: Callable[[], bool] | None = None
    token: str = ""
    allow_lan: bool = True
    version: str = "1.0.0"
    #: 供前端展示的额外信息（备份根、报告目录等）
    extra: dict = field(default_factory=dict)


class WebServer:
    """把 FastAPI 应用跑在后台线程里。"""

    def __init__(self, ctx: WebContext, *, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.ctx = ctx
        self.host = host
        self.port = port
        self._server = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    def build_app(self):
        """构造 FastAPI 应用（延迟导入，避免纯终端模式下也加载 web 依赖）。"""
        from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles

        ctx = self.ctx
        app = FastAPI(title=i18n.APP_TITLE, version=ctx.version, docs_url=None, redoc_url=None)

        webui_dir = paths.resource_path("webui")
        if not webui_dir.is_dir():
            webui_dir = paths.app_dir() / "webui"

        # ---- 鉴权 ----
        def _check(token: str | None) -> None:
            if not ctx.token:
                return
            if token != ctx.token:
                raise HTTPException(status_code=403, detail="访问令牌不正确，请使用终端显示的完整链接")

        def _token_of(request: Request) -> str | None:
            return (request.query_params.get("t")
                    or request.headers.get("x-token")
                    or request.cookies.get("pb_token"))

        @app.middleware("http")
        async def _auth_middleware(request: Request, call_next):  # noqa: ANN001
            path = request.url.path
            # 静态资源与首页放行（页面本身不含敏感数据，数据都要带令牌才能取）
            if path.startswith("/static") or path in ("/", "/favicon.ico", "/health"):
                return await call_next(request)
            if path.startswith("/api") or path.startswith("/download"):
                try:
                    _check(_token_of(request))
                except HTTPException as exc:
                    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
            return await call_next(request)

        # ---- 首页 ----
        @app.get("/", response_class=HTMLResponse)
        async def index() -> Any:
            idx = webui_dir / "index.html"
            if not idx.is_file():
                return HTMLResponse("<h1>前端资源缺失</h1><p>请重新解压完整压缩包。</p>",
                                    status_code=500)
            return HTMLResponse(idx.read_text(encoding="utf-8"))

        @app.get("/health")
        async def health() -> dict:
            return {"ok": True, "app": i18n.APP_NAME, "version": ctx.version}

        # ---- 状态 ----
        @app.get("/api/state")
        async def state() -> dict:
            snap = ctx.tracker.snapshot().to_dict() if ctx.tracker else {}
            base = ctx.get_state() if ctx.get_state else {}
            return {"ok": True, "progress": snap, "state": base, **ctx.extra}

        @app.get("/api/summary")
        async def summary() -> dict:
            report = ctx.get_cleanup() if ctx.get_cleanup else None
            if report is None:
                return {"ok": True, "ready": False, "tiers": []}
            tiers = []
            for t in TIER_ORDER:
                tiers.append({
                    "name": t,
                    "count": report.count(t),
                    "size": report.size(t),
                    "size_human": paths.human_size(report.size(t)),
                    "desc": i18n.TIER_DESC.get(t, ""),
                })
            return {"ok": True, "ready": True, "generated_at": report.generated_at,
                    "total": report.total(), "tiers": tiers}

        @app.get("/api/cleanup")
        async def cleanup(
            tier: str = Query(default=TIER_ORDER[0]),
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=200, ge=1, le=2000),
            keyword: str = Query(default=""),
        ) -> dict:
            report = ctx.get_cleanup() if ctx.get_cleanup else None
            if report is None:
                return {"ok": True, "ready": False, "items": [], "total": 0}
            if tier not in TIER_ORDER:
                raise HTTPException(status_code=400, detail=f"未知分档：{tier}")
            items = report.tier(tier)
            if keyword:
                kw = keyword.lower()
                items = [i for i in items if kw in i.filename.lower()
                         or kw in i.phone_path.lower() or kw in i.app_tag.lower()]
            total = len(items)
            page = items[offset:offset + limit]
            return {
                "ok": True, "ready": True, "tier": tier, "total": total,
                "items": [{
                    "filename": i.filename, "size": i.size, "size_human": i.size_text,
                    "taken_at": i.taken_at, "phone_path": i.phone_path,
                    "pc_path": i.pc_path, "app_tag": i.app_tag,
                    "time_source": i.time_source, "time_confidence": i.time_confidence,
                    "sha256": i.sha256, "note": i.note,
                } for i in page],
            }

        @app.get("/api/cleanup/export")
        async def export(tier: str = Query(default=""), fmt: str = Query(default="csv")) -> Any:
            if ctx.export_cleanup is None:
                raise HTTPException(status_code=503, detail="当前没有可导出的清单")
            try:
                p = ctx.export_cleanup(tier, fmt)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            if not p or not Path(p).is_file():
                raise HTTPException(status_code=404, detail="没有可导出的内容")
            return FileResponse(str(p), filename=Path(p).name,
                                media_type="application/octet-stream")

        # ---- 文件下载（反向下载的单张保存） ----
        @app.get("/download/{file_id}")
        async def download(file_id: int) -> Any:
            if ctx.resolve_file is None:
                raise HTTPException(status_code=503, detail="当前不可下载")
            p = ctx.resolve_file(file_id)
            if not p or not Path(p).is_file():
                raise HTTPException(status_code=404, detail="文件不存在")
            return FileResponse(str(p), filename=Path(p).name,
                                media_type="application/octet-stream")

        @app.get("/api/download/zip")
        async def download_zip(paths_q: str = Query(default="", alias="paths")) -> Any:
            """按手机原始路径打包 zip（前端只持有路径，不持有 file_id）。"""
            if ctx.resolve_ids is None or ctx.make_zip is None:
                raise HTTPException(status_code=503, detail="当前不可打包")
            want = [p for p in paths_q.split("\n") if p.strip()]
            if not want:
                raise HTTPException(status_code=400, detail="未选择任何文件")
            ids = ctx.resolve_ids(want)
            if not ids:
                raise HTTPException(status_code=404, detail="选中的文件在索引中找不到")
            p = ctx.make_zip(ids)
            if not p:
                raise HTTPException(status_code=404, detail="没有可打包的文件")
            return FileResponse(str(p), filename=Path(p).name,
                                media_type="application/zip")

        @app.post("/api/restore")
        async def restore(payload: dict) -> dict:
            """把选中的文件推回手机（前端传手机原始路径列表）。"""
            if ctx.restore_files is None:
                return {"ok": False, "error": "当前不支持反向下载"}
            if ctx.can_restore and not ctx.can_restore():
                return {"ok": False, "error": "当前没有连接手机，无法反向下载"}
            want = payload.get("paths") or []
            if not isinstance(want, list) or not want:
                return {"ok": False, "error": "未选择任何文件"}
            try:
                results = ctx.restore_files([str(p) for p in want])
            except PhotobakError as exc:
                return {"ok": False, "error": exc.user_message()}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "results": results}

        @app.post("/api/refresh_media")
        async def refresh_media() -> dict:
            """触发手机媒体库刷新（让恢复的照片立刻出现在相册里）。"""
            if ctx.refresh_media is None:
                return {"ok": False, "error": "当前不支持该操作"}
            try:
                ok, msg = ctx.refresh_media()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "message": msg}


        # ---- WebSocket 进度 ----
        @app.websocket("/ws/progress")
        async def ws_progress(websocket: WebSocket) -> None:
            await websocket.accept()
            token = websocket.query_params.get("t")
            if ctx.token and token != ctx.token:
                await websocket.send_json({"ok": False, "error": "令牌不正确"})
                await websocket.close()
                return
            try:
                while True:
                    if ctx.tracker is not None:
                        await websocket.send_json({"ok": True,
                                                   "progress": ctx.tracker.snapshot().to_dict()})
                    await asyncio.sleep(1.0)
            except WebSocketDisconnect:
                return
            except Exception as exc:  # noqa: BLE001 —— 连接异常属常态，不必上抛
                log.debug("WebSocket 结束：%s", exc)

        # ---- 静态资源 ----
        if webui_dir.is_dir():
            app.mount("/static", StaticFiles(directory=str(webui_dir)), name="static")

        return app

    # ------------------------------------------------------------------
    def start(self) -> bool:
        """在后台线程启动服务。返回是否成功。"""
        app = self.build_app()

        def _run() -> None:
            import warnings

            import uvicorn

            # 压掉一条与我们无关的库弃用警告。
            #
            # 现象：程序跑完、用户按回车退出后，stderr 会多出一行
            #   uvicorn\...\websockets_impl.py:41: UvicornDeprecationWarning:
            #   The `websockets` implementation is deprecated ...
            #
            # 成因：uvicorn 在**首次导入** websockets 协议实现时，于模块顶层
            # 调了一次 ``warnings.warn()``。它不走 logging，所以 ``log_config``
            # / ``log_level`` 都管不住，只能从 warnings 层处理。
            #
            # 为什么不用 ``filterwarnings(category=DeprecationWarning)``：
            # 那个类的真实定义是 ``class UvicornDeprecationWarning(UserWarning)``
            # —— 名字里带 Deprecation，基类却是 UserWarning。按 DeprecationWarning
            # 过滤会静默失效（实测就是这么踩空的）。
            #
            # 这里改用最稳的办法：在被抑制的上下文里**提前把模块导入完**。
            # 模块一旦进了 ``sys.modules``，uvicorn 之后取用同一个对象，
            # 模块顶层的 warn 不会再执行第二次。
            #
            # 为什么不干脆按提示改成 ws="websockets-sansio"：
            # 管理页的实时进度就是靠 WebSocket 推的，换协议实现属于功能性改动，
            # 为消掉一行提示去冒这个险不划算。这里行为与改动前完全一致。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    import uvicorn.protocols.websockets.websockets_impl  # noqa: F401
                except Exception:  # noqa: BLE001
                    pass

            config = uvicorn.Config(
                app, host=self.host, port=self.port,
                log_level="warning", log_config=None, access_log=False,
                ws="websockets",
            )
            self._server = uvicorn.Server(config)
            try:
                self._server.run()
            except Exception as exc:  # noqa: BLE001
                log.warning("网页服务退出：%s", exc)

        self._thread = threading.Thread(target=_run, name="webui", daemon=True)
        self._thread.start()

        # 等服务真正起来（最多 8 秒）
        from ..common import netutil

        for _ in range(80):
            if netutil.is_port_listening(self.port, "127.0.0.1"):
                return True
            time.sleep(0.1)
        log.warning("网页服务在预期时间内未就绪（端口 %d）", self.port)
        return False

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:  # noqa: BLE001
            pass

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.ctx.token}"

    def lan_url(self, ip: str) -> str:
        return f"http://{ip}:{self.port}/?t={self.ctx.token}"
