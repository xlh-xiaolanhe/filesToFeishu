/* Run against python -m tests.browser_server; all Feishu calls are simulated. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");

(async () => {
  const live = process.env.RUN_LIVE_TESTS === "1";
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(live ? "http://127.0.0.1:8765" : "http://127.0.0.1:8766");
    await page.locator("#target").waitFor();
    await page.waitForFunction(() => document.getElementById("target").value.includes("/wiki/"));
    const targetHost = new URL(await page.locator("#target").inputValue()).hostname;
    await page.locator("#resolve").click();
    await page.waitForFunction(() => document.getElementById("target-name").textContent || !document.getElementById("error").hidden);
    assert.equal(await page.locator("#error").isVisible(), false, await page.locator("#error").textContent());
    await page.locator("#file").setInputFiles(path.resolve("output/samples/representative.pdf"));
    await page.locator("#convert").click();
    await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("待确认"), null, { timeout: 120000 });
    assert.equal(await page.locator(".comparison").count(), 2);
    assert.match(await page.locator(".converted").allTextContents().then(x => x.join("")), /This paragraph must remain editable/);
    assert.match(await page.locator("#notices").textContent(), /第 2 页.*表格/s);
    assert.equal(await page.locator(".converted ol").count(), 1);
    assert.equal(await page.locator(".converted ol li").count(), 2);
    assert.equal(await page.locator("#publish").isDisabled(), true);
    if (live) await page.locator("#title").fill("MVP 验收 · 原创中英文样本 · 2026-09-17");
    await page.locator("#resolve").click();
    await page.waitForFunction(() => document.getElementById("target-name").textContent.includes("子页面"));
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll(".comparison").length === 2);
    await fs.mkdir("output/playwright", { recursive: true });
    await page.screenshot({ path: `output/playwright/${live ? "live-" : ""}preview.png`, fullPage: true });
    if (live) await page.locator("#title").fill("MVP 验收 · 原创中英文样本 · 2026-09-17");
    await page.locator("#confirmed").check();
    await page.locator("#publish").click();
    await page.waitForFunction(() => !document.getElementById("result").hidden || !document.getElementById("error").hidden, null, { timeout: 180000 });
    assert.equal(await page.locator("#error").isVisible(), false, await page.locator("#error").textContent());
    const result = await page.locator("#result a").getAttribute("href");
    assert.equal(new URL(result).hostname, targetHost);
    if (!live) assert.equal(result, "https://test.feishu.cn/wiki/child");
    await page.reload();
    await page.waitForFunction(() => !document.getElementById("result").hidden);
    await page.screenshot({ path: `output/playwright/${live ? "success-live" : "success-simulated"}.png`, fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: "output/playwright/mobile.png", fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
    console.log(`PASS: real PDF upload, preview, target, confirmation, ${live ? "LIVE" : "simulated"} publish, refresh, mobile. ${result}`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
