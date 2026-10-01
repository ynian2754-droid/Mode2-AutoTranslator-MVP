"""Read-only concept support reports, affected units, and scan planning."""

from __future__ import annotations

import copy
from typing import Any, Mapping

import mode2_common
from core import concept_automation, quality_prepare_record, quality_requests
from core.exceptions import PipelineError
from core.project_state import ProjectStateCell, ensure_open, validate_expected_project_id
from core.quality_state import quality_unit_sources
from core.quality_support import (
    DEFAULT_SCAN_SOURCE_WORDS,
    affected_units_for_cards,
    batch_retry_descriptor,
    normalize_quality_support,
    planned_batches,
    scanned_unit_ids,
    select_reference_candidates,
    summarize_counts,
    terminology_mismatches,
    terminology_rules,
)


QUALITY_SCAN_SCOPES = {"current", "selected", "continue"}


class QualityQueries:
    def __init__(self, cell: ProjectStateCell) -> None:
        self.cell = cell

    def support_read(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
        """Read the optional feature state without ever writing it back.

        A legacy project without ``quality_support`` is simply empty. Opening
        or reading a project must not materialize an empty feature object.
        """
        with self.cell.lock:
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            sources = quality_unit_sources(self.cell.state.get("units") or [])
            return support, sources


    def stale_reference_units_locked(
        self,
        support: Mapping[str, Any],
        unit_sources: Mapping[str, tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Translated units whose frozen automatic reference is no longer live.

        Read-only reporting: the unit keeps the snapshot it was translated
        with and the translation is never rewritten. The list only says "this
        finished unit used an automatic reference that would not be injected
        today", so a human can decide what to do about it.
        """

        decisions = concept_automation.current_decisions(support)
        cards = support.get("cards") or {}
        rows: list[dict[str, Any]] = []
        for unit in self.cell.state.get("units") or []:
            if not isinstance(unit, dict) or not str(unit.get("translation") or "").strip():
                continue
            reference = unit.get("quality_reference")
            if not isinstance(reference, Mapping):
                continue
            seen: dict[str, dict[str, Any]] = {}
            for kind in ("translation", "review"):
                entry = reference.get(kind)
                snapshot = entry.get("snapshot") if isinstance(entry, Mapping) else None
                cards_in_snapshot = (snapshot or {}).get("cards") if isinstance(snapshot, Mapping) else None
                for card in cards_in_snapshot or []:
                    if not isinstance(card, Mapping) or str(card.get("origin") or "") != "automatic":
                        continue
                    card_id = str(card.get("card_id") or "")
                    live = cards.get(card_id)
                    decision = decisions.get(card_id)
                    frozen_revision = int(card.get("card_revision") or 0)
                    if not isinstance(live, dict):
                        reason = "参考卡已经不存在。"
                    elif not isinstance(decision, Mapping) or str(decision.get("verdict") or "") != "adopt":
                        reason = "自动采用决定已经失效或被撤销。"
                    elif concept_automation.is_manual_protected(live):
                        # The most actionable reason wins: a human already owns
                        # this card, so the automatic reference is never
                        # injected again whatever else also changed.
                        reason = "该卡已有人工决定或批准内容，自动参考不再注入。"
                    elif int(decision.get("content_revision") or 0) != frozen_revision:
                        reason = (
                            f"参考内容已经更新（当时用的是第 {frozen_revision} 版，"
                            f"现在是第 {int(decision.get('content_revision') or 0)} 版）。"
                        )
                    else:
                        problems = concept_automation.decision_problems(
                            decision, live, unit_sources=unit_sources
                        )
                        reason = "；".join(problems[:2])
                    if not reason or card_id in seen:
                        continue
                    seen[card_id] = {
                        "unit_id": str(unit.get("id") or ""),
                        "card_id": card_id,
                        "decision_id": str(card.get("decision_id") or ""),
                        "frozen_card_revision": int(card.get("card_revision") or 0),
                        "translation_revision": int(unit.get("translation_revision") or 0),
                        "reason": reason,
                    }
            rows.extend(seen.values())
        return sorted(rows, key=lambda row: (row["unit_id"], row["card_id"]))


    def support(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only view of cards, versions and scan coverage."""
        support, sources = self.support_read(
            expected_project_id=expected_project_id
        )
        cards = sorted(support.get("cards", {}).values(), key=lambda card: card["id"])
        batches = support.get("batches") or []
        # Coverage comes from the persistent scanned_unit_ids set, not from the
        # 40-entry batch display history.
        covered = list(scanned_unit_ids(support))
        counts = summarize_counts(support)
        automation = concept_automation.normalize_automation(support.get("automation"))
        prepare_record = automation.get("prepare")
        reference_mode = concept_automation.reference_mode(self.cell.state.get("project"))
        with self.cell.lock:
            audit_units = copy.deepcopy([
                unit for unit in (self.cell.state.get("units") or []) if isinstance(unit, dict)
            ])
        term_mismatch_rows: list[dict[str, Any]] = []
        term_conflict_rows: list[dict[str, Any]] = []
        for unit in audit_units:
            unit_id = str(unit.get("id") or "")
            source_text = str(unit.get("source") or "")
            if not unit_id or not source_text:
                continue
            candidates = select_reference_candidates(
                support, unit_id=unit_id, unit_sources=sources, mode=reference_mode,
                decisions=automation["decisions"],
            )
            projection = terminology_rules(candidates, source_text)
            term_conflict_rows.extend(
                {"unit_id": unit_id, **row} for row in projection["conflicts"]
            )
            translation = str(unit.get("translation") or "")
            if translation.strip():
                term_mismatch_rows.extend(
                    {"unit_id": unit_id, "status": str(unit.get("status") or ""), **row}
                    for row in terminology_mismatches(projection["rules"], translation)
                )
        # The automatic channel is what makes a card usable, so the view has to
        # show it next to the manual one. The projection is read-only and uses
        # the injection predicates: ``approved`` still means a human approval
        # and stays untouched, while an automatic decision is reported as an
        # automatic decision only.
        automatic_view = concept_automation.automatic_reference_view(
            support, unit_sources=sources, mode=reference_mode
        )
        cards = [
            {**card, "automatic": automatic_view.get(str(card.get("id") or ""))}
            for card in cards
        ]
        # Failed-batch recovery, derived read-only: which batches the operator
        # may hand-retry right now, and why the others may not be. Reading never
        # migrates or writes; the retry entry point re-checks the same rules
        # under the lock before anything is frozen.
        current_prepare_id = ""
        if isinstance(prepare_record, Mapping):
            current_prepare_id = str(prepare_record.get("prepare_id") or "")
        retryable_batches = [
            batch_retry_descriptor(
                support,
                batch,
                unit_sources=sources,
                mode=reference_mode,
                current_prepare_id=current_prepare_id,
            )
            for batch in (support.get("batches") or [])
            if isinstance(batch, Mapping)
        ]
        return {
            "schema_version": support["schema_version"],
            "revision": support["revision"],
            "approved_version": support["approved_version"],
            "reference_mode": reference_mode,
            "retryable_batches": retryable_batches,
            "reference_revision": int(automation.get("reference_revision") or 0),
            "prepare": (
                quality_prepare_record.prepare_summary_from_record(prepare_record)
                if isinstance(prepare_record, Mapping)
                else None
            ),
            "automatic_decisions": len(automation.get("decisions") or {}),
            "stale_reference_units": self.stale_reference_units_locked(support, sources),
            "terminology_audit": {
                "mismatches": term_mismatch_rows,
                "conflicts": term_conflict_rows,
            },
            "counts": counts,
            "cards": cards,
            "batches": batches,
            "covered_unit_ids": covered,
            "scanned_unit_ids": covered,
            "prompt_version": "quality-support-v1",
            # No scan caps are reported: batches are split by the submitted word
            # target alone and a plan can be terminated from the page at any time.
            "limits": {"default_scan_source_words": DEFAULT_SCAN_SOURCE_WORDS},
        }


    def scan_units_locked(
        self,
        *,
        scope: str,
        unit_ids: list[str] | None,
        current_unit_id: str | None,
    ) -> list[dict[str, Any]]:
        units = [unit for unit in (self.cell.state.get("units") or []) if isinstance(unit, dict)]
        if scope == "current":
            if not current_unit_id:
                raise PipelineError("请先选择一个单元，再从当前单元提取概念。")
            return [unit for unit in units if unit.get("id") == current_unit_id]
        if scope == "selected":
            if not unit_ids:
                raise PipelineError("请先在单元列表中选择要扫描的单元。")
            wanted = set(unit_ids)
            return [unit for unit in units if unit.get("id") in wanted]
        covered = scanned_unit_ids(normalize_quality_support(self.cell.state.get("quality_support")))
        remaining = [unit for unit in units if str(unit.get("id")) not in covered]
        if not remaining:
            raise PipelineError("所有单元都已经被扫描过，无需继续。")
        return remaining


    def plan_scan(
        self,
        *,
        scope: str = "selected",
        unit_ids: list[str] | None = None,
        current_unit_id: str | None = None,
        max_parallel_batches: int | None = None,
        max_source_words: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Describe the work of one scan without calling any model.

        Only sanity floors are enforced: the scan itself has no batch-count,
        per-batch unit-count or character ceiling, and the word target merely
        decides where a batch is split.
        """
        scope = str(scope or "selected").strip().casefold()
        if scope not in QUALITY_SCAN_SCOPES:
            raise PipelineError("扫描范围只能是 current、selected 或 continue。")
        effective_parallel_batches = 1 if max_parallel_batches is None else int(max_parallel_batches)
        effective_source_words = (
            DEFAULT_SCAN_SOURCE_WORDS if max_source_words is None else int(max_source_words)
        )
        if effective_parallel_batches < 1:
            raise PipelineError("概念扫描并行批数至少为 1。")
        if effective_source_words < 100:
            raise PipelineError("每批源文词数至少为 100。")
        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            targets = self.scan_units_locked(
                scope=scope, unit_ids=unit_ids, current_unit_id=current_unit_id
            )
            if not targets:
                raise PipelineError("没有可扫描的单元。")
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            approved_expressions = quality_requests.approved_expressions(support)
        plan = planned_batches(
            targets,
            max_source_words=effective_source_words,
            word_counter=mode2_common.english_word_count,
        )
        planned_units = [unit_id for batch in plan["batches"] for unit_id in batch["unit_ids"]]
        return {
            "scope": scope,
            "unit_count": len(targets),
            "planned_unit_ids": planned_units,
            # Explicit server-side slices with stable batch ids. The frontend
            # must consume these instead of guessing its own unit slices.
            "batches": plan["batches"],
            "batch_count": plan["batch_count"],
            "max_parallel_batches": effective_parallel_batches,
            "max_source_words": effective_source_words,
            "max_requests": plan["max_requests"],
            "unscannable_units": plan["unscannable_units"],
            "remaining_unit_ids": plan["remaining_unit_ids"],
            "approved_expression_count": len(approved_expressions),
            "note": "AI 将提出候选并检查依据，人工批准后才用于翻译。",
        }


    def affected_units(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """List units whose current source matches approved cards."""
        with self.cell.lock:
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            units = [unit for unit in (self.cell.state.get("units") or []) if isinstance(unit, dict)]
            versions: dict[str, Any] = {}
            for unit in units:
                reference = unit.get("quality_reference")
                if isinstance(reference, dict):
                    translation = reference.get("translation")
                    if isinstance(translation, dict):
                        versions[str(unit.get("id"))] = translation.get("approved_version")
        affected = affected_units_for_cards(support, units, reference_versions=versions)
        unknown = [
            {
                "unit_id": item["unit_id"],
                "status": item["status"],
                "reason": "没有记录当时使用的概念参考版本。",
            }
            for item in affected
            if item["previous_reference_version"] is None
        ]
        return {
            "approved_version": support["approved_version"],
            "affected": affected,
            "unknown_reference_count": len(unknown),
            "note": "可能受影响，不代表已发现误译。",
        }

