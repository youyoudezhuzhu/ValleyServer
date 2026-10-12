"""后台监管：NAS 重启自动启动、异常退出自动恢复（含重试上限与退避）、定时快照、磁盘告警。

「异常退出自动恢复」的重试语义：
  * 只在**非用户主动停止**的情况下自动拉起；
  * 连续失败次数超过 `policy.start_retry_max` 后停止自动拉起并生成告警事件；
  * 退避时间按 `start_retry_backoff_seconds * 2^(n-1)` 增长，封顶 10 分钟。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from mgr import config as C
from mgr.logmod import get_logger, trim_log

LOG = get_logger("manager")


class Supervisor:
    def __init__(self, app) -> None:
        self.app = app
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._last_auto_snapshot = 0.0
        self._user_stopped = False
        # 自动开服（载档）的并发保护与失败计数
        self._host_lock = threading.Lock()
        self._host_running = False
        self._host_failures: dict[str, int] = {}
        self._last_phase = ""
        self._last_down_since = 0.0
        self.state: dict[str, Any] = {"phase": "未启动", "detail": ""}

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        cfg = C.load_config()
        if not cfg.policy.supervisor_enabled:
            LOG.info("后台监管已在策略中关闭")
            return
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()
        LOG.info("后台监管线程已启动")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def note_user_stop(self) -> None:
        """用户在界面上主动停止服务器：重置重试计数，不自动拉起。"""
        self._user_stopped = True
        self._failures = 0

    def note_user_start(self) -> None:
        self._user_stopped = False
        # 自动开服（载档）的并发保护与失败计数
        self._host_lock = threading.Lock()
        self._host_running = False
        self._host_failures: dict[str, int] = {}
        self._failures = 0

    # ── 主循环 ──────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        # 启动延迟：避免与 fnOS 应用中心启动流程抢资源
        if self._stop.wait(25):
            return
        self._boot_recovery()
        counter = 0
        while not self._stop.wait(10):
            counter += 1
            try:
                self._tick()
                if counter % 6 == 0:   # 约每分钟
                    trim_log(C.LOG_DIR / "manager.log")
                    self._periodic_tasks()
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"监管循环异常：{exc}")

    def _boot_recovery(self) -> None:
        """NAS 重启后：如果策略允许且之前是运行状态，自动拉起。"""
        cfg = C.load_config()
        if not cfg.policy.auto_start_with_nas:
            LOG.info("策略未开启「随 NAS 启动」，跳过开机自启")
            return
        st = self.app.runtime.status()
        if not st.exists:
            LOG.info("尚未放入游戏本体，跳过开机自启")
            self.state = {"phase": "未安装", "detail": ""}
            return
        if st.running:
            LOG.info("游戏已在运行，等待健康检查")
            self.state = self.app.runtime.readiness()
            try:
                self.app.versions.record(self.app.versions.detect_runtime(), note="开机检测")
            except Exception as exc:  # noqa: BLE001
                LOG.warn(f"记录运行版本失败：{exc}")
            return
        LOG.info("检测到游戏未运行，尝试自动启动（NAS 重启恢复）")
        self._auto_start("NAS 启动后自动恢复")

    def _tick(self) -> None:
        cfg = C.load_config()
        st = self.app.runtime.status()
        ready = self.app.runtime.readiness()
        self.state = ready
        phase = ready.get("phase", "")
        if phase != self._last_phase:
            LOG.info(f"服务器状态：{phase}（{ready.get('detail', '')}）")
            self.app.db.event("info", "supervisor", f"状态变为 {phase}")
            self._last_phase = phase

        if not st.exists or st.running:
            if st.running:
                self._last_down_since = 0.0
            return

        # 游戏不在运行
        if self._user_stopped:
            return
        if not cfg.policy.supervisor_enabled:
            return
        if phase == "启动失败":
            self._maybe_auto_start("健康检查失败", cfg)
            return
        now = time.time()
        if self._last_down_since == 0.0:
            self._last_down_since = now
            return
        if now - self._last_down_since < 5:
            return
        self._maybe_auto_start(f"游戏进程异常退出（{st.status}，退出码 {st.exit_code}）", cfg)

    def _maybe_auto_start(self, reason: str, cfg: C.ManagerConfig) -> None:
        max_retry = max(0, int(cfg.policy.start_retry_max))
        if self._failures >= max_retry:
            if self._failures == max_retry:
                message = (f"自动恢复已停止：连续失败 {self._failures} 次，"
                           f"达到上限 {max_retry}。请人工检查日志。")
                LOG.error(message)
                self.app.db.event("error", "supervisor", message)
                self._failures += 1  # 保证只告警一次
            return
        backoff = min(600, max(5, cfg.policy.start_retry_backoff_seconds) * (2 ** self._failures))
        wait = backoff - (time.time() - self._last_down_since) if self._last_down_since else 0
        if wait > 0:
            return
        self._auto_start(reason)
        self._failures += 1
        self._last_down_since = time.time()

    def _schedule_auto_host(self, reason: str) -> None:
        """等 SMAPI 就绪后自动载档开服（后台线程，不阻塞监管循环）。"""
        with self._host_lock:
            if self._host_running:
                LOG.info("自动开服已在进行中，跳过重复调度")
                return
            self._host_running = True

        def worker():
            try:
                # 等 SMAPI 加载完成（最长 5 分钟），再交给共享实现
                deadline = time.time() + 300
                while time.time() < deadline and not self._stop.is_set():
                    if self.app.runtime.readiness().get("layers", {}).get("L3_smapi_loaded"):
                        break
                    self._stop.wait(5)
                else:
                    LOG.warn("等待 SMAPI 就绪超时，跳过自动开服；可在「总览」页点「一键开服」")
                    return
                from orchestration import auto_host_load_save
                status = auto_host_load_save(
                    self.app.runtime,
                    lambda m: self._host_log(m),
                    C.load_config(),
                    failures=self._host_failures,
                )
                if status == "hosted":
                    self.app.db.event("info", "supervisor",
                                      f"自动恢复后已载档开服（触发原因：{reason}）")
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"自动开服异常：{exc}")
            finally:
                with self._host_lock:
                    self._host_running = False

        threading.Thread(target=worker, name="auto-host", daemon=True).start()

    def _host_log(self, message: str) -> None:
        LOG.info(f"[自动开服] {message}")
        try:
            self.app.db.event("info", "supervisor", f"自动开服：{message}")
        except Exception:  # noqa: BLE001
            pass

    def _auto_start(self, reason: str) -> None:
        LOG.warn(f"自动恢复：{reason}")
        self.app.db.event("warn", "supervisor", f"自动恢复触发：{reason}")
        try:
            cfg = C.load_config()
            self.app.runtime.prepare(cfg)
            res = self.app.runtime.up()
            if res.ok:
                LOG.info("自动恢复启动指令已下发")
                # 重置下探起点，让健康轮询接管
                self._last_down_since = 0.0
                # ⚠️ 启动之后必须补上"载档开服"这一步：
                # ALOS 重启后不会自己载档，否则会出现"进程在跑但不开服"的假运行状态。
                # 排查发现：这条路径（NAS 开机自启 / 崩溃恢复）此前直接调 runtime.up()，
                # 绕过了 server.start 任务里的自动载档，于是"无人值守"在重启后失效。
                self._schedule_auto_host(reason)
            else:
                LOG.error(f"自动恢复启动失败：{res.tail[:400]}")
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"自动恢复异常：{exc}")

    def _periodic_tasks(self) -> None:
        cfg = C.load_config()
        # 定时快照
        if cfg.policy.auto_snapshot_enabled:
            interval = max(30, int(cfg.policy.auto_snapshot_interval_minutes)) * 60
            now = time.time()
            if self._last_auto_snapshot == 0.0:
                self._last_auto_snapshot = now
            elif now - self._last_auto_snapshot >= interval:
                self._last_auto_snapshot = now
                self._schedule_snapshot()
        # 磁盘告警
        try:
            from mgr.util import free_mb
            free = free_mb(C.DATA_ROOT)
            if free < cfg.policy.min_free_space_mb:
                self.app.db.event(
                    "error", "disk",
                    f"磁盘可用空间不足：{free} MiB（策略下限 {cfg.policy.min_free_space_mb} MiB）",
                )
        except OSError:
            pass

    def _schedule_snapshot(self) -> None:
        """定时快照走统一任务队列（不与其它高风险任务并发）。"""
        try:
            from mgr.tasks import get_manager
            mgr = get_manager()
            conflict = mgr.active_conflict("snapshot.create")
            if conflict:
                LOG.info(f"已有高风险任务在跑（{conflict['id']} / {conflict['kind']}），"
                         "本次定时快照推迟")
                return
            mgr.submit("snapshot.create", title="定时自动快照", operator="system",
                       payload={"kind": "auto", "reason": "schedule",
                                "note": "由后台定时策略触发"})
            LOG.info("已排入定时快照任务")
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"排入定时快照失败：{exc}")
