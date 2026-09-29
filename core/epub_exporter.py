"""Generate a standards-shaped EPUB from the materialized document nodes."""

from __future__ import annotations

import html
import hashlib
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .assembler import DocumentAssembler
from .epub_layout import (
    EpubLayout,
    NavigationEntry,
    build_epub_layout,
    source_toc_level,
    split_translated_paragraphs,
)
from .exceptions import PipelineError
from .utils import now_iso


class EpubExportError(PipelineError):
    """The project cannot be written as a valid EPUB."""


class EpubExporter:
    """Write a reflowable EPUB3 while preserving node and Unit trace IDs."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.output_root = (self.runtime_dir / "output").resolve()
        self.assembler = DocumentAssembler(self.runtime_dir)

    def export(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            nodes = self.assembler.assemble_nodes(state)
            trace = self.assembler.trace_map(state)
            layout = build_epub_layout(nodes)
            chapters = [list(chapter) for chapter in layout.chapters]
            title = self._title(state)
            target = self._target_path(state)
            target.parent.mkdir(parents=True, exist_ok=True)
            book_id = f"urn:uuid:{uuid.uuid4()}"
            chapter_files = [f"text/chapter-{index:03d}.xhtml" for index in range(1, len(chapters) + 1)]
            payload = self._build_archive(
                title=title,
                language=str((state.get("config") or {}).get("target_language") or "zh-CN"),
                book_id=book_id,
                chapters=chapters,
                chapter_files=chapter_files,
                trace=trace,
                layout=layout,
            )
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(target)
            trace_path = self.assembler.write_trace_map(target, trace)
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is not None:
                    raise EpubExportError("EPUB 压缩包完整性检查失败。")
            return {
                "format": "epub",
                "path": target.relative_to(self.runtime_dir).as_posix(),
                "trace_map_path": trace_path.relative_to(self.runtime_dir).as_posix(),
                "filename": target.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "exported_at": now_iso(),
                "included_unit_count": len(state.get("units") or []),
                "size_bytes": target.stat().st_size,
                "chapter_count": len(chapters),
            }
        except EpubExportError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise EpubExportError(f"EPUB 生成失败：{exc}") from exc

    def _build_archive(
        self,
        *,
        title: str,
        language: str,
        book_id: str,
        chapters: list[list[dict[str, Any]]],
        chapter_files: list[str],
        trace: dict[str, Any],
        layout: EpubLayout | None = None,
    ) -> bytes:
        modified = now_iso().replace("+00:00", "Z")
        container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
        manifest_items = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="style" href="styles.css" media-type="text/css"/>',
            '<item id="trace" href="trace-map.json" media-type="application/json"/>',
        ]
        for index, path in enumerate(chapter_files, 1):
            manifest_items.append(
                f'<item id="chapter-{index:03d}" href="{path}" media-type="application/xhtml+xml"/>'
            )
        spine_items = "\n    ".join(
            f'<itemref idref="chapter-{index:03d}"/>' for index in range(1, len(chapters) + 1)
        )
        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{html.escape(book_id)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:language>{html.escape(language)}</dc:language>
    <meta property="dcterms:modified">{html.escape(modified)}</meta>
  </metadata>
  <manifest>
    {chr(10).join('    ' + item for item in manifest_items)}
  </manifest>
  <spine>
    {spine_items}
  </spine>
</package>
"""
        nav_links = []
        navigation = layout.navigation if layout is not None else self._fallback_navigation(chapters)
        for entry in navigation:
            if not entry.label:
                continue
            href = chapter_files[entry.chapter_index]
            nav_links.append(
                f'<li class="nav-level-{entry.level}"><a href="{html.escape(href)}#'
                f'{html.escape(entry.node_id)}">{html.escape(self._plain_text(entry.label))}</a></li>'
            )
        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
  <body><nav epub:type="toc" id="toc"><h1>目录</h1><ol class="nav-list">{''.join(nav_links)}</ol></nav></body>
</html>
"""
        css = """body { font-family: serif; line-height: 1.65; margin: 1em; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.4em 0 0.6em; }
h1.front-title { text-align: center; font-size: 1.65em; line-height: 1.2; margin: 1.1em 0 0.7em; }
h1.chapter-fragment, h2.chapter-fragment { font-size: 1.1em; line-height: 1.25; margin: 0.15em 0 0.45em; }
.frontmatter { margin: 0 0 1em; }
.source-centered { text-align: center; }
.source-title { font-size: 1.55em; line-height: 1.2; margin: 1.1em 0 0.7em; }
body.copyright-page { padding-top: 7em; }
.source-block-group > p { margin: 0 0 0.75em; }
.footnote-group { margin-top: 1.2em; font-size: 0.88em; line-height: 1.45; }
.footnote { margin: 0 0 0.45em; padding-left: 1.35em; text-indent: -1.35em; }
.contents-title { margin-top: 0.8em; }
.contents-running { font-size: 0.9em; font-weight: normal; text-align: center; margin: 1.2em 0 0.35em; }
.contents-block { margin: 0.15em 0 0.45em; }
.contents-entry { display: flex; align-items: baseline; gap: 0.5em; padding: 0.15em 0; line-height: 1.35; }
.contents-label { flex: 1 1 auto; min-width: 0; }
.contents-page { flex: 0 0 auto; margin-left: auto; min-width: 2.5em; padding-left: 0.5em; text-align: right; font-variant-numeric: tabular-nums; }
.contents-entry.contents-level-2 { padding-left: 1.25em; }
.contents-unresolved { white-space: normal; }
.nav-list { padding-left: 1.4em; }
.nav-list li { margin: 0.15em 0; }
.nav-list li.nav-level-2 { margin-left: 1.25em; }
p { margin: 0 0 1em; white-space: normal; }
blockquote { margin: 1em 1.5em; color: #444; }
pre { white-space: pre-wrap; font-family: monospace; background: #f5f5f5; padding: 0.7em; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #999; padding: 0.35em; vertical-align: top; }
.page-marker { break-before: page; color: #666; font-size: 0.85em; }
"""
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OEBPS/content.opf", opf)
            archive.writestr("OEBPS/nav.xhtml", nav)
            archive.writestr("OEBPS/styles.css", css)
            archive.writestr("OEBPS/trace-map.json", json.dumps(trace, ensure_ascii=False, indent=2))
            for path, chapter in zip(chapter_files, chapters):
                copyright_page = any(
                    "copyright" in str((node.get("attributes") or {}).get("chapter_file") or "")
                    .replace("\\", "/").rsplit("/", 1)[-1].casefold()
                    for node in chapter
                )
                body_class = ' class="copyright-page"' if copyright_page else ""
                body = "\n".join(
                    self._node_markup(
                        node,
                        role=(layout.roles.get(str(node.get("id") or "")) if layout else None),
                    )
                    for node in chapter
                )
                xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(language)}">
  <head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="../styles.css"/></head>
  <body{body_class}>{body}</body>
</html>
"""
                archive.writestr(f"OEBPS/{path}", xhtml)
        return archive_buffer.getvalue()

    @staticmethod
    def _chapter_groups(nodes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [list(chapter) for chapter in build_epub_layout(nodes).chapters]

    def _node_markup(self, node: dict[str, Any], *, role: str | None = None) -> str:
        node_id = html.escape(str(node["id"]))
        unit_ids = html.escape(" ".join(node.get("unit_ids") or []))
        attrs = f'id="{node_id}" data-unit-ids="{unit_ids}"'
        text = str(node.get("translated_text") or "")
        node_type = node.get("type")
        metadata = node.get("attributes") or {}
        source_classes = self._source_style_classes(node)
        if role in {"contents", "contents-running"}:
            return self._contents_markup(node, attrs, text, role=role)
        if node_type == "heading":
            level = max(1, min(6, int(metadata.get("level") or 1)))
            text = re.sub(r"^\s*#{1,6}\s+", "", text)
            classes = [
                "front-title"
                if role == "title" or "source-title" in source_classes
                else "chapter-heading"
                if role == "chapter"
                else "chapter-fragment"
                if role == "chapter-fragment"
                else ""
            ]
            classes.extend(source_classes)
            classes = list(dict.fromkeys(value for value in classes if value))
            class_attr = f' class="{" ".join(classes)}"' if classes else ""
            return f"<h{level}{class_attr} {attrs}>{self._inline_markup(text)}</h{level}>"
        if node_type == "list":
            ordered = bool(metadata.get("ordered"))
            tag = "ol" if ordered else "ul"
            items = []
            for line in text.splitlines():
                item = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", line)
                if item.strip():
                    items.append(f"<li>{self._inline_markup(item)}</li>")
            return f"<{tag} {attrs}>{''.join(items)}</{tag}>"
        if node_type == "blockquote":
            lines = [re.sub(r"^\s*>\s?", "", line) for line in text.splitlines()]
            return f"<blockquote {attrs}>{self._multiline_markup(lines)}</blockquote>"
        if node_type == "code":
            lines = text.splitlines()
            if lines and re.match(r"^\s*(?:```+|~~~+)", lines[0]):
                lines = lines[1:]
            if lines and re.match(r"^\s*(?:```+|~~~+)\s*$", lines[-1]):
                lines = lines[:-1]
            return f"<pre {attrs}><code>{html.escape(chr(10).join(lines))}</code></pre>"
        if node_type == "table":
            rows = []
            for row_index, line in enumerate(text.splitlines()):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if not cells or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    continue
                tag = "th" if row_index == 0 else "td"
                rows.append("<tr>" + "".join(f"<{tag}>{self._inline_markup(cell)}</{tag}>" for cell in cells) + "</tr>")
            return f"<table {attrs}><tbody>{''.join(rows)}</tbody></table>"
        if metadata.get("page_number"):
            classes = ["page-marker"]
        else:
            classes = []
        if role == "frontmatter":
            classes.append("frontmatter")
        classes.extend(source_classes)
        classes = list(dict.fromkeys(classes))
        paragraphs = split_translated_paragraphs(node)
        if len(paragraphs) > 1:
            wrapper_classes = [value for value in classes if value != "footnote"]
            if "footnote" in source_classes:
                wrapper_classes.append("footnote-group")
            wrapper_classes = list(dict.fromkeys([*wrapper_classes, "source-block-group"]))
            wrapper_attrs = f'{attrs} class="{" ".join(wrapper_classes)}"'
            paragraph_classes = [
                value for value in source_classes if value in {"footnote"}
            ]
            paragraph_class_attr = (
                f' class="{" ".join(paragraph_classes)}"' if paragraph_classes else ""
            )
            children = "".join(
                f"<p{paragraph_class_attr}>{self._multiline_markup(paragraph.splitlines())}</p>"
                for paragraph in paragraphs
            )
            return f"<div {wrapper_attrs}>{children}</div>"
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        return f"<p {attrs}{class_attr}>{self._multiline_markup(text.splitlines())}</p>"

    @staticmethod
    def _source_style_classes(node: dict[str, Any]) -> list[str]:
        metadata = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
        tokens = {
            str(value).strip().casefold()
            for value in metadata.get("class_tokens") or []
            if str(value).strip()
        }
        chapter_file = str(metadata.get("chapter_file") or "").replace("\\", "/")
        filename = chapter_file.rsplit("/", 1)[-1].casefold()
        structure_type = str(metadata.get("structure_type") or "").casefold()
        classes: list[str] = []
        if structure_type in {"footnote", "endnote"} or any(
            token == "footnote" or token.startswith(("footnote-", "endnote"))
            for token in tokens
        ):
            classes.append("footnote")
        if (
            any(token.startswith("center") for token in tokens)
            or tokens.intersection({"author", "dedication", "half-title"})
            or "copyright" in filename
            or any("copyright" in token for token in tokens)
        ):
            classes.append("source-centered")
        if "book-title" in tokens:
            classes.append("source-title")
        return list(dict.fromkeys(classes))

    def _contents_markup(
        self,
        node: dict[str, Any],
        attrs: str,
        text: str,
        *,
        role: str = "contents",
    ) -> str:
        is_contents_header = self._plain_text(text).strip() in {"CONTENTS", "目录"}
        metadata = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
        if str(metadata.get("structure_type") or "").casefold() == "toc_entry":
            rows = []
            for entry in split_translated_paragraphs(node):
                label = entry.strip()
                if not label:
                    continue
                level = source_toc_level(label)
                rows.append(
                    f'<div class="contents-entry contents-level-{level}">'
                    f'<span class="contents-label">{self._multiline_markup(label.splitlines())}</span></div>'
                )
            return f'<div class="contents-block" {attrs}>{"".join(rows)}</div>'
        entries = self._contents_entries(text)
        if node.get("type") == "heading" and (not entries or is_contents_header):
            heading_class = "contents-running" if role == "contents-running" else "contents-title"
            heading_level = "h2" if role == "contents-running" else "h1"
            return f'<{heading_level} class="{heading_class}" {attrs}>{self._inline_markup(text)}</{heading_level}>'
        if not entries:
            return f'<div class="contents-block contents-unresolved" {attrs}>{self._multiline_markup(text.splitlines())}</div>'
        rows = []
        for index, (label, page) in enumerate(entries):
            level = 1 if node.get("type") == "heading" and index == 0 else 2 if node.get("type") != "heading" else 1
            row_class = f"contents-entry contents-level-{level}"
            page_markup = f'<span class="contents-page">{self._inline_markup(page)}</span>' if page else ""
            rows.append(
                f'<div class="{row_class}"><span class="contents-label">{self._inline_markup(label)}</span>{page_markup}</div>'
            )
        return f'<div class="contents-block" {attrs}>{"".join(rows)}</div>'

    @staticmethod
    def _contents_entries(value: str) -> list[tuple[str, str]]:
        from .epub_layout import parse_contents_entries

        return parse_contents_entries(value)

    @staticmethod
    def _fallback_navigation(chapters: list[list[dict[str, Any]]]) -> tuple[NavigationEntry, ...]:
        entries: list[NavigationEntry] = []
        for index, chapter in enumerate(chapters):
            heading = next((node for node in chapter if node.get("type") == "heading"), None)
            anchor = str((heading or chapter[0]).get("id") or "")
            label = str((heading or {}).get("translated_text") or f"第 {index + 1} 章")
            entries.append(NavigationEntry(anchor, label, 1, "chapter", index))
        return tuple(entries)

    @classmethod
    def _multiline_markup(cls, lines: list[str]) -> str:
        return "<br />".join(cls._inline_markup(line) for line in lines)

    @staticmethod
    def _inline_markup(value: str) -> str:
        escaped = html.escape(value, quote=False)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
        return escaped

    @staticmethod
    def _plain_text(value: str) -> str:
        return re.sub(r"[*_`#]", "", value).strip()

    def _title(self, state: dict[str, Any]) -> str:
        return self.assembler.source_title(state)

    def _target_path(self, state: dict[str, Any]) -> Path:
        target = (self.output_root / f"{self.assembler.source_stem(state)}_translated.epub").resolve()
        if target.parent != self.output_root:
            raise EpubExportError("EPUB 输出路径必须位于当前项目的 output 目录内。")
        return target
