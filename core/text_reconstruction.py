"""Conservative, local reconstruction of PDF page text.

PDF extraction frequently exposes visual line wraps, repeated running headers,
page numbers, and page boundaries as plain text.  This module removes only
high-confidence layout noise and joins text without asking a model to infer
meaning.  It deliberately keeps page-level provenance for the later segment
and document-model stages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # Avoid a runtime import cycle with ``core.importers``.
    from core.importers import PdfPageSpan


_EDGE_LINE_COUNT = 4
_MIN_REPEATED_EDGE_PAGES = 3
_FRONT_MATTER_ROLES = frozenset(
    {"title_page", "copyright_page", "contents_page", "preface_page"}
)
_COMPOUND_HYPHEN_PREFIXES = frozenset(
    {
        "all",
        "anti",
        "co",
        "cross",
        "ex",
        "full",
        "half",
        "high",
        "ill",
        "long",
        "low",
        "mid",
        "multi",
        "non",
        "part",
        "post",
        "pre",
        "pro",
        "re",
        "self",
        "short",
        "state",
        "well",
        "world",
    }
)
_PAGE_NUMBER_RE = re.compile(r"^\(?\d{1,4}\)?$")
_ROMAN_PAGE_NUMBER_RE = re.compile(r"^[IVXLCDM]{1,8}$", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z]+")
_LIST_ITEM_RE = re.compile(r"^(?:[-*•]|(?:\d+|[A-Za-z])[.)])\s+")
# A named section word at the *start* of a line is not enough to prove that
# the line is a heading.  In particular, PDF prose commonly contains wrapped
# continuations such as ``This\nconclusion also follows ...``.  Treating that
# continuation as a heading inserts paragraph breaks and makes a later
# sentence-aware splitter cut the sentence in half.  This pattern is therefore
# deliberately limited to standalone section labels (with the common optional
# section number / appendix label forms).
_NAMED_HEADING_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*|[IVXLCDM]+)\s+)?"
    r"(?P<label>"
    r"appendix(?:\s+[A-Z0-9][A-Za-z0-9.-]*)?"
    r"|chapter(?:\s+\d+(?:\.\d+)*)?"
    r"|part(?:\s+[IVXLCDM]+|\s+\d+)?"
    r"|bibliography|contents|conclusions?|introduction|preface|references"
    r")\s*[:.]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RemovedPdfLayoutLine:
    """A page-edge line removed as high-confidence layout noise."""

    page_number: int
    text: str
    reason: str


@dataclass(frozen=True)
class ReconstructedPdfPage:
    """One physical PDF page after local cleanup and line reconstruction."""

    page_number: int
    text: str
    text_start: int
    text_end: int
    joins_previous: bool
    layout_role: str = ""


@dataclass(frozen=True)
class PdfTextReconstruction:
    """Reconstructed source text plus enough page anchors for later mapping."""

    text: str
    pages: tuple[ReconstructedPdfPage, ...]
    removed_lines: tuple[RemovedPdfLayoutLine, ...]


@dataclass
class _PageDraft:
    page_number: int
    text: str
    starts_heading: bool
    layout_role: str = ""
    joins_previous: bool = False
    separator_before: str = ""


def reconstruct_pdf_pages(pages: Sequence[PdfPageSpan]) -> PdfTextReconstruction:
    """Rebuild readable source text from ordered physical PDF pages.

    The routine is intentionally conservative:

    * only repeated *edge* lines on at least three pages are classified as a
      running header/footer;
    * the first line of each consecutive repeated run is retained, because it
      can be a real chapter title;
    * isolated page numbers are removed only at page edges;
    * page boundaries never become an automatic sentence boundary.

    It does not change the legacy marker-bearing import text.  Until the
    document-model integration stage consumes this object, callers can keep
    using their existing PDF import path unchanged.
    """

    ordered_pages = tuple(pages)
    if not ordered_pages:
        return PdfTextReconstruction(text="", pages=(), removed_lines=())

    edge_keys_by_page = tuple(_edge_keys(page.text) for page in ordered_pages)
    repeated_keys = _repeated_edge_keys(edge_keys_by_page)
    first_repeated_edge_pages = _first_repeated_edge_pages(
        edge_keys_by_page,
        repeated_keys,
    )
    removed_lines: list[RemovedPdfLayoutLine] = []
    drafts: list[_PageDraft] = []

    for page_index, page in enumerate(ordered_pages):
        retained_lines = _retained_page_lines(
            page=page,
            page_index=page_index,
            repeated_keys=repeated_keys,
            first_repeated_edge_pages=first_repeated_edge_pages,
            previous_page_continues=_previous_page_continues(
                ordered_pages,
                page_index,
                repeated_keys,
            ),
            removed_lines=removed_lines,
        )
        layout_role = _classify_layout_role(retained_lines)
        first_content_line = next((line for line in retained_lines if line), "")
        drafts.append(
            _PageDraft(
                page_number=page.page_number,
                text=_reconstruct_page_lines(
                    retained_lines,
                    contents_page=layout_role == "contents_page",
                ),
                starts_heading=_is_heading_like(first_content_line),
                layout_role=layout_role,
            )
        )

    previous_nonempty_index: int | None = None
    for draft_index, draft in enumerate(drafts):
        if not draft.text:
            continue
        if previous_nonempty_index is not None:
            previous = drafts[previous_nonempty_index]
            if previous.layout_role in _FRONT_MATTER_ROLES or draft.layout_role in _FRONT_MATTER_ROLES:
                # Front matter is laid out as separate physical pages.  Keep
                # that boundary even when the extracted last line is not a
                # sentence, while ordinary body pages retain cross-page joins.
                separator, remove_previous_hyphen = "\n\n", False
            else:
                separator, remove_previous_hyphen = _page_separator(
                    previous.text,
                    draft.text,
                    starts_heading=draft.starts_heading,
                )
            if remove_previous_hyphen:
                previous.text = previous.text[:-1]
            draft.separator_before = separator
            draft.joins_previous = separator != "\n\n"
        previous_nonempty_index = draft_index

    return _materialize_reconstruction(drafts, removed_lines)


def _edge_keys(page_text: str) -> set[str]:
    nonempty_lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    edge_lines = nonempty_lines[:_EDGE_LINE_COUNT] + nonempty_lines[-_EDGE_LINE_COUNT:]
    return {_line_key(line) for line in edge_lines if _line_key(line)}


def _repeated_edge_keys(edge_keys_by_page: Sequence[set[str]]) -> set[str]:
    occurrences: dict[str, int] = {}
    for keys in edge_keys_by_page:
        for key in keys:
            occurrences[key] = occurrences.get(key, 0) + 1
    return {
        key
        for key, count in occurrences.items()
        if count >= _MIN_REPEATED_EDGE_PAGES and _looks_like_running_text(key)
    }


def _first_repeated_edge_pages(
    edge_keys_by_page: Sequence[set[str]],
    repeated_keys: set[str],
) -> dict[str, int]:
    """Record the first edge-page occurrence for each verified running line.

    Books often alternate two running headers between recto and verso pages.
    Requiring a header to occur on the immediately previous page leaves every
    one of those alternating headers in the reconstructed prose.  Keep the
    earliest edge occurrence as a possible real title, then remove the later
    occurrences wherever they recur at a page edge.
    """

    first_pages: dict[str, int] = {}
    for page_index, keys in enumerate(edge_keys_by_page):
        for key in keys:
            if key in repeated_keys:
                first_pages.setdefault(key, page_index)
    return first_pages


def _retained_page_lines(
    *,
    page: PdfPageSpan,
    page_index: int,
    repeated_keys: set[str],
    first_repeated_edge_pages: dict[str, int],
    previous_page_continues: bool,
    removed_lines: list[RemovedPdfLayoutLine],
) -> list[str]:
    raw_lines = page.text.splitlines()
    nonempty_positions = [index for index, line in enumerate(raw_lines) if line.strip()]
    top_edge_positions = set(nonempty_positions[:_EDGE_LINE_COUNT])
    edge_positions = set(nonempty_positions[:_EDGE_LINE_COUNT] + nonempty_positions[-_EDGE_LINE_COUNT:])
    retained: list[str] = []

    for line_index, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        if not line:
            retained.append("")
            continue
        if line_index in edge_positions and _is_isolated_page_number(line):
            removed_lines.append(RemovedPdfLayoutLine(page.page_number, line, "page_number"))
            continue
        key = _line_key(line)
        first_page = first_repeated_edge_pages.get(key)
        is_later_repeated_edge = first_page is not None and first_page < page_index
        is_first_header_interrupting_continuation = (
            first_page == page_index
            and line_index in top_edge_positions
            and previous_page_continues
        )
        if (
            line_index in edge_positions
            and key in repeated_keys
            and (is_later_repeated_edge or is_first_header_interrupting_continuation)
        ):
            removed_lines.append(RemovedPdfLayoutLine(page.page_number, line, "repeated_edge"))
            continue
        retained.append(line)

    while retained and not retained[0]:
        retained.pop(0)
    while retained and not retained[-1]:
        retained.pop()
    return retained


def _previous_page_continues(
    pages: Sequence[PdfPageSpan],
    page_index: int,
    repeated_keys: set[str],
) -> bool:
    """Whether the preceding page ends in an unfinished content line.

    This is only used to reject the first occurrence of a verified running
    header when that header visibly interrupts a sentence across a page break.
    It deliberately ignores page numbers and already-verified running lines.
    """

    if page_index <= 0:
        return False
    for raw_line in reversed(pages[page_index - 1].text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        key = _line_key(line)
        if _is_isolated_page_number(line) or key in repeated_keys:
            continue
        return not _ends_sentence(line)
    return False


def _ends_sentence(value: str) -> bool:
    text = value.rstrip()
    while text and text[-1] in '"\'\u201d\u2019\xbb\uff09)]}':
        text = text[:-1].rstrip()
    return text.endswith((".", "?", "!", "\u2026"))


def _reconstruct_page_lines(
    lines: Sequence[str],
    *,
    contents_page: bool = False,
) -> str:
    if contents_page:
        return _reconstruct_contents_page(lines)
    result = ""
    previous_line = ""
    paragraph_break_pending = False

    for line in lines:
        if not line:
            if result:
                paragraph_break_pending = True
            continue
        if not result:
            result = line
        elif paragraph_break_pending or _needs_structural_break(previous_line, line):
            result = f"{result}\n\n{line}"
        else:
            result = _join_wrapped_lines(result, line)
        previous_line = line
        paragraph_break_pending = False
    return result.strip()


_TOC_PAGE_LABEL_RE = re.compile(r"(?P<label>\d{1,4}|[IVXLCDM]{1,8})$")
_TOC_ENTRY_START_RE = re.compile(
    r"^(?:\d+\s+[A-Z]|appendix\b|contents\b|introduction\b|preface\b|"
    r"overview\b|notes\b|references\b|name\s+index\b|subject\s+index\b)",
    re.IGNORECASE,
)


def _classify_layout_role(lines: Sequence[str]) -> str:
    """Identify only high-confidence front-matter layouts.

    The normal reconstruction path remains unchanged for body pages.  These
    roles provide presentation hints for title, copyright, contents, and
    preface pages without turning every physical PDF page into a hard break.
    """

    nonempty = [line.strip() for line in lines if line.strip()]
    if not nonempty:
        return ""
    text = " ".join(nonempty)
    folded = text.casefold()
    first = nonempty[0].casefold()
    if first == "contents":
        return "contents_page"
    if re.search(r"\b(first published|all rights reserved|isbn)\b", folded):
        return "copyright_page"
    if (
        len(text) <= 300
        and "london and new york" in folded
        and re.search(r"\bben\s+fine\b", folded)
    ):
        return "title_page"
    if (
        len(nonempty) >= 3
        and all(_looks_like_title_fragment(line) for line in nonempty[:2])
        and len(" ".join(nonempty[2:])) >= 200
    ):
        # Some publishers put the title above a short descriptive blurb.  It
        # is still a title page for output purposes, even though it is longer
        # than a conventional cover page.
        return "title_page"
    if first == "preface":
        return "preface_page"
    return ""


def _looks_like_title_fragment(value: str) -> bool:
    text = value.strip()
    return bool(
        text
        and is_likely_pdf_heading(text)
        and not _NAMED_HEADING_RE.match(text)
        and not re.match(r"^\d+\s+", text)
    )


def _reconstruct_contents_page(lines: Sequence[str]) -> str:
    """Keep table-of-contents entries separate while joining visual wraps."""

    entries: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            entries.append(" ".join(current).strip())
            current.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if _is_isolated_page_number(line):
            continue
        if current and _contents_line_starts_entry(line, " ".join(current)):
            flush()
        current.append(line)
    flush()
    return "\n\n".join(entry for entry in entries if entry)


def _contents_line_starts_entry(line: str, current: str) -> bool:
    if current.casefold() == "contents":
        return True
    if _TOC_PAGE_LABEL_RE.search(current):
        return True
    return bool(_TOC_ENTRY_START_RE.match(line.strip()))


def _needs_structural_break(previous_line: str, line: str) -> bool:
    return (
        _is_heading_like(previous_line)
        or _is_heading_like(line)
        or _LIST_ITEM_RE.match(previous_line) is not None
        or _LIST_ITEM_RE.match(line) is not None
    )


def _page_separator(previous_text: str, text: str, *, starts_heading: bool) -> tuple[str, bool]:
    if starts_heading:
        return "\n\n", False
    if _can_join_hyphenated(previous_text, text):
        return "", not _keep_hyphen(previous_text)
    return " ", False


def _join_wrapped_lines(previous_text: str, line: str) -> str:
    if _can_join_hyphenated(previous_text, line):
        if _keep_hyphen(previous_text):
            return f"{previous_text}{line.lstrip()}"
        return f"{previous_text[:-1]}{line.lstrip()}"
    return f"{previous_text.rstrip()} {line.lstrip()}"


def _can_join_hyphenated(previous_text: str, next_text: str) -> bool:
    return previous_text.rstrip().endswith("-") and bool(re.match(r"^[a-z]", next_text.lstrip()))


def _keep_hyphen(previous_text: str) -> bool:
    match = re.search(r"([A-Za-z]+)-$", previous_text.rstrip())
    return bool(match and match.group(1).casefold() in _COMPOUND_HYPHEN_PREFIXES)


def is_likely_pdf_heading(line: str) -> bool:
    """Return whether a short PDF line is likely a standalone heading.

    The function intentionally favors false negatives over false positives:
    a missed heading only affects document structure, while a prose line
    mistakenly classified as a heading destroys a possible sentence boundary.
    """

    stripped = line.strip()
    if not stripped:
        return False
    # PDF equations can be made entirely from one-letter symbols (for example
    # ``R(Q) = V / Q``), which otherwise satisfies a naive "all caps" test.
    # They belong to the surrounding prose, especially when a sentence
    # introduces the equation on the line before it.
    if any(symbol in stripped for symbol in "=<>±×÷*/^{}[]()"):
        return False
    words = _WORD_RE.findall(stripped)
    if not words or len(words) > 16:
        return False
    named_heading = _NAMED_HEADING_RE.match(stripped)
    if named_heading:
        # A lower-case continuation such as ``this\nintroduction.`` is prose,
        # not a section title.  PDF text frequently exposes it as a short line
        # at a page boundary, so accepting it here would create a hard split
        # inside a sentence.
        label = named_heading.group("label")
        return bool(label and label[0].isupper())
    letters = "".join(words)
    return len(letters) >= 3 and letters.isupper() and not re.search(r"[.!?]$", stripped)


def _is_heading_like(line: str) -> bool:
    """Backward-compatible internal name for reconstruction call sites."""

    return is_likely_pdf_heading(line)


def _is_isolated_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER_RE.fullmatch(line) or _ROMAN_PAGE_NUMBER_RE.fullmatch(line))


def _line_key(line: str) -> str:
    normalized = line.casefold().replace("’", "'")
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def _looks_like_running_text(key: str) -> bool:
    letters = "".join(character for character in key if character.isalpha())
    return len(letters) >= 4


def _materialize_reconstruction(
    drafts: Sequence[_PageDraft],
    removed_lines: Sequence[RemovedPdfLayoutLine],
) -> PdfTextReconstruction:
    parts: list[str] = []
    pages: list[ReconstructedPdfPage] = []
    cursor = 0
    emitted_text = False

    for draft in drafts:
        if draft.text and emitted_text:
            parts.append(draft.separator_before)
            cursor += len(draft.separator_before)
        text_start = cursor
        if draft.text:
            parts.append(draft.text)
            cursor += len(draft.text)
            emitted_text = True
        pages.append(
            ReconstructedPdfPage(
                page_number=draft.page_number,
                text=draft.text,
                text_start=text_start,
                text_end=cursor,
                joins_previous=draft.joins_previous,
                layout_role=draft.layout_role,
            )
        )

    return PdfTextReconstruction(
        text="".join(parts),
        pages=tuple(pages),
        removed_lines=tuple(removed_lines),
    )
