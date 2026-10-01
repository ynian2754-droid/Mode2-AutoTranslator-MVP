"""Persistent concurrent translation pipeline for the Mode2 MVP.

The controller keeps the source binding and state transitions local. Providers
only receive one unit at a time, so translation and review can be replaced
independently without changing the web layer.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import mode2_common
from core.api_settings import ApiSettingsStore
from core.assembler import AssemblyError, DocumentAssembler
from core import concept_automation
from core.document_model import empty_document
from core.docx_exporter import DocxExportError, DocxExporter
from core.epub_exporter import EpubExportError, EpubExporter
from core.exceptions import ConflictError, PipelineError
from core.pipeline_output import OUTPUT_FORMATS
from core import execution
from core.execution import STOP_GRACE_SECONDS
from core.unit_state import (
    ACTIVE_STATUSES, WAITING_STATUSES, PROCESSING_STATUSES,
    TRANSLATION_PROCESSING_STATUSES, ACTION_STATUSES,
    EDITABLE_TRANSLATION_STATUSES, REVIEWABLE_TRANSLATION_STATUSES,
    CANCELLABLE_START_STATUSES,
)
from core.execution_runtime import ExecutionRuntime, InvocationTracker
from core.editorial_workflow import EditorialWorkflow
from core.unit_workflow import UnitWorkflow
from core import quality_prepare_plan, quality_prepare_record
from core.quality_limits import (
    DEFAULT_ADDITIONAL_WORK_LIMIT,
    MAX_RECHECK_CARDS,
    MAX_LOOKUP_UNITS_PER_EXPRESSION,
    MAX_CHECK_REQUEST_UNITS,
    MAX_CHECK_REQUEST_CHARS,
    MAX_LOOKUP_CARDS,
    resolve_parallel_batches,
    resolve_additional_work_limit,
)
from core.quality_batches import QualityBatchWorkflow
from core.quality_queries import QUALITY_SCAN_SCOPES, QualityQueries
from core.quality_cards import (
    QUALITY_ACTION_LABELS,
    QUALITY_BATCH_ACTIONS,
    QUALITY_CARD_ACTIONS,
    QualityCards,
)
from core import quality_recovery, quality_requests
from core.quality_prepare_state import PrepareState
from core.quality_recheck import PrepareRecheck
from core.quality_lookup import PrepareLookup
from core.quality_commit import PrepareCommit
from core.quality_resolution import PrepareResolution
from core.quality_progress import PrepareProgress
from core.quality_runtime import QualityRuntime
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import (
    DEFAULT_SCAN_SOURCE_WORDS,
    MAX_BATCH_CARD_ACTIONS,
    PROMPT_VERSION,
    QualitySupportError,
    content_signature,
    normalize_card_content,
    affected_units_for_cards,
    apply_card_decision,
    apply_check_result,
    batch_approval_problems,
    batch_retry_descriptor,
    batch_retry_state,
    build_reference_snapshot,
    normalize_batch_retry,
    normalize_quality_support,
    planned_batches,
    record_batch,
    refresh_check_result,
    scanned_unit_ids,
    select_reference_candidates,
    select_reference_cards,
    summarize_counts,
    terminology_mismatches,
    terminology_rules,
    upsert_candidate,
)
from core.importers import SourceImporter
from core.pdf_exporter import PdfExportError, PdfExporter
from core.pdf_fonts import PDF_MATH_FONT_PATH, load_pdf_fonts
from core.pdf_glyph_support import scan_text_glyphs, unavailable_scan
from core import pipeline_output, project_settings, project_state, provider_routing, unit_requests, unit_state, unit_validation
from core.project_factory import DEFAULT_SAMPLE_SOURCE, ProjectFactory, empty_output_state
from core.project_loading import ProjectLoader
from core.segmenter import DEFAULT_TARGET_WORDS, MarkdownSegmenter, validate_target_words
from core.storage import ProjectStore
from core.translation_context import (
    MAX_CONTEXT_WORDS,
    build_translation_context,
    configured_context_words,
    default_context_words,
    validate_context_words,
)
from core.utils import now_iso

from providers.api_provider import (
    OpenAICompatibleReviewProvider,
    OpenAICompatibleTranslationProvider,
)
from providers.base import (
    ReviewRequest,
    ReviewResult,
    TranslationRequest,
    TranslationResult,
)
from providers.demo_provider import DemoReviewProvider, DemoTranslationProvider
from providers.repair_loop import (
    ContentRepairExhausted,
    RepairControl,
    RepairProgress,
)
from providers.quality_provider import (
    ConceptCheckRequest,
    ConceptScanRequest,
    ConceptUnitRef,
    EditorialSuggestionRequest,
    FakeQualityProvider,
    OpenAICompatibleConceptCheckProvider,
    OpenAICompatibleConceptGenerationProvider,
    OpenAICompatibleConceptResolutionProvider,
    OpenAICompatibleEditorialSuggestionProvider,
    QualityProviderError,
    normalize_resolution,
)




_UNSET = project_settings._UNSET


def _unit_request_clock() -> str:
    return now_iso()


def _new_invocation_id() -> str:
    return uuid.uuid4().hex


def _unit_provider_factories() -> provider_routing.UnitFactories:
    return provider_routing.UnitFactories(
        OpenAICompatibleTranslationProvider,
        OpenAICompatibleReviewProvider,
        DemoTranslationProvider,
        DemoReviewProvider,
    )


def _quality_provider_factories() -> provider_routing.QualityFactories:
    return provider_routing.QualityFactories(
        OpenAICompatibleConceptGenerationProvider,
        OpenAICompatibleConceptCheckProvider,
        OpenAICompatibleEditorialSuggestionProvider,
        OpenAICompatibleConceptResolutionProvider,
        FakeQualityProvider,
    )


def _execution_executor(**kwargs: Any) -> Any:
    return ThreadPoolExecutor(**kwargs)


def _execution_event() -> Any:
    return threading.Event()


def _execution_timer(*args: Any, **kwargs: Any) -> Any:
    return threading.Timer(*args, **kwargs)


def _execution_stop_grace() -> float:
    return STOP_GRACE_SECONDS


def _output_export_resources() -> pipeline_output.OutputExportResources:
    return pipeline_output.OutputExportResources(
        AssemblyError,
        DocxExportError,
        DocxExporter,
        EpubExportError,
        PdfExportError,
        EpubExporter,
        PdfExporter,
    )


class PipelineManager:
    """Own one local project and serialize every imported state mutation."""

    def __init__(
        self,
        runtime_dir: Path | str | None = None,
        *,
        api_settings: ApiSettingsStore | None = None,
        api_settings_dir: Path | str | None = None,
        translation_provider: Any | None = None,
        review_provider: Any | None = None,
        quality_generation_provider: Any | None = None,
        quality_check_provider: Any | None = None,
        quality_editorial_provider: Any | None = None,
        quality_resolution_provider: Any | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir or Path(__file__).with_name(".runtime"))
        self.store = ProjectStore(self.runtime_dir)
        self.state_path = self.store.state_path
        self.segmenter = MarkdownSegmenter()
        self.project_factory = ProjectFactory(self.segmenter)
        self.source_importer = SourceImporter()
        self.assembler = DocumentAssembler(self.runtime_dir)
        self.api_settings = api_settings or ApiSettingsStore(api_settings_dir or self.runtime_dir)
        self.lock = threading.RLock()
        self._project_state = project_state.ProjectStateCell(
            state={}, lock=self.lock, store=self.store, closed=False
        )
        self._unit_requests = unit_requests.UnitRequests(
            self._project_state, self.api_settings, _unit_request_clock
        )
        self._quality_cards = QualityCards(self._project_state, clock=lambda: now_iso())
        self._provider_bindings = provider_routing.ProviderBindings(
            translation_provider=translation_provider,
            review_provider=review_provider,
            quality_generation_provider=quality_generation_provider,
            quality_check_provider=quality_check_provider,
            quality_editorial_provider=quality_editorial_provider,
            quality_resolution_provider=quality_resolution_provider,
        )
        self._quality_runtime = QualityRuntime()
        self._quality_progress = PrepareProgress(
            self._project_state, self._quality_runtime, clock=lambda: now_iso()
        )
        self._quality_queries = QualityQueries(self._project_state)
        self._prepare_state = PrepareState(
            self._project_state, self._quality_runtime, self._quality_progress,
            clock=lambda: now_iso(),
        )
        self._execution = ExecutionRuntime()
        self._invocations = InvocationTracker(
            self._project_state, self._execution, _new_invocation_id
        )
        self._provider_router = provider_routing.ProviderRouter(
            self._project_state, self._provider_bindings, self.api_settings,
            _unit_provider_factories, _quality_provider_factories,
        )
        self._prepare_recheck = PrepareRecheck(
            self._project_state, self._prepare_state, self._quality_progress,
            self._provider_router, clock=lambda: now_iso(),
        )
        self._prepare_lookup = PrepareLookup(
            self._project_state, self._prepare_state, self._quality_progress,
            self._provider_router, clock=lambda: now_iso(),
        )
        self._prepare_resolution = PrepareResolution(
            self._project_state, self._prepare_state, self._quality_progress,
            self._provider_router,
        )
        self._prepare_commit = PrepareCommit(
            self._project_state, self._prepare_state, self._quality_progress,
            clock=lambda: now_iso(),
        )
        self._quality_batches = QualityBatchWorkflow(
            self._project_state, self._quality_runtime, self._quality_progress,
            self._provider_router, clock=lambda: now_iso(),
        )
        self._editorial_workflow = EditorialWorkflow(
            self._project_state, self._provider_router
        )
        self._unit_workflow = UnitWorkflow(
            self._project_state, self._unit_requests, self._provider_router,
            self._invocations, _unit_request_clock,
        )
        self._scheduler = execution.ExecutionScheduler(
            self._project_state, self._execution, self._unit_workflow,
            _unit_request_clock,
            execution.ExecutionFactories(
                _execution_executor, _execution_event, _execution_timer,
                _new_invocation_id, _execution_stop_grace,
            ),
        )
        self._output = pipeline_output.OutputOwner(
            self._project_state, self.assembler, self.runtime_dir,
            _unit_request_clock, _output_export_resources,
        )
        self._project_loader = ProjectLoader(
            self._project_state, self.project_factory, self.source_importer,
            self.segmenter, self.runtime_dir, _unit_request_clock,
        )
        self._closed = False
        self.state = self._project_loader.load()

    @property
    def state(self) -> dict[str, Any]:
        return self._project_state.state

    @state.setter
    def state(self, value: dict[str, Any]) -> None:
        self._project_state.state = value

    @property
    def _closed(self) -> bool:
        return self._project_state.closed

    @_closed.setter
    def _closed(self, value: bool) -> None:
        self._project_state.closed = value

    @property
    def translation_provider(self) -> Any | None:
        return self._provider_bindings.translation_provider

    @translation_provider.setter
    def translation_provider(self, value: Any | None) -> None:
        self._provider_bindings.translation_provider = value

    @property
    def review_provider(self) -> Any | None:
        return self._provider_bindings.review_provider

    @review_provider.setter
    def review_provider(self, value: Any | None) -> None:
        self._provider_bindings.review_provider = value

    @property
    def quality_generation_provider(self) -> Any | None:
        return self._provider_bindings.quality_generation_provider

    @quality_generation_provider.setter
    def quality_generation_provider(self, value: Any | None) -> None:
        self._provider_bindings.quality_generation_provider = value

    @property
    def quality_check_provider(self) -> Any | None:
        return self._provider_bindings.quality_check_provider

    @quality_check_provider.setter
    def quality_check_provider(self, value: Any | None) -> None:
        self._provider_bindings.quality_check_provider = value

    @property
    def quality_editorial_provider(self) -> Any | None:
        return self._provider_bindings.quality_editorial_provider

    @quality_editorial_provider.setter
    def quality_editorial_provider(self, value: Any | None) -> None:
        self._provider_bindings.quality_editorial_provider = value

    @property
    def quality_resolution_provider(self) -> Any | None:
        return self._provider_bindings.quality_resolution_provider

    @quality_resolution_provider.setter
    def quality_resolution_provider(self, value: Any | None) -> None:
        self._provider_bindings.quality_resolution_provider = value






    #: The controller authors a review when an attempt fails technically
    #: (transport error, a payload the strict gate rejected). Such a record
    #: describes *that attempt*; it is never the review of a saved draft.
    _TECHNICAL_REVIEW_PROVIDER = "controller"





    def _save_locked(self) -> None:
        project_state.save_project(self._project_state)

    def _ensure_open_locked(self) -> None:
        project_state.ensure_open(self._project_state)

    def _event_locked(self, event_type: str, message: str, unit_id: str | None = None, **details: Any) -> None:
        project_state.append_event(self._project_state, event_type, message, unit_id, details, now_iso)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return unit_state.snapshot_with_unit_stats(self.state)

    def _glyph_precheck_locked(self) -> dict[str, Any]:
        """Read-only scan of the saved translations against the embedded PDF font.

        Used by the pre-export warning so the workbench can name the affected
        units before the user clicks export.  Cached by a cheap state signature
        (the UI polls the output status) and never raises: when ReportLab or the
        font is unavailable it degrades to an explicit ``available: False``
        result instead of pretending there is nothing to report.
        """

        return self._output.glyph_precheck_locked(
            pdf_math_font_path=PDF_MATH_FONT_PATH,
            load_fonts=load_pdf_fonts,
            scan_glyphs=scan_text_glyphs,
            unavailable_scan=unavailable_scan,
        )

    def output_status(self) -> dict[str, Any]:
        with self.lock:
            status = self._output.status_locked()
            status["glyph_precheck"] = self._glyph_precheck_locked()
            return status

    def generate_output(self, output_format: str | None = None) -> dict[str, Any]:
        with self.lock:
            self._ensure_open_locked()
            result = self._output.generate_locked(output_format)
            result["readiness"] = self.output_status()
            return result

    def output_file_path(self, output_format: str | None = None) -> Path:
        with self.lock:
            return self._output.file_path_locked(output_format)


    def segmentation_settings(self) -> dict[str, Any]:
        with self.lock:
            return project_settings.segmentation_settings(self.state)

    def concurrency_settings(self) -> dict[str, Any]:
        """Return the persisted project concurrency and any frozen Run value."""

        with self.lock:
            return project_settings.concurrency_settings(self.state)

    def update_concurrency_settings(self, max_concurrency: Any) -> dict[str, Any]:
        """Persist a scheduler size only while the project is idle.

        The idle executor is discarded so the next Run cannot silently reuse
        a pool created with an older value.
        """

        value = project_settings.resolve_concurrency(max_concurrency)

        with self.lock:
            self._ensure_open_locked()
            run = self.state.get("run") or {}
            if run.get("running") or self._execution.active_unit_ids:
                raise ConflictError("当前流水线正在运行，不能修改并发数。")
            current = int(self.state.get("config", {}).get("max_concurrency") or 3)
            if current != value:
                self._scheduler.close_executor_locked()
                self.state.setdefault("config", {})["max_concurrency"] = value
                self._event_locked("concurrency_updated", f"并发数已确认设置为 {value}。")
                self._save_locked()
            return self.concurrency_settings()

    def translation_context_settings(self) -> dict[str, Any]:
        """Return the request-local source-context settings for this project."""

        with self.lock:
            return project_settings.translation_context_settings(self.state)

    def update_translation_context_settings(
        self,
        previous_context_words: Any = _UNSET,
        next_context_words: Any = _UNSET,
    ) -> dict[str, Any]:
        """Persist explicit context budgets without changing Units or translations."""

        with self.lock:
            project_settings.update_translation_context_settings(
                self._project_state, previous_context_words, next_context_words,
            )
            return self.translation_context_settings()

    def update_segmentation_settings(
        self,
        max_words: Any = _UNSET,
        *,
        target_words: Any = _UNSET,
    ) -> dict[str, Any]:
        with self.lock:
            project_settings.update_segmentation_settings(
                self._project_state, max_words, target_words=target_words,
            )
            return self.segmentation_settings()


    def get_unit(self, unit_id: str) -> dict[str, Any]:
        with self.lock:
            unit = unit_state.find_unit(self.state, unit_id)
            return copy.deepcopy(unit)

    def create_project(
        self,
        source_text: str | None = None,
        *,
        demo_mode: bool = False,
        max_concurrency: int = 3,
        provider: str = "demo",
        source_language: str = "English",
        target_language: str = "简体中文",
        source_file: dict[str, Any] | None = None,
        target_segment_words: int | None = None,
        max_segment_words: int | None = None,
    ) -> dict[str, Any]:
        if int(max_concurrency) < 1:
            raise PipelineError("并发数必须为大于或等于 1 的整数。")
        with self.lock:
            self._ensure_open_locked()
            if self.state.get("run", {}).get("running"):
                raise ConflictError("当前流水线仍在运行，不能替换项目。")
            self._scheduler.close_executor_locked()
            segment_words = project_settings.resolve_segment_words(
                self.state,
                target_segment_words=target_segment_words,
                max_segment_words=max_segment_words,
            )
            self.state = self._project_loader.new_state(
                source_text if source_text is not None else DEFAULT_SAMPLE_SOURCE,
                demo_mode=demo_mode,
                max_concurrency=int(max_concurrency),
                provider=provider,
                source_language=source_language,
                target_language=target_language,
                source_file=source_file,
                target_segment_words=segment_words,
            )
            self._save_locked()
            return unit_state.snapshot_with_unit_stats(self.state)

    def import_source_file(
        self,
        filename: str,
        content: bytes,
        *,
        demo_mode: bool = False,
        max_concurrency: int = 3,
        provider: str = "demo",
        source_language: str = "English",
        target_language: str = "简体中文",
        target_segment_words: int | None = None,
        max_segment_words: int | None = None,
    ) -> dict[str, Any]:
        """Extract, segment, and open one uploaded source as the active project."""
        try:
            imported = self.source_importer.import_bytes(filename, content)
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc
        if int(max_concurrency) < 1:
            raise PipelineError("并发数必须为大于或等于 1 的整数。")
        with self.lock:
            self._ensure_open_locked()
            if self.state.get("run", {}).get("running"):
                raise ConflictError("当前流水线仍在运行，不能替换项目。")
            self._scheduler.close_executor_locked()
            segment_words = project_settings.resolve_segment_words(
                self.state,
                target_segment_words=target_segment_words,
                max_segment_words=max_segment_words,
            )
            new_state = self._project_loader.new_state(
                imported.text,
                demo_mode=demo_mode,
                max_concurrency=int(max_concurrency),
                provider=provider,
                source_language=source_language,
                target_language=target_language,
                source_file=imported.metadata(),
                pdf_reconstruction=imported.pdf_reconstruction,
                structure_blocks=imported.structure_blocks,
                group_adjacent_paragraphs=imported.format in {"text", "markdown"},
                target_segment_words=segment_words,
            )
            try:
                stored_path = self.store.save_source_file(
                    new_state["project"]["id"],
                    imported.original_name,
                    content,
                )
            except (OSError, ValueError) as exc:
                raise PipelineError(f"源文件保存失败：{exc}") from exc
            new_state["project"]["source_file"]["stored_path"] = stored_path
            self.state = new_state
            self._event_locked(
                "source_imported",
                f"已导入 {imported.original_name}，切分为 {len(new_state['units'])} 个翻译单元。",
                format=imported.format,
                size_bytes=imported.size_bytes,
            )
            self._save_locked()
            return unit_state.snapshot_with_unit_stats(self.state)

    def resegment_source(self, *, confirm_reset: bool = False) -> dict[str, Any]:
        """Explicitly rebuild Units from the stored source after writing a backup.

        Re-segmentation changes immutable unit IDs and invalidates every prior
        translation/review binding, so it never happens during project load or
        setting updates.  The caller must opt in with ``confirm_reset=True``.
        """

        if not confirm_reset:
            raise PipelineError("重新切分会清除当前译文和校验结果；请明确确认后再继续。")
        with self.lock:
            self._ensure_open_locked()
            if self.state.get("run", {}).get("running"):
                raise ConflictError("当前流水线仍在运行，不能重新切分。")
            project = self.state.get("project") if isinstance(self.state.get("project"), dict) else {}
            config = self.state.get("config") if isinstance(self.state.get("config"), dict) else {}
            source_file = project.get("source_file") if isinstance(project, dict) else None
            if not isinstance(source_file, dict):
                raise PipelineError("当前项目没有已保存的源文件，无法重新切分。")
            stored_path = str(source_file.get("stored_path") or "")
            if not stored_path:
                raise PipelineError("当前项目没有可读取的源文件副本，无法重新切分。")
            runtime_root = self.runtime_dir.resolve()
            source_path = (runtime_root / Path(stored_path)).resolve()
            if runtime_root not in source_path.parents or not source_path.is_file():
                raise PipelineError("项目源文件不存在或路径无效，无法重新切分。")
            try:
                imported = self.source_importer.import_bytes(
                    str(source_file.get("name") or source_path.name),
                    source_path.read_bytes(),
                )
            except (OSError, ValueError) as exc:
                raise PipelineError(f"源文件重新导入失败：{exc}") from exc

            target_words = project_settings.configured_target_words(config)
            old_state = copy.deepcopy(self.state)
            self._scheduler.close_executor_locked()
            try:
                backup_path = self.store.backup_state(old_state, reason="before_resegment")
            except OSError as exc:
                raise PipelineError(f"重新切分前备份失败：{exc}") from exc

            try:
                source_metadata = imported.metadata()
                source_metadata["stored_path"] = stored_path
                replacement = self._project_loader.new_state(
                    imported.text,
                    demo_mode=bool(project.get("demo_mode")),
                    max_concurrency=int(config.get("max_concurrency") or 3),
                    provider=str(config.get("provider") or "demo"),
                    source_language=str(config.get("source_language") or "English"),
                    target_language=str(config.get("target_language") or "简体中文"),
                    source_file=source_metadata,
                    pdf_reconstruction=imported.pdf_reconstruction,
                    structure_blocks=imported.structure_blocks,
                    group_adjacent_paragraphs=imported.format in {"text", "markdown"},
                    target_segment_words=target_words,
                )
            except (PipelineError, ValueError) as exc:
                raise PipelineError(f"重新切分失败：{exc}") from exc

            replacement_project = replacement["project"]
            replacement_project["id"] = str(project.get("id") or replacement_project["id"])
            replacement_project["name"] = project.get("name")
            replacement_project["created_at"] = project.get("created_at") or replacement_project["created_at"]
            replacement_config = copy.deepcopy(config)
            replacement_config["target_segment_words"] = target_words
            replacement_config.pop("max_segment_words", None)
            replacement["config"] = replacement_config
            old_events = old_state.get("events") if isinstance(old_state.get("events"), list) else []
            replacement["events"] = copy.deepcopy(old_events[-159:])
            self.state = replacement
            self._event_locked(
                "source_resegmented",
                f"已按目标 {target_words} 词重新切分为 {len(replacement['units'])} 个翻译单元。",
                backup_path=backup_path,
                previous_unit_count=len(old_state.get("units") or []),
                target_words=target_words,
                format=imported.format,
            )
            self._save_locked()
            result = unit_state.snapshot_with_unit_stats(self.state)
            result["resegmentation"] = {
                "backup_path": backup_path,
                "previous_unit_count": len(old_state.get("units") or []),
                "unit_count": len(result.get("units") or []),
                "target_words": target_words,
            }
            return result

    def has_live_work_locked(self) -> bool:
        """Read deletion eligibility while the caller holds the project lock."""
        run = self.state.get("run") or {}
        return bool(
            run.get("running")
            or run.get("status") == "stopping"
            or self._execution.has_live_tasks()
        )

    def close(self) -> None:
        """Release the project scheduler after the session has become idle."""
        with self.lock:
            if self._closed:
                return
            self._scheduler.close_executor_locked()
            self._closed = True






    def start(self, unit_ids: list[str] | None = None) -> dict[str, Any]:
        return self._scheduler.start(unit_ids)

    def stop(self) -> dict[str, Any]:
        """Request a cooperative stop for the current run.

        Provider calls already in flight are allowed to return, but their
        results are discarded and no following pipeline stage is started.
        """
        return self._scheduler.stop()

    def _request_for_unit_locked(self, unit: dict[str, Any]) -> tuple[TranslationRequest, dict[str, Any] | None]:
        return self._unit_requests.translation_locked(unit)

    def _review_request_for_unit_locked(
        self,
        unit: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> ReviewRequest:
        return self._unit_requests.review_locked(unit, snapshot)


    # --- bounded model-repair invocation bookkeeping -----------------------
    #
    # The provider owns the message history; the controller only publishes the
    # per-execution summary and guards against stale notifications.  Nothing
    # here is shared between units, projects, translation and review.

    MODEL_REPAIR_KINDS = ("translation", "review")












    def save_translation(
        self,
        unit_id: str,
        translation: str,
        *,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist one user-edited translation without invoking either provider."""

        if not isinstance(translation, str) or not translation.strip():
            raise PipelineError("人工译文不能为空。")
        with self.lock:
            self._ensure_open_locked()
            if unit_id in self._execution.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许编辑正式译文。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            pipeline_output.invalidate_output(self.state)
            clean_translation = translation.strip()
            unit["translation"] = clean_translation
            unit["user_edited_translation"] = clean_translation
            unit["translation_revision"] += 1
            # Saving is deliberately independent from review.  Keep the last
            # review payload for traceability, but its revision now makes it
            # clear that it is not a review of this newly saved version.
            unit["pending_translation_feedback"] = None
            unit["user_decision"] = "edit"
            if unit.get("status") == "passed":
                unit["status"] = "user_modified"
                unit["last_error"] = None
            elif unit.get("status") == "user_modified":
                unit["last_error"] = None
            elif unit.get("status") == "accepted_risk":
                # Keep the explicit user risk-acceptance state visible after
                # a later manual edit.  The revision on the retained review
                # payload makes the old review inapplicable to this text;
                # retranslation is the supported path to obtain a new review.
                unit["last_error"] = None
            unit["updated_at"] = now_iso()
            self._event_locked("user_translation_saved", "人工译文已保存，等待用户明确复检。", unit_id)
            unit_state.recompute_unit_stats(self.state)
            self._save_locked()
            return copy.deepcopy(unit)

    def review_unit(
        self,
        unit_id: str,
        *,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit review of the persisted formal translation."""

        with self.lock:
            self._ensure_open_locked()
            if unit_id in self._execution.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in REVIEWABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新校验。")
            if not isinstance(unit.get("translation"), str) or not unit["translation"].strip():
                raise PipelineError("没有可供校验的正式译文。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            self._scheduler.start_job_locked([unit_id], "review")
            return copy.deepcopy(unit_state.find_unit(self.state, unit_id))

    def retranslate_unit(
        self,
        unit_id: str,
        *,
        expected_project_id: str | None = None,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit translation retry using only persisted feedback."""

        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            if unit_id in self._execution.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新翻译。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            current_revision = unit["translation_revision"]
            # The draft this retry replaces, kept with the revision it belongs to
            # and the review that judged it. Retrying again after a failure
            # carries the *same* group forward: the unit's own `translation` /
            # `review` fields describe the attempt (which the queue renumbers
            # before the model runs), so they must never overwrite it.
            # One-shot: a successful commit drops the whole record; network
            # errors, cancellation, a late result and a failed save all keep it.
            retained = unit_state.retained_previous_draft(unit)
            unit["pending_translation_feedback"] = {
                "source_revision": retained["revision"] if retained else current_revision,
                "suggestions": retained["suggestions"] if retained else [],
                "previous_translation": retained["translation"] if retained else None,
                "previous_review": retained["review"] if retained else None,
            }
            unit["status"] = "pending"
            unit["translation"] = ""
            unit["review"] = None
            unit["review_issues"] = []
            unit["review_suggestions"] = []
            unit["user_decision"] = "retry"
            unit["last_error"] = None
            unit["updated_at"] = now_iso()
            self._event_locked("user_requested_retry", "用户要求重新翻译并复检。", unit_id)
            self._scheduler.start_job_locked([unit_id], "translation")
            return copy.deepcopy(unit_state.find_unit(self.state, unit_id))

    # ------------------------------------------------------------------
    # Quality support: concept cards, bounded scans, editorial suggestions.
    # ------------------------------------------------------------------


    def _validate_expected_project_id_locked(
        self,
        expected_project_id: str | None,
    ) -> None:
        """Reject a stale page binding while holding the manager lock."""
        project_state.validate_expected_project_id(self._project_state, expected_project_id)


    def stale_reference_units_locked(
        self,
        support: Mapping[str, Any],
        unit_sources: Mapping[str, tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Translated units whose frozen automatic reference is no longer live.

        Read-only reporting: the unit keeps the snapshot it was translated
        with and the translation is never rewritten. The list only says "this
        finished unit used an automatic reference that would not be injected
        today", so a human can decide what to do about it.
        """
        return self._quality_queries.stale_reference_units_locked(support, unit_sources)

    def quality_support(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only view of cards, versions and scan coverage."""
        return self._quality_queries.support(expected_project_id=expected_project_id)


    def plan_quality_scan(
        self,
        *,
        scope: str = "selected",
        unit_ids: list[str] | None = None,
        current_unit_id: str | None = None,
        max_parallel_batches: int | None = None,
        max_source_words: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Describe the work of one scan without calling any model.

        Only sanity floors are enforced: the scan itself has no batch-count,
        per-batch unit-count or character ceiling, and the word target merely
        decides where a batch is split.
        """
        return self._quality_queries.plan_scan(
            scope=scope, unit_ids=unit_ids, current_unit_id=current_unit_id,
            max_parallel_batches=max_parallel_batches, max_source_words=max_source_words,
            expected_project_id=expected_project_id,
        )



    def _quality_commit_locked(
        self,
        support: dict[str, Any],
        *,
        old_support: Any,
        old_events: list[Any],
    ) -> None:
        """Swap in the mutated support and persist it atomically.

        If ``ProjectStore.save`` fails, the in-memory state and the event log
        are rolled back to the last committed version so memory, disk, the
        effective reference version and every later read stay consistent.
        """

        commit_quality_support(
            self._project_state, support, old_support=old_support, old_events=old_events
        )

    def scan_quality_batch(
        self,
        *,
        batch_id: str,
        unit_ids: list[str],
        expected_project_id: str | None = None,
        mode: str = "manual",
        retry_close: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one bounded generate+check batch and store only candidates.

        Model calls happen outside the project lock. Nothing is written until
        the source bindings are re-validated after the calls return. A batch
        that was already saved with a failed independent check can be retried
        with the same batch_id; the retry re-runs only the check, never the
        paid generation.

        ``retry_close`` is set only by ``retry_quality_batch``: it makes the
        hand-retry record and its prepare sync part of this batch's own commit,
        so a stored result is never visible without the recovery state that
        describes it. A scan may not start while another batch is being
        recovered by hand; the recovery's own batch is allowed here because
        generation recovery runs through this very path.
        """
        return self._quality_batches.scan_quality_batch(
            batch_id=batch_id,
            unit_ids=unit_ids,
            expected_project_id=expected_project_id,
            mode=mode,
            retry_close=retry_close,
        )

    def retry_quality_batch(
        self,
        batch_id: str,
        *,
        expected_project_id: str | None = None,
        expected_revision: int | None = None,
        allow_parallel: bool = False,
    ) -> dict[str, Any]:
        """Resume one failed batch after the operator explicitly confirmed it.

        The request carries identity only: which units, which step, which
        candidates and which mode all come from the batch's own frozen record, so
        a client can never widen the scope or pick a different stage. Model calls
        happen outside the project lock, and nothing is reported as successful
        before the result is saved.
        """
        return self._quality_batches.retry_quality_batch(
            batch_id,
            expected_project_id=expected_project_id,
            expected_revision=expected_revision,
            allow_parallel=allow_parallel,
        )


    def update_quality_card(
        self,
        card_id: str,
        action: str,
        *,
        content: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        expected_draft_revision: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit, approve, defer or reject one card under the project lock."""
        return self._quality_cards.update_quality_card(
            card_id, action, content=content, expected_revision=expected_revision,
            expected_draft_revision=expected_draft_revision,
            expected_project_id=expected_project_id,
        )

    def batch_quality_card_action(
        self,
        action: str,
        items: Sequence[Mapping[str, Any]],
        *,
        expected_revision: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Approve, defer or reject several pending cards in one atomic write.

        Every card is validated first and all decisions are applied to a copy of
        the concept data; only then is that copy committed with a single save.
        A stale project or global revision, a card that is not batch-approvable,
        a stale card draft revision or a failed save therefore leaves the stored
        state exactly as it was — there is no per-card write to compensate and no
        partially applied group.  Local unsaved browser edits are not visible
        here; the page must not send such cards.
        """

        return self._quality_cards.batch_quality_card_action(
            action, items, expected_revision=expected_revision,
            expected_project_id=expected_project_id,
        )

    def quality_affected_units(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """List units whose current source matches approved cards."""
        return self._quality_queries.affected_units(expected_project_id=expected_project_id)

    # ------------------------------------------------------------------
    # automatic reference preparation (V1)
    # ------------------------------------------------------------------


    def set_reference_mode(
        self,
        mode: str,
        *,
        expected_project_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Switch the project between manual and automatic reference mode.

        Switching back to manual retires every stored automatic decision (they
        stay in storage but stop applying to new requests); already frozen
        snapshots are per unit and are never rewritten. Enabling automatic mode
        does not run anything by itself.
        """

        return self._quality_cards.set_reference_mode(
            mode, expected_project_id=expected_project_id,
            expected_revision=expected_revision,
        )

    def quality_prepare(
        self,
        *,
        phase: str,
        plan: Mapping[str, Any] | None = None,
        unit_ids: list[str] | None = None,
        current_unit_id: str | None = None,
        max_source_words: int | None = None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None = None,
        expected_revision: int | None = None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Run one phase of the automatic reference preparation.

        ``plan``     is **read-only**: it freezes the scope into a preview and
                     makes **zero model calls** and zero writes;
        ``execute``  is the confirmed step: it freezes the single active prepare,
                     runs the bounded generation + check batches, judges the
                     related groups, then validates and stores the decisions in
                     one commit (model calls: yes);
        ``resolve`` / ``commit`` stay available for the frozen prepare as
                     separate steps (a page that already holds a plan id may
                     drive them one by one).

        Every phase binds the project, the global revision and the frozen
        ``prepare_id``; a stale binding is a 409 and never triggers a model call.
        """

        phase = str(phase or "").strip().casefold()
        plan_id = str((plan or {}).get("prepare_id") or "")
        work_limit = resolve_additional_work_limit(additional_work_limit)
        parallel_batches = resolve_parallel_batches(max_parallel_batches)
        if phase == "plan":
            return self._quality_prepare_plan(
                unit_ids=unit_ids,
                current_unit_id=current_unit_id,
                max_source_words=max_source_words,
                max_parallel_batches=parallel_batches,
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
                additional_work_limit=work_limit,
            )
        if phase == "execute":
            return self._quality_prepare_execute(
                plan={"prepare_id": plan_id} if plan_id else {},
                unit_ids=unit_ids,
                current_unit_id=current_unit_id,
                max_source_words=max_source_words,
                max_parallel_batches=parallel_batches,
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
                additional_work_limit=work_limit,
            )
        if phase == "resolve":
            return self._quality_prepare_resolve(
                plan={"prepare_id": plan_id} if plan_id else {},
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
            )
        if phase == "commit":
            return self._prepare_commit.commit(
                plan={"prepare_id": plan_id} if plan_id else {},
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
            )
        raise PipelineError("prepare 的 phase 只能是 plan、execute、resolve 或 commit。")

    # ------------------------------------------------------------------
    # R1: read-only preview
    # ------------------------------------------------------------------


    def _quality_prepare_plan(
        self,
        *,
        unit_ids: list[str] | None,
        current_unit_id: str | None,
        max_source_words: int | None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None,
        expected_revision: int | None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Describe the preparation without any model call or write.

        The preview is deliberately honest about what is *not* known yet: it
        reports how many cards exist, how many are currently eligible, and how
        many groups would need a judgment. The generation/check results — and
        therefore the real candidate counts — only exist after ``execute``.
        """

        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            if concept_automation.reference_mode(self.state.get("project")) != concept_automation.AUTOMATIC_MODE:
                raise PipelineError("当前项目是人工参考模式，请先切换为自动模式。")
            support = normalize_quality_support(self.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            automation = concept_automation.normalize_automation(support.get("automation"))
            running = automation.get("prepare")
            if isinstance(running, Mapping) and str(running.get("status") or "") == "running":
                raise ConflictError("已有一个准备任务在进行中，请先完成或等待它结束。")
            if self._quality_runtime.prepare_inflight:
                raise ConflictError("已有一个准备任务正在进行中，请等待它结束。")
            if self._quality_runtime.retry_inflight:
                raise ConflictError(
                    f"批次 {sorted(self._quality_runtime.retry_inflight)[0]} 正在恢复中，请等待结束再准备。"
                )
            targets = self._quality_queries.scan_units_locked(
                scope="selected" if unit_ids else ("current" if current_unit_id else "continue"),
                unit_ids=[str(item) for item in (unit_ids or [])],
                current_unit_id=current_unit_id,
            )
            if not targets:
                raise PipelineError("没有可准备的单元。")
            effective_source_words = (
                DEFAULT_SCAN_SOURCE_WORDS if max_source_words is None else int(max_source_words)
            )
            if effective_source_words < 100:
                raise PipelineError("每批源文词数至少为 100。")
            unit_states = quality_prepare_plan.prepare_unit_reuse(automation, targets)
            work_units = [unit for unit in targets if unit_states[str(unit.get("id"))]["state"] != "reused"]
            plan_batches = (
                planned_batches(
                    work_units,
                    max_source_words=effective_source_words,
                    word_counter=mode2_common.english_word_count,
                )
                if work_units
                else {"batches": [], "batch_count": 0}
            )
            unit_sources = quality_unit_sources(self.state.get("units") or [])
            in_scope = [str(unit.get("id")) for unit in targets]
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)
            group_reuse = quality_prepare_plan.prepare_group_reuse(
                support, automation, unit_sources, unit_ids=in_scope
            )
            # Read-only eligibility preview over the cards that already exist.
            # Candidates created by the confirmed execution are judged then.
            existing_eligible = 0
            for card in (support.get("cards") or {}).values():
                if not isinstance(card, dict):
                    continue
                verdict, _reason = concept_automation.adoption_eligibility(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if verdict == "adopt":
                    existing_eligible += 1
            prepare_id = f"prepare-{uuid.uuid4().hex[:12]}"
            dry_run_required = not bool((support.get("cards") or {}))
            reused_unit_ids = [unit_id for unit_id, state in unit_states.items() if state["state"] == "reused"]
            reused_group_ids = [group_id for group_id, row in group_reuse.items() if row["reused"]]
            local_units_pending = sum(
                int(row.get("local_units_pending") or 0) for row in group_reuse.values()
            )
            local_units_reused = sum(
                int(row.get("local_units_reused") or 0) for row in group_reuse.values()
            )
            pending_group_ids = [
                str(group["group_id"])
                for group in groups
                if str(group["group_id"]) not in reused_group_ids
                and (
                    (len(group["card_ids"]) >= 2 and not group["oversized"])
                    # An oversized group with units still owed is pending work
                    # just like a group that needs a fresh judgment.
                    or int(group_reuse.get(str(group["group_id"]), {}).get("local_units_pending") or 0) > 0
                )
            ]
            budget_limit = int(
                additional_work_limit
                if additional_work_limit is not None
                else DEFAULT_ADDITIONAL_WORK_LIMIT
            )
            return {
                "status": "ok",
                "phase": "plan",
                "prepare_id": prepare_id,
                "prepare_status": "planned",
                "plan": {
                    "prepare_id": prepare_id,
                    "scope": in_scope,
                    "scope_fingerprint": concept_automation.prepare_fingerprint(targets),
                    "batches": [
                        {"batch_id": f"auto-{uuid.uuid4().hex[:10]}", "unit_ids": batch["unit_ids"]}
                        for batch in plan_batches["batches"]
                    ],
                    "groups": [
                        {
                            "group_id": group["group_id"],
                            "card_ids": group["card_ids"],
                            "oversized": group["oversized"],
                        }
                        for group in groups
                    ],
                    "resolved_groups": {},
                    "reused_units": reused_unit_ids,
                    "unit_states": copy.deepcopy(unit_states),
                    "baseline_revision": int(support.get("revision") or 0),
                    "baseline_approved_version": int(support.get("approved_version") or 0),
                    "max_source_words": effective_source_words,
                    "max_parallel_batches": resolve_parallel_batches(max_parallel_batches),
                    "additional_work_limit": additional_work_limit,
                },
                "preview": {
                    "unit_count": len(targets),
                    "batch_count": int(plan_batches["batch_count"]),
                    "existing_card_count": len(support.get("cards") or {}),
                    "existing_eligible_count": existing_eligible,
                    "group_count": len(groups),
                    "oversized_groups": [g["group_id"] for g in groups if g["oversized"]],
                    # The confirmed execution runs one generation + one check per
                    # frozen batch of *unfinished* units, plus one judgment per
                    # related group whose input is not reusable.
                    "expected_generation_calls": int(plan_batches["batch_count"]),
                    "expected_check_calls": int(plan_batches["batch_count"]),
                    "expected_group_calls_max": len(
                        [group_id for group_id in pending_group_ids if group_id not in {
                            str(group["group_id"]) for group in groups if group["oversized"]
                        }]
                    ),
                    # The oversized groups' remaining units are paid from the
                    # shared pool, so their number of calls this execution is
                    # bounded by that limit — never by the whole group.
                    "expected_local_calls_max": min(budget_limit, local_units_pending),
                    "runs_generation": bool(plan_batches["batch_count"]),
                    # One pool for the whole execution: bounded lookups and
                    # large-group local judgments are not separate budgets.
                    "budget": {
                        "limit": budget_limit,
                        "used": 0,
                        "by_kind": {},
                        "note": "额外逻辑请求总数（含补查与大组局部分辨），不是各阶段各一份。",
                    },
                    # What is already valid and what still needs work, with the
                    # reason, so the page can show the incremental scope instead
                    # of only a total.
                    "reuse": {
                        "reused_unit_count": len(reused_unit_ids),
                        "work_unit_count": len(work_units),
                        "reused_units": [
                            {"unit_id": unit_id, "reason": unit_states[unit_id]["reason"]}
                            for unit_id in sorted(reused_unit_ids)
                        ],
                        "work_units": [
                            {"unit_id": str(unit.get("id")), "reason": unit_states[str(unit.get("id"))]["reason"]}
                            for unit in targets
                            if unit_states[str(unit.get("id"))]["state"] != "reused"
                        ],
                        "reused_group_count": len(reused_group_ids),
                        "pending_group_count": len(pending_group_ids),
                        # Local per-unit work of the oversized groups: what an
                        # earlier confirmation already answered and what is still
                        # owed. Both are shown, so "unfinished" can never look
                        # like "done" on the page.
                        "local_units_reused": local_units_reused,
                        "local_units_pending": local_units_pending,
                        "reused_groups": [
                            {"group_id": group_id, "reason": group_reuse[group_id]["reason"]}
                            for group_id in sorted(reused_group_ids)
                        ],
                        "pending_groups": [
                            {"group_id": group_id, "reason": group_reuse.get(group_id, {}).get("reason") or ""}
                            for group_id in pending_group_ids
                        ],
                    },
                },
                "summary": None,
                "model_calls": 0,
                "note": (
                    "预览不调用模型、不写入任何决定；已完成的单元和仍有效的组辨析会被复用，"
                    "预算内未完成的局部辨析会在再次确认后继续，确认后只处理未完成或已失效的部分。"
                    "准备结束不会自动开始翻译。"
                ),
            }

    # ------------------------------------------------------------------
    # R6: the single active prepare + lifecycle guard
    # ------------------------------------------------------------------







    # ------------------------------------------------------------------
    # R1 + R5: the confirmed execution
    # ------------------------------------------------------------------

    def _quality_prepare_execute(
        self,
        *,
        plan: Mapping[str, Any],
        unit_ids: list[str] | None,
        current_unit_id: str | None,
        max_source_words: int | None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None,
        expected_revision: int | None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Freeze the single active prepare and run the whole confirmed chain."""

        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            if concept_automation.reference_mode(self.state.get("project")) != concept_automation.AUTOMATIC_MODE:
                raise PipelineError("当前项目是人工参考模式，请先切换为自动模式。")
            support = normalize_quality_support(self.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            automation = concept_automation.normalize_automation(support.get("automation"))
            running = automation.get("prepare")
            if isinstance(running, Mapping) and str(running.get("status") or "") == "running":
                raise ConflictError("已有一个准备任务在进行中，请先完成或等待它结束。")
            requested_id = str(plan.get("prepare_id") or "")
            if requested_id and isinstance(running, Mapping) and requested_id == str(running.get("prepare_id") or ""):
                raise ConflictError("这个准备计划已经执行过，请重新预览后再确认。")
            if self._quality_runtime.prepare_inflight and requested_id not in self._quality_runtime.prepare_inflight:
                # A concurrent execution must not start a second run: only the
                # already-frozen prepare may continue.
                raise ConflictError("已有一个准备任务正在进行中，请等待它结束。")
            if self._quality_runtime.retry_inflight:
                raise ConflictError(
                    f"批次 {sorted(self._quality_runtime.retry_inflight)[0]} 正在恢复中，请等待结束再准备。"
                )
            targets = self._quality_queries.scan_units_locked(
                scope="selected" if unit_ids else ("current" if current_unit_id else "continue"),
                unit_ids=[str(item) for item in (unit_ids or [])],
                current_unit_id=current_unit_id,
            )
            if not targets:
                raise PipelineError("没有可准备的单元。")
            effective_source_words = (
                DEFAULT_SCAN_SOURCE_WORDS if max_source_words is None else int(max_source_words)
            )
            if effective_source_words < 100:
                raise PipelineError("每批源文词数至少为 100。")
            unit_sources_live = quality_unit_sources(self.state.get("units") or [])
            # Incremental scope: units whose previous generation + check finished
            # for the same source hash are not scanned again, and the judgments
            # whose frozen input still matches are handed over by the freeze step
            # below (the prior record is captured before it is replaced).
            unit_states = quality_prepare_plan.prepare_unit_reuse(automation, targets)
            work_units = [unit for unit in targets if unit_states[str(unit.get("id"))]["state"] != "reused"]
            plan_batches = (
                planned_batches(
                    work_units,
                    max_source_words=effective_source_words,
                    word_counter=mode2_common.english_word_count,
                )
                if work_units
                else {"batches": [], "batch_count": 0}
            )
            prior_judgments = concept_automation.prior_group_judgments(automation.get("prepare"))
            prior_record = automation.get("prepare")
            # Executed lookups are handed over with the run: the confirmed
            # execution that follows must not pay again for a question this
            # project already asked with exactly the same content and evidence.
            prior_lookups = list((prior_record or {}).get("lookups") or [])
            prior_lookup_state = concept_automation.lookup_state_of(
                (prior_record or {}).get("lookup_state")
            )
            prepare_id = requested_id or f"prepare-{uuid.uuid4().hex[:12]}"
            prepared = quality_prepare_plan.prepare_plan_payload(
                {
                    "prepare_id": prepare_id,
                    "scope": [str(unit.get("id")) for unit in targets],
                    "scope_fingerprint": concept_automation.prepare_fingerprint(targets),
                    "batches": [
                        {"batch_id": f"auto-{uuid.uuid4().hex[:10]}", "unit_ids": batch["unit_ids"]}
                        for batch in plan_batches["batches"]
                    ],
                    "groups": [],
                    "reused_units": [
                        unit_id for unit_id, state in unit_states.items() if state["state"] == "reused"
                    ],
                    "unit_states": copy.deepcopy(unit_states),
                    "baseline_revision": int(support.get("revision") or 0),
                    "baseline_approved_version": int(support.get("approved_version") or 0),
                    "max_source_words": effective_source_words,
                    # Frozen like the batch size: the confirmed run executes with
                    # the parallelism its preview showed, not with a later edit.
                    "max_parallel_batches": resolve_parallel_batches(max_parallel_batches),
                    "additional_work_limit": additional_work_limit,
                }
            )
            automation["prepare"] = quality_prepare_record.new_prepare_record(
                prepared,
                clock=lambda: now_iso(),
                unit_states=unit_states,
                unit_sources=unit_sources_live,
                lookups=prior_lookups,
                lookup_state=prior_lookup_state,
            )
            support["automation"] = automation
            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            try:
                self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            except Exception:
                self._prepare_state.finish(prepare_id)
                raise
            # Bind the guard only after the frozen record is in the live state,
            # so the identity later steps see is the one registered here.
            self._prepare_state.begin_locked(prepare_id)
            self._quality_progress.begin_locked(
                prepare_id,
                prepared,
                automation.get("prepare") if isinstance(automation.get("prepare"), Mapping) else {},
            )

        try:
            batch_failures = self._prepare_run_batches(prepare_id, prepared, prior_judgments)
            if batch_failures and len(batch_failures) >= len(prepared["batches"]):
                # Every batch failed: this is a failed preparation, not a
                # zero-candidate success. The record already carries the real
                # per-unit failures; the caller gets the first error to show.
                # A run with nothing left to do has no batches and cannot fail
                # this way — reusing everything is a valid result.
                raise PipelineError(str(batch_failures[0]))
            self._prepare_resolution.resolve_pending(prepare_id)
            return self._prepare_commit.commit(
                plan={"prepare_id": prepare_id},
                expected_project_id=expected_project_id,
                expected_revision=None,  # the guard checked the live revision above
                internal=True,
            )
        except Exception as exc:
            # A failed execution stays visible for what it really was; the
            # frozen record is not rewritten into "complete" afterwards. An
            # invalidation (closed/replaced/changed input) is reported as
            # ``stale``: nothing of it may be adopted.
            with self.lock:
                if not self._closed:
                    self._prepare_state.mark_failed_locked(
                        prepare_id,
                        str(exc) or "准备执行中断或失败。",
                        status="stale" if isinstance(exc, ConflictError) else "failed",
                    )
            raise
        finally:
            self._prepare_state.finish(prepare_id)




    def _prepare_run_batches(
        self,
        prepare_id: str,
        prepared: Mapping[str, Any],
        prior_judgments: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[str]:
        """Run the bounded generation + check batches with a bounded worker pool.

        The pool size is the parallelism the preview froze into the plan, never
        more than there are batches; the default 1 keeps the historical
        one-at-a-time behaviour (and then no pool is created at all). Every
        batch keeps its own identity while it runs: the model calls happen
        outside the project lock, the per-unit results and the failed-unit
        counter are written as they happen, and the request counters of all
        batches add up under the lock. A batch that fails for model reasons is
        recorded and its siblings keep going; a guard rejection (closed,
        superseded or mode-switched prepare) stops the scheduling and
        propagates, so a late result is never written.

        Returns the error of every batch that failed, in batch order; the
        per-unit results and the failed-unit counter are written as they
        happen, so a partly failed preparation can never be summarized as a
        completed one.
        """

        prior_judgments = prior_judgments or {}
        # No early exit for an empty batch list: a scope whose units are all
        # reused still has to freeze its groups and honour the bounded lookup.
        batches = list(prepared["batches"])
        # Batch errors are collected per position so the reported order does not
        # depend on which thread finished first; the A2 block below appends the
        # group-level failures to the same list, exactly as it always did.
        failures: list[str] = []
        batch_errors: list[str | None] = [None] * len(batches)

        def run_batch(index: int, batch: Mapping[str, Any]) -> None:
            """One batch, start to finish; the caller owns the thread."""

            with self.lock:
                self._prepare_state.guard_locked(prepare_id)
            try:
                result = self.scan_quality_batch(
                    batch_id=batch["batch_id"],
                    unit_ids=batch["unit_ids"],
                    expected_project_id=None,
                    mode="automatic",
                )
            except ConflictError:
                # A stale/superseded prepare is not a batch failure: it ends the
                # run, exactly as the serial loop did.
                raise
            except Exception as exc:
                batch_errors[index] = f"批次 {batch['batch_id']}：{exc}"
                with self.lock:
                    self._prepare_state.guard_locked(prepare_id)
                    support = normalize_quality_support(self.state.get("quality_support"))

                    def mutate_fail(record: dict[str, Any], batch=batch, exc=exc) -> None:
                        record["counts"]["failed_units"] = int(
                            record["counts"].get("failed_units") or 0
                        ) + len(batch["unit_ids"])
                        record["counts"]["skipped"] = int(record["counts"].get("skipped") or 0) + len(
                            batch["unit_ids"]
                        )
                        record["errors"].append(f"批次 {batch['batch_id']}：{exc}")
                        for row in record.get("unit_results") or []:
                            if row["unit_id"] in batch["unit_ids"]:
                                row["status"] = "failed"
                                row["reason"] = str(exc)[:300]

                    self._prepare_state.update_record_locked(support, prepare_id, mutate_fail)
                return
            with self.lock:
                self._prepare_state.guard_locked(prepare_id)
                support = normalize_quality_support(self.state.get("quality_support"))
                repair = result.get("repair") or {}
                generation_calls = int((repair.get("generate") or {}).get("api_calls") or 0)
                check_calls = int((repair.get("check") or {}).get("api_calls") or 0)
                if not generation_calls:
                    generation_calls = 1
                if not check_calls and result.get("candidate_count"):
                    check_calls = 1
                check_failed = str(result.get("check_status") or "") != "completed"
                saved = len(result.get("saved") or [])
                duplicates = int(result.get("duplicate_count") or 0)
                failed = len(result.get("failed") or [])

                def mutate_ok(
                    record: dict[str, Any],
                    batch=batch,
                    generation_calls=generation_calls,
                    check_calls=check_calls,
                    check_failed=check_failed,
                    saved=saved,
                    duplicates=duplicates,
                    failed=failed,
                ) -> None:
                    record["requests"]["generation"] = int(record["requests"].get("generation") or 0) + generation_calls
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + check_calls
                    for key, value in (("skipped", duplicates + failed), ("unresolved", 0)):
                        if value:
                            record["counts"][key] = int(record["counts"].get(key) or 0) + value
                    if not check_failed and not saved and not duplicates and not failed:
                        # A finished batch that produced no candidate at all is a
                        # real result, and it must be visible as "no candidates"
                        # rather than as a silent zero.
                        record["counts"]["no_candidate_units"] = int(
                            record["counts"].get("no_candidate_units") or 0
                        ) + len(batch["unit_ids"])
                    if check_failed:
                        record["counts"]["failed_units"] = int(record["counts"].get("failed_units") or 0) + len(
                            batch["unit_ids"]
                        )
                        record["errors"].append(
                            f"批次 {batch['batch_id']}：独立检查未完成，候选保留但不参与自动采用。"
                        )
                    for row in record.get("unit_results") or []:
                        if row["unit_id"] in batch["unit_ids"]:
                            row["status"] = "failed" if check_failed else "completed"
                            row["batch_id"] = batch["batch_id"]
                            row["reason"] = "独立检查未完成。" if check_failed else ""

                self._prepare_state.update_record_locked(support, prepare_id, mutate_ok)

        workers = max(
            1,
            min(
                resolve_parallel_batches(prepared.get("max_parallel_batches")),
                len(batches),
            ),
        )
        if workers == 1:
            for index, batch in enumerate(batches):
                run_batch(index, batch)
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="prepare-batch"
            ) as pool:
                futures = [pool.submit(run_batch, index, batch) for index, batch in enumerate(batches)]
                for future in as_completed(futures):
                    error = future.exception()
                    if error is not None:
                        # One guard rejection ends the whole confirmed run: stop
                        # whatever has not started and report it as the caller
                        # did before. Batches already in flight finish and are
                        # rejected by the guard when they try to write.
                        for other in futures:
                            other.cancel()
                        raise error
        # Merged before the A2 block: it appends the group-level failures and the
        # function keeps its single ``return failures`` exit point.
        failures.extend(item for item in batch_errors if item)

        # A2: the incrementally reused units keep their old cards, but an old
        # card is not automatically a usable one. Before the groups are frozen
        # from the live cards, the checks that cannot be adopted as is are
        # refreshed (same provider, same bounded repair loop, no new candidates)
        # and the run's single bounded lookup is spent on the questions that
        # asked for one. Both steps refuse to write anything their frozen
        # identity no longer matches.
        with self.lock:
            self._prepare_state.guard_locked(prepare_id)
        self._prepare_recheck.refresh_checks(prepare_id, prepared)
        self._prepare_lookup.bounded_lookup(prepare_id)
        # Freeze the related groups only now: they are derived from the cards
        # the confirmed execution just produced, not from the empty preview.
        with self.lock:
            self._prepare_state.guard_locked(prepare_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources = quality_unit_sources(self.state.get("units") or [])
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)

            frozen_groups: list[dict[str, Any]] = []
            reused_outcomes: dict[str, dict[str, Any]] = {}
            for group in groups:
                members = quality_prepare_plan.prepare_group_members(support, group["card_ids"])
                fingerprint = concept_automation.group_input_fingerprint(
                    group["group_id"], members, support, unit_sources
                )
                frozen_groups.append(
                    {
                        "group_id": group["group_id"],
                        "card_ids": group["card_ids"],
                        "oversized": group["oversized"],
                        "members": members,
                        # Frozen with the members: the judgment stays usable only
                        # while the live cards and sources still match this.
                        "input_fingerprint": fingerprint,
                    }
                )
                prior = prior_judgments.get(str(group["group_id"])) or {}
                # An oversized group may carry a *local* outcome from the previous
                # run (A3): it is reused exactly like a whole-group judgment, so an
                # unchanged project still costs nothing. A group without one keeps
                # waiting; no member is ever cut to make it fit.
                reusable = not group["oversized"] or bool(
                    (prior.get("outcome") or {}).get("local")
                )
                if (
                    not reusable
                    or len(members) < 2
                    or not fingerprint
                    or str(prior.get("input_fingerprint") or "") != fingerprint
                ):
                    continue
                # A reused judgment is re-validated against the live members and
                # sources before it is trusted: a fingerprint match never skips
                # the local protocol checks.
                outcome = copy.deepcopy(dict(prior.get("outcome") or {}))
                try:
                    normalized = normalize_resolution(
                        outcome.get("payload") or {},
                        member_ids={str(member["card_id"]) for member in members},
                        group_id=str(group["group_id"]),
                        source_units=dict(unit_sources),
                    )
                except Exception:
                    continue
                outcome["payload"] = normalized
                outcome["relation"] = str(normalized.get("relation") or outcome.get("relation") or "")
                outcome["reused"] = True
                outcome["from_prepare_id"] = str(prior.get("prepare_id") or "")
                reused_outcomes[str(group["group_id"])] = outcome

            def mutate_groups(
                record: dict[str, Any],
                frozen=frozen_groups,
                reused=reused_outcomes,
            ) -> None:
                plan = record.get("plan") or {}
                plan["groups"] = copy.deepcopy(frozen)
                resolved = dict(plan.get("resolved_groups") or {})
                resolved.update(copy.deepcopy(reused))
                plan["resolved_groups"] = resolved
                record["counts"]["reused_groups"] = len(reused)
                record["plan"] = plan

            self._prepare_state.update_record_locked(support, prepare_id, mutate_groups)
        return failures

    # ------------------------------------------------------------------
    # A2: the re-check of reused cards and the one bounded lookup
    # ------------------------------------------------------------------







    def _quality_prepare_resolve(
        self,
        *,
        plan: Mapping[str, Any],
        expected_project_id: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """Resolve the pending groups of a frozen prepare (separate step)."""

        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            record = quality_prepare_record.prepare_record(support, plan)
        self._prepare_resolution.resolve_pending(record["prepare_id"])
        return self._prepare_view_locked(prepare_id=record["prepare_id"])



    def _prepare_view_locked(
        self,
        *,
        prepare_id: str,
        support: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        support = support or normalize_quality_support(self.state.get("quality_support"))
        record = quality_prepare_record.prepare_record(support, {"prepare_id": prepare_id})
        return {
            "status": "ok",
            "prepare_id": record["prepare_id"],
            "prepare_status": record["status"],
            "summary": quality_prepare_record.prepare_summary_from_record(record),
        }

    def quality_prepare_status(
        self,
        *,
        expected_project_id: str | None = None,
        prepare_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only, bounded view of the current prepare and its live progress."""

        with self.lock:
            self._validate_expected_project_id_locked(expected_project_id)
            project_id = str(self.state.get("project", {}).get("id") or "")
            support = normalize_quality_support(self.state.get("quality_support"))
            automation = concept_automation.normalize_automation(support.get("automation"))
            record = automation.get("prepare")
            record_id = str(record.get("prepare_id") or "") if isinstance(record, Mapping) else ""
            requested_id = str(prepare_id or "").strip()
            if requested_id and requested_id != record_id:
                raise ConflictError("准备任务已经被替换或不存在，请刷新项目后重试观察。")

            progress = self._quality_runtime.progress
            if (
                not isinstance(progress, Mapping)
                or str(progress.get("prepare_id") or "") != record_id
                or str(progress.get("project_id") or "") != project_id
            ):
                progress = None
            active = bool(
                record_id
                and record_id in self._quality_runtime.prepare_inflight
                and isinstance(progress, Mapping)
                and progress.get("active") is True
            )
            persisted_status = str(record.get("status") or "") if isinstance(record, Mapping) else ""
            status = persisted_status or None
            if status == "running" and not active:
                # A durable running record without an owner is never resumed by
                # this read. The manager normally normalizes it on open; this
                # defensive view remains read-only if ownership was lost in-proc.
                status = "interrupted"
            if isinstance(progress, Mapping) and not active and progress.get("status"):
                # A commit/write error can leave a durable record in its previous
                # state while this live process knows the execution failed.
                if status == "interrupted" and progress.get("status") in {"failed", "stale"}:
                    status = str(progress.get("status"))

            if isinstance(record, Mapping):
                summary = quality_prepare_record.prepare_summary_from_record(record)
                summary["status"] = status or summary.get("status")
                # The status route is polled: keep its embedded error view
                # bounded without changing the existing quality-support result
                # contract returned by other routes.
                summary["errors"] = [
                    str(item)[:300]
                    for item in (summary.get("errors") or [])
                    if str(item).strip()
                ][-20:]
            else:
                summary = None

            stage_names = (
                "generation", "check", "recheck", "lookup",
                "group_resolution", "local_resolution", "commit",
            )
            if isinstance(progress, Mapping):
                stages = {
                    name: copy.deepcopy(dict((progress.get("stages") or {}).get(name) or {}))
                    for name in stage_names
                }
            else:
                # After restart only the persisted business summary is known.
                # Do not reconstruct counters or pretend a phase ran.
                stages = {
                    name: {
                        "state": "unknown",
                        "completed": None,
                        "total": None,
                        "running": None,
                        "failed": None,
                        "reused": None,
                        "not_required": None,
                        "pending": None,
                        "unit": unit,
                    }
                    for name, unit in (
                        ("generation", "batch"), ("check", "batch"),
                        ("recheck", "card"), ("lookup", "request"),
                        ("group_resolution", "group"),
                        ("local_resolution", "group_unit"), ("commit", "commit"),
                    )
                }
            if not record_id:
                stages = {
                    name: {
                        "state": "not_required", "completed": 0, "total": 0,
                        "running": 0, "failed": 0, "reused": 0,
                        "not_required": 0,
                        "pending": 0, "unit": unit,
                    }
                    for name, unit in (
                        ("generation", "batch"), ("check", "batch"),
                        ("recheck", "card"), ("lookup", "request"),
                        ("group_resolution", "group"),
                        ("local_resolution", "group_unit"), ("commit", "commit"),
                    )
                }

            if isinstance(progress, Mapping):
                active_items = [
                    copy.deepcopy(dict(item))
                    for item in (progress.get("_active") or {}).values()
                    if isinstance(item, Mapping)
                ][-32:]
                recent_activity = [
                    copy.deepcopy(dict(item))
                    for item in (progress.get("_recent_activity") or [])
                    if isinstance(item, Mapping)
                ][-20:]
                progress_errors = [str(item) for item in (progress.get("_errors") or []) if str(item).strip()]
                provider_calls = copy.deepcopy(dict(progress.get("provider_invocations") or {}))
                progress_revision = int(progress.get("progress_revision") or 0)
                started_at = str(progress.get("started_at") or "") or None
                updated_at = str(progress.get("updated_at") or "") or None
            else:
                active_items = []
                recent_activity = []
                progress_errors = []
                provider_calls = None
                progress_revision = None
                started_at = str(record.get("started_at") or "") or None if isinstance(record, Mapping) else None
                updated_at = str(record.get("finished_at") or record.get("started_at") or "") or None if isinstance(record, Mapping) else None
            summary_errors = list((summary or {}).get("errors") or [])
            errors: list[str] = []
            for item in summary_errors + progress_errors:
                if item and item not in errors:
                    errors.append(item)
            errors = errors[-20:]
            return {
                "project_id": project_id,
                "prepare_id": record_id or None,
                "status": status,
                "persisted_status": persisted_status or None,
                "active": active,
                "progress_revision": progress_revision,
                "started_at": started_at,
                "updated_at": updated_at,
                "stages": stages,
                "active_items": active_items,
                "recent_activity": recent_activity,
                "provider_calls": provider_calls,
                # The product does not expose an authoritative aggregate HTTP
                # counter across all configured providers.
                "provider_http": None,
                "errors": errors,
                "prepare": summary,
                "reference_mode": concept_automation.reference_mode(self.state.get("project")),
                "reference_revision": int(automation.get("reference_revision") or 0),
                "decisions": len(automation.get("decisions") or {}),
            }

    def editorial_suggestions(
        self,
        unit_id: str,
        *,
        expected_project_id: str | None = None,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Return optional local wording suggestions for a saved translation."""
        return self._editorial_workflow.suggestions(
            unit_id,
            expected_project_id=expected_project_id,
            expected_source_sha256=expected_source_sha256,
            expected_translation_revision=expected_translation_revision,
        )

    def decide(
        self,
        unit_id: str,
        decision: str,
        *,
        translation: str | None = None,
        expected_source_sha256: str | None = None,
    ) -> dict[str, Any]:
        decision = decision.strip().casefold()
        if decision not in {"edit", "accept-risk", "retry"}:
            raise PipelineError("裁决只能是 edit、accept-risk 或 retry。")
        with self.lock:
            self._ensure_open_locked()
            if unit_id in self._execution.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.state, unit_id)
            if unit.get("status") not in ACTION_STATUSES:
                raise PipelineError("只有待裁决的单元才能进行人工裁决。")
            if expected_source_sha256 and expected_source_sha256 != unit["source_sha256"]:
                raise PipelineError("源文已经变化，请刷新后再提交裁决。")
            pipeline_output.invalidate_output(self.state)
            if decision == "accept-risk":
                unit["status"] = "accepted_risk"
                unit["user_decision"] = "accept-risk"
                unit["last_error"] = None
                unit["updated_at"] = now_iso()
                self._event_locked("user_accepted_risk", "用户选择直接通过并接受当前校验风险。", unit_id)
                unit_state.recompute_unit_stats(self.state)
                self.state["run"]["status"] = execution.derived_run_status(self.state)
                self._save_locked()
                return copy.deepcopy(unit)
            if decision == "retry":
                unit_state.ensure_unit_feedback_fields(unit)
                current_revision = unit["translation_revision"]
                review = unit.get("review")
                review_revision = review.get("translation_revision") if isinstance(review, dict) else None
                if review_revision != current_revision:
                    raise PipelineError("当前校验结果与译文版本不一致，不能据此重新翻译。")
                # Same frozen record as the explicit retranslate entry: the
                # previous draft keeps its own revision and its own review, and
                # a repeated failure carries that same group forward instead of
                # re-binding it to this attempt.
                retained = unit_state.retained_previous_draft(unit)
                unit["pending_translation_feedback"] = {
                    "source_revision": retained["revision"] if retained else current_revision,
                    "suggestions": retained["suggestions"] if retained else [],
                    "previous_translation": retained["translation"] if retained else None,
                    "previous_review": retained["review"] if retained else None,
                }
                unit["status"] = "pending"
                unit["translation"] = ""
                unit["review"] = None
                unit["review_issues"] = []
                unit["review_suggestions"] = []
                unit["user_decision"] = "retry"
                unit["last_error"] = None
                unit["updated_at"] = now_iso()
                self._event_locked("user_requested_retry", "用户要求重新翻译并复检。", unit_id)
                self._scheduler.start_job_locked([unit_id], "translation")
                return copy.deepcopy(unit)
            if not translation or not translation.strip():
                raise PipelineError("修改后的译文不能为空。")
            unit_state.ensure_unit_feedback_fields(unit)
            unit["translation"] = translation.strip()
            unit["translation_revision"] += 1
            unit["pending_translation_feedback"] = None
            unit["review_suggestions"] = []
            unit["status"] = "reviewing"
            unit["user_decision"] = "edit"
            unit["review"] = None
            unit["review_issues"] = []
            unit["last_error"] = None
            unit["updated_at"] = now_iso()
            self._event_locked("user_submitted_edit", "用户修改译文，重新进入独立校验。", unit_id)
            self._scheduler.start_job_locked([unit_id], "review")
            return copy.deepcopy(unit)


manager: PipelineManager | None = None
