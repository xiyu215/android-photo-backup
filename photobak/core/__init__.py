"""领域层：与"通道"无关的全部业务规则。

分层约定
--------
``core`` 里的模块**不直接调用 ADB 或网络**。它们只处理：
    - 数据（哈希、时间、路径、索引）
    - 规则（扫描范围、去重、分档、校验策略）
这样 USB 与 WiFi 两条通道才能共用同一套逻辑，保证"规则完全一致"。
"""

from __future__ import annotations

__all__ = [
    "config", "db", "hashutil", "times", "naming", "scanner",
    "dedup", "archive", "verify", "cleanup", "doctor", "restore", "exif_reader",
]
