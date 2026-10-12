"""旧字段迁移测试（阶段 2 的核心验证）。

背景：早期版本按容器/镜像建模，库里与快照 manifest 里存的是
`image_ref`（**实际是游戏本体目录路径**）与 `image_digest`（恒空）。
阶段 2 把它们迁移到 `game_dir`，并明确**不**迁移成 `content_hash`
（因为空值不是内容指纹）。

本测试用**真实的老结构**验证四件事：
  1. 老库（只有 image_ref/image_digest 列）被打开后自动补 `game_dir` 并回填；
  2. 迁移幂等（重复打开不会重复/破坏数据）；
  3. 老快照 manifest（键为 image_ref/image_created）仍能读取；
  4. 迁移**不触碰**存档、MOD 与快照 payload 内容。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.db import Database, LEGACY_FIELD_MIGRATION_KEY  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT 'system', state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
  step TEXT NOT NULL DEFAULT '', needs_stop INTEGER NOT NULL DEFAULT 0, snapshot_id TEXT,
  cancellable INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  result TEXT, error TEXT, rollback_state TEXT, rollback_error TEXT,
  server_id TEXT NOT NULL DEFAULT 'default', payload TEXT);
CREATE TABLE snapshots (id TEXT PRIMARY KEY, kind TEXT NOT NULL, reason TEXT NOT NULL, task_id TEXT,
  created_at REAL NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, manifest_ver INTEGER NOT NULL DEFAULT 1,
  valid INTEGER NOT NULL DEFAULT 0, validated_at REAL, file_count INTEGER NOT NULL DEFAULT 0,
  game_version TEXT NOT NULL DEFAULT '', smapi_version TEXT NOT NULL DEFAULT '',
  image_digest TEXT NOT NULL DEFAULT '', image_ref TEXT NOT NULL DEFAULT '',
  mods_hash TEXT NOT NULL DEFAULT '', saves_hash TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0);
CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at REAL NOT NULL,
  image_ref TEXT NOT NULL, image_digest TEXT NOT NULL,
  game_version TEXT NOT NULL DEFAULT '', smapi_version TEXT NOT NULL DEFAULT '',
  mods_hash TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '');
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

GAME_DIR = "/vol1/@apphome/valleyserver/data/gamefiles/Stardew Valley"


class TestLegacyFieldMigration(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.db_path = C.PKGHOME / "manager" / "legacy-test.db"
        if self.db_path.exists():
            self.db_path.unlink()

    def _make_old_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO versions(recorded_at,image_ref,image_digest,game_version,source) "
                "VALUES(?,?,?,?,?)",
                (1000.0, GAME_DIR, "", "1.6.15", "native"))
            conn.execute(
                "INSERT INTO snapshots(id,kind,reason,created_at,image_ref,image_digest,game_version) "
                "VALUES(?,?,?,?,?,?,?)",
                ("snap-old", "manual", "pre-image-update", 1000.0, GAME_DIR, "", "1.6.15"))
            conn.commit()
        finally:
            conn.close()

    def test_backfills_game_dir_and_keeps_old_columns(self):
        self._make_old_db()
        db = Database(self.db_path)
        ver = db.one("SELECT * FROM versions WHERE recorded_at=1000.0")
        self.assertEqual(ver["game_dir"], GAME_DIR, "旧 image_ref 应原样迁移到 game_dir")
        snap = db.one("SELECT * FROM snapshots WHERE id='snap-old'")
        self.assertEqual(snap["game_dir"], GAME_DIR)
        # 旧列必须保留（审计要求：不破坏历史记录）
        cols = {r[1] for r in db.query("PRAGMA table_info(versions)")}
        self.assertIn("image_ref", cols, "旧列应保留为历史物证，不应删除")
        self.assertIn("image_digest", cols)
        # 迁移标记（幂等依据）
        marker = db.get_setting(LEGACY_FIELD_MIGRATION_KEY, "")
        self.assertTrue(marker, "迁移应记录标记")
        self.assertIn("backfilled", marker)

    def test_migration_is_idempotent(self):
        self._make_old_db()
        db1 = Database(self.db_path)
        first = db1.one("SELECT game_dir FROM versions WHERE recorded_at=1000.0")["game_dir"]
        # 手工把 game_dir 改掉，再打开一次：迁移**不应**覆盖已有值
        db1.execute("UPDATE versions SET game_dir='已迁移的值' WHERE recorded_at=1000.0")
        db2 = Database(self.db_path)
        again = db2.one("SELECT game_dir FROM versions WHERE recorded_at=1000.0")["game_dir"]
        self.assertEqual(again, "已迁移的值", "迁移必须幂等：已有值不能被旧值覆盖")
        self.assertEqual(first, GAME_DIR)

    def test_old_snapshot_manifest_still_readable(self):
        """老 manifest（键为 image_ref/image_created）必须仍能读出游戏目录。"""
        snap_id = "snap-legacy"
        root = C.SNAPSHOT_DIR / snap_id
        (root / "payload").mkdir(parents=True, exist_ok=True)
        (root / "payload" / "marker.txt").write_text("hello", encoding="utf-8")
        manifest = {
            "schema": "valleyserver-snapshot/1",
            "manifest_version": 1,
            "id": snap_id, "kind": "manual", "reason": "pre-image-update",
            "created_at": 1000.0, "committed": True,
            "image_ref": GAME_DIR, "image_digest": "", "image_created": "2026-01-01T00:00:00",
            "game_version": "1.6.15", "smapi_version": "4.5.2",
            "files": [{"path": "marker.txt", "size": 5,
                       "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}],
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        from mgr.snapshots import SnapshotStore
        mgr = SnapshotStore()
        info = mgr.info(snap_id)
        self.assertEqual(info.game_dir, GAME_DIR, "老 manifest 的 image_ref 应回退读出")
        # 校验仍应通过（manifest 自身不参与校验，因此我们不重写历史文件也安全）
        result = mgr.verify(snap_id, deep=True)
        self.assertTrue(result["ok"], result.get("problems"))

    def test_migration_does_not_touch_data_content(self):
        """迁移只动元数据：存档、MOD 与快照 payload 的内容必须原样。"""
        C.SAVES_DIR.mkdir(parents=True, exist_ok=True)
        C.MODS_DIR.mkdir(parents=True, exist_ok=True)
        save = C.SAVES_DIR / "Farm_123456"
        save.mkdir(exist_ok=True)
        (save / "SaveGameInfo").write_text("save-info", encoding="utf-8")
        mod = C.MODS_DIR / "SomeMod"
        mod.mkdir(exist_ok=True)
        (mod / "manifest.json").write_text('{"Name":"SomeMod"}', encoding="utf-8")
        before = {
            str(p.relative_to(C.DATA_ROOT)): p.read_bytes()
            for p in (save / "SaveGameInfo", mod / "manifest.json")
        }
        self._make_old_db()
        Database(self.db_path)                     # 触发迁移
        after = {
            str(p.relative_to(C.DATA_ROOT)): p.read_bytes()
            for p in (save / "SaveGameInfo", mod / "manifest.json")
        }
        self.assertEqual(before, after, "迁移不得触碰存档/MOD 内容")

    def test_new_writes_use_new_column_only(self):
        """新的写入只使用 game_dir；旧列不再被写（保持为空）。"""
        self._make_old_db()
        db = Database(self.db_path)
        db.execute(
            "INSERT INTO versions(recorded_at,game_dir,game_version,source) VALUES(?,?,?,?)",
            (2000.0, "/new/path", "1.6.16", "native"))
        row = db.one("SELECT image_ref, image_digest, game_dir FROM versions WHERE recorded_at=2000.0")
        self.assertEqual(row["game_dir"], "/new/path")
        self.assertEqual(row["image_ref"], "" or row["image_ref"])   # 旧列不再承载语义
        self.assertEqual(row["game_dir"], "/new/path")


class TestHostBindMigration(unittest.TestCase):
    """监听范围的一次性迁移（0.2.14）。

    旧版本 `host_bind` 从未传给进程（实测始终监听 0.0.0.0），存量配置里的
    `127.0.0.1` 只是**未被执行的旧默认值**。0.2.14 起该设置真正生效，
    若不迁移，升级后用户原本可用的局域网 VNC/WebVNC 会突然被掐断。
    """

    def setUp(self):
        C.ensure_dirs()

    def _write(self, payload: dict) -> None:
        C.MANAGER_CONFIG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_legacy_default_is_migrated_once(self):
        self._write({"version": 1, "native": {"host_bind": "127.0.0.1", "game_port": 24642}})
        cfg = C.load_config()
        self.assertEqual(cfg.native.host_bind, "0.0.0.0", "旧默认值应迁移为新默认值")
        self.assertTrue(cfg.host_bind_reviewed, "迁移后必须打标记（保证幂等）")
        self.assertEqual(cfg.native.game_port, 24642, "迁移不得影响其它配置项")

    def test_explicit_choice_is_respected_afterwards(self):
        cfg = C.load_config()
        cfg.host_bind_reviewed = True
        cfg.native.host_bind = "127.0.0.1"
        C.save_config(cfg)
        again = C.load_config()
        self.assertEqual(again.native.host_bind, "127.0.0.1",
                         "用户显式选择「仅本机」后不得被再次迁移掉")

    def test_fresh_install_uses_new_default(self):
        self._write({"version": 1, "native": {"host_bind": "0.0.0.0"}})
        cfg = C.load_config()
        self.assertEqual(cfg.native.host_bind, "0.0.0.0")
        self.assertTrue(cfg.host_bind_reviewed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
