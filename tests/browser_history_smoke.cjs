/* Original fixtures, simulated API; never removes real tasks or contacts Feishu. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const root = path.resolve(__dirname, "../src/files_to_feishu");
const batch = "b".repeat(32), selected = "a".repeat(32), processing = "c".repeat(32), sending = "d".repeat(32);
const make = (id, status, extra = {}) => ({id, status, filename: `样例-${id[0]}.zip`, source_kind: "wechat", parsed: true, progress: "等待核对", notifications: [], preview: {source_kind: "wechat", metadata: {title: "原创标题"}, elements: [{kind: "text", text: "原创文章内容"}], notices: [], assets: []}, ...extra});
const jobs = new Map([
  [selected, make(selected, "ready", {batch_id: batch})],
  [processing, make(processing, "fetching")],
  [sending, make(sending, "succeeded", {notifications: [{event: "succeeded", status: "sending"}]})],
]);
let rejectDelete = false;
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
      const req = route.request(), url = new URL(req.url()).pathname;
      if (url === "/api/health") return route.fulfill({json: {feishu_configured: true, models_ready: true, default_target: "https://test.feishu.cn/wiki/old", notifications_enabled: true}});
      if (url === "/api/jobs") return route.fulfill({json: [...jobs.values()]});
      const id = url.split("/")[3];
      if (req.method() === "DELETE") {
        mutations.push(id);
        if (rejectDelete) return route.fulfill({status: 400, json: {detail: "任务正在处理中"}});
        jobs.delete(id); return route.fulfill({json: {deleted: true}});
      }
      return route.fulfill(jobs.has(id) ? {json: jobs.get(id)} : {status: 404, json: {detail: "任务已删除"}});
    });
    const base = `http://127.0.0.1:${server.address().port}`;
    await page.goto(base);
    await page.locator(".queue-row").first().waitFor();
    await page.evaluate(({selected, batch}) => {
      localStorage.setItem("pdf-target", "https://test.feishu.cn/wiki/old");
      localStorage.setItem("pdf-job", selected); localStorage.setItem("content-batch", batch);
      localStorage.setItem("unrelated-setting", "keep");
    }, {selected, batch});
    await page.locator("#title").fill("私密旧标题");
    await page.locator("#target").fill("https://test.feishu.cn/wiki/old");
    await page.locator("#source-kind").selectOption("wechat");
    await page.locator("#article-url").fill("https://mp.weixin.qq.com/s/old");
    await page.reload();
    await page.locator(".queue-row").first().waitFor();
    for (const id of ["title", "target", "article-url"]) {
      assert.equal(await page.locator(`#${id}`).inputValue(), "");
      assert.equal(await page.locator(`#${id}`).getAttribute("autocomplete"), "off");
    }
    assert.equal(await page.locator("#history").inputValue(), "");
    assert.equal(await page.locator("#batch-filter").inputValue(), "");
    assert.equal(await page.locator("#empty").isVisible(), true);
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), ["unrelated-setting"]);
    assert.equal(await page.locator(`[data-job-id="${processing}"] .delete-job`).isDisabled(), true);
    assert.equal(await page.locator(`[data-job-id="${sending}"] .delete-job`).isDisabled(), true);
    await page.locator("#batch-filter").selectOption(batch);
    await page.locator("#history").selectOption(selected);
    await page.locator(".article-preview").waitFor();
    await page.locator("#confirmed").check();
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    await page.locator("#confirmed").check();
    assert.match(await page.locator("#publish-batch").textContent(), /（1）/);
    page.once("dialog", dialog => dialog.dismiss());
    await page.getByRole("button", {name: "删除", exact: true}).click();
    assert.equal(mutations.length, 0);
    rejectDelete = true;
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", {name: "删除", exact: true}).click();
    await page.locator("#error").waitFor();
    assert.match(await page.locator("#error").textContent(), /正在处理/);
    assert.equal(await page.locator(".article-preview").isVisible(), true);
    rejectDelete = false;
    page.once("dialog", dialog => { assert.match(dialog.message(), /本地素材和发布核验记录仍保留/); dialog.accept(); });
    await page.getByRole("button", {name: "删除", exact: true}).click();
    await page.waitForFunction(id => !document.querySelector(`[data-job-id="${id}"]`), selected);
    assert.equal(await page.locator("#empty").isVisible(), true);
    assert.equal(await page.locator("#title").inputValue(), "");
    assert.equal(await page.locator("#target").inputValue(), "");
    assert.equal(await page.locator("#original").isVisible(), false);
    assert.equal(await page.locator(`#history option[value="${selected}"]`).count(), 0);
    assert.equal(await page.locator(`#batch-filter option[value="${batch}"]`).count(), 0);
    assert.match(await page.locator("#publish-batch").textContent(), /（0）/);
    // A deletion in another tab clears an already-open preview on the next queue poll.
    jobs.set(processing, make(processing, "ready"));
    await page.locator("#history").selectOption(processing);
    await page.locator(".article-preview").waitFor();
    jobs.delete(processing);
    await page.locator("#empty").waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await fs.mkdir("output/playwright", {recursive: true});
    await page.screenshot({path: "output/playwright/history-mobile.png", fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    jobs.set(sending, make(sending, "succeeded"));
    await page.waitForFunction(() => !document.querySelector(".delete-job").disabled);
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", {name: "删除", exact: true}).click();
    await page.locator("#queue-panel").waitFor({state: "hidden"});
    await page.reload();
    await page.waitForFunction(() => !document.querySelector("#convert").disabled);
    assert.equal(await page.locator("#queue-panel").isVisible(), false);
    assert.deepEqual(errors, []);
    console.log("PASS: no remembered inputs; cancel/delete/reject; active/notification guards; batch/review cleanup; cross-tab deletion; empty history; mobile.");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
