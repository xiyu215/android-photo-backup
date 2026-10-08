"""索引-文件一致性体检（doctor）。

为什么必须有这个模块？
    用户的核心诉求是"索引库和照片文件必须始终保持一致，不能出现索引说有但
    文件不存在"。虽然我们的写顺序（先落盘 fsync、再写索引）已经在设计上
    杜绝了这种情况，但现实里仍可能出现：
        - 用户手工删了备份目录里的文件；
        - 用户把文件"移动"到别处（索引里那条就悬空了）；
        - 磁盘坏道导致文件损坏；
        - 索引库被第三方工具改过。
    体检就是用来发现并修复这些"事后不一致"的。

修复原则
--------
    - **索引有、文件无** → 把该内容标记为未验证（不删记录，保留审计线索），
      这样它对应的手机文件会自动落到「暂不能删」档，避免误删。
    - **文件有、索引无** → 补录（重算哈希、重新判定时间），不删用户的文件。
    - 任何情况下**都不删除用户的照片文件**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..common import paths
from ..common.logger import get_logger
from . import hashutil, naming, times
from .archive import TREE_ARCHIVE, ArchiveManager
from .config import Config
from .db import IndexDB

log = get_logger("doctor")


@dataclass
class DoctorReport:
    """体检结果。"""

    checked: int = 0
    missing_files: list[str] = field(default_factory=list)      # 索引有、文件无
    unregistered: list[str] = field(default_factory=list)       # 文件有、索引无
    size_mismatch: list[str] = field(default_factory=list)      # 大小不符
    broken_index: list[int] = field(default_factory=list)       # 索引内部矛盾
    repaired: int = 0
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return not (self.missing_files or self.size_mismatch or self.broken_index)

    def summary(self) -> str:
        lines = [
            f"检查条目：{self.checked}",
            f"索引有但文件缺失：{len(self.missing_files)}",
            f"文件存在但未登记：{len(self.unregistered)}",
            f"大小不一致：{len(self.size_mismatch)}",
            f"索引内部矛盾：{len(self.broken_index)}",
        ]
        if self.repaired:
            lines.append(f"已修复：{self.repaired}")
        return "\n".join(lines)


class Doctor:
    """一致性体检与修复。"""

    def __init__(self, cfg: Config, db: IndexDB) -> None:
        self.cfg = cfg
        self.db = db
        self.root = cfg.backup_root_path
        self.archive = ArchiveManager(cfg, db)

    # ------------------------------------------------------------------
    def check(self, *, deep: bool = False) -> DoctorReport:
        """执行体检。

        :param deep: 是否对大小一致的文件再算一次哈希（很慢，默认关闭）。
        """
        report = DoctorReport()

        # ① 索引 → 文件
        for row in self.db.raw_query(
            "SELECT s.rel_path, s.tree, f.id AS file_id, f.size, f.sha256, f.verified"
            "  FROM storage s JOIN files f ON f.id = s.file_id"
        ):
            report.checked += 1
            rel = row["rel_path"]
            try:
                p = paths.safe_join(self.root, rel)
            except ValueError:
                report.broken_index.append(int(row["file_id"]))
                continue
            if not p.is_file():
                report.missing_files.append(rel)
                continue
            try:
                actual = p.stat().st_size
            except OSError:
                report.missing_files.append(rel)
                continue
            if actual != int(row["size"]):
                report.size_mismatch.append(rel)
                continue
            if deep:
                if hashutil.hash_file(p).sha256 != row["sha256"]:
                    report.size_mismatch.append(f"{rel}（内容哈希不符）")

        # ② 文件 → 索引（只看归档树，镜像树是它的链接/副本，天然会重复）
        known = self.db.all_storage_paths()
        archive_root = self.root / self.cfg.archive_dir
        if archive_root.is_dir():
            media = self.cfg.all_media_ext()
            for p in archive_root.rglob("*"):
                if not p.is_file():
                    continue
                ext = p.suffix.lower().lstrip(".")
                if ext not in media:
                    continue
                rel = paths.relpath_of(p, self.root)
                if rel not in known:
                    report.unregistered.append(rel)

        report.detail = report.summary()
        return report

    # ------------------------------------------------------------------
    def repair(self, report: DoctorReport, *, register_new: bool = True) -> DoctorReport:
        """按体检结果修复。"""
        fixed = 0

        # 缺失文件 → 标记未验证（手机侧对应文件会自动降档到「暂不能删」）
        for rel in report.missing_files:
            rows = self.db.raw_query("SELECT file_id FROM storage WHERE rel_path=?", (rel,))
            for r in rows:
                self.db.mark_unverified(int(r["file_id"]), "体检：磁盘文件缺失")
                fixed += 1

        # 大小/哈希不符 → 同样标记未验证
        for rel in report.size_mismatch:
            base = rel.split("（", 1)[0]
            rows = self.db.raw_query("SELECT file_id FROM storage WHERE rel_path=?", (base,))
            for r in rows:
                self.db.mark_unverified(int(r["file_id"]), "体检：文件内容不符")
                fixed += 1

        # 未登记文件 → 补录
        if register_new:
            for rel in report.unregistered:
                try:
                    fixed += self._register(rel)
                except Exception as exc:  # noqa: BLE001 —— 单个文件失败不影响整体
                    log.warning("补录失败 %s：%s", rel, exc)

        report.repaired = fixed
        return report

    def _register(self, rel: str) -> int:
        """把一个已存在于备份目录的文件补录进索引。"""
        p = paths.safe_join(self.root, rel)
        result = hashutil.hash_file(p)
        ext = p.suffix.lower().lstrip(".")
        kind = self.cfg.kind_of(ext)
        info = times.resolve_final(p, kind=kind, name=p.name)
        file_id = self.db.upsert_file(
            sha256=result.sha256, size=result.size, kind=kind, ext=ext,
            taken_at=info.to_iso(), taken_at_source=info.source,
            time_confidence=info.confidence, is_time_uncertain=info.is_uncertain,
            verified=True, second_read_ok=None,
        )
        self.db.add_storage(file_id=file_id, rel_path=rel, tree=TREE_ARCHIVE, link_type="file")
        log.info("已补录未登记文件：%s", rel)
        return 1

    # ------------------------------------------------------------------
    def drop_missing_storage(self) -> int:
        """把"指向不存在文件"的存储记录清掉。

        仅在用户明确要求"清理悬空记录"时调用；默认不用，因为保留悬空记录
        反而有利于追溯（它会持续让对应手机文件停留在「暂不能删」档）。
        """
        removed = 0
        for rel in list(self.db.all_storage_paths()):
            try:
                p = paths.safe_join(self.root, rel)
            except ValueError:
                self.db.drop_storage(rel)
                removed += 1
                continue
            if not p.exists():
                self.db.drop_storage(rel)
                removed += 1
        return removed
