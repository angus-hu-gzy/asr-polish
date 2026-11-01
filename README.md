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

`asr-polish` 补上这一层，提供三个能力：

## 三个核心能力

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
- **英文变体大小写不敏感**：`PASS` / `Pass` / `pass` 都命中 `Python`
- **每处订正可追溯**：返回原文、位置、类别，方便人工核对与回滚

### ② 多引擎交叉验证 `crosscheck`

对同一段音频的多个转写结果做字符级序列对齐，输出片段级置信度。

```python
from asr_polish import cross_check, confidence_report

cloud = "今天讲SQL和Python，还有Flask框架。"
local = "今天讲色考和PASS，还有福拉斯克框架。"
result = cross_check(cloud, local)
print(confidence_report(result))
# 整体置信度：80%
# [有分歧] (1/2 引擎一致) SQL
# [有分歧] (1/2 引擎一致) ython
```

- 多个引擎一致 → 高置信；互相矛盾 → 低置信
- 输出 `low_confidence_segments` / `conflict_segments`，供总结时对不确定片段谨慎处理

### ③ 转写质量自评 `quality`

五维打分，一眼看出这段文字能不能信。

```python
from asr_polish import assess

report = assess(text, duration_seconds=1316)
print(report.summary())
# 转写质量评分：97 / 100
#   乱码检测: 0.00% → 100分
#   可疑同音字: 1.00处 → 85分
#   ...
```

五个维度：乱码检测（硬伤）、重复片段、可疑同音字、术语可识别度、字符密度。乱码或重复严重时**一票封顶**，总分不会及格的假象。

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

## 实测效果

用一段 22 分钟的真实技术视频，对比「本地 faster-whisper」与「必剪云端 ASR」两份转写：

| 指标 | 本地 whisper | 必剪云端 |
|---|---|---|
| 术语订正命中 | 5 处（`接生→JSON`、`PASS→Python`…） | 1 处 |
| 质量评分 | **85 分** | **97 分** |
| 交叉验证整体置信度 | 96% | |

质量评分客观地区分出了两个引擎的真实差距——这正是「转写完不知道哪个准」的解法。

## 安装

核心能力零第三方依赖，Python 3.9+ 即可。

```bash
pip install -e .              # 只装核心（订正 / 交叉验证 / 打分）
pip install -e .[bilibili]    # 额外支持从 B站视频取文本（引入 yt-dlp）
```

发布到 PyPI 后可直接 `pip install asr-polish`（或 `asr-polish[bilibili]`）。

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

# 运行测试
python tests/test_basic.py tests/test_lexicon_io.py
```

## 自定义词表

词表位于 `asr_polish/data/corrections.json`，结构如下：

```json
{
  "entries": [
    { "correct": "SQL", "category": "编程语言",
      "variants": ["色考", "色口", "seq l"], "note": "结构化查询语言" }
  ]
}
```

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
asr-polish lexicon import examples/import_sample.txt   # 写入内置词表
asr-polish lexicon import my.csv --out my_lexicon.json # 导入到新文件
```

> 完整示例见 `examples/import_sample.txt` 和 `examples/import_sample.csv`。

## 目录结构

```
asr-polish/
├── pyproject.toml         # 打包配置（pip install 可装，提供 asr-polish 命令）
├── asr_polish/
│   ├── __init__.py        # 统一入口
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
└── tests/
    ├── test_basic.py      # 三个核心能力测试
    └── test_lexicon_io.py # 词表导入/导出测试
```

## 许可证

[MIT](./LICENSE) · 作者 [angus-hu-gzy](https://github.com/angus-hu-gzy)

## 诚实说明（边界）

- **订正基于词表，不是万能**。它能抓词表里收录的错误；词表没覆盖的错字抓不到，且存在误伤风险（如医疗内容里的「接生」会被误判为 `JSON`）。因此每处订正都**留痕可回溯**，重要场景请人工核对。
- **交叉验证基于字符级对齐**。它衡量「多引擎是否一致」，而非「是否正确」——两个引擎可能同时错成同一个词。它回答的是「这段可不可信」，不是「这段绝对正确」。
- **质量评分是启发式的**，用于横向对比和快速筛查，不是绝对标准。
- **fetch 依赖外部服务**。字幕轨来自 B站 API（可能因风控或视频设置拿不到）；无字幕时的必剪 ASR 需要 `yt-dlp` 下载音频并把音轨上传至 B站服务器。二者任一失败都会给出明确报错，而非静默返回空文本。
