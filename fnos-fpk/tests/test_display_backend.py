"""显示后端（软件渲染 Xvfb / 核显 Weston+Xwayland）测试。

背景：用户的 NAS 有 Intel 核显，但游戏一直在用 CPU 软件渲染（约 290% CPU）。
根因是 **Xvfb 不支持硬件 GLX**。实测可行方案是
Weston headless(renderer=gl) + Xwayland，让 X11 客户端走核显。
实测数据：就绪后 CPU 约 290% → 约 114%，代价是首次启动 14-19s → 33s。

这里覆盖实测踩到的三个坑（都是"能跑但功能缺失"的隐性缺陷）：
  1. `status()/readiness()` 的进程名写死，切后端后查不到 weston/xwayland
     → 显示栈判据永远 False，游戏明明在跑却被判"启动失败"；
  2. Wayland 环境变量被无差别传给所有子进程 → **x11vnc 直接退出**
     （"Wayland display server detected ... Exiting."）→ 游戏在跑但 VNC 连不上；
  3. Xwayland 用 `-rootless` 时没有根窗口 → x11vnc 抓屏 `X_GetImage` 报
     `BadMatch` 并崩溃 → 同样是 VNC 连不上。
"""

from __future__ import annotations

import inspect
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr import native_runtime as NR  # noqa: E402
from mgr.native_runtime import NativeRuntime, WAYLAND_SOCKET  # noqa: E402


def cfg_with(backend: str):
    cfg = C.load_config()
    cfg.native.display_backend = backend
    return cfg


class TestSpecs(unittest.TestCase):
    def setUp(self):
        self.rt = NativeRuntime()

    def test_xvfb_backend_processes(self):
        names = [s.name for s in self.rt._specs(cfg_with("xvfb"))]
        self.assertEqual(names, ["xvfb", "openbox", "x11vnc", "websockify", "game"])

    def test_weston_backend_processes(self):
        names = [s.name for s in self.rt._specs(cfg_with("weston"))]
        self.assertEqual(names, ["weston", "xwayland", "openbox", "x11vnc",
                                 "websockify", "game"])
        self.assertNotIn("xvfb", names)

    def test_weston_pattern_matches_real_command(self):
        """进程归属识别要能匹配真实启动命令（否则查不到 PID）。"""
        import re
        spec = next(s for s in self.rt._specs(cfg_with("weston")) if s.name == "weston")
        cmdline = ("/bin/weston --backend=headless-backend.so --renderer=gl "
                   f"--socket={WAYLAND_SOCKET} --width=1280 --height=720 --idle-time=0")
        self.assertIsNotNone(re.search(spec.pattern, cmdline))

    def test_xwayland_pattern_matches_real_command(self):
        import re
        spec = next(s for s in self.rt._specs(cfg_with("weston")) if s.name == "xwayland")
        self.assertIsNotNone(re.search(spec.pattern, "/bin/Xwayland :99 -noreset -nolisten tcp"))


class TestArgv(unittest.TestCase):
    def setUp(self):
        self.rt = NativeRuntime()

    def test_weston_uses_gl_renderer(self):
        argv = self.rt._weston_argv(cfg_with("weston"))
        self.assertIn("--renderer=gl", argv, "必须显式要求 GL 渲染，否则可能退回软件")
        self.assertIn("--backend=headless-backend.so", argv)

    def test_weston_output_size_follows_config(self):
        cfg = cfg_with("weston")
        cfg.native.screen_width, cfg.native.screen_height = 1024, 768
        argv = self.rt._weston_argv(cfg)
        self.assertIn("--width=1024", argv)
        self.assertIn("--height=768", argv)

    def test_xwayland_is_rootful(self):
        """**不能**用 -rootless：没有根窗口时 x11vnc 会 BadMatch 崩溃。"""
        argv = self.rt._xwayland_argv(cfg_with("weston"))
        self.assertNotIn("-rootless", argv)

    def test_xwayland_has_no_logfile(self):
        """Xwayland 不支持 Xorg 的 -logfile，传了会启动失败。"""
        self.assertNotIn("-logfile", self.rt._xwayland_argv(cfg_with("weston")))


class TestEnvRoles(unittest.TestCase):
    """环境变量必须按进程角色区分 —— 否则 x11vnc 会误判并退出。"""

    def setUp(self):
        self.rt = NativeRuntime()

    def test_xvfb_backend_forces_software_gl(self):
        env = self.rt._env(cfg_with("xvfb"), role="x11")
        self.assertEqual(env.get("LIBGL_ALWAYS_SOFTWARE"), "1")

    def test_weston_backend_never_forces_software_gl(self):
        """核显模式若还强制软件渲染，核显就白搭了。"""
        for role in ("weston", "xwayland", "x11"):
            with self.subTest(role=role):
                env = self.rt._env(cfg_with("weston"), role=role)
                self.assertNotIn("LIBGL_ALWAYS_SOFTWARE", env)

    def test_x11_role_has_no_wayland_display(self):
        """关键回归：x11vnc 看到 WAYLAND_DISPLAY 会直接退出。"""
        env = self.rt._env(cfg_with("weston"), role="x11")
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertTrue(env.get("DISPLAY"))

    def test_xwayland_role_has_wayland_display(self):
        env = self.rt._env(cfg_with("weston"), role="xwayland")
        self.assertEqual(env.get("WAYLAND_DISPLAY"), WAYLAND_SOCKET)
        self.assertTrue(env.get("XDG_RUNTIME_DIR"))

    def test_weston_role_has_neither_display(self):
        env = self.rt._env(cfg_with("weston"), role="weston")
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)

    def test_wayland_runtime_dir_is_0700(self):
        """Wayland 协议要求 XDG_RUNTIME_DIR 为私有目录。"""
        d = self.rt._wayland_runtime_dir()
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertTrue(str(d).startswith(str(C.PKGHOME)),
                        "运行目录必须在应用家目录内，不能落到 /tmp 或系统目录")

    def test_wayland_runtime_dir_is_not_in_data_root(self):
        """不能落在数据区，否则会被快照/诊断带走。"""
        d = self.rt._wayland_runtime_dir()
        self.assertFalse(str(d).startswith(str(C.DATA_ROOT)))


class TestProcessDiscovery(unittest.TestCase):
    """进程清单必须由 _specs() 派生（回归缺陷 1）。"""

    def test_status_uses_specs_not_hardcoded_names(self):
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertNotIn('for name in ("xvfb", "openbox"', src,
                         "进程清单不能写死；必须遍历 _specs()")
        self.assertIn("_specs(cfg)", src)

    def test_readiness_checks_backend_specific_display(self):
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertIn('"weston"', src)
        self.assertIn('"xwayland"', src)

    def test_no_unconditional_xvfb_indexing(self):
        """不能再出现 out["processes"]["xvfb"] 这种硬取（KeyError 来源）。"""
        src = inspect.getsource(NR.NativeRuntime._readiness_uncached)
        self.assertNotIn('out["processes"]["xvfb"]', src)


class TestStopCoverage(unittest.TestCase):
    def test_stop_covers_weston_and_xwayland(self):
        # `stop()` 现在只负责打「停止中」标记，实体逻辑在 `_stop_locked`
        src = inspect.getsource(NR.NativeRuntime.stop) + inspect.getsource(NR.NativeRuntime._stop_locked)
        for name in ("weston", "xwayland"):
            self.assertIn(name, src, f"停止流程必须清理 {name}")

    def test_stop_order_puts_display_server_last(self):
        """x11vnc/openbox 依赖显示服务器，所以显示服务器要最后停。"""
        src = inspect.getsource(NR.NativeRuntime.stop) + inspect.getsource(NR.NativeRuntime._stop_locked)
        order_line = [l for l in src.splitlines() if "order = " in l][0]
        order = order_line
        self.assertLess(order.index("x11vnc"), order.index("weston"))
        self.assertLess(order.index("openbox"), order.index("xwayland"))

    def test_cleanup_removes_wayland_socket(self):
        src = inspect.getsource(NR.NativeRuntime._cleanup_sockets)
        self.assertIn("WAYLAND_SOCKET", src)



class TestHostBindActuallyApplied(unittest.TestCase):
    """监听范围必须**真的**传给进程。

    真机踩到：配置 `host_bind=127.0.0.1`（界面写"仅本机"），但两个端口实际都监听
    0.0.0.0 —— 配置项只被用于校验与提示，从未传给 x11vnc / websockify。
    """

    def setUp(self):
        from mgr.native_runtime import NativeRuntime
        self.rt = NativeRuntime()

    @staticmethod
    def _with_bind(bind):
        cfg = C.load_config()
        cfg.native.host_bind = bind
        return cfg

    def test_websockify_binds_configured_address(self):
        from mgr.native_runtime import NativeRuntime
        rt = NativeRuntime()
        for bind in ("0.0.0.0", "127.0.0.1"):
            cfg = self._with_bind(bind)
            argv = rt._websockify_argv(cfg)
            want = f"{bind}:{int(cfg.native.webvnc_port)}"
            self.assertIn(want, argv, f"websockify 源参数应含 {want}")

    def test_x11vnc_localhost_only_when_loopback(self):
        from mgr.native_runtime import NativeRuntime
        rt = NativeRuntime()
        self.assertIn("-localhost", rt._x11vnc_argv(self._with_bind("127.0.0.1")))
        self.assertNotIn("-localhost", rt._x11vnc_argv(self._with_bind("0.0.0.0")))

    def test_default_is_all_interfaces(self):
        """默认是所有网络接口（用户选择：局域网可直接连 VNC/WebVNC）。"""
        self.assertEqual(C.NativeConfig().host_bind, "0.0.0.0")

    def test_loopback_helper(self):
        from mgr.native_runtime import is_loopback
        for v in ("127.0.0.1", "::1", "localhost", "[::1]"):
            self.assertTrue(is_loopback(v), v)
        for v in ("0.0.0.0", "192.168.1.5", ""):
            self.assertFalse(is_loopback(v), v)

    def test_ui_text_is_accurate(self):
        """文案必须说清：哪些端口受影响、CommandWebUI 无法约束、默认是所有接口。"""
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        self.assertIn("CommandWebUI(29103) 由上游模组自行监听所有网卡，无法约束", app)
        self.assertIn("['0.0.0.0', '所有网络接口（默认）']", app)
        self.assertIn("只影响 VNC 与 WebVNC；CommandWebUI 无法约束", app)
        # 不能再出现"配置 127.0.0.1 就等于只监听本机"的旧说法
        self.assertNotIn("选「所有接口」会一并开放 VNC", app)


class TestAutoBackend(unittest.TestCase):
    """默认必须是 `auto`：**有 GPU 的机器默认就用上 GPU**。

    背景：第一版把默认值设成保守的 `xvfb`（纯软件渲染），
    结果用户装完发现"GPU 占用 0、CPU 99%"，还得手动去配置页切换 ——
    那等于把该自动的事丢给用户。
    """

    def setUp(self):
        self.rt = NativeRuntime()

    def test_default_is_auto(self):
        self.assertEqual(C.NativeConfig().display_backend, "auto")

    def test_auto_picks_weston_when_gpu_available(self):
        cfg = cfg_with("auto")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": True, "missing": [], "render_nodes": ["/dev/dri/renderD128"],
                                      "weston": True, "xwayland": True,
                                      "renderer": "Mesa Intel(R) Graphics", "software_renderer": False}
        try:
            self.assertEqual(self.rt.effective_backend(cfg), "weston")
        finally:
            self.rt.gpu_status = orig

    def test_auto_falls_back_to_xvfb_without_gpu(self):
        cfg = cfg_with("auto")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": False, "missing": ["weston"],
                                      "render_nodes": [], "weston": False, "xwayland": False,
                                      "renderer": "", "software_renderer": False}
        try:
            self.assertEqual(self.rt.effective_backend(cfg), "xvfb")
        finally:
            self.rt.gpu_status = orig

    def test_explicit_values_win_over_detection(self):
        """显式指定 xvfb / weston 时不受检测影响。"""
        cfg = cfg_with("xvfb")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": True, "missing": [], "render_nodes": [],
                                      "weston": True, "xwayland": True,
                                      "renderer": "x", "software_renderer": False}
        try:
            self.assertEqual(self.rt.effective_backend(cfg), "xvfb")
            self.assertEqual(self.rt.effective_backend(cfg_with("weston")), "weston")
        finally:
            self.rt.gpu_status = orig

    def test_specs_follow_effective_backend(self):
        """auto + 有核显时，进程清单应当就是核显栈（含 weston/xwayland）。"""
        cfg = cfg_with("auto")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": True, "missing": [], "render_nodes": [],
                                      "weston": True, "xwayland": True,
                                      "renderer": "x", "software_renderer": False}
        try:
            names = {s.name for s in self.rt._specs(cfg)}
            self.assertIn("weston", names)
            self.assertIn("xwayland", names)
            self.assertNotIn("xvfb", names)
        finally:
            self.rt.gpu_status = orig

    def test_env_does_not_force_software_gl_under_auto_with_gpu(self):
        cfg = cfg_with("auto")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": True, "missing": [], "render_nodes": [],
                                      "weston": True, "xwayland": True,
                                      "renderer": "x", "software_renderer": False}
        try:
            env = self.rt._env(cfg, role="x11")
            self.assertNotIn("LIBGL_ALWAYS_SOFTWARE", env,
                             "auto 选到核显时不能还强制软件渲染")
        finally:
            self.rt.gpu_status = orig

    def test_validation_accepts_auto(self):
        import orchestration
        cfg = cfg_with("auto")
        orchestration._apply_config_payload(cfg, {"native": {"display_backend": "auto"}})
        self.assertEqual(cfg.native.display_backend, "auto")

    def test_frontend_defaults_to_auto(self):
        app = (Path(__file__).resolve().parent.parent
               / "fnpack/app/www/static/app.js").read_text(encoding="utf-8")
        # 设置页改为"草稿式"渲染后，默认值由后端配置提供；前端只负责把
        # 「自动」作为首选项呈现。这里断言选项与默认语义仍在。
        self.assertIn("'display_backend', '显示后端', 'select'", app)
        self.assertIn("['auto', '自动（推荐）']", app)

    def test_no_leftover_config_only_backend_checks(self):
        """回归：所有后端判断都必须走 effective_backend（否则 auto 会被当成非核显）。"""
        import inspect
        src = inspect.getsource(NR)
        self.assertNotIn('native.display_backend == "weston"', src,
                         "还有地方直接比较配置值，auto 模式下会走错分支")


class TestBackendSwitchCleanup(unittest.TestCase):
    """切换显示后端时必须清掉另一个后端的遗留进程。

    真机踩到：从核显模式（Weston + Xwayland）回滚到 Xvfb 后，
    Weston/Xwayland 仍在运行并占着 `:99`，而 `_prepare_display()` 认为
    "是本应用自己的"就放行了 → Xvfb 启动失败：
      Cannot establish any listening sockets - Make sure an X server isn't already running
    结果配置变更任务超时 → 回滚 → 回滚也失败，服务停在"已停止"。
    """

    def test_all_specs_covers_both_backends(self):
        rt = NativeRuntime()
        names = {s.name for s in rt._all_specs(cfg_with("xvfb"))}
        for expect in ("xvfb", "weston", "xwayland", "openbox", "x11vnc",
                       "websockify", "game"):
            self.assertIn(expect, names)

    def test_current_backend_specs_stay_minimal(self):
        rt = NativeRuntime()
        xvfb_names = {s.name for s in rt._specs(cfg_with("xvfb"))}
        weston_names = {s.name for s in rt._specs(cfg_with("weston"))}
        self.assertNotIn("weston", xvfb_names)
        self.assertNotIn("xvfb", weston_names)

    def test_stop_uses_union_of_backends(self):
        """stop() 必须覆盖两个后端，否则切后端会留下占着显示号的进程。"""
        import inspect
        # 停止的实体逻辑在 _stop_locked（stop 只负责打「停止中」标记）
        src = inspect.getsource(NR.NativeRuntime.stop) + inspect.getsource(NR.NativeRuntime._stop_locked)
        self.assertIn("_all_specs", src)

    def test_start_cleans_stale_backend(self):
        import inspect
        src = inspect.getsource(NR.NativeRuntime.start)
        self.assertIn("_stop_stale_backend_stack", src)

    def test_stale_targets_the_other_backend(self):
        import inspect
        src = inspect.getsource(NR.NativeRuntime._stop_stale_backend_stack)
        # xvfb 模式要清 weston/xwayland；weston 模式要清 xvfb
        self.assertIn('stale_backend = "xwayland" if current == "xvfb" else "xvfb"', src)
        self.assertIn('"weston", "xwayland"', src)


class TestGpuStatus(unittest.TestCase):
    def setUp(self):
        self.rt = NativeRuntime()

    def test_reports_required_keys(self):
        g = self.rt.gpu_status()
        for key in ("available", "render_nodes", "weston", "xwayland",
                    "missing", "renderer", "software_renderer"):
            self.assertIn(key, g)

    def test_detects_software_renderer(self):
        """若 Weston 退化成 llvmpipe，必须能被识别出来（不能让用户以为用上了核显）。"""
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = C.LOG_DIR / "weston.log"
        backup = log.read_text() if log.is_file() else None
        try:
            log.write_text("[..] GL renderer: llvmpipe (LLVM 15.0.6, 256 bits)\\n")
            g = self.rt.gpu_status()
            self.assertTrue(g["software_renderer"])
            self.assertIn("llvmpipe", g["renderer"])
        finally:
            if backup is None:
                log.unlink(missing_ok=True)
            else:
                log.write_text(backup)

    def test_validation_rejects_weston_without_gpu(self):
        """选了核显但没有 GPU/组件时，必须在校验阶段报错并给出可操作的出路。"""
        cfg = cfg_with("weston")
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": False, "missing": ["weston", "/dev/dri/renderD*"],
                                      "render_nodes": [], "weston": False, "xwayland": False,
                                      "renderer": "", "software_renderer": False}
        try:
            rep = self.rt.validate(cfg)
            self.assertFalse(rep["ok"])
            msg = " ".join(rep["errors"])
            self.assertIn("核显", msg)
            self.assertIn("Xvfb", msg)      # 要指出退路
        finally:
            self.rt.gpu_status = orig

    def test_weston_backend_warns_when_software_gl_still_on(self):
        """核显模式下「强制软件渲染」不生效，应给出警告而不是默默忽略。

        ⚠️ 这条必须**打桩 GPU 可用**：CI 机器没有 /dev/dri，
        真实 gpu_status() 会返回不可用，于是走"报错"分支而拿不到这条警告 ——
        第一版测试就因此只在本地通过、在 CI 失败（测试不该依赖宿主机硬件）。
        """
        cfg = cfg_with("weston")
        cfg.native.software_gl = True
        orig = self.rt.gpu_status
        self.rt.gpu_status = lambda: {"available": True, "missing": [], "render_nodes": ["/dev/dri/renderD128"],
                                      "weston": True, "xwayland": True,
                                      "renderer": "Mesa Intel(R) Graphics", "software_renderer": False}
        try:
            rep = self.rt.validate(cfg)
            self.assertTrue(any("软件渲染" in w for w in rep["warnings"]), rep["warnings"])
        finally:
            self.rt.gpu_status = orig


class TestConfigValidation(unittest.TestCase):
    def test_backend_whitelisted(self):
        import orchestration
        self.assertIn("display_backend", orchestration.ALLOWED_CONFIG_FIELDS["native"])

    def test_invalid_backend_rejected(self):
        import orchestration
        from mgr.util import ValidationError
        for bad in ("vnc", "wayland", "", "XVFB"):
            with self.subTest(value=bad):
                cfg = cfg_with("xvfb")
                with self.assertRaises(ValidationError) as ctx:
                    orchestration._apply_config_payload(
                        cfg, {"native": {"display_backend": bad}})
                self.assertIn("显示后端", str(ctx.exception))

    def test_valid_backends_accepted(self):
        import orchestration
        for good in ("xvfb", "weston"):
            with self.subTest(value=good):
                cfg = cfg_with("xvfb")
                orchestration._apply_config_payload(
                    cfg, {"native": {"display_backend": good}})
                self.assertEqual(cfg.native.display_backend, good)

    def test_default_backend_uses_gpu_when_available(self):
        """默认是 auto —— 有核显就用核显（不再让用户手动切）。"""
        self.assertEqual(C.NativeConfig().display_backend, "auto")


if __name__ == "__main__":
    unittest.main(verbosity=2)
