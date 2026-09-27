"""Pure EPUB presentation planning for translated document nodes.

The importer and assembler preserve the source-derived node stream.  This module
only decides how that stream is grouped and labelled for a reflowable EPUB; it
never writes to a project, changes a Unit, or calls a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Sequence


_PAGE_TOKEN_RE = re.compile(r"(?<![\w-])(?:\d{1,3}|[ivxlcdm]{1,7})(?![\w-])", re.IGNORECASE)
_HEADING_MARK_RE = re.compile(r"^\s*#{1,6}\s+")

# These are conservative source-language anchors found in the book's own
# contents.  They are only fallbacks for PDF text-shape nodes; explicit layout
# metadata and ordinary Markdown headings always take precedence.
_KNOWN_MAJOR_PREFIXES = (
    "INTRODUCTION",
    "PREFACE",
    "ON PATRIARCHY",
    "WOMEN AND THE LABOUR-MARKET",
    "GENDER AND ACCESS TO THE MEANS OF PRODUCTION",
    "WOMEN AND THE BRITISH LABOUR-MARKET",
    "APPENDIX",
    "NOTES",
    "REFERENCES",
    "NAME INDEX",
    "SUBJECT INDEX",
)


@dataclass(frozen=True)
class NavigationEntry:
    """One reader-navigation entry anchored to an original node."""

    node_id: str
    label: str
    level: int
    role: str
    chapter_index: int


@dataclass(frozen=True)
class EpubLayout:
    """An immutable-in-use export view over the original node sequence."""

    chapters: tuple[tuple[dict[str, Any], ...], ...]
    roles: dict[str, str]
    chapter_by_node: dict[str, int]
    navigation: tuple[NavigationEntry, ...]
    diagnostics: tuple[str, ...]

    @property
    def navigation_by_id(self) -> dict[str, NavigationEntry]:
        return {entry.node_id: entry for entry in self.navigation}


def parse_contents_entries(value: str) -> list[tuple[str, str]]:
    """Split a contents line into ``(label, printed_page)`` pairs.

    The PDF importer often gives us one flat line such as ``Overview 1 Beyond
    the domestic labour debate 10``.  Only page-shaped tokens are separated;
    all other text is retained verbatim.  A leading chapter number is kept in
    the label rather than mistaken for a page.
    """

    text = str(value or "").strip()
    if not text:
        return []
    matches = list(_PAGE_TOKEN_RE.finditer(text))
    if not matches:
        return [(text, "")]

    leading_chapter = (
        len(matches) >= 2
        and matches[0].start() == 0
        and text[matches[0].end() : matches[1].start()].strip()
    )
    entries: list[tuple[str, str]] = []
    cursor = 0
    for index, match in enumerate(matches):
        if leading_chapter and index == 0:
            # The next page token closes the complete label, e.g.
            # ``1 ON PATRIARCHY 22``.
            continue
        label = text[cursor : match.start()].strip()
        page = match.group(0)
        if label:
            entries.append((label, page))
            cursor = match.end()
        else:
            cursor = match.end()

    remainder = text[cursor:].strip()
    if remainder:
        # Do not discard an ambiguous suffix.  Keeping it as a label makes the
        # unresolved part visible in the EPUB and in the diagnostic report.
        entries.append((remainder, ""))
    return entries or [(text, "")]


def build_epub_layout(nodes: Sequence[dict[str, Any]]) -> EpubLayout:
    """Build an EPUB-only grouping, role and navigation plan.

    The function accepts materialized assembler nodes but does not call the
    assembler itself.  It treats PDF text-shape ``level=1`` headings as weak
    evidence and only creates a new spine group for explicit or well-supported
    major-heading boundaries.
    """

    items = [item for item in nodes if isinstance(item, dict)]
    if not items:
        return EpubLayout((tuple(),), {}, {}, tuple(), ("empty node stream",))

    contents_start = _find_contents_start(items)
    contents_end = _find_contents_end(items, contents_start)
    contents_range = (
        set(range(contents_start, contents_end + 1))
        if contents_start is not None and contents_end is not None
        else set()
    )
    major_prefixes = set(_KNOWN_MAJOR_PREFIXES)
    major_prefixes.update(_contents_major_prefixes(items, contents_range))

    heading_runs = _heading_runs(items)
    run_by_index = {index: run for run in heading_runs for index in run}
    explicit_roles: dict[int, str] = {}
    diagnostics: list[str] = []
    for index, item in enumerate(items):
        role = _explicit_role(item)
        if role:
            explicit_roles[index] = role

    boundaries: set[int] = set()
    major_indices: set[int] = set()
    seen_major_keys: set[str] = set()
    for index, item in enumerate(items):
        if index in contents_range:
            continue
        if _is_major_start(
            items,
            index,
            run_by_index.get(index, (index,)),
            major_prefixes,
            explicit_roles,
        ):
            major_key = _major_match_key(
                items,
                index,
                run_by_index.get(index, (index,)),
                major_prefixes,
                explicit_roles,
            )
            if major_key and major_key in seen_major_keys:
                # Running headers or repeated section labels are common in
                # PDF extraction.  Keep their text in the current chapter,
                # but do not create a second spine boundary.
                continue
            if major_key:
                seen_major_keys.add(major_key)
            if index > 0:
                boundaries.add(index)
            major_indices.add(index)

    if contents_start is not None and contents_start > 0:
        boundaries.add(contents_start)
    if contents_end is not None and contents_end + 1 < len(items):
        # The first front-matter heading after contents starts a new flow.
        boundaries.add(contents_end + 1)

    # A title fragment sequence before the contents is one front-matter flow;
    # an explicit Markdown H1 remains a chapter boundary everywhere else.
    chapters: list[list[dict[str, Any]]] = []
    chapter_by_node: dict[str, int] = {}
    for index, item in enumerate(items):
        if not chapters or (index in boundaries and chapters[-1]):
            chapters.append([])
        chapters[-1].append(item)
        node_id = str(item.get("id") or "")
        if node_id:
            chapter_by_node[node_id] = len(chapters) - 1

    roles: dict[str, str] = {}
    for index, item in enumerate(items):
        node_id = str(item.get("id") or "")
        if not node_id:
            continue
        explicit = explicit_roles.get(index)
        if explicit:
            roles[node_id] = explicit
        elif index in contents_range:
            roles[node_id] = (
                "contents-running"
                if index != contents_start
                and item.get("type") == "heading"
                and _normalise(_display_text(item)) in {"CONTENTS", "目录"}
                else "contents"
            )
        elif contents_start is not None and index < contents_start:
            roles[node_id] = "title" if _is_title_fragment(index, run_by_index, items) else "frontmatter"
        elif index in major_indices:
            roles[node_id] = "chapter"
        elif item.get("type") == "heading":
            roles[node_id] = "section"
        else:
            roles[node_id] = "body"

    # Mark only the continuation lines that complete a known chapter title.
    # They remain separate anchored nodes, but the exporter can give them
    # compact title styling and navigation can keep one entry.
    for run in heading_runs:
        if not run:
            continue
        first_id = str(items[run[0]].get("id") or "")
        if roles.get(first_id) != "chapter":
            continue
        group = [run[0]]
        for index in run[1:]:
            node_id = str(items[index].get("id") or "")
            if roles.get(node_id) != "section":
                break
            combined = _normalise(
                " ".join(_heading_source_label(items[item_index]) for item_index in group + [index])
            )
            if not _matches_major_fragment(combined, major_prefixes):
                break
            roles[node_id] = "chapter-fragment"
            group.append(index)

    # If there is no detected contents block, a leading heading run is still a
    # title block only when it is followed by non-heading content.  This keeps
    # ordinary Markdown fixtures predictable without guessing at paragraphs.
    if contents_start is None:
        for run in heading_runs:
            if len(run) >= 2 and run[0] == 0:
                for index in run:
                    node_id = str(items[index].get("id") or "")
                    if node_id and not _is_explicit_markdown_heading(items[index]):
                        roles[node_id] = "title"

    navigation: list[NavigationEntry] = []
    for run in heading_runs:
        position = 0
        while position < len(run):
            index = run[position]
            item = items[index]
            node_id = str(item.get("id") or "")
            role = roles.get(node_id, "body")
            if role in {"title", "contents", "contents-running", "frontmatter", "chapter-fragment"}:
                position += 1
                continue

            group = [index]
            if role == "section":
                # Adjacent PDF-shaped section lines are usually one wrapped
                # title.  They share one navigation label while each source
                # node keeps its own body anchor.
                cursor = position + 1
                while cursor < len(run):
                    candidate = run[cursor]
                    candidate_id = str(items[candidate].get("id") or "")
                    if roles.get(candidate_id) not in {"section", "chapter-fragment"}:
                        break
                    if chapter_by_node.get(candidate_id) != chapter_by_node.get(node_id):
                        break
                    if _PAGE_TOKEN_RE.search(_heading_source_label(items[candidate])):
                        break
                    group.append(candidate)
                    cursor += 1
                position = cursor
            elif role == "chapter":
                # A true chapter heading may itself be split across adjacent
                # PDF blocks.  Consume only the prefix that matches a known
                # major title; a following subsection remains a separate nav
                # entry (for example chapter 4's first section).
                cursor = position + 1
                while cursor < len(run):
                    candidate = run[cursor]
                    candidate_id = str(items[candidate].get("id") or "")
                    if roles.get(candidate_id) not in {"section", "chapter-fragment"}:
                        break
                    combined = _normalise(
                        " ".join(
                            _heading_source_label(items[item_index])
                            for item_index in group + [candidate]
                        )
                    )
                    if not _matches_major_fragment(combined, major_prefixes):
                        break
                    group.append(candidate)
                    cursor += 1
                position = cursor
            else:
                position += 1

            label = " ".join(_heading_label(items[item_index]) for item_index in group).strip()
            if not label:
                diagnostics.append(f"empty navigation label for {node_id}")
                continue
            navigation.append(
                NavigationEntry(
                    node_id=node_id,
                    label=label,
                    level=1 if role == "chapter" else 2,
                    role=role,
                    chapter_index=chapter_by_node.get(node_id, 0),
                )
            )
            for skipped_index in group[1:]:
                skipped_id = str(items[skipped_index].get("id") or "")
                if skipped_id:
                    diagnostics.append(f"navigation label merged for {skipped_id} into {node_id}")

    if contents_start is None:
        diagnostics.append("contents region not identified; source text kept intact")
    elif not any(roles.get(str(item.get("id") or "")) == "contents" for item in items):
        diagnostics.append("contents region empty")

    return EpubLayout(
        chapters=tuple(tuple(chapter) for chapter in chapters if chapter),
        roles=roles,
        chapter_by_node=chapter_by_node,
        navigation=tuple(navigation),
        diagnostics=tuple(diagnostics),
    )


def _find_contents_start(nodes: Sequence[dict[str, Any]]) -> int | None:
    for index, item in enumerate(nodes):
        if _normalise(_display_text(item)) in {"CONTENTS", "目录"}:
            return index
    return None


def _find_contents_end(nodes: Sequence[dict[str, Any]], start: int | None) -> int | None:
    if start is None:
        return None
    for index in range(start + 1, len(nodes)):
        if nodes[index].get("type") != "heading":
            continue
        value = _normalise(_display_text(nodes[index]))
        if value in {"PREFACE", "前言"}:
            return index - 1
        # A fallback for books whose preface label is not available.
        if index > start and value in {"INTRODUCTION", "引言"}:
            return index - 1
    return len(nodes) - 1


def _contents_major_prefixes(nodes: Sequence[dict[str, Any]], contents: set[int]) -> set[str]:
    prefixes: set[str] = set()
    for index in sorted(contents):
        item = nodes[index]
        if item.get("type") != "heading":
            continue
        text = _source_text(item)
        clean = _normalise(_strip_heading_mark(text))
        match = re.match(r"^\d+\s+(.+?)\s+\d{1,3}$", clean)
        if match:
            prefixes.add(match.group(1).strip())
        elif clean in {"APPENDIX", "NOTES", "REFERENCES", "NAME INDEX", "SUBJECT INDEX"}:
            prefixes.add(clean)
    return prefixes


def _heading_runs(nodes: Sequence[dict[str, Any]]) -> list[tuple[int, ...]]:
    runs: list[tuple[int, ...]] = []
    current: list[int] = []
    for index, item in enumerate(nodes):
        if item.get("type") == "heading":
            current.append(index)
            continue
        if current:
            runs.append(tuple(current))
            current = []
    if current:
        runs.append(tuple(current))
    return runs


def _is_major_start(
    nodes: Sequence[dict[str, Any]],
    index: int,
    run: Iterable[int],
    prefixes: set[str],
    explicit_roles: dict[int, str],
) -> bool:
    item = nodes[index]
    role = explicit_roles.get(index)
    if role in {"chapter", "major"}:
        return True
    if role in {"section", "title", "contents", "contents-running", "frontmatter"}:
        return False
    if _is_explicit_markdown_heading(item):
        return True
    if item.get("type") != "heading":
        return False
    run_indices = tuple(run)
    if run_indices and index != run_indices[0]:
        return False
    source = " ".join(_normalise(_strip_heading_mark(_source_text(nodes[i]))) for i in run_indices)
    translated = " ".join(_normalise(_strip_heading_mark(_display_text(nodes[i]))) for i in run_indices)
    if _major_match_key(nodes, index, run_indices, prefixes, explicit_roles) is not None:
        return True
    for candidate in (source, translated):
        if re.match(r"^CHAPTER\s+\d+\b", candidate):
            return True
        if re.match(r"^\d+\s+", candidate) and len(candidate) > 3:
            return True
        if _matches_major_prefix(candidate, prefixes):
            return True
    return False


def _major_match_key(
    nodes: Sequence[dict[str, Any]],
    index: int,
    run: Iterable[int],
    prefixes: set[str],
    explicit_roles: dict[int, str],
) -> str | None:
    if explicit_roles.get(index) in {"chapter", "major"} or _is_explicit_markdown_heading(nodes[index]):
        return None
    run_indices = tuple(run)
    first = run_indices[0] if run_indices else index
    combined_candidates = [
        _normalise(" ".join(_strip_heading_mark(_source_text(nodes[i])) for i in run_indices)),
        _normalise(" ".join(_strip_heading_mark(_display_text(nodes[i])) for i in run_indices)),
    ]
    first_candidates = [
        _normalise(_strip_heading_mark(_source_text(nodes[first]))),
        _normalise(_strip_heading_mark(_display_text(nodes[first]))),
    ]
    for candidate in combined_candidates:
        if re.match(r"^CHAPTER\s+\d+\b", candidate) or re.match(r"^\d+\s+", candidate):
            return None
        for prefix in prefixes:
            normalized_prefix = _normalise(prefix)
            if (
                candidate == normalized_prefix
                or candidate.startswith(normalized_prefix + " ")
            ):
                return normalized_prefix
    for candidate in first_candidates:
        if re.match(r"^CHAPTER\s+\d+\b", candidate) or re.match(r"^\d+\s+", candidate):
            return None
        for prefix in prefixes:
            normalized_prefix = _normalise(prefix)
            if (
                candidate == normalized_prefix
                or candidate.startswith(normalized_prefix + " ")
                or (len(candidate.split()) >= 3 and normalized_prefix.startswith(candidate + " "))
            ):
                return normalized_prefix
    return None


def _matches_major_prefix(candidate: str, prefixes: set[str]) -> bool:
    candidate = _normalise(candidate)
    for prefix in prefixes:
        prefix = _normalise(prefix)
        if candidate == prefix or candidate.startswith(prefix + " "):
            return True
        # A PDF line can stop halfway through a real chapter title.  Require
        # at least three words before accepting a prefix to avoid promoting
        # short section names such as ``ON`` or ``WOMEN``.
        words = candidate.split()
        if len(words) >= 3 and prefix.startswith(candidate + " "):
            return True
    return False


def _matches_major_fragment(candidate: str, prefixes: set[str]) -> bool:
    """Return true only while a split heading is still incomplete."""

    candidate = _normalise(candidate)
    for prefix in prefixes:
        normalized_prefix = _normalise(prefix)
        if candidate == normalized_prefix:
            return True
        if len(candidate.split()) >= 3 and normalized_prefix.startswith(candidate + " "):
            return True
    return False


def _is_title_fragment(index: int, runs: dict[int, tuple[int, ...]], nodes: Sequence[dict[str, Any]]) -> bool:
    if nodes[index].get("type") != "heading":
        return False
    run = runs.get(index, (index,))
    return len(run) >= 2 and all(not _is_explicit_markdown_heading(nodes[item]) for item in run)


def _explicit_role(item: dict[str, Any]) -> str | None:
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    for key in ("epub_role", "layout_role", "display_role"):
        value = str(attributes.get(key) or "").strip().lower()
        if value in {"title", "contents", "frontmatter", "chapter", "major", "section", "body"}:
            return "chapter" if value == "major" else value
    if attributes.get("chapter_break") or attributes.get("chapter_start"):
        return "chapter"
    return None


def _is_explicit_markdown_heading(item: dict[str, Any]) -> bool:
    if item.get("type") != "heading":
        return False
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    detected = str(attributes.get("detected_from") or "").lower()
    source = _source_text(item)
    return detected in {"markdown", "markdown_heading", "md"} or bool(_HEADING_MARK_RE.match(source))


def _heading_label(item: dict[str, Any]) -> str:
    return _strip_heading_mark(_display_text(item)).strip()


def _heading_source_label(item: dict[str, Any]) -> str:
    return _strip_heading_mark(_source_text(item)).strip()


def _display_text(item: dict[str, Any]) -> str:
    return str(item.get("translated_text") or item.get("text") or item.get("source") or "")


def _source_text(item: dict[str, Any]) -> str:
    return str(item.get("source") or item.get("translated_text") or item.get("text") or "")


def _strip_heading_mark(value: str) -> str:
    return _HEADING_MARK_RE.sub("", str(value or "")).strip()


def _normalise(value: str) -> str:
    value = (
        str(value or "")
        .replace("’", "'")
        .replace("–", "-")
        .replace("—", "-")
        .replace("&", " AND ")
    )
    value = re.sub(r"\s+", " ", value).strip().upper()
    return value
