"""字幕导出的单元测试。运行：python -m unittest discover -s tests -q"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from asr_polish import export  # noqa: E402
from asr_polish.bili import FetchResult, expand_pages  # noqa: E402


def _result(**kw) -> FetchResult:
    base = dict(bvid="BV1xx411c7mD", title="测试标题", owner="某UP",
                duration=273, source="字幕轨", text="第一句\n第二句",
                subtitle_lan="ai-zh", page=1,
                segments=[{"start": 0.64, "end": 2.64, "text": "第一句"},
                          {"start": 2.64, "end": 4.36, "text": "第二句"}])
    base.update(kw)
    return FetchResult(**base)


class TestFilename(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(export.safe_name('a/b:c*d?"e"|f'), "a_b_c_d_e_f")

    def test_empty_fallback(self):
        self.assertEqual(export.safe_name("   "), "untitled")

    def test_truncated(self):
        self.assertLessEqual(len(export.safe_name("字" * 200)), 40)

    def test_single_page_uses_title(self):
        name = export.base_name(_result(), "分P标题", 1)
        self.assertEqual(name, "BV1xx411c7mD-测试标题")

    def test_multi_page_uses_part(self):
        name = export.base_name(_result(page=3), "第03节：循环", 100)
        self.assertEqual(name, "BV1xx411c7mD-P3-第03节：循环")


class TestFormats(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(export.parse_formats("txt,srt"), ["txt", "srt"])

    def test_default_txt(self):
        self.assertEqual(export.parse_formats(""), ["txt"])

    def test_reject_unknown(self):
        with self.assertRaises(ValueError):
            export.parse_formats("docx")


class TestRender(unittest.TestCase):
    def test_srt_timecode(self):
        out = export.render_srt(_result())
        self.assertIn("00:00:00,640 --> 00:00:02,640", out)
        self.assertIn("00:00:02,640 --> 00:00:04,360", out)
        self.assertTrue(out.startswith("1\n"))

    def test_srt_without_segments_still_valid(self):
        r = _result(segments=[], text="整段文本")
        out = export.render_srt(r)
        self.assertIn("00:00:00,000 -->", out)
        self.assertIn("整段文本", out)

    def test_json_roundtrip(self):
        payload = json.loads(export.render_json(_result()))
        self.assertEqual(payload["bvid"], "BV1xx411c7mD")
        self.assertEqual(payload["track"], "ai-zh")
        self.assertEqual(len(payload["segments"]), 2)

    def test_json_without_segments_keeps_text(self):
        payload = json.loads(export.render_json(_result(segments=[], text="整段")))
        self.assertEqual(payload["text"], "整段")

    def test_md_marks_ai_track(self):
        out = export.render_md(_result())
        self.assertIn("AI 生成", out)
        self.assertIn("https://www.bilibili.com/video/BV1xx411c7mD", out)

    def test_md_human_track(self):
        out = export.render_md(_result(subtitle_lan="zh-CN"))
        self.assertIn("人工", out)


class TestExpandPages(unittest.TestCase):
    def test_single(self):
        pages = expand_pages({"cid": 11, "title": "T", "duration": 100})
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["index"], 1)
        self.assertEqual(pages[0]["cid"], 11)

    def test_multi(self):
        video = {"cid": 11, "title": "T", "duration": 300, "pages": [
            {"cid": 11, "part": "P1 标题", "duration": 100},
            {"cid": 22, "part": "P2 标题", "duration": 200},
        ]}
        pages = expand_pages(video)
        self.assertEqual([p["index"] for p in pages], [1, 2])
        self.assertEqual(pages[1]["title"], "P2 标题")
        self.assertEqual(pages[1]["cid"], 22)


class TestWriteAll(unittest.TestCase):
    def test_writes_and_leaves_no_temp_files(self):
        """原子写：先写同目录临时文件再 os.replace，不能留下半成品。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            written = export.write_all(out, "stem", ["txt", "srt"], _result())
            self.assertEqual(len(written), 2)
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(names, ["stem.srt", "stem.txt"])
            self.assertEqual((out / "stem.txt").read_text(encoding="utf-8"),
                             "第一句\n第二句")

    def test_skip_existing_keeps_old_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "stem.txt").write_text("旧内容", encoding="utf-8")
            written = export.write_all(out, "stem", ["txt"], _result(),
                                       skip_existing=True)
            self.assertEqual(written, [])
            self.assertEqual((out / "stem.txt").read_text(encoding="utf-8"),
                             "旧内容")

    def test_overwrites_atomically(self):
        """覆盖时旧文件始终可读——不会出现「先截断再写」的窗口。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            target = out / "stem.txt"
            target.write_text("旧" * 1000, encoding="utf-8")
            export.write_all(out, "stem", ["txt"], _result())
            text = target.read_text(encoding="utf-8")
            self.assertEqual(text, "第一句\n第二句")
            self.assertEqual([p.name for p in out.iterdir()], ["stem.txt"])


if __name__ == "__main__":
    unittest.main()
