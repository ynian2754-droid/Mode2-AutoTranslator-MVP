"""Shared deterministic concept-content rules.

This plain-data layer owns expression and evidence normalization, content
identity, matching, and fingerprints. It has no project storage, model calls,
or dependency on quality-support orchestration.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

class QualitySupportError(ValueError):
    """The concept payload or the requested operation is invalid."""



_NON_WORD_EDGE = r"(?<![A-Za-z0-9])(?P<body>{body})(?![A-Za-z0-9])"
_WHITESPACE_RUN_RE = re.compile(r"\s+")

# Fullwidth ASCII letters/digits -> halfwidth, used only by the expression
# matching path added in this round. Deliberately not NFKC: NFKC also folds
# maths and superscripts, which must stay distinct (x² is not x2, x⁰ is not x0).
_FULLWIDTH_ASCII_TRANS = {
    chr(0xFF10 + offset): chr(0x30 + offset) for offset in range(10)
}
_FULLWIDTH_ASCII_TRANS.update(
    {chr(0xFF21 + offset): chr(0x41 + offset) for offset in range(26)}
)
_FULLWIDTH_ASCII_TRANS.update(
    {chr(0xFF41 + offset): chr(0x61 + offset) for offset in range(26)}
)
_FULLWIDTH_ASCII_RE = re.compile(
    "[" + "".join(re.escape(char) for char in _FULLWIDTH_ASCII_TRANS) + "]"
)
# An all-uppercase token of at least two characters is treated as an
# abbreviation and keeps its case, so ``US`` never collapses into ``us``.
_ALL_CAPS_ABBREVIATION_RE = re.compile(r"^[A-Z][A-Z0-9]+$")


def normalize_expression(value: Any) -> str:
    """Fold one English expression for identity and matching purposes."""
    text = _WHITESPACE_RUN_RE.sub(" ", str(value or "")).strip().casefold()
    return text



def _fold_fullwidth_ascii(value: str) -> str:
    return _FULLWIDTH_ASCII_RE.sub(
        lambda match: _FULLWIDTH_ASCII_TRANS[match.group()], value
    )



def _orthographic_expression_key(value: Any) -> str:
    """Fold one expression for the writing-variant matching path only.

    This is the matching key used by the exact-duplicate lookup for the
    ``expressions`` field, and by nothing else. It adds exactly two things on
    top of :func:`normalize_expression`:

    * an explicit fullwidth ASCII letters/digits -> halfwidth mapping, so
      ``ｌａｂｏｕｒ market`` and ``labour market`` are the same writing;
    * an abbreviation guard: an all-uppercase token of at least two characters
      keeps its case, so ``US`` is never made equal to ``us`` merely by
      ignoring case.

    Everything else is deliberately untouched: no punctuation folding, no
    stemming, no spelling variants, no zero-width stripping, and no single
    case-equivalence rule beyond the existing one. It is not NFKC.
    """
    text = _WHITESPACE_RUN_RE.sub(" ", _fold_fullwidth_ascii(str(value or ""))).strip()
    return " ".join(
        token if _ALL_CAPS_ABBREVIATION_RE.match(token) else token.casefold()
        for token in text.split(" ")
    )



def card_id_for(expressions: Sequence[Any], meaning: Any = "") -> str:
    """Derive a stable id from ALL expressions plus the claimed meaning.

    Identity deliberately includes the meaning text: two candidates that share
    one expression but describe different senses must stay independent cards
    (``语义相同与否由证据和人复检``), never overwrite each other. Lexical or
    model-side similarity must not decide that two senses are the same.
    """

    normalized = [normalize_expression(item) for item in expressions if normalize_expression(item)]
    meaning_basis = normalize_expression(meaning)
    basis = "|".join(normalized) + "#" + meaning_basis
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]
    return f"card-{digest}"



def _string_list(value: Any, *, limit: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise QualitySupportError(f"{field} 必须是数组。")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) > limit:
            raise QualitySupportError(f"{field} 最多 {limit} 项。")
    return result



def _bounded_text(value: Any, *, limit: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise QualitySupportError(f"{field} 必须是字符串。")
    text = value.strip()
    if len(text) > limit:
        raise QualitySupportError(f"{field} 超过 {limit} 字符上限。")
    return text



def _excerpt_matches(source_text: str, excerpt: str) -> bool:
    """Verify a model-provided excerpt verbatim against the bound unit source.

    Exact substring first; a single-space-collapsed comparison is accepted as a
    fallback because PDF reconstruction can alter whitespace runs. Both paths
    are deterministic and can only match real source text.
    """
    if not excerpt:
        return False
    if excerpt in source_text:
        return True
    return _WHITESPACE_RUN_RE.sub(" ", excerpt).strip() in _WHITESPACE_RUN_RE.sub(" ", source_text)



def verify_evidence(
    evidence: Any,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Return normalized evidence and raise when an excerpt is not verifiable."""
    if not isinstance(evidence, list) or not evidence:
        raise QualitySupportError("概念卡至少需要一条可核验的原文证据。")
    if len(evidence) > 6:
        raise QualitySupportError("概念卡证据最多 6 条。")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise QualitySupportError(f"evidence[{index}] 必须是对象。")
        if set(item) != {"unit_id", "source_sha256", "source_excerpt"}:
            raise QualitySupportError(
                f"evidence[{index}] 必须且只能包含 unit_id、source_sha256、source_excerpt。"
            )
        unit_id = item["unit_id"]
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise QualitySupportError(f"evidence[{index}].unit_id 必须是非空字符串。")
        unit_id = unit_id.strip()
        source_sha256 = item["source_sha256"]
        if not isinstance(source_sha256, str) or not source_sha256.strip():
            raise QualitySupportError(f"evidence[{index}].source_sha256 必须是非空字符串。")
        excerpt = _bounded_text(item["source_excerpt"], limit=400, field=f"evidence[{index}].source_excerpt")
        if not excerpt:
            raise QualitySupportError(f"evidence[{index}].source_excerpt 不能为空。")
        if unit_id not in unit_sources:
            raise QualitySupportError(f"evidence[{index}] 指向不存在的单元 {unit_id}。")
        source_text, expected_sha256 = unit_sources[unit_id]
        if source_sha256.strip() != expected_sha256:
            raise QualitySupportError(f"evidence[{index}] 的源文哈希与单元 {unit_id} 不匹配。")
        if not _excerpt_matches(source_text, excerpt):
            raise QualitySupportError(
                f"evidence[{index}] 的原文摘录不在单元 {unit_id} 的源文中，不能作为证据。"
            )
        normalized.append(
            {
                "unit_id": unit_id,
                "source_sha256": source_sha256.strip(),
                "source_excerpt": excerpt,
            }
        )
    return normalized



def normalize_card_content(
    content: Any,
    *,
    unit_sources: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate and normalize one card content payload."""
    if not isinstance(content, dict):
        raise QualitySupportError("概念内容必须是对象。")

    expressions = _string_list(content.get("expressions"), limit=8, field="expressions")
    if not expressions:
        raise QualitySupportError("概念卡至少需要一个英文表达。")
    for expression in expressions:
        if len(expression) > 120:
            raise QualitySupportError("单个概念表达不能超过 120 个字符。")

    acceptable = _string_list(
        content.get("acceptable_translations"), limit=8, field="acceptable_translations"
    )
    if not acceptable:
        raise QualitySupportError("概念卡至少需要一个可接受译法。")

    priority = content.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        priority = 0
    priority = max(0, min(priority, 100))

    evidence = (
        verify_evidence(content.get("evidence"), unit_sources)
        if unit_sources is not None
        else _string_list_evidence_stub(content.get("evidence"))
    )

    return {
        "expressions": expressions,
        "meaning": _bounded_text(content.get("meaning"), limit=1200, field="meaning"),
        "applies_when": _bounded_text(content.get("applies_when"), limit=1200, field="applies_when"),
        "acceptable_translations": acceptable,
        "confusions": _string_list(content.get("confusions"), limit=8, field="confusions"),
        "evidence": evidence,
        "open_questions": _string_list(content.get("open_questions"), limit=8, field="open_questions"),
        "priority": priority,
    }



def _string_list_evidence_stub(evidence: Any) -> list[dict[str, Any]]:
    """Keep evidence shape-only checks when no unit source map is supplied."""
    if not isinstance(evidence, list) or not evidence:
        raise QualitySupportError("概念卡至少需要一条可核验的原文证据。")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise QualitySupportError(f"evidence[{index}] 必须是对象。")
        if set(item) != {"unit_id", "source_sha256", "source_excerpt"}:
            raise QualitySupportError(
                f"evidence[{index}] 必须且只能包含 unit_id、source_sha256、source_excerpt。"
            )
    return [dict(item) for item in evidence]



def _coerce_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value



def _expression_pattern(expression: str) -> re.Pattern[str] | None:
    body = re.escape(expression)
    if not body:
        return None
    try:
        return re.compile(_NON_WORD_EDGE.format(body=body), re.IGNORECASE)
    except re.error:  # pragma: no cover - escape always yields a valid pattern
        return None



def _priority_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, 100))



def _normalized_set(values: Any, normalizer: Any = None) -> tuple[str, ...]:
    fold = normalizer or normalize_expression
    if not isinstance(values, list):
        return ()
    return tuple(sorted({fold(item) for item in values if fold(item)}))



def content_signature(
    content: Mapping[str, Any],
    *,
    expressions_key: Any = None,
) -> str:
    """Signature over every normalized semantic field, priority and evidence.

    The signature exists only to answer one question: *is this exactly what a
    card already holds?*  It therefore reads every field a card stores —
    expressions, meaning, applies_when, acceptable translations, confusions,
    open questions, priority and evidence — and applies the existing
    case/whitespace normalization.  Nothing else happens here: no punctuation
    folding, no stemming, no similarity and no model judgement.

    The ``expressions_key`` hook swaps the normalizer for the ``expressions``
    field alone.  The duplicate lookup passes the writing-variant key so a
    fullwidth spelling matches; every other field keeps the default
    normalization, and the default call behaves exactly as before.

    Lists are compared as sets so a re-ordered alias list or a re-ordered
    evidence list is the same content; *added* or *changed* evidence is a
    different signature on purpose, so new evidence is never treated as a
    duplicate.
    """

    if not isinstance(content, Mapping):
        return ""
    fold_expression = expressions_key or normalize_expression
    evidence_rows: list[str] = []
    evidence = content.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            evidence_rows.append(
                "|".join(
                    (
                        str(item.get("unit_id") or "").strip(),
                        str(item.get("source_sha256") or "").strip(),
                        normalize_expression(item.get("source_excerpt")),
                    )
                )
            )
    payload = (
        _normalized_set(content.get("expressions"), fold_expression),
        normalize_expression(content.get("meaning")),
        normalize_expression(content.get("applies_when")),
        _normalized_set(content.get("acceptable_translations")),
        _normalized_set(content.get("confusions")),
        _normalized_set(content.get("open_questions")),
        tuple(sorted(evidence_rows)),
        _priority_value(content.get("priority")),
    )
    return "sig-" + hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]



def _orthographic_signature(content: Mapping[str, Any]) -> str:
    return content_signature(content, expressions_key=_orthographic_expression_key)
