"""Language, copy and coverage gates for pure-agent translation results."""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

import mode2_common


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
EN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
MARKER_RE = re.compile(
    r"\[PDF Page\s+[^\]]+\]|\[(?:Figure|Table)\b[^\]]*\]|\[\^[^\]\s]+\]",
    re.I,
)
REFERENCE_HEADING_RE = re.compile(r"^#{1,6}\s*(references|bibliography|works cited)\b", re.I)
INDEX_HEADING_RE = re.compile(r"^#{1,6}\s*(index|subject index|name index)\b", re.I)
CITATION_RE = re.compile(r"(?:doi\.org/|ISBN\b|OECD Publishing|University Press)", re.I)
FOOTNOTE_DEFINITION_RE = re.compile(r"^\[\^[^\]\s]+\]\s*[:：]")
INDIAN_NUMBER_UNIT_RE = re.compile(
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s+(?P<unit>lakh\s+crores?|crores?|lakhs?)\b",
    re.I,
)


def english_words(value: str) -> int:
    return len(EN_WORD_RE.findall(value))


def han_chars(value: str) -> int:
    return len(HAN_RE.findall(value))


def visible_text(value: str) -> str:
    value = MARKER_RE.sub(" ", value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[`*_>#|\[\](){}]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized(value: str) -> str:
    return re.sub(r"\W+", "", visible_text(value).casefold(), flags=re.UNICODE)


def classify_chapter_blocks(blocks: list[dict[str, Any]]) -> dict[str, str]:
    """Compatibility view over the v2 evidence-bearing classifier."""
    structure = classify_chapter_structure(blocks)
    result: dict[str, str] = {}
    for block in blocks:
        block_id = block["id"]
        text = block["text"].strip()
        if FOOTNOTE_DEFINITION_RE.match(text):
            result[block_id] = "note"
            continue
        record = structure["roles"][block_id]
        if record["role"] == "preserve":
            result[block_id] = "index" if "index" in record["reason"] else "bibliography"
        elif record["role"] == "structural":
            result[block_id] = "structural"
        elif text.startswith(("```", "~~~")):
            result[block_id] = "formula"
        else:
            result[block_id] = "translatable"
    return result


def classify_chapter_structure(blocks: list[dict[str, Any]], chapter_id: str | None = None) -> dict[str, Any]:
    chapter = chapter_id or (str(blocks[0].get("chapter")) if blocks else "book")
    structure = mode2_common.classify_semantic_blocks(blocks, chapter_id=chapter)
    for block in blocks:
        if FOOTNOTE_DEFINITION_RE.match(block["text"].strip()):
            structure["roles"][block["id"]] = {
                "role": "translate",
                "reason": "footnote_definition",
                "confidence": 1.0,
                "section_id": structure["roles"][block["id"]]["section_id"],
                "classifier_version": mode2_common.ROLE_CLASSIFIER_VERSION,
            }
    return structure


def validate_translation(
    source_blocks: list[dict[str, Any]],
    translated: list[tuple[str, str]],
    roles: dict[str, str],
) -> dict[str, Any]:
    """Return hard failures, review advisories and audit-friendly metrics."""
    target_by_id = dict(translated)
    hard: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    metrics: dict[str, dict[str, Any]] = {}
    seen_targets: dict[str, str] = {}
    total_source_words = 0
    total_han = 0

    for source in source_blocks:
        block_id = source["id"]
        role_value = roles.get(block_id, "translatable")
        if isinstance(role_value, dict):
            declared = role_value.get("role", "translate")
            role = "bibliography" if declared == "preserve" else ("structural" if declared == "structural" else "translatable")
        else:
            role = role_value
        src = visible_text(source["text"])
        tgt = visible_text(target_by_id.get(block_id, ""))
        src_words = english_words(src)
        tgt_words = english_words(tgt)
        tgt_han = han_chars(tgt)
        similarity = SequenceMatcher(None, normalized(src), normalized(tgt)).ratio() if src and tgt else 0.0
        metrics[block_id] = {
            "role": role,
            "source_words": src_words,
            "target_english_words": tgt_words,
            "target_han_chars": tgt_han,
            "source_similarity": round(similarity, 4),
        }

        if role in {"bibliography", "index"}:
            if source["text"].strip() != target_by_id.get(block_id, "").strip():
                hard.append({"rule": "preserved_backmatter_modified", "block_id": block_id, "role": role})
            continue
        if role not in {"translatable", "note"}:
            continue
        total_source_words += src_words
        total_han += tgt_han

        if src_words >= 30 and normalized(src) == normalized(tgt):
            hard.append({"rule": "source_copied_verbatim", "block_id": block_id})
        elif src_words >= 30 and similarity >= 0.90:
            hard.append({"rule": "source_copy_similarity_high", "block_id": block_id, "ratio": round(similarity, 3)})
        if src_words >= 30 and tgt_han < 8 and tgt_words >= src_words * 0.5:
            hard.append({"rule": "target_language_missing", "block_id": block_id})
        elif src_words >= 8 and tgt_han < 5 and tgt_words >= src_words * 0.70:
            # Short titles, acronyms and labels are too ambiguous to justify a
            # non-interactive rejection.  Keep the evidence visible for review.
            advisories.append({"rule": "short_source_line_untranslated", "block_id": block_id})
        if src_words >= 100 and tgt_han < src_words * 0.20:
            advisories.append({"rule": "block_content_severely_short", "block_id": block_id})
        elif src_words >= 80 and tgt_han < src_words * 0.55:
            advisories.append({"rule": "block_content_short", "block_id": block_id})
        if re.search(r"[國體臺灣後發裡與為於學會]", tgt):
            advisories.append({"rule": "traditional_character_candidate", "block_id": block_id})

        indian_units = list(INDIAN_NUMBER_UNIT_RE.finditer(src))
        if indian_units:
            advisories.append(
                {
                    "rule": "indian_number_unit_requires_review",
                    "block_id": block_id,
                    "values": [match.group(0) for match in indian_units],
                }
            )
            for match in indian_units:
                unit = match.group("unit").casefold()
                raw_value = match.group("value")
                if "crore" in unit and "lakh crore" not in unit:
                    same_number_as_yi = re.search(
                        rf"(?<![\d.,]){re.escape(raw_value)}\s*亿(?:卢比)?",
                        tgt,
                    )
                    if same_number_as_yi:
                        advisories.append(
                            {
                                "rule": "indian_crore_same_number_as_yi",
                                "block_id": block_id,
                                "source_value": raw_value,
                                "source_unit": match.group("unit"),
                            }
                        )
                        break

        fingerprint = normalized(tgt)
        if len(fingerprint) >= 40:
            previous = seen_targets.get(fingerprint)
            if previous and normalized(src) != normalized(next(item["text"] for item in source_blocks if item["id"] == previous)):
                advisories.append({"rule": "duplicate_target_text", "block_id": block_id, "matches": previous})
            else:
                seen_targets[fingerprint] = block_id

    if total_source_words >= 200 and total_han < total_source_words * 0.30:
        advisories.append(
            {
                "rule": "unit_target_language_missing",
                "source_words": total_source_words,
                "target_han_chars": total_han,
            }
        )

    role_records = {}
    for block_id, role in roles.items():
        if isinstance(role, dict):
            role_records[block_id] = role
        else:
            role_records[block_id] = {
                "role": "preserve" if role in {"bibliography", "index"} else "translate",
                "confidence": 1.0,
                "reason": "agent_role_map",
            }
    completeness = mode2_common.scan_translation_completeness(source_blocks, target_by_id, role_records)
    existing = {(item.get("rule"), item.get("block_id")) for item in hard}
    for item in completeness["hard_failures"]:
        key = (item.get("rule"), item.get("block_id"))
        if key not in existing:
            hard.append(item)
            existing.add(key)
    counts = Counter(item["rule"] for item in hard)
    return {
        "ok": not hard,
        "hard_failures": hard,
        "advisories": advisories,
        "metrics": metrics,
        "summary": {
            "source_words": total_source_words,
            "target_han_chars": total_han,
            "hard_failure_count": len(hard),
            "advisory_count": len(advisories),
            "hard_rules": dict(counts),
            **completeness["summary"],
            "catastrophic": completeness["catastrophic"],
        },
        "completeness": completeness,
    }
