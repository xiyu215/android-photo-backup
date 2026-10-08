"""清理清单：四档判定、导出 CSV/TXT、导出「手动删除脚本」。

安全底线（不可协商）
--------------------
本模块**只生成文件**，绝不执行任何删除动作。删除永远由用户手动完成。
用户明确要求（Q13）额外生成一份"删除脚本"，因此这里提供了
:func:`export_delete_script`——但它产出的 ``.bat`` 里带有醒目警告与
``pause`` 二次确认，且必须由用户自己双击运行，程序不会去调用它。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..common import i18n, paths
from ..common.logger import get_logger
from .config import Config
from .db import IndexDB
from .scanner import is_cache_path

log = get_logger("cleanup")

# 档位常量统一从 i18n 取，保证终端、CSV、网页三处措辞一致
TIER_SAFE = i18n.TIER_SAFE
TIER_KEEP = i18n.TIER_KEEP
TIER_CACHE = i18n.TIER_CACHE
TIER_PENDING = i18n.TIER_PENDING
TIER_ORDER = i18n.TIER_ORDER


@dataclass
class CleanupItem:
    """清单里的一行。"""

    tier: str
    filename: str
    size: int
    taken_at: str
    time_source: str
    time_confidence: int
    phone_path: str
    pc_path: str
    app_tag: str
    sha256: str
    note: str = ""

    @property
    def size_text(self) -> str:
        return paths.human_size(self.size)


@dataclass
class CleanupReport:
    """一次清理建议的完整结果。"""

    items: dict[str, list[CleanupItem]] = field(default_factory=dict)
    generated_at: str = ""

    def tier(self, name: str) -> list[CleanupItem]:
        return self.items.get(name, [])

    def count(self, name: str) -> int:
        return len(self.items.get(name, []))

    def size(self, name: str) -> int:
        return sum(i.size for i in self.items.get(name, []))

    def total(self) -> int:
        return sum(len(v) for v in self.items.values())

    def summary_lines(self) -> list[str]:
        lines = []
        for t in TIER_ORDER:
            lines.append(f"{t}：{self.count(t)} 个文件，合计 {paths.human_size(self.size(t))}")
        return lines


class CleanupBuilder:
    """根据索引库生成四档清理建议。"""

    def __init__(self, cfg: Config, db: IndexDB) -> None:
        self.cfg = cfg
        self.db = db
        self.root = cfg.backup_root_path
        self._storage_cache: dict[int, str | None] = {}

    # ------------------------------------------------------------------
    def build(self, device_id: int) -> CleanupReport:
        """扫描索引，生成分档清单。

        判定顺序刻意设计为"先判缓存、再判未备份、最后才判可删"，
        保证任何"不确定"的情况都会落到「暂不能删」，而不是误判为可删。
        """
        report = CleanupReport(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        for t in TIER_ORDER:
            report.items[t] = []

        rows = self.db.raw_query(
            "SELECT i.source_path, i.app_tag, i.root_tag,"
            "       f.id AS file_id, f.sha256, f.size, f.taken_at, f.taken_at_source,"
            "       f.time_confidence, f.is_time_uncertain, f.verified, f.second_read_ok"
            "  FROM instances i JOIN files f ON f.id = i.file_id"
            " WHERE i.device_id = ? AND i.is_present = 1",
            (device_id,),
        )

        seen_paths: set[str] = set()
        for r in rows:
            item = self._judge(r)
            report.items[item.tier].append(item)
            seen_paths.add(item.phone_path)

        # ---- 未备份文件：让清单覆盖"手机上确实存在、但没进备份"的那部分 ----
        # 少了这一段，用户会在清单里找不到某个文件，误以为工具漏扫了。
        for r in self._unbacked_rows(device_id):
            src = r["source_path"]
            if src in seen_paths:
                continue  # 同一路径已由 instances 覆盖，不重复列
            tier = TIER_CACHE if (r["tier_hint"] == "cache") else TIER_PENDING
            note = r["reason_text"] or "未备份"
            if tier == TIER_CACHE:
                note = f"{note}，删除无损失"
            else:
                note = f"未备份（{note}），电脑上没有副本，请勿删除"
            report.items[tier].append(CleanupItem(
                tier=tier,
                filename=src.rsplit("/", 1)[-1],
                size=int(r["size"] or 0),
                taken_at="",
                time_source="unknown",
                time_confidence=0,
                phone_path=src,
                pc_path="",
                app_tag="Other",
                sha256="",
                note=note,
            ))

        for t in TIER_ORDER:
            report.items[t].sort(key=lambda x: x.size, reverse=True)
        return report

    def _unbacked_rows(self, device_id: int):
        """读「未备份文件」表；老库没有这张表时安静地当作空。"""
        try:
            return self.db.unbacked_of_device(device_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取未备份清单失败：%s", exc)
            return []

    # ------------------------------------------------------------------
    def _judge(self, row) -> CleanupItem:
        """对单个实例做四档判定。"""
        src = row["source_path"]
        name = src.rsplit("/", 1)[-1]
        file_id = row["file_id"]
        size = int(row["size"])
        verified = bool(row["verified"])
        second_ok = row["second_read_ok"]
        uncertain = bool(row["is_time_uncertain"])
        app_tag = row["app_tag"] or "Other"

        pc_rel = self._storage_of(file_id)
        base = dict(
            filename=name,
            size=size,
            taken_at=row["taken_at"] or "",
            time_source=row["taken_at_source"] or "unknown",
            time_confidence=int(row["time_confidence"] or 0),
            phone_path=src,
            pc_path=pc_rel or "",
            app_tag=app_tag,
            sha256=row["sha256"],
        )

        # ---- ③ App 缓存可清（优先级最高：缓存没有备份价值） ----
        # 注意：这里的路径启发式只对**手机侧路径**成立。宿主机的本地绝对路径
        # （例如 Windows 的 C:\Users\x\AppData\Local\Temp\...）会命中 "/temp/"
        # 而被误判成缓存 —— 曾经真的踩过这个坑：整份清单都被标成"缓存可清"，
        # 连相机原片都绕过了"建议保留"的保护。因此加一道前置判断：只有以 "/"
        # 开头的 POSIX 绝对路径（手机路径的形态）才参与启发式判定。
        if (src.startswith("/") and is_cache_path(src)) or app_tag in ("Cache",):
            return CleanupItem(TIER_CACHE, note="缓存/缩略图，删除无损失", **base)

        # ---- ④ 暂不能删：一切"不确定" ----
        if not verified:
            return CleanupItem(TIER_PENDING, note="尚未通过双端校验，请勿删除", **base)
        if second_ok == 0:
            return CleanupItem(TIER_PENDING, note="写盘后复读校验失败，请先检查磁盘", **base)
        if not pc_rel:
            return CleanupItem(TIER_PENDING, note="索引中找不到对应的备份文件，请先运行体检", **base)
        if not self._file_exists(pc_rel):
            return CleanupItem(TIER_PENDING, note="备份文件在磁盘上不存在，请先运行体检", **base)
        if uncertain and not self.cfg.uncertain_can_delete:
            return CleanupItem(TIER_PENDING, note="时间存疑（可能是转存/下载当天），建议人工确认", **base)

        # ---- ② 建议保留：相机原片 ----
        if self.cfg.keep_camera_original and app_tag in ("Camera", "Screenshot"):
            return CleanupItem(TIER_KEEP, note="原始拍摄素材，建议再确认一次", **base)

        # ---- ① 可放心删 ----
        note = "双端校验通过，电脑上已有完整副本"
        if second_ok is None:
            note += "（未做写后复读校验）"
        return CleanupItem(TIER_SAFE, note=note, **base)

    # ------------------------------------------------------------------
    def _storage_of(self, file_id: int) -> str | None:
        """取该内容在归档树中的相对路径（不存在则取任意一条）。"""
        if file_id in self._storage_cache:
            return self._storage_cache[file_id]
        rows = self.db.storages_of_file(file_id)
        chosen = None
        for r in rows:
            if r["tree"] == "archive":
                chosen = r["rel_path"]
                break
        if chosen is None and rows:
            chosen = rows[0]["rel_path"]
        self._storage_cache[file_id] = chosen
        return chosen

    def _file_exists(self, rel: str) -> bool:
        try:
            return paths.safe_join(self.root, rel).is_file()
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_csv(self, report: CleanupReport, out_dir: str | Path | None = None,
                   *, per_tier: bool = True) -> list[Path]:
        """导出 CSV（UTF-8 with BOM，Excel 双击不乱码）。

        :param per_tier: True=每档一个文件；False=合并成一个总表。
        """
        d = paths.ensure_dir(out_dir or self.cfg.report_dir)
        stamp = datetime.now().strftime("%Y-%m-%d")
        written: list[Path] = []

        if per_tier:
            for tier in TIER_ORDER:
                items = report.tier(tier)
                if not items and tier != TIER_SAFE:
                    continue  # 空档位不生成文件（可放心删即使为空也生成，便于确认）
                p = d / f"{tier}_{stamp}.csv"
                self._write_csv(items, p, title=tier)
                written.append(p)
        else:
            p = d / f"清理清单_{stamp}.csv"
            all_items = [i for t in TIER_ORDER for i in report.tier(t)]
            self._write_csv(all_items, p, title="全部")
            written.append(p)
        return written

    def _write_csv(self, items: list[CleanupItem], path: Path, *, title: str) -> None:
        # newline="" + utf-8-sig：Windows Excel 打开中文与 emoji 都不乱码
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(i18n.CSV_HEADERS)
            for idx, it in enumerate(items, 1):
                writer.writerow([
                    idx,
                    it.tier,
                    it.filename,
                    it.size,
                    it.size_text,
                    it.taken_at,
                    i18n.TIME_SOURCE_LABEL.get(it.time_source, it.time_source),
                    i18n.TIME_CONFIDENCE_LABEL.get(it.time_confidence, str(it.time_confidence)),
                    it.phone_path,
                    it.pc_path,
                    i18n.APP_TAG_LABEL.get(it.app_tag, it.app_tag),
                    it.sha256,
                    it.note,
                ])
        log.info("已导出 CSV：%s（%d 行）", path, len(items))

    def export_txt(self, report: CleanupReport, out_dir: str | Path | None = None) -> list[Path]:
        """导出 TXT（手机上对照着删最方便：每行都带手机原始路径）。"""
        d = paths.ensure_dir(out_dir or self.cfg.report_dir)
        stamp = datetime.now().strftime("%Y-%m-%d")
        written: list[Path] = []
        for tier in TIER_ORDER:
            items = report.tier(tier)
            if not items and tier != TIER_SAFE:
                continue
            p = d / f"{tier}_{stamp}.txt"
            with open(p, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("=" * 72 + "\n")
                fh.write(f"  {tier} —— 共 {len(items)} 个文件，"
                         f"合计 {paths.human_size(report.size(tier))}\n")
                fh.write(f"  说明：{i18n.TIER_DESC.get(tier, '')}\n")
                fh.write("=" * 72 + "\n\n")
                fh.write("【重要】本清单只是建议，删除动作请你自己在手机上手动完成。\n\n")
                for i, it in enumerate(items, 1):
                    fh.write(f"[{i:04d}] {it.filename}\n")
                    fh.write(f"       大小：{it.size_text}    "
                             f"时间：{it.taken_at or '未知'}    "
                             f"来源：{i18n.APP_TAG_LABEL.get(it.app_tag, it.app_tag)}\n")
                    fh.write(f"       手机路径：{it.phone_path}\n")
                    fh.write(f"       备份路径：{it.pc_path}\n")
                    if it.note:
                        fh.write(f"       备注：{it.note}\n")
                    fh.write("\n")
            written.append(p)
            log.info("已导出 TXT：%s（%d 行）", p, len(items))
        return written

    def export_delete_script(
        self,
        report: CleanupReport,
        *,
        adb_exe: str,
        serial: str = "",
        out_dir: str | Path | None = None,
        tiers: tuple[str, ...] = (TIER_SAFE,),
    ) -> Path | None:
        """生成"手动删除脚本"（用户 Q13 要求）。

        这是一个**带二次确认的 Windows 批处理文件**：
            - 第一屏打印醒目警告；
            - 必须按任意键才会继续；
            - 逐条删除前再回显一次路径；
            - 程序自身永远不会调用它。

        :param tiers: 只对哪些档位生成删除命令，默认只包含「可放心删」。
        """
        items = [i for t in tiers for i in report.tier(t)]
        if not items:
            log.info("没有需要生成删除脚本的文件（目标档位为空）")
            return None

        d = paths.ensure_dir(out_dir or self.cfg.report_dir)
        stamp = datetime.now().strftime("%Y-%m-%d")
        out = d / f"删除脚本_{stamp}_请确认后再运行.bat"

        lines: list[str] = [
            "@echo off",
            "chcp 65001 >nul",
            "title 安卓相册备份 - 手动删除脚本（请谨慎操作）",
            "echo ============================================================",
            "echo   警告：运行本脚本会从你的手机上删除文件，且无法撤销！",
            "echo ============================================================",
            "echo.",
            "echo   执行前请务必确认：",
            f"echo     1) 共 {len(items)} 个文件，"
            f"合计 {paths.human_size(sum(i.size for i in items))}",
            "echo     2) 电脑上的备份文件都在，并且能正常打开",
            "echo     3) 手机已用数据线连接，并已授权 USB 调试",
            "echo.",
            "echo   本脚本由「安卓相册备份」生成，程序本身不会自动运行它。",
            "echo   如果上面的备份还没确认好，请直接关掉这个窗口。",
            "echo.",
            "pause",
            "echo.",
            "echo 开始删除……",
            "echo.",
        ]

        adb = str(adb_exe)
        sflag = f' -s "{serial}"' if serial else ""
        for it in items:
            # 逐条回显 + 删除。用 -f 忽略不存在的文件，避免中途中断。
            lines.append(f'echo   删除 {it.phone_path}')
            lines.append(f'"{adb}"{sflag} shell rm -f "{it.phone_path}"')
        lines += [
            "echo.",
            "echo 删除完成。建议在手机上打开相册确认一下。",
            "echo.",
            "pause",
        ]

        out.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
        log.info("已生成手动删除脚本：%s（%d 条命令）", out, len(items))
        return out
