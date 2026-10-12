"""原生运行后端：直接在本机运行 游戏 + SMAPI + 虚拟显示栈。

实测依据（详见 docs/native-deployment.md）：
  * 游戏本体与 SMAPI 是 .NET 6 **自包含**发布（runtimeconfig 为 `includedFrameworks`），
    机器上不需要安装 .NET 运行时；`ldd StardewModdingAPI` 没有任何 dotnet 依赖。
  * 需要的只有虚拟显示栈：Xvfb + 窗口管理器 + x11vnc（+ websockify/noVNC 供 WebVNC），
    由 `mgr.xstack` 捆绑在应用目录内，运行时不改系统包状态。
  * 对照实验：`DISPLAY` 指向不存在的显示器时游戏报
    "Your graphics card isn't compatible with Stardew Valley"；
    由捆绑 Xvfb 提供真实显示器后，`[SMAPI] Mods loaded and ready!` 正常输出。

进程模型（除游戏外，全部由管理器从应用目录启动，可精确按 PID 停止）：

    Xvfb :99 ──┬─ openbox（窗口管理器，游戏需要它才能正常建窗）
               └─ x11vnc :5900 ── websockify :5800（noVNC，可选）

    StardewModdingAPI（前台子进程，stdout/stderr → console.log）
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as C
from . import consolews
from . import xstack
from .runtime_base import (
    invalidate_port_cache,
    CommandResult, RuntimeStatus, MODE_NATIVE, Runtime, list_pids_matching,
    pid_alive, port_conflict_owner, port_open, proc_rss_kb, proc_started_at,
)
from .util import ValidationError, atomic_write_json, atomic_write_text, run

WAYLAND_SOCKET = "valleyserver-wl"


def find_tool_ok(name: str) -> bool:
    """显示组件是否可用（优先用捆绑的，其次系统 PATH）。"""
    return bool(xstack.find_tool(name) or shutil.which(name))
CONSOLE_LOG_NAME = "console.log"
CONSOLE_ROTATE_BYTES = 5 * 1024 * 1024
CONSOLE_KEEP_BYTES = 1 * 1024 * 1024


@dataclass
class ProcSpec:
    name: str
    pidfile: Path
    pattern: str          # 用于兜底查找的 cmdline 正则
    port: int = 0
    required: bool = True



def is_loopback(bind: str) -> bool:
    """判断绑定地址是否只限本机（127.0.0.1 / ::1 / localhost）。"""
    return (bind or "").strip() in ("127.0.0.1", "::1", "localhost", "[::1]")


class NativeRuntime(Runtime):
    mode = MODE_NATIVE

    def __init__(self) -> None:
        # 探测结果短缓存（见 invalidate_cache 的说明）
        self._probe_cache: dict[str, tuple[float, Any]] = {}
        # 受管子进程对象（按名字）。
        # 两个用途：
        #   1) 向**游戏进程的 stdin 写 SMAPI 控制台指令**（进程不再用 DEVNULL 起）；
        #   2) 回收僵尸：此前从不 wait/poll，失败的启动会留下 <defunct> 直到管理器重启
        #      （实测在真机上留下过 3 个僵尸进程）。
        self._children: dict[str, subprocess.Popen] = {}
        # 状态模型用：停止过程中标记「停止中」；载档指令已发但端口未就绪时标记「待确认」
        self._stopping = False
        self._host_pending = False
        self.rundir = C.RUNDIR
        self.game_dir = C.GAME_FILES_DIR
        self.console = C.GAME_DIR / CONSOLE_LOG_NAME
        self.selected = C.GAME_DIR / "selected-save"

    # ── 路径 ────────────────────────────────────────────────────────────────
    @property
    def game_root(self) -> Path:
        """游戏本体目录（含 StardewModdingAPI）。"""
        return self.game_dir / "Stardew Valley"

    @property
    def launcher(self) -> Path:
        return self.game_root / "StardewModdingAPI"

    def mods_dir(self) -> Path:
        """原生模式下 MOD 目录就是游戏目录里的 Mods（SMAPI 的标准位置）。"""
        return self.game_root / "Mods"

    def _specs(self, cfg: C.ManagerConfig) -> list[ProcSpec]:
        n = cfg.native
        disp = f":{n.display_number}"
        if self.effective_backend(cfg) == "weston":
            # 核显后端：Weston headless 用 GPU 渲染，再让 Xwayland 提供 X11 给游戏。
            # Xvfb 不支持硬件 GLX，所以这是唯一能让游戏用上核显的路径（实测 CPU 约降 2.6 倍）。
            head = [
                ProcSpec("weston", self.rundir / "weston.pid",
                         r"weston\s+.*--socket[= ]" + re.escape(WAYLAND_SOCKET), 0, True),
                ProcSpec("xwayland", self.rundir / "xwayland.pid",
                         rf"Xwayland\s+{re.escape(disp)}", 0, True),
            ]
        else:
            head = [
                ProcSpec("xvfb", self.rundir / "xvfb.pid",
                         rf"Xvfb\s+{re.escape(disp)}", 0, True),
            ]
        return head + [
            ProcSpec("openbox", self.rundir / "openbox.pid", r"openbox", 0, False),
            ProcSpec("x11vnc", self.rundir / "x11vnc.pid", r"x11vnc", int(n.vnc_port), False),
            ProcSpec("websockify", self.rundir / "websockify.pid", r"websockify",
                     int(n.webvnc_port), False),
            ProcSpec("game", self.rundir / "game.pid", r"StardewModdingAPI", 0, True),
        ]

    # ── 核显后端（Weston headless + Xwayland）辅助 ──────────────────────────
    def _wayland_runtime_dir(self) -> Path:
        """Wayland 的运行目录。必须在应用家目录内，且权限 0700（协议要求）。"""
        d = C.PKGHOME / "xstack" / "run"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    def _weston_argv(self, cfg: C.ManagerConfig) -> list[str]:
        weston = xstack.find_tool("weston") or shutil.which("weston") or "weston"
        return [weston,
                "--backend=headless-backend.so",
                "--renderer=gl",                    # 用 GPU 渲染；起不来会明确报错
                f"--socket={WAYLAND_SOCKET}",
                # 虚拟输出尺寸跟随配置，否则默认 1024x640 与用户设置不符
                f"--width={int(cfg.native.screen_width)}",
                f"--height={int(cfg.native.screen_height)}",
                "--idle-time=0",
                f"--log={C.LOG_DIR / 'weston.log'}"]

    def _xwayland_argv(self, cfg: C.ManagerConfig) -> list[str]:
        xw = xstack.find_tool("Xwayland") or shutil.which("Xwayland") or "Xwayland"
        # 两个实测踩到的坑：
        #  1) Xwayland **不支持** Xorg 的 `-logfile` 参数，传了会直接启动失败；
        #  2) **不能用 `-rootless`**：rootless 模式下没有根窗口，
        #     x11vnc 抓屏时 `X_GetImage` 报 `BadMatch (invalid parameter attributes)`
        #     并崩溃 —— 表现是"游戏在跑但 VNC 连不上"。
        #     非 rootless（rootful）模式有真正的根窗口，x11vnc 可正常工作。
        return [xw, f":{cfg.native.display_number}",
                "-noreset", "-nolisten", "tcp"]

    def effective_backend(self, cfg: C.ManagerConfig | None = None) -> str:
        """解析**实际使用**的显示后端。

        `display_backend="auto"`（默认）表示：本机有可用的核显栈就用 `weston`，
        否则退回 `xvfb`。这样"有 GPU 的机器默认就用上 GPU"，
        而没有 GPU 的机器也不会因此起不来。
        """
        cfg = cfg or C.load_config()
        want = getattr(cfg.native, "display_backend", "auto")
        if want in ("xvfb", "weston"):
            return want
        return "weston" if self.gpu_status()["available"] else "xvfb"

    def gpu_status(self) -> dict[str, Any]:
        """核显可用性与**当前实际使用的渲染器**（供界面与校验使用）。

        为什么要报渲染器：光看"有没有 GPU"不够 —— 用户需要能确认游戏**真的**用上了
        核显。Weston 的日志里会写 `GL renderer:`，直接读出来给用户看，比让他猜可靠。
        """
        render = [p for p in Path("/dev/dri").glob("renderD*")] if Path("/dev/dri").is_dir() else []
        weston = bool(xstack.find_tool("weston") or shutil.which("weston"))
        xwayland = bool(xstack.find_tool("Xwayland") or shutil.which("Xwayland"))
        renderer = ""
        try:
            log = (C.LOG_DIR / "weston.log").read_text(errors="replace")
            m = re.findall(r"GL renderer:\s*(.+)", log)
            if m:
                renderer = m[-1].strip()
        except OSError:
            pass
        return {
            "available": bool(render) and weston and xwayland,
            "render_nodes": [str(p) for p in render],
            "weston": weston,
            "xwayland": xwayland,
            "renderer": renderer,
            "software_renderer": "llvmpipe" in renderer.lower() or "swrast" in renderer.lower(),
            "missing": [name for name, ok in (("weston", weston),
                                              ("Xwayland", xwayland),
                                              ("/dev/dri/renderD*", bool(render))) if not ok],
        }

    # ── 配置渲染 ────────────────────────────────────────────────────────────
    def render(self, cfg: C.ManagerConfig | None = None) -> str:
        cfg = cfg or C.load_config()
        n = cfg.native
        lines = [
            "# 原生部署启动描述（由 ValleyServer 管理器生成，仅用于展示/自检）",
            "# 运行方式：原生（直接在本机运行）",
            f"DISPLAY=:{n.display_number}   屏幕 {n.screen_width}x{n.screen_height}x{n.screen_depth}",
            "",
            "[显示栈]",
            f"  {' '.join(self._xvfb_argv(cfg))}",
            f"  {shutil.which('openbox') or xstack.find_tool('openbox')} &",
            f"  {' '.join(self._x11vnc_argv(cfg))}",
            f"  {' '.join(self._websockify_argv(cfg))}",
            "",
            "[游戏]",
            f"  cwd={self.game_root}",
            f"  HOME={self._game_home(cfg)}",
            f"  {' '.join(self._game_argv(cfg))}",
            "",
            "[MOD 目录]",
            f"  {self.mods_dir()}",
            "",
            "[存档目录]",
            f"  {C.SAVES_DIR}  →  $HOME/.config/StardewValley/Saves",
        ]
        return "\n".join(lines)

    # 子进程角色 → 需要哪些"显示相关"环境变量
    #   x11      : 普通 X11 客户端（openbox / x11vnc / websockify / 游戏）
    #   weston   : Wayland 合成器本身（创建 socket，不需要 DISPLAY/WAYLAND_DISPLAY）
    #   xwayland : Wayland 客户端（需要 WAYLAND_DISPLAY 才能连上 weston）
    #
    # 为什么要区分：实测踩到 x11vnc 在核显模式下**直接退出**并报
    #   "Wayland display server detected. Wayland sessions are as of now only
    #    supported via -rawfb ... Exiting."
    # 它看到 WAYLAND_DISPLAY 就以为自己在 Wayland 会话里，于是拒绝工作 ——
    # 而它其实只需要通过 -display :N 连 Xwayland 提供的 X11 显示。
    # 结果就是：游戏在跑、VNC 却连不上（L2 端口判据失败）。
    ENV_ROLES = ("x11", "weston", "xwayland")

    def _env(self, cfg: C.ManagerConfig | None = None,
             role: str = "x11") -> dict[str, str]:
        """子进程环境。

        ⚠️ 必须在这里设 DISPLAY：openbox 依赖环境变量 DISPLAY（不像 x11vnc 有 -display 参数）。
        实测踩到：漏设时 openbox 反复报
          "Failed to open the display from the DISPLAY environment variable."
        并退出 —— 而管理器仍显示"游戏在跑"，属于隐性功能缺失。
        同理 HOME 也要一致，游戏据此定位存档目录。
        """
        cfg = cfg or C.load_config()
        env = dict(os.environ)
        env.update(xstack.env_for_tools())
        env["DISPLAY"] = f":{cfg.native.display_number}"
        env["HOME"] = str(self._game_home(cfg))
        if self.effective_backend(cfg) == "weston":
            # 核显模式：XDG_RUNTIME_DIR 必须是 0700 的私有目录，否则 Wayland 拒绝连接。
            env["XDG_RUNTIME_DIR"] = str(self._wayland_runtime_dir())
            # 关键：不能强制软件渲染，否则核显白搭（iris 会被绕过）。
            env.pop("LIBGL_ALWAYS_SOFTWARE", None)
            if role == "weston":
                env.pop("DISPLAY", None)
                env.pop("WAYLAND_DISPLAY", None)
            elif role == "xwayland":
                env["WAYLAND_DISPLAY"] = WAYLAND_SOCKET
            else:
                # X11 客户端：必须**去掉** WAYLAND_DISPLAY，否则 x11vnc 会误判为
                # Wayland 会话并退出（详见上面 ENV_ROLES 的说明）。
                env.pop("WAYLAND_DISPLAY", None)
                env["SDL_VIDEODRIVER"] = "x11"
        elif cfg.native.software_gl:
            env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            env.setdefault("SDL_VIDEODRIVER", "x11")
        env.setdefault("SDL_AUDIODRIVER", "dummy")
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @staticmethod
    def _game_home(cfg: C.ManagerConfig) -> Path:
        """游戏读取的 $HOME —— 存档就在 $HOME/.config/StardewValley/Saves。

        用固定的数据目录（而不是 systemd 的 HOME），保证存档可预测、可快照。
        """
        return C.PKGHOME / "gamehome"

    def _xvfb_argv(self, cfg: C.ManagerConfig) -> list[str]:
        n = cfg.native
        tool = xstack.find_tool("Xvfb")
        return [
            tool or "Xvfb", f":{n.display_number}",
            "-screen", "0", f"{n.screen_width}x{n.screen_height}x{n.screen_depth}",
            "-ac", "+extension", "GLX", "+extension", "RANDR", "+render",
            "-noreset", "-core",
        ]

    def _x11vnc_argv(self, cfg: C.ManagerConfig) -> list[str]:
        n = cfg.native
        tool = xstack.find_tool("x11vnc") or "x11vnc"
        argv = [
            tool, "-display", f":{n.display_number}", "-forever", "-shared",
            "-passwd", n.vnc_password, "-rfbport", str(int(n.vnc_port)),
            "-noxdamage", "-bg", "-o", str(C.LOG_DIR / "x11vnc.log"),
        ]
        # 监听范围要真的生效：以前这个配置项根本没传给进程，
        # 于是界面写着"仅本机"、实际却监听 0.0.0.0（真机实测）。
        if is_loopback(n.host_bind):
            argv.append("-localhost")
        return argv

    def _websockify_argv(self, cfg: C.ManagerConfig) -> list[str]:
        n = cfg.native
        tool = xstack.find_tool("websockify") or "websockify"
        webroot = xstack.novnc_webroot()
        argv = [tool]
        if webroot:
            argv.append(f"--web={webroot}")
        # websockify 的源参数支持 `[host:]port`（websocketproxy.py 用 rsplit(':',1) 解析），
        # 因此把监听范围写进去才真正生效。
        src = f"{n.host_bind}:{int(n.webvnc_port)}" if n.host_bind else str(int(n.webvnc_port))
        argv += [src, f"localhost:{int(n.vnc_port)}"]
        return argv

    def _game_argv(self, cfg: C.ManagerConfig) -> list[str]:
        n = cfg.native
        argv = [str(self.launcher)]
        argv += self._save_args()
        extra = (n.extra_args or "").strip()
        if extra:
            argv += [a for a in extra.split() if a]
        return argv

    def _save_args(self) -> list[str]:
        """从 selected-save 生成 --load-save 参数。"""
        if not self.selected.is_file():
            return []
        from .util import read_json
        raw = read_json(self.selected, {}) or {}
        name = str(raw.get("save", "")).strip() if isinstance(raw, dict) else ""
        if not name or "/" in name or ".." in name:
            return []
        if not (C.SAVES_DIR / name).is_dir():
            return []
        return ["--load-save", name]

    def write_selected_save(self, dir_name: str) -> Path:
        self.ensure_dirs()
        atomic_write_json(self.selected, {"save": dir_name}, mode=0o644)
        return self.selected

    def write_runtime_manifest(self, cfg: C.ManagerConfig | None = None) -> Path:
        """把当前启动描述落盘一份，便于排查「实际按什么参数启动的」。"""
        self.ensure_dirs()
        path = C.GAME_DIR / "runtime-manifest.txt"
        atomic_write_text(path, self.render(cfg), mode=0o644)
        return path

    def ensure_dirs(self) -> None:
        for d in (C.GAME_DIR, self.rundir, self.game_dir, C.SAVES_DIR, C.LOG_DIR,
                  self._game_home(C.load_config()) / ".config" / "StardewValley" / "Saves",
                  self._game_home(C.load_config()) / ".config" / "StardewValley" / "ErrorLogs"):
            d.mkdir(parents=True, exist_ok=True)

    def prepare(self, cfg: C.ManagerConfig | None = None) -> None:
        """启动前准备：目录、存档映射、MOD 目录、显示栈。"""
        cfg = cfg or C.load_config()
        self.ensure_dirs()
        self._link_saves(cfg)
        self._link_mods(cfg)
        if cfg.native.xstack_source == "bundled" and not xstack.is_installed():
            xstack.provision(on_log=lambda m: None)

    def _link_saves(self, cfg: C.ManagerConfig) -> None:
        """让 $HOME/.config/StardewValley/Saves 指向我们管理的 Saves 目录。

        优先用符号链接（避免复制导致存档不一致）；已有真实目录时保留其内容。
        """
        target = self._game_home(cfg) / ".config" / "StardewValley" / "Saves"
        C.SAVES_DIR.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if target.resolve() == C.SAVES_DIR.resolve():
                return
            target.unlink()
        elif target.exists():
            # 已经存在且非空：把内容并入 Saves 后改为链接
            moved = 0
            for child in target.iterdir():
                dest = C.SAVES_DIR / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
                    moved += 1
            if moved:
                pass
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(C.SAVES_DIR)

    def _link_mods(self, cfg: C.ManagerConfig) -> None:
        """把游戏目录里的 Mods **符号链接**到管理器数据区的 Mods。

        为什么必须这样做：ALOS 等 MOD 会把每存档运行数据写在
        `Mods/<MOD>/data/<存档名>.json`（见审计报告 P11），
        而快照只覆盖 `DATA_ROOT/{Saves,Mods}`。
        如果 MOD 留在游戏本体目录里，这些"每存档状态"就**不会进快照**，
        卸载/覆盖 MOD 时会静默丢失。
        统一到 DATA_ROOT/Mods 后：MOD 管理、快照、恢复三处看到的是同一份数据。

        游戏本体目录里原有的 Mods（用户手工放进去的）会先并入数据区，避免丢文件。
        """
        C.MODS_DIR.mkdir(parents=True, exist_ok=True)
        game_mods = self.mods_dir()
        if game_mods.is_symlink():
            if game_mods.resolve() == C.MODS_DIR.resolve():
                return
            game_mods.unlink()
        elif game_mods.is_dir():
            moved = 0
            for child in game_mods.iterdir():
                dest = C.MODS_DIR / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
                    moved += 1
            try:
                game_mods.rmdir()
            except OSError:
                shutil.rmtree(game_mods, ignore_errors=True)
            if moved:
                pass
        game_mods.parent.mkdir(parents=True, exist_ok=True)
        game_mods.symlink_to(C.MODS_DIR)

    # ── 依赖检测 ────────────────────────────────────────────────────────────
    def detect(self) -> dict[str, Any]:
        cfg = C.load_config()
        tools = xstack.tool_status()
        guidance: list[str] = []
        # 让界面/诊断能看到核显状态与当前渲染器
        tools["gpu"] = self.gpu_status()
        if cfg.native.xstack_source == "bundled" and not tools["installed"]:
            guidance.append(
                "虚拟显示组件尚未就绪。可在「系统」页点击「安装显示组件」自动获取"
                "（会下载到应用目录，不修改系统包状态）。"
            )
        if not os.access(str(C.GAME_FILES_DIR), os.W_OK):
            guidance.append(f"游戏数据目录不可写：{C.GAME_FILES_DIR}")
        if not self.launcher.is_file():
            guidance.append(
                "尚未放入游戏本体。原生部署需要 Stardew Valley 的游戏文件（含 SMAPI），"
                "请在「系统」页上传，或从上游 Release 的 Content.zip 解压放入。"
            )
        if self.effective_backend(cfg) == "weston":
            gpu = self.gpu_status()
            tools = dict(tools)
            tools["backend"] = "weston"
            tools["backend_config"] = cfg.native.display_backend
            tools["gpu"] = gpu
            if not gpu["available"]:
                guidance.append(
                    "已选择核显显示后端，但缺少 " + "、".join(gpu["missing"])
                    + "。请安装后重试，或在「配置」页改回 Xvfb（软件渲染）。")
            ok = gpu["available"] and all(
                find_tool_ok(name) for name in ("x11vnc", "openbox", "websockify")
            ) and self.launcher.is_file()
        else:
            tools["backend"] = "xvfb"
            tools["backend_config"] = cfg.native.display_backend
            if cfg.native.display_backend == "auto":
                gpu = self.gpu_status()
                if not gpu["available"]:
                    guidance.append(
                        "未能启用核显（缺少 " + "、".join(gpu["missing"])
                        + "），已自动退回 Xvfb 软件渲染。")
            ok = tools["ok"] and self.launcher.is_file()
        return {
            "mode": MODE_NATIVE,
            "ready": ok,
            "xstack": tools,
            "game": {
                "root": str(self.game_root),
                "launcher": str(self.launcher),
                "launcher_present": self.launcher.is_file(),
                "self_contained_dotnet": True,
                "dotnet_runtime_required": False,
            },
            "guidance": guidance,
        }

    def validate(self, cfg: C.ManagerConfig | None = None) -> dict[str, Any]:
        cfg = cfg or C.load_config()
        errors: list[str] = []
        warnings: list[str] = []
        det = self.detect()
        if self.effective_backend(cfg) == "weston":
            # 核显后端（含 auto 解析出的核显）：不要求 Xvfb，
            # 但必须有 weston + Xwayland + 渲染节点
            gpu = self.gpu_status()
            if not gpu["available"]:
                errors.append(
                    "已选择核显显示后端，但缺少 " + "、".join(gpu["missing"])
                    + "（可改回 Xvfb 软件渲染）")
            elif cfg.native.software_gl:
                warnings.append(
                    "已选择核显后端，但「强制软件渲染」仍为开启；运行时会以核显为准"
                    "（该开关在核显模式下不生效）")
            missing = [b for b in ("x11vnc", "openbox", "websockify") if not find_tool_ok(b)]
            if missing:
                errors.append("缺少显示组件：" + ", ".join(missing))
            if cfg.native.display_backend == "auto":
                warnings.append("显示后端为「自动」：本机有可用核显，将使用核显渲染"
                                "（Weston + Xwayland）。如需强制软件渲染，请改为 xvfb。")
        elif not det["xstack"]["ok"]:
            errors.append("虚拟显示组件缺失：" + ", ".join(det["xstack"]["missing_required"]))
        if not self.launcher.is_file():
            errors.append(
                f"游戏启动器不存在：{self.launcher}（需要先放入游戏本体与 SMAPI）")
        for field_name in ("display_number", "screen_width", "screen_height", "screen_depth"):
            value = getattr(cfg.native, field_name)
            # 注意 bool 是 int 的子类：`int(True)` 合法，必须显式排除
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"native.{field_name} 必须是整数")
                continue
            if not (1 <= value <= 65535):
                errors.append(f"native.{field_name} 超出范围（1-65535）")
        for port_field in ("vnc_port", "webvnc_port", "game_port", "cmdwebui_port"):
            value = getattr(cfg.native, port_field)
            if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 65535):
                errors.append(f"native.{port_field} 必须是 1-65535 的整数")
        if cfg.native.vnc_password and not re.fullmatch(r"[\x21-\x7e]{8}", cfg.native.vnc_password):
            errors.append("VNC 密码必须是恰好 8 位可见 ASCII 字符（VNC 协议限制）")
        if not cfg.native.vnc_password:
            warnings.append("尚未设置 VNC 密码，启动时会自动生成")
        for port in (cfg.native.vnc_port, cfg.native.webvnc_port):
            if port == cfg.panel_tcp_port:
                errors.append(f"端口 {port} 与管理面板端口冲突")
        # 端口占用检测（只报属于其它应用的占用）
        # 端口占用检测：只对**类型已合法**的端口做，否则 int() 会直接抛异常
        # （实测踩到：传 "abc" 时 validate 崩溃，而不是返回"校验失败"）
        for port in (cfg.native.vnc_port, cfg.native.webvnc_port, cfg.native.game_port):
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                continue
            owner = port_conflict_owner(port)
            if owner == -1:
                warnings.append(f"端口 {port} 已被**其它应用**占用，启动可能失败")
        return {"ok": not errors, "errors": errors, "warnings": warnings,
                "guidance": det["guidance"]}

    # ── 运行控制 ────────────────────────────────────────────────────────────
    def _pid_of(self, spec: ProcSpec) -> int:
        try:
            raw = spec.pidfile.read_text().strip()
            pid = int(raw.split()[0])
        except (OSError, ValueError, IndexError):
            return 0
        if pid_alive(pid):
            return pid
        return 0

    def resolve_pid(self, name: str, cfg: C.ManagerConfig | None = None) -> int:
        """统一的进程解析入口：先信 PID 文件，再用归属判定兜底。

        `readiness()` / `stats()` / `stop()` 必须都用这一个入口，
        否则会出现"统计里看着在跑、健康判据却说没在跑"的不一致（实测踩过）。
        """
        cfg = cfg or C.load_config()
        spec = next((s for s in self._specs(cfg) if s.name == name), None)
        if spec is None:
            return 0
        return self._pid_of(spec) or self._find_by_pattern(spec, cfg)

    def _find_by_pattern(self, spec: ProcSpec, cfg: C.ManagerConfig) -> int:
        """兜底：PID 文件不可信（进程 daemon 化）时，按「可执行文件真实路径」确认归属。

        为什么需要这一步（实测踩到）：
          `x11vnc -bg` 与 `openbox` 会 fork/daemon 化，Popen 返回的 PID 很快就不代表
          真正的服务进程；而它们的 cmdline 里既没有应用目录也没有 PKGHOME，
          于是只用「cmdline 命中路径」会把它们误判为"没在运行"。
        正确做法是读 /proc/<pid>/exe：二进制来自我们捆绑的 xstack 目录，就一定是本应用启动的。
        对系统安装的 xstack（xstack_source=system），再退回 cmdline 判据。
        """
        xroot = str(xstack.xstack_root())
        ours = (str(C.PKGHOME), str(C.APPDEST), str(C.GAME_FILES_DIR), str(C.GAME_DIR))
        pids = list_pids_matching(spec.pattern)
        candidates: list[int] = []
        for pid in pids:
            # 1) 可执行文件真实路径落在我们捆绑的显示栈内 → 一定是我们启动的
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = ""
            if exe and xroot and exe.startswith(xroot):
                return pid
            # 2) 进程的工作目录是我们管理的目录（Popen 时 cwd=game_root）
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            if cwd and any(cwd.startswith(o) for o in ours if o):
                return pid
            # 3) 命令行里带我们的目录
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                    b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                cmdline = ""
            if cmdline and any(o and o in cmdline for o in ours):
                return pid
            candidates.append(pid)
        # 系统安装的显示栈：二进制不在应用目录。只接受"唯一候选"，
        # 避免把 NAS 上其它应用的同名进程当成自己的。
        if len(candidates) == 1 and cfg.native.xstack_source == "system":
            return candidates[0]
        return 0

    def status(self) -> RuntimeStatus:
        return self._cached("status", self._status_uncached)

    def _status_uncached(self) -> RuntimeStatus:
        cfg = C.load_config()
        specs = self._specs(cfg)
        game_pid = self.resolve_pid("game", cfg)
        launcher_present = self.launcher.is_file()
        display_name = "weston" if self.effective_backend(cfg) == "weston" else "xvfb"
        display_pid = self.resolve_pid(display_name, cfg)

        if not launcher_present and not game_pid:
            return RuntimeStatus(exists=False, running=False, status="absent",
                                   mode=MODE_NATIVE,
                                   error="尚未放入游戏本体" if not launcher_present else "")
        running = bool(game_pid)
        st = RuntimeStatus(
            exists=True, running=running,
            status="running" if running else "stopped",
            health="none", mode=MODE_NATIVE,
            game_dir=str(self.game_root),
            started_at=proc_started_at(game_pid) if game_pid else "",
        )
        st.error = "" if display_pid or not running else "显示栈未运行"
        return st

    def _spawn(self, argv: list[str], *, logfile: Path, name: str,
               cfg: C.ManagerConfig, role: str = "x11",
               stdin_pipe: bool = False) -> int:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = open(logfile, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(self.game_root), env=self._env(cfg, role=role),
                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            fh.close()
        self._children[name] = proc
        (self.rundir / f"{name}.pid").write_text(f"{proc.pid}\n")
        return proc.pid

    # ── SMAPI 控制台指令（无人值守开服的关键能力）──────────────────────────
    def send_console(self, command: str, *, collect_seconds: float = 2.5,
                     via: str = "auto") -> CommandResult:
        """向 SMAPI 控制台发送一条指令，并尽量取回输出。

        **通道选择（实测结论，不是猜的）**：
          * stdin 管道**不通** —— 即便把游戏进程的 stdin 接成管道且写入成功，
            游戏侧没有任何线程读取它（线程 wchan 无管道读），`help` 发出去
            控制台毫无回显。第一版就是这么做的，实测失败。
          * 上游 `CommandWebUI` 模组的 WebSocket（`ws://127.0.0.1:<cmdwebui_port>/ws`）
            **可用**：实测发 `help` 能收到 SMAPI 的完整帮助输出。

        ⚠️ **风险提示（务必让用户知情）**：该模组的 WS 写循环没有捕获异常
        （`Mods/CommandWebUI/Server.cs:129`），客户端断开时它可能因
        "Broken pipe" 抛出**未处理异常并让整个游戏进程崩溃**（本项目实测触发过一次）。
        因此调用方必须把它当成高风险操作，并且本函数在关闭前会尽可能排空对端输出。
        """
        cmd = (command or "").strip()
        if not cmd:
            return CommandResult(tuple(), returncode=1, stderr="指令不能为空")
        if "\n" in cmd or "\r" in cmd:
            return CommandResult(tuple(), returncode=1, stderr="指令不能包含换行")
        if len(cmd) > 500:
            return CommandResult(tuple(), returncode=1, stderr="指令过长（上限 500 字符）")

        cfg = C.load_config()
        port = int(cfg.native.cmdwebui_port)
        reply = consolews.send_command("127.0.0.1", port, cmd,
                                       collect_seconds=collect_seconds)
        text = reply.text
        if not reply.ok:
            return CommandResult(
                tuple(), returncode=1,
                stderr=(f"无法通过 SMAPI 控制台通道发送指令（127.0.0.1:{port}）："
                        f"{reply.error}。"
                        "请确认 CommandWebUI 模组已安装且服务器正在运行。"))
        # 把模组/游戏的输出原样带回，便于界面显示与实际核对
        return CommandResult(tuple(), returncode=0,
                             stdout=f"已发送：{cmd}" + (f"\n{text}" if text else ""))

    def reap_children(self) -> list[str]:
        """回收已结束的子进程，避免留下僵尸。

        此前从不 wait/poll（只写 PID 文件），失败的启动会在 `ps` 里留下
        `<defunct>` 直到管理器重启 —— 实测在真机上留下过 3 个。
        """
        reaped: list[str] = []
        for name, proc in list(self._children.items()):
            try:
                if proc.poll() is not None:
                    proc.wait(timeout=1)
                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass
                    self._children.pop(name, None)
                    reaped.append(name)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return reaped

    def _stop_pid(self, pid: int, *, grace: int, label: str) -> None:
        if not pid_alive(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + max(1, grace)
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    # ── 探测结果短缓存 ──────────────────────────────────────────────────────
    # 实测：status+readiness+stats 合计约 52ms（要走 /proc 扫进程与端口）。
    # 界面每 5 秒轮询一次、多个标签页还会叠加，所以这里做 1.5 秒 TTL 缓存：
    # 用户感知不到延迟，重复轮询的开销降到接近 0；启停等写操作会主动失效缓存。
    PROBE_TTL = 1.5

    def _cached(self, key: str, producer):
        now = time.monotonic()
        hit = self._probe_cache.get(key)
        if hit and (now - hit[0]) < self.PROBE_TTL:
            return hit[1]
        value = producer()
        self._probe_cache[key] = (now, value)
        return value

    def invalidate_cache(self) -> None:
        self._probe_cache.clear()
        invalidate_port_cache()

    # ── X 显示号占用处理 ────────────────────────────────────────────────────
    def _display_holders(self, display: int) -> list[int]:
        """列出正在使用 `:display` 的进程（按 cmdline 匹配 `:N`）。"""
        return list_pids_matching(rf"[: ]:{display}(\s|$)")

    def _start_weston_stack(self, cfg: C.ManagerConfig,
                            specs: dict[str, ProcSpec]) -> str:
        """启动 Weston headless（GPU）+ Xwayland。返回空串表示成功，否则返回原因。"""
        gpu = self.gpu_status()
        if not gpu["available"]:
            return ("核显显示后端不可用：缺少 " + "、".join(gpu["missing"])
                    + "。请在「配置」页把显示后端改回 Xvfb（软件渲染）。")

        # 显示号占用检查（与 Xvfb 同一套逻辑：清陈旧、复用自有、别人占用则报错）
        problem = self._prepare_display(cfg)
        if problem:
            return problem

        log = C.LOG_DIR
        if not self._pid_of(specs["weston"]):
            argv = self._weston_argv(cfg)
            if not (os.path.isabs(argv[0]) or shutil.which(argv[0])):
                return f"找不到 weston：{argv[0]}"
            self._spawn(argv, logfile=log / "weston.log", name="weston", cfg=cfg,
                        role="weston")
            # 等 Wayland socket 出现
            sock = self._wayland_runtime_dir() / WAYLAND_SOCKET
            if not self._wait_for(lambda: sock.exists(), 20):
                tail = self._tail_log(log / "weston.log")
                return ("Weston 启动后未创建 Wayland socket。" + tail)

        if not self._pid_of(specs["xwayland"]):
            argv = self._xwayland_argv(cfg)
            if not (os.path.isabs(argv[0]) or shutil.which(argv[0])):
                return f"找不到 Xwayland：{argv[0]}"
            # Xwayland 必须能看到 WAYLAND_DISPLAY 与 XDG_RUNTIME_DIR
            self._spawn(argv, logfile=log / "xwayland.log", name="xwayland", cfg=cfg,
                        role="xwayland")
            xsock = Path(f"/tmp/.X11-unix/X{cfg.native.display_number}")
            if not self._wait_for(lambda: xsock.exists(), 25):
                tail = self._tail_log(log / "xwayland.log")
                return (f"Xwayland 未创建 X socket（显示 :{cfg.native.display_number}）。" + tail)
        return ""

    @staticmethod
    def _wait_for(predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except OSError:
                pass
            time.sleep(0.3)
        return False

    @staticmethod
    def _tail_log(path: Path, limit: int = 300) -> str:
        try:
            text = path.read_text(errors="replace")[-limit:].strip()
        except OSError:
            return ""
        return ("　日志尾部：" + text.replace("\n", " ")) if text else ""

    def _prepare_display(self, cfg: C.ManagerConfig, on_log=None) -> str:
        """确保 `:N` 可用；返回空串表示可用，否则返回不可用的原因。

        三种情况（实测都踩过，第一种是我自己的测试留下的）：
          1. 锁文件/socket 存在，但**没有任何进程**在用 → 陈旧残留，清掉（崩溃/重启后会这样）；
          2. 有进程在用，且是**本应用**的 Xvfb → 直接复用，不必重启；
          3. 有进程在用，但是**别人的** X server → 不动它，明确报错让用户改显示号。
        """
        log = on_log or (lambda _m: None)
        num = int(cfg.native.display_number)
        lock = Path(f"/tmp/.X{num}-lock")
        sock = Path(f"/tmp/.X11-unix/X{num}")
        holders = [pid for pid in self._display_holders(num) if pid_alive(pid)]
        ours = [pid for pid in holders if self._owned_by_app(pid)]

        if not holders:
            if lock.exists() or sock.exists():
                log(f"发现陈旧的 X 显示痕迹（:${num} 无进程占用），已清理")
                try:
                    lock.unlink(missing_ok=True)
                    if sock.exists() and not sock.is_socket():
                        sock.unlink(missing_ok=True)
                    elif sock.is_socket():
                        sock.unlink(missing_ok=True)
                except OSError as exc:
                    return f"清理陈旧的 X 痕迹失败（{lock} / {sock}）：{exc}"
            return ""

        if ours:
            return ""      # 我们自己的 Xvfb 还在，复用

        # 别人占着：不要动
        detail = []
        for pid in holders[:3]:
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
            except OSError:
                cmd = ""
            detail.append(f"pid {pid}: {cmd[:80]}")
        return (f"显示 :{num} 已被其它进程占用，不能启动。" + "；".join(detail)
                + f"。请在「配置」页把「显示编号」改成别的（例如 :{num + 1}）后重试。")

    def _owned_by_app(self, pid: int) -> bool:
        """该进程是否属于本应用（exe / cwd / cmdline 落在应用目录内）。"""
        roots = (str(C.PKGHOME), str(C.APPDEST), str(C.GAME_FILES_DIR), str(C.GAME_DIR))
        for kind in ("exe", "cwd"):
            try:
                target = os.readlink(f"/proc/{pid}/{kind}")
            except OSError:
                continue
            if any(root and root in target for root in roots):
                return True
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            return False
        return any(root and root in cmd for root in roots)

    # ── 显示后端切换的遗留清理 ──────────────────────────────────────────────
    def _backend_specs(self, cfg: C.ManagerConfig, backend: str) -> list[ProcSpec]:
        """按指定后端构造进程定义（不改变当前配置）。"""
        import copy
        other = copy.deepcopy(cfg)
        other.native.display_backend = backend
        return self._specs(other)

    def _all_specs(self, cfg: C.ManagerConfig) -> list[ProcSpec]:
        """**两个后端**的进程定义合集（同名去重）。

        为什么需要：`stop()` 只按当前后端的 specs 停止，
        切换后端（例如从核显切回 Xvfb）时另一个后端的进程会残留 ——
        残留的 Xwayland 会继续占着 `:99`，导致 Xvfb 启动失败
        "Cannot establish any listening sockets - Make sure an X server isn't already running"。
        """
        seen: dict[str, ProcSpec] = {}
        for backend in ("xvfb", "weston"):
            for spec in self._backend_specs(cfg, backend):
                seen.setdefault(spec.name, spec)
        return list(seen.values())

    def _cleanup_stale_display(self, cfg: C.ManagerConfig) -> list[str]:
        """启动前清掉**本应用自己的、已失去跟踪的**显示栈进程（同后端也要清）。

        真机踩到（0.2.14）：旧 weston 栈的 Xwayland 仍在运行并占着 `:99`
        （它的 PID 文件已被新进程覆盖 / 指向已死进程），此时启动新栈必然失败：

            Xwayland 未创建 X socket（显示 :99）
            … server already running … Cannot establish any listening sockets

        之前的 `_stop_stale_backend_stack()` 只处理"另一个后端"（weston ↔ xvfb），
        **同后端的陈旧栈会漏掉** —— 这里按"是否仍被 PID 文件跟踪"来判断：
        受跟踪且存活 → 保留（正常栈）；否则视为孤儿 → 停掉并清理 socket。
        """
        specs = {s.name: s for s in self._all_specs(cfg)}
        stopped: list[str] = []
        cleaned_pidfile = False
        for name in ("weston", "xwayland", "xvfb", "openbox", "x11vnc", "websockify"):
            spec = specs.get(name)
            if spec is None:
                continue
            tracked = 0
            try:
                tracked = int(spec.pidfile.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                tracked = 0
            for pid in list_pids_matching(spec.pattern):
                if pid == tracked and pid_alive(tracked):
                    continue          # 受跟踪且活着 → 是正常栈，别动
                if not self._owned_by_app(pid):
                    continue          # 不是本应用的进程，绝不碰
                self._stop_pid(pid, grace=5, label=name)
                stopped.append(f"{name}:{pid}")
                cleaned_pidfile = True
            if cleaned_pidfile:
                try:
                    spec.pidfile.unlink()
                except OSError:
                    pass
                cleaned_pidfile = False
        if stopped:
            self._cleanup_sockets(cfg)
        return stopped

    def _stop_stale_backend_stack(self, cfg: C.ManagerConfig) -> list[str]:
        """停掉"另一个显示后端"遗留的本应用进程，返回被停掉的名字。

        实测踩到（真机）：从核显模式回滚到 Xvfb 时，Weston/Xwayland 还占着 `:99`，
        而 `_prepare_display()` 认定"是本应用自己的"就放行了 → Xvfb 起不来。
        """
        current = self.effective_backend(cfg)
        stale_backend = "xwayland" if current == "xvfb" else "xvfb"
        stopped: list[str] = []
        wanted = (("weston", "xwayland") if current == "xvfb" else ("xvfb",))
        for spec in self._backend_specs(cfg, stale_backend):
            if spec.name not in wanted:
                continue
            pid = self.resolve_pid(spec.name, cfg) or self._find_by_pattern(spec, cfg)
            if not pid:
                continue
            # 只停"本应用自己的"进程（_find_by_pattern 已经按归属筛选过）
            self._stop_pid(pid, grace=5, label=spec.name)
            try:
                spec.pidfile.unlink()
            except OSError:
                pass
            stopped.append(spec.name)
        return stopped

    def start(self, *, timeout: int = 300) -> CommandResult:
        self.invalidate_cache()
        cfg = C.load_config()
        det = self.detect()
        if not det["xstack"]["ok"]:
            return CommandResult(tuple(), returncode=1,
                                 stderr="虚拟显示组件缺失：" + ", ".join(det["xstack"]["missing_required"]))
        if not self.launcher.is_file():
            return CommandResult(tuple(), returncode=1,
                                 stderr=f"游戏启动器不存在：{self.launcher}")
        if self.status().running:
            return CommandResult(tuple(), returncode=0, stdout="已在运行")

        self.prepare(cfg)
        # 先清掉"另一个显示后端"遗留的进程（切换/回滚后必有残留），
        # 否则 Xvfb / Weston 会因显示号被占而启动失败。
        stale = self._stop_stale_backend_stack(cfg)
        if stale:
            LOG.info(f"已清理上一个显示后端的残留进程：{', '.join(stale)}")
        # 同后端的陈旧栈（例如上一轮遗留的 Xwayland 仍占着 :99）也要清 ——
        # 否则新 Xwayland 会 "server already running" 起不来（真机实测）。
        orphans = self._cleanup_stale_display(cfg)
        if orphans:
            LOG.info(f"已清理失去跟踪的显示栈进程：{', '.join(orphans)}")
        self._rotate_console()
        specs = {s.name: s for s in self._specs(cfg)}

        # 1) 显示服务器：Xvfb（软件）或 Weston+Xwayland（核显）
        #    auto 模式在这里已经解析成具体后端（有核显就是 weston）
        if self.effective_backend(cfg) == "weston":
            problem = self._start_weston_stack(cfg, specs)
            if problem:
                return CommandResult(tuple(), returncode=1, stderr=problem)
        elif "xvfb" in specs and not self._pid_of(specs["xvfb"]):
            # 先处理显示号占用/陈旧痕迹 —— 否则 Xvfb 只会报
            # "Cannot establish any listening sockets"，用户完全看不懂
            problem = self._prepare_display(cfg)
            if problem:
                return CommandResult(tuple(), returncode=1, stderr=problem)
            argv = self._xvfb_argv(cfg)
            if not os.access(argv[0], os.X_OK):
                return CommandResult(tuple(argv), returncode=1,
                                     stderr=f"Xvfb 不可执行：{argv[0]}")
            self._spawn(argv, logfile=C.LOG_DIR / "xvfb.log", name="xvfb", cfg=cfg)
            # 等它真的起来（X socket 出现）
            sock = Path(f"/tmp/.X11-unix/X{cfg.native.display_number}")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not sock.exists():
                time.sleep(0.3)
            if not sock.exists():
                tail = ""
                try:
                    tail = (C.LOG_DIR / "xvfb.log").read_text(errors="replace")[-400:].strip()
                except OSError:
                    pass
                return CommandResult(tuple(argv), returncode=1, stderr=(
                    f"Xvfb 启动后未创建 X socket（显示 :{cfg.native.display_number}）。"
                    + ("Xvfb 输出：" + tail.replace("\n", " ")[-300:] if tail else
                       "请查看 xvfb.log")))

        # 2) 窗口管理器（游戏需要）
        wm = xstack.find_tool(cfg.native.window_manager) or \
            xstack.find_tool("openbox") or shutil.which(cfg.native.window_manager)
        if wm and not self._pid_of(specs["openbox"]):
            self._spawn([wm], logfile=C.LOG_DIR / "openbox.log", name="openbox", cfg=cfg)
            time.sleep(1.5)

        # 3) x11vnc
        if not self._pid_of(specs["x11vnc"]):
            argv = self._x11vnc_argv(cfg)
            if argv[0] and not os.access(argv[0], os.X_OK):
                argv[0] = "x11vnc"
            self._spawn(argv, logfile=C.LOG_DIR / "x11vnc.log", name="x11vnc", cfg=cfg)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not port_open(int(cfg.native.vnc_port)):
                time.sleep(0.4)

        # 4) websockify + noVNC（可选：没有 noVNC 也能跑，只是没有网页画面）
        if not self._pid_of(specs["websockify"]):
            webroot = xstack.novnc_webroot()
            if webroot:
                self._spawn(self._websockify_argv(cfg),
                            logfile=C.LOG_DIR / "websockify.log", name="websockify", cfg=cfg)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not port_open(int(cfg.native.webvnc_port)):
                    time.sleep(0.4)

        # 5) 游戏（前台进程，输出重定向到 console.log）
        env = self._env(cfg)
        argv = self._game_argv(cfg)
        console = open(self.console, "ab", buffering=0)
        try:
            # stdin 用管道：管理器要能向 SMAPI 控制台**发指令**
            # （例如 ServerCMD.new_save / load_save / set_multiplayermode 2）。
            # 用 /dev/null 时游戏进程收不到任何输入，无人值守就永远开不了服。
            proc = subprocess.Popen(
                argv, cwd=str(self.game_root), env=env,
                stdin=subprocess.PIPE, stdout=console, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            console.close()
            return CommandResult(tuple(argv), returncode=1, stderr=f"启动游戏失败：{exc}")
        finally:
            try:
                console.close()
            except Exception:  # noqa: BLE001
                pass
        # ⚠️ 必须登记：游戏是在这里内联 Popen 的（不走 _spawn）。
        # 漏登记的后果实测过：stdin 明明是管道，但 send_console 报
        # "游戏进程未在运行（或不是由本管理器启动）"，指令发不出去。
        self._children["game"] = proc
        (self.rundir / "game.pid").write_text(f"{proc.pid}\n")
        return CommandResult(tuple(argv), returncode=0,
                             stdout=f"已启动原生游戏进程 pid={proc.pid}")

    def stop(self, *, timeout: int = 180) -> CommandResult:
        # 状态模型：「停止中」是独立状态，不能显示成"已停止"
        self._stopping = True
        try:
            return self._stop_locked(timeout=timeout)
        finally:
            self._stopping = False

    def _stop_locked(self, *, timeout: int = 180) -> CommandResult:
        cfg = C.load_config()
        # 用**两个后端**的合集：否则切换后端后，另一个后端的进程会残留
        # （残留的 Xwayland 会继续占着显示号，让 Xvfb 起不来）。
        specs = {s.name: s for s in self._all_specs(cfg)}
        # 先停游戏（给它时间收尾存档），再拆显示栈。
        # 顺序很重要：x11vnc/websockify/openbox 依赖显示服务器，所以显示服务器最后停；
        # 核显模式下还要把 weston/xwayland 一起收掉（否则残留会占住 GPU 与 socket）。
        order = ["game", "websockify", "x11vnc", "openbox", "xwayland", "weston", "xvfb"]
        for name in [n for n in order if n in specs]:
            spec = specs[name]
            pid = self.resolve_pid(name, cfg)
            if pid:
                grace = cfg.native.graceful_stop_seconds if name == "game" else 8
                self._stop_pid(pid, grace=grace, label=name)
            # 清理子进程记录（关掉 stdin 管道，避免句柄泄漏）
            proc = self._children.pop(name, None)
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass
            # 清理 PID 文件
            try:
                spec.pidfile.unlink()
            except OSError:
                pass
        self._cleanup_sockets(cfg)
        return CommandResult(tuple(), returncode=0, stdout="已停止")

    def _cleanup_sockets(self, cfg: C.ManagerConfig) -> None:
        wl_dir = C.PKGHOME / "xstack" / "run"
        for p in (Path(f"/tmp/.X11-unix/X{cfg.native.display_number}"),
                  Path(f"/tmp/.X{cfg.native.display_number}-lock"),
                  wl_dir / WAYLAND_SOCKET, wl_dir / f"{WAYLAND_SOCKET}.lock"):
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass

    def restart(self, *, timeout: int = 300) -> CommandResult:
        self.stop(timeout=timeout)
        time.sleep(2)
        return self.start(timeout=timeout)

    def down(self, *, timeout: int = 300) -> CommandResult:
        return self.stop(timeout=timeout)

    def up(self, *, pull: bool = True, timeout: int = 900) -> CommandResult:
        return self.start(timeout=timeout)

    def recreate(self, *, timeout: int = 900) -> CommandResult:
        return self.restart(timeout=timeout)

    # ── 日志 / 指标 ─────────────────────────────────────────────────────────
    def _rotate_console(self) -> None:
        """原地 tail-trim：不能 mv/rename（游戏进程持有该 fd）。"""
        if not self.console.is_file():
            return
        try:
            size = self.console.stat().st_size
        except OSError:
            return
        if size <= CONSOLE_ROTATE_BYTES:
            return
        tmp = self.console.with_suffix(".rotate.tmp")
        try:
            with open(self.console, "rb") as fh:
                fh.seek(max(0, size - CONSOLE_KEEP_BYTES))
                data = fh.read()
            nl = data.find(b"\n")
            if nl >= 0:
                data = data[nl + 1:]
            with open(tmp, "wb") as out:
                out.write(f"[logrotate] 控制台日志超过 {CONSOLE_ROTATE_BYTES} 字节，保留尾部\n".encode())
                out.write(data)
            with open(tmp, "rb") as src, open(self.console, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError:
            pass
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def console_log(self, *, tail_bytes: int = 200_000) -> str:
        if not self.console.is_file():
            return ""
        try:
            size = self.console.stat().st_size
            with open(self.console, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    fh.readline()
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def logs(self, *, tail: int = 300, since: str = "") -> str:
        parts: list[str] = []
        labels = {
            "console.log": "游戏/SMAPI 控制台",
            "x11vnc.log": "x11vnc",
            "websockify.log": "websockify",
            "openbox.log": "openbox",
        }
        for name, label in labels.items():
            path = (self.console if name == "console.log" else C.LOG_DIR / name)
            if not path.is_file():
                continue
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            parts.append(f"===== {label}（{path}） =====")
            parts.extend(lines[-max(20, tail // len(labels)):])
        return "\n".join(parts)

    def stats(self) -> dict[str, Any]:
        return self._cached("stats", self._stats_uncached)

    def _stats_uncached(self) -> dict[str, Any]:
        cfg = C.load_config()
        specs = self._specs(cfg)
        total = 0
        procs: list[dict[str, Any]] = []
        for spec in specs:
            pid = self.resolve_pid(spec.name, cfg)
            if not pid:
                continue
            rss = proc_rss_kb(pid)
            total += rss
            procs.append({"name": spec.name, "pid": pid, "rss_kb": rss})
        if not procs:
            return {"available": False, "error": "没有正在运行的原生进程"}
        # 原生模式没有 cgroup，这里给出汇总值并明确说明口径
        return {
            "available": True,
            "mode": MODE_NATIVE,
            "processes": procs,
            # 口径必须写清楚：这是本应用受管进程的 RSS 合计，不是整机占用
            # （原生模式没有 cgroup，无法给出"占整机百分比"这种口径的数字）
            "mem_usage": f"{total / 1024:.0f} MiB（{len(procs)} 个受管进程 RSS 合计）",
            "mem_percent": "",
            "cpu_percent": "",
            "cpu_note": "原生模式未做 cgroup 隔离，CPU 百分比请在「系统」页查看整机负载",
            "pids": str(len(procs)),
        }

    # ── 三层健康判据 ──────────────────────────────────────────────────────
    def readiness(self) -> dict[str, Any]:
        return self._cached("readiness", self._readiness_uncached)

    def _readiness_uncached(self) -> dict[str, Any]:
        cfg = C.load_config()
        self.reap_children()          # 顺带回收僵尸（失败启动会留下 <defunct>）
        st = self.status()
        # 「是否有存档」必须在**所有提前返回之前**算出来：
        # 否则未安装/已停止等分支拿不到这个字段（实测踩到测试直接 KeyError）。
        # 为什么重要：进程在跑、三层判据全绿，但游戏可能停在**标题页**没开服 ——
        # ALOS 是 `SaveLoaded` 之后才 setServerMode/enableServer，
        # **没有存档就永远不会开服**。
        try:
            saves = [p for p in C.SAVES_DIR.iterdir() if p.is_dir()] if C.SAVES_DIR.is_dir() else []
        except OSError:
            saves = []
        out: dict[str, Any] = {
            "mode": MODE_NATIVE,
            "runtime": st.to_dict(),
            "saves_count": len(saves),
            "needs_save": bool(st.running and not saves),
            "layers": {
                "L1_game_process": st.running,
                "L1_display_stack": False,
                "L2_ports_listening": False,
                "L3_smapi_loaded": False,
            },
            "ready": False,
            "phase": "未运行",
            "detail": "",
            "processes": {},
        }
        # 进程清单必须来自 _specs()：写死名字的话，切到核显后端后就查不到
        # weston/xwayland，显示栈判据永远是 False（实测踩到：游戏明明在跑，
        # 却因为 L1_display_stack=False 被判"启动失败"）。
        for spec in self._specs(cfg):
            out["processes"][spec.name] = bool(self.resolve_pid(spec.name, cfg))

        if not self.launcher.is_file() and not st.running:
            out["phase"] = "未安装"
            out["detail"] = ("尚未放入游戏本体。原生部署需要 Stardew Valley 游戏文件（含 SMAPI），"
                             "可在「系统」页上传或放入数据目录。")
            out.update(self._state_model(out, cfg))
            return out
        # 显示栈判据要按后端取对应进程：核显模式是 weston+xwayland，软件模式是 xvfb。
        # 若写死 xvfb，切到核显后会永远显示"显示栈未就绪"。
        if self.effective_backend(cfg) == "weston":
            out["layers"]["L1_display_stack"] = bool(
                out["processes"].get("weston") and out["processes"].get("xwayland"))
        else:
            out["layers"]["L1_display_stack"] = bool(out["processes"].get("xvfb"))
        if not st.running:
            out["phase"] = "已停止"
            out["detail"] = "游戏进程未运行"
            out.update(self._state_model(out, cfg))
            return out
        if not out["layers"]["L1_display_stack"]:
            label = "Weston + Xwayland（核显）" if self.effective_backend(cfg) == "weston" \
                else "Xvfb（软件渲染）"
            out["phase"] = "启动失败"
            out["detail"] = f"显示栈（{label}）未运行，游戏无法创建窗口"
            out.update(self._state_model(out, cfg))
            return out

        vnc_ok = port_open(int(cfg.native.vnc_port))
        web_ok = port_open(int(cfg.native.webvnc_port))
        out["layers"]["L2_ports_listening"] = vnc_ok
        out["port_detail"] = {"vnc": vnc_ok, "webvnc": web_ok}

        console = self.console_log(tail_bytes=400_000)
        # ⚠️ 只看**本轮启动之后**的日志：console.log 是追加写入的，
        # 里面可能留着上一次运行（甚至上一次崩溃）的文本。
        # 实测踩到：上一次崩溃留下的 `Unhandled exception` 让本轮
        # L3_smapi_loaded 永远是 False —— 明明 SMAPI 已经正常加载。
        current = console
        markers = list(re.finditer(r"SMAPI\s+\d+\.\d+[^\n]*with Stardew Valley", console))
        if markers:
            current = console[markers[-1].start():]
        loaded = bool(re.search(r"SMAPI\s+\d+\.\d+", current)) and \
            bool(re.search(r"Mods loaded|mods loaded|Loaded \d+ mods|Mods loaded and ready", current))
        fatal = bool(re.search(r"isn't compatible with Stardew Valley|Unhandled exception|"
                              r"CRITICAL|Fatal error", current))
        out["layers"]["L3_smapi_loaded"] = loaded and not fatal
        if fatal and not loaded:
            out["phase"] = "启动失败"
            hint = ""
            if "compatible with Stardew Valley" in console:
                hint = "（显卡/显示不可用：请确认显示栈已启动，或开启软件渲染回退）"
            out["detail"] = "SMAPI 控制台记录了致命异常，请查看控制台日志" + hint
        elif loaded and out["needs_save"]:
            # 进程在跑、SMAPI 也加载了，但**没有任何存档** → 不会自动开服。
            # ALOS 是 SaveLoaded 之后才 setServerMode/enableServer，
            # 只显示"运行中"会让用户以为一切正常（实测踩到）。
            out["phase"] = "运行中（未开服）"
            out["detail"] = (
                "服务已启动（游戏进程在运行、MOD 已加载），但**还没有开服**："
                "没有任何存档 —— ALOS 只在「存档已加载」之后才会开启主机。"
                "请打开 WebVNC 在游戏里**手动新建一个存档**（或到「存档」页导入），"
                "然后**重启服务端**，管理器会自动载入最新存档并开服。")
        elif loaded:
            out["phase"] = "运行中"
            out["detail"] = "SMAPI 已加载完成"
        else:
            out["phase"] = "启动中"
            out["detail"] = "游戏进程已启动，等待 SMAPI 加载完成"
        out["ready"] = bool(out["layers"]["L1_game_process"]
                            and out["layers"]["L3_smapi_loaded"]
                            and not out["needs_save"])
        out["console_tail"] = console[-4000:] if console else ""
        out.update(self._state_model(out, cfg))
        return out

    def _state_model(self, out: dict[str, Any], cfg: C.ManagerConfig) -> dict[str, Any]:
        """把状态拆成**两个正交维度**（需求 §5）。

        以前只有一个 `phase`，会把"服务在跑"与"农场已开服"混为一谈 ——
        用户看到"运行中"会以为可以进游戏了，其实游戏还停在标题页。

        * 服务运行状态：未启动 / 启动中 / 运行中 / 停止中 / 已停止 / 启动失败 / 状态未知
        * 农场开服状态：尚未创建存档 / 尚未载入农场 / 农场已载入 / 开服状态待确认
        """
        layers = out.get("layers", {})
        phase = out.get("phase", "")
        # ⚠️ 必须从 `runtime` 里取 exists：readiness 的顶层**没有** `exists` 键
        # （只有 `runtime` 状态字典）。早期这里写 out.get("exists") 恒为 None，
        # 于是"进程在跑"也会被判成「未启动」（真机实测：6 个进程全在、phase 是
        # 运行中（未开服），界面却显示"未启动"）。
        exists = bool((out.get("runtime") or {}).get("exists")) or bool(out.get("exists"))
        if not exists:
            service = "未启动"
        elif phase == "启动失败":
            service = "启动失败"
        elif not layers.get("L1_game_process"):
            service = "已停止"
        elif not (layers.get("L1_display_stack") and layers.get("L3_smapi_loaded")):
            service = "启动中"
        elif self._stopping:
            service = "停止中"
        elif out.get("needs_save") or layers.get("L3_smapi_loaded"):
            service = "运行中"
        else:
            service = "状态未知"

        hosting = False
        try:
            from mgr.runtime_base import udp_port_open
            hosting = bool(udp_port_open(int(cfg.native.game_port)))
        except Exception:  # noqa: BLE001
            hosting = False

        if not out.get("saves_count"):
            farm = "尚未创建存档"
        elif hosting:
            farm = "农场已载入"
        elif getattr(self, "_host_pending", False):
            farm = "开服状态待确认"
        else:
            farm = "尚未载入农场"

        return {
            "service": {
                "state": service,
                "running": bool(layers.get("L1_game_process")),
                "hint": out.get("detail", "") if service != "运行中" else "",
            },
            "farm": {
                "state": farm,
                "hosting": hosting,
                "saves_count": int(out.get("saves_count") or 0),
                "hint": (
                    "没有任何存档：ALOS 只在存档加载后才会开启主机。"
                    "请手动新建存档（或导入）后重启服务端。"
                    if farm == "尚未创建存档" else
                    "已有存档但尚未载入：重启服务端会自动载入最新存档并开服。"
                    if farm == "尚未载入农场" else
                    "已发送载档指令，正在等待游戏端口就绪。"
                    if farm == "开服状态待确认" else
                    "游戏端口已监听，远程玩家可以加入。"
                ),
            },
        }

    def version_info(self) -> dict[str, Any]:
        cfg = C.load_config()
        return {
            "game_dir": str(self.game_root),
            "game_root": str(self.game_root),
            "launcher": str(self.launcher),
            "display": f":{cfg.native.display_number}",
        }

    def has_image_support(self) -> bool:
        return False
