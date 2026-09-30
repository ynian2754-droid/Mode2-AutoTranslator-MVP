"""Stage-local provider routing, independent of project workflow and prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from core.api_settings import ApiConfig


class ConfigPort(Protocol):
    def config_for_task(self, task: str) -> ApiConfig: ...


@dataclass
class ProviderBindings:
    translation_provider: Any | None = None
    review_provider: Any | None = None
    quality_generation_provider: Any | None = None
    quality_check_provider: Any | None = None
    quality_editorial_provider: Any | None = None
    quality_resolution_provider: Any | None = None


@dataclass(frozen=True)
class UnitFactories:
    translation: Callable[..., Any]
    review: Callable[..., Any]
    demo_translation: Callable[..., Any]
    demo_review: Callable[..., Any]


@dataclass(frozen=True)
class QualityFactories:
    generation: Callable[..., Any]
    check: Callable[..., Any]
    editorial: Callable[..., Any]
    resolution: Callable[..., Any]
    fake: Callable[..., Any]


def resolve_unit_pair(
    provider_name: str,
    bindings: ProviderBindings,
    settings: ConfigPort,
    factories: UnitFactories,
) -> tuple[Any, Any]:
    if bindings.translation_provider is not None or bindings.review_provider is not None:
        if provider_name == "openai-compatible":
            default_translation = factories.translation(
                config=settings.config_for_task("unit_translation")
            )
            default_review = factories.review(
                config=settings.config_for_task("unit_review")
            )
        else:
            default_translation = factories.demo_translation()
            default_review = factories.demo_review()
        return (
            bindings.translation_provider or default_translation,
            bindings.review_provider or default_review,
        )
    if provider_name == "openai-compatible":
        return (
            factories.translation(config=settings.config_for_task("unit_translation")),
            factories.review(config=settings.config_for_task("unit_review")),
        )
    return factories.demo_translation(), factories.demo_review()


def resolve_quality_channels(
    provider_name: str,
    injected: tuple[Any, Any, Any, Any],
    settings: ConfigPort,
    factories: QualityFactories,
) -> tuple[Any, Any, Any, Any]:
    if provider_name != "openai-compatible" or any(p is not None for p in injected):
        # A partially injected set means an offline harness; the missing
        # channels fall back to the double instead of a real HTTP client.
        fake = factories.fake()
        return tuple(provider if provider is not None else fake for provider in injected)
    return (
        factories.generation(config=settings.config_for_task("concept_generation")),
        factories.check(config=settings.config_for_task("concept_check")),
        factories.editorial(config=settings.config_for_task("expression")),
        factories.resolution(config=settings.config_for_task("concept_disambiguation")),
    )

