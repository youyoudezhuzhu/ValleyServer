"""安全测试：路径穿越、命令注入、ZIP 攻击、符号链接、非法参数。

覆盖项目要求 §9.13 / §9.14：
  测试 13：恶意 ZIP 路径、路径穿越、符号链接和异常文件。
  测试 14：错误密码、越权访问、任意命令注入和非法路径参数。
"""

from __future__ import annotations

import os
import stat
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import util  # noqa: E402
from mgr.util import ValidationError  # noqa: E402


class TestNameValidation(unittest.TestCase):
    def test_rejects_traversal_names(self):
        for bad in ("..", ".", "../etc", "a/b", "a\\b", "", "   ", "a\x00b",
                    "C:", "con", "NUL", "a" * 129):
            with self.subTest(name=bad):
                with self.assertRaises(ValidationError):
                    util.validate_name(bad, field="测试名")

    def test_accepts_normal_names(self):
        for good in ("MyFarm_123456789", "存档 A", "mod-name.v2", "a" * 128):
            self.assertEqual(util.validate_name(good), good.strip())

    def test_rejects_control_chars(self):
        with self.assertRaises(ValidationError):
            util.validate_name("a\tb", field="x")

    def test_no_space_option(self):
        with self.assertRaises(ValidationError):
            util.validate_name("a b", allow_space=False)


class TestSafeJoin(unittest.TestCase):
    def setUp(self):
        self.root = Path(ENV["TRIM_PKGHOME"]) / "joinroot"
        self.root.mkdir(parents=True, exist_ok=True)

    def test_normal(self):
        p = util.safe_join(self.root, "sub", "file.txt")
        self.assertTrue(str(p).startswith(str(self.root.resolve())))

    def test_rejects_traversal(self):
        for parts in (("..",), ("a", "..", "..", "etc"), ("/etc",), ("a/b",),
                      ("a\\b",), ("",), (".",), ("..", "..", "x")):
            with self.subTest(parts=parts):
                with self.assertRaises(ValidationError):
                    util.safe_join(self.root, *parts)

    def test_assert_within(self):
        inside = self.root / "a" / "b.txt"
        util.assert_within(inside, self.root)
        with self.assertRaises(ValidationError):
            util.assert_within(Path("/etc/passwd"), self.root)


class TestZipInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(ENV["TRIM_PKGHOME"]) / "ziptests"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def _make_zip(self, name: str, entries: dict[str, bytes]) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arc, data in entries.items():
                zf.writestr(arc, data)
        return path

    def test_normal_zip_ok(self):
        z = self._make_zip("ok.zip", {
            "MySave/SaveGameInfo": b"<xml/>",
            "MySave/MySave_123": b"<xml/>",
        })
        r = util.inspect_zip(z)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.file_count, 2)
        self.assertIn("MySave", r.top_level_dirs)

    def test_rejects_zip_slip_relative(self):
        z = self._make_zip("slip.zip", {"../../evil.txt": b"x"})
        r = util.inspect_zip(z)
        self.assertFalse(r.ok)
        self.assertIn("..", r.reason)

    def test_rejects_absolute_path(self):
        z = self._make_zip("abs.zip", {"/etc/cron.d/evil": b"x"})
        r = util.inspect_zip(z)
        self.assertFalse(r.ok)
        self.assertIn("绝对路径", r.reason)

    def test_rejects_drive_letter(self):
        z = self._make_zip("drive.zip", {"C:/windows/evil.dll": b"x"})
        self.assertFalse(util.inspect_zip(z).ok)

    def test_rejects_backslash_traversal(self):
        z = self._make_zip("bslash.zip", {"a\\..\\..\\evil": b"x"})
        self.assertFalse(util.inspect_zip(z).ok)

    def test_rejects_symlink_entry(self):
        path = self.tmp / "symlink.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
        r = util.inspect_zip(path)
        self.assertFalse(r.ok)
        self.assertIn("符号链接", r.reason)

    def test_rejects_fifo_entry(self):
        path = self.tmp / "fifo.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("pipe")
            info.create_system = 3
            info.external_attr = (stat.S_IFIFO | 0o644) << 16
            zf.writestr(info, b"")
        r = util.inspect_zip(path)
        self.assertFalse(r.ok)
        self.assertIn("特殊文件", r.reason)

    def test_rejects_too_many_files(self):
        entries = {f"d/f{i}.txt": b"x" for i in range(50)}
        z = self._make_zip("many.zip", entries)
        r = util.inspect_zip(z, max_files=10)
        self.assertFalse(r.ok)
        self.assertIn("文件数量过多", r.reason)

    def test_rejects_oversize(self):
        z = self._make_zip("big.zip", {"d/big.bin": b"0" * 4096})
        r = util.inspect_zip(z, max_total=1024)
        self.assertFalse(r.ok)
        self.assertIn("体积过大", r.reason)

    def test_bomb_ratio_flagged(self):
        # 注意 inspect_zip 的实现顺序：先查条目数，再查总量，最后才查压缩比。
        # 因此「高压缩比」要被观测到，总量必须仍在允许上限内。
        z = self._make_zip("bomb.zip", {"d/zero.bin": b"\x00" * (2 * 1024 * 1024)})
        r = util.inspect_zip(z, max_total=8 * 1024 * 1024, max_ratio=10.0)
        self.assertFalse(r.ok, f"高压缩比未被拦截: {r}")
        self.assertIn("压缩比", r.reason)

    def test_not_a_zip(self):
        bad = self.tmp / "notzip.zip"
        bad.write_bytes(b"this is not a zip file")
        self.assertFalse(util.inspect_zip(bad).ok)

    def test_safe_extract_refuses_malicious(self):
        z = self._make_zip("slip2.zip", {"../escape.txt": b"x"})
        dest = self.tmp / "extract"
        with self.assertRaises(ValidationError):
            util.safe_extract_zip(z, dest)
        self.assertFalse((dest.parent / "escape.txt").exists())

    def test_safe_extract_ok_and_modes(self):
        z = self._make_zip("good.zip", {"a/b.txt": b"hello", "a/c/d.txt": b"world"})
        dest = self.tmp / "extract2"
        count = util.safe_extract_zip(z, dest)
        self.assertEqual(count, 2)
        self.assertEqual((dest / "a" / "b.txt").read_bytes(), b"hello")

    def test_reserved_device_name(self):
        z = self._make_zip("dev.zip", {"d/CON": b"x"})
        self.assertFalse(util.inspect_zip(z).ok)


class TestRunnersNoShell(unittest.TestCase):
    def test_rejects_string_argv(self):
        with self.assertRaises(ValidationError):
            util.run("echo hello")
        with self.assertRaises(ValidationError):
            util.run(b"echo hello")

    def test_no_shell_injection(self):
        payload = "a; echo INJECTED; id"
        res = util.run(["printf", "%s", payload])
        self.assertNotIn("uid=", res.stdout)
        self.assertEqual(res.stdout, payload)

    def test_missing_binary_returns_127(self):
        res = util.run(["/definitely/not/a/binary"])
        self.assertEqual(res.returncode, 127)

    def test_timeout_returns_124(self):
        res = util.run(["sleep", "5"], timeout=1)
        self.assertEqual(res.returncode, 124)


class TestNumericValidation(unittest.TestCase):
    def test_ports(self):
        self.assertEqual(util.validate_port(8080), 8080)
        self.assertEqual(util.validate_port("8080"), 8080)
        for bad in (0, -1, 65536, "abc", None, 3.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    util.validate_port(bad)

    def test_bool_rejected_as_port(self):
        with self.assertRaises(ValidationError):
            util.validate_port(True)

    def test_choice(self):
        self.assertEqual(util.validate_choice("a", ["a", "b"]), "a")
        with self.assertRaises(ValidationError):
            util.validate_choice("c", ["a", "b"])


    def test_env_name(self):
        self.assertEqual(util.validate_env_name("GOOD_NAME"), "GOOD_NAME")
        for bad in ("1BAD", "BAD-NAME", "", "A B", "A;B"):
            with self.assertRaises(ValidationError):
                util.validate_env_name(bad)

    def test_relative_yaml_path(self):
        self.assertEqual(util.validate_relative_yaml_path("Mods"), "Mods")
        self.assertEqual(util.validate_relative_yaml_path("a/b"), "a/b")
        for bad in ("/etc", "..", "../x", "~/x", "a b", "a;b", "C:/x", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    util.validate_relative_yaml_path(bad)


class TestDebugRedaction(unittest.TestCase):
    def test_redacts_secrets(self):
        from mgr.diagnostics import Diagnostics as D
        text = (
            "VNC_PASSWORD=supersecret\n"
            "password: hunter2\n"
            'AccessToken": "tok_abcdefghijklmnopqrstuvwxyz"\n'
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
            "token=ghp_" + "a" * 30 + "\n"
        )
        out = D.redact_text(text)
        self.assertNotIn("supersecret", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("tok_abcdefghijklmnopqrstuvwxyz", out)
        self.assertNotIn("ghp_" + "a" * 30, out)
        self.assertIn("[REDACTED", out)

    def test_redacts_config_dict(self):
        from mgr.diagnostics import Diagnostics as D
        data = {"native": {"vnc_password": "12345678", "game_port": 24642},
                "policy": {"keep_auto_snapshots": 10}}
        out = D._redact_config(data)
        self.assertEqual(out["native"]["vnc_password"], "[REDACTED]")
        self.assertEqual(out["native"]["game_port"], 24642)
        self.assertEqual(out["policy"]["keep_auto_snapshots"], 10)


class TestAtomicWrite(unittest.TestCase):
    def test_atomic_write_and_mode(self):
        p = Path(ENV["TRIM_PKGHOME"]) / "atomic" / "f.json"
        util.atomic_write_json(p, {"a": 1}, mode=0o600)
        self.assertEqual(util.read_json(p), {"a": 1})
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        # 再写一次不会残留临时文件
        util.atomic_write_json(p, {"a": 2})
        leftovers = [f.name for f in p.parent.iterdir() if f.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_sha256(self):
        p = Path(ENV["TRIM_PKGHOME"]) / "atomic" / "hash.bin"
        util.atomic_write_bytes(p, b"hello")
        self.assertEqual(
            util.sha256_file(p),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
