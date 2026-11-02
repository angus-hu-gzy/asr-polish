# 变更记录

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

每一条实测数字都来自仓库里的脚本，可复现：`tools/benchmark_recall.py`（人造扰动召回）、
`tools/eval_corrections.py`（开发集/留出集门禁）、`tools/eval_against_human.py`（人工字幕真值）。

---

## [0.6.0] - 2026-11-02

从「订正字幕」到「取全字幕」。把此前被纠错能力盖住的另一半显性化：一条命令把 B站字幕完整取下来。

### 新增

- `subs` 子命令：单视频 / 多 P / UP 主投稿 / 合集四个来源，导出 `txt`、`srt`、`json`、`md` 四种格式
  - `--pages 1,2,3|all`、`-j` 并发、`--skip-existing` 增量跳过
  - `--up <mid> [--limit N]`、`--season <mid> <sid>`
- `export.py`：四种渲染器 + 文件名清洗（连续分隔符合并、截断 40 字），多 P 用分 P 标题命名
- `bili.py`：字幕解析保留 `segments`（每句起止时间），此前只留纯文本，SRT 无法生成
- `bili.py`：`expand_pages` 展开多 P；`list_up_videos`（wbi 签名）、`list_season`（免签名）、`extract_mid`
- 16 项导出测试（`tests/test_export.py`），测试总数 71 → 87

### 修复

- **UP 主 / 合集接口必然「风控校验失败」**：只有 `fetch` 自己会找 cookie，而 `_http_json` 走的是不带登录态的
  opener。改为 `_opener` 统一自动查找登录态。
- **必剪兜底路径静默失效**：`model_id=7` 已被服务端废弃（返回 `139201 第三方服务异常`），
  无字幕时 `transcribe_bcut` 会返回 `None` 而不报错。改为 `8` 并提取为常量 `_BCUT_MODEL_ID`。

### 变更

- `fetch` 增加 `allow_asr` 开关；`subs` 默认**不订正**（`--correct` 才开）、默认**不走 ASR**（`--asr` 才开）——
  抓取要忠实原文，不替用户做决定。

---

## [0.6.1] - 2026-11-02

把 0.6.0 之后散落的东西补齐，让「取字幕」这条路能走完整，也让项目的工程面自洽。

### 新增

- `subs --search <关键词> [--search-pages N]`：按关键词搜一批视频再抓字幕
  （`bili.search_videos`，wbi 签名；标题里的 `<em>` 高亮会先清掉再进文件名）
- `tools/collect_corpus.py`：采集 AI 字幕语料，`--keywords` 搜索采集或 `--candidates` 指定清单，
  只收 `lan == ai-zh`（人工轨文本本身是对的，混进来会污染召回统计）、多 P 取最接近 20 分钟那集、
  增量写 `manifest.json`。基准报告里的数字从此可在仓库内复现。
- `CHANGELOG.md`：逐版本变更记录，含每版实测数字；README 的变更记录收敛成摘要表
- `docs/`：两份测量报告入仓（召回基准、真实字幕错误分析）
- CI 新增 `tag-check`：标签名 = `__version__` = `CHANGELOG` 条目，三者不一致直接红

### 修复

- **README 承诺的 Cookie 自动查找路径与代码不符**：文档列了三条，代码只查一条
  （`~/.config/bili-subs/cookies.txt` 从未被搜过）。已补齐 `~/Downloads/www.bilibili.com_cookies.txt`
  与 `~/.config/asr-polish/cookies.txt`。
- **CI 只跑了 2 个测试文件**：`tests/test_export.py` 的 16 项从未进过 CI。改为
  `python -m unittest discover -s tests`，新增测试文件自动纳入。

### 变更

- 测试 87 → 89（新增 `_strip_em`、语料采集选片逻辑）
- CI 新增「装轮子再验一次」：`pip install dist/*.whl` 后在包外目录 import，
  确认词表随包进、入口点可用

---

## [0.5.6] - 2026-11-02 · 未打标签

用人工字幕当真值，把上一版的结论推翻。**这一版没有新增纠错能力，只新增了反对自己的证据。**

> 本版未单独打标签：提交内 `__version__` 仍为 `0.5.5`，只是文档口径记作 0.5.6。
> 与其给一个自报版本号对不上的提交贴标签，不如让它留在 0.5.5 → 0.6.0 的历史里。

### 新增

- `tools/eval_against_human.py`：同一视频的人工字幕 vs AI 字幕逐字对齐算 CER
  （`--list` / `--bvid` / `--human-lan` / `--show` / `--json`）
- 按时间交集对齐两条轨：句数不同、覆盖时长不同的情况都必须先取交集，否则假差异高达 16%

### 实测

13 个双轨视频里剔除 4 个内容不成比例的，剩 9 个 / 62,231 字：

| 指标 | 实测 |
|---|---|
| 字错率 | 8.47% |
| 真实错误（中文同音 + 英文错拼 + 漏句） | 2.35% |
| 其中 AI 整句漏掉 | 47% |
| 中文标点密度 | 103 万字里 14 处 |
| 本工具净修正 | **0 字** |

### 结论（写进 README 的边界）

- 人造扰动假设「错误偶发」；真实 ASR 是**系统性偏置**——同一个词一直错，正确写法全文不出现，
  文档级证据因此失效。0.5.5 的 72% 召回在这个口径下不成立。
- 真实错误里 170/290 是单字（`它/他`、`作/做`、`再/在`），歧义大到人类校对标准都不统一。
- 对照：同一段 131 字，规则订正 0 处改动（CER 3.8%），语言模型修正后 CER 0%。

---

## [0.5.5] - 2026-11-02

中文同音错终于能订正了。

### 新增

- **文档级主题证据**：medium 变体除 ±8 字局部上下文外，还可靠整篇用词分布判定——
  正确写法 ≥2 次 且 ≥2×(变体次数+1)。`+1` 用于挡住 `兵法书` 这类句子。
- `ordinary_variants` 字段：`一部`、`爱的` 这类本身就是正常词的变体，禁止靠主题翻案
- `tools/audit_variants.py`：量出每个变体出现在多少篇里（`一部` 37 篇，其余 ≤6 篇）
- 词表重复键检测（`json.object_pairs_hook`）：`guards` 写两遍会静默覆盖，`兵法` 的保护第一次就这样丢了

### 变更

- `benchmark_recall.py` 新增 `--window 0`（整篇输入），对得上 CLI 的真实调用方式

### 实测（人造扰入口径）

medium 变体召回 2.3% → 72.1%，整体 76.7% → 93.0%，干净语料误伤仍为 0。
自然语料 103 万字新增 7 处改动，全部是真错（`刀客`×3、`多克`×4 → Docker）。

测试 64 → 71。

---

## [0.5.4] - 2026-11-02

先能测量，再谈准确。

### 新增

- `tools/benchmark_recall.py`：把文中本来写对的术语替换成变体，测能否改回
  （`--clean` 数误伤、`--window 0` 整篇输入、`--json`）
- 采集 196 篇 / 102.9 万字真实 B站 AI 字幕作为语料底座（`bench/collect_corpus.py`）

### 修复

- `fetch` 只取多 P 视频第一 P 的缺陷，新增 `--page`

### 实测

整体召回 76.7%，`high` 100%、`medium` 2.3%，精确率 100%。
四个反证实验全部否决：全放开 medium（104→78，56 处误伤）、窗口 ±100、弱词阈值 2→1、技术词密度。

其中「弱词阈值 2→1」在语料上测不出代价，却被单元测试否掉（`我们讲一部和并发` → `异步`）。
**语料上测不出代价 ≠ 没代价。**

---

## [0.5.3] - 2026-11-02

### 新增

- `eval/cases.jsonl`（开发集）、`eval/holdout.jsonl`（留出集，不参与调参）
- `tools/eval_corrections.py`：精确率 / 召回率门禁
- CI 新增 eval job：开发集 ≥0.98 / 0.95，留出集 ≥0.98 / 0.90，退化即红

此前所有「改对了」都只是肉眼看上去对，从本版起每次改动都要过门禁。

---

## [0.5.2] - 2026-11-02

### 新增

- `left_guards`（左邻负向后顾）

### 修复

- `6/6 PASS`、`全部 PASS`、`80% PASS` 被改成 `Python`；同时中文紧邻的 `学PASS语言`、
  `PASS的缩进规则` 仍正常订正
- `correct` 与 `assess` 共用同一套左邻判定，不会出现「订正没动、评分却扣分」

`left_guards` 与 `guards` 一样在导入时透传并做正则预校验（lookbehind 必须定长，非法即告警丢弃）。

测试 62 → 64，词表测试 11 → 13。

---

## [0.5.1] - 2026-11-02

### 修复

- `lexicon import` / 合并时透传 `guards` 与 `strong_context`（此前被静默丢弃，导入后保护全失效）
- 加载时预校验 `guards` 正则，不再等运行到 `correct()` 才崩；列表字段容错
- ≤2 字中文变体不再被条目级 `confidence: high` 顺带升级（`全占`/`给特`/`色口`/`推里` 改回 medium）

### 新增

- `assess` 对 medium 变体改用与订正引擎相同的上下文判定
- `tools/audit_corrections.py`：在语料上审计误伤

11M 字符语料又修一批误伤：`病发`（疾病发生）、`吉特`（林吉特）、`鲁斯特`（普鲁斯特）、
`PASS/FAIL`、`a i18next`（ASCII 词边界现在连数字一起挡）。测试 46 → 62。

---

## [0.5.0] - 2026-11-01

### 修复

- **P0 回归**：CLI `--out` 帮助文本中 `%APPDATA%` 未转义，argparse 构建时抛
  `ValueError`，导致**该版本 CLI 完全不可用**。0.5.0 之前的版本不受影响，直接升到 0.5.1 及以后。
- `quality` 与 `correction` 统一匹配逻辑（英文词边界 + 长词优先去重），`pass`/`compass` 不再被误标，
  重叠变体不再双重计数
- `cross_check` 剔除空引擎

### 新增

- medium 上下文分强弱：`strong_context` 命中 1 个即可，`context` 弱词需 ≥2
- `guards` 邻接保护
- `Correction.confidence` 字段，`summary()` 标注 medium 改动

### 变更

- CI 补充 editable 安装冒烟、wheel 内容校验、Windows job 与 ruff

---

## [0.4.0] - 2026-11-01

### 变更（含行为变更）

- 中文变体默认改为 `medium`（安全优先），需 ±8 字内上下文命中才订正
- 交叉验证报告由「整体置信度」更名为「字符一致率」；两引擎冲突（0.5）现计入低置信片段
- `USER_LEXICON_PATH` 改为平台感知（Windows `%APPDATA%`，POSIX 尊重 `XDG_CONFIG_HOME`）

### 新增

- `high_variants` 白名单、`context` 窄词表上下文

---

## [0.3.0] - 2026-11-01

### 破坏性变更

- `CrossCheckResult.merged` 由 dataclass 字段改为只读属性（等价于 `base_text`）。
  直接以 `CrossCheckResult(merged=...)` 构造的调用会报 `TypeError`，请改用 `base_text=`。

---

## [0.2.0] - 2026-11-01

### 新增

- `fetch` 子命令：从 B站链接取文本（字幕轨优先，无字幕走必剪云端 ASR）

---

## [0.1.0] - 2026-11-01

首个版本：术语订正、多引擎交叉验证、转写质量自评，词表批量导入/导出、CLI 与 pip 安装支持。

[0.6.1]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.5...v0.6.0
[0.5.6]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.5...4ea8c76
[0.5.5]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.4.0...v0.5.0
