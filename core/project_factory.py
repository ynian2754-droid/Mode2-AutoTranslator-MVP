"""Create a new project state from already extracted source text."""

from __future__ import annotations

import uuid
from typing import Any

from .document_model import empty_document
from .segmenter import DEFAULT_TARGET_WORDS, MarkdownSegmenter, validate_target_words
from .text_reconstruction import PdfTextReconstruction
from .translation_context import default_context_words
from .utils import now_iso, sha256_text


DEFAULT_SAMPLE_SOURCE = """Translation workflows become reliable when every unit has an immutable identifier.

Parallel workers can translate independent units while the controller preserves order.

A separate reviewer should inspect the source and target without inheriting hidden reasoning.

A failed review becomes a user decision instead of a silent overwrite, and the user can inspect the evidence before choosing a repair or a risk acceptance. The controller records that decision in the audit trail for later release checks.

Small corrections can return to review without rerunning the entire book.

Every result keeps its source hash so stale work cannot enter the project."""

SUPPORTED_PROVIDERS = {"demo", "openai-compatible"}


def empty_output_state() -> dict[str, Any]:
    """A brand-new, unshared output state for one project.

    Every writer of the empty output shape goes through this helper so the
    field set, the defaults and the JSON key order cannot drift apart.
    """

    return {
        "format": None,
        "path": None,
        "filename": None,
        "sha256": None,
        "exported_at": None,
        "included_unit_count": 0,
        "artifacts": {},
    }


def _resolve_target_words(
    target_segment_words: Any | None,
    max_segment_words: Any | None,
) -> int:
    """Resolve the canonical target while accepting the legacy argument."""
    if target_segment_words is not None and max_segment_words is not None:
        target = validate_target_words(target_segment_words)
        legacy = validate_target_words(max_segment_words)
        if target != legacy:
            raise ValueError("target_segment_words 与 max_segment_words 必须一致。")
        return target
    value = target_segment_words if target_segment_words is not None else max_segment_words
    return validate_target_words(DEFAULT_TARGET_WORDS if value is None else value)


class ProjectFactory:
    """Own project initialization; it does not run providers or write files."""

    def __init__(self, segmenter: MarkdownSegmenter | None = None) -> None:
        self.segmenter = segmenter or MarkdownSegmenter()

    def create_empty_state(
        self,
        project_name: str,
        *,
        max_concurrency: int = 3,
        provider: str = "openai-compatible",
        source_language: str = "English",
        target_language: str = "简体中文",
        target_segment_words: int | None = None,
        max_segment_words: int | None = None,
    ) -> dict[str, Any]:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"不支持的 Provider：{provider}")
        target_segment_words = _resolve_target_words(target_segment_words, max_segment_words)
        context_words = default_context_words(target_segment_words)
        now = now_iso()
        return {
            "schema_version": 1,
            "protocol": "mode2-auto-translator-mvp-v1",
            "project": {
                "id": f"project-{uuid.uuid4().hex[:10]}",
                "name": project_name,
                "created_at": now,
                "source_sha256": sha256_text(""),
                "source_file": None,
                "demo_mode": False,
                # New projects start in automatic reference mode. Legacy
                # projects without the field read as manual and only switch
                # when the user asks (see core.concept_automation).
                "reference_mode": "automatic",
            },
            "config": {
                "source_language": source_language,
                "target_language": target_language,
                "provider": provider,
                "review_provider": provider,
                "max_concurrency": max_concurrency,
                "target_segment_words": target_segment_words,
                "previous_context_words": context_words,
                "next_context_words": context_words,
            },
            "run": {
                "run_id": None,
                "status": "ready",
                "running": False,
                "max_concurrency": None,
                "started_at": None,
                "completed_at": None,
                "unit_ids": [],
                "completed_unit_ids": [],
                "cancel_requested": False,
                "stop_requested_at": None,
                "cancelled_at": None,
                "stop_timeout_at": None,
            },
            "document": empty_document(
                source_sha256=sha256_text(""),
            ),
            "output": empty_output_state(),
            "units": [],
            "stats": {
                "total": 0,
                "pending": 0,
                "active": 0,
                "waiting": 0,
                "cancelled": 0,
                "passed": 0,
                "user_modified": 0,
                "needs_action": 0,
                "accepted_risk": 0,
                "failed": 0,
                "done": 0,
                "progress_percent": 0,
            },
            "events": [
                {
                    "at": now,
                    "type": "project_created",
                    "message": "项目已创建，等待导入源文件。",
                }
            ],
        }

    def create_state(
        self,
        source_text: str,
        *,
        demo_mode: bool,
        max_concurrency: int,
        provider: str,
        source_language: str = "English",
        target_language: str = "简体中文",
        source_file: dict[str, Any] | None = None,
        pdf_reconstruction: PdfTextReconstruction | None = None,
        structure_blocks: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        group_adjacent_paragraphs: bool = False,
        target_segment_words: int | None = None,
        max_segment_words: int | None = None,
    ) -> dict[str, Any]:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"不支持的 Provider：{provider}")
        target_segment_words = _resolve_target_words(target_segment_words, max_segment_words)
        context_words = default_context_words(target_segment_words)
        file_info = source_file or {
            "name": "demo.md",
            "format": "markdown",
            "size_bytes": len(source_text.encode("utf-8")),
            "stored_path": None,
        }
        if pdf_reconstruction is not None:
            units, document = self.segmenter.segment_pdf_reconstruction(
                pdf_reconstruction,
                demo_mode=demo_mode,
                target_words=target_segment_words,
                source_name=str(file_info.get("name") or "source.pdf"),
            )
        else:
            units, document = self.segmenter.segment_document(
                source_text,
                demo_mode=demo_mode,
                max_words=target_segment_words,
                document_format=str(file_info.get("format") or "markdown"),
                source_name=str(file_info.get("name") or "demo.md"),
                structure_blocks=structure_blocks,
                group_adjacent_paragraphs=group_adjacent_paragraphs,
            )
        for unit in units:
            if isinstance(unit, dict):
                unit.setdefault("user_edited_translation", None)
        return {
            "schema_version": 1,
            "protocol": "mode2-auto-translator-mvp-v1",
            "project": {
                "id": f"project-{uuid.uuid4().hex[:10]}",
                "created_at": now_iso(),
                "source_sha256": sha256_text(source_text),
                "source_file": file_info,
                "demo_mode": demo_mode,
                # New projects start in automatic reference mode; a project
                # written before this field existed reads as manual.
                "reference_mode": "automatic",
            },
            "config": {
                "source_language": source_language,
                "target_language": target_language,
                "provider": provider,
                "review_provider": provider,
                "max_concurrency": max_concurrency,
                "target_segment_words": target_segment_words,
                "previous_context_words": context_words,
                "next_context_words": context_words,
            },
            "run": {
                "run_id": None,
                "status": "ready",
                "running": False,
                "max_concurrency": None,
                "started_at": None,
                "completed_at": None,
                "unit_ids": [],
                "completed_unit_ids": [],
                "cancel_requested": False,
                "stop_requested_at": None,
                "cancelled_at": None,
                "stop_timeout_at": None,
            },
            "document": document,
            "output": empty_output_state(),
            "units": units,
            "stats": {},
            "events": [
                {
                    "at": now_iso(),
                    "type": "project_created",
                    "message": f"项目已创建，共 {len(units)} 个翻译单元。",
                }
            ],
        }
