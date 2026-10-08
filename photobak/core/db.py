"""SQLite 索引库。

核心设计约束（来自用户要求）
----------------------------
1. **只存相对路径**：整个备份文件夹可以被剪切、改名、换盘符，索引依然有效。
2. **写顺序铁律**：先落盘并 fsync，再写索引。绝不允许"索引说有、文件不存在"。
3. **去重只认 SHA-256 完全一致**：``files.sha256`` 唯一索引是唯一判据。
   编辑版（``IMG_x.jpg`` vs ``IMG_x_edit.jpg``）哈希不同 → 两条独立记录。
4. **崩溃安全**：WAL + ``synchronous=FULL``；每次任务结束自动备份索引库。

表结构说明见设计文档 §2.4，这里只补充两点实现取舍：
    - ``instances`` 与 ``storage`` 都挂在 ``files`` 上，删除文件记录时级联清理；
    - ``transfer_jobs`` 的唯一键是 ``(device_id, source_path, expected_size)``，
      这是断点续传能够"精确命中上次未完成的那一个"的关键。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..common import paths
from ..common.errors import InternalError
from ..common.logger import get_logger

log = get_logger("db")

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    serial          TEXT UNIQUE,
    model           TEXT,
    android_version TEXT,
    display_name    TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256            TEXT    NOT NULL UNIQUE,
    size              INTEGER NOT NULL,
    kind              TEXT    NOT NULL,
    ext               TEXT    NOT NULL,
    taken_at          TEXT,
    taken_at_source   TEXT,
    time_confidence   INTEGER NOT NULL DEFAULT 0,
    is_time_uncertain INTEGER NOT NULL DEFAULT 0,
    verified          INTEGER NOT NULL DEFAULT 0,
    verified_at       TEXT,
    second_read_ok    INTEGER,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_sha      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_taken    ON files(taken_at);
CREATE INDEX IF NOT EXISTS idx_files_verified ON files(verified);

CREATE TABLE IF NOT EXISTS instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    source_path  TEXT    NOT NULL,
    source_mtime TEXT,
    app_tag      TEXT,
    root_tag     TEXT,
    last_seen_at TEXT    NOT NULL,
    is_present   INTEGER NOT NULL DEFAULT 1,
    UNIQUE(device_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_inst_file   ON instances(file_id);
CREATE INDEX IF NOT EXISTS idx_inst_device ON instances(device_id, is_present);

CREATE TABLE IF NOT EXISTS storage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    rel_path   TEXT    NOT NULL,
    tree       TEXT    NOT NULL,
    link_type  TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE(tree, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_storage_file ON storage(file_id);
CREATE INDEX IF NOT EXISTS idx_storage_path ON storage(rel_path);

CREATE TABLE IF NOT EXISTS backup_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    channel     TEXT,
    backup_root TEXT,
    status      TEXT,
    total_files INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    ok_count    INTEGER DEFAULT 0,
    skip_count  INTEGER DEFAULT 0,
    fail_count  INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS transfer_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES backup_runs(id),
    source_path     TEXT    NOT NULL,
    device_id       INTEGER NOT NULL,
    expected_size   INTEGER NOT NULL,
    expected_sha256 TEXT,
    bytes_done      INTEGER NOT NULL DEFAULT 0,
    part_path       TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    retries         INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      TEXT    NOT NULL,
    UNIQUE(device_id, source_path, expected_size)
);

CREATE TABLE IF NOT EXISTS failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES backup_runs(id),
    source_path TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT,
    retries     INTEGER DEFAULT 0,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fail_run ON failures(run_id);

-- 未备份文件：被扫描规则"排除"但用户需要知道的那几类
--   （超限被拒 / 缓存目录 / .nomedia 目录）。
-- 为什么单独一张表，而不复用 failures？
--   failures 记的是"备份过程出了错"，会拉高失败计数；而这里是"按规则主动不备份"，
--   属于正常结果，不该算失败。但两者都必须出现在清理清单里，否则用户会以为
--   "我手机上这张照片怎么不见了"。每次备份都会整体重建本表（因为每轮都会重新
--   枚举全部文件），因此不需要额外的失效处理。
CREATE TABLE IF NOT EXISTS unbacked_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    source_path  TEXT    NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    reason_code  TEXT    NOT NULL,
    reason_text  TEXT,
    tier_hint    TEXT    NOT NULL DEFAULT 'pending',
    last_seen_at TEXT    NOT NULL,
    UNIQUE(device_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_unbacked_device ON unbacked_files(device_id);

CREATE TABLE IF NOT EXISTS scan_roots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER REFERENCES backup_runs(id),
    root     TEXT NOT NULL,
    included INTEGER NOT NULL,
    pattern  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 行对象
# ---------------------------------------------------------------------------
@dataclass
class FileRow:
    id: int
    sha256: str
    size: int
    kind: str
    ext: str
    taken_at: str | None
    taken_at_source: str | None
    time_confidence: int
    is_time_uncertain: int
    verified: int
    verified_at: str | None
    second_read_ok: int | None


@dataclass
class JobRow:
    id: int
    source_path: str
    device_id: int
    expected_size: int
    expected_sha256: str | None
    bytes_done: int
    part_path: str
    status: str
    retries: int
    last_error: str | None


class IndexDB:
    """索引库封装。

    线程安全策略：内部一把可重入锁。本项目的数据量很小（百级文件），
    锁竞争可以忽略；换来的是"任何线程都能安全读写"的简单心智模型。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- 连接管理 ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        paths.ensure_dir(self.path.parent)
        try:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        except sqlite3.Error as exc:
            raise InternalError("E702", f"无法打开索引库 {self.path}：{exc}") from exc
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._init_schema()
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：异常自动回滚。"""
        conn = self.connect()
        with self._lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- 建表与迁移 -------------------------------------------------------
    def _init_schema(self) -> None:
        conn = self._conn
        assert conn is not None
        with self._lock:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?,?)",
                             (SCHEMA_VERSION, _now()))
            conn.commit()

    def integrity_check(self) -> tuple[bool, str]:
        """SQLite 自身的一致性检查（快速版）。"""
        conn = self.connect()
        with self._lock:
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
                msg = row[0] if row else "unknown"
                return (msg == "ok"), msg
            except sqlite3.Error as exc:
                return False, str(exc)

    def reindex(self) -> None:
        conn = self.connect()
        with self._lock:
            conn.execute("REINDEX")
            conn.commit()

    # -- devices ----------------------------------------------------------
    def upsert_device(self, serial: str, model: str, android: str, display: str) -> int:
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE devices SET model=?, android_version=?, display_name=?, last_seen_at=? WHERE id=?",
                    (model, android, display, _now(), row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO devices(serial, model, android_version, display_name, first_seen_at, last_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (serial, model, android, display, _now(), _now()),
            )
            return int(cur.lastrowid)

    def get_device(self, device_id: int) -> sqlite3.Row | None:
        conn = self.connect()
        with self._lock:
            return conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()

    # -- files ------------------------------------------------------------
    def get_file_by_sha(self, sha256: str) -> FileRow | None:
        conn = self.connect()
        with self._lock:
            row = conn.execute("SELECT * FROM files WHERE sha256=?", (sha256,)).fetchone()
        return self._to_file_row(row) if row else None

    @staticmethod
    def _to_file_row(row: sqlite3.Row) -> FileRow:
        return FileRow(
            id=row["id"], sha256=row["sha256"], size=row["size"], kind=row["kind"],
            ext=row["ext"], taken_at=row["taken_at"], taken_at_source=row["taken_at_source"],
            time_confidence=row["time_confidence"], is_time_uncertain=row["is_time_uncertain"],
            verified=row["verified"], verified_at=row["verified_at"],
            second_read_ok=row["second_read_ok"],
        )

    def upsert_file(
        self,
        *,
        sha256: str,
        size: int,
        kind: str,
        ext: str,
        taken_at: str | None,
        taken_at_source: str | None,
        time_confidence: int,
        is_time_uncertain: bool,
        verified: bool,
        second_read_ok: bool | None = None,
    ) -> int:
        """插入或更新一条内容记录，返回 file_id。"""
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM files WHERE sha256=?", (sha256,)).fetchone()
            if row:
                fid = int(row["id"])
                conn.execute(
                    "UPDATE files SET size=?, kind=?, ext=?, taken_at=?, taken_at_source=?,"
                    " time_confidence=?, is_time_uncertain=?, verified=?,"
                    " verified_at=COALESCE(?, verified_at),"
                    " second_read_ok=COALESCE(?, second_read_ok), last_seen_at=?"
                    " WHERE id=?",
                    (size, kind, ext, taken_at, taken_at_source, time_confidence,
                     1 if is_time_uncertain else 0, 1 if verified else 0,
                     _now() if verified else None,
                     None if second_read_ok is None else (1 if second_read_ok else 0),
                     _now(), fid),
                )
                return fid
            cur = conn.execute(
                "INSERT INTO files(sha256, size, kind, ext, taken_at, taken_at_source,"
                " time_confidence, is_time_uncertain, verified, verified_at, second_read_ok,"
                " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sha256, size, kind, ext, taken_at, taken_at_source, time_confidence,
                 1 if is_time_uncertain else 0, 1 if verified else 0,
                 _now() if verified else None,
                 None if second_read_ok is None else (1 if second_read_ok else 0),
                 _now(), _now()),
            )
            return int(cur.lastrowid)

    def mark_verified(self, file_id: int, *, second_read_ok: bool | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE files SET verified=1, verified_at=?, second_read_ok=COALESCE(?, second_read_ok)"
                " WHERE id=?",
                (_now(), None if second_read_ok is None else (1 if second_read_ok else 0), file_id),
            )

    def mark_unverified(self, file_id: int, reason: str = "") -> None:
        with self.tx() as conn:
            conn.execute("UPDATE files SET verified=0 WHERE id=?", (file_id,))
            if reason:
                conn.execute("UPDATE files SET last_seen_at=? WHERE id=?", (_now(), file_id))

    def iter_files(self, *, verified_only: bool = False) -> Iterator[FileRow]:
        conn = self.connect()
        sql = "SELECT * FROM files" + (" WHERE verified=1" if verified_only else "")
        with self._lock:
            rows = conn.execute(sql).fetchall()
        for r in rows:
            yield self._to_file_row(r)

    def count_files(self) -> tuple[int, int]:
        """返回 (总数, 已验证数)。"""
        conn = self.connect()
        with self._lock:
            total = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
            ok = conn.execute("SELECT COUNT(*) c FROM files WHERE verified=1").fetchone()["c"]
        return int(total), int(ok)

    # -- instances --------------------------------------------------------
    def upsert_instance(
        self, *, file_id: int, device_id: int, source_path: str,
        source_mtime: str | None, app_tag: str, root_tag: str,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO instances(file_id, device_id, source_path, source_mtime, app_tag,"
                " root_tag, last_seen_at, is_present) VALUES (?,?,?,?,?,?,?,1)"
                " ON CONFLICT(device_id, source_path) DO UPDATE SET"
                " file_id=excluded.file_id, source_mtime=excluded.source_mtime,"
                " app_tag=excluded.app_tag, root_tag=excluded.root_tag,"
                " last_seen_at=excluded.last_seen_at, is_present=1",
                (file_id, device_id, source_path, source_mtime, app_tag, root_tag, _now()),
            )

    def mark_absent(self, device_id: int, seen_paths: Sequence[str]) -> int:
        """把本次扫描未再出现的实例标记为"可能已删除"。

        分批处理，避免 ``IN (...)`` 参数过多触发 SQLite 变量上限。
        """
        conn = self.connect()
        total = 0
        with self._lock:
            rows = conn.execute(
                "SELECT id, source_path FROM instances WHERE device_id=? AND is_present=1",
                (device_id,),
            ).fetchall()
            absent = [r["id"] for r in rows if r["source_path"] not in set(seen_paths)]
            for i in range(0, len(absent), 400):
                batch = absent[i:i + 400]
                conn.executemany("UPDATE instances SET is_present=0 WHERE id=?",
                                 [(x,) for x in batch])
                total += len(batch)
            conn.commit()
        return total

    def instances_of_file(self, file_id: int) -> list[sqlite3.Row]:
        conn = self.connect()
        with self._lock:
            return conn.execute("SELECT * FROM instances WHERE file_id=?", (file_id,)).fetchall()

    # -- unbacked（未备份文件）--------------------------------------------
    def replace_unbacked(self, device_id: int, rows: list[dict]) -> int:
        """整体重建某台设备的"未备份文件"清单。

        为什么要整体重建而不是逐条 upsert？
            每一轮备份都会重新枚举手机上的**全部**文件，因此"本轮没被排除的文件"
            就等价于"已经不需要留在未备份清单里"。整体替换比逐条比对简单得多，
            也不会出现"改了配置之后旧记录阴魂不散"的问题。

        :param rows: 每项形如 ``{"source_path":…, "size":…, "reason_code":…,
                     "reason_text":…, "tier_hint":…}``
        """
        with self.tx() as conn:
            conn.execute("DELETE FROM unbacked_files WHERE device_id=?", (device_id,))
            if not rows:
                return 0
            now = _now()
            payload = [
                (device_id, r["source_path"], int(r.get("size") or 0),
                 r["reason_code"], r.get("reason_text") or "",
                 r.get("tier_hint") or "pending", now)
                for r in rows
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO unbacked_files"
                "(device_id, source_path, size, reason_code, reason_text, tier_hint, last_seen_at)"
                " VALUES (?,?,?,?,?,?,?)",
                payload,
            )
            return len(payload)

    def unbacked_of_device(self, device_id: int) -> list[sqlite3.Row]:
        conn = self.connect()
        with self._lock:
            return conn.execute(
                "SELECT * FROM unbacked_files WHERE device_id=? ORDER BY size DESC",
                (device_id,),
            ).fetchall()

    # -- storage ----------------------------------------------------------
    def add_storage(self, *, file_id: int, rel_path: str, tree: str, link_type: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO storage(file_id, rel_path, tree, link_type, created_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(tree, rel_path) DO UPDATE SET"
                " file_id=excluded.file_id, link_type=excluded.link_type",
                (file_id, rel_path, tree, link_type, _now()),
            )

    def storages_of_file(self, file_id: int) -> list[sqlite3.Row]:
        conn = self.connect()
        with self._lock:
            return conn.execute("SELECT * FROM storage WHERE file_id=?", (file_id,)).fetchall()

    def all_storage_paths(self) -> set[str]:
        conn = self.connect()
        with self._lock:
            rows = conn.execute("SELECT rel_path FROM storage").fetchall()
        return {r["rel_path"] for r in rows}

    def drop_storage(self, rel_path: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM storage WHERE rel_path=?", (rel_path,))

    # -- runs -------------------------------------------------------------
    def start_run(self, channel: str, backup_root: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO backup_runs(started_at, channel, backup_root, status)"
                " VALUES (?,?,?,?)",
                (_now(), channel, backup_root, "running"),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, status: str, total_files: int, total_bytes: int,
                   ok: int, skip: int, fail: int, note: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE backup_runs SET finished_at=?, status=?, total_files=?, total_bytes=?,"
                " ok_count=?, skip_count=?, fail_count=?, note=? WHERE id=?",
                (_now(), status, total_files, total_bytes, ok, skip, fail, note, run_id),
            )

    def last_unfinished_run(self) -> sqlite3.Row | None:
        """查找上次未正常结束的任务（用于崩溃恢复提示）。"""
        conn = self.connect()
        with self._lock:
            return conn.execute(
                "SELECT * FROM backup_runs WHERE status='running' ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        conn = self.connect()
        with self._lock:
            return conn.execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone()

    def mark_run_crashed(self, run_id: int) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE backup_runs SET status='crashed', note='上次运行被中断' WHERE id=?",
                         (run_id,))

    # -- transfer jobs ----------------------------------------------------
    def upsert_job(self, *, run_id: int | None, source_path: str, device_id: int,
                   expected_size: int, expected_sha256: str | None, part_path: str) -> int:
        """创建或复用一条传输任务。

        ``run_id`` 允许为 None：任务记录本身是可选的（外键约束要求它必须存在），
        这样在"只想试算一个任务"的场景下也不会因为外键失败。
        """
        rid = run_id or None
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id, bytes_done, status FROM transfer_jobs"
                " WHERE device_id=? AND source_path=? AND expected_size=?",
                (device_id, source_path, expected_size),
            ).fetchone()
            if row:
                jid = int(row["id"])
                # 已完成的不动；未完成的刷新 run_id 并复位为 pending 以便续传
                if row["status"] != "done":
                    conn.execute(
                        "UPDATE transfer_jobs SET run_id=?, part_path=?, status='pending',"
                        " expected_sha256=COALESCE(?, expected_sha256), updated_at=? WHERE id=?",
                        (rid, part_path, expected_sha256, _now(), jid),
                    )
                return jid
            cur = conn.execute(
                "INSERT INTO transfer_jobs(run_id, source_path, device_id, expected_size,"
                " expected_sha256, bytes_done, part_path, status, retries, updated_at)"
                " VALUES (?,?,?,?,?,0,?, 'pending', 0, ?)",
                (rid, source_path, device_id, expected_size, expected_sha256, part_path, _now()),
            )
            return int(cur.lastrowid)

    def get_job(self, job_id: int) -> JobRow | None:
        conn = self.connect()
        with self._lock:
            row = conn.execute("SELECT * FROM transfer_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return JobRow(row["id"], row["source_path"], row["device_id"], row["expected_size"],
                      row["expected_sha256"], row["bytes_done"], row["part_path"],
                      row["status"], row["retries"], row["last_error"])

    def update_job(self, job_id: int, *, bytes_done: int | None = None, status: str | None = None,
                   retries: int | None = None, last_error: str | None = None,
                   part_path: str | None = None) -> None:
        sets, args = [], []
        if bytes_done is not None:
            sets.append("bytes_done=?"); args.append(bytes_done)
        if status is not None:
            sets.append("status=?"); args.append(status)
        if retries is not None:
            sets.append("retries=?"); args.append(retries)
        if last_error is not None:
            sets.append("last_error=?"); args.append(last_error)
        if part_path is not None:
            sets.append("part_path=?"); args.append(part_path)
        if not sets:
            return
        sets.append("updated_at=?"); args.append(_now())
        args.append(job_id)
        with self.tx() as conn:
            conn.execute(f"UPDATE transfer_jobs SET {', '.join(sets)} WHERE id=?", args)

    def pending_jobs(self) -> list[JobRow]:
        conn = self.connect()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM transfer_jobs WHERE status IN ('pending','running','paused')"
            ).fetchall()
        return [JobRow(r["id"], r["source_path"], r["device_id"], r["expected_size"],
                       r["expected_sha256"], r["bytes_done"], r["part_path"],
                       r["status"], r["retries"], r["last_error"]) for r in rows]

    def cleanup_old_parts(self, keep_days: int = 7) -> int:
        """清理过期的未完成传输（用户设定：异常中断的 .part 保留 7 天）。"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as conn:
            cur = conn.execute(
                "DELETE FROM transfer_jobs WHERE status!='done' AND updated_at < ?", (cutoff,)
            )
            return cur.rowcount or 0

    # -- failures ---------------------------------------------------------
    def add_failure(self, *, run_id: int | None, source_path: str, reason_code: str,
                    reason_text: str, retries: int = 0) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO failures(run_id, source_path, reason_code, reason_text, retries, occurred_at)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, source_path, reason_code, reason_text, retries, _now()),
            )

    def failures_of_run(self, run_id: int) -> list[sqlite3.Row]:
        conn = self.connect()
        with self._lock:
            return conn.execute("SELECT * FROM failures WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    # -- scan roots -------------------------------------------------------
    def record_scan_roots(self, run_id: int, roots: Sequence[tuple[str, bool, str]]) -> None:
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO scan_roots(run_id, root, included, pattern) VALUES (?,?,?,?)",
                [(run_id, r, 1 if inc else 0, pat) for r, inc, pat in roots],
            )

    # -- settings ---------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        conn = self.connect()
        with self._lock:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- 备份 -------------------------------------------------------------
    def backup_to(self, dest: str | Path) -> None:
        """把索引库复制一份（滚动保留由调用方负责）。

        用 SQLite 官方的 backup API 而不是文件拷贝：WAL 模式下直接拷文件
        可能拿到不一致的快照。
        """
        dest_p = Path(dest)
        paths.ensure_dir(dest_p.parent)
        try:
            target = sqlite3.connect(str(dest_p))
            with self._lock:
                assert self._conn is not None
                self._conn.backup(target)
            target.close()
        except sqlite3.Error as exc:
            log.warning("索引库备份失败：%s", exc)

    def raw_query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        with self._lock:
            return conn.execute(sql, args).fetchall()
