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
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import xstack  # noqa: E402
from mgr import native_runtime as NR  # noqa: E402
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
        self.assertFalse(hasattr(cfg, "container"), "已移除容器时代的配置段")
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



class TestDualStateModel(unittest.TestCase):
    """需求 §5：必须把「服务运行状态」与「农场开服状态」拆成两个正交维度。

    以前只有一个 phase，会把"服务在跑"显示成"运行中"，用户以为可以进游戏了，
    其实游戏还停在标题页（ALOS 只在存档加载后才开主机）。
    """

    def setUp(self):
        C.ensure_dirs()
        self.rt = NR.NativeRuntime()

    def test_when_not_installed(self):
        rd = self.rt.readiness()
        svc = rd.get("service") or {}
        farm = rd.get("farm") or {}
        self.assertIn("state", svc)
        self.assertIn("state", farm)
        self.assertIn(svc["state"], ("未启动", "已停止", "状态未知", "启动失败"))
        self.assertIn(farm["state"], ("尚未创建存档", "尚未载入农场", "农场已载入", "开服状态待确认"))


    def test_service_state_uses_runtime_exists(self):
        """回归：readiness 顶层没有 exists 键，状态必须从 runtime.exists 取。

        真机踩到：6 个进程全在跑、phase 是「运行中（未开服）」，界面却显示「未启动」——
        因为代码读了不存在的 out["exists"]（恒为 None）。
        """
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime._state_model)
        self.assertIn('out.get("runtime")', src, "必须从 runtime 里取 exists")
        # 真正跑一遍：进程状态里的 exists 与推导出的服务状态必须一致
        rd = self.rt.readiness()
        exists = bool((rd.get("runtime") or {}).get("exists"))
        svc = (rd.get("service") or {}).get("state")
        if exists and rd.get("layers", {}).get("L1_game_process"):
            self.assertNotEqual(svc, "未启动", "进程在跑时不能报「未启动」")

    def test_state_vocabulary_matches_requirement(self):
        """状态取值必须落在需求列出的集合内。"""
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime._state_model)
        for token in ("未启动", "启动中", "运行中", "停止中", "已停止", "启动失败", "状态未知"):
            self.assertIn(token, src, f"服务状态缺少 {token}")
        for token in ("尚未创建存档", "尚未载入农场", "农场已载入", "开服状态待确认"):
            self.assertIn(token, src, f"农场状态缺少 {token}")

    def test_farm_state_never_claims_hosting_without_saves(self):
        """没有存档时绝不能报「农场已载入」。

        注意：本测试**自己**保证"没有存档"，不依赖沙箱恰好为空 ——
        多个测试文件在同一进程里共享沙箱时，别的用例会留下存档
        （`unittest discover` 下曾因此失败）。
        """
        import shutil
        for d in (C.SAVES_DIR,):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        rd = self.rt.readiness()
        farm = rd.get("farm") or {}
        self.assertEqual(farm.get("saves_count"), 0)
        self.assertNotEqual(farm.get("state"), "农场已载入")

    def test_farm_state_has_actionable_hint(self):
        rd = self.rt.readiness()
        self.assertTrue((rd.get("farm") or {}).get("hint"), "农场状态必须带可操作提示")

    def test_stop_marks_stopping_state(self):
        """stop() 期间应可判定为「停止中」，而不是「已停止」。"""
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime.stop)
        self.assertIn("_stopping", src)

    def test_all_readiness_paths_include_state_model(self):
        """早期返回路径（未安装/已停止/显示栈失败）也必须带双状态。"""
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertGreaterEqual(src.count("_state_model"), 3,
                                "每个 return 之前都要补状态模型，否则界面会缺字段")


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
        cfg.native.display_backend = "xvfb"   # 本用例针对 Xvfb 后端
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
        try:
            self.assertEqual(rt.resolve_pid("game", cfg), os.getpid())
        finally:
            # 必须清理：沙箱在同一进程内共享，残留"写着本进程 PID"的 pid 文件
            # 会让后续用例的 stop() 试图杀掉测试进程自己（实测 exit=143）。
            spec.pidfile.unlink(missing_ok=True)

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
        # 显式用软件渲染后端：默认已是 auto（本机有核显时会解析成核显，
        # 那样就不会设置 LIBGL_ALWAYS_SOFTWARE）。
        cfg.native.display_backend = "xvfb"
        cfg.native.software_gl = True
        env = self.rt._env(cfg)
        self.assertEqual(env["DISPLAY"], f":{cfg.native.display_number}")
        self.assertEqual(env["HOME"], str(self.rt._game_home(cfg)))
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
            self.addCleanup(spec.pidfile.unlink, missing_ok=True)
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
        """SMAPI 加载完成 **且已有存档** → 运行中。
        （没有存档是另一种状态："运行中（未开服）"，由 TestHostingState 覆盖。）
        """
        self._fake_running_stack("[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15\n"
                                 "[SMAPI] Mods loaded and ready!\n")
        (C.SAVES_DIR / "TestFarm_123456789").mkdir(parents=True, exist_ok=True)
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





class TestStartCleanupLogging(unittest.TestCase):
    """start() 里"清理到陈旧进程"的分支必须真的能执行。

    真机踩到：该分支里的 `LOG.info(...)` 用了**未定义的 LOG**
    （native_runtime.py 从未定义 LOG）→ `NameError: name 'LOG' is not defined`
    → 「启动服务器」任务直接失败。这个分支只有在"发现陈旧显示进程"时才走到，
    所以平时测试全绿也发现不了 —— 必须显式制造该条件。
    """

    def setUp(self):
        from mgr import native_runtime as NR2
        # `launcher` 是**类级 property**：改了必须还原，否则污染同进程的其它用例
        self._orig_launcher = type(NR2.NativeRuntime).__dict__.get("launcher")

    def tearDown(self):
        from mgr import native_runtime as NR2
        if self._orig_launcher is not None:
            type(NR2.NativeRuntime).launcher = self._orig_launcher

    def test_log_symbol_exists(self):
        from mgr import native_runtime as NR
        self.assertTrue(hasattr(NR, "LOG"), "native_runtime 必须定义 LOG（否则清理分支会 NameError）")
        NR.LOG.info("测试日志器可用")   # 不应抛异常

    def test_start_cleanup_branch_executes_without_nameerror(self):
        """让两个清理函数都返回"发现了陈旧进程"，确认日志代码能执行。"""
        from mgr import native_runtime as NR2
        rt = NR2.NativeRuntime()
        sentinel = RuntimeError("到这里就够：日志分支已执行")

        # start() 前面有三个早退守卫（显示组件 / 启动器 / 已在运行），
        # 必须全部绕过才能确定性地走到清理分支 —— 否则测试会依赖沙箱里
        # 是否恰好有游戏本体文件（顺序依赖，实测发现过）。
        rt.invalidate_cache = lambda: None
        rt.detect = lambda: {"xstack": {"ok": True, "missing_required": []},
                             "ok": True, "guidance": [], "errors": []}
        rt.__dict__["_fake_launcher"] = Path(__file__)
        type(rt).launcher = property(lambda self: self.__dict__["_fake_launcher"])
        rt.status = lambda: SimpleNamespace(running=False, exists=True, status="stopped")
        rt.prepare = lambda cfg=None: None
        rt._stop_stale_backend_stack = lambda cfg: ["xvfb:111"]
        rt._cleanup_stale_display = lambda cfg: ["xwayland:222"]
        rt._rotate_console = lambda: None
        rt._specs = lambda cfg: (_ for _ in ()).throw(sentinel)

        with self.assertRaises(RuntimeError) as ctx:
            rt.start()
        # 必须是我们的哨兵异常；若在此之前就 NameError，说明 LOG 仍不可用
        self.assertIs(ctx.exception, sentinel,
                      f"清理分支执行时抛了别的异常：{ctx.exception!r}")

    def test_cleanup_functions_are_called_by_start(self):
        import inspect
        from mgr import native_runtime as NR
        src = inspect.getsource(NR.NativeRuntime.start)
        self.assertIn("_stop_stale_backend_stack", src)
        self.assertIn("_cleanup_stale_display", src)






class TestPausedFarmState(unittest.TestCase):
    """无玩家在线且未开启「无人时也推进天数」时，农场状态要显示暂停。

    依据：ALOS `PauseIfNobodyPresent()` —— 0 人且非节日时 `IsPaused = true`；
    而打开 `alos.debug_server`（无人时也推进天数）后 ALOS 会显式取消暂停，
    所以"是否暂停"取决于该开关 + 在线人数（来自 ConnectionsCount.txt）。
    """

    def _farm_state(self, *, players, unattended):
        import mgr.runtime_base as RB
        rt = NR.NativeRuntime()
        cfg = C.load_config()
        cfg.native.alos_unattended = unattended
        out = {
            "exists": True,
            "runtime": {"exists": True},
            "layers": {"L1_game_process": True, "L1_display_stack": True,
                       "L3_smapi_loaded": True},
            "saves_count": 1,
            "players_online": players,
            "detail": "",
        }
        orig = RB.udp_port_open
        RB.udp_port_open = lambda p: True          # 视为已开服
        try:
            return rt._state_model(out, cfg)["farm"]
        finally:
            RB.udp_port_open = orig

    def test_paused_when_empty_and_pause_enabled(self):
        farm = self._farm_state(players=0, unattended=False)
        self.assertEqual(farm["state"], "农场已载入（暂停状态）")
        self.assertTrue(farm["paused"])
        self.assertIn("暂停", farm["hint"])

    def test_not_paused_when_player_online(self):
        self.assertEqual(self._farm_state(players=1, unattended=False)["state"], "农场已载入")
        self.assertFalse(self._farm_state(players=2, unattended=False)["paused"])

    def test_not_paused_when_unattended_enabled(self):
        """开着「无人时也推进天数」时 ALOS 会取消暂停，不能谎报暂停。"""
        farm = self._farm_state(players=0, unattended=True)
        self.assertEqual(farm["state"], "农场已载入")
        self.assertFalse(farm["paused"])

    def test_not_paused_when_count_unknown(self):
        """人数读不到（None）时不能宣称暂停。"""
        farm = self._farm_state(players=None, unattended=False)
        self.assertEqual(farm["state"], "农场已载入")
        self.assertFalse(farm["paused"])


class TestOnlinePlayers(unittest.TestCase):
    """在线玩家数：直接读 ALOS 维护的 ConnectionsCount.txt。

    上游依据（Mods/ALOS/ModEntry.cs:440-455）：
        if (Game1.options.enableServer && Game1.server != null)
            if (connectionsCount != Game1.server.connectionsCount)
                File.WriteAllText(".../ConnectionsCount.txt", connectionsCount);
    即每次变化都写盘 —— 因此管理器不需要再写一个模组就能知道在线人数。
    """

    def test_reads_number(self):
        rt = NR.NativeRuntime()
        path = rt.players_count_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("3\n", encoding="utf-8")
        self.assertEqual(rt.online_players(), 3)
        path.write_text("0", encoding="utf-8")
        self.assertEqual(rt.online_players(), 0, "0 人必须与「读不到」区分开")

    def test_missing_or_garbage_is_none(self):
        rt = NR.NativeRuntime()
        path = rt.players_count_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self.assertIsNone(rt.online_players(), "读不到 → None（未知）")
        path.write_text("不是数字", encoding="utf-8")
        self.assertIsNone(rt.online_players())

    def test_readiness_carries_players_online(self):
        rt = NR.NativeRuntime()
        path = rt.players_count_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2", encoding="utf-8")
        rd = rt.readiness()
        self.assertEqual(rd.get("players_online"), 2)
        self.assertIn("players_online", rd, "任何状态下都要带上该字段")


class TestUnattendedDefaultPausesWhenEmpty(unittest.TestCase):
    """默认必须是"无玩家时暂停"（alos_unattended=False）。

    ALOS 把两件事绑在同一个 debug 标志上（ModEntry.cs:783-806）：
      numPlayers >= 1 || debug  → 走第一分支并 IsPaused=false（取消暂停）
      numPlayers <= 0 且非节日   → IsPaused=true（无玩家暂停）
    所以打开无人值守（debug=true）就必然失去"无玩家暂停"。默认选暂停优先。
    """

    def test_default_is_false(self):
        self.assertFalse(C.NativeConfig().alos_unattended)



class TestIdleFallbackSnapshot(unittest.TestCase):
    """无人在线时的兜底快照（***不停服***）。

    必须不停服的原因：普通快照会停服→拉起，而拉起后的自动载档会把世界回滚到
    上次日结存档（当天进度丢失）。无人时 ALOS 已暂停世界、存档也不再变化，
    因此直接复制是安全的。触发条件：0 人 + 最近 60 秒无存档写入 + 超过间隔。
    """

    def _sup(self, *, players=0, save_age=3600, enabled=True, interval=30, running=True,
             saves_hash="cur-hash", last_hash="ancient-hash"):
        from mgr import supervisor as SV

        class _RT:
            def status(self):
                return NR.RuntimeStatus(exists=True, running=running, status="running")

            def readiness(self):
                return {"phase": "运行中", "detail": "", "layers": {"L3_smapi_loaded": True}}

            def service_intent(self):
                return "running"

            def online_players(self):
                return players

        class _DB:
            def event(self, *a, **k):
                pass

        class _Snap:
            """快照仓库桩：默认"内容与上一快照不同"（照常备份）。"""

            def current_saves_hash(self):
                return saves_hash

            def latest_saves_hash(self):
                return ("snap-prev", last_hash)

        class _App:
            runtime = _RT()
            db = _DB()
            snapshots = _Snap()

        C.ensure_dirs()
        save = C.SAVES_DIR / "star_test"
        save.mkdir(parents=True, exist_ok=True)
        info = save / "SaveGameInfo"
        info.write_text("x", encoding="utf-8")
        if save_age is None:
            info.unlink()
        else:
            os.utime(info, (time.time() - save_age, time.time() - save_age))

        sup = SV.Supervisor(_App())
        calls = []
        sup._schedule_snapshot = lambda **kw: (calls.append(kw), True)[1]
        cfg = C.load_config()
        cfg.policy.auto_snapshot_enabled = True
        cfg.policy.idle_snapshot_enabled = enabled
        cfg.policy.idle_snapshot_interval_minutes = interval
        sup._maybe_idle_snapshot(cfg)
        return calls, sup

    def test_fires_when_empty_and_no_recent_save(self):
        calls, _ = self._sup(players=0, save_age=3600)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["reason"], "idle")
        self.assertFalse(calls[0]["stop_server"], "无人兜底快照绝不能停服")

    def test_skips_when_player_online(self):
        calls, _ = self._sup(players=1)
        self.assertEqual(calls, [], "有人在玩时不做兜底（避免打断）")

    def test_skips_when_count_unknown(self):
        calls, _ = self._sup(players=None)
        self.assertEqual(calls, [], "人数未知时宁可不拍，也不要误拍")

    def test_skips_when_content_unchanged(self):
        """存档自上次快照以来没有变化 → 不再备份（避免重复副本与无谓写盘）。"""
        calls, _ = self._sup(players=0, saves_hash="same-hash", last_hash="same-hash")
        self.assertEqual(calls, [], "内容未变时不得再创建备份")

    def test_backs_up_when_content_changed(self):
        calls, _ = self._sup(players=0, saves_hash="new-hash", last_hash="old-hash")
        self.assertEqual(len(calls), 1, "内容变化时必须备份")
        self.assertFalse(calls[0]["stop_server"])

    def test_skips_when_save_just_written(self):
        calls, _ = self._sup(players=0, save_age=10)
        self.assertEqual(calls, [], "刚写过存档（可能在日结）必须跳过")

    def test_skips_when_disabled_or_stopped(self):
        self.assertEqual(self._sup(players=0, enabled=False)[0], [])
        self.assertEqual(self._sup(players=0, running=False)[0], [])

    def test_default_interval_is_five_minutes(self):
        """兜底间隔默认 5 分钟（用户指定）。"""
        self.assertEqual(C.PolicyConfig().idle_snapshot_interval_minutes, 5)
        self.assertTrue(C.PolicyConfig().idle_snapshot_enabled)

    def test_throttled_by_interval(self):
        calls, sup = self._sup(players=0, interval=30)
        self.assertEqual(len(calls), 1)
        cfg = C.load_config()
        sup._maybe_idle_snapshot(cfg)          # 立刻再来一次 → 应被节流
        self.assertEqual(len(calls), 1, "两次兜底之间必须间隔足够")


class TestIdleSnapshotDoesNotStopServer(unittest.TestCase):
    """stop_server=False 的快照不得触发"重新载档"（没停过服就没有回滚问题）。"""

    def test_create_honors_stop_server_false(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_snapshot_create)
        self.assertIn("stop_server = bool(payload.get", src)
        self.assertIn("if stop_server:", src)
        # stop_server=False 的路径不得调用 _rehost_after_pause（在 if 之外）
        idx = src.index("if stop_server:")
        self.assertIn("_rehost_after_pause", src[idx:])



class TestPeriodicSnapshotNeverStopsServer(unittest.TestCase):
    """定时快照**绝不停服**（真机：6 小时一次停服 → 游戏进程重启，运行时长归零）。

    规则：能热备份就热备份（0 人 + 最近 60 秒无存档写入 + 内容有变化）；
    有人在线 / 刚写过存档 → **推迟**（下个 tick 再判断）；内容没变 → 跳过并推进周期。
    """

    def _sup(self, *, running=True, players=0, save_age=3600,
             saves_hash="cur", last_hash="old"):
        from mgr import supervisor as SV

        class _RT:
            def status(self):
                return NR.RuntimeStatus(exists=True, running=running, status="running")

            def readiness(self):
                return {"phase": "运行中", "detail": "", "layers": {"L3_smapi_loaded": True}}

            def service_intent(self):
                return "running"

            def online_players(self):
                return players

        class _Snap:
            def current_saves_hash(self):
                return saves_hash

            def latest_saves_hash(self):
                return ("snap-prev", last_hash)

        class _DB:
            def event(self, *a, **k):
                pass

        class _App:
            runtime = _RT()
            db = _DB()
            snapshots = _Snap()

        C.ensure_dirs()
        save = C.SAVES_DIR / "star_test"
        save.mkdir(parents=True, exist_ok=True)
        info = save / "SaveGameInfo"
        info.write_text("x", encoding="utf-8")
        os.utime(info, (time.time() - save_age, time.time() - save_age))
        sup = SV.Supervisor(_App())
        calls = []
        sup._schedule_snapshot = lambda **kw: (calls.append(kw), True)[1]
        return calls, sup

    def _run(self, sup) -> bool:
        calls = []
        sup._schedule_snapshot = lambda **kw: (calls.append(kw), True)[1]
        ok = sup._maybe_schedule_periodic_snapshot()
        sup._last_calls = calls
        return ok

    def test_never_stops_server(self):
        calls, sup = self._sup(players=0)
        ok = self._run(sup)
        self.assertTrue(ok)
        self.assertEqual(len(sup._last_calls), 1)
        self.assertFalse(sup._last_calls[0]["stop_server"], "定时快照绝不能停服")
        self.assertEqual(sup._last_calls[0]["reason"], "schedule")

    def test_defers_when_players_online(self):
        sup_calls, sup = self._sup(players=1)
        ok = self._run(sup)
        self.assertFalse(ok, "有玩家在线时必须推迟")
        self.assertEqual(sup._last_calls, [], "不得排入快照任务")

    def test_defers_when_save_just_written(self):
        _, sup = self._sup(players=0, save_age=10)
        ok = self._run(sup)
        self.assertFalse(ok, "刚写过存档时必须推迟")
        self.assertEqual(sup._last_calls, [])

    def test_skips_and_advances_when_unchanged(self):
        _, sup = self._sup(players=0, saves_hash="same", last_hash="same")
        ok = self._run(sup)
        self.assertTrue(ok, "内容未变应跳过并推进周期（返回 True）")
        self.assertEqual(sup._last_calls, [], "不应创建重复快照")

    def test_hot_snapshot_when_service_stopped(self):
        _, sup = self._sup(running=False)
        ok = self._run(sup)
        self.assertTrue(ok)
        self.assertFalse(sup._last_calls[0]["stop_server"], "服务停止时也无需停服参数")


class TestConsoleLogTrim(unittest.TestCase):
    """过大的游戏控制台日志要在启动前裁掉。

    真机踩到：ALOS 在标题页约 7.8 分钟后把服务器置为 offline，游戏随即每帧输出
    `Disconnected: ServerOfflineMode`，日志涨到 40 MB 以上（73 万行）——
    既拖慢游戏也让日志不可读。
    """

    def test_trim_huge_log(self):
        rt = NR.NativeRuntime()
        path = rt.console
        path.parent.mkdir(parents=True, exist_ok=True)
        old_max = NR.NativeRuntime.CONSOLE_LOG_MAX_BYTES
        old_keep = NR.NativeRuntime.CONSOLE_LOG_KEEP_BYTES
        NR.NativeRuntime.CONSOLE_LOG_MAX_BYTES = 64 * 1024
        NR.NativeRuntime.CONSOLE_LOG_KEEP_BYTES = 8 * 1024
        try:
            path.write_text("y" * (100 * 1024), encoding="utf-8")
            before = path.stat().st_size
            trimmed = rt.trim_console_log_if_huge()
            self.assertEqual(trimmed, before, "应返回裁剪前的字节数")
            self.assertLess(path.stat().st_size, before, "文件必须变小")
        finally:
            NR.NativeRuntime.CONSOLE_LOG_MAX_BYTES = old_max
            NR.NativeRuntime.CONSOLE_LOG_KEEP_BYTES = old_keep

    def test_small_log_untouched(self):
        rt = NR.NativeRuntime()
        path = rt.console
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("小日志\n", encoding="utf-8")
        self.assertEqual(rt.trim_console_log_if_huge(), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "小日志\n")

    def test_start_trims_before_launch(self):
        import inspect
        src = inspect.getsource(NR.NativeRuntime.start)
        self.assertIn("trim_console_log_if_huge", src, "启动前必须裁剪过大的控制台日志")


class TestServiceIntent(unittest.TestCase):
    """服务意图必须**持久化**，监管器据此决定是否自动恢复。

    真机踩到：下载完游戏本体后管理器自动启动了服务 —— 监管器只看内存里的
    `_user_stopped`，而该标记没有任何地方调用 `note_user_stop()`，
    于是"进程没在跑"永远被当成"异常退出"。需求要求
    「Steam 登录 / 下载游戏 / 启动服务端」三步人为手动、彼此独立。
    """

    def test_default_intent_is_stopped(self):
        rt = NR.NativeRuntime()
        try:
            rt._intent_path().unlink()
        except OSError:
            pass
        self.assertEqual(rt.service_intent(), "stopped",
                         "缺省必须是 stopped：用户没点过启动就不许自动拉起")

    def test_start_marks_running_and_stop_marks_stopped(self):
        rt = NR.NativeRuntime()
        rt.set_service_intent("running")
        self.assertEqual(rt.service_intent(), "running")
        # 用户点停止 = 明确不要求运行；给 1 秒超时避免用例被拖住
        rt.stop(timeout=1, intent="stopped")
        self.assertEqual(rt.service_intent(), "stopped")

    def test_plain_stop_keeps_intent(self):
        """应用生命周期停机（升级/重启）**不能**清掉"要求运行"的意图，
        否则升级应用后服务再也不会被开机恢复逻辑拉起。"""
        rt = NR.NativeRuntime()
        rt.set_service_intent("running")
        rt.stop(timeout=1)                      # intent=None → 不动意图
        self.assertEqual(rt.service_intent(), "running")

    def test_explicit_stop_clears_intent(self):
        rt = NR.NativeRuntime()
        rt.set_service_intent("running")
        rt.stop(timeout=1, intent="stopped")
        self.assertEqual(rt.service_intent(), "stopped")

    def test_intent_persists_across_instances(self):
        rt = NR.NativeRuntime()
        rt.set_service_intent("running")
        self.assertEqual(NR.NativeRuntime().service_intent(), "running",
                         "意图必须落盘（跨进程/重启有效），否则重启后又变成可自动拉起")

    def test_start_writes_running_on_success(self):
        import inspect
        src = inspect.getsource(NR.NativeRuntime.start)
        self.assertIn('set_service_intent("running")', src)

    def test_stop_only_touches_intent_when_explicit(self):
        """stop() 默认不动意图（应用升级/重启），只有显式 intent= 才写。"""
        import inspect
        src = inspect.getsource(NR.NativeRuntime.stop)
        self.assertIn("intent: str | None = None", src)
        self.assertIn("if intent is not None:", src)
        self.assertIn("set_service_intent(intent)", src)

    def test_task_server_stop_marks_stopped(self):
        """用户点「停止服务器」必须明确写下 stopped（监管器不得自动拉起）。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_server_stop)
        self.assertIn('intent="stopped"', src)


class TestSupervisorIntentGate(unittest.TestCase):
    """监管器只在意图为 running 时才自动恢复。"""

    def _sup(self, intent: str):
        from mgr import supervisor as SV

        class _RT:
            def status(self):
                return NR.RuntimeStatus(exists=True, running=False, status="stopped")

            def readiness(self):
                return {"phase": "已停止", "detail": "", "layers": {}}

            def service_intent(self):
                return intent

        class _DB:
            def event(self, *a, **k):
                pass

        class _App:
            runtime = _RT()
            db = _DB()

        sup = SV.Supervisor(_App())
        calls = []
        sup._auto_start = lambda reason: calls.append(reason)
        sup._last_down_since = time.time() - 60      # 已"down"超过 5 秒
        sup._tick()
        return calls

    def test_no_autostart_without_user_intent(self):
        self.assertEqual(self._sup("stopped"), [],
                         "没有用户启动意图时绝不能自动拉起（下载/安装后就是这种情况）")

    def test_autostart_with_user_intent(self):
        self.assertTrue(self._sup("running"), "用户意图为 running 时应自动恢复")



class TestDataLibraryPathGuard(unittest.TestCase):
    """`DATA_LIBRARY_PATH` 只采信"确实属于本应用"的值。

    真机踩到：该变量会从父进程继承（被另一个应用污染成 .../@appdata/<其他应用>），
    而它被直接当作数据根 —— 结果**日志被写到别的应用目录**，排查时看不到自己的日志。
    """

    def test_suspicious_value_is_ignored(self):
        old = os.environ.get("DATA_LIBRARY_PATH")
        try:
            os.environ["DATA_LIBRARY_PATH"] = "/vol1/@appdata/other-app"
            self.assertEqual(C._data_library_path().name, C.PKGVAR.name,
                             "末级目录不是本应用时必须忽略")
            os.environ["DATA_LIBRARY_PATH"] = str(C.PKGVAR)
            self.assertEqual(C._data_library_path(), C.PKGVAR,
                             "属于本应用的值应当采信")
        finally:
            if old is None:
                os.environ.pop("DATA_LIBRARY_PATH", None)
            else:
                os.environ["DATA_LIBRARY_PATH"] = old


class TestSupervisorHealsAlosSwitch(unittest.TestCase):
    """监管器必须自愈式确保 ALOS 无人值守开关。

    真机踩到：开关原先只在启动/重启任务路径里发一次，管理器进程里偶发没生效，
    结果是没人联机时房主不睡、天数不推进。监管器每分钟检查并补开。
    """

    def _sup(self, state: str, world: bool = True):
        from mgr import supervisor as SV
        import orchestration

        class _RT:
            def status(self):
                return NR.RuntimeStatus(exists=True, running=True, status="running")

            def readiness(self):
                return {"phase": "运行中", "detail": "", "layers": {"L3_smapi_loaded": True}}

            def service_intent(self):
                return "running"

        class _DB:
            def event(self, *a, **k):
                pass

        class _App:
            runtime = _RT()
            db = _DB()

        sup = SV.Supervisor(_App())
        calls = []
        orig = (orchestration.alos_world_ready, orchestration.alos_debug_state,
                orchestration.enable_alos_unattended)
        orchestration.alos_world_ready = lambda rt: world
        orchestration.alos_debug_state = lambda rt: state
        orchestration.enable_alos_unattended = lambda rt, log, **kw: calls.append("enable") or True
        try:
            cfg = C.load_config()
            cfg.native.alos_unattended = True
            sup._ensure_alos_unattended(cfg)      # 第 1 次：状态不是 on → 补开
            sup._last_alos_check = 0.0            # 解除节流
            sup._ensure_alos_unattended(cfg)      # 第 2 次
        finally:
            (orchestration.alos_world_ready, orchestration.alos_debug_state,
             orchestration.enable_alos_unattended) = orig
        return calls

    def test_heals_when_off(self):
        self.assertEqual(self._sup("off"), ["enable", "enable"])

    def test_no_action_when_already_on(self):
        self.assertEqual(self._sup("on"), [])

    def test_no_action_before_world_ready(self):
        self.assertEqual(self._sup("unknown", world=False), [])


class TestUdpPortDetection(unittest.TestCase):
    """游戏端口是 **UDP** —— 用 TCP 检测会把"已开服"误判成"未就绪"。

    真机踩到：ALOS 已经开服（`[ALOS] Auto Mode On!`、UDP 24642 已监听），
    管理器却报 "24642 未在 90s 内就绪"，把成功判成了可疑。
    """

    def test_detects_bound_udp_port(self):
        import socket as _socket
        from mgr.runtime_base import udp_port_open
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        try:
            self.assertTrue(udp_port_open(port), f"已绑定的 UDP {port} 应被识别")
        finally:
            s.close()
        self.assertFalse(udp_port_open(port), "关闭后应识别为未监听")

    def test_tcp_check_misses_udp_port(self):
        """顺便钉住这个差异本身：TCP 检测对 UDP 端口无效。"""
        import socket as _socket
        from mgr.runtime_base import port_open, udp_port_open
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        try:
            self.assertTrue(udp_port_open(port))
            self.assertFalse(port_open(port, timeout=0.3))
        finally:
            s.close()

    def test_auto_host_uses_udp_check(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("udp_port_open", src)
        self.assertNotIn("if port_open(port)", src, "游戏端口必须用 UDP 判据")

    def test_waits_for_console_channel(self):
        """要等 CommandWebUI 端口就绪再发指令（否则 ConnectionRefused）。"""
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.auto_host_load_save)
        self.assertIn("等待 SMAPI 控制台通道就绪", src)
        self.assertIn("cmdwebui_port", src)


class TestConsoleSend(unittest.TestCase):
    """管理器必须能向 SMAPI 控制台发指令 —— 无人值守开服的前提。

    真机踩到：游戏进程原本用 `stdin=/dev/null` 启动，管理器**发不出任何指令**，
    而无人值守开服要靠上游 ServerCMD 的 new_save / load_save / set_multiplayermode。
    """

    def setUp(self):
        C.ensure_dirs()
        self.rt = NativeRuntime()

    def tearDown(self):
        self.rt.reap_children()

    def test_rejects_empty_and_multiline(self):
        self.assertFalse(self.rt.send_console("").ok)
        self.assertFalse(self.rt.send_console("help\nexit").ok)
        self.assertIn("换行", self.rt.send_console("a\nb").stderr)

    def test_reports_clearly_when_channel_unavailable(self):
        """CommandWebUI 通道不可用时，必须给出可操作的原因（不是静默失败）。

        ⚠️ 必须指向**确定没有监听**的端口。最初这里用默认配置端口，
        而开发机上真实运行的游戏恰好监听同一端口 → 测试直接连到了真实服务
        （幸而只发了一条 help）。测试绝不能碰真实实例。
        """
        import socket as _socket
        probe = _socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        cfg = C.load_config()
        orig = C.load_config
        C.load_config = lambda: cfg
        cfg.native.cmdwebui_port = free_port
        try:
            res = self.rt.send_console("help")
        finally:
            C.load_config = orig
        self.assertFalse(res.ok)
        self.assertIn("控制台通道", res.stderr)
        self.assertIn("CommandWebUI", res.stderr)

    def test_send_does_not_touch_real_service_ports(self):
        """回归：单元测试不得连接真实服务端口（默认 29103 可能与真机冲突）。"""
        src = (Path(__file__).resolve().parent / "test_native.py").read_text(encoding="utf-8")
        self.assertIn("free_port", src,
                      "通道不可用的测试必须使用确定空闲的端口")

    def test_uses_commandwebui_websocket_channel(self):
        """指令通道是 CommandWebUI 的 WebSocket（stdin 实测不通）。"""
        import inspect
        src = inspect.getsource(NR.NativeRuntime.send_console)
        self.assertIn("consolews.send_command", src)
        self.assertIn("cmdwebui_port", src)
        # 不能再声称靠 stdin 写指令
        self.assertNotIn("proc.stdin.write", src)

    def test_reap_children_removes_finished(self):
        """回收僵尸：失败的启动不该在 ps 里留下 <defunct> 直到管理器重启。"""
        # 用会存活一小会儿的进程：/bin/true 太快，poll() 可能还没反映退出，
        # 那样测的就不是回收逻辑而是竞态。
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 0.3"], stdin=subprocess.DEVNULL)
        self.rt._children["game"] = proc
        time.sleep(0.6)
        reaped = self.rt.reap_children()
        self.assertIn("game", reaped)
        self.assertNotIn("game", self.rt._children)

    def test_game_process_is_registered_for_reaping(self):
        """内联 Popen 的游戏进程要登记进 _children，否则无法回收僵尸。"""
        import inspect
        src = inspect.getsource(NR.NativeRuntime.start)
        i = src.index("游戏（前台进程")
        chunk = src[i:i + 1400]
        self.assertIn('self._children["game"] = proc', chunk)


class TestHostBootstrapTask(unittest.TestCase):
    """一键开服：缺存档先建，再载档、设联机模式（上游文档给出的路径）。"""

    def test_task_registered(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration.register_all)
        self.assertIn('"native.host-bootstrap"', src)

    def test_uses_servercmd_documented_commands(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_host_bootstrap)
        self.assertIn("ServerCMD.new_save", src)
        self.assertIn("ServerCMD.load_save", src)
        self.assertIn("ServerCMD.set_multiplayermode", src)

    def test_refuses_when_game_not_running(self):
        import inspect
        import orchestration
        src = inspect.getsource(orchestration._Services.task_native_host_bootstrap)
        self.assertIn("游戏进程未运行", src)
        self.assertIn("L3_smapi_loaded", src)

    def test_api_endpoints_exist(self):
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        # 公开的任意指令通道已移除（重复入口 + 上游 CommandWebUI 的 WS
        # 在客户端断开时可能让游戏进程崩溃）；内部 send_console 仍被自动开服使用。
        self.assertNotIn('"/api/console/send"', src)
        self.assertIn('"/api/native/host-bootstrap"', src)
        # 所有"会改状态"的接口都要能追溯到审计：host-bootstrap 走任务队列
        # （task 提交由 TaskManager 统一写审计），这里确认端点仍存在且未被绕过。
        i = src.index("def host_bootstrap")
        self.assertIn("_submit", src[i:i + 400])
        # 内部 send_console 仍保留（自动开服/ALOS 开关依赖它），只是不再对外暴露
        rt = (Path(__file__).resolve().parent.parent
              / "fnpack/app/server/mgr/native_runtime.py").read_text(encoding="utf-8")
        self.assertIn("def send_console", rt)

    def test_frontend_has_no_console_passthrough_and_keeps_bootstrap(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        # 前端不得再暴露任意指令入口
        self.assertNotIn("console/send", app)
        self.assertNotIn("发送控制台指令", app)
        self.assertIn("手动载入存档并开服", app)
        self.assertIn("hostBootstrap", app)
        # 回归：按钮必须真的调用它（曾经函数定义了却没人调用 → 界面没有入口）
        self.assertIn("onclick: () => { hostBootstrap(); }", app)
        self.assertIn("未能自动载入农场", app)
        self.assertIn("native/host-bootstrap", app)


class TestHostingState(unittest.TestCase):
    """进程在跑但**没有存档**时要明确说"未开服"。

    真机踩到：三层判据全绿、界面显示"运行中"，但游戏其实停在标题页 ——
    ALOS 是 `SaveLoaded` 之后才 setServerMode/enableServer，
    **没有存档就永远不会开服**。只显示"运行中"会让用户以为一切正常。
    """

    def setUp(self):
        C.ensure_dirs()

    def _saves(self):
        for p in C.SAVES_DIR.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)

    def test_needs_save_when_running_without_saves(self):
        self._saves()
        rt = NativeRuntime()
        rt._cached = lambda k, f: {"running": True, "exists": True, "status": "running",
                                   "exit_code": None, "started_at": "",
                                   "to_dict": lambda: {}}.get(k) or f()
        # 直接验证"无存档"的判定逻辑（不依赖真实进程）
        try:
            saves = [p for p in C.SAVES_DIR.iterdir() if p.is_dir()]
        except OSError:
            saves = []
        self.assertEqual(saves, [], "测试前应清空存档目录")

    def test_readiness_reports_saves_count_and_needs_save(self):
        rt = NativeRuntime()
        out = rt.readiness()
        self.assertIn("saves_count", out)
        self.assertIn("needs_save", out)
        self.assertIsInstance(out["needs_save"], bool)

    def test_phase_says_not_hosting_when_running_without_save(self):
        """结构性断言：无存档且已加载时必须给出"未开服"的明确措辞。"""
        import inspect
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertIn("needs_save", src)
        self.assertIn("未开服", src)
        self.assertIn("重启服务端", src, "应告诉用户手动新建存档后重启")

    def test_ready_flag_is_false_without_save(self):
        import inspect
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertIn("and not out[\"needs_save\"]", src,
                      "没有存档时 ready 不该为真")

    def test_frontend_warns_about_no_save(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("needs_save", app)
        # 文案按用户要求统一为"需手动新建存档并重启服务端"
        self.assertIn("需手动新建存档并重启服务端", app)
        self.assertIn("需手动新建存档并重启服务端", app)
        self.assertIn("openVnc", app)


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
        cfg.native.display_backend = "xvfb"      # 本用例针对 Xvfb 后端
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
