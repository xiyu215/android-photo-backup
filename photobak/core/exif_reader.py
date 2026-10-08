"""照片/视频的"拍摄时间"读取（纯 Python，不依赖外部命令行工具）。

为什么自己写解析器？
    用户要求"目标电脑零依赖"。mediainfo / ffprobe / exiftool 都是外部程序，
    会显著增加体积与失败面。照片时间只需要读 JPEG 的 EXIF，视频时间只需要读
    MP4 容器的 mvhd 头——两者的解析量都很小，完全可以自己实现。

时间可信度分级（与 :mod:`core.times` 配合）：
    EXIF DateTimeOriginal   → 最可信（相机写入的拍摄时刻）
    MP4 mvhd creation_time  → 较可信（编码器写入）
    文件名时间串            → 中等
    mtime                   → 不可信（会被转存/下载/系统迁移改写）
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common.logger import get_logger

log = get_logger("exif")

# EXIF 的合理时间范围，超出视为无效（很多设备会写入 1970 或 0）
_MIN_YEAR = 1990
_MAX_YEAR = 2100

# 常见 EXIF 时间格式
_EXIF_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M")

# TIFF/EXIF 标签号
_TAG_DATETIME = 0x0132          # IFD0: DateTime（修改时间，次选）
_TAG_EXIF_IFD = 0x8769          # IFD0: 指向 Exif 子 IFD
_TAG_DATETIME_ORIGINAL = 0x9003  # Exif IFD: 原始拍摄时间（首选）
_TAG_DATETIME_DIGITIZED = 0x9004  # Exif IFD: 数字化时间（次选）

# MP4/MOV epoch：1904-01-01 UTC
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _valid(dt: datetime | None) -> datetime | None:
    """时间合理性校验：过滤掉 1970/0/未来时间等明显错误值。"""
    if dt is None:
        return None
    if not (_MIN_YEAR <= dt.year <= _MAX_YEAR):
        return None
    # 允许 1 天的未来偏差（时区换算可能造成）
    if dt > datetime.now() + timedelta(days=1):
        return None
    return dt


def _parse_exif_datetime(text: str) -> datetime | None:
    s = (text or "").strip().strip("\x00")
    if not s or s.startswith("0000"):
        return None
    for fmt in _EXIF_FORMATS:
        try:
            return _valid(datetime.strptime(s, fmt))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# JPEG / EXIF
# ---------------------------------------------------------------------------
def _read_exif_datetime_raw(data: bytes) -> datetime | None:
    """从 JPEG 字节流中解析 EXIF 拍摄时间。

    只做必要的定位：扫 APP1 → 解析 TIFF 头 → 遍历 IFD0 → 进入 Exif IFD
    取 0x9003。不做完整 EXIF 树展开（不需要，也更快）。
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":  # SOI
        return None

    pos = 2
    n = len(data)
    tiff: bytes | None = None
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:  # SOS，图像数据开始，EXIF 不可能在后面
            break
        if pos + 4 > n:
            break
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if seg_len < 2:
            break
        seg = data[pos + 4: pos + 2 + seg_len]
        if marker == 0xE1 and seg.startswith(b"Exif\x00\x00"):
            tiff = seg[6:]
            break
        pos += 2 + seg_len

    if tiff is None:
        return None

    try:
        return _parse_tiff_datetime(tiff)
    except (struct.error, IndexError, ValueError):
        return None


def _parse_tiff_datetime(tiff: bytes) -> datetime | None:
    """解析 TIFF 结构中的时间标签。"""
    if len(tiff) < 8:
        return None
    bo = tiff[0:2]
    if bo == b"II":
        endian = "<"
    elif bo == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
        return None
    ifd0_off = struct.unpack(endian + "I", tiff[4:8])[0]

    def read_ifd(offset: int) -> tuple[dict[int, tuple[int, int, int]], int]:
        """返回 ({tag: (type, count, value_offset)}, 下一 IFD 偏移)。"""
        if offset + 2 > len(tiff):
            return {}, 0
        count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
        entries: dict[int, tuple[int, int, int]] = {}
        base = offset + 2
        for i in range(count):
            e = base + i * 12
            if e + 12 > len(tiff):
                break
            tag, typ, cnt = struct.unpack(endian + "HHI", tiff[e:e + 8])
            val = struct.unpack(endian + "I", tiff[e + 8:e + 12])[0]
            entries[tag] = (typ, cnt, val)
        nxt = 0
        nxt_pos = base + count * 12
        if nxt_pos + 4 <= len(tiff):
            nxt = struct.unpack(endian + "I", tiff[nxt_pos:nxt_pos + 4])[0]
        return entries, nxt

    def read_ascii(entry: tuple[int, int, int]) -> str:
        typ, cnt, val = entry
        if cnt <= 4:
            raw = struct.pack(endian + "I", val)[:cnt]
        else:
            raw = tiff[val:val + cnt]
        return raw.split(b"\x00")[0].decode("ascii", errors="ignore")

    ifd0, _ = read_ifd(ifd0_off)

    # 首选：Exif 子 IFD 里的 DateTimeOriginal
    if _TAG_EXIF_IFD in ifd0:
        exif_off = ifd0[_TAG_EXIF_IFD][2]
        exif_ifd, _ = read_ifd(exif_off)
        for tag in (_TAG_DATETIME_ORIGINAL, _TAG_DATETIME_DIGITIZED):
            if tag in exif_ifd:
                dt = _parse_exif_datetime(read_ascii(exif_ifd[tag]))
                if dt:
                    return dt

    # 次选：IFD0 的 DateTime（通常是"修改时间"，但聊胜于无）
    if _TAG_DATETIME in ifd0:
        return _parse_exif_datetime(read_ascii(ifd0[_TAG_DATETIME]))
    return None


def read_jpeg_time(path: str | Path, *, max_scan: int = 512 * 1024) -> datetime | None:
    """读取 JPEG 的 EXIF 拍摄时间。

    只读取文件头部 ``max_scan`` 字节：EXIF 一定在 APP1 段内，且紧跟在 SOI 之后，
    512KB 足够覆盖任何正常相机的 EXIF（含缩略图）。这样对 15MB 的照片几乎零成本。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(max_scan)
    except OSError:
        return None
    return _read_exif_datetime_raw(head)


def read_photo_time(path: str | Path, ext: str | None = None) -> datetime | None:
    """读取照片拍摄时间（目前支持 JPEG 系；HEIC 等退回 None 由上层兜底）。

    先尝试 Pillow（若环境里有，能覆盖更多格式），失败再走内置解析器。
    这样打包版（带 Pillow）覆盖最广，纯源码版最小依赖也能工作。
    """
    e = (ext or Path(path).suffix).lower().lstrip(".")
    if e in ("jpg", "jpeg", "jpe", "jfif", "tif", "tiff", "png", "webp"):
        try:
            from PIL import Image  # type: ignore

            with Image.open(path) as im:
                exif = im.getexif()
                if exif:
                    for tag in (0x9003, 0x9004, 0x0132):
                        raw = exif.get(tag)
                        if raw:
                            dt = _parse_exif_datetime(str(raw))
                            if dt:
                                return dt
        except Exception:  # noqa: BLE001 —— Pillow 不可用/格式异常都退回内置解析
            pass
        if e in ("jpg", "jpeg", "jpe", "jfif"):
            return read_jpeg_time(path)
    return None


# ---------------------------------------------------------------------------
# MP4 / MOV / 3GP —— mvhd creation_time
# ---------------------------------------------------------------------------
def _iter_atoms(data: bytes, start: int, end: int):
    """迭代 ISO-BMFF 的 box。产出 (type, header_size, box_end)。"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos  # 延伸到文件末尾
        if size < header or pos + size > end:
            return
        yield btype, header, pos, pos + size
        pos += size


def _parse_mvhd(data: bytes, offset: int, end: int) -> datetime | None:
    """解析 mvhd box 的 creation_time。"""
    if offset + 4 > end:
        return None
    version = data[offset]
    if version == 1:
        if offset + 12 > end:
            return None
        secs = struct.unpack(">Q", data[offset + 4:offset + 12])[0]
    else:
        if offset + 8 > end:
            return None
        secs = struct.unpack(">I", data[offset + 4:offset + 8])[0]
    if secs <= 0:
        return None
    try:
        dt_utc = _MP4_EPOCH + timedelta(seconds=secs)
    except (OverflowError, ValueError):
        return None

    # 规范说 creation_time 是 UTC，但不少安卓机型写入的是"本地时间当作 UTC"。
    # 这里按 UTC 转本地；若结果落在未来，则说明写入方已经用了本地时间，
    # 直接去掉时区信息当作本地时间使用。
    dt_local = dt_utc.astimezone().replace(tzinfo=None)
    if dt_local > datetime.now() + timedelta(days=1):
        naive = dt_utc.replace(tzinfo=None)
        if naive <= datetime.now() + timedelta(days=1):
            return _valid(naive)
    return _valid(dt_local)


def read_video_time(path: str | Path, *, max_scan: int = 32 * 1024 * 1024) -> datetime | None:
    """读取 MP4/MOV/3GP 的创建时间。

    需要定位 ``moov`` box。安卓相机通常把 moov 放在文件头；部分机型放在文件尾。
    因此先读头部；找不到再读尾部（最多 32MB，足以覆盖 moov）。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return None

    with open(p, "rb") as fh:
        head = fh.read(min(size, max_scan))
        result = _search_moov(head, 0, len(head))
        if result:
            return result
        if size > max_scan:
            tail_len = min(size, max_scan)
            fh.seek(size - tail_len)
            tail = fh.read(tail_len)
            result = _search_moov(tail, 0, len(tail))
            if result:
                return result
    return None


def _search_moov(data: bytes, start: int, end: int) -> datetime | None:
    """在给定字节区间内寻找 moov → mvhd。"""
    try:
        for btype, header, pos, box_end in _iter_atoms(data, start, end):
            if btype == b"moov":
                inner = pos + header
                for b2, h2, p2, e2 in _iter_atoms(data, inner, box_end):
                    if b2 == b"mvhd":
                        return _parse_mvhd(data, p2 + h2, e2)
    except (struct.error, IndexError):
        return None
    return None


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def read_media_time(path: str | Path, kind: str) -> datetime | None:
    """按媒体类型读取"容器内时间"。读不到返回 None。"""
    if kind == "image":
        return read_photo_time(path)
    if kind == "video":
        return read_video_time(path)
    return None
