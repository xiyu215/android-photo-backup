"""反向下载：把电脑上的照片放回手机。

安全边界
--------
这是本工具**唯一**会写入手机的路径，因此做了三重约束：
    1. 只写入 ``配置.txt`` 里指定的目录（默认 ``/sdcard/DCIM/Restored/``），
       绝不触碰手机上的任何既有文件；
    2. 同名但内容不同的文件**追加后缀**，绝不覆盖；
    3. ``配置.txt`` 里把 ``启用反向下载=0`` 即可彻底关闭此能力。

媒体库刷新
----------
安卓的相册读取的是 MediaStore 数据库，直接把文件拷进去并不会立刻出现在相册里，
必须主动触发媒体扫描。不同 Android 版本可用的命令不同，这里按版本分层尝试，
全部失败时提示用户手动打开一次文件管理器（通常也会触发扫描）。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..common import paths
from ..common.logger import get_logger
from .config import Config
from .db import IndexDB

log = get_logger("restore")


class PushCapable(Protocol):
    """反向下载所需的最小通道能力（由 :mod:`channels.usb_adb` 实现）。"""

    def push(self, local: str | Path, remote: str) -> None: ...
    def shell(self, command: str, timeout: float = 60.0) -> str: ...
    def free_space(self, remote_dir: str) -> int: ...


@dataclass
class RestoreResult:
    file_id: int
    local_path: str
    remote_path: str
    ok: bool
    skipped: bool = False
    detail: str = ""


class RestoreManager:
    """负责"电脑 → 手机"方向的复制与媒体库刷新。"""

    def __init__(self, cfg: Config, db: IndexDB) -> None:
        self.cfg = cfg
        self.db = db
        self.root = cfg.backup_root_path

    # ------------------------------------------------------------------
    def plan(self, file_ids: list[int]) -> list[tuple[int, Path]]:
        """把 file_id 列表解析成 (file_id, 本地绝对路径)。"""
        out: list[tuple[int, Path]] = []
        for fid in file_ids:
            rows = self.db.storages_of_file(fid)
            for r in rows:
                if r["tree"] != "archive":
                    continue
                try:
                    p = paths.safe_join(self.root, r["rel_path"])
                except ValueError:
                    continue
                if p.is_file():
                    out.append((fid, p))
                    break
        return out

    def push_files(
        self,
        source: PushCapable,
        file_ids: list[int],
        *,
        dest_dir: str | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> list[RestoreResult]:
        """把选中的文件推回手机。

        :param progress: 回调 ``(已完成数, 总数, 当前文件名)``。
        """
        if not self.cfg.enable_restore:
            log.warning("反向下载已在配置中禁用")
            return []

        dest = (dest_dir or self.cfg.restore_dir).rstrip("/")
        items = self.plan(file_ids)
        results: list[RestoreResult] = []
        total = len(items)

        # 空间预检：手机剩余空间不够就提前失败，避免传一半
        need = sum(p.stat().st_size for _, p in items)
        try:
            free = source.free_space("/sdcard")
            if free and need > free * 0.98:
                log.error("手机剩余空间不足（需 %s，剩余 %s）",
                          paths.human_size(need), paths.human_size(free))
                return [RestoreResult(fid, str(p), "", False,
                                      detail="手机剩余空间不足，请先清理手机") for fid, p in items]
        except Exception as exc:  # noqa: BLE001 —— 空间探测失败不阻断
            log.debug("手机空间探测失败（忽略）：%s", exc)

        # 确保目标目录存在
        try:
            source.shell(f'mkdir -p "{dest}"')
        except Exception as exc:  # noqa: BLE001
            log.warning("创建恢复目录失败：%s", exc)

        for i, (fid, local) in enumerate(items, 1):
            if cancel and cancel():
                break
            if progress:
                progress(i - 1, total, local.name)

            remote = f"{dest}/{local.name}"
            # 冲突检查：手机端已存在同名文件时追加后缀，绝不覆盖
            try:
                exists = source.shell(f'[ -e "{remote}" ] && echo yes || echo no').strip()
            except Exception:  # noqa: BLE001
                exists = "no"
            if exists == "yes":
                remote = self._unique_remote(source, dest, local.name)

            try:
                source.push(local, remote)
                results.append(RestoreResult(fid, str(local), remote, True))
            except Exception as exc:  # noqa: BLE001
                log.warning("推送失败 %s：%s", local.name, exc)
                results.append(RestoreResult(fid, str(local), remote, False, detail=str(exc)))

        if progress:
            progress(total, total, "")
        return results

    def _unique_remote(self, source: PushCapable, dest: str, name: str, max_try: int = 50) -> str:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        else:
            ext = "." + ext
        for i in range(1, max_try + 1):
            cand = f"{dest}/{stem}_restored_{i}{ext}"
            try:
                if source.shell(f'[ -e "{cand}" ] && echo yes || echo no').strip() != "yes":
                    return cand
            except Exception:  # noqa: BLE001
                return cand
        return f"{dest}/{stem}_restored_{max_try}{ext}"

    # ------------------------------------------------------------------
    def refresh_media(self, source: PushCapable, remote_dir: str | None = None,
                      android_version: int = 0) -> tuple[bool, str]:
        """触发安卓媒体库刷新，让恢复的照片立刻出现在相册里。

        按 Android 版本分层尝试（越新的版本越严格）：
            Android 11+：``cmd media_scanner scan <dir>``
            Android 10 及以下：``am broadcast MEDIA_SCANNER_SCAN_FILE``
            兜底：``am broadcast MEDIA_MOUNTED``
        """
        target = (remote_dir or self.cfg.restore_dir).rstrip("/")
        attempts: list[tuple[str, str]] = []

        if android_version >= 11 or android_version == 0:
            attempts.append(("cmd media_scanner scan", f'cmd media_scanner scan "{target}"'))
        attempts += [
            ("am broadcast MEDIA_SCANNER_SCAN_FILE",
             f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE'
             f' -d file://"{target}"'),
            ("am broadcast MEDIA_MOUNTED",
             'am broadcast -a android.intent.action.MEDIA_MOUNTED -d file:///sdcard'),
        ]

        for name, cmd in attempts:
            try:
                out = source.shell(cmd, timeout=20)
            except Exception as exc:  # noqa: BLE001
                log.debug("媒体刷新命令 %s 失败：%s", name, exc)
                continue
            low = (out or "").lower()
            if "error" in low or "exception" in low or "security" in low:
                log.debug("媒体刷新命令 %s 返回异常：%s", name, out.strip()[:200])
                continue
            log.info("已触发媒体库刷新（%s）", name)
            return True, f"已通过「{name}」触发媒体库刷新"

        hint = ("自动刷新未成功。请在手机上打开一次「文件管理」进入该目录，"
                "或重启手机，相册即可看到恢复的照片。")
        log.warning(hint)
        return False, hint

    # ------------------------------------------------------------------
    def make_zip(
        self,
        file_ids: list[int],
        out_path: str | Path | None = None,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path | None:
        """把选中的文件打包成一个 zip（供手机浏览器下载）。

        用 ``ZIP_STORED``（不压缩）：照片视频本身已经是压缩格式，
        再压缩几乎不省空间却要耗费大量 CPU，得不偿失。
        """
        items = self.plan(file_ids)
        if not items:
            return None
        out = Path(out_path) if out_path else (
            self.cfg.restore_staging /
            f"恢复包_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        paths.ensure_dir(out.parent)
        used: set[str] = set()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for i, (_fid, p) in enumerate(items, 1):
                name = p.name
                if name in used:  # zip 内重名处理
                    stem, dot, ext = name.rpartition(".")
                    name = f"{stem}_{i}{('.' + ext) if dot else ''}"
                used.add(name)
                zf.write(p, arcname=name)
                if progress:
                    progress(i, len(items))
        log.info("已生成恢复包：%s（%d 个文件，%s）",
                 out, len(items), paths.human_size(out.stat().st_size))
        return out
