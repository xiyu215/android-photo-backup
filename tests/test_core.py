"""核心单元测试：哈希 / 时间 / 命名 / 扫描 / 索引库 / 归档 / 校验 / 清单。

运行： python tests/run_all.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from photobak.common import paths  # noqa: E402
from photobak.core import hashutil, naming, scanner, times  # noqa: E402
from photobak.core.archive import ArchiveManager  # noqa: E402
from photobak.core.cleanup import (TIER_CACHE, TIER_PENDING, TIER_SAFE,  # noqa: E402
                                   CleanupBuilder)
from photobak.core.config import Config  # noqa: E402
from photobak.core.db import IndexDB  # noqa: E402
from photobak.core.dedup import Action, Deduper  # noqa: E402
from photobak.core.scanner import RemoteFile  # noqa: E402
from photobak.core.verify import check_landed  # noqa: E402
from tools.gen_testdata import make_exif_bytes, write_jpeg, write_video  # noqa: E402


class TempCase(unittest.TestCase):
    """带临时目录的测试基类。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="photobak_test_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def make_cfg(self, **kw) -> Config:
        cfg = Config()
        cfg.backup_root = str(self.tmp / "backup")
        paths.ensure_dir(cfg.backup_root)
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg


# ---------------------------------------------------------------------------
class TestHash(TempCase):
    def test_chunked_equals_whole(self) -> None:
        p = self.tmp / "a.bin"
        p.write_bytes(os.urandom(3 * 1024 * 1024 + 12345))
        got = hashutil.hash_file(p, chunk_size=64 * 1024).sha256
        expect = hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertEqual(got, expect, "分块哈希必须与整体哈希完全一致")

    def test_progress_callback(self) -> None:
        p = self.tmp / "b.bin"
        p.write_bytes(b"x" * (5 * 1024 * 1024))
        seen: list[tuple[int, int]] = []
        hashutil.hash_file(p, chunk_size=1024 * 1024, progress=lambda d, t: seen.append((d, t)))
        self.assertGreaterEqual(len(seen), 5)
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_empty_file(self) -> None:
        p = self.tmp / "empty.bin"
        p.write_bytes(b"")
        self.assertEqual(hashutil.hash_file(p).sha256,
                         hashlib.sha256(b"").hexdigest())

    def test_normalize_remote_output(self) -> None:
        sha = "a" * 64
        self.assertEqual(hashutil.normalize_sha256(f"{sha}  /sdcard/a.jpg"), sha)
        self.assertEqual(hashutil.normalize_sha256(sha.upper()), sha)
        self.assertIsNone(hashutil.normalize_sha256("not-a-hash"))
        self.assertIsNone(hashutil.normalize_sha256(""))
        self.assertIsNone(hashutil.normalize_sha256(None))


class TestTimes(TempCase):
    def test_filename_patterns(self) -> None:
        cases = {
            "IMG_20230715_123456.jpg": datetime(2023, 7, 15, 12, 34, 56),
            "VID_20230715_123456.mp4": datetime(2023, 7, 15, 12, 34, 56),
            "Screenshot_2023-07-15-12-34-56.png": datetime(2023, 7, 15, 12, 34, 56),
            "2023-07-15 12.34.56.jpg": datetime(2023, 7, 15, 12, 34, 56),
            "20230715_123456.jpg": datetime(2023, 7, 15, 12, 34, 56),
            "2023-07-15.jpg": datetime(2023, 7, 15, 0, 0, 0),
        }
        for name, expect in cases.items():
            with self.subTest(name=name):
                self.assertEqual(times.from_filename(name), expect)

    def test_wechat_mmexport(self) -> None:
        # mmexport1690848000000 → 2023-08-01 08:00:00（UTC+8 本地时间）
        got = times.from_filename("mmexport1690848000000.jpg")
        self.assertIsNotNone(got)
        self.assertEqual(got.year, 2023)

    def test_invalid_dates_rejected(self) -> None:
        self.assertIsNone(times.from_filename("IMG_20231345_996666.jpg"))
        self.assertIsNone(times.from_filename("nothing.jpg"))

    def test_exif_reading(self) -> None:
        p = self.tmp / "exif.jpg"
        write_jpeg(p, exif_datetime="2023:07:15 12:34:56")
        info = times.resolve_final(p, kind="image", name=p.name)
        self.assertEqual(info.source, times.SRC_EXIF)
        self.assertEqual(info.confidence, 3)
        self.assertEqual(info.taken_at, datetime(2023, 7, 15, 12, 34, 56))
        self.assertFalse(info.is_uncertain)

    def test_video_mvhd_reading(self) -> None:
        p = self.tmp / "v.mp4"
        write_video(p, when=(2023, 9, 4, 1, 0))
        info = times.resolve_final(p, kind="video", name=p.name)
        self.assertEqual(info.source, times.SRC_VIDEO)
        self.assertIsNotNone(info.taken_at)
        self.assertEqual(info.taken_at.year, 2023)
        self.assertEqual(info.taken_at.month, 9)

    def test_mtime_fallback_is_uncertain(self) -> None:
        p = self.tmp / "random.jpg"
        p.write_bytes(b"no exif here")
        info = times.resolve_final(p, kind="image", name=p.name, mtime=1690000000)
        self.assertEqual(info.source, times.SRC_MTIME)
        self.assertTrue(info.is_uncertain)

    def test_archive_subdir(self) -> None:
        known = times.TimeInfo(datetime(2023, 7, 15, 1, 2, 3), times.SRC_EXIF, 3)
        self.assertEqual(times.archive_subdir(known, "_时间存疑", "_未知时间"),
                         ["2023", "2023-07", "2023-07-15"])
        unsure = times.TimeInfo(datetime(2023, 7, 15), times.SRC_MTIME, 1)
        self.assertEqual(times.archive_subdir(unsure, "_时间存疑", "_未知时间"),
                         ["_时间存疑", "2023", "2023-07"])
        self.assertEqual(times.archive_subdir(times.UNKNOWN, "_时间存疑", "_未知时间"),
                         ["_未知时间"])


class TestNaming(TempCase):
    def test_illegal_chars(self) -> None:
        self.assertEqual(naming.sanitize_component('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_reserved_names(self) -> None:
        self.assertTrue(naming.sanitize_component("CON.jpg").startswith("_"))
        self.assertTrue(naming.sanitize_component("nul").startswith("_"))
        self.assertEqual(naming.sanitize_component("console.jpg"), "console.jpg")

    def test_trailing_dots_and_spaces(self) -> None:
        self.assertEqual(naming.sanitize_component("name... "), "name")
        self.assertEqual(naming.sanitize_component("  .hidden"), "hidden")

    def test_emoji_preserved(self) -> None:
        self.assertEqual(naming.sanitize_component("生日🎂.jpg"), "生日🎂.jpg")

    def test_long_path_compression(self) -> None:
        name = ("很长的名字" * 60) + ".jpg"     # 约 300 字符，必然超限
        res = naming.build_rel_path(
            ["照片归档", "2023", "2023-07", "2023-07-15"], name,
            hash_hex="a" * 64, root_abs="D:/备份",
        )
        full = len(str(Path("D:/备份").resolve())) + 1 + len(res.rel_path)
        self.assertLessEqual(full, paths.WIN_PATH_LIMIT)
        self.assertTrue(res.stored_name.endswith(".jpg"))
        self.assertIn("~", res.stored_name)
        self.assertTrue(res.notes)

    def test_unique_rel_path(self) -> None:
        root = self.tmp
        (root / "a.jpg").write_bytes(b"one")
        # 内容不同 → 追加 _1
        rel, reused = naming.unique_rel_path(root, "a.jpg", same_content=lambda p: False)
        self.assertEqual(rel, "a_1.jpg")
        self.assertFalse(reused)
        # 内容相同 → 复用
        rel2, reused2 = naming.unique_rel_path(root, "a.jpg", same_content=lambda p: True)
        self.assertEqual(rel2, "a.jpg")
        self.assertTrue(reused2)

    def test_device_dir_name(self) -> None:
        self.assertEqual(naming.device_dir_name("红米K90", "AB:CD:EF:12"), "红米K90_EF12")


class TestScanner(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.make_cfg()

    def _rf(self, rel: str, size: int = 1000, name: str | None = None) -> RemoteFile:
        return RemoteFile(path="/sdcard/" + rel, rel=rel, size=size,
                          mtime=1690000000.0) if name is None else \
            RemoteFile(path="/sdcard/" + rel, rel=rel, size=size, mtime=1690000000.0)

    def test_cache_and_thumbnails_excluded(self) -> None:
        sc = scanner.Scanner(self.cfg)
        for rel in ("DCIM/.thumbnails/t.jpg", "Pictures/cache/c.jpg",
                    "DCIM/Camera/ok.jpg"):
            ok, _ = sc.accept_file(self._rf(rel))
            if "ok.jpg" in rel:
                self.assertTrue(ok)
            else:
                self.assertFalse(ok, f"{rel} 应被排除")

    def test_nomedia_directory_excluded(self) -> None:
        sc = scanner.Scanner(self.cfg)
        files = [
            RemoteFile("/sdcard/Pictures/App/.nomedia", "Pictures/App/.nomedia", 0),
            RemoteFile("/sdcard/Pictures/App/x.jpg", "Pictures/App/x.jpg", 100),
        ]
        out = sc.filter(files)
        self.assertEqual([f.rel for f in out], [], ".nomedia 目录下的一切都应被排除")

    def test_nomedia_ignored_when_configured(self) -> None:
        cfg = self.make_cfg(ignore_nomedia=True)
        sc = scanner.Scanner(cfg)
        files = [
            RemoteFile("/sdcard/Pictures/App/.nomedia", "Pictures/App/.nomedia", 0),
            RemoteFile("/sdcard/Pictures/App/x.jpg", "Pictures/App/x.jpg", 100),
        ]
        out = sc.filter(files)
        self.assertEqual([f.rel for f in out], ["Pictures/App/x.jpg"])

    def test_ext_whitelist_and_junk(self) -> None:
        sc = scanner.Scanner(self.cfg)
        self.assertFalse(sc.accept_file(self._rf("DCIM/Camera/a.txt"))[0])
        self.assertFalse(sc.accept_file(self._rf("DCIM/Camera/a.tmp"))[0])
        self.assertTrue(sc.accept_file(self._rf("DCIM/Camera/a.mp4"))[0])
        self.assertTrue(sc.accept_file(self._rf("DCIM/Camera/a.HEIC"))[0])

    def test_zero_byte_skipped(self) -> None:
        sc = scanner.Scanner(self.cfg)
        self.assertFalse(sc.accept_file(self._rf("DCIM/Camera/a.jpg", size=0))[0])

    def test_max_file_size_rejected(self) -> None:
        cfg = self.make_cfg(max_file_mb=1)
        sc = scanner.Scanner(cfg)
        ok, why = sc.accept_file(self._rf("DCIM/Camera/big.jpg", size=2 * 1024 * 1024))
        self.assertFalse(ok)
        self.assertIn("单文件上限", why)

    def test_out_of_scope_rejected(self) -> None:
        sc = scanner.Scanner(self.cfg)
        self.assertFalse(sc.accept_file(self._rf("Documents/a.jpg"))[0])
        self.assertFalse(sc.accept_file(self._rf("Android/data/com.x/a.jpg"))[0])

    def test_include_dirs_supports_subpath(self) -> None:
        """``包含目录`` 必须支持子路径 —— 这是配置注释里明确写的用法。

        回归背景（真实踩过的坑）
        ------------------------
        早期实现只拿路径**第一段**去和 ``包含目录`` 做全等比较，于是
        ``包含目录=DCIM/Screenshots`` 这种写法一条都匹配不上，程序还会
        安静地报"纳入备份 0 个"——用户的备份就这么变成了空的。
        已用真实设备复现（扫描到 1 个文件、过滤后剩 0 个、无任何报错）。
        """
        cfg = self.make_cfg(include_dirs=["DCIM/Screenshots"])
        sc = scanner.Scanner(cfg)
        self.assertTrue(sc.accept_file(self._rf("DCIM/Screenshots/a.jpg"))[0],
                        "子目录写法必须能匹配到")
        self.assertFalse(sc.accept_file(self._rf("DCIM/Camera/a.jpg"))[0],
                         "未列入的兄弟目录不应被包含")

    def test_include_dirs_supports_single_file(self) -> None:
        """``包含目录`` 指向单个文件时，只应匹配这一个文件。"""
        cfg = self.make_cfg(include_dirs=["DCIM/Camera/IMG_0001.jpg"])
        sc = scanner.Scanner(cfg)
        self.assertTrue(sc.accept_file(self._rf("DCIM/Camera/IMG_0001.jpg"))[0])
        self.assertFalse(sc.accept_file(self._rf("DCIM/Camera/IMG_0002.jpg"))[0])
        self.assertFalse(sc.accept_file(self._rf("DCIM/Screenshots/a.jpg"))[0])

    def test_include_dirs_prefix_not_confused_by_similar_name(self) -> None:
        """前缀匹配必须在路径分段处成立，不能把 DCIMX 误判成 DCIM 下。"""
        cfg = self.make_cfg(include_dirs=["DCIM"])
        sc = scanner.Scanner(cfg)
        self.assertTrue(sc.accept_file(self._rf("DCIM/Camera/a.jpg"))[0])
        self.assertFalse(sc.accept_file(self._rf("DCIMX/a.jpg"))[0],
                         "DCIMX 不是 DCIM 的子路径")
        self.assertFalse(sc.accept_file(self._rf("Documents/DCIM/a.jpg"))[0])

    def test_default_include_dirs_unchanged(self) -> None:
        """默认配置的行为必须与改动前完全一致。"""
        sc = scanner.Scanner(self.cfg)
        for rel in ("DCIM/Camera/a.jpg", "Pictures/WeiXin/a.jpg",
                    "Movies/a.mp4", "Download/a.jpg"):
            self.assertTrue(sc.accept_file(self._rf(rel))[0], f"{rel} 应被包含")
        for rel in ("Documents/a.jpg", "Android/data/x/a.jpg", "Music/a.jpg"):
            self.assertFalse(sc.accept_file(self._rf(rel))[0], f"{rel} 应被排除")

    def test_app_tag_classification(self) -> None:
        sc = scanner.Scanner(self.cfg)
        self.assertEqual(sc.app_tag_of("DCIM/Camera/IMG.jpg"), "Camera")
        self.assertEqual(sc.app_tag_of("Pictures/WeiXin/a.jpg"), "WeiXin")
        self.assertEqual(sc.app_tag_of("Tencent/QQ_Images/a.jpg"), "QQ")
        self.assertEqual(sc.app_tag_of("Pictures/Screenshots/a.png"), "Screenshot")
        self.assertEqual(sc.app_tag_of("DCIM/Camera/unknown_dir/a.jpg"), "Camera")

    def test_edit_variant_not_deduped_by_name(self) -> None:
        self.assertTrue(scanner.is_edit_variant("IMG_1_edit.jpg"))
        self.assertFalse(scanner.is_edit_variant("IMG_1.jpg"))


class TestDB(TempCase):
    def test_schema_and_crud(self) -> None:
        db = IndexDB(self.tmp / "index.db")
        db.connect()
        self.addCleanup(db.close)

        ok, msg = db.integrity_check()
        self.assertTrue(ok, msg)

        did = db.upsert_device("S1", "型号", "14(API 34)", "型号")
        fid = db.upsert_file(
            sha256="b" * 64, size=123, kind="image", ext="jpg",
            taken_at="2023-07-15 12:34:56", taken_at_source="exif",
            time_confidence=3, is_time_uncertain=False, verified=True,
            second_read_ok=True,
        )
        db.upsert_instance(file_id=fid, device_id=did, source_path="/sdcard/a.jpg",
                           source_mtime="1", app_tag="Camera", root_tag="DCIM")
        db.add_storage(file_id=fid, rel_path="照片归档/2023/a.jpg", tree="archive",
                       link_type="file")

        self.assertIsNotNone(db.get_file_by_sha("b" * 64))
        self.assertEqual(db.count_files(), (1, 1))
        self.assertEqual(len(db.storages_of_file(fid)), 1)

        # 重复插入同一哈希应更新而非新增
        fid2 = db.upsert_file(
            sha256="b" * 64, size=123, kind="image", ext="jpg",
            taken_at="2023-07-15 12:34:56", taken_at_source="exif",
            time_confidence=3, is_time_uncertain=False, verified=True,
        )
        self.assertEqual(fid, fid2)
        self.assertEqual(db.count_files()[0], 1)

    def test_mark_absent(self) -> None:
        db = IndexDB(self.tmp / "index.db")
        db.connect()
        self.addCleanup(db.close)
        did = db.upsert_device("S1", "m", "14", "m")
        fid = db.upsert_file(sha256="c" * 64, size=1, kind="image", ext="jpg",
                             taken_at=None, taken_at_source=None, time_confidence=0,
                             is_time_uncertain=True, verified=True)
        db.upsert_instance(file_id=fid, device_id=did, source_path="/a.jpg",
                           source_mtime=None, app_tag="Other", root_tag="Other")
        db.upsert_instance(file_id=fid, device_id=did, source_path="/b.jpg",
                           source_mtime=None, app_tag="Other", root_tag="Other")
        absent = db.mark_absent(did, ["/a.jpg"])
        self.assertEqual(absent, 1)
        rows = db.raw_query("SELECT source_path, is_present FROM instances ORDER BY source_path")
        self.assertEqual([(r["source_path"], r["is_present"]) for r in rows],
                         [("/a.jpg", 1), ("/b.jpg", 0)])

    def test_transfer_job_resume_key(self) -> None:
        db = IndexDB(self.tmp / "index.db")
        db.connect()
        self.addCleanup(db.close)
        did = db.upsert_device("S1", "m", "14", "m")
        j1 = db.upsert_job(run_id=0, source_path="/a.jpg", device_id=did,
                           expected_size=100, expected_sha256=None, part_path="1.part")
        db.update_job(j1, bytes_done=50, status="paused")
        j2 = db.upsert_job(run_id=0, source_path="/a.jpg", device_id=did,
                           expected_size=100, expected_sha256=None, part_path="1.part")
        self.assertEqual(j1, j2, "同一 (设备,路径,大小) 必须命中同一条任务以支持续传")
        self.assertEqual(db.get_job(j1).bytes_done, 50)


class TestVerify(TempCase):
    def test_ok_and_mismatch(self) -> None:
        p = self.tmp / "f.bin"
        p.write_bytes(b"hello world")
        good = hashlib.sha256(b"hello world").hexdigest()

        r = check_landed(p, expected_sha=good, expected_size=11)
        self.assertTrue(r.ok)
        self.assertTrue(r.dual_sided)

        r2 = check_landed(p, expected_sha="f" * 64, expected_size=11)
        self.assertFalse(r2.ok)
        self.assertIn("哈希不一致", r2.detail)

    def test_size_mismatch_detected_early(self) -> None:
        p = self.tmp / "f.bin"
        p.write_bytes(b"abc")
        r = check_landed(p, expected_sha=None, expected_size=999)
        self.assertFalse(r.ok)
        self.assertIn("大小", r.detail)

    def test_single_sided_check(self) -> None:
        p = self.tmp / "f.bin"
        p.write_bytes(b"abc")
        r = check_landed(p, expected_sha=None, expected_size=3)
        self.assertTrue(r.ok)
        self.assertFalse(r.dual_sided, "无源端哈希时必须标记为单向校验")

    def test_second_read(self) -> None:
        p = self.tmp / "f.bin"
        p.write_bytes(b"abc" * 1000)
        r = check_landed(p, expected_sha=hashlib.sha256(b"abc" * 1000).hexdigest(),
                         expected_size=3000, second_read=True)
        self.assertTrue(r.ok)
        self.assertTrue(r.second_read_ok)


class TestArchiveAndDedup(TempCase):
    def test_place_and_hardlink(self) -> None:
        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        am = ArchiveManager(cfg, db)

        part = cfg.tmp_dir / "1.part"
        paths.ensure_dir(part.parent)
        part.write_bytes(b"content-abc")

        sha = hashlib.sha256(b"content-abc").hexdigest()
        fid = db.upsert_file(sha256=sha, size=11, kind="image", ext="jpg",
                             taken_at="2023-07-15 12:34:56", taken_at_source="exif",
                             time_confidence=3, is_time_uncertain=False, verified=False)
        info = times.TimeInfo(datetime(2023, 7, 15, 12, 34, 56), times.SRC_EXIF, 3)
        rf = RemoteFile("/sdcard/DCIM/Camera/IMG_1.jpg", "DCIM/Camera/IMG_1.jpg",
                        11, 1690000000.0)
        res = am.place(part_path=part, file_id=fid, filename="IMG_1.jpg",
                       hash_hex=sha, info=info, rf=rf, device_dir="测试机_1234")

        arch = paths.safe_join(cfg.backup_root_path, res.archive_rel)
        self.assertTrue(arch.is_file())
        self.assertIn("2023", res.archive_rel)
        if res.mirror_rel:
            mirror = paths.safe_join(cfg.backup_root_path, res.mirror_rel)
            self.assertTrue(mirror.is_file())
            self.assertIn("手机镜像", res.mirror_rel)
            if paths.supports_hardlink(cfg.backup_root_path):
                self.assertEqual(res.link_type, "hardlink")

    def test_mirror_off(self) -> None:
        cfg = self.make_cfg(mirror_link_mode="off")
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        am = ArchiveManager(cfg, db)
        part = cfg.tmp_dir / "2.part"
        paths.ensure_dir(part.parent)
        part.write_bytes(b"x")
        sha = hashlib.sha256(b"x").hexdigest()
        fid = db.upsert_file(sha256=sha, size=1, kind="image", ext="jpg", taken_at=None,
                             taken_at_source=None, time_confidence=0,
                             is_time_uncertain=True, verified=False)
        rf = RemoteFile("/sdcard/DCIM/Camera/a.jpg", "DCIM/Camera/a.jpg", 1, None)
        res = am.place(part_path=part, file_id=fid, filename="a.jpg", hash_hex=sha,
                       info=times.UNKNOWN, rf=rf, device_dir="dev")
        self.assertIsNone(res.mirror_rel, "镜像树关闭时不应产生镜像条目")

    def test_dedup_only_by_sha(self) -> None:
        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        dd = Deduper(cfg, db)

        sha = "d" * 64
        self.assertEqual(dd.decide(sha, 100).action, Action.NEW)

        fid = db.upsert_file(sha256=sha, size=100, kind="image", ext="jpg", taken_at=None,
                             taken_at_source=None, time_confidence=0,
                             is_time_uncertain=True, verified=False)
        # 未验证 → 仍需传输
        self.assertEqual(dd.decide(sha, 100).action, Action.NEW)

        db.mark_verified(fid, second_read_ok=True)
        # 已验证但磁盘无文件 → 仍按新文件处理（防"索引说有、文件没有"）
        self.assertEqual(dd.decide(sha, 100).action, Action.NEW)

        # 真正落盘后才允许秒传
        p = paths.safe_join(cfg.backup_root_path, "照片归档/2023/a.jpg")
        paths.ensure_dir(p.parent)
        p.write_bytes(b"x" * 100)
        db.add_storage(file_id=fid, rel_path="照片归档/2023/a.jpg", tree="archive",
                       link_type="file")
        db.mark_verified(fid, second_read_ok=True)   # 上一步的"缺失"已把 verified 复位
        self.assertEqual(dd.decide(sha, 100).action, Action.FAST)


class TestCleanup(TempCase):
    def _seed(self, cfg: Config, db: IndexDB) -> int:
        did = db.upsert_device("S", "m", "14", "m")
        # 可放心删：已验证 + 文件存在
        fid1 = db.upsert_file(sha256="1" * 64, size=10, kind="image", ext="jpg",
                              taken_at="2023-07-15 12:34:56", taken_at_source="exif",
                              time_confidence=3, is_time_uncertain=False, verified=True,
                              second_read_ok=True)
        rel1 = "照片归档/2023/2023-07/2023-07-15/a.jpg"
        p1 = paths.safe_join(cfg.backup_root_path, rel1)
        paths.ensure_dir(p1.parent)
        p1.write_bytes(b"x" * 10)
        db.add_storage(file_id=fid1, rel_path=rel1, tree="archive", link_type="file")
        db.upsert_instance(file_id=fid1, device_id=did, source_path="/sdcard/DCIM/Camera/a.jpg",
                           source_mtime=None, app_tag="Camera", root_tag="DCIM")

        # 暂不能删：未验证
        fid2 = db.upsert_file(sha256="2" * 64, size=10, kind="image", ext="jpg",
                              taken_at=None, taken_at_source=None, time_confidence=0,
                              is_time_uncertain=True, verified=False)
        db.upsert_instance(file_id=fid2, device_id=did, source_path="/sdcard/DCIM/Camera/b.jpg",
                           source_mtime=None, app_tag="Camera", root_tag="DCIM")

        # 缓存可清
        fid3 = db.upsert_file(sha256="3" * 64, size=10, kind="image", ext="jpg",
                              taken_at=None, taken_at_source=None, time_confidence=0,
                              is_time_uncertain=True, verified=False)
        db.upsert_instance(file_id=fid3, device_id=did,
                           source_path="/sdcard/DCIM/.thumbnails/t.jpg",
                           source_mtime=None, app_tag="Other", root_tag="DCIM")

        # 时间存疑但已验证 → 默认仍进"暂不能删"
        fid4 = db.upsert_file(sha256="4" * 64, size=10, kind="image", ext="jpg",
                              taken_at="2023-07-15 00:00:00", taken_at_source="mtime",
                              time_confidence=1, is_time_uncertain=True, verified=True,
                              second_read_ok=True)
        rel4 = "照片归档/_时间存疑/2023/2023-07/c.jpg"
        p4 = paths.safe_join(cfg.backup_root_path, rel4)
        paths.ensure_dir(p4.parent)
        p4.write_bytes(b"y" * 10)
        db.add_storage(file_id=fid4, rel_path=rel4, tree="archive", link_type="file")
        db.upsert_instance(file_id=fid4, device_id=did, source_path="/sdcard/Download/c.jpg",
                           source_mtime=None, app_tag="Download", root_tag="Download")
        return did

    def test_four_tiers(self) -> None:
        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        did = self._seed(cfg, db)
        report = CleanupBuilder(cfg, db).build(did)

        self.assertEqual(report.count(TIER_SAFE), 1)
        self.assertEqual(report.count(TIER_CACHE), 1)
        self.assertGreaterEqual(report.count(TIER_PENDING), 2)
        self.assertEqual(report.total(), 4)

    def test_uncertain_can_be_allowed(self) -> None:
        cfg = self.make_cfg(uncertain_can_delete=True)
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        did = self._seed(cfg, db)
        report = CleanupBuilder(cfg, db).build(did)
        self.assertEqual(report.count(TIER_SAFE), 2, "允许时时间存疑文件应可进入可放心删")

    def test_missing_backup_file_downgrades(self) -> None:
        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        did = self._seed(cfg, db)
        # 删掉磁盘上的备份文件 → 对应手机文件必须降档
        p = paths.safe_join(cfg.backup_root_path, "照片归档/2023/2023-07/2023-07-15/a.jpg")
        p.unlink()
        report = CleanupBuilder(cfg, db).build(did)
        self.assertEqual(report.count(TIER_SAFE), 0)

    def test_export_and_delete_script(self) -> None:
        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        db.connect()
        self.addCleanup(db.close)
        did = self._seed(cfg, db)
        cb = CleanupBuilder(cfg, db)
        report = cb.build(did)

        csvs = cb.export_csv(report)
        self.assertTrue(csvs)
        for c in csvs:
            self.assertTrue(c.is_file())
            self.assertTrue(c.read_bytes().startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM")

        txts = cb.export_txt(report)
        self.assertTrue(all(t.is_file() for t in txts))

        script = cb.export_delete_script(report, adb_exe="C:/x/adb.exe", serial="S1")
        self.assertIsNotNone(script)
        content = script.read_text(encoding="utf-8-sig")
        self.assertIn("pause", content.lower())
        self.assertIn("警告", content)
        self.assertIn("rm -f", content)
        self.assertIn("adb.exe", content)


class TestTerminalColor(TempCase):
    """终端配色开关：关闭颜色时**任何**输出都不得残留 ANSI 转义序列。

    回归背景（真实踩过的坑）
    ------------------------
    ``logger.rule()`` 早期直接拼接 ``C.GREY`` / ``C.BOLD`` 等常量，绕过了
    ``_use_color`` 检查。结果是：只要把输出重定向到文件
    （``安卓相册备份.exe --usb --dry-run > 日志.txt``），文件里就会混进
    ``←[90m`` 之类的乱码（4 个分节标题 = 24 个转义序列），而模块文档一直
    承诺"重定向时自动降级为纯文本"。本测试把这条约定钉死。
    """

    ESC = "\033"

    def setUp(self) -> None:
        super().setUp()
        from photobak.common import logger as lg

        self.lg = lg
        self._saved = lg._use_color          # noqa: SLF001
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.lg._use_color = self._saved      # noqa: SLF001

    def test_rule_is_plain_when_color_off(self) -> None:
        self.lg._use_color = False            # noqa: SLF001
        for text in ("", "试运行结果", "本次配置"):
            got = self.lg.rule(text)
            self.assertNotIn(self.ESC, got, f"关闭颜色后 rule({text!r}) 仍含转义序列")
            self.assertIn("─", got)

    def test_rule_colored_when_on(self) -> None:
        self.lg._use_color = True             # noqa: SLF001
        self.assertIn(self.ESC, self.lg.rule("试运行结果"), "开启颜色时应有转义序列")

    def test_banner_never_has_ansi(self) -> None:
        """标题框只用制表符，任何情况下都不该带颜色（要能直接写进日志）。"""
        for flag in (True, False):
            self.lg._use_color = flag         # noqa: SLF001
            self.assertNotIn(self.ESC, self.lg.banner("标题", "副标题"))

    def test_terminal_helpers_respect_switch(self) -> None:
        from photobak.common.logger import C
        from photobak.ui import terminal

        self.lg._use_color = False            # noqa: SLF001
        self.assertEqual(terminal._color("x", C.RED), "x")   # noqa: SLF001
        self.assertFalse(terminal._supports_ansi())          # noqa: SLF001

        self.lg._use_color = True             # noqa: SLF001
        self.assertIn(self.ESC, terminal._color("x", C.RED))  # noqa: SLF001

    def test_c_helper_matches_switch(self) -> None:
        from photobak.common.logger import C

        self.lg._use_color = False            # noqa: SLF001
        self.assertEqual(self.lg._c(C.GREY), "")
        self.lg._use_color = True             # noqa: SLF001
        self.assertEqual(self.lg._c(C.GREY), C.GREY)


class TestQrRender(TempCase):
    """终端二维码：字符块渲染必须**无损**，否则手机扫不出来。

    为什么值得测
    ------------
    二维码是"手机打开查看页"的唯一省事入口（地址里带 32 位令牌，手敲不现实）。
    用半角块（▀▄█）把两个模块行压进一行文本时，只要映射写错一格，整个码就废了，
    而且**人眼看不出来** —— 必须靠往返比对来保证。
    """

    URL = "http://192.168.1.7:8765/?t=AbCdEf1234567890AbCdEf1234567890"

    def test_roundtrip_is_lossless(self) -> None:
        from photobak.ui import qrterm

        orig = qrterm.build_matrix(self.URL, border=2)
        lines = qrterm.render(self.URL, ansi=False, invert=False)

        decoded: list[list[bool]] = []
        for line in lines:
            top, bot = [], []
            for ch in line:
                top.append(ch in ("█", "▀"))
                bot.append(ch in ("█", "▄"))
            decoded.append(top)
            decoded.append(bot)
        decoded = decoded[:len(orig)]

        self.assertEqual(len(decoded), len(orig), "行数应与矩阵一致")
        self.assertEqual(len(decoded[0]), len(orig[0]), "列数应与矩阵一致")
        diff = sum(
            1
            for r in range(len(orig))
            for c in range(len(orig[0]))
            if bool(orig[r][c]) != bool(decoded[r][c])
        )
        self.assertEqual(diff, 0, f"渲染丢失了 {diff} 个模块，二维码将无法识别")

    def test_size_is_reasonable(self) -> None:
        """终端二维码不能宽到折行 —— 折行后同样扫不出来。"""
        from photobak.ui import qrterm

        rows, cols = qrterm.matrix_size(self.URL)
        self.assertLessEqual(cols + 2, 80, "二维码宽度不应超过常见终端宽度")
        self.assertEqual(rows % 2, 1, "矩阵行数应为奇数，渲染成文本后才是整数行")

    def test_color_prefix_only_when_enabled(self) -> None:
        from photobak.ui import qrterm

        colored = qrterm.render_for_terminal(self.URL, color_enabled=True)
        plain = qrterm.render_for_terminal(self.URL, color_enabled=False)
        self.assertIn("\033", colored, "开启颜色时应带 ANSI 白底")
        self.assertNotIn("\033", plain, "关闭颜色时不得残留 ANSI（会污染重定向的日志）")


class TestAutoConfirm(TempCase):
    """``--yes`` 必须**真的**跳过所有交互提问。

    回归背景（真实踩过的坑）
    ------------------------
    ``--yes`` 的说明是"跳过所有确认（用于自动化）"，但早期版本只把 ``args.yes``
    传给了编排器，终端的 ``input()`` 照样会问。实测：备份本身 8 秒就跑完了，
    进程却永久卡在"是否生成「删除脚本」"这一问上（后台运行时 stdin 不给数据），
    只能手动杀进程 —— 自动化场景下这是致命的。
    """

    def setUp(self) -> None:
        super().setUp()
        from photobak.ui import terminal

        self.terminal = terminal
        self.addCleanup(terminal.set_auto_confirm, terminal.is_auto_confirm())

    def test_no_stdin_read_when_auto_confirm(self) -> None:
        import unittest.mock as mock

        self.terminal.set_auto_confirm(True)
        with mock.patch("builtins.input", side_effect=AssertionError("自动确认时不该读 stdin")):
            self.assertTrue(self.terminal.confirm("开始备份？", default=True))
            self.assertFalse(self.terminal.confirm("生成删除脚本？", default=False))
            self.assertEqual(self.terminal.ask("名字？", default="默认值"), "默认值")
            self.terminal.pause("按回车退出…")   # 必须立即返回，不能阻塞

    def test_still_asks_when_not_auto_confirm(self) -> None:
        import unittest.mock as mock

        self.terminal.set_auto_confirm(False)
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(self.terminal.confirm("继续吗？", default=False),
                            "未开自动确认时，用户的 y 应该被采纳")
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(self.terminal.confirm("继续吗？", default=True),
                             "未开自动确认时，用户的 n 应该被采纳")


class TestReportExport(TempCase):
    """备份跑完后必须真的产出可清理清单（CSV/TXT）。

    回归背景（真实踩过的坑）
    ------------------------
    ``main.do_export()`` 曾调用根本不存在的 ``session.export_reports()``。
    异常被 ``except`` 吞成一行 warning，于是 reports 目录**永远是空的**：
    用户拿不到清单，程序也不报错，双方都发现不了。这里用真实的
    CleanupBuilder + CleanupReport 跑一遍，确保文件真的落到磁盘上。
    """

    def test_do_export_writes_csv_and_txt(self) -> None:
        from types import SimpleNamespace

        from photobak.core.cleanup import CleanupBuilder
        from photobak.main import do_export

        cfg = self.make_cfg()
        db = IndexDB(cfg.index_db)
        did = db.upsert_device("S", "m", "17", "m")

        # 一条"已验证 + 文件存在"的记录 → 会进入「可放心删」档
        fid = db.upsert_file(sha256="a" * 64, size=10, kind="image", ext="jpg",
                             taken_at="2026-09-12 12:19:17", taken_at_source="exif",
                             time_confidence=3, is_time_uncertain=False,
                             verified=True, second_read_ok=True)
        rel = "照片归档/2026/2026-09/2026-09-12/a.jpg"
        p = paths.safe_join(cfg.backup_root_path, rel)
        paths.ensure_dir(p.parent)
        p.write_bytes(b"x" * 10)
        db.add_storage(file_id=fid, rel_path=rel, tree="archive", link_type="file")
        db.upsert_instance(file_id=fid, device_id=did,
                           source_path="/sdcard/DCIM/Camera/a.jpg",
                           source_mtime=None, app_tag="Camera", root_tag="DCIM")

        cb = CleanupBuilder(cfg, db)
        report = cb.build(device_id=did)
        session = SimpleNamespace(cfg=cfg, cleanup=cb, report=report,
                                  db=db, run_id=1)

        files = do_export(session)

        self.assertTrue(files, "do_export 必须产出报告文件（曾经一个都没有）")
        self.assertTrue(all(f.is_file() for f in files), f"报告文件应真实落盘：{files}")
        exts = {f.suffix for f in files}
        self.assertIn(".csv", exts, "应导出 CSV 清单")
        self.assertIn(".txt", exts, "应导出 TXT 清单")

        # CSV 必须带 BOM，否则 Excel 双击中文乱码
        csvs = [f for f in files if f.suffix == ".csv"]
        self.assertTrue(csvs[0].read_bytes().startswith(b"\xef\xbb\xbf"),
                        "CSV 应带 UTF-8 BOM")


if __name__ == "__main__":
    unittest.main(verbosity=2)
