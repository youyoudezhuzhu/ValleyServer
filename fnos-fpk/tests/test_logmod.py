"""日志读取（tail）回归测试。

真机踩到：`tail()` 用 `open(path, "r")` 文本模式打开，却按字节拼接
（`data = fh.read(step) + data`）→ `TypeError: can only concatenate str
(not "bytes") to str` → `/api/server/logs` 直接 500，用户在「日志」页什么也看不到。
此前没有任何测试覆盖"非空日志文件"的读取路径（工具里的 test-logrotate.sh 只测轮转）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

isolated_env()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fnpack" / "app" / "server"))

from mgr import logmod  # noqa: E402


class TestLogTail(unittest.TestCase):
    def _src(self, tmp: str, text: str):
        p = Path(tmp) / "container.log"
        p.write_text(text, encoding="utf-8")
        return logmod.Logger(log_dir=Path(tmp), name="runtime")

    def test_tail_returns_lines_from_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = "\n".join(f"2026-10-12 21:00:{i % 60:02d} [INFO ] 第{i}行" for i in range(200))
            src = self._src(tmp, body + "\n")
            out = src.tail(lines=10)
            self.assertEqual(len(out), 10, out)
            self.assertIn("第199行", out[-1])

    def test_tail_level_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._src(tmp, "a [INFO ] x\nb [ERROR] y\nc [INFO ] z\n")
            self.assertEqual(src.tail(lines=10, level="error"), ["b [ERROR] y"])

    def test_tail_unknown_level_returns_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._src(tmp, "a [INFO ] x\nb [ERROR] y\n")
            self.assertEqual(len(src.tail(lines=10, level="")), 2)

    def test_tail_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = logmod.Logger(log_dir=Path(tmp), name="runtime")
            self.assertEqual(src.tail(lines=10), [])

    def test_tail_large_file_reads_tail_only(self):
        """超过 256 KiB 块时也要从尾部正确回读（不能乱码/截断）。"""
        with tempfile.TemporaryDirectory() as tmp:
            body = "".join(f"2026-10-12 21:00:00 [INFO ] 行{i} " + "x" * 60 + "\n"
                           for i in range(6000))
            src = self._src(tmp, body)
            out = src.tail(lines=5)
            self.assertEqual(len(out), 5)
            self.assertIn("行5999", out[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
