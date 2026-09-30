"""Project output generation and cached glyph readiness with explicit resources."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from core import project_state
from core.exceptions import PipelineError
from core.project_factory import empty_output_state
from core.project_state import ProjectStateCell

if TYPE_CHECKING:
    from core.assembler import DocumentAssembler

OUTPUT_FORMATS = ("markdown", "text", "pdf", "epub", "docx")


@dataclass(frozen=True)
class OutputExportResources:
    assembly_error: type[BaseException]
    docx_export_error: type[BaseException]
    docx_exporter: type[Any]
    epub_export_error: type[BaseException]
    pdf_export_error: type[BaseException]
    epub_exporter: type[Any]
    pdf_exporter: type[Any]


def invalidate_output(state: dict[str, Any]) -> None:
    output = state.get("output")
    if isinstance(output, dict) and output.get("path"):
        state["output"] = empty_output_state()


class OutputOwner:
    def __init__(self, cell: ProjectStateCell, assembler: DocumentAssembler,
                 runtime_dir: Path, clock: Callable[[], str],
                 export_resources: Callable[[], OutputExportResources]) -> None:
        self.cell = cell
        self.assembler = assembler
        self.runtime_dir = runtime_dir
        self.clock = clock
        self.export_resources = export_resources
        # Read-only PDF glyph pre-check cache, keyed by a cheap state signature
        # because the workbench polls the output status.
        self.glyph_precheck_cache: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def glyph_precheck_locked(
        self,
        *,
        pdf_math_font_path: Path | str,
        load_fonts: Callable[[], Any],
        scan_glyphs: Callable[[Any, Callable[[int], bool]], dict[str, Any]],
        unavailable_scan: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Return the cached read-only glyph scan while the manager lock is held."""

        units = self.cell.state.get("units") if isinstance(self.cell.state.get("units"), list) else []
        # The font chain decides coverage, so the signature also carries
        # whether the optional math fallback asset is present: removing or
        # adding it changes the verdict for the same project state.
        math_asset_present = Path(pdf_math_font_path).is_file()
        signature = (
            len(units),
            sum(
                int(unit.get("translation_revision") or 0)
                for unit in units
                if isinstance(unit, dict)
            ),
            max(
                (str(unit.get("updated_at") or "") for unit in units if isinstance(unit, dict)),
                default="",
            ),
            tuple(
                (
                    str(unit.get("id") or ""),
                    str(unit.get("source_sha256") or ""),
                    int(unit.get("translation_revision") or 0),
                    "\uFFFD" in str(unit.get("source") or ""),
                    "\uFFFD" in str(unit.get("translation") or ""),
                    bool(unit.get("user_edited_translation")),
                    str(unit.get("status") or ""),
                )
                for unit in units
                if isinstance(unit, dict)
            ),
            math_asset_present,
        )
        cached = self.glyph_precheck_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            fonts = load_fonts()
            scan = scan_glyphs(units, fonts.has_glyph)
        except Exception:
            scan = unavailable_scan()
        self.glyph_precheck_cache = (signature, scan)
        return scan


    def status_locked(self) -> dict[str, Any]:
        """Build the existing output status payload while the manager lock is held."""

        status = self.assembler.readiness(self.cell.state)
        output_state = dict(self.cell.state.get("output") or {})
        artifacts = output_state.get("artifacts") if isinstance(output_state.get("artifacts"), dict) else {}
        formats: dict[str, Any] = {}
        for output_format in OUTPUT_FORMATS:
            metadata = dict(artifacts.get(output_format) or {})
            output_path = self.assembler.output_path(metadata)
            metadata.setdefault("path", None)
            metadata.setdefault("trace_map_path", None)
            metadata.setdefault("filename", None)
            metadata.setdefault("sha256", None)
            metadata.setdefault("exported_at", None)
            metadata.setdefault("included_unit_count", 0)
            metadata.update(
                {
                    "format": output_format,
                    "ready": bool(status["ready"]),
                    "available": bool(status["ready"] and output_path is not None),
                }
            )
            formats[output_format] = metadata
        last_format = str(output_state.get("format") or self.default_output_format_locked())
        if last_format not in OUTPUT_FORMATS:
            last_format = self.default_output_format_locked()
        metadata = dict(formats.get(last_format) or {})
        status["formats"] = formats
        status["output"] = metadata
        return status


    def generate_locked(self, output_format: str | None) -> dict[str, Any]:
        """Generate and persist one output while the manager lock is held."""
        resources = self.export_resources()

        output_format = self.resolve_output_format_locked(output_format)
        try:
            if output_format in {"markdown", "text"}:
                metadata = self.assembler.export(self.cell.state, output_format=output_format)
            elif output_format == "docx":
                metadata = resources.docx_exporter(self.runtime_dir).export(self.cell.state)
            elif output_format == "epub":
                metadata = resources.epub_exporter(self.runtime_dir).export(self.cell.state)
            elif output_format == "pdf":
                metadata = resources.pdf_exporter(self.runtime_dir).export(self.cell.state)
            else:  # pragma: no cover - guarded by _resolve_output_format_locked
                raise PipelineError(f"不支持的输出格式：{output_format}")
        except resources.assembly_error:
            raise
        except (resources.docx_export_error, resources.epub_export_error, resources.pdf_export_error):
            raise
        artifacts = self.cell.state.setdefault("output", {}).setdefault("artifacts", {})
        artifacts[output_format] = copy.deepcopy(metadata)
        self.cell.state["output"] = {
            **metadata,
            "format": output_format,
            "artifacts": artifacts,
        }
        project_state.append_event(
            self.cell,
            "document_exported",
            f"完整译文档已生成：{metadata['filename']}。",
            None,
            {
                "format": output_format,
                "included_unit_count": metadata["included_unit_count"],
                "sha256": metadata["sha256"],
            },
            self.clock,
        )
        project_state.save_project(self.cell)
        return {
            "status": "ok",
            "output": copy.deepcopy(metadata),
            "download_url": f"/api/project/output/latest?format={output_format}",
        }


    def file_path_locked(
        self,
        output_format: str | None,
    ) -> Path:
        """Resolve an existing output file while the manager lock is held."""

        if not self.assembler.readiness(self.cell.state)["ready"]:
            raise PipelineError("当前项目尚不可下载完整译文档。")
        output_format = self.resolve_output_format_locked(output_format)
        output_state = self.cell.state.get("output") or {}
        artifacts = output_state.get("artifacts") if isinstance(output_state.get("artifacts"), dict) else {}
        metadata = artifacts.get(output_format) or (output_state if output_state.get("format") == output_format else {})
        path = self.assembler.output_path(metadata)
        if path is None:
            raise PipelineError(f"当前项目还没有可下载的 {output_format} 完整译文档。")
        return path


    def default_output_format_locked(self) -> str:
        """Return the existing default output format from the current document."""

        document_format = str((self.cell.state.get("document") or {}).get("format") or "")
        return "text" if document_format == "text" else "markdown"


    def resolve_output_format_locked(
        self,
        value: Any | None,
    ) -> str:
        """Normalize and validate a requested output format."""

        output_format = self.default_output_format_locked() if value is None else str(value).strip().casefold()
        if output_format not in OUTPUT_FORMATS:
            raise PipelineError(f"输出格式必须是：{', '.join(OUTPUT_FORMATS)}。")
        return output_format
