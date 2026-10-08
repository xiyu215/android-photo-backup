"""生成测试用假数据（含各种边界情况）。

覆盖的边界
----------
    - 正常 JPEG（带 EXIF DateTimeOriginal）
    - 文件名含 emoji
    - 超长文件名（触发 Windows 260 字符限制的压缩逻辑）
    - 重名但内容不同（触发 _1 后缀）
    - 编辑双版本（IMG_x.jpg / IMG_x_edit.jpg，**不得**被当成重复）
    - 完全相同的两份文件（触发秒传）
    - 零字节文件（应跳过）
    - 缓存目录 / .thumbnails / .nomedia 目录（应排除）
    - 超过单文件上限的大文件（应拒绝并跳过）
    - 无 EXIF 的文件（时间退回文件名 → mtime）

用法： python tools/gen_testdata.py <目标目录>
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path


def make_exif_bytes(datetime_str: str) -> bytes:
    """手工构造最小可用的 EXIF（只含 DateTimeOriginal）。

    为什么要手工构造？
        不想为了测试再引入 piexif 之类的依赖。EXIF 的结构本身很简单：
        TIFF 头 + IFD0（指向 Exif 子 IFD）+ Exif 子 IFD（含 0x9003 标签）。
    """
    value = (datetime_str + "\x00").encode("ascii")
    ifd0_off, exif_ifd_off, str_off = 8, 26, 44

    tiff = bytearray()
    tiff += b"MM\x00\x2a" + struct.pack(">I", ifd0_off)      # TIFF 头（大端）
    tiff += struct.pack(">H", 1)                              # IFD0: 1 个条目
    tiff += struct.pack(">HHI", 0x8769, 4, 1) + struct.pack(">I", exif_ifd_off)
    tiff += struct.pack(">I", 0)                              # 无下一个 IFD
    tiff += struct.pack(">H", 1)                              # Exif IFD: 1 个条目
    tiff += struct.pack(">HHI", 0x9003, 2, len(value)) + struct.pack(">I", str_off)
    tiff += struct.pack(">I", 0)
    tiff += value
    return bytes(tiff)


def write_jpeg(path: Path, *, exif_datetime: str | None = None, size: int = 0) -> None:
    """写一个"看起来像 JPEG"的最小文件（有 SOI/APP1/SOS 结构，足够解析）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray()
    data += b"\xff\xd8"                                        # SOI
    if exif_datetime:
        payload = b"Exif\x00\x00" + make_exif_bytes(exif_datetime)
        data += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    data += b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(64)  # DQT
    data += b"\xff\xda" + struct.pack(">H", 8) + bytes(6)              # SOS
    data += b"\x00" * max(0, size)                                     # "压缩数据"
    data += b"\xff\xd9"                                                # EOI
    path.write_bytes(bytes(data))


def write_video(path: Path, *, creation_secs: int | None = None,
                when: tuple | None = None, size: int = 4096) -> None:
    """写一个含 moov/mvhd 的最小 MP4（足够被我们的解析器识别）。

    :param creation_secs: 自 **1904-01-01**（MP4 纪元）起的秒数。
    :param when: 也可直接给 ``(年, 月, 日, 时, 分)``，内部换算成 MP4 纪元秒数。
    """
    if when is not None:
        from datetime import datetime

        creation_secs = int((datetime(*when) - datetime(1904, 1, 1)).total_seconds())
    if creation_secs is None:
        raise ValueError("必须提供 creation_secs 或 when")

    path.parent.mkdir(parents=True, exist_ok=True)

    mvhd_payload = struct.pack(">B3sIQ", 0, b"\x00\x00\x00", creation_secs, creation_secs)
    mvhd_payload += b"\x00" * 80
    mvhd = struct.pack(">I", len(mvhd_payload) + 8) + b"mvhd" + mvhd_payload
    moov = struct.pack(">I", len(mvhd) + 8) + b"moov" + mvhd
    ftyp = struct.pack(">I", 16) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00"
    free = struct.pack(">I", size + 8) + b"free" + b"\x00" * size
    path.write_bytes(ftyp + moov + free)


def build(phone_root: Path) -> dict:
    """在 ``phone_root`` 下构造完整的假手机目录树。"""
    if phone_root.exists():
        import shutil

        shutil.rmtree(phone_root)
    phone_root.mkdir(parents=True, exist_ok=True)

    cam = phone_root / "DCIM" / "Camera"
    shots = phone_root / "Pictures" / "Screenshots"
    wechat = phone_root / "Pictures" / "WeiXin"
    thumbs = phone_root / "DCIM" / ".thumbnails"
    cached = phone_root / "Pictures" / "cache"
    nomedia_dir = phone_root / "Pictures" / "AppSecret"
    download = phone_root / "Download"
    movies = phone_root / "Movies"

    # 先把所有目录建好（后面的 write_bytes 不会自动创建父目录）
    for d in (cam, shots, wechat, thumbs, cached, nomedia_dir, download, movies):
        d.mkdir(parents=True, exist_ok=True)

    # 1) 带 EXIF 的正常照片
    write_jpeg(cam / "IMG_20230715_123456.jpg", exif_datetime="2023:07:15 12:34:56", size=3000)
    # 2) 编辑双版本（必须被当成两个不同文件）
    write_jpeg(cam / "IMG_20230715_123456_edit.jpg", exif_datetime="2023:07:15 12:34:56", size=3200)
    # 3) 完全相同的两份（应触发秒传）
    same = b"identical-content-" + b"x" * 5000
    (cam / "IMG_20230801_080000.jpg").write_bytes(same)
    (wechat / "mmexport1690848000000.jpg").write_bytes(same)
    # 4) emoji 文件名
    write_jpeg(cam / "生日蛋糕🎂_20230901_101010.jpg", exif_datetime="2023:09:01 10:10:10", size=2000)
    # 5) 超长文件名（触发路径压缩）
    long_name = ("超长文件名" * 30) + "_20230902_101010.jpg"
    write_jpeg(cam / long_name, exif_datetime="2023:09:02 10:10:10", size=1500)
    # 6) 无 EXIF，时间靠文件名
    write_jpeg(shots / "Screenshot_2023-09-03-14-25-36.png", size=1200)
    # 7) 无 EXIF 也无文件名时间 → 时间存疑（mtime）
    (download / "random_picture.jpg").write_bytes(b"no-exif-no-time" + b"y" * 900)
    # 8) 视频（mvhd 的创建时间按 MP4 纪元 = 1904-01-01 计算）
    write_video(movies / "VID_20230904_090000.mp4", when=(2023, 9, 4, 1, 0), size=2048)
    # 9) 零字节文件（应跳过）
    (cam / "IMG_empty.jpg").write_bytes(b"")
    # 10) 缓存目录（应排除）
    write_jpeg(thumbs / "thumb_1.jpg", size=100)
    write_jpeg(cached / "cache_1.jpg", size=100)
    # 11) .nomedia 目录（应排除）
    write_jpeg(nomedia_dir / "secret.jpg", exif_datetime="2023:09:05 09:09:09", size=800)
    (nomedia_dir / ".nomedia").write_bytes(b"")
    # 12) 超大文件（超过上限时应被拒绝；默认上限 4GB，测试里会调小配置）
    write_jpeg(cam / "IMG_huge_20230906_060606.jpg", exif_datetime="2023:09:06 06:06:06", size=3_000_000)
    # 13) 非媒体文件（应跳过）
    (cam / "notes.txt").write_text("hello", encoding="utf-8")

    return {
        "root": phone_root,
        "huge": cam / "IMG_huge_20230906_060606.jpg",
    }


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_testdata/phone")
    info = build(target)
    total = sum(1 for _ in target.rglob("*") if _.is_file())
    print(f"已在 {info['root']} 生成 {total} 个文件")
