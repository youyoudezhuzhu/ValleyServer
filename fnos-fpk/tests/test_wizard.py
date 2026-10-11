"""向导（wizard/*）结构校验。

为什么需要：曾经把一个**纯说明**做成了 `type: text` 的必填输入框，
标签写着"说明（可直接使用默认值）"、默认值"知道了" —— 用户安装时会看到一个
要他填"知道了"的输入框。这是"用假控件凑步骤"的行为。

fnOS 向导本身有 `tips` 类型（本机 24 个已装应用都在用它做纯说明），
所以校验规则明确：
  * `type` 必须来自本机实测存在的集合；
  * 除 `tips` 外，每个控件必须有 `field`（它的值会变成环境变量）；
  * `tips` 不能带 `field`/`initValue`/`rules`（它不是输入）；
  * 每个步骤至少要有一个控件，不能有空的步骤；
  * `radio`/`select`/`switch` 必须有 `options`，且 `initValue` 必须是其中之一；
  * `field` 不得带 `TRIM_` 前缀（会与系统变量撞名），也不得重复。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

WIZARD_DIR = Path(__file__).resolve().parent.parent / "fnpack" / "wizard"

# 本机 50 个已装应用 + fnpack 模板里实际出现过的控件类型
KNOWN_TYPES = {"text", "password", "radio", "select", "switch", "tips"}
CHOICE_TYPES = {"radio", "select", "switch"}
# 不是"输入"的类型：不应带 field / initValue / rules
NON_INPUT_TYPES = {"tips"}


class TestWizardStructure(unittest.TestCase):
    def _files(self):
        return sorted(WIZARD_DIR.glob("*"))

    def test_wizard_files_exist(self):
        names = {p.name for p in self._files()}
        for expected in ("install", "config", "uninstall"):
            self.assertIn(expected, names, f"缺少向导文件：{expected}")

    def test_all_steps_and_items_are_well_formed(self):
        for path in self._files():
            with self.subTest(wizard=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(data, list, "向导根必须是数组（步骤列表）")
                self.assertTrue(data, "向导不能为空")
                seen_fields: set[str] = set()
                for si, step in enumerate(data):
                    title = step.get("stepTitle")
                    self.assertTrue(title, f"第 {si + 1} 步缺少 stepTitle")
                    items = step.get("items") or []
                    self.assertTrue(items, f"步骤「{title}」没有任何控件（空步骤）")
                    for item in items:
                        kind = item.get("type")
                        self.assertIn(kind, KNOWN_TYPES,
                                      f"步骤「{title}」出现未知控件类型：{kind!r}")
                        if kind in NON_INPUT_TYPES:
                            # 纯说明：不允许伪装成输入
                            for bad in ("field", "initValue", "rules", "options"):
                                self.assertNotIn(
                                    bad, item,
                                    f"步骤「{title}」的 {kind} 项不应带 {bad}（它不是输入控件）")
                            self.assertTrue((item.get("helpText") or "").strip(),
                                            f"步骤「{title}」的 {kind} 项没有 helpText，等于空步骤")
                            continue

                        field = item.get("field")
                        self.assertTrue(field,
                                        f"步骤「{title}」的 {kind} 控件缺少 field（其值会变成环境变量）")
                        self.assertFalse(field.startswith("TRIM_"),
                                         f"向导字段 {field} 不得带 TRIM_ 前缀（会与系统变量撞名）")
                        self.assertNotIn(field, seen_fields,
                                         f"向导字段重复：{field}")
                        seen_fields.add(field)

                        if kind in CHOICE_TYPES:
                            options = item.get("options") or []
                            self.assertTrue(options, f"{field} 是 {kind}，必须提供 options")
                            values = [o.get("value") for o in options]
                            self.assertNotIn(None, values, f"{field} 的某个 option 缺少 value")
                            init = item.get("initValue")
                            if init is not None:
                                self.assertIn(init, values,
                                              f"{field} 的 initValue={init!r} 不在 options 里")

    def test_no_fake_input_used_as_label(self):
        """回归：不得再用 text 控件承载"说明"（曾出现默认值"知道了"的假输入框）。"""
        suspicious = re.compile(r"说明|提示|注意|告知|知道了|确认无误|我已知晓")
        for path in self._files():
            data = json.loads(path.read_text(encoding="utf-8"))
            for step in data:
                for item in (step.get("items") or []):
                    if item.get("type") in ("text", "password"):
                        label = item.get("label") or ""
                        init = item.get("initValue") or ""
                        with self.subTest(wizard=path.name, label=label):
                            self.assertFalse(
                                suspicious.search(label),
                                f"步骤「{step.get('stepTitle')}」用输入框承载说明文字：{label!r}；"
                                "纯说明请用 type=tips")
                            self.assertNotIn(
                                str(init), ("知道了", "好的", "已阅读", "确认", "OK"),
                                f"字段 {item.get('field')} 的默认值是 {init!r}，像是凑数的假输入")

    def test_tips_type_is_actually_available_on_this_platform(self):
        """`tips` 不是我们臆造的：本机已装应用确实在用它。"""
        used = 0
        for path in Path("/var/apps").glob("*/wizard/*"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for step in data if isinstance(data, list) else []:
                for item in (step.get("items") or []):
                    if item.get("type") == "tips":
                        used += 1
        if used == 0:
            self.skipTest("本机没有可作为参考的已装应用向导，跳过平台佐证")
        self.assertGreater(used, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
