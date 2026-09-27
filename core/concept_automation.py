"""Automatic concept reference: pure data rules (V1).

This module owns the shape and the validation rules of the automatic reference
feature and nothing else: no HTTP, no persistence, no model calls. The pipeline
and the API layer call into it; the page only reads what the GET endpoint
reports.

Core boundaries, in one place:

* ``reference_mode`` is a project setting. A project without the field reads as
  ``manual`` (legacy compatibility) and reading never writes it back.
* ``automation.decisions`` record what the program verified. They are stored
  separately from ``approved``: ``approved`` still means a human approval, and
  this module never fabricates one, never calls the human approve path and never
  touches ``approved_version``.
* A card that a human edited, approved, deferred or rejected exits automatic
  management. When the card's history cannot be told apart from a human action,
  the conservative choice is protection, never the opposite.
"""

from __future__ import annotations

import hashlib
import json

import copy
import re
from typing import Any, Iterable, Mapping, Sequence

from core import quality_support as qs
from core.utils import now_iso

import mode2_common
from core.concept_automation_assessment import (
    ASSESSMENT_SCHEMA_VERSION,
    BINDING_KINDS,
    MAX_ASSESSMENT_BINDINGS,
    MAX_LOOKUP_EXPRESSIONS,
    QUESTION_KINDS,
    QUESTION_STATUSES,
    AutomationError,
    _assessment_evidence,
    _assessment_text,
    _expression_key,
    _recorded_content_fingerprint,
    adoptable_bindings,
    assessment_fingerprint,
    assessment_of,
    normalize_assessment,
    question_block,
)
from core.concept_automation_lookup import (
    LOOKUP_LOG_STATUSES,
    LOOKUP_REQUEST_LIMITS,
    LOOKUP_RESULT_STATUSES,
    MAX_LOOKUP_LEDGER,
    MAX_LOOKUP_STATE_EXPRESSIONS,
    MAX_LOOKUP_STATE_UNITS,
    _folded_with_offsets,
    charge_budget,
    check_fingerprint,
    lookup_allowance_spent,
    lookup_batch,
    lookup_content_version,
    lookup_identity,
    lookup_ledger_row,
    lookup_material_intact,
    lookup_material_units,
    lookup_occurrences,
    lookup_request_blockers,
    lookup_request_shape,
    lookup_state_get,
    lookup_state_of,
    lookup_state_pruned,
    lookup_state_set,
    lookup_state_units,
)

#: Stored shape of the ``automation`` object.
AUTOMATION_SCHEMA_VERSION = 1

#: Reference modes a project may declare.
REFERENCE_MODES = ("manual", "automatic")
MANUAL_MODE = "manual"
AUTOMATIC_MODE = "automatic"

#: States of the most recent prepare run. ``running`` must be normalized to
#: ``interrupted`` after a restart, never continued in the background.
PREPARE_STATUSES = ("running", "complete", "partial", "failed", "interrupted", "stale")

#: Statuses of a previous preparation whose per-unit results may be handed to
#: the next run. A ``failed`` run proves nothing, and a ``stale`` run was
#: invalidated because its input changed, so neither may feed a reuse decision.
REUSABLE_PREPARE_STATUSES = ("complete", "partial", "interrupted", "running")

#: Unit result statuses that prove the generation + check work really finished.
FINISHED_UNIT_STATUSES = ("completed", "reused")

#: Group relations that are judgments. ``skipped`` and ``failed`` only record
#: that no judgment exists, so they are never handed to a later run.
REUSABLE_RELATIONS = ("equivalent", "distinct", "unresolved")

#: What a single decision may say about one card.
DECISION_VERDICTS = ("adopt", "duplicate", "unresolved", "ineligible", "protected")

#: A decision that puts a card into the automatic reference set.
ADOPTED_VERDICT = "adopt"

#: Upper bounds of one group resolution call (new calls only; the old scan caps
#: stay deleted).
MAX_RESOLUTION_CARDS = 10
MAX_RESOLUTION_CHARS = 24000

#: Structured result of the independent check for the questions one draft raised.
#: The model only answers questions and maps expressions to translations; every
#: control field (content revision, approval, save state) stays with the
#: program. Frozen field names live in ``tests/fixtures/reference_practical_cases.json``.

# --------------------------------------------------------------------------
# reference mode
# --------------------------------------------------------------------------

def reference_mode(project: Any) -> str:
    """Return the project's reference mode, defaulting to manual.

    Unknown values (including a future mode this build does not know) also read
    as manual: automation is never enabled by accident.
    """

    if isinstance(project, Mapping):
        value = project.get("reference_mode")
        if isinstance(value, str):
            mode = value.strip().casefold()
            if mode in REFERENCE_MODES:
                return mode
    return MANUAL_MODE

def requires_human_approval(project: Any) -> bool:
    """Whether new references need an explicit human approval to become usable."""

    return reference_mode(project) == MANUAL_MODE

# --------------------------------------------------------------------------
# state shape
# --------------------------------------------------------------------------

def empty_automation() -> dict[str, Any]:
    return {
        "schema_version": AUTOMATION_SCHEMA_VERSION,
        "reference_revision": 0,
        "prepare": None,
        "decisions": {},
    }

def normalize_prepare(value: Any) -> dict[str, Any] | None:
    """Read one stored prepare record tolerantly; return None when absent."""

    if not isinstance(value, Mapping):
        return None
    status = str(value.get("status") or "").strip().casefold()
    if status not in PREPARE_STATUSES:
        status = "failed"
    unit_results: list[dict[str, Any]] = []
    raw_results = value.get("unit_results")
    if isinstance(raw_results, list):
        for row in raw_results:
            if not isinstance(row, Mapping):
                continue
            unit_id = str(row.get("unit_id") or "").strip()
            unit_results.append(
                {
                    "unit_id": unit_id,
                    "source_sha256": str(row.get("source_sha256") or ""),
                    "status": str(row.get("status") or "").strip().casefold(),
                    "adopted": int(row.get("adopted") or 0),
                    "skipped": int(row.get("skipped") or 0),
                    "reason": str(row.get("reason") or "")[:400],
                }
            )
    counts = value.get("counts") if isinstance(value.get("counts"), Mapping) else {}
    raw_reasons = value.get("not_adopted_reasons")
    not_adopted_reasons: list[dict[str, Any]] = []
    if isinstance(raw_reasons, list):
        for row in raw_reasons:
            if not isinstance(row, Mapping):
                continue
            reason = str(row.get("reason") or "").strip()
            if reason:
                not_adopted_reasons.append({"reason": reason[:300], "count": int(row.get("count") or 0)})
    raw_conflicts = value.get("group_conflicts")
    group_conflicts: list[dict[str, Any]] = []
    if isinstance(raw_conflicts, list):
        for row in raw_conflicts:
            if not isinstance(row, Mapping):
                continue
            unit_id = str(row.get("unit_id") or "")
            if not unit_id:
                continue
            group_conflicts.append(
                {
                    "unit_id": unit_id,
                    "card_ids": [str(item) for item in (row.get("card_ids") or [])],
                    "kind": str(row.get("kind") or ""),
                    "reason": str(row.get("reason") or "")[:300],
                }
            )
    raw_plan = value.get("plan")
    return {
        # The frozen plan (scope, batches, planned groups, resolution outcomes)
        # is carried verbatim-shaped: the commit phase validates it against the
        # live cards, so re-shaping it here would silently drop the guard.
        "plan": copy.deepcopy(dict(raw_plan)) if isinstance(raw_plan, Mapping) else {},
        "committed": value.get("committed") is True,
        "revoked_decisions": [
            str(item) for item in (value.get("revoked_decisions") or []) if str(item).strip()
        ],
        # Decisions this commit deliberately kept although the round did not
        # re-confirm them (a check or a judgment that failed or was never paid
        # for): the operator can see that the reference survived on purpose.
        "kept_unrefreshed_decisions": [
            {
                "card_id": str(item.get("card_id") or ""),
                "reason": str(item.get("reason") or "")[:300],
            }
            for item in (value.get("kept_unrefreshed_decisions") or [])
            if isinstance(item, Mapping) and str(item.get("card_id") or "")
        ],
        "prepare_id": str(value.get("prepare_id") or ""),
        # Set when a hand retry finished a batch this prepare had recorded as
        # failed: the generation/check results are reusable now, but the operator
        # still has to re-preview before new references are adopted.
        "reference_refresh_required": value.get("reference_refresh_required") is True,
        "reference_refresh_required_at": str(value.get("reference_refresh_required_at") or ""),
        "mode": reference_mode({"reference_mode": value.get("mode")}),
        "status": status,
        "scope": [str(item) for item in (value.get("scope") or []) if isinstance(item, str)],
        "scope_fingerprint": str(value.get("scope_fingerprint") or ""),
        "baseline_revision": int(value.get("baseline_revision") or 0),
        "baseline_approved_version": int(value.get("baseline_approved_version") or 0),
        "unit_results": unit_results,
        "counts": {
            "adopted": int(counts.get("adopted") or 0),
            "skipped": int(counts.get("skipped") or 0),
            "unresolved": int(counts.get("unresolved") or 0),
            "protected": int(counts.get("protected") or 0),
            "ineligible": int(counts.get("ineligible") or 0),
            # A normalized record must not silently lose the failure counters:
            # they decide whether the preparation may be reported as complete.
            "failed_units": int(counts.get("failed_units") or 0),
            "eligible_units": int(counts.get("eligible_units") or 0),
            "uncovered_units": int(counts.get("uncovered_units") or 0),
            # Incremental bookkeeping (V2): which units and groups were reused
            # instead of being sent to a model again.
            "reused_units": int(counts.get("reused_units") or 0),
            "processed_units": int(counts.get("processed_units") or 0),
            "reused_groups": int(counts.get("reused_groups") or 0),
            "judged_groups": int(counts.get("judged_groups") or 0),
            # A2/A3 extras: an old check that was refreshed, one bounded lookup,
            # one unit's local judgment. They are counted apart from the base
            # requests and must survive normalization, or a page would report
            # work that really happened as zero.
            "refreshed_checks": int(counts.get("refreshed_checks") or 0),
            "recheck_pending": int(counts.get("recheck_pending") or 0),
            "lookup_rounds": int(counts.get("lookup_rounds") or 0),
            "lookup_hits": int(counts.get("lookup_hits") or 0),
            "lookup_refreshed": int(counts.get("lookup_refreshed") or 0),
            "lookup_misses": int(counts.get("lookup_misses") or 0),
            # A lookup that was already executed for the same content and the
            # same evidence, or one that did not fit this execution's input
            # bounds: neither is a request of this run.
            "lookup_settled": int(counts.get("lookup_settled") or 0),
            "lookup_deferred": int(counts.get("lookup_deferred") or 0),
            "lookup_failed": int(counts.get("lookup_failed") or 0),
            "lookup_rejected": int(counts.get("lookup_rejected") or 0),
            "local_groups": int(counts.get("local_groups") or 0),
            "local_units_judged": int(counts.get("local_units_judged") or 0),
            "local_units_oversized": int(counts.get("local_units_oversized") or 0),
            "local_units_reused": int(counts.get("local_units_reused") or 0),
            "local_units_pending": int(counts.get("local_units_pending") or 0),
            "budget_pending": int(counts.get("budget_pending") or 0),
            # A5 split: what the AI actually answered versus what stayed open,
            # and the units a finished batch found nothing for.
            "ai_resolved_cards": int(counts.get("ai_resolved_cards") or 0),
            "resolved_questions": int(counts.get("resolved_questions") or 0),
            "remaining_questions": int(counts.get("remaining_questions") or 0),
            "no_candidate_units": int(counts.get("no_candidate_units") or 0),
        },
        "recheck_reasons": {
            str(key): int(value or 0)
            for key, value in (
                counts.get("recheck_reasons")
                if isinstance(counts.get("recheck_reasons"), Mapping)
                else {}
            ).items()
        },
        # The executed lookups of this run (input identity + how they ended).
        # Without them a normalized record would forget what was already asked
        # and the next confirmation would pay for the same lookup again.
        "lookups": [
            row
            for row in (
                lookup_ledger_row(raw)
                for raw in (value.get("lookups") or [])[-MAX_LOOKUP_LEDGER:]
            )
            if row is not None
        ],
        # The per-card dedup state of executed lookups. It survives a reopen and
        # is deliberately not trimmed by the display log above, so an early
        # answered question never gets paid for again.
        "lookup_state": lookup_state_of(value.get("lookup_state")),
        # The one shared pool of extra logical requests: what it allowed, what it
        # paid for and by kind. Kept even when nothing was spent, because "0 of 0"
        # and "0 of 10" are different statements about the same run.
        "budget": {
            "limit": int((value.get("budget") or {}).get("limit") or 0),
            "used": int((value.get("budget") or {}).get("used") or 0),
            "by_kind": {
                str(key): int(amount or 0)
                for key, amount in (
                    (value.get("budget") or {}).get("by_kind")
                    if isinstance((value.get("budget") or {}).get("by_kind"), Mapping)
                    else {}
                ).items()
            },
        },
        # Why candidates were not adopted, aggregated and bounded so the page
        # can show real reasons instead of only totals.
        "not_adopted_reasons": not_adopted_reasons,
        # Units that two readings of one expression both claimed. The program
        # drops such a unit for everyone; the record keeps why.
        "group_conflicts": group_conflicts,
        "requests": {
            "generation": int((value.get("requests") or {}).get("generation") or 0),
            "check": int((value.get("requests") or {}).get("check") or 0),
            "resolution": int((value.get("requests") or {}).get("resolution") or 0),
            "repair_rounds": int((value.get("requests") or {}).get("repair_rounds") or 0),
        },
        "errors": [str(item)[:300] for item in (value.get("errors") or []) if str(item).strip()],
        "next_group": str(value.get("next_group") or ""),
        "started_at": str(value.get("started_at") or ""),
        "finished_at": str(value.get("finished_at") or ""),
    }

def normalize_automation(value: Any) -> dict[str, Any]:
    """Read the stored automation object tolerantly without writing it back."""

    state = empty_automation()
    if not isinstance(value, Mapping):
        return state
    state["reference_revision"] = qs._coerce_revision(value.get("reference_revision"))
    state["prepare"] = normalize_prepare(value.get("prepare"))
    decisions = value.get("decisions")
    if isinstance(decisions, Mapping):
        for card_id, raw in decisions.items():
            if not isinstance(card_id, str) or not isinstance(raw, Mapping):
                continue
            decision = _normalize_decision(raw, card_id)
            if decision is not None:
                state["decisions"][card_id] = decision
    return state

def _normalize_decision(raw: Mapping[str, Any], card_id: str) -> dict[str, Any] | None:
    verdict = str(raw.get("verdict") or "").strip().casefold()
    if verdict not in DECISION_VERDICTS:
        return None
    evidence: list[dict[str, str]] = []
    for item in raw.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        evidence.append(
            {
                "unit_id": str(item.get("unit_id") or ""),
                "source_sha256": str(item.get("source_sha256") or ""),
            }
        )
    member_ids = [str(item) for item in (raw.get("member_ids") or []) if isinstance(item, str)]
    allowed = [str(item) for item in (raw.get("allowed_unit_ids") or []) if isinstance(item, str)]
    return {
        "card_id": str(raw.get("card_id") or card_id),
        "verdict": verdict,
        "representative_id": str(raw.get("representative_id") or ""),
        "reason": str(raw.get("reason") or "")[:400],
        "member_ids": member_ids,
        "allowed_unit_ids": allowed,
        "content_revision": qs._coerce_revision(raw.get("content_revision")),
        "content_fingerprint": str(raw.get("content_fingerprint") or ""),
        "evidence": evidence,
        "check_verdict": str(raw.get("check_verdict") or ""),
        "check_revision": qs._coerce_revision(raw.get("check_revision")),
        "check_reason": str(raw.get("check_reason") or "")[:300],
        # Which structured binding entries this adoption was derived from.
        "binding_ids": [
            str(item) for item in (raw.get("binding_ids") or []) if isinstance(item, str)
        ],
        "assessment_fingerprint": str(raw.get("assessment_fingerprint") or ""),
        "prepare_id": str(raw.get("prepare_id") or ""),
        "decided_at": str(raw.get("decided_at") or ""),
    }

def automation_of(support: Mapping[str, Any]) -> dict[str, Any]:
    """Read the automation object of one quality support mapping."""

    return normalize_automation(support.get("automation"))

def current_decisions(support: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return automation_of(support)["decisions"]

# --------------------------------------------------------------------------
# manual protection
# --------------------------------------------------------------------------

#: The reason the concept page writes when a human saves an edited draft.
HUMAN_EDIT_REASON = "人工修改后，旧的 AI 检查结论已过期。"

def is_human_edited(card: Mapping[str, Any]) -> bool:
    """Whether the card carries the formal human-edit marker.

    The edit path stamps the check with an ``unchecked`` verdict and this exact
    reason. That marker is what makes "a human owns this card" provable from the
    data alone — it never has to be guessed from an origin or a timestamp.
    """

    if not isinstance(card, Mapping):
        return False
    check = card.get("check")
    if not isinstance(check, Mapping):
        return False
    if str(check.get("verdict") or "") != "unchecked":
        return False
    reasons = [str(item) for item in (check.get("reasons") or [])]
    return any(HUMAN_EDIT_REASON in reason for reason in reasons)

def mark_human_owned_card(card: dict[str, Any]) -> bool:
    """Persist the minimal protection marker for a formally edited card.

    Called before an automatic write touches the card, so a saved manual edit
    exits automatic management even when the marker was never written before
    (older data). Unknown provenance stays conservative: nothing is inferred to
    be safe to overwrite.
    """

    if not is_human_edited(card):
        return False
    if card.get("manual_protected") is True:
        return False
    card["manual_protected"] = True
    return True

def is_manual_protected(card: Mapping[str, Any]) -> bool:
    """Whether automatic management must keep its hands off this card.

    Protection is conservative: a human approved copy, an explicit manual
    marker, or a human decision (deferred/rejected) all protect the card. A card
    whose history cannot be established is protected as well — the module never
    guesses that a card is safe to overwrite. A plain pending scan draft with no
    human signal is the only case that stays under automatic management.
    """

    if not isinstance(card, Mapping):
        return True
    if card.get("manual_protected") is True:
        return True
    if is_human_edited(card):
        return True
    if isinstance(card.get("approved"), Mapping):
        return True
    status = str(card.get("status") or "")
    if status in ("deferred", "rejected"):
        return True
    if isinstance(card.get("approved_at"), str) and card["approved_at"].strip():
        return True
    return False

def set_card_protection(support: dict[str, Any], card_id: str, protected: bool) -> dict[str, Any]:
    """Set or clear the minimal explicit manual marker. Never changes the card id."""

    cards = support.setdefault("cards", {})
    card = cards.get(str(card_id))
    if not isinstance(card, dict):
        raise AutomationError(f"找不到概念卡 {card_id}。")
    if protected:
        card["manual_protected"] = True
    else:
        card.pop("manual_protected", None)
    return card

# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------

def _member_evidence_units(member: Mapping[str, Any]) -> list[str]:
    """The units one frozen group member carries evidence for.

    A member is frozen as ``{card_id, content_revision, payload}`` by the
    prepare plan, while a bare card payload is the same content one level up;
    both shapes are read here so a caller can never get "no contention" because
    it wrapped the card differently.
    """

    content = member.get("payload") if isinstance(member.get("payload"), Mapping) else member
    units: list[str] = []
    for item in content.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        unit_id = str(item.get("unit_id") or "")
        if unit_id and unit_id not in units:
            units.append(unit_id)
    return units

def local_contention_plans(
    group_id: str,
    members: Sequence[Mapping[str, Any]],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    unit_ids: Sequence[str],
    max_cards: int = MAX_RESOLUTION_CARDS,
) -> dict[str, Any]:
    """Per-unit contention sets for one group that is too large to judge whole.

    The set for a unit keeps **every** member that carries valid evidence for
    that unit — supported, disputed and undecided alike — so nothing convenient
    is filtered out before "no conflict" is claimed. A set larger than
    ``max_cards`` is never truncated: the unit is reported as oversized and no
    request is issued for it.
    """

    limit = max(1, int(max_cards))
    by_unit: dict[str, set[str]] = {}
    for member in members:
        card_id = str(member.get("card_id") or member.get("id") or "")
        if not card_id:
            continue
        for unit_id in _member_evidence_units(member):
            if unit_id in unit_sources:
                by_unit.setdefault(unit_id, set()).add(card_id)
    requests: list[dict[str, Any]] = []
    oversized: list[str] = []
    for unit_id in [str(item) for item in unit_ids]:
        card_ids = sorted(by_unit.get(unit_id) or [])
        if not card_ids:
            continue
        if len(card_ids) > limit:
            oversized.append(unit_id)
            continue
        requests.append(
            {
                "group_id": str(group_id),
                "unit_id": unit_id,
                "member_ids": card_ids,
                "request_fingerprint": "",
            }
        )
    return {
        "group_id": str(group_id),
        "requests": requests,
        "oversized_units": oversized,
        "member_count": len(members),
        "max_cards": limit,
    }

def local_judgment_payloads(outcome: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The per-unit local judgments a previous run recorded, by unit id.

    One oversized group is judged unit by unit, and each of those judgments is
    complete **for its own unit**. The outcome therefore keeps them apart
    (``units``) instead of only the merged whole-group payload: the next run can
    hand over the units that were really judged and continue with the rest, and
    can never mistake a partial group for a finished one. A missing or foreign
    map yields an empty result — nothing is guessed from the merged payload.
    """

    if not isinstance(outcome, Mapping):
        return {}
    rows = outcome.get("units")
    if not isinstance(rows, Mapping):
        return {}
    payloads: dict[str, dict[str, Any]] = {}
    for unit_id, payload in rows.items():
        key = str(unit_id or "")
        if key and isinstance(payload, Mapping):
            payloads[key] = copy.deepcopy(dict(payload))
    return payloads

#: How one executed lookup ended. Every one of them is "already asked for this
#: exact input", so none of them may be repeated on identical input.

def merge_local_judgments(
    group_id: str,
    *,
    judgements: Sequence[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """One group payload built from per-unit local judgments.

    Each local judgment is complete over its own unit's contention set, so the
    merge may only restate what those sets already said **for their own unit**:
    ``applied_units`` is the union restricted to the judged units, and every
    equivalence class keeps the unit it was judged for, so a class from one
    unit's set can never claim another unit. Nothing is truncated and no member
    is dropped — a member without a claim simply holds no unit for this group.

    A mix of ``equivalent`` and ``distinct`` local judgments cannot be expressed
    as one group relation without inventing a statement neither judgment made,
    so it returns ``None`` and the group stays unjudged instead of guessed.
    """

    rows = [
        row
        for row in judgements or []
        if isinstance(row, Mapping)
        and isinstance(row.get("payload"), Mapping)
        and str(row.get("unit_id") or "")
    ]
    if not rows:
        return None
    relations = {str(row["payload"].get("relation") or "") for row in rows}
    if len(relations) != 1:
        return None
    relation = next(iter(relations))
    if relation not in ("equivalent", "distinct"):
        return None
    member_ids = [str(item.get("card_id") or "") for item in members if str(item.get("card_id") or "")]
    applied: dict[str, list[str]] = {}
    equivalent: list[dict[str, Any]] = []
    distinct: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        unit_id = str(row.get("unit_id") or "")
        raw_applied = payload.get("applied_units")
        for card_id, units in (raw_applied if isinstance(raw_applied, Mapping) else {}).items():
            card_id = str(card_id)
            for unit in units or []:
                # A local judgment speaks for its own unit only; anything else
                # would smuggle a whole-group claim through a per-unit answer.
                if str(unit) != unit_id:
                    continue
                units_now = applied.setdefault(card_id, [])
                if unit_id not in units_now:
                    units_now.append(unit_id)
        if relation == "equivalent":
            for item in payload.get("equivalent") or []:
                if not isinstance(item, Mapping):
                    continue
                equivalent.append(
                    {
                        "representative_id": str(item.get("representative_id") or ""),
                        "member_ids": [str(mid) for mid in (item.get("member_ids") or [])],
                        "reason": str(item.get("reason") or ""),
                        "unit_id": unit_id,
                    }
                )
        else:
            for item in payload.get("distinct") or []:
                if not isinstance(item, Mapping):
                    continue
                distinct.append(
                    {
                        "card_id": str(item.get("card_id") or ""),
                        "reason": str(item.get("reason") or ""),
                        "unit_id": unit_id,
                    }
                )
    if not applied:
        return None
    if relation == "equivalent" and not equivalent:
        return None
    if relation == "distinct" and not distinct:
        return None
    # Sorted, not "in the order the units happened to be judged": the merged
    # payload must be byte-identical for any order of the local judgments.
    equivalent.sort(key=lambda item: (item["unit_id"], item["representative_id"], item["member_ids"]))
    distinct.sort(key=lambda item: (item["unit_id"], item["card_id"]))
    return {
        "group_id": str(group_id),
        "members": sorted({card_id for card_id in member_ids if card_id}),
        "relation": relation,
        "equivalent": equivalent,
        "distinct": distinct,
        "unresolved": [],
        "applied_units": {card_id: sorted(units) for card_id, units in applied.items() if units},
    }

def adoption_eligibility(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    unit_ids: Sequence[str],
) -> tuple[str, str]:
    """Decide whether one card may become an automatic reference.

    Returns ``(verdict, reason)`` where verdict is one of ``adopt``,
    ``protected``, ``ineligible``. Every non-adopt verdict carries a short
    reason an operator can read.
    """

    if is_manual_protected(card):
        return "protected", "该卡有人工决定或已批准副本，自动准备不会改写它。"
    draft = card.get("draft")
    if not isinstance(draft, Mapping):
        return "ineligible", "当前卡片没有待审草稿。"
    try:
        normalized = qs.normalize_card_content(draft, unit_sources=unit_sources)
    except qs.QualitySupportError as exc:
        return "ineligible", f"内容不通过校验：{exc}"
    if not normalized.get("expressions"):
        return "ineligible", "内容没有可用表达。"
    check = card.get("check")
    if not isinstance(check, Mapping):
        return "ineligible", "当前草稿没有独立检查结论。"
    revision = int(card.get("draft_revision") or 0)
    if int(check.get("draft_revision") or 0) != revision:
        return "ineligible", "检查结论不是针对当前内容版本的。"
    if str(check.get("verdict") or "") != "supported":
        return "ineligible", "独立检查结论不是 supported。"
    blocks_all, _blocked_ids, block_reason = question_block(card, unit_sources=unit_sources)
    if blocks_all:
        return "ineligible", block_reason or "仍有关键待确认问题，未解决前不自动采用。"
    evidence_units = {
        str(item.get("unit_id"))
        for item in (normalized.get("evidence") or [])
        if str(item.get("unit_id") or "")
    }
    applicable = [str(unit) for unit in unit_ids if str(unit) in evidence_units]
    if not applicable:
        return "ineligible", "没有携带当前有效证据的适用单元。"
    return "adopt", "内容、证据与检查结论均有效，可自动采用。"

def applicable_unit_ids(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    unit_ids: Iterable[str],
) -> list[str]:
    """Units this card may cover, derived from its own verified evidence only."""

    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    evidence_units = {
        str(item.get("unit_id"))
        for item in (draft.get("evidence") or [])
        if isinstance(item, Mapping) and str(item.get("unit_id") or "")
    }
    return [str(unit) for unit in unit_ids if str(unit) in evidence_units and str(unit) in unit_sources]

# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def _content_fingerprint(card: Mapping[str, Any]) -> str:
    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    try:
        return qs.content_signature(draft)
    except Exception:  # pragma: no cover - signature is total for mappings
        return ""

def _evidence_pairs(card: Mapping[str, Any]) -> list[dict[str, str]]:
    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    pairs: list[dict[str, str]] = []
    for item in draft.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        pairs.append(
            {
                "unit_id": str(item.get("unit_id") or ""),
                "source_sha256": str(item.get("source_sha256") or ""),
            }
        )
    return pairs

def make_decision(
    card: Mapping[str, Any],
    *,
    verdict: str,
    reason: str,
    allowed_unit_ids: Sequence[str],
    unit_sources: Mapping[str, tuple[str, str]],
    prepare_id: str,
    representative_id: str = "",
    member_ids: Sequence[str] | None = None,
    known_card_ids: Iterable[str] | None = None,
    now_iso_value: str | None = None,
) -> dict[str, Any]:
    """Build one validated decision record for ``card``.

    Rejects unknown verdicts, missing members and any unit scope the card's own
    current evidence cannot support — the program, not the model, owns the
    scope. A decision never fabricates an approved copy, and every referenced
    card must already exist in the caller-provided pool (pass
    ``known_card_ids`` when the decision may name other members).
    """

    if not isinstance(card, Mapping) or not str(card.get("id") or ""):
        raise AutomationError("决定必须绑定一张已存在的概念卡。")
    verdict = str(verdict or "").strip().casefold()
    if verdict not in DECISION_VERDICTS:
        raise AutomationError(f"不支持的自动裁决：{verdict}")
    card_id = str(card["id"])
    member_ids = [str(item) for item in (member_ids if member_ids is not None else [card_id])]
    if card_id not in member_ids:
        raise AutomationError("决定必须包含卡片自身。")
    if known_card_ids is not None:
        known = {str(item) for item in known_card_ids}
        unknown = [item for item in member_ids if item not in known]
        if unknown:
            raise AutomationError(f"决定引用了不存在的成员卡：{'、'.join(unknown)}")
    elif len(member_ids) != 1:
        raise AutomationError("未提供成员卡全集时，决定只能引用卡片自身。")
    allowed = [str(item) for item in allowed_unit_ids]
    if verdict == ADOPTED_VERDICT:
        if not allowed:
            raise AutomationError("采用决定必须至少覆盖一个携带有效证据的单元。")
        for unit_id in allowed:
            source = unit_sources.get(unit_id)
            if not isinstance(source, tuple) or len(source) != 2:
                raise AutomationError(f"采用决定引用了不存在的单元 {unit_id}。")
        own = set(applicable_unit_ids(card, unit_sources=unit_sources, unit_ids=allowed))
        for unit_id in allowed:
            if unit_id not in own:
                raise AutomationError(f"卡片没有单元 {unit_id} 的当前有效证据。")
    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    check = card.get("check") if isinstance(card.get("check"), Mapping) else {}
    live_assessment = assessment_of(card, unit_sources=unit_sources)
    check_reason = "；".join(str(item) for item in (check.get("reasons") or []) if str(item).strip())
    return {
        "card_id": card_id,
        "verdict": verdict,
        "representative_id": str(representative_id or ""),
        "reason": str(reason or "")[:400],
        "member_ids": member_ids,
        "allowed_unit_ids": allowed,
        "content_revision": int(card.get("draft_revision") or 0),
        "content_fingerprint": _content_fingerprint(card),
        "evidence": _evidence_pairs(card),
        "check_verdict": str(check.get("verdict") or ""),
        "check_revision": int(check.get("draft_revision") or 0),
        "check_reason": check_reason[:300],
        "binding_ids": [
            str(item["binding_id"])
            for item in adoptable_bindings(card, unit_sources=unit_sources)
        ],
        # The assessment content this adoption was derived from: a later change
        # to a preferred translation, guidance or basis invalidates the decision
        # instead of silently injecting the new content.
        # Only a card with a live structured result records an identity: a truly
        # legacy decision stays legacy and keeps its old reading.
        "assessment_fingerprint": (
            assessment_fingerprint(live_assessment) if live_assessment else ""
        ),
        "prepare_id": str(prepare_id or ""),
        "decided_at": now_iso_value or now_iso(),
        "draft": copy.deepcopy(dict(draft)),
    }

def decision_problems(
    decision: Mapping[str, Any],
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[str]:
    """Why one stored decision no longer applies to the live card, if any."""

    problems: list[str] = []
    if not isinstance(decision, Mapping) or not isinstance(card, Mapping):
        return ["决定或卡片缺失。"]
    if str(decision.get("verdict") or "") != ADOPTED_VERDICT:
        problems.append("该决定不是采用决定。")
    if str(decision.get("card_id") or "") != str(card.get("id") or ""):
        problems.append("决定与卡片不匹配。")
    if int(decision.get("content_revision") or 0) != int(card.get("draft_revision") or 0):
        problems.append("卡片内容版本已变化。")
    elif str(decision.get("content_fingerprint") or "") and _content_fingerprint(card) != str(
        decision.get("content_fingerprint")
    ):
        problems.append("卡片内容与采用时的指纹不一致。")
    check = card.get("check") if isinstance(card.get("check"), Mapping) else {}
    if str(check.get("verdict") or "") != "supported":
        problems.append("当前检查结论不是 supported。")
    if int(check.get("draft_revision") or 0) != int(card.get("draft_revision") or 0):
        problems.append("检查结论不是针对当前内容版本的。")
    for evidence in decision.get("evidence") or []:
        unit_id = str(evidence.get("unit_id") or "")
        source = unit_sources.get(unit_id)
        if not isinstance(source, tuple) or len(source) != 2:
            problems.append(f"证据单元 {unit_id} 已不存在。")
            continue
        if str(evidence.get("source_sha256") or "") != str(source[1]):
            problems.append(f"证据单元 {unit_id} 的源文已变化。")
    for unit_id in decision.get("allowed_unit_ids") or []:
        if str(unit_id) not in unit_sources:
            problems.append(f"适用范围 {unit_id} 已不存在。")
    if is_manual_protected(card):
        problems.append("该卡已有人工决定或批准内容。")
    # A stored decision predating the structured protocol keeps its legacy
    # reading; only a live structured result that now blocks is a new problem.
    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    recorded = str(decision.get("assessment_fingerprint") or "")
    live_assessment = assessment_of(card, unit_sources=unit_sources)
    if live_assessment is not None:
        # A card does not need open questions for its structured result to matter:
        # the answers and bindings are what the reference says, so any content
        # change invalidates the stored decision.
        if not recorded:
            # The card grew a structured result the decision never adopted (for
            # example a legacy decision plus a later check): the old decision
            # pauses until a locked commit stores the matching new decision.
            problems.append(
                "该卡已升级为结构化检查结论，旧决定需在重新准备并提交后才能再次采用。"
            )
        elif recorded != assessment_fingerprint(live_assessment):
            problems.append(
                "检查结论的内容（首选译名/指导/适用依据或其证据）已变化，旧决定不再适用。"
            )
        blocks_all, _blocked, _reason = question_block(card, unit_sources=unit_sources)
        if blocks_all:
            problems.append("结构化疑问结论已变化，未解决的问题再次阻止该卡。")
    elif recorded:
        # The decision was derived from a structured result that is no longer
        # usable (invalid identity, stale evidence, or a removed check). A truly
        # legacy decision never recorded a fingerprint and keeps its old reading.
        problems.append("结构化检查结论已失效或身份不一致，旧决定不再适用。")
    return problems

def set_decision(support: dict[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    """Store one decision in a support mapping and bump the reference revision."""

    state = support.setdefault("automation", empty_automation())
    normalized_state = normalize_automation(state)
    card_id = str(decision.get("card_id") or "")
    if not card_id:
        raise AutomationError("决定缺少 card_id。")
    stored = copy.deepcopy(dict(decision))
    stored.pop("draft", None)
    normalized_state["decisions"][card_id] = stored
    normalized_state["reference_revision"] = int(normalized_state.get("reference_revision") or 0) + 1
    support["automation"] = normalized_state
    return stored

def clear_automation(support: dict[str, Any], *, reason: str = "") -> None:
    """Drop every automatic decision (used when the mode returns to manual)."""

    state = normalize_automation(support.get("automation"))
    if not state["decisions"] and not state["prepare"]:
        return
    state["decisions"] = {}
    state["reference_revision"] = int(state["reference_revision"]) + 1
    if state["prepare"] is not None:
        state["prepare"]["status"] = "stale"
        state["prepare"]["status_note"] = reason[:200]
    support["automation"] = state

# --------------------------------------------------------------------------
# automatic reference view
# --------------------------------------------------------------------------

def reference_payload(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    unit_id: str = "",
) -> dict[str, Any] | None:
    """The reference content one card may inject **into one target unit**.

    A card without questions keeps its draft verbatim (the historical path).
    A card whose structured assessment answered its questions injects only the
    bindings that (a) survived every unresolved question and (b) carry their own
    verified evidence for *this* unit. Nothing of the card is merged back: the
    excluded part's expressions, translations, meaning and open questions are
    all dropped, while the expression-to-translation pairing is kept explicitly.
    ``None`` means "nothing safe for this unit".
    """

    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else None
    if not isinstance(draft, Mapping):
        return None
    assessment = assessment_of(card, unit_sources=unit_sources)
    if assessment is None:
        questions = [
            str(item).strip() for item in (draft.get("open_questions") or []) if str(item).strip()
        ]
        return None if questions else copy.deepcopy(dict(draft))

    bindings = adoptable_bindings(card, unit_sources=unit_sources)
    wanted = str(unit_id or "")
    applicable: list[dict[str, Any]] = []
    for binding in bindings:
        evidence_units = {
            str(item.get("unit_id") or "") for item in (binding.get("evidence") or [])
        }
        if wanted and wanted in evidence_units:
            applicable.append(binding)
    if not applicable:
        return None

    everything_applies = len(applicable) == len(assessment["bindings"])
    expressions: list[str] = []
    preferred: list[str] = []
    variants: list[str] = []
    conditions: list[str] = []
    carried: list[dict[str, Any]] = []
    for binding in applicable:
        for expression in binding["expressions"]:
            if expression not in expressions:
                expressions.append(expression)
        if binding["preferred_translation"] and binding["preferred_translation"] not in preferred:
            preferred.append(binding["preferred_translation"])
        for variant in binding["acceptable_variants"]:
            if variant not in variants and variant not in preferred:
                variants.append(variant)
        condition = str(binding.get("applies_when") or "").strip() or str(
            binding.get("guidance") or ""
        ).strip()
        if condition and condition not in conditions:
            conditions.append(condition)
        carried.append(
            {
                "binding_id": binding["binding_id"],
                "expressions": list(binding["expressions"]),
                "kind": binding["kind"],
                "preferred_translation": binding["preferred_translation"],
                "acceptable_variants": list(binding["acceptable_variants"]),
                "guidance": binding["guidance"],
                "applies_when": binding["applies_when"],
            }
        )

    payload: dict[str, Any] = {
        "expressions": expressions,
        "preferred_translations": preferred,
        "acceptable_translations": list(dict.fromkeys(preferred + variants)),
        "bindings": carried,
        "open_questions": [],
        "priority": int(draft.get("priority") or 0),
        "meaning": "",
        "confusions": [],
        "applies_when": "；".join(conditions),
    }
    if everything_applies:
        # Only a card whose every binding is safe for this unit may carry the
        # whole-card meaning and confusions: otherwise that prose can describe
        # the excluded interpretation.
        payload["meaning"] = str(draft.get("meaning") or "")
        payload["confusions"] = [str(item) for item in (draft.get("confusions") or [])]
    return payload

def auto_reference_cards(
    support: Mapping[str, Any],
    *,
    unit_id: str,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Cards that may serve as automatic references for one unit.

    A decision applies only when it is an ``adopt`` decision, its content
    version and evidence still match the live state, the unit is inside the
    decision's verified scope, and the card is not under manual protection.
    Manual cards are never produced here: the human reference path stays the
    caller's responsibility so the two origins can be merged with manual first.
    """

    cards = support.get("cards") or {}
    results: list[dict[str, Any]] = []
    for card_id, decision in sorted(current_decisions(support).items()):
        card = cards.get(card_id)
        if not isinstance(card, dict):
            continue
        if str(decision.get("verdict") or "") != ADOPTED_VERDICT:
            continue
        if str(unit_id) not in [str(item) for item in decision.get("allowed_unit_ids") or []]:
            continue
        if decision_problems(decision, card, unit_sources=unit_sources):
            continue
        draft = card.get("draft")
        if not isinstance(draft, dict):
            continue
        payload = reference_payload(card, unit_sources=unit_sources, unit_id=str(unit_id))
        if payload is None:
            # The card raised questions and no safe binding survives them: it
            # must not be offered as an automatic reference.
            continue
        results.append(
            {
                "card_id": card_id,
                "origin": "automatic",
                "content_revision": int(card.get("draft_revision") or 0),
                "card_revision": int(decision.get("content_revision") or 0),
                "decision_id": str(decision.get("prepare_id") or ""),
                "bindings": [
                    str(item.get("binding_id") or "") for item in (payload.get("bindings") or [])
                ],
                "payload": payload,
            }
        )
    return results

def automatic_reference_view(
    support: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    mode: str = "automatic",
) -> dict[str, dict[str, Any]]:
    """Read-only view of what every stored decision means for the live project.

    The injection predicates decide ``live`` here as well — the verdict, the
    same :func:`decision_problems` check and a surviving per-unit payload — so
    ``live_units`` is exactly the set of units this card can enter the reference
    **selection** for. It is a statement about the candidate pool, not about
    injection: the selection then applies the unit's expression match, ranks
    manual references first and drops whole cards that do not fit the 6-card /
    6000-character budget, so a live card can still be omitted from what one
    unit actually received. The unit's frozen snapshot (``quality_reference``)
    is the record of what was used; this view never claims more. Only units that
    already exist in ``unit_sources`` can count, and a project back in manual
    mode offers no automatic reference at all.

    Nothing is written: cards, decisions and revisions are untouched, and an
    automatic decision is never reported as a human approval. Callers use this
    to *show* the automatic channel next to the manual one, never to store it.
    """

    cards = support.get("cards") or {}
    automatic_mode = str(mode or "").strip().casefold() == "automatic"
    view: dict[str, dict[str, Any]] = {}
    for card_id, decision in sorted(current_decisions(support).items()):
        card = cards.get(card_id)
        if not isinstance(card, Mapping):
            continue
        problems = decision_problems(decision, card, unit_sources=unit_sources)
        if not automatic_mode:
            problems = ["项目当前处于人工模式，自动参考不会注入。", *problems]
        scope = [str(item) for item in decision.get("allowed_unit_ids") or []]
        live_units = (
            [
                unit_id
                for unit_id in scope
                if unit_id in unit_sources
                and reference_payload(card, unit_sources=unit_sources, unit_id=unit_id)
                is not None
            ]
            if automatic_mode and not problems
            else []
        )
        view[card_id] = {
            "decision_id": str(decision.get("prepare_id") or ""),
            "decided_at": str(decision.get("decided_at") or ""),
            "content_revision": int(decision.get("content_revision") or 0),
            "check_verdict": str(decision.get("check_verdict") or ""),
            "scope_units": scope,
            "live_units": live_units,
            "live": bool(live_units),
            "problems": problems,
        }
    return view

# --------------------------------------------------------------------------
# writing paths
# --------------------------------------------------------------------------

def candidate_card_id(content: Mapping[str, Any]) -> str:
    """The card id a candidate would create or refresh (never recomputed later)."""

    expressions = [str(item) for item in (content.get("expressions") or []) if str(item).strip()]
    return qs.card_id_for(expressions, content.get("meaning"))

def upsert_automatic_draft(
    support: dict[str, Any],
    content: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    prepare_id: str,
    unit_ids: Sequence[str],
    now_iso_value: str,
    check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one generated candidate as a draft unless a human owns the card.

    This is the path that must not secretly overwrite manual work: a protected
    card keeps its draft, its revision and its status, and the caller receives
    ``outcome="protected"``. Otherwise the candidate goes through the existing
    duplicate-aware writer so the exact-duplicate zero-write behaviour (case,
    whitespace, fullwidth) is untouched.
    """

    target_id = candidate_card_id(content)
    existing = (support.get("cards") or {}).get(target_id)
    if isinstance(existing, dict) and is_manual_protected(existing):
        return {
            "outcome": "protected",
            "card_id": target_id,
            "reason": "该卡有人工决定或批准内容，自动准备不覆盖。",
        }
    return qs.upsert_candidate(
        support,
        content,
        unit_sources=unit_sources,
        batch_id=str(prepare_id or "prepare"),
        unit_ids=list(unit_ids),
        now_iso_value=now_iso_value,
        check=check,
    )

def prepare_fingerprint(
    units: Sequence[Mapping[str, Any]],
) -> str:
    """Stable fingerprint of one prepare scope: unit id + source hash, sorted."""

    rows = sorted(
        f"{str(unit.get('id') or '')}:{str(unit.get('source_sha256') or '')}"
        for unit in units
        if str(unit.get("id") or "")
    )
    return qs.content_signature({"expressions": rows}) if rows else ""

def group_input_fingerprint(
    group_id: str,
    members: Sequence[Mapping[str, Any]],
    support: Mapping[str, Any],
    unit_sources: Mapping[str, tuple[str, str]],
) -> str:
    """Fingerprint of everything one group judgment is allowed to read.

    The prepare guard only knows the project, the mode and the prepare id, so a
    formal human edit (or an approval, a protection change or a re-imported
    source) leaves it untouched. This fingerprint closes that hole: it covers
    every member's frozen card id, content revision and content, the *live*
    provenance of that card (status, approval, protection, check conclusion and
    current draft revision) and the live source hash of every unit the frozen
    evidence points at.

    It is computed once when the group is frozen and recomputed before the
    judgment is used. The prepare's own bookkeeping (record, counters, revision)
    is deliberately not part of it, so the run's own writes never invalidate
    themselves; only a change to what the model was shown does.
    """

    cards = (support or {}).get("cards") or {}
    rows: list[str] = []
    referenced: set[str] = set()
    for member in sorted(members, key=lambda item: str(item.get("card_id") or "")):
        card_id = str(member.get("card_id") or "")
        if not card_id:
            continue
        card = cards.get(card_id)
        card = card if isinstance(card, Mapping) else {}
        payload = member.get("payload") if isinstance(member.get("payload"), Mapping) else {}
        check = card.get("check") if isinstance(card.get("check"), Mapping) else {}
        for evidence in payload.get("evidence") or []:
            if isinstance(evidence, Mapping) and str(evidence.get("unit_id") or ""):
                referenced.add(str(evidence.get("unit_id")))
        rows.append(
            "|".join(
                (
                    card_id,
                    f"frozen@{int(member.get('content_revision') or 0)}",
                    qs.content_signature(payload),
                    f"live@{int(card.get('draft_revision') or 0)}",
                    str(card.get("status") or ""),
                    "approved" if card.get("approved") else "-",
                    f"v{int(card.get('approved_version') or 0)}",
                    "protected" if is_manual_protected(card) else "-",
                    f"check:{str(check.get('verdict') or '')}@{int(check.get('draft_revision') or 0)}",
                )
            )
        )
    sources = sorted(
        f"{unit_id}:{str(unit_sources[unit_id][1])}"
        for unit_id in referenced
        if unit_id in unit_sources
    )
    if not rows:
        return ""
    return qs.content_signature({"expressions": [f"group:{group_id}", *rows, "sources:", *sources]})

def group_key(expressions: Sequence[str]) -> str:
    """Deterministic key of one related-expression group."""

    keys = sorted({qs._orthographic_expression_key(item) for item in expressions if str(item).strip()})
    return "|".join(keys)

# --------------------------------------------------------------------------
# incremental reuse (V2)
# --------------------------------------------------------------------------

def prior_unit_coverage(record: Any) -> dict[str, str]:
    """Unit id -> the source hash a previous preparation really finished.

    Only rows that reached the end of the generation + check work count. The
    stored ``scanned_unit_ids`` set is deliberately not consulted: it only says
    "some scan touched this unit", carries no source version and proves nothing
    about the automatic preparation, so it can never make a unit reusable.
    """

    if not isinstance(record, Mapping):
        return {}
    if str(record.get("status") or "") not in REUSABLE_PREPARE_STATUSES:
        return {}
    coverage: dict[str, str] = {}
    for row in record.get("unit_results") or []:
        if not isinstance(row, Mapping):
            continue
        unit_id = str(row.get("unit_id") or "")
        source_sha256 = str(row.get("source_sha256") or "")
        if unit_id and source_sha256 and str(row.get("status") or "") in FINISHED_UNIT_STATUSES:
            coverage[unit_id] = source_sha256
    return coverage

def prior_group_judgments(record: Any) -> dict[str, dict[str, Any]]:
    """Group id -> the judgment a previous preparation may hand over.

    A judgment is reusable only when the run that produced it may feed the next
    one, the outcome is a real relation (not "skipped"/"failed") and the frozen
    input fingerprint is recorded. The caller still compares that fingerprint
    with the live one before using the payload.
    """

    if not isinstance(record, Mapping):
        return {}
    if str(record.get("status") or "") not in REUSABLE_PREPARE_STATUSES:
        return {}
    plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    fingerprints: dict[str, str] = {}
    for group in plan.get("groups") or []:
        if isinstance(group, Mapping):
            fingerprints[str(group.get("group_id") or "")] = str(group.get("input_fingerprint") or "")
    judgments: dict[str, dict[str, Any]] = {}
    for group_id, outcome in (plan.get("resolved_groups") or {}).items():
        group_id = str(group_id)
        if not isinstance(outcome, Mapping):
            continue
        if str(outcome.get("relation") or "") not in REUSABLE_RELATIONS:
            continue
        fingerprint = fingerprints.get(group_id) or ""
        if not fingerprint:
            continue
        judgments[group_id] = {
            "input_fingerprint": fingerprint,
            "prepare_id": str(record.get("prepare_id") or ""),
            "outcome": copy.deepcopy(dict(outcome)),
        }
    return judgments

def group_unit_assignments(
    group_id: str,
    outcome: Mapping[str, Any] | None,
    *,
    members: Sequence[Mapping[str, Any]],
    unit_sources: Mapping[str, tuple[str, str]],
    unit_ids: Sequence[str],
) -> dict[str, Any]:
    """Which member of one group may represent it in which unit.

    The judgment owns the relation, the program owns the scope: a unit is
    assigned to a member only when that member carries verified evidence for the
    unit *and* the judgment listed the unit for it. At most one member is
    assigned per unit, so two mutually exclusive readings of the same
    expression can never be injected into the same unit; a unit that two
    distinct senses both claim is dropped for all of them instead of letting the
    processing order decide.

    Nothing is copied between members: each assignment carries the member's own
    evidence unit, and the caller still validates it against the live card.
    """

    scope = [str(unit) for unit in unit_ids]
    payload: Mapping[str, Any] = {}
    if isinstance(outcome, Mapping) and isinstance(outcome.get("payload"), Mapping):
        payload = outcome["payload"]
    relation = str((outcome or {}).get("relation") or "") if isinstance(outcome, Mapping) else ""

    own: dict[str, list[str]] = {}
    for member in members or []:
        if not isinstance(member, Mapping):
            continue
        card_id = str(member.get("card_id") or "")
        if not card_id:
            continue
        content = member.get("payload") if isinstance(member.get("payload"), Mapping) else {}
        evidence_units = {
            str(item.get("unit_id") or "")
            for item in (content.get("evidence") or [])
            if isinstance(item, Mapping) and str(item.get("unit_id") or "")
        }
        own[card_id] = [unit for unit in scope if unit in evidence_units and unit in unit_sources]

    declared_raw = payload.get("applied_units")
    declared = declared_raw if isinstance(declared_raw, Mapping) else {}
    effective: dict[str, list[str]] = {}
    for card_id, units in own.items():
        claimed = [str(unit) for unit in (declared.get(card_id) or [])]
        # "没有把握就留空": a member is only assigned to units the judgment named
        # for it, and only where its own verified evidence exists.
        effective[card_id] = [unit for unit in units if unit in claimed]

    assignments: dict[str, list[str]] = {card_id: [] for card_id in own}
    excluded: dict[str, str] = {}
    conflicted: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    representatives: list[str] = []
    unresolved_cards: list[str] = []

    if relation == "equivalent":
        # Claims are collected per equivalence class, not per card: only that
        # grouping can tell "the representative of this class" from "another
        # class of the same group claiming the same unit".
        class_claims: dict[str, dict[str, list[str]]] = {}
        class_representatives: dict[str, str] = {}
        for index, item in enumerate(payload.get("equivalent") or []):
            if not isinstance(item, Mapping):
                continue
            class_key = str(index)
            representative = str(item.get("representative_id") or "")
            class_representatives[class_key] = representative
            class_ids = [str(card_id) for card_id in item.get("member_ids") or []]
            if representative and representative not in class_ids:
                class_ids.append(representative)
            class_ids = [card_id for card_id in class_ids if card_id in own]
            if representative in own and representative not in representatives:
                representatives.append(representative)
            for card_id in dict.fromkeys(class_ids):
                for unit in effective.get(card_id) or []:
                    class_claims.setdefault(unit, {}).setdefault(class_key, []).append(card_id)

        for unit in sorted(class_claims):
            claimants_by_class = class_claims[unit]
            if len(claimants_by_class) > 1:
                # Two equivalence classes of one group both claim this unit. The
                # program cannot tell which reading belongs there, and it must
                # neither pick one class by card id nor merge the classes: the
                # unit is dropped for every claimant of every competing class.
                # The record is order-free: swapping the classes or their
                # members produces the same entry.
                card_ids = sorted({card_id for ids in claimants_by_class.values() for card_id in ids})
                conflicts.append(
                    {
                        "unit_id": unit,
                        "card_ids": card_ids,
                        "kind": "equivalent-classes",
                        "reason": "同一单元被两个等价类同时声明，该单元不采用这组参考。",
                        "classes": sorted(
                            sorted(claimants_by_class[class_key]) for class_key in claimants_by_class
                        ),
                        "representatives": sorted(
                            representative
                            for key in claimants_by_class
                            for representative in [class_representatives.get(key) or ""]
                            if representative
                        ),
                    }
                )
                for card_id in card_ids:
                    conflicted.setdefault(
                        card_id, "该卡所在的等价类与另一个等价类争用同一个单元，冲突单元不采用。"
                    )
                continue
            ((class_key, claimants),) = claimants_by_class.items()
            representative = class_representatives.get(class_key, "")
            # The representative wins a tie; otherwise the smallest card id
            # decides. Both are stable, so the outcome cannot depend on the
            # order in which the classes or their members were listed.
            ordered = sorted(set(claimants), key=lambda card_id: (card_id != representative, card_id))
            assignments[ordered[0]].append(unit)
    elif relation == "distinct":
        distinct_ids = [
            str(item.get("card_id") or "")
            for item in payload.get("distinct") or []
            if isinstance(item, Mapping)
        ]
        claims = {}
        for card_id in [item for item in distinct_ids if item in own]:
            for unit in effective.get(card_id) or []:
                claims.setdefault(unit, []).append(card_id)
        for unit in sorted(claims):
            claimants = sorted(claims[unit])
            if len(claimants) > 1:
                conflicts.append(
                    {
                        "unit_id": unit,
                        "card_ids": claimants,
                        "kind": "distinct",
                        "reason": "同一单元被两个义项同时声明，无法区分适用者，该单元不采用这组参考。",
                    }
                )
                continue
            assignments[claimants[0]].append(unit)

    judged = relation in ("equivalent", "distinct") and bool(
        payload.get("equivalent") or payload.get("distinct")
    )
    if not judged:
        unresolved_cards = sorted(own)
        for card_id in unresolved_cards:
            excluded[card_id] = "组辨析未确定，本次不采用该组的自动参考。"
    else:
        for card_id in [str(item) for item in payload.get("unresolved") or []]:
            if card_id in own:
                unresolved_cards.append(card_id)
                excluded[card_id] = "组辨析未确定，本次不采用该组的自动参考。"

    return {
        "group_id": str(group_id),
        "relation": relation,
        "assignments": {
            card_id: sorted(units) for card_id, units in assignments.items() if units
        },
        "excluded": excluded,
        # Cards that lost a unit to a class conflict. Kept apart from
        # ``excluded``: such a card may still hold other units its own class
        # claimed, so it must not be dropped as a whole.
        "conflicted": conflicted,
        "unresolved": sorted(set(unresolved_cards)),
        "representatives": sorted(set(representatives)),
        "conflicts": conflicts,
    }

def planned_groups(
    support: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    max_cards: int = MAX_RESOLUTION_CARDS,
) -> list[dict[str, Any]]:
    """Groups that need an AI judgment before adoption.

    A group exists when two or more cards share a definite expression key (the
    same ranking the review page uses). Groups larger than the call budget or
    without a common constraint that can be split safely are reported as
    ``oversized`` so the caller marks them unresolved instead of truncating.
    """

    buckets: dict[str, list[str]] = {}
    for card_id, card in (support.get("cards") or {}).items():
        if not isinstance(card, dict):
            continue
        content = card.get("draft") or card.get("approved") or {}
        for expression in content.get("expressions") or []:
            key = qs._orthographic_expression_key(expression)
            if not key:
                continue
            buckets.setdefault(key, [])
            if card_id not in buckets[key]:
                buckets[key].append(card_id)
    groups: list[dict[str, Any]] = []
    for key, card_ids in sorted(buckets.items()):
        card_ids = sorted(card_ids)
        if len(card_ids) < 2:
            continue
        groups.append(
            {
                "group_id": f"group-{key}"[:120],
                "key": key,
                "card_ids": card_ids,
                "oversized": len(card_ids) > max_cards,
            }
        )
    return groups

def prepare_scope_summary(
    support: Mapping[str, Any],
    *,
    units: Sequence[Mapping[str, Any]],
    unit_sources: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    """Read-only preview of one automatic prepare: no model call, no write."""

    groups = planned_groups(support, unit_sources=unit_sources)
    targeted: list[dict[str, Any]] = []
    for card_id, card in (support.get("cards") or {}).items():
        if not isinstance(card, dict) or is_manual_protected(card):
            continue
        verdict, reason = adoption_eligibility(
            card,
            unit_sources=unit_sources,
            unit_ids=[str(unit.get("id")) for unit in units],
        )
        targeted.append({"card_id": card_id, "verdict": verdict, "reason": reason})
    return {
        "unit_count": len(units),
        "scope_fingerprint": prepare_fingerprint(units),
        "group_count": len(groups),
        "oversized_groups": [group["group_id"] for group in groups if group["oversized"]],
        "candidate_count": len(targeted),
        "eligible_count": sum(1 for item in targeted if item["verdict"] == "adopt"),
    }
