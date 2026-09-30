"""Duplicate-aware writes to caller-owned concept-card data.

These helpers never acquire a lock, save project state, or call a model.
Callers own locking and atomic persistence of the mutated support dictionary.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from core.concept_content import (
    QualitySupportError,
    _bounded_text,
    _orthographic_signature,
    _string_list,
    card_id_for,
    content_signature,
    normalize_card_content,
)

DUPLICATE_CANDIDATE_REASON = "与既有概念卡的内容、证据完全一致，未重复进入审核队列。"

CHECK_VERDICTS = {"supported", "disputed", "insufficient", "unchecked"}



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
