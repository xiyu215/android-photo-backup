"""通道抽象层。

为什么要抽象？
    用户要求"USB 直连 / WiFi 扫码两种通道汇入同一个校验与索引引擎，保证规则
    完全一致"。做法就是：**通道只负责"枚举文件"和"读字节"**，所有规则
    （扫描范围、时间解析、去重、校验、归档、分档）全部在 :mod:`core` 里。
    这样即使将来新增第三条通道，业务规则也不需要改一行。

按 Q1/Q2 的确认，本项目只实现两条通道，且两条都是 ADB：
    - :class:`channels.usb_adb.AdbSource` —— 数据线
    - :class:`channels.wifi_adb.WifiAdbSource` —— 无线（ADB over TCP/IP）
MTP 兜底按 Q2 决定不做。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..core.scanner import RemoteFile


@dataclass
class DeviceInfo:
    """一台已连接设备的基本信息。"""

    serial: str
    model: str = "安卓设备"
    android_version: int = 0        # API level，如 34
    android_release: str = ""       # 版本号，如 "14"
    storage_root: str = "/sdcard"
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.model or "安卓设备"


@dataclass
class Capabilities:
    """通道能力声明。

    编排层据此决定是否启用秒传、是否做断点续传；**但校验强度不会降级**：
    即使通道拿不到源端哈希，落盘后依旧会做本地全量校验（只是无法双端比对，
    这种情况会在清单里标注「仅单向校验」）。
    """

    auto_enumerate: bool = True     # 能否自动枚举相册（浏览器通道做不到）
    remote_hash: bool = True        # 能否在手机侧直接算 SHA-256（秒传前提）
    range_read: bool = True         # 能否从任意偏移读取（断点续传前提）
    push: bool = True               # 能否反向下载

    def describe(self) -> str:
        bits = []
        bits.append("自动枚举" if self.auto_enumerate else "手动选择")
        bits.append("秒传" if self.remote_hash else "无秒传")
        bits.append("断点续传" if self.range_read else "无续传")
        bits.append("反向下载" if self.push else "无反向下载")
        return " / ".join(bits)


class ContentSource(ABC):
    """内容源统一接口。"""

    #: 通道显示名（终端展示用）
    name: str = "未知通道"

    #: 进度回调： (已完成字节, 总字节, 说明)
    ProgressCb = Callable[[int, int, str], None]

    @abstractmethod
    def probe(self) -> DeviceInfo:
        """探测设备并返回基本信息；失败抛 :class:`common.errors.LinkError`。"""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """声明本通道的能力。"""

    @abstractmethod
    def list_files(
        self,
        roots: list[str],
        *,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> Iterator[RemoteFile]:
        """枚举指定根目录下的所有文件（只返回元数据）。

        :param on_progress: 回调 ``(当前目录, 已发现文件数)``，用于展示扫描进度。
        """

    @abstractmethod
    def remote_sha256(self, remote_path: str) -> str | None:
        """在手机侧计算 SHA-256；通道不支持时返回 None。"""

    @abstractmethod
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
        """把远端文件拉到本地 ``dest_part``（支持从 ``offset`` 续传）。

        :return: 本次实际写入的字节数。
        """

    @abstractmethod
    def push(self, local: str | Path, remote: str) -> None:
        """把本地文件推送到手机（反向下载用）。"""

    @abstractmethod
    def shell(self, command: str, timeout: float = 60.0) -> str:
        """在手机上执行一条 shell 命令，返回标准输出。"""

    @abstractmethod
    def free_space(self, remote_dir: str) -> int:
        """手机端该目录所在分区的剩余字节数；探测失败返回 0。"""

    # 可选实现
    def close(self) -> None:  # pragma: no cover - 默认无资源
        """释放资源。"""

    def __enter__(self) -> "ContentSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class ScanRoot:
    """一个扫描根目录。"""

    remote: str
    rel: str
    included: bool = True
    pattern: str = ""


@dataclass
class ScanPlan:
    """由配置推导出的扫描计划（终端会展示给用户确认）。"""

    roots: list[ScanRoot] = field(default_factory=list)

    def included_roots(self) -> list[ScanRoot]:
        return [r for r in self.roots if r.included]

    def describe(self) -> str:
        return "、".join(r.remote for r in self.included_roots()) or "（无）"
