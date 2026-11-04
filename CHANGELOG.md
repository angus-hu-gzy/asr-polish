# 变更记录

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

每一条实测数字都来自仓库里的脚本，可复现：`tools/benchmark_recall.py`（人造扰动召回）、
`tools/eval_corrections.py`（开发集/留出集门禁）、`tools/eval_against_human.py`（人工字幕真值）。

---

## [0.9.0] - 2026-11-04

修一个能把任务状态写坏的缺陷：**同一个任务被两份执行同时跑。**

`POST /api/tasks/{id}/retry` 一直允许对 `running` 的任务重试。但重试只改数据库，
管不了还在跑的线程——于是旧 worker 继续跑，新 worker 又从第一格开始，两份执行
抢着写同一行状态、同一个输出文件。

### 修复

- **任务写回加所有权令牌**（fencing token）。任务表新增 `attempt` 列，每次
  `claim()` 加一；worker 记住自己领到的是第几轮，之后的 `heartbeat` / `finish` /
  `fail` 全部带 `WHERE id=? AND attempt=?`。写不进去就说明已被接管，安静退出。

  实测修复前（`running` 重试）：

  ```
  worker-A 领走        last_heartbeat=...
  用户点重试           status=pending
  worker-B 领走        同一个任务，两份执行
  A 刷心跳             progress=0.30   ← B 的进度被 A 覆写
  A 跑完 finish        status=done     ← 任务被标完成，B 还在跑
  B 跑完 finish        StateError      ← 日志打出「已重新排队」，其实什么都没排
  ```

  修复后：A 的心跳返回 `False` 立刻停手，`finish` 被拒，最终结果只由 B 写入。

- **僵尸回收路径上的同类问题**。这条更隐蔽，**没有任何人点重试**：worker 因 GC
  停顿 / 机器休眠心跳中断，回收器判定僵尸并放回队列，新 worker 领走；旧 worker
  醒来后跑完，`running → done` 恰好是合法迁移，于是一个**还在跑的任务**被标成完成。
  现在 `reclaim_stale()` 的两条 UPDATE 也带 `attempt=attempt+1`，旧令牌当场作废。

- **过期执行不再写假日志**。以前 `finish()` 撞上终态抛 `StateError`，被 worker 当
  普通异常接住转去 `fail()`，而 `fail()` 见终态直接返回——日志于是打出
  「失败，已重新排队（重试 1/2）」，实际什么都没发生。现在由返回值区分
  「我失败了」和「我被接管了」。

- **字幕文件原子写**。`write_all` 原来是 `path.write_text`（先截断再写），两份执行
  并发写同一文件名时，读的人会看到被截断的文件。改为同目录临时文件 + `os.replace`，
  读者要么看到旧完整版，要么看到新完整版。

### 变更

- `retry` 对 `running` 的语义明确为**强制重启**，响应体多了 `note` 字段说明
  「原执行的结果会被丢弃」。`failed` 的重试行为不变。
- `Store.finish()` 返回 `bool`（是否写入成功），`Store.fail()` 返回
  `Optional[str]`（`None` = 令牌过期，这次失败不该由我记录），`heartbeat()` 返回
  `bool`。三者都新增可选参数 `attempt`；不传时不校验，管理员路径与旧调用不受影响。
- 任务 JSON 多了 `attempt` 字段，可以直接看出这个任务被重启过几次。

### 升级

已有的 `tasks.db` 会在打开时自动 `ALTER TABLE` 补 `attempt` 列（默认 0），
不需要删库重建。

### 测试

207 项（新增 15 项：令牌单调递增、`running` 重试后旧执行的三类写回全被拒、
僵尸回收后旧执行不能抢先标完成、预算用尽回收也 bump 尝试、旧库自动迁移且幂等、
worker 在接管后丢弃结果与失败记录、原子写不留临时文件）。

---

## [0.8.0] - 2026-11-04

批量抓不会再把账号抓进风控。**这一版新增了限速、风控退避、Cookie 状态提示。**

起因是一个真实的失败：抓 `up:` 几十条投稿时没有任何主动间隔，几百个请求连着打出去，
B站直接回 `-352 风控校验失败`。而旧代码把这个和「这条视频取不到」一视同仁——
记一条失败，接着打下一条，正好帮对方把封禁续上。

### 新增

- **请求节流**（`asr_polish/throttle.py`）。按域名记录上次请求时间，同一域名
  两次请求间隔不足 `min_interval` 就等待。默认 **1 秒 + ±30% 抖动**。
  - 放在 `_http_json` 底层而不是批量循环里：循环里加 `sleep` 管不到翻页、
    wbi 密钥这些旁路请求，风控照样会来。
  - 只节流 `api.bilibili.com`。必剪（`member.bilibili.com`）的轮询本来就该是
    1 秒一次，字幕 CDN 是静态资源，这两类被拖慢换不来任何风控上的好处。
  - `--min-interval` 可调（`subs` 与 `serve` 都有）。注意 `subs -j` 的并发数
    从此不再影响 API 压力，只影响音频下载与本地处理。
- **风控识别与退避。** 新增 `BiliRiskControl`，识别 `-352 / -412 / -799 / -509`
  与 HTTP `412 / 429`，以及「接口返回 HTML 验证页」。任务遇风控后
  **60 → 180 → 300 秒退避再重试同一条**，而不是记一条失败继续打下一个。
  - 退避被切成 15 秒一段，每段刷新心跳与 `message`——否则 300 秒没有心跳，
    会被僵尸回收器判定为卡死并重新排队。面板上能直接看到「退避 180s，还需 60s」。
  - 累计 9 分钟仍被拦才判失败，错误信息直接给出可操作建议。
- **区分「没登录」和「太快」。** 两者返回码都是 `-352`，处置却相反。
  遇到 `-352` 会先用 `nav` 探一次登录态（结果缓存 5 分钟，且在缓存键里带上
  Cookie 文件的 mtime，重新导出后立刻生效），未登录就直接报 `BiliLoginRequired`
  并提示换 Cookie，不再白等 9 分钟。
- **Cookie 状态提示。** `bili.cookie_status()` 返回路径 / 是否可解析 /
  是否有 SESSDATA / 文件年龄 / 是否真的登录。`GET /api/health` 默认带上它，
  面板顶部横幅按 `ok`/`warn`/`error` 上色，点「检查登录态」可强制重探。
- **`asr-polish cookie` 命令。** 不用装浏览器插件，也不用自己拼 Netscape
  格式：从开发者工具复制 `Cookie:` 请求头那一行，`asr-polish cookie set`
  直接读剪贴板，解析后立即向 B站 确认登录态。
  - 写入是**合并**而不是覆盖：只更新同名的那几条，`buvid3` 这类设备指纹保留。
    只贴了 SESSDATA 就整份覆盖，会顺手把设备指纹抹掉，反而更容易被风控。
  - 先写临时文件、验证能读回来、再原子替换。粘贴内容有问题时原文件不受影响。
  - 没有 `SESSDATA` 的内容直接拒绝——那是「未登录」状态的 Cookie，写进去只会
    让人以为配好了，然后对着 `-352` 干瞪眼。
  `?verify=0` 关掉验证，`?fresh=1` 绕过缓存。

### 修复

- **取字幕时每条视频都重复请求一次 `nav`。** 明明有 `_wbi_mixin_key()` 的进程内
  缓存却没用，抓 50 条就是 50 个多余请求。改用 `_signed_query()` 后每条视频的
  API 请求从 4 个降到 3 个。
- **wbi 密钥长度未校验** —— 密钥短于 64 字符时 `raw[i]` 直接 `IndexError`，
  是个看不懂的崩溃。现在明确报「wbi 密钥长度异常，接口可能已变更」。
- **`Store.close()` 只关当前线程的连接。** worker 线程退出后它的连接没人关，
  在 Windows 上会锁住库文件（测试里表现为 `unclosed database` 警告，实测 11 个）。
  现在维护一份连接全集统一关闭，并给连接加 `check_same_thread=False`
  ——否则跨线程关闭会被 SQLite 直接拒绝，`except` 一吞就永远查不出来。
- **重试后成功的任务保留着上一次的 error。** 面板上一条「完成」的任务底下挂着
  一行红字，看的人只会以为它失败了。`finish()` 现在会清空 error。
- **未登录时「无字幕」的结论不准。** B站的 AI 字幕（`ai-zh`）需要登录才下发，
  未登录时「取不到字幕」和「这视频本来没字幕」长得一模一样。现在会追加一条
  warning 说明，任务结果里也能看到（`items[].note`）。
- **`#HttpOnly_` 开头的 SESSDATA 被当成注释。** 浏览器插件导出的 cookies.txt
  会给 HttpOnly 项加这个前缀，而原来的文本扫描直接 `line.startswith("#")` 跳过，
  于是有效 Cookie 被判成「文件里没有 SESSDATA」。现在按格式规范识别该前缀。

### 变更

- `status` 与 `submit --wait` 的失败信息可能变长（退避提示、修复建议），
  面板上仍按 80 字符截断。
- 批量抓取比以前慢：每条视频 3 个请求 × 1 秒间隔。这是刻意的，
  「不被封」优先于「快」。

### 测试

192 项（新增 75 项：节流间隔与并发不重叠、风控码识别、退避序列与心跳、
Cookie 三态与登录探测缓存、Cookie 导入的解析/合并/拒绝与原子写入、
重试后清 error、连接泄漏）。

## [0.7.1] - 2026-11-03

修一个能让任务无限重跑的 bug。**这一版没有新增功能。**

### 修复

- **僵尸回收不检查重试上限。** `reclaim_stale()` 把心跳超时的任务放回队列时
  只做 `retries = retries + 1`，从不和 `max_retries` 比较。后果是：一个
  「一跑就把 worker 搞死」的任务（比如批量太大把内存撑爆、被 OOM killer 干掉）
  会被无限回收、无限重跑，**永远落不到 `failed`**。用户看到的是「一直在排队」，
  和真正卡死的僵尸任务一样查不出原因——正好是这一版想要消灭的那种故障。

  现在分两步走：还有重试预算的才回队列，预算用完的直接判 `failed`，
  `error` 写明「心跳超时且重试次数已用尽」。

### 变更

- `status` 表格里的状态值改为中文（`排队`/`进行中`/`完成`/`失败`），
  与中文表头一致；`submit --wait` 的进度行同样。
- 删掉 `worker.run_once()`。它的 docstring 说「给测试和 `submit --wait` 用」，
  但 `submit --wait` 走的是 HTTP 轮询，测试里也没有调用——是死代码，注释还在误导人。
- 测试 115 → 117（僵尸回收上限、零预算立即失败）

## [0.7.0] - 2026-11-03

服务化。批量抓 100 条投稿要几分钟，同步接口会被网关 30 秒掐断、页面一关任务就废。
这一版把抓取包装成任务队列：请求只登记，执行交给 worker，状态落在 SQLite。

### 新增

- `asr_polish/server/`：零依赖的任务队列 + HTTP 服务
  - `store.py`：SQLite 任务表 + 严格状态机，非法迁移抛 `StateError`；
    `claim()` 用 `BEGIN IMMEDIATE` 先拿写锁再查改，两个 worker 不可能领到同一行
  - `worker.py`：worker 池、心跳、僵尸回收（`kill -9` 留下的 running 会被放回队列）
  - `jobs.py`：任务执行器，`target` 支持 `BV号` / `up:mid` / `season:mid:sid` / `search:关键词`
  - `app.py`：REST 接口 + 每 3 秒刷新的只读面板
- CLI 三个新命令：`serve`、`submit --wait`、`status`
- 测试 89 → 115（新增状态机、并发抢任务、重试、僵尸回收、HTTP 路由）

### 修复

- **pyproject 的 `packages` 写死成 `["asr_polish"]`**，子包不会进 wheel，
  `pip install` 后 `asr-polish serve` 会 ImportError。改为 `packages.find`。

### 踩坑记录

写这一版踩的四个坑，代码里都留了注释：

1. 每次连接都执行 `PRAGMA journal_mode=WAL`，并发下写事务变成 readonly。
   WAL 是持久属性，初始化时设一次就够。
2. 每次操作都 open/close 连接，高频并发下同样撞 readonly，还慢。改成线程本地复用。
3. `with sqlite3.connect(...) as conn` 只提交事务、**不关闭连接**，长连接会泄漏。
4. `_check_transition` 里 `src == dst` 直接放行，导致「重复 finish」被当成无操作。
   终态必须报错，否则「同一个任务被完成两次」这种 bug 永远查不出来。

### 边界

单机、单库、无鉴权，默认只监听 `127.0.0.1`，对外要先套反向代理。
不是分布式队列，多机部署得换 PostgreSQL 或 Redis——写它是为了看清骨架，不是替代 Celery。


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

[0.7.1]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.5...v0.6.0
[0.5.6]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.5...4ea8c76
[0.5.5]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/angus-hu-gzy/asr-polish/compare/v0.4.0...v0.5.0
