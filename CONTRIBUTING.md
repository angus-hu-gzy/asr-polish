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
python -m unittest discover -s tests -q                    # 全部测试
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
3. **等这个提交的 CI 绿了再打标签**：`python tools/ci_guard.py`（0 绿 / 1 红或还在跑 / 2 查不到）
4. `git tag -a vx.y.z -m "..."` ，说明按「新增 / 修复 / 设计取舍」写
5. `git push origin main && git push origin vx.y.z`

CI 的 `tag-check` 会校验「标签名 = `__version__` = CHANGELOG 条目」三者一致，不一致直接红。
这条规则是补出来的：0.5.6 当时只在文档里记了新版本，代码里的 `__version__` 忘了改，
结果那个提交至今无法被正确地打标签。

### 别把红提交打成版本（已经发生过两次）

- **v0.8.0**：标签那轮 CI 红在两个 job 上（Windows 的 cp1252、ubuntu 3.9 的 `#HttpOnly_`），
  没人去 Actions 看，标签就这么打下去了，直到下一版才发现。
- **v0.9.0**：标签指向的提交本身是红的，只能删标签、重指、重推。

两次的原因一样：打标签只看「代码改完了没」，不看「CI 过了没」。而 CI 是异步的，
推上去要等几分钟，人早就去干别的了。

现在有一道闸门：

```bash
git config core.hooksPath tools/hooks     # 装一次，之后推标签自动检查
python tools/ci_guard.py --sha 969f1f3    # 也可以手动查任意提交
```

它只看**标签**，推分支一律放行。判定按提交而不是按「最新一次运行」——同一提交上
可能有两个运行（推 main 一个、推标签一个），v0.8.0 正是 main 绿、标签红，
只看最新一次会漏。确认要硬推时：`ASR_POLISH_SKIP_CI=1 git push origin <tag>`。

## 发到 PyPI

`pip install asr-polish` 走的是 `.github/workflows/publish.yml`：推 `v*` 标签自动发布，
也可以在 Actions 页面手动跑 `Publish` 并指定标签（补发用，不必为了发布去删标签重推）。

认证走 **OIDC 信任发布**，仓库里不存任何 token：workflow 用 `id-token: write` 换一张
短期凭据，PyPI 校验「这个仓库的这条 workflow 在这个环境里跑的」才放行。

### 一次性的 PyPI 侧配置

1. 注册 / 登录 [pypi.org](https://pypi.org)，开启 2FA（PyPI 强制）
2. 打开 [Account settings → Publishing](https://pypi.org/manage/account/publishing/)，在
   「Add a new pending publisher」里填：

   | 字段 | 值 |
   |---|---|
   | PyPI Project Name | `asr-polish` |
   | Owner | `angus-hu-gzy` |
   | Repository name | `asr-polish` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. 保存。之后第一次成功上传时，这个「待认领发布者」会自动转成项目所有者。

### 几条硬约束

- **已发布的版本不能覆盖，只能发新版本。** 发错了 `pip` 用户已经装到手上，PyPI 只允许
  `yank`（把版本标记为不推荐，仍可被精确版本安装）。所以 `twine check` + 轮子自检都放在
  upload 之前，而不是之后。
- **版本号只能往上走。** `0.9.1` 发过一次，再发 `0.9.1` 就是 400。
- workflow 里第一步会查这个版本在 PyPI 上有没有，已存在就整段跳过——手动补跑不会红一片。

### 没有 OIDC 时的手动上传

临时排障可以用 token，别提交到仓库：

```bash
python -m build && python -m twine check dist/*
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-xxxx python -m twine upload dist/*
```

## 边界

这个项目**不做**字幕纠错，只做高置信的术语订正与质量体检。真实 AI 字幕上的实测
（[docs/真实字幕错误分析.md](./docs/真实字幕错误分析.md)）说明：同音字、漏句、标点这些
占错误大头的问题，规则方案够不着。想往那个方向走，需要引入语言模型，那是另一个项目。
