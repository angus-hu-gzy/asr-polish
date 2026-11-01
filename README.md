# asr-polish

中文语音转写（ASR）**质量打磨**工具 —— 把「转写出来的文字」变得可信。

纯 Python 标准库实现，**零第三方依赖**。

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

## 实测效果

用一段 22 分钟的真实技术视频，对比「本地 faster-whisper」与「必剪云端 ASR」两份转写：

| 指标 | 本地 whisper | 必剪云端 |
|---|---|---|
| 术语订正命中 | 5 处（`接生→JSON`、`PASS→Python`…） | 1 处 |
| 质量评分 | **85 分** | **97 分** |
| 交叉验证整体置信度 | 96% | |

质量评分客观地区分出了两个引擎的真实差距——这正是「转写完不知道哪个准」的解法。

## 快速开始

```bash
# 直接运行示例
python examples/demo.py

# 运行测试
python tests/test_basic.py
```

无需安装任何依赖，Python 3.9+ 即可。

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

## 目录结构

```
asr-polish/
├── asr_polish/
│   ├── __init__.py       # 统一入口
│   ├── correction.py     # 术语订正引擎
│   ├── crosscheck.py     # 多引擎交叉验证
│   ├── quality.py        # 转写质量自评
│   ├── lexicon.py        # 词表加载
│   └── data/
│       └── corrections.json   # 默认术语词表
├── examples/demo.py      # 演示
└── tests/test_basic.py   # 单元测试
```

## 许可证

[MIT](./LICENSE)

## 诚实说明（边界）

- **订正基于词表，不是万能**。它能抓词表里收录的错误；词表没覆盖的错字抓不到，且存在误伤风险（如医疗内容里的「接生」会被误判为 `JSON`）。因此每处订正都**留痕可回溯**，重要场景请人工核对。
- **交叉验证基于字符级对齐**。它衡量「多引擎是否一致」，而非「是否正确」——两个引擎可能同时错成同一个词。它回答的是「这段可不可信」，不是「这段绝对正确」。
- **质量评分是启发式的**，用于横向对比和快速筛查，不是绝对标准。
