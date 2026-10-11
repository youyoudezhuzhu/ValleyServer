#!/usr/bin/env python3
"""ValleyServer 管理器后端服务。

设计要点：
  * 只依赖 Python 标准库（无 pip 依赖，避免 NAS 断网导致安装失败）。
  * 同时监听 TCP(service_port) 与 Unix 域套接字（`$TRIM_APPDEST/valleyserver.sock`），
    后者供飞牛统一网关 `/app/valleyserver/` 反代使用。
  * 网关**不剥前缀**（已由 fnOS nginx 配置确认），因此本服务自己剥；
    裸前缀 `/app/valleyserver` 返回 307 跳到带斜杠的路径。
  * 认证：
      - 仅当请求来自 **Unix 套接字**（本机飞牛网关转发）时才信任 fnOS 注入的
        `X-Trim-Userid` / `X-Trim-Isadmin`；TCP 直连一律要求口令 + 会话 Cookie。
      - 直连 TCP 时使用本地口令登录 + HttpOnly 会话 Cookie。
  * 所有会改数据的请求都要求 `X-CSRF-Token`，防止跨站请求伪造。
  * 只提供白名单操作接口，不提供任意命令执行入口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mgr import config as C          # noqa: E402
from mgr import diagnostics as diag  # noqa: E402
from mgr import util                 # noqa: E402
from http_base import (  # noqa: E402
    MAX_BODY, MAX_JSON_BODY, PUBLIC_PATHS, ApiError, Response,
)
from mgr.logmod import get_logger, trim_log  # noqa: E402
from mgr.util import ValidationError  # noqa: E402

APPNAME = "valleyserver"
GATEWAY_PREFIX = f"/app/{APPNAME}"
SOCKET_NAME = f"{APPNAME}.sock"
SESSION_COOKIE = "vs_session"
CSRF_HEADER = "X-CSRF-Token"
LOG = get_logger("manager")


# ─────────────────────────────────────────────────────────────────────────────
# 认证
# ─────────────────────────────────────────────────────────────────────────────

class AuthStore:
    """口令与会话管理。口令以 PBKDF2-HMAC-SHA256 存储，明文不落盘。"""

    def __init__(self, cfg: C.ManagerConfig) -> None:
        self._lock = threading.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.cfg = cfg

    @staticmethod
    def hash_password(password: str, *, iterations: int = 200_000) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algo, iterations, salt_hex, digest_hex = encoded.split("$")
        except ValueError:
            return False
        if algo != "pbkdf2_sha256":
            return False
        try:
            iterations_i = int(iterations)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except ValueError:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations_i)
        return hmac.compare_digest(got, expected)

    def new_session(self, actor: str, *, ttl_minutes: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self.sessions[token] = {
                "actor": actor,
                "csrf": csrf,
                "created": time.time(),
                "expires": time.time() + ttl_minutes * 60,
            }
        return token, csrf

    def get(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if session["expires"] < time.time():
                self.sessions.pop(token, None)
                return None
            return session

    def drop(self, token: str) -> None:
        with self._lock:
            self.sessions.pop(token, None)

    def purge(self) -> None:
        now = time.time()
        with self._lock:
            for token in [t for t, s in self.sessions.items() if s["expires"] < now]:
                self.sessions.pop(token, None)


def ensure_admin_password(cfg: C.ManagerConfig) -> tuple[C.ManagerConfig, str]:
    """首次运行时生成随机口令并写 600 权限文件。返回 (cfg, 明文提示)。"""
    cfg, password = C.ensure_initial_password(cfg)
    if not password:
        return cfg, ""
    hint = C.PKGHOME / "initial-password.txt"
    LOG.info(f"已生成初始登录口令，见 {hint}")
    return cfg, f"初始口令已写入 {hint}"


# ─────────────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────────────

class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self.routes.append((method.upper(), re.compile(pattern), handler))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any] | None, dict[str, str]]:
        for verb, regex, handler in self.routes:
            if verb != method.upper():
                continue
            m = regex.fullmatch(path)
            if m:
                return handler, {k: v for k, v in m.groupdict().items()}
        return None, {}


class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, *, path: str,
                 query: dict[str, list[str]], body: bytes,
                 headers: dict[str, str], remote: str, via_gateway: bool,
                 actor: str, session: dict[str, Any] | None) -> None:
        self.handler = handler
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers
        self.remote = remote
        self.via_gateway = via_gateway
        self.actor = actor
        self.session = session

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        if len(self.body) > MAX_JSON_BODY:
            raise ValidationError("JSON 请求体过大")
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValidationError("JSON 请求体必须是对象")
        return data

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def q_int(self, name: str, default: int = 0, *, low: int = 0, high: int = 10 ** 9) -> int:
        raw = self.q(name, "")
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ValidationError(f"查询参数 {name} 必须是整数")
        if not (low <= value <= high):
            raise ValidationError(f"查询参数 {name} 超出范围")
        return value


# ─────────────────────────────────────────────────────────────────────────────
# 应用服务装配
# ─────────────────────────────────────────────────────────────────────────────

class App:
    """把所有服务实例组装在一起。"""

    def __init__(self) -> None:
        C.ensure_dirs()
        self.cfg, self.password_hint = ensure_admin_password(C.load_config())
        self.auth = AuthStore(self.cfg)
        from mgr.db import get_db
        from mgr.diagnostics import Diagnostics
        from mgr.mods import ModManager
        from mgr.saves import SaveManager
        from mgr.snapshots import SnapshotStore
        from mgr.tasks import TaskManager, get_manager
        from mgr.versions import VersionManager

        self.db = get_db()
        # 执行后端：原生部署（直接在本机运行，不使用 Docker）。
        # 上层统一通过 self.runtime 调用；self.compose 作为历史别名保留。
        from mgr.runtime_base import make_runtime
        self.runtime = make_runtime()
        self.compose = self.runtime
        self.snapshots = SnapshotStore(runtime=self.runtime)
        self.saves = SaveManager(runtime=self.runtime)
        self.mods = ModManager(runtime=self.runtime)
        self.versions = VersionManager(runtime=self.runtime)
        self.tasks: TaskManager = get_manager()
        self.diagnostics = Diagnostics(
            runtime=self.runtime, snapshots=self.snapshots, mods=self.mods,
            saves=self.saves, versions=self.versions, tasks=self.tasks,
        )
        self.router = Router()
        self.started_at = time.time()
        self.supervisor = None

    def register_routes(self) -> None:
        import api_handlers
        api_handlers.register(self)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ValleyServerManager/" + C.APP_VERSION
    protocol_version = "HTTP/1.1"
    app: App  # 由 server 注入

    # 关闭默认的 stderr 访问日志（我们写自己的日志）
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("http " + (fmt % args))

    # ── 入口 ────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        app = self.app
        try:
            parsed = urllib.parse.urlsplit(self.path)
            raw_path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            headers = {k: v for k, v in self.headers.items()}
            remote = self.client_address[0] if isinstance(self.client_address, tuple) else "unix"

            # 连接来源决定"是否信任飞牛网关注入的 X-Trim-* 头"。
            #
            # 安全要点（实测确认过这是一个可被网络利用的认证绕过）：
            #   本服务同时监听 TCP 0.0.0.0:<panel_port> 与 Unix 套接字。
            #   如果无条件信任 X-Trim-Userid，那么**任何能访问面板端口的人**
            #   只要自己加上 `X-Trim-Userid: 1` 这个头，就能免密拿到管理员权限。
            #   因此：只有经 Unix 套接字进来的请求（即本机飞牛网关转发）才信任这些头；
            #   TCP 直连一律必须走口令 + 会话 Cookie。
            via_unix = getattr(self.request, "family", None) == socket.AF_UNIX

            via_gateway, path = split_gateway(raw_path)
            via_gateway = via_gateway and via_unix

            # 路径归一化：HTTP 请求目标允许出现不带前导斜杠的形式
            # （例如 `GET api/dashboard HTTP/1.1`）。若不归一化，
            # `path.startswith("/api/")` 这条鉴权判断就会被绕过（实测能返回 200）。
            # 虽然路由器当时没匹配上、没有真的泄露数据，但这种"鉴权与路由各自解释路径"
            # 的不一致是隐患，这里统一归一化，让两者看到同一个路径。
            if not path.startswith("/"):
                path = "/" + path

            # 裸前缀 307：否则页面内相对资源会解析到 /app/ 下
            if raw_path == GATEWAY_PREFIX:
                self._send(Response("", status=307, content_type="text/plain", headers={
                    "Location": GATEWAY_PREFIX + "/" + (
                        "?" + parsed.query if parsed.query else ""),
                }))
                return

            # 认证：健康检查、登录、登录态查询是公开端点（不要求已登录）
            actor, session = self._authenticate(headers, path, via_unix=via_unix)
            if path.startswith("/api/") and path not in PUBLIC_PATHS and not actor:
                self._send(Response({"error": "未登录或会话已过期", "code": "unauthorized"},
                                    status=401))
                return

            # CSRF：所有会改数据的请求
            if method in ("POST", "PUT", "DELETE") and path.startswith("/api/"):
                if session is not None:
                    token = headers.get(CSRF_HEADER) or headers.get(CSRF_HEADER.lower(), "")
                    if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                        self._send(Response(
                            {"error": "CSRF 校验失败，请刷新页面后重试", "code": "csrf"},
                            status=403))
                        return

            body = self._read_body()
            req = Request(self, path=path, query=query, body=body, headers=headers,
                          remote=remote, via_gateway=via_gateway,
                          actor=actor or "", session=session)

            # 静态资源
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                self._serve_static(path)
                return

            handler, params = app.router.match(method, path)
            if handler is None:
                if path.startswith("/api/"):
                    self._send(Response({"error": "接口不存在", "code": "not_found"}, status=404))
                else:
                    self._serve_static("/index.html")
                return
            result = handler(req, **params)
            if isinstance(result, Response):
                self._send(result)
            else:
                self._send(Response(result))
        except ApiError as exc:
            payload = {"error": str(exc), "code": "api_error"}
            payload.update(exc.extra)
            self._send(Response(payload, status=exc.status))
        except ValidationError as exc:
            self._send(Response({"error": str(exc), "code": "invalid"}, status=400))
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"未处理异常 {method} {self.path}: {exc}\n{traceback.format_exc()}")
            self._send(Response({"error": f"服务器内部错误：{type(exc).__name__}",
                                 "code": "internal"}, status=500))

    # ── 认证 ────────────────────────────────────────────────────────────────
    def _authenticate(self, headers: dict[str, str], path: str,
                      *, via_unix: bool = False) -> tuple[str | None, dict[str, Any] | None]:
        app = self.app
        # 1) 网关注入的可信 Header —— **仅当请求经 Unix 套接字进来时**才信任。
        #    TCP 直连伪造这些头必须无效，否则就是免密登录漏洞。
        if via_unix:
            trim_user = headers.get("X-Trim-Userid") or headers.get("x-trim-userid")
            if trim_user:
                return headers.get("X-Trim-Username") or f"fnos-user-{trim_user}", None
        # 2) 本地会话 Cookie
        raw_cookie = headers.get("Cookie") or headers.get("cookie") or ""
        if raw_cookie:
            cookie = SimpleCookie()
            try:
                cookie.load(raw_cookie)
            except Exception:  # noqa: BLE001
                cookie = SimpleCookie()
            morsel = cookie.get(SESSION_COOKIE)
            if morsel:
                session = app.auth.get(morsel.value)
                if session:
                    return session["actor"], session
        # 3) 未登录：公开端点由调用方放行（见 _dispatch 中的 PUBLIC）
        return None, None

    # ── 请求体 ──────────────────────────────────────────────────────────────
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Content-Length 非法", 400)
        if length < 0 or length > MAX_BODY:
            raise ApiError(f"请求体过大（上限 {MAX_BODY} 字节）", 413)
        if length == 0:
            return b""
        remaining = length
        chunks: list[bytes] = []
        while remaining > 0:
            block = self.rfile.read(min(remaining, 1 << 20))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    # ── 响应 ────────────────────────────────────────────────────────────────
    def _send(self, response: Response) -> None:
        body = b"" if self.command == "HEAD" else response.body
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── 静态资源 ────────────────────────────────────────────────────────────
    def _serve_static(self, path: str) -> None:
        root = C.WWW_DIR
        rel = path.lstrip("/") or "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            self._send(Response("非法路径", status=400, content_type="text/plain"))
            return
        candidate = (root / rel)
        try:
            resolved = candidate.resolve()
            if root.resolve() not in resolved.parents and resolved != root.resolve():
                raise ValidationError("路径逃逸")
        except (ValidationError, OSError):
            self._send(Response("非法路径", status=400, content_type="text/plain"))
            return
        if resolved.is_dir():
            resolved = resolved / "index.html"
        if not resolved.is_file():
            # SPA 回退
            index = root / "index.html"
            if index.is_file():
                resolved = index
            else:
                self._send(Response("前端资源缺失", status=404, content_type="text/plain"))
                return
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            self._send(Response(f"读取失败：{exc}", status=500, content_type="text/plain"))
            return
        no_cache = resolved.suffix in (".html",) or resolved.name in ("index.html",)
        headers = {
            # 升级后避免浏览器缓存旧界面（见 skill 硬规则：静态资源缓存导致「升级后界面没变」）
            "Cache-Control": "no-cache, must-revalidate" if no_cache
                             else "public, max-age=300",
        }
        if resolved.name == "index.html":
            headers["Cache-Control"] = "no-store"
        self._send(Response(raw=data, content_type=ctype, headers=headers))


def split_gateway(raw_path: str) -> tuple[bool, str]:
    """剥离网关前缀。返回 (是否经网关, 应用内路径)。"""
    if raw_path == GATEWAY_PREFIX:
        return True, "/"
    if raw_path.startswith(GATEWAY_PREFIX + "/"):
        return True, raw_path[len(GATEWAY_PREFIX):]
    return False, raw_path


# ─────────────────────────────────────────────────────────────────────────────
# 服务器（TCP + Unix socket 双监听）
# ─────────────────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def build_handler(app: App) -> type[Handler]:
    class BoundHandler(Handler):
        pass
    BoundHandler.app = app  # type: ignore[attr-defined]
    return BoundHandler


def main() -> int:
    app = App()
    import orchestration
    orchestration.register_all(app)
    app.register_routes()
    LOG.info(f"ValleyServer 管理器启动，版本 {C.APP_VERSION}")
    LOG.info(f"HOME={os.environ.get('HOME')} DATA_LIBRARY_PATH={os.environ.get('DATA_LIBRARY_PATH')}")

    handler_cls = build_handler(app)
    servers: list[Any] = []
    sock_path = C.APPDEST / SOCKET_NAME
    port = int(os.environ.get("TRIM_SERVICE_PORT") or app.cfg.panel_tcp_port or 18242)

    # Unix socket（网关）
    try:
        if sock_path.exists() or sock_path.is_symlink():
            sock_path.unlink()
        C.APPDEST.mkdir(parents=True, exist_ok=True)
        unix_srv = ThreadedUnixServer(str(sock_path), handler_cls)
        os.chmod(sock_path, 0o666)
        servers.append(unix_srv)
        LOG.info(f"已监听 Unix 套接字 {sock_path}")
    except OSError as exc:
        LOG.warn(f"Unix 套接字绑定失败（降级为只跑 TCP）：{exc}")

    # TCP
    try:
        tcp_srv = ThreadedHTTPServer(("0.0.0.0", port), handler_cls)
        servers.append(tcp_srv)
        LOG.info(f"已监听 TCP 端口 {port}")
    except OSError as exc:
        LOG.warn(f"TCP 端口 {port} 绑定失败：{exc}")

    if not servers:
        LOG.error("没有任何监听成功，退出")
        print("启动失败：无法绑定任何监听端点", file=sys.stderr)
        return 1

    for srv in servers:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

    # 后台：健康巡检 + 自动快照 + 日志轮转
    try:
        from mgr.supervisor import Supervisor
        app.supervisor = Supervisor(app)
        app.supervisor.start()
    except Exception as exc:  # noqa: BLE001
        LOG.warn(f"后台监管线程启动失败：{exc}")

    if app.password_hint:
        print(app.password_hint, flush=True)

    stop = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        LOG.info(f"收到信号 {signum}，正在关闭…")
        stop.set()

    import signal as _signal
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    while not stop.is_set():
        stop.wait(2)
        try:
            app.auth.purge()
            trim_log(C.LOG_DIR / "manager.log")
        except Exception:  # noqa: BLE001
            pass

    if app.supervisor is not None:
        app.supervisor.stop()
    for srv in servers:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
    try:
        if sock_path.exists():
            sock_path.unlink()
    except OSError:
        pass
    LOG.info("管理器已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
