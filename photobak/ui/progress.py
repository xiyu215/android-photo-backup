"""进度跟踪：总进度 + 当前文件 + 速度 + 预计剩余时间。

为什么要单独一个类？
    用户场景是"1 万张照片、100GB"级别的任务，跑几十分钟很正常。这时候
    只显示一个百分比是不够的——用户需要知道"还要多久"和"卡在哪一步"。
    同时 webserver 的 WebSocket 也需要一份结构化的进度快照，
    所以把状态集中在这里，终端和网页共用同一份数据。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..common import paths


@dataclass
class FileProgress:
    """当前正在处理的文件。"""

    name: str = ""
    size: int = 0
    done: int = 0

    @property
    def ratio(self) -> float:
        return (self.done / self.size) if self.size else 0.0


@dataclass
class Snapshot:
    """给 UI / WebSocket 用的进度快照。"""

    stage: str = "准备中"
    total_files: int = 0
    done_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    current_name: str = ""
    current_size: int = 0
    current_done: int = 0
    speed: float = 0.0
    eta_seconds: float = 0.0
    elapsed: float = 0.0
    message: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes > 0:
            return min(100.0, self.done_bytes / self.total_bytes * 100.0)
        if self.total_files > 0:
            return min(100.0, self.done_files / self.total_files * 100.0)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "percent": round(self.percent, 2),
            "total_files": self.total_files,
            "done_files": self.done_files,
            "skipped_files": self.skipped_files,
            "failed_files": self.failed_files,
            "total_bytes": self.total_bytes,
            "total_human": paths.human_size(self.total_bytes),
            "done_bytes": self.done_bytes,
            "done_human": paths.human_size(self.done_bytes),
            "current_name": self.current_name,
            "current_size": self.current_size,
            "current_done": self.current_done,
            "speed": round(self.speed, 1),
            "speed_human": paths.human_speed(self.speed),
            "eta_seconds": round(self.eta_seconds, 1),
            "eta_human": format_duration(self.eta_seconds),
            "elapsed": round(self.elapsed, 1),
            "elapsed_human": format_duration(self.elapsed),
            "message": self.message,
        }


def format_duration(seconds: float) -> str:
    """把秒数格式化为 ``1 小时 23 分`` / ``3 分 05 秒``。"""
    if seconds <= 0 or seconds != seconds:  # 含 NaN
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60:02d} 秒"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


class ProgressTracker:
    """线程安全的进度跟踪器。"""

    def __init__(self, *, window: int = 20) -> None:
        self._lock = threading.RLock()
        self._stage = "准备中"
        self._message = ""
        self._total_files = 0
        self._done_files = 0
        self._skipped = 0
        self._failed = 0
        self._total_bytes = 0
        self._done_bytes = 0
        self._current = FileProgress()
        self._started = time.time()
        self._last_tick = time.time()
        self._last_bytes = 0
        self._speed = 0.0
        # 用滑动窗口平滑速度，避免 WiFi 波动导致数字乱跳
        self._samples: deque[tuple[float, int]] = deque(maxlen=window)

    # ------------------------------------------------------------------
    def set_stage(self, stage: str, message: str = "") -> None:
        with self._lock:
            self._stage = stage
            self._message = message

    def set_totals(self, files: int, total_bytes: int) -> None:
        with self._lock:
            self._total_files = files
            self._total_bytes = total_bytes

    # ------------------------------------------------------------------
    def start_file(self, name: str, size: int) -> None:
        with self._lock:
            self._current = FileProgress(name=name, size=size, done=0)

    def update_file(self, done: int) -> None:
        with self._lock:
            self._current.done = done
            now = time.time()
            self._samples.append((now, self._done_bytes + done))
            self._recalc_speed(now)

    def finish_file(self, *, size: int, ok: bool = True, skipped: bool = False) -> None:
        with self._lock:
            self._done_files += 1
            if skipped:
                self._skipped += 1
            elif not ok:
                self._failed += 1
            self._done_bytes += size
            self._current = FileProgress()

    def add_failure(self) -> None:
        with self._lock:
            self._failed += 1

    # ------------------------------------------------------------------
    def _recalc_speed(self, now: float) -> None:
        """用滑动窗口估算瞬时速度。"""
        if len(self._samples) < 2:
            self._speed = 0.0
            return
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt <= 0.05:
            return
        self._speed = max(0.0, (b1 - b0) / dt)

    # ------------------------------------------------------------------
    def snapshot(self) -> Snapshot:
        with self._lock:
            elapsed = time.time() - self._started
            remaining = max(0, self._total_bytes - self._done_bytes)
            eta = (remaining / self._speed) if self._speed > 0 else 0.0
            return Snapshot(
                stage=self._stage,
                total_files=self._total_files,
                done_files=self._done_files,
                skipped_files=self._skipped,
                failed_files=self._failed,
                total_bytes=self._total_bytes,
                done_bytes=self._done_bytes,
                current_name=self._current.name,
                current_size=self._current.size,
                current_done=self._current.done,
                speed=self._speed,
                eta_seconds=eta,
                elapsed=elapsed,
                message=self._message,
            )

    # ------------------------------------------------------------------
    def render_line(self) -> str:
        """渲染一行供终端原地刷新的进度文本。"""
        from . import terminal

        snap = self.snapshot()
        label = snap.current_name or snap.message or snap.stage
        return terminal.progress_bar(
            snap.done_bytes, snap.total_bytes,
            label=f"{snap.done_files}/{snap.total_files} {label}",
            speed=snap.speed,
        )

    def summary_lines(self) -> list[str]:
        snap = self.snapshot()
        return [
            f"耗时：{format_duration(snap.elapsed)}",
            f"完成：{snap.done_files} 个（其中秒传 {snap.skipped_files} 个）",
            f"失败：{snap.failed_files} 个",
            f"数据量：{paths.human_size(snap.done_bytes)}",
            f"平均速度：{paths.human_speed(snap.done_bytes / snap.elapsed if snap.elapsed else 0)}",
        ]
