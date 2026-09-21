/* Isolated browser interaction test: local static UI, simulated APIs, no Feishu writes. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");

const root = path.resolve(__dirname, "../src/files_to_feishu");
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jkXcAAAAASUVORK5CYII=", "base64");
const articleUrl = "https://mp.weixin.qq.com/s/example";
const code = "function sample() {\n  return '<script>doNotRun()</script>';\n}";
const article = {
  source_kind: "wechat", pages: null, page_images: [],
  metadata: {url: articleUrl, title: "原创文章测试", account: "测试公众号", author: "作者", published_at: "2026-09-21"},
  assets: [{name: "web-1.png", media_type: "image/png", digest: "fixture"}],
  notices: [{page: null, reason: "装饰样式已简化，正文与图片完整保留。"}],
  elements: [
    {kind: "heading", level: 2, text: "可编辑内容"},
    {kind: "text", text: "粗体斜体删除代码安全链接危险链接<script>doNotRun()</script>", runs: [
      {text: "粗体", bold: true}, {text: "斜体", italic: true}, {text: "删除", strike: true},
      {text: "代码", inline_code: true}, {text: "安全链接", link: "https://example.com/read"},
      {text: "危险链接", link: "javascript:doNotRun()"}, {text: "<script>doNotRun()</script>"},
    ]},
    {kind: "ordered", text: "第三项", list_depth: 0, list_start: 3},
    {kind: "text", text: "列表内的补充段落", list_depth: 1},
    {kind: "image", asset: "web-1.png", text: "原创图片", list_depth: 1},
    {kind: "code", text: code, language: "javascript", code_origin: "html", code_reviewed: true, list_depth: 1},
    {kind: "bullet", text: "嵌套项目", list_depth: 1, runs: [{text: "嵌套项目", bold: true}]},
    {kind: "text", text: "嵌套项目的补充段落", list_depth: 2},
    {kind: "ordered", text: "第七条细节", list_depth: 2, list_start: 7},
    {kind: "ordered", text: "第八条细节", list_depth: 2},
    {kind: "ordered", text: "第四项", list_depth: 0},
    {kind: "quote", text: "引用", runs: [{text: "引用", italic: true}]},
    {kind: "table", rows: [["表格链接", "值"]], table_runs: [[[{text: "表格链接", link: "https://example.com/table", bold: true}], [{text: "值"}]]]},
  ],
};
const pdfJob = {
  id: "pdf-old", filename: "旧任务.pdf", source_kind: "pdf", status: "ready", progress: "请核对预览后保存", parsed: true,
  content_revision: 1, review_token: "1".repeat(64),
  preview: {pages: 1, page_images: ["page-1.png"], notices: [{page: 1, reason: "PDF 原有提示"}], elements: [
    {kind: "heading", page: 1, level: 2, text: "旧 PDF 标题"},
    {kind: "ordered", page: 1, text: "原列表一"}, {kind: "ordered", page: 1, text: "原列表二"},
  ]},
};
const jobs = new Map([[pdfJob.id, pdfJob]]);
const mutations = [];
let nextJob = 0, finishCancellation = false;
const server = http.createServer(async (request, response) => {
  const files = {"/": ["templates/index.html", "text/html; charset=utf-8"], "/static/app.js": ["static/app.js", "application/javascript"], "/static/style.css": ["static/style.css", "text/css"], "/static/icon.svg": ["static/icon.svg", "image/svg+xml"]};
  const file = files[request.url];
  if (!file) { response.writeHead(404).end(); return; }
  response.writeHead(200, {"Content-Type": file[1]}); response.end(await fs.readFile(path.join(root, file[0])));
});

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      const body = request.method() === "POST" ? request.postDataJSON() : {};
      const fulfill = data => route.fulfill({json: structuredClone(data)});
      if (pathname.endsWith(".png")) return route.fulfill({body: pixel, contentType: "image/png"});
      if (pathname === "/api/health") return fulfill({feishu_configured: true, models_ready: false, default_target: "https://test.feishu.cn/wiki/parent", max_bytes: 20971520});
      if (pathname === "/api/jobs" && request.method() === "GET") return fulfill([...jobs.values()]);
      if (pathname === "/api/jobs/wechat") {
        assert.equal(body.url, articleUrl);
        const job = {id: `article-${++nextJob}`, source_kind: "wechat", source_url: body.url, filename: "公众号文章", status: "waiting_verification", progress: "请完成浏览器验证", parsed: false};
        jobs.set(job.id, job); return fulfill(job);
      }
      if (pathname === "/api/targets/resolve") return fulfill({title: "父页面", host: "test.feishu.cn", node_token: "parent", space_id: "space"});
      const [, , , id, action] = pathname.split("/");
      const job = jobs.get(id);
      if (!job) return route.fulfill({status: 404, json: {detail: "任务不存在"}});
      if (request.method() === "GET") {
        if (job.status === "cancelling" && finishCancellation) Object.assign(job, {status: "cancelled", progress: "获取已取消"});
        return fulfill(job);
      }
      mutations.push({action, body, id});
      if (action === "continue") Object.assign(job, {status: "ready", progress: "请核对完整正文", parsed: true, preview: structuredClone(article), content_revision: 1, review_token: "1".repeat(64)});
      else if (action === "cancel") Object.assign(job, {status: "cancelling", progress: "正在关闭临时验证会话"});
      else if (action === "code") {
        assert.equal(body.expected_revision, job.content_revision);
        Object.assign(job.preview.elements[Number(pathname.split("/")[5])], {text: body.text, language: body.language, code_reviewed: true});
        job.content_revision++;
        job.review_token = String(job.content_revision).repeat(64);
      } else if (action === "review") {
        assert.equal(body.expected_revision, job.content_revision);
        assert.equal(body.review_token, job.review_token);
        return fulfill({confirmation_token: "a".repeat(64), content_revision: job.content_revision});
      }
      else if (action === "publish") {
        assert.equal(body.confirmed, true);
        assert.equal(body.review_token, job.review_token);
        assert.equal(body.confirmation_token, "a".repeat(64));
        Object.assign(job, {status: "succeeded", progress: "已保存", url: "https://test.feishu.cn/wiki/article", document_id: "doc", content_locked: true});
      } else throw new Error(`Unexpected API ${pathname}`);
      return fulfill(job);
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(() => document.querySelector("#history option[value='pdf-old']"));
    await page.locator("#history").selectOption("pdf-old");
    await page.locator(".comparison").waitFor();
    assert.equal(await page.locator(".comparison").count(), 1);
    assert.equal(await page.locator(".converted ol li").count(), 2);
    assert.match(await page.locator("#notices").textContent(), /第 1 页/);
    assert.equal(await page.locator("#original").textContent(), "下载原 PDF");

    await page.locator("#source-kind").selectOption("wechat");
    assert.equal(await page.locator("#file").isDisabled(), true);
    assert.equal(await page.locator("#setup").isVisible(), false);
    await page.locator("#article-url").fill(articleUrl);
    await page.locator("#convert").click();
    await page.locator("#verification").waitFor();
    assert.equal(await page.locator("#continue-fetch").isEnabled(), true);
    assert.equal(await page.locator("#cancel-fetch").isVisible(), true);
    assert.equal(await page.locator("#original").isVisible(), false);
    assert.equal(await page.locator("#confirmed").isDisabled(), true);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.locator("#continue-fetch").click();
    await page.locator(".article-preview").waitFor();
    assert.equal(await page.locator(".page-label").count(), 0);
    assert.equal(await page.locator(".comparison").count(), 0);
    assert.equal(await page.locator(".article-preview p strong").textContent(), "粗体");
    assert.equal(await page.locator(".article-preview p em").textContent(), "斜体");
    assert.equal(await page.locator(".article-preview p s").textContent(), "删除");
    assert.equal(await page.locator(".article-preview p code").textContent(), "代码");
    assert.equal(await page.locator(".article-preview p a").count(), 1);
    assert.equal(await page.locator(".article-preview script").count(), 0);
    assert.equal(await page.locator(".article-preview > ol").getAttribute("start"), "3");
    assert.equal(await page.locator(".article-preview > ol > li").count(), 2);
    assert.equal(await page.locator(".article-preview > ol > li > ul > li strong").textContent(), "嵌套项目");
    const parentItem = page.locator(".article-preview > ol > li").first();
    assert.deepEqual(await parentItem.evaluate(li => [...li.children].map(node => node.tagName)), ["P", "FIGURE", "SECTION", "UL"]);
    assert.equal(await parentItem.locator(":scope > p").textContent(), "列表内的补充段落");
    assert.equal(await parentItem.locator(":scope > figure img").getAttribute("alt"), "原创图片");
    assert.equal(await parentItem.locator(":scope > section textarea").inputValue(), code);
    const nestedItem = parentItem.locator(":scope > ul > li");
    assert.deepEqual(await nestedItem.evaluate(li => [...li.children].map(node => node.tagName)), ["STRONG", "P", "OL"]);
    assert.equal(await nestedItem.locator(":scope > ol").getAttribute("start"), "7");
    assert.deepEqual(await nestedItem.locator(":scope > ol > li").allTextContents(), ["第七条细节", "第八条细节"]);
    assert.equal(await page.locator(".article-preview > ol > li").last().textContent(), "第四项");
    assert.equal(await page.locator(".article-preview > figure, .article-preview > section").count(), 0);
    assert.equal(await page.locator(".article-preview blockquote em").textContent(), "引用");
    assert.equal(await page.locator(".article-preview td a strong").textContent(), "表格链接");
    assert.equal(await page.locator(".code-editor textarea").inputValue(), code);
    assert.equal(await page.locator(".code-reference").count(), 0);
    assert.equal(await page.locator("#original").textContent(), "下载文章归档 ZIP");
    assert.doesNotMatch(await page.locator("#notices").textContent(), /第 null 页|第 undefined 页/);
    await page.locator(".article-metadata summary").click();
    assert.match(await page.locator(".article-metadata").textContent(), /作者：作者/);
    assert.equal(await page.locator("#confirmed").isChecked(), false);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    const editedCode = code + "\n// 已校对";
    await page.locator(".code-editor textarea").fill(editedCode);
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    await page.locator("#confirmed").check();
    await page.waitForFunction(() => !document.querySelector("#confirmed").disabled);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.locator(".code-save").click();
    await page.locator(".code-save").waitFor({state: "hidden"});
    assert.equal(await page.locator(".code-editor textarea").inputValue(), editedCode);
    assert.equal(await page.locator("#confirmed").isChecked(), false);
    await page.locator("#confirmed").check();
    await page.waitForFunction(() => !document.querySelector("#confirmed").disabled);
    await page.locator("#publish").click();
    await page.locator("#result a").waitFor();
    assert.equal(mutations.filter(x => x.action === "publish").length, 1);
    assert.equal(await page.locator(".code-editor textarea").isEditable(), false);
    const reopenedId = await page.locator("#history").inputValue();
    await page.reload();
    await page.locator(`#history option[value="${reopenedId}"]`).waitFor({state: "attached"});
    await page.locator("#history").selectOption(reopenedId);
    await page.locator(".article-preview").waitFor();
    assert.equal(await page.locator("#confirmed").isChecked(), false);
    await fs.mkdir("output/playwright", {recursive: true});
    await page.screenshot({path: "output/playwright/wechat-preview.png", fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: "output/playwright/wechat-mobile.png", fullPage: true});

    await page.locator("#article-url").fill(articleUrl);
    await page.locator("#convert").click();
    await page.locator("#verification").waitFor();
    await page.locator("#cancel-fetch").click();
    await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("正在取消"));
    assert.equal(await page.locator("#cancel-fetch").isDisabled(), true);
    assert.equal(await page.locator("#convert").isDisabled(), true);
    assert.equal(await page.locator("#source-kind").isDisabled(), true);
    assert.equal(await page.locator("#history").isDisabled(), true);
    assert.equal(await page.locator("#confirmed").isDisabled(), true);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    assert.equal(mutations.filter(x => x.action === "cancel").length, 1);
    finishCancellation = true;
    await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("已取消"));
    assert.equal(await page.locator("#verification").isVisible(), false);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    assert.equal(await page.locator("#convert").isEnabled(), true);
    assert.deepEqual(errors, []);
    console.log("PASS: legacy PDF preview; WeChat verification/continue/async cancellation; continuous rich text, nested lists with paragraphs/images/code, quotes, table, safe links; explicit confirmation, refresh and mobile.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
