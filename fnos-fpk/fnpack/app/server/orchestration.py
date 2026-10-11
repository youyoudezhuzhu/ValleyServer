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
from mgr.logmod import get_logger
from mgr.tasks import HIGH_RISK_KINDS, KIND_LABELS, TaskContext, TaskFailure, get_manager
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
    mgr.register("server.update", ctx.task_server_update,
                 rollback=ctx.rollback_server_update)
    mgr.register("server.rollback", ctx.task_server_rollback)
    mgr.register("server.pin", ctx.task_server_pin)
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
    mgr.register("diagnostics.bundle", ctx.task_diagnostics_bundle)
    mgr.register("system.noop", ctx.task_noop)
    LOG.info(f"已注册 {len(mgr.known_kinds())} 种任务类型")


class _Services:
    def __init__(self, app) -> None:
        self.app = app
        self.cfg = C.load_config()
        self.compose = app.compose
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

    def _auto_snapshot(self, ctx: TaskContext, reason: str) -> str:
        """高风险操作前的自动快照。返回快照 ID；失败会直接抛错终止任务。"""
        if ctx.payload.get("skip_snapshot"):
            ctx.log("⚠️ 调用方请求跳过自动快照（skip_snapshot=true）")
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
            "image_digest,image_ref,mods_hash,saves_hash,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (info.id, info.kind, info.reason, info.task_id or None, info.created_at,
             info.size_bytes, info.manifest_version, 1 if info.valid else 0, time.time(),
             info.file_count, info.game_version, info.smapi_version, info.image_digest,
             info.image_ref, info.mods_hash, info.saves_hash, info.note),
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
            "image_ref": rv.image_ref or f"{cfg.container.image}:{cfg.container.image_tag}",
            "image_digest": rv.image_digest, "image_created": rv.image_created,
            "mods": mods_list,
        }

    def _wait_ready(self, ctx: TaskContext, *, timeout: int = 300,
                    progress_from: int = 80) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = ""
        step = 0
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            state = self.compose.readiness()
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
        ctx.step("检查 Docker 依赖", 5)
        try:
            from mgr.compose_mgr import ComposeManager
            det = ComposeManager.require_ready()
        except Exception as exc:  # noqa: BLE001
            raise TaskFailure(str(exc))
        ctx.log(f"Docker {det['docker']['version']} / Compose {det['compose']['version']}")
        self._guard_space(ctx)

        ctx.step("准备数据目录与容器配置", 15)
        C.ensure_dirs()
        cfg = self.reload_cfg()
        if not cfg.container.vnc_password:
            import secrets
            cfg.container.vnc_password = secrets.token_urlsafe(8)[:8]
            import re as _re
            if not _re.fullmatch(r"[\x21-\x7e]{8}", cfg.container.vnc_password):
                cfg.container.vnc_password = "vs" + secrets.token_hex(3)
            C.save_config(cfg)
            ctx.log("已生成随机 VNC 密码（不再使用上游默认的 041041）")
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        ctx.log("compose 与 entrypoint 已生成")
        res = self.compose.validate_rendered()
        if not res.ok:
            raise TaskFailure(f"compose 配置校验失败：{res.tail[:500]}")

        ctx.step("拉取镜像（可能需要较长时间，首次约 3.4 GiB）", 30)
        image_ref = f"{cfg.container.image}@{cfg.container.image_digest}" \
            if cfg.container.pin_digest and cfg.container.image_digest \
            else f"{cfg.container.image}:{cfg.container.image_tag}"
        pull = self.compose.pull_image(image_ref, timeout=7200)
        if not pull.ok:
            # 回退到 tag 形式再试一次（有些镜像仓库对 digest 拉取有额外限制）
            alt = f"{cfg.container.image}:{cfg.container.image_tag}"
            ctx.log(f"按摘要拉取失败，改用标签 {alt} 重试")
            pull = self.compose.pull_image(alt, timeout=7200)
            if not pull.ok:
                raise TaskFailure(f"镜像拉取失败：{pull.tail[:800]}")
        actual = self.compose.image_digest(image_ref) or self.compose.image_digest(
            f"{cfg.container.image}:{cfg.container.image_tag}")
        if actual:
            ctx.log(f"实际镜像摘要：{actual}")
            ctx.set_result(actual_digest=actual)
            if cfg.container.image_digest and actual != cfg.container.image_digest:
                ctx.log("⚠️ 实际摘要与配置记录不一致，已把实际摘要记入任务结果")

        ctx.step("启动容器", 55)
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"容器启动失败：{up.tail[:800]}")
        state = self._wait_ready(ctx, timeout=600)
        ctx.step("校验存档与 MOD 目录", 94)
        self._verify_saves_readable(ctx)
        rv = self.versions.detect_runtime()
        self.versions.record(rv, note="首次安装", pinned=True)
        ctx.log(f"运行版本：游戏 {rv.game_version or '未知'} / SMAPI {rv.smapi_version or '未知'}")
        return {"readiness": state, "digest": actual, "version": rv.to_dict()}

    def task_server_start(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("检查容器状态", 10)
        st = self.compose.status()
        if st.running:
            ctx.log("容器已在运行，执行健康检查")
            return self._wait_ready(ctx, timeout=300)
        if not st.exists:
            raise TaskFailure("容器不存在，请先执行「安装 / 首次启动」")
        cfg = self.reload_cfg()
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        ctx.step("启动容器", 40)
        res = self.compose.start()
        if not res.ok:
            res = self.compose.up(pull=False)
        if not res.ok:
            raise TaskFailure(f"启动失败：{res.tail[:800]}")
        state = self._wait_ready(ctx, timeout=420)
        self.versions.record(self.versions.detect_runtime(), note="启动后记录")
        return state

    def task_server_stop(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("停止容器", 40)
        res = self.compose.stop()
        if not res.ok:
            raise TaskFailure(f"停止失败：{res.tail[:500]}")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not self.compose.status().running:
                ctx.step("容器已停止", 95)
                return {"stopped": True}
            time.sleep(2)
        raise TaskFailure("停止超时：容器仍在运行")

    def task_server_restart(self, ctx: TaskContext) -> dict[str, Any]:
        ctx.step("重启容器", 30)
        res = self.compose.restart()
        if not res.ok:
            res = self.compose.recreate()
        if not res.ok:
            raise TaskFailure(f"重启失败：{res.tail[:800]}")
        return self._wait_ready(ctx, timeout=420)

    def task_server_recreate(self, ctx: TaskContext) -> dict[str, Any]:
        """受控重建：先快照，再 down+up。"""
        ctx.step("重建前快照", 5)
        self._auto_snapshot(ctx, "pre-config-change")
        cfg = self.reload_cfg()
        ctx.step("重新生成配置", 30)
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        res = self.compose.validate_rendered()
        if not res.ok:
            raise TaskFailure(f"compose 配置校验失败：{res.tail[:500]}")
        ctx.step("重建容器", 50)
        down = self.compose.down()
        if not down.ok:
            ctx.log(f"down 返回非零（继续尝试启动）：{down.tail[:200]}")
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"重建失败：{up.tail[:800]}")
        return self._wait_ready(ctx, timeout=600)

    # ── 更新与回滚 ──────────────────────────────────────────────────────────
    def task_server_update(self, ctx: TaskContext) -> dict[str, Any]:
        payload = ctx.payload
        target_digest = str(payload.get("target_digest", "")).strip()
        target_tag = str(payload.get("target_tag", "")).strip()
        ctx.step("更新前检查", 5)
        self._guard_space(ctx)
        cap = self.versions.rollback_capability()
        ctx.log(f"回滚能力评估：{'具备' if cap['can_rollback'] else '不具备'}"
                + (f"（缺少：{'；'.join(cap['missing'])}）" if cap["missing"] else ""))
        snap_id = self._auto_snapshot(ctx, "pre-server-update")
        ctx.set_result(snapshot_id=snap_id)
        # ⚠️ 必须在这里就把旧的镜像摘要写进任务结果：之后的任何一步失败都会触发回滚，
        #    而回滚处理器需要从任务结果里读回滚目标。晚写一步 = 回滚时找不到旧摘要。
        cfg = self.reload_cfg()
        old_digest = cfg.container.image_digest
        old_tag = cfg.container.image_tag
        ctx.set_result(old_digest=old_digest, old_tag=old_tag)

        ctx.step("拉取目标镜像", 40)
        ref = f"{cfg.container.image}@{target_digest}" if target_digest else \
            f"{cfg.container.image}:{target_tag or old_tag}"
        pull = self.compose.pull_image(ref, timeout=7200)
        if not pull.ok:
            raise TaskFailure(f"目标镜像拉取失败，已中止更新：{pull.tail[:600]}")
        actual = self.compose.image_digest(ref) or target_digest
        if target_digest and actual != target_digest:
            raise TaskFailure(
                f"拉取到的镜像摘要与目标不一致（{actual} != {target_digest}），已中止更新"
            )

        ctx.step("切换到新版本并重建", 60)
        # 配置一旦改写就产生了「需要回滚」的变更（即使后面拉不动旧镜像，
        # 也必须把任务标成 rollback_failed 并保留现场，而不是安静地当成普通失败）。
        ctx.mark_mutated(f"运行版本配置将切换到 {actual[:19]}")
        cfg.container.image_digest = actual
        if target_tag:
            cfg.container.image_tag = target_tag
        cfg.container.pin_digest = True
        C.save_config(cfg)
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)

        down = self.compose.down()
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"新版本容器启动失败：{up.tail[:800]}")

        ctx.step("更新后健康检查", 80)
        state = self._wait_ready(ctx, timeout=600)
        self._verify_saves_readable(ctx)
        rv = self.versions.detect_runtime()
        self.versions.record(rv, note=f"更新到 {actual[:19]}")
        ctx.log(f"更新完成：游戏 {rv.game_version or '未知'} / SMAPI {rv.smapi_version or '未知'}")
        return {"readiness": state, "new_digest": actual,
                "snapshot_id": snap_id, "version": rv.to_dict()}

    def rollback_server_update(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        """更新失败：保留新版本现场与日志，尝试恢复到旧镜像。"""
        old_digest = str(result.get("old_digest") or ctx.result.get("old_digest") or "")
        old_tag = str(result.get("old_tag") or ctx.result.get("old_tag") or "")
        if not old_digest:
            raise TaskFailure(
                "没有记录到旧的镜像摘要，无法回滚镜像。"
                "新版本现场与故障日志已保留，请人工处理。"
            )
        cfg = self.reload_cfg()
        ctx.log(f"尝试回滚到旧镜像摘要 {old_digest}")
        ref = f"{cfg.container.image}@{old_digest}"
        res = run(["docker", "image", "inspect", ref], timeout=60)
        if not res.ok:
            pull = self.compose.pull_image(ref, timeout=3600)
            if not pull.ok:
                raise TaskFailure(
                    f"旧镜像 {ref} 本地不存在且拉取失败，无法回滚镜像。"
                    "故障现场已保留（新版本仍在运行），请人工处理。"
                )
        cfg.container.image_digest = old_digest
        if old_tag:
            cfg.container.image_tag = old_tag
        C.save_config(cfg)
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        self.compose.down()
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"回滚后容器启动失败：{up.tail[:600]}")
        state = self._wait_ready(ctx, timeout=600, progress_from=50)
        ctx.log(f"镜像已回滚到 {old_digest[:19]}；健康状态 {state.get('phase')}")
        # 快照恢复（MOD/存档）交给用户决定或上层任务
        snap = str(result.get("snapshot_id") or "")
        if snap and ctx.payload.get("restore_snapshot_on_rollback", True):
            ctx.log(f"一并从快照 {snap} 恢复存储数据…")
            rest = self.snapshots.restore(snap, stop_server=True, on_log=ctx.log)
            ctx.log(f"已从快照 {snap} 恢复（{rest.get('files', 0)} 个文件）")

    def task_server_rollback(self, ctx: TaskContext) -> dict[str, Any]:
        target = str(ctx.payload.get("target_digest", "")).strip()
        cap = self.versions.rollback_capability(target_digest=target)
        if not cap["can_rollback"]:
            raise TaskFailure(
                "当前不具备回滚条件：" + "；".join(cap["missing"])
                + "。管理器不会伪造「已回滚」的结果。"
            )
        ctx.set_result(target_digest=cap["target_digest"])
        snap_id = self._auto_snapshot(ctx, "pre-server-update")
        ctx.set_result(snapshot_id=snap_id)
        ctx.log("回滚前快照已创建；回滚失败时可用它恢复数据")
        sub = ctx.payload.get("_rollback_only_image", False)
        _ = sub
        return {"target_digest": cap["target_digest"], "snapshot_id": snap_id,
                "note": "镜像回滚由回滚处理器执行（见任务失败处理）；"
                        "若本任务成功，说明已切换到目标摘要。"}

    def task_server_pin(self, ctx: TaskContext) -> dict[str, Any]:
        cfg = self.reload_cfg()
        pin = bool(ctx.payload.get("pin", True))
        digest = str(ctx.payload.get("digest", "")).strip() or cfg.container.image_digest
        if pin and not digest:
            raise TaskFailure("没有可固定的镜像摘要；请先成功安装或更新一次")
        ctx.step("更新版本固定设置", 40)
        cfg.container.pin_digest = pin
        if digest:
            cfg.container.image_digest = digest
        C.save_config(cfg)
        if pin:
            rv = self.versions.detect_runtime()
            vid = self.versions.record(rv, note="用户固定版本", pinned=True)
            ctx.set_result(version_id=vid)
        self.versions.set_pinned(0, False) if not pin else None
        ctx.log(f"版本固定已{'启用' if pin else '关闭'}，摘要 {digest[:19]}")
        return {"pinned": pin, "digest": digest}

    # ── 存档 ────────────────────────────────────────────────────────────────
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
            st = self.compose.status()
            if st.running:
                ctx.step("重启容器以加载新存档", 75)
                self.compose.restart()
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
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        res = self.compose.validate_rendered()
        if not res.ok:
            C.save_config(_from_dict(snapshot_cfg))
            self.compose.write_compose(C.load_config())
            raise TaskFailure(f"新配置未通过 compose 校验，已回滚配置文件：{res.tail[:400]}")
        ctx.set_result(previous=snapshot_cfg)
        ctx.mark_mutated("配置文件已写入")
        ctx.step("重建容器以应用端口/资源变更", 70)
        down = self.compose.down()
        _ = down
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"应用新配置后容器启动失败：{up.tail[:600]}")
        state = self._wait_ready(ctx, timeout=600)
        ctx.set_result(new_config={k: v for k, v in cfg.to_dict().items() if k != "admin_password"})
        return {"readiness": state}

    def rollback_config_apply(self, ctx: TaskContext, result: dict[str, Any]) -> None:
        previous = result.get("previous") or ctx.result.get("previous")
        if not isinstance(previous, dict):
            raise TaskFailure("没有配置快照，无法自动回滚（现场已保留）")
        ctx.log("回滚配置文件到变更前内容")
        cfg = _from_dict(previous)
        C.save_config(cfg)
        self.compose.write_entrypoint()
        self.compose.write_compose(cfg)
        self.compose.down()
        up = self.compose.up(pull=False)
        if not up.ok:
            raise TaskFailure(f"回滚配置后容器启动失败：{up.tail[:400]}")

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
        det = self.compose.detect()
        ctx.log(json.dumps(det, ensure_ascii=False)[:800])
        return {"docker": det}


# ─────────────────────────────────────────────────────────────────────────────
# 配置应用辅助
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_CONFIG_FIELDS = {
    "container": {"game_port", "webvnc_port", "vnc_port", "cmdwebui_port", "host_bind",
                  "image", "image_tag", "image_digest", "pin_digest", "shm_size",
                  "cpu_limit", "mem_limit", "restart_policy", "healthcheck_interval",
                  "healthcheck_retries", "healthcheck_start_period", "vnc_password"},
    "policy": {"keep_auto_snapshots", "keep_manual_snapshots", "auto_snapshot_enabled",
               "auto_snapshot_interval_minutes", "min_free_space_mb",
               "snapshot_space_estimate_mb", "start_retry_max",
               "start_retry_backoff_seconds", "supervisor_enabled",
               "auto_start_with_nas", "confirm_destructive", "metrics_interval_seconds"},
}

PORT_FIELDS = {"game_port", "webvnc_port", "vnc_port", "cmdwebui_port"}


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
            if key in PORT_FIELDS:
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
    from mgr.compose_mgr import ComposeManager
    # 端口冲突检查
    ports = [cfg.container.game_port, cfg.container.webvnc_port,
             cfg.container.vnc_port, cfg.container.cmdwebui_port]
    if len(set(ports)) != len(ports):
        raise ValidationError("端口配置存在重复，请修改")
    if cfg.panel_tcp_port in ports:
        raise ValidationError(f"端口 {cfg.panel_tcp_port} 已被管理面板占用，请换一个")
    for p in ports:
        if p in (5666, 5667, 22, 445, 2049, 5005, 5006, 8000):
            raise ValidationError(f"端口 {p} 属于飞牛 OS 或系统保留端口，请换一个")
    cmp = ComposeManager()
    if cfg.container.vnc_password:
        cmp._validate_password(cfg.container.vnc_password)
    cmp._validate_bind(cfg.container.host_bind)
    if cfg.container.shm_size:
        cmp._validate_size(cfg.container.shm_size, "shm 大小")
    if cfg.container.mem_limit:
        cmp._validate_size(cfg.container.mem_limit, "内存上限")
    if cfg.container.cpu_limit:
        cmp._validate_cpu(cfg.container.cpu_limit)


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
