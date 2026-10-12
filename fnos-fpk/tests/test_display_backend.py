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
        src = inspect.getsource(NR.NativeRuntime.stop)
        for name in ("weston", "xwayland"):
            self.assertIn(name, src, f"stop() 必须清理 {name}")

    def test_stop_order_puts_display_server_last(self):
        """x11vnc/openbox 依赖显示服务器，所以显示服务器要最后停。"""
        src = inspect.getsource(NR.NativeRuntime.stop)
        order_line = [l for l in src.splitlines() if "order = " in l][0]
        order = order_line
        self.assertLess(order.index("x11vnc"), order.index("weston"))
        self.assertLess(order.index("openbox"), order.index("xwayland"))

    def test_cleanup_removes_wayland_socket(self):
        src = inspect.getsource(NR.NativeRuntime._cleanup_sockets)
        self.assertIn("WAYLAND_SOCKET", src)


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

    def test_default_backend_is_conservative(self):
        """默认必须是软件渲染（兼容性最好），核显要用户显式选择。"""
        self.assertEqual(C.NativeConfig().display_backend, "xvfb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
