"""终端界面：ASCII 标题、彩色状态灯、表格、进度条、交互提问。

设计取舍
--------
用户要"终端输出要美观"，但又明确"不生成图片文件、减少依赖"。因此这里
只用 ANSI 转义与 Unicode 制表符实现，不引入 rich / colorama 之类的库——
它们会让打包体积增加数 MB，而收益仅仅是省下这里 200 行代码。

对 Windows 的兼容
-----------------
    - 启动时统一把代码页切到 UTF-8（见 :func:`common.logger.enable_utf8_console`），
      否则中文与制表符会变成乱码；
    - 若终端不支持 ANSI（例如被重定向到文件），所有颜色函数会自动降级为纯文本。
"""

from __future__ import annotations

import sys
import time
from typing import Iterable, Sequence

from ..common import i18n, paths
from ..common.logger import C, banner as _banner, disp_width, get_logger, pad_to, rule

log = get_logger("ui")

# 终端可用宽度（用于自适应排版）
_WIDTH = 78


def _color(text: str, color: str) -> str:
    from ..common import logger as _lg

    return f"{color}{text}{C.RESET}" if _lg._use_color else text  # noqa: SLF001


def _supports_ansi() -> bool:
    from ..common import logger as _lg

    return _lg._use_color  # noqa: SLF001


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def out(text: str = "") -> None:
    """普通输出（会走 stdout，便于重定向到文件）。"""
    print(text)


def info(text: str) -> None:
    out(f"{_color('●', C.CYAN)} {text}")


def ok(text: str) -> None:
    out(f"{_color('●', C.GREEN)} {text}")


def warn(text: str) -> None:
    out(f"{_color('●', C.YELLOW)} {_color(text, C.YELLOW)}")


def error(text: str) -> None:
    out(f"{_color('●', C.RED)} {_color(text, C.RED)}")


def step(text: str) -> None:
    out(f"{_color('▶', C.BRIGHT_CYAN)} {text}")


def dim(text: str) -> None:
    out(_color(text, C.GREY))


# ---------------------------------------------------------------------------
# 标题与分节
# ---------------------------------------------------------------------------
def show_banner(subtitle: str = "") -> None:
    """打印启动横幅。"""
    out()
    out(_color(_banner(f"{i18n.APP_TITLE}  {i18n.APP_NAME}", subtitle or i18n.BANNER_SUBTITLE),
               C.BRIGHT_CYAN))
    out()


def section(title: str) -> None:
    """打印分节标题。"""
    out()
    out(rule(title))


def safety_notice() -> None:
    """每次启动都强调"绝不删除手机文件"，强化用户的安全预期。"""
    out()
    out(_color("  ⚠ 安全说明", C.BRIGHT_YELLOW))
    for line in i18n.SAFETY_NOTICE.splitlines():
        out(f"    {line}")


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def kv_table(rows: Sequence[tuple[str, str]], *, indent: int = 2) -> None:
    """两列键值表（按**显示宽度**自动对齐，中文键名不会错位）。"""
    if not rows:
        return
    width = max(disp_width(str(k)) for k, _ in rows)
    pad = " " * indent
    for k, v in rows:
        out(f"{pad}{_color(pad_to(str(k), width), C.GREY)}  {v}")


def table(headers: Sequence[str], rows: Iterable[Sequence[object]],
          *, indent: int = 2, max_col: int = 40) -> None:
    """简易三线表（列宽按内容自适应，超长截断）。

    列宽一律用 :func:`disp_width` 计算：手机路径里混着中文和英文时，
    用 len() 会让表格逐行错位。
    """
    data = [[("" if c is None else str(c))[:max_col] for c in row] for row in rows]
    widths = [disp_width(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], disp_width(cell))

    pad = " " * indent
    head = "  ".join(_color(pad_to(h, widths[i]), C.BOLD) for i, h in enumerate(headers))
    out(f"{pad}{head}")
    out(f"{pad}{_color('─' * (sum(widths) + 2 * (len(widths) - 1)), C.GREY)}")
    for row in data:
        cells = [pad_to(row[i], widths[i]) if i < len(row) else " " * widths[i]
                 for i in range(len(widths))]
        out(f"{pad}{'  '.join(cells)}")


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------
_BAR_FULL = "█"
_BAR_EMPTY = "░"


def progress_bar(done: int, total: int, *, width: int = 34,
                 label: str = "", speed: float = 0.0) -> str:
    """生成一行进度条字符串（不换行输出，用 :func:`status_line` 原地刷新）。"""
    if total > 0:
        ratio = min(1.0, max(0.0, done / total))
    else:
        ratio = 0.0
    filled = int(width * ratio)
    bar = _BAR_FULL * filled + _BAR_EMPTY * (width - filled)
    pct = f"{ratio * 100:5.1f}%"
    parts = [f"[{_color(bar, C.BRIGHT_GREEN)}]", pct,
             f"{paths.human_size(done)}/{paths.human_size(total)}" if total else paths.human_size(done)]
    if speed > 0:
        parts.append(paths.human_speed(speed))
    if label:
        parts.append(label[:36])
    return "  ".join(parts)


def status_line(text: str) -> None:
    """原地刷新一行（用于进度条）。"""
    if _supports_ansi():
        sys.stdout.write("\r\033[K" + text)
        sys.stdout.flush()
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def end_status() -> None:
    """结束原地刷新（补一个换行）。"""
    if _supports_ansi():
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# 交互
# ---------------------------------------------------------------------------
# 全局"自动确认"开关，由命令行 ``--yes`` 打开，用于无人值守 / 自动化。
#
# **为什么必须有这个开关**：``--yes`` 的说明是"跳过所有确认（用于自动化）"，
# 但早期版本只把 ``args.yes`` 传给了编排器，终端这边的 ``input()`` 照样会问。
# 后果是自动化场景下进程会**永久卡死** —— 实测：备份本身 8 秒就跑完了，
# 却卡在"是否生成「删除脚本」"这一问上，直到手动杀进程（两次都这样）。
# 打开后所有提问一律直接返回**默认值**，绝不碰 stdin。
_auto_confirm = False


def set_auto_confirm(on: bool = True) -> None:
    """打开/关闭自动确认。由 ``--yes`` 调用。"""
    global _auto_confirm
    _auto_confirm = bool(on)


def is_auto_confirm() -> bool:
    return _auto_confirm


def ask(prompt: str, *, default: str = "") -> str:
    """提问并读取一行输入。Ctrl+C / EOF / 自动确认时返回默认值，避免程序卡死。"""
    if _auto_confirm:
        return default
    suffix = f"（默认 {default}）" if default else ""
    try:
        raw = input(f"{_color('?', C.BRIGHT_CYAN)} {prompt}{suffix}：").strip()
    except (EOFError, KeyboardInterrupt):
        out()
        return default
    return raw or default


def confirm(question: str, *, default: bool = True) -> bool:
    """是/否确认。自动确认模式下直接返回 ``default``（即"默认选项"）。"""
    if _auto_confirm:
        return default
    hint = "Y/n" if default else "y/N"
    raw = ask(f"{question} [{hint}]", default="y" if default else "n").strip().lower()
    return raw in ("y", "yes", "是", "1", "true")


def pause(message: str = "按回车继续…") -> None:
    """暂停等待用户。自动确认模式下直接跳过，避免无人值守时挂住。"""
    if _auto_confirm:
        return
    try:
        input(f"\n{_color('·', C.GREY)} {message}")
    except (EOFError, KeyboardInterrupt):
        out()


def countdown(seconds: int, message: str = "将在 %d 秒后自动继续") -> bool:
    """倒计时，用户按任意键可提前继续。

    :return: True=用户按键继续；False=自然走完
    """
    out(f"{message % seconds}（按回车立即继续）")
    try:
        import threading

        got = threading.Event()

        def _reader() -> None:
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            got.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        for _ in range(seconds):
            if got.wait(1.0):
                return True
        return False
    except Exception:  # noqa: BLE001 —— 倒计时失败不该影响主流程
        time.sleep(seconds)
        return False


# ---------------------------------------------------------------------------
# 常用组合展示
# ---------------------------------------------------------------------------
def show_scan_plan(plan_rows: Sequence[tuple[str, str, str]]) -> None:
    """展示扫描计划表：(目录, 是否包含, 说明)。"""
    table(["手机目录", "是否包含", "说明"], plan_rows)


def show_tier_summary(report) -> None:
    """展示四档清理结果汇总。"""
    rows = []
    for tier in i18n.TIER_ORDER:
        rows.append([
            tier,
            f"{report.count(tier)} 个",
            paths.human_size(report.size(tier)),
            i18n.TIER_DESC.get(tier, ""),
        ])
    table(["分档", "文件数", "合计大小", "说明"], rows)


def show_error(exc) -> None:
    """展示一个受控异常的完整提示（含具体解决办法）。"""
    from ..common.errors import PhotobakError

    out()
    if isinstance(exc, PhotobakError):
        error(f"【{exc.code}】{exc.title}")
        if exc.detail:
            out(f"    {_color('原因：', C.GREY)}{exc.detail}")
        out(f"    {_color('解决办法：', C.BRIGHT_YELLOW)}")
        for line in exc.hint.splitlines():
            out(f"      {line}")
    else:
        error(str(exc))
    out()
