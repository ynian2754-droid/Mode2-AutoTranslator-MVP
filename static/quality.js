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

// Snapshot card text is frozen model input. Translate only the fixed labels
// supplied by quality_support.py; keep terms, meanings and translations intact.
function qualityDisplayCardText(value) {
  const text = String(value || "");
  if (window.Mode2I18n?.locale !== "en") return text;
  return text.split("\n").map(line => line
    .replace(/^来源：程序按有效证据自动采用的参考（非人工批准，人工参考优先）。$/, "Source: automatically adopted from eligible evidence (not manually approved; manual references take priority).")
    .replace(/^概念：/, "Concept: ")
    .replace(/^条目：/, "Entry: ")
    .replace(/→ 统一译名：/g, "→ canonical translation: ")
    .replace(/ 区别：/g, " Guidance: ")
    .replace(/ 适用：/g, " Applies when: ")
    .replace(/^本处含义：/, "Meaning here: ")
    .replace(/^适用范围：/, "Applicable context: ")
    .replace(/^统一译名：/, "Canonical translation: ")
    .replace(/^易混淆：/, "Common confusions: ")
    .replace(/^待核实：/, "Open questions: ")
  ).join("\n");
}

function qualityReferenceSnapshotMarkup(unit) {
  const en = window.Mode2I18n?.locale === "en";
  const reference = unit && unit.quality_reference;
  if (!reference || typeof reference !== "object") {
    return `<p class="quality-reference-empty">当前单元还没有参考快照。先扫描并批准概念，再触发翻译或重新审校即可生成快照。</p>`;
  }
  const entries = [];
  for (const [kind, entry] of Object.entries(reference)) {
    if (!entry || typeof entry !== "object") continue;
    const label = kind === "translation" ? (en ? "At translation time" : "翻译时") : (en ? "At review time" : "审校时");
    const flag = entry.is_new_reference
      ? (en ? " (new reference requested manually)" : "（人工触发的新参考）")
      : kind === "review" ? (en ? " (automatic review reused the same reference)" : "（自动审校沿用同一参考）") : "";
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
      omittedText = en ? "Omitted card count was not recorded in this snapshot." : "本次省略数量：这张快照没有记录，不做推测。";
    } else if (omittedCount === 0) {
      omittedText = en ? "No cards were omitted this time." : "本次没有省略卡片。";
    } else {
      const automaticPart = Number.isInteger(automaticOmittedCount)
        ? (en ? ` (${automaticOmittedCount} automatically adopted)` : `（其中自动采用 ${automaticOmittedCount} 张）`)
        : (en ? " (automatic adoption count not recorded)" : "（自动采用部分未记录）");
      omittedText = en
        ? `${omittedCount} cards omitted${automaticPart}: they matched this unit but exceeded the 6-card/6,000-character per-unit limit. Manual references take priority; cards are omitted whole.`
        : `本次省略 ${omittedCount} 张${automaticPart}：它们命中了这个单元，但没进本次注入——每单元最多 6 张卡／6000 字符，人工参考优先，放不下的整张省略。`;
    }
    const omittedMarkup = `<p class="quality-card-meta">${qualityEscape(omittedText)}</p>`;
    const cardsMarkup = cards.length
      ? `<ul class="quality-reference-cards">${cards
          .map(
            (card) => `<li>
              <p data-i18n-ignore="true">${qualityEscape(qualityDisplayCardText(card.text))}</p>
              <p class="quality-card-meta">card ${qualityEscape(card.card_id || "")} v${Number(card.card_revision) || 0} · ${en ? "matched" : "命中"} <span data-i18n-ignore="true">${qualityEscape((card.matched_expressions || []).join("、") || "—")}</span></p>
            </li>`
          )
          .join("")}</ul>`
      : `<p class="quality-card-meta">${en ? "No concept cards were injected (version record only)." : "本次未注入概念卡（仅含版本记录）"}</p>`;
    entries.push(
      `<li>
        <strong>${label}${qualityEscape(flag)}</strong>
        <p class="quality-card-meta">${en ? "Approved version" : "批准版本"} ${Number(entry.approved_version) || 0} · ${en ? "Cards used" : "使用卡片"} ${Number(entry.card_count) || 0} · ${en ? "Translation revision" : "译文版本"} ${Number(entry.translation_revision) || 0}</p>
        ${cardsMarkup}
        ${omittedMarkup}
        <p class="quality-card-meta">${en ? "Recorded" : "记录于"} <span data-i18n-ignore="true">${qualityEscape(entry.at || "")}</span></p>
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
    $("qualitySuggestList").innerHTML = suggestions.map(item => `<li><strong data-i18n-ignore="true">${qualityEscape(item.suggested_expression || "")}</strong><p>原文：<span data-i18n-ignore="true">${qualityEscape(item.source_excerpt || "")}</span></p><p>已保存译文：<span data-i18n-ignore="true">${qualityEscape(item.translation_excerpt || "")}</span></p><p data-i18n-ignore="true">${qualityEscape(item.reason || "")}</p></li>`).join("");
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

window.addEventListener("mode2:localechange", () => {
  const workbench = document.getElementById("qualityUnitReference");
  if (workbench && typeof ui !== "undefined") {
    const unit = ui.project?.units?.find(item => item.id === ui.selectedUnitId);
    if (unit) workbench.innerHTML = qualityReferenceSnapshotMarkup(unit);
  }
  const source = document.getElementById("sourceReference");
  if (source && typeof qp !== "undefined") {
    const unit = qp.units?.find(item => item.id === qp.sourceId);
    if (unit) source.innerHTML = qualityReferenceSnapshotMarkup(unit);
  }
});
