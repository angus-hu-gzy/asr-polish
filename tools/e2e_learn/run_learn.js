/* 用真 Chrome 跑一遍「一键学完」：真页面、真 localStorage、真模型请求。
 *
 * 为什么不用 jsdom：流水线要真的发请求、真的读写 localStorage、真的按篇更新
 * DOM 节点，jsdom 这三样里两样要打桩，测的就不再是那条路径了。
 *
 * 只依赖 node 22 自带的 fetch / WebSocket 和一个开着 --remote-debugging-port
 * 的 Chrome，没有 npm 包。
 */
const URL_READER = process.argv[2];
const CFG = JSON.parse(process.argv[3]);
const CDP = "http://127.0.0.1:9333";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(CDP + "/json/list")).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* chrome 还没起来 */ }
    await sleep(250);
  }
  throw new Error("连不上 Chrome 的调试端口");
}

let ws, seq = 0;
const waiting = new Map();

function call(method, params) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params: params || {} }));
  return new Promise((res, rej) => {
    waiting.set(id, { res, rej });
    setTimeout(() => { if (waiting.has(id)) { waiting.delete(id); rej(new Error(method + " 超时")); } }, 30000);
  });
}

async function evaluate(expr) {
  const r = await call("Runtime.evaluate", {
    expression: expr, awaitPromise: true, returnByValue: true,
  });
  if (r.exceptionDetails) {
    throw new Error("页面里抛异常：" + JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description || r.exceptionDetails.text));
  }
  return r.result && r.result.value;
}

async function main() {
  const t = await target();
  ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) {
      const w = waiting.get(m.id); waiting.delete(m.id);
      if (m.error) w.rej(new Error(m.error.message)); else w.res(m.result);
    }
  };
  await call("Runtime.enable");
  await call("Page.enable");

  // 配置先写进 localStorage，再进页面——页面一加载就是「配好了」的状态
  await call("Page.navigate", { url: "http://127.0.0.1:8123/reader" });
  await sleep(800);
  await evaluate("localStorage.clear(); localStorage.setItem('asr-polish:model:v1',"
    + JSON.stringify(JSON.stringify(CFG)) + "); true");

  await call("Page.navigate", { url: URL_READER });
  // 等正文真的渲染出来（4 篇的目录、4 个 section）
  for (let i = 0; i < 60; i++) {
    const ready = await evaluate("document.querySelectorAll('#doc .part').length > 0 && !document.querySelector('#learnbtn').hidden");
    if (ready) break;
    await sleep(250);
  }

  const before = await evaluate(`(function(){
    var b = document.querySelector('#learnbtn');
    return { hidden: b.hidden, text: b.textContent, title: b.title,
             parts: document.querySelectorAll('#toclist li').length,
             stat: document.querySelector('#toclist').textContent.slice(0, 200) };
  })()`);
  console.log("① 进页面：", JSON.stringify(before, null, 1));

  // 点「一键学完」→ 出预估
  await evaluate("document.querySelector('#learnbtn').click(); true");
  for (let i = 0; i < 40; i++) {
    const ok = await evaluate("!!document.querySelector('#learnbody .lact')");
    if (ok) break;
    await sleep(250);
  }
  const est = await evaluate(`(function(){
    return { panel: !document.querySelector('#learn').hidden,
             body: document.querySelector('#learnbody').innerText };
  })()`);
  console.log("\n② 预估：", JSON.stringify(est, null, 1));

  // 点「开始跑」
  const started = await evaluate(`(function(){
    var bs=document.querySelectorAll('#learnbody button');
    for (var i=0;i<bs.length;i++) if (bs[i].textContent==='开始跑'){ bs[i].click(); return true }
    return false;
  })()`);
  if (!started) throw new Error("预估面板里没有「开始跑」");
  console.log("\n③ 已点开始跑，等流水线…");

  const t0 = Date.now();
  let last = "";
  while (Date.now() - t0 < 420000) {
    const st = await evaluate("document.querySelector('#learnstat').textContent");
    if (st !== last) { console.log("   [" + Math.round((Date.now() - t0) / 1000) + "s] " + st); last = st; }
    if (st === "跑完了" || st === "已停止") break;
    await sleep(1500);
  }

  const done = await evaluate(`(function(){
    var out = { stat: document.querySelector('#learnstat').textContent,
                body: document.querySelector('#learnbody').innerText,
                btn: document.querySelector('#learnbtn').textContent,
                toc: [] };
    document.querySelectorAll('#toclist li').forEach(function(li){
      out.toc.push(li.innerText.replace(/\\n/g, ' | '));
    });
    var ls = Object.keys(localStorage).sort();
    out.sumKeys = ls.filter(function(k){ return k.indexOf(':summary:') >= 0 });
    out.quizKeys = ls.filter(function(k){ return k.indexOf(':quiz:') >= 0 });
    out.sumParts = [];
    out.sumKeys.forEach(function(k){
      var d = JSON.parse(localStorage.getItem(k) || '{}');
      out.sumParts.push(Object.keys(d.parts || {}).length);
    });
    out.quizParts = [];
    out.quizKeys.forEach(function(k){
      var d = JSON.parse(localStorage.getItem(k) || '{}');
      out.quizParts.push(Object.keys(d.parts || {}).length);
    });
    return out;
  })()`);
  console.log("\n④ 跑完：", JSON.stringify(done, null, 1));

  // 再点一次：全做完之后应该是 0 次调用
  await evaluate("document.querySelector('#learnbtn').click(); true");
  await sleep(1500);
  const again = await evaluate("document.querySelector('#learnbody').innerText");
  console.log("\n⑤ 第二次点（应当 0 次调用）：\n" + again);

  ws.close();
}

main().then(() => process.exit(0)).catch((e) => {
  console.error("E2E 失败：", e && e.stack || e);
  process.exit(1);
});
