# asr-polish

[![CI](https://github.com/angus-hu-gzy/asr-polish/actions/workflows/ci.yml/badge.svg)](https://github.com/angus-hu-gzy/asr-polish/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/asr-polish?label=pypi&color=blue)](https://pypi.org/project/asr-polish/)
[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)
[![Zero deps](https://img.shields.io/badge/dependencies-0-brightgreen)]()
[![Tag](https://img.shields.io/github/v/tag/angus-hu-gzy/asr-polish?label=version&color=blue)](https://github.com/angus-hu-gzy/asr-polish/releases)
[![Author](https://img.shields.io/badge/author-angus--hu--gzy-555555)](https://github.com/angus-hu-gzy)

**把 B站字幕批量取下来，并把抓取做成能排队的本地服务**；取到的文本顺手订正到可信。
纯 Python 标准库实现、**零第三方依赖**；仅「无字幕时走必剪云端 ASR」需要可选依赖 `yt-dlp`。

## 不会装？让 AI 替你装

这个项目自带一份**写给 AI 看的施工图**：[FOR_AI.md](./FOR_AI.md)。

把你常用的 AI（能操作你电脑的那种：WorkBuddy、Claude、Cursor 之类）打开，复制下面这段：

> 帮我装一个工具，再把我给的视频做成笔记库。
> 仓库 https://github.com/angus-hu-gzy/asr-polish ，根目录有一份 FOR_AI.md，
> 读不到就用这个地址打开：https://raw.githubusercontent.com/angus-hu-gzy/asr-polish/main/FOR_AI.md
> 先读它，按它做。每一步动手之前先告诉我你要做什么；不要删我电脑上的文件；
> 装完之后先验收，别急着说完成了。

剩下的事它来做：判断环境、装 Python、装包、问你登录、抓字幕、整理成 `notes/INDEX.md` 加一堆
能读的笔记文件。你只需要在它问的时候回一句「好」。

**前提**：那个 AI 得能碰你的电脑（跑命令、读写文件）。只在网页聊天框里说话的 AI 装不了东西——
它也写不出真结果，只会给你一套看起来像样的步骤。这一点 `FOR_AI.md` 里第一节就写了。

## 它解决什么

两件事，一前一后。

**一、字幕要能批量拿下来。** B站没有官方批量导出：单条视频的字幕要在播放器面板里一条条看，UP 主几百条投稿、合集几十集只能手点。而抓密了会触发 `-352 风控校验失败`，之后一段时间内**所有**接口一起拒，不是只拒被拦的那一条。手动抓的人因此陷入「越抓越慢、越慢越急」。

`asr-polish subs` 把这件事变成一条命令（单视频 / 多P / UP 主 / 合集 / 关键词搜索）；`serve` 让它在后台排队跑，页面关掉任务也不丢；内置限速按域名把请求间隔拉开，不把账号抓进风控。

**二、拿下来的文本不一定能用。** ASR 在中文技术内容上有三个顽固问题，而现有工具都只做到「取到文本」这一步，**没人管取到的文本准不准**：

1. **术语被转成同音错字** —— `SQL` → `色考`、`Python` → `PASS`、`JSON` → `接生`、`Flask` → `福拉斯克`
2. **多引擎结果互相矛盾** —— 云端 ASR 和本地 whisper 各说各话，不知道该信谁
3. **转写完不知道质量** —— 不敢直接拿去总结，担心错字误导结论

两层可以分开用：批量抓（`subs` / `serve`）只负责**完整、快速、可批量**，不承诺修正内容；单条取文本（`fetch`）默认接上订正和打分，不想要就 `--no-correct`。下面按这个顺序讲。

## 核心能力

前四项是**抓取链路**：取一条 → 抓一批 → 排队跑 → 不被封。后三项是**文本加工**：订正、交叉验证、质检。两组可以只用其一。

### ① 从 B站视频取文本 `fetch`

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

### ② 批量抓字幕 `subs`

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

# 按关键词搜一批再抓（--search-pages 控制搜几页，每页约 42 条）
asr-polish subs --search "Python教程" --search-pages 2 --limit 30
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
~/.config/asr-polish/cookies.txt
```

或用 `--cookie /path/to/cookies.txt` 指定。

**怎么拿到这份 Cookie（不用装插件）**：

1. 浏览器打开 `https://www.bilibili.com`，确认右上角是自己的头像（已登录）；
2. `F12` → **网络 / Network** → 按 `F5` 刷新页面；
3. 点列表里第一条请求 → 右侧 **请求标头 / Request Headers** → 找到 `cookie:` 那行，
   右键 **复制值 / Copy value**。

然后：

```bash
asr-polish cookie set      # 直接读剪贴板，写完立刻向 B站 确认登录态
```

也可以 `--file 某个.txt` 指定 cookies.txt 全文。写入是**合并**：只更新同名的那几条，
`buvid3` 这类设备指纹会保留。没有 `SESSDATA` 的内容直接拒绝——那是「未登录」的 Cookie，
写进去只会让人以为配好了。

随时用 `asr-polish cookie` 查看状态。**判断是否有效只认 `nav` 接口的 `isLogin`**：
文件在、字段齐、过期时间还剩半年，都可能已经失效（改密码、被踢下线后服务端会注销）。

#### 用谁的账号、存在哪、发给谁

这三件经常被问到，一次说清。

**用你自己的。** `cookies.txt` 里的 `SESSDATA` 就是你浏览器里的登录凭证。带上它，对 B站
来说这批请求就是你本人发的——风控、限流、你账号的访问记录，都落在这一个账号上。
没有 cookie 也能跑，但只能抓单个 BV 号：UP 主投稿、合集、字幕列表都要登录态。

**只在你机器上。** 那三条默认路径都在仓库之外（家目录下），`.gitignore` 另外排除了
`cookies.txt` / `bilibili_cookies.txt`，就算有人把它放进项目目录也不会被提交。
`asr-polish cookie` 只读本地文件，不做任何上传。

**只发给 B站。** cookie jar 按域名匹配，`SESSDATA` 只随 `*.bilibili.com` 的请求出去；
下载字幕那次是 CDN 域名，不带它。项目对外的域名只有 bilibili 那几个，没有任何统计上报。

**两件不碰你账号的事**：无字幕时走的必剪云端 ASR 只带 `User-Agent`、不带 cookie（音频确实
上传到 B站服务器，但不是以你的身份）；阅读页的「总结」和「问答」只把正文发给你自己配的
模型服务——key 只存在你的浏览器里，随请求发给本机服务转发，不落盘、不进日志。

> **别把 `cookies.txt` 发给任何人，也别贴进 issue。** 它不是「一份配置」，它是你的账号——
> 谁拿到谁就是你。反过来也一样：别人把 cookie 递给你、让你「帮忙跑一下」，别接。

**别人用这个项目，用的是他自己的 cookie。** 仓库里不含任何人的凭证，谁来用谁自己导出、
自己保存，风控也只落在他自己的账号和 IP 上。

### ③ 服务化：抓取变成任务队列 `serve`

批量抓 100 个 UP 主投稿要几分钟，同步接口扛不住：网关 30 秒就掐断连接，页面一关任务也废了。所以给同一个抓取能力加了一层**任务队列**：

```bash
asr-polish serve --port 8040          # 起服务（内含 2 个 worker）
asr-polish submit up:12345 --limit 50 --wait   # 提交并按进度等它跑完
asr-polish status                     # 看队列与最近任务
```

浏览器打开 `http://127.0.0.1:8040` 就是任务面板，每 3 秒刷新。

| 接口 | 说明 |
|---|---|
| `POST /api/tasks` | 提交任务，立刻返回 `202` 和任务 id（不等执行） |
| `GET /api/tasks/{id}` | 查状态、进度、结果、失败原因 |
| `GET /api/tasks?status=&limit=` | 列表 |
| `POST /api/tasks/{id}/retry` | `failed` 重新排队；`running` 强制重启（旧执行的结果会被丢弃） |
| `POST /api/tasks/{id}/retry-failed` | 只重跑失败的条目：**新建一个任务**，带 `only`，已成功的文件不动 |
| `GET /api/tasks/{id}/transcript` | 只读：把已落盘的字幕读成分段（阅读页用，历史任务也能读） |
| `POST /api/tasks/{id}/summary` | 单篇总结：把前端拼好的正文转发给 OpenAI 兼容接口，返回结构化摘要（**不落盘、不记 key**） |
| `POST /api/tasks/{id}/ask` | 单篇问答：正文 + 问题 + 最近几轮历史转发出去，回答带原文时间（**同样不落盘、不记 key**） |
| `GET /api/health` | 存活与 worker 占用 |

三件事决定了它的可靠性，都在 `asr_polish/server/` 里：

- **请求只登记，执行交给 worker**。接口写一行 `pending` 就返回，长活跟请求彻底解耦。
- **状态在 SQLite，不在内存**。进程重启、页面关掉，任务都还在；失败可重试、能查卡在哪一条。
- **抢任务用 CAS，不靠运气**。`BEGIN IMMEDIATE` 先拿写锁再 `SELECT + UPDATE`，两个 worker 不可能领到同一行；再加心跳与僵尸回收，进程被 `kill -9` 留下的 `running` 也能被放回队列。
- **写回要持有所有权**。每次 `claim()` 把 `attempt` 加一，worker 之后的心跳和结论都带 `WHERE id=? AND attempt=?`，写不进去就说明自己已被接管。没有这一条，一个被误判僵尸、又醒过来的 worker 会和接管者同时写同一行——它甚至能把接管者**还在跑**的任务抢先标成完成。

### 抓完之后要能读

字幕落盘之后是一堆 `BV1xx-标题.srt`，想看一眼讲了什么还得打开播放器拖进度条。
所以跑完的任务后面带一个「阅读 →」，点进去是 `/reader`：`http://127.0.0.1:8040/reader`，
也可以直接 `?task=T…` 打开某个历史任务、`?q=关键词` 把定位好的链接发给别人。

页面把字幕**按时间轴合并成段落**——一条只有十来个字的 AI 字幕逐条画出来仍然是碎的，
只是每行多了个时间戳。断段靠三条判据：真实停顿 → 「那 / 所以 / 但是」这类句子起始词 →
120 字上限。只按长度合并的话，58 条字幕会变成 3 段、每段 200 字。

搜索框里的词当场黄底高亮，回车跳下一处；四态徽章、失败明细、「只重跑这 N 条」跟面板
是同一套。**没字幕不算出错**：页面会说清「B站上并非每个视频都有字幕，要转写音频得走
`--asr`」，被风控则直接说「等几分钟，或把 `min-interval` 调大」。

读到想问「这真是这么说的吗」，句子是点得动的：点一下，右侧展开 B站官方播放器，落在
这一句的起始秒（`bvid` / `p` / `t` 三个官方参数，不下载视频、不拼盗链）。必剪 ASR 那条
路没有逐句时间，整篇是 `0 → 99:59:59` 的占位块，这种任务句子不可点、不建播放器，并在
正文上方写明原因——跳到视频开头很容易，但那是错的。

句子上悬停露 ☆，点一下就是重点，黄底加左侧竖条；攒够了导出成 md，文件头带视频标题和
BV 号，每条带时间戳、分 P、原文。重点只存在浏览器 `localStorage` 里，按任务 id 分组，
换机器就没了——这一版不做账号，也不做后端同步。

多分 P 的任务左侧多一栏目录，点哪篇切哪篇，搜索只算当前篇；单分 P 不显示目录。默认只
渲染当前篇，一个 84P 的合集若一次铺开，一屏就是几百段。

读到一半有疑问，顶栏「问答」能就这一篇提问。回答里的每条事实都要带一处原文时间，而这些
时间会被拿真实的段首再核一遍：对得上的可以点，点一下回到那一段并闪一下、再点一次跳到视频
的同一秒；对不上的灰着、点不动，一条都对不上就在回答下面写明「仅供参考」。原文明明没讲
的，模型会写「原文中没有提到」——它编出来的引用过不了这道核对。

对话按「任务 + 分 P」存在浏览器里，刷新还在，切篇各看各的；「清空对话」只清当前这一篇。
**每问一次都是一次模型调用**：正文要重发一遍，所以只带最近 6 轮历史过去，更早的留在页面上
不发。任务重跑、字幕变了，旧对话按和第 1 层总结同一个指纹作废，不会拿着旧对话配新字幕。
必剪 ASR 那种没有逐句时间的篇照常能问，只是不产生时间锚点——问答区顶上会说清原因。

页面是一个 HTML 字符串，无框架、无构建。唯一的外部资源是那个官方播放器，而且点句子时
才加载——不点就仍然是断网可用。正文一律走 `textContent`——字幕是别人视频里的文本，
拼 `innerHTML` 等于给自己开一个 XSS 口子。

### 部分成功不等于成功

一批 20 条里成了 18 条，和 20 条全成，以前在任务表上是同一个样子：状态 `done`，
只有 `result.failed` 一个数字不同。要补那 2 条只能整个任务重来，重来还会再吃一次风控。

现在结果里多一个 `outcome`，四态：

| `outcome` | 含义 | 任务状态 |
|---|---|---|
| `complete` | 每一条都取到字幕 | `done` |
| `partial` | 有产出，但差了几条（失败或没字幕） | `done` |
| `empty` | 零产出：全都没字幕，或指定的条目里没有可跑的 | `done` |
| `failed` | 零产出且全是错误 | `failed` |

`failed_items` 列出每一处失败：`bvid`、`page`、`error`，以及 `retry` ——
`retry` 就是重跑时该传的写法（`BV1xx` 或 `BV1xx#3`），面板和 CLI 直接照抄。
面板上每条任务带一个徽章和「N 处失败明细」，展开后能点「只重跑这 N 条」；
CLI 跑完会打印一条可直接复制的命令：

```bash
asr-polish subs --only BV1xx411c003 BV1xx411c011#3
```

`--only` 只按给定的 BV 号跑，**不会重新解析 target**——UP 主投稿一直在变，
重新解析拿到的是「现在最新的 20 条」，不是你要补的那几条。

```python
# asr_polish/server/store.py：抢任务的原子写法
conn.execute("BEGIN IMMEDIATE")
row = conn.execute("SELECT id FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1")
conn.execute("UPDATE tasks SET status='running', worker=?, started_at=? WHERE id=? AND status='pending'", ...)
```

**边界**：单机、单库、无鉴权，默认只监听 `127.0.0.1`——要对外必须先自己套反向代理。它不是分布式队列，多机部署得换 PostgreSQL 或 Redis。写它是为了让你看清这套骨架，不是为了替代 Celery。

### 在 AI 对话里直接用（MCP）

上面三条路（`fetch` / `subs` / `serve`）的入口都是「你记得命令、敲对参数、再去目录里翻文件」。
但真实场景里人是这么说话的：**「帮我把这条视频的字幕取下来，顺手订正一下」**。

`asr-polish-mcp` 把同一个 `run_subs` 包成 [MCP](https://modelcontextprotocol.io) 工具，
装完之后在 Claude Desktop / Cursor 这类客户端里说一句就行：

```json
{
  "mcpServers": {
    "asr-polish": {
      "command": "asr-polish-mcp"
    }
  }
}
```

装的是命令行脚本没进 `PATH` 的话，用 `"command": "python", "args": ["-m", "asr_polish.mcp"]`。

| 工具 | 干什么 |
|---|---|
| `fetch_subtitles` | 取字幕落盘。`target` 支持 BV 号 / `up:` / `season:` / `search:`，返回四态结果与失败明细 |
| `list_videos` | 只列清单不下载：先看看有什么，再决定抓哪些（避免一次性抓几十条吃风控） |
| `correct_text` | 术语订正，不联网、不改文件 |
| `check_login` | 真的请求一次 `nav` 接口验证 Cookie——文件存在、字段齐全都说明不了问题 |

三点取舍：

- **零新增依赖。** MCP 的 stdio 传输就是「一行一条 JSON-RPC 消息」，标准库够了。
  为此引入官方 SDK 会带进 pydantic / httpx / anyio 一串，与「零第三方依赖」直接冲突。
- **不另写一套判定。** 工具调的就是 CLI 和服务端在用的那个函数，`complete` / `partial` /
  `empty` / `failed` 和「只重跑失败条目」的行为完全一致；失败时返回里直接给出 `only` 该怎么传。
- **多 P 合集默认有上限。** 一条 84 P 的课程在调用方眼里就是「一条视频」，不设限就是
  84 次请求、166 个文件、几万字节的返回体。默认取前 20 P，并明说「共 84 P 只取了 20 个」；
  要全取传 `max_parts: 0`。批量仍然是 `serve` 的正路——那里有队列和断点。

### ④ 批量抓不被封：限速与风控

抓 `up:` 几十条投稿时，每条要打 3～4 个接口（`view` → `nav` → `player/wbi/v2` → 字幕 CDN），全速发就是几百个请求连着出去。B站对这类模式的反应是 `-352 风控校验失败`，而且**之后一段时间内所有接口都拒绝**，不是只拒被拦的那一条。

所以「自己偶尔抓一条」和「挂着长期跑」是两件事。这一版补了三样：

**① 请求间隔下限。** 默认同一域名 1 秒一个请求，带 ±30% 抖动（固定等间隔本身就是机器特征）。放在 `_http_json` 底层而不是批量循环里——循环里加 `sleep` 管不到翻页、wbi 密钥这些旁路请求。

```bash
asr-polish serve --min-interval 1.5          # 风控频繁时调到 2~3
asr-polish subs --up 12345 --min-interval 2
```

注意：速率由它控制，`subs -j` 的并发数不再影响 API 压力（只影响音频下载与本地处理）。

**② 遇风控整体退避，而不是记一条失败。** 风控是按 IP / 账号记的，继续打只会延长限制。默认 60 → 180 → 300 秒退避后重试同一条，期间持续刷新心跳（不刷会被僵尸回收器当成卡死任务重新排队），面板上能看到「第 2 次被风控，退避 180s 后重试，还需 60s」。累计约 9 分钟仍被拦才判失败。

**但如果 B站自己在响应头里说了要等多久（`Retry-After`），就听它的。** 412 / 429 会带这个头，两种写法都认（秒数、HTTP 日期）；夹在 30～600 秒之间——给 0 秒等于没退避（刚被拦立刻再打，判定更重），说一小时后再来就直接失败并说清原因，而不是 300 秒后继续撞三次。没有这个头时（大多数情况，风控走的是业务码而不是 HTTP 状态）才回落到上面的固定档位。

**③ 区分「没登录」和「太快」。** 这两件事返回码都是 `-352`，处置却相反：前者换 Cookie 有用，后者等一下就好。遇到 `-352` 会先用 `nav` 探一次登录态（缓存 5 分钟），未登录就直接告诉你换 Cookie，不白等。

面板顶部那条横幅就是 Cookie 状态：**绿** = 有效，**黄** = 超过 30 天没更新，**红** = 失效或缺失。判断依据是 `nav` 接口的 `isLogin`，不是文件里的过期时间——SESSDATA **可能时间上没过期、服务端却已注销**（改密码、被踢下线），只看文件永远发现不了。点「检查登录态」强制重探。

```bash
curl "http://127.0.0.1:8040/api/health"              # 默认就验证登录态
curl "http://127.0.0.1:8040/api/health?verify=0"     # 只做本地检查，不联网
curl "http://127.0.0.1:8040/api/health?fresh=1"      # 绕过缓存强制重探
```

### ⑤ 术语订正 `correction`

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

### ⑥ 多引擎交叉验证 `crosscheck`

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

### ⑦ 转写质量自评 `quality`

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

## 实测效果

用一段 22 分钟的真实技术视频，对比「本地 faster-whisper」与「必剪云端 ASR」两份转写：

| 指标 | 本地 whisper | 必剪云端 |
|---|---|---|
| 术语订正命中 | 3 处 | 0 处 |
| 质量评分 | **81 分** | **96 分** |
| 交叉验证字符一致率 | 96% | |

质量评分客观地区分出了两个引擎的真实差距——本地 whisper 的错误变体（可疑同音字）明显更多。这正是「转写完不知道哪个准」的解法。

## 安装

**取字幕、订正、评分都不需要第三方依赖**，Python 3.9+ 即可：

```bash
pip install asr-polish                # 全部能力，零第三方依赖
pip install "asr-polish[bilibili]"    # 只在「无字幕视频要转写」时需要（引入 yt-dlp）
```

想改代码就从源码装：

```bash
git clone https://github.com/angus-hu-gzy/asr-polish.git
pip install -e ".[dev]"
```

> `yt-dlp` 仅在「视频没有字幕轨 → 转走必剪云端 ASR」时才被 import。有字幕的视频（含 B站 AI 字幕）不装它也能取。

装好后得到两个命令：`asr-polish`（命令行）和 `asr-polish-mcp`（MCP server，供 AI 客户端调用）。

```bash
# 从 B站视频取文本（字幕轨优先，无字幕走必剪云端 ASR）
asr-polish fetch "https://www.bilibili.com/video/BVxxxxxxxxxx"
asr-polish fetch BVxxxxxxxxxx --out transcript.txt   # 文本存文件

# 批量抓字幕到文件（txt / srt / json / md）
asr-polish subs BVxxxxxxxxxx -f txt,srt -o ./subs
asr-polish subs --up 329954990 --limit 20 -j 3       # 某 UP 主最新 20 条

# 术语订正
asr-polish correct "今天讲色考和PASS，用福拉斯克搭微服务，讲h t t p和k8s"

# 质量打分（--duration 传音频秒数，用于计算字符密度）
asr-polish assess "转写文本" --duration 1316

# 交叉验证两个转写文本文件
asr-polish crosscheck cloud.txt local.txt

# 词表管理
asr-polish lexicon list                    # 列出词表
asr-polish lexicon import my_words.txt     # 批量导入
asr-polish lexicon export --out out.json   # 导出
```

> `fetch` 文本走 stdout、元信息与评分走 stderr，方便 `asr-polish fetch <链接> > text.txt` 直接存文本。

## 快速开始

装好之后最常用的四条：

```bash
# 单个视频取文本（默认接上订正 + 打分）
asr-polish fetch BV1xx411c7mD

# 批量抓字幕，一次导出四种格式
asr-polish subs BV1xx411c7mD -f txt,srt,json,md -o ./subs

# 某个 UP 主最新 20 条投稿，并发 3
asr-polish subs --up 329954990 --limit 20 -j 3

# 起本地服务，批量任务交给它排队跑
asr-polish serve --port 8040
```

开发与验证（从源码目录执行）：

```bash
# 直接运行示例（无需安装）
python examples/demo.py

# 运行全部测试
python -m unittest discover -s tests -v
# 或直接跑单个文件
python tests/test_basic.py

# 准确性门禁（CI 里跑的就是这两条）
python tools/eval_corrections.py --min-precision 0.98 --min-recall 0.95
python tools/eval_corrections.py --cases eval/holdout.jsonl --min-precision 0.98 --min-recall 0.90

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
├── CHANGELOG.md           # 逐版本变更记录（含每版的实测数字）
├── CONTRIBUTING.md        # 怎么跑测试、怎么改词表、怎么发版
├── LICENSE                # MIT
├── NOTICE                 # 第三方声明（bcut-asr 参考）
├── asr_polish/
│   ├── __init__.py        # 统一入口
│   ├── __main__.py        # 支持 python -m asr_polish
│   ├── py.typed           # PEP 561 类型标记
│   ├── cli.py             # 命令行入口（fetch / subs / serve / submit / status / correct …）
│   ├── mcp.py             # MCP server（stdio 传输，zero-dep，供 agent 调用）
│   ├── correction.py      # 术语订正引擎
│   ├── crosscheck.py      # 多引擎交叉验证
│   ├── quality.py         # 转写质量自评
│   ├── bili.py            # B站字幕抓取 + 取文本（字幕轨 → 必剪云端 ASR）
│   ├── export.py          # txt / srt / json / md 四种导出
│   ├── lexicon.py         # 词表加载
│   ├── lexicon_io.py      # 词表批量导入/导出
│   ├── server/            # 服务化：任务队列 + HTTP（零依赖）
│   │   ├── store.py       #   SQLite 任务表 + 状态机（CAS 抢任务）
│   │   ├── worker.py      #   worker 池 + 心跳 + 僵尸回收
│   │   ├── jobs.py        #   任务执行器（把抓取包装成可调度的 job）
│   │   ├── app.py         #   HTTP 路由 + 面板
│   │   ├── reader.py      #   /reader 阅读页（单文件 HTML，无外链）
│   │   ├── transcript.py  #   只读：已落盘字幕 → 分段（选格式 / 路径校验）
│   │   ├── summary.py     #   单篇总结：调 OpenAI 兼容接口（标准库 urllib）
│   │   └── qa.py          #   单篇问答：提示词 + 历史裁剪，复用 summary 的调用
│   └── data/
│       └── corrections.json   # 默认术语词表
├── eval/
│   ├── cases.jsonl        # 开发集（调参用）
│   └── holdout.jsonl      # 留出集（不参与调参，衡量泛化）
├── docs/
│   ├── 召回基准报告.md       # 召回率怎么测的、为什么四个反证实验被否掉
│   └── 真实字幕错误分析.md   # 人工字幕当真值，真实错误长什么样
├── examples/
│   ├── demo.py            # 演示
│   ├── import_sample.txt  # 批量导入示例（TXT）
│   └── import_sample.csv  # 批量导入示例（CSV）
├── tools/                 # 开发工具，不随包安装
│   ├── eval_corrections.py    # 精确率/召回率门禁（CI 用）
│   ├── benchmark_recall.py    # 人造扰动召回基准
│   ├── eval_against_human.py  # 人工字幕 vs AI 字幕，算真实 CER
│   ├── collect_corpus.py      # 采集 AI 字幕语料（给上面两个基准用）
│   ├── audit_corrections.py   # 语料审计：误伤
│   ├── audit_variants.py      # 语料审计：哪些变体其实是常用词
│   ├── ci_guard.py            # 打标签前的 CI 闸门（0 绿 / 1 红或未完成 / 2 查不到）
│   └── hooks/pre-push         # 推标签时自动跑 ci_guard，推分支不受影响
└── tests/
    ├── test_basic.py          # 核心能力 + 误伤回归 + medium 用例
    ├── test_lexicon_io.py     # 词表导入/导出
    ├── test_export.py         # 四种导出格式 + 文件名清洗 + 原子写
    ├── test_risk.py           # 风控识别、退避重试、Cookie 状态
    ├── test_throttle.py       # 节流器
    ├── test_cli.py            # 命令行健壮性（管道编码、--only）
    ├── test_cookie_import.py  # Cookie 解析与写入（含 #HttpOnly_）
    ├── test_server.py         # 状态机 / 并发抢任务 / 重试 / 僵尸回收 / HTTP
    ├── test_jobs_outcome.py   # 批量任务四态 + 只重跑失败条目 + 分P上限
    ├── test_retry_after.py    # Retry-After 两种写法、夹逼、超上限快速失败
    ├── test_reader.py         # srt 解析（CRLF / 坏块 / 占位块）+ 接口四态
    ├── test_reader_ui.py      # 阅读页前端纯函数（node + DOM 桩，没 node 就跳过）
    ├── test_summary.py        # 总结：端点拼装 / 时间解析 / 模型输出兜底 / key 不外泄
    ├── test_qa.py             # 问答：提示词三条约束 / 正文只发一次 / 历史裁剪 / key 不外泄
    ├── test_mcp.py            # MCP 协议层（通知不回包 / 坏 JSON 不中断）+ 工具层
    └── test_ci_guard.py       # 标签闸门：按提交判定、标签对象解引用、只拦标签
```

## 变更记录

完整逐版本记录见 [CHANGELOG.md](./CHANGELOG.md)，每个版本都写明实测数字与判断依据。最近五版：

| 版本 | 一句话 |
|---|---|
| **0.12.0** | 阅读页能点句子跳官方播放器，可划重点、导出 md，多分 P 有目录 |
| **0.11.0** | 加 `/reader` 阅读页：抓到的字幕排成能读的文字稿，搜索高亮 + 时间戳分段 |
| **0.10.0** | 加 MCP server：agent 在对话里就能取字幕；退避开始听服务端给的 `Retry-After` |
| **0.9.2** | 首次发到 PyPI，`pip install asr-polish`；这一版只改「怎么装」 |
| **0.9.1** | 结果区分全部成功 / 部分成功 / 无产出；列出失败明细，`retry-failed` 与 `subs --only` 只补失败的那几条 |

> 0.5.0 有一个 P0 回归（`--out` 帮助文本里 `%APPDATA%` 未转义，argparse 崩溃，CLI 全不可用），
> 已在 0.5.1 修复。装到 0.5.0 的请直接升级。


## 许可证

[MIT](./LICENSE) · 作者 [angus-hu-gzy](https://github.com/angus-hu-gzy)

其他文档：[变更记录](./CHANGELOG.md) · [参与贡献](./CONTRIBUTING.md) · [测量报告](./docs)

## 诚实说明（边界）

- **只取公开可见的内容，且不保证每条都有**。视频得有字幕轨才能直接取（UP 主上传的 CC，或 B站自己的 AI 字幕）；AI 字幕要带登录态 Cookie 才下发。完全没有字幕的视频，单条 `fetch` 会转走必剪云端 ASR（需 `yt-dlp`，音频上传至 B站服务器）。
- **限速能把风控概率压低，不是保证**。B站的判定按 IP / 账号记，长时间高频抓仍可能被拦；被拦后按 60 → 180 → 300 秒退避，通常能恢复，但恢复时间不由我们控制。
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

两份测量报告在 [`docs/`](./docs)：[召回基准报告](./docs/召回基准报告.md)（怎么测、四个反证实验为什么被否掉）、
[真实字幕错误分析](./docs/真实字幕错误分析.md)（真实错误长什么样、为什么规则方案够不着）。

- **交叉验证基于 token 级对齐**。它衡量「多引擎是否一致」，而非「是否正确」——两个引擎可能同时错成同一个词。它回答的是「这段可不可信」，不是「这段绝对正确」。
- **质量评分是启发式的**，用于同一段内容的横向对比和快速筛查，不是绝对标准。
- **fetch 依赖外部服务**。字幕轨来自 B站 API（可能因风控或视频设置拿不到）；无字幕时的必剪 ASR 需要 `yt-dlp` 下载音频并把音轨上传至 B站服务器。二者任一失败都会给出明确报错，而非静默返回空文本。
