"""Bounded concept lookup material and allowance bookkeeping.

The module contains deterministic search, request sizing and persisted lookup
identity rules.  It has no provider, pipeline or façade dependency.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from core import concept_content as cc
import mode2_common

from core.concept_automation_assessment import assessment_fingerprint


def _folded_with_offsets(text: str) -> tuple[str, list[int]]:
    """Fold one text with the project's orthographic rules, keeping the offsets.

    Full-width ASCII is folded and whitespace runs collapse exactly like the
    existing dedup/orthographic rules do. The offset map lets a caller quote the
    **original** characters: a normalized preview must never masquerade as source
    evidence.
    """

    folded_chars: list[str] = []
    offsets: list[int] = []
    previous_space = False
    for index, char in enumerate(text):
        folded = cc._fold_fullwidth_ascii(char)
        if folded.isspace():
            if previous_space or not folded_chars:
                continue
            folded = " "
            previous_space = True
        else:
            previous_space = False
        folded_chars.append(folded)
        offsets.append(index)
    return "".join(folded_chars), offsets


def lookup_occurrences(
    expressions: Sequence[Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    unit_ids: Sequence[str] | None = None,
    excerpt_chars: int = 200,
    max_units_per_expression: int = 0,
) -> dict[str, list[dict[str, str]]]:
    """Deterministic in-project search for the ``lookup_expressions`` of a check.

    Matching reuses the project's existing orthographic rules: full-width ASCII
    folding, whitespace-run collapsing and the same word-edge protection, so an
    abbreviation never matches inside a longer word. No stemming, no guessed
    aliases, no network, no whole-book pairwise comparison. Every returned
    excerpt is sliced out of the **original** source text (never the normalized
    form), and a hit is only read-only material for one bounded follow-up
    request — never an adoption or an expansion authorization.
    """

    wanted = [str(item).strip() for item in expressions if str(item or "").strip()]
    order = [str(item) for item in (unit_ids if unit_ids is not None else unit_sources.keys())]
    results: dict[str, list[dict[str, str]]] = {}
    for expression in wanted:
        folded_expression, _offsets = _folded_with_offsets(expression)
        pattern = cc._expression_pattern(folded_expression) if folded_expression else None
        rows: list[dict[str, str]] = []
        if pattern is not None:
            for unit_id in order:
                source = unit_sources.get(unit_id)
                if not isinstance(source, tuple) or len(source) != 2:
                    continue
                text, source_sha256 = str(source[0]), str(source[1])
                folded_text, offsets = _folded_with_offsets(text)
                match = pattern.search(folded_text)
                if match is None:
                    continue
                start_original = offsets[match.start()]
                last = min(match.end() - 1, len(offsets) - 1)
                end_original = offsets[last] + 1
                padding = max(0, int(excerpt_chars) - (end_original - start_original))
                left = max(0, start_original - padding // 2)
                right = min(len(text), end_original + padding // 2)
                rows.append(
                    {
                        "unit_id": unit_id,
                        "source_sha256": source_sha256,
                        "source_excerpt": text[left:right].strip(),
                    }
                )
                if max_units_per_expression and len(rows) >= int(max_units_per_expression):
                    break
        results[expression] = rows
    return results


def charge_budget(record: dict[str, Any], kind: str, *, amount: int = 1) -> bool:
    """Consume the one shared pool of extra logical requests.

    Lookups and large-group local judgments draw from the same number; a refused
    charge changes nothing, so a caller that cannot pay must record the work as
    unfinished instead of silently doing it.
    """

    existing = record.get("budget")
    step = max(1, int(amount))
    limit = int(existing.get("limit") or 0) if isinstance(existing, dict) else 0
    used = int(existing.get("used") or 0) if isinstance(existing, dict) else 0
    if used + step > limit:
        # A refusal is a pure read: no budget object is created and no counter
        # moves, so a caller cannot accidentally record work it never paid for.
        return False
    budget = existing if isinstance(existing, dict) else {"limit": 0, "used": 0, "by_kind": {}}
    record["budget"] = budget
    budget["used"] = used + step
    kinds = budget.setdefault("by_kind", {})
    if not isinstance(kinds, dict):  # pragma: no cover - defensive
        kinds = {}
        budget["by_kind"] = kinds
    key = str(kind or "extra")
    kinds[key] = int(kinds.get(key) or 0) + step
    return True
LOOKUP_RESULT_STATUSES = ("completed", "failed", "rejected")

#: How one lookup *request* ended, for the bounded display log only. "partial"
#: means some of its cards could be written and some could not.
LOOKUP_LOG_STATUSES = ("completed", "partial", "failed", "rejected")

#: The ledger is a bounded record of the lookups really executed, not a cache of
#: model answers: it stores the input identity and how the attempt ended.
MAX_LOOKUP_LEDGER = 20
def lookup_identity(
    expressions: Sequence[str],
    cards: Sequence[Mapping[str, Any]],
    material: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    """Identity of one executed lookup: the question, the cards and the evidence.

    Two runs with the same identity asked exactly this question about exactly
    these cards with exactly this material, so the answer cannot be new and the
    request is not repeated. A changed card content/revision, a changed excerpt
    or a changed source hash produces a different identity: the earlier result
    then stops applying and one fresh lookup is allowed.
    """

    rows = [
        "|".join(
            (
                str(card.get("card_id") or ""),
                f"@{int(card.get('draft_revision') or 0)}",
                str(card.get("content_fingerprint") or ""),
                # The card's current structured conclusion is part of the
                # question: a re-check that changed the preferred translation or
                # an answer makes this a different lookup, not the same one.
                str(card.get("assessment_fingerprint") or ""),
            )
        )
        for card in sorted(cards, key=lambda item: str(item.get("card_id") or ""))
    ]
    evidence = [
        "|".join(
            (
                str(expression),
                str(item.get("unit_id") or ""),
                str(item.get("source_sha256") or ""),
                str(item.get("source_excerpt") or ""),
            )
        )
        for expression in sorted(str(key) for key in material)
        for item in (material.get(expression) or [])
        if isinstance(item, Mapping)
    ]
    return "lk-" + hashlib.sha256(
        json.dumps(
            {
                "expressions": sorted(str(item) for item in expressions),
                "cards": sorted(rows),
                "evidence": sorted(evidence),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]


def lookup_ledger_row(row: Any) -> dict[str, Any] | None:
    """Read one stored executed-lookup row tolerantly (identity + result)."""

    if not isinstance(row, Mapping):
        return None
    identity = str(row.get("identity") or "")
    if not identity:
        return None
    status = str(row.get("status") or "").strip().casefold()
    if status not in LOOKUP_LOG_STATUSES:
        status = "failed"
    return {
        "identity": identity,
        "status": status,
        "cards": [str(item) for item in (row.get("cards") or []) if str(item)],
        "expressions": [str(item) for item in (row.get("expressions") or []) if str(item)],
        "prepare_id": str(row.get("prepare_id") or ""),
        "at": str(row.get("at") or ""),
    }


#: Input bounds of one lookup request (plan §4.1: 10 cards, 10 source units,
#: 4000 English words, 24000 characters — whichever is reached first).
LOOKUP_REQUEST_LIMITS = {"cards": 10, "units": 10, "words": 4000, "chars": 24000}


def lookup_request_shape(
    cards: Sequence[Mapping[str, Any]],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    material: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, int]:
    """The four sizes of one proposed lookup request: cards, units, words, chars.

    The counts cover what the request would really carry: the candidate payloads
    (cards), the source texts of every unit it would cite (its own evidence units
    **and** the units the read-only hits were quoted from), and the English words
    and characters of those sources plus the excerpts. Nothing is normalised or
    trimmed here — a caller that cannot fit an item must leave it out of the
    request and record it as unfinished instead of cutting the evidence.
    """

    unit_ids: list[str] = []
    words = 0
    chars = 0
    for card in cards:
        chars += len(json.dumps(card.get("draft") or {}, ensure_ascii=False))
        for unit_id in card.get("unit_ids") or []:
            key = str(unit_id)
            if key in unit_sources and key not in unit_ids:
                unit_ids.append(key)
        for expression in card.get("expressions") or []:
            for row in (material or {}).get(str(expression)) or []:
                if not isinstance(row, Mapping):
                    continue
                key = str(row.get("unit_id") or "")
                # A hit is quoted in the request, so its source must be cited with
                # it: the model may only point at units the request really sent.
                if key in unit_sources and key not in unit_ids:
                    unit_ids.append(key)
    for unit_id in unit_ids:
        text = str((unit_sources.get(unit_id) or ("", ""))[0])
        words += mode2_common.english_word_count(text)
        chars += len(text)
    for card in cards:
        for expression in card.get("expressions") or []:
            for row in (material or {}).get(str(expression)) or []:
                if not isinstance(row, Mapping):
                    continue
                excerpt = str(row.get("source_excerpt") or "")
                words += mode2_common.english_word_count(excerpt)
                chars += len(excerpt)
    return {"cards": len(cards), "units": len(unit_ids), "words": words, "chars": chars}


def lookup_request_blockers(
    shape: Mapping[str, Any],
    *,
    limits: Mapping[str, int] | None = None,
) -> list[str]:
    """Which of the plan's input bounds this shape exceeds (empty means it fits)."""

    bounds = limits or LOOKUP_REQUEST_LIMITS
    return [
        key
        for key in ("cards", "units", "words", "chars")
        if int(shape.get(key) or 0) > int(bounds.get(key) or 0)
    ]


def lookup_batch(
    cards: Sequence[Mapping[str, Any]],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    material: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Choose the cards of one lookup request inside the plan's input bounds.

    Cards are taken in their given (sorted) order while the accumulated request
    still fits; a card that would exceed a bound is recorded as **deferred** and
    the scan continues, so one oversized card cannot starve the cards behind it.
    Nothing is trimmed: a deferred card keeps its whole question and is sent by a
    later confirmation.
    """

    bounds = dict(limits or LOOKUP_REQUEST_LIMITS)
    kept: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for card in cards:
        blockers = lookup_request_blockers(
            lookup_request_shape([*kept, card], unit_sources=unit_sources, material=material),
            limits=bounds,
        )
        if blockers:
            deferred.append({"card_id": str(card.get("card_id") or ""), "blocked_by": blockers})
            continue
        kept.append(dict(card))
    return {
        "cards": kept,
        "deferred": deferred,
        "shape": lookup_request_shape(kept, unit_sources=unit_sources, material=material),
        "limits": bounds,
    }


#: How many expressions one card's recorded allowance may carry. It is the same
#: whitelist bound the structured assessment already enforces, so the record can
#: never grow past what one card can actually ask for.
MAX_LOOKUP_STATE_EXPRESSIONS = 8

#: How many material units one card's recorded allowance may carry. One bounded
#: request may cite at most this many units (plan §4.1), so a record that claims
#: more was not produced by this contract — and it is refused rather than
#: shortened, because a shortened identity would silently stop matching.
MAX_LOOKUP_STATE_UNITS = int(LOOKUP_REQUEST_LIMITS["units"])


def lookup_material_units(
    expressions: Sequence[str],
    material: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, str]]:
    """The units one card's lookup really reads, in the order its expressions find them.

    A lookup reads the excerpts of its own expressions — and those hits may sit
    in units the card's evidence never mentioned (the whole point of looking
    something up). This list is what a later change has to be compared against;
    the excerpt itself needs no separate digest, because for a frozen expression
    it is a deterministic slice of the source text it is quoted from.
    """

    units: list[dict[str, str]] = []
    for expression in expressions:
        for row in material.get(str(expression)) or []:
            if not isinstance(row, Mapping):
                continue
            unit_id = str(row.get("unit_id") or "")
            if not unit_id:
                continue
            entry = {
                "unit_id": unit_id,
                "source_sha256": str(row.get("source_sha256") or ""),
            }
            if entry["source_sha256"] and entry not in units:
                units.append(entry)
    return units


def lookup_state_units(value: Any) -> list[dict[str, str]] | None:
    """Read the material identity of one stored allowance row.

    ``None`` means the row cannot be read as a record of what was read (an entry
    without a unit or a hash, junk instead of a list, or more units than one
    bounded request could ever cite). The caller then treats the row as proving
    nothing rather than as proving a shortened identity. An absent field is the
    honest "this row predates the material identity" and reads as empty.
    """

    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return None
    units: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        unit_id = str(raw.get("unit_id") or "")
        source_sha256 = str(raw.get("source_sha256") or "")
        if not unit_id or not source_sha256:
            return None
        entry = {"unit_id": unit_id, "source_sha256": source_sha256}
        if entry not in units:
            units.append(entry)
    if len(units) > MAX_LOOKUP_STATE_UNITS:
        return None
    return units


def lookup_material_intact(
    row: Mapping[str, Any] | None,
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> bool:
    """Whether the material an executed lookup read is still the same material.

    Every unit the answer was quoted from must still exist under the same source
    hash. Anything else — a rewritten paragraph, a re-imported unit, a deletion —
    means the recorded answer answers a question about a different text, so it
    cannot be reused as "already asked". A row that carries no material identity
    at all cannot show the material was ever read: it proves nothing either.
    """

    if not isinstance(row, Mapping):
        return False
    units = lookup_state_units(row.get("units"))
    if not units:
        return False
    for entry in units:
        source = unit_sources.get(str(entry["unit_id"]))
        if not isinstance(source, tuple) or len(source) != 2:
            return False
        if str(source[1]) != str(entry["source_sha256"]):
            return False
    return True


def lookup_content_version(
    card_row: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> str:
    """The content version one lookup allowance belongs to.

    "One bounded lookup per content version" (plan §4.1) means the allowance is
    tied to what the project actually holds: the card's draft revision, its
    content fingerprint and the source hashes its evidence points at. Re-writing
    the card's *conclusion* — by the lookup itself or by a formal re-check — does
    not change this version; editing the draft, re-importing a source or changing
    the cited evidence does.
    """

    card_id = str(card_row.get("card_id") or card_row.get("id") or "")
    revision = int(card_row.get("draft_revision") or 0)
    fingerprint = str(card_row.get("content_fingerprint") or "")
    hashes = sorted(
        f"{str(unit_id)}:{str((unit_sources.get(str(unit_id)) or ('', ''))[1])}"
        for unit_id in (card_row.get("unit_ids") or [])
        if str(unit_id)
    )
    return "lv-" + hashlib.sha256(
        json.dumps(
            {
                "card_id": card_id,
                "draft_revision": revision,
                "content_fingerprint": fingerprint,
                "sources": hashes,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]


def check_fingerprint(check: Mapping[str, Any] | None) -> str:
    """A stable digest of one stored check, used to tell writers apart.

    The lookup's own answer must not buy itself another lookup, while a *formal*
    replacement of the conclusion (the controlled re-check, a human edit) may
    legitimately reopen one bounded lookup. Comparing this digest with the one
    recorded when the lookup was executed distinguishes exactly those two: the
    lookup records the check it wrote, the re-check writes a different one.
    """

    if not isinstance(check, Mapping):
        return ""
    context = (
        check.get("assessment_context")
        if isinstance(check.get("assessment_context"), Mapping)
        else {}
    )
    hashes = (
        context.get("source_hashes")
        if isinstance(context.get("source_hashes"), Mapping)
        else {}
    )
    assessment = check.get("automation_assessment")
    return "cf-" + hashlib.sha256(
        json.dumps(
            {
                "draft_revision": int(check.get("draft_revision") or 0),
                "verdict": str(check.get("verdict") or ""),
                "reasons": sorted(str(item) for item in (check.get("reasons") or [])),
                "notes": str(check.get("notes") or ""),
                "content_fingerprint": str(check.get("content_fingerprint") or ""),
                "prompt_version": str(context.get("prompt_version") or ""),
                "model": str(context.get("model") or ""),
                "source_hashes": sorted(
                    f"{str(unit_id)}:{str(sha)}" for unit_id, sha in hashes.items()
                ),
                "assessment": (
                    assessment_fingerprint(assessment)
                    if isinstance(assessment, Mapping)
                    else ""
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]


def lookup_state_of(value: Any) -> dict[str, dict[str, Any]]:
    """The record of the lookup allowance really spent, one row per card.

    Each row says which **content version** the card's one bounded lookup was
    spent on, which check that execution left behind, which expressions were
    frozen into that request, **which units that request really read** (the hits,
    including the ones the card's evidence never mentioned) and how it ended. It
    is deliberately **not** the display log — the log is trimmed for the page,
    this map is not — and it is keyed per card, so a lookup answer that proposes
    new expressions (or rewrites its own conclusion) can never mint a new
    allowance, while a changed draft, source, read material or formally replaced
    conclusion still can.
    """

    if not isinstance(value, Mapping):
        return {}
    state: dict[str, dict[str, Any]] = {}
    for card_id, entry in value.items():
        card_key = str(card_id or "")
        if not card_key or not isinstance(entry, Mapping):
            continue
        version = str(entry.get("version") or "")
        check = str(entry.get("check") or "")
        if not version or not check:
            # A row without an allowance identity proves nothing: the card simply
            # spends its (single) lookup once under the new contract.
            continue
        units = lookup_state_units(entry.get("units"))
        if units is None:
            # A material identity this contract could not have written proves
            # nothing either; the card is not silently silenced by it.
            continue
        status = str(entry.get("status") or "").strip().casefold()
        if status not in LOOKUP_RESULT_STATUSES:
            status = "failed"
        state[card_key] = {
            "version": version,
            "check": check,
            "expressions": [
                str(item)
                for item in (entry.get("expressions") or [])[:MAX_LOOKUP_STATE_EXPRESSIONS]
                if str(item)
            ],
            "units": units,
            "status": status,
            "at": str(entry.get("at") or ""),
        }
    return state


def lookup_state_get(
    state: Mapping[str, Mapping[str, Any]],
    card_id: str,
) -> dict[str, Any] | None:
    """The recorded allowance for one card, or None when it never spent one."""

    entry = state.get(str(card_id))
    return dict(entry) if isinstance(entry, Mapping) else None


def lookup_allowance_spent(
    state: Mapping[str, Mapping[str, Any]],
    card_id: str,
    *,
    version: str,
    check: str,
    unit_sources: Mapping[str, tuple[str, str]],
) -> bool:
    """Whether this card's one bounded lookup is already spent for this input.

    Spent means all three parts hold: the same content version was already asked,
    the check the card holds now is the one that execution left behind (or the one
    it was asked against, for a failed or rejected attempt), **and** the material
    that answer was quoted from is still the material the sources hold now. The
    third part is what makes a hit outside the card's own evidence count: editing
    (or re-importing, or deleting) a unit the lookup really read turns the answer
    into an answer about a different text, and the card may spend its one lookup
    again — once, on the new material, after which the record matches again.

    Anything else — a new draft, a re-imported source, a formally replaced
    conclusion, a read unit that changed, or a record that never named what it
    read — is not proof that this input was answered, so the allowance is not
    considered spent. Nothing here counts the lookup's own write: the material
    identity is written from the sources the request was built on, which its
    answer cannot change.
    """

    recorded = lookup_state_get(state, card_id)
    if not recorded:
        return False
    if str(recorded.get("version") or "") != str(version or ""):
        return False
    if str(recorded.get("check") or "") != str(check or ""):
        return False
    return lookup_material_intact(recorded, unit_sources=unit_sources)


def lookup_state_set(
    state: dict[str, dict[str, Any]],
    card_id: str,
    *,
    version: str,
    check: str,
    expressions: Sequence[str],
    units: Sequence[Mapping[str, Any]],
    status: str,
    at: str,
) -> None:
    """Record that this card spent its one bounded lookup on this content version.

    ``units`` is the material the request really read (see
    :func:`lookup_material_units`). It is stored exactly as read: it is the part
    of the identity that later decides whether the answer still applies, so a row
    written without it can never be mistaken for one that proved the material.
    """

    card_key = str(card_id or "")
    version_value = str(version or "")
    check_value = str(check or "")
    if not card_key or not version_value or not check_value:
        return
    result = str(status or "").strip().casefold()
    if result not in LOOKUP_RESULT_STATUSES:
        result = "failed"
    state[card_key] = {
        "version": version_value,
        "check": check_value,
        "expressions": [
            str(item) for item in expressions[:MAX_LOOKUP_STATE_EXPRESSIONS] if str(item)
        ],
        "units": lookup_state_units(list(units)) or [],
        "status": result,
        "at": str(at or ""),
    }


def lookup_state_pruned(
    state: Mapping[str, Mapping[str, Any]],
    live_card_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Keep only the rows of cards that still exist in the project."""

    live = {str(card_id) for card_id in live_card_ids if str(card_id)}
    rows = lookup_state_of(state)
    return {card_id: entry for card_id, entry in rows.items() if card_id in live}
