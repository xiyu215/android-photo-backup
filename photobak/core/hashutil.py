"""哈希工具：分块 SHA-256（内存恒定）。

为什么不用一次性 ``hashlib.sha256(f.read())``？
    用户的数据规模可能到 100GB、单个视频可能 4GB+。一次性读取会把整个文件
    塞进内存，在 32 位进程或小内存机器上直接崩溃。这里统一按 8MB 分块流式
    计算，内存占用恒定在 8MB 左右，与文件大小无关。

为什么分块大小选 8MB？
    - 太小（如 64KB）：Python 层循环次数暴增，CPU 开销明显；
    - 太大（如 128MB）：单次读取延迟高、进度条更新迟钝、内存占用上升；
    - 8MB 是实测吞吐与响应性的平衡点，且与断点续传的读取块大小保持一致，
      便于复用同一套进度上报逻辑。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Protocol

from ..common.logger import get_logger

log = get_logger("hash")

#: 分块大小（字节）。同时作为传输与校验的默认块大小。
CHUNK_SIZE = 8 * 1024 * 1024

#: 清单里展示的哈希前缀长度（完整值仍存库）
DISPLAY_PREFIX = 16

#: 秒传初筛用的"快速指纹"取样大小（首尾各取这么多字节）
_FINGERPRINT_SAMPLE = 1024 * 1024


class _Canceled(Exception):
    """内部信号：用户取消。"""


# 进度回调签名： (已处理字节数, 总字节数)
ProgressCb = Callable[[int, int], None]
# 取消检查签名：返回 True 表示应当中止
CancelCb = Callable[[], bool]


class Readable(Protocol):
    def read(self, n: int) -> bytes: ...


@dataclass
class HashResult:
    """一次哈希计算的结果。"""

    sha256: str
    size: int

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"{self.sha256[:DISPLAY_PREFIX]}…({self.size} 字节)"


def hash_file(
    path: str | Path,
    *,
    chunk_size: int = CHUNK_SIZE,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> HashResult:
    """计算本地文件的分块 SHA-256。

    :param progress: 每处理完一块回调一次 (done, total)。
    :param cancel: 返回 True 时抛出 :class:`_Canceled` 中止（由上层转成用户提示）。
    """
    p = Path(path)
    total = p.stat().st_size
    h = hashlib.sha256()
    done = 0
    with open(p, "rb", buffering=0) as fh:
        while True:
            if cancel and cancel():
                raise _Canceled()
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if progress and done == 0:
        progress(0, 0)  # 空文件也要报一次进度，避免进度条卡住
    return HashResult(h.hexdigest(), total)


def hash_stream(
    reader: Readable,
    *,
    total: int = 0,
    chunk_size: int = CHUNK_SIZE,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> HashResult:
    """对任意可读流计算 SHA-256（用于手机侧字节流）。"""
    h = hashlib.sha256()
    done = 0
    while True:
        if cancel and cancel():
            raise _Canceled()
        chunk = reader.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
        done += len(chunk)
        if progress:
            progress(done, total or done)
    return HashResult(h.hexdigest(), done)


def hash_bytes(data: bytes) -> str:
    """计算内存中数据的 SHA-256（小数据用）。"""
    return hashlib.sha256(data).hexdigest()


def iter_chunks(path: str | Path, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """按块迭代文件内容（供上传/复制复用）。"""
    with open(path, "rb", buffering=0) as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


def copy_stream(
    src: Readable,
    dst_path: str | Path,
    *,
    total: int = 0,
    chunk_size: int = CHUNK_SIZE,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    mode: str = "wb",
    start_offset: int = 0,
) -> HashResult:
    """把流写入本地文件，同时计算其 SHA-256。

    "边写边算"是刻意的：避免为了校验再把刚写完的大文件完整读一遍
    （除非用户开启了写后复读校验，那是有意为之的额外保险）。
    """
    p = Path(dst_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    done = 0
    if start_offset:
        # 续传场景：先对已落盘部分补算哈希，再继续追加
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                done += len(chunk)
    with open(p, mode, buffering=0) as fh:
        while True:
            if cancel and cancel():
                fh.flush()
                raise _Canceled()
            chunk = src.read(chunk_size)
            if not chunk:
                break
            fh.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total or done)
        fh.flush()
        os.fsync(fh.fileno())  # 关键：先真正落盘，再由上层写索引
    return HashResult(h.hexdigest(), done)


def quick_fingerprint(path: str | Path) -> str:
    """快速指纹：``大小-首1MB哈希-尾1MB哈希``。

    用途：**仅**作为"这次要不要算完整 SHA-256"的初筛提示，
    **绝不**用于去重判定——去重只认完整 SHA-256（用户明确要求）。
    对 150 张、单张 15MB 的场景其实用不上，保留是为了将来大规模数据时省时间。
    """
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(p, "rb", buffering=0) as fh:
        head = fh.read(min(_FINGERPRINT_SAMPLE, size))
        h.update(head)
        if size > _FINGERPRINT_SAMPLE:
            fh.seek(max(0, size - _FINGERPRINT_SAMPLE))
            h.update(fh.read(_FINGERPRINT_SAMPLE))
    return h.hexdigest()


def normalize_sha256(value: str | None) -> str | None:
    """规范化远端返回的哈希字符串。

    手机侧 ``sha256sum`` 的输出形如 ``<hash>  <filename>``，这里只取第一段，
    并校验长度与字符集，避免把解析错误当成"校验不一致"（那会导致无谓重传）。
    """
    if not value:
        return None
    token = str(value).strip().split()[0] if str(value).strip() else ""
    token = token.lower()
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    return None


def short(sha: str | None) -> str:
    """取哈希前缀用于展示。"""
    return (sha or "")[:DISPLAY_PREFIX]
