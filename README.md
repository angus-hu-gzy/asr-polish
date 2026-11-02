# asr-polish

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)
[![Zero deps](https://img.shields.io/badge/dependencies-0-brightgreen)]()
[![Author](https://img.shields.io/badge/author-angus--hu--gzy-555555)](https://github.com/angus-hu-gzy)

中文语音转写（ASR）**质量打磨**工具 —— 把「转写出来的文字」变得可信，也能直接**从 B站视频取文本**。

核心能力纯 Python 标准库实现、**零第三方依赖**；仅「从 B站取文本」在无字幕场景需要可选依赖 `yt-dlp`。

## 为什么需要它

语音转文字（ASR）在中文技术内容上有三个顽固问题，而现有工具都只做到「取到文本」这一步，**没人管取到的文本准不准**：

1. **术语被转成同音错字** —— `SQL` → `色考`、`Python` → `PASS`、`JSON` → `接生`、`Flask` → `福拉斯克`
2. **多引擎结果互相矛盾** —— 云端 ASR 和本地 whisper 各说各话，不知道该信谁
3. **转写完不知道质量** —— 不敢直接拿去总结，担心错字误导结论

`asr-polish` 补上这一层，提供下面四个能力，外加一个批量抓字幕的入口：

## 核心能力

### ① 术语订正 `correction`

基于可扩展的词表，自动把 ASR 的同音/形近错字改回正确术语。

```python
from asr_polish import correct_text

raw = "我们今天讲色考和PASS，还有福拉斯克框架。"
result = correct_text(raw)
print(result.text)       # 我们今天讲SQL和Python，还有Flask框架。
print(result.summary())  # 订正 3 处：色考 → SQL；PASS → Python；福拉斯克 → Flask
```

- **长词优先**：`Aging Decoding` 整体订正为 `Agentic Coding`，不会被子串抢先误替换
- **英文变体加词边界**：`PASS`（全大写）命中 `Python`，但英文常用词 `pass`、`compass` 不受影响（订正与打分用同一套判定）
- **变体级置信分级**：置信度标在「变体」上，而非整条词目。规则默认 ASCII 变体 high、中文变体 medium；**≤2 字的中文变体即使条目写了 `confidence: high` 也按 medium 处理**，确认安全的必须用 `high_variants` 逐个升级（两字中文与日常词碰撞率太高，`全占`/`给特`/`色口` 都会出现在正常句子里）。medium 变体（如 `一部`→`异步`）需要上下文佐证才订正，`我们看一部电影` 不会被误改成 `我们看异步电影`
- **文档级主题证据**：中文同音错（`酸法`/`算法`、`还存`/`缓存`）在 ±8 字内根本分不开，于是再给一条证据——**整篇在讲什么**。一篇讲算法的字幕里「算法」出现几十次、「酸法」只出现一两次，那几次几乎必然是转写错误；反过来讲腌菜的视频里「算法」不出现，就不会改。判定要求「正确写法 ≥2 次，且 ≥2×(变体次数+1)」。这条只在输入是整篇文本时生效（`correct_text(整篇)` / `correct 文件`），传单句时自动退化为纯上下文判定。1.03M 字真实 B 站 AI 字幕实测：medium 变体召回 2.3% → 72%，干净语料误伤 0。
**但这个数字要按正确口径读**：它测的是「把本来就写对的术语替换成变体，能不能改回来」，属于人造扰动下的上限。真实字幕是**系统性偏置**——同一个词被一直写错，正确写法在文里根本不出现，文档证据因此失效。用人工字幕做参照实测（`tools/eval_against_human.py`）：5 个视频 46,218 字，订正器改动 **0 处**。见下文「已知边界」。
- **上下文分强弱**：`strong_context`（如「虚拟机」「序列化」）命中 1 个即可，`context` 弱词需命中 ≥2 个——`我们讲爪哇的虚拟机` 能订正为 Java，而 `杰森·斯坦森主演的动作片` 不会误判为 JSON
- **邻接保护 `guards` / `left_guards`**：给「会被更长正常词包含」的变体加负向前瞻（右邻）或负向后顾（左邻）。如 `全占(?!满|领|了)`、`给特(?!写|别)`、`买色口(?!红|腔)`，以及 `PASS` 前有空白时不订正（`6/6 PASS`、`全部 PASS` 是测试日志不是 Python）。注意它只覆盖词表里已登记的搭配，不是通用分词，未登记的新搭配仍可能误伤
- **每处订正可追溯**：返回原文、位置、类别、置信度；`summary()` 会给 medium 改动标注「待确认」，方便只审这类改动

### ② 多引擎交叉验证 `crosscheck`

对同一段音频的多个转写结果做 token 级序列对齐，输出片段级置信度。

```python
from asr_polish import cross_check, confidence_report

cloud = "今天讲SQL和Python，还有Flask框架。"
local = "今天讲色考和PASS，还有福拉斯克框架。"
result = cross_check(cloud, local)
print(confidence_report(result))
# 字符一致率：71%
# 片段总数：7，低置信片段：3 个
# [低置信] (1/2 引擎一致) SQL
# [低置信] (1/2 引擎一致) Python
# [低置信] (1/2 引擎一致) Flask
```

> 上面的「字符一致率」是**按字符加权的片段平均一致度**（衡量多引擎有多一致），不是「71% 内容是对的」——这个例子里三个术语其实全错，只是各引擎各说各话。

- 多个引擎一致 → 高置信；互相矛盾 → 低置信
- 输出 `low_confidence_segments` / `conflict_segments`，供总结时对不确定片段谨慎处理

### ③ 转写质量自评 `quality`

四维打分，一眼看出这段文字能不能信。

```python
from asr_polish import assess

report = assess(text, duration_seconds=1316)
print(report.summary())
# 转写质量评分：96 / 100
#   乱码检测: 0.00% → 100分
#   可疑同音字: 1.00处 → 85分
#   ...
```

四个维度（全部为负向/中性信号）：乱码检测（硬伤）、重复片段、可疑同音字、字符密度。乱码或重复严重时**一票封顶**，总分不会及格的假象。

> 分数只用于「同一段内容的横向对比」（如对比两个引擎对同一音频的转写），不可跨内容比较。

### ④ 从 B站视频取文本 `fetch`

一条命令，直接从 B站视频链接拿到可用的转写文本（字幕轨优先，无字幕走必剪云端 ASR），并自动接上订正 + 打分。

```python
from asr_polish import fetch

result = fetch("https://www.bilibili.com/video/BVxxxxxxxxxx")
print(result.title)   # 视频标题
print(result.source)  # "字幕轨" 或 "必剪云端ASR"
print(result.text)    # 转写文本（可直接接 correct_text / assess）
```

- **字幕轨优先**：有字幕直接取（纯标准库、秒级），无字幕自动切必剪云端 ASR（约 13 秒）
- **有 Cookie 能取到 AI 字幕**：传入 cookies.txt 路径可多取到 B站 AI 字幕轨
- **隐私**：无字幕时音频会上传至 B站服务器（公开视频音轨，风险低）

### ⑤ 批量抓字幕 `subs`

把字幕**取下来**，落到文件里。这条路径不承诺修正内容——它的目标是完整、快速、可批量。

```bash
# 单个视频，一次导出四种格式
asr-polish subs BV1xx411c7mD -f txt,srt,json,md -o ./subs

# 多 P 视频只取其中三集
asr-polish subs BV1xx411c7mD --pages 1,2,3

# 某个 UP 主最新的 20 条投稿
asr-polish subs --up 329954990 --limit 20 -j 3

# 某个合集/系列
asr-polish subs --season 329954990 12345
```

输出（`-f txt`）：

```
subs/
├── BV1RXx169Exu-P1-【前言】Python从零入门到精通系统学习大纲.txt
├── BV1RXx169Exu-P2-【基础篇】Python解释器的安装.txt
└── BV1RXx169Exu-P3-【基础篇】PyCharm编辑器的安装.txt
```

- **四种格式**：`txt` 纯文本、`srt` 带时间轴字幕、`json` 结构化（含每句起止时间）、`md` 带标题/UP主/来源的笔记
- **多 P 命名**：自动带上 `P{n}` 和分 P 标题，不会因为总标题太长而一堆文件重名
- **默认不走 ASR**：无字幕就直接记为「无字幕」跳过，不会给每个视频白传一遍音频（需要兜底时加 `--asr`）
- **默认不订正**：抓下来的是字幕原文，保持忠实；要顺手做术语订正加 `--correct`
- **并发可调**：`-j 3` 默认，过高容易触发风控

`fetch` 与 `subs` 的分工：`fetch` 是「取一篇到 stdout，接订正和打分」；`subs` 是「按批取下来，落成文件」。

#### 必须配 Cookie

B站只向**登录态**下发字幕列表——游客请求 `player/wbi/v2` 拿到的是空数组，不是报错，很容易误判成「这个视频没字幕」。

把浏览器导出的 `cookies.txt`（Netscape 格式）放到以下任一位置即可自动加载：

```
~/.workbuddy/data/bilibili-video-summary/bilibili_cookies.txt
~/Downloads/www.bilibili.com_cookies.txt
~/.config/bili-subs/cookies.txt
```

或用 `--cookie /path/to/cookies.txt` 指定。

## 实测效果

用一段 22 分钟的真实技术视频，对比「本地 faster-whisper」与「必剪云端 ASR」两份转写：

| 指标 | 本地 whisper | 必剪云端 |
|---|---|---|
| 术语订正命中 | 3 处 | 0 处 |
| 质量评分 | **81 分** | **96 分** |
| 交叉验证字符一致率 | 96% | |

质量评分客观地区分出了两个引擎的真实差距——本地 whisper 的错误变体（可疑同音字）明显更多。这正是「转写完不知道哪个准」的解法。

## 安装

核心能力零第三方依赖，Python 3.9+ 即可。

```bash
pip install -e .              # 只装核心（订正 / 交叉验证 / 打分）
pip install -e .[bilibili]    # 额外支持从 B站视频取文本（引入 yt-dlp）
```

> 当前尚未发布 PyPI，请用上面的源码安装方式；发布后即可 `pip install asr-polish`（或 `asr-polish[bilibili]`）。

装好后即可用命令行工具 `asr-polish`：

```bash
# 术语订正
asr-polish correct "今天讲色考和PASS，用福拉斯克搭微服务，讲h t t p和k8s"

# 质量打分（--duration 传音频秒数，用于计算字符密度）
asr-polish assess "转写文本" --duration 1316

# 交叉验证两个转写文本文件
asr-polish crosscheck cloud.txt local.txt

# 从 B站视频取文本（字幕轨优先，无字幕走必剪云端 ASR）
asr-polish fetch "https://www.bilibili.com/video/BVxxxxxxxxxx"
asr-polish fetch BVxxxxxxxxxx --out transcript.txt   # 文本存文件

# 批量抓字幕到文件（txt / srt / json / md）
asr-polish subs BVxxxxxxxxxx -f txt,srt -o ./subs
asr-polish subs --up 329954990 --limit 20 -j 3       # 某 UP 主最新 20 条

# 词表管理
asr-polish lexicon list                    # 列出词表
asr-polish lexicon import my_words.txt     # 批量导入
asr-polish lexicon export --out out.json   # 导出
```

> `fetch` 文本走 stdout、元信息与评分走 stderr，方便 `asr-polish fetch <链接> > text.txt` 直接存文本。

## 快速开始

```bash
# 直接运行示例（无需安装）
python examples/demo.py

# 运行测试（两个测试文件需分别执行）
python tests/test_basic.py
python tests/test_lexicon_io.py

# 用语料审计词表误伤（开发工具，不随包安装）
python tools/audit_corrections.py 语料.txt 或/语料目录
```

## 自定义词表

词表位于 `asr_polish/data/corrections.json`，结构如下：

```json
{
  "entries": [
    { "correct": "SQL", "category": "编程语言",
      "confidence": "high",
      "variants": ["色考", "色口", "seq l"],
      "guards": {"色口": "红"}, "note": "结构化查询语言" },
    { "correct": "异步", "category": "技术术语",
      "high_variants": ["易步"],
      "variants": ["一部", "易步"],
      "strong_context": ["协程", "非阻塞"],
      "context": ["线程", "协程", "回调", "并发"], "note": "Async" }
  ]
}
```

字段说明：
- `confidence`：整体指定 `high`/`medium`；中文变体默认 medium（安全优先），ASCII 变体默认 high。**≤2 字中文变体不受条目级 `high` 影响**，需在 `high_variants` 里逐个声明。
- `high_variants`：把确认「不会独立成词」的安全中文变体升级为 high（无条件订正），如 `色考`、`易步`。
- `strong_context`：medium 变体的强上下文词，±8 字内命中 **1 个**即订正（如「虚拟机」对「爪哇」）。
- `context`：medium 变体的弱上下文词，±8 字内需命中 **≥2 个**才订正。
- `ordinary_variants`：登记「本身就是正常中文词」的变体（如 `一部`、`爱的`），对它们**禁用文档级主题证据**，只认 ±8 字局部上下文——`一部` 在任何一篇里都可能是量词，同篇「异步」再高频也不能翻案。该登记哪些要在真实语料上量（看这个变体出现在多少篇里），工具：`python tools/audit_variants.py 语料目录`。
- `guards`：`{变体: 正则}`，给变体加负向前瞻做右邻保护——如 `"色口": "红|腔"` 让「买色口红色号」「买色口腔」不被误判。
- `left_guards`：`{变体: 正则}`，加负向后顾做左邻保护——如 `"PASS": "\\s"` 让「6/6 PASS」「全部 PASS」不被改成 Python。lookbehind 必须定长，变长正则会告警后丢弃。
- 列表字段与 `guards` / `left_guards` 都会在 `lexicon import` 时透传并做正则预校验，非法 guard 会告警后丢弃。

按你的领域增删条目即可，也支持加载外部词表：

```python
from asr_polish import Lexicon, correct_text

lexicon = Lexicon("/path/to/my_corrections.json")
result = correct_text(text, lexicon)
```

### 批量导入（不用手写 JSON）

手写 JSON 既慢又容易格式出错。`lexicon import` 支持三种人类友好的格式，自动识别：

**TXT**（每行一条，`#` 注释，分隔符支持 `=` / `->` / `:`）：

```
# 医疗影像领域
DICOM = 迪康, 迪克姆, d i c o m
PACS = 帕克斯, p a c s
```

**CSV**（表头 `correct,category,variants,note`，变体用 `|` 或 `;` 分隔）：

```csv
correct,category,variants,note
Vue,框架工具,"v u e|维优",前端框架
```

**JSON**（标准词表结构或其 `entries` 数组）：

```json
[{"correct": "K8s", "variants": ["k 8 s", "库伯内特斯"]}]
```

导入时按 `correct` 去重合并：已存在的术语自动合并变体，新术语追加，并保留词表的名称/描述等元信息。

```bash
asr-polish lexicon import examples/import_sample.txt   # 写入用户词表（~/.config/asr-polish/lexicon.json）
asr-polish lexicon import my.csv --out my_lexicon.json # 导入到指定文件
```

> 完整示例见 `examples/import_sample.txt` 和 `examples/import_sample.csv`。

## 目录结构

```
asr-polish/
├── pyproject.toml         # 打包配置（pip install 可装，提供 asr-polish 命令）
├── LICENSE                # MIT
├── NOTICE                 # 第三方声明（bcut-asr 参考）
├── asr_polish/
│   ├── __init__.py        # 统一入口
│   ├── __main__.py        # 支持 python -m asr_polish
│   ├── py.typed           # PEP 561 类型标记
│   ├── cli.py             # 命令行入口
│   ├── correction.py      # 术语订正引擎
│   ├── crosscheck.py      # 多引擎交叉验证
│   ├── quality.py         # 转写质量自评
│   ├── bili.py            # B站视频取文本（字幕轨 → 必剪云端 ASR）
│   ├── lexicon.py         # 词表加载
│   ├── lexicon_io.py      # 词表批量导入/导出
│   └── data/
│       └── corrections.json   # 默认术语词表
├── examples/
│   ├── demo.py            # 演示
│   ├── import_sample.txt  # 批量导入示例（TXT）
│   └── import_sample.csv  # 批量导入示例（CSV）
├── tools/
│   └── audit_corrections.py  # 语料审计：暴露「变体只是更长正常词的一部分」的误伤
└── tests/
    ├── test_basic.py      # 核心能力 + 误伤回归 + medium 用例测试
    └── test_lexicon_io.py # 词表导入/导出测试
```

## 变更记录

- **0.5.4 → 0.5.5**：中文同音错终于能订正了。新增**文档级主题证据**：medium 变体除了 ±8 字上下文，还可以靠「整篇的正确写法出现得够多」来判定（要求 ≥2 次且 ≥2×(变体次数+1)）。1.03M 字真实 B 站 AI 字幕上（人造扰入口径，非真实覆盖率），medium 变体召回 **2.3% → 72%**，整体召回 76.7% → 93.0%，干净语料误伤仍为 0；自然语料实测 103 万字新增 7 处改动，全部是真错（`刀客`×3、`多克`×4 → Docker）。配套：`ordinary_variants` 字段（`一部`/`爱的` 这类正常词禁止靠主题翻案）、`tools/audit_variants.py`（在语料上量出哪些变体其实是常用词）、词表重复键检测（`guards` 写两遍会被静默覆盖，`兵法` 的保护就是这样丢的）、`兵法书`/`蒙提派森` 搭配保护。`benchmark_recall.py` 新增 `--window 0`（整篇输入，对得上 CLI 的真实调用方式）。测试 64 → 71。
- **0.5.1 → 0.5.2**：新增 `left_guards`（左邻负向后顾），修掉 `6/6 PASS`、`全部 PASS`、`80% PASS` 被改成 Python 的问题，同时中文紧邻的 `学PASS语言`、`PASS的缩进规则` 仍正常订正；`correct` 与 `assess` 共用同一套左邻判定，不会出现「订正不动、评分却扣分」。`left_guards` 与 `guards` 一样在导入时透传并做正则预校验（lookbehind 必须定长，非法即告警丢弃）。测试 62 → 64，词表测试 11 → 13。
- **0.5.0 → 0.5.1**：`lexicon import`/合并透传 `guards` 与 `strong_context`（此前会被静默丢弃，导致导入后保护失效）；词表加载时预校验 `guards` 正则（非法即告警丢弃，不再运行时崩在 `correct()`）、列表字段容错；≤2 字中文变体不再被条目级 `confidence: high` 顺带升级（`全占`/`给特`/`色口`/`推里` 改回 medium 并补 guards 与上下文，`完全占领`、`给特别`、`买色口腔` 不再误伤）；`assess` 对 medium 变体改用与订正引擎相同的上下文判定，技术语境里的 medium 级错别字重新计入评分。新增 `tools/audit_corrections.py` 用语料审计误伤，按 11M 字符语料又修掉一批：`病发`（疾病发生）、`吉特`（林吉特）、`鲁斯特`（普鲁斯特）、`PASS/FAIL`、`a i18next`（ASCII 词边界现在连数字一起挡）；补了 `bili` 纯函数测试，测试数 46 → 62。
- **0.4.0 → 0.5.0**：修复 CLI `--out` help 中 `%APPDATA%` 未转义导致 argparse 崩溃的 P0 回归（该版本 CLI 全不可用，请直接升到本版）；`quality` 与 `correction` 统一匹配逻辑（英文词边界 + 长词优先去重），`pass`/`compass` 不再被误标、重叠变体不再双重计数；medium 上下文分强弱（`strong_context` 命中 1 个即可，`context` 弱词需 ≥2）；新增 `guards` 邻接保护；`Correction` 增加 `confidence` 字段并在 `summary()` 标注 medium 改动；`cross_check` 剔除空引擎；CI 补充 editable 安装冒烟、wheel 内容校验、Windows job 与 ruff。
- **0.3.0 → 0.4.0**：中文变体默认改为 `medium`（安全优先），引入 `high_variants` 白名单与 `context` 窄词表上下文；`USER_LEXICON_PATH` 改为平台感知（Windows `%APPDATA%`，POSIX 尊重 `XDG_CONFIG_HOME`）；交叉验证报告由「整体置信度」更名为「字符一致率」，两引擎冲突（0.5）现计入低置信片段。
- **0.2.0 → 0.3.0**：`CrossCheckResult.merged` 由 dataclass 字段改为只读属性（等价于 `base_text`），直接以 `CrossCheckResult(merged=...)` 构造的调用会报 `TypeError`，请改用 `base_text=`。

## 许可证

[MIT](./LICENSE) · 作者 [angus-hu-gzy](https://github.com/angus-hu-gzy)

## 诚实说明（边界）

- **订正基于词表，不是万能**。它能抓词表里收录的错误；词表没覆盖的错字抓不到。因此每处订正都**留痕可回溯**（含置信度），重要场景请人工核对，尤其 `summary()` 标注「待确认」的 medium 改动。为降低误伤，英文变体加了词边界、中文变体默认 medium（需 ±8 字内强上下文命中 1 个或弱上下文命中 ≥2 个才订正）、易被正常词包含的变体另配 `guards` 邻接保护，「不可能独立成词」的安全变体经 `high_variants` 白名单升级，但仍可能有个别误判。
### 真实字幕上的实测（这条最重要）

上面的召回率是**人造扰入口径**：把文中本来就写对的术语替换成变体，再测能否改回来。
它回答「给定这个错法能不能改对」，**不等于真实字幕上的覆盖率**。

拿「同一视频的人工字幕 vs AI 字幕」当参照系测才是真值（`tools/eval_against_human.py`）。
13 个同时有两条轨的视频，剔除人工字幕与 AI 内容不成比例的 4 个，剩 9 个 / 62,231 字：

| 指标 | 实测 |
|---|---|
| 字错率（含大小写、数字风格差异） | **8.47%** |
| 真实错误（中文同音 + 英文错拼 + 漏句） | **2.35%** |
| 其中「AI 整句漏掉」占 | **47%** |
| 中文标点密度 | 103 万字里 **14 处** |
| 本工具净修正 | **0 字** |

三个必须承认的结论：

1. **预先枚举变体不可扩展**。B站把 `Git` 写成「盖特」、`Docker` 写成「多克/刀客」——
   同一术语的音译写法是开放集合，词表永远收不全。13 个视频里一处都没触发，正是因为变体没收录。
2. **词表方向错了**。真实错误主体是**中文技术术语的同音错**：`基类→鸡肋`（单个视频里 50 次）、
   `友元→有缘`、`遍历→便利`、`键值对→剑指队`。此前收的却是英文术语的中文音译。
3. **最大的问题不是错字**。中文同音错里 170/290 是单字（`它/他`、`作/做`、`再/在`），
   歧义大到人类校对标准都不统一；再加上整句漏失和零标点，这些**规则方案处理不了**。

对照实验：同一段 131 字字幕，规则订正器 0 处改动（CER 3.8% 原样），
语言模型修正后 CER 0%。

所以本项目的定位是**高置信、可解释、可离线的那一小块**（术语与专名），
而不是「字幕纠错」。要覆盖标点、同音词、漏句，需要语言模型。

- **交叉验证基于 token 级对齐**。它衡量「多引擎是否一致」，而非「是否正确」——两个引擎可能同时错成同一个词。它回答的是「这段可不可信」，不是「这段绝对正确」。
- **质量评分是启发式的**，用于同一段内容的横向对比和快速筛查，不是绝对标准。
- **fetch 依赖外部服务**。字幕轨来自 B站 API（可能因风控或视频设置拿不到）；无字幕时的必剪 ASR 需要 `yt-dlp` 下载音频并把音轨上传至 B站服务器。二者任一失败都会给出明确报错，而非静默返回空文本。
