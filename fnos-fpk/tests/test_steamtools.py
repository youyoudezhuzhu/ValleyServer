"""Steam 扫码取游戏本体的底层测试。

全部离线（用真实抓取的 DepotDownloader 输出作夹具），不打网络。

夹具 `tests/fixtures/depotdownloader-qr.log` 是**真机实跑** `DepotDownloader -qr`
的输出，含两张 29×29 二维码（挑战码刷新时它会再打印一张）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
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
        src = inspect.getsource(st._run_depot)
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

    def test_run_fetch_requires_login_first(self):
        """登录与下载分离：没登录时必须明确让用户先去登录。"""
        with self.assertRaises(ValidationError) as ctx:
            st.run_fetch(Path("/tmp/x"))
        msg = str(ctx.exception)
        self.assertIn("尚未登录", msg)
        self.assertIn("扫码登录", msg)

    def test_run_fetch_requires_tool(self):
        st.save_username("someone")
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
        """取消检查在公共实现 `_run_depot` 里（登录与下载共用）。"""
        import inspect
        src = inspect.getsource(st._run_depot)
        self.assertIn("should_cancel", src)
        self.assertIn("cancelled", src)
        for fn in (st.run_fetch, st.run_login):
            self.assertIn("should_cancel", inspect.getsource(fn))

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
        src = inspect.getsource(st._run_depot)
        self.assertIn("select.select", src, "必须用 select 带超时等数据")
        self.assertNotIn("chunk = os.read(fd, 4096)", src.split("select.select")[0] + "SENTINEL",
                         "读取前应先 select，不能直接阻塞 read")

    def test_context_cancelled_property_exists(self):
        from mgr.tasks import TaskContext
        self.assertTrue(isinstance(getattr(TaskContext, "cancelled", None), property))


class TestGameRootDetection(unittest.TestCase):
    """下载完之后必须能找到游戏根目录（真机踩到：458MB 下完却被判失败）。

    DepotDownloader 的 `-dir` 就是目标目录，它**直接**把 depot 文件写进 `-dir` 的根，
    不会额外建一层 `Stardew Valley/`。最初只找"名为 Stardew Valley 的子目录"，
    结果下载全部成功之后仍报"没有找到下载出来的游戏目录"，白下一遍。
    """

    def setUp(self):
        self.work = C.STAGING_DIR / f"root-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _seed(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        (root / "Stardew Valley.dll").write_bytes(b"MZ")
        (root / "libhostfxr.so").write_bytes(b"\x7fELF")
        (root / "Content").mkdir(exist_ok=True)
        return root

    def test_files_directly_in_dest(self):
        """最常见形态：文件直接在 -dir 下（真实残留目录就是这个形态）。"""
        self._seed(self.work)
        self.assertEqual(st.locate_game_root(self.work), self.work)

    def test_files_in_named_subdir(self):
        sub = self._seed(self.work / "Stardew Valley")
        self.assertEqual(st.locate_game_root(self.work), sub)

    def test_files_in_single_unnamed_subdir(self):
        sub = self._seed(self.work / "depot-413153")
        self.assertEqual(st.locate_game_root(self.work), sub)

    def test_empty_dir_returns_none(self):
        self.assertIsNone(st.locate_game_root(self.work))

    def test_unrelated_dir_returns_none(self):
        (self.work / "Saves").mkdir()
        (self.work / "Mods").mkdir()
        self.assertIsNone(st.locate_game_root(self.work))

    def test_real_leftover_layout_would_be_found(self):
        """回归：用真机上那次"下完却判失败"的目录形态验证。"""
        # 模拟该目录：游戏文件 + Content/ + 18 个 .so 直接在根
        root = self.work
        (root / "Stardew Valley.dll").write_bytes(b"MZ")
        (root / "Stardew Valley.deps.json").write_text("{}", encoding="utf-8")
        (root / "steam_appid.txt").write_text("413150", encoding="utf-8")
        for i in range(18):
            (root / f"lib{i}.so").write_bytes(b"\x7fELF")
        (root / "Content" / "Animals").mkdir(parents=True)
        found = st.locate_game_root(root)
        self.assertIsNotNone(found, "这次下载不会再被判失败")
        self.assertEqual(found, root)


class TestLoginKeyDetection(unittest.TestCase):
    """登录密钥的真实位置（真机踩到：登录成功却报 cached_login=False）。

    DepotDownloader 用 .NET Isolated Storage 保存密钥，落在
    `$HOME/.local/share/IsolatedStorage/.../account.config`，
    **不是** `$XDG_CONFIG_HOME/DepotDownloader`。
    """

    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        st.tools_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def test_no_key_initially(self):
        self.assertIsNone(st.login_key_file())
        self.assertFalse(st.has_cached_login())

    def test_finds_isolated_storage_key(self):
        d = (st.tools_dir() / ".local" / "share" / "IsolatedStorage"
             / "inlinrrl.x3z" / "v4hmon53.23m" / "Url.xxx" / "AssemFiles")
        d.mkdir(parents=True)
        (d / "account.config").write_bytes(b"<key/>")
        self.assertIsNotNone(st.login_key_file())
        self.assertTrue(st.has_cached_login())

    def test_empty_key_file_is_not_a_login(self):
        d = st.tools_dir() / ".local" / "share" / "IsolatedStorage" / "x"
        d.mkdir(parents=True)
        (d / "account.config").write_bytes(b"")
        self.assertIsNone(st.login_key_file())

    def test_saved_username_roundtrip(self):
        self.assertEqual(st.saved_username(), "")
        st.save_username("fee11one1y")
        self.assertEqual(st.saved_username(), "fee11one1y")
        self.assertTrue(st.has_cached_login(), "记住账号后也该算已有登录")

    def test_login_key_is_outside_data_root(self):
        self.assertFalse(str(st.credentials_dir()).startswith(str(C.DATA_ROOT)))


class TestCredentialLocation(unittest.TestCase):
    """登录凭据的位置必须与检测位置一致（真机踩到的隐蔽路径 bug）。

    历史 bug：`env["HOME"]` 写成 `credentials_dir().parent`（多退了一级），
    于是 DepotDownloader 把密钥落到 `$PKGHOME/.local/...`，
    而 `login_key_file()` 只在 `tools_dir()/.local/...` 找 ——
    表现为"明明能免密登录，却报 cached_login=False / 建议重新登录"。
    """

    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        st.tools_dir().mkdir(parents=True, exist_ok=True)
        self.legacy = st.legacy_isolated_root()
        shutil.rmtree(self.legacy, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        shutil.rmtree(self.legacy, ignore_errors=True)

    def _seed(self, root):
        d = root / "hash1" / "hash2" / "Url.x" / "AssemFiles"
        d.mkdir(parents=True, exist_ok=True)
        (d / "account.config").write_bytes(b"<key/>")
        return d / "account.config"

    def test_paths_are_distinct_and_related(self):
        self.assertTrue(str(st.isolated_root()).startswith(str(st.tools_dir())))
        self.assertTrue(str(st.legacy_isolated_root()).startswith(str(C.PKGHOME)))
        self.assertNotEqual(st.isolated_root(), st.legacy_isolated_root())

    def test_finds_key_in_new_location(self):
        self._seed(st.isolated_root())
        self.assertIsNotNone(st.login_key_file())

    def test_finds_key_in_legacy_location(self):
        """旧位置也要能找到（迁移失败时不至于误判为"未登录"）。"""
        self._seed(st.legacy_isolated_root())
        self.assertIsNotNone(st.login_key_file())

    def test_migration_moves_key(self):
        self._seed(st.legacy_isolated_root())
        self.assertTrue(st.migrate_legacy_login())
        self.assertFalse(self.legacy.is_dir(), "旧位置应已清空")
        found = st.login_key_file()
        self.assertIsNotNone(found, "迁移后必须能在新位置找到密钥")
        self.assertTrue(str(found).startswith(str(st.isolated_root())))

    def test_migration_is_idempotent(self):
        self._seed(st.legacy_isolated_root())
        st.migrate_legacy_login()
        self.assertFalse(st.migrate_legacy_login(), "已经迁移过就不该再动")

    def test_migration_does_not_clobber_new_location(self):
        self._seed(st.isolated_root())
        self._seed(st.legacy_isolated_root())
        self.assertFalse(st.migrate_legacy_login())
        self.assertTrue(st.isolated_root().is_dir())

    def test_home_is_tools_dir(self):
        """结构断言：HOME 的赋值必须就是 tools_dir（与检测位置一致）。

        只看**赋值语句**，不看注释 —— 源码注释里会提到历史 bug 的写法。
        """
        import inspect
        src = inspect.getsource(st._run_depot)
        self.assertIn('env["HOME"] = str(credentials_dir())', src)
        assigns = [l.strip() for l in src.splitlines()
                   if l.strip().startswith('env["HOME"]')]
        self.assertEqual(len(assigns), 1, f"HOME 应当只赋值一次：{assigns}")
        self.assertNotIn(".parent", assigns[0], f"HOME 不该指向上一级：{assigns[0]}")

    def test_login_incomplete_flag(self):
        st.save_username("someone")
        self.assertTrue(st.status()["login_incomplete"],
                        "只有账号没有密钥时，必须提示登录不完整")
        self._seed(st.isolated_root())
        self.assertFalse(st.status()["login_incomplete"])


class TestLoginSuccessParsing(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        st.tools_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def test_parses_real_message(self):
        """用 DepotDownloader 真实回显的那行。"""
        line = ("Success! Next time you can login with -username fee11one1y "
                "-remember-password instead of -qr.")
        self.assertEqual(st.note_login_success(line), "fee11one1y")
        self.assertEqual(st.saved_username(), "fee11one1y")

    def test_ignores_unrelated_lines(self):
        for line in ("Connecting to Steam3... Done!",
                     "Use the Steam Mobile App to sign in with this QR code:",
                     "The QR code has changed:"):
            with self.subTest(line=line[:30]):
                self.assertEqual(st.note_login_success(line), "")

    def test_no_qr_after_login_remembered(self):
        """记住账号后，构造的命令行不应再要求扫码。"""
        st.save_username("someone")
        argv = st.build_argv(Path("/tmp/x"))
        self.assertNotIn("-qr", argv)
        self.assertIn("-username", argv)
        self.assertIn("someone", argv)
        self.assertIn("-remember-password", argv)

    def test_qr_when_no_login_saved(self):
        argv = st.build_argv(Path("/tmp/x"))
        self.assertIn("-qr", argv)

    def test_explicit_use_qr_override(self):
        st.save_username("someone")
        self.assertIn("-qr", st.build_argv(Path("/tmp/x"), use_qr=True))
        self.assertNotIn("-qr", st.build_argv(Path("/tmp/x"), use_qr=False))


class TestQrRefreshDetection(unittest.TestCase):
    """二维码刷新必须能被识别（真机踩到：用户扫到的是过期码）。

    DepotDownloader 每次刷新的二维码都是 29×29，**模块数量完全相同**。
    最初前端用"模块数量变了才重绘"判断，于是界面永远停在第一张（已过期）的码上，
    用户在手机上"登录成功"了，管理器这边却认证失败。
    前端改为比较**内容指纹**；这里断言前端确实这么做。
    """

    def test_frontend_compares_content_fingerprint(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("qrFingerprint", app, "必须按内容指纹判断二维码是否刷新")
        self.assertNotIn("matrix.length !== shown", app,
                         "不能再用模块数量判断（每次刷新数量都一样）")

    def test_fixture_qr_codes_have_identical_size_but_different_content(self):
        """夹具里的两张二维码尺寸相同、内容不同 —— 这正是当初判断写错的原因。"""
        codes = st.parse_qr_codes(fixture_text())
        self.assertEqual(len(codes), 2)
        self.assertEqual(codes[0].modules, codes[1].modules)
        self.assertNotEqual(codes[0].matrix, codes[1].matrix)

    def test_fingerprint_distinguishes_codes(self):
        """前端的指纹算法必须能区分这两张码（用等价实现验证）。"""
        codes = st.parse_qr_codes(fixture_text())
        self.assertNotEqual(_fp(codes[0].matrix), _fp(codes[1].matrix))


def _fp(matrix) -> int:
    """与前端 qrFingerprint 等价的实现（用于验证算法可区分不同二维码）。"""
    h = 0
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            h = ((h * 31) + (1 if v else 7) + r * 3 + c) % 1000000007
    return h


class TestLoginFetchSeparation(unittest.TestCase):
    """登录与获取/更新是两个独立动作（用户明确要求分开）。

    理由：登录是秒级的会话建立，下载是几百 MB、几分钟的操作。
    揉在一起时用户分不清"扫完码还没好"到底是卡住还是在下载，
    失败时也说不清是登录失败还是下载失败。
    """

    def setUp(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)
        st.tools_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(st.tools_dir(), ignore_errors=True)

    def test_login_argv_is_manifest_only(self):
        """登录用 -manifest-only：只登录+拉清单，不下载文件内容。"""
        argv = st.build_login_argv(Path("/tmp/x"), force_qr=True)
        self.assertIn("-manifest-only", argv)
        self.assertIn("-qr", argv)
        self.assertIn("-remember-password", argv)
        self.assertNotIn("-validate", argv)
        self.assertEqual(argv[argv.index("-app") + 1], "413150")

    def test_login_argv_uses_username_when_not_forcing_qr(self):
        argv = st.build_login_argv(Path("/tmp/x"), force_qr=False, username="fee11one1y")
        self.assertNotIn("-qr", argv)
        self.assertIn("-username", argv)
        self.assertIn("fee11one1y", argv)

    def test_fetch_argv_never_scans_qr(self):
        """下载动作不该再出现扫码 —— 那是登录动作的事。"""
        st.save_username("fee11one1y")
        argv = st.build_argv(Path("/tmp/x"))
        self.assertNotIn("-qr", argv)
        self.assertIn("-username", argv)
        self.assertIn("-validate", argv)
        self.assertNotIn("-manifest-only", argv)

    def test_fetch_refuses_without_login(self):
        with self.assertRaises(ValidationError) as ctx:
            st.run_fetch(Path("/tmp/x"))
        self.assertIn("尚未登录", str(ctx.exception))

    def test_login_task_registered_and_separate(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.register_all)
        self.assertIn('"native.steam-login"', src)
        self.assertIn('"native.steam-fetch"', src)
        login = inspect.getsource(orchestration._Services.task_native_steam_login)
        # 登录任务不得触碰游戏目录
        self.assertNotIn("_place_game_body", login)
        self.assertNotIn("C.GAME_FILES_DIR", login)

    def test_login_task_has_no_snapshot(self):
        """登录不改游戏数据，不该触发快照。"""
        import inspect
        import orchestration
        login = inspect.getsource(orchestration._Services.task_native_steam_login)
        self.assertNotIn("_auto_snapshot", login)

    def test_fetch_task_requires_login(self):
        import inspect
        import orchestration
        fetch = inspect.getsource(orchestration._Services.task_native_steam_fetch)
        self.assertIn("saved_username", fetch)
        self.assertIn("尚未登录", fetch)

    def test_api_endpoints_split(self):
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        self.assertIn('"/api/native/steam/login"', src)
        self.assertIn('"/api/native/steam/fetch"', src)
        self.assertIn('"/api/native/steam/logout"', src)


class TestProgressReporting(unittest.TestCase):
    """下载进度必须能反映到界面（真机踩到"看着像卡住"）。

    登录成功后要下载 400+ MB（约 1~2 分钟），而任务进度里**整段下载算一步**，
    于是进度条一直冻在 20% —— 用户判定为"卡住了"。
    这里覆盖进度解析与状态文件的读写/过期。
    """

    LINES = [
        ("Connecting to Steam3... Done!", "连接 Steam"),
        ("Logging in with QR code...", "等待扫码"),
        ("Success! Next time you can login with -username fee11one1y "
         "-remember-password instead of -qr.", "已登录"),
        ("Got 243 licenses for account!", "已登录"),
        ("Downloading depot 413153 manifest", "下载游戏文件"),
        ("Pre-allocating /tmp/x/Content/Animals/cat.xnb", "下载游戏文件"),
        ("Validating 12 files", "校验文件"),
        ("413153 - Downloaded 458056832 bytes (708901690 bytes uncompressed)", "下载完成"),
        ("Total downloaded: 458056832 bytes (708901690 bytes uncompressed) from 1 depots",
         "下载完成"),
    ]

    def test_stage_detection_on_real_lines(self):
        for line, stage in self.LINES:
            with self.subTest(line=line[:44]):
                self.assertEqual(st.parse_progress_line(line).get("stage"), stage)

    def test_percent_parsing(self):
        info = st.parse_progress_line("12.34% /tmp/x/BmFont.dll")
        self.assertEqual(info.get("percent"), 12.34)

    def test_total_bytes_parsing(self):
        info = st.parse_progress_line(
            "Total downloaded: 458056832 bytes (708901690 bytes uncompressed) from 1 depots")
        self.assertEqual(info.get("total_bytes"), 458056832)

    def test_unknown_line_yields_nothing(self):
        self.assertEqual(st.parse_progress_line("Disconnected from Steam"), {})
        self.assertEqual(st.parse_progress_line(""), {})

    def test_progress_state_roundtrip(self):
        st.write_progress({"stage": "下载游戏文件", "percent": 42.5,
                           "started_at": time.time()})
        got = st.read_progress()
        self.assertIsNotNone(got)
        self.assertEqual(got["stage"], "下载游戏文件")
        self.assertEqual(got["percent"], 42.5)

    def test_progress_cleared(self):
        st.write_progress({"stage": "下载游戏文件"})
        st.write_progress(None)
        self.assertIsNone(st.read_progress())

    def test_stale_progress_not_served(self):
        """任务早就结束了，不该继续显示过期进度。"""
        st.write_progress({"stage": "下载游戏文件"})
        data = json.loads(st.progress_state_file().read_text(encoding="utf-8"))
        data["updated_at"] = time.time() - 600
        st.progress_state_file().write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(st.read_progress())

    def test_run_fetch_writes_and_clears_progress(self):
        """下载流程必须维护进度状态（否则界面拿不到任何进度）。"""
        import inspect
        src = inspect.getsource(st._run_depot)
        self.assertIn("write_progress", src)
        self.assertIn("write_progress(None)", src)

    def test_payload_lines_are_progress_detected(self):
        for line in ("Downloading depot 413153 manifest",
                     "Pre-allocating /tmp/x/a.dll",
                     "Total downloaded: 1 bytes"):
            self.assertTrue(st._is_progress(line), line)


class TestNoQrWhenLoginCached(unittest.TestCase):
    """有缓存登录时不会打印二维码 —— 界面不能说"等待扫码"。

    真机风险：用户上次扫码成功后我们记住了账号，下次会用
    `-username X -remember-password` 直接登录，**永远不会出现二维码**。
    如果界面还提示"正在生成二维码…"，用户就会一直等一张不存在的码（=卡住）。
    """

    def test_argv_has_no_qr_when_login_saved(self):
        st.save_username("fee11one1y")
        self.assertNotIn("-qr", st.build_argv(Path("/tmp/x")))

    def test_frontend_has_separate_login_and_fetch(self):
        """登录与获取/更新必须是**两个独立入口**（用户明确要求）。"""
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("async function steamLogin()", app)
        self.assertIn("async function steamFetchGame()", app)
        self.assertIn("native/steam/login", app)
        self.assertIn("native/steam/fetch", app)
        self.assertIn("'Steam 扫码登录'", app)
        self.assertIn("'获取 / 更新游戏本体'", app)

    def test_frontend_shows_progress_and_does_not_swallow_errors(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("native/steam/progress", app, "必须轮询下载进度")
        self.assertIn("console.warn", app, "轮询出错不能静默吞掉（否则表现为界面卡住）")

    def test_force_qr_discards_saved_login(self):
        """缓存登录可能失效，必须能强制重新扫码。"""
        st.save_username("fee11one1y")
        self.assertTrue(st.has_cached_login())
        st.clear_saved_login()
        self.assertEqual(st.saved_username(), "")
        self.assertIn("-qr", st.build_argv(Path("/tmp/x")))

    def test_api_and_task_accept_force_qr(self):
        root = Path(__file__).resolve().parent.parent
        api = (root / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        orch = (root / "fnpack/app/server/orchestration.py").read_text(encoding="utf-8")
        self.assertIn("force_qr", api)
        self.assertIn("force_qr", orch)

    def test_frontend_offers_relogin_and_logout(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("重新扫码登录", app)
        self.assertIn("退出登录", app)
        self.assertIn("native/steam/logout", app)

    def test_backend_exposes_progress_endpoint(self):
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        self.assertIn("/api/native/steam/progress", src)


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
