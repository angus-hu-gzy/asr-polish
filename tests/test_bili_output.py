# -*- coding: utf-8 -*-
"""外部进程的输出解码：不许因为一行中文就崩掉。

``subprocess.run(..., text=True)`` 按**系统区域编码**解码。中文 Windows 是 cp936，
英文 Windows/CI 是 cp1252，而 yt-dlp 往管道里写的是 UTF-8——它打一行中文标题，
解码就在读线程里抛 UnicodeDecodeError，任务拿到的失败原因是

    UnicodeDecodeError: 'charmap' codec can't decode byte 0xe4 in position 0

用户看到这个只会以为程序坏了，实际是编码猜错了。这里钉住三件事：
UTF-8 正常解、非 UTF-8 也不抛、超时/起不来都翻成人话。
"""
from __future__ import annotations

import subprocess
import sys
import types
import unittest
from unittest import mock

from asr_polish import bili
from asr_polish.bili import BiliError


def _proc(code: int, out: bytes = b"", err: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["yt-dlp"], returncode=code,
                                       stdout=out, stderr=err)


class DecodeOutputTest(unittest.TestCase):
    def test_utf8_chinese(self) -> None:
        raw = "下载失败：视频不存在".encode("utf-8")
        self.assertEqual(bili._decode_output(raw), "下载失败：视频不存在")

    def test_falls_back_to_locale_encoding(self) -> None:
        """yt-dlp 偶尔按控制台编码输出（老版本、或设置了 --encoding）。"""
        raw = "下载失败：视频不存在".encode("gbk")
        with mock.patch("asr_polish.bili.locale.getpreferredencoding",
                        return_value="cp936"):
            self.assertEqual(bili._decode_output(raw), "下载失败：视频不存在")

    def test_never_raises_on_garbage(self) -> None:
        """两种编码都解不了也得给出字符串：这里宁可糊，也不能抛。"""
        raw = b"\xff\xfe\x00\xd8 not text at all \xff"
        with mock.patch("asr_polish.bili.locale.getpreferredencoding",
                        return_value="cp936"):
            out = bili._decode_output(raw)          # 不抛就算过
        self.assertIsInstance(out, str)

    def test_empty(self) -> None:
        self.assertEqual(bili._decode_output(b""), "")
        self.assertEqual(bili._decode_output(None), "")


class DownloadAudioTest(unittest.TestCase):
    """走真实的 _download_audio，但把 yt-dlp 与子进程都换掉。"""

    def setUp(self) -> None:
        self.video = {"page_url": "https://www.bilibili.com/video/BV1xx"}
        self._mods = mock.patch.dict(sys.modules,
                                     {"yt_dlp": types.ModuleType("yt_dlp")})
        self._mods.start()
        self.addCleanup(self._mods.stop)

    def test_non_utf8_stderr_becomes_readable_error(self) -> None:
        """这就是那个 bug 本身：以前这里抛 UnicodeDecodeError。"""
        err = "ERROR: 该视频不存在，可能已被删除".encode("gbk")
        with mock.patch("asr_polish.bili.subprocess.run", return_value=_proc(1, err=err)):
            with self.assertRaises(BiliError) as ctx:
                bili._download_audio(self.video, "C:/tmp", cookie_path=None)
        self.assertIn("yt-dlp 下载音频失败", str(ctx.exception))
        self.assertNotIn("codec", str(ctx.exception))

    def test_utf8_stderr_passes_through(self) -> None:
        err = "ERROR: 需要登录才能观看".encode("utf-8")
        with mock.patch("asr_polish.bili.subprocess.run", return_value=_proc(1, err=err)):
            with self.assertRaises(BiliError) as ctx:
                bili._download_audio(self.video, "C:/tmp", cookie_path=None)
        self.assertIn("需要登录才能观看", str(ctx.exception))

    def test_timeout_is_explained(self) -> None:
        """TimeoutExpired 原来是裸抛的，面板上只显示一个异常类名。"""
        boom = subprocess.TimeoutExpired(cmd="yt-dlp", timeout=600)
        with mock.patch("asr_polish.bili.subprocess.run", side_effect=boom):
            with self.assertRaises(BiliError) as ctx:
                bili._download_audio(self.video, "C:/tmp", cookie_path=None)
        self.assertIn("超时", str(ctx.exception))

    def test_missing_executable_is_explained(self) -> None:
        with mock.patch("asr_polish.bili.subprocess.run",
                        side_effect=OSError("no such file")):
            with self.assertRaises(BiliError) as ctx:
                bili._download_audio(self.video, "C:/tmp", cookie_path=None)
        self.assertIn("启动失败", str(ctx.exception))

    def test_success_returns_first_audio_file(self) -> None:
        with mock.patch("asr_polish.bili.subprocess.run", return_value=_proc(0)), \
                mock.patch("asr_polish.bili.os.listdir",
                           return_value=["audio.m4a.part", "audio.m4a"]):
            self.assertEqual(bili._download_audio(self.video, "C:/tmp"),
                             "C:/tmp\\audio.m4a" if sys.platform == "win32"
                             else "C:/tmp/audio.m4a")


class BcutSegmentsTest(unittest.TestCase):
    """必剪的 utterances → 统一的 ``{start, end, text}``（秒）。

    原来这里只拼文本、把时间丢了，于是走 ASR 的篇等于「没有逐句时间」：srt 导不出、
    ``--correct`` 只能整篇糊一遍、笔记锚点全空。时间本来就在返回里，没有理由丢。
    """

    def test_milliseconds_become_seconds(self) -> None:
        got = bili._bcut_segments({"utterances": [
            {"start_time": 219, "end_time": 920, "transcript": "大家好"},
            {"start_time": 12500, "end_time": 13200, "transcript": "这一节讲分层"},
        ]})
        self.assertEqual([s["start"] for s in got], [0.22, 12.5])
        self.assertEqual([s["end"] for s in got], [0.92, 13.2])
        self.assertEqual([s["text"] for s in got], ["大家好", "这一节讲分层"])

    def test_empty_lines_are_dropped(self) -> None:
        got = bili._bcut_segments({"utterances": [
            {"start_time": 0, "end_time": 400, "transcript": "  "},
            {"start_time": 400, "end_time": 800, "transcript": None},
            {"start_time": 800, "end_time": 1200, "transcript": "有内容"},
        ]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["start"], 0.8)
        self.assertEqual(got[0]["text"], "有内容")

    def test_missing_time_stays_none(self) -> None:
        """时间缺了就留 None，不要补 0——补出来的锚点会指到视频开头。"""
        got = bili._bcut_segments({"utterances": [{"transcript": "没有时间的句子"}]})
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["start"])
        self.assertIsNone(got[0]["end"])

    def test_no_utterances(self) -> None:
        self.assertEqual(bili._bcut_segments({}), [])
        self.assertEqual(bili._bcut_segments({"utterances": []}), [])


class PageUrlTest(unittest.TestCase):
    """换 P 必须换 URL，否则 ASR 下的是第 1 P 的音频。

    实际踩到的：抓一个 6 P 的合集，六个文件全是第 1 P 的正文（330 段、4175 字、
    尾行都一样），因为 `page_url` 在建 `video` 时就固定成不带 `?p=` 的地址，而
    `_download_audio` 只认它。字幕轨那条路用 cid，所以从来没暴露。
    """

    video = {"bvid": "BV1xx", "title": "t", "owner": "o", "duration": 600,
             "cid": 111, "page_url": "https://www.bilibili.com/video/BV1xx",
             "pages": [{"cid": 111, "duration": 300, "part": "P1"},
                       {"cid": 222, "duration": 301, "part": "P2"}]}

    def _fetch(self, page: int) -> str:
        seen = {}

        def fake_download(v, tmpdir, cookie_path=None):
            seen["url"] = v["page_url"]
            seen["cid"] = v["cid"]
            return None                     # 早退：不真下载

        with mock.patch.object(bili, "get_video_info", return_value=dict(self.video)), \
                mock.patch.object(bili, "_fetch_subtitle_detail", return_value=None), \
                mock.patch.object(bili, "_resolve_cookie_path", return_value=None), \
                mock.patch.object(bili, "_download_audio", side_effect=fake_download):
            with self.assertRaises(BiliError):
                bili.fetch("BV1xx", page=page, allow_asr=True)
        self.assertEqual(seen["cid"], 222)
        return seen["url"]

    def test_second_page_url_carries_p2(self) -> None:
        self.assertEqual(self._fetch(2),
                         "https://www.bilibili.com/video/BV1xx?p=2")


if __name__ == "__main__":
    unittest.main()
