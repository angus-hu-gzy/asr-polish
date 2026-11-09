# 一键学完：真浏览器跑一遍

单测能证明「计划怎么排、队列怎么跑」，证不了三件事：页面上的按钮在不在、停下
之后按钮能不能再点、进度行会不会卡在「…」。这三件只有真浏览器 + 真模型能验。

## 一次性准备

```bash
python tools/e2e_learn/setup.py            # 造 4 篇的合集，记下 TASK_ID
python tools/e2e_learn/setup.py --single   # 再造一个只有 1 篇的，记下 TASK_ID
```

起服务（在 `_work/` 里起，笔记就落在那，不会脏了仓库根目录）：

```bash
cd tools/e2e_learn/_work
PYTHONPATH=<repo> python -m asr_polish serve --db tasks.db --port 8123 --workers 0
```

起一个**独立 profile** 的无头 Chrome（别复用日常那个，9222 常被它占着）：

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --headless=new --disable-gpu --remote-debugging-port=9333 \
  --user-data-dir=<abs>/tools/e2e_learn/_work/chrome-profile \
  --no-proxy-server about:blank &
```

脚本只依赖 node 22 自带的 `fetch` / `WebSocket`，不需要装包。

## 两个脚本

```bash
# 主流程：进页面 → 预估 → 跑完 4 篇 12 层 → 再点一次应当是 0 次调用
NO_PROXY="*" node tools/e2e_learn/run_learn.js \
  "http://127.0.0.1:8123/reader?task=<合集ID>" \
  "{\"base\":\"...\",\"model\":\"...\",\"key\":\"$KEY\"}"

# 界面上的三件硬要求：单篇不显示按钮、跑着时按钮锁住、点停止后解锁
NO_PROXY="*" node tools/e2e_learn/run_guards.js \
  "http://127.0.0.1:8123/reader?task=<单篇ID>" \
  "http://127.0.0.1:8123/reader?task=<合集ID>" \
  "{\"base\":\"...\",\"model\":\"...\",\"key\":\"$KEY\"}"
```

跑一次会真花钱（4 篇 × 3 层 = 12 次调用；停下来那次大约 1~2 次）。切短字幕就是
为了把这个数压下来。

## 0.19.0 的实测记录

- 4 篇 12 层全部成功，耗时 **36 秒**；第二次点「一键学完」→「要跑 0 篇 · 跳过 4 篇」，
  面板写「0 次调用」。
- 停下来之后：`总结 ✓` 留着，正在飞的那一层退回 `·`，三个单篇按钮立刻能再点。

第二组第一次跑出来是**红的**，两个 bug 都是这样捞到的：那一层在目录里永远挂着
`…`（作废的请求没人再管它），以及 `S.sumBusy` 没收导致「总结」灰到刷新页面为止。
现在两处都有单测钉住（`test_aborted_requests_still_release_the_button`、
`test_stopping_clears_the_layer_that_was_in_flight`）。
