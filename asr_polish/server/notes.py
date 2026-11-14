# -*- coding: utf-8 -*-
"""笔记库：把一篇视频的要点与概念写成 Markdown 文件，攒成一个能直接用的库。

为什么落成文件，而不是又一个 localStorage
------------------------------------------
前三层（总结 / 问答 / 测验）存浏览器：它们只服务「这一次阅读」。这一层不一样，
它要跨篇攒起来，还要能被 Obsidian、被 grep、被 git 管版本。于是只能落盘，
而且必须是**人可读的 md**：没有数据库、没有导出、没有转换。

目录沿用 `FOR_AI.md` 里已经写下的那个约定（`notes/` + `INDEX.md`），不另起一套：

    notes/
      INDEX.md              目录：先视频、后概念
      <标题>.md             一篇视频一份
      concepts/<概念>.md    一个概念一份

为什么概念要单独成文、还要去重
------------------------------
同一件事在不同视频里被讲两遍，才是「攒成库」的意义。所以概念按名字归一后
落到同一个文件里，第二次出现只追加一行出处。归一规则刻意保守——只做
`NFKC` + 去首尾空白 + 折叠中间空白 + ASCII 小写：

- 不做近义合并（「阈值」和「门限」不会自动并成一个），那是语义判断，做错了
  比不做更糟：两个概念被强行焊在一起，用户再也分不开。
- 不做繁简转换，也不做同义词表——这张表没有尽头，而这里要的只是
  「同一个字符串的两种写法」（全角括号、多余空格、大小写）别开两个文件。

关于 key 与成本
---------------
和前三层同一条链：`summary.require_cfg` → `complete` → 错误翻译。这一层多的
只有两件事：**写文件**，以及把已有概念名（只名字）带进提示词。已有名字是这
一层最便宜的防重复手段——模型每次给同一个概念起个新名，库就永远聚不起来。

关于时间戳
----------
和总结、问答、测验同一套：模型抄行首的时间，服务端拿这一篇**真实的段首**比一遍
（1.5 秒容差）。对得上才写成可点的锚点，对不上就只留文字、不写时间——编一个
时间写进文件，比不写坏得多，因为文件会被读很多次。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from .._atomic import replace_with_retry
from ..export import safe_name as _safe
from .summary import (BODY_HEAD, BODY_TAIL, DEFAULT_TIMEOUT, SummaryError,
                      complete, json_object, parse_stamp, require_len)

#: 笔记库的目录名，落在服务进程的当前目录下（和 `FOR_AI.md` 的约定一致）。
DIR_NAME = "notes"

#: 视频笔记的目录索引。名字是约定的，Obsidian 打开就能看到。
INDEX_NAME = "INDEX.md"

#: 概念笔记放这个子目录，免得和视频笔记混在一层。
CONCEPT_DIR = "concepts"

#: 锚点容差，和阅读页 `matchStamp` 的默认值一致。为什么是 1.5 秒：
#: 页面显示的时间四舍五入过（start 62.7 显示成 01:03），不放容差每条都会判无出处。
TOL = 1.5

#: 一篇最多写几条要点、几个概念。上限挡的是模型「一条拆成三条」把文件灌满。
MAX_POINTS = 12
MAX_CONCEPTS = 20

#: 提示词。和前三层一样做成常量、不给用户改：输出形状被代码解析，格式一变整块就废。
#:
#: 四件事各自对应一件真事：
#: 1. **只抽原文讲过的**——否则它会拿训练数据里的常识凑数，写进文件后没人看得出；
#: 2. **每个概念带定义和出处**——定义是为了半年后还能看懂，出处是为了点得回去；
#: 3. **概念名短且稳定**——名字是文件名、是链接、是去重的键，改一次就分家一次；
#: 4. **已有概念名原样复用**——这一层最容易做假的地方，见模块头。
PROMPT = """你是学习助理，正在帮人把视频笔记攒成一个长期的知识库。

下面是一篇视频文字稿，每行开头的 [分:秒] 是这一行对应的时间。请整理成 JSON，
**只输出一个 JSON 对象**，不要解释、不要 markdown 围栏：

{
  "points": [{"text": "要点", "t": "01:23"}],
  "concepts": [{"term": "概念名", "def": "一句话定义", "t": "01:23"}]
}

要求：

1. points 3 到 8 条，按原文顺序，每条 30 字以内，用自己的话概括，不要整段抄原文。
2. concepts 3 到 8 个，只挑这一篇里**确实讲过**、且值得记下来的概念。原文没讲的
   不许写，不许用你自己的知识补。
3. 每个 point 和 concept 的 t，必须**原样抄**它所依据的那一行行首的时间，写成
   "01:23" 这样的字符串（不带方括号）。不许自己换算成秒数，不许写原文里没有的时间。
4. term 要短（2 到 8 个字）、要稳定：这篇里管它叫「阈值」，就别在这一篇里又写成
   「阈值参数」「阈值设定」。用最常见的那个说法。
5. def 一句话说透，40 字以内，不要客套话。
6. 下面会给出这个库**已经有的概念名**。如果这一篇讲的某个概念和其中某条指的是
   同一件事，**必须原样复用那个名字**，不要换一种说法再起一个新的。已有名字里
   没有的，才起新名。"""

#: 已有概念名放在这里给模型看。只给名字，不给全文——给全文等于每次把整个库
#: 发一遍，token 花在重复内容上。
KNOWN_HEAD = "\n\n【这个库已经有的概念名】\n"

#: 概念名之间用顿号分隔更符合中文习惯，但模型对「、」和「,」的切分都很稳，
#: 用「、」还能少一点和 JSON 里的逗号混淆。
KNOWN_SEP = "、"

#: 没有逐句时间时追加的一段。不追加的话，模型看见模板里写着 [分:秒] 却发现
#: 行首没有，很可能自己编一个——编出来的会被判无出处，但写在文件里就是假信息。
NO_TIME_RULE = """

注意：这一篇的文字稿没有逐句时间戳，行首没有 [分:秒]。这种情况下所有 t 一律写
null，def 和 text 里也不要写任何时间，不要编造时间。"""


# --------------------------------------------------------------------------- #
# 概念名归一
# --------------------------------------------------------------------------- #

#: 概念名里的空白：全角空格、制表、换行都算。折叠成一个普通空格。
_WS = re.compile(r"\s+")


def concept_key(name: str) -> str:
    """概念名的归一键：**只认写法差异，不认语义差异**。

    NFKC 处理全角/半角（`（`→`(`、全角字母→半角），再去首尾空白、把中间的
    连续空白折成一个，最后把 ASCII 字母转小写（`HTTP` / `http` 指同一个东西）。

    刻意不做的三件事，理由都在模块头：不做近义合并、不做繁简转换、
    不做标点删除——「A/B 测试」和「A/B测试」是同一个，但「AB 测试」不是。
    """
    s = unicodedata.normalize("NFKC", name or "")
    s = _WS.sub(" ", s).strip()
    return s.lower()


def concept_name(name: str) -> str:
    """给人和文件名用的形态：NFKC + 去首尾空白 + 折叠空白，**不改大小写**。

    `HTTP 缓存` 保持大写，因为那是它本来的写法；只有归一键才折成小写。
    """
    s = unicodedata.normalize("NFKC", name or "")
    return _WS.sub(" ", s).strip()


# --------------------------------------------------------------------------- #
# 模型输出
# --------------------------------------------------------------------------- #

def _pick(obj: dict, *keys):
    """按顺序取第一个存在且非空的值。

    不用 `or` 串联：`0` 和 `""` 都是假值，用 `or` 会把下标 0 跳过去。
    """
    for k in keys:
        if k in obj:
            v = obj[k]
            if v is not None and v != "":
                return v
    return None


def _text_of(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def normalize(obj: dict, has_time: bool = True) -> dict:
    """模型吐出来的对象 → ``{"points": [...], "concepts": [...], "skipped": [...]}``。

    **不补内容**：没有定义就是没有，不拿概念名顶上去；没有出处就是没有，不写
    一个看起来像时间的 00:00。残缺的概念不写进文件——文件会被读很多次，一条
    假信息留在里面比少一条糟得多。

    逐条记原因（`skipped`）带回前端：一道都对不了时，用户需要知道是「没给定义」
    还是「没给出处」，这决定了该重试还是换模型。
    """
    if not isinstance(obj, dict):
        raise SummaryError("parse", "模型返回的不是一个 JSON 对象。", 502)

    raw_points = _pick(obj, "points", "要点", "key_points")
    if not isinstance(raw_points, list):
        raw_points = []

    points, skipped = [], []
    for n, p in enumerate(raw_points, 1):
        if isinstance(p, str):
            text, stamp = p.strip(), None
        elif isinstance(p, dict):
            text = _text_of(_pick(p, "text", "point", "内容"))
            stamp = _pick(p, "t", "time", "stamp")
        else:
            skipped.append("第 %d 条要点不是文字" % n)
            continue
        if not text:
            skipped.append("第 %d 条要点是空的" % n)
            continue
        points.append({"text": text, "t": parse_stamp(stamp)})
        if len(points) >= MAX_POINTS:
            break

    raw_concepts = _pick(obj, "concepts", "概念", "terms")
    if not isinstance(raw_concepts, list):
        raw_concepts = []

    concepts = []
    for n, c in enumerate(raw_concepts, 1):
        if isinstance(c, str):
            # 少数模型写成「概念：定义」
            term, _, define = c.partition("：")
            term, define, stamp = term.strip(), define.strip(), None
        elif isinstance(c, dict):
            term = _text_of(_pick(c, "term", "name", "概念", "术语"))
            define = _text_of(_pick(c, "def", "definition", "desc", "定义", "解释"))
            stamp = _pick(c, "t", "time", "stamp")
        else:
            skipped.append("第 %d 个概念不是文字" % n)
            continue

        if not term:
            skipped.append("第 %d 个概念没有名字" % n)
            continue
        if not define:
            skipped.append("概念「%s」没有定义" % concept_name(term)[:20])
            continue
        t = parse_stamp(stamp)
        if has_time and t is None:
            # 有逐句时间的篇里没给出处，等于这条概念点不回去。宁可不要：
            # 留着它，读者会以为「这一条本来就没时间」。
            skipped.append("概念「%s」没有出处时间" % concept_name(term)[:20])
            continue
        concepts.append({"term": concept_name(term), "def": define, "t": t})
        if len(concepts) >= MAX_CONCEPTS:
            break

    if not points and not concepts:
        raise SummaryError(
            "parse",
            "模型这次没给出能用的内容（要点和概念都为空，或者全都缺定义/缺出处）。"
            "点「存入笔记库」再试一次，或者换一个更听话的模型。", 502)

    # 同一个概念被写了两次（`阈值` 和 `  阈值 `）：归一之后同名，只留第一次。
    # 只在写文件那一步去重是不够的——接口回给界面的仍是三条，界面就会说
    # 「3 个概念」，而文件里只有 2 个，谁看谁都觉得少了一个。
    return {"points": points, "concepts": _dedupe(concepts),
            "skipped": skipped}


def parse_content(content: str, has_time: bool = True) -> dict:
    """助手说的话 → 要点与概念。拆信封、挖 JSON 那两步复用总结那一套。"""
    return normalize(json_object(content), has_time)


def build_system(known: list[str], has_time: bool) -> str:
    """拼系统提示词：常量 + 已有概念名 +（没有时间时的）额外说明。

    已有概念名为空时明说「还没有」——留空会让模型以为「这一段没给」，转而
    自己发挥；写清楚它才知道这一篇是开路的第一篇。
    """
    names = [concept_name(x) for x in (known or []) if concept_name(x)]
    if names:
        head = KNOWN_HEAD + KNOWN_SEP.join(names) + "\n"
    else:
        head = KNOWN_HEAD + "（还没有，这是第一篇）\n"
    return PROMPT + head + ("" if has_time else NO_TIME_RULE)


def call(cfg: dict, text: str, known: Optional[list[str]] = None,
         has_time: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """让模型读一遍这一篇，回要点和概念。**这一步花钱**，只在点按钮时调。"""
    body = require_len(text)
    if not body:
        raise SummaryError(
            "empty", "这一篇没有正文，写不出笔记。换一篇有字幕的再试。", 400)
    out = complete(cfg, [
        {"role": "system", "content": build_system(known or [], has_time)},
        {"role": "user", "content": BODY_HEAD + body + BODY_TAIL},
    ], timeout=timeout)
    return {"notes": parse_content(out["content"], has_time),
            "model": out["model"]}


# --------------------------------------------------------------------------- #
# 锚点
# --------------------------------------------------------------------------- #

def match_stamp(segments: list[dict], sec: Optional[int],
                tol: float = TOL) -> int:
    """这个时间落在这篇的第几段；够不着返回 -1。

    和阅读页同一条规则：取最近的段首，差的绝对值不超过 1.5 秒才算数。
    这里只用来决定「写不写锚点」，不决定内容——对不上就只留文字。
    """
    if sec is None or not segments:
        return -1
    best, best_d = -1, float("inf")
    for i, s in enumerate(segments):
        st = s.get("start")
        if st is None:
            continue
        d = abs(float(st) - float(sec))
        if d < best_d:
            best, best_d = i, d
    return best if best_d <= tol else -1


def clock(seconds: float) -> str:
    """秒 → ``mm:ss``（超过一小时写 ``h:mm:ss``）。

    和阅读页 `fmt` 的格式一致：进了文件的时间戳要和页面上的长得一样，否则
    用户在两边看到 62:30 / 1:02:30 会以为是两个时间。
    """
    s = int(round(seconds or 0))
    if s >= 3600:
        return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)
    return "%02d:%02d" % (s // 60, s % 60)


def part_sig(part: dict) -> str:
    """这一篇的指纹，用来判断「笔记是不是配得上现在的字幕」。

    形状与阅读页的 `summarySig` 一致（段数:字数:首24|尾24），但**段的口径不同**：
    阅读页用的是合并后的段落，这里是 `transcript` 的原始段。不改成一个口径是因为
    两边都只和自己比——笔记的写和比都在服务端，同一个函数算两遍，不会叉开。
    用内容而不是任务时间：任务重跑不等于字幕变，字幕变了指纹一定变。
    """
    segs = (part or {}).get("segments") or []
    if not segs:
        return "0"
    chars = sum(len(str(s.get("text") or "")) for s in segs)
    head = str(segs[0].get("text") or "")[:24]
    tail = str(segs[-1].get("text") or "")[-24:]
    return "%d:%d:%s|%s" % (len(segs), chars, head, tail)


# --------------------------------------------------------------------------- #
# 文件
# --------------------------------------------------------------------------- #

def _yaml_str(s: str) -> str:
    """YAML 里的字符串一律用 JSON 双引号形式。

    标题里满是冒号、方括号、竖线（`P1: 先讲什么 | 后讲什么`），裸写会把
    frontmatter 解析成别的东西。JSON 字符串恰好是合法的 YAML 双引号标量，
    而且 `json` 本来就在手上——不用为此引入 PyYAML。
    """
    return json.dumps(str(s), ensure_ascii=False)


def _yaml_read(raw: str) -> str:
    """读回 `_yaml_str` 写的那种值；不是引号形式就原样返回。"""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return str(json.loads(s))
        except ValueError:
            return s[1:-1]
    return s


def split_front(text: str) -> tuple[dict, str]:
    """``---`` 包起来的 frontmatter → ``(字段, 正文)``。

    解析刻意宽松：认不出的行跳过，没有 frontmatter 就返回空。笔记文件是用户
    会手改的东西——他删掉一行、加一行注释，不该让整个笔记库打不开。
    """
    if not (text or "").startswith("---"):
        return {}, text or ""
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block = text[3:end]
    body = text[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    meta: dict[str, str] = {}
    for line in block.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = _yaml_read(val)
    return meta, body


def _write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再替换。

    直接覆盖一个正在 Obsidian 里开着的文件，Windows 上会拿到共享冲突；
    `replace_with_retry` 会退避重试。写完统一 LF，git 里好看。

    用 `open(..., newline="\\n")` 而不是 `Path.write_text`：后者到 3.10 才有
    `newline` 参数，而这个项目支持 3.9。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.replace("\r\n", "\n"))
    replace_with_retry(tmp, path)


def _reader_url(host: str, task_id: str, part_index: int,
                sec: Optional[int]) -> str:
    """笔记里那一条锚点指向的地址；`sec` 为 None 时只定位到这一篇。

    用绝对地址而不是相对路径：笔记是给人用 Obsidian 打开的 md，相对路径在
    Obsidian 里点不动；绝对地址至少能在浏览器里打开同一个本机服务。端口变了
    旧链接会失效——笔记页会按当前端口重新生成链接，md 里的那份是当时的样子。
    """
    base = (host or "127.0.0.1:8040").strip()
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    url = "%s/reader?task=%s&part=%d" % (base.rstrip("/"), _quote(task_id),
                                         int(part_index))
    if sec is not None:
        url += "&t=%d" % int(sec)
    return url


def _link(text: str, url: str) -> str:
    """普通 Markdown 链接（文件路径走 `_md_link`，那个要用尖括号）。"""
    return "[%s](%s)" % (text, url)


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s), safe="")


def _md_link(text: str, path: str) -> str:
    """Markdown 链接，路径用尖括号包起来。

    文件名里有空格、括号、中文，用 `%E9%98%88…` 那种编码在编辑器里读不了；
    `[标题](<文件名.md>)` 是 CommonMark 认的写法，Obsidian 也照常打开。
    """
    return "[%s](<%s>)" % (text, path)


def _anchor(host: str, task_id: str, part_index: int,
            sec: Optional[int]) -> str:
    """`[12:41](url)`；没有可用的锚点时返回空串。

    没有锚点就**一个字符都不写**——写个 `[--:--]` 或「无出处」进文件，半年后
    看笔记的人只会以为这条本来是坏的。
    """
    if sec is None:
        return ""
    return "[%s](%s)" % (clock(sec), _reader_url(host, task_id, part_index, sec))


def video_path(root: Path, title: str, label: str = "") -> Path:
    """视频笔记的落点：``notes/<标题>.md``，多分 P 时带 ``· P2``。

    不带 P 号的话，一个合集里每一篇都会写到同一个文件上，后写的把先写的盖掉。
    标题取视频标题；标题为空时退回分 P 名（总比 `untitled.md` 有用）。
    """
    stem = _safe(title or "未命名", limit=60)
    if label:
        stem = "%s · %s" % (stem, label)
    return root / (stem + ".md")


def concept_path(root: Path, name: str) -> Path:
    """概念笔记的落点：``notes/concepts/<概念>.md``。"""
    return root / CONCEPT_DIR / (_safe(name or "未命名", limit=60) + ".md")


def _concept_path_n(root: Path, name: str, n: int) -> Path:
    """带序号的落点：``<概念> (2).md``。

    序号要先留出位置再截断——`_safe` 砍 60 个字符，先拼 `" (2)"` 再截的话，
    一个刚好 60 字的概念名会把序号截掉，两个不同的概念又落回同一个文件名。
    """
    if n <= 1:
        return concept_path(root, name)
    tag = " (%d)" % n
    stem = _safe(name or "未命名", limit=60 - len(tag))
    return root / CONCEPT_DIR / (stem + tag + ".md")


def _free_concept_path(root: Path, name: str, taken: set) -> Path:
    """给概念找个还没被占的文件名。

    不同概念经 `_safe` 之后可能同名（`A/B` 和 `A:B`、`TCP/IP` 和 `TCP_IP`），
    直接写就互相覆盖。加序号，但**只改文件名，不改概念名**：名字是要写进
    frontmatter 的，带个 `(2)` 之后下次就再也匹配不上原来那一条了，序号
    只会一路往下滚（`(3)`、`(4)`…），库里同一个概念散成一串。
    """
    path = concept_path(root, name)
    if not path.exists() and path.name not in taken:
        return path
    for n in range(2, 1000):
        cand = _concept_path_n(root, name, n)
        if not cand.exists() and cand.name not in taken:
            return cand
    return _concept_path_n(root, name, 1000)


def _wikilink(name: str, stem: str = "") -> str:
    """`[[文件]]`；文件名和概念名不一致时写成 `[[文件|显示名]]`。

    Obsidian 按**文件名**解析 wikilink，而文件名是 `_safe` 之后的形态
    （`TCP/IP` 落到 `TCP_IP.md`）。只写 `[[TCP/IP]]` 在 Obsidian 里是死链；
    只写 `[[TCP_IP]]` 又在笔记页搜不到——那一页拿链接里的字当搜索词，按概念名
    匹配。两个都要，就得带别名（Obsidian 认 `[[文件|显示]]`，笔记页也认）。
    """
    label = (name or stem).replace("|", "｜")
    if not stem or stem == label:
        return "[[%s]]" % label
    return "[[%s|%s]]" % (stem, label)


def fmt_date(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or datetime.now().timestamp()).strftime("%Y-%m-%d")


def fmt_time(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or datetime.now().timestamp()).strftime("%Y-%m-%d %H:%M")


#: 产物文件名：`BV1xx411c7mD-标题.srt` 或 `BV1xx411c7mD-P2-标题.srt`。
#: 和阅读页 `BV_RE`、`export.base_name` 是同一套规则，不另起一套。
_PART_NAME = re.compile(r"^(BV[0-9A-Za-z]{8,14})(?:-P(\d+))?-(.*)$")


def part_meta(task, part: dict, index: int, total: int,
              same_bv: bool = True) -> dict:
    """从产物文件名 + 任务结果里取这一篇的标题 / BV 号 / 分 P。

    和阅读页 `partMeta` 一样从文件名解，不去问接口——`transcript` 返回的
    `name` 本来就带着这两样，为这件事改接口不值得。

    视频标题优先用 `task.result.items` 里那条（B站给的标题），查不到才退回
    文件名里那段。多分 P 时文件名里那段往往是分 P 标题，不是视频标题。
    """
    stem = Path(part.get("name") or "").stem
    m = _PART_NAME.match(stem)
    bvid = m.group(1) if m else ""
    page = int(m.group(2)) if (m and m.group(2)) else 1
    part_title = (m.group(3) if m else stem) or stem

    items = (getattr(task, "result", None) or {}).get("items") or []
    video_title = ""
    for it in items:
        if isinstance(it, dict) and it.get("bvid") == bvid and it.get("title"):
            video_title = str(it["title"])
            break

    label = ""
    if total > 1:
        # 同一个合集的多个分 P 用 P2；多视频任务（up: / search:）写「第 2 篇」。
        # 两种写法都和阅读页目录一致，免得同一个东西有两个名字。
        label = ("P%d" % page) if same_bv else ("第 %d 篇" % (index + 1))

    return {"bvid": bvid, "page": page, "part_title": part_title,
            "title": video_title or part_title, "label": label,
            "part_index": index, "part_name": part.get("name") or ""}


# --------------------------------------------------------------------------- #
# 写笔记
# --------------------------------------------------------------------------- #

def render_video_note(meta: dict, points: list[dict], concepts: list[dict],
                      host: str) -> str:
    """视频笔记的正文。结构固定，笔记页也是照着这个结构认的。

    frontmatter 里除了任务书要的那六项，还多记两样：`指纹`（判断字幕变没变）
    和 `分P文件`（回到 transcript 的哪一份）。没有它们，过期判断只能靠时间戳，
    而任务重跑不等于字幕变了。

    `meta` 用英文键（title/bvid/label/...），写进文件才换成中文——内部的键
    跟着代码走，文件里的键跟着人走，两边不必一致。
    """
    task_id = meta.get("task_id") or ""
    idx = int(meta.get("part_index") or 0)
    fields = [("标题", meta.get("title", "")), ("BV号", meta.get("bvid", "")),
              ("分P", meta.get("label", "")), ("日期", meta.get("date", "")),
              ("来源任务", task_id), ("生成时间", meta.get("at", "")),
              ("分P序号", str(int(meta.get("part_index") or 0))),
              ("指纹", meta.get("sig", "")),
              ("分P文件", meta.get("part_name", ""))]
    if meta.get("model"):
        fields.append(("模型", meta["model"]))
    out = ["---"]
    for key, val in fields:
        out.append("%s: %s" % (key, _yaml_str(val)))
    out.append("---")
    out.append("")
    out.append("# " + (meta.get("title") or "未命名"))
    out.append("")

    if not meta.get("has_time"):
        out.append("> 这一篇没有逐句时间戳（必剪 ASR 或纯文本导出），"
                   "概念无法定位到视频。")
        out.append("")

    out.append("## 要点")
    out.append("")
    if not points:
        out.append("_模型这次没给出要点。_")
    for p in points:
        a = _anchor(host, task_id, idx, p.get("sec"))
        out.append("- " + p["text"] + ((" " + a) if a else ""))
    out.append("")

    out.append("## 概念")
    out.append("")
    if not concepts:
        out.append("_模型这次没给出概念。_")
    for c in concepts:
        a = _anchor(host, task_id, idx, c.get("sec"))
        # 链接指到**落盘的那个文件**：`stem` 由 save_video_note 预先算好
        # （`TCP/IP` → `TCP_IP`），显示名仍是模型给的原话。
        line = "- %s — %s" % (_wikilink(c.get("term") or "", c.get("stem") or ""),
                              c["def"])
        out.append(line + ((" " + a) if a else ""))
    out.append("")
    return "\n".join(out)


def render_concept_note(name: str, first: str, define: str,
                        occ: list[dict], host: str) -> str:
    """概念笔记。定义只留第一次那一份——同一个概念在不同视频里的说法会飘，
    每次覆盖会让文件变成「最后一篇怎么讲」，而不是「这个概念是什么」。
    """
    out = ["---",
           "%s: %s" % ("概念", _yaml_str(name)),
           "%s: %s" % ("首次出现", _yaml_str(first)),
           "---", "",
           "# " + name, "",
           define or "_还没有定义。_", "",
           "## 出现过的地方", ""]
    for o in occ:
        a = _anchor(host, o.get("task_id", ""), int(o.get("part_index") or 0),
                    o.get("sec"))
        bits = [o.get("title") or o.get("part_name") or "（未知视频）"]
        if o.get("label"):
            bits.append(o["label"])
        # 标题本身也做成链接：这一篇出现过，但说不到第几秒（必剪 ASR 没有
        # 逐句时间戳，或者模型的 12:41 对不上任何一段）。只写一行死字的话，
        # 读的人知道它在这儿出现过，却没法跳过去看一眼。
        #
        # 另一个原因是它必须可解析：这一行下次会被读回来、再整篇重写一遍，
        # 认不出 task 的行一重写就没了——「出现过的地方」会随着保存次数变少。
        line = "- " + _link(
            " · ".join(bits),
            _reader_url(host, o.get("task_id", ""),
                        int(o.get("part_index") or 0), None))
        if a:
            line += " · " + a
        out.append(line)
    out.append("")
    return "\n".join(out)


def parse_concept_note(text: str) -> dict:
    """读一份概念笔记 → ``{name, first, define, occ}``。

    `occ` 里的每一项是从链接里反解出来的（task / part / t）；手工加的行解析
    不了就跳过——笔记库是能被人手改的，读不动的那几行不该让整页崩。
    """
    meta, body = split_front(text)
    name = meta.get("概念") or ""
    lines = body.split("\n")
    define, occ, in_occ = [], [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            in_occ = "出现过" in s
            continue
        if in_occ:
            occ.append(_parse_occ_line(s))
            continue
        if s and not s.startswith("#") and not s.startswith("_"):
            define.append(s)
    return {"name": name, "first": meta.get("首次出现") or "",
            "define": " ".join(define).strip(),
            "occ": [o for o in occ if o]}


_OCC_T = re.compile(r"[?&]t=(\d+)")
_OCC_PART = re.compile(r"[?&]part=(\d+)")
_OCC_TASK = re.compile(r"[?&]task=([^&\s)]+)")
_OCC_LABEL = re.compile(r"^(?:P\d+|第\s*\d+\s*篇)$")


def _parse_occ_line(line: str) -> Optional[dict]:
    """``- [标题 · P1](…&part=0) · [12:41](…&t=761)`` → dict。

    `t` 可以没有：没有锚点的出处照样是一条出处。以前这里硬要 `t`，于是
    「必剪转写的一篇 · P2」这种行读不回来，下次保存重写整篇时就被抹掉——
    库里的出处会随着保存次数一次次变少，而且看不出是谁删的。
    """
    if not line.startswith("- "):
        return None
    from urllib.parse import unquote
    m_task = _OCC_TASK.search(line)
    if not m_task:
        return None
    m_part, m_t = _OCC_PART.search(line), _OCC_T.search(line)

    body = line[2:].strip()
    head = re.match(r"^\[([^\]]*)\]\(", body)
    if head:
        text = head.group(1).strip()
    else:
        # 老写法（标题不在链接里）：`标题 · P1 · [12:41](url)`
        text = re.split(r"\s\[|\(http", body, maxsplit=1)[0].strip()
    bits = [b.strip() for b in text.split("·") if b.strip()]
    label = bits[-1] if len(bits) > 1 and _OCC_LABEL.match(bits[-1]) else ""
    title = " · ".join(bits[:-1]) if label else " · ".join(bits)

    return {"title": title, "label": label,
            "task_id": unquote(m_task.group(1)),
            "part_index": int(m_part.group(1)) if m_part else 0,
            "sec": int(m_t.group(1)) if m_t else None}


def load_concepts(root: Path) -> list[dict]:
    """读出库里所有概念笔记（按名字排序）。

    读不动的文件跳过并在 `bad` 里留一条——一个手改坏的文件不该让整页空掉，
    但也不能装作没发生：笔记页会把这条显示出来。
    """
    d = root / CONCEPT_DIR
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.md")):
        try:
            data = parse_concept_note(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not data["name"]:
            data["name"] = path.stem
        data["file"] = path.name
        # 文件名（去扩展名）和概念名是两回事：`TCP/IP` 在磁盘上叫 `TCP_IP`。
        # wikilink 认前者，搜索认后者，两个都得留着。
        data["stem"] = path.stem
        out.append(data)
    return out


def _as_index(raw) -> Optional[int]:
    """frontmatter 里的「分P序号」→ int；没有或者不是数字就当 0。

    0 是安全的默认：第 1 篇就是 0，而手工改坏一个数字最坏也只是跳到第 1 篇，
    不该让整条链接没有 part。
    """
    try:
        return max(0, int(str(raw or "").strip()))
    except (TypeError, ValueError):
        return 0


def load_videos(root: Path) -> list[dict]:
    """读出库里所有视频笔记。`concepts/<x>.md` 和 `INDEX.md` 不算。"""
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.md")):
        if path.name == INDEX_NAME:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = split_front(text)
        if not meta.get("标题") and not meta.get("来源任务"):
            # 不是我们写的文件（用户自己放进来的笔记）不硬当成视频笔记
            continue
        out.append({
            "file": path.name,
            "title": meta.get("标题") or path.stem,
            "bvid": meta.get("BV号") or "",
            "label": meta.get("分P") or "",
            "date": meta.get("日期") or "",
            "at": meta.get("生成时间") or "",
            "task_id": meta.get("来源任务") or "",
            "part_index": _as_index(meta.get("分P序号")),
            "part_name": meta.get("分P文件") or "",
            "sig": meta.get("指纹") or "",
            "model": meta.get("模型") or "",
            "body": body,
        })
    return out


def _plan_concepts(root: Path, concepts: list[dict]) -> list[dict]:
    """这一批概念各自落到哪个文件：``[{"path": Path, "existing": dict|None}]``。

    只算、不写。算这一趟是为了视频笔记里的 `[[链接]]`——链接指向的是**文件名**
    （`TCP/IP` 落到 `TCP_IP.md`），不先算出来就只能猜，猜错就是 Obsidian 里的
    一个死链，而且会跟文件一起攒在库里。

    同一批里的两个概念可能撞到同一个文件名（`A/B` 和 `A:B`），所以这里一边算
    一边占位，和真正写入的顺序保持一致。读一次盘：一批概念共用一份索引，
    而不是每个概念重新扫一遍目录。
    """
    index: dict = {}
    for item in load_concepts(root):
        key = concept_key(item["name"])
        if key and key not in index:
            index[key] = item

    plan, taken = [], set()
    for c in concepts:
        name = c.get("term") or ""
        hit = index.get(concept_key(name))
        if hit is not None:
            path = root / CONCEPT_DIR / hit["file"]
            taken.add(path.name)
            plan.append({"path": path, "existing": hit})
            continue
        path = _free_concept_path(root, name, taken)
        taken.add(path.name)
        plan.append({"path": path, "existing": None})
    return plan


def save_video_note(root: Path, meta: dict, points: list[dict],
                    concepts: list[dict], host: str) -> dict:
    """写视频笔记 + 概念笔记 + INDEX。

    顺序有讲究：**先写视频笔记，再追加概念**。反过来的话，概念里已经有这条
    出处了、视频笔记却没写成，库里就多出一条「指向不存在笔记」的引用。

    返回 ``{"file", "added", "reused"}``：界面上要能说清「新建了几个概念、
    复用了几个」——这是用户判断「库到底有没有聚起来」的唯一依据。

    同一个概念在一条回答里出现两次（模型爱干这事：`阈值` 和 `  阈值 `）只在
    视频里写一行、出处也只追加一行。不归并的话，界面会同时说「新增 1 个、
    复用 1 个」，用户没法判断库到底是聚上了还是没聚上。
    """
    points = [_with_sec(p, meta) for p in points]
    concepts = _dedupe([_with_sec(c, meta) for c in concepts])

    # 落点先算清楚（仍不落盘），视频笔记里的链接才指得到真文件
    plan = _plan_concepts(root, concepts)
    for c, slot in zip(concepts, plan):
        c["stem"] = slot["path"].stem

    path = video_path(root, meta.get("title", ""), meta.get("label", ""))
    _write(path, render_video_note(meta, points, concepts, host))

    added, reused = [], []
    for c, slot in zip(concepts, plan):
        _, is_new = upsert_concept(root, c, meta, host, slot)
        (added if is_new else reused).append(c.get("term") or "")

    rebuild_index(root, host)
    return {"file": path.name, "added": added, "reused": reused}


def _dedupe(items: list[dict]) -> list[dict]:
    """按 `concept_key` 去重，留第一次出现的那条（顺序也跟着第一次）。"""
    seen, out = set(), []
    for it in items:
        key = concept_key(it.get("term") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _with_sec(item: dict, meta: dict) -> dict:
    """给每条配上「落点那一段的秒数」，对不上就 None（不写锚点）。

    `sec` 用的是**真实段首**而不是模型给的数：写进文件的时间要和读者点进去
    看到的那一段一致。模型给的 01:23 若四舍五入自 82.6 秒，这里写的就是 83。

    `t` 允许是 "01:23" 这种字符串：`normalize` 出来的已经是秒数，但直接调
    `save_video_note` 的地方（测试、以后的脚本）给字符串不该炸。
    """
    out = dict(item)
    stamp = item.get("t")
    if isinstance(stamp, str):
        stamp = parse_stamp(stamp)
    idx = match_stamp(meta.get("segments") or [], stamp)
    out["sec"] = int(round(meta["segments"][idx]["start"])) if idx >= 0 else None
    return out


def upsert_concept(root: Path, c: dict, meta: dict, host: str,
                   slot: Optional[dict] = None) -> tuple[str, bool]:
    """把一个概念写进库：有就追加一行出处，没有就新建。返回 ``(路径, 是否新建)``。

    去重按 `concept_key`。同一个键第二次出现时**只追加出处**，不覆盖定义——
    定义是「这个概念是什么」，不随哪一篇先讲而变化；覆盖会把它变成
    「最后一篇怎么讲」。

    `slot` 是 `_plan_concepts` 算好的落点（`{"path", "existing"}`）。给定时
    不再自己扫目录——同一批概念挨个扫一遍，是 O(概念数 × 库大小) 的白读。
    """
    name = c.get("term") or ""
    if slot is None:
        slot = _plan_concepts(root, [c])[0]
    path, existing = slot["path"], slot["existing"]

    if existing is None:
        occ = [dict(meta, sec=c.get("sec"))]
        # 写进文件的是**原始概念名**，不是文件名：`TCP/IP` 落到 `TCP_IP.md`
        # 是磁盘的规矩，`# TCP/IP` 才是给人看的东西。撞名加了 `(2)` 时也照此
        # 办——那条概念的身份仍然是它本来的名字，下次才认得出是同一条。
        _write(path, render_concept_note(name or path.stem,
                                         meta.get("date") or fmt_date(),
                                         c.get("def", ""), occ, host))
        return str(path), True

    # 已有文件的落点由 plan 给的 existing 决定，不再按名字重算一次——
    # 名字和文件名早就不是一回事了（`TCP/IP` ↔ `TCP_IP.md`）。
    path = root / CONCEPT_DIR / existing["file"]
    name_used = existing["name"]
    define = existing["define"] or c.get("def", "")
    occ = list(existing["occ"])
    here = {"title": meta.get("title") or "", "label": meta.get("label") or "",
            "task_id": meta.get("task_id") or "",
            "part_index": int(meta.get("part_index") or 0),
            "sec": c.get("sec")}
    # 出处的最小单位是「这一篇」，不是「这一秒」：同一篇里这个概念被讲了两次
    # （或者重存一遍、模型这次给的时间又落在别的段上），只留一行。按秒去重会
    # 让同一篇列出好几行，读者以为它出现在好几个视频里。
    same = [i for i, o in enumerate(occ)
            if o.get("task_id") == here["task_id"]
            and int(o.get("part_index") or 0) == here["part_index"]]
    if not same:
        occ.append(here)
    elif occ[same[0]].get("sec") is None and here.get("sec") is not None:
        # 原来那条没有锚点、这次对上了：换成能跳的那条
        occ[same[0]] = here
    _write(path, render_concept_note(name_used, existing["first"] or fmt_date(),
                                    define, occ, host))
    return str(path), False


# --------------------------------------------------------------------------- #
# INDEX
# --------------------------------------------------------------------------- #

def _first_sec(c: dict):
    """这个概念第一次被讲到的秒数；碰不到时间戳（必剪那种篇）就给 None。

    取 occ 里的第一条：追加是按时间先后，第一条就是最早那次。同一篇里两条
    出处按「任务 + 分P」去重，所以这里拿到的一定是某一篇的段首。
    """
    for o in c.get("occ") or []:
        if o.get("sec") is not None:
            try:
                return int(o["sec"])
            except (TypeError, ValueError):
                return None
    return None


def _sorted(videos: list[dict], concepts: list[dict]) -> tuple:
    """视频按日期倒序；概念先按日期倒序，同一天的按**第一次讲到的先后**。

    `load_videos` / `load_concepts` 按文件名排是为了「读过一次就固定」，但那是
    文件系统顺序（`带时间` 排在 `必剪` 前面，取决于码位），不是人想要的顺序。
    这一页是给人翻的，新的在上。

    概念单按日期排等于没排：同一天存的一篇视频里，六个概念日期全一样，于是
    退回按名字的码位序——「苍穹、工程化、增删、后端、Java Web、Agent」，中文
    和拉丁字母混着来，和它们在视频里出现的顺序毫无关系。按秒数排，翻概念表就
    是顺着那一篇讲的顺序读下去。
    """
    videos = sorted(videos, key=lambda x: (x.get("date") or "", x.get("title") or ""),
                    reverse=True)
    # 两趟稳定排序：先按「出现的先后」定同一批里的次序，再按日期分档。
    # 反过来做（先日期后秒数）会把日期这一层也排乱。
    concepts = sorted(concepts,
                      key=lambda x: (_first_sec(x) is None,
                                     _first_sec(x) or 0,
                                     x.get("name") or ""))
    concepts = sorted(concepts, key=lambda x: x.get("first") or "", reverse=True)
    return videos, concepts


def rebuild_index(root: Path, host: str = "") -> Path:
    """重写 ``notes/INDEX.md``：先视频、后概念，只做目录。

    每次从磁盘上的文件重建，而不是增量维护。增量要记住「上一次写了什么」，
    而用户随时会手删一个文件、改一个标题；重建是幂等的，永远和目录里的实情一致。
    """
    videos, concepts = _sorted(load_videos(root), load_concepts(root))
    out = ["# 笔记库", "",
           "最后更新：%s　　视频 %d 篇　　概念 %d 个"
           % (fmt_time(), len(videos), len(concepts)), ""]

    out += ["## 视频笔记", ""]
    if not videos:
        out.append("_还没有。在阅读页点「笔记 → 存入笔记库」。_")
    for v in videos:
        bits = [v["title"]]
        if v["bvid"]:
            bits.append(v["bvid"])
        if v["label"]:
            bits.append(v["label"])
        if v["date"]:
            bits.append(v["date"])
        out.append("- " + _md_link(" · ".join(bits), v["file"]))
    out.append("")

    out += ["## 概念笔记", ""]
    if not concepts:
        out.append("_还没有。概念是跟着视频笔记一起写进来的。_")
    for c in concepts:
        # 概念名写成 wikilink：Obsidian 按文件名在全库解析，放哪个子目录都找得到。
        # 不写成 `[[名]](路径)` —— 那是「wikilink 后面跟一段括号」，两边都不算链接。
        # 文件名被 `_safe` 改过的（`TCP/IP` → `TCP_IP`）带别名写，免得指向空气。
        out.append("- %s · 出现 %d 次 · 首次 %s"
                   % (_wikilink(c["name"], c.get("stem") or ""),
                      max(1, len(c["occ"])), c["first"] or "—"))
    out.append("")
    path = root / INDEX_NAME
    _write(path, "\n".join(out))
    return path


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #

def search(root: Path, q: str) -> dict:
    """在笔记里做关键词匹配：标题、概念名、正文都算。

    只做**字符串包含**（NFKC + 小写），不做分词、不做语义。理由：笔记是中文
    短句，分词要靠词典，而词典是第三方依赖；字符串包含在这个体量上足够用，
    而且「搜什么就命中什么」对用户是可预测的——语义检索给不出这种确定感。

    返回两组：`videos`（命中的视频笔记，带命中原因）和 `concepts`（命中的
    概念笔记）。按视频分组由笔记页做——它知道怎么把概念挂到视频标题下面。

    空查询返回全部（没有 `hits`）：接口自己得站得住，不能靠「调用方一定先判
    空」这种约定——`/api/notes/search?q=` 被直接打开时该看到库，不是一片空白。
    """
    key = concept_key(q or "")
    all_videos, all_concepts = _sorted(load_videos(root), load_concepts(root))
    if not key:
        return {"q": "", "videos": all_videos, "concepts": all_concepts}

    videos = []
    for v in all_videos:
        reasons = []
        if key in concept_key(v["title"]):
            reasons.append("标题")
        if key in concept_key(v["bvid"]):
            reasons.append("BV 号")
        if key in concept_key(v["body"]):
            reasons.append("正文")
        if reasons:
            videos.append(dict(v, hits=reasons))

    concepts = []
    for c in all_concepts:
        reasons = []
        if key in concept_key(c["name"]):
            reasons.append("概念名")
        if key in concept_key(c["define"]):
            reasons.append("定义")
        if reasons:
            concepts.append(dict(c, hits=reasons))

    return {"q": q, "videos": videos, "concepts": concepts}


def list_all(root: Path, transcripts=None) -> dict:
    """笔记页要的全部数据：视频、概念，以及每篇是否「来源已更新」。

    `transcripts` 是一个回调 ``task_id -> {part_name: part}``，由 app 层提供
    （它才拿得到任务和磁盘）。不给就跳过过期判断——单测里不需要真任务。
    """
    videos = load_videos(root)
    for v in videos:
        v["stale"], v["stale_why"] = False, ""
        if transcripts and v["task_id"]:
            parts = transcripts(v["task_id"]) or {}
            part = parts.get(v["part_name"])
            if part is None:
                v["stale"] = True
                v["stale_why"] = "来源任务里找不到这一篇了（被删、或产物被清理）"
            elif part_sig(part) != (v["sig"] or ""):
                v["stale"] = True
                v["stale_why"] = "字幕变过了，这份笔记的出处已经对不上现在的正文"
    videos, concepts = _sorted(videos, load_concepts(root))
    return {"videos": videos, "concepts": concepts,
            "index": INDEX_NAME, "dir": str(root)}
