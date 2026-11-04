"""Cookie 导入的解析与写入。

这块最容易出的错不是崩溃，而是「静默写进一份看起来正常、实际没登录的文件」，
所以重点测三件事：值不能被截断、缺 SESSDATA 必须拒绝、写失败不能毁掉原文件。
"""
import http.cookiejar
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asr_polish import bili

_HEADER = ("cookie: buvid3=AAA%2CBB; SESSDATA=abc%2Cdef%3DGHI; "
           "bili_jct=deadbeef; DedeUserID=12345")

_NETSCAPE = """# Netscape HTTP Cookie File
.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tabc
.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tbili_jct\tjct
"""


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8")


class TestParseHeader(unittest.TestCase):
    def test_strips_prefix_and_keeps_full_value(self):
        pairs = bili._parse_cookie_header(_HEADER)
        got = dict(pairs)
        self.assertEqual(len(pairs), 4)
        self.assertEqual(got["SESSDATA"], "abc%2Cdef%3DGHI")   # 值里的 = 不能被切掉
        self.assertEqual(got["DedeUserID"], "12345")

    def test_multiline_payload(self):
        pairs = bili._parse_cookie_header("SESSDATA=a;\nbili_jct=b\n")
        self.assertEqual(dict(pairs), {"SESSDATA": "a", "bili_jct": "b"})

    def test_ignores_empty_and_broken_chunks(self):
        pairs = bili._parse_cookie_header("SESSDATA=a;;  ;=noname; flag; b=2")
        self.assertEqual(dict(pairs), {"SESSDATA": "a", "b": "2"})

    def test_quoted_values(self):
        pairs = bili._parse_cookie_header('SESSDATA="a b"')
        self.assertEqual(dict(pairs)["SESSDATA"], "a b")


class TestImportHeader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = str(Path(self.dir) / "cookies.txt")

    def test_writes_parsable_file(self):
        rep = bili.import_cookies(_HEADER, self.target)
        self.assertEqual(rep["changed"], 4)
        self.assertTrue(rep["has_sessdata"])
        jar = http.cookiejar.MozillaCookieJar(self.target)
        jar.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual(len(list(jar)), 4)

    def test_second_import_is_noop(self):
        bili.import_cookies(_HEADER, self.target)
        self.assertEqual(bili.import_cookies(_HEADER, self.target)["changed"], 0)

    def test_partial_import_keeps_other_cookies(self):
        bili.import_cookies(_HEADER, self.target)
        bili.import_cookies("SESSDATA=new; bili_jct=newjct", self.target)
        text = _read(self.target)
        self.assertIn("buvid3", text)          # 设备指纹不能被抹掉
        self.assertIn("DedeUserID", text)
        self.assertIn("newjct", text)

    def test_same_name_not_duplicated(self):
        bili.import_cookies(_HEADER, self.target)
        bili.import_cookies("SESSDATA=v2", self.target)
        jar = http.cookiejar.MozillaCookieJar(self.target)
        jar.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual(sum(1 for c in jar if c.name == "SESSDATA"), 1)

    def test_rejects_logged_out_cookie(self):
        with self.assertRaises(bili.BiliError) as ctx:
            bili.import_cookies("buvid3=AAA; buvid4=BBB", self.target)
        self.assertIn("没有 SESSDATA", str(ctx.exception))
        self.assertFalse(Path(self.target).exists())   # 不该留下半成品

    def test_rejects_empty(self):
        with self.assertRaises(bili.BiliError):
            bili.import_cookies("   \n  ", self.target)

    def test_rejects_unrecognizable(self):
        with self.assertRaises(bili.BiliError):
            bili.import_cookies('{"cookies": []}', self.target)


class TestImportNetscape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = str(Path(self.dir) / "cookies.txt")

    def test_accepts_whole_file(self):
        rep = bili.import_cookies(_NETSCAPE, self.target)
        self.assertEqual(rep["kind"], "netscape")
        self.assertEqual(rep["total"], 2)
        self.assertTrue(_read(self.target).startswith("# Netscape HTTP Cookie File"))

    def test_accepts_httponly_prefix(self):
        text = ("# Netscape HTTP Cookie File\n"
                "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tzzz\n")
        self.assertTrue(bili._text_has_sessdata(text))
        self.assertEqual(bili.import_cookies(text, self.target)["total"], 1)

    def test_rejects_netscape_without_sessdata(self):
        text = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t1\tbuvid3\ta\n"
        with self.assertRaises(bili.BiliError):
            bili.import_cookies(text, self.target)

    def test_broken_file_does_not_clobber_existing(self):
        bili.import_cookies(_NETSCAPE, self.target)
        before = _read(self.target)
        with self.assertRaises(bili.BiliError):
            bili._commit_cookie_file("# Netscape HTTP Cookie File\n", Path(self.target))
        self.assertEqual(_read(self.target), before)

    def test_creates_missing_directories(self):
        deep = str(Path(self.dir) / "a" / "b" / "cookies.txt")
        bili.import_cookies(_NETSCAPE, deep)
        self.assertTrue(Path(deep).exists())


class TestTargetPath(unittest.TestCase):
    def test_prefers_existing_file(self):
        d = Path(tempfile.mkdtemp())
        existing = d / "here.txt"
        existing.write_text(_NETSCAPE, encoding="utf-8")
        with mock.patch.object(bili, "_resolve_cookie_path", return_value=existing):
            self.assertEqual(bili.default_cookie_target(), existing)

    def test_falls_back_to_standard_path(self):
        with mock.patch.object(bili, "_resolve_cookie_path", return_value=None):
            self.assertEqual(bili.default_cookie_target(),
                             bili._DEFAULT_COOKIE_PATHS[-1])


if __name__ == "__main__":
    unittest.main()
