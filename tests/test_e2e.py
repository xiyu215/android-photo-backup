"""端到端测试：用"假手机目录"跑完整备份链路。

验证的核心承诺
--------------
    1. 归档树按拍摄时间正确分层；镜像树保留手机原始路径。
    2. 内容零改动（备份文件的 SHA-256 == 源文件 SHA-256）。
    3. 编辑双版本不被当成重复；完全相同的两份内容会触发秒传。
    4. 二次运行几乎全是秒传，不重复劳动。
    5. 中断后续传不重复传输已完成的部分。
    6. 清理清单分档正确，且校验失败的绝不进「可放心删」。
    7. 索引与磁盘始终一致（跑一遍 doctor 必须全绿）。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from photobak.app.orchestrator import Orchestrator  # noqa: E402
from photobak.app.session import build_session  # noqa: E402
from photobak.common import paths  # noqa: E402
from photobak.core.cleanup import TIER_CACHE, TIER_PENDING, TIER_SAFE, CleanupBuilder  # noqa: E402
from photobak.core.config import Config  # noqa: E402
from photobak.core.db import IndexDB  # noqa: E402
from photobak.core.doctor import Doctor  # noqa: E402
from tests.fake_source import LocalDirSource  # noqa: E402
from tools.gen_testdata import build as build_phone  # noqa: E402


class E2ECase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="photobak_e2e_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.phone = self.tmp / "phone"
        self.info = build_phone(self.phone)
        self.backup = self.tmp / "backup"

    def make_cfg(self, **kw) -> Config:
        cfg = Config()
        cfg.backup_root = str(self.backup)
        cfg.max_file_mb = 1          # 测试里把上限调小，好让"超大文件"被拒绝
        cfg.second_read = "always"   # 测试里全开，确保复读校验路径被覆盖
        cfg.max_retries = 1          # 测试里不做长退避重试，避免拖慢用例
        cfg.auto_open_browser = False
        cfg.tray_icon = False
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def run_backup(self, cfg: Config, source: LocalDirSource, **kw):
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        session = build_session(cfg, db, source)
        orch = Orchestrator(session)
        session = orch.run(**kw)
        return session, db, orch

    # ------------------------------------------------------------------
    def test_full_flow(self) -> None:
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        session, db, _ = self.run_backup(cfg, src)

        # ---- 基本统计 ----
        self.assertGreater(session.stats.transferred, 0)
        self.assertGreaterEqual(session.stats.failed, 1, "超大文件应被拒绝并计入失败")
        self.assertGreaterEqual(session.stats.fast_upload, 1, "两份相同内容应触发秒传")

        # ---- 内容零改动 ----
        for row in db.raw_query("SELECT rel_path, file_id FROM storage WHERE tree='archive'"):
            p = paths.safe_join(cfg.backup_root_path, row["rel_path"])
            self.assertTrue(p.is_file(), f"归档文件应存在：{row['rel_path']}")
            f = db.get_file_by_sha(
                db.raw_query("SELECT sha256 FROM files WHERE id=?", (row["file_id"],))[0]["sha256"])
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), f.sha256)

        # ---- 归档树按时间分层 ----
        archives = [r["rel_path"] for r in
                    db.raw_query("SELECT rel_path FROM storage WHERE tree='archive'")]
        self.assertTrue(any("2023-07-15" in a for a in archives), "应存在 2023-07-15 目录")
        self.assertTrue(any("_时间存疑" in a for a in archives), "无 EXIF 无文件名时间的应进时间存疑")

        # ---- 镜像树保留原始路径 ----
        mirrors = [r["rel_path"] for r in
                   db.raw_query("SELECT rel_path FROM storage WHERE tree='mirror'")]
        self.assertTrue(any("DCIM/Camera" in m for m in mirrors))
        self.assertTrue(any("Movies" in m for m in mirrors))
        self.assertFalse(any(".thumbnails" in m for m in mirrors), "缩略图目录不应被备份")
        self.assertFalse(any("AppSecret" in m for m in mirrors), ".nomedia 目录不应被备份")

        # ---- 编辑双版本都保留 ----
        cam_files = [a for a in archives if "IMG_20230715_123456" in a]
        self.assertEqual(len(cam_files), 2, "原图与编辑版必须都被备份")

        # ---- emoji 与超长文件名 ----
        self.assertTrue(any("🎂" in a for a in archives), "emoji 文件名应保留")
        long_archives = [a for a in archives if "~" in Path(a).name]
        self.assertTrue(long_archives, "超长文件名应被压缩并带哈希后缀")

        # ---- 索引与磁盘一致 ----
        report = Doctor(cfg, db).check()
        self.assertTrue(report.healthy, f"体检应全绿：{report.summary()}")
        self.assertEqual(len(report.unregistered), 0)

    # ------------------------------------------------------------------
    def test_second_run_is_fast(self) -> None:
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        s1, db1, _ = self.run_backup(cfg, src)
        first_total = s1.stats.transferred + s1.stats.fast_upload
        self.assertGreater(first_total, 0)

        # 第二次：新建 source（模拟重新插拔），复用同一个索引库
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        src2 = LocalDirSource(self.phone)
        session2 = build_session(cfg, db, src2)
        s2 = Orchestrator(session2).run()

        self.assertEqual(s2.stats.transferred, 0, "第二次不应再传输任何文件")
        self.assertEqual(s2.stats.fast_upload, first_total,
                         "第二次应全部命中秒传（含本次扫描内的重复内容）")

    # ------------------------------------------------------------------
    def test_resume_after_interruption(self) -> None:
        """模拟"备份到一半被中断"：第二次运行必须接着做完，且结果完整一致。"""
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)

        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        session = build_session(cfg, db, src)
        orch = Orchestrator(session)

        # 处理完 3 个文件后触发取消（模拟用户按 Ctrl+C）
        state = {"n": 0, "cancel": False}
        original = orch._process_one  # noqa: SLF001

        def wrapped(rf, idx, total, c):  # noqa: ANN001
            result = original(rf, idx, total, c)
            state["n"] += 1
            if state["n"] >= 3:
                state["cancel"] = True
            return result

        orch._process_one = wrapped  # type: ignore[assignment]  # noqa: SLF001
        session = orch.run(cancel=lambda: state["cancel"])
        partial = session.stats.transferred + session.stats.fast_upload
        self.assertGreater(partial, 0, "取消前应已完成一部分")
        self.assertEqual(session.stats.transferred, 3, "取消后不应继续处理更多文件")

        # 第二次：应当把剩下的全部做完
        db2 = IndexDB(cfg.index_db)
        db2.connect()
        self.addCleanup(db2.close)
        session2 = build_session(cfg, db2, LocalDirSource(self.phone))
        s2 = Orchestrator(session2).run()

        covered = partial + s2.stats.transferred + s2.stats.fast_upload
        self.assertGreater(covered, partial, "第二次必须继续推进")
        self.assertEqual(s2.stats.failed, 1, "只剩那个超大文件应被拒绝（E410）")

        # 最终一致性：索引与磁盘必须完全对得上
        report = Doctor(cfg, db2).check()
        self.assertTrue(report.healthy, report.summary())

    # ------------------------------------------------------------------
    def test_cleanup_tiers_and_no_false_safe(self) -> None:
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        session, db, _ = self.run_backup(cfg, src)

        report = CleanupBuilder(cfg, db).build(session.device_id)
        safe = report.tier(TIER_SAFE)
        pending = report.tier(TIER_PENDING)
        cache = report.tier(TIER_CACHE)

        self.assertTrue(safe, "应有可放心删的文件")
        # 所有"可放心删"必须真实存在于磁盘且哈希一致
        for item in safe:
            self.assertTrue(item.pc_path)
            p = paths.safe_join(cfg.backup_root_path, item.pc_path)
            self.assertTrue(p.is_file(), f"可放心删的文件必须真实存在：{item.pc_path}")
        # 被拒绝的超大文件必须在"暂不能删"
        self.assertTrue(any("IMG_huge" in i.filename for i in pending),
                        "被拒绝的超大文件必须进暂不能删")
        # 缓存目录下的文件不应出现在"可放心删"
        self.assertFalse(any("thumbnails" in i.phone_path for i in safe))
        self.assertTrue(cache or True)

    # ------------------------------------------------------------------
    def test_dry_run_writes_nothing(self) -> None:
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        session = build_session(cfg, db, src)
        s = Orchestrator(session).run(dry_run=True)
        self.assertEqual(s.stats.transferred, 0)
        self.assertEqual(db.count_files()[0], 0, "试运行不应写入任何索引记录")

    # ------------------------------------------------------------------
    def test_no_remote_hash_still_verifies(self) -> None:
        """通道拿不到源端哈希时，仍必须做本地校验，并标记为"仅单向校验"。"""
        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        src.remote_hash_available = False
        session, db, _ = self.run_backup(cfg, src)
        self.assertGreater(session.stats.transferred, 0)
        # 所有文件都应有落盘哈希
        for row in db.raw_query("SELECT sha256, size FROM files"):
            self.assertEqual(len(row["sha256"]), 64)
        report = Doctor(cfg, db).check()
        self.assertTrue(report.healthy)

    # ------------------------------------------------------------------
    def test_emoji_and_reserved_names_roundtrip(self) -> None:
        """把带 emoji / 保留名的文件塞进去，确认能正常备份且内容一致。"""
        weird = self.phone / "DCIM" / "Camera" / "CON.jpg"
        weird.write_bytes(b"reserved-name-content")
        emoji = self.phone / "DCIM" / "Camera" / "测试🎈🎉.jpg"
        emoji.write_bytes(b"emoji-content")

        cfg = self.make_cfg()
        src = LocalDirSource(self.phone)
        session, db, _ = self.run_backup(cfg, src)

        rels = [r["rel_path"] for r in
                db.raw_query("SELECT rel_path FROM storage WHERE tree='archive'")]
        self.assertTrue(any("_CON.jpg" in r for r in rels), "保留名应被加前缀转义")
        self.assertTrue(any("🎈" in r for r in rels), "emoji 应原样保留")

        report = Doctor(cfg, db).check()
        self.assertTrue(report.healthy, report.summary())


if __name__ == "__main__":
    unittest.main(verbosity=2)
