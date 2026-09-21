/* Original fixture and simulated APIs; never sends a real Feishu message. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const root = path.resolve(__dirname, "../src/files_to_feishu");
const receipt = {event: "succeeded", status: "failed", error: "缺少消息发送权限"};
const job = {
  id: "a".repeat(32), filename: "原创通知样例.zip", source_kind: "wechat", status: "succeeded",
  progress: "正文、附件与位置已核验", parsed: true, content_locked: true,
  url: "https://example.feishu.cn/wiki/sample", notifications: [receipt],
  preview: {source_kind: "wechat", metadata: {title: "原创通知样例"}, elements: [{kind: "text", text: "原创内容"}], notices: [], assets: []},
};
const mutations = [], errors = [];
const server = http.createServer(async (request, response) => {
  const files = {"/": ["templates/index.html", "text/html; charset=utf-8"], "/static/app.js": ["static/app.js", "application/javascript"], "/static/style.css": ["static/style.css", "text/css"], "/static/icon.svg": ["static/icon.svg", "image/svg+xml"]};
  const file = files[request.url];
  if (!file) return response.writeHead(404).end();
  response.writeHead(200, {"Content-Type": file[1]}); response.end(await fs.readFile(path.join(root, file[0])));
});
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()).pathname;
      if (request.method() === "POST") {
        mutations.push(url);
        assert.equal(url, `/api/jobs/${job.id}/notifications/succeeded/retry`);
        Object.assign(receipt, {status: "pending", error: ""});
      }
      const data = url === "/api/health" ? {feishu_configured: true, notifications_enabled: true, default_target: "https://example.feishu.cn/wiki/parent"} : url === "/api/jobs" ? [job] : job;
      await route.fulfill({json: structuredClone(data)});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator("#history").selectOption(job.id);
    await page.getByRole("button", {name: "仅重试通知"}).waitFor();
    assert.match(await page.locator("#result").textContent(), /保存成功/);
    assert.match(await page.locator("#notifications").textContent(), /缺少消息发送权限/);
    await page.getByRole("button", {name: "仅重试通知"}).click();
    await page.waitForFunction(() => document.querySelector("#notifications").textContent.includes("等待发送"));
    Object.assign(receipt, {status: "sent", message_id: "om_example"});
    await page.waitForFunction(() => document.querySelector("#notifications").textContent.includes("已发送"));
    assert.equal(mutations.length, 1);
    assert.match(await page.locator("#result a").getAttribute("href"), /wiki\/sample/);
    Object.assign(receipt, {status: "uncertain", error: "请在飞书核对，未自动重发。"});
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#notifications").textContent.includes("发送结果待核对"));
    assert.equal(await page.getByRole("button", {name: "仅重试通知"}).count(), 0);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await fs.mkdir("output/playwright", {recursive: true});
    await page.screenshot({path: "output/playwright/notifications-mobile.png", fullPage: true});
    assert.deepEqual(errors, []);
    console.log("Notification UI: separate status, retry-only, pending polling, uncertain no-retry and mobile passed.");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
