"""日志与终端输出。

包含三块能力：
1. **彩色控制台**：Windows 10+ 通过开启 VT 处理让 ANSI 转义生效；
   不依赖 colorama，减少打包体积。
2. **文件日志**：每次任务一个日志文件，滚动保留（默认 20 份）。
3. **崩溃捕获**：``sys.excepthook`` + ``threading.excepthook``，
   把未捕获异常完整写入崩溃日志，保证"任何异常都不会让程序静默退出"。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

from . import paths

# ---------------------------------------------------------------------------
# ANSI 颜色
# ---------------------------------------------------------------------------
class C:
    """ANSI 颜色常量。仅用于终端展示，不写进文件日志。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_CYAN = "\033[96m"


# 状态灯：用彩色圆点直观表达"成功/进行中/警告/失败"
LAMP_OK = f"{C.GREEN}●{C.RESET}"
LAMP_RUN = f"{C.CYAN}●{C.RESET}"
LAMP_WARN = f"{C.YELLOW}●{C.RESET}"
LAMP_ERR = f"{C.RED}●{C.RESET}"
LAMP_IDLE = f"{C.GREY}○{C.RESET}"

_LEVEL_COLOR = {
    logging.DEBUG: C.GREY,
    logging.INFO: C.RESET,
    logging.WARNING: C.YELLOW,
    logging.ERROR: C.RED,
    logging.CRITICAL: f"{C.BOLD}{C.RED}",
}

_LEVEL_LAMP = {
    logging.DEBUG: LAMP_IDLE,
    logging.INFO: LAMP_RUN,
    logging.WARNING: LAMP_WARN,
    logging.ERROR: LAMP_ERR,
    logging.CRITICAL: LAMP_ERR,
}

_use_color = True
_initialized = False
_log_file: Path | None = None


def _c(color: str) -> str:
    """按当前颜色开关返回颜色码，关闭时返回空串。

    为什么需要这个函数
    ------------------
    ``rule()`` / 崩溃提示这类**直接拼接** ``C.GREY`` 等常量的地方，绕过了
    ``_use_color`` 检查。后果是：只要把输出重定向到文件
    （``安卓相册备份.exe --usb --dry-run > 日志.txt``），文件里就会混进
    ``←[90m`` 之类的转义乱码 —— 实测光 4 个分节标题就带出 24 个转义序列。
    本模块的文档一直承诺"终端不支持 ANSI 时自动降级为纯文本"，这里是补齐。

    规则：**凡是拼接颜色，一律走这个函数**，不要直接用 ``C.XXX``。
    """
    return color if _use_color else ""


# ---------------------------------------------------------------------------
# 终端准备
# ---------------------------------------------------------------------------
def enable_vt() -> bool:
    """在 Windows 控制台开启 ANSI 转义处理。

    Windows 10 1511+ 支持 VT，但默认关闭；不开启的话彩色输出会显示成
    一堆 ``←[32m`` 之类的乱码。失败时返回 False，调用方应关闭颜色。
    """
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def enable_utf8_console() -> None:
    """把控制台代码页切到 UTF-8（65001），保证中文与二维码字符块正常显示。

    只在 Windows 上、且确实连着控制台时执行；打包成窗口程序时无控制台，
    这里会安静地失败。
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    # 让 Python 的标准输出也使用 UTF-8，避免 GBK 编码异常
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 格式化器
# ---------------------------------------------------------------------------
class _ColorFormatter(logging.Formatter):
    """控制台格式：``状态灯 时间 级别 消息``。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        lamp = _LEVEL_LAMP.get(record.levelno, LAMP_RUN)
        color = _LEVEL_COLOR.get(record.levelno, C.RESET)
        name = record.name.replace("photobak.", "")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))
        if _use_color:
            return f"{lamp} {C.GREY}{ts}{C.RESET} {color}{msg}{C.RESET}"
        return f"[{ts}] {name}: {msg}"


class _PlainFormatter(logging.Formatter):
    """文件格式：完整时间 + 级别 + 模块 + 消息。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return f"{ts} [{record.levelname:<7}] {record.name}: {msg}"


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
def setup(
    log_dir: str | Path | None = None,
    level: str = "INFO",
    color: str = "auto",
    *,
    keep: int = 20,
) -> Path | None:
    """初始化日志系统。

    :param log_dir: 日志目录；None 表示只输出到控制台。
    :param level: DEBUG/INFO/WARN/ERROR。
    :param color: auto/always/never。
    :param keep: 日志文件滚动保留数量。
    :return: 本次任务的日志文件路径（未落文件时为 None）。
    """
    global _use_color, _initialized, _log_file

    enable_utf8_console()
    if color == "never":
        _use_color = False
    elif color == "always":
        _use_color = True
    else:  # auto
        _use_color = enable_vt() and sys.stdout.isatty()

    lvl = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }.get(str(level).upper(), logging.INFO)

    root = logging.getLogger("photobak")
    root.setLevel(lvl)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(lvl)
    console.setFormatter(_ColorFormatter())
    root.addHandler(console)

    if log_dir is not None:
        d = paths.ensure_dir(log_dir)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        _log_file = d / f"{stamp}.log"
        try:
            fh = logging.FileHandler(_log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)  # 文件里永远记全量，便于排障
            fh.setFormatter(_PlainFormatter())
            root.addHandler(fh)
        except OSError:
            _log_file = None
        else:
            _rotate(d, keep)

    # 让第三方库（uvicorn 等）的日志也汇入同一套格式
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(max(lvl, logging.WARNING))

    _install_crash_hooks()
    _initialized = True
    return _log_file


def _rotate(log_dir: Path, keep: int) -> None:
    """日志滚动：按修改时间保留最新 keep 份。"""
    try:
        files = sorted(
            (p for p in log_dir.glob("*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def get_logger(name: str = "") -> logging.Logger:
    """获取子 logger。"""
    return logging.getLogger(f"photobak.{name}" if name else "photobak")


def current_log_file() -> Path | None:
    return _log_file


# ---------------------------------------------------------------------------
# 崩溃捕获
# ---------------------------------------------------------------------------
def _install_crash_hooks() -> None:
    """安装全局异常钩子。

    用户明确要求"任何异常都不得导致程序崩溃退出"。虽然我们在业务层做了
    大量 try/except，但意外总是存在；这里做最后一道兜底：把完整堆栈写进
    崩溃日志，并在控制台给出可读提示与日志路径。
    """
    log = get_logger("crash")

    def _hook(exc_type, exc_value, exc_tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("程序遇到未预期错误（E701）", exc_info=(exc_type, exc_value, exc_tb))
        where = f"\n崩溃日志：{_log_file}" if _log_file else ""
        red, bold, reset = _c(C.RED), _c(C.BOLD), _c(C.RESET)
        print(
            f"\n{red}{bold}程序遇到未预期错误，已安全停止。{reset}"
            f"\n请把日志文件反馈给开发者。{where}\n",
            file=sys.stderr,
        )

    sys.excepthook = _hook

    def _thread_hook(args):  # noqa: ANN001
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "后台线程出现未预期错误（E701）",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    try:
        threading.excepthook = _thread_hook
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 展示辅助（供 ui 层复用，避免各处重复拼串）
# ---------------------------------------------------------------------------
def disp_width(text: str) -> int:
    """按"终端实际占几列"来算字符串宽度。

    为什么不能用 len()？
        中文、全角标点、emoji 在终端里占 **2 列**，而 len() 只算 1。
        用 len() 去补齐边框，中文一多整个框就会歪掉——这是最容易在
        "看起来没问题"的情况下翻车的细节，所以单独抽一个函数统一处理。
    """
    w = 0
    for ch in str(text):
        if unicodedata.combining(ch):
            continue                      # 组合附加符号不占宽度
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2                        # 宽/全角
        else:
            w += 1
    return w


def pad_to(text: str, width: int, *, align: str = "left") -> str:
    """按显示宽度补齐到 ``width`` 列（不足则补空格，超出则原样返回）。"""
    gap = max(0, width - disp_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def banner(title: str, subtitle: str = "", width: int = 66) -> str:
    """生成 ASCII 标题框（按显示宽度对齐，中文/emoji 都不会撑歪边框）。"""
    inner = width - 2                     # 左右各一个边框字符
    lines = ["╔" + "═" * inner + "╗"]
    lines.append("║" + pad_to(f"  {title}", inner) + "║")
    if subtitle:
        lines.append("║" + pad_to(f"  {subtitle}", inner) + "║")
    lines.append("╚" + "═" * inner + "╝")
    return "\n".join(lines)


def rule(text: str = "", width: int = 66) -> str:
    """生成分隔线（可带居中小标题）。

    颜色一律经 :func:`_c` 取得，保证输出被重定向到文件时不残留转义序列。
    """
    grey, bold, reset = _c(C.GREY), _c(C.BOLD), _c(C.RESET)
    if not text:
        return grey + "─" * width + reset
    pad = max(0, width - disp_width(text) - 2)
    left = pad // 2
    return f"{grey}{'─' * left}{reset} {bold}{text}{reset} {grey}{'─' * (pad - left)}{reset}"
