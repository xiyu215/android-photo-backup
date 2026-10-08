"""终端二维码渲染（字符块，不生成图片文件）。

为什么不用图片？
    用户明确要求"二维码用字符块直接渲染在终端（不生成图片文件，减少依赖）"。
    这样就不需要 Pillow 的图片保存路径，也不会在磁盘上留垃圾文件，
    还能在纯命令行环境下工作。

为什么用半角块（▀▄█）而不是两个字符表示一个模块？
    终端字符的宽高比大约是 1:2（宽:高），直接用 '██' 表示一个模块会让二维码
    被纵向拉长，手机扫不出来。用半角块把两个模块行压进一行文本，
    模块就近似正方形了。

颜色处理
--------
二维码必须是"深色模块 + 浅色背景"才能被识别。终端主题有深有浅，
所以这里主动用 ANSI 把整块区域设成**白底黑块**，无论终端主题如何都能扫。
若终端不支持 ANSI（例如输出被重定向到文件），则退化为"深色终端"的假设。
"""

from __future__ import annotations

from typing import Sequence

# 半角块字符
_BOTH = "█"      # U+2588 全块
_UPPER = "▀"     # U+2580 上半块
_LOWER = "▄"     # U+2584 下半块
_EMPTY = " "

# ANSI：白底 + 黑字
_WHITE_BG_BLACK_FG = "\033[48;5;231m\033[38;5;16m"
_RESET = "\033[0m"


def build_matrix(data: str, *, border: int = 2) -> list[list[bool]]:
    """用 qrcode 库生成 0/1 矩阵。"""
    import qrcode  # 延迟导入：只有真的要显示二维码时才需要

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # M 级：兼顾容量与容错
        box_size=1,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def render(data: str, *, ansi: bool = True, border: int = 2,
           invert: bool = False) -> list[str]:
    """把二维码渲染成若干行文本。

    :param ansi: 是否使用 ANSI 白底（True 时在任何主题下都能扫）。
    :param invert: 在无 ANSI 场景下，是否按"深色终端"反转（浅色模块画成字符）。
    """
    matrix = build_matrix(data, border=border)
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0

    lines: list[str] = []
    for r in range(0, rows, 2):
        top = matrix[r]
        bottom = matrix[r + 1] if r + 1 < rows else [False] * cols
        buf: list[str] = []
        for c in range(cols):
            t = top[c]
            b = bottom[c]
            if invert:
                # 深色终端：把"浅色模块"画成白块，深色模块留作背景
                t, b = (not t), (not b)
            if t and b:
                buf.append(_BOTH)
            elif t:
                buf.append(_UPPER)
            elif b:
                buf.append(_LOWER)
            else:
                buf.append(_EMPTY)
        lines.append("".join(buf))
    return lines


def render_for_terminal(data: str, *, color_enabled: bool = True,
                        dark_theme: bool = True) -> str:
    """生成可直接 ``print`` 的二维码文本（含 ANSI 白底）。"""
    if color_enabled:
        body = render(data, ansi=True, invert=False)
        prefix, suffix = _WHITE_BG_BLACK_FG, _RESET
    else:
        # 无颜色时：深色终端需要反转，浅色终端保持原样
        body = render(data, ansi=False, invert=dark_theme)
        prefix = suffix = ""

    # 左右各留一列白边，避免紧贴终端边框
    return "\n".join(prefix + " " + line + " " + suffix for line in body)


def render_plain(data: str, *, invert: bool = True) -> str:
    """不依赖 ANSI 的纯文本渲染（用于日志文件）。"""
    return "\n".join(render(data, ansi=False, invert=invert))


def matrix_size(data: str, *, border: int = 2) -> tuple[int, int]:
    """返回 (行数, 列数)，用于估算终端占用空间。"""
    m = build_matrix(data, border=border)
    return len(m), (len(m[0]) if m else 0)
