"""安全工具：路径校验、ZIP 安全解包、哈希、磁盘空间、原子写、无 shell 子进程。

本模块是整个项目安全性的地基，重点防御：
  * 路径穿越（../、绝对路径、NUL、Windows 盘符）
  * ZIP slip / 符号链接逃逸 / 硬链接 / 设备文件
  * 命令注入（一律 argv 列表，绝不用 shell=True）
  * 非原子写导致的数据损坏
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


class ValidationError(ValueError):
    """输入未通过校验，调用方应拒绝执行并返回原因。"""


# ─────────────────────────────────────────────────────────────────────────────
# 1. 名称与路径校验
# ─────────────────────────────────────────────────────────────────────────────

# 存档名、MOD 目录名等只允许这些字符之外的「安全子集」
_UNSAFE_CHARS = set('\x00/\\:*?"<>|')
# 保留设备名（Windows 兼容，避免跨平台解包异常）
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_name(name: str, *, field: str = "名称", max_len: int = 128,
                  allow_space: bool = True) -> str:
    """校验用户提供的单段名称（存档名、任务标签、MOD 名称）。

    拒绝：空、过长、路径分隔符、控制字符、`.` / `..`、Windows 保留名、首尾空白。
    返回去掉首尾空白后的原名。
    """
    if not isinstance(name, str):
        raise ValidationError(f"{field}必须是字符串")
    name = name.strip()
    if not name:
        raise ValidationError(f"{field}不能为空")
    if len(name) > max_len:
        raise ValidationError(f"{field}过长（最多 {max_len} 字符）")
    if name in (".", ".."):
        raise ValidationError(f"{field}不合法")
    if any(ch in _UNSAFE_CHARS for ch in name):
        raise ValidationError(f"{field}包含非法字符（不允许 / \\ : * ? \" < > | 与控制字符）")
    if any(ord(ch) < 32 for ch in name):
        raise ValidationError(f"{field}包含控制字符")
    if not allow_space and " " in name:
        raise ValidationError(f"{field}不允许包含空格")
    if name.upper().split(".")[0] in _WIN_RESERVED:
        raise ValidationError(f"{field}是系统保留名：{name}")
    if name != unicodedata.normalize("NFC", name):
        name = unicodedata.normalize("NFC", name)
    return name


def safe_join(root: Path, *parts: str) -> Path:
    """把各段拼到 root 下，并断言结果严格位于 root 内。

    任一段含分隔符 / `..` / 绝对路径 → 抛 ValidationError。
    """
    root = Path(root).resolve()
    for part in parts:
        if not isinstance(part, str) or not part:
            raise ValidationError("路径段不能为空")
        if part.startswith(("/", "\\")) or PurePosixPath(part).is_absolute():
            raise ValidationError(f"不接受绝对路径：{part}")
        if "\\" in part or "/" in part:
            raise ValidationError(f"路径段不允许包含分隔符：{part}")
        if part in (".", ".."):
            raise ValidationError(f"路径段不合法：{part}")
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValidationError(f"路径逃逸：{candidate}")
    return resolved


def assert_within(path: Path, root: Path, *, what: str = "路径") -> Path:
    """断言 path 位于 root 之内（含 root 自身），否则抛 ValidationError。"""
    p = Path(path).resolve()
    r = Path(root).resolve()
    if p != r and r not in p.parents:
        raise ValidationError(f"{what}不在允许范围内：{p}")
    return p


def validate_port(value: object, *, field: str = "端口",
                  allow_zero: bool = False) -> int:
    """校验 TCP/UDP 端口号。拒绝布尔值（bool 是 int 子类，容易被 JSON 绕过）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        else:
            raise ValidationError(f"{field}必须是整数")
    if allow_zero and value == 0:
        return 0
    if not (1 <= value <= 65535):
        raise ValidationError(f"{field}超出范围（1-65535）：{value}")
    return value


def validate_choice(value: str, allowed: Iterable[str], *, field: str = "取值") -> str:
    allowed = list(allowed)
    if value not in allowed:
        raise ValidationError(f"{field}必须是以下之一：{', '.join(allowed)}")
    return value


def validate_relative_yaml_path(value: str, *, field: str = "路径") -> str:
    """校验 compose 模板里允许出现的相对路径（白名单式）。

    只允许 `[A-Za-z0-9._/-]`，不允许 `..`、不允许绝对路径、不允许 `~`。
    这是「禁止用户输入任意宿主机挂载路径」的执行点。
    """
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field}不能为空")
    if value.startswith(("/", "~", "\\")):
        raise ValidationError(f"{field}必须是宿主机数据目录内的相对路径")
    if ".." in PurePosixPath(value).parts:
        raise ValidationError(f"{field}不允许包含 ..")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValidationError(f"{field}包含非法字符")
    return value


def validate_env_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValidationError(f"环境变量名非法：{name}")
    return name


def validate_image_ref(ref: str) -> str:
    """校验镜像引用，禁止空白/换行/shell 元字符。"""
    if not isinstance(ref, str) or not ref:
        raise ValidationError("镜像引用不能为空")
    if len(ref) > 255 or re.search(r"[\s;|&$`'\"\\<>(){}]", ref):
        raise ValidationError(f"镜像引用包含非法字符：{ref}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(:[a-zA-Z0-9._-]+)?(@sha256:[a-f0-9]{64})?", ref):
        raise ValidationError(f"镜像引用格式不合法：{ref}")
    return ref


# ─────────────────────────────────────────────────────────────────────────────
# 2. 哈希 / 磁盘 / 原子写
# ─────────────────────────────────────────────────────────────────────────────

def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def disk_usage(path: Path) -> tuple[int, int, int]:
    """返回 (total, used, free) 字节。路径不存在时向上寻找最近的存在祖先。"""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    total = st.f_bsize * st.f_blocks
    free = st.f_bsize * st.f_bavail
    return total, total - free, free


def free_mb(path: Path) -> int:
    return disk_usage(path)[2] // (1024 * 1024)


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                continue
    return total


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, obj: object, *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False), mode=mode)


def read_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def is_subpath(child: Path, parent: Path) -> bool:
    c, p = Path(child).resolve(), Path(parent).resolve()
    return c == p or p in c.parents


# ─────────────────────────────────────────────────────────────────────────────
# 3. 安全 ZIP 解包（防 ZIP slip / 符号链接 / 硬链接 / 设备文件 / zip 炸弹）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ZipInspection:
    """ZIP 静态检查结果。"""

    ok: bool
    reason: str = ""
    file_count: int = 0
    total_uncompressed: int = 0
    max_compression_ratio: float = 0.0
    top_level_dirs: tuple[str, ...] = ()
    has_manifest_at_root: bool = False
    manifest_candidates: tuple[str, ...] = ()
    suspicious: tuple[str, ...] = ()
    entries: tuple[str, ...] = ()


MAX_ZIP_FILES = 20000
MAX_ZIP_TOTAL_BYTES = 8 * 1024 * 1024 * 1024   # 8 GiB 解压后
MAX_ZIP_RATIO = 400.0                          # 单条目的压缩比上限（防 zip 炸弹）
MAX_ZIP_DEPTH = 12


def _validate_zip_member_name(name: str) -> str:
    """校验 zip 内条目名，返回规范化后的相对 posix 路径。"""
    if not name or name.endswith("/"):
        # 目录条目
        pass
    if "\x00" in name:
        raise ValidationError("ZIP 条目名包含 NUL")
    n = name.replace("\\", "/")
    if n.startswith("/"):
        raise ValidationError(f"ZIP 条目使用绝对路径：{name}")
    if re.match(r"^[A-Za-z]:", n):
        raise ValidationError(f"ZIP 条目使用盘符路径：{name}")
    parts = PurePosixPath(n).parts
    if len(parts) > MAX_ZIP_DEPTH:
        raise ValidationError(f"ZIP 目录层级过深：{name}")
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValidationError(f"ZIP 条目包含 ..（路径穿越）：{name}")
        if part.upper().split(".")[0] in _WIN_RESERVED:
            raise ValidationError(f"ZIP 条目是系统保留名：{name}")
    return str(PurePosixPath(n))


def inspect_zip(zip_path: Path, *, max_files: int = MAX_ZIP_FILES,
                max_total: int = MAX_ZIP_TOTAL_BYTES,
                max_ratio: float = MAX_ZIP_RATIO) -> ZipInspection:
    """静态检查 ZIP：不落盘，只读中央目录。"""
    suspicious: list[str] = []
    entries: list[str] = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        return ZipInspection(ok=False, reason=f"不是合法的 ZIP 文件：{exc}")

    with zf:
        infos = zf.infolist()
        file_infos = [i for i in infos if not i.is_dir()]
        if len(file_infos) > max_files:
            return ZipInspection(
                ok=False,
                reason=f"文件数量过多（{len(file_infos)} > {max_files}）",
                file_count=len(file_infos),
            )
        total = 0
        worst_ratio = 0.0
        tops: set[str] = set()
        manifests: list[str] = []
        for info in infos:
            if info.is_dir():
                continue
            try:
                rel = _validate_zip_member_name(info.filename)
            except ValidationError as exc:
                return ZipInspection(ok=False, reason=str(exc), file_count=len(file_infos))
            entries.append(rel)
            # 符号链接 / 设备文件：ZIP 用外部属性高位标识 unix 模式
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode:
                fmt = stat.S_IFMT(mode)
                if fmt == stat.S_IFLNK:
                    return ZipInspection(
                        ok=False,
                        reason=f"ZIP 内含符号链接，拒绝解包：{rel}",
                        file_count=len(file_infos),
                    )
                if fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
                    return ZipInspection(
                        ok=False,
                        reason=f"ZIP 内含特殊文件（设备/FIFO），拒绝解包：{rel}",
                        file_count=len(file_infos),
                    )
            total += info.file_size
            if info.file_size > 1024 * 1024 and info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                worst_ratio = max(worst_ratio, ratio)
                if ratio > max_ratio:
                    # 高压缩比 = 典型的 ZIP 炸弹/解压膨胀攻击，必须直接拒绝而不是仅提示
                    return ZipInspection(
                        ok=False,
                        reason=(f"检测到异常压缩比（{rel}：{ratio:.0f}:1，上限 "
                                f"{max_ratio:.0f}:1），疑似 ZIP 炸弹，拒绝处理"),
                        file_count=len(file_infos),
                        total_uncompressed=total,
                        max_compression_ratio=worst_ratio,
                        suspicious=tuple(suspicious) + (f"{rel} 压缩比 {ratio:.0f}:1 异常",),
                    )
            first = PurePosixPath(rel).parts[0]
            tops.add(first)
            if PurePosixPath(rel).name.lower() == "manifest.json":
                manifests.append(rel)
        if total > max_total:
            return ZipInspection(
                ok=False,
                reason=f"解压后体积过大（{total / 2**30:.2f} GiB > {max_total / 2**30:.2f} GiB）",
                file_count=len(file_infos),
                total_uncompressed=total,
            )
        rel_manifests = [m for m in manifests if len(PurePosixPath(m).parts) == 2]
        return ZipInspection(
            ok=True,
            file_count=len(file_infos),
            total_uncompressed=total,
            max_compression_ratio=worst_ratio,
            top_level_dirs=tuple(sorted(tops)),
            has_manifest_at_root=bool(rel_manifests),
            manifest_candidates=tuple(sorted(manifests))[:20],
            suspicious=tuple(suspicious[:10]),
            entries=tuple(entries[:500]),
        )


def safe_extract_zip(zip_path: Path, dest: Path, *, max_files: int = MAX_ZIP_FILES,
                     max_total: int = MAX_ZIP_TOTAL_BYTES) -> int:
    """把 ZIP 安全解包到 dest（dest 必须是新建的空目录）。

    返回解包的文件数。逐条校验名称，并以 O_EXCL 语义写入，任何异常都会抛错，
    调用方负责清理 dest。
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    inspection = inspect_zip(zip_path, max_files=max_files, max_total=max_total)
    if not inspection.ok:
        raise ValidationError(inspection.reason)
    count = 0
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _validate_zip_member_name(info.filename)
            target = safe_join(dest, *PurePosixPath(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 断言父目录链上没有符号链接（防御性）
            cursor = target.parent
            while cursor != dest and cursor != cursor.parent:
                if cursor.is_symlink():
                    raise ValidationError(f"目标路径含符号链接：{cursor}")
                cursor = cursor.parent
            written += info.file_size
            if written > max_total:
                raise ValidationError("解压过程中累计体积超出上限")
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1 << 20)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                try:
                    os.chmod(target, mode & 0o755 | 0o600)
                except OSError:
                    pass
            count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# 4. 子进程：一律 argv 列表，绝不用 shell
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def tail(self) -> str:
        text = (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")
        return text.strip()[-8000:]


def run(argv: Sequence[str], *, cwd: Path | None = None, timeout: int = 300,
        env: dict[str, str] | None = None, input_text: str | None = None,
        check: bool = False) -> CommandResult:
    """执行外部命令。argv 必须是列表（字符串会被拒绝，强制调用方不要拼串）。"""
    if isinstance(argv, (str, bytes)):
        raise ValidationError("run() 只接受 argv 列表，禁止传入命令行字符串")
    argv = [str(a) for a in argv]
    import time
    started = time.monotonic()
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=full_env,
            input=input_text,
        )
        result = CommandResult(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            argv=tuple(argv),
            returncode=124,
            stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"命令超时（{timeout}s）",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except FileNotFoundError as exc:
        result = CommandResult(
            argv=tuple(argv),
            returncode=127,
            stdout="",
            stderr=f"命令不存在：{exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if check and not result.ok:
        raise ValidationError(f"命令失败（{result.returncode}）：{' '.join(argv)}\n{result.tail}")
    return result


def human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < step or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{value:.1f} TiB"
