"""原生模式的虚拟显示栈供给（Xvfb / openbox / x11vnc / websockify / noVNC）。

为什么要「应用内捆绑」而不是 `apt-get install`：
  * 飞牛 OS 是 Debian 12，apt 可用，但**安装系统包会改动 NAS 的系统状态**，
    违反「不修改无关系统配置」的约束；
  * 捆绑在应用目录内可以随应用升级/卸载一起管理，不影响其它应用；
  * 依赖不满足时不会把系统搞成半装状态。

做法：`apt-get download`（只下载，不安装）→ `dpkg -x` 解到
`$TRIM_PKGHOME/xstack/root`，运行时用 PATH + LD_LIBRARY_PATH + PYTHONPATH 解析。
**全程不写系统包数据库。**

实测（本机飞牛 OS，Debian 12）：
  * Xvfb / openbox / x11vnc 仅靠本地前缀即可运行（见 docs/native-deployment.md §2）；
  * websockify 需要 PYTHONPATH 指向捆绑的 dist-packages；
  * 游戏本体与 SMAPI 为 .NET 6 **自包含**发布，不需要安装 dotnet。
"""

from __future__ import annotations

import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

from . import config as C
from .util import ValidationError, atomic_write_json, read_json, run

# 需要捆绑的包（含所有依赖，由 apt-get 解析）
XSTACK_PACKAGES = (
    "xvfb", "x11vnc", "websockify", "openbox", "novnc",
    "x11-utils", "x11-xserver-utils", "xauth", "xkb-data",
)
# 运行时必须能找到的可执行文件
REQUIRED_BINARIES = ("Xvfb", "x11vnc", "openbox", "websockify")
OPTIONAL_BINARIES = ("xdpyinfo", "xrandr", "xset", "xkbcomp", "xauth", "scrot")


def xstack_root() -> Path:
    """捆绑显示栈的安装根（即 dpkg -x 的目标，里面有 usr/bin、usr/lib 等）。"""
    return C.PKGHOME / "xstack" / "root"


def xstack_meta_path() -> Path:
    return C.PKGHOME / "xstack" / "meta.json"


def is_installed() -> bool:
    root = xstack_root()
    return all((root / "usr" / "bin" / b).is_file() for b in REQUIRED_BINARIES)


def installed_version() -> str:
    meta = read_json(xstack_meta_path(), {}) or {}
    return str(meta.get("installed_at", "")) if isinstance(meta, dict) else ""


# 系统 PATH 上可能出现这些名字，但我们要优先用捆绑版本（版本可控）
_KEEP_SYSTEM_SAFE = {"Xvfb", "x11vnc", "openbox", "websockify"}


def find_tool(name: str) -> str:
    """解析可执行文件：优先捆绑版本，其次系统版本。返回空串表示找不到。"""
    if os.sep in name:
        return name if Path(name).is_file() else ""
    bundled = xstack_root() / "usr" / "bin" / name
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    system = shutil.which(name)
    return system or ""


def tool_status() -> dict[str, Any]:
    out: dict[str, Any] = {"bundled_root": str(xstack_root()), "installed": is_installed()}
    for name in REQUIRED_BINARIES + OPTIONAL_BINARIES:
        path = find_tool(name)
        out[name] = {
            "found": bool(path), "path": path,
            "source": ("bundled" if path.startswith(str(xstack_root())) else "system")
                      if path else "",
        }
    out["missing_required"] = [b for b in REQUIRED_BINARIES if not find_tool(b)]
    out["ok"] = not out["missing_required"]
    return out


def env_for_tools() -> dict[str, str]:
    """构造运行显示栈所需的环境（PATH / LD_LIBRARY_PATH / PYTHONPATH）。

    websockify 是 Python 程序，Debian 包会把模块放在
    /usr/lib/python3/dist-packages；捆绑后必须显式加进 PYTHONPATH，
    否则会报 `PackageNotFoundError: No package metadata was found for websockify`（实测）。
    """
    root = xstack_root()
    env: dict[str, str] = {}
    if not root.is_dir():
        return env
    bindirs = [str(root / "usr" / "bin"), str(root / "bin")]
    env["PATH"] = ":".join([d for d in bindirs if Path(d).is_dir()] + [os.environ.get("PATH", "")])
    libdirs = [
        str(root / "usr" / "lib" / "x86_64-linux-gnu"),
        str(root / "usr" / "lib"),
        str(root / "lib" / "x86_64-linux-gnu"),
        str(root / "lib"),
    ]
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(
        [d for d in libdirs if Path(d).is_dir()] + ([existing] if existing else []))
    for py in ("usr/lib/python3/dist-packages", "usr/lib/python3.11/dist-packages",
               "usr/lib/python3/site-packages"):
        d = root / py
        if d.is_dir():
            old = os.environ.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{d}{os.pathsep}{old}" if old else str(d)
            break
    env["XKB_CONFIG_ROOT"] = str(root / "usr" / "share" / "X11" / "xkb")
    # ⚠️ 必须设置 XDG_DATA_DIRS：窗口管理器（openbox）要在其中查找主题与菜单。
    # 实测踩到：漏设时 openbox 报 "Unable to load the theme 'Clearlooks'" 并**直接退出**，
    # 表现为"游戏在跑但没有窗口管理器"，很难排查。
    shares = [str(root / "usr" / "share"), str(root / "usr" / "local" / "share")]
    existing_xdg = os.environ.get("XDG_DATA_DIRS", "")
    shares = [d for d in shares if Path(d).is_dir()]
    if existing_xdg:
        shares.append(existing_xdg)
    elif Path("/usr/share").is_dir():
        shares.append("/usr/share")
    if shares:
        env["XDG_DATA_DIRS"] = ":".join(shares)
    return env


def novnc_webroot() -> str:
    root = xstack_root()
    for cand in ("usr/share/novnc", "usr/share/novnc/"):
        p = root / cand
        if (p / "vnc.html").is_file() or (p / "vnc_lite.html").is_file():
            return str(p)
    system = Path("/usr/share/novnc")
    if (system / "vnc.html").is_file():
        return str(system)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 供给（下载 + 解包，不写系统包数据库）
# ─────────────────────────────────────────────────────────────────────────────

def provision(*, force: bool = False, on_log: Callable[[str], None] | None = None,
              timeout: int = 1800) -> dict[str, Any]:
    """下载并解包显示栈到应用目录。幂等；失败不会留下半成品标记。"""
    log = on_log or (lambda _m: None)
    if is_installed() and not force:
        log("显示栈已存在，跳过下载")
        return {"installed": True, "skipped": True, **tool_status()}

    base = C.PKGHOME / "xstack"
    debs = base / "debs"
    tmp_root = base / "root.part"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    debs.mkdir(parents=True, exist_ok=True)

    if not shutil.which("apt-get"):
        raise ValidationError(
            "系统上没有 apt-get，无法自动获取虚拟显示组件。"
            "请手动安装 xvfb、x11vnc、websockify、openbox、novnc 后重试。"
        )
    if not shutil.which("dpkg"):
        raise ValidationError("系统上没有 dpkg，无法解包显示组件。")

    # 1) 解析完整依赖闭包（Inst 行 = 需要新装/升级的；已装的可执行文件系统 PATH 也能找到）
    log("正在解析显示组件的依赖闭包…")
    res = run(["apt-get", "install", "-s"] + list(XSTACK_PACKAGES), timeout=300)
    if not res.ok:
        raise ValidationError(f"apt 依赖解析失败：{res.tail[:400]}")
    packages: set[str] = set()
    for line in res.stdout.splitlines():
        if line.startswith("Inst "):
            packages.add(line.split()[1])
    # 把主包也加进去（已装时不会出现在 Inst 行，但我们要捆绑它们）
    packages.update(XSTACK_PACKAGES)
    if not packages:
        raise ValidationError("apt 没有解析出任何需要下载的包，请检查软件源。")
    log(f"需要下载 {len(packages)} 个包")

    # 2) 只下载，不安装
    log("下载中（不会修改系统包状态）…")
    dl = run(["apt-get", "download", "--print-uris"] + sorted(packages), cwd=debs, timeout=300)
    res = run(["apt-get", "download"] + sorted(packages), cwd=debs, timeout=timeout)
    if not res.ok:
        raise ValidationError(
            f"下载显示组件失败：{res.tail[:400]}\n"
            "如果这是网络问题，可在「系统」页稍后重试，或手动安装这些包后把 "
            "native.xstack_source 改为 system。")
    debs_found = sorted(debs.glob("*.deb"))
    if not debs_found:
        raise ValidationError("下载完成但没有得到任何 .deb 文件，请检查软件源配置。")
    log(f"已下载 {len(debs_found)} 个 .deb（{sum(f.stat().st_size for f in debs_found) // 1024 // 1024} MiB）")

    # 3) 解包到临时根，全部成功后再原子换入
    tmp_root.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for deb in debs_found:
        r = run(["dpkg", "-x", str(deb), str(tmp_root)], timeout=300)
        if not r.ok:
            # 覆盖已有文件的冲突按非致命处理（dpkg -x 会跳过并返回非零）
            failed.append(deb.name)
    log(f"解包完成（{len(debs_found) - len(failed)}/{len(debs_found)} 个成功）")

    final = xstack_root()
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.rename(str(tmp_root), str(final))

    status = tool_status()
    if not status["ok"]:
        raise ValidationError(
            "显示栈解包后仍缺少必要组件：" + ", ".join(status["missing_required"])
            + "。请把 native.xstack_source 改为 system 并手动安装后重试。"
        )
    atomic_write_json(xstack_meta_path(), {
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "packages": sorted(packages),
        "deb_count": len(debs_found),
        "failed_extract": failed[:20],
        "root": str(final),
        "source": "apt-get download + dpkg -x（未修改系统包数据库）",
    }, mode=0o644)
    log(f"显示栈已就绪：{final}")
    return {"installed": True, "skipped": False, **status}


def uninstall() -> bool:
    """删除捆绑的显示栈（卸载「删除全部数据」时调用）。"""
    base = C.PKGHOME / "xstack"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
        return True
    return False
