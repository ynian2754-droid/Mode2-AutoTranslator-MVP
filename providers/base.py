"""Small, dependency-free contracts between the scheduler and model adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .repair_loop import RepairControl


@dataclass(frozen=True)
class TranslationRequest:
    unit_id: str
    source_text: str
    source_sha256: str
    source_language: str
    target_language: str
    context: dict[str, Any] = field(default_factory=dict)
    # Optional, request-local execution hook (cancel/staleness check + progress
    # notifications).  Never serialized, never part of ``context``, and ``None``
    # for callers that do not need the bounded content-repair loop.
    control: "RepairControl | None" = None


@dataclass(frozen=True)
class TranslationResult:
    unit_id: str
    source_sha256: str
    translated_text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    # Minimal per-execution summary of the bounded repair loop, or None when the
    # provider did not run one.
    repair: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewRequest:
    unit_id: str
    source_text: str
    translated_text: str
    source_sha256: str
    source_language: str
    target_language: str
    context: dict[str, Any] = field(default_factory=dict)
    control: "RepairControl | None" = None


@dataclass(frozen=True)
class ReviewResult:
    unit_id: str
    source_sha256: str
    verdict: str
    issues: list[dict[str, Any]]
    metrics: dict[str, Any]
    provider: str
    model: str
    repair: dict[str, Any] | None = None


class TranslationProvider(Protocol):
    name: str
    model: str

    def translate(self, request: TranslationRequest) -> TranslationResult:
        """Translate one immutable unit and return its source binding."""


class ReviewProvider(Protocol):
    name: str
    model: str

    def review(self, request: ReviewRequest) -> ReviewResult:
        """Review one translation without receiving the translator's reasoning."""
