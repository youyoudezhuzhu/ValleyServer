"""Steam 扫码取游戏本体的底层测试。

全部离线（用真实抓取的 DepotDownloader 输出作夹具），不打网络。

夹具 `tests/fixtures/depotdownloader-qr.log` 是**真机实跑** `DepotDownloader -qr`
的输出，含两张 29×29 二维码（挑战码刷新时它会再打印一张）。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import steamtools as st  # noqa: E402
from mgr.util import ValidationError  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "depotdownloader-qr.log"


def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


class TestQrParsing(unittest.TestCase):
    def test_fixture_exists(self):
        self.assertTrue(FIXTURE.is_file(), "缺少抓取到的二维码夹具")

    def test_parses_both_qr_codes(self):
        """实测输出里有两张二维码（挑战码刷新），两张都应能解析出来。"""
        codes = st.parse_qr_codes(fixture_text())
        self.assertEqual(len(codes), 2)
        for code in codes:
            self.assertEqual(code.modules, 29)     # QR 版本 3
            self.assertEqual(code.version, 3)

    def test_matrix_is_square_and_binary(self):
        code = st.parse_qr_art(fixture_text())
        self.assertIsNotNone(code)
        self.assertEqual(len(code.matrix), code.modules)
        for row in code.matrix:
            self.assertEqual(len(row), code.modules)
            self.assertTrue(set(row) <= {0, 1})

    def test_finder_patterns_present(self):
        """三个定位图案必须存在 —— 这是"解析对了"的硬证据。"""
        code = st.parse_qr_art(fixture_text())
        m, n = code.matrix, code.modules
        pat = [[1] * 7, [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1],
               [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1],
               [1, 0, 0, 0, 0, 0, 1], [1] * 7]
        for r0, c0, name in ((0, 0, "左上"), (0, n - 7, "右上"), (n - 7, 0, "左下")):
            with self.subTest(finder=name):
                self.assertTrue(all(m[r0 + i][c0 + j] == pat[i][j]
                                    for i in range(7) for j in range(7)),
                                f"{name}定位图案不匹配")

    def test_picks_last_code(self):
        """刷新场景下必须用最后一张（最早那张可能已失效）。"""
        codes = st.parse_qr_codes(fixture_text())
        self.assertIsNotNone(st.parse_qr_art(fixture_text()))
        self.assertEqual(st.parse_qr_art(fixture_text()).matrix, codes[-1].matrix)

    def test_rejects_non_qr_text(self):
        for text in ("", "hello", "Connecting to Steam3... Done!",
                     "Logging in with QR code..."):
            with self.subTest(text=text[:20]):
                self.assertIsNone(st.parse_qr_art(text))

    def test_rejects_half_a_qr(self):
        lines = [l for l in fixture_text().splitlines() if any(c in "█▀▄" for c in l)]
        self.assertIsNone(st.parse_qr_art("\n".join(lines[:15])),
                          "只有半张不应被当成有效二维码")

    def test_rejects_tampered_code(self):
        """打乱定位图案后必须被自校验拒绝 —— 宁可报错也不显示一个假码。"""
        lines = [l for l in fixture_text().splitlines() if any(c in "█▀▄" for c in l)][:29]
        broken = list(lines)
        broken[3] = broken[3].replace("█", " ", 5)
        self.assertIsNone(st.parse_qr_art("\n".join(broken)))

    def test_ansi_colors_are_stripped(self):
        colored = "\x1b[47m\x1b[30m" + fixture_text() + "\x1b[0m"
        self.assertEqual(len(st.parse_qr_codes(colored)), 2)

    def test_to_dict_is_json_serialisable(self):
        code = st.parse_qr_art(fixture_text())
        blob = json.dumps(code.to_dict())
        self.assertIn("matrix", blob)
        self.assertLess(len(blob), 20000, "矩阵不该大到把接口撑爆")


class TestStreamingDetection(unittest.TestCase):
    """流式读取时也必须能识别二维码（回归）。

    真机踩到：`run_fetch` 里边读边按行输出日志，日志循环会把块字符行从缓冲区里
    消费掉，导致二维码**永远凑不齐一整张** —— 任务是 running，界面一直拿不到码。
    这里用不同的分块大小模拟真实管道读取。
    """

    def _stream(self, chunk_size: int) -> list[int]:
        art = fixture_text()
        buffer = ""
        seen = 0
        modules: list[int] = []
        for i in range(0, len(art), chunk_size):
            chunk = art[i:i + chunk_size]
            buffer += chunk
            if st._BLOCK_CHARS & set(chunk):
                codes = st.parse_qr_codes(buffer)
                if len(codes) > seen:
                    seen = len(codes)
                    modules.append(codes[-1].modules)
                    buffer = ""
        return modules

    def test_detects_qr_with_various_chunk_sizes(self):
        for size in (64, 256, 512, 1024, 4096, 65536):
            with self.subTest(chunk=size):
                got = self._stream(size)
                self.assertTrue(got, f"分块 {size} 字节时识别不到二维码")
                self.assertEqual(got[0], 29)

    def test_whole_output_in_one_chunk(self):
        self.assertEqual(self._stream(len(fixture_text())), [29])

    def test_run_fetch_keeps_separate_qr_buffer(self):
        """结构断言：二维码必须有独立累积缓冲，不能被行日志共用。"""
        import inspect
        src = inspect.getsource(st.run_fetch)
        self.assertIn("qr_buffer", src)
        self.assertNotIn("parse_qr_codes(buffer)", src,
                         "二维码解析不应再共用会被逐行消费的 buffer")


class TestQrState(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        st.tools_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def test_write_and_read_roundtrip(self):
        code = st.parse_qr_art(fixture_text())
        st.write_qr_state(code)
        got = st.read_qr_state()
        self.assertIsNotNone(got)
        self.assertEqual(got["modules"], code.modules)
        self.assertEqual(got["matrix"], code.matrix)

    def test_clear(self):
        st.write_qr_state(st.parse_qr_art(fixture_text()))
        st.write_qr_state(None)
        self.assertIsNone(st.read_qr_state())

    def test_expired_qr_is_not_served(self):
        """过期的二维码不该继续展示（扫了也没用）。"""
        code = st.parse_qr_art(fixture_text())
        st.write_qr_state(code)
        data = json.loads(st.qr_state_file().read_text(encoding="utf-8"))
        data["updated_at"] = 0            # 伪造很久以前
        st.qr_state_file().write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(st.read_qr_state())

    def test_qr_state_mode_is_0600(self):
        """二维码等效于一次一次性凭据，文件不该对其他人可读。"""
        st.write_qr_state(st.parse_qr_art(fixture_text()))
        mode = st.qr_state_file().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"权限应为 600，实际 {oct(mode)}")


class TestToolProvision(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def test_not_installed_initially(self):
        self.assertFalse(st.is_installed())
        self.assertEqual(st.installed_version(), "")

    def test_status_shape(self):
        s = st.status()
        for key in ("installed", "version", "license", "repo", "app_id", "cached_login"):
            self.assertIn(key, s)
        self.assertEqual(s["license"], "GPL-2.0")
        self.assertEqual(s["app_id"], "413150")

    def test_repo_is_pinned_to_expected_project(self):
        self.assertEqual(st.DEPOTDOWNLOADER_REPO, "SteamRE/DepotDownloader")
        self.assertEqual(st.ASSET_NAME, "DepotDownloader-linux-x64.zip")

    def test_download_hosts_are_restricted(self):
        for bad in ("example.com", "evil.test", "127.0.0.1"):
            self.assertNotIn(bad, st.ALLOWED_HOSTS)
        self.assertIn("github.com", st.ALLOWED_HOSTS)

    def test_credentials_dir_is_not_under_data_root(self):
        """Steam 登录密钥必须放在 DATA_ROOT 之外 —— 否则会被快照/诊断收集。"""
        cred = st.credentials_dir()
        self.assertFalse(str(cred).startswith(str(C.DATA_ROOT)),
                         f"凭据目录落在数据区内，可能被快照带走：{cred}")
        self.assertTrue(str(cred).startswith(str(C.PKGHOME)))

    def test_build_argv_has_linux_platform_and_qr(self):
        argv = st.build_argv(Path("/tmp/x"))
        self.assertIn("-qr", argv)
        self.assertIn("-remember-password", argv)
        # 必须显式指定平台，否则可能拉到 Windows/macOS depot
        self.assertEqual(argv[argv.index("-os") + 1], "linux")
        self.assertEqual(argv[argv.index("-osarch") + 1], "64")
        self.assertEqual(argv[argv.index("-app") + 1], "413150")

    def test_run_fetch_requires_tool(self):
        with self.assertRaises(ValidationError) as ctx:
            st.run_fetch(Path("/tmp/x"))
        self.assertIn("尚未安装", str(ctx.exception))

    def test_download_rejects_non_https(self):
        with self.assertRaises(ValidationError):
            st._download("http://example.com/x.zip", Path("/tmp/x.zip"))


class TestCancellation(unittest.TestCase):
    """长任务必须可取消，且取消不应被当成"失败"。

    真机踩到：`native.steam-fetch` 没标 cancellable，点取消时后端直接抛
    TaskFailure → 接口返回 500「服务器内部错误」；而且 DepotDownloader 进程
    还在后台跑（取消事件没人检查）。
    """

    def test_task_submitted_as_cancellable(self):
        """cancellable 是**提交**时的参数（不是注册时），必须在接口层传 True。

        真机踩到：没传导致点取消直接 500「服务器内部错误」，而且后台进程还在跑。
        """
        import inspect
        import api_handlers
        src = inspect.getsource(api_handlers)
        i = src.find('"native.steam-fetch"')
        self.assertGreater(i, -1)
        chunk = src[i:i + 400]
        self.assertIn("cancellable=True", chunk,
                      "提交 steam-fetch 时必须标 cancellable")

    def test_run_fetch_checks_cancel(self):
        import inspect
        src = inspect.getsource(st.run_fetch)
        self.assertIn("should_cancel", src)
        self.assertIn("cancelled", src)

    def test_run_result_has_cancelled_flag(self):
        r = st.SteamRunResult()
        self.assertFalse(r.cancelled)

    def test_task_raises_task_cancelled_not_failure(self):
        """取消 → TaskCancelled（终态 cancelled），不是 TaskFailure。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_steam_fetch)
        self.assertIn("TaskCancelled", src)
        i_cancel = src.find("if result.cancelled:")
        i_fail = src.find("if not result.ok:")
        self.assertGreater(i_cancel, -1)
        self.assertLess(i_cancel, i_fail, "应先判取消、再判失败")

    def test_reading_does_not_block_cancel_checks(self):
        """读取必须带超时，否则等扫码期间取消/超时检查永远不会跑。

        真机踩到：`os.read()` 阻塞在"等扫码"上，用户点了取消、后端也记录了请求，
        但任务一直停在 running。
        """
        import inspect
        src = inspect.getsource(st.run_fetch)
        self.assertIn("select.select", src, "必须用 select 带超时等数据")
        self.assertNotIn("chunk = os.read(fd, 4096)", src.split("select.select")[0] + "SENTINEL",
                         "读取前应先 select，不能直接阻塞 read")

    def test_context_cancelled_property_exists(self):
        from mgr.tasks import TaskContext
        self.assertTrue(isinstance(getattr(TaskContext, "cancelled", None), property))


class TestProgressDetection(unittest.TestCase):
    def test_detects_progress_lines(self):
        for line in ("Downloading depot 731... 12.34% (123/1000)",
                     "Validating 45.6%", "Depot 731 manifest 123"):
            self.assertTrue(st._is_progress(line), line)

    def test_ignores_plain_lines(self):
        for line in ("Connecting to Steam3... Done!", "Logged in OK",
                     "Use the Steam Mobile App to sign in with this QR code:"):
            self.assertFalse(st._is_progress(line), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
