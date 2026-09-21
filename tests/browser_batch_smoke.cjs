/* TEST_BATCH_PREVIEW=1 python -m tests.browser_server; isolated data and mock Feishu. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");

(async () => {
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:8766");
    const bytes = await fs.readFile("output/samples/representative.pdf");
    await page.locator("#file").setInputFiles([
      {name: "first.pdf", mimeType: "application/pdf", buffer: bytes},
      {name: "broken.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a PDF")},
      {name: "second.pdf", mimeType: "application/pdf", buffer: Buffer.concat([bytes, Buffer.from("\n% second original\n")])},
    ]);
    await page.locator("#convert").click();
    await page.waitForFunction(() => document.querySelector("#batch-message").textContent.includes("已提交 2/3"));
    await page.waitForFunction(() => document.querySelectorAll(".queue-row").length === 3 && document.querySelector("#queue-summary").textContent.includes("待确认 2"));
    assert.match(await page.locator("#batch-message").textContent(), /broken.pdf/);
    assert.equal(await page.locator("#publish-batch").isDisabled(), true);
    const open = async title => {
      await page.locator(".queue-row").filter({hasText: title}).getByRole("button", {name: "打开"}).click();
      await page.waitForFunction(() => !document.querySelector("#confirmed").disabled);
    };
    const oldPdfId = await page.locator(".queue-row").filter({hasText: "first.pdf"}).getAttribute("data-job-id");
    await open("first.pdf");
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    await page.locator("#title").fill("第一篇独立标题");
    await page.locator("#confirmed").check();
    await open("second.pdf");
    await page.locator("#title").fill("第二篇独立标题");
    await page.locator("#confirmed").check();
    assert.match(await page.locator("#publish-batch").textContent(), /（2）/);
    await open("first.pdf");
    assert.equal(await page.locator("#title").inputValue(), "第一篇独立标题");
    assert.equal(await page.locator("#confirmed").isChecked(), true);
    // Refresh keeps history, but clears inputs, selected task/batch and review confirmation.
    const pdfBatch = await page.locator("#batch-filter").inputValue();
    await page.reload();
    await page.locator(`#batch-filter option[value="${pdfBatch}"]`).waitFor({state: "attached"});
    assert.equal(await page.locator("#title").inputValue(), "");
    assert.equal(await page.locator("#target").inputValue(), "");
    await page.locator("#batch-filter").selectOption(pdfBatch);
    await open("first.pdf");
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    await page.locator(".comparison").first().waitFor();
    assert.equal(await page.locator(".queue-row").count(), 3);
    assert.equal(await page.locator("#confirmed").isChecked(), false);
    assert.equal(await page.locator("#publish-batch").isDisabled(), true);
    await page.locator("#confirmed").check();
    await open("second.pdf");
    await page.locator("#confirmed").check();
    await page.locator("#publish-batch").click();
    await page.waitForFunction(() => document.querySelector("#queue-summary").textContent.includes("已保存 2"));
    assert.equal(await page.locator(".queue-row a").count(), 2);
    assert.match(await page.locator("#queue-summary").textContent(), /失败 1/);

    await page.locator("#source-kind").selectOption("wechat");
    await page.locator("#article-url").fill("https://mp.weixin.qq.com/s/first\nhttps://example.com/invalid\nhttps://mp.weixin.qq.com/s/second\nhttps://mp.weixin.qq.com/s/first");
    await page.locator("#convert").click();
    await page.waitForFunction(() => document.querySelector("#batch-message").textContent.includes("example.com/invalid"));
    await page.waitForFunction(() => document.querySelector("#queue-summary").textContent.includes("待确认 2"));
    assert.equal(await page.locator(".queue-row").count(), 2);
    const rows = await page.locator(".queue-row").evaluateAll(nodes => nodes.map(node => node.dataset.jobId));
    for (const id of rows) {
      await page.locator(`.queue-row[data-job-id="${id}"]`).getByRole("button", {name: "打开", exact: true}).click();
      await page.locator(".article-preview").waitFor();
      await page.locator("#confirmed").check();
    }
    assert.match(await page.locator("#publish-batch").textContent(), /（2）/);
    // Opening an older task can change the target without an input event.
    const oldPath = `**/api/jobs/${oldPdfId}`;
    await page.route(oldPath, async route => {
      const response = await route.fetch(); const data = await response.json();
      data.target = {host: "test.feishu.cn", node_token: "other-parent", title: "另一个父页面"};
      await route.fulfill({json: data});
    });
    await page.locator("#history").selectOption(oldPdfId);
    await page.waitForFunction(() => document.querySelector("#target").value.endsWith("/other-parent"));
    await page.locator(`.queue-row[data-job-id="${rows[1]}"]`).getByRole("button", {name: "打开", exact: true}).click();
    await page.locator(".article-preview").waitFor();
    await page.waitForFunction(() => !document.querySelector("#confirmed").disabled);
    assert.equal(await page.locator("#confirmed").isChecked(), false);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.unroute(oldPath);
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    for (const id of rows) {
      await page.locator(`.queue-row[data-job-id="${id}"]`).getByRole("button", {name: "打开", exact: true}).click();
      await page.waitForFunction(() => !document.querySelector("#confirmed").disabled);
      await page.locator("#confirmed").check();
    }
    await page.locator(".code-editor textarea").fill("const corrected = 1;\n  // preserve indent");
    assert.match(await page.locator("#publish-batch").textContent(), /（1）/);
    await page.locator(".code-save").click();
    await page.locator(".code-save").waitFor({state: "hidden"});
    await page.locator("#confirmed").check();
    // Changing a common target invalidates both prior confirmations.
    await page.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    assert.match(await page.locator("#publish-batch").textContent(), /（0）/);
    for (const id of rows) {
      await page.locator(`.queue-row[data-job-id="${id}"]`).getByRole("button", {name: "打开", exact: true}).click();
      await page.locator(".article-preview").waitFor();
      await page.locator("#confirmed").check();
    }
    await page.waitForFunction(() => [...document.querySelectorAll(".article-preview img")].every(img => img.complete && img.naturalWidth > 0));
    await fs.mkdir("output/playwright", {recursive: true});
    await page.screenshot({path: "output/playwright/batch-preview.png", fullPage: true});
    await page.locator("#publish-batch").click();
    await page.waitForFunction(() => document.querySelector("#queue-summary").textContent.includes("已保存 2"));
    assert.equal(await page.locator(".queue-row a").count(), 2);
    const articleBatch = await page.locator("#batch-filter").inputValue();
    await page.reload();
    await page.locator(`#batch-filter option[value="${articleBatch}"]`).waitFor({state: "attached"});
    await page.locator("#batch-filter").selectOption(articleBatch);
    await page.locator("#history").selectOption(rows[0]);
    await page.locator(".article-preview").waitFor();
    assert.equal(await page.locator("#publish-batch").isDisabled(), true);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: "output/playwright/batch-mobile.png", fullPage: true});
    await page.locator("#article-url").fill(Array.from({length: 21}, (_, i) => `https://mp.weixin.qq.com/s/limit-${i}`).join("\n"));
    await page.locator("#convert").click();
    assert.match(await page.locator("#error").textContent(), /1–20/);
    assert.equal(await page.locator(".queue-row").count(), 2);
    assert.deepEqual(errors, []);
    console.log("PASS: multiple PDF/URL inputs; isolated failures; exact URL dedupe; saved batch history; per-item review/title; confirmation invalidation; batch publish and result links; reload/mobile.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
