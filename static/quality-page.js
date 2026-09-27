"use strict";

// Project-scoped quality workbench. Server plans and card revisions are authoritative.
const $ = id => document.getElementById(id);
const qp = {
  project: null, catalogId: "", runtimeId: "", units: [], selected: new Set(),
  support: null, affected: null, plan: null, busy: false, scanning: false,
  stop: false, blocked: true, activeCard: null, drafts: new Map(),
  sourceId: null, toastTimer: null, leaveUrl: null, pendingLeave: null,
  batch: new Set(), group: "", pendingBatch: null,
  // Explicit "writing-duplicate" selection inside one comparison group. It
  // starts empty on purpose: nothing is ever pre-selected, approved or merged.
  write: {keep: null, defers: new Set()},
  // Automatic reference preparation. ``preparePlan`` is the frozen plan the
  // server returned; the confirm button only appears after it exists, so a
  // model call always follows a visible preview plus one explicit confirmation.
  referenceMode: "manual", prepare: null, preparePlan: null, preparationBusy: false,
  pendingMode: null, scopeMode: null, preparePhase: null, connectionIssue: "", reviewDetail: false,
  prepareProgress: null, observedPrepareId: null, prepareOutcomeUnknown: false,
  prepareLastSyncedAt: null, prepareTerminalRefreshedId: null,
  // Manual recovery of one failed scan batch.  The descriptor is frozen by the
  // server; the page only keeps the confirmation state and never reconstructs
  // its unit/card scope locally.
  pendingRetry: null, retryBusy: false, retryNotice: "",
  retrySelection: new Set(), retrySelectionTouched: false,
  pendingRetryBulk: null, retryRun: null, retryParallel: 1,
};
const qNames = {supported:"检查支持", disputed:"存在疑问", insufficient:"证据不足", unchecked:"未检查"};
const qStatus = {pending_review:"待人工复检", approved:"已生效", deferred:"已暂缓", rejected:"已驳回"};
const qBatchLimit = 20;
// Writing-duplicate comparison classes. These are field-comparison results, not
// model or semantic judgements, and never an automatic merge permission.
const qWriteClassNames = {exact:"内容完全重复", evidence:"内容一致但证据不同", fields:"解释/其他字段不同"};
const qWriteClassReasons = {
  exact:"这些卡片的内容字段与原文证据规范化后完全一致，只是书写写法不同。仍不会自动合并，也不会自动批准。",
  evidence:"这些卡片的解释与其他字段一致，但原文证据不同。暂缓其他卡不会把它们的证据迁移到保留卡，也不会扩大保留卡的参考范围。",
  fields:"这些卡片只是表达写法相同或相近，含义、适用语境、译法或优先级不同，不是内容重复。请逐张判断。",
};
const qe = qualityEscape;

function qToast(text) {
  clearTimeout(qp.toastTimer); $("toast").textContent = text; $("toast").hidden = false;
  qp.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
}
function qRanges(numbers) {
  const values = [...new Set(numbers)].sort((a,b) => a-b), out = [];
  for (let i=0; i<values.length; i++) {
    const start=values[i]; let end=start;
    while (values[i+1]===end+1) end=values[++i];
    out.push(start===end ? String(start) : `${start}–${end}`);
  }
  return out.join("、");
}
function qParseRanges(value, units) {
  const text=String(value).trim().replace(/[，、；;]/g,",").replace(/[–—]/g,"-");
  if (!text) throw new Error("请输入编号，例如 1-20,25,30-45。");
  const ids=new Set();
  for (const part of text.split(",")) {
    const match=part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!match) throw new Error("范围格式错误，请用逗号分隔编号或范围。");
    const a=Number(match[1]), b=Number(match[2]||a);
    if (!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||a<1||b<a||b>units.length) throw new Error(`编号范围须在 1–${units.length} 内，起点不得大于终点。`);
    for (let i=a; i<=b; i++) if (String(units[i-1].source||"").trim()) ids.add(units[i-1].id);
  }
  return ids;
}
function qIndexUnits(project) {
  // Follow explicit document reading order. Never guess chapters from prose.
  const nodes=new Map((project.document?.nodes||[]).map(node=>[node.id,node]));
  const sections=new Map(); let current="未分章";
  for (const id of project.document?.reading_order||[]) {
    const node=nodes.get(id); if (!node) continue;
    if (node.type==="heading") current=String(node.source||"").trim()||"未分章";
    for (const unitId of node.unit_ids||[]) {
      // A Unit touching multiple differently named sections is ambiguous.
      sections.set(unitId, sections.has(unitId)&&sections.get(unitId)!==current ? "未分章" : current);
    }
  }
  return (project.units||[]).map((unit,index)=>({...unit, displayNumber:index+1, section:sections.get(unit.id)||"未分章"}));
}
function qCovered() { return new Set(qp.support?.covered_unit_ids||qp.support?.scanned_unit_ids||[]); }
function qFiltered() {
  const query=$("search").value.trim().toLowerCase(), covered=qCovered();
  return qp.units.filter(unit => (!query||`${unit.displayNumber} ${unit.id} ${unit.source}`.toLowerCase().includes(query))
    && (!$("chapter").value||unit.section===$("chapter").value)
    && (!$("scanState").value||(covered.has(unit.id)?"scanned":"new")===$("scanState").value)
    && (!$("selectedOnly").checked||qp.selected.has(unit.id)));
}
async function qRequest(path, body, method, extraOptions={}) {
  const options = {cache:"no-store", ...extraOptions};
  if (body !== undefined) {
    options.method=method||(path.includes("/cards/")?"PUT":"POST");
    options.headers={"Content-Type":"application/json"};
    options.body=JSON.stringify({...body, expected_project_id:qp.runtimeId});
  } else if (path!=="/api/project"&&!path.includes("expected_project_id=")) {
    path += (path.includes("?") ? "&" : "?")+new URLSearchParams({expected_project_id:qp.runtimeId});
  }
  const response=await fetch(path, options);
  const result=await response.json();
  if (!response.ok) {
    const error=new Error(typeof result.detail==="string"?result.detail:JSON.stringify(result.detail||result));
    error.status=response.status; throw error;
  }
  return result;
}
function qCheckProject(project) {
  if (!project?.current_project?.id||!project?.project?.id) throw new Error("请先选择一个项目并导入原文。");
  const wanted=new URLSearchParams(location.search).get("project_id");
  if ((wanted&&wanted!==String(project.current_project.id)) || (qp.runtimeId&&qp.runtimeId!==String(project.project.id))) {
    throw new Error("当前项目已切换，与此页面绑定的项目不一致。请返回项目选择后重新进入。");
  }
}
function qPageError(message) {
  qp.blocked=true; $("pageLoading").hidden=true;
  $("pageError").hidden=false; $("pageError").replaceChildren(document.createTextNode(message+" "));
  const link=document.createElement("a"); link.href="/"; link.textContent="返回项目选择"; $("pageError").append(link);
  $("connectionText").textContent="操作已暂停"; qControls();
}
async function qLoad(initial=false) {
  if (qp.busy || qp.preparationBusy) return;
  const cardDetailOpen=qCardDetailOpenState();
  qCaptureDraft(); qSetBusy(true);
  try {
    const project=await qRequest("/api/project"); qCheckProject(project);
    if (qp.runtimeId && qp.runtimeId !== String(project.project.id)) {
      qp.retrySelection.clear(); qp.retrySelectionTouched=false; qp.retryRun=null;
    }
    qp.catalogId=String(project.current_project.id); qp.runtimeId=String(project.project.id);
    const [support, affected]=await Promise.all([qRequest("/api/project/quality-support"),qRequest("/api/project/quality-support/affected-units")]);
    qCheckProject(await qRequest("/api/project"));
    qp.project=project; qp.units=qIndexUnits(project); qp.support=support; qp.affected=affected;
    if (initial && (support.retryable_batches||[]).some(row=>row.retryable)) $("executionDetails").open=true;
    qp.referenceMode=String(support.reference_mode||"manual"); qp.prepare=support.prepare||null; qp.preparePlan=null;
    const loadedPrepareId=String(support.prepare?.prepare_id||"")||null;
    if(qp.observedPrepareId!==loadedPrepareId){qp.prepareProgress=null;qp.prepareTerminalRefreshedId=null;}
    qp.observedPrepareId=loadedPrepareId;
    qp.prepareOutcomeUnknown=false;
    qp.blocked=false; $("pageError").hidden=true; $("pageLoading").hidden=true; $("qualityWorkspace").hidden=false;
    $("projectName").dataset.i18nIgnore="true"; $("projectName").textContent=project.current_project.name||qp.catalogId; $("connectionText").textContent="本地项目已连接";
    const target=new URLSearchParams(location.search).get("unit_id");
    if (initial && target && qp.units.some(u=>u.id===target&&String(u.source||"").trim())) qp.selected.add(target);
    if (initial) qp.scopeMode = target ? "custom" : "all";
    const known=new Set(qp.units.map(u=>u.id)); qp.selected=new Set([...qp.selected].filter(id=>known.has(id)));
    $("editorLink").href="/editor?"+new URLSearchParams({project_id:qp.catalogId,...(target?{unit_id:target}:{})});
    const previous=$("chapter").value;
    $("chapter").innerHTML='<option value="">全部章节</option>'+[...new Set(qp.units.map(u=>u.section))].map(section=>`<option value="${qe(section)}">${qe(section)}</option>`).join("");
    if ([...$("chapter").options].some(o=>o.value===previous)) $("chapter").value=previous;
    $("nextCount").max=String(qp.units.length); $("totalCount").textContent=`共 ${qp.units.length} 个`;
    if(initial){qp.batch.clear();qp.group="";qWriteReset();}
    qInvalidate(); qRenderUnits(); qRenderCards(); qRenderTerminologyAudit(); qRestoreCardDetailOpenState(cardDetailOpen); qRenderHistory(); qRenderPrepare();
    if(qp.observedPrepareId) await qPollPrepareStatus({manual:true});
  } catch (error) { qPageError(error.message); }
  finally { qSetBusy(false); }
}
async function qRefreshSupport() {
  const cardDetailOpen=qCardDetailOpenState();
  const [support,affected]=await Promise.all([qRequest("/api/project/quality-support"),qRequest("/api/project/quality-support/affected-units")]);
  qCaptureDraft(); qp.support=support; qp.affected=affected;
  qp.referenceMode=String(support.reference_mode||"manual"); qp.prepare=support.prepare||null;
  const latestId=String(support.prepare?.prepare_id||"")||null;
  if(!qp.observedPrepareId&&latestId)qp.observedPrepareId=latestId;
  else if(qp.observedPrepareId&&latestId&&latestId!==qp.observedPrepareId){
    qp.connectionIssue="服务器当前记录的是另一项准备任务；本页仍绑定原任务，没有切换观察。";
  }
  qRenderCards(); qRenderTerminologyAudit(); qRestoreCardDetailOpenState(cardDetailOpen); qRenderHistory(); qRenderUnits(); qRenderPrepare();
}
function qRenderTerminologyAudit() {
  const audit=qp.support?.terminology_audit||{}, mismatches=audit.mismatches||[], conflicts=audit.conflicts||[];
  $("terminologyAuditTitle").textContent=`术语一致性检查 · 疑似 ${mismatches.length} 条 · 冲突 ${conflicts.length} 条`;
  const mismatchRows=mismatches.map(row=>`<div class="history-item"><strong><span data-i18n-ignore="true">${qe(row.unit_id)} · ${qe(row.expression)}</span></strong><p><span>统一译名：</span><span data-i18n-ignore="true">${qe(row.canonical)}</span><span> · </span><span>${qe(row.reason)}</span>${row.found_variants?`<span> · </span><span>其他译法：</span><span data-i18n-ignore="true">${qe(row.found_variants)}</span>`:""}</p><p>当前状态：${qe(row.status)}</p></div>`);
  const conflictRows=conflicts.map(row=>`<div class="history-item"><strong><span data-i18n-ignore="true">${qe(row.unit_id)} · ${qe(row.expression)}</span><span> · </span><span>译名冲突</span></strong><p><span>候选：</span><span data-i18n-ignore="true">${qe((row.canonicals||[]).join("、")||"缺少明确译名")}</span><span> · </span><span>卡片：</span><span data-i18n-ignore="true">${qe((row.card_ids||[]).join("、"))}</span></p><p>此处不强制猜测统一译名，请人工核对义项。</p></div>`);
  $("terminologyAuditList").innerHTML=[...mismatchRows,...conflictRows].join("")||'<p class="tiny">当前未发现可识别的疑似差异或卡片冲突。</p>';
}
function qCardDetailOpenState() {
  const root=$("cardDetail");
  return root?Array.from(root.querySelectorAll("details")).map(detail=>!!detail.open):[];
}
function qRestoreCardDetailOpenState(states) {
  if(!Array.isArray(states))return;
  Array.from($("cardDetail").querySelectorAll("details")).forEach((detail,index)=>{if(index<states.length)detail.open=states[index];});
}
function qSetBusy(value) { qp.busy=value; qControls(); if (!value) qFinishLeave(); }
function qControls() {
  const taskActive=qp.prepareProgress?.active===true||qp.prepareProgress?.status==="running";
  const disabled=qp.busy||qp.blocked||qp.preparationBusy||taskActive;
  const readDisabled=disabled&&!qp.retryBusy;
  for (const id of ["search","chapter","scanState","selectedOnly","reviewState","conceptSearch"]) { const el=$(id); if(el) el.disabled=readDisabled; }
  for (const id of ["nextCount","selectNext","rangeInput","addRange","removeRange","maxParallel","maxWords","clearSelection"]) { const el=$(id); if(el) el.disabled=disabled; }
  $("refreshButton").disabled=qp.busy||qp.preparationBusy;
  for (const id of ["openScope","openSettings","scopeAll","scopeCustom","modeButton"]) if ($(id)) $(id).disabled=disabled;
  $("prepareButton").disabled=disabled||!qScopeUnits().length;
  $("prepareConfirmButton").disabled=disabled;
  // The manual scan controls live in the same panel and belong to manual mode:
  // in automatic mode they are hidden whatever the scan state is, while the
  // manual flow keeps owning their visibility (qInvalidate / qMakePlan).
  const manual = qp.referenceMode !== "automatic";
  if (!manual) {
    $("planButton").hidden = true;
    $("startButton").hidden = true;
    $("terminateButton").hidden = true;
  } else {
    // The preview button is the manual flow's entry point: visible as soon as
    // the project is in manual mode, disabled until something is selected.
    $("planButton").hidden = false;
    $("planButton").disabled=disabled||!qScopeUnits().length;
    // "开始扫描" stays visible while a run is in progress and greys out instead
    // of disappearing; terminating the plan is the only stop control.
    $("startButton").disabled=disabled||qp.scanning||!qp.plan?.batches?.length;
    $("terminateButton").hidden=!(qp.plan||qp.scanning);
    $("terminateButton").disabled=qp.blocked||qp.stop;
  }
  const manualBox = $("prepareManual"); if (manualBox) manualBox.hidden = !manual;
  $("sourceSelect").disabled=disabled;
  $("selectVisible").disabled=disabled||!qFiltered().some(u=>String(u.source||"").trim());
  document.querySelectorAll("[data-unit]").forEach(input=>{input.disabled=disabled||!String(qp.units.find(u=>u.id===input.dataset.unit)?.source||"").trim();});
  for (const id of ["batchApprove","batchDefer","batchReject","batchClear"]) $(id).disabled=disabled;
  // The prepare observer only redraws its own panel. Keep a user's open concept
  // draft editable while that independent server task runs; writes/actions still
  // remain guarded by the ordinary request and revision checks.
  document.querySelectorAll("#cardDetail input,#cardDetail textarea").forEach(el=>{el.disabled=qp.busy||qp.blocked;});
  document.querySelectorAll("#cardDetail button[data-card-action]").forEach(el=>{el.disabled=qp.busy||qp.blocked||(disabled&&el.dataset.cardAction!=="edit");});
  // A defer checkbox blocked by eligibility stays blocked while idle too; the
  // write bar owns the confirm button's disabled state.
  document.querySelectorAll("#cardDetail [data-write-defer]").forEach(el=>{el.disabled=disabled||el.dataset.writeBlocked==="1";});
  document.querySelectorAll("#cardDetail [data-write-keep]").forEach(el=>{el.disabled=disabled;});
  document.querySelectorAll("#cardList button").forEach(el=>{el.disabled=readDisabled;});
  document.querySelectorAll("#cardList input[data-card-select]").forEach(el=>{el.disabled=disabled;});
  document.querySelectorAll("[data-retry-open]").forEach(el=>{el.disabled=disabled||qp.retryBusy||el.dataset.retryable!=="1";});
  document.querySelectorAll("[data-retry-select],[data-retry-select-all]").forEach(el=>{
    el.disabled=disabled||qp.retryBusy||(el.dataset.retrySelectAll!==undefined&&!qRetryRows().some(row=>row.retryable));
  });
  document.querySelectorAll("[data-retry-clear],[data-retry-bulk-open]").forEach(el=>{
    el.disabled=disabled||qp.retryBusy||!qp.retrySelection.size;
  });
  const retryConfirm=$("retryBatchConfirm");
  if (retryConfirm) retryConfirm.disabled=disabled||qp.retryBusy||!qp.pendingRetry;
  const retryCancel=$("retryBatchCancel");
  if (retryCancel) retryCancel.disabled=qp.retryBusy;
  const bulkConfirm=$("retryBulkConfirm");
  if (bulkConfirm) bulkConfirm.disabled=disabled||qp.retryBusy||!qp.pendingRetryBulk?.length;
  const bulkCancel=$("retryBulkCancel");
  if (bulkCancel) bulkCancel.disabled=qp.retryBusy;
  const retryParallel=$("retryParallel");
  if (retryParallel) retryParallel.disabled=disabled||qp.retryBusy;
  const retryShortcut=$("retryShortcut");
  if (retryShortcut) retryShortcut.disabled=readDisabled;
  qUpdateWriteBar();
}
function qRenderUnits() {
  const visible=qFiltered(), covered=qCovered(); const eligible=visible.filter(u=>String(u.source||"").trim());
  $("filteredCount").textContent=`(${eligible.length})`;
  const n=eligible.filter(u=>qp.selected.has(u.id)).length;
  $("selectVisible").checked=!!eligible.length&&n===eligible.length; $("selectVisible").indeterminate=n>0&&n<eligible.length;
  $("unitList").innerHTML=visible.map(unit=>`<div class="unit-row ${qp.selected.has(unit.id)?"selected":""}"><input type="checkbox" aria-label="选择单元 ${unit.displayNumber}" data-unit="${qe(unit.id)}" ${qp.selected.has(unit.id)?"checked":""}><span class="unit-number">${String(unit.displayNumber).padStart(3,"0")}</span><div class="unit-copy"><button class="unit-title" data-source="${qe(unit.id)}" data-i18n-ignore="true">${qe(unit.id)} · ${qe(unit.section)}</button><p data-i18n-ignore="true">${qe(unit.source)}</p></div><div class="unit-meta"><span>${Number(unit.source_words||0)} 词</span><span class="${covered.has(unit.id)?"scanned":""}">${!String(unit.source||"").trim()?"无原文":covered.has(unit.id)?"已扫描":"未扫描"}</span></div></div>`).join("")||'<div class="empty">没有匹配的单元。请调整筛选，或先在工作台导入原文。</div>';
  $("listFootnote").textContent=`显示 ${visible.length} / ${qp.units.length} 个单元`;
  const chosen=qp.units.filter(u=>qp.selected.has(u.id)), hidden=chosen.length-n;
  $("listFootnote").textContent += hidden ? ` · ${hidden} 个已选被筛选隐藏` : "";
  $("selectedCount").textContent=chosen.length;
  $("selectionWords").textContent=chosen.length?`约 ${chosen.reduce((sum,u)=>sum+Number(u.source_words||0),0).toLocaleString()} 个英文词`:"尚未选择单元";
  qControls();
}
function qChangeSelection(fn) { if(qp.busy||qp.blocked||qp.preparationBusy)return; qp.scopeMode="custom"; fn(); qInvalidate(); qRenderUnits(); }
function qInvalidate() {
  // A discarded plan also clears the "stopped" flag of the previous run, so the
  // next preview's terminate button is usable again.
  qp.plan=null; qp.stop=false; $("startButton").hidden=true; $("planButton").hidden=false;
  // The automatic preview freezes the same batch settings and the same scope:
  // editing either one makes the frozen plan stale, so it is discarded too.
  qp.preparePlan=null;
  $("planPreview").innerHTML='<p class="tiny">点「预览扫描计划」查看批次。</p>';
  const settingsEcho = $("settingsSummary");
  if (settingsEcho) settingsEcho.textContent=`并行 ${$("maxParallel").value} 批 · 每批约 ${$("maxWords").value} 词`;
  qRenderPrepare(); qControls();
}
function qInteger(id,min,max) {
  const n=Number($(id).value);
  if (!Number.isInteger(n)||n<min||(max!=null&&n>max)) { $(id).focus(); throw new Error(max==null?`请输入不小于 ${min} 的整数。`:`请输入 ${min}–${max} 之间的整数。`); }
  return n;
}
function qPlanMarkup(plan) {
  const batches=plan.batches||[], planned=batches.flatMap(b=>b.unit_ids||[]);
  const parallel=Math.max(1,Math.min(Number(plan.max_parallel_batches)||1,batches.length||1));
  const first=batches.slice(0,parallel).flatMap(b=>b.unit_ids||[]);
  const heavy=batches.map((b,i)=>({index:i+1,units:(b.unit_ids||[]).length,words:Number(b.source_words)||0})).filter(b=>b.units>10||b.words>4000);
  return `<div class="plan-title"><strong>计划已就绪</strong></div><div class="plan-numbers"><div><strong>${first.length}</strong><span>首轮并行 ${parallel} 批覆盖的单元</span></div><div><strong>${batches.length}</strong><span>批 · 最多 ${Number(plan.max_requests)||0} 次模型请求</span></div></div><p class="tiny">共 ${planned.length} / ${Number(plan.unit_count)||qp.selected.size} 个单元 · 每批约 ${Number(plan.max_source_words)||0} 词</p>${heavy.length?`<p class="notice">第 ${heavy.slice(0,5).map(b=>b.index).join("、")}${heavy.length>5?" 等":""} 批较大，可能变慢或截断。</p>`:""}<details><summary>每批范围</summary><div class="plan-batches">${batches.map((b,i)=>`<div class="batch-row"><span>第 ${i+1} 批 · ${qe(b.unit_ids.join("、"))}</span><span>${Number(b.source_words)||0} 词</span></div>`).join("")}</div></details>`;
}
async function qMakePlan() {
  if(qp.busy||qp.blocked||!qScopeUnits().length)return;
  let options; try { options={max_parallel_batches:qInteger("maxParallel",1),max_source_words:qInteger("maxWords",100)}; } catch(error){qToast(error.message);return;}
  qSetBusy(true); qp.plan=null; qp.stop=false;
  $("planPreview").textContent="正在规划，不调用模型…";
  try {
    const plan=await qRequest("/api/project/quality-support/plan",{scope:"selected",unit_ids:qScopeUnits().map(u=>u.id),...options});
    qp.plan={...plan, runtimeId:qp.runtimeId}; $("planPreview").innerHTML=qPlanMarkup(plan);
    $("planButton").hidden=true; $("startButton").hidden=false;
    $("startButton").textContent=plan.batches?.length?`开始扫描 · ${plan.batches.length} 批`:"没有可执行的批次，请调整选择或参数";
  } catch(error){$("planPreview").textContent=`计划生成失败：${error.message}`;qToast(error.message);}
  finally{qSetBusy(false);}
}
async function qRunPlan() {
  if(qp.busy||qp.blocked||!qp.plan?.batches?.length)return;
  const plan=qp.plan; if(plan.runtimeId!==qp.runtimeId)return;
  if(qScopeAll()) qp.selected=new Set(qScopeUnits().map(u=>u.id));
  qp.scopeMode="custom";
  qp.scanning=true; qp.stop=false; qSetBusy(true); qRenderPrepare();
  let next=0, done=0, failed=0, launched=0, created=0, updated=0, duplicated=0;
  const errors=[], states=plan.batches.map(()=>"等待");
  const progress=()=>{$("planPreview").innerHTML=`<strong>${qp.stop?"正在收尾":"正在扫描"}</strong><p class="tiny">成功 ${done} / ${plan.batches.length} 批 · 失败 ${failed}${qp.stop?" · 不再启动新批次":""}</p><div class="progress"><span style="width:${(done+failed)/plan.batches.length*100}%"></span></div><div class="plan-batches">${states.map((s,i)=>`<div class="batch-row"><span>第 ${i+1} 批</span><span>${s}</span></div>`).join("")}</div>`;};
  const worker=async()=>{
    while(!qp.stop&&next<plan.batches.length){
      const i=next++, batch=plan.batches[i]; launched++; states[i]="运行中"; progress();
      try {
        const result=await qRequest("/api/project/quality-support/scan",{batch_id:batch.batch_id,unit_ids:batch.unit_ids});
        if(result.status!=="ok"||result.batch_id!==batch.batch_id)throw new Error("扫描响应与批次不一致，单元保留待核对。");
        const repair=qRepairText(result.repair);
        if(result.check_status==="failed"||(result.failed||[]).length)throw new Error(`候选已部分保存，但独立检查或部分候选失败${repair?`（${repair}）`:""}。请查看复检与扫描记录；这些单元保留在选择中。`);
        created+=Number(result.created_count)||0; updated+=Number(result.updated_count)||0; duplicated+=Number(result.duplicate_count)||0;
        done++; states[i]=qScanOutcome(result); batch.unit_ids.forEach(id=>qp.selected.delete(id));
      } catch(error) { failed++; states[i]="失败"; errors.push(`第 ${i+1} 批：${error.message}`); if(error.status===409)qp.stop=true; }
      progress();
    }
  };
  const parallelism=Math.max(1,Math.min(Number(plan.max_parallel_batches)||1,plan.batches.length));
  try {
    await Promise.all(Array.from({length:parallelism},()=>worker()));
    try { await qRefreshSupport(); } catch(error){errors.push(`结果刷新失败，请手动刷新：${error.message}`);}
  } finally {
    qp.scanning=false; qp.plan=null; $("planButton").hidden=false; $("startButton").hidden=true;
    $("planPreview").innerHTML=`<strong>${qp.stop?"已终止计划":"扫描结束"}</strong><p class="tiny">成功 ${done} 批，失败 ${failed} 批，未启动 ${plan.batches.length-launched} 批。${qScanTotals(created,updated,duplicated)}${qp.selected.size} 个单元保留在选择中。</p>${errors.map(e=>`<p class="notice">${qe(e)}</p>`).join("")}<button id="goReview" class="full">前往人工复检 →</button>`;
    $("goReview").onclick=()=>qSwitchView("review"); qSetBusy(false); qRenderUnits();
  }
}
function qTerminate() {
  if(qp.scanning){
    // Cooperative stop: in-flight batches finish and are saved; only the
    // launch of further batches stops.
    qp.stop=true; qControls(); qToast("不再启动新批次，已发请求将自然结束。"); return;
  }
  if(!qp.plan)return;
  qInvalidate(); qToast("已终止计划，未调用模型。");
}

function qNorm(value) { return String(value||"").replace(/\s+/g," ").trim().toLowerCase(); }
function qFoldFullwidth(value) {
  // Fullwidth ASCII letters/digits only: the mirror of the server's expression
  // matching map (U+FF10-FF19, U+FF21-FF3A, U+FF41-FF5A). Those three blocks sit
  // 0xFEE0 above their halfwidth twin. Deliberately not NFKC, and never applied
  // to any field other than expressions.
  let out="";
  for(const ch of String(value==null?"":value)){
    const code=ch.charCodeAt(0);
    const fullwidth=(code>=0xFF10&&code<=0xFF19)||(code>=0xFF21&&code<=0xFF3A)||(code>=0xFF41&&code<=0xFF5A);
    out+=fullwidth?String.fromCharCode(code-0xFEE0):ch;
  }
  return out;
}
function qOrtho(value) {
  // The expression matching key this page shares with the server: fullwidth
  // fold, whitespace collapse, lower-casing — except an all-uppercase
  // abbreviation token, which keeps its case so US never equals us. Producing
  // the same key is only a display/grouping hint; it is never permission to
  // merge two cards or to skip one automatically.
  const text=qFoldFullwidth(value).replace(/\s+/g," ").trim();
  return text.split(" ").map(token=>/^[A-Z][A-Z0-9]+$/.test(token)?token:token.toLowerCase()).join(" ");
}
function qCardContent(card) { return card?.draft||card?.approved||{}; }
function qCardExpressions(card) {
  // Grouping uses the same expression matching key as the server, so writing
  // variants group together; it never claims two expressions mean the same
  // thing.
  const seen=new Set(), out=[];
  for(const content of [card?.draft,card?.approved]) {
    if(!content)continue;
    for(const expression of content.expressions||[]) {
      const key=qOrtho(expression); if(!key||seen.has(key))continue; seen.add(key); out.push({key,label:String(expression).trim()});
    }
  }
  return out;
}
function qCardStatusLabel(card) {
  const base=card.status?qStatus[card.status]:(card.approved?"已生效":"未定");
  return base&&card.approved&&card.draft?`${base} · 另有待审草稿`:base;
}
// Two ways a card becomes an effective reference. "manual" is a human approval
// and stays the only thing that may claim one; "automatic" is an automatic
// decision the server says would really be injected today. A card whose
// automatic decision stopped applying has no origin and is back to review only.
function qCardOrigin(card) {
  if(card?.approved)return "manual";
  return card?.automatic?.live?"automatic":"";
}
// The copy an effective card is shown with: the approved one when the human
// approved it, otherwise the draft the automatic decision was made on.
function qEffectiveContent(card) { return card?.approved||card?.draft||{}; }
function qCanonicalNames(card) {
  if(qCardOrigin(card)==="automatic") {
    const bindings=card?.check?.automation_assessment?.bindings||[];
    const names=bindings.filter(row=>row?.kind==="term"&&String(row.preferred_translation||"").trim())
      .map(row=>`${(row.expressions||[]).join("、")} → ${row.preferred_translation}`);
    return names.join("；");
  }
  return String(qEffectiveContent(card).acceptable_translations?.[0]||"").trim();
}
function qOriginLabel(origin) { return origin==="manual"?"人工批准":origin==="automatic"?"自动采用":""; }
function qOriginChip(card) {
  const origin=qCardOrigin(card);
  return origin?`<span class="origin-chip ${origin}">${qOriginLabel(origin)}</span>`:"";
}
function qGroups() {
  // One expression is one group; a card may appear in several groups. Groups
  // only mean "these expressions overlap", never "these cards are equivalent".
  const groups=new Map();
  for(const card of qp.support?.cards||[]) {
    for(const {key,label} of qCardExpressions(card)) {
      let group=groups.get(key); if(!group){group={key,label,cards:[]};groups.set(key,group);}
      if(!group.cards.some(item=>item.id===card.id))group.cards.push(card);
      if(label&&!group.label)group.label=label;
    }
  }
  return [...groups.values()].filter(group=>group.cards.length>1)
    .sort((a,b)=>b.cards.length-a.cards.length||a.label.localeCompare(b.label));
}
function qGroupCards() {
  if(!qp.group)return [];
  return (qp.support?.cards||[]).filter(card=>qCardExpressions(card).some(item=>item.key===qp.group))
    .sort((a,b)=>Number(b.status==="pending_review")-Number(a.status==="pending_review")||a.id.localeCompare(b.id));
}
function qSelectedCards() {
  const byId=new Map((qp.support?.cards||[]).map(card=>[card.id,card]));
  return [...qp.batch].map(id=>byId.get(id)).filter(Boolean);
}
// -- writing-duplicate review -------------------------------------------------
// The comparison below is a plain field comparison. It mirrors the server's
// split exactly: expressions use the writing-variant key, every other field
// keeps the existing case/whitespace normalization.
function qCardContentKey(content) {
  // The writing-variant key belongs to the expressions field alone — the same
  // split the server makes. Every other field keeps the existing case/whitespace
  // normalization, so a fullwidth character inside a translation or a confusion
  // is an ordinary field difference, never "内容完全重复".
  // Lists are encoded as JSON arrays so element boundaries survive: two
  // different splits of the same separator can never collapse into one set.
  const plainSet=list=>[...new Set((list||[]).map(qNorm).filter(Boolean))].sort();
  const expressionSet=list=>[...new Set((list||[]).map(qOrtho).filter(Boolean))].sort();
  return JSON.stringify([
    expressionSet(content.expressions), qNorm(content.meaning), qNorm(content.applies_when),
    plainSet(content.acceptable_translations), plainSet(content.confusions), plainSet(content.open_questions),
    Number(content.priority||0),
  ]);
}
function qCardEvidenceKey(content) {
  const rows=(content.evidence||[]).map(item=>[String(item.unit_id||"").trim(),String(item.source_sha256||"").trim(),qNorm(item.source_excerpt)]).sort();
  return JSON.stringify(rows);
}
function qWriteClass(cards) {
  const contents=cards.map(qCardContent);
  if(new Set(contents.map(qCardContentKey)).size>1)return "fields";
  return new Set(contents.map(qCardEvidenceKey)).size>1?"evidence":"exact";
}
function qHasWritingDifference(cards) {
  // True when two cards carry expression strings that differ as raw text but
  // fold to the same matching key, i.e. there is a writing variant to review.
  for(let i=0;i<cards.length;i++)for(let j=i+1;j<cards.length;j++){
    const a=qCardExpressions(cards[i]),b=qCardExpressions(cards[j]);
    if(a.some(x=>b.some(y=>x.key===y.key&&x.label!==y.label)))return true;
  }
  return false;
}
function qWriteOnlyOn() { return !!$("writeOnly").checked; }
function qWriteDeferProblem(card) {
  // A card may only be deferred by this entry when it is a current pending
  // draft with no effective copy; approved/deferred/rejected cards stay
  // reference-only and a card that also has an approved copy is never in the
  // recommended defer list.
  if(!card)return "卡片已不存在";
  if(card.status!=="pending_review"||!card.draft)return "不是待复检草稿，按卡片逐条处理";
  if(card.approved)return "已有已生效版本，请在该卡复核区单独处理";
  if(qp.drafts.has(card.id))return "有未保存的本地编辑，请先保存或放弃";
  return "";
}
function qWriteReset() { qp.write={keep:null,defers:new Set()}; }
function qWriteState() {
  const cards=qGroupCards();
  const defers=[...qp.write.defers].filter(id=>cards.some(card=>card.id===id)).map(id=>cards.find(card=>card.id===id));
  const keep=qp.write.keep?cards.find(card=>card.id===qp.write.keep)||null:null;
  return {cards,defers:defers.filter(Boolean),keep};
}
function qUpdateWriteBar() {
  const {defers,keep}=qWriteState(), count=$("writeCount"), confirm=$("writeConfirm");
  if(count)count.textContent=`已选暂缓 ${defers.length} / ${qBatchLimit} 张${keep?` · 保留 ${keep.id}`:""}`;
  if(confirm)confirm.disabled=qp.busy||qp.blocked||!defers.length;
}
function qWriteBar() {
  return `<div class="write-bar" id="writeBar"><span id="writeCount" class="write-count">已选暂缓 0 / ${qBatchLimit} 张</span><button id="writeConfirm" class="primary small" data-write-confirm disabled>确认暂缓所选</button><p class="tiny">默认不选择任何卡片；只暂缓你勾选的待审草稿。保留一张不会批准它，也不会把其他卡的证据迁到保留卡上。</p></div>`;
}
function qWriteConfirm() {
  if(qp.busy||qp.blocked)return;
  const {defers,keep}=qWriteState();
  if(!defers.length){qToast("请先勾选要暂缓的卡片。");return;}
  if(defers.length>qBatchLimit){qToast(`每次最多暂缓 ${qBatchLimit} 张，请减少选择。`);return;}
  const problems=defers.map(qWriteDeferProblem).filter(Boolean);
  if(problems.length){qToast(`有 ${problems.length} 张不能暂缓（${problems[0]}），请取消选择。`);return;}
  if(keep&&qp.drafts.has(keep.id)){qToast(`保留的 ${keep.id} 有未保存编辑，请先保存或放弃。`);return;}
  qOpenBatchDialog("defer",defers,keep);
}
function qBatchProblem(action,card) {
  if(!card)return "卡片已不存在";
  if(card.status!=="pending_review"||!card.draft)return "不是待复检草稿，按卡片逐条处理";
  if(qp.drafts.has(card.id))return "有未保存的本地编辑，请先保存或放弃";
  if(action==="approve") {
    const check=card.check;
    if(!check||typeof check!=="object")return "没有独立检查结论";
    const verdict=String(check.verdict||"");
    if(verdict!=="supported")return `独立检查结论是${qNames[verdict]||verdict||"未知"}`;
    if(Number(check.draft_revision||0)!==Number(card.draft_revision||0))return "检查结论不是当前草稿版本的";
  }
  return "";
}
function qBatchClear(message) {
  if(!qp.batch.size){if(message)qToast(message);return;}
  qp.batch.clear(); qRenderCards(); if(message)qToast(message);
}
function qBatchToggle(id,checked) {
  if(checked) {
    if(qp.batch.size>=qBatchLimit){qToast(`每次最多选择 ${qBatchLimit} 张，请先取消其他选择。`);return false;}
    qp.batch.add(id);
  } else qp.batch.delete(id);
  return true;
}
function qFilterNotice() { return "筛选或分组已变化，批量选择已清空。"; }
function qCards() {
  const state=$("reviewState").value, query=$("conceptSearch").value.trim().toLowerCase();
  const group=qp.group?new Set(qGroupCards().map(card=>card.id)):null;
  // The effective view holds both origins, and reads each card's own content:
  // an approved copy when there is one, otherwise the draft the automatic
  // decision was made on. Human approvals rank first, as they do at injection.
  const effective=state==="approved", contentOf=card=>effective?qEffectiveContent(card):(card.draft||card.approved||{});
  const rank=card=>qCardOrigin(card)==="manual"?0:1;
  return (qp.support?.cards||[]).filter(card=>effective?!!qCardOrigin(card):state==="all"?!!(card.draft||card.approved):card.status===state&&!!card.draft)
    .filter(card=>!group||group.has(card.id))
    .filter(card=>JSON.stringify(contentOf(card)).toLowerCase().includes(query))
    .sort((a,b)=>(effective?rank(a)-rank(b):0)||Number(contentOf(b)?.priority||0)-Number(contentOf(a)?.priority||0)||a.id.localeCompare(b.id));
}
function qCaptureDraft() {
  const form=$("cardDetail").querySelector("[data-edit-card]"); if(!form)return;
  const draft=qp.drafts.get(form.dataset.editCard); if(!draft)return;
  for(const field of form.querySelectorAll("[name]")) draft.fields[field.name]=field.value;
}
function qRenderGroups() {
  const writeOnly=qWriteOnlyOn(), all=qGroups();
  // Filtering only changes what is listed. It never reads or writes card data,
  // and it is never a transitive "these are equivalent" relation.
  const groups=writeOnly?all.filter(group=>qHasWritingDifference(group.cards)):all;
  if(qp.group&&!groups.some(group=>group.key===qp.group)){qp.group="";qWriteReset();}
  $("groupCount").textContent=writeOnly?(groups.length?`${groups.length} 组书写重复`:""):(groups.length?`${groups.length} 组`:"");
  $("groupList").innerHTML=groups.map(group=>{const statuses=[...new Set(group.cards.map(qCardStatusLabel))].join(" · ");
    return `<div class="group-row ${group.key===qp.group?"active":""}"><div class="group-copy"><strong>${qe(group.label)}</strong><span>${group.cards.length} 张 · ${qe(statuses)}</span></div><button class="small" data-group="${qe(group.key)}">${group.key===qp.group?"取消分组筛选":"并排比较"}</button></div>`;}).join("")||(writeOnly?'<p class="tiny">当前没有只差书写写法的重复候选。取消筛选可查看全部相关表达分组。</p>':'<p class="tiny">当前没有跨卡片重复出现的表达。</p>');
  return groups;
}
function qRenderBatch() {
  const cards=qSelectedCards();
  $("batchBar").hidden=!cards.length;
  $("batchCount").textContent=`已选 ${cards.length} / ${qBatchLimit} 张待复检概念`;
  const blockedApprove=cards.filter(card=>qBatchProblem("approve",card));
  for(const id of ["batchApprove","batchDefer","batchReject"])$(id).disabled=!cards.length;
  const hints=[];
  if(blockedApprove.length)hints.push(`其中 ${blockedApprove.length} 张不满足批量批准条件（检查结论或本地编辑），仍可逐条核对后批准。`);
  $("batchHint").hidden=!hints.length;
  $("batchHint").textContent=hints.join(" ");
  return cards;
}
function qRenderCards() {
  qCaptureDraft(); qRenderGroups(); const cards=qCards();
  $("pendingCount").textContent=Number(qp.support?.counts?.pending_review||0);
  const all=qp.support?.cards||[];
  const manual=all.filter(card=>qCardOrigin(card)==="manual").length;
  const automatic=all.filter(card=>qCardOrigin(card)==="automatic").length;
  // Counts what may take part in reference selection, not what was injected:
  // a unit's frozen snapshot is the only record of what it actually used.
  $("referenceVersion").textContent=`有效参考：人工批准 ${manual} 张 · 自动采用 ${automatic} 张。卡片解释不承诺每次注入；命中、每单元 6 张卡／6000 字符上限与人工优先决定解释是否注入，命中的统一译名另随规则发送，以该单元的冻结快照为准 · 逐条批准${qp.group?` · 分组筛选：${qGroups().find(group=>group.key===qp.group)?.label||qp.group}`:""}`;
  if(!cards.some(c=>c.id===qp.activeCard)) qp.activeCard=cards[0]?.id||null;
  const pending=["all","pending_review"].includes($("reviewState").value);
  $("cardList").innerHTML=cards.map(card=>{const content=$("reviewState").value==="approved"?qEffectiveContent(card):(card.draft||card.approved||{});
    const label=(content.expressions||[]).join("、")||"未命名概念";
    const item=`<button class="concept-item ${card.id===qp.activeCard?"active":""}" data-card="${qe(card.id)}" ${card.id===qp.activeCard?'aria-current="true"':""}><strong data-i18n-ignore="true">${qe(label)}</strong><p><span data-i18n-ignore="true">${qe((content.acceptable_translations||[]).join("、"))}</span>${qp.drafts.has(card.id)?'<span> · 有编辑草稿</span>':""}</p>${qOriginChip(card)}<span class="badge ${qe(card.check?.verdict||"unchecked")}">${qe(qNames[card.check?.verdict]||"未检查")}</span></button>`;
    return `<div class="concept-row">${pending&&card.status==="pending_review"?`<input type="checkbox" class="card-check" data-card-select="${qe(card.id)}" aria-label="选择概念 ${qe(label)}" ${qp.batch.has(card.id)?"checked":""}>`:""}${item}</div>`;}).join("")||'<div class="empty">此列表暂无概念</div>';
  qRenderBatch();
  qRenderCard();
}
function qCompareMarkup() {
  const cards=qGroupCards(); if(cards.length<2)return "";
  const label=qGroups().find(group=>group.key===qp.group)?.label||qp.group;
  const values=pick=>cards.map(card=>qNorm(pick(qCardContent(card))));
  const differing=pick=>new Set(values(pick)).size>1;
  const differs={meanings:differing(content=>content.meaning),applies:differing(content=>content.applies_when),
    translations:differing(content=>(content.acceptable_translations||[]).join("｜"))};
  const pendingCount=cards.filter(card=>card.status==="pending_review"&&card.draft).length;
  const writeClass=qWriteClass(cards);
  const items=cards.map((card,index)=>{const content=qCardContent(card),check=card.check&&typeof card.check==="object"?card.check:null;
    const evidence=(content.evidence||[]).map(item=>`<blockquote>${qe(item.source_excerpt||"")}<span class="evidence-caption"> · ${qe(item.unit_id)} · 原文版本 ${qe(String(item.source_sha256||"").slice(0,12))}</span></blockquote>`).join("")||'<p class="tiny">没有可显示的原文证据。</p>';
    const row=(name,value,diff)=>`<div class="cmp-row ${diff?"diff":""}"><dt>${name}${diff?'<span class="diff-tag">有差异</span>':""}</dt><dd>${qe(value||"（空）")}</dd></div>`;
    // Selection defaults to nothing; an ineligible card cannot even be ticked.
    // There is deliberately no per-card "keep this one" shortcut here: every
    // deferral (including the cards left behind by a kept card) goes through
    // the guarded tick -> confirm flow, so an effective copy, a status problem
    // or an unsaved edit can never be bypassed.
    const deferProblem=qWriteDeferProblem(card), deferred=qp.write.defers.has(card.id), kept=qp.write.keep===card.id;
    const writeControls=`<div class="cmp-write"><label class="check-label" title="${qe(deferProblem||"暂缓这一张待审草稿")}"><input type="checkbox" data-write-defer="${qe(card.id)}" data-write-blocked="${deferProblem?1:0}" ${deferred?"checked":""} ${deferProblem?"disabled":""}>暂缓这一张</label><label class="check-label"><input type="radio" name="qWriteKeep" data-write-keep="${qe(card.id)}" ${kept?"checked":""}>保留这一张</label>${deferProblem?`<span class="tiny">${qe(deferProblem)}</span>`:""}</div>`;
    return `<article class="cmp-item ${card.id===qp.activeCard?"active":""}"><header><span class="cmp-index">${index+1}</span><strong>${qe((content.expressions||[]).join("、")||"未命名概念")}</strong><span class="badge ${qe(check?.verdict||"unchecked")}">${qe(check?.verdict?qNames[check.verdict]:"未检查")}</span><span class="tiny">${qe(qCardStatusLabel(card))} · ${qe(card.id)}</span></header>${writeControls}<dl>${row("含义",content.meaning,differs.meanings)}${row("候选译法",(content.acceptable_translations||[]).join("、"),differs.translations)}${row("适用语境",content.applies_when,differs.applies)}${row("检查结果",check?(check.reasons||[]).join("；")||"无备注":"没有独立检查结论",false)}</dl><details class="cmp-evidence"><summary>证据摘要（${(content.evidence||[]).length} 条）</summary>${evidence}</details><div class="cmp-actions"><button class="small" data-compare="${qe(card.id)}">查看并处理这一张</button></div></article>`;}).join("");
  return `<section class="compare"><div class="compare-head"><div><strong>相关表达分组 · ${qe(label)}</strong><p class="tiny">同一表达出现在 ${cards.length} 张卡上（待复检 ${pendingCount} 张）。只表示表达可能相关，不代表语义等价；差异仅供参考，请自行判断是否同一含义。</p><p class="cmp-class ${qe(writeClass)}"><span class="diff-tag">${qe(qWriteClassNames[writeClass])}</span> ${qe(qWriteClassReasons[writeClass])}</p></div><button class="text-button" data-group="${qe(qp.group)}">取消分组筛选</button></div>${qWriteBar()}${items}</section>`;
}
function qRenderCard() {
  const card=(qp.support?.cards||[]).find(c=>c.id===qp.activeCard);
  const groupCompare=qCompareMarkup();
  if(!card){
    // A group's comparison is its own workspace: it keeps every status visible
    // even when the current state filter has no card left to show.
    $("cardDetail").innerHTML=groupCompare?`${groupCompare}<div class="empty"><strong>当前状态下没有可逐条处理的卡片</strong>分组比较仍显示该表达的全部卡片与状态；可取消分组筛选或切换状态。</div>`:'<div class="empty"><strong>暂无需要显示的概念</strong>可以切换状态，或回到“准备参考”生成候选。</div>';
    return;
  }
  const compare=groupCompare
  const origin=qCardOrigin(card), approved=origin==="manual";
  const content=(origin?qEffectiveContent(card):card.draft)||{}, draft=!approved&&qp.drafts.get(card.id);
  const automatic=card.automatic&&typeof card.automatic==="object"?card.automatic:null;
  const liveUnits=(automatic?.live_units||[]).length;
  // Both origins are effective, and the page says which one is in force. The
  // automatic note also reports a decision that stopped applying, so a card
  // that silently dropped out of the automatic channel is visible as such.
  // The card explanation is budgeted; matching canonical terms are sent as
  // compact rules separately. The frozen per-unit snapshot records both.
  const automaticNote=!automatic?""
    :approved?"该卡已由人工批准，原来的自动决定不再注入。"
    :automatic.live?`自动采用：适用范围 ${liveUnits} 个单元（决策 ${qe(automatic.decision_id||"未记录")}）。自动采用不是人工批准；卡片解释仍受命中、每个单元 6 张卡／6000 字符上限与人工优先影响，命中的统一译名另随规则发送，以该单元的冻结快照为准。在这里批准后转为人工批准版本；暂缓或驳回会立即停止这条自动参考。`
    :`自动参考已失效，当前不会进入参考选择：${qe((automatic.problems||[]).map(item=>String(item).replace(/。$/,"")).join("；")||"该决定已不再适用")}。`;
  const kicker=approved?`人工批准 · 生效版本 ${Number(card.approved_revision)||0} · 有效参考`
    :origin==="automatic"?`自动采用 · 有效参考 · 适用范围 ${liveUnits} 个单元`
    :`${qe(qStatus[card.status]||card.status)} · 草稿版本 ${Number(card.draft_revision)||0}${card.approved?" · 另有已生效版本（本次审核不会自动撤销旧版）":""}`;
  const verdict=card.check?.verdict||"unchecked";
  const evidence=(content.evidence||[]).map(item=>`<section class="evidence"><div class="evidence-head"><span>原文证据 · ${qe(item.unit_id)}</span><button class="text-button" data-source="${qe(item.unit_id)}">查看上下文 ↗</button></div><blockquote>${qe(item.source_excerpt||"")}</blockquote><p class="evidence-caption">原文版本 ${qe(String(item.source_sha256||"").slice(0,12))}</p></section>`).join("")||'<p class="notice">该概念没有可显示的原文证据。</p>';
  const checkReasonText=(card.check?.reasons||[]).join("；");
  const checkNoteMarkup=approved
    ? '<span>以下展示人工批准的内容；独立检查记录针对候选草稿，可能与当前生效版本不同。</span>'
    : checkReasonText?`<span data-i18n-ignore="true">${qe(checkReasonText)}</span>`:'<span>请核对解释、适用范围和原文证据后逐条判断。</span>';
  const confusionText=(content.confusions||[]).join("；");
  const confusionMarkup=confusionText?`<p data-i18n-ignore="true">${qe(confusionText)}</p>`:'<p>未列出</p>';
  const openQuestionText=(content.open_questions||[]).join("；");
  const openQuestionsMarkup=openQuestionText?`<p data-i18n-ignore="true">${qe(openQuestionText)}</p>`:'<p>未列出</p>';
  const originBatchId=String(card.origin?.batch_id||"").trim();
  const originUnitIds=(card.origin?.unit_ids||[]).join("、");
  const originMarkup=`<p><span>来源：</span>${originBatchId?`<span data-i18n-ignore="true">${qe(originBatchId)}</span>`:'<span>未记录</span>'}<span> · </span><span data-i18n-ignore="true">${qe(originUnitIds)}</span></p>`;
  const canonical=qCanonicalNames(card);
  const canonicalNote=origin&&canonical?`<p class="auto-note live">当前统一译名（按适用义项）：${qe(canonical)}。其他候选只供人工比较，不参与模型参考。</p>`
    :origin==="automatic"?'<p class="auto-note">尚无检查确定的统一译名；候选只供人工比较，该卡暂不施加译名硬约束。</p>':"";
  $("cardDetail").innerHTML=compare+canonicalNote+`<div class="detail-heading"><div><div class="detail-kicker">${kicker}</div><h2>${qe((content.expressions||[]).join("、")||"未命名概念")}</h2></div><span class="badge ${qe(verdict)}">${qe(qNames[verdict]||"未检查")}</span></div><div class="check-note ${qe(verdict)}">${checkNoteMarkup}</div>${automaticNote?`<p class="auto-note${origin==="automatic"?" live":""}">${automaticNote}</p>`:""}${draft?qEditMarkup(card,draft):`<section class="detail-section"><div class="section-label">概念含义</div><p class="meaning">${qe(content.meaning||"")}</p></section><div class="concept-fields"><section><div class="section-label">候选译法</div><span class="translation-chip">${qe((content.acceptable_translations||[]).join("、"))}</span></section><section><div class="section-label">适用语境</div><p>${qe(content.applies_when||"")}</p></section></div><section class="detail-section"><div class="section-label">容易混淆</div>${confusionMarkup}</section>`}<details class="detail-expand"><summary>原文证据 · ${(content.evidence||[]).length} 条</summary>${evidence}</details><details class="detail-expand"><summary>待确认问题</summary>${openQuestionsMarkup}</details><details class="detail-expand"><summary>来源批次与影响范围</summary>${originMarkup}${qAffectedMarkup()}</details><div class="review-actions">${draft?'<span class="tiny">请先保存或取消编辑，再进行审核。</span>':approved?'<span class="tiny">人工批准的内容 · 有效参考</span>':card.status==="pending_review"?'<button data-card-action="edit">编辑解释</button><button data-card-action="reject" class="quiet">驳回</button><button data-card-action="defer" class="quiet">暂缓</button><button data-card-action="approve" class="primary">批准并查看下一条 →</button>':'<button data-card-action="restore" class="primary">重新放回复检队列</button>'}</div><p id="cardError" role="alert">${qe(draft?.error||"")}</p><p class="approval-hint">${draft?"页内切换保留编辑。保存不是批准；发生版本冲突时先保留输入，再刷新核对。":"只处理当前概念，不自动改写译文。"}</p>`;
  qControls();
}
function qAffectedMarkup() {
  const affected=qp.affected||{}, items=affected.affected||[];
  return `<p>项目整体可能受影响：${items.length} 个单元（不是当前卡片的精确影响清单，也不代表发现误译）。${Number(affected.unknown_reference_count)||0} 个无历史参考记录。</p>${items.length?`<p data-i18n-ignore="true">${qe(items.map(i=>i.unit_id).join("、"))}</p>`:""}`;
}
function qEditMarkup(card,draft) {
  const field=(key,label,rows=2)=>`<label>${label}<textarea name="${key}" rows="${rows}">${qe(draft.fields[key])}</textarea></label>`;
  return `<div class="edit-fields" data-edit-card="${qe(card.id)}">${field("expressions","表达 / 别名（顿号分隔）",1)}${field("acceptable_translations","译名候选（首项为统一译名，顿号分隔）",1)}${field("meaning","解释与区别",3)}<details class="detail-expand"><summary>更多编辑字段</summary>${field("applies_when","适用语境")}${field("confusions","易混淆（顿号分隔）")}${field("open_questions","待确认问题（顿号分隔）")}<label>人工优先级（0–100）<input type="number" name="priority" min="0" max="100" value="${qe(draft.fields.priority)}"></label></details></div><div class="q-edit-bar"><span>编辑草稿 · 切换卡片会保留</span><div><button class="quiet small" data-card-action="cancel">取消修改</button><button class="primary small" data-card-action="save">保存修改</button></div></div>`;
}
function qNewDraft(card) {
  const source=card.draft||{}, fields={};
  for(const key of ["expressions","meaning","acceptable_translations","applies_when","confusions","open_questions","priority"]) fields[key]=Array.isArray(source[key])?source[key].join("、"):String(source[key]??(key==="priority"?0:""));
  return {fields, revision:qp.support.revision, draftRevision:card.draft_revision, evidence:structuredClone(source.evidence||[]), error:""};
}
function qDraftContent(draft) {
  const f=draft.fields, split=value=>String(value).split(/[、,，]/).map(v=>v.trim()).filter(Boolean);
  if(!f.meaning.trim()||!split(f.expressions).length||!split(f.acceptable_translations).length) throw new Error("请填写英文表达、概念含义与候选译法。");
  const priority=Number(f.priority); if(!Number.isInteger(priority)||priority<0||priority>100)throw new Error("优先级须为 0–100 之间的整数。");
  return {expressions:split(f.expressions),meaning:f.meaning.trim(),acceptable_translations:split(f.acceptable_translations),applies_when:f.applies_when.trim(),confusions:split(f.confusions),open_questions:split(f.open_questions),priority,evidence:draft.evidence};
}
async function qCardAction(action) {
  if(qp.busy||qp.blocked||((qp.preparationBusy||qPrepareIsActive())&&action!=="edit"))return; qCaptureDraft();
  const card=(qp.support?.cards||[]).find(c=>c.id===qp.activeCard); if(!card)return;
  if(action==="edit"){qp.drafts.set(card.id,qp.drafts.get(card.id)||qNewDraft(card));qRenderCards();return;}
  if(action==="cancel"){qp.drafts.delete(card.id);$("cardDetail").replaceChildren();qRenderCards();return;}
  const draft=qp.drafts.get(card.id), payload={action:action==="save"||action==="restore"?"edit":action,expected_revision:qp.support.revision,expected_draft_revision:card.draft_revision};
  if(action==="save") {
    try{payload.content=qDraftContent(draft);}catch(error){$("cardError").textContent=error.message;return;}
    payload.expected_revision=draft.revision; payload.expected_draft_revision=draft.draftRevision;
    if(draft.draftRevision!==card.draft_revision||draft.revision!==qp.support.revision){
      draft.error="数据版本已更新，输入已保留。请复制需要保留的修改，再放弃编辑并基于最新版本重新编辑。";$("cardError").textContent=draft.error;return;
    }
  } else if(action==="restore") payload.content=card.draft;
  const next=qCards().filter(c=>c.id!==card.id)[0]?.id||null;
  qSetBusy(true);
  try {
    await qRequest(`/api/project/quality-support/cards/${encodeURIComponent(card.id)}`,payload);
    qp.drafts.delete(card.id); $("cardDetail").replaceChildren();
    if(action!=="save")qp.activeCard=next;
    try { await qRefreshSupport(); qToast(action==="approve"?"当前概念已批准，未改写译文。":"操作已保存。"); }
    catch(error){qPageError(`操作已保存，但刷新失败：${error.message}。请刷新概念数据，勿重复提交。`);}
  } catch(error) {
    if(draft)draft.error=`保存失败：${error.message}。输入已保留。`;
    $("cardError").textContent=draft?.error||`操作失败：${error.message}`;
  } finally {qSetBusy(false);}
}
function qScanOutcome(result) {
  // Distinguish new cards, refreshed cards and exact duplicates: a duplicate is
  // not presented as a new candidate.
  const parts=[];
  const created=Number(result.created_count)||0, updated=Number(result.updated_count)||0, duplicated=Number(result.duplicate_count)||0;
  if(created)parts.push(`新增 ${created}`);
  if(updated)parts.push(`更新 ${updated}`);
  if(duplicated)parts.push(`重复跳过 ${duplicated}`);
  const repair=qRepairText(result.repair);
  const summary=parts.join(" · ")||"没有候选";
  return `${summary}${repair?` · ${repair}`:""}`;
}
function qScanTotals(created,updated,duplicated) {
  const parts=[];
  if(created)parts.push(`新增 ${created} 张`);
  if(updated)parts.push(`更新 ${updated} 张`);
  if(duplicated)parts.push(`完全重复跳过 ${duplicated} 张`);
  return parts.length?`${parts.join("，")}。`:"";
}
function qRepairText(repair) {
  // Only the two model calls of a batch carry repair bookkeeping; a batch that
  // succeeded on the first round shows nothing.
  const entries=Object.values(repair||{}).filter(entry=>entry&&typeof entry==="object");
  if(!entries.length)return "";
  const rounds=Math.max(...entries.map(entry=>Number(entry.round)||1));
  if(rounds<=1)return "";
  const calls=entries.reduce((sum,entry)=>sum+(Number(entry.api_calls)||0),0);
  return `修正 ${rounds-1} 轮 · ${calls} 次请求`;
}
function qRenderHistory() {
  const batches=(qp.support?.batches||[]).slice(-5).reverse();$("historyCount").textContent=`最近 ${batches.length} 批`;
  $("historyList").innerHTML=batches.map(b=>{const repair=qRepairText(b.repair),created=Number(b.created_count)||0,updated=Number(b.updated_count)||0,duplicated=Number(b.duplicate_count)||0;
    const split=[created?`新增 ${created}`:"",updated?`更新 ${updated}`:"",duplicated?`重复跳过 ${duplicated}`:""].filter(Boolean).join(" · ");
    return `<div class="history-item"><strong>${qe(b.batch_id)}</strong><p>${qe(b.status)} · 候选 ${Number(b.candidate_count)||0} · 保存 ${Number(b.saved_count)||0}${split?`（${qe(split)}）`:""} · 失败 ${Number(b.failed_count)||0}${repair?` · ${qe(repair)}`:""}</p><p>${qe((b.unit_ids||[]).join("、"))}</p><p>${qe(b.at||"")}</p></div>`;}).join("")||'<p class="tiny">暂无扫描记录</p>';
}
function qActionLabel(action) { return {approve:"批准",defer:"暂缓",reject:"驳回"}[action]||action; }
function qBatchChecklist(cards) {
  // Every card is shown, but the page never claims the operator has read it.
  return cards.map(card=>{const content=card.draft||{},check=card.check&&typeof card.check==="object"?card.check:null;
    return `<article class="batch-item"><header><strong>${qe((content.expressions||[]).join("、")||"未命名概念")}</strong><span class="badge ${qe(check?.verdict||"unchecked")}">${qe(check?.verdict?qNames[check.verdict]:"未检查")}</span></header><dl><div><dt>含义</dt><dd>${qe(content.meaning||"（空）")}</dd></div><div><dt>适用语境</dt><dd>${qe(content.applies_when||"（空）")}</dd></div><div><dt>候选译法</dt><dd>${qe((content.acceptable_translations||[]).join("、")||"（空）")}</dd></div><div><dt>检查结论</dt><dd>${qe(check?(check.reasons||[]).join("；")||"无备注":"没有独立检查结论")}</dd></div></dl><p class="tiny">${qe(card.id)} · 草稿版本 ${Number(card.draft_revision)||0}</p></article>`;}).join("");
}
function qBatchList(cards) {
  return cards.map(card=>{const content=card.draft||{};
    return `<p class="batch-line">${qe(card.id)} · ${qe((content.expressions||[]).join("、")||"未命名概念")} · ${qe(content.meaning||"（空）")}</p>`;}).join("");
}
function qOpenBatchDialog(action,cards,keepCard) {
  qp.pendingBatch={action,cards,keepCard:keepCard||null};
  const keepLabel=keepCard?((qCardContent(keepCard).expressions||[]).join("、")||keepCard.id):"";
  if(keepCard) {
    $("batchTitle").textContent="保留一张，其余暂缓";
    // The preview only ever promises a defer of the listed cards. The kept card
    // is not approved here, and the wording must not suggest otherwise.
    $("batchMessage").textContent=`只暂缓其余 ${cards.length} 张；保留的 ${keepCard.id}（${keepLabel}）不做任何改动——保留不等于批准，需要批准时请单独点“批准并查看下一条”。这不是合并：证据与卡片都保留，可随时重新放回复检队列。`;
    $("batchConfirm").textContent=`暂缓 ${cards.length} 张`;
  } else if(action==="approve") {
    $("batchTitle").textContent="确认批量批准";
    $("batchMessage").textContent=`将批准 ${cards.length} 张待复检概念。批准只更新翻译参考，不改写已有译文，也不会自动撤销旧版；只有独立检查为 supported 且未被本地修改的待审卡才会出现在这里。`;
    $("batchConfirm").textContent=`批准 ${cards.length} 张`;
  } else {
    $("batchTitle").textContent=`确认批量${qActionLabel(action)}`;
    $("batchMessage").textContent=`将${qActionLabel(action)} ${cards.length} 张待复检概念；${action==="defer"?"可随时重新放回复检队列。":"不会删除卡片，也不会删除证据。"}`;
    $("batchConfirm").textContent=`${qActionLabel(action)} ${cards.length} 张`;
  }
  $("batchList").innerHTML=keepCard?qBatchList(cards):(action==="approve"?qBatchChecklist(cards):qBatchList(cards));
  $("batchDialog").showModal();
}
function qRequestBatch(action,cards) {
  const support=qp.support||{};
  return qRequest("/api/project/quality-support/cards/batch-action",{
    action,
    expected_revision:Number(support.revision||0),
    items:cards.map(card=>({card_id:card.id,expected_draft_revision:Number(card.draft_revision||0)})),
  },"POST");
}
async function qBatchSend(action,cards) {
  const problems=cards.map(card=>({card,reason:qBatchProblem(action,card)})).filter(row=>row.reason);
  if(problems.length) {
    const first=problems[0];
    qToast(`${problems.length} 张不符合${qActionLabel(action)}条件（例如 ${(qCardContent(first.card).expressions||[]).join("、")||first.card.id}：${first.reason}）；请取消选择后逐条处理。`);
    return;
  }
  qSetBusy(true);
  try {
    const result=await qRequestBatch(action,cards);
    qp.batch.clear();
    qWriteReset();
    try { await qRefreshSupport(); qToast(`已${qActionLabel(action)} ${Number(result.count)||cards.length} 张概念，未改写译文。`); }
    catch(error) { qPageError(`批量操作已保存，但刷新失败：${error.message}。请刷新概念数据，勿重复提交。`); }
  } catch(error) {
    qToast(`批量${qActionLabel(action)}未执行：${error.message}`);
    if(error.status===409) { try { await qRefreshSupport(); } catch(refreshError) { /* the toast above already explains it */ } }
  } finally { qSetBusy(false); }
}
async function qKeepOne(keepCard,rest) {
  // "保留" only means "do not defer this card". It is never an approval: the
  // kept card keeps its status, draft, approved copy, check and revisions, and
  // approving it stays an explicit separate click in the review pane.
  if(!rest.length){qToast("该组没有其他待复检卡片需要暂缓。");return;}
  qSetBusy(true);
  let applied;
  try {
    applied=await qRequestBatch("defer",rest);
  } catch(error) {
    // Nothing else is written after a failure or a 409: the selection and the
    // operator's input stay exactly as they are.
    qSetBusy(false);
    qToast(`暂缓未执行，保留的 ${keepCard.id} 未做任何改动：${error.message}`);
    if(error.status===409) { try { await qRefreshSupport(); } catch(refreshError) { /* the toast above explains it */ } }
    return;
  }
  rest.forEach(card=>qp.batch.delete(card.id));
  qWriteReset();
  qSetBusy(false);
  try { await qRefreshSupport(); }
  catch(error) { qPageError(`暂缓已保存，但刷新失败：${error.message}。请刷新概念数据，勿重复提交。`); return; }
  qToast(`已暂缓 ${Number(applied.count)||rest.length} 张；保留的 ${keepCard.id} 未做任何改动，需要批准请单独点“批准”。`);
}
async function qBatchConfirm() {
  const pending=qp.pendingBatch; qp.pendingBatch=null;
  $("batchDialog").close();
  if(!pending)return;
  if(pending.keepCard) { await qKeepOne(pending.keepCard,pending.cards); return; }
  await qBatchSend(pending.action,pending.cards);
}
function qSwitchView(view) {
  qCaptureDraft(); $("scanView").hidden=view!=="scan"; $("reviewView").hidden=view!=="review";
  for(const name of ["scan","review"]){$(name+"Tab").classList.toggle("active",name===view);if(name===view)$(name+"Tab").setAttribute("aria-current","page");else $(name+"Tab").removeAttribute("aria-current");}
  if(view==="review"){qRenderCards();qReviewLayout();}
}
function qOpenSource(id) {
  const index=qp.units.findIndex(u=>u.id===id);if(index<0){qToast("当前项目没有对应原文，可能已重新导入。");return;}
  const unit=qp.units[index];qp.sourceId=id;$("sourceTitle").textContent=`单元 ${unit.displayNumber} · ${id}`;
  $("sourceText").innerHTML=qp.units.slice(Math.max(0,index-1),index+2).map(u=>`<section><h3>${u.id===id?"当前原文":u.displayNumber<unit.displayNumber?"前文":"后文"} · ${qe(u.id)}</h3><p>${qe(u.source)}</p></section>`).join("");
  $("sourceReference").innerHTML=qualityReferenceSnapshotMarkup(unit);
  $("sourceSelect").textContent=qp.selected.has(id)?"移出扫描选择":"加入扫描选择";qControls();$("sourceDialog").showModal();
}
function qFinishLeave() {
  if(!qp.busy&&qp.pendingLeave){const url=qp.pendingLeave;qp.pendingLeave=null;qp.drafts.clear();location.href=url;}
}
// -- automatic reference preparation ----------------------------------------
// The page never decides anything here: the server owns the eligibility rules,
// this section only shows the mode, runs the approved phases and renders the
// summary. Page load and mode switching never call a model.
function qPrepareCounts() {
  const p = qp.prepare || {};
  return {
    adopted: Number(p.adopted || 0),
    skipped: Number(p.skipped || 0),
    unresolved: Number(p.unresolved || 0),
    protected: Number(p.protected || 0),
    ineligible: Number(p.ineligible || 0),
    unresolvedGroups: Number(p.unresolved_groups || 0),
    status: String(p.status || ""),
    budget: (p.budget && typeof p.budget === "object") ? {
      limit: Number(p.budget.limit || 0),
      used: Number(p.budget.used || 0),
      byKind: (p.budget.by_kind && typeof p.budget.by_kind === "object") ? p.budget.by_kind : {},
    } : null,
    plannedUnits: Number(p.planned_units || 0),
    failedUnits: Number(p.failed_units || 0),
    unexecutedUnits: Number(p.unexecuted_units || 0),
    finishedUnits: Number(p.finished_units || 0),
    reusedUnits: Number(p.reused_units || 0),
    processedUnits: Number(p.processed_units || 0),
    reusedGroups: Number(p.reused_groups || 0),
    judgedGroups: Number(p.judged_groups || 0),
    reasons: Array.isArray(p.not_adopted_reasons) ? p.not_adopted_reasons : [],
    reuseNote: String(p.reuse_note || ""),
    status: String(p.status || ""),
    committed: !!p.committed,
    requests: p.requests || {},
    // A5 split: an answered question, an open one, an old check that had to be
    // re-done, one bounded lookup, one unit's local judgment and the work the
    // shared pool could not pay for are six different statements. None of them
    // may be added into one "done" number.
    eligibleUnits: Number(p.eligible_units || 0),
    uncoveredUnits: Number(p.uncovered_units || 0),
    coveredUnits: Number(p.covered_units || 0),
    aiResolvedCards: Number(p.ai_resolved_cards || 0),
    resolvedQuestions: Number(p.resolved_questions || 0),
    remainingQuestions: Number(p.remaining_questions || 0),
    refreshedChecks: Number(p.refreshed_checks || 0),
    recheckPending: Number(p.recheck_pending || 0),
    lookupRounds: Number(p.lookup_rounds || 0),
    lookupHits: Number(p.lookup_hits || 0),
    lookupMisses: Number(p.lookup_misses || 0),
    // 补查的三种结局各自成数：写入、请求失败、写入被拒（已执行但没写进去）。
    lookupRefreshed: Number(p.lookup_refreshed || 0),
    lookupFailed: Number(p.lookup_failed || 0),
    lookupRejected: Number(p.lookup_rejected || 0),
    // 同一内容与依据的补查已经执行过（不计为本次请求），以及因输入超限而
    // 没有发出的补查：两者都不是"已采用"，也不是"没找到"。
    lookupSettled: Number(p.lookup_settled || 0),
    lookupDeferred: Number(p.lookup_deferred || 0),
    localGroups: Number(p.local_groups || 0),
    localUnitsJudged: Number(p.local_units_judged || 0),
    localUnitsOversized: Number(p.local_units_oversized || 0),
    // 已判单元从上次确认直接交回：既不是本次判断，也不是未完成。
    localUnitsReused: Number(p.local_units_reused || 0),
    localUnitsPending: Number(p.local_units_pending || 0),
    oversizedGroups: Number(p.oversized_groups || 0),
    noCandidateUnits: Number(p.no_candidate_units || 0),
    budgetPending: Number(p.budget_pending || 0),
    // 预算口径：基础重查不占额外预算，一次补查占一格；两者上限不同，不能相加。
    basis: (p.budget_basis && typeof p.budget_basis === "object") ? p.budget_basis : null,
  };
}
function qBudgetBasisText(basis) {
  if (!basis) return "";
  const en = window.Mode2I18n?.locale === "en";
  const recheck = basis.recheck || {}, lookup = basis.lookup || {};
  const rlimits = recheck.limits || {}, llimits = lookup.limits || {};
  const fmt = (limits, keys) => keys
    .filter(key => Number(limits[key] || 0) > 0)
    .map(key => `${Number(limits[key])}${en ? ({cards:" cards",units:" source units",words:" English words",chars:" characters"}[key]) : (key === "cards" ? " 卡" : key === "units" ? " 源单元" : key === "words" ? " 英文词" : " 字符")}`)
    .join(" / ");
  const parts = [];
  const recheckText = fmt(rlimits, ["cards", "units", "chars"]);
  if (recheckText) parts.push(en ? `Baseline recheck uses no extra budget (up to ${recheckText} per request; excess remains unfinished)` : `基础重查不占额外预算（单次 ≤${recheckText}，超出记未完成）`);
  const lookupText = fmt(llimits, ["cards", "units", "words", "chars"]);
  if (lookupText) parts.push(en ? `One lookup uses one shared budget slot (up to ${lookupText} per request; first limit reached applies)` : `一次补查占共享预算 1 格（单次 ≤${lookupText}，先到者为限）`);
  return parts.length ? parts.join(" · ") : "";
}
function qReasonList(rows, keyName) {
  // Reasons are shared by many ids; grouping keeps the panel readable.
  const groups = new Map();
  for (const row of rows || []) {
    const reason = String(row.reason || "");
    if (!groups.has(reason)) groups.set(reason, []);
    groups.get(reason).push(String(row[keyName] || ""));
  }
  return [...groups.entries()]
    .map(([reason, ids]) => `<li>${qe(reason)}：${qe(ids.slice(0, 40).join("、"))}${ids.length > 40 ? ` 等 ${ids.length} 个` : ""}</li>`)
    .join("");
}
function qReuseMarkup(plan) {
  const reuse = plan && plan.reuse;
  if (!reuse) return "";
  const kept = reuse.reused_units || [], work = reuse.work_units || [];
  const keptGroups = reuse.reused_groups || [], pendingGroups = reuse.pending_groups || [];
  const localReused = Number(reuse.local_units_reused || 0);
  const localPending = Number(reuse.local_units_pending || 0);
  const parts = [];
  if (kept.length) parts.push(`<details><summary>已复用 ${kept.length} 个单元（不再调用模型）</summary><ul>${qReasonList(kept, "unit_id")}</ul></details>`);
  if (work.length) parts.push(`<details><summary>需要处理 ${work.length} 个单元</summary><ul>${qReasonList(work, "unit_id")}</ul></details>`);
  // 大组的逐单元工作：已判的直接交回，未完成的这次继续——不能只说"沿用上次结论"。
  if (localReused || localPending) {
    const detail = `大组局部辨析：已判 ${localReused} 个单元（直接复用）`
      + (localPending ? ` · 还有 ${localPending} 个未完成（预算内继续判断）` : " · 已全部完成");
    parts.push(`<div class="tiny">${detail}</div>`);
  }
  if (keptGroups.length) parts.push(`<details><summary>沿用上次组辨析 ${keptGroups.length} 个组</summary><ul>${qReasonList(keptGroups, "group_id")}</ul></details>`);
  if (pendingGroups.length) parts.push(`<details><summary>需要重新辨析 ${pendingGroups.length} 个组</summary><ul>${qReasonList(pendingGroups, "group_id")}</ul></details>`);
  return parts.length ? `<strong>增量范围</strong>${parts.join("")}` : "";
}
function qStaleReferenceMarkup(rows) {
  const list = rows || [];
  if (!list.length) return "";
  const details = list.slice(0, 20).map(row => {
    const reasons = String(row.reason || "").split("；").filter(Boolean);
    return `<li><span data-i18n-ignore="true">${qe(row.unit_id)} · ${qe(row.card_id)}</span><span>：</span>${reasons.map(reason => `<span>${qe(reason)}</span>`).join('<span> · </span>')}</li>`;
  }).join("");
  return `<strong>有 ${list.length} 条旧自动参考留在已译单元里</strong>`
    + `<p class="tiny">只做提示：不会自动重写译文。需要更新时请重新翻译对应单元。</p>`
    + `<ul>${details}</ul>`
    + (list.length > 20 ? `<p class="tiny">只显示前 20 条，共 ${list.length} 条。</p>` : "");
}
function qPrepareProgressMarkup(c, running) {
  // Result counts, not a progress bar: failed work must never look completed.
  if (!c.plannedUnits && !c.finishedUnits && !c.failedUnits && !c.unexecutedUnits) return "";
  return `<section class="q-unit-outcome" data-role="progress"><div class="q-outcome-heading"><h3>${running?"已返回的单元结果":"单元处理结果"}</h3><span>本次范围 ${c.plannedUnits} 个单元</span></div><p class="q-outcome-counts"><span class="q-outcome-done">完成 <strong>${c.finishedUnits}</strong></span><span class="${c.failedUnits?"q-outcome-failed":""}">失败 <strong>${c.failedUnits}</strong></span>${c.unexecutedUnits?`<span>未执行 <strong>${c.unexecutedUnits}</strong></span>`:""}</p>${running?'<p class="tiny">任务仍在运行，以上是已返回的结果，不是整体完成比例。</p>':c.reusedUnits?`<p class="tiny">完成数量中包含 ${c.reusedUnits} 个复用单元。</p>`:""}</section>`;
}
function qPendingMarkup(c) {
  const entries=[];
  if(c.failedUnits)entries.push(["处理失败",`${c.failedUnits} 个单元`,"先查看下方错误记录；解决原因后，再预览需要重做的工作。"]);
  if(c.unexecutedUnits)entries.push(["尚未执行",`${c.unexecutedUnits} 个单元`,"再次预览，确认剩余范围后执行。"]);
  if(c.localUnitsPending)entries.push(["待局部辨析",`${c.localUnitsPending} 个单元`,"再次预览可按额度继续，已完成的判断会复用。"]);
  if(c.recheckPending)entries.push(["待重新检查",`${c.recheckPending} 张卡`,"再次预览后继续检查，已有有效结果保留。"]);
  if(c.lookupDeferred)entries.push(["补查受限",`${c.lookupDeferred} 张卡`,"达到输入上限等限制；再次确认不保证解除，详见统计中的预算口径。"]);
  if(c.budgetPending&&!c.localUnitsPending&&!c.recheckPending&&!c.lookupDeferred)entries.push(["有工作受额度限制","待继续","再次预览剩余工作；接口尚未给出完整分类。"]);
  return entries.length?`<section class="q-pending"><h3>需要处理</h3><p class="tiny">不影响使用仍有效的参考继续翻译。</p><ul>${entries.map(([label,count,hint])=>`<li><div><strong>${label}</strong><span>${count}</span></div><p>${hint}</p></li>`).join("")}</ul></section>`:"";
}
function qRetryStageLabel(row) {
  return String(row?.stage || "").toLowerCase() === "generation" ? "生成候选" : "独立检查";
}
function qRetryScopeLabel(row) {
  const units = Array.isArray(row?.units) ? row.units : [];
  if (!units.length) return "原批次范围未记录";
  const shown = units.slice(0, 6).map(qe).join("、");
  return `${shown}${units.length > 6 ? ` 等 ${units.length} 个单元` : `（${units.length} 个单元）`}`;
}
function qRetryRows() {
  return (qp.support?.retryable_batches || [])
    .filter(row => row && (row.stage || row.retryable))
    .slice()
    .sort((a,b) => Number(!!b.retryable) - Number(!!a.retryable) || String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
}
function qSyncRetrySelection(rows) {
  const eligible=new Set(rows.filter(row=>row.retryable).map(row=>String(row.batch_id)));
  qp.retrySelection=qp.retrySelectionTouched
    ? new Set([...qp.retrySelection].filter(id=>eligible.has(id)))
    : eligible;
}
function qBulkRetryResultMarkup() {
  const run=qp.retryRun;
  if (!run) return "";
  const counts={completed:0,failed:0,conflict:0,unknown:0,pending:0,running:0};
  for (const row of run.rows) counts[run.outcomes[row.batch_id]?.state||"pending"]++;
  const attempted=run.rows.length-counts.pending;
  const title=run.stopped?"已停止发送后续批次":run.active?"正在恢复批次":counts.failed?"已完成，部分批次仍失败":"所选批次已恢复";
  const next=run.stopped?`${run.stopped}；已发送的批次仍会完成，剩余批次未发送请求。请核对服务端状态后，再决定是否重新选择。`
    : run.active?`最多 ${run.parallel} 批同时恢复；页面会显示每批返回的结果。`
    : qp.referenceMode==="automatic"?"请再预览更新参考并确认；恢复批次不会直接采用参考。":"请在概念管理中继续处理候选。";
  const names={completed:"成功",failed:"失败",conflict:"已停止",unknown:"待核对",pending:"未执行",running:"进行中"};
  return `<section class="retry-bulk-result" aria-live="polite"><h3>本次结果</h3><strong>${qe(title)}</strong><p>${qe(next)}</p>`
    + `<div class="retry-bulk-meter" role="progressbar" aria-label="已尝试批次数" aria-valuemin="0" aria-valuemax="${run.rows.length}" aria-valuenow="${attempted}"><span style="width:${run.rows.length?attempted/run.rows.length*100:0}%"></span></div>`
    + `<p>已尝试 ${attempted} / ${run.rows.length} 批 · 成功 ${counts.completed} · 失败 ${counts.failed} · 冲突 ${counts.conflict} · 待核对 ${counts.unknown} · 未执行 ${counts.pending}</p>`
    + `<ul>${run.rows.map(row=>{const outcome=run.outcomes[row.batch_id]||{state:"pending"};return `<li><b class="${qe(outcome.state)}">${qe(names[outcome.state]||"未执行")}</b><span>${qe(qRetryStageLabel(row))} · ${qe(row.batch_id)}${outcome.error?` · ${qe(outcome.error)}`:""}</span></li>`;}).join("")}</ul></section>`;
}
function qRetryableBatchesMarkup() {
  const rows=qRetryRows();
  qSyncRetrySelection(rows);
  if (!rows.length && !qp.retryNotice && !qp.retryRun) return "";
  const available=rows.filter(row=>row.retryable), blocked=rows.filter(row=>!row.retryable);
  const chosen=available.filter(row=>qp.retrySelection.has(String(row.batch_id)));
  const allChecked=!!available.length&&chosen.length===available.length;
  const notice = qp.retryNotice
    ? `<p class="retry-batch-notice ${qp.retryBusy?"is-running":""}" role="status" data-role="retry-notice">${qe(qp.retryNotice)}</p>`
    : "";
  const body = available.map(row => {
    const stage = qRetryStageLabel(row);
    const actionCount = stage === "独立检查" ? Number(row.actionable_cards || 0) : Number(row.unit_count || (row.units || []).length || 0);
    const latest = String(row.last_error || row.blocked_reason || "未提供具体原因").trim();
    const state = String(row.state || "").toLowerCase();
    const stateLabel = state === "running" ? "上次运行中断" : "可重试";
    return `<article class="retry-batch-row is-retryable ${qp.retrySelection.has(String(row.batch_id))?"is-selected":""}" data-role="retry-batch" data-batch-id="${qe(row.batch_id)}">`
      + `<label class="retry-batch-choice"><input type="checkbox" data-retry-select="${qe(row.batch_id)}" aria-label="选择批次 ${qe(row.batch_id)}" ${qp.retrySelection.has(String(row.batch_id))?"checked":""}></label>`
      + `<div class="retry-batch-main"><div class="retry-batch-title"><strong>${qe(stage)}</strong><span class="retry-batch-state">${qe(stateLabel)}</span></div>`
      + `<p class="retry-batch-scope">${qRetryScopeLabel(row)}</p>`
      + `<p class="retry-batch-error" data-role="retry-latest-error">${qe(latest)}</p>`
      + `<p class="retry-batch-meta">手动重试 ${Number(row.attempt_count || 0)} 次 · 可处理 ${actionCount} ${stage === "独立检查" ? "张候选" : "个单元"}</p></div>`
      + `<div class="retry-batch-action"><button class="small retry-batch-action" data-retry-open="${qe(row.batch_id)}" data-retryable="1">只重试这一批</button></div>`
      + `<details class="retry-batch-raw" data-detail-key="retry-${qe(row.batch_id)}"><summary>查看原始错误</summary><pre>${qe(latest)}</pre></details>`
      + `</article>`;
  }).join("");
  return `<section id="retryBatchPanel" class="q-record-fold retry-batch-fold" aria-label="失败批次集中重试"><div class="retry-batch-heading"><div><h3>失败批次 <span>${available.length} 个可重试</span></h3><p class="tiny">一次选好要恢复的批次；检查失败的批次不会重新生成候选。</p></div><button class="primary small" data-retry-bulk-open ${chosen.length?"":"disabled"}>重试选中的 ${chosen.length} 批</button></div>`
    + `<div class="retry-batch-toolbar"><label><input type="checkbox" data-retry-select-all ${allChecked?"checked":""}>全选可重试批次</label><span>已选 ${chosen.length} 批</span><label class="retry-parallel-label">并行批次 <input id="retryParallel" type="number" min="1" step="1" inputmode="numeric" aria-label="集中重试并行批次" value="${qp.retryParallel}"></label><button class="text-button" data-retry-clear ${chosen.length?"":"disabled"}>清空选择</button></div>`
    + notice+qBulkRetryResultMarkup()+`<div class="retry-batch-list">${body}</div>`
    + (blocked.length?`<details class="retry-batch-blocked-list" data-detail-key="retry-blocked"><summary>当前不可重试 ${blocked.length} 批</summary>${blocked.map(row=>`<div><strong>${qe(qRetryStageLabel(row))} · ${qe(row.batch_id)}</strong><p>${qe(row.blocked_reason||"当前不可重试")}</p></div>`).join("")}</details>`:"")
    + `</section>`;
}
function qErrorGroupsMarkup(errors) {
  const groups=new Map();
  for(const item of errors||[]) {
    const text=typeof item==="string"?item:JSON.stringify(item);
    // Display grouping only. Every original record is retained verbatim.
    const label=/HTTP\s*5\d\d/i.test(text)?"服务端错误":/独立检查未完成|检查失败/.test(text)?"独立检查未完成":/timeout|超时/i.test(text)?"请求超时":"其他错误";
    if(!groups.has(label))groups.set(label,[]);groups.get(label).push(text);
  }
  if(!groups.size)return "";
  return `<details class="q-record-fold" data-detail-key="errors"><summary>错误记录 <span>${(errors||[]).length} 条</span></summary><p class="tiny">记录数不是失败单元数；一个批次可能包含多个单元。</p>${[...groups].map(([label,items],i)=>`<details class="q-error-group" data-detail-key="error-${i}"><summary>${label}<span>${items.length} 条</span></summary><ul>${items.map(text=>`<li>${qe(text)}</li>`).join("")}</ul></details>`).join("")}</details>`;
}
function qErrorRecordsMarkup(errors) {
  return [qRetryableBatchesMarkup(), qErrorGroupsMarkup(errors)].filter(Boolean).join("");
}
function qRetryParallelCount() {
  const value=Number(qp.retryParallel);
  return Number.isSafeInteger(value)&&value>0?value:1;
}
function qOpenBulkRetryDialog() {
  if (qp.busy || qp.blocked || qp.preparationBusy || qp.retryBusy || qPrepareIsActive()) return;
  const rows=qRetryRows().filter(row=>row.retryable&&qp.retrySelection.has(String(row.batch_id)));
  if (!rows.length) return;
  qp.pendingRetryBulk=rows;
  const generation=rows.filter(row=>row.stage==="generation").length;
  $("retryBulkSummary").textContent=`将重试 ${rows.length} 批，最多 ${Math.min(qRetryParallelCount(),rows.length)} 批并行：生成候选 ${generation} 批，独立检查 ${rows.length-generation} 批。`;
  $("retryBulkList").innerHTML=rows.map(row=>`<li><strong>${qe(qRetryStageLabel(row))} · ${qe(row.batch_id)}</strong><span>${qRetryScopeLabel(row)}</span></li>`).join("");
  $("retryBulkWarning").textContent="确认后会按所设并行数调用当前模型，可能产生多次请求和费用。明确返回失败时继续其余批次；身份冲突、资格变化或结果无法确认时停止发送后续批次，已发送的批次会等待结果，不自动补发。";
  $("retryBulkDialog").showModal();
  qControls();
}
function qCloseBulkRetryDialog() {
  if (qp.retryBusy) return;
  qp.pendingRetryBulk=null;
  const dialog=$("retryBulkDialog");
  if (dialog.open) dialog.close();
}
async function qRetrySelectedBatches() {
  const rows=qp.pendingRetryBulk;
  if (!rows?.length || qp.retryBusy || qp.busy || qp.preparationBusy || qp.blocked || qPrepareIsActive()) return;
  qp.pendingRetryBulk=null;
  $("retryBulkDialog").close();
  qp.retrySelectionTouched=true;
  const parallel=Math.min(rows.length,qRetryParallelCount());
  qp.retryRun={rows, parallel, outcomes:Object.fromEntries(rows.map(row=>[row.batch_id,{state:"pending"}])),active:true,stopped:""};
  qp.retryBusy=true;
  qSetBusy(true);
  qRenderPrepare();
  const active=new Set();
  let nextIndex=0;
  async function runOne(row, revision) {
    let result;
    try {
      result=await qRequest(`/api/project/quality-support/batches/${encodeURIComponent(row.batch_id)}/retry`, {
        expected_revision:revision,
        allow_parallel:parallel>1,
      }, "POST");
    } catch (error) {
      const conflict=error?.status===409;
      qp.retryRun.outcomes[row.batch_id]={state:conflict?"conflict":"unknown",error:error?.message||"请求结果无法确认"};
      qp.retryRun.stopped ||= conflict?"批次依据或项目身份已变化":"请求结果无法确认";
      qRenderPrepare();
      return;
    }
    const reason=String(result?.error||result?.check_error||"").trim();
    qp.retryRun.outcomes[row.batch_id]=String(result?.check_status||"").toLowerCase()==="completed"&&!reason
      ? {state:"completed"} : {state:"failed",error:reason||`服务端返回 ${result?.check_status||"未完成"}`};
    qRenderPrepare();
  }
  async function waitForAdmission(row, revision, task) {
    // The POST does not respond until the provider finishes. Its first locked
    // save records "running" and advances the revision. Observe that save
    // before submitting another request with a fresh revision; never bypass
    // the server's optimistic concurrency guard.
    for(let attempt=0;attempt<40&&!qp.retryRun.stopped;attempt++) {
      if(!active.has(task))return;
      const support=await qRequest("/api/project/quality-support");
      const current=(support.retryable_batches||[]).find(item=>String(item.batch_id||"")===String(row.batch_id));
      if(Number(support.revision)>revision&&current?.state==="running")return;
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    if(active.has(task)&&!qp.retryRun.stopped)qp.retryRun.stopped="尚未确认上一批已被服务端登记";
  }
  try {
    while(nextIndex<rows.length&&!qp.retryRun.stopped) {
      if(active.size>=parallel){await Promise.race(active);continue;}
      const row=rows[nextIndex];
      let support;
      try { support=await qRequest("/api/project/quality-support"); }
      catch(error){qp.retryRun.stopped=`无法核对下一批资格：${error.message}`;break;}
      if(qp.retryRun.stopped)break;
      const current=(support.retryable_batches||[]).find(item=>String(item.batch_id||"")===String(row.batch_id));
      if(!current?.retryable){qp.retryRun.stopped=`批次 ${row.batch_id} 的资格已变化，未发送本批及后续请求`;break;}
      const revision=Number(support.revision);
      qp.retryRun.outcomes[row.batch_id]={state:"running"};
      qRenderPrepare();
      const task=runOne(row,revision);
      active.add(task);
      task.then(()=>active.delete(task),()=>active.delete(task));
      nextIndex++;
      if(nextIndex<rows.length&&active.size<parallel){
        try { await waitForAdmission(row,revision,task); }
        catch(error){qp.retryRun.stopped=`无法核对已发送批次：${error.message}`;}
      }
    }
  } finally {
    await Promise.allSettled([...active]);
    try { await qRefreshSupport(); }
    catch(error){qp.retryRun.stopped ||= `结果已返回，但只读刷新失败：${error.message}`;}
    qp.retryRun.active=false;
    qp.retryBusy=false;
    qSetBusy(false);
    qRenderPrepare();
    const completed=Object.values(qp.retryRun.outcomes).filter(item=>item.state==="completed").length;
    qp.retryNotice=qp.retryRun.stopped
      ? `集中重试已停止：${qp.retryRun.stopped}。不会自动补发请求。`
      : qp.referenceMode==="automatic"
        ? `已恢复 ${completed} 批；参考尚待更新，请再次预览更新参考并确认。`
        : `已恢复 ${completed} 批；请在概念管理中继续处理候选。`;
    qRenderPrepare();
    qToast(qp.retryNotice);
  }
}
function qOpenRetryDialog(row) {
  if (!row || !row.retryable || qp.busy || qp.preparationBusy || qp.retryBusy) return;
  qp.retryNotice = "";
  qp.pendingRetry = row;
  const checkOnly = String(row.stage || "").toLowerCase() === "check";
  const actionCount = checkOnly ? Number(row.actionable_cards || 0) : Number(row.unit_count || (row.units || []).length || 0);
  $("retryBatchTitle").textContent = `确认重试${qRetryStageLabel(row)}批次`;
  $("retryBatchMessage").textContent = checkOnly
    ? "将只检查这批已经保存的候选，不重新生成候选。"
    : "将重新生成这批候选，并对新候选进行独立检查。";
  $("retryBatchScope").innerHTML = `<div><dt>原批次范围</dt><dd>${qRetryScopeLabel(row)}</dd></div><div><dt>实际可处理</dt><dd>${actionCount} ${checkOnly ? "张候选" : "个单元"}</dd></div><div><dt>最近错误</dt><dd>${qe(row.last_error || row.blocked_reason || "未提供具体原因")}</dd></div>`;
  $("retryBatchWarning").textContent = "确认后会调用当前配置的模型，可能产生费用；取消不会写入或调用模型。";
  $("retryBatchConfirm").textContent = checkOnly ? "确认只检查这一批" : "确认重新生成并检查";
  $("retryBatchResult").textContent = "";
  $("retryBatchDialog").showModal();
  qControls();
}
function qCloseRetryDialog() {
  qp.pendingRetry = null;
  const dialog = $("retryBatchDialog");
  if (dialog?.open || dialog?.opened) dialog.close();
}
async function qRetryRefreshOnly(message) {
  try { await qRefreshSupport(); }
  catch (refreshError) { qToast(`${message}；只读刷新失败：${refreshError.message}`); return false; }
  return true;
}
async function qRetryBatch() {
  const row = qp.pendingRetry;
  if (!row || !row.retryable || qp.retryBusy || qp.busy || qp.preparationBusy) return;
  qp.retryBusy = true;
  qSetBusy(true);
  qp.retryNotice = `批次 ${row.batch_id} 正在恢复；确认窗口已收起，可以继续查看页面。`;
  $("retryBatchDialog").close();
  qp.pendingRetry = null;
  qRenderPrepare();
  try {
    const result = await qRequest(`/api/project/quality-support/batches/${encodeURIComponent(row.batch_id)}/retry`, {
      expected_revision: Number(qp.support?.revision || 0),
    }, "POST");
    const checkStatus = String(result?.check_status || "").toLowerCase();
    const concreteError = String(result?.error || result?.check_error || "").trim();
    if (checkStatus === "completed" && !concreteError) {
      const stageText = qRetryStageLabel(row);
      const automatic = qp.referenceMode === "automatic";
      qp.retryNotice = automatic
        ? `本批已恢复（${stageText}）；参考尚待更新。请再次预览更新参考并确认，不会自动采用、辨析、补查或翻译。`
        : `本批已恢复（${stageText}）；请在概念管理中继续处理候选。`;
      $("retryBatchDialog").close();
      qp.pendingRetry = null;
      await qRefreshSupport();
      qToast(qp.retryNotice);
    } else {
      const reason = concreteError || `服务端返回 ${result?.check_status || "未完成"}`;
      $("retryBatchResult").textContent = `本次重试未完成：${reason}。如需再次尝试，请重新打开确认。`;
      await qRetryRefreshOnly("重试未完成");
      $("retryBatchDialog").close();
      qp.pendingRetry = null;
      qToast(`重试未完成：${reason}`);
    }
  } catch (error) {
    const message = error?.message || "未知错误";
    if (error?.status === 409) {
      $("retryBatchDialog").close();
      qp.pendingRetry = null;
      await qRetryRefreshOnly(`重试未执行：${message}`);
      qToast(`重试未执行：${message}`);
    } else if (error && error.status === undefined) {
      // The request outcome is unknown. A GET is safe; sending the POST again
      // would risk a duplicate provider call.
      $("retryBatchDialog").close();
      qp.pendingRetry = null;
      await qRetryRefreshOnly("重试结果待确认，已只读刷新；不会自动补发请求");
      qToast("重试结果待确认，已只读刷新；不会自动补发请求。请查看批次状态后再决定。 ");
    } else {
      $("retryBatchDialog").close();
      qp.pendingRetry = null;
      await qRetryRefreshOnly(`重试请求失败：${message}`);
      qToast(`重试请求失败：${message}`);
    }
  } finally {
    qp.retryBusy = false;
    qSetBusy(false);
    qRenderPrepare();
  }
}
function qReplaceDetails(element,markup) {
  const opened=new Set([...element.querySelectorAll('details[open][data-detail-key]')].map(el=>el.dataset.detailKey));
  element.innerHTML=markup;
  element.querySelectorAll('details[data-detail-key]').forEach(el=>{el.open=opened.has(el.dataset.detailKey);});
}
let qProgressTimer = null;
let qStatusInFlight = null;
let qStatusController = null;
let qStatusEpoch = 0;
let qStatusFailures = 0;
function qStopProgressPoll() {
  if (qProgressTimer) { clearTimeout(qProgressTimer); qProgressTimer = null; }
}
function qPrepareIsActive() {
  return qp.prepareProgress?.active === true || qp.prepareProgress?.status === "running";
}
function qShouldObservePrepare() {
  return qPrepareIsActive() || qp.prepareOutcomeUnknown
    || (qp.preparationBusy && qp.preparePhase === "execute")
    || Boolean(qp.observedPrepareId && !qp.prepareProgress);
}
function qIsRestartPersistedTerminalSnapshot(payload) {
  const status = String(payload?.status || "");
  const prepareId = String(payload?.prepare_id || "").trim();
  return Boolean(prepareId)
    && payload?.progress_revision === null
    && ["complete", "partial", "failed", "stale", "interrupted"].includes(status)
    && payload?.persisted_status === status
    && payload?.active === false;
}
function qCanAcceptRestartPersistedTerminal(payload, previous) {
  if (!qIsRestartPersistedTerminalSnapshot(payload)) return false;
  if (!previous) return true;
  // qMergePrepareStatus passes `previous` only after matching the same task ID.
  // Once the manager has reopened, the persisted terminal status is authoritative
  // even when the old page still held a numeric in-process revision.
  return String(previous.prepare_id || "") === String(payload.prepare_id || "");
}
function qIsTerminalPrepareSnapshot(payload) {
  return Boolean(payload && payload.active === false
    && ["complete", "partial", "failed", "stale", "interrupted"].includes(payload.status));
}
function qPrepareStatusPath() {
  const query = new URLSearchParams({expected_project_id: qp.runtimeId});
  if (qp.observedPrepareId) query.set("prepare_id", qp.observedPrepareId);
  return `/api/project/quality-support/prepare/status?${query}`;
}
function qInvalidatePrepareStatusReads() {
  qStatusEpoch++;
  qStopProgressPoll();
  if (qStatusController) qStatusController.abort();
  qStatusController = null;
  qStatusInFlight = null;
}
function qMergePrepareStatus(payload) {
  if (!payload || String(payload.project_id || "") !== String(qp.runtimeId || "")) return false;
  const incomingId = String(payload.prepare_id || "") || null;
  if (qp.observedPrepareId && incomingId !== qp.observedPrepareId) return false;
  if (!qp.observedPrepareId && incomingId) qp.observedPrepareId = incomingId;
  const previous = qp.prepareProgress;
  const samePreviousTask = previous && String(previous.prepare_id || "") === String(incomingId || "");
  if (payload.progress_revision === null
      && !qCanAcceptRestartPersistedTerminal(payload, samePreviousTask ? previous : null)) return false;
  if (samePreviousTask) {
    const incomingIsRunning = payload.active === true || payload.status === "running";
    if (qIsTerminalPrepareSnapshot(previous) && incomingIsRunning) return false;
    const incomingRevision = payload.progress_revision;
    const previousRevision = previous.progress_revision;
    if (typeof incomingRevision === "number" && Number.isFinite(incomingRevision)
        && typeof previousRevision === "number" && Number.isFinite(previousRevision)
        && incomingRevision <= previousRevision) return false;
  }
  const postOutcomeWasUnknown=qp.prepareOutcomeUnknown;
  qp.prepareProgress = payload;
  if (payload.prepare && typeof payload.prepare === "object") {
    qp.prepare = payload.prepare;
    if (payload.status) qp.prepare.status = payload.status;
  }
  qp.prepareLastSyncedAt = new Date().toISOString();
  qp.prepareOutcomeUnknown = false;
  qp.connectionIssue = postOutcomeWasUnknown&&payload.active===true
    ? "执行连接中断；服务器确认任务仍在运行，最终结果待确认。" : "";
  qStatusFailures = 0;
  if (typeof qRenderPrepare === "function") qRenderPrepare();
  if (typeof qControls === "function") qControls();
  if (incomingId && payload.active !== true && qp.prepareTerminalRefreshedId !== incomingId) {
    qp.prepareTerminalRefreshedId = incomingId;
    // Refresh results once at terminal state. qRefreshSupport captures the
    // open card draft before rendering it again; routine progress never calls it.
    qRefreshSupport().catch(error => {
      qp.connectionIssue = `任务结果已确认，但概念数据刷新失败：${error.message}`;
      qRenderPrepare();
    });
  }
  if (qShouldObservePrepare()) qSchedulePreparePoll(2000);
  else qStopProgressPoll();
  return true;
}
function qStatusRetryDelay() {
  return Math.min(12000, 2000 * (2 ** Math.min(qStatusFailures, 3)));
}
function qSchedulePreparePoll(delay=2000) {
  if (qProgressTimer || !qShouldObservePrepare()) return;
  qProgressTimer = setTimeout(() => {
    qProgressTimer = null;
    qPollPrepareStatus();
  }, delay);
}
async function qPollPrepareStatus(options={}) {
  if (!qp.runtimeId || (!qp.observedPrepareId && !options.allowUnbound)) return null;
  if (qStatusInFlight) return qStatusInFlight;
  const epoch = qStatusEpoch;
  const requestedId = qp.observedPrepareId;
  const controller = new AbortController();
  qStatusController = controller;
  const timeout = setTimeout(() => controller.abort(), 8000);
  const request = (async () => {
    try {
      const payload = await qRequest(qPrepareStatusPath(), undefined, undefined, {signal: controller.signal});
      if (epoch !== qStatusEpoch || requestedId !== qp.observedPrepareId) return null;
      const accepted = qMergePrepareStatus(payload);
      if (!accepted) {
        const sameIdentity=String(payload?.project_id||"")===String(qp.runtimeId||"")
          && String(payload?.prepare_id||"")===String(qp.observedPrepareId||"");
        const current=qp.prepareProgress;
        const sameCurrentTask=current
          && String(current.prepare_id||"")===String(payload?.prepare_id||"");
        const incomingRevision=payload?.progress_revision;
        const previousRevision=current?.progress_revision;
        const staleRevision=sameIdentity&&sameCurrentTask
          && typeof incomingRevision==="number"&&Number.isFinite(incomingRevision)
          && typeof previousRevision==="number"&&Number.isFinite(previousRevision)
          && incomingRevision<=previousRevision;
        const staleTerminalRegression=sameIdentity&&sameCurrentTask
          && qIsTerminalPrepareSnapshot(current)
          && (payload?.active===true||payload?.status==="running");
        if(staleRevision||staleTerminalRegression){
          // The live read succeeded for the same frozen identity. Keep the
          // newest business snapshot, but still record a successful sync and
          // clear a connection warning (notably after offline recovery).
          qp.prepareLastSyncedAt=new Date().toISOString();
          qp.connectionIssue="";
          qp.prepareOutcomeUnknown=false;
          qStatusFailures=0;
          qRenderPrepare();
          if(qShouldObservePrepare())qSchedulePreparePoll(2000);
          else qStopProgressPoll();
          return null;
        }
        if(sameIdentity&&sameCurrentTask&&incomingRevision===null
            && !qCanAcceptRestartPersistedTerminal(payload,current)){
          qp.connectionIssue="服务器返回了无法核实版本的状态，未覆盖当前任务；稍后继续只读确认。";
          if(qShouldObservePrepare())qSchedulePreparePoll(qStatusRetryDelay());
          else qStopProgressPoll();
          qRenderPrepare();
          return null;
        }
        qp.connectionIssue = "收到的任务状态与本页绑定的项目或任务不一致，已忽略。";
        qRenderPrepare();
        qStopProgressPoll();
        return null;
      }
      return payload;
    } catch (error) {
      if (epoch !== qStatusEpoch) return null;
      qStatusFailures++;
      if (error.status === 409 && qp.preparationBusy && qp.preparePhase === "execute") {
        // The confirmed task may not appear in the status record until its
        // execute POST reaches the guarded start. Keep observing the same ID.
        qp.connectionIssue = "服务器正在登记已确认的任务；继续只读核对同一任务。";
        qSchedulePreparePoll(1000);
      } else if (error.status === 409) {
        qp.prepareOutcomeUnknown = false;
        qp.connectionIssue = `任务已被替换或当前项目已变化：${error.message}`;
        qStopProgressPoll();
      } else {
        qp.connectionIssue = qp.prepareOutcomeUnknown
          ? "连接中断，任务结果待确认；只读状态查询会继续，不会重复执行。"
          : "进度连接暂时中断；任务状态尚待同步，正在稍后重试只读查询。";
        if (qShouldObservePrepare()) qSchedulePreparePoll(qStatusRetryDelay());
      }
      qRenderPrepare();
      return null;
    } finally {
      clearTimeout(timeout);
      if (qStatusInFlight === request) qStatusInFlight = null;
      if (qStatusController === controller) qStatusController = null;
    }
  })();
  qStatusInFlight = request;
  return request;
}
function qStartProgressPoll(delay=0) {
  qSchedulePreparePoll(delay);
}
function qRenderPrepare() {
  const chip = $("referenceModeChip");
  if (!chip) return;
  const automatic = qp.referenceMode === "automatic";
  chip.textContent = automatic ? "自动模式" : "人工模式";
  chip.className = "mode-chip" + (automatic ? "" : " manual");
  const note = $("workflowModeNote");
  if (note) note.textContent = automatic ? "自动模式" : "人工模式";
  $("prepareIntro").textContent = automatic
    ? "确认后自动提取候选、检查并辨析；合格概念直接用于翻译参考，已完成的单元会复用。"
    : "人工模式：只生成候选，逐卡批准后才进入参考。";
  // The scope the next preview would freeze: the selection narrows it, and
  // saying so here keeps the two panels from looking unrelated.
  const scopeLine = $("prepareScope");
  if (scopeLine) {
    const selectedCount = qScopeUnits().length;
    const total = (qp.units || []).filter(u => String(u.source || "").trim()).length;
    scopeLine.textContent = selectedCount
      ? `范围：${qScopeAll()?"整个项目":"自选"} ${selectedCount} 个单元`
      : "范围：自选为空，不能预览";
  }
  $("prepareButton").hidden = !automatic;
  $("prepareSwitchButton").hidden = true;
  $("prepareEnableButton").hidden = true;
  if (!automatic) {
    $("prepareConfirmButton").hidden = true;
  }
  const preview = $("preparePreview");
  if (qp.preparePlan) {
    const plan = qp.preparePlan;
    const batches = (plan.batches || []).length;
    const groups = (plan.groups || []).length;
    const reuse = plan.reuse || {};
    const workUnits = Number(reuse.work_unit_count || 0);
    const localPending = Number(reuse.local_units_pending || 0);
    // The plan reads as a small card: headline, four counters, then the notes.
    const stats = [
      `<div class="plan-stat"><strong>${plan.scope.length}</strong><span>个单元</span></div>`,
      `<div class="plan-stat"><strong>${batches ? workUnits : 0}</strong><span>需要生成与检查</span></div>`,
      `<div class="plan-stat"><strong>${batches}</strong><span>个批次</span></div>`,
      `<div class="plan-stat"><strong>${Number(plan.max_parallel_batches || 1)}</strong><span>并行批数</span></div>`,
      `<div class="plan-stat"><strong>${groups}</strong><span>相关表达组</span></div>`,
    ].join("");
    preview.innerHTML = `<div class="plan-card">`
      + `<div class="plan-card-head"><span class="plan-flag">✓</span><strong>计划已就绪</strong></div>`
      + `<div class="plan-stats">${stats}</div>`
      + (batches ? "" : `<p class="plan-note">本次不需要生成与检查（全部复用）。</p>`)
      + (localPending ? `<p class="plan-note">大组还有 ${localPending} 个单元的局部辨析未完成（预算内继续）</p>` : "")
      + `<p class="tiny">预览不调用模型；确认后才执行。</p>`
      + `</div>`;
  } else {
    preview.innerHTML = "";
  }
  const reuseBox = $("prepareReuse");
  if (reuseBox) reuseBox.innerHTML = qReuseMarkup(qp.preparePlan);
  $("prepareConfirmButton").hidden = !automatic || !qp.preparePlan || qp.preparationBusy;
  const c = qPrepareCounts();
  const rows = [];
  if (qp.prepare) {
    // The bar replaces the old "单元：计划 …" line: same numbers, one glance.
    const progress = qPrepareProgressMarkup(c, qp.preparationBusy || c.status === "running");
    if (progress) rows.push(progress);
    rows.push(`<div class="summary-row"><span>有效概念（已采用）</span><strong data-role="adopted">${c.adopted}</strong></div>`);
    rows.push(`<div class="summary-row"><span>跳过候选（含不合格 ${c.ineligible} · 人工保护 ${c.protected}）</span><strong data-role="skipped">${c.skipped}</strong></div>`);
    rows.push(`<div class="summary-row"><span>未采用组</span><strong data-role="unresolved">${c.unresolvedGroups + c.unresolved}</strong></div>`);
    if (c.reusedUnits || c.processedUnits || c.reusedGroups || c.judgedGroups) {
      rows.push(`<div class="summary-row unit-row" data-role="incremental"><span>增量：复用单元 ${c.reusedUnits} · 本次处理 ${c.processedUnits} · 沿用辨析 ${c.reusedGroups} · 本次辨析 ${c.judgedGroups}</span></div>`);
    }
    // 覆盖：分母是本次范围内的单元，不是卡数；未覆盖与"没有候选"是两件事。
    if (c.eligibleUnits) {
      rows.push(`<div class="summary-row unit-row" data-role="coverage"><span>单元覆盖：已覆盖 ${c.coveredUnits} / 可覆盖 ${c.eligibleUnits}${c.uncoveredUnits ? ` · 未覆盖 ${c.uncoveredUnits}` : ""}${c.noCandidateUnits ? ` · 无候选单元 ${c.noCandidateUnits}` : ""}</span></div>`);
    }
    // 问题分项：AI 答掉的、仍开放的，都不能与采用数混成一个总数。
    if (c.aiResolvedCards || c.resolvedQuestions || c.remainingQuestions) {
      const parts = [`AI 已解决疑问：${c.resolvedQuestions} 项（涉及 ${c.aiResolvedCards} 张卡）`];
      parts.push(`剩余未解决：${c.remainingQuestions} 项`);
      rows.push(`<div class="summary-row unit-row" data-role="questions"><span>${parts.join(" · ")}</span></div>`);
    }
    // A2/A3 额外动作：重查、补查、局部辨析各自可见；失败与无候选不混用。
    if (c.refreshedChecks || c.recheckPending || c.lookupRounds || c.lookupSettled || c.lookupMisses || c.localGroups || c.localUnitsReused || c.localUnitsOversized || c.oversizedGroups) {
      const parts = [];
      if (c.refreshedChecks || c.recheckPending) {
        parts.push(`旧检查重查 ${c.refreshedChecks}${c.recheckPending ? ` · 仍待重查 ${c.recheckPending}` : ""}`);
      }
      if (c.lookupRounds || c.lookupMisses || c.lookupSettled || c.lookupFailed || c.lookupRejected) {
        const outcomes = [];
        if (c.lookupRefreshed) outcomes.push(`写入 ${c.lookupRefreshed} 张`);
        if (c.lookupRejected) outcomes.push(`写入被拒 ${c.lookupRejected} 张`);
        if (c.lookupFailed) outcomes.push(`请求失败 ${c.lookupFailed} 张`);
        parts.push(
          `有界补查 ${c.lookupRounds} 次（命中 ${c.lookupHits}，无命中卡 ${c.lookupMisses}`
          + (outcomes.length ? `；${outcomes.join(" · ")}` : "")
          + "）"
        );
      }
      // 同一内容与依据的补查已经执行过：不是本次请求，也不重复消耗预算。
      if (c.lookupSettled) parts.push(`同内容同依据已查过 ${c.lookupSettled} 张卡（不重复调用）`);
      if (c.localGroups || c.localUnitsReused) {
        parts.push(`大组局部辨析 ${c.localGroups} 组 / 本次判断 ${c.localUnitsJudged} 个单元${c.localUnitsReused ? ` · 复用已判 ${c.localUnitsReused} 个` : ""}`);
      }
      if (c.localUnitsOversized) parts.push(`争用集合超限单元 ${c.localUnitsOversized}`);
      if (c.oversizedGroups) parts.push(`整组超限 ${c.oversizedGroups}`);
      rows.push(`<div class="summary-row unit-row" data-role="extra-work"><span>额外动作：${parts.join(" · ")}</span></div>`);
    }
    if (c.budget && c.budget.limit > 0) {
      // One shared pool: bounded lookup and large-group local judgments draw
      // from the same number, never one budget each.
      const exhausted = c.budget.used >= c.budget.limit;
      // 预算用尽本身不是问题：只有确实还有未完成的可选步骤时才提示留待下次确认。
      const pending = Math.max(
        c.budgetPending,
        c.localUnitsPending + c.recheckPending + c.lookupDeferred
      );
      const unfinished = pending > 0 || c.status === "partial" || c.status === "failed";
      const hint = (exhausted && unfinished) ? `（已用尽；仍有工作未完成，留待下次确认）` : "";
      rows.push(`<div class="summary-row unit-row" data-role="budget"><span>额外请求预算：${c.budget.used} / ${c.budget.limit} 次${hint}</span></div>`);
    }
    // 预算口径：基础重查与一次补查分别说明，避免把两种上限当成一个数字。
    // 说明较长且只在核对上限时有用，默认折起来，避免把面板正文挤满。
    const basisText = qBudgetBasisText(c.basis);
    if (basisText) {
      rows.push(`<details class="summary-details"><summary>预算口径</summary><div class="summary-row unit-row" data-role="budget-basis"><span>预算口径：${qe(basisText)}</span></div></details>`);
    }
    // 预算未完成 / 输入超限的工作：各自单独一行，不并进"未采用"，也不伪装成已完成。
    if (c.budgetPending || c.localUnitsPending || c.recheckPending || c.lookupDeferred) {
      const parts = [
        `单元局部辨析 ${c.localUnitsPending}`,
        `旧检查重查 ${c.recheckPending}`,
      ];
      if (c.lookupDeferred) parts.push(`超范围补查 ${c.lookupDeferred} 张卡`);
      rows.push(`<div class="summary-row unit-row" data-role="unfinished"><span>预算未完成：${parts.join(" · ")}（可继续翻译；再次确认会从这些未完成项继续，已完成的不会重做）</span></div>`);
    }
    if (c.failedUnits) {
      rows.push(`<div class="summary-row unit-row" data-role="failures"><span>失败单元：${c.failedUnits}（失败的批次不参与自动采用）</span></div>`);
    }
    if (c.reasons.length) {
      rows.push(`<div class="summary-row unit-row" data-role="reasons"><span>未采用原因：${c.reasons.map(row => `${qe(row.reason)}（${Number(row.count) || 0}）`).join("；")}</span></div>`);
    }
  }
  qReplaceDetails($("prepareSummary"), qp.prepare ? (rows[0] && rows[0].includes('data-role="progress"') ? rows.shift() : "") + qPendingMarkup(c) + `<details class="q-record-fold" data-detail-key="statistics"><summary>详细统计与未采用原因</summary>${rows.join("")}</details>` : "");
  const status = $("prepareStatus");
  if (qp.preparePlan && !qp.preparationBusy) {
    status.textContent = "计划已就绪。";
  } else if (!qp.prepare) {
    status.textContent = automatic ? "尚未准备：先预览计划。" : "";
  } else if (qp.preparationBusy) {
    status.textContent = "正在准备…";
  } else if (c.status === "partial") {
    const errs = (qp.prepare.errors || []).length;
    const unfinished = Math.max(
      c.budgetPending,
      c.localUnitsPending + c.recheckPending + c.lookupDeferred
    );
    const followUp = unfinished
      ? "仍有工作未完成，请查看下方分类；再次预览可继续符合条件的工作。"
      : "可以先使用已完成的参考开始翻译，已完成的单元下次直接复用。";
    const failures = c.failedUnits ? `，${c.failedUnits} 个单元的批次失败` : "";
    status.textContent = `部分完成：${c.adopted} 张可用参考${failures}${errs ? `（${errs} 条错误）` : ""}。${followUp}`;
  } else if (c.status === "complete") {
    // The note is a sentence of its own: a trailing full stop would collide with
    // the one that closes this line.
    const reuse = (c.reuseNote || "").replace(/。\s*$/, "");
    status.textContent = c.adopted
      ? `准备完成：采用 ${c.adopted} 张${reuse ? `（${reuse}）` : ""}，可直接开始翻译。`
      : "准备完成：本次没有合格候选，成功 0 张也是有效结果。";
  } else if (c.status === "failed") {
    const errs = (qp.prepare.errors || []).length;
    status.textContent = `准备失败：${c.failedUnits} 个单元未完成${errs ? `（${errs} 条错误）` : ""}。已有参考是否有效，请查看概念管理。`;
  } else if (c.status === "interrupted") {
    status.textContent = "上次准备已中断：已完成的单元会直接复用，重新预览只重做未完成或已失效的部分。";
  } else if (c.status === "stale") {
    status.textContent = "上次准备已失效：本次没有采用任何概念，人工改动保留，重新预览即可。";
  } else {
    status.textContent = `准备状态：${c.status}。`;
  }
  const req = c.requests || {}, providerCalls=qp.prepareProgress?.provider_calls;
  const repairRounds=Number.isFinite(req.repair_rounds)?req.repair_rounds:null;
  if (qp.prepare || qp.prepareProgress) {
    $("prepareNotice").hidden = false;
    const requestLine=(qp.prepare&&[req.generation,req.check,req.resolution].some(Number.isFinite))
      ?`记录请求：生成 ${req.generation} · 检查 ${req.check} · 辨析 ${req.resolution}（不同口径，不相加）`:"记录请求：待确定";
    const callLine=providerCalls&&typeof providerCalls==="object"
      ?`Provider 调用：生成 ${Number.isFinite(providerCalls.generation)?providerCalls.generation:"待确定"} · 检查 ${Number.isFinite(providerCalls.check)?providerCalls.check:"待确定"} · 辨析 ${Number.isFinite(providerCalls.resolution)?providerCalls.resolution:"待确定"}（不是 HTTP 次数）`
      :"Provider 调用：待确定（状态接口未返回）";
    const httpLine=Number.isFinite(qp.prepareProgress?.provider_http)
      ?`Provider HTTP：${qp.prepareProgress.provider_http}`:"Provider HTTP：不可用（接口未聚合真实 HTTP 次数）";
    const repairLine=repairRounds===null?"内容修正轮：待确定":"内容修正轮："+repairRounds;
    $("prepareNotice").textContent=[requestLine,callLine,httpLine,repairLine].join("\n");
  } else {
    $("prepareNotice").hidden = true;
  }
  // Units that were translated with an automatic reference that is no longer
  // live. Purely informational: nothing is rewritten, no snapshot is touched.
  const staleBox = $("prepareStale");
  if (staleBox) {
    const stale = qp.support?.stale_reference_units || [];
    staleBox.hidden = !stale.length;
    staleBox.innerHTML = qStaleReferenceMarkup(stale);
  }
  qRenderWorkflow(); qControls();
}
function qAskModeSwitch(mode) {
  // An in-page dialog instead of window.confirm: native dialogs can be blocked
  // or auto-dismissed by the embedding browser, which silently turned the mode
  // switch into a no-op with no explanation. This dialog is always visible and
  // always reports the outcome.
  const dialog = $("modeDialog");
  qp.pendingMode = mode;
  $("modeMessage").textContent =
    mode === "manual"
      ? "切回人工模式后，新的翻译请求不再使用自动采用的参考；已经跑过的单元保持它们当时冻结的参考，已采用的自动结果也不会被删除，随时可以切回自动模式恢复。"
      : "切换为自动模式后，页面会出现「解析并准备参考」：一次确认即可让通过校验的合格概念直接用于翻译参考。打开页面或切换模式本身都不会调用模型，也不会自动开始翻译。";
  $("modeConfirm").textContent = mode === "manual" ? "切回人工模式" : "切换为自动模式";
  dialog.showModal();
}
async function qSetReferenceMode(mode) {
  if (qp.busy || qp.blocked || qp.preparationBusy || qPrepareIsActive()) return;
  qp.pendingMode = null;
  qSetBusy(true);
  try {
    const result = await qRequest("/api/project/quality-support/reference-mode", {
      reference_mode: mode,
      expected_project_id: qp.runtimeId,
      expected_revision: Number(qp.support?.revision || 0),
    }, "PUT");
    await qRefreshSupport();
    qRenderPrepare();
    qToast(mode === "automatic" ? "已切换为自动模式：请点击「解析并准备参考」。" : "已切回人工模式：新请求只使用人工批准的参考。");
  } catch (error) {
    // Never fail silently: the switch always reports why it did not happen.
    const hint = error.status === 409
      ? "（页面数据可能已过期，请先刷新概念数据再试）"
      : error.status === 400 || error.status === 422
        ? "（只能切换当前打开项目的参考模式）"
        : "";
    qToast(`切换模式失败：${error.message}${hint}`);
    if (error.status === 409) { try { await qRefreshSupport(); qRenderPrepare(); } catch (refreshError) { /* the toast explains it */ } }
  } finally { qSetBusy(false); }
}
async function qPreparePlan() {
  if (qp.busy || qp.blocked || qp.preparationBusy || qPrepareIsActive()) return;
  const unitIds = qScopeUnits().map(u=>u.id);
  if (!unitIds.length) { qToast("请先选择要准备的单元。"); return; }
  let parallel, words; try { parallel = qInteger("maxParallel", 1); words=qInteger("maxWords",100); } catch (error) { qToast(error.message); return; }
  qp.preparationBusy = true; qp.preparePhase="preview"; qRenderPrepare();
  try {
    const result = await qRequest("/api/project/quality-support/prepare", {
      phase: "plan",
      unit_ids: unitIds,
      max_source_words: words,
      // The batch settings are shared with the manual scan; the preview freezes
      // this worker count so the confirmed run executes what was previewed.
      max_parallel_batches: parallel,
      expected_project_id: qp.runtimeId,
      expected_revision: Number(qp.support?.revision || 0),
    });
    // The preview writes nothing, so the revision it bound is still live; the
    // frozen plan id is what the confirmed execute will continue.
    qp.preparePlan = {...result.plan, reuse: result.preview?.reuse || null};
    qRenderPrepare();
    const reuse = result.preview?.reuse || {};
    qToast(`计划已就绪：需处理 ${Number(reuse.work_unit_count || 0)} 个、复用 ${Number(reuse.reused_unit_count || 0)} 个。`);
  } catch (error) {
    qToast(`准备计划失败：${error.message}`);
    if (error.status === 409) { try { await qRefreshSupport(); } catch (refreshError) { /* the toast explains it */ } }
  } finally { qp.preparationBusy = false; qp.preparePhase=null; qRenderPrepare(); qFinishLeave(); }
}
async function qPrepareRun() {
  // The confirmed step is one server-side phase: it freezes the single active
  // prepare, runs generation + check + group judgment and commits the verified
  // decisions. Preview stays read-only, this is the only model-calling action.
  if (qp.busy || qp.blocked || qp.preparationBusy || qPrepareIsActive() || !qp.preparePlan) return;
  const frozenPlan = qp.preparePlan;
  const planId = frozenPlan.prepare_id;
  qp.observedPrepareId = planId;
  qp.prepareProgress = null;
  qp.prepareOutcomeUnknown = false;
  qp.prepareTerminalRefreshedId = null;
  qp.preparationBusy = true; qp.preparePhase="execute";
  qp.connectionIssue="正在提交确认请求；页面会通过只读状态查询观察同一任务。";
  qp.prepare = null; qp.preparePlan = null; qRenderPrepare();
  try {
    const post = qRequest("/api/project/quality-support/prepare", {
      phase: "execute",
      prepare_id: planId,
      // The confirmed run re-sends the frozen preview scope: what was previewed
      // is exactly what executes, and expected_revision rejects any write that
      // happened in between.
      unit_ids: [...(frozenPlan.scope || [])],
      max_source_words: Number(frozenPlan.max_source_words || 4000),
      max_parallel_batches: Number(frozenPlan.max_parallel_batches || 1),
      expected_project_id: qp.runtimeId,
      expected_revision: Number(qp.support?.revision || 0),
    });
    qStartProgressPoll(350);
    const result = await post;
    // Its summary has no progress_revision, so reconcile through the
    // identity-bound status endpoint rather than overwrite newer observations.
    qInvalidatePrepareStatusReads();
    qp.prepare = result.summary || qp.prepare;
    qp.connectionIssue = "执行请求已返回，正在核对服务器最终状态。";
    qRenderPrepare();
    const observed = await qPollPrepareStatus({manual:true});
    const finalStatus=observed||qp.prepareProgress;
    if (finalStatus&&String(finalStatus.prepare_id||"")===String(planId)&&!finalStatus.active) {
      const c = qPrepareCounts();
      const reused = c.reusedUnits ? `（复用 ${c.reusedUnits} 个单元，本次处理 ${c.processedUnits} 个）` : "";
      if (c.status === "partial" || c.status === "failed") {
        qToast(`准备${c.status === "partial" ? "部分完成" : "失败"}：采用 ${c.adopted} 张，失败单元 ${c.failedUnits} 个；可以使用已完成的参考开始翻译。`);
      } else if (c.status === "complete") {
        qToast(`准备完成：采用 ${c.adopted} 张${reused}，跳过 ${c.skipped} 张；实际注入仍以单元冻结快照为准。`);
      } else {
        qToast(`准备状态：${c.status || "待核对"}。请查看流程详情。`);
      }
    }
  } catch (error) {
    qp.prepareOutcomeUnknown = error.status === undefined;
    qp.connectionIssue = error.status === undefined
      ? "连接中断，任务结果待确认；正在只读查询，不会重发执行请求。"
      : `执行请求返回 ${error.status}：${error.message}；正在只读核对任务状态。`;
    qRenderPrepare();
    const observed = await qPollPrepareStatus({manual:true});
    if (observed && String(observed.prepare_id||"")===String(planId) && observed.active!==true) {
      const c=qPrepareCounts();
      if(c.status==="partial"||c.status==="failed")qToast(`准备${c.status==="partial"?"部分完成":"失败"}：采用 ${c.adopted} 张，失败单元 ${c.failedUnits} 个；可以使用已完成的参考开始翻译。`);
      else if(c.status==="complete")qToast(`准备完成：采用 ${c.adopted} 张；实际注入仍以单元冻结快照为准。`);
      else qToast(`准备状态：${c.status||"待核对"}。请查看流程详情。`);
    } else if (error.status === 409 && !observed) qToast(`准备请求冲突：${error.message}。已保留任务身份，未重新提交。`);
    else if (observed?.active===true) qToast("请求已返回错误，但服务器确认任务仍在运行；页面会继续只读观察。");
    else if (!observed) qToast("连接中断，任务结果待确认；请稍后刷新状态，不要重复执行。");
  } finally { qp.preparationBusy = false; qp.preparePhase=null; qRenderPrepare(); qFinishLeave(); }
}

function qScopeAll() { return qp.scopeMode === "all"; }
function qScopeUnits() { return qp.units.filter(u=>String(u.source||"").trim()&&(qScopeAll()||qp.selected.has(u.id))); }
function qReviewLayout() { $("reviewView").classList.toggle("q-show-detail",qp.reviewDetail); }
function qPrepareStageMarkup(stageKey,label,stage={}) {
  const units={generation:"批次",check:"批次",recheck:"张卡",lookup:"次请求",group_resolution:"组",local_resolution:"个单元",commit:"次提交"};
  const states={unknown:"待确定",pending:"待开始",running:"处理中",partial:"部分完成",failed:"失败",reused:"已复用",complete:"完成",not_required:"无需执行"};
  const state=String(stage.state||"unknown"),unit=units[stageKey]||String(stage.unit||"项");
  const n=value=>Number.isFinite(value)&&value>=0?Math.floor(value):null;
  const total=n(stage.total),done=n(stage.completed),running=n(stage.running),failed=n(stage.failed),reused=n(stage.reused);
  const extra=[];
  if(running>0)extra.push("处理中 "+running+" "+unit);
  if(failed>0)extra.push("失败 "+failed+" "+unit);
  if(reused>0)extra.push("复用 "+reused+" "+unit);
  if(n(stage.not_required)>0)extra.push("无需执行 "+n(stage.not_required)+" "+unit);
  if(n(stage.pending)>0)extra.push("待处理 "+n(stage.pending)+" "+unit);
  let count=total===null?"总量待确定":(done===null?"已完成待确定":done)+" / "+total+" "+unit;
  if(total===0&&state==="not_required")count="无需执行";
  return '<article class="q-stage-card" data-stage="'+qe(stageKey)+'" data-status="'+qe(state)+'"><header><h3>'+qe(label)+'</h3><span class="q-step-state">'+qe(states[state]||"待确定")+'</span></header><p class="q-stage-count">'+qe(count)+'</p>'+(extra.length?'<p class="q-stage-extra">'+qe(extra.join(" · "))+'</p>':"")+'</article>';
}
function qPrepareActiveMarkup(progress) {
  const names={generation:"生成候选",check:"独立检查",recheck:"旧检查重查",lookup:"核实疑问",group_resolution:"相关概念辨析",local_resolution:"大组局部辨析",commit:"提交参考"};
  const active=Array.isArray(progress?.active_items)?progress.active_items:[],errors=Array.isArray(progress?.errors)?progress.errors:[];
  const status=String(progress?.status||qp.prepare?.status||((qp.preparationBusy&&qp.preparePhase==="execute")?"running":""));
  const rows=active.slice(0,8).map(item=>{
    const label=String(item.label||item.id||"执行中"),name=names[String(item.stage||"")]||"准备任务";
    const round=Number.isFinite(item.attempt_round)&&Number.isFinite(item.max_rounds)?'<span class="q-activity-round">修正轮 '+Math.floor(item.attempt_round)+' / '+Math.floor(item.max_rounds)+'</span>':"";
    return '<li><span><strong>'+qe(name)+'</strong> · '+qe(label)+'</span>'+round+'</li>';
  });
  const recent=errors.slice(-4).map(error=>'<li class="q-activity-error">'+qe(error)+'</li>');
  if(!rows.length&&!recent.length)return '<p class="q-activity-empty">'+(status==="running"?"执行中，等待阶段结果。":"当前没有可显示的活动项或错误。")+'</p>';
  return (rows.length?'<ul class="q-activity-list">'+rows.join("")+'</ul>':"")+(recent.length?'<details class="q-live-errors"><summary>最近错误 · '+recent.length+'</summary><ul>'+recent.join("")+'</ul></details>':"");
}
function qTimeText(value) {
  if(!value)return window.Mode2I18n?.locale==="en"?"Not available":"待确定";
  const date=new Date(value);
  const locale=window.Mode2I18n?.locale==="en"?"en-US":undefined;
  return Number.isNaN(date.getTime())?String(value):date.toLocaleTimeString(locale, {hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function qPrepareBusinessStatusLabel(status) {
  return ({running:"正在执行",partial:"部分完成",complete:"准备完成",failed:"准备失败",
    stale:"结果已失效",interrupted:"准备已中断"})[String(status||"")]||"待确定";
}
function qRenderWorkflow() {
  if(!$("prepareFlow"))return;
  const c=qPrepareCounts(),automatic=qp.referenceMode==="automatic",units=qScopeUnits(),has=!!qp.prepare;
  const progress=qp.prepareProgress,statusValue=String(progress?.status||qp.prepare?.status||""),running=qPrepareIsActive();
  const title=qp.preparePhase==="preview"?"正在预览计划":qp.preparationBusy&&!running?"执行请求等待服务器响应":statusValue?qPrepareBusinessStatusLabel(statusValue):(qp.preparePlan?"等待确认":"尚未准备");
  $("scopeSummary").textContent=qScopeAll()?"整个项目 · "+units.length+" 个单元":"自选单元 · "+units.length+" 个单元";
  $("scopeWords").textContent="约 "+units.reduce((n,u)=>n+Number(u.source_words||0),0).toLocaleString()+" 英文词 · 每批 "+$("maxWords").value+" 词 · 并行 "+$("maxParallel").value+" 批";
  $("scopeAll").checked=qScopeAll();$("scopeCustom").checked=!qScopeAll();
  $("flowTitle").textContent=automatic?"自动准备流程":"人工参考流程";
  $("flowState").setAttribute("data-status",running?"running":statusValue);
  $("flowState").textContent=automatic?title:qp.scanning?"正在扫描":qp.plan?"计划已就绪":"人工复检";
  $("prepareHeadline").textContent=automatic?title:"扫描后人工批准";
  const online=typeof navigator==="undefined"||navigator.onLine!==false;
  $("prepareConnectionLabel").textContent=qp.connectionIssue?"状态同步受阻":!online?"当前离线":running?"已连接 · 后端任务运行中":"已连接 · 任务状态可同步";
  $("prepareConnection").setAttribute("data-status",qp.connectionIssue||!online?"warning":running?"running":"ready");
  $("prepareStatusRefresh").disabled=qp.busy||qp.blocked||!qp.observedPrepareId;
  $("flowConnection").hidden=!qp.connectionIssue;$("flowConnection").textContent=qp.connectionIssue;
  const planned=Number.isFinite(qp.prepare?.planned_units)?qp.prepare.planned_units:null;
  $("prepareTaskScope").textContent=qp.observedPrepareId?"任务范围："+(planned===null?"待确定":planned+" 个单元")+" · "+qp.observedPrepareId:"当前尚无已确认的准备任务。";
  $("prepareSyncMeta").textContent="最近同步 "+qTimeText(qp.prepareLastSyncedAt)+" · 后端最近进展 "+qTimeText(progress?.updated_at);
  if(automatic){
    const stages=progress?.stages||{},unknown={state:"unknown",unit:"item",completed:null,total:null,running:null,failed:null,reused:null,not_required:null,pending:null};
    const generation=qPrepareStageMarkup("generation","生成候选",stages.generation||unknown);
    const check=qPrepareStageMarkup("check","独立检查",stages.check||unknown);
    const rest=[qPrepareStageMarkup("recheck","旧检查重查",stages.recheck||unknown),qPrepareStageMarkup("lookup","核实疑问",stages.lookup||unknown),qPrepareStageMarkup("group_resolution","相关概念辨析",stages.group_resolution||unknown),qPrepareStageMarkup("local_resolution","大组局部辨析",stages.local_resolution||unknown),qPrepareStageMarkup("commit","提交参考",stages.commit||unknown)].join("");
    const scope=qp.observedPrepareId?'<p class="q-progress-scope">本任务范围：'+qe(planned===null?"单元数待确定":planned+" 个单元")+'</p>':"";
    $("prepareFlow").innerHTML=scope+'<section class="q-stage-group"><h3>候选与检查 <span>可并行</span></h3><div class="q-stage-parallel">'+generation+check+'</div></section><section class="q-stage-group"><h3>按需处理与提交</h3><div class="q-stage-list">'+rest+'</div></section><section class="q-live-activity"><h3>当前活动与问题</h3><div>'+qPrepareActiveMarkup(progress)+'</div></section>';
  }else{
    const rows=[["生成候选与独立检查",qp.scanning?"处理中":"等待扫描","预览计划后确认扫描；已发请求与后续批次分开处理。"],["人工复检与批准","人工操作","在概念管理中逐条或批量核对解释、证据与检查意见。"],["前往翻译工作台","随时可前往","人工模式只使用人工批准的参考；没有参考的单元仍可翻译。"]];
    $("prepareFlow").innerHTML=rows.map((row,index)=>'<section class="q-flow-step"><span class="q-step-number">'+(index+1)+'</span><div class="q-step-content"><header><h3>'+qe(row[0])+'</h3><span class="q-step-state">'+qe(row[1])+'</span></header><p>'+qe(row[2])+'</p></div></section>').join("");
  }
  $("prepareOverview").innerHTML=automatic&&has&&!qp.preparePlan?'<dl class="q-result-list"><div><dt>采用参考</dt><dd>'+c.adopted+' 张</dd></div><div><dt>本次可覆盖单元</dt><dd>'+c.coveredUnits+' 个</dd></div><div><dt>待重查 / 待补查</dt><dd>'+c.recheckPending+' / '+c.lookupDeferred+' 张</dd></div><div><dt>待局部辨析</dt><dd>'+c.localUnitsPending+' 个单元</dd></div><div><dt>失败单元</dt><dd>'+c.failedUnits+' 个</dd></div></dl>':"";
  $("prepareButton").textContent=has?"再次预览剩余工作":"预览准备计划";
  $("prepareButton").hidden=!automatic||!!qp.preparePlan;
  $("prepareEditorLink").href=$("editorLink").href;
  const retryableCount=qRetryRows().filter(row=>row.retryable).length;
  $("retryShortcut").hidden=!retryableCount;
  $("retryShortcut").textContent=`${retryableCount} 个失败批次可重试 · 查看并选择`;
  qReplaceDetails($("prepareErrors"),qErrorRecordsMarkup(qp.prepare?.errors||[]));
  qControls();
}
function qBind() {
  $("modeButton").onclick=()=>qAskModeSwitch(qp.referenceMode==="automatic"?"manual":"automatic");
  for(const [open,dialog,close,done] of [["openScope","scopeDialog","closeScope","doneScope"],["openSettings","settingsDialog","closeSettings","doneSettings"]]) {
    $(open).onclick=()=>$(dialog).showModal();$(close).onclick=()=>$(dialog).close();$(done).onclick=()=>$(dialog).close();
  }
  for(const [id,mode] of [["scopeAll","all"],["scopeCustom","custom"]])$(id).onchange=()=>{if(qp.busy||qp.preparationBusy)return;qp.scopeMode=mode;qInvalidate();qRenderUnits();};
  $("backToCards").onclick=()=>{qCaptureDraft();qp.reviewDetail=false;qReviewLayout();};

  $("refreshButton").onclick=()=>qLoad();
  $("prepareStatusRefresh").onclick=()=>qPollPrepareStatus({manual:true});
  $("prepareErrors").addEventListener("change",e=>{if(e.target.id!=="retryParallel")return;const value=Number(e.target.value);qp.retryParallel=Number.isSafeInteger(value)&&value>0?value:1;e.target.value=String(qp.retryParallel);});
  $("retryShortcut").onclick=()=>{const target=$("retryBatchPanel");if(!target)return;$("executionDetails").open=true;target.scrollIntoView({block:"start",behavior:"smooth"});};
  for(const id of ["search","chapter","scanState","selectedOnly"])$(id).addEventListener(id==="search"?"input":"change",qRenderUnits);
  for(const id of ["maxParallel","maxWords"])$(id).addEventListener("input",qInvalidate);
  $("unitList").onchange=e=>{const id=e.target.dataset.unit;if(!id)return;const checked=e.target.checked;qChangeSelection(()=>checked?qp.selected.add(id):qp.selected.delete(id));const replacement=[...$("unitList").querySelectorAll("[data-unit]")].find(el=>el.dataset.unit===id);replacement?.focus({preventScroll:true});};
  $("unitList").onclick=e=>{const target=e.target.closest("[data-source]");if(target)qOpenSource(target.dataset.source);};
  $("selectVisible").onchange=e=>{const units=qFiltered(),checked=e.target.checked;qChangeSelection(()=>units.filter(u=>String(u.source||"").trim()).forEach(u=>checked?qp.selected.add(u.id):qp.selected.delete(u.id)));};
  $("clearSelection").onclick=()=>qChangeSelection(()=>qp.selected.clear());
  $("selectNext").onclick=()=>{try{const n=qInteger("nextCount",1,Math.max(1,qp.units.length)),covered=qCovered(),items=qFiltered().filter(u=>String(u.source||"").trim()&&!covered.has(u.id)&&!qp.selected.has(u.id)).slice(0,n);qChangeSelection(()=>items.forEach(u=>qp.selected.add(u.id)));qToast(`已加入 ${items.length} 个单元${items.length<n?"，当前筛选下可选数量不足":""}。`);}catch(error){qToast(error.message);}};
  for(const [id,add] of [["addRange",true],["removeRange",false]])$(id).onclick=()=>{try{const ids=qParseRanges($("rangeInput").value,qp.units),before=qp.selected.size;qChangeSelection(()=>ids.forEach(unit=>add?qp.selected.add(unit):qp.selected.delete(unit)));qToast(`${add?"加入":"移出"} ${Math.abs(qp.selected.size-before)} 个单元。`);}catch(error){qToast(error.message);}};
  $("rangeInput").onkeydown=e=>{if(e.key==="Enter")$("addRange").click();};
  $("planButton").onclick=qMakePlan;$("startButton").onclick=qRunPlan;$("terminateButton").onclick=qTerminate;
  $("prepareButton").onclick=qPreparePlan;$("prepareConfirmButton").onclick=qPrepareRun;
  $("prepareSwitchButton").onclick=()=>qAskModeSwitch("manual");
  $("prepareEnableButton").onclick=()=>qAskModeSwitch("automatic");
  $("modeConfirm").onclick=()=>{const mode=qp.pendingMode;qp.pendingMode=null;$("modeDialog").close();if(mode)qSetReferenceMode(mode);};
  $("modeCancel").onclick=()=>{qp.pendingMode=null;$("modeDialog").close();};
  $("closeMode").onclick=()=>{qp.pendingMode=null;$("modeDialog").close();};
  $("scanTab").onclick=()=>qSwitchView("scan");$("reviewTab").onclick=()=>qSwitchView("review");
  $("cardList").onclick=e=>{const b=e.target.closest("[data-card]");if(b){qCaptureDraft();qp.activeCard=b.dataset.card;qp.reviewDetail=true;qRenderCards();qReviewLayout();}};
  $("cardList").onchange=e=>{
    const box=e.target.closest("[data-card-select]"); if(!box)return;
    const accepted=qBatchToggle(box.dataset.cardSelect,box.checked);
    if(!accepted)box.checked=false;
    qRenderBatch();
  };
  $("reviewState").onchange=()=>{const had=qp.batch.size>0;qBatchClear(had?qFilterNotice():"");qp.group="";qWriteReset();qRenderCards();};
  $("conceptSearch").oninput=()=>{const had=qp.batch.size>0;qBatchClear(had?qFilterNotice():"");qRenderCards();};
  $("writeOnly").onchange=()=>{
    // Pure display filter: it never reads or writes card data. Turning it on
    // drops the current group selection so a hidden group is not left active.
    const had=qp.batch.size>0;qp.group="";qWriteReset();qBatchClear(had?qFilterNotice():"");qRenderCards();
    qToast(qWriteOnlyOn()?"只显示书写写法不同的重复候选；分组不代表语义等价。":"已显示全部相关表达分组。");
  };
  $("batchClear").onclick=()=>qBatchClear("已清空批量选择。");
  for(const [id,action] of [["batchApprove","approve"],["batchDefer","defer"],["batchReject","reject"]])
    $(id).onclick=()=>{const cards=qSelectedCards();if(!cards.length)return;qOpenBatchDialog(action,cards);};
   $("batchConfirm").onclick=qBatchConfirm;
   $("closeBatch").onclick=()=>{$("batchDialog").close();qp.pendingBatch=null;};
   $("batchCancel").onclick=()=>{$("batchDialog").close();qp.pendingBatch=null;};
   $("prepareErrors").onclick=e=>{
     if(e.target.closest("[data-retry-bulk-open]")){qOpenBulkRetryDialog();return;}
     if(e.target.closest("[data-retry-clear]")){
       qp.retrySelectionTouched=true;qp.retrySelection.clear();qRenderPrepare();return;
     }
     const button=e.target.closest("[data-retry-open]");
     if(!button)return;
     const row=(qp.support?.retryable_batches||[]).find(item=>String(item.batch_id||"")===String(button.dataset.retryOpen||""));
     qOpenRetryDialog(row);
   };
   $("prepareErrors").onchange=e=>{
     const one=e.target.closest("[data-retry-select]");
     const all=e.target.closest("[data-retry-select-all]");
     if((!one&&!all)||qp.busy||qp.preparationBusy||qp.retryBusy)return;
     qp.retrySelectionTouched=true;
     if(all) qp.retrySelection=all.checked?new Set(qRetryRows().filter(row=>row.retryable).map(row=>String(row.batch_id))):new Set();
     else if(one.checked)qp.retrySelection.add(one.dataset.retrySelect);
     else qp.retrySelection.delete(one.dataset.retrySelect);
     qRenderPrepare();
   };
   $("retryBatchConfirm").onclick=qRetryBatch;
   $("retryBatchCancel").onclick=qCloseRetryDialog;
   $("closeRetryBatch").onclick=qCloseRetryDialog;
   $("retryBulkConfirm").onclick=qRetrySelectedBatches;
   $("retryBulkCancel").onclick=qCloseBulkRetryDialog;
   $("closeRetryBulk").onclick=qCloseBulkRetryDialog;
   $("retryBulkDialog").addEventListener("cancel",()=>{qp.pendingRetryBulk=null;});
   $("groupList").onclick=e=>{const button=e.target.closest("[data-group]");if(!button)return;
    const had=qp.batch.size>0;qp.group=qp.group===button.dataset.group?"":button.dataset.group;
    qWriteReset();qBatchClear(had?qFilterNotice():"");qRenderCards();
    qToast(qp.group?"已按表达分组筛选；组内卡片只是表达可能相关。":"已取消分组筛选。");};
  $("cardDetail").onchange=e=>{
    // The writing-duplicate selection is explicit and starts empty; nothing is
    // pre-checked, approved or written by ticking a box.
    const defer=e.target.closest("[data-write-defer]");
    if(defer){
      const id=defer.dataset.writeDefer;
      if(defer.checked&&qp.write.defers.size>=qBatchLimit&&!qp.write.defers.has(id)){defer.checked=false;qToast(`每次最多选择 ${qBatchLimit} 张，请先取消其他选择。`);return;}
      if(defer.checked)qp.write.defers.add(id);else qp.write.defers.delete(id);
      qUpdateWriteBar();return;
    }
    const keep=e.target.closest("[data-write-keep]");
    if(keep){qp.write.keep=keep.checked?keep.dataset.writeKeep:null;qUpdateWriteBar();}
  };
  $("cardDetail").onclick=e=>{
    const action=e.target.closest("[data-card-action]"),source=e.target.closest("[data-source]"),
      compare=e.target.closest("[data-compare]"),group=e.target.closest("[data-group]"),
      write=e.target.closest("[data-write-confirm]");
    if(action)qCardAction(action.dataset.cardAction);
    if(source)qOpenSource(source.dataset.source);
    if(compare){qCaptureDraft();qp.activeCard=compare.dataset.compare;qp.reviewDetail=true;qRenderCards();qReviewLayout();}
    if(group){const had=qp.batch.size>0;qp.group="";qWriteReset();qBatchClear(had?qFilterNotice():"");qRenderCards();}
    if(write)qWriteConfirm();
  };
  $("closeSource").onclick=()=>$("sourceDialog").close();$("sourceSelect").onclick=()=>{qChangeSelection(()=>qp.selected.has(qp.sourceId)?qp.selected.delete(qp.sourceId):qp.selected.add(qp.sourceId));$("sourceDialog").close();};
  document.addEventListener("click",e=>{const a=e.target.closest("a[href]");if(!a||(!qp.busy&&!qp.drafts.size))return;e.preventDefault();qCaptureDraft();qp.leaveUrl=a.href;$("leaveMessage").textContent=qp.busy?"当前请求尚未结束。可以停止后续批次并等待已发请求结束后离开。未保存的概念编辑也会丢弃。":"有未保存的概念编辑，离开将丢弃这些修改；已确认的准备任务会在服务器继续执行，可稍后回来查看。";$("leaveButton").hidden=false;$("leaveButton").textContent=qp.busy?"停止后离开":"丢弃编辑并离开";$("leaveDialog").showModal();});
  $("stayButton").onclick=()=>$("leaveDialog").close();$("leaveButton").onclick=()=>{qp.stop=true;qp.pendingLeave=qp.leaveUrl;$("leaveDialog").close();qControls();qFinishLeave();};
  window.addEventListener("beforeunload",e=>{qCaptureDraft();if(qp.busy||qp.drafts.size){e.preventDefault();e.returnValue="";}});
  window.addEventListener("online",()=>{qStatusFailures=0;if(qp.observedPrepareId){qStopProgressPoll();qPollPrepareStatus({manual:true});}});
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible"&&qp.observedPrepareId){qStopProgressPoll();qPollPrepareStatus({manual:true});}});
}
function qInit() { qBind(); qLoad(true); }
document.addEventListener("DOMContentLoaded",qInit);
window.addEventListener("mode2:localechange",()=>{
  const sync=$("prepareSyncMeta");
  if(sync)sync.textContent="最近同步 "+qTimeText(qp.prepareLastSyncedAt)+" · 后端最近进展 "+qTimeText(qp.prepareProgress?.updated_at);
  if(qp.prepare || qp.preparePlan)qRenderPrepare();
});
