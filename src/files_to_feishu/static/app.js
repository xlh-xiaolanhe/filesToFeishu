"use strict";
const $ = (id) => document.getElementById(id);
let job = null, health = null, timer = null, painted = "";
let savingCode = false;
const codeDrafts = new Map();
const active = new Set(["queued", "parsing", "publishing", "verifying", "archiving"]);
const labels = {queued:"排队",parsing:"解析中",ready:"待确认",publishing:"发布中",verifying:"核验中",archiving:"归档中",succeeded:"已保存",failed:"失败",needs_review:"待核对"};
function message(id, text) { $(id).textContent = text; $(id).hidden = !text; }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求无效，请检查输入。");
  return data;
}
function post(path, data) { return api(path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); }
function enable() {
  const unreviewed = job?.preview?.elements.some(e=>e.kind==="code" && !e.code_reviewed);
  $("publish").disabled = !job?.parsed || active.has(job?.status) || !health?.feishu_configured || !$("confirmed").checked || unreviewed || !!document.querySelector('.code-editor[data-dirty="true"]');
  $("publish").disabled ||= savingCode;
  $("history").disabled = savingCode;
  const codeLocked = savingCode || job?.status !== "ready" || job?.content_locked;
  document.querySelectorAll(".code-editor button, .code-editor select, .convert-code").forEach(node => node.disabled = codeLocked);
  document.querySelectorAll(".code-editor textarea").forEach(node => node.readOnly = codeLocked);
  $("publish").textContent = job?.status === "succeeded" ? "核对并打开已有文档" : (["failed","needs_review"].includes(job?.status) ? "核对状态并重试" : "保存到飞书");
  $("convert").disabled = savingCode || active.has(job?.status);
  $("target").disabled = !!job?.document_id || active.has(job?.status);
  $("title").disabled = !!job?.document_id || active.has(job?.status);
}
function element(tag, text, className) { const node=document.createElement(tag); if(text !== undefined) node.textContent=text; if(className)node.className=className; return node; }
function asset(name) { return `/api/jobs/${job.id}/assets/${encodeURIComponent(name)}`; }
function codeEditor(item, index) {
  const draftKey = `${job.id}:${index}`, draft = codeDrafts.get(draftKey);
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
    codeDrafts.set(draftKey, {text: input.value, language: language.value});
    editor.dataset.dirty = "true";
    $("confirmed").checked = false;
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
      await post(`/api/jobs/${job.id}/code/${index}`, {text: input.value, language: language.value});
      codeDrafts.delete(draftKey);
      editor.dataset.dirty = "false";
      painted = "";
      $("confirmed").checked = false;
      message("error", "");
      await refresh();
    } catch (error) {
      message("error", error.message);
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
function preview() {
  if (!job.preview || painted === job.id) return;
  painted=job.id; $("empty").hidden=true; $("preview").replaceChildren();
  message("notices", job.preview.notices.map(n=>`第 ${n.page} 页：${n.reason}`).join("\n"));
  for(let page=1;page<=job.preview.pages;page++) {
    $("preview").append(element("div",`第 ${page} / ${job.preview.pages} 页`,"page-label"));
    const pair=element("div",undefined,"comparison"), left=element("div"), right=element("div");
    left.append(element("div","原 PDF","column-label")); right.append(element("div","转换预览","column-label"));
    const image=element("img",undefined,"source-image");image.src=asset(job.preview.page_images[page-1]);image.alt=`原文第 ${page} 页`;image.loading="lazy";left.append(image);
    const content=element("div",undefined,"converted");
    for(const [index,item] of job.preview.elements.entries()) {
      if (item.kind === "code" && item.page !== page && item.code_sources?.some(source => source.page === page)) {
        const link = element("a", `查看合并后的代码（始于第 ${item.page} 页）`, "code-continuation");
        link.href = `#code-${index}`;
        content.append(link);
      }
      if(item.page!==page)continue;
      if(item.kind==="image") {const figure=element("figure"), img=element("img");img.src=asset(item.asset);img.alt=item.text;img.loading="lazy";figure.append(img,element("figcaption",item.text));const convert=element("button","这是代码图片：改为代码片段","secondary convert-code");convert.type="button";convert.addEventListener("click",()=>{convert.hidden=true;codeDrafts.set(`${job.id}:${index}`,{text:"",language:"plaintext"});const editor=codeEditor(item,index);figure.append(editor);$("confirmed").checked=false;enable();});figure.append(convert);if(codeDrafts.has(`${job.id}:${index}`)){convert.hidden=true;figure.append(codeEditor(item,index));}content.append(figure);}
      else if (item.kind === "code") {
        const section = element("section", undefined, "code-preview");
        section.id = `code-${index}`;
        section.append(codeEditor(item, index));
        const sources = item.code_sources?.length ? item.code_sources : [{page: item.page, asset: item.asset}];
        if (item.asset || item.code_sources?.length) {
          const reference = element("details", undefined, "code-reference");
          const pages = sources.length > 1 ? ` · 第 ${sources.map(source => source.page).join("、")} 页` : "";
          reference.append(element("summary", `查看原代码区域（仅用于校对）${pages}`));
          for (const source of sources) {
            const img = element("img");
            if (sources.length > 1) reference.append(element("div", `原 PDF 第 ${source.page} 页`, "muted"));
            img.src = asset(source.asset || `page-${source.page}.png`);
            img.alt = sources.length > 1 ? `原 PDF 第 ${source.page} 页代码区域` : "原 PDF 代码区域";
            img.loading = "lazy";
            reference.append(img);
          }
          section.append(reference);
        }
        content.append(section);
      }
      else if(item.kind==="table") {const table=element("table"); for(const row of item.rows){const tr=element("tr");for(const cell of row)tr.append(element("td",cell));table.append(tr);}content.append(table);}
      else if(["bullet","ordered"].includes(item.kind)){const tag=item.kind==="ordered"?"OL":"UL";let list=content.lastElementChild;if(list?.tagName!==tag){list=element(tag.toLowerCase());content.append(list);}list.append(element("li",item.text));}
      else content.append(element(item.kind==="heading"?"h3":"p",item.text));
    }
    right.append(content);pair.append(left,right);$("preview").append(pair);
  }
}
async function refresh() {
  clearTimeout(timer);
  if(!job)return;
  try {
    job=await api(`/api/jobs/${job.id}`);
    $("status").textContent=`${labels[job.status]} · ${job.progress}`;
    message("error",job.error);message("result","");
    if(job.status==="succeeded") {message("result","保存成功，正文、附件与目标位置已核对。 ");const link=element("a","打开飞书文档 ↗");link.href=job.url;link.target="_blank";link.rel="noopener noreferrer";$("result").append(link);}
    else if(job.document_id) {$("status").textContent+=` · 远端文档 ID：${job.document_id}`;}
    $("original").href=`/api/jobs/${job.id}/original`;$("original").hidden=false;
    preview();enable();
    if(active.has(job.status))timer=setTimeout(refresh,1200);else await history();
  }catch(error){message("error",error.message);timer=setTimeout(refresh,4000);}
}
async function select(id) {
  clearTimeout(timer);painted="";$("confirmed").checked=false;$("preview").replaceChildren();$("empty").hidden=false;message("notices","");
  job=await api(`/api/jobs/${id}`);localStorage.setItem("pdf-job",id);
  $("title").value=job.requested_title || job.filename.replace(/\.pdf$/i,"");
  if(job.target){$("target").value=`https://${job.target.host}/wiki/${job.target.node_token}`;message("target-name",`保存为「${job.target.title}」的子页面`);}
  await refresh();
}
async function history() {
  const items=await api("/api/jobs");$("history").replaceChildren(element("option","选择任务查看进度"));$("history").firstChild.value="";
  for(const item of items){const option=element("option",`${labels[item.status]} · ${item.filename}`);option.value=item.id;$("history").append(option);}
  if(job)$("history").value=job.id;
}
$("upload-form").addEventListener("submit",async event=>{
  event.preventDefault();message("error","");const file=$("file").files[0];if(!file)return;
  if(file.size>health.max_bytes){message("error","文件超过 20 MB 限制。");return;}
  $("convert").disabled=true;
  try{const data=new FormData();data.append("file",file);const created=await api("/api/jobs",{method:"POST",body:data});await select(created.id);}catch(error){message("error",error.message);}finally{enable();}
});
$("resolve").addEventListener("click",async()=>{ $("resolve").disabled=true;try{const target=await post("/api/targets/resolve",{url:$("target").value});localStorage.setItem("pdf-target",$("target").value);message("target-name",`保存为「${target.title}」的子页面`);message("error","");}catch(error){message("error",error.message);message("target-name","");}finally{$("resolve").disabled=false;}});
$("target").addEventListener("input",()=>message("target-name",""));
$("confirmed").addEventListener("change",enable);
$("history").addEventListener("change",()=>{if($("history").value)select($("history").value).catch(e=>message("error",e.message));});
$("publish").addEventListener("click",async()=>{ $("publish").disabled=true;try{await post(`/api/jobs/${job.id}/publish`,{url:$("target").value,title:$("title").value});localStorage.setItem("pdf-target",$("target").value);await refresh();}catch(error){message("error",error.message);enable();}});
(async()=>{try{health=await api("/api/health");$("target").value=localStorage.getItem("pdf-target") || health.default_target;const notes=[];if(!health.models_ready)notes.push("本地模型尚未准备，请按 README 下载模型后再转换。");if(!health.feishu_configured)notes.push("飞书凭证未配置：在本地 .env 填写 App ID 与 App Secret 后重启，即可检查目标与发布。");message("setup",notes.join("\n"));await history();const last=localStorage.getItem("pdf-job");if(last)await select(last);}catch(error){message("error",error.message);}enable();})();
