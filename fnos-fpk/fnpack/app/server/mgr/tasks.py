"""统一任务队列：状态机 + 互斥调度 + 进度/日志 + 回滚语义。

事务规则（见项目要求第六节）：
  * 所有安装/更新/恢复/存档切换/MOD 变更都必须经过这里执行。
  * 状态机：pending → running → succeeded | failed | cancelled
                    running → rolling_back → rolled_back | rollback_failed
  * 同一服务器禁止并发执行相互冲突的高风险任务（exclusive 互斥）。
  * 每个失败路径都必须显式落库，禁止"快照失败后继续"。
  * 进程崩溃后残留的 running 任务在启动时被标记为 interrupted，不会被当成成功。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import config as C
from .db import get_db


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = {
    TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
    TaskState.ROLLED_BACK, TaskState.ROLLBACK_FAILED, TaskState.INTERRUPTED,
}
# 是否"高风险"：需要与其它高风险任务互斥
HIGH_RISK_KINDS = {
    "server.install", "server.start", "server.stop", "server.restart", "server.recreate",
    "save.import", "save.delete", "save.activate", "save.restore", "save.export",
    "mod.install", "mod.update", "mod.remove", "mod.disable", "mod.enable", "mod.overwrite",
    "snapshot.create", "snapshot.restore", "snapshot.delete", "snapshot.verify",
    "config.apply", "upstream.update", "diagnostics.bundle",
    "native.install-gamefiles", "native.install-smapi", "native.steam-fetch",
    "version.record",
}

KIND_LABELS = {
    "server.install": "安装 / 首次启动",
    "server.start": "启动服务器",
    "server.stop": "停止服务器",
    "server.restart": "重启服务器",
    "server.recreate": "受控重建运行环境",
    "native.install-gamefiles": "安装游戏本体（原生部署）",
    "native.install-smapi": "获取 / 更新 SMAPI",
    "native.steam-fetch": "用 Steam 扫码获取 / 更新游戏本体",
    "version.record": "记录版本指纹",
    "save.import": "导入存档",
    "save.export": "导出存档",
    "save.delete": "删除存档",
    "save.activate": "切换当前存档",
    "save.restore": "从快照恢复存档",
    "mod.install": "安装 MOD",
    "mod.update": "更新 MOD",
    "mod.remove": "卸载 MOD",
    "mod.disable": "禁用 MOD",
    "mod.enable": "启用 MOD",
    "mod.overwrite": "覆盖安装 MOD",
    "snapshot.create": "创建快照",
    "snapshot.restore": "从快照恢复",
    "snapshot.delete": "删除快照",
    "snapshot.verify": "校验快照",
    "config.apply": "应用配置变更",
    "upstream.update": "同步上游代码",
    "diagnostics.bundle": "生成诊断包",
    "system.noop": "系统检查",
}


class TaskFailure(RuntimeError):
    """任务执行中的可预期失败，message 会展示给用户。"""

    def __init__(self, message: str, *, rollback_hint: str = ""):
        super().__init__(message)
        self.rollback_hint = rollback_hint


class TaskCancelled(TaskFailure):
    pass


# 全局互斥：同一服务器的高风险任务串行执行
_server_locks: dict[str, threading.Lock] = {}
_server_locks_guard = threading.Lock()


def _server_lock(server_id: str) -> threading.Lock:
    with _server_locks_guard:
        lock = _server_locks.get(server_id)
        if lock is None:
            lock = threading.Lock()
            _server_locks[server_id] = lock
        return lock


@dataclass
class TaskContext:
    """传给任务函数的上下文，提供进度、日志、取消检查与子步骤。"""

    task_id: str
    kind: str
    payload: dict[str, Any]
    server_id: str = "default"
    _cancel: threading.Event = field(default_factory=threading.Event)
    _progress: int = 0
    _step: str = ""
    _result: dict[str, Any] = field(default_factory=dict)
    # 只有真正产生了变更，失败时才需要回滚。
    # 否则「快照失败/前置校验失败」这类"什么都没做"的失败会被错误地走一遍回滚，
    # 把任务状态从 failed 变成 rollback_failed，掩盖真实原因。
    _mutated: bool = False

    # ── 进度与日志 ──────────────────────────────────────────────────────────
    def log(self, message: str, level: str = "info") -> None:
        manager = get_manager()
        manager._append_log(self.task_id, level, message)

    def step(self, message: str, progress: int | None = None) -> None:
        """声明当前步骤；progress 为 0-99，完成时由调度器置 100。"""
        self._step = message
        if progress is not None:
            self._progress = max(0, min(99, int(progress)))
        manager = get_manager()
        manager._update_progress(self.task_id, self._progress, message)
        manager._append_log(self.task_id, "info", message)

    def set_result(self, **kwargs: Any) -> None:
        self._result.update(kwargs)

    @property
    def cancelled(self) -> bool:
        """是否已被请求取消。

        长任务（例如 Steam 下载）必须周期性检查它并主动退出 ——
        否则「取消任务」只会把状态标成 cancelled，进程还在后台跑。
        """
        return self._cancel.is_set()

    def mark_mutated(self, note: str = "") -> None:
        """声明「已经产生了需要回滚的变更」。此后失败才会触发回滚。"""
        self._mutated = True
        if note:
            self.log(f"变更已发生，失败时将回滚：{note}")

    @property
    def result(self) -> dict[str, Any]:
        return dict(self._result)

    # ── 取消 ────────────────────────────────────────────────────────────────
    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise TaskCancelled("任务已被用户取消")

    def request_cancel(self) -> None:
        self._cancel.set()


class TaskManager:
    def __init__(self, max_workers: int = 4):
        self.db = get_db()
        self.max_workers = max_workers
        self._handlers: dict[str, Callable[[TaskContext], dict[str, Any] | None]] = {}
        self._rollback_handlers: dict[str, Callable[[TaskContext, dict[str, Any]], None]] = {}
        self._contexts: dict[str, TaskContext] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._reap_interrupted()

    # ── 注册 ────────────────────────────────────────────────────────────────
    def register(self, kind: str,
                 handler: Callable[[TaskContext], dict[str, Any] | None],
                 rollback: Callable[[TaskContext, dict[str, Any]], None] | None = None) -> None:
        self._handlers[kind] = handler
        if rollback is not None:
            self._rollback_handlers[kind] = rollback

    def known_kinds(self) -> list[str]:
        return sorted(self._handlers)

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def _reap_interrupted(self) -> None:
        """启动时把崩溃残留的 running/rolling_back 任务标记为 interrupted。"""
        rows = self.db.query(
            "SELECT id, state FROM tasks WHERE state IN (?,?)",
            (TaskState.RUNNING.value, TaskState.ROLLING_BACK.value),
        )
        for row in rows:
            self.db.execute(
                "UPDATE tasks SET state=?, error=?, finished_at=? WHERE id=?",
                (TaskState.INTERRUPTED.value,
                 "管理器进程在任务执行期间退出，任务结果未知，已标记为中断；请检查现场后重试。",
                 time.time(), row["id"]),
            )
            self.db.event("warn", "task",
                          f"任务 {row['id']} 因管理器重启被标记为 interrupted（原状态 {row['state']}）")

    def active_conflict(self, kind: str, server_id: str = "default") -> dict[str, Any] | None:
        """若存在冲突的在跑高风险任务，返回该任务摘要。"""
        if kind not in HIGH_RISK_KINDS:
            return None
        rows = self.db.query(
            "SELECT id, kind, title, state, started_at FROM tasks "
            "WHERE server_id=? AND state IN (?,?) ORDER BY started_at ASC",
            (server_id, TaskState.RUNNING.value, TaskState.ROLLING_BACK.value),
        )
        for row in rows:
            if row["kind"] in HIGH_RISK_KINDS:
                return dict(row)
        return None

    def submit(self, kind: str, *, title: str = "", operator: str = "admin",
               payload: dict[str, Any] | None = None, server_id: str = "default",
               needs_stop: bool = False, snapshot_id: str | None = None,
               cancellable: bool = False, run_async: bool = True) -> str:
        if kind not in self._handlers:
            raise TaskFailure(f"未知任务类型：{kind}")
        task_id = uuid.uuid4().hex[:16]
        db = self.db
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO tasks(id,kind,title,operator,state,progress,step,needs_stop,"
                "snapshot_id,cancellable,created_at,server_id,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, kind, title or KIND_LABELS.get(kind, kind), operator,
                 TaskState.PENDING.value, 0, "排队中", 1 if needs_stop else 0,
                 snapshot_id, 1 if cancellable else 0, time.time(), server_id,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )
        ctx = TaskContext(task_id=task_id, kind=kind, payload=payload or {}, server_id=server_id)
        with self._lock:
            self._contexts[task_id] = ctx
        self._append_log(task_id, "info", f"任务已创建：{KIND_LABELS.get(kind, kind)}")
        if run_async:
            thread = threading.Thread(target=self._run, args=(task_id,), daemon=True,
                                      name=f"task-{task_id}")
            with self._lock:
                self._threads[task_id] = thread
            thread.start()
        return task_id

    def run_sync(self, kind: str, **kwargs: Any) -> str:
        kwargs["run_async"] = False
        task_id = self.submit(kind, **kwargs)
        self._run(task_id)
        return task_id

    def _run(self, task_id: str) -> None:
        with self._lock:
            ctx = self._contexts.get(task_id)
        if ctx is None:
            return
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            return
        kind = row["kind"]
        is_high_risk = kind in HIGH_RISK_KINDS
        lock = _server_lock(ctx.server_id)
        acquired = False
        try:
            if is_high_risk:
                # 非阻塞试探：若已有高风险任务在跑，则排队等待（串行化）
                acquired = lock.acquire(timeout=3600)
                if not acquired:
                    raise TaskFailure("等待其它高风险任务完成超时（1 小时）")
            self.db.execute(
                "UPDATE tasks SET state=?, started_at=?, step=? WHERE id=?",
                (TaskState.RUNNING.value, time.time(), "开始执行", task_id),
            )
            self._append_log(task_id, "info", "开始执行")
            handler = self._handlers[kind]
            result = handler(ctx) or {}
            if ctx._cancel.is_set():
                raise TaskCancelled("任务在完成前被取消")
            merged = dict(ctx.result)
            merged.update(result or {})
            self.db.execute(
                "UPDATE tasks SET state=?, progress=100, step=?, finished_at=?, result=? WHERE id=?",
                (TaskState.SUCCEEDED.value, "已完成", time.time(),
                 json.dumps(merged, ensure_ascii=False), task_id),
            )
            self._append_log(task_id, "info", "任务成功完成")
        except TaskCancelled as exc:
            self.db.execute(
                "UPDATE tasks SET state=?, step=?, finished_at=?, error=? WHERE id=?",
                (TaskState.CANCELLED.value, "已取消", time.time(), str(exc), task_id),
            )
            self._append_log(task_id, "warn", f"任务已取消：{exc}")
        except TaskFailure as exc:
            self._fail(task_id, ctx, str(exc), rollback_hint=exc.rollback_hint)
        except BaseException as exc:  # noqa: BLE001 - 必须记录所有异常
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._append_log(task_id, "error", detail)
            self._fail(task_id, ctx, f"{type(exc).__name__}: {exc}")
        finally:
            if acquired:
                try:
                    lock.release()
                except RuntimeError:
                    pass
            with self._lock:
                self._threads.pop(task_id, None)
                self._contexts.pop(task_id, None)

    def _fail(self, task_id: str, ctx: TaskContext, message: str, *,
              rollback_hint: str = "") -> None:
        """失败路径：写入失败原因，并按需触发回滚。"""
        self.db.execute(
            "UPDATE tasks SET state=?, step=?, finished_at=?, error=? WHERE id=?",
            (TaskState.FAILED.value, "失败", time.time(), message[:4000], task_id),
        )
        self._append_log(task_id, "error", f"任务失败：{message}")
        rollback = self._rollback_handlers.get(ctx.kind)
        if rollback is None:
            return
        if ctx.payload.get("skip_rollback"):
            self._append_log(task_id, "warn", "按请求跳过回滚（skip_rollback=true）")
            return
        if not ctx._mutated:
            # 任务在产生任何变更之前就失败了（例如快照失败、前置校验失败）——
            # 没有东西需要回滚，保持 failed 与真实原因不变。
            self._append_log(
                task_id, "info",
                "任务在产生任何变更之前失败，无需回滚（快照/前置校验失败属于这种情况）")
            return
        self.db.execute(
            "UPDATE tasks SET state=?, rollback_state=? WHERE id=?",
            (TaskState.ROLLING_BACK.value, "running", task_id),
        )
        self._append_log(task_id, "warn", "开始回滚")
        try:
            rollback(ctx, ctx.result)
            # 回滚成功后状态是 rolled_back（不是 failed）——让界面和任务历史如实反映
            # 「变更已撤销、数据已回到操作前」。真实原因仍保留在 error 字段里。
            self.db.execute(
                "UPDATE tasks SET state=?, rollback_state=?, rollback_error=? WHERE id=?",
                (TaskState.ROLLED_BACK.value, "succeeded", "", task_id),
            )
            self._append_log(
                task_id, "info",
                "回滚完成，数据已回到操作前状态；原始失败原因保留在 error 字段，可人工核查")
            if rollback_hint:
                self._append_log(task_id, "warn", rollback_hint)
        except BaseException as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            self.db.execute(
                "UPDATE tasks SET state=?, rollback_state=?, rollback_error=? WHERE id=?",
                (TaskState.ROLLBACK_FAILED.value, "failed", detail[:4000], task_id),
            )
            self._append_log(task_id, "error",
                             f"回滚失败，已保留故障现场，请勿清理：{detail}")

    # ── 进度/日志落库 ───────────────────────────────────────────────────────
    def _update_progress(self, task_id: str, progress: int, step: str) -> None:
        self.db.execute("UPDATE tasks SET progress=?, step=? WHERE id=?",
                        (progress, step[:200], task_id))

    def _append_log(self, task_id: str, level: str, message: str) -> None:
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO task_logs(task_id,ts,level,message) VALUES(?,?,?,?)",
                    (task_id, time.time(), level[:16], message[:4000]),
                )
        except Exception:  # noqa: BLE001 - 日志失败不能影响任务
            pass

    # ── 查询 ────────────────────────────────────────────────────────────────
    def get(self, task_id: str, *, with_logs: bool = True, log_limit: int = 500) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            return None
        data = dict(row)
        if data.get("payload"):
            try:
                data["payload"] = json.loads(data["payload"])
            except json.JSONDecodeError:
                data["payload"] = {}
        if data.get("result"):
            try:
                data["result"] = json.loads(data["result"])
            except json.JSONDecodeError:
                data["result"] = {}
        data["kind_label"] = KIND_LABELS.get(data["kind"], data["kind"])
        data["terminal"] = data["state"] in {s.value for s in TERMINAL_STATES}
        if with_logs:
            rows = self.db.query(
                "SELECT ts,level,message FROM task_logs WHERE task_id=? ORDER BY id ASC LIMIT ?",
                (task_id, int(log_limit)),
            )
            data["logs"] = [dict(r) for r in rows]
        return data

    def list(self, *, limit: int = 100, state: str | None = None,
             kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        where = []
        if state:
            where.append("state=?")
            params.append(state)
        if kind:
            where.append("kind=?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self.db.query(sql, tuple(params)):
            d = dict(row)
            d.pop("payload", None)
            if d.get("result"):
                try:
                    d["result"] = json.loads(d["result"])
                except json.JSONDecodeError:
                    d["result"] = {}
            d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
            d["terminal"] = d["state"] in {s.value for s in TERMINAL_STATES}
            out.append(d)
        return out

    def cancel(self, task_id: str, *, actor: str = "admin") -> bool:
        row = self.db.one("SELECT id,state,cancellable FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise TaskFailure("任务不存在")
        if row["state"] in {s.value for s in TERMINAL_STATES}:
            raise TaskFailure(f"任务已处于终态（{row['state']}），无法取消")
        if not row["cancellable"]:
            raise TaskFailure("该任务不允许取消（涉及数据一致性，请等待其结束）")
        with self._lock:
            ctx = self._contexts.get(task_id)
        if ctx is None:
            raise TaskFailure("任务上下文已不存在，无法取消")
        ctx.request_cancel()
        self._append_log(task_id, "warn", f"用户 {actor} 请求取消任务")
        return True

    def stats(self) -> dict[str, Any]:
        rows = self.db.query("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
        counts = {r["state"]: r["n"] for r in rows}
        running = self.db.query(
            "SELECT id,kind,title,progress,step,started_at FROM tasks WHERE state=?",
            (TaskState.RUNNING.value,),
        )
        return {
            "counts": counts,
            "running": [dict(r) for r in running],
            "queued": self.db.one(
                "SELECT COUNT(*) AS n FROM tasks WHERE state=?", (TaskState.PENDING.value,)
            )["n"],
        }


_manager: TaskManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> TaskManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TaskManager()
    return _manager


def set_manager(manager: TaskManager) -> None:
    global _manager
    _manager = manager
