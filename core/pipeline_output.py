"""Output-related operations used by :class:`PipelineManager`.

The manager remains the owner of project state, locks, persistence, and
lifecycle. Callers keep the existing manager methods and lock scopes; these
functions only hold the corresponding method bodies.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from core.exceptions import PipelineError


def glyph_precheck_locked(
    manager: Any,
    *,
    pdf_math_font_path: Path | str,
    load_fonts: Callable[[], Any],
    scan_glyphs: Callable[[Any, Callable[[int], bool]], dict[str, Any]],
    unavailable_scan: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Return the cached read-only glyph scan while the manager lock is held."""

    units = manager.state.get("units") if isinstance(manager.state.get("units"), list) else []
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
    cached = manager._glyph_precheck_cache
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        fonts = load_fonts()
        scan = scan_glyphs(units, fonts.has_glyph)
    except Exception:
        scan = unavailable_scan()
    manager._glyph_precheck_cache = (signature, scan)
    return scan


def output_status_locked(manager: Any, output_formats: tuple[str, ...]) -> dict[str, Any]:
    """Build the existing output status payload while the manager lock is held."""

    status = manager.assembler.readiness(manager.state)
    output_state = dict(manager.state.get("output") or {})
    artifacts = output_state.get("artifacts") if isinstance(output_state.get("artifacts"), dict) else {}
    formats: dict[str, Any] = {}
    for output_format in output_formats:
        metadata = dict(artifacts.get(output_format) or {})
        output_path = manager.assembler.output_path(metadata)
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
    last_format = str(output_state.get("format") or manager._default_output_format_locked())
    if last_format not in output_formats:
        last_format = manager._default_output_format_locked()
    metadata = dict(formats.get(last_format) or {})
    status["formats"] = formats
    status["output"] = metadata
    status["glyph_precheck"] = manager._glyph_precheck_locked()
    return status


def generate_output_locked(
    manager: Any,
    output_format: str | None,
    *,
    assembly_error: type[BaseException],
    docx_export_error: type[BaseException],
    docx_exporter: type[Any],
    epub_export_error: type[BaseException],
    pdf_export_error: type[BaseException],
    epub_exporter: type[Any],
    pdf_exporter: type[Any],
) -> dict[str, Any]:
    """Generate and persist one output while the manager lock is held."""

    output_format = manager._resolve_output_format_locked(output_format)
    try:
        if output_format in {"markdown", "text"}:
            metadata = manager.assembler.export(manager.state, output_format=output_format)
        elif output_format == "docx":
            metadata = docx_exporter(manager.runtime_dir).export(manager.state)
        elif output_format == "epub":
            metadata = epub_exporter(manager.runtime_dir).export(manager.state)
        elif output_format == "pdf":
            metadata = pdf_exporter(manager.runtime_dir).export(manager.state)
        else:  # pragma: no cover - guarded by _resolve_output_format_locked
            raise PipelineError(f"不支持的输出格式：{output_format}")
    except assembly_error:
        raise
    except (docx_export_error, epub_export_error, pdf_export_error):
        raise
    artifacts = manager.state.setdefault("output", {}).setdefault("artifacts", {})
    artifacts[output_format] = copy.deepcopy(metadata)
    manager.state["output"] = {
        **metadata,
        "format": output_format,
        "artifacts": artifacts,
    }
    manager._event_locked(
        "document_exported",
        f"完整译文档已生成：{metadata['filename']}。",
        format=output_format,
        included_unit_count=metadata["included_unit_count"],
        sha256=metadata["sha256"],
    )
    manager._save_locked()
    return {
        "status": "ok",
        "output": copy.deepcopy(metadata),
        "download_url": f"/api/project/output/latest?format={output_format}",
        "readiness": manager.output_status(),
    }


def output_file_path_locked(
    manager: Any,
    output_format: str | None,
) -> Path:
    """Resolve an existing output file while the manager lock is held."""

    if not manager.assembler.readiness(manager.state)["ready"]:
        raise PipelineError("当前项目尚不可下载完整译文档。")
    output_format = manager._resolve_output_format_locked(output_format)
    output_state = manager.state.get("output") or {}
    artifacts = output_state.get("artifacts") if isinstance(output_state.get("artifacts"), dict) else {}
    metadata = artifacts.get(output_format) or (output_state if output_state.get("format") == output_format else {})
    path = manager.assembler.output_path(metadata)
    if path is None:
        raise PipelineError(f"当前项目还没有可下载的 {output_format} 完整译文档。")
    return path


def default_output_format_locked(manager: Any) -> str:
    """Return the existing default output format from the current document."""

    document_format = str((manager.state.get("document") or {}).get("format") or "")
    return "text" if document_format == "text" else "markdown"


def resolve_output_format_locked(
    manager: Any,
    value: Any | None,
    output_formats: tuple[str, ...],
) -> str:
    """Normalize and validate a requested output format."""

    output_format = manager._default_output_format_locked() if value is None else str(value).strip().casefold()
    if output_format not in output_formats:
        raise PipelineError(f"输出格式必须是：{', '.join(output_formats)}。")
    return output_format
