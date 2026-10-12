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
    game_dir      TEXT NOT NULL DEFAULT '',
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
    game_dir      TEXT NOT NULL DEFAULT '',
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



# ── 旧字段迁移（Docker 时代的命名 → 原生语义）───────────────────────────────
#
# 背景：早期版本按容器/镜像建模，字段名沿用了 `image_ref` / `image_digest`：
#   * `image_ref`    实际存的是**游戏本体目录路径** → 迁移为 `game_dir`
#   * `image_digest` 在原生部署下**恒为空**，没有内容指纹的含义 → 不再使用
#
# 迁移原则（对应需求 2.3）：
#   1. 只加新列、回填，**不删旧列** —— SQLite 删列有版本限制，且审计要求不破坏历史记录；
#   2. 幂等：可重复执行（只在目标列为空时回填）；
#   3. 不猜测：`image_digest` 是空值而非指纹，因此**不**迁移成 `content_hash`；
#   4. 不触碰存档/MOD/快照内容，只动元数据列。
#
# 迁移完成后代码只读写新列；旧列保留为历史物证（不再出现在 API 输出中）。
LEGACY_FIELD_MIGRATION_KEY = "migrated_legacy_fields_v1"



# 重建用的目标结构（与 _SCHEMA 一致；旧列保留但放宽为 DEFAULT ''，
# 使新代码不再写旧列时也不会触发 NOT NULL 失败）
_VERSIONS_V2 = """
CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   REAL NOT NULL,
    game_dir      TEXT NOT NULL DEFAULT '',
    game_version  TEXT NOT NULL DEFAULT '',
    smapi_version TEXT NOT NULL DEFAULT '',
    mods_hash     TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    image_ref     TEXT NOT NULL DEFAULT '',
    image_digest  TEXT NOT NULL DEFAULT ''
)
"""

_SNAPSHOTS_V2 = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
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
    game_dir      TEXT NOT NULL DEFAULT '',
    mods_hash     TEXT NOT NULL DEFAULT '',
    saves_hash    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    missing       INTEGER NOT NULL DEFAULT 0,
    image_ref     TEXT NOT NULL DEFAULT '',
    image_digest  TEXT NOT NULL DEFAULT ''
)
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate_legacy_fields(conn: sqlite3.Connection) -> dict[str, int]:
    """把容器时代的字段迁移到原生语义字段。返回统计（便于日志与验证）。

    为什么需要**重建表**而不只是加列：老库里
        `versions.image_ref TEXT NOT NULL`（且**没有默认值**）
    而新代码不再写这一列 —— 若不处理，升级后**任何新插入都会
    报 `NOT NULL constraint failed: versions.image_ref`**（迁移测试实测抓到）。
    SQLite 无法给已有列补默认值，因此需要标准的"建新表→搬数据→换名"重建。

    重建后仍然**保留** image_ref / image_digest 两列（放宽为 `DEFAULT ''`），
    使历史记录继续可查（审计要求：不破坏历史），但代码不再读写它们。
    """
    stats = {"added": 0, "backfilled": 0, "rebuilt": 0}
    for table, ddl in (("versions", _VERSIONS_V2), ("snapshots", _SNAPSHOTS_V2)):
        cols = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        if "game_dir" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN game_dir TEXT NOT NULL DEFAULT ''")
            stats["added"] += 1
        if "image_ref" in cols:
            cur = conn.execute(
                f"UPDATE {table} SET game_dir = image_ref "
                "WHERE (game_dir IS NULL OR game_dir = '') AND image_ref <> ''")
            stats["backfilled"] += int(cur.rowcount or 0)

        cols = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        legacy_notnull = any(
            name in cols and cols[name][3] == 1 and cols[name][4] is None
            for name in ("image_ref", "image_digest"))
        if legacy_notnull:
            tmp = f"{table}__migrating"
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
            conn.execute(ddl.replace(f"CREATE TABLE IF NOT EXISTS {table}",
                                     f"CREATE TABLE {tmp}"))
            new_cols = [row[1] for row in conn.execute(f"PRAGMA table_info({tmp})")]
            shared = [c for c in new_cols if c in cols]
            collist = ",".join(shared)
            conn.execute(f"INSERT INTO {tmp}({collist}) SELECT {collist} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
            stats["rebuilt"] += 1
    return stats


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
                # 旧字段迁移：每次打开都跑（幂等），但只在首次真正写入时记录标记
                stats = _migrate_legacy_fields(conn)
                if stats["added"] or stats["backfilled"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                        (LEGACY_FIELD_MIGRATION_KEY, json.dumps(stats, ensure_ascii=False)))
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
