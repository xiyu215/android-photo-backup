"""秒传与去重判定。

铁律（用户明确要求）
--------------------
**去重只认 SHA-256 完全一致。**

这条规则看着简单，但它排除了所有"看起来像"的启发式做法：
    - 不按文件名去重 → ``IMG_x.jpg`` 与 ``IMG_x_edit.jpg`` 是**两个不同文件**，
      编辑版必须原样备份（用户明确点名要求）；
    - 不按"大小 + 名字"去重 → 同名同大小但内容不同的文件（例如两次导出的
      同尺寸截图）必须都保留；
    - 不按 mtime 去重 → 转存导致的 mtime 变化不影响判重。

秒传的**安全前提**
------------------
命中"索引里有这个哈希"还不够，必须再确认"磁盘上那份文件真的存在且大小一致"。
否则一旦用户手工删过备份目录里的文件，就会出现"秒传跳过 → 结果手机上也没了
→ 数据丢失"。这是本项目最不能接受的事故，所以这里做一次存在性复核。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from ..common import paths
from ..common.logger import get_logger
from .config import Config
from .db import IndexDB

log = get_logger("dedup")


class Action(enum.Enum):
    """对一个候选文件的处置决定。"""

    NEW = "new"                 # 需要传输
    FAST = "fast"               # 秒传：服务端已有且已验证
    IN_BATCH = "in_batch"       # 本次扫描内重复（同一内容出现多次）


@dataclass
class Decision:
    action: Action
    file_id: int | None = None
    reason: str = ""
    #: 秒传时，已存在的归档相对路径（便于直接登记实例）
    existing_rel: str | None = None


class Deduper:
    """基于索引库的秒传判定器。"""

    def __init__(self, cfg: Config, db: IndexDB) -> None:
        self.cfg = cfg
        self.db = db
        self.root = cfg.backup_root_path
        #: 本次任务内已经处理过的哈希 → file_id（处理"同一张图在多个 App 目录"）
        self._seen_in_run: dict[str, int] = {}

    # ------------------------------------------------------------------
    def decide(self, sha256: str, size: int) -> Decision:
        """判定某个内容是否需要传输。"""
        # ① 本次任务内已处理过（同一次扫描中的重复内容）
        cached = self._seen_in_run.get(sha256)
        if cached is not None:
            return Decision(Action.IN_BATCH, cached, "本次扫描中已处理过相同内容")

        # ② 查索引库
        row = self.db.get_file_by_sha(sha256)
        if row is None:
            return Decision(Action.NEW, None, "索引库中没有该内容")

        if row.size != size:
            # 哈希相同但大小不同，理论上不可能；若出现说明索引被破坏
            log.warning("哈希 %s 的大小与索引记录不一致（索引 %d / 实际 %d），按新文件处理",
                        sha256[:16], row.size, size)
            return Decision(Action.NEW, row.id, "索引记录异常，重新传输")

        if not row.verified:
            return Decision(Action.NEW, row.id, "索引中存在但尚未通过校验")

        # ③ 关键：确认磁盘上那份文件真的在
        existing = self._find_existing(row.id)
        if existing is None:
            log.warning("索引记录 %s 声称已验证，但磁盘上找不到对应文件，已按新文件处理",
                        sha256[:16])
            self.db.mark_unverified(row.id, "磁盘文件缺失")
            return Decision(Action.NEW, row.id, "索引记录的文件已丢失，重新传输")

        self._seen_in_run[sha256] = row.id
        return Decision(Action.FAST, row.id, "服务端已有相同内容且已通过校验", existing)

    # ------------------------------------------------------------------
    def register(self, sha256: str, file_id: int) -> None:
        """登记"本次任务已处理过该内容"。"""
        self._seen_in_run[sha256] = file_id

    def reset(self) -> None:
        self._seen_in_run.clear()

    # ------------------------------------------------------------------
    def _find_existing(self, file_id: int) -> str | None:
        """在索引里找一条确实存在于磁盘上的存储路径。"""
        rows = self.db.storages_of_file(file_id)
        # 优先归档树（它是实体文件；镜像树可能是硬链接或副本）
        ordered = sorted(rows, key=lambda r: 0 if r["tree"] == "archive" else 1)
        for r in ordered:
            rel = r["rel_path"]
            try:
                p = paths.safe_join(self.root, rel)
            except ValueError:
                continue
            if p.is_file():
                return rel
        return None

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        """秒传统计（供任务报告使用）。"""
        total, verified = self.db.count_files()
        return {"files_total": total, "files_verified": verified}
