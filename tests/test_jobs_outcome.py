# -*- coding: utf-8 -*-
"""批量任务的结果语义：部分成功要看得出来，失败的那几条要能单独重跑。

这三个测试对应一条真实反馈：20 条里成了 18 条和 20 条全成，在面板上长得
一模一样；全失败时又只剩一句错误字符串，用户既看不出差在哪，也没法只补
那 2 条，只能整个任务重来（还会再吃一次风控）。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest import mock

from asr_polish import bili
from asr_polish.bili import FetchResult
from asr_polish.server import jobs


def _bvs(n: int) -> list[str]:
    return ["BV1xx411c%03d" % i for i in range(n)]


def _info(pages: int = 1) -> dict:
    return {"title": "标题", "cid": 1, "duration": 10,
            "pages": [{"cid": i + 1, "part": "P%d" % (i + 1), "duration": 5}
                      for i in range(pages)]}


def _res(bv: str, page: int = 1, segments: bool = True) -> FetchResult:
    return FetchResult(
        bvid=bv, title="标题", owner="", duration=10, source="字幕轨",
        text="x" if segments else "", page=page,
        segments=[{"start": 0, "end": 1, "text": "x"}] if segments else [])


class _RunJob(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asrjob_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, payload: dict, fetch_fn, targets=None, info=None):
        """跑一次 run_subs，网络与写盘全部替掉。"""
        info = info or _info()
        with mock.patch.object(jobs, "resolve_targets",
                               return_value=targets or []) as resolve, \
                mock.patch.object(jobs, "get_video_info",
                                  side_effect=lambda bv: info), \
                mock.patch.object(jobs, "fetch", side_effect=fetch_fn), \
                mock.patch.object(jobs.export, "write_all"):
            result = jobs.run_subs(payload, lambda _f, _m: None)
        return result, resolve


class TestParseOnly(unittest.TestCase):
    def test_whole_video_and_single_page(self) -> None:
        bv = "BV1xx411c7mD"
        self.assertEqual(bili.parse_only_targets([bv]), [(bv, None)])
        self.assertEqual(bili.parse_only_targets([bv + "#3"]), [(bv, 3)])

    def test_string_form_and_dedupe(self) -> None:
        bv = "BV1xx411c7mD"
        self.assertEqual(bili.parse_only_targets(bv + " " + bv),
                         [(bv, None)])
        self.assertEqual(bili.parse_only_targets(bv + "," + bv + "#2"),
                         [(bv, None), (bv, 2)])

    def test_absent(self) -> None:
        self.assertIsNone(bili.parse_only_targets(None))
        self.assertIsNone(bili.parse_only_targets([]))
        self.assertIsNone(bili.parse_only_targets(""))

    def test_rejects_garbage(self) -> None:
        for bad in (["not-a-bv"], ["BV1xx411c7mD#x"], ["BV1xx411c7mD#0"]):
            with self.assertRaises(ValueError):
                bili.parse_only_targets(bad)

    def test_group_pages(self) -> None:
        # 同一个视频既有「整条」又有具体分 P：按整条算
        self.assertEqual(bili.group_only_pages([("BV1", None), ("BV1", 2)]),
                         {"BV1": None})
        self.assertEqual(bili.group_only_pages([("BV1", 2), ("BV1", 3)]),
                         {"BV1": {2, 3}})


class TestOutcome(_RunJob):
    def test_all_ok_is_complete(self) -> None:
        bvs = _bvs(3)
        result, _ = self._run({"target": "up:1", "outdir": self.tmp},
                              lambda bv, page=1, allow_asr=False: _res(bv), bvs)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["failed_items"], [])

    def test_partial_lists_failed_entries(self) -> None:
        """20 条成 18 条：结论必须是 partial，而且要说清是哪 2 条。"""
        bvs = _bvs(20)
        bad = {bvs[3], bvs[11]}

        def fetch(bv, page=1, allow_asr=False):
            if bv in bad:
                raise bili.BiliError("字幕接口返回空")
            return _res(bv)

        result, _ = self._run({"target": "up:1", "outdir": self.tmp}, fetch, bvs)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["parts_written"], 18)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(sorted(x["retry"] for x in result["failed_items"]),
                         sorted(bad))
        self.assertTrue(all("字幕接口返回空" in x["error"]
                            for x in result["failed_items"]))
        items = {x["bvid"] for x in result["items"] if x["failed_pages"]}
        self.assertEqual(items, bad)

    def test_nosub_only_is_empty_not_success(self) -> None:
        """一条都没字幕不是「成功」——产出是零，得说成「没有产出」。"""
        bvs = _bvs(2)
        result, _ = self._run({"target": "up:1", "outdir": self.tmp},
                              lambda bv, page=1, allow_asr=False:
                              _res(bv, segments=False), bvs)
        self.assertEqual(result["outcome"], "empty")
        self.assertEqual(result["parts_written"], 0)
        self.assertEqual(result["no_subtitle"], 2)
        self.assertEqual(result["failed_items"], [])

    def test_some_nosub_is_partial(self) -> None:
        bvs = _bvs(2)

        def fetch(bv, page=1, allow_asr=False):
            return _res(bv, segments=(bv == bvs[0]))

        result, _ = self._run({"target": "up:1", "outdir": self.tmp}, fetch, bvs)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["no_subtitle"], 1)

    def test_zero_output_is_empty_even_with_errors(self) -> None:
        """一条都没产出时不能叫「部分成功」——18 条没字幕 + 2 条报错，产出是零。"""
        bvs = _bvs(3)

        def fetch(bv, page=1, allow_asr=False):
            if bv == bvs[0]:
                raise bili.BiliError("风控")
            return _res(bv, segments=False)

        result, _ = self._run({"target": "up:1", "outdir": self.tmp}, fetch, bvs)
        self.assertEqual(result["outcome"], "empty")
        self.assertEqual(result["parts_written"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["failed_items"]), 1)

    def test_nothing_to_do_is_empty(self) -> None:
        """指定了不存在的分 P：既不成功也没失败，别判成 failed。"""
        bv = _bvs(1)[0]
        result, _ = self._run(
            {"target": bv, "only": [bv + "#9"], "outdir": self.tmp},
            lambda b, page=1, allow_asr=False: _res(b, page), [bv],
            info=_info(pages=2))
        self.assertEqual(result["outcome"], "empty")
        self.assertEqual(result["failed"], 0)

    def test_all_fail_still_carries_details(self) -> None:
        """全失败仍是任务失败，但明细要跟着一起落库，不能只剩一句话。"""
        bvs = _bvs(2)

        def fetch(bv, page=1, allow_asr=False):
            raise bili.BiliError("风控")

        with self.assertRaises(jobs.JobFailure) as ctx:
            self._run({"target": "up:1", "outdir": self.tmp}, fetch, bvs)
        detail = ctx.exception.result
        self.assertIsNotNone(detail)
        self.assertEqual(detail["outcome"], "failed")
        self.assertEqual(len(detail["failed_items"]), 2)
        self.assertEqual(sorted(x["bvid"] for x in detail["failed_items"]),
                         sorted(bvs))
        self.assertIn("全部失败", str(ctx.exception))

    def test_page_failure_keeps_page_number(self) -> None:
        """多 P 里某一 P 挂了：重跑要给 BV#P，不能整条重来。"""
        bv = _bvs(1)[0]

        def fetch(bv_, page=1, allow_asr=False):
            if page == 2:
                raise bili.BiliError("P2 取不到")
            return _res(bv_, page)

        result, _ = self._run({"target": bv, "outdir": self.tmp}, fetch, [bv],
                              info=_info(pages=3))
        self.assertEqual(result["parts_written"], 2)
        self.assertEqual([x["retry"] for x in result["failed_items"]],
                         ["%s#2" % bv])
        self.assertEqual(result["failed_items"][0]["page"], 2)


class TestOnly(_RunJob):
    def test_only_skips_target_resolution(self) -> None:
        """重跑不能重新解析 target：UP 主投稿一变就跑错东西了。"""
        bvs = _bvs(3)
        payload = {"target": "up:999", "only": ["BV1xx411c001"],
                   "outdir": self.tmp}
        result, resolve = self._run(
            payload, lambda bv, page=1, allow_asr=False: _res(bv), bvs)
        resolve.assert_not_called()
        self.assertEqual(result["videos"], 1)
        self.assertEqual(result["parts_written"], 1)
        self.assertEqual(result["only"], ["BV1xx411c001"])

    def test_only_limits_to_failed_page(self) -> None:
        calls: list = []

        def fetch(bv, page=1, allow_asr=False):
            calls.append((bv, page))
            return _res(bv, page)

        bv = "BV1xx411c007"
        self._run({"target": "up:1", "only": [bv + "#2"], "outdir": self.tmp},
                  fetch, [bv], info=_info(pages=3))
        self.assertEqual(calls, [(bv, 2)])

    def test_only_ignores_limit(self) -> None:
        """重跑清单本来就只有几条，不能被原任务的 --limit 截断。"""
        bvs = _bvs(5)
        result, _ = self._run(
            {"target": "up:1", "limit": 2, "only": bvs, "outdir": self.tmp},
            lambda bv, page=1, allow_asr=False: _res(bv), bvs)
        self.assertEqual(result["videos"], 5)


if __name__ == "__main__":
    unittest.main()
