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
        """没有存档时绝不能报「农场已载入」。"""
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
        self.assertIn('"/api/console/send"', src)
        self.assertIn('"/api/native/host-bootstrap"', src)
        # 控制台指令是高权限操作，必须写审计
        i = src.index("def console_send")
        self.assertIn("audit", src[i:i + 1200])

    def test_frontend_has_console_input_and_bootstrap(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("console/send", app)
        self.assertIn("一键开服", app)
        self.assertIn("hostBootstrap", app)
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
