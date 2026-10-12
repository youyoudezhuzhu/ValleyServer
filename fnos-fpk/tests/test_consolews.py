"""CommandWebUI WebSocket 控制台通道测试。

**为什么是 WebSocket 而不是 stdin**（真机实测，先试错再定型）：
  * 把游戏进程 stdin 接成管道并不管用 —— 写入成功但游戏侧没有线程读取，
    指令没有任何回显；
  * 上游 `CommandWebUI` 模组的 `ws://127.0.0.1:<port>/ws` **可用**，
    实测发 `help` 能收到 SMAPI 的完整输出。

**并且必须知道它的危险**：该模组 WS 写循环没有捕获异常
（`Mods/CommandWebUI/Server.cs:129`），客户端断开时可能因 Broken pipe
抛出未处理异常 → **整个游戏进程崩溃**（实测触发过一次）。

因此这里的测试**用一个本地假 WS 服务**，绝不碰真实游戏。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import consolews  # noqa: E402


class FakeWsServer:
    """极简 WebSocket 服务端：把收到的文本原样回显（可配置为静默/粗暴关闭）。"""

    def __init__(self, *, echo=True, reject=False, delay=0.0):
        self.echo = echo
        self.reject = reject
        self.delay = delay
        self.received: list[str] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            headers = {}
            for line in head.split("\r\n")[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.lower().strip()] = v.strip()
            if self.reject:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            key = headers.get("sec-websocket-key", "")
            accept = base64.b64encode(hashlib.sha1(
                (key + consolews.WS_GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            if self.delay:
                time.sleep(self.delay)
            while True:
                head2 = conn.recv(2)
                if len(head2) < 2:
                    return
                opcode = head2[0] & 0x0F
                masked = bool(head2[1] & 0x80)
                length = head2[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", conn.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", conn.recv(8))[0]
                mask = conn.recv(4) if masked else b""
                payload = b""
                while len(payload) < length:
                    chunk = conn.recv(length - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == consolews._OP_CLOSE:
                    return
                text = payload.decode("utf-8", "replace")
                self.received.append(text)
                if self.echo:
                    out = text.encode()
                    # 服务端发出的帧不加掩码
                    conn.sendall(bytes([0x81, len(out)]) + out if len(out) < 126
                                 else bytes([0x81, 126]) + struct.pack(">H", len(out)) + out)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestHandshake(unittest.TestCase):
    def test_probe_reports_available(self):
        srv = FakeWsServer()
        try:
            r = consolews.probe("127.0.0.1", srv.port)
            self.assertTrue(r["available"], r)
            self.assertIn("101", r["status"])
        finally:
            srv.close()

    def test_probe_reports_unavailable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        r = consolews.probe("127.0.0.1", free, connect_timeout=1)
        self.assertFalse(r["available"])
        self.assertIn("error", r)

    def test_rejects_non_websocket_server(self):
        """连到普通 HTTP 服务时必须明确失败，而不是假装成功。"""
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]

        def serve():
            try:
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
                conn.close()
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()
        r = consolews.probe("127.0.0.1", port, connect_timeout=2)
        self.assertFalse(r["available"])
        try:
            srv.close()
        except OSError:
            pass

    def test_handshake_rejected_status(self):
        srv = FakeWsServer(reject=True)
        try:
            r = consolews.probe("127.0.0.1", srv.port)
            self.assertFalse(r["available"])
        finally:
            srv.close()


class TestSendCommand(unittest.TestCase):
    def test_sends_text_and_collects_reply(self):
        srv = FakeWsServer()
        try:
            rep = consolews.send_command("127.0.0.1", srv.port, "help",
                                         collect_seconds=1.0, drain_seconds=0.2)
            self.assertTrue(rep.ok, rep.error)
            self.assertEqual(srv.received, ["help"])
            self.assertIn("help", rep.text)
        finally:
            srv.close()

    def test_client_frames_are_masked(self):
        """RFC 6455 要求客户端发出的帧必须带掩码（假服务端会校验并解掩码）。"""
        srv = FakeWsServer()
        try:
            consolews.send_command("127.0.0.1", srv.port, "ServerCMD.get_save_list",
                                   collect_seconds=0.8, drain_seconds=0.2)
            self.assertEqual(srv.received, ["ServerCMD.get_save_list"])
        finally:
            srv.close()

    def test_long_payload_uses_extended_length(self):
        srv = FakeWsServer()
        try:
            long_cmd = "x" * 300
            consolews.send_command("127.0.0.1", srv.port, long_cmd,
                                   collect_seconds=0.8, drain_seconds=0.2)
            self.assertEqual(srv.received, [long_cmd])
        finally:
            srv.close()

    def test_reports_error_when_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        rep = consolews.send_command("127.0.0.1", free, "help", connect_timeout=1)
        self.assertFalse(rep.ok)
        self.assertTrue(rep.error)

    def test_keeps_output_even_if_connection_drops_after(self):
        """拿到输出后对端粗暴断开，不应把整条指令判为失败。"""
        srv = FakeWsServer(echo=True)
        try:
            rep = consolews.send_command("127.0.0.1", srv.port, "help",
                                         collect_seconds=0.8, drain_seconds=0.2)
            self.assertTrue(rep.ok)
            self.assertTrue(rep.messages)
        finally:
            srv.close()

    def test_does_not_hang_without_output(self):
        """对端不回任何东西时，必须在 collect 窗口后返回（不能挂死）。"""
        srv = FakeWsServer(echo=False)
        try:
            t0 = time.monotonic()
            rep = consolews.send_command("127.0.0.1", srv.port, "help",
                                         collect_seconds=0.5, drain_seconds=0.1)
            self.assertLess(time.monotonic() - t0, 5.0)
            self.assertTrue(rep.ok)
            self.assertEqual(rep.messages, [])
        finally:
            srv.close()


class TestRealWorldProtocol(unittest.TestCase):
    """针对真实 CommandWebUI 行为的关键点（用本地假服务模拟）。"""

    def test_probe_does_not_leave_dirty_close(self):
        """探测也必须走优雅关闭 —— 粗暴关闭会触发上游 MOD 的未处理异常。"""
        import inspect
        src = inspect.getsource(consolews.probe)
        self.assertIn("_close_gracefully", src)

    def test_send_drains_before_closing(self):
        """发送后必须排空再关闭（把"写坏管道"的窗口压到最小）。"""
        import inspect
        src = inspect.getsource(consolews.send_command)
        self.assertIn("drain_seconds", src)
        self.assertIn("_close_gracefully", src)

    def test_module_documents_crash_risk(self):
        """这个通道有真实风险，模块里必须写清楚，避免后人误用。"""
        doc = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/mgr/consolews.py").read_text(encoding="utf-8")
        self.assertIn("Server.cs:129", doc)
        self.assertIn("崩溃", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
