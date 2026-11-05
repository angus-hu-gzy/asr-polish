"""FOR_AI.md 的自检：这份文档写给 AI 照着做，所以它写的每一句都得是真的。

为什么值得单开一个测试文件——**这类文档最常见的死法是烂掉，不是写错**。
半年后某个子命令改名、某个参数被删，文档还写着老命令；用户把文档丢给 AI，
AI 会非常自信地照着执行、非常自信地宣布成功。人看不出问题，因为人根本不会逐字读它。

这里能离线验的就三条：
1. 文档里出现的每条 `asr-polish ...` 命令，真的能被当前的解析器接受（子命令、参数都在）；
2. `-f` 后面写的格式，真的在 `export.FORMATS` 里；
3. 提到的 MCP 工具名，真的在 `mcp._TOOLS` 里。

联网才能验的部分（真抓一条视频）不放在这里——CI 里没有 B站，也不该有。
"""
from __future__ import annotations

import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path

from asr_polish import cli, export, mcp

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "FOR_AI.md"
README = ROOT / "README.md"


def _bash_blocks(text: str) -> list:
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def _command_lines(text: str) -> list:
    """代码块里的可执行行（去掉注释行、行尾注释）。"""
    out = []
    for block in _bash_blocks(text):
        for raw in block.splitlines():
            line = raw.split("  #")[0].strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _as_asr_polish(line: str):
    """把一行命令收成 `asr-polish` 的参数列表；不是本项目的命令就返回 None。"""
    if line.startswith("asr-polish "):
        rest = line[len("asr-polish "):]
    elif line.startswith("python -m asr_polish "):
        rest = line[len("python -m asr_polish "):]
    else:
        return None
    rest = re.sub(r"<[^>]*>", "123", rest)      # <mid 或空间链接> → 123
    return shlex.split(rest)


class TestForAiDoc(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOC.read_text(encoding="utf-8")
        cls.lines = _command_lines(cls.text)

    def test_doc_exists_and_is_short_enough(self) -> None:
        """防膨胀闸门：这份文档的作用是「一页能读完并照着做」。

        行数上限不是模型注意力的问题——模型读得下几千行。是维护和复制的问题：
        一旦写成十屏长文，用户不会去读、改的人也不会通读，它就开始烂。
        真要写长，该拆成 docs/ 下的正文 + 这里留一页导航。
        """
        self.assertTrue(DOC.exists(), "FOR_AI.md 不见了")
        n = len(self.text.splitlines())
        self.assertLess(n, 280, "FOR_AI.md 已经 %d 行，该拆了" % n)

    def test_every_command_parses(self) -> None:
        """文档里每条命令都得被当前解析器接受。改名、删参数会在这里炸。"""
        parser = cli.build_parser()
        checked = 0
        for line in self.lines:
            argv = _as_asr_polish(line)
            if argv is None:
                continue
            checked += 1
            with self.subTest(cmd=line), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    parser.parse_args(argv)
                except SystemExit as exc:
                    # --version 会正常退出 0；未知参数/缺值退出 2
                    self.assertIn(exc.code, (0, None), "这条命令解析不过：%s" % line)
        self.assertGreaterEqual(checked, 5, "一条 asr-polish 命令都没扫到，解析写错了？")

    def test_formats_are_real(self) -> None:
        """`-f md,json` 里的每种格式都得真能导出。"""
        for line in self.lines:
            m = re.search(r"-f\s+(\S+)", line)
            if not m:
                continue
            for fmt in m.group(1).split(","):
                self.assertIn(fmt, export.FORMATS, "%s 里的格式 %s 不存在" % (line, fmt))

    def test_never_suggests_limit_zero(self) -> None:
        """`--limit 0` 是不限，不是「不抓」。文档里只能在警告句里出现，不能在命令里。"""
        for line in self.lines:
            self.assertNotIn("--limit 0", line,
                             "这行会一次抓走整个 UP 主：%s" % line)

    def test_mcp_tools_are_real(self) -> None:
        names = {t["name"] for t in mcp._TOOLS}
        for tool in ("fetch_subtitles", "list_videos", "correct_text", "check_login"):
            self.assertIn(tool, names)
            self.assertIn(tool, self.text, "文档里该提到 %s" % tool)

    def test_tells_the_ai_to_stop_when_it_cannot_act(self) -> None:
        """碰不到用户电脑的 AI 必须停下来。缺了这句，它会编一套假步骤。"""
        self.assertIn("碰不到用户的电脑", self.text)
        self.assertIn("停下来", self.text)

    def test_forbids_the_dangerous_habits(self) -> None:
        for word in ("curl", "不许删用户的文件", "cookie 的内容"):
            self.assertIn(word, self.text)
        # 只扫代码块：正文里那句「不许 curl ... | sh」是在禁止它，不该被当成教它
        bad = [line for line in self.lines if re.search(r"curl[^\n]*\|\s*(?:ba)?sh", line)]
        self.assertEqual(bad, [], "文档里的命令出现了 curl | sh：%s" % bad)

    def test_requires_evidence_before_claiming_done(self) -> None:
        """没有验收点，AI 会幻觉式收尾——这是这类文档最要紧的一节。"""
        self.assertIn("验收", self.text)
        self.assertIn("- [ ]", self.text)
        self.assertIn("asr-polish --version", self.text)

    def test_readme_has_the_copy_paste_entry(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn("FOR_AI.md", readme)
        self.assertIn("https://github.com/angus-hu-gzy/asr-polish", readme)
        # 入口得在开头，藏在文末等于没有
        first_40 = "\n".join(readme.splitlines()[:40])
        self.assertIn("FOR_AI.md", first_40, "入口没放在 README 开头")


if __name__ == "__main__":
    unittest.main()
