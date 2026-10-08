"""USB 通道（ADB 直连）。

前置条件（用户必读）
--------------------
    1. 手机开启「开发者选项」与「USB 调试」；
    2. 用**支持数据传输**的数据线连接；
    3. 手机通知栏 USB 用途选择「传输文件 / MTP」；
    4. 首次连接时在手机上点「允许 USB 调试」（可勾选"始终允许"）。

红米/小米（澎湃OS）特别注意
--------------------------
    澎湃OS 的「USB 调试（安全设置）」需要**登录小米账号并插入 SIM 卡**
    才能打开；这是系统限制，与本工具无关。若授权一直失败，请先确认这一点。

按 Q2 的决定，本项目**不实现 MTP 兜底**：ADB 覆盖绝大多数场景，
若 ADB 不可用，我们给出明确的排查指引，而不是让用户掉进 MTP 的各种机型坑。
"""

from __future__ import annotations

from pathlib import Path

from ..common.errors import LinkError
from ..common.logger import get_logger
from .adb_client import AdbClient, human_adb_hint
from .adb_source import AdbSource

log = get_logger("usb")


class UsbAdbSource(AdbSource):
    """通过 USB 数据线连接的 ADB 设备。"""

    name = "USB(ADB)"

    @classmethod
    def connect(cls, adb_exe: str | Path | None = None, *, serial: str | None = None,
                wait_seconds: float = 0.0) -> "UsbAdbSource":
        """探测并连接一台 USB 设备。

        :param serial: 指定序列号（多设备时使用）；None 表示自动选第一台。
        :param wait_seconds: 若无设备，等待多少秒后重试一次（用于"刚插上还没就绪"）。
        """
        client = AdbClient(adb_exe)
        try:
            client.start_server()
        except LinkError as exc:
            raise LinkError("E105", f"{exc.detail or exc}；{human_adb_hint(exc)}") from exc

        entries = client.devices()
        target = _pick_device(entries, serial, wait_seconds=wait_seconds, client=client)

        log.info("USB 通道已连接：%s（%s）", target.serial, target.model or "未知型号")
        return cls(client, target.serial)


def _pick_device(entries, serial: str | None, *, wait_seconds: float, client: AdbClient):
    """从设备列表里挑一台可用的，并把各种异常状态翻译成可执行提示。"""
    import time

    def _lookup():
        return client.devices()

    if not entries and wait_seconds > 0:
        time.sleep(wait_seconds)
        entries = _lookup()

    if serial:
        for e in entries:
            if e.serial == serial:
                _ensure_ready(e)
                return e
        raise LinkError("E101", f"未找到序列号为 {serial} 的设备")

    if not entries:
        raise LinkError("E101", "adb devices 未返回任何设备")

    # 优先挑状态正常的
    for e in entries:
        if e.state == "device":
            return e

    # 没有正常设备 → 按具体状态给提示
    e = entries[0]
    _ensure_ready(e)
    return e


def _ensure_ready(entry) -> None:
    """检查设备状态，异常时抛出带具体解决办法的异常。"""
    state = entry.state
    if state == "device":
        return
    if state == "unauthorized":
        raise LinkError("E103", f"设备 {entry.serial} 未授权 USB 调试")
    if state == "offline":
        raise LinkError("E107", f"设备 {entry.serial} 处于 offline 状态")
    if "permission" in state:
        raise LinkError("E104", f"设备 {entry.serial} 权限不足（{state}）")
    raise LinkError("E101", f"设备 {entry.serial} 状态异常：{state}")
