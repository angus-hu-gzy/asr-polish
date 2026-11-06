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


class TestSubsOnly(unittest.TestCase):
    """`subs --only` 只跑给定条目：不列 UP 主投稿，也不重跑已成功的分 P。"""

    def test_only_runs_listed_page_only(self):
        import shutil
        import tempfile

        from asr_polish import bili, export

        calls: list = []
        info = {"title": "标题", "cid": 1, "duration": 10,
                "pages": [{"cid": i, "part": "P%d" % i, "duration": 5}
                          for i in (1, 2, 3)]}

        def fake_fetch(bvid, cookie_path=None, page=1, allow_asr=False):
            calls.append((bvid, page))
            return bili.FetchResult(
                bvid=bvid, title="标题", owner="", duration=10, source="字幕轨",
                text="x", page=page,
                segments=[{"start": 0, "end": 1, "text": "x"}])

        out = tempfile.mkdtemp(prefix="asrcli_")
        self.addCleanup(shutil.rmtree, out, True)
        with mock.patch.object(bili, "get_video_info",
                               lambda *a, **k: info), \
                mock.patch.object(bili, "list_up_videos",
                                  side_effect=AssertionError("不该列投稿")) as up, \
                mock.patch.object(bili, "fetch", fake_fetch), \
                mock.patch.object(export, "write_all"), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            cli.main(["subs", "--only", "BV1xx411c003#2", "-o", out])

        up.assert_not_called()
        self.assertEqual(calls, [("BV1xx411c003", 2)])
        self.assertIn("全部成功", err.getvalue())


class TestNoOutputIsNotSuccess(unittest.TestCase):
    """零产出不能报「全部成功」。

    `--pages 5` 打在只有 3 个分 P 的视频上，每条视频的 pages 都被过滤成空，
    以前落到「结果：全部成功（0 段）」并退出 0——输出目录是空的，脚本化调用
    会把「什么都没做」当成成功。服务端 `_judge` 对同一形状返回 empty，两套
    判定本来就该一致。
    """

    def _run(self, argv):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def _out(self):
        import shutil
        import tempfile
        out = tempfile.mkdtemp(prefix="asrcli_")
        self.addCleanup(shutil.rmtree, out, True)
        return out

    def test_pages_beyond_range_is_not_success(self):
        from asr_polish import bili
        info = {"title": "标题", "cid": 1, "duration": 10,
                "pages": [{"cid": i, "part": "P%d" % i, "duration": 5}
                          for i in (1, 2, 3)]}
        with mock.patch.object(bili, "get_video_info", lambda *a, **k: info), \
                mock.patch.object(bili, "fetch",
                                  side_effect=AssertionError("越界的 P 不该去抓")):
            code, err = self._run(["subs", "BV1xx411c003", "--pages", "5",
                                   "-o", self._out()])
        self.assertEqual(code, 2)
        self.assertIn("没有产出", err)
        self.assertNotIn("全部成功", err)

    def test_unparsable_pages_errors_out(self):
        """`1-3` 解析不出数字，不该带着空集往下跑。"""
        code, err = self._run(["subs", "BV1xx411c003", "--pages", "1-3",
                               "-o", self._out()])
        self.assertEqual(code, 2)
        self.assertIn("一个数字都没解析出来", err)


if __name__ == "__main__":
    unittest.main()
