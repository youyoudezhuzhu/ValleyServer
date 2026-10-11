"""运行时公共类型与工具。

管理器直接在本机运行 游戏客户端 + SMAPI + 自动化 MOD + 虚拟显示栈。
`Runtime` 这层接口把"怎么把游戏跑起来"与上层隔开，便于将来替换实现。

模块内容：
  * `ContainerStatus` —— 统一的运行状态结构（字段名沿用早期命名，避免上层大改）
  * `CommandResult`  —— 外部命令/操作结果
  * `Runtime`        —— 后端接口（当前唯一实现：`NativeRuntime`）
  * 进程/端口工具    —— PID 存活、cmdline 匹配、端口占用归属、内存读取
"""

from __future__ import annotations

import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as C

MODE_NATIVE = "native"


@dataclass
class ContainerStatus:
    """统一的运行状态结构。

    字段名沿用早期（容器时代）的命名，避免上层与前端大改；`mode` 恒为 `native`。
    """

    exists: bool
    running: bool
    status: str = "absent"
    health: str = "none"
    image: str = ""
    image_digest: str = ""
    started_at: str = ""
    restart_count: int = 0
    exit_code: int | None = None
    error: str = ""
    mode: str = MODE_NATIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists, "running": self.running, "status": self.status,
            "health": self.health, "image": self.image,
            "image_digest": self.image_digest, "started_at": self.started_at,
            "restart_count": self.restart_count, "exit_code": self.exit_code,
            "error": self.error, "mode": self.mode,
        }


@dataclass
class CommandResult:
    argv: tuple
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def tail(self) -> str:
        return ((self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")).strip()[-4000:]


class Runtime:
    """运行后端接口。当前唯一实现是 `NativeRuntime`。"""

    mode: str = MODE_NATIVE

    def render(self, cfg: "C.ManagerConfig | None" = None) -> str:
        """把当前配置渲染成可展示的启动描述（前端「配置」页与自检用）。"""
        raise NotImplementedError

    def validate(self, cfg: "C.ManagerConfig | None" = None) -> dict[str, Any]:
        """校验配置与依赖，返回 {ok, errors, warnings, guidance}。"""
        raise NotImplementedError

    def status(self) -> ContainerStatus:
        raise NotImplementedError

    def start(self, *, timeout: int = 300) -> CommandResult:
        raise NotImplementedError

    def stop(self, *, timeout: int = 180) -> CommandResult:
        raise NotImplementedError

    def restart(self, *, timeout: int = 300) -> CommandResult:
        raise NotImplementedError

    def down(self, *, timeout: int = 300) -> CommandResult:
        """stop 的别名（语义兼容）。"""
        return self.stop(timeout=timeout)

    def up(self, *, timeout: int = 900) -> CommandResult:
        """start 的别名（语义兼容）。"""
        return self.start(timeout=timeout)

    def recreate(self, *, timeout: int = 900) -> CommandResult:
        return self.restart(timeout=timeout)

    def logs(self, *, tail: int = 300, since: str = "") -> str:
        raise NotImplementedError

    def console_log(self, *, tail_bytes: int = 200_000) -> str:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        raise NotImplementedError

    def detect(self) -> dict[str, Any]:
        raise NotImplementedError

    def readiness(self) -> dict[str, Any]:
        """三层健康判据：显示栈/进程 → 端口 → SMAPI 日志。"""
        raise NotImplementedError

    def prepare(self, cfg: "C.ManagerConfig | None" = None) -> None:
        """生成配置/目录/链接，确保 start 能成功。"""

    def write_selected_save(self, dir_name: str) -> Path:
        raise NotImplementedError

    def version_info(self) -> dict[str, Any]:
        """版本信息（原生模式没有镜像摘要，返回游戏目录等）。"""
        return {}


def make_runtime() -> Runtime:
    """创建运行后端（当前只有原生）。"""
    from .native_runtime import NativeRuntime
    return NativeRuntime()


# ─────────────────────────────────────────────────────────────────────────────
# 进程 / 端口工具
# ─────────────────────────────────────────────────────────────────────────────

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _listening_pids(port: int) -> list[int]:
    """从 /proc/net/tcp + /proc/*/fd 反查监听指定端口的 PID（不依赖 ss/lsof）。"""
    hex_port = f"{port:04X}"
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            if parts[3] != "0A":           # 0A = LISTEN
                continue
            if parts[1].split(":")[-1].upper() != hex_port:
                continue
            inodes.add(parts[9])
    if not inodes:
        return []
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    out.append(int(entry.name))
                    break
        except OSError:
            continue
    return sorted(set(out))


def port_conflict_owner(port: int) -> int:
    """端口占用归属：0 = 无占用，-1 = 被**其它应用**占用，>0 = 本应用的 PID。

    只用于原生模式：避免误杀 NAS 上其它应用的进程。
    """
    own_roots = (str(C.MODS_DIR), str(C.SAVES_DIR), str(C.PKGHOME), str(C.APPDEST),
                 str(C.GAME_FILES_DIR))
    listening = _listening_pids(port)
    if not listening:
        return 0
    for pid in listening:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if any(root and root in cmdline for root in own_roots):
            return pid
    return -1


def list_pids_matching(pattern: str, *, exclude: tuple[int, ...] = ()) -> list[int]:
    """按 cmdline 正则匹配进程，排除指定 PID 与自身。"""
    rx = re.compile(pattern)
    me = os.getpid()
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me or pid in exclude:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
        except OSError:
            continue
        if cmdline and rx.search(cmdline):
            out.append(pid)
    return sorted(set(out))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # 排除僵尸
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def proc_rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def proc_started_at(pid: int) -> str:
    try:
        st = os.stat(f"/proc/{pid}")
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_ctime))
    except OSError:
        return ""
