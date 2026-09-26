// 路径可视化页面自检：无界面 Chrome 打开 HTML，收集 JS 报错，模拟常用操作并截图。
// 用法：node tools/check_visualizer.mjs <路径可视化.html> [截图输出目录]
// 需要本机装有 Chrome 或 Edge；Node 18+（自带 fetch / WebSocket）。
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const htmlPath = resolve(process.argv[2] || "");
const outDir = resolve(process.argv[3] || join(tmpdir(), "visualizer-shots"));
mkdirSync(outDir, { recursive: true });
const browsers = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
];
const browser = browsers.find(existsSync);
if (!browser) throw new Error("没有找到 Chrome/Edge");
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), "viz-profile-"));
const proc = spawn(browser, [
  "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1600,950", "--no-first-run", "--disable-gpu", "about:blank",
], { stdio: "ignore" });

const sleep = ms => new Promise(r => setTimeout(r, ms));
let targets;
for (let i = 0; i < 50; i++) {
  try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(200); }
}
const page = targets.find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let nextId = 1;
const pending = new Map();
const errors = [];
ws.addEventListener("message", event => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.exceptionThrown") errors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") errors.push(msg.params.args.map(a => a.value ?? a.description).join(" "));
});
const send = (method, params = {}) => new Promise(r => { const id = nextId++; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const evaluate = async expr => {
  const res = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (res.result.exceptionDetails) errors.push(`evaluate(${expr.slice(0, 60)}): ${res.result.exceptionDetails.exception?.description}`);
  return res.result.result?.value;
};
const shot = async name => {
  const res = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, name), Buffer.from(res.result.data, "base64"));
};

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 950, deviceScaleFactor: 1, mobile: false });
const t0 = Date.now();
await send("Page.navigate", { url: pathToFileURL(htmlPath).href });
for (let i = 0; i < 100 && !(await evaluate("window.__cableViewerReady === true")); i++) await sleep(100);
const loadMs = Date.now() - t0;
await sleep(300);
await shot("1_plan.png");
const report = { loadMs };
report.listCount = await evaluate("document.getElementById('listCount').textContent");
report.items = await evaluate("document.querySelectorAll('.cable-item').length");
// 键盘切换、筛选、排序、密度、问题清单、三维、点击拾取
await evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown'}))");
await evaluate("(() => { const s = document.getElementById('sortBy'); s.value = 'diff'; s.dispatchEvent(new Event('change')); })()");
report.firstAfterSort = await evaluate("document.querySelector('.cable-item .cable-line span').textContent");
await evaluate("document.getElementById('showDensity').click()");
await sleep(200);
await shot("2_density.png");
await evaluate("document.getElementById('issuesBtn').click()");
await sleep(300);
report.issues = await evaluate("document.querySelectorAll('#issueList li').length");
await shot("3_issues.png");
await evaluate("document.getElementById('drawerClose').click()");
await evaluate("document.getElementById('view3d').click()");
await sleep(300);
const t3 = Date.now();
for (let i = 0; i < 5; i++) await evaluate("document.getElementById('resetView').click()");
report.render3dMs = Math.round((Date.now() - t3) / 5);
await shot("4_3d.png");
await evaluate("(() => { const s = document.getElementById('search'); s.value = 'zzzz-不存在'; s.dispatchEvent(new Event('input')); })()");
report.emptyList = await evaluate("document.querySelector('#cableList .empty')?.textContent");
await evaluate("(() => { const s = document.getElementById('search'); s.value = ''; s.dispatchEvent(new Event('input')); })()");
await evaluate("document.getElementById('viewPlan').click()");
await evaluate("(() => { const s = document.getElementById('statusFilter'); s.value = 'FAIL'; s.dispatchEvent(new Event('change')); })()");
report.failItems = await evaluate("document.querySelectorAll('.cable-item').length");
// 检查点交互：覆盖小间隙、大范围缩放、跨层三维定位与实际鼠标拾取。
const gapCount = await evaluate("data.gaps.length");
if (gapCount) {
  const check = async (name, expr) => {
    if (!(await evaluate(expr))) errors.push(`检查点验收失败：${name}`);
  };
  await check("默认隐藏检查点", "!state.showGaps && !document.getElementById('showGaps').checked && !svg.querySelector('[data-gap-index]')");
  await evaluate("focusGap(0)");
  await check("默认包含周边", "planViewBox()[2] >= Number(params['CAD每米单位'])*12");
  await shot("5_gap_context.png");
  await evaluate("document.getElementById('gapDetail').click()");
  const detailWidth = await evaluate("planViewBox()[2]");
  await shot("6_gap_detail.png");
  for(let i=0;i<18;i++) await evaluate("document.getElementById('zoomOut').click()");
  await check("可以从厘米细节缩回楼层范围", `planViewBox()[2] > ${detailWidth}*5`);
  await check("缩小后红圈仍可辨认", "Math.abs(Number(svg.querySelector('[data-gap-index] circle').getAttribute('r'))/planUnitsPerPixel(planViewBox())-17)<0.1");
  await evaluate("document.getElementById('fitFloor').click()");
  await check("全层仍保留检查点", "state.gap===0 && state.gapScope==='floor' && document.getElementById('gapInfo').style.display==='block'");
  await shot("7_gap_floor.png");
  await evaluate("document.getElementById('gapContext').click(); document.getElementById('view3d').click()");
  await check("三维保留详情和楼层", "state.view==='3d' && state.gap===0 && state.floor===data.gaps[0].floor && document.getElementById('gapInfo').style.display==='block'");
  await check("三维标记居中", "(()=>{const g=data.gaps[state.gap];const p=lastProjector(wx(g.floor,(g.x+g.px)/2),wy(g.floor,(g.y+g.py)/2),zOf(g.floor));return Math.hypot(p.x-canvas.clientWidth/2,p.y-canvas.clientHeight/2)<1})()");
  await shot("8_gap_3d.png");
  if(gapCount>1) {
    await evaluate("document.getElementById('gapNext').click()");
    await check("下一处保持三维且跟随楼层", "state.view==='3d' && state.gap===1 && state.floor===data.gaps[1].floor && camera.tz===zOf(data.gaps[1].floor)");
  }
  await evaluate("document.getElementById('gapDetail').click()");
  await shot("9_gap_3d_detail.png");
  // 点击三维红圈：使用浏览器鼠标事件，检查实际拾取链路。
  const hit = await evaluate("(()=>{const h=gapHits.find(h=>h.index===state.gap),r=canvas.getBoundingClientRect();return {x:r.left+h.x,y:r.top+h.y}})()");
  await send("Input.dispatchMouseEvent",{type:"mousePressed",...hit,button:"left",clickCount:1});
  await send("Input.dispatchMouseEvent",{type:"mouseReleased",...hit,button:"left",clickCount:1});
  await sleep(350);
  await check("红圈点击显示对应检查点", "state.gapScope==='context' && document.getElementById('gapInfo').style.display==='block'");
  for(let i=0;i<gapCount;i++) {
    await evaluate(`focusGap(${i})`);
    await check(`检查点 ${i+1} 三维楼层与定位`, `state.view==='3d' && state.gap===${i} && state.floor===data.gaps[${i}].floor && camera.tz===zOf(data.gaps[${i}].floor) && pickGap(canvas.clientWidth/2,canvas.clientHeight/2)>=0`);
  }
  await evaluate("document.getElementById('gapContext').click(); const beforeZoom=camera.zoom; document.getElementById('zoomIn').click(); window._zoomPassed=camera.zoom>beforeZoom");
  await check("三维按钮放大", "window._zoomPassed");
  await evaluate("document.getElementById('fitFloor').click()");
  await check("三维全层仍保留检查点", "state.gap>=0 && state.gapScope==='floor'");
  await send("Emulation.setDeviceMetricsOverride", {width:1024,height:800,deviceScaleFactor:1,mobile:false});
  await evaluate("document.getElementById('gapContext').click()");
  await check("窄窗口说明不挡图", "document.querySelector('.detail').getBoundingClientRect().top >= document.querySelector('.stage').getBoundingClientRect().bottom-1");
  await shot("10_gap_narrow.png");
  await send("Emulation.setDeviceMetricsOverride", {width:1600,height:950,deviceScaleFactor:1,mobile:false});
  await evaluate("document.getElementById('viewPlan').click(); document.getElementById('issuesBtn').click(); document.querySelector('#issueList button').click()");
  await check("问题清单仍可定位", "state.gap>=0 && !document.getElementById('drawer').classList.contains('open')");
  await evaluate("document.getElementById('gapExit').click()");
  await check("退出恢复电缆详情", "state.gap===-1 && document.getElementById('detail').style.display!== 'none'");
  await check("返回路径也隐藏标记", "!state.showGaps && !svg.querySelector('[data-gap-index]') && document.getElementById('gapLegend').hidden");
  await evaluate("selectCable(data.cables.findIndex(c=>c.status==='OK')); state.floor='ALL'; renderFloorTabs(); renderMap(); focusGap(0); document.getElementById('showGaps').click()");
  await check("隐藏恢复原楼层及路径", "state.gap===-1 && state.floor==='ALL' && !state.showGaps && document.getElementById('gapInfo').style.display==='none' && !svg.querySelector('[data-gap-index]') && [...svg.querySelectorAll('line')].some(l=>l.getAttribute('stroke')==='var(--ok)')");
  await shot("11_gaps_hidden_plan.png");
  await evaluate("document.getElementById('showLabels').click(); document.getElementById('fitFloor').click()");
  await check("重绘不重新显示", "!svg.querySelector('[data-gap-index]') && !state.showGaps");
  await evaluate("document.getElementById('showGaps').click()");
  await check("重新勾选显示且不进入检查模式", "state.showGaps && state.gap===-1 && svg.querySelectorAll('[data-gap-index]').length===data.gaps.length && document.getElementById('detail').style.display!=='none'");
  await evaluate("document.getElementById('view3d').click(); focusGap(0); document.getElementById('showGaps').click()");
  await check("三维隐藏并移除点击命中", "!state.showGaps && state.gap===-1 && gapHits.length===0 && pickGap(canvas.clientWidth/2,canvas.clientHeight/2)===-1");
  await shot("12_gaps_hidden_3d.png");
  await evaluate("document.getElementById('showGaps').click()");
  await check("三维重新显示", "state.showGaps && gapHits.length===data.gaps.length");
  await evaluate("document.getElementById('showGaps').click(); document.getElementById('viewPlan').click()");
  await check("切换视图保持隐藏", "!state.showGaps && !svg.querySelector('[data-gap-index]')");
  await evaluate("document.getElementById('issuesBtn').click(); document.querySelector('#issueList button').click()");
  await check("清单定位自动重新显示", "state.showGaps && state.gap>=0 && document.getElementById('showGaps').checked && svg.querySelector('[data-gap-index]')");
  await evaluate("document.getElementById('gapExit').click()");
  report.gapChecks = gapCount;
}
report.errors = errors;
console.log(JSON.stringify(report, null, 1));
console.log(`截图：${outDir}`);
ws.close();
proc.kill();
if(errors.length) process.exitCode=1;
process.exit(errors.length ? 1 : 0);
