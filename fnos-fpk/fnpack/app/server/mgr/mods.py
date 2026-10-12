"""MOD 管理：上传安装、manifest 解析、依赖检查、启用/禁用、更新、卸载、日志解析。

设计要点（项目要求 5.3）：
  * manifest 解析**大小写不敏感**（SMAPI 语义；上游 `ChangeServerPort` 发布包的
    `EntryDLl` 字段名就是错的，见审计报告 P10）。
  * 支持任意用户 MOD，不限于 ValleyServer 自带 MOD。
  * **不自动删除用户未知来源的文件**：卸载只删被识别为 MOD 的目录，且先移到现场备份。
  * 高风险操作（安装/更新/覆盖/禁用/卸载）由上层任务强制先创建快照。
  * 检查重复 ID、重复目录、缺失依赖、依赖版本不满足、目录结构错误。
  * 展示 SMAPI 加载错误并关联到具体 MOD。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from . import config as C
from .util import (
    ValidationError, atomic_write_json, read_json, inspect_zip, safe_extract_zip, safe_join,
    sha256_file, validate_name,
)

DISABLE_SUFFIX = ".disabled-by-valleyserver"
MANIFEST_NAMES = ("manifest.json", "manifest.json" + DISABLE_SUFFIX)


# ─────────────────────────────────────────────────────────────────────────────
# manifest 解析
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ModManifest:
    name: str = ""
    author: str = ""
    version: str = ""
    description: str = ""
    unique_id: str = ""
    entry_dll: str = ""
    minimum_api_version: str = ""
    update_keys: list[str] = field(default_factory=list)
    dependencies: list[dict[str, str]] = field(default_factory=list)
    content_pack_for: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "author": self.author, "version": self.version,
            "description": self.description, "unique_id": self.unique_id,
            "entry_dll": self.entry_dll,
            "minimum_api_version": self.minimum_api_version,
            "update_keys": self.update_keys, "dependencies": self.dependencies,
            "content_pack_for": self.content_pack_for,
            "problems": self.problems,
        }


def _ci_get(raw: dict[str, Any], *names: str) -> Any:
    """大小写不敏感取值（SMAPI 语义）。"""
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def parse_manifest_file(path: Path) -> ModManifest:
    manifest = ModManifest()
    if not path.is_file():
        manifest.problems.append("manifest.json 不存在")
        return manifest
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        manifest.problems.append(f"manifest.json 解析失败：{exc}")
        return manifest
    if not isinstance(raw, dict):
        manifest.problems.append("manifest.json 顶层不是对象")
        return manifest
    manifest.raw = raw

    manifest.name = str(_ci_get(raw, "Name") or "").strip()
    manifest.author = str(_ci_get(raw, "Author") or "").strip()
    manifest.version = str(_ci_get(raw, "Version") or "").strip()
    manifest.description = str(_ci_get(raw, "Description") or "").strip()
    manifest.unique_id = str(_ci_get(raw, "UniqueID", "UniqueId") or "").strip()
    manifest.entry_dll = str(_ci_get(raw, "EntryDll") or "").strip()
    manifest.minimum_api_version = str(_ci_get(raw, "MinimumApiVersion", "MinimumAPIVersion") or "").strip()
    manifest.content_pack_for = str(_ci_get(raw, "ContentPackFor") or "")[:200] \
        if not isinstance(_ci_get(raw, "ContentPackFor"), dict) else \
        str((_ci_get(raw, "ContentPackFor") or {}).get("UniqueID", ""))

    keys = _ci_get(raw, "UpdateKeys")
    if isinstance(keys, list):
        manifest.update_keys = [str(k) for k in keys if isinstance(k, (str, int))]

    deps = _ci_get(raw, "Dependencies")
    if isinstance(deps, list):
        for item in deps:
            if not isinstance(item, dict):
                continue
            uid = str(_ci_get(item, "UniqueID", "UniqueId") or "").strip()
            if not uid:
                continue
            manifest.dependencies.append({
                "unique_id": uid,
                "minimum_version": str(_ci_get(item, "MinimumVersion") or "").strip(),
                "required": "false" if str(_ci_get(item, "IsRequired")).lower() == "false" else "true",
            })

    # 结构性问题
    if not manifest.unique_id:
        manifest.problems.append("缺少 UniqueID（SMAPI 将拒绝加载）")
    if not manifest.name:
        manifest.problems.append("缺少 Name")
    if not manifest.entry_dll and not manifest.content_pack_for:
        manifest.problems.append("缺少 EntryDll（且不是内容包）")
    if manifest.version and not re.fullmatch(r"\d+(\.\d+)*", manifest.version.split("-")[0]):
        manifest.problems.append(f"Version 格式可疑：{manifest.version}")
    return manifest


def parse_version(value: str) -> tuple[int, ...]:
    """把语义化版本解析为可比较元组。无法解析的部分忽略。"""
    if not value:
        return ()
    core = str(value).strip().split("-")[0].split("+")[0]
    out: list[int] = []
    for part in core.split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def version_ge(a: str, b: str) -> bool:
    ta, tb = parse_version(a), parse_version(b)
    if not tb:
        return True
    if not ta:
        return False
    return ta >= tb


# ─────────────────────────────────────────────────────────────────────────────
# 已安装 MOD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InstalledMod:
    dir_name: str
    enabled: bool
    manifest: ModManifest
    size_bytes: int = 0
    file_count: int = 0
    tree_hash: str = ""
    modified_at: float = 0.0
    has_private_data: bool = False
    private_data_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_name": self.dir_name,
            "enabled": self.enabled,
            "manifest": self.manifest.to_dict(),
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "tree_hash": self.tree_hash,
            "modified_at": self.modified_at,
            "modified_at_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(self.modified_at)) if self.modified_at else "",
            "has_private_data": self.has_private_data,
            "private_data_files": self.private_data_files,
        }


class ModManager:
    def __init__(self, mods_dir: Path | None = None, runtime=None) -> None:
        self.mods_dir = Path(mods_dir) if mods_dir else C.MODS_DIR
        self.runtime = runtime

    # ── 运行窗口 ────────────────────────────────────────────────────────────
    def require_safe_window(self) -> dict[str, Any]:
        if self.runtime is None:
            return {"running": False, "checked": False}
        st = self.runtime.status()
        if st.running:
            raise ValidationError(
                "服务器正在运行。变更 MOD 需要先停止服务器，否则 SMAPI 可能在写入过程中"
                "读取到半成品文件。请先在「控制台」页面停止服务器。"
            )
        return {"running": False, "checked": True}

    # ── 列出 ────────────────────────────────────────────────────────────────
    def list_mods(self) -> list[InstalledMod]:
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        out: list[InstalledMod] = []
        for child in sorted(self.mods_dir.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith("."):
                continue
            out.append(self.inspect(child.name))
        out.sort(key=lambda m: (not m.enabled, (m.manifest.name or m.dir_name).lower()))
        return out

    def dir_for(self, dir_name: str) -> Path:
        validate_name(dir_name, field="MOD 目录名", max_len=120)
        return safe_join(self.mods_dir, dir_name)

    def inspect(self, dir_name: str) -> InstalledMod:
        try:
            path = self.dir_for(dir_name)
        except ValidationError as exc:
            return InstalledMod(dir_name=dir_name, enabled=False,
                                manifest=ModManifest(problems=[str(exc)]))
        enabled = True
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            disabled_path = path / ("manifest.json" + DISABLE_SUFFIX)
            if disabled_path.is_file():
                manifest_path = disabled_path
                enabled = False
        manifest = parse_manifest_file(manifest_path)

        size = 0
        count = 0
        private = 0
        for dirpath, dirnames, filenames in os.walk(path):
            rel_dir = Path(dirpath).relative_to(path)
            if rel_dir.parts and rel_dir.parts[0] == "data":
                private += len(filenames)
            for name in filenames:
                fp = Path(dirpath) / name
                if fp.is_symlink():
                    continue
                count += 1
                try:
                    size += fp.stat().st_size
                except OSError:
                    pass
        from .snapshots import hash_tree
        try:
            tree_hash, _ = hash_tree(path)
        except OSError:
            tree_hash = ""
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return InstalledMod(
            dir_name=dir_name, enabled=enabled, manifest=manifest,
            size_bytes=size, file_count=count, tree_hash=tree_hash,
            modified_at=modified, has_private_data=private > 0,
            private_data_files=private,
        )

    # ── 诊断：重复 / 依赖 ───────────────────────────────────────────────────
    def diagnose(self) -> dict[str, Any]:
        mods = self.list_mods()
        issues: list[dict[str, Any]] = []

        # 1. 重复 UniqueID
        by_id: dict[str, list[str]] = {}
        for mod in mods:
            if mod.manifest.unique_id:
                by_id.setdefault(mod.manifest.unique_id, []).append(mod.dir_name)
        for uid, dirs in by_id.items():
            if len(dirs) > 1:
                issues.append({
                    "level": "error", "kind": "duplicate_unique_id",
                    "message": f"UniqueID「{uid}」被多个 MOD 目录重复使用：{', '.join(dirs)}",
                    "mods": dirs,
                    "impact": "SMAPI 会拒绝加载其中之一，且加载顺序不确定。请只保留一份。",
                })

        # 2. 重复 MOD 名称（不同 ID 但同名，容易混淆）
        by_name: dict[str, list[str]] = {}
        for mod in mods:
            if mod.manifest.name:
                by_name.setdefault(mod.manifest.name.lower(), []).append(mod.dir_name)
        for name, dirs in by_name.items():
            if len(dirs) > 1:
                issues.append({
                    "level": "warn", "kind": "duplicate_name",
                    "message": f"多个 MOD 使用相同名称「{name}」：{', '.join(dirs)}",
                    "mods": dirs,
                    "impact": "可能是不完整升级留下的旧版本目录，建议核对后删除多余目录。",
                })

        # 3. 重复目录（大小写不同，Linux 下会共存但 Windows 下冲突）
        lower: dict[str, list[str]] = {}
        for mod in mods:
            lower.setdefault(mod.dir_name.lower(), []).append(mod.dir_name)
        for key, dirs in lower.items():
            if len(dirs) > 1:
                issues.append({
                    "level": "warn", "kind": "case_conflict",
                    "message": f"目录名仅大小写不同：{', '.join(dirs)}",
                    "mods": dirs,
                    "impact": "在大小写不敏感的文件系统上会互相覆盖。",
                })

        # 4. manifest 结构问题
        for mod in mods:
            if mod.manifest.problems:
                issues.append({
                    "level": "error", "kind": "manifest_invalid",
                    "message": f"MOD「{mod.dir_name}」的 manifest 有问题：" + "；".join(mod.manifest.problems),
                    "mods": [mod.dir_name],
                    "impact": "SMAPI 可能拒绝加载该 MOD。",
                })

        # 5. 缺失依赖 / 版本不满足
        installed = {m.manifest.unique_id: m for m in mods if m.manifest.unique_id}
        enabled_ids = {m.manifest.unique_id for m in mods if m.enabled and m.manifest.unique_id}
        for mod in mods:
            for dep in mod.manifest.dependencies:
                uid = dep["unique_id"]
                if uid.lower() in ("pathoschild.smapi", "smapi"):
                    continue  # SMAPI 自身
                required = dep.get("required", "true") != "false"
                if uid not in installed:
                    issues.append({
                        "level": "error" if required else "warn",
                        "kind": "missing_dependency",
                        "message": (f"MOD「{mod.dir_name}」依赖「{uid}」，但未安装"
                                    + ("" if required else "（可选依赖）")),
                        "mods": [mod.dir_name],
                        "impact": "该 MOD 可能加载失败；请一并安装被依赖的 MOD。",
                    })
                    continue
                if uid not in enabled_ids:
                    issues.append({
                        "level": "warn", "kind": "dependency_disabled",
                        "message": f"MOD「{mod.dir_name}」依赖的「{uid}」已被禁用",
                        "mods": [mod.dir_name, installed[uid].dir_name],
                        "impact": "被依赖的 MOD 禁用后，本 MOD 可能加载失败。",
                    })
                need = dep.get("minimum_version", "")
                have = installed[uid].manifest.version
                if need and not version_ge(have, need):
                    issues.append({
                        "level": "error", "kind": "dependency_version",
                        "message": (f"MOD「{mod.dir_name}」要求「{uid}」至少 {need}，"
                                    f"当前为 {have}"),
                        "mods": [mod.dir_name, installed[uid].dir_name],
                        "impact": "SMAPI 会拒绝加载，或运行时出现异常。",
                    })

        # 6. 自带 MOD 私有数据提醒
        for mod in mods:
            if mod.has_private_data:
                issues.append({
                    "level": "info", "kind": "private_data",
                    "message": (f"MOD「{mod.dir_name}」在目录内保存了 {mod.private_data_files} "
                                "个每存档运行数据文件（data/）"),
                    "mods": [mod.dir_name],
                    "impact": "卸载该 MOD 会一并删除这些数据；管理器会在卸载前强制快照。",
                })

        if not mods:
            issues.append({
                "level": "warn", "kind": "empty",
                "message": "MOD 目录为空。首次部署时这是正常的，但服务器不会无人值守运行。",
                "mods": [],
                "impact": "请安装 ALOS 等自动化 MOD（可从上游 Release 获取或自行上传）。",
            })

        errors = sum(1 for i in issues if i["level"] == "error")
        return {
            "mods": [m.to_dict() for m in mods],
            "issues": issues,
            "summary": {
                "total": len(mods),
                "enabled": sum(1 for m in mods if m.enabled),
                "disabled": sum(1 for m in mods if not m.enabled),
                "errors": errors,
                "warnings": sum(1 for i in issues if i["level"] == "warn"),
            },
        }

    # ── 上传预检 ────────────────────────────────────────────────────────────
    def plan_install(self, zip_path: Path) -> dict[str, Any]:
        """安装前预检：结构、manifest、重复、磁盘。只读。"""
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise ValidationError("上传的压缩包不存在")
        inspection = inspect_zip(zip_path)
        if not inspection.ok:
            raise ValidationError(f"压缩包检查未通过：{inspection.reason}")
        if inspection.file_count == 0:
            raise ValidationError("压缩包内没有任何文件")

        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
        # 找到所有 manifest.json 及其所属 MOD 根目录
        candidates: list[dict[str, Any]] = []
        for name in names:
            if PurePosixPath(name).name.lower() != "manifest.json":
                continue
            parts = PurePosixPath(name).parts
            if len(parts) < 2:
                candidates.append({"root": ".（压缩包根目录）", "root_key": "", "manifest": name})
                continue
            root_parts = parts[:-1]
            # 若包了一层游戏进程目录且这一层不是 MOD 根，尝试剥离
            candidates.append({
                "root": root_parts[0],
                "root_key": root_parts[0],
                "manifest": name,
            })
        if not candidates:
            raise ValidationError(
                "压缩包内没有找到任何 manifest.json：SMAPI MOD 必须包含 manifest.json。"
                "如果这是内容包（Content Pack），它同样必须有 manifest.json。"
            )

        results: list[dict[str, Any]] = []
        existing = {m.dir_name for m in self.list_mods()}
        existing_ids = {m.manifest.unique_id: m.dir_name
                        for m in self.list_mods() if m.manifest.unique_id}
        with zipfile.ZipFile(zip_path) as zf:
            for cand in candidates:
                manifest_rel = cand["manifest"]
                try:
                    raw = json.loads(zf.read(manifest_rel).decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
                    results.append({"root": cand["root"], "ok": False,
                                    "reason": f"manifest.json 无法解析：{exc}"})
                    continue
                tmp_manifest = ModManifest()
                if isinstance(raw, dict):
                    lowered = {str(k).lower(): v for k, v in raw.items()}
                    tmp_manifest.name = str(lowered.get("name", "")).strip()
                    tmp_manifest.unique_id = str(lowered.get("uniqueid", "")).strip()
                    tmp_manifest.version = str(lowered.get("version", "")).strip()
                    tmp_manifest.author = str(lowered.get("author", "")).strip()
                    tmp_manifest.entry_dll = str(lowered.get("entrydll", "")).strip()
                    deps = lowered.get("dependencies")
                    if isinstance(deps, list):
                        for item in deps:
                            if isinstance(item, dict):
                                li = {str(k).lower(): v for k, v in item.items()}
                                uid = str(li.get("uniqueid", "")).strip()
                                if uid:
                                    tmp_manifest.dependencies.append({
                                        "unique_id": uid,
                                        "minimum_version": str(li.get("minimumversion", "")).strip(),
                                        "required": "false" if str(li.get("isrequired")).lower() == "false" else "true",
                                    })
                root_name = cand["root_key"] or (tmp_manifest.name or "mod")
                conflicts = {
                    "dir_conflict": root_name in existing,
                    "id_conflict": bool(tmp_manifest.unique_id
                                        and tmp_manifest.unique_id in existing_ids
                                        and existing_ids[tmp_manifest.unique_id] != root_name),
                    "installed_as": existing_ids.get(tmp_manifest.unique_id, ""),
                }
                results.append({
                    "root": root_name,
                    "ok": bool(tmp_manifest.unique_id and tmp_manifest.name),
                    "reason": "" if (tmp_manifest.unique_id and tmp_manifest.name)
                              else "缺少 Name 或 UniqueID",
                    "manifest": tmp_manifest.to_dict(),
                    **conflicts,
                })

        return {
            "zip": str(zip_path),
            "zip_sha256": sha256_file(zip_path),
            "zip_size": zip_path.stat().st_size,
            "inspection": {
                "file_count": inspection.file_count,
                "total_uncompressed": inspection.total_uncompressed,
                "top_level": list(inspection.top_level_dirs)[:20],
                "suspicious": list(inspection.suspicious),
            },
            "mods": results,
            "single_mod": len(results) == 1,
        }

    # ── 安装 / 覆盖 ─────────────────────────────────────────────────────────
    def install_zip(self, zip_path: Path, *, dir_name: str = "", overwrite: bool = False,
                    on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
        """安装（或覆盖安装）ZIP 内的 MOD。上传包必须是单个 MOD。

        安装前**必须**已由上层任务创建快照；本方法自身不再创建快照，但会拒绝在
        服务器运行时安装。
        """
        log = on_log or (lambda _m: None)
        plan = self.plan_install(Path(zip_path))
        if len(plan["mods"]) != 1:
            raise ValidationError(
                f"上传包内包含 {len(plan['mods'])} 个 MOD。系统只接受一次安装一个 MOD，"
                "请分别上传（或使用「安装上游 MOD 包」功能）。"
            )
        entry = plan["mods"][0]
        if not entry["ok"]:
            raise ValidationError(f"MOD 校验未通过：{entry['reason']}")
        target_name = validate_name(dir_name or entry["root"], field="MOD 目录名", max_len=120)

        self.require_safe_window()
        mods_dir = self.mods_dir
        mods_dir.mkdir(parents=True, exist_ok=True)
        target = mods_dir / target_name
        if target.exists() and not overwrite:
            raise ValidationError(
                f"MOD 目录「{target_name}」已存在。覆盖安装会替换现有文件"
                "（含 data/ 下的每存档状态），请在界面确认后重试。"
            )

        staging = C.STAGING_DIR / f"mod-install-{int(time.time())}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        try:
            log("解压并做安全检查…")
            safe_extract_zip(Path(zip_path), staging)
            # 定位真正的 MOD 根目录
            roots = [p for p in staging.iterdir() if p.is_dir()]
            src: Path | None = None
            direct_manifest = staging / "manifest.json"
            if direct_manifest.is_file():
                src = staging
            else:
                for r in roots:
                    if (r / "manifest.json").is_file():
                        if src is not None:
                            raise ValidationError(
                                "压缩包内包含多个 MOD 根目录，请一次只上传一个 MOD"
                            )
                        src = r
            if src is None:
                raise ValidationError("解压后未找到包含 manifest.json 的 MOD 目录")

            # 保险：任何路径穿越/符号链接已在 safe_extract_zip 拦截
            if target.exists():
                log("覆盖前把现有 MOD 目录移到现场备份…")
                backup = C.RESTORE_TMP_DIR / f"pre-mod-install-{target_name}-{int(time.time())}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.rename(str(target), str(backup))
            try:
                os.rename(str(src), str(target))
            except OSError:
                shutil.copytree(src, target)

            installed = self.inspect(target_name)
            if installed.manifest.problems:
                raise ValidationError(
                    "安装后 manifest 校验失败：" + "；".join(installed.manifest.problems)
                )
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            log(f"MOD「{target_name}」安装成功（{installed.file_count} 个文件）")
            return {
                "dir_name": target_name,
                "name": installed.manifest.name,
                "unique_id": installed.manifest.unique_id,
                "version": installed.manifest.version,
                "overwritten": backup is not None,
                "files": installed.file_count,
                "bytes": installed.size_bytes,
                "tree_hash": installed.tree_hash,
                "zip_sha256": plan["zip_sha256"],
            }
        except BaseException:
            try:
                if backup is not None and backup.exists():
                    if target.exists():
                        broken = C.RESTORE_TMP_DIR / f"broken-mod-{target_name}-{int(time.time())}"
                        broken.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(str(target), str(broken))
                    os.rename(str(backup), str(target))
                    log("安装失败，已回滚到原 MOD 目录")
            except OSError as exc:
                log(f"⚠️ 回滚失败，请手工检查（备份在 {backup}）：{exc}")
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── 启用 / 禁用 ─────────────────────────────────────────────────────────
    def set_enabled(self, dir_name: str, enabled: bool) -> dict[str, Any]:
        self.require_safe_window()
        path = self.dir_for(dir_name)
        if not path.is_dir():
            raise ValidationError(f"MOD 目录不存在：{dir_name}")
        live = path / "manifest.json"
        off = path / ("manifest.json" + DISABLE_SUFFIX)
        if enabled:
            if live.is_file():
                return {"dir_name": dir_name, "enabled": True, "changed": False}
            if not off.is_file():
                raise ValidationError(f"MOD「{dir_name}」没有可用的 manifest.json")
            os.rename(str(off), str(live))
            return {"dir_name": dir_name, "enabled": True, "changed": True}
        if off.is_file():
            return {"dir_name": dir_name, "enabled": False, "changed": False}
        if not live.is_file():
            raise ValidationError(f"MOD「{dir_name}」没有可用的 manifest.json")
        os.rename(str(live), str(off))
        return {"dir_name": dir_name, "enabled": False, "changed": True}

    # ── 卸载 ────────────────────────────────────────────────────────────────
    def remove(self, dir_name: str, *, keep_private_data: bool = True) -> dict[str, Any]:
        """卸载 MOD：目录整体移入现场备份区（不直接 rmtree）。"""
        self.require_safe_window()
        path = self.dir_for(dir_name)
        if not path.is_dir():
            raise ValidationError(f"MOD 目录不存在：{dir_name}")
        installed = self.inspect(dir_name)
        trash = C.RESTORE_TMP_DIR / f"removed-mod-{dir_name}-{int(time.time())}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(path), str(trash))

        data_kept = False
        if keep_private_data and installed.has_private_data:
            src_data = trash / "data"
            if src_data.is_dir():
                keep = C.RESTORE_TMP_DIR / f"kept-mod-data-{dir_name}-{int(time.time())}"
                shutil.copytree(src_data, keep)
                data_kept = True
        return {
            "dir_name": dir_name,
            "name": installed.manifest.name,
            "moved_to": str(trash),
            "private_data_files": installed.private_data_files,
            "private_data_kept": data_kept,
            "note": ("MOD 目录已移入现场备份区（未直接删除）。"
                     + ("每存档数据已额外保留一份副本。" if data_kept else "")
                     + "确认无误后可清理现场备份。"),
        }

    # ── 上游 MOD 包安装 ─────────────────────────────────────────────────────
    def install_bundle(self, zip_path: Path, *, on_log: Callable[[str], None] | None = None,
                       overwrite: bool = False) -> dict[str, Any]:
        """安装包含多个 MOD 的整包（如上游 Release 的 `Mods.zip`）。

        逐个 MOD 走 `install_zip` 的同一套安全校验；任一失败即整体失败并回滚。
        """
        log = on_log or (lambda _m: None)
        zip_path = Path(zip_path)
        inspection = inspect_zip(zip_path)
        if not inspection.ok:
            raise ValidationError(f"压缩包检查未通过：{inspection.reason}")

        staging = C.STAGING_DIR / f"bundle-{int(time.time())}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        done: list[str] = []
        backups: dict[str, Path] = {}
        try:
            safe_extract_zip(zip_path, staging)
            mod_roots = [
                d for d in sorted(staging.iterdir())
                if d.is_dir() and (d / "manifest.json").is_file()
            ]
            if not mod_roots:
                raise ValidationError("包内没有找到任何含 manifest.json 的 MOD")
            log(f"包内共发现 {len(mod_roots)} 个 MOD")
            self.require_safe_window()
            for src in mod_roots:
                name = validate_name(src.name, field="MOD 目录名", max_len=120)
                target = self.mods_dir / name
                if target.exists():
                    if not overwrite:
                        raise ValidationError(
                            f"MOD 目录「{name}」已存在；整包安装需要覆盖确认。"
                        )
                    backup = C.RESTORE_TMP_DIR / f"pre-bundle-{name}-{int(time.time())}"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(str(target), str(backup))
                    backups[name] = backup
                os.rename(str(src), str(target))
                done.append(name)
                mod = self.inspect(name)
                results.append({"dir_name": name, "version": mod.manifest.version,
                                "name": mod.manifest.name})
                log(f"  ✓ {name} v{mod.manifest.version}")
            for backup in backups.values():
                shutil.rmtree(backup, ignore_errors=True)
            return {"installed": results, "count": len(results),
                    "zip_sha256": inspection_sha(zip_path)}
        except BaseException:
            log("整包安装失败，正在回滚已安装的 MOD…")
            for name in done:
                target = self.mods_dir / name
                if target.exists():
                    broken = C.RESTORE_TMP_DIR / f"broken-bundle-{name}-{int(time.time())}"
                    broken.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(str(target), str(broken))
                backup = backups.get(name)
                if backup and backup.exists():
                    os.rename(str(backup), str(target))
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── SMAPI 日志解析 ──────────────────────────────────────────────────────
    @staticmethod
    def parse_smapi_log(text: str) -> dict[str, Any]:
        """解析 SMAPI 控制台日志：加载成功/失败、每个 MOD 的状态与错误。

        区分**确定性错误**（SMAPI 明确报错）与**推断**（我们按规则判断），
        并把无法确认的内容单独列出。
        """
        if not text:
            return {"available": False, "message": "尚无日志"}
        lines = text.splitlines()
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        versions = {"smapi": "", "game": ""}

        m = re.search(r"SMAPI\s+(\d+\.\d+\.\d+[^\s]*)", text)
        if m:
            versions["smapi"] = m.group(1)
        m = re.search(r"Stardew Valley\s+(\d+\.\d+\.\d+[^\s]*)", text)
        if m:
            versions["game"] = m.group(1)
        m = re.search(r"Loading mods.*?(\d+)\s+mods", text)
        if m:
            versions["mod_count"] = m.group(1)

        loaded = _parse_loaded_mods(lines)

        legacy_re = re.compile(
            r"Loaded mod:\s*(?P<name>.+?)\s*$")   # 旧版 SMAPI 的单行格式
        for line in lines:
            mm = legacy_re.search(_strip_log_prefix(line))
            if mm:
                loaded.append({"name": mm.group("name").strip(),
                               "line": mm.group("name").strip()})
                continue
            if re.search(r"\bERROR\b", line) or re.search(r"\bCRITICAL\b", line):
                mod = _extract_mod_name(line)
                errors.append({
                    "level": "error",
                    "mod": mod,
                    "line": line.strip()[:600],
                    "certainty": "确定性错误（SMAPI 明确报错）",
                })
            elif re.search(r"\bWARN\b", line) and "SMAPI" in line:
                warnings.append(line.strip()[:400])

        skipped = re.findall(r"skipped\s+(.+?)\s+because", text)
        for name in skipped[:20]:
            errors.append({
                "level": "error", "mod": name.strip(),
                "line": f"MOD 被跳过：{name.strip()}",
                "certainty": "确定性错误（SMAPI 明确跳过）",
            })

        note = []
        if not versions["smapi"]:
            note.append("未能从日志中识别 SMAPI 版本号（可能尚未启动完成）")
        if not loaded and not errors:
            note.append("未能识别任何 MOD 加载记录（日志可能被截断或尚未加载完成）")
        return {
            "available": True,
            "versions": versions,
            "loaded": loaded[:100],
            "loaded_count": len(loaded),
            "errors": errors[:50],
            "warnings": warnings[:30],
            "notes": note,
            "certainty_policy": (
                "「确定性错误」= SMAPI 日志里明确写出的错误行；"
                "其他判断均为规则推断，需人工确认。"
            ),
        }


# SMAPI 4.x 控制台/日志里 MOD 列表的形式：
#   [SMAPI] Loaded 7 mods:
#   [SMAPI]    Console Commands 4.5.2 by SMAPI | Adds SMAPI console commands ...
# 旧版是每行一条：`[12:00:00 INFO SMAPI] Loaded mod: <名字>`。
# 这里两种都支持 —— 只认旧格式正是"控制台显示 已加载 MOD 0"的原因（真机踩到）。
_LOG_PREFIX_RE = re.compile(r"^\s*(?:\[\d{2}:\d{2}:\d{2}\s+[A-Z]+\s+[\w.]+\]\s*|\[SMAPI\]\s*)*")
_LOADED_HEADER_RE = re.compile(r"Loaded\s+(?P<count>\d+)\s+mods?\s*:?\s*$")
# 名字里可以带空格，所以从右边切：`<名字> <版本> by <作者>[ | <说明>]`
_MOD_ENTRY_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<version>\d+(?:\.\d+)*[^\s]*)\s+by\s+"
    r"(?P<author>[^|]+?)(?:\s*\|\s*(?P<description>.*))?$")


def _strip_log_prefix(line: str) -> str:
    """去掉 `[12:00:00 INFO SMAPI]` / `[SMAPI]` 这类前缀。"""
    return _LOG_PREFIX_RE.sub("", line or "").rstrip()


def _parse_loaded_mods(lines: list[str]) -> list[dict[str, str]]:
    """解析 SMAPI 4.x 的 `Loaded N mods:` 多行列表。

    ⚠️ 只取**最后一次**加载记录：`console.log` 跨重启累积，
    里面可能有多块 `Loaded N mods:`（例如先 2 个、升级后又 7 个）。
    若不取最后一次，会把历史运行与当前运行的 MOD 叠加成一个虚高的数字
    （实测：真实是 7 个，累加后得到 9 个）。
    """
    headers: list[tuple[int, int]] = []      # (行号, 声明的数量)
    for idx, line in enumerate(lines):
        mm = _LOADED_HEADER_RE.search(_strip_log_prefix(line))
        if mm:
            headers.append((idx, int(mm.group("count"))))
    if not headers:
        return []
    start, declared = headers[-1]

    out: list[dict[str, str]] = []
    i = start + 1
    while i < len(lines) and len(out) < max(declared, 1) * 3:
        entry = _strip_log_prefix(lines[i])
        if not entry:
            break                           # 列表结束（空行）
        mm = _MOD_ENTRY_RE.match(entry)
        if not mm:
            break                           # 不是条目（例如 "Patched game code"）
        out.append({
            "name": mm.group("name").strip(),
            "version": mm.group("version").strip(),
            "author": mm.group("author").strip(),
            "description": (mm.group("description") or "").strip(),
            "line": entry.strip(),
        })
        i += 1
    return out


def _extract_mod_name(line: str) -> str:
    for pattern in (
        r"for mod ([^\s:]+)",
        r"\[([^\]]+)\]\s",
        r"mod ([A-Za-z0-9._-]+)",
    ):
        m = re.search(pattern, line)
        if m:
            return m.group(1)[:80]
    return ""


def inspection_sha(path: Path) -> str:
    return sha256_file(path)
