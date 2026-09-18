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
    const primary = page.locator(".code-preview .code-editor textarea");
    assert.equal(await primary.count(), 1);
    assert.equal(await primary.inputValue(), "const n = 1\n  n.toUperCase()");
    assert.equal(await primary.isEditable(), true);
    assert.equal(await page.locator(".code-preview > pre").count(), 0);
    assert.equal(await page.getByText("编辑代码或语言", { exact: true }).count(), 0);
    assert.equal(await page.getByText("校对识别结果", { exact: true }).count(), 0);
    assert.equal(await page.locator(".code-preview details").count(), 1);
    assert.equal(await page.locator(".code-reference").getAttribute("open"), null);
    assert.equal(await page.locator(".code-reference img").isVisible(), false);
    await page.locator(".code-reference summary").click();
    assert.equal(await page.locator(".code-reference img").isVisible(), true);
    await page.locator(".code-reference summary").click();
    assert.equal(await page.locator(".code-reference img").isVisible(), false);
    await page.locator("#confirmed").check();
    assert.equal(await page.locator("#publish").isDisabled(), true);
    const code = "const text = '<script>alert(1)</script>'\n  text.toUperCase()";
    await primary.fill("if (ready) {\n");
    await primary.press("Tab");
    await primary.pressSequentially("run()");
    assert.equal(await primary.inputValue(), "if (ready) {\n  run()");
    await primary.fill(code);
    await page.locator(".convert-code").click();
    await page.locator("figure .code-editor textarea").fill("let draft: number = 7");
    await page.locator(".code-preview .code-save").click();
    await page.locator(".code-preview .code-save").waitFor({state: "hidden"});
    assert.equal(await primary.inputValue(), code);
    assert.equal(await page.locator("figure .code-editor textarea").inputValue(), "let draft: number = 7");
    await page.locator("#confirmed").check();
    assert.equal(await page.locator("#publish").isDisabled(), true);
    await page.locator("figure .code-cancel").click();
    assert.equal(await page.locator("figure .code-editor").count(), 0);
    assert.equal(await page.locator(".code-preview script").count(), 0);
    await page.reload();
    await primary.waitFor();
    assert.equal(await primary.inputValue(), code);
    assert.equal(await page.locator(".code-reference").getAttribute("open"), null);
    await primary.fill("discard this draft");
    await page.locator(".code-preview .code-cancel").click();
    assert.equal(await primary.inputValue(), code);
    await page.locator(".convert-code").click();
    const manual = page.locator("figure .code-editor");
    await manual.locator("textarea").fill("let count: number = 1");
    await manual.locator("select").selectOption("typescript");
    await manual.locator(".code-save").click();
    await page.waitForFunction(() => document.querySelectorAll(".code-preview").length === 2);
    assert.deepEqual(await page.locator(".code-preview").evaluateAll(nodes => nodes.map(node => ({editors: node.querySelectorAll("textarea").length, readOnlyCopies: node.querySelectorAll("pre").length, references: node.querySelectorAll("details:not([open])").length}))), [
      {editors: 1, readOnlyCopies: 0, references: 1},
      {editors: 1, readOnlyCopies: 0, references: 1},
    ]);
    assert.equal(await page.locator(".code-preview select").nth(1).inputValue(), "typescript");
    const currentJob = await page.locator("#history").inputValue();
    const persisted = await (await page.request.get(`http://127.0.0.1:8766/api/jobs/${currentJob}`)).json();
    assert.equal(persisted.preview.elements[0].text, code);
    assert.equal(persisted.preview.elements[1].text, "let count: number = 1");
    assert.equal(persisted.preview.elements[1].language, "typescript");
    await fs.mkdir("output/playwright", { recursive: true });
    await page.screenshot({ path: "output/playwright/code-preview.png", fullPage: true });
    await page.locator("#confirmed").check();
    await page.locator("#publish").click();
    await page.locator("#result a").waitFor();
    assert.equal(await page.locator("#error").isVisible(), false);
    assert.equal(await page.locator(".code-editor textarea").first().isEditable(), false);
    assert.equal(await page.locator(".code-editor textarea").first().isDisabled(), false);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: "output/playwright/code-mobile.png", fullPage: true });
    assert.deepEqual(errors, []);
    console.log("PASS: single inline code editor, collapsed reference, indentation, OCR review gate, draft persistence/cancel, language, simulated native-code publish, read-only published code, mobile");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
