"""快照系统：独立、可测试的核心模块（不散落在按钮处理函数里）。

一致性协议（项目要求 5.4，逐步实现于 `create_snapshot`）：
  1. 检查服务器状态并锁定当前变更任务（由调用方持有高风险任务锁）
  2. 必要时安全停服，确保游戏不再写入存档
  3. 创建快照到临时目录
  4. 生成文件清单与校验值
  5. 检查必要文件、大小、校验结果和元信息
  6. 全部通过后原子提交快照并标记为有效
  7. 任一步骤失败都不能标记为成功，也不得继续原定高风险操作

**绝不只是复制正在运行的存档然后宣称一致**：只要容器在运行，就必须先停服并
确认进程退出（见 `pause_for_snapshot`）。

manifest 采用带版本号的 JSON（`manifest_version`），并保留迁移函数
`migrate_manifest`，以便后续 schema 演进时旧快照仍可恢复。
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config as C
from .util import (
    ValidationError, atomic_write_json, dir_size_bytes, disk_usage, free_mb,
    is_subpath, read_json, sha256_file, validate_name,
)

MANIFEST_VERSION = 1
SNAPSHOT_SCHEMA = "valleyserver.snapshot"
SNAPSHOT_KINDS = ("auto", "manual")
REASONS = {
    "manual": "用户手动创建",
    "schedule": "定时自动快照",
    "pre-mod-install": "安装 MOD 前",
    "pre-mod-update": "更新 MOD 前",
    "pre-mod-remove": "卸载 MOD 前",
    "pre-mod-overwrite": "覆盖安装 MOD 前",
    "pre-mod-disable": "禁用 MOD 前",
    "pre-mod-enable": "启用 MOD 前",
    "pre-save-import": "导入存档前",
    "pre-save-delete": "删除存档前",
    "pre-save-activate": "切换存档前",
    "pre-save-restore": "恢复存档前",
    "pre-server-update": "更新运行版本前",
    "pre-image-update": "更新运行镜像前",
    "pre-config-change": "修改关键配置前",
    "pre-snapshot-restore": "从快照恢复前",
    "pre-uninstall": "卸载前",
}

# 快照必须包含的内容根（相对 DATA_ROOT）
INCLUDE_DIRS = ("Saves", "Mods")
# 额外收集的文件（相对 DATA_ROOT）
INCLUDE_FILES = ("game/docker-compose.yaml", "game/entrypoint.sh")
# 可选：MOD 的 manifest 与校验值单独落到 mods/ 便于快速比对
MODS_META_DIR = "mods"


class SnapshotError(RuntimeError):
    pass


class SnapshotInconsistent(SnapshotError):
    """快照内容与 manifest 不符；恢复必须拒绝并保留现场。"""


@dataclass
class FileEntry:
    path: str
    size: int
    sha256: str
    mode: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256, "mode": self.mode}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FileEntry":
        return cls(
            path=str(raw["path"]),
            size=int(raw.get("size", 0)),
            sha256=str(raw.get("sha256", "")),
            mode=int(raw.get("mode", 0)),
        )


@dataclass
class SnapshotInfo:
    id: str
    kind: str
    reason: str
    created_at: float
    size_bytes: int = 0
    file_count: int = 0
    valid: bool = False
    manifest_version: int = MANIFEST_VERSION
    game_version: str = ""
    smapi_version: str = ""
    image_ref: str = ""
    image_digest: str = ""
    mods_hash: str = ""
    saves_hash: str = ""
    note: str = ""
    task_id: str = ""
    missing: bool = False
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "reason": self.reason,
            "reason_label": REASONS.get(self.reason, self.reason),
            "created_at": self.created_at,
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)),
            "size_bytes": self.size_bytes, "file_count": self.file_count,
            "valid": self.valid, "manifest_version": self.manifest_version,
            "game_version": self.game_version, "smapi_version": self.smapi_version,
            "image_ref": self.image_ref, "image_digest": self.image_digest,
            "mods_hash": self.mods_hash, "saves_hash": self.saves_hash,
            "note": self.note, "task_id": self.task_id, "missing": self.missing,
            "pinned": self.pinned,
        }


def _hash_tree(root: Path, *, exclude_names: Iterable[str] = ()) -> tuple[str, int]:
    """对目录做确定性内容哈希（相对路径 + 大小 + 内容 sha256）。

    返回 (聚合哈希, 文件数)。用于快速判断 Saves/Mods 是否变化。
    """
    import hashlib
    exclude = set(exclude_names)
    entries: list[tuple[str, int, str]] = []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude)
            for name in sorted(filenames):
                if name in exclude:
                    continue
                fp = Path(dirpath) / name
                try:
                    if fp.is_symlink():
                        continue
                    rel = fp.relative_to(root).as_posix()
                    entries.append((rel, fp.stat().st_size, sha256_file(fp)))
                except OSError:
                    continue
    h = hashlib.sha256()
    for rel, size, digest in entries:
        h.update(f"{rel}\0{size}\0{digest}\n".encode("utf-8"))
    return h.hexdigest(), len(entries)


def hash_tree(root: Path, *, exclude_names: Iterable[str] = ()) -> tuple[str, int]:
    return _hash_tree(root, exclude_names=exclude_names)


class SnapshotStore:
    """快照仓库。所有方法都是幂等/可重试的安全操作。"""

    def __init__(self, root: Path | None = None, data_root: Path | None = None,
                 compose=None) -> None:
        self.root = Path(root) if root else C.SNAPSHOT_DIR
        self.data_root = Path(data_root) if data_root else C.DATA_ROOT
        self.staging = Path(C.STAGING_DIR)
        self._compose = compose

    # ── 基础设施 ────────────────────────────────────────────────────────────
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def path_for(self, snapshot_id: str) -> Path:
        sid = validate_snapshot_id(snapshot_id)
        return self.root / sid

    def manifest_path(self, snapshot_id: str) -> Path:
        return self.path_for(snapshot_id) / "manifest.json"

    # ── 停服/恢复服务（一致性前提） ────────────────────────────────────────
    def pause_for_snapshot(self, *, on_log: Callable[[str], None] | None = None) -> bool:
        """必要时停服，并确认游戏进程已退出。返回「是否被本方法停止」。"""
        log = on_log or (lambda _m: None)
        if self._compose is None:
            return False
        st = self._compose.status()
        if not st.exists or not st.running:
            log("容器未在运行，无需停服")
            return False
        log("正在安全停服（避免快照到正在写入的存档）…")
        res = self._compose.stop(timeout=180)
        if not res.ok:
            raise SnapshotError(f"停服失败，拒绝在运行状态下创建快照：{res.tail[:500]}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            st = self._compose.status()
            if not st.running:
                log("容器已停止，可以安全快照")
                return True
            time.sleep(2)
        raise SnapshotError("停服后容器仍在运行，拒绝创建快照（无法保证一致性）")

    def resume_after(self, was_running: bool, *, on_log: Callable[[str], None] | None = None) -> None:
        if not was_running or self._compose is None:
            return
        log = on_log or (lambda _m: None)
        log("正在重新启动服务器…")
        res = self._compose.start(timeout=300)
        if not res.ok:
            log(f"重启失败（可由「启动」按钮重试）：{res.tail[:300]}")

    # ── 创建 ────────────────────────────────────────────────────────────────
    def create(self, *, kind: str = "manual", reason: str = "manual",
               task_id: str = "", note: str = "", operator: str = "admin",
               version_info: dict[str, Any] | None = None,
               stop_server: bool = True,
               on_log: Callable[[str], None] | None = None) -> SnapshotInfo:
        """按一致性协议创建快照。

        `stop_server=True` 时会在容器运行时先停服（默认；这是唯一能保证一致的方式）。
        调用方必须已持有高风险任务锁（由 TaskManager 保证）。
        """
        log = on_log or (lambda _m: None)
        if kind not in SNAPSHOT_KINDS:
            raise ValidationError(f"快照类型必须是 {SNAPSHOT_KINDS}")
        self.ensure()
        self._guard_space()

        snapshot_id = self._new_id(kind)
        staging = self.staging / f"{snapshot_id}.part"
        final = self.path_for(snapshot_id)

        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if final.exists():
            raise SnapshotError(f"快照 ID 冲突：{snapshot_id}")

        was_running = False
        try:
            if stop_server:
                was_running = self.pause_for_snapshot(on_log=log)

            log("步骤 3/6：复制数据到临时目录…")
            staging.mkdir(parents=True, exist_ok=True)
            payload = staging / "payload"
            payload.mkdir(parents=True, exist_ok=True)
            entries: list[FileEntry] = []
            total = 0

            for rel in INCLUDE_DIRS:
                src = self.data_root / rel
                if not src.is_dir():
                    log(f"  跳过不存在的目录：{rel}")
                    continue
                dst = payload / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)
                log(f"  已复制 {rel}/")

            for rel in INCLUDE_FILES:
                src = self.data_root / rel
                if src.is_file():
                    dst = payload / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            log("步骤 4/6：生成文件清单与校验值…")
            for dirpath, dirnames, filenames in os.walk(payload):
                dirnames.sort()
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    if fp.is_symlink():
                        continue
                    rel = fp.relative_to(payload).as_posix()
                    st = fp.stat()
                    digest = sha256_file(fp)
                    entries.append(FileEntry(rel, st.st_size, digest, st.st_mode & 0o777))
                    total += st.st_size

            log(f"  清单共 {len(entries)} 个文件、{total} 字节")

            log("步骤 5/6：校验必要内容与元信息…")
            self._validate_payload(payload, entries)

            mods_hash, mods_count = hash_tree(payload / "Mods", exclude_names={"data"})
            saves_hash, saves_count = hash_tree(payload / "Saves")

            vi = dict(version_info or {})
            manifest = {
                "schema": SNAPSHOT_SCHEMA,
                "manifest_version": MANIFEST_VERSION,
                "id": snapshot_id,
                "kind": kind,
                "reason": reason,
                "reason_label": REASONS.get(reason, reason),
                "created_at": time.time(),
                "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "operator": operator,
                "task_id": task_id,
                "note": note,
                "server_stopped_for_snapshot": bool(stop_server),
                "game_version": str(vi.get("game_version", "")),
                "smapi_version": str(vi.get("smapi_version", "")),
                "image_ref": str(vi.get("image_ref", "")),
                "image_digest": str(vi.get("image_digest", "")),
                "image_created": str(vi.get("image_created", "")),
                "mods": vi.get("mods", []),
                "manager_version": C.APP_VERSION,
                "counts": {
                    "files": len(entries),
                    "bytes": total,
                    "mod_dirs": mods_count,
                    "save_dirs": saves_count,
                },
                "hashes": {"mods": mods_hash, "saves": saves_hash},
                "files": [e.to_dict() for e in entries],
                "committed": False,
                "checksum_algorithm": "sha256",
            }
            atomic_write_json(staging / "manifest.json", manifest, mode=0o640)

            log("步骤 6/6：原子提交快照…")
            # 同一文件系统内 rename 是原子的；失败时 final 不会出现
            os.rename(str(staging), str(final))
            # 提交后再把 manifest 的 committed 置位（此时 final 已可见）
            manifest["committed"] = True
            atomic_write_json(final / "manifest.json", manifest, mode=0o640)
        except BaseException:
            # 失败路径：清掉半成品，但**保留**可能的故障现场供人工核查
            if staging.exists():
                failed = self.staging / f"{snapshot_id}.failed"
                try:
                    if failed.exists():
                        shutil.rmtree(failed, ignore_errors=True)
                    os.rename(str(staging), str(failed))
                except OSError:
                    shutil.rmtree(staging, ignore_errors=True)
            self.resume_after(was_running, on_log=log)
            raise
        self.resume_after(was_running, on_log=log)

        info = SnapshotInfo(
            id=snapshot_id, kind=kind, reason=reason, created_at=manifest["created_at"],
            size_bytes=total, file_count=len(entries), valid=True,
            game_version=manifest["game_version"], smapi_version=manifest["smapi_version"],
            image_ref=manifest["image_ref"], image_digest=manifest["image_digest"],
            mods_hash=mods_hash, saves_hash=saves_hash, note=note, task_id=task_id,
        )
        log(f"快照 {snapshot_id} 创建成功")
        return info

    def _guard_space(self) -> None:
        cfg = C.load_config()
        required = cfg.policy.snapshot_space_estimate_mb
        available = free_mb(self.root if self.root.exists() else self.data_root)
        if available < required:
            raise SnapshotError(
                f"磁盘空间不足：剩余 {available} MiB，低于快照所需的 {required} MiB。"
                "请清理空间或降低保留策略后再重试。"
            )

    def _validate_payload(self, payload: Path, entries: list[FileEntry]) -> None:
        got = {e.path for e in entries}
        for rel in INCLUDE_DIRS:
            if (self.data_root / rel).is_dir() and not any(p.startswith(rel + "/") for p in got):
                raise SnapshotError(f"快照缺少必要内容：{rel}/ 为空或未复制成功")
        if not entries:
            raise SnapshotError("快照清单为空，拒绝标记为有效")
        # 抽样复校验：随机取最多 8 个文件重算哈希
        sample = entries[:: max(1, len(entries) // 8)][:8]
        for entry in sample:
            fp = payload / entry.path
            if not fp.is_file():
                raise SnapshotError(f"清单中的文件不存在：{entry.path}")
            if sha256_file(fp) != entry.sha256:
                raise SnapshotError(f"校验值不一致：{entry.path}")
            if fp.stat().st_size != entry.size:
                raise SnapshotError(f"文件大小不一致：{entry.path}")

    def _new_id(self, kind: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"{stamp}-{kind}-{uuid.uuid4().hex[:6]}"

    # ── 读取 ────────────────────────────────────────────────────────────────
    def load_manifest(self, snapshot_id: str) -> dict[str, Any]:
        path = self.manifest_path(snapshot_id)
        if not path.is_file():
            raise SnapshotError(f"快照 {snapshot_id} 不存在或缺少 manifest")
        raw = read_json(path, None)
        if not isinstance(raw, dict):
            raise SnapshotError(f"快照 {snapshot_id} 的 manifest 无法解析")
        return migrate_manifest(raw)

    def info(self, snapshot_id: str) -> SnapshotInfo:
        raw = self.load_manifest(snapshot_id)
        counts = raw.get("counts", {}) or {}
        hashes = raw.get("hashes", {}) or {}
        size = 0
        p = self.path_for(snapshot_id)
        if p.is_dir():
            size = dir_size_bytes(p)
        return SnapshotInfo(
            id=str(raw.get("id", snapshot_id)),
            kind=str(raw.get("kind", "manual")),
            reason=str(raw.get("reason", "")),
            created_at=float(raw.get("created_at", 0.0)),
            size_bytes=int(counts.get("bytes", size)),
            file_count=int(counts.get("files", 0)),
            valid=bool(raw.get("committed", False)),
            manifest_version=int(raw.get("manifest_version", MANIFEST_VERSION)),
            game_version=str(raw.get("game_version", "")),
            smapi_version=str(raw.get("smapi_version", "")),
            image_ref=str(raw.get("image_ref", "")),
            image_digest=str(raw.get("image_digest", "")),
            mods_hash=str(hashes.get("mods", "")),
            saves_hash=str(hashes.get("saves", "")),
            note=str(raw.get("note", "")),
            task_id=str(raw.get("task_id", "")),
        )

    def list(self) -> list[SnapshotInfo]:
        self.ensure()
        pinned = self._pinned_ids()
        out: list[SnapshotInfo] = []
        for child in sorted(self.root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            try:
                info = self.info(child.name)
                info.pinned = info.id in pinned
                out.append(info)
            except SnapshotError:
                out.append(SnapshotInfo(id=child.name, kind="unknown", reason="unknown",
                                        created_at=child.stat().st_mtime, valid=False,
                                        missing=True, note="manifest 缺失或损坏"))
        out.sort(key=lambda i: i.created_at, reverse=True)
        return out

    # ── 校验 ────────────────────────────────────────────────────────────────
    def verify(self, snapshot_id: str, *, deep: bool = True,
               on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """校验快照完整性。`deep=True` 时逐文件重算哈希。"""
        log = on_log or (lambda _m: None)
        raw = self.load_manifest(snapshot_id)
        root = self.path_for(snapshot_id)
        payload = root / "payload"
        problems: list[str] = []
        if not payload.is_dir():
            return {"ok": False, "problems": ["payload 目录不存在"], "checked": 0}
        if not raw.get("committed", False):
            problems.append("manifest 未标记为 committed（可能是失败的中断快照）")
        files = raw.get("files", []) or []
        checked = 0
        missing = 0
        mismatch = 0
        total_bytes = 0
        for entry_raw in files:
            entry = FileEntry.from_dict(entry_raw)
            fp = payload / entry.path
            if not fp.is_file():
                missing += 1
                if len(problems) < 20:
                    problems.append(f"缺失：{entry.path}")
                continue
            st = fp.stat()
            total_bytes += st.st_size
            if st.st_size != entry.size:
                mismatch += 1
                if len(problems) < 20:
                    problems.append(f"大小不符：{entry.path}（{st.st_size} != {entry.size}）")
                continue
            if deep:
                if sha256_file(fp) != entry.sha256:
                    mismatch += 1
                    if len(problems) < 20:
                        problems.append(f"校验值不符：{entry.path}")
            checked += 1
        log(f"校验完成：检查 {checked} 个文件，缺失 {missing}，不符 {mismatch}")
        return {
            "ok": not problems,
            "checked": checked,
            "missing": missing,
            "mismatch": mismatch,
            "total_bytes": total_bytes,
            "declared_files": len(files),
            "problems": problems[:50],
            "manifest_version": raw.get("manifest_version"),
        }

    # ── 恢复 ────────────────────────────────────────────────────────────────
    def restore(self, snapshot_id: str, *, stop_server: bool = True,
                pre_snapshot_id: str = "",
                on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """从快照恢复 Saves/Mods。

        安全协议：
          1. 先深度校验快照；**校验不通过直接拒绝**（项目要求 §9.7）
          2. 保存当前状态到 restore-tmp（以便失败回滚）
          3. 解包到 restore-tmp 后校验，再原子换入
          4. 任一步失败 → 回滚到原状态，并**保留故障现场**
        """
        log = on_log or (lambda _m: None)
        root = self.path_for(snapshot_id)
        if not root.is_dir():
            raise SnapshotError(f"快照 {snapshot_id} 不存在")

        log("恢复前深度校验快照…")
        report = self.verify(snapshot_id, deep=True, on_log=log)
        if not report["ok"]:
            raise SnapshotInconsistent(
                "快照校验未通过，拒绝恢复（已保留现场）：" + "；".join(report["problems"][:5])
            )

        payload = root / "payload"
        restore_tmp = C.RESTORE_TMP_DIR / f"restore-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        backup_dir = restore_tmp / "backup"
        incoming = restore_tmp / "incoming"
        restore_tmp.mkdir(parents=True, exist_ok=True)

        was_running = False
        try:
            if stop_server:
                was_running = self.pause_for_snapshot(on_log=log)

            log("备份当前状态（用于失败回滚）…")
            backup_dir.mkdir(parents=True, exist_ok=True)
            for rel in INCLUDE_DIRS:
                src = self.data_root / rel
                if src.is_dir():
                    shutil.copytree(src, backup_dir / rel, symlinks=False, dirs_exist_ok=True)

            log("准备恢复内容…")
            incoming.mkdir(parents=True, exist_ok=True)
            for rel in INCLUDE_DIRS:
                src = payload / rel
                if src.is_dir():
                    shutil.copytree(src, incoming / rel, symlinks=False, dirs_exist_ok=True)
            for rel in INCLUDE_FILES:
                src = payload / rel
                if src.is_file():
                    dst = incoming / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            # 恢复内容自校验
            for rel in INCLUDE_DIRS:
                man = [f for f in (self.load_manifest(snapshot_id).get("files") or [])
                       if f["path"].startswith(rel + "/")]
                for item in man[:200]:
                    fp = incoming / item["path"]
                    if item["size"] > 1024 * 64:
                        continue  # 大文件跳过逐个抽样，前面 verify 已全量校验
                    if not fp.is_file() or fp.stat().st_size != item["size"]:
                        raise SnapshotInconsistent(f"恢复暂存区内容不完整：{item['path']}")

            log("原子换入恢复内容…")
            for rel in INCLUDE_DIRS:
                src = incoming / rel
                dst = self.data_root / rel
                if not src.is_dir():
                    continue
                swap = dst.parent / f".{rel}.swap-{uuid.uuid4().hex[:8]}"
                if dst.exists():
                    os.rename(str(dst), str(swap))
                try:
                    os.rename(str(src), str(dst))
                except OSError:
                    # 失败则把原目录换回
                    if swap.exists() and not dst.exists():
                        os.rename(str(swap), str(dst))
                    raise
                shutil.rmtree(swap, ignore_errors=True)
            for rel in INCLUDE_FILES:
                src = incoming / rel
                if src.is_file():
                    dst = self.data_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            log("恢复后校验…")
            post = self._verify_restored(snapshot_id)
            if not post["ok"]:
                raise SnapshotInconsistent("恢复后校验失败：" + "；".join(post["problems"][:5]))

            result = {
                "snapshot_id": snapshot_id,
                "restored": list(INCLUDE_DIRS),
                "files": report["checked"],
                "pre_snapshot_id": pre_snapshot_id,
                "workspace": str(restore_tmp),
            }
            shutil.rmtree(restore_tmp, ignore_errors=True)
            log("恢复成功")
            self.resume_after(was_running, on_log=log)
            return result
        except BaseException:
            log("恢复失败：正在回滚到操作前状态…")
            try:
                for rel in INCLUDE_DIRS:
                    src = backup_dir / rel
                    dst = self.data_root / rel
                    if not src.is_dir():
                        continue
                    broken = restore_tmp / f"broken-{rel}"
                    if dst.exists():
                        os.rename(str(dst), str(broken))
                    try:
                        os.rename(str(src), str(dst))
                    except OSError:
                        if broken.exists() and not dst.exists():
                            os.rename(str(broken), str(dst))
                        raise
                log("已回滚到操作前状态；故障现场保留在：" + str(restore_tmp))
            except BaseException as exc:  # noqa: BLE001
                log(f"⚠️ 回滚失败，故障现场保留在 {restore_tmp}，请勿清理：{exc}")
            self.resume_after(was_running, on_log=log)
            raise

    def _verify_restored(self, snapshot_id: str) -> dict[str, Any]:
        raw = self.load_manifest(snapshot_id)
        problems: list[str] = []
        for entry_raw in (raw.get("files") or [])[:400]:
            entry = FileEntry.from_dict(entry_raw)
            fp = self.data_root / entry.path
            if not fp.is_file():
                problems.append(f"恢复后缺失：{entry.path}")
                continue
            if fp.stat().st_size != entry.size:
                problems.append(f"恢复后大小不符：{entry.path}")
        return {"ok": not problems, "problems": problems[:20]}

    # ── 删除与保留策略 ──────────────────────────────────────────────────────
    def _pinned_ids(self) -> set[str]:
        """固定状态存在 DB 的 snapshots 表里（manifest 不存用户偏好）。"""
        try:
            from .db import get_db
            return {row["id"] for row in get_db().query(
                "SELECT id FROM snapshots WHERE pinned=1")}
        except Exception:  # noqa: BLE001 - DB 不可用时按「未固定」处理，避免误报
            return set()

    def delete(self, snapshot_id: str, *, force: bool = False) -> None:
        root = self.path_for(snapshot_id)
        if not root.is_dir():
            raise SnapshotError(f"快照 {snapshot_id} 不存在")
        if not force and snapshot_id in self._pinned_ids():
            raise SnapshotError(f"快照 {snapshot_id} 已固定，请先取消固定再删除")
        # 引用检查：不得删除正在被恢复任务使用的快照
        from .db import get_db
        db = get_db()
        busy = db.one(
            "SELECT id FROM tasks WHERE snapshot_id=? AND state IN ('running','rolling_back')",
            (snapshot_id,),
        )
        if busy:
            raise SnapshotError(f"快照 {snapshot_id} 正被任务 {busy['id']} 使用，拒绝删除")
        shutil.rmtree(root)

    def enforce_retention(self, *, on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """按保留策略清理。自动/手动分别计算；固定快照永不清理。"""
        from .db import get_db
        log = on_log or (lambda _m: None)
        cfg = C.load_config()
        db = get_db()
        keep_auto = max(0, cfg.policy.keep_auto_snapshots)
        keep_manual = max(0, cfg.policy.keep_manual_snapshots)

        pinned_ids = self._pinned_ids()
        used_ids = {
            row["id"] for row in db.query("SELECT snapshot_id FROM tasks WHERE snapshot_id IS NOT NULL")
        }

        all_infos = self.list()
        by_kind: dict[str, list[SnapshotInfo]] = {"auto": [], "manual": []}
        for info in all_infos:
            by_kind.setdefault(info.kind, []).append(info)
        # 保留「最新 N 个有效快照」；固定或仍被任务引用的跳过
        deleted: list[str] = []
        kept_report: dict[str, list[str]] = {}
        for kind, keep in (("auto", keep_auto), ("manual", keep_manual)):
            items = sorted(by_kind.get(kind, []), key=lambda i: i.created_at, reverse=True)
            keep_ids = {i.id for i in items[:keep]}
            kept_report[kind] = sorted(keep_ids)
            for info in items[keep:]:
                if info.id in pinned_ids or info.id in used_ids:
                    log(f"跳过仍被引用/已固定的快照：{info.id}")
                    continue
                try:
                    shutil.rmtree(self.path_for(info.id))
                    deleted.append(info.id)
                    db.execute("DELETE FROM snapshots WHERE id=?", (info.id,))
                except OSError as exc:
                    log(f"删除失败 {info.id}：{exc}")
        log(f"保留策略执行完毕：删除 {len(deleted)} 个旧快照")
        return {"deleted": deleted, "kept": kept_report,
                "keep_auto": keep_auto, "keep_manual": keep_manual}


def validate_snapshot_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValidationError("快照 ID 不能为空")
    if not (1 <= len(snapshot_id) <= 64):
        raise ValidationError("快照 ID 长度非法")
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]+", snapshot_id):
        raise ValidationError(f"快照 ID 含非法字符：{snapshot_id}")
    if snapshot_id in (".", ".."):
        raise ValidationError("快照 ID 非法")
    return snapshot_id


def migrate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """manifest 版本迁移。旧版本在这里升级到当前 MANIFEST_VERSION。"""
    if not isinstance(raw, dict):
        raise SnapshotError("manifest 不是对象")
    version = int(raw.get("manifest_version", 0) or 0)
    if version < 1:
        # v0：早期只有文件清单，没有 hashes/counts
        raw = dict(raw)
        raw["manifest_version"] = 1
        raw.setdefault("hashes", {})
        raw.setdefault("counts", {})
        raw.setdefault("files", [])
        raw.setdefault("committed", True)
    if version > MANIFEST_VERSION:
        raise SnapshotError(
            f"快照 manifest 版本 {version} 高于本管理器支持的 {MANIFEST_VERSION}，"
            "请升级管理器后再恢复（拒绝按低版本语义解读，以免损坏数据）"
        )
    return raw
