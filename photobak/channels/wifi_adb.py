"""WiFi 通道（ADB over TCP/IP）。

按用户确认的 Q1=B：WiFi 走 **ADB over WiFi**，而不是"手机浏览器上传"。
这样两条通道的能力完全对等（都能自动枚举相册、都能秒传、都能断点续传），
而且**手机端不需要安装任何东西**（用户明确要求）。

两条建立连接的路径
------------------
① **无线调试配对码（推荐，Android 11+，无需数据线）**
     手机：开发者选项 → 无线调试 → 使用配对码配对设备
     屏幕上会显示「IP:配对端口」与 6 位配对码 → 输入本程序即可完成配对。
     配对成功后，用「无线调试」页面上显示的「IP:连接端口」建立连接。

② **adb tcpip 5555（备选，需要先插一次数据线）**
     先用数据线连一次并执行 ``adb tcpip 5555``，之后拔掉数据线，
     在同一 WiFi 下用 ``adb connect <手机IP>:5555`` 连接。

为什么优先路径①？
    Android 11 起，``adb tcpip`` 在某些机型上会被限制；而"无线调试"是官方
    提供的正规入口，在澎湃OS / Android 17 上更可靠，且完全不需要数据线。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ..common import netutil
from ..common.errors import LinkError
from ..common.logger import get_logger
from .adb_client import AdbClient, human_adb_hint
from .adb_source import AdbSource

log = get_logger("wifi")

#: 无线调试的连接端口在 Android 11+ 是随机高位端口；配对端口也是随机的。
_HOST_PORT_RE = re.compile(r"^\s*([0-9a-zA-Z_.\-]+):(\d{1,5})\s*$")


def parse_host_port(text: str) -> tuple[str, int]:
    """解析用户从手机上抄下来的 ``IP:端口``。

    用户很可能把手机上显示的一整行（含中文说明）粘贴进来，所以这里做宽松解析：
    先用正则找 ``ip:port`` 片段，再退回"按冒号切分"。
    """
    raw = (text or "").strip().strip('"').strip()
    m = _HOST_PORT_RE.match(raw)
    if m:
        return m.group(1), int(m.group(2))
    m2 = re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3}):(\d{1,5})", raw)
    if m2:
        return m2.group(1), int(m2.group(2))
    # 只给了 IP，端口留空由调用方补
    m3 = re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", raw)
    if m3:
        return m3.group(1), 0
    raise ValueError(f"无法从「{text}」中解析出 IP 和端口，请照手机上的显示原样输入")


class WifiAdbSource(AdbSource):
    """通过局域网连接的 ADB 设备。"""

    name = "WiFi(ADB)"

    # ------------------------------------------------------------------
    # 路径①：无线调试配对码
    # ------------------------------------------------------------------
    @classmethod
    def pair_and_connect(
        cls,
        *,
        pair_target: str,
        pair_code: str,
        connect_target: str | None = None,
        adb_exe: str | Path | None = None,
    ) -> "WifiAdbSource":
        """用配对码完成配对并连接。

        :param pair_target:    手机「使用配对码配对设备」页显示的 ``IP:端口``
        :param pair_code:      手机显示的 6 位配对码
        :param connect_target: 手机「无线调试」主页显示的 ``IP:端口``；
                               不传则用配对 IP + 默认端口猜测（可能失败）。
        """
        client = AdbClient(adb_exe)
        client.start_server()

        try:
            pair_ip, pair_port = parse_host_port(pair_target)
        except ValueError as exc:
            raise LinkError("E113", str(exc)) from exc
        if not pair_port:
            raise LinkError("E113", "配对地址缺少端口，请把手机上的「IP:端口」完整抄下来")

        code = str(pair_code).strip().replace(" ", "")
        if not (code.isdigit() and len(code) == 6):
            raise LinkError("E113", f"配对码应当是 6 位数字，你输入的是「{pair_code}」")

        log.info("正在配对：%s:%d …", pair_ip, pair_port)
        ok, msg = client.pair(f"{pair_ip}:{pair_port}", code)
        if not ok:
            raise LinkError("E113", msg or "配对失败")
        log.info("配对成功")

        # 连接：优先用用户给的连接端口；没给就用同 IP 的常见端口探测
        candidates: list[int] = []
        if connect_target:
            try:
                c_ip, c_port = parse_host_port(connect_target)
                if c_port:
                    candidates.append(c_port)
            except ValueError:
                pass
        candidates += [5555, 37000, 37100]

        last = ""
        for port in dict.fromkeys(candidates):
            ok, msg = client.connect(f"{pair_ip}:{port}", timeout=15)
            if ok:
                log.info("已连接：%s:%d", pair_ip, port)
                return cls(client, f"{pair_ip}:{port}")
            last = msg
            time.sleep(0.4)

        raise LinkError(
            "E113",
            f"配对成功但连接失败（最后一条：{last}）。请把手机「无线调试」主页上"
            f"显示的「IP 地址和端口」也填进来，通常是 3xxxx 端口。",
        )

    # ------------------------------------------------------------------
    # 路径②：先 USB 开启 tcpip，再无线连接
    # ------------------------------------------------------------------
    @classmethod
    def enable_tcpip_over_usb(
        cls, *, port: int = 5555, adb_exe: str | Path | None = None, serial: str | None = None,
    ) -> str:
        """通过 USB 执行 ``adb tcpip``，返回手机的局域网 IP（供后续连接）。"""
        client = AdbClient(adb_exe)
        client.start_server()
        entries = [e for e in client.devices() if e.state == "device"]
        if not entries:
            raise LinkError("E101", "开启无线调试需要先用数据线连接手机")
        target = entries[0]
        if serial:
            for e in entries:
                if e.serial == serial:
                    target = e
                    break

        ok, msg = client.tcpip(port, serial=target.serial)
        if not ok:
            raise LinkError("E113", f"adb tcpip 失败：{msg}")

        ip = _device_lan_ip(client, target.serial)
        if not ip:
            raise LinkError(
                "E108",
                "已切换为无线模式，但读不到手机的局域网 IP。请确认手机已连接 WiFi，"
                "然后在手机上查看「设置 → WLAN → 当前网络 → IP 地址」，手动输入。",
            )
        log.info("无线模式已开启，手机 IP：%s:%d（现在可以拔掉数据线了）", ip, port)
        return f"{ip}:{port}"

    @classmethod
    def connect(cls, target: str, *, adb_exe: str | Path | None = None,
                retries: int = 3) -> "WifiAdbSource":
        """直接连接一个已知的 ``IP:端口``。"""
        client = AdbClient(adb_exe)
        client.start_server()
        try:
            ip, port = parse_host_port(target)
        except ValueError as exc:
            raise LinkError("E108", str(exc)) from exc
        port = port or 5555
        addr = f"{ip}:{port}"

        last = ""
        for attempt in range(1, max(1, retries) + 1):
            ok, msg = client.connect(addr, timeout=20)
            if ok:
                # adb connect 会立刻返回成功，但设备可能还没就绪，轮询确认
                for _ in range(10):
                    for e in client.devices():
                        if e.serial == addr and e.state == "device":
                            log.info("WiFi 通道已连接：%s", addr)
                            return cls(client, addr)
                    time.sleep(0.5)
                last = "连接已建立但设备尚未就绪"
            else:
                last = msg
            log.warning("第 %d/%d 次连接失败：%s", attempt, retries, last)
            time.sleep(1.5)

        # 连接失败时顺手判断一下是不是 AP 隔离
        hint = ""
        if netutil.guess_ap_isolation(ip):
            hint = ("；另外检测到你可能遇到了「AP 隔离」——手机和电脑虽然连同一个 WiFi，"
                    "但互相不通。请登录路由器关闭「AP 隔离/客户端隔离」，"
                    "或改用「手机开热点、电脑连热点」的方式。")
        raise LinkError("E108", f"无法连接 {addr}：{last}{hint}")

    # ------------------------------------------------------------------
    @classmethod
    def connect_with_fallback(
        cls, *, adb_exe: str | Path | None = None, serial: str | None = None,
        port: int = 5555,
    ) -> "WifiAdbSource":
        """一条龙：先用 USB 开启无线，再无线连接。

        适合"用户只有数据线一次、之后想无线跑"的场景。
        """
        addr = cls.enable_tcpip_over_usb(port=port, adb_exe=adb_exe, serial=serial)
        time.sleep(1.5)  # 给设备重启 adbd 一点时间
        return cls.connect(addr, adb_exe=adb_exe)


def _device_lan_ip(client: AdbClient, serial: str) -> str | None:
    """从设备上读取 WiFi 的局域网 IP。

    依次尝试 ``ip route`` / ``ip addr`` / ``ifconfig``，兼容不同 Android 版本
    （Android 10+ 的 toybox 通常有 ``ip``；老设备可能只有 ``ifconfig``）。
    """
    commands = [
        "ip route 2>/dev/null | grep -o 'src [0-9.]*' | head -1",
        "ip -4 addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | head -1",
        "ifconfig wlan0 2>/dev/null | grep -o 'inet addr:[0-9.]*' | head -1",
        "ifconfig wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | head -1",
    ]
    for cmd in commands:
        try:
            out = client.shell(cmd, serial=serial, timeout=15).strip()
        except LinkError:
            continue
        m = re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", out)
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    return None


def adb_pair_hint() -> str:
    """配对失败时的通用指引（配合 :data:`common.i18n.MSG_WIFI_PAIR_GUIDE` 使用）。"""
    return human_adb_hint(LinkError("E113", "pair failed"))
