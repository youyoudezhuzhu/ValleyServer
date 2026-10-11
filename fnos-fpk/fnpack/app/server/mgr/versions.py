"""版本档案、更新检查、固定与回滚。

核心原则（项目要求 5.5）：
  * 版本档案必须记录**实际内容**，不能只保存会随时间变化的 `latest` 标签。
    本项目记录 `image@sha256:<digest>` + 游戏版本 + SMAPI 版本 + MOD 哈希。
  * 更新前必须创建完整快照、检查磁盘空间、运行状态与恢复能力。
  * 更新后必须做健康检查、SMAPI 加载检查与存档可读取性检查。
  * 更新失败时保留故障日志与新版本现场，并尝试恢复旧镜像/配置/MOD/存档。
  * **不伪称**「可回滚到任意历史版本」：只能回滚到本地已保留的镜像摘要。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as C
from .snapshots import hash_tree
from .util import (
    ValidationError, atomic_write_json, free_mb, read_json, run, sha256_file,
    validate_image_ref,
)


@dataclass
class RuntimeVersion:
    image_ref: str = ""
    image_digest: str = ""
    image_created: str = ""
    image_size: int = 0
    game_version: str = ""
    smapi_version: str = ""
    mods_hash: str = ""
    mod_count: int = 0
    saves_hash: str = ""
    save_count: int = 0
    recorded_at: float = 0.0
    source: str = "runtime"

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_ref": self.image_ref, "image_digest": self.image_digest,
            "image_created": self.image_created, "image_size": self.image_size,
            "game_version": self.game_version, "smapi_version": self.smapi_version,
            "mods_hash": self.mods_hash, "mod_count": self.mod_count,
            "saves_hash": self.saves_hash, "save_count": self.save_count,
            "recorded_at": self.recorded_at,
            "recorded_at_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(self.recorded_at)) if self.recorded_at else "",
            "source": self.source,
        }


class VersionManager:
    def __init__(self, compose=None) -> None:
        self.compose = compose

    # ── 探测当前运行版本 ────────────────────────────────────────────────────
    def detect_runtime(self, *, from_image: bool = True) -> RuntimeVersion:
        cfg = C.load_config()
        rv = RuntimeVersion(recorded_at=time.time(), source="image" if from_image else "config")
        if self.compose is None:
            return rv
        st = self.compose.status()
        ref = st.image or cfg.container.image
        if not ref:
            return rv
        if "@" not in ref:
            ref_with_tag = f"{cfg.container.image}:{cfg.container.image_tag}"
            rv.image_ref = ref_with_tag
            rv.image_digest = self.compose.image_digest(ref_with_tag) or cfg.container.image_digest
        else:
            rv.image_ref = ref
            rv.image_digest = ref.split("@", 1)[1]
        if not rv.image_digest:
            rv.image_digest = cfg.container.image_digest
        inspect_ref = rv.image_ref
        info = self.compose.image_inspect(inspect_ref)
        if info:
            rv.image_created = str(info.get("Created", ""))
            rv.image_size = int(info.get("Size", 0) or 0)
        # 游戏 / SMAPI 版本只能从容器内读取（镜像内已烘焙）
        game, smapi = "", ""
        if st.running:
            res = self.compose.exec_in_container(
                ["sh", "-c",
                 "strings '/content/Stardew Valley/StardewModdingAPI.dll' 2>/dev/null | "
                 "grep -oE '^4\\.[0-9]+\\.[0-9]+$' | sort -u | tail -1"],
                timeout=60,
            )
            if res.ok:
                smapi = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
            res = self.compose.exec_in_container(
                ["sh", "-c",
                 "strings '/content/Stardew Valley/Stardew Valley.dll' 2>/dev/null | "
                 "grep -oE '1\\.6\\.[0-9]+' | sort -u | tail -1"],
                timeout=60,
            )
            if res.ok:
                game = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        rv.game_version = game
        rv.smapi_version = smapi
        try:
            rv.mods_hash, rv.mod_count = hash_tree(C.MODS_DIR, exclude_names={"data"})
            rv.saves_hash, rv.save_count = hash_tree(C.SAVES_DIR)
        except OSError:
            pass
        return rv

    # ── 持久化档案 ──────────────────────────────────────────────────────────
    def record(self, rv: RuntimeVersion, *, note: str = "", pinned: bool = False) -> int:
        from .db import get_db
        db = get_db()
        with db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO versions(recorded_at,image_ref,image_digest,game_version,"
                "smapi_version,mods_hash,pinned,note,source) VALUES(?,?,?,?,?,?,?,?,?)",
                (rv.recorded_at or time.time(), rv.image_ref, rv.image_digest,
                 rv.game_version, rv.smapi_version, rv.mods_hash,
                 1 if pinned else 0, note, rv.source),
            )
            vid = int(cur.lastrowid or 0)
        # 同时落一份 JSON 档案，便于离线查看
        C.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(rv.recorded_at or time.time()))
        atomic_write_json(C.ARCHIVE_DIR / f"version-{stamp}-{vid}.json", {
            **rv.to_dict(), "note": note, "pinned": pinned,
        }, mode=0o640)
        return vid

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        from .db import get_db
        rows = get_db().query(
            "SELECT * FROM versions ORDER BY recorded_at DESC LIMIT ?", (int(limit),)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["recorded_at_iso"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(d["recorded_at"]))
            out.append(d)
        return out

    def pinned(self) -> dict[str, Any] | None:
        from .db import get_db
        row = get_db().one("SELECT * FROM versions WHERE pinned=1 ORDER BY recorded_at DESC LIMIT 1")
        return dict(row) if row else None

    def set_pinned(self, version_id: int, pinned: bool) -> None:
        from .db import get_db
        db = get_db()
        with db.tx() as conn:
            if pinned:
                conn.execute("UPDATE versions SET pinned=0")
            conn.execute("UPDATE versions SET pinned=? WHERE id=?",
                         (1 if pinned else 0, int(version_id)))

    # ── 更新检查（不依赖不稳定网页解析） ───────────────────────────────────
    def check_updates(self) -> dict[str, Any]:
        """检查上游镜像是否存在更新。

        数据源（均为官方/稳定 API，不做 HTML 解析）：
          1. Docker Hub Registry v2 API：列举 tag 与 manifest digest。
          2. GitHub Releases API：上游 MOD 包版本。
        """
        cfg = C.load_config()
        out: dict[str, Any] = {
            "checked_at": time.time(),
            "image": {"repo": cfg.container.image, "current_tag": cfg.container.image_tag,
                      "current_digest": cfg.container.image_digest,
                      "available_tags": [], "newer_digest": "", "update_available": False,
                      "error": "", "note": ""},
            "mods_bundle": {"repo": cfg.upstream_repo, "latest_release": "",
                            "download_url": "", "error": "", "update_available": False},
            "limits": self.limitations(),
        }
        # ── 镜像 ──
        try:
            repo = validate_image_ref(cfg.container.image)
            if "/" not in repo:
                repo = f"library/{repo}"
            token_res = run([
                "curl", "-sS", "--max-time", "20",
                f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull",
            ], timeout=40)
            token = ""
            if token_res.ok:
                try:
                    token = json.loads(token_res.stdout).get("token", "")
                except json.JSONDecodeError:
                    token = ""
            if token:
                tags_res = run([
                    "curl", "-sS", "--max-time", "20",
                    f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100",
                ], timeout=40)
                if tags_res.ok:
                    try:
                        data = json.loads(tags_res.stdout)
                        tags = []
                        for item in (data.get("results") or []):
                            name = item.get("name", "")
                            images = item.get("images") or []
                            digest = images[0].get("digest") if images else ""
                            tags.append({
                                "tag": name,
                                "pushed": item.get("last_updated", ""),
                                "digest": digest,
                                "size": item.get("full_size", 0),
                            })
                        out["image"]["available_tags"] = tags
                        current = next(
                            (t for t in tags if t["tag"] == cfg.container.image_tag), None
                        )
                        if current and current["digest"] and \
                                current["digest"] != cfg.container.image_digest:
                            out["image"]["newer_digest"] = current["digest"]
                            out["image"]["update_available"] = True
                            out["image"]["note"] = (
                                f"上游 tag「{cfg.container.image_tag}」的摘要已变化："
                                "内容被重新推送，与本地记录不一致。"
                            )
                        elif not current:
                            out["image"]["note"] = (
                                f"上游已不存在 tag「{cfg.container.image_tag}」；"
                                "当前运行的镜像只能靠本地保留。"
                            )
                    except json.JSONDecodeError as exc:
                        out["image"]["error"] = f"解析 Docker Hub 响应失败：{exc}"
                else:
                    out["image"]["error"] = tags_res.tail[:200]
            else:
                out["image"]["error"] = "无法获取 Docker Hub 匿名令牌（网络不可达？）"
        except (ValidationError, OSError) as exc:
            out["image"]["error"] = str(exc)

        # ── 上游 MOD 包 ──
        try:
            rel_res = run([
                "curl", "-sS", "--max-time", "20", "-H", "Accept: application/vnd.github+json",
                f"https://api.github.com/repos/{cfg.upstream_repo}/releases?per_page=20",
            ], timeout=40)
            if rel_res.ok:
                try:
                    rels = json.loads(rel_res.stdout)
                    if isinstance(rels, list) and rels:
                        latest = rels[0]
                        out["mods_bundle"]["latest_release"] = latest.get("tag_name", "")
                        for asset in latest.get("assets", []):
                            if asset.get("name", "").lower() == "mods.zip":
                                out["mods_bundle"]["download_url"] = asset.get("browser_download_url", "")
                                out["mods_bundle"]["size"] = asset.get("size", 0)
                                break
                        out["mods_bundle"]["update_available"] = bool(
                            out["mods_bundle"]["latest_release"]
                            and out["mods_bundle"]["latest_release"] != cfg.upstream_baseline_tag
                        )
                except json.JSONDecodeError as exc:
                    out["mods_bundle"]["error"] = f"解析 GitHub 响应失败：{exc}"
            else:
                out["mods_bundle"]["error"] = rel_res.tail[:200]
        except OSError as exc:
            out["mods_bundle"]["error"] = str(exc)
        return out

    def limitations(self) -> list[str]:
        """明确披露的能力边界，避免用户误以为可以做完整回滚。"""
        return [
            "上游 Docker Hub 仅发布了单一镜像标签（v26.5），因此**不存在**来自上游的历史版本可供回滚；"
            "回滚只能回到本地已保留的镜像摘要。",
            "SMAPI 与游戏本体被烘焙在同一镜像内，无法单独升级或降级 SMAPI。",
            "管理器的回滚 = 旧镜像 + 配置 + MOD + 存档四者一起恢复；"
            "若旧镜像已被本地删除，则只能恢复配置/MOD/存档，不能恢复游戏本体。",
        ]

    # ── 回滚能力评估 ────────────────────────────────────────────────────────
    def rollback_capability(self, *, target_digest: str = "") -> dict[str, Any]:
        """回答「现在能不能回滚」以及缺什么。"""
        cfg = C.load_config()
        digest = target_digest or self._previous_digest()
        out: dict[str, Any] = {
            "can_rollback": False,
            "target_digest": digest,
            "image_present": False,
            "missing": [],
            "notes": self.limitations(),
        }
        if not digest:
            out["missing"].append("没有任何已记录的旧版本摘要可供回滚")
            return out
        ref = f"{cfg.container.image}@{digest}"
        res = run(["docker", "image", "inspect", ref], timeout=60)
        out["image_present"] = res.ok
        if not res.ok:
            out["missing"].append(
                f"本地没有镜像 {ref}；需要重新拉取（上游可能已删除该摘要）。"
            )
        if self.compose is None:
            out["missing"].append("Docker 管理器不可用")
            return out
        if free_mb(C.DATA_ROOT) < cfg.policy.min_free_space_mb:
            out["missing"].append("磁盘可用空间低于策略下限，无法安全执行回滚")
        out["can_rollback"] = out["image_present"] and not out["missing"]
        return out

    def _previous_digest(self) -> str:
        from .db import get_db
        cfg = C.load_config()
        row = get_db().one(
            "SELECT image_digest FROM versions WHERE image_digest<>'' AND image_digest<>? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (cfg.container.image_digest,),
        )
        return row["image_digest"] if row else ""


def _unused(*_a: Any) -> None:  # pragma: no cover
    """占位，避免未使用导入告警。"""
    _ = (_a, read_json, sha256_file)
