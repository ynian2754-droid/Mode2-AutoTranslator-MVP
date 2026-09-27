const ui = {
  project: null,
  selectedUnitId: null,
  selectedUnitIds: new Set(),
  filter: "all",
  pollTimer: null,
  toastTimer: null,
  outputStatus: null,
  outputFormat: "markdown",
  exporting: false,
  inFlightUnitIds: new Set(),
  editDrafts: new Map(),
  pendingProjectRender: false,
  detailRenderKey: null,
  pointerInteraction: false,
  stopRequested: false,
  concurrencyDirty: false,
  concurrencySaving: false,
  glyphRepairBusy: false,
  glyphRepairProgress: null,
  draftProjectId: null,
  unitActionDialogResolver: null,
};

const $ = (id) => document.getElementById(id);
const EDITABLE_STATUSES = new Set(["needs_action", "passed", "user_modified", "accepted_risk"]);
const PROCESSING_STATUSES = new Set(["pending", "waiting_translation", "translating", "waiting_review", "reviewing"]);
const DRAFT_STORAGE_KEY = "mode2.translationDraft.v1";

function fitTranslationEditorToContent(textarea) {
  // The initial `rows` value is only a browser fallback.  The editor itself
  // must show its entire persisted translation without an inner scrollbar.
  textarea.style.height = "auto";
  // Keep one pixel of headroom for browser sub-pixel rounding at non-100%
  // zoom, so a long translation cannot retain a one-pixel inner overflow.
  textarea.style.height = `${Math.ceil(textarea.scrollHeight) + 1}px`;
}

function projectIdOf(project = ui.project) {
  return String(project?.current_project?.id || project?.project?.id || "");
}

function readDraftRecords() {
  try {
    if (!window.localStorage) return [];
    const raw = window.localStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    const records = Array.isArray(parsed)
      ? parsed
      : parsed && parsed.schema_version === 1 && Array.isArray(parsed.drafts)
        ? parsed.drafts
        : [];
    return records.filter((record) => record && record.schema_version === 1
      && typeof record.project_id === "string"
      && typeof record.unit_id === "string"
      && typeof record.source_sha256 === "string"
      && Number.isInteger(record.base_translation_revision)
      && typeof record.base_translation === "string"
      && typeof record.content === "string");
  } catch (_error) {
    return [];
  }
}

function writeDraftRecords(records) {
  try {
    if (!window.localStorage) return;
    window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({ schema_version: 1, drafts: records }));
  } catch (_error) {
    // Private browsing, a disabled storage area, or quota exhaustion should
    // not break the editor. The in-memory draft remains available this turn.
  }
}

function draftRecordFor(projectId, unitId) {
  return readDraftRecords().find((record) => record.project_id === projectId && record.unit_id === unitId) || null;
}

function syncDraftsForProject(project) {
  const projectId = projectIdOf(project);
  const units = Array.isArray(project?.units) ? project.units : [];
  const unitMap = new Map(units.map((unit) => [unit.id, unit]));
  const previousProjectId = ui.draftProjectId;
  const previousDrafts = ui.editDrafts;
  const allRecords = readDraftRecords();
  const currentRecords = [];
  const retainedRecords = [];
  const nextDrafts = new Map();
  allRecords.forEach((record) => {
    if (record.project_id !== projectId) {
      retainedRecords.push(record);
      return;
    }
    const unit = unitMap.get(record.unit_id);
    if (!unit || record.source_sha256 !== String(unit.source_sha256 || "")) return;
    currentRecords.push(record);
    nextDrafts.set(record.unit_id, {
      value: record.content,
      baseValue: record.base_translation,
      baseRevision: record.base_translation_revision,
      sourceSha256: record.source_sha256,
      dirty: record.content !== record.base_translation,
      updatedAt: record.updated_at,
    });
  });
  // Keep a dirty in-memory draft for this same project when storage is
  // temporarily unavailable (private mode, quota exhaustion, or a malformed
  // value).  Never carry it across projects, even if Unit IDs happen to
  // collide.
  if (previousProjectId === projectId) {
    units.forEach((unit) => {
      const existing = previousDrafts.get(unit.id);
      if (
        existing?.dirty
        && existing.sourceSha256 === String(unit.source_sha256 || "")
        && !nextDrafts.has(unit.id)
      ) {
        nextDrafts.set(unit.id, existing);
      }
    });
  }
  ui.editDrafts = nextDrafts;
  ui.draftProjectId = projectId;
  const nextRecords = retainedRecords.concat(currentRecords);
  if (JSON.stringify(nextRecords) !== JSON.stringify(allRecords)) writeDraftRecords(nextRecords);
}

function draftForUnit(unit) {
  if (!unit) return null;
  const existing = ui.editDrafts.get(unit.id);
  if (existing && existing.sourceSha256 === String(unit.source_sha256 || "")) return existing;
  const projectId = projectIdOf();
  const record = draftRecordFor(projectId, unit.id);
  if (record && record.source_sha256 === String(unit.source_sha256 || "")) {
    const draft = {
      value: record.content,
      baseValue: record.base_translation,
      baseRevision: record.base_translation_revision,
      sourceSha256: record.source_sha256,
      dirty: record.content !== record.base_translation,
      updatedAt: record.updated_at,
    };
    ui.editDrafts.set(unit.id, draft);
    return draft;
  }
  return null;
}

function ensureDraftForUnit(unit) {
  const existing = draftForUnit(unit);
  if (existing) {
    const serverRevision = Number.isInteger(unit.translation_revision) ? unit.translation_revision : 0;
    const serverTranslation = String(unit.translation || "");
    if (!existing.dirty && (existing.baseRevision !== serverRevision || existing.baseValue !== serverTranslation)) {
      const refreshed = {
        ...existing,
        value: serverTranslation,
        baseValue: serverTranslation,
        baseRevision: serverRevision,
        dirty: false,
        updatedAt: new Date().toISOString(),
      };
      ui.editDrafts.set(unit.id, refreshed);
      return refreshed;
    }
    return existing;
  }
  const baseValue = String(unit.translation || "");
  const draft = {
    value: baseValue,
    baseValue,
    baseRevision: Number.isInteger(unit.translation_revision) ? unit.translation_revision : 0,
    sourceSha256: String(unit.source_sha256 || ""),
    dirty: false,
    updatedAt: new Date().toISOString(),
  };
  ui.editDrafts.set(unit.id, draft);
  return draft;
}

function persistDraft(unit, value) {
  if (!unit) return;
  const current = ensureDraftForUnit(unit);
  const content = String(value ?? "");
  const draft = {
    ...current,
    value: content,
    dirty: content !== current.baseValue,
    updatedAt: new Date().toISOString(),
  };
  ui.editDrafts.set(unit.id, draft);
  const projectId = projectIdOf();
  const records = readDraftRecords().filter((record) => !(record.project_id === projectId && record.unit_id === unit.id));
  if (draft.dirty) {
    records.push({
      schema_version: 1,
      project_id: projectId,
      unit_id: unit.id,
      source_sha256: draft.sourceSha256,
      base_translation_revision: draft.baseRevision,
      base_translation: draft.baseValue,
      content: draft.value,
      updated_at: draft.updatedAt,
    });
  }
  writeDraftRecords(records);
  updateDraftIndicators();
}

function removeDraft(unit) {
  if (!unit) return;
  const projectId = projectIdOf();
  ui.editDrafts.delete(unit.id);
  writeDraftRecords(readDraftRecords().filter((record) => !(record.project_id === projectId && record.unit_id === unit.id)));
  updateDraftIndicators();
}

function isDraftDirty(unit) {
  const draft = draftForUnit(unit);
  return Boolean(draft?.dirty && draft.value !== draft.baseValue);
}

function draftCount(project = ui.project) {
  const projectId = projectIdOf(project);
  return (Array.isArray(project?.units) ? project.units : []).reduce(
    (count, unit) => count + (isDraftDirty(unit) && projectId === projectIdOf(project) ? 1 : 0),
    0,
  );
}

function updateDraftIndicators() {
  const count = draftCount();
  const status = $("draftStatus");
  if (status) {
    status.hidden = count === 0;
    setElementText(status, count ? `未保存：${count} 个单元` : "");
    status.classList.toggle("has-drafts", count > 0);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusText(status) {
  return {
    pending: "待翻译",
    waiting_translation: "等待中",
    waiting_review: "等待中",
    translating: "翻译中",
    reviewing: "校验中",
    passed: "已通过",
    user_modified: "用户已修改",
    needs_action: "待裁决",
    accepted_risk: "接受风险",
    cancelled: "已停止",
  }[status] || status;
}

function statusClass(status) {
  return String(status || "pending").replaceAll("_", "_");
}

// Bounded model-repair rounds are reported per work kind.  A valid FAIL is a
// completed review, not a failed call, so it is shown as 完成：不通过.
function modelRepairRoundText(entry) {
  const round = Number(entry?.round) || 0;
  const maxRounds = Number(entry?.max_rounds) || 0;
  if (!round) return "";
  return maxRounds ? `第${round}/${maxRounds}轮` : `第${round}轮`;
}

function modelRepairItemMarkup(kind, entry, unit) {
  if (!entry || typeof entry !== "object") return "";
  const status = String(entry.status || "");
  const label = kind === "translation" ? "翻译" : "校验";
  const round = modelRepairRoundText(entry);
  let text;
  if (status === "running" || status === "repairing") {
    text = Number(entry.round) > 1 ? `${label}修正中 ${round}` : `${label}中 ${round}`;
  } else if (status === "succeeded") {
    const successRound = Number(entry.success_round) || 1;
    if (kind === "review") {
      const verdict = unit?.review && typeof unit.review === "object" ? unit.review.verdict : null;
      text = `${successRound > 1 ? `校验第${successRound}轮完成` : "校验一次完成"}：${verdict === "FAIL" ? "不通过" : "通过"}`;
    } else {
      text = successRound > 1 ? `翻译第${successRound}轮通过` : "翻译一次通过";
    }
  } else if (status === "cancelled") {
    text = `${label}已取消`;
  } else if (status === "failed") {
    text = `${label}修正失败（已尝试 ${round || "1"}）`;
  } else {
    return "";
  }
  const errors = Array.isArray(entry.errors) ? entry.errors : [];
  const last = errors.length ? errors[errors.length - 1] : null;
  const detail = last && last.detail ? `<span class="model-repair-detail">${escapeHtml(last.detail)}</span>` : "";
  return `<div class="model-repair-item ${escapeHtml(status)}" data-repair-kind="${kind}"><strong>${escapeHtml(text)}</strong>${detail}</div>`;
}

function modelRepairMarkup(unit) {
  const repair = unit?.model_repair;
  if (!repair || typeof repair !== "object") return "";
  const items = [
    modelRepairItemMarkup("translation", repair.translation, unit),
    modelRepairItemMarkup("review", repair.review, unit),
  ].filter(Boolean).join("");
  return items ? `<div class="model-repair" role="status" aria-live="polite">${items}</div>` : "";
}

function formatTime(value) {
  if (!value) return "--:--:--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--:--:--" : date.toLocaleTimeString([], { hour12: false });
}

function hasActiveTextSelection() {
  const selection = window.getSelection?.();
  if (selection && selection.rangeCount && !selection.isCollapsed) return true;
  const active = document.activeElement;
  if (!active || !("selectionStart" in active) || !("selectionEnd" in active)) return false;
  return active.selectionStart !== active.selectionEnd;
}

function hasProtectedEditor() {
  const textarea = $("editedTranslation");
  if (!textarea || !textarea.dataset.unitId) return false;
  if (ui.selectedUnitId !== textarea.dataset.unitId) return false;
  const draft = ui.editDrafts.get(textarea.dataset.unitId);
  return document.activeElement === textarea || Boolean(draft?.dirty);
}

function shouldDeferProjectRender() {
  return ui.pointerInteraction || hasActiveTextSelection() || hasProtectedEditor();
}

function pipelineIsBusy(project = ui.project) {
  const run = project?.run || {};
  // `stopping` is only a busy state while the server still reports a live
  // run.  A persisted `running: false, status: stopping` pair is stale data,
  // and must not permanently disable the start controls.
  return Boolean(run.running || ui.stopRequested);
}

function pipelineIsStopping(project = ui.project) {
  const run = project?.run || {};
  return Boolean(
    pipelineIsBusy(project)
      && (ui.stopRequested || (run.running && (run.cancel_requested || run.status === "stopping"))),
  );
}

function flushDeferredProjectRender() {
  if (!ui.pendingProjectRender || shouldDeferProjectRender() || !ui.project) return;
  ui.pendingProjectRender = false;
  applyProjectRender(ui.project);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    headers,
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(ui.toastTimer);
  ui.toastTimer = setTimeout(() => toast.classList.remove("visible"), 3000);
}

function resolveUnitActionDialog(value) {
  const dialog = $("unitActionDialog");
  const resolver = ui.unitActionDialogResolver;
  ui.unitActionDialogResolver = null;
  if (dialog?.open) dialog.close(value || "cancel");
  if (resolver) resolver(value || "cancel");
}

function openUnitActionDialog({ title, message, options }) {
  const dialog = $("unitActionDialog");
  const titleElement = $("unitActionDialogTitle");
  const messageElement = $("unitActionDialogMessage");
  const optionsElement = $("unitActionDialogOptions");
  if (!dialog || !titleElement || !messageElement || !optionsElement || typeof dialog.showModal !== "function") {
    return Promise.resolve("cancel");
  }
  if (ui.unitActionDialogResolver) resolveUnitActionDialog("cancel");
  titleElement.textContent = title;
  messageElement.textContent = message;
  optionsElement.replaceChildren();
  options.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `button ${option.className || "secondary"}`;
    button.textContent = option.label;
    button.addEventListener("click", () => resolveUnitActionDialog(option.value));
    optionsElement.append(button);
  });
  return new Promise((resolve) => {
    ui.unitActionDialogResolver = resolve;
    dialog.showModal();
  });
}

function renderProject(project) {
  if (!ui.project) {
    const entry = new URLSearchParams(location.search);
    if (!entry.get('project_id') || entry.get('project_id') === projectIdOf(project)) {
      const target = entry.get('unit_id');
      if (project.units?.some(unit => unit.id === target)) ui.selectedUnitId = target;
    }
  }
  ui.project = project;
  syncDraftsForProject(project);
  updateDraftIndicators();
  const run = project?.run || {};
  if (!run.running) {
    ui.stopRequested = false;
  }
  // The control strip is deliberately updated even when the editor body is
  // protected to preserve a selection or an in-progress manual edit.
  renderPipelineControls(project);
  if (shouldDeferProjectRender()) {
    // The protected editor keeps the user's text, but the rest of the workbench
    // still follows the server: the current panel's status, failure reasons,
    // previous draft, banner and button states are refreshed in place here,
    // before this branch could skip them.
    refreshDetailChrome(project.units?.find((unit) => unit.id === ui.selectedUnitId) || null);
    ui.pendingProjectRender = true;
    return;
  }
  applyProjectRender(project);
}

function applyProjectRender(project) {
  ui.pendingProjectRender = false;
  const stats = project.stats || {};
  const units = project.units || [];
  const selectableIds = new Set(
    units.filter((unit) => ["pending", "cancelled"].includes(unit.status)).map((unit) => unit.id),
  );
  ui.selectedUnitIds.forEach((unitId) => {
    if (!selectableIds.has(unitId)) ui.selectedUnitIds.delete(unitId);
  });
  if (!ui.selectedUnitId || !units.some((unit) => unit.id === ui.selectedUnitId)) {
    ui.selectedUnitId = units[0]?.id || null;
  }
  setElementText("metricTotal", stats.total ?? 0);
  setElementText("metricActive", stats.active ?? 0);
  setElementText("metricPassed", stats.passed ?? 0);
  setElementText("metricNeedsAction", stats.needs_action ?? 0);
  setElementText("metricAccepted", stats.accepted_risk ?? 0);
  setElementText("progressText", `${stats.progress_percent ?? 0}%`);
  const progressWidth = `${stats.progress_percent ?? 0}%`;
  if ($("progressBar").style.width !== progressWidth) $("progressBar").style.width = progressWidth;
  setElementText("unitCount", `${units.length} 个`);
  const concurrencyInput = $("concurrencyInput");
  if (!ui.concurrencyDirty && document.activeElement !== concurrencyInput) {
    concurrencyInput.value = project.config?.max_concurrency ?? 3;
  }
  const sourceFile = project.project?.source_file;
  const currentProject = project.current_project || {};
  const sourceLabel = sourceFile?.name
    ? `${sourceFile.name} · ${sourceFile.format || "source"}`
    : "尚未导入源文件";
  setElementText("projectSource", `${currentProject.name || "当前项目"} · ${sourceLabel}`);
  setElementText("projectBreadcrumb", currentProject.name || "当前项目");
  renderUnits(units);
  renderDetail(units.find((unit) => unit.id === ui.selectedUnitId));
  renderEvents(project.events || []);
  renderSelectionControls(units, pipelineIsBusy(project));
  renderOutputStatus(ui.outputStatus);
  // Keep the top-bar concept link pointed at the current project/unit even when
  // the detail body is empty or protected from re-rendering.
  if (typeof window.qualitySyncEntry === "function") {
    window.qualitySyncEntry();
  }
}

function renderPipelineControls(project) {
  const run = project?.run || {};
  const busy = pipelineIsBusy(project);
  const stopping = pipelineIsStopping(project);
  const normalizedRun = !run.running && run.status === "stopping"
    ? { ...run, status: run.cancel_requested ? "cancelled" : "ready" }
    : run;
  const displayedRun = stopping ? { ...normalizedRun, status: "stopping", cancel_requested: true } : normalizedRun;
  renderRunBadge(displayedRun);

  const start = $("startButton");
  const stop = $("stopButton");
  const importButton = $("importButton");
  const concurrencyInput = $("concurrencyInput");
  const confirmConcurrency = $("confirmConcurrencyButton");
  const concurrencyStatus = $("concurrencyStatus");
  if (start) start.disabled = busy || ui.concurrencyDirty || ui.selectedUnitIds.size === 0;
  if (stop) {
    stop.disabled = !busy || stopping;
    setElementText(stop, stopping ? "正在停止…" : "停止流水线");
  }
  if (importButton) importButton.disabled = busy;
  if (concurrencyInput) concurrencyInput.disabled = busy || ui.concurrencySaving;
  if (confirmConcurrency) {
    confirmConcurrency.disabled = busy || ui.concurrencySaving || !ui.concurrencyDirty;
    setElementText(confirmConcurrency, ui.concurrencySaving ? "正在确认…" : "确认并发数");
  }
  if (concurrencyStatus) {
    const confirmed = Number(project?.config?.max_concurrency ?? 3);
    const runConcurrency = project?.run?.running ? project.run.max_concurrency : null;
    const message = runConcurrency != null
      ? `本次运行：${runConcurrency}`
      : ui.concurrencyDirty
        ? `未确认（当前已确认：${confirmed}）`
        : `已确认：${confirmed}`;
    setElementText(concurrencyStatus, message);
    concurrencyStatus.classList.toggle("dirty", ui.concurrencyDirty && !busy);
  }

  // Keep the unit-selection controls in the same local busy state.  This is
  // important for the short interval between the user's click and the stop
  // endpoint response: the whole control strip must agree that the run is
  // stopping, even if the editor body is protected from re-rendering.
  updateSelectionControls();

  const notice = $("pipelineStopNotice");
  if (notice) {
    notice.hidden = !stopping;
    setElementText(notice, "停止请求已发出，正在等待当前任务安全结束…");
  }
}

function glyphUnitList(units, limit = 6) {
  const list = Array.isArray(units) ? units.filter(Boolean).map(String) : [];
  if (!list.length) return "未知单元";
  const shown = list.slice(0, limit).join("、");
  return list.length > limit ? `${shown} 等 ${list.length} 个` : shown;
}

function glyphPrecheckText(status) {
  const precheck = status?.glyph_precheck;
  if (!precheck || precheck.available !== true) {
    if (!ui.glyphRepairBusy || !ui.glyphRepairProgress) return "";
    return `正在加入重译队列：${ui.glyphRepairProgress.queued} / ${ui.glyphRepairProgress.total}${ui.glyphRepairProgress.current_id ? `，当前 ${ui.glyphRepairProgress.current_id}` : ""}。`;
  }
  const replaceable = Array.isArray(precheck.replaceable) ? precheck.replaceable : [];
  const blocking = (Array.isArray(precheck.blocking) ? precheck.blocking : [])
    .filter((row) => String(row.codepoint || "").toUpperCase() !== "U+FFFD");
  const damaged = Array.isArray(precheck.replacement_character_units)
    ? precheck.replacement_character_units : [];
  const damagedTranslations = damaged.filter((row) => row.translation_contains);
  const repairable = damagedTranslations.filter((row) => row.translation_repairable);
  const sourceDamaged = damaged.filter((row) => row.source_contains);
  const parts = [];
  if (damagedTranslations.length) {
    const units = damagedTranslations.slice(0, 6)
      .map((row) => `${row.id}${row.translation_excerpt ? `「${row.translation_excerpt}」` : ""}`);
    const moreUnits = damagedTranslations.length > units.length
      ? ` 等另外 ${damagedTranslations.length - units.length} 个单元` : "";
    parts.push(`PDF 暂不能导出：${damagedTranslations.length} 个译文含 U+FFFD（�），原字符可能已丢失；${units.join("；")}${moreUnits}。`);
    if (repairable.length) {
      parts.push(`${repairable.length} 个源文完整的机器译文可批量重译并复检。`);
    }
    const sourceBlocked = damagedTranslations.filter((row) => row.source_contains);
    if (sourceBlocked.length) {
      parts.push(`其中 ${sourceBlocked.length} 个单元的源文也含 U+FFFD，无法从当前文件自动还原：${glyphUnitList(sourceBlocked.map((row) => row.id))}。`);
    }
    const humanOwned = damagedTranslations.filter((row) => row.user_edited_translation && !row.source_contains);
    if (humanOwned.length) {
      parts.push(`另有 ${humanOwned.length} 个单元是人工译文，软件会保留原文，不自动覆盖。`);
    }
    const otherBlocked = damagedTranslations.filter((row) => !row.source_contains && !row.user_edited_translation && !row.translation_repairable);
    if (otherBlocked.length) {
      parts.push(`另有 ${otherBlocked.length} 个单元当前状态不允许自动重译。`);
    }
  } else if (sourceDamaged.length) {
    parts.push(`源文中发现 U+FFFD，原文件可能已丢失字符：${glyphUnitList(sourceDamaged.map((row) => row.id))}。当前译文未包含该标记。`);
  }
  if (blocking.length) {
    const detail = blocking
      .map((row) => `${row.codepoint}${row.character ? `（${row.character}）` : ""} 出现在 ${glyphUnitList(row.units)}`)
      .join("；");
    parts.push(`PDF 字体目前无法绘制：${detail}。`);
  }
  if (replaceable.length) {
    const detail = replaceable
      .map((row) => `${row.codepoint}→${row.replacement} 出现在 ${glyphUnitList(row.units)}`)
      .join("；");
    parts.push(`导出时有 ${replaceable.length} 种字符会按已登记规则自动处理：${detail}。`);
  }
  if (ui.glyphRepairBusy && ui.glyphRepairProgress) {
    const progress = ui.glyphRepairProgress;
    parts.push(`正在加入重译队列：${progress.queued} / ${progress.total}${progress.current_id ? `，当前 ${progress.current_id}` : ""}。`);
  }
  return parts.join(" ");
}

function renderOutputStatus(status) {
  const button = $("exportButton");
  const label = $("outputStatusText");
  const select = $("outputFormatSelect");
  if (!button || !label) return;
  ui.outputStatus = status;
  const selectedStatus = status?.formats?.[ui.outputFormat] || status?.output || {};
  const ready = Boolean(selectedStatus.ready ?? status?.ready) && !pipelineIsBusy();
  const glyphScan = status?.glyph_precheck;
  const pdfGlyphBlocked = ui.outputFormat === "pdf"
    && glyphScan?.available === true
    && Array.isArray(glyphScan.blocking)
    && glyphScan.blocking.length > 0;
  button.disabled = !ready || ui.exporting || pdfGlyphBlocked;
  button.title = pdfGlyphBlocked ? "PDF 有字符无法安全绘制，请先处理下方提示" : "";
  setElementText(button, ui.exporting ? "正在生成…" : "输出完整文档");
  if (select && select.value !== ui.outputFormat && document.activeElement !== select) select.value = ui.outputFormat;
  const precheck = $("glyphPrecheck");
  if (precheck) {
    const precheckText = status ? glyphPrecheckText(status) : "";
    setElementText("glyphPrecheckText", precheckText);
    precheck.hidden = !precheckText && !ui.glyphRepairBusy;
    const glyph = status?.glyph_precheck || {};
    const hasReplacement = (glyph.replacement_character_units || []).some((row) => row.translation_contains);
    precheck.classList.toggle("blocking", Boolean(glyph.blocking?.length || hasReplacement));
    const repairButton = $("glyphRepairButton");
    const repairable = (glyph.replacement_character_units || []).filter((row) => row.translation_repairable);
    if (repairButton) {
      repairButton.hidden = repairable.length === 0 && !ui.glyphRepairBusy;
      repairButton.disabled = Boolean(ui.glyphRepairBusy || pipelineIsBusy() || draftCount() > 0);
      setElementText(repairButton, ui.glyphRepairBusy
        ? `正在排队 ${ui.glyphRepairProgress?.queued || 0} / ${ui.glyphRepairProgress?.total || 0}`
        : `批量重译并复检 ${repairable.length} 个单元`);
      repairButton.title = draftCount() > 0 ? "请先保存或放弃浏览器中的编辑草稿" : "将调用现有翻译与校验流程";
    }
  }
  if (!status) {
    setElementText(label, "正在检查输出状态");
    return;
  }
  const output = selectedStatus;
  const replaced = Number(output.character_replacements?.count) || 0;
  const replacedNote = replaced ? ` · 已按等价字符替换 ${replaced} 处` : "";
  if (output.available && output.filename && output.exported_at) {
    setElementText(label, `最近生成：${output.filename} · ${formatTime(output.exported_at)}${replacedNote}`);
  } else {
    setElementText(label, status.reason || "尚不可输出");
  }
}

function setElementText(elementOrId, value) {
  const element = typeof elementOrId === "string" ? $(elementOrId) : elementOrId;
  if (element && element.textContent !== String(value ?? "")) element.textContent = String(value ?? "");
}

function renderRunBadge(run) {
  const badge = $("runBadge");
  const status = run.status || "ready";
  const labels = {
    ready: "准备就绪",
    running: "流水线运行中",
    stopping: "正在停止",
    cancelled: "流水线已停止",
    needs_action: "等待人工裁决",
    completed: "项目已完成",
  };
  setElementText(badge, labels[status] || status);
  const nextClass = `run-badge ${status}`;
  if (badge.className !== nextClass) badge.className = nextClass;
}

function matchesFilter(unit) {
  if (ui.filter === "all") return true;
  if (ui.filter === "active") return ["waiting_translation", "waiting_review", "translating", "reviewing"].includes(unit.status);
  if (ui.filter === "needs_action") return unit.status === "needs_action";
  if (ui.filter === "done") return ["passed", "user_modified", "accepted_risk"].includes(unit.status);
  return true;
}

function createUnitCard(unit) {
  const card = document.createElement("div");
  card.dataset.unitId = unit.id;
  card.innerHTML = `<div class="unit-card-top"><label class="unit-select" title="选择此翻译单元"><input data-role="select" type="checkbox"><span class="sr-only">选择此翻译单元</span></label><strong data-role="id"></strong><span class="status-label" data-role="status"></span></div>
    <div class="unit-preview" data-role="preview"></div>
    <div class="unit-card-footer"><span class="unit-card-footer-meta"><span data-role="alert"></span><span data-role="words"></span></span><span data-role="time"></span></div>`;
  card.addEventListener("click", () => {
    ui.selectedUnitId = card.dataset.unitId;
    renderProject(ui.project);
  });
  card.querySelector('[data-role="select"]').addEventListener("click", (event) => event.stopPropagation());
  card.querySelector('[data-role="select"]').addEventListener("change", (event) => {
    const checkbox = event.currentTarget;
    if (checkbox.checked) ui.selectedUnitIds.add(card.dataset.unitId);
    else ui.selectedUnitIds.delete(card.dataset.unitId);
    updateSelectionControls();
  });
  return card;
}

function updateUnitCard(card, unit) {
  const nextCardClass = `unit-card${unit.id === ui.selectedUnitId ? " selected" : ""}`;
  if (card.className !== nextCardClass) card.className = nextCardClass;
  const id = card.querySelector('[data-role="id"]');
  const status = card.querySelector('[data-role="status"]');
  const preview = card.querySelector('[data-role="preview"]');
  const alert = card.querySelector('[data-role="alert"]');
  const words = card.querySelector('[data-role="words"]');
  const time = card.querySelector('[data-role="time"]');
  const checkbox = card.querySelector('[data-role="select"]');
  const draft = draftForUnit(unit);
  const previewText = draft?.dirty ? draft.value : (unit.translation || unit.source);
  const alertText = draft?.dirty
    ? "修改未保存"
    : unit.status === "needs_action"
      ? "需要处理"
      : unit.status === "cancelled"
        ? "已停止，可重启"
        : "";

  if (id.textContent !== unit.id) id.textContent = unit.id;
  const nextStatusClass = `status-label ${statusClass(unit.status)}`;
  if (status.className !== nextStatusClass) status.className = nextStatusClass;
  if (status.textContent !== statusText(unit.status)) status.textContent = statusText(unit.status);
  if (preview.textContent !== previewText) preview.textContent = previewText;
  const nextAlertClass = draft?.dirty ? "unit-alert draft-alert" : unit.status === "needs_action" ? "unit-alert" : "";
  if (alert.className !== nextAlertClass) alert.className = nextAlertClass;
  if (alert.textContent !== alertText) alert.textContent = alertText;
  const wordsText = `${unit.source_words} 词`;
  if (words.textContent !== wordsText) words.textContent = wordsText;
  const timeText = formatTime(unit.updated_at);
  if (time.textContent !== timeText) time.textContent = timeText;
  const selectable = ["pending", "cancelled"].includes(unit.status);
  checkbox.disabled = !selectable || pipelineIsBusy();
  if (checkbox.checked !== ui.selectedUnitIds.has(unit.id)) checkbox.checked = ui.selectedUnitIds.has(unit.id);
}

function updateSelectionControls() {
  const count = ui.selectedUnitIds.size;
  const selectAll = $("selectAllButton");
  const clear = $("clearSelectionButton");
  const start = $("startButton");
  const running = pipelineIsBusy();
  const selectableCount = (ui.project?.units || []).filter((unit) => ["pending", "cancelled"].includes(unit.status)).length;
  setElementText("selectedCount", `已选 ${count} 个`);
  if (selectAll) selectAll.disabled = running || selectableCount === 0 || count === selectableCount;
  if (clear) clear.disabled = running || count === 0;
  if (start) start.disabled = running || count === 0 || ui.concurrencyDirty;
}

function renderSelectionControls(units, running) {
  updateSelectionControls();
  const selectableCount = units.filter((unit) => ["pending", "cancelled"].includes(unit.status)).length;
  setElementText("selectionHint", selectableCount ? "勾选要处理的单元" : "暂无可启动的单元");
}

function renderUnits(units) {
  const list = $("unitList");
  const visible = units.filter(matchesFilter);
  document.querySelectorAll(".filter-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.filter === ui.filter));
  if (shouldDeferProjectRender()) {
    ui.pendingProjectRender = true;
    return;
  }
  if (!visible.length) {
    if (!(list.children.length === 1 && list.firstElementChild.classList.contains("empty-list"))) {
      Array.from(list.children).forEach((child) => child.remove());
      const empty = document.createElement("div");
      empty.className = "empty-list";
      empty.textContent = "当前筛选下没有单元";
      list.appendChild(empty);
    }
    return;
  }
  list.querySelector(".empty-list")?.remove();
  const existing = new Map(
    Array.from(list.querySelectorAll(":scope > .unit-card")).map((card) => [card.dataset.unitId, card]),
  );
  const visibleIds = new Set(visible.map((unit) => unit.id));
  let cursor = list.firstElementChild;
  for (const unit of visible) {
    const card = existing.get(unit.id) || createUnitCard(unit);
    updateUnitCard(card, unit);
    if (card !== cursor) list.insertBefore(card, cursor);
    cursor = card.nextElementSibling;
  }
  Array.from(list.querySelectorAll(":scope > .unit-card")).forEach((card) => {
    if (!visibleIds.has(card.dataset.unitId)) card.remove();
  });
}

function issueMarkup(issue) {
  const warning = issue.severity === "warning" ? " warning" : "";
  const suggestion = typeof issue.evidence?.suggestion === "string"
    ? issue.evidence.suggestion.trim()
    : "";
  const suggestionMarkup = suggestion
    ? `<p class="issue-suggestion"><strong>翻译建议</strong>${escapeHtml(suggestion)}</p>`
    : "";
  return `<div class="issue-card${warning}"><strong>${escapeHtml(issue.rule || "review")}</strong><p>${escapeHtml(issue.message || "需要人工确认。")}</p>${suggestionMarkup}</div>`;
}

// The draft a retranslation is replacing, kept readable while the new result is
// on its way. It lives in the one-shot feedback record together with the
// revision it belongs to, so it is always shown as the *previous* version: it
// never takes the place of the current translation, and its old verdict is
// never presented as the result of the run in flight.
function previousDraftMarkup(unit) {
  const feedback = unit && typeof unit.pending_translation_feedback === "object" && unit.pending_translation_feedback
    ? unit.pending_translation_feedback
    : null;
  const text = feedback && typeof feedback.previous_translation === "string" ? feedback.previous_translation : "";
  if (!feedback || !text.trim()) return "";
  const revision = Number.isInteger(feedback.source_revision) ? feedback.source_revision : null;
  const review = feedback.previous_review && typeof feedback.previous_review === "object" ? feedback.previous_review : null;
  const verdict = review ? String(review.verdict || "") : "";
  const verdictText = verdict === "PASS"
    ? "上一版校验通过（只针对上一版，不代表本次重译结果）"
    : verdict
      ? `上一版校验结论：${verdict === "FAIL" ? "不通过" : verdict}（只针对上一版）`
      : "上一版没有校验结论";
  return `<div class="text-box" data-role="previous-draft">
    <h3>上一版译文${revision === null ? "" : `（第 ${revision} 版）`} · 重译前</h3>
    <p>${escapeHtml(text)}</p>
    <p class="hash-line">${escapeHtml(verdictText)}</p>
  </div>`;
}

function unitActionBarMarkup(unit) {
  if (!EDITABLE_STATUSES.has(unit.status)) return "";
  const finalAction = unit.status === "needs_action"
    ? `<button class="button danger action-final" data-action="accept-risk" type="button">⚠ 直接通过</button>`
    : `<button class="button primary action-final" data-action="next" type="button">→ 下一条</button>`;
  const reviewAction = unit.status === "accepted_risk" ? "" : (() => {
    const cleanReviewLabel = unit.status === "needs_action" ? "✓ 修改并复检" : "⟳ 重新校验";
    return `<button class="button secondary action-review" data-action="review" type="button">
        <span class="action-label-slot" aria-hidden="true">
          <span class="action-label action-label-clean">${cleanReviewLabel}</span>
          <span class="action-label action-label-dirty">✓ 修改并复检</span>
        </span>
      </button>`;
  })();
  return `<div class="unit-action-bar" data-role="unit-action-bar">
    <div class="action-group action-group-left">
      <button class="button secondary" data-action="retranslate" type="button">↻ 重新翻译</button>
      ${reviewAction}
    </div>
    <div class="action-group action-group-edit" data-role="edit-actions" aria-hidden="true">
      <span class="edit-dirty-hint" data-role="dirty-hint">● 未保存修改</span>
      <button class="button secondary" data-action="discard" type="button">放弃修改</button>
      <button class="button secondary" data-action="save" type="button">💾 保存修改</button>
    </div>
    <div class="action-group action-group-final">${finalAction}</div>
  </div>`;
}

function nextUnitFor(unit) {
  const units = Array.isArray(ui.project?.units) ? ui.project.units : [];
  const index = units.findIndex((item) => item.id === unit?.id);
  return index >= 0 ? units[index + 1] || null : null;
}

function renderDetail(unit) {
  const panel = $("detailPanel");
  syncEditDraftFromDom();
  if (!unit) {
    if (ui.detailRenderKey === "empty") return;
    panel.innerHTML = '<div class="empty-state">选择一个翻译单元查看详情</div>';
    panel.dataset.detailUnitId = "";
    ui.detailRenderKey = "empty";
    return;
  }
  const editable = EDITABLE_STATUSES.has(unit.status);
  const draft = editable ? ensureDraftForUnit(unit) : draftForUnit(unit);
  const activeTextarea = $("editedTranslation");
  if (
    activeTextarea
    && activeTextarea.dataset.unitId === unit.id
    && editable
    && (document.activeElement === activeTextarea || isDraftDirty(unit))
  ) {
    // The editor keeps the user's text, but the rest of the panel is not
    // frozen: status, failure reasons, the previous draft and the button states
    // all follow the server.
    refreshDetailChrome(unit);
    ui.pendingProjectRender = true;
    return;
  }
  const renderKey = JSON.stringify({
    id: unit.id,
    status: unit.status,
    source: unit.source,
    source_sha256: unit.source_sha256,
    translation: unit.translation,
    translation_revision: unit.translation_revision,
    translation_model: unit.translation_model,
    review_issues: unit.review_issues,
    user_decision: unit.user_decision,
    model_repair: unit.model_repair,
    editable,
  });
  if (panel.dataset.detailUnitId === unit.id && ui.detailRenderKey === renderKey) {
    updateDecisionControls(unit);
    return;
  }
  if (shouldDeferProjectRender()) {
    // A protected editor keeps its own text, but nothing else about this unit
    // may go stale: status, failure reasons, the previous draft and the button
    // states are refreshed in place.
    refreshDetailChrome(unit);
    ui.pendingProjectRender = true;
    return;
  }
  const issues = unit.review_issues || [];
  const actionBar = unitActionBarMarkup(unit);
  const textColumns = `<div class="text-columns">
    <div class="text-box"><h3>源文</h3><p>${escapeHtml(unit.source)}</p></div>
    <div class="text-box translation-box"><h3>${editable ? "译文（可编辑）" : "译文"}</h3>${editable
      ? `<textarea id="editedTranslation" rows="1" aria-label="编辑译文">${escapeHtml(draft?.value ?? unit.translation ?? "")}</textarea><p id="editorDraftHint" class="editor-draft-hint"></p>`
      : `<p>${escapeHtml(unit.translation || "等待翻译")}</p>`}</div>
  </div>`;
  const hashMarker = unit.status === "accepted_risk"
    ? `<span class="inline-state risk">已接受风险</span>`
    : unit.status === "user_modified"
      ? `<span class="inline-state manual">用户已修改</span>`
      : "";
  const stateBanner = detailStateMarkup(unit);
  const stateBlock = `<div data-role="detail-state">${stateBanner}</div>`;
  const state = stateBanner || (unit.status === "user_modified" ? "" : textColumns);
  // The pieces that must keep updating even while the editor is protected are
  // wrapped so `refreshDetailChrome` can replace them without touching the
  // textarea; the previous draft always has a container, empty or not.
  const issuesBlock = detailIssuesMarkup(unit);
  const previousDraft = `<div data-role="detail-previous">${previousDraftMarkup(unit)}</div>`;
  const detailBody = unit.status === "needs_action"
    ? `${stateBlock}${issuesBlock}${actionBar}${previousDraft}${textColumns}`
    : unit.status === "accepted_risk"
      ? `${stateBlock}${issuesBlock}${actionBar}${previousDraft}${textColumns}`
      : unit.status === "passed"
        ? `${stateBlock}${actionBar}${textColumns}`
        : unit.status === "user_modified"
          ? `${stateBlock}${issuesBlock}${actionBar}${previousDraft}${textColumns}`
          // `state` is the banner itself for the waiting/running states, and
          // that banner already sits inside `stateBlock`: appending it again
          // would render the same hint twice. Only the text columns are added.
          : `${stateBlock}${stateBanner ? "" : state}${issuesBlock}${previousDraft}`;
  const qualityTools = `<details class="quality-unit-tools"><summary>当前单元参考快照</summary><div id="qualityUnitReference"></div></details>
    <details class="quality-unit-tools"><summary>表达与语气建议</summary>
      <p class="quality-section-note">只针对已保存的译文生成建议，不会覆盖编辑中的草稿。</p>
      <button id="qualitySuggestButton" class="button secondary" type="button" disabled>请求表达建议</button>
      <p id="qualitySuggestStatus" role="status"></p>
      <ul id="qualitySuggestList" class="quality-suggestion-list"></ul>
    </details>`;
  const repairMarkup = modelRepairMarkup(unit);
  panel.innerHTML = `<div class="panel-heading" data-role="detail-head">${detailHeadInnerMarkup(unit)}</div>
    <div class="detail-content"><div class="detail-title-row"><div><h2>${escapeHtml(unit.source.slice(0, 80))}${unit.source.length > 80 ? "…" : ""}</h2><div class="hash-line"><span class="hash-label">sha256 · ${escapeHtml(unit.source_sha256)}</span>${hashMarker}</div></div></div>${repairMarkup}${detailBody}${qualityTools}</div>`;
  panel.dataset.detailUnitId = unit.id;
  ui.detailRenderKey = renderKey;
  const textarea = $("editedTranslation");
  if (textarea) {
    textarea.dataset.unitId = unit.id;
    // What this element was rendered from. `syncEditDraftFromDom` compares the
    // live value against it: a textarea that still holds exactly this text was
    // not edited by anyone, so a server-side update rebases it instead of being
    // recorded as human input.
    textarea.dataset.projectId = projectIdOf();
    textarea.dataset.baseValue = String(draft?.value ?? unit.translation ?? "");
    textarea.dataset.baseRevision = String(Number.isInteger(unit.translation_revision) ? unit.translation_revision : 0);
    fitTranslationEditorToContent(textarea);
    textarea.addEventListener("focus", () => {
      ensureDraftForUnit(unit);
    });
    textarea.addEventListener("input", () => {
      persistDraft(unit, textarea.value);
      fitTranslationEditorToContent(textarea);
      updateDecisionControls(unit);
      const selectorId = window.CSS?.escape ? CSS.escape(unit.id) : String(unit.id).replaceAll('"', '\\"');
      const card = document.querySelector(`.unit-card[data-unit-id="${selectorId}"]`);
      if (card) updateUnitCard(card, unit);
    });
  }
  panel.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => handleUnitAction(unit, button.dataset.action)));
  updateDecisionControls(unit);
  // The panel was rebuilt: rebind and repopulate the unit-level concept tools.
  if (typeof window.qualityAttachUnit === "function") {
    window.qualityAttachUnit(unit);
  }
}

// Refresh the parts of a protected detail panel that are not the editor.
// Uses the containers `renderDetail` renders; a panel that has none (an older
// markup shape) simply keeps its current content.
function refreshDetailChrome(unit) {
  const panel = $("detailPanel");
  if (!panel || !unit || panel.dataset.detailUnitId !== unit.id) return;
  const head = panel.querySelector('[data-role="detail-head"]') || panel.querySelector(".panel-heading");
  if (head) {
    head.setAttribute("data-role", "detail-head");
    head.innerHTML = detailHeadInnerMarkup(unit);
  }
  // Banner, failure reasons and the previous draft follow the server; the
  // containers are created if this panel never had them (a `passed` panel has
  // neither), and the editor inside `.text-columns` is never touched.
  const state = ensureDetailContainer(panel, "detail-state", [".unit-action-bar", ".text-columns", ".detail-content"]);
  if (state) state.innerHTML = detailStateMarkup(unit);
  const issues = ensureDetailContainer(panel, "detail-issues", [".unit-action-bar", ".text-columns", ".detail-content"]);
  if (issues) issues.outerHTML = detailIssuesMarkup(unit);
  const previous = ensureDetailContainer(panel, "detail-previous", [".text-columns", ".detail-content"]);
  if (previous) previous.innerHTML = previousDraftMarkup(unit);
  updateDecisionControls(unit);
}

function detailHeadInnerMarkup(unit) {
  return `<div><h2>${escapeHtml(unit.id)} · ${statusText(unit.status)}</h2></div><span class="status-label ${statusClass(unit.status)}">${escapeHtml(unit.translation_model || "待处理")}</span>`;
}

// The status banner alone (empty for statuses that show columns instead). Shared
// by the full render and the in-place refresh, so a stale "校验通过" banner can be
// replaced or cleared without touching a protected editor.
function detailStateMarkup(unit) {
  if (unit.status === "passed") {
    return `<div class="result-banner success"><div class="state-icon success">✓</div><div><strong>独立校验通过</strong><span>该单元已进入完成集合。</span></div></div>`;
  }
  if (unit.status === "translating" || unit.status === "reviewing") {
    return `<div class="waiting-state"><strong>${statusText(unit.status)}</strong><span>正在调用 API，完成后会自动更新这里。</span></div>`;
  }
  if (unit.status === "waiting_translation" || unit.status === "waiting_review") {
    return `<div class="waiting-state"><strong>等待中</strong><span>任务已进入队列，等待可用的 API 工作槽。</span></div>`;
  }
  return "";
}

// A panel rendered from a status without an issue or previous-draft section has
// no container for one. It is created next to a stable anchor on first use —
// never by rebuilding the panel, and never over the protected textarea.
function ensureDetailContainer(panel, role, anchorSelectors) {
  const present = panel.querySelector(`[data-role="${role}"]`);
  if (present) return present;
  const anchor = anchorSelectors.map((selector) => panel.querySelector(selector)).find(Boolean);
  if (!anchor || !anchor.parentNode || typeof document.createElement !== "function") return null;
  const container = document.createElement("div");
  container.setAttribute("data-role", role);
  anchor.parentNode.insertBefore(container, anchor);
  return container;
}

function detailIssuesMarkup(unit) {
  const issues = unit.review_issues || [];
  return `<div class="issue-section" data-role="detail-issues"${issues.length ? "" : " hidden"}>${issues.length ? `<h3>校验反馈 · ${issues.length}</h3>${issues.map(issueMarkup).join("")}` : ""}</div>`;
}

function syncEditDraftFromDom() {
  const textarea = $("editedTranslation");
  if (!textarea || !textarea.dataset.unitId) return;
  // A panel of another project is not this editor's business: the draft stores
  // are per project, and a stale textarea must never seed a record elsewhere.
  if (textarea.dataset.projectId && textarea.dataset.projectId !== projectIdOf()) return;
  const unit = ui.project?.units?.find((item) => item.id === textarea.dataset.unitId);
  if (!unit || !EDITABLE_STATUSES.has(unit.status)) return;
  // A textarea that still holds exactly what it was rendered with is not a
  // human edit — the server moved on and the panel is about to follow it. This
  // decision is taken on the live value itself, in the same pass, before any
  // draft is rebased; it is never inferred from a second look at the DOM.
  if (typeof textarea.dataset.baseValue === "string" && textarea.value === textarea.dataset.baseValue) return;
  const current = ensureDraftForUnit(unit);
  if (textarea.value !== current.value) persistDraft(unit, textarea.value);
}

function updateDecisionControls(unitOrId) {
  const unitId = typeof unitOrId === "string" ? unitOrId : unitOrId?.id;
  const unit = typeof unitOrId === "string"
    ? ui.project?.units?.find((item) => item.id === unitOrId)
    : unitOrId;
  const panel = $("detailPanel");
  // An asynchronous action can finish after the UI has already moved to a
  // different Unit (for example, "保存并继续").  Never apply the old
  // Unit's dirty/in-flight state to the newly selected detail panel.
  if (!panel || !unit || panel.dataset.detailUnitId !== unitId) return;
  const dirty = unit ? isDraftDirty(unit) : false;
  const inFlight = ui.inFlightUnitIds.has(unitId);
  const reviewButton = panel.querySelector('[data-action="review"]');
  const finalButton = panel.querySelector('[data-action="accept-risk"], [data-action="next"]');
  const retranslateButton = panel.querySelector('[data-action="retranslate"]');
  const editActions = panel.querySelector('[data-role="edit-actions"]');
  if (editActions) {
    editActions.classList.toggle("is-visible", dirty);
    editActions.classList.toggle("is-hidden", !dirty);
    editActions.setAttribute("aria-hidden", String(!dirty));
    editActions.inert = !dirty;
  }
  if (reviewButton) {
    const cleanLabel = reviewButton.querySelector(".action-label-clean");
    const dirtyLabel = reviewButton.querySelector(".action-label-dirty");
    const needsAction = unit?.status === "needs_action";
    if (cleanLabel) cleanLabel.textContent = needsAction ? "✓ 修改并复检" : "⟳ 重新校验";
    if (dirtyLabel) dirtyLabel.textContent = "✓ 修改并复检";
    reviewButton.classList.toggle("is-dirty", dirty);
    reviewButton.setAttribute("aria-label", dirty || needsAction ? "修改并复检" : "重新校验");
    reviewButton.classList.toggle("primary", dirty);
    reviewButton.classList.toggle("secondary", !dirty);
    // A needs_action review is deliberately unavailable until the user has
    // changed the translation.  For passed/user_modified, clean review is
    // the normal explicit recheck action.
    reviewButton.disabled = inFlight || (needsAction && !dirty);
  }
  if (retranslateButton) {
    retranslateButton.disabled = inFlight;
    retranslateButton.classList.toggle("blocked-draft", dirty);
    retranslateButton.title = dirty ? "请先保存编辑内容" : "";
  }
  if (finalButton) {
    const isNext = finalButton.dataset.action === "next";
    const nextAvailable = Boolean(nextUnitFor(unit));
    finalButton.disabled = inFlight || (isNext && !nextAvailable);
    finalButton.classList.toggle("primary", isNext && !dirty);
    finalButton.classList.toggle("secondary", isNext && dirty);
    finalButton.classList.toggle("danger", !isNext);
    if (isNext && !nextAvailable) finalButton.title = "已是最后一条";
    else finalButton.title = "";
  }
  panel.querySelectorAll("[data-action]").forEach((button) => {
    if (["review", "retranslate", "accept-risk", "next"].includes(button.dataset.action)) return;
    if (inFlight) {
      button.disabled = true;
      return;
    }
    if (button.dataset.action === "save" || button.dataset.action === "discard") {
      button.disabled = !dirty;
    } else {
      // Dirty actions remain clickable so the handler can explain why the
      // operation is blocked instead of silently doing nothing.
      button.disabled = false;
    }
  });
  const hint = panel.querySelector("#editorDraftHint");
  if (hint) {
    setElementText(hint, dirty ? "修改未保存：保存后才会写入项目。" : "当前内容已与项目正式译文同步。");
    hint.classList.toggle("dirty", dirty);
  }
  updateDraftIndicators();
}

function renderEvents(events) {
  const list = $("eventList");
  const latest = events.slice(-28).reverse();
  if (shouldDeferProjectRender()) {
    ui.pendingProjectRender = true;
    return;
  }
  if (!latest.length) {
    if (list.children.length === 1 && list.firstElementChild.classList.contains("empty-list")) return;
    Array.from(list.children).forEach((child) => child.remove());
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "暂无运行记录";
    list.appendChild(empty);
    return;
  }
  list.querySelector(".empty-list")?.remove();
  const existing = new Map(
    Array.from(list.querySelectorAll(":scope > .event-row")).map((row) => [row.dataset.eventKey, row]),
  );
  const seen = new Set();
  const occurrences = new Map();
  let cursor = list.firstElementChild;
  latest.forEach((event) => {
    const baseKey = `${event.at || ""}|${event.unit_id || ""}|${event.message || ""}`;
    const occurrence = occurrences.get(baseKey) || 0;
    occurrences.set(baseKey, occurrence + 1);
    const eventKey = `${baseKey}|${occurrence}`;
    const row = existing.get(eventKey) || createEventRow(eventKey);
    updateEventRow(row, event);
    seen.add(eventKey);
    if (row !== cursor) list.insertBefore(row, cursor);
    cursor = row.nextElementSibling;
  });
  Array.from(list.querySelectorAll(":scope > .event-row")).forEach((row) => {
    if (!seen.has(row.dataset.eventKey)) row.remove();
  });
}

function createEventRow(eventKey) {
  const row = document.createElement("div");
  row.className = "event-row";
  row.dataset.eventKey = eventKey;
  row.innerHTML = '<span class="event-time"></span><span class="event-message"><strong data-role="unit"></strong><span data-role="separator"></span><span data-role="text"></span></span>';
  return row;
}

function updateEventRow(row, event) {
  setElementText(row.querySelector(".event-time"), formatTime(event.at));
  setElementText(row.querySelector('[data-role="unit"]'), event.unit_id || "");
  setElementText(row.querySelector('[data-role="separator"]'), event.unit_id ? " · " : "");
  setElementText(row.querySelector('[data-role="text"]'), event.message || "");
}

async function refresh() {
  try {
    const [project, outputStatus] = await Promise.all([
      api("/api/project"),
      api("/api/project/output-status"),
    ]);
    $("connectionDot").classList.remove("offline");
    setElementText("connectionText", "本地服务已连接");
    ui.outputStatus = outputStatus;
    renderProject(project);
    if (pipelineIsBusy(project)) {
      clearTimeout(ui.pollTimer);
      ui.pollTimer = setTimeout(refresh, 850);
    } else {
      clearTimeout(ui.pollTimer);
    }
  } catch (error) {
    $("connectionDot").classList.add("offline");
    setElementText("connectionText", "服务不可用");
    renderOutputStatus(null);
    showToast(error.message);
    clearTimeout(ui.pollTimer);
    ui.pollTimer = setTimeout(refresh, 2200);
  }
}

async function exportDocument() {
  if (ui.exporting) return;
  ui.exporting = true;
  renderOutputStatus(ui.outputStatus);
  try {
    const result = await api("/api/project/output", {
      method: "POST",
      body: JSON.stringify({ format: ui.outputFormat }),
    });
    const link = document.createElement("a");
    link.href = result.download_url || `/api/project/output/latest?format=${encodeURIComponent(ui.outputFormat)}`;
    link.download = result.output?.filename || "translated_document";
    document.body.appendChild(link);
    link.click();
    link.remove();
    showToast(`完整译文档已生成：${result.output?.filename || "已开始下载"}`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    ui.exporting = false;
    renderOutputStatus(ui.outputStatus);
  }
}

async function startPipeline() {
  if (pipelineIsBusy()) return;
  if (draftCount() > 0) {
    showToast("请保存编辑内容！");
    return;
  }
  if (ui.concurrencyDirty) {
    showToast("并发数尚未确认，请先点击“确认并发数”。");
    return;
  }
  const unitIds = Array.from(ui.selectedUnitIds);
  if (!unitIds.length) {
    showToast("请先选择至少一个翻译处理单元。");
    return;
  }
  try {
    await api("/api/pipeline/start", { method: "POST", body: JSON.stringify({ unit_ids: unitIds }) });
    ui.selectedUnitIds.clear();
    showToast("流水线已启动，单元正在并发处理");
    await refresh();
  } catch (error) { showToast(error.message); }
}

async function repairGlyphTranslations() {
  if (ui.glyphRepairBusy) return;
  if (pipelineIsBusy()) {
    showToast("请等待当前流水线结束，再处理这些单元。");
    return;
  }
  if (draftCount() > 0) {
    showToast("请先保存或放弃浏览器中的编辑草稿，再批量重译。");
    return;
  }
  const precheck = ui.outputStatus?.glyph_precheck;
  const candidates = (precheck?.replacement_character_units || [])
    .filter((row) => row.translation_repairable);
  if (!candidates.length) {
    showToast("当前没有可自动重译的单元，请刷新输出检查。");
    return;
  }
  const projectId = projectIdOf(ui.project);
  if (!projectId) {
    showToast("无法确认当前项目，未提交重译请求。");
    return;
  }
  const ids = candidates.map((row) => row.id).filter(Boolean);
  const shownIds = ids.slice(0, 12);
  const omittedIds = ids.length - shownIds.length;
  const confirmed = window.confirm(
    `将对 ${ids.length} 个单元重新调用翻译，并运行独立校验。\n` +
    `这会产生模型调用；每个单元使用现有修正轮数。旧稿按既有规则保留，人工译文与源文含 U+FFFD 的单元不会被覆盖。\n\n` +
    `${shownIds.join("、")}${omittedIds ? `，另有 ${omittedIds} 个单元` : ""}\n\n继续吗？`,
  );
  if (!confirmed) return;

  ui.glyphRepairBusy = true;
  ui.glyphRepairProgress = { queued: 0, total: candidates.length, current_id: "" };
  renderOutputStatus(ui.outputStatus);
  let queued = 0;
  let currentId = "";
  try {
    for (const row of candidates) {
      currentId = String(row.id || "");
      if (projectIdOf(ui.project) !== projectId) {
        throw new Error("当前项目已切换，已停止提交剩余请求。");
      }
      if (!currentId || !row.source_sha256 || !Number.isInteger(row.translation_revision)) {
        throw new Error(`${currentId || "某单元"}缺少版本身份，已停止提交剩余请求。`);
      }
      ui.glyphRepairProgress.current_id = currentId;
      renderOutputStatus(ui.outputStatus);
      await api(`/api/units/${encodeURIComponent(currentId)}/retranslate`, {
        method: "POST",
        body: JSON.stringify({
          expected_project_id: projectId,
          source_sha256: row.source_sha256,
          expected_translation_revision: row.translation_revision,
        }),
      });
      queued++;
      ui.glyphRepairProgress.queued = queued;
      ui.glyphRepairProgress.current_id = "";
      currentId = "";
      renderOutputStatus(ui.outputStatus);
      if (queued === 1) await refresh();
    }
    showToast(`已将 ${queued} 个单元加入现有翻译与校验流程；工作台会持续显示任务进度，完成后再导出 PDF。`);
  } catch (error) {
    const uncertain = currentId ? `第 ${currentId} 个请求结果可能尚未确认` : "后续请求未提交";
    showToast(`${uncertain}；已确认排队 ${queued} 个单元，已停止后续提交。${error.message}`);
  } finally {
    ui.glyphRepairBusy = false;
    ui.glyphRepairProgress = null;
    await refresh().catch(() => {});
    renderOutputStatus(ui.outputStatus);
  }
}

async function confirmConcurrency() {
  if (pipelineIsBusy() || ui.concurrencySaving) return;
  const input = $("concurrencyInput");
  const value = Number(input.value);
  if (!Number.isInteger(value) || value < 1) {
    showToast("并发数必须是大于或等于 1 的整数。");
    input.focus();
    return;
  }
  ui.concurrencySaving = true;
  renderPipelineControls(ui.project);
  try {
    const settings = await api("/api/project/concurrency-settings", {
      method: "PUT",
      body: JSON.stringify({ max_concurrency: value }),
    });
    if (ui.project?.config) ui.project.config.max_concurrency = settings.max_concurrency;
    ui.concurrencyDirty = false;
    showToast(`并发数已确认设置为 ${settings.max_concurrency}`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    ui.concurrencySaving = false;
    renderPipelineControls(ui.project);
  }
}

async function stopPipeline() {
  if (!pipelineIsBusy() || pipelineIsStopping()) return;
  // Update the visible control strip before waiting for the HTTP response.
  // The server response may take as long as the in-flight provider request.
  ui.stopRequested = true;
  renderPipelineControls(ui.project);
  showToast("停止请求已发出，正在停止流水线");
  try {
    const stopped = await api("/api/pipeline/stop", { method: "POST", body: "{}" });
    renderProject(stopped);
    await refresh();
  } catch (error) {
    ui.stopRequested = false;
    renderPipelineControls(ui.project);
    showToast(error.message);
    await refresh();
  }
}

async function importSourceFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const concurrency = Number($("concurrencyInput").value);
  if (!Number.isInteger(concurrency) || concurrency < 1) {
    showToast("并发数必须是大于或等于 1 的整数。");
    $("concurrencyInput").focus();
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("max_concurrency", String(concurrency));
  form.append("source_language", "English");
  form.append("target_language", "简体中文");
  try {
    await api("/api/projects/import", { method: "POST", body: form });
    ui.selectedUnitId = null;
    ui.concurrencyDirty = false;
    showToast(`${file.name} 已导入并完成切分`);
    await refresh();
  } catch (error) { showToast(error.message); }
}

function unitActionPayload(unit) {
  return {
    source_sha256: unit.source_sha256,
    expected_translation_revision: unit.translation_revision,
  };
}

async function saveDraftTranslation(unit) {
  const draft = draftForUnit(unit);
  if (!draft || !isDraftDirty(unit)) return unit;
  const saved = await api(`/api/units/${encodeURIComponent(unit.id)}/translation`, {
    method: "PUT",
    body: JSON.stringify({
      translation: draft.value,
      source_sha256: draft.sourceSha256 || unit.source_sha256,
      expected_translation_revision: draft.baseRevision,
    }),
  });
  removeDraft(unit);
  return saved && saved.id
    ? saved
    : {
        ...unit,
        translation: draft.value,
        translation_revision: Number(unit.translation_revision || 0) + 1,
      };
}

async function handleUnitAction(unit, action) {
  if (ui.inFlightUnitIds.has(unit.id)) return;
  const draft = draftForUnit(unit);
  const dirty = isDraftDirty(unit);
  if (action === "retranslate" && dirty) {
    showToast("请保存编辑内容！");
    return;
  }
  if (action === "review" && unit.status === "needs_action" && !dirty) return;
  if (action === "discard") {
    removeDraft(unit);
    const textarea = $("editedTranslation");
    if (textarea && textarea.dataset.unitId === unit.id) textarea.value = String(unit.translation || "");
    if (ui.project) renderProject(ui.project);
    showToast("已放弃未保存修改");
    return;
  }
  if (action === "save" && !dirty) return;

  if (action === "accept-risk" && dirty) {
    const choice = await openUnitActionDialog({
      title: "当前译文存在未保存修改",
      message: "直接通过必须先明确保存当前编辑内容。是否保存修改并接受校验风险？",
      options: [
        { value: "cancel", label: "取消", className: "secondary" },
        { value: "save-and-accept", label: "保存修改并直接通过", className: "danger" },
      ],
    });
    if (choice !== "save-and-accept") return;
  }

  if (action === "next") {
    const next = nextUnitFor(unit);
    if (!next) return;
    let nextChoice = "continue";
    if (dirty) {
      nextChoice = await openUnitActionDialog({
        title: "当前译文存在未保存修改",
        message: "进入下一条前，请选择如何处理当前编辑内容。",
        options: [
          { value: "cancel", label: "取消", className: "secondary" },
          { value: "discard-and-continue", label: "放弃修改并继续", className: "secondary" },
          { value: "save-and-continue", label: "保存并继续", className: "primary" },
        ],
      });
      if (nextChoice === "cancel") return;
    }
    if (nextChoice === "discard-and-continue") {
      removeDraft(unit);
      ui.selectedUnitId = next.id;
      if (ui.project) renderProject(ui.project);
      return;
    }
    if (nextChoice === "continue") {
      ui.selectedUnitId = next.id;
      if (ui.project) renderProject(ui.project);
      return;
    }
    ui.inFlightUnitIds.add(unit.id);
    updateDecisionControls(unit);
    try {
      await saveDraftTranslation(unit);
      await refresh();
      ui.selectedUnitId = next.id;
      if (ui.project) renderProject(ui.project);
      showToast("已保存并进入下一条");
    } catch (error) {
      showToast(`保存失败，请重试。${error?.message ? ` ${error.message}` : ""}`);
    } finally {
      ui.inFlightUnitIds.delete(unit.id);
      updateDecisionControls(unit);
    }
    return;
  }

  ui.inFlightUnitIds.add(unit.id);
  updateDecisionControls(unit);
  let savedForFollowUp = false;
  try {
    if (action === "save") {
      if (!draft) throw new Error("没有可保存的编辑内容");
      await saveDraftTranslation(unit);
      showToast("人工译文已保存");
    } else if (action === "review") {
      let reviewUnit = unit;
      if (dirty) {
        reviewUnit = await saveDraftTranslation(unit);
        savedForFollowUp = true;
      }
      await api(`/api/units/${encodeURIComponent(unit.id)}/review`, {
        method: "POST",
        body: JSON.stringify(unitActionPayload(reviewUnit)),
      });
      showToast(dirty ? "已保存并提交复检" : "已提交复检");
    } else if (action === "retranslate") {
      await api(`/api/units/${encodeURIComponent(unit.id)}/retranslate`, {
        method: "POST",
        body: JSON.stringify(unitActionPayload(unit)),
      });
      showToast("已重新排队");
    } else if (action === "accept-risk") {
      let decisionUnit = unit;
      if (dirty) {
        decisionUnit = await saveDraftTranslation(unit);
        savedForFollowUp = true;
      }
      await api(`/api/units/${encodeURIComponent(unit.id)}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision: "accept-risk", source_sha256: decisionUnit.source_sha256 || unit.source_sha256 }),
      });
      showToast("已接受风险");
    }
    await refresh();
  } catch (error) {
    if (savedForFollowUp) {
      showToast(`人工译文已保存，但后续操作失败。${error?.message ? ` ${error.message}` : ""}`);
      await refresh().catch(() => {});
    } else {
      showToast(action === "save" ? `保存失败，请重试。${error?.message ? ` ${error.message}` : ""}` : error.message);
    }
  }
  finally {
    ui.inFlightUnitIds.delete(unit.id);
    updateDecisionControls(unit);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const unitActionDialog = $("unitActionDialog");
  if (unitActionDialog) {
    unitActionDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      resolveUnitActionDialog("cancel");
    });
    unitActionDialog.addEventListener("click", (event) => {
      if (event.target === unitActionDialog) resolveUnitActionDialog("cancel");
    });
  }
  $("startButton").addEventListener("click", startPipeline);
  $("stopButton").addEventListener("click", stopPipeline);
  $("confirmConcurrencyButton").addEventListener("click", confirmConcurrency);
  $("concurrencyInput").addEventListener("input", (event) => {
    const confirmed = Number(ui.project?.config?.max_concurrency ?? 3);
    ui.concurrencyDirty = event.target.value.trim() === "" || Number(event.target.value) !== confirmed;
    renderPipelineControls(ui.project);
  });
  $("selectAllButton").addEventListener("click", () => {
    if (pipelineIsBusy()) return;
    (ui.project?.units || []).forEach((unit) => {
      if (["pending", "cancelled"].includes(unit.status)) ui.selectedUnitIds.add(unit.id);
    });
    if (ui.project) renderProject(ui.project);
  });
  $("clearSelectionButton").addEventListener("click", () => {
    ui.selectedUnitIds.clear();
    if (ui.project) renderProject(ui.project);
  });
  $("importButton").addEventListener("click", () => $("sourceFileInput").click());
  $("sourceFileInput").addEventListener("change", importSourceFile);
  $("exportButton").addEventListener("click", exportDocument);
  $("glyphRepairButton").addEventListener("click", repairGlyphTranslations);
  $("outputFormatSelect").addEventListener("change", (event) => {
    ui.outputFormat = event.target.value;
    renderOutputStatus(ui.outputStatus);
  });
  document.querySelectorAll(".filter-tab").forEach((tab) => tab.addEventListener("click", () => {
    ui.filter = tab.dataset.filter;
    if (ui.project) renderProject(ui.project);
  }));
  document.addEventListener("pointerdown", () => {
    ui.pointerInteraction = true;
  }, true);
  const releasePointerInteraction = () => {
    ui.pointerInteraction = false;
    setTimeout(flushDeferredProjectRender, 0);
  };
  document.addEventListener("pointerup", releasePointerInteraction, true);
  document.addEventListener("pointercancel", releasePointerInteraction, true);
  window.addEventListener("blur", releasePointerInteraction);
  document.addEventListener("selectionchange", () => setTimeout(flushDeferredProjectRender, 0));
  document.addEventListener("focusout", () => setTimeout(flushDeferredProjectRender, 0));
  refresh();
});
