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


if __name__ == "__main__":
    unittest.main()
