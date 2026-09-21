"use strict";
const $ = (id) => document.getElementById(id);
let job = null, health = null, timer = null, painted = "";
let savingCode = false, pendingAction = false, titleEdited = false, selecting = false, selectionVersion = 0;
const codeDrafts = new Map(), reviews = new Map();
let allJobs = [], batchFilter = "", queueTimer = null, historyVersion = 0, historyOffset = 0;
// Remove only this app's old remembered fields, including data from earlier versions.
try { for (const key of ["pdf-target", "pdf-job", "content-batch"]) localStorage.removeItem(key); }
catch { /* Storage may be unavailable; form initialization still works. */ }
const MAX_BATCH = 20;
function invalidateReview() {
  if (job) reviews.delete(job.id);
  $("confirmed").checked = false;
}
function batchItems() { return allJobs.filter(item => batchFilter && item.batch_id === batchFilter); }
function publishable(item) { return item?.parsed && ["ready", "failed", "needs_review", "succeeded"].includes(item.status); }
function reviewedBatch() { return batchItems().filter(item => publishable(item) && reviews.has(item.id)); }
async function rememberReview() {
  if (!job) return;
  const dirty = [...codeDrafts.keys()].some(key => key.startsWith(`${job.id}:`));
  if ($("confirmed").checked && publishable(job) && !dirty && !job.preview.elements.some(e => e.kind === "code" && !e.code_reviewed)) {
    const id = job.id;
    const review = {title: $("title").value, review_token: job.review_token || "", url: $("target").value};
    pendingAction = true; enable();
    try {
      const result = await post(`/api/jobs/${id}/review`, {...review, expected_revision: job.content_revision});
      if (job?.id === id && $("confirmed").checked && review.title === $("title").value && review.url === $("target").value) {
        reviews.set(id, {...review, confirmation_token: result.confirmation_token});
        message("error", "");
      }
    } catch (error) { invalidateReview(); message("error", error.message); }
    finally { pendingAction = false; enable(); }
  } else reviews.delete(job.id);
}
function renderQueue() {
  $("queue-panel").hidden = !allJobs.length && historyOffset === 0;
  const items = batchFilter ? batchItems() : allJobs;
  const counts = {};
  for (const item of items) counts[labels[item.status] || item.status] = (counts[labels[item.status] || item.status] || 0) + 1;
  $("queue-summary").textContent = `${items.length} 个任务 · ` + Object.entries(counts).map(([name, count]) => `${name} ${count}`).join(" / ");
  if (allJobs.some(item => item.status === "waiting_verification")) $("queue-summary").textContent += " · 队列等待验证，请打开等待中的任务继续或取消";
  $("queue-list").replaceChildren();
  for (const item of items) {
    const row = element("div", undefined, "queue-row"); row.dataset.jobId = item.id;
    row.setAttribute("aria-current", String(job?.id === item.id));
    const info = element("div", undefined, "queue-info");
    info.append(element("p", item.requested_title || (item.filename === "公众号文章.zip" ? item.source_url : item.filename) || "未命名任务"));
    info.append(element("p", `${labels[item.status] || item.status}${reviews.has(item.id) ? " · 已核对" : ""}${item.error ? " · " + item.error : ""}`, "muted"));
    for (const receipt of item.notifications || []) info.append(element("p", notificationLabel(receipt), "muted small"));
    const view = element("button", "打开", "secondary"); view.type = "button";
    view.disabled = pendingAction || savingCode || selecting;
    view.addEventListener("click", () => select(item.id).catch(error => message("error", error.message)));
    row.append(info, view);
    const remove = element("button", "删除", "secondary delete-job"); remove.type = "button";
    const busy = active.has(item.status) || (item.notifications || []).some(n => ["pending", "sending"].includes(n.status));
    const unresolved = item.status !== "succeeded" && (item.document_id || item.content_locked);
    remove.disabled = (item.allowed_actions ? !item.allowed_actions.includes("delete") : busy || unresolved) || pendingAction || savingCode || selecting;
    remove.title = busy ? "任务处理或通知发送完成后可删除" : (unresolved ? "已有未完成核验的飞书写入，请先核对原任务" : "从本地历史列表删除");
    remove.addEventListener("click", () => deleteJob(item));
    row.append(remove);
    if (item.status === "succeeded" && externalUrl(item.url)) {
      const link = element("a", "飞书文档 ↗"); link.href = externalUrl(item.url); link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link);
    }
    $("queue-list").append(row);
  }
  const eligible = reviewedBatch();
  $("publish-batch").textContent = `保存本批已确认任务（${eligible.length}）`;
  $("publish-batch").disabled = !eligible.length || !health?.feishu_configured || !$("target").value.trim() || pendingAction || savingCode || selecting;
}
const active = new Set(["queued", "publish_queued", "fetching", "waiting_verification", "cancelling", "parsing", "publishing", "verifying", "archiving"]);
const labels = {queued:"排队",publish_queued:"等待发布",fetching:"获取文章中",waiting_verification:"等待验证",cancelling:"正在取消",parsing:"解析中",ready:"待确认",publishing:"发布中",verifying:"核验中",archiving:"归档中",succeeded:"已保存",failed:"失败",cancelled:"已取消",needs_review:"待核对"};
function message(id, text) { $(id).textContent = text; $(id).hidden = !text; }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "请求无效，请检查输入。");
    error.status = response.status; throw error;
  }
  return data;
}
function clearSelection() {
  ++selectionVersion; clearTimeout(timer);
  job = null; painted = ""; titleEdited = false; selecting = false;
  $("title").value = ""; $("target").value = ""; $("confirmed").checked = false;
  $("preview").replaceChildren(); $("empty").hidden = false;
  $("original").hidden = true; $("original").removeAttribute("href");
  $("history").value = ""; $("status").textContent = "等待选择文件或文章";
  for (const id of ["error", "notices", "result", "notifications", "target-name"]) message(id, "");
  enable();
}
async function deleteJob(item) {
  if (pendingAction || savingCode || selecting) return;
  if (!window.confirm(`从历史列表删除「${item.requested_title || item.filename || "此任务"}」？\n已生成的飞书文档不受影响。本地素材和发布核验记录仍保留，用于避免重复导入。`)) return;
  pendingAction = true; ++historyVersion; enable();
  try {
    await api(`/api/jobs/${item.id}`, {method: "DELETE"});
    ++historyVersion; reviews.delete(item.id);
    for (const key of codeDrafts.keys()) if (key.startsWith(`${item.id}:`)) codeDrafts.delete(key);
    allJobs = allJobs.filter(entry => entry.id !== item.id);
    if (job?.id === item.id) clearSelection();
    await history();
  } catch (error) { message("error", error.message); }
  finally { pendingAction = false; enable(); }
}
function post(path, data) { return api(path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); }
function notificationLabel(receipt) {
  const events = {succeeded:"保存结果",failed:"失败结果",needs_review:"待核对结果"};
  const states = {pending:"等待发送",sending:"发送中",sent:"已发送",failed:"发送失败",uncertain:"发送结果待核对"};
  return `${events[receipt.event] || "处理结果"}通知：${states[receipt.status] || receipt.status}`;
}
function renderNotifications(item) {
  const box = $("notifications"); box.replaceChildren();
  const receipts = item?.notifications || []; box.hidden = !receipts.length;
  for (const receipt of receipts) {
    box.append(element("p", notificationLabel(receipt) + (receipt.error ? ` · ${receipt.error}` : "")));
    if (receipt.status === "failed" && receipt.event === item.status && health?.notifications_enabled) {
      const retry = element("button", "仅重试通知", "secondary"); retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true; const id = item.id;
        try {
          await post(`/api/jobs/${id}/notifications/${receipt.event}/retry`, {});
          if (job?.id === id) await refresh();
        } catch (error) { if (job?.id === id) message("error", error.message); }
        finally { retry.disabled = false; }
      });
      box.append(retry);
    }
  }
}
function renderRecovery(item) {
  const box = $("recovery"); box.replaceChildren(); box.hidden = !item?.recovery_steps?.length;
  if (box.hidden) return;
  box.append(element("p", "部分写入结果不确定。请在飞书找到候选文档或素材令牌，系统核验匹配后才允许续接。无法取得可靠候选时保留待核对状态。"));
  for (const step of item.recovery_steps) {
    const label = element("label", step.kind === "document" ? "候选文档 ID" : `候选素材令牌（${step.key}）`);
    const input = element("input"); input.autocomplete = "off";
    const button = element("button", "核验并关联", "secondary"); button.type = "button";
    button.addEventListener("click", async () => {
      if (!input.value.trim() || pendingAction) return;
      pendingAction = true; enable();
      try {
        await post(`/api/jobs/${item.id}/recover`, {key: step.key, candidate: input.value.trim(), expected_revision: item.content_revision});
        invalidateReview(); await refresh();
      } catch (error) { message("error", error.message); }
      finally { pendingAction = false; enable(); }
    });
    label.append(input); box.append(label, button);
  }
}
function sourceKind() { return job?.source_kind || job?.preview?.source_kind || "pdf"; }
function setupNotes() {
  if (!health) return;
  const notes = [];
  if ($("source-kind").value === "pdf" && !health.models_ready) notes.push("PDF 本地模型尚未准备，请按 README 下载模型后再转换。公众号文章不需要 PDF 模型。");
  if (!health.feishu_configured) notes.push("飞书凭证未配置：在本地 .env 填写 App ID 与 App Secret 后重启，即可检查目标与发布。");
  message("setup", notes.join("\n"));
}
function sourceInputs() {
  const wechat = $("source-kind").value === "wechat";
  $("pdf-input").hidden = wechat;
  $("wechat-input").hidden = !wechat;
  $("file").required = !wechat;
  $("file").disabled = wechat;
  $("article-url").required = wechat;
  $("article-url").disabled = !wechat;
  $("convert").textContent = wechat ? "获取文章并预览" : "开始本地转换";
  setupNotes();
}
function enable() {
  const unreviewed = job?.preview?.elements.some(e => e.kind === "code" && !e.code_reviewed);
  const busy = savingCode || pendingAction || selecting;
  $("publish").disabled = !reviews.has(job?.id) || !job?.parsed || !["ready", "failed", "needs_review", "succeeded"].includes(job?.status) || !health?.feishu_configured || !$("target").value.trim() || !$("confirmed").checked || unreviewed || !!document.querySelector('.code-editor[data-dirty="true"]') || busy;
  $("history").disabled = busy || job?.status === "cancelling";
  $("confirmed").disabled = busy || !job?.parsed || active.has(job?.status) || job?.status === "cancelled";
  const codeLocked = busy || job?.status !== "ready" || job?.content_locked;
  document.querySelectorAll(".code-editor button, .code-editor select, .convert-code").forEach(node => node.disabled = codeLocked);
  document.querySelectorAll(".code-editor textarea").forEach(node => node.readOnly = codeLocked);
  $("publish").textContent = job?.status === "succeeded" ? "核对并打开已有文档" : (["failed", "needs_review"].includes(job?.status) ? "核对状态并重试" : "保存到飞书");
  $("convert").disabled = busy || job?.status === "cancelling" || !health;
  $("source-kind").disabled = busy || job?.status === "cancelling";
  $("file").disabled = busy || $("source-kind").value !== "pdf";
  $("article-url").disabled = busy || $("source-kind").value !== "wechat";
  $("target").disabled = !!job?.document_id || active.has(job?.status) || busy;
  $("title").disabled = !!job?.document_id || active.has(job?.status) || busy;
  $("verification").hidden = job?.status !== "waiting_verification";
  $("fetch-actions").hidden = job?.status !== "queued" && (sourceKind() !== "wechat" || !["fetching", "waiting_verification", "cancelling"].includes(job?.status));
  $("continue-fetch").disabled = busy || job?.status !== "waiting_verification";
  $("cancel-fetch").disabled = busy || job?.status === "cancelling";
  $("cancel-fetch").textContent = job?.status === "cancelling" ? "正在取消…" : (job?.status === "queued" ? "取消排队" : "取消获取");
  renderQueue();
}
function element(tag, text, className) { const node=document.createElement(tag); if(text !== undefined) node.textContent=text; if(className)node.className=className; return node; }
function asset(name) { return `/api/jobs/${job.id}/assets/${encodeURIComponent(name)}`; }
function codeEditor(item, index) {
  const draftKey = `${job.id}:${index}`, draft = codeDrafts.get(draftKey);
  let baseRevision = draft?.expected_revision ?? job.content_revision;
  const editor = element("div", undefined, "code-editor");
  const toolbar = element("div", undefined, "code-toolbar");
  const input = element("textarea"), language = element("select");
  const save = element("button", "保存", "code-action code-save");
  const cancel = element("button", "撤销", "code-action code-cancel");
  const body = element("div", undefined, "code-body");
  const gutter = element("div", undefined, "code-gutter"), numbers = element("div");
  gutter.setAttribute("aria-hidden", "true");
  gutter.append(numbers);
  input.value = draft?.text ?? (item.kind === "code" ? item.text : "");
  input.maxLength = 20000;
  input.spellcheck = false;
  input.wrap = "off";
  input.setAttribute("aria-label", `代码片段 ${index + 1}`);
  input.setAttribute("autocapitalize", "off");
  input.setAttribute("autocomplete", "off");
  for (const [value, label] of [["plaintext", "纯文本"], ["javascript", "JavaScript"], ["typescript", "TypeScript"]]) {
    const option = element("option", label);
    option.value = value;
    language.append(option);
  }
  language.value = draft?.language || item.language || "plaintext";
  language.setAttribute("aria-label", "代码语言");
  save.type = cancel.type = "button";
  save.title = "保存代码及校对结果";
  cancel.title = "撤销本段未保存的修改";
  if (draft) editor.dataset.dirty = "true";
  function updateEditor() {
    const lines = input.value.split("\n").length;
    input.rows = Math.min(28, Math.max(2, lines));
    numbers.textContent = Array.from({length: lines}, (_, i) => i + 1).join("\n");
    numbers.style.transform = `translateY(-${input.scrollTop}px)`;
    save.hidden = item.kind === "code" && item.code_reviewed && editor.dataset.dirty !== "true";
    cancel.hidden = editor.dataset.dirty !== "true";
  }
  function dirty() {
    codeDrafts.set(draftKey, {text: input.value, language: language.value, expected_revision: baseRevision});
    editor.dataset.dirty = "true";
    invalidateReview();
    updateEditor();
    enable();
  }
  input.addEventListener("input", dirty);
  language.addEventListener("change", dirty);
  input.addEventListener("scroll", () => { numbers.style.transform = `translateY(-${input.scrollTop}px)`; });
  input.addEventListener("keydown", event => {
    if (event.key === "Tab" && !event.shiftKey && !input.readOnly) {
      event.preventDefault();
      input.setRangeText("  ", input.selectionStart, input.selectionEnd, "end");
      dirty();
    }
  });
  save.addEventListener("click", async () => {
    savingCode = true;
    enable();
    try {
      const saved = await post(`/api/jobs/${job.id}/code/${index}`, {text: input.value, language: language.value, expected_revision: baseRevision});
      // Only our own successful edit can advance other drafts from the same baseline.
      for (const [key, value] of codeDrafts) if (key.startsWith(`${job.id}:`) && value.expected_revision === baseRevision) value.expected_revision = saved.content_revision;
      baseRevision = saved.content_revision;
      codeDrafts.delete(draftKey);
      editor.dataset.dirty = "false";
      painted = "";
      invalidateReview();
      message("error", "");
      await refresh();
    } catch (error) {
      message("error", error.message);
      if (error.status === 409) {
        const reload = element("button", "加载最新版本对比（保留草稿）", "secondary"); reload.type = "button";
        reload.addEventListener("click", async () => {
          const latest = await api(`/api/jobs/${job.id}`);
          const comparison = element("pre", latest.preview.elements[index]?.text || "该区域已不存在", "code-conflict");
          editor.append(comparison);
          reload.textContent = "已显示服务器版本；比较后再次保存将采用新版本号";
          reload.disabled = true; job = latest; baseRevision = latest.content_revision;
          dirty();
        });
        editor.append(reload);
      }
    } finally {
      savingCode = false;
      enable();
    }
  });
  cancel.addEventListener("click", () => {
    codeDrafts.delete(draftKey);
    painted = "";
    preview();
    enable();
  });
  toolbar.append(element("span", "代码块", "code-kind"), language, cancel, save);
  body.append(gutter, input);
  editor.append(toolbar, body);
  updateEditor();
  return editor;
}
function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}
function richText(container, text, runs = []) {
  if (!runs.length) { container.textContent = text || ""; return; }
  for (const run of runs) {
    let node = document.createTextNode(run.text || "");
    for (const [flag, tag] of [["inline_code", "code"], ["strike", "s"], ["italic", "em"], ["bold", "strong"]]) {
      if (run[flag]) { const wrapper = element(tag); wrapper.append(node); node = wrapper; }
    }
    const href = externalUrl(run.link);
    if (href) { const link = element("a"); link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.append(node); node = link; }
    container.append(node);
  }
}
function renderElements(content, indexedItems) {
  const lists = [];
  for (const [index, item] of indexedItems) {
    const depth = Math.min(item.list_depth || 0, lists.length);
    const destination = depth > 0 ? lists[depth - 1].lastItem : content;
    if (["bullet", "ordered"].includes(item.kind)) {
      lists.length = Math.min(lists.length, depth + 1);
      const tag = item.kind === "ordered" ? "OL" : "UL";
      let entry = lists[depth];
      if (!entry || entry.list.tagName !== tag || destination.lastElementChild !== entry.list) {
        const list = element(tag.toLowerCase());
        if (item.kind === "ordered" && item.list_start) list.start = item.list_start;
        destination.append(list);
        entry = {list, lastItem: null};
        lists[depth] = entry;
      }
      const li = element("li");
      if (item.kind === "ordered" && item.list_start) li.value = item.list_start;
      richText(li, item.text, item.runs);
      entry.list.append(li);
      entry.lastItem = li;
      continue;
    }
    lists.length = depth;
    if (item.kind === "image") {
      const figure = element("figure"), img = element("img");
      img.src = asset(item.asset); img.alt = item.text || "原文图片"; img.loading = "lazy";
      figure.append(img);
      if (item.text) figure.append(element("figcaption", item.text));
      const convert = element("button", "这是代码图片：改为代码片段", "secondary convert-code");
      convert.type = "button";
      convert.addEventListener("click", () => {
        convert.hidden = true;
        codeDrafts.set(`${job.id}:${index}`, {text: "", language: "plaintext", expected_revision: job.content_revision});
        figure.append(codeEditor(item, index)); invalidateReview(); enable();
      });
      figure.append(convert);
      if (codeDrafts.has(`${job.id}:${index}`)) { convert.hidden = true; figure.append(codeEditor(item, index)); }
      destination.append(figure);
    } else if (item.kind === "code") {
      const section = element("section", undefined, "code-preview");
      section.id = `code-${index}`; section.append(codeEditor(item, index));
      const sources = item.code_sources?.length ? item.code_sources : [{page: item.page, asset: item.asset}];
      if (item.asset || item.code_sources?.length) {
        const reference = element("details", undefined, "code-reference");
        const pages = sources.length > 1 ? ` · 第 ${sources.map(source => source.page).join("、")} 页` : "";
        reference.append(element("summary", `查看原代码区域（仅用于校对）${pages}`));
        for (const source of sources) {
          const img = element("img");
          if (sources.length > 1) reference.append(element("div", `原 PDF 第 ${source.page} 页`, "muted"));
          img.src = asset(source.asset || `page-${source.page}.png`);
          img.alt = sources.length > 1 ? `原 PDF 第 ${source.page} 页代码区域` : "原代码区域";
          img.loading = "lazy"; reference.append(img);
        }
        section.append(reference);
      }
      destination.append(section);
    } else if (item.kind === "table") {
      const table = element("table");
      for (const [rowIndex, row] of item.rows.entries()) {
        const tr = element("tr");
        for (const [cellIndex, cell] of row.entries()) {
          const td = element("td");
          richText(td, cell, item.table_runs?.[rowIndex]?.[cellIndex]); tr.append(td);
        }
        table.append(tr);
      }
      destination.append(table);
    } else {
      const tag = item.kind === "heading" ? `h${Math.min(6, Math.max(1, Number(item.level) || 1))}` : item.kind === "quote" ? "blockquote" : "p";
      const node = element(tag, undefined, item.kind === "heading" ? "document-heading" : undefined);
      richText(node, item.text, item.runs); destination.append(node);
    }
  }
}
function preview() {
  if (!job.preview || painted === job.id) return;
  painted = job.id; $("empty").hidden = true; $("preview").replaceChildren();
  const parsed = job.preview;
  message("notices", parsed.notices.map(n => n.page ? `第 ${n.page} 页：${n.reason}` : n.reason).join("\n"));
  if (sourceKind() === "wechat") {
    const metadata = parsed.metadata || {};
    const details = element("details", undefined, "article-metadata");
    details.append(element("summary", "来源信息"));
    for (const [name, value] of [["文章", metadata.title], ["公众号", metadata.account], ["作者", metadata.author], ["发布时间", metadata.published_at]]) {
      if (value) details.append(element("p", `${name}：${value}`));
    }
    const href = externalUrl(metadata.url);
    if (href) { const link = element("a", "查看公众号原文 ↗"); link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; details.append(link); }
    const article = element("article", undefined, "converted article-preview");
    renderElements(article, [...parsed.elements.entries()]);
    $("preview").append(details, article);
    return;
  }
  for (let page = 1; page <= parsed.pages; page++) {
    $("preview").append(element("div", `第 ${page} / ${parsed.pages} 页`, "page-label"));
    const pair = element("div", undefined, "comparison"), left = element("div"), right = element("div");
    left.append(element("div", "原 PDF", "column-label")); right.append(element("div", "转换预览", "column-label"));
    const image = element("img", undefined, "source-image"); image.src = asset(parsed.page_images[page - 1]); image.alt = `原文第 ${page} 页`; image.loading = "lazy"; left.append(image);
    const content = element("div", undefined, "converted");
    for (const [index, item] of parsed.elements.entries()) {
      if (item.kind === "code" && item.page !== page && item.code_sources?.some(source => source.page === page)) {
        const link = element("a", `查看合并后的代码（始于第 ${item.page} 页）`, "code-continuation"); link.href = `#code-${index}`; content.append(link);
      }
    }
    renderElements(content, [...parsed.elements.entries()].filter(([, item]) => item.page === page));
    right.append(content); pair.append(left, right); $("preview").append(pair);
  }
}
async function refresh() {
  clearTimeout(timer);
  if (!job) return;
  const selectedId = job.id;
  try {
    const hadPreview = !!job.parsed;
    const updated = await api(`/api/jobs/${selectedId}`);
    if (job?.id !== selectedId) return;
    job = updated;
    const review = reviews.get(job.id);
    if (review && review.review_token !== (job.review_token || "")) invalidateReview();
    allJobs = allJobs.map(item => item.id === job.id ? job : item);
    if (!hadPreview && job.parsed) invalidateReview();
    $("status").textContent = `${labels[job.status] || job.status} · ${job.progress}`;
    message("error", job.error); message("result", "");
    renderNotifications(job); renderRecovery(job);
    if (job.status === "succeeded") {
      message("result", "保存成功，正文、附件与目标位置已核对。 ");
      const link = element("a", "打开飞书文档 ↗"); const href = externalUrl(job.url);
      if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; $("result").append(link); }
    } else if (job.document_id) $("status").textContent += ` · 远端文档 ID：${job.document_id}`;
    $("original").href = `/api/jobs/${job.id}/original`;
    $("original").textContent = sourceKind() === "wechat" ? "下载文章归档 ZIP" : "下载原 PDF";
    $("original").hidden = sourceKind() === "wechat" && !job.parsed;
    if (!titleEdited && !reviews.has(job.id) && job.preview?.metadata?.title) $("title").value = job.requested_title || job.preview.metadata.title;
    preview(); enable();
    if (active.has(job.status) || (job.notifications || []).some(n => ["pending", "sending"].includes(n.status))) timer = setTimeout(refresh, job.status === "waiting_verification" ? 3000 : 1200);
    else await history();
  } catch (error) {
    if (job?.id !== selectedId) return;
    if (error.status === 404) { clearSelection(); await history(); return; }
    message("error", error.message); timer = setTimeout(refresh, 4000);
  }
}
async function select(id) {
  const version = ++selectionVersion;
  selecting = true; enable();
  clearTimeout(timer); painted = ""; titleEdited = false; $("confirmed").checked = false; $("preview").replaceChildren(); $("empty").hidden = false; message("notices", "");
  try {
    const selected = await api(`/api/jobs/${id}`);
    if (version !== selectionVersion) return;
    job = selected;
    $("source-kind").value = sourceKind(); sourceInputs();
    $("title").value = reviews.get(job.id)?.title || job.requested_title || job.preview?.metadata?.title || (sourceKind() === "pdf" ? (job.filename || "").replace(/\.pdf$/i, "") : "");
    if (job.target) { $("target").value = `https://${job.target.host}/wiki/${job.target.node_token}`; message("target-name", `保存为「${job.target.title}」的子页面`); }
    const review = reviews.get(job.id);
    $("confirmed").checked = !!review && review.review_token === (job.review_token || "") && review.url === $("target").value;
    if (review && !$("confirmed").checked) reviews.delete(job.id);
    await refresh();
  } finally { if (version === selectionVersion) { selecting = false; enable(); } }
}

async function history() {
  const version = ++historyVersion;
  const items = await api(historyOffset ? `/api/jobs?limit=50&offset=${historyOffset}` : "/api/jobs");
  if (version !== historyVersion) return;
  allJobs = items;
  const ids = new Set(items.map(item => item.id));
  if (job && !ids.has(job.id)) {
    try { await api(`/api/jobs/${job.id}`); }
    catch (error) { if (error.status === 404) clearSelection(); else throw error; }
  }
  $("history-newer").disabled = historyOffset === 0;
  $("history-older").disabled = items.length < 50;
  $("history-page").textContent = `第 ${Math.floor(historyOffset / 50) + 1} 页（每页最多 50 项）`;
  const batches = new Map();
  for (const item of items) if (item.batch_id && !batches.has(item.batch_id)) batches.set(item.batch_id, item.created_at);
  $("batch-filter").replaceChildren(element("option", "全部任务")); $("batch-filter").firstChild.value = "";
  for (const [id, date] of batches) { const option = element("option", `${date ? new Date(date).toLocaleString() : "本次导入"} · ${items.filter(item => item.batch_id === id).length} 项`); option.value = id; $("batch-filter").append(option); }
  if (batchFilter && !batches.has(batchFilter)) batchFilter = "";
  $("batch-filter").value = batchFilter;
  $("history").replaceChildren(element("option", "选择任务查看进度")); $("history").firstChild.value = "";
  for (const item of items) { const option = element("option", `${labels[item.status] || item.status} · ${item.source_kind === "wechat" ? "文章 · " : ""}${item.requested_title || item.filename || item.source_url || "未命名任务"}`); option.value = item.id; $("history").append(option); }
  if (job) $("history").value = job.id;
  renderQueue();
}
$("history-newer").addEventListener("click", async () => { historyOffset = Math.max(0, historyOffset - 50); await history(); });
$("history-older").addEventListener("click", async () => { historyOffset += 50; await history(); });
$("source-kind").addEventListener("change", () => { sourceInputs(); invalidateReview(); enable(); });
$("article-url").addEventListener("input", () => { invalidateReview(); enable(); });
$("file").addEventListener("change", () => { invalidateReview(); enable(); });
$("title").addEventListener("input", () => { titleEdited = true; invalidateReview(); enable(); });
$("upload-form").addEventListener("submit", async event => {
  event.preventDefault(); message("error", "");
  if (pendingAction) return;
  const wechat = $("source-kind").value === "wechat";
  const inputs = wechat ? [...new Set($("article-url").value.split(/\r?\n/).map(url => url.trim()).filter(Boolean))] : [...$("file").files];
  if (!inputs.length || inputs.length > MAX_BATCH) { message("error", "每批请选择 1–20 个 PDF，或填写 1–20 个文章链接（每行一个）。"); return; }
  pendingAction = true; enable();
  const batchId = crypto.randomUUID().replaceAll("-", ""), created = [], errors = [];
  try {
    for (const [index, input] of inputs.entries()) {
      message("batch-message", `正在提交 ${index + 1}/${inputs.length}…`);
      try {
        let entry;
        if (wechat) entry = await post("/api/jobs/wechat", {url: input, batch_id: batchId});
        else {
          if (input.size > health.max_bytes) throw new Error("文件超过 20 MB 限制。");
          const data = new FormData(); data.append("file", input); data.append("batch_id", batchId);
          entry = await api("/api/jobs", {method: "POST", body: data});
        }
        created.push(entry);
      } catch (error) { errors.push(`${wechat ? input : input.name}：${error.message}`); }
    }
    historyOffset = 0; batchFilter = batchId;
    await history();
    if (created.length) await select(created[0].id);
    const report = `已提交 ${created.length}/${inputs.length} 个任务。` + (errors.length ? "\n未成功提交，请处理后单独重试：\n" + errors.join("\n") : "可在队列中逐篇核对。");
    message("batch-message", report);
    if (errors.length) message("error", report);
  } catch (error) { message("error", error.message); }
  finally { pendingAction = false; enable(); }
});
async function fetchAction(action) {
  if (pendingAction || !job) return;
  pendingAction = true; enable();
  try { await post(`/api/jobs/${job.id}/${action}`, {}); await refresh(); }
  catch (error) { message("error", error.message); }
  finally { pendingAction = false; enable(); }
}
$("continue-fetch").addEventListener("click", () => fetchAction("continue"));
$("cancel-fetch").addEventListener("click", () => fetchAction("cancel"));
$("resolve").addEventListener("click", async () => {
  $("resolve").disabled = true;
  try { const target = await post("/api/targets/resolve", {url: $("target").value}); message("target-name", `保存为「${target.title}」的子页面`); message("error", ""); }
  catch (error) { message("error", error.message); message("target-name", ""); }
  finally { $("resolve").disabled = false; }
});
$("target").addEventListener("input", () => { message("target-name", ""); reviews.clear(); invalidateReview(); enable(); });
$("confirmed").addEventListener("change", () => { rememberReview().catch(e => message("error", e.message)); enable(); });
$("history").addEventListener("change", () => { if ($("history").value) select($("history").value).catch(e => message("error", e.message)); });
$("publish").addEventListener("click", async () => {
  if (!job || !$("confirmed").checked || pendingAction) return;
  pendingAction = true; enable();
  try { await post(`/api/jobs/${job.id}/publish`, {url: $("target").value, title: $("title").value, confirmed: $("confirmed").checked, review_token: job.review_token || "", confirmation_token: reviews.get(job.id)?.confirmation_token || ""}); await refresh(); }
  catch (error) { message("error", error.message); }
  finally { pendingAction = false; enable(); }
});
$("batch-filter").addEventListener("change", () => { batchFilter = $("batch-filter").value; renderQueue(); });
$("publish-batch").addEventListener("click", async () => {
  if (pendingAction || savingCode) return;
  const items = reviewedBatch(), target = $("target").value;
  if (!items.length) return;
  pendingAction = true; enable();
  const errors = []; let accepted = 0;
  try {
    for (const item of items) {
      const review = reviews.get(item.id);
      try {
        if (review.url !== target) throw new Error("保存位置已改变，请重新核对该任务");
        await post(`/api/jobs/${item.id}/publish`, {url: target, title: review.title, confirmed: true, review_token: review.review_token, confirmation_token: review.confirmation_token});
        accepted++; reviews.delete(item.id);
      } catch (error) { errors.push(`${review.title || item.filename}：${error.message}`); }
    }
    message("batch-message", `已将 ${accepted} 个任务加入发布队列，完成状态请查看上方列表。` + (errors.length ? "\n" + errors.join("\n") : ""));
    await history(); if (job) await refresh();
  } finally { pendingAction = false; enable(); }
});
async function pollQueue() {
  clearTimeout(queueTimer);
  try { if (!pendingAction && !savingCode) await history(); }
  catch { /* The current preview reports connection errors; keep polling the durable queue. */ }
  finally { queueTimer = setTimeout(pollQueue, 2000); }
}
(async () => {
  try {
    for (const id of ["title", "target", "article-url", "file"]) $(id).value = "";
    $("confirmed").checked = false; $("source-kind").value = "pdf";
    health = await api("/api/health");
    sourceInputs(); await history();
  } catch (error) { message("error", error.message); }
  enable(); pollQueue();
})();
window.addEventListener("pageshow", event => { if (event.persisted) window.location.reload(); });
