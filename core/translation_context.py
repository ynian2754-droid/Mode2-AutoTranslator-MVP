"""Request-local source context extracted from neighbouring translation units.

The context is deliberately built from source text only.  It must never depend on
neighbouring translations or on the order in which worker threads finish.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import mode2_common

from .sentence_segmentation import sentence_spans


DEFAULT_CONTEXT_RATIO = 0.4
MAX_CONTEXT_WORDS = 4_000
_NON_SPACE_RE = re.compile(r"\S+")


class TranslationContextError(ValueError):
    """The source units or context budget cannot form a safe request context."""


def default_context_words(target_words: int) -> int:
    """Return the initial per-side context budget for a target chunk length."""

    if isinstance(target_words, bool) or not isinstance(target_words, int) or target_words <= 0:
        raise TranslationContextError("目标切分词数必须是大于 0 的整数。")
    return max(0, round(target_words * DEFAULT_CONTEXT_RATIO))


def validate_context_words(value: Any, *, field_name: str = "context_words") -> int:
    """Validate an explicit context budget without silently coercing user input."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TranslationContextError(f"{field_name} 必须是非负整数。")
    if value < 0:
        raise TranslationContextError(f"{field_name} 不能小于 0。")
    if value > MAX_CONTEXT_WORDS:
        raise TranslationContextError(
            f"{field_name} 不能超过 {MAX_CONTEXT_WORDS} 个词。"
        )
    return value


def configured_context_words(
    config: Mapping[str, Any] | None,
    *,
    field_name: str,
    target_words: int,
) -> int:
    """Read a project setting while keeping legacy projects usable.

    Stage 4 will expose and strictly validate these settings through the settings
    API.  Until then, missing or malformed persisted values use the documented
    target-length-derived default rather than breaking an existing project.
    """

    raw = config.get(field_name) if isinstance(config, Mapping) else None
    if raw is None:
        return default_context_words(target_words)
    try:
        return validate_context_words(raw, field_name=field_name)
    except TranslationContextError:
        return default_context_words(target_words)


def build_translation_context(
    units: Sequence[Mapping[str, Any]],
    current_unit_id: str,
    *,
    previous_words: int,
    next_words: int,
) -> dict[str, str]:
    """Build the previous/next source context for one stable Unit ID.

    Neighbouring units are collected in document order.  A context side starts at
    the current boundary, keeps whole sentence-like spans where possible, and only
    falls back to a word boundary for one oversized, unterminated span.
    """

    previous_budget = validate_context_words(
        previous_words,
        field_name="previous_context_words",
    )
    next_budget = validate_context_words(
        next_words,
        field_name="next_context_words",
    )
    ordered = _ordered_units(units)
    current_id = str(current_unit_id or "").strip()
    if not current_id:
        raise TranslationContextError("current_unit_id 不能为空。")

    current_index = next(
        (index for index, unit in enumerate(ordered) if str(unit.get("id") or "") == current_id),
        None,
    )
    if current_index is None:
        raise TranslationContextError(f"找不到当前翻译单元：{current_id}")

    result: dict[str, str] = {}
    if previous_budget:
        previous_source = _collect_neighbour_source(
            ordered[:current_index],
            previous_budget,
            reverse=True,
        )
        selected = _select_tail(previous_source, previous_budget)
        if selected:
            result["previous_context"] = selected
    if next_budget:
        next_source = _collect_neighbour_source(
            ordered[current_index + 1 :],
            next_budget,
            reverse=False,
        )
        selected = _select_head(next_source, next_budget)
        if selected:
            result["next_context"] = selected
    return result


def _ordered_units(units: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    entries: list[tuple[int, Mapping[str, Any]]] = []
    seen_orders: set[int] = set()
    seen_ids: set[str] = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            raise TranslationContextError(f"units[{index}] 必须是对象。")
        unit_id = str(unit.get("id") or "").strip()
        if not unit_id:
            raise TranslationContextError(f"units[{index}].id 不能为空。")
        if unit_id in seen_ids:
            raise TranslationContextError(f"翻译单元 ID 重复：{unit_id}")
        seen_ids.add(unit_id)

        order = unit.get("order")
        if isinstance(order, bool) or not isinstance(order, int):
            raise TranslationContextError(f"翻译单元 {unit_id} 缺少有效 order。")
        if order in seen_orders:
            raise TranslationContextError(f"翻译单元 order 重复：{order}")
        seen_orders.add(order)
        entries.append((order, unit))
    entries.sort(key=lambda item: item[0])
    return [unit for _order, unit in entries]


def _collect_neighbour_source(
    neighbours: Sequence[Mapping[str, Any]],
    budget: int,
    *,
    reverse: bool,
) -> str:
    if not neighbours or budget <= 0:
        return ""

    selected: list[str] = []
    total_words = 0
    iterable = reversed(neighbours) if reverse else neighbours
    for unit in iterable:
        source = str(unit.get("source") or "").strip()
        if not source:
            continue
        if reverse:
            selected.insert(0, source)
        else:
            selected.append(source)
        total_words += _word_count(source)
        if total_words >= budget:
            break
    return "\n".join(selected)


def _select_tail(value: str, budget: int) -> str:
    spans = sentence_spans(value)
    if not spans:
        return ""
    selected: list[str] = []
    total_words = 0
    for span in reversed(spans):
        text = span.text
        span_words = _word_count(text)
        if not text.strip():
            continue
        if not selected and span_words > budget:
            return _take_words(text, budget, tail=True)
        selected.append(text)
        total_words += span_words
        if total_words >= budget:
            break
    return "".join(reversed(selected)).strip()


def _select_head(value: str, budget: int) -> str:
    spans = sentence_spans(value)
    if not spans:
        return ""
    selected: list[str] = []
    total_words = 0
    for span in spans:
        text = span.text
        span_words = _word_count(text)
        if not text.strip():
            continue
        if not selected and span_words > budget:
            return _take_words(text, budget, tail=False)
        selected.append(text)
        total_words += span_words
        if total_words >= budget:
            break
    return "".join(selected).strip()


def _take_words(value: str, budget: int, *, tail: bool) -> str:
    if budget <= 0:
        return ""
    matches = list(_NON_SPACE_RE.finditer(value))
    if len(matches) <= budget:
        return value.strip()
    if tail:
        return value[matches[-budget].start() :].strip()
    return value[: matches[budget - 1].end()].strip()


def _word_count(value: str) -> int:
    count = mode2_common.english_word_count(value)
    if count:
        return count
    return len(_NON_SPACE_RE.findall(value or ""))
