"""快照系统测试：一致性协议、原子提交、校验、拒绝恢复损坏快照、保留策略。

覆盖项目要求 §9.7 / §9.8 / §9.9 / §9.11：
  测试 7：快照完整性校验和损坏快照拒绝恢复。
  测试 8：快照创建期间磁盘空间不足。
  测试 9：快照失败时原操作必须取消。
  测试 11：恢复失败时保留现场。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.snapshots import (  # noqa: E402
    MANIFEST_VERSION, SnapshotError, SnapshotInconsistent, SnapshotStore,
    migrate_manifest,
)
from mgr.util import atomic_write_text, sha256_file  # noqa: E402


class FakeStatus:
    def __init__(self, running: bool = False, exists: bool = True):
        self.running = running
        self.exists = exists


class FakeRuntime:
    """模拟 ComposeManager：记录 stop/start 调用，并可让 stop 失败。"""

    def __init__(self, running: bool = True, stop_fails: bool = False):
        self.running = running
        self.stop_fails = stop_fails
        self.calls: list[str] = []

    def status(self):
        return FakeStatus(self.running)

    def stop(self, timeout=180):
        self.calls.append("stop")
        if self.stop_fails:
            return FakeResult(False, "失败")
        self.running = False
        return FakeResult(True, "")

    def start(self, timeout=300):
        self.calls.append("start")
        self.running = True
        return FakeResult(True, "")


class FakeResult:
    def __init__(self, ok: bool, tail: str = ""):
        self.ok = ok
        self.tail = tail
        self.returncode = 0 if ok else 1


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        # 干净的 Saves / Mods
        for d in (C.SAVES_DIR, C.MODS_DIR, C.SNAPSHOT_DIR, C.STAGING_DIR, C.RESTORE_TMP_DIR):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        self._seed_saves()
        self._seed_mods()
        self.compose = FakeRuntime(running=True)
        self.store = SnapshotStore(runtime=self.compose)

    def _seed_saves(self):
        save = C.SAVES_DIR / "Farm_111111111"
        save.mkdir(parents=True)
        (save / "SaveGameInfo").write_bytes(b"<SaveGame><farmName><string>TestFarm</string></farmName></SaveGame>")
        (save / "Farm_111111111").write_bytes(b"<SaveGame>" + b"x" * 500 + b"</SaveGame>")

    def _seed_mods(self):
        mod = C.MODS_DIR / "ALOS"
        (mod / "data").mkdir(parents=True)
        (mod / "manifest.json").write_text(json.dumps({
            "Name": "ALOS", "Version": "1.0.6", "UniqueID": "Lixeer.Always_On_Server",
            "EntryDll": "ALOS.dll", "Author": "Lixeer",
        }), encoding="utf-8")
        (mod / "ALOS.dll").write_bytes(b"MZ fake dll")
        (mod / "data" / "Farm_111111111.json").write_text('{"FarmingLevel":10}', encoding="utf-8")

    def _mods_hash(self) -> str:
        from mgr.snapshots import hash_tree
        return hash_tree(C.MODS_DIR, exclude_names={"data"})[0]


class TestSnapshotCreation(SnapshotTestBase):
    def test_creates_and_verifies(self):
        info = self.store.create(kind="manual", reason="manual")
        self.assertTrue(info.valid)
        self.assertGreater(info.file_count, 0)
        self.assertTrue(self.store.path_for(info.id).is_dir())
        report = self.store.verify(info.id, deep=True)
        self.assertTrue(report["ok"], report["problems"])
        self.assertGreaterEqual(report["checked"], 3)

    def test_stops_server_before_snapshot(self):
        self.store.create(kind="manual", reason="manual")
        self.assertIn("stop", self.compose.calls)
        self.assertIn("start", self.compose.calls)
        # 顺序必须是 stop 在 start 之前
        self.assertLess(self.compose.calls.index("stop"), self.compose.calls.index("start"))

    def test_manifest_contents(self):
        info = self.store.create(kind="auto", reason="pre-mod-install",
                                 task_id="task123", note="测试",
                                 version_info={"game_version": "1.6.15",
                                               "smapi_version": "4.5.1",
                                               "image_digest": "sha256:" + "a" * 64})
        raw = self.store.load_manifest(info.id)
        self.assertEqual(raw["manifest_version"], MANIFEST_VERSION)
        self.assertEqual(raw["schema"], "valleyserver.snapshot")
        self.assertTrue(raw["committed"])
        self.assertEqual(raw["game_version"], "1.6.15")
        self.assertEqual(raw["smapi_version"], "4.5.1")
        self.assertEqual(raw["task_id"], "task123")
        self.assertTrue(raw["server_stopped_for_snapshot"])
        self.assertIn("hashes", raw)
        self.assertIn("files", raw)
        for entry in raw["files"]:
            self.assertTrue(entry["sha256"])
            self.assertGreaterEqual(entry["size"], 0)

    def test_includes_mod_private_data(self):
        """MOD 目录内的每存档数据必须进入快照（否则卸载 MOD 时会丢）。"""
        info = self.store.create(kind="manual", reason="manual")
        payload = self.store.path_for(info.id) / "payload"
        self.assertTrue((payload / "Mods" / "ALOS" / "data" / "Farm_111111111.json").is_file())

    def test_refuses_when_stop_fails(self):
        """停服失败时绝不能在运行状态下创建快照并宣称一致。"""
        compose = FakeRuntime(running=True, stop_fails=True)
        store = SnapshotStore(runtime=compose)
        with self.assertRaises(SnapshotError):
            store.create(kind="manual", reason="manual")
        self.assertEqual(list(store.root.iterdir()), [])

    def test_no_server_needed(self):
        store = SnapshotStore(runtime=None)
        info = store.create(kind="manual", reason="manual")
        self.assertTrue(info.valid)

    def test_disk_guard(self):
        cfg = C.load_config()
        original = cfg.policy.snapshot_space_estimate_mb
        cfg.policy.snapshot_space_estimate_mb = 10 ** 9  # 不可能满足
        C.save_config(cfg)
        try:
            with self.assertRaises(SnapshotError) as ctx:
                self.store.create(kind="manual", reason="manual")
            self.assertIn("磁盘空间不足", str(ctx.exception))
        finally:
            cfg.policy.snapshot_space_estimate_mb = original
            C.save_config(cfg)


class TestSnapshotVerification(SnapshotTestBase):
    def test_detects_tampered_file(self):
        info = self.store.create(kind="manual", reason="manual")
        victim = self.store.path_for(info.id) / "payload" / "Saves" / "Farm_111111111" / "Farm_111111111"
        victim.write_bytes(b"<SaveGame>TAMPERED</SaveGame>")
        report = self.store.verify(info.id, deep=True)
        self.assertFalse(report["ok"])
        self.assertGreater(report["mismatch"], 0)

    def test_detects_missing_file(self):
        info = self.store.create(kind="manual", reason="manual")
        payload = self.store.path_for(info.id) / "payload"
        (payload / "Mods" / "ALOS" / "ALOS.dll").unlink()
        report = self.store.verify(info.id, deep=True)
        self.assertFalse(report["ok"])
        self.assertGreater(report["missing"], 0)

    def test_rejects_uncommitted_manifest(self):
        info = self.store.create(kind="manual", reason="manual")
        mpath = self.store.manifest_path(info.id)
        raw = json.loads(mpath.read_text(encoding="utf-8"))
        raw["committed"] = False
        mpath.write_text(json.dumps(raw), encoding="utf-8")
        report = self.store.verify(info.id)
        self.assertFalse(report["ok"])
        self.assertTrue(any("committed" in p for p in report["problems"]))

    def test_snapshot_info_reports_invalid(self):
        info = self.store.create(kind="manual", reason="manual")
        (self.store.path_for(info.id) / "payload").rename(
            self.store.path_for(info.id) / "payload-moved")
        refreshed = self.store.info(info.id)
        report = self.store.verify(info.id)
        self.assertFalse(report["ok"])


class TestSnapshotRestore(SnapshotTestBase):
    def test_restore_requires_valid_snapshot(self):
        info = self.store.create(kind="manual", reason="manual", stop_server=True)
        # 破坏快照
        victim = self.store.path_for(info.id) / "payload" / "Saves" / "Farm_111111111" / "Farm_111111111"
        victim.write_bytes(b"CORRUPT")
        with self.assertRaises(SnapshotInconsistent) as ctx:
            self.store.restore(info.id)
        self.assertIn("校验未通过", str(ctx.exception))
        # 原数据必须**未被改动**
        self.assertNotEqual(
            (C.SAVES_DIR / "Farm_111111111" / "Farm_111111111").read_bytes(), b"CORRUPT")

    def test_restore_reverts_changes(self):
        info = self.store.create(kind="manual", reason="manual")
        original = (C.MODS_DIR / "ALOS" / "manifest.json").read_bytes()
        # 变更 MOD
        (C.MODS_DIR / "ALOS" / "manifest.json").write_text('{"Name":"CHANGED"}', encoding="utf-8")
        (C.MODS_DIR / "NewMod").mkdir()
        (C.MODS_DIR / "NewMod" / "manifest.json").write_text("{}", encoding="utf-8")
        self.store.restore(info.id)
        self.assertEqual((C.MODS_DIR / "ALOS" / "manifest.json").read_bytes(), original)
        self.assertFalse((C.MODS_DIR / "NewMod").exists())

    def test_restore_missing_snapshot(self):
        with self.assertRaises(SnapshotError):
            self.store.restore("20200101-000000-manual-aaaaaa")

    def test_restore_invalid_snapshot_id(self):
        from mgr.util import ValidationError
        for bad in ("../etc", "a/b", "", "x" * 100, ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.path_for(bad)


class TestRetention(SnapshotTestBase):
    def _make(self, kind: str, n: int):
        ids = []
        for i in range(n):
            info = self.store.create(kind=kind, reason="manual" if kind == "manual" else "schedule")
            ids.append(info.id)
        return ids

    def test_enforce_retention_counts(self):
        cfg = C.load_config()
        cfg.policy.keep_auto_snapshots = 2
        cfg.policy.keep_manual_snapshots = 1
        C.save_config(cfg)
        self._make("auto", 4)
        self._make("manual", 3)
        result = self.store.enforce_retention()
        self.assertEqual(len([i for i in self.store.list() if i.kind == "auto"]), 2)
        self.assertEqual(len([i for i in self.store.list() if i.kind == "manual"]), 1)
        self.assertEqual(len(result["deleted"]), 4)

    def test_retention_never_deletes_pinned(self):
        from mgr.db import get_db
        cfg = C.load_config()
        cfg.policy.keep_auto_snapshots = 1
        cfg.policy.keep_manual_snapshots = 1
        C.save_config(cfg)
        ids = self._make("manual", 3)
        db = get_db()
        db.execute("INSERT OR REPLACE INTO snapshots(id,kind,reason,created_at,pinned) "
                   "VALUES(?,?,?,?,1)", (ids[0], "manual", "manual", 0))
        self.store.enforce_retention()
        remaining = {i.id for i in self.store.list()}
        self.assertIn(ids[0], remaining)

    def test_delete_pinned_refused(self):
        from mgr.db import get_db
        ids = self._make("manual", 1)
        get_db().execute(
            "INSERT OR REPLACE INTO snapshots(id,kind,reason,created_at,pinned) VALUES(?,?,?,?,1)",
            (ids[0], "manual", "manual", 0))
        with self.assertRaises(SnapshotError):
            self.store.delete(ids[0])
        self.store.delete(ids[0], force=True)
        self.assertFalse(self.store.path_for(ids[0]).exists())


class TestManifestMigration(unittest.TestCase):
    def test_v0_migrated(self):
        raw = migrate_manifest({"files": []})
        self.assertEqual(raw["manifest_version"], 1)
        self.assertTrue(raw["committed"])

    def test_future_version_refused(self):
        with self.assertRaises(SnapshotError):
            migrate_manifest({"manifest_version": 99})

    def test_bad_input(self):
        with self.assertRaises(SnapshotError):
            migrate_manifest("not a dict")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main(verbosity=2)
