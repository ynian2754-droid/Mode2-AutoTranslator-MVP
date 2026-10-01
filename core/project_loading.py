"""Load legacy projects and construct normalized project states."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core import project_settings, project_state, unit_state
from core.document_model import empty_document
from core.exceptions import PipelineError
from core.importers import SourceImporter
from core.project_factory import DEFAULT_SAMPLE_SOURCE, ProjectFactory, empty_output_state
from core.project_state import ProjectStateCell
from core.segmenter import MarkdownSegmenter


def normalize_interrupted_prepare(state: dict[str, Any], clock: Callable[[], str]) -> bool:
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
    record["finished_at"] = str(record.get("finished_at") or "") or clock()
    errors = record.get("errors") if isinstance(record.get("errors"), list) else []
    note = "应用重启，准备任务已中断；未完成的部分可以重新准备，已提交的参考不受影响。"
    if note not in errors:
        errors.append(note)
    record["errors"] = errors
    return True


class ProjectLoader:
    """Explicit project resources; callers retain the original lock boundaries."""

    def __init__(self, cell: ProjectStateCell, factory: ProjectFactory,
                 importer: SourceImporter, segmenter: MarkdownSegmenter,
                 runtime_dir: Path, clock: Callable[[], str]) -> None:
        self.cell = cell
        self.project_factory = factory
        self.source_importer = importer
        self.segmenter = segmenter
        self.runtime_dir = runtime_dir
        self.clock = clock

    def load(self) -> dict[str, Any]:
        state = self.cell.store.load()
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
                was_translating = unit.get("status") in unit_state.TRANSLATION_PROCESSING_STATUSES
                unit_state.ensure_unit_feedback_fields(unit)
                if was_translating and unit["translation_revision"] > 0:
                    # A translating unit has not committed a new translation yet.
                    # Roll back its queued revision so persisted feedback remains
                    # attached to the next translation attempt after a restart.
                    unit["translation_revision"] -= 1
                if unit.get("status") in unit_state.PROCESSING_STATUSES:
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
                state["run"]["completed_at"] = state["run"].get("completed_at") or self.clock()
            self.cell.state = state
            if provider_migrated:
                project_state.append_event(
                    self.cell,
                    "provider_migrated",
                    "旧 Provider 配置已迁移为 OpenAI Compatible。",
                    None,
                    {
                        "previous_provider": previous_provider,
                        "previous_review_provider": previous_review_provider,
                    },
                    self.clock,
                )
            self.ensure_document_manifest()
            normalize_interrupted_prepare(state, self.clock)
            unit_state.recompute_unit_stats(self.cell.state)
            project_state.save_project(self.cell)
            return state
        state = self.new_state(DEFAULT_SAMPLE_SOURCE, demo_mode=True, max_concurrency=3, provider="demo")
        self.cell.state = state
        project_state.save_project(self.cell)
        return state


    def ensure_document_manifest(self) -> None:
        """Backfill manifests only when the stored source proves the unit mapping."""
        document = self.cell.state.get("document")
        if isinstance(document, dict) and isinstance(document.get("parts"), list) and document.get("parts"):
            return
        units = self.cell.state.get("units") or []
        if not units:
            self.cell.state["document"] = empty_document(
                source_sha256=self.cell.state.get("project", {}).get("source_sha256", ""),
            )
            return
        source_file = self.cell.state.get("project", {}).get("source_file") or {}
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
                    demo_mode=bool(self.cell.state.get("project", {}).get("demo_mode")),
                    max_words=project_settings.configured_target_words(self.cell.state.get("config", {})),
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
                self.cell.state["document"] = generated_document
                return


    def new_state(
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

