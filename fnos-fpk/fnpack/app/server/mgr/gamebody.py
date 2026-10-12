"""游戏本体平台识别与校验。

为什么需要：本项目跑在**飞牛 OS（Linux）**上，而《星露谷物语》同时发行
Windows / macOS / Linux 三个版本。用户上传 ZIP 时很容易传错平台 ——
而 Windows 版**不含 Linux 的 .NET 运行时**（没有 `libhostfxr.so` / `libcoreclr.so`），
装上去以后启动器只会报 `You must install .NET to run this application`，
用户很难从这句话联想到"我传错平台了"（实测踩到）。

因此在**安装前**就把平台判清楚，给出明确、可操作的中文提示。

判定依据（实测对比 Linux 版与 Windows 版的目录内容）：

| 标记 | Linux 版 | Windows 版 |
| :--- | :--- | :--- |
| `libhostfxr.so` / `libcoreclr.so` | ✅ 有 | ❌ 无 |
| `Stardew Valley.exe` | ❌ 无 | ✅ 有 |
| `StardewModdingAPI.exe` | ❌ 无 | ✅ 有 |
| `.so` 文件个数 | 十几个 | 0 |
| `createdump` / `createdump.exe` | `createdump` | `createdump.exe` |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLATFORM_LINUX = "linux"
PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
PLATFORM_UNKNOWN = "unknown"

# Linux 上必须存在的 .NET 原生宿主库；缺了就无法启动自包含应用
REQUIRED_LINUX_RUNTIME = ("libhostfxr.so", "libcoreclr.so", "libhostpolicy.so")
# 用于识别平台的特征文件
WINDOWS_MARKERS = ("Stardew Valley.exe", "StardewModdingAPI.exe", "createdump.exe")
MACOS_MARKERS = ("Stardew Valley.app", "StardewModdingAPI.bundle")
LINUX_MARKERS = ("libhostfxr.so", "libcoreclr.so", "StardewModdingAPI")


@dataclass
class GameBodyReport:
    root: Path
    platform: str = PLATFORM_UNKNOWN
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "platform": self.platform, "errors": self.errors,
                "warnings": self.warnings, "info": self.info}


def detect_platform(root: Path) -> str:
    """判断游戏本体是哪个平台的构建。"""
    root = Path(root)
    if not root.is_dir():
        return PLATFORM_UNKNOWN

    def has(name: str) -> bool:
        return (root / name).exists()

    if any(has(n) for n in MACOS_MARKERS):
        return PLATFORM_MACOS
    so_count = sum(1 for p in root.glob("*.so"))
    if has("libhostfxr.so") or has("libcoreclr.so") or so_count >= 3:
        return PLATFORM_LINUX
    if any(has(n) for n in WINDOWS_MARKERS) or has("Microsoft.DiaSymReader.Native.amd64.dll"):
        return PLATFORM_WINDOWS
    return PLATFORM_UNKNOWN


def inspect(root: Path) -> GameBodyReport:
    """安装前校验游戏本体：平台、主程序集、资源目录、自包含运行时。"""
    root = Path(root)
    rep = GameBodyReport(root=root)
    if not root.is_dir():
        rep.errors.append(f"游戏目录不存在：{root}")
        return rep

    rep.platform = detect_platform(root)
    names = {p.name for p in root.iterdir()} if root.is_dir() else set()
    so_files = sorted(p.name for p in root.glob("*.so"))
    rep.info = {
        "platform": rep.platform,
        "has_dll": "Stardew Valley.dll" in names,
        "has_content": (root / "Content").is_dir(),
        "has_deps_json": "Stardew Valley.deps.json" in names,
        "so_count": len(so_files),
        "so_files": so_files[:24],
        "missing_runtime": [n for n in REQUIRED_LINUX_RUNTIME if n not in names],
    }

    if rep.platform == PLATFORM_WINDOWS:
        rep.errors.append(
            "这是 **Windows 版**游戏（含 `Stardew Valley.exe`，且没有任何 `.so` 运行时文件），"
            "本机是飞牛 OS（Linux），**无法运行**。请提供 **Linux 版**游戏本体"
            "（Steam 上右键游戏 → 属性 → 兼容性，或从 Linux 客户端下载；"
            "Linux 版目录里应有 `libhostfxr.so`、`libcoreclr.so` 等十几个 .so 文件，"
            "且没有 `Stardew Valley.exe`）。")
    elif rep.platform == PLATFORM_MACOS:
        rep.errors.append(
            "这看起来是 **macOS 版**游戏，本机是 Linux，无法运行。请提供 **Linux 版**游戏本体。")
    elif rep.platform == PLATFORM_UNKNOWN:
        rep.warnings.append(
            "无法判断游戏本体的平台（既没有 Linux 的 `.so` 运行时，也没有 Windows 的 `.exe`）。"
            "请确认上传的是完整的 Linux 版游戏目录。")

    if "Stardew Valley.dll" not in names:
        rep.errors.append("缺少主程序集 `Stardew Valley.dll`，这不是完整的游戏目录。")
    if not (root / "Content").is_dir():
        rep.errors.append("缺少 `Content/` 资源目录，游戏无法启动。")

    # 平台判为 Linux 时，进一步确认是自包含发布（否则需要系统装 .NET）
    if rep.platform == PLATFORM_LINUX:
        missing = rep.info["missing_runtime"]
        if missing:
            rep.errors.append(
                "Linux 版游戏缺少 .NET 运行时文件：" + "、".join(missing)
                + "。这说明它不是 **.NET 6 自包含**发布，本机需要另行安装 .NET 运行时才能启动。"
                  "请提供自包含发布的游戏目录。")
    return rep
