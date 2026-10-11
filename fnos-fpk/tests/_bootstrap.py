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
    # 关键：清除 fnOS 真实注入的 DATA_LIBRARY_PATH，检验代码的兜底逻辑
    os.environ.pop("DATA_LIBRARY_PATH", None)
    os.environ["HOME"] = str(paths["TRIM_PKGHOME"])

    server_dir = Path(__file__).resolve().parent.parent / "fnpack" / "app" / "server"
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))

    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    return {**{k: str(v) for k, v in paths.items()}, "TEST_ROOT": str(root)}


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
