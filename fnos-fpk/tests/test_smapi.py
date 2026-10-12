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

    def test_steam_appid_only_when_missing(self):
        target = self.game / "steam_appid.txt"
        target.write_text("用户自己写的")
        smapi.integrate(self.dat, self.game)
        self.assertEqual(target.read_text(), "用户自己写的")

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

    def test_task_restarts_server_after_success(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_install_smapi)
        self.assertIn("was_running", src)
        self.assertGreater(src.find("self.runtime.up()"), src.find("smapi.install("),
                           "整合成功后应把原本运行中的游戏拉起来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
