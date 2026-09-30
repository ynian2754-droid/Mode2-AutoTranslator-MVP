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
from core.quality_cards import (
    QUALITY_ACTION_LABELS,
    QUALITY_BATCH_ACTIONS,
    QUALITY_CARD_ACTIONS,
    QualityCards,
)
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
    ConceptResolutionRequest,
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

#: One shared pool of *extra* logical requests per confirmed execution: the
#: bounded lookup and the large-group local judgments draw from the same number,
#: never one budget each.
DEFAULT_ADDITIONAL_WORK_LIMIT = 10

#: Cards one re-check request may carry. A reused card is re-checked only when
#: its stored check is legacy or its verification identity changed; the bound
#: keeps one execution from turning a large incremental scope into an unbounded
#: number of requests.
MAX_RECHECK_CARDS = 20
#: Units one bounded lookup may cite as evidence. The search itself is local and
#: free; the bound is on the material handed to the one follow-up request.
MAX_LOOKUP_UNITS_PER_EXPRESSION = 5
#: One request slot is not a licence for an unbounded payload: a re-check or a
#: lookup request is also bounded by the units it may cite and by the characters
#: of cards + cited sources it would carry. What does not fit is recorded as
#: unfinished (or carried by the next confirmation), never cut down to size.
MAX_CHECK_REQUEST_UNITS = 40
MAX_CHECK_REQUEST_CHARS = 60_000
MAX_LOOKUP_CARDS = 20

QUALITY_SCAN_SCOPES = {"current", "selected", "continue"}

ACTIVE_STATUSES = {"translating", "reviewing"}
WAITING_STATUSES = {"waiting_translation", "waiting_review"}
PROCESSING_STATUSES = ACTIVE_STATUSES | WAITING_STATUSES
TRANSLATION_PROCESSING_STATUSES = {"waiting_translation", "translating"}
ACTION_STATUSES = {"needs_action"}
EDITABLE_TRANSLATION_STATUSES = {"needs_action", "passed", "user_modified", "accepted_risk"}
# Accepting a risk is a user override, not an AI review result.  It remains
# editable and can be sent through the normal translate->review retry flow,
# but it must not expose a direct review operation of the already accepted
# version.
REVIEWABLE_TRANSLATION_STATUSES = {"needs_action", "passed", "user_modified"}
CANCELLABLE_START_STATUSES = {"pending", "cancelled"}
OUTPUT_FORMATS = ("markdown", "text", "pdf", "epub", "docx")
STOP_GRACE_SECONDS = 5.0
_UNSET = object()


def _unit_request_clock() -> str:
    return now_iso()


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
        self._executor: ThreadPoolExecutor | None = None
        self._executor_max_concurrency: int | None = None
        self._active_unit_ids: set[str] = set()
        self._active_futures: dict[str, Future[Any]] = {}
        self._active_task_meta: dict[str, dict[str, Any]] = {}
        self._run_cancel_events: dict[str, threading.Event] = {}
        self._stop_timers: dict[str, threading.Timer] = {}
        self._retired_executors: dict[str, ThreadPoolExecutor] = {}
        # (unit_id, "translation"|"review") -> the invocation that currently owns
        # that work.  In-memory only: it exists to reject a late notification
        # from a superseded execution, and is never persisted.
        self._active_invocations: dict[tuple[str, str], str] = {}
        # Read-only PDF glyph pre-check cache, keyed by a cheap state signature
        # because the workbench polls the output status.
        self._glyph_precheck_cache: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self._closed = False
        self.state = self._load_state()

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

    @staticmethod
    def _same_automatic_decision(
        stored: Mapping[str, Any] | None,
        fresh: Mapping[str, Any] | None,
    ) -> bool:
        """Whether two decisions say the same thing about the same card.

        The run id and the decision timestamp describe *when* a decision was
        written, not what it allows. Re-running a preparation over unchanged
        input must therefore keep the existing record verbatim instead of
        re-stamping it, so the reference revision and the frozen snapshot's
        decision id stay valid."""

        if not isinstance(stored, Mapping) or not isinstance(fresh, Mapping):
            return False
        ignored = {"prepare_id", "decided_at"}
        for key in (set(stored) | set(fresh)) - ignored:
            left, right = stored.get(key), fresh.get(key)
            if key in ("allowed_unit_ids", "member_ids", "evidence"):
                if json.dumps(left, sort_keys=True, ensure_ascii=False) != json.dumps(
                    right, sort_keys=True, ensure_ascii=False
                ):
                    return False
            elif left != right:
                return False
        return True

    @staticmethod
    def _approved_expressions(support: Mapping[str, Any]) -> tuple[str, ...]:
        """Every expression a human-approved card currently carries, sorted."""

        return tuple(
            sorted(
                {
                    expression
                    for card in (support.get("cards") or {}).values()
                    for expression in ((card.get("approved") or {}).get("expressions") or [])
                }
            )
        )

    @staticmethod
    def _concept_unit_refs(units: Sequence[Mapping[str, Any]]) -> tuple[ConceptUnitRef, ...]:
        """One ``ConceptUnitRef`` per unit, using the unit's own source binding."""

        return tuple(
            ConceptUnitRef(
                unit_id=str(unit["id"]),
                source_text=str(unit.get("source") or ""),
                source_sha256=str(unit.get("source_sha256") or ""),
            )
            for unit in units
        )

    @staticmethod
    def _concept_unit_refs_from_sources(
        unit_ids: Sequence[str],
        unit_sources: Mapping[str, tuple[str, str]],
    ) -> tuple[ConceptUnitRef, ...]:
        """One ``ConceptUnitRef`` per known unit id, read from a source mapping.

        An id the project no longer holds is skipped: a request may only cite
        units that still exist.
        """

        return tuple(
            ConceptUnitRef(
                unit_id=unit_id,
                source_text=str((unit_sources.get(unit_id) or ("", ""))[0]),
                source_sha256=str((unit_sources.get(unit_id) or ("", ""))[1]),
            )
            for unit_id in unit_ids
            if unit_id in unit_sources
        )

    @staticmethod
    def _resolve_explicit_target_words(
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

    #: The controller authors a review when an attempt fails technically
    #: (transport error, a payload the strict gate rejected). Such a record
    #: describes *that attempt*; it is never the review of a saved draft.
    _TECHNICAL_REVIEW_PROVIDER = "controller"

    def _load_state(self) -> dict[str, Any]:
        state = self.store.load()
        if isinstance(state, dict) and state.get("schema_version") == 1:
            stale = bool(state.get("run", {}).get("running"))
            state.setdefault("events", [])
            state.setdefault("config", {})
            state.setdefault("units", [])
            state.setdefault("run", {})
            state.setdefault("project", {})
            state.setdefault("document", None)
            state.setdefault("output", empty_output_state())
            state["project"].setdefault("source_file", None)
            if not isinstance(state["run"].get("unit_ids"), list):
                state["run"]["unit_ids"] = []
            state["run"].setdefault("unit_ids", [])
            if not isinstance(state["run"].get("completed_unit_ids"), list):
                state["run"]["completed_unit_ids"] = []
            state["run"].setdefault("completed_unit_ids", [])
            state["run"].setdefault("max_concurrency", None)
            state["run"].setdefault("cancel_requested", False)
            state["run"].setdefault("stop_requested_at", None)
            state["run"].setdefault("cancelled_at", None)
            state["run"].setdefault("stop_timeout_at", None)
            previous_provider = state["config"].get("provider")
            previous_review_provider = state["config"].get("review_provider")
            provider_migrated = (
                previous_provider != "openai-compatible"
                or previous_review_provider != "openai-compatible"
            )
            state["config"]["provider"] = "openai-compatible"
            state["config"]["review_provider"] = "openai-compatible"
            # Keep legacy projects read-compatible without adding or rewriting
            # the canonical field during load.
            project_settings.configured_target_words(state["config"])
            for unit in state["units"]:
                was_translating = unit.get("status") in TRANSLATION_PROCESSING_STATUSES
                unit_state.ensure_unit_feedback_fields(unit)
                if was_translating and unit["translation_revision"] > 0:
                    # A translating unit has not committed a new translation yet.
                    # Roll back its queued revision so persisted feedback remains
                    # attached to the next translation attempt after a restart.
                    unit["translation_revision"] -= 1
                if unit.get("status") in PROCESSING_STATUSES:
                    unit["status"] = "pending"
                    unit["last_error"] = "应用重启后已回到待处理队列。"
            state["run"]["running"] = False
            if stale:
                state["run"]["status"] = "ready"
                state["run"]["cancel_requested"] = False
                state["run"]["stop_requested_at"] = None
            elif state["run"].get("status") == "stopping":
                # A previous process may have persisted the transient state
                # after its worker had already disappeared.  Do not resurrect
                # that impossible state on the next page load.
                state["run"]["status"] = "cancelled" if state["run"].get("cancel_requested") else "ready"
                state["run"]["completed_at"] = state["run"].get("completed_at") or now_iso()
            self.state = state
            if provider_migrated:
                self._event_locked(
                    "provider_migrated",
                    "旧 Provider 配置已迁移为 OpenAI Compatible。",
                    previous_provider=previous_provider,
                    previous_review_provider=previous_review_provider,
                )
            self._ensure_document_manifest_locked()
            self._normalize_interrupted_prepare_locked(state)
            self._recompute_stats_locked()
            self._save_locked()
            return state
        state = self._new_state(DEFAULT_SAMPLE_SOURCE, demo_mode=True, max_concurrency=3, provider="demo")
        self.state = state
        self._save_locked()
        return state

    def _normalize_interrupted_prepare_locked(self, state: dict[str, Any]) -> bool:
        """A restarted process cannot still be running a preparation.

        A persisted ``running`` prepare record is a crash image: nothing
        continues it in the background (there is no runner), so on load it must
        read as ``interrupted`` — otherwise it would block every new
        preparation forever — and the normalized state is written back so the
        recovery is visible on disk, not only in this process's memory.
        """

        support = state.get("quality_support")
        if not isinstance(support, dict):
            return False
        automation = support.get("automation")
        if not isinstance(automation, dict):
            return False
        record = automation.get("prepare")
        if not isinstance(record, dict) or str(record.get("status") or "") != "running":
            return False
        record["status"] = "interrupted"
        record["committed"] = False
        record["finished_at"] = str(record.get("finished_at") or "") or now_iso()
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        note = "应用重启，准备任务已中断；未完成的部分可以重新准备，已提交的参考不受影响。"
        if note not in errors:
            errors.append(note)
        record["errors"] = errors
        return True

    def _ensure_document_manifest_locked(self) -> None:
        """Backfill manifests only when the stored source proves the unit mapping."""
        document = self.state.get("document")
        if isinstance(document, dict) and isinstance(document.get("parts"), list) and document.get("parts"):
            return
        units = self.state.get("units") or []
        if not units:
            self.state["document"] = empty_document(
                source_sha256=self.state.get("project", {}).get("source_sha256", ""),
            )
            return
        source_file = self.state.get("project", {}).get("source_file") or {}
        stored_path = str(source_file.get("stored_path") or "")
        if not stored_path:
            return
        runtime_root = self.runtime_dir.resolve()
        source_path = (runtime_root / Path(stored_path)).resolve()
        if runtime_root not in source_path.parents or not source_path.is_file():
            return
        try:
            imported = self.source_importer.import_bytes(source_file.get("name") or source_path.name, source_path.read_bytes())
        except (OSError, ValueError):
            return
        segmentation_options: list[dict[str, Any]] = [{}]
        if imported.format == "epub" and imported.structure_blocks:
            segmentation_options.append({"structure_blocks": imported.structure_blocks})
        elif imported.format in {"text", "markdown"}:
            segmentation_options.append({"group_adjacent_paragraphs": True})
        try:
            existing_binding = [
                (str(unit.get("id") or ""), int(unit.get("order") or 0), str(unit.get("source_sha256") or ""))
                for unit in units
            ]
        except (AttributeError, TypeError, ValueError):
            return
        for option_index, options in enumerate(segmentation_options):
            try:
                generated_units, generated_document = self.segmenter.segment_document(
                    imported.text,
                    demo_mode=bool(self.state.get("project", {}).get("demo_mode")),
                    max_words=project_settings.configured_target_words(self.state.get("config", {})),
                    document_format=imported.format,
                    source_name=imported.original_name,
                    **options,
                )
            except (OSError, ValueError):
                if option_index == 0:
                    return
                continue
            try:
                generated_binding = [
                    (str(unit.get("id") or ""), int(unit.get("order") or 0), str(unit.get("source_sha256") or ""))
                    for unit in generated_units
                ]
            except (AttributeError, TypeError, ValueError):
                continue
            if existing_binding == generated_binding:
                # This fills the document-to-existing-unit manifest only.  It
                # never replaces or reorders the persisted units themselves.
                self.state["document"] = generated_document
                return

    def _new_state(
        self,
        source_text: str,
        *,
        demo_mode: bool,
        max_concurrency: int,
        provider: str,
        source_language: str = "English",
        target_language: str = "简体中文",
        source_file: dict[str, Any] | None = None,
        pdf_reconstruction: Any | None = None,
        structure_blocks: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        group_adjacent_paragraphs: bool = False,
        target_segment_words: int | None = None,
        max_segment_words: int | None = None,
    ) -> dict[str, Any]:
        try:
            state = self.project_factory.create_state(
                source_text,
                demo_mode=demo_mode,
                max_concurrency=max_concurrency,
                provider=provider,
                source_language=source_language,
                target_language=target_language,
                source_file=source_file,
                pdf_reconstruction=pdf_reconstruction,
                structure_blocks=structure_blocks,
                group_adjacent_paragraphs=group_adjacent_paragraphs,
                target_segment_words=target_segment_words,
                max_segment_words=max_segment_words,
            )
            # Segmenters are shared with legacy callers.  Normalize the
            # persisted Unit contract here so every newly-created project has
            # the manual-edit field without changing the segmenter API.
            for unit in state.get("units") or []:
                if isinstance(unit, dict):
                    unit_state.ensure_unit_feedback_fields(unit)
            return state
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc

    def _save_locked(self) -> None:
        project_state.save_project(self._project_state)

    def _ensure_open_locked(self) -> None:
        project_state.ensure_open(self._project_state)

    def _invalidate_output_locked(self) -> None:
        output = self.state.get("output")
        if isinstance(output, dict) and output.get("path"):
            self.state["output"] = empty_output_state()

    def _event_locked(self, event_type: str, message: str, unit_id: str | None = None, **details: Any) -> None:
        project_state.append_event(self._project_state, event_type, message, unit_id, details, now_iso)

    def _recompute_stats_locked(self) -> None:
        units = self.state.get("units", [])
        counts = {
            "total": len(units),
            "pending": sum(item.get("status") == "pending" for item in units),
            "active": sum(item.get("status") in ACTIVE_STATUSES for item in units),
            "waiting": sum(item.get("status") in WAITING_STATUSES for item in units),
            "cancelled": sum(item.get("status") == "cancelled" for item in units),
            "passed": sum(item.get("status") == "passed" for item in units),
            "user_modified": sum(item.get("status") == "user_modified" for item in units),
            "needs_action": sum(item.get("status") == "needs_action" for item in units),
            "accepted_risk": sum(item.get("status") == "accepted_risk" for item in units),
            "failed": sum(item.get("status") == "failed" for item in units),
        }
        counts["done"] = counts["passed"] + counts["user_modified"] + counts["accepted_risk"]
        counts["progress_percent"] = round((counts["done"] / counts["total"]) * 100, 1) if counts["total"] else 0
        self.state["stats"] = counts

    def _snapshot_locked(self) -> dict[str, Any]:
        self._recompute_stats_locked()
        return copy.deepcopy(self.state)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def _glyph_precheck_locked(self) -> dict[str, Any]:
        """Read-only scan of the saved translations against the embedded PDF font.

        Used by the pre-export warning so the workbench can name the affected
        units before the user clicks export.  Cached by a cheap state signature
        (the UI polls the output status) and never raises: when ReportLab or the
        font is unavailable it degrades to an explicit ``available: False``
        result instead of pretending there is nothing to report.
        """

        return pipeline_output.glyph_precheck_locked(
            self,
            pdf_math_font_path=PDF_MATH_FONT_PATH,
            load_fonts=load_pdf_fonts,
            scan_glyphs=scan_text_glyphs,
            unavailable_scan=unavailable_scan,
        )

    def output_status(self) -> dict[str, Any]:
        with self.lock:
            return pipeline_output.output_status_locked(self, OUTPUT_FORMATS)

    def generate_output(self, output_format: str | None = None) -> dict[str, Any]:
        with self.lock:
            self._ensure_open_locked()
            return pipeline_output.generate_output_locked(
                self,
                output_format,
                assembly_error=AssemblyError,
                docx_export_error=DocxExportError,
                docx_exporter=DocxExporter,
                epub_export_error=EpubExportError,
                pdf_export_error=PdfExportError,
                epub_exporter=EpubExporter,
                pdf_exporter=PdfExporter,
            )

    def output_file_path(self, output_format: str | None = None) -> Path:
        with self.lock:
            return pipeline_output.output_file_path_locked(self, output_format)

    def _default_output_format_locked(self) -> str:
        return pipeline_output.default_output_format_locked(self)

    def _resolve_output_format_locked(self, value: Any | None) -> str:
        return pipeline_output.resolve_output_format_locked(self, value, OUTPUT_FORMATS)

    def segmentation_settings(self) -> dict[str, Any]:
        with self.lock:
            target_words = project_settings.configured_target_words(self.state.get("config", {}))
            return {
                "target_words": target_words,
                "max_words": target_words,
                "default_target_words": DEFAULT_TARGET_WORDS,
                "default_max_words": DEFAULT_TARGET_WORDS,
                "target_is_hard_limit": False,
                "sentence_boundary_priority": True,
                "emergency_fallback": "unterminated_oversized_text",
                "project_name": self.state.get("project", {}).get("name"),
            }

    def concurrency_settings(self) -> dict[str, Any]:
        """Return the persisted project concurrency and any frozen Run value."""

        with self.lock:
            run = self.state.get("run") or {}
            return {
                "max_concurrency": int(self.state.get("config", {}).get("max_concurrency") or 3),
                "run_max_concurrency": (
                    int(run["max_concurrency"])
                    if run.get("running") and run.get("max_concurrency") is not None
                    else None
                ),
                "running": bool(run.get("running")),
                "min_concurrency": 1,
            }

    def update_concurrency_settings(self, max_concurrency: Any) -> dict[str, Any]:
        """Persist a scheduler size only while the project is idle.

        The idle executor is discarded so the next Run cannot silently reuse
        a pool created with an older value.
        """

        if isinstance(max_concurrency, bool):
            raise ValueError("并发数必须是大于或等于 1 的整数。")
        try:
            value = int(max_concurrency)
        except (TypeError, ValueError) as exc:
            raise ValueError("并发数必须是大于或等于 1 的整数。") from exc
        if value != max_concurrency or value < 1:
            raise ValueError("并发数必须是大于或等于 1 的整数。")

        with self.lock:
            self._ensure_open_locked()
            run = self.state.get("run") or {}
            if run.get("running") or self._active_unit_ids:
                raise ConflictError("当前流水线正在运行，不能修改并发数。")
            current = int(self.state.get("config", {}).get("max_concurrency") or 3)
            if current != value:
                self._close_executor_locked()
                self.state.setdefault("config", {})["max_concurrency"] = value
                self._event_locked("concurrency_updated", f"并发数已确认设置为 {value}。")
                self._save_locked()
            return self.concurrency_settings()

    def translation_context_settings(self) -> dict[str, Any]:
        """Return the request-local source-context settings for this project."""

        with self.lock:
            config = self.state.get("config", {})
            target_words = project_settings.configured_target_words(config)
            default_words = default_context_words(target_words)
            previous_words, next_words = unit_requests.configured_context_words_for_state(self.state)
            return {
                "previous_context_words": previous_words,
                "next_context_words": next_words,
                "default_previous_context_words": default_words,
                "default_next_context_words": default_words,
                "max_context_words": MAX_CONTEXT_WORDS,
                "sentence_boundary_priority": True,
                "target_segment_words": target_words,
                "project_name": self.state.get("project", {}).get("name"),
            }

    def update_translation_context_settings(
        self,
        previous_context_words: Any = _UNSET,
        next_context_words: Any = _UNSET,
    ) -> dict[str, Any]:
        """Persist explicit context budgets without changing Units or translations."""

        with self.lock:
            self._ensure_open_locked()
            if isinstance(previous_context_words, dict) and next_context_words is _UNSET:
                payload = previous_context_words
                previous_context_words = payload.get("previous_context_words", _UNSET)
                next_context_words = payload.get("next_context_words", _UNSET)
            if previous_context_words is _UNSET and next_context_words is _UNSET:
                raise ValueError("必须提供 previous_context_words 或 next_context_words。")

            config = self.state.setdefault("config", {})
            current_previous, current_next = unit_requests.configured_context_words_for_state(self.state)
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
            self._save_locked()
            return self.translation_context_settings()

    def update_segmentation_settings(
        self,
        max_words: Any = _UNSET,
        *,
        target_words: Any = _UNSET,
    ) -> dict[str, Any]:
        with self.lock:
            self._ensure_open_locked()
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

            config = self.state.setdefault("config", {})
            config["target_segment_words"] = value
            config.pop("max_segment_words", None)
            self._save_locked()
            return self.segmentation_settings()

    def _resolve_segment_words_locked(
        self,
        value: Any | None = None,
        *,
        target_segment_words: Any | None = None,
        max_segment_words: Any | None = None,
    ) -> int:
        if value is not None:
            if target_segment_words is not None or max_segment_words is not None:
                raise ValueError("不能同时使用位置参数和命名切分词数参数。")
            max_segment_words = value
        explicit = self._resolve_explicit_target_words(
            target_segment_words,
            max_segment_words,
        )
        return explicit if explicit is not None else project_settings.configured_target_words(
            self.state.get("config", {})
        )

    def get_unit(self, unit_id: str) -> dict[str, Any]:
        with self.lock:
            unit = self._find_unit_locked(unit_id)
            return copy.deepcopy(unit)

    def _find_unit_locked(self, unit_id: str) -> dict[str, Any]:
        for unit in self.state["units"]:
            if unit["id"] == unit_id:
                return unit
        raise PipelineError(f"找不到翻译单元：{unit_id}")

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
            self._close_executor_locked()
            segment_words = self._resolve_segment_words_locked(
                target_segment_words=target_segment_words,
                max_segment_words=max_segment_words,
            )
            self.state = self._new_state(
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
            return self._snapshot_locked()

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
            self._close_executor_locked()
            segment_words = self._resolve_segment_words_locked(
                target_segment_words=target_segment_words,
                max_segment_words=max_segment_words,
            )
            new_state = self._new_state(
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
            return self._snapshot_locked()

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
            self._close_executor_locked()
            try:
                backup_path = self.store.backup_state(old_state, reason="before_resegment")
            except OSError as exc:
                raise PipelineError(f"重新切分前备份失败：{exc}") from exc

            try:
                source_metadata = imported.metadata()
                source_metadata["stored_path"] = stored_path
                replacement = self._new_state(
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
            result = self._snapshot_locked()
            result["resegmentation"] = {
                "backup_path": backup_path,
                "previous_unit_count": len(old_state.get("units") or []),
                "unit_count": len(result.get("units") or []),
                "target_words": target_words,
            }
            return result

    def _provider_pair(self) -> tuple[Any, Any]:
        with self.lock:
            provider_name = str(self.state["config"].get("provider") or "demo")
        return provider_routing.resolve_unit_pair(
            provider_name,
            self._provider_bindings,
            self.api_settings,
            provider_routing.UnitFactories(
                OpenAICompatibleTranslationProvider,
                OpenAICompatibleReviewProvider,
                DemoTranslationProvider,
                DemoReviewProvider,
            ),
        )

    def _ensure_executor_locked(self) -> ThreadPoolExecutor:
        self._ensure_open_locked()
        run = self.state.get("run") or {}
        configured = run.get("max_concurrency") if run.get("running") else None
        max_workers = int(configured or self.state["config"].get("max_concurrency") or 3)
        if self._executor is not None and self._executor_max_concurrency != max_workers:
            if self._active_unit_ids:
                raise ConflictError("当前流水线正在运行，不能切换任务调度器的并发数。")
            self._executor.shutdown(wait=True)
            self._executor = None
            self._executor_max_concurrency = None
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="mode2-worker",
            )
            self._executor_max_concurrency = max_workers
        return self._executor

    def _close_executor_locked(self) -> None:
        if self._active_unit_ids:
            raise ConflictError("当前项目仍有单元在运行，不能关闭任务调度器。")
        for timer in self._stop_timers.values():
            timer.cancel()
        self._stop_timers.clear()
        self._run_cancel_events.clear()
        executor = self._executor
        self._executor = None
        self._executor_max_concurrency = None
        self._active_futures.clear()
        if executor is not None:
            executor.shutdown(wait=True)
        retired_executors = list(self._retired_executors.values())
        self._retired_executors.clear()
        for retired_executor in retired_executors:
            retired_executor.shutdown(wait=False)

    def close(self) -> None:
        """Release the project scheduler after the session has become idle."""
        with self.lock:
            if self._closed:
                return
            self._close_executor_locked()
            self._closed = True

    def _begin_run_locked(self, unit_count: int, mode: str) -> str:
        self._ensure_open_locked()
        run = self.state["run"]
        if not run.get("running"):
            run_id = f"run-{uuid.uuid4().hex[:10]}"
            cancel_event = threading.Event()
            self._run_cancel_events[run_id] = cancel_event
            run.update(
                {
                    "run_id": run_id,
                    "status": "running",
                    "running": True,
                    "max_concurrency": int(self.state["config"].get("max_concurrency") or 3),
                    "started_at": now_iso(),
                    "completed_at": None,
                    "unit_ids": [],
                    "completed_unit_ids": [],
                    "cancel_requested": False,
                    "stop_requested_at": None,
                    "cancelled_at": None,
                    "stop_timeout_at": None,
                }
            )
            self._event_locked(
                "run_started",
                f"开始处理 {unit_count} 个单元，并发数 {run['max_concurrency']}。",
                mode=mode,
            )
            return run_id
        if run.get("cancel_requested"):
            raise ConflictError("当前流水线正在停止，请等待停止完成后再提交任务。")
        run_id = str(run.get("run_id") or f"run-{uuid.uuid4().hex[:10]}")
        run["run_id"] = run_id
        self._run_cancel_events.setdefault(run_id, threading.Event())
        return run_id

    def _cancel_requested_locked(self, run_id: str) -> bool:
        run = self.state.get("run") or {}
        if run.get("run_id") == run_id and run.get("cancel_requested"):
            return True
        cancel_event = self._run_cancel_events.get(run_id)
        return bool(cancel_event and cancel_event.is_set())

    def _run_has_active_tasks_locked(self, run_id: str) -> bool:
        return any(
            task_meta.get("run_id") == run_id
            for task_meta in self._active_task_meta.values()
        )

    def _cancel_stop_timer_locked(self, run_id: str) -> None:
        timer = self._stop_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    def _shutdown_retired_executor_locked(self, run_id: str) -> None:
        executor = self._retired_executors.pop(run_id, None)
        if executor is not None:
            # This is called by a worker's done callback.  Waiting here would
            # make the worker wait for its own executor to shut down.
            executor.shutdown(wait=False)

    def _mark_cancelled_locked(self, unit_id: str, message: str = "用户已停止当前流水线。") -> None:
        unit = self._find_unit_locked(unit_id)
        if unit.get("status") == "cancelled":
            return
        unit["status"] = "cancelled"
        unit["last_error"] = message
        unit["updated_at"] = now_iso()
        self._event_locked("unit_cancelled", message, unit_id)

    def _finish_run_if_idle_locked(self, run_id: str) -> None:
        run = self.state["run"]
        if run.get("run_id") != run_id or self._run_has_active_tasks_locked(run_id):
            return
        expected_unit_ids = {str(unit_id) for unit_id in run.get("unit_ids") or []}
        completed_unit_ids = {str(unit_id) for unit_id in run.get("completed_unit_ids") or []}
        if expected_unit_ids and not expected_unit_ids.issubset(completed_unit_ids):
            return
        if not run.get("running"):
            return
        run["running"] = False
        run["completed_at"] = now_iso()
        if run.get("cancel_requested"):
            run["status"] = "cancelled"
            run["cancelled_at"] = run["completed_at"]
        else:
            run["status"] = self._derived_run_status_locked()
        self._event_locked(
            "run_finished",
            f"当前任务集合结束：{run['status']}。",
            counts=self.state["stats"],
        )
        self._cancel_stop_timer_locked(run_id)
        self._run_cancel_events.pop(run_id, None)
        self._shutdown_retired_executor_locked(run_id)

    def _force_finish_stopping_run(self, run_id: str) -> None:
        """Close the controller state if a provider ignores cooperative stop.

        Python threads cannot be safely killed.  The per-run cancellation event
        remains set so a late provider response is discarded, while the UI and
        scheduler are allowed to move on to a new run for other units.
        """
        with self.lock:
            run = self.state.get("run") or {}
            if run.get("run_id") != run_id or not run.get("running") or not run.get("cancel_requested"):
                return
            active_unit_ids = [
                unit_id
                for unit_id, task_meta in self._active_task_meta.items()
                if task_meta.get("run_id") == run_id
            ]
            if not active_unit_ids:
                expected_unit_ids = {
                    str(unit_id) for unit_id in run.get("unit_ids") or []
                }
                completed_unit_ids = run.setdefault("completed_unit_ids", [])
                for unit_id in expected_unit_ids.difference(completed_unit_ids):
                    unit = self._find_unit_locked(unit_id)
                    if unit.get("status") in PROCESSING_STATUSES or unit.get("status") == "pending":
                        self._mark_cancelled_locked(unit_id, "停止请求已收尾，未启动的任务已取消。")
                    completed_unit_ids.append(unit_id)
                self._recompute_stats_locked()
                self._finish_run_if_idle_locked(run_id)
                self._save_locked()
                return
            for unit_id in active_unit_ids:
                self._mark_cancelled_locked(unit_id, "停止等待超时，已放弃等待该请求。")
            completed_at = now_iso()
            run["running"] = False
            run["status"] = "cancelled"
            run["completed_at"] = completed_at
            run["cancelled_at"] = completed_at
            run["stop_timeout_at"] = completed_at
            self._recompute_stats_locked()
            # Do not let a provider that ignores cancellation occupy the
            # executor needed by the next Run.  Its old workers remain
            # isolated and can only discard their late results.
            if self._executor is not None:
                self._retired_executors[run_id] = self._executor
                self._executor = None
            self._event_locked(
                "run_finished",
                "停止等待超时，流水线已结束；迟到的请求结果将被丢弃。",
                active_unit_count=len(active_unit_ids),
            )
            self._cancel_stop_timer_locked(run_id)
            self._save_locked()

    def _queue_unit_locked(self, unit_id: str, mode: str, run_id: str) -> None:
        self._ensure_open_locked()
        if unit_id in self._active_unit_ids:
            raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
        if mode not in {"translation", "review"}:
            raise PipelineError(f"不支持的任务模式：{mode}")

        unit = self._find_unit_locked(unit_id)
        unit_state.ensure_unit_feedback_fields(unit)
        previous_revision: int | None = None
        if mode == "translation":
            if unit.get("status") not in CANCELLABLE_START_STATUSES:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新翻译。")
            previous_revision = unit["translation_revision"]
            unit["translation_revision"] = previous_revision + 1
            unit["status"] = "waiting_translation"
            unit["translation_attempts"] += 1
            unit["updated_at"] = now_iso()
            self._event_locked("translation_queued", "翻译任务已进入共享调度器。", unit_id)
        else:
            if unit.get("status") not in {"reviewing", *REVIEWABLE_TRANSLATION_STATUSES}:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新校验。")
            unit["status"] = "waiting_review"
            unit["updated_at"] = now_iso()
            self._event_locked("review_queued", "校验任务已进入共享调度器。", unit_id)

        self._active_unit_ids.add(unit_id)
        self.state["run"].setdefault("unit_ids", [])
        if unit_id not in self.state["run"]["unit_ids"]:
            self.state["run"]["unit_ids"].append(unit_id)
        self._active_task_meta[unit_id] = {
            "mode": mode,
            "translation_revision": unit.get("translation_revision"),
            "run_id": run_id,
        }
        executor = self._ensure_executor_locked()
        try:
            future = executor.submit(self._run_unit, unit_id, mode, run_id)
            self._active_futures[unit_id] = future
            future.add_done_callback(
                lambda completed, unit_id=unit_id, run_id=run_id: self._task_finished(
                    unit_id,
                    run_id,
                    completed,
                )
            )
        except Exception as exc:
            self._active_unit_ids.discard(unit_id)
            self._active_futures.pop(unit_id, None)
            self._active_task_meta.pop(unit_id, None)
            if previous_revision is not None:
                unit["translation_revision"] = previous_revision
            self._mark_failure_locked(unit_id, "scheduler_error", f"任务入队失败：{exc}")
            raise PipelineError(f"任务入队失败：{exc}") from exc

    def _start_job_locked(self, unit_ids: list[str], mode: str) -> dict[str, Any]:
        self._ensure_open_locked()
        unit_ids = list(dict.fromkeys(str(unit_id) for unit_id in unit_ids))
        if not unit_ids:
            return self._snapshot_locked()
        for unit_id in unit_ids:
            unit = self._find_unit_locked(unit_id)
            if unit_id in self._active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            if mode == "translation" and unit.get("status") not in CANCELLABLE_START_STATUSES:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新翻译。")
            if mode == "review" and unit.get("status") not in {"reviewing", *REVIEWABLE_TRANSLATION_STATUSES}:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新校验。")
        self._invalidate_output_locked()
        was_running = bool(self.state["run"].get("running"))
        run_id = self._begin_run_locked(len(unit_ids), mode)
        # Freeze the scope before submitting any Future.  A completion callback
        # must never be able to finish a run while the remaining units are
        # still being submitted.
        if not was_running:
            self.state["run"]["unit_ids"] = list(unit_ids)
            self.state["run"]["completed_unit_ids"] = []
        else:
            existing_unit_ids = self.state["run"].setdefault("unit_ids", [])
            for unit_id in unit_ids:
                if unit_id not in existing_unit_ids:
                    existing_unit_ids.append(unit_id)
        for unit_id in unit_ids:
            self._queue_unit_locked(unit_id, mode, run_id)
        self._save_locked()
        return self._snapshot_locked()

    def start(self, unit_ids: list[str] | None = None) -> dict[str, Any]:
        with self.lock:
            self._ensure_open_locked()
            if self.state["run"].get("running"):
                raise ConflictError("当前流水线仍在运行，请等待结束或先停止。")
            if unit_ids is None:
                selected_ids = [
                    unit["id"]
                    for unit in self.state["units"]
                    if unit.get("status") in CANCELLABLE_START_STATUSES
                    and unit["id"] not in self._active_unit_ids
                ]
            else:
                selected_ids = list(dict.fromkeys(str(unit_id) for unit_id in unit_ids))
                if not selected_ids:
                    raise PipelineError("请至少选择一个可翻译的处理单元。")
            return self._start_job_locked(selected_ids, "translation")

    def stop(self) -> dict[str, Any]:
        """Request a cooperative stop for the current run.

        Provider calls already in flight are allowed to return, but their
        results are discarded and no following pipeline stage is started.
        """
        with self.lock:
            self._ensure_open_locked()
            run = self.state["run"]
            if not run.get("running"):
                return self._snapshot_locked()
            if not run.get("cancel_requested"):
                run["cancel_requested"] = True
                run["status"] = "stopping"
                run["stop_requested_at"] = now_iso()
                run_id = str(run.get("run_id") or "")
                cancel_event = self._run_cancel_events.get(run_id)
                if cancel_event is not None:
                    cancel_event.set()
                self._event_locked("run_stop_requested", "用户请求停止当前流水线。")
                timer = threading.Timer(
                    STOP_GRACE_SECONDS,
                    self._force_finish_stopping_run,
                    args=(run_id,),
                )
                timer.daemon = True
                self._stop_timers[run_id] = timer
                timer.start()

            # Mark every active unit first. This prevents a provider callback
            # from committing a result while the stop request is being handled.
            for unit_id in list(run.get("unit_ids") or []):
                if unit_id not in self._active_unit_ids:
                    continue
                unit = self._find_unit_locked(unit_id)
                task_meta = self._active_task_meta.get(unit_id) or {}
                if (
                    task_meta.get("mode") == "translation"
                    and unit.get("status") in TRANSLATION_PROCESSING_STATUSES
                    and unit.get("translation_revision") == task_meta.get("translation_revision")
                ):
                    unit["translation_revision"] = max(0, int(unit["translation_revision"]) - 1)
                if unit.get("status") in PROCESSING_STATUSES:
                    self._mark_cancelled_locked(unit_id)

            # Futures which have not started never enter provider code.
            for future in list(self._active_futures.values()):
                future.cancel()

            self._recompute_stats_locked()
            self._finish_run_if_idle_locked(str(run.get("run_id") or ""))
            self._save_locked()
            return self._snapshot_locked()

    def _run_unit(self, unit_id: str, mode: str, run_id: str) -> None:
        with self.lock:
            if self._cancel_requested_locked(run_id):
                self._mark_cancelled_locked(unit_id)
                self._save_locked()
                return
        if mode == "translation":
            self._translate_and_review_unit(unit_id, run_id)
        else:
            self._review_unit(unit_id, run_id)

    def _task_finished(self, unit_id: str, run_id: str, future: Future[Any]) -> None:
        with self.lock:
            self._active_unit_ids.discard(unit_id)
            self._active_futures.pop(unit_id, None)
            self._active_task_meta.pop(unit_id, None)
            run = self.state.get("run") or {}
            if run.get("run_id") == run_id:
                completed_unit_ids = run.setdefault("completed_unit_ids", [])
                if unit_id not in completed_unit_ids:
                    completed_unit_ids.append(unit_id)
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - final safety net
                try:
                    unit = self._find_unit_locked(unit_id)
                except PipelineError:
                    unit = None
                if unit is not None and unit.get("status") in PROCESSING_STATUSES:
                    self._mark_failure_locked(unit_id, "worker_error", f"工作器异常：{exc}")

            self._recompute_stats_locked()
            self._finish_run_if_idle_locked(run_id)
            current_run = self.state.get("run") or {}
            if (
                not self._run_has_active_tasks_locked(run_id)
                and (
                    current_run.get("run_id") != run_id
                    or not current_run.get("running")
                )
            ):
                self._cancel_stop_timer_locked(run_id)
                self._run_cancel_events.pop(run_id, None)
                self._shutdown_retired_executor_locked(run_id)
            self._save_locked()

    def _derived_run_status_locked(self) -> str:
        stats = self.state.get("stats") or {}
        if stats.get("needs_action", 0):
            return "needs_action"
        if stats.get("pending", 0) or stats.get("waiting", 0) or stats.get("active", 0):
            return "ready"
        if stats.get("cancelled", 0):
            return "ready"
        if stats.get("total", 0) and stats.get("done", 0) == stats.get("total", 0):
            return "completed"
        return "ready"

    def _request_for_unit_locked(self, unit: dict[str, Any]) -> tuple[TranslationRequest, dict[str, Any] | None]:
        return self._unit_requests.translation_locked(unit)

    def _review_request_for_unit_locked(
        self,
        unit: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> ReviewRequest:
        return self._unit_requests.review_locked(unit, snapshot)

    def _mark_failure_locked(self, unit_id: str, rule: str, message: str) -> None:
        unit = self._find_unit_locked(unit_id)
        unit_state.ensure_unit_feedback_fields(unit)
        unit["status"] = "needs_action"
        unit["last_error"] = message
        unit["review_suggestions"] = []
        unit["review_issues"] = [
            {
                "rule": rule,
                "severity": "error",
                "block_id": unit_id,
                "message": message,
                "evidence": {},
            }
        ]
        unit["review"] = {
            "verdict": "FAIL",
            "issues": unit["review_issues"],
            "metrics": {},
            "provider": "controller",
            "model": "strict-import-gate",
            "translation_revision": unit["translation_revision"],
            "at": now_iso(),
        }
        unit["updated_at"] = now_iso()
        self._event_locked("unit_failed", message, unit_id, rule=rule)

    # --- bounded model-repair invocation bookkeeping -----------------------
    #
    # The provider owns the message history; the controller only publishes the
    # per-execution summary and guards against stale notifications.  Nothing
    # here is shared between units, projects, translation and review.

    MODEL_REPAIR_KINDS = ("translation", "review")

    @classmethod
    def _repair_summary_payload(
        cls,
        *,
        invocation_id: str,
        status: str,
        round_no: Any,
        max_rounds: Any,
        api_calls: Any,
        success_round: Any,
        errors: Any,
        source_sha256: str | None = None,
        translation_revision: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "invocation_id": str(invocation_id or ""),
            "status": status,
            "round": max(0, int(round_no or 0)),
            "max_rounds": max(0, int(max_rounds or 0)),
            "api_calls": max(0, int(api_calls or 0)),
            "success_round": success_round if status == "succeeded" else None,
            "errors": unit_state.normalize_repair_errors(errors),
        }
        if source_sha256:
            payload["source_sha256"] = str(source_sha256)
        if isinstance(translation_revision, int) and not isinstance(translation_revision, bool):
            payload["translation_revision"] = translation_revision
        return payload

    def _begin_invocation_locked(self, unit_id: str, kind: str) -> str:
        invocation_id = uuid.uuid4().hex
        self._active_invocations[(str(unit_id), kind)] = invocation_id
        return invocation_id

    def _end_invocation_locked(self, unit_id: str, kind: str, invocation_id: str) -> None:
        key = (str(unit_id), kind)
        if self._active_invocations.get(key) == invocation_id:
            self._active_invocations.pop(key, None)

    def _invocation_is_current_locked(self, unit_id: str, kind: str, invocation_id: str) -> bool:
        return self._active_invocations.get((str(unit_id), kind)) == invocation_id

    def _repair_control_locked(
        self,
        unit_id: str,
        kind: str,
        invocation_id: str,
        run_id: str,
        *,
        source_sha256: str,
        translation_revision: int | None = None,
    ) -> RepairControl:
        """Bind one execution's hook; every check re-reads live state under the lock."""

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.lock:
                unit = self._find_unit_locked(unit_id)
                if self._cancel_requested_locked(run_id):
                    raise PipelineError("已取消，不再发起下一轮模型修正。")
                if not self._invocation_is_current_locked(unit_id, kind, invocation_id):
                    raise PipelineError("该次执行已被更新的执行取代，不再发起下一轮模型修正。")
                if unit.get("source_sha256") != source_sha256:
                    raise PipelineError("源文已变化，不再发起下一轮模型修正。")
                if translation_revision is not None and unit.get("translation_revision") != translation_revision:
                    raise PipelineError("译文版本已变化，不再发起下一轮模型修正。")

        def on_progress(progress: RepairProgress) -> None:
            with self.lock:
                self._apply_repair_progress_locked(
                    unit_id,
                    kind,
                    invocation_id,
                    run_id,
                    source_sha256=source_sha256,
                    translation_revision=translation_revision,
                    progress=progress,
                )

        return RepairControl(
            invocation_id=invocation_id,
            kind=kind,
            before_attempt=before_attempt,
            on_progress=on_progress,
        )

    def _apply_repair_progress_locked(
        self,
        unit_id: str,
        kind: str,
        invocation_id: str,
        run_id: str,
        *,
        source_sha256: str,
        translation_revision: int | None,
        progress: RepairProgress,
    ) -> None:
        """Publish an in-flight round only when it still belongs to this execution."""
        if progress.kind != kind or progress.invocation_id != invocation_id:
            return
        if not self._invocation_is_current_locked(unit_id, kind, invocation_id):
            return
        if self._cancel_requested_locked(run_id):
            return
        unit = self._find_unit_locked(unit_id)
        if unit.get("source_sha256") != source_sha256:
            return
        if translation_revision is not None and unit.get("translation_revision") != translation_revision:
            return
        # In-memory only: a transient round must never be persisted, so a restart
        # cannot display it as still running.
        unit.setdefault("model_repair", {})[kind] = self._repair_summary_payload(
            invocation_id=invocation_id,
            status=progress.status if progress.status in {"running", "repairing"} else "failed",
            round_no=progress.round,
            max_rounds=progress.max_rounds,
            api_calls=progress.api_calls,
            success_round=None,
            errors=progress.errors,
            source_sha256=source_sha256,
            translation_revision=translation_revision,
        )

    def _repair_failure_summary_locked(
        self,
        unit_id: str,
        kind: str,
        invocation_id: str,
        error: BaseException,
        *,
        source_sha256: str,
        translation_revision: int | None,
        status: str = "failed",
    ) -> None:
        """Record a terminal failure without leaking raw exception text."""
        unit = self._find_unit_locked(unit_id)
        existing = unit.setdefault("model_repair", {}).get(kind)
        outcome = getattr(error, "outcome", None)
        if outcome is not None:
            payload = self._repair_summary_payload(
                invocation_id=invocation_id,
                status=status,
                round_no=getattr(outcome, "round", 0),
                max_rounds=getattr(outcome, "max_rounds", 0),
                api_calls=getattr(outcome, "api_calls", 0),
                success_round=None,
                errors=list(getattr(outcome, "errors", ()) or ()),
                source_sha256=source_sha256,
                translation_revision=translation_revision,
            )
        else:
            saved = existing if isinstance(existing, dict) else {}
            payload = self._repair_summary_payload(
                invocation_id=invocation_id,
                status=status,
                round_no=saved.get("round", 0),
                max_rounds=saved.get("max_rounds", 0),
                api_calls=saved.get("api_calls", 0),
                success_round=None,
                errors=[
                    {
                        "code": "provider_error" if status == "failed" else "cancelled",
                        "location": "request",
                        "detail": "模型调用未完成，未进行内容修正。"
                        if status == "failed"
                        else "该次模型执行已被取消。",
                    }
                ],
                source_sha256=source_sha256,
                translation_revision=translation_revision,
            )
        unit["model_repair"][kind] = payload

    def _restore_unit_commit_locked(
        self,
        unit_id: str,
        unit_snapshot: dict[str, Any],
        events_snapshot: list[Any],
    ) -> None:
        """Roll one failed result commit back to its pre-commit in-memory state.

        A model result is not business state until it is on disk.  Without this,
        a failed save would leave the half-committed unit (and its success
        event) in memory for the next save to publish, even though the user was
        told the commit failed.  Only the two result-commit paths use this; it
        is not a storage layer or a general transaction mechanism.
        """
        unit = self._find_unit_locked(unit_id)
        unit.clear()
        unit.update(unit_snapshot)
        self.state["events"] = list(events_snapshot)

    def _repair_summary_mark_save_failed_locked(
        self,
        unit: dict[str, Any],
        kind: str,
        repair: Any = None,
    ) -> None:
        """A failed save must never leave a success claim behind.

        ``repair`` is the completed model summary when the caller still holds
        it; otherwise the entry already on the unit is downgraded.
        """
        existing = (unit.get("model_repair") or {}).get(kind)
        saved = existing if isinstance(existing, dict) else {}
        data = repair if isinstance(repair, dict) else saved
        errors = list(data.get("errors") or saved.get("errors") or [])
        errors.append(
            {
                "code": "save_failed",
                "location": "commit",
                "detail": "结果落盘失败，未记录为成功。",
            }
        )
        unit.setdefault("model_repair", {})[kind] = self._repair_summary_payload(
            invocation_id=data.get("invocation_id") or saved.get("invocation_id") or "",
            status="failed",
            round_no=data.get("round", saved.get("round")),
            max_rounds=data.get("max_rounds", saved.get("max_rounds")),
            api_calls=data.get("api_calls", saved.get("api_calls")),
            success_round=None,
            errors=errors,
            source_sha256=unit.get("source_sha256"),
            translation_revision=unit.get("translation_revision")
            if kind == "review"
            else None,
        )

    def _repair_terminal_summary_locked(
        self,
        unit: dict[str, Any],
        kind: str,
        repair: Any,
        *,
        status: str,
    ) -> None:
        """Attach a terminal summary inside the same commit as the result."""
        data = repair if isinstance(repair, dict) else {}
        unit.setdefault("model_repair", {})[kind] = self._repair_summary_payload(
            invocation_id=data.get("invocation_id") or "",
            status=status,
            round_no=data.get("round"),
            max_rounds=data.get("max_rounds"),
            api_calls=data.get("api_calls"),
            success_round=data.get("success_round"),
            errors=data.get("errors"),
            source_sha256=unit.get("source_sha256"),
            translation_revision=unit.get("translation_revision")
            if kind == "review"
            else None,
        )

    def _translate_and_review_unit(self, unit_id: str, _run_id: str) -> None:
        with self.lock:
            unit = self._find_unit_locked(unit_id)
            if unit.get("status") != "waiting_translation":
                return
            if self._cancel_requested_locked(_run_id):
                self._mark_cancelled_locked(unit_id)
                self._save_locked()
                return
            unit["status"] = "translating"
            unit["updated_at"] = now_iso()
            request, snapshot = self._request_for_unit_locked(unit)
            source_sha256 = unit["source_sha256"]
            invocation_id = self._begin_invocation_locked(unit_id, "translation")
            request = replace(
                request,
                control=self._repair_control_locked(
                    unit_id,
                    "translation",
                    invocation_id,
                    _run_id,
                    source_sha256=source_sha256,
                ),
            )
            self._event_locked("translation_started", "翻译端已接收单元。", unit_id)
            self._save_locked()
        try:
            translator, _reviewer = self._provider_pair()
            result = translator.translate(request)
            with self.lock:
                if self._cancel_requested_locked(_run_id):
                    self._end_invocation_locked(unit_id, "translation", invocation_id)
                    self._mark_cancelled_locked(unit_id)
                    self._save_locked()
                    return
            unit_validation.validate_translation_result(unit, result)
        except Exception as exc:
            with self.lock:
                self._end_invocation_locked(unit_id, "translation", invocation_id)
                cancelled = self._cancel_requested_locked(_run_id)
                if cancelled:
                    self._mark_cancelled_locked(unit_id)
                else:
                    self._mark_failure_locked(unit_id, "translation_error", str(exc))
                self._repair_failure_summary_locked(
                    unit_id,
                    "translation",
                    invocation_id,
                    exc,
                    source_sha256=source_sha256,
                    translation_revision=None,
                    status="cancelled" if cancelled else "failed",
                )
                self._save_locked()
            return

        with self.lock:
            unit = self._find_unit_locked(unit_id)
            self._end_invocation_locked(unit_id, "translation", invocation_id)
            if self._cancel_requested_locked(_run_id):
                self._mark_cancelled_locked(unit_id)
                self._save_locked()
                return
            commit_snapshot = copy.deepcopy(unit)
            events_snapshot = list(self.state.get("events") or [])
            unit["translation"] = result.translated_text
            # Feedback is one-shot: a successful translation consumes it before review.
            unit["pending_translation_feedback"] = None
            # The new AI result supersedes any saved manual reference.  Keep
            # this mutation in the same commit as the translation so failures
            # and cancellations leave the old reference intact.
            unit["user_edited_translation"] = None
            unit["translation_provider"] = result.provider
            unit["translation_model"] = result.model
            unit["usage"] = result.usage
            unit["status"] = "waiting_review"
            unit["last_error"] = None
            unit["updated_at"] = now_iso()
            # The snapshot is bound to the revision this translation created.
            unit_requests.store_reference(unit, snapshot, kind="translation", clock=_unit_request_clock)
            if result.repair is not None:
                # Same protected commit as the translation itself: success is
                # never recorded before the result is safely stored.
                self._repair_terminal_summary_locked(
                    unit, "translation", result.repair, status="succeeded"
                )
            self._event_locked("translation_imported", "翻译结果已通过严格导入，进入独立校验。", unit_id)
            try:
                self._save_locked()
            except Exception as exc:
                # The result was never stored: roll the unit back to its
                # pre-commit business state (previous translation, manual
                # reference and quality reference) and report the commit
                # failure through the existing failure path.  No model re-call.
                self._restore_unit_commit_locked(unit_id, commit_snapshot, events_snapshot)
                self._mark_failure_locked(unit_id, "save_error", f"翻译结果保存失败：{exc}")
                self._repair_summary_mark_save_failed_locked(unit, "translation", result.repair)
                return
        self._review_unit(unit_id, _run_id, reference=snapshot)

    def _review_unit(
        self,
        unit_id: str,
        _run_id: str,
        reference: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            unit = self._find_unit_locked(unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in {"waiting_review", "needs_action"}:
                return
            if self._cancel_requested_locked(_run_id):
                self._mark_cancelled_locked(unit_id)
                self._save_locked()
                return
            if not unit.get("translation"):
                self._mark_failure_locked(unit_id, "empty_translation", "没有可供校验的译文。")
                self._save_locked()
                return
            unit["status"] = "reviewing"
            unit["updated_at"] = now_iso()
            request = self._review_request_for_unit_locked(unit, reference)
            review_revision = unit["translation_revision"]
            source_sha256 = unit["source_sha256"]
            invocation_id = self._begin_invocation_locked(unit_id, "review")
            request = replace(
                request,
                control=self._repair_control_locked(
                    unit_id,
                    "review",
                    invocation_id,
                    _run_id,
                    source_sha256=source_sha256,
                    translation_revision=review_revision,
                ),
            )
            self._event_locked("review_started", "独立校验端已接收译文。", unit_id)
            self._save_locked()
        try:
            _translator, reviewer = self._provider_pair()
            result = reviewer.review(request)
            with self.lock:
                if self._cancel_requested_locked(_run_id):
                    self._end_invocation_locked(unit_id, "review", invocation_id)
                    self._mark_cancelled_locked(unit_id)
                    self._save_locked()
                    return
                current = self._find_unit_locked(unit_id)
                if current.get("translation_revision") != review_revision:
                    raise PipelineError("校验结果对应的译文版本已经变化，已拒绝导入。")
                unit_validation.validate_review_result(current, result)
        except Exception as exc:
            with self.lock:
                self._end_invocation_locked(unit_id, "review", invocation_id)
                cancelled = self._cancel_requested_locked(_run_id)
                if cancelled:
                    self._mark_cancelled_locked(unit_id)
                else:
                    self._mark_failure_locked(unit_id, "review_error", str(exc))
                self._repair_failure_summary_locked(
                    unit_id,
                    "review",
                    invocation_id,
                    exc,
                    source_sha256=source_sha256,
                    translation_revision=review_revision,
                    status="cancelled" if cancelled else "failed",
                )
                self._save_locked()
            return

        with self.lock:
            unit = self._find_unit_locked(unit_id)
            self._end_invocation_locked(unit_id, "review", invocation_id)
            if self._cancel_requested_locked(_run_id):
                self._mark_cancelled_locked(unit_id)
                self._save_locked()
                return
            commit_snapshot = copy.deepcopy(unit)
            events_snapshot = list(self.state.get("events") or [])
            review_suggestions = unit_state.extract_review_suggestions(result.issues)
            unit["review_attempts"] += 1
            unit["review"] = {
                "verdict": result.verdict,
                "issues": result.issues,
                "metrics": result.metrics,
                "provider": result.provider,
                "model": result.model,
                "translation_revision": unit["translation_revision"],
                "at": now_iso(),
            }
            unit["review_issues"] = result.issues
            unit["review_suggestions"] = review_suggestions
            unit["last_error"] = None if result.verdict == "PASS" else "独立校验未通过，等待用户裁决。"
            unit["status"] = "passed" if result.verdict == "PASS" else "needs_action"
            unit["updated_at"] = now_iso()
            if result.repair is not None:
                # A valid FAIL still completes the model execution normally; the
                # verdict is never rewritten to PASS by the repair loop.
                self._repair_terminal_summary_locked(
                    unit, "review", result.repair, status="succeeded"
                )
            if result.verdict == "PASS":
                self._event_locked("review_passed", "独立校验通过。", unit_id)
            else:
                self._event_locked("review_failed", "独立校验未通过，已交给用户处理。", unit_id)
            try:
                self._save_locked()
            except Exception as exc:
                # Same rule as the translation commit: an unstored verdict —
                # including a valid PASS — is not a result.  Roll back and
                # report through the existing failure path instead of leaving
                # a "passed" unit that the next save would publish.
                self._restore_unit_commit_locked(unit_id, commit_snapshot, events_snapshot)
                self._mark_failure_locked(unit_id, "save_error", f"校验结果保存失败：{exc}")
                self._repair_summary_mark_save_failed_locked(unit, "review", result.repair)
                return

    @staticmethod
    def _validate_expected_revision(value: Any | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PipelineError("译文版本号无效，请刷新后重试。")
        return value

    def _validate_unit_write_guard_locked(
        self,
        unit: dict[str, Any],
        *,
        expected_source_sha256: str | None,
        expected_translation_revision: int | None,
    ) -> None:
        if expected_source_sha256 is not None:
            if not isinstance(expected_source_sha256, str) or not expected_source_sha256.strip():
                raise PipelineError("源文哈希无效，请刷新后再提交。")
            if expected_source_sha256 != unit["source_sha256"]:
                raise PipelineError("源文已经变化，请刷新后再提交。")
        expected_revision = self._validate_expected_revision(expected_translation_revision)
        if expected_revision is not None and expected_revision != unit["translation_revision"]:
            raise ConflictError("译文版本已经变化，请刷新后再提交。")

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
            if unit_id in self._active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = self._find_unit_locked(unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许编辑正式译文。")
            self._validate_unit_write_guard_locked(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            self._invalidate_output_locked()
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
            self._recompute_stats_locked()
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
            if unit_id in self._active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = self._find_unit_locked(unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in REVIEWABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新校验。")
            if not isinstance(unit.get("translation"), str) or not unit["translation"].strip():
                raise PipelineError("没有可供校验的正式译文。")
            self._validate_unit_write_guard_locked(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            self._start_job_locked([unit_id], "review")
            return copy.deepcopy(self._find_unit_locked(unit_id))

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
            if unit_id in self._active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = self._find_unit_locked(unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新翻译。")
            self._validate_unit_write_guard_locked(
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
            self._start_job_locked([unit_id], "translation")
            return copy.deepcopy(self._find_unit_locked(unit_id))

    # ------------------------------------------------------------------
    # Quality support: concept cards, bounded scans, editorial suggestions.
    # ------------------------------------------------------------------

    _quality_unit_sources = staticmethod(quality_unit_sources)

    def _validate_expected_project_id_locked(
        self,
        expected_project_id: str | None,
    ) -> None:
        """Reject a stale page binding while holding the manager lock."""
        project_state.validate_expected_project_id(self._project_state, expected_project_id)

    def _quality_support_read(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
        """Read the optional feature state without ever writing it back.

        A legacy project without ``quality_support`` is simply empty. Opening
        or reading a project must not materialize an empty feature object.
        """
        with self.lock:
            self._validate_expected_project_id_locked(expected_project_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            sources = self._quality_unit_sources(self.state.get("units") or [])
            return support, sources

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

        decisions = concept_automation.current_decisions(support)
        cards = support.get("cards") or {}
        rows: list[dict[str, Any]] = []
        for unit in self.state.get("units") or []:
            if not isinstance(unit, dict) or not str(unit.get("translation") or "").strip():
                continue
            reference = unit.get("quality_reference")
            if not isinstance(reference, Mapping):
                continue
            seen: dict[str, dict[str, Any]] = {}
            for kind in ("translation", "review"):
                entry = reference.get(kind)
                snapshot = entry.get("snapshot") if isinstance(entry, Mapping) else None
                cards_in_snapshot = (snapshot or {}).get("cards") if isinstance(snapshot, Mapping) else None
                for card in cards_in_snapshot or []:
                    if not isinstance(card, Mapping) or str(card.get("origin") or "") != "automatic":
                        continue
                    card_id = str(card.get("card_id") or "")
                    live = cards.get(card_id)
                    decision = decisions.get(card_id)
                    frozen_revision = int(card.get("card_revision") or 0)
                    if not isinstance(live, dict):
                        reason = "参考卡已经不存在。"
                    elif not isinstance(decision, Mapping) or str(decision.get("verdict") or "") != "adopt":
                        reason = "自动采用决定已经失效或被撤销。"
                    elif concept_automation.is_manual_protected(live):
                        # The most actionable reason wins: a human already owns
                        # this card, so the automatic reference is never
                        # injected again whatever else also changed.
                        reason = "该卡已有人工决定或批准内容，自动参考不再注入。"
                    elif int(decision.get("content_revision") or 0) != frozen_revision:
                        reason = (
                            f"参考内容已经更新（当时用的是第 {frozen_revision} 版，"
                            f"现在是第 {int(decision.get('content_revision') or 0)} 版）。"
                        )
                    else:
                        problems = concept_automation.decision_problems(
                            decision, live, unit_sources=unit_sources
                        )
                        reason = "；".join(problems[:2])
                    if not reason or card_id in seen:
                        continue
                    seen[card_id] = {
                        "unit_id": str(unit.get("id") or ""),
                        "card_id": card_id,
                        "decision_id": str(card.get("decision_id") or ""),
                        "frozen_card_revision": int(card.get("card_revision") or 0),
                        "translation_revision": int(unit.get("translation_revision") or 0),
                        "reason": reason,
                    }
            rows.extend(seen.values())
        return sorted(rows, key=lambda row: (row["unit_id"], row["card_id"]))

    def quality_support(
        self,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only view of cards, versions and scan coverage."""
        support, sources = self._quality_support_read(
            expected_project_id=expected_project_id
        )
        cards = sorted(support.get("cards", {}).values(), key=lambda card: card["id"])
        batches = support.get("batches") or []
        # Coverage comes from the persistent scanned_unit_ids set, not from the
        # 40-entry batch display history.
        covered = list(scanned_unit_ids(support))
        counts = summarize_counts(support)
        automation = concept_automation.normalize_automation(support.get("automation"))
        prepare_record = automation.get("prepare")
        reference_mode = concept_automation.reference_mode(self.state.get("project"))
        with self.lock:
            audit_units = copy.deepcopy([
                unit for unit in (self.state.get("units") or []) if isinstance(unit, dict)
            ])
        term_mismatch_rows: list[dict[str, Any]] = []
        term_conflict_rows: list[dict[str, Any]] = []
        for unit in audit_units:
            unit_id = str(unit.get("id") or "")
            source_text = str(unit.get("source") or "")
            if not unit_id or not source_text:
                continue
            candidates = select_reference_candidates(
                support, unit_id=unit_id, unit_sources=sources, mode=reference_mode
            )
            projection = terminology_rules(candidates, source_text)
            term_conflict_rows.extend(
                {"unit_id": unit_id, **row} for row in projection["conflicts"]
            )
            translation = str(unit.get("translation") or "")
            if translation.strip():
                term_mismatch_rows.extend(
                    {"unit_id": unit_id, "status": str(unit.get("status") or ""), **row}
                    for row in terminology_mismatches(projection["rules"], translation)
                )
        # The automatic channel is what makes a card usable, so the view has to
        # show it next to the manual one. The projection is read-only and uses
        # the injection predicates: ``approved`` still means a human approval
        # and stays untouched, while an automatic decision is reported as an
        # automatic decision only.
        automatic_view = concept_automation.automatic_reference_view(
            support, unit_sources=sources, mode=reference_mode
        )
        cards = [
            {**card, "automatic": automatic_view.get(str(card.get("id") or ""))}
            for card in cards
        ]
        # Failed-batch recovery, derived read-only: which batches the operator
        # may hand-retry right now, and why the others may not be. Reading never
        # migrates or writes; the retry entry point re-checks the same rules
        # under the lock before anything is frozen.
        current_prepare_id = ""
        if isinstance(prepare_record, Mapping):
            current_prepare_id = str(prepare_record.get("prepare_id") or "")
        retryable_batches = [
            batch_retry_descriptor(
                support,
                batch,
                unit_sources=sources,
                mode=reference_mode,
                current_prepare_id=current_prepare_id,
            )
            for batch in (support.get("batches") or [])
            if isinstance(batch, Mapping)
        ]
        return {
            "schema_version": support["schema_version"],
            "revision": support["revision"],
            "approved_version": support["approved_version"],
            "reference_mode": reference_mode,
            "retryable_batches": retryable_batches,
            "reference_revision": int(automation.get("reference_revision") or 0),
            "prepare": (
                self._prepare_summary_from_record(prepare_record)
                if isinstance(prepare_record, Mapping)
                else None
            ),
            "automatic_decisions": len(automation.get("decisions") or {}),
            "stale_reference_units": self.stale_reference_units_locked(support, sources),
            "terminology_audit": {
                "mismatches": term_mismatch_rows,
                "conflicts": term_conflict_rows,
            },
            "counts": counts,
            "cards": cards,
            "batches": batches,
            "covered_unit_ids": covered,
            "scanned_unit_ids": covered,
            "prompt_version": "quality-support-v1",
            # No scan caps are reported: batches are split by the submitted word
            # target alone and a plan can be terminated from the page at any time.
            "limits": {"default_scan_source_words": DEFAULT_SCAN_SOURCE_WORDS},
        }

    def _quality_scan_units_locked(
        self,
        *,
        scope: str,
        unit_ids: list[str] | None,
        current_unit_id: str | None,
    ) -> list[dict[str, Any]]:
        units = [unit for unit in (self.state.get("units") or []) if isinstance(unit, dict)]
        if scope == "current":
            if not current_unit_id:
                raise PipelineError("请先选择一个单元，再从当前单元提取概念。")
            return [unit for unit in units if unit.get("id") == current_unit_id]
        if scope == "selected":
            if not unit_ids:
                raise PipelineError("请先在单元列表中选择要扫描的单元。")
            wanted = set(unit_ids)
            return [unit for unit in units if unit.get("id") in wanted]
        covered = scanned_unit_ids(normalize_quality_support(self.state.get("quality_support")))
        remaining = [unit for unit in units if str(unit.get("id")) not in covered]
        if not remaining:
            raise PipelineError("所有单元都已经被扫描过，无需继续。")
        return remaining

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
        scope = str(scope or "selected").strip().casefold()
        if scope not in QUALITY_SCAN_SCOPES:
            raise PipelineError("扫描范围只能是 current、selected 或 continue。")
        effective_parallel_batches = 1 if max_parallel_batches is None else int(max_parallel_batches)
        effective_source_words = (
            DEFAULT_SCAN_SOURCE_WORDS if max_source_words is None else int(max_source_words)
        )
        if effective_parallel_batches < 1:
            raise PipelineError("概念扫描并行批数至少为 1。")
        if effective_source_words < 100:
            raise PipelineError("每批源文词数至少为 100。")
        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            targets = self._quality_scan_units_locked(
                scope=scope, unit_ids=unit_ids, current_unit_id=current_unit_id
            )
            if not targets:
                raise PipelineError("没有可扫描的单元。")
            support = normalize_quality_support(self.state.get("quality_support"))
            approved_expressions = self._approved_expressions(support)
        plan = planned_batches(
            targets,
            max_source_words=effective_source_words,
            word_counter=mode2_common.english_word_count,
        )
        planned_units = [unit_id for batch in plan["batches"] for unit_id in batch["unit_ids"]]
        return {
            "scope": scope,
            "unit_count": len(targets),
            "planned_unit_ids": planned_units,
            # Explicit server-side slices with stable batch ids. The frontend
            # must consume these instead of guessing its own unit slices.
            "batches": plan["batches"],
            "batch_count": plan["batch_count"],
            "max_parallel_batches": effective_parallel_batches,
            "max_source_words": effective_source_words,
            "max_requests": plan["max_requests"],
            "unscannable_units": plan["unscannable_units"],
            "remaining_unit_ids": plan["remaining_unit_ids"],
            "approved_expression_count": len(approved_expressions),
            "note": "AI 将提出候选并检查依据，人工批准后才用于翻译。",
        }

    @staticmethod
    def _resolve_parallel_batches(value: Any) -> int:
        """How many generation+check batches the confirmed execution may run at once.

        This is a *worker* count for one confirmed run, frozen into the preview
        like the batch size — never a server-wide queue or a rate limiter. The
        caller may bound it further by the number of batches it really has;
        ``None`` keeps the historical one-batch-at-a-time behaviour.
        """

        if value is None:
            return 1
        if isinstance(value, bool):
            raise PipelineError("并行批数必须是大于或等于 1 的整数。")
        try:
            parallel = int(value)
        except (TypeError, ValueError) as exc:
            raise PipelineError("并行批数必须是大于或等于 1 的整数。") from exc
        if parallel < 1:
            raise PipelineError("并行批数必须是大于或等于 1 的整数。")
        return parallel

    @staticmethod
    def _resolve_additional_work_limit(value: Any) -> int:
        """The extra logical request budget shared by every A-stage extra step.

        It is one pool for the whole confirmed execution — bounded lookups and
        large-group local judgments draw from the same number — never one pool
        per kind. ``None`` keeps the documented default.
        """

        if value is None:
            return DEFAULT_ADDITIONAL_WORK_LIMIT
        if isinstance(value, bool):
            raise PipelineError("额外请求预算必须是 0 到 10 之间的整数。")
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise PipelineError("额外请求预算必须是 0 到 10 之间的整数。") from exc
        if limit < 0 or limit > DEFAULT_ADDITIONAL_WORK_LIMIT:
            raise PipelineError(
                f"额外请求预算必须在 0 到 {DEFAULT_ADDITIONAL_WORK_LIMIT} 之间。"
            )
        return limit

    def _assessment_context_payload(
        self,
        candidate: Mapping[str, Any],
        *,
        unit_sources: Mapping[str, tuple[str, str]],
        model: str,
    ) -> dict[str, Any]:
        """The verification identity the program writes, never the model."""

        fingerprint = ""
        hashes: dict[str, str] = {}
        try:
            normalized = normalize_card_content(candidate, unit_sources=unit_sources)
        except Exception:  # pragma: no cover - a rejected candidate fails earlier
            normalized = {}
        if normalized:
            try:
                fingerprint = content_signature(normalized)
            except Exception:  # pragma: no cover - signature is total for mappings
                fingerprint = ""
            for item in normalized.get("evidence") or []:
                unit_id = str(item.get("unit_id") or "")
                if unit_id:
                    hashes[unit_id] = str(item.get("source_sha256") or "")
        return {
            "content_fingerprint": fingerprint,
            "source_hashes": hashes,
            "prompt_version": PROMPT_VERSION,
            # A non-secret identifier only: never a key or a credentialed URL.
            "model": str(model or ""),
        }

    @staticmethod
    def _batch_row_copy(
        support: Mapping[str, Any],
        batch_id: str,
    ) -> dict[str, Any] | None:
        """A detached copy of one stored batch row, or ``None`` when absent.

        Read-only callers use this so a later write to the snapshot cannot
        change what they already inspected. A writer that must mutate the row
        in place reads the live row itself.
        """

        return next(
            (
                dict(item)
                for item in (support.get("batches") or [])
                if str(item.get("batch_id")) == str(batch_id)
            ),
            None,
        )

    def _stored_check_payload(
        self,
        check: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        unit_sources: Mapping[str, tuple[str, str]],
        model: str,
    ) -> dict[str, Any]:
        """One model verdict plus the program-written verification identity.

        The structured question result travels with the check; dropping it here
        would silently turn a resolved question back into a block. The identity
        is always written by the program, never taken from the model.
        """

        payload = {
            "verdict": check["verdict"],
            "reasons": check["reasons"],
            "notes": check.get("notes") or "",
        }
        assessment = check.get("automation_assessment")
        if isinstance(assessment, dict):
            payload["automation_assessment"] = assessment
        payload["assessment_context"] = self._assessment_context_payload(
            candidate,
            unit_sources=unit_sources,
            model=model,
        )
        return payload

    def _quality_providers(self) -> tuple[Any, Any, Any, Any]:
        """The four concept channels: generation, check, editorial, resolution.

        The group-resolution channel is its own slot on purpose: the check
        provider protocol has no ``resolve_group``, so borrowing the check object
        only ever worked with the offline double. Both real and injected
        providers are returned here. Each channel resolves its own task preset
        (task choice first, otherwise its group's preset).
        """

        injected = (
            self.quality_generation_provider,
            self.quality_check_provider,
            self.quality_editorial_provider,
            self.quality_resolution_provider,
        )
        with self.lock:
            provider_name = str(self.state.get("config", {}).get("provider") or "demo")
        return provider_routing.resolve_quality_channels(
            provider_name,
            injected,
            self.api_settings,
            provider_routing.QualityFactories(
                OpenAICompatibleConceptGenerationProvider,
                OpenAICompatibleConceptCheckProvider,
                OpenAICompatibleEditorialSuggestionProvider,
                OpenAICompatibleConceptResolutionProvider,
                FakeQualityProvider,
            ),
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

    def _quality_batch_repair_control_locked(
        self,
        batch_id: str,
        signature: str,
        kind: str,
        *,
        frozen_cards: Mapping[str, Mapping[str, Any]] | None = None,
        frozen_units: Sequence[Mapping[str, Any]] | None = None,
        frozen_mode: str = "",
        prepare_id: str = "",
        progress_stage: str = "",
    ) -> RepairControl:
        """Authorize every concept repair request immediately before it is sent.

        A batch has no run to cancel, so the gate answers two questions: is this
        still the in-flight execution of a live project, and is the material the
        model is answering for still exactly what was frozen? A closed manager, a
        superseded batch, a changed source, a project that changed mode, or a card
        a human took over, edited or re-versioned stops the next round instead of
        paying for it. A refusal here is a conflict: a late result must not be
        written.
        """

        def verify() -> None:
            with self.lock:
                if self._closed:
                    raise ConflictError("项目已关闭，不再发起下一轮模型修正。")
                if self._quality_runtime.batch_inflight.get(batch_id) != signature:
                    raise ConflictError("该批次已被更新的执行取代，不再发起下一轮模型修正。")
                if frozen_mode and concept_automation.reference_mode(
                    self.state.get("project")
                ) != frozen_mode:
                    raise ConflictError("项目参考模式已经切换，本次批次结果不再适用。")
                if frozen_units is not None:
                    unit_sources = self._quality_unit_sources(self.state.get("units") or [])
                    for unit in frozen_units:
                        unit_id = str(unit.get("id") or "")
                        live = unit_sources.get(unit_id)
                        if live is None:
                            raise ConflictError(
                                f"单元 {unit_id} 已经不存在，本次批次结果不再适用。"
                            )
                        if str(live[1]) != str(unit.get("source_sha256") or "") or len(
                            str(live[0])
                        ) != len(str(unit.get("source") or "")):
                            raise ConflictError(
                                f"单元 {unit_id} 的源文已经变化，本次批次结果不再适用。"
                            )
                if frozen_cards is None:
                    return
                support = normalize_quality_support(self.state.get("quality_support"))
                for card_id, expected in frozen_cards.items():
                    card = (support.get("cards") or {}).get(card_id)
                    if not isinstance(card, Mapping):
                        raise ConflictError(f"卡片 {card_id} 已经不存在，本次批次结果不再适用。")
                    if str(card.get("status") or "") != "pending_review":
                        raise ConflictError(f"卡片 {card_id} 已被人工处理，本次批次结果不再适用。")
                    if concept_automation.is_manual_protected(card):
                        raise ConflictError(f"卡片 {card_id} 已被人工接管，本次批次结果不再适用。")
                    if int(card.get("draft_revision") or 0) != int(
                        expected.get("draft_revision") or 0
                    ) or concept_automation._content_fingerprint(card) != str(
                        expected.get("content_fingerprint") or ""
                    ):
                        raise ConflictError(f"卡片 {card_id} 的内容已经变化，本次批次结果不再适用。")

        return RepairControl(
            invocation_id=batch_id,
            kind=kind,
            before_attempt=lambda round_no, api_calls: verify(),
            on_progress=(
                self._quality_progress.repair_callback(prepare_id, progress_stage, batch_id)
                if prepare_id and progress_stage
                else None
            ),
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
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise PipelineError("缺少批次标识。")
        wanted = [str(unit_id) for unit_id in unit_ids or []]
        if not wanted:
            raise PipelineError("本批次没有指定单元。")

        retry_inputs: list[dict[str, Any]] | None = None
        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            if self._quality_runtime.retry_inflight and batch_id not in self._quality_runtime.retry_inflight:
                raise ConflictError(
                    f"批次 {sorted(self._quality_runtime.retry_inflight)[0]} 正在恢复中，请等待结束再扫描。"
                )
            units = {
                str(unit.get("id")): unit
                for unit in (self.state.get("units") or [])
                if isinstance(unit, dict)
            }
            selected: list[dict[str, Any]] = []
            for unit_id in wanted:
                unit = units.get(unit_id)
                if unit is None:
                    raise PipelineError(f"单元 {unit_id} 不存在。")
                selected.append(unit)
            signatures = [
                (unit["id"], unit["source_sha256"], len(str(unit.get("source") or "")))
                for unit in selected
            ]
            signature = repr(signatures)
            previous = self._quality_runtime.batch_inflight.get(batch_id)
            if previous == signature:
                raise ConflictError(f"批次 {batch_id} 正在处理中，请勿重复提交。")
            if previous is not None and previous != signature:
                raise ConflictError(f"批次 {batch_id} 的输入已经变化，请重新规划扫描。")
            support = normalize_quality_support(self.state.get("quality_support"))
            # Freeze the project's actual reference mode for the whole call.
            # ``mode`` is the scan/prepare execution lane; it is not a
            # substitute for the project's current reference-mode identity.
            frozen_mode = concept_automation.reference_mode(self.state.get("project"))
            stored_batch = self._batch_row_copy(support, batch_id)
            if stored_batch is not None:
                self._quality_runtime.batch_inflight.pop(batch_id, None)
                if [str(item) for item in stored_batch.get("unit_ids") or []] != wanted:
                    raise ConflictError(f"批次 {batch_id} 的单元与已保存记录不一致，请重新规划。")
                stored_stage = str((stored_batch.get("retry") or {}).get("stage") or "")
                stored_state = batch_retry_state(stored_batch)
                if stored_stage == "generation" and stored_state != "completed":
                    # The record only says "generation failed": nothing was ever
                    # saved, so this is the first real generation for this batch
                    # rather than a duplicate submit. Its units still get no scan
                    # coverage from the failed attempt.
                    self._quality_runtime.batch_inflight[batch_id] = signature
                    project_id = str(self.state.get("project", {}).get("id") or "")
                    prepare_record = concept_automation.automation_of(
                        normalize_quality_support(self.state.get("quality_support"))
                    ).get("prepare")
                    owning_prepare = (
                        str(prepare_record.get("prepare_id") or "")
                        if mode == "automatic" and isinstance(prepare_record, Mapping)
                        else ""
                    )
                    approved_expressions = self._approved_expressions(support)
                    retry_inputs = None
                else:
                    if str(stored_batch.get("check_status") or "") != "failed":
                        raise ConflictError(f"批次 {batch_id} 已经保存过，不要重复提交。")
                    # Do not call the checker while this entry lock is held.  The
                    # retry path is the same network boundary as first-time scan.
                    retry_inputs = copy.deepcopy(selected)
            else:
                self._quality_runtime.batch_inflight[batch_id] = signature
                project_id = str(self.state.get("project", {}).get("id") or "")
                # Which prepare owns this batch, captured *before* the model call
                # so a later failure is never attributed to a new generation.
                prepare_record = concept_automation.automation_of(
                    normalize_quality_support(self.state.get("quality_support"))
                ).get("prepare")
                owning_prepare = (
                    str(prepare_record.get("prepare_id") or "")
                    if mode == "automatic" and isinstance(prepare_record, Mapping)
                    else ""
                )
                approved_expressions = self._approved_expressions(support)

        if retry_inputs is not None:
            return self._retry_quality_check_locked(
                batch_id, retry_inputs, retry_close=retry_close
            )

        refs = self._concept_unit_refs(selected)
        generation, checker, _editorial, _resolution = self._quality_providers()
        # Counted at the call site, never inferred from the outcome: these are
        # the provider invocations this request really made. A provider that
        # reports its own transport usage adds the HTTP counts in ``repair``.
        generation_calls = 1
        progress_prepare_id = owning_prepare if mode == "automatic" else ""
        if progress_prepare_id:
            self._quality_progress.change(
                progress_prepare_id,
                "generation",
                batch_id,
                "start",
                unit="batch",
                label="生成候选",
                metadata={"batch_id": batch_id, "unit_count": len(selected)},
                provider_channel="generation",
            )
        try:
            scan_result = generation.generate_candidates(
                ConceptScanRequest(
                    project_id=project_id,
                    batch_id=batch_id,
                    units=refs,
                    approved_expressions=approved_expressions,
                    control=self._quality_batch_repair_control_locked(
                        batch_id,
                        signature,
                        "概念候选生成",
                        frozen_units=selected,
                        frozen_mode=frozen_mode,
                        prepare_id=progress_prepare_id,
                        progress_stage="generation",
                    ),
                )
            )
        except ContentRepairExhausted as exc:
            if progress_prepare_id:
                self._quality_progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error=str(exc),
                )
                self._quality_progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成失败，本批不进入独立检查",
                )
            with self.lock:
                self._quality_runtime.batch_inflight.pop(batch_id, None)
            # A first-run exhaustion still needs a durable generation retry
            # record. A hand retry already has its running record; its outer
            # recovery path closes that record with this concrete error.
            if retry_close is None:
                with self.lock:
                    self._record_failed_generation_locked(
                        batch_id=batch_id,
                        units=selected,
                        mode=mode,
                        prepare_id=owning_prepare,
                        error=exc,
                    )
            # The controlled message already says what happened and how many
            # rounds were tried; wrapping it again would only duplicate it.
            raise PipelineError(str(exc)) from exc
        except ConflictError:
            if progress_prepare_id:
                self._quality_progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error="准备输入身份已变化。",
                )
                self._quality_progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成未完成，本批不进入独立检查",
                )
            # A frozen-input or mode guard failure is not a generation failure.
            # Do not create a retry record for a result that was never valid for
            # this project; the caller must receive the conflict unchanged.
            with self.lock:
                self._quality_runtime.batch_inflight.pop(batch_id, None)
            raise
        except Exception as exc:
            if progress_prepare_id:
                self._quality_progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error=str(exc),
                )
                self._quality_progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成失败，本批不进入独立检查",
                )
            with self.lock:
                self._quality_runtime.batch_inflight.pop(batch_id, None)
                # The failure has to survive the request: without a record the
                # operator could never find this batch again. It is written as a
                # recovery record only, so scan coverage does not move.
                self._record_failed_generation_locked(
                    batch_id=batch_id,
                    units=selected,
                    mode=mode,
                    prepare_id=owning_prepare,
                    error=exc,
                )
            raise PipelineError(f"概念候选生成失败：{exc}") from exc
        if progress_prepare_id:
            self._quality_progress.change(
                progress_prepare_id, "generation", batch_id, "complete", unit="batch"
            )

        check_status = "completed"
        checks: list[dict[str, Any]] = []
        check_repair: dict[str, Any] | None = None
        check_error = ""
        check_calls = 0
        if scan_result.candidates:
            check_calls = 1
            if progress_prepare_id:
                self._quality_progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "start",
                    unit="batch",
                    label="独立检查",
                    metadata={"batch_id": batch_id, "candidate_count": len(scan_result.candidates)},
                    provider_channel="check",
                )
            try:
                check_result = checker.check_candidates(
                    ConceptCheckRequest(
                        project_id=project_id,
                        batch_id=batch_id,
                        units=refs,
                        candidates=tuple(scan_result.candidates),
                        control=self._quality_batch_repair_control_locked(
                            batch_id,
                            signature,
                            "概念独立检查",
                            frozen_units=selected,
                            frozen_mode=frozen_mode,
                            prepare_id=progress_prepare_id,
                            progress_stage="check",
                        ),
                    )
                )
                checks = check_result.checks
                check_repair = check_result.repair
            except ConflictError:
                if progress_prepare_id:
                    self._quality_progress.change(
                        progress_prepare_id, "check", batch_id, "failed",
                        unit="batch", error="准备输入身份已变化。",
                    )
                with self.lock:
                    self._quality_runtime.batch_inflight.pop(batch_id, None)
                raise
            except Exception as exc:  # noqa: BLE001 - the concrete reason is kept
                # A failed independent check must never be presented as
                # "evidence supported"; candidates are still kept as drafts.
                # The real reason travels with the batch record and the
                # response instead of being flattened into a generic sentence.
                check_status = "failed"
                check_error = str(exc)
                checks = []
                if progress_prepare_id:
                    self._quality_progress.change(
                        progress_prepare_id, "check", batch_id, "failed",
                        unit="batch", error=str(exc),
                    )
            else:
                if progress_prepare_id:
                    self._quality_progress.change(
                        progress_prepare_id, "check", batch_id, "complete", unit="batch"
                    )
        elif progress_prepare_id:
            self._quality_progress.change(
                progress_prepare_id,
                "check",
                batch_id,
                "start",
                unit="batch",
                label="独立检查无需执行",
                metadata={"batch_id": batch_id, "candidate_count": 0},
            )
            self._quality_progress.change(
                progress_prepare_id, "check", batch_id, "not_required", unit="batch"
            )

        with self.lock:
            self._quality_runtime.batch_inflight.pop(batch_id, None)
            if self._closed:
                raise ConflictError("当前项目管理器已关闭，不能保存概念候选。")
            if frozen_mode and concept_automation.reference_mode(
                self.state.get("project")
            ) != frozen_mode:
                raise ConflictError("项目参考模式已经切换，概念候选未保存。")
            units_now = {
                str(unit.get("id")): unit
                for unit in (self.state.get("units") or [])
                if isinstance(unit, dict)
            }
            for unit_id, source_sha256, length in signatures:
                current = units_now.get(unit_id)
                if current is None:
                    raise ConflictError("项目单元已经变化，概念候选未保存。")
                if str(current.get("source_sha256") or "") != str(source_sha256) or len(
                    str(current.get("source") or "")
                ) != length:
                    raise ConflictError("源文已经变化，概念候选未保存。")
            unit_sources = self._quality_unit_sources(list(units_now.values()))
            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            support = normalize_quality_support(self.state.get("quality_support"))
            saved: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            duplicate_skipped: list[dict[str, Any]] = []
            for index, candidate in enumerate(scan_result.candidates):
                # The check result stays bound to the candidate index: skipping
                # a duplicate never shifts what the other candidates receive.
                check = checks[index] if index < len(checks) else None
                try:
                    # The automatic prepare path must not overwrite a card a
                    # human owns; the manual scan path keeps the historical
                    # writer untouched.
                    if mode == "automatic":
                        # The formal human-edit path must also exit automatic
                        # management: a saved manual edit is detected from its
                        # own marker and protected before any write happens.
                        target_id = concept_automation.candidate_card_id(candidate)
                        target = (support.get("cards") or {}).get(target_id)
                        if isinstance(target, dict):
                            concept_automation.mark_human_owned_card(target)
                        outcome = concept_automation.upsert_automatic_draft(
                            support,
                            candidate,
                            unit_sources=unit_sources,
                            prepare_id=batch_id,
                            unit_ids=[ref.unit_id for ref in refs],
                            now_iso_value=now_iso(),
                            check=(
                                self._stored_check_payload(
                                    check,
                                    candidate,
                                    unit_sources=unit_sources,
                                    model=str(getattr(checker, "model", "") or ""),
                                )
                                if check is not None
                                else None
                            ),
                        )
                    else:
                        outcome = upsert_candidate(
                            support,
                            candidate,
                            unit_sources=unit_sources,
                            batch_id=batch_id,
                            unit_ids=[ref.unit_id for ref in refs],
                            now_iso_value=now_iso(),
                            check=(
                                self._stored_check_payload(
                                    check,
                                    candidate,
                                    unit_sources=unit_sources,
                                    model=str(getattr(checker, "model", "") or ""),
                                )
                                if check is not None
                                else None
                            ),
                        )
                except QualitySupportError as exc:
                    failed.append(
                        {
                            "expressions": candidate.get("expressions") or [],
                            "reason": str(exc),
                        }
                    )
                    continue
                if outcome["outcome"] == "duplicate":
                    # The batch's own check for this candidate is dropped: it
                    # belongs to a candidate this project already holds, and the
                    # matched card keeps its origin, status and conclusion.
                    duplicate_skipped.append(
                        {
                            "index": index,
                            "card_id": outcome["card_id"],
                            "reason": outcome["reason"],
                        }
                    )
                    continue
                saved.append(
                    {
                        "card_id": outcome["card_id"],
                        "expressions": candidate["expressions"],
                        "outcome": outcome["outcome"],
                    }
                )
            repair_summary: dict[str, Any] = {}
            if scan_result.repair is not None:
                repair_summary["generate"] = dict(scan_result.repair)
            if check_repair is not None:
                repair_summary["check"] = dict(check_repair)
            created_count = sum(1 for row in saved if row["outcome"] == "created")
            updated_count = sum(1 for row in saved if row["outcome"] == "updated")
            retry_record = (
                self._batch_retry_record(
                    stage="check",
                    mode=mode,
                    prepare_id=owning_prepare,
                    units=selected,
                    cards=[
                        support["cards"][row["card_id"]]
                        for row in saved
                        if row["card_id"] in (support.get("cards") or {})
                    ],
                    state="failed",
                    attempt_count=0,
                    last_error=(
                        check_error.strip()
                        or "独立检查未完成，候选保留但不参与自动采用。"
                    ),
                )
                if check_status != "completed"
                else None
            )
            batch_record = {
                "batch_id": batch_id,
                "unit_ids": [ref.unit_id for ref in refs],
                **({"retry": retry_record} if retry_record is not None else {}),
                "status": "completed" if not failed and check_status == "completed" else "partial",
                "candidate_count": len(scan_result.candidates),
                "saved_count": len(saved),
                "created_count": created_count,
                "updated_count": updated_count,
                "duplicate_count": len(duplicate_skipped),
                "duplicate_skipped": duplicate_skipped,
                "failed_count": len(failed),
                "check_status": check_status,
                "model": scan_result.model,
                "usage": dict(scan_result.usage or {}),
                # Bounded-repair bookkeeping: which round succeeded and how
                # many HTTP requests the two calls really sent.
                "repair": repair_summary,
                "at": now_iso(),
            }
            # A successful generation recovery has no new retry record to put
            # in this result, but the running retry object carries the owning
            # prepare_id needed by _apply_retry_close_locked. Keep it through
            # this replacement of the batch row; otherwise record_batch would
            # erase the identity before the shared close can sync prepare rows.
            if retry_record is None and retry_close is not None and stored_batch is not None:
                previous_retry = copy.deepcopy(stored_batch.get("retry"))
                if previous_retry:
                    batch_record["retry"] = previous_retry
            record_batch(support, batch_record)
            self._event_locked(
                "quality_scan_batch_saved",
                f"概念候选批次 {batch_id} 已保存 {len(saved)} 张（新增 {created_count}、"
                f"更新 {updated_count}），完全重复跳过 {len(duplicate_skipped)} 张，失败 {len(failed)} 张。",
                batch_id=batch_id,
            )
            # A hand retry closes its own record — and promotes the prepare rows
            # it recovered — inside this same commit.
            retry_recorded = (
                self._apply_retry_close_locked(
                    support,
                    batch_id=batch_id,
                    close=retry_close,
                    check_status=check_status,
                    check_error=check_error,
                )
                if retry_close is not None
                else False
            )
            self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            return {
                "status": "ok",
                "batch_id": batch_id,
                "candidate_count": len(scan_result.candidates),
                "saved": saved,
                "duplicate_skipped": duplicate_skipped,
                "created_count": created_count,
                "updated_count": updated_count,
                "duplicate_count": len(duplicate_skipped),
                "failed": failed,
                "check_status": check_status,
                "check_error": check_error,
                # Invocations counted where they happened; "0" means the provider
                # was never called, not that the outcome is unknown.
                "provider_calls": {
                    "generation": generation_calls,
                    "check": check_calls,
                },
                "retry_recorded": retry_recorded,
                "repair": repair_summary,
                "revision": support["revision"],
                "approved_version": support["approved_version"],
            }

    @staticmethod
    def _batch_retry_record(
        *,
        stage: str,
        mode: str,
        prepare_id: str,
        units: Sequence[Mapping[str, Any]],
        cards: Sequence[Mapping[str, Any]] = (),
        state: str,
        attempt_count: int,
        last_error: str,
    ) -> dict[str, Any]:
        """The recovery identity frozen onto one batch record.

        Only what a later hand retry needs: which step failed, which mode and
        prepare produced it, the source binding it was made for, and (for a
        failed check) the card revisions it was made for. No prompt, source text
        or model answer is copied here.
        """

        return {
            "stage": str(stage),
            "mode": str(mode),
            "prepare_id": str(prepare_id or ""),
            "source_bindings": [
                {
                    "unit_id": str(unit.get("id") or ""),
                    "source_sha256": str(unit.get("source_sha256") or ""),
                }
                for unit in units
            ],
            "card_bindings": [
                {
                    "card_id": str(card.get("id") or ""),
                    "draft_revision": int(card.get("draft_revision") or 0),
                    "content_fingerprint": concept_automation._content_fingerprint(card),
                }
                for card in cards
            ],
            "state": str(state),
            "attempt_count": max(0, int(attempt_count or 0)),
            "last_error": str(last_error or "")[:400],
            "updated_at": now_iso(),
        }

    def _record_failed_generation_locked(
        self,
        *,
        batch_id: str,
        units: Sequence[Mapping[str, Any]],
        mode: str,
        prepare_id: str,
        error: BaseException,
    ) -> None:
        """Register a failed generation as a recovery record and persist it.

        It is written through ``record_batch`` with ``touch_coverage=False``: the
        units produced no scan result, so this must never move
        ``scanned_unit_ids`` (that would make "continue" skip them forever).
        """

        if self._closed:
            return
        old_support = copy.deepcopy(self.state.get("quality_support"))
        old_events = copy.deepcopy(self.state.get("events") or [])
        support = normalize_quality_support(self.state.get("quality_support"))
        existing = self._batch_row_copy(support, batch_id) or {}
        record = self._batch_retry_record(
            stage="generation",
            mode=mode,
            prepare_id=prepare_id,
            units=units,
            state="failed",
            attempt_count=int((existing.get("retry") or {}).get("attempt_count") or 0),
            last_error=str(error),
        )
        batch = {**existing, "batch_id": str(batch_id)}
        # Display fields: the record has to be findable, and its failure reason
        # must be the real one rather than an empty string.
        batch.setdefault("unit_ids", [str(unit.get("id") or "") for unit in units])
        batch.setdefault("status", "failed")
        batch["check_status"] = "not_run"
        batch["failed_count"] = len(batch.get("unit_ids") or [])
        batch["at"] = now_iso()
        batch["retry"] = record
        record_batch(support, batch, touch_coverage=False)
        self._event_locked(
            "quality_scan_generation_failed",
            f"概念候选批次 {batch_id} 生成失败，已登记为可重试批次。",
            batch_id=str(batch_id),
        )
        self._quality_commit_locked(support, old_support=old_support, old_events=old_events)

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

        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise PipelineError("缺少批次标识。")

        # Phase 1 — under the lock: verify identity, freeze the attempt, and make
        # the running state durable. A failure here must not reach the model.
        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(
                support.get("revision") or 0
            ):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            stored = self._batch_row_copy(support, batch_id)
            if stored is None:
                raise ConflictError("找不到这个批次的记录，请刷新后重试。")
            mode = concept_automation.reference_mode(self.state.get("project"))
            prepare_record = concept_automation.automation_of(support).get("prepare")
            current_prepare_id = (
                str(prepare_record.get("prepare_id") or "")
                if isinstance(prepare_record, Mapping)
                else ""
            )
            descriptor = batch_retry_descriptor(
                support,
                stored,
                unit_sources=self._quality_unit_sources(self.state.get("units") or []),
                mode=mode,
                current_prepare_id=current_prepare_id,
            )
            if not descriptor["retryable"]:
                raise ConflictError(descriptor["blocked_reason"] or "这个批次当前不能重试。")
            if batch_id in self._quality_runtime.retry_inflight:
                raise ConflictError(f"批次 {batch_id} 正在恢复中，请勿重复提交。")
            if self._quality_runtime.retry_inflight and (
                not allow_parallel
                or self._quality_runtime.retry_inflight != self._quality_runtime.retry_parallel
            ):
                raise ConflictError(
                    f"批次 {sorted(self._quality_runtime.retry_inflight)[0]} 正在恢复中，请等待结束再重试。"
                )
            if set(self._quality_runtime.batch_inflight) - self._quality_runtime.retry_inflight:
                raise ConflictError("概念扫描仍在进行，请等待结束再重试。")
            if isinstance(prepare_record, Mapping) and str(
                prepare_record.get("status") or ""
            ) == "running":
                raise ConflictError("自动准备仍在进行，请等待结束或先终止准备。")
            if self._quality_runtime.prepare_inflight:
                # The same exclusion the other direction enforces: a retry must
                # not start behind a prepare that is already running, or the
                # prepare's own batches would be refused mid-flight.
                raise ConflictError("自动准备仍在进行，请等待结束或先终止准备。")
            stage = str(descriptor["stage"])
            units_by_id = {
                str(unit.get("id")): unit
                for unit in (self.state.get("units") or [])
                if isinstance(unit, dict)
            }
            selected = [
                units_by_id[unit_id]
                for unit_id in descriptor["units"]
                if unit_id in units_by_id
            ]
            if not selected:
                raise ConflictError("批次引用的单元已经不存在，请重新预览。")
            if self._quality_runtime.retry_inflight:
                requested_units = set(descriptor["units"])
                requested_cards = {
                    str(item.get("card_id") or "")
                    for item in (stored.get("retry") or {}).get("card_bindings") or []
                }
                for active_id in self._quality_runtime.retry_inflight:
                    active = self._batch_row_copy(support, active_id) or {}
                    active_retry = active.get("retry") or {}
                    active_units = {
                        str(item.get("unit_id") or "")
                        for item in active_retry.get("source_bindings") or []
                    }
                    active_cards = {
                        str(item.get("card_id") or "")
                        for item in active_retry.get("card_bindings") or []
                    }
                    if requested_units & active_units or requested_cards & active_cards:
                        raise ConflictError("这批与正在恢复的批次涉及同一单元或候选，请等待后再重试。")
            attempt_count = int(descriptor["attempt_count"]) + 1
            frozen = normalize_batch_retry(stored.get("retry")) or {}
            running = self._batch_retry_record(
                stage=stage,
                mode=str(descriptor["mode"]),
                prepare_id=str(frozen.get("prepare_id") or ""),
                units=selected,
                cards=[],  # the frozen card bindings are carried over verbatim
                state="running",
                attempt_count=attempt_count,
                last_error=str(descriptor["last_error"]),
            )
            running["card_bindings"] = list(frozen.get("card_bindings") or [])
            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            support = normalize_quality_support(self.state.get("quality_support"))
            target = next(
                item
                for item in (support.get("batches") or [])
                if str(item.get("batch_id")) == batch_id
            )
            target["retry"] = running
            # Admission is a durable state transition. Advance the support
            # revision here so another confirmed batch can obtain a fresh
            # version while this provider call is still in flight.
            record_batch(support, target, touch_coverage=False)
            self._event_locked(
                "quality_batch_retry_started",
                f"批次 {batch_id} 的手动恢复已开始（第 {attempt_count} 次）。",
                batch_id=batch_id,
            )
            try:
                self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            except Exception as exc:
                # The attempt was never frozen: no model call may follow.
                raise PipelineError(f"批次恢复开始前保存失败：{exc}") from exc
            self._quality_runtime.retry_inflight.add(batch_id)
            if allow_parallel:
                self._quality_runtime.retry_parallel.add(batch_id)

        # Phase 2 — outside the lock: the same call chain the batch used before.
        # The writer closes the recovery record and promotes the prepare rows it
        # recovered in its own commit, so this is a request, not a second save.
        close: dict[str, Any] = {
            "stage": stage,
            "attempt_count": attempt_count,
            "units": [str(unit.get("id")) for unit in selected],
        }
        failure: BaseException | None = None
        result: dict[str, Any] = {}
        try:
            if stage == "generation":
                # Re-running the generation chain is exactly a scan of this
                # batch: it re-checks the sources, calls generation + check, and
                # records its own outcome on the batch.
                result = self.scan_quality_batch(
                    batch_id=batch_id,
                    unit_ids=[str(unit.get("id")) for unit in selected],
                    expected_project_id=None,
                    mode=str(descriptor["mode"]),
                    retry_close=close,
                )
                check_completed = str(result.get("check_status") or "") == "completed"
            else:
                result = dict(
                    self._retry_quality_check_locked(
                        batch_id, copy.deepcopy(selected), retry_close=close
                    )
                )
                check_completed = str(result.get("check_status") or "") == "completed"
        except ConflictError as exc:
            # A guard rejection is not a batch failure and must stay a conflict:
            # it is never swallowed into the generic failure branch below. The
            # record keeps the concrete reason the guard refused.
            self._close_batch_retry_record(
                batch_id,
                state="failed",
                attempt_count=attempt_count,
                last_error=str(exc),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then reported honestly
            failure = exc
            self._close_batch_retry_record(
                batch_id,
                state="failed",
                attempt_count=attempt_count,
                last_error=str(exc),
            )
        else:
            # The result, its recovery record and the prepare rows it recovered
            # were committed together by the writer above; ``recorded`` is what
            # that one commit actually reported, never an assumption.
            recorded = bool(result.get("retry_recorded"))
        finally:
            with self.lock:
                self._quality_runtime.retry_inflight.discard(batch_id)
                self._quality_runtime.retry_parallel.discard(batch_id)

        if failure is not None:
            if isinstance(failure, OSError):
                # A save failure must say so: the model already answered and its
                # result was not stored, and nothing may be re-called for it.
                raise PipelineError(f"批次恢复结果保存失败：{failure}") from failure
            raise PipelineError(f"批次恢复失败：{failure}") from failure
        repair = dict(result.get("repair") or {})
        # Two measured counters, deliberately not merged: how many times this
        # request invoked each provider, and how many transport requests those
        # providers reported sending (format-repair rounds included). A stage
        # that sent nothing reports 0 — nothing is inferred from the outcome.
        provider_calls = {
            "generation": int((result.get("provider_calls") or {}).get("generation") or 0),
            "check": int((result.get("provider_calls") or {}).get("check") or 0),
        }
        provider_http = {
            "generation": int((repair.get("generate") or {}).get("api_calls") or 0),
            "check": int((repair.get("check") or {}).get("api_calls") or 0),
        }
        with self.lock:
            support_after = normalize_quality_support(self.state.get("quality_support"))
            stored_after = next(
                (
                    dict(item)
                    for item in (support_after.get("batches") or [])
                    if str(item.get("batch_id")) == batch_id
                ),
                {},
            )
            descriptor_after = batch_retry_descriptor(
                support_after,
                stored_after,
                unit_sources=self._quality_unit_sources(self.state.get("units") or []),
                mode=concept_automation.reference_mode(self.state.get("project")),
                current_prepare_id=(
                    str(
                        (
                            concept_automation.automation_of(support_after).get("prepare") or {}
                        ).get("prepare_id")
                        or ""
                    )
                ),
            )
        return {
            "status": "ok",
            "batch_id": batch_id,
            "stage": stage,
            "mode": str(descriptor["mode"]),
            "units": [str(unit.get("id")) for unit in selected],
            "check_status": str(result.get("check_status") or ""),
            "attempt_count": attempt_count,
            # Provider invocations this request really made ("0" = never called),
            # and the transport requests those providers reported sending, which
            # include their bounded format-repair rounds. Never one field faking
            # the other.
            "provider_calls": provider_calls,
            "provider_http": provider_http,
            # Post-retry view of the same batch: how many candidates were still
            # actionable, which ones a human took over, and which changed while
            # the model was answering.
            "actionable_cards": int(descriptor_after.get("actionable_cards") or 0),
            "protected_cards": int(descriptor_after.get("protected_cards") or 0),
            "changed_cards": int(descriptor_after.get("changed_cards") or 0),
            "retryable_after": bool(descriptor_after.get("retryable")),
            "recorded": bool(recorded),
            "repair_rounds": {
                "generation": int((repair.get("generate") or {}).get("rounds") or 0),
                "check": int((repair.get("check") or {}).get("rounds") or 0),
            },
            # The concrete reason, never an empty string that hides a failure.
            "error": str(result.get("check_error") or "").strip(),
            "reference_refresh_required": bool(
                self._prepare_reference_refresh_required()
            ),
        }

    def _close_batch_retry_record(
        self,
        batch_id: str,
        *,
        state: str,
        attempt_count: int,
        last_error: str,
    ) -> bool:
        """Best-effort bookkeeping for a retry that stored no result.

        Only the failure path uses this: with no stored result there is nothing
        to commit together, and a record left as "running" asks for a fresh
        confirmation instead of resuming anything by itself. Returns whether the
        record was written; a failure here never replaces the real outcome.
        """

        try:
            self._finish_batch_retry_locked(
                batch_id,
                state=state,
                attempt_count=attempt_count,
                last_error=last_error,
            )
            return True
        except Exception:  # noqa: BLE001 - deliberately reported through the return value
            return False

    def _finish_batch_retry_locked(
        self,
        batch_id: str,
        *,
        state: str,
        attempt_count: int,
        last_error: str,
    ) -> bool:
        """Record how one hand retry ended when its result was never stored."""

        with self.lock:
            if self._closed:
                return False
            support = normalize_quality_support(self.state.get("quality_support"))
            target = next(
                (
                    item
                    for item in (support.get("batches") or [])
                    if str(item.get("batch_id")) == batch_id
                ),
                None,
            )
            if target is None:
                return False
            frozen = normalize_batch_retry(target.get("retry")) or {}
            target["retry"] = self._retry_record_update(
                frozen,
                state=state,
                attempt_count=attempt_count,
                last_error=last_error,
            )
            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            return True

    @staticmethod
    def _retry_record_update(
        frozen: Mapping[str, Any],
        *,
        state: str,
        attempt_count: int,
        last_error: str,
        stage: str = "",
    ) -> dict[str, Any]:
        """The stored shape of one recovery record after an attempt ended."""

        hint = str(last_error or "").strip()
        return {
            **frozen,
            "stage": str(frozen.get("stage") or stage or "check"),
            "state": str(state),
            "attempt_count": int(attempt_count),
            # A finished recovery clears the current error; the original one
            # stays in the record this update started from (and in the events).
            "last_error": hint
            or ("" if state == "completed" else str(frozen.get("last_error") or "")),
            "updated_at": now_iso(),
        }

    def _apply_retry_close_locked(
        self,
        support: dict[str, Any],
        *,
        batch_id: str,
        close: Mapping[str, Any],
        check_status: str,
        check_error: str,
    ) -> bool:
        """Write the hand-retry record — and its prepare sync — into one snapshot.

        The caller runs this in the very locked block that stores the recovered
        result, so the verdict, the recovery state and the prepare rows it
        recovered share one save. A failed check promotes nothing and keeps the
        concrete reason; only a completed one promotes the frozen units.
        Returns whether the batch row was there to update.
        """

        target = next(
            (
                item
                for item in (support.get("batches") or [])
                if str(item.get("batch_id")) == batch_id
            ),
            None,
        )
        if target is None:
            return False
        frozen = normalize_batch_retry(target.get("retry")) or {}
        completed = str(check_status or "") == "completed"
        target["retry"] = self._retry_record_update(
            frozen,
            state="completed" if completed else "failed",
            attempt_count=int(close.get("attempt_count") or 0),
            last_error="" if completed else (str(check_error or "").strip() or "独立检查未完成。"),
            stage=str(close.get("stage") or ""),
        )
        prepare_id = str(frozen.get("prepare_id") or "")
        units = [str(unit_id) for unit_id in close.get("units") or []]
        if completed and prepare_id and units:
            self._sync_prepare_rows(
                support, batch_id=batch_id, units=units, prepare_id=prepare_id
            )
        return True

    def _prepare_reference_refresh_required(self) -> bool:
        support = normalize_quality_support(self.state.get("quality_support"))
        record = concept_automation.automation_of(support).get("prepare")
        return bool(isinstance(record, Mapping) and record.get("reference_refresh_required"))

    @staticmethod
    def _sync_prepare_rows(
        support: dict[str, Any],
        *,
        batch_id: str,
        units: Sequence[str],
        prepare_id: str,
    ) -> bool:
        """Promote the matching prepare rows inside one support snapshot.

        Only the current prepare's rows for exactly these units are touched, and
        only while they are still ``failed``: the original error entries stay as
        history, the group judgments, decisions and frozen references are
        untouched, and the record is never rewritten into ``complete``.
        ``reference_refresh_required`` only asks the operator to re-preview.
        Returns whether anything changed.
        """

        record = concept_automation.automation_of(support).get("prepare")
        if not isinstance(record, Mapping) or str(record.get("prepare_id") or "") != str(
            prepare_id
        ):
            # The prepare was replaced meanwhile: resuming across records is out
            # of scope, and the recovery itself is still valid.
            return False
        batch_unit_ids = {str(unit_id) for unit_id in units}
        current = copy.deepcopy(dict(record))
        touched = False
        for row in current.get("unit_results") or []:
            if str(row.get("unit_id")) not in batch_unit_ids:
                continue
            if str(row.get("status") or "") != "failed":
                continue
            row["status"] = "completed"
            row["batch_id"] = batch_id
            row["reason"] = ""
            touched = True
        if not touched:
            return False
        counts = current.setdefault("counts", {})
        counts["failed_units"] = max(0, int(counts.get("failed_units") or 0) - len(batch_unit_ids))
        # A run whose every failure has now been recovered by hand stops being
        # "failed" — otherwise the next preview would refuse to reuse rows that
        # are genuinely finished. It is never promoted to "complete": the record
        # keeps its history and the operator still has to re-preview, which is
        # what the marker below says.
        if not int(counts.get("failed_units") or 0) and str(
            current.get("status") or ""
        ) not in concept_automation.REUSABLE_PREPARE_STATUSES:
            current["status"] = "partial"
        current["reference_refresh_required"] = True
        current["reference_refresh_required_at"] = now_iso()
        automation = concept_automation.automation_of(support)
        automation["prepare"] = current
        support["automation"] = automation
        return True

    def _retry_quality_check_locked(
        self,
        batch_id: str,
        selected: list[dict[str, Any]],
        *,
        retry_close: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-run only the independent check of one already-saved batch.

        The generation request is never repeated: its candidates are already
        persisted. Model call happens outside the lock; the unit bindings are
        re-validated before anything is written. ``retry_close`` is set only by
        ``retry_quality_batch`` and folds the hand-retry record and its prepare
        sync into the same commit as the verdict itself.
        """
        # Re-capture the retry input under the manager lock, then release it
        # before the independent checker call.
        with self.lock:
            self._ensure_open_locked()
            selected = copy.deepcopy(selected)
            support = normalize_quality_support(self.state.get("quality_support"))
            signatures = [
                (unit["id"], unit["source_sha256"], len(str(unit.get("source") or "")))
                for unit in selected
            ]
            signature = repr(signatures)
            refs = self._concept_unit_refs(selected)
            cards = [
                card
                for card in support.get("cards", {}).values()
                if isinstance(card, dict)
                and str((card.get("origin") or {}).get("batch_id") or "") == batch_id
                and card.get("status") == "pending_review"
                and isinstance(card.get("draft"), dict)
                and not concept_automation.is_manual_protected(card)
            ]
            cards.sort(key=lambda card: str(card.get("id") or ""))
            candidates = tuple(card["draft"] for card in cards)  # type: ignore[misc]
            # The identity the model is about to answer for: card id, content
            # version, content fingerprint and the project/mode it belongs to.
            # Every repair request and the write that follows re-check it, so a
            # card a human took over mid-flight never receives a late verdict.
            frozen_cards = {
                str(card.get("id") or ""): {
                    "draft_revision": int(card.get("draft_revision") or 0),
                    "content_fingerprint": concept_automation._content_fingerprint(card),
                }
                for card in cards
            }
            frozen_mode = concept_automation.reference_mode(self.state.get("project"))
            frozen_batch_ids = set(frozen_cards)
            project_id = str(self.state.get("project", {}).get("id") or "")
            # A batch whose candidates were all exact duplicates has no card of
            # its own left to check. Retrying the check must not spend a model
            # call on an empty candidate list, and it must never reach for a card
            # this batch does not own: the retry only ever touches cards whose
            # origin is this batch_id.
            should_call = bool(candidates)
            if should_call:
                self._quality_runtime.batch_inflight[batch_id] = signature

        _generation, checker, _editorial, _resolution = self._quality_providers()
        check_repair: dict[str, Any] | None = None
        check_error = ""
        checked_candidates = len(candidates)
        check_calls = 0
        if not should_call:
            checks = []
            check_status = "completed"
            checked_candidates = 0
        else:
            check_calls = 1
            try:
                check_result = checker.check_candidates(
                    ConceptCheckRequest(
                        project_id=project_id,
                        batch_id=batch_id,
                        units=refs,
                        candidates=candidates,
                        control=self._quality_batch_repair_control_locked(
                            batch_id,
                            signature,
                            "概念独立检查",
                            frozen_cards=frozen_cards,
                            frozen_units=selected,
                            frozen_mode=frozen_mode,
                        ),
                    )
                )
                # The stored check must carry the same program-written identity
                # the first-time scan writes: without it the automatic path reads
                # this result as a legacy check and pays for another one. The
                # context is built here, from the live sources — never trusted
                # from the model.
                unit_sources = self._quality_unit_sources(
                    [unit for unit in selected if isinstance(unit, Mapping)]
                )
                checks = [
                    (
                        self._stored_check_payload(
                            check,
                            candidates[index],
                            unit_sources=unit_sources,
                            model=str(getattr(checker, "model", "") or ""),
                        )
                        if isinstance(check, Mapping) and index < len(candidates)
                        else check
                    )
                    for index, check in enumerate(check_result.checks)
                ]
                check_repair = check_result.repair
                check_status = "completed"
            except ConflictError:
                # A guard refusal (closed, superseded, mode switch, human takeover
                # or a card whose content moved on) is not a batch failure: it must
                # reach the caller as a conflict and must not be written.
                with self.lock:
                    self._quality_runtime.batch_inflight.pop(batch_id, None)
                raise
            except Exception as exc:  # noqa: BLE001 - reported, never silently emptied
                with self.lock:
                    self._quality_runtime.batch_inflight.pop(batch_id, None)
                checks = []
                check_status = "failed"
                check_error = str(exc)

        with self.lock:
            self._quality_runtime.batch_inflight.pop(batch_id, None)
            if self._closed:
                raise ConflictError("当前项目管理器已关闭，不能保存检查结果。")
            if frozen_mode and concept_automation.reference_mode(
                self.state.get("project")
            ) != frozen_mode:
                raise ConflictError("项目参考模式已经切换，检查结果未保存。")
            units_now = {
                str(unit.get("id")): unit
                for unit in (self.state.get("units") or [])
                if isinstance(unit, dict)
            }
            for unit_id, source_sha256, length in signatures:
                current = units_now.get(unit_id)
                if current is None:
                    raise ConflictError("项目单元已经变化，检查结果未保存。")
                if str(current.get("source_sha256") or "") != str(source_sha256) or len(
                    str(current.get("source") or "")
                ) != length:
                    raise ConflictError("源文已经变化，检查结果未保存。")
            # The identity is re-checked once more at the write boundary: the model
            # answered for material that must still be exactly the frozen one,
            # otherwise the verdict is dropped instead of applied.
            live_support = normalize_quality_support(self.state.get("quality_support"))
            for card_id, expected in frozen_cards.items():
                card = (live_support.get("cards") or {}).get(card_id)
                if not isinstance(card, Mapping) or str(card.get("status") or "") != "pending_review":
                    raise ConflictError(f"卡片 {card_id} 已被人工处理，检查结果未保存。")
                if concept_automation.is_manual_protected(card):
                    raise ConflictError(f"卡片 {card_id} 已被人工接管，检查结果未保存。")
                if int(card.get("draft_revision") or 0) != int(
                    expected.get("draft_revision") or 0
                ) or concept_automation._content_fingerprint(card) != str(
                    expected.get("content_fingerprint") or ""
                ):
                    raise ConflictError(f"卡片 {card_id} 的内容已经变化，检查结果未保存。")
            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            support = normalize_quality_support(self.state.get("quality_support"))
            if checks:
                apply_check_result(
                    support,
                    batch_id=batch_id,
                    checks=checks,
                    now_iso_value=now_iso(),
                )
            stored_batch = self._batch_row_copy(support, batch_id)
            if stored_batch is not None:
                stored_batch["check_status"] = check_status
                if check_status == "completed":
                    stored_batch["status"] = (
                        "completed" if not stored_batch.get("failed_count") else "partial"
                    )
                if check_repair is not None:
                    stored_batch["repair"] = {
                        **dict(stored_batch.get("repair") or {}),
                        "check": dict(check_repair),
                    }
                record_batch(support, stored_batch)
            self._event_locked(
                "quality_check_retried",
                f"概念批次 {batch_id} 的独立检查已重试，结果：{check_status}。",
                batch_id=batch_id,
            )
            # Hand-retry bookkeeping is written by the same commit that stores
            # the verdict: a partial success — verdict saved, recovery state or
            # prepare rows missing — is not expressible here.
            retry_recorded = (
                self._apply_retry_close_locked(
                    support,
                    batch_id=batch_id,
                    close=retry_close,
                    check_status=check_status,
                    check_error=check_error,
                )
                if retry_close is not None
                else False
            )
            self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            return {
                "status": "ok",
                "batch_id": batch_id,
                "candidate_count": len(candidates),
                "saved": [],
                "failed": [],
                "check_status": check_status,
                "check_error": check_error,
                "checked_candidates": checked_candidates,
                # Invocations counted at the call site: the checker is only
                # called when there is at least one candidate to check.
                "provider_calls": {"generation": 0, "check": check_calls},
                "retry_recorded": retry_recorded,
                "repair": {"check": dict(check_repair)} if check_repair else {},
                "revision": support["revision"],
                "approved_version": support["approved_version"],
                "retry": "check-only",
            }

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
        with self.lock:
            self._validate_expected_project_id_locked(expected_project_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            units = [unit for unit in (self.state.get("units") or []) if isinstance(unit, dict)]
            versions: dict[str, Any] = {}
            for unit in units:
                reference = unit.get("quality_reference")
                if isinstance(reference, dict):
                    translation = reference.get("translation")
                    if isinstance(translation, dict):
                        versions[str(unit.get("id"))] = translation.get("approved_version")
        affected = affected_units_for_cards(support, units, reference_versions=versions)
        unknown = [
            {
                "unit_id": item["unit_id"],
                "status": item["status"],
                "reason": "没有记录当时使用的概念参考版本。",
            }
            for item in affected
            if item["previous_reference_version"] is None
        ]
        return {
            "approved_version": support["approved_version"],
            "affected": affected,
            "unknown_reference_count": len(unknown),
            "note": "可能受影响，不代表已发现误译。",
        }

    # ------------------------------------------------------------------
    # automatic reference preparation (V1)
    # ------------------------------------------------------------------

    def _lazy_automation(self) -> dict[str, Any]:
        """The automation state of the live project, normalized read-only."""

        return concept_automation.automation_of(
            normalize_quality_support(self.state.get("quality_support"))
        )

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
        work_limit = self._resolve_additional_work_limit(additional_work_limit)
        parallel_batches = self._resolve_parallel_batches(max_parallel_batches)
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
            return self._quality_prepare_commit(
                plan={"prepare_id": plan_id} if plan_id else {},
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
            )
        raise PipelineError("prepare 的 phase 只能是 plan、execute、resolve 或 commit。")

    # ------------------------------------------------------------------
    # R1: read-only preview
    # ------------------------------------------------------------------

    def _prepare_unit_reuse_locked(
        self,
        automation: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, str]]:
        """Which units of this scope still count as finished, and why not.

        A unit is reused only when the previous preparation recorded that its
        generation + check work finished for the *same* source hash. A unit the
        manual panel merely marked as scanned is not covered: that flag carries
        no source version and says nothing about the automatic preparation.
        """

        coverage = concept_automation.prior_unit_coverage(automation.get("prepare"))
        states: dict[str, dict[str, str]] = {}
        for unit in targets:
            unit_id = str(unit.get("id") or "")
            source_sha256 = str(unit.get("source_sha256") or "")
            if unit_id and coverage.get(unit_id) == source_sha256:
                states[unit_id] = {
                    "state": "reused",
                    "reason": "上次准备已完成这个单元，源文与结论都没有变化。",
                }
            elif coverage.get(unit_id):
                states[unit_id] = {
                    "state": "work",
                    "reason": "源文已经变化，需要重新生成与检查。",
                }
            else:
                states[unit_id] = {
                    "state": "work",
                    "reason": "还没有完成的自动扫描，需要生成与检查。",
                }
        return states

    def _prepare_group_reuse_locked(
        self,
        support: Mapping[str, Any],
        automation: Mapping[str, Any],
        unit_sources: Mapping[str, tuple[str, str]],
        *,
        unit_ids: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """The groups the next run would judge, and which judgments are reusable.

        The comparison is per group and uses the same input fingerprint that the
        judgment itself is frozen with, computed from the *live* membership: a
        group that gained or lost a member therefore never matches, even though
        its id stays the same.

        An oversized group is never "done" just because it has an outcome: it is
        judged one unit at a time, so the preview reports how many units the
        earlier confirmations already answered and how many are still owed. That
        remaining work is what a new confirmation continues — the finished units
        are handed over and never judged again.
        """

        prior = concept_automation.prior_group_judgments(automation.get("prepare"))
        scope = [str(unit_id) for unit_id in (unit_ids or [])] or sorted(unit_sources)
        rows: dict[str, dict[str, Any]] = {}
        for group in concept_automation.planned_groups(support, unit_sources=unit_sources):
            group_id = str(group["group_id"])
            members = self._prepare_group_members_locked(support, group.get("card_ids") or [])
            entry: dict[str, Any] = {"members": len(members), "reused": False, "reason": ""}
            stored = prior.get(group_id) or {}
            fingerprint = concept_automation.group_input_fingerprint(
                group_id, members, support, unit_sources
            )
            stale = bool(fingerprint) and str(stored.get("input_fingerprint") or "") != fingerprint
            if group.get("oversized"):
                plans = concept_automation.local_contention_plans(
                    group_id, members, unit_sources=unit_sources, unit_ids=scope
                )
                judged = {} if stale else concept_automation.local_judgment_payloads(
                    stored.get("outcome")
                )
                remaining = [
                    str(row["unit_id"])
                    for row in plans["requests"]
                    if str(row["unit_id"]) not in judged
                ]
                entry["local_units_reused"] = len(judged)
                entry["local_units_pending"] = len(remaining)
                entry["local_units_oversized"] = len(plans["oversized_units"])
                if not remaining:
                    entry["reused"] = True
                    entry["reason"] = "组成员与来源都没有变化，沿用上次的局部辨析结论。"
                elif not judged:
                    entry["reason"] = (
                        f"组过大，本次按单元局部辨析：还有 {len(remaining)} 个单元待判断，"
                        "预算内能判断多少就判断多少。"
                    )
                else:
                    entry["reason"] = (
                        f"还有 {len(remaining)} 个单元没有完成局部辨析，确认后继续；"
                        f"已完成的 {len(judged)} 个单元直接复用。"
                    )
            elif len(members) < 2:
                entry["reason"] = "组内没有两张可自动管理的卡片，不需要辨析。"
            elif fingerprint and not stale:
                entry["reused"] = True
                entry["reason"] = "组成员与来源都没有变化，沿用上次辨析结论。"
            else:
                entry["reason"] = "组成员、内容、检查或原文有变化，需要重新辨析。"
            rows[group_id] = entry
        return rows

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
            targets = self._quality_scan_units_locked(
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
            unit_states = self._prepare_unit_reuse_locked(automation, targets)
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
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            in_scope = [str(unit.get("id")) for unit in targets]
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)
            group_reuse = self._prepare_group_reuse_locked(
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
                    "max_parallel_batches": self._resolve_parallel_batches(max_parallel_batches),
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

    def _prepare_signature_locked(self) -> tuple[str, str, str]:
        """The identity a running prepare is bound to: project, mode, record id."""

        support = normalize_quality_support(self.state.get("quality_support"))
        automation = concept_automation.normalize_automation(support.get("automation"))
        record = automation.get("prepare")
        return (
            str(self.state.get("project", {}).get("id") or ""),
            concept_automation.reference_mode(self.state.get("project")),
            str((record or {}).get("prepare_id") or ""),
        )

    def _prepare_guard_locked(self, prepare_id: str) -> None:
        """Authorize one more model round for the live prepare.

        A close, a project switch, a mode change or a superseded/replaced prepare
        record all invalidate the run: late results are discarded instead of
        being written. The stored global revision is not part of the identity —
        this run's own commits move it — but a *foreign* writer replaces the
        prepare record or switches the mode, which is what the guard detects.
        """

        self._ensure_open_locked()
        bound = self._quality_runtime.prepare_inflight.get(prepare_id)
        if bound is None:
            raise ConflictError("准备任务已经结束或被取代，迟到的结果不再写入。")
        if bound != self._prepare_signature_locked():
            raise ConflictError("项目、模式或准备任务已经变化，准备结果不再写入。")

    def _prepare_group_fresh_locked(
        self, prepare_id: str, group_id: str, fingerprint: str
    ) -> None:
        """Refuse a group judgment whose frozen input is no longer live.

        The identity guard above only sees the project, the mode and the prepare
        id, so a formal human edit or an approval leaves it untouched. Here the
        live cards and sources are compared against the fingerprint frozen with
        the members, which is what actually makes the old judgment unusable.
        """

        self._ensure_open_locked()
        support = normalize_quality_support(self.state.get("quality_support"))
        record = self._prepare_record_locked(support, {"prepare_id": prepare_id})
        stored_plan = self._prepare_plan_payload(record.get("plan") or {})
        group = next(
            (item for item in stored_plan.get("groups") or [] if item["group_id"] == group_id),
            None,
        )
        if group is None:
            raise ConflictError("准备计划已经变化，组辨析结果不再写入。")
        members = group.get("members")
        if not isinstance(members, list) or not members:
            members = self._prepare_group_members_locked(support, group.get("card_ids") or [])
        live = concept_automation.group_input_fingerprint(
            group_id,
            members,
            support,
            self._quality_unit_sources(self.state.get("units") or []),
        )
        if not fingerprint or live != fingerprint:
            raise ConflictError("组辨析期间相关卡片或原文已经变化，本次准备结果已失效。")

    def _prepare_group_control_locked(
        self,
        prepare_id: str,
        group_id: str,
        fingerprint: str,
        *,
        suffix: str = "",
        progress_stage: str = "",
        progress_item_id: str = "",
    ) -> RepairControl:
        """Authorize every request of one group judgment, repairs included.

        ``suffix`` only distinguishes the invocation identity of the several
        local judgments of one oversized group; the authorization itself is
        always the whole group's frozen input, because that is what all of them
        read.
        """

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                self._prepare_group_fresh_locked(prepare_id, group_id, fingerprint)

        return RepairControl(
            invocation_id=f"{prepare_id}:{group_id}{suffix}",
            kind="concept-resolution",
            before_attempt=before_attempt,
            on_progress=(
                self._quality_progress.repair_callback(
                    prepare_id,
                    progress_stage,
                    progress_item_id or f"{group_id}{suffix}",
                )
                if progress_stage
                else None
            ),
        )

    def _begin_prepare_locked(self, prepare_id: str) -> tuple[str, str, str]:
        for other_id in list(self._quality_runtime.prepare_inflight):
            if other_id != prepare_id:
                raise ConflictError("已有一个准备任务正在进行中，请等待它结束。")
        signature = self._prepare_signature_locked()
        self._quality_runtime.prepare_inflight[prepare_id] = signature
        return signature


    def _finish_prepare(self, prepare_id: str) -> None:
        with self.lock:
            self._quality_runtime.prepare_inflight.pop(prepare_id, None)
            self._quality_progress.finish_locked(prepare_id)

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
            targets = self._quality_scan_units_locked(
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
            unit_sources_live = self._quality_unit_sources(self.state.get("units") or [])
            # Incremental scope: units whose previous generation + check finished
            # for the same source hash are not scanned again, and the judgments
            # whose frozen input still matches are handed over by the freeze step
            # below (the prior record is captured before it is replaced).
            unit_states = self._prepare_unit_reuse_locked(automation, targets)
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
            prepared = self._prepare_plan_payload(
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
                    "max_parallel_batches": self._resolve_parallel_batches(max_parallel_batches),
                    "additional_work_limit": additional_work_limit,
                }
            )
            automation["prepare"] = self._new_prepare_record(
                prepared,
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
                self._finish_prepare(prepare_id)
                raise
            # Bind the guard only after the frozen record is in the live state,
            # so the identity later steps see is the one registered here.
            self._begin_prepare_locked(prepare_id)
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
            self._prepare_resolve_pending(prepare_id)
            return self._quality_prepare_commit(
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
                    self._mark_prepare_failed_locked(
                        prepare_id,
                        str(exc) or "准备执行中断或失败。",
                        status="stale" if isinstance(exc, ConflictError) else "failed",
                    )
            raise
        finally:
            self._finish_prepare(prepare_id)

    def _new_prepare_record(
        self,
        prepared: Mapping[str, Any],
        *,
        unit_states: Mapping[str, Mapping[str, str]] | None = None,
        unit_sources: Mapping[str, tuple[str, str]] | None = None,
        lookups: Sequence[Mapping[str, Any]] | None = None,
        lookup_state: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        unit_states = unit_states or {}
        unit_sources = unit_sources or {}

        def result_row(unit_id: str) -> dict[str, Any]:
            state = unit_states.get(unit_id) or {}
            reused = str(state.get("state") or "") == "reused"
            return {
                "unit_id": unit_id,
                # ``reused`` means the work was already finished by an earlier
                # run for this exact source hash; ``pending`` means this run
                # still owes the unit its generation + check.
                "status": "reused" if reused else "pending",
                "batch_id": "",
                "reason": str(state.get("reason") or ""),
                # Recorded with the row: the next run proves a reused unit by
                # this hash, never by "some scan touched it".
                "source_sha256": str((unit_sources.get(unit_id) or ("", ""))[1]),
            }

        reused_count = sum(1 for unit_id in prepared["scope"] if result_row(unit_id)["status"] == "reused")
        return {
            "prepare_id": prepared["prepare_id"],
            "mode": concept_automation.AUTOMATIC_MODE,
            "status": "running",
            "scope": prepared["scope"],
            "scope_fingerprint": prepared["scope_fingerprint"],
            "baseline_revision": prepared["baseline_revision"],
            "baseline_approved_version": prepared["baseline_approved_version"],
            "plan": dict(prepared),
            "counts": {
                "adopted": 0,
                "skipped": 0,
                "unresolved": 0,
                "protected": 0,
                "ineligible": 0,
                "failed_units": 0,
                "eligible_units": len(prepared["scope"]),
                "uncovered_units": 0,
                "reused_units": reused_count,
                "processed_units": 0,
                "reused_groups": 0,
                "judged_groups": 0,
                # A2/A3 extras: a re-check answers an old card, a lookup answers
                # one question, a local judgment answers one unit — none of them
                # is an adoption, and every one of them is counted here.
                "refreshed_checks": 0,
                "recheck_pending": 0,
                "recheck_reasons": {},
                "lookup_rounds": 0,
                "lookup_hits": 0,
                "lookup_refreshed": 0,
                "lookup_misses": 0,
                # Already answered with exactly this question (never re-paid),
                # skipped by the input bounds, or executed without a usable write.
                "lookup_settled": 0,
                "lookup_deferred": 0,
                "lookup_failed": 0,
                "lookup_rejected": 0,
                "budget_pending": 0,
                "local_groups": 0,
                "local_units_judged": 0,
                "local_units_oversized": 0,
                "local_units_reused": 0,
                "local_units_pending": 0,
            },
            "requests": {"generation": 0, "check": 0, "resolution": 0, "repair_rounds": 0},
            # Extra logical requests actually spent, by kind. Never shared with
            # the base generation/check/resolution counters above.
            "budget": {
                "limit": int(prepared.get("additional_work_limit") or 0),
                "used": 0,
                "by_kind": {},
            },
            # Executed lookups travel with the run: the next confirmed execution
            # inherits them, so the same question is never asked (and never paid
            # for) twice. Refused lookups are deliberately absent — they were not
            # executed and must stay available. ``lookups`` is the bounded display
            # log; ``lookup_state`` is the per-card dedup state and is *not*
            # trimmed by it, so an older answered question is never forgotten.
            "lookups": [dict(row) for row in (lookups or []) if isinstance(row, Mapping)],
            "lookup_state": concept_automation.lookup_state_of(lookup_state),
            "unit_results": [result_row(unit_id) for unit_id in prepared["scope"]],
            "not_adopted_reasons": [],
            "errors": [],
            "started_at": now_iso(),
            "finished_at": "",
        }

    def _update_prepare_record_locked(
        self,
        support: dict[str, Any],
        prepare_id: str,
        mutate: Any,
    ) -> None:
        """Mutate the frozen prepare record in place and persist it once."""

        automation = concept_automation.normalize_automation(support.get("automation"))
        record = automation.get("prepare")
        if not isinstance(record, Mapping) or str(record.get("prepare_id") or "") != prepare_id:
            raise ConflictError("准备任务已经变化，本次结果不再写入。")
        record = copy.deepcopy(dict(record))
        mutate(record)
        automation["prepare"] = record
        support["automation"] = automation
        old_support = copy.deepcopy(self.state.get("quality_support"))
        old_events = copy.deepcopy(self.state.get("events") or [])
        self._quality_commit_locked(support, old_support=old_support, old_events=old_events)

    def _mark_prepare_failed_locked(
        self, prepare_id: str, reason: str, *, status: str = "failed"
    ) -> None:
        support = normalize_quality_support(self.state.get("quality_support"))
        try:
            def mutate(record: dict[str, Any]) -> None:
                record["status"] = status if status in concept_automation.PREPARE_STATUSES else "failed"
                record["finished_at"] = now_iso()
                if reason and reason not in record.get("errors", []):
                    record.setdefault("errors", []).append(reason)

            self._update_prepare_record_locked(support, prepare_id, mutate)
        except Exception:  # pragma: no cover - best effort while unwinding
            pass

    @staticmethod
    def _mark_local_units_locked(
        record: dict[str, Any], *, oversized: int = 0, reused: int = 0
    ) -> None:
        """Record how an oversized group's units were accounted for this run.

        ``oversized`` is a unit whose complete contention set is larger than one
        request may carry (never truncated); ``reused`` is a unit an earlier
        confirmation already judged and this run hands over without asking
        again. Neither is work of this run, and neither may be reported as one.
        """

        counts = record.setdefault("counts", {})
        if oversized:
            counts["local_units_oversized"] = int(counts.get("local_units_oversized") or 0) + int(
                oversized
            )
        if reused:
            counts["local_units_reused"] = int(counts.get("local_units_reused") or 0) + int(reused)

    def _prepare_cards_stale_locked(
        self,
        cards: Mapping[str, tuple[int, str]],
        unit_hashes: Mapping[str, str],
    ) -> str:
        """Why a request's frozen input is no longer live, or "" when it still is.

        The lifecycle guard only knows the project, the mode and the prepare id,
        so a formal human edit, a protection change or a re-imported source
        leaves it untouched. Every request that may be followed by a repair round
        therefore re-checks here, immediately before each round, the card ids and
        draft revisions it was shown, their content fingerprints, their live
        protection state and the hash of every source it read.
        """

        support = normalize_quality_support(self.state.get("quality_support"))
        live_sources = self._quality_unit_sources(self.state.get("units") or [])
        cards_now = (support.get("cards") or {}) if isinstance(support, Mapping) else {}
        for card_id, pair in cards.items():
            revision, fingerprint = (int(pair[0]), str(pair[1]))
            card = cards_now.get(str(card_id))
            if not isinstance(card, dict):
                return f"请求所依据的卡片 {card_id} 已经不存在。"
            if concept_automation.is_manual_protected(card):
                return f"卡片 {card_id} 已经有人工内容，自动结果不再写入。"
            if int(card.get("draft_revision") or 0) != revision:
                return f"卡片 {card_id} 的草稿版本已经变化。"
            live_fingerprint = self._assessment_context_payload(
                dict(card.get("draft") or {}),
                unit_sources=live_sources,
                model="",
            )["content_fingerprint"]
            if not live_fingerprint or live_fingerprint != fingerprint:
                return f"卡片 {card_id} 的内容已经变化。"
        for unit_id, expected in unit_hashes.items():
            if str((live_sources.get(str(unit_id)) or ("", ""))[1]) != str(expected):
                return f"原文 {unit_id} 已经变化。"
        return ""

    @staticmethod
    def _bound_check_request(
        items: Sequence[Mapping[str, Any]],
        unit_sources: Mapping[str, tuple[str, str]],
        *,
        max_cards: int,
        max_units: int,
        max_chars: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """The longest prefix of one request that fits every input bound.

        The card count, the number of distinct source units the request would
        cite and the characters of its candidates plus those sources are all
        bounded, because one request slot says nothing about the payload size.
        The remainder is returned as "not sent this time": it stays pending and
        is carried by a later confirmation instead of being truncated inside a
        request. Items are taken in their given (sorted) order, so the split is
        deterministic and independent of dict iteration order.
        """

        kept: list[dict[str, Any]] = []
        units: list[str] = []
        chars = 0
        for item in items:
            if len(kept) >= max(1, int(max_cards)):
                break
            row_units = [
                str(unit_id)
                for unit_id in (item.get("unit_ids") or [])
                if str(unit_id) in unit_sources and str(unit_id) not in units
            ]
            row_chars = len(json.dumps(item.get("draft") or {}, ensure_ascii=False)) + sum(
                len(str((unit_sources.get(unit_id) or ("", ""))[0])) for unit_id in row_units
            )
            if len(units) + len(row_units) > max(1, int(max_units)):
                break
            if chars + row_chars > max(1, int(max_chars)):
                break
            kept.append(dict(item))
            units.extend(row_units)
            chars += row_chars
        return kept, max(0, len(items) - len(kept))

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
                self._prepare_guard_locked(prepare_id)
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
                    self._prepare_guard_locked(prepare_id)
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

                    self._update_prepare_record_locked(support, prepare_id, mutate_fail)
                return
            with self.lock:
                self._prepare_guard_locked(prepare_id)
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

                self._update_prepare_record_locked(support, prepare_id, mutate_ok)

        workers = max(
            1,
            min(
                self._resolve_parallel_batches(prepared.get("max_parallel_batches")),
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
            self._prepare_guard_locked(prepare_id)
        self._prepare_refresh_checks(prepare_id, prepared)
        self._prepare_bounded_lookup(prepare_id)
        # Freeze the related groups only now: they are derived from the cards
        # the confirmed execution just produced, not from the empty preview.
        with self.lock:
            self._prepare_guard_locked(prepare_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)

            frozen_groups: list[dict[str, Any]] = []
            reused_outcomes: dict[str, dict[str, Any]] = {}
            for group in groups:
                members = self._prepare_group_members_locked(support, group["card_ids"])
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

            self._update_prepare_record_locked(support, prepare_id, mutate_groups)
        return failures

    # ------------------------------------------------------------------
    # A2: the re-check of reused cards and the one bounded lookup
    # ------------------------------------------------------------------

    def _prepare_guard_control_locked(
        self,
        prepare_id: str,
        kind: str,
        *,
        cards: Mapping[str, tuple[int, str]] | None = None,
        unit_hashes: Mapping[str, str] | None = None,
        progress_stage: str = "",
        progress_item_id: str = "",
    ) -> RepairControl:
        """Authorize every round of one prepare request that is not a group judgment.

        A re-check or a lookup reads specific cards of the frozen scope, so the
        lifecycle identity alone is not enough: a formal human edit, a protection
        change or a re-imported source leaves that identity untouched while the
        answer the next round would produce is already about content nobody asked
        about any more. Before the first round and before **every** repair round,
        the frozen card identities (id, draft revision, content fingerprint), their
        live protection state and the hash of every source the request read are
        compared with the live project. A refusal raises :class:`ConflictError`,
        which the callers must propagate instead of recording it as a failed task.
        """

        frozen_cards = dict(cards or {})
        frozen_hashes = dict(unit_hashes or {})

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                stale = self._prepare_cards_stale_locked(frozen_cards, frozen_hashes)
                if stale:
                    raise ConflictError(f"{stale}本次请求已失效，结果不再写入。")

        return RepairControl(
            invocation_id=f"{prepare_id}:{kind}",
            kind=kind,
            before_attempt=before_attempt,
            on_progress=(
                self._quality_progress.repair_callback(
                    prepare_id, progress_stage, progress_item_id
                )
                if progress_stage and progress_item_id
                else None
            ),
        )

    def _prepare_stale_check_cards_locked(
        self,
        support: Mapping[str, Any],
        unit_sources: Mapping[str, tuple[str, str]],
        scope: Sequence[str],
        *,
        model: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Cards whose stored check cannot be used by the automatic path as is.

        Three independent reasons, none of them "has open questions":

        * ``legacy`` — the check predates the structured protocol, so there is no
          verifiable conclusion to adopt;
        * ``unverified`` — a structured assessment is present but no longer
          verifies (its evidence or its recorded content identity changed);
        * ``identity`` — it verifies, but it was produced under a different
          prompt version, a different check model or against different source
          hashes than the live ones.

        Only cards of this frozen scope, still pending review and not human-owned,
        are returned; the caller decides what to do when it cannot pay for them.
        """

        scope_set = {str(unit_id) for unit_id in scope}
        counts = {"legacy": 0, "unverified": 0, "identity": 0}
        items: list[dict[str, Any]] = []
        for card_id in sorted((support.get("cards") or {}).keys()):
            card = (support.get("cards") or {}).get(card_id)
            if not isinstance(card, dict) or str(card_id) != str(card.get("id") or card_id):
                continue
            if str(card.get("status") or "") != "pending_review":
                continue
            if concept_automation.is_manual_protected(card):
                continue
            draft = card.get("draft")
            if not isinstance(draft, Mapping):
                continue
            unit_ids = sorted(
                {
                    str(item.get("unit_id") or "")
                    for item in (draft.get("evidence") or [])
                    if isinstance(item, Mapping) and str(item.get("unit_id") or "")
                }
            )
            if not unit_ids or not (set(unit_ids) & scope_set):
                continue
            check = card.get("check") if isinstance(card.get("check"), Mapping) else None
            revision = int(card.get("draft_revision") or 0)
            if not isinstance(check, Mapping) or int(check.get("draft_revision") or 0) != revision:
                # A missing or older-revision check cannot be re-verified: it
                # never described this draft, so refreshing it would fake support.
                continue
            if str(check.get("verdict") or "") == "unchecked":
                continue
            questions = [
                str(item).strip() for item in (draft.get("open_questions") or []) if str(item).strip()
            ]
            reason = ""
            if concept_automation.assessment_of(card, unit_sources=unit_sources) is None:
                if not isinstance(check.get("automation_assessment"), Mapping) and not questions:
                    # Nothing to upgrade: a card that never raised a question keeps
                    # the historical draft path, so an older check is still exactly
                    # as usable as it was. Re-checking it would spend a request to
                    # change behaviour nobody asked to change.
                    continue
                reason = (
                    "legacy"
                    if not isinstance(check.get("automation_assessment"), Mapping)
                    else "unverified"
                )
            else:
                context = (
                    check.get("assessment_context")
                    if isinstance(check.get("assessment_context"), Mapping)
                    else {}
                )
                if str(context.get("prompt_version") or "") != PROMPT_VERSION:
                    reason = "identity"
                elif str(context.get("model") or "") != str(model or ""):
                    reason = "identity"
                else:
                    hashes = (
                        context.get("source_hashes")
                        if isinstance(context.get("source_hashes"), Mapping)
                        else {}
                    )
                    if any(
                        str(hashes.get(unit_id) or "") != (unit_sources.get(unit_id) or ("", ""))[1]
                        for unit_id in unit_ids
                    ):
                        reason = "identity"
            if not reason:
                continue
            fingerprint = self._assessment_context_payload(
                dict(draft), unit_sources=unit_sources, model=model
            )["content_fingerprint"]
            if not fingerprint:
                continue
            counts[reason] = counts.get(reason, 0) + 1
            items.append(
                {
                    "card_id": str(card_id),
                    "draft_revision": revision,
                    "content_fingerprint": fingerprint,
                    "unit_ids": unit_ids,
                    "draft": copy.deepcopy(dict(draft)),
                    "reason": reason,
                }
            )
        return items, counts

    def _prepare_refresh_checks(self, prepare_id: str, prepared: Mapping[str, Any]) -> None:
        """Re-check the reused cards whose stored check cannot be used as is.

        One bounded request per execution (``MAX_RECHECK_CARDS`` cards), issued
        through the same check provider and its bounded repair loop. Candidates
        are never regenerated: the request carries the frozen drafts and the
        page's own candidates stay exactly as they are.
        """

        scope = [str(unit_id) for unit_id in prepared.get("scope") or []]
        with self.lock:
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            _generation, checker, _editorial, _resolution = self._quality_providers()
            self._prepare_guard_locked(prepare_id)
            items, reasons = self._prepare_stale_check_cards_locked(
                support,
                unit_sources,
                scope,
                model=str(getattr(checker, "model", "") or ""),
            )
            if not items:
                self._quality_progress.change(
                    prepare_id,
                    "recheck",
                    "recheck-none",
                    "configure",
                    unit="card",
                    total=0,
                    metadata={"request_count": 0, "stale_cards": 0},
                )
                return
            items, over_limit = self._bound_check_request(
                items,
                unit_sources,
                max_cards=MAX_RECHECK_CARDS,
                max_units=MAX_CHECK_REQUEST_UNITS,
                max_chars=MAX_CHECK_REQUEST_CHARS,
            )
            recheck_total = len(items) + over_limit
            self._quality_progress.change(
                prepare_id,
                "recheck",
                "recheck-plan",
                "configure",
                unit="card",
                total=recheck_total,
                metadata={
                    "request_count": 1,
                    "selected_cards": len(items),
                    "over_limit_cards": over_limit,
                },
            )
            # ``over_limit`` cards keep their unusable check (and are therefore
            # not adopted) instead of being silently dropped, truncated inside a
            # request or answered in an unbounded run: they are recorded as work
            # the next confirmation carries.
            unit_ids = sorted({unit_id for item in items for unit_id in item["unit_ids"]})
            refs = self._concept_unit_refs_from_sources(unit_ids, unit_sources)
            project_id = str(self.state.get("project", {}).get("id") or "")
            frozen = {
                item["card_id"]: (item["draft_revision"], item["content_fingerprint"])
                for item in items
            }
            frozen_hashes = {
                unit_id: str((unit_sources.get(unit_id) or ("", ""))[1]) for unit_id in unit_ids
            }
            request = ConceptCheckRequest(
                project_id=project_id,
                batch_id=f"recheck-{prepare_id}"[:120],
                units=refs,
                candidates=tuple(copy.deepcopy(item["draft"]) for item in items),
                # The frozen cards and sources this request reads are part of its
                # authorization: every round, repairs included, re-checks them.
                control=self._prepare_guard_control_locked(
                    prepare_id,
                    "recheck",
                    cards=frozen,
                    unit_hashes=frozen_hashes,
                    progress_stage="recheck",
                    progress_item_id=f"recheck:{prepare_id}",
                ),
                expected=tuple(
                    {
                        "card_id": item["card_id"],
                        "draft_revision": item["draft_revision"],
                        "content_fingerprint": item["content_fingerprint"],
                    }
                    for item in items
                ),
            )
        recheck_item_id = f"recheck:{prepare_id}"
        self._quality_progress.change(
            prepare_id,
            "recheck",
            recheck_item_id,
            "start",
            unit="card",
            weight=len(items),
            label="复用卡片身份重查",
            metadata={"card_count": len(items), "over_limit_cards": over_limit},
            provider_channel="check",
        )
        try:
            result = checker.check_candidates(request)
            checks = list(result.checks)
            repair = result.repair
        except ConflictError:
            # A control refusal is not a failed task: the frozen input changed or
            # the run was closed, so this execution is stale and must end as one.
            self._quality_progress.change(
                prepare_id,
                "recheck",
                recheck_item_id,
                "failed",
                unit="card",
                weight=len(items),
                error="重查输入身份已经变化。",
            )
            raise
        except Exception as exc:
            with self.lock:
                self._prepare_guard_locked(prepare_id)

                def mutate_failed(record: dict[str, Any], exc: Exception = exc) -> None:
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + len(items) + over_limit
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                    record["errors"].append(f"复用单元重查失败：{str(exc)[:200]}")

                self._update_prepare_record_locked(support, prepare_id, mutate_failed)
            self._quality_progress.change(
                prepare_id,
                "recheck",
                recheck_item_id,
                "failed",
                unit="card",
                weight=len(items),
                error=f"复用单元重查失败：{str(exc)[:240]}",
            )
            return
        with self.lock:
            self._prepare_guard_locked(prepare_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources_now = self._quality_unit_sources(self.state.get("units") or [])
            cards_now = {
                str(card.get("id")): card
                for card in (support.get("cards") or {}).values()
                if isinstance(card, dict)
            }
            pairs: list[dict[str, Any]] = []
            rows: list[dict[str, Any]] = []
            mismatch = ""
            for item, check in zip(items, checks):
                if not isinstance(check, Mapping):
                    mismatch = "重查结果缺少对象。"
                    break
                card = cards_now.get(item["card_id"])
                if card is None:
                    mismatch = "重查结果指向的卡片已经不存在。"
                    break
                if int(card.get("draft_revision") or 0) != int(item["draft_revision"]):
                    mismatch = "重查结果对应的草稿版本已经变化。"
                    break
                live_fingerprint = self._assessment_context_payload(
                    dict(card.get("draft") or {}), unit_sources=unit_sources_now, model=""
                )["content_fingerprint"]
                if not live_fingerprint or live_fingerprint != item["content_fingerprint"]:
                    mismatch = "重查结果对应的卡片内容已经变化。"
                    break
                for unit_id in frozen_hashes:
                    if str((unit_sources_now.get(unit_id) or ("", ""))[1]) != frozen_hashes[unit_id]:
                        mismatch = "重查所依据的原文已经变化。"
                        break
                if mismatch:
                    break
                pairs.append(
                    {"card_id": item["card_id"], "draft_revision": item["draft_revision"]}
                )
                rows.append(
                    {
                        **dict(check),
                        "assessment_context": self._assessment_context_payload(
                            dict(card.get("draft") or {}),
                            unit_sources=unit_sources_now,
                            model=str(getattr(checker, "model", "") or ""),
                        ),
                    }
                )
            updated: list[dict[str, Any]] = []
            if not mismatch and len(rows) == len(items):
                updated = refresh_check_result(
                    support,
                    cards=pairs,
                    checks=rows,
                    now_iso_value=now_iso(),
                    is_protected=concept_automation.is_manual_protected,
                )
                if len(updated) != len(items):
                    mismatch = "重查结果未能写入（卡片已被裁决、保护或版本不一致）。"
            strict_failed = bool(mismatch) or len(updated) != len(items)

            def mutate_refreshed(
                record: dict[str, Any],
                updated=len(updated),
                over_limit=over_limit,
                reasons=reasons,
                repair=repair,
                strict_failed=strict_failed,
                mismatch=mismatch,
            ) -> None:
                record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                if repair:
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + max(0, int(repair.get("round") or 1) - 1)
                if strict_failed:
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + over_limit + (0 if mismatch else len(items))
                    record["errors"].append(f"复用单元重查未写入：{mismatch or '身份或写入保护拒绝'}")
                else:
                    record["counts"]["refreshed_checks"] = int(
                        record["counts"].get("refreshed_checks") or 0
                    ) + updated
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + over_limit
                    by_reason = dict(record["counts"].get("recheck_reasons") or {})
                    for key, value in (reasons or {}).items():
                        if value:
                            by_reason[key] = int(by_reason.get(key) or 0) + int(value)
                    record["counts"]["recheck_reasons"] = by_reason

            self._update_prepare_record_locked(support, prepare_id, mutate_refreshed)
        self._quality_progress.change(
            prepare_id,
            "recheck",
            recheck_item_id,
            "failed" if strict_failed else "complete",
            unit="card",
            weight=len(items),
            error=mismatch or "重查结果未能写入。" if strict_failed else "",
        )

    def _prepare_bounded_lookup(self, prepare_id: str) -> None:
        """Spend at most one extra request on the deterministic lookup of this run.

        The search itself is local and free: it reads the frozen scope's own
        sources with the project's orthographic rules and returns excerpts sliced
        out of the original text. The scope of this run is chosen **before** the
        request is built: a card whose exact question (content version, evidence,
        current conclusion and the material its hits were read from, keyed per
        card) was already executed is counted as settled and never asked again,
        while the rest are
        packed into one request inside the plan's input bounds (10 cards, 10
        source units, 4000 English words, 24000 characters — whichever is reached
        first). A card that does not fit is recorded as unfinished and the scan
        continues, so one oversized card cannot starve the cards behind it, and
        no evidence is ever trimmed to make a request look complete.

        The one request is charged to the shared budget; a refusal leaves every
        card pending without a single write. The answer is written only while the
        frozen identity still matches, and the state each answer leaves behind is
        recorded as "this exact question was answered" — so a lookup can never
        re-trigger itself through its own conclusion. Nothing here adopts a card,
        widens the scope or retries a failure.
        """

        with self.lock:
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            record = self._prepare_record_locked(support, {"prepare_id": prepare_id})
            state = concept_automation.lookup_state_of(record.get("lookup_state"))
            stored_plan = self._prepare_plan_payload(record.get("plan") or {})
            scope = [str(unit_id) for unit_id in stored_plan.get("scope") or []]
            self._prepare_guard_locked(prepare_id)
            scope_set = set(scope)
            candidates: list[dict[str, Any]] = []
            wanted: list[str] = []
            for card_id in sorted((support.get("cards") or {}).keys()):
                card = (support.get("cards") or {}).get(card_id)
                if not isinstance(card, dict) or str(card.get("status") or "") != "pending_review":
                    continue
                if concept_automation.is_manual_protected(card):
                    continue
                draft = card.get("draft")
                if not isinstance(draft, Mapping):
                    continue
                unit_ids = [
                    str(item.get("unit_id") or "")
                    for item in (draft.get("evidence") or [])
                    if isinstance(item, Mapping) and str(item.get("unit_id") or "")
                ]
                if not unit_ids or not (set(unit_ids) & scope_set):
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None or not assessment.get("lookup_expressions"):
                    continue
                check = card.get("check") if isinstance(card.get("check"), Mapping) else None
                fingerprint = self._assessment_context_payload(
                    dict(draft), unit_sources=unit_sources, model=""
                )["content_fingerprint"]
                if not fingerprint:
                    continue
                expressions = [
                    str(item) for item in assessment["lookup_expressions"] if str(item).strip()
                ]
                if not expressions:
                    continue
                candidates.append(
                    {
                        "card_id": str(card_id),
                        "draft_revision": int(card.get("draft_revision") or 0),
                        "content_fingerprint": fingerprint,
                        # The conclusion the card holds right now is what the
                        # lookup would ask about; it is recorded only to name the
                        # question, never as a licence for another request.
                        "assessment_fingerprint": concept_automation.assessment_fingerprint(
                            assessment
                        ),
                        # "One bounded lookup per content version": the version is
                        # the draft + its cited sources, and the check digest tells
                        # the lookup's own write apart from a formal re-check.
                        "version": concept_automation.lookup_content_version(
                            {
                                "card_id": card_id,
                                "draft_revision": int(card.get("draft_revision") or 0),
                                "content_fingerprint": fingerprint,
                                "unit_ids": unit_ids,
                            },
                            unit_sources=unit_sources,
                        ),
                        "check": concept_automation.check_fingerprint(check),
                        "draft": copy.deepcopy(dict(draft)),
                        "unit_ids": unit_ids,
                        "expressions": expressions,
                        "verdict": str((check or {}).get("verdict") or ""),
                    }
                )
                for expression in expressions:
                    if expression not in wanted:
                        wanted.append(expression)
        if not candidates:
            self._quality_progress.change(
                prepare_id,
                "lookup",
                "lookup-none",
                "configure",
                unit="request",
                total=0,
                metadata={"candidate_cards": 0, "request_count": 0},
            )
            return
        hits = concept_automation.lookup_occurrences(
            wanted,
            unit_sources=unit_sources,
            unit_ids=scope,
            max_units_per_expression=MAX_LOOKUP_UNITS_PER_EXPRESSION,
        )
        material = {expression: rows for expression, rows in hits.items() if rows}
        # Which cards still owe a lookup, which were already answered and which
        # asked for an expression the project's own text has no occurrence of.
        settled: list[str] = []
        misses: list[str] = []
        pending: list[dict[str, Any]] = []
        for candidate in candidates:
            asked = [
                expression for expression in candidate["expressions"] if expression in material
            ]
            if not asked:
                misses.append(candidate["card_id"])
                continue
            candidate["asked"] = asked
            # The material this card's lookup reads: the excerpts found for its
            # own expressions, including the hit units its evidence never
            # mentioned. Recording it is what makes a later edit of one of those
            # sources a changed question instead of a settled one.
            candidate["material_units"] = concept_automation.lookup_material_units(
                asked, material
            )
            # "疑问每个内容版本最多一次额外补查"：额度只看内容版本、检查摘要与
            # 真正读过的材料——补查自己的回答（改 guidance、轮换或新增表达）不再
            # 产生新额度。
            if concept_automation.lookup_allowance_spent(
                state,
                candidate["card_id"],
                version=candidate["version"],
                check=candidate["check"],
                unit_sources=unit_sources,
            ):
                settled.append(candidate["card_id"])
            else:
                pending.append(candidate)
        if settled or misses:
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                support = normalize_quality_support(self.state.get("quality_support"))

                def mutate_classified(
                    record: dict[str, Any], settled=len(settled), misses=len(misses)
                ) -> None:
                    if settled:
                        record["counts"]["lookup_settled"] = (
                            int(record["counts"].get("lookup_settled") or 0) + settled
                        )
                    if misses:
                        record["counts"]["lookup_misses"] = (
                            int(record["counts"].get("lookup_misses") or 0) + misses
                        )

                self._update_prepare_record_locked(support, prepare_id, mutate_classified)
        if not pending:
            self._quality_progress.change(
                prepare_id,
                "lookup",
                "lookup-none-pending",
                "configure",
                unit="request",
                total=0,
                metadata={
                    "candidate_cards": len(candidates),
                    "settled_cards": len(settled),
                    "no_hit_cards": len(misses),
                    "request_count": 0,
                },
            )
            return
        selection = concept_automation.lookup_batch(
            pending,
            unit_sources=unit_sources,
            material=material,
            limits=concept_automation.LOOKUP_REQUEST_LIMITS,
        )
        items: list[dict[str, Any]] = selection["cards"]
        deferred = [row["card_id"] for row in selection["deferred"]]
        if not items:
            # Nothing fits this execution's input bounds: the work stays
            # unfinished and is retried by a later confirmation — never cut down
            # to size and never sent as a partial question.
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                support = normalize_quality_support(self.state.get("quality_support"))

                def mutate_deferred(record: dict[str, Any], count=len(pending)) -> None:
                    record["counts"]["lookup_deferred"] = (
                        int(record["counts"].get("lookup_deferred") or 0) + int(count)
                    )

                self._update_prepare_record_locked(support, prepare_id, mutate_deferred)
            self._quality_progress.change(
                prepare_id,
                "lookup",
                "lookup-pending",
                "configure",
                unit="request",
                total=1,
                metadata={
                    "candidate_cards": len(pending),
                    "deferred_cards": len(deferred),
                    "request_count": 0,
                },
            )
            return
        # Every unit this request will cite: the cards' own evidence units plus
        # the units the read-only hits were quoted from. A hit whose source is not
        # cited could not be used by the answer, so both travel together.
        cited_units = {unit for item in items for unit in item["unit_ids"]}
        for item in items:
            for expression in item["asked"]:
                for row in material[expression]:
                    cited_units.add(str(row.get("unit_id") or ""))
        request_units = [
            unit_id
            for unit_id in scope
            if unit_id in cited_units and unit_id in unit_sources
        ]
        request_expressions = sorted(
            {expression for item in items for expression in item["asked"]}
        )
        request_material = {
            expression: material[expression] for expression in request_expressions
        }
        # The request-level identity of the display log: what this one request
        # asked. It never decides anything (the per-card state does).
        request_identity = concept_automation.lookup_identity(
            request_expressions, items, request_material
        )
        with self.lock:
            self._prepare_guard_locked(prepare_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            _generation, checker, _editorial, _resolution = self._quality_providers()
            charged = False

            def mutate_charge(record: dict[str, Any]) -> None:
                nonlocal charged
                charged = concept_automation.charge_budget(record, "lookup")

            self._update_prepare_record_locked(support, prepare_id, mutate_charge)
            if not charged:
                # Zero-modification refusal: the record only learns why the work
                # is still pending, exactly like an unpaid large-group judgment.
                # Nothing was executed, so nothing is recorded as asked.

                def mutate_unpaid(
                    record: dict[str, Any], count=len(pending), deferred=len(deferred)
                ) -> None:
                    record["counts"]["budget_pending"] = (
                        int(record["counts"].get("budget_pending") or 0) + int(count)
                    )
                    if deferred:
                        record["counts"]["lookup_deferred"] = (
                            int(record["counts"].get("lookup_deferred") or 0) + int(deferred)
                        )

                self._update_prepare_record_locked(support, prepare_id, mutate_unpaid)
                self._quality_progress.change(
                    prepare_id,
                    "lookup",
                    "lookup-pending",
                    "configure",
                    unit="request",
                    total=1,
                    metadata={
                        "candidate_cards": len(pending),
                        "selected_cards": len(items),
                        "deferred_cards": len(deferred),
                        "budget_refused": True,
                        "request_count": 0,
                    },
                )
                return
            refs = self._concept_unit_refs_from_sources(request_units, unit_sources)
            project_id = str(self.state.get("project", {}).get("id") or "")
            frozen_hashes = {
                unit_id: str((unit_sources.get(unit_id) or ("", ""))[1])
                for unit_id in request_units
            }
            frozen_cards = {
                item["card_id"]: (item["draft_revision"], item["content_fingerprint"])
                for item in items
            }
            request = ConceptCheckRequest(
                project_id=project_id,
                batch_id=f"lookup-{prepare_id}"[:120],
                units=refs,
                candidates=tuple(copy.deepcopy(item["draft"]) for item in items),
                # The cards and the hit sources this lookup quotes are part of its
                # authorization: a change to either stops the next round before it
                # is sent, and stops the write even after the answer arrives.
                control=self._prepare_guard_control_locked(
                    prepare_id, "lookup", cards=frozen_cards, unit_hashes=frozen_hashes
                ),
                lookup_evidence=copy.deepcopy(request_material),
                expected=tuple(
                    {
                        "card_id": item["card_id"],
                        "draft_revision": item["draft_revision"],
                        "content_fingerprint": item["content_fingerprint"],
                    }
                    for item in items
                ),
            )
        lookup_item_id = f"lookup:{prepare_id}"
        self._quality_progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "configure",
            unit="request",
            total=1,
            metadata={
                "candidate_cards": len(pending),
                "selected_cards": len(items),
                "deferred_cards": len(deferred),
                "request_count": 1,
            },
        )
        self._quality_progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "start",
            unit="request",
            label="核实候选疑问",
            metadata={
                "card_count": len(items),
                "unit_count": len(request_units),
                "expression_count": len(request_expressions),
            },
            provider_channel="check",
        )
        asked_pairs = [
            {
                "card_id": item["card_id"],
                "version": item["version"],
                "check": item["check"],
                "expressions": list(item["asked"]),
                "units": list(item["material_units"]),
                "status": "failed",
            }
            for item in items
        ]
        try:
            result = checker.check_candidates(request)
            checks = list(result.checks)
            repair = result.repair
        except ConflictError:
            # A control refusal is not a failed task: the frozen input changed or
            # the run was closed, so this execution is stale and must end as one.
            self._quality_progress.change(
                prepare_id,
                "lookup",
                lookup_item_id,
                "failed",
                unit="request",
                error="补查输入身份已经变化。",
            )
            raise
        except Exception as exc:
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                support = normalize_quality_support(self.state.get("quality_support"))

                def mutate_failed(
                    record: dict[str, Any],
                    exc: Exception = exc,
                    pairs=asked_pairs,
                    cards=[item["card_id"] for item in items],
                    expressions=request_expressions,
                    deferred=len(deferred),
                    request_identity=request_identity,
                ) -> None:
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                    record["counts"]["lookup_failed"] = int(
                        record["counts"].get("lookup_failed") or 0
                    ) + len(cards)
                    record["errors"].append(f"有界补查失败（不再重试）：{str(exc)[:200]}")
                    # The failure was *executed*: it is recorded with its identity
                    # so the identical input does not turn into an endless retry.
                    # A changed content or evidence identity is a new question and
                    # may be looked up again.
                    self._record_lookup_state_locked(
                        record,
                        pairs=pairs,
                        live_cards=[
                            str(card.get("id"))
                            for card in (support.get("cards") or {}).values()
                            if isinstance(card, dict)
                        ],
                    )
                    if deferred:
                        record["counts"]["lookup_deferred"] = (
                            int(record["counts"].get("lookup_deferred") or 0) + int(deferred)
                        )
                    self._append_lookup_ledger_locked(
                        record,
                        identity=request_identity,
                        status="failed",
                        card_ids=cards,
                        expressions=expressions,
                    )

                self._update_prepare_record_locked(support, prepare_id, mutate_failed)
            self._quality_progress.change(
                prepare_id,
                "lookup",
                lookup_item_id,
                "failed",
                unit="request",
                error=f"有界补查失败：{str(exc)[:240]}",
            )
            return
        with self.lock:
            self._prepare_guard_locked(prepare_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            unit_sources_now = self._quality_unit_sources(self.state.get("units") or [])
            cards_now = {
                str(card.get("id")): card
                for card in (support.get("cards") or {}).values()
                if isinstance(card, dict)
            }
            pairs: list[dict[str, Any]] = []
            rows: list[dict[str, Any]] = []
            mismatch = ""
            for item, check in zip(items, checks):
                if not isinstance(check, Mapping):
                    mismatch = "补查结果缺少对象。"
                    break
                card = cards_now.get(item["card_id"])
                if card is None or int(card.get("draft_revision") or 0) != int(
                    item["draft_revision"]
                ):
                    mismatch = "补查期间草稿版本已经变化。"
                    break
                live_fingerprint = self._assessment_context_payload(
                    dict(card.get("draft") or {}), unit_sources=unit_sources_now, model=""
                )["content_fingerprint"]
                if not live_fingerprint or live_fingerprint != item["content_fingerprint"]:
                    mismatch = "补查期间卡片内容已经变化。"
                    break
                if any(
                    str((unit_sources_now.get(unit_id) or ("", ""))[1]) != frozen_hashes[unit_id]
                    for unit_id in frozen_hashes
                ):
                    mismatch = "补查所依据的原文已经变化。"
                    break
                pairs.append(
                    {"card_id": item["card_id"], "draft_revision": item["draft_revision"]}
                )
                rows.append(
                    {
                        **dict(check),
                        "assessment_context": self._assessment_context_payload(
                            dict(card.get("draft") or {}),
                            unit_sources=unit_sources_now,
                            model=str(getattr(checker, "model", "") or ""),
                        ),
                    }
                )
            updated: list[dict[str, Any]] = []
            if not mismatch and len(rows) == len(items):
                updated = refresh_check_result(
                    support,
                    cards=pairs,
                    checks=rows,
                    now_iso_value=now_iso(),
                    is_protected=concept_automation.is_manual_protected,
                )
            written = {str(card.get("id")) for card in updated}
            # Every card of this request gets a recorded state: the written ones
            # under the identity their *new* conclusion defines (that is what
            # makes the lookup recognise its own answer next time), the rest under
            # the identity they were asked with (executed, but not written).
            state_updates: list[dict[str, Any]] = []
            for item in items:
                card_id = item["card_id"]
                settled_check = item["check"]
                if card_id in written:
                    card = cards_now.get(card_id) or {}
                    settled_check = concept_automation.check_fingerprint(
                        card.get("check") if isinstance(card.get("check"), Mapping) else None
                    )
                state_updates.append(
                    {
                        "card_id": card_id,
                        "version": item["version"],
                        "check": settled_check,
                        "expressions": list(item["asked"]),
                        # The material this execution read, from the sources the
                        # request was built on — an answer cannot rewrite what it
                        # was quoted from, so this stays external to its write.
                        "units": list(item["material_units"]),
                        "status": "completed" if card_id in written else "rejected",
                    }
                )

            def mutate_lookup(
                record: dict[str, Any],
                updates=state_updates,
                written=len(written),
                target=len(items),
                material=request_material,
                mismatch=mismatch,
                repair=repair,
                deferred=len(deferred),
                expressions=request_expressions,
                card_ids=[item["card_id"] for item in items],
                live=[str(card.get("id")) for card in cards_now.values()],
                request_identity=request_identity,
            ) -> None:
                record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                if repair:
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + max(0, int(repair.get("round") or 1) - 1)
                record["counts"]["lookup_rounds"] = int(
                    record["counts"].get("lookup_rounds") or 0
                ) + 1
                record["counts"]["lookup_hits"] = int(
                    record["counts"].get("lookup_hits") or 0
                ) + sum(len(rows) for rows in material.values())
                if deferred:
                    record["counts"]["lookup_deferred"] = int(
                        record["counts"].get("lookup_deferred") or 0
                    ) + int(deferred)
                self._record_lookup_state_locked(record, pairs=updates, live_cards=live)
                if written:
                    record["counts"]["lookup_refreshed"] = int(
                        record["counts"].get("lookup_refreshed") or 0
                    ) + int(written)
                if target - written:
                    # The answer arrived but these cards could not take it
                    # (decided, protected or changed meanwhile): executed, not
                    # written, and never reported as refreshed.
                    record["counts"]["lookup_rejected"] = int(
                        record["counts"].get("lookup_rejected") or 0
                    ) + (target - written)
                if mismatch:
                    record["errors"].append(f"有界补查未写入：{mismatch}")
                status_now = (
                    "completed" if written == target else ("rejected" if not written else "partial")
                )
                self._append_lookup_ledger_locked(
                    record,
                    identity=request_identity,
                    status=status_now,
                    card_ids=card_ids,
                    expressions=expressions,
                )

            self._update_prepare_record_locked(support, prepare_id, mutate_lookup)
        self._quality_progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "failed" if mismatch else "complete",
            unit="request",
            error=f"有界补查未写入：{mismatch}" if mismatch else "",
        )

    @staticmethod
    def _append_lookup_ledger_locked(
        record: dict[str, Any],
        *,
        identity: str,
        status: str,
        card_ids: Sequence[str],
        expressions: Sequence[str],
    ) -> None:
        """Add one line to the bounded **display** log of executed lookups.

        The log is what the page reads: the request-level identity of what was
        asked and how it ended (completed / partial / failed / rejected). It is
        trimmed to ``MAX_LOOKUP_LEDGER`` rows, and it deliberately holds no
        decision: whether a question has already been answered is decided by the
        per-card dedup state, so trimming this log can never make the project pay
        for the same work twice. A refused request is never logged — it was not
        executed.
        """

        if not identity:
            return
        rows = [
            row
            for row in (record.get("lookups") or [])
            if isinstance(row, Mapping) and str(row.get("identity") or "") != str(identity)
        ]
        rows = rows[-(concept_automation.MAX_LOOKUP_LEDGER - 1) :]
        rows.append(
            {
                "identity": str(identity),
                "status": str(status or "failed"),
                "cards": [str(item) for item in card_ids][:40],
                "expressions": [str(item) for item in expressions][:16],
                "prepare_id": str(record.get("prepare_id") or ""),
                "at": now_iso(),
            }
        )
        record["lookups"] = rows

    def _record_lookup_state_locked(
        self,
        record: dict[str, Any],
        *,
        pairs: Sequence[Mapping[str, Any]],
        live_cards: Sequence[str],
    ) -> None:
        """Record which card spent its one bounded lookup, and how it ended.

        This is the state that stops a lookup from being paid for twice — not the
        display log: the log is trimmed for the page, this map keeps one row per
        card and content version, so an answer that rewrites its own conclusion or
        proposes new expressions never mints a new allowance, while a changed
        draft, source, read material or formally replaced conclusion still does.
        Rows of cards that no longer exist are dropped.
        """

        state = concept_automation.lookup_state_of(record.get("lookup_state"))
        stamp = now_iso()
        for pair in pairs:
            concept_automation.lookup_state_set(
                state,
                str(pair.get("card_id") or ""),
                version=str(pair.get("version") or ""),
                check=str(pair.get("check") or ""),
                expressions=[str(item) for item in (pair.get("expressions") or [])],
                units=[
                    dict(item)
                    for item in (pair.get("units") or [])
                    if isinstance(item, Mapping)
                ],
                status=str(pair.get("status") or "failed"),
                at=stamp,
            )
        record["lookup_state"] = concept_automation.lookup_state_pruned(state, live_cards)

    def _prepare_resolve_pending(self, prepare_id: str) -> None:
        """Judge every pending related group through the resolution channel."""

        with self.lock:
            support = normalize_quality_support(self.state.get("quality_support"))
            record = self._prepare_record_locked(support, {"prepare_id": prepare_id})
            stored_plan = self._prepare_plan_payload(record.get("plan") or {})
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            resolved = dict(stored_plan.get("resolved_groups") or {})
            scope_units = [str(unit_id) for unit_id in stored_plan.get("scope") or []]
            pending: list[dict[str, Any]] = []
            # A3: oversized groups are no longer skipped as a whole. Their local
            # contention sets are judged per unit, so a unit whose complete set
            # fits the call budget still gets a judgment while the rest keeps
            # waiting — nothing is truncated and no member is pre-filtered.
            local_pending: list[dict[str, Any]] = []
            local_oversized: set[str] = set()
            local_reused = 0
            local_total = 0
            for group in stored_plan.get("groups") or []:
                existing = resolved.get(group["group_id"])
                # A group that already carries a whole-group relation is done. An
                # oversized one is not: its outcome may be a per-unit partial
                # result, and the units it could not pay for are still owed a
                # judgment. Those are continued below instead of being reported as
                # finished, and the units already judged are handed over as is.
                if existing is not None and not group.get("oversized"):
                    continue
                # The frozen members are what the judgment may read. A group
                # frozen before this round may legitimately have no members yet.
                members = group.get("members")
                if not isinstance(members, list) or not members:
                    members = self._prepare_group_members_locked(support, group.get("card_ids") or [])
                # The input was frozen right after the batches. If the live cards
                # or sources no longer match it, the judgment would be computed
                # on something the operator already changed: refuse, don't guess.
                frozen = str(group.get("input_fingerprint") or "")
                live = concept_automation.group_input_fingerprint(
                    group["group_id"], members, support, unit_sources
                )
                if frozen and frozen != live:
                    raise ConflictError(
                        "组辨析开始前，相关卡片或原文已经变化，本次准备结果已失效。"
                    )
                if group.get("oversized"):
                    # A3: judged whole is impossible, judged per unit is not. The
                    # contention set of one unit keeps every member with valid
                    # evidence for it — supported, disputed and undecided alike —
                    # so a local judgment can never be "conflict-free" because a
                    # member was filtered out first.
                    plans = concept_automation.local_contention_plans(
                        group["group_id"],
                        members,
                        unit_sources=unit_sources,
                        unit_ids=scope_units,
                    )
                    prior_units = concept_automation.local_judgment_payloads(existing)
                    local_total += len(plans["requests"]) + len(plans["oversized_units"]) + len(prior_units)
                    remaining = [
                        row for row in plans["requests"] if str(row["unit_id"]) not in prior_units
                    ]
                    local_oversized.update(plans["oversized_units"])
                    # Units judged by an earlier confirmation are handed over
                    # without a new request; they are counted as reused work, not
                    # as work of this run.
                    local_reused += len(prior_units)
                    if remaining:
                        local_pending.append(
                            {
                                **group,
                                "members": members,
                                "input_fingerprint": frozen or live,
                                "local": {
                                    "requests": remaining,
                                    "oversized_units": plans["oversized_units"],
                                },
                                "prior_units": prior_units,
                            }
                        )
                    continue
                if len(members) < 2:
                    resolved[group["group_id"]] = {
                        "relation": "skipped",
                        "reason": "组内没有两张可自动管理的卡片。",
                        "payload": {},
                    }
                    continue
                # A judgment can only matter when at least one member could still
                # be adopted: a card whose check failed, never existed, or whose
                # evidence is outside this scope is unadoptable before the group
                # is even read. Judging such a group spends a call that cannot
                # produce a committable reference (C2: 19 of the 21 groups that
                # needed a judgment were exactly this), while a group with one
                # usable member keeps being judged as before.
                cards_now = support.get("cards") or {}
                if not any(
                    concept_automation.adoption_eligibility(
                        cards_now.get(str(member.get("card_id") or "")) or {},
                        unit_sources=unit_sources,
                        unit_ids=scope_units,
                    )[0]
                    == "adopt"
                    for member in members
                ):
                    resolved[group["group_id"]] = {
                        "relation": "skipped",
                        "reason": (
                            "组内没有可用于自动采用的检查结论"
                            "（检查未完成、未通过或证据不在本次范围内），本次不辨析。"
                        ),
                        "payload": {},
                    }
                    continue
                pending.append({**group, "members": members, "input_fingerprint": frozen or live})
            if resolved != (stored_plan.get("resolved_groups") or {}):
                stored_plan["resolved_groups"] = resolved
                # persist the skipped ones so the plan stays the single source
                with self.lock:
                    self._update_prepare_plan_locked(prepare_id, stored_plan)
            if local_oversized or local_reused:
                self._update_prepare_record_locked(
                    support,
                    prepare_id,
                    lambda record: self._mark_local_units_locked(
                        record, oversized=len(local_oversized), reused=local_reused
                    ),
                )
            normal_groups = [
                group for group in stored_plan.get("groups") or [] if not group.get("oversized")
            ]
            normal_pending_ids = {str(group.get("group_id") or "") for group in pending}
            normal_resolved = dict(stored_plan.get("resolved_groups") or {})
            normal_reused = sum(
                1
                for group in normal_groups
                if isinstance(normal_resolved.get(str(group.get("group_id") or "")), Mapping)
                and normal_resolved[str(group.get("group_id") or "")].get("reused") is True
            )
            normal_failed = sum(
                1
                for group in normal_groups
                if str((normal_resolved.get(str(group.get("group_id") or "")) or {}).get("relation") or "")
                in {"failed", "unresolved"}
                and str(group.get("group_id") or "") not in normal_pending_ids
            )
            normal_not_required = sum(
                1
                for group in normal_groups
                if str((normal_resolved.get(str(group.get("group_id") or "")) or {}).get("relation") or "")
                == "skipped"
            )
            normal_completed = max(
                0,
                len(normal_groups)
                - len(normal_pending_ids)
                - normal_reused
                - normal_failed
                - normal_not_required,
            )
            self._quality_progress.change(
                prepare_id,
                "group_resolution",
                "group-plan",
                "configure",
                unit="group",
                total=len(normal_groups),
                metadata={
                    "completed": normal_completed,
                    "failed": normal_failed,
                    "reused": normal_reused,
                    "not_required": normal_not_required,
                    "provider_group_count": len(pending),
                },
            )
            self._quality_progress.change(
                prepare_id,
                "local_resolution",
                "local-plan",
                "configure",
                unit="group_unit",
                total=local_total,
                metadata={
                    "reused": local_reused,
                    "oversized_units": len(local_oversized),
                    "request_count": sum(
                        len(item.get("local", {}).get("requests") or []) for item in local_pending
                    ),
                },
            )
            if not pending and not local_pending:
                return

        _generation, _checker, _editorial, resolver = self._quality_providers()
        for group in pending:
            with self.lock:
                self._prepare_guard_locked(prepare_id)
            unit_refs = {}
            for member in group["members"]:
                for evidence in (member["payload"].get("evidence") or []):
                    unit_id = str(evidence.get("unit_id") or "")
                    if unit_id and unit_id in unit_sources:
                        unit_refs[unit_id] = unit_sources[unit_id]
            request = ConceptResolutionRequest(
                project_id=str(self.state.get("project", {}).get("id") or ""),
                group_id=group["group_id"],
                members=tuple(group["members"]),
                unit_sources=unit_refs,
                input_fingerprint=group["input_fingerprint"],
                # Every model request of the judgment — the first and every
                # repair round — is authorized by the live prepare before it
                # is sent.
                control=self._prepare_group_control_locked(
                    prepare_id,
                    group["group_id"],
                    group["input_fingerprint"],
                    progress_stage="group_resolution",
                    progress_item_id=str(group["group_id"]),
                ),
            )
            group_id = str(group["group_id"])
            self._quality_progress.change(
                prepare_id,
                "group_resolution",
                group_id,
                "start",
                unit="group",
                label="相关概念辨析",
                metadata={"group_id": group_id, "member_count": len(group["members"])},
                provider_channel="resolution",
            )
            request_error = ""
            repair_rounds = 0
            outcome: dict[str, Any]
            try:
                result = resolver.resolve_group(request)
                # A double that reports its own call count must be read once.
            except ConflictError:
                # The judgment was invalidated while it was in flight (or the
                # controller refused the next request): this is not a group
                # failure to record, it ends the whole run as stale.
                self._quality_progress.change(
                    prepare_id,
                    "group_resolution",
                    group_id,
                    "failed",
                    unit="group",
                    error="组辨析输入身份已经变化。",
                )
                raise
            except Exception as exc:
                outcome = {"relation": "failed", "reason": f"组辨析失败：{exc}", "payload": {}}
                request_error = str(exc)
                # A failed judgment still spent its content rounds. The repair
                # summary travels on the exception, so the counter can report
                # them instead of a bare zero that hides real calls.
                spent = getattr(exc, "outcome", None)
                if spent is not None:
                    repair_rounds = max(0, int(getattr(spent, "round", 1) or 1) - 1)
            else:
                try:
                    validated = normalize_resolution(
                        result.payload,
                        member_ids={str(item["card_id"]) for item in group["members"]},
                        group_id=group["group_id"],
                        source_units=unit_refs,
                    )
                except Exception as exc:
                    outcome = {"relation": "unresolved", "reason": f"组辨析结果未通过本地校验：{exc}", "payload": {}}
                else:
                    outcome = {
                        "relation": str(validated["relation"]),
                        "reason": "",
                        "payload": validated,
                        "repair": result.repair,
                    }
                    # ``repair`` reports the round that produced the answer, so
                    # the repair rounds are the extra content rounds beyond the
                    # first attempt (round 1 means none were needed).
                    repair_rounds = max(0, int(((result.repair or {}).get("round") or 1)) - 1)
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                # The result may only be stored while the input is still the one
                # the model was shown. A formal human edit, an approval or a
                # changed source in the meantime makes this outcome stale.
                self._prepare_group_fresh_locked(
                    prepare_id, group["group_id"], group["input_fingerprint"]
                )
                support = normalize_quality_support(self.state.get("quality_support"))

                def mutate_group(
                    record: dict[str, Any],
                    group=group,
                    outcome=outcome,
                    request_error=request_error,
                    repair_rounds=repair_rounds,
                ) -> None:
                    record["requests"]["resolution"] = int(record["requests"].get("resolution") or 0) + 1
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + repair_rounds
                    plan = record.get("plan") or {}
                    resolved = dict(plan.get("resolved_groups") or {})
                    resolved[group["group_id"]] = outcome
                    plan["resolved_groups"] = resolved
                    record["plan"] = plan
                    if request_error:
                        record["errors"].append(f"组 {group['group_id']}：{request_error}")

                self._update_prepare_record_locked(support, prepare_id, mutate_group)
            self._quality_progress.change(
                prepare_id,
                "group_resolution",
                group_id,
                "failed" if request_error or str(outcome.get("relation") or "") == "unresolved" else "complete",
                unit="group",
                error=request_error or str(outcome.get("reason") or "")
                if request_error or str(outcome.get("relation") or "") == "unresolved"
                else "",
            )

        # A3: the per-unit local judgments of the oversized groups. Each request
        # carries the unit's **complete** contention set and only that unit's
        # source, so the answer can neither speak for another unit nor for a
        # member that was left out. Every request is paid for from the one shared
        # budget before it is sent; a refusal leaves the unit pending, never
        # judged by a smaller set.
        for item in local_pending:
            group = item
            prior_units = dict(item.get("prior_units") or {})
            judgements: list[dict[str, Any]] = []
            judged_units: list[str] = []
            pending_units: list[str] = []
            failures: list[str] = []
            local_completed_items: list[tuple[str, str]] = []
            for plan_row in item["local"]["requests"]:
                unit_id = str(plan_row["unit_id"])
                member_ids = {str(card_id) for card_id in plan_row["member_ids"]}
                members = [
                    member
                    for member in item["members"]
                    if str(member.get("card_id") or "") in member_ids
                ]
                with self.lock:
                    self._prepare_guard_locked(prepare_id)
                    charged = False

                    def mutate_charge(record: dict[str, Any]) -> None:
                        nonlocal charged
                        charged = concept_automation.charge_budget(record, "local_group")

                    self._update_prepare_record_locked(support, prepare_id, mutate_charge)
                if not charged:
                    pending_units.append(unit_id)
                    continue
                if unit_id not in unit_sources:
                    pending_units.append(unit_id)
                    continue
                request = ConceptResolutionRequest(
                    project_id=str(self.state.get("project", {}).get("id") or ""),
                    group_id=item["group_id"],
                    members=tuple(members),
                    unit_sources={unit_id: unit_sources[unit_id]},
                    input_fingerprint=item["input_fingerprint"],
                    control=self._prepare_group_control_locked(
                        prepare_id,
                        item["group_id"],
                        item["input_fingerprint"],
                        suffix=f":{unit_id}",
                        progress_stage="local_resolution",
                        progress_item_id=f"{item['group_id']}:{unit_id}",
                    ),
                )
                local_item_id = f"{item['group_id']}:{unit_id}"
                self._quality_progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "start",
                    unit="group_unit",
                    label="大组单元局部辨析",
                    metadata={
                        "group_id": str(item["group_id"]),
                        "unit_id": unit_id,
                        "member_count": len(members),
                    },
                    provider_channel="resolution",
                )
                try:
                    result = resolver.resolve_group(request)
                except ConflictError:
                    self._quality_progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error="局部辨析输入身份已经变化。",
                    )
                    raise
                except Exception as exc:
                    failures.append(f"局部辨析失败（{unit_id}）：{exc}")
                    self._quality_progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error=str(exc),
                    )
                    continue
                try:
                    validated = normalize_resolution(
                        result.payload,
                        member_ids=member_ids,
                        group_id=item["group_id"],
                        source_units={unit_id: unit_sources[unit_id]},
                    )
                except Exception as exc:
                    failures.append(f"局部辨析结果未通过本地校验（{unit_id}）：{exc}")
                    self._quality_progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error=str(exc),
                    )
                    continue
                judgements.append({"unit_id": unit_id, "payload": validated})
                judged_units.append(unit_id)
                local_completed_items.append((local_item_id, unit_id))
                self._quality_progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "awaiting_commit",
                    unit="group_unit",
                )
            with self.lock:
                self._prepare_guard_locked(prepare_id)
                self._prepare_group_fresh_locked(
                    prepare_id, item["group_id"], item["input_fingerprint"]
                )
                support = normalize_quality_support(self.state.get("quality_support"))
                # The units judged by an earlier confirmation are merged back in
                # unchanged: this run pays only for the units it actually asked
                # about, and the group's conclusion stays the union of what every
                # per-unit judgment really said.
                new_units = {
                    str(row["unit_id"]): row["payload"]
                    for row in judgements
                    if str(row["unit_id"])
                }
                all_units = {**prior_units, **new_units}
                merged = concept_automation.merge_local_judgments(
                    item["group_id"],
                    judgements=[
                        {"unit_id": unit_id, "payload": payload}
                        for unit_id, payload in all_units.items()
                    ],
                    members=item["members"],
                )
                outcome = {
                    "relation": str((merged or {}).get("relation") or "oversized"),
                    "reason": (
                        ""
                        if merged
                        else "该组过大，只有部分单元能在预算内完成局部辨析；未判断的单元保持待确认。"
                    ),
                    "payload": merged or {},
                    "local": True,
                    # Per-unit judgments, kept apart so the next confirmation can
                    # continue exactly where this one stopped.
                    "units": copy.deepcopy(all_units),
                    "units_judged": sorted(all_units),
                    "units_oversized": sorted(item["local"]["oversized_units"]),
                    "units_pending": sorted(pending_units),
                }
                if not merged and not judged_units and not failures and not prior_units:
                    # Nothing was judged and nothing new is known: keep the group
                    # exactly as an oversized one instead of writing an outcome.
                    outcome = {}

                def mutate_local(
                    record: dict[str, Any],
                    outcome=outcome,
                    judged=judged_units,
                    pending_units=pending_units,
                    failures=failures,
                ) -> None:
                    if outcome:
                        plan = record.get("plan") or {}
                        resolved_now = dict(plan.get("resolved_groups") or {})
                        resolved_now[item["group_id"]] = copy.deepcopy(outcome)
                        plan["resolved_groups"] = resolved_now
                        record["plan"] = plan
                        record["counts"]["local_groups"] = int(
                            record["counts"].get("local_groups") or 0
                        ) + 1
                    if judged:
                        record["counts"]["local_units_judged"] = int(
                            record["counts"].get("local_units_judged") or 0
                        ) + len(judged)
                    if pending_units:
                        # An unpayable unit is recorded as unfinished work, even
                        # when no conclusion was written at all: a run that could
                        # not afford the judgment must not look like one that
                        # found nothing to judge.
                        record["counts"]["local_units_pending"] = int(
                            record["counts"].get("local_units_pending") or 0
                        ) + len(pending_units)
                        record["counts"]["budget_pending"] = int(
                            record["counts"].get("budget_pending") or 0
                        ) + len(pending_units)
                    if judged:
                        record["requests"]["resolution"] = int(
                            record["requests"].get("resolution") or 0
                        ) + len(judged)
                    for failure in failures:
                        record["errors"].append(failure)

                self._update_prepare_record_locked(support, prepare_id, mutate_local)
            for local_item_id, unit_id in local_completed_items:
                self._quality_progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "complete",
                    unit="group_unit",
                    metadata={"unit_id": unit_id},
                )

    def _update_prepare_plan_locked(self, prepare_id: str, plan: Mapping[str, Any]) -> None:
        support = normalize_quality_support(self.state.get("quality_support"))

        def mutate(record: dict[str, Any]) -> None:
            record["plan"] = dict(plan)

        self._update_prepare_record_locked(support, prepare_id, mutate)

    def _prepare_plan_payload(
        self,
        plan: Mapping[str, Any],
        *,
        support: Mapping[str, Any] | None = None,
        unit_sources: Mapping[str, tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """The stored plan record with a stable, validated shape.

        Accepts both a bare plan and the persisted prepare block (``plan``
        nested under ``plan``) so every caller shares one validation path.
        """

        nested = plan.get("plan")
        if isinstance(nested, Mapping):
            plan = nested
        scope = [str(item) for item in (plan.get("scope") or []) if str(item)]
        batches = [
            {"batch_id": str(item.get("batch_id") or ""), "unit_ids": [str(u) for u in item.get("unit_ids") or []]}
            for item in (plan.get("batches") or [])
            if isinstance(item, Mapping)
        ]
        resolved_groups = {
            str(key): copy.deepcopy(dict(value))
            for key, value in (plan.get("resolved_groups") or {}).items()
            if isinstance(value, Mapping)
        }
        cards = (support or {}).get("cards") or {}
        groups: list[dict[str, Any]] = []
        for item in plan.get("groups") or []:
            if not isinstance(item, Mapping):
                continue
            group_id = str(item.get("group_id") or "")
            card_ids = [str(c) for c in item.get("card_ids") or []]
            group: dict[str, Any] = {
                "group_id": group_id,
                "card_ids": card_ids,
                "oversized": bool(item.get("oversized")),
            }
            members = item.get("members")
            if isinstance(members, list) and members:
                group["members"] = copy.deepcopy(members)
            fingerprint = str(item.get("input_fingerprint") or "")
            if fingerprint:
                group["input_fingerprint"] = fingerprint
            groups.append(group)
        unit_states = {
            str(unit_id): {
                "state": str(row.get("state") or ""),
                "reason": str(row.get("reason") or ""),
            }
            for unit_id, row in (plan.get("unit_states") or {}).items()
            if isinstance(row, Mapping)
        }
        return {
            "prepare_id": str(plan.get("prepare_id") or ""),
            "scope": scope,
            "scope_fingerprint": str(plan.get("scope_fingerprint") or ""),
            "batches": batches,
            "groups": groups,
            "resolved_groups": resolved_groups,
            "reused_units": [str(item) for item in (plan.get("reused_units") or []) if str(item)],
            "unit_states": unit_states,
            "baseline_revision": int(plan.get("baseline_revision") or 0),
            "baseline_approved_version": int(plan.get("baseline_approved_version") or 0),
            "max_source_words": int(plan.get("max_source_words") or DEFAULT_SCAN_SOURCE_WORDS),
            # Worker count of the confirmed run: frozen with the plan for the same
            # reason the batch size is, so the previewed number is what executes.
            "max_parallel_batches": max(1, int(plan.get("max_parallel_batches") or 1)),
            # The confirmed execution's extra-request budget is frozen with the
            # plan: the record reads it back from here, so a page that previews
            # one number cannot execute another. Missing means "the documented
            # default", exactly like the preview shows.
            "additional_work_limit": int(
                plan.get("additional_work_limit")
                if plan.get("additional_work_limit") is not None
                else DEFAULT_ADDITIONAL_WORK_LIMIT
            ),
        }

    def _prepare_group_members_locked(
        self, support: Mapping[str, Any], card_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """The card payloads one group judgment may read (manual cards excluded)."""

        cards = support.get("cards") or {}
        members: list[dict[str, Any]] = []
        for card_id in card_ids:
            card = cards.get(str(card_id))
            if not isinstance(card, dict) or concept_automation.is_manual_protected(card):
                continue
            content = card.get("draft") or card.get("approved") or {}
            members.append(
                {
                    "card_id": str(card_id),
                    "content_revision": int(card.get("draft_revision") or 0),
                    "payload": copy.deepcopy(dict(content)),
                }
            )
        return members

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
            record = self._prepare_record_locked(support, plan)
        self._prepare_resolve_pending(record["prepare_id"])
        return self._prepare_view_locked(prepare_id=record["prepare_id"])

    def _quality_prepare_commit(
        self,
        *,
        plan: Mapping[str, Any],
        expected_project_id: str | None,
        expected_revision: int | None,
        internal: bool = False,
    ) -> dict[str, Any]:
        """Validate the finished preparation and store the automatic decisions."""

        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            support = normalize_quality_support(self.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            record = self._prepare_record_locked(support, plan)
            if str(record.get("mode") or "") != concept_automation.AUTOMATIC_MODE:
                raise ConflictError("准备任务已经变化，请重新开始准备。")
            stored_plan = self._prepare_plan_payload(record.get("plan") or {})
            self._quality_progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "configure",
                unit="commit",
                total=1,
                metadata={"save_count": 1},
            )
            self._quality_progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "start",
                unit="commit",
                label="提交参考与最终摘要",
            )
            unit_sources = self._quality_unit_sources(self.state.get("units") or [])
            # R6: a judgment is only committed while the input it was computed on
            # is still live. A human edit, an approval or a changed source
            # between the judgment and this commit makes the run stale instead
            # of adopting a relation the operator may already have contradicted.
            # A record that already committed keeps its result: a later edit is
            # handled where it belongs, by the injection-time checks.
            stale_groups = [
                str(group["group_id"])
                for group in stored_plan.get("groups") or []
                if record.get("committed") is not True
                and str(group.get("group_id") or "") in (stored_plan.get("resolved_groups") or {})
                and str(group.get("input_fingerprint") or "")
                and concept_automation.group_input_fingerprint(
                    str(group["group_id"]),
                    group.get("members") if isinstance(group.get("members"), list) else [],
                    support,
                    unit_sources,
                )
                != str(group.get("input_fingerprint") or "")
            ]
            if stale_groups:
                self._mark_prepare_failed_locked(
                    record["prepare_id"],
                    "组辨析所依据的卡片或原文已经变化，本次准备结果已失效。",
                    status="stale",
                )
                raise ConflictError("组辨析所依据的卡片或原文已经变化，本次准备结果已失效。")
            units = [unit for unit in (self.state.get("units") or []) if isinstance(unit, dict)]
            in_scope = [str(unit.get("id")) for unit in units if str(unit.get("id")) in set(stored_plan.get("scope") or [])]
            cards = support.get("cards") or {}
            resolved_groups = stored_plan.get("resolved_groups") or {}
            # A formal human edit is materialized as the explicit protection
            # marker before any automatic decision is taken. This must also
            # happen when this run reuses already-finished units (V2), so the
            # marker never depends on whether a model call happened to touch the
            # card in this particular run.
            for card in cards.values():
                if isinstance(card, dict):
                    concept_automation.mark_human_owned_card(card)

            # A group outcome decides which member may represent the group in
            # which unit (V2-H). Every card of a group is constrained by *all*
            # of its groups by intersecting the assigned units, so the result
            # cannot depend on the order in which the groups were processed, and
            # a card that one group cannot judge is never adopted through
            # another one. A group that only blocks its own cards never blocks
            # the other groups or units.
            group_allowed: dict[str, set[str]] = {}
            group_reasons: dict[str, str] = {}
            group_conflicts: list[dict[str, Any]] = []
            group_cards: set[str] = set()
            representatives: set[str] = set()
            unresolved_cards: set[str] = set()
            for group in stored_plan.get("groups") or []:
                members = group.get("members")
                if not isinstance(members, list) or not members:
                    members = self._prepare_group_members_locked(support, group.get("card_ids") or [])
                outcome = resolved_groups.get(group["group_id"]) or {}
                if not isinstance(outcome, Mapping) or not outcome:
                    outcome = {
                        "relation": "oversized" if group.get("oversized") else "unresolved",
                        "payload": {},
                    }
                assignment = concept_automation.group_unit_assignments(
                    str(group["group_id"]),
                    outcome,
                    members=members,
                    unit_sources=unit_sources,
                    unit_ids=in_scope,
                )
                representatives.update(str(item) for item in assignment.get("representatives") or [])
                unresolved_cards.update(str(item) for item in assignment.get("unresolved") or [])
                group_conflicts.extend(assignment.get("conflicts") or [])
                # A3: a locally judged oversized group knows *why* a member has no
                # unit — the unit it claims was never judged (over the pool or over
                # one request). Saying that is honest where a generic "no unit
                # assigned" would hide unfinished work as a decision.
                pending_units = {str(item) for item in (outcome.get("units_pending") or [])}
                oversized_units = {str(item) for item in (outcome.get("units_oversized") or [])}
                for member in members:
                    card_id = str(member.get("card_id") or "")
                    if not card_id:
                        continue
                    group_cards.add(card_id)
                    assigned = set(assignment["assignments"].get(card_id) or [])
                    current = group_allowed.get(card_id)
                    group_allowed[card_id] = assigned if current is None else (current & assigned)
                    if not assigned and card_id not in (assignment.get("excluded") or {}):
                        reason = "组辨析没有给这张卡分配任何单元，本次不采用。"
                        claims = {
                            str(item.get("unit_id") or "")
                            for item in ((member.get("payload") or {}).get("evidence") or [])
                            if isinstance(item, Mapping)
                        }
                        if claims & pending_units:
                            reason = "该单元未在本次的额外请求预算内完成局部辨析，留待下次确认。"
                        elif claims & oversized_units:
                            reason = "该单元的争用集合超过单次请求上限，本次不采用。"
                        group_reasons.setdefault(card_id, reason)
                for card_id, reason in (assignment.get("excluded") or {}).items():
                    group_cards.add(str(card_id))
                    group_allowed[str(card_id)] = set()
                    group_reasons.setdefault(str(card_id), str(reason))

            adopted: list[dict[str, Any]] = []
            skipped: list[tuple[str, str]] = []
            for card_id, card in sorted(cards.items()):
                if not isinstance(card, dict):
                    continue
                if card_id in group_cards:
                    assigned = group_allowed.get(card_id) or set()
                    if not assigned:
                        skipped.append(
                            (card_id, group_reasons.get(card_id) or "该卡所属的相关组没有可用的辨析结论，本次不采用。")
                        )
                        continue
                if concept_automation.is_manual_protected(card):
                    skipped.append((card_id, "有人工决定或批准内容，自动准备不采用。"))
                    continue
                verdict, reason = concept_automation.adoption_eligibility(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if verdict != "adopt":
                    skipped.append((card_id, reason))
                    continue
                allowed = concept_automation.applicable_unit_ids(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if card_id in group_cards:
                    # The judgment owns the relation, the card's own evidence owns
                    # the scope: both must allow the unit.
                    allowed = [unit for unit in allowed if unit in (group_allowed.get(card_id) or set())]
                if not allowed:
                    skipped.append((card_id, "这张卡没有可自动采用的适用单元。"))
                    continue
                adopted.append(
                    concept_automation.make_decision(
                        card,
                        verdict="adopt",
                        reason="内容、证据与检查结论均有效，可自动采用。",
                        allowed_unit_ids=allowed,
                        unit_sources=unit_sources,
                        prepare_id=record["prepare_id"],
                        representative_id=card_id if card_id in representatives else "",
                    )
                )

            automation = concept_automation.normalize_automation(support.get("automation"))
            previous_prepare = automation.get("prepare")
            # Idempotency: committing the same plan twice returns the same result
            # and must not bump versions or re-write decisions.
            if (
                isinstance(previous_prepare, Mapping)
                and str(previous_prepare.get("prepare_id") or "") == record["prepare_id"]
                and bool(previous_prepare.get("committed"))
            ):
                return {
                    "status": "ok",
                    "phase": "commit",
                    "idempotent": True,
                    "prepare_id": record["prepare_id"],
                    "adopted": int((previous_prepare.get("counts") or {}).get("adopted") or 0),
                    "reference_revision": int(automation.get("reference_revision") or 0),
                    "revision": int(support.get("revision") or 0),
                    "summary": self._prepare_summary_from_record(previous_prepare),
                }

            old_support = copy.deepcopy(self.state.get("quality_support"))
            old_events = copy.deepcopy(self.state.get("events") or [])
            decisions = automation.get("decisions") or {}
            decisions_before = copy.deepcopy(dict(decisions))
            # R4 + C2: this commit owns the whole scope, but "not adopted this
            # round" is not by itself evidence against a card. A decision is
            # revoked when the live data really invalidates it, or when a group
            # judgment that **finished** answered for its cards and still left the
            # card unadopted. A request that failed, was truncated, or was never
            # paid for proves nothing about the card, so its still-valid decision
            # stays and is recorded as unrefreshed instead of silently dropping a
            # reference that the sources still support.
            scope_set = set(stored_plan.get("scope") or [])
            adopted_ids = {decision["card_id"] for decision in adopted}
            answered_group_cards: set[str] = set()
            for group in stored_plan.get("groups") or []:
                outcome = resolved_groups.get(str(group.get("group_id"))) or {}
                if str(outcome.get("relation") or "") in {"equivalent", "distinct"}:
                    answered_group_cards.update(str(card) for card in group.get("card_ids") or [])
            # A decision is kept only when this round really could not answer for
            # the card: a recorded check/judgment failure (a failed or truncated
            # request, a stopped run, a lookup that could not run). A run that
            # finished cleanly and still left the card unadopted did so for a
            # reason of its own, and keeps the R4 behavior.
            round_failure = str(record.get("errors") and record["errors"][0] or "").strip()
            revoked: list[str] = []
            kept_unrefreshed: list[dict[str, str]] = []
            for card_id, card in cards.items():
                decision = decisions.get(card_id)
                if not isinstance(decision, Mapping) or str(decision.get("verdict") or "") != "adopt":
                    continue
                touches_scope = bool(set(str(u) for u in decision.get("allowed_unit_ids") or []) & scope_set)
                if not touches_scope or card_id in adopted_ids:
                    continue
                problems = concept_automation.decision_problems(
                    decision, card, unit_sources=unit_sources
                )
                if problems or card_id in answered_group_cards or not round_failure:
                    decisions.pop(card_id, None)
                    revoked.append(card_id)
                    continue
                kept_unrefreshed.append(
                    {
                        "card_id": card_id,
                        "reason": (
                            "本轮没能完成该卡的重新确认（调用失败或未支付，非内容失效）；"
                            "卡片内容、证据与身份仍然有效，保留旧决定。本轮首个失败："
                            f"{round_failure[:120]}"
                        ),
                    }
                )
            for decision in adopted:
                card_id = decision["card_id"]
                fresh = {key: value for key, value in decision.items() if key != "draft"}
                stored = decisions_before.get(card_id)
                # Unchanged input keeps the original decision record (and its
                # run id) instead of re-stamping it as if something changed.
                decisions[card_id] = (
                    copy.deepcopy(dict(stored))
                    if self._same_automatic_decision(stored, fresh)
                    else fresh
                )
            automation["decisions"] = decisions
            # The revision counts real reference changes: a run that reuses
            # everything and adopts the same cards again must not move it.
            changed_decisions = sum(
                1
                for card_id in set(decisions_before) | set(decisions)
                if decisions_before.get(card_id) != decisions.get(card_id)
            )
            automation["reference_revision"] = (
                int(automation.get("reference_revision") or 0) + changed_decisions
            )
            failures = int(record["counts"].get("failed_units") or 0)
            plan_status = str(record.get("status") or "")
            scope_size = len(stored_plan.get("scope") or [])
            group_failures = sum(
                1
                for outcome in resolved_groups.values()
                if str((outcome or {}).get("relation") or "") == "failed"
            )
            if failures and failures >= scope_size:
                # Every planned unit failed: this is a failure, not a completion.
                final_status = "failed"
            elif failures or group_failures or plan_status in ("partial", "failed", "interrupted", "stale"):
                # A failed group call is a partial result: the groups that could
                # be judged still apply, and the summary must say so.
                final_status = "partial"
            elif (
                int(record["counts"].get("local_units_pending") or 0)
                or int(record["counts"].get("budget_pending") or 0)
                or int(record["counts"].get("recheck_pending") or 0)
                or int(record["counts"].get("lookup_deferred") or 0)
            ):
                # Work this run could not finish (an unpaid unit, a re-check that
                # did not fit, a deferred lookup) is not a completion. The result
                # is still usable — that is what "partial" means — but it must
                # never be reported as "everything is done".
                final_status = "partial"
            else:
                final_status = "complete"

            reused_units = sum(
                1 for row in record.get("unit_results") or [] if str(row.get("status") or "") == "reused"
            )
            processed_units = sum(
                1
                for row in record.get("unit_results") or []
                if str(row.get("status") or "") in ("completed", "failed")
            )
            reused_groups = sum(
                1
                for outcome in resolved_groups.values()
                if isinstance(outcome, Mapping) and outcome.get("reused") is True
            )
            judged_groups = sum(
                1
                for outcome in resolved_groups.values()
                if isinstance(outcome, Mapping)
                and not outcome.get("reused")
                and str(outcome.get("relation") or "") in concept_automation.REUSABLE_RELATIONS
            )
            # A5 split: an answered question is not the same thing as an adopted
            # card, and an unanswered one is the work the page has to show. Both
            # are counted on the live cards, adopted and not adopted separately.
            ai_resolved_cards = 0
            resolved_questions = 0
            remaining_questions = 0
            for decision in adopted:
                card = cards.get(decision["card_id"])
                if not isinstance(card, dict):
                    continue
                draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
                if not [q for q in (draft.get("open_questions") or []) if str(q).strip()]:
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None:
                    continue
                ai_resolved_cards += 1
                resolved_questions += sum(
                    1 for row in assessment["question_results"] if row["status"] == "resolved"
                )
            for card_id, _reason in skipped:
                card = cards.get(card_id)
                if not isinstance(card, dict):
                    continue
                draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
                questions = [q for q in (draft.get("open_questions") or []) if str(q).strip()]
                if not questions:
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None:
                    # No usable structured answer: every question is still open.
                    remaining_questions += len(questions)
                    continue
                remaining_questions += sum(
                    1 for row in assessment["question_results"] if row["status"] == "unresolved"
                )

            reason_counts: dict[str, int] = {}
            for _card_id, reason in skipped:
                text = str(reason or "").strip()
                if text:
                    reason_counts[text] = reason_counts.get(text, 0) + 1
            not_adopted_reasons = [
                {"reason": reason, "count": count}
                for reason, count in sorted(reason_counts.items(), key=lambda row: (-row[1], row[0]))[:8]
            ]

            def finalize(record: dict[str, Any]) -> None:
                record["counts"]["adopted"] = len(adopted)
                record["counts"]["skipped"] = len(skipped) + int(record["counts"].get("skipped") or 0)
                record["counts"]["unresolved"] = sum(
                    1 for card_id in unresolved_cards if isinstance(cards.get(card_id), dict)
                )
                record["counts"]["protected"] = sum(
                    1
                    for card_id, card in cards.items()
                    if isinstance(card, dict) and concept_automation.is_manual_protected(card)
                )
                record["counts"]["ineligible"] = len(skipped)
                record["counts"]["uncovered_units"] = sum(
                    1
                    for unit_id in in_scope
                    if not any(str(unit_id) in (d.get("allowed_unit_ids") or []) for d in adopted)
                )
                record["counts"]["ai_resolved_cards"] = ai_resolved_cards
                record["counts"]["resolved_questions"] = resolved_questions
                record["counts"]["remaining_questions"] = remaining_questions
                record["counts"]["reused_units"] = reused_units
                record["counts"]["processed_units"] = processed_units
                record["counts"]["reused_groups"] = reused_groups
                record["counts"]["judged_groups"] = judged_groups
                record["not_adopted_reasons"] = copy.deepcopy(not_adopted_reasons)
                record["group_conflicts"] = copy.deepcopy(group_conflicts[:20])
                record["status"] = final_status
                record["committed"] = True
                record["revoked_decisions"] = revoked
                record["kept_unrefreshed_decisions"] = copy.deepcopy(kept_unrefreshed)
                record["finished_at"] = now_iso()

            finalize(record)
            automation["prepare"] = record
            support["automation"] = automation
            self._event_locked(
                "quality_prepare_committed",
                f"自动参考准备完成：采用 {len(adopted)} 张，跳过 {len(skipped)} 张；"
                f"未采用项不阻塞翻译。",
                prepare_id=record["prepare_id"],
            )
            try:
                self._quality_commit_locked(support, old_support=old_support, old_events=old_events)
            except Exception as exc:
                self._quality_progress.change(
                    str(record.get("prepare_id") or ""),
                    "commit",
                    "commit",
                    "failed",
                    unit="commit",
                    error=f"最终保存失败：{str(exc)[:240]}",
                )
                raise
            self._quality_progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "complete",
                unit="commit",
            )
            return {
                "status": "ok",
                "phase": "commit",
                "idempotent": False,
                "prepare_id": record["prepare_id"],
                "adopted": len(adopted),
                "revoked": revoked,
                "skipped": [{"card_id": card_id, "reason": reason} for card_id, reason in skipped],
                "reference_revision": int(automation.get("reference_revision") or 0),
                "revision": int(support.get("revision") or 0),
                "summary": self._prepare_summary_from_record(record),
            }

    def _prepare_record_locked(self, support: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        """The running prepare record, checked against the caller's plan id."""

        automation = concept_automation.normalize_automation(support.get("automation"))
        record = automation.get("prepare")
        if not isinstance(record, Mapping):
            raise ConflictError("当前项目没有进行中的准备任务，请重新开始准备。")
        requested = str(plan.get("prepare_id") or "")
        if requested and requested != str(record.get("prepare_id") or ""):
            raise ConflictError("准备计划已经变化，请重新开始准备。")
        return {
            "prepare_id": str(record.get("prepare_id") or ""),
            "mode": concept_automation.AUTOMATIC_MODE,
            "status": str(record.get("status") or "running"),
            "committed": record.get("committed") is True,
            "scope": list(record.get("scope") or []),
            "scope_fingerprint": str(record.get("scope_fingerprint") or ""),
            "baseline_revision": int(record.get("baseline_revision") or 0),
            "baseline_approved_version": int(record.get("baseline_approved_version") or 0),
            "plan": copy.deepcopy(dict(record.get("plan") or {})),
            "counts": dict(record.get("counts") or {}),
            "requests": dict(record.get("requests") or {}),
            # The frozen extra-request pool travels with the record: every extra
            # step reads the same number the preview showed.
            "budget": copy.deepcopy(dict(record.get("budget") or {})),
            # The executed lookups of this run (input identity + result), so a
            # later confirmation can prove the identical question was answered
            # already instead of paying for it again.
            "lookups": [
                dict(row)
                for row in (record.get("lookups") or [])
                if isinstance(row, Mapping)
            ],
            # The dedup state, kept apart from the bounded display log: it is what
            # decides whether a question was already answered (and paid for).
            "lookup_state": concept_automation.lookup_state_of(record.get("lookup_state")),
            "errors": list(record.get("errors") or []),
            "unit_results": list(record.get("unit_results") or []),
            "started_at": str(record.get("started_at") or ""),
            "finished_at": str(record.get("finished_at") or ""),
        }

    def _prepare_summary_from_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The result overview: concepts, candidates, groups and units are kept
        as separate units of count — they must never be added into one total."""

        counts = dict(record.get("counts") or {})
        plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
        batches = [batch for batch in (plan.get("batches") or []) if isinstance(batch, Mapping)]
        groups = [group for group in (plan.get("groups") or []) if isinstance(group, Mapping)]
        resolved = plan.get("resolved_groups") or {}
        unresolved_groups = sum(
            1
            for group in groups
            if str((resolved.get(group.get("group_id")) or {}).get("relation") or "") in ("unresolved", "skipped", "failed")
        )
        oversized_groups = sum(1 for group in groups if group.get("oversized"))
        errors = [str(item) for item in (record.get("errors") or []) if str(item).strip()]
        status = str(record.get("status") or "")
        planned_units = len(record.get("scope") or [])
        failed_units = int(counts.get("failed_units") or 0)
        reused_units = int(counts.get("reused_units") or 0)
        processed_units = int(counts.get("processed_units") or 0)
        raw_reasons = record.get("not_adopted_reasons")
        not_adopted_reasons = [
            {"reason": str(row.get("reason") or ""), "count": int(row.get("count") or 0)}
            for row in (raw_reasons if isinstance(raw_reasons, list) else [])
            if isinstance(row, Mapping) and str(row.get("reason") or "")
        ][:8]
        return {
            "prepare_id": str(record.get("prepare_id") or ""),
            "status": status,
            "scope": list(record.get("scope") or []),
            "committed": record.get("committed") is True,
            # Concepts that became usable references.
            "adopted": int(counts.get("adopted") or 0),
            # Candidates the preparation skipped, with the split that matters.
            "skipped": int(counts.get("skipped") or 0),
            "ineligible": int(counts.get("ineligible") or 0),
            "protected": int(counts.get("protected") or 0),
            "unresolved": int(counts.get("unresolved") or 0),
            # Why candidates were not adopted, so the page can show reasons
            # rather than only a total.
            "not_adopted_reasons": not_adopted_reasons,
            # Groups that were judged and groups that could not be judged.
            "group_count": len(groups),
            "unresolved_groups": unresolved_groups,
            "oversized_groups": oversized_groups,
            # Incremental scope (V2): what was handed over and what was redone.
            "reused_units": reused_units,
            "processed_units": processed_units,
            "reused_groups": int(counts.get("reused_groups") or 0),
            "judged_groups": int(counts.get("judged_groups") or 0),
            # Units: planned / finished / failed / not executed. The failed
            # count comes from the recorded per-unit results, never from a guess.
            "planned_units": planned_units,
            "finished_units": max(0, planned_units - failed_units),
            "failed_units": failed_units,
            # Set when a hand retry finished a batch this prepare recorded as
            # failed: its generation/check results are reusable by the next
            # confirmation, but the references still need an explicit re-preview.
            "reference_refresh_required": record.get("reference_refresh_required") is True,
            "reference_refresh_required_at": str(
                record.get("reference_refresh_required_at") or ""
            ),
            "unexecuted_units": max(0, planned_units - len(batches) and 0 or 0),
            "uncovered_units": int(counts.get("uncovered_units") or 0),
            "eligible_units": int(counts.get("eligible_units") or 0),
            "errors": errors,
            "requests": dict(record.get("requests") or {}),
            "budget": dict(record.get("budget") or {}),
            # A2/A3 split: an old check that was refreshed, one bounded lookup,
            # one unit's local judgment. Every one of them is still a candidate
            # the run had to answer, never an adoption by itself.
            "refreshed_checks": int(counts.get("refreshed_checks") or 0),
            "recheck_pending": int(counts.get("recheck_pending") or 0),
            "recheck_reasons": dict(counts.get("recheck_reasons") or {}),
            "lookup_rounds": int(counts.get("lookup_rounds") or 0),
            "lookup_hits": int(counts.get("lookup_hits") or 0),
            "lookup_refreshed": int(counts.get("lookup_refreshed") or 0),
            "lookup_misses": int(counts.get("lookup_misses") or 0),
            # A question already asked with exactly this content and evidence, or
            # one whose input did not fit this execution's bounds: neither is a
            # request of this run, and neither is an adoption.
            "lookup_settled": int(counts.get("lookup_settled") or 0),
            "lookup_deferred": int(counts.get("lookup_deferred") or 0),
            # A failed request and a refused write are different outcomes: both
            # were executed, neither wrote anything, and neither is reported as a
            # refresh or as a plain miss.
            "lookup_failed": int(counts.get("lookup_failed") or 0),
            "lookup_rejected": int(counts.get("lookup_rejected") or 0),
            "local_groups": int(counts.get("local_groups") or 0),
            "local_units_judged": int(counts.get("local_units_judged") or 0),
            "local_units_oversized": int(counts.get("local_units_oversized") or 0),
            # Units whose local judgment an earlier confirmation already made:
            # handed over without a request, so they are neither "judged now" nor
            # "still pending".
            "local_units_reused": int(counts.get("local_units_reused") or 0),
            "local_units_pending": int(counts.get("local_units_pending") or 0),
            # The base re-check and the extra lookup are accounted for on
            # different bases; the page and the report must not merge them.
            "budget_basis": {
                "recheck": {
                    "pool": "base",
                    "counted_as": "requests.check",
                    "note": "基础重查是复用单元的必要步骤，不占额外请求预算；单次确认的请求有输入上限，超出记 recheck_pending。",
                    "limits": {
                        "cards": MAX_RECHECK_CARDS,
                        "units": MAX_CHECK_REQUEST_UNITS,
                        "chars": MAX_CHECK_REQUEST_CHARS,
                    },
                },
                "lookup": {
                    "pool": "shared_extra",
                    "slots_per_execution": 1,
                    "note": "一次确认最多一次补查，占共享额外请求预算 1 格；请求规模按计划 §4.1 的上限，先到者为限。",
                    "limits": dict(concept_automation.LOOKUP_REQUEST_LIMITS),
                },
            },
            # Work the shared pool could not pay for. Shown as "still pending",
            # never as a completed step.
            "budget_pending": int(counts.get("budget_pending") or 0),
            # A5 split: answered questions, open questions and the units a
            # finished batch found nothing for.
            "ai_resolved_cards": int(counts.get("ai_resolved_cards") or 0),
            "resolved_questions": int(counts.get("resolved_questions") or 0),
            "remaining_questions": int(counts.get("remaining_questions") or 0),
            "no_candidate_units": int(counts.get("no_candidate_units") or 0),
            "covered_units": max(
                0,
                int(counts.get("eligible_units") or 0) - int(counts.get("uncovered_units") or 0),
            ),
            "note": (
                "成功 0 张概念也是有效结果：没有候选的单元仍可正常翻译。"
                if status in ("complete", "partial") and not counts.get("adopted")
                else ""
            ),
            "reuse_note": (
                f"本次复用已完成的单元 {reused_units} 个、仍有效的组辨析 {int(counts.get('reused_groups') or 0)} 个，"
                f"只重新处理了 {processed_units} 个单元。"
                if reused_units or int(counts.get("reused_groups") or 0)
                else ""
            ),
        }

    def _prepare_view_locked(
        self,
        *,
        prepare_id: str,
        support: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        support = support or normalize_quality_support(self.state.get("quality_support"))
        record = self._prepare_record_locked(support, {"prepare_id": prepare_id})
        return {
            "status": "ok",
            "prepare_id": record["prepare_id"],
            "prepare_status": record["status"],
            "summary": self._prepare_summary_from_record(record),
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
                summary = self._prepare_summary_from_record(record)
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

    def _editorial_request_inputs_locked(
        self,
        unit: dict[str, Any],
    ) -> tuple[EditorialSuggestionRequest, tuple[str, str, int]]:
        unit_state.ensure_unit_feedback_fields(unit)
        context = unit_requests.unit_translation_context(self.state, unit)
        adjacent = tuple(
            value
            for key in ("previous_context", "next_context")
            for value in [context.get(key)]
            if isinstance(value, str) and value.strip()
        )
        support = normalize_quality_support(self.state.get("quality_support"))
        approved_expressions = self._approved_expressions(support)
        # Hand the editorial model the actual approved card content (meaning,
        # acceptable translations, ...) frozen at a concrete version, not just a
        # bare expression list. Selection is bounded by the same character
        # budget as translation/review reference injection.
        selection = select_reference_cards(
            support,
            source_text=str(unit.get("source") or ""),
            adjacent_texts=list(adjacent),
        )
        approved_cards = tuple(
            {
                "card_id": str(card.get("card_id") or ""),
                "card_revision": int(card.get("card_revision") or 0),
                "expressions": list(card.get("expressions") or []),
                "text": str(card.get("text") or ""),
            }
            for card in (selection.get("cards") or [])
        )
        request = EditorialSuggestionRequest(
            project_id=str(self.state.get("project", {}).get("id") or ""),
            unit_id=unit["id"],
            source_text=unit["source"],
            source_sha256=unit["source_sha256"],
            translated_text=unit["translation"],
            translation_revision=unit["translation_revision"],
            adjacent_source=adjacent,
            approved_expressions=approved_expressions,
            approved_cards=approved_cards,
        )
        return request, (
            unit["source_sha256"],
            unit["source"],
            unit["translation_revision"],
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
        with self.lock:
            self._ensure_open_locked()
            self._validate_expected_project_id_locked(expected_project_id)
            unit = self._find_unit_locked(unit_id)
            if not isinstance(unit.get("translation"), str) or not unit["translation"].strip():
                raise PipelineError("只有已保存译文的单元才能请求表达建议。")
            self._validate_unit_write_guard_locked(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            request, binding = self._editorial_request_inputs_locked(unit)

        _generation, _checker, editorial, _resolution = self._quality_providers()

        def before_attempt(round_no: int, api_calls: int) -> None:
            # Bound to the same unit identity the result is validated against,
            # so a repair round is never sent for a stale translation.
            with self.lock:
                if self._closed:
                    raise PipelineError("项目已关闭，不再发起下一轮模型修正。")
                current = self._find_unit_locked(unit_id)
                source_sha256, source_text, revision = binding
                if (
                    str(current.get("source_sha256") or "") != source_sha256
                    or str(current.get("source") or "") != source_text
                    or int(current.get("translation_revision") or 0) != revision
                ):
                    raise PipelineError("单元内容已经变化，不再发起下一轮模型修正。")

        request = replace(
            request,
            control=RepairControl(
                invocation_id=unit_id, kind="表达建议", before_attempt=before_attempt
            ),
        )
        try:
            result = editorial.suggest(request)
        except ContentRepairExhausted as exc:
            raise PipelineError(str(exc)) from exc
        except QualityProviderError as exc:
            raise PipelineError(f"表达建议失败：{exc}") from exc
        except Exception as exc:
            raise PipelineError(f"表达建议失败：{exc}") from exc

        with self.lock:
            if self._closed:
                raise ConflictError("当前项目管理器已关闭，不能返回表达建议。")
            self._validate_expected_project_id_locked(expected_project_id)
            current = self._find_unit_locked(unit_id)
            source_sha256, source_text, revision = binding
            if (
                str(current.get("source_sha256") or "") != source_sha256
                or str(current.get("source") or "") != source_text
                or int(current.get("translation_revision") or 0) != revision
            ):
                raise ConflictError("单元内容已经变化，表达建议已失效，请重新请求。")
            return {
                "status": "ok",
                "project_id": request.project_id,
                "unit_id": result.unit_id,
                "source_sha256": result.source_sha256,
                "translation_revision": result.translation_revision,
                "provider": result.provider,
                "model": result.model,
                "suggestions": result.suggestions,
                "note": "建议仅供人工选择，不会自动修改译文。",
            }

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
            if unit_id in self._active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = self._find_unit_locked(unit_id)
            if unit.get("status") not in ACTION_STATUSES:
                raise PipelineError("只有待裁决的单元才能进行人工裁决。")
            if expected_source_sha256 and expected_source_sha256 != unit["source_sha256"]:
                raise PipelineError("源文已经变化，请刷新后再提交裁决。")
            self._invalidate_output_locked()
            if decision == "accept-risk":
                unit["status"] = "accepted_risk"
                unit["user_decision"] = "accept-risk"
                unit["last_error"] = None
                unit["updated_at"] = now_iso()
                self._event_locked("user_accepted_risk", "用户选择直接通过并接受当前校验风险。", unit_id)
                self._recompute_stats_locked()
                self.state["run"]["status"] = self._derived_run_status_locked()
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
                self._start_job_locked([unit_id], "translation")
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
            self._start_job_locked([unit_id], "review")
            return copy.deepcopy(unit)


manager: PipelineManager | None = None
