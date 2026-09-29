"""Pydantic request schemas shared by the HTTP boundary.

This module owns validation shapes only.  It intentionally does not import
web.api, PipelineManager or app, so the route module can re-export the same


class objects without a cycle.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.quality_support import MAX_BATCH_CARD_ACTIONS

try:
    from core.segmenter import validate_target_words
except ImportError:  # Backward compatibility until the segmenter API is upgraded.
    from core.segmenter import validate_max_words as validate_target_words


def _segmentation_values_match(left: Any, right: Any) -> bool:
    """Compare legacy and canonical values after normalizing valid inputs."""
    try:
        return validate_target_words(left) == validate_target_words(right)
    except (TypeError, ValueError):
        # Let PipelineManager report invalid values; equal raw values are not a
        # target/max conflict even when both will subsequently be rejected.
        return left == right


OutputFormat = Literal["markdown", "text", "pdf", "epub", "docx"]


class ProjectRequest(BaseModel):
    source_text: str | None = None
    max_concurrency: int = Field(default=3, ge=1)
    source_language: str = "English"
    target_language: str = "简体中文"


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DecisionRequest(BaseModel):
    decision: Literal["edit", "accept-risk", "retry"]
    translation: str | None = None
    source_sha256: str | None = None


class ManualTranslationRequest(BaseModel):
    translation: str
    source_sha256: str = Field(min_length=1)
    expected_translation_revision: int = Field(ge=0)


class UnitActionRequest(BaseModel):
    source_sha256: str | None = Field(default=None, min_length=1)
    expected_translation_revision: int | None = Field(default=None, ge=0)


class RetranslateUnitRequest(UnitActionRequest):
    expected_project_id: str | None = Field(default=None, min_length=1)


class PipelineStartRequest(BaseModel):
    unit_ids: list[str] | None = None


class ConcurrencySettingsRequest(BaseModel):
    max_concurrency: int = Field(ge=1)


class ApiSettingsRequest(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    reasoning_effort: str = ""
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=2000, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=90, ge=1, le=600)


class ApiConnectionRequest(ApiSettingsRequest):
    """Editor values for a connection test or model list; ``preset_id`` lets
    the server apply that preset's saved OC Go contract."""

    preset_id: str | None = Field(default=None, max_length=64)


class ApiPresetRequest(ApiSettingsRequest):
    name: str = Field(min_length=1, max_length=40)


class ApiPresetChoiceRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)


class ApiTaskPresetRequest(BaseModel):
    """``preset_id`` null means the task follows its group again."""

    preset_id: str | None = Field(default=None, min_length=1, max_length=64)


class OcGoCompatibilityRequest(BaseModel):
    enabled: bool = True


class PromptPresetRequest(BaseModel):
    """Name + full system-prompt text of one editable prompt preset."""

    name: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=200_000)


class PromptSelectionRequest(BaseModel):
    """``"default"`` selects the read-only built-in prompt."""

    preset_id: str = Field(min_length=1, max_length=64)


class SegmentationSettingsRequest(BaseModel):
    target_words: Any | None = None
    max_words: Any | None = None

    @model_validator(mode="after")
    def validate_word_fields(self) -> "SegmentationSettingsRequest":
        if self.target_words is None and self.max_words is None:
            raise ValueError("必须提供 target_words 或 max_words。")
        if (
            self.target_words is not None
            and self.max_words is not None
            and not _segmentation_values_match(self.target_words, self.max_words)
        ):
            raise ValueError("target_words 与 max_words 必须相同。")
        return self

    def resolved_words(self) -> Any:
        """Return the canonical value sent to PipelineManager."""
        return self.target_words if self.target_words is not None else self.max_words


class TranslationContextSettingsRequest(BaseModel):
    previous_context_words: Any | None = None
    next_context_words: Any | None = None

    @model_validator(mode="after")
    def validate_context_fields(self) -> "TranslationContextSettingsRequest":
        if self.previous_context_words is None and self.next_context_words is None:
            raise ValueError("必须提供 previous_context_words 或 next_context_words。")
        return self

    def resolved(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.previous_context_words is not None:
            values["previous_context_words"] = self.previous_context_words
        if self.next_context_words is not None:
            values["next_context_words"] = self.next_context_words
        return values


class QualityScanPlanRequest(BaseModel):
    scope: str = "selected"
    unit_ids: list[str] | None = None
    current_unit_id: str | None = None
    expected_project_id: str | None = Field(default=None, min_length=1)
    # Only sanity floors remain: a scan has no batch-count, per-batch unit-count
    # or character ceiling any more, and the word target only decides where a
    # batch is split. An unknown "max_batches" field is ignored.
    max_parallel_batches: int = Field(default=1, ge=1)
    max_source_words: int | None = Field(default=None, ge=100)


class QualityScanBatchRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=120)
    unit_ids: list[str] = Field(min_length=1)
    # Bound the request to the project the frontend believes it is talking to;
    # a stale tab pointing at another project is rejected before any model call.
    expected_project_id: str | None = Field(default=None, min_length=1)


class QualityBatchRetryRequest(BaseModel):
    """One hand-confirmed recovery of a single failed concept batch.

    The body carries identity only. Which step (generation or check), which
    units, which candidates and which mode are read from the batch's own frozen
    record on the server, so a client can neither widen the scope nor choose a
    different stage than the one that failed.
    """

    expected_project_id: str | None = Field(default=None, min_length=1)
    expected_revision: int | None = Field(default=None, ge=0)
    # Opt in only for independently frozen, non-overlapping batch recoveries.
    # The ordinary single-batch endpoint remains exclusive by default.
    allow_parallel: bool = False


class QualityReferenceModeRequest(BaseModel):
    """Switch one project between manual and automatic reference mode."""

    reference_mode: Literal["manual", "automatic"]
    expected_project_id: str | None = Field(default=None, min_length=1)
    expected_revision: int | None = Field(default=None, ge=0)


class QualityPrepareRequest(BaseModel):
    """One phase of the automatic reference preparation.

    ``plan`` is the read-only preview: it freezes the scope and reports what
    would run, with no model call and no write. ``execute`` is the confirmed
    single action: it binds the previewed ``prepare_id``, runs the generation,
    check and group-judgment batches and commits the verified decisions.
    ``resolve`` and ``commit`` remain as the sub-steps of the same workflow
    (kept for compatibility). Every phase binds the project and the global
    revision, so a stale page cannot continue a newer run.
    """

    phase: Literal["plan", "execute", "resolve", "commit"]
    unit_ids: list[str] | None = None
    current_unit_id: str | None = None
    max_source_words: int | None = Field(default=None, ge=100)
    # How many generation+check batches the confirmed execution may run at the
    # same time. The preview freezes it into the plan, so what was previewed is
    # what executes; ``None`` keeps the historical one-at-a-time behaviour.
    max_parallel_batches: int | None = Field(default=None, ge=1)
    prepare_id: str | None = Field(default=None, max_length=120)
    expected_project_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    # One shared pool of extra logical requests (bounded lookup + large-group
    # local judgments). 0 still runs the base checks and the normal group
    # judgments; it only refuses the optional extra work.
    additional_work_limit: int | None = Field(default=None, ge=0, le=10)


class QualityCardRequest(BaseModel):
    action: Literal["edit", "approve", "defer", "reject"]
    content: dict[str, Any] | None = None
    expected_project_id: str | None = Field(default=None, min_length=1)
    expected_revision: int | None = Field(default=None, ge=0)
    expected_draft_revision: int | None = Field(default=None, ge=0)


class QualityCardBatchItem(BaseModel):
    card_id: str = Field(min_length=1, max_length=120)
    expected_draft_revision: int = Field(ge=0)


class QualityCardBatchRequest(BaseModel):
    """One atomic batch decision over explicitly selected pending cards."""

    action: Literal["approve", "defer", "reject"]
    items: list[QualityCardBatchItem] = Field(min_length=1, max_length=MAX_BATCH_CARD_ACTIONS)
    # This new entry point has no legacy caller, so the project and the global
    # revision bindings are both required.
    expected_project_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_unique_cards(self) -> "QualityCardBatchRequest":
        ids = [item.card_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("items 中不能包含重复的 card_id。")
        return self


class EditorialSuggestionRequest(BaseModel):
    expected_project_id: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(default=None, min_length=1)
    expected_translation_revision: int | None = Field(default=None, ge=0)


class ResegmentProjectRequest(BaseModel):
    confirm_reset: bool = False


class OutputRequest(BaseModel):
    format: OutputFormat | None = None
