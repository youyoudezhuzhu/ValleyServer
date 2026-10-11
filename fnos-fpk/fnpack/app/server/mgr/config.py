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
GAME_DIR = DATA_ROOT / "game"           # 宿主机侧的运行数据容器（compose 工作目录）
SAVES_DIR = DATA_ROOT / "Saves"         # 存档（挂到容器 /root/.config/StardewValley/Saves）
MODS_DIR = DATA_ROOT / "Mods"           # MOD（挂到容器 /content/Stardew Valley/Mods）
CONFIG_DIR = PKGHOME / "manager"        # 管理器自身配置/数据库/审计日志
SNAPSHOT_DIR = DATA_ROOT / "snapshots"  # 快照仓库
STAGING_DIR = DATA_ROOT / "staging"     # 更新暂存区
RESTORE_TMP_DIR = DATA_ROOT / "restore-tmp"  # 恢复临时区
ARCHIVE_DIR = DATA_ROOT / "archives"    # 版本档案（镜像/游戏/SMAPI/MOD 版本记录）
LOG_DIR = PKGVAR / "logs"               # 运行日志（PKGVAR：运行数据）
TASK_DB = CONFIG_DIR / "tasks.db"       # 任务队列 SQLite
MANAGER_CONFIG = CONFIG_DIR / "config.json"
BRANDING_DIR = DATA_ROOT / "branding"   # VNC 密码等运行期生成的凭证


def ensure_dirs() -> None:
    """幂等创建全部数据目录。install_callback / 启动时调用。"""
    for d in (
        DATA_ROOT, GAME_DIR, SAVES_DIR, MODS_DIR, CONFIG_DIR, SNAPSHOT_DIR,
        STAGING_DIR, RESTORE_TMP_DIR, ARCHIVE_DIR, LOG_DIR, BRANDING_DIR,
        PKGTMP,
    ):
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 管理器配置
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_IMAGE = "lixeer/valley-server"
DEFAULT_IMAGE_TAG = "v26.5"
# 已核实的上游不可变标识（2026-10-11 从 registry manifest 读取，见 docs/audit-report.md）
DEFAULT_IMAGE_DIGEST = (
    "sha256:d84f924108b0fed5c459f5276a56bbb47fe450c0de2db807a55990adfd58e1b5"
)


@dataclass
class ContainerConfig:
    """被管理的游戏容器参数（全部进白名单模板，不接受任意 YAML）。"""

    image: str = DEFAULT_IMAGE
    image_tag: str = DEFAULT_IMAGE_TAG
    image_digest: str = DEFAULT_IMAGE_DIGEST
    # None 表示使用 tag；显式 pin 后使用 digest 拉取，可保证内容不可变
    pin_digest: bool = True
    game_port: int = 24642          # UDP，游戏联机端口
    webvnc_port: int = 5800         # WebVNC (noVNC)
    vnc_port: int = 5900            # 原生 VNC
    cmdwebui_port: int = 29103      # CommandWebUI（SMAPI 控制台）
    host_bind: str = "0.0.0.0"      # 监听地址；可设 127.0.0.1 关闭外网访问
    vnc_password: str = ""          # 空 = 启动时随机生成
    shm_size: str = "512m"
    cpu_limit: str = ""             # 空 = 不限制
    mem_limit: str = ""             # 空 = 不限制
    restart_policy: str = "unless-stopped"
    healthcheck_interval: int = 30
    healthcheck_retries: int = 3
    healthcheck_start_period: int = 180   # 游戏启动慢，给足冷启动余量


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
    metrics_interval_seconds: int = 10


@dataclass
class ManagerConfig:
    version: int = 1
    container: ContainerConfig = field(default_factory=ContainerConfig)
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config() -> ManagerConfig:
    cfg = ManagerConfig()
    if MANAGER_CONFIG.is_file():
        try:
            raw = json.loads(MANAGER_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        if isinstance(raw, dict):
            cfg = _merge(cfg, raw)
    return cfg


def _merge(cfg: ManagerConfig, raw: dict[str, Any]) -> ManagerConfig:
    """把字典合并进 dataclass，未知键忽略，类型不符回落默认值。"""
    for key, value in raw.items():
        if not hasattr(cfg, key):
            continue
        current = getattr(cfg, key)
        if isinstance(current, (ContainerConfig, PolicyConfig)) and isinstance(value, dict):
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
