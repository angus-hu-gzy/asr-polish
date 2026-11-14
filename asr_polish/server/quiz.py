# -*- coding: utf-8 -*-
"""单篇小测验：把这一篇出成几道单选题，每题带出处和一句解析。

为什么单独一个文件
------------------
和第 1、2 层同一个理由：拼端点、带 key、翻译提供方的错误已经在
`summary.complete` 里了，这里只多一样东西——**输出的形状不一样**。总结要
`tldr/points`，问答要散文，测验要选择题。所以这里只有提示词和解析，
一次调用仍然走 `complete`，不复制第二份发请求的代码。

为什么出题比总结更不能编
------------------------
总结编了，读者顶多读到一句不准的话；题目编了，读者会被自己的「答对」骗过去——
他以为学会了，其实那一页根本没讲。所以这层的规矩更硬：题干只能考原文讲过的，
解析要能指回原文那一行，出处时间再由前端拿真实段首核一遍（1.5 秒容差），
对不上的标「无出处」。

关于 answer 为什么按字母收
--------------------------
让模型给 `"answer": 2` 最省事，但它到底从 0 数还是从 1 数，谁也说不准：给 2
可能是 C 也可能是 B，而错的那一半**表面上完全正常**——用户被自己的「答错」骗了，
比少一道题糟得多。所以提示词要字母（`"B"`），解析时依次认三种写法：
字母 → 选项原文（一字不差）→ 0 起的下标。三种都不通的题直接丢掉。

关于干扰项
----------
「下面哪个是错的」配三个一眼假的选项，答对了也说明不了什么。所以提示词里点明
干扰项要错得像真的：原文提过但问的不是它、数字或顺序被改、两个概念换了位置。
这一条核不了，只能要求；能核的只有时间。
"""
from __future__ import annotations

import re

from .summary import (BODY_HEAD, BODY_TAIL, DEFAULT_TIMEOUT, SummaryError,
                      complete, json_object, parse_stamp, require_len)

#: 一次出几道。五道是一屏能读完、又不至于「蒙也蒙对一半」的量。
DEFAULT_COUNT = 5

#: 每题几个选项。四选一：三个干扰项刚好能错得像真的，再多就是凑数。
CHOICES = 4

#: 一次最多要几道。再多模型会开始互相重复，界面也读不完。
MAX_COUNT = 10

#: 提示词。和第 1 层一样做成常量、不给用户改：输出形状被前端解析，格式一变
#: 整块就废。`%(count)d` 出现两次，用 % 而不是 str.format——正文里那个 JSON
#: 示例本身全是花括号，format 会把它们当占位符。
#:
#: 五条要求各自对应一件真事：
#: 1. **只能考原文讲过的**——不然它会拿训练数据里的常识出题，答对了也没有意义；
#: 2. **带出处时间**——有了时间才能点回原文，也才能被前端核对；
#: 3. **解析里也带时间**——解析是答完之后唯一能验证「为什么」的东西；
#: 4. **干扰项要合理**——一眼能排除的题等于没出；
#: 5. **答案位置打乱**——不然做三道就知道全选 B。
QUIZ_PROMPT = """你是出题老师。下面是一篇视频文字稿，每行开头的 [分:秒] 是这一行对应的
时间。

请就这段文字稿出 %(count)d 道单选题，**只输出一个 JSON 对象**，不要解释、不要 markdown
围栏：

{
  "questions": [
    {"stem": "题干",
     "options": ["选项一的文字", "选项二的文字", "选项三的文字", "选项四的文字"],
     "answer": "B", "why": "原文说……，所以选它 [01:23]。", "t": "01:23"}
  ]
}

要求，五条，每条都要做到：

1. 每道题只能考这段文字稿里**确实讲过**的事：事实、做法、顺序、数字、因果、结论。
   稿子里没有的内容不许出题、不许编，也不许用你自己的知识补。每道题都要能在原文里找到依据。
2. 每道题带一个 t：原样抄它所依据的那一行行首的时间，写成 "01:23" 这样的字符串
   （不带方括号）。不许自己换算成秒数，不许写原文里没有的时间。
3. 每道题带一句 why：说清为什么选它，**句子里要带上同一个 [分:秒]**，让读者能回去看。
   一句话说透，不要客套。
4. 三个干扰项要合理：不能一眼就能排除（完全无关的名词、明显荒唐的说法），也不能
   含糊到几个选项都成立。它们应该是「原文提过别的、但问的不是它」「把数字或顺序
   改错」「把两个概念换了位置」这一类——错得像只有真读懂的人才会避开。
5. 正确答案在 A/B/C/D 上的位置要打乱：%(count)d 道题里四个位置都要出现，不要都放在
   同一个字母上。

options 恰好 4 个，每个都是一句完整的话，长度相近；answer 只写一个大写字母 A/B/C/D。
stem 要问得具体（谁、做了什么、为什么、多少），不要写「以下哪项正确」这种没有指向的问法。
用中文出题。"""

#: 这一篇没有逐句时间（必剪 ASR / 纯文本导出）时追加的一句。不追加的话，模型看见
#: 模板里写着「每行开头有 [分:秒]」却发现没有，很可能自己编一个出来——它编的会被
#: 前端判成无出处，但用户看到满屏「无出处」只会以为功能坏了。
NO_TIME_RULE = """

注意：这一篇的文字稿没有逐句时间戳，行首没有 [分:秒]。这种情况下每道题的 t 一律写
null，why 里也不要写任何时间，不要编造时间。"""


def _pick(obj: dict, *keys):
    """按顺序取第一个**存在且非空**的字段。

    不能写成 `obj.get("answer") or obj.get("correct")`：正确答案的下标 0 是假值，
    会被 `or` 跳过去，于是拿到另一个字段或者空——一道题的答案就这么变了。
    """
    for k in keys:
        if k in obj:
            v = obj[k]
            if v is not None and v != "":
                return v
    return None


def _text_of(v) -> str:
    return v.strip() if isinstance(v, str) else ""


#: 选项自带的标号：`A. 文字` / `B、文字` / `C．文字` / `D 文字` 四种写法都认。
#: 字母后面必须跟标点或者空白——「A股大涨」没有分隔，那是内容，不是标号。
#: 认到 E 是给削枝那一路用的：模型给五个选项时会写 A–E，`_strip_labels_after_shrink`
#: 要先认得出第 5 个字母，才谈得上把它连同削掉的那项一起去干净。
_OPT_LABEL = re.compile(r"^([A-Ea-e])(?:\s*[.、．)）:：]\s*|\s+)")


def _strip_labels(out: list) -> list:
    """去掉选项自带的 A/B/C/D 标号。**四条都在正位上才去。**

    光看形状分不出「A 股」和「A 装 Python」——两者都是字母加空格。所以加一个
    条件：第 1 条是 A、第 2 条是 B、第 3 条是 C、第 4 条是 D。误伤就只剩
    「四个选项分别以 A/B/C/D 开头且顺序一致」，这种列表基本不存在，而模型
    自带标号倒是很常见。
    """
    if len(out) != CHOICES:
        return out
    for i, o in enumerate(out):
        m = _OPT_LABEL.match(o)
        if not m or m.group(1).upper() != "ABCD"[i]:
            return out
    bare = [_OPT_LABEL.sub("", o).strip() for o in out]
    return bare if all(bare) else out


def _strip_labels_after_shrink(out: list) -> list:
    """削回四个之后再去一次标号——这一批的标号是「五个里缺一个」的形状。

    不能复用 ``_strip_labels``：它的判据钉着「第 i 条必须是 ABCD[i]」，而削掉的
    那项如果在中间，剩下的就是 A/B/C/E（答案是 E 时删的是 D），顺序校验认不出，
    标号全留下——前端自己还会画 A/B/C/D，界面上就出现「D  E. 装 Go」。

    判据放宽到「每个字母都比上一个前进 1 或 2」：前进 1 是正常连号，前进 2 是跳过
    了被削掉的那个。第一条仍必须是 A，这条是防误伤的主闸——「B 站很火 / A 股大涨」
    这类内容列表第一条就不会是 A 标号。

    代价与 ``_strip_labels`` 同级：「A 股大涨 / B 站很火 / C 盘满了 / D 盘没空间」
    这种恰好从 A 起、字母又连号的真实内容仍会被误剥。这是已知的、与现状相同的
    风险，不因为这次改动变差；真要根治得看选项文字本身，那是另一个量级的事。
    """
    if len(out) != CHOICES:
        return out
    expect = ord("A")
    for i, o in enumerate(out):
        m = _OPT_LABEL.match(o)
        if not m:
            return out
        cur = ord(m.group(1).upper())
        if i == 0:
            if cur != expect:          # 第一条不是 A 就不是标号，是内容
                return out
        elif cur - expect not in (0, 1):
            return out
        expect = cur + 1
    bare = [_OPT_LABEL.sub("", o).strip() for o in out]
    return bare if all(bare) else out


def _options_of(raw) -> list:
    """选项列表 → 四个干净的字符串。形状不对就返回空列表（整题丢掉）。

    `"A. 打开官网"` 这种自带标号的去掉：界面上会自己画 A/B/C/D，留着就变成
    「A  A. 打开官网」。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for o in raw:
        if isinstance(o, dict):
            o = _pick(o, "text", "label", "value", "内容")
        if not isinstance(o, str):
            return []
        o = o.strip()
        if not o:
            return []
        out.append(o)
    return _strip_labels(out)


def answer_index(raw, options) -> int | None:
    """模型给的「正确答案」→ 下标。认不出返回 None（这道题会被丢掉）。

    三种写法依次认：
    1. 选项原文一字不差地抄回来——最可信，因为它和四个选项本身能直接对上；
    2. 字母 A–E（允许「选项B」「B.」这种写法）；
    3. 0 起的下标（整数字面量，或纯数字字符串）。

    字母认到 E，是因为 ``normalize`` 要先把「多给了一个选项」的题削回四个再判：
    五选项的题，正确答案可能就落在第 5 个上，那时候必须先能算出一个越界于「四个」
    但合法于「五个」的下标，削枝才知道该动哪一项。传四个选项进来时 E 照样越界、
    照样返回 None（`test_unusable_values_give_none` 钉着），所以放宽这个字符集
    不会让四选项的判分变松。

    先认原文再认字母：模型经常在 answer 里把正确选项整句抄一遍，那是最不容易
    歧义的表达。纯数字按 0 起算，和 JSON 里给整数时的含义保持一致——两处含义
    不同会让人踩一次就再也不敢信这个字段。
    """
    n = len(options or [])
    if not n or raw is None or isinstance(raw, bool):
        return None

    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, int):
        return raw if 0 <= raw < n else None

    s = _text_of(raw)
    if not s:
        return None

    for i, o in enumerate(options):
        if o.strip() == s:
            return i

    m = re.search(r"(?:^|[^A-Za-z])([A-Ea-e])(?![A-Za-z])", s)
    if m:
        i = ord(m.group(1).upper()) - ord("A")
        return i if 0 <= i < n else None

    digits = re.fullmatch(r"(\d+)", s)
    if digits:
        i = int(digits.group(1))
        return i if 0 <= i < n else None
    return None


def _shrink_to_four(opts: list, answer) -> tuple[list, int | None]:
    """五个选项削回四个，返回 ``(新选项, 新答案下标)``。削不动时返回 ``(原样, None)``。

    为什么削而不是丢题：五选一在中文单选里很常见，模型给五个选项并不说明这道题
    坏了——题干、出处、解析可能都对，只是多了一个干扰项。整题丢掉等于把好题一起
    扔了，而第二轮真跑里 P4 是五道题全这样，一篇的测验直接变成「一道都用不了」。

    削哪一个：**最后一个非答案项**。削哪个干扰项在题目质量上没有差别（都只是少
    一个干扰项），但「最后一个」是确定的——测试能钉住，用户复现时也不用猜。

    答案先按五个选项算：它可能就落在第 5 个上（"E"）。不先算出来，就不知道该
    保谁、也就无从判断削完之后答案还在不在。
    """
    idx = answer_index(answer, opts)
    if idx is None:
        return opts, None

    drop = max(i for i in range(len(opts)) if i != idx)
    kept = [o for i, o in enumerate(opts) if i != drop]
    # 五个进来时标号还留着（`_strip_labels` 只在恰好四个时动手），削完再抓一次。
    kept = _strip_labels_after_shrink(kept)

    # 重算而不是直接挪下标：答案是原文抄的，去掉标号之后可能不再一字不差。
    new_idx = answer_index(answer, kept)
    if new_idx is None:
        # 用位移兜底，这不是猜：删掉的一定是非答案项，剩下四项的相对位置只受这
        # 一次删除影响，映射是确定的。两个走这里的场景——答案给的是整数下标 4
        # （重算必然越界），以及「E 装 Go」这种带标号的原文（去掉标号后字母 E
        # 已经越界于四个选项）。
        new_idx = idx - (1 if drop < idx else 0)
    if not 0 <= new_idx < len(kept):
        return kept, None
    return kept, new_idx


def normalize(obj: dict) -> dict:
    """模型吐出来的对象 → 前端要的 ``{"questions": [...]}``。

    容错只做「形状」这一层：字段名认几个常见别名、不合格的题丢掉、缺的字段留空。
    **不补内容**：没有解析就是没有，不拿题干顶上去——那等于替模型编一句解析，
    而解析正是读者用来判断「我到底会不会」的东西。

    一道题要同时满足：题干非空、四个选项、正确答案能对上。缺一样就丢：
    少一个选项的题，剩下的选项反而更好猜；答案对不上的题，判分就是错的。

    **多一个选项是唯一的例外**：削掉一个干扰项，题照出。五选一在中文单选里很
    常见，模型给五个不说明题干或出处坏了，整题丢掉损失的是本来能用的题。但只削
    一个——六个及以上说明模型对形状已经完全没概念了，那种不替它揉。

    削过的题不进 ``skipped``：能判分就是能用，界面不需要知道「这题本来多一个干扰
    项」。那对读者没有意义，只会把一次安静的修正变成一条需要解读的提示。

    丢掉的每一道都记一条理由，放在 ``skipped`` 里带回前端。一道都不剩时要报
    「哪一项不对」（第几题、几个选项、答案对不上），而不是一句「生成失败」——
    前者能让人判断该重試还是该换模型，后者只能让人再点一次。
    """
    if not isinstance(obj, dict):
        raise SummaryError("parse", "模型返回的不是一个 JSON 对象。", 502)

    raw = _pick(obj, "questions", "quiz", "items", "题目")
    if not isinstance(raw, list):
        raise SummaryError(
            "parse", "模型返回的 JSON 里没有 questions 数组，多半是它没按格式来。"
                     "点「重新生成一套」再试一次。", 502)

    out, skipped = [], []
    for n, q in enumerate(raw, 1):
        if not isinstance(q, dict):
            skipped.append("第 %d 题不是一个对象" % n)
            continue
        stem = _text_of(_pick(q, "stem", "question", "q", "题干"))
        if not stem:
            skipped.append("第 %d 题没有题干" % n)
            continue
        opts = _options_of(_pick(q, "options", "choices", "选项"))
        answer = _pick(q, "answer", "correct", "答案")
        if len(opts) == CHOICES + 1:
            # 多给了一个：削掉一个干扰项，题留下来（见 docstring）。
            opts, idx = _shrink_to_four(opts, answer)
        elif len(opts) != CHOICES:
            # 少了不能补：「不补内容」同样适用于选项——凑出来的第四项只能靠编。
            skipped.append("第 %d 题有 %d 个选项（要 %d 个）" % (n, len(opts), CHOICES))
            continue
        else:
            idx = answer_index(answer, opts)
        if idx is None:
            skipped.append("第 %d 题的答案对不上任何一个选项" % n)
            continue
        out.append({
            "stem": stem,
            "options": opts,
            "answer": idx,
            "why": _text_of(_pick(q, "why", "explain", "explanation", "解析")),
            "t": parse_stamp(_pick(q, "t", "time", "stamp")),
        })

    if not raw:
        raise SummaryError(
            "parse", "模型一道题都没给（questions 是空数组），多半是它没按格式来。"
                     "点「重新生成一套」再试一次。", 502)
    if not out:
        raise SummaryError(
            "parse",
            "模型回了 %d 道题，一道都用不了：%s。点「重新生成一套」再试一次，"
            "或者换一个更听话的模型。"
            % (len(raw), "；".join(skipped[:5]) + ("；……" if len(skipped) > 5 else "")),
            502)

    result = {"questions": out}
    if skipped:
        # 有的能用、有的不能用：能用的照常出，坏的那几道只报数、不渲染
        result["skipped"] = skipped
    return result


def parse_content(content: str) -> dict:
    """助手说的话 → 题目（模型输出这一层）。

    拆信封、挖 JSON 那两步和总结共用 ``summary.json_object``；这里只解释
    「questions 长什么样」。
    """
    return normalize(json_object(content))


def count_of(raw) -> int:
    """要几道题。认不出就用默认值，超出范围就夹住。

    不因为一个奇怪的 count 把整次调用废掉：出题是主线，题数是偏好。
    """
    if isinstance(raw, bool) or raw is None:
        return DEFAULT_COUNT
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str):
        digits = re.fullmatch(r"\s*(\d+)\s*", raw)
        if not digits:
            return DEFAULT_COUNT
        raw = int(digits.group(1))
    if not isinstance(raw, int):
        return DEFAULT_COUNT
    return max(1, min(MAX_COUNT, raw))


def call(cfg: dict, text: str, has_time: bool = True,
         count=DEFAULT_COUNT, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """出一次题。成功返回 ``{"quiz": {"questions": [...]}, "model": 模型名}``。

    `cfg` 还是那三样：``{"base", "model", "key"}``。key 用完即弃，不落盘、
    不进日志、不进任何一条错误消息。
    """
    body = require_len(text)
    if not body:
        raise SummaryError(
            "empty", "这一篇没有正文，出不了题。换一篇有字幕的再试。", 400)

    n = count_of(count)
    system = (QUIZ_PROMPT % {"count": n}) + ("" if has_time else NO_TIME_RULE)
    out = complete(cfg, [
        {"role": "system", "content": system},
        # 正文包起来给：同一对标记问答也在用，模型看到的边界一致
        {"role": "user", "content": BODY_HEAD + body + BODY_TAIL},
    ], timeout=timeout, json_mode=True)
    return {"quiz": parse_content(out["content"]), "model": out["model"]}

