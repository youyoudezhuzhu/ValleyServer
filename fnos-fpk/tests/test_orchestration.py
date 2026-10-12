"""编排与后台监管的直接测试。

覆盖项目要求 §9 中还缺直接证据的几项：
  测试 3：NAS 重启后的自动恢复（supervisor._boot_recovery）
  测试 4：容器异常退出与重启次数限制（supervisor 重试上限 + 指数退避）
  测试 9：快照失败时原操作必须取消（mod.install / config.apply 端到端）
  （镜像式更新/回滚已随 Docker 后端移除，改为断言「原生模式不提供该能力」）
并验证每个失败路径都会留下明确的失败原因与回滚状态。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.runtime_base import RuntimeStatus  # noqa: E402
from mgr.db import reset_db  # noqa: E402
from mgr.mods import ModManager  # noqa: E402
from mgr.saves import SaveManager  # noqa: E402
from mgr.snapshots import SnapshotError, SnapshotStore  # noqa: E402
from mgr.tasks import TaskFailure, TaskManager, TaskState  # noqa: E402
import orchestration  # noqa: E402
from mgr.versions import VersionManager  # noqa: E402
import mgr.tasks as T  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────────────────────

class Result:
    """既当 Runtime 方法的返回值，也当 util.run() 的返回值。"""

    def __init__(self, ok=True, tail="", stdout=""):
        self.ok = ok
        self.tail = tail
        self.stdout = stdout
        self.stderr = "" if ok else tail
        self.returncode = 0 if ok else 1


class FakeRuntime:
    """原生运行后端替身：可编程、记录调用，用于断言编排行为。"""

    mode = "native"

    def __init__(self, *, running=False, exists=True, ready=True, phase="运行中",
                 stop_ok=True, start_ok=True, console=""):
        self.calls: list[str] = []
        self._running = running
        self._exists = exists
        self.ready = ready
        self.phase = phase
        self.stop_ok = stop_ok
        self.start_ok = start_ok
        self._console = console
        self.start_count = 0
        self.stop_count = 0

    def detect(self):
        return {"ready": self.ready,
                "xstack": {"ok": True, "missing_required": [], "installed": True},
                "game": {"launcher_present": self._exists},
                "guidance": [] if self.ready else ["显示组件缺失"]}

    def validate(self, cfg=None):
        return {"ok": True, "errors": [], "warnings": [], "guidance": []}

    def prepare(self, cfg=None):
        self.calls.append("prepare")

    def status(self):
        return RuntimeStatus(exists=self._exists, running=self._running,
                             status="running" if self._running else "stopped",
                             game_dir="/data/gamefiles/Stardew Valley")

    def readiness(self):
        return {"phase": self.phase, "detail": "stub", "ready": self.ready,
                "layers": {"L1_game_process": self._running,
                           "L1_display_stack": self._running,
                           "L2_ports_listening": self.ready,
                           "L3_smapi_loaded": self.ready},
                "processes": {"xvfb": self._running, "openbox": self._running,
                              "x11vnc": self._running, "websockify": self._running,
                              "game": self._running}}

    def stats(self):
        return {"available": True, "mem_usage": "100 MiB"}

    def start(self, *, timeout=300):
        self.calls.append("start")
        self.start_count += 1
        if self.start_ok:
            self._running = True
            self._exists = True
        return Result(self.start_ok, "" if self.start_ok else "start failed")

    def stop(self, *, timeout=180):
        self.calls.append("stop")
        self.stop_count += 1
        if self.stop_ok:
            self._running = False
        return Result(self.stop_ok, "" if self.stop_ok else "stop failed")

    def restart(self, *, timeout=300):
        self.calls.append("restart")
        return Result(self.start_ok)

    def up(self, *, timeout=900):
        return self.start(timeout=timeout)

    def down(self, *, timeout=300):
        return self.stop(timeout=timeout)

    def recreate(self, *, timeout=900):
        return self.restart(timeout=timeout)

    def logs(self, *, tail=300, since=""):
        return ""

    def console_log(self, *, tail_bytes=200_000):
        return self._console

    def write_selected_save(self, dir_name):
        return Path("/dev/null")

    def version_info(self):
        return {"game_dir": "/data/gamefiles/Stardew Valley"}


class FakeApp:
    """最小应用对象：只提供 orchestration.register_all 需要的属性。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.compose = runtime          # 历史别名
        self.snapshots = SnapshotStore(runtime=runtime)
        self.saves = SaveManager(runtime=runtime)
        self.mods = ModManager(runtime=runtime)
        self.versions = VersionManager(runtime=runtime)
        self.diagnostics = None
        from mgr.db import get_db
        self.db = get_db()


def fresh_services(runtime, *, present_refs=None, image_missing=False):
    """干净的数据目录 + 独立 DB + 重新加载 orchestration。"""
    C.ensure_dirs()
    for d in (C.SAVES_DIR, C.MODS_DIR, C.SNAPSHOT_DIR, C.STAGING_DIR, C.RESTORE_TMP_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    db_path = C.PKGHOME / "manager" / f"orch-{time.time_ns()}.db"
    reset_db(db_path)
    app = FakeApp(runtime)
    mgr = TaskManager()
    T.set_manager(mgr)
    import importlib
    import orchestration
    importlib.reload(orchestration)
    orchestration.register_all(app)
    from mgr.db import get_db
    app.db = get_db()
    return app, mgr


def seed_mod(name="ALOS", uid="Lixeer.Always_On_Server", version="1.0.6"):
    d = C.MODS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "Name": name, "Author": "Lixeer", "Version": version,
        "UniqueID": uid, "EntryDll": f"{name}.dll",
    }), encoding="utf-8")
    (d / f"{name}.dll").write_bytes(b"MZ")
    return d


def seed_save(name="Farm_111111111"):
    d = C.SAVES_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SaveGameInfo").write_text(
        "<SaveGame><farmName><string>F</string></farmName></SaveGame>")
    (d / name).write_text("<SaveGame/>")
    return d


def make_mod_zip(path: Path, dir_name="NewMod", uid="Tester.NewMod"):
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dir_name}/manifest.json", json.dumps({
            "Name": dir_name, "Author": "t", "Version": "1.0.0",
            "UniqueID": uid, "EntryDll": f"{dir_name}.dll"}))
        zf.writestr(f"{dir_name}/{dir_name}.dll", "MZ")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# 快照失败 → 原操作必须取消（项目要求 §9.9）
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotFailureCancelsOperation(unittest.TestCase):
    def test_mod_install_aborted_when_snapshot_fails(self):
        """停服失败 → 快照失败 → 绝不能继续安装 MOD。"""
        rt = FakeRuntime(running=True, stop_ok=False)
        app, mgr = fresh_services(rt)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "new.zip")
        tid = mgr.run_sync("mod.install", payload={"zip_path": str(zip_path)})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("快照创建失败", task["error"])
        self.assertFalse((C.MODS_DIR / "NewMod").exists(),
                         "快照失败后仍安装了 MOD —— 保护机制失效")

    def test_disk_guard_aborts_before_install(self):
        rt = FakeRuntime(running=False)
        cfg = C.load_config()
        original = cfg.policy.snapshot_space_estimate_mb
        cfg.policy.snapshot_space_estimate_mb = 10 ** 9
        C.save_config(cfg)
        try:
            app, mgr = fresh_services(rt)
            zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "x.zip")
            tid = mgr.run_sync("mod.install", payload={"zip_path": str(zip_path)})
            task = mgr.get(tid)
            self.assertEqual(task["state"], TaskState.FAILED.value)
            self.assertIn("磁盘空间不足", task["error"])
            self.assertFalse((C.MODS_DIR / "NewMod").exists())
        finally:
            cfg.policy.snapshot_space_estimate_mb = original
            C.save_config(cfg)

    def test_mod_install_succeeds_with_snapshot(self):
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "ok.zip")
        tid = mgr.run_sync("mod.install", payload={"zip_path": str(zip_path)})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertTrue((C.MODS_DIR / "NewMod" / "manifest.json").is_file())
        valid = [i for i in app.snapshots.list() if i.valid]
        self.assertEqual(len(valid), 1)
        payload = app.snapshots.path_for(valid[0].id) / "payload"
        self.assertTrue((payload / "Mods" / "ALOS").is_dir())
        self.assertFalse((payload / "Mods" / "NewMod").exists())

    def test_save_delete_aborted_when_snapshot_fails(self):
        rt = FakeRuntime(running=True, stop_ok=False)
        app, mgr = fresh_services(rt)
        seed_save()
        tid = mgr.run_sync("save.delete",
                           payload={"dir_name": "Farm_111111111", "confirm": "Farm_111111111"})
        self.assertEqual(mgr.get(tid)["state"], TaskState.FAILED.value)
        self.assertTrue((C.SAVES_DIR / "Farm_111111111").is_dir(),
                        "快照失败后存档仍被删除 —— 保护机制失效")

    def test_config_apply_aborted_when_snapshot_fails(self):
        rt = FakeRuntime(running=True, stop_ok=False)
        app, mgr = fresh_services(rt)
        before = C.load_config().native.game_port
        tid = mgr.run_sync("config.apply",
                           payload={"changes": {"native": {"game_port": 25000}}})
        self.assertEqual(mgr.get(tid)["state"], TaskState.FAILED.value)
        self.assertEqual(C.load_config().native.game_port, before,
                         "快照失败后配置仍被修改 —— 保护机制失效")

    def test_skip_snapshot_flag_is_honored(self):
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "s.zip")
        tid = mgr.run_sync("mod.install",
                           payload={"zip_path": str(zip_path), "skip_snapshot": True})
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        self.assertEqual([i for i in app.snapshots.list() if i.valid], [])


# ─────────────────────────────────────────────────────────────────────────────
# 服务器生命周期（原生）
# ─────────────────────────────────────────────────────────────────────────────

class TestServerLifecycle(unittest.TestCase):
    def test_install_requires_xstack(self):
        rt = FakeRuntime(exists=False)
        rt.detect = lambda: {"ready": False, "xstack": {"ok": False, "missing_required": ["Xvfb"]},
                             "game": {"launcher_present": False}, "guidance": ["显示组件缺失"]}
        app, mgr = fresh_services(rt)
        tid = mgr.run_sync("server.install")
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("依赖检查未通过", task["error"])

    def test_install_requires_game_files(self):
        rt = FakeRuntime(exists=False)
        rt.detect = lambda: {"ready": True, "xstack": {"ok": True, "missing_required": []},
                             "game": {"launcher_present": False}, "guidance": []}
        app, mgr = fresh_services(rt)
        tid = mgr.run_sync("server.install")
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("游戏本体", task["error"])

    def test_install_starts_and_records_version(self):
        rt = FakeRuntime(exists=True, running=False,
                         console="[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356\n"
                                 "[SMAPI] Mods loaded and ready!")
        app, mgr = fresh_services(rt)
        seed_save()
        tid = mgr.run_sync("server.install")
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertIn("start", rt.calls)
        hist = app.versions.history(limit=5)
        self.assertTrue(hist, "安装后应记录版本档案")
        self.assertEqual(hist[0]["game_version"], "1.6.15")
        self.assertEqual(hist[0]["smapi_version"], "4.5.1")

    def test_start_requires_install(self):
        rt = FakeRuntime(exists=False, running=False)
        app, mgr = fresh_services(rt)
        tid = mgr.run_sync("server.start")
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("尚未安装", task["error"])

    def test_stop_and_restart(self):
        rt = FakeRuntime(running=True, exists=True)
        app, mgr = fresh_services(rt)
        tid = mgr.run_sync("server.stop")
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        self.assertIn("stop", rt.calls)
        tid = mgr.run_sync("server.restart")
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        self.assertIn("restart", rt.calls)

    def test_recreate_snapshots_then_restarts(self):
        rt = FakeRuntime(running=True, exists=True)
        app, mgr = fresh_services(rt)
        seed_save()
        tid = mgr.run_sync("server.recreate")
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        self.assertIn("stop", rt.calls)
        self.assertGreaterEqual(rt.start_count, 1)
        self.assertTrue([i for i in app.snapshots.list() if i.valid])


# ─────────────────────────────────────────────────────────────────────────────
# 原生模式必须拒绝镜像式更新/回滚（没有这能力就不假装有）
# ─────────────────────────────────────────────────────────────────────────────

class TestNoContainerSemantics(unittest.TestCase):
    '''原生模式不应有「镜像 / 容器」语义的任务与能力。'''

    def test_update_and_rollback_kinds_are_not_registered(self):
        rt = FakeRuntime()
        app, mgr = fresh_services(rt)
        kinds = set(mgr.known_kinds())
        for bad in ("server.update", "server.rollback", "server.pin"):
            self.assertNotIn(bad, kinds, f"原生模式不应存在镜像类任务：{bad}")

    def test_rollback_capability_uses_snapshots(self):
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        # 这条用例只验证"回滚能力来自快照"，不应受运行环境磁盘余量影响：
        # 把磁盘下限临时设为 0（磁盘守卫本身另有专门用例覆盖）。
        cfg = C.load_config()
        original = cfg.policy.min_free_space_mb
        cfg.policy.min_free_space_mb = 0
        C.save_config(cfg)
        try:
            cap = app.versions.rollback_capability()
            self.assertEqual(cap["method"], "snapshot")
            self.assertFalse(cap["can_rollback"])
            self.assertTrue(cap["missing"])
            # 建一个快照后应报告"具备回滚能力（用快照）"
            seed_save()
            info = app.snapshots.create(kind="manual", reason="manual", stop_server=False)
            app.db.execute(
                "INSERT OR REPLACE INTO snapshots(id,kind,reason,created_at,valid) VALUES(?,?,?,?,1)",
                (info.id, "manual", "manual", info.created_at))
            cap2 = app.versions.rollback_capability()
            self.assertTrue(cap2["can_rollback"], cap2["missing"])
            self.assertEqual(cap2["latest_snapshot"]["id"], info.id)
        finally:
            cfg.policy.min_free_space_mb = original
            C.save_config(cfg)

    def test_rollback_capability_blocked_by_disk_guard(self):
        """磁盘低于策略下限时，即使有快照也必须判为"不可安全回滚"。"""
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        seed_save()
        info = app.snapshots.create(kind="manual", reason="manual", stop_server=False)
        app.db.execute(
            "INSERT OR REPLACE INTO snapshots(id,kind,reason,created_at,valid) VALUES(?,?,?,?,1)",
            (info.id, "manual", "manual", info.created_at))
        cfg = C.load_config()
        original = cfg.policy.min_free_space_mb
        cfg.policy.min_free_space_mb = 10 ** 9      # 必然高于真实可用空间
        C.save_config(cfg)
        try:
            cap = app.versions.rollback_capability()
            self.assertFalse(cap["can_rollback"])
            self.assertTrue(any("磁盘" in m for m in cap["missing"]), cap["missing"])
        finally:
            cfg.policy.min_free_space_mb = original
            C.save_config(cfg)

    def test_version_archive_records_content_not_tags(self):
        rt = FakeRuntime(console="SMAPI 4.5.1 with Stardew Valley 1.6.15")
        app, mgr = fresh_services(rt)
        seed_mod()
        seed_save()
        # 让「游戏本体目录」真实存在，才能验证它被记录（原生模式下该目录由用户提供）
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        game_root.mkdir(parents=True, exist_ok=True)
        (game_root / "Stardew Valley.dll").write_bytes(b"stub")
        rv = app.versions.detect_runtime()
        self.assertEqual(rv.game_version, "1.6.15")
        self.assertEqual(rv.smapi_version, "4.5.1")
        self.assertGreater(rv.mod_count, 0)
        self.assertGreater(rv.save_count, 0)
        self.assertTrue(rv.game_dir.endswith("Stardew Valley"), "版本档案应记录游戏本体目录")
        before = {f.name for f in C.ARCHIVE_DIR.glob("version-*.json")}
        vid = app.versions.record(rv, note="测试")
        new_files = [f for f in C.ARCHIVE_DIR.glob("version-*.json") if f.name not in before]
        self.assertTrue(new_files, "版本档案 JSON 未生成")
        payload = json.loads(new_files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["game_version"], "1.6.15")
        self.assertTrue(payload["mods_hash"])
        _ = vid


# ─────────────────────────────────────────────────────────────────────────────
# 后台监管：NAS 重启恢复与重试上限（项目要求 §9.3 / §9.4）
# ─────────────────────────────────────────────────────────────────────────────

class FakeSupervisorApp:
    def __init__(self, runtime):
        self.runtime = runtime
        self.compose = runtime
        from mgr.db import get_db
        self.db = get_db()
        self.versions = VersionManager(runtime=runtime)


class TestSupervisorRecovery(unittest.TestCase):
    def _supervisor(self, runtime):
        fresh_services(runtime)
        from mgr.supervisor import Supervisor
        return Supervisor(FakeSupervisorApp(runtime))

    def test_boot_recovery_starts_game(self):
        rt = FakeRuntime(running=False, exists=True)
        sup = self._supervisor(rt)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = True
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertIn("start", rt.calls)
        self.assertEqual(rt.start_count, 1)

    def test_boot_recovery_respects_policy(self):
        rt = FakeRuntime(running=False, exists=True)
        sup = self._supervisor(rt)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertNotIn("start", rt.calls)

    def test_boot_recovery_skips_when_already_running(self):
        rt = FakeRuntime(running=True, exists=True)
        sup = self._supervisor(rt)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = True
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertNotIn("start", rt.calls)

    def test_retry_limit_stops_recovery(self):
        rt = FakeRuntime(running=False, exists=True, start_ok=False)
        sup = self._supervisor(rt)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        cfg.policy.start_retry_max = 3
        cfg.policy.start_retry_backoff_seconds = 5
        C.save_config(cfg)
        for _ in range(10):
            sup._last_down_since = time.time() - 10_000
            sup._maybe_auto_start("测试：游戏进程异常退出", C.load_config())
        self.assertEqual(rt.start_count, 3,
                         f"重试次数 {rt.start_count} 应恰好等于上限 3 —— 崩溃循环未被抑制")
        from mgr.db import get_db
        events = get_db().recent_events(limit=50)
        self.assertTrue(any(e["level"] == "error" for e in events),
                        "达到重试上限后没有写入告警事件")

    def test_user_stop_prevents_auto_restart(self):
        rt = FakeRuntime(running=True, exists=True)
        sup = self._supervisor(rt)
        sup.note_user_stop()
        rt._running = False
        for _ in range(5):
            sup._last_down_since = 0.0
            sup._tick()
            time.sleep(0.01)
        self.assertEqual(rt.start_count, 0, "用户主动停止后仍被自动拉起")

    def test_disabled_supervisor_does_not_restart(self):
        rt = FakeRuntime(running=False, exists=True)
        sup = self._supervisor(rt)
        cfg = C.load_config()
        cfg.policy.supervisor_enabled = False
        C.save_config(cfg)
        for _ in range(5):
            sup._last_down_since = 0.0
            sup._tick()
            time.sleep(0.01)
        self.assertEqual(rt.start_count, 0)

    def test_state_reported(self):
        rt = FakeRuntime(running=True, exists=True, ready=True, phase="运行中")
        sup = self._supervisor(rt)
        sup._tick()
        self.assertEqual(sup.state.get("phase"), "运行中")


# ─────────────────────────────────────────────────────────────────────────────
# 注册与其余编排路径
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestrationRegistration(unittest.TestCase):
    def test_all_kinds_registered(self):
        rt = FakeRuntime()
        app, mgr = fresh_services(rt)
        expected = {
            "snapshot.create", "snapshot.verify", "snapshot.delete", "snapshot.restore",
            "server.install", "server.start", "server.stop", "server.restart",
            "server.recreate",
            "save.import", "save.export", "save.delete", "save.activate", "save.restore",
            "mod.install", "mod.update", "mod.overwrite", "mod.remove", "mod.disable",
            "mod.enable", "mod.install-bundle", "config.apply", "upstream.update",
            "diagnostics.bundle", "system.noop", "native.install-gamefiles",
        }
        missing = expected - set(mgr.known_kinds())
        self.assertEqual(missing, set(), f"未注册的任务类型：{missing}")

    def test_save_export_works_while_running(self):
        rt = FakeRuntime(running=True)
        app, mgr = fresh_services(rt)
        seed_save()
        tid = mgr.run_sync("save.export", payload={"dir_name": "Farm_111111111"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertTrue(Path(task["result"]["zip"]).is_file())

    def test_save_delete_refused_while_running(self):
        rt = FakeRuntime(running=True)
        app, mgr = fresh_services(rt)
        seed_save()
        tid = mgr.run_sync("save.delete",
                           payload={"dir_name": "Farm_111111111", "confirm": "Farm_111111111"})
        self.assertEqual(mgr.get(tid)["state"], TaskState.FAILED.value)
        self.assertTrue((C.SAVES_DIR / "Farm_111111111").is_dir())

    def test_mod_remove_refused_while_running(self):
        rt = FakeRuntime(running=True)
        app, mgr = fresh_services(rt)
        seed_mod()
        tid = mgr.run_sync("mod.remove", payload={"dir_name": "ALOS"})
        self.assertEqual(mgr.get(tid)["state"], TaskState.FAILED.value)
        self.assertTrue((C.MODS_DIR / "ALOS").is_dir())

    def test_snapshot_restore_refuses_corrupt_snapshot(self):
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        seed_mod()
        seed_save()
        good = app.snapshots.create(kind="manual", reason="manual", stop_server=False)
        shutil.rmtree(app.snapshots.path_for(good.id) / "payload" / "Mods")
        tid = mgr.run_sync("snapshot.restore", payload={"snapshot_id": good.id})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("校验未通过", task["error"])
        self.assertTrue((C.MODS_DIR / "ALOS" / "manifest.json").is_file())

    def test_native_install_gamefiles_rejects_bad_zip(self):
        rt = FakeRuntime()
        app, mgr = fresh_services(rt)
        bad = C.STAGING_DIR / "uploads" / "bad.zip"
        bad.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "x")
        tid = mgr.run_sync("native.install-gamefiles", payload={"zip_path": str(bad)})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("压缩包检查未通过", task["error"])




class TestAutoHostCoverage(unittest.TestCase):
    """所有"由管理器启动服务端"的路径都必须补上载档开服。

    排查发现（本轮）：ALOS 重启后不会自己载档，所以凡是由管理器启动的路径
    如果不补这一步，就会留下"进程在跑但不开服"的假运行状态。曾经漏掉的：
      * supervisor 的 NAS 开机自启 / 崩溃自动恢复（直接调 runtime.up()）
      * 配置变更后的重启（task_config_apply）
      * 配置回滚后的重启（rollback_config_apply）
      * 首次安装启动（task_server_install）
    """

    def _svc_src(self, name):
        import inspect
        import orchestration
        return inspect.getsource(getattr(orchestration._Services, name))

    def test_config_apply_refreshes_hosting(self):
        self.assertIn("_maybe_auto_host", self._svc_src("task_config_apply"),
                      "改配置会重启运行时，重启后必须重新载档开服")

    def test_rollback_refreshes_hosting(self):
        self.assertIn("_maybe_auto_host", self._svc_src("rollback_config_apply"))

    def test_install_refreshes_hosting(self):
        self.assertIn("_maybe_auto_host", self._svc_src("task_server_install"))

    def test_supervisor_auto_start_schedules_hosting(self):
        from mgr import supervisor
        import inspect
        src = inspect.getsource(supervisor.Supervisor._auto_start)
        self.assertIn("_schedule_auto_host", src,
                      "NAS 开机自启/崩溃恢复后必须补载档开服")

    def test_supervisor_hosting_uses_shared_impl(self):
        from mgr import supervisor
        import inspect
        src = inspect.getsource(supervisor.Supervisor._schedule_auto_host)
        self.assertIn("auto_host_load_save", src)
        self.assertIn("threading.Thread", src, "不能阻塞监管循环")

    def test_supervisor_has_concurrency_guard(self):
        from mgr import supervisor
        import inspect
        init = inspect.getsource(supervisor.Supervisor.__init__)
        self.assertIn("_host_lock", init)
        self.assertIn("_host_running", init)
        self.assertIn("_host_failures", init)

    def test_supervisor_hosting_respects_stop(self):
        """监管停止时不应继续开服流程。"""
        from mgr import supervisor
        import inspect
        src = inspect.getsource(supervisor.Supervisor._schedule_auto_host)
        self.assertIn("self._stop.is_set()", src)

    def test_shared_impl_is_module_level(self):
        """共享实现必须是模块级函数，否则 supervisor 无法复用。"""
        import orchestration
        self.assertTrue(callable(getattr(orchestration, "auto_host_load_save", None)))

    def test_wait_durations_overridable_by_env(self):
        """等待时长可用环境变量覆盖（单测靠它避免真等 60 秒）。"""
        import orchestration
        self.assertTrue(callable(getattr(orchestration, "_wait_seconds", None)))


class TestThreeActionsAreIndependent(unittest.TestCase):
    """「Steam 扫码登录」「下载获取游戏」「启动服务端」必须是**三个独立动作**，
    每个都由用户手动点击（用户明确要求）。

    背景：此前实现把三者串成了自动接力 ——
      * 下载完成后 `start_after` 默认为 true → 自动启动了服务端；
      * SMAPI 更新完成后也会自动把服务端拉起来；
      * 失败路径还会"恢复运行"。
    用户要求：不要自动接力，每一步都要自己点。
    """

    def _src(self, name):
        import inspect
        import orchestration
        return inspect.getsource(getattr(orchestration._Services, name))

    def test_login_does_not_download(self):
        src = self._src("task_native_steam_login")
        self.assertNotIn("run_fetch", src, "登录任务不能触发下载")
        self.assertNotIn("_place_game_body", src, "登录任务不能就位游戏本体")

    def test_login_does_not_start_service(self):
        self.assertNotIn("runtime.up()", self._src("task_native_steam_login"))

    def test_fetch_does_not_start_service(self):
        src = self._src("task_native_steam_fetch")
        self.assertNotIn("runtime.up()", src, "下载任务不能自动启动服务端")
        self.assertNotIn("start_after", src)

    def test_smapi_install_does_not_start_service(self):
        src = self._src("task_native_install_smapi")
        self.assertNotIn("runtime.up()", src, "SMAPI 更新不能自动启动服务端")

    def test_fetch_tells_user_to_start_manually(self):
        src = self._src("task_native_steam_fetch")
        self.assertIn("启动服务器", src)
        self.assertIn("needs_manual_start", src)

    def test_api_has_no_start_after(self):
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        i = src.index("def native_steam_fetch")
        chunk = src[i:i + 900]
        self.assertNotIn('"start_after"', chunk)

    def test_frontend_has_separate_entries(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        # 三个动作各有入口
        self.assertIn("async function steamLogin()", app)
        self.assertIn("async function steamFetchGame()", app)
        self.assertIn("serverAction('start')", app)
        # 下载不再自动启动
        self.assertNotIn("start_after: true", app)

    def test_only_install_and_rollback_start_the_service(self):
        """仅「安装/首次启动」与「配置回滚」允许启动服务端（它们是启动语义本身）。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services)
        # 出现 runtime.up() 的方法只应是这两个
        import re
        blocks = re.split(r"\n    def ", src)
        owners = [b.split("(")[0] for b in blocks if "runtime.up()" in b]
        self.assertEqual(sorted(owners), ["rollback_config_apply", "task_server_install"],
                         f"还有别的方法会自动启动服务端：{owners}")


class TestAutoHostOnStart(unittest.TestCase):
    """启动后自动开服 —— 因为 ALOS 重启后不会自己载档（真机实测结论）。

    实测过程：先 `ServerCMD.new_save` 建好存档并开服；然后重启服务，
    180 秒内游戏一直停在 `titleScreenGameMode (0)`、24642 未监听 →
    **ALOS 不会自动载档**。再手动发
      `ServerCMD.set_multiplayermode 2`（先）→ `ServerCMD.load_save <名>`（后）
    才在 5 秒后出现 `Starting server / Starting LAN server` 与 `[ALOS] Auto Mode On!`。
    顺序不能反：游戏是在**载档时**读 Game1.multiplayerMode。
    """

    def test_set_mode_comes_before_load_save(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_host_bootstrap)
        # 只看**真正的调用**：文档字符串里也会提到这两个指令名，
        # 用 index("set_multiplayermode") 会命中注释而误判（第一版就这么错了）。
        i_mode = src.index('send(f"ServerCMD.set_multiplayermode')
        i_load = src.index('send(f"ServerCMD.load_save')
        self.assertLess(i_mode, i_load, "必须先设联机模式再载档（实测确认）")

    def test_auto_host_task_hook_exists(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_server_start)
        self.assertIn("_maybe_auto_host", src)

    def test_auto_host_can_be_disabled(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("auto_host_on_start", src)

    def test_auto_host_has_crash_loop_guard(self):
        """必须防"崩溃→重启→再崩溃"的死循环（该通道有已知崩溃风险）。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("failures", src)
        self.assertIn("崩溃循环", src)

    def test_auto_host_skips_without_save(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("尚未检测到存档", src)
        self.assertIn("WebVNC", src)
        self.assertIn("重启服务端", src)
        self.assertIn("需手动新建存档并重启服务端", src)

    def test_never_auto_creates_a_save(self):
        """**不自动建存档**（用户明确要求）。

        原因：上游 ServerCMD.new_save 的实现是
        `new TitleMenu().createdNewCharacter(true)` —— 它会跳过命名流程，
        建出玩家名/农场名都为空的存档（实测生成 `_451468556`）。
        这种存档不该替用户创建，所以自动开服与一键开服都不得调用 new_save。
        """
        import inspect
        import orchestration
        for fn in (orchestration._Services._maybe_auto_host,
                   orchestration._Services.task_native_host_bootstrap):
            src = inspect.getsource(fn)
            # 只看**真正的调用**：注释里会说明"为什么不用它"，不能把注释也算进来
            # （第一版断言就因此误报）。
            for call in ('send("ServerCMD.new_save', "send(f\"ServerCMD.new_save",
                         "'ServerCMD.new_save'", '"ServerCMD.new_save"'):
                self.assertNotIn(call, src.replace("ServerCMD.new_save` 的实现是", ""),
                                 f"{fn.__name__} 不应再调用 new_save")

    def test_bootstrap_without_save_gives_manual_steps(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_host_bootstrap)
        self.assertIn("尚未检测到存档", src)
        self.assertIn("需手动新建存档并重启服务端", src)

    def test_auto_host_verifies_port(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("udp_port_open", src, "游戏端口是 UDP，判据必须用它")

    def test_config_default_is_on(self):
        from mgr import config as C
        self.assertTrue(C.NativeConfig().auto_host_on_start)

    def test_config_whitelisted(self):
        import orchestration
        self.assertIn("auto_host_on_start", orchestration.ALLOWED_CONFIG_FIELDS["native"])


class TestStaleConsoleTextNotFatal(unittest.TestCase):
    """console.log 是追加写入的，历史崩溃文本不能把本轮判成致命。"""

    def test_readiness_only_looks_after_last_startup(self):
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertIn("with Stardew Valley", src)
        self.assertIn("markers[-1]", src,
                      "必须只看最后一次启动横幅之后的日志")

    def test_real_console_with_old_crash_still_reports_ok(self):
        """用真机 console.log（含历史 Unhandled exception）验证解析。"""
        fixture = (Path(__file__).resolve().parent / "fixtures" / "smapi-console.log")
        if not fixture.is_file():
            self.skipTest("缺少控制台夹具")
        text = fixture.read_text(encoding="utf-8", errors="replace")
        self.assertIn("Unhandled exception", text, "夹具里应包含历史崩溃记录")
        # 截取最后一次启动之后的部分应当没有致命文本
        import re
        markers = list(re.finditer(r"SMAPI\s+\d+\.\d+[^\n]*with Stardew Valley", text))
        self.assertTrue(markers)
        current = text[markers[-1].start():]
        self.assertNotIn("Unhandled exception", current,
                         "最后一次启动之后不该有崩溃文本")


class TestFirstInstallNotBlockedByEmptySnapshot(unittest.TestCase):
    """首次安装（没有存档/MOD/旧本体）不能被"空快照"卡死。

    真机踩到：全新安装时 `Saves/` 与 `Mods/` 都是空的，快照清单必然为空，
    而空清单会被校验拒绝 → `native.install-gamefiles` 在 10% 处失败，
    用户**永远装不上游戏本体**。
    """

    def _empty(self):
        rt = FakeRuntime(running=False)
        app, mgr = fresh_services(rt)
        C.ensure_dirs()
        for d in (C.SAVES_DIR, C.MODS_DIR):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        if game_root.exists():
            shutil.rmtree(game_root)
        return app, mgr

    def test_nothing_to_protect_detected_on_fresh_install(self):
        app, mgr = self._empty()
        svc = orchestration._Services(app)
        self.assertTrue(svc._nothing_to_protect(), "全新安装应判定为没有可保护内容")

    def test_nothing_to_protect_false_when_save_exists(self):
        app, mgr = self._empty()
        seed_save()
        svc = orchestration._Services(app)
        self.assertEqual(svc._nothing_to_protect(), "")

    def test_nothing_to_protect_false_when_mod_exists(self):
        app, mgr = self._empty()
        seed_mod()
        svc = orchestration._Services(app)
        self.assertEqual(svc._nothing_to_protect(), "")

    def test_nothing_to_protect_false_when_old_gamebody_exists(self):
        app, mgr = self._empty()
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        game_root.mkdir(parents=True, exist_ok=True)
        (game_root / "Stardew Valley.dll").write_bytes(b"MZ")
        svc = orchestration._Services(app)
        self.assertEqual(svc._nothing_to_protect(), "", "已有旧游戏本体时必须保护")

    def test_allow_empty_skips_snapshot_but_normal_path_does_not(self):
        """关键差异：allow_empty=True 跳过快照；不带该参数时仍然强制快照并失败。"""
        app, mgr = self._empty()
        svc = orchestration._Services(app)

        ctx = _FakeCtx(payload={})
        self.assertEqual(svc._auto_snapshot(ctx, "pre-gamefiles-install", allow_empty=True), "")
        self.assertTrue(any("跳过变更前快照" in m for m in ctx.logs), ctx.logs)

        ctx2 = _FakeCtx(payload={})
        with self.assertRaises(TaskFailure):
            svc._auto_snapshot(ctx2, "pre-mod-install")

    def test_install_gamefiles_rejects_windows_build_before_touching_files(self):
        """上传 Windows 版游戏本体 → 必须在校验阶段失败，且不动现有游戏目录。"""
        rt = FakeRuntime(running=False, exists=False)
        app, mgr = fresh_services(rt)
        # 放一个"现有的 Linux 游戏本体"，验证失败时它不被破坏
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        game_root.mkdir(parents=True, exist_ok=True)
        (game_root / "Stardew Valley.dll").write_bytes(b"MZ-old")

        # 造一个 Windows 版的 ZIP
        zip_path = C.STAGING_DIR / "uploads" / "windows.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Stardew Valley/Stardew Valley.dll", "MZ")
            zf.writestr("Stardew Valley/Stardew Valley.exe", "MZ")
            zf.writestr("Stardew Valley/createdump.exe", "MZ")
            zf.writestr("Stardew Valley/Content/Animals/cat.xnb", "x")

        tid = mgr.run_sync("native.install-gamefiles",
                           payload={"zip_path": str(zip_path), "skip_snapshot": True})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("Windows", task["error"])
        self.assertIn("Linux", task["error"])
        self.assertEqual((game_root / "Stardew Valley.dll").read_bytes(), b"MZ-old",
                         "校验失败时不应破坏现有游戏本体")


class _FakeCtx:
    """_auto_snapshot 只用到 payload / log / step / task_id。"""

    def __init__(self, payload=None):
        self.payload = payload or {}
        self.logs: list[str] = []
        self.task_id = "test-task"

    def log(self, message):
        self.logs.append(str(message))

    def step(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
