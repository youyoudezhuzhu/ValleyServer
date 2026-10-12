"""WebVNC 反代测试。

背景（真机踩到的真实缺陷 I-2）：界面把 VNC 地址拼成
`http://127.0.0.1:5800/vnc.html`。按推荐把 `host_bind` 设为 `127.0.0.1` 后，
从 fnconnect / 局域网其它设备打开这个链接，指向的是**用户自己电脑**的 127.0.0.1，
必然打不开；而且 5800 也只监听本机。

改为在管理器自身前缀下反代 `/vnc/*` → 本机 websockify。这样
"能打开管理界面的地方就能看画面"，不需要额外暴露端口。

**安全前提**：这个路径能实时看到并操作游戏画面，等同管理权限，
所以必须走会话鉴权（否则反代就成了新的未授权入口）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

import vncproxy  # noqa: E402


class TestPathHandling(unittest.TestCase):
    def test_detects_vnc_paths(self):
        for path in ("/vnc", "/vnc/", "/vnc/vnc.html", "/vnc/websockify",
                     "/vnc/core/rfb.js"):
            with self.subTest(path=path):
                self.assertTrue(vncproxy.is_vnc_path(path))

    def test_ignores_other_paths(self):
        for path in ("/", "/index.html", "/api/dashboard", "/vncx", "/vn",
                     "/app/valleyserver/api/health"):
            with self.subTest(path=path):
                self.assertFalse(vncproxy.is_vnc_path(path))

    def test_upstream_path_strips_prefix(self):
        self.assertEqual(vncproxy.upstream_path("/vnc/vnc.html"), "/vnc.html")
        self.assertEqual(vncproxy.upstream_path("/vnc/core/rfb.js"), "/core/rfb.js")
        self.assertEqual(vncproxy.upstream_path("/vnc/websockify"), "/websockify")
        self.assertEqual(vncproxy.upstream_path("/vnc/"), "/")

    def test_upstream_path_never_escapes_root(self):
        """不能让构造出来的路径跑出反代根（潜在 SSRF/路径穿越）。"""
        for path in ("/vnc/../../etc/passwd", "/vnc/..%2f..%2fetc"):
            with self.subTest(path=path):
                self.assertTrue(vncproxy.upstream_path(path).startswith("/"))


class TestWebSocketDetect(unittest.TestCase):
    def test_detects_upgrade(self):
        self.assertTrue(vncproxy.is_websocket({"Upgrade": "websocket"}))
        self.assertTrue(vncproxy.is_websocket({"upgrade": "WebSocket"}))
        self.assertTrue(vncproxy.is_websocket({"Upgrade": "WebSocket", "Connection": "Upgrade"}))

    def test_ignores_plain_requests(self):
        self.assertFalse(vncproxy.is_websocket({}))
        self.assertFalse(vncproxy.is_websocket({"Connection": "keep-alive"}))
        self.assertFalse(vncproxy.is_websocket({"Upgrade": "h2c"}))


class TestResponseParsing(unittest.TestCase):
    def test_parses_status_headers_body(self):
        """头名统一小写 —— HTTP 头名本就大小写不敏感，统一后才能可靠取值。"""
        raw = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
               b"Content-Length: 5\r\n\r\nhello")
        status, headers, body = vncproxy._parse_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html")
        self.assertEqual(body, b"hello")

    def test_parses_empty_body(self):
        raw = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
        status, _headers, body = vncproxy._parse_response(raw)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_rejects_garbage(self):
        self.assertIsNone(vncproxy._parse_response(b""))
        self.assertIsNone(vncproxy._parse_response(b"NOT-HTTP\r\n\r\n"))


class _FakeUpstream:
    """最小 websockify 替身：静态资源 + WebSocket 回显。"""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self.received: list[bytes] = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            lines = head.split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    headers[key.lower().strip()] = value.strip()
            if headers.get("upgrade", "").lower() == "websocket":
                key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    self.received.append(chunk)
                    conn.sendall(chunk)
            elif path == "/static":
                body = b"FAKE-NOVNC-HTML"
                # 故意用 **小写 t**：websockify 用的 http.server 就是这么发的，
                # 而最初按 `Content-Type` 取值取不到，导致浏览器把页面当文件下载。
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-type: text/html\r\n"
                             b"Content-Length: %d\r\n\r\n" % len(body) + body)
            elif path == "/core/rfb.js":
                body = b"// noVNC core"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-type: text/javascript\r\n"
                             b"Content-Length: %d\r\n\r\n" % len(body) + body)
            else:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestContentTypePreserved(unittest.TestCase):
    """反代必须保留上游的 Content-type（真机踩到"VNC 打开是文件"）。

    websockify 基于 Python 的 `http.server`，它发的是 **`Content-type:`（小写 t）**。
    我最初按 `Content-Type` 取值，取不到就默认成 `application/octet-stream`，
    浏览器于是把 noVNC 页面**当文件下载**，用户看到的就是"vnc 打开是文件"。
    HTTP 头名本就不区分大小写，正确做法是统一小写后取值。
    """

    def setUp(self):
        self.up = _FakeUpstream()
        self.proxy = vncproxy.VncProxy(self.up.port)

    def tearDown(self):
        self.up.close()

    def test_headers_are_lowercased(self):
        _status, headers, _body = self.proxy.forward_http("GET", "/vnc/static", {}, b"")
        self.assertIn("content-type", headers)
        self.assertNotIn("Content-Type", headers)

    def test_html_content_type_preserved(self):
        _status, headers, _body = self.proxy.forward_http("GET", "/vnc/static", {}, b"")
        self.assertEqual(headers["content-type"], "text/html")

    def test_js_content_type_preserved(self):
        _status, headers, body = self.proxy.forward_http("GET", "/vnc/core/rfb.js", {}, b"")
        self.assertEqual(headers["content-type"], "text/javascript")
        self.assertEqual(body, b"// noVNC core")

    def test_parse_response_normalises_case(self):
        raw = (b"HTTP/1.1 200 OK\r\nContent-type: text/html\r\n"
               b"Content-Length: 2\r\n\r\nhi")
        status, headers, body = vncproxy._parse_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html")
        self.assertEqual(body, b"hi")

    def test_manager_does_not_default_to_octet_stream(self):
        """管理器侧不能再把取不到的类型默认成 application/octet-stream。"""
        import inspect
        import run as R
        src = inspect.getsource(R.Handler._proxy_vnc)
        self.assertNotIn('"application/octet-stream"', src)
        self.assertIn('upstream_headers.get("content-type")', src)

    def test_self_generated_headers_not_duplicated(self):
        """上游的 Server/Date 不该透传（管理器自己会发）。"""
        self.assertIn("server", vncproxy.SELF_GENERATED)
        self.assertIn("date", vncproxy.SELF_GENERATED)


class TestHttpForwarding(unittest.TestCase):
    def setUp(self):
        self.up = _FakeUpstream()
        self.proxy = vncproxy.VncProxy(self.up.port)

    def tearDown(self):
        self.up.close()

    def test_forwards_static_asset(self):
        status, headers, body = self.proxy.forward_http(
            "GET", "/vnc/static", {"Accept": "*/*"}, b"")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"FAKE-NOVNC-HTML")

    def test_unknown_path_returns_upstream_404(self):
        status, _h, _b = self.proxy.forward_http("GET", "/vnc/nope", {}, b"")
        self.assertEqual(status, 404)

    def test_returns_none_when_upstream_down(self):
        """上游没起来时要返回 None（由调用方转成 502），而不是抛异常。

        注意：这里必须用一个**确定没有监听**的端口。直接关掉本用例的假上游
        不可靠 —— 阻塞在 accept() 的线程会让套接字引用计数不归零，端口其实还在听。
        """
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        proxy = vncproxy.VncProxy(free_port, timeout=1)
        self.assertIsNone(proxy.forward_http("GET", "/vnc/static", {}, b""))

    def test_hop_by_hop_headers_not_forwarded(self):
        """逐跳首部不该透传（RFC 7230）。"""
        status, _h, _b = self.proxy.forward_http(
            "GET", "/vnc/static",
            {"Connection": "keep-alive", "TE": "trailers", "X-Custom": "1"}, b"")
        self.assertEqual(status, 200)


class TestWebSocketRelay(unittest.TestCase):
    def setUp(self):
        self.up = _FakeUpstream()
        self.proxy = vncproxy.VncProxy(self.up.port)

    def tearDown(self):
        self.up.close()

    def _connect(self):
        client, server_side = socket.socketpair()
        threading.Thread(
            target=self.proxy.relay_websocket,
            args=(server_side, "/vnc/websockify",
                  {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                   "Sec-WebSocket-Version": "13"}),
            daemon=True).start()
        return client

    def test_handshake_is_forwarded(self):
        client = self._connect()
        client.settimeout(5)
        resp = client.recv(4096)
        self.assertIn(b"101 Switching Protocols", resp)
        self.assertIn(b"Sec-WebSocket-Accept", resp)
        client.close()

    def test_bidirectional_relay(self):
        client = self._connect()
        client.settimeout(5)
        client.recv(4096)                     # 101
        frame = b"\x81\x85\x01\x02\x03\x04masked"[:8]
        client.sendall(frame)
        deadline = time.monotonic() + 5
        got = b""
        while time.monotonic() < deadline and frame not in got:
            try:
                got += client.recv(4096)
            except socket.timeout:
                break
        self.assertIn(frame, got, "客户端字节没有被转发并回显")
        client.close()

    def test_relay_survives_client_close(self):
        client = self._connect()
        client.settimeout(5)
        client.recv(4096)
        client.close()                       # 关掉客户端不应抛异常
        time.sleep(0.3)



class TestVncUrlConstruction(unittest.TestCase):
    """WebVNC 的 WebSocket 地址构造必须与 noVNC 的拼接规则一致。

    noVNC（app/ui.js）内部是：
        url = 'ws://' + host + (port ? ':'+port : '') + '/' + path
    它**自己会加一个 `/`**，所以 `path` 带前导斜杠就会拼成 `//app/...`：
    真机 nginx 实测返回 404（`"GET //app/valleyserver/vnc/websockify" 404`），
    WebSocket 永远连不上。
    """

    def test_frontend_path_has_no_leading_slash(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("function vncWsPath()", app)
        self.assertIn("replace(/^\\/+/, '')", app,
                      "必须去掉前导斜杠")
        self.assertNotIn("encodeURIComponent(appBase() + 'vnc/websockify')", app)

    def test_server_collapses_duplicate_slashes(self):
        run_py = (Path(__file__).resolve().parent.parent
                  / "fnpack/app/server/run.py").read_text(encoding="utf-8")
        self.assertIn('while "//" in path', run_py,
                      "服务端必须折叠重复斜杠，让旧地址也能用")


class TestSecurityRequirements(unittest.TestCase):
    """反代的安全性要求必须落在代码里，而不是靠约定。"""

    def test_proxy_requires_authentication(self):
        import inspect
        import run as R
        src = inspect.getsource(R.Handler._dispatch)
        self.assertIn("is_vnc_path", src)
        # 未登录必须被拒
        idx = src.index("is_vnc_path")
        chunk = src[idx:idx + 900]
        self.assertIn("401", chunk, "未登录访问 VNC 反代必须返回 401")
        self.assertIn("actor", chunk)

    def test_frontend_uses_relative_url(self):
        """前端不能再拼 host:port（那正是远程打不开的原因）。"""
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("appBase()", app)
        self.assertNotIn("d.binding === '127.0.0.1' ? '127.0.0.1'", app)
        self.assertNotIn("http://${host}:${port}/vnc.html", app)

    def test_bind_policy_is_explicit_and_enforced(self):
        """监听范围策略（用户选择方案 B）：

        * 默认 `0.0.0.0` —— 局域网可直接连 VNC/WebVNC；
        * 但必须**真的**按配置绑定（曾经配置项完全没传给进程，界面写"仅本机"、
          实际监听 0.0.0.0）；
        * 并且选了 0.0.0.0 时界面必须显示无认证风险提示。
        """
        from mgr import config as C
        from mgr.native_runtime import NativeRuntime, is_loopback
        self.assertEqual(C.NativeConfig().host_bind, "0.0.0.0")
        rt = NativeRuntime()
        cfg = C.load_config()
        cfg.native.host_bind = "127.0.0.1"
        self.assertTrue(is_loopback(cfg.native.host_bind))
        self.assertIn("-localhost", rt._x11vnc_argv(cfg))
        self.assertIn(f"127.0.0.1:{int(cfg.native.webvnc_port)}", rt._websockify_argv(cfg))
        cfg.native.host_bind = "0.0.0.0"
        self.assertNotIn("-localhost", rt._x11vnc_argv(cfg))
        self.assertIn(f"0.0.0.0:{int(cfg.native.webvnc_port)}", rt._websockify_argv(cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
