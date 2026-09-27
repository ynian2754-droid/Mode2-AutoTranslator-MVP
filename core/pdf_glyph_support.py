"""Curated, recorded equivalence substitutions for the embedded PDF font.

The portable PDF path draws with the primary embedded TrueType font and, for a
reviewed set of Unicode ranges, an optional math fallback.  The frozen
architecture decision (``归档/PDF_PORTABILITY_ATOMIC_REPAIR_PLAN.md`` §2.2)
forbids *silently* replacing a missing glyph with a box, a question mark, or
nothing.

This module implements the single approved amendment to that rule: a short,
human-reviewed list of characters that have a semantically equivalent
counterpart inside the embedded fonts may be substituted at **render time**,
and every substitution is reported (codepoint, replacement, and the affected
unit ids).  Substitution is the *last resort* — a character the font chain can
draw is never rewritten — and any other codepoint without a glyph still aborts
the export before a single byte is written, exactly as before.

Nothing here reads or writes persisted translations; the substitution is a
presentation step that can be repeated and audited.

This module deliberately does not import ReportLab: ``PipelineManager`` uses the
scan for the read-only pre-export check even in environments where the optional
PDF dependency is unavailable, so the caller passes a ``has_glyph`` predicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

# How many affected units a single error/precheck line names before eliding.
MAX_REPORTED_UNITS = 8
REPLACEMENT_CHARACTER = "\uFFFD"
_AUTO_RETRANSLATION_STATUSES = {"needs_action", "passed", "accepted_risk"}


@dataclass(frozen=True)
class EquivalentCharacter:
    """One reviewed mapping from an unrenderable codepoint to a rendered one."""

    codepoint: int
    replacement: str
    name: str
    reason: str

    @property
    def label(self) -> str:
        return f"U+{self.codepoint:04X}"


# Verified against the embedded fonts: the primary CJK font
# (assets/fonts/pdf/Mode2SansCJKCN-Regular.ttf) has no glyph for U+27E8/U+27E9,
# while the math fallback (assets/fonts/pdf/DejaVuSans.ttf) does — so these
# entries are the fallback for a checkout without the math asset, not the
# normal path.  U+3008/U+3009 exist in the primary font and carry the same
# bracket meaning in mathematical expectation notation, which makes the
# fallback substitution faithful when it is needed at all.
# U+2329/U+232A were probed too and are already renderable, so they are
# deliberately NOT mapped: only characters the fonts cannot draw are rewritten.
PDF_EQUIVALENT_CHARACTERS: tuple[EquivalentCharacter, ...] = (
    EquivalentCharacter(
        0x27E8,
        "\u3008",
        "MATHEMATICAL LEFT ANGLE BRACKET",
        "数学字体资产缺失时的兜底；CJK 左尖括号〈 语义等价",
    ),
    EquivalentCharacter(
        0x27E9,
        "\u3009",
        "MATHEMATICAL RIGHT ANGLE BRACKET",
        "数学字体资产缺失时的兜底；CJK 右尖括号〉 语义等价",
    ),
)

_BY_CODEPOINT: dict[int, EquivalentCharacter] = {
    item.codepoint: item for item in PDF_EQUIVALENT_CHARACTERS
}


def equivalent_for(codepoint: int) -> EquivalentCharacter | None:
    """Return the reviewed mapping for one codepoint, or None if it is blocking."""

    return _BY_CODEPOINT.get(codepoint)


def apply_equivalents(
    text: str,
    *,
    renderable: Callable[[int], bool] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Replace only allow-listed codepoints; return the text and one record each.

    ``renderable`` is the font chain's coverage predicate.  A character the
    fonts can draw is kept exactly as written — substitution is the last
    resort for codepoints that are still unrenderable, not a preference.

    Records carry the controlled vocabulary used in reports and artifact
    metadata: ``codepoint``, ``character`` and ``replacement``.
    """

    if not text:
        return text, []
    records: list[dict[str, str]] = []
    pieces: list[str] = []
    for character in text:
        codepoint = ord(character)
        entry = _BY_CODEPOINT.get(codepoint)
        if entry is None or (renderable is not None and renderable(codepoint)):
            pieces.append(character)
            continue
        pieces.append(entry.replacement)
        records.append(
            {
                "codepoint": entry.label,
                "character": character,
                "replacement": entry.replacement,
            }
        )
    if not records:
        return text, []
    return "".join(pieces), records


def _empty_scan(available: bool) -> dict[str, Any]:
    return {
        "available": available,
        "replaceable": [],
        "blocking": [],
        "units": [],
        "replacement_character_units": [],
        "missing_total": 0,
    }


def unavailable_scan() -> dict[str, Any]:
    """Neutral result for callers that cannot load the font (never misreports)."""

    return _empty_scan(False)


def scan_text_glyphs(
    units: Iterable[Any],
    has_glyph: Callable[[int], bool],
) -> dict[str, Any]:
    """Classify every codepoint used by saved translations against the font.

    Returns ``replaceable`` (allow-listed), ``blocking`` (must abort export),
    and per-unit ``units`` attribution.  Blank translations are ignored.
    """

    replaceable: dict[int, dict[str, Any]] = {}
    blocking: dict[int, dict[str, Any]] = {}
    unit_rows: list[dict[str, Any]] = []
    replacement_character_units: list[dict[str, Any]] = []
    glyph_cache: dict[int, bool] = {}

    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "")
        text = str(unit.get("translation") or "")
        source = str(unit.get("source") or "")
        source_index = source.find(REPLACEMENT_CHARACTER)
        translation_index = text.find(REPLACEMENT_CHARACTER)
        if source_index >= 0 or translation_index >= 0:
            user_edited = bool(unit.get("user_edited_translation"))
            source_sha256 = str(unit.get("source_sha256") or "")
            revision = unit.get("translation_revision")
            status = str(unit.get("status") or "")
            replacement_character_units.append(
                {
                    "id": unit_id,
                    "source_contains": source_index >= 0,
                    "translation_contains": translation_index >= 0,
                    "translation_repairable": bool(
                        translation_index >= 0
                        and source_index < 0
                        and not user_edited
                        and status in _AUTO_RETRANSLATION_STATUSES
                        and source_sha256
                        and isinstance(revision, int)
                        and not isinstance(revision, bool)
                    ),
                    "user_edited_translation": user_edited,
                    "source_sha256": source_sha256,
                    "translation_revision": revision,
                    "source_excerpt": _replacement_excerpt(source, source_index),
                    "translation_excerpt": _replacement_excerpt(text, translation_index),
                }
            )
        if not text.strip():
            continue
        unit_labels: list[str] = []
        for character in text:
            # Whitespace is normalized by the renderer before drawing and has no
            # glyph of its own, so it is never a coverage question.  Scanning the
            # raw saved text would otherwise report every translated newline.
            if character.isspace():
                continue
            codepoint = ord(character)
            # U+FFFD is a data-loss marker, not a normal printable glyph.
            # Block it even if a font contains a replacement-box glyph.
            has = False if character == REPLACEMENT_CHARACTER else glyph_cache.get(codepoint)
            if has is None:
                has = bool(has_glyph(codepoint))
                glyph_cache[codepoint] = has
            if has:
                continue
            entry = equivalent_for(codepoint)
            bucket = replaceable if entry is not None else blocking
            row = bucket.get(codepoint)
            if row is None:
                row = {
                    "codepoint": f"U+{codepoint:04X}",
                    "character": character,
                    "count": 0,
                    "units": [],
                }
                if entry is not None:
                    row["replacement"] = entry.replacement
                    row["name"] = entry.name
                    row["reason"] = entry.reason
                bucket[codepoint] = row
            row["count"] += 1
            if unit_id and unit_id not in row["units"]:
                row["units"].append(unit_id)
            label = f"U+{codepoint:04X}"
            if label not in unit_labels:
                unit_labels.append(label)
        if unit_labels:
            unit_rows.append({"id": unit_id, "characters": unit_labels})

    def ordered(bucket: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        return [bucket[codepoint] for codepoint in sorted(bucket)]

    scan = _empty_scan(True)
    scan["replaceable"] = ordered(replaceable)
    scan["blocking"] = ordered(blocking)
    scan["units"] = unit_rows
    scan["replacement_character_units"] = replacement_character_units
    scan["missing_total"] = sum(
        row["count"] for row in scan["replaceable"] + scan["blocking"]
    )
    return scan


def _replacement_excerpt(text: str, index: int, *, radius: int = 36) -> str:
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(REPLACEMENT_CHARACTER) + radius)
    excerpt = text[start:end].replace("\r", " ").replace("\n", " ")
    return ("…" if start else "") + excerpt + ("…" if end < len(text) else "")


def format_unit_list(units: list[str], *, limit: int = MAX_REPORTED_UNITS) -> str:
    """Render a bounded, human-readable unit id list."""

    shown = [str(unit) for unit in units[:limit] if str(unit)]
    if not shown:
        return ""
    suffix = f" 等 {len(units)} 个单元" if len(units) > limit else ""
    return "、".join(shown) + suffix


def blocking_message(scan: dict[str, Any]) -> str:
    """One actionable message naming the codepoints and the units to fix."""

    rows = list(scan.get("blocking") or [])
    parts: list[str] = []
    replacement_rows = [row for row in rows if str(row.get("codepoint") or "").upper() == "U+FFFD"]
    other_rows = [row for row in rows if str(row.get("codepoint") or "").upper() != "U+FFFD"]
    if replacement_rows:
        units = sorted({str(unit) for row in replacement_rows for unit in row.get("units", []) if str(unit)})
        where = format_unit_list(units, limit=3)
        parts.append(
            "译文中检测到 Unicode 替代字符 U+FFFD（�），原字符可能已经丢失"
            + (f"，涉及 {where}" if where else "")
            + "。源文完整的单元可在工作台批量重译并复检；源文也含该字符的单元需要核对原文件。"
        )
    for row in other_rows[:6]:
        where = format_unit_list(list(row.get("units") or []), limit=3)
        character = str(row.get("character") or "")
        parts.append(
            f"{row.get('codepoint', '?')}（{character}）" + (f" 出现在 {where}" if where else "")
        )
    if len(other_rows) > 6:
        parts.append(f"…（另有 {len(other_rows)} 种字体缺字）")
    if other_rows:
        parts.insert(0, "PDF 字体缺少字符，且没有已登记的等价字符")
        parts.append("请先修改这些单元后再导出 PDF")
    return "；".join(parts) + ("。" if parts else "")


def summarize_replacements(
    records: list[dict[str, str]],
    scan: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the artifact summary, or None when nothing was replaced."""

    if not records:
        return None
    counts: dict[str, int] = {}
    for record in records:
        label = str(record.get("codepoint") or "")
        if label:
            counts[label] = counts.get(label, 0) + 1

    characters: list[dict[str, Any]] = []
    known: set[str] = set()
    for row in scan.get("replaceable") or []:
        label = str(row.get("codepoint") or "")
        if not label or label not in counts:
            continue
        known.add(label)
        characters.append(
            {
                "codepoint": label,
                "character": row.get("character"),
                "replacement": row.get("replacement"),
                "count": counts[label],
                "units": list(row.get("units") or []),
            }
        )
    # Defensive: a render-time replacement the scan did not attribute.
    for label in sorted(counts):
        if label in known:
            continue
        characters.append(
            {
                "codepoint": label,
                "character": None,
                "replacement": None,
                "count": counts[label],
                "units": [],
            }
        )
    if not characters:
        return None
    return {
        "count": sum(int(item["count"]) for item in characters),
        "characters": characters,
    }
