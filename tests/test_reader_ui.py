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


def _scripts() -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html(), re.S)


def _page_script() -> str:
    """页面主脚本。

    head 里还有一小段「先把主题定下来」的引导脚本排在前面，所以不能取第一个。
    改用 ``window.__reader`` 这个钩子认人：按位置取的话，以后谁再往 head 加
    一段脚本，这里就会悄悄抓错，而抓错的后果是整层逻辑测试全绿却什么也没测。
    """
    for block in _scripts():
        if "window.__reader" in block:
            return block
    raise AssertionError("阅读页里没有挂 window.__reader 的主脚本")


def _boot_script() -> str:
    """head 里那段定主题的脚本：必须早于样式表跑，否则夜间会闪一帧白。"""
    for block in _scripts():
        if "window.__reader" not in block and "data-theme" in block:
            return block
    raise AssertionError("阅读页里没有 head 引导脚本")


def _root_block(selector: str) -> str:
    """取出 ``:root{…}`` / ``:root[data-theme="dark"]{…}`` 这一块的声明。"""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", html(), re.S)
    if not m:
        raise AssertionError("找不到样式块 %s" % selector)
    return m.group(1)


def _declared_vars(selector: str) -> set[str]:
    return set(re.findall(r"--([\w-]+)\s*:", _root_block(selector)))


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

    def test_search_leaves_hidden_parts_alone(self) -> None:
        """切分 P 之后，搜索的命中与计数都得跟着这一篇走。

        原来 runSearch 只给可见段落上色，但收集命中时用的是不带过滤的
        ``mark.hit``：上一篇藏起来的高亮被一起数进来，切过去显示「1 / 3」，
        按 ↓ 又顺着这些看不见的命中把页面弹回上一篇。
        """
        m = re.search(r"function runSearch\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 runSearch")
        body = m.group(0)
        self.assertIn(".part[hidden] .cue", body)             # 藏起来那篇要擦掉高亮
        self.assertIn(".part:not([hidden]) mark.hit", body)   # 命中只收看得见的

    def test_switching_part_refreshes_search(self) -> None:
        """换篇时重搜一次，否则计数和高亮都停在上一个分 P。"""
        m = re.search(r"function selectPart\(.*?\n\}", _page_script(), re.S)
        self.assertIn("runSearch(q, true)", m.group(0))

    def test_resume_position_is_local_only(self) -> None:
        """读位也只存浏览器：它和重点一样是「本机方便」，不是任务数据。"""
        self.assertIn("asr-polish:pos:v1:", self.html)
        self.assertNotIn("/api/position", self.html)

    def test_resume_reuses_search_scroll(self) -> None:
        """续读要复用搜索那套「滚过去 + 闪一下」，不能另写一套滚动。

        另写一套的后果不是多几行代码：闪动的时长、滚动的居中方式、分 P 没
        显示时的处理全会慢慢分叉，两处行为迟早不一致。
        """
        m = re.search(r"function restorePos\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 restorePos")
        body = m.group(0)
        self.assertIn("focusCue(", body)
        self.assertIn("selectPart(", body)      # 位置在别的篇里，先切篇再滚

    def test_resume_saves_on_scroll_and_unload(self) -> None:
        """滚动停下写一次，关页面前补一次——只靠滚动节流会丢掉最后一屏。"""
        s = _page_script()
        self.assertIn('addEventListener("scroll"', s)
        self.assertIn('addEventListener("pagehide"', s)
        self.assertIn("pickCueIndex", s)

    def test_resume_does_not_fight_search(self) -> None:
        """带 ?q= 进来是明确的「搜这个词」：两套滚动抢起来会落到一处谁也不想要的地方。"""
        m = re.search(r"function renderDoc\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 renderDoc")
        self.assertIn("else restorePos()", m.group(0))

    # ── 夜间模式 ──────────────────────────────────────
    def test_theme_boot_runs_before_styles(self) -> None:
        """定主题的那段必须排在 <style> 之前。

        排后面的话，浏览器已经按白天配色画完一帧才切成夜间——夜间用户每次
        刷新都要被闪一下白，而这正是这个功能本身要解决的问题。
        """
        page = html()
        self.assertLess(page.index(_boot_script()), page.index("<style>"))
        # 两套都声明给 UA 看，它才不会先按自己的猜测铺一层底色
        self.assertIn('content="light dark"', page)

    def test_theme_is_local_only(self) -> None:
        """选择和重点、读位一样只在本机：出现后端同步就是越界了。"""
        self.assertIn("asr-polish:theme:v1", html())
        self.assertNotIn("/api/theme", html())

    def test_boot_and_page_share_one_key(self) -> None:
        """两段脚本各写一遍键名（跨 <script> 块共享不了常量）。

        写岔了的表现很隐蔽：切完当时是好的，下次打开又回去了。这里钉住一致。
        """
        key = "asr-polish:theme:v1"
        self.assertIn(key, _boot_script())
        self.assertIn(key, _page_script())
        self.assertEqual(html().count(key), 2)

    def test_no_colour_hardcoded_outside_the_token_blocks(self) -> None:
        """颜色只能写在两组变量里。

        只要漏一处硬编码，切到夜间它就变成一块漏网的白底——而这恰好是最难
        用肉眼扫全的东西。唯一的例外是播放器框里的 ``#000``：视频底色，白天
        夜里都该是黑的。
        """
        css = re.search(r"<style>(.*?)</style>", html(), re.S).group(1)
        css = re.sub(r':root(\[[^\]]*\])?\s*\{[^}]*\}', "", css)
        left = set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", css))
        self.assertEqual(left, {"#000"})

    def test_dark_defines_every_light_token(self) -> None:
        """夜间那组要把白天每个变量都覆盖掉。

        漏一个，那个元素在夜间还是白天的颜色：白卡片浮在深底上，比整个忘了
        做夜间模式还显眼。反向不要求，夜间可以有白天没有的变量。
        """
        light = _declared_vars(":root")
        self.assertTrue(len(light) > 20, "变量表怎么这么小")
        self.assertEqual(light - _declared_vars(':root[data-theme="dark"]'), set())

    def test_theme_writes_only_on_click(self) -> None:
        """跟随系统的用户不能被悄悄写成固定值，否则系统换了主题他也不跟着换。"""
        m = re.search(r"function initTheme\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 initTheme")
        self.assertNotIn("lsWrite", m.group(0))
        self.assertIn("lsWrite(THEME_KEY", _page_script())


@unittest.skipUnless(_NODE, "没有 node，跳过前端逻辑测试")
class ReaderLogicTest(unittest.TestCase):
    """纯函数行为：用 node + DOM 桩真跑一遍页面脚本。"""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_page_script())
            cls.script = fh.name
        # encoding 必须显式给 utf-8：node 输出的是 UTF-8，而 Windows 的默认
        # 区域编码是 cp1252/gbk，靠 text=True 去猜会在读线程里抛 UnicodeDecodeError，
        # 然后 stdout 变成 None——本机（UTF-8 区域）不会复现，CI 上必炸。
        out = subprocess.run([_NODE, str(_HARNESS), cls.script],
                             capture_output=True, encoding="utf-8",
                             errors="replace", timeout=60)
        if out.returncode != 0 or not out.stdout:
            raise AssertionError("node 跑页面脚本失败（退出码 %s）：%s\n%s"
                                 % (out.returncode, (out.stdout or "")[-500:],
                                    (out.stderr or "")[-2000:]))
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

    # ── 断点续读 ──────────────────────────────────────
    def test_position_key_is_per_task(self) -> None:
        """按任务 id 分块：换一个任务读到别处，不能串。"""
        self.assertEqual(self.d["posKey"], "asr-polish:pos:v1:T-test")

    def test_bad_position_data_falls_back_silently(self) -> None:
        """坏 JSON / 不是对象 / parts 不是对象，一律当「没存过」。

        这里不能抛：抛出来就是整页白屏，而失效的位置本来只是「从头读」而已。
        """
        for key in ("posEmpty", "posBroken", "posNotObject",
                    "posNoParts", "posPartsIsArray"):
            self.assertEqual(self.d[key], {"v": 1, "parts": {}}, key)

    def test_illegal_offsets_are_dropped_one_by_one(self) -> None:
        """一条脏记录不该毁掉整块：负数、字符串、小数、布尔都丢掉，0 留着。"""
        self.assertEqual(self.d["posDirty"]["parts"], {"a": 3, "f": 0})

    def test_position_read_write(self) -> None:
        self.assertEqual(self.d["posRead"], 3)
        self.assertIsNone(self.d["posReadMissing"])
        self.assertIsNone(self.d["posReadNoStore"])
        self.assertEqual(self.d["posWriteNew"]["parts"], {"a": 3, "b": 17})
        self.assertEqual(self.d["posWriteSame"]["parts"], {"a": 40})
        # 写入不能改动传进来的那份：读-改-写之间抛了，半截数据不该流回去
        self.assertEqual(self.d["posWriteLeavesOrig"], 3)

    def test_pick_cue_at_viewport_top(self) -> None:
        """header 下面第一条完整的段落就是「正在读的那段」。"""
        self.assertEqual(self.d["idxMid"], 3)          # 前两段还在界上方
        self.assertEqual(self.d["idxAllAbove"], 2)     # 滚到底：取最后一段
        self.assertEqual(self.d["idxAllBelow"], 0)     # 还在顶上：取第一段
        self.assertEqual(self.d["idxEmpty"], -1)
        self.assertEqual(self.d["idxExact"], 0)        # 正好贴线算「已到」

    def test_pick_cue_is_not_the_previous_one(self) -> None:
        """按「离界最近」会稳定地选错成上一段——这是实测出来的那次偏差。

        `.cue` 有 scroll-margin-top:96px，滚到位后目标段停在界（69）下面 27px，
        而上一段的下沿在 44px 处、离界只有 25px。谁更近一目了然，但更近的那个
        是刚滚过去、正被 header 挡住的段落。判据必须是「第一个 >= 界」。
        """
        # tops 依次是 -42(上一段)、44(再上一段)、96(目标)、183、269
        self.assertEqual(self.d["idxRealLayout"], 2)

    # ── 夜间模式 ──────────────────────────────────────
    def test_theme_key_shared_with_boot_script(self) -> None:
        self.assertEqual(self.d["themeKey"], "asr-polish:theme:v1")

    def test_stored_choice_beats_system(self) -> None:
        """手动选过就听人的，系统怎么设都不管——否则「记住上次选的」就是空话。"""
        self.assertEqual(self.d["themeStoredDarkOnLight"], "dark")
        self.assertEqual(self.d["themeStoredLightOnDark"], "light")

    def test_follows_system_when_never_chosen(self) -> None:
        """没存过就跟随系统：系统深色进夜间，浅色进白天。"""
        self.assertEqual(self.d["themeSystemDark"], "dark")
        self.assertEqual(self.d["themeSystemLight"], "light")

    def test_junk_value_falls_back_to_system(self) -> None:
        """存了个认不出的值（大小写不对、半截 JSON）当没存过。

        不能猜、也不能停在「两套都不对」的状态：退回跟随系统是唯一说得通的
        默认，而且不会白屏。
        """
        self.assertEqual(self.d["themeJunkFollowsSystem"], "dark")
        self.assertEqual(self.d["themeEmptyFollowsSystem"], "light")

    def test_toggle_flips_both_ways(self) -> None:
        self.assertEqual(self.d["nextFromLight"], "dark")
        self.assertEqual(self.d["nextFromDark"], "light")

    def test_label_says_what_clicking_does(self) -> None:
        """按钮上写的是「点了会变成什么」，title 里把当前状态和目标都说全。

        只放一个月亮图标，用户得先点一次才知道它是干嘛的。
        """
        light = self.d["labelLight"]
        self.assertEqual(light["text"], "🌙 夜间")
        self.assertIn("当前是白天", light["title"])
        self.assertIn("点一下切到夜间", light["title"])

        dark = self.d["labelDark"]
        self.assertEqual(dark["text"], "☀️ 白天")
        self.assertIn("当前是夜间", dark["title"])
        self.assertIn("切回白天", dark["title"])

        # 认不出的当前值当白天处理，别给出个空按钮
        self.assertEqual(self.d["labelJunk"]["text"], "🌙 夜间")


if __name__ == "__main__":
    unittest.main()
