"""README 里关于 Cookie 的说法，不能和代码各说各的。

README 承诺了「放到这三个位置就会自动加载」，代码里是 `_DEFAULT_COOKIE_PATHS`。
两边一旦不一致，用户把文件放在文档写的位置、工具却找不到，报错是「没有找到
Cookie 文件」——看起来像他放错了，其实是文档没跟上代码。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
sys.path.insert(0, str(ROOT))

from asr_polish import bili  # noqa: E402


class ReadmeCookieTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding="utf-8")

    def test_default_paths_match_code(self) -> None:
        """README 列的默认路径集合，必须与 _DEFAULT_COOKIE_PATHS 完全相等。"""
        home = Path.home().as_posix()
        want = {p.as_posix().replace(home, "~") for p in bili._DEFAULT_COOKIE_PATHS}
        listed = {ln.strip() for ln in self.text.splitlines()
                  if ln.strip().startswith("~") and "cookies.txt" in ln}
        self.assertEqual(listed, want,
                         "README 与 _DEFAULT_COOKIE_PATHS 不一致：只在文档 %s，"
                         "只在代码 %s" % (sorted(listed - want), sorted(want - listed)))

    def test_answers_the_three_questions_about_the_account(self) -> None:
        """用谁的账号 / 存在哪 / 发给谁——少答一个，就会有人来问。"""
        for phrase in ("用你自己的", "只在你机器上", "只发给 B站"):
            self.assertIn(phrase, self.text, "README 少了「%s」这一问" % phrase)

    def test_warns_against_sending_the_cookie_file_around(self) -> None:
        """这句最容易被「精简一下」删掉，但它救的是账号。"""
        self.assertIn("别把 `cookies.txt` 发给任何人", self.text)
        self.assertIn("谁拿到谁就是你", self.text)

    def test_says_others_use_their_own_account(self) -> None:
        """别人 clone 下来用的是他自己的 cookie——这点不写清，会被当成共用一个号。"""
        self.assertIn("别人用这个项目，用的是他自己的 cookie", self.text)


if __name__ == "__main__":
    unittest.main()
