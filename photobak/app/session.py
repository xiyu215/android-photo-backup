"""一次备份会话的上下文。

把"这一次备份"需要的东西收拢到一个对象里，避免在编排器里到处传参。
同时它也是"本次任务结束后该做什么"（备份索引、导出清单、打开报告目录）
的统一入口。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..channels.base import ContentSource, DeviceInfo
from ..common import paths
from ..common.logger import get_logger
from ..core.archive import ArchiveManager
from ..core.cleanup import CleanupBuilder, CleanupReport
from ..core.config import Config
from ..core.db import IndexDB
from ..core.dedup import Deduper
from ..core.doctor import Doctor
from ..core.naming import device_dir_name
from ..core.scanner import Scanner
from ..core.verify import Verifier

log = get_logger("session")


@dataclass
class RunStats:
    """一次任务的统计。"""

    scanned: int = 0
    accepted: int = 0
    accepted_bytes: int = 0
    transferred: int = 0
    fast_upload: int = 0
    failed: int = 0
    skipped: int = 0
    written_bytes: int = 0
    started_at: str = ""
    finished_at: str = ""

    def as_row(self) -> dict:
        return {
            "扫描文件": self.scanned,
            "纳入备份": self.accepted,
            "实际传输": self.transferred,
            "秒传跳过": self.fast_upload,
            "失败": self.failed,
            "跳过": self.skipped,
            "数据量": paths.human_size(self.written_bytes),
        }


@dataclass
class Session:
    """一次备份会话。"""

    cfg: Config
    db: IndexDB
    source: ContentSource
    device: DeviceInfo
    device_id: int
    device_dir: str

    tracker: object | None = None
    stats: RunStats = field(default_factory=RunStats)
    run_id: int = 0
    report: CleanupReport | None = None

    scanner: Scanner = field(init=False)
    deduper: Deduper = field(init=False)
    archive: ArchiveManager = field(init=False)
    verifier: Verifier = field(init=False)
    cleanup: CleanupBuilder = field(init=False)
    doctor: Doctor = field(init=False)

    def __post_init__(self) -> None:
        self.scanner = Scanner(self.cfg)
        self.deduper = Deduper(self.cfg, self.db)
        self.archive = ArchiveManager(self.cfg, self.db)
        self.verifier = Verifier(self.cfg)
        self.cleanup = CleanupBuilder(self.cfg, self.db)
        self.doctor = Doctor(self.cfg, self.db)

    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.cfg.backup_root_path

    def part_path(self, job_id: int) -> Path:
        return self.cfg.tmp_dir / f"{job_id}.part"

    # ------------------------------------------------------------------
    def backup_index(self, keep: int = 3) -> Path | None:
        """把索引库备份一份并滚动保留。

        每次任务结束都做一次：万一后续操作把索引写坏，还有最近的快照可回滚。
        """
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = self.cfg.backup_dir / f"index_{stamp}.db"
        self.db.backup_to(dest)
        try:
            files = sorted(self.cfg.backup_dir.glob("index_*.db"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[keep:]:
                old.unlink()
        except OSError:
            pass
        return dest if dest.exists() else None

    def cleanup_tmp(self, *, keep_days: int = 7) -> int:
        """清理过期的 .part 临时文件（异常中断的保留 7 天以便续传）。"""
        removed = 0
        cutoff = datetime.now().timestamp() - keep_days * 86400
        try:
            for p in self.cfg.tmp_dir.glob("*.part"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        if removed:
            log.info("已清理 %d 个过期临时文件", removed)
        return removed

    def disk_ok(self, need_bytes: int, margin: float = 1.1) -> tuple[bool, int]:
        """检查备份盘剩余空间是否够用。返回 (是否够, 剩余字节)。"""
        free = paths.free_space(self.root)
        return (free >= need_bytes * margin), free

    def free_space_of_root(self) -> int:
        return paths.free_space(self.root)

    # ------------------------------------------------------------------
    def open_in_explorer(self, path: str | Path | None = None) -> None:
        """在资源管理器中打开目录（Windows 专用，失败静默）。"""
        import os
        import subprocess

        target = Path(path or self.cfg.report_dir)
        try:
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                paths.ensure_dir(target)
                os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.debug("打开目录失败：%s", exc)


def build_session(cfg: Config, db: IndexDB, source: ContentSource,
                  tracker=None) -> Session:
    """探测设备并创建会话。"""
    device = source.probe()
    device_id = db.upsert_device(
        serial=device.serial, model=device.model,
        android=f"{device.android_release}(API {device.android_version})",
        display=device.display_name,
    )
    ddir = device_dir_name(device.model, device.serial)
    return Session(cfg=cfg, db=db, source=source, device=device,
                   device_id=device_id, device_dir=ddir, tracker=tracker)


def copy_tree_marker(src: Path, dst: Path) -> None:
    """把标记文件复制过去（内部工具函数，保留给将来扩展）。"""
    try:
        shutil.copy2(src, dst)
    except OSError:
        pass
