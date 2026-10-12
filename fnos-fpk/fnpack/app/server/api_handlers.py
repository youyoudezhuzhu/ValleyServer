"""REST API 路由。所有写操作先做参数白名单校验，再交给统一任务队列。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from mgr import config as C
from mgr import util
from mgr import xstack
from mgr import smapi
from mgr import steamtools
from mgr.logmod import get_logger
from mgr.tasks import KIND_LABELS, TaskFailure, get_manager
from mgr.util import ValidationError, atomic_write_bytes, free_mb, human_bytes

LOG = get_logger("manager")

UPLOAD_DIR = C.STAGING_DIR / "uploads"


def register(app) -> None:
    from http_base import MAX_UPLOAD, ApiError, Response

    r = app.router

    # ── 元信息 / 认证 ───────────────────────────────────────────────────────
    def health(req, **kw):
        cfg = C.load_config()
        return {
            "ok": True,
            "version": C.APP_VERSION,
            "appname": "valleyserver",
            "home": os.environ.get("HOME", ""),
            "data_library_path": os.environ.get("DATA_LIBRARY_PATH", ""),
            "pkghome": str(C.PKGHOME),
            "pkgevar": str(C.PKGVAR),
            "pkgevar_exists": C.PKGVAR.is_dir(),
            "started_at": app.started_at,
            "uptime_seconds": int(time.time() - app.started_at),
        }

    def auth_state(req, **kw):
        cfg = C.load_config()
        return {
            "authenticated": bool(req.actor),
            "actor": req.actor,
            "via_gateway": req.via_gateway,
            "needs_login": not bool(req.actor),
            "panel_port": cfg.panel_tcp_port,
            "gateway_prefix": "/app/valleyserver",
        }

    def login(req, **kw):
        cfg = C.load_config()
        data = req.json()
        password = str(data.get("password", ""))
        if not password:
            raise ApiError("请输入口令", 400)
        if not cfg.admin_password:
            raise ApiError("管理器尚未初始化口令，请检查应用安装是否完成", 500)
        if not app.auth.verify_password(password, cfg.admin_password):
            app.db.audit(req.actor or "anonymous", "login", ok=False,
                         detail="口令错误", remote=req.remote)
            raise ApiError("口令错误", 401)
        token, csrf = app.auth.new_session("admin", ttl_minutes=cfg.session_ttl_minutes)
        app.db.audit("admin", "login", ok=True, remote=req.remote)
        return Response(
            {"ok": True, "csrf": csrf},
            headers={"Set-Cookie": f"vs_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age="
                                   f"{cfg.session_ttl_minutes * 60}"},
        )

    def logout(req, **kw):
        if req.session:
            app.auth.drop(req.session.get("token", ""))
        app.db.audit(req.actor or "anonymous", "logout", remote=req.remote)
        return Response({"ok": True},
                        headers={"Set-Cookie": "vs_session=; Path=/; Max-Age=0"})

    def change_password(req, **kw):
        cfg = C.load_config()
        data = req.json()
        current = str(data.get("current", ""))
        new = str(data.get("new", ""))
        if not app.auth.verify_password(current, cfg.admin_password):
            raise ApiError("当前口令不正确", 401)
        if len(new) < 8:
            raise ApiError("新口令至少 8 位", 400)
        cfg.admin_password = app.auth.hash_password(new)
        C.save_config(cfg)
        app.db.audit(req.actor, "change_password", remote=req.remote)
        return {"ok": True}

    r.add("GET", r"/api/health", health)
    r.add("GET", r"/api/auth/state", auth_state)
    r.add("POST", r"/api/login", login)
    r.add("POST", r"/api/logout", logout)
    r.add("POST", r"/api/auth/password", change_password)

    # ── 总览 ────────────────────────────────────────────────────────────────
    def dashboard(req, **kw):
        cfg = C.load_config()
        st = app.runtime.status()
        ready = app.runtime.readiness()
        stats = app.runtime.stats()
        saves = app.saves.list_saves()
        try:
            mods_report = app.mods.diagnose()
        except Exception as exc:  # noqa: BLE001
            mods_report = {"mods": [], "issues": [], "summary": {}, "error": str(exc)}
        try:
            snaps = app.snapshots.list()
        except Exception:  # noqa: BLE001
            snaps = []
        try:
            task_stats = app.tasks.stats()
        except Exception:  # noqa: BLE001
            task_stats = {}
        total, used, free = util.disk_usage(C.DATA_ROOT)
        return {
            "manager_version": C.APP_VERSION,
            "upstream": {
                "repo": cfg.upstream_repo,
                "baseline_commit": cfg.upstream_baseline_commit,
                "baseline_tag": cfg.upstream_baseline_tag,
                "fork": cfg.fork_repo,
            },
            "server": {
                "container": st.to_dict(),
                "readiness": {k: v for k, v in ready.items() if k != "console_tail"},
                "phase": ready.get("phase"),
                "detail": ready.get("detail"),
                "stats": stats,
            },
            "saves": {
                "count": len(saves),
                "active": app.saves.active_save(),
                "invalid": sum(1 for s in saves if not s.valid),
                "latest": [s.to_dict() for s in saves[:3]],
            },
            "mods": {
                "summary": mods_report.get("summary", {}),
                "top_issues": mods_report.get("issues", [])[:5],
            },
            "snapshots": {
                "count": len(snaps),
                "latest": snaps[0].to_dict() if snaps else None,
                "auto": sum(1 for s in snaps if s.kind == "auto"),
                "manual": sum(1 for s in snaps if s.kind == "manual"),
                "invalid": sum(1 for s in snaps if not s.valid),
            },
            "tasks": task_stats,
            "disk": {
                "total": total, "used": used, "free": free,
                "free_human": human_bytes(free),
                "low": free // (1024 * 1024) < cfg.policy.min_free_space_mb,
                "threshold_mb": cfg.policy.min_free_space_mb,
            },
            "binding": cfg.native.host_bind,
            "security_warning": cfg.native.host_bind == "0.0.0.0",
        }

    r.add("GET", r"/api/dashboard", dashboard)

    # ── 服务器控制 ──────────────────────────────────────────────────────────
    def _submit(req, kind: str, payload: dict[str, Any], *, title: str = "",
                needs_stop: bool = False, cancellable: bool = False):
        mgr = get_manager()
        conflict = mgr.active_conflict(kind)
        if conflict:
            raise ApiError(
                f"已有高风险任务正在运行：{conflict['kind_label'] if 'kind_label' in conflict else conflict['kind']}"
                f"（{conflict['id']}）。请等待其结束或先取消它。",
                409,
                extra={"conflict": conflict},
            )
        task_id = mgr.submit(kind, title=title or KIND_LABELS.get(kind, kind),
                             operator=req.actor or "admin", payload=payload,
                             needs_stop=needs_stop, cancellable=cancellable)
        app.db.audit(req.actor or "admin", kind, target=str(payload)[:200],
                     remote=req.remote)
        return {"task_id": task_id, "kind": kind}

    def server_install(req, **kw):
        return _submit(req, "server.install", req.json(), needs_stop=True)

    def server_start(req, **kw):
        app.supervisor.note_user_start() if app.supervisor else None
        return _submit(req, "server.start", req.json())

    def server_stop(req, **kw):
        app.supervisor.note_user_stop() if app.supervisor else None
        return _submit(req, "server.stop", req.json(), needs_stop=True)

    def server_restart(req, **kw):
        body = req.json()
        payload = {"skip_snapshot": not bool(body.get("snapshot", True))}
        return _submit(req, "server.restart", payload, needs_stop=True)

    def server_recreate(req, **kw):
        return _submit(req, "server.recreate", req.json(), needs_stop=True)

    def server_status(req, **kw):
        st = app.runtime.status()
        ready = app.runtime.readiness()
        return {
            "container": st.to_dict(),
            "readiness": ready,
            "stats": app.runtime.stats(),
            "ports": {
                "game": C.load_config().native.game_port,
                "webvnc": C.load_config().native.webvnc_port,
                "vnc": C.load_config().native.vnc_port,
                "cmdwebui": C.load_config().native.cmdwebui_port,
                "panel": C.load_config().panel_tcp_port,
            },
            "webvnc_url": f"http://{_host_hint()}:{C.load_config().native.webvnc_port}/vnc.html",
            "bind": C.load_config().native.host_bind,
        }

    def server_logs(req, **kw):
        source = req.q("source", "container")
        tail = req.q_int("tail", 300, low=1, high=5000)
        level = req.q("level", "")
        if source == "manager":
            from mgr.logmod import get_logger
            return {"source": "manager", "lines": get_logger("manager").tail(tail, level)}
        if source == "console":
            return {"source": "console", "lines": app.runtime.console_log().splitlines()[-tail:]}
        if source == "task":
            from mgr.logmod import get_logger
            return {"source": "task", "lines": get_logger("task").tail(tail, level)}
        text = app.runtime.logs(tail=tail)
        return {"source": "container", "lines": text.splitlines()[-tail:]}

    r.add("POST", r"/api/server/install", server_install)
    r.add("POST", r"/api/server/start", server_start)
    r.add("POST", r"/api/server/stop", server_stop)
    r.add("POST", r"/api/server/restart", server_restart)
    r.add("POST", r"/api/server/recreate", server_recreate)
    r.add("GET", r"/api/server/status", server_status)
    r.add("GET", r"/api/server/logs", server_logs)

    # ── 存档 ────────────────────────────────────────────────────────────────
    def saves_list(req, **kw):
        saves = app.saves.list_saves()
        return {
            "saves": [s.to_dict() for s in saves],
            "active": app.saves.active_save(),
            "saves_dir": str(C.SAVES_DIR),
            "idle": not app.runtime.status().running,
        }

    def save_detail(req, dir_name, **kw):
        return app.saves.inspect(dir_name).to_dict()

    def save_import(req, **kw):
        info = _receive_upload(req, kind="save-zip")
        plan = app.saves.plan_import(Path(info["path"]))
        result = _submit(req, "save.import", {
            "zip_path": info["path"],
            "dir_name": req.headers.get("X-Save-Name", "") or plan["plausible_candidate"],
            "overwrite": req.headers.get("X-Overwrite", "").lower() == "true",
        }, needs_stop=True)
        result["plan"] = plan
        return result

    def save_import_plan(req, **kw):
        info = _receive_upload(req, kind="save-zip-preview")
        plan = app.saves.plan_import(Path(info["path"]))
        return {"upload": info, "plan": plan}

    def save_export(req, **kw):
        data = req.json()
        return _submit(req, "save.export", {"dir_name": str(data.get("dir_name", ""))})

    def save_download(req, **kw):
        dir_name = req.q("name", "")
        export_dir = C.STAGING_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = export_dir / f"{dir_name}-{stamp}.zip"
        result = app.saves.export_zip(dir_name, dest, allow_running=True)
        data = dest.read_bytes()
        return Response(raw=data, content_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{dest.name}"',
        })

    def save_delete(req, **kw):
        data = req.json()
        return _submit(req, "save.delete", {
            "dir_name": str(data.get("dir_name", "")),
            "confirm": str(data.get("confirm", "")),
        }, needs_stop=True)

    def save_activate(req, **kw):
        data = req.json()
        return _submit(req, "save.activate", {
            "dir_name": str(data.get("dir_name", "")),
            "restart": bool(data.get("restart", False)),
        }, needs_stop=bool(data.get("restart", False)))

    def save_restore(req, **kw):
        data = req.json()
        return _submit(req, "save.restore", {
            "snapshot_id": str(data.get("snapshot_id", "")),
            "dir_name": str(data.get("dir_name", "")),
        }, needs_stop=True)

    r.add("GET", r"/api/saves", saves_list)
    r.add("GET", r"/api/saves/(?P<dir_name>[^/]+)", save_detail)
    r.add("POST", r"/api/saves/import", save_import)
    r.add("POST", r"/api/saves/import-plan", save_import_plan)
    r.add("POST", r"/api/saves/export", save_export)
    r.add("GET", r"/api/saves/export/download", save_download)
    r.add("POST", r"/api/saves/delete", save_delete)
    r.add("POST", r"/api/saves/activate", save_activate)
    r.add("POST", r"/api/saves/restore", save_restore)

    # ── MOD ─────────────────────────────────────────────────────────────────
    def mods_list(req, **kw):
        report = app.mods.diagnose()
        report["mods_dir"] = str(C.MODS_DIR)
        report["idle"] = not app.runtime.status().running
        return report

    def mod_detail(req, dir_name, **kw):
        return app.mods.inspect(dir_name).to_dict()

    def mod_install_plan(req, **kw):
        info = _receive_upload(req, kind="mod-zip-preview")
        plan = app.mods.plan_install(Path(info["path"]))
        return {"upload": info, "plan": plan}

    def mod_install(req, **kw):
        info = _receive_upload(req, kind="mod-zip")
        overwrite = req.headers.get("X-Overwrite", "").lower() == "true"
        kind = "mod.overwrite" if overwrite else "mod.install"
        return _submit(req, kind, {
            "zip_path": info["path"],
            "dir_name": req.headers.get("X-Mod-Dir", ""),
            "overwrite": overwrite,
        }, needs_stop=True)

    def mod_remove(req, **kw):
        data = req.json()
        return _submit(req, "mod.remove", {
            "dir_name": str(data.get("dir_name", "")),
            "keep_private_data": bool(data.get("keep_private_data", True)),
        }, needs_stop=True)

    def mod_toggle(req, **kw):
        data = req.json()
        dir_name = str(data.get("dir_name", ""))
        enable = bool(data.get("enable", False))
        return _submit(req, "mod.enable" if enable else "mod.disable",
                       {"dir_name": dir_name}, needs_stop=True)

    def mod_bundle_info(req, **kw):
        cfg = C.load_config()
        url = (f"https://github.com/{cfg.upstream_repo}/releases/download/"
               f"{cfg.upstream_baseline_tag}/Mods.zip")
        return {
            "url": url,
            "repo": cfg.upstream_repo,
            "tag": cfg.upstream_baseline_tag,
            "note": ("该包由上游发布，包含本仓库维护的 5 个 MOD。"
                     "下载后系统会显示实际 SHA-256 供你核对；"
                     "游戏本体与第三方 MOD 不在其中。"),
            "license_note": "ALOS 基于 perkmi/Always-On-Server-for-Multiplayer（MIT + 附加协议），"
                            "商业使用请署名原仓库地址。",
        }

    def mod_bundle_download(req, **kw):
        """下载上游 MOD 包到暂存区（需用户显式确认 URL）。"""
        data = req.json()
        url = str(data.get("url", "")).strip()
        cfg = C.load_config()
        allowed_prefix = f"https://github.com/{cfg.upstream_repo}/releases/download/"
        if not url.startswith(allowed_prefix):
            raise ApiError(f"只允许从上游仓库下载 MOD 包（{allowed_prefix}…）", 400)
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/releases/download/"
                            r"[A-Za-z0-9._-]+/Mods\.zip", url):
            raise ApiError("下载地址格式不合法", 400)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / f"upstream-mods-{int(time.time())}.zip"
        res = util.run(["curl", "-sSL", "--fail", "--max-time", "600", "-o", str(dest), url],
                       timeout=700)
        if not res.ok or not dest.is_file():
            raise ApiError(f"下载失败：{res.tail[:300]}", 502)
        size = dest.stat().st_size
        sha = util.sha256_file(dest)
        app.db.audit(req.actor, "download_upstream_mods", target=url, detail=sha[:64],
                     remote=req.remote)
        result = _submit(req, "mod.install-bundle", {
            "zip_path": str(dest),
            "overwrite": bool(data.get("overwrite", False)),
        }, needs_stop=True)
        result.update({"size": size, "sha256": sha, "url": url})
        return result

    def mod_upstream_status(req, **kw):
        bundle = req.json().get("task_id", "")
        _ = bundle
        return {"tasks": app.tasks.list(limit=10, kind="mod.install-bundle")}

    def mods_smapi_report(req, **kw):
        console = app.runtime.console_log(tail_bytes=500_000)
        report = app.mods.parse_smapi_log(console)
        report["console_available"] = bool(console)
        report["paths"] = {
            "console_log": str(C.GAME_DIR / "console.log"),
            "console_log": str(C.GAME_DIR / "console.log"),
        }
        return report

    # ⚠️ 路由**按注册顺序**匹配：具体路径必须排在通配路径之前。
    # 真机踩到：`/api/mods/upstream-bundle` 与 `/api/mods/smapi-log` 曾被
    # `/api/mods/{dir_name}` 吃掉，前端拿到的是一个 MOD 对象而不是下载信息，
    # 于是把一个 undefined 提交回后端，报出"只允许从上游仓库下载 MOD 包"这句
    # 与真实原因完全无关的提示（见 docs/known-issues.md I-1）。
    r.add("GET", r"/api/mods", mods_list)
    r.add("POST", r"/api/mods/install-plan", mod_install_plan)
    r.add("POST", r"/api/mods/install", mod_install)
    r.add("POST", r"/api/mods/remove", mod_remove)
    r.add("POST", r"/api/mods/toggle", mod_toggle)
    r.add("GET", r"/api/mods/upstream-bundle", mod_bundle_info)
    r.add("POST", r"/api/mods/upstream-bundle", mod_bundle_download)
    r.add("GET", r"/api/mods/smapi-log", mods_smapi_report)
    # 通配路由放最后，并显式排除上面这些保留名（双保险：即使将来有人调整顺序，
    # 也不会再把保留路径当成 MOD 目录名）
    r.add("GET",
          r"/api/mods/(?P<dir_name>(?!upstream-bundle$|smapi-log$|install-plan$"
          r"|install$|remove$|toggle$)[^/]+)",
          mod_detail)

    # ── 快照 ────────────────────────────────────────────────────────────────
    def snapshots_list(req, **kw):
        cfg = C.load_config()
        try:
            snaps = app.snapshots.list()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"读取快照列表失败：{exc}", 500)
        pinned_ids = {row["id"] for row in app.db.query(
            "SELECT id FROM snapshots WHERE pinned=1")}
        for s in snaps:
            s.pinned = s.id in pinned_ids
        return {
            "snapshots": [s.to_dict() for s in snaps],
            "policy": {
                "keep_auto": cfg.policy.keep_auto_snapshots,
                "keep_manual": cfg.policy.keep_manual_snapshots,
                "auto_enabled": cfg.policy.auto_snapshot_enabled,
                "interval_minutes": cfg.policy.auto_snapshot_interval_minutes,
                "min_free_space_mb": cfg.policy.min_free_space_mb,
                "snapshot_space_estimate_mb": cfg.policy.snapshot_space_estimate_mb,
            },
            "disk_free_mb": free_mb(C.DATA_ROOT),
            "dir": str(C.SNAPSHOT_DIR),
        }

    def snapshot_detail(req, snapshot_id, **kw):
        info = app.snapshots.info(snapshot_id)
        raw = app.snapshots.load_manifest(snapshot_id)
        data = info.to_dict()
        data["manifest"] = {
            "manifest_version": raw.get("manifest_version"),
            "schema": raw.get("schema"),
            "counts": raw.get("counts"),
            "hashes": raw.get("hashes"),
            "image_digest": raw.get("image_digest"),
            "server_stopped_for_snapshot": raw.get("server_stopped_for_snapshot"),
            "operator": raw.get("operator"),
            "mods": raw.get("mods", [])[:50],
        }
        data["files_preview"] = (raw.get("files") or [])[:200]
        data["file_total"] = len(raw.get("files") or [])
        return data

    def snapshot_create(req, **kw):
        data = req.json()
        kind = str(data.get("kind", "manual"))
        if kind not in ("manual", "auto"):
            raise ApiError("kind 必须是 manual 或 auto", 400)
        return _submit(req, "snapshot.create", {
            "kind": kind,
            "reason": str(data.get("reason", "manual")),
            "note": str(data.get("note", ""))[:400],
            "operator": req.actor or "admin",
        })

    def snapshot_verify(req, **kw):
        data = req.json()
        return _submit(req, "snapshot.verify", {
            "snapshot_id": str(data.get("snapshot_id", "")),
            "deep": bool(data.get("deep", True)),
        })

    def snapshot_restore(req, **kw):
        data = req.json()
        return _submit(req, "snapshot.restore", {
            "snapshot_id": str(data.get("snapshot_id", "")),
        }, needs_stop=True)

    def snapshot_delete(req, **kw):
        data = req.json()
        return _submit(req, "snapshot.delete", {
            "snapshot_id": str(data.get("snapshot_id", "")),
            "force": bool(data.get("force", False)),
        })

    def snapshot_download(req, **kw):
        sid = req.q("id", "")
        root = app.snapshots.path_for(sid)
        if not root.is_dir():
            raise ApiError("快照不存在", 404)
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(root), arcname=sid)
        return Response(raw=buf.getvalue(), content_type="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="{sid}.tar.gz"',
        })

    def snapshot_policy(req, **kw):
        data = req.json()
        return _submit(req, "config.apply", {"changes": {"policy": {
            k: v for k, v in data.items()
            if k in ("keep_auto_snapshots", "keep_manual_snapshots",
                     "auto_snapshot_enabled", "auto_snapshot_interval_minutes")
        }}})

    r.add("GET", r"/api/snapshots", snapshots_list)
    r.add("GET", r"/api/snapshots/(?P<snapshot_id>[A-Za-z0-9._-]+)", snapshot_detail)
    r.add("POST", r"/api/snapshots/create", snapshot_create)
    r.add("POST", r"/api/snapshots/verify", snapshot_verify)
    r.add("POST", r"/api/snapshots/restore", snapshot_restore)
    r.add("POST", r"/api/snapshots/delete", snapshot_delete)
    r.add("GET", r"/api/snapshots/download", snapshot_download)
    r.add("POST", r"/api/snapshots/policy", snapshot_policy)

    # ── 版本 ────────────────────────────────────────────────────────────────
    def versions_current(req, **kw):
        cfg = C.load_config()
        rv = app.versions.detect_runtime()
        pinned = app.versions.pinned()
        return {
            "current": rv.to_dict(),
            "game_files": app.versions.game_files_status(),
            "note": "原生部署没有镜像摘要；版本由 游戏版本 + SMAPI 版本 + MOD/存档内容哈希 标识。",
            "pinned": {
                "id": pinned["id"], "digest": pinned["image_digest"],
                "note": pinned["note"], "recorded_at": pinned["recorded_at"],
            } if pinned else None,
            "rollback_capability": app.versions.rollback_capability(),
            "limitations": app.versions.limitations(),
            "history": app.versions.history(limit=50),
        }

    def versions_history(req, **kw):
        return {"history": app.versions.history(limit=req.q_int("limit", 50, low=1, high=500))}

    def versions_updates(req, **kw):
        """检查上游发布的 MOD 包（不做 HTML 解析，走稳定 API）。"""
        out = app.versions.check_updates()
        cfg = C.load_config()
        out["local_baseline"] = cfg.upstream_baseline_tag
        return out

    def versions_record(req, **kw):
        """把当前的内容指纹写入版本档案（只读操作，走任务队列以便留痕）。"""
        return _submit(req, "version.record", {})

    def versions_rollback(req, **kw):
        """原生部署的"回滚"= 从快照恢复（没有镜像可切）。"""
        data = req.json()
        sid = str(data.get("snapshot_id", "")).strip()
        if not sid:
            snaps = app.db.query(
                "SELECT id FROM snapshots WHERE valid=1 ORDER BY created_at DESC LIMIT 1")
            if not snaps:
                raise ApiError(
                    "没有任何通过校验的快照，无法回滚。请先在「快照」页创建一个快照。", 409)
            sid = snaps[0]["id"]
        return _submit(req, "snapshot.restore", {"snapshot_id": sid})

    r.add("GET", r"/api/versions", versions_current)
    r.add("GET", r"/api/versions/history", versions_history)
    r.add("GET", r"/api/versions/updates", versions_updates)
    r.add("GET", r"/api/versions/check", versions_updates)   # 旧路径保留为别名
    r.add("POST", r"/api/versions/record", versions_record)
    r.add("POST", r"/api/versions/rollback", versions_rollback)

    # ── 配置 ────────────────────────────────────────────────────────────────
    def config_get(req, **kw):
        cfg = C.load_config()
        data = cfg.to_dict()
        data.pop("admin_password", None)
        data["_meta"] = {
            "port_hints": "避开 5666/5667（Web UI）、22、445/2049、5005/5006、8000（影视）",
            "panel_port": cfg.panel_tcp_port,
            "allowed_sections": ["container", "native", "policy"],
            "data_root": str(C.DATA_ROOT),
            "share_path": str(C.PKGHOME),
            "disk_free_mb": free_mb(C.DATA_ROOT),
        }
        data["runtime"] = app.runtime.detect()
        return data

    def config_apply(req, **kw):
        data = req.json()
        changes = data.get("changes", data)
        if not isinstance(changes, dict):
            raise ApiError("changes 必须是对象", 400)
        filtered = {k: v for k, v in changes.items()
                    if k in ("container", "native", "policy")}
        if not filtered:
            raise ApiError("没有可应用的配置变更", 400)
        return _submit(req, "config.apply", {"changes": filtered}, needs_stop=True)

    r.add("GET", r"/api/config", config_get)
    r.add("POST", r"/api/config", config_apply)

    # ── 任务 ────────────────────────────────────────────────────────────────
    def tasks_list(req, **kw):
        return {
            "tasks": app.tasks.list(
                limit=req.q_int("limit", 60, low=1, high=500),
                state=req.q("state") or None,
                kind=req.q("kind") or None,
            ),
            "stats": app.tasks.stats(),
            "kinds": {k: KIND_LABELS.get(k, k) for k in app.tasks.known_kinds()},
        }

    def task_detail(req, task_id, **kw):
        data = app.tasks.get(task_id, with_logs=True,
                             log_limit=req.q_int("logs", 500, low=1, high=5000))
        if data is None:
            raise ApiError("任务不存在", 404)
        return data

    def task_cancel(req, **kw):
        data = req.json()
        task_id = str(data.get("task_id", ""))
        app.tasks.cancel(task_id, actor=req.actor or "admin")
        return {"ok": True, "task_id": task_id}

    r.add("GET", r"/api/tasks", tasks_list)
    r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9._-]+)", task_detail)
    r.add("POST", r"/api/tasks/cancel", task_cancel)

    # ── 执行后端 / 原生部署 ─────────────────────────────────────────────────
    def runtime_info(req, **kw):
        """运行后端信息（本项目只有原生部署）。"""
        cfg = C.load_config()
        rt = app.runtime
        return {
            "mode": "native",
            "runtime_class": type(rt).__name__,
            "deployment": "原生部署（直接在本机运行）",
            "detect": rt.detect(),
            "validate": rt.validate(cfg),
            "native": {
                "xstack": xstack.tool_status(),
                "xstack_installed": xstack.is_installed(),
                "xstack_source": cfg.native.xstack_source,
                "game_root": str(C.GAME_FILES_DIR / "Stardew Valley"),
                "launcher_present": (C.GAME_FILES_DIR / "Stardew Valley"
                                     / "StardewModdingAPI").is_file(),
                "dotnet_runtime_required": False,
                "dotnet_note": "游戏与 SMAPI 是 .NET 6 自包含发布，不需要安装 .NET 运行时。",
                "mods_dir": str(C.MODS_DIR),
                "saves_dir": str(C.SAVES_DIR),
            },
        }

    def xstack_provision(req, **kw):
        """下载并解包虚拟显示栈到应用目录（不修改系统包状态）。"""
        try:
            result = xstack.provision(force=bool(req.json().get("force", False)),
                                      on_log=lambda m: LOG.info(m))
        except ValidationError as exc:
            raise ApiError(str(exc), 500)
        app.db.audit(req.actor or "admin", "provision_xstack",
                     detail=str(result.get("missing_required", "")), remote=req.remote)
        return result

    def native_gamefiles_status(req, **kw):
        cfg = C.load_config()
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        launcher = game_root / "StardewModdingAPI"
        size = 0
        if game_root.is_dir():
            for dirpath, _dirs, files in os.walk(game_root):
                for name in files:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
        return {
            "game_root": str(game_root),
            "launcher_present": launcher.is_file(),
            "size_bytes": size,
            "size_human": util.human_bytes(size),
            "mods_dir": str(C.MODS_DIR),
            "mods_linked": (game_root / "Mods").is_symlink() and
                           (game_root / "Mods").resolve() == C.MODS_DIR.resolve(),
            "dotnet_runtime_required": False,
            "requirements": [
                "**只需要上传游戏本体**（可以不带 SMAPI）：SMAPI 由管理器自己从官方 Release 获取并整合。",
                "ZIP 内需包含：`Stardew Valley.dll`（主程序集）、`Content/` 资源目录、"
                "`Stardew Valley.deps.json`（用于识别游戏版本）。",
                "游戏本体需是 **.NET 6 自包含**发布（目录内自带 `libcoreclr.so` 等），"
                "否则本机会缺少 .NET 运行时。",
                "整个游戏目录打包上传即可（解压后会自动定位程序集所在层级，支持多层嵌套）。",
                "若你的包里本来就带了 SMAPI，管理器会识别并跳过获取，不会重复下载。",
            ],
            "sources": [
                {"name": "你自己已合法拥有的游戏副本（推荐）",
                 "detail": "Steam / GOG 等平台购买后，取得游戏目录（Linux 版、.NET 自包含发布），"
                           "打包 ZIP 上传。不需要自己装 SMAPI。",
                 "url": "https://store.steampowered.com/app/413150/"},
                {"name": "上游发行的游戏镜像内已含完整游戏本体",
                 "detail": "上游把游戏本体烘焙在镜像里，路径为 /content/Stardew Valley/。"
                           "本项目不采用该方式运行，但它可以作为取得游戏文件的来源之一"
                           "（取用请自行确认授权）。",
                 "ref": f"{cfg.upstream_repo} 的发行镜像 :v26.5",
                 "digest": "sha256:d84f924108b0fed5c459f5276a56bbb47fe450c0de2db807a55990adfd58e1b5"},
            ],
            # 摊平成一个键：界面用 smapi.installed / smapi.version / smapi.game_version。
            # （曾经这里写成嵌套的 {"installed": {…}}，导致前端取 smapi.version 取不到。）
            "steam": steamtools.status(),
            "smapi": dict(smapi.status(game_root), **{
                "auto": True,
                "note": "SMAPI 由管理器从官方 GitHub Release 获取并整合到游戏目录，"
                        "版本按游戏本体版本自动挑选（发布说明里的最低游戏版本要求）。",
                "repo": smapi.SMAPI_REPO,
            }),
            "warnings": [
                "⚠️ 上游 `ValleyContent` Release 的 `Content.zip` **只是游戏资源目录**"
                "（解压后是 Animals/、Characters/ 等，不含 SMAPI 与游戏程序），"
                "**单独用它无法启动游戏**。",
                "⚠️ 上游 2.x 的 Release 里**没有游戏本体**："
                "`ValleyServer` 的 v2.0.0 只发布了 `Mods.zip` 与 `oneclick-script.zip`。"
                "不要在上面找游戏本体。",
                "本项目不打包、不再分发游戏本体与 SMAPI；请自行取得。",
            ],
        }

    def native_gamefiles_upload(req, **kw):
        """上传游戏本体 ZIP（用户自备，本项目不做任何再分发）。"""
        info = _receive_upload(req, kind="gamefiles")
        zip_path = Path(info["path"])
        plan = util.inspect_zip(zip_path)
        if not plan.ok:
            raise ApiError(f"压缩包检查未通过：{plan.reason}", 400)
        return _submit(req, "native.install-gamefiles", {
            "zip_path": info["path"],
            "sha256": info["sha256"],
            "size": info["size"],
        })

    r.add("GET", r"/api/runtime", runtime_info)
    r.add("POST", r"/api/runtime/xstack", xstack_provision)
    r.add("GET", r"/api/native/gamefiles", native_gamefiles_status)
    r.add("POST", r"/api/native/gamefiles", native_gamefiles_upload)

    def native_smapi_install(req, **kw):
        """获取 / 更新 SMAPI（用户只提供游戏本体，SMAPI 由管理器整合）。"""
        data = req.json() if req.body else {}
        payload = {
            "version": str(data.get("version", "")).strip(),
            "force": bool(data.get("force", False)),
        }
        return _submit(req, "native.install-smapi", payload)

    def native_smapi_releases(req, **kw):
        """列出可选的 SMAPI 版本（供界面选择），失败时如实报错。"""
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        game_ver = smapi.read_game_version(game_root)
        try:
            releases = smapi.list_releases(force=req.q_int("force", 0) == 1)
        except ValidationError as exc:
            raise ApiError(str(exc), 502)
        picked = None
        if game_ver:
            try:
                picked = smapi.pick_release(releases, game_ver).version
            except ValidationError:
                picked = None
        return {
            "game_version": game_ver,
            "recommended": picked,
            "installed": smapi.status(game_root),
            "releases": [r.to_dict() for r in releases[:12]],
        }

    def native_steam_status(req, **kw):
        """Steam 扫码取游戏本体的状态（工具是否就绪、是否已缓存登录、当前二维码）。"""
        return steamtools.status()

    def native_steam_prepare(req, **kw):
        """准备 DepotDownloader（运行时下载，GPL-2.0，不随本应用分发）。"""
        data = req.json() if req.body else {}
        try:
            info = steamtools.provision(force=bool(data.get("force", False)),
                                        on_log=lambda m: LOG.info(m))
        except ValidationError as exc:
            raise ApiError(str(exc), 502)
        app.db.audit(req.actor or "admin", "provision_steam_tools",
                     detail=str(info.get("version", "")), remote=req.remote)
        return info

    def native_steam_fetch(req, **kw):
        """用 Steam 手机 App 扫码登录并下载 / 更新游戏本体。"""
        data = req.json() if req.body else {}
        if not steamtools.is_installed():
            raise ApiError("还没准备好 Steam 工具，请先点「准备 Steam 工具」。", 409)
        # 长任务（下载可能几分钟到几十分钟）→ 必须可取消，否则用户只能干等；
        # 这也意味着任务处理器要周期性检查 ctx.cancelled（见 steamtools.run_fetch）。
        return _submit(req, "native.steam-fetch", {
            "start_after": bool(data.get("start_after", True)),
        }, cancellable=True)

    r.add("GET", r"/api/native/steam", native_steam_status)
    r.add("POST", r"/api/native/steam/prepare", native_steam_prepare)
    r.add("POST", r"/api/native/steam/fetch", native_steam_fetch)
    r.add("GET", r"/api/native/steam/qr", lambda req, **kw: {"qr": steamtools.read_qr_state()})

    r.add("POST", r"/api/native/smapi", native_smapi_install)
    r.add("GET", r"/api/native/smapi/releases", native_smapi_releases)

    # ── 系统 / 诊断 ─────────────────────────────────────────────────────────
    def system_info(req, **kw):
        return app.diagnostics.system_info()

    def system_health(req, **kw):
        return app.diagnostics.health()

    def system_audit(req, **kw):
        return {"audit": app.db.recent_audit(req.q_int("limit", 200, low=1, high=1000)),
                "events": app.db.recent_events(req.q_int("limit", 200, low=1, high=1000))}

    def diagnostics_bundle(req, **kw):
        return _submit(req, "diagnostics.bundle", {})

    def diagnostics_download(req, **kw):
        name = req.q("name", "")
        if not re.fullmatch(r"diagnostics-[0-9]{8}-[0-9]{6}\.tar\.gz", name):
            raise ApiError("诊断包名称不合法", 400)
        path = C.STAGING_DIR / name
        if not path.is_file():
            raise ApiError("诊断包不存在或已被清理", 404)
        return Response(raw=path.read_bytes(), content_type="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
        })

    def system_deps(req, **kw):
        return {"runtime": app.runtime.detect(), "python": _python_info()}

    r.add("GET", r"/api/system", system_info)
    r.add("GET", r"/api/system/health", system_health)
    r.add("GET", r"/api/system/audit", system_audit)
    r.add("GET", r"/api/system/deps", system_deps)
    r.add("POST", r"/api/diagnostics/bundle", diagnostics_bundle)
    r.add("GET", r"/api/diagnostics/download", diagnostics_download)

    # ── 关于 ────────────────────────────────────────────────────────────────
    def about(req, **kw):
        cfg = C.load_config()
        return {
            "manager": {
                "name": "ValleyServer 管理器",
                "appname": "valleyserver",
                "version": C.APP_VERSION,
                "license": "Apache-2.0",
                "source": f"https://github.com/{cfg.fork_repo}",
            },
            "upstream": {
                "repo": cfg.upstream_repo,
                "url": f"https://github.com/{cfg.upstream_repo}",
                "baseline_commit": cfg.upstream_baseline_commit,
                "baseline_tag": cfg.upstream_baseline_tag,
                "license": "Apache-2.0",
                "mode": "2.x（真实游戏客户端 + SMAPI + 自动化 MOD）",
            },
            "deployment": {
                "mode": "原生部署",
                "note": "管理器直接在本机运行 游戏客户端 + SMAPI + 自动化 MOD + 虚拟显示栈。"
                        "游戏本体与 SMAPI 由用户自备，本项目不再分发。",
                "display_stack": "Xvfb / openbox / x11vnc / websockify + noVNC，"
                                 "由管理器捆绑在应用目录内（不修改系统包状态）",
                "dotnet": "不需要安装 .NET 运行时（游戏与 SMAPI 为 .NET 6 自包含发布）",
            },
            "third_party": [
                {"name": "Stardew Valley", "license": "专有（ConcernedApe）",
                 "note": "不在本包内，也不再分发；由用户自备游戏文件。"},
                {"name": "SMAPI", "license": "MIT",
                 "note": "随游戏本体由用户提供，与本管理器分开发行。"},
                {"name": "Xvfb / openbox / x11vnc / websockify / noVNC",
                 "license": "MIT / GPL-2.0 / GPL-2.0 / BSD-2-Clause / MPL-2.0",
                 "note": "虚拟显示栈，由管理器按 Debian 仓库原样下载并解包到应用数据目录，"
                         "不修改系统包状态；随应用卸载一并删除。"},
                {"name": "ALOS", "license": "MIT + 附加协议",
                 "note": "基于 perkmi/Always-On-Server-for-Multiplayer；商业使用须署名原仓库。"},
                {"name": "ServerCMD / ChatCommand / CommandWebUI / ChangeServerPort",
                 "license": "Apache-2.0", "note": "上游 Lixeer/ValleyServer 维护。"},
                {"name": "Pathoschild.Stardew.ModBuildConfig", "license": "MIT"},
                {"name": "Lib.Harmony", "license": "MIT"},
                {"name": "Lidgren.Network", "license": "MIT"},
                {"name": "EmbedIO", "license": "MIT"},
            ],
            "disclaimer": [
                "本管理器不打包、不再分发游戏本体与任何第三方 MOD；游戏文件由用户自备。",
                "上游 2.x 处于维护模式，仅做修复性维护。",
                "本项目为原生部署；上游发行物仅用于审计取证，不作为运行依赖。",
            ],
            "audit_reference": {
                "note": "以下是从上游镜像里核实的事实，仅用于说明上游基线；本项目不依赖该镜像运行。",
                "upstream_game_version": cfg.upstream_game_version,
                "upstream_smapi_version": cfg.upstream_smapi_version,
                "image_baked_content": True,
                "image_has_healthcheck": False,
            },
        }

    r.add("GET", r"/api/about", about)

    # ── 上传辅助 ────────────────────────────────────────────────────────────
    def _receive_upload(req, *, kind: str) -> dict[str, Any]:
        if not req.body:
            raise ApiError("请求体为空，未收到文件", 400)
        name = req.headers.get("X-Filename", "") or "upload.zip"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[-80:]
        if not safe.lower().endswith((".zip", ".tar.gz", ".tgz")):
            raise ApiError("只接受 .zip / .tar.gz / .tgz 格式", 400)
        if len(req.body) > MAX_UPLOAD:
            raise ApiError("文件过大", 413)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / f"{kind}-{int(time.time())}-{os.getpid()}-{safe}"
        atomic_write_bytes(dest, req.body, mode=0o600)
        LOG.info(f"收到上传 {kind}: {safe}（{len(req.body)} 字节）→ {dest}")
        return {"path": str(dest), "name": safe, "size": len(req.body),
                "sha256": util.sha256_file(dest)}


def _host_hint() -> str:
    """给出一个可读的访问地址提示（不保证在所有网络环境下正确，仅作提示）。"""
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.connect(("223.5.5.5", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "<NAS-IP>"


def _python_info() -> dict[str, Any]:
    import sys
    from shutil import which
    return {
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "executable": sys.executable,
        "python312": which("python3.12") or "",
        "python3": which("python3") or "",
    }
