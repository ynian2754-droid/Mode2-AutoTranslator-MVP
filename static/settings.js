const scopes = ["inspection", "translation"];
const labels = { inspection: "检验", translation: "翻译" };

function formFor(scope) {
  return document.querySelector(`[data-form="${scope}"]`);
}

function field(form, name) {
  return form.querySelector(`[data-field="${name}"]`);
}

function showResult(scope, message, success = false) {
  const result = document.querySelector(`[data-result="${scope}"]`);
  result.textContent = message;
  result.className = `settings-result ${success ? "success" : "error"}`;
}

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

function showOcGoResult(message, success = false) {
  const result = document.querySelector("#ocGoResult");
  result.textContent = message;
  result.className = `settings-result ${success ? "success" : "error"}`;
}

function ocGoEnabled() {
  return scopes.every((scope) => field(formFor(scope), "oc_go_compatibility").value === "true");
}

function updateOcGoState() {
  const enabled = ocGoEnabled();
  const anyEnabled = scopes.some(
    (scope) => field(formFor(scope), "oc_go_compatibility").value === "true",
  );
  const state = document.querySelector("#ocGoState");
  const button = document.querySelector("#ocGoToggleButton");
  state.textContent = enabled
    ? "已对检验与翻译启用"
    : anyEnabled
      ? "部分启用"
      : "未启用";
  button.textContent = enabled ? "关闭 OC Go 适配" : "启用 OC Go 适配";
  button.disabled = false;
}

function applyOcGoSettings(settings) {
  scopes.forEach((scope) => {
    const config = settings?.[scope];
    if (!config) return;
    const form = formFor(scope);
    const enabled = field(form, "oc_go_compatibility");
    const session = field(form, "opencode_session_id");
    if (typeof config.oc_go_compatibility === "boolean") {
      enabled.value = String(config.oc_go_compatibility);
    }
    if (typeof config.opencode_session_id === "string") {
      session.value = config.opencode_session_id;
    }
  });
  updateOcGoState();
}

function modelListFor(scope) {
  return document.querySelector(`[data-model-list="${scope}"]`);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function readConfig(scope) {
  const form = formFor(scope);
  return {
    base_url: field(form, "base_url").value.trim(),
    api_key: field(form, "api_key").value.trim(),
    model: field(form, "model").value.trim(),
    reasoning_effort: field(form, "reasoning_effort").value.trim(),
    temperature: Number(field(form, "temperature").value),
    max_output_tokens: Number(field(form, "max_output_tokens").value),
    timeout_seconds: Number(field(form, "timeout_seconds").value),
    oc_go_compatibility: field(form, "oc_go_compatibility").value === "true",
    opencode_session_id: field(form, "opencode_session_id").value.trim(),
  };
}

function fillConfig(scope, config) {
  const form = formFor(scope);
  Object.entries(config || {}).forEach(([name, value]) => {
    const input = field(form, name);
    if (input) input.value = value ?? "";
  });
  document.querySelector(`[data-state="${scope}"]`).textContent = "已加载";
}

async function loadSettings() {
  try {
    const data = await request("/api/settings");
    scopes.forEach((scope) => fillConfig(scope, data[scope]));
    updateOcGoState();
    document.querySelector("#connectionDot").classList.remove("offline");
    document.querySelector("#connectionText").textContent = "本地服务已连接";
  } catch (error) {
    document.querySelector("#connectionDot").classList.add("offline");
    document.querySelector("#connectionText").textContent = "服务不可用";
    document.querySelector("#pageStatus").textContent = error.message;
  }
}

async function toggleOcGoCompatibility() {
  const button = document.querySelector("#ocGoToggleButton");
  const enabled = !ocGoEnabled();
  button.disabled = true;
  showOcGoResult(enabled ? "正在启用 OC Go 适配…" : "正在关闭 OC Go 适配…");
  try {
    const data = await request("/api/settings/oc-go", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    });
    applyOcGoSettings(data.settings);
    showOcGoResult(
      enabled
        ? "OC Go 适配已启用；后续检验与翻译请求会携带专用 Agent 标识和稳定会话 ID。"
        : "OC Go 适配已关闭；请求已恢复普通兼容模式。",
      true,
    );
  } catch (error) {
    updateOcGoState();
    showOcGoResult(error.message);
  } finally {
    button.disabled = false;
  }
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

async function saveScope(scope) {
  const form = formFor(scope);
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const data = await request(`/api/settings/${scope}`, {
      method: "PUT",
      body: JSON.stringify(readConfig(scope)),
    });
    fillConfig(scope, data.config);
    updateOcGoState();
    showResult(scope, `${labels[scope]} API 配置已保存。`, true);
  } catch (error) {
    showResult(scope, error.message);
  } finally {
    button.disabled = false;
  }
}

async function testScope(scope) {
  const form = formFor(scope);
  if (!form.reportValidity()) return;
  const button = form.querySelector('[data-action="test"]');
  button.disabled = true;
  showResult(scope, "正在测试连接…");
  try {
    const data = await request(`/api/settings/${scope}/test`, {
      method: "POST",
      body: JSON.stringify(readConfig(scope)),
    });
    showResult(scope, `${labels[scope]} API 连接成功 · ${data.model}`, true);
  } catch (error) {
    showResult(scope, error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadModels(scope) {
  const form = formFor(scope);
  // Model discovery only needs the endpoint.  Requiring a model name here
  // would force the user to guess a model before the list can be fetched.
  if (!field(form, "base_url").reportValidity()) return;
  const button = form.querySelector('[data-action="models"]');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "正在拉取…";
  showResult(scope, "正在拉取模型…");
  try {
    const data = await request(`/api/settings/${scope}/models`, {
      method: "POST",
      body: JSON.stringify(readConfig(scope)),
    });
    const select = modelListFor(scope);
    select.replaceChildren(new Option("选择已拉取的模型", ""));
    data.models.forEach((model) => select.add(new Option(model, model)));
    select.hidden = false;
    const currentModel = field(form, "model").value.trim();
    select.value = data.models.includes(currentModel) ? currentModel : "";
    showResult(scope, `${labels[scope]} API 已获取 ${data.models.length} 个模型，可选择或继续手动输入。`, true);
  } catch (error) {
    showResult(scope, `拉取模型失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("#segmentationForm").addEventListener("submit", saveSegmentationSettings);
  document.querySelector("#translationContextForm").addEventListener("submit", saveTranslationContextSettings);
  document.querySelector("#resegmentSourceButton").addEventListener("click", resegmentSource);
  document.querySelector("#ocGoToggleButton").addEventListener("click", toggleOcGoCompatibility);
  scopes.forEach((scope) => {
    const form = formFor(scope);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveScope(scope);
    });
    form.querySelector('[data-action="test"]').addEventListener("click", () => testScope(scope));
    form.querySelector('[data-action="models"]').addEventListener("click", () => loadModels(scope));
    modelListFor(scope).addEventListener("change", (event) => {
      if (event.target.value) field(form, "model").value = event.target.value;
    });
  });
  loadSettings();
  loadSegmentationSettings();
  loadTranslationContextSettings();
});
