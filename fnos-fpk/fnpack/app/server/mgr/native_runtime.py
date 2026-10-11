"""原生部署后端：直接在本机运行 游戏 + SMAPI + 虚拟显示栈，**不依赖 Docker**。

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
from . import xstack
from .runtime_base import (
    CommandResult, ContainerStatus, MODE_NATIVE, Runtime, list_pids_matching,
    pid_alive, port_conflict_owner, port_open, proc_rss_kb, proc_started_at,
)
from .util import ValidationError, atomic_write_json, atomic_write_text, run

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


class NativeRuntime(Runtime):
    mode = MODE_NATIVE

    def __init__(self) -> None:
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
        return [
            ProcSpec("xvfb", self.rundir / "xvfb.pid",
                     rf"Xvfb\s+{re.escape(disp)}", 0, True),
            ProcSpec("openbox", self.rundir / "openbox.pid", r"openbox", 0, False),
            ProcSpec("x11vnc", self.rundir / "x11vnc.pid", r"x11vnc", int(n.vnc_port), False),
            ProcSpec("websockify", self.rundir / "websockify.pid", r"websockify",
                     int(n.webvnc_port), False),
            ProcSpec("game", self.rundir / "game.pid", r"StardewModdingAPI", 0, True),
        ]

    # ── 配置渲染 ────────────────────────────────────────────────────────────
    def render(self, cfg: C.ManagerConfig | None = None) -> str:
        cfg = cfg or C.load_config()
        n = cfg.native
        lines = [
            "# 原生部署启动描述（由 ValleyServer 管理器生成，仅用于展示/自检）",
            "# 执行后端：原生部署（直接在本机运行，不使用 Docker）",
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

    def _env(self, cfg: C.ManagerConfig | None = None) -> dict[str, str]:
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
        if cfg.native.software_gl:
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
        return [
            tool, "-display", f":{n.display_number}", "-forever", "-shared",
            "-passwd", n.vnc_password, "-rfbport", str(int(n.vnc_port)),
            "-noxdamage", "-bg", "-o", str(C.LOG_DIR / "x11vnc.log"),
        ]

    def _websockify_argv(self, cfg: C.ManagerConfig) -> list[str]:
        n = cfg.native
        tool = xstack.find_tool("websockify") or "websockify"
        webroot = xstack.novnc_webroot()
        argv = [tool]
        if webroot:
            argv.append(f"--web={webroot}")
        argv += [str(int(n.webvnc_port)), f"localhost:{int(n.vnc_port)}"]
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
        if not det["xstack"]["ok"]:
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

    def status(self) -> ContainerStatus:
        cfg = C.load_config()
        specs = self._specs(cfg)
        game_pid = self.resolve_pid("game", cfg)
        launcher_present = self.launcher.is_file()
        display_pid = self.resolve_pid("xvfb", cfg)

        if not launcher_present and not game_pid:
            return ContainerStatus(exists=False, running=False, status="absent",
                                   mode=MODE_NATIVE,
                                   error="尚未放入游戏本体" if not launcher_present else "")
        running = bool(game_pid)
        st = ContainerStatus(
            exists=True, running=running,
            status="running" if running else "stopped",
            health="none", mode=MODE_NATIVE,
            image=f"native:{self.game_root}",
            started_at=proc_started_at(game_pid) if game_pid else "",
        )
        st.error = "" if display_pid or not running else "显示栈未运行"
        return st

    def _spawn(self, argv: list[str], *, logfile: Path, name: str,
               cfg: C.ManagerConfig) -> int:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = open(logfile, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(self.game_root), env=self._env(cfg),
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            fh.close()
        (self.rundir / f"{name}.pid").write_text(f"{proc.pid}\n")
        return proc.pid

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

    def start(self, *, timeout: int = 300) -> CommandResult:
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
        self._rotate_console()
        specs = {s.name: s for s in self._specs(cfg)}

        # 1) Xvfb
        if not self._pid_of(specs["xvfb"]):
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
                return CommandResult(tuple(argv), returncode=1,
                                     stderr="Xvfb 启动后未创建 X socket，请查看 xvfb.log")

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
            proc = subprocess.Popen(
                argv, cwd=str(self.game_root), env=env,
                stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
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
        (self.rundir / "game.pid").write_text(f"{proc.pid}\n")
        return CommandResult(tuple(argv), returncode=0,
                             stdout=f"已启动原生游戏进程 pid={proc.pid}")

    def stop(self, *, timeout: int = 180) -> CommandResult:
        cfg = C.load_config()
        specs = {s.name: s for s in self._specs(cfg)}
        # 先停游戏（给它时间收尾存档），再拆显示栈
        for name in ("game", "websockify", "x11vnc", "openbox", "xvfb"):
            spec = specs[name]
            pid = self.resolve_pid(name, cfg)
            if pid:
                grace = cfg.native.graceful_stop_seconds if name == "game" else 8
                self._stop_pid(pid, grace=grace, label=name)
            # 清理 PID 文件
            try:
                spec.pidfile.unlink()
            except OSError:
                pass
        self._cleanup_sockets(cfg)
        return CommandResult(tuple(), returncode=0, stdout="已停止")

    def _cleanup_sockets(self, cfg: C.ManagerConfig) -> None:
        for p in (Path(f"/tmp/.X11-unix/X{cfg.native.display_number}"),
                  Path(f"/tmp/.X{cfg.native.display_number}-lock")):
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

    # ── 三层健康判据（与 Docker 模式同构） ────────────────────────────────
    def readiness(self) -> dict[str, Any]:
        cfg = C.load_config()
        st = self.status()
        out: dict[str, Any] = {
            "mode": MODE_NATIVE,
            "container": st.to_dict(),
            "layers": {
                "L1_container_running": st.running,
                "L1_display_stack": False,
                "L2_ports_listening": False,
                "L3_smapi_loaded": False,
            },
            "ready": False,
            "phase": "未运行",
            "detail": "",
            "processes": {},
        }
        for name in ("xvfb", "openbox", "x11vnc", "websockify", "game"):
            out["processes"][name] = bool(self.resolve_pid(name, cfg))

        if not self.launcher.is_file() and not st.running:
            out["phase"] = "未安装"
            out["detail"] = ("尚未放入游戏本体。原生部署需要 Stardew Valley 游戏文件（含 SMAPI），"
                             "可在「系统」页上传或放入数据目录。")
            return out
        out["layers"]["L1_display_stack"] = out["processes"]["xvfb"]
        if not st.running:
            out["phase"] = "已停止"
            out["detail"] = "游戏进程未运行"
            return out
        if not out["processes"]["xvfb"]:
            out["phase"] = "启动失败"
            out["detail"] = "显示栈（Xvfb）未运行，游戏无法创建窗口"
            return out

        vnc_ok = port_open(int(cfg.native.vnc_port))
        web_ok = port_open(int(cfg.native.webvnc_port))
        out["layers"]["L2_ports_listening"] = vnc_ok
        out["port_detail"] = {"vnc": vnc_ok, "webvnc": web_ok}

        console = self.console_log(tail_bytes=200_000)
        loaded = bool(re.search(r"SMAPI\s+\d+\.\d+", console)) and \
            bool(re.search(r"Mods loaded|mods loaded|Loaded \d+ mods|Mods loaded and ready", console))
        fatal = bool(re.search(r"isn't compatible with Stardew Valley|Unhandled exception|"
                              r"CRITICAL|Fatal error", console))
        out["layers"]["L3_smapi_loaded"] = loaded and not fatal
        if fatal and not loaded:
            out["phase"] = "启动失败"
            hint = ""
            if "compatible with Stardew Valley" in console:
                hint = "（显卡/显示不可用：请确认显示栈已启动，或开启软件渲染回退）"
            out["detail"] = "SMAPI 控制台记录了致命异常，请查看控制台日志" + hint
        elif loaded:
            out["phase"] = "运行中"
            out["detail"] = "SMAPI 已加载完成"
        else:
            out["phase"] = "启动中"
            out["detail"] = "游戏进程已启动，等待 SMAPI 加载完成"
        out["ready"] = bool(out["layers"]["L1_container_running"]
                            and out["layers"]["L3_smapi_loaded"])
        out["console_tail"] = console[-4000:] if console else ""
        return out

    def version_info(self) -> dict[str, Any]:
        cfg = C.load_config()
        return {
            "image_ref": f"native:{self.game_root}",
            "image_digest": "",
            "game_root": str(self.game_root),
            "launcher": str(self.launcher),
            "display": f":{cfg.native.display_number}",
        }

    def has_image_support(self) -> bool:
        return False
