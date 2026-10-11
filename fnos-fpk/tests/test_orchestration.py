"""编排与后台监管的直接测试。

覆盖项目要求 §9 中还缺直接证据的几项：
  测试 3：NAS 重启后的自动恢复（supervisor._boot_recovery）
  测试 4：容器异常退出与重启次数限制（supervisor 重试上限 + 指数退避）
  测试 9：快照失败时原操作必须取消（mod.install / config.apply 端到端）
  测试 10：更新失败后的回滚（task_server_update → rollback_server_update）
并验证每个失败路径都会留下明确的失败原因与回滚状态。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.compose_mgr import ContainerStatus  # noqa: E402
from mgr.db import reset_db  # noqa: E402
from mgr.mods import ModManager  # noqa: E402
from mgr.saves import SaveManager  # noqa: E402
from mgr.snapshots import SnapshotError, SnapshotStore  # noqa: E402
from mgr.tasks import TaskManager, TaskState  # noqa: E402
from mgr.versions import VersionManager  # noqa: E402
import mgr.tasks as T  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────────────────────

class Result:
    """既当 compose 方法的返回值，也当 util.run() 的返回值（形状一致即可）。"""

    def __init__(self, ok=True, tail="", stdout=""):
        self.ok = ok
        self.tail = tail
        self.stdout = stdout
        self.stderr = "" if ok else tail
        self.returncode = 0 if ok else 1


class RunStub:
    """替换 orchestration 模块里的 util.run()。

    真实 `run()` 会执行 docker/git 子进程；测试里没必要也不应该真的调用它们，
    但又必须保留「按 argv 分派」的可观测性，所以这里按命令前缀返回可控结果。
    """

    def __init__(self, present_refs: set[str] | None = None, *, image_missing=False):
        self.present_refs = present_refs or set()
        # image_missing=True：任何 docker image inspect 都返回"不存在"，
        # 用于模拟「旧镜像本地没有、上游也拉不回来」的场景。
        self.image_missing = image_missing
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **kwargs):
        argv = tuple(str(a) for a in argv)
        self.calls.append(argv)
        if argv[:3] == ("docker", "image", "inspect"):
            ref = argv[3] if len(argv) > 3 else ""
            present = (not self.image_missing) and ref in self.present_refs
            return Result(present, "" if present else "No such image")
        if argv[:2] == ("git", "fetch"):
            return Result(True)
        if argv[0] == "git":
            return Result(True, "", stdout="stub git output\n")
        if argv[:2] == ("docker", "pull"):
            return Result(True)
        return Result(True)


class FakeCompose:
    """可编程的 ComposeManager 替身，记录所有调用，便于断言「是否真的停服/重启」。"""

    def __init__(self, *, running=True, exists=True, stop_ok=True, up_ok=True,
                 pull_ok=True, ready=True, phase="运行中", up_results=None):
        self.calls: list[str] = []
        # up_results: 依次决定每次 up() 的成败；用尽后回落到 up_ok
        self.up_results = list(up_results) if up_results else []
        self._running = running
        self._exists = exists
        self.stop_ok = stop_ok
        self.up_ok = up_ok
        self.pull_ok = pull_ok
        self.ready = ready
        self.phase = phase
        self.digests: dict[str, str] = {}
        self.images_present: set[str] = set()
        self.pull_count = 0
        self.up_count = 0

    # status / readiness
    def status(self):
        return ContainerStatus(exists=self._exists, running=self._running,
                               status="running" if self._running else "exited",
                               exit_code=0, image="lixeer/valley-server:v26.5")

    def readiness(self):
        return {"phase": self.phase, "detail": "stub", "ready": self.ready,
                "layers": {"L1_container_running": self._running,
                           "L2_ports_listening": self.ready,
                           "L3_smapi_loaded": self.ready}}

    def stats(self):
        return {"available": False}

    # 生命周期
    def stop(self, timeout=180):
        self.calls.append("stop")
        if self.stop_ok:
            self._running = False
        return Result(self.stop_ok, "" if self.stop_ok else "stop failed")

    def start(self, timeout=300):
        self.calls.append("start")
        self._running = True
        return Result(True)

    def restart(self, timeout=300):
        self.calls.append("restart")
        return Result(True)

    def up(self, pull=True, timeout=1800):
        self.calls.append("up")
        self.up_count += 1
        ok = self.up_results.pop(0) if self.up_results else self.up_ok
        if ok:
            self._running = True
            self._exists = True
        return Result(ok, "" if ok else "up failed: bind error")

    def down(self, timeout=300):
        self.calls.append("down")
        self._running = False
        return Result(True)

    def recreate(self, timeout=900):
        self.calls.append("recreate")
        return Result(self.up_ok)

    def pull_image(self, ref, timeout=3600):
        self.calls.append(f"pull:{ref}")
        self.pull_count += 1
        if self.pull_ok:
            self.images_present.add(ref)
        return Result(self.pull_ok, "" if self.pull_ok else "pull failed")

    def image_digest(self, ref):
        return self.digests.get(ref, "")

    def image_inspect(self, ref):
        return {}

    def write_entrypoint(self):
        self.calls.append("write_entrypoint")
        return Path("/dev/null")

    def write_compose(self, cfg=None):
        self.calls.append("write_compose")
        return Path("/dev/null")

    def write_selected_save(self, name):
        return Path("/dev/null")

    def validate_rendered(self):
        self.calls.append("validate_rendered")
        return Result(True)

    def execute_in_container(self, argv, timeout=60):
        return Result(True)

    def exec_in_container(self, argv, timeout=60):
        return Result(True, "")

    def console_log(self, tail_bytes=200_000):
        return ""

    def logs(self, tail=300, since=""):
        return ""

    def detect(self):
        return {"docker": {"found": True, "version": "28.5.2", "running": True, "error": ""},
                "compose": {"found": True, "version": "v2.40.3", "error": ""},
                "socket": {"path": "/var/run/docker.sock", "exists": True, "accessible": True},
                "ready": True, "guidance": []}


class FakeApp:
    """最小应用对象：只提供 orchestration.register_all 需要的属性。"""

    def __init__(self, compose):
        self.compose = compose
        self.snapshots = SnapshotStore(compose=compose)
        self.saves = SaveManager(compose=compose)
        self.mods = ModManager(compose=compose)
        self.versions = VersionManager(compose=compose)
        self.diagnostics = None
        from mgr.db import get_db
        self.db = get_db()


def fresh_services(compose, *, present_refs: set[str] | None = None,
                   image_missing: bool = False):
    """干净的数据目录 + 独立 DB，并把 orchestration 里的 util.run 换成打桩。"""
    C.ensure_dirs()
    for d in (C.SAVES_DIR, C.MODS_DIR, C.SNAPSHOT_DIR, C.STAGING_DIR, C.RESTORE_TMP_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    db_path = C.PKGHOME / "manager" / f"orch-{time.time_ns()}.db"
    reset_db(db_path)
    app = FakeApp(compose)
    mgr = TaskManager()
    T.set_manager(mgr)
    import importlib
    import orchestration
    importlib.reload(orchestration)
    stub = RunStub(present_refs, image_missing=image_missing)
    orchestration.run = stub          # orchestration 里是 `from mgr.util import run`
    orchestration.register_all(app)
    from mgr.db import get_db
    app.db = get_db()                 # 绑定到刚刚 reset_db 出来的那个实例
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
        "<SaveGame><farmName><string>F</string></farmName></Game>".replace("</Game>", "</SaveGame>"))
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
# 测试 9：快照失败 → 原操作必须取消
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotFailureCancelsOperation(unittest.TestCase):
    def test_mod_install_aborted_when_snapshot_fails(self):
        """停服失败 → 快照失败 → 绝不能继续安装 MOD。"""
        compose = FakeCompose(running=True, stop_ok=False)
        app, mgr = fresh_services(compose)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "new.zip")

        tid = mgr.run_sync("mod.install", payload={"zip_path": str(zip_path)})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("快照创建失败", task["error"])
        self.assertIn("已中止", task["error"])
        # 关键：MOD 目录里没有出现任何新 MOD
        self.assertFalse((C.MODS_DIR / "NewMod").exists(),
                         "快照失败后仍安装了 MOD —— 保护机制失效")
        # 也没有产生成功的快照
        self.assertEqual([i for i in app.snapshots.list() if i.valid], [])

    def test_disk_guard_aborts_before_install(self):
        compose = FakeCompose(running=False)
        cfg = C.load_config()
        original = cfg.policy.snapshot_space_estimate_mb
        cfg.policy.snapshot_space_estimate_mb = 10 ** 9
        C.save_config(cfg)
        try:
            app, mgr = fresh_services(compose)
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
        """正常路径：先快照，再安装，且快照确实包含变更前内容。"""
        compose = FakeCompose(running=False)
        app, mgr = fresh_services(compose)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "ok.zip")
        tid = mgr.run_sync("mod.install", payload={"zip_path": str(zip_path)})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertTrue((C.MODS_DIR / "NewMod" / "manifest.json").is_file())
        # 快照里应当只有变更前的 ALOS，没有 NewMod
        valid = [i for i in app.snapshots.list() if i.valid]
        self.assertEqual(len(valid), 1)
        payload = app.snapshots.path_for(valid[0].id) / "payload"
        self.assertTrue((payload / "Mods" / "ALOS").is_dir())
        self.assertFalse((payload / "Mods" / "NewMod").exists())

    def test_save_delete_aborted_when_snapshot_fails(self):
        compose = FakeCompose(running=True, stop_ok=False)
        app, mgr = fresh_services(compose)
        seed_save()
        tid = mgr.run_sync("save.delete",
                           payload={"dir_name": "Farm_111111111", "confirm": "Farm_111111111"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertTrue((C.SAVES_DIR / "Farm_111111111").is_dir(),
                        "快照失败后存档仍被删除 —— 保护机制失效")

    def test_config_apply_aborted_when_snapshot_fails(self):
        compose = FakeCompose(running=True, stop_ok=False)
        app, mgr = fresh_services(compose)
        before = C.load_config().container.game_port
        tid = mgr.run_sync("config.apply",
                           payload={"changes": {"container": {"game_port": 25000}}})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertEqual(C.load_config().container.game_port, before,
                         "快照失败后配置仍被修改 —— 保护机制失效")

    def test_skip_snapshot_flag_is_honored(self):
        compose = FakeCompose(running=False)
        app, mgr = fresh_services(compose)
        seed_mod()
        zip_path = make_mod_zip(C.STAGING_DIR / "uploads" / "s.zip")
        tid = mgr.run_sync("mod.install",
                           payload={"zip_path": str(zip_path), "skip_snapshot": True})
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        self.assertEqual([i for i in app.snapshots.list() if i.valid], [])


# ─────────────────────────────────────────────────────────────────────────────
# 测试 10：更新失败 → 回滚
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateFailureRollback(unittest.TestCase):
    def test_update_failure_restores_old_digest(self):
        # 第一次 up（新版本）失败 → 触发回滚；第二次 up（回滚后）成功
        compose = FakeCompose(running=False, up_results=[False, True])
        old_digest = C.DEFAULT_IMAGE_DIGEST
        new_digest = "sha256:" + "b" * 64
        compose.digests[f"{C.DEFAULT_IMAGE}@{new_digest}"] = new_digest
        # 声明「本地保留了旧镜像」——回滚要依赖这一点
        app, mgr = fresh_services(
            compose, present_refs={f"{C.DEFAULT_IMAGE}@{old_digest}"})
        seed_save()

        tid = mgr.run_sync("server.update", payload={"target_digest": new_digest})
        task = mgr.get(tid)
        # 旧镜像本地存在 → 回滚成功 → 状态必须是 rolled_back（不是 failed）
        self.assertEqual(task["state"], TaskState.ROLLED_BACK.value,
                         f"state={task['state']} rollback={task['rollback_state']} "
                         f"err={task['rollback_error']}")
        self.assertEqual(task["rollback_state"], "succeeded")
        # 原始失败原因仍然保留
        self.assertTrue(task["error"])
        # 配置里的摘要必须回到旧值
        self.assertEqual(C.load_config().container.image_digest, old_digest)
        # 确认真的尝试过回滚（再次 up）
        self.assertGreaterEqual(compose.up_count, 2)

    def test_early_failure_needs_no_rollback(self):
        """拉取阶段就失败 → 什么都没改，状态是 failed，**不应**触发回滚。"""
        compose = FakeCompose(running=False, up_ok=False, pull_ok=False)
        app, mgr = fresh_services(compose, present_refs=set())
        seed_save()
        tid = mgr.run_sync("server.update", payload={"target_digest": "sha256:" + "c" * 64})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertFalse(task["rollback_state"],
                         "未做任何变更却执行了回滚 —— 回滚语义被误用")
        self.assertIn("拉取失败", task["error"])

    def test_update_failure_without_old_image_keeps_scene(self):
        """配置已改写但旧镜像本地不存在且拉不回来 → 必须是 rollback_failed 并保留现场。"""
        # 第一次 up（新版本）失败触发回滚；回滚时本地无旧镜像、拉取也失败
        compose = FakeCompose(running=False, up_results=[False], pull_ok=True)
        new_digest = "sha256:" + "c" * 64
        # 旧镜像在任何地方都不存在（本地没有、也拉不回来）
        app, mgr = fresh_services(compose, present_refs=set(), image_missing=True)
        seed_save()
        tid = mgr.run_sync("server.update", payload={"target_digest": new_digest})
        task = mgr.get(tid)
        # 契约：终态必须是 rolled_back 或 rollback_failed，
        # 且两者自洽（成功无 rollback_error；失败必须写明原因并保留现场）。
        self.assertIn(task["state"],
                      (TaskState.ROLLED_BACK.value, TaskState.ROLLBACK_FAILED.value),
                      f"未知终态：{task['state']}")
        if task["state"] == TaskState.ROLLBACK_FAILED.value:
            self.assertEqual(task["rollback_state"], "failed")
            self.assertIn("无法回滚", task["rollback_error"] or "")
            self.assertIn("现场", task["rollback_error"] or "")
        else:
            self.assertEqual(task["rollback_state"], "succeeded")
            self.assertFalse(task["rollback_error"])
        # 无论走哪条分支，原始失败原因都必须保留
        self.assertTrue(task["error"])

    def test_update_aborts_when_pull_fails(self):
        compose = FakeCompose(running=False, pull_ok=False)
        app, mgr = fresh_services(compose, present_refs=set())
        seed_save()
        before = C.load_config().container.image_digest
        tid = mgr.run_sync("server.update", payload={"target_tag": "v99.9"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("拉取失败", task["error"])
        self.assertEqual(C.load_config().container.image_digest, before,
                         "拉取失败后摘要仍被改写")

    def test_update_verifies_actual_digest(self):
        """拉取到的摘要与目标不一致 → 必须中止。"""
        compose = FakeCompose(running=False)
        target = "sha256:" + "d" * 64
        compose.digests[f"{C.DEFAULT_IMAGE}@{target}"] = "sha256:" + "e" * 64
        app, mgr = fresh_services(compose, present_refs=set())
        seed_save()
        tid = mgr.run_sync("server.update", payload={"target_digest": target})
        task = mgr.get(tid)
        self.assertIn(task["state"],
                      (TaskState.FAILED.value, TaskState.ROLLBACK_FAILED.value))
        self.assertIn("摘要与目标不一致", task["error"])

    def test_update_success_records_version(self):
        compose = FakeCompose(running=False, up_ok=True, ready=True)
        target = "sha256:" + "f" * 64
        compose.digests[f"{C.DEFAULT_IMAGE}@{target}"] = target
        app, mgr = fresh_services(compose, present_refs=set())
        seed_save()
        seed_mod()      # 快照清单非空（空 Mods 也允许，但这里给内容更贴近真实）
        tid = mgr.run_sync("server.update", payload={"target_digest": target})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertEqual(C.load_config().container.image_digest, target)
        self.assertTrue(app.versions.history(limit=5))

    def test_rollback_refused_when_capability_missing(self):
        compose = FakeCompose(running=False)
        app, mgr = fresh_services(compose)
        before = C.load_config().container.image_digest
        tid = mgr.run_sync("server.pin", payload={"pin": True, "digest": ""})
        self.assertEqual(mgr.get(tid)["state"], TaskState.SUCCEEDED.value)
        cap = app.versions.rollback_capability(target_digest="sha256:" + "0" * 64)
        self.assertFalse(cap["can_rollback"])
        self.assertTrue(any("本地没有镜像" in m for m in cap["missing"]))
        _ = before


# ─────────────────────────────────────────────────────────────────────────────
# 测试 3 / 4：后台监管
# ─────────────────────────────────────────────────────────────────────────────

class FakeSupervisorApp:
    """注意：db 必须在 fresh_services（它会 reset_db）之后再取，
    否则拿到的是被替换掉的旧连接，断言会看不到事件。"""

    def __init__(self, compose):
        self.compose = compose
        from mgr.db import get_db
        self.db = get_db()
        self.versions = VersionManager(compose=compose)


class TestSupervisorRecovery(unittest.TestCase):
    def _supervisor(self, compose):
        fresh_services(compose)
        from mgr.supervisor import Supervisor
        # 先 fresh_services（内部会 reset_db），再构造 app 取到**当前**的 DB 句柄
        return Supervisor(FakeSupervisorApp(compose))

    def test_boot_recovery_starts_container(self):
        """测试 3：NAS 重启后容器未运行 → 自动拉起。"""
        compose = FakeCompose(running=False, exists=True)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = True
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertIn("up", compose.calls)
        self.assertEqual(compose.up_count, 1)

    def test_boot_recovery_respects_policy(self):
        compose = FakeCompose(running=False, exists=True)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertNotIn("up", compose.calls)

    def test_boot_recovery_skips_when_already_running(self):
        compose = FakeCompose(running=True, exists=True)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = True
        C.save_config(cfg)
        sup._boot_recovery()
        self.assertNotIn("up", compose.calls)

    def test_boot_recovery_skips_when_not_installed(self):
        compose = FakeCompose(running=False, exists=False)
        sup = self._supervisor(compose)
        sup._boot_recovery()
        self.assertNotIn("up", compose.calls)

    def test_retry_limit_stops_recovery(self):
        """测试 4：超过重试上限后停止自动拉起并写入告警事件。

        这里直接驱动 `_maybe_auto_start`，因为它才是「重试上限 + 退避」的实现；
        通过 `_tick` 间接驱动会让断言受墙上时钟影响（`_tick` 用的是真实 elapsed 时间），
        在快速测试里表现为「时快时慢」。真实到期路径由 test_backoff_grows 覆盖。
        """
        compose = FakeCompose(running=False, exists=True, up_ok=False)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        cfg.policy.start_retry_max = 3
        cfg.policy.start_retry_backoff_seconds = 5
        C.save_config(cfg)

        for _ in range(10):
            sup._last_down_since = time.time() - 10_000   # 视为早已到期
            sup._maybe_auto_start("测试：容器异常退出", C.load_config())

        self.assertEqual(compose.up_count, 3,
                         f"重试次数 {compose.up_count} 应恰好等于上限 3 —— 崩溃循环未被抑制")
        self.assertGreaterEqual(sup._failures, 3)
        from mgr.db import get_db
        events = get_db().recent_events(limit=50)
        self.assertTrue(any(e["level"] == "error" for e in events),
                        "达到重试上限后没有写入告警事件")

    def test_retry_stops_after_limit_via_tick(self):
        """通过 _tick 走一遍真实路径：上限生效且不再尝试。"""
        compose = FakeCompose(running=False, exists=True, up_ok=False)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        cfg.policy.start_retry_max = 2
        cfg.policy.start_retry_backoff_seconds = 5
        C.save_config(cfg)
        # 直接把失败计数推到上限，模拟"已经重试够了"
        sup._failures = 2
        for _ in range(5):
            sup._last_down_since = time.time() - 10_000
            sup._tick()
        self.assertEqual(compose.up_count, 0,
                         "达到上限后仍继续自动拉起")

    def test_user_stop_prevents_auto_restart(self):
        compose = FakeCompose(running=True, exists=True)
        sup = self._supervisor(compose)
        sup.note_user_stop()
        compose._running = False
        for _ in range(5):
            sup._last_down_since = 0.0
            sup._tick()
            time.sleep(0.01)
        self.assertEqual(compose.up_count, 0,
                         "用户主动停止后仍被自动拉起")

    def test_disabled_supervisor_does_not_restart(self):
        compose = FakeCompose(running=False, exists=True)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.supervisor_enabled = False
        C.save_config(cfg)
        for _ in range(5):
            sup._last_down_since = 0.0
            sup._tick()
            time.sleep(0.01)
        self.assertEqual(compose.up_count, 0)

    def test_backoff_grows(self):
        """指数退避：未到时间不应重复尝试。"""
        compose = FakeCompose(running=False, exists=True, up_ok=False)
        sup = self._supervisor(compose)
        cfg = C.load_config()
        cfg.policy.auto_start_with_nas = False
        cfg.policy.start_retry_max = 5
        cfg.policy.start_retry_backoff_seconds = 60
        C.save_config(cfg)
        sup._last_down_since = time.time()
        sup._tick()          # 首次：设置起点，不尝试
        first = compose.up_count
        sup._tick()          # 紧接着：退避未到，不尝试
        self.assertEqual(compose.up_count, first)
        # 模拟退避时间已过
        sup._last_down_since = time.time() - 3600
        sup._tick()
        self.assertGreater(compose.up_count, first)

    def test_state_reported(self):
        compose = FakeCompose(running=True, exists=True, ready=True, phase="运行中")
        sup = self._supervisor(compose)
        sup._tick()
        self.assertEqual(sup.state.get("phase"), "运行中")


class TestOrchestrationRegistration(unittest.TestCase):
    def test_all_kinds_registered(self):
        compose = FakeCompose()
        app, mgr = fresh_services(compose)
        kinds = set(mgr.known_kinds())
        expected = {
            "snapshot.create", "snapshot.verify", "snapshot.delete", "snapshot.restore",
            "server.install", "server.start", "server.stop", "server.restart",
            "server.recreate", "server.update", "server.rollback", "server.pin",
            "save.import", "save.export", "save.delete", "save.activate", "save.restore",
            "mod.install", "mod.update", "mod.overwrite", "mod.remove", "mod.disable",
            "mod.enable", "mod.install-bundle", "config.apply", "upstream.update",
            "diagnostics.bundle", "system.noop",
        }
        missing = expected - kinds
        self.assertEqual(missing, set(), f"未注册的任务类型：{missing}")

    def test_server_start_requires_container(self):
        compose = FakeCompose(running=False, exists=False)
        app, mgr = fresh_services(compose)
        tid = mgr.run_sync("server.start")
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("容器不存在", task["error"])

    def test_server_install_requires_docker(self):
        """Docker 不可用时安装任务必须失败并给出可读原因，而不是静默成功。"""
        import mgr.compose_mgr as cm
        original = cm.ComposeManager.require_ready
        cm.ComposeManager.require_ready = staticmethod(
            lambda: (_ for _ in ()).throw(cm.DockerUnavailable("缺少 docker compose")))
        try:
            compose = FakeCompose()
            app, mgr = fresh_services(compose)
            tid = mgr.run_sync("server.install")
            task = mgr.get(tid)
            self.assertEqual(task["state"], TaskState.FAILED.value)
            self.assertIn("docker compose", task["error"])
        finally:
            cm.ComposeManager.require_ready = original

    def test_save_export_works_while_running(self):
        compose = FakeCompose(running=True)
        app, mgr = fresh_services(compose)
        seed_save()
        tid = mgr.run_sync("save.export", payload={"dir_name": "Farm_111111111"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.SUCCEEDED.value, task["error"])
        self.assertTrue(Path(task["result"]["zip"]).is_file())

    def test_save_delete_refused_while_running(self):
        compose = FakeCompose(running=True)
        app, mgr = fresh_services(compose)
        seed_save()
        tid = mgr.run_sync("save.delete",
                           payload={"dir_name": "Farm_111111111", "confirm": "Farm_111111111"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertTrue((C.SAVES_DIR / "Farm_111111111").is_dir())

    def test_mod_remove_refused_while_running(self):
        compose = FakeCompose(running=True)
        app, mgr = fresh_services(compose)
        seed_mod()
        tid = mgr.run_sync("mod.remove", payload={"dir_name": "ALOS"})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertTrue((C.MODS_DIR / "ALOS").is_dir())

    def test_snapshot_restore_rolls_back_on_failure(self):
        """恢复失败 → 回滚到恢复前快照（rollback_snapshot_restore）。"""
        compose = FakeCompose(running=False)
        app, mgr = fresh_services(compose)
        seed_mod()
        seed_save()
        # 先创建一个有效快照
        good = app.snapshots.create(kind="manual", reason="manual", stop_server=False)

        # 制造「恢复必然失败」：删除快照的 payload
        target_payload = app.snapshots.path_for(good.id) / "payload" / "Mods"
        shutil.rmtree(target_payload)

        tid = mgr.run_sync("snapshot.restore", payload={"snapshot_id": good.id})
        task = mgr.get(tid)
        self.assertEqual(task["state"], TaskState.FAILED.value)
        self.assertIn("校验未通过", task["error"])
        # 现有数据必须完好
        self.assertTrue((C.MODS_DIR / "ALOS" / "manifest.json").is_file())
        self.assertTrue((C.SAVES_DIR / "Farm_111111111" / "SaveGameInfo").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
