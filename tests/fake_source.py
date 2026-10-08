"""测试用的"假手机"内容源。

为什么需要它？
    真实测试需要一台连着数据线的安卓手机，这在 CI / 开发机上不可能随时具备。
    这里用本地目录模拟手机存储，实现与 :class:`channels.base.ContentSource`
    完全相同的接口——于是"扫描 → 哈希 → 秒传 → 传输 → 校验 → 归档 → 索引
    → 清单"整条链路都可以在没有手机的情况下端到端跑通。

    这也是本项目分层设计（通道只负责枚举与读字节）带来的直接好处。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterator

from photobak.channels.base import Capabilities, ContentSource, DeviceInfo
from photobak.core.hashutil import hash_file
from photobak.core.scanner import RemoteFile


class LocalDirSource(ContentSource):
    """把本地某个目录当作"手机存储"。"""

    name = "本地目录（测试用）"

    def __init__(self, phone_root: str | Path, *, model: str = "测试手机",
                 serial: str = "TEST1234", android: int = 34) -> None:
        self.root = Path(phone_root).resolve()
        self.model = model
        self.serial = serial
        self.android = android
        #: 供测试模拟"源端文件被删除/修改"
        self.remote_hash_available = True
        self.fetch_hook: Callable[[str, Path, int], None] | None = None
        self.push_calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    def probe(self) -> DeviceInfo:
        return DeviceInfo(serial=self.serial, model=self.model,
                          android_version=self.android, android_release="14",
                          storage_root=str(self.root))

    def capabilities(self) -> Capabilities:
        return Capabilities(auto_enumerate=True, remote_hash=self.remote_hash_available,
                            range_read=True, push=True)

    # ------------------------------------------------------------------
    def _abs(self, remote: str) -> Path:
        """把"手机路径"映射回本地目录。"""
        rel = str(remote).replace("\\", "/").lstrip("/")
        # 测试里 storage_root 是本地目录，remote 形如 "<本地目录>/DCIM/..."
        rp = Path(str(self.root))
        if rel.startswith(str(rp).replace("\\", "/").lstrip("/")):
            rel = rel[len(str(rp).replace("\\", "/").lstrip("/")):].lstrip("/")
        if rel.startswith("sdcard/"):
            rel = rel[len("sdcard/"):]
        return self.root / rel

    def list_files(self, roots: list[str], *, on_progress=None) -> Iterator[RemoteFile]:
        base = str(self.root).replace("\\", "/")
        for root in roots:
            local_root = self._abs(root)
            if not local_root.is_dir():
                continue
            for p in sorted(local_root.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.root).as_posix()
                st = p.stat()
                if on_progress:
                    on_progress(root, 0)
                yield RemoteFile(path=f"{base}/{rel}", rel=rel,
                                 size=st.st_size, mtime=st.st_mtime)

    # ------------------------------------------------------------------
    def remote_sha256(self, remote_path: str) -> str | None:
        if not self.remote_hash_available:
            return None
        p = self._abs(remote_path)
        if not p.is_file():
            return None
        return hash_file(p).sha256

    def fetch(self, remote_path: str, dest_part, *, offset: int = 0,
              expected_size: int | None = None, progress=None, cancel=None) -> int:
        src = self._abs(remote_path)
        if not src.is_file():
            from photobak.common.errors import TransferError

            raise TransferError("E204", f"文件不存在：{remote_path}")
        if self.fetch_hook:
            self.fetch_hook(remote_path, Path(dest_part), offset)

        dest = Path(dest_part)
        dest.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if offset and dest.exists() else "wb"
        written = 0
        with open(src, "rb") as fh, open(dest, mode) as out:
            if offset:
                fh.seek(offset)
            while True:
                if cancel and cancel():
                    break
                chunk = fh.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if progress:
                    progress(offset + written, expected_size or (offset + written))
        return written

    # ------------------------------------------------------------------
    def push(self, local, remote: str) -> None:
        self.push_calls.append((str(local), remote))
        dst = self._abs(remote)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local), str(dst))

    def shell(self, command: str, timeout: float = 60.0) -> str:
        # 测试环境没有真正的 shell；对存在性检查做最小实现，其余返回空
        if "[ -e" in command:
            target = command.split('"')[1] if '"' in command else ""
            return "yes" if self._abs(target).exists() else "no"
        return ""

    def free_space(self, remote_dir: str) -> int:
        return 64 * 1024 ** 3

    def close(self) -> None:
        pass
