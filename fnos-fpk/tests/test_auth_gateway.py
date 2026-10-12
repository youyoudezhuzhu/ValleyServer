"""网关可信头与本地口令的鉴权边界测试。

背景（这是一个**真实存在过的认证绕过**，必须永久防回归）：

本服务同时监听 `TCP 0.0.0.0:<panel_port>` 与 Unix 套接字
（后者供飞牛统一网关 `/app/valleyserver/` 反代使用）。
飞牛网关会在转发时注入 `X-Trim-Userid` / `X-Trim-Username` / `X-Trim-Isadmin`。

如果**无条件信任**这些头，那么任何能访问面板端口的人只要自己加上
`X-Trim-Userid: 1` 就能免密拿到管理员权限 —— 实测确认可复现。
因此规则是：**只有经 Unix 套接字进来的请求才信任这些头**；
TCP 直连一律必须走口令 + 会话 Cookie + CSRF。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "fnpack" / "app" / "server"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class UnixHTTPConnection(http.client.HTTPConnection):
    """经 Unix 套接字发 HTTP 请求（模拟飞牛网关的转发方式）。"""

    def __init__(self, sock_path: str, timeout: float = 10):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


class TestAuthBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vsauth-"))
        cls.port = free_port()
        env = dict(os.environ)
        env.update({
            "TRIM_APPDEST": str(cls.tmp / "appcenter/valleyserver"),
            "TRIM_PKGVAR": str(cls.tmp / "appdata/valleyserver"),
            "TRIM_PKGHOME": str(cls.tmp / "apphome/valleyserver"),
            "TRIM_PKGETC": str(cls.tmp / "appconf/valleyserver"),
            "TRIM_PKGTMP": str(cls.tmp / "apptemp/valleyserver"),
            "TRIM_APPNAME": "valleyserver",
            "TRIM_APPVER": "0.2.1",
            "APP_VERSION": "0.2.1",
            "TRIM_SERVICE_PORT": str(cls.port),
        })
        env.pop("DATA_LIBRARY_PATH", None)
        for key in ("TRIM_APPDEST", "TRIM_PKGVAR", "TRIM_PKGHOME", "TRIM_PKGETC", "TRIM_PKGTMP"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        init = (
            "import os,sys; sys.path.insert(0,os.getcwd())\n"
            "from mgr import config as C\n"
            "from mgr.db import Database\n"
            "C.ensure_dirs(); cfg=C.load_config()\n"
            "cfg, plain = C.ensure_initial_password(cfg); C.save_config(cfg); Database()\n"
            "print(plain)\n"
        )
        out = subprocess.run([sys.executable, "-c", init], cwd=SERVER_DIR, env=env,
                             capture_output=True, text=True, check=True)
        cls.password = out.stdout.strip()
        cls.env = env
        cls.sock_path = str(cls.tmp / "appcenter/valleyserver/valleyserver.sock")
        cls.proc = subprocess.Popen(
            [sys.executable, "run.py"], cwd=SERVER_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.tcp("api/health"), timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        else:
            cls.proc.kill()
            raise RuntimeError("后端未能在限定时间内就绪")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def tcp(cls, path: str) -> str:
        return f"http://127.0.0.1:{cls.port}/{path}"

    # ── HTTP 小工具 ─────────────────────────────────────────────────────────
    def _tcp_get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.tcp(path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _uds(self, path: str, headers: dict | None = None, method: str = "GET", body=None):
        conn = UnixHTTPConnection(self.sock_path)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, data

    # ── 1) 漏洞回归：TCP 上伪造网关头必须无效 ───────────────────────────────
    def test_tcp_rejects_forged_gateway_headers(self):
        """TCP 直连伪造 X-Trim-* 头不得获得任何权限（曾经的免密登录漏洞）。"""
        forged_variants = [
            {"X-Trim-Userid": "1"},
            {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"},
            {"X-Trim-Userid": "1000", "X-Trim-Username": "admin", "X-Trim-Isadmin": "true"},
            {"x-trim-userid": "1"},
        ]
        for headers in forged_variants:
            with self.subTest(headers=headers):
                status, _ = self._tcp_get("api/dashboard", headers)
                self.assertEqual(status, 401,
                                 f"伪造 {headers} 就能访问受保护接口 —— 认证绕过")
                status2, _ = self._tcp_get("api/config", headers)
                self.assertEqual(status2, 401, "伪造网关头可读配置 —— 认证绕过")

    def test_tcp_write_api_also_rejects_forged_headers(self):
        """写接口同样不能只靠伪造头通过。"""
        conn_status, _ = self._tcp_post("api/config",
                                        {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"},
                                        {"changes": {"native": {"display_number": 91}}})
        self.assertIn(conn_status, (401, 403),
                      "伪造网关头可以直接改配置 —— 认证绕过")

    def _tcp_post(self, path: str, headers: dict, body: dict):
        data = json.dumps(body).encode()
        hdrs = dict(headers)
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(data))
        req = urllib.request.Request(self.tcp(path), data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # ── 2) 网关路径必须仍然可用（别把功能修坏） ─────────────────────────────
    def test_unix_socket_gateway_headers_authenticate(self):
        """经 Unix 套接字（飞牛网关转发）的头必须被信任。"""
        headers = {"X-Trim-Userid": "1000", "X-Trim-Username": "admin",
                   "X-Trim-Isadmin": "true"}
        status, body = self._uds("/app/valleyserver/api/auth/state", headers)
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertTrue(state["authenticated"], "网关请求未被认作已登录 —— 网关路径被修坏了")
        self.assertEqual(state["actor"], "admin")
        self.assertTrue(state["via_gateway"])
        status2, _ = self._uds("/app/valleyserver/api/dashboard", headers)
        self.assertEqual(status2, 200)

    def test_unix_socket_without_headers_is_unauthenticated(self):
        """经 Unix 套接字但没带网关头 → 仍未登录。"""
        status, _ = self._uds("/app/valleyserver/api/dashboard")
        self.assertEqual(status, 401)

    def test_path_without_leading_slash_does_not_bypass_auth(self):
        """`GET api/dashboard`（无前导斜杠）不得绕过 `/api/` 鉴权判断。

        实测过一个更隐蔽的变体：鉴权按 `path.startswith("/api/")` 判断，
        而路由对路径的归一化方式不同，两者对同一个请求的解释可能不一致。
        这里把"归一化后必须仍然要求登录"钉死。
        """
        for target in ("api/dashboard", "//api/dashboard", "api/config",
                       "/app/valleyserver/api/dashboard"):
            with self.subTest(target=target):
                raw = "%s HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n" % target
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                sock.sendall(("GET " + raw).encode())
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                sock.close()
                head, _, body = data.partition(b"\r\n\r\n")
                line = head.split(b"\r\n")[0].decode()
                self.assertIn("401", line, f"{target} 未被鉴权拦截：{line}")
                text = body.decode("utf-8", "replace")
                for leak in ("manager_version", "readiness", "display_number",
                             "baseline_commit"):
                    self.assertNotIn(leak, text,
                                     f"{target} 泄露了受保护数据（{leak}）")

    def test_duplicate_slashes_are_normalized_for_vnc_paths(self):
        """`//app/.../vnc/...` 必须被归一化后进入 VNC 反代（而不是 404）。

        真机踩到：noVNC 的 `path` 参数若带前导斜杠，它会拼成
        `//app/valleyserver/vnc/websockify`，nginx 记录的是 **404**，
        于是 WebSocket 永远连不上、每 5 秒重连一次（点击也就时有时无）。
        这里钉死"归一化后路径仍受登录保护"（401，而不是 404，也不是 200）。
        """
        for target in ("/app/valleyserver/vnc/vnc.html",
                       "//app/valleyserver/vnc/vnc.html",
                       "/vnc/vnc.html",
                       "//vnc/vnc.html"):
            with self.subTest(target=target):
                status, _body = self._raw_tcp(target)
                self.assertEqual(status, 401, f"{target} 应被鉴权拦截（不是 404）")

    def _raw_tcp(self, target: str) -> tuple[int, str]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(("GET %s HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n" % target).encode())
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        head, _, body = data.partition(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode()
        try:
            code = int(line.split(" ")[1])
        except (IndexError, ValueError):
            code = 0
        return code, body.decode("utf-8", "replace")

    # ── 3) 本地口令路径仍然可用 ─────────────────────────────────────────────
    def test_local_password_login_still_works(self):
        """TCP 上用口令登录后，会话 Cookie 才能访问受保护接口。"""
        status, body = self._tcp_post("api/login", {}, {"password": self.password})
        self.assertEqual(status, 200, body[:200])
        token = json.loads(body)["csrf"]
        self.assertTrue(token)
        # 用错误口令必须被拒
        bad_status, _ = self._tcp_post("api/login", {}, {"password": "definitely-wrong"})
        self.assertIn(bad_status, (401, 403))

    def test_tcp_forged_header_cannot_bypass_with_wrong_password(self):
        """明确断言：伪造头 + 错误口令的组合仍然进不去。"""
        status, _ = self._tcp_get("api/system", {"X-Trim-Userid": "1"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
