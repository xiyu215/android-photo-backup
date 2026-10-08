"""时间优先级链解析。

背景（用户明确提出的问题）
--------------------------
安卓上的文件 ``mtime`` 非常不可信：
    - App 读写会刷新 mtime；
    - 系统迁移、云同步会刷新 mtime；
    - **微信/QQ 转存的图片，mtime 全是转存当天**，完全不是拍摄时间。
因此直接把 mtime 当拍摄时间，会让归档树的时间线彻底错乱。

优先级链
--------
    ① EXIF ``DateTimeOriginal``（照片）      → confidence 3
    ② 视频容器 ``mvhd creation_time``（视频） → confidence 2
    ③ 文件名中的时间串                        → confidence 2
    ④ ``mtime``（兜底）                       → confidence 1，标记"时间存疑"
    ⑤ 全部失败                                → confidence 0，进「未知时间」

两阶段解析（关键设计）
----------------------
扫描阶段文件还在手机上，读不到 EXIF/容器头；所以：
    - 扫描时用 :func:`resolve_preliminary`（只靠文件名 + mtime）给出**预估**；
    - 文件落盘后用 :func:`resolve_final`（能读 EXIF/mvhd）给出**最终**结论，
      并据此决定归档路径。
这样既能提前展示进度，又能保证归档时间线准确。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..common.logger import get_logger
from . import exif_reader

log = get_logger("times")

SRC_EXIF = "exif"
SRC_VIDEO = "video_mvhd"
SRC_FILENAME = "filename"
SRC_MTIME = "mtime"
SRC_UNKNOWN = "unknown"

_MIN_YEAR = 1990
_MAX_YEAR = 2100


@dataclass(frozen=True)
class TimeInfo:
    """一次时间解析的完整结论。"""

    taken_at: datetime | None
    source: str
    confidence: int  # 3=EXIF / 2=容器或文件名 / 1=mtime / 0=未知

    @property
    def is_uncertain(self) -> bool:
        """是否属于"时间存疑"（只有 mtime 或干脆没有时间）。"""
        return self.confidence <= 1

    @property
    def is_known(self) -> bool:
        return self.taken_at is not None

    def to_iso(self) -> str | None:
        return self.taken_at.strftime("%Y-%m-%d %H:%M:%S") if self.taken_at else None

    @classmethod
    def unknown(cls) -> "TimeInfo":
        return cls(None, SRC_UNKNOWN, 0)


UNKNOWN = TimeInfo.unknown()


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if not (_MIN_YEAR <= dt.year <= _MAX_YEAR):
        return None
    if dt > datetime.now() + timedelta(days=1):
        return None
    return dt


# ---------------------------------------------------------------------------
# 文件名时间串
# ---------------------------------------------------------------------------
# 说明：正则按"越具体越靠前"的顺序尝试，避免误匹配。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 微信导出：mmexport1689000000000（13 位毫秒时间戳）
    (re.compile(r"mmexport(\d{13})", re.I), "ms_epoch"),
    # 微信导出（秒级）：mmexport1689000000
    (re.compile(r"mmexport(\d{10})(?!\d)", re.I), "s_epoch"),
    # 常见相机：IMG_20230715_123456 / VID_20230715_123456 / PXL_20230715_123456789
    (re.compile(r"(?:IMG|VID|PXL|MVIMG|DSC|DSCF|BURST|PANORAMA)[_-](\d{8})[_-](\d{6})", re.I), "ymd_hms"),
    # 截屏：Screenshot_2023-07-15-12-34-56
    (re.compile(r"(?:Screenshot|screen)[_-](\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})", re.I), "ymd_dash"),
    # 通用：2023-07-15 12.34.56 / 2023-07-15_12-34-56 / 20230715_123456
    (re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})[-_ .](\d{2})[-_.](\d{2})[-_.](\d{2})"), "ymd_dash"),
    (re.compile(r"(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"), "ymd_hms"),
    # 只有日期：2023-07-15 / 20230715
    (re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})"), "ymd"),
    (re.compile(r"(?:^|[^0-9])(\d{4})(\d{2})(\d{2})(?:[^0-9]|$)"), "ymd"),
    # 独立 13 位毫秒时间戳（如 1689000000000.jpg）
    (re.compile(r"(?:^|[^0-9])(1[0-9]{12})(?:[^0-9]|$)"), "ms_epoch"),
]


def from_filename(name: str) -> datetime | None:
    """从文件名中提取时间。提取不到或时间不合理返回 None。"""
    stem = Path(name).stem
    for pattern, kind in _PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        try:
            g = m.groups()
            if kind == "ms_epoch":
                dt = datetime.fromtimestamp(int(g[0]) / 1000.0)
            elif kind == "s_epoch":
                dt = datetime.fromtimestamp(int(g[0]))
            elif kind == "ymd_hms":
                # 该分支对应两种正则：一种把日期与时间各捕获成一段（2 组），
                # 另一种逐段捕获（6 组）。这里统一处理，避免分组数不匹配。
                if len(g) == 2:
                    d, t = g[0], g[1]
                    dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]),
                                  int(t[:2]), int(t[2:4]), int(t[4:6]))
                else:
                    dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                                  int(g[3]), int(g[4]), int(g[5]))
            elif kind == "ymd_dash":
                dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                              int(g[3]), int(g[4]), int(g[5]))
            elif kind == "ymd":
                dt = datetime(int(g[0]), int(g[1]), int(g[2]))
            else:  # pragma: no cover - 防御分支
                continue
        except (ValueError, OSError, OverflowError, IndexError):
            continue  # 例如 2023-13-45 这种非法组合
        dt = _valid(dt)
        if dt:
            return dt
    return None


def from_mtime(mtime: float | None) -> datetime | None:
    """把 mtime 时间戳转成 datetime（带合理性校验）。"""
    if not mtime:
        return None
    try:
        return _valid(datetime.fromtimestamp(float(mtime)))
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# 两阶段解析
# ---------------------------------------------------------------------------
def resolve_preliminary(name: str, mtime: float | None) -> TimeInfo:
    """扫描阶段的预估时间（文件还在手机上，读不到 EXIF）。"""
    dt = from_filename(name)
    if dt:
        return TimeInfo(dt, SRC_FILENAME, 2)
    dt = from_mtime(mtime)
    if dt:
        return TimeInfo(dt, SRC_MTIME, 1)
    return UNKNOWN


def resolve_final(
    local_path: str | Path,
    *,
    kind: str,
    name: str,
    mtime: float | None = None,
) -> TimeInfo:
    """落盘后的最终时间判定（能读 EXIF / 视频容器）。"""
    try:
        dt = exif_reader.read_media_time(local_path, kind)
    except Exception as exc:  # noqa: BLE001 —— 解析失败绝不应中断备份
        log.debug("读取媒体时间失败 %s：%s", local_path, exc)
        dt = None
    if dt:
        return TimeInfo(dt, SRC_EXIF if kind == "image" else SRC_VIDEO, 3 if kind == "image" else 2)

    dt = from_filename(name)
    if dt:
        return TimeInfo(dt, SRC_FILENAME, 2)

    if mtime is None:
        try:
            mtime = Path(local_path).stat().st_mtime
        except OSError:
            mtime = None
    dt = from_mtime(mtime)
    if dt:
        return TimeInfo(dt, SRC_MTIME, 1)
    return UNKNOWN


# ---------------------------------------------------------------------------
# 归档路径
# ---------------------------------------------------------------------------
def archive_subdir(info: TimeInfo, uncertain_dir: str, unknown_dir: str) -> list[str]:
    """把时间结论映射为归档子目录层级。

    - 时间可信（confidence ≥ 2）：``2023/2023-07/2023-07-15``
    - 时间存疑（confidence == 1）：``_时间存疑/2023/2023-07``
    - 完全未知（confidence == 0）：``_未知时间``

    为什么存疑的要单独放？因为 mtime 可能是转存当天，如果混进正常时间线，
    会让"按时间找照片"这个核心用途失效。单独放可以让用户一眼看出哪些
    需要人工复核。
    """
    if info.taken_at is None:
        return [unknown_dir]
    dt = info.taken_at
    if info.is_uncertain:
        return [uncertain_dir, f"{dt.year:04d}", f"{dt.year:04d}-{dt.month:02d}"]
    return [f"{dt.year:04d}", f"{dt.year:04d}-{dt.month:02d}",
            f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"]


def time_confidence_label(confidence: int) -> str:
    return {3: "高（EXIF）", 2: "中（容器/文件名）", 1: "低（mtime，存疑）", 0: "无"}.get(confidence, "无")
