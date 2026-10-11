"""存档与 MOD 管理测试。

覆盖项目要求 §9.5 / §9.6，以及安全约束：
  * 存档导入的结构校验、冲突检测、覆盖确认、运行中拒绝写入。
  * MOD manifest 大小写不敏感解析、依赖检查、重复检测、启用/禁用、卸载保留数据。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.mods import ModManager, parse_manifest_file, parse_version, version_ge  # noqa: E402
from mgr.saves import (  # noqa: E402
    SAVE_INFO_NAMES, SaveError, SaveManager, ServerBusy,
)
from mgr.util import ValidationError  # noqa: E402


class FakeResult:
    def __init__(self, ok=True, tail=""):
        self.ok = ok
        self.tail = tail


class FakeRuntime:
    def __init__(self, running=False, exists=True):
        self._status = type("S", (), {"running": running, "exists": exists,
                                      "status": "running" if running else "exited",
                                      "exit_code": 0})()
    def status(self):
        return self._status


def make_save_zip(path: Path, dir_name: str = "Farm_111111111",
                  layout: str = "flat", missing_info: bool = False,
                  extra_bad: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        base = f"{dir_name}/" if layout == "flat" else f"Saves/{dir_name}/"
        if not missing_info:
            zf.writestr(base + "SaveGameInfo",
                        "<SaveGame><farmName><string>TestFarm</string></farmName>"
                        "<name><string>Farmer</string></name>"
                        "<date><season>spring</season><dayOfMonth>5</dayOfMonth>"
                        "<year>1</year></date>"
                        "<money>5000</money><gameVersion>1.6.15</gameVersion>"
                        "</SaveGame>")
        zf.writestr(base + dir_name, "<SaveGame>" + "y" * 300 + "</SaveGame>")
        if extra_bad:
            zf.writestr("../escape.txt", "pwned")
    return path


def make_mod_zip(path: Path, dir_name: str = "TestMod", manifest: dict | None = None,
                 layout: str = "flat") -> Path:
    manifest = manifest or {
        "Name": "TestMod", "Author": "tester", "Version": "1.0.0",
        "Description": "test", "UniqueID": "Tester.TestMod", "EntryDll": "TestMod.dll",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        prefix = "" if layout == "root" else f"{dir_name}/"
        zf.writestr(prefix + "manifest.json", json.dumps(manifest))
        zf.writestr(prefix + "TestMod.dll", "MZ fake")
    return path


class SaveTestBase(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        for d in (C.SAVES_DIR, C.STAGING_DIR, C.RESTORE_TMP_DIR):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        self.uploads = C.STAGING_DIR / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)

    def _seed_save(self, name="Farm_111111111"):
        d = C.SAVES_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SaveGameInfo").write_text(
            "<SaveGame>"
            "<farmName><string>TestFarm</string></farmName>"
            "<name><string>Farmer</string></name>"
            "<date><season>spring</season><dayOfMonth>5</dayOfMonth><year>1</year></date>"
            "<money>5000</money><gameVersion>1.6.15</gameVersion>"
            "</SaveGame>")
        (d / name).write_text("<SaveGame/>")
        return d


class TestSaveListing(SaveTestBase):
    def test_lists_and_parses_metadata(self):
        make_save_zip(self.uploads / "s.zip").unlink()
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        saves = mgr.list_saves()
        self.assertEqual(len(saves), 1)
        self.assertTrue(saves[0].valid)
        self.assertEqual(saves[0].farm_name, "TestFarm")
        self.assertEqual(saves[0].player_name, "Farmer")
        self.assertEqual(saves[0].game_version, "1.6.15")
        self.assertEqual(saves[0].game_date, "1年 spring 第5天")
        self.assertGreater(saves[0].file_count, 0)

    def test_invalid_save_detected(self):
        d = C.SAVES_DIR / "Broken"
        d.mkdir(parents=True)
        (d / "garbage.txt").write_text("nope")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        saves = {s.dir_name: s for s in mgr.list_saves()}
        self.assertFalse(saves["Broken"].valid)
        self.assertTrue(saves["Broken"].problems)

    def test_unknown_files_registered_not_deleted(self):
        d = self._seed_save()
        (d / "user-notes.txt").write_text("keep me")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        info = mgr.inspect("Farm_111111111")
        self.assertIn("user-notes.txt", info.unknown_files)
        self.assertTrue((d / "user-notes.txt").is_file())


class TestSaveImport(SaveTestBase):
    def test_plan_flat(self):
        z = make_save_zip(self.uploads / "ok.zip")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        plan = mgr.plan_import(z)
        self.assertEqual(plan["plausible_candidate"], "Farm_111111111")
        self.assertFalse(plan["needs_manual_choice"])
        self.assertFalse(plan["save_candidates"][0]["conflict"])

    def test_plan_saves_wrapper_dir(self):
        z = make_save_zip(self.uploads / "wrapped.zip", layout="wrapped")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        plan = mgr.plan_import(z)
        self.assertTrue(any(c["plausible"] for c in plan["save_candidates"]))

    def test_import_success(self):
        z = make_save_zip(self.uploads / "ok.zip")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        result = mgr.import_zip(z)
        self.assertEqual(result["dir_name"], "Farm_111111111")
        self.assertTrue((C.SAVES_DIR / "Farm_111111111" / "SaveGameInfo").is_file())

    def test_import_refused_while_running(self):
        z = make_save_zip(self.uploads / "ok.zip")
        mgr = SaveManager(runtime=FakeRuntime(running=True))
        with self.assertRaises(ServerBusy):
            mgr.import_zip(z)
        self.assertFalse((C.SAVES_DIR / "Farm_111111111").exists())

    def test_import_refused_without_savegameinfo(self):
        z = make_save_zip(self.uploads / "noid.zip", missing_info=True)
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError):
            mgr.import_zip(z)

    def test_import_refuses_traversal_zip(self):
        z = make_save_zip(self.uploads / "evil.zip", extra_bad=True)
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError):
            mgr.import_zip(z)
        self.assertFalse((C.STAGING_DIR / "escape.txt").exists())

    def test_import_conflict_requires_overwrite(self):
        z = make_save_zip(self.uploads / "ok.zip")
        self._seed_save()  # 已存在同名
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError) as ctx:
            mgr.import_zip(z)
        self.assertIn("已存在", str(ctx.exception))
        # 覆盖确认后可以导入
        result = mgr.import_zip(z, overwrite=True)
        self.assertTrue(result["overwritten"])

    def test_import_invalid_dir_name(self):
        z = make_save_zip(self.uploads / "ok.zip")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        # 注意：dir_name="" 是「未指定」的语义（自动识别），不是非法值，故不在本用例中
        for bad in ("../etc", "a/b", "..", "x" * 200):
            with self.subTest(bad=bad):
                with self.assertRaises((ValidationError, SaveError)):
                    mgr.import_zip(z, dir_name=bad)

    def test_import_unknown_dir_name(self):
        z = make_save_zip(self.uploads / "ok.zip")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError):
            mgr.import_zip(z, dir_name="NotInZip")


class TestSaveExport(SaveTestBase):
    def test_export_roundtrip(self):
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        dest = C.STAGING_DIR / "out.zip"
        result = mgr.export_zip("Farm_111111111", dest)
        self.assertTrue(dest.is_file())
        self.assertGreater(result["files"], 0)
        self.assertTrue(result["sha256"])
        # 导出文件必须能被重新预检并通过
        plan = mgr.plan_import(dest)
        self.assertTrue(plan["plausible_candidate"])

    def test_export_allowed_while_running(self):
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=True))
        dest = C.STAGING_DIR / "out2.zip"
        mgr.export_zip("Farm_111111111", dest, allow_running=True)
        self.assertTrue(dest.is_file())


class TestSaveDeleteActivate(SaveTestBase):
    def test_delete_requires_confirm(self):
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError):
            mgr.delete("Farm_111111111", confirm="wrong")
        result = mgr.delete("Farm_111111111", confirm="Farm_111111111")
        self.assertFalse((C.SAVES_DIR / "Farm_111111111").exists())
        self.assertTrue(Path(result["moved_to"]).is_dir())  # 现场备份保留

    def test_delete_refused_while_running(self):
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=True))
        with self.assertRaises(ServerBusy):
            mgr.delete("Farm_111111111", confirm="Farm_111111111")
        self.assertTrue((C.SAVES_DIR / "Farm_111111111").is_dir())

    def test_activate_and_read_back(self):
        self._seed_save()
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        self.assertEqual(mgr.active_save(), "")
        mgr.set_active_save("Farm_111111111")
        self.assertEqual(mgr.active_save(), "Farm_111111111")

    def test_activate_refuses_invalid(self):
        d = C.SAVES_DIR / "Broken"
        d.mkdir(parents=True)
        (d / "x.txt").write_text("no")
        mgr = SaveManager(runtime=FakeRuntime(running=False))
        with self.assertRaises(SaveError):
            mgr.set_active_save("Broken")


class ModTestBase(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        for d in (C.MODS_DIR, C.STAGING_DIR, C.RESTORE_TMP_DIR):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        self.uploads = C.STAGING_DIR / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)

    def _seed_mod(self, name="ExistingMod", unique_id="Tester.Existing",
                  version="1.0.0", enabled=True):
        d = C.MODS_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        manifest_name = "manifest.json" if enabled else "manifest.json.disabled-by-valleyserver"
        (d / manifest_name).write_text(json.dumps({
            "Name": name, "Author": "a", "Version": version,
            "UniqueID": unique_id, "EntryDll": f"{name}.dll",
        }))
        (d / f"{name}.dll").write_bytes(b"MZ")
        return d


class TestManifestParsing(ModTestBase):
    def test_case_insensitive(self):
        p = C.PKGHOME / "manifest-upper.json"
        p.write_text(json.dumps({"version": "1.0.0", "name": "X", "author": "a",
                                 "EntryDLl": "X.dll", "UniqueID": "A.B",
                                 "MinimumApiVersion": "4.0.0"}))
        m = parse_manifest_file(p)
        self.assertEqual(m.name, "X")
        self.assertEqual(m.version, "1.0.0")
        self.assertEqual(m.entry_dll, "X.dll")
        self.assertEqual(m.unique_id, "A.B")
        self.assertEqual(m.problems, [])

    def test_missing_fields_reported(self):
        p = C.PKGHOME / "manifest-bad.json"
        p.write_text(json.dumps({"Name": "NoId"}))
        m = parse_manifest_file(p)
        self.assertTrue(any("UniqueID" in x for x in m.problems))
        self.assertTrue(any("EntryDll" in x for x in m.problems))

    def test_invalid_json(self):
        p = C.PKGHOME / "manifest-invalid.json"
        p.write_text("{not json")
        m = parse_manifest_file(p)
        self.assertTrue(m.problems)

    def test_content_pack(self):
        p = C.PKGHOME / "manifest-cp.json"
        p.write_text(json.dumps({"Name": "CP", "UniqueID": "A.CP",
                                 "ContentPackFor": {"UniqueID": "Pathoschild.ContentPatcher"}}))
        m = parse_manifest_file(p)
        self.assertEqual(m.content_pack_for, "Pathoschild.ContentPatcher")
        self.assertFalse(any("EntryDll" in x for x in m.problems))

    def test_version_compare(self):
        self.assertTrue(version_ge("1.6.15", "1.6.0"))
        self.assertTrue(version_ge("4.5.1", "4.5.1"))
        self.assertFalse(version_ge("4.0.0", "4.5.1"))
        self.assertTrue(version_ge("2.0", "1.9.9"))
        self.assertEqual(parse_version("1.2.3-beta"), (1, 2, 3))


class TestModListAndDiagnose(ModTestBase):
    def test_list(self):
        self._seed_mod()
        mgr = ModManager(runtime=FakeRuntime())
        mods = mgr.list_mods()
        self.assertEqual(len(mods), 1)
        self.assertTrue(mods[0].enabled)
        self.assertEqual(mods[0].manifest.unique_id, "Tester.Existing")

    def test_disabled_mod_detected(self):
        self._seed_mod(enabled=False)
        mgr = ModManager(runtime=FakeRuntime())
        mods = mgr.list_mods()
        self.assertFalse(mods[0].enabled)

    def test_duplicate_unique_id(self):
        self._seed_mod("A", "X.Same")
        self._seed_mod("B", "X.Same")
        mgr = ModManager(runtime=FakeRuntime())
        report = mgr.diagnose()
        self.assertTrue(any(i["kind"] == "duplicate_unique_id" for i in report["issues"]))
        self.assertGreaterEqual(report["summary"]["errors"], 1)

    def test_missing_dependency(self):
        self._seed_mod("WithDep", "X.WithDep")
        d = C.MODS_DIR / "WithDep" / "manifest.json"
        raw = json.loads(d.read_text())
        raw["Dependencies"] = [{"UniqueID": "X.Missing", "MinimumVersion": "1.0.0"}]
        d.write_text(json.dumps(raw))
        mgr = ModManager(runtime=FakeRuntime())
        report = mgr.diagnose()
        self.assertTrue(any(i["kind"] == "missing_dependency" for i in report["issues"]))

    def test_dependency_version_unsatisfied(self):
        self._seed_mod("Dep", "X.Dep", version="1.0.0")
        self._seed_mod("User", "X.User")
        d = C.MODS_DIR / "User" / "manifest.json"
        raw = json.loads(d.read_text())
        raw["Dependencies"] = [{"UniqueID": "X.Dep", "MinimumVersion": "2.0.0"}]
        d.write_text(json.dumps(raw))
        mgr = ModManager(runtime=FakeRuntime())
        report = mgr.diagnose()
        self.assertTrue(any(i["kind"] == "dependency_version" for i in report["issues"]))

    def test_private_data_noticed(self):
        self._seed_mod()
        (C.MODS_DIR / "ExistingMod" / "data").mkdir()
        (C.MODS_DIR / "ExistingMod" / "data" / "save.json").write_text("{}")
        mgr = ModManager(runtime=FakeRuntime())
        mods = mgr.list_mods()
        self.assertTrue(mods[0].has_private_data)
        report = mgr.diagnose()
        self.assertTrue(any(i["kind"] == "private_data" for i in report["issues"]))

    def test_empty_dir_warns(self):
        mgr = ModManager(runtime=FakeRuntime())
        report = mgr.diagnose()
        self.assertTrue(any(i["kind"] == "empty" for i in report["issues"]))


class TestModInstall(ModTestBase):
    def test_plan_install(self):
        z = make_mod_zip(self.uploads / "m.zip")
        mgr = ModManager(runtime=FakeRuntime())
        plan = mgr.plan_install(z)
        self.assertEqual(len(plan["mods"]), 1)
        self.assertTrue(plan["mods"][0]["ok"])
        self.assertFalse(plan["mods"][0]["dir_conflict"])

    def test_install_success(self):
        z = make_mod_zip(self.uploads / "m.zip")
        mgr = ModManager(runtime=FakeRuntime())
        result = mgr.install_zip(z)
        self.assertEqual(result["dir_name"], "TestMod")
        self.assertEqual(result["unique_id"], "Tester.TestMod")
        self.assertTrue((C.MODS_DIR / "TestMod" / "manifest.json").is_file())

    def test_install_refused_while_running(self):
        z = make_mod_zip(self.uploads / "m.zip")
        mgr = ModManager(runtime=FakeRuntime(running=True))
        with self.assertRaises(ValidationError):
            mgr.install_zip(z)
        self.assertFalse((C.MODS_DIR / "TestMod").exists())

    def test_install_conflict_needs_overwrite(self):
        self._seed_mod("TestMod", "Tester.TestMod")
        z = make_mod_zip(self.uploads / "m.zip")
        mgr = ModManager(runtime=FakeRuntime())
        with self.assertRaises(ValidationError) as ctx:
            mgr.install_zip(z)
        self.assertIn("已存在", str(ctx.exception))
        result = mgr.install_zip(z, overwrite=True)
        self.assertTrue(result["overwritten"])

    def test_install_rejects_zip_without_manifest(self):
        z = self.uploads / "nomanifest.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("TestMod/readme.txt", "hi")
        mgr = ModManager(runtime=FakeRuntime())
        with self.assertRaises(ValidationError):
            mgr.install_zip(z)

    def test_install_rejects_malicious_zip(self):
        z = self.uploads / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("TestMod/manifest.json", json.dumps(
                {"Name": "T", "UniqueID": "A.B", "EntryDll": "T.dll"}))
            zf.writestr("../../escape.txt", "pwned")
        mgr = ModManager(runtime=FakeRuntime())
        with self.assertRaises((ValidationError, Exception)):
            mgr.install_zip(z)
        self.assertFalse((C.MODS_DIR.parent / "escape.txt").exists())

    def test_install_rejects_multi_mod_zip(self):
        z = self.uploads / "multi.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for name in ("ModA", "ModB"):
                zf.writestr(f"{name}/manifest.json", json.dumps(
                    {"Name": name, "UniqueID": f"A.{name}", "EntryDll": f"{name}.dll"}))
        mgr = ModManager(runtime=FakeRuntime())
        with self.assertRaises(ValidationError):
            mgr.install_zip(z)


class TestModToggleRemove(ModTestBase):
    def test_disable_enable(self):
        self._seed_mod()
        mgr = ModManager(runtime=FakeRuntime())
        r = mgr.set_enabled("ExistingMod", False)
        self.assertTrue(r["changed"])
        self.assertTrue((C.MODS_DIR / "ExistingMod" / "manifest.json.disabled-by-valleyserver").is_file())
        self.assertFalse(mgr.list_mods()[0].enabled)
        r2 = mgr.set_enabled("ExistingMod", True)
        self.assertTrue(r2["changed"])
        self.assertTrue(mgr.list_mods()[0].enabled)

    def test_toggle_idempotent(self):
        self._seed_mod()
        mgr = ModManager(runtime=FakeRuntime())
        mgr.set_enabled("ExistingMod", True)
        r = mgr.set_enabled("ExistingMod", True)
        self.assertFalse(r["changed"])

    def test_remove_keeps_scene_and_private_data(self):
        d = self._seed_mod()
        (d / "data").mkdir()
        (d / "data" / "save.json").write_text('{"x":1}')
        mgr = ModManager(runtime=FakeRuntime())
        result = mgr.remove("ExistingMod", keep_private_data=True)
        self.assertFalse((C.MODS_DIR / "ExistingMod").exists())
        self.assertTrue(Path(result["moved_to"]).is_dir())
        self.assertTrue(result["private_data_kept"])

    def test_remove_refused_while_running(self):
        self._seed_mod()
        mgr = ModManager(runtime=FakeRuntime(running=True))
        with self.assertRaises(ValidationError):
            mgr.remove("ExistingMod")
        self.assertTrue((C.MODS_DIR / "ExistingMod").is_dir())

    def test_install_bundle(self):
        z = self.uploads / "bundle.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for name in ("ModA", "ModB"):
                zf.writestr(f"{name}/manifest.json", json.dumps(
                    {"Name": name, "Version": "1.0.0", "UniqueID": f"A.{name}",
                     "EntryDll": f"{name}.dll"}))
                zf.writestr(f"{name}/{name}.dll", "MZ")
        mgr = ModManager(runtime=FakeRuntime())
        result = mgr.install_bundle(z)
        self.assertEqual(result["count"], 2)
        self.assertTrue((C.MODS_DIR / "ModA").is_dir())
        self.assertTrue((C.MODS_DIR / "ModB").is_dir())


class TestSmapiLogParsing(unittest.TestCase):
    def test_parses_versions_and_errors(self):
        log = "\n".join([
            "[00:00:00 INFO  SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 on .NET 6.0",
            "[00:00:01 INFO  SMAPI] Loading mods...",
            "[00:00:02 INFO  SMAPI] Loaded mod: ALOS (1.0.6 by Lixeer)",
            "[00:00:02 INFO  SMAPI] Loaded mod: Save Backup (4.5.1 by SMAPI)",
            "[00:00:03 ERROR SMAPI] Skipped mod TestMod because it needs a newer version of SMAPI.",
            "[00:00:04 WARN  SMAPI] Something odd",
        ])
        report = ModManager.parse_smapi_log(log)
        self.assertTrue(report["available"])
        self.assertEqual(report["versions"]["smapi"], "4.5.1")
        self.assertEqual(report["versions"]["game"], "1.6.15")
        self.assertEqual(report["loaded_count"], 2)
        self.assertTrue(any("TestMod" in e["line"] or e["mod"] == "TestMod"
                            for e in report["errors"]))
        self.assertTrue(all("确定性" in e["certainty"] for e in report["errors"]))

    def test_empty_log(self):
        report = ModManager.parse_smapi_log("")
        self.assertFalse(report["available"])

    def test_garbage_log(self):
        report = ModManager.parse_smapi_log("random text\nno smapi here")
        self.assertTrue(report["available"])
        self.assertTrue(report["notes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
