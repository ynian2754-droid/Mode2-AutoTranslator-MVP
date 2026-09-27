"""Source segmentation, kept independent from translation scheduling."""

from __future__ import annotations

import re
from typing import Any

import mode2_common

from .chunk_optimizer import optimize_sentence_chunks
from .document_model import empty_document
from .sentence_segmentation import sentence_spans, split_sentences
from .text_reconstruction import PdfTextReconstruction, is_likely_pdf_heading
from .utils import now_iso, sha256_text


# This is a translation-unit policy, not the scheduler's wave-size setting.
# It is a preferred target length: complete sentence boundaries take priority.
DEFAULT_TARGET_WORDS = 500
MAX_TARGET_WORDS = 100_000
# Deprecated compatibility aliases for existing callers and persisted v1
# configuration.  New code should use the TARGET names above.
DEFAULT_MAX_WORDS = DEFAULT_TARGET_WORDS
MAX_MAX_WORDS = MAX_TARGET_WORDS
_WORD_SPAN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_UNTERMINATED_FALLBACK_TARGET_FACTOR = 1.5

# Words that signal a title line has been visually wrapped and the next line
# is likely its continuation. Kept narrow on purpose: a wrong accept would
# merge two unrelated short headings.
_TITLE_CONTINUATION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "over",
        "the",
        "their",
        "to",
        "under",
        "upon",
        "via",
        "with",
    }
)
# A TOC page label must be a standalone token: a digit run or a roman
# numeral preceded by the start of the line or whitespace. Without that word
# boundary, the trailing roman-letters of an ordinary word were treated as a
# page number (``ECONOMIC`` -> ``MIC``, ``Appendix`` -> ``dix``).
_TOC_PAGE_LABEL_RE = re.compile(
    r"(?:^|\s)(?:\d{1,4}|[IVXLCDM]{1,8})\s*$", re.IGNORECASE
)
_INDEPENDENT_TOC_ENTRY_RE = re.compile(
    r"^(?:\d+\s+\S|introduction\b|overview\b|preface\b|appendix\b|"
    r"notes\b|references\b|name\s+index\b|subject\s+index\b|"
    r"contents\b|the\b)",
    re.IGNORECASE,
)


def _last_word(value: str) -> str:
    words = _WORD_SPAN_RE.findall(value or "")
    return words[-1].lower() if words else ""


def validate_target_words(value: Any) -> int:
    """Validate the user-facing preferred English-word target."""
    if isinstance(value, bool):
        raise ValueError("目标切分词数必须是大于 0 的整数。")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError("目标切分词数必须是大于 0 的整数。")
    if result <= 0:
        raise ValueError("目标切分词数必须是大于 0 的整数。")
    if result > MAX_TARGET_WORDS:
        raise ValueError(f"目标切分词数不能超过 {MAX_TARGET_WORDS}。")
    return result


def validate_max_words(value: Any) -> int:
    """Deprecated compatibility alias for :func:`validate_target_words`."""
    return validate_target_words(value)


def new_unit_record(
    *,
    unit_id: str,
    order: int,
    node_id: str,
    chunk_index: int,
    chunk_count: int,
    source_range: dict[str, Any],
    source: str,
    source_words: int,
    demo_mode: bool,
    now: str,
    page_numbers: list[int] | None = None,
) -> dict[str, Any]:
    """The initial record every segmentation path writes for one new Unit.

    Only the caller-specific pieces are parameters: the source range unit
    (``normalized_chars`` vs ``reconstructed_chars``), the optional PDF page
    anchors and the word count. Field set, defaults and key order live here so
    the two paths cannot drift apart.
    """

    return {
        "id": unit_id,
        "order": order,
        "node_id": node_id,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "source_range": source_range,
        **({"source_page_numbers": list(page_numbers)} if page_numbers is not None else {}),
        "source": source,
        "source_sha256": sha256_text(source),
        "source_words": source_words,
        "status": "pending",
        "translation": "",
        "translation_attempts": 0,
        "review_attempts": 0,
        "translation_revision": 0,
        "review_suggestions": [],
        "pending_translation_feedback": None,
        "review": None,
        "review_issues": [],
        "user_decision": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        # Only the local demo fixture intentionally creates one failure.
        "demo_force_review": bool(demo_mode and order == 4),
    }


class MarkdownSegmenter:
    """Split source text into stable, ordered units for the pipeline."""

    def split_blocks_with_separators(self, value: str) -> list[tuple[str, str]]:
        """Return blocks together with the literal blank-line separator before each block."""
        lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        records: list[tuple[str, str]] = []
        current: list[str] = []
        pending_separator = ""
        current_separator = ""
        fence: str | None = None
        for line in lines:
            stripped = line.strip()
            marker = re.match(r"^(```+|~~~+)", stripped)
            if marker:
                token = marker.group(1)[0]
                fence = None if fence == token else (token if fence is None else fence)
            if not stripped and fence is None:
                if current:
                    block = "\n".join(current).strip()
                    if block:
                        records.append((block, current_separator))
                    current = []
                    # The line ending after the block and the blank line's
                    # ending both belong to the separator between blocks.
                    pending_separator = "\n\n"
                elif records:
                    pending_separator += "\n"
                continue
            if not current:
                current_separator = pending_separator if records else ""
                pending_separator = ""
            current.append(line)
        if current:
            block = "\n".join(current).strip()
            if block:
                records.append((block, current_separator))
        return records

    def split_blocks(self, value: str) -> list[str]:
        """Split Markdown at blank lines without cutting fenced code blocks."""
        return [block for block, _separator in self.split_blocks_with_separators(value)]

    def split_for_units(self, value: str, *, max_words: int = DEFAULT_MAX_WORDS) -> list[str]:
        """Split paragraphs and safely break only paragraphs over the configured limit."""
        limit = validate_max_words(max_words)
        return [
            chunk
            for block in self.split_blocks(value)
            for chunk in self._split_oversized_block(block, limit)
        ]

    def _split_oversized_block(self, block: str, max_words: int) -> list[str]:
        target_words = validate_target_words(max_words)
        chunks = optimize_sentence_chunks(block, target_words=target_words)
        if not chunks:
            return [block]
        sentences = sentence_spans(block)
        if (
            len(chunks) == 1
            and chunks[0].word_count > target_words * _UNTERMINATED_FALLBACK_TARGET_FACTOR
            and len(sentences) == 1
            and not sentences[0].is_complete
        ):
            # A punctuation-free blob gives the optimizer no defensible
            # sentence boundary. Keep an explicit bounded fallback for API
            # safety, while normal complete sentences are never cut here.
            return self._split_by_words(block, target_words)
        return [chunk.text for chunk in chunks]

    @staticmethod
    def _classify_block(block: str, document_format: str) -> tuple[str, dict[str, Any]]:
        """Classify only structure that survives the current text importer."""
        lines = block.splitlines()
        first = lines[0].strip() if lines else ""
        attributes: dict[str, Any] = {}
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", first)
        if heading:
            attributes["level"] = len(heading.group(1))
            return "heading", attributes
        if document_format.casefold() == "pdf" and MarkdownSegmenter._is_pdf_heading(lines):
            attributes["level"] = 1
            attributes["detected_from"] = "pdf_text_shape"
            return "heading", attributes
        if re.match(r"^(?:```+|~~~+)", first):
            attributes["fence"] = first[:3]
            return "code", attributes
        list_lines = [line.strip() for line in lines if line.strip()]
        if list_lines and all(re.match(r"^(?:[-+*]|\d+[.)])\s+", line) for line in list_lines):
            attributes["ordered"] = bool(re.match(r"^\d+[.)]\s+", list_lines[0]))
            return "list", attributes
        if len(lines) >= 2 and all("|" in line for line in lines):
            if any(re.match(r"^\s*\|?\s*:?-{3,}", line) for line in lines[1:]):
                return "table", attributes
        page_marker = re.match(r"^>\s*\[PDF Page\s+(\d+)\]\s*$", first, re.IGNORECASE)
        if page_marker:
            attributes["page_number"] = int(page_marker.group(1))
            attributes["page_marker"] = first
            attributes["page_break_before"] = bool(document_format == "pdf")
            return "paragraph", attributes
        if first.startswith(">"):
            return "blockquote", attributes
        return "paragraph", attributes

    @staticmethod
    def _is_pdf_heading(lines: list[str]) -> bool:
        nonempty = [line.strip() for line in lines if line.strip()]
        if not 1 <= len(nonempty) <= 4:
            return False
        text = " ".join(nonempty)
        words = re.findall(r"[A-Za-z]+", text)
        if not words or len(words) > 20:
            return False
        return is_likely_pdf_heading(text)

    @staticmethod
    def _sentence_parts(value: str) -> list[str]:
        return split_sentences(value) or [value]

    @staticmethod
    def _split_by_words(value: str, max_words: int) -> list[str]:
        spans = list(_WORD_SPAN_RE.finditer(value))
        if not spans:
            return [value]
        chunks: list[str] = []
        start = 0
        for index in range(max_words - 1, len(spans), max_words):
            end = spans[index].end()
            chunks.append(value[start:end])
            start = end
        if start < len(value):
            chunks.append(value[start:])
        return chunks

    def segment(
        self,
        source_text: str,
        *,
        demo_mode: bool = False,
        max_words: int = DEFAULT_MAX_WORDS,
    ) -> list[dict[str, Any]]:
        units, _document = self.segment_document(
            source_text,
            demo_mode=demo_mode,
            max_words=max_words,
        )
        return units

    def segment_document(
        self,
        source_text: str,
        *,
        demo_mode: bool = False,
        max_words: int = DEFAULT_MAX_WORDS,
        document_format: str = "markdown",
        source_name: str | None = None,
        structure_blocks: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        group_adjacent_paragraphs: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Segment source text and retain enough structure for later assembly."""
        preserve_text_source = (
            bool(group_adjacent_paragraphs)
            and (document_format or "markdown").casefold() in {"text", "markdown"}
            and not structure_blocks
        )
        normalized_source = (
            source_text
            if preserve_text_source
            else source_text.replace("\r\n", "\n").replace("\r", "\n")
        )
        if structure_blocks:
            records = self._epub_segmentation_records(normalized_source, structure_blocks, max_words)
        else:
            records = self._text_segmentation_records(
                normalized_source,
                document_format or "markdown",
                max_words,
                group_adjacent_paragraphs=group_adjacent_paragraphs,
            )
        if not records:
            raise ValueError("源文不能为空，至少需要一个非空段落。")
        units: list[dict[str, Any]] = []
        parts: list[dict[str, str]] = []
        nodes: list[dict[str, Any]] = []
        reading_order: list[str] = []
        index = 0
        for block_index, record in enumerate(records):
            block = record["source"]
            separator_before = record["separator_before"]
            if separator_before and (
                block_index or record.get("preserve_leading_separator", False)
            ):
                parts.append({"type": "literal", "text": separator_before})
            node_id = f"node-{block_index + 1:03d}"
            source_start = record["source_start"]
            source_end = record["source_end"]
            node_type = record["node_type"]
            attributes = dict(record["attributes"])
            node = {
                "id": node_id,
                "order": block_index + 1,
                "type": node_type,
                "source": block,
                "source_sha256": sha256_text(block),
                "source_range": {
                    "start": source_start,
                    "end": source_end,
                    "unit": "normalized_chars",
                },
                "separator_before": separator_before,
                "unit_ids": [],
                "attributes": attributes,
            }
            nodes.append(node)
            reading_order.append(node_id)
            chunks = record.get("chunks")
            if chunks is None:
                chunks = (
                    self._split_oversized_block(block, validate_max_words(max_words))
                    if block
                    else []
                )
            if "".join(chunks) != block:
                raise ValueError("切分范围必须按原文顺序无损覆盖。")
            chunk_cursor = 0
            for chunk_index, chunk in enumerate(chunks):
                index += 1
                unit_id = f"unit-{index:03d}"
                parts.append({"type": "unit", "unit_id": unit_id})
                chunk_start = block.find(chunk, chunk_cursor)
                if chunk_start < 0:
                    chunk_start = chunk_cursor
                chunk_end = chunk_start + len(chunk)
                chunk_cursor = chunk_end
                node["unit_ids"].append(unit_id)
                units.append(
                    new_unit_record(
                        unit_id=unit_id,
                        order=index,
                        node_id=node_id,
                        chunk_index=chunk_index,
                        chunk_count=len(chunks),
                        source_range={
                            "start": source_start + chunk_start,
                            "end": source_start + chunk_end,
                            "unit": "normalized_chars",
                        },
                        source=chunk,
                        source_words=mode2_common.english_word_count(chunk),
                        demo_mode=demo_mode,
                        now=now_iso(),
                    )
                )
            if record.get("separator_after"):
                parts.append({"type": "literal", "text": record["separator_after"]})
        document = empty_document(
            document_format=document_format or "markdown",
            source_name=source_name,
            source_sha256=sha256_text(source_text),
        )
        document.update(
            {
                "unit_count": len(units),
                "parts": parts,
                "nodes": nodes,
                "reading_order": reading_order,
                "segmentation": {
                    "strategy": "sentence_target_optimizer",
                    "target_words": validate_target_words(max_words),
                    "version": 1,
                },
            }
        )
        return units, document

    def _text_segmentation_records(
        self,
        source_text: str,
        document_format: str,
        target_words: int,
        *,
        group_adjacent_paragraphs: bool,
    ) -> list[dict[str, Any]]:
        preserve_source = (
            group_adjacent_paragraphs
            and document_format.casefold() in {"text", "markdown"}
        )
        if preserve_source:
            raw_source_records = self._split_blocks_with_source_ranges(source_text)
            split_records = [
                (
                    record["source"],
                    record["separator_before"],
                    record["source_start"],
                    record["source_end"],
                    record.get("separator_after", ""),
                )
                for record in raw_source_records
            ]
        else:
            split_records = [
                (block, separator, None, None, "")
                for block, separator in self.split_blocks_with_separators(source_text)
            ]
        if not split_records:
            return []
        raw_records: list[dict[str, Any]] = []
        source_cursor = 0
        previous_end = 0
        for block_index, (block, split_separator, exact_start, exact_end, separator_after) in enumerate(split_records):
            if exact_start is None:
                source_start = source_text.find(block, source_cursor)
                if source_start < 0:
                    source_start = source_cursor
                source_end = source_start + len(block)
            else:
                source_start = exact_start
                source_end = exact_end
            source_cursor = source_end
            separator_before = (
                split_separator if preserve_source else source_text[previous_end:source_start]
            )
            if not raw_records and split_separator:
                separator_before = split_separator
            node_type, attributes = self._classify_block(block, document_format)
            if (
                group_adjacent_paragraphs
                and document_format.casefold() == "markdown"
                and node_type == "list"
            ):
                item_ranges = self._markdown_list_item_ranges(block)
                if len(item_ranges) > 1:
                    for item_index, (relative_start, relative_end) in enumerate(item_ranges):
                        item_start = source_start + relative_start
                        item_end = source_start + relative_end
                        item_separator = (
                            separator_before
                            if item_index == 0
                            else source_text[raw_records[-1]["source_end"]:item_start]
                        )
                        raw_records.append(
                            {
                                "source": source_text[item_start:item_end],
                                "source_start": item_start,
                                "source_end": item_end,
                                "separator_before": item_separator,
                                "node_type": node_type,
                                "attributes": dict(attributes),
                                "kind": "markdown_list_item",
                                "group_id": f"markdown-list-{attributes.get('ordered', False)}",
                                "block_start": block_index,
                                "block_end": block_index,
                                "item_index": item_index,
                                "separator_after": (
                                    separator_after if item_index == len(item_ranges) - 1 else ""
                                ),
                                "preserve_leading_separator": preserve_source and block_index == 0,
                            }
                        )
                else:
                    raw_records.append(
                        self._plain_record(
                            block, source_start, source_end, separator_before, node_type,
                            attributes, block_index, "markdown_list_item",
                            f"markdown-list-{attributes.get('ordered', False)}",
                        )
                    )
                    raw_records[-1]["separator_after"] = separator_after
                    raw_records[-1]["preserve_leading_separator"] = preserve_source and block_index == 0
            else:
                raw_records.append(
                    self._plain_record(
                        block,
                        source_start,
                        source_end,
                        separator_before,
                        node_type,
                        attributes,
                        block_index,
                        "paragraph" if node_type == "paragraph" else node_type,
                        f"text-block-{block_index}",
                    )
                )
                raw_records[-1]["separator_after"] = separator_after
                raw_records[-1]["preserve_leading_separator"] = preserve_source and block_index == 0
            previous_end = source_end
        if document_format.casefold() not in {"text", "markdown"}:
            return [
                {
                    key: record[key]
                    for key in (
                        "source", "source_start", "source_end", "separator_before",
                        "node_type", "attributes",
                    )
                }
                for record in raw_records
            ]
        if not group_adjacent_paragraphs:
            return [
                {
                    key: record[key]
                    for key in (
                        "source", "source_start", "source_end", "separator_before",
                        "node_type", "attributes",
                    )
                }
                for record in raw_records
            ]
        return self._group_text_records(
            source_text, raw_records, document_format.casefold(), target_words
        )

    @staticmethod
    def _split_blocks_with_source_ranges(value: str) -> list[dict[str, Any]]:
        """Split blank-line blocks while retaining exact source offsets and line endings."""
        lines = value.splitlines(keepends=True)
        blocks: list[tuple[int, int]] = []
        fence: str | None = None
        current_start: int | None = None
        current_end: int | None = None
        cursor = 0

        for line in lines:
            content = line.rstrip("\r\n")
            stripped = content.strip()
            marker = re.match(r"^(```+|~~~+)", stripped)
            if marker:
                token = marker.group(1)[0]
                fence = None if fence == token else (token if fence is None else fence)

            if not stripped and fence is None:
                if current_start is not None and current_end is not None:
                    blocks.append((current_start, current_end))
                    current_start = None
                    current_end = None
                cursor += len(line)
                continue

            if current_start is None:
                current_start = cursor
            current_end = cursor + len(content)
            cursor += len(line)

        if current_start is not None and current_end is not None:
            blocks.append((current_start, current_end))

        result: list[dict[str, Any]] = []
        previous_end = 0
        for index, (start, end) in enumerate(blocks):
            result.append(
                {
                    "source": value[start:end],
                    "source_start": start,
                    "source_end": end,
                    "separator_before": value[previous_end:start],
                }
            )
            previous_end = end
        if result:
            result[-1]["separator_after"] = value[previous_end:]
        return result

    @staticmethod
    def _plain_record(
        source: str,
        source_start: int,
        source_end: int,
        separator_before: str,
        node_type: str,
        attributes: dict[str, Any],
        block_index: int,
        kind: str,
        group_id: str,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "source_start": source_start,
            "source_end": source_end,
            "separator_before": separator_before,
            "node_type": node_type,
            "attributes": dict(attributes),
            "kind": kind,
            "group_id": group_id,
            "block_start": block_index,
            "block_end": block_index,
        }

    @staticmethod
    def _plain_group_record(source_text: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        first, last = records[0], records[-1]
        source_start, source_end = first["source_start"], last["source_end"]
        attributes = dict(first["attributes"])
        attributes["source_block_range"] = {
            "start": first["block_start"],
            "end": last["block_end"],
            "count": last["block_end"] - first["block_start"] + 1,
        }
        if "item_index" in first and "item_index" in last:
            attributes["source_item_range"] = {
                "start": first["item_index"],
                "end": last["item_index"],
                "count": last["item_index"] - first["item_index"] + 1,
            }
        return {
            "source": source_text[source_start:source_end],
            "source_start": source_start,
            "source_end": source_end,
            "separator_before": first["separator_before"],
            "separator_after": last.get("separator_after", ""),
            "preserve_leading_separator": bool(
                first.get("preserve_leading_separator", False)
            ),
            "node_type": first["node_type"],
            "attributes": attributes,
        }

    def _group_text_records(
        self,
        source_text: str,
        records: list[dict[str, Any]],
        document_format: str,
        target_words: int,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        index = 0
        while index < len(records):
            first = records[index]
            end = index + 1
            if first["kind"] == "paragraph":
                while end < len(records) and records[end]["kind"] == "paragraph":
                    end += 1
            elif (
                document_format == "markdown"
                and first["kind"] == "markdown_list_item"
            ):
                while (
                    end < len(records)
                    and records[end]["kind"] == "markdown_list_item"
                    and records[end]["group_id"] == first["group_id"]
                ):
                    end += 1
                result.extend(self._pack_atomic_records(source_text, records[index:end], target_words))
                index = end
                continue
            result.append(self._plain_group_record(source_text, records[index:end]))
            index = end
        return result

    @staticmethod
    def _markdown_list_item_ranges(block: str) -> list[tuple[int, int]]:
        lines = block.splitlines(keepends=True)
        offsets: list[tuple[int, int]] = []
        cursor = 0
        first_indent: int | None = None
        for line in lines:
            marker = re.match(r"^( *)(?:[-+*]|\d+[.)])\s+", line)
            if marker:
                indent = len(marker.group(1))
                if first_indent is None:
                    first_indent = indent
                if indent == first_indent:
                    offsets.append((cursor, cursor + len(line)))
                elif offsets:
                    # Nested items remain part of their parent item so the
                    # document manifest does not invent a cross-list merge.
                    previous_start, _previous_end = offsets[-1]
                    offsets[-1] = (previous_start, cursor + len(line))
            elif offsets:
                previous_start, _previous_end = offsets[-1]
                offsets[-1] = (previous_start, cursor + len(line))
            cursor += len(line)
        if len(offsets) < 2:
            return [(0, len(block))]
        return [(start, end) for start, end in offsets]

    def _epub_segmentation_records(
        self,
        source_text: str,
        structure_blocks: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        target_words: int,
    ) -> list[dict[str, Any]]:
        raw_records: list[dict[str, Any]] = []
        previous_end = 0
        for fallback_order, block in enumerate(structure_blocks):
            if not isinstance(block, dict):
                raise ValueError("EPUB 结构块格式无效。")
            try:
                source_start = int(block["start"])
                source_end = int(block["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("EPUB 结构块缺少有效文本范围。") from exc
            block_text = str(block.get("text") or "")
            if source_start < previous_end or source_end < source_start or source_end > len(source_text):
                raise ValueError("EPUB 结构块范围不符合源文顺序。")
            if source_text[source_start:source_end] != block_text:
                raise ValueError("EPUB 结构块文本与规范化源文不一致。")
            kind = str(block.get("kind") or "paragraph")
            if kind in {"image", "raw"} and source_start == source_end and not block_text:
                # Keep the boundary in ImportedSource.structure_blocks, but do
                # not manufacture an empty translation node without a unit.
                previous_end = source_end
                continue
            node_type = {
                "heading": "heading",
                "paragraph": "paragraph",
                "footnote": "paragraph",
                "toc_entry": "paragraph",
                "list_item": "list_item",
                "table": "table",
                "code": "code",
                "blockquote": "blockquote",
                "image": "image",
                "raw": "raw",
            }.get(kind, "paragraph")
            attributes = dict(block.get("attributes") or {})
            attributes.update(
                {
                    "structure_type": kind,
                    "chapter_file": str(block.get("chapter_file") or ""),
                    "structure_id": str(block.get("group_id") or ""),
                    "source_block_range": {
                        "start": int(block.get("block_order", fallback_order)),
                        "end": int(block.get("block_order", fallback_order)),
                        "count": 1,
                    },
                }
            )
            if block.get("heading_level") is not None:
                attributes["level"] = int(block["heading_level"])
            raw_records.append(
                {
                    "source": block_text,
                    "source_start": source_start,
                    "source_end": source_end,
                    "separator_before": source_text[previous_end:source_start],
                    "node_type": node_type,
                    "attributes": attributes,
                    "kind": kind,
                    "group_id": str(block.get("group_id") or ""),
                    "chapter_file": str(block.get("chapter_file") or ""),
                    "container_id": str(block.get("container_id") or ""),
                    "block_start": int(block.get("block_order", fallback_order)),
                    "block_end": int(block.get("block_order", fallback_order)),
                }
            )
            previous_end = source_end

        result: list[dict[str, Any]] = []
        index = 0
        atomic_kinds = {"footnote", "toc_entry", "list_item"}
        while index < len(raw_records):
            first = raw_records[index]
            end = index + 1
            if first["kind"] == "paragraph" or first["kind"] in atomic_kinds:
                while end < len(raw_records):
                    candidate = raw_records[end]
                    if (
                        candidate["kind"] != first["kind"]
                        or candidate["group_id"] != first["group_id"]
                        or candidate["chapter_file"] != first["chapter_file"]
                        or candidate["block_start"] != raw_records[end - 1]["block_end"] + 1
                    ):
                        break
                    end += 1
            group = raw_records[index:end]
            if first["kind"] in atomic_kinds:
                result.extend(self._pack_atomic_records(source_text, group, target_words))
            else:
                result.append(self._plain_group_record(source_text, group))
            index = end
        return result

    def _pack_atomic_records(
        self,
        source_text: str,
        records: list[dict[str, Any]],
        target_words: int,
    ) -> list[dict[str, Any]]:
        target = validate_target_words(target_words)
        packed: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        current_words = 0

        def emit(items: list[dict[str, Any]], chunks: list[str] | None = None) -> None:
            if not items:
                return
            spec = self._plain_group_record(source_text, items)
            spec["attributes"]["item_count"] = len(items)
            # Packed atomic records are already split at item boundaries.
            # Keep that complete range as one unit unless a single item is
            # itself oversized and explicitly supplied sentence chunks.
            spec["chunks"] = chunks if chunks is not None else [spec["source"]]
            packed.append(spec)

        for record in records:
            item_words = mode2_common.english_word_count(record["source"])
            if item_words > target:
                emit(current)
                current = []
                current_words = 0
                if record["source"]:
                    chunks = [
                        chunk.text
                        for chunk in optimize_sentence_chunks(
                            record["source"], target_words=target
                        )
                    ] or [record["source"]]
                    if "".join(chunks) != record["source"]:
                        raise ValueError("超长结构条目切分必须无损。")
                    emit([record], chunks)
                else:
                    emit([record], [])
                continue
            if current and current_words + item_words > target:
                emit(current)
                current = []
                current_words = 0
            if current and abs(target - current_words) <= abs(target - (current_words + item_words)):
                emit(current)
                current = []
                current_words = 0
            current.append(record)
            current_words += item_words
        emit(current)
        return packed

    def segment_pdf_reconstruction(
        self,
        reconstruction: PdfTextReconstruction,
        *,
        demo_mode: bool = False,
        target_words: int = DEFAULT_TARGET_WORDS,
        source_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Create PDF units from reconstructed text at optimized sentence boundaries.

        Consecutive ordinary paragraphs are held in one logical document node.
        This is the smallest compatible way to let a unit span a natural
        paragraph boundary while retaining the existing one-node-to-many-units
        assembly contract. Headings and other structural blocks remain their
        own nodes.
        """

        target_words = validate_target_words(target_words)
        source_text = reconstruction.text
        records = self.split_blocks_with_separators(source_text)
        records = self._coalesce_pdf_title_records(source_text, records, reconstruction)
        if not records:
            raise ValueError("PDF 重建后没有可翻译文字。")

        blocks: list[dict[str, Any]] = []
        source_cursor = 0
        for block_index, (block, separator_before, normalized) in enumerate(records):
            source_start = source_text.find(block, source_cursor)
            if source_start < 0:
                source_start = source_cursor
            source_end = source_start + len(block)
            source_cursor = source_end
            node_type, attributes = self._classify_block(block, "pdf")
            page_records = self._pages_for_range(reconstruction, source_start, source_end)
            page_role = page_records[0].layout_role if page_records else ""
            if page_role:
                attributes["layout_role"] = page_role
            if page_role == "contents_page" and block.strip().casefold() != "contents":
                # Contents rows are kept as independent paragraph nodes.  A
                # row that visually wraps still remains one source block, so
                # the existing sentence/unit optimizer is untouched.
                node_type = "paragraph"
                attributes["contents_entry"] = True
                attributes["toc_level"] = self._toc_level(block)
                page_label = self._toc_page_label(block)
                if page_label:
                    attributes["toc_page_label"] = page_label
            if (
                page_records
                and page_role in {"title_page", "copyright_page", "contents_page", "preface_page"}
                and source_start > 0
                and source_start == page_records[0].text_start
            ):
                attributes["page_break_before"] = True
            # ``text`` is what becomes the unit's source when this block
            # stands alone; coalesced title fragments also keep the literal
            # slice as ``text_literal`` so paragraph grouping preserves
            # blank lines.
            blocks.append(
                {
                    "order": block_index + 1,
                    "text": block,
                    "text_literal": normalized or block,
                    "separator_before": separator_before,
                    "start": source_start,
                    "end": source_end,
                    "type": node_type,
                    "attributes": attributes,
                }
            )

        units: list[dict[str, Any]] = []
        parts: list[dict[str, str]] = []
        nodes: list[dict[str, Any]] = []
        reading_order: list[str] = []
        node_index = 0
        unit_index = 0
        fallback_node_count = 0

        def append_node(entries: list[dict[str, Any]], node_type: str, base_attributes: dict[str, Any]) -> None:
            nonlocal node_index, unit_index, fallback_node_count
            node_index += 1
            node_id = f"node-{node_index:03d}"
            source_start = int(entries[0]["start"])
            source_end = int(entries[-1]["end"])
            # When the coalescer normalized a wrapped title, ``text_literal``
            # differs from the literal slice; use the normalized form for
            # the unit source. For paragraph merges, ``text_literal`` is the
            # original block text (joined with separators preserved).
            literal_text = source_text[source_start:source_end]
            if len(entries) == 1:
                node_source = entries[0].get("text_literal") or literal_text
            else:
                node_source = literal_text
            separator_before = str(entries[0]["separator_before"])
            attributes = dict(base_attributes)
            attributes["source_page_numbers"] = self._page_numbers_for_range(
                reconstruction,
                source_start,
                source_end,
            )
            attributes["source_block_ranges"] = [
                {
                    "order": int(entry["order"]),
                    "start": int(entry["start"]),
                    "end": int(entry["end"]),
                    "type": str(entry["type"]),
                    "source_page_numbers": self._page_numbers_for_range(
                        reconstruction,
                        int(entry["start"]),
                        int(entry["end"]),
                    ),
                }
                for entry in entries
            ]
            node: dict[str, Any] = {
                "id": node_id,
                "order": node_index,
                "type": node_type,
                "source": node_source,
                "source_sha256": sha256_text(node_source),
                "source_range": {
                    "start": source_start,
                    "end": source_end,
                    "unit": "reconstructed_chars",
                },
                "separator_before": separator_before,
                "unit_ids": [],
                "attributes": attributes,
            }
            nodes.append(node)
            reading_order.append(node_id)
            if separator_before:
                parts.append({"type": "literal", "text": separator_before})

            chunks = optimize_sentence_chunks(node_source, target_words=target_words)
            chunk_slices = [(chunk.start, chunk.end, chunk.word_count) for chunk in chunks]
            sentences = sentence_spans(node_source)
            if (
                len(chunks) == 1
                and chunks[0].word_count > target_words * _UNTERMINATED_FALLBACK_TARGET_FACTOR
                and len(sentences) == 1
                and not sentences[0].is_complete
            ):
                chunk_slices = self._forced_word_chunk_slices(node_source, target_words)
                attributes["chunking_fallback"] = "unterminated_oversized_text"
                fallback_node_count += 1
            for chunk_index, (chunk_start, chunk_end, chunk_word_count) in enumerate(chunk_slices):
                unit_index += 1
                unit_id = f"unit-{unit_index:03d}"
                unit_start = source_start + chunk_start
                unit_end = source_start + chunk_end
                chunk_source = node_source[chunk_start:chunk_end]
                parts.append({"type": "unit", "unit_id": unit_id})
                node["unit_ids"].append(unit_id)
                units.append(
                    new_unit_record(
                        unit_id=unit_id,
                        order=unit_index,
                        node_id=node_id,
                        chunk_index=chunk_index,
                        chunk_count=len(chunks),
                        source_range={
                            "start": unit_start,
                            "end": unit_end,
                            "unit": "reconstructed_chars",
                        },
                        source=chunk_source,
                        source_words=chunk_word_count,
                        demo_mode=demo_mode,
                        now=now_iso(),
                        page_numbers=self._page_numbers_for_range(
                            reconstruction,
                            unit_start,
                            unit_end,
                        ),
                    )
                )

        pending_paragraphs: list[dict[str, Any]] = []
        for block in blocks:
            if (
                block["type"] == "paragraph"
                and not block["attributes"].get("contents_entry")
                and not block["attributes"].get("layout_role")
            ):
                pending_paragraphs.append(block)
                continue
            if pending_paragraphs:
                append_node(pending_paragraphs, "paragraph", {})
                pending_paragraphs = []
            append_node([block], str(block["type"]), dict(block["attributes"]))
        if pending_paragraphs:
            append_node(pending_paragraphs, "paragraph", {})

        document = empty_document(
            document_format="pdf",
            source_name=source_name,
            source_sha256=sha256_text(source_text),
        )
        document.update(
            {
                "unit_count": len(units),
                "parts": parts,
                "nodes": nodes,
                "reading_order": reading_order,
                "segmentation": {
                    "strategy": "sentence_target_optimizer",
                    "target_words": target_words,
                    "version": 1,
                    "emergency_word_fallback_node_count": fallback_node_count,
                },
                "source_pages": [
                    {
                        "page_number": page.page_number,
                        "text_range": {
                            "start": page.text_start,
                            "end": page.text_end,
                            "unit": "reconstructed_chars",
                        },
                        "joins_previous": page.joins_previous,
                        "layout_role": page.layout_role,
                    }
                    for page in reconstruction.pages
                ],
                "reconstruction": {
                    "version": 1,
                    "removed_layout_line_count": len(reconstruction.removed_lines),
                },
            }
        )
        return units, document

    @staticmethod
    def _pages_for_range(
        reconstruction: PdfTextReconstruction,
        start: int,
        end: int,
    ) -> list[Any]:
        return [
            page
            for page in reconstruction.pages
            if page.text_end > start and page.text_start < end
        ]

    @classmethod
    def _coalesce_pdf_title_records(
        cls,
        source_text: str,
        records: list[tuple[str, str]],
        reconstruction: PdfTextReconstruction,
    ) -> list[tuple[str, str]]:
        """Join visual title lines on a high-confidence title page only.

        Also joins PDF title fragments that wrap across a blank line on the
        same page: an incomplete TOC entry on a contents page and a chapter
        title whose first line ends with a continuation word. Body sentences
        ending with terminal punctuation are never coalesced.
        """

        result: list[tuple[str, str]] = []
        cursor = 0
        index = 0
        while index < len(records):
            block, separator_before = records[index]
            start = source_text.find(block, cursor)
            if start < 0:
                start = cursor
            end = start + len(block)
            pages = cls._pages_for_range(reconstruction, start, end)
            join_title_page = bool(
                pages
                and pages[0].layout_role == "title_page"
                and cls._is_title_line(block)
            )
            join_wrapped_fragment = cls._looks_like_wrapped_title_pair(
                block,
                records[index + 1] if index + 1 < len(records) else None,
                source_text,
                end,
                reconstruction,
            )
            if not (join_title_page or join_wrapped_fragment):
                result.append((block, separator_before, ""))
                cursor = end
                index += 1
                continue

            group_end = end
            last_index = index
            while True:
                candidate_index = last_index + 1
                if candidate_index >= len(records):
                    break
                candidate_block, _candidate_separator = records[candidate_index]
                candidate_start = source_text.find(candidate_block, group_end)
                if candidate_start < 0:
                    break
                candidate_end = candidate_start + len(candidate_block)
                previous_block = records[last_index][0]
                if join_title_page:
                    candidate_pages = cls._pages_for_range(
                        reconstruction, candidate_start, candidate_end
                    )
                    if (
                        not candidate_pages
                        or candidate_pages[0].layout_role != "title_page"
                        or not cls._is_title_line(candidate_block)
                    ):
                        break
                else:
                    # Wrapped fragment chain: include the candidate when the
                    # (last_index, candidate_index) pair looks like one
                    # wrapped title. The pair (index, index+1) has already
                    # been validated by the outer check, so this check is
                    # only meaningful for further chain extensions.
                    if not cls._looks_like_wrapped_title_pair(
                        previous_block,
                        records[candidate_index],
                        source_text,
                        group_end,
                        reconstruction,
                    ):
                        break
                group_end = candidate_end
                last_index = candidate_index
            literal_block = source_text[start:group_end].strip()
            # Title-page coalesce preserves the original blank-line layout
            # so existing readers and tests keep their expectations.
            # Wrapped fragment coalesce (TQ-014/TQ-015) replaces the
            # ``\\n\\n`` between fragments with a single space so the unit
            # text reads as one continuous title.
            normalized_block = (
                re.sub(r"\s+", " ", literal_block).strip()
                if not join_title_page
                else ""
            )
            result.append((literal_block, separator_before, normalized_block))
            cursor = group_end
            index = last_index + 1
        return result

    @classmethod
    def _looks_like_wrapped_title_pair(
        cls,
        previous_block: str,
        next_record: tuple[str, str] | None,
        source_text: str,
        previous_end: int,
        reconstruction: PdfTextReconstruction,
    ) -> bool:
        """Whether the previous block and the next record look like one wrapped title."""

        if not next_record:
            return False
        next_block = next_record[0]
        previous_pages = cls._pages_for_range(reconstruction, max(previous_end - 1, 0), previous_end)
        next_start = source_text.find(next_block, previous_end)
        if next_start < 0:
            return False
        next_end = next_start + len(next_block)
        next_pages = cls._pages_for_range(reconstruction, next_start, next_end)
        if not previous_pages or not next_pages:
            return False
        # Both fragments must live on the same physical page. Cross-page
        # coalescing is deliberately out of scope for this fix.
        if previous_pages[0].page_number != next_pages[0].page_number:
            return False
        previous_role = previous_pages[0].layout_role
        next_role = next_pages[0].layout_role
        if previous_role == "contents_page" and next_role == "contents_page":
            # Incomplete TOC entry (without a page label) followed by a
            # title-like fragment that ends with a page number. The header
            # line "CONTENTS" is never a continuation of a TOC entry.
            joined_so_far = previous_block.strip()
            if joined_so_far.casefold() == "contents":
                return False
            if _TOC_PAGE_LABEL_RE.search(joined_so_far):
                return False
            # A numbered entry or a well-known top-level TOC heading starts a
            # new record even when its own page label is on the same visual
            # line.  Without this guard, ``1 THEORY OF`` + ``2 PRODUCTION``
            # and ``THEORY OF`` + ``Introduction`` were swallowed together.
            if cls._starts_independent_toc_entry(next_block):
                return False
            if not _TOC_PAGE_LABEL_RE.search(next_block):
                return False
            # The next fragment must be a page-number-only continuation of the
            # previous entry (e.g. "5" or "..... 12"), not a brand-new
            # independent TOC entry that already carries its own heading word
            # (e.g. "Introduction ......... 5"). Stripping the trailing page
            # label must leave only leader dots or whitespace.
            label = cls._toc_page_label(next_block)
            if label:
                prefix = next_block[: next_block.rfind(label)].strip()
                prefix_words = _WORD_SPAN_RE.findall(prefix)
                if any(word.isalpha() for word in prefix_words):
                    # next carries its own heading word. Only merge when the
                    # previous fragment ended with a continuation word, so a
                    # wrapped title line ("... MEANS OF" + "PRODUCTION 99")
                    # still joins but an independent entry ("Appendix" +
                    # "Introduction ......... 5") does not.
                    if _last_word(joined_so_far) not in _TITLE_CONTINUATION_WORDS:
                        return False
            # ``_is_title_line`` rejects TOC entry starts that begin with a
            # digit; we accept them only here, on a contents page.
            return True
        if not cls._is_title_line(previous_block) or not cls._is_title_line(next_block):
            return False
        # Otherwise: require the first fragment to end with a continuation
        # word so we don't merge two unrelated short headings.
        last_word = _last_word(previous_block)
        return last_word in _TITLE_CONTINUATION_WORDS

    @staticmethod
    def _is_title_line(value: str) -> bool:
        text = " ".join(value.split())
        return bool(text) and is_likely_pdf_heading(text) and not re.match(r"^\d+\s+", text)

    @staticmethod
    def _toc_page_label(value: str) -> str:
        match = re.search(r"(?:^|\s)(\d{1,4}|[IVXLCDM]{1,8})\s*$", value, re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _starts_independent_toc_entry(value: str) -> bool:
        return bool(_INDEPENDENT_TOC_ENTRY_RE.match(" ".join(value.split())))

    @classmethod
    def _toc_level(cls, value: str) -> int:
        text = value.strip()
        return 0 if re.match(r"^(?:\d+\s+[A-Z]|APPENDIX\b)", text) else 1

    @staticmethod
    def _page_numbers_for_range(
        reconstruction: PdfTextReconstruction,
        start: int,
        end: int,
    ) -> list[int]:
        return [
            page.page_number
            for page in reconstruction.pages
            if page.text_end > start and page.text_start < end
        ]

    @staticmethod
    def _forced_word_chunk_slices(value: str, target_words: int) -> list[tuple[int, int, int]]:
        """Last-resort slices for malformed, punctuation-free oversized text."""

        spans = list(_WORD_SPAN_RE.finditer(value))
        if not spans:
            return [(0, len(value), mode2_common.english_word_count(value))]
        chunks: list[tuple[int, int, int]] = []
        start = 0
        for word_index in range(target_words - 1, len(spans), target_words):
            end = spans[word_index].end()
            chunks.append((start, end, mode2_common.english_word_count(value[start:end])))
            start = end
        if start < len(value):
            chunks.append((start, len(value), mode2_common.english_word_count(value[start:])))
        return chunks


_DEFAULT_SEGMENTER = MarkdownSegmenter()


def markdown_blocks(value: str) -> list[str]:
    """Compatibility function for callers of the original MVP module."""
    return _DEFAULT_SEGMENTER.split_blocks(value)
