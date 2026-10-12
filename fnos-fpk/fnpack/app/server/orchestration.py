"""任务编排：把业务动作注册成统一任务队列的处理器。

每个高风险动作都遵循同一套骨架（见 `_guarded`）：
  1. 校验前提（依赖、磁盘、运行状态、目标是否合法）
  2. 若需要，先创建并**校验**快照
  3. 停服 → 执行变更 → 启动 → 健康检查
  4. 失败：写入失败原因 + 触发回滚 + **保留故障现场**
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from mgr import config as C
from mgr import util
from mgr import smapi
from mgr import gamebody
from mgr import steamtools
from mgr.runtime_base import port_open, udp_port_open
from mgr.logmod import get_logger
from mgr.tasks import (HIGH_RISK_KINDS, KIND_LABELS, TaskCancelled, TaskContext,
                       TaskFailure, get_manager)
from mgr.util import ValidationError, free_mb, run

LOG = get_logger("task")


def register_all(app) -> None:
    """把所有处理器注册到全局 TaskManager。"""
    mgr = get_manager()
    ctx = _Services(app)
    mgr.register("snapshot.create", ctx.task_snapshot_create)
    mgr.register("snapshot.verify", ctx.task_snapshot_verify)
    mgr.register("snapshot.delete", ctx.task_snapshot_delete)
    mgr.register("snapshot.restore", ctx.task_snapshot_restore,
                 rollback=ctx.rollback_snapshot_restore)
    mgr.register("server.install", ctx.task_server_install)
    mgr.register("server.start", ctx.task_server_start)
    mgr.register("server.stop", ctx.task_server_stop)
    mgr.register("server.restart", ctx.task_server_restart)
    mgr.register("server.recreate", ctx.task_server_recreate)
    mgr.register("save.import", ctx.task_save_import, rollback=ctx.rollback_save_import)
    mgr.register("save.export", ctx.task_save_export)
    mgr.register("save.delete", ctx.task_save_delete)
    mgr.register("save.activate", ctx.task_save_activate)
    mgr.register("save.restore", ctx.task_save_restore)
    mgr.register("mod.install", ctx.task_mod_install, rollback=ctx.rollback_mod)
    mgr.register("mod.update", ctx.task_mod_install, rollback=ctx.rollback_mod)
    mgr.register("mod.overwrite", ctx.task_mod_install, rollback=ctx.rollback_mod)
    mgr.register("mod.remove", ctx.task_mod_remove, rollback=ctx.rollback_mod_remove)
    mgr.register("mod.disable", ctx.task_mod_toggle, rollback=ctx.rollback_mod_toggle)
    mgr.register("mod.enable", ctx.task_mod_toggle, rollback=ctx.rollback_mod_toggle)
    mgr.register("mod.install-bundle", ctx.task_mod_install_bundle)
    mgr.register("config.apply", ctx.task_config_apply, rollback=ctx.rollback_config_apply)
    mgr.register("upstream.update", ctx.task_upstream_update)
    mgr.register("version.record", ctx.task_version_record)
    mgr.register("native.install-smapi", ctx.task_native_install_smapi)
    # 注意：cancellable 是**提交**任务时的参数，不是注册时的（见 api_handlers._submit）
    mgr.register("native.steam-login", ctx.task_native_steam_login)
    mgr.register("native.steam-fetch", ctx.task_native_steam_fetch)
    mgr.register("native.host-bootstrap", ctx.task_native_host_bootstrap)
    mgr.register("native.install-gamefiles", ctx.task_native_install_gamefiles)
    mgr.register("diagnostics.bundle", ctx.task_diagnostics_bundle)
    mgr.register("system.noop", ctx.task_noop)
    LOG.info(f"已注册 {len(mgr.known_kinds())} 种任务类型")


# ── 启动后自动载档开服（共享实现）─────────────────────────────────────────
#
# 为什么必须是"共享"的：ALOS 只在「存档已加载」之后才开主机，而**重启后它不会
# 自己载档**（真机实测：重启 180s 仍停在 titleScreenGameMode，游戏端口未监听）。
# 因此凡是"由管理器把服务端启动起来"的路径都必须补上这一步，否则会留下
# "服务在跑但不开服"的假运行状态。曾经漏掉的路径（本轮排查发现）：
#   * supervisor 的 NAS 开机自启 / 崩溃自动恢复（直接调 runtime.up()）
#   * 配置变更后重启（task_config_apply）
#   * 配置回滚后的重启（rollback_config_apply）
# 等待时长（秒）。做成模块常量是为了让测试能设成很小值 ——
# 否则每个碰到"启动后自动开服"的单测都要真等 60+90 秒。
CONSOLE_CHANNEL_WAIT = 60
HOST_PORT_WAIT = 90


def _wait_seconds(env_name: str, default: float) -> float:
    """允许用环境变量覆盖等待时长。

    用环境变量而不是直接改模块常量：单测里的 `fresh_services()` 会
    `importlib.reload(orchestration)`，模块常量会被重置回源码默认值
    （实测踩到：设了 0.2 仍真等了 60 秒）。
    """
    try:
        return float(os.environ.get(env_name, default))
    except (TypeError, ValueError):
        return default


def auto_host_load_save(runtime, log, cfg=None, *, failures=None,
                        max_failures: int = 3, step=None) -> str:
    """载入最新存档并设为网络联机，返回状态字符串。

    状态：disabled / not_ready / no_save / failed / hosted / unconfirmed
    """
    cfg = cfg or C.load_config()
    step = step or (lambda _m, _p=None: None)

    if not getattr(cfg.native, "auto_host_on_start", True):
        log("已关闭「启动后自动载档开服」，跳过。可在「总览」页点「一键开服」。")
        return "disabled"

    if not runtime.readiness().get("layers", {}).get("L3_smapi_loaded"):
        log("SMAPI 尚未加载完成，跳过自动开服（可在「总览」页点「一键开服」重试）。")
        return "not_ready"

    try:
        saves = sorted([p for p in C.SAVES_DIR.iterdir() if p.is_dir()],
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        saves = []
    if not saves:
        log("⚠️ 尚未检测到存档：服务可以启动，但**不会开服**（ALOS 只在存档加载后才开主机）。")
        log("**需手动新建存档并重启服务端**：")
        log("  ① 打开 WebVNC，在游戏里新建一个存档（这样玩家名/农场名才正常）；")
        log("  ② 重启服务端 —— 管理器会自动载入最新存档并开服，之后 ALOS 接管无人值守。")
        log("（本管理器不会替你自动建存档，避免生成名字为空的存档。）")
        return "no_save"

    box = failures if failures is not None else {}
    attempts = int(box.get("n", 0))
    if attempts >= max_failures:
        log(f"⚠️ 自动开服已连续失败 {max_failures} 次，停止重试以避免崩溃循环。"
            "请到「控制台」页查看原因，修复后再点「一键开服」。")
        return "failed"

    # 等控制台通道：CommandWebUI 的 HTTP/WS 是在 `Mods loaded and ready!`
    # 之后才 bind 的，立刻发指令会 ConnectionRefused（实测踩到）。
    port_console = int(cfg.native.cmdwebui_port)
    step("等待 SMAPI 控制台通道就绪", 90)
    deadline = time.time() + _wait_seconds("VS_CONSOLE_CHANNEL_WAIT", CONSOLE_CHANNEL_WAIT)
    while time.time() < deadline:
        if port_open(port_console):
            break
        time.sleep(2)
    else:
        log(f"控制台通道（127.0.0.1:{port_console}）60s 内未就绪，跳过自动开服；"
            "可稍后在「总览」页点「一键开服」。")
        box["n"] = attempts + 1
        return "failed"
    log(f"控制台通道已就绪（127.0.0.1:{port_console}）")

    target = saves[0].name
    step(f"自动开服：载入最新存档 {target}", 92)
    runtime._host_pending = True      # 状态模型：载档指令已发、端口未就绪 → 「开服状态待确认」
    try:
        # 顺序关键：**先**设联机模式，游戏在载档时才读 Game1.multiplayerMode
        for cmd in (f"ServerCMD.set_multiplayermode 2",
                    f"ServerCMD.load_save {target}"):
            res = runtime.send_console(cmd)
            log(f"→ {cmd}（{'成功' if res.ok else '失败'}）")
            if not res.ok:
                raise TaskFailure(res.stderr or "发送失败")
            time.sleep(2)
    except Exception as exc:  # noqa: BLE001
        box["n"] = attempts + 1
        runtime._host_pending = False
        log(f"⚠️ 自动开服失败（第 {attempts + 1} 次）：{exc}")
        log("游戏可能因此退出；监管器会自动拉起。可在「总览」页点「一键开服」重试。")
        return "failed"

    port = int(cfg.native.game_port)
    deadline = time.time() + _wait_seconds("VS_HOST_PORT_WAIT", HOST_PORT_WAIT)
    while time.time() < deadline:
        # ⚠️ 游戏端口是 **UDP**：TCP 检测会永远 False，把"已开服"误判成"未就绪"
        if udp_port_open(port):
            box["n"] = 0
            runtime._host_pending = False
            log(f"✅ 已自动开服：游戏端口 {port}/UDP 正在监听，ALOS 接管无人值守。")
            return "hosted"
        time.sleep(3)
    box["n"] = attempts + 1
    log(f"⚠️ 已发送载档指令，但 {port} 未在 90s 内就绪（第 {attempts + 1} 次）。"
        "请到「控制台」页核对 ServerCMD 的输出。")
    return "unconfirmed"


class _Services:
    def __init__(self, app) -> None:
        self.app = app
        # 自动开服的连续失败计数（防止"崩溃→重启→再崩溃"的死循环）
        self._auto_host_attempts: dict[str, int] = {}
        self.cfg = C.load_config()
        self.runtime = app.runtime
        self.snapshots = app.snapshots
        self.saves = app.saves
        self.mods = app.mods
        self.versions = app.versions
        self.diag = app.diagnostics
        self.db = app.db

    # ── 公共工具 ────────────────────────────────────────────────────────────
    def reload_cfg(self) -> C.ManagerConfig:
        self.cfg = C.load_config()
        return self.cfg

    def _guard_space(self, ctx: TaskContext, *, need_mb: int = 0) -> None:
        cfg = self.reload_cfg()
        required = max(need_mb, cfg.policy.snapshot_space_estimate_mb)
        available = free_mb(C.DATA_ROOT)
        if available < required:
            raise TaskFailure(
                f"磁盘空间不足：剩余 {available} MiB，本操作需要至少 {required} MiB。"
                "请清理旧快照或扩容后再试。"
            )
        ctx.log(f"磁盘可用 {available} MiB，满足要求（≥{required} MiB）")

    def _nothing_to_protect(self) -> str:
        """判断当前是否"没有任何可保护的内容"（首次安装的常态）。

        返回空串表示确有不空的内容需要保护；否则返回说明文字。
        为什么需要：全新安装时 `Saves/` 与 `Mods/` 都是空的，快照清单必然为空，
        而空清单会被校验拒绝 —— 结果**首次安装永远走不下去**（实测踩到）。
        这种情况没有数据可丢，跳过快照是安全的；真正有数据时仍然强制快照。
        """
        for path, label in ((C.SAVES_DIR, "存档"), (C.MODS_DIR, "MOD")):
            if path.is_dir():
                for child in path.iterdir():
                    if child.name.startswith("."):
                        continue
                    return ""      # 有东西 → 需要保护
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        if game_root.is_dir():
            for child in game_root.iterdir():
                if child.name.startswith("."):
                    continue
                if child.name in ("Mods", "Saves"):
                    continue
                return ""          # 已有游戏本体 → 需要保护
        return "尚无存档、MOD 与游戏本体（首次安装的常态）"

    def _auto_snapshot(self, ctx: TaskContext, reason: str, *, allow_empty: bool = False) -> str:
        """高风险操作前的自动快照。返回快照 ID；失败会直接抛错终止任务。

        `allow_empty=True` 时，如果当前确实没有任何可保护的内容，则跳过快照
        （用于首次安装那类"没有旧数据可丢"的场景）。
        """
        if ctx.payload.get("skip_snapshot"):
            ctx.log("⚠️ 调用方请求跳过自动快照（skip_snapshot=true）")
            return ""
        if allow_empty:
            nothing = self._nothing_to_protect()
            if nothing:
                ctx.log(f"跳过变更前快照：{nothing}，没有可保护的内容")
                return ""
        cfg = self.reload_cfg()
        if not cfg.policy.auto_snapshot_enabled and reason != "manual":
            ctx.log("自动快照已在策略中关闭，跳过")
            return ""
        ctx.step(f"创建变更前快照（{reason}）", 10)
        try:
            info = self.snapshots.create(
                kind="auto", reason=reason, task_id=ctx.task_id,
                operator="system", version_info=self._version_info(),
                stop_server=True, on_log=ctx.log,
            )
        except Exception as exc:  # noqa: BLE001
            # 快照失败 → 绝不继续原定的高风险操作
            raise TaskFailure(
                f"变更前快照创建失败，已中止本操作（这是正确的保护行为）：{exc}"
            )
        ctx.log(f"快照 {info.id} 已创建并校验通过")
        self._record_snapshot(info)
        try:
            self.snapshots.enforce_retention(on_log=ctx.log)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"保留策略执行失败（不影响本次操作）：{exc}")
        return info.id

    def _record_snapshot(self, info) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots(id,kind,reason,task_id,created_at,size_bytes,"
            "manifest_ver,valid,validated_at,file_count,game_version,smapi_version,"
            "game_dir,mods_hash,saves_hash,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (info.id, info.kind, info.reason, info.task_id or None, info.created_at,
             info.size_bytes, info.manifest_version, 1 if info.valid else 0, time.time(),
             info.file_count, info.game_version, info.smapi_version,
             info.game_dir, info.mods_hash, info.saves_hash, info.note),
        )

    def _version_info(self) -> dict[str, Any]:
        cfg = self.reload_cfg()
        rv = self.versions.detect_runtime()
        mods_list = []
        try:
            for m in self.mods.list_mods():
                mods_list.append({
                    "dir": m.dir_name, "name": m.manifest.name,
                    "version": m.manifest.version, "unique_id": m.manifest.unique_id,
                    "enabled": m.enabled, "tree_hash": m.tree_hash,
                })
        except Exception as exc:  # noqa: BLE001
            LOG.warn(f"收集 MOD 清单失败：{exc}")
        return {
            "game_version": rv.game_version, "smapi_version": rv.smapi_version,
            "game_dir": rv.game_dir, "game_dir_mtime": rv.game_dir_mtime,
            "mods": mods_list,
        }

    def _wait_ready(self, ctx: TaskContext, *, timeout: int = 300,
                    progress_from: int = 80) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = ""
        step = 0
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            state = self.runtime.readiness()
            phase = state.get("phase", "")
            if phase != last:
                ctx.log(f"健康状态：{phase}（{state.get('detail', '')}）")
                last = phase
            if state.get("ready"):
                ctx.step("健康检查通过", 92)
                return state
            if phase == "启动失败":
                raise TaskFailure(f"启动后健康检查失败：{state.get('detail')}")
            step += 1
            pct = min(progress_from + step, 90)
            ctx.step(f"等待 SMAPI 加载完成（{int(time.monotonic() - (deadline - timeout))}s）", pct)
            time.sleep(3)
        raise TaskFailure(
            f"等待健康检查超时（{timeout}s）。请到「控制台」查看日志定位原因。"
        )

    def _verify_saves_readable(self, ctx: TaskContext) -> None:
        saves = self.saves.list_saves()
        bad = [s for s in saves if not s.valid]
        ctx.log(f"存档可读性检查：共 {len(saves)} 个存档，异常 {len(bad)} 个")
        if bad:
            raise TaskFailure(
                "存档可读性检查失败：" + "；".join(f"{s.dir_name}({'、'.join(s.problems)})"
                                                   for s in bad[:5])
            )

    # ── 快照任务 ────────────────────────────────────────────────────────────
    def task_snapshot_create(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        kind = payload.get("kind", "manual")
        reason = payload.get("reason", "manual" if kind == "manual" else "schedule")
        ctx.step("准备创建快照", 5)
        info = self.snapshots.create(
            kind=kind, reason=reason, task_id=ctx.task_id,
            note=str(payload.get("note", ""))[:500],
            operator=ctx.payload.get("operator", "admin"),
            version_info=self._version_info(), stop_server=True, on_log=ctx.log,
        )
        self._record_snapshot(info)
        ctx.step("快照已提交，执行完整性校验", 95)
        report = self.snapshots.verify(info.id, deep=True, on_log=ctx.log)
        if not report["ok"]:
            raise TaskFailure("快照创建后自检失败：" + "；".join(report["problems"][:5]))
        self.db.execute("UPDATE snapshots SET validated_at=?, valid=1 WHERE id=?",
                        (time.time(), info.id))
        try:
            self.snapshots.enforce_retention(on_log=ctx.log)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"保留策略执行失败（不影响快照有效性）：{exc}")
        ctx.set_result(snapshot=info.to_dict(), verify=report)
        return {"snapshot": info.to_dict(), "verify": report}

    def task_snapshot_verify(self, ctx: TaskContext) -> dict[str, Any]:
        sid = str(ctx.payload.get("snapshot_id", ""))
        ctx.step(f"校验快照 {sid}", 30)
        report = self.snapshots.verify(sid, deep=bool(ctx.payload.get("deep", True)),
                                       on_log=ctx.log)
        self.db.execute("UPDATE snapshots SET validated_at=?, valid=? WHERE id=?",
                        (time.time(), 1 if report["ok"] else 0, sid))
        if not report["ok"]:
            raise TaskFailure("快照校验未通过：" + "；".join(report["problems"][:5]))
        return report

    def task_snapshot_delete(self, ctx: TaskContext) -> dict[str, Any]:
        sid = str(ctx.payload.get("snapshot_id", ""))
        ctx.step(f"删除快照 {sid}", 50)
        self.snapshots.delete(sid, force=bool(ctx.payload.get("force", False)))
        self.db.execute("DELETE FROM snapshots WHERE id=?", (sid,))
        return {"deleted": sid}

    def task_snapshot_restore(self, ctx: TaskContext) -> dict[str, Any]:
        sid = str(ctx.payload.get("snapshot_id", ""))
        ctx.step("恢复前对当前状态做一次快照", 5)
        pre = ""
        if not ctx.payload.get("skip_pre_snapshot"):
            info = self.snapshots.create(
                kind="auto", reason="pre-snapshot-restore", task_id=ctx.task_id,
                version_info=self._version_info(), stop_server=True, on_log=ctx.log,
            )
            self._record_snapshot(info)
            pre = info.id
            ctx.set_result(pre_snapshot_id=pre)
        ctx.step(f"从快照 {sid} 恢复", 50)
        result = self.snapshots.restore(sid, stop_server=True, pre_snapshot_id=pre,
                                        on_log=ctx.log)
        return result

    def rollback_snapshot_restore(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        pre = str(result.get("pre_snapshot_id") or ctx.result.get("pre_snapshot_id") or "")
        if not pre:
            raise TaskFailure("没有找到恢复前的快照，无法自动回滚（现场已保留）")
        ctx.log(f"正在从恢复前快照 {pre} 回滚…")
        # 注意：SnapshotStore.restore() 本身不创建"恢复前快照"（那是 task_snapshot_restore
        # 的职责），因此这里不能传 skip_pre_snapshot —— 该参数并不存在。
        self.snapshots.restore(pre, stop_server=True, on_log=ctx.log)

    # ── 服务器控制 ──────────────────────────────────────────────────────────
    def task_server_install(self, ctx: TaskContext) -> dict[str, Any]:
        """安装 / 首次启动（原生部署）。"""
        cfg = self.reload_cfg()
        ctx.log("运行方式：原生（直接在本机运行）")
        self._guard_space(ctx, need_mb=4096)

        ctx.step("检查依赖", 5)
        det = self.runtime.detect()
        if not det.get("ready"):
            reasons = det.get("guidance") or ["依赖未就绪"]
            raise TaskFailure("依赖检查未通过：" + "；".join(str(r) for r in reasons)[:600])
        xst = det.get("xstack") or {}
        if not xst.get("ok"):
            raise TaskFailure(
                "虚拟显示组件缺失：" + ", ".join(xst.get("missing_required", []))
                + "。可在「系统」页点击「安装显示组件」自动获取（不修改系统包状态）。")
        if not det.get("game", {}).get("launcher_present"):
            raise TaskFailure(
                "尚未放入游戏本体（缺少 StardewModdingAPI）。"
                "请在「系统」页上传你自己已合法拥有的游戏目录 ZIP（含 SMAPI）。")

        ctx.step("准备运行环境", 15)
        C.ensure_dirs()
        if not cfg.native.vnc_password:
            import re as _re
            import secrets as _secrets
            for _ in range(20):
                cand = _secrets.token_urlsafe(8)[:8]
                if _re.fullmatch(r"[\x21-\x7e]{8}", cand):
                    cfg.native.vnc_password = cand
                    break
            if not cfg.native.vnc_password:
                cfg.native.vnc_password = "vs" + _secrets.token_hex(3)
            C.save_config(cfg)
            ctx.log("已生成随机 VNC 密码")

        self.runtime.prepare(cfg)
        ctx.step("启动显示栈与游戏", 60)
        up = self.runtime.up()
        if not up.ok:
            raise TaskFailure(f"启动失败：{up.tail[:800]}")
        state = self._wait_ready(ctx, timeout=600)
        self._maybe_auto_host(ctx, cfg)
        ctx.step("校验存档目录", 94)
        self._verify_saves_readable(ctx)
        rv = self.versions.detect_runtime()
        self.versions.record(rv, note="首次安装")
        ctx.log(f"运行版本：游戏 {rv.game_version or '未识别'} / SMAPI {rv.smapi_version or '未识别'}")
        return {"readiness": state, "version": rv.to_dict()}

    # ── 启动后自动开服（因为 ALOS 重启后不会自己载档）──────────────────────
    def _maybe_auto_host(self, ctx: TaskContext, cfg: C.ManagerConfig) -> str:
        """服务启动后自动载档开服（实现见模块级 auto_host_load_save）。"""
        return auto_host_load_save(
            self.runtime, ctx.log, cfg,
            failures=self._auto_host_attempts,
            step=lambda m, p=None: ctx.step(m, p if p is not None else 92),
        )

    def task_server_start(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("检查运行状态", 10)
        st = self.runtime.status()
        if st.running:
            ctx.log("游戏进程已在运行，执行健康检查")
            state = self._wait_ready(ctx, timeout=300)
            self._maybe_auto_host(ctx, C.load_config())
            return state
        if not st.exists:
            raise TaskFailure("尚未安装游戏本体，请先执行「安装 / 首次启动」")
        cfg = self.reload_cfg()
        self.runtime.prepare(cfg)
        ctx.step("启动显示栈与游戏", 40)
        res = self.runtime.start()
        if not res.ok:
            raise TaskFailure(f"启动失败：{res.tail[:800]}")
        state = self._wait_ready(ctx, timeout=420)
        self.versions.record(self.versions.detect_runtime(), note="启动后记录")
        self._maybe_auto_host(ctx, C.load_config())
        return state

    def task_server_stop(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("停止游戏与显示栈", 40)
        res = self.runtime.stop()
        if not res.ok:
            raise TaskFailure(f"停止失败：{res.tail[:500]}")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not self.runtime.status().running:
                ctx.step("已停止", 95)
                return {"stopped": True}
            time.sleep(2)
        raise TaskFailure("停止超时：游戏进程仍在运行")

    def task_server_restart(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("重启游戏与显示栈", 30)
        res = self.runtime.restart()
        if not res.ok:
            raise TaskFailure(f"重启失败：{res.tail[:800]}")
        return self._wait_ready(ctx, timeout=420)

    def task_server_recreate(self, ctx: TaskContext) -> dict[str, Any]:
        """受控重建：先快照，再彻底停掉并重新拉起显示栈与游戏。"""
        ctx.step("重建前快照", 5)
        self._auto_snapshot(ctx, "pre-config-change")
        cfg = self.reload_cfg()
        ctx.step("重新准备运行环境", 30)
        self.runtime.stop()
        self.runtime.prepare(cfg)
        ctx.step("重新启动", 50)
        up = self.runtime.start()
        if not up.ok:
            raise TaskFailure(f"重建失败：{up.tail[:800]}")
        return self._wait_ready(ctx, timeout=600)

    # ── 更新与回滚 ──────────────────────────────────────────────────────────
    def task_save_import(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        zip_path = Path(str(payload.get("zip_path", "")))
        dir_name = str(payload.get("dir_name", ""))
        overwrite = bool(payload.get("overwrite", False))
        if not zip_path.is_file():
            raise TaskFailure("上传的压缩包不存在或已被清理")
        ctx.step("导入前快照", 5)
        self._auto_snapshot(ctx, "pre-save-import")
        ctx.step("检查并导入存档", 50)
        result = self.saves.import_zip(zip_path, dir_name=dir_name, overwrite=overwrite,
                                       on_log=ctx.log)
        ctx.mark_mutated("存档已导入/覆盖")
        ctx.set_result(**result)
        return result

    def rollback_save_import(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        name = str(result.get("dir_name") or "")
        if not name:
            raise TaskFailure("无法确定要回滚的存档名（现场已保留）")
        ctx.log(f"删除本次导入的存档 {name} 以回滚（现场备份保留）")
        try:
            self.saves.delete(name, confirm=name)
        except Exception as exc:  # noqa: BLE001
            raise TaskFailure(f"回滚导入失败：{exc}")

    def task_save_export(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        dir_name = str(payload.get("dir_name", ""))
        export_dir = Path(payload.get("export_dir") or (C.STAGING_DIR / "exports"))
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = export_dir / f"{dir_name}-{stamp}.zip"
        ctx.step("打包存档", 40)
        result = self.saves.export_zip(dir_name, dest, allow_running=True)
        ctx.set_result(**result)
        return result

    def task_save_delete(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        dir_name = str(payload.get("dir_name", ""))
        ctx.step("删除前快照", 5)
        self._auto_snapshot(ctx, "pre-save-delete")
        ctx.step("删除存档", 60)
        result = self.saves.delete(dir_name, confirm=str(payload.get("confirm", "")))
        return result

    def task_save_activate(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        dir_name = str(payload.get("dir_name", ""))
        ctx.step("切换前快照", 5)
        self._auto_snapshot(ctx, "pre-save-activate")
        ctx.step("写入当前存档选择", 60)
        self.saves.set_active_save(dir_name)
        ctx.log(f"当前存档已切换为「{dir_name}」，将在下次启动游戏时生效")
        restarted = False
        if bool(payload.get("restart", False)):
            st = self.runtime.status()
            if st.running:
                ctx.step("重启游戏以加载新存档", 75)
                self.runtime.restart()
                self._wait_ready(ctx, timeout=420, progress_from=80)
                restarted = True
        return {"dir_name": dir_name, "restarted": restarted}

    def task_save_restore(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        sid = str(payload.get("snapshot_id", ""))
        dir_name = str(payload.get("dir_name", ""))
        ctx.step(f"从快照恢复存档「{dir_name}」", 30)
        result = self.saves.restore_from_snapshot(sid, dir_name, store=self.snapshots,
                                                  on_log=ctx.log)
        return result

    # ── MOD ─────────────────────────────────────────────────────────────────
    def task_mod_install(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        zip_path = Path(str(payload.get("zip_path", "")))
        if not zip_path.is_file():
            raise TaskFailure("上传的压缩包不存在或已被清理")
        ctx.step("安装前预检", 5)
        plan = self.mods.plan_install(zip_path)
        ctx.log(f"预检通过：包内 {len(plan['mods'])} 个 MOD")
        for entry in plan["mods"]:
            if entry.get("dir_conflict") or entry.get("id_conflict"):
                ctx.log(f"⚠️ 冲突提示：{entry['root']} "
                        f"(目录冲突={entry.get('dir_conflict')}, ID 冲突={entry.get('id_conflict')})")
        reason = {"mod.install": "pre-mod-install", "mod.update": "pre-mod-update",
                  "mod.overwrite": "pre-mod-overwrite"}.get(ctx.kind, "pre-mod-install")
        ctx.step("安装前快照", 10)
        self._auto_snapshot(ctx, reason)
        ctx.step("安装 MOD", 55)
        result = self.mods.install_zip(
            zip_path, dir_name=str(payload.get("dir_name", "")),
            overwrite=bool(payload.get("overwrite", ctx.kind in ("mod.update", "mod.overwrite"))),
            on_log=ctx.log,
        )
        # 只有真正装进去了才需要回滚
        ctx.mark_mutated(f"MOD {result.get('dir_name')} 已安装")
        ctx.set_result(**result)
        return result

    def rollback_mod(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        name = str(result.get("dir_name") or "")
        if not name:
            raise TaskFailure("无法确定要回滚的 MOD（现场已保留）")
        ctx.log(f"移除本次安装的 MOD 目录 {name} 以回滚")
        try:
            self.mods.remove(name, keep_private_data=False)
        except Exception as exc:  # noqa: BLE001
            raise TaskFailure(f"回滚 MOD 安装失败：{exc}")

    def task_mod_remove(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        dir_name = str(payload.get("dir_name", ""))
        ctx.step("卸载前快照（MOD 目录可能含每存档数据）", 5)
        self._auto_snapshot(ctx, "pre-mod-remove")
        ctx.step("卸载 MOD", 60)
        result = self.mods.remove(dir_name,
                                  keep_private_data=bool(payload.get("keep_private_data", True)))
        ctx.mark_mutated(f"MOD {dir_name} 已移入现场备份区")
        return result

    def rollback_mod_remove(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        moved = str(result.get("moved_to") or "")
        name = str(result.get("dir_name") or "")
        if not moved or not Path(moved).is_dir() or not name:
            raise TaskFailure("找不到被移走的 MOD 目录，无法自动回滚（现场已保留）")
        target = self.mods.mods_dir / name
        if target.exists():
            raise TaskFailure(f"目标目录已存在（{target}），拒绝覆盖式回滚")
        os.rename(moved, str(target))
        ctx.log(f"MOD {name} 已回滚恢复")

    def task_mod_toggle(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        dir_name = str(payload.get("dir_name", ""))
        enable = ctx.kind == "mod.enable"
        ctx.step("变更前快照", 5)
        self._auto_snapshot(ctx, "pre-mod-enable" if enable else "pre-mod-disable")
        ctx.step("切换 MOD 启用状态", 60)
        result = self.mods.set_enabled(dir_name, enable)
        if result.get("changed"):
            ctx.mark_mutated(f"MOD {dir_name} 启用状态已变更")
        ctx.set_result(**result)
        return result

    def rollback_mod_toggle(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        if not result.get("changed"):
            return
        name = str(result.get("dir_name") or "")
        ctx.log(f"回滚启用状态：{name}")
        self.mods.set_enabled(name, not bool(result.get("enabled")))

    def task_mod_install_bundle(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        zip_path = Path(str(payload.get("zip_path", "")))
        if not zip_path.is_file():
            raise TaskFailure("上游 MOD 包不存在或已被清理")
        ctx.step("整包安装前快照", 5)
        self._auto_snapshot(ctx, "pre-mod-install")
        ctx.step("安装 MOD 整包", 50)
        result = self.mods.install_bundle(
            zip_path, overwrite=bool(payload.get("overwrite", False)), on_log=ctx.log)
        if result.get("count"):
            ctx.mark_mutated(f"MOD 整包已安装（{result['count']} 个）")
        return result

    def task_version_record(self, ctx: TaskContext) -> dict[str, Any]:
        """把当前运行内容的内容指纹写进版本档案（只读，不改任何游戏文件）。"""
        ctx.step("读取当前版本指纹", 30)
        rv = self.versions.detect_runtime()
        if not rv.game_version and not rv.smapi_version:
            ctx.log("⚠️ 未能识别游戏/SMAPI 版本（游戏可能尚未启动过，控制台日志为空）")
        ctx.step("写入档案", 70)
        vid = self.versions.record(rv, note=ctx.payload.get("note", "手动记录"))
        ctx.log(f"已记录版本档案 #{vid}：游戏 {rv.game_version or '未识别'} / "
                f"SMAPI {rv.smapi_version or '未识别'} / MOD {rv.mod_count} 个 / "
                f"存档 {rv.save_count} 个")
        return rv.to_dict()

    # ── 原生部署：SMAPI 供给 ────────────────────────────────────────────────
    def task_native_install_smapi(self, ctx: TaskContext) -> dict[str, Any]:
        """获取并整合 SMAPI（用户只需提供游戏本体）。

        单独做成任务的价值：游戏本体上传后 SMAPI 拉取失败时，这里可以重试，
        不必重新上传 800 MB 的游戏本体。
        """
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        if not (game_root / "Stardew Valley.dll").is_file():
            raise TaskFailure(
                "还没放入游戏本体（缺少 Stardew Valley.dll）。"
                "请先在「系统」页上传你自己已合法拥有的游戏本体 ZIP。")

        payload = ctx.payload
        want = str(payload.get("version", "")).strip()
        force = bool(payload.get("force", False))

        ctx.step("检查游戏版本", 10)
        game_ver = smapi.read_game_version(game_root)
        if game_ver:
            ctx.log(f"检测到游戏版本：{game_ver}")
        else:
            ctx.log("⚠️ 未能识别游戏版本；将按最新兼容版本安装，由 SMAPI 启动时自行校验")

        have = smapi.read_smapi_version(game_root) if smapi.is_installed(game_root) else ""
        if have and not force and not want:
            ctx.log(f"当前已是 SMAPI {have}；如需更新请勾选「强制重新获取」")
            return {"skipped": True, "installed": have, "game_version": game_ver}

        # 必须先停服：正在运行的游戏会持有 StardewModdingAPI / smapi-internal，
        # 直接覆盖会拿到 `ETXTBSY: Text file busy`（实测踩到）。
        # 停服后重新拉起，用户视角就是"升级 SMAPI 顺带重启一次"。
        was_running = bool(self.runtime.status().running)
        if was_running:
            ctx.step("停止游戏（升级期间不能占用 SMAPI 文件）", 20)
            stop = self.runtime.stop()
            if not stop.ok:
                raise TaskFailure(f"升级 SMAPI 前停止游戏失败：{stop.tail[:300]}")
            if self.runtime.status().running:
                raise TaskFailure(
                    "停止游戏后进程仍在运行，无法安全替换 SMAPI 文件。"
                    "请稍后重试，或先在「总览」页手动停止。")
            ctx.log("游戏已停止，开始替换 SMAPI")

        ctx.step("获取 SMAPI（联网下载约 40 MiB）", 40)
        try:
            info = smapi.install(game_root, version=want, force=True,
                                 on_log=lambda m: ctx.log(m))
        except ValidationError as exc:
            # 已经停了服却没换成：尽量把游戏恢复回去，别让用户处于"停着"的状态
            if was_running:
                ctx.log("SMAPI 整合失败。服务端已停止且**不会自动重启** —— "
                        "如需恢复运行请到「总览」页点「启动服务器」。")
            raise TaskFailure(f"SMAPI 获取/整合失败：{exc}") from exc

        ctx.mark_mutated("SMAPI 已写入游戏目录")
        ctx.step("校验整合结果", 90)
        now = smapi.read_smapi_version(game_root)
        if not smapi.is_installed(game_root):
            raise TaskFailure("整合后仍未检测到 SMAPI，请检查磁盘空间与权限")
        ctx.log(f"完成：SMAPI {now or info.get('smapi_version')}"
                + (f"（游戏 {game_ver}）" if game_ver else ""))
        info["installed_now"] = now or info.get("smapi_version", "")
        info["game_version"] = game_ver

        # **不自动重启**（用户明确要求：启动服务端必须是独立的手动动作）。
        if was_running:
            ctx.log("为替换 SMAPI 文件，本次已停止服务端；完成整合后**不会自动启动** —— "
                    "请到「总览」页点「启动服务器」。")
        else:
            ctx.log("SMAPI 已更新。**不会自动启动** —— 请到「总览」页点「启动服务器」。")
        info["needs_manual_start"] = bool(was_running)
        return info

    # ── 原生部署：把已定位的游戏本体目录就位（上传 / Steam 下载共用）──────
    def _place_game_body(self, ctx: TaskContext, found: Path, *,
                         progress: int = 45, source: str = "") -> dict[str, Any]:
        """校验 → 替换线上游戏目录 → 建立链接 → 补齐 SMAPI。

        两条来源共用同一套逻辑（用户上传 ZIP / 用 Steam 扫码下载），
        保证无论从哪来，平台校验与 SMAPI 供给的行为完全一致。
        """
        target = C.GAME_FILES_DIR / "Stardew Valley"
        ctx.step("校验游戏本体（平台与完整性）", progress)
        report = gamebody.inspect(found)
        ctx.log(f"识别平台：{report.platform}"
                + (f"（.so 文件 {report.info.get('so_count')} 个）"
                   if report.platform == gamebody.PLATFORM_LINUX else ""))
        for warn in report.warnings:
            ctx.log(f"⚠️ {warn}")
        if not report.ok:
            raise TaskFailure(
                "游戏本体校验未通过（未做任何替换，原游戏目录保持不动）："
                + "；".join(report.errors))

        backup: Path | None = None
        ctx.step("就位（替换旧的游戏本体）", min(progress + 25, 92))
        if target.exists():
            backup = C.RESTORE_TMP_DIR / f"pre-gamefiles-{int(time.time())}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(target), str(backup))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(str(found), str(target))
        except OSError:
            shutil.copytree(found, target)

        ctx.step("建立 MOD / 存档链接", min(progress + 33, 94))
        if hasattr(self.runtime, "_link_mods"):
            self.runtime._link_mods(C.load_config())
        if hasattr(self.runtime, "_link_saves"):
            self.runtime._link_saves(C.load_config())

        # 游戏本体到位后补齐 SMAPI（失败不判整个安装失败，但要明确告知并给重试入口）
        ctx.step("准备 SMAPI（由管理器自动获取）", min(progress + 40, 96))
        game_ver = smapi.read_game_version(target)
        if smapi.is_installed(target):
            have = smapi.read_smapi_version(target)
            ctx.log(f"游戏目录里已有 SMAPI {have or '（版本未识别）'}，跳过获取")
        else:
            if game_ver:
                ctx.log(f"检测到游戏版本：{game_ver}")
            else:
                ctx.log("⚠️ 未能识别游戏版本，将按最新兼容版本安装 SMAPI")
            try:
                info = smapi.install(target, on_log=lambda m: ctx.log(m))
                ctx.log(f"SMAPI {info.get('installed_now') or info.get('smapi_version')} 已整合完成")
            except ValidationError as exc:
                ctx.log(f"⚠️ SMAPI 自动获取失败：{exc}")
                ctx.log("游戏本体已就位；可在「系统」页点「获取 / 更新 SMAPI」重试。")
                raise TaskFailure(
                    "游戏本体已就位，但 SMAPI 自动获取失败：" + str(exc)
                    + "（可在「系统」页点「获取 / 更新 SMAPI」重试）")

        launcher = target / "StardewModdingAPI"
        if not launcher.is_file():
            raise TaskFailure("整合后仍未找到 StardewModdingAPI 启动器")
        os.chmod(launcher, 0o755)

        # 无论 SMAPI 是本次整合的还是本来就有的，都要校准 steam_appid.txt：
        # Steam depot 里带的是 480，会让 Steam 联机/成就失效（详见 mgr/smapi.py）。
        smapi.ensure_app_id(target, on_log=lambda m: ctx.log(m))

        ctx.mark_mutated("游戏本体已安装")
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        ctx.log(f"游戏本体已就位：{target}（{size / 1024 / 1024:.0f} MiB）"
                + (f"　来源：{source}" if source else ""))
        return {"game_root": str(target), "size_bytes": size,
                "platform": report.platform, "game_version": game_ver}

    # ── 原生部署：Steam 扫码登录（独立动作，不下载游戏文件）────────────────
    def task_native_steam_login(self, ctx: TaskContext) -> dict[str, Any]:
        """只做一件事：完成 Steam 扫码登录。

        为什么和"获取游戏本体"分开（用户明确要求）：
          * 两者性质不同：登录是**一次性的会话建立**，获取/更新是**可能几百 MB 的下载**；
          * 揉在一起时，界面上"扫完码还要等几分钟"，用户分不清是卡住了还是在下载；
          * 分开后登录是秒级的（`-manifest-only`，只登录+拉清单、不下内容），
            失败也能立刻定位到"登录失败"而不是"下载失败"。
        """
        payload = ctx.payload
        force_qr = bool(payload.get("force_qr", True))

        ctx.step("准备 DepotDownloader（运行时获取，不随包分发）", 10)
        try:
            info = steamtools.provision(on_log=lambda m: ctx.log(m))
        except ValidationError as exc:
            raise TaskFailure(f"准备 DepotDownloader 失败：{exc}") from exc
        if info.get("skipped"):
            ctx.log(f"已就绪：DepotDownloader {info.get('version') or ''}")

        work = C.STAGING_DIR / f"steam-login-{int(time.time())}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        ctx.step("Steam 扫码登录（只登录，不下载游戏文件）", 30)
        steamtools.write_qr_state(None)
        steamtools.write_progress({"stage": "启动 DepotDownloader", "started_at": time.time()})

        def on_qr(code) -> None:
            steamtools.write_qr_state(code)
            ctx.log(f"二维码已刷新（{code.modules}×{code.modules} 模块，QR 版本 {code.version}）")

        def on_login(username: str) -> None:
            steamtools.write_qr_state(None)
            ctx.log(f"✅ Steam 登录成功（账号 {username}）")

        try:
            result = steamtools.run_login(
                work,
                force_qr=force_qr,
                on_log=lambda m: ctx.log(m),
                on_qr=on_qr,
                on_login=on_login,
                should_cancel=lambda: ctx.cancelled,
            )
        except ValidationError as exc:
            steamtools.write_qr_state(None)
            steamtools.write_progress(None)
            raise TaskFailure(f"Steam 登录失败：{exc}") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

        steamtools.write_qr_state(None)
        steamtools.write_progress(None)
        if result.cancelled:
            raise TaskCancelled("Steam 登录已被取消")
        if not result.ok:
            raise TaskFailure("Steam 登录未完成：" + (result.error or "")[:400]
                              + "（若二维码未被确认，请重新发起并在有效期内扫码）")

        username = steamtools.saved_username()
        if not username:
            raise TaskFailure("DepotDownloader 已结束，但没有识别到登录账号；请重试。")
        ctx.step("登录完成", 100)
        ctx.log(f"已保存登录（账号 {username}）；现在可以「获取 / 更新游戏本体」。")
        return {"username": username, "cached_login": steamtools.has_cached_login()}

    # ── 原生部署：用 Steam 扫码获取 / 更新游戏本体 ──────────────────────────
    def task_native_steam_fetch(self, ctx: TaskContext) -> dict[str, Any]:
        """用 Steam 手机 App 扫码登录，从 Steam 下载游戏本体（合法路径：用你自己的账号）。

        流程：准备 DepotDownloader → 停服（要替换游戏文件）→ 变更前快照 →
              扫码登录 → 下载 → 平台校验 → 就位 → 按需拉起游戏。
        登录密钥会缓存，之后「更新游戏本体」无需再次扫码。
        """
        payload = ctx.payload

        # 登录与下载分开：这里只负责下载，登录必须已经完成。
        username = steamtools.saved_username()
        if not username:
            raise TaskFailure(
                "尚未登录 Steam，无法获取游戏本体。"
                "请先在「系统」页点「Steam 扫码登录」完成登录（登录与下载是两个独立动作）。")
        if not steamtools.has_cached_login():
            raise TaskFailure(
                f"未找到 Steam 登录凭据（账号 {username}）。"
                "请重新执行「Steam 扫码登录」。")
        ctx.log(f"使用已登录账号：{username}")

        ctx.step("准备 DepotDownloader（运行时获取，不随包分发）", 4)
        try:
            info = steamtools.provision(on_log=lambda m: ctx.log(m))
        except ValidationError as exc:
            raise TaskFailure(f"准备 DepotDownloader 失败：{exc}") from exc
        if info.get("skipped"):
            ctx.log(f"已就绪：DepotDownloader {info.get('version') or ''}")
        ctx.log(f"许可证：{steamtools.ASSET_LICENSE}（仅运行时下载，不随本应用分发）")

        # 要替换游戏本体文件，必须先停服
        was_running = bool(self.runtime.status().running)
        if was_running:
            ctx.step("停止游戏（下载期间不能占用游戏文件）", 8)
            stop = self.runtime.stop()
            if not stop.ok:
                raise TaskFailure(f"下载前停止游戏失败：{stop.tail[:300]}")
            if self.runtime.status().running:
                raise TaskFailure("停止游戏后进程仍在运行，无法安全替换游戏文件。请稍后重试。")

        ctx.step("变更前快照（若已有存档/MOD/旧游戏本体）", 12)
        self._auto_snapshot(ctx, "pre-steam-fetch", allow_empty=True)

        staging = C.STAGING_DIR / f"steam-fetch-{int(time.time())}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        ctx.step("Steam 扫码登录并下载（请用 Steam 手机 App 扫二维码）", 20)
        steamtools.write_qr_state(None)      # 清掉上一次的二维码

        def on_login(username: str) -> None:
            # 登录一成功就清掉二维码：否则界面会继续显示那张**已经用完**的码，
            # 用户会以为"扫了但没反应"（实测正是这个体验问题）。
            steamtools.write_qr_state(None)
            ctx.log(f"已保存登录（账号 {username}）；之后「更新游戏本体」无需再扫码")

        def on_qr(code) -> None:
            # 界面通过 GET /api/native/steam/qr 读取；同时也把二维码打进任务日志，
            # 便于用户在终端/日志里排查（日志里会带上矩阵大小）。
            steamtools.write_qr_state(code)
            ctx.log(f"二维码已刷新（{code.modules}×{code.modules} 模块，QR 版本 {code.version}），"
                    "请在「系统」页扫码；若界面未显示可重试本任务")

        try:
            result = steamtools.run_fetch(
                staging,
                on_log=lambda m: ctx.log(m),
                on_qr=on_qr,
                on_login=on_login,
                should_cancel=lambda: ctx.cancelled,
            )
        except ValidationError as exc:
            steamtools.write_qr_state(None)
            if was_running:
                ctx.log("前提：为替换游戏文件已停止服务端；本次失败后**不会自动启动** —— "
                        "如需恢复运行请到「总览」页点「启动服务器」。")
            raise TaskFailure(f"Steam 下载失败：{exc}") from exc

        if result.cancelled:
            # 用户主动取消：恢复现场并把任务判为"已取消"，而不是"失败"
            steamtools.write_qr_state(None)
            if was_running:
                ctx.log("本次已停止服务端；任务被取消后**不会自动启动** —— "
                        "如需恢复运行请到「总览」页点「启动服务器」。")
            raise TaskCancelled("Steam 下载已被取消（未做任何替换）")

        if not result.ok:
            steamtools.write_qr_state(None)
            tail = (result.error or result.stdout_tail or "")[:600]
            if was_running:
                ctx.log("前提：为替换游戏文件已停止服务端；本次未成功，"
                        "**不会自动启动** —— 如需恢复运行请到「总览」页点「启动服务器」。")
            raise TaskFailure(
                "Steam 下载未完成：" + tail
                + "（若二维码未被确认，请重新发起并在 5 分钟内扫码）")

        steamtools.write_qr_state(None)
        if result.downloaded_root is None:
            raise TaskFailure(
                "DepotDownloader 已结束，但没有找到下载出来的游戏目录。"
                f"输出尾部：{(result.stdout_tail or '')[-400:]}")

        ctx.log(f"下载完成：{result.downloaded_root}"
                f"（{util.human_bytes(util.dir_size_bytes(result.downloaded_root))}）")

        placed = self._place_game_body(
            ctx, result.downloaded_root, progress=70, source="Steam（扫码登录）")
        rv = self.versions.detect_runtime()
        self.versions.record(rv, note="Steam 扫码获取游戏本体")
        placed["steam"] = {"app_id": steamtools.STEAM_APP_ID,
                           "cached_login": steamtools.has_cached_login()}
        shutil.rmtree(staging, ignore_errors=True)

        # **不自动启动服务端**（用户明确要求）：
        # 「Steam 扫码登录」「下载获取游戏」「启动服务端」是三个独立动作，
        # 每个都要用户自己点。下载完就停在这里，由用户去「总览」页点「启动服务器」。
        if was_running:
            ctx.log("注意：为替换游戏文件，本次已停止正在运行的服务端；"
                    "下载完成后**不会自动启动** —— 请到「总览」页点「启动服务器」。")
        else:
            ctx.log("游戏本体已就位。**不会自动启动** —— 请到「总览」页点「启动服务器」。")
        placed["needs_manual_start"] = True
        return placed

    # ── 原生部署：游戏本体安装 ──────────────────────────────────────────────
    def task_native_install_gamefiles(self, ctx: TaskContext) -> dict[str, Any]:
        """把用户上传的游戏本体 ZIP 安装到数据区（SMAPI 自带）。

        合规要点：游戏本体与 SMAPI **由用户自备**，本项目不打包、不再分发。
        这里只做「安全解包 + 定位 + 校验 + 就位」，并在完成后校验启动器可用。
        """
        payload = ctx.payload
        zip_path = Path(str(payload.get("zip_path", "")))
        if not zip_path.is_file():
            raise TaskFailure("上传的压缩包不存在或已被清理")
        ctx.step("检查压缩包", 5)
        plan = util.inspect_zip(zip_path)
        if not plan.ok:
            raise TaskFailure(f"压缩包检查未通过：{plan.reason}")
        ctx.log(f"压缩包包含 {plan.file_count} 个文件，解压后约 "
                f"{plan.total_uncompressed / 1024 / 1024:.0f} MiB")

        ctx.step("安装前快照（若已有存档/MOD/旧游戏本体）", 10)
        # 首次安装时 Saves/Mods 都是空的 → 没有数据可丢，允许跳过（否则永远装不上）
        self._auto_snapshot(ctx, "pre-gamefiles-install", allow_empty=True)

        staging = C.STAGING_DIR / f"gamefiles-{int(time.time())}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            ctx.step("解压到暂存区", 35)
            from mgr.util import safe_extract_zip
            safe_extract_zip(zip_path, staging)
            ctx.log("解压完成，正在定位游戏根目录…")

            # 定位含 StardewModdingAPI 或 Stardew Valley.dll 的目录（支持多层嵌套）
            found: Path | None = None
            for dirpath, dirnames, filenames in os.walk(staging):
                names = set(filenames)
                if "StardewModdingAPI" in names or "Stardew Valley.dll" in names:
                    found = Path(dirpath)
                    break
            if found is None:
                raise TaskFailure(
                    "解压后没有找到游戏本体：需要包含 `Stardew Valley.dll`（或 "
                    "`StardewModdingAPI`）。请确认上传的是完整游戏目录，"
                    "而不是只有 Mods/ 或 Saves/。")
            ctx.log(f"识别到游戏根目录：{found.relative_to(staging)}")

            # 平台/完整性校验：必须在替换线上游戏目录**之前**做。
            # 实测踩到：用户上传了 Windows 版，装完启动器只会报
            # "You must install .NET to run this application"，很难联想到平台错了。
            result = self._place_game_body(ctx, found, progress=45,
                                           source="用户上传的 ZIP")
            return {**result, "sha256": payload.get("sha256", "")}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── 原生部署：一键开服（首次自动建档 → 载档 → 联机）──────────────────
    def task_native_host_bootstrap(self, ctx: TaskContext) -> dict[str, Any]:
        """把"游戏进程在跑但没开服"变成"真的开服"。

        为什么需要它（真机踩到）：进程起来、MOD 加载完成、三层判据全绿，
        但游戏其实停在**标题页** —— ALOS 是 `SaveLoaded` 之后才
        setServerMode/enableServer，**没有存档就永远不会开服**。

        上游文档给出的无人值守路径（Mods/ServerCMD/README.md）：
            ServerCMD.set_multiplayermode <0|1|2>   ← 2 = 网络联机（**必须先设**）
            ServerCMD.load_save <存档名>            ← 载档时游戏才会读 multiplayerMode

        **不自动创建存档**（用户明确要求）：上游 `ServerCMD.new_save` 会跳过命名流程，
        建出玩家名/农场名都为空的存档（实测目录名 `_451468556`）。
        没有存档时本任务直接失败，并给出手动步骤：VNC 里新建存档 → 重启服务端。

        注意：这些指令要写进**游戏进程的 stdin**，而此前的进程是用
        `stdin=/dev/null` 起的（收不到任何输入）—— 所以这项能力是本次新增的。
        """
        payload = ctx.payload
        mode = int(payload.get("multiplayer_mode", 2) or 2)
        if mode not in (0, 1, 2):
            raise TaskFailure("联机模式只能是 0（关闭）/ 1（本地联机）/ 2（网络联机）")
        timeout = int(payload.get("save_load_timeout", 180))

        ctx.step("检查游戏进程与控制台通道", 5)
        ready = self.runtime.readiness()
        if not ready.get("layers", {}).get("L1_game_process"):
            raise TaskFailure("游戏进程未运行。请先在「总览」页启动服务器。")
        if not ready.get("layers", {}).get("L3_smapi_loaded"):
            raise TaskFailure("SMAPI 尚未加载完成，请稍候再试（可到「控制台」页看进度）。")

        def send(cmd: str, wait: float = 1.0) -> None:
            ctx.log(f"→ {cmd}")
            try:
                res = self.runtime.send_console(cmd)
            except Exception as exc:  # noqa: BLE001
                raise TaskFailure(f"发送控制台指令失败：{exc}") from exc
            if not res.ok:
                raise TaskFailure(f"发送控制台指令失败：{res.stderr or res.stdout}")
            time.sleep(wait)

        def save_dirs() -> list[Path]:
            try:
                return sorted([p for p in C.SAVES_DIR.iterdir() if p.is_dir()],
                              key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return []

        # 1) 必须有存档 —— **不自动创建**
        #
        # 为什么不自动建：上游 `ServerCMD.new_save` 的实现是
        # `new TitleMenu().createdNewCharacter(true)`，它确实能建出可玩存档，
        # 但**跳过了命名流程** → 玩家名与农场名都是空的
        # （实测生成 `_451468556` 这种目录，玩家名 `<name />`）。
        # 这种存档不该替用户创建，所以这里直接停下来，让用户手动建一个正常的。
        saves = save_dirs()
        created = ""
        if not saves:
            raise TaskFailure(
                "尚未检测到存档：服务能启动，但不会开服。\n"
                "**需手动新建存档并重启服务端**：\n"
                "① 打开 WebVNC（总览页有入口），在游戏里新建一个存档"
                "（这样玩家名/农场名才正常）；\n"
                "② 重启服务端 —— 管理器会自动载入最新存档并开服，"
                "之后 ALOS 接管无人值守。\n"
                "（本管理器不会替你自动建存档。）")
        ctx.log(f"已有 {len(saves)} 个存档，使用最新的：{saves[0].name}")

        # 2) **先**设置联机模式，**再**载档。
        # 实测确认（上游 ServerCMD 源码里 LoadSave 也注释着 `//Game1.multiplayerMode = 2;`）：
        # 游戏是在**载入存档时**读取 Game1.multiplayerMode 来决定要不要开服，
        # 顺序反了就不会开服 —— 我第一版就是反的。
        target_save = created or saves[0].name
        ctx.step(f"设置联机模式 {mode}（2 = 网络联机）", 45)
        send(f"ServerCMD.set_multiplayermode {mode}", wait=2.0)

        # 3) 载入存档
        ctx.step(f"载入存档 {target_save}", 70)
        send(f"ServerCMD.load_save {target_save}", wait=3.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            tail = self.runtime.console_log(tail_bytes=200_000)
            if target_save in tail or "Loading game" in tail or "SaveGame.Load" in tail:
                ctx.log("已在控制台看到存档加载相关输出。")
                break
            time.sleep(3)
        else:
            ctx.log("⚠️ 未在控制台确认到加载输出，继续尝试设置联机模式（可到控制台页核对）")

        # 4) 复核：控制台是否出现"开始服务器"的迹象 + 端口是否就绪
        ctx.step("等待服务器开始监听", 88)
        hosting = False
        deadline = time.time() + 90
        port = int(C.load_config().native.game_port)
        while time.time() < deadline:
            if udp_port_open(port):      # 游戏端口是 UDP（见上）
                hosting = True
                break
            time.sleep(3)
        after = self.runtime.readiness()
        ctx.log(f"当前状态：{after.get('phase')} —— {str(after.get('detail') or '')[:100]}")
        if hosting:
            ctx.log(f"✅ 服务器已在监听游戏端口 {port}（UDP）")
        else:
            ctx.log(f"⚠️ 尚未检测到游戏端口 {port} 就绪，请到「控制台」页核对 ServerCMD 的输出")
        ok_hosting = hosting
        if not ok_hosting:
            ctx.log("⚠️ 状态复核仍显示『未开服/无存档』，请到「控制台」页确认游戏输出。")
        ctx.mark_mutated("已发送开服指令（新建/载入存档并设置联机模式）")
        return {
            "save": target_save,
            "created": bool(created),
            "multiplayer_mode": mode,
            "hosting_recheck": ok_hosting,
            "next": "若玩家仍无法加入，请到「控制台」页查看 ServerCMD 的输出；"
                    "端口为服务器端口（UDP）。",
        }

    # ── 配置 / 上游 / 诊断 ──────────────────────────────────────────────────
    def task_config_apply(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        ctx.step("配置变更前快照", 5)
        self._auto_snapshot(ctx, "pre-config-change")
        ctx.step("校验并应用配置", 40)
        cfg = self.reload_cfg()
        snapshot_cfg = json.loads(json.dumps(cfg.to_dict()))
        try:
            _apply_config_payload(cfg, payload.get("changes") or {})
        except ValidationError as exc:
            raise TaskFailure(f"配置校验失败，未做任何修改：{exc}")
        C.save_config(cfg)
        # 用运行后端的校验（含显示组件、游戏本体、端口冲突）
        report = self.runtime.validate(cfg)
        if not report["ok"]:
            C.save_config(_from_dict(snapshot_cfg))
            raise TaskFailure("新配置未通过校验，已回滚配置文件：" + "；".join(report["errors"])[:400])
        self.runtime.prepare(cfg)
        ctx.set_result(previous=snapshot_cfg)
        ctx.mark_mutated("配置文件已写入")
        ctx.step("重启服务以应用变更", 70)
        # 原有服务可能已在运行：先停再起，避免新旧端口/显示并存
        self.runtime.stop(timeout=120)
        up = self.runtime.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"应用新配置后启动失败：{up.tail[:600]}")
        state = self._wait_ready(ctx, timeout=600)
        # 配置变更会重启运行时；重启后必须补上"载档开服"，
        # 否则改完设置服务器就停在标题页（排查发现的同类缺口）。
        self._maybe_auto_host(ctx, cfg)
        ctx.set_result(new_config={k: v for k, v in cfg.to_dict().items() if k != "admin_password"})
        return {"readiness": state}

    def rollback_config_apply(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        previous = result.get("previous") or ctx.result.get("previous")
        if not isinstance(previous, dict):
            raise TaskFailure("没有配置快照，无法自动回滚（现场已保留）")
        ctx.log("回滚配置文件到变更前内容")
        cfg = _from_dict(previous)
        C.save_config(cfg)
        self.runtime.stop()
        self.runtime.prepare(cfg)
        up = self.runtime.up()
        if not up.ok:
            raise TaskFailure(f"回滚配置后启动失败：{up.tail[:400]}")
        # 同上：回滚也重启了运行时，要补载档开服
        self._maybe_auto_host(ctx, cfg)

    def task_upstream_update(self, ctx: TaskContext) -> dict[str, Any]:
        """同步上游代码（git fetch），只做只读检查并报告差异。"""
        repo_dir = Path(ctx.payload.get("repo_dir") or (C.PKGHOME / "upstream-repo"))
        ctx.step("检查上游仓库", 20)
        if not (repo_dir / ".git").is_dir():
            raise TaskFailure(f"上游仓库目录不存在：{repo_dir}（请先初始化）")
        res = run(["git", "fetch", "--tags", "origin"], cwd=repo_dir, timeout=300)
        if not res.ok:
            raise TaskFailure(f"git fetch 失败：{res.tail[:400]}")
        log = run(["git", "log", "--oneline", "-10", "origin/main"], cwd=repo_dir, timeout=60)
        diff = run(["git", "diff", "--stat", "HEAD..origin/main"], cwd=repo_dir, timeout=60)
        ctx.log("最近上游提交：\n" + log.stdout[-2000:])
        return {"recent": log.stdout[-2000:], "diff_stat": diff.stdout[-2000:]}

    def task_diagnostics_bundle(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("收集诊断信息", 20)
        bundle = self.diag.build_bundle(task_id=ctx.task_id, progress=ctx.step)
        size = bundle.stat().st_size
        ctx.log(f"诊断包已生成：{bundle}（{size} 字节）")
        return {"path": str(bundle), "size": size, "name": bundle.name}

    def task_noop(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("检查系统依赖", 50)
        det = self.runtime.detect()
        ctx.log(json.dumps(det, ensure_ascii=False)[:800])
        return {"runtime": det}


# ─────────────────────────────────────────────────────────────────────────────
# 配置应用辅助
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_CONFIG_FIELDS = {
    "native": {"display_number", "screen_width", "screen_height", "screen_depth",
               "window_manager", "vnc_port", "vnc_password", "webvnc_port", "game_port",
               "cmdwebui_port", "host_bind", "graceful_stop_seconds", "xstack_source",
               "extra_args", "software_gl", "display_backend",
               "auto_host_on_start"},
    "policy": {"keep_auto_snapshots", "keep_manual_snapshots", "auto_snapshot_enabled",
               "auto_snapshot_interval_minutes", "min_free_space_mb",
               "snapshot_space_estimate_mb", "start_retry_max",
               "start_retry_backoff_seconds", "supervisor_enabled",
               "auto_start_with_nas", "confirm_destructive"},
}

PORT_FIELDS = {"game_port", "webvnc_port", "vnc_port", "cmdwebui_port"}
NATIVE_CHOICES = {"xstack_source": ("bundled", "system"),
                  "window_manager": ("openbox", "matchbox-window-manager", "twm", "fluxbox", "jwm")}


def _apply_config_payload(cfg: C.ManagerConfig, changes: dict[str, Any]) -> None:
    """把前端提交的**受限字段**写入配置。未知字段一律拒绝。"""
    if not isinstance(changes, dict):
        raise ValidationError("changes 必须是对象")
    for section, fields in changes.items():
        if section not in ALLOWED_CONFIG_FIELDS:
            raise ValidationError(f"不允许修改的配置段：{section}")
        if not isinstance(fields, dict):
            raise ValidationError(f"配置段 {section} 必须是对象")
        target = getattr(cfg, section)
        allowed = ALLOWED_CONFIG_FIELDS[section]
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"不允许修改的配置项：{section}.{key}")
            current = getattr(target, key)
            if key in NATIVE_CHOICES:
                if not isinstance(value, str) or value not in NATIVE_CHOICES[key]:
                    raise ValidationError(
                        f"{key} 必须是以下之一：{', '.join(NATIVE_CHOICES[key])}")
            elif key in PORT_FIELDS:
                value = _to_int(value, key, 1, 65535)
            elif isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ValidationError(f"{key} 必须是布尔值")
            elif isinstance(current, int):
                value = _to_int(value, key, 0, 10 ** 9)
            elif isinstance(current, str):
                if not isinstance(value, str):
                    raise ValidationError(f"{key} 必须是字符串")
                value = value.strip()
                if len(value) > 300:
                    raise ValidationError(f"{key} 过长")
            else:
                raise ValidationError(f"不支持的配置类型：{key}")
            setattr(target, key, value)
    # ── 端口唯一性（原生部署只有一套端口）──
    ports = [cfg.native.game_port, cfg.native.webvnc_port,
             cfg.native.vnc_port, cfg.native.cmdwebui_port]
    if len(set(ports)) != len(ports):
        raise ValidationError(f"端口配置存在重复，请修改：{ports}")
    for p in ports:
        if p in (5666, 5667, 22, 445, 2049, 5005, 5006, 8000):
            raise ValidationError(f"端口 {p} 属于飞牛 OS 或系统保留端口，请换一个")
    if cfg.panel_tcp_port in ports:
        raise ValidationError(f"端口 {cfg.panel_tcp_port} 已被管理面板占用，请换一个")

    # ── 字符串字段格式 ──
    if cfg.native.vnc_password:
        _validate_vnc_password(cfg.native.vnc_password)
    if cfg.native.host_bind:
        _validate_bind(cfg.native.host_bind)
    if cfg.native.xstack_source not in ("bundled", "system"):
        raise ValidationError("显示组件来源必须是 bundled 或 system")
    if cfg.native.window_manager not in ("openbox", "matchbox-window-manager", "twm",
                                         "fluxbox", "jwm"):
        raise ValidationError("窗口管理器取值不合法")
    if cfg.native.extra_args and len(cfg.native.extra_args) > 200:
        raise ValidationError("extra_args 过长")
    if cfg.native.display_backend not in ("auto", "xvfb", "weston"):
        raise ValidationError(
            "显示后端只能是 auto（自动，默认）/ xvfb（软件渲染）/ weston（核显）")
    for small in ("display_number", "screen_width", "screen_height", "screen_depth"):
        value = getattr(cfg.native, small)
        if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 65535):
            raise ValidationError(f"native.{small} 必须是 1-65535 的整数")


def _validate_vnc_password(value: str) -> str:
    """VNC 协议只使用前 8 个字符，因此要求恰好 8 位可见 ASCII。"""
    import re as _re
    if not _re.fullmatch(r"[\x21-\x7e]{8}", value):
        raise ValidationError("VNC 密码必须是恰好 8 位可见 ASCII 字符")
    return value


def _validate_bind(value: str) -> str:
    import ipaddress
    import re as _re
    if value in ("0.0.0.0", "::"):
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if _re.fullmatch(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}", value):
        return value
    raise ValidationError("监听地址只允许 0.0.0.0 / :: 或合法 IP 地址")


def _to_int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是整数")
    if not (low <= number <= high):
        raise ValidationError(f"{field} 超出范围（{low}-{high}）")
    return number


def _from_dict(raw: dict[str, Any]) -> C.ManagerConfig:
    cfg = C.ManagerConfig()
    from mgr.config import _merge
    return _merge(cfg, raw)
