"""SMAPI 供给：由管理器自动获取并整合，用户只需要提供游戏本体。

设计依据（全部实测确认，见 docs/ 与测试）：

1. **SMAPI 官方分发在 GitHub Releases**：资产名形如
   `SMAPI-<版本>-installer.zip`（约 42 MB）。
   `smapi.io` 的 mod API 对 SMAPI 自身返回空数组（它不是 Nexus 上的 mod），
   所以不能用它查询。
2. **installer.zip 里的可用负载是 `internal/linux/install.dat`**：
   那本身又是一个 ZIP，内含真正要放进游戏目录的文件
   （`StardewModdingAPI`、`StardewModdingAPI.dll`、`.runtimeconfig.json`、
   `smapi-internal/`、bundled `Mods/`、`unix-launcher.sh`、`steam_appid.txt`）。
   注意：`internal/linux/` 下的其他文件是**安装器程序本身**，不是 SMAPI。
3. **兼容性可从发布说明解析**：正文含
   `Requires Stardew Valley 1.6.14 or later` → 正则取出最低游戏版本。
4. **游戏版本可从 `Stardew Valley.deps.json` 读**：
   `libraries` 里有形如 `"Stardew Valley/1.6.15.24356"` 的键，最稳；
   取不到再用 `strings` 读 `Stardew Valley.dll` 里的 `1.6.x`。

安全约束：
  * 只允许 HTTPS，且**只允许 GitHub 系域名**（防 SSRF / 被重定向到任意主机）；
  * 限制下载体积与超时；
  * 解包走 util.safe_extract_zip（路径穿越 / 符号链接 / ZIP 炸弹防护）；
  * **绝不覆盖用户 MOD 目录中已存在的文件**（bundled mod 只在缺失时补齐）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as C
from .util import (
    ValidationError, atomic_write_json, human_bytes, read_json, run, safe_extract_zip, sha256_file,
)

SMAPI_REPO = "Pathoschild/SMAPI"
RELEASES_API = f"https://api.github.com/repos/{SMAPI_REPO}/releases?per_page=20"
RELEASE_API = f"https://api.github.com/repos/{SMAPI_REPO}/releases/tags/{{tag}}"

# 只允许从这些域名下载（GitHub 的资产会 302 到 objects.githubusercontent.com）
ALLOWED_HOSTS = (
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "codeload.github.com", "release-assets.githubusercontent.com",
)

MAX_INSTALLER_BYTES = 220 * 1024 * 1024        # 安装器 zip 上限（实际约 42 MB）

# 发布说明里的兼容性声明，实测样例：
#   "Requires Stardew Valley 1.6.14 or later on Linux/macOS/Windows or Steam Deck."
_REQ_RE = re.compile(r"Requires Stardew Valley\s+(\d+\.\d+(?:\.\d+)?)\s+or later", re.I)
_ASSET_RE = re.compile(r"^SMAPI-(?P<ver>[\d.]+)-installer\.zip$", re.I)
_GAME_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+(?:\.\d+)?)$")

# 进游戏目录时属于"SMAPI 自己的文件"，可以安全覆盖
RUNTIME_PREFIXES = ("StardewModdingAPI", "smapi-internal")
RUNTIME_FILES = ("unix-launcher.sh",)
# bundled MOD：只补缺失，绝不覆盖用户已有内容
BUNDLED_MODS_DIR = "Mods"

CACHE_TTL_SECONDS = 6 * 3600


@dataclass
class Release:
    version: str
    tag: str
    prerelease: bool
    published_at: str
    min_game_version: str = ""
    download_url: str = ""
    size: int = 0
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "tag": self.tag, "prerelease": self.prerelease,
            "published_at": self.published_at, "min_game_version": self.min_game_version,
            "download_url": self.download_url, "size": self.size,
            "compatible_note": (f"需要游戏 {self.min_game_version} 或更高"
                                if self.min_game_version else "（发布说明未声明最低游戏版本）"),
        }


def parse_min_game_version(body: str) -> str:
    """从发布说明里解析最低游戏版本；解析不到返回空串（不猜测）。"""
    m = _REQ_RE.search(body or "")
    return m.group(1) if m else ""


def version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(text or "").split("."):
        digits = re.sub(r"\D", "", chunk)
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def satisfies(game_version: str, min_version: str) -> bool:
    """game_version 是否 >= min_version（min 为空时不判断，返回 True）。"""
    if not min_version:
        return True
    return version_tuple(game_version) >= version_tuple(min_version)


# ─────────────────────────────────────────────────────────────────────────────
# 游戏版本探测
# ─────────────────────────────────────────────────────────────────────────────

def read_game_version(game_root: Path) -> str:
    """读游戏版本。

    首选 `Stardew Valley.deps.json` 的 libraries 键（形如 `Stardew Valley/1.6.15.24356`），
    它不依赖外部命令；取不到再用 `strings` 读 DLL 兜底。
    """
    root = Path(game_root)
    deps = root / "Stardew Valley.deps.json"
    if deps.is_file():
        try:
            data = json.loads(deps.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for key in (data.get("libraries") or {}):
                name, _, ver = str(key).partition("/")
                if name.strip().lower() == "stardew valley" and ver:
                    m = _GAME_VERSION_RE.match(ver.strip())
                    if m:
                        full = m.group(1)
                        # 1.6.15.24356 -> 1.6.15（去掉构建号）
                        return ".".join(full.split(".")[:3])
    # 兜底：从主程序集里找 1.x.y
    dll = root / "Stardew Valley.dll"
    if dll.is_file():
        res = run(["strings", "-a", str(dll)], timeout=90)
        if res.ok:
            found = re.findall(r"\b(\d+\.\d+\.\d+)\b", res.stdout or "")
            versions = sorted({v for v in found if v.startswith(("1.", "2."))},
                              key=version_tuple)
            if versions:
                return versions[-1]
    return ""


def read_smapi_version(game_root: Path) -> str:
    """读游戏目录里已整合的 SMAPI 版本（读不到返回空串）。"""
    root = Path(game_root)
    dll = root / "StardewModdingAPI.dll"
    if not dll.is_file():
        return ""
    res = run(["strings", "-a", str(dll)], timeout=90)
    if res.ok:
        found = re.findall(r"\b(\d+\.\d+\.\d+)\b", res.stdout or "")
        cands = sorted({v for v in found if v.startswith("4.")}, key=version_tuple)
        if cands:
            return cands[-1]
    return ""


def is_installed(game_root: Path) -> bool:
    root = Path(game_root)
    return (root / "StardewModdingAPI").is_file() and (root / "smapi-internal").is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# 发布列表（带磁盘缓存，避免频繁打 GitHub 限流）
# ─────────────────────────────────────────────────────────────────────────────

def _cache_path() -> Path:
    return C.CONFIG_DIR / "smapi-releases.json"


def list_releases(*, force: bool = False, timeout: int = 40) -> list[Release]:
    cache = _cache_path()
    if not force:
        cached = read_json(cache, None)
        if isinstance(cached, dict):
            age = time.time() - float(cached.get("fetched_at") or 0)
            if age < CACHE_TTL_SECONDS and isinstance(cached.get("releases"), list):
                return [_release_from_dict(d) for d in cached["releases"]]

    res = run([
        "curl", "-sS", "--max-time", str(timeout), "--proto", "=https",
        "-H", "Accept: application/vnd.github+json",
        "-H", "User-Agent", "valleyserver-manager",
        RELEASES_API,
    ], timeout=timeout + 20)
    if not res.ok:
        cached = read_json(cache, None)
        if isinstance(cached, dict) and isinstance(cached.get("releases"), list):
            return [_release_from_dict(d) for d in cached["releases"]]
        raise ValidationError(
            "获取 SMAPI 发布列表失败（检查 NAS 是否能访问 api.github.com）："
            + (res.tail or "")[:200])
    try:
        raw = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"SMAPI 发布列表不是合法 JSON：{exc}") from exc
    if not isinstance(raw, list):
        raise ValidationError("SMAPI 发布列表格式异常")

    releases: list[Release] = []
    for item in raw:
        tag = str(item.get("tag_name") or "").strip()
        if not tag:
            continue
        body = str(item.get("body") or "")
        asset = _pick_asset(item)
        if asset is None:
            continue
        releases.append(Release(
            version=tag, tag=tag, prerelease=bool(item.get("prerelease")),
            published_at=str(item.get("published_at") or ""),
            min_game_version=parse_min_game_version(body),
            download_url=str(asset.get("browser_download_url") or ""),
            size=int(asset.get("size") or 0), body=body,
        ))
    if not releases:
        raise ValidationError("SMAPI 发布列表里没有找到 installer.zip 资产")
    try:
        atomic_write_json(cache, {
            "fetched_at": time.time(),
            "releases": [r.to_dict() | {"body": r.body} for r in releases],
        }, mode=0o640)
    except OSError:
        pass
    return releases


def _pick_asset(item: dict) -> dict | None:
    assets = item.get("assets") or []
    preferred = None
    for asset in assets:
        name = str(asset.get("name") or "")
        if name.lower().endswith("double-zipped.zip"):
            continue
        if _ASSET_RE.match(name):
            return asset
        if name.lower().startswith("smapi-") and "installer" in name.lower():
            preferred = preferred or asset
    return preferred


def _release_from_dict(d: dict) -> Release:
    return Release(
        version=str(d.get("version") or ""), tag=str(d.get("tag") or ""),
        prerelease=bool(d.get("prerelease")),
        published_at=str(d.get("published_at") or ""),
        min_game_version=str(d.get("min_game_version") or ""),
        download_url=str(d.get("download_url") or ""),
        size=int(d.get("size") or 0), body=str(d.get("body") or ""),
    )


def pick_release(releases: list[Release], game_version: str,
                 *, want: str = "") -> Release:
    """选出要安装的 SMAPI 版本。

    规则：优先满足游戏版本要求的最新稳定版；`want` 指定时按指定版本。
    一个都不可用时**明确报错**，而不是装一个不兼容的进去。
    """
    if not releases:
        raise ValidationError("SMAPI 发布列表为空")
    if want:
        for r in releases:
            if r.version == want or r.tag == want:
                if game_version and not satisfies(game_version, r.min_game_version):
                    raise ValidationError(
                        f"SMAPI {r.version} 需要游戏 {r.min_game_version} 或更高，"
                        f"当前游戏是 {game_version}")
                return r
        raise ValidationError(f"找不到 SMAPI 版本 {want}")

    stable = [r for r in releases if not r.prerelease]
    candidates = stable or releases
    usable = [r for r in candidates
              if not game_version or satisfies(game_version, r.min_game_version)]
    if not usable:
        need = min((r.min_game_version for r in candidates if r.min_game_version),
                   key=version_tuple, default="?")
        raise ValidationError(
            f"没有与游戏 {game_version} 兼容的 SMAPI 版本（最新版要求至少 {need}）。"
            "请上传更新的游戏本体，或手动指定 SMAPI 版本。")
    usable.sort(key=lambda r: version_tuple(r.version), reverse=True)
    return usable[0]


# ─────────────────────────────────────────────────────────────────────────────
# 下载与整合
# ─────────────────────────────────────────────────────────────────────────────

def download(release: Release, dest: Path, *, on_log: Callable[[str], None] | None = None,
             timeout: int = 900) -> Path:
    """下载 installer.zip。校验协议、域名、体积与 ZIP 有效性。"""
    log = on_log or (lambda _m: None)
    if not release.download_url:
        raise ValidationError("该 SMAPI 版本没有可下载的 installer.zip")
    if not release.download_url.lower().startswith("https://"):
        raise ValidationError("SMAPI 下载地址必须是 HTTPS")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    size_note = f"（约 {human_bytes(release.size)}）" if release.size else ""
    log(f"下载 SMAPI {release.version} 安装包{size_note}…")
    res = run([
        "curl", "-sSL", "--fail", "--max-time", str(timeout),
        "--proto", "=https", "--max-redirs", "5",
        "--max-filesize", str(MAX_INSTALLER_BYTES),
        # 只打印最终 URL，用于校验重定向落点
        "-w", "%{url_effective}",
        "-o", str(dest), release.download_url,
    ], timeout=timeout + 60)
    if not res.ok:
        raise ValidationError(
            "SMAPI 下载失败：" + (res.tail or f"退出码 {res.returncode}")[:300])

    effective = (res.stdout or "").strip()
    host = ""
    if "://" in effective:
        host = effective.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    if host and host not in ALLOWED_HOSTS:
        dest.unlink(missing_ok=True)
        raise ValidationError(f"SMAPI 下载被重定向到非允许的主机：{host}")

    if not dest.is_file() or dest.stat().st_size == 0:
        raise ValidationError("SMAPI 下载结果为空")
    if dest.stat().st_size > MAX_INSTALLER_BYTES:
        dest.unlink(missing_ok=True)
        raise ValidationError("SMAPI 安装包超过体积上限，已丢弃")
    return dest


def payload_members(installer_zip: Path) -> dict[str, Any]:
    """从 installer.zip 里取出 install.dat 的成员表（不解包到磁盘）。"""
    import zipfile
    with zipfile.ZipFile(installer_zip) as zf:
        names = zf.namelist()
        dat = None
        for n in names:
            if n.endswith("internal/linux/install.dat"):
                dat = n
                break
        if dat is None:
            for n in names:
                if n.endswith("install.dat") and "/linux/" in n:
                    dat = n
                    break
        if dat is None:
            raise ValidationError(
                "SMAPI 安装包里没有找到 internal/linux/install.dat —— "
                "文件可能不完整，或该版本改动了打包结构")
    return {"install_dat": dat}


def extract_install_dat(installer_zip: Path, dest: Path) -> Path:
    """把 install.dat（本身是 ZIP）落盘。"""
    info = payload_members(installer_zip)
    import zipfile
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(installer_zip) as zf:
        with zf.open(info["install_dat"]) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, length=1 << 20)
    if not dest.is_file() or dest.stat().st_size < 1024:
        raise ValidationError("install.dat 解出为空")
    return dest


def _atomic_copy(src: Path, dst: Path) -> None:
    """先写同目录临时文件，再 os.replace 到目标。

    为什么不能直接 `shutil.copy2(src, dst)`：如果目标是一个**正在运行的可执行文件**，
    Linux 会返回 `ETXTBSY`（Text file busy）—— 实测在升级正在运行的 SMAPI 时踩到。
    `os.replace` 是原子重命名，运行中的进程仍然持有旧 inode，不受影响。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.new-{os.getpid()}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)      # 原子替换，绕开 ETXTBSY
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# 《星露谷物语》在 Steam 上的 App ID
STARDEW_APP_ID = "413150"


def ensure_app_id(game_root: Path, *, on_log=None) -> str:
    """确保 `steam_appid.txt` 是游戏真正的 App ID。

    真机踩到的坑：**Steam depot 里的 `steam_appid.txt` 内容是 `480`（Spacewar 测试应用）** ——
    那是开发者构建时留下的值；正常用 Steam 客户端安装时，客户端会把它改写成 413150，
    但 DepotDownloader 只是**原样解包 depot**，于是这个错误的值被带进游戏目录。
    Steamworks 会以 480 初始化 → Steam 联机/成就失效。
    （已实测确认：SMAPI 自己的负载里这个文件是正确的 413150，所以是 depot 带来的问题。）

    返回说明文字："" 表示本来就正确，否则说明做了什么。
    """
    log = on_log or (lambda _m: None)
    target = Path(game_root) / "steam_appid.txt"
    want = STARDEW_APP_ID
    try:
        current = target.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        current = ""
    if current == want:
        return ""
    try:
        target.write_text(want + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"⚠️ 无法修正 steam_appid.txt：{exc}")
        return ""
    if current:
        log(f"修正 steam_appid.txt：{current} → {want}"
            "（Steam depot 里带的是开发者测试用的 480，会让 Steam 联机/成就失效）")
        return f"fixed:{current}->{want}"
    log(f"补写 steam_appid.txt：{want}")
    return "created"


def _ensure_deps_json(game_root: Path) -> str:
    """确保 `StardewModdingAPI.deps.json` 存在（**自包含**应用缺它就无法启动）。

    真机踩到的坑（用 Steam 正版副本时必现）：
      SMAPI 的 Linux 负载里**不含** `StardewModdingAPI.deps.json`，
      官方的 `install on Linux.sh` 是拿**游戏自己的 deps.json 复制一份**过来的。
      缺了它，.NET apphost 会把应用判定为"依赖共享框架"（split/FX 模式），
      而系统里并没有装 .NET 运行时（游戏是自包含发布），于是启动器
      只打印一段 host 用法帮助就退出 —— 用户完全看不出问题在哪。

    为什么之前没暴露：上游发行包那份游戏**本来就装过 SMAPI**，这个文件已存在；
      一旦换成干净的游戏目录（例如用 Steam 扫码下载的正版副本），就会踩到。

    返回写入的相对文件名；无法处理时返回空串（不阻断整合）。
    """
    target = game_root / "StardewModdingAPI.deps.json"
    if target.is_file():
        return ""
    src = game_root / "Stardew Valley.deps.json"
    if not src.is_file():
        for cand in sorted(game_root.glob("*.deps.json")):
            if cand.name == "StardewModdingAPI.deps.json":
                continue
            if "Stardew Valley" in cand.name:
                src = cand
                break
    if not src.is_file():
        return ""
    _atomic_copy(src, target)
    return target.name


def integrate(payload_zip: Path, game_root: Path, *,
              on_log: Callable[[str], None] | None = None,
              keep_bundled_mods: bool = True) -> dict[str, Any]:
    """把 payload 整合进游戏目录。

    规则（重要）：
      * SMAPI 自身的文件（`StardewModdingAPI*`、`smapi-internal/`、`unix-launcher.sh`）
        可以覆盖 —— 那是升级 SMAPI 的正常语义；
      * `Mods/` 下**已存在的文件一律不动**，只补齐缺失的 bundled mod
        （用户自己的 MOD 目录不能被安装流程改写）；
      * `steam_appid.txt` **确保内容为 413150**（Steam depot 里是开发者测试用的 480，
        原样保留会让 Steam 联机/成就失效）。
    """
    log = on_log or (lambda _m: None)
    game_root = Path(game_root)
    if not game_root.is_dir():
        raise ValidationError(f"游戏目录不存在：{game_root}")

    staging = C.STAGING_DIR / f"smapi-payload-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skipped: list[str] = []
    mods_added: list[str] = []
    try:
        # safe_extract_zip 返回解包的文件数（不是 inspect 对象）
        file_count = safe_extract_zip(payload_zip, staging)
        log(f"SMAPI 负载解包完成（{file_count} 个文件）")

        for src in sorted(staging.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(staging)
            rel_posix = rel.as_posix()
            top = rel.parts[0]

            if top == BUNDLED_MODS_DIR:
                if not keep_bundled_mods:
                    skipped.append(rel_posix)
                    continue
                # bundled mod：只补缺失；已存在（含用户自己的同名 MOD）一律不覆盖
                target = game_root / rel
                if target.exists():
                    skipped.append(rel_posix)
                    continue
                _atomic_copy(src, target)
                written.append(rel_posix)
                if len(rel.parts) == 3:
                    mod_name = rel.parts[1]
                    if mod_name not in mods_added:
                        mods_added.append(mod_name)
                continue

            if top == "steam_appid.txt":
                # 不能只看"文件在不在"，也不能照抄 depot 里的值：
                # 详见 ensure_app_id() 的说明（depot 里是 480）。
                ensure_app_id(game_root, on_log=log)
                skipped.append(rel_posix)
                continue

            is_runtime = (
                top.startswith(RUNTIME_PREFIXES)
                or top in RUNTIME_FILES
            )
            if not is_runtime:
                # 未知的顶层条目：不擅自放进游戏目录，记录下来供排查
                skipped.append(rel_posix)
                continue

            _atomic_copy(src, game_root / rel)
            written.append(rel_posix)

        launcher = game_root / "StardewModdingAPI"
        if not launcher.is_file():
            raise ValidationError("SMAPI 负载里没有 StardewModdingAPI 启动器")
        os.chmod(launcher, 0o755)
        if not (game_root / "smapi-internal").is_dir():
            raise ValidationError("SMAPI 负载里没有 smapi-internal/")
        if not (game_root / "StardewModdingAPI.dll").is_file():
            raise ValidationError("SMAPI 负载里没有 StardewModdingAPI.dll")

        ensure_app_id(game_root, on_log=log)
        deps = _ensure_deps_json(game_root)
        if deps:
            written.append(deps)

        return {"written": len(written), "skipped": len(skipped),
                "bundled_mods": mods_added, "files": written[:200],
                "deps_json": deps}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install(game_root: Path, *, version: str = "", force: bool = False,
            on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """完整流程：探测游戏版本 → 选 SMAPI → 下载 → 整合。"""
    log = on_log or (lambda _m: None)
    game_root = Path(game_root)
    if not game_root.is_dir():
        raise ValidationError(f"游戏目录不存在：{game_root}")

    game_version = read_game_version(game_root)
    if game_version:
        log(f"检测到游戏版本：{game_version}")
    else:
        log("⚠️ 未能识别游戏版本，将按最新稳定版 SMAPI 安装（启动时由 SMAPI 自行校验）")

    existing = read_smapi_version(game_root) or ""
    if is_installed(game_root) and existing and not force and not version:
        log(f"游戏目录里已有 SMAPI {existing}，跳过（如需更新请选「重新获取」）")
        return {"skipped": True, "installed": existing, "game_version": game_version}

    releases = list_releases(force=force)
    chosen = pick_release(releases, game_version, want=version)
    log(f"选用 SMAPI {chosen.version}"
        + (f"（要求游戏 ≥ {chosen.min_game_version}）" if chosen.min_game_version else ""))

    work = C.STAGING_DIR / "smapi-download"
    installer = work / f"SMAPI-{chosen.version}-installer.zip"
    download(chosen, installer, on_log=log)

    digest = sha256_file(installer)
    log(f"安装包校验值 sha256={digest[:16]}…（{human_bytes(installer.stat().st_size)}）")

    dat = extract_install_dat(installer, work / "install.dat")
    result = integrate(dat, game_root, on_log=log)
    result.update({
        "skipped": False,
        "smapi_version": chosen.version,
        "min_game_version": chosen.min_game_version,
        "game_version": game_version,
        "installer_sha256": digest,
        "installer_bytes": installer.stat().st_size,
        "download_url": chosen.download_url,
        "installed_now": read_smapi_version(game_root) or chosen.version,
    })
    log(f"SMAPI {result['installed_now']} 已整合进 {game_root}"
        + (f"（新增 bundled MOD：{', '.join(result['bundled_mods'])}）"
           if result.get("bundled_mods") else ""))
    shutil.rmtree(work, ignore_errors=True)
    return result


def status(game_root: Path) -> dict[str, Any]:
    """给界面用的状态：是否已整合、版本、游戏版本。"""
    root = Path(game_root)
    installed = is_installed(root)
    return {
        "installed": installed,
        "version": read_smapi_version(root) if installed else "",
        "game_version": read_game_version(root),
        "game_root": str(root),
        "source": "由管理器自动获取并整合" if installed else "",
    }
