"""Read EPUB spine documents into compact, source-ranged structure blocks.

The extractor follows only local package metadata and never fetches linked
resources. It intentionally records semantic structure rather than retaining
an HTML tree or reproducing EPUB layout.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


_BLOCK_SEPARATOR = "\ue000"
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "dd", "div", "dl", "dt", "figcaption",
        "figure", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "pre",
        "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)
_SKIP_TAGS = frozenset({"head", "title", "script", "style", "svg", "noscript", "template"})
_FOOTNOTE_CLASSES = frozenset({"footnote", "footnotes", "endnote", "endnotes"})
_TOC_CLASSES = frozenset({"toc", "contents", "table-of-contents"})
_FOOTNOTE_TYPES = frozenset({"footnote", "footnotes", "endnote", "endnotes", "rearnote"})
_FOOTNOTE_ROLES = frozenset({"doc-footnote", "doc-endnote", "doc-rearnote"})
_TOC_TYPES = frozenset({"toc"})
_TOC_ROLES = frozenset({"doc-toc", "navigation"})


def extract_epub_structure(content: bytes) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Return normalized spine text and ranges for its semantic source blocks."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            paths = epub_document_paths(archive)
            raw_blocks: list[dict[str, Any]] = []
            for chapter_file in paths:
                try:
                    html = archive.read(chapter_file).decode("utf-8-sig", errors="replace")
                except KeyError:
                    continue
                list_specs = _list_metadata(html)
                parser = _StructuredHtmlParser(chapter_file, list_specs)
                parser.feed(html)
                parser.close()
                raw_blocks.extend(parser.blocks)
    except (zipfile.BadZipFile, OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"EPUB 解析失败：{exc}") from exc

    text_parts: list[str] = []
    blocks: list[dict[str, Any]] = []
    cursor = 0
    has_text = False
    for order, raw_block in enumerate(raw_blocks):
        block_text = str(raw_block.get("text") or "")
        if block_text:
            if has_text:
                text_parts.append("\n\n")
                cursor += 2
            start = cursor
            text_parts.append(block_text)
            cursor += len(block_text)
            end = cursor
            has_text = True
        else:
            # Empty structural markers (for example an image) remain in the
            # ordered manifest without injecting placeholder text for models.
            start = end = cursor
        block = {key: value for key, value in raw_block.items() if key != "text"}
        block.update({"block_order": order, "start": start, "end": end, "text": block_text})
        blocks.append(block)
    return "".join(text_parts).strip(), tuple(blocks)


def epub_document_paths(archive: zipfile.ZipFile) -> list[str]:
    """Resolve local XHTML/HTML documents in OPF spine order."""
    names = set(archive.namelist())
    fallback = sorted(
        name for name in names if name.casefold().endswith((".xhtml", ".html", ".htm"))
    )
    container_name = "META-INF/container.xml"
    if container_name not in names:
        return fallback
    container_root = ElementTree.fromstring(archive.read(container_name))
    rootfile = next(
        (element for element in container_root.iter() if _local_name(element.tag) == "rootfile"),
        None,
    )
    if rootfile is None or not rootfile.attrib.get("full-path"):
        return fallback
    opf_path = posixpath.normpath(unquote(rootfile.attrib["full-path"]))
    opf_root = ElementTree.fromstring(archive.read(opf_path))
    manifest: dict[str, str] = {}
    for element in opf_root.iter():
        if _local_name(element.tag) != "item" or not element.attrib.get("id"):
            continue
        href = element.attrib.get("href")
        media_type = element.attrib.get("media-type", "")
        if not href or not ("html" in media_type or PurePosixPath(href).suffix.casefold() in {".xhtml", ".html", ".htm"}):
            continue
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            continue
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(opf_path), unquote(parsed.path))
        )
        manifest[element.attrib["id"]] = path
    ordered: list[str] = []
    for element in opf_root.iter():
        if _local_name(element.tag) != "itemref":
            continue
        path = manifest.get(element.attrib.get("idref", ""))
        if path in names:
            ordered.append(path)
    return ordered or [path for path in sorted(manifest.values()) if path in names]


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].casefold()


def _list_metadata(html: str) -> list[dict[str, Any]]:
    parser = _ListMetadataParser()
    parser.feed(html)
    parser.close()
    return parser.lists


class _ListMetadataParser(HTMLParser):
    """Count direct list items so reversed ordered lists keep their numbers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.lists: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        frame: dict[str, Any] = {"tag": tag}
        if tag in {"ol", "ul"}:
            frame["list_index"] = len(self.lists)
            self.lists.append({"tag": tag, "item_count": 0})
        elif tag == "li":
            list_frame = next(
                (item for item in reversed(self.stack) if item.get("list_index") is not None),
                None,
            )
            if list_frame is not None:
                self.lists[list_frame["list_index"]]["item_count"] += 1
        self.stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        match_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index]["tag"] == tag),
            None,
        )
        if match_index is not None:
            del self.stack[match_index:]


class _StructuredHtmlParser(HTMLParser):
    def __init__(self, chapter_file: str, list_specs: list[dict[str, Any]]) -> None:
        super().__init__(convert_charrefs=True)
        self.chapter_file = chapter_file
        self.list_specs = list_specs
        self.stack: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None
        self.pending_text: list[str] = []
        self.body_seen = False
        self.section_number = 0
        self.area_number = 0
        self.list_number = 0
        self.toc_number = 0
        self.scope_numbers = {"footnote": 0, "toc": 0}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        parent = self.stack[-1] if self.stack else {}
        parent_item = next(
            (frame for frame in reversed(self.stack) if frame["tag"] == "li"),
            None,
        )
        skip = bool(parent.get("skip")) or tag in _SKIP_TAGS
        if tag == "body":
            self.body_seen = True

        scope = self._semantic_scope(attributes, tag) or parent.get("scope")
        area_id = parent.get("area_id") or f"{self.chapter_file}#area-root"
        if tag in {"section", "article"}:
            self.area_number += 1
            area_id = f"{self.chapter_file}#area-{self.area_number}"
        scope_id = parent.get("scope_id") if scope == parent.get("scope") else None
        list_id = parent.get("list_id")
        list_counter = None
        list_style = ""
        list_reversed = False
        parent_item_id = None
        item_id = None
        if tag in {"ol", "ul"}:
            list_spec = (
                self.list_specs[self.list_number]
                if self.list_number < len(self.list_specs)
                else {}
            )
            self.list_number += 1
            list_id = f"{self.chapter_file}#list-{self.list_number}"
            list_style = attributes.get("type", "")
            list_reversed = "reversed" in attributes
            parent_item_id = parent_item.get("item_id") if parent_item else None
            try:
                if "start" in attributes:
                    list_counter = int(attributes["start"])
                elif list_reversed:
                    list_counter = int(list_spec.get("item_count", 0))
                else:
                    list_counter = 1
            except ValueError:
                list_counter = 1
        list_marker = None
        list_ordinal = None
        if tag == "li":
            list_frame = next(
                (frame for frame in reversed(self.stack) if frame["tag"] in {"ol", "ul"}),
                None,
            )
            if list_frame is not None and list_frame["tag"] == "ol":
                list_style = list_frame.get("list_style", "")
                list_reversed = list_frame.get("list_reversed", False)
                try:
                    list_ordinal = int(attributes.get("value", list_frame.get("list_counter", 1)))
                except ValueError:
                    list_ordinal = list_frame.get("list_counter", 1)
                step = -1 if list_frame.get("list_reversed") else 1
                list_frame["list_counter"] = list_ordinal + step
                list_marker = _ordered_list_marker(list_ordinal, list_frame.get("list_style", ""))
            elif list_frame is not None:
                list_style = list_frame.get("list_style", "")
                list_ordinal = int(list_frame.get("list_counter", 1))
                list_frame["list_counter"] = list_ordinal + 1
                list_marker = "• "
            if list_frame is not None:
                parent_item_id = list_frame.get("parent_item_id")
                item_id = attributes.get("id") or (
                    f"{list_frame['list_id']}#item-{list_ordinal}"
                    if list_ordinal is not None
                    else f"{list_frame['list_id']}#item"
                )
        toc_id = parent.get("toc_id")
        classes = set(attributes.get("class", "").casefold().split())
        roles = set(attributes.get("role", "").casefold().split())
        epub_types = set(attributes.get("epub:type", "").casefold().split())
        toc_container = (
            tag in {"nav", "ol", "ul"}
            or bool(classes & _TOC_CLASSES)
            or bool(roles & _TOC_ROLES)
            or bool(epub_types & _TOC_TYPES)
        )
        if scope == "toc" and parent.get("scope") != "toc" and toc_container:
            self.toc_number += 1
            toc_id = f"{self.chapter_file}#toc-{self.toc_number}"
        scope_container = (
            tag in {"aside", "div", "section", "article", "nav", "ol", "ul"}
            and scope in self.scope_numbers
            and parent.get("scope") != scope
        )
        if scope_container:
            self.scope_numbers[scope] += 1
            scope_id = f"{self.chapter_file}#{scope}-{self.scope_numbers[scope]}"
        frame = {
            "tag": tag,
            "skip": skip,
            "scope": scope,
            "scope_id": scope_id,
            "area_id": area_id,
            "list_id": list_id,
            "toc_id": toc_id,
            "attributes": attributes,
            "list_counter": list_counter,
            "list_style": list_style,
            "list_reversed": list_reversed,
            "list_marker": list_marker,
            "list_ordinal": list_ordinal,
            "parent_item_id": parent_item_id,
            "item_id": item_id,
        }

        visible = not skip and self._inside_body(parent, tag)
        if visible:
            if tag == "br":
                if self.active is not None:
                    self.active["pieces"].append(" ")
                else:
                    self.pending_text.append(" ")
            elif tag == "img":
                self._flush_pending()
                if self.active:
                    self._finish_active()
                self._append_block("image", "", tag, attributes, f"{self.chapter_file}#section-{self.section_number}:image")
            elif tag == "hr":
                self._flush_pending()
                if self.active:
                    self._finish_active()
                self._append_block("raw", "", tag, attributes, f"{self.chapter_file}#section-{self.section_number}:barrier")
            else:
                self._start_or_separate_block(tag, attributes, frame)
        self.stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        match_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index]["tag"] == tag),
            None,
        )
        if match_index is None:
            return
        if tag == "body":
            self._flush_pending()
        if self.active and match_index == self.active["root_stack_index"]:
            self._finish_active()
        elif self.active and tag in _BLOCK_TAGS:
            self.active["pieces"].append(_BLOCK_SEPARATOR)
        elif not self.active and tag in _BLOCK_TAGS:
            self._flush_pending()
        del self.stack[match_index:]

    def handle_data(self, data: str) -> None:
        if not self._visible_now():
            return
        if self.active is not None:
            self.active["pieces"].append(data)
        elif data:
            self.pending_text.append(data)

    def handle_entityref(self, name: str) -> None:  # pragma: no cover - convert_charrefs handles normal HTML
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:  # pragma: no cover - convert_charrefs handles normal HTML
        self.handle_data(f"&#{name};")

    def close(self) -> None:
        super().close()
        self._flush_pending()
        if self.active:
            self._finish_active()

    def _inside_body(self, parent: dict[str, Any], current_tag: str) -> bool:
        if self.body_seen:
            return current_tag == "body" or any(frame["tag"] == "body" for frame in self.stack)
        return not any(frame["tag"] == "head" or frame["skip"] for frame in self.stack)

    def _visible_now(self) -> bool:
        if any(frame["skip"] for frame in self.stack):
            return False
        if self.body_seen:
            return any(frame["tag"] == "body" for frame in self.stack)
        return True

    def _start_or_separate_block(
        self,
        tag: str,
        attributes: dict[str, str],
        frame: dict[str, Any],
    ) -> None:
        if tag in {"ol", "ul"} and self.active and self.active["kind"] == "list_item":
            self._finish_active()
        kind = self._block_kind(tag, attributes, frame.get("scope"))
        if kind is None:
            return
        self._flush_pending()
        if self.active is not None:
            self.active["pieces"].append(_BLOCK_SEPARATOR)
            return
        if kind == "heading":
            self.section_number += 1
        scope = frame.get("scope")
        section = self.section_number
        list_id = frame.get("list_id")
        toc_id = frame.get("toc_id")
        area_id = frame.get("area_id") or f"{self.chapter_file}#area-root"
        scope_id = frame.get("scope_id")
        if kind == "list_item":
            group_id = str(list_id or f"{self.chapter_file}#section-{section}:list-unmarked")
        elif kind == "toc_entry":
            group_id = str(list_id or toc_id or scope_id or f"{area_id}#section-{section}:toc")
        elif kind == "footnote":
            group_id = str(scope_id or f"{area_id}#section-{section}:footnote")
        else:
            group_id = f"{area_id}#section-{section}:{kind}"
        if kind == "heading":
            group_id = f"{area_id}#heading-{section}"
        level = int(tag[1]) if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit() else None
        self.active = {
            "kind": kind,
            "tag": tag,
            "root_stack_index": len(self.stack),
            "pieces": [],
            "group_id": group_id,
            "container_id": list_id if kind == "list_item" else (toc_id if kind == "toc_entry" else None),
            "heading_level": level,
            "attributes": self._source_attributes(attributes),
            "list_marker": frame.get("list_marker"),
            "list_ordinal": frame.get("list_ordinal"),
            "list_style": frame.get("list_style", ""),
        }
        if frame.get("list_marker") is not None:
            self.active["attributes"]["list_marker"] = frame["list_marker"]
        if frame.get("list_ordinal") is not None:
            self.active["attributes"]["list_ordinal"] = frame["list_ordinal"]
        if frame.get("parent_item_id") is not None:
            self.active["attributes"]["parent_item_id"] = frame["parent_item_id"]

    @staticmethod
    def _block_kind(tag: str, attributes: dict[str, str], scope: str | None) -> str | None:
        if re.fullmatch(r"h[1-6]", tag):
            return "heading"
        if tag == "table":
            return "table"
        if tag == "pre":
            return "code"
        if tag == "blockquote":
            return "blockquote"
        classes = set(attributes.get("class", "").casefold().split())
        if tag == "li":
            return "toc_entry" if scope == "toc" else "list_item"
        if tag in {"p", "dd", "dt", "figcaption"}:
            if scope == "footnote" or _has_footnote_class(classes):
                return "footnote"
            if scope == "toc" or _has_toc_class(classes):
                return "toc_entry"
            return "paragraph"
        if tag in {"aside", "div"}:
            # These elements establish semantic scope. Their child paragraphs
            # are the individual entries; treating the wrapper as one block
            # would erase per-note/per-entry identity.
            return None
        return None

    @staticmethod
    def _source_attributes(attributes: dict[str, str]) -> dict[str, Any]:
        classes = attributes.get("class", "").split()
        values: dict[str, Any] = {}
        if classes:
            values["class_tokens"] = classes
        if attributes.get("role"):
            values["aria_role"] = attributes["role"]
        if attributes.get("epub:type"):
            values["epub_type"] = attributes["epub:type"].split()
        if attributes.get("id"):
            values["source_id"] = attributes["id"]
        return values

    @staticmethod
    def _semantic_scope(attributes: dict[str, str], tag: str = "") -> str | None:
        epub_types = set(attributes.get("epub:type", "").casefold().split())
        roles = set(attributes.get("role", "").casefold().split())
        classes = set(attributes.get("class", "").casefold().split())
        if epub_types & _FOOTNOTE_TYPES or roles & _FOOTNOTE_ROLES or _has_footnote_class(classes):
            return "footnote"
        if tag == "nav" or epub_types & _TOC_TYPES or roles & _TOC_ROLES or _has_toc_class(classes):
            return "toc"
        return None

    def _flush_pending(self) -> None:
        if not self.pending_text:
            return
        text = _normalize_block_text("".join(self.pending_text))
        self.pending_text.clear()
        if not text:
            return
        frame = self.stack[-1] if self.stack else {}
        scope = frame.get("scope")
        kind = "footnote" if scope == "footnote" else ("toc_entry" if scope == "toc" else "paragraph")
        group_id = frame.get("scope_id") or (
            f"{frame.get('area_id') or self.chapter_file + '#area-root'}#section-{self.section_number}:{kind}"
        )
        self._append_block(
            kind,
            text,
            "#text",
            frame.get("attributes", {}),
            str(group_id),
        )

    def _finish_active(self) -> None:
        active = self.active
        self.active = None
        if active is None:
            return
        text = _normalize_block_text("".join(active["pieces"]))
        list_marker = active.get("list_marker")
        if (
            text
            and list_marker
            and not text.startswith(list_marker)
            and not _has_source_list_marker(
                text,
                active.get("list_ordinal"),
                active.get("list_style", ""),
                list_marker,
            )
        ):
            text = f"{list_marker}{text}"
        if not text and active["kind"] not in {"image", "raw"}:
            return
        block = {
            "chapter_file": self.chapter_file,
            "kind": active["kind"],
            "group_id": active["group_id"],
            "container_id": active["container_id"],
            "source_tag": active["tag"],
            "attributes": active["attributes"],
            "text": text,
        }
        if active["heading_level"] is not None:
            block["heading_level"] = active["heading_level"]
        self.blocks.append(block)

    def _append_block(
        self,
        kind: str,
        text: str,
        tag: str,
        attributes: dict[str, str],
        group_id: str,
    ) -> None:
        self.blocks.append(
            {
                "chapter_file": self.chapter_file,
                "kind": kind,
                "group_id": group_id,
                "container_id": None,
                "source_tag": tag,
                "attributes": self._source_attributes(attributes),
                "text": text,
            }
        )


def _has_footnote_class(classes: set[str]) -> bool:
    return any(
        token in _FOOTNOTE_CLASSES or token.startswith(("footnote-", "endnote-"))
        for token in classes
    )


def _has_toc_class(classes: set[str]) -> bool:
    return any(
        token in _TOC_CLASSES or re.fullmatch(r"toc-entry\d*", token)
        for token in classes
    )


def _ordered_list_marker(ordinal: int, style: str) -> str:
    if style in {"a", "A"} and ordinal > 0:
        letters = ""
        value = ordinal
        while value:
            value, remainder = divmod(value - 1, 26)
            letters = chr(ord("a") + remainder) + letters
        return f"{letters.upper() if style == 'A' else letters}. "
    if style in {"i", "I"} and 0 < ordinal < 4000:
        values = (
            (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
            (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
        )
        remaining = ordinal
        roman = ""
        for value, symbol in values:
            count, remaining = divmod(remaining, value)
            roman += symbol * count
        return f"{roman if style == 'I' else roman.lower()}. "
    return f"{ordinal}. "


def _has_source_list_marker(
    text: str, ordinal: int | None, style: str, expected_marker: str
) -> bool:
    if expected_marker.startswith("•"):
        return bool(re.match(r"^\s*[-+*•▪◦‣]\s+", text))
    match = re.match(r"^\s*([A-Za-z]+|\d+)[.)]\s+", text)
    if not match:
        return False
    label = match.group(1)
    if style in {"a", "A", "i", "I"}:
        expected_label = expected_marker.split(".", 1)[0]
        return label.casefold() == expected_label.casefold()
    return label.isdigit() and ordinal is not None and int(label) == ordinal


def _normalize_block_text(value: str) -> str:
    segments = []
    for raw_segment in value.split(_BLOCK_SEPARATOR):
        normalized = re.sub(r"\s+", " ", raw_segment.replace("\x00", "")).strip()
        if normalized:
            segments.append(normalized)
    return "\n\n".join(segments)
