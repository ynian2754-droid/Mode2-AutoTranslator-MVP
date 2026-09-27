"""Small, dependency-free protocol shared by the two standalone Mode 2 editions.

The file is vendored into each edition.  The tools never import one another and
book projects never share state; keeping this module byte-identical merely makes
the human-facing rules consistent.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence


COMMON_PROTOCOL_VERSION = "mode2-common-v2"
ROLE_CLASSIFIER_VERSION = "mode2-role-v2"
COMPLETENESS_SCANNER_VERSION = "mode2-completeness-v2.2"

DEFAULT_TRANSLATE_TARGET_WORDS = 7_000
DEFAULT_TRANSLATE_MAX_WORDS = 10_000
DEFAULT_TRANSLATE_HARD_MAX_WORDS = 12_000
STABLE_TRANSLATE_TARGET_WORDS = 10_000
STABLE_TRANSLATE_MAX_WORDS = 12_000
STABLE_TRANSLATE_HARD_MAX_WORDS = 15_000
DEFAULT_REVIEW_TARGET_WORDS = 16_000
DEFAULT_REVIEW_MAX_WORDS = 22_000
DEFAULT_WAVE_BODY_CHAPTERS = 4
DEFAULT_WAVE_WORDS = 40_000
DEFAULT_AGENT_CONCURRENCY = 2

SUBSTANTIVE_ROLES = frozenset({"body", "frontmatter_body", "introduction"})
NON_BODY_ROLES = frozenset({"misc", "references", "index"})

# Only failures which make the artifact untrustworthy or unmergeable are hard
# gates.  Content heuristics remain visible, but a human decision can resolve or
# defer them without editing program state by hand.
HARD_GATE_RULES = frozenset(
    {
        "protocol_mismatch",
        "stage_mismatch",
        "chapter_mismatch",
        "unit_mismatch",
        "source_hash_mismatch",
        "packet_hash_mismatch",
        "block_missing",
        "block_added",
        "block_duplicate",
        "block_order_mismatch",
        "empty_translation",
        "source_dir_missing",
        "source_chapters_missing",
        "target_dir_missing",
        "target_chapter_missing",
        "note_labels_mismatch",
        "object_markers_mismatch",
    }
)
NON_OVERRIDABLE_COMPLETENESS_RULES = frozenset(
    {
        "source_copy_similarity_high",
        "source_copied_verbatim",
        "target_language_missing",
        "catastrophic_incomplete_translation",
        "eligible_word_accounting_mismatch",
        "protected_anchor_substitution",
        "formula_identity_mismatch",
    }
)
HUMAN_DECISIONS = frozenset({"keep", "accept-risk", "repair", "edit", "defer", "restore", "preview-only"})
BLOCK_CONTRACT = {
    "identity": "immutable",
    "required": ("id", "source_sha256"),
    "result_requirements": ("same_id", "same_order", "non_empty"),
}


def normalized_role(value: Any) -> str:
    role = str(value or "body").strip().casefold()
    return role if role in SUBSTANTIVE_ROLES | NON_BODY_ROLES else "body"


def is_substantive_role(value: Any) -> bool:
    return normalized_role(value) in SUBSTANTIVE_ROLES


def first_substantive_chapter(
    chapter_ids: Sequence[str], role_map: Mapping[str, str] | None = None
) -> str | None:
    roles = role_map or {}
    return next((item for item in chapter_ids if is_substantive_role(roles.get(item, "body"))), None)


def select_wave(
    chapter_ids: Sequence[str],
    *,
    role_map: Mapping[str, str] | None = None,
    word_counts: Mapping[str, int] | None = None,
    max_body_chapters: int = DEFAULT_WAVE_BODY_CHAPTERS,
    max_words: int = DEFAULT_WAVE_WORDS,
) -> list[str]:
    """Select one ordered work wave.

    Miscellaneous matter may ride in the same wave without consuming a body
    chapter slot. References and indexes are deterministic output policy, not
    model translation work, so they are excluded here.
    """
    if max_body_chapters <= 0 or max_words <= 0:
        raise ValueError("wave limits must be positive")
    roles = role_map or {}
    words = word_counts or {}
    selected: list[str] = []
    body_count = 0
    body_words = 0
    for chapter_id in chapter_ids:
        role = normalized_role(roles.get(chapter_id, "body"))
        if role in {"references", "index"}:
            continue
        size = max(0, int(words.get(chapter_id, 0)))
        if is_substantive_role(role):
            if body_count and (body_count >= max_body_chapters or body_words + size > max_words):
                break
            body_count += 1
            body_words += size
        selected.append(chapter_id)
    return selected


def is_hard_gate(rule: Any) -> bool:
    return str(rule or "").strip() in HARD_GATE_RULES


def is_non_overridable_completeness_rule(rule: Any) -> bool:
    return str(rule or "").strip() in NON_OVERRIDABLE_COMPLETENESS_RULES


_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
_REFERENCE_RE = re.compile(r"^(references|bibliography|works cited|endnotes|notes)\b", re.I)
_INDEX_RE = re.compile(r"^(index|subject index|name index)\b", re.I)
_MAJOR_RE = re.compile(
    r"^(?:(?:c\s*h\s*a\s*p\s*t\s*e\s*r)|chapter|part|volume|annex|appendix)\s*(?:[0-9ivxlcdm]+\b|[:.\-])",
    re.I,
)
_ANNEX_GROUP_RE = re.compile(r"^(?:annexes|appendices)\s+to\s+chapter\s+[0-9ivxlcdm]+\b", re.I)
_FRONT_MAJOR_RE = re.compile(
    r"^(?:contents|table of contents|list of abbreviations|acknowledg(?:e)?ments|"
    r"executive summary|introduction|preface|foreword|technical annexes?)$",
    re.I,
)
_CITATION_RE = re.compile(
    r"(?:\((?:18|19|20)\d{2}[a-z]?\)|\b(?:18|19|20)\d{2}[a-z]?\b|doi(?:\.org|:)|https?://|isbn\b|"
    r"\b(?:press|publishing|publisher|journal|vol\.|volume|no\.|pp\.)\b)",
    re.I,
)
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,}(?:[-/][A-Z0-9]+)*(?![A-Za-z0-9])")
_FORMULA_VARIABLE_RE = re.compile(r"\b[A-Z](?=\s*(?:\(|\[|\{|=|\+|-|\*|/|$))")
_FORMULA_FRAGMENT_TOKEN_RE = re.compile(
    r"[A-Z][A-Z0-9]{1,}(?:[-/][A-Z0-9]+)*|"
    r"\d+(?:\.\d+)?%?|"
    r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])|"
    r"[Δδβγλμσθρ]|"
    r"[=+\-−×÷/*^]"
)


def english_word_count(value: str) -> int:
    return len(_EN_WORD_RE.findall(value or ""))


def han_character_count(value: str) -> int:
    return len(_HAN_RE.findall(value or ""))


def protected_acronyms(value: str) -> set[str]:
    """Return explicit source anchors that normally survive Chinese translation."""
    # Older executors occasionally nested a MODE2 protocol comment inside a
    # target block.  Protocol vocabulary is not translation content and must
    # never appear as a newly introduced acronym.
    return {item.upper() for item in _ACRONYM_RE.findall(_plain_text(value or ""))}


def formula_signature(value: str) -> dict[str, Any] | None:
    """Extract a deliberately small signature for displayed or inline equations."""
    text = _plain_text(value)
    if "=" not in text:
        return None
    variables = {item.upper() for item in _FORMULA_VARIABLE_RE.findall(text)}
    return {"has_equals": True, "variables": sorted(variables)}


def _formula_fragment_tokens(value: str) -> list[str]:
    """Return stable math anchors from a short OCR/MinerU formula fragment."""
    tokens = _FORMULA_FRAGMENT_TOKEN_RE.findall(_plain_text(value))
    return ["-" if token == "−" else token.upper() if len(token) > 1 and token[0].isalpha() else token for token in tokens]


def _formula_fragment_shape(value: str) -> bool:
    """Identify short formula pieces without treating ordinary short prose as math."""
    raw = (value or "").strip()
    # MinerU commonly emits chapter titles and table-of-contents lines as short
    # all-caps blocks next to real equations.  A heading is a hard boundary for
    # formula-fragment expansion, even when it contains a chapter number.
    if _heading(raw):
        return False
    text = _plain_text(value)
    if not text or len(text) > 64:
        return False
    tokens = _formula_fragment_tokens(text)
    if not tokens:
        return False
    words = re.findall(r"[A-Za-z]+", text)
    ordinary_words = [word for word in words if len(word) > 1 and not word.isupper()]
    has_math_signal = bool(re.search(r"[=+\-−×÷/*^Δδβγλμσθρ%]|\d", text))
    if has_math_signal:
        return True
    # A lone variable/acronym may be a fragmented equation label.  Multi-word
    # all-caps prose such as "TECHNICAL ANNEXES" is not.
    return len(tokens) == 1 and len(words) <= 1 and not ordinary_words


def _is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    iterator = iter(actual)
    return all(any(candidate == token for candidate in iterator) for token in expected)


def _formula_fragment_indices(blocks: Sequence[Mapping[str, Any]]) -> set[int]:
    """Expand equation-bearing blocks over adjacent short OCR formula pieces."""
    plain = [_plain_text(str(block.get("text") or "")) for block in blocks]
    seeds = [index for index, text in enumerate(plain) if formula_signature(text)]
    fragments: set[int] = set()
    for seed in seeds:
        if _formula_fragment_shape(plain[seed]):
            fragments.add(seed)
        index = seed - 1
        while index >= 0 and _formula_fragment_shape(plain[index]):
            fragments.add(index)
            index -= 1
        index = seed + 1
        while index < len(plain) and _formula_fragment_shape(plain[index]):
            fragments.add(index)
            index += 1
    return fragments


def _plain_text(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value or "", flags=re.S)
    value = re.sub(r"\[PDF Page\s+[^\]]+\]", " ", value, flags=re.I)
    value = re.sub(r"\[(?:Figure|Table)\b[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"\[\^[^\]\s]+\]", " ", value)
    value = re.sub(r"[`*_>#|\[\](){}]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normal_text(value: str) -> str:
    return re.sub(r"\W+", "", _plain_text(value).casefold(), flags=re.UNICODE)


def _heading(value: str) -> tuple[int, str] | None:
    match = _HEADING_RE.match((value or "").strip())
    if not match:
        return None
    return len(match.group(1)), re.sub(r"\s+", " ", match.group(2)).strip()


def _major_heading(level: int, title: str) -> bool:
    compact = re.sub(r"(?i)^c\s*h\s*a\s*p\s*t\s*e\s*r", "CHAPTER", title).strip()
    return bool(_MAJOR_RE.match(compact) or _ANNEX_GROUP_RE.match(compact))


def _semantic_heading(value: str) -> tuple[int, str] | None:
    heading = _heading(value)
    if heading:
        return heading
    plain = _plain_text(value)
    if _major_heading(0, plain):
        return 0, plain
    return None


def _citation_confidence(text: str) -> float:
    plain = _plain_text(text)
    if not plain:
        return 0.0
    signals = len(_CITATION_RE.findall(plain))
    author_shape = bool(re.match(r"^[A-Z][A-Za-z'’\-]+(?:,|\s+[A-Z]\.)", plain))
    hanging_entry = bool(re.match(r"^(?:\[?\d+\]?\.?\s+|[A-Z][A-Za-z'’\-]+,)", plain))
    score = 0.35 * min(signals, 2) + (0.2 if author_shape else 0.0) + (0.15 if hanging_entry else 0.0)
    return round(min(score, 1.0), 3)


def classify_semantic_blocks(blocks: Sequence[Mapping[str, Any]], *, chapter_id: str = "book") -> dict[str, Any]:
    """Build semantic sections and conservative role declarations.

    File boundaries are deliberately ignored.  A References span ends at the
    next major heading, and only citation-shaped entries are safe to preserve.
    Everything ambiguous remains translatable.
    """
    sections: list[dict[str, Any]] = []
    roles: dict[str, dict[str, Any]] = {}
    current_section = f"{chapter_id}_s001"
    current_title = chapter_id
    backmatter: str | None = None
    section_start = 0

    def close_section(end: int) -> None:
        if end < section_start:
            return
        sections.append(
            {
                "id": current_section,
                "title": current_title,
                "start_ordinal": section_start + 1,
                "end_ordinal": end + 1,
            }
        )

    semantic_headings = [_semantic_heading(str(block.get("text") or "")) for block in blocks]
    explicit_major = {
        index for index, heading in enumerate(semantic_headings)
        if heading and _major_heading(*heading)
    }
    level_one = {
        index for index, heading in enumerate(semantic_headings)
        if heading and heading[0] == 1
    }
    # A clean Markdown/EPUB source may use H1 as its chapter boundary.  MinerU
    # can also label almost every heading H1, so H1 is trusted only when there
    # are no stronger chapter markers and the count is still plausible.
    trust_level_one = not explicit_major and 0 < len(level_one) <= 40
    front_major = {
        index for index, heading in enumerate(semantic_headings)
        if heading and heading[0] in {0, 1} and _FRONT_MAJOR_RE.match(heading[1])
    }
    major_boundaries = explicit_major | front_major | (level_one if trust_level_one else set())

    for index, block in enumerate(blocks):
        block_id = str(block.get("id") or f"{chapter_id}_b{index + 1:04d}")
        text = str(block.get("text") or "")
        heading = semantic_headings[index]
        if heading and index in major_boundaries:
            if index:
                close_section(index - 1)
            current_section = f"{chapter_id}_s{len(sections) + 1:03d}"
            current_title = heading[1]
            section_start = index
            backmatter = None
        markdown_heading = _heading(text)
        heading_title = markdown_heading[1] if markdown_heading else ""
        if markdown_heading and _REFERENCE_RE.match(heading_title):
            backmatter = "references"
        elif markdown_heading and _INDEX_RE.match(heading_title):
            backmatter = "index"

        role = "translate"
        reason = "default_translate"
        confidence = 1.0
        if not _plain_text(text):
            role, reason = "structural", "no_reader_text"
        elif markdown_heading or index in major_boundaries:
            role, reason = "translate", "reader_visible_heading"
        elif backmatter == "references":
            citation_confidence = _citation_confidence(text)
            if citation_confidence >= 0.70:
                role, reason, confidence = "preserve", "bounded_citation_entry", citation_confidence
            else:
                role, reason, confidence = "translate", "ambiguous_reference_span", citation_confidence
        elif backmatter == "index":
            index_shape = bool(re.search(r"(?:,\s*)?\b\d{1,4}(?:[-–,]\d{1,4})*\s*$", _plain_text(text)))
            if index_shape:
                role, reason, confidence = "preserve", "bounded_index_entry", 0.85
            else:
                role, reason, confidence = "translate", "ambiguous_index_span", 0.4
        roles[block_id] = {
            "role": role,
            "reason": reason,
            "confidence": confidence,
            "section_id": current_section,
            "classifier_version": ROLE_CLASSIFIER_VERSION,
        }
    if blocks:
        close_section(len(blocks) - 1)
    return {"roles": roles, "major_sections": sections, "classifier_version": ROLE_CLASSIFIER_VERSION}


def scan_translation_completeness(
    source_blocks: Sequence[Mapping[str, Any]],
    targets: Mapping[str, str],
    role_records: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect copied/untranslated prose independently from upstream labels."""
    declarations = role_records or {}
    hard: list[dict[str, Any]] = []
    suspicious: list[dict[str, Any]] = []
    raw_words = eligible_words = preserved_words = checked_words = 0
    consecutive: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    formula_fragments = _formula_fragment_indices(source_blocks)

    def flush_run() -> None:
        nonlocal consecutive
        if len(consecutive) >= 3 and sum(item["source_words"] for item in consecutive) >= 150:
            runs.append(
                {
                    "block_ids": [item["block_id"] for item in consecutive],
                    "source_words": sum(item["source_words"] for item in consecutive),
                }
            )
        consecutive = []

    for block_index, block in enumerate(source_blocks):
        block_id = str(block.get("id") or "")
        source = str(block.get("text") or "")
        source_words = english_word_count(_plain_text(source))
        raw_words += source_words
        declaration = declarations.get(block_id, {})
        if isinstance(declaration, str):
            role, confidence = declaration, 0.0
        else:
            role = str(declaration.get("role") or declaration.get("declared_role") or "translate")
            confidence = float(declaration.get("confidence") or declaration.get("role_confidence") or 0.0)
        reliable_preserve = role in {"preserve", "bibliography", "index"} and confidence >= 0.70
        if reliable_preserve:
            preserved_words += source_words
            flush_run()
            continue
        eligible_words += source_words
        target = str(targets.get(block_id) or "")
        if block_id in targets:
            checked_words += source_words
        src_norm = _normal_text(source)
        tgt_norm = _normal_text(target)
        similarity = SequenceMatcher(None, src_norm, tgt_norm).ratio() if src_norm and tgt_norm else 0.0
        target_english = english_word_count(_plain_text(target))
        target_han = han_character_count(_plain_text(target))
        source_acronyms = protected_acronyms(source)
        target_acronyms = protected_acronyms(target)
        retained_acronyms = source_acronyms & target_acronyms
        foreign_acronyms = target_acronyms - source_acronyms
        if (
            len(source_acronyms) >= 2
            and len(foreign_acronyms) >= 2
            and len(retained_acronyms) < max(1, (len(source_acronyms) + 1) // 2)
        ):
            hard.append(
                {
                    "rule": "protected_anchor_substitution",
                    "block_id": block_id,
                    "source_anchors": sorted(source_acronyms),
                    "retained_anchors": sorted(retained_acronyms),
                    "unexpected_target_anchors": sorted(foreign_acronyms),
                }
            )
        source_formula = formula_signature(source)
        if source_formula:
            target_formula = formula_signature(target)
            source_variables = set(source_formula["variables"])
            target_variables = set((target_formula or {}).get("variables", []))
            required_variables = min(len(source_variables), max(1, (len(source_variables) + 1) // 2)) if source_variables else 0
            if target_formula is None or len(source_variables & target_variables) < required_variables:
                hard.append(
                    {
                        "rule": "formula_identity_mismatch",
                        "block_id": block_id,
                        "source_variables": sorted(source_variables),
                        "retained_variables": sorted(source_variables & target_variables),
                        "target_has_equals": bool(target_formula),
                    }
                )
        if block_index in formula_fragments:
            source_tokens = _formula_fragment_tokens(source)
            target_tokens = _formula_fragment_tokens(target)
            if source_tokens and not _is_subsequence(source_tokens, target_tokens):
                hard.append(
                    {
                        "rule": "formula_fragment_mismatch",
                        "block_id": block_id,
                        "source_tokens": source_tokens,
                        "target_tokens": target_tokens,
                    }
                )
        copied = source_words >= 30 and similarity >= 0.90
        missing_target = source_words >= 30 and not _plain_text(target)
        untranslated = source_words >= 30 and target_han < 8 and target_english >= max(15, source_words * 0.5)
        if copied or untranslated or missing_target:
            item = {
                "block_id": block_id,
                "source_words": source_words,
                "similarity": round(similarity, 4),
                "rule": "source_copy_similarity_high" if copied else "target_language_missing",
            }
            suspicious.append(item)
            consecutive.append(item)
            hard.append(item.copy())
        else:
            flush_run()
    flush_run()
    suspicious_words = sum(item["source_words"] for item in suspicious)
    accounting_ok = raw_words == eligible_words + preserved_words and checked_words == eligible_words
    if not accounting_ok:
        hard.append(
            {
                "rule": "eligible_word_accounting_mismatch",
                "raw_source_words": raw_words,
                "eligible_source_words": eligible_words,
                "reliably_preserved_words": preserved_words,
                "actually_checked_words": checked_words,
            }
        )
    catastrophic = bool(
        suspicious_words >= 500
        or (eligible_words and suspicious_words / eligible_words >= 0.02)
        or runs
    )
    if catastrophic:
        hard.append(
            {
                "rule": "catastrophic_incomplete_translation",
                "untranslated_or_copied_words": suspicious_words,
                "eligible_source_words": eligible_words,
                "ratio": round(suspicious_words / max(eligible_words, 1), 4),
                "continuous_runs": runs,
            }
        )
    return {
        "ok": not hard,
        "catastrophic": catastrophic,
        "hard_failures": hard,
        "suspicious_blocks": suspicious,
        "continuous_runs": runs,
        "summary": {
            "scanner_version": COMPLETENESS_SCANNER_VERSION,
            "raw_source_words": raw_words,
            "eligible_source_words": eligible_words,
            "reliably_preserved_words": preserved_words,
            "actually_checked_words": checked_words,
            "untranslated_or_copied_words": suspicious_words,
        },
    }


def search_hint(text: str, limit: int = 52) -> str:
    plain = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    plain = re.sub(r"\[[^\]]+\]\([^)]*\)", " ", plain)
    plain = re.sub(r"[#>*_`|]+", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= limit:
        return plain
    return plain[:limit].rstrip(" ,.;:，。；：") + "…"


def build_human_locator(
    *,
    chapter_id: str,
    chapter_title: str | None = None,
    source_kind: str | None = None,
    spine_href: str | None = None,
    heading: str | None = None,
    paragraph_ordinal: int | None = None,
    block_ids: Iterable[str] = (),
    pdf_pages: Iterable[int] = (),
    source_text: str = "",
) -> dict[str, Any]:
    pages = sorted({int(page) for page in pdf_pages if int(page) > 0})
    if pages:
        page_label = f"原 PDF 第 {pages[0]} 页" if len(pages) == 1 else f"原 PDF 第 {pages[0]}–{pages[-1]} 页"
    else:
        page_label = None
    parts = [chapter_title or chapter_id]
    if heading and heading != chapter_title:
        parts.append(f"小节“{heading}”")
    if paragraph_ordinal:
        parts.append(f"第 {paragraph_ordinal} 段")
    hint = search_hint(source_text)
    if page_label:
        human = page_label + "；" + "，".join(parts)
    else:
        human = "，".join(parts)
        if hint:
            human += f"；检索“{hint}”"
    return {
        "source_kind": source_kind or "unknown",
        "chapter_id": chapter_id,
        "chapter_title": chapter_title or chapter_id,
        "spine_href": spine_href,
        "heading": heading,
        "paragraph_ordinal": paragraph_ordinal,
        "block_ids": [str(item) for item in block_ids],
        "pdf_pages": pages,
        "page_label": page_label or "原书无可靠页码",
        "search_hint": hint,
        "human_location": human,
    }
