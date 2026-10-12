"""路由注册顺序测试。

背景（真机踩到的真实缺陷 I-1）：路由器按**注册顺序**匹配。
`/api/mods/upstream-bundle` 与 `/api/mods/smapi-log` 曾被通配路由
`/api/mods/(?P<dir_name>[^/]+)` 吃掉，前端因此拿到一个 MOD 对象而不是下载信息，
把 undefined 提交回后端，最后报出"只允许从上游仓库下载 MOD 包"这句
与真实原因毫不相干的提示 —— 用户完全查不到问题在哪。

这类缺陷不会自己暴露（接口返回 200、结构看似正常），所以必须用测试钉住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

import api_handlers  # noqa: E402
from run import Router  # noqa: E402


class _FakeApp:
    """只提供 register 需要的东西，用来拿到**真实**路由表。"""

    def __init__(self):
        self.router = Router()
        api_handlers.register(self)


APP = _FakeApp()


def resolve(method: str, path: str):
    handler, groups = APP.router.match(method, path)
    return getattr(handler, "__name__", None), groups


class TestModRoutesNotShadowed(unittest.TestCase):
    """具体路径必须命中各自的处理器，不能被通配路由抢走。"""

    def test_upstream_bundle_is_not_treated_as_a_mod_name(self):
        name, groups = resolve("GET", "/api/mods/upstream-bundle")
        self.assertNotEqual(name, "mod_detail",
                            "`upstream-bundle` 被当成 MOD 目录名了 —— 前端会拿到错误结构")
        self.assertEqual(name, "mod_bundle_info")
        self.assertEqual(groups, {})

    def test_upstream_bundle_post(self):
        name, _ = resolve("POST", "/api/mods/upstream-bundle")
        self.assertEqual(name, "mod_bundle_download")

    def test_smapi_log_is_not_treated_as_a_mod_name(self):
        name, _ = resolve("GET", "/api/mods/smapi-log")
        self.assertNotEqual(name, "mod_detail")
        self.assertEqual(name, "mods_smapi_report")

    def test_other_reserved_names_not_treated_as_mods(self):
        for path in ("/api/mods/install-plan", "/api/mods/install",
                     "/api/mods/remove", "/api/mods/toggle"):
            with self.subTest(path=path):
                name = resolve("POST", path)[0]
                self.assertNotEqual(name, "mod_detail", f"{path} 被通配路由吃掉了")

    def test_real_mod_names_still_resolve_to_detail(self):
        for name in ("ConsoleCommands", "SaveBackup", "my-mod", "a" * 40):
            with self.subTest(mod=name):
                handler, groups = resolve("GET", f"/api/mods/{name}")
                self.assertEqual(handler, "mod_detail")
                self.assertEqual(groups.get("dir_name"), name)

    def test_mod_list_still_works(self):
        self.assertEqual(resolve("GET", "/api/mods")[0], "mods_list")


class TestRouteOrderingInvariant(unittest.TestCase):
    """把"具体路径先于通配路径"钉成不变量，防止以后调整顺序时再犯。"""

    def test_specific_routes_registered_before_wildcard(self):
        mods = [(i, r.pattern) for i, (verb, r, _h) in enumerate(APP.router.routes)
                if verb == "GET" and r.pattern.startswith("/api/mods")]
        wildcard = [i for i, p in mods if "dir_name" in p]
        specific = [i for i, p in mods
                    if "dir_name" not in p and p not in ("/api/mods",)]
        self.assertTrue(wildcard, "没有找到 MOD 通配路由")
        self.assertTrue(specific, "没有找到 MOD 具体路由")
        self.assertLess(max(specific), min(wildcard),
                        "MOD 具体路由必须全部注册在通配路由之前")

    def test_wildcard_excludes_reserved_names(self):
        """双保险：即使顺序被改，保留名也不会被当成 MOD 目录名。"""
        for name in ("upstream-bundle", "smapi-log", "install-plan",
                     "install", "remove", "toggle"):
            with self.subTest(name=name):
                self.assertNotEqual(resolve("GET", f"/api/mods/{name}")[0], "mod_detail")


class TestApiShapeContracts(unittest.TestCase):
    """前端依赖的字段形状：曾经因为嵌套结构导致前端 KeyError。"""

    def test_dashboard_has_no_fake_players_field(self):
        """已删除的占位字段不该再回来。"""
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/api_handlers.py").read_text(encoding="utf-8")
        self.assertNotIn('"players": {', src,
                         "总览页已移除「在线玩家」占位卡片，接口不该再返回 players")

    def test_config_has_no_dead_metric_setting(self):
        src = (Path(__file__).resolve().parent.parent
               / "fnpack/app/server/mgr/config.py").read_text(encoding="utf-8")
        self.assertNotIn("metrics_interval_seconds", src,
                         "该设置从未被任何代码读取，已删除，不该回来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
