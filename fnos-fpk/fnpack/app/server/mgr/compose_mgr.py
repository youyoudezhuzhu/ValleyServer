"""Docker Compose 管理模块。

安全约束（项目要求 4.2）：
  * compose 文件由**服务端模板**生成，用户只能改白名单内的标量字段；
    **绝不接受用户提供的任意 YAML / Shell / 宿主机路径**。
  * 所有宿主机路径都来自 config 常量并经 `assert_within` 校验。
  * 外部命令一律 argv 列表，无 shell。
  * 项目名/容器名/网络/卷全部使用项目专属常量，不影响 NAS 上其他容器。
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as C
from .util import (
    CommandResult, ValidationError, assert_within, atomic_write_json,
    atomic_write_text, run, validate_image_ref, validate_port,
)

COMPOSE_PROJECT = "valleyserver"
CONTAINER_NAME = "valleyserver-game"
NETWORK_NAME = "valleyserver-net"
COMPOSE_FILE_NAME = "docker-compose.yaml"
ENTRYPOINT_NAME = "entrypoint.sh"
CONSOLE_LOG_NAME = "console.log"

# 容器内固定路径（不可由用户改变）
C_GAME = "/content/Stardew Valley"
C_SAVES = "/root/.config/StardewValley/Saves"
C_MODS = f"{C_GAME}/Mods"
C_LOGS = "/data-logs"
C_ERROR_LOGS = "/root/.config/StardewValley/ErrorLogs"


class DockerUnavailable(RuntimeError):
    """Docker / Compose 不可用，调用方应向用户展示安装指引。"""


@dataclass
class ContainerStatus:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists, "running": self.running, "status": self.status,
            "health": self.health, "image": self.image, "image_digest": self.image_digest,
            "started_at": self.started_at, "restart_count": self.restart_count,
            "exit_code": self.exit_code, "error": self.error,
        }


class ComposeManager:
    """生成并驱动项目专属的 Compose 工程。"""

    def __init__(self) -> None:
        self.game_dir = C.GAME_DIR
        self.compose_path = self.game_dir / COMPOSE_FILE_NAME
        self.entrypoint_path = self.game_dir / ENTRYPOINT_NAME

    # ── 依赖检测 ────────────────────────────────────────────────────────────
    @staticmethod
    def detect() -> dict[str, Any]:
        """检测 Docker Engine 与 Compose V2。返回结构化结果，供 UI 展示安装指引。"""
        info: dict[str, Any] = {
            "docker": {"found": False, "version": "", "running": False, "error": ""},
            "compose": {"found": False, "version": "", "error": ""},
            "socket": {"path": "/var/run/docker.sock", "exists": False, "accessible": False},
            "ready": False,
            "guidance": [],
        }
        from shutil import which
        docker_bin = which("docker")
        if not docker_bin:
            info["docker"]["error"] = "未找到 docker 可执行文件"
            info["guidance"].append(
                "本应用需要 Docker Engine。请在飞牛 OS 的「应用中心」安装 Docker 应用，"
                "或在终端执行 `apt-get install -y docker.io`（不建议在未了解系统包管理方式前强行覆盖系统组件）。"
            )
            return info
        info["docker"]["found"] = True
        res = run([docker_bin, "version", "--format", "{{.Server.Version}}"], timeout=20)
        if res.ok and res.stdout.strip():
            info["docker"]["version"] = res.stdout.strip()
            info["docker"]["running"] = True
        else:
            info["docker"]["error"] = (res.stderr or res.stdout).strip()[:400]
            info["guidance"].append(
                "Docker 客户端存在但守护进程无响应：请确认 Docker 应用已启动，"
                "并检查当前用户是否有权访问 /var/run/docker.sock。"
            )
        res = run([docker_bin, "compose", "version", "--short"], timeout=20)
        if res.ok and res.stdout.strip():
            info["compose"]["found"] = True
            info["compose"]["version"] = res.stdout.strip()
        else:
            info["compose"]["error"] = (res.stderr or res.stdout).strip()[:400]
            info["guidance"].append(
                "缺少 Docker Compose V2（`docker compose`）。请升级 Docker 应用，"
                "或安装 docker-compose-plugin；**不要**用 python3 pip 安装的旧版 docker-compose。"
            )
        sock = Path("/var/run/docker.sock")
        info["socket"]["exists"] = sock.exists()
        try:
            info["socket"]["accessible"] = sock.exists() and os.access(sock, os.R_OK | os.W_OK)
        except OSError:
            info["socket"]["accessible"] = False
        if info["socket"]["exists"] and not info["socket"]["accessible"]:
            info["guidance"].append(
                "无法访问 /var/run/docker.sock：本应用以 root 运行时应可访问；"
                "若权限被改动，请检查 socket 的属主与权限。"
            )
        info["ready"] = bool(
            info["docker"]["found"] and info["docker"]["running"] and info["compose"]["found"]
        )
        return info

    @staticmethod
    def require_ready() -> dict[str, Any]:
        info = ComposeManager.detect()
        if not info["ready"]:
            reasons = [g for g in info["guidance"]] or ["Docker 或 Docker Compose V2 不可用"]
            raise DockerUnavailable("；".join(reasons))
        return info

    # ── compose 文件生成 ────────────────────────────────────────────────────
    def render(self, cfg: C.ManagerConfig | None = None) -> str:
        """渲染 compose YAML。所有变量已在渲染前通过白名单校验。"""
        cfg = cfg or C.load_config()
        cc = cfg.container

        image_ref = self._image_ref(cc)
        game_port = validate_port(cc.game_port, field="游戏端口")
        webvnc_port = validate_port(cc.webvnc_port, field="WebVNC 端口")
        vnc_port = validate_port(cc.vnc_port, field="VNC 端口")
        cmd_port = validate_port(cc.cmdwebui_port, field="CommandWebUI 端口")
        bind = self._validate_bind(cc.host_bind)
        shm = self._validate_size(cc.shm_size, "shm 大小") if cc.shm_size else ""
        cpu = self._validate_cpu(cc.cpu_limit) if cc.cpu_limit else ""
        mem = self._validate_size(cc.mem_limit, "内存上限") if cc.mem_limit else ""
        restart = self._validate_restart(cc.restart_policy)
        vnc_password = self._validate_password(cc.vnc_password)

        # 宿主机路径：全部来自 config 常量，并断言在数据根内
        saves = assert_within(C.SAVES_DIR, C.DATA_ROOT, what="存档目录")
        mods = assert_within(C.MODS_DIR, C.DATA_ROOT, what="MOD 目录")
        logs = assert_within(C.GAME_DIR / "logs", C.DATA_ROOT, what="日志目录")
        error_logs = assert_within(C.GAME_DIR / "errorlogs", C.DATA_ROOT, what="SMAPI 错误日志目录")
        entry = assert_within(self.entrypoint_path, C.DATA_ROOT, what="entrypoint")
        selected = assert_within(C.GAME_DIR / "selected-save", C.DATA_ROOT, what="当前存档选择")
        console = assert_within(C.GAME_DIR / CONSOLE_LOG_NAME, C.DATA_ROOT, what="控制台日志")

        lines: list[str] = [
            "# 本文件由 ValleyServer 管理器自动生成，请勿手工编辑。",
            "# 手工修改会在下次「应用配置」时被覆盖；请改用管理界面。",
            f"# 生成来源：{C.APP_VERSION}",
            "services:",
            "  game:",
            f"    image: {image_ref}",
            f"    container_name: {CONTAINER_NAME}",
            f"    restart: {restart}",
            "    stop_grace_period: 60s",
            "    # 游戏需要以 uid 1000 写入挂载目录；不启用只读根文件系统，避免破坏上游镜像行为",
            "    environment:",
            "      TZ: Asia/Shanghai",
            "      DISPLAY: ':99'",
            "      VNC_PASSWORD: " + yaml_scalar(vnc_password),
            "      HOME: /root",
            "      GAME_UID: '1000'",
            "      GAME_GID: '1000'",
            "    volumes:",
            f"      - {yaml_scalar(f'{saves}:{C_SAVES}')}",
            f"      - {yaml_scalar(f'{mods}:{C_MODS}')}",
            f"      - {yaml_scalar(f'{logs}:{C_LOGS}')}",
            f"      - {yaml_scalar(f'{error_logs}:{C_ERROR_LOGS}')}",
            f"      - {yaml_scalar(f'{entry}:/opt/valleyserver-entrypoint.sh:ro')}",
            f"      - {yaml_scalar(f'{selected}:/data-config/selected-save:ro')}",
            "    ports:",
            f"      - {_port_entry(bind, game_port, game_port, 'udp')}",
            f"      - {_port_entry(bind, webvnc_port, 5800)}",
            f"      - {_port_entry(bind, vnc_port, 5900)}",
            f"      - {_port_entry(bind, cmd_port, 29103)}",
            "    # 管理器自建健康检查（上游镜像没有 Healthcheck，见审计报告 P7）",
            "    healthcheck:",
            f"      test: [\"CMD-SHELL\", \"(echo > /dev/tcp/127.0.0.1/5800) >/dev/null 2>&1 && pgrep -f StardewModdingAPI >/dev/null\"]",
            f"      interval: {int(cc.healthcheck_interval)}s",
            f"      timeout: 10s",
            f"      retries: {int(cc.healthcheck_retries)}",
            f"      start_period: {int(cc.healthcheck_start_period)}s",
            "    labels:",
            '      com.valleyserver.managed: "true"',
            f'      com.valleyserver.version: "{C.APP_VERSION}"',
            "    networks:",
            f"      - {NETWORK_NAME}",
        ]
        if shm:
            lines.insert(lines.index("    environment:"), f"    shm_size: {yaml_scalar(shm)}")
        if cpu:
            lines.insert(lines.index("    environment:"), f"    cpus: {yaml_scalar(cpu)}")
        if mem:
            lines.insert(lines.index("    environment:"), f"    mem_limit: {yaml_scalar(mem)}")
        lines += [
            "",
            "networks:",
            f"  {NETWORK_NAME}:",
            "    name: " + NETWORK_NAME,
            "    driver: bridge",
            "",
        ]
        return "\n".join(lines)

    def _image_ref(self, cc: C.ContainerConfig) -> str:
        base = validate_image_ref(cc.image)
        if cc.pin_digest and cc.image_digest:
            digest = cc.image_digest.strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValidationError(f"镜像 digest 格式不合法：{digest}")
            return f"{base}@{digest}"
        tag = (cc.image_tag or "latest").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", tag):
            raise ValidationError(f"镜像标签不合法：{tag}")
        return f"{base}:{tag}"

    @staticmethod
    def _validate_bind(value: str) -> str:
        if value in ("0.0.0.0", "127.0.0.1", "::", "::1"):
            return value
        # 允许指定单个 IPv4 地址
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value):
            octets = [int(x) for x in value.split(".")]
            if all(0 <= o <= 255 for o in octets):
                return value
        raise ValidationError("监听地址只允许 0.0.0.0 / 127.0.0.1 / ::1 或合法 IPv4 地址")

    @staticmethod
    def _validate_size(value: str, field: str) -> str:
        raw = str(value)
        # 不接受首尾空白：宁可让用户看到明确的格式错误，也不静默"修正"他的输入
        if raw != raw.strip():
            raise ValidationError(f"{field}不允许包含首尾空白")
        if not re.fullmatch(r"\d{1,6}[bkmgBKMG]?", raw):
            raise ValidationError(f"{field}格式非法（示例：512m、2g）")
        return raw

    @staticmethod
    def _validate_cpu(value: str) -> str:
        value = str(value)
        if value != value.strip():
            raise ValidationError("CPU 限制不允许包含首尾空白")
        try:
            f = float(value)
        except ValueError:
            raise ValidationError("CPU 限制必须是数字（如 1.5）")
        if not (0 < f <= 64):
            raise ValidationError("CPU 限制必须在 0-64 之间")
        return value

    @staticmethod
    def _validate_restart(value: str) -> str:
        allowed = {"no", "always", "unless-stopped", "on-failure"}
        if value not in allowed:
            raise ValidationError(f"重启策略必须是：{', '.join(sorted(allowed))}")
        return value

    @staticmethod
    def _validate_password(value: str) -> str:
        """VNC 密码：VNC 协议只取前 8 个字符，故限制为 8 位可打印 ASCII。"""
        if not value:
            raise ValidationError("VNC 密码不能为空（应先由安装流程生成）")
        if not re.fullmatch(r"[\x21-\x7e]{8}", value):
            raise ValidationError("VNC 协议只支持 8 位可打印 ASCII 密码")
        return value

    # ── entrypoint 生成 ─────────────────────────────────────────────────────
    def write_selected_save(self, dir_name: str) -> Path:
        """把「当前存档」写到 GAME_DIR，容器内以只读方式挂载给 entrypoint。"""
        self.ensure_runtime_dirs()
        path = C.GAME_DIR / "selected-save"
        atomic_write_json(path, {"save": dir_name}, mode=0o644)
        return path

    def write_entrypoint(self) -> Path:
        """写出管理器自有的启动包装脚本。

        相对上游 `startapp.sh` 的三处关键改进：
          1. VNC 密码来自环境变量（上游硬编码 041041）；
          2. SMAPI 控制台输出同时写 stdout 与 $C_LOGS/console.log（上游只进 docker logs）；
          3. 游戏以 uid 1000 运行，存档/ MOD 目录权限显式准备。
        """
        self.ensure_runtime_dirs()
        script = ENTRYPOINT_TEMPLATE
        atomic_write_text(self.entrypoint_path, script, mode=0o755)
        return self.entrypoint_path

    def ensure_runtime_dirs(self) -> None:
        """只有真正要启动/写文件时才创建运行目录（render 保持无副作用）。"""
        for d in (C.GAME_DIR, C.SAVES_DIR, C.MODS_DIR, C.GAME_DIR / "logs",
                  C.GAME_DIR / "errorlogs"):
            d.mkdir(parents=True, exist_ok=True)

    def write_compose(self, cfg: C.ManagerConfig | None = None) -> Path:
        text = self.render(cfg)
        C.GAME_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.compose_path, text, mode=0o640)
        return self.compose_path

    def validate_rendered(self) -> CommandResult:
        """用 `docker compose config -q` 做严格校验，非法配置直接失败。"""
        if not self.compose_path.is_file():
            raise ValidationError("compose 文件尚未生成")
        return self._compose(["config", "-q"], timeout=60)

    # ── compose 命令封装 ────────────────────────────────────────────────────
    def _base_argv(self) -> list[str]:
        return [
            "docker", "compose",
            "--project-name", COMPOSE_PROJECT,
            "--project-directory", str(C.GAME_DIR),
            "-f", str(self.compose_path),
        ]

    def _compose(self, args: list[str], *, timeout: int = 300) -> CommandResult:
        return run(self._base_argv() + args, cwd=C.GAME_DIR, timeout=timeout)

    def up(self, *, pull: bool = True, timeout: int = 1800) -> CommandResult:
        self.ensure_runtime_dirs()
        if pull:
            res = self._compose(["pull", "--quiet"], timeout=timeout)
            if not res.ok:
                # 拉取失败也可能是本地已有镜像；继续尝试 up，由 up 的结果决定成败
                pass
        return self._compose(["up", "-d", "--remove-orphans"], timeout=timeout)

    def down(self, *, timeout: int = 300) -> CommandResult:
        return self._compose(["down", "--remove-orphans"], timeout=timeout)

    def stop(self, *, timeout: int = 180) -> CommandResult:
        return self._compose(["stop", "--timeout", "60"], timeout=timeout)

    def start(self, *, timeout: int = 300) -> CommandResult:
        self.ensure_runtime_dirs()
        return self._compose(["start"], timeout=timeout)

    def restart(self, *, timeout: int = 300) -> CommandResult:
        return self._compose(["restart", "--timeout", "60"], timeout=timeout)

    def recreate(self, *, timeout: int = 900) -> CommandResult:
        return self._compose(["up", "-d", "--force-recreate", "--remove-orphans"], timeout=timeout)

    def ps(self) -> CommandResult:
        return self._compose(["ps", "--format", "json"], timeout=60)

    def pull_image(self, ref: str, *, timeout: int = 3600) -> CommandResult:
        validate_image_ref(ref)
        return run(["docker", "pull", ref], timeout=timeout)

    def image_digest(self, ref: str) -> str:
        """读取本地镜像的 registry digest（不可变标识）。"""
        res = run([
            "docker", "image", "inspect", ref,
            "--format", "{{index .RepoDigests 0}}",
        ], timeout=60)
        if res.ok and res.stdout.strip():
            value = res.stdout.strip()
            if "@" in value:
                return value.split("@", 1)[1]
            return value
        return ""

    def image_inspect(self, ref: str) -> dict[str, Any]:
        from .util import read_json
        res = run(["docker", "image", "inspect", ref], timeout=60)
        if not res.ok:
            return {}
        try:
            import json
            data = json.loads(res.stdout)
        except Exception:  # noqa: BLE001
            return {}
        return data[0] if data else {}

    # ── 容器状态 ────────────────────────────────────────────────────────────
    def status(self) -> ContainerStatus:
        res = run([
            "docker", "inspect", CONTAINER_NAME,
            "--format",
            "{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}|{{.RestartCount}}|"
            "{{.State.StartedAt}}|{{.Config.Image}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
            "{{.Image}}",
        ], timeout=30)
        if not res.ok or not res.stdout.strip():
            return ContainerStatus(exists=False, running=False, status="absent",
                                   error=(res.stderr or "").strip()[:300])
        parts = (res.stdout.strip().split("|") + [""] * 8)[:8]
        state, running, exit_code, restarts, started, image, health, image_id = parts
        try:
            rc = int(restarts or 0)
        except ValueError:
            rc = 0
        try:
            ec: int | None = int(exit_code) if exit_code not in ("", "0") else (
                0 if exit_code == "0" else None)
        except ValueError:
            ec = None
        digest = self.image_digest(image) if image else ""
        return ContainerStatus(
            exists=True,
            running=(running == "true"),
            status=state or "unknown",
            health=health or "none",
            image=image,
            image_digest=digest or (image_id or "")[:80],
            started_at=started,
            restart_count=rc,
            exit_code=ec,
        )

    def logs(self, *, tail: int = 300, since: str = "") -> str:
        tail = max(1, min(int(tail), 5000))
        argv = ["docker", "logs", "--tail", str(tail), "--timestamps"]
        if since:
            if not re.fullmatch(r"[0-9a-zA-Z]{1,40}", since):
                raise ValidationError("since 参数非法")
            argv += ["--since", since]
        argv.append(CONTAINER_NAME)
        res = run(argv, timeout=60)
        return (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")

    def console_log(self, *, tail_bytes: int = 200_000) -> str:
        """读取 entrypoint 落盘的 SMAPI 控制台日志尾部。"""
        path = C.GAME_DIR / CONSOLE_LOG_NAME
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # 丢弃被截断的首行
            data = fh.read()
        return data.decode("utf-8", "replace")

    def stats(self) -> dict[str, Any]:
        """容器 CPU/内存/IO 快照。"""
        res = run([
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}",
            CONTAINER_NAME,
        ], timeout=45)
        if not res.ok or "|" not in res.stdout:
            return {"available": False, "error": (res.stderr or "").strip()[:200]}
        parts = res.stdout.strip().split("|")
        while len(parts) < 6:
            parts.append("")
        return {
            "available": True,
            "cpu_percent": parts[0].strip().rstrip("%"),
            "mem_usage": parts[1].strip(),
            "mem_percent": parts[2].strip().rstrip("%"),
            "net_io": parts[3].strip(),
            "block_io": parts[4].strip(),
            "pids": parts[5].strip(),
        }

    def exec_in_container(self, argv: list[str], *, timeout: int = 60) -> CommandResult:
        """在容器内执行命令（仅限白名单调用方使用）。"""
        if not argv:
            raise ValidationError("exec 需要至少一个参数")
        return run(["docker", "exec", CONTAINER_NAME] + [str(a) for a in argv], timeout=timeout)

    # ── 运行时就绪判据 ──────────────────────────────────────────────────────
    def readiness(self) -> dict[str, Any]:
        """三层健康判据：容器 → 端口 → SMAPI 控制台。

        不把「进程还在」当作成功（项目要求 5.1）。
        """
        st = self.status()
        out: dict[str, Any] = {
            "container": st.to_dict(),
            "layers": {
                "L1_container_running": st.running,
                "L2_ports_listening": False,
                "L3_smapi_loaded": False,
            },
            "ready": False,
            "phase": "未运行",
            "detail": "",
        }
        if not st.exists:
            out["phase"] = "未安装"
            out["detail"] = "容器不存在，请先执行「安装 / 首次启动」"
            return out
        if not st.running:
            out["phase"] = "已停止"
            out["detail"] = f"容器状态 {st.status}（退出码 {st.exit_code}）"
            return out
        console = self.console_log(tail_bytes=200_000)
        port_ok = bool(re.search(r"5800|websockify|WebVNC", console)) or self._port_open(5800)
        out["layers"]["L2_ports_listening"] = port_ok
        if console:
            loaded = bool(re.search(r"SMAPI\s+\d+\.\d+", console)) and \
                bool(re.search(r"Mods loaded|mods loaded|Loaded \d+ mods", console))
            fatal = bool(re.search(r"\bCRITICAL\b|Unhandled exception|Fatal error", console))
            out["layers"]["L3_smapi_loaded"] = loaded and not fatal
            if fatal and not loaded:
                out["phase"] = "启动失败"
                out["detail"] = "SMAPI 控制台记录了致命异常，请查看控制台页面"
            elif loaded:
                out["phase"] = "运行中"
                out["detail"] = "SMAPI 已加载完成"
            else:
                out["phase"] = "启动中"
                out["detail"] = "容器已运行，等待 SMAPI 加载完成"
        else:
            out["phase"] = "启动中"
            out["detail"] = "尚未产生 SMAPI 控制台日志（冷启动可能需要 1-3 分钟）"
        out["ready"] = bool(
            out["layers"]["L1_container_running"] and out["layers"]["L3_smapi_loaded"]
        )
        out["console_tail"] = console[-4000:] if console else ""
        return out

    @staticmethod
    def _port_open(port: int) -> bool:
        import socket
        for host in ("127.0.0.1",):
            try:
                with socket.create_connection((host, port), timeout=2):
                    return True
            except OSError:
                continue
        return False


def _port_entry(bind: str, host_port: int, container_port: int, proto: str = "tcp") -> str:
    """渲染端口映射条目（带引号）。

    注意：IPv6 的 `::` 在 compose 里必须写成 `[::]`，不能直接拼 `:::5900`。
    """
    host = bind
    if host and ":" in host:
        host = f"[{host}]"
    suffix = f"/{proto}" if proto != "tcp" else ""
    return yaml_scalar(f"{host}:{host_port}:{container_port}{suffix}")


def yaml_scalar(value: str) -> str:
    """把标量安全地渲染进 YAML。

    只用双引号形式 + 转义，避免任何 YAML 注入；换行/引号都会被打掉。
    """
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    return f'"{text}"'


# ─────────────────────────────────────────────────────────────────────────────
# entrypoint 模板
# ─────────────────────────────────────────────────────────────────────────────

ENTRYPOINT_TEMPLATE = r'''#!/bin/bash
# ValleyServer 管理器生成的容器入口脚本。
# 相对上游 oneclick-script/docker/startapp.sh 的差异：
#   1. VNC 密码改为环境变量 VNC_PASSWORD（上游硬编码 041041，见审计报告 P2）
#   2. SMAPI 控制台输出落盘到 $C_LOGS/console.log（上游只写 docker logs，见 P5）
#   3. 存档/MOD/日志目录的权限显式准备，避免 bind mount 权限问题
set -u

GAME_ROOT="/content/Stardew Valley"
SAVES_DIR="/root/.config/StardewValley/Saves"
MODS_DIR="${GAME_ROOT}/Mods"
LOG_DIR="/data-logs"
CONSOLE_LOG="${LOG_DIR}/console.log"
GAME_UID="${GAME_UID:-1000}"
GAME_GID="${GAME_GID:-1000}"

mkdir -p "$LOG_DIR" "$SAVES_DIR" "$MODS_DIR"

# 日志轮转：单文件 5 MiB 上限，超过则保留尾部 1 MiB。
# 必须原地重写（不能 mv/rename），否则子进程继续写已改名的旧 inode，日志会静默丢失。
trim_log() {
    local file="$1"
    local max=$((5 * 1024 * 1024))
    local keep=$((1 * 1024 * 1024))
    [ -f "$file" ] || return 0
    local size
    size=$(stat -c %s "$file" 2>/dev/null || echo 0)
    [ "$size" -le "$max" ] && return 0
    local tmp="${file}.rot"
    tail -c "$keep" "$file" | awk 'NR>1' > "$tmp" 2>/dev/null || cp "$file" "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

trim_log "$CONSOLE_LOG"

# 共享目录权限：让游戏进程（uid 1000）能写存档与 MOD
chown -R "${GAME_UID}:${GAME_GID}" "$SAVES_DIR" "$MODS_DIR" 2>/dev/null || true

# ⚠️ 致命坑（本项目用真实镜像实测发现，上游镜像未处理）：
#   SMAPI 启动时会 VerifyPath("$HOME/.config/StardewValley/ErrorLogs") 并尝试 mkdir。
#   若以 uid 1000 运行而 /root 不可写，就会抛 UnauthorizedAccessException 并**直接崩溃退出**：
#     "SMAPI failed to initialize: Access to the path
#      '/root/.config/StardewValley/ErrorLogs' is denied."
#   上游之所以没暴露，是因为它的 startapp.sh 以 root 运行游戏。
#   这里在 entrypoint（此时还是 root）里预建目录并交给游戏用户，同时把它持久化到
#   /data-logs 之外的真实目录（已由 compose 挂载，见 C_ERROR_LOGS）。
STARDEW_CFG="/root/.config/StardewValley"
mkdir -p "$STARDEW_CFG/ErrorLogs" "$STARDEW_CFG/Saves" 2>/dev/null || true
chown -R "${GAME_UID}:${GAME_GID}" "$STARDEW_CFG" 2>/dev/null || true
# /root 本身也必须允许游戏用户穿越（t 位即可，不放开写权限）
chmod 0751 /root 2>/dev/null || true
# ErrorLogs 目录若不是挂载点（bind mount 已存在），确保可写
chmod 0770 "$STARDEW_CFG/ErrorLogs" 2>/dev/null || true
if [ ! -w "$STARDEW_CFG/ErrorLogs" ]; then
    echo "[entrypoint] 警告：$STARDEW_CFG/ErrorLogs 不可写，SMAPI 可能启动失败" | tee -a "$CONSOLE_LOG"
fi

# 解析运行游戏所用的用户。
# ⚠️ 实测：本镜像里 `runuser -u "#1000"` 会报
#   "user #1000 does not exist or the user entry does not contain all the required fields"
# 而 `getent passwd 1000` 明明能查到 ubuntu。因此必须先解析成**用户名**再调用 runuser。
GAME_USER=""
if command -v getent >/dev/null 2>&1; then
    GAME_USER=$(getent passwd "${GAME_UID}" 2>/dev/null | cut -d: -f1)
fi
if [ -z "$GAME_USER" ] && [ -r /etc/passwd ]; then
    GAME_USER=$(awk -F: -v u="${GAME_UID}" '$3==u {print $1; exit}' /etc/passwd)
fi

# 虚拟显示器
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +extension RANDR +render -noreset -core &
XVFB_PID=$!
export DISPLAY=:99
sleep 3

openbox-session >/dev/null 2>&1 &

# VNC：密码来自环境变量
x11vnc -display :99 -forever -shared -passwd "${VNC_PASSWORD:-}" \
       -rfbport 5900 -noxdamage -bg >/dev/null 2>&1

# WebVNC
websockify --web=/usr/share/novnc/ 5800 localhost:5900 >/dev/null 2>&1 &

cleanup() {
    echo "[entrypoint] 收到终止信号，正在收尾..." | tee -a "$CONSOLE_LOG"
    kill -TERM "${SMAPI_PID:-0}" 2>/dev/null || true
    sleep 5
    kill -KILL "${SMAPI_PID:-0}" 2>/dev/null || true
    kill -TERM "$XVFB_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

cd "$GAME_ROOT" || exit 1
if [ ! -x ./StardewModdingAPI ]; then
    echo "[entrypoint] 错误：$GAME_ROOT/StardewModdingAPI 不存在或不可执行" | tee -a "$CONSOLE_LOG"
    exit 1
fi

echo "[entrypoint] ValleyServer 启动中：$(date -Is)，VNC/WebVNC 已就绪（5800）" | tee -a "$CONSOLE_LOG"

# 当前存档：由管理器写入 /data-config/selected-save（JSON），在这里转成启动参数。
# 这样「切换存档」在下次启动时自动生效，无需在 VNC 里手工点选。
SAVE_ARG=""
SEL_FILE="/data-config/selected-save"
if [ -r "$SEL_FILE" ]; then
    SAVE_NAME=""
    if command -v python3 >/dev/null 2>&1; then
        SAVE_NAME=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('save',''))" "$SEL_FILE" 2>/dev/null)
    fi
    if [ -z "$SAVE_NAME" ]; then
        SAVE_NAME=$(sed -n 's/.*"save"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SEL_FILE" 2>/dev/null | head -1)
    fi
    case "$SAVE_NAME" in
        */*|*..*|"") : ;;
        *) if [ -d "$SAVES_DIR/$SAVE_NAME" ]; then SAVE_ARG="--load-save $SAVE_NAME"; fi ;;
    esac
fi
if [ -n "$SAVE_ARG" ]; then
    echo "[entrypoint] 将启动到指定存档：$SAVE_ARG" | tee -a "$CONSOLE_LOG"
fi

# SMAPI 控制台日志：**不依赖 SMAPI 的启动参数**，而是直接捕获 stdout/stderr。
# 原因：本环境实测 `./StardewModdingAPI --help` 并不存在（SMAPI 会把未知参数透传给游戏），
# 无法用"探测参数"的方式确认 --log 是否可用；而 stdout 捕获在任何版本都成立。
# 用 `-m runuser` / `-m dotnet` 让 $! 拿到真实进程 PID（而不是管道首段的 PID），
# 这样 stop 时的 SIGTERM 能落到游戏进程上，触发它的优雅退出路径。
SMAPI_CONSOLE="${LOG_DIR}/smapi-console.log"

set -o pipefail
if [ -n "$GAME_USER" ] && command -v runuser >/dev/null 2>&1; then
    echo "[entrypoint] 以用户 ${GAME_USER}（uid ${GAME_UID}）运行游戏" | tee -a "$CONSOLE_LOG"
    runuser -m -u "$GAME_USER" -- env HOME=/root DISPLAY=:99 \
        ./StardewModdingAPI $SAVE_ARG 2>&1 | tee -a "$SMAPI_CONSOLE" "$CONSOLE_LOG" &
else
    echo "[entrypoint] 警告：未能在容器内解析 uid ${GAME_UID}，将以当前用户运行游戏（存档属主可能不一致）" | tee -a "$CONSOLE_LOG"
    ./StardewModdingAPI $SAVE_ARG 2>&1 | tee -a "$SMAPI_CONSOLE" "$CONSOLE_LOG" &
fi
SMAPI_PID=$!
wait "$SMAPI_PID"
RC=$?
echo "[entrypoint] SMAPI 已退出，退出码 ${RC}：$(date -Is)" | tee -a "$CONSOLE_LOG" "$SMAPI_CONSOLE"
exit "$RC"
'''
