const { t } = window.Mode2ApiSettingsI18n;
const $ = (selector, root = document) => root.querySelector(selector);
const serverText = (message) => window.Mode2I18n?.translate?.(message) ?? message;

async function request(path, options = {}) {
  const response = await window.Mode2Request.fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item?.msg || String(item)).join("; ")
      : data.detail;
    throw new Error(detail || "请求失败");
  }
  return data;
}

// ---------------------------------------------------------------------------
// API presets and task assignment
// ---------------------------------------------------------------------------

const GROUPS = ["translation", "review", "concept_create", "concept_verify"];
const TASKS = [
  { id: "unit_translation", group: "translation", works: ["first_translation", "retranslation"] },
  { id: "expression", group: "translation", works: ["expression"] },
  { id: "unit_review", group: "review", works: ["auto_review", "manual_recheck"] },
  { id: "concept_generation", group: "concept_create", works: ["concept_generation"] },
  { id: "concept_check", group: "concept_verify", works: ["candidate_check", "recheck_old", "question_lookup"] },
  { id: "concept_disambiguation", group: "concept_verify", works: ["group_disambiguation", "local_disambiguation"] },
];
const HUE_COUNT = 6;
const PROMPT_TASKS = ["unit_translation", "unit_review"];
const DEFAULT_PROMPT_ID = "default";
const NEW_PRESET_DEFAULTS = {
  name: "", base_url: "", api_key: "", model: "", reasoning_effort: "",
  temperature: 0.7, max_output_tokens: 2000, timeout_seconds: 90,
};

const api = {
  data: null,
  hues: new Map(),
  expanded: new Set(),
  presetsOpen: false,
  globalChoice: "",
  busy: false,
  flash: null,
  ocGoResult: null,
  loadError: null,
};

const presetList = () => api.data?.presets ?? [];
const presetById = (id) => presetList().find((preset) => preset.id === id);
const taskById = (id) => TASKS.find((task) => task.id === id);
const hueOf = (preset) => api.hues.get(preset.id) ?? 0;

function effective(task) {
  const own = api.data.overrides[task.id];
  return own
    ? { preset: presetById(own), source: "override" }
    : { preset: presetById(api.data.groups[task.group]), source: "group" };
}

function references(presetId) {
  return {
    groups: GROUPS.filter((group) => api.data.groups[group] === presetId),
    overrides: TASKS.filter((task) => api.data.overrides[task.id] === presetId).map((task) => task.id),
  };
}

const tasksUsing = (presetId) =>
  TASKS.map((task) => ({ task, ...effective(task) })).filter((entry) => entry.preset?.id === presetId);
const overrideCount = (group) =>
  TASKS.filter((task) => (!group || task.group === group) && api.data.overrides[task.id]).length;

// ---------- prompt presets (a separate library from API presets) ----------
const promptState = (task) => api.data?.prompts?.[task] ?? { selected: DEFAULT_PROMPT_ID, custom: [] };
const promptById = (task, id) => promptState(task).custom.find((preset) => preset.id === id);
const promptName = (task, id) =>
  id === DEFAULT_PROMPT_ID ? t("prompt.builtin") : promptById(task, id)?.name ?? t("prompt.builtin");
const promptText = (task, id) =>
  id === DEFAULT_PROMPT_ID ? promptState(task).builtin?.text ?? "" : promptById(task, id)?.text ?? "";
const promptSelected = (task) => promptName(task, promptState(task).selected);

function assignHues() {
  const ids = new Set(presetList().map((preset) => preset.id));
  [...api.hues.keys()].forEach((id) => { if (!ids.has(id)) api.hues.delete(id); });
  presetList().forEach((preset) => {
    if (api.hues.has(preset.id)) return;
    const counts = Array.from({ length: HUE_COUNT }, (_, hue) => [...api.hues.values()].filter((h) => h === hue).length);
    api.hues.set(preset.id, counts.indexOf(Math.min(...counts)));
  });
}

// ---------- formatting ----------
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

function host(url) {
  try { return new URL(url).host; } catch (_) { return url; }
}

const presetLabel = (preset) =>
  window.Mode2ApiSettingsI18n.isEnglish() ? `${preset.name} (${preset.model})` : `${preset.name}（${preset.model}）`;

const presetOptions = (selected) =>
  presetList()
    .map((preset) => `<option value="${esc(preset.id)}"${preset.id === selected ? " selected" : ""}>${esc(presetLabel(preset))}</option>`)
    .join("");

const swatch = '<span class="swatch" aria-hidden="true"></span>';

// ---------- static text ----------
function renderStatic() {
  document.querySelectorAll("[data-api-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.apiI18n);
  });
  $("#fEffort").placeholder = t("f.effortPh");
  $("#modelList").setAttribute("aria-label", t("f.modelListLabel"));
  $("#ocGoHelp").innerHTML = esc(t("ocgo.help", { code: "%CODE%" })).replace("%CODE%", "<code>x-opencode-session</code>");
  if (!$("#fetchModels").disabled) $("#fetchModels").textContent = t("f.fetch");
  if (!$("#testConnection").disabled) $("#testConnection").textContent = t("t.run");
  setKeyVisible($("#fKey").type === "text");
}

// ---------- main view ----------
function renderGlobal() {
  if (!presetById(api.globalChoice)) api.globalChoice = api.data.groups.translation ?? presetList()[0]?.id ?? "";
  $("#globalPreset").innerHTML = presetOptions(api.globalChoice);
}

function renderGroups() {
  $("#groupGrid").innerHTML = GROUPS.map((group) => {
    const preset = presetById(api.data.groups[group]);
    const count = overrideCount(group);
    const lines = TASKS.filter((task) => task.group === group)
      .map((task) => {
        const own = api.data.overrides[task.id] && presetById(api.data.overrides[task.id]);
        return `<li><span>${esc(t(`task.${task.id}`))}</span><span class="src${own ? " is-override" : ""}">${
          own ? esc(t("group.taskOverride", { name: own.name })) : esc(t("group.taskFollow"))
        }</span></li>`;
      })
      .join("");
    return `
      <article class="group-card hue-${hueOf(preset)}" aria-labelledby="g-${group}-title">
        <h3 id="g-${group}-title">${esc(t(`group.${group}`))}</h3>
        <p class="group-purpose">${esc(t(`groupPurpose.${group}`))}</p>
        <div class="field">
          <label for="g-${group}-select">${esc(t("group.presetLabel"))}</label>
          <select id="g-${group}-select" data-group="${group}" data-fk="group-${group}">${presetOptions(preset.id)}</select>
        </div>
        <p class="model-line">${swatch}<span class="model-name">${esc(preset.model)}</span></p>
        <ul class="task-lines">${lines}</ul>
        <p class="group-foot">
          <span class="override-count${count ? " has" : ""}">${esc(count ? t("group.overrideCount", { count }) : t("group.overrideNone"))}</span>
          ${count ? `<a href="#advanced:${group}" data-fk="adjust-${group}">${esc(t("group.adjust"))}</a>` : ""}
        </p>
      </article>`;
  }).join("");
}

function renderAdvancedEntry() {
  const count = overrideCount();
  $("#advancedEntryMeta").textContent = count ? t("advanced.entryMeta", { count }) : t("advanced.entryMetaNone");
}

function usagePanel(preset) {
  const refs = references(preset.id);
  if (!refs.groups.length && !refs.overrides.length) return `<p>${esc(t("presets.usageEmpty"))}</p>`;
  const list = (title, items) =>
    items.length ? `<div><h4>${esc(title)}</h4><ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : "";
  return (
    list(t("presets.effectiveTasks"), tasksUsing(preset.id).map(({ task, source }) =>
      source === "override"
        ? t("usage.viaOverride", { task: t(`task.${task.id}`) })
        : t("usage.viaGroup", { task: t(`task.${task.id}`), group: t(`group.${task.group}`) }),
    )) +
    list(t("presets.refGroups"), refs.groups.map((group) => t(`group.${group}`))) +
    list(t("presets.refOverrides"), refs.overrides.map((id) => t(`task.${id}`)))
  );
}

function renderPresets() {
  const list = $("#presetList");
  const summary = $("#presetSummary");
  const toggle = $("#presetsToggle");
  const presets = presetList();
  toggle.textContent = api.presetsOpen ? t("presets.collapse") : t("presets.expand", { count: presets.length });
  toggle.setAttribute("aria-expanded", String(api.presetsOpen));
  list.hidden = !api.presetsOpen;
  summary.hidden = api.presetsOpen;
  summary.innerHTML = presets.map((preset) => {
    const count = tasksUsing(preset.id).length;
    return `<li class="preset-chip hue-${hueOf(preset)}">${swatch}<strong>${esc(preset.name)}</strong><span class="model-name">${esc(preset.model)}</span><span class="chip-usage">${esc(count ? t("presets.usedBy", { count }) : t("presets.unused"))}</span></li>`;
  }).join("");
  if (!presets.length) {
    list.innerHTML = `<li class="empty">${esc(t("presets.empty"))}</li>`;
    return;
  }
  list.innerHTML = presets.map((preset) => {
    const count = tasksUsing(preset.id).length;
    const open = api.expanded.has(preset.id);
    const effort = preset.reasoning_effort
      ? t("presets.effort", { value: preset.reasoning_effort })
      : t("presets.effortNone");
    const id = esc(preset.id);
    return `
      <li class="preset-item hue-${hueOf(preset)}${api.flash === preset.id ? " is-flash" : ""}" data-preset="${id}">
        <div class="preset-main">
          ${swatch}
          <div class="preset-id">
            <h3 class="preset-name">${esc(preset.name)}</h3>
            <p class="preset-meta">
              <span class="model-name">${esc(preset.model)}</span>
              <span>${esc(host(preset.base_url))}</span>
              <span>${esc(effort)}</span>
              <span>${esc(t("presets.temp", { value: preset.temperature }))}</span>
              <span>${esc(preset.api_key ? t("presets.hasKey") : t("presets.noKey"))}</span>
            </p>
          </div>
          <span class="usage-badge${count ? "" : " unused"}">${esc(count ? t("presets.usedBy", { count }) : t("presets.unused"))}</span>
        </div>
        <div class="preset-actions">
          <button class="button quiet" type="button" data-act="usage" aria-expanded="${open}" aria-controls="usage-${id}" data-fk="usage-${id}">${esc(open ? t("presets.usageHide") : t("presets.usage"))}</button>
          <button class="button secondary" type="button" data-act="edit" data-fk="edit-${id}">${esc(t("presets.edit"))}</button>
          <button class="button secondary" type="button" data-act="dup" data-fk="dup-${id}">${esc(t("presets.duplicate"))}</button>
          <button class="button danger" type="button" data-act="del" data-fk="del-${id}">${esc(t("presets.delete"))}</button>
        </div>
        <div class="usage-panel" id="usage-${id}"${open ? "" : " hidden"}>${usagePanel(preset)}</div>
      </li>`;
  }).join("");
}

function renderOcGo() {
  const state = api.data.oc_go || {};
  $("#ocGoState").textContent = state.enabled ? t("ocgo.on") : state.partial ? t("ocgo.partial") : t("ocgo.off");
  const button = $("#ocGoToggleButton");
  button.textContent = state.enabled ? t("ocgo.disable") : t("ocgo.enable");
  button.disabled = api.busy;
  const result = $("#ocGoResult");
  const { key, message, tone } = api.ocGoResult || {};
  result.textContent = key ? t(key) : message ? serverText(message) : "";
  result.className = `settings-result${tone ? ` ${tone}` : ""}`;
}

// ---------- advanced view ----------
function renderAdvanced() {
  $("#resetOverrides").disabled = overrideCount() === 0;
  $("#advancedGroups").innerHTML = GROUPS.map((group) => {
    const groupPreset = presetById(api.data.groups[group]);
    const rows = TASKS.filter((task) => task.group === group).map((task) => {
      const own = api.data.overrides[task.id] || "";
      const { preset, source } = effective(task);
      const works = task.works
        .map((work) => `<li><strong>${esc(t(`work.${work}`))}</strong><span>${esc(t(`work.${work}.d`))}</span></li>`)
        .join("");
      return `
        <article class="task-row" aria-labelledby="task-${task.id}-title">
          <div class="task-info">
            <h3 id="task-${task.id}-title">${esc(t(`task.${task.id}`))}</h3>
            <p class="works-label">${esc(t("advanced.covers"))}</p>
            <ul class="work-list">${works}</ul>
          </div>
          <div class="task-control">
            <div class="field">
              <label for="task-${task.id}-select">${esc(t("advanced.select"))}</label>
              <select id="task-${task.id}-select" data-task="${task.id}" data-fk="task-${task.id}">
                <option value=""${own ? "" : " selected"}>${esc(t("advanced.follow", { name: groupPreset.name }))}</option>
                <optgroup label="${esc(t("advanced.specific"))}">${presetOptions(own)}</optgroup>
              </select>
            </div>
            <div class="effective hue-${hueOf(preset)}">
              <span class="eff-label">${esc(t("advanced.effective"))}</span>
              <span class="eff-value">${swatch}<strong>${esc(preset.name)}</strong><span class="model-name">${esc(preset.model)}</span></span>
              <span class="source-tag ${source}">${esc(source === "override" ? t("advanced.sourceOverride") : t("advanced.sourceGroup"))}</span>
            </div>
            ${own ? `<button class="button quiet restore" type="button" data-restore="${task.id}" data-fk="restore-${task.id}">${esc(t("advanced.restore"))}</button>` : ""}
          </div>
        </article>`;
    }).join("");
    return `
      <section class="adv-group" aria-labelledby="ag-${group}">
        <header class="adv-group-head">
          <h2 id="ag-${group}" tabindex="-1">${esc(t(`group.${group}`))}</h2>
          <p class="adv-group-preset hue-${hueOf(groupPreset)}"><span>${esc(t("advanced.groupPreset"))}</span>${swatch}<strong>${esc(groupPreset.name)}</strong></p>
        </header>
        ${rows}
      </section>`;
  }).join("");

  const gen = effective(taskById("concept_generation")).preset.name;
  const check = effective(taskById("concept_check")).preset.name;
  $("#retryList").innerHTML =
    `<li>${esc(t("retry.generation", { gen, check }))}</li><li>${esc(t("retry.check", { check }))}</li>`;
}

// ---------- prompt presets view ----------
function renderPromptSummary() {
  $("#promptSummary").innerHTML = PROMPT_TASKS.map((task) => {
    const active = promptState(task);
    return `
      <li class="prompt-row">
        <div class="prompt-row-info">
          <strong>${esc(t(`task.${task}`))}</strong>
          <span class="prompt-scope">${esc(t(`prompt.scope.${task}`))}</span>
        </div>
        <div class="prompt-row-current">
          <span class="prompt-using">${esc(t("prompt.current"))}</span>
          <span class="prompt-name">${esc(promptSelected(task))}</span>
          <a href="#prompts:${task}" data-fk="prompt-open-${task}">${esc(t("prompt.manage"))}</a>
        </div>
      </li>`;
  }).join("");
}

function promptItem(task, preset, builtin) {
  const active = promptState(task).selected === preset.id;
  const chars = promptText(task, preset.id).length;
  const title = esc(builtin ? t("prompt.builtin") : preset.name);
  const badges = `${builtin ? `<span class="tag">${esc(t("prompt.builtinBadge"))}</span>` : ""}${active ? `<span class="tag in-use">${esc(t("prompt.inUse"))}</span>` : ""}`;
  const useButton = active
    ? ""
    : `<button class="button primary" type="button" data-pact="use" data-fk="puse-${esc(preset.id)}">${esc(builtin ? t("prompt.selectDefault") : t("prompt.select"))}</button>`;
  const actions = builtin
    ? `<button class="button secondary" type="button" data-pact="view" data-fk="pview-${task}">${esc(t("prompt.view"))}</button>
       <button class="button secondary" type="button" data-pact="copy" data-fk="pcopy-${task}-default">${esc(t("prompt.copyBuiltin"))}</button>
       ${useButton}`
    : `<button class="button secondary" type="button" data-pact="edit" data-fk="pedit-${esc(preset.id)}">${esc(t("presets.edit"))}</button>
       <button class="button secondary" type="button" data-pact="copy" data-fk="pcopy-${esc(preset.id)}">${esc(t("presets.duplicate"))}</button>
       ${useButton}
       <button class="button danger" type="button" data-pact="del" data-fk="pdel-${esc(preset.id)}">${esc(t("presets.delete"))}</button>`;
  return `
    <li class="prompt-item${builtin ? " is-builtin" : ""}${active ? " is-active" : ""}" data-ppreset="${esc(preset.id)}">
      <div class="prompt-id">
        <h3 class="prompt-title">${title} ${badges}</h3>
        <p class="prompt-meta">${esc(t("prompt.chars", { count: chars }))}</p>
      </div>
      <div class="preset-actions">${actions}</div>
    </li>`;
}

function renderPrompts() {
  if (!api.data?.prompts) return;
  $("#promptGroups").innerHTML = PROMPT_TASKS.map((task) => {
    const active = promptState(task);
    const items = [
      promptItem(task, { id: DEFAULT_PROMPT_ID }, true),
      ...active.custom.map((preset) => promptItem(task, preset, false)),
    ].join("");
    return `
      <section class="adv-group" aria-labelledby="pg-${task}">
        <header class="adv-group-head">
          <h2 id="pg-${task}" tabindex="-1">${esc(t(`task.${task}`))}</h2>
          <p class="prompt-scope-line">
            <span>${esc(t(`prompt.scope.${task}`))}</span>
            <span class="prompt-now">${esc(t("prompt.current"))}：${esc(promptSelected(task))}</span>
          </p>
        </header>
        <ul class="prompt-list">${items}</ul>
        <div class="prompt-group-foot">
          <button class="button secondary" type="button" data-pcreate="${task}" data-fk="pcreate-${task}">${esc(t("prompt.create"))}</button>
        </div>
      </section>`;
  }).join("");
}

// ---------- prompt preset dialog ----------
const promptEditor = { mode: "view", task: null, id: null, returnKey: null, saving: false };

function updatePromptDialogChrome() {
  const task = promptEditor.task;
  const view = promptEditor.mode === "view";
  const name = promptEditor.mode === "edit" ? promptName(task, promptEditor.id) : "";
  $("#promptDialogTitle").textContent = view
    ? t("pe.viewTitle", { task: t(`task.${task}`) })
    : promptEditor.mode === "edit"
      ? t("pe.editTitle", { task: t(`task.${task}`), name })
      : t("pe.createTitle", { task: t(`task.${task}`) });
  $("#promptDialogScope").textContent = t(`prompt.scope.${task}`);
  $("#pfName").readOnly = view;
  $("#pfText").readOnly = view;
  if (view) $("#pfName").value = t("prompt.builtin");
  $("#pfWarning").textContent = t(`pe.warning.${task}`);
  $("#pfBuiltinNote").textContent = view ? t("pe.builtinNote") : "";
  $("#promptCancel").textContent = view ? t("pe.close") : t("common.cancel");
  $("#promptCopy").hidden = !view;
  $("#promptSubmit").hidden = view;
  if (!view) $("#promptSubmit").textContent = promptEditor.mode === "create" ? t("pe.create") : t("pe.save");
}

function openPromptDialog(mode, task, seed, returnKey) {
  // seed: a stored preset for view/edit; {name, text} starter values for create.
  promptEditor.mode = mode;
  promptEditor.task = task;
  promptEditor.id = mode === "edit" ? seed?.id ?? null : null;
  promptEditor.returnKey = returnKey;
  promptEditor.saving = false;
  $("#peName").textContent = "";
  $("#peText").textContent = "";
  $("#promptError").textContent = "";
  $("#pfName").removeAttribute("aria-invalid");
  $("#pfText").removeAttribute("aria-invalid");
  $("#pfName").value = mode === "view" ? "" : (seed?.name ?? "");
  $("#pfText").value = seed ? (seed.text ?? promptText(task, seed.id)) : "";
  $("#pfCount").textContent = t("prompt.chars", { count: $("#pfText").value.length });
  $("#promptSubmit").disabled = false;
  updatePromptDialogChrome();
  hideToast();
  // The dialog may already be open when "copy" morphs view mode into create.
  if (!$("#promptDialog").open) $("#promptDialog").showModal();
  (mode === "view" ? $("#promptCopy") : $("#pfName")).focus();
}

function validatePromptEditor() {
  const name = $("#pfName").value.trim();
  const text = $("#pfText").value.trim();
  const duplicate = promptState(promptEditor.task).custom.some(
    (preset) => preset.id !== promptEditor.id && preset.name.toLocaleLowerCase() === name.toLocaleLowerCase(),
  );
  const errors = {};
  if (!name) errors.name = "err.nameRequired";
  else if (duplicate) errors.name = "err.promptNameDuplicate";
  if (!text) errors.text = "err.promptTextRequired";
  return { errors, name, text };
}

async function submitPromptEditor(event) {
  event.preventDefault();
  if (promptEditor.saving || promptEditor.mode === "view") return;
  const { errors, name, text } = validatePromptEditor();
  if (Object.keys(errors).length) {
    ["name", "text"].forEach((field) => {
      const message = errors[field] ? t(errors[field]) : "";
      $(`#pe${field === "name" ? "Name" : "Text"}`).textContent = message;
      if (message) $(`#pf${field === "name" ? "Name" : "Text"}`).setAttribute("aria-invalid", "true");
      else $(`#pf${field === "name" ? "Name" : "Text"}`).removeAttribute("aria-invalid");
    });
    if (errors.name) $("#pfName").focus(); else $("#pfText").focus();
    return;
  }
  const task = promptEditor.task;
  const creating = promptEditor.mode === "create";
  const submit = $("#promptSubmit");
  promptEditor.saving = true;
  submit.disabled = true;
  submit.textContent = t("pe.saving");
  $("#promptError").textContent = "";
  try {
    const snapshot = await request(
      creating ? `/api/settings/prompts/${task}` : `/api/settings/prompts/${task}/${encodeURIComponent(promptEditor.id)}`,
      { method: creating ? "POST" : "PUT", body: JSON.stringify({ name, text }) },
    );
    applySnapshot(snapshot);
    const preset = creating
      ? promptById(task, snapshot.prompt_id)
      : promptById(task, promptEditor.id);
    toast(t(creating ? "prompt.created" : promptState(task).selected === preset.id ? "prompt.updatedActive" : "prompt.updated", { name: preset.name, task: t(`task.${task}`) }));
    promptEditor.returnKey = `pedit-${preset.id}`;
    $("#promptDialog").close("saved");
  } catch (error) {
    $("#promptError").textContent = t("error.save", { message: serverText(error.message) });
  } finally {
    promptEditor.saving = false;
    submit.disabled = false;
    submit.textContent = promptEditor.mode === "create" ? t("pe.create") : t("pe.save");
  }
}

function uniquePromptName(task, base) {
  const taken = new Set(promptState(task).custom.map((preset) => preset.name.toLocaleLowerCase()));
  const clip = (value) => value.slice(0, 40);
  if (!taken.has(clip(base).toLocaleLowerCase())) return clip(base);
  let index = 2;
  while (taken.has(clip(`${base} ${index}`).toLocaleLowerCase())) index += 1;
  return clip(`${base} ${index}`);
}

function copyPromptPreset(task, preset) {
  const base = preset.id === DEFAULT_PROMPT_ID ? t("prompt.builtin") : preset.name;
  const name = uniquePromptName(task, `${base} ${t("presets.copySuffix")}`);
  openPromptDialog("create", task, { name, text: promptText(task, preset.id) }, `pcopy-${preset.id}`);
}

async function deletePromptPreset(task, preset) {
  if (promptState(task).selected === preset.id) {
    await ask({
      title: t("prompt.blockedTitle", { name: preset.name }),
      paragraphs: [t("prompt.blockedBody", { task: t(`task.${task}`) })],
      ok: t("prompt.blockedOk"),
      info: true,
    });
    render(`pdel-${preset.id}`);
    return;
  }
  const confirmed = await ask({
    title: t("prompt.deleteTitle", { name: preset.name }),
    paragraphs: [t("prompt.deleteBody")],
    ok: t("prompt.deleteOk"),
    danger: true,
  });
  if (!confirmed) {
    render(`pdel-${preset.id}`);
    return;
  }
  const snapshot = await mutate(`/api/settings/prompts/${task}/${encodeURIComponent(preset.id)}`, "DELETE", undefined, `pdel-${preset.id}`);
  if (!snapshot) return;
  const remaining = promptState(task).custom;
  const neighbour = remaining[0];
  render(neighbour ? `pedit-${neighbour.id}` : `pcreate-${task}`);
  toast(t("prompt.deleted", { name: preset.name }));
}

function render(focusKey) {
  if (!api.data) return;
  const key = focusKey ?? document.activeElement?.dataset?.fk;
  renderGlobal();
  renderGroups();
  renderAdvancedEntry();
  renderPresets();
  renderOcGo();
  renderAdvanced();
  renderPromptSummary();
  renderPrompts();
  document.querySelector("#settingsMain").setAttribute("aria-busy", String(api.busy));
  api.flash = null;
  if (key) {
    const target = document.querySelector(`[data-fk="${CSS.escape(key)}"]`);
    if (target && target !== document.activeElement && !target.closest("[hidden]")) target.focus({ preventScroll: !focusKey });
  }
}

// ---------- toast ----------
let toastTimer = 0;
function toast(message, error = false) {
  const element = $("#apiToast");
  element.textContent = message;
  element.classList.toggle("is-error", error);
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), error ? 7000 : 4200);
}
function hideToast() {
  clearTimeout(toastTimer);
  $("#apiToast").classList.remove("visible");
  $("#apiToast").textContent = "";
}

// ---------- server calls: every change is saved immediately ----------
function applySnapshot(snapshot) {
  api.data = snapshot;
  assignHues();
}

async function mutate(path, method, body, focusKey) {
  if (api.busy) {
    toast(t("error.busy"), true);
    render(focusKey);
    return null;
  }
  api.busy = true;
  try {
    const snapshot = await request(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
    applySnapshot(snapshot);
    return snapshot;
  } catch (error) {
    toast(t("error.save", { message: serverText(error.message) }), true);
    return null;
  } finally {
    api.busy = false;
    render(focusKey);
  }
}

async function loadApiSettings() {
  try {
    applySnapshot(await request("/api/settings"));
    api.loadError = null;
    $("#apiLoadError").hidden = true;
    $("#apiContent").hidden = false;
    $("#globalForm").hidden = false;
    document.querySelector("#connectionDot").classList.remove("offline");
    document.querySelector("#connectionText").textContent = "本地服务已连接";
    render();
    route();
  } catch (error) {
    $("#apiContent").hidden = true;
    $("#globalForm").hidden = true;
    $("#apiLoadError").hidden = false;
    api.loadError = error.message;
    $("#apiLoadErrorText").textContent = t("load.error", { message: serverText(error.message) });
    document.querySelector("#connectionDot").classList.add("offline");
    document.querySelector("#connectionText").textContent = "服务不可用";
  }
}

// ---------- confirm / info dialog ----------
function ask({ title, paragraphs = [], items = [], ok, cancel = t("common.cancel"), danger = false, info = false }) {
  const dialog = $("#confirmDialog");
  $("#confirmTitle").textContent = title;
  const body = $("#confirmBody");
  body.replaceChildren(...paragraphs.map((text) => Object.assign(document.createElement("p"), { textContent: text })));
  if (items.length) {
    const list = document.createElement("ul");
    items.forEach((text) => list.append(Object.assign(document.createElement("li"), { textContent: text })));
    body.append(list);
  }
  const okButton = $("#confirmOk");
  const cancelButton = $("#confirmCancel");
  okButton.textContent = ok;
  okButton.className = `button ${danger ? "danger-solid" : "primary"}`;
  cancelButton.textContent = cancel;
  cancelButton.hidden = info;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    const finish = () => {
      okButton.onclick = cancelButton.onclick = null;
      resolve(dialog.returnValue === "ok");
    };
    okButton.onclick = () => dialog.close("ok");
    cancelButton.onclick = () => dialog.close("cancel");
    dialog.addEventListener("close", finish, { once: true });
    hideToast();
    dialog.showModal();
    (info ? okButton : cancelButton).focus();
  });
}

// ---------- routing between main and advanced ----------
let currentView = "main";
function route() {
  if (!api.data) return;
  const hash = location.hash;
  const next = hash.startsWith("#advanced") ? "advanced" : hash.startsWith("#prompts") ? "prompts" : "main";
  const previous = currentView;
  currentView = next;
  $("#viewMain").hidden = next !== "main";
  $("#viewAdvanced").hidden = next !== "advanced";
  $("#viewPrompts").hidden = next !== "prompts";
  if (next === "advanced") {
    const group = hash.split(":")[1];
    const heading = (group && document.getElementById(`ag-${group}`)) || $("#advancedTitle");
    window.scrollTo(0, 0);
    heading.focus({ preventScroll: !group });
    if (group) heading.scrollIntoView({ block: "start" });
  } else if (next === "prompts") {
    const task = hash.split(":")[1];
    const heading = (task && document.getElementById(`pg-${task}`)) || $("#promptsViewTitle");
    window.scrollTo(0, 0);
    heading.focus({ preventScroll: !task });
    if (task) heading.scrollIntoView({ block: "start" });
  } else if (previous !== "main") {
    window.scrollTo(0, 0);
    const entry = previous === "prompts" ? $(`[data-fk="prompts-open"]`) : $("#advancedEntry");
    entry.focus({ preventScroll: true });
    entry.scrollIntoView({ block: "center" });
  }
}

// ---------- preset editor ----------
const editor = { dialog: null, mode: "create", id: null, returnKey: null, runToken: 0, saving: false };
const fields = {
  name: "#fName",
  base_url: "#fBase",
  api_key: "#fKey",
  model: "#fModel",
  reasoning_effort: "#fEffort",
  temperature: "#fTemp",
  max_output_tokens: "#fTokens",
  timeout_seconds: "#fTimeout",
};
const errorTargets = {
  name: "#eName",
  base_url: "#eBase",
  model: "#eModel",
  temperature: "#eTemp",
  max_output_tokens: "#eTokens",
  timeout_seconds: "#eTimeout",
};

function setKeyVisible(visible) {
  const button = $("#toggleKey");
  $("#fKey").type = visible ? "text" : "password";
  button.setAttribute("aria-pressed", String(visible));
  button.textContent = t(visible ? "f.keyHide" : "f.keyShow");
  button.setAttribute("aria-label", t(visible ? "f.keyHideLabel" : "f.keyShowLabel"));
}

function clearErrors() {
  Object.entries(errorTargets).forEach(([name, selector]) => {
    $(selector).textContent = "";
    const input = $(fields[name]);
    input.removeAttribute("aria-invalid");
    input.setAttribute("aria-describedby", selector.slice(1));
  });
  $("#editorError").textContent = "";
}

function setModelHint(text, tone = "") {
  const hint = $("#modelHint");
  hint.textContent = text;
  hint.className = `field-hint${tone ? ` ${tone}` : ""}`;
}

function openEditor(mode, preset, returnKey) {
  editor.mode = mode;
  editor.id = preset?.id ?? null;
  editor.returnKey = returnKey;
  editor.runToken += 1;
  editor.saving = false;
  const values = preset ?? NEW_PRESET_DEFAULTS;
  Object.entries(fields).forEach(([name, selector]) => { $(selector).value = values[name] ?? ""; });
  setKeyVisible(false);
  clearErrors();
  const list = $("#modelList");
  list.hidden = true;
  list.replaceChildren();
  setModelHint(t("f.fetchHint"));
  $("#fetchModels").disabled = false;
  $("#fetchModels").textContent = t("f.fetch");
  $("#testConnection").disabled = false;
  $("#testConnection").textContent = t("t.run");
  $("#testResult").textContent = "";
  $("#testResult").className = "test-result";
  $("#moreParams").open = false;
  $("#editorTitle").textContent = mode === "create" ? t("editor.createTitle") : t("editor.editTitle", { name: preset.name });
  const count = preset ? tasksUsing(preset.id).length : 0;
  $("#editorUsage").textContent = mode === "create" ? t("editor.usageNew") : count ? t("editor.usage", { count }) : t("editor.usageNone");
  $("#editorSubmit").disabled = false;
  $("#editorSubmit").textContent = mode === "create" ? t("editor.create") : t("editor.update");
  hideToast();
  editor.dialog.showModal();
  $("#fName").focus();
}

function readEditor() {
  const raw = Object.fromEntries(Object.entries(fields).map(([name, selector]) => [name, $(selector).value.trim()]));
  const number = (value) => (value === "" ? NaN : Number(value));
  return {
    ...raw,
    temperature: number(raw.temperature),
    max_output_tokens: number(raw.max_output_tokens),
    timeout_seconds: number(raw.timeout_seconds),
  };
}

function baseUrlError(value) {
  if (!value) return "err.baseRequired";
  if (!/^https?:\/\/\S+$/i.test(value)) return "err.baseInvalid";
  try { new URL(value); } catch (_) { return "err.baseInvalid"; }
  return null;
}

function validate(values, only) {
  const errors = {};
  const check = (name, key) => { if (key && (!only || only.includes(name))) errors[name] = key; };
  const duplicate = presetList().some(
    (preset) => preset.id !== editor.id && preset.name.toLocaleLowerCase() === values.name.toLocaleLowerCase(),
  );
  check("name", !values.name ? "err.nameRequired" : duplicate ? "err.nameDuplicate" : null);
  check("base_url", baseUrlError(values.base_url));
  check("model", values.model ? null : "err.modelRequired");
  check("temperature", Number.isFinite(values.temperature) && values.temperature >= 0 && values.temperature <= 2 ? null : "err.temp");
  check("max_output_tokens", Number.isInteger(values.max_output_tokens) && values.max_output_tokens >= 1 && values.max_output_tokens <= 1000000 ? null : "err.tokens");
  check("timeout_seconds", Number.isFinite(values.timeout_seconds) && values.timeout_seconds >= 1 && values.timeout_seconds <= 600 ? null : "err.timeout");
  return errors;
}

function showErrors(errors) {
  clearErrors();
  const names = Object.keys(errors);
  names.forEach((name) => {
    $(errorTargets[name]).textContent = t(errors[name]);
    $(fields[name]).setAttribute("aria-invalid", "true");
  });
  if (!names.length) return;
  if (names.some((name) => name === "max_output_tokens" || name === "timeout_seconds")) $("#moreParams").open = true;
  $(fields[names[0]]).focus();
}

const configPayload = ({ name, ...config }) => config;

async function submitEditor(event) {
  event.preventDefault();
  if (editor.saving) return;
  const values = readEditor();
  const errors = validate(values);
  if (Object.keys(errors).length) {
    showErrors(errors);
    return;
  }
  const submit = $("#editorSubmit");
  editor.saving = true;
  submit.disabled = true;
  submit.textContent = t("editor.saving");
  $("#editorError").textContent = "";
  try {
    const creating = editor.mode === "create";
    const snapshot = await request(creating ? "/api/settings/presets" : `/api/settings/presets/${encodeURIComponent(editor.id)}`, {
      method: creating ? "POST" : "PUT",
      body: JSON.stringify(values),
    });
    applySnapshot(snapshot);
    const preset = presetById(snapshot.preset_id);
    if (creating) {
      api.presetsOpen = true;
      toast(t("presets.created", { name: preset.name }));
    } else {
      const count = tasksUsing(preset.id).length;
      toast(t(count ? "presets.updated" : "presets.updatedUnused", { name: preset.name, count }));
    }
    editor.returnKey = `edit-${preset.id}`;
    api.flash = preset.id;
    editor.dialog.close("saved");
  } catch (error) {
    $("#editorError").textContent = t("error.save", { message: serverText(error.message) });
  } finally {
    editor.saving = false;
    submit.disabled = false;
    submit.textContent = editor.mode === "create" ? t("editor.create") : t("editor.update");
  }
}

async function runConnectionTest() {
  const values = readEditor();
  const errors = validate(values, ["base_url", "model", "temperature", "max_output_tokens", "timeout_seconds"]);
  const result = $("#testResult");
  if (Object.keys(errors).length) {
    showErrors(errors);
    result.textContent = t("err.testNeeds");
    result.className = "test-result error";
    return;
  }
  clearErrors();
  const token = ++editor.runToken;
  const button = $("#testConnection");
  button.disabled = true;
  button.textContent = t("t.running");
  result.textContent = t("t.running");
  result.className = "test-result";
  try {
    const data = await request("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({ ...configPayload(values), preset_id: editor.id }),
    });
    if (token !== editor.runToken) return;
    result.textContent = t("t.ok", { model: data.model });
    result.className = "test-result success";
  } catch (error) {
    if (token !== editor.runToken) return;
    result.textContent = t("t.fail", { message: serverText(error.message) });
    result.className = "test-result error";
  } finally {
    button.disabled = false;
    button.textContent = t("t.run");
  }
}

async function loadModels() {
  const values = readEditor();
  const errors = validate(values, ["base_url", "temperature", "max_output_tokens", "timeout_seconds"]);
  if (Object.keys(errors).length) {
    showErrors(errors);
    setModelHint(t("err.fetchNeeds"), "error");
    return;
  }
  clearErrors();
  const token = ++editor.runToken;
  const button = $("#fetchModels");
  button.disabled = true;
  button.textContent = t("f.fetching");
  try {
    const data = await request("/api/settings/models", {
      method: "POST",
      body: JSON.stringify({ ...configPayload(values), preset_id: editor.id }),
    });
    if (token !== editor.runToken) return;
    const models = Array.isArray(data.models) ? data.models : [];
    if (!models.length) {
      setModelHint(t("f.fetchEmpty"), "error");
      return;
    }
    const list = $("#modelList");
    list.replaceChildren(new Option(t("f.modelListPh"), ""), ...models.map((model) => new Option(model, model)));
    list.value = models.includes(values.model) ? values.model : "";
    list.hidden = false;
    setModelHint(t("f.fetchDone", { count: models.length }), "success");
    list.focus();
  } catch (error) {
    if (token !== editor.runToken) return;
    setModelHint(t("f.fetchFail", { message: serverText(error.message) }), "error");
  } finally {
    button.disabled = false;
    button.textContent = t("f.fetch");
  }
}

// ---------- preset actions ----------
function uniqueName(base) {
  const taken = new Set(presetList().map((preset) => preset.name.toLocaleLowerCase()));
  const clip = (text) => text.slice(0, 40);
  if (!taken.has(clip(base).toLocaleLowerCase())) return clip(base);
  let index = 2;
  while (taken.has(clip(`${base} ${index}`).toLocaleLowerCase())) index += 1;
  return clip(`${base} ${index}`);
}

async function duplicatePreset(preset) {
  const { id, oc_go_compatibility, opencode_session_id, ...values } = preset;
  const name = uniqueName(`${preset.name} ${t("presets.copySuffix")}`);
  const snapshot = await mutate("/api/settings/presets", "POST", { ...values, name }, `dup-${preset.id}`);
  if (!snapshot) return;
  api.flash = snapshot.preset_id;
  render(`edit-${snapshot.preset_id}`);
  toast(t("presets.duplicated", { name }));
}

async function deletePreset(preset) {
  const returnKey = `del-${preset.id}`;
  const refs = references(preset.id);
  if (refs.groups.length || refs.overrides.length) {
    await ask({
      title: t("presets.blockedTitle", { name: preset.name }),
      paragraphs: [t("presets.blockedBody")],
      items: [
        ...refs.groups.map((group) => t("presets.blockedGroup", { name: t(`group.${group}`) })),
        ...refs.overrides.map((id) => t("presets.blockedTask", { name: t(`task.${id}`) })),
      ],
      ok: t("presets.blockedOk"),
      info: true,
    });
    render(returnKey);
    return;
  }
  const confirmed = await ask({
    title: t("presets.deleteTitle", { name: preset.name }),
    paragraphs: [t("presets.deleteBody")],
    ok: t("presets.deleteOk"),
    danger: true,
  });
  if (!confirmed) {
    render(returnKey);
    return;
  }
  const index = presetList().indexOf(preset);
  const snapshot = await mutate(`/api/settings/presets/${encodeURIComponent(preset.id)}`, "DELETE", undefined, returnKey);
  if (!snapshot) return;
  api.expanded.delete(preset.id);
  const neighbour = snapshot.presets[Math.min(index, snapshot.presets.length - 1)];
  render(neighbour ? `edit-${neighbour.id}` : "create-preset");
  toast(t("presets.deleted", { name: preset.name }));
}

async function applyGlobal(event) {
  event.preventDefault();
  const preset = presetById($("#globalPreset").value);
  if (!preset) return;
  api.globalChoice = preset.id;
  const count = overrideCount();
  const confirmed = await ask({
    title: t("global.confirmTitle", { name: preset.name }),
    paragraphs: [t(count ? "global.confirmBody" : "global.confirmBodyNone", { name: preset.name, count }), t("global.confirmNote")],
    ok: t("global.confirmOk"),
  });
  if (!confirmed) {
    render("global-apply");
    return;
  }
  if (await mutate("/api/settings/apply-all", "POST", { preset_id: preset.id }, "global-apply")) {
    toast(t("global.done", { name: preset.name }));
  }
}

async function toggleOcGo() {
  if (api.busy) return;
  const enabled = !api.data.oc_go?.enabled;
  api.ocGoResult = { key: enabled ? "ocgo.enabling" : "ocgo.disabling" };
  api.busy = true;
  render();
  try {
    const data = await request("/api/settings/oc-go", { method: "PUT", body: JSON.stringify({ enabled }) });
    applySnapshot(data.settings);
    api.ocGoResult = { key: enabled ? "ocgo.enabled" : "ocgo.disabled", tone: "success" };
  } catch (error) {
    api.ocGoResult = { message: error.message, tone: "error" };
  } finally {
    api.busy = false;
    render();
    $("#ocGoToggleButton").focus();
  }
}

function bindApiSettings() {
  editor.dialog = $("#editorDialog");
  $("#globalForm").hidden = true;

  $("#globalForm").addEventListener("submit", applyGlobal);
  $("#globalPreset").addEventListener("change", (event) => { api.globalChoice = event.target.value; });
  $("#apiReload").addEventListener("click", loadApiSettings);

  $("#groupGrid").addEventListener("change", async (event) => {
    const group = event.target.dataset.group;
    if (!group) return;
    const snapshot = await mutate(`/api/settings/groups/${group}`, "PUT", { preset_id: event.target.value }, `group-${group}`);
    if (snapshot) toast(t("group.saved", { group: t(`group.${group}`), name: presetById(snapshot.groups[group]).name }));
  });

  $("#advancedGroups").addEventListener("change", async (event) => {
    const id = event.target.dataset.task;
    if (!id) return;
    const value = event.target.value || null;
    const snapshot = await mutate(`/api/settings/tasks/${id}`, "PUT", { preset_id: value }, `task-${id}`);
    if (snapshot) {
      toast(t(value ? "advanced.overridden" : "advanced.restored", { task: t(`task.${id}`), name: effective(taskById(id)).preset.name }));
    }
  });

  $("#advancedGroups").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-restore]");
    if (!button) return;
    const id = button.dataset.restore;
    const snapshot = await mutate(`/api/settings/tasks/${id}`, "PUT", { preset_id: null }, `task-${id}`);
    if (snapshot) toast(t("advanced.restored", { task: t(`task.${id}`), name: effective(taskById(id)).preset.name }));
  });

  $("#resetOverrides").addEventListener("click", async () => {
    if (await mutate("/api/settings/tasks", "DELETE", undefined, "back-link")) {
      $("#advancedTitle").focus();
      toast(t("advanced.resetDone"));
    }
  });

  $("#backLink").addEventListener("click", (event) => {
    event.preventDefault();
    history.pushState(null, "", location.pathname + location.search);
    route();
  });
  $("#promptsBack").addEventListener("click", (event) => {
    event.preventDefault();
    history.pushState(null, "", location.pathname + location.search);
    route();
  });

  // Prompt presets: same immediate-save convention as API presets.
  $("#promptGroups").addEventListener("click", async (event) => {
    const create = event.target.closest("[data-pcreate]");
    if (create) {
      openPromptDialog("create", create.dataset.pcreate, null, `pcreate-${create.dataset.pcreate}`);
      return;
    }
    const button = event.target.closest("[data-pact]");
    if (!button) return;
    const groupEl = button.closest("[aria-labelledby]");
    const task = groupEl?.getAttribute("aria-labelledby")?.replace("pg-", "");
    const id = button.closest("[data-ppreset]")?.dataset.ppreset;
    const preset = id === DEFAULT_PROMPT_ID ? { id: DEFAULT_PROMPT_ID } : promptById(task, id);
    if (!task || !preset) return;
    const act = button.dataset.pact;
    if (act === "view") openPromptDialog("view", task, { id: DEFAULT_PROMPT_ID }, `pview-${task}`);
    else if (act === "edit") openPromptDialog("edit", task, preset, `pedit-${id}`);
    else if (act === "copy") copyPromptPreset(task, preset);
    else if (act === "use") {
      const snapshot = await mutate(
        `/api/settings/prompts/${task}/selection`, "PUT", { preset_id: id }, `puse-${id}`,
      );
      if (snapshot) toast(t("prompt.selected", { task: t(`task.${task}`), name: promptName(task, id) }));
    } else if (act === "del") deletePromptPreset(task, preset);
  });

  $("#promptForm").addEventListener("submit", submitPromptEditor);
  $("#promptCancel").addEventListener("click", () => $("#promptDialog").close("cancel"));
  $("#promptCopy").addEventListener("click", () => {
    copyPromptPreset(promptEditor.task, { id: DEFAULT_PROMPT_ID });
  });
  $("#promptDialog").addEventListener("cancel", (event) => { if (promptEditor.saving) event.preventDefault(); });
  $("#promptDialog").addEventListener("close", () => {
    render(promptEditor.returnKey);
  });
  $("#pfText").addEventListener("input", (event) => {
    $("#pfCount").textContent = t("prompt.chars", { count: event.target.value.length });
    $("#peText").textContent = "";
    event.target.removeAttribute("aria-invalid");
  });
  $("#pfName").addEventListener("input", (event) => {
    $("#peName").textContent = "";
    event.target.removeAttribute("aria-invalid");
  });

  $("#createPreset").addEventListener("click", () => openEditor("create", null, "create-preset"));
  $("#presetsToggle").addEventListener("click", () => {
    api.presetsOpen = !api.presetsOpen;
    render("presets-toggle");
  });

  $("#presetList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const preset = presetById(button.closest("[data-preset]").dataset.preset);
    if (!preset) return;
    const act = button.dataset.act;
    if (act === "usage") {
      if (api.expanded.has(preset.id)) api.expanded.delete(preset.id);
      else api.expanded.add(preset.id);
      render(`usage-${preset.id}`);
    } else if (act === "edit") openEditor("edit", preset, `edit-${preset.id}`);
    else if (act === "dup") duplicatePreset(preset);
    else if (act === "del") deletePreset(preset);
  });

  $("#editorForm").addEventListener("submit", submitEditor);
  $("#editorCancel").addEventListener("click", () => editor.dialog.close("cancel"));
  editor.dialog.addEventListener("cancel", (event) => { if (editor.saving) event.preventDefault(); });
  editor.dialog.addEventListener("close", () => {
    editor.runToken += 1;
    render(editor.returnKey);
  });
  Object.entries(errorTargets).forEach(([name, selector]) => {
    $(fields[name]).addEventListener("input", (event) => {
      $(selector).textContent = "";
      event.target.removeAttribute("aria-invalid");
    });
  });
  $("#toggleKey").addEventListener("click", () => setKeyVisible($("#fKey").type === "password"));
  $("#fetchModels").addEventListener("click", loadModels);
  $("#testConnection").addEventListener("click", runConnectionTest);
  $("#modelList").addEventListener("change", (event) => {
    if (event.target.value) $("#fModel").value = event.target.value;
  });
  $("#ocGoToggleButton").addEventListener("click", toggleOcGo);

  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
  window.addEventListener("mode2:localechange", () => {
    hideToast();
    renderStatic();
    if (api.loadError) $("#apiLoadErrorText").textContent = t("load.error", { message: serverText(api.loadError) });
    render();
  });
  renderStatic();
}

// ---------------------------------------------------------------------------
// Segmentation and translation-context settings (unchanged behaviour)
// ---------------------------------------------------------------------------

function showSegmentationResult(message, success = false) {
  const result = document.querySelector("#segmentationResult");
  result.textContent = message;
  result.className = `settings-result ${success ? "success" : "error"}`;
}

function showTranslationContextResult(message, success = false) {
  const result = document.querySelector("#translationContextResult");
  result.textContent = message;
  result.className = `settings-result ${success ? "success" : "error"}`;
}

function readTargetWords() {
  const raw = document.querySelector("#targetSegmentWords").value.trim();
  if (!/^[1-9]\d*$/.test(raw)) throw new Error("目标英文词数必须是大于 0 的整数。");
  const value = Number(raw);
  if (value > 100000) throw new Error("目标英文词数不能超过 100000。");
  return value;
}

async function loadSegmentationSettings() {
  try {
    const data = await request("/api/project/segmentation-settings");
    document.querySelector("#targetSegmentWords").value = data.target_words;
    document.querySelector("#segmentationProject").textContent = data.project_name || "当前项目";
    showSegmentationResult(`已加载当前项目设置，默认目标为 ${data.default_target_words} 个英文词。`, true);
  } catch (error) {
    document.querySelector("#segmentationProject").textContent = "未选择项目";
    showSegmentationResult(error.message);
  }
}

async function saveSegmentationSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const targetWords = readTargetWords();
    const data = await request("/api/project/segmentation-settings", {
      method: "PUT",
      body: JSON.stringify({ target_words: targetWords }),
    });
    document.querySelector("#targetSegmentWords").value = data.target_words;
    showSegmentationResult("目标英文词数已保存；完整句子优先，将在下次导入或重新切分时生效。", true);
  } catch (error) {
    showSegmentationResult(error.message);
  } finally {
    button.disabled = false;
  }
}

function readContextWords(id, label) {
  const input = document.querySelector(`#${id}`);
  const raw = input.value.trim();
  if (!/^\d+$/.test(raw)) throw new Error(`${label}必须是非负整数。`);
  const value = Number(raw);
  const maximum = Number(input.max || 4000);
  if (value > maximum) throw new Error(`${label}不能超过 ${maximum} 个词。`);
  return value;
}

async function loadTranslationContextSettings() {
  try {
    const data = await request("/api/project/translation-context-settings");
    document.querySelector("#previousContextWords").value = data.previous_context_words;
    document.querySelector("#nextContextWords").value = data.next_context_words;
    document.querySelector("#translationContextProject").textContent = data.project_name || "当前项目";
    showTranslationContextResult(
      `已加载；默认每侧为 ${data.default_previous_context_words} 个英文词。`,
      true,
    );
  } catch (error) {
    document.querySelector("#translationContextProject").textContent = "未选择项目";
    showTranslationContextResult(error.message);
  }
}

async function saveTranslationContextSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const previousWords = readContextWords("previousContextWords", "前文参考词数");
    const nextWords = readContextWords("nextContextWords", "后文参考词数");
    const data = await request("/api/project/translation-context-settings", {
      method: "PUT",
      body: JSON.stringify({
        previous_context_words: previousWords,
        next_context_words: nextWords,
      }),
    });
    document.querySelector("#previousContextWords").value = data.previous_context_words;
    document.querySelector("#nextContextWords").value = data.next_context_words;
    showTranslationContextResult("翻译前后文设置已保存，将从下一次翻译请求开始生效。", true);
  } catch (error) {
    showTranslationContextResult(error.message);
  } finally {
    button.disabled = false;
  }
}

async function resegmentSource() {
  const confirmed = window.confirm(
    "重新切分会清除当前项目的译文、校验结果和导出文件。系统会先保存一份项目状态备份。是否继续？",
  );
  if (!confirmed) return;
  const button = document.querySelector("#resegmentSourceButton");
  button.disabled = true;
  try {
    const data = await request("/api/project/resegment", {
      method: "POST",
      body: JSON.stringify({ confirm_reset: true }),
    });
    const info = data.resegmentation || {};
    document.querySelector("#targetSegmentWords").value = info.target_words ?? data.config?.target_segment_words ?? "";
    showSegmentationResult(
      `已重新切分为 ${info.unit_count ?? data.units?.length ?? 0} 个单元；原状态备份：${info.backup_path || "已保存"}。`,
      true,
    );
  } catch (error) {
    showSegmentationResult(error.message);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("#segmentationForm").addEventListener("submit", saveSegmentationSettings);
  document.querySelector("#translationContextForm").addEventListener("submit", saveTranslationContextSettings);
  document.querySelector("#resegmentSourceButton").addEventListener("click", resegmentSource);
  bindApiSettings();
  loadApiSettings();
  loadSegmentationSettings();
  loadTranslationContextSettings();
});
