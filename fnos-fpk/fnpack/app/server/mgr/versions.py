"""版本档案：记录**实际运行内容**，而不是会漂移的标签。

原生部署下没有镜像摘要，因此版本指纹由这些信息组成：
  * 游戏版本（从容器外的真实二进制里读，见 `read_game_version`）
  * SMAPI 版本（同上；也可从控制台日志解析）
  * MOD 目录内容哈希 + MOD 数量
  * 存档目录内容哈希 + 存档数量
  * 游戏本体目录（路径 + 文件数 + 大小）

「更新」在原生部署里的含义是**替换游戏本体文件**，因此：
  * 更新前必须创建快照（含游戏本体与 MOD/存档）；
  * 更新后必须做 SMAPI 加载检查；
  * 失败时用快照回滚（而不是回滚镜像）。

本项目不提供按镜像摘要固定/回滚的能力：回滚一律通过**快照**完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as C
from .snapshots import hash_tree
from .util import atomic_write_json, free_mb, read_json, run, sha256_file

# 从二进制里匹配版本号（与审计阶段用的方法一致）
_STRINGS_RE_CACHE: dict[str, str] = {}


@dataclass
class RuntimeVersion:
    image_ref: str = ""          # 原生模式下放游戏本体目录（保留字段名以兼容档案格式）
    image_digest: str = ""       # 原生模式下留空（没有镜像摘要）
    image_created: str = ""      # 游戏本体目录的修改时间
    image_size: int = 0          # 游戏本体目录大小
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


def read_dll_version(dll: Path, pattern: str) -> str:
    """用 `strings` 从 .NET 二进制里读版本号。

    这是审计阶段用来确认「镜像内游戏 1.6.15 / SMAPI 4.5.1」的同一套方法；
    原生部署下游戏文件就在本机，直接读即可。
    """
    if not dll.is_file():
        return ""
    key = f"{dll}:{pattern}"
    if key in _STRINGS_RE_CACHE:
        return _STRINGS_RE_CACHE[key]
    version = ""
    res = run(["strings", "-a", str(dll)], timeout=60)
    if res.ok:
        found = re.findall(pattern, res.stdout)
        if found:
            version = sorted(set(found))[-1]
    _STRINGS_RE_CACHE[key] = version
    return version


def read_version_from_console(console_text: str) -> tuple[str, str]:
    """从 SMAPI 控制台日志解析版本（最权威，因为那是 SMAPI 自己打印的）。"""
    if not console_text:
        return "", ""
    smapi = game = ""
    m = re.search(r"SMAPI\s+(\d+\.\d+\.\d+[^\s]*)", console_text)
    if m:
        smapi = m.group(1)
    m = re.search(r"Stardew Valley\s+(\d+\.\d+\.\d+[^\s]*)", console_text)
    if m:
        game = m.group(1)
    return game, smapi


class VersionManager:
    def __init__(self, runtime=None) -> None:
        self.runtime = runtime

    # ── 探测当前运行内容 ────────────────────────────────────────────────────
    def game_root(self) -> Path:
        return C.GAME_FILES_DIR / "Stardew Valley"

    def detect_runtime(self, *, from_image: bool = True) -> RuntimeVersion:
        rv = RuntimeVersion(recorded_at=time.time(), source="native")
        root = self.game_root()
        if root.is_dir():
            rv.image_ref = str(root)
            try:
                st = root.stat()
                rv.image_created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
            except OSError:
                pass
            total = 0
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
            rv.image_size = total

        # 1) 优先用控制台日志里 SMAPI 自己报的版本
        game = smapi = ""
        if self.runtime is not None:
            try:
                game, smapi = read_version_from_console(self.runtime.console_log())
            except Exception:  # noqa: BLE001
                pass
        # 2) 回落到直接读二进制里的版本字符串
        if not game:
            game = read_dll_version(root / "Stardew Valley.dll", r"\b1\.\d+\.\d+\b")
        if not smapi:
            smapi = read_dll_version(root / "StardewModdingAPI.dll", r"\b4\.\d+\.\d+\b")
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
        C.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(rv.recorded_at or time.time()))
        atomic_write_json(C.ARCHIVE_DIR / f"version-{stamp}-{vid}.json", {
            **rv.to_dict(), "note": note, "pinned": pinned,
        }, mode=0o640)
        return vid

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        from .db import get_db
        rows = get_db().query(
            "SELECT * FROM versions ORDER BY recorded_at DESC LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            d = dict(r)
            d["recorded_at_iso"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(d["recorded_at"]))
            out.append(d)
        return out

    def pinned(self) -> dict[str, Any] | None:
        from .db import get_db
        row = get_db().one(
            "SELECT * FROM versions WHERE pinned=1 ORDER BY recorded_at DESC LIMIT 1")
        return dict(row) if row else None

    def set_pinned(self, version_id: int, pinned: bool) -> None:
        from .db import get_db
        db = get_db()
        with db.tx() as conn:
            if pinned:
                conn.execute("UPDATE versions SET pinned=0")
            conn.execute("UPDATE versions SET pinned=? WHERE id=?",
                         (1 if pinned else 0, int(version_id)))

    # ── 上游 MOD 包检查（稳定 API，不做 HTML 解析） ──────────────────────────
    def check_updates(self) -> dict[str, Any]:
        cfg = C.load_config()
        out: dict[str, Any] = {
            "checked_at": time.time(),
            "mode": "native",
            "game_files": self.game_files_status(),
            "mods_bundle": {"repo": cfg.upstream_repo, "latest_release": "",
                            "download_url": "", "error": "", "update_available": False},
            "limitations": self.limitations(),
        }
        try:
            res = run([
                "curl", "-sS", "--max-time", "20", "-H", "Accept: application/vnd.github+json",
                f"https://api.github.com/repos/{cfg.upstream_repo}/releases?per_page=20",
            ], timeout=40)
            if res.ok:
                rels = json.loads(res.stdout)
                if isinstance(rels, list) and rels:
                    latest = rels[0]
                    out["mods_bundle"]["latest_release"] = latest.get("tag_name", "")
                    for asset in latest.get("assets", []):
                        if asset.get("name", "").lower() == "mods.zip":
                            out["mods_bundle"]["download_url"] = asset.get(
                                "browser_download_url", "")
                            out["mods_bundle"]["size"] = asset.get("size", 0)
                            break
                    out["mods_bundle"]["update_available"] = bool(
                        out["mods_bundle"]["latest_release"]
                        and out["mods_bundle"]["latest_release"] != cfg.upstream_baseline_tag)
            else:
                out["mods_bundle"]["error"] = res.tail[:200]
        except (OSError, json.JSONDecodeError) as exc:
            out["mods_bundle"]["error"] = str(exc)
        return out

    def game_files_status(self) -> dict[str, Any]:
        root = self.game_root()
        launcher = root / "StardewModdingAPI"
        size = 0
        files = 0
        if root.is_dir():
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(dirpath, n))
                    except OSError:
                        continue
        return {
            "root": str(root),
            "launcher_present": launcher.is_file(),
            "file_count": files,
            "size_bytes": size,
            "content_present": (root / "Content").is_dir(),
            "mods_linked": (root / "Mods").is_symlink()
                           and (root / "Mods").resolve() == C.MODS_DIR.resolve(),
        }

    def limitations(self) -> list[str]:
        """明确披露原生部署下的能力边界，避免用户误以为有镜像级回滚。"""
        return [
            "原生部署没有镜像摘要，因此**不存在**「按镜像回滚」这种能力；"
            "版本回滚通过**快照**完成（把游戏本体、MOD、存档与配置一起恢复到某个时点）。",
            "「更新游戏」= 替换游戏本体文件。新版文件需要由你自行获取（本项目不再分发游戏本体）。",
            "SMAPI 随游戏本体一起替换，无法单独升降级。",
            "更新前必须创建快照；快照失败则更新会被中止。",
        ]

    def rollback_capability(self, *, target_digest: str = "") -> dict[str, Any]:
        """原生部署下的"回滚能力"回答：用快照，而不是镜像。"""
        from .db import get_db
        snaps = get_db().query(
            "SELECT id, created_at, valid, kind FROM snapshots ORDER BY created_at DESC LIMIT 20")
        usable = [s for s in snaps if s["valid"]]
        out = {
            "method": "snapshot",
            "can_rollback": bool(usable),
            "snapshot_count": len(usable),
            "latest_snapshot": (dict(usable[0]) if usable else None),
            "missing": [] if usable else [
                "没有任何通过校验的快照；请先在「快照」页创建一个，"
                "之后所有高风险变更都会自动先快照。"],
            "notes": self.limitations(),
        }
        if free_mb(C.DATA_ROOT) < C.load_config().policy.min_free_space_mb:
            out["missing"].append("磁盘可用空间低于策略下限，无法安全执行回滚")
            out["can_rollback"] = False
        return out


def _unused(*_a: Any) -> None:  # pragma: no cover - 占位，避免未使用导入告警
    _ = (_a, read_json, sha256_file, hashlib, field)
