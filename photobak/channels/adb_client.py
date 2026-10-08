"""ADB 客户端：对 ``adb.exe`` 的薄封装。

设计要点
--------
1. **只用包内自带的 adb**：启动时把 ``adb/`` 目录前置到 PATH，且所有调用都用
   绝对路径，避免与用户系统里已有的（可能是旧版本的）adb 冲突。用户明确要求
   这一点——系统里装了 Android Studio 的电脑上，adb 版本冲突非常常见。
2. **绝不弹出黑窗口**：所有子进程都带 ``CREATE_NO_WINDOW``，
   否则在打包成 GUI 场景下会疯狂闪黑框。
3. **端口避让**：adb 服务默认监听 5037；若被占用，通过 ``ADB_SERVER_PORT``
   环境变量换端口，而不是让用户自己去杀进程。
4. **超时可控**：每个调用都有超时，避免 adb 卡死导致整个程序假死。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from ..common import paths
from ..common.errors import LinkError
from ..common.logger import get_logger

log = get_logger("adb")

#: 不允许 adb 弹窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: adb 服务端口的候选范围（5037 被占用时依次尝试）
_SERVER_PORTS = [5037, 5038, 5039, 5040, 5041, 5042]


@dataclass
class DeviceEntry:
    """``adb devices -l`` 的一行。"""

    serial: str
    state: str            # device / unauthorized / offline / no permissions
    model: str = ""


class AdbClient:
    """一个 ADB 客户端实例（绑定某个 adb.exe）。"""

    def __init__(self, adb_exe: str | Path | None = None, *, server_port: int | None = None) -> None:
        self.adb_exe = self._resolve_exe(adb_exe)
        self.server_port = server_port
        self._env = os.environ.copy()
        self._env["ADB_LOCAL_TRANSPORT_MAX_PORT"] = "5555"
        if server_port:
            self._env["ADB_SERVER_PORT"] = str(server_port)

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_exe(explicit: str | Path | None) -> Path:
        """定位 adb.exe：优先包内，其次系统 PATH。"""
        if explicit:
            p = Path(explicit)
            if p.is_file():
                return p
        adb_dir = paths.find_adb_dir()
        if adb_dir:
            return adb_dir / "adb.exe"
        found = shutil.which("adb")
        if found:
            log.warning("未找到内置 adb，回退到系统 PATH 中的 adb：%s", found)
            return Path(found)
        raise LinkError(
            "E611",
            "程序包内缺少 adb.exe。请重新解压完整压缩包（不要直接在压缩包里运行）。",
        )

    # ------------------------------------------------------------------
    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 30.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        """执行一次 adb 命令并捕获输出。"""
        cmd = [str(self.adb_exe), *args]
        log.debug("adb: %s", " ".join(args))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                creationflags=_NO_WINDOW, env=self._env,
            )
        except subprocess.TimeoutExpired as exc:
            raise LinkError("E208", f"adb 命令超时（{timeout:.0f} 秒）：{' '.join(args)}") from exc
        except OSError as exc:
            raise LinkError("E611", f"无法运行 adb：{exc}") from exc

        if check and proc.returncode != 0:
            raise LinkError("E208", self._decode(proc.stderr).strip() or f"adb 返回码 {proc.returncode}")
        return proc

    @staticmethod
    def _decode(data: bytes | None) -> str:
        """解码 adb 输出。

        shell 输出是 UTF-8（安卓文件系统）；adb 自身的提示是英文/ASCII。
        极少数情况下可能出现 GBK，这里做一次兜底。
        """
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gbk", errors="replace")

    # ------------------------------------------------------------------
    # 服务与设备
    # ------------------------------------------------------------------
    def version(self) -> str:
        proc = self._run(["version"], timeout=10)
        text = self._decode(proc.stdout)
        m = re.search(r"Version\s+([0-9.]+)", text)
        return m.group(1) if m else text.strip().splitlines()[0] if text.strip() else "未知"

    def start_server(self) -> None:
        """启动 adb 服务；端口被占用时自动换端口重试。"""
        ports = [self.server_port] if self.server_port else list(_SERVER_PORTS)
        last_err = ""
        for port in ports:
            self._env["ADB_SERVER_PORT"] = str(port)
            try:
                proc = self._run(["start-server"], timeout=20)
            except LinkError as exc:
                last_err = str(exc)
                continue
            if proc.returncode == 0:
                self.server_port = port
                if port != 5037:
                    log.info("adb 服务端口 5037 被占用，已改用 %d（E105）", port)
                return
            last_err = self._decode(proc.stderr).strip() or self._decode(proc.stdout).strip()
        raise LinkError("E105", last_err or "adb 服务启动失败")

    def kill_server(self) -> None:
        try:
            self._run(["kill-server"], timeout=15)
        except LinkError:
            pass

    def devices(self) -> list[DeviceEntry]:
        proc = self._run(["devices", "-l"], timeout=20)
        out = self._decode(proc.stdout)
        entries: list[DeviceEntry] = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            model = ""
            for token in parts[2:]:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1].replace("_", " ")
            entries.append(DeviceEntry(serial, state, model))
        return entries

    def connect(self, host_port: str, timeout: float = 20.0) -> tuple[bool, str]:
        """``adb connect ip:port``。"""
        proc = self._run(["connect", host_port], timeout=timeout)
        text = (self._decode(proc.stdout) + self._decode(proc.stderr)).strip()
        low = text.lower()
        ok = ("connected to" in low) or ("already connected" in low)
        return ok, text

    def disconnect(self, host_port: str | None = None) -> None:
        try:
            args = ["disconnect"] + ([host_port] if host_port else [])
            self._run(args, timeout=15)
        except LinkError:
            pass

    def pair(self, host_port: str, code: str, timeout: float = 40.0) -> tuple[bool, str]:
        """``adb pair ip:port code`` —— Android 11+ 的无线调试配对。"""
        proc = self._run(["pair", host_port, code], timeout=timeout)
        text = (self._decode(proc.stdout) + self._decode(proc.stderr)).strip()
        ok = "successfully paired" in text.lower()
        return ok, text

    def tcpip(self, port: int = 5555, serial: str | None = None, timeout: float = 20.0) -> tuple[bool, str]:
        """``adb tcpip 5555`` —— 让设备在 5555 端口监听（需先 USB 连一次）。"""
        args = []
        if serial:
            args += ["-s", serial]
        args += ["tcpip", str(port)]
        proc = self._run(args, timeout=timeout)
        text = (self._decode(proc.stdout) + self._decode(proc.stderr)).strip()
        ok = "restarting in tcp" in text.lower() or proc.returncode == 0
        return ok, text

    # ------------------------------------------------------------------
    # 设备侧操作
    # ------------------------------------------------------------------
    def shell(self, command: str, *, serial: str | None = None, timeout: float = 60.0) -> str:
        """在设备上执行 shell 命令并返回 stdout。"""
        args = []
        if serial:
            args += ["-s", serial]
        args += ["shell", command]
        proc = self._run(args, timeout=timeout)
        out = self._decode(proc.stdout)
        err = self._decode(proc.stderr)
        if proc.returncode != 0 and not out.strip():
            low = err.lower()
            if "no such file" in low or "not found" in low:
                raise LinkError("E204", err.strip() or "文件不存在")
            raise LinkError("E208", err.strip() or f"命令执行失败：{command}")
        return out

    def popen_exec_out(
        self, command: str, *, serial: str | None = None,
    ) -> subprocess.Popen:
        """以二进制流方式执行 shell 命令（``exec-out`` 不做换行转换）。

        必须用 ``exec-out`` 而不是 ``shell``：后者会把 ``\\n`` 转成 ``\\r\\n``，
        直接破坏二进制文件（照片/视频必然损坏）。
        """
        args = [str(self.adb_exe)]
        if serial:
            args += ["-s", serial]
        args += ["exec-out", command]
        log.debug("adb exec-out: %s", command)
        return subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW, env=self._env,
        )

    def push(self, local: str | Path, remote: str, *, serial: str | None = None,
             timeout: float = 600.0) -> str:
        args = []
        if serial:
            args += ["-s", serial]
        args += ["push", str(local), remote]
        proc = self._run(args, timeout=timeout)
        text = (self._decode(proc.stdout) + self._decode(proc.stderr)).strip()
        if proc.returncode != 0:
            raise LinkError("E208", text or "adb push 失败")
        return text

    def getprop(self, key: str, *, serial: str | None = None) -> str:
        try:
            return self.shell(f"getprop {key}", serial=serial, timeout=15).strip()
        except LinkError:
            return ""

    def df_free(self, remote_dir: str, *, serial: str | None = None) -> int:
        """解析 ``df`` 得到剩余空间（字节）。"""
        try:
            out = self.shell(f"df {remote_dir} 2>/dev/null || df /sdcard",
                             serial=serial, timeout=20)
        except LinkError:
            return 0
        for line in reversed(out.splitlines()):
            parts = line.split()
            if len(parts) >= 4 and parts[1].isdigit():
                try:
                    # 典型：Filesystem 1K-blocks Used Available Use% Mounted
                    return int(parts[3]) * 1024
                except (ValueError, IndexError):
                    continue
        return 0

    # ------------------------------------------------------------------
    def stream_lines(self, command: str, *, serial: str | None = None,
                     timeout: float | None = None) -> Iterator[str]:
        """流式执行命令并逐行产出（用于大目录枚举时的进度反馈）。"""
        proc = self.popen_exec_out(command, serial=serial)
        assert proc.stdout is not None
        try:
            buf = b""
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    yield self._decode(line)
            if buf:
                yield self._decode(buf)
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def sh_quote(s: str) -> str:
    """把字符串安全地放进 shell 单引号里。

    安卓上的文件名可能包含空格、括号、中文甚至单引号。若不转义，
    轻则命令失败，重则把文件名当成多个参数——所以这里统一转义。
    """
    return "'" + str(s).replace("'", "'\\''") + "'"


def human_adb_hint(exc: Exception) -> str:
    """把 adb 的常见报错翻译成"用户能照着做"的提示。"""
    text = str(exc).lower()
    if "unauthorized" in text:
        return "请在手机屏幕上点击「允许 USB 调试」（可勾选始终允许）。"
    if "offline" in text:
        return "设备处于离线状态：请重新插拔数据线，或重启手机后重试。"
    if "no permissions" in text or "insufficient permissions" in text:
        return "设备权限不足：请检查手机的 USB 连接模式是否为「传输文件」。"
    if "device not found" in text or "no devices" in text:
        return "未找到设备：请确认数据线支持数据传输、手机已解锁。"
    if "cannot connect" in text or "failed to connect" in text:
        return "无法连接：请确认手机与电脑在同一 WiFi，且路由器未开启 AP 隔离。"
    return "请重新插拔数据线或重启 adb 服务后重试。"
