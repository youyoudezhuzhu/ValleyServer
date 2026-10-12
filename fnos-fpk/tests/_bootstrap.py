"""测试引导：在导入 mgr.* 之前，把 TRIM_* 指向沙箱目录。

**这一步是必须的**：mgr/config.py 在导入时解析路径常量。若在真实 fnOS 环境下
不重定向，测试会去操作真实应用的目录（本项目所在机器本身就是 fnOS 真机，
`/vol1/@apphome/valleyserver` 等路径是真实存在的）。

用法：
    from _bootstrap import isolated_env
    env = isolated_env()          # 返回 dict，含 sandbox 路径
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path


def isolated_env(appname: str = "valleyserver") -> dict[str, str]:
    root = Path(tempfile.mkdtemp(prefix=f"vstest-{appname}-"))
    paths = {
        "TRIM_APPDEST": root / "appcenter" / appname,
        "TRIM_PKGVAR": root / "appdata" / appname,
        "TRIM_PKGHOME": root / "apphome" / appname,
        "TRIM_PKGETC": root / "appconf" / appname,
        "TRIM_PKGTMP": root / "apptemp" / appname,
        "TRIM_APPNAME": appname,
        "TRIM_APPVER": "0.1.0-test",
        "APP_VERSION": "0.1.0-test",
    }
    for key, value in paths.items():
        os.environ[key] = str(value)
    # 把"等待控制台通道/端口就绪"的时长调到极小：单测不该真等 60+90 秒。
    # 用环境变量（而不是改模块常量）：测试会 importlib.reload(orchestration)，
    # 模块常量会被重置（实测踩到）。
    os.environ["VS_CONSOLE_CHANNEL_WAIT"] = "0.2"
    os.environ["VS_HOST_PORT_WAIT"] = "0.2"
    # 关键：清除 fnOS 真实注入的 DATA_LIBRARY_PATH，检验代码的兜底逻辑
    os.environ.pop("DATA_LIBRARY_PATH", None)
    os.environ["HOME"] = str(paths["TRIM_PKGHOME"])

    server_dir = Path(__file__).resolve().parent.parent / "fnpack" / "app" / "server"
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))

    _isolate_ports()

    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    return {**{k: str(v) for k, v in paths.items()}, "TEST_ROOT": str(root)}


def _isolate_ports() -> None:
    """把沙箱配置里的端口改成**确定空闲**的端口。

    为什么必须做：默认端口（游戏 24642、CommandWebUI 29103、管理器 18242 …）
    在本机上是**真实应用正在使用**的端口。测试若沿用默认值，就会出现
    "单元测试摸到真实实例"—— 实测踩到过两次：
      * 一次测试直接连上了真实游戏的控制台通道并发出指令；
      * 一次断言"控制台通道不可用"却因真机在监听而失败。
    仅靠"不写死地址"是不够的，必须在引导阶段就把端口隔离掉。
    """
    import socket

    from mgr import config as C

    def free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


    C.ensure_dirs()
    cfg = C.load_config()
    for field in ("service_port", "game_port", "vnc_port", "webvnc_port",
                  "cmdwebui_port", "steamcmd_port"):
        if hasattr(cfg.native, field):
            setattr(cfg.native, field, free_port())
    C.save_config(cfg)


def sandbox_paths() -> dict[str, Path]:
    from mgr import config as C
    return {
        "root": C.PKGHOME.parent.parent,
        "appdest": C.APPDEST,
        "pkgevar": C.PKGVAR,
        "pkghome": C.PKGHOME,
        "data": C.DATA_ROOT,
        "saves": C.SAVES_DIR,
        "mods": C.MODS_DIR,
        "snapshots": C.SNAPSHOT_DIR,
        "staging": C.STAGING_DIR,
        "restore": C.RESTORE_TMP_DIR,
        "logs": C.LOG_DIR,
    }
