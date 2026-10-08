"""通道层：只负责"枚举文件"和"读字节"。

按用户确认（Q1=B、Q2=C），本项目只实现 ADB 通道的两种连接方式：
    - :class:`channels.usb_adb.UsbAdbSource`   —— USB 数据线
    - :class:`channels.wifi_adb.WifiAdbSource` —— WiFi（ADB over TCP/IP）

MTP 兜底按 Q2 决定不做；手机浏览器上传按 Q1 决定不做（改为本机网页只读查看）。
"""

from __future__ import annotations

from .adb_client import AdbClient, human_adb_hint, sh_quote
from .adb_source import AdbSource
from .base import Capabilities, ContentSource, DeviceInfo, ScanPlan, ScanRoot
from .usb_adb import UsbAdbSource
from .wifi_adb import WifiAdbSource, parse_host_port

__all__ = [
    "AdbClient", "AdbSource", "Capabilities", "ContentSource", "DeviceInfo",
    "ScanPlan", "ScanRoot", "UsbAdbSource", "WifiAdbSource",
    "human_adb_hint", "sh_quote", "parse_host_port",
]
