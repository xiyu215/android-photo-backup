"""ADB 内容源：USB 与 WiFi 两条通道共用的实现。

两条通道的差别只有"设备是怎么连上的"（数据线 vs 网络），一旦连上，
枚举、取哈希、拉取字节流、推送、shell 全部完全一致。因此这里实现一次，
:mod:`channels.usb_adb` 与 :mod:`channels.wifi_adb` 只负责建立连接。

文件枚举的实现取舍
------------------
优先用 ``find ... -printf``（一次调用拿到 大小/时间/路径，最快）；
若设备的 toybox 不支持 ``-printf``，自动回退到 ``find -print0 | xargs -0 stat``。
两种模式都在**启动时探测一次并缓存**，避免每个目录都试错。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from ..common import paths
from ..common.errors import LinkError, TransferError
from ..common.logger import get_logger
from ..core.hashutil import CHUNK_SIZE, normalize_sha256
from ..core.scanner import RemoteFile
from .adb_client import AdbClient, sh_quote
from .base import Capabilities, ContentSource, DeviceInfo

log = get_logger("adb-source")

#: 单次传输无数据的超时（秒）→ 判定为卡死（E207）
_STALL_TIMEOUT = 120.0

#: 读取缓冲块大小
_IO_CHUNK = 1024 * 1024


class AdbSource(ContentSource):
    """基于 ADB 的通用内容源。"""

    name = "ADB"

    def __init__(self, client: AdbClient, serial: str) -> None:
        self.client = client
        self.serial = serial
        self._device: DeviceInfo | None = None
        self._printf_ok: bool | None = None      # find -printf 是否可用
        self._sha_tool: str | None = None        # sha256sum / toybox sha256sum

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def probe(self) -> DeviceInfo:
        if self._device is not None:
            return self._device

        model = (self.client.getprop("ro.product.model", serial=self.serial)
                 or self.client.getprop("ro.product.brand", serial=self.serial)
                 or "安卓设备").strip()
        sdk_raw = self.client.getprop("ro.build.version.sdk", serial=self.serial).strip()
        release = self.client.getprop("ro.build.version.release", serial=self.serial).strip()
        try:
            sdk = int(sdk_raw)
        except (TypeError, ValueError):
            sdk = 0

        # 存储根：绝大多数设备是 /sdcard，个别设备用 /storage/emulated/0
        storage = "/sdcard"
        try:
            probe = self.client.shell("[ -d /sdcard ] && echo ok || echo no",
                                      serial=self.serial, timeout=15).strip()
            if probe != "ok":
                storage = "/storage/emulated/0"
        except LinkError:
            pass

        self._device = DeviceInfo(
            serial=self.serial, model=model, android_version=sdk,
            android_release=release, storage_root=storage,
            display_name=model,
        )
        log.info("设备：%s（Android %s / API %d），存储根 %s",
                 model, release or "未知", sdk, storage)
        return self._device

    def capabilities(self) -> Capabilities:
        return Capabilities(auto_enumerate=True, remote_hash=True,
                            range_read=True, push=True)

    # ------------------------------------------------------------------
    # 枚举
    # ------------------------------------------------------------------
    def list_files(
        self,
        roots: list[str],
        *,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> Iterator[RemoteFile]:
        device = self.probe()
        base = device.storage_root.rstrip("/")
        count = 0

        for root in roots:
            remote_root = root if root.startswith("/") else f"{base}/{root}"
            if on_progress:
                on_progress(remote_root, count)
            log.debug("枚举目录：%s", remote_root)

            for rf in self._walk(remote_root, base):
                count += 1
                if on_progress and count % 50 == 0:
                    on_progress(remote_root, count)
                yield rf

        if on_progress:
            on_progress("", count)

    def _walk(self, remote_root: str, base: str) -> Iterator[RemoteFile]:
        """枚举单个根目录下的所有文件。"""
        if self._printf_ok is None:
            self._printf_ok = self._probe_printf(remote_root)

        lines = self._find_lines(remote_root)
        prefix = base.rstrip("/") + "/"
        for size, mtime, path in lines:
            if not path.startswith(prefix):
                # 极少数设备会把 /sdcard 报成 /storage/emulated/0 的软链形式
                if path.startswith("/sdcard/"):
                    rel = path[len("/sdcard/"):]
                else:
                    continue
            else:
                rel = path[len(prefix):]
            yield RemoteFile(path=path, rel=rel, size=size, mtime=mtime)

    def _probe_printf(self, root: str) -> bool:
        """探测该设备 toybox 的 ``find`` 是否支持 ``-printf``。"""
        try:
            out = self.client.shell(
                f"find {sh_quote(root)} -maxdepth 0 -printf 'PHOTOBAK_OK\\n' 2>/dev/null",
                serial=self.serial, timeout=20,
            )
            ok = "PHOTOBAK_OK" in out
        except LinkError:
            ok = False
        log.info("find -printf 支持情况：%s（%s）",
                 "支持" if ok else "不支持，改用 stat 回退模式",
                 "快速模式" if ok else "兼容模式")
        return ok

    def _find_lines(self, root: str) -> Iterator[tuple[int, float | None, str]]:
        """执行 find 并解析出 (size, mtime, path)。"""
        q = sh_quote(root)
        if self._printf_ok:
            cmd = f"find {q} -type f -printf '%s\\t%T@\\t%p\\n' 2>/dev/null"
        else:
            # 兼容模式：print0 + xargs stat（tab 分隔，路径里含 tab 也能靠 maxsplit 保留）
            cmd = (f"find {q} -type f -print0 2>/dev/null | "
                   f"xargs -0 -r stat -c '%s\\t%Y\\t%n' 2>/dev/null")

        try:
            for line in self.client.stream_lines(cmd, serial=self.serial):
                line = line.rstrip("\r\n")
                if not line:
                    continue
                parts = line.split("\t", 2)
                if len(parts) < 3:
                    continue
                try:
                    size = int(parts[0])
                except ValueError:
                    continue
                try:
                    mtime = float(parts[1])
                except ValueError:
                    mtime = None
                yield size, mtime, parts[2]
        except LinkError as exc:
            # 单个根目录枚举失败不应中断整体（例如该目录不存在）
            log.warning("枚举 %s 失败：%s", root, exc)

    # ------------------------------------------------------------------
    # 哈希
    # ------------------------------------------------------------------
    def remote_sha256(self, remote_path: str) -> str | None:
        if self._sha_tool is None:
            self._sha_tool = self._detect_sha_tool()
        if not self._sha_tool:
            return None
        try:
            out = self.client.shell(
                f"{self._sha_tool} {sh_quote(remote_path)}",
                serial=self.serial, timeout=300,
            )
        except LinkError as exc:
            log.debug("远端哈希失败 %s：%s", remote_path, exc)
            return None
        value = normalize_sha256(out)
        if value is None:
            log.debug("远端哈希输出无法解析：%r", out[:120])
        return value

    def _detect_sha_tool(self) -> str:
        """探测设备上可用的 sha256 工具。"""
        for tool in ("sha256sum", "toybox sha256sum", "busybox sha256sum"):
            try:
                out = self.client.shell(f"echo x | {tool} 2>/dev/null",
                                        serial=self.serial, timeout=15)
            except LinkError:
                continue
            if normalize_sha256(out):
                log.info("设备可用的哈希工具：%s", tool)
                return tool
        log.warning("设备上没有可用的 sha256sum，本次将关闭秒传（改为传输后本地校验）")
        return ""

    # ------------------------------------------------------------------
    # 拉取
    # ------------------------------------------------------------------
    def fetch(
        self,
        remote_path: str,
        dest_part: str | Path,
        *,
        offset: int = 0,
        expected_size: int | None = None,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> int:
        """把远端文件拉到本地，支持从 ``offset`` 续传。

        为什么用 ``tail -c +N`` 而不是 ``dd``？
            断点可能落在任意字节位置（不一定是块大小的整数倍），
            ``tail -c +N`` 支持任意偏移且是纯流式复制，最省事也最稳。
            仅在设备没有 tail 时回退到 dd。
        """
        dest = Path(dest_part)
        paths.ensure_dir(dest.parent)
        mode = "ab" if offset and dest.exists() else "wb"
        if offset and not dest.exists():
            offset = 0
            mode = "wb"

        q = sh_quote(remote_path)
        cmd = f"tail -c +{offset + 1} {q}" if offset >= 0 else f"cat {q}"

        proc = self.client.popen_exec_out(cmd, serial=self.serial)
        assert proc.stdout is not None

        written = 0
        heartbeat = time.time()
        stop_flag = threading.Event()
        read_error: list[BaseException] = []

        def _watchdog() -> None:
            """卡死检测：超过阈值没有任何数据流动就杀掉进程（E207）。"""
            while not stop_flag.wait(5.0):
                if time.time() - heartbeat > _STALL_TIMEOUT:
                    log.warning("传输超过 %.0f 秒无数据，判定为卡死，正在中断", _STALL_TIMEOUT)
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    return

        watcher = threading.Thread(target=_watchdog, name="adb-watchdog", daemon=True)
        watcher.start()

        total = (expected_size or 0)
        try:
            with open(dest, mode, buffering=0) as fh:
                while True:
                    if cancel and cancel():
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass
                        raise TransferError("E204", "用户已取消当前传输")
                    chunk = proc.stdout.read(_IO_CHUNK)
                    if not chunk:
                        break
                    heartbeat = time.time()
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(offset + written, total or (offset + written))
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            stop_flag.set()
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

        err_text = ""
        try:
            if proc.stderr is not None:
                err_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            pass

        if written == 0 and expected_size:
            if "no such file" in err_text.lower() or "not found" in err_text.lower():
                raise TransferError("E204", f"文件在手机端已不存在：{remote_path}")
            raise TransferError("E203", err_text or f"读取不到任何数据：{remote_path}")

        return written

    # ------------------------------------------------------------------
    # 反向下载
    # ------------------------------------------------------------------
    def push(self, local: str | Path, remote: str) -> None:
        self.client.push(local, remote, serial=self.serial)

    def shell(self, command: str, timeout: float = 60.0) -> str:
        return self.client.shell(command, serial=self.serial, timeout=timeout)

    def free_space(self, remote_dir: str) -> int:
        return self.client.df_free(remote_dir, serial=self.serial)

    # ------------------------------------------------------------------
    def close(self) -> None:
        pass
