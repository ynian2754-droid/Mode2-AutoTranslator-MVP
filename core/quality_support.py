"""Optional per-project translation quality support data.

This module holds the concept cards, their evidence verification, the
deterministic reference matcher used by translation and review, and the
reference snapshots that make "which concepts did this request actually use"
answerable after the fact.

It is deliberately a plain dict layer:

- it never writes project state by itself;
- it never talks to a model;
- an absent ``quality_support`` object means "feature empty", so legacy
  projects keep working without any migration.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

from core.concept_content import (
    QualitySupportError,
    normalize_expression,
    _fold_fullwidth_ascii,
    _orthographic_expression_key,
    card_id_for,
    _string_list,
    _bounded_text,
    _excerpt_matches,
    verify_evidence,
    normalize_card_content,
    _string_list_evidence_stub,
    _coerce_revision,
    _expression_pattern,
    _priority_value,
    _normalized_set,
    content_signature,
    _orthographic_signature,
    _NON_WORD_EDGE,
    _WHITESPACE_RUN_RE,
    _FULLWIDTH_ASCII_TRANS,
    _FULLWIDTH_ASCII_RE,
    _ALL_CAPS_ABBREVIATION_RE,
)

QUALITY_SUPPORT_SCHEMA_VERSION = 1

# Extraction scan defaults. There are deliberately no scan caps any more: the
# word target below only decides where a batch is split, and how many batches
# run at once is a client-side worker setting the operator can terminate at any
# time. MAX_CARDS_PER_BATCH / MAX_CHECK_CARDS_PER_BATCH were never wired to a
# scan path and stay unused.
DEFAULT_SCAN_SOURCE_WORDS = 4000
MAX_CARDS_PER_BATCH = 12
MAX_CHECK_CARDS_PER_BATCH = 12

# Bounded per-request reference injection.
# One batch approval/defer/reject request may carry at most this many cards.
# This is a write-operation boundary, not a scan limit.
MAX_BATCH_CARD_ACTIONS = 20

MAX_REFERENCE_CARDS = 6
MAX_REFERENCE_CHARS = 6000
MAX_EDITORIAL_SUGGESTIONS = 5

# Failed-batch recovery (one generation/check batch, retried by the operator).
# The record lives on the batch itself — no separate task store — and only these
# two steps can be resumed by hand.
BATCH_RETRY_STAGES = ("generation", "check")
#: ``failed`` and ``running`` are unresolved: the display cap must never truncate
#: them and the retry entry point may resume them. ``running`` found in storage
#: means the attempt was interrupted, so it needs a fresh confirmation and never
#: an automatic call.
UNRESOLVED_BATCH_RETRY_STATES = ("failed", "running")
BATCH_RETRY_STATES = (*UNRESOLVED_BATCH_RETRY_STATES, "completed", "stale")
#: How many resolved (completed/stale) batch entries are kept for display.
#: Unresolved recovery records are kept on top of this cap.
BATCH_HISTORY_LIMIT = 40

# Controlled text for the one duplicate outcome the scanner can produce: an
# incoming candidate whose normalized content and evidence are exactly what an
# existing card already holds.  It is never a semantic judgement.
DUPLICATE_CANDIDATE_REASON = "与既有概念卡的内容、证据完全一致，未重复进入审核队列。"

# Exact-duplicate matching runs two signatures and accepts either one:
#   * the pre-existing case/whitespace signature (``content_signature``), which
#     keeps the historical behaviour — including its known casefold limitation
#     for abbreviations such as US/us, which this module deliberately does not
#     "fix"; and
#   * the writing-variant signature (``_orthographic_signature``), which folds
#     fullwidth ASCII letters/digits in the expressions field and is the only
#     place the all-caps abbreviation guard applies.
# Neither signature is a semantic judgement; both are plain string comparisons.

CARD_DRAFT_STATUSES = {"pending_review", "deferred", "rejected"}
CHECK_VERDICTS = {"supported", "disputed", "insufficient", "unchecked"}
CARD_CONTENT_KEYS = {
    "expressions",
    "meaning",
    "applies_when",
    "acceptable_translations",
    "confusions",
    "evidence",
    "open_questions",
    "priority",
}
PROMPT_VERSION = "quality-support-v1"


def empty_quality_support() -> dict[str, Any]:
    return {
        "schema_version": QUALITY_SUPPORT_SCHEMA_VERSION,
        "revision": 0,
        "approved_version": 0,
        "cards": {},
        "batches": [],
        "scanned_unit_ids": [],
    }


def normalize_card(value: Any) -> dict[str, Any] | None:
    """Read one stored card tolerantly; return None when unusable."""
    if not isinstance(value, dict):
        return None
    card_id = value.get("id")
    if not isinstance(card_id, str) or not card_id.strip():
        return None
    card: dict[str, Any] = {
        "id": card_id.strip(),
        "created_at": str(value.get("created_at") or ""),
        "updated_at": str(value.get("updated_at") or ""),
        "origin": value.get("origin") if isinstance(value.get("origin"), dict) else {},
    }

    approved = value.get("approved")
    card["approved"] = approved if isinstance(approved, dict) else None
    card["approved_revision"] = _coerce_revision(value.get("approved_revision"))
    card["approved_at"] = str(value.get("approved_at") or "") if card["approved"] else ""

    draft = value.get("draft")
    card["draft"] = draft if isinstance(draft, dict) else None
    card["draft_revision"] = _coerce_revision(value.get("draft_revision"))

    status = value.get("status")
    card["status"] = status if status in CARD_DRAFT_STATUSES and card["draft"] is not None else None

    check = value.get("check")
    card["check"] = check if isinstance(check, dict) else None

    # Minimal explicit manual marker used by the automatic reference mode. It
    # is only carried when set, never changes the card id, and an absent marker
    # is not evidence of anything (protection still falls back to the
    # conservative signals in ``concept_automation.is_manual_protected``).
    if value.get("manual_protected") is True:
        card["manual_protected"] = True
    return card


def normalize_batch_retry(value: Any) -> dict[str, Any] | None:
    """Read one batch's recovery record tolerantly; ``None`` when it has none.

    A record written by an older version simply has no ``retry`` object, and a
    missing record is *not* an invitation to retry: the caller must treat ``None``
    as "no recovery identity" and say so, never fabricate one from the display
    fields.
    """

    if not isinstance(value, Mapping):
        return None
    stage = str(value.get("stage") or "").strip().casefold()
    if stage not in BATCH_RETRY_STAGES:
        return None
    state = str(value.get("state") or "").strip().casefold()
    if state not in BATCH_RETRY_STATES:
        state = "failed"
    source_bindings: list[dict[str, str]] = []
    for row in value.get("source_bindings") or []:
        if not isinstance(row, Mapping):
            continue
        unit_id = str(row.get("unit_id") or "").strip()
        if not unit_id:
            continue
        source_bindings.append(
            {"unit_id": unit_id, "source_sha256": str(row.get("source_sha256") or "")}
        )
    card_bindings: list[dict[str, Any]] = []
    for row in value.get("card_bindings") or []:
        if not isinstance(row, Mapping):
            continue
        card_id = str(row.get("card_id") or "").strip()
        if not card_id:
            continue
        card_bindings.append(
            {
                "card_id": card_id,
                "draft_revision": int(row.get("draft_revision") or 0),
                "content_fingerprint": str(row.get("content_fingerprint") or ""),
            }
        )
    return {
        "stage": stage,
        "mode": str(value.get("mode") or "").strip().casefold(),
        "prepare_id": str(value.get("prepare_id") or ""),
        "source_bindings": source_bindings,
        "card_bindings": card_bindings,
        "state": state,
        "attempt_count": max(0, int(value.get("attempt_count") or 0)),
        "last_error": str(value.get("last_error") or "")[:400],
        "updated_at": str(value.get("updated_at") or ""),
    }


def batch_retry_state(batch: Mapping[str, Any]) -> str:
    """The stored recovery state, or ``""`` when this batch has no record."""

    record = normalize_batch_retry(batch.get("retry") if isinstance(batch, Mapping) else None)
    return str(record.get("state") or "") if record else ""


def retain_batches(batches: Sequence[Any]) -> list[Any]:
    """Trim the display history without ever dropping an unresolved record.

    Completed/stale history keeps the most recent ``BATCH_HISTORY_LIMIT`` entries
    (the historical behaviour). A failed or interrupted recovery is not display
    history: it is the only place the project still knows that this batch needs a
    hand retry, so it always stays.
    """

    rows = [row for row in batches if isinstance(row, Mapping)]
    unresolved = [dict(row) for row in rows if batch_retry_state(row) in UNRESOLVED_BATCH_RETRY_STATES]
    resolved = [dict(row) for row in rows if batch_retry_state(row) not in UNRESOLVED_BATCH_RETRY_STATES]
    return resolved[-BATCH_HISTORY_LIMIT:] + unresolved


def normalize_quality_support(value: Any) -> dict[str, Any]:
    """Read stored quality support tolerantly without ever writing it back."""
    support = empty_quality_support()
    if not isinstance(value, dict):
        return support
    support["revision"] = _coerce_revision(value.get("revision"))
    support["approved_version"] = _coerce_revision(value.get("approved_version"))

    cards = value.get("cards")
    if isinstance(cards, dict):
        for card_id, raw in cards.items():
            if not isinstance(card_id, str):
                continue
            card = normalize_card({**raw, "id": raw.get("id", card_id)} if isinstance(raw, dict) else None)
            if card is not None:
                support["cards"][card["id"]] = card
    elif isinstance(cards, list):
        for raw in cards:
            card = normalize_card(raw)
            if card is not None:
                support["cards"][card["id"]] = card

    batches = value.get("batches")
    if isinstance(batches, list):
        for raw in retain_batches(batches):
            if isinstance(raw, dict) and isinstance(raw.get("batch_id"), str):
                support["batches"].append(dict(raw))

    # Persistent scan coverage. The batch list is display history capped at
    # 40 entries; which units have already been scanned must survive that
    # cap, otherwise "continue" would silently re-scan (and re-pay) old units.
    scanned = value.get("scanned_unit_ids")
    if isinstance(scanned, list):
        for unit_id in scanned:
            if isinstance(unit_id, str) and unit_id.strip() and unit_id not in support["scanned_unit_ids"]:
                support["scanned_unit_ids"].append(unit_id)
    # Legacy projects written before the field existed: derive coverage from
    # whatever batch history is still stored.
    for batch in support["batches"]:
        if batch_retry_state(batch) in UNRESOLVED_BATCH_RETRY_STATES:
            # A batch that never produced a saved scan result must not be read as
            # coverage, whatever the batch carried back then.
            continue
        for unit_id in batch.get("unit_ids") or []:
            if isinstance(unit_id, str) and unit_id not in support["scanned_unit_ids"]:
                support["scanned_unit_ids"].append(unit_id)

    # The automatic-reference state is carried through verbatim-shaped: its
    # validation rules live in ``core.concept_automation``. A legacy project
    # without the key simply keeps it absent — reading must never materialize
    # the feature (or a mode) for old data.
    if "automation" in value:
        from core import concept_automation as _automation

        support["automation"] = _automation.normalize_automation(value.get("automation"))
    return support


def scanned_unit_ids(support: Mapping[str, Any]) -> set[str]:
    """All units known to have been scanned, independent of the 40-batch cap."""
    return {str(item) for item in support.get("scanned_unit_ids") or []}


def effective_cards(support: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Approved cards, ordered by manual priority then stable id.

    A stored approved payload without expressions is not usable for matching,
    so it is not treated as effective. It is still kept for the human record.
    """
    cards = [
        card
        for card in support.get("cards", {}).values()
        if isinstance(card.get("approved"), dict) and (card["approved"].get("expressions") or [])
    ]
    return sorted(cards, key=lambda card: (-int(card["approved"].get("priority", 0)), card["id"]))


def card_expressions(card: Mapping[str, Any]) -> list[str]:
    approved = card.get("approved")
    if isinstance(approved, dict):
        return list(approved.get("expressions") or [])
    return []


def _effective_payload(card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The content a reference candidate actually carries.

    An automatic reference candidate stores its verified payload in ``payload``
    (the stored card may also have a draft or an approved copy of its own, which
    automation must not borrow). A stored manual card keeps using ``approved``.
    """

    payload = card.get("payload")
    if isinstance(payload, Mapping):
        return payload
    approved = card.get("approved")
    if isinstance(approved, Mapping):
        return approved
    return None


def _candidate_priority(card: Mapping[str, Any]) -> int:
    payload = _effective_payload(card)
    if payload is None:
        return 0
    try:
        return int(payload.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def find_card_hits(
    card: Mapping[str, Any],
    texts: Sequence[str],
) -> list[str]:
    """Return the expressions of one reference candidate that occur in the texts."""
    hits: list[str] = []
    payload = _effective_payload(card)
    if payload is None:
        return hits
    for expression in payload.get("expressions") or []:
        pattern = _expression_pattern(expression)
        if pattern is None:
            continue
        if any(pattern.search(text) for text in texts if isinstance(text, str)):
            hits.append(expression)
    return hits


def terminology_rules(
    candidate_cards: Sequence[Mapping[str, Any]], source_text: str
) -> dict[str, list[dict[str, Any]]]:
    """Resolve one canonical name per expression actually present in a unit.

    This is a read-only projection of effective cards, independent of the
    explanatory card budget. A conflicting or distinction-only expression is
    reported rather than assigned an arbitrary name.
    """
    choices: dict[str, list[dict[str, Any]]] = {}
    for card in candidate_cards:
        payload = _effective_payload(card) or {}
        bindings = payload.get("bindings")
        if str(card.get("origin") or "") == "automatic" and isinstance(bindings, list):
            entries = [
                (
                    binding.get("expressions") or [],
                    str(binding.get("preferred_translation") or "").strip()
                    if binding.get("kind") == "term" else "",
                    binding.get("acceptable_variants") or [],
                )
                for binding in bindings if isinstance(binding, Mapping)
            ]
        else:
            translations = [
                str(item).strip() for item in
                (payload.get("acceptable_translations") or []) if str(item).strip()
            ]
            preferred = [
                str(item).strip() for item in
                (payload.get("preferred_translations") or []) if str(item).strip()
            ]
            canonical = (
                (preferred[0] if len(preferred) == 1 else "")
                if str(card.get("origin") or "") == "automatic"
                else (translations[0] if translations else "")
            )
            entries = [(payload.get("expressions") or [], canonical, translations[1:])]
        for expressions, canonical, variants in entries:
            for expression in expressions:
                expression = str(expression).strip()
                pattern = _expression_pattern(expression)
                spans = [match.span() for match in pattern.finditer(source_text)] if pattern else []
                if not spans:
                    continue
                key = expression.casefold()
                choices.setdefault(key, []).append({
                    "expression": expression,
                    "canonical": canonical,
                    "variants": [str(item).strip() for item in variants if str(item).strip()],
                    "card_id": str(card.get("id") or card.get("card_id") or ""),
                    "spans": spans,
                })
    rules: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    keys = sorted(choices)
    overlaps: dict[str, set[str]] = {key: set() for key in keys}
    for index, key in enumerate(keys):
        for other in keys[index + 1:]:
            names = {row["canonical"] for row in choices[key]}
            other_names = {row["canonical"] for row in choices[other]}
            if names == other_names and len(names) == 1:
                continue
            if any(
                start < other_end and other_start < end
                for row in choices[key] for start, end in row["spans"]
                for other_row in choices[other]
                for other_start, other_end in other_row["spans"]
            ):
                overlaps[key].add(other)
                overlaps[other].add(key)
    for key in keys:
        rows = choices[key]
        related_rows = [row for related in {key, *overlaps[key]} for row in choices[related]]
        canonicals = sorted({row["canonical"] for row in related_rows if row["canonical"]})
        card_ids = sorted({row["card_id"] for row in related_rows if row["card_id"]})
        if len({row["canonical"] for row in rows}) != 1 or overlaps[key] or not canonicals:
            conflicts.append({
                "expression": rows[0]["expression"],
                "expressions": sorted({row["expression"] for row in related_rows}),
                "canonicals": canonicals,
                "card_ids": card_ids,
            })
            continue
        canonical = canonicals[0]
        variants = sorted({
            variant for row in rows for variant in row["variants"]
            if variant != canonical and variant not in canonical
        })
        rules.append({
            "expression": rows[0]["expression"],
            "canonical": canonical,
            "variants": variants,
            "card_ids": card_ids,
        })
    return {"rules": rules, "conflicts": conflicts}


def terminology_mismatches(
    rules: Sequence[Mapping[str, Any]], translated_text: str
) -> list[dict[str, str]]:
    """Find literal, reviewable departures from frozen canonical names."""
    translation = str(translated_text or "")
    mismatches: list[dict[str, str]] = []
    for rule in rules:
        canonical = str(rule.get("canonical") or "").strip()
        if not canonical:
            continue
        variants = [
            str(item).strip() for item in (rule.get("variants") or [])
            if str(item).strip() and str(item).strip() not in canonical
        ]
        found = [item for item in variants if item in translation]
        if canonical not in translation or found:
            mismatches.append({
                "expression": str(rule.get("expression") or ""),
                "canonical": canonical,
                "found_variants": "、".join(found),
                "reason": "使用了其他候选译名" if found else "未找到统一译名",
            })
    return mismatches


def render_reference_card(
    card: Mapping[str, Any], *, conflicted_expressions: set[str] | None = None
) -> str:
    """Render one reference card compactly for a model request.

    ``card`` is either a stored card (the human-approved path) or a reference
    candidate produced by :func:`reference_cards_for_unit` (which prefers its
    ``payload`` and may mark the origin as automatic).
    """

    approved = card.get("payload") if isinstance(card.get("payload"), Mapping) else (card.get("approved") or {})
    lines: list[str] = []
    if str(card.get("origin") or "") == "automatic":
        lines.append("来源：程序按有效证据自动采用的参考（非人工批准，人工参考优先）。")
    expressions = approved.get("expressions") or []
    if expressions:
        lines.append(f"概念：{', '.join(expressions)}")
    conflicts = conflicted_expressions or set()
    for item in approved.get("bindings") or []:
        if not isinstance(item, Mapping):
            continue
        parts = [", ".join(str(entry) for entry in (item.get("expressions") or []))]
        binding_conflict = any(
            str(entry).strip().casefold() in conflicts
            for entry in (item.get("expressions") or [])
        )
        if not binding_conflict and str(item.get("preferred_translation") or "").strip():
            parts.append(f"→ 统一译名：{str(item['preferred_translation']).strip()}")
        if str(item.get("guidance") or "").strip():
            parts.append(f"区别：{str(item['guidance']).strip()}")
        if str(item.get("applies_when") or "").strip():
            parts.append(f"适用：{str(item['applies_when']).strip()}")
        lines.append("条目：" + " ".join(part for part in parts if part))
    if approved.get("meaning"):
        lines.append(f"本处含义：{approved['meaning']}")
    if approved.get("applies_when"):
        lines.append(f"适用范围：{approved['applies_when']}")
    if not approved.get("bindings"):
        acceptable = approved.get("acceptable_translations") or []
        preferred = approved.get("preferred_translations") or []
        canonical = (
            preferred[0] if len(preferred) == 1 else ""
        ) if str(card.get("origin") or "") == "automatic" else (
            acceptable[0] if acceptable else ""
        )
        card_conflict = any(
            str(entry).strip().casefold() in conflicts
            for entry in expressions
        )
        if canonical and not card_conflict:
            lines.append(f"统一译名：{canonical}")
    if approved.get("confusions"):
        lines.append(f"易混淆：{'；'.join(approved['confusions'])}")
    if approved.get("open_questions"):
        lines.append(f"待核实：{'；'.join(approved['open_questions'])}")
    return "\n".join(lines)


def select_reference_candidates(
    support: Mapping[str, Any],
    *,
    unit_id: str,
    unit_sources: Mapping[str, tuple[str, str]],
    mode: str = "automatic",
) -> list[dict[str, Any]]:
    """The candidate pool one unit may draw from under the project's mode.

    ``manual`` (and every legacy project that never switched) sees only
    human-approved cards — identical to the historical behaviour. ``automatic``
    additionally sees verified automatic references, still ranked after the
    manual ones. A project that switches back to manual therefore stops using
    automatic references for *new* requests; already frozen snapshots are
    stored per unit and are not rewritten.
    """

    cards = list(effective_cards(support))
    if str(mode or "").strip().casefold() != "automatic":
        return cards
    return reference_cards_for_unit(support, unit_id=unit_id, unit_sources=unit_sources)


def reference_cards_for_unit(
    support: Mapping[str, Any],
    *,
    unit_id: str,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Every reference candidate one unit may see, manual first.

    Manual (human-approved) cards keep their previous behaviour and their
    priority ranking. Automatic references are only added when the automatic
    mode produced a live decision for *this* unit; the two origins are merged
    here and the caller applies the unchanged 6-card / 6000-character budget.
    Reading this never writes anything.
    """

    merged: list[tuple[int, int, str, dict[str, Any]]] = []
    for card in effective_cards(support):
        priority = -int((card.get("approved") or {}).get("priority", 0))
        merged.append((0, priority, str(card.get("id") or ""), card))
    from core import concept_automation as _automation

    if str(unit_id) in unit_sources:
        for item in _automation.auto_reference_cards(
            support, unit_id=str(unit_id), unit_sources=unit_sources
        ):
            card = (support.get("cards") or {}).get(item["card_id"])
            payload = item.get("payload") or {}
            priority = -int((payload.get("priority") if isinstance(payload, Mapping) else 0) or 0)
            merged.append(
                (
                    1,
                    priority,
                    item["card_id"],
                    {
                        "id": item["card_id"],
                        "origin": "automatic",
                        "payload": payload,
                        "approved_revision": 0,
                        "card_revision": int(item.get("content_revision") or 0),
                        "decision_id": str(item.get("decision_id") or ""),
                        "priority": payload.get("priority") if isinstance(payload, Mapping) else 0,
                        "base_card": card,
                    },
                )
            )
    merged.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in merged]


def select_reference_cards(
    support: Mapping[str, Any],
    *,
    source_text: str,
    adjacent_texts: Sequence[str] = (),
    max_cards: int = MAX_REFERENCE_CARDS,
    max_chars: int = MAX_REFERENCE_CHARS,
    candidate_cards: Sequence[Mapping[str, Any]] | None = None,
    conflicted_expressions: set[str] | None = None,
) -> dict[str, Any]:
    """Deterministically choose reference cards under a character budget.

    Order: current-unit hits first, then adjacent-source hits, then priority and
    stable id. A card is never truncated: when the next card does not fit, it is
    counted as omitted instead of being cut mid-way.

    ``candidate_cards`` overrides the default candidate pool (manual cards only,
    the historical behaviour). A caller that also prepared automatic references
    passes :func:`reference_cards_for_unit` output here so manual cards keep
    their rank while verified automatic cards fill the remaining budget. The
    budget itself is unchanged, and every rendered card records its origin so a
    later reader cannot mistake an automatic card for a human approval.
    """

    pool: Sequence[Mapping[str, Any]] = (
        candidate_cards if candidate_cards is not None else effective_cards(support)
    )
    candidates: list[tuple[int, int, int, str, dict[str, Any], list[str]]] = []
    for card in pool:
        current_hits = find_card_hits(card, [source_text])
        all_hits = current_hits or find_card_hits(card, list(adjacent_texts))
        if not all_hits:
            continue
        rank = 0 if current_hits else 1
        # Manual candidates always outrank automatic ones at the same hit rank;
        # within one origin the previous priority/id order is preserved.
        origin_rank = 0 if str(card.get("origin") or "") != "automatic" else 1
        candidates.append(
            (rank, origin_rank, -_candidate_priority(card), str(card.get("id") or card.get("card_id") or ""), dict(card), all_hits)
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    selected: list[dict[str, Any]] = []
    used_chars = 0
    omitted = 0
    automatic_omitted = 0
    for _rank, _origin, _priority, _card_id, card, hits in candidates:
        origin = "automatic" if str(card.get("origin") or "") == "automatic" else "manual"
        if len(selected) >= max_cards:
            omitted += 1
            automatic_omitted += 1 if origin == "automatic" else 0
            continue
        rendered = render_reference_card(
            card, conflicted_expressions=conflicted_expressions
        )
        if used_chars + len(rendered) > max_chars:
            omitted += 1
            automatic_omitted += 1 if origin == "automatic" else 0
            continue
        payload = _effective_payload(card) or {}
        selected.append(
            {
                "card_id": str(card.get("id") or card.get("card_id") or ""),
                "origin": origin,
                "card_revision": int(card.get("approved_revision") or card.get("card_revision") or 0),
                "expressions": list(payload.get("expressions") or []),
                "matched_expressions": hits,
                "text": rendered,
                "decision_id": str(card.get("decision_id") or ""),
            }
        )
        used_chars += len(rendered)
    return {
        "cards": selected,
        "omitted_card_count": omitted,
        "automatic_omitted_count": automatic_omitted,
        "used_chars": used_chars,
        "max_cards": max_cards,
        "max_chars": max_chars,
    }


def build_reference_snapshot(
    *,
    approved_version: int,
    selection: Mapping[str, Any],
    context_budget: Mapping[str, Any],
    structure_role: str,
    structure_role_source: str,
    term_rules: Mapping[str, Any] | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, Any]:
    """Freeze everything a later reader needs to know about one reference set."""
    return {
        "schema_version": QUALITY_SUPPORT_SCHEMA_VERSION,
        "prompt_version": prompt_version,
        "approved_version": int(approved_version),
        "card_count": len(selection.get("cards") or []),
        "cards": [
            {
                "card_id": card["card_id"],
                "origin": str(card.get("origin") or "manual"),
                "card_revision": card["card_revision"],
                "expressions": list(card.get("expressions") or []),
                "matched_expressions": list(card.get("matched_expressions") or []),
                "decision_id": str(card.get("decision_id") or ""),
                "text": card["text"],
            }
            for card in selection.get("cards") or []
        ],
        "omitted_card_count": int(selection.get("omitted_card_count") or 0),
        "automatic_omitted_count": int(selection.get("automatic_omitted_count") or 0),
        "origin_counts": {
            "manual": sum(1 for card in selection.get("cards") or [] if str(card.get("origin") or "manual") != "automatic"),
            "automatic": sum(1 for card in selection.get("cards") or [] if str(card.get("origin") or "") == "automatic"),
        },
        "context_budget": dict(context_budget or {}),
        "structure_role": structure_role,
        "structure_role_source": structure_role_source,
        "term_rules": copy.deepcopy(list((term_rules or {}).get("rules") or [])),
        "term_conflicts": copy.deepcopy(list((term_rules or {}).get("conflicts") or [])),
    }


def affected_units_for_cards(
    support: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    *,
    reference_versions: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List units whose current source matches an approved card.

    The result is a hint, not a diagnosis: a match means "this concept is
    present and the reference version may have changed", never "a mistranslation
    was found".
    """
    reference_versions = reference_versions or {}
    cards = effective_cards(support)
    results: list[dict[str, Any]] = []
    for unit in units:
        unit_id = str(unit.get("id") or "")
        source_text = str(unit.get("source") or "")
        if not unit_id or not source_text:
            continue
        matched: list[dict[str, Any]] = []
        for card in cards:
            hits = find_card_hits(card, [source_text])
            if hits:
                matched.append(
                    {
                        "card_id": card["id"],
                        "expressions": card_expressions(card),
                        "matched_expressions": hits,
                        "card_revision": int(card.get("approved_revision") or 0),
                    }
                )
        if not matched:
            continue
        stored = reference_versions.get(unit_id)
        results.append(
            {
                "unit_id": unit_id,
                "status": str(unit.get("status") or ""),
                "matched_cards": matched,
                "previous_reference_version": (
                    stored if isinstance(stored, (int, str)) or stored is None else None
                ),
            }
        )
    return results


def summarize_counts(support: Mapping[str, Any]) -> dict[str, int]:
    """Counts for the collapsed UI header; the three views may overlap."""
    cards = support.get("cards", {}).values()
    approved = 0
    pending = 0
    doubtful = 0
    deferred = 0
    for card in cards:
        if isinstance(card.get("approved"), dict):
            approved += 1
        if card.get("status") == "pending_review" and isinstance(card.get("draft"), dict):
            pending += 1
            check = card.get("check")
            verdict = check.get("verdict") if isinstance(check, dict) else None
            if verdict in {"disputed", "insufficient", "unchecked"}:
                doubtful += 1
        if card.get("status") in {"deferred", "rejected"}:
            deferred += 1
    return {
        "approved": approved,
        "pending_review": pending,
        "doubtful": doubtful,
        "deferred_or_rejected": deferred,
    }


# --------------------------------------------------------------------------
# Mutations. Every helper mutates the caller-owned support dict and bumps the
# optimistic revision. Callers must hold the project lock and save atomically.
# --------------------------------------------------------------------------


def _touch(support: dict[str, Any], *, approved_changed: bool = False) -> None:
    support["revision"] = int(support.get("revision") or 0) + 1
    if approved_changed:
        support["approved_version"] = int(support.get("approved_version") or 0) + 1


def _normalize_check(check: Mapping[str, Any], draft_revision: int) -> dict[str, Any]:
    verdict = str(check.get("verdict") or "")
    if verdict not in CHECK_VERDICTS:
        raise QualitySupportError("独立检查结论必须是 supported、disputed、insufficient 或 unchecked。")
    normalized = {
        "draft_revision": int(draft_revision),
        "verdict": verdict,
        "reasons": _string_list(check.get("reasons"), limit=8, field="reasons"),
        "notes": _bounded_text(check.get("notes"), limit=1200, field="notes"),
    }
    # The structured question result and the program-written verification
    # identity travel with the check. Their deep validation lives in
    # ``core.concept_automation`` (it needs the live unit sources); this layer
    # only guarantees the shape survives normalization and a disk round trip.
    assessment = check.get("automation_assessment")
    if isinstance(assessment, Mapping):
        normalized["automation_assessment"] = copy.deepcopy(dict(assessment))
    context = check.get("assessment_context")
    if isinstance(context, Mapping):
        normalized["assessment_context"] = copy.deepcopy(dict(context))
    fingerprint = check.get("content_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        normalized["content_fingerprint"] = fingerprint
    return normalized


def find_exact_duplicate(
    support: Mapping[str, Any],
    content: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return the existing card that already holds exactly this content.

    Both the pending draft and the approved copy count: re-scanning an
    unchanged unit must not push an already effective card (or a card the
    operator deferred) back into the review queue.  When several cards hold the
    same content — history can contain such twins and none of them is deleted
    automatically — the approved copy wins, then the lowest card id, so the
    choice is deterministic and reproducible.

    A card matches when *either* signature is identical: the pre-existing
    case/whitespace signature (so an ordinary ``LABOUR MARKET`` re-scan is still
    a full duplicate, exactly as before this round) or the writing-variant
    signature, which folds the ``expressions`` field through
    :func:`_orthographic_expression_key` so a fullwidth spelling of an existing
    expression is recognised as well. Every other field keeps the default
    case/whitespace comparison in both signatures, and the abbreviation guard
    only constrains the writing-variant signature — it must never turn a plain
    all-caps phrase into a fresh candidate.
    """

    legacy_signature = content_signature(content)
    orthographic_signature = _orthographic_signature(content)
    if not legacy_signature and not orthographic_signature:
        return None
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for card in support.get("cards", {}).values():
        if not isinstance(card, dict):
            continue
        rank = None
        for payload, payload_rank in ((card.get("approved"), 0), (card.get("draft"), 1)):
            if rank is not None:
                break  # an approved copy always outranks a pending draft
            if not isinstance(payload, dict):
                continue
            if legacy_signature and content_signature(payload) == legacy_signature:
                rank = payload_rank
            elif (
                orthographic_signature
                and _orthographic_signature(payload) == orthographic_signature
            ):
                rank = payload_rank
        if rank is not None:
            matches.append((rank, str(card.get("id") or ""), card))
    if not matches:
        return None
    matches.sort(key=lambda row: (row[0], row[1]))
    return matches[0][2]


def upsert_candidate(
    support: dict[str, Any],
    content: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    batch_id: str,
    unit_ids: Sequence[str],
    now_iso_value: str,
    check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Save one candidate as a draft unless a card already holds it exactly.

    Returns one outcome record instead of raising for the duplicate case:
    ``{"outcome": "created"|"updated"|"duplicate", "card_id", "reason"}``.
    A duplicate writes nothing at all — it keeps the existing draft, the
    approved copy, the status, the origin and the check conclusion of the card
    it matched, and it never receives this batch's check result.
    """

    normalized = normalize_card_content(content, unit_sources=unit_sources)
    duplicate = find_exact_duplicate(support, normalized)
    if duplicate is not None:
        return {
            "outcome": "duplicate",
            "card_id": str(duplicate.get("id") or ""),
            "reason": DUPLICATE_CANDIDATE_REASON,
        }
    card_id = card_id_for(normalized["expressions"], normalized.get("meaning"))
    outcome = "updated" if card_id in (support.get("cards") or {}) else "created"
    card = upsert_draft(
        support,
        normalized,
        unit_sources=unit_sources,
        batch_id=batch_id,
        unit_ids=unit_ids,
        now_iso_value=now_iso_value,
        check=check,
    )
    return {"outcome": outcome, "card_id": card["id"], "reason": ""}


def upsert_draft(
    support: dict[str, Any],
    content: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    batch_id: str,
    unit_ids: Sequence[str],
    now_iso_value: str,
    check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or replace the pending draft of one card, never its approved copy.

    This is the raw writer: it always replaces the draft.  Scan paths go
    through :func:`upsert_candidate`, which first reuses a card that already
    holds exactly the same content.
    """
    normalized = normalize_card_content(content, unit_sources=unit_sources)
    expressions = normalized["expressions"]
    card_id = card_id_for(expressions, normalized.get("meaning"))
    cards = support.setdefault("cards", {})
    existing = cards.get(card_id)
    if existing is None:
        card = {
            "id": card_id,
            "approved": None,
            "approved_revision": 0,
            "approved_at": "",
            "draft": None,
            "draft_revision": 0,
            "status": None,
            "check": None,
            "created_at": now_iso_value,
            "updated_at": now_iso_value,
            "origin": {"batch_id": batch_id, "unit_ids": list(unit_ids)},
        }
        cards[card_id] = card
    else:
        card = existing

    card["draft"] = dict(normalized)
    card["draft_revision"] = int(card.get("draft_revision") or 0) + 1
    card["status"] = "pending_review"
    card["updated_at"] = now_iso_value
    card["origin"] = {"batch_id": batch_id, "unit_ids": list(unit_ids)}
    if check is None:
        card["check"] = None
    else:
        card["check"] = _normalize_check(check, card["draft_revision"])
    _touch(support)
    return card


def apply_check_result(
    support: dict[str, Any],
    *,
    batch_id: str,
    checks: Sequence[Mapping[str, Any]],
    now_iso_value: str,
) -> list[dict[str, Any]]:
    """Attach check verdicts to the still-pending drafts of one saved batch.

    Used by the check-only retry: the generation already happened and its
    candidates are saved, so a failed independent check can be redone without
    paying for generation again. Drafts that were edited or decided in the
    meantime keep their current state; a human edit already marked the old
    check stale and must not be overwritten by a late retry.
    """

    cards = [
        card
        for card in support.get("cards", {}).values()
        if isinstance(card, dict)
        and str((card.get("origin") or {}).get("batch_id") or "") == str(batch_id)
        and card.get("status") == "pending_review"
        and isinstance(card.get("draft"), dict)
    ]
    cards.sort(key=lambda card: str(card.get("id") or ""))
    updated: list[dict[str, Any]] = []
    for card, check in zip(cards, checks):
        # A human edit bumps draft_revision and writes an "unchecked" verdict
        # bound to that revision. Overwriting it with a retry result for an
        # older draft would fake AI support for edited content.
        existing = card.get("check")
        if isinstance(existing, dict) and int(existing.get("draft_revision") or 0) == int(
            card.get("draft_revision") or 0
        ) and str(existing.get("verdict") or "") != "unchecked":
            continue
        card["check"] = _normalize_check(check, card.get("draft_revision"))
        card["updated_at"] = now_iso_value
        updated.append(card)
    if updated:
        _touch(support)
    return updated


def refresh_check_result(
    support: dict[str, Any],
    *,
    cards: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    now_iso_value: str,
    is_protected: Any = None,
) -> list[dict[str, Any]]:
    """Store a re-check result for **explicitly named** cards.

    Unlike :func:`apply_check_result` (which only fills in checks that are
    missing or bound to an older draft), this entry point may replace a check
    that is already up to date — the automatic path needs it when a card must be
    upgraded to the structured protocol or re-verified after the check identity
    changed. The protections stay exactly the same:

    * every result is paired with the card id **and** the draft revision it was
      frozen against, so a late answer for another revision is dropped;
    * a card that is no longer pending review, or that the caller reports as
      human-owned, is never touched;
    * no global "force" flag exists and the check is never written through an
      ``unchecked`` intermediate state.
    """

    guarded = is_protected if callable(is_protected) else (lambda _card: False)
    by_id = {
        str(card.get("id")): card
        for card in (support.get("cards") or {}).values()
        if isinstance(card, dict)
    }
    updated: list[dict[str, Any]] = []
    for pair, check in zip(cards, checks):
        if not isinstance(pair, Mapping) or not isinstance(check, Mapping):
            continue
        card = by_id.get(str(pair.get("card_id") or ""))
        if card is None or str(card.get("status") or "") != "pending_review":
            continue
        if int(pair.get("draft_revision") or 0) != int(card.get("draft_revision") or 0):
            continue
        if guarded(card):
            continue
        card["check"] = _normalize_check(check, card.get("draft_revision"))
        card["updated_at"] = now_iso_value
        updated.append(card)
    if updated:
        _touch(support)
    return updated


def approval_problems(card: Mapping[str, Any], *, unit_sources: Mapping[str, tuple[str, str]]) -> list[str]:
    """Reasons why a draft cannot become effective right now."""
    draft = card.get("draft")
    if not isinstance(draft, dict):
        return ["当前卡片没有待复检草稿。"]
    problems: list[str] = []
    try:
        normalize_card_content(draft, unit_sources=unit_sources)
    except QualitySupportError as exc:
        problems.append(str(exc))
    return problems


def batch_approval_problems(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[str]:
    """Reasons this card may not be approved as part of a batch.

    Batch approval is deliberately narrower than the single-card flow: the
    independent check must have supported *this* draft revision.  An unchecked,
    disputed or insufficient card, or one whose check belongs to an older
    draft, is never approved just because it was selected together with others
    — it stays available for per-card review.  The whole request is rejected
    when any selected card fails here; nothing is partially applied.
    """

    if not isinstance(card.get("draft"), dict):
        return ["当前卡片没有待复检草稿。"]
    if str(card.get("status") or "") != "pending_review":
        return ["只有待复检的草稿可以批量批准。"]
    check = card.get("check")
    if not isinstance(check, dict):
        return ["当前草稿没有独立检查结论，不能批量批准。"]
    if str(check.get("verdict") or "") != "supported":
        return ["独立检查结论不是 supported，不能批量批准（可逐条核对后批准）。"]
    if int(check.get("draft_revision") or 0) != int(card.get("draft_revision") or 0):
        return ["独立检查结论不是针对当前草稿版本的，不能批量批准。"]
    return approval_problems(card, unit_sources=unit_sources)


def apply_card_decision(
    support: dict[str, Any],
    card_id: str,
    action: str,
    *,
    content: Mapping[str, Any] | None = None,
    unit_sources: Mapping[str, tuple[str, str]] | None = None,
    expected_revision: int | None = None,
    expected_draft_revision: int | None = None,
    now_iso_value: str = "",
) -> dict[str, Any]:
    """Edit, approve, defer or reject one card without ever auto-approving."""
    action = str(action or "").strip().casefold()
    if action not in {"edit", "approve", "defer", "reject"}:
        raise QualitySupportError("卡片操作只能是 edit、approve、defer 或 reject。")
    cards = support.setdefault("cards", {})
    card = cards.get(str(card_id))
    if card is None:
        raise QualitySupportError("找不到指定的概念卡。")

    if expected_revision is not None:
        if int(expected_revision) != int(support.get("revision") or 0):
            raise QualitySupportError("概念数据已经变化，请刷新后重试。")
    if expected_draft_revision is not None and int(expected_draft_revision) != int(
        card.get("draft_revision") or 0
    ):
        raise QualitySupportError("概念卡草稿版本已经变化，请刷新后重试。")

    if action == "edit":
        if content is None:
            raise QualitySupportError("保存草稿需要提供卡片内容。")
        normalized = normalize_card_content(
            content, unit_sources=unit_sources if unit_sources is not None else {}
        )
        card["draft"] = normalized
        card["draft_revision"] = int(card.get("draft_revision") or 0) + 1
        card["status"] = "pending_review"
        card["updated_at"] = now_iso_value
        # A human edit invalidates the previous machine check on purpose. The
        # card can still be approved; it just must not claim AI support.
        card["check"] = {
            "draft_revision": card["draft_revision"],
            "verdict": "unchecked",
            "reasons": ["人工修改后，旧的 AI 检查结论已过期。"],
            "notes": "",
        }
        _touch(support)
        return card

    if action == "approve":
        if unit_sources is None:
            raise QualitySupportError("批准概念卡需要校验原文证据。")
        problems = approval_problems(card, unit_sources=unit_sources)
        if problems:
            raise QualitySupportError("；".join(problems))
        card["approved"] = dict(card["draft"])
        card["approved_revision"] = int(card.get("approved_revision") or 0) + 1
        card["approved_at"] = now_iso_value
        card["draft"] = None
        card["status"] = None
        card["check"] = None
        card["updated_at"] = now_iso_value
        _touch(support, approved_changed=True)
        return card

    if not isinstance(card.get("draft"), dict):
        raise QualitySupportError("当前卡片没有可搁置的草稿。")
    card["status"] = "deferred" if action == "defer" else "rejected"
    card["updated_at"] = now_iso_value
    _touch(support)
    return card


def _batch_cards(support: Mapping[str, Any], batch_id: str) -> list[dict[str, Any]]:
    """Every card this batch owns, in stable id order."""

    cards = [
        card
        for card in (support.get("cards") or {}).values()
        if isinstance(card, Mapping)
        and str((card.get("origin") or {}).get("batch_id") or "") == str(batch_id)
    ]
    cards.sort(key=lambda card: str(card.get("id") or ""))
    return [dict(card) for card in cards]


def batch_retry_descriptor(
    support: Mapping[str, Any],
    batch: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    mode: str,
    current_prepare_id: str = "",
) -> dict[str, Any]:
    """Read-only eligibility of one failed batch, and what a retry would cover.

    The descriptor is derived, never stored: reading it must not migrate or write
    anything. It is also the authority the retry entry point re-checks under the
    lock, so the page can never widen the scope the record froze.
    """

    batch_id = str(batch.get("batch_id") or "")
    record = normalize_batch_retry(batch.get("retry"))
    row: dict[str, Any] = {
        "batch_id": batch_id,
        "stage": str(record.get("stage") or "") if record else "",
        "mode": str(record.get("mode") or "") if record else "",
        "state": str(record.get("state") or "") if record else "",
        "attempt_count": int(record.get("attempt_count") or 0) if record else 0,
        "last_error": str(record.get("last_error") or "") if record else "",
        "updated_at": str(record.get("updated_at") or "") if record else "",
        "units": [str(item) for item in batch.get("unit_ids") or []],
        "unit_count": len([item for item in batch.get("unit_ids") or []]),
        "actionable_cards": 0,
        "protected_cards": 0,
        "changed_cards": 0,
        "retryable": False,
        "blocked_reason": "",
    }
    if record is None:
        # A batch whose check completed has nothing to resume; a failed/partial
        # one without a recovery record predates this feature, and saying "needs
        # identity" is honest — inventing one from the display fields would let a
        # retry guess a scope nobody froze.
        if str(batch.get("check_status") or "") == "completed":
            row["blocked_reason"] = "本批已经完成，无需重试。"
        else:
            row["blocked_reason"] = "这条记录来自旧版本，缺少恢复身份；请重新预览后再试。"
        return row
    row["units"] = [item["unit_id"] for item in record["source_bindings"]] or row["units"]
    row["unit_count"] = len(row["units"])
    # The candidate counts are informational and are reported whatever the state
    # is: the page shows how many cards a batch still covers even after a
    # successful recovery (zero) or after the candidates were taken over.
    if record["stage"] == "check":
        from core import concept_automation as _automation

        bindings = {item["card_id"]: item for item in record["card_bindings"]}
        actionable: list[dict[str, Any]] = []
        protected = 0
        for card in _batch_cards(support, batch_id):
            if card.get("status") != "pending_review" or not isinstance(card.get("draft"), Mapping):
                protected += 1
                continue
            if _automation.is_manual_protected(card):
                protected += 1
                continue
            actionable.append(card)
        row["protected_cards"] = protected
        row["actionable_cards"] = len(actionable)
        row["changed_cards"] = sum(
            1
            for card in actionable
            if int(bindings.get(str(card.get("id")), {}).get("draft_revision") or 0)
            != int(card.get("draft_revision") or 0)
        )
    if record["state"] == "completed":
        row["blocked_reason"] = "本批已经完成，无需重试。"
        return row
    if record["state"] == "stale":
        row["blocked_reason"] = "本批记录已失效（已被新的准备取代），请重新预览。"
        return row
    # An automatic batch belongs to the automatic-reference pipeline: if the
    # project left that mode the batch has no purpose left, so it must be
    # re-previewed. A manual-scan batch is a plain candidate extraction and stays
    # resumable whatever mode the project is in.
    if record.get("mode") == "automatic" and str(mode or "").strip().casefold() != "automatic":
        row["blocked_reason"] = "本批属于自动模式，项目当前是人工模式；请切回自动模式或重新预览。"
        return row
    if record.get("mode") == "automatic":
        if not current_prepare_id or str(record.get("prepare_id") or "") != str(current_prepare_id):
            row["blocked_reason"] = "记录已过期，需重新预览。"
            return row
    for item in record["source_bindings"]:
        live = unit_sources.get(item["unit_id"])
        if not isinstance(live, tuple) or len(live) != 2:
            row["blocked_reason"] = f"批次引用的单元 {item['unit_id']} 已不存在，请重新预览。"
            return row
        if str(live[1]) != str(item["source_sha256"]):
            row["blocked_reason"] = "批次引用的源文已经变化，请重新预览。"
            return row
    if record["stage"] == "check" and not row["actionable_cards"]:
        row["blocked_reason"] = "没有仍需重试的候选（候选已被人工接管、裁决或删除）。"
        return row
    row["retryable"] = True
    return row


def record_batch(
    support: dict[str, Any],
    batch: Mapping[str, Any],
    *,
    touch_coverage: bool = True,
) -> None:
    """Store a short batch result; duplicates of one batch_id are idempotent.

    Batch dedup (one batch_id saved once) is a transport-level guarantee and is
    deliberately separate from any semantic merging of similar candidates.
    The unit coverage is persisted in ``scanned_unit_ids`` so the 40-entry
    display history can never make the system forget what was already scanned.
    """
    batch_id = str(batch.get("batch_id") or "")
    if not batch_id:
        raise QualitySupportError("批次缺少 batch_id。")
    batches = support.setdefault("batches", [])
    if touch_coverage:
        coverage = support.setdefault("scanned_unit_ids", [])
        for unit_id in batch.get("unit_ids") or []:
            if isinstance(unit_id, str) and unit_id not in coverage:
                coverage.append(unit_id)
    for index, existing in enumerate(batches):
        if str(existing.get("batch_id") or "") == batch_id:
            batches[index] = dict(batch)
            support["batches"] = retain_batches(batches)
            _touch(support)
            return
    batches.append(dict(batch))
    support["batches"] = retain_batches(batches)
    _touch(support)


def planned_batches(
    units: Sequence[Mapping[str, Any]],
    *,
    max_source_words: int = DEFAULT_SCAN_SOURCE_WORDS,
    word_counter: Any = None,
) -> dict[str, Any]:
    """Plan a scan whose batches are split by the word target and nothing else.

    Each planned batch carries a deterministic ``batch_id`` derived from its
    unit ids, so the frontend consumes server-defined slices instead of
    guessing its own, and a retry of the same content reuses the same id.

    There is no operation, unit-count or character cap: a unit larger than the
    target forms a batch of its own and every unit with source text is planned.
    ``unscannable_units`` and ``remaining_unit_ids`` remain in the response for
    shape compatibility but are always empty now.
    """

    counter = word_counter or (lambda text: len(str(text).split()))
    batches: list[dict[str, Any]] = []
    current: list[str] = []
    current_words = 0

    def _finish_batch() -> None:
        if not current:
            return
        basis = "|".join(current)
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
        batches.append(
            {
                "batch_id": f"scan-{digest}",
                "unit_ids": list(current),
                "source_words": current_words,
            }
        )

    for unit in units:
        unit_id = str(unit.get("id") or "")
        source = str(unit.get("source") or "")
        if not unit_id or not source:
            continue
        words = int(counter(source))
        if current and current_words + words > max_source_words:
            _finish_batch()
            current = []
            current_words = 0
        current.append(unit_id)
        current_words += words
    _finish_batch()

    return {
        "batches": batches,
        "batch_count": len(batches),
        "unscannable_units": [],
        "remaining_unit_ids": [],
        "max_requests": len(batches) * 2,
    }
