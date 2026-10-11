"""Compose 模板与 entrypoint 测试。

覆盖项目要求 §4.2：
  * 用户输入只能是白名单标量，**不得注入任意 YAML / Shell / 宿主机路径**。
  * 非法端口/监听地址/资源限制必须被拒绝。
  * 生成的 compose 必须是合法 YAML，且路径都落在数据目录内。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.compose_mgr import ComposeManager, yaml_scalar  # noqa: E402
from mgr.util import ValidationError  # noqa: E402


class TestYamlScalar(unittest.TestCase):
    def test_escapes_dangerous(self):
        self.assertEqual(yaml_scalar('a"b'), '"a\\"b"')
        self.assertEqual(yaml_scalar("a\nb"), '"a\\nb"')
        self.assertEqual(yaml_scalar("a\tb"), '"a\\tb"')
        self.assertEqual(yaml_scalar("a\\b"), '"a\\\\b"')

    def test_newline_cannot_break_structure(self):
        injected = 'x"\n  evil: true\n  other: "' + "y"
        out = yaml_scalar(injected)
        self.assertNotIn("\n", out)
        self.assertTrue(out.startswith('"') and out.endswith('"'))


class TestComposeRendering(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.cfg = C.ManagerConfig()
        self.cfg.container.vnc_password = "Ab3dEf9z"
        self.cmp = ComposeManager()

    def test_renders_valid_yaml(self):
        text = self.cmp.render(self.cfg)
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None
        if yaml is not None:
            data = yaml.safe_load(text)
            self.assertIsInstance(data, dict)
            self.assertIn("services", data)
            self.assertIn("game", data["services"])
        # 无论有没有 PyYAML，都做基本结构断言
        self.assertIn("container_name: valleyserver-game", text)
        self.assertIn("networks:", text)
        self.assertIn("valleyserver-net", text)

    def test_uses_digest_when_pinned(self):
        text = self.cmp.render(self.cfg)
        self.assertIn(f"lixeer/valley-server@sha256:{'a'*64}" if False else
                      self.cfg.container.image_digest, text)

    def test_tag_when_not_pinned(self):
        self.cfg.container.pin_digest = False
        text = self.cmp.render(self.cfg)
        self.assertIn("lixeer/valley-server:v26.5", text)

    def test_paths_are_absolute_and_inside_data_root(self):
        text = self.cmp.render(self.cfg)
        self.assertIn(str(C.SAVES_DIR), text)
        self.assertIn(str(C.MODS_DIR), text)
        self.assertNotIn(": ./", text)          # 不得使用相对挂载
        self.assertNotIn("/etc", text)
        self.assertNotIn("/var/run/docker.sock", text)   # 绝不把 docker socket 交给容器

    def test_no_docker_socket_mount(self):
        text = self.cmp.render(self.cfg)
        self.assertNotIn("docker.sock", text)

    def test_all_four_ports_present(self):
        text = self.cmp.render(self.cfg)
        for port in (24642, 5800, 5900, 29103):
            self.assertIn(str(port), text)

    def test_binds_localhost_by_default(self):
        self.cfg.container.host_bind = "127.0.0.1"
        text = self.cmp.render(self.cfg)
        self.assertIn('"127.0.0.1:24642:24642/udp"', text)
        self.assertNotIn('"0.0.0.0:', text)

    def test_healthcheck_present(self):
        text = self.cmp.render(self.cfg)
        self.assertIn("healthcheck:", text)
        self.assertIn("start_period:", text)


class TestComposeRejectsBadInput(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.cmp = ComposeManager()

    def _cfg(self):
        cfg = C.ManagerConfig()
        cfg.container.vnc_password = "Ab3dEf9z"
        return cfg

    def test_reject_bad_bind(self):
        cfg = self._cfg()
        for bad in ("0.0.0.0; rm -rf /", "$(id)", "a b", "999.1.1.1", "hello",
                    "0.0.0.0\n  evil: true"):
            with self.subTest(bad=bad):
                cfg.container.host_bind = bad
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_ports(self):
        for field in ("game_port", "webvnc_port", "vnc_port", "cmdwebui_port"):
            for bad in (0, -1, 70000, "abc", None, True):
                cfg = self._cfg()
                setattr(cfg.container, field, bad)
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        self.cmp.render(cfg)

    def test_reject_bad_sizes(self):
        cfg = self._cfg()
        for bad in ("512m; rm -rf /", "$(id)", "-1g", "1g ", "abc", "1" * 20):
            cfg.container.shm_size = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_cpu(self):
        cfg = self._cfg()
        for bad in ("abc", "-1", "0", "1000", "$(id)"):
            cfg.container.cpu_limit = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_restart_policy(self):
        cfg = self._cfg()
        for bad in ("always; rm", "yes", "", "../x"):
            cfg.container.restart_policy = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_vnc_password(self):
        cfg = self._cfg()
        for bad in ("", "short", "toolongpassword", "1234567", "abcdefghi",
                    'with"quote', "with space"):
            cfg.container.vnc_password = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_image(self):
        cfg = self._cfg()
        for bad in ("foo;rm -rf /", "$(id)", "foo bar", "../evil", "", "a" * 300):
            cfg.container.image = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)

    def test_reject_bad_digest(self):
        cfg = self._cfg()
        for bad in ("sha256:xyz", "notadigest", "sha256:" + "a" * 63):
            cfg.container.image_digest = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.cmp.render(cfg)


class TestEntrypointScript(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.cmp = ComposeManager()
        self.path = self.cmp.write_entrypoint()

    def test_written_and_executable(self):
        self.assertTrue(self.path.is_file())
        mode = self.path.stat().st_mode & 0o777
        self.assertTrue(mode & 0o100, f"entrypoint 不可执行：{oct(mode)}")

    def test_bash_syntax_ok(self):
        res = subprocess.run(["bash", "-n", str(self.path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    @staticmethod
    def _code_only(text: str) -> str:
        """剥掉注释与空行，只保留真正会被执行的代码。

        断言必须对「代码」成立，而不是被解释性注释误导（或反过来）。
        """
        out = []
        for line in text.splitlines():
            stripped = line.split("#", 1)[0].rstrip()
            if stripped:
                out.append(stripped)
        return "\n".join(out)

    def test_password_from_env_not_hardcoded(self):
        code = self._code_only(self.path.read_text(encoding="utf-8"))
        # x11vnc 的 -passwd 必须取自环境变量
        self.assertIn('-passwd "${VNC_PASSWORD:-}"', code)
        self.assertNotIn("041041", code)           # 上游硬编码口令不得出现在任何被执行的代码里

    def test_console_log_redirected(self):
        code = self._code_only(self.path.read_text(encoding="utf-8"))
        self.assertIn('CONSOLE_LOG="${LOG_DIR}/console.log"', code)
        self.assertIn('tee -a "$CONSOLE_LOG"', code)

    def test_active_save_passed_to_game(self):
        code = self._code_only(self.path.read_text(encoding="utf-8"))
        self.assertIn("SAVE_ARG", code)
        self.assertIn("selected-save", code)
        self.assertIn("--load-save", code)

    def test_inplace_log_rotation(self):
        code = self._code_only(self.path.read_text(encoding="utf-8"))
        # 必须原地重写（cat > ），不能 mv/rename
        self.assertIn('cat "$tmp" > "$file"', code)
        self.assertNotIn("mv $", code)
        self.assertNotIn("mv \"", code)

    def test_no_shell_injection_from_compose(self):
        """entrypoint 里的变量都来自 compose 的静态值，不接受用户自由文本。"""
        cfg = C.ManagerConfig()
        cfg.container.vnc_password = 'a"b$(id)`x`'  # 非法，渲染应失败
        with self.assertRaises(ValidationError):
            self.cmp.render(cfg)


class TestComposeFileWriteAndValidate(unittest.TestCase):
    def setUp(self):
        C.ensure_dirs()
        self.cfg = C.ManagerConfig()
        self.cfg.container.vnc_password = "Ab3dEf9z"
        self.cmp = ComposeManager()

    def test_write_and_mode(self):
        path = self.cmp.write_compose(self.cfg)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "docker-compose.yaml")
        self.assertEqual(path.parent, C.GAME_DIR)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_config_q_when_docker_available(self):
        """若本机有 Docker，则用 `docker compose config -q` 做真校验。"""
        from shutil import which
        if not which("docker"):
            self.skipTest("本机没有 docker")
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("本机没有 docker compose")
        self.cmp.write_entrypoint()
        self.cmp.write_compose(self.cfg)
        res = self.cmp.validate_rendered()
        self.assertTrue(res.ok, f"compose 校验失败：{res.tail}")
        # 校验后不要留下任何容器
        down = subprocess.run(
            ["docker", "compose", "--project-name", "valleyserver",
             "--project-directory", str(C.GAME_DIR),
             "-f", str(self.cmp.compose_path), "down", "--remove-orphans"],
            capture_output=True, text=True,
        )
        _ = down


class TestDockerDetection(unittest.TestCase):
    def test_detect_shape(self):
        info = ComposeManager.detect()
        for key in ("docker", "compose", "socket", "ready", "guidance"):
            self.assertIn(key, info)
        if not info["ready"]:
            self.assertTrue(info["guidance"], "不可用时必须给出安装指引")

    def test_require_ready_raises_or_returns(self):
        info = ComposeManager.detect()
        if info["ready"]:
            self.assertTrue(ComposeManager.require_ready()["ready"])
        else:
            from mgr.compose_mgr import DockerUnavailable
            with self.assertRaises(DockerUnavailable):
                ComposeManager.require_ready()


if __name__ == "__main__":
    unittest.main(verbosity=2)
