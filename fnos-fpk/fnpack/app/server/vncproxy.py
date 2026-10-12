"""把 WebVNC（noVNC + websockify）反代到管理器自己的 HTTP 前缀下。

**为什么需要它**（真机踩到）：原先界面把 VNC 地址拼成
`http://{host}:{webvnc_port}/vnc.html`，而按推荐 `host_bind=127.0.0.1` 时
`host` 恒为 `127.0.0.1` —— 从 fnconnect / 局域网其它设备打开必然是
"用户自己电脑的 127.0.0.1"，打不开；而且 5800 端口也只监听本机，远程本来就连不上。

反代之后：**能打开管理器的地方就能看画面**，无需额外暴露端口，也不受 fnconnect 限制。

设计：
* 管理器把 `/vnc/*` 转发到本机 websockify（noVNC 静态文件 + WebSocket 桥接；
  websockify 负责把 WebSocket 流量桥到 x11vnc 的裸 VNC 协议）；
* **必须鉴权**：否则这个路径就成了新的未授权入口（任何人都能看到并操作游戏画面）；
* WebSocket 升级请求做双向字节转发（浏览器会带上同一站点的会话 Cookie）。
"""

from __future__ import annotations

import select
import socket
import threading
import urllib.parse
from typing import Any

# 转发时不应透传的逐跳首部（Hop-by-hop headers, RFC 7230 §6.1）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

RELAY_CHUNK = 65536
RELAY_TIMEOUT = 300


def is_vnc_path(path: str) -> bool:
    return path == "/vnc" or path.startswith("/vnc/")


def upstream_path(path: str) -> str:
    """`/vnc/foo` → `/foo`（websockify 的服务根）。"""
    rest = path[len("/vnc"):]
    return rest or "/"


def is_websocket(headers: dict[str, str]) -> bool:
    upgrade = ""
    for key, value in headers.items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
    return upgrade == "websocket"


class VncProxy:
    """把请求转发到本机 websockify。

    `port` 由调用方传入（来自 native.webvnc_port / native.vnc_port 配置）。
    """

    def __init__(self, http_port: int, timeout: float = 30.0) -> None:
        self.http_port = int(http_port)
        self.timeout = timeout

    # ── 普通 HTTP 转发 ──────────────────────────────────────────────────────
    def forward_http(self, method: str, path: str, headers: dict[str, str],
                     body: bytes) -> tuple[int, dict[str, str], bytes] | None:
        upstream = upstream_path(path)
        head = [f"{method} {upstream} HTTP/1.1",
                f"Host: 127.0.0.1:{self.http_port}",
                "Connection: close"]
        for key, value in headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "content-length"):
                continue
            head.append(f"{key}: {value}")
        if body:
            head.append(f"Content-Length: {len(body)}")
        payload = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body

        try:
            with socket.create_connection(("127.0.0.1", self.http_port),
                                          timeout=self.timeout) as sock:
                sock.sendall(payload)
                raw = b""
                while True:
                    chunk = sock.recv(RELAY_CHUNK)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > 32 * 1024 * 1024:      # 防御：noVNC 资源不大
                        break
        except OSError:
            return None
        return _parse_response(raw)

    # ── WebSocket 双向转发 ──────────────────────────────────────────────────
    def relay_websocket(self, client_sock: socket.socket, path: str,
                        headers: dict[str, str], initial: bytes = b"") -> None:
        """把已建立的客户端连接与上游做全双工字节转发。

        这里不做协议解析：客户端发的握手请求原样转给 websockify，
        之后两边都是字节流（WebSocket 帧），直接对拷即可。
        """
        upstream = upstream_path(path)
        head = [f"GET {upstream} HTTP/1.1",
                f"Host: 127.0.0.1:{self.http_port}"]
        for key, value in headers.items():
            low = key.lower()
            if low in ("host", "content-length"):
                continue
            head.append(f"{key}: {value}")
        payload = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + initial

        try:
            upstream_sock = socket.create_connection(("127.0.0.1", self.http_port),
                                                     timeout=self.timeout)
        except OSError:
            return
        try:
            upstream_sock.sendall(payload)
            _pump(client_sock, upstream_sock)
        finally:
            for s in (client_sock, upstream_sock):
                try:
                    s.close()
                except OSError:
                    pass


def _pump(a: socket.socket, b: socket.socket) -> None:
    """在两个 socket 之间对拷，任一侧关闭即结束。"""
    sockets = [a, b]
    for s in sockets:
        s.setblocking(False)
    import time
    last = time.monotonic()
    try:
        while True:
            try:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)
            except (OSError, ValueError):
                return
            if errored:
                return
            if not readable:
                if time.monotonic() - last > RELAY_TIMEOUT:
                    return
                continue
            last = time.monotonic()
            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(RELAY_CHUNK)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        for s in sockets:
            try:
                s.setblocking(True)
            except OSError:
                pass


def _parse_response(raw: bytes) -> tuple[int, dict[str, str], bytes] | None:
    if not raw:
        return None
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        return None
    try:
        status = int(lines[0].split(b" ")[1])
    except (IndexError, ValueError):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
    # 上游用 Connection: close，所以这里不需要处理 chunked
    return status, headers, body


def start_relay_thread(client_sock: socket.socket, path: str,
                       headers: dict[str, str], initial: bytes = b"",
                       proxy: VncProxy | None = None) -> threading.Thread:
    proxy = proxy or VncProxy(_default_port())
    thread = threading.Thread(target=proxy.relay_websocket,
                              args=(client_sock, path, headers, initial),
                              daemon=True)
    thread.start()
    return thread


def _default_port() -> int:
    from . import config as C
    return int(C.load_config().native.webvnc_port)
