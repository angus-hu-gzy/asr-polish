# -*- coding: utf-8 -*-
"""阅读页前端逻辑的测试（点句跳视频 / 划重点 / 导出 md / 目录）。

为什么要单独一层
----------------
这几个功能全在浏览器里跑，而测试是 Python。只断言「HTML 里出现了
`localStorage`」证明不了算得对：BV 号从产物文件名里解析得对不对、导出的 md 里
时间戳和句子有没有串位——这类错字符串断言一个都发现不了。

所以页面脚本把几个**纯函数**挂在 `window.__reader` 上（不读 DOM、不发请求），
这里用 node 把它们灌进极简 DOM 桩里跑一遍，再拿结果做断言。真正带点击的流程
（点句子、点星标、切分 P、下载）另有浏览器验证，见提交说明。

node 不在就跳过：这一层是加分项，不该让没装前端工具的人跑不动测试。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from asr_polish.server.reader import html

_HERE = Path(__file__).resolve().parent
_HARNESS = _HERE / "js" / "reader_logic.js"
_NODE = shutil.which("node")


def _page_script() -> str:
    m = re.search(r"<script>(.*?)</script>", html(), re.S)
    if not m:
        raise AssertionError("阅读页里没有 <script> 块")
    return m.group(1)


class ReaderMarkupTest(unittest.TestCase):
    """不依赖 node 的部分：页面里该有的东西在不在。"""

    def setUp(self) -> None:
        self.html = html()

    def test_player_is_official_host(self) -> None:
        """只能内嵌官方播放器。自己拼盗链或者下视频都不是这个项目该干的事。"""
        self.assertIn("https://player.bilibili.com/player.html?", self.html)
        # 外部域名就这两个：播放器本身，和「在 B站打开」的跳链。
        # 多出第三个（CDN 字体、统计脚本…）就说明页面不再自包含。
        hosts = set(re.findall(r"https?://([^/\s\"'<>)]+)", self.html))
        self.assertEqual(hosts, {"player.bilibili.com", "www.bilibili.com"})

    def test_marks_are_local_only(self) -> None:
        """重点只存浏览器：出现后端同步的迹象就是越界了。"""
        self.assertIn("asr-polish:marks:v1:", self.html)
        self.assertNotIn("/api/marks", self.html)

    def test_export_and_toc_entries(self) -> None:
        self.assertIn("导出 md", self.html)
        self.assertIn('id="toc"', self.html)
        self.assertIn("window.__reader", self.html)

    def test_download_uses_blob(self) -> None:
        """导出走 Blob + a[download]，不新增后端接口。"""
        self.assertIn("createObjectURL", self.html)
        self.assertIn(".download", self.html)

    def test_iframe_is_lazy(self) -> None:
        """初始 iframe 没有 src：打开一篇纯文字稿不该先去拉一个播放器。"""
        self.assertNotIn('id="pframe" src=', self.html)

    def test_stat_follows_current_part(self) -> None:
        """右上角统计只算当前这一篇。

        多分 P 时把各篇加起来，会出现屏幕上摆着 8 段、右上角写「182 段」：
        看的人只会以为正文没加载全。所以统计必须随分 P 切换重算。
        """
        self.assertNotIn("S.chars", self.html)      # 全局累加那套不能再回来
        m = re.search(r"function selectPart\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 selectPart")
        self.assertIn("updateStat()", m.group(0))


@unittest.skipUnless(_NODE, "没有 node，跳过前端逻辑测试")
class ReaderLogicTest(unittest.TestCase):
    """纯函数行为：用 node + DOM 桩真跑一遍页面脚本。"""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_page_script())
            cls.script = fh.name
        out = subprocess.run([_NODE, str(_HARNESS), cls.script],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError("node 跑页面脚本失败：%s\n%s"
                                 % (out.stdout[-500:], out.stderr[-2000:]))
        cls.d = json.loads(out.stdout)

    @classmethod
    def tearDownClass(cls) -> None:
        Path(cls.script).unlink(missing_ok=True)

    # ── 点句子跳视频 ────────────────────────────────────
    def test_player_url_uses_official_params(self) -> None:
        url = self.d["playerUrl"]
        self.assertTrue(url.startswith("https://player.bilibili.com/player.html?"))
        self.assertIn("bvid=BV1RXx169Exu", url)
        self.assertIn("p=2", url)
        self.assertIn("t=62", url)          # 62.7 秒向下取整，跳早了不会漏句
        self.assertIn("autoplay=1", url)

    def test_player_url_zero(self) -> None:
        self.assertIn("t=0", self.d["playerUrlZero"])

    def test_part_meta_from_filename(self) -> None:
        """多 P 的 P 号必须从文件名里来——那是唯一带页码的地方。"""
        multi = self.d["partMulti"]
        self.assertEqual(multi["bvid"], "BV1RXx169Exu")
        self.assertEqual(multi["page"], 2)
        self.assertEqual(multi["title"], "【基础篇】Python解释器的安装")
        single = self.d["partSingle"]
        self.assertEqual(single["page"], 1)
        self.assertEqual(single["title"], "【2024最新】Python从零入门")

    def test_part_meta_falls_back_to_target(self) -> None:
        """文件名没 BV 号（--only 重跑/手工文件）时退回 target，并读出 ?p=3。"""
        m = self.d["partFromTarget"]
        self.assertEqual(m["bvid"], "BV1xx411c7mD")
        self.assertEqual(m["page"], 3)

    def test_part_meta_without_bv(self) -> None:
        """都拿不到 BV 号时返回空——界面据此置灰并说明原因，而不是拼个假链接。"""
        self.assertEqual(self.d["partNoBv"]["bvid"], "")

    def test_has_real_time(self) -> None:
        self.assertTrue(self.d["jumpReal"])
        self.assertFalse(self.d["jumpAsr"])     # 必剪 ASR：整块占位时间，没有 start

    # ── 划重点 ────────────────────────────────────────
    def test_mark_key_is_stable(self) -> None:
        self.assertEqual(self.d["mkKey"], "a.srt|3.12|文字")
        self.assertEqual(self.d["mkKeyNoTime"], "a.srt|x|文字")

    # ── 导出 md ───────────────────────────────────────
    def test_markdown_header(self) -> None:
        md = self.d["mdSingle"]
        self.assertTrue(md.startswith("# 【2024最新】Python从零入门到精通系统学习大纲"))
        self.assertIn("- 视频：【2024最新】Python从零入门到精通系统学习大纲（BV1RXx169Exu）", md)
        self.assertIn("- 任务：T-test", md)
        self.assertIn("- 导出：2026-11-05 13:43", md)
        self.assertIn("- 重点：2 条", md)

    def test_markdown_items(self) -> None:
        md = self.d["mdSingle"]
        self.assertIn("1. **00:03** · P1 · [在视频里看]"
                      "(https://www.bilibili.com/video/BV1RXx169Exu/?p=1&t=3)", md)
        self.assertIn("2. **01:27** · P2", md)
        self.assertIn("   第一句重点", md)             # 原文另起一行，缩进进列表项
        self.assertNotIn("第二句\n重点", md)           # 正文里的换行被压平

    def test_markdown_needs_no_timestamp(self) -> None:
        """没有时间戳的重点照样导出，但写明「无时间戳」，不给假链接。"""
        md = self.d["mdAsr"]
        self.assertIn("〔无时间戳〕 · P1", md)
        self.assertNotIn("在视频里看", md)

    def test_markdown_multi_video(self) -> None:
        """跨视频（up:/search: 任务）没法用一个标题，就把 BV 号都列出来。"""
        md = self.d["mdMixed"]
        self.assertIn("2 条 · BV1RXx169Exu、BV1demoASR", md)

    def test_filename_is_safe(self) -> None:
        name = self.d["mdFile"]
        self.assertTrue(name.endswith("-重点.md"), name)
        for bad in '/:*?"<>|':
            self.assertNotIn(bad, name)
        self.assertNotIn("--", name)             # 连续短横压成一个
        self.assertEqual(self.d["mdFileEmpty"], "BV1RXx169Exu-重点.md")

    def test_time_format(self) -> None:
        self.assertEqual(self.d["fmt87"], "01:27")
        self.assertEqual(self.d["fmt3725"], "1:02:05")

    def test_link_and_no_link(self) -> None:
        self.assertEqual(self.d["link"],
                         "https://www.bilibili.com/video/BV1RXx169Exu/?p=1&t=3")
        self.assertEqual(self.d["linkNoTime"], "")


if __name__ == "__main__":
    unittest.main()
