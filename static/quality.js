// Shared concept helpers and compact translation-workbench integration.
// The independent scanner/reviewer lives in quality-page.js.
function qualityEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function qualityReferenceSnapshotMarkup(unit) {
  const reference = unit && unit.quality_reference;
  if (!reference || typeof reference !== "object") {
    return `<p class="quality-reference-empty">当前单元还没有参考快照。先扫描并批准概念，再触发翻译或重新审校即可生成快照。</p>`;
  }
  const entries = [];
  for (const [kind, entry] of Object.entries(reference)) {
    if (!entry || typeof entry !== "object") continue;
    const label = kind === "translation" ? "翻译时" : "审校时";
    const flag = entry.is_new_reference ? "（人工触发的新参考）" : "（自动审校沿用同一参考）";
    const snapshot = entry.snapshot && typeof entry.snapshot === "object" ? entry.snapshot : {};
    const cards = Array.isArray(snapshot.cards) ? snapshot.cards : [];
    // This snapshot is what the unit really used; cards that matched but did
    // not fit the per-unit 6-card / 6000-character budget are the omitted
    // count. Older snapshots never recorded it, and a missing number is
    // reported as missing instead of being guessed.
    const omittedCount = snapshot.omitted_card_count;
    const automaticOmittedCount = snapshot.automatic_omitted_count;
    let omittedText;
    if (!Number.isInteger(omittedCount)) {
      omittedText = "本次省略数量：这张快照没有记录，不做推测。";
    } else if (omittedCount === 0) {
      omittedText = "本次没有省略卡片。";
    } else {
      const automaticPart = Number.isInteger(automaticOmittedCount)
        ? `（其中自动采用 ${automaticOmittedCount} 张）`
        : "（自动采用部分未记录）";
      omittedText = `本次省略 ${omittedCount} 张${automaticPart}：它们命中了这个单元，但没进本次注入——每单元最多 6 张卡／6000 字符，人工参考优先，放不下的整张省略。`;
    }
    const omittedMarkup = `<p class="quality-card-meta">${qualityEscape(omittedText)}</p>`;
    const cardsMarkup = cards.length
      ? `<ul class="quality-reference-cards">${cards
          .map(
            (card) => `<li>
              <p>${qualityEscape(card.text || "")}</p>
              <p class="quality-card-meta">card ${qualityEscape(card.card_id || "")} v${Number(card.card_revision) || 0} · 命中 ${qualityEscape((card.matched_expressions || []).join("、") || "—")}</p>
            </li>`
          )
          .join("")}</ul>`
      : `<p class="quality-card-meta">本次未注入概念卡（仅含版本记录）</p>`;
    entries.push(
      `<li>
        <strong>${label}${qualityEscape(flag)}</strong>
        <p class="quality-card-meta">approved_version=${Number(entry.approved_version) || 0} · card_count=${Number(entry.card_count) || 0} · translation_revision=${Number(entry.translation_revision) || 0}</p>
        ${cardsMarkup}
        ${omittedMarkup}
        <p class="quality-card-meta">记录于 ${qualityEscape(entry.at || "")}</p>
      </li>`
    );
  }
  if (!entries.length) {
    return `<p class="quality-reference-empty">当前单元还没有参考快照。</p>`;
  }
  return `<ul class="quality-reference-list">${entries.join("")}</ul>`;
}


const qualityWorkbench = { suggestionKey: "", suggesting: false };

function qualityProjectId() {
  return String(ui.project?.project?.id || "");
}

// The concept entry lives in the shared top bar, so it must stay in sync even
// when the detail panel is empty or protected from re-rendering.
function qualitySyncEntry() {
  const entry = $("qualityEntryLink");
  if (!entry) return;
  const catalogId = String(ui.project?.current_project?.id || "");
  const unit = ui.project?.units?.find(item => item.id === ui.selectedUnitId);
  entry.href = catalogId ? "/quality?" + new URLSearchParams({
    project_id: catalogId, ...(unit?.id ? {unit_id: unit.id} : {}),
  }) : "/";
}

// renderDetail() rebuilds the unit panel, so the snapshot and suggestion list
// are repopulated and rebound for the freshly created nodes on every unit.
// This deliberately does not clear the suggestion result: the async handler
// must still own that output after its own final refresh.
function qualityRenderUnit(unit) {
  const snapshot = $("qualityUnitReference");
  if (snapshot) snapshot.innerHTML = qualityReferenceSnapshotMarkup(unit);
  if ($("qualitySuggestButton")) $("qualitySuggestButton").disabled = qualityWorkbench.suggesting || !unit?.translation;
}

function qualityAttachUnit(unit) {
  qualityWorkbench.suggestionKey = "";
  qualitySyncEntry();
  const button = $("qualitySuggestButton");
  if (button) button.addEventListener("click", qualityFetchSuggestions);
  qualityRenderUnit(unit);
}

async function qualityFetchSuggestions() {
  const unit = ui.project?.units?.find(item => item.id === ui.selectedUnitId);
  if (!unit?.translation || qualityWorkbench.suggesting) return;
  const projectId = qualityProjectId();
  const key = JSON.stringify([projectId, unit.id, unit.source_sha256, Number(unit.translation_revision || 0)]);
  qualityWorkbench.suggestionKey = key;
  qualityWorkbench.suggesting = true;
  qualityRenderUnit(unit);
  $("qualitySuggestStatus").textContent = "正在为已保存的译文生成表达建议…";
  try {
    const result = await api(`/api/units/${encodeURIComponent(unit.id)}/editorial-suggestions`, {
      method: "POST",
      body: JSON.stringify({expected_project_id: projectId, source_sha256: unit.source_sha256,
        expected_translation_revision: Number(unit.translation_revision || 0)}),
    });
    if (key !== qualityWorkbench.suggestionKey) return;
    if (String(result.project_id) !== projectId || result.unit_id !== unit.id ||
        result.source_sha256 !== unit.source_sha256 ||
        Number(result.translation_revision) !== Number(unit.translation_revision || 0)) throw new Error("表达建议与当前单元版本不一致，请刷新后重试。");
    const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
    $("qualitySuggestList").innerHTML = suggestions.map(item => `<li><strong>${qualityEscape(item.suggested_expression || "")}</strong><p>原文：${qualityEscape(item.source_excerpt || "")}</p><p>已保存译文：${qualityEscape(item.translation_excerpt || "")}</p><p>${qualityEscape(item.reason || "")}</p></li>`).join("");
    $("qualitySuggestStatus").textContent = suggestions.length ? "仅供参考，不会自动替换译文。" : "本次没有新的表达建议。";
  } catch (error) {
    if (key === qualityWorkbench.suggestionKey) $("qualitySuggestStatus").textContent = `获取失败：${error.message}`;
  } finally {
    qualityWorkbench.suggesting = false;
    qualityRenderUnit(ui.project?.units?.find(item => item.id === ui.selectedUnitId));
  }
}

window.qualitySyncEntry = qualitySyncEntry;
window.qualityAttachUnit = qualityAttachUnit;
