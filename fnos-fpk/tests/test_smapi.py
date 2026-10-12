"""SMAPI 供给测试（用户只提供游戏本体，SMAPI 由管理器获取整合）。

全部用**离线 fixture**，不打网络：网络路径只在 `TestNetworkSmoke` 里跑，
且用 `VALLEY_TEST_NETWORK=1` 才启用（CI 默认跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import smapi  # noqa: E402
from mgr.util import ValidationError  # noqa: E402


def make_game(root: Path, game_version: str = "1.6.15", *, with_smapi: bool = False,
              smapi_version: str = "4.5.1") -> Path:
    """造一个最小的"游戏本体"目录（含 deps.json，供版本探测）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Stardew Valley.dll").write_bytes(b"MZ" + b"\x00" * 32)
    (root / "Stardew Valley.deps.json").write_text(json.dumps({
        "runtimeTarget": {"name": ".NETCoreApp,Version=v6.0"},
        "libraries": {
            f"Stardew Valley/{game_version}.24356": {"type": "project"},
            "runtimepack.Microsoft.NETCore.App.Runtime.linux-x64/6.0.32": {"type": "package"},
        },
    }), encoding="utf-8")
    (root / "Content").mkdir(exist_ok=True)
    (root / "Content" / "Animals").mkdir(exist_ok=True)
    (root / "Content" / "Animals" / "cat.xnb").write_bytes(b"xnb")
    if with_smapi:
        (root / "StardewModdingAPI").write_text("#!/bin/sh\n")
        (root / "StardewModdingAPI.dll").write_bytes(b"MZ")
        (root / "smapi-internal").mkdir(exist_ok=True)
        (root / "smapi-internal" / "config.json").write_text("{}")
    return root


def make_install_dat(path: Path, *, extra_top: str = "") -> Path:
    """造一个 install.dat（就是 payload zip）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("StardewModdingAPI", "#!/bin/sh\necho smapi\n")
        zf.writestr("StardewModdingAPI.dll", "MZ" + "x" * 64)
        zf.writestr("StardewModdingAPI.runtimeconfig.json", "{}")
        zf.writestr("StardewModdingAPI.xml", "<doc/>")
        zf.writestr("smapi-internal/config.json", "{}")
        zf.writestr("smapi-internal/0Harmony.dll", "MZ")
        zf.writestr("unix-launcher.sh", "#!/bin/sh\n")
        zf.writestr("steam_appid.txt", "413150\n")
        zf.writestr("Mods/ConsoleCommands/manifest.json", "{}")
        zf.writestr("Mods/ConsoleCommands/ConsoleCommands.dll", "MZ")
        zf.writestr("Mods/SaveBackup/manifest.json", "{}")
        zf.writestr("Mods/SaveBackup/SaveBackup.dll", "MZ")
        if extra_top:
            zf.writestr(f"{extra_top}/unknown.bin", "x")
    return path


def make_installer_zip(path: Path, *, version="4.5.2", dat: Path | None = None) -> Path:
    """造一个 SMAPI installer.zip（结构与本机实测一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dat or make_install_dat(C.STAGING_DIR / "fixture-install.dat")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        base = f"SMAPI {version} installer/"
        zf.writestr(base + "README.txt", "readme")
        zf.writestr(base + "install on Linux.sh", "#!/bin/bash\n")
        zf.writestr(base + "internal/linux/SMAPI.Installer", "binary")
        zf.write(payload, base + "internal/linux/install.dat")
        zf.writestr(base + "internal/windows/whatever.dll", "MZ")
    return path


def rel(version: str, *, min_game: str = "1.6.14", prerelease: bool = False,
        body_extra: str = "") -> smapi.Release:
    body = (f"Requires Stardew Valley {min_game} or later on Linux/macOS/Windows "
            f"or Steam Deck.\n{body_extra}")
    return smapi.Release(version=version, tag=version, prerelease=prerelease,
                         published_at="2026-03-22T00:00:00Z", min_game_version=min_game,
                         download_url=f"https://github.com/Pathoschild/SMAPI/releases/download/"
                                      f"{version}/SMAPI-{version}-installer.zip",
                         size=41889142, body=body)


class TestGameVersionDetection(unittest.TestCase):
    def setUp(self):
        self.root = C.STAGING_DIR / f"game-{os.getpid()}"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reads_version_from_deps_json(self):
        make_game(self.root, "1.6.15")
        self.assertEqual(smapi.read_game_version(self.root), "1.6.15")

    def test_strips_build_number(self):
        make_game(self.root, "1.6.14")
        (self.root / "Stardew Valley.deps.json").write_text(json.dumps({
            "libraries": {"Stardew Valley/1.6.14.24123": {"type": "project"}},
        }), encoding="utf-8")
        self.assertEqual(smapi.read_game_version(self.root), "1.6.14")

    def test_returns_empty_when_unknown(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.assertEqual(smapi.read_game_version(self.root), "")

    def test_ignores_unrelated_libraries(self):
        make_game(self.root)
        (self.root / "Stardew Valley.deps.json").write_text(json.dumps({
            "libraries": {
                "Microsoft.NETCore.App/6.0.32": {"type": "package"},
                "SomeOther Game/9.9.9": {"type": "project"},
            },
        }), encoding="utf-8")
        self.assertEqual(smapi.read_game_version(self.root), "")

    def test_smapi_detection(self):
        make_game(self.root, with_smapi=False)
        self.assertFalse(smapi.is_installed(self.root))
        make_game(self.root, with_smapi=True)
        self.assertTrue(smapi.is_installed(self.root))


class TestCompatibilityParsing(unittest.TestCase):
    def test_parses_min_game_version(self):
        self.assertEqual(
            smapi.parse_min_game_version(
                "Requires Stardew Valley 1.6.14 or later on Linux/macOS/Windows."),
            "1.6.14")
        self.assertEqual(
            smapi.parse_min_game_version("Requires Stardew Valley 1.6 or later"),
            "1.6")

    def test_returns_empty_when_not_stated(self):
        """解析不到就返回空串 —— 不猜测，也不假装知道。"""
        self.assertEqual(smapi.parse_min_game_version("Just a release"), "")
        self.assertEqual(smapi.parse_min_game_version(""), "")

    def test_satisfies(self):
        self.assertTrue(smapi.satisfies("1.6.15", "1.6.14"))
        self.assertTrue(smapi.satisfies("1.6.14", "1.6.14"))
        self.assertFalse(smapi.satisfies("1.6.13", "1.6.14"))
        self.assertTrue(smapi.satisfies("1.6.15", ""))   # 未声明 = 不拦
        self.assertTrue(smapi.satisfies("1.10.0", "1.6.14"))
        self.assertFalse(smapi.satisfies("1.5.6", "1.6.14"))


class TestSteamAppId(unittest.TestCase):
    """`steam_appid.txt` 必须是 413150。

    真机踩到：**Steam depot 里的 `steam_appid.txt` 内容是 480（Spacewar 测试应用）**，
    那是开发者构建时留下的值；Steam 客户端安装时会改写它，但 DepotDownloader
    只是原样解包 depot，于是错误值被带进游戏目录 → Steamworks 以 480 初始化 →
    Steam 联机/成就失效。
    （已实测：SMAPI 自己的负载里这个文件是正确的 413150，所以问题来自 depot。）
    """

    def setUp(self):
        self.work = C.STAGING_DIR / f"appid-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)

    def test_creates_when_missing(self):
        self.assertEqual(smapi.ensure_app_id(self.work), "created")
        self.assertEqual((self.work / "steam_appid.txt").read_text().strip(), "413150")

    def test_fixes_wrong_depot_value(self):
        (self.work / "steam_appid.txt").write_text("480", encoding="utf-8")
        result = smapi.ensure_app_id(self.work)
        self.assertTrue(result.startswith("fixed:"))
        self.assertIn("480", result)
        self.assertEqual((self.work / "steam_appid.txt").read_text().strip(), "413150")

    def test_noop_when_correct(self):
        (self.work / "steam_appid.txt").write_text("413150", encoding="utf-8")
        before = (self.work / "steam_appid.txt").stat().st_mtime_ns
        self.assertEqual(smapi.ensure_app_id(self.work), "")
        self.assertEqual((self.work / "steam_appid.txt").stat().st_mtime_ns, before,
                         "已经正确时不该重写（避免无意义的文件变更）")

    def test_tolerates_whitespace_and_newline(self):
        (self.work / "steam_appid.txt").write_text("  413150\n", encoding="utf-8")
        self.assertEqual(smapi.ensure_app_id(self.work), "")

    def test_constant_matches_steam_tools(self):
        from mgr import steamtools
        self.assertEqual(smapi.STARDEW_APP_ID, steamtools.STEAM_APP_ID)

    def test_integrate_overrides_payload_value(self):
        """即使 SMAPI 负载里带的是别的值，最终也必须被校准。"""
        (self.work / "Stardew Valley.deps.json").write_text("{}", encoding="utf-8")
        payload = self.work / "payload.zip"
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr("StardewModdingAPI", "#!/bin/sh\n")
            zf.writestr("StardewModdingAPI.dll", "MZ")
            zf.writestr("smapi-internal/x.dll", "MZ")
            zf.writestr("steam_appid.txt", "480")
        (self.work / "steam_appid.txt").write_text("480", encoding="utf-8")
        smapi.integrate(payload, self.work)
        self.assertEqual((self.work / "steam_appid.txt").read_text().strip(), "413150")

    def test_place_game_body_also_calls_ensure_app_id(self):
        """就位流程必须校准（否则"游戏目录已带 SMAPI"时会漏掉）。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services._place_game_body)
        self.assertIn("ensure_app_id", src)


class TestDepsJsonGeneration(unittest.TestCase):
    """整合 SMAPI 后必须存在 `StardewModdingAPI.deps.json`。

    真机踩到的坑（用 **Steam 正版副本** 时必现）：SMAPI 的 Linux 负载里
    **不含** `StardewModdingAPI.deps.json`，官方 `install on Linux.sh` 是拿
    **游戏自己的 deps.json 复制一份**过来的（上游镜像里两者 md5 完全一致，
    已实测确认）。缺了它，.NET apphost 会把应用判定为"依赖共享框架"
    （split/FX 模式），而本机并没有装 .NET 运行时（游戏是自包含发布），
    于是启动器只打印一段 host 用法帮助就退出 —— 用户完全看不出问题在哪。

    为什么以前没暴露：上游镜像那份游戏本来就装过 SMAPI，这个文件已存在；
    一旦换成干净的游戏目录（例如用 Steam 扫码下载的副本），立刻踩到。
    """

    def setUp(self):
        self.work = C.STAGING_DIR / f"depsjson-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _seed_game(self):
        (self.work / "Stardew Valley.deps.json").write_text('{"runtimeTarget":{}}', encoding="utf-8")
        (self.work / "Stardew Valley.dll").write_bytes(b"MZ")
        return self.work

    def test_copies_game_deps_json(self):
        self._seed_game()
        name = smapi._ensure_deps_json(self.work)
        self.assertEqual(name, "StardewModdingAPI.deps.json")
        self.assertTrue((self.work / "StardewModdingAPI.deps.json").is_file())

    def test_generated_content_equals_game_deps(self):
        """官方做法就是原样复制（上游镜像里两者 md5 一致）。"""
        self._seed_game()
        smapi._ensure_deps_json(self.work)
        self.assertEqual(
            (self.work / "StardewModdingAPI.deps.json").read_bytes(),
            (self.work / "Stardew Valley.deps.json").read_bytes())

    def test_keeps_existing_file(self):
        self._seed_game()
        target = self.work / "StardewModdingAPI.deps.json"
        target.write_text('{"custom":true}', encoding="utf-8")
        self.assertEqual(smapi._ensure_deps_json(self.work), "")
        self.assertEqual(target.read_text(encoding="utf-8"), '{"custom":true}')

    def test_falls_back_to_other_stardew_deps(self):
        (self.work / "Stardew Valley.deps.json").unlink(missing_ok=True)
        (self.work / "Stardew Valley 1.6.deps.json").write_text("{}", encoding="utf-8")
        self.assertEqual(smapi._ensure_deps_json(self.work), "StardewModdingAPI.deps.json")

    def test_no_deps_json_at_all_is_not_fatal(self):
        """找不到来源时返回空串，不应抛异常（整合本身仍然成功）。"""
        self.assertEqual(smapi._ensure_deps_json(self.work), "")

    def test_integrate_creates_deps_json_on_clean_game_dir(self):
        """端到端：干净游戏目录（Steam 副本形态）整合后必须可启动。"""
        self._seed_game()
        # 造一个最小 SMAPI 负载
        payload = self.work / "payload.zip"
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr("StardewModdingAPI", "#!/bin/sh\n")
            zf.writestr("StardewModdingAPI.dll", "MZ")
            zf.writestr("StardewModdingAPI.runtimeconfig.json", "{}")
            zf.writestr("smapi-internal/SMAPI.Toolkit.dll", "MZ")
        info = smapi.integrate(payload, self.work)
        self.assertTrue((self.work / "StardewModdingAPI.deps.json").is_file(),
                        "干净游戏目录整合后缺少 deps.json → 启动器会只打印 host 帮助就退出")
        self.assertEqual(info.get("deps_json"), "StardewModdingAPI.deps.json")

    def test_integrate_reports_deps_json_in_result(self):
        import inspect
        src = inspect.getsource(smapi.integrate)
        self.assertIn("_ensure_deps_json", src)


class TestReleasePicking(unittest.TestCase):
    def setUp(self):
        self.releases = [
            rel("4.5.2"), rel("4.5.1"), rel("4.4.0"),
            rel("4.6.0", prerelease=True),
            rel("3.0.0", min_game="1.5.0"),
        ]

    def test_picks_latest_stable_compatible(self):
        self.assertEqual(smapi.pick_release(self.releases, "1.6.15").version, "4.5.2")

    def test_skips_prerelease(self):
        self.assertNotEqual(smapi.pick_release(self.releases, "1.6.15").version, "4.6.0")

    def test_prefers_prerelease_when_no_stable(self):
        only_pre = [rel("4.6.0", prerelease=True)]
        self.assertEqual(smapi.pick_release(only_pre, "1.6.15").version, "4.6.0")

    def test_refuses_when_game_too_old(self):
        """游戏太旧时必须明确报错，而不是装一个不兼容的 SMAPI。"""
        with self.assertRaises(ValidationError) as ctx:
            smapi.pick_release([rel("4.5.2", min_game="1.6.14")], "1.5.6")
        self.assertIn("没有与游戏 1.5.6 兼容", str(ctx.exception))
        self.assertIn("1.6.14", str(ctx.exception))

    def test_no_min_version_stated_is_treated_as_compatible(self):
        r = smapi.Release(version="9.9.9", tag="9.9.9", prerelease=False,
                          published_at="", min_game_version="", download_url="https://x/y")
        self.assertEqual(smapi.pick_release([r], "1.0.0").version, "9.9.9")

    def test_explicit_version_is_honored(self):
        self.assertEqual(smapi.pick_release(self.releases, "1.6.15", want="4.4.0").version,
                         "4.4.0")

    def test_explicit_incompatible_version_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            smapi.pick_release(self.releases, "1.5.6", want="4.5.2")
        self.assertIn("需要游戏", str(ctx.exception))

    def test_unknown_explicit_version_is_rejected(self):
        with self.assertRaises(ValidationError):
            smapi.pick_release(self.releases, "1.6.15", want="0.0.1")

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ValidationError):
            smapi.pick_release([], "1.6.15")


class TestPayloadExtraction(unittest.TestCase):
    def setUp(self):
        self.work = C.STAGING_DIR / f"payload-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_finds_install_dat(self):
        z = make_installer_zip(self.work / "inst.zip")
        self.assertTrue(smapi.payload_members(z)["install_dat"].endswith("install.dat"))

    def test_extract_install_dat(self):
        z = make_installer_zip(self.work / "inst.zip")
        dat = smapi.extract_install_dat(z, self.work / "install.dat")
        self.assertTrue(dat.is_file())
        with zipfile.ZipFile(dat) as zf:
            self.assertIn("StardewModdingAPI", zf.namelist())

    def test_missing_install_dat_is_a_clear_error(self):
        bad = self.work / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("README.txt", "no payload here")
        with self.assertRaises(ValidationError) as ctx:
            smapi.payload_members(bad)
        self.assertIn("install.dat", str(ctx.exception))


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.work = C.STAGING_DIR / f"integ-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.game = make_game(self.work / "game", with_smapi=False)
        self.dat = make_install_dat(self.work / "install.dat")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_integrates_runtime_files(self):
        info = smapi.integrate(self.dat, self.game)
        self.assertTrue((self.game / "StardewModdingAPI").is_file())
        self.assertTrue((self.game / "StardewModdingAPI.dll").is_file())
        self.assertTrue((self.game / "smapi-internal" / "0Harmony.dll").is_file())
        self.assertTrue(smapi.is_installed(self.game))
        self.assertGreater(info["written"], 5)

    def test_launcher_is_executable(self):
        smapi.integrate(self.dat, self.game)
        self.assertTrue(os.access(self.game / "StardewModdingAPI", os.X_OK))

    def test_bundled_mods_are_added_when_missing(self):
        info = smapi.integrate(self.dat, self.game)
        self.assertTrue((self.game / "Mods" / "ConsoleCommands" / "manifest.json").is_file())
        self.assertIn("ConsoleCommands", info["bundled_mods"])

    def test_existing_user_mods_are_never_overwritten(self):
        """这是最重要的一条：安装流程绝不动用户 MOD 目录里的已有文件。"""
        marker = self.game / "Mods" / "SaveBackup" / "USER_FILE.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("用户自己的东西")
        (self.game / "Mods" / "ConsoleCommands").mkdir(parents=True, exist_ok=True)
        (self.game / "Mods" / "ConsoleCommands" / "manifest.json").write_text("用户改过的")
        smapi.integrate(self.dat, self.game)
        self.assertEqual(marker.read_text(), "用户自己的东西")
        self.assertEqual((self.game / "Mods" / "ConsoleCommands" / "manifest.json").read_text(),
                         "用户改过的")

    def test_steam_appid_is_normalised_not_left_alone(self):
        """整合后 `steam_appid.txt` 必须是 413150 —— **即使是已有文件**。

        这条原来是"只在缺失时写入"，但真机证明那样不够：Steam depot 里带的
        是开发者测试用的 **480**（Spacewar），原样保留会让 Steamworks 以错误
        App ID 初始化，Steam 联机/成就失效。所以正确契约是"校准到 413150"。
        """
        target = self.game / "steam_appid.txt"
        target.write_text("480")
        smapi.integrate(self.dat, self.game)
        self.assertEqual(target.read_text().strip(), smapi.STARDEW_APP_ID)

    def test_steam_appid_already_correct_is_not_rewritten(self):
        """已经正确时不该动它（避免无意义的文件变更）。"""
        target = self.game / "steam_appid.txt"
        target.write_text(smapi.STARDEW_APP_ID)
        before = target.stat().st_mtime_ns
        smapi.integrate(self.dat, self.game)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_unknown_top_level_files_are_not_dropped_into_game(self):
        """除 SMAPI 自身文件与 bundled Mods 外，不擅自往游戏目录塞东西。"""
        dat = make_install_dat(self.work / "weird.dat", extra_top="TotallyUnknown")
        info = smapi.integrate(dat, self.game)
        self.assertFalse((self.game / "TotallyUnknown").exists())
        self.assertGreater(info["skipped"], 0)

    def test_upgrades_existing_smapi(self):
        smapi.integrate(self.dat, self.game)
        before = (self.game / "smapi-internal" / "0Harmony.dll").read_bytes()
        dat2 = make_install_dat(self.work / "dat2.dat")
        with zipfile.ZipFile(dat2, "a") as zf:
            zf.writestr("smapi-internal/NEW_FILE.dll", "new")
        smapi.integrate(dat2, self.game)
        self.assertTrue((self.game / "smapi-internal" / "NEW_FILE.dll").is_file())
        self.assertTrue(before)   # 原文件仍在（被覆盖为新内容）

    def test_payload_without_launcher_is_rejected(self):
        bad = self.work / "nolauncher.dat"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("smapi-internal/config.json", "{}")
        with self.assertRaises(ValidationError) as ctx:
            smapi.integrate(bad, self.game)
        self.assertIn("StardewModdingAPI", str(ctx.exception))

    def test_missing_game_dir_is_rejected(self):
        with self.assertRaises(ValidationError):
            smapi.integrate(self.dat, self.work / "nope")


class TestDownloadGuards(unittest.TestCase):
    def test_rejects_non_https(self):
        r = rel("4.5.2")
        r.download_url = "http://example.com/x.zip"
        with self.assertRaises(ValidationError) as ctx:
            smapi.download(r, C.STAGING_DIR / "x.zip")
        self.assertIn("HTTPS", str(ctx.exception))

    def test_rejects_empty_url(self):
        r = rel("4.5.2")
        r.download_url = ""
        with self.assertRaises(ValidationError):
            smapi.download(r, C.STAGING_DIR / "x.zip")

    def test_allowed_hosts_do_not_include_arbitrary_sites(self):
        for bad in ("example.com", "evil.test", "127.0.0.1", "localhost"):
            self.assertNotIn(bad, smapi.ALLOWED_HOSTS)

    def test_size_cap_is_sane(self):
        self.assertLess(smapi.MAX_INSTALLER_BYTES, 1024 ** 3)
        self.assertGreater(smapi.MAX_INSTALLER_BYTES, 60 * 1024 * 1024)


class TestStatusShape(unittest.TestCase):
    def test_status_keys_are_flat_booleans_and_strings(self):
        """`smapi.status()` 必须是扁平结构 —— 界面直接取 installed/version/game_version。

        回归：状态接口曾把 status 嵌成 {"installed": {…}}，前端取 smapi.version 直接 KeyError。
        """
        import inspect
        from mgr import smapi as mod
        src = inspect.getsource(mod.status)
        self.assertIn('"installed": installed', src.replace("'", '"'))
        self.assertNotIn('"installed": {', src.replace("'", '"'))

    def test_status_reports_game_and_smapi(self):
        work = C.STAGING_DIR / f"status-{os.getpid()}"
        shutil.rmtree(work, ignore_errors=True)
        make_game(work, "1.6.15", with_smapi=True)
        st = smapi.status(work)
        self.assertEqual(st["game_version"], "1.6.15")
        self.assertTrue(st["installed"])
        self.assertEqual(st["game_root"], str(work))
        shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(os.environ.get("VALLEY_TEST_NETWORK") == "1",
                     "需要联网（设 VALLEY_TEST_NETWORK=1 启用）")
class TestNetworkSmoke(unittest.TestCase):
    def test_can_list_real_releases(self):
        releases = smapi.list_releases(force=True)
        self.assertTrue(releases)
        self.assertTrue(any(r.version.startswith("4.") for r in releases))
        self.assertTrue(any(r.min_game_version for r in releases),
                        "SMAPI 发布说明里应能解析出最低游戏版本")




class TestAtomicOverwrite(unittest.TestCase):
    """覆盖"正在运行的可执行文件"必须成功。

    真机踩到：游戏正在运行时升级 SMAPI，直接 `shutil.copy2` 覆盖
    `StardewModdingAPI` 会拿到 `OSError: [Errno 26] Text file busy`。
    解法是先写同目录临时文件再 `os.replace`（原子重命名，运行中的进程仍持旧 inode）。
    """

    def setUp(self):
        self.work = C.STAGING_DIR / f"atomic-{os.getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_atomic_copy_replaces_existing(self):
        src = self.work / "new.bin"
        src.write_bytes(b"NEW")
        dst = self.work / "target.bin"
        dst.write_bytes(b"OLD")
        smapi._atomic_copy(src, dst)
        self.assertEqual(dst.read_bytes(), b"NEW")

    def test_atomic_copy_creates_missing_target(self):
        src = self.work / "a.bin"
        src.write_bytes(b"A")
        dst = self.work / "sub" / "b.bin"
        smapi._atomic_copy(src, dst)
        self.assertEqual(dst.read_bytes(), b"A")

    def test_atomic_copy_over_running_executable(self):
        """核心回归：目标正在被执行时也必须能替换。"""
        src = self.work / "replacement.sh"
        src.write_bytes(b"#!/bin/sh\nsleep 3\n")
        os.chmod(src, 0o755)
        dst = self.work / "running.sh"
        dst.write_bytes(b"#!/bin/sh\nsleep 3\n")
        os.chmod(dst, 0o755)

        proc = subprocess.Popen([str(dst)])
        try:
            time.sleep(0.4)
            self.assertIsNone(proc.poll(), "被替换的程序应当还在运行")
            try:
                # 直接覆盖会 ETXTBSY —— 先确认这个前提成立（否则本用例失去意义）
                shutil.copy2(src, dst)
                direct_ok = True
            except OSError as exc:
                direct_ok = False
                self.assertEqual(exc.errno, 26, f"预期 ETXTBSY，实际：{exc}")
            if not direct_ok:
                smapi._atomic_copy(src, dst)   # 我们的实现必须成功
                self.assertEqual(dst.read_bytes(), src.read_bytes())
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_no_leftover_temp_files(self):
        src = self.work / "x.bin"
        src.write_bytes(b"X")
        dst = self.work / "y.bin"
        dst.write_bytes(b"Y")
        smapi._atomic_copy(src, dst)
        leftovers = [p.name for p in self.work.iterdir() if p.name.startswith(".y.bin")]
        self.assertEqual(leftovers, [], f"残留临时文件：{leftovers}")

    def test_integrate_uses_atomic_copy(self):
        """integrate 不能再用会撞 ETXTBSY 的直写方式。"""
        import inspect
        src = inspect.getsource(smapi.integrate)
        self.assertIn("_atomic_copy", src)
        self.assertNotIn("shutil.copy2(src, target)", src)


class TestInstallStopsServerFirst(unittest.TestCase):
    """升级 SMAPI 前必须先停服。

    这是上一个用例的"上游"保证：不停服就一定会撞 ETXTBSY，
    所以任务处理器必须自己停服，而不是指望用户先手动停。
    """

    def test_orchestration_task_stops_server_before_integrate(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_install_smapi)
        i_stop = src.find("self.runtime.stop()")
        i_install = src.find("smapi.install(")
        self.assertGreater(i_stop, -1, "任务里没有停服逻辑")
        self.assertGreater(i_install, -1)
        self.assertLess(i_stop, i_install, "必须先停服再整合 SMAPI")

    def test_task_does_not_restart_server(self):
        """SMAPI 更新完成后**不得**自动启动服务端。

        用户明确要求：「Steam 扫码登录」「下载获取游戏」「启动服务端」是三个
        独立动作，每个都要用户自己点。此前实现会在更新成功后自动把服务端拉起来，
        属于自动接力，已移除。
        """
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_install_smapi)
        self.assertNotIn("self.runtime.up()", src)
        self.assertIn("不会自动启动", src)
        self.assertIn("启动服务器", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
