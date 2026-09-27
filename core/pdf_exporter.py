"""Create a small, portable, reflowable PDF from translated nodes."""

from __future__ import annotations

import hashlib
import html
import io
import re
from pathlib import Path
from typing import Any
from unicodedata import east_asian_width

from .assembler import DocumentAssembler
from .exceptions import PipelineError
from .pdf_fonts import PdfFontChain, PdfFontError, load_pdf_font, load_pdf_fonts
from .pdf_glyph_support import (
    apply_equivalents,
    blocking_message,
    scan_text_glyphs,
    summarize_replacements,
)
from .utils import now_iso


# The following STSong/Courier objects and helpers are retained only for the
# historical diagnostic renderer.  Public ``export()`` never calls that path;
# production output uses the embedded project TrueType font above.
# Adobe's STSong-Light CMap assigns CID 1 to U+0020 and consecutive CIDs to
# the remaining printable ASCII range.  Keep its published widths in the
# diagnostic font declaration for old comparison fixtures.
_STSONG_ASCII_WIDTHS = (
    207, 270, 342, 467, 462, 797, 710, 239, 374, 374, 423, 605, 238, 375, 238, 334,
    462, 462, 462, 462, 462, 462, 462, 462, 462, 462, 238, 238, 605, 605, 605, 344,
    748, 684, 560, 695, 739, 563, 511, 729, 793, 318, 312, 666, 526, 896, 758, 772,
    544, 772, 628, 465, 607, 753, 711, 972, 647, 620, 607, 374, 333, 374, 606, 500,
    239, 417, 503, 427, 529, 415, 264, 444, 518, 241, 230, 495, 228, 793, 527, 524,
    524, 504, 338, 336, 277, 517, 450, 652, 466, 452, 407, 370, 258, 370, 605,
)
_FALLBACK_ASCII_REPLACEMENTS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
}


class PdfExportError(PipelineError):
    """The project cannot be written as a readable PDF."""


class _PdfObjects:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def set(self, object_id: int, body: bytes) -> None:
        self.objects[object_id - 1] = body

    def render(self, root_id: int, info_id: int | None = None) -> bytes:
        if any(body is None for body in self.objects):
            raise PdfExportError("PDF 内部对象未完成。")
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, body in enumerate(self.objects, 1):
            offsets.append(len(output))
            output.extend(f"{index} 0 obj\n".encode("ascii"))
            output.extend(body or b"")
            output.extend(b"\nendobj\n")
        xref_offset = len(output)
        output.extend(f"xref\n0 {len(self.objects) + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(b"trailer\n")
        trailer = f"<< /Size {len(self.objects) + 1} /Root {root_id} 0 R"
        if info_id is not None:
            trailer += f" /Info {info_id} 0 R"
        output.extend((trailer + " >>\n").encode("ascii"))
        output.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
        return bytes(output)


class PdfExporter:
    """Export semantic document structure to a readable A4 PDF."""

    PAGE_WIDTH = 595.28
    PAGE_HEIGHT = 841.89
    MARGIN_X = 54.0
    MARGIN_TOP = 58.0
    MARGIN_BOTTOM = 54.0

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.output_root = (self.runtime_dir / "output").resolve()
        self.assembler = DocumentAssembler(self.runtime_dir)

    def export(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            nodes = self.assembler.assemble_nodes(state)
            trace = self.assembler.trace_map(state)
            try:
                fonts = load_pdf_fonts()
            except PdfFontError as exc:
                raise PdfExportError(str(exc)) from exc
            # Read-only pre-check, before any byte is written.  A character is
            # only "missing" when neither the primary font nor the reviewed
            # bounded fallback can draw it; allow-listed equivalents are the last
            # resort for codepoints still uncovered.
            scan = scan_text_glyphs(state.get("units") or [], fonts.has_glyph)
            if scan["blocking"]:
                raise PdfExportError(blocking_message(scan))
            replacements: list[dict[str, str]] = []
            lines = self._document_lines(nodes, replacements=replacements, renderable=fonts.has_glyph)
            payload = self._render_pdf(lines, self._title(state), fonts=fonts)
            target = self._target_path(state)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(target)
            trace_path = self.assembler.write_trace_map(target, trace)
            metadata: dict[str, Any] = {
                "format": "pdf",
                "path": target.relative_to(self.runtime_dir).as_posix(),
                "trace_map_path": trace_path.relative_to(self.runtime_dir).as_posix(),
                "filename": target.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "exported_at": now_iso(),
                "included_unit_count": len(state.get("units") or []),
                "size_bytes": target.stat().st_size,
                "page_count": self._page_count(payload),
            }
            summary = summarize_replacements(replacements, scan)
            if summary is not None:
                metadata["character_replacements"] = summary
            return metadata
        except PdfExportError:
            raise
        except (OSError, ValueError) as exc:
            raise PdfExportError(f"PDF 生成失败：{exc}") from exc

    def _render_pdf(
        self,
        lines: list[tuple[str, float, float, bool]],
        title: str,
        *,
        fonts: PdfFontChain | None = None,
    ) -> bytes:
        try:
            from reportlab.pdfgen.canvas import Canvas
        except ImportError as exc:
            raise PdfExportError("PDF 导出需要 ReportLab，请按 requirements.txt 安装 reportlab。") from exc
        if fonts is None:
            try:
                fonts = load_pdf_fonts()
            except PdfFontError as exc:
                raise PdfExportError(str(exc)) from exc

        for text, _size, _indent, _is_heading in lines:
            if text != "\f":
                try:
                    fonts.validate_text(text)
                except PdfFontError as exc:
                    raise PdfExportError(str(exc)) from exc

        try:
            stream = io.BytesIO()
            canvas = Canvas(stream, pagesize=(self.PAGE_WIDTH, self.PAGE_HEIGHT), pageCompression=1)
            canvas.setTitle(title)
            canvas.setAuthor("Mode2 AutoTranslator MVP")

            pages = self._paginate(lines, fonts.char_width)
            for page_index, page_lines in enumerate(pages, 1):
                y = self.PAGE_HEIGHT - self.MARGIN_TOP
                for text, size, indent, _is_heading in page_lines:
                    cursor_x = self.MARGIN_X + indent
                    if text:
                        # One text object per line, switching the font between
                        # runs inside it.  A line covered by the primary font
                        # yields exactly one run (so ordinary text is unchanged),
                        # and text extraction keeps a mixed line continuous
                        # instead of breaking it at every font boundary.
                        text_object = canvas.beginText(cursor_x, y)
                        for run_text, font_name in fonts.runs(text):
                            if not run_text:
                                continue
                            text_object.setFont(font_name, size)
                            text_object.textOut(run_text)
                        canvas.drawText(text_object)
                    y -= self._line_height(size)
                if page_index < len(pages):
                    canvas.showPage()
            canvas.save()
            return stream.getvalue()
        except PdfExportError:
            raise
        except Exception as exc:  # ReportLab exposes multiple generation-time exception types.
            raise PdfExportError(f"PDF 生成失败：{exc}") from exc

    def _pdf_char_width(self, char: str, size: float) -> float:
        """Return the production font's measured width for one character."""

        try:
            return load_pdf_font().char_width(char, size)
        except PdfFontError as exc:
            raise PdfExportError(str(exc)) from exc

    def _render_pdf_fallback(self, lines: list[tuple[str, float, float, bool]], title: str) -> bytes:
        """Build the pre-portability PDF for diagnostics only; never public export."""

        objects = _PdfObjects()
        descendant_id = objects.add(
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
            # STSong's default CID width is one em.  Declare its actual
            # printable-ASCII widths instead of letting readers expand every
            # Latin character to a full CJK cell.
            b"/DW 1000 /W [1 ["
            + " ".join(str(width) for width in _STSONG_ASCII_WIDTHS).encode("ascii")
            + b"]] >>"
        )
        font_id = objects.add(
            f"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H "
            f"/DescendantFonts [{descendant_id} 0 R] >>".encode("ascii")
        )
        ascii_font_id = objects.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
        )
        pages_id = objects.reserve()
        page_ids: list[int] = []
        pages = self._paginate(lines, self._fallback_char_width)
        for page_lines in pages:
            commands: list[str] = []
            y = self.PAGE_HEIGHT - self.MARGIN_TOP
            for text, size, indent, _is_heading in page_lines:
                commands.extend(
                    self._fallback_text_commands(
                        text,
                        size,
                        self.MARGIN_X + indent,
                        y,
                    )
                )
                y -= self._line_height(size)
            stream = "\n".join(commands).encode("ascii")
            contents_id = objects.add(
                b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
            )
            page_id = objects.add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {self.PAGE_WIDTH:.2f} {self.PAGE_HEIGHT:.2f}] "
                    f"/Resources << /Font << /F1 {font_id} 0 R /F2 {ascii_font_id} 0 R >> >> /Contents {contents_id} 0 R >>"
                ).encode("ascii")
            )
            page_ids.append(page_id)
        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects.set(pages_id, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii"))
        catalog_id = objects.add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))
        info_id = objects.add(
            f"<< /Title <{self._pdf_info_text(title)}> /Producer (Mode2 AutoTranslator MVP) >>".encode("ascii")
        )
        return objects.render(catalog_id, info_id)

    def _document_lines(
        self,
        nodes: list[dict[str, Any]],
        replacements: list[dict[str, str]] | None = None,
        renderable: Any | None = None,
    ) -> list[tuple[str, float, float, bool]]:
        result: list[tuple[str, float, float, bool]] = []
        for node in self._legacy_layout_nodes(nodes):
            separator = str(node.get("separator_before") or "")
            metadata = node.get("attributes") or {}
            if result and metadata.get("page_break_before"):
                result.append(("\f", 0.0, 0.0, False))
            elif result and separator:
                result.append(("", 10.0, 0.0, False))
            text = str(node.get("translated_text") or "")
            if metadata.get("contents_entry"):
                page_label = str(metadata.get("toc_page_label") or "")
                text = self._plain_text(text)
                if page_label:
                    text = re.sub(
                        rf"\s*(?:{re.escape(page_label)}|\d{{1,4}}|[IVXLCDM]{{1,8}})\s*$",
                        "",
                        text,
                        flags=re.IGNORECASE,
                    ).rstrip()
                    text = f"{text}    {page_label}"
                indent = 0.0 if int(metadata.get("toc_level") or 0) == 0 else 12.0
                result.append((text, 10.5 if indent else 11.0, indent, False))
                continue
            if metadata.get("page_number") and result:
                result.append(("\f", 0.0, 0.0, False))
                result.append((f"[PDF Page {metadata['page_number']} ]".replace(" ]", "]"), 9.0, 0.0, False))
            node_type = node.get("type")
            if node_type == "heading":
                level = max(1, min(6, int(metadata.get("level") or 1)))
                text = re.sub(r"^\s*#{1,6}\s+", "", text)
                text = re.sub(r"\s*\n\s*", " ", text)
                result.append((self._plain_text(text), max(11.0, 19.0 - level * 1.5), 0.0, True))
                result.append(("", 8.0, 0.0, False))
                continue
            if node_type == "list":
                for line in text.splitlines():
                    item = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", line)
                    if item.strip():
                        result.append(("- " + self._plain_text(item), 11.0, 12.0, False))
                continue
            if node_type == "blockquote":
                for line in text.splitlines():
                    result.append(("| " + self._plain_text(re.sub(r"^\s*>\s?", "", line)), 10.5, 12.0, False))
                continue
            if node_type == "code":
                code_lines = text.splitlines()
                if code_lines and re.match(r"^\s*(?:```+|~~~+)", code_lines[0]):
                    code_lines = code_lines[1:]
                if code_lines and re.match(r"^\s*(?:```+|~~~+)\s*$", code_lines[-1]):
                    code_lines = code_lines[:-1]
                result.extend((line, 9.0, 12.0, False) for line in code_lines)
                continue
            if node_type == "table":
                for line in text.splitlines():
                    if all(re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*\|?\s*", part) for part in line.split("|")):
                        continue
                    result.append((self._plain_text(line), 10.0, 0.0, False))
                result.append(("", 6.0, 0.0, False))
                continue
            result.extend((self._plain_text(line), 11.0, 0.0, False) for line in text.splitlines() or [""])
        if replacements is None:
            return result
        # Apply the reviewed equivalences here, before measurement and wrapping,
        # so line widths use the character that will actually be drawn.
        converted: list[tuple[str, float, float, bool]] = []
        for line_text, size, indent, is_heading in result:
            if not line_text:
                converted.append((line_text, size, indent, is_heading))
                continue
            # Characters the fonts can draw are kept exactly as written; the
            # reviewed equivalences only cover what is still unrenderable.
            new_text, records = apply_equivalents(line_text, renderable=renderable)
            if records:
                replacements.extend(records)
            converted.append((new_text, size, indent, is_heading))
        return converted

    @classmethod
    def _legacy_layout_nodes(cls, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build an export-only layout view for projects imported before layout metadata.

        Older projects stored the right text and Unit links but flattened title
        and contents-page blocks.  This adapter creates display nodes only; it
        never writes them back to the project state or changes the trace map.
        Newer structured projects already carry layout metadata and pass
        through unchanged.
        """

        if not nodes or any(
            isinstance(node.get("attributes"), dict)
            and (node["attributes"].get("layout_role") or node["attributes"].get("contents_entry"))
            for node in nodes
        ):
            return nodes

        grouped: list[dict[str, Any]] = []
        front_matter = True
        index = 0
        while index < len(nodes):
            node = nodes[index]
            if node.get("type") != "heading":
                grouped.append(node)
                index += 1
                continue
            if front_matter and str(node.get("source") or "").strip().upper() == "PREFACE":
                front_matter = False
                grouped.append(node)
                index += 1
                continue
            if not front_matter:
                grouped.append(node)
                index += 1
                continue

            run = [node]
            pages = cls._source_pages(node)
            next_index = index + 1
            while next_index < len(nodes):
                candidate = nodes[next_index]
                if (
                    not pages
                    or candidate.get("type") != "heading"
                    or cls._source_pages(candidate) != pages
                ):
                    break
                run.append(candidate)
                next_index += 1
            if len(run) == 1:
                grouped.append(node)
            else:
                merged = dict(run[0])
                merged["source"] = " ".join(str(item.get("source") or "").strip() for item in run).strip()
                merged["translated_text"] = " ".join(
                    str(item.get("translated_text") or "").strip() for item in run
                ).strip()
                merged["unit_ids"] = [
                    unit_id
                    for item in run
                    for unit_id in (item.get("unit_ids") or [])
                ]
                attributes = dict(run[0].get("attributes") or {})
                attributes["legacy_display_group"] = True
                merged["attributes"] = attributes
                grouped.append(merged)
            index = next_index

        result: list[dict[str, Any]] = []
        in_contents = False
        for node in grouped:
            source = str(node.get("source") or "").strip()
            source_upper = source.upper()
            if source_upper == "CONTENTS":
                in_contents = True
                result.append(node)
                continue
            if in_contents and source_upper == "PREFACE":
                in_contents = False
                result.append(node)
                continue
            if not in_contents:
                result.append(node)
                continue
            result.extend(cls._legacy_contents_entries(node))
        return result

    @staticmethod
    def _source_pages(node: dict[str, Any]) -> tuple[int, ...]:
        attributes = node.get("attributes") or {}
        pages = node.get("source_page_numbers") or attributes.get("source_page_numbers") or []
        try:
            return tuple(int(page) for page in pages)
        except (TypeError, ValueError):
            return ()

    @classmethod
    def _legacy_contents_entries(cls, node: dict[str, Any]) -> list[dict[str, Any]]:
        source = str(node.get("source") or "")
        translated = str(node.get("translated_text") or "")
        labels = cls._toc_page_labels(source)
        if not labels:
            return [node]

        matches: list[tuple[str, int, int]] = []
        cursor = 0
        for label in labels:
            match = re.search(cls._page_label_pattern(label), translated[cursor:], flags=re.IGNORECASE)
            if match is None:
                unresolved = dict(node)
                attributes = dict(node.get("attributes") or {})
                attributes["legacy_contents_unresolved"] = True
                unresolved["attributes"] = attributes
                return [unresolved]
            start = cursor + match.start()
            end = cursor + match.end()
            matches.append((label, start, end))
            cursor = end

        entries: list[dict[str, Any]] = []
        segment_start = 0
        for entry_index, (label, _start, end) in enumerate(matches, 1):
            segment = translated[segment_start:end].strip()
            if not segment:
                continue
            entry = dict(node)
            entry["id"] = f"{node.get('id', 'legacy-node')}-toc-{entry_index}"
            entry["translated_text"] = segment
            entry["separator_before"] = node.get("separator_before", "") if not entries else ""
            attributes = dict(node.get("attributes") or {})
            attributes.update(
                {
                    "contents_entry": True,
                    "toc_page_label": label,
                    "toc_level": 0 if node.get("type") == "heading" else 1,
                    "toc_heading": node.get("type") == "heading",
                    "legacy_display_split": True,
                }
            )
            entry["attributes"] = attributes
            entries.append(entry)
            segment_start = end

        trailing = translated[segment_start:].strip()
        if trailing and entries:
            entries[-1]["translated_text"] = f"{entries[-1]['translated_text']} {trailing}"
        if not entries:
            return [node]
        return entries

    @staticmethod
    def _toc_page_labels(source: str) -> list[str]:
        pattern = re.compile(r"(?<![A-Za-z0-9])(?:[IVXLCDM]{1,8}|\d{1,4})(?![A-Za-z0-9])", re.IGNORECASE)
        matches = list(pattern.finditer(source))
        if len(matches) > 1 and re.match(r"^\s*\d+\s+", source):
            matches = matches[1:]
        return [match.group(0) for match in matches]

    @staticmethod
    def _page_label_pattern(label: str) -> str:
        if label.isdigit():
            return rf"(?<!\d){re.escape(label)}(?!\d)"
        return rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])"

    def _paginate(
        self,
        lines: list[tuple[str, float, float, bool]],
        char_width: Any | None = None,
    ) -> list[list[tuple[str, float, float, bool]]]:
        measure = char_width or self._fallback_char_width
        pages: list[list[tuple[str, float, float, bool]]] = [[]]
        y = self.PAGE_HEIGHT - self.MARGIN_TOP
        max_width = self.PAGE_WIDTH - 2 * self.MARGIN_X
        for text, size, indent, is_heading in lines:
            if text == "\f":
                if pages[-1]:
                    pages.append([])
                y = self.PAGE_HEIGHT - self.MARGIN_TOP
                continue
            wrapped = self._wrap_text(text, size, max_width - indent, measure) or [""]
            for line in wrapped:
                line_height = self._line_height(size)
                # A single paragraph can be taller than one page.  Check each
                # wrapped line rather than the whole paragraph so long source
                # blocks continue naturally onto following pages.
                if pages[-1] and y - line_height < self.MARGIN_BOTTOM:
                    pages.append([])
                    y = self.PAGE_HEIGHT - self.MARGIN_TOP
                pages[-1].append((line, size, indent, is_heading))
                y -= line_height
        return pages or [[]]

    @staticmethod
    def _wrap_text(
        value: str,
        size: float,
        max_width: float,
        char_width: Any | None = None,
    ) -> list[str]:
        if not value:
            return [""]
        measure = char_width or PdfExporter._fallback_char_width
        lines: list[str] = []
        current: list[str] = []
        width = 0.0

        def recalculate_break() -> int | None:
            break_index: int | None = None
            for index, character in enumerate(current, 1):
                if character.isspace() or east_asian_width(character) in {"W", "F"}:
                    break_index = index
            return break_index

        def recalculate_width() -> float:
            return sum(measure(char, size) for char in current)

        last_break: int | None = None
        for char in value:
            current_char_width = measure(char, size)
            if current and width + current_char_width > max_width:
                if char.isspace():
                    # A separating space may sit just beyond the edge.  Drop
                    # it and remember the preceding word as the next legal
                    # break instead of splitting that word on the space.
                    last_break = len(current)
                    continue
                if last_break is not None and last_break > 0:
                    line = "".join(current[:last_break]).rstrip()
                    if line:
                        lines.append(line)
                    current = current[last_break:]
                    while current and current[0].isspace():
                        current.pop(0)
                    width = recalculate_width()
                    last_break = recalculate_break()
                else:
                    lines.append("".join(current).rstrip())
                    current = []
                    width = 0.0
                    last_break = None
            current.append(char)
            width += current_char_width
            if char.isspace() or east_asian_width(char) in {"W", "F"}:
                last_break = len(current)
        if current:
            line = "".join(current).rstrip()
            if line:
                lines.append(line)
        return lines or [""]

    @staticmethod
    def _fallback_char_width(char: str, size: float) -> float:
        # This fixed metric belongs to the historical diagnostic path only.
        # The production renderer receives ``font.char_width`` from the
        # embedded font and never uses this helper.
        return size * (0.6 if PdfExporter._is_ascii_run_char(char) else 1.0)

    @staticmethod
    def _is_ascii_run_char(char: str) -> bool:
        codepoint = ord(char)
        return 32 <= codepoint < 127 or char in _FALLBACK_ASCII_REPLACEMENTS

    @classmethod
    def _fallback_runs(cls, text: str) -> list[tuple[bool, str, str]]:
        """Return ``(is_ascii, rendered_text, source_text)`` font runs."""

        runs: list[tuple[bool, str, str]] = []
        source_chars: list[str] = []
        run_ascii: bool | None = None

        def flush() -> None:
            nonlocal source_chars, run_ascii
            if not source_chars or run_ascii is None:
                return
            source_text = "".join(source_chars)
            rendered_text = (
                "".join(_FALLBACK_ASCII_REPLACEMENTS.get(char, char) for char in source_chars)
                if run_ascii
                else source_text
            )
            runs.append((run_ascii, rendered_text, source_text))
            source_chars = []
            run_ascii = None

        for char in text:
            is_ascii = 32 <= ord(char) < 127 or char in _FALLBACK_ASCII_REPLACEMENTS
            if run_ascii is None:
                run_ascii = is_ascii
            elif run_ascii != is_ascii:
                flush()
                run_ascii = is_ascii
            source_chars.append(char)
        flush()
        return runs

    @classmethod
    def _fallback_text_commands(
        cls,
        text: str,
        size: float,
        x: float,
        y: float,
    ) -> list[str]:
        """Emit mixed-font text runs for the dependency-free PDF renderer.

        STSong-Light is retained for CJK glyph coverage, while printable ASCII
        is drawn with a standard built-in font.  Some viewers render the Latin
        subset of a CID font as widely spaced or overlapping glyphs; switching
        fonts at run boundaries keeps the fallback readable without requiring
        an external font file.
        """

        if not text:
            return []
        commands: list[str] = []
        cursor_x = x
        for is_ascii, rendered_text, source_text in cls._fallback_runs(text):
            # Keep each font run in its own text object.  Poppler and some
            # older readers otherwise retain the CID font's character mapping
            # when a Type1 run follows STSong-Light in the same BT/ET block.
            commands.append("BT")
            commands.append(f"/F2 {size:g} Tf" if is_ascii else f"/F1 {size:g} Tf")
            commands.append(f"1 0 0 1 {cursor_x:.2f} {y:.2f} Tm")
            if is_ascii:
                commands.append(f"<{rendered_text.encode('ascii').hex().upper()}> Tj")
            else:
                commands.append(f"<{cls._pdf_text(rendered_text)}> Tj")
            commands.append("ET")
            cursor_x += sum(cls._fallback_char_width(char, size) for char in source_text)
        return commands

    @staticmethod
    def _line_height(size: float) -> float:
        return size * 1.45

    @staticmethod
    def _pdf_text(value: str) -> str:
        """Encode a Type0 font text string without a Unicode metadata BOM."""

        return value.encode("utf-16-be").hex().upper()

    @staticmethod
    def _pdf_info_text(value: str) -> str:
        """Encode a PDF Info Unicode string with its required UTF-16 BOM."""

        return ("\ufeff" + value).encode("utf-16-be").hex().upper()

    @staticmethod
    def _plain_text(value: str) -> str:
        value = html.unescape(value)
        # Collapse every whitespace run to one space.  A tab or newline is not a
        # drawable glyph, so it must never reach the font coverage check; the
        # earlier newline-only rule left tabs inside a line and failed the export.
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"[*_`]+", "", value)
        return value.strip()

    def _title(self, state: dict[str, Any]) -> str:
        return self.assembler.source_title(state)

    def _target_path(self, state: dict[str, Any]) -> Path:
        target = (self.output_root / f"{self.assembler.source_stem(state)}_translated.pdf").resolve()
        if target.parent != self.output_root:
            raise PdfExportError("PDF 输出路径必须位于当前项目的 output 目录内。")
        return target

    @staticmethod
    def _page_count(payload: bytes) -> int:
        # ReportLab serializes the page dictionary with a newline after
        # ``/Page`` while the dependency-free writer keeps ``/Parent`` on the
        # same line.  Match the token boundary instead of one exact spacing
        # shape, and avoid counting the plural ``/Pages`` dictionary.
        return max(1, len(re.findall(rb"/Type\s*/Page(?:\s|/)", payload)))
