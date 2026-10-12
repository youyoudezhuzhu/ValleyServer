"""原生运行时测试：显示栈解析、配置校验、进程归属判定、环境注入。

这些是原生部署的"最容易静默出错"的部分（都是实测踩过的坑）：
  * 缺少 PYTHONPATH → websockify 报 PackageNotFoundError
  * 缺少 XDG_DATA_DIRS → openbox 加载不到主题并**直接退出**
  * 缺少 DISPLAY → openbox 报 Failed to open the display 并退出
  * daemon 化进程（x11vnc -bg / openbox）的 PID 文件不可信
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import xstack  # noqa: E402
from mgr.native_runtime import NativeRuntime  # noqa: E402
from mgr.runtime_base import (  # noqa: E402
    MODE_NATIVE, list_pids_matching, make_runtime, pid_alive, port_conflict_owner,
)
from mgr.util import ValidationError  # noqa: E402


class TestXstackResolution(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        root = xstack.xstack_root()
        if root.exists():
            shutil.rmtree(root)

    def _fake_stack(self, tools=("Xvfb", "x11vnc", "openbox", "websockify")):
        root = xstack.xstack_root()
        bindir = root / "usr" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for name in tools:
            p = bindir / name
            p.write_text("#!/bin/sh\nexit 0\n")
            p.chmod(0o755)
        (root / "usr" / "lib" / "x86_64-linux-gnu").mkdir(parents=True, exist_ok=True)
        (root / "usr" / "lib" / "python3" / "dist-packages").mkdir(parents=True, exist_ok=True)
        (root / "usr" / "share" / "X11" / "xkb").mkdir(parents=True, exist_ok=True)
        (root / "usr" / "share" / "novnc").mkdir(parents=True, exist_ok=True)
        (root / "usr" / "share" / "novnc" / "vnc.html").write_text("<html></html>")
        return root

    def test_not_installed_initially(self):
        """没有应用内捆绑的显示栈时必须报"未安装"。

        注意：不能断言"缺少 Xvfb"——CI 运行器（ubuntu-latest）系统里可能自带
        Xvfb/openbox，那样 missing_required 就不会包含它们。这里改为断言
        「应用内未安装」+「捆绑来源的必需组件没有全部找到」，与运行器环境无关。
        """
        self.assertFalse(xstack.is_installed())
        st = xstack.tool_status()
        self.assertFalse(st["ok"])
        self.assertTrue(st["missing_required"], "未安装捆绑栈时 missing_required 不应为空")
        for name in xstack.REQUIRED_BINARIES:
            self.assertIn(name, st, f"工具状态里缺少 {name} 的条目")
            # 系统里就算装了，也不允许被记为 bundled
            self.assertNotEqual(st[name]["source"], "bundled",
                                f"{name} 不应被识别为捆绑来源（未安装）")

    def test_installed_after_fake_stack(self):
        self._fake_stack()
        self.assertTrue(xstack.is_installed())
        st = xstack.tool_status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["Xvfb"]["source"], "bundled")

    def test_find_tool_prefers_bundled(self):
        self._fake_stack()
        path = xstack.find_tool("Xvfb")
        self.assertTrue(path.startswith(str(xstack.xstack_root())))

    def test_env_injects_all_three_critical_vars(self):
        """PYTHONPATH / XDG_DATA_DIRS / XKB_CONFIG_ROOT 缺一不可。"""
        root = self._fake_stack()
        env = xstack.env_for_tools()
        self.assertIn(str(root / "usr" / "bin"), env["PATH"])
        self.assertIn(str(root / "usr" / "lib" / "x86_64-linux-gnu"), env["LD_LIBRARY_PATH"])
        self.assertIn(str(root / "usr" / "lib" / "python3" / "dist-packages"), env["PYTHONPATH"])
        self.assertIn(str(root / "usr" / "share"), env["XDG_DATA_DIRS"])
        self.assertEqual(env["XKB_CONFIG_ROOT"], str(root / "usr" / "share" / "X11" / "xkb"))

    def test_novnc_webroot(self):
        root = self._fake_stack()
        self.assertEqual(xstack.novnc_webroot(), str(root / "usr" / "share" / "novnc"))

    def test_uninstall(self):
        self._fake_stack()
        self.assertTrue(xstack.is_installed())
        xstack.uninstall()
        self.assertFalse(xstack.is_installed())

    def test_provision_requires_apt(self):
        """没有 apt-get 时必须给出明确提示，而不是静默失败。"""
        original = shutil.which
        shutil.which = lambda n: None if n == "apt-get" else original(n)
        try:
            with self.assertRaises(ValidationError) as ctx:
                xstack.provision()
            self.assertIn("apt-get", str(ctx.exception))
        finally:
            shutil.which = original


class TestRuntimeFactory(unittest.TestCase):
    def test_make_runtime_is_native(self):
        C.ensure_dirs()
        rt = make_runtime()
        self.assertEqual(rt.mode, MODE_NATIVE)
        self.assertIsInstance(rt, NativeRuntime)

    def test_no_docker_mode_in_config(self):
        cfg = C.ManagerConfig()
        self.assertFalse(hasattr(cfg, "mode"), "已移除执行后端切换，配置里不应再有 mode")
        self.assertFalse(hasattr(cfg, "container"), "已移除 Docker 配置段")
        self.assertTrue(hasattr(cfg, "native"))

    def test_old_config_with_docker_keys_ignored(self):
        """旧版（Docker）配置文件里的未知键必须被安全忽略，不能让应用起不来。"""
        C.ensure_dirs()
        C.MANAGER_CONFIG.write_text(json.dumps({
            "version": 1,
            "mode": "docker",
            "container": {"image": "lixeer/valley-server", "game_port": 24642},
            "native": {"display_number": 95},
            "policy": {"keep_auto_snapshots": 7},
        }, ensure_ascii=False), encoding="utf-8")
        cfg = C.load_config()
        self.assertEqual(cfg.native.display_number, 95)
        self.assertEqual(cfg.policy.keep_auto_snapshots, 7)


class TestNativeConfigValidation(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.rt = NativeRuntime()

    def test_validate_reports_missing_stack_and_game(self):
        root = xstack.xstack_root()
        if root.exists():
            shutil.rmtree(root)
        os.environ["TRIM_PKGHOME"]
        game_root = C.GAME_FILES_DIR / "Stardew Valley"
        if game_root.exists():
            shutil.rmtree(game_root)
        report = self.rt.validate(C.load_config())
        self.assertFalse(report["ok"])
        joined = " ".join(report["errors"])
        self.assertIn("显示组件", joined)
        self.assertIn("游戏启动器", joined)

    def test_validate_rejects_bad_ports(self):
        cfg = C.load_config()
        for bad in (0, -1, 70000, "abc", None, True):
            cfg.native.vnc_port = bad
            report = self.rt.validate(cfg)
            self.assertFalse(report["ok"], f"端口 {bad!r} 应被拒绝")

    def test_validate_rejects_bad_vnc_password(self):
        cfg = C.load_config()
        for bad in ("short", "toolongpassword", "1234567"):
            cfg.native.vnc_password = bad
            report = self.rt.validate(cfg)
            self.assertFalse(report["ok"])
            self.assertTrue(any("VNC" in e for e in report["errors"]))

    def test_validate_warns_on_port_conflict_by_other_app(self):
        """端口被**其它应用**占用时应给出警告（-1），而不是误杀。"""
        import socket
        from mgr.runtime_base import invalidate_port_cache
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            invalidate_port_cache()
            self.assertEqual(port_conflict_owner(port), -1)
        finally:
            sock.close()
        # 端口归属结果有 1 秒 TTL 缓存（见 runtime_base）；释放后需先失效再断言，
        # 否则读到的是刚缓存的"被占用"，会误判为产品缺陷。
        invalidate_port_cache()
        self.assertEqual(port_conflict_owner(port), 0)

    def test_port_cache_ttl_is_short_enough_to_be_safe(self):
        """缓存 TTL 必须很短：端口归属会用于冲突提示，太旧会误导用户。"""
        from mgr import runtime_base as rb
        self.assertLessEqual(rb._LISTEN_TTL, 3.0)


class TestProcessOwnership(unittest.TestCase):
    """进程归属判定：只能认自己启动的进程，避免误杀 NAS 上其它应用。"""

    def test_pid_alive_self(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(999999999))

    def test_list_pids_matching_excludes_self(self):
        pids = list_pids_matching(r"python")
        self.assertNotIn(os.getpid(), pids)

    def test_find_by_pattern_rejects_foreign_process(self):
        """一个指向别处目录的同名进程不应被认领（用当前 python 进程模拟）。"""
        C.ensure_dirs()
        root = xstack.xstack_root()
        bindir = root / "usr" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "Xvfb").write_text("#!/bin/sh\n")
        (bindir / "Xvfb").chmod(0o755)
        (bindir / "x11vnc").write_text("#!/bin/sh\n")
        (bindir / "x11vnc").chmod(0o755)
        (bindir / "websockify").write_text("#!/bin/sh\n")
        (bindir / "websockify").chmod(0o755)
        (bindir / "openbox").write_text("#!/bin/sh\n")
        (bindir / "openbox").chmod(0o755)
        rt = NativeRuntime()
        cfg = C.load_config()
        # 自身的 python 进程不会匹配 Xvfb 模式
        spec = next(s for s in rt._specs(cfg) if s.name == "xvfb")
        self.assertEqual(rt._find_by_pattern(spec, cfg), 0)

    def test_resolve_pid_prefers_pidfile(self):
        C.ensure_dirs()
        self._fake_stack()
        rt = NativeRuntime()
        cfg = C.load_config()
        spec = next(s for s in rt._specs(cfg) if s.name == "game")
        spec.pidfile.parent.mkdir(parents=True, exist_ok=True)
        spec.pidfile.write_text(f"{os.getpid()}\n")
        self.assertEqual(rt.resolve_pid("game", cfg), os.getpid())

    @staticmethod
    def _fake_stack():
        root = xstack.xstack_root()
        bindir = root / "usr" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for name in ("Xvfb", "x11vnc", "openbox", "websockify"):
            p = bindir / name
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)


class TestRenderAndPrepare(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        TestProcessOwnership._fake_stack()
        self.rt = NativeRuntime()

    def test_render_describes_native_startup_only(self):
        """渲染出的启动描述里不能出现任何 Docker 运行形式（仅允许说明性文字）。"""
        text = self.rt.render(C.load_config())
        self.assertIn("原生部署", text)
        self.assertIn("StardewModdingAPI", text)
        self.assertIn("DISPLAY=:", text)
        low = text.lower()
        for forbidden in ("docker run", "docker compose", "image:", "container_name",
                          "docker pull"):
            self.assertNotIn(forbidden, low, f"启动描述里不应出现 {forbidden}")

    def test_render_includes_ports_and_paths(self):
        cfg = C.load_config()
        text = self.rt.render(cfg)
        self.assertIn(str(cfg.native.vnc_port), text)
        self.assertIn(str(cfg.native.webvnc_port), text)
        self.assertIn(str(self.rt.mods_dir()), text)

    def test_prepare_links_saves_and_mods(self):
        cfg = C.load_config()
        self.rt.prepare(cfg)
        saves_link = self.rt._game_home(cfg) / ".config" / "StardewValley" / "Saves"
        self.assertTrue(saves_link.is_symlink())
        self.assertEqual(saves_link.resolve(), C.SAVES_DIR.resolve())
        mods_link = self.rt.mods_dir()
        self.assertTrue(mods_link.is_symlink())
        self.assertEqual(mods_link.resolve(), C.MODS_DIR.resolve())

    def test_prepare_merges_existing_mods(self):
        """游戏目录里手工放进去的 MOD 必须并入数据区，不能被丢掉。"""
        game_mods = self.rt.game_root / "Mods"
        game_mods.parent.mkdir(parents=True, exist_ok=True)
        if game_mods.is_symlink():
            game_mods.unlink()
        game_mods.mkdir(parents=True, exist_ok=True)
        (game_mods / "ManualMod").mkdir()
        (game_mods / "ManualMod" / "manifest.json").write_text("{}")
        self.rt.prepare(C.load_config())
        self.assertTrue((C.MODS_DIR / "ManualMod" / "manifest.json").is_file(),
                        "游戏目录里的现有 MOD 未被并入数据区")
        self.assertTrue(self.rt.mods_dir().is_symlink())

    def test_save_args_only_for_existing_valid_save(self):
        self.rt.prepare(C.load_config())
        self.assertEqual(self.rt._save_args(), [])
        self.rt.write_selected_save("..")
        self.assertEqual(self.rt._save_args(), [], "非法存档名不得进入命令行")
        d = C.SAVES_DIR / "Farm_1"
        d.mkdir(parents=True, exist_ok=True)
        self.rt.write_selected_save("Farm_1")
        self.assertEqual(self.rt._save_args(), ["--load-save", "Farm_1"])

    def test_env_sets_display_and_home(self):
        cfg = C.load_config()
        env = self.rt._env(cfg)
        self.assertEqual(env["DISPLAY"], f":{cfg.native.display_number}")
        self.assertEqual(env["HOME"], str(self.rt._game_home(cfg)))
        if cfg.native.software_gl:
            self.assertEqual(env.get("LIBGL_ALWAYS_SOFTWARE"), "1")


class TestReadinessLayers(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        TestProcessOwnership._fake_stack()
        self.rt = NativeRuntime()
        # 清空运行态：PID 文件残留会让 readiness 误判为"有进程在跑"
        if C.RUNDIR.exists():
            shutil.rmtree(C.RUNDIR)
        C.RUNDIR.mkdir(parents=True, exist_ok=True)


    def _fake_running_stack(self, console_text, *, vnc_socket=None):
        """伪造"显示栈 + 游戏都在跑"：把当前 python 进程 PID 写进各 PID 文件。

        readiness 要求 L1 显示栈（Xvfb）在运行且游戏进程存活；测试里用同一个
        真实存活的进程顶替，从而精确验证 L3（SMAPI 日志）判定逻辑。
        """
        self.rt.game_root.mkdir(parents=True, exist_ok=True)
        (self.rt.launcher).write_text("#!/bin/sh\n")
        self.rt.console.parent.mkdir(parents=True, exist_ok=True)
        self.rt.console.write_text(console_text)
        cfg = C.load_config()
        for spec in self.rt._specs(cfg):
            spec.pidfile.parent.mkdir(parents=True, exist_ok=True)
            spec.pidfile.write_text(f"{os.getpid()}\n")
        return cfg

    def test_not_installed_reports_guidance(self):
        if self.rt.game_root.exists():
            shutil.rmtree(self.rt.game_root)
        rd = self.rt.readiness()
        # 没有任何进程、也没有启动器时，应报告"未安装"并给出可操作的说明
        self.assertEqual(rd["phase"], "未安装")
        self.assertIn("游戏本体", rd["detail"])
        self.assertFalse(rd["ready"])
        self.assertEqual(rd["mode"], MODE_NATIVE)

    def test_running_without_smapi_log_is_starting(self):
        self._fake_running_stack("[SMAPI] Waiting for game to launch...\n")
        rd = self.rt.readiness()
        self.assertEqual(rd["phase"], "启动中")
        self.assertFalse(rd["ready"])
        self.assertTrue(rd["layers"]["L1_display_stack"])

    def test_graphics_error_detected(self):
        self._fake_running_stack(
            "[SMAPI] Your graphics card isn't compatible with Stardew Valley.\n")
        rd = self.rt.readiness()
        self.assertEqual(rd["phase"], "启动失败")
        self.assertIn("显示", rd["detail"])

    def test_smapi_loaded_means_ready(self):
        self._fake_running_stack("[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15\n"
                                 "[SMAPI] Mods loaded and ready!\n")
        rd = self.rt.readiness()
        self.assertTrue(rd["layers"]["L1_display_stack"])
        self.assertTrue(rd["layers"]["L3_smapi_loaded"])
        self.assertEqual(rd["phase"], "运行中")
        self.assertTrue(rd["ready"])

    def test_display_stack_down_reported_as_failure(self):
        """只有游戏进程、显示栈没起来 → 必须报"启动失败"并指明原因。"""
        self.rt.game_root.mkdir(parents=True, exist_ok=True)
        (self.rt.launcher).write_text("#!/bin/sh\n")
        self.rt.console.parent.mkdir(parents=True, exist_ok=True)
        self.rt.console.write_text("[SMAPI] SMAPI 4.5.1 ...\n")
        cfg = C.load_config()
        game = next(s for s in self.rt._specs(cfg) if s.name == "game")
        game.pidfile.parent.mkdir(parents=True, exist_ok=True)
        game.pidfile.write_text(f"{os.getpid()}\n")
        rd = self.rt.readiness()
        self.assertEqual(rd["phase"], "启动失败")
        self.assertIn("显示栈", rd["detail"])

    def test_console_log_rotation_is_in_place(self):
        """控制台日志必须原地轮转（游戏进程持有该 fd）。"""
        self.rt.console.parent.mkdir(parents=True, exist_ok=True)
        self.rt.console.write_bytes(b"x" * (6 * 1024 * 1024))
        ino_before = self.rt.console.stat().st_ino
        self.rt._rotate_console()
        self.assertEqual(self.rt.console.stat().st_ino, ino_before,
                         "日志轮转改变了 inode（用了 mv/rename）")
        self.assertLess(self.rt.console.stat().st_size, 6 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDisplayOccupation(unittest.TestCase):
    """显示号占用处理。

    背景（真机踩到的真实问题）：测试用的沙箱 Xvfb 也占了默认的 `:99`，
    它被 daemon 化后没有随测试进程退出，导致用户自己的 Xvfb 一直报
    "Cannot establish any listening sockets / server already running"，
    而管理界面只显示一句"Xvfb 启动后未创建 X socket"，完全看不出被谁占了。
    """

    def setUp(self):
        C.ensure_dirs()
        TestProcessOwnership._fake_stack()
        self.rt = NativeRuntime()
        for name in ("xvfb", "openbox", "x11vnc", "websockify", "game"):
            p = C.RUNDIR / f"{name}.pid"
            p.unlink(missing_ok=True)

    def _cfg(self, display: int):
        cfg = C.load_config()
        cfg.native.display_number = display
        return cfg

    def test_clean_display_returns_empty(self):
        cfg = self._cfg(71)
        Path("/tmp/.X71-lock").unlink(missing_ok=True)
        Path("/tmp/.X11-unix/X71").unlink(missing_ok=True)
        self.assertEqual(self.rt._prepare_display(cfg), "")

    def test_stale_lock_without_process_is_cleaned(self):
        """锁文件在、但没有任何进程用这个显示号 → 陈旧残留，清掉并放行。"""
        cfg = self._cfg(72)
        lock = Path("/tmp/.X72-lock")
        lock.write_text("stale")
        self.assertEqual(self.rt._prepare_display(cfg), "")
        self.assertFalse(lock.exists(), "陈旧的 X 锁文件没有被清理")

    def test_foreign_holder_is_reported_not_killed(self):
        """别人占着显示号时，必须报错而不是把别人的 X server 杀掉。"""
        cfg = self._cfg(73)
        # 用当前进程冒充"占用 :73 的进程"：把命令行的 argv 设成含 ":73"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6)", ":73"])
        try:
            msg = self.rt._prepare_display(cfg)
            self.assertTrue(msg, "有人占用时不应返回可用")
            self.assertIn(":73", msg)
            self.assertIn("显示编号", msg)
            self.assertTrue(holder.poll() is None, "占用者不应被本函数杀掉")
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_our_own_xvfb_is_reused_not_restarted(self):
        """是本应用自己的 Xvfb → 直接复用（返回可用），不报错也不重杀。"""
        cfg = self._cfg(74)
        # 把当前进程写进 xvfb.pid 并让 _owned_by_app 通过 PID 文件路径归属于本应用
        (C.RUNDIR / "xvfb.pid").write_text(f"{os.getpid()}\n")
        spec = next(s for s in self.rt._specs(cfg) if s.name == "xvfb")
        # 让 _display_holders 认为有进程占用 :74（当前进程 argv 不含 :74，这里直接打桩）
        orig = self.rt._display_holders
        self.rt._display_holders = lambda d: [os.getpid()]
        try:
            self.rt._owned_by_app = lambda pid: True
            self.assertEqual(self.rt._prepare_display(cfg), "")
        finally:
            self.rt._display_holders = orig


class TestProbeCache(unittest.TestCase):
    """探测结果缓存：界面每几秒轮询一次，重复扫描 /proc 是纯浪费。"""

    def setUp(self):
        C.ensure_dirs()
        TestProcessOwnership._fake_stack()
        self.rt = NativeRuntime()

    def test_cache_returns_same_object_within_ttl(self):
        self.rt.invalidate_cache()
        a = self.rt.status()
        b = self.rt.status()
        self.assertIs(a, b, "TTL 内应返回同一个缓存对象")

    def test_invalidate_forces_recompute(self):
        self.rt.invalidate_cache()
        a = self.rt.status()
        self.rt.invalidate_cache()
        b = self.rt.status()
        self.assertIsNot(a, b)

    def test_start_invalidates_cache(self):
        """启停后必须立刻失效缓存，否则界面会显示过期状态。"""
        self.rt.invalidate_cache()
        self.rt.status()
        cfg = C.load_config()
        self.rt._spawn = lambda *a, **k: 0        # 不真的起进程
        self.rt.start()
        # 缓存已被清空 → 下次 status() 重新计算
        self.assertEqual(self.rt._probe_cache, {})

    def test_port_cache_is_shared_and_bounded(self):
        from mgr.runtime_base import _LISTEN_CACHE, invalidate_port_cache, _listening_pids
        invalidate_port_cache()
        _listening_pids(5999)
        self.assertIn(5999, _LISTEN_CACHE)
        for p in range(6000, 6140):
            _listening_pids(p)
        self.assertLessEqual(len(_LISTEN_CACHE), 64, "端口缓存必须有上界")
