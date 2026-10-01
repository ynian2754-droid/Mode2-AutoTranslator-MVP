"""Persisted project configuration compatibility readers and caller-locked writes."""

from __future__ import annotations

from typing import Any, Mapping

from core import project_state
from core.project_state import ProjectStateCell
from core.segmenter import DEFAULT_TARGET_WORDS, validate_target_words
from core.translation_context import (
    MAX_CONTEXT_WORDS, configured_context_words, default_context_words,
    validate_context_words,
)

_UNSET = object()


def configured_target_words(config: Any) -> int:
    """Read the canonical setting, falling back to the legacy key only."""
    if not isinstance(config, dict):
        return DEFAULT_TARGET_WORDS
    key = "target_segment_words" if "target_segment_words" in config else "max_segment_words"
    if key not in config:
        return DEFAULT_TARGET_WORDS
    try:
        return validate_target_words(config[key])
    except (TypeError, ValueError):
        return DEFAULT_TARGET_WORDS


def configured_context_words_for_state(state: Mapping[str, Any]) -> tuple[int, int]:
    """The persisted per-side source-context budgets of this project.

        A missing or malformed value falls back to the documented
        target-derived default (see ``core.translation_context``), so a legacy
        project keeps working without rewriting its config.
        """

    config = state.get("config", {})
    target_words = configured_target_words(config)
    return (
        configured_context_words(
            config, field_name="previous_context_words", target_words=target_words
        ),
        configured_context_words(
            config, field_name="next_context_words", target_words=target_words
        ),
    )


def resolve_explicit_target_words(
    target_segment_words: Any | None,
    max_segment_words: Any | None,
) -> int | None:
    """Resolve explicit new/legacy arguments and reject disagreement."""
    if target_segment_words is not None and max_segment_words is not None:
        target = validate_target_words(target_segment_words)
        legacy = validate_target_words(max_segment_words)
        if target != legacy:
            raise ValueError("target_segment_words 与 max_segment_words 必须一致。")
        return target
    if target_segment_words is not None:
        return validate_target_words(target_segment_words)
    if max_segment_words is not None:
        return validate_target_words(max_segment_words)
    return None


def resolve_segment_words(
    state: Mapping[str, Any],
    value: Any | None = None,
    *,
    target_segment_words: Any | None = None,
    max_segment_words: Any | None = None,
) -> int:
    if value is not None:
        if target_segment_words is not None or max_segment_words is not None:
            raise ValueError("不能同时使用位置参数和命名切分词数参数。")
        max_segment_words = value
    explicit = resolve_explicit_target_words(
        target_segment_words,
        max_segment_words,
    )
    return explicit if explicit is not None else configured_target_words(
        state.get("config", {})
    )


def resolve_concurrency(max_concurrency: Any) -> int:
    if isinstance(max_concurrency, bool):
        raise ValueError("并发数必须是大于或等于 1 的整数。")
    try:
        value = int(max_concurrency)
    except (TypeError, ValueError) as exc:
        raise ValueError("并发数必须是大于或等于 1 的整数。") from exc
    if value != max_concurrency or value < 1:
        raise ValueError("并发数必须是大于或等于 1 的整数。")

    return value


def segmentation_settings(state: Mapping[str, Any]) -> dict[str, Any]:
    target_words = configured_target_words(state.get("config", {}))
    return {
        "target_words": target_words,
        "max_words": target_words,
        "default_target_words": DEFAULT_TARGET_WORDS,
        "default_max_words": DEFAULT_TARGET_WORDS,
        "target_is_hard_limit": False,
        "sentence_boundary_priority": True,
        "emergency_fallback": "unterminated_oversized_text",
        "project_name": state.get("project", {}).get("name"),
    }


def concurrency_settings(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the persisted project concurrency and any frozen Run value."""

    run = state.get("run") or {}
    return {
        "max_concurrency": int(state.get("config", {}).get("max_concurrency") or 3),
        "run_max_concurrency": (
            int(run["max_concurrency"])
            if run.get("running") and run.get("max_concurrency") is not None
            else None
        ),
        "running": bool(run.get("running")),
        "min_concurrency": 1,
    }


def translation_context_settings(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the request-local source-context settings for this project."""

    config = state.get("config", {})
    target_words = configured_target_words(config)
    default_words = default_context_words(target_words)
    previous_words, next_words = configured_context_words_for_state(state)
    return {
        "previous_context_words": previous_words,
        "next_context_words": next_words,
        "default_previous_context_words": default_words,
        "default_next_context_words": default_words,
        "max_context_words": MAX_CONTEXT_WORDS,
        "sentence_boundary_priority": True,
        "target_segment_words": target_words,
        "project_name": state.get("project", {}).get("name"),
    }


def update_translation_context_settings(
    cell: ProjectStateCell,
    previous_context_words: Any = _UNSET,
    next_context_words: Any = _UNSET,
) -> None:
    """Persist explicit context budgets without changing Units or translations."""

    project_state.ensure_open(cell)
    if isinstance(previous_context_words, dict) and next_context_words is _UNSET:
        payload = previous_context_words
        previous_context_words = payload.get("previous_context_words", _UNSET)
        next_context_words = payload.get("next_context_words", _UNSET)
    if previous_context_words is _UNSET and next_context_words is _UNSET:
        raise ValueError("必须提供 previous_context_words 或 next_context_words。")

    config = cell.state.setdefault("config", {})
    current_previous, current_next = configured_context_words_for_state(cell.state)
    previous_value = (
        current_previous
        if previous_context_words is _UNSET
        else validate_context_words(
            previous_context_words,
            field_name="previous_context_words",
        )
    )
    next_value = (
        current_next
        if next_context_words is _UNSET
        else validate_context_words(
            next_context_words,
            field_name="next_context_words",
        )
    )
    config["previous_context_words"] = previous_value
    config["next_context_words"] = next_value
    project_state.save_project(cell)


def update_segmentation_settings(
    cell: ProjectStateCell,
    max_words: Any = _UNSET,
    *,
    target_words: Any = _UNSET,
) -> None:
    project_state.ensure_open(cell)
    if isinstance(max_words, dict) and target_words is _UNSET:
        payload = max_words
        target_words = payload.get(
            "target_words",
            payload.get("target_segment_words", _UNSET),
        )
        max_words = payload.get("max_words", _UNSET)
    if target_words is _UNSET and max_words is _UNSET:
        raise ValueError("必须提供 target_words 或 max_words。")

    if target_words is not _UNSET and max_words is not _UNSET:
        value = validate_target_words(target_words)
        legacy_value = validate_target_words(max_words)
        if value != legacy_value:
            raise ValueError("target_words 与 max_words 必须一致。")
    elif target_words is not _UNSET:
        value = validate_target_words(target_words)
    else:
        value = validate_target_words(max_words)

    config = cell.state.setdefault("config", {})
    config["target_segment_words"] = value
    config.pop("max_segment_words", None)
    project_state.save_project(cell)
