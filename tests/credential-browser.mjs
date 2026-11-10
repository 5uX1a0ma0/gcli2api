/** Run after pnpm build; uses the existing Chromium and mocked panel endpoints. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = process.env.GCLI_BROWSER_PROOF_DIR || "/tmp/gcli-credential-browser-proof";
await mkdir(output, { recursive: true });
const accounts = Array.from({ length: 12 }, (_, index) => ({
  email: index === 8 ? "very-long-account-identifier-with-1234567890-and-many-segments@example.test"
    : `account-${index}@example.test`,
  disabled: index === 3, error_codes: index === 3 ? [403] : [], model_cooldowns: {},
  tier: "pro", project_id: "original-project", paid: index % 2 === 0,
  paid_checked_at: Date.now() / 1000,
}));
const settings = { free_account_priority_enabled: true, credential_select_mode: "cache" };
let scenario = "partial", active = 0, peak = 0, summaries = 0, empty = false;
const checks = [], saves = [], unexpected = [], exceptions = [], captures = [];
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const send = (body, status = 200) => {
    if (!res.destroyed) { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(body)); }
  };
  try {
    if (url.pathname === "/version/info") return send({ version: "test" });
    if (url.pathname === "/config/get") return send({ config: settings, env_locked: [] });
    if (url.pathname === "/config/save") {
      let body = ""; for await (const chunk of req) body += chunk;
      const config = JSON.parse(body).config; saves.push(config); Object.assign(settings, config);
      return send({ message: "配置保存成功", saved_config: config });
    }
    if (url.pathname === "/creds/status") {
      summaries++;
      return send({ items: empty ? [] : accounts, total: empty ? 0 : accounts.length,
        stats: { total: accounts.length, normal: accounts.filter((account) => !account.disabled).length,
          disabled: accounts.filter((account) => account.disabled).length } });
    }
    if (url.pathname.startsWith("/creds/verify-project/")) {
      assert.equal(req.method, "POST");
      const email = decodeURIComponent(url.pathname.slice("/creds/verify-project/".length));
      const index = accounts.findIndex((account) => account.email === email); assert(index >= 0);
      checks.push(email); peak = Math.max(peak, ++active); const current = scenario;
      await delay(current === "partial" ? (index < 4 ? 350 : 1400) : 650); active--;
      if (current === "partial" && index === 1) return send({ detail: "上游暂时不可用" }, 500);
      if (current === "partial" && index === 2) return send({ success: false, error: "Project ID 检验失败" });
      Object.assign(accounts[index], { project_id: `verified-${index}`, tier: "ultra", disabled: false, error_codes: [] });
      return send({ success: true, email, project_id: accounts[index].project_id, subscription_tier: "ultra" });
    }
    if (url.pathname.startsWith("/creds/test/") || url.pathname.startsWith("/creds/model-status/")) {
      unexpected.push(url.pathname); return send({ detail: "Unexpected model request" }, 500);
    }
    const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    const data = await readFile(path.join(root, "dist", name));
    res.writeHead(200, { "content-type": name.endsWith(".js") ? "text/javascript"
      : name.endsWith(".css") ? "text/css" : "text/html" }); res.end(data);
  } catch (error) { send({ detail: String(error) }, 500); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = spawn(process.env.CHROMIUM_BINARY || "chromium", [
  "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  "--disable-background-networking", "--remote-debugging-port=0",
  `--user-data-dir=${output}/browser-${process.pid}`, "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
let browserLog = ""; browser.stderr.on("data", (chunk) => { browserLog += chunk; });
let socket;
try {
  const deadline = Date.now() + 15000;
  while (!/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/.test(browserLog)) {
    if (Date.now() > deadline || browser.exitCode != null) throw Error(`Browser launch failed: ${browserLog}`);
    await delay(100);
  }
  const port = browserLog.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/)[1];
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(tabs.find((tab) => tab.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve); socket.addEventListener("error", reject); });
  let sequence = 0; const pending = new Map();
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    const request = pending.get(message.id);
    if (request) { pending.delete(message.id); message.error ? request.reject(message.error) : request.resolve(message.result); }
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const waitFor = async (condition, label) => {
    const until = Date.now() + 15000;
    while (Date.now() < until) { if (await condition()) return; await delay(50); }
    throw Error(`Timeout: ${label}`);
  };
  const hasText = (text) => evaluate(`document.body?.innerText.includes(${JSON.stringify(text)}) ?? false`);
  const button = (text) => `[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(text)})`;
  const click = (text) => evaluate(`(()=>{const b=${button(text)};if(!b||b.disabled)throw Error('Unavailable button');b.click();})()`);
  const listReady = () => waitFor(() => evaluate("document.querySelectorAll('input[aria-label^=\"选择凭证 \" ]').length===12"), "credential list");
  const select = async (count) => {
    await evaluate(`document.querySelectorAll('input[aria-label^="选择凭证 "]').forEach((i,index)=>{if(index<${count}&&!i.checked)i.click();})`);
    await waitFor(() => hasText(`已选 ${count} 项`), "selection");
  };
  const viewport = (width, height, mobile = false) => cdp("Emulation.setDeviceMetricsOverride", { width, height, mobile, deviceScaleFactor: 1 });
  const capture = async (name, full = false) => {
    if (full) await evaluate("window.scrollTo(0,0)");
    else await evaluate("document.querySelector('[role=progressbar]')?.scrollIntoView({block:'center'})");
    await delay(250);
    const params = { format: "png", captureBeyondViewport: full };
    if (full) { const { cssContentSize: size } = await cdp("Page.getLayoutMetrics");
      params.clip = { x: 0, y: 0, width: size.width, height: size.height, scale: 1 }; }
    const data = await cdp("Page.captureScreenshot", params);
    const file = path.join(output, `${name}.png`); await writeFile(file, Buffer.from(data.data, "base64")); captures.push(file);
  };
  await cdp("Runtime.enable"); await cdp("Page.enable");
  await cdp("Page.addScriptToEvaluateOnNewDocument", { source: "localStorage.setItem('panelToken','browser-test')" });
  await viewport(1440, 1000); await cdp("Page.navigate", { url: `http://127.0.0.1:${server.address().port}/` });
  await waitFor(() => hasText("凭证管理"), "navigation"); await click("凭证管理"); await listReady();
  assert.equal(await evaluate(`${button("批量检验")}.disabled`), true);
  await capture("desktop-before", true); await select(8); const before = summaries;
  await click("批量检验"); await waitFor(() => checks.length === 4, "first workers");
  assert.equal(await evaluate(`${button("刷新状态")}.disabled`), true);
  assert.equal(await evaluate("[...document.querySelectorAll('button[aria-haspopup=listbox]')].every(b=>b.disabled)"), true);
  await waitFor(() => evaluate("Number(document.querySelector('[role=progressbar]')?.getAttribute('aria-valuenow'))===4"), "progress");
  await capture("desktop-running"); await waitFor(() => hasText("批量检验完成"), "completion");
  assert.equal(checks.length, 8); assert.equal(peak, 4); assert.equal(summaries, before + 1);
  assert(await hasText("成功 6")); assert(await hasText("失败 2")); assert.equal(accounts[10].project_id, "original-project");
  await click("查看检验结果"); assert(await hasText("上游暂时不可用")); assert(await hasText("Project ID 检验失败"));
  assert.equal(await evaluate("document.querySelectorAll('#batch-verification-results li').length"), 8);
  await capture("desktop-results");
  scenario = "success"; const beforeStop = checks.length; await select(12); await click("批量检验");
  await waitFor(() => checks.length === beforeStop + 4, "stop workers"); await click("停止后续检验");
  await waitFor(() => hasText("批量检验已停止"), "stopping"); assert.equal(checks.length, beforeStop + 4); assert(await hasText("未检验 8"));
  await capture("desktop-stopped");
  const beforeUnmount = checks.length; await select(12); await click("批量检验");
  await waitFor(() => checks.length === beforeUnmount + 4, "unmount workers"); await click("批量导入");
  await delay(900); assert.equal(checks.length, beforeUnmount + 4);
  await click("配置管理"); const toggle = "document.querySelector('[role=switch][aria-label=\"免费账户优先\"]')";
  await waitFor(() => evaluate(`!!${toggle}`), "priority toggle"); assert.equal(await evaluate(`${toggle}.getAttribute('aria-checked')`), "true");
  await evaluate(`${toggle}.click()`); await waitFor(() => evaluate(`${toggle}.getAttribute('aria-checked')==='false'`), "toggle changed");
  await click("保存配置"); await waitFor(() => hasText("配置保存成功"), "saved"); assert.equal(saves[0].free_account_priority_enabled, false);
  await click("关闭"); await click("凭证管理"); await listReady(); await click("配置管理");
  await waitFor(() => evaluate(`${toggle}?.getAttribute('aria-checked')==='false'`), "saved reentry");
  await evaluate(`${toggle}.focus()`); await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
  await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
  await waitFor(() => evaluate(`${toggle}.getAttribute('aria-checked')==='true'`), "keyboard toggle");
  await viewport(390, 844, true); await evaluate(`${toggle}.scrollIntoView({block:'center'})`); await capture("mobile-settings");
  await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  await click("凭证管理"); await listReady(); await select(12); const beforeMobile = checks.length;
  await click("批量检验"); await waitFor(() => checks.length === beforeMobile + 4, "mobile workers");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('[role=progressbar]>div')).transitionProperty"), "none");
  assert.equal(await evaluate("[...document.querySelectorAll('svg.animate-spin')].every(svg=>getComputedStyle(svg).animationName==='none')"), true);
  await capture("mobile-running"); await waitFor(() => hasText("批量检验完成"), "mobile done"); await click("查看检验结果");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true); await capture("mobile-results", true);
  await viewport(844, 390, true); await capture("landscape-results"); assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true);
  empty = true; await click("刷新状态"); await waitFor(() => hasText("没有凭证"), "empty list");
  assert.deepEqual(unexpected, []); assert.deepEqual(exceptions, []);
  const report = { passed: true, checked: checks.length, peakConcurrency: peak, summaries, captures,
    states: ["selected", "running", "partial failure", "stopped", "unmount", "saved setting", "keyboard", "mobile", "landscape", "reduced motion", "empty"] };
  await writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2));
} finally {
  socket?.close(); browser.kill(); await new Promise((resolve) => server.close(resolve));
}
