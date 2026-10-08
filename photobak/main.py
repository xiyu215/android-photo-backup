"""程序入口：自检 → 配置 → 连接 → 备份 → 报告。

交互式主菜单
------------
直接双击运行时，会显示一个菜单让用户选择连接方式；也支持命令行参数
（见 ``--help``）以便自动化与排障。

崩溃恢复
--------
启动时会检查索引库里是否有"上次未正常结束"的任务（``status='running'``），
若有则提示用户"继续即可从断点续传"，并把那条记录标记为 crashed。
真正保证续传的是 ``.photobak/tmp/*.part`` 与 ``transfer_jobs`` 表，
与这条提示无关——提示只是让用户安心。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path

from . import __version__
from .app.orchestrator import Orchestrator
from .app.session import build_session
from .app.state import RuntimeState
from .channels.adb_client import AdbClient, human_adb_hint
from .channels.usb_adb import UsbAdbSource
from .channels.wifi_adb import WifiAdbSource, parse_host_port
from .common import i18n, logger, netutil, paths
from .common.errors import EnvError, PhotobakError
from .core import config as cfgmod
from .core.cleanup import TIER_ORDER
from .core.db import IndexDB
from .core.doctor import Doctor
from .ui import terminal
from .ui.progress import ProgressTracker, format_duration
from .ui.tray import TrayIcon
from .ui.webserver import WebContext, WebServer

log = logger.get_logger("main")


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------
def self_check() -> None:
    """检查包内资源是否完整（用户最常见的失败原因就是"在压缩包里直接双击"）。"""
    missing: list[str] = []
    if paths.find_adb_dir() is None:
        missing.append("adb/adb.exe")
    if not paths.resource_path("webui", "index.html").is_file():
        missing.append("webui/index.html")
    if missing:
        raise EnvError(
            "E611",
            "缺少必需文件：" + "、".join(missing)
            + "。请右键压缩包 → 解压到文件夹（不要直接在压缩包里运行），然后重新双击。",
        )


def prepend_adb_to_path() -> Path | None:
    """把包内 adb 目录前置到 PATH。

    用户明确要求"避免与系统已有 ADB 版本冲突"：包内 adb 永远优先。
    注意：所有实际调用都使用绝对路径，PATH 只是给子进程环境的保险。
    """
    adb_dir = paths.find_adb_dir()
    if adb_dir is None:
        return None
    os.environ["PATH"] = str(adb_dir) + os.pathsep + os.environ.get("PATH", "")
    return adb_dir


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def connect_usb(adb_dir: Path | None) -> UsbAdbSource:
    """连接 USB 设备。

    这里**不**打印错误：连接失败一律往上抛，由 :func:`main` 统一展示。
    早期版本在这里打了一次、main 里又打了一次，用户会看到两遍同样的提示。
    """
    exe = (adb_dir / "adb.exe") if adb_dir else None
    terminal.step("正在通过 USB 检测设备…")
    return UsbAdbSource.connect(exe, wait_seconds=2.0)


def connect_wifi_pair(adb_dir: Path | None) -> WifiAdbSource:
    """无线调试配对（Android 11+，无需数据线）。"""
    exe = (adb_dir / "adb.exe") if adb_dir else None
    terminal.out()
    terminal.out(i18n.MSG_WIFI_PAIR_GUIDE)
    terminal.out()
    pair_target = terminal.ask("请把手机上「使用配对码配对设备」页显示的 IP:端口 粘贴到这里")
    if not pair_target:
        raise EnvError("E113", "未输入配对地址")
    pair_code = terminal.ask("请输入 6 位配对码")
    connect_target = terminal.ask("（可选）再粘贴「无线调试」主页显示的 IP:端口，直接回车可跳过",
                                  default="")
    return WifiAdbSource.pair_and_connect(
        pair_target=pair_target, pair_code=pair_code,
        connect_target=connect_target or None, adb_exe=exe,
    )


def connect_wifi_tcpip(adb_dir: Path | None) -> WifiAdbSource:
    """备选：先用 USB 打开 tcpip，再无线连接。"""
    exe = (adb_dir / "adb.exe") if adb_dir else None
    terminal.out()
    terminal.out(i18n.MSG_WIFI_TCPIP_GUIDE)
    terminal.out()
    terminal.pause("确认手机已用数据线连接并已授权后，按回车继续…")
    return WifiAdbSource.connect_with_fallback(adb_exe=exe)


def connect_wifi_manual(adb_dir: Path | None) -> WifiAdbSource:
    """已知 IP:端口时直接连接。"""
    exe = (adb_dir / "adb.exe") if adb_dir else None
    target = terminal.ask("请输入手机的 IP:端口（例如 192.168.1.23:5555）")
    if not target:
        raise EnvError("E108", "未输入地址")
    return WifiAdbSource.connect(target, adb_exe=exe)


# ---------------------------------------------------------------------------
# 主菜单
# ---------------------------------------------------------------------------
MENU = [
    ("1", "USB 数据线备份（最稳定，推荐）"),
    ("2", "WiFi 无线备份 · 配对码方式（Android 11+，无需数据线）"),
    ("3", "WiFi 无线备份 · 先用 USB 打开无线（备选）"),
    ("4", "WiFi 无线备份 · 手动输入 IP 连接"),
    ("5", "试运行（只扫描出报告，不传输任何文件）"),
    ("6", "索引体检与修复"),
    ("7", "查看帮助与使用说明"),
    ("0", "退出"),
]


def show_menu() -> str:
    terminal.section("请选择操作")
    for key, text in MENU:
        terminal.out(f"  [{key}] {text}")
    terminal.out()
    return terminal.ask("请输入序号", default="1")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def show_run_summary(session, tracker: ProgressTracker) -> None:
    terminal.section("备份结果")
    terminal.kv_table(list(session.stats.as_row().items()))

    snap = tracker.snapshot()
    terminal.out()
    terminal.kv_table([
        ("耗时", format_duration(snap.elapsed)),
        ("平均速度", f"{paths.human_size(snap.done_bytes / snap.elapsed if snap.elapsed else 0)}/s"),
    ])

    if session.report is None:
        return

    terminal.section("清理建议（四档）")
    terminal.show_tier_summary(session.report)
    terminal.out()
    terminal.warn("工具不会删除你手机上的任何文件；请对照清单自己手动删除。")

    failures = session.db.failures_of_run(session.run_id)
    if failures:
        terminal.section(f"失败清单（{len(failures)} 个）")
        for r in failures[:20]:
            terminal.out(f"  {r['reason_code']}  {r['source_path']}")
            terminal.out(f"        {r['reason_text']}")
        if len(failures) > 20:
            terminal.dim(f"  …还有 {len(failures) - 20} 条，详见报告目录")


def do_export(session) -> list[Path]:
    """导出 CSV/TXT/失败清单，返回产出文件列表。

    **曾经的真实 bug**：这里原来调用 ``session.export_reports()``，而 ``Session``
    根本没有这个方法（它只提供 ``session.cleanup`` 与 ``session.report``）。
    异常被下面的 except 吞掉、只留一行 warning，于是"备份完成后给你一份可清理
    清单"这个承诺**从未兑现**——reports 目录一直是空的，而用户看不出哪里不对。
    现在改为直接调用 :class:`CleanupBuilder` 的 ``export_csv`` / ``export_txt``。
    """
    files: list[Path] = []

    if session.report is None:
        log.warning("本次没有生成清理清单，跳过报告导出。")
    else:
        for name, fn in (("CSV", session.cleanup.export_csv),
                         ("TXT", session.cleanup.export_txt)):
            try:
                files += fn(session.report, out_dir=session.cfg.report_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("导出 %s 失败：%s", name, exc)

    # 失败清单
    try:
        failures = session.db.failures_of_run(session.run_id)
        if failures:
            import csv
            from datetime import datetime

            p = session.cfg.report_dir / f"失败清单_{datetime.now():%Y-%m-%d}.csv"
            with open(p, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["序号", "错误码", "手机路径", "原因", "时间"])
                for i, r in enumerate(failures, 1):
                    w.writerow([i, r["reason_code"], r["source_path"],
                                r["reason_text"], r["occurred_at"]])
            files.append(p)
    except Exception as exc:  # noqa: BLE001
        log.warning("导出失败清单失败：%s", exc)

    return files


def maybe_export_delete_script(session, adb_dir: Path | None, tracker) -> None:
    """询问是否生成"手动删除脚本"（用户 Q13 要求，默认询问）。"""
    if session.report is None or session.report.count(TIER_ORDER[0]) == 0:
        return
    terminal.out()
    terminal.out(f"「{TIER_ORDER[0]}」档共有 {session.report.count(TIER_ORDER[0])} 个文件。")
    terminal.dim("本工具绝不自动删除手机文件。你可以让它生成一个删除脚本，"
                 "由你自己检查后手动双击运行。")
    if not terminal.confirm("是否生成「删除脚本」（需要你手动执行，默认不生成）", default=False):
        return
    exe = (adb_dir / "adb.exe") if adb_dir else Path("adb.exe")
    try:
        p = session.cleanup.export_delete_script(
            session.report, adb_exe=str(exe), serial=session.device.serial,
        )
        if p:
            terminal.ok(f"已生成：{p}")
            terminal.warn("请务必先确认电脑上的备份文件都能正常打开，再运行该脚本。")
    except Exception as exc:  # noqa: BLE001
        terminal.error(f"生成删除脚本失败：{exc}")


# ---------------------------------------------------------------------------
# 手机查看入口（终端二维码）
# ---------------------------------------------------------------------------
def show_phone_qr(url: str) -> None:
    """在终端用字符块画出二维码，手机扫一下就能打开查看页。

    为什么要二维码
    --------------
    手机查看页的地址长这样：``http://192.168.1.7:8765/?t=<32 位令牌>``。
    让用户在手机浏览器里手敲这串东西不现实，二维码是唯一省事的入口。

    为什么是字符块而不是图片
    ------------------------
    用户明确要求"不生成图片文件、减少依赖"（见设计文档约束）。这里用半角块
    字符（▀▄█）把两个模块行压进一行文本，模块接近正方形，手机能正常识别。

    容错
    ----
    二维码画不出来（终端过窄、``qrcode`` 库缺失、系统不支持）**绝不能影响
    主流程** —— 备份已经做完了，绝不能因为一个装饰性输出而报错退出。
    因此整体兜底：失败时只记一条调试日志，用户仍然能拿到上面那行地址。
    """
    try:
        from .ui import qrterm

        text = qrterm.render_for_terminal(url, color_enabled=logger._use_color)  # noqa: SLF001
        terminal.out()
        terminal.out(text)
        terminal.dim("用手机相机或微信扫上面的码即可打开查看页（需与电脑连同一个 WiFi）。")
    except Exception as exc:  # noqa: BLE001
        log.debug("二维码渲染跳过：%s", exc)


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------
def run_doctor(cfg: cfgmod.Config, db: IndexDB, *, deep: bool = False) -> None:
    terminal.section("索引体检")
    doc = Doctor(cfg, db)
    terminal.step("正在核对索引与磁盘文件…")
    report = doc.check(deep=deep)
    terminal.out()
    terminal.kv_table([
        ("检查条目", str(report.checked)),
        ("索引有但文件缺失", str(len(report.missing_files))),
        ("文件存在但未登记", str(len(report.unregistered))),
        ("大小/内容不一致", str(len(report.size_mismatch))),
    ])
    if report.healthy and not report.unregistered:
        terminal.ok("一切正常，索引与文件完全一致。")
        return
    if not terminal.confirm("是否现在修复？", default=True):
        return
    report = doc.repair(report)
    terminal.ok(f"修复完成，共处理 {report.repaired} 项。")
    terminal.dim("说明：缺失的文件对应的手机文件会被自动降档到「暂不能删」，不会误删。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="安卓相册备份",
        description=f"{i18n.APP_TITLE} v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  安卓相册备份.exe                    交互式菜单（推荐）\n"
            "  安卓相册备份.exe --usb --yes        直接走 USB 备份，不再询问\n"
            "  安卓相册备份.exe --wifi --yes       走 WiFi 配对码备份\n"
            "  安卓相册备份.exe --dry-run          只扫描出报告，不传输\n"
            "  安卓相册备份.exe --doctor           索引体检与修复\n"
        ),
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--usb", action="store_true", help="直接使用 USB 通道")
    g.add_argument("--wifi", action="store_true", help="直接使用 WiFi 配对码方式")
    g.add_argument("--wifi-tcpip", action="store_true", help="先用 USB 打开无线，再无线连接")
    g.add_argument("--wifi-connect", metavar="IP:PORT", help="直接连接指定的无线设备")
    g.add_argument("--doctor", action="store_true", help="只做索引体检与修复")
    p.add_argument("--dry-run", action="store_true", help="试运行：只扫描，不传输不落盘")
    p.add_argument("--deep", action="store_true", help="体检时对文件做完整哈希校验（慢）")
    p.add_argument("--backup-root", metavar="PATH", help="指定备份根目录（覆盖配置文件）")
    p.add_argument("--config", metavar="PATH", help="指定配置文件路径")
    p.add_argument("--port", type=int, help="本地管理页端口（默认 8765）")
    p.add_argument("--no-gui", action="store_true", help="不启动本地管理页与托盘图标")
    p.add_argument("--yes", "-y", action="store_true", help="跳过所有确认（用于自动化）")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    # ---- 0. 终端准备 ----
    # 必须放在最前面，**早于** argparse。
    # 原因：`--version` / `--help` 是在 parse_args() 内部直接打印并退出的，
    # 如果等到之后才切 UTF-8，这两条输出就会用系统默认代码页（中文 Windows 上
    # 是 GBK）编码，在 UTF-8 终端里显示成乱码。
    logger.enable_utf8_console()
    args = build_parser().parse_args(argv)

    # ``--yes`` 必须**立刻**生效，而不是等到调用编排器时。
    # 后面收尾阶段还有"是否生成删除脚本"、"按回车退出"这类交互提问，
    # 它们不经过编排器；不在这里打开开关的话，自动化场景会卡在这些提问上
    # （实测：备份 8 秒跑完，然后永久挂住）。
    terminal.set_auto_confirm(args.yes)

    color = "auto"

    # ---- 1. 自检 ----
    try:
        self_check()
    except PhotobakError as exc:
        logger.setup(None, "INFO", color)
        terminal.show_banner()
        terminal.show_error(exc)
        return 2

    adb_dir = prepend_adb_to_path()

    # ---- 2. 配置 ----
    try:
        cfg = cfgmod.load(args.config)
    except Exception as exc:  # noqa: BLE001
        logger.setup(None, "INFO", color)
        terminal.show_banner()
        terminal.error(f"读取配置失败：{exc}")
        return 2

    color = cfg.color
    logger.setup(None, cfg.log_level, color)
    terminal.show_banner()
    terminal.safety_notice()

    if args.backup_root:
        cfg.backup_root = args.backup_root
    if args.port:
        cfg.port = args.port

    # ---- 3. 备份根目录（三级降级） ----
    try:
        cfgmod.resolve_backup_root(cfg, interactive=not args.yes)
    except PhotobakError as exc:
        terminal.show_error(exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        terminal.error(f"确定备份目录失败：{exc}")
        return 2

    # 有了备份根之后，日志改写到备份目录下（便于随备份一起搬走）
    logger.setup(cfg.log_dir, cfg.log_level, color)
    log.info("日志文件：%s", logger.current_log_file())

    terminal.section("本次配置")
    terminal.kv_table([
        ("备份目录", str(cfg.backup_root_path)),
        ("归档目录", str(cfg.archive_root)),
        ("镜像目录", str(cfg.mirror_root)),
        ("扫描范围", "、".join(cfg.include_dirs)),
        ("包含 Android/data", "是" if cfg.include_android_data else "否"),
        ("二次读校验", cfg.second_read),
        ("单文件上限", f"{cfg.max_file_mb} MB"),
        ("镜像链接方式", cfg.mirror_link_mode),
    ])

    # ---- 4. 索引库 ----
    db = IndexDB(cfg.index_db)
    try:
        db.connect()
    except PhotobakError as exc:
        terminal.show_error(exc)
        return 2

    ok, msg = db.integrity_check()
    if not ok:
        terminal.warn(f"索引库自检异常（{msg}），正在尝试重建索引…")
        try:
            db.reindex()
        except Exception as exc:  # noqa: BLE001
            log.warning("重建索引失败：%s", exc)

    # ---- 5. 崩溃恢复提示 ----
    unfinished = db.last_unfinished_run()
    if unfinished:
        terminal.warn(f"检测到上次备份未正常结束（{unfinished['started_at']}）。")
        terminal.dim("继续运行即可从断点续传，已传输的部分不会重来。")
        db.mark_run_crashed(int(unfinished["id"]))

    # ---- 6. 体检模式 ----
    if args.doctor:
        run_doctor(cfg, db, deep=args.deep)
        db.close()
        if not args.no_gui:
            terminal.pause("按回车退出…")
        return 0

    # ---- 7. 启动本地管理页 + 托盘 ----
    tracker = ProgressTracker()
    state = RuntimeState(
        backup_root=str(cfg.backup_root_path),
        report_dir=str(cfg.report_dir),
        restore_dir=cfg.restore_dir,
        log_file=str(logger.current_log_file() or ""),
    )
    token = cfg.access_token or secrets.token_urlsafe(12)
    web = None
    tray = None
    holder: dict = {"session": None, "source": None}

    if not args.no_gui:
        web, tray = _start_ui(cfg, tracker, state, token, db, holder)

    # ---- 8~9. 选择模式 + 连接 ----
    #
    # 这两步放在一个循环里。理由很实际：第一次用的人几乎必然会撞上
    # "未检测到设备"——数据线不对、手机没授权、USB 模式没切。
    # 如果一失败就退出程序，用户得重新双击、重新走一遍前面的流程，
    # 体验很差。放在循环里，他改完线或授权后按个回车就能重试。
    source = None
    dry_run = args.dry_run
    while source is None:
        mode = _decide_mode(args)
        if mode == "0":
            terminal.out("已退出。")
            db.close()
            return 0
        if mode == "7":
            terminal.out()
            terminal.out(i18n.MSG_NO_DEVICE_GUIDE)
            terminal.out()
            terminal.out(i18n.MSG_WIFI_PAIR_GUIDE)
            terminal.pause("按回车返回菜单…")
            continue

        dry_run = args.dry_run or mode == "5"
        if mode == "6":
            run_doctor(cfg, db)
            db.close()
            return 0

        try:
            if mode == "2" or args.wifi:
                source = connect_wifi_pair(adb_dir)
            elif mode == "3" or args.wifi_tcpip:
                source = connect_wifi_tcpip(adb_dir)
            elif mode == "4":
                source = connect_wifi_manual(adb_dir)
            elif args.wifi_connect:
                source = WifiAdbSource.connect(args.wifi_connect,
                                               adb_exe=(adb_dir / "adb.exe") if adb_dir else None)
            else:
                source = connect_usb(adb_dir)
        except PhotobakError as exc:
            terminal.show_error(exc)
            terminal.out("提示：也可以改用 WiFi 方式（菜单第 2 项）重试。")
            if args.yes or not terminal.confirm("处理完之后要重试吗？", default=True):
                db.close()
                return 3
        except Exception as exc:  # noqa: BLE001
            terminal.error(f"连接失败：{exc}")
            if args.yes or not terminal.confirm("要重试吗？", default=True):
                db.close()
                return 3

    holder["source"] = source

    # ---- 10. 会话 ----
    try:
        session = build_session(cfg, db, source, tracker)
    except PhotobakError as exc:
        terminal.show_error(exc)
        db.close()
        return 3

    holder["session"] = session
    state.update(
        device=session.device.display_name, serial=session.device.serial,
        channel=source.name, android=f"{session.device.android_release} (API {session.device.android_version})",
        can_restore=cfg.enable_restore,
        caps={"自动枚举": True, "秒传": source.capabilities().remote_hash,
              "断点续传": source.capabilities().range_read},
    )

    terminal.section("设备信息")
    terminal.kv_table([
        ("设备型号", session.device.display_name),
        ("序列号", session.device.serial),
        ("Android 版本", f"{session.device.android_release}（API {session.device.android_version}）"),
        ("存储根目录", session.device.storage_root),
        ("连接通道", source.name),
        ("通道能力", source.capabilities().describe()),
    ])

    orch = Orchestrator(session)

    # ---- 11. 扫描计划确认 ----
    plan = orch.build_plan()
    terminal.section("扫描计划")
    terminal.show_scan_plan(plan.rows)
    if not args.yes and not terminal.confirm("按以上范围开始备份？", default=True):
        terminal.out("已取消。")
        source.close()
        db.close()
        return 0

    # ---- 12. 执行 ----
    cancel_flag = {"v": False}
    try:
        session = orch.run(dry_run=dry_run, cancel=lambda: cancel_flag["v"],
                           auto_confirm=args.yes)
    except KeyboardInterrupt:
        terminal.out()
        terminal.warn("已中断。已传输的文件均已保留，下次运行会自动续传。")
    except PhotobakError as exc:
        terminal.show_error(exc)
    except Exception as exc:  # noqa: BLE001 —— 最后一道兜底
        log.exception("未预期错误")
        terminal.error(f"发生未预期错误：{exc}")
        terminal.dim(f"详细日志：{logger.current_log_file()}")

    # ---- 13. 报告 ----
    if dry_run:
        terminal.section("试运行结果")
        terminal.kv_table(list(session.stats.as_row().items()))
        terminal.dim("试运行不传输、不写盘；去掉 --dry-run 或选择其他菜单项即可正式备份。")
    else:
        show_run_summary(session, tracker)
        files = do_export(session)
        if files:
            terminal.section("报告已导出")
            for f in files:
                terminal.out(f"  {f}")
        maybe_export_delete_script(session, adb_dir, tracker)

        if session.report is not None:
            state.update(cleanup_ready=True, last_run_status="done")

        if cfg.auto_open_browser or True:
            try:
                session.open_in_explorer(cfg.report_dir)
            except Exception:  # noqa: BLE001
                pass

    # ---- 14. 收尾 ----
    if tray is not None and tray.available and not dry_run:
        tray.notify(
            f"备份完成：传输 {session.stats.transferred} 个，"
            f"秒传 {session.stats.fast_upload} 个，失败 {session.stats.failed} 个"
        )

    source.close()
    if not args.no_gui and web is not None:
        terminal.out()
        terminal.ok(f"本地管理页：{web.url}")
        lan = netutil.best_lan_ip()
        if lan:
            lan_url = web.lan_url(lan)
            terminal.ok(f"手机查看（同一 WiFi）：{lan_url}")
            show_phone_qr(lan_url)
        terminal.dim("管理页里可以查看清理清单、导出 CSV/TXT、把照片放回手机。")
        terminal.pause("全部完成。按回车退出程序…")
    else:
        terminal.out()
        terminal.ok("全部完成。")

    if tray is not None:
        tray.stop()
    if web is not None:
        web.stop()
    db.close()
    return 0


def _decide_mode(args) -> str:
    """决定运行模式（命令行优先，否则弹菜单）。"""
    if args.usb:
        return "1"
    if args.wifi:
        return "2"
    if args.wifi_tcpip:
        return "3"
    if args.wifi_connect:
        return "4"
    if args.dry_run:
        return "5"
    if args.doctor:
        return "6"
    if args.yes:
        return "1"      # 自动化默认走 USB
    return show_menu()


def _start_ui(cfg, tracker: ProgressTracker, state: RuntimeState,
              token: str, db: IndexDB, holder: dict):
    """启动本地网页管理页与托盘图标（实现见 :func:`_start_ui_impl`）。"""
    return _start_ui_impl(cfg, tracker, state, token, db, holder)


def _start_ui_impl(cfg, tracker, state, token, db, holder):
    from .core import restore as restore_mod

    port = cfg.port
    if not netutil.is_port_free(port):
        try:
            port = netutil.find_free_port(port + 1)
            terminal.warn(f"端口 {cfg.port} 被占用，已改用 {port}（E601）")
        except OSError as exc:
            terminal.warn(f"找不到可用端口：{exc}；已关闭本地管理页")
            return None, None

    def _get_cleanup():
        s = holder.get("session")
        return s.report if s else None

    def _export_cleanup(tier: str, fmt: str):
        s = holder.get("session")
        if not s or s.report is None:
            return None
        if fmt == "csv":
            return s.cleanup.export_csv(s.report, per_tier=False)[0]
        return s.cleanup.export_txt(s.report, per_tier=False)[0]

    def _resolve_ids(paths_list: list[str]) -> list[int]:
        s = holder.get("session")
        if not s:
            return []
        out: list[int] = []
        for p in paths_list:
            rows = db.raw_query(
                "SELECT file_id FROM instances WHERE device_id=? AND source_path=?",
                (s.device_id, p),
            )
            if rows:
                out.append(int(rows[0]["file_id"]))
        return out

    def _resolve_file(file_id: int):
        s = holder.get("session")
        if not s:
            return None
        rows = db.storages_of_file(file_id)
        for r in rows:
            if r["tree"] == "archive":
                try:
                    p = paths.safe_join(cfg.backup_root_path, r["rel_path"])
                except ValueError:
                    continue
                if p.is_file():
                    return p
        return None

    def _make_zip(ids: list[int]):
        s = holder.get("session")
        if not s:
            return None
        rm = restore_mod.RestoreManager(cfg, db)
        return rm.make_zip(ids)

    def _restore(paths_list: list[str]) -> list[dict]:
        s = holder.get("session")
        src = holder.get("source")
        if not s or not src:
            raise EnvError("E101", "当前没有连接设备")
        ids = _resolve_ids(paths_list)
        rm = restore_mod.RestoreManager(cfg, db)
        results = rm.push_files(src, ids)
        rm.refresh_media(src, android_version=s.device.android_version)
        return [{"ok": r.ok, "remote_path": r.remote_path, "detail": r.detail} for r in results]

    def _refresh_media():
        s = holder.get("session")
        src = holder.get("source")
        if not s or not src:
            return False, "当前没有连接设备"
        rm = restore_mod.RestoreManager(cfg, db)
        return rm.refresh_media(src, android_version=s.device.android_version)

    ctx = WebContext(
        tracker=tracker,
        get_state=state.to_dict,
        get_cleanup=_get_cleanup,
        export_cleanup=_export_cleanup,
        resolve_ids=_resolve_ids,
        resolve_file=_resolve_file,
        make_zip=_make_zip,
        restore_files=_restore,
        refresh_media=_refresh_media,
        can_restore=lambda: holder.get("source") is not None,
        token=token,
        version=__version__,
    )
    web = WebServer(ctx, host="0.0.0.0", port=port)
    started = web.start()
    if started:
        terminal.ok(f"本地管理页已启动：{web.url}")
        if cfg.auto_open_browser:
            _open_browser(web.url)
    else:
        terminal.warn("本地管理页启动失败（不影响备份），可继续使用终端界面。")

    tray = None
    if cfg.tray_icon:
        tray = TrayIcon(
            on_open_page=lambda: _open_browser(web.url),
            on_open_backup=lambda: _open_explorer(cfg.backup_root_path),
            on_open_reports=lambda: _open_explorer(cfg.report_dir),
            on_quit=lambda: os._exit(0),
        )
        tray.start()
    return web, tray


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log.debug("打开浏览器失败：%s", exc)


def _open_explorer(path) -> None:
    try:
        paths.ensure_dir(path)
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.debug("打开目录失败：%s", exc)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
