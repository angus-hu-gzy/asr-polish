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

    # ── 全文与导出 ────────────────────────────────────
    def test_full_export_sits_in_the_toolbar(self) -> None:
        """全文导出放在搜索条那一行，不塞进重点面板。

        重点面板默认是收起的，放进去等于「先点重点才能导全文」——而全文跟
        重点没有任何关系，一个是整篇，一个是摘录。放工具栏才一直够得着。
        """
        page = self.html
        self.assertIn('id="exportFull"', page)
        self.assertLess(page.index('id="mkbtn"'), page.index('id="exportFull"'))
        # 和重点导出并排，但文案必须分得清是两件事
        self.assertIn("导出全文 md", page)
        self.assertIn("导出 md", page)

    def test_full_export_disabled_until_there_is_a_body(self) -> None:
        """没有产出就不给导出：空文件比按钮点不动更让人困惑。"""
        self.assertIn('id="exportFull" type="button" disabled', self.html)
        m = re.search(r"function renderDoc\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 renderDoc")
        body = m.group(0)
        self.assertIn('$("#exportFull").disabled = true', body)     # 没正文那一支
        self.assertIn('$("#exportFull").disabled = false', body)    # 渲染完才放开

    def test_full_export_goes_through_the_shared_assembler(self) -> None:
        """按钮只准调 buildFullText，不许自己遍历 segments。

        一旦有人在回调里另写一遍「怎么取全文」，这个底座就名存实亡了：以后
        总结层调底座、导出走回调，两条路的段落边界会慢慢分叉，而且不报错。
        """
        m = re.search(r"function exportFull\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 exportFull")
        body = m.group(0)
        self.assertIn("buildFullText(", body)
        self.assertIn("buildFullMarkdown(", body)
        self.assertNotIn("segments", body)
        self.assertNotIn("buildMarkdown(", body)        # 全文不是重点列表

    def test_both_exports_share_one_download_path(self) -> None:
        """重点导出和全文导出都走同一个 saveBlob。

        各写一套的话，revoke 的时机、文件名转义会各改一半，而这种分叉只有
        用户下到空文件时才会发现。
        """
        s = _page_script()
        for fn in ("exportMarks", "exportFull"):
            m = re.search(r"function " + fn + r"\(.*?\n\}", s, re.S)
            self.assertIsNotNone(m, "找不到 " + fn)
            self.assertIn("saveBlob(", m.group(0))
        self.assertEqual(s.count("createObjectURL"), 1)

    # ── 单篇总结 ──────────────────────────────────────
    def test_model_settings_are_local_and_stated_plainly(self) -> None:
        """模型三样只存浏览器，而且页面必须把「全文会发出去」说出来。

        这不是免责声明，是这个功能最需要坦白的一件事：用户点的是「总结」，
        实际动作是把整篇文字稿发到第三方、按那边的规则花钱。藏着不说，
        等他发现账单或发现内容外传，就没有第二次了。
        """
        page = self.html
        self.assertIn('id="modelbox"', page)
        for box in ("mbase", "mname", "mkey"):
            self.assertIn('id="%s"' % box, page)
        self.assertIn('id="mkey" type="password"', page)   # key 不明文晾在屏幕上
        self.assertIn("asr-polish:model:v1", page)
        self.assertNotIn("/api/config", page)              # 没有把配置交给后端存
        self.assertIn("全文会发给你填的这个服务", page)
        self.assertIn("按它的计费规则花钱", page)

    def test_summary_button_says_what_is_missing(self) -> None:
        """没配好就说缺什么——但不能靠在旁边挂一段字来提示。

        工具栏在 1440 宽下本来就是一整行；多一段二十多字的灰字会把它挤成两行，
        正文跟着往下跳。所以状态写在按钮自己身上：文案变「先配模型」、虚线框、
        title 里写全缺哪几样。
        """
        page = self.html
        self.assertIn('id="sumbtn" type="button"', page)
        # 不加 aria-disabled：它是可点的（点了打开设置），对读屏器谎称禁用是错的
        self.assertNotIn('aria-disabled="true"', page)
        self.assertIn('class="btn sm off sumbtn"', page)
        self.assertIn("先配模型", page)
        self.assertIn("先在设置里填服务地址、模型名、API key", page)
        self.assertNotIn('id="sumwhy"', page)      # 那一段灰字不能再回来
        # 在顶栏，不在工具栏：工具栏在 1440 宽下本来就满，再加一个按钮就换行
        self.assertLess(page.index('class="bar"'), page.index('id="sumbtn"'))
        self.assertLess(page.index('id="sumbtn"'), page.index("</header>"))
        m = re.search(r"function updateSumButton\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 updateSumButton")
        body = m.group(0)
        self.assertIn("modelReady(", body)
        self.assertIn("textContent", body)          # 文案跟着配置变
        self.assertIn("off", body)                  # 未配置时换成虚线框

    def test_summary_never_runs_on_page_load(self) -> None:
        """打开页面绝不自动调模型——每次调用都是钱。

        只有两处会调：点「总结」且这一篇还没缓存、点「重新生成」。
        渲染正文、切任务、切分 P 都不许顺手调一把。
        """
        s = _page_script()
        for fn in ("renderDoc", "switchTo", "selectPart"):
            m = re.search(r"function " + fn + r"\(.*?\n\}", s, re.S)
            self.assertIsNotNone(m, "找不到 " + fn)
            self.assertNotIn("generateSummary", m.group(0),
                             fn + " 里不该调模型")
        m = re.search(r'\$\("#sumbtn"\)\.addEventListener\("click".*?\n\}\);', s, re.S)
        self.assertIsNotNone(m, "找不到总结按钮的点击处理")
        self.assertIn("generateSummary(", m.group(0))
        m = re.search(r'\$\("#sumregen"\)\.addEventListener\("click".*?\n\}\);',
                      s, re.S)
        self.assertIn("generateSummary(", m.group(0))

    def test_summary_is_kept_per_part(self) -> None:
        """多分 P：每篇一份，切到哪篇看哪篇。"""
        self.assertIn("asr-polish:summary:v1:", _page_script())
        m = re.search(r"function selectPart\(.*?\n\}", _page_script(), re.S)
        self.assertIn("renderSummary()", m.group(0))

    def test_stale_summary_is_checked_against_the_text(self) -> None:
        """拿着旧总结配新字幕是最坏的一种坏：每条要点都点得动，但都指到别处。"""
        m = re.search(r"function renderSummary\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 renderSummary")
        self.assertIn("summarySig(", m.group(0))

    def test_switching_task_invalidates_in_flight_responses(self) -> None:
        """切任务时，上一轮的请求可能还在路上。

        clearTimeout 只停得掉还没发出的那一次；已经 await 出去的那次回来时，
        会把旧任务的失败原因、分 P 元信息写到新任务页面上，甚至让「点句子」
        跳到上一个视频。所以要一个代次标记，回来先对一眼。
        """
        s = _page_script()
        switch = re.search(r"function switchTo\(.*?\n\}", s, re.S)
        self.assertIsNotNone(switch, "找不到 switchTo")
        self.assertIn("S.gen++", switch.group(0))

        poll = re.search(r"async function poll\(.*?\n\}", s, re.S).group(0)
        self.assertIn("gen !== S.gen", poll)

        load = re.search(r"async function loadTranscript\(.*?\n\}", s, re.S).group(0)
        self.assertIn("gen !== S.gen", load)
        # 拉的是「传进来这个任务」的正文，不是「此刻 S.id 指的那个」——
        # 两者在切换那一瞬间会叉开。
        self.assertIn("encodeURIComponent(task.id)", load)

    def test_unverifiable_anchors_are_not_clickable(self) -> None:
        """没有出处的时间不给点击。

        只判了「有没有 click」不够——真正要守的是**只有核对通过的那一支才绑**。
        所以这里连事件绑定的次数一起钉住：多出来的那一个，就是某处又给
        编出来的时间戳开了后门。
        """
        m = re.search(r"function anchorEl\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 anchorEl")
        body = m.group(0)
        self.assertIn("r.idx >= 0", body)
        self.assertEqual(body.count("addEventListener"), 1)

    # ── 单篇问答 ──────────────────────────────────────
    def test_ask_entry_sits_in_the_header_and_panel_in_the_flow(self) -> None:
        """入口在顶栏，面板在正文上方——和总结同一个位置，不另起浮层。

        工具栏（.tools）在 1440 宽下本来就是一整行，第 1 层把总结按钮挪进顶栏
        就是为了这个；问答同理，不能再往工具栏里塞。
        """
        page = self.html
        self.assertIn('id="askbtn" type="button"', page)
        self.assertLess(page.index('class="bar"'), page.index('id="askbtn"'))
        self.assertLess(page.index('id="askbtn"'), page.index("</header>"))
        self.assertIn('class="qa" id="qa"', page)
        # 面板必须在 main 里、在正文舞台之前：它不挡正文，也不浮动盖住谁
        self.assertLess(page.index('id="qa"'), page.index('class="stage"'))
        for box in ("qain", "qabtn", "qalist", "qanote", "qaclear"):
            self.assertIn('id="%s"' % box, page)

    def test_ask_button_says_what_is_missing(self) -> None:
        """没配好就说缺什么，沿用总结按钮那一套（文案 + 虚线框 + title）。

        另外这里比总结多一样：输入框那一行有地方，所以按钮左边直接写出「缺 API key」，
        不必让人把鼠标停在按钮上才知道差什么。
        """
        self.assertIn('id="qamiss"', self.html)
        m = re.search(r"function updateAskButton\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 updateAskButton")
        body = m.group(0)
        self.assertIn("modelReady(", body)
        self.assertIn("先配模型", body)
        self.assertIn("off", body)
        self.assertIn("qamiss", body)
        self.assertIn("missing.join", body)

    def test_every_question_is_a_paid_call(self) -> None:
        """每次提问都是一次计费调用，界面上必须说清楚，而且常驻。

        这句是价格标签，不是免责声明：用户隔一会儿来问一句，只在第一次弹的
        提示等于没说过。
        """
        page = self.html
        self.assertIn("每点一次「提问」都真的调一次模型服务", page)
        self.assertIn("按你填的那家计费", page)
        self.assertIn("最近 6 轮", page)

    def test_conversation_is_local_only(self) -> None:
        """对话只存浏览器：没有账号，也就不该有第二份。"""
        page = self.html
        self.assertIn("asr-polish:qa:v1:", page)
        self.assertNotIn("/api/qa", page)          # 没有把对话交给后端存

    def test_ask_never_runs_on_page_load(self) -> None:
        """打开页面、切任务、切分 P 都不许顺手调模型。"""
        s = _page_script()
        for fn in ("renderDoc", "switchTo", "selectPart", "renderQa"):
            m = re.search(r"function " + fn + r"\(.*?\n\}", s, re.S)
            self.assertIsNotNone(m, "找不到 " + fn)
            self.assertNotIn("askQuestion(", m.group(0), fn + " 里不该调模型")
        # 只有两处会发问：示例问题、输入框提交
        self.assertEqual(s.count("askQuestion("), 3)   # 定义 1 + 调用 2

    def test_switching_part_repaints_the_conversation(self) -> None:
        """多分 P：每篇一份对话，切到哪篇看哪篇。"""
        m = re.search(r"function selectPart\(.*?\n\}", _page_script(), re.S)
        self.assertIn("renderQa()", m.group(0))
        m = re.search(r"function switchTo\(.*?\n\}", _page_script(), re.S)
        self.assertIn('$("#qa").hidden = true', m.group(0))

    def test_stale_conversation_is_checked_against_the_text(self) -> None:
        """拿着旧对话配新字幕，比没有对话更糟：问题问的是旧内容。"""
        m = re.search(r"function renderQa\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(m, "找不到 renderQa")
        self.assertIn("summarySig(", m.group(0))

    def test_answer_without_anchors_says_so(self) -> None:
        """一条有效出处都没有时明说，不能悄悄摆得和有出处的一样可信。"""
        s = _page_script()
        m = re.search(r"function answerEl\(.*?\n\}", s, re.S)
        self.assertIsNotNone(m, "找不到 answerEl")
        body = m.group(0)
        self.assertIn("本次回答没有可验证的出处，仅供参考。", body)
        self.assertIn("!valid", body)

    def test_part_without_time_warns_at_the_top(self) -> None:
        """没有逐句时间的篇：说明在顶上、常驻，而且一个锚点都不给。"""
        page = self.html
        self.assertIn("这一篇没有逐句时间戳，回答无法定位到视频", page)
        # 说明在对话列表之前：读到回答的最后才发现「定位不了」，等于白读
        self.assertLess(page.index('id="qanote"'), page.index('id="qalist"'))
        body = re.search(r"function answerEl\(.*?\n\}", _page_script(), re.S).group(0)
        self.assertIn("!part.hasTime", body)       # 无时间的篇原样留方括号、不画锚点
        self.assertIn("if (part.hasTime && !valid)", body)

    def test_txt_only_part_offers_a_redo_button(self) -> None:
        """只落了 txt 的篇：给一个能点的按钮，不是让人去学命令行。

        原来这句写的是「命令行加 --format srt / 接口里写 formats」。对着从
        面板建任务的人等于没说：那两处他都没有。说得清「怎么补」不够，得让
        他点得动。
        """
        body = re.search(r"function partNote\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(body, "找不到 partNote")
        self.assertIn('el("button", "btn sm redo"', body.group(0))
        # 只有「这次确实没要 srt」才给按钮。必剪 ASR 落的是带占位时间的 srt，
        # 走不到这一支；没有 BV 号的那一篇重抓也无从下手，也不给。
        self.assertIn('part.source === "txt" && pm.bvid', body.group(0))
        # 代价写清楚：新任务、只抓这一篇、原来那份留着
        self.assertIn("原来那个任务留着不动", body.group(0))
        self.assertIn('"只抓第 " + (pm.page || 1)', body.group(0))
        # 按钮另起一行。和解释挤同一行时说明会被折成两行，第一版就这样
        self.assertIn('el("div", "redo-row")', body.group(0))

    def test_redo_asks_for_a_time_axis(self) -> None:
        """重抓这一篇：要 srt，只抓这一分 P，失败要说原因。

        只抓这一篇是有意的——一条 100 P 的课程从「点不动」变成「重抓两小时」
        不是这个按钮该干的事。失败要能再点一次，而不是把按钮永久变灰。
        """
        body = re.search(r"async function refetchWithTime\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(body, "找不到 refetchWithTime")
        src = body.group(0)
        self.assertIn('formats: ["srt", "txt"]', src)
        self.assertIn("pages: String(pm.page || 1)", src)
        self.assertIn("target: pm.bvid", src)
        self.assertIn("没能提交", src)              # 失败说原因，不是只说「失败」
        self.assertIn("btn.disabled = false", src)  # 还能再点
        self.assertIn("switchTo(res.data.id", src)  # 成了就切到新任务并开始轮询

    def test_submit_asks_for_a_time_axis(self) -> None:
        """阅读页自己提交任务时，srt 与 txt 一起要。

        只要 txt 的任务在这个页面上是「点不动、标不出锚点」的半残品；只要 srt
        又和面板建出来的任务产物不一样。同一个任务在哪儿建的，落盘不该两样。
        """
        body = re.search(r"async function submit\(.*?\n\}", _page_script(), re.S)
        self.assertIsNotNone(body, "找不到 submit")
        self.assertIn('formats: ["srt", "txt"]', body.group(0))

    def test_empty_conversation_offers_examples(self) -> None:
        s = _page_script()
        m = re.search(r"var QA_SUG = \[(.*?)\];", s, re.S)
        self.assertIsNotNone(m, "找不到示例问题")
        self.assertEqual(len(re.findall(r'"', m.group(1))), 6)   # 三句
        self.assertIn("这篇主要讲了什么？", m.group(1))
        m = re.search(r"function renderSuggestions\(.*?\n\}", s, re.S)
        self.assertIn("askQuestion(", m.group(0))

    def test_summary_and_ask_anchors_do_not_share_click_state(self) -> None:
        """两边各自的「第二次点击」要分开记。

        混在一起时：在总结里点过 01:03，再到问答里点同一个 01:03，会直接跳视频
        而不是先回到那一段——一次点击干了两件事。
        """
        m = re.search(r"function anchorEl\(.*?\n\}", _page_script(), re.S)
        body = m.group(0)
        self.assertIn("scope", body)
        self.assertIn("S.sumAnchor = key", body)
        # 问答那边必须传自己的作用域
        s = _page_script()
        self.assertIn('anchorEl(r, part, "qa")', s)


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

    # ── 全文底座（工作台各层的入口）────────────────────
    def test_full_text_groups_by_part_and_keeps_titles(self) -> None:
        """按分 P 分组，每篇留着标题——以后总结要按篇归档，没标题分不清谁是谁。"""
        full = self.d["fullMulti"]
        self.assertEqual(len(full["parts"]), 2)
        self.assertEqual([p["label"] for p in full["parts"]], ["P1", "P2"])
        self.assertEqual(full["parts"][0]["title"], "【前言】Python从零入门到精通系统学习大纲")
        self.assertEqual(full["parts"][1]["title"], "【基础篇】Python解释器的安装")
        self.assertEqual(full["parts"][1]["bvid"], "BV1RXx169Exu")
        self.assertEqual(full["paragraphs"], 4)

    def test_full_text_keeps_seconds_not_strings(self) -> None:
        """段内时间是秒（float），不是格式化过的字符串。

        以后「这句话在第几分钟」「引到视频哪一秒」都靠它；这里存成 "01:03"
        就等于把精度丢了，再想算回去得反解析。
        """
        segs = self.d["fullMulti"]["parts"][1]["segments"]
        self.assertEqual([s["start"] for s in segs], [63, 70])
        self.assertEqual(segs[0], {"start": 63, "end": 66, "text": "打开官网下载安装包"})
        self.assertTrue(self.d["fullMulti"]["parts"][1]["hasTime"])

    def test_asr_placeholder_loses_its_fake_time(self) -> None:
        """必剪 ASR 的 0 → 99:59:59 占位块：时间一律 null，并标明没有时间戳。

        留着 99:59:59 比没有时间还糟——下游会把它当成真的去引用。也不能「平均
        摊」出一串时间，那是编数据。
        """
        part = self.d["fullAsr"]["parts"][0]
        self.assertFalse(part["hasTime"])
        self.assertEqual([s["start"] for s in part["segments"]], [None, None])
        self.assertEqual([s["text"] for s in part["segments"]], ["第一段", "第二段"])
        md = self.d["fullAsrMd"]
        # 说明文字里提到 99:59:59 是可以的（那是在解释为什么没有时间），
        # 但每一段前面的标注不能是一个数字时间
        self.assertNotIn("[99:59:59]", md)
        stamps = [ln for ln in md.splitlines() if ln.startswith("[")]
        self.assertEqual(len(stamps), 2)
        self.assertTrue(all(ln.startswith("[无时间戳]") for ln in stamps), stamps)
        self.assertIn("[无时间戳] 第一段", md)
        self.assertIn("没有逐句时间戳", md)

    def test_txt_part_degrades_without_inventing_time(self) -> None:
        """纯文本导出本来就没有时间：一样是 null，一行一段。"""
        part = self.d["fullTxt"]["parts"][0]
        self.assertFalse(part["hasTime"])
        self.assertEqual(len(part["segments"]), 2)
        self.assertTrue(all(s["start"] is None for s in part["segments"]))

    def test_multi_video_uses_index_not_page_number(self) -> None:
        """跨视频任务（up:/search:）里每个文件都是 P1，标 P1/P1 只会让人困惑。"""
        labels = [p["label"] for p in self.d["fullMixed"]["parts"]]
        self.assertEqual(labels, ["第 1 篇", "第 2 篇"])
        self.assertIn("- BV 号：2 条 · BV1RXx169Exu、BV1demoASR", self.d["fullMixedMd"])

    def test_single_part_has_no_label(self) -> None:
        """单篇不挂 P1：一个文件而已，标个 P 号反而像还有别的没导出来。"""
        self.assertEqual(self.d["fullAsr"]["parts"][0]["label"], "")
        self.assertNotIn("## P1", self.d["fullAsrMd"])

    def test_empty_task_stays_empty(self) -> None:
        """空任务不抛、不造一个空段落出来；三个出口都干净。"""
        for key in ("fullEmpty", "fullNull"):
            self.assertEqual(self.d[key]["parts"], [])
            self.assertEqual(self.d[key]["paragraphs"], 0)
            self.assertEqual(self.d[key]["chars"], 0)
        self.assertEqual(self.d["fullEmptyPlain"], "")
        self.assertEqual(self.d["fullEmptyMd"], "")

    def test_full_markdown_header(self) -> None:
        md = self.d["fullMultiMd"]
        self.assertTrue(md.startswith("# 【2024最新】Python从零入门到精通系统学习大纲 · 全文"))
        self.assertIn("- 标题：【2024最新】Python从零入门到精通系统学习大纲", md)
        self.assertIn("- BV 号：BV1RXx169Exu", md)
        self.assertIn("- 分 P：2 篇", md)
        self.assertIn("- 导出：2026-11-05 20:31", md)
        self.assertIn("- 正文：41 字 · 4 段", md)

    def test_full_markdown_sections_and_stamps(self) -> None:
        md = self.d["fullMultiMd"]
        self.assertIn("## P1 · 【前言】Python从零入门到精通系统学习大纲", md)
        self.assertIn("## P2 · 【基础篇】Python解释器的安装", md)
        # 时间戳就是页面上那个 fmt：同一段在两处显示必须一致，否则用户会以为
        # 导出的是另一份数据
        self.assertIn("[00:00] 大家好，这一期我们聊聊 Python", md)
        self.assertIn("[01:03] 打开官网下载安装包", md)
        # 顺序：P1 两段都在 P2 之前
        self.assertLess(md.index("[00:05] 先从安装开始"), md.index("## P2"))

    def test_plain_text_is_what_gets_sent_to_a_model(self) -> None:
        """发给模型的正文：一行一段、分 P 之间 ---、不带时间戳。

        时间戳在结构里一个都没丢，需要的那一层直接读结构；塞进纯文本只是噪音。
        """
        txt = self.d["fullMultiPlain"]
        self.assertIn("## P1 · 【前言】Python从零入门到精通系统学习大纲", txt)
        self.assertIn("大家好，这一期我们聊聊 Python\n先从安装开始", txt)
        self.assertIn("\n\n---\n\n", txt)
        self.assertNotIn("[00:00]", txt)
        # 单篇不套壳：一句话就是一段
        self.assertEqual(self.d["fullAsrPlain"], "第一段\n第二段")

    def test_full_filename_follows_the_marks_habit(self) -> None:
        self.assertEqual(self.d["fullName"],
                         "【2024最新】Python从零入门到精通系统学习大纲-全文.md")
        # 同一个标题，重点导出仍是 -重点.md：加了个参数不能改变老行为
        self.assertEqual(self.d["mdFile"], "Python-从零-入门-精讲-重点.md")

    # ── 单篇总结 ──────────────────────────────────────
    def test_model_config_round_trip_and_junk(self) -> None:
        """三样读得回来；坏 JSON、字段类型不对，一律当「没配」。"""
        self.assertEqual(self.d["modelOk"], {
            "base": "api.deepseek.com/v1", "model": "deepseek-chat",
            "key": "sk-fake-for-test"})
        for key in ("modelJunk", "modelBroken"):
            self.assertEqual(self.d[key], {"base": "", "model": "", "key": ""}, key)

    def test_missing_config_names_the_missing_parts(self) -> None:
        """缺什么说什么：只缺 key 时不该把地址也一起报上去。"""
        self.assertTrue(self.d["readyOk"]["ok"])
        self.assertEqual(self.d["readyMissingKey"]["missing"], ["API key"])
        self.assertIn("API key", self.d["readyMissingKey"]["why"])
        self.assertEqual(self.d["readyMissingTwo"]["missing"], ["服务地址"])
        self.assertEqual(self.d["readyEmpty"]["missing"],
                         ["服务地址", "模型名", "API key"])

    def test_storage_keys(self) -> None:
        self.assertEqual(self.d["modelKeyName"], "asr-polish:model:v1")
        self.assertEqual(self.d["sumKeyName"], "asr-polish:summary:v1:T-test")

    def test_summary_source_carries_real_timestamps(self) -> None:
        """发给模型的正文必须带行首时间，否则它只能编——而编出来的会被判无出处。

        时间用的是页面上那个 fmt，和正文里显示的一致：模型抄回去的是它看见的
        那个字符串，两处不一样就永远对不上。
        """
        src = self.d["srcP2"]
        self.assertTrue(src["hasTime"])
        self.assertEqual(src["text"],
                         "[01:03] 打开官网下载安装包\n[01:10] 下一步下一步就行")

    def test_summary_source_for_a_part_without_time(self) -> None:
        """必剪 ASR / 纯文本：不标时间，并且明确告诉模型 t 写 null。"""
        src = self.d["srcAsr"]
        self.assertFalse(src["hasTime"])
        self.assertIn("t 请写成 null", src["text"])
        self.assertIn("第一段\n第二段", src["text"])
        self.assertNotIn("[", src["text"].split("）")[-1])   # 正文里没有假时间

    def test_summary_source_out_of_range(self) -> None:
        self.assertEqual(self.d["srcOob"]["text"], "")
        self.assertFalse(self.d["srcOob"]["hasTime"])

    def test_stamp_formats(self) -> None:
        self.assertEqual(self.d["stampMmss"], 63)
        self.assertEqual(self.d["stampHms"], 3725)
        self.assertEqual(self.d["stampSec"], 83)
        self.assertEqual(self.d["stampDigits"], 83)      # 数字串当秒
        self.assertEqual(self.d["stampBracket"], 63)     # 连方括号一起抄回来了
        self.assertIsNone(self.d["stampBad"])
        self.assertIsNone(self.d["stampNull"])

    def test_anchor_matching_has_a_small_tolerance(self) -> None:
        """容差是为了「页面上四舍五入过的时间」——不是为了让编的时间通过。"""
        self.assertEqual(self.d["matchNear"], 2)    # 63.4 → 第 3 段
        self.assertEqual(self.d["matchFar"], -1)    # 70 秒：原文里没有，判无出处
        self.assertEqual(self.d["matchNull"], -1)
        self.assertEqual(self.d["matchEmpty"], -1)

    def test_made_up_timestamps_become_unanchored(self) -> None:
        """模型编的时间不给锚点，没给时间的也一样。"""
        rows = self.d["anchors"]
        self.assertEqual(rows[0]["idx"], 2)         # 01:03 真的在原文里
        self.assertEqual(rows[1]["idx"], -1)        # 07:12 是编的
        self.assertEqual(rows[1]["t"], 432)         # 时间本身仍记着，用来标注
        self.assertEqual(rows[2]["idx"], -1)        # 没给

    def test_part_without_time_ignores_model_stamps(self) -> None:
        """这一篇没有逐句时间：模型就算给了时间也是编的，一律无出处。"""
        self.assertEqual(self.d["anchorsNoTime"][0]["idx"], -1)

    def test_signature_changes_with_the_text(self) -> None:
        """同一篇稳定，换一篇就变——缓存靠它判断「是不是同一份字幕」。"""
        self.assertTrue(self.d["sigStable"])
        self.assertTrue(self.d["sigChangesWithText"])
        self.assertEqual(self.d["sigEmpty"], "0")

    def test_summary_store_round_trip(self) -> None:
        store = self.d["sumStore"]
        self.assertEqual(sorted(store["parts"]), ["a.srt", "b.srt"])
        item = self.d["sumOfA"]
        self.assertEqual(item["summary"]["tldr"], "一句话总览")
        self.assertEqual(item["summary"]["points"][0]["text"], "第一条")
        self.assertEqual(item["sig"], "3:20:开头|结尾")
        # b 那条 points 不是数组：收成空数组，不把整条丢掉（sig 还有用）
        self.assertEqual(store["parts"]["b.srt"]["summary"]["points"], [])

    def test_bad_summary_stores_fall_back(self) -> None:
        for store in self.d["badStores"]:
            self.assertEqual(store, {"v": 1, "parts": {}}, store)
        self.assertIsNone(self.d["sumOfMissing"])
        self.assertIsNone(self.d["sumOfNull"])

    def test_writing_a_summary_does_not_mutate_the_input(self) -> None:
        self.assertEqual(self.d["sumWrite"]["parts"]["b.srt"]["sig"], "9:9:新|新")
        self.assertEqual(self.d["sumWriteLeavesOrig"], "2:9:x|y")

    # ── 单篇问答 ──────────────────────────────────────
    def test_citation_split_only_takes_bracketed_stamps(self) -> None:
        """只认方括号里的 [分:秒]。

        裸的 12:34 在回答里可能是比例、比数、页码；认错了就是给一句普通的话
        配了个跳转，比没有锚点更误导人。
        """
        self.assertEqual(self.d["citesPlain"], [{"text": "没有时间的一句话"}])
        one = self.d["citesOne"]
        self.assertEqual(one[1]["raw"], "[01:03]")
        self.assertEqual(one[1]["t"], 63)
        self.assertEqual(one[0]["text"], "先装 Python ")
        # 两句引用挨着写也要各算各的
        self.assertEqual([r["t"] for r in self.d["citesAdjacent"]], [63, 70])
        self.assertEqual(self.d["citesHms"][1]["t"], 3725)
        # 不是时间的方括号原样留着，不许被当成引用切走
        self.assertEqual(self.d["citesJunk"], [{"text": "[abc] 和 [12:34:56:78]"}])
        self.assertEqual(self.d["citesEmpty"], [])

    def test_answer_citations_are_checked_against_real_segments(self) -> None:
        """模型给的时间要拿真实段首核一遍，对不上的不给锚点。"""
        runs = self.d["linesOk"][0]["runs"]
        cites = [r for r in runs if r.get("cite")]
        self.assertEqual([c["idx"] for c in cites], [0, 1])
        made_up = [r for r in self.d["linesMadeUp"][0]["runs"] if r.get("cite")]
        self.assertEqual([r["idx"] for r in made_up], [-1, -1])   # 00:00 和 07:12
        # 空行保留成段落间隔，两段之间不能粘成一段
        self.assertTrue(self.d["linesMadeUp"][1]["blank"])
        self.assertEqual([r["idx"] for r in self.d["linesMadeUp"][2]["runs"]
                          if r.get("cite")], [0])

    def test_answer_on_a_part_without_time_never_anchors(self) -> None:
        """这一篇本来没有逐句时间：模型硬给一个也一律无出处。"""
        runs = self.d["linesNoTime"][0]["runs"]
        cites = [r for r in runs if r.get("cite")]
        self.assertEqual(len(cites), 1)
        self.assertEqual(cites[0]["idx"], -1)

    def test_ask_source_reuses_the_summary_assembler(self) -> None:
        """正文只有一份拼法：问答用的就是 buildSummarySource 的结果。

        唯一的差别是摘掉总结那句「所有要点的 t 请写成 null」——问答没有「要点」，
        那句话混在正文里会被当成原文。摘掉之后「这一篇没有时间」由服务端提示词说。
        """
        p1 = self.d["askSrcP1"]
        self.assertTrue(p1["hasTime"])
        self.assertTrue(p1["text"].startswith("[01:03] "), p1["text"][:20])
        asr = self.d["askSrcAsr"]
        self.assertFalse(asr["hasTime"])
        self.assertNotIn("t 请写成 null", asr["text"])
        self.assertTrue(asr["text"].startswith("第一段"), asr["text"][:20])

    def test_conversation_store_is_per_part(self) -> None:
        store = self.d["qaStore"]
        # 空的（msgs 是空数组）和坏的（msgs 不是数组）都不占位
        self.assertEqual(sorted(store["parts"]), ["a.srt"])
        # 只有「问」没有「答」的那一轮要丢掉：半截对话喂不回模型
        self.assertEqual([m["q"] for m in store["parts"]["a.srt"]["msgs"]], ["问1"])
        self.assertEqual(store["parts"]["a.srt"]["msgs"][0]["a"], "答1 [01:03]")
        self.assertEqual(self.d["qaKeyName"], "asr-polish:qa:v1:T-test")

    def test_bad_conversation_stores_fall_back(self) -> None:
        for store in self.d["qaBad"]:
            self.assertEqual(store, {"v": 1, "parts": {}}, store)

    def test_clearing_keeps_the_other_parts(self) -> None:
        """「清空对话」清的是当前这一篇，不是全部。"""
        cleared = self.d["qaClear"]
        self.assertEqual(sorted(cleared["parts"]), ["b.srt"])
        self.assertEqual(cleared["parts"]["b.srt"]["msgs"][0]["q"], "q2")

    def test_writing_a_conversation_does_not_mutate_the_input(self) -> None:
        written = self.d["qaWrite"]
        self.assertIn("d.srt", written["parts"])
        self.assertEqual(written["parts"]["a.srt"]["msgs"][0]["q"], "旧问")


if __name__ == "__main__":
    unittest.main()
