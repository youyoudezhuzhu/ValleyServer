"""应用日志：单文件上限 5 MiB，超限保留尾部 1 MiB。

规则（skill 硬规则 #17 / 总纲 §9）：
  * **原地重写**，绝不 `mv`/`rename` —— 改名的旧 inode 仍被子进程持有，会造成「日志看起来在
    轮转、实际在丢」。
  * 默认值写在函数体内，便于测试脚本用 `sed` 从真实文件提取函数。
  * 日志读取支持按级别过滤与尾部截取，供 Web 界面使用。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import config as C

MAX_BYTES = 5 * 1024 * 1024
KEEP_BYTES = 1 * 1024 * 1024
LEVELS = ("debug", "info", "warn", "error")


def trim_log(path: Path, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> bool:
    """超限则原地 tail-trim。返回是否发生了轮转。

    默认值刻意写在函数签名里（带默认值），测试可直接导入或 sed 提取。
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    tmp = path.with_name(path.name + ".rot")
    try:
        with open(path, "rb") as src:
            src.seek(max(0, size - keep_bytes))
            data = src.read()
        # 丢掉被截断的首行（与 Python 版一致；首行可能不完整）
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
        marker = (f"[logrotate] 日志超过 {max_bytes} 字节，保留尾部 {keep_bytes} 字节"
                  f"（原 {size} 字节）\n").encode("utf-8")
        with open(tmp, "wb") as out:
            out.write(marker)
            out.write(data)
        # 原地重写：保持 inode 不变
        with open(tmp, "rb") as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.unlink(tmp)
    except OSError:
        return False
    return True


class Logger:
    """极简结构化日志器：写 `manager.log`，并按级别过滤。"""

    FILES = {
        "manager": "manager.log",
        "task": "task.log",
        "container": "container.log",
        "audit": "audit.log",
    }

    def __init__(self, log_dir: Path | None = None, name: str = "manager") -> None:
        self.log_dir = Path(log_dir) if log_dir else C.LOG_DIR
        self.name = name if name in self.FILES else "manager"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / self.FILES[self.name]
        self._counter = 0

    def write(self, message: str, level: str = "info") -> None:
        level = level if level in LEVELS else "info"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        line = f"{timestamp} [{level.upper():5s}] {message}\n"
        try:
            if self._counter % 200 == 0:
                trim_log(self.path)
            self._counter += 1
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    def debug(self, m: str) -> None:
        self.write(m, "debug")

    def info(self, m: str) -> None:
        self.write(m, "info")

    def warn(self, m: str) -> None:
        self.write(m, "warn")

    def error(self, m: str) -> None:
        self.write(m, "error")

    def tail(self, lines: int = 300, level: str = "") -> list[str]:
        lines = max(1, min(int(lines), 5000))
        if not self.path.is_file():
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                block = 256 * 1024
                data = b""
                while size > 0 and data.count(b"\n") <= lines and len(data) < 4 * 1024 * 1024:
                    step = min(block, size)
                    size -= step
                    fh.seek(size)
                    data = fh.read(step) + data
        except OSError:
            return []
        text = data.decode("utf-8", "replace")
        out = [ln for ln in text.splitlines() if ln]
        if level in LEVELS:
            out = [ln for ln in out if f"[{level.upper()}" in ln]
        return out[-lines:]

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, filename in self.FILES.items():
            p = self.log_dir / filename
            try:
                st = p.stat()
                out[name] = {
                    "file": str(p), "size": st.st_size,
                    "modified_at": st.st_mtime,
                    "modified_at_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                                     time.localtime(st.st_mtime)),
                }
            except OSError:
                out[name] = {"file": str(p), "size": 0, "modified_at": 0,
                             "modified_at_iso": ""}
        return out


_loggers: dict[str, Logger] = {}


def get_logger(name: str = "manager") -> Logger:
    if name not in _loggers:
        _loggers[name] = Logger(name=name)
    return _loggers[name]


class Tee:
    """同时写文件与标准输出的日志门面（供 run.py 使用）。"""

    def __init__(self, logger: Logger, echo: bool = False) -> None:
        self.logger = logger
        self.echo = echo

    def __call__(self, message: str, level: str = "info") -> None:
        self.logger.write(message, level)
        if self.echo:
            print(f"[{level}] {message}", flush=True)
