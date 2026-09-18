/* Run with TEST_CODE_PREVIEW=1 python -m tests.browser_server; never writes to Feishu. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:8766");
    await page.locator("#file").setInputFiles("output/samples/representative.pdf");
    await page.locator("#convert").click();
    await page.locator(".code-editor textarea").waitFor();
    assert.equal(await page.locator(".code-preview pre").textContent(), "const n = 1\n  n.toUperCase()");
    await page.locator("#confirmed").check();
    assert.equal(await page.locator("#publish").isDisabled(), true);
    const code = "const text = '<script>alert(1)</script>'\n  text.toUperCase()";
    await page.locator(".code-editor textarea").fill(code);
    await page.locator(".convert-code").click();
    await page.locator("figure .code-editor textarea").fill("let draft: number = 7");
    await page.locator(".code-preview").getByRole("button", { name: "保存代码校对", exact: true }).click();
    await page.waitForFunction(expected => document.querySelector(".code-preview pre").textContent === expected, code);
    assert.equal(await page.locator("figure .code-editor textarea").inputValue(), "let draft: number = 7");
    await page.locator("#confirmed").check();
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.locator("figure").getByRole("button", { name: "取消本段修改", exact: true }).click();
    assert.equal(await page.locator("figure .code-editor").count(), 0);
    assert.equal(await page.locator(".code-preview script").count(), 0);
    await page.reload();
    await page.locator(".code-preview pre").waitFor();
    assert.equal(await page.locator(".code-editor textarea").inputValue(), code);
    await page.locator(".convert-code").click();
    const manual = page.locator("figure .code-editor");
    await manual.locator("textarea").fill("let count: number = 1");
    await manual.locator("select").selectOption("typescript");
    await manual.getByRole("button", { name: "保存代码校对", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".code-preview").length === 2);
    await fs.mkdir("output/playwright", { recursive: true });
    await page.screenshot({ path: "output/playwright/code-preview.png", fullPage: true });
    await page.locator("#confirmed").check();
    await page.locator("#publish").click();
    await page.locator("#result a").waitFor();
    assert.equal(await page.locator("#error").isVisible(), false);
    assert.equal(await page.locator(".code-editor textarea").first().isDisabled(), true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: "output/playwright/code-mobile.png", fullPage: true });
    assert.deepEqual(errors, []);
    console.log("PASS: code preview, OCR review gate, literal HTML, edit persistence, image conversion, simulated publish, frozen edits, mobile");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
