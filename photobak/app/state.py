"""运行时状态：供网页、托盘、终端三方共享的一份"当前情况"快照。

为什么不直接查数据库？
    网页每秒都在轮询/推送进度，如果每次都去查索引库，会造成大量无谓的 IO
    与锁竞争。这里用一份内存状态做缓存，只在关键节点（扫描完成、备份结束）
    才去数据库取一次汇总数据。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeState:
    """当前运行状态。"""

    # 设备与通道
    device: str = ""
    serial: str = ""
    channel: str = ""
    android: str = ""

    # 路径
    backup_root: str = ""
    report_dir: str = ""
    restore_dir: str = ""
    log_file: str = ""

    # 能力
    caps: dict = field(default_factory=dict)
    can_restore: bool = False

    # 统计
    scan_total: int = 0
    scan_bytes: int = 0
    last_run_status: str = ""

    # 清单是否已就绪
    cleanup_ready: bool = False

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "device": self.device,
                "serial": self.serial,
                "channel": self.channel,
                "android": self.android,
                "backup_root": self.backup_root,
                "report_dir": self.report_dir,
                "restore_dir": self.restore_dir,
                "log_file": self.log_file,
                "caps": self.caps,
                "can_restore": self.can_restore,
                "scan_total": self.scan_total,
                "scan_bytes": self.scan_bytes,
                "last_run_status": self.last_run_status,
                "cleanup_ready": self.cleanup_ready,
            }
