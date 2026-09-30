"""Prepare plan shapes, unit reuse, and frozen group inputs from supplied data."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from core import concept_automation
from core.quality_limits import DEFAULT_ADDITIONAL_WORK_LIMIT
from core.quality_support import DEFAULT_SCAN_SOURCE_WORDS


def prepare_unit_reuse(
    automation: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Which units of this scope still count as finished, and why not.

    A unit is reused only when the previous preparation recorded that its
    generation + check work finished for the *same* source hash. A unit the
    manual panel merely marked as scanned is not covered: that flag carries
    no source version and says nothing about the automatic preparation.
    """

    coverage = concept_automation.prior_unit_coverage(automation.get("prepare"))
    states: dict[str, dict[str, str]] = {}
    for unit in targets:
        unit_id = str(unit.get("id") or "")
        source_sha256 = str(unit.get("source_sha256") or "")
        if unit_id and coverage.get(unit_id) == source_sha256:
            states[unit_id] = {
                "state": "reused",
                "reason": "上次准备已完成这个单元，源文与结论都没有变化。",
            }
        elif coverage.get(unit_id):
            states[unit_id] = {
                "state": "work",
                "reason": "源文已经变化，需要重新生成与检查。",
            }
        else:
            states[unit_id] = {
                "state": "work",
                "reason": "还没有完成的自动扫描，需要生成与检查。",
            }
    return states


def prepare_group_reuse(
    support: Mapping[str, Any],
    automation: Mapping[str, Any],
    unit_sources: Mapping[str, tuple[str, str]],
    *,
    unit_ids: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """The groups the next run would judge, and which judgments are reusable.

    The comparison is per group and uses the same input fingerprint that the
    judgment itself is frozen with, computed from the *live* membership: a
    group that gained or lost a member therefore never matches, even though
    its id stays the same.

    An oversized group is never "done" just because it has an outcome: it is
    judged one unit at a time, so the preview reports how many units the
    earlier confirmations already answered and how many are still owed. That
    remaining work is what a new confirmation continues — the finished units
    are handed over and never judged again.
    """

    prior = concept_automation.prior_group_judgments(automation.get("prepare"))
    scope = [str(unit_id) for unit_id in (unit_ids or [])] or sorted(unit_sources)
    rows: dict[str, dict[str, Any]] = {}
    for group in concept_automation.planned_groups(support, unit_sources=unit_sources):
        group_id = str(group["group_id"])
        members = prepare_group_members(support, group.get("card_ids") or [])
        entry: dict[str, Any] = {"members": len(members), "reused": False, "reason": ""}
        stored = prior.get(group_id) or {}
        fingerprint = concept_automation.group_input_fingerprint(
            group_id, members, support, unit_sources
        )
        stale = bool(fingerprint) and str(stored.get("input_fingerprint") or "") != fingerprint
        if group.get("oversized"):
            plans = concept_automation.local_contention_plans(
                group_id, members, unit_sources=unit_sources, unit_ids=scope
            )
            judged = {} if stale else concept_automation.local_judgment_payloads(
                stored.get("outcome")
            )
            remaining = [
                str(row["unit_id"])
                for row in plans["requests"]
                if str(row["unit_id"]) not in judged
            ]
            entry["local_units_reused"] = len(judged)
            entry["local_units_pending"] = len(remaining)
            entry["local_units_oversized"] = len(plans["oversized_units"])
            if not remaining:
                entry["reused"] = True
                entry["reason"] = "组成员与来源都没有变化，沿用上次的局部辨析结论。"
            elif not judged:
                entry["reason"] = (
                    f"组过大，本次按单元局部辨析：还有 {len(remaining)} 个单元待判断，"
                    "预算内能判断多少就判断多少。"
                )
            else:
                entry["reason"] = (
                    f"还有 {len(remaining)} 个单元没有完成局部辨析，确认后继续；"
                    f"已完成的 {len(judged)} 个单元直接复用。"
                )
        elif len(members) < 2:
            entry["reason"] = "组内没有两张可自动管理的卡片，不需要辨析。"
        elif fingerprint and not stale:
            entry["reused"] = True
            entry["reason"] = "组成员与来源都没有变化，沿用上次辨析结论。"
        else:
            entry["reason"] = "组成员、内容、检查或原文有变化，需要重新辨析。"
        rows[group_id] = entry
    return rows


def prepare_plan_payload(
    plan: Mapping[str, Any],
    *,
    support: Mapping[str, Any] | None = None,
    unit_sources: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """The stored plan record with a stable, validated shape.

    Accepts both a bare plan and the persisted prepare block (``plan``
    nested under ``plan``) so every caller shares one validation path.
    """

    nested = plan.get("plan")
    if isinstance(nested, Mapping):
        plan = nested
    scope = [str(item) for item in (plan.get("scope") or []) if str(item)]
    batches = [
        {"batch_id": str(item.get("batch_id") or ""), "unit_ids": [str(u) for u in item.get("unit_ids") or []]}
        for item in (plan.get("batches") or [])
        if isinstance(item, Mapping)
    ]
    resolved_groups = {
        str(key): copy.deepcopy(dict(value))
        for key, value in (plan.get("resolved_groups") or {}).items()
        if isinstance(value, Mapping)
    }
    cards = (support or {}).get("cards") or {}
    groups: list[dict[str, Any]] = []
    for item in plan.get("groups") or []:
        if not isinstance(item, Mapping):
            continue
        group_id = str(item.get("group_id") or "")
        card_ids = [str(c) for c in item.get("card_ids") or []]
        group: dict[str, Any] = {
            "group_id": group_id,
            "card_ids": card_ids,
            "oversized": bool(item.get("oversized")),
        }
        members = item.get("members")
        if isinstance(members, list) and members:
            group["members"] = copy.deepcopy(members)
        fingerprint = str(item.get("input_fingerprint") or "")
        if fingerprint:
            group["input_fingerprint"] = fingerprint
        groups.append(group)
    unit_states = {
        str(unit_id): {
            "state": str(row.get("state") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for unit_id, row in (plan.get("unit_states") or {}).items()
        if isinstance(row, Mapping)
    }
    return {
        "prepare_id": str(plan.get("prepare_id") or ""),
        "scope": scope,
        "scope_fingerprint": str(plan.get("scope_fingerprint") or ""),
        "batches": batches,
        "groups": groups,
        "resolved_groups": resolved_groups,
        "reused_units": [str(item) for item in (plan.get("reused_units") or []) if str(item)],
        "unit_states": unit_states,
        "baseline_revision": int(plan.get("baseline_revision") or 0),
        "baseline_approved_version": int(plan.get("baseline_approved_version") or 0),
        "max_source_words": int(plan.get("max_source_words") or DEFAULT_SCAN_SOURCE_WORDS),
        # Worker count of the confirmed run: frozen with the plan for the same
        # reason the batch size is, so the previewed number is what executes.
        "max_parallel_batches": max(1, int(plan.get("max_parallel_batches") or 1)),
        # The confirmed execution's extra-request budget is frozen with the
        # plan: the record reads it back from here, so a page that previews
        # one number cannot execute another. Missing means "the documented
        # default", exactly like the preview shows.
        "additional_work_limit": int(
            plan.get("additional_work_limit")
            if plan.get("additional_work_limit") is not None
            else DEFAULT_ADDITIONAL_WORK_LIMIT
        ),
    }


def prepare_group_members(
    support: Mapping[str, Any], card_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """The card payloads one group judgment may read (manual cards excluded)."""

    cards = support.get("cards") or {}
    members: list[dict[str, Any]] = []
    for card_id in card_ids:
        card = cards.get(str(card_id))
        if not isinstance(card, dict) or concept_automation.is_manual_protected(card):
            continue
        content = card.get("draft") or card.get("approved") or {}
        members.append(
            {
                "card_id": str(card_id),
                "content_revision": int(card.get("draft_revision") or 0),
                "payload": copy.deepcopy(dict(content)),
            }
        )
    return members
