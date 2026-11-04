"""命令行入口的健壮性。

这里测的不是业务逻辑，是「这条命令能不能跑完」。CI 的 Windows runner 上
``asr-polish subs --help`` 曾经 0 秒退出码 1：stdout 重定向到管道时用的是
系统代码页（西文机器是 cp1252），打印中文帮助直接 ``UnicodeEncodeError``。
真实用户在 ``asr-polish subs --help > help.txt`` 时撞的是同一件事。
"""
import io
import sys
import unittest
from unittest import mock

from asr_polish import cli


def _run(argv, encoding="cp1252"):
    """在指定编码的 stdout 下跑一次 CLI，返回 (退出码, 输出文本)。"""
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding=encoding)
    code = 0
    with mock.patch.object(sys, "stdout", wrapper):
        try:
            cli.main(argv)
        except SystemExit as exc:
            code = exc.code
        finally:
            wrapper.flush()
    return code, buf.getvalue().decode("utf-8", "replace")


class TestStdioEncoding(unittest.TestCase):
    def test_help_survives_non_utf8_pipe(self):
        code, text = _run(["subs", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("min-interval", text)          # 帮助确实打出来了
        self.assertIn("抓取", text)                  # 中文没被替换掉

    def test_version_survives_non_utf8_pipe(self):
        code, text = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("asr-polish", text)

    def test_no_command_prints_help_and_exits_nonzero(self):
        code, text = _run([])
        self.assertEqual(code, 1)
        self.assertIn("usage", text)
        self.assertIn("词表管理", text)              # 中文子命令说明也在


if __name__ == "__main__":
    unittest.main()
