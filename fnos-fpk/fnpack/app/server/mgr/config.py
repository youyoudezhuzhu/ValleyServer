"""ValleyServer 管理器 —— 路径与配置常量。

设计原则（见 docs/architecture.md）：
  * 所有"管理"数据由 FPK 持有，位于 $TRIM_PKGHOME（升级/卸载默认保留）。
  * 所有"游戏运行"数据位于 $TRIM_PKGHOME/data（可整体迁移到 data-share 目录）。
  * 绝不把持久数据放在 $TRIM_APPDEST（升级会被整体替换）。
  * 本模块只依赖 Python 标准库。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# 环境变量桥接：fnOS 不注入 DATA_LIBRARY_PATH（已由二进制反查证实），
# 因此必须用 TRIM_PKGVAR 兜底；HOME 必须无条件覆盖为 TRIM_PKGHOME
# （fnOS 已注入 HOME=/root，用 ${HOME:-...} 不会生效）。
# ─────────────────────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


APPDEST = Path(_env("TRIM_APPDEST", "/vol1/@appcenter/valleyserver"))
PKGVAR = Path(_env("DATA_LIBRARY_PATH") or _env("TRIM_PKGVAR", "/vol1/@appdata/valleyserver"))
PKGHOME = Path(_env("TRIM_PKGHOME", "/vol1/@apphome/valleyserver"))
PKGETC = Path(_env("TRIM_PKGETC", "/vol1/@appconf/valleyserver"))
PKGTMP = Path(_env("TRIM_PKGTMP", "/vol1/@apptemp/valleyserver"))
APP_VERSION = _env("APP_VERSION", "0.0.0-dev")

# 代码目录（app.tgz 解包后落在 $TRIM_APPDEST/server）
SERVER_DIR = APPDEST / "server"
WWW_DIR = APPDEST / "www"
# 静态资源在 apdest/www，开发时回落仓库内 app/www
if not (WWW_DIR / "index.html").is_file():
    _dev_www = Path(__file__).resolve().parent.parent.parent / "www"
    if (_dev_www / "index.html").is_file():
        WWW_DIR = _dev_www

# ── 数据目录（全部位于 PKGHOME，升级保留） ───────────────────────────────────
DATA_ROOT = PKGHOME / "data"            # 游戏运行数据总体根
GAME_DIR = DATA_ROOT / "game"           # 运行数据目录（PID、日志、启动描述）
SAVES_DIR = DATA_ROOT / "Saves"         # 存档（原生：符号链接到 $HOME/.config/StardewValley/Saves）
MODS_DIR = DATA_ROOT / "Mods"           # MOD（原生：符号链接到游戏的 Mods 目录）
CONFIG_DIR = PKGHOME / "manager"        # 管理器自身配置/数据库/审计日志
GAME_FILES_DIR = DATA_ROOT / "gamefiles"    # 原生模式的游戏本体（用户提供）
RUNDIR = GAME_DIR / "run"                   # 原生模式的 PID / 运行态
SNAPSHOT_DIR = DATA_ROOT / "snapshots"  # 快照仓库
STAGING_DIR = DATA_ROOT / "staging"     # 更新暂存区
RESTORE_TMP_DIR = DATA_ROOT / "restore-tmp"  # 恢复临时区
ARCHIVE_DIR = DATA_ROOT / "archives"    # 版本档案（游戏/SMAPI/MOD 版本与内容指纹记录）
LOG_DIR = PKGVAR / "logs"               # 运行日志（PKGVAR：运行数据）
TASK_DB = CONFIG_DIR / "tasks.db"       # 任务队列 SQLite
MANAGER_CONFIG = CONFIG_DIR / "config.json"
BRANDING_DIR = DATA_ROOT / "branding"   # VNC 密码等运行期生成的凭证


def ensure_dirs() -> None:
    """幂等创建全部数据目录。install_callback / 启动时调用。"""
    for d in (
        DATA_ROOT, GAME_DIR, RUNDIR, GAME_FILES_DIR, SAVES_DIR, MODS_DIR,
        CONFIG_DIR, SNAPSHOT_DIR,
        STAGING_DIR, RESTORE_TMP_DIR, ARCHIVE_DIR, LOG_DIR, BRANDING_DIR,
        PKGTMP,
    ):
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 管理器配置
# ─────────────────────────────────────────────────────────────────────────────



@dataclass
class NativeConfig:
    """原生部署的运行参数。

    实测结论（见 docs/native-deployment.md）：
      * 游戏本体与 SMAPI 都是 **.NET 6 自包含**发布（runtimeconfig 里是
        `includedFrameworks`），机器上**不需要安装 .NET 运行时**；
      * 唯一缺的是虚拟显示栈（Xvfb/x11vnc/websockify/openbox/novnc），
        这些都能从 Debian 仓库取得，由管理器**捆绑进应用目录**并在运行时解析，
        **不修改系统包状态**。
    """

    # 显示
    display_number: int = 99
    screen_width: int = 1280
    screen_height: int = 720
    screen_depth: int = 24
    window_manager: str = "openbox"
    # VNC
    vnc_port: int = 5900
    vnc_password: str = ""
    # WebVNC
    webvnc_port: int = 5800
    # 游戏
    game_port: int = 24642
    cmdwebui_port: int = 29103
    # 监听范围（**只对 VNC 与 WebVNC 生效**）：
    #   "0.0.0.0"   —— 默认：局域网可直接连 VNC(5900)/WebVNC(5800)。
    #                  这两个入口没有独立认证（VNC 有 8 位随机密码，WebVNC 无），
    #                  因此界面会显示风险提示。
    #   "127.0.0.1" —— 仅本机：x11vnc 加 -localhost、websockify 绑回环；
    #                  此时只能经管理器（18242）或飞牛网关使用 WebVNC。
    # 注意：CommandWebUI 由上游模组自行监听所有网卡，本项**无法约束**它。
    host_bind: str = "0.0.0.0"
    # 停止游戏时先发 SIGTERM 并等待的秒数（给 SMAPI/ALOS 收尾时间）
    graceful_stop_seconds: int = 60
    # 显示栈来源：bundled（应用内捆绑，默认）| system（系统已装）
    xstack_source: str = "bundled"
    # 额外传给 SMAPI 的参数（空 = 不加）
    extra_args: str = ""
    # 是否启用软件渲染回退（无 GPU 时建议开启）
    software_gl: bool = True
    # 显示后端：
    #   "auto"   —— **默认**：有可用核显栈（render 节点 + weston + Xwayland）就用核显，
    #                否则自动退回软件渲染。这样"有 GPU 的机器默认就用上 GPU"。
    #   "xvfb"   —— 强制 Xvfb 纯软件渲染（兼容性最好，但游戏会用满约 3 个核）。
    #   "weston" —— 强制 Weston headless(GPU) + Xwayland，X11 客户端走核显（iris）。
    #
    # 实测（Intel ADL-N 核显）：核显模式下游戏 CPU 约 290% → 约 30%，
    # 整机负载 8~10 → 0.83；代价是首次启动略慢（着色器编译）。
    display_backend: str = "auto"
    # 每次启动服务器后，是否自动载入最新存档并开服。
    # 为什么需要：实测确认 **ALOS 重启后不会自动载档** —— 游戏会一直停在标题页，
    # 必须主动发 `ServerCMD.set_multiplayermode 2` + `ServerCMD.load_save <名>`。
    # 默认开启，否则重启一次服务器就不再是"无人值守"了。
    auto_host_on_start: bool = True
    # 无人值守（真·没有人联机时也推进天数）：
    # ALOS 的自动作息条件是 `numPlayers >= 1 || debug`，而 numPlayers 是
    # `Game1.otherFarmers.Count`（**不含房主**）—— 默认没人联机时房主不会睡觉，
    # 时间/天数不推进。打开本项后，管理器在开服后发送 ALOS 自带的
    # `alos.debug_server`（官方说明 "lets server run when no players are connected"），
    # 让房主在 22:00（游戏内）自动睡觉。关闭则需至少 1 名玩家在线。
    alos_unattended: bool = True


@dataclass
class PolicyConfig:
    """生命周期/保留策略。"""

    keep_auto_snapshots: int = 10
    keep_manual_snapshots: int = 5
    auto_snapshot_enabled: bool = True
    auto_snapshot_interval_minutes: int = 360
    min_free_space_mb: int = 5120          # 低于此值拒绝高风险变更
    snapshot_space_estimate_mb: int = 512  # 单次快照预留估算
    start_retry_max: int = 5               # 异常退出自动恢复最大重试次数
    start_retry_backoff_seconds: int = 20  # 首次退避；指数递增
    supervisor_enabled: bool = True
    auto_start_with_nas: bool = True
    confirm_destructive: bool = True


@dataclass
class ManagerConfig:
    version: int = 1
    native: NativeConfig = field(default_factory=NativeConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    # 网页面板设置
    panel_tcp_port: int = 18242
    session_ttl_minutes: int = 720
    # 首次运行生成的登录口令（明文仅存于 PKGHOME，权限 600）
    admin_password: str = ""
    upstream_repo: str = "Lixeer/ValleyServer"
    upstream_baseline_commit: str = "97604b6aa2388c10d767be63b4dc516f3bfec4fb"
    upstream_baseline_tag: str = "v2.0.0"
    fork_repo: str = "youyoudezhuzhu/ValleyServer"
    # 审计时从上游发行包核实的版本（仅作为参考基线展示，不参与部署）
    upstream_game_version: str = "1.6.15"
    upstream_smapi_version: str = "4.5.1"
    # 监听范围的一次性迁移标记（见 migrate_host_bind）。
    # 旧版本里 host_bind 根本没传给进程，所以存量的 "127.0.0.1" 只是**未被执行的旧默认值**；
    # 0.2.14 起该设置真正生效，若不迁移，升级后用户原本可用的局域网 VNC 会突然被掐断。
    host_bind_reviewed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def migrate_host_bind(cfg: ManagerConfig) -> ManagerConfig:
    """把"未被执行的旧默认值"迁移成新默认值（一次性、幂等）。

    背景：0.2.13 及以前，`native.host_bind` **没有传给任何进程**（实测两个端口始终监听
    0.0.0.0），界面却写着"仅本机"。0.2.14 起该设置真正生效，于是存量配置里的
    `127.0.0.1` 会突然把局域网 VNC/WebVNC 关掉 —— 那不是用户的意图，只是旧默认值。

    规则：仅当 config 里没记录过 `host_bind_reviewed`，且值恰为旧默认 `127.0.0.1` 时，
    改成新默认 `0.0.0.0` 并打上标记；之后用户在界面上的任何选择都会被尊重（不再自动改）。
    """
    if getattr(cfg, "host_bind_reviewed", False):
        return cfg
    if cfg.native.host_bind == "127.0.0.1":
        cfg.native.host_bind = "0.0.0.0"
    cfg.host_bind_reviewed = True
    try:
        save_config(cfg)
    except OSError:
        pass
    return cfg


def load_config() -> ManagerConfig:
    cfg = ManagerConfig()
    if MANAGER_CONFIG.is_file():
        try:
            raw = json.loads(MANAGER_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        if isinstance(raw, dict):
            cfg = _merge(cfg, raw)
            if not raw.get("host_bind_reviewed"):
                cfg = migrate_host_bind(cfg)
    return cfg


def _merge(cfg: ManagerConfig, raw: dict[str, Any]) -> ManagerConfig:
    """把字典合并进 dataclass，未知键忽略，类型不符回落默认值。"""
    for key, value in raw.items():
        if not hasattr(cfg, key):
            continue
        current = getattr(cfg, key)
        if isinstance(current, (PolicyConfig, NativeConfig)) and isinstance(value, dict):
            for k2, v2 in value.items():
                if hasattr(current, k2) and isinstance(v2, type(getattr(current, k2))):
                    setattr(current, k2, v2)
                else:
                    # bool 是 int 的子类，单独放行
                    if hasattr(current, k2) and isinstance(getattr(current, k2), bool) \
                            and isinstance(v2, bool):
                        setattr(current, k2, v2)
        elif isinstance(value, type(current)):
            setattr(cfg, key, value)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 初始口令（安装期生成，与 run.py 的 AuthStore 使用同一算法）
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(password: str, *, iterations: int = 200_000) -> str:
    """PBKDF2-HMAC-SHA256。明文不落盘。"""
    import hashlib
    import secrets as _secrets
    salt = _secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    import hashlib
    import hmac as _hmac
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$")
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        expected = bytes.fromhex(digest_hex)
        salt = bytes.fromhex(salt_hex)
        rounds = int(iterations)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return _hmac.compare_digest(got, expected)


def _stamp() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_initial_password(cfg: ManagerConfig) -> tuple[ManagerConfig, str]:
    """若尚无口令则生成随机口令并落盘。返回 (cfg, 明文)。幂等。

    安装期（install_callback）与首次启动都会调用；已有口令时直接返回空串。
    """
    if cfg.admin_password:
        return cfg, ""
    import secrets as _secrets
    password = _secrets.token_urlsafe(12)
    cfg.admin_password = hash_password(password)
    save_config(cfg)
    hint = PKGHOME / "initial-password.txt"
    try:
        hint.parent.mkdir(parents=True, exist_ok=True)
        hint.write_text(
            "ValleyServer 管理器初始登录口令（首次登录后请立即修改）\n"
            f"口令：{password}\n"
            f"生成时间：{_stamp()}\n"
            f"登录地址：飞牛应用中心 → ValleyServer 管理器；或 http://<NAS-IP>:{cfg.panel_tcp_port}/\n",
            encoding="utf-8",
        )
        os.chmod(hint, 0o600)
    except OSError:
        pass
    return cfg, password


def save_config(cfg: ManagerConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANAGER_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MANAGER_CONFIG)
    try:
        os.chmod(MANAGER_CONFIG, 0o600)
    except OSError:
        pass
