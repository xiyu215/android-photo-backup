"""系统托盘图标（用户 Q15 明确要求）。

行为
----
    - 双击托盘图标 → 重新打开/聚焦本地管理页；
    - 右键菜单 → 打开管理页 / 打开备份目录 / 打开报告目录 / 退出；
    - 点窗口关闭按钮 → 默认最小化到托盘而不是退出（可在配置里关掉）。

实现取舍
--------
用 pystray + Pillow。这两者会让打包体积增加约 8MB（压缩后），
换来的是成熟的跨平台托盘实现，比用 ctypes 手写 ``Shell_NotifyIcon``
可靠得多（后者在通知区域重建、DPI 缩放等场景下坑很多）。

若 pystray 不可用（例如用户在纯源码模式下没装依赖），本模块会静默降级：
所有方法变成空操作，程序照常运行，只是没有托盘。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..common import i18n, paths
from ..common.logger import get_logger

log = get_logger("tray")

try:  # pragma: no cover - 环境相关
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore

    _AVAILABLE = True
except Exception:  # noqa: BLE001 —— 缺依赖时降级，不影响主功能
    _AVAILABLE = False
    pystray = None  # type: ignore


def _load_icon_image(size: int = 64):
    """加载图标；找不到 ico 文件时现场画一个（保证托盘一定能显示）。"""
    ico = paths.resource_path("assets", "icon.ico")
    if ico.is_file():
        try:
            return Image.open(ico).convert("RGBA").resize((size, size))
        except Exception as exc:  # noqa: BLE001
            log.debug("读取图标失败，改用内置绘制：%s", exc)

    # 兜底：画一个"相册"风格的图标（圆角方块 + 山峰 + 太阳）
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 6, size - 3, size - 7], radius=8,
                        fill=(38, 132, 255, 255), outline=(20, 90, 200, 255), width=2)
    d.polygon([(8, size - 12), (size * 0.42, size * 0.45), (size * 0.68, size - 12)],
              fill=(255, 255, 255, 235))
    d.polygon([(size * 0.48, size - 12), (size * 0.72, size * 0.55), (size - 8, size - 12)],
              fill=(230, 245, 255, 225))
    d.ellipse([size - 22, 12, size - 12, 22], fill=(255, 226, 120, 255))
    return img


class TrayIcon:
    """托盘图标封装。所有方法在依赖缺失时都是安全的空操作。"""

    def __init__(
        self,
        *,
        on_open_page: Callable[[], None] | None = None,
        on_open_backup: Callable[[], None] | None = None,
        on_open_reports: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        title: str | None = None,
    ) -> None:
        self.title = title or f"{i18n.APP_NAME} - {i18n.APP_TITLE}"
        self._on_open_page = on_open_page
        self._on_open_backup = on_open_backup
        self._on_open_reports = on_open_reports
        self._on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return _AVAILABLE

    def start(self) -> bool:
        """启动托盘（在后台线程里跑事件循环）。"""
        if not _AVAILABLE:
            log.info("未安装 pystray/Pillow，跳过托盘图标（不影响备份功能）")
            return False
        if self._started:
            return True

        try:
            menu = pystray.Menu(
                pystray.MenuItem("打开管理页", self._wrap(self._on_open_page), default=True),
                pystray.MenuItem("打开备份目录", self._wrap(self._on_open_backup)),
                pystray.MenuItem("打开报告目录", self._wrap(self._on_open_reports)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出程序", self._wrap(self._on_quit)),
            )
            self._icon = pystray.Icon(
                "photobak", _load_icon_image(), self.title, menu,
            )
            self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self._thread.start()
            self._started = True
            log.info("托盘图标已启动")
            return True
        except Exception as exc:  # noqa: BLE001 —— 托盘失败绝不能影响备份
            log.warning("托盘图标启动失败（不影响备份功能）：%s", exc)
            return False

    def _run(self) -> None:
        try:
            assert self._icon is not None
            self._icon.run()
        except Exception as exc:  # noqa: BLE001
            log.debug("托盘事件循环退出：%s", exc)

    @staticmethod
    def _wrap(fn: Callable[[], None] | None):
        """把回调包一层异常保护，避免托盘线程里的异常导致整个图标消失。"""
        def _inner(icon=None, item=None):  # noqa: ANN001
            if fn is None:
                return
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("托盘菜单动作失败：%s", exc)

        return _inner

    # ------------------------------------------------------------------
    def notify(self, message: str, title: str | None = None) -> None:
        """弹一条系统通知（备份完成时用）。"""
        if not self._icon:
            return
        try:
            self._icon.notify(message, title or self.title)
        except Exception as exc:  # noqa: BLE001
            log.debug("托盘通知失败：%s", exc)

    def update_tooltip(self, text: str) -> None:
        if not self._icon:
            return
        try:
            self._icon.title = f"{self.title}\n{text}"
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        """关闭托盘。"""
        if not self._icon:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            pass
        self._icon = None
        self._started = False


def make_icon_file(dest: str | Path) -> bool:
    """生成 ``assets/icon.ico``（打包前由 tools 脚本调用）。"""
    if not _AVAILABLE:
        return False
    try:
        img = _load_icon_image(256)
        dest_p = Path(dest)
        paths.ensure_dir(dest_p.parent)
        img.save(dest_p, format="ICO",
                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("生成图标失败：%s", exc)
        return False
