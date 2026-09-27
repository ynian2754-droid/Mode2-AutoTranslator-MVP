"""Conservative sentence-boundary detection for locally extracted English text."""

from __future__ import annotations

import re
from dataclasses import dataclass


_CLOSING_PUNCTUATION = frozenset('"\'”’»）)]}')
_TERMINAL_PUNCTUATION = frozenset(".!?…。！？")
_HARD_NON_TERMINAL_ABBREVIATIONS = frozenset(
    {
        "a.m",
        "approx",
        "art",
        "assoc",
        "ave",
        "capt",
        "cf",
        "co",
        "col",
        "corp",
        "dr",
        "e.g",
        "ed",
        "eds",
        "etc",
        "fig",
        "gen",
        "gov",
        "i.e",
        "inc",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "jr",
        "ltd",
        "maj",
        "messrs",
        "mr",
        "mrs",
        "ms",
        "no",
        "pp",
        "p",
        "prof",
        "rev",
        "sen",
        "sr",
        "st",
        "u.k",
        "u.s",
        "vol",
        "vs",
    }
)
_INITIALISM_RE = re.compile(r"(?:[A-Za-z]\.){2,}$")
_SINGLE_INITIAL_RE = re.compile(r"[A-Z]\.$")


@dataclass(frozen=True)
class SentenceSpan:
    """An exact, lossless source range ending at a safe sentence boundary."""

    start: int
    end: int
    text: str
    is_complete: bool = False


def sentence_spans(value: str) -> list[SentenceSpan]:
    """Return sentence-like spans without splitting common English abbreviations.

    This is deliberately a conservative boundary detector rather than a grammar
    parser.  A doubtful full stop remains inside the current span; that may
    make a span longer, but it never fabricates a sentence break merely because
    a PDF extractor exposed punctuation next to a visual line wrap.
    """

    if not value:
        return []

    spans: list[SentenceSpan] = []
    start = 0
    index = 0
    length = len(value)

    while index < length:
        character = value[index]
        if character not in _TERMINAL_PUNCTUATION:
            index += 1
            continue

        punctuation_end = index + 1
        while punctuation_end < length and value[punctuation_end] in _TERMINAL_PUNCTUATION:
            punctuation_end += 1
        closing_end = punctuation_end
        while closing_end < length and value[closing_end] in _CLOSING_PUNCTUATION:
            closing_end += 1

        cjk_terminal = any(mark in "。！？" for mark in value[index:punctuation_end])
        if closing_end < length and not value[closing_end].isspace() and not cjk_terminal:
            index = punctuation_end
            continue
        if not _is_sentence_boundary(value, index, punctuation_end, closing_end):
            index = punctuation_end
            continue

        end = closing_end
        while end < length and value[end].isspace():
            end += 1
        spans.append(SentenceSpan(start=start, end=end, text=value[start:end], is_complete=True))
        start = end
        index = end

    if start < length:
        spans.append(SentenceSpan(start=start, end=length, text=value[start:length], is_complete=False))
    return spans


def split_sentences(value: str) -> list[str]:
    """Compatibility-friendly text-only view of :func:`sentence_spans`."""

    return [span.text for span in sentence_spans(value)]


def _is_sentence_boundary(
    value: str,
    punctuation_start: int,
    punctuation_end: int,
    closing_end: int,
) -> bool:
    punctuation = value[punctuation_start:punctuation_end]
    if any(character in "!?…。！？" for character in punctuation):
        return True
    if len(punctuation) > 1:
        # An ellipsis (or multiple full stops used as one) is a safe boundary
        # whenever it is followed by whitespace/end-of-input.
        return True
    if value[punctuation_start] != ".":
        return False

    if _is_decimal_point(value, punctuation_start):
        return False
    token = _period_token(value, punctuation_start)
    if token.casefold() in _HARD_NON_TERMINAL_ABBREVIATIONS:
        return False
    if _INITIALISM_RE.search(value[: punctuation_start + 1]):
        return _following_text_starts_sentence(value, closing_end)
    if _SINGLE_INITIAL_RE.search(value[: punctuation_start + 1]):
        return False
    return True


def _is_decimal_point(value: str, index: int) -> bool:
    return index > 0 and index + 1 < len(value) and value[index - 1].isdigit() and value[index + 1].isdigit()


def _period_token(value: str, period_index: int) -> str:
    start = period_index
    while start > 0 and (value[start - 1].isalpha() or value[start - 1] == "."):
        start -= 1
    return value[start:period_index]


def _following_text_starts_sentence(value: str, index: int) -> bool:
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value):
        return True
    while index < len(value) and value[index] in _CLOSING_PUNCTUATION:
        index += 1
    return index >= len(value) or value[index].isupper()
