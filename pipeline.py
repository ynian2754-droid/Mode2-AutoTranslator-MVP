"""Persistent translation API facade over explicit project and domain owners.

PipelineManager assembles the shared state cell, execution and quality resources,
then delegates domain workflows while retaining project replacement, scheduler,
and output coordination at their original lock and persistence boundaries.
"""

from __future__ import annotations

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.api_settings import ApiSettingsStore
from core.assembler import AssemblyError, DocumentAssembler
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
from core.unit_commands import UnitCommands
from core.quality_batches import QualityBatchWorkflow
from core.quality_queries import QUALITY_SCAN_SCOPES, QualityQueries
from core.quality_cards import (
    QualityCards,
)
from core.quality_prepare_state import PrepareState
from core.quality_prepare_views import PrepareViews
from core.quality_prepare import PrepareCoordinator
from core.quality_recheck import PrepareRecheck
from core.quality_lookup import PrepareLookup
from core.quality_commit import PrepareCommit
from core.quality_resolution import PrepareResolution
from core.quality_progress import PrepareProgress
from core.quality_runtime import QualityRuntime
from core.importers import SourceImporter
from core.pdf_exporter import PdfExportError, PdfExporter
from core.pdf_fonts import PDF_MATH_FONT_PATH, load_pdf_fonts
from core.pdf_glyph_support import scan_text_glyphs, unavailable_scan
from core import pipeline_output, project_settings, project_state, provider_routing, unit_requests, unit_state
from core.project_factory import DEFAULT_SAMPLE_SOURCE, ProjectFactory
from core.project_loading import ProjectLoader
from core.segmenter import MarkdownSegmenter
from core.storage import ProjectStore
from core.utils import now_iso

from providers.api_provider import (
    OpenAICompatibleReviewProvider,
    OpenAICompatibleTranslationProvider,
)
from providers.base import (
    ReviewRequest,
    TranslationRequest,
)
from providers.demo_provider import DemoReviewProvider, DemoTranslationProvider
from providers.quality_provider import (
    FakeQualityProvider,
    OpenAICompatibleConceptCheckProvider,
    OpenAICompatibleConceptGenerationProvider,
    OpenAICompatibleConceptResolutionProvider,
    OpenAICompatibleEditorialSuggestionProvider,
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
        self._prepare_views = PrepareViews(
            self._project_state, self._quality_runtime, self._quality_queries,
            new_id=lambda: uuid.uuid4().hex,
        )
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
        self._prepare_coordinator = PrepareCoordinator(
            cell=self._project_state,
            runtime=self._quality_runtime,
            progress=self._quality_progress,
            prepare_state=self._prepare_state,
            queries=self._quality_queries,
            views=self._prepare_views,
            batches=self._quality_batches,
            recheck=self._prepare_recheck,
            lookup=self._prepare_lookup,
            resolution=self._prepare_resolution,
            committer=self._prepare_commit,
            clock=lambda: now_iso(),
            new_id=lambda: uuid.uuid4().hex,
            executor_factory=lambda **kwargs: ThreadPoolExecutor(**kwargs),
            completed_futures=lambda futures: as_completed(futures),
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
        self._unit_commands = UnitCommands(
            self._project_state, self._execution, self._scheduler.start_job_locked,
            _unit_request_clock,
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

        return self._unit_commands.save_translation(
            unit_id, translation,
            expected_source_sha256=expected_source_sha256,
            expected_translation_revision=expected_translation_revision,
        )

    def review_unit(
        self,
        unit_id: str,
        *,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit review of the persisted formal translation."""

        return self._unit_commands.review_unit(
            unit_id,
            expected_source_sha256=expected_source_sha256,
            expected_translation_revision=expected_translation_revision,
        )

    def retranslate_unit(
        self,
        unit_id: str,
        *,
        expected_project_id: str | None = None,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit translation retry using only persisted feedback."""

        return self._unit_commands.retranslate_unit(
            unit_id,
            expected_project_id=expected_project_id,
            expected_source_sha256=expected_source_sha256,
            expected_translation_revision=expected_translation_revision,
        )

    # ------------------------------------------------------------------
    # Quality support: concept cards, bounded scans, editorial suggestions.
    # ------------------------------------------------------------------


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
        return self._prepare_coordinator.run(
            phase=phase, plan=plan, unit_ids=unit_ids, current_unit_id=current_unit_id,
            max_source_words=max_source_words, max_parallel_batches=max_parallel_batches,
            expected_project_id=expected_project_id, expected_revision=expected_revision,
            additional_work_limit=additional_work_limit,
        )


    def quality_prepare_status(
        self,
        *,
        expected_project_id: str | None = None,
        prepare_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only, bounded view of the current prepare and its live progress."""
        return self._prepare_views.status(
            expected_project_id=expected_project_id, prepare_id=prepare_id,
        )

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
        return self._unit_commands.decide(
            unit_id, decision, translation=translation,
            expected_source_sha256=expected_source_sha256,
        )


manager: PipelineManager | None = None
