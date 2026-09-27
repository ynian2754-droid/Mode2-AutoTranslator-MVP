const $ = (id) => document.getElementById(id);
const DRAFT_STORAGE_KEY = "mode2.translationDraft.v1";
let projectSummaries = new Map();

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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

function formatDate(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  const locale = window.Mode2I18n?.locale === "en" ? "en-US" : undefined;
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString(locale, { hour12: false });
}

function statusText(status) {
  return {
    ready: "准备就绪",
    running: "处理中",
    needs_action: "待人工裁决",
    completed: "已完成",
  }[status] || "准备就绪";
}

function showStatus(message, error = false) {
  const status = $("pageStatus");
  status.textContent = message;
  status.className = `project-page-status${error ? " error" : ""}`;
}

function clearProjectDrafts(projectId) {
  try {
    if (!window.localStorage) return true;
    const raw = window.localStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return true;
    const parsed = JSON.parse(raw);
    const records = Array.isArray(parsed)
      ? parsed
      : parsed && parsed.schema_version === 1 && Array.isArray(parsed.drafts)
        ? parsed.drafts
        : null;
    if (!records) return true;
    const retained = records.filter((record) => record?.project_id !== String(projectId));
    if (retained.length !== records.length) {
      window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({ schema_version: 1, drafts: retained }));
    }
    return true;
  } catch (_error) {
    return false;
  }
}

function renderProjects(projects) {
  const grid = $("projectGrid");
  const empty = $("emptyState");
  projectSummaries = new Map(projects.map((project) => [String(project.id), project]));
  if (!projects.length) {
    grid.replaceChildren();
    grid.hidden = true;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  grid.hidden = false;
  grid.innerHTML = projects.map((project) => {
    const sourceNameMarkup = project.source_name
      ? `<span data-i18n-ignore="true">${escapeHtml(project.source_name)}</span>`
      : `<span>尚未导入源文件</span>`;
    return `
    <article class="project-card panel">
      <div class="project-card-top">
        <div>
          <p class="eyebrow">TRANSLATION PROJECT</p>
          <h2 data-i18n-ignore="true">${escapeHtml(project.name)}</h2>
        </div>
        ${project.is_current ? '<span class="project-current">当前项目</span>' : ""}
      </div>
      <div class="project-meta">
        <span>${escapeHtml(statusText(project.status))}</span>
        <span>最近修改 <time data-i18n-ignore="true">${escapeHtml(formatDate(project.updated_at))}</time></span>
      </div>
      <p class="project-summary">
        ${sourceNameMarkup}<span> · </span><span>${project.unit_count ?? 0} 个翻译单元</span>
      </p>
       <div class="project-actions">
         <button class="button secondary enter-project-button" type="button" data-project-id="${escapeHtml(project.id)}">进入项目</button>
         <button class="button danger delete-project-button" type="button" data-project-id="${escapeHtml(project.id)}">删除项目</button>
       </div>
    </article>
  `;
  }).join("");
  grid.querySelectorAll(".enter-project-button").forEach((button) => {
    button.addEventListener("click", () => enterProject(button.dataset.projectId, button));
  });
  grid.querySelectorAll(".delete-project-button").forEach((button) => {
    button.addEventListener("click", () => deleteProject(button.dataset.projectId, button));
  });
}

async function loadProjects() {
  try {
    const data = await request("/api/projects");
    renderProjects(data.projects || []);
    $("connectionDot").classList.remove("offline");
    $("connectionText").textContent = "本地服务已连接";
    showStatus("");
  } catch (error) {
    $("connectionDot").classList.add("offline");
    $("connectionText").textContent = "服务不可用";
    showStatus(error.message, true);
  }
}

async function enterProject(projectId, button) {
  button.disabled = true;
  showStatus("正在打开项目…");
  try {
    await request(`/api/projects/${encodeURIComponent(projectId)}/select`, { method: "POST", body: "{}" });
    window.location.href = "/editor";
  } catch (error) {
    button.disabled = false;
    showStatus(error.message, true);
  }
}

async function deleteProject(projectId, button) {
  const project = projectSummaries.get(String(projectId));
  const projectName = project?.name || projectId;
  if (!window.confirm(`确定删除项目“${projectName}”？项目文件和输出将被永久删除。`)) return;
  button.disabled = true;
  showStatus("正在删除项目…");
  try {
    await request(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
    const draftsCleared = clearProjectDrafts(projectId);
    showStatus(draftsCleared ? "项目已删除。" : "项目已删除，但本地草稿清理失败，请稍后检查浏览器存储。", !draftsCleared);
    await loadProjects();
  } catch (error) {
    button.disabled = false;
    showStatus(error.message, true);
  }
}

function openProjectDialog() {
  $("dialogStatus").textContent = "";
  $("projectNameInput").value = "";
  $("projectDialog").showModal();
  $("projectNameInput").focus();
}

function closeProjectDialog() {
  $("projectDialog").close();
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const action = $("createProjectAction");
  action.disabled = true;
  $("dialogStatus").textContent = "正在创建…";
  try {
    await request("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name: $("projectNameInput").value.trim() }),
    });
    window.location.href = "/editor";
  } catch (error) {
    $("dialogStatus").textContent = error.message;
    action.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("newProjectButton").addEventListener("click", openProjectDialog);
  $("emptyCreateButton").addEventListener("click", openProjectDialog);
  $("cancelProjectButton").addEventListener("click", closeProjectDialog);
  $("cancelProjectAction").addEventListener("click", closeProjectDialog);
  $("projectForm").addEventListener("submit", createProject);
  $("projectDialog").addEventListener("click", (event) => {
    if (event.target === $("projectDialog")) closeProjectDialog();
  });
  loadProjects();
});

window.addEventListener("mode2:localechange", () => {
  if (projectSummaries.size) renderProjects([...projectSummaries.values()]);
});
