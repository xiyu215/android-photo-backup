"""文件名与路径安全化。

必须处理的三类问题
------------------
1. **Windows 非法字符**：``< > : " / \\ | ? *`` 与控制字符。手机文件名完全可能
   含这些字符（尤其是从网页/聊天软件保存的文件）。
2. **超长路径**：Windows 默认路径上限 260 字符。手机相册里 ``DCIM/Camera`` 下
   的文件名不长，但一旦套上"设备名 + 原路径镜像"就可能超限。
3. **保留名与结尾点/空格**：``CON``、``NUL`` 等是设备名；结尾的点和空格会被
   Windows 静默丢弃，导致"存进去的名字和记录的不一致"，破坏索引一致性。

设计原则
--------
**只改"存储用的名字"，绝不改内容，也绝不留不住原名的线索。**
被改名的文件会把「原文件名 → 存储名」的映射写进清理清单，保证用户仍能对照查找。
emoji 在 NTFS 上是合法的，因此**保留不改**，只在日志里做转义展示。
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..common.logger import get_logger
from ..common.paths import WIN_PATH_LIMIT, path_len

log = get_logger("naming")

# Windows 非法字符
_ILLEGAL = set('<>:"/\\|?*')
_ILLEGAL |= {chr(i) for i in range(32)}

# Windows 保留设备名（不区分大小写，且**带扩展名也算**，如 con.jpg）
_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_RESERVED |= {f"COM{i}" for i in range(1, 10)}
_RESERVED |= {f"LPT{i}" for i in range(1, 10)}

# 控制字符与零宽字符（手机端偶见）
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")

# 单个文件名保留的最大字符数（用于超长路径压缩）
_NAME_MAX = 100
_NAME_MIN = 24


@dataclass
class NameResult:
    """路径构造结果。"""

    rel_path: str                     # 相对备份根的 POSIX 风格路径
    original_name: str                # 手机上的原文件名
    stored_name: str                  # 实际存储用的文件名
    notes: list[str] = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return self.original_name != self.stored_name


def sanitize_component(name: str, *, replacement: str = "_") -> str:
    """清理单个路径分量（目录名或文件名）。

    处理顺序很重要：
        去零宽字符 → 替换非法字符 → 去结尾点/空格 → 处理保留名 → 处理空名。
    """
    if not name:
        return "_"

    s = _INVISIBLE_RE.sub("", str(name))
    # 归一化：把全角字符等统一，避免同一文件在不同来源下被当成两个名字
    s = unicodedata.normalize("NFC", s)

    s = "".join(replacement if ch in _ILLEGAL else ch for ch in s)
    s = s.rstrip(" .")          # Windows 不允许结尾的点或空格
    s = s.lstrip(" .")          # 开头的点会导致隐藏文件，也不友好

    if not s:
        return "_"

    # 保留名：CON / NUL / COM1 ... （带扩展名也要处理，如 con.jpg）
    stem = s.split(".")[0].upper()
    if stem in _RESERVED:
        s = "_" + s

    return s


def sanitize_relative(rel: str) -> str:
    """清理一个相对路径（按 ``/`` 拆分逐段处理）。"""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return "_"
    return "/".join(sanitize_component(p) for p in parts)


def _compress_filename(name: str, max_len: int, hash_prefix: str) -> str:
    """压缩文件名：``很长的名字.jpg`` → ``很长的名~a1b2c3d4.jpg``。

    保留扩展名与哈希前缀（哈希前缀可用于在清单中唯一定位），
    这样即使名字被截断，用户依然能确认"这就是那个文件"。
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:  # 没有扩展名
        stem, ext = name, ""
    else:
        ext = "." + ext
        if len(ext) > 16:  # 扩展名异常长，视为名字的一部分
            stem, ext = name, ""

    tail = f"~{hash_prefix[:8]}{ext}"
    keep = max(4, max_len - len(tail))
    return stem[:keep] + tail


def _truncate_dir(name: str, max_len: int) -> str:
    """压缩目录名（保留头部，去掉尾部）。"""
    return name if len(name) <= max_len else name[:max_len]


def build_rel_path(
    dir_parts: list[str],
    filename: str,
    *,
    hash_hex: str,
    root_abs: str | Path | None = None,
    limit: int = WIN_PATH_LIMIT,
) -> NameResult:
    """构造一个"一定能在 Windows 上写下去"的相对路径。

    :param dir_parts: 目标目录层级（如 ``["照片归档", "2023", "2023-07"]``）。
    :param filename:  手机上的原文件名。
    :param hash_hex:  内容哈希，用于压缩时生成可辨识后缀。
    :param root_abs:  备份根绝对路径，用于计算真实长度（None 时按 60 字符估算）。
    :return: :class:`NameResult`
    """
    notes: list[str] = []
    original = filename

    parts = [sanitize_component(p) for p in dir_parts if p]
    stored = sanitize_component(filename)

    if stored != original:
        notes.append("文件名含非法字符或保留名，已安全转义")

    root_len = len(str(Path(root_abs).resolve())) if root_abs else 60
    # 预留 .part 后缀与可能的重名后缀
    budget = limit - root_len - 16

    def dirs_len(dirs: list[str]) -> int:
        return sum(len(d) + 1 for d in dirs)

    def full_len(name: str, dirs: list[str]) -> int:
        return root_len + dirs_len(dirs) + len(name)

    # 第 1 级：按"实际剩余预算"压缩文件名。
    #
    # 早期版本这里用 budget // 2 当作目标长度，是拍脑袋的估计：当目录层级较深时
    # 压完仍然超限，于是直接跌到第 3 级变成纯哈希名，把可读的文件名白白丢掉。
    # 正确做法是把"根路径 + 目录层级 + 预留量"都扣掉，一次算准可用长度。
    avail = budget - root_len - dirs_len(parts)
    if len(stored) > avail:
        target = max(_NAME_MIN, min(avail, _NAME_MAX))
        new_name = _compress_filename(stored, target, hash_hex)
        if new_name != stored:
            stored = new_name
            notes.append("路径过长，已缩短文件名（原文件名见「原名」列）")

    # 第 2 级：文件名压到头仍超限（说明目录层级本身太长）→ 压缩目录名
    if full_len(stored, parts) > budget:
        changed = False
        for i in sorted(range(len(parts)), key=lambda k: len(parts[k]), reverse=True):
            if full_len(stored, parts) <= budget:
                break
            if i == 0:
                continue  # 第一级（归档根/镜像根）保持可读，方便用户认出是哪棵树
            # 按"超出多少就砍多少"来缩，一次到位
            over = full_len(stored, parts) - budget
            shorter = _truncate_dir(parts[i], max(8, len(parts[i]) - over))
            if shorter != parts[i]:
                parts[i] = shorter
                changed = True
        if changed:
            notes.append("路径过长，已缩短目录名")

    # 第 3 级：仍然超限 → 只保留哈希后缀的极短名字（兜底，正常不会走到）
    if full_len(stored, parts) > budget:
        stem, dot, ext = stored.rpartition(".")
        stored = f"{hash_hex[:8]}{('.' + ext) if dot else ''}"
        notes.append("路径严重超限，已改用哈希作为文件名")

    rel = "/".join([*parts, stored])
    return NameResult(rel, original, stored, notes)


def unique_rel_path(
    root_abs: str | Path,
    rel: str,
    *,
    same_content,  # Callable[[Path], bool] —— 已存在文件是否与本次内容相同
    max_try: int = 50,
) -> tuple[str, bool]:
    """处理重名冲突。

    关键语义（用户明确要求）：
        - **只有内容哈希不同才叫"重名冲突"**，此时追加 ``_1``、``_2``；
        - 如果已存在文件内容与本次相同 → 视为同一份，直接复用（走秒传/跳过），
          不产生 ``_1`` 副本。

    :param same_content: 回调，入参为候选绝对路径，返回"该文件内容是否与本次相同"。
    :return: (最终相对路径, 是否复用了已存在文件)
    """
    root = Path(root_abs)
    candidate = root / Path(*rel.split("/"))
    if not candidate.exists():
        return rel, False
    try:
        if same_content(candidate):
            return rel, True
    except OSError:
        pass

    stem, dot, ext = rel.rpartition(".")
    if not dot:
        stem, ext = rel, ""
    else:
        ext = "." + ext
    for i in range(1, max_try + 1):
        cand_rel = f"{stem}_{i}{ext}"
        cand = root / Path(*cand_rel.split("/"))
        if not cand.exists():
            return cand_rel, False
        try:
            if same_content(cand):
                return cand_rel, True
        except OSError:
            continue
    # 极端情况：同名太多，用哈希收尾
    return f"{stem}_{os.urandom(3).hex()}{ext}", False


def escape_display(name: str) -> str:
    """把文件名转成"纯 ASCII 可见"的展示形式。

    仅用于日志与控制台输出：Windows 控制台在部分字体下无法显示 emoji，
    转义后至少能看清是哪个文件。清单文件里保留原样（UTF-8 BOM，Excel 正常）。
    """
    out = []
    for ch in name:
        if ch.isprintable() and ord(ch) < 0x2000:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def fit_directory(name: str, max_len: int = 60) -> str:
    """把一个字符串压成合法的目录名（用于设备名等）。"""
    return _truncate_dir(sanitize_component(name), max_len) or "未知设备"


def device_dir_name(model: str | None, serial: str | None) -> str:
    """生成镜像树下的设备目录名，如 ``红米K90_A1B2``。

    用户明确"暂不需要多设备，但保留接口"：这里始终带上序列号后缀，
    将来接入第二台手机时天然不会互相覆盖。
    """
    base = fit_directory(model or "安卓设备", 32)
    tail = (serial or "0000").replace(":", "").replace("-", "")[-4:].upper()
    return f"{base}_{tail}"
