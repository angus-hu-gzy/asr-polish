# -*- coding: utf-8 -*-
"""第 4 层「合集知识库」的测试：提示词、概念怎么归一、笔记怎么写、怎么搜回来。

为什么这一层要单独一个文件
--------------------------
前三层的结果都在浏览器里，写错了最多是这一次读得不爽；这一层往磁盘上写文件，
而文件会被读很多次、被 Obsidian 打开、被 git 管版本。于是有三类错必须挡住：

1. **编出来的东西进了文件**——没有定义的概念、对不上的时间锚点。假信息一旦落盘，
   半年后没人分得出哪条是原文讲过的。
2. **该合并的没合并、不该合并的合并了**——概念按名字去重，归一太松会把两个概念
   焊死（不可逆），太紧会让同一个概念散成十个文件（这个库就白攒了）。
3. **重写把旧内容抹掉**——概念笔记是「读出来 → 追加 → 整篇写回」，读的时候漏认
   一行，写回去那行就没了，而且看不出是谁删的。

后两类都在「同一个概念出现两次」这条路上，所以这里花了最多篇幅去压这条路径。

页面的纯函数那部分（`window.__notes`）用 node 灌进 DOM 桩里真跑一遍，见文末。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from asr_polish.server import App, Store, make_server, notes, notes_page
from asr_polish.server.summary import SummaryError

_KEY = "sk-secret-must-not-leak"

#: 一篇有逐句时间的正文（行首带 [mm:ss]，和阅读页 buildModelSource 拼的一样）。
_BODY = "[00:00] 大家好，这一期聊限流\n[00:30] 先看阈值怎么调\n[01:03] 装完配 PATH"

#: 这一篇的真实段首。锚点必须落在这些数上（±1.5 秒）。
_SEGS = [{"start": 0.0, "end": 4.0, "text": "大家好，这一期聊限流"},
         {"start": 30.4, "end": 34.0, "text": "先看阈值怎么调"},
         {"start": 63.0, "end": 66.0, "text": "装完配 PATH"}]


def _meta(**kw) -> dict:
    """写一篇笔记要的那一份 meta，按需覆盖。"""
    d = {"title": "带时间的一篇", "bvid": "BV1TEST00001", "label": "P1",
         "date": "2026-11-07", "at": "2026-11-07 13:00",
         "task_id": "T569ceb995d", "part_index": 0,
         "part_name": "BV1TEST00001-P1-带时间的一篇.srt",
         "has_time": True, "sig": notes.part_sig({"segments": _SEGS}),
         "model": "fake-notes", "segments": _SEGS}
    d.update(kw)
    return d


def _c(term, define, t=None):
    return {"term": term, "def": define, "t": t}


class _Rec:
    def __init__(self) -> None:
        self.body: dict = {}


def _serve(reply: str, code: int = 200):
    """一个只回固定 JSON 的假模型服务，端口由系统分配。"""
    rec = _Rec()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:                       # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                rec.body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                rec.body = {}
            payload = reply.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, rec


def _reply(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

class PromptTest(unittest.TestCase):
    """提示词是服务端常量：这几条少一条，后面全白搭。"""

    def test_four_requirements_are_written_down(self) -> None:
        p = notes.PROMPT
        self.assertIn("确实讲过", p)        # 只抽原文讲过的
        self.assertIn("不许用你自己的知识补", p)
        self.assertIn("原样抄", p)          # 出处时间原样抄行首
        self.assertIn("不许写原文里没有的时间", p)
        self.assertIn("一句话", p)          # 每个概念一句话定义（模板 + 第 5 条）
        self.assertIn("要短", p)            # 概念名短且稳定
        self.assertIn("必须原样复用那个名字", p)   # 已有概念名要复用

    def test_no_time_rule_is_not_in_the_base_prompt(self) -> None:
        """有逐句时间的篇不许把「t 一律写 null」发过去，否则模型连时间都不敢写。"""
        self.assertIn("没有逐句时间戳", notes.NO_TIME_RULE)
        self.assertNotIn(notes.NO_TIME_RULE, notes.PROMPT)

    def test_known_names_are_names_only(self) -> None:
        """已有概念名只给名字：给全文等于每次把整个库发一遍。"""
        s = notes.build_system(["阈值", "限流"], True)
        self.assertIn(notes.KNOWN_HEAD, s)
        self.assertIn("阈值、限流", s)
        self.assertNotIn("触发判定", s)

    def test_first_ever_note_says_so(self) -> None:
        """库还空着的时候要明说「还没有」。

        留空的话模型会以为「这一段没给」，转而自己发挥，第一个概念就起偏了。
        """
        s = notes.build_system([], True)
        self.assertIn("还没有", s)

    def test_names_are_normalized_before_showing(self) -> None:
        s = notes.build_system(["  阈 值 ", "全角（括号）"], True)
        self.assertIn("阈 值", s)
        self.assertIn("全角(括号)", s)      # NFKC 把全角括号折成半角

    def test_no_time_rule_appended_only_when_needed(self) -> None:
        self.assertNotIn(notes.NO_TIME_RULE, notes.build_system([], True))
        self.assertIn(notes.NO_TIME_RULE, notes.build_system([], False))


class ConceptKeyTest(unittest.TestCase):
    """概念名归一：认写法差异，不认语义差异。"""

    def test_whitespace_and_width(self) -> None:
        k = notes.concept_key
        self.assertEqual(k("  阈值 "), k("阈值"))
        self.assertEqual(k("全角（括号）"), k("全角(括号)"))
        self.assertEqual(k("a\tb"), k("a  b"))
        self.assertEqual(k(" HTTP 缓存 "), k("http 缓存"))

    def test_semantics_are_not_merged(self) -> None:
        """近义、繁简都不合并：焊错了不可逆，用户再也分不开。"""
        k = notes.concept_key
        self.assertNotEqual(k("阈值"), k("门限"))
        self.assertNotEqual(k("阈值"), k("閥值"))
        # 标点不动：「A/B 测试」和「AB 测试」不是一回事
        self.assertNotEqual(k("A/B 测试"), k("AB 测试"))

    def test_display_form_keeps_case(self) -> None:
        """写进文件的形态不改大小写：`HTTP 缓存` 就该是大写。"""
        self.assertEqual(notes.concept_name("  HTTP  缓存 "), "HTTP 缓存")
        self.assertEqual(notes.concept_key("HTTP 缓存"), "http 缓存")


# --------------------------------------------------------------------------- #
# 模型输出
# --------------------------------------------------------------------------- #

class NormalizeTest(unittest.TestCase):
    """模型的输出 → 写进文件的那几条。**不补内容**是这一层的底线。"""

    def test_ok(self) -> None:
        got = notes.normalize({
            "points": [{"text": "讲了限流", "t": "00:00"}],
            "concepts": [_c("阈值", "触发判定的界线", "00:30")]})
        self.assertEqual(got["points"][0]["t"], 0)
        self.assertEqual(got["concepts"][0]["term"], "阈值")
        self.assertEqual(got["concepts"][0]["t"], 30)
        self.assertEqual(got["skipped"], [])

    def test_missing_definition_is_dropped_with_reason(self) -> None:
        got = notes.normalize({"points": [{"text": "讲了点啥", "t": "00:00"}],
                               "concepts": [_c("阈值", "")]})
        self.assertEqual(got["concepts"], [])
        self.assertTrue(any("没有定义" in s for s in got["skipped"]))

    def test_missing_time_is_dropped_when_the_part_has_times(self) -> None:
        """有逐句时间的篇里没给出处 = 这条概念点不回去。留着会让人以为本来就没时间。"""
        got = notes.normalize({"points": [{"text": "讲了点啥", "t": "00:00"}],
                               "concepts": [_c("阈值", "定义")]}, has_time=True)
        self.assertEqual(got["concepts"], [])
        self.assertTrue(any("没有出处时间" in s for s in got["skipped"]))

    def test_missing_time_is_fine_without_timestamps(self) -> None:
        """必剪那一篇本来就没有逐句时间：留着，但 t 是 None（不编）。"""
        got = notes.normalize({"points": [{"text": "讲了点啥"}],
                               "concepts": [_c("阈值", "定义")]}, has_time=False)
        self.assertIsNone(got["concepts"][0]["t"])
        self.assertEqual(len(got["concepts"]), 1)

    def test_empty_name_is_dropped(self) -> None:
        got = notes.normalize({"points": [{"text": "x", "t": "00:00"}],
                               "concepts": [_c("   ", "有定义")]})
        self.assertEqual(got["concepts"], [])
        self.assertTrue(any("没有名字" in s for s in got["skipped"]))

    def test_same_concept_twice_is_merged(self) -> None:
        """`阈值` 和 `  阈值 ` 归一后同名：文件里一条，界面也只该说一条。"""
        got = notes.normalize({
            "points": [{"text": "x", "t": "00:00"}],
            "concepts": [_c("阈值", "第一条定义", "00:00"),
                         _c("  阈值 ", "换个空格再写一遍", "00:30")]})
        self.assertEqual([c["term"] for c in got["concepts"]], ["阈值"])
        self.assertEqual(got["concepts"][0]["def"], "第一条定义")

    def test_nothing_usable_is_an_error(self) -> None:
        with self.assertRaises(SummaryError) as cm:
            notes.normalize({"points": [], "concepts": [_c("阈值", "")]})
        self.assertEqual(cm.exception.kind, "parse")
        self.assertIn("缺定义", cm.exception.message)

    def test_caps(self) -> None:
        """一条拆成三条把文件灌满：上限挡在这里。"""
        got = notes.normalize({
            "points": [{"text": "第 %d 条" % i, "t": "00:00"}
                       for i in range(notes.MAX_POINTS + 5)],
            "concepts": [_c("概念%d" % i, "定义", "00:00")
                         for i in range(notes.MAX_CONCEPTS + 5)]})
        self.assertEqual(len(got["points"]), notes.MAX_POINTS)
        self.assertEqual(len(got["concepts"]), notes.MAX_CONCEPTS)


class StampTest(unittest.TestCase):
    """锚点：模型抄的时间要和这一篇**真实的段首**对得上。"""

    def test_parse(self) -> None:
        self.assertEqual(notes.parse_stamp("01:03"), 63)
        self.assertEqual(notes.parse_stamp("1:02:05"), 3725)
        self.assertEqual(notes.parse_stamp("[00:30]"), 30)
        self.assertIsNone(notes.parse_stamp(None))
        self.assertIsNone(notes.parse_stamp("稍后"))

    def test_match_within_tolerance(self) -> None:
        """页面显示的是四舍五入后的时间：不放容差，每条都会被判无出处。"""
        self.assertEqual(notes.match_stamp(_SEGS, 63), 2)
        self.assertEqual(notes.match_stamp(_SEGS, 30), 1)     # 真实段首 30.4
        self.assertEqual(notes.match_stamp(_SEGS, 31), 1)     # 差 0.6 秒，算对上
        self.assertEqual(notes.match_stamp(_SEGS, 32), -1)    # 差 1.6 秒，出了容差

    def test_match_outside_tolerance(self) -> None:
        self.assertEqual(notes.match_stamp(_SEGS, 59), -1)    # 编的 59:59
        self.assertEqual(notes.match_stamp(_SEGS, 120), -1)
        self.assertEqual(notes.match_stamp(_SEGS, None), -1)
        self.assertEqual(notes.match_stamp([], 0), -1)

    def test_anchor_uses_real_segment_start(self) -> None:
        """写进文件的时间是真实段首，不是模型给的数——点进去要落在同一段上。"""
        segs = [{"start": 82.6, "end": 90.0, "text": "x"}]
        self.assertEqual(notes.match_stamp(segs, 83), 0)
        self.assertEqual(notes.clock(round(segs[0]["start"])), "01:23")

    def test_clock(self) -> None:
        self.assertEqual(notes.clock(0), "00:00")
        self.assertEqual(notes.clock(63), "01:03")
        self.assertEqual(notes.clock(3725), "1:02:05")


# --------------------------------------------------------------------------- #
# 写文件
# --------------------------------------------------------------------------- #

class _TempNotes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asrpolish_notes_")
        self.root = Path(self.tmp) / "notes"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class VideoNoteTest(_TempNotes):
    """一篇视频笔记长什么样。结构是死的——笔记页照着它认。"""

    def test_frontmatter_has_what_the_layer_needs(self) -> None:
        meta, body = notes.split_front(notes.render_video_note(
            _meta(), [{"text": "讲了限流", "sec": 0}],
            [_c("阈值", "触发判定的界线", "00:30")], "127.0.0.1:8040"))
        for key in ("标题", "BV号", "分P", "日期", "来源任务", "生成时间"):
            self.assertIn(key, meta, "缺少 %s" % key)
        # 过期判断要的两样：指纹、分P文件
        self.assertEqual(meta["指纹"], notes.part_sig({"segments": _SEGS}))
        self.assertEqual(meta["分P文件"], "BV1TEST00001-P1-带时间的一篇.srt")
        self.assertIn("## 要点", body)
        self.assertIn("## 概念", body)

    def test_colons_and_pipes_survive_the_frontmatter(self) -> None:
        """标题里带冒号、竖线是常事：YAML 得原样读回来。"""
        text = notes.render_video_note(
            _meta(title='A: B | C #1', part_name="a b:c.srt"), [], [], "")
        meta, _ = notes.split_front(text)
        self.assertEqual(meta["标题"], "A: B | C #1")
        self.assertEqual(meta["分P文件"], "a b:c.srt")

    def test_anchors_are_clickable_and_ok_to_miss(self) -> None:
        text = notes.render_video_note(
            _meta(), [{"text": "讲了限流", "sec": 0}, {"text": "没有落点"}],
            [_c("限流", "单位时间放固定数量", "59:59")], "127.0.0.1:8040")
        body = notes.split_front(text)[1]
        self.assertIn("[00:00](http://127.0.0.1:8040/reader?task=T569ceb995d"
                      "&part=0&t=0)", body)
        # 对不上的那条：只留文字，一个时间字符都不写
        self.assertIn("- 没有落点", body)
        self.assertIn("- [[限流]] — 单位时间放固定数量", body)
        self.assertNotIn("59:59", text)

    def test_no_time_part_says_so_at_the_top(self) -> None:
        """必剪那一篇：笔记照写，但顶上要说清「概念无法定位到视频」。"""
        text = notes.render_video_note(
            _meta(label="P2", has_time=False),
            [{"text": "只按行拆开", "sec": None}],
            [_c("阈值", "触发判定的界线")], "h")
        body = notes.split_front(text)[1]
        self.assertIn("这一篇没有逐句时间戳", body)
        self.assertNotIn("](http", body)          # 一个锚点都没有
        self.assertIn("- [[阈值]]", body)

    def test_file_name_carries_the_part(self) -> None:
        """一个合集里每一篇都得落到不同文件上，否则后写的盖掉先写的。"""
        a = notes.video_path(self.root, "带时间的一篇", "P1")
        b = notes.video_path(self.root, "带时间的一篇", "P2")
        self.assertNotEqual(a.name, b.name)
        self.assertTrue(a.name.startswith("带时间的一篇"))

    def test_illegal_characters_are_replaced(self) -> None:
        p = notes.video_path(self.root, 'a/b:c*d?"e"', "")
        self.assertNotIn("/", p.name)
        self.assertNotIn(":", p.name)


class SaveTest(_TempNotes):
    """写入的整条路：视频笔记 + 概念笔记 + INDEX。"""

    def save(self, meta, points, concepts) -> dict:
        return notes.save_video_note(self.root, meta, points, concepts, "h")

    def test_first_save_creates_three_kinds_of_files(self) -> None:
        got = self.save(_meta(), [{"text": "讲了限流", "sec": 0}],
                        [_c("阈值", "触发判定的界线", "00:30")])
        self.assertEqual(got["file"], "带时间的一篇 · P1.md")
        self.assertEqual(got["added"], ["阈值"])
        self.assertTrue((self.root / got["file"]).is_file())
        self.assertTrue((self.root / "concepts" / "阈值.md").is_file())
        self.assertTrue((self.root / "INDEX.md").is_file())

    def test_second_video_appends_one_line_not_a_new_file(self) -> None:
        """同一个概念在第二个视频里又出现：追加一行出处，不新建文件。"""
        self.save(_meta(), [], [_c("阈值", "触发判定的界线", "00:30")])
        got = self.save(_meta(title="第二篇", label="P2", part_index=1,
                              part_name="BV1TEST00001-P2-第二篇.srt"),
                        [], [_c("阈值", "换了个人讲的定义", "00:00")])
        self.assertEqual(got["added"], [])
        self.assertEqual(got["reused"], ["阈值"])
        self.assertEqual(len(list((self.root / "concepts").glob("*.md"))), 1)
        text = self.read("concepts/阈值.md")
        self.assertEqual(text.count("- ["), 2)                 # 两行出处
        self.assertIn("带时间的一篇 · P1", text)
        self.assertIn("第二篇 · P2", text)
        # 定义留在第一篇那份：概念笔记写的是「这是什么」，不是「最后一篇怎么说」
        self.assertIn("触发判定的界线", text)
        self.assertNotIn("换了个人讲的定义", text)

    def test_resaving_the_same_part_does_not_pile_up_lines(self) -> None:
        """同一篇再存一次（模型这次给的时间落在别的段上）：出处还是一条。"""
        self.save(_meta(), [], [_c("阈值", "定义", "00:00")])
        self.save(_meta(), [], [_c("阈值", "定义", "00:30")])
        text = self.read("concepts/阈值.md")
        self.assertEqual(text.count("- ["), 1)

    def test_occurrence_without_anchor_survives_a_rewrite(self) -> None:
        """没有锚点的出处（必剪那一篇）也必须在重写之后还在。

        以前这里要求出处行必须带 `t=`，于是「必剪转写的一篇 · P2」这种行读不
        回来，下次保存整篇重写时就被抹掉——库里的出处会随着保存次数变少。
        """
        self.save(_meta(label="P2", part_index=1, has_time=False,
                        part_name="BV1TEST00001-P2-必剪.srt"), [],
                  [_c("阈值", "定义")])
        self.save(_meta(), [], [_c("阈值", "定义", "00:30")])   # 触发重读重写
        text = self.read("concepts/阈值.md")
        # 必剪那一行的标题是 meta 里的标题，分 P 是 P2——认它靠的是这两样
        self.assertIn("· P2]", text)
        self.assertIn("· P1]", text)
        self.assertEqual(text.count("- ["), 2)

    def test_occurrence_gets_an_anchor_when_one_shows_up_later(self) -> None:
        """第一次没对上、第二次对上了：换成能点的那条，不是并排两条。"""
        self.save(_meta(has_time=False), [], [_c("阈值", "定义")])
        self.save(_meta(), [], [_c("阈值", "定义", "00:30")])
        occ = notes.parse_concept_note(self.read("concepts/阈值.md"))["occ"]
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0]["sec"], 30)

    def test_duplicate_concept_in_one_answer_is_written_once(self) -> None:
        got = self.save(_meta(), [], [_c("阈值", "定义", "00:00"),
                                      _c("  阈值 ", "换个空格", "00:30")])
        self.assertEqual(got["added"], ["阈值"])
        body = notes.split_front(self.read(got["file"]))[1]
        self.assertEqual(body.count("[[阈值]]"), 1)

    def test_concept_names_that_collide_on_disk_do_not_overwrite(self) -> None:
        """去掉非法字符后重名的两个概念：第二个换个文件名，不许覆盖。"""
        self.save(_meta(), [], [_c("A/B", "斜杠版本", "00:30")])
        got = self.save(_meta(), [], [_c("A:B", "冒号版本", "00:30")])
        self.assertEqual(got["added"], ["A:B"])
        files = sorted(p.name for p in (self.root / "concepts").glob("*.md"))
        self.assertEqual(len(files), 2)

    def test_index_is_a_directory_and_nothing_else(self) -> None:
        """INDEX 只做目录：视频在前、概念在后，概念只写名字与次数，不写全文。"""
        self.save(_meta(), [], [_c("阈值", "触发判定的那条界线", "00:30")])
        self.save(_meta(title="第二篇", label="P2", part_index=1,
                        part_name="BV1TEST00001-P2-第二篇.srt"),
                  [], [_c("阈值", "定义", "00:00")])
        idx = self.read("INDEX.md")
        self.assertLess(idx.index("## 视频笔记"), idx.index("## 概念笔记"))
        self.assertIn("[[阈值]] · 出现 2 次 · 首次 2026-11-07", idx)
        self.assertIn("第二篇", idx)
        self.assertNotIn("触发判定的那条界线", idx)     # 定义不进目录

    def test_index_is_rebuilt_from_disk(self) -> None:
        """用户手删一个文件之后，INDEX 不该还指着它（重建而不是增量维护）。"""
        self.save(_meta(), [], [_c("阈值", "定义", "00:30")])
        (self.root / "concepts" / "阈值.md").unlink()
        notes.rebuild_index(self.root, "h")
        self.assertNotIn("[[阈值]]", self.read("INDEX.md"))


class ConceptNoteTest(_TempNotes):
    """概念笔记的读写闭环。"""

    def test_round_trip(self) -> None:
        text = notes.render_concept_note(
            "阈值", "2026-11-01", "触发判定的界线",
            [{"title": "第一篇", "label": "P1", "task_id": "T1",
              "part_index": 0, "sec": 30}], "h")
        got = notes.parse_concept_note(text)
        self.assertEqual(got["name"], "阈值")
        self.assertEqual(got["first"], "2026-11-01")
        self.assertEqual(got["define"], "触发判定的界线")
        self.assertEqual(got["occ"], [{"title": "第一篇", "label": "P1",
                                       "task_id": "T1", "part_index": 0,
                                       "sec": 30}])

    def test_line_without_anchor_round_trips_too(self) -> None:
        text = notes.render_concept_note(
            "阈值", "2026-11-01", "定义",
            [{"title": "必剪篇", "label": "P2", "task_id": "T2",
              "part_index": 1, "sec": None}], "h")
        got = notes.parse_concept_note(text)
        self.assertEqual(len(got["occ"]), 1)
        self.assertIsNone(got["occ"][0]["sec"])
        self.assertEqual(got["occ"][0]["task_id"], "T2")

    def test_part_index_is_not_lost(self) -> None:
        """分P序号读错一位，锚点就会跳到另一篇的同一秒上。"""
        text = notes.render_concept_note(
            "阈值", "2026-11-01", "定义",
            [{"title": "第二篇", "label": "P2", "task_id": "T",
              "part_index": 3, "sec": 63}], "h")
        occ = notes.parse_concept_note(text)["occ"][0]
        self.assertEqual(occ["part_index"], 3)
        self.assertEqual(occ["sec"], 63)

    def test_hand_written_lines_are_skipped_not_fatal(self) -> None:
        """库是能被人手改的：认不出的行跳过，不许让整篇读不动。"""
        text = ("---\n概念: \"阈值\"\n首次出现: \"2026-11-01\"\n---\n\n# 阈值\n\n"
                "自己补的一句话定义\n\n## 出现过的地方\n\n"
                "- 手工记的一行，没有链接\n"
                "- [第一篇 · P1](http://h/reader?task=T1&part=0) · [00:30](http://h/r?t=30)\n")
        got = notes.parse_concept_note(text)
        self.assertEqual(got["define"], "自己补的一句话定义")
        self.assertEqual(len(got["occ"]), 1)

    def test_broken_file_does_not_kill_the_list(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "concepts").mkdir()
        (self.root / "concepts" / "好.md").write_text(
            notes.render_concept_note("好", "2026-11-07", "正常的", [], "h"),
            encoding="utf-8")
        (self.root / "concepts" / "坏.md").write_text("不是我们写的\n", encoding="utf-8")
        got = notes.load_concepts(self.root)
        self.assertEqual(sorted(c["name"] for c in got), ["坏", "好"])


# --------------------------------------------------------------------------- #
# 清单 / 搜索 / 过期
# --------------------------------------------------------------------------- #

class ListSearchTest(_TempNotes):
    def setUp(self) -> None:
        super().setUp()
        notes.save_video_note(
            self.root, _meta(), [{"text": "先看阈值怎么调", "sec": 30}],
            [_c("阈值", "触发判定的界线", "00:30"), _c("限流", "单位时间放固定数量", None)], "h")
        notes.save_video_note(
            self.root, _meta(title="必剪转写的一篇", label="P2", part_index=1,
                             has_time=False, date="2026-11-06",
                             part_name="BV1TEST00001-P2-必剪.srt"),
            [{"text": "只按行拆开", "sec": None}], [_c("阈值", "定义", None)], "h")

    def test_list_all(self) -> None:
        got = notes.list_all(self.root)
        self.assertEqual(len(got["videos"]), 2)
        self.assertEqual(len(got["concepts"]), 2)
        self.assertEqual(got["index"], "INDEX.md")
        self.assertEqual(got["videos"][0]["title"], "带时间的一篇")   # 日期倒序：11-07 在前
        self.assertEqual(got["videos"][1]["part_index"], 1)

    def test_search_by_title_body_and_concept(self) -> None:
        by_title = notes.search(self.root, "必剪")
        self.assertEqual([v["title"] for v in by_title["videos"]], ["必剪转写的一篇"])

        by_body = notes.search(self.root, "阈值怎么调")
        self.assertEqual([v["title"] for v in by_body["videos"]], ["带时间的一篇"])
        self.assertIn("正文", by_body["videos"][0]["hits"])

        by_concept = notes.search(self.root, "触发判定")
        self.assertEqual([c["name"] for c in by_concept["concepts"]], ["阈值"])
        self.assertIn("定义", by_concept["concepts"][0]["hits"])

    def test_search_is_keyword_only(self) -> None:
        """不做语义检索：搜「门限」不该把「阈值」捞出来。"""
        self.assertEqual(notes.search(self.root, "门限")["concepts"], [])

    def test_empty_query_lists_everything(self) -> None:
        got = notes.search(self.root, "")
        self.assertEqual(len(got["videos"]), 2)
        self.assertEqual(len(got["concepts"]), 2)

    def test_stale_when_the_transcript_changed(self) -> None:
        """字幕变了 → 标「来源已更新」。和总结缓存、问答记录用同一个规则。"""
        def reader(task_id):
            return {"BV1TEST00001-P1-带时间的一篇.srt": {"segments": _SEGS},
                    "BV1TEST00001-P2-必剪.srt": {"segments": [{"start": None}]}}

        got = notes.list_all(self.root, reader)
        by_title = {v["title"]: v for v in got["videos"]}
        self.assertFalse(by_title["带时间的一篇"]["stale"])   # 指纹对得上
        self.assertTrue(by_title["必剪转写的一篇"]["stale"])   # 3 段 → 1 段
        self.assertIn("字幕变过", by_title["必剪转写的一篇"]["stale_why"])

    def test_stale_when_the_part_is_gone(self) -> None:
        got = notes.list_all(self.root, lambda task_id: {})
        self.assertTrue(all(v["stale"] for v in got["videos"]))
        self.assertIn("找不到这一篇", got["videos"][0]["stale_why"])

    def test_not_stale_without_a_reader(self) -> None:
        """单测里不传回调就不判断过期，别把它当成「都过期了」。"""
        self.assertFalse(any(v["stale"] for v in notes.list_all(self.root)["videos"]))

    def test_foreign_markdown_is_not_a_video_note(self) -> None:
        """用户自己放进 notes/ 的 md 不当成视频笔记——它没有我们的 frontmatter。"""
        (self.root / "随手记.md").write_text("# 随手记\n\n今天学了点东西\n",
                                             encoding="utf-8")
        titles = [v["title"] for v in notes.list_all(self.root)["videos"]]
        self.assertNotIn("随手记", titles)


# --------------------------------------------------------------------------- #
# 真的走一遍模型
# --------------------------------------------------------------------------- #

class CallTest(unittest.TestCase):
    def test_call_sends_the_known_names_and_parses_the_answer(self) -> None:
        answer = json.dumps({"points": [{"text": "讲了限流", "t": "00:00"}],
                             "concepts": [_c("阈值", "触发判定的界线", "00:30")]},
                            ensure_ascii=False)
        srv, rec = _serve(_reply(answer))
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m", "key": _KEY}
            got = notes.call(cfg, _BODY, known=["限流"], has_time=True)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(got["model"], "m")
        self.assertEqual(got["notes"]["concepts"][0]["term"], "阈值")
        system = rec.body["messages"][0]["content"]
        self.assertIn("【这个库已经有的概念名】", system)
        self.assertIn("限流", system)
        user = rec.body["messages"][1]["content"]
        self.assertIn("[00:00] 大家好", user)      # 正文原样发过去

    def test_provider_error_is_passed_through(self) -> None:
        """提供方说的原因要原样带出来，不能糊成「生成失败」。"""
        srv, _ = _serve(json.dumps({"error": {"message": "余额不足"}}), 402)
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m", "key": _KEY}
            with self.assertRaises(SummaryError) as cm:
                notes.call(cfg, _BODY)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn("余额不足", cm.exception.message)

    def test_key_never_leaks_into_the_error(self) -> None:
        srv, _ = _serve(json.dumps({"error": {"message": "no"}}), 401)
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m", "key": _KEY}
            with self.assertRaises(SummaryError) as cm:
                notes.call(cfg, _BODY)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertNotIn(_KEY, cm.exception.message)

    def test_no_key(self) -> None:
        with self.assertRaises(SummaryError) as cm:
            notes.call({"base": "http://127.0.0.1:1/v1", "model": "m"}, _BODY)
        self.assertEqual(cm.exception.kind, "no_key")

    def test_empty_body(self) -> None:
        with self.assertRaises(SummaryError) as cm:
            notes.call({"base": "http://127.0.0.1:1/v1", "model": "m",
                        "key": _KEY}, "   ")
        self.assertEqual(cm.exception.kind, "empty")


# --------------------------------------------------------------------------- #
# HTTP：三个新接口 + 那一页
# --------------------------------------------------------------------------- #

class NotesHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="asrpolish_notes_http_")
        cls.root = Path(cls.tmp) / "notes"
        cls.store = Store(os.path.join(cls.tmp, "t.db"))
        cls.app = App(cls.store, None, notes_dir=str(cls.root))
        cls.srv = make_server("127.0.0.1", 0, cls.app)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever,
                         kwargs={"poll_interval": 0.1}, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.model_srv, self.rec = _serve(_reply(json.dumps({
            "points": [{"text": "讲了限流", "t": "00:00"}],
            "concepts": [_c("阈值", "触发判定的界线", "00:00")]},
            ensure_ascii=False)))

    def tearDown(self) -> None:
        self.model_srv.shutdown()
        self.model_srv.server_close()

    def cfg(self) -> dict:
        return {"base": "http://127.0.0.1:%d/v1" % self.model_srv.server_address[1],
                "model": "m", "key": _KEY}

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 错误响应也是一个打开着的 HTTPResponse：不关它，Python 3.13+ 会在
            # 回收时打一条 ResourceWarning，把真正的失败淹掉。
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()

    def post(self, path: str, obj) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 错误响应也是一个打开着的 HTTPResponse：不关它，Python 3.13+ 会在
            # 回收时打一条 ResourceWarning，把真正的失败淹掉。
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()

    def _task(self, files: list[str]) -> str:
        t = self.store.create("subs", {"target": "BV1TEST00001"})
        self.store.claim("w1")
        self.store.finish(t.id, {"outdir": self.tmp, "files": files,
                                 "outcome": "complete"})
        return t.id

    def _write_srt(self, name: str, body: str) -> str:
        p = Path(self.tmp) / name
        p.write_text(body, encoding="utf-8")
        return str(p)

    def test_notes_page_is_served(self) -> None:
        with urllib.request.urlopen(self.base + "/notes") as r:
            html = r.read().decode("utf-8")
        self.assertIn("笔记库", html)
        self.assertIn("window.__notes", html)
        self.assertIn("/api/notes", html)

    def test_notes_and_search_endpoints(self) -> None:
        code, data = self.get("/api/notes")
        self.assertEqual(code, 200)
        self.assertIn("videos", data)
        code, data = self.get("/api/notes/search?q=x")
        self.assertEqual(code, 200)
        self.assertEqual(data["q"], "x")

    def test_save_one_part(self) -> None:
        srt = self._write_srt(
            "BV1TEST00001-P1-带时间的一篇.srt",
            "1\n00:00:00,000 --> 00:00:04,000\n大家好，这一期聊限流\n\n"
            "2\n00:00:30,400 --> 00:00:34,000\n先看阈值怎么调\n")
        tid = self._task([srt])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": self.cfg(), "text": _BODY,
                                "part": "BV1TEST00001-P1-带时间的一篇.srt"})
        self.assertEqual(code, 200, data)
        # 只有一篇的任务不带分 P 标记：P1 这种写法是给合集的（否则每篇都要多四个字）
        self.assertEqual(data["file"], "带时间的一篇.md")
        self.assertEqual(data["added"], ["阈值"])
        self.assertTrue(data["hasTime"])
        self.assertEqual(data["partName"], "BV1TEST00001-P1-带时间的一篇.srt")
        self.assertTrue((self.root / data["file"]).is_file())

        code, data = self.get("/api/notes")
        self.assertEqual([v["title"] for v in data["videos"]], ["带时间的一篇"])
        self.assertFalse(data["videos"][0]["stale"])   # 指纹刚算过，对得上

    def test_ambiguous_part_is_rejected(self) -> None:
        a = self._write_srt("BV1TEST00001-P1-甲.srt", "1\n00:00:00,000 --> 00:00:01,000\n甲\n")
        b = self._write_srt("BV1TEST00001-P2-乙.srt", "1\n00:00:00,000 --> 00:00:01,000\n乙\n")
        tid = self._task([a, b])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": self.cfg(), "text": _BODY})
        self.assertEqual(code, 400)
        self.assertIn("得说清存哪一篇", data["error"])

    def test_unknown_part_is_rejected(self) -> None:
        a = self._write_srt("BV1TEST00001-P1-甲.srt", "1\n00:00:00,000 --> 00:00:01,000\n甲\n")
        tid = self._task([a])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": self.cfg(), "text": _BODY, "part": "没有这一篇"})
        self.assertEqual(code, 404)
        self.assertIn("没有这一篇", data["error"])

    def test_unknown_task(self) -> None:
        code, data = self.post("/api/tasks/Tnope/notes",
                               {"model": self.cfg(), "text": _BODY})
        self.assertEqual(code, 404)

    def test_unreadable_transcript(self) -> None:
        """产物被删了：说清是读不出正文，不是「生成失败」。"""
        tid = self._task([str(Path(self.tmp) / "gone.srt")])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": self.cfg(), "text": _BODY})
        self.assertEqual(code, 409)
        self.assertIn("读不出正文", data["error"])

    def test_no_key_reports_kind(self) -> None:
        a = self._write_srt("BV1TEST00001-P1-甲.srt", "1\n00:00:00,000 --> 00:00:01,000\n甲\n")
        tid = self._task([a])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": {"base": "http://127.0.0.1:1/v1",
                                          "model": "m"},
                                "text": _BODY,
                                "part": "BV1TEST00001-P1-甲.srt"})
        self.assertEqual(code, 400)
        self.assertEqual(data["kind"], "no_key")
        self.assertIn("API key", data["error"])

    def test_rate_limit_reason_is_passed_through(self) -> None:
        """限流时提供方说的话要原样出来：和「模型乱答」的处理方式不一样。"""
        self.model_srv.shutdown()
        self.model_srv.server_close()
        self.model_srv, self.rec = _serve(
            json.dumps({"error": {"message": "Rate limit reached"}}), 429)
        a = self._write_srt("BV1TEST00001-P1-丙.srt", "1\n00:00:00,000 --> 00:00:01,000\n丙\n")
        tid = self._task([a])
        code, data = self.post("/api/tasks/%s/notes" % tid,
                               {"model": self.cfg(), "text": _BODY,
                                "part": "BV1TEST00001-P1-丙.srt"})
        self.assertEqual(code, 429)
        self.assertEqual(data["kind"], "rate")
        self.assertIn("Rate limit", data["error"])


# --------------------------------------------------------------------------- #
# 页面纯函数：用 node 真跑一遍
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_HARNESS = _HERE / "js" / "notes_logic.js"
_NODE = shutil.which("node")


def _page_script() -> str:
    """笔记页的那段脚本。用 `window.__notes` 认人，不按位置取。"""
    for block in re.findall(r"<script>(.*?)</script>", notes_page.html(), re.S):
        if "window.__notes" in block:
            return block
    raise AssertionError("笔记页里没有挂 window.__notes 的脚本")


@unittest.skipUnless(_NODE, "没有 node，跳过前端逻辑测试")
class NotesPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_page_script())
            cls.script = fh.name
        out = subprocess.run([_NODE, str(_HARNESS), cls.script],
                             capture_output=True, encoding="utf-8",
                             errors="replace", timeout=60)
        if out.returncode != 0 or not out.stdout:
            raise AssertionError("node 跑笔记页脚本失败（退出码 %s）：%s\n%s"
                                 % (out.returncode, (out.stdout or "")[-500:],
                                    (out.stderr or "")[-2000:]))
        cls.d = json.loads(out.stdout)

    @classmethod
    def tearDownClass(cls) -> None:
        Path(cls.script).unlink(missing_ok=True)

    def test_wikilink_is_replaced_before_normal_link(self) -> None:
        """`[[阈值]] — 定义 [00:00](url)` 里的两个链接都得认对。

        顺序反了的话，普通链接那条正则会把第一个方括号一路吃到 `](`，
        链接文字变成「[阈值]] — 触发判定的那条界线 [00:00」。
        """
        got = self.d["inlineMixed"]
        self.assertIn('href="/notes?q=%E9%98%88%E5%80%BC">阈值</a>', got)
        self.assertIn('<a class="t" href="http://h/r?t=0"', got)
        self.assertIn("— 触发判定的那条界线 ", got)
        self.assertNotIn("[[", got)

    def test_text_is_escaped(self) -> None:
        got = self.d["inlineEsc"]
        self.assertNotIn("<script>", got)
        self.assertIn("&lt;script&gt;", got)
        self.assertIn("&amp;", got)

    def test_body_renders_only_the_fixed_structures(self) -> None:
        body = self.d["body"]
        self.assertIn('<h3>要点</h3>', body)
        self.assertIn('<h3>概念</h3>', body)
        self.assertEqual(body.count("<ul class=\"items\">"), 2)
        self.assertEqual(body.count("</ul>"), 2)
        # 大标题不重复画一次：卡片顶上已经有了
        self.assertNotIn("<h1>", body)
        self.assertNotIn("<h2>", body)
        # 正文里的尖括号照样得转义
        self.assertNotIn("<b>", body)

    def test_notime_note_is_kept_as_a_notice(self) -> None:
        """必剪那篇顶上那句「无法定位到视频」要留在卡片里，不能被当成正文吞掉。"""
        got = self.d["bodyNotime"]
        self.assertIn('<p class="note">', got)
        self.assertIn("没有逐句时间戳", got)

    def test_empty_bodies(self) -> None:
        self.assertEqual(self.d["bodyEmpty"], "")

    def test_video_card(self) -> None:
        got = self.d["videosHtml"].split('<div class="card">')[1]
        self.assertIn('href="/reader?task=T569ceb995d&amp;part=0"', got)
        self.assertIn("BV1TEST00001 · P1 · 2026-11-07", got)
        self.assertIn("00:30", got)          # 锚点文字是时钟
        self.assertNotIn('class="stale"', got)

    def test_stale_badge_carries_the_reason(self) -> None:
        got = self.d["videoStale"]
        self.assertIn("来源已更新，建议重新生成", got)
        self.assertIn('title="字幕变了（5 段 → 6 段）"', got)

    def test_empty_states_say_what_to_do(self) -> None:
        """空库和「搜不到」是两件事：说的话不一样。"""
        self.assertIn("还没有视频笔记", self.d["videosEmpty"])
        self.assertIn("存入笔记库", self.d["videosEmpty"])
        self.assertIn("没有命中", self.d["videosEmptySearch"])
        self.assertIn("还没有概念笔记", self.d["conceptsEmpty"])

    def test_concept_card(self) -> None:
        got = self.d["conceptsHtml"]
        self.assertIn('href="/notes?q=%E9%98%88%E5%80%BC">阈值</a>', got)
        self.assertIn("出现 2 次 · 首次 2026-11-07", got)
        self.assertIn('<div class="def">触发判定的那条界线。</div>', got)
        self.assertIn("命中：概念名", got)
        self.assertIn("必剪转写的一篇 · P2", got)   # 没有锚点的那条也要列出来

    def test_concept_without_definition_still_renders(self) -> None:
        got = self.d["conceptNoDef"]
        self.assertIn("空的", got)
        self.assertNotIn('<div class="def">', got)

    def test_hrefs(self) -> None:
        self.assertEqual(self.d["hrefFull"], "/reader?task=T%201&part=1&t=63")
        self.assertEqual(self.d["hrefNoSec"], "/reader?task=T&part=0")
        self.assertEqual(self.d["hrefNoPart"], "/reader?task=T")
        self.assertEqual(self.d["hrefEmpty"], "")
        self.assertEqual(self.d["noteHref"], "/reader?task=T&part=2")

    def test_clocks_match_the_reader(self) -> None:
        """和笔记文件里、阅读页上长得一样：三处不能有两种写法。"""
        self.assertEqual(self.d["clocks"],
                         ["00:00", "00:09", "01:03", "10:05", "1:02:05"])

    def test_meta_bits_skip_empties(self) -> None:
        self.assertEqual(self.d["metaBits"][0], "BV1TEST00001 · P1 · 2026-11-07")
        self.assertEqual(self.d["metaBits"][1], "")


if __name__ == "__main__":
    unittest.main()
