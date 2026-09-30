"""Quality provider inputs and program-written verification identity."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.concept_content import content_signature, normalize_card_content
from core.quality_support import PROMPT_VERSION
from providers.quality_provider import ConceptUnitRef


def approved_expressions(support: Mapping[str, Any]) -> tuple[str, ...]:
    """Every expression a human-approved card currently carries, sorted."""

    return tuple(
        sorted(
            {
                expression
                for card in (support.get("cards") or {}).values()
                for expression in ((card.get("approved") or {}).get("expressions") or [])
            }
        )
    )


def concept_unit_refs(units: Sequence[Mapping[str, Any]]) -> tuple[ConceptUnitRef, ...]:
    """One ``ConceptUnitRef`` per unit, using the unit's own source binding."""

    return tuple(
        ConceptUnitRef(
            unit_id=str(unit["id"]),
            source_text=str(unit.get("source") or ""),
            source_sha256=str(unit.get("source_sha256") or ""),
        )
        for unit in units
    )


def concept_unit_refs_from_sources(
    unit_ids: Sequence[str],
    unit_sources: Mapping[str, tuple[str, str]],
) -> tuple[ConceptUnitRef, ...]:
    """One ``ConceptUnitRef`` per known unit id, read from a source mapping.

    An id the project no longer holds is skipped: a request may only cite
    units that still exist.
    """

    return tuple(
        ConceptUnitRef(
            unit_id=unit_id,
            source_text=str((unit_sources.get(unit_id) or ("", ""))[0]),
            source_sha256=str((unit_sources.get(unit_id) or ("", ""))[1]),
        )
        for unit_id in unit_ids
        if unit_id in unit_sources
    )


def assessment_context_payload(
    candidate: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    model: str,
) -> dict[str, Any]:
    """The verification identity the program writes, never the model."""

    fingerprint = ""
    hashes: dict[str, str] = {}
    try:
        normalized = normalize_card_content(candidate, unit_sources=unit_sources)
    except Exception:  # pragma: no cover - a rejected candidate fails earlier
        normalized = {}
    if normalized:
        try:
            fingerprint = content_signature(normalized)
        except Exception:  # pragma: no cover - signature is total for mappings
            fingerprint = ""
        for item in normalized.get("evidence") or []:
            unit_id = str(item.get("unit_id") or "")
            if unit_id:
                hashes[unit_id] = str(item.get("source_sha256") or "")
    return {
        "content_fingerprint": fingerprint,
        "source_hashes": hashes,
        "prompt_version": PROMPT_VERSION,
        # A non-secret identifier only: never a key or a credentialed URL.
        "model": str(model or ""),
    }


def stored_check_payload(
    check: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    model: str,
) -> dict[str, Any]:
    """One model verdict plus the program-written verification identity.

    The structured question result travels with the check; dropping it here
    would silently turn a resolved question back into a block. The identity
    is always written by the program, never taken from the model.
    """

    payload = {
        "verdict": check["verdict"],
        "reasons": check["reasons"],
        "notes": check.get("notes") or "",
    }
    assessment = check.get("automation_assessment")
    if isinstance(assessment, dict):
        payload["automation_assessment"] = assessment
    payload["assessment_context"] = assessment_context_payload(
        candidate,
        unit_sources=unit_sources,
        model=model,
    )
    return payload
