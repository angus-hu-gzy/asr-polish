# 参与贡献

这个项目的核心约束只有一条：**每个「改得更好」的说法都要有数字支撑**。
所以提 PR 时，能带上「改之前 / 改之后」的评测结果就带，没有也能提，但别用体感代替测量。

## 环境

```bash
git clone https://github.com/angus-hu-gzy/asr-polish.git
cd asr-polish
python -m pip install -e ".[dev]"   # 只有 ruff 和 build，运行时零依赖
```

Python 3.9+。核心功能纯标准库；只有「无字幕时的必剪 ASR」需要 `pip install -e ".[bilibili]"`（即 `yt-dlp`）。

## 提交前跑这几条

```bash
python -m unittest discover -s tests -q                    # 全部测试（89 项）
python -m ruff check --select F,E9 .                       # 与 CI 的 lint job 一致
python tools/eval_corrections.py --min-precision 0.98 --min-recall 0.95            # 开发集
python tools/eval_corrections.py --cases eval/holdout.jsonl --min-precision 0.98 --min-recall 0.90  # 留出集
```

CI 会在这四条之外再跑：多 Python 版本矩阵、Windows job、wheel 内容校验、装轮子后 import 冒烟。

## 改词表（`asr_polish/data/corrections.json`）

最容易搞砸的部分，三条规矩：

1. **新加变体先跑审计**：`python tools/audit_variants.py 语料目录`，看它在语料里会不会其实是正常词
   （`一部`、`爱的` 这类要用 `ordinary_variants` 登记，禁用文档级证据）。
2. **搭配保护优先于加词**：`guards`（右邻）/ `left_guards`（左邻）能挡住 `兵法书`、`6/6 PASS`
   这类句子，比把整个变体拉黑更精准。
3. **改完必须过评测**：精确率跌破 0.98 说明引入误伤，召回跌破门禁说明改坏了。

## 加测试

按现有风格写在 `tests/` 下，`unittest` 即可，不需要 pytest。
测试要能用 `python -m unittest discover -s tests` 跑到，别依赖网络与 Cookie。

## 发布流程

版本号是单一真相源：`asr_polish/__init__.py` 里的 `__version__`。

1. 改 `__version__`（语义化版本）
2. 在 `CHANGELOG.md` 顶部加 `## [x.y.z] - 日期`，写清新增 / 修复 / **实测数字**
3. `git tag -a vx.y.z -m "..."` ，说明按「新增 / 修复 / 设计取舍」写
4. `git push origin main && git push origin vx.y.z`

CI 的 `tag-check` 会校验「标签名 = `__version__` = CHANGELOG 条目」三者一致，不一致直接红。
这条规则是补出来的：0.5.6 当时只在文档里记了新版本，代码里的 `__version__` 忘了改，
结果那个提交至今无法被正确地打标签。

## 边界

这个项目**不做**字幕纠错，只做高置信的术语订正与质量体检。真实 AI 字幕上的实测
（[docs/真实字幕错误分析.md](./docs/真实字幕错误分析.md)）说明：同音字、漏句、标点这些
占错误大头的问题，规则方案够不着。想往那个方向走，需要引入语言模型，那是另一个项目。
