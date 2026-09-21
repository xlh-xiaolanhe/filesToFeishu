/* TEST_CODE_PREVIEW=1 python -m tests.browser_server; all content original, Feishu mocked. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const base = "http://127.0.0.1:8766";

(async () => {
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const first = await context.newPage(), second = await context.newPage();
    const errors = [];
    for (const page of [first, second]) page.on("pageerror", error => errors.push(error.message));
    await first.goto(base);
    await first.locator("#file").setInputFiles("output/samples/representative.pdf");
    await first.locator("#convert").click();
    await first.locator(".code-preview textarea").waitFor();
    const id = await first.locator("#history").inputValue();
    await second.goto(base);
    await second.locator(`#history option[value="${id}"]`).waitFor({state: "attached"});
    await second.locator("#history").selectOption(id);
    await second.locator(".code-preview textarea").waitFor();
    const original = await (await context.request.get(`${base}/api/jobs/${id}`)).json();

    const firstCode = "const firstWindow = 1;\n  // first saved revision";
    const draftCode = "const secondWindow = 2;\n  // keep my draft after a conflict";
    await first.locator(".code-preview textarea").fill(firstCode);
    const firstSave = first.waitForResponse(response => response.url().endsWith(`/code/0`));
    await first.locator(".code-save").click();
    assert.equal((await firstSave).status(), 200);
    await first.locator(".code-save").waitFor({state: "hidden"});

    await second.locator(".code-preview textarea").fill(draftCode);
    const staleSave = second.waitForResponse(response => response.url().endsWith(`/code/0`));
    await second.locator(".code-save").click();
    const conflict = await staleSave;
    assert.equal(conflict.status(), 409);
    assert.equal(conflict.request().postDataJSON().expected_revision, original.content_revision);
    await second.getByRole("button", {name: "加载最新版本对比（保留草稿）", exact: true}).waitFor();
    assert.equal(await second.locator(".code-preview textarea").inputValue(), draftCode);
    assert.equal(await second.locator("#publish").isDisabled(), true);
    let persisted = await (await context.request.get(`${base}/api/jobs/${id}`)).json();
    assert.equal(persisted.preview.elements[0].text, firstCode);
    assert.equal(persisted.content_revision, original.content_revision + 1);

    await second.getByRole("button", {name: "加载最新版本对比（保留草稿）", exact: true}).click();
    await second.locator(".code-conflict").waitFor();
    assert.equal(await second.locator(".code-conflict").textContent(), firstCode);
    assert.equal(await second.locator(".code-preview textarea").inputValue(), draftCode);
    await fs.mkdir("output/playwright", {recursive: true});
    await second.screenshot({path: "output/playwright/review-conflict.png", fullPage: true});
    const resolvedSave = second.waitForResponse(response => response.url().endsWith(`/code/0`));
    await second.locator(".code-save").click();
    const accepted = await resolvedSave;
    assert.equal(accepted.status(), 200);
    assert.equal(accepted.request().postDataJSON().expected_revision, persisted.content_revision);
    await second.locator(".code-save").waitFor({state: "hidden"});
    persisted = await (await context.request.get(`${base}/api/jobs/${id}`)).json();
    assert.equal(persisted.preview.elements[0].text, draftCode);
    assert.equal(persisted.content_revision, original.content_revision + 2);

    await second.locator("#title").fill("Reviewed title");
    await second.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    const confirm = async () => {
      const response = second.waitForResponse(value => value.url().endsWith(`/${id}/review`));
      await second.locator("#confirmed").check();
      const reviewed = await response;
      assert.equal(reviewed.status(), 200);
      assert.equal(reviewed.request().postDataJSON().expected_revision, persisted.content_revision);
      await second.waitForFunction(() => !document.querySelector("#publish").disabled);
      return (await reviewed.json()).confirmation_token;
    };
    const firstConfirmation = await confirm();
    await second.locator("#title").fill("Changed title needs confirmation");
    assert.equal(await second.locator("#confirmed").isChecked(), false);
    assert.equal(await second.locator("#publish").isDisabled(), true);
    const secondConfirmation = await confirm();
    assert.notEqual(secondConfirmation, firstConfirmation);
    await second.locator("#target").fill("https://test.feishu.cn/wiki/other-parent");
    assert.equal(await second.locator("#confirmed").isChecked(), false);
    assert.equal(await second.locator("#publish").isDisabled(), true);
    await second.locator("#target").fill("https://test.feishu.cn/wiki/parent");
    const finalConfirmation = await confirm();
    const publication = second.waitForResponse(value => value.url().endsWith(`/${id}/publish`));
    await second.locator("#publish").click();
    const published = await publication;
    assert.equal(published.status(), 202);
    assert.equal(published.request().postDataJSON().confirmation_token, finalConfirmation);
    await second.locator("#result a").waitFor();
    assert.equal(await second.locator(".code-preview textarea").inputValue(), draftCode);
    assert.equal(await second.locator(".code-preview textarea").isEditable(), false);
    assert.deepEqual(errors, []);
    console.log("PASS: two-tab edit CAS; 409 keeps draft; explicit comparison then save; title/target confirmation invalidation; frozen confirmation on publish.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
