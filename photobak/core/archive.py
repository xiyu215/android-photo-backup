"""双层归档树：按拍摄时间的归档树 + 按手机原始路径的镜像树。

为什么要两棵树？
    用户有两个互相冲突的诉求：
      ① "我想按时间浏览照片"（人看）→ 需要按年月日组织的归档树；
      ② "我想知道这个文件原来在手机哪个目录"（对账）→ 需要镜像树。
    两棵树如果各存一份实体，空间直接翻倍。所以用**硬链接**让两个路径
    指向同一份数据：既不占额外空间，又都能正常访问。

硬链接的限制与降级（用户已确认 Q3：A 默认 + 提供开关）
    - NTFS / ReFS：支持硬链接 ✅（用户的目标盘就是 NTFS）
    - exFAT / FAT32：**不支持**硬链接 ❌ → 降级为复制，并主动提示占用双倍空间
    - 跨卷：不支持（EXDEV）→ 降级为复制
    - 用户也可用 ``镜像树链接方式=off`` 完全关掉镜像树
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..common import paths
from ..common.errors import StorageError
from ..common.logger import get_logger
from . import naming, times
from .config import DEFAULT_UNCERTAIN_DIR, DEFAULT_UNKNOWN_DIR, Config
from .db import IndexDB
from .scanner import RemoteFile

log = get_logger("archive")

TREE_ARCHIVE = "archive"
TREE_MIRROR = "mirror"

LINK_FILE = "file"        # 归档树里的实体文件
LINK_HARDLINK = "hardlink"
LINK_COPY = "copy"
LINK_OFF = "off"


@dataclass
class PlaceResult:
    """一次"落地"的结果。"""

    archive_rel: str
    mirror_rel: str | None
    link_type: str
    notes: list[str] = field(default_factory=list)


class ArchiveManager:
    """负责把校验通过的文件放进双层归档树，并登记索引。"""

    def __init__(self, cfg: Config, db: IndexDB) -> None:
        self.cfg = cfg
        self.db = db
        self.root = cfg.backup_root_path

    # ------------------------------------------------------------------
    # 路径规划
    # ------------------------------------------------------------------
    def archive_dir_parts(self, info: times.TimeInfo) -> list[str]:
        """归档树的目录层级：``照片归档/2023/2023-07/2023-07-15``。

        时间存疑与未知时间的文件会被放进带下划线前缀的独立分支，
        避免污染正常时间线（原因见 :mod:`core.times`）。
        """
        return [self.cfg.archive_dir, *times.archive_subdir(
            info,
            uncertain_dir=DEFAULT_UNCERTAIN_DIR,
            unknown_dir=DEFAULT_UNKNOWN_DIR,
        )]

    def mirror_dir_parts(self, device_dir: str, rf: RemoteFile) -> list[str]:
        """镜像树的目录层级：``手机镜像/<设备>/<手机原始目录>``。"""
        rel_dir = rf.rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rf.rel else ""
        parts = [self.cfg.mirror_dir, device_dir]
        if rel_dir:
            parts.extend(p for p in rel_dir.split("/") if p)
        return parts

    def plan(
        self,
        *,
        filename: str,
        hash_hex: str,
        info: times.TimeInfo,
        rf: RemoteFile,
        device_dir: str,
    ) -> tuple[naming.NameResult, list[str] | None]:
        """规划归档路径与镜像路径（不落盘）。

        :return: (归档路径构造结果, 镜像目录层级或 None)
        """
        arch = naming.build_rel_path(
            self.archive_dir_parts(info), filename,
            hash_hex=hash_hex, root_abs=self.root,
        )
        if self.cfg.mirror_link_mode == "off":
            return arch, None
        return arch, self.mirror_dir_parts(device_dir, rf)

    # ------------------------------------------------------------------
    # 落地
    # ------------------------------------------------------------------
    def place(
        self,
        *,
        part_path: str | Path,
        file_id: int,
        filename: str,
        hash_hex: str,
        info: times.TimeInfo,
        rf: RemoteFile,
        device_dir: str,
    ) -> PlaceResult:
        """把已校验的临时文件放入归档树，并按需建立镜像树链接。

        调用前提：``part_path`` 的内容**已经通过双端校验**。
        """
        notes: list[str] = []
        arch = naming.build_rel_path(
            self.archive_dir_parts(info), filename,
            hash_hex=hash_hex, root_abs=self.root,
        )
        notes.extend(arch.notes)

        # 重名处理：内容相同则复用，不同则追加序号
        final_rel, reused = naming.unique_rel_path(
            self.root, arch.rel_path,
            same_content=lambda p: self._same_content(p, hash_hex),
        )
        if reused:
            notes.append("目标位置已存在相同内容的文件，直接复用（未产生副本）")
        elif final_rel != arch.rel_path:
            notes.append("存在同名但内容不同的文件，已追加序号后缀")

        final_abs = paths.safe_join(self.root, final_rel)

        # ---- 落地：原子替换 ----
        try:
            paths.ensure_dir(final_abs.parent)
            if final_abs.exists() and not reused:
                # 极小概率的竞态：再确认一次
                pass
            if not reused:
                os.replace(str(part_path), str(final_abs))
            else:
                # 复用时删除临时文件，不覆盖已有实体
                self._silent_unlink(part_path)
        except OSError as exc:
            raise StorageError("E308", f"无法写入归档文件 {final_abs}：{paths.errno_name(exc)}") from exc

        # ---- 镜像树 ----
        mirror_rel: str | None = None
        link_type = LINK_OFF
        if self.cfg.mirror_link_mode != "off":
            mirror_rel, link_type, mirror_notes = self._make_mirror(
                final_abs, filename=arch.stored_name, hash_hex=hash_hex,
                rf=rf, device_dir=device_dir,
            )
            notes.extend(mirror_notes)

        # ---- 登记索引 ----
        self.db.add_storage(file_id=file_id, rel_path=final_rel, tree=TREE_ARCHIVE,
                            link_type=LINK_FILE)
        if mirror_rel:
            self.db.add_storage(file_id=file_id, rel_path=mirror_rel, tree=TREE_MIRROR,
                                link_type=link_type)

        return PlaceResult(final_rel, mirror_rel, link_type, notes)

    # ------------------------------------------------------------------
    def _make_mirror(
        self, source_abs: Path, *, filename: str, hash_hex: str,
        rf: RemoteFile, device_dir: str,
    ) -> tuple[str | None, str, list[str]]:
        """建立镜像树条目。返回 (相对路径, 链接类型, 提示)。"""
        notes: list[str] = []
        parts = self.mirror_dir_parts(device_dir, rf)
        mirror = naming.build_rel_path(parts, rf.name, hash_hex=hash_hex, root_abs=self.root)
        notes.extend(mirror.notes)

        rel, reused = naming.unique_rel_path(
            self.root, mirror.rel_path,
            same_content=lambda p: self._same_content(p, hash_hex),
        )
        dst_abs = paths.safe_join(self.root, rel)
        if reused:
            return rel, LINK_HARDLINK, notes  # 已存在同内容，无需再建

        try:
            paths.ensure_dir(dst_abs.parent)
        except OSError as exc:
            notes.append(f"镜像目录创建失败：{paths.errno_name(exc)}")
            return None, LINK_OFF, notes

        mode = self.cfg.mirror_link_mode
        if mode == "copy":
            return self._do_copy(source_abs, dst_abs, rel, notes)

        # auto / hardlink：先试硬链接
        try:
            os.link(str(source_abs), str(dst_abs))
            return rel, LINK_HARDLINK, notes
        except OSError as exc:
            if mode == "hardlink":
                # 用户明确要求"只允许硬链接"，那就要如实报错
                raise StorageError(
                    "E306",
                    f"镜像树硬链接失败（{paths.errno_name(exc)}）。"
                    f"可把「配置.txt」中的 镜像树链接方式 改为 auto 或 copy。",
                ) from exc
            # auto 模式：降级为复制
            if exc.errno in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES, errno.EMLINK):
                if exc.errno == errno.EXDEV:
                    notes.append("两棵树位于不同磁盘，已降级为独立副本")
                else:
                    notes.append("当前文件系统不支持硬链接，镜像树已降级为独立副本（占用双倍空间）")
                return self._do_copy(source_abs, dst_abs, rel, notes)
            notes.append(f"镜像树创建失败（{paths.errno_name(exc)}），已跳过镜像")
            return None, LINK_OFF, notes

    def _do_copy(self, src: Path, dst: Path, rel: str, notes: list[str]) -> tuple[str, str, list[str]]:
        """复制实体（硬链接不可用时的降级路径）。"""
        try:
            shutil.copy2(str(src), str(dst))
            return rel, LINK_COPY, notes
        except OSError as exc:
            notes.append(f"镜像副本创建失败（{paths.errno_name(exc)}），已跳过镜像")
            return rel, LINK_OFF, notes

    # ------------------------------------------------------------------
    def _same_content(self, path: Path, expected_sha: str) -> bool:
        """判断磁盘上已有文件的内容是否就是我们要的那份。

        先用大小快速排除（绝大多数不同内容的文件大小也不同），
        只有大小一致才去算哈希——避免在"重名"路径上做无谓的全量哈希。
        """
        expected = self._expected_size(expected_sha)
        try:
            if expected > 0 and path.stat().st_size != expected:
                return False
        except OSError:
            return False
        try:
            from .hashutil import hash_file

            return hash_file(path).sha256 == expected_sha
        except OSError:
            return False

    def _expected_size(self, sha: str) -> int:
        """从索引取该内容的大小；取不到返回 -1（表示"未知，不能据此排除"）。"""
        row = self.db.get_file_by_sha(sha)
        return row.size if row else -1

    @staticmethod
    def _silent_unlink(p: str | Path) -> None:
        try:
            Path(p).unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 体检辅助
    # ------------------------------------------------------------------
    def abs_of(self, rel: str) -> Path:
        return paths.safe_join(self.root, rel)

    def verify_hardlinks(self, sample: int = 200) -> list[str]:
        """抽查硬链接是否仍然成立（返回问题描述列表）。

        硬链接的特点：删除任一目录项都不影响另一个。但如果用户用"移动"或
        "另存为"的方式操作过镜像树，链接可能已经断成两份独立实体——这不算
        错误，只是浪费空间，因此这里只做提示。
        """
        problems: list[str] = []
        count = 0
        for rel in self.db.all_storage_paths():
            if count >= sample:
                break
            p = self.abs_of(rel)
            if not p.exists():
                problems.append(f"索引记录的文件不存在：{rel}")
            count += 1
        return problems
