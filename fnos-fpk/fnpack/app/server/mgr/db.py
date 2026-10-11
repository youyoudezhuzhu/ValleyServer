"""SQLite 持久层：任务队列、事件日志、版本档案、审计日志、快照索引。

只依赖标准库 sqlite3。所有写操作使用事务；开启 WAL 提升并发读能力。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config as C

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    operator        TEXT NOT NULL DEFAULT 'system',
    state           TEXT NOT NULL,
    progress        INTEGER NOT NULL DEFAULT 0,
    step            TEXT NOT NULL DEFAULT '',
    needs_stop      INTEGER NOT NULL DEFAULT 0,
    snapshot_id     TEXT,
    cancellable     INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    result          TEXT,
    error           TEXT,
    rollback_state  TEXT,
    rollback_error  TEXT,
    server_id       TEXT NOT NULL DEFAULT 'default',
    payload         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

CREATE TABLE IF NOT EXISTS task_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT NOT NULL,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '',
    remote    TEXT NOT NULL DEFAULT '',
    ok        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS snapshots (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,          -- auto | manual
    reason        TEXT NOT NULL,
    task_id       TEXT,
    created_at    REAL NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    manifest_ver  INTEGER NOT NULL DEFAULT 1,
    valid         INTEGER NOT NULL DEFAULT 0,
    validated_at  REAL,
    file_count    INTEGER NOT NULL DEFAULT 0,
    game_version  TEXT NOT NULL DEFAULT '',
    smapi_version TEXT NOT NULL DEFAULT '',
    image_digest  TEXT NOT NULL DEFAULT '',
    image_ref     TEXT NOT NULL DEFAULT '',
    mods_hash     TEXT NOT NULL DEFAULT '',
    saves_hash    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    missing       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_created ON snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   REAL NOT NULL,
    image_ref     TEXT NOT NULL,
    image_digest  TEXT NOT NULL,
    game_version  TEXT NOT NULL DEFAULT '',
    smapi_version TEXT NOT NULL DEFAULT '',
    mods_hash     TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_versions_recorded ON versions(recorded_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    level  TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_server_events_ts ON server_events(ts DESC);
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else C.TASK_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._init_lock:
            conn = sqlite3.connect(str(self.path), timeout=30)
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ── settings ────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_json_setting(self, key: str, default: Any = None) -> Any:
        raw = self.get_setting(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json_setting(self, key: str, value: Any) -> None:
        self.set_setting(key, json.dumps(value, ensure_ascii=False))

    # ── audit ───────────────────────────────────────────────────────────────
    def audit(self, actor: str, action: str, *, target: str = "", detail: str = "",
              remote: str = "", ok: bool = True) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO audit(ts,actor,action,target,detail,remote,ok) VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor[:120], action[:120], target[:400], detail[:4000],
                 remote[:120], 1 if ok else 0),
            )

    def recent_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    # ── server events ───────────────────────────────────────────────────────
    def event(self, level: str, source: str, message: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO server_events(ts,level,source,message) VALUES(?,?,?,?)",
                (time.time(), level[:16], source[:64], message[:2000]),
            )

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM server_events ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db


def reset_db(path: Path) -> Database:
    """测试用：替换全局连接。"""
    global _db
    with _db_lock:
        _db = Database(path)
    return _db
