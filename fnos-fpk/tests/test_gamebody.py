"""游戏本体平台识别与校验测试。

背景（真机踩到的真实问题）：用户上传了 **Windows 版**《星露谷物语》，
而本项目跑在 Linux 上。Windows 版**不含 Linux 的 .NET 运行时**
（没有 `libhostfxr.so` / `libcoreclr.so`），装上去以后启动器只报
`You must install .NET to run this application` —— 用户完全联想不到是平台错了。
所以在安装前就把平台判清楚并给出可操作的中文提示。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import gamebody  # noqa: E402


def make_linux_game(root: Path) -> Path:
    """最小可用的"Linux 版自包含游戏"。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Stardew Valley.dll").write_bytes(b"MZ")
    (root / "Stardew Valley.deps.json").write_text(json.dumps({
        "libraries": {"Stardew Valley/1.6.15.24356": {"type": "project"}},
    }), encoding="utf-8")
    (root / "Content").mkdir(exist_ok=True)
    (root / "StardewModdingAPI").write_text("#!/bin/sh\n")
    for name in ("libhostfxr.so", "libcoreclr.so", "libhostpolicy.so",
                 "libclrjit.so", "libSystem.Native.so", "createdump"):
        (root / name).write_bytes(b"\x7fELF")
    (root / "System.Private.CoreLib.dll").write_bytes(b"MZ")
    return root


def make_windows_game(root: Path) -> Path:
    """实测的 Windows 版特征：有 .exe，没有 .so。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Stardew Valley.dll").write_bytes(b"MZ")
    (root / "Stardew Valley.exe").write_bytes(b"MZ")
    (root / "createdump.exe").write_bytes(b"MZ")
    (root / "Galaxy64.dll").write_bytes(b"MZ")
    (root / "Microsoft.DiaSymReader.Native.amd64.dll").write_bytes(b"MZ")
    (root / "Content").mkdir(exist_ok=True)
    return root


class TestPlatformDetection(unittest.TestCase):
    def setUp(self):
        self.work = C.STAGING_DIR / f"gb-{os_getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_detects_linux(self):
        root = make_linux_game(self.work / "linux")
        self.assertEqual(gamebody.detect_platform(root), gamebody.PLATFORM_LINUX)

    def test_detects_windows(self):
        root = make_windows_game(self.work / "win")
        self.assertEqual(gamebody.detect_platform(root), gamebody.PLATFORM_WINDOWS)

    def test_detects_macos(self):
        root = self.work / "mac"
        (root / "Stardew Valley.app").mkdir(parents=True)
        self.assertEqual(gamebody.detect_platform(root), gamebody.PLATFORM_MACOS)

    def test_unknown_when_empty(self):
        root = self.work / "empty"
        root.mkdir(parents=True)
        self.assertEqual(gamebody.detect_platform(root), gamebody.PLATFORM_UNKNOWN)

    def test_missing_dir_is_unknown(self):
        self.assertEqual(gamebody.detect_platform(self.work / "nope"),
                         gamebody.PLATFORM_UNKNOWN)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.work = C.STAGING_DIR / f"gbv-{os_getpid()}"
        shutil.rmtree(self.work, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_linux_game_passes(self):
        root = make_linux_game(self.work / "linux")
        rep = gamebody.inspect(root)
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(rep.platform, gamebody.PLATFORM_LINUX)
        self.assertEqual(rep.info["so_count"], 5)  # 5 个 .so；createdump 不是 .so

    def test_windows_game_is_rejected_with_actionable_message(self):
        root = make_windows_game(self.work / "win")
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.platform, gamebody.PLATFORM_WINDOWS)
        msg = " ".join(rep.errors)
        self.assertIn("Windows", msg)
        self.assertIn("Linux", msg)
        self.assertIn("Stardew Valley.exe", msg)      # 告诉他依据是什么

    def test_macos_game_is_rejected(self):
        root = self.work / "mac"
        (root / "Stardew Valley.app").mkdir(parents=True)
        (root / "Stardew Valley.dll").write_bytes(b"MZ")
        (root / "Content").mkdir(exist_ok=True)
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok)
        self.assertIn("macOS", " ".join(rep.errors))

    def test_missing_main_assembly_rejected(self):
        root = self.work / "noasm"
        make_linux_game(root)
        (root / "Stardew Valley.dll").unlink()
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok)
        self.assertIn("Stardew Valley.dll", " ".join(rep.errors))

    def test_missing_content_rejected(self):
        root = self.work / "nocontent"
        make_linux_game(root)
        shutil.rmtree(root / "Content")
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok)
        self.assertIn("Content", " ".join(rep.errors))

    def test_linux_missing_runtime_is_rejected(self):
        """Linux 版但缺 .NET 运行时 → 不是自包含发布，必须提前拒绝。

        这正是用户那个案例的"Linux 版对照"：如果平台对但运行时不全，
        装上去同样只会在启动时抛一句难懂的错误。
        """
        root = self.work / "thin"
        make_linux_game(root)
        for name in ("libhostfxr.so", "libcoreclr.so"):
            (root / name).unlink()
        # 保留一个 .so 让平台仍判定为 linux
        (root / "libSystem.Native.so").write_bytes(b"\x7fELF")
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok)
        msg = " ".join(rep.errors)
        self.assertIn("自包含", msg)
        self.assertIn("libhostfxr.so", msg)

    def test_real_windows_like_layout_matches_this_project_expectation(self):
        """回归：本机踩到的那个 Windows 目录形态必须被判为不可用。"""
        root = self.work / "realwin"
        make_windows_game(root)
        # 用户还装了 Linux 版 SMAPI（管理器整合进去的），但游戏本体仍是 Windows 的
        (root / "StardewModdingAPI").write_text("#!/bin/sh\n")
        (root / "smapi-internal").mkdir(exist_ok=True)
        rep = gamebody.inspect(root)
        self.assertFalse(rep.ok, "装了 Linux SMAPI 也不能让 Windows 游戏本体通过")
        self.assertEqual(rep.platform, gamebody.PLATFORM_WINDOWS)


def os_getpid() -> int:
    import os
    return os.getpid()




if __name__ == "__main__":
    unittest.main(verbosity=2)
