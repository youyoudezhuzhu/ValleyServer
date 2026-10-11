"""统一任务队列测试：状态机、互斥、失败回滚、崩溃残留处理。

覆盖项目要求 §9.9 / §9.10 / §9.12：
  测试 9：快照失败时原操作必须取消。
  测试 10：更新失败后的回滚。
  测试 12：同一时间发起两个高风险任务。
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.db import Database, reset_db  # noqa: E402
import mgr.tasks as T  # noqa: E402


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        # 每个测试独立 DB，避免相互污染
        self.db_path = C.PKGHOME / "manager" / f"tasks-{id(self)}.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self.db_path.unlink()
        self.db = reset_db(self.db_path)
        self.mgr = T.TaskManager()
        T.set_manager(self.mgr)

    def tearDown(self):
        T.set_manager(T.TaskManager())


class TestStateMachine(QueueTestBase):
    def test_success_path(self):
        def handler(ctx):
            ctx.step("做事", 50)
            ctx.check_cancelled()
            return {"value": 42}

        self.mgr.register("system.noop", handler)
        tid = self.mgr.run_sync("system.noop", title="t")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.SUCCEEDED.value)
        self.assertEqual(data["progress"], 100)
        self.assertEqual(data["result"]["value"], 42)
        self.assertTrue(data["terminal"])

    def test_failure_records_reason(self):
        def handler(ctx):
            raise T.TaskFailure("故意失败：磁盘不可写")

        self.mgr.register("system.noop", handler)
        tid = self.mgr.run_sync("system.noop")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.FAILED.value)
        self.assertIn("故意失败", data["error"])
        self.assertTrue(data["terminal"])

    def test_unexpected_exception_is_captured(self):
        def handler(ctx):
            raise ZeroDivisionError("boom")

        self.mgr.register("system.noop", handler)
        tid = self.mgr.run_sync("system.noop")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.FAILED.value)
        self.assertIn("ZeroDivisionError", data["error"])
        self.assertTrue(any("ZeroDivisionError" in (l["message"] or "")
                            for l in data["logs"]))

    def test_cancel_only_when_allowed(self):
        started = threading.Event()
        release = threading.Event()

        def handler(ctx):
            started.set()
            release.wait(10)
            ctx.check_cancelled()
            return {}

        self.mgr.register("system.noop", handler)
        tid = self.mgr.submit("system.noop", cancellable=False)
        started.wait(5)
        with self.assertRaises(T.TaskFailure):
            self.mgr.cancel(tid)
        release.set()
        time.sleep(0.3)

    def test_cancel_marks_cancelled(self):
        started = threading.Event()
        release = threading.Event()

        def handler(ctx):
            started.set()
            release.wait(10)
            ctx.check_cancelled()
            return {}

        self.mgr.register("system.noop", handler)
        tid = self.mgr.submit("system.noop", cancellable=True)
        started.wait(5)
        self.mgr.cancel(tid)
        release.set()
        for _ in range(50):
            data = self.mgr.get(tid)
            if data["state"] in (T.TaskState.CANCELLED.value, T.TaskState.FAILED.value):
                break
            time.sleep(0.1)
        self.assertEqual(data["state"], T.TaskState.CANCELLED.value)
        self.assertIn("取消", (data["error"] or "") + data["step"])

    def test_rollback_on_failure(self):
        """变更已发生 + 失败 + 回滚成功 → 终态是 rolled_back（不是 failed）。"""
        rolled: list[str] = []

        def handler(ctx):
            ctx.set_result(target="x")
            ctx.mark_mutated("已完成变更")     # 声明"确实改了东西"
            raise T.TaskFailure("失败触发回滚")

        def rollback(ctx, result):
            rolled.append(result.get("target", ""))

        self.mgr.register("server.update", handler, rollback=rollback)
        tid = self.mgr.run_sync("server.update")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.ROLLED_BACK.value)
        self.assertEqual(data["rollback_state"], "succeeded")
        self.assertEqual(rolled, ["x"])
        # 原始失败原因必须仍然保留
        self.assertIn("失败触发回滚", data["error"])

    def test_no_mutation_means_no_rollback(self):
        """什么都没改就失败（例如快照失败）→ 保持 failed，不得误走回滚。"""
        called: list[bool] = []

        def handler(ctx):
            raise T.TaskFailure("变更前快照创建失败，已中止本操作")

        def rollback(ctx, result):
            called.append(True)

        self.mgr.register("server.update", handler, rollback=rollback)
        tid = self.mgr.run_sync("server.update")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.FAILED.value)
        self.assertEqual(called, [], "未做任何变更却执行了回滚")
        self.assertFalse(data["rollback_state"])
        self.assertIn("快照创建失败", data["error"])
        self.assertTrue(any("无需回滚" in (l["message"] or "") for l in data["logs"]))

    def test_rollback_failure_is_recorded_and_scene_kept(self):
        def handler(ctx):
            ctx.mark_mutated("已完成变更")
            raise T.TaskFailure("主操作失败")

        def rollback(ctx, result):
            raise T.TaskFailure("回滚也失败了")

        self.mgr.register("server.update", handler, rollback=rollback)
        tid = self.mgr.run_sync("server.update")
        data = self.mgr.get(tid)
        self.assertEqual(data["state"], T.TaskState.ROLLBACK_FAILED.value)
        self.assertIn("回滚也失败", data["rollback_error"])

    def test_skip_rollback_flag(self):
        called: list[bool] = []

        def handler(ctx):
            ctx.mark_mutated("已完成变更")
            raise T.TaskFailure("x")

        def rollback(ctx, result):
            called.append(True)

        self.mgr.register("server.update", handler, rollback=rollback)
        self.mgr.run_sync("server.update", payload={"skip_rollback": True})
        self.assertEqual(called, [])

    def test_unknown_kind_refused(self):
        with self.assertRaises(T.TaskFailure):
            self.mgr.submit("not.registered")


class TestMutualExclusion(QueueTestBase):
    def test_two_high_risk_tasks_serialize(self):
        order: list[str] = []
        lock = threading.Lock()

        def handler(ctx):
            with lock:
                order.append(f"enter:{ctx.payload.get('tag')}")
            time.sleep(0.5)
            with lock:
                order.append(f"exit:{ctx.payload.get('tag')}")
            return {}

        self.mgr.register("server.restart", handler)
        t1 = self.mgr.submit("server.restart", payload={"tag": "A"})
        time.sleep(0.15)
        t2 = self.mgr.submit("server.restart", payload={"tag": "B"})
        for _ in range(60):
            if all(self.mgr.get(t)["terminal"] for t in (t1, t2)):
                break
            time.sleep(0.1)
        # 必须是严格串行：enter A → exit A → enter B → exit B（或反序）
        self.assertEqual(len(order), 4)
        self.assertEqual(order[0], "enter:" + order[0].split(":")[1])
        self.assertEqual(order[0].split(":")[1], order[1].split(":")[1])
        self.assertNotEqual(order[0].split(":")[1], order[2].split(":")[1])

    def test_conflict_detection(self):
        started = threading.Event()
        release = threading.Event()

        def handler(ctx):
            started.set()
            release.wait(10)
            return {}

        self.mgr.register("mod.install", handler)
        tid = self.mgr.submit("mod.install")
        started.wait(5)
        conflict = self.mgr.active_conflict("mod.remove")
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["id"], tid)
        release.set()
        time.sleep(0.4)
        self.assertIsNone(self.mgr.active_conflict("mod.remove"))

    def test_low_risk_not_blocked(self):
        started = threading.Event()
        release = threading.Event()

        def slow(ctx):
            started.set()
            release.wait(10)
            return {}

        def quick(ctx):
            return {"ok": True}

        self.mgr.register("mod.install", slow)
        self.mgr.register("system.noop", quick)
        self.mgr.submit("mod.install")
        started.wait(5)
        tid = self.mgr.run_sync("system.noop")
        self.assertEqual(self.mgr.get(tid)["state"], T.TaskState.SUCCEEDED.value)
        release.set()
        time.sleep(0.3)


class TestCrashRecovery(QueueTestBase):
    def test_interrupted_tasks_reaped_on_start(self):
        self.db.execute(
            "INSERT INTO tasks(id,kind,title,operator,state,progress,step,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("stale1", "server.update", "旧任务", "admin",
             T.TaskState.RUNNING.value, 40, "执行中", time.time()),
        )
        self.db.execute(
            "INSERT INTO tasks(id,kind,title,operator,state,progress,step,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("stale2", "snapshot.create", "旧快照任务", "system",
             T.TaskState.ROLLING_BACK.value, 60, "回滚中", time.time()),
        )
        mgr = T.TaskManager()   # 构造时触发 reap
        for tid in ("stale1", "stale2"):
            data = mgr.get(tid, with_logs=False)
            self.assertEqual(data["state"], T.TaskState.INTERRUPTED.value)
            self.assertIn("中断", data["error"])

    def test_completed_tasks_not_touched(self):
        self.db.execute(
            "INSERT INTO tasks(id,kind,title,operator,state,progress,step,created_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("done1", "server.update", "完成任务", "admin",
             T.TaskState.SUCCEEDED.value, 100, "已完成", time.time(), time.time()),
        )
        T.TaskManager()
        self.assertEqual(self.db.one("SELECT state FROM tasks WHERE id='done1'")["state"],
                         T.TaskState.SUCCEEDED.value)


class TestQueries(QueueTestBase):
    def test_list_and_filter(self):
        self.mgr.register("system.noop", lambda ctx: {})
        self.mgr.run_sync("system.noop", title="一")
        self.mgr.run_sync("system.noop", title="二")
        all_tasks = self.mgr.list(limit=10)
        self.assertEqual(len(all_tasks), 2)
        succeeded = self.mgr.list(state=T.TaskState.SUCCEEDED.value)
        self.assertEqual(len(succeeded), 2)
        self.assertEqual(self.mgr.list(state="failed"), [])
        self.assertEqual(len(self.mgr.list(kind="system.noop")), 2)

    def test_stats(self):
        self.mgr.register("system.noop", lambda ctx: {})
        self.mgr.run_sync("system.noop")
        stats = self.mgr.stats()
        self.assertEqual(stats["counts"].get(T.TaskState.SUCCEEDED.value), 1)

    def test_logs_recorded(self):
        def handler(ctx):
            ctx.log("第一步")
            ctx.step("阶段二", 50)
            return {}

        self.mgr.register("system.noop", handler)
        tid = self.mgr.run_sync("system.noop")
        data = self.mgr.get(tid)
        messages = [l["message"] for l in data["logs"]]
        self.assertTrue(any("第一步" in m for m in messages))
        self.assertTrue(any("阶段二" in m for m in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
