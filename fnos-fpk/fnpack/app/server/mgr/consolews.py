"""通过 CommandWebUI 的 WebSocket 向 SMAPI 控制台发送指令并取回输出。

**为什么是这条路**（真机实测，先试错再定型）：

1. 最初的做法是把游戏进程的 stdin 接成管道，直接往里写指令。
   实测**不通**：`/proc/<pid>/fd/0` 确实是 `pipe:[…]`，写入也成功，
   但游戏侧没有任何线程在读它（线程 wchan 里没有管道读），
   `help` 发出去后控制台**没有任何回显** → 指令等于丢进了黑洞。
2. 上游自带的 `CommandWebUI` 模组正是为此设计的：它把 SMAPI 控制台
   通过 WebSocket 暴露出来，**指令就是纯文本消息**。
   实测：连 `ws://127.0.0.1:29103/ws` 发 `help`，能收到 SMAPI 的完整帮助输出。

因此本模块实现一个**极简 WebSocket 客户端**（纯标准库，本项目管理器不引入第三方依赖）：
握手 → 发掩码文本帧 → 收帧（处理 126/127 长度、ping/pong、close）→ 关闭。

安全提醒（务必让用户知情）：
`CommandWebUI` 默认监听 `0.0.0.0:<Port>`，而且**其 `/ws` 端点不校验 AccessToken**
（该模组 README 自己写明"这种加密方式对 websocketIO 无效"）。
也就是说：同一局域网内任何人连上 `/ws` 就能执行 SMAPI 指令 ——
SMAPI 指令能改存档，个别模组甚至能运行代码。
本管理器只连 `127.0.0.1`；暴露面来自那个模组本身，需要在防火墙上限制该端口。
"""

from __future__ import annotations

import base64
import os
import select
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PATH = "/ws"
_OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


@dataclass
class ConsoleReply:
    ok: bool = False
    messages: list[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(m.strip() for m in self.messages if m and m.strip())


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket, timeout: float) -> tuple[int, bytes] | None:
    """读一个 WebSocket 帧。返回 (opcode, payload)；超时返回 None。"""
    if not select.select([sock], [], [], timeout)[0]:
        return None
    head = _recv_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _close_gracefully(sock: socket.socket, timeout: float = 1.5) -> None:
    """发关闭帧并读掉对方的关闭帧 —— **不做**这一步会让对端抛
    `WebSocketException: The remote party closed the WebSocket connection
    without completing the close handshake.`，并污染后续连接（实测踩到）。"""
    try:
        sock.sendall(bytes([0x80 | _OP_CLOSE, 0x80]) + b"\x00\x00\x00\x00")
    except OSError:
        return
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            frame = _read_frame(sock, max(0.05, end - time.monotonic()))
        except (OSError, ConnectionError, struct.error):
            return
        if frame is None or frame[0] == _OP_CLOSE:
            return


def _build_text_frame(text: str) -> bytes:
    """客户端发出的帧**必须掩码**（RFC 6455 §5.3）。"""
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytes([0x80 | _OP_TEXT])
    n = len(data)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def handshake(sock: socket.socket, host: str, port: int, path: str) -> str:
    """完成 WebSocket 握手；返回服务端返回的首行（失败时抛异常）。"""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\n"
           f"Host: {host}:{port}\r\n"
           f"Upgrade: websocket\r\n"
           f"Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手期间连接被关闭")
        buf += chunk
        if len(buf) > 65536:
            raise ConnectionError("握手响应异常（过大）")
    head, _, _rest = buf.partition(b"\r\n\r\n")
    text = head.decode("latin-1", "replace")
    status_line = text.split("\r\n", 1)[0]
    if " 101 " not in status_line + " ":
        raise ConnectionError(f"WebSocket 握手失败：{status_line[:120]}")
    expect = base64.b64encode(
        __import__("hashlib").sha1((key + WS_GUID).encode()).digest()).decode()
    if expect.lower() not in text.lower():
        # 严格校验可防"连到了别的 HTTP 服务"这种情况
        raise ConnectionError("WebSocket 握手响应缺少匹配的 Sec-WebSocket-Accept")
    return status_line


def send_command(host: str, port: int, command: str, *,
                 path: str = DEFAULT_PATH, collect_seconds: float = 2.5,
                 drain_seconds: float = 2.0,
                 connect_timeout: float = 5.0,
                 max_messages: int = 200) -> ConsoleReply:
    """连接 CommandWebUI，发送一条指令并收集输出。

    `collect_seconds` 是等待输出的时长：SMAPI 的指令输出是**异步文本流**，
    没有"响应结束"标记，所以用一段短窗口收集。
    """
    reply = ConsoleReply()
    started = time.monotonic()
    sock = None
    handshake_done = False
    try:
        sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
        sock.settimeout(connect_timeout)
        handshake(sock, host, int(port), path)
        handshake_done = True
        sock.sendall(_build_text_frame(command))
        deadline = time.monotonic() + max(0.1, collect_seconds)
        while len(reply.messages) < max_messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            frame = _read_frame(sock, remaining)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == _OP_CLOSE:
                break
            if opcode == _OP_PING:
                # 回 pong，保持连接健康
                sock.sendall(bytes([0x80 | _OP_PONG, 0x80]) + b"\x00\x00\x00\x00")
                continue
            if opcode in (_OP_TEXT, _OP_BINARY):
                reply.messages.append(payload.decode("utf-8", "replace"))
        reply.ok = True
    except (OSError, ConnectionError, ValueError, struct.error) as exc:
        # 已经握手成功且拿到了输出时，之后的读错误（例如对端 reset）
        # 不应让整条指令判为失败 —— 指令其实已经送到了（实测踩到）。
        if handshake_done and reply.messages:
            reply.ok = True
            reply.error = f"（读取输出时中断，指令已送达）{type(exc).__name__}: {exc}"
        else:
            reply.error = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            # ⚠️ 关键安全细节：CommandWebUI 的 WS 写循环**没有捕获异常**
            # （Mods/CommandWebUI/Server.cs:129）。只要它在**我们已关闭连接之后**
            # 还要写一次，Bottle 管道破裂的异常就会在 .NET 线程池线程上变成
            # `Unhandled exception` → **整个游戏进程崩溃**（实测发生过一次）。
            # 因此：发完关闭帧后**继续排空一段时间**，让对端把该写的写完，
            # 最后才关 socket —— 把"写坏管道"的窗口压到最小。
            try:
                _close_gracefully(sock, timeout=max(0.5, drain_seconds))
                end = time.monotonic() + max(0.0, drain_seconds)
                while time.monotonic() < end:
                    try:
                        frame = _read_frame(sock, max(0.05, end - time.monotonic()))
                    except (OSError, ConnectionError, struct.error):
                        break
                    if frame is None or frame[0] == _OP_CLOSE:
                        break
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
    reply.elapsed = round(time.monotonic() - started, 2)
    return reply


def probe(host: str, port: int, *, connect_timeout: float = 3.0) -> dict[str, Any]:
    """探测 CommandWebUI 是否可用（供界面提示与诊断使用）。"""
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
        sock.settimeout(connect_timeout)
        line = handshake(sock, host, int(port), DEFAULT_PATH)
        _close_gracefully(sock)
        return {"available": True, "status": line}
    except (OSError, ConnectionError, ValueError) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
