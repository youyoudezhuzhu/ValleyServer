"""存档管理：列出、导入、导出、切换、删除、恢复。

安全与一致性约束（项目要求 5.2）：
  * 上传前检查压缩包格式、文件路径、文件数量和大小（`zipfs.inspect_zip`）。
  * 导入前检查目标是否已存在；覆盖必须由调用方显式传 `overwrite=True`（UI 二次确认）。
  * **玩家在线或游戏进程仍可能写入存档时，禁止直接覆盖/删除/切换**：所有写操作前
    由 `require_safe_window` 强制检查游戏进程状态，必要时要求调用方先停服。
  * 每次恢复前先保存当前状态（由快照/恢复任务负责），失败时保留故障现场。
  * 不删除未知来源的文件：只操作被识别为合法存档的目录。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import config as C
from .util import (
    ValidationError, atomic_write_json, disk_usage, free_mb, read_json,
    inspect_zip, safe_extract_zip, safe_join, sha256_file, validate_name,
)

# Stardew Valley 存档的规范标记文件
SAVE_INFO_NAMES = ("SaveGameInfo", "SaveGameInfo.xml")
SAVE_DATA_SUFFIXES = (".xml", "")
MAX_SAVE_BYTES = 2 * 1024 * 1024 * 1024  # 单存档 2 GiB


class SaveError(RuntimeError):
    pass


class ServerBusy(SaveError):
    """服务器正在运行，禁止执行会破坏存档一致性的操作。"""


@dataclass
class SaveInfo:
    dir_name: str
    valid: bool
    farm_name: str = ""
    player_name: str = ""
    game_date: str = ""
    season: str = ""
    day: str = ""
    year: str = ""
    money: str = ""
    game_version: str = ""
    days_played: str = ""
    size_bytes: int = 0
    file_count: int = 0
    modified_at: float = 0.0
    active: bool = False
    problems: list[str] = field(default_factory=list)
    unknown_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_name": self.dir_name, "valid": self.valid,
            "farm_name": self.farm_name, "player_name": self.player_name,
            "game_date": self.game_date, "season": self.season,
            "day": self.day, "year": self.year, "money": self.money,
            "game_version": self.game_version, "days_played": self.days_played,
            "size_bytes": self.size_bytes, "size_human": _human(self.size_bytes),
            "file_count": self.file_count,
            "modified_at": self.modified_at,
            "modified_at_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(self.modified_at)) if self.modified_at else "",
            "active": self.active, "problems": self.problems,
            "unknown_files": self.unknown_files[:20],
        }


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


class SaveManager:
    def __init__(self, saves_dir: Path | None = None, runtime=None, compose=None) -> None:
        self.saves_dir = Path(saves_dir) if saves_dir else C.SAVES_DIR
        self.runtime = runtime if runtime is not None else compose
        self.compose = self.runtime   # 兼容旧调用点
        # 单一事实来源：游戏进程 entrypoint 以只读方式挂载的同一个文件（见 runtime_mgr）。
        self.selected_file = C.GAME_DIR / "selected-save"

    # ── 运行窗口守卫 ────────────────────────────────────────────────────────
    def require_safe_window(self, *, allow_running: bool = False) -> dict[str, Any]:
        """所有会写存档的操作都必须先过这一关。

        游戏在运行时不保证存档一致（SMAPI/ALOS 可能正在写盘），因此默认拒绝。
        `allow_running=True` 只用于「导出」这类只读操作。
        """
        if self.runtime is None:
            return {"running": False, "checked": False}
        st = self.runtime.status()
        state = {"running": st.running, "status": st.status, "checked": True}
        if st.running and not allow_running:
            raise ServerBusy(
                "服务器正在运行，游戏进程可能仍在写入存档。为避免存档损坏，"
                "请先在「控制台」页面停止服务器，然后重试本操作。"
            )
        return state

    def disk_guard(self, *, needed_mb: int = 256) -> None:
        available = free_mb(self.saves_dir if self.saves_dir.exists() else C.DATA_ROOT)
        if available < needed_mb:
            raise SaveError(
                f"磁盘空间不足：剩余 {available} MiB，本操作至少需要 {needed_mb} MiB。"
            )

    # ── 列出与识别 ──────────────────────────────────────────────────────────
    def list_saves(self) -> list[SaveInfo]:
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        active = self.active_save()
        out: list[SaveInfo] = []
        for child in sorted(self.saves_dir.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            out.append(self.inspect(child.name, active=(child.name == active)))
        out.sort(key=lambda s: s.modified_at, reverse=True)
        return out

    def path_for(self, dir_name: str) -> Path:
        validate_name(dir_name, field="存档目录名", max_len=120)
        p = safe_join(self.saves_dir, dir_name)
        if not p.is_dir():
            raise SaveError(f"存档不存在：{dir_name}")
        return p

    def inspect(self, dir_name: str, *, active: bool = False) -> SaveInfo:
        problems: list[str] = []
        try:
            path = safe_join(self.saves_dir, validate_name(dir_name, field="存档目录名",
                                                           max_len=120))
        except ValidationError as exc:
            return SaveInfo(dir_name=dir_name, valid=False, problems=[str(exc)])
        info = SaveInfo(dir_name=dir_name, valid=False, active=active)
        if not path.is_dir():
            info.problems.append("目录不存在")
            return info

        files: list[Path] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for name in filenames:
                fp = Path(dirpath) / name
                if fp.is_symlink():
                    continue
                files.append(fp)
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
        info.file_count = len(files)
        info.size_bytes = total
        try:
            info.modified_at = path.stat().st_mtime
        except OSError:
            pass

        names = {f.name for f in files}
        info_file = next((path / n for n in SAVE_INFO_NAMES if n in names), None)
        data_files = [
            f for f in files
            if f.parent == path and f.name not in SAVE_INFO_NAMES
            and not f.name.startswith(".") and f.suffix in SAVE_DATA_SUFFIXES
        ]
        if info_file is None:
            problems.append("缺少 SaveGameInfo（不是合法的星露谷存档目录）")
        if not data_files:
            problems.append("缺少存档数据文件")
        info.valid = not problems

        # 未知来源文件只登记、绝不自动删除
        known = set(SAVE_INFO_NAMES) | {f.name for f in data_files}
        info.unknown_files = sorted(
            f.name for f in files if f.parent == path and f.name not in known
        )

        if info_file is not None:
            self._parse_info(info_file, info)

        _ = disk_usage(self.saves_dir)  # 保持磁盘信息新鲜
        info.problems = problems
        return info

    @staticmethod
    def _parse_info(info_file: Path, info: SaveInfo) -> None:
        """尽力解析存档元信息；解析失败不算致命（仍展示为可读但无元信息）。"""
        try:
            if info_file.stat().st_size > 8 * 1024 * 1024:
                info.problems.append("SaveGameInfo 过大，已跳过元信息解析")
                return
            tree = ET.parse(str(info_file))
            root = tree.getroot()
        except (ET.ParseError, OSError) as exc:
            info.problems.append(f"SaveGameInfo 解析失败：{exc}")
            return
        # 真实存档是 <SaveGame>；部分工具产生的元信息外层是 <SaveGameInfo>。
        # 统一到「元信息所在的那一层」，避免把 <name> 之类查成空。
        if root.tag not in ("SaveGame",):
            for child in root:
                if child.tag == "SaveGame":
                    root = child
                    break

        def find(*names: str) -> str:
            for tag in names:
                node = root.find(tag)
                if node is not None:
                    for child in node:
                        if child.tag in ("string", "int", "long", "double", "bool", "GameDate"):
                            return (child.text or "").strip()
                    if node.text and node.text.strip():
                        return node.text.strip()
            return ""

        info.farm_name = find("farmName")
        info.player_name = find("name")
        info.money = find("money")
        info.game_version = find("gameVersion")
        info.days_played = find("daysPlayed")
        date_node = root.find("date")
        if date_node is not None:
            for child in date_node:
                text = (child.text or "").strip()
                if child.tag == "season":
                    info.season = text
                elif child.tag == "dayOfMonth":
                    info.day = text
                elif child.tag == "year":
                    info.year = text
        if info.season and info.day and info.year:
            info.game_date = f"{info.year}年 {info.season} 第{info.day}天"

    # ── 当前存档 ────────────────────────────────────────────────────────────
    def active_save(self) -> str:
        if not self.selected_file.is_file():
            return ""
        try:
            raw = self.selected_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not raw:
            return ""
        name = ""
        if raw.startswith("{"):
            # set_active_save 写的是 JSON（含时间戳），这里解析出来
            try:
                payload = json.loads(raw)
                name = str(payload.get("save", "")).strip()
            except (json.JSONDecodeError, AttributeError):
                return ""
        else:
            name = raw
        if not name:
            return ""
        try:
            validate_name(name, field="当前存档名", max_len=120)
        except ValidationError:
            return ""
        return name if (self.saves_dir / name).is_dir() else ""

    def set_active_save(self, dir_name: str) -> None:
        """切换当前存档（写入管理器侧的选择文件，由游戏进程 entrypoint 读取）。"""
        path = self.path_for(dir_name)
        info = self.inspect(path.name)
        if not info.valid:
            raise SaveError(
                f"存档「{dir_name}」不合法，拒绝切换：" + "；".join(info.problems)
            )
        atomic_write_json(self.selected_file, {
            "save": dir_name,
            "set_at": time.time(),
            "set_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, mode=0o644)

    # ── 导出 ────────────────────────────────────────────────────────────────
    def export_zip(self, dir_name: str, dest_zip: Path, *,
                   allow_running: bool = False) -> dict[str, Any]:
        self.require_safe_window(allow_running=allow_running)
        path = self.path_for(dir_name)
        dest_zip = Path(dest_zip)
        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        total = 0
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    if fp.is_symlink():
                        continue
                    rel = PurePosixPath(dir_name) / PurePosixPath(
                        fp.relative_to(path).as_posix())
                    zf.write(fp, arcname=str(rel))
                    count += 1
                    total += fp.stat().st_size
        return {
            "dir_name": dir_name, "zip": str(dest_zip), "files": count,
            "bytes": total, "sha256": sha256_file(dest_zip),
            "size_bytes": dest_zip.stat().st_size,
        }

    # ── 导入 ────────────────────────────────────────────────────────────────
    def plan_import(self, zip_path: Path) -> dict[str, Any]:
        """导入前预检：只读，不改动任何数据。"""
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise SaveError("上传的压缩包不存在")
        inspection = inspect_zip(zip_path)
        if not inspection.ok:
            raise SaveError(f"压缩包检查未通过：{inspection.reason}")
        if inspection.file_count == 0:
            raise SaveError("压缩包内没有任何文件")
        if inspection.total_uncompressed > MAX_SAVE_BYTES:
            raise SaveError(
                f"压缩包解压后过大（{_human(inspection.total_uncompressed)}），"
                f"超过单存档上限 {_human(MAX_SAVE_BYTES)}"
            )

        roots: dict[str, list[str]] = {}
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                parts = PurePosixPath(name).parts
                if not parts:
                    continue
                root = parts[0]
                if len(parts) >= 2 and root.rstrip("/").lower() == "saves":
                    root = parts[1] if len(parts) > 1 else root
                roots.setdefault(root, []).append("/".join(parts))

        candidates: list[dict[str, Any]] = []
        for root_name, members in sorted(roots.items()):
            if root_name in ("", "."):
                continue
            names = {PurePosixPath(m).name for m in members}
            has_info = any(n in SAVE_INFO_NAMES for n in names)
            candidates.append({
                "name": root_name,
                "files": len(members),
                "has_save_game_info": has_info,
                "plausible": has_info,
            })
        plausible = [c for c in candidates if c["plausible"]]
        existing = {s.dir_name for s in self.list_saves()}
        for c in candidates:
            c["conflict"] = c["name"] in existing
        return {
            "zip": str(zip_path),
            "zip_sha256": sha256_file(zip_path),
            "zip_size": zip_path.stat().st_size,
            "inspection": {
                "file_count": inspection.file_count,
                "total_uncompressed": inspection.total_uncompressed,
                "total_human": _human(inspection.total_uncompressed),
                "top_level": list(inspection.top_level_dirs)[:20],
                "suspicious": list(inspection.suspicious),
            },
            "save_candidates": candidates[:30],
            "plausible_candidate": plausible[0]["name"] if len(plausible) == 1 else "",
            "needs_manual_choice": len(plausible) != 1,
            "existing_names": sorted(existing),
        }

    def import_zip(self, zip_path: Path, *, dir_name: str = "",
                   overwrite: bool = False,
                   on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """把 ZIP 里的一个存档导入 Saves/。

        一致性：**导入本身不改动已有存档**（新存档名不与现有冲突时）。
        覆盖同名存档时必须 `overwrite=True`（UI 已二次确认），且会先停服检查。
        """
        log = on_log or (lambda _m: None)
        zip_path = Path(zip_path)
        plan = self.plan_import(zip_path)

        target_name = dir_name or plan["plausible_candidate"]
        if not target_name:
            raise SaveError(
                "无法在压缩包中唯一确定存档目录，请在请求中显式指定 dir_name。"
                f"候选：{', '.join(c['name'] for c in plan['save_candidates']) or '（无）'}"
            )
        target_name = validate_name(target_name, field="存档目录名", max_len=120)
        candidate = next((c for c in plan["save_candidates"] if c["name"] == target_name), None)
        if candidate is None:
            raise SaveError(f"压缩包中不存在名为「{target_name}」的顶层目录")
        if not candidate["has_save_game_info"]:
            raise SaveError(
                f"「{target_name}」缺少 SaveGameInfo，不是合法的星露谷存档目录，拒绝导入"
            )

        self.require_safe_window()
        self.disk_guard(needed_mb=max(256, plan["inspection"]["total_uncompressed"] // 2 // 1024 // 1024))

        existing = (self.saves_dir / target_name)
        if existing.exists() and not overwrite:
            raise SaveError(
                f"存档「{target_name}」已存在。覆盖会永久替换现有存档，"
                "请在界面上确认覆盖后重试。"
            )

        self.saves_dir.mkdir(parents=True, exist_ok=True)
        staging = C.STAGING_DIR / f"save-import-{int(time.time())}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        try:
            log("解压上传包到暂存区并做安全检查…")
            safe_extract_zip(zip_path, staging)

            src = staging / target_name
            if not src.is_dir():
                # 兼容包了一层 Saves/ 的情况
                alt = staging / "Saves" / target_name
                if alt.is_dir():
                    src = alt
                else:
                    raise SaveError("解压后未找到期望的存档目录")

            names = {p.name for p in src.iterdir()}
            if not any(n in SAVE_INFO_NAMES for n in names):
                raise SaveError("解压后的存档目录缺少 SaveGameInfo，拒绝导入")

            if existing.exists():
                log("覆盖前把现有存档移到暂存备份…")
                backup = staging.parent / f"save-backup-{target_name}-{int(time.time())}"
                os.rename(str(existing), str(backup))
            try:
                os.rename(str(src), str(existing))
            except OSError:
                # 跨设备时退化为 copy
                shutil.copytree(src, existing)
            # 校验导入结果
            info = self.inspect(target_name)
            if not info.valid:
                raise SaveError("导入后校验失败：" + "；".join(info.problems))
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            log(f"存档「{target_name}」导入成功（{info.file_count} 个文件）")
            return {"dir_name": target_name, "overwritten": backup is not None,
                    "files": info.file_count, "bytes": info.size_bytes,
                    "zip_sha256": plan["zip_sha256"]}
        except BaseException:
            # 失败回滚：恢复被移走的原存档，保留故障现场
            try:
                if backup is not None and backup.exists():
                    if existing.exists():
                        broken = C.RESTORE_TMP_DIR / f"broken-import-{int(time.time())}"
                        broken.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(str(existing), str(broken))
                    os.rename(str(backup), str(existing))
                    log("导入失败，已回滚到原存档")
            except OSError as exc:
                log(f"⚠️ 回滚失败，请手工检查（原存档备份在 {backup}）：{exc}")
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── 删除 ────────────────────────────────────────────────────────────────
    def delete(self, dir_name: str, *, confirm: str = "") -> dict[str, Any]:
        """删除存档。必须显式传 `confirm == dir_name`（UI 二次确认）。"""
        if confirm != dir_name:
            raise SaveError("删除存档需要二次确认：confirm 字段必须与存档名完全一致")
        self.require_safe_window()
        path = self.path_for(dir_name)
        info = self.inspect(dir_name)
        # 保留现场副本，而不是直接 rmtree
        trash = C.RESTORE_TMP_DIR / f"deleted-save-{dir_name}-{int(time.time())}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(path), str(trash))
        if self.active_save() == dir_name:
            try:
                self.selected_file.unlink()
            except OSError:
                pass
        return {
            "dir_name": dir_name,
            "moved_to": str(trash),
            "files": info.file_count,
            "bytes": info.size_bytes,
            "note": "删除的存档已移入 restore-tmp 作为故障现场备份，可在确认无误后清理。",
        }

    # ── 从快照恢复单个存档 ──────────────────────────────────────────────────
    def restore_from_snapshot(self, snapshot_id: str, dir_name: str, *,
                              store, on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        log = on_log or (lambda _m: None)
        dir_name = validate_name(dir_name, field="存档目录名", max_len=120)
        raw = store.load_manifest(snapshot_id)
        rel = f"Saves/{dir_name}"
        members = [f for f in (raw.get("files") or []) if f["path"].startswith(rel + "/")]
        if not members:
            raise SaveError(f"快照 {snapshot_id} 中没有存档「{dir_name}」")
        payload = store.path_for(snapshot_id) / "payload" / rel
        if not payload.is_dir():
            raise SaveError(f"快照内容缺失：{rel}")
        self.require_safe_window()
        target = self.saves_dir / dir_name
        backup: Path | None = None
        staging = C.STAGING_DIR / f"save-restore-{int(time.time())}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(payload, staging)
        try:
            if target.exists():
                backup = C.RESTORE_TMP_DIR / f"pre-restore-{dir_name}-{int(time.time())}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.rename(str(target), str(backup))
            os.rename(str(staging), str(target))
            info = self.inspect(dir_name)
            if not info.valid:
                raise SaveError("恢复后校验失败：" + "；".join(info.problems))
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            log(f"存档「{dir_name}」已从快照恢复")
            return {"dir_name": dir_name, "snapshot_id": snapshot_id,
                    "files": info.file_count}
        except BaseException:
            try:
                if backup is not None and backup.exists():
                    if target.exists():
                        os.rename(str(target), str(staging) + ".failed")
                    os.rename(str(backup), str(target))
                    log("恢复失败，已回滚到操作前状态")
            except OSError as exc:
                log(f"⚠️ 回滚失败，请手工检查：{exc}")
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
