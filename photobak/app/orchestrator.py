"""备份编排器：本项目的主引擎。

一次完整备份的状态机
--------------------
    探测设备 → 建立扫描计划 → 枚举文件 → 规则过滤 → 空间预检
      → 逐文件：源端哈希 → 秒传判定 → 传输 → 落盘校验 →（可选）复读校验
                → 原子落地 → 建镜像链接 → 写索引 → 标记已验证
      → 汇总 → 备份索引 → 生成清理清单

三条不可违反的顺序/边界约束
--------------------------
1. **先落盘并 fsync，再写索引**（否则断电会出现"索引说有、文件不存在"）。
2. **只有双端校验通过的文件才会被标记 verified=1**，才可能进入「可放心删」。
3. **绝不删除手机上的任何文件**——本模块对手机侧只有读操作
   （唯一例外是用户主动调用的反向下载，走 :mod:`core.restore`，不在这里）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..channels.base import ContentSource, ScanRoot
from ..common import i18n, paths
from ..common.errors import (
    EnvError, FileOpError, LinkError, PhotobakError, StorageError, TransferError, VerifyError,
)
from ..common.logger import get_logger
from ..core import scanner, times
from ..core.hashutil import hash_file, normalize_sha256
from ..core.verify import remove_part
from .session import Session

log = get_logger("orchestrator")

#: 事件回调签名： (事件名, 数据)
EventCb = Callable[[str, dict], None]


@dataclass
class Plan:
    """一次备份的执行计划（会展示给用户确认）。"""

    roots: list[ScanRoot]
    rows: list[tuple[str, str, str]]     # (目录, 是否包含, 说明)


class Orchestrator:
    """驱动整个备份流程。"""

    def __init__(self, session: Session, *, on_event: EventCb | None = None) -> None:
        self.s = session
        self.cfg = session.cfg
        self.db = session.db
        self.source = session.source
        self.tracker = session.tracker
        self.on_event = on_event

    # ------------------------------------------------------------------
    # 事件与进度
    # ------------------------------------------------------------------
    def _emit(self, kind: str, **payload) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001 —— UI 回调失败不能影响备份
                pass

    def _stage(self, text: str, message: str = "") -> None:
        if self.tracker:
            self.tracker.set_stage(text, message)
        log.info(text)
        self._emit("stage", stage=text, message=message)

    # ------------------------------------------------------------------
    # 扫描计划
    # ------------------------------------------------------------------
    def build_plan(self) -> Plan:
        """由配置推导出扫描计划。"""
        device = self.s.device
        base = device.storage_root.rstrip("/")
        roots: list[ScanRoot] = []
        rows: list[tuple[str, str, str]] = []

        for d in self.cfg.include_dirs:
            name = d.strip().strip("/")
            if not name:
                continue
            remote = f"{base}/{name}"
            roots.append(ScanRoot(remote=remote, rel=name, included=True))
            rows.append((remote, "包含", "默认相册/媒体目录"))

        if self.cfg.include_android_data:
            remote = f"{base}/Android/data"
            roots.append(ScanRoot(remote=remote, rel="Android/data", included=True,
                                  pattern="用户已在配置中开启"))
            rows.append((remote, "包含", "App 私有目录（可能含大量缓存）"))
        else:
            rows.append((f"{base}/Android/data", "排除", "App 私有目录，默认排除"))

        for pat, why in ((".thumbnails", "缩略图缓存"),
                         ("cache / temp", "各类缓存目录"),
                         ("Android/obb", "游戏资源包"),
                         (".nomedia 目录", "App 私有内容")):
            rows.append((pat, "排除", why))

        return Plan(roots=roots, rows=rows)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, *, dry_run: bool = False,
            cancel: Callable[[], bool] | None = None,
            auto_confirm: bool = False) -> Session:
        """执行一次完整备份。

        :param dry_run: 试运行——只扫描并出报告，不传输、不落盘。
        :param cancel: 返回 True 时尽快安全中止（已完成的部分全部保留）。
        """
        s = self.s
        s.stats.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        s.run_id = self.db.start_run(self.source.name, str(self.cfg.backup_root_path))

        try:
            plan = self.build_plan()
            self._emit("plan", rows=plan.rows)

            # ---- 1. 枚举 ----
            self._stage("正在扫描手机相册…")
            raw = self._enumerate(plan.roots, cancel)
            s.stats.scanned = len(raw)
            self._stage(f"扫描完成，发现 {len(raw)} 个候选文件")

            # ---- 2. 规则过滤 ----
            rejected: list[tuple[str, str]] = []
            unbacked: list[dict] = []

            def _on_reject(rf, code: str, reason: str) -> None:
                """被扫描规则排除的文件。

                两类处理，语义完全不同：
                  * **超限被拒（E410）**——用户明确要求"直接做拒绝上传加跳过处理"。
                    它属于"该备份但没备份"，要计入失败数并写失败清单，让用户在
                    报告里看到为什么少了个文件。
                  * **缓存目录 / .nomedia（E420 / E430）**——这是按规则主动不备份，
                    属于正常结果，**不能算失败**。但同样要留痕，否则用户在清理
                    清单里找不到这些文件会以为工具漏了。它们进「未备份文件」表。
                """
                if code == scanner.REJECT_TOO_BIG:
                    rejected.append((rf.path, reason))
                    s.stats.failed += 1
                    self.db.add_failure(run_id=s.run_id, source_path=rf.path,
                                        reason_code=code, reason_text=reason)
                unbacked.append({
                    "source_path": rf.path,
                    "size": rf.size,
                    "reason_code": code,
                    "reason_text": reason,
                    "tier_hint": scanner.reject_tier_hint(code),
                })

            accepted = s.scanner.filter(raw, on_reject=_on_reject)
            s.stats.accepted = len(accepted)
            s.stats.accepted_bytes = sum(rf.size for rf in accepted)
            log.info("规则过滤后剩 %d 个文件（%s），跳过 %d 个",
                     len(accepted), paths.human_size(s.stats.accepted_bytes), len(raw) - len(accepted))
            for p, why in rejected:
                log.warning("已跳过（%s）：%s", why, p)

            # 把"未备份文件"落库（整体重建；试运行不写盘）
            if not dry_run:
                try:
                    n = self.db.replace_unbacked(s.device_id, unbacked)
                    if n:
                        log.info("已记录 %d 个未备份文件（缓存/超限/.nomedia），"
                                 "它们会出现在清理清单里", n)
                except Exception as exc:  # noqa: BLE001 —— 记录失败不能影响备份主流程
                    log.warning("写入未备份清单失败：%s", exc)

            if self.tracker:
                self.tracker.set_totals(len(accepted), s.stats.accepted_bytes)

            if not accepted:
                self._stage("没有需要备份的文件")
                return self._finish("done", "本次没有需要备份的文件")

            # ---- 3. 空间预检 ----
            self._precheck_space(s.stats.accepted_bytes)

            if dry_run:
                self._stage("试运行模式：不传输、不写盘")
                return self._finish("dry-run", "试运行结束")

            # ---- 4. 逐文件处理 ----
            self._stage(f"开始备份（共 {len(accepted)} 个文件）")
            for idx, rf in enumerate(accepted, 1):
                if cancel and cancel():
                    self._stage("用户已取消，正在收尾…")
                    return self._finish("cancelled", "用户取消")
                self._process_one(rf, idx, len(accepted), cancel)

            return self._finish("done", "")

        except PhotobakError as exc:
            log.error("任务中止：%s", exc.user_message())
            self._emit("error", code=exc.code, message=exc.user_message())
            return self._finish("aborted", exc.title)
        except KeyboardInterrupt:
            return self._finish("cancelled", "用户中断")
        except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让程序崩掉
            log.exception("未预期错误")
            self._emit("error", code="E701", message=str(exc))
            return self._finish("crashed", str(exc))

    # ------------------------------------------------------------------
    def _enumerate(self, roots: list[ScanRoot], cancel) -> list:
        """通过通道枚举文件（带进度回调）。"""
        collected = []
        last = [0]

        def _progress(cur_dir: str, count: int) -> None:
            last[0] = count
            if self.tracker:
                self.tracker.set_stage("正在扫描手机相册…", f"已发现 {count} 个文件")
            self._emit("scan", dir=cur_dir, count=count)

        for rf in self.source.list_files([r.remote for r in roots], on_progress=_progress):
            collected.append(rf)
            if cancel and cancel():
                break
        return collected

    def _precheck_space(self, need: int) -> None:
        """磁盘空间预检：不够就直接拒绝启动，避免跑到一半爆盘。"""
        free = paths.free_space(self.cfg.backup_root_path)
        if free <= 0:
            raise StorageError("E301", f"备份目录不可写或所在磁盘不可用：{self.cfg.backup_root_path}")
        if need * 1.1 > free:
            raise StorageError(
                "E301",
                f"需要约 {paths.human_size(need)}，但 {self.cfg.backup_root_path} 所在磁盘"
                f"只剩 {paths.human_size(free)}",
            )
        log.info("空间预检通过：需要约 %s，可用 %s", paths.human_size(need), paths.human_size(free))

    # ------------------------------------------------------------------
    def _process_one(self, rf, idx: int, total: int, cancel) -> None:
        """处理单个文件：哈希 → 秒传 → 传输 → 校验 → 落地 → 索引。"""
        s = self.s
        name = rf.name
        try:
            if self.tracker:
                self.tracker.start_file(name, rf.size)

            # ---- 源端哈希 ----
            expected = self._remote_hash(rf)
            if expected:
                decision = s.deduper.decide(expected, rf.size)
                if decision.action.value in ("fast", "in_batch"):
                    self._record_fast(rf, decision)
                    return
            else:
                decision = None

            # ---- 传输 + 校验 ----
            outcome = self._transfer(rf, expected, cancel)
            if not outcome.ok:
                self._record_failure(rf, outcome.error_code or "E503", outcome.detail)
                return

            # ---- 落地 + 索引 ----
            self._land(rf, outcome.local_sha or "", outcome, expected)
            s.stats.transferred += 1
            s.stats.written_bytes += rf.size
            if self.tracker:
                self.tracker.finish_file(size=rf.size, ok=True)
            self._emit("file_ok", path=rf.path, index=idx, total=total)

        except (LinkError, TransferError) as exc:
            # 连接/传输类：记录后继续下一个（不因单个文件失败而整体中止）
            self._record_failure(rf, exc.code, exc.detail or exc.title)
        except (StorageError, EnvError) as exc:
            # 存储/环境类：这是"整体性问题"，继续跑没有意义
            raise
        except PhotobakError as exc:
            self._record_failure(rf, exc.code, exc.detail or exc.title)
        except Exception as exc:  # noqa: BLE001 —— 单个文件异常不影响整体
            log.exception("处理 %s 时出现未预期错误", rf.path)
            self._record_failure(rf, "E701", str(exc))

    # ------------------------------------------------------------------
    def _remote_hash(self, rf) -> str | None:
        """取源端哈希（拿不到就返回 None，后续退化为"仅单向校验"）。"""
        caps = self.source.capabilities()
        if not caps.remote_hash:
            return None
        value = self.source.remote_sha256(rf.path)
        return normalize_sha256(value)

    def _transfer(self, rf, expected: str | None, cancel):
        """建立传输任务并执行"传输 + 校验 + 重试"。"""
        s = self.s
        job_id = self.db.upsert_job(
            run_id=s.run_id, source_path=rf.path, device_id=s.device_id,
            expected_size=rf.size, expected_sha256=expected,
            part_path=str(Path(s.part_path(0)).name),
        )
        part = s.part_path(job_id)
        self.db.update_job(job_id, part_path=str(part.name), status="running")

        def _do_transfer(resume: bool) -> None:
            offset = 0
            if resume and part.exists():
                offset = part.stat().st_size
                if offset > rf.size:      # 残留文件比源文件还大 → 丢弃重来
                    remove_part(part)
                    offset = 0
                elif offset == rf.size:
                    return                # 已经传完，直接进入校验
                log.info("从断点续传：%s（已传 %s）", rf.name, paths.human_size(offset))
            written = self.source.fetch(
                rf.path, part,
                offset=offset,
                expected_size=rf.size,
                progress=self._file_progress,
                cancel=cancel,
            )
            self.db.update_job(job_id, bytes_done=offset + written)

        def _on_retry(attempt: int, reason: str) -> None:
            log.warning("第 %d 次重试：%s（%s）", attempt, rf.name, reason)
            self._emit("retry", path=rf.path, attempt=attempt, reason=reason)

        outcome = s.verifier.run(
            do_transfer=_do_transfer,
            part_path=part,
            expected_sha=expected,
            expected_size=rf.size,
            cancel=cancel,
            progress=self._file_progress,
            on_retry=_on_retry,
        )
        if outcome.ok:
            self.db.update_job(job_id, status="done", bytes_done=rf.size, last_error=None)
        else:
            self.db.update_job(job_id, status="failed", last_error=outcome.detail)
        return outcome

    def _file_progress(self, done: int, total: int) -> None:
        if self.tracker:
            self.tracker.update_file(done)

    # ------------------------------------------------------------------
    def _land(self, rf, sha: str, outcome, expected: str | None) -> None:
        """把校验通过的文件放进双层归档树并登记索引。

        顺序严格遵循：**文件先落地并 fsync → 再写索引**。
        """
        s = self.s
        part = self._find_part(rf)
        if part is None:
            raise StorageError("E308", f"找不到已下载的临时文件：{rf.path}")

        kind = self.cfg.kind_of(rf.ext)
        # 时间判定在**落盘后**做，这样能读到 EXIF / 视频容器头
        info = times.resolve_final(part, kind=kind, name=rf.name, mtime=rf.mtime)

        # ① 先建索引记录（verified=0），拿到 file_id
        file_id = self.db.upsert_file(
            sha256=sha, size=rf.size, kind=kind, ext=rf.ext,
            taken_at=info.to_iso(), taken_at_source=info.source,
            time_confidence=info.confidence, is_time_uncertain=info.is_uncertain,
            verified=False, second_read_ok=None,
        )

        # ② 落地（移动 + 建镜像链接），此时文件已在磁盘上
        result = s.archive.place(
            part_path=part, file_id=file_id, filename=rf.name,
            hash_hex=sha, info=info, rf=rf, device_dir=s.device_dir,
        )
        for note in result.notes:
            log.info("[%s] %s", rf.name, note)

        # ③ 最后才标记"已验证"
        s.db.mark_verified(file_id, second_read_ok=outcome.second_read_ok)
        s.db.upsert_instance(
            file_id=file_id, device_id=s.device_id, source_path=rf.path,
            source_mtime=str(rf.mtime) if rf.mtime else None,
            app_tag=rf.app_tag, root_tag=rf.root_tag,
        )
        s.deduper.register(sha, file_id)

        if not outcome.dual_sided:
            log.warning("[%s] 该文件仅做了单向校验（通道无法提供源端哈希）", rf.name)

    def _find_part(self, rf) -> Path | None:
        """找到该文件对应的 .part 临时文件。"""
        rows = self.db.raw_query(
            "SELECT part_path FROM transfer_jobs WHERE device_id=? AND source_path=?"
            " AND expected_size=? ORDER BY id DESC LIMIT 1",
            (self.s.device_id, rf.path, rf.size),
        )
        for r in rows:
            p = self.cfg.tmp_dir / r["part_path"]
            if p.exists():
                return p
        # 兜底：扫一遍临时目录里大小匹配的
        for p in self.cfg.tmp_dir.glob("*.part"):
            try:
                if p.stat().st_size == rf.size:
                    return p
            except OSError:
                continue
        return None

    # ------------------------------------------------------------------
    def _record_fast(self, rf, decision) -> None:
        """秒传：不传输，只登记实例。"""
        s = self.s
        s.stats.fast_upload += 1
        if decision.file_id:
            s.db.upsert_instance(
                file_id=decision.file_id, device_id=s.device_id, source_path=rf.path,
                source_mtime=str(rf.mtime) if rf.mtime else None,
                app_tag=rf.app_tag, root_tag=rf.root_tag,
            )
        if self.tracker:
            self.tracker.finish_file(size=rf.size, ok=True, skipped=True)
        log.info("秒传：%s（%s）", rf.name, decision.reason)
        self._emit("file_skip", path=rf.path, reason=decision.reason)

    def _record_failure(self, rf, code: str, detail: str) -> None:
        """记录一个失败文件（会进失败清单，且绝不会进「可放心删」）。"""
        s = self.s
        s.stats.failed += 1
        self.db.add_failure(run_id=s.run_id, source_path=rf.path,
                            reason_code=code, reason_text=detail)
        if self.tracker:
            self.tracker.finish_file(size=0, ok=False)
        log.warning("失败（%s）：%s —— %s", code, rf.path, detail)
        self._emit("file_fail", path=rf.path, code=code, detail=detail)

    # ------------------------------------------------------------------
    def _finish(self, status: str, note: str) -> Session:
        """收尾：写任务记录、备份索引、生成清理清单。"""
        s = self.s
        s.stats.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.db.finish_run(
                s.run_id, status=status,
                total_files=s.stats.accepted, total_bytes=s.stats.accepted_bytes,
                ok=s.stats.transferred, skip=s.stats.fast_upload, fail=s.stats.failed,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("写入任务记录失败：%s", exc)

        # 备份索引库（滚动保留 3 份）
        try:
            s.backup_index()
        except Exception as exc:  # noqa: BLE001
            log.warning("备份索引库失败：%s", exc)

        # 生成清理清单
        if status in ("done", "cancelled", "crashed") and s.stats.transferred + s.stats.fast_upload > 0:
            try:
                s.report = s.cleanup.build(s.device_id)
                self._stage("正在生成清理清单…")
            except Exception as exc:  # noqa: BLE001
                log.warning("生成清理清单失败：%s", exc)

        # 清理过期临时文件
        try:
            s.cleanup_tmp(keep_days=7)
        except Exception:  # noqa: BLE001
            pass

        log.info("任务结束：状态=%s，传输 %d，秒传 %d，失败 %d",
                 status, s.stats.transferred, s.stats.fast_upload, s.stats.failed)
        self._emit("finish", status=status, stats=s.stats.as_row())
        return s

    # ------------------------------------------------------------------
    def export_reports(self) -> dict[str, list[Path]]:
        """导出全部报告文件（CSV / TXT / 失败清单）。"""
        s = self.s
        out: dict[str, list[Path]] = {"csv": [], "txt": [], "script": []}
        if s.report is None:
            return out
        out["csv"] = s.cleanup.export_csv(s.report)
        out["txt"] = s.cleanup.export_txt(s.report)
        return out
