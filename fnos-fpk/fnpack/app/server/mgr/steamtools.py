"""Steam 扫码登录 + 获取/更新游戏本体。

设计依据（全部实测确认，见 tests/test_steamtools.py 与 docs/known-issues.md）：

1. **用 DepotDownloader 做实际下载**（官方 Steam 客户端协议的成熟开源实现）：
   - 它提供 **`-qr`**：打印登录二维码，用 Steam 手机 App 扫码即可登录，
     **不需要在 NAS 上输入 Steam 账号密码，也不需要 Steam Guard 验证码**；
   - 它提供**自包含**的 `DepotDownloader-linux-x64.zip`（约 33 MB，不需要 .NET 运行时）；
   - 许可证是 **GPL-2.0** → 因此本项目**只在运行时从官方 Release 下载**，
     **不把二进制打进 FPK**（避免再分发义务），与显示栈的处理方式一致。

2. **二维码是终端字符画，需要还原成模块矩阵**：
   DepotDownloader 用 `█` 与空格打印二维码。实测其排布为
   **每个模块宽 2 个字符、高 1 行**，且**会打印多张**（挑战码刷新时再打一张）。
   直接塞进网页 <pre> 会因字体宽高比失真而扫不出来，所以这里把它解析回
   0/1 矩阵，交给前端用 SVG 精确渲染（矢量、无字体依赖）。
   解析结果用三个定位图案 + 时序图案自校验；不合法就**不对外展示**（宁可报错也不给个假码）。

3. **登录态会被缓存**：`-remember-password` 让 DepotDownloader 保存登录密钥，
   之后「更新游戏本体」无需再次扫码（密钥存在应用家目录，权限 0700，
   且**不进入快照与诊断包**）。
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config as C
from .util import ValidationError, atomic_write_json, human_bytes, read_json, run, sha256_file

# Steam 上的《星露谷物语》
STEAM_APP_ID = "413150"
STEAM_APP_NAME = "Stardew Valley"

DEPOTDOWNLOADER_REPO = "SteamRE/DepotDownloader"
RELEASES_API = f"https://api.github.com/repos/{DEPOTDOWNLOADER_REPO}/releases/latest"
ASSET_NAME = "DepotDownloader-linux-x64.zip"
# DepotDownloader 是 GPL-2.0；只运行时下载，不随本 FPK 分发。
ASSET_LICENSE = "GPL-2.0"

ALLOWED_HOSTS = (
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
MAX_ASSET_BYTES = 200 * 1024 * 1024

# 二维码自校验用的定位图案（7x7）
_FINDER = [
    [1, 1, 1, 1, 1, 1, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 1, 1, 1, 1, 1, 1],
]
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BLOCK_CHARS = {"█", "▀", "▄"}


def tools_dir() -> Path:
    return C.PKGHOME / "steamtools"


def binary_path() -> Path:
    return tools_dir() / "DepotDownloader"


def credentials_dir() -> Path:
    """DepotDownloader 的工作/凭据根目录（即我们下发给它的 $HOME，权限 0700）。

    为什么要单独说明：DepotDownloader 用 .NET 的 **Isolated Storage** 保存登录密钥，
    实际落在 `$HOME/.local/share/IsolatedStorage/.../account.config`，
    而**不是** `$XDG_CONFIG_HOME/DepotDownloader`（我最初按后者找，导致
    明明登录成功了却报 cached_login=False，第二次又要求扫码）。
    """
    return tools_dir()


def login_key_file() -> Path | None:
    """找出 DepotDownloader 实际写入的登录密钥文件。"""
    root = credentials_dir() / ".local" / "share" / "IsolatedStorage"
    if not root.is_dir():
        return None
    for p in root.rglob("account.config"):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def qr_state_file() -> Path:
    return tools_dir() / "qr.json"


# ─────────────────────────────────────────────────────────────────────────────
# 二维码解析
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QrCode:
    matrix: list[list[int]]
    modules: int
    version: int
    text_lines: int

    def to_dict(self) -> dict[str, Any]:
        return {"matrix": self.matrix, "modules": self.modules,
                "version": self.version, "text_lines": self.text_lines}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _block_rows(lines: Iterable[str]) -> list[list[str]]:
    """把输出里连续的"二维码行"按组切出来（每组是一张二维码）。"""
    groups: list[list[str]] = []
    current: list[str] = []
    for raw in lines:
        line = _strip_ansi(raw).rstrip("\n\r")
        if line and set(line) <= (_BLOCK_CHARS | {" "}) and any(c in _BLOCK_CHARS for c in line):
            current.append(line)
        else:
            if current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups


def _group_to_matrix(group: list[str]) -> QrCode | None:
    """把一组字符行还原成模块矩阵。

    实测排布：**每个模块宽 2 字符、高 1 行**。因此模块数 = 行数 = 字符宽度/2。
    用定位图案与版本号自校验，不合法返回 None。
    """
    if not group:
        return None
    width = max(len(l) for l in group)
    rows = [l.ljust(width) for l in group]
    # 去掉四周空白（quiet zone）
    cols = [c for c in range(width) if any(r[c] in _BLOCK_CHARS for r in rows)]
    if not cols:
        return None
    left, right = cols[0], cols[-1]
    crop = [r[left:right + 1] for r in rows]
    while crop and not any(c in _BLOCK_CHARS for c in crop[0]):
        crop.pop(0)
    while crop and not any(c in _BLOCK_CHARS for c in crop[-1]):
        crop.pop()
    if not crop:
        return None

    h, w = len(crop), len(crop[0])
    if w != h * 2:
        return None                     # 2 字符/模块 → 宽必须是高的 2 倍
    n = h
    if (n - 17) % 4 or not (1 <= (n - 17) // 4 <= 40):
        return None
    matrix = [[1 if crop[r][c * 2] in _BLOCK_CHARS else 0 for c in range(n)]
              for r in range(n)]

    def finder(r0: int, c0: int) -> bool:
        return all(matrix[r0 + i][c0 + j] == _FINDER[i][j]
                   for i in range(7) for j in range(7))

    if not (finder(0, 0) and finder(0, n - 7) and finder(n - 7, 0)):
        return None
    if not all(matrix[6][c] == (1 - c % 2) for c in range(8, n - 8)):
        return None
    if not all(matrix[r][6] == (1 - r % 2) for r in range(8, n - 8)):
        return None
    return QrCode(matrix=matrix, modules=n, version=(n - 17) // 4, text_lines=len(group))


def parse_qr_codes(text: str) -> list[QrCode]:
    """从 DepotDownloader 的输出里取出所有**通过自校验**的二维码。"""
    out: list[QrCode] = []
    for group in _block_rows(text.splitlines()):
        code = _group_to_matrix(group)
        if code is not None:
            out.append(code)
    return out


def parse_qr_art(text: str) -> QrCode | None:
    """取最后一张二维码（挑战码刷新时最后一张才是当前有效的）。"""
    codes = parse_qr_codes(text)
    return codes[-1] if codes else None


# ─────────────────────────────────────────────────────────────────────────────
# DepotDownloader 供给（运行时下载，不随包分发）
# ─────────────────────────────────────────────────────────────────────────────

def is_installed() -> bool:
    return binary_path().is_file() and os.access(binary_path(), os.X_OK)


def installed_version() -> str:
    if not is_installed():
        return ""
    res = run([str(binary_path())], timeout=60)
    m = re.search(r"DepotDownloader v([\d.]+)", (res.stdout or "") + (res.stderr or ""))
    return m.group(1) if m else ""


def _download(url: str, dest: Path, *, on_log=None, timeout: int = 600) -> Path:
    log = on_log or (lambda _m: None)
    if not url.lower().startswith("https://"):
        raise ValidationError("下载地址必须是 HTTPS")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    log(f"下载 DepotDownloader（{ASSET_NAME}）…")
    res = run(["curl", "-sSL", "--fail", "--max-time", str(timeout),
               "--proto", "=https", "--max-redirs", "5",
               "--max-filesize", str(MAX_ASSET_BYTES),
               "-w", "%{url_effective}", "-o", str(dest), url],
              timeout=timeout + 60)
    if not res.ok:
        raise ValidationError("DepotDownloader 下载失败：" + (res.tail or "")[:300])
    effective = (res.stdout or "").strip()
    host = effective.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower() if "://" in effective else ""
    if host and host not in ALLOWED_HOSTS:
        dest.unlink(missing_ok=True)
        raise ValidationError(f"下载被重定向到非允许的主机：{host}")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ValidationError("DepotDownloader 下载结果为空")
    return dest


def provision(*, force: bool = False, on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """下载并解包 DepotDownloader 到应用家目录（**不**改系统状态）。"""
    log = on_log or (lambda _m: None)
    if is_installed() and not force:
        return {"ok": True, "skipped": True, "version": installed_version(),
                "path": str(binary_path())}

    res = run(["curl", "-sS", "--max-time", "40", "--proto", "=https",
               "-H", "Accept: application/vnd.github+json",
               "-H", "User-Agent", "valleyserver-manager", RELEASES_API], timeout=60)
    if not res.ok:
        raise ValidationError("获取 DepotDownloader 发布信息失败（检查 NAS 能否访问 api.github.com）："
                              + (res.tail or "")[:200])
    try:
        rel = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"发布信息不是合法 JSON：{exc}") from exc
    tag = str(rel.get("tag_name") or "")
    asset = None
    for item in (rel.get("assets") or []):
        if str(item.get("name")) == ASSET_NAME:
            asset = item
            break
    if asset is None:
        raise ValidationError(f"该发布里没有 {ASSET_NAME} 资产（tag={tag}）")
    url = str(asset.get("browser_download_url") or "")
    size = int(asset.get("size") or 0)
    log(f"DepotDownloader {tag}（{human_bytes(size)}，许可证 {ASSET_LICENSE}）")

    work = tools_dir() / ".download"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        zpath = _download(url, work / ASSET_NAME, on_log=log)
        digest = sha256_file(zpath)
        from .util import safe_extract_zip
        safe_extract_zip(zpath, work / "x")
        src = work / "x" / "DepotDownloader"
        if not src.is_file():
            for cand in (work / "x").rglob("DepotDownloader"):
                src = cand
                break
        if not src.is_file():
            raise ValidationError("压缩包里没有找到 DepotDownloader 可执行文件")
        tools_dir().mkdir(parents=True, exist_ok=True)
        os.replace(src, binary_path())
        os.chmod(binary_path(), 0o755)
        lic = work / "x" / "LICENSE"
        if lic.is_file():
            shutil.copy2(lic, tools_dir() / "LICENSE.DepotDownloader")
        # 我们用 tools_dir 作为 DepotDownloader 的 $HOME：登录密钥（Isolated Storage）
        # 就会落在应用家目录内，权限 0700，且不在数据区 → 不进快照/诊断。
        os.chmod(tools_dir(), stat.S_IRWXU)                # 0700：里面有 Steam 登录密钥
        atomic_write_json(tools_dir() / "meta.json", {
            "tag": tag, "asset": ASSET_NAME, "size": size, "sha256": digest,
            "license": ASSET_LICENSE, "repo": DEPOTDOWNLOADER_REPO,
            "downloaded_at": time.time(),
            "note": "GPL-2.0，运行时从官方 Release 下载，不随 FPK 分发。",
        }, mode=0o600)
        log(f"DepotDownloader {installed_version() or tag} 已就绪：{binary_path()}")
        return {"ok": True, "skipped": False, "version": installed_version() or tag,
                "path": str(binary_path()), "sha256": digest, "license": ASSET_LICENSE}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def uninstall() -> None:
    shutil.rmtree(tools_dir(), ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# 获取 / 更新游戏本体
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SteamRunResult:
    ok: bool = False
    exit_code: int | None = None
    downloaded_root: Path | None = None
    qr_shown: bool = False
    cancelled: bool = False
    username: str = ""
    used_cached_login: bool = False
    stdout_tail: str = ""
    error: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


def build_argv(dest: Path, *, validate: bool = True, username: str = "",
               use_qr: bool | None = None) -> list[str]:
    """构造 DepotDownloader 命令行。

    * `-os linux -osarch 64`：本机是 Linux，必须显式指定，否则可能拉到别的平台 depot；
    * 登录方式二选一：
        - 首次：`-qr` 扫码（无需账号密码 / Steam Guard）；
        - 之后：`-username <上次扫码成功的账号> -remember-password`
          （DepotDownloader 登录成功时会回显这个账号名，我们记下来复用）
          —— 这样"更新游戏本体"不必每次都扫码；
    * `-validate`：校验已下载文件（更新时的默认行为）。
    """
    user = username or saved_username()
    qr = use_qr if use_qr is not None else not user
    argv = [str(binary_path()),
            "-app", STEAM_APP_ID,
            "-os", "linux", "-osarch", "64",
            "-dir", str(dest)]
    if qr:
        argv += ["-qr", "-remember-password"]
    else:
        argv += ["-username", user, "-remember-password"]
    if validate:
        argv.append("-validate")
    return argv


def run_fetch(
    dest: Path,
    *,
    on_log: Callable[[str], None] | None = None,
    on_qr: Callable[[QrCode], None] | None = None,
    on_login: Callable[[str], None] | None = None,
    qr_timeout_seconds: int = 420,
    total_timeout_seconds: int = 7200,
    should_cancel: Callable[[], bool] | None = None,
    validate: bool = True,
) -> SteamRunResult:
    """运行 DepotDownloader：先扫码登录，随后下载到 dest。

    * 输出是**流式**读取的：二维码一出现就通过 `on_qr` 交给上层（界面立刻能显示）；
    * 二维码挑战会刷新，因此**每拿到一张就更新一次**，界面用最后一张；
    * 整体超时与取消由调用方通过 `should_cancel` 控制（任务取消用）。
    """
    import subprocess

    log = on_log or (lambda _m: None)
    if not is_installed():
        raise ValidationError("DepotDownloader 尚未安装（请先在界面点「准备 Steam 工具」）")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(credentials_dir().parent)      # 登录密钥落在应用家目录内
    env["XDG_CONFIG_HOME"] = str(credentials_dir())
    env["TERM"] = "xterm-256color"
    # 让 DepotDownloader 把二维码按 UTF-8 打印
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"

    user = saved_username()
    argv = build_argv(dest, validate=validate)
    if user and "-qr" not in argv:
        log(f"检测到已保存的登录（账号 {user}），本次无需扫码")
    else:
        log("启动 DepotDownloader（Steam 扫码登录；不会保存你的 Steam 密码）")
    log("命令：" + " ".join(argv))

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, bufsize=0, env=env,
                            cwd=str(tools_dir()))
    result = SteamRunResult()
    login_ok = False
    buffer = ""            # 供"按行输出日志"使用（会被逐行消费）
    qr_buffer = ""         # 只给二维码解析用（**不**被行日志消费，否则永远凑不齐一整张）
    collected: list[str] = []
    started = time.monotonic()
    last_qr_at = 0.0
    last_log_at = 0.0
    seen_codes = 0
    login_ok = False
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                result.cancelled = True
                result.error = "任务已取消"
                break
            now = time.monotonic()
            if now - started > total_timeout_seconds:
                proc.kill()
                result.error = f"超过总时限（{total_timeout_seconds}s）"
                break
            # 关键：必须用 select 带超时地等数据，**不能**直接阻塞 read。
            # DepotDownloader 在等用户扫码时长时间不产生输出，阻塞 read 会让
            # 「取消」与「超时」检查永远得不到执行 —— 任务会卡在 running 出不来
            # （实测踩到：点了取消、后端也记录了，但任务一直是 running）。
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                ready = []
            if not ready:
                continue                      # 回到循环顶部检查取消 / 超时
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            buffer += text
            qr_buffer += text
            collected.append(text)
            if len(collected) > 4000:
                del collected[:2000]
            # qr_buffer 增长有界：只保留尾部（一张二维码最多几十行）
            if len(qr_buffer) > 200_000:
                qr_buffer = qr_buffer[-100_000:]

            # 二维码：输出里凑齐完整的一张就解析并上报。
            # 注意这里必须用**独立的 qr_buffer**：下面的行日志循环会把 buffer 里的行
            # 逐行消费掉（块字符行被跳过），如果共用 buffer，二维码行会在凑齐前就被删掉，
            # 表现为"任务在跑但界面一直拿不到二维码"（实测踩到过）。
            if _BLOCK_CHARS & set(text):
                codes = parse_qr_codes(qr_buffer)
                if len(codes) > seen_codes:
                    code = codes[-1]
                    seen_codes = len(codes)
                    last_qr_at = now
                    result.qr_shown = True
                    log("已生成 Steam 登录二维码：请用 Steam 手机 App 扫码（"
                        f"模块 {code.modules}×{code.modules}，QR 版本 {code.version}）")
                    if on_qr:
                        try:
                            on_qr(code)
                        except Exception as exc:  # noqa: BLE001
                            log(f"上报二维码失败：{exc}")
                    qr_buffer = ""   # 已消费，等下一张（挑战码刷新）
            # 普通日志：按行输出，节流
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = _strip_ansi(line).strip()
                if not line:
                    continue
                if any(ch in line for ch in _BLOCK_CHARS):
                    continue
                who = note_login_success(line)
                if who:
                    result.used_cached_login = False
                    login_ok = True
                    log(f"✅ Steam 登录成功（账号 {who}）；已记住该账号，"
                        "之后更新游戏本体无需再扫码。开始下载游戏文件…")
                    if on_login:
                        try:
                            on_login(who)
                        except Exception as exc:  # noqa: BLE001
                            log(f"登录回调失败：{exc}")
                    continue
                if _is_progress(line):
                    if now - last_log_at > 5:
                        last_log_at = now
                        log("进度：" + line[:160])
                else:
                    log(line[:300])
            if (now - last_qr_at > qr_timeout_seconds and result.qr_shown
                    and not login_ok and proc.poll() is None):
                log(f"二维码 {qr_timeout_seconds}s 内未被确认，等待下一次刷新或超时")
                last_qr_at = now
        rc = proc.wait(timeout=30)
        result.exit_code = rc
        result.ok = (rc == 0)
        if not result.ok and not result.error:
            result.error = f"DepotDownloader 退出码 {rc}"
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass

    result.stdout_tail = "".join(collected)[-4000:]
    result.downloaded_root = locate_game_root(dest)
    result.username = saved_username()
    return result


# 判定"这个目录本身就是游戏根"的标志物
_GAME_MARKERS = ("Stardew Valley.dll", "StardewModdingAPI", "Stardew Valley.deps.json")


def _is_game_root(path: Path) -> bool:
    return path.is_dir() and any((path / m).exists() for m in _GAME_MARKERS)


def locate_game_root(dest: Path) -> Path | None:
    """在下载目录里找出真正的游戏根。

    **实测踩到的坑**：DepotDownloader 的 `-dir` 就是目标目录，它会**直接**把
    depot 里的文件（`Stardew Valley.dll`、`Content/`、`*.so`…）写进 `-dir` 的**根**，
    不会额外建一层 `Stardew Valley/`。我最初只找"名为 Stardew Valley 的子目录"
    或"恰好只有一个子目录"，结果 458 MB 下载**全部成功**之后仍被判为
    "没有找到下载出来的游戏目录"而失败 —— 白下一遍。
    """
    if _is_game_root(dest):
        return dest                                  # 最常见：文件直接在 -dir 下
    for cand in ("Stardew Valley", "stardew valley"):
        p = dest / cand
        if _is_game_root(p):
            return p
    # 兜底：向下一层找（DepotDownloader 未来若改变布局）
    subs = [p for p in dest.iterdir() if p.is_dir()] if dest.is_dir() else []
    hits = [p for p in subs if _is_game_root(p)]
    if len(hits) == 1:
        return hits[0]
    return None


def _is_progress(line: str) -> bool:
    return bool(re.search(r"\d+(\.\d+)?\s*%|Downloading|Validating|Depot", line))


# ─────────────────────────────────────────────────────────────────────────────
# 界面状态
# ─────────────────────────────────────────────────────────────────────────────

def read_qr_state() -> dict[str, Any] | None:
    data = read_json(qr_state_file(), None)
    if not isinstance(data, dict) or not data.get("matrix"):
        return None
    if time.time() - float(data.get("updated_at") or 0) > 15 * 60:
        return None              # 二维码过期就没必要继续显示
    return data


def write_qr_state(code: QrCode | None) -> None:
    if code is None:
        try:
            qr_state_file().unlink(missing_ok=True)
        except OSError:
            pass
        return
    atomic_write_json(qr_state_file(), dict(code.to_dict(),
                                            updated_at=time.time()),
                      mode=0o600)


def has_cached_login() -> bool:
    """是否已有缓存的 Steam 登录密钥（有则更新时无需再次扫码）。"""
    return login_key_file() is not None or bool(saved_username())


def saved_username() -> str:
    """上次扫码成功后 DepotDownloader 回显的账号名（用于后续免扫码登录）。"""
    info = read_json(tools_dir() / "login.json", None)
    if isinstance(info, dict):
        return str(info.get("username") or "")
    return ""


def save_username(username: str) -> None:
    if not username:
        return
    atomic_write_json(tools_dir() / "login.json",
                      {"username": username, "saved_at": time.time()}, mode=0o600)


# DepotDownloader 登录成功后会回显：Success! Next time you can login with
# -username fee11one1y -remember-password instead of -qr.
_USERNAME_RE = re.compile(r"-username\s+(\S+)\s+-remember-password")


def note_login_success(line: str) -> str:
    """从输出里识别"登录成功"，并记住账号名。返回账号名（未识别则空串）。"""
    m = _USERNAME_RE.search(line or "")
    if m:
        save_username(m.group(1))
        return m.group(1)
    return ""


def status() -> dict[str, Any]:
    return {
        "installed": is_installed(),
        "version": installed_version(),
        "binary": str(binary_path()) if is_installed() else "",
        "license": ASSET_LICENSE,
        "repo": DEPOTDOWNLOADER_REPO,
        "app_id": STEAM_APP_ID,
        "app_name": STEAM_APP_NAME,
        "cached_login": has_cached_login(),
        "saved_username": saved_username(),
        "login_key": str(login_key_file() or ""),
        "qr": read_qr_state(),
        "note": ("用 Steam 手机 App 扫码即可获取/更新游戏本体（无需在 NAS 输入账号密码）。"
                 "扫码一次后会缓存登录密钥，之后更新无需再扫。"),
        "privacy": "登录密钥保存在应用家目录（权限 0700），不进快照、不进诊断包。",
    }
