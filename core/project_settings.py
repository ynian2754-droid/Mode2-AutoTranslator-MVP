"""Persisted project configuration compatibility rules."""

from __future__ import annotations

from typing import Any

from core.segmenter import DEFAULT_TARGET_WORDS, validate_target_words


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

