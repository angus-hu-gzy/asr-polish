/* 补两个只有真浏览器才能证的事：
 *   A. 单分 P 的任务不显示「一键学完」
 *   B. 跑着的时候点「停止」真的会停，且已跑完的篇留着
 *   C. 跑着的时候单篇那几个按钮是锁着的（不然会并发打同一篇）
 */
const CDP = "http://127.0.0.1:9333";
const SINGLE = process.argv[2];
const MULTI = process.argv[3];
const CFG = JSON.parse(process.argv[4]);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(CDP + "/json/list")).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page;
    } catch (e) {}
    await sleep(250);
  }
  throw new Error("连不上 Chrome");
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
async function ev(expr) {
  const r = await call("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error("页面异常：" + (r.exceptionDetails.exception && r.exceptionDetails.exception.description));
  return r.result && r.result.value;
}

async function go(url, waitFor) {
  await call("Page.navigate", { url });
  for (let i = 0; i < 60; i++) {
    if (await ev(waitFor)) return;
    await sleep(250);
  }
  throw new Error("页面没等到：" + waitFor);
}

async function main() {
  const t = await target();
  ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && waiting.has(m.id)) {
      const w = waiting.get(m.id); waiting.delete(m.id);
      if (m.error) w.rej(new Error(m.error.message)); else w.res(m.result);
    }
  };
  await call("Runtime.enable");
  await call("Page.enable");

  // A. 单分 P
  await go(SINGLE, "document.querySelectorAll('#doc .part').length > 0");
  await sleep(600);
  const single = await ev(`(function(){
    var b=document.querySelector('#learnbtn');
    return { hidden:b.hidden, parts: document.querySelectorAll('#doc .part').length,
             toc: document.querySelectorAll('#toclist li').length };
  })()`);
  console.log("A. 单分 P：", JSON.stringify(single));

  // B/C. 多分 P：先开跑，跑起来之后看锁、再停
  await go(MULTI, "document.querySelectorAll('#doc .part').length > 1 && !document.querySelector('#learnbtn').hidden");
  await ev("document.querySelector('#learnbtn').click(); true");
  for (let i = 0; i < 40; i++) {
    if (await ev("!!document.querySelector('#learnbody .lact')")) break;
    await sleep(250);
  }
  // 用「重新跑全部」进入全量重跑，这样立刻就有事可停
  await ev(`(function(){
    var bs=document.querySelectorAll('#learnbody button');
    for (var i=0;i<bs.length;i++) if (bs[i].textContent==='重新跑全部'){ bs[i].click(); return 1 }
    return 0;
  })()`);
  await sleep(700);
  await ev(`(function(){
    var bs=document.querySelectorAll('#learnbody button');
    for (var i=0;i<bs.length;i++) if (bs[i].textContent==='开始跑'){ bs[i].click(); return 1 }
    return 0;
  })()`);

  await sleep(2500);
  const running = await ev(`(function(){
    var b=document.querySelector('#learnbtn');
    return { btn:b.textContent, title:b.title, cls:b.className,
             sumbtn: document.querySelector('#sumbtn').disabled,
             quizbtn: document.querySelector('#quizbtn').disabled,
             notesbtn: document.querySelector('#notesbtn').disabled,
             stat: document.querySelector('#learnstat').textContent,
             sumbtnTitle: document.querySelector('#sumbtn').title };
  })()`);
  console.log("B. 跑着的时候：", JSON.stringify(running, null, 1));

  // 点顶栏那个已经变成「停止」的按钮
  await ev("document.querySelector('#learnbtn').click(); true");
  for (let i = 0; i < 60; i++) {
    const st = await ev("document.querySelector('#learnstat').textContent");
    if (st === "已停止" || st === "跑完了") break;
    await sleep(1000);
  }
  const after = await ev(`(function(){
    var b=document.querySelector('#learnbtn');
    return { btn:b.textContent, hidden:b.hidden, stat:document.querySelector('#learnstat').textContent,
             body:document.querySelector('#learnbody').innerText,
             sumbtn: document.querySelector('#sumbtn').disabled,
             toc: Array.prototype.map.call(document.querySelectorAll('#toclist li'),
                   function(li){ return li.innerText.replace(/\\n/g,' | ') }) };
  })()`);
  console.log("\nC. 点了停止之后：", JSON.stringify(after, null, 1));
  ws.close();
}

main().then(() => process.exit(0)).catch((e) => { console.error("失败：", e && e.stack || e); process.exit(1); });
