"""扫描规则：决定"扫什么、不扫什么、算哪一类"。

职责边界
--------
本模块**不做 IO**：它只接收通道层枚举出来的文件元数据，按规则过滤与打标。
这样 USB 与 WiFi（ADB over WiFi）两条通道天然共用同一套规则——
用户要求"两种连接通道规则完全一致"，靠的就是这个切分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..common.logger import get_logger
from .config import Config

log = get_logger("scanner")

#: 缩略图/缓存类目录名（小写精确匹配）——必须排除，否则会把 App 缓存当照片备份
CACHE_DIR_NAMES = {
    ".thumbnails", ".thumbnail", "thumbnails", "thumbnail",
    ".cache", "cache", ".temp", "temp", "tmp", ".tmp",
    "thumbs", ".thumbs", ".trashed", ".trash", "trash",
    ".recycle", "recycle", "lost.dir", ".nomedia_dir",
    "android/data", "android/obb",   # 兜底：即使包含 Android/data 也跳过 obb
}

#: 缓存目录的路径特征（用于清理清单的「App缓存可清」分档）
CACHE_PATH_HINTS = (".thumbnails", "/cache/", "/.cache/", "/temp/", "/tmp/",
                    "android/data/", "android/obb/", "lost.dir")

#: 明确无备份价值的文件后缀
JUNK_EXT = {"tmp", "temp", "log", "thumb", "thumbdata", "nomedia", "db-journal", "part"}

#: 需要跳过的特殊文件名
JUNK_NAMES = {".nomedia", "thumbs.db", "desktop.ini", ".ds_store"}

_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,12})$")

# ---------------------------------------------------------------------------
# 被排除文件的原因码
# ---------------------------------------------------------------------------
# 为什么要给"排除"也编个码？
#     清理清单的价值在于"手机上有什么、为什么不能删"。如果某个文件被规则排除、
#     既没备份、又不出现在清单里，用户就会困惑"我这张照片怎么不见了"。
#     因此下面三类"排除"要带原因码写进「未备份清单」，在清理页里如实呈现：
#       - 超限被拒 → 「暂不能删」（它还在手机上，电脑上没有副本）
#       - 缓存目录 → 「App 缓存可清」（删了没有损失）
#       - .nomedia → 「暂不能删」（App 私有内容，删了可能影响 App）
REJECT_TOO_BIG = "E410"
REJECT_CACHE = "E420"
REJECT_NOMEDIA = "E430"

_REJECT_CODES: tuple[tuple[str, str], ...] = (
    ("超过单文件上限", REJECT_TOO_BIG),
    ("位于缓存目录", REJECT_CACHE),
    ("位于 .nomedia 目录", REJECT_NOMEDIA),
)


def reject_code(reason: str) -> str:
    """把排除原因文本映射成原因码；不属于"需要告知用户"的类别则返回空串。"""
    for prefix, code in _REJECT_CODES:
        if reason.startswith(prefix):
            return code
    return ""


def reject_tier_hint(code: str) -> str:
    """该原因码在清理清单里应落到哪一档。"""
    return "cache" if code == REJECT_CACHE else "pending"


@dataclass
class RemoteFile:
    """通道层枚举出来的一个远端文件（只含元数据）。"""

    path: str            # 手机上的绝对路径，如 /sdcard/DCIM/Camera/IMG_x.jpg
    rel: str             # 相对存储根的路径，如 DCIM/Camera/IMG_x.jpg
    size: int
    mtime: float | None = None
    app_tag: str = "Other"
    root_tag: str = "Other"

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def ext(self) -> str:
        m = _EXT_RE.search(self.name)
        return m.group(1).lower() if m else ""


@dataclass
class ScanStats:
    """扫描统计（用于终端展示与日志）。"""

    total_seen: int = 0
    skipped_ext: int = 0
    skipped_dir: int = 0
    skipped_nomedia: int = 0
    skipped_junk: int = 0
    skipped_empty: int = 0
    skipped_too_big: int = 0
    accepted: int = 0
    accepted_bytes: int = 0
    by_root: dict[str, int] = field(default_factory=dict)
    by_app: dict[str, int] = field(default_factory=dict)


class Scanner:
    """按配置执行扫描规则的过滤器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.media_ext = cfg.all_media_ext()
        # 归一化包含目录：统一小写、去首尾斜杠
        self.include_roots = [d.strip().strip("/").lower() for d in cfg.include_dirs if d.strip()]
        # 映射表按 key 长度降序，保证 "dcim/screenshots" 先于 "dcim" 命中
        self._app_map = sorted(cfg.app_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        #: 含 .nomedia 的目录（相对路径，小写）——由 :meth:`learn_nomedia` 填充
        self._nomedia_dirs: set[str] = set()
        self.stats = ScanStats()

    # ------------------------------------------------------------------
    # .nomedia 学习
    # ------------------------------------------------------------------
    def learn_nomedia(self, files) -> int:
        """从文件列表里识别"哪些目录含 .nomedia"。

        为什么要这样绕一圈？
            ``.nomedia`` 本身就是一个普通文件，通道枚举时一定会列到它。
            因此不需要为每个目录额外跑一次 shell 去探测——枚举一遍就顺带拿到了。
            这对"目录多、文件少"的相册场景能省下大量 IO。
        """
        self._nomedia_dirs.clear()
        for rf in files:
            if rf.name == ".nomedia":
                d = rf.rel.replace("\\", "/").rsplit("/", 1)[0].lower() if "/" in rf.rel else ""
                self._nomedia_dirs.add(d)
        return len(self._nomedia_dirs)

    def _under_nomedia(self, rel: str) -> bool:
        """该文件是否位于（或就是）某个含 .nomedia 的目录下。"""
        if not self._nomedia_dirs:
            return False
        low = rel.replace("\\", "/").lower()
        d = low.rsplit("/", 1)[0] if "/" in low else ""
        if d in self._nomedia_dirs:
            return True
        # 祖先目录命中同样视为被标记
        parts = d.split("/") if d else []
        for i in range(1, len(parts) + 1):
            if "/".join(parts[:i]) in self._nomedia_dirs:
                return True
        return False


    # ------------------------------------------------------------------
    # 目录级判定
    # ------------------------------------------------------------------
    def should_descend(self, rel_dir: str, has_nomedia: bool = False) -> tuple[bool, str]:
        """是否应该进入该目录。返回 (是否进入, 原因)。

        参数 ``has_nomedia`` 由通道层在列目录时顺带探测（列一次目录就能看到
        有没有 .nomedia 文件），避免额外一次 IO。
        """
        d = rel_dir.replace("\\", "/").strip("/")
        low = d.lower()
        if not low:
            return True, ""

        name = low.rsplit("/", 1)[-1]

        # ① 缓存/缩略图目录：直接不进
        if name in CACHE_DIR_NAMES:
            self.stats.skipped_dir += 1
            return False, "缓存/缩略图目录"
        if f"/{name}/" in "/" + low + "/" and low.endswith(("cache", "temp", "tmp")):
            self.stats.skipped_dir += 1
            return False, "缓存目录"

        # ② Android/data 与 Android/obb：默认排除，可配置包含（obb 永远排除）
        if low.startswith("android/"):
            if low.startswith("android/obb"):
                self.stats.skipped_dir += 1
                return False, "Android/obb 游戏资源包"
            if low.startswith("android/data") and not self.cfg.include_android_data:
                self.stats.skipped_dir += 1
                return False, "Android/data 私有目录（默认排除）"
            if low.startswith("android/media"):
                pass  # Android/media 是标准媒体目录，允许进入
            elif low.startswith("android/") and not self.cfg.include_android_data:
                self.stats.skipped_dir += 1
                return False, "Android 系统目录（默认排除）"

        # ③ .nomedia 标记
        if has_nomedia and not self.cfg.ignore_nomedia:
            self.stats.skipped_nomedia += 1
            return False, "含 .nomedia 标记（App 私有内容）"

        # ④ 隐藏目录（以点开头）一般不包含用户媒体
        if name.startswith(".") and name not in (".", ".."):
            self.stats.skipped_dir += 1
            return False, "隐藏目录"

        return True, ""

    def is_root_allowed(self, rel_dir: str) -> bool:
        """路径是否落在扫描范围内（``rel_dir`` 传的是**相对 /sdcard 的路径**）。

        ``包含目录`` 里写的都是相对 /sdcard 的路径，按配置文件里的说明，
        下面三种写法都应该有效：

        ==============================  ==========================
        写法                             匹配范围
        ==============================  ==========================
        ``DCIM``                        DCIM 下全部内容
        ``DCIM/Screenshots``            只匹配这个子目录
        ``DCIM/Camera/IMG_0001.jpg``    只匹配这一个文件
        ==============================  ==========================

        **曾经踩过的坑**：早期实现只取路径的**第一段**去和 ``include_roots``
        做全等比较（``"dcim" in ["dcim/screenshots"]`` → False）。后果是按
        配置注释写子路径的用户会看到"扫描到 N 个文件、纳入备份 0 个"，
        程序**不报任何错**，备份静默变成空的。已实测复现，这里改为按路径
        分段做前缀匹配。

        默认配置（``DCIM;Pictures;Movies;Download``）行为完全不变。
        """
        low = rel_dir.replace("\\", "/").strip("/").lower()
        if not low:
            return True
        for root in self.include_roots:
            # 全等（目录本身）或以 "根/" 开头（根下面的内容）
            if low == root or low.startswith(root + "/"):
                return True
        first = low.split("/")[0]
        if first == "android" and self.cfg.include_android_data:
            return True
        return False

    # ------------------------------------------------------------------
    # 文件级判定
    # ------------------------------------------------------------------
    def accept_file(self, rf: RemoteFile) -> tuple[bool, str]:
        """单个文件是否纳入备份。返回 (是否接受, 拒绝原因)。"""
        self.stats.total_seen += 1
        name = rf.name
        low_name = name.lower()

        # ① 垃圾文件名
        if low_name in JUNK_NAMES:
            self.stats.skipped_junk += 1
            return False, "系统/标记文件"

        # ② 扩展名白名单
        ext = rf.ext
        if not ext or ext not in self.media_ext:
            self.stats.skipped_ext += 1
            return False, f"非媒体扩展名（.{ext or '无'}）"
        if ext in JUNK_EXT:
            self.stats.skipped_junk += 1
            return False, "临时文件"

        # ③ 零字节
        if rf.size <= 0:
            self.stats.skipped_empty += 1
            return False, "零字节文件"

        # ④ 单文件大小上限（用户明确：超过则拒绝上传并跳过）
        if rf.size > self.cfg.max_file_bytes():
            self.stats.skipped_too_big += 1
            return False, (f"超过单文件上限 {self.cfg.max_file_mb} MB"
                           f"（实际 {rf.size / 1024 / 1024:.1f} MB）")

        # ⑤ 路径必须落在允许的顶层目录内
        if not self.is_root_allowed(rf.rel):
            self.stats.skipped_dir += 1
            return False, "不在扫描范围内"

        # ⑥ .nomedia 标记（用户未选择忽略时，该目录下的一切都不备份）
        if not self.cfg.ignore_nomedia and self._under_nomedia(rf.rel):
            self.stats.skipped_nomedia += 1
            return False, "位于 .nomedia 目录（App 私有内容）"

        # ⑦ 路径中任一段是缓存目录
        low_rel = rf.rel.lower().replace("\\", "/")
        if any(hint in "/" + low_rel for hint in CACHE_PATH_HINTS):
            self.stats.skipped_dir += 1
            return False, "位于缓存目录"

        return True, ""

    # ------------------------------------------------------------------
    # 打标
    # ------------------------------------------------------------------
    def classify(self, rf: RemoteFile) -> RemoteFile:
        """补充 app_tag / root_tag。"""
        rf.app_tag = self.app_tag_of(rf.rel)
        rf.root_tag = self.root_tag_of(rf.rel)
        return rf

    def app_tag_of(self, rel: str) -> str:
        """根据配置的映射表判断文件来自哪个 App。"""
        low = "/" + rel.replace("\\", "/").lower().lstrip("/")
        for key, tag in self._app_map:
            k = key.lower().strip("/")
            if not k:
                continue
            if f"/{k}/" in low or low.endswith(f"/{k}") or low.startswith(f"/{k}/"):
                return tag
        return "Other"

    def root_tag_of(self, rel: str) -> str:
        first = rel.replace("\\", "/").strip("/").split("/")[0].lower()
        mapping = {
            "dcim": "DCIM", "pictures": "Pictures", "movies": "Movies",
            "download": "Download", "downloads": "Download",
            "android": "Android_data", "dcim/camera": "DCIM",
        }
        return mapping.get(first, first.capitalize() if first else "Other")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def filter(self, files, *, on_reject=None) -> list[RemoteFile]:
        """过滤一批远端文件。

        :param files: 可迭代的 :class:`RemoteFile`
        :param on_reject: 可选回调 ``(RemoteFile, code, reason)``。只有
                          :func:`reject_code` 认得出来的类别才会回调——
                          即"用户需要知道为什么没备份"的那几类，其余（扩展名
                          不符、零字节等）属正常过滤，不打扰用户。
        """
        materialized = list(files)
        # 先学习 .nomedia 分布，再逐条判定（两遍扫描，纯内存操作，无额外 IO）
        nomedia_dirs = self.learn_nomedia(materialized)
        if nomedia_dirs:
            log.info("检测到 %d 个含 .nomedia 的目录，将按配置%s",
                     nomedia_dirs, "忽略该标记" if self.cfg.ignore_nomedia else "跳过这些目录")

        out: list[RemoteFile] = []
        for rf in materialized:
            ok, reason = self.accept_file(rf)
            if not ok:
                if on_reject:
                    code = reject_code(reason)
                    if code:
                        on_reject(rf, code, reason)
                continue
            self.classify(rf)
            out.append(rf)
            self.stats.accepted += 1
            self.stats.accepted_bytes += rf.size
            self.stats.by_root[rf.root_tag] = self.stats.by_root.get(rf.root_tag, 0) + 1
            self.stats.by_app[rf.app_tag] = self.stats.by_app.get(rf.app_tag, 0) + 1
        return out


def is_cache_path(rel: str) -> bool:
    """该路径是否属于缓存类（供清理清单分档复用）。

    **调用约定**：入参必须是"手机侧的路径"（``/sdcard/...`` 或相对存储根的
    相对路径）。**不要**把宿主机的本地绝对路径传进来——Windows 上的
    ``C:\\Users\\xx\\AppData\\Local\\Temp\\...`` 会命中 ``/temp/`` 而误判。
    :meth:`CleanupBuilder._judge` 里的调用点已按此约定做了前置判断。
    """
    low = "/" + rel.replace("\\", "/").lower().lstrip("/")
    return any(hint in low for hint in CACHE_PATH_HINTS)


def is_edit_variant(name: str) -> bool:
    """是否为"编辑版"文件名（仅用于清单分组展示，**绝不**用于去重判定）。

    用户明确要求：``IMG_xxx.jpg`` 与 ``IMG_xxx_edit.jpg`` 必须视为两个不同文件，
    去重只认 SHA-256 完全一致。这里只提供"看起来像编辑版"的提示。

    注意后缀列表必须**显式枚举**：早期版本用了 ``_\\d+$`` 这种宽泛匹配，
    会把 ``IMG_1.jpg``、``照片_2.jpg`` 这类正常文件名误判成编辑版。
    """
    stem = name.rsplit(".", 1)[0].lower()
    return bool(re.search(
        r"(_edit|_edited|_hdr|_enhance|_enhanced|_filter|_beauty|_mosaic|_crop"
        r"|_adjust|_retouch|_vivid|_mono|_watermark)(_\d+)?$",
        stem,
    ))
