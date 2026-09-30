"""Prepare record construction, plan binding, and bounded public summaries."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation
from core.exceptions import ConflictError
from core.quality_limits import MAX_CHECK_REQUEST_CHARS, MAX_CHECK_REQUEST_UNITS, MAX_RECHECK_CARDS


def new_prepare_record(
    prepared: Mapping[str, Any],
    *,
    clock: Callable[[], str],
    unit_states: Mapping[str, Mapping[str, str]] | None = None,
    unit_sources: Mapping[str, tuple[str, str]] | None = None,
    lookups: Sequence[Mapping[str, Any]] | None = None,
    lookup_state: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    unit_states = unit_states or {}
    unit_sources = unit_sources or {}

    def result_row(unit_id: str) -> dict[str, Any]:
        state = unit_states.get(unit_id) or {}
        reused = str(state.get("state") or "") == "reused"
        return {
            "unit_id": unit_id,
            # ``reused`` means the work was already finished by an earlier
            # run for this exact source hash; ``pending`` means this run
            # still owes the unit its generation + check.
            "status": "reused" if reused else "pending",
            "batch_id": "",
            "reason": str(state.get("reason") or ""),
            # Recorded with the row: the next run proves a reused unit by
            # this hash, never by "some scan touched it".
            "source_sha256": str((unit_sources.get(unit_id) or ("", ""))[1]),
        }

    reused_count = sum(1 for unit_id in prepared["scope"] if result_row(unit_id)["status"] == "reused")
    return {
        "prepare_id": prepared["prepare_id"],
        "mode": concept_automation.AUTOMATIC_MODE,
        "status": "running",
        "scope": prepared["scope"],
        "scope_fingerprint": prepared["scope_fingerprint"],
        "baseline_revision": prepared["baseline_revision"],
        "baseline_approved_version": prepared["baseline_approved_version"],
        "plan": dict(prepared),
        "counts": {
            "adopted": 0,
            "skipped": 0,
            "unresolved": 0,
            "protected": 0,
            "ineligible": 0,
            "failed_units": 0,
            "eligible_units": len(prepared["scope"]),
            "uncovered_units": 0,
            "reused_units": reused_count,
            "processed_units": 0,
            "reused_groups": 0,
            "judged_groups": 0,
            # A2/A3 extras: a re-check answers an old card, a lookup answers
            # one question, a local judgment answers one unit — none of them
            # is an adoption, and every one of them is counted here.
            "refreshed_checks": 0,
            "recheck_pending": 0,
            "recheck_reasons": {},
            "lookup_rounds": 0,
            "lookup_hits": 0,
            "lookup_refreshed": 0,
            "lookup_misses": 0,
            # Already answered with exactly this question (never re-paid),
            # skipped by the input bounds, or executed without a usable write.
            "lookup_settled": 0,
            "lookup_deferred": 0,
            "lookup_failed": 0,
            "lookup_rejected": 0,
            "budget_pending": 0,
            "local_groups": 0,
            "local_units_judged": 0,
            "local_units_oversized": 0,
            "local_units_reused": 0,
            "local_units_pending": 0,
        },
        "requests": {"generation": 0, "check": 0, "resolution": 0, "repair_rounds": 0},
        # Extra logical requests actually spent, by kind. Never shared with
        # the base generation/check/resolution counters above.
        "budget": {
            "limit": int(prepared.get("additional_work_limit") or 0),
            "used": 0,
            "by_kind": {},
        },
        # Executed lookups travel with the run: the next confirmed execution
        # inherits them, so the same question is never asked (and never paid
        # for) twice. Refused lookups are deliberately absent — they were not
        # executed and must stay available. ``lookups`` is the bounded display
        # log; ``lookup_state`` is the per-card dedup state and is *not*
        # trimmed by it, so an older answered question is never forgotten.
        "lookups": [dict(row) for row in (lookups or []) if isinstance(row, Mapping)],
        "lookup_state": concept_automation.lookup_state_of(lookup_state),
        "unit_results": [result_row(unit_id) for unit_id in prepared["scope"]],
        "not_adopted_reasons": [],
        "errors": [],
        "started_at": clock(),
        "finished_at": "",
    }


def prepare_record(support: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    """The running prepare record, checked against the caller's plan id."""

    automation = concept_automation.normalize_automation(support.get("automation"))
    record = automation.get("prepare")
    if not isinstance(record, Mapping):
        raise ConflictError("当前项目没有进行中的准备任务，请重新开始准备。")
    requested = str(plan.get("prepare_id") or "")
    if requested and requested != str(record.get("prepare_id") or ""):
        raise ConflictError("准备计划已经变化，请重新开始准备。")
    return {
        "prepare_id": str(record.get("prepare_id") or ""),
        "mode": concept_automation.AUTOMATIC_MODE,
        "status": str(record.get("status") or "running"),
        "committed": record.get("committed") is True,
        "scope": list(record.get("scope") or []),
        "scope_fingerprint": str(record.get("scope_fingerprint") or ""),
        "baseline_revision": int(record.get("baseline_revision") or 0),
        "baseline_approved_version": int(record.get("baseline_approved_version") or 0),
        "plan": copy.deepcopy(dict(record.get("plan") or {})),
        "counts": dict(record.get("counts") or {}),
        "requests": dict(record.get("requests") or {}),
        # The frozen extra-request pool travels with the record: every extra
        # step reads the same number the preview showed.
        "budget": copy.deepcopy(dict(record.get("budget") or {})),
        # The executed lookups of this run (input identity + result), so a
        # later confirmation can prove the identical question was answered
        # already instead of paying for it again.
        "lookups": [
            dict(row)
            for row in (record.get("lookups") or [])
            if isinstance(row, Mapping)
        ],
        # The dedup state, kept apart from the bounded display log: it is what
        # decides whether a question was already answered (and paid for).
        "lookup_state": concept_automation.lookup_state_of(record.get("lookup_state")),
        "errors": list(record.get("errors") or []),
        "unit_results": list(record.get("unit_results") or []),
        "started_at": str(record.get("started_at") or ""),
        "finished_at": str(record.get("finished_at") or ""),
    }


def prepare_summary_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The result overview: concepts, candidates, groups and units are kept
    as separate units of count — they must never be added into one total."""

    counts = dict(record.get("counts") or {})
    plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    batches = [batch for batch in (plan.get("batches") or []) if isinstance(batch, Mapping)]
    groups = [group for group in (plan.get("groups") or []) if isinstance(group, Mapping)]
    resolved = plan.get("resolved_groups") or {}
    unresolved_groups = sum(
        1
        for group in groups
        if str((resolved.get(group.get("group_id")) or {}).get("relation") or "") in ("unresolved", "skipped", "failed")
    )
    oversized_groups = sum(1 for group in groups if group.get("oversized"))
    errors = [str(item) for item in (record.get("errors") or []) if str(item).strip()]
    status = str(record.get("status") or "")
    planned_units = len(record.get("scope") or [])
    failed_units = int(counts.get("failed_units") or 0)
    reused_units = int(counts.get("reused_units") or 0)
    processed_units = int(counts.get("processed_units") or 0)
    raw_reasons = record.get("not_adopted_reasons")
    not_adopted_reasons = [
        {"reason": str(row.get("reason") or ""), "count": int(row.get("count") or 0)}
        for row in (raw_reasons if isinstance(raw_reasons, list) else [])
        if isinstance(row, Mapping) and str(row.get("reason") or "")
    ][:8]
    return {
        "prepare_id": str(record.get("prepare_id") or ""),
        "status": status,
        "scope": list(record.get("scope") or []),
        "committed": record.get("committed") is True,
        # Concepts that became usable references.
        "adopted": int(counts.get("adopted") or 0),
        # Candidates the preparation skipped, with the split that matters.
        "skipped": int(counts.get("skipped") or 0),
        "ineligible": int(counts.get("ineligible") or 0),
        "protected": int(counts.get("protected") or 0),
        "unresolved": int(counts.get("unresolved") or 0),
        # Why candidates were not adopted, so the page can show reasons
        # rather than only a total.
        "not_adopted_reasons": not_adopted_reasons,
        # Groups that were judged and groups that could not be judged.
        "group_count": len(groups),
        "unresolved_groups": unresolved_groups,
        "oversized_groups": oversized_groups,
        # Incremental scope (V2): what was handed over and what was redone.
        "reused_units": reused_units,
        "processed_units": processed_units,
        "reused_groups": int(counts.get("reused_groups") or 0),
        "judged_groups": int(counts.get("judged_groups") or 0),
        # Units: planned / finished / failed / not executed. The failed
        # count comes from the recorded per-unit results, never from a guess.
        "planned_units": planned_units,
        "finished_units": max(0, planned_units - failed_units),
        "failed_units": failed_units,
        # Set when a hand retry finished a batch this prepare recorded as
        # failed: its generation/check results are reusable by the next
        # confirmation, but the references still need an explicit re-preview.
        "reference_refresh_required": record.get("reference_refresh_required") is True,
        "reference_refresh_required_at": str(
            record.get("reference_refresh_required_at") or ""
        ),
        "unexecuted_units": max(0, planned_units - len(batches) and 0 or 0),
        "uncovered_units": int(counts.get("uncovered_units") or 0),
        "eligible_units": int(counts.get("eligible_units") or 0),
        "errors": errors,
        "requests": dict(record.get("requests") or {}),
        "budget": dict(record.get("budget") or {}),
        # A2/A3 split: an old check that was refreshed, one bounded lookup,
        # one unit's local judgment. Every one of them is still a candidate
        # the run had to answer, never an adoption by itself.
        "refreshed_checks": int(counts.get("refreshed_checks") or 0),
        "recheck_pending": int(counts.get("recheck_pending") or 0),
        "recheck_reasons": dict(counts.get("recheck_reasons") or {}),
        "lookup_rounds": int(counts.get("lookup_rounds") or 0),
        "lookup_hits": int(counts.get("lookup_hits") or 0),
        "lookup_refreshed": int(counts.get("lookup_refreshed") or 0),
        "lookup_misses": int(counts.get("lookup_misses") or 0),
        # A question already asked with exactly this content and evidence, or
        # one whose input did not fit this execution's bounds: neither is a
        # request of this run, and neither is an adoption.
        "lookup_settled": int(counts.get("lookup_settled") or 0),
        "lookup_deferred": int(counts.get("lookup_deferred") or 0),
        # A failed request and a refused write are different outcomes: both
        # were executed, neither wrote anything, and neither is reported as a
        # refresh or as a plain miss.
        "lookup_failed": int(counts.get("lookup_failed") or 0),
        "lookup_rejected": int(counts.get("lookup_rejected") or 0),
        "local_groups": int(counts.get("local_groups") or 0),
        "local_units_judged": int(counts.get("local_units_judged") or 0),
        "local_units_oversized": int(counts.get("local_units_oversized") or 0),
        # Units whose local judgment an earlier confirmation already made:
        # handed over without a request, so they are neither "judged now" nor
        # "still pending".
        "local_units_reused": int(counts.get("local_units_reused") or 0),
        "local_units_pending": int(counts.get("local_units_pending") or 0),
        # The base re-check and the extra lookup are accounted for on
        # different bases; the page and the report must not merge them.
        "budget_basis": {
            "recheck": {
                "pool": "base",
                "counted_as": "requests.check",
                "note": "基础重查是复用单元的必要步骤，不占额外请求预算；单次确认的请求有输入上限，超出记 recheck_pending。",
                "limits": {
                    "cards": MAX_RECHECK_CARDS,
                    "units": MAX_CHECK_REQUEST_UNITS,
                    "chars": MAX_CHECK_REQUEST_CHARS,
                },
            },
            "lookup": {
                "pool": "shared_extra",
                "slots_per_execution": 1,
                "note": "一次确认最多一次补查，占共享额外请求预算 1 格；请求规模按计划 §4.1 的上限，先到者为限。",
                "limits": dict(concept_automation.LOOKUP_REQUEST_LIMITS),
            },
        },
        # Work the shared pool could not pay for. Shown as "still pending",
        # never as a completed step.
        "budget_pending": int(counts.get("budget_pending") or 0),
        # A5 split: answered questions, open questions and the units a
        # finished batch found nothing for.
        "ai_resolved_cards": int(counts.get("ai_resolved_cards") or 0),
        "resolved_questions": int(counts.get("resolved_questions") or 0),
        "remaining_questions": int(counts.get("remaining_questions") or 0),
        "no_candidate_units": int(counts.get("no_candidate_units") or 0),
        "covered_units": max(
            0,
            int(counts.get("eligible_units") or 0) - int(counts.get("uncovered_units") or 0),
        ),
        "note": (
            "成功 0 张概念也是有效结果：没有候选的单元仍可正常翻译。"
            if status in ("complete", "partial") and not counts.get("adopted")
            else ""
        ),
        "reuse_note": (
            f"本次复用已完成的单元 {reused_units} 个、仍有效的组辨析 {int(counts.get('reused_groups') or 0)} 个，"
            f"只重新处理了 {processed_units} 个单元。"
            if reused_units or int(counts.get("reused_groups") or 0)
            else ""
        ),
    }
