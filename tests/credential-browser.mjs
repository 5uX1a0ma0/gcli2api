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
const settings = {
  free_account_priority_enabled: true, credential_select_mode: "cache",
  auto_ban_error_codes: [403], retry_error_codes: [400, 403, 429, 500, 503],
  request_log_error_codes: [400], request_log_enabled: true,
};
let envLocked = [], dedupScenario = "partial";
const dedupRequests = [];
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
    if (url.pathname === "/config/get") return send({ config: settings, env_locked: envLocked });
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
    if (url.pathname === "/creds/deduplicate") {
      assert.equal(req.method, "POST");
      let raw = ""; for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw); dedupRequests.push(body);
      if (dedupScenario === "slow") await delay(2000);
      if (dedupScenario === "error") return send({ detail: "扫描失败，请重试" }, 500);
      if (!body.dry_run && dedupScenario === "conflict") {
        return send({ detail: "凭证已变化，请重新扫描后再去重" }, 409);
      }
      const scope = body.emails ?? accounts.map((account) => account.email);
      const duplicates = scope.length > 8 ? [scope[1], scope[8]] : [scope[1]];
      const groups = dedupScenario === "empty" || scope.length < 2 ? [] : [{
        actual_email: "actual-account-with-a-long-identifier-and-many-segments@example.test",
        keep_email: scope[0], duplicate_emails: duplicates,
      }];
      const errors = dedupScenario !== "empty" && scope.length > 2
        ? [{ email: scope[2], message: "查询实际邮箱失败，已保留凭证" }] : [];
      const duplicateCount = groups.length ? duplicates.length : 0;
      if (!body.dry_run) {
        assert.equal(body.preview, "test-preview");
        for (const group of groups) for (const email of group.duplicate_emails) {
          accounts.splice(accounts.findIndex((account) => account.email === email), 1);
        }
      }
      return send({ dry_run: body.dry_run, total_count: scope.length,
        verified_count: scope.length - errors.length, duplicate_count: duplicateCount,
        removed_count: body.dry_run ? 0 : duplicateCount, groups, errors, preview: "test-preview" });
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
  const listReady = () => waitFor(() => evaluate(`document.querySelectorAll('input[aria-label^=\"选择凭证 \" ]').length===${accounts.length}`), "credential list");
  const select = async (count) => {
    await evaluate(`document.querySelectorAll('input[aria-label^="选择凭证 "]').forEach((i,index)=>{if(index<${count}&&!i.checked)i.click();})`);
    await waitFor(() => hasText(`已选 ${count} 项`), "selection");
  };
  const viewport = (width, height, mobile = false) => cdp("Emulation.setDeviceMetricsOverride", { width, height, mobile, deviceScaleFactor: 1 });
  const capture = async (name, full = false, target = "[role=progressbar]") => {
    if (full) await evaluate("window.scrollTo(0,0)");
    else await evaluate(`document.querySelector(${JSON.stringify(target)})?.scrollIntoView({block:'center'})`);
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

  // All error-code fields preserve invalid drafts, block save, and normalize valid input.
  await viewport(1440, 1000); await click("配置管理");
  await waitFor(() => evaluate("!!document.getElementById('retry_error_codes')"), "retry code setting");
  const inputValue = async (id, value) => {
    await evaluate(`(()=>{const input=document.getElementById(${JSON.stringify(id)});input.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});
      input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await waitFor(() => evaluate(`document.getElementById(${JSON.stringify(id)}).value===${JSON.stringify(value)}`), "input draft");
  };
  const saveCount = saves.length;
  for (const id of ["auto_ban_error_codes", "retry_error_codes", "request_log_error_codes"]) {
    await inputValue(id, "[403, 429");
    assert.equal(await evaluate(`document.getElementById(${JSON.stringify(id)}).getAttribute('aria-invalid')`), "true");
    assert.equal(await evaluate(`${button("保存配置")}.disabled`), true);
    await evaluate(`document.getElementById(${JSON.stringify(id)}).blur()`);
    assert.equal(await evaluate(`document.getElementById(${JSON.stringify(id)}).value`), "[403, 429");
  }
  await capture("desktop-code-errors", false, "#request_log_error_codes");
  assert.equal(saves.length, saveCount);
  await inputValue("auto_ban_error_codes", "403,400,403");
  await inputValue("retry_error_codes", "403,");
  assert.equal(await evaluate("document.getElementById('retry_error_codes').value"), "403,");
  await inputValue("retry_error_codes", "[502,429,502]");
  await inputValue("request_log_error_codes", "400oops,429");
  assert.equal(await evaluate("document.getElementById('request_log_error_codes').getAttribute('aria-invalid')"), "true");
  await viewport(390, 844, true); await capture("mobile-code-error", false, "#request_log_error_codes");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true);
  await inputValue("request_log_error_codes", "[429,400]");
  assert.equal(await evaluate(`${button("保存配置")}.disabled`), false);
  await click("保存配置"); await waitFor(() => hasText("配置保存成功"), "normalized codes saved");
  assert.deepEqual(saves.at(-1).auto_ban_error_codes, [400, 403]);
  assert.deepEqual(saves.at(-1).retry_error_codes, [429, 502]);
  assert.deepEqual(saves.at(-1).request_log_error_codes, [400, 429]);
  await click("关闭");
  assert.equal(await evaluate("document.getElementById('retry_error_codes').value"), "429, 502");
  await capture("mobile-codes-valid", false, "#retry_error_codes");
  await inputValue("retry_error_codes", "[]");
  assert(await hasText("空列表，不按状态码自动重试"));
  await inputValue("request_log_error_codes", "");
  assert(await hasText("留空使用默认错误码 400"));
  await click("保存配置"); await waitFor(() => hasText("配置保存成功"), "empty codes saved");
  assert.deepEqual(saves.at(-1).retry_error_codes, []);
  await click("关闭");
  envLocked = ["retry_error_codes"];
  await click("刷新配置");
  await waitFor(() => hasText("该配置已被环境变量锁定"), "environment locked codes");
  assert.equal(await evaluate("document.getElementById('retry_error_codes') == null"), true);
  assert(await hasText("该配置已被环境变量锁定"));
  envLocked = []; await click("刷新配置");
  await waitFor(() => evaluate("!!document.getElementById('retry_error_codes')"), "unlocked codes");

  // Scan selected records first; handle conflict, cancellation, errors and empty results.
  await viewport(1440, 1000); await click("凭证管理"); await listReady(); await select(3);
  await click("邮箱去重");
  assert.equal(await evaluate("document.querySelector('input[name=deduplication-scope][value=selected]').checked"), true);
  await click("扫描重复凭证"); await waitFor(() => hasText("可清理 1 项"), "selected preview");
  assert.deepEqual(dedupRequests.at(-1).emails, accounts.slice(0, 3).map((account) => account.email));
  assert.equal(accounts.length, 12); assert(await hasText("保留：account-0@example.test"));
  await capture("desktop-dedup-preview", false, "#credential-deduplication");
  await select(4); await waitFor(() => hasText("所选凭证已变化，请重新扫描"), "changed selection");
  assert.equal(await evaluate(`${button("确认去重（1 项）")} == null`), true);
  await evaluate("document.querySelectorAll('input[aria-label^=\"选择凭证 \" ]')[3].click()");
  await waitFor(() => hasText("已选 3 项"), "restored selection");
  await click("扫描重复凭证"); await waitFor(() => hasText("可清理 1 项"), "updated selected preview");
  dedupScenario = "conflict"; await click("确认去重（1 项）");
  await waitFor(() => hasText("凭证已变化，请重新扫描后再去重"), "stale preview");
  assert.equal(await evaluate(`${button("确认去重（1 项）")} == null`), true);
  assert.equal(accounts.length, 12); await capture("desktop-dedup-conflict", false, "#credential-deduplication");
  dedupScenario = "slow"; const beforeSlow = dedupRequests.length;
  await click("扫描重复凭证"); await waitFor(() => dedupRequests.length === beforeSlow + 1, "slow scan");
  assert.equal(await evaluate(`${button("刷新状态")}.disabled`), true);
  assert.equal(await evaluate("[...document.querySelectorAll('button[aria-haspopup=listbox]')].every(b=>b.disabled)"), true);
  await capture("desktop-dedup-running", false, "#credential-deduplication");
  await click("停止扫描"); await waitFor(() => hasText("扫描已停止，没有清理任何凭证"), "scan cancelled");
  assert.equal(accounts.length, 12);
  dedupScenario = "error"; await click("扫描重复凭证");
  await waitFor(() => hasText("扫描失败，请重试"), "scan error");
  dedupScenario = "empty"; await click("扫描重复凭证");
  await waitFor(() => hasText("未发现可清理的重复凭证"), "no duplicates");
  assert.equal(await evaluate(`${button("确认去重（1 项）")} == null`), true);
  dedupScenario = "partial";
  await evaluate("document.querySelector('input[name=deduplication-scope][value=all]').click()");
  await viewport(390, 844, true); await click("扫描重复凭证");
  await waitFor(() => hasText("可清理 2 项"), "all records preview");
  assert.equal(dedupRequests.at(-1).emails, null);
  await evaluate("document.querySelector('#credential-deduplication details').open=true");
  assert(await hasText("查询实际邮箱失败，已保留凭证"));
  await capture("mobile-dedup-preview", false, "#credential-deduplication");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true);
  const beforeApply = summaries;
  await click("确认去重（2 项）"); await waitFor(() => hasText("邮箱去重完成，已清理 2 项"), "cleanup complete");
  await listReady(); assert.equal(accounts.length, 10); assert.equal(summaries, beforeApply + 1);
  assert(await hasText("已选 0 项"));
  assert.equal(dedupRequests.at(-1).dry_run, false); assert.equal(dedupRequests.at(-1).preview, "test-preview");
  const resultVisible = await evaluate("(()=>{const node=[...document.querySelectorAll('#credential-deduplication [role=status] span')].find(n=>n.textContent.includes('邮箱去重完成'));const rect=node.getBoundingClientRect();return rect.top>=0&&rect.bottom<=innerHeight})()");
  assert.equal(resultVisible, true);
  await capture("mobile-dedup-complete", false, "#credential-deduplication");
  await viewport(844, 390, true); await capture("landscape-dedup-complete", false, "#credential-deduplication");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true);
  await evaluate("document.querySelector('button[aria-label=\"关闭邮箱去重\"]').click()");
  assert.equal(await evaluate(`${button("邮箱去重")}.getAttribute('aria-expanded')`), "false");
  empty = true; await click("刷新状态"); await waitFor(() => hasText("没有凭证"), "empty list");
  assert.deepEqual(unexpected, []); assert.deepEqual(exceptions, []);
  const report = { passed: true, checked: checks.length, peakConcurrency: peak, summaries,
    configSaves: saves.length, deduplicationRequests: dedupRequests.length, captures,
    states: ["selected", "running", "partial failure", "stopped", "unmount", "saved setting", "keyboard", "mobile", "landscape", "reduced motion", "empty",
      "invalid code drafts", "normalized code save", "empty codes", "environment lock",
      "selected deduplication", "changed selection", "all deduplication", "dedup preview", "conflict", "scan cancellation", "scan error", "no duplicates", "cleanup"] };
  await writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2));
} finally {
  socket?.close(); browser.kill(); await new Promise((resolve) => server.close(resolve));
}
