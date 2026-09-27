"""Choose translation-unit boundaries at complete sentence boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Sequence

import mode2_common

from .sentence_segmentation import SentenceSpan, sentence_spans


_MAX_CANDIDATE_SENTENCES = 96
_SOFT_MAX_TARGET_FACTOR = 2


@dataclass(frozen=True)
class SentenceChunk:
    """A lossless source slice selected by the target-length optimizer."""

    start_sentence: int
    end_sentence: int
    start: int
    end: int
    word_count: int
    text: str


def optimize_sentence_chunks(
    value: str,
    *,
    target_words: int,
    spans: Sequence[SentenceSpan] | None = None,
) -> list[SentenceChunk]:
    """Globally choose chunks whose English word counts are near ``target_words``.

    A target is deliberately not an upper bound.  The dynamic-programming
    search considers only complete sentence spans and minimizes the total
    squared deviation from the target across the whole input.  Consequently a
    520-word chunk is preferred to a 430-word chunk followed by a 90-word
    fragment when the configured target is 500.

    The candidate window is bounded for long documents, but a single long
    sentence is always allowed as its own chunk.  The function never divides a
    sentence; callers that need an emergency fallback for malformed,
    punctuation-free data must make that policy explicit outside this planner.
    """

    if target_words <= 0:
        raise ValueError("目标切分词数必须是大于 0 的整数。")
    sentence_list = tuple(spans if spans is not None else sentence_spans(value))
    if not sentence_list:
        return []
    if "".join(span.text for span in sentence_list) != value:
        raise ValueError("句子范围必须按原文顺序无损覆盖。")

    word_counts = [mode2_common.english_word_count(span.text) for span in sentence_list]
    candidate_limit = max(target_words * _SOFT_MAX_TARGET_FACTOR, max(word_counts, default=0))
    costs = [inf] * (len(sentence_list) + 1)
    previous: list[int | None] = [None] * (len(sentence_list) + 1)
    costs[0] = 0.0

    for end_index in range(1, len(sentence_list) + 1):
        chunk_words = 0
        checked = 0
        best_score: tuple[float, int, int] | None = None
        for start_index in range(end_index - 1, -1, -1):
            chunk_words += word_counts[start_index]
            checked += 1
            if checked > _MAX_CANDIDATE_SENTENCES:
                break
            if chunk_words > candidate_limit and start_index < end_index - 1:
                break
            if costs[start_index] == inf:
                continue
            score = (
                costs[start_index] + _chunk_cost(chunk_words, target_words),
                abs(chunk_words - target_words),
                start_index,
            )
            if best_score is None or score < best_score:
                best_score = score
                costs[end_index] = score[0]
                previous[end_index] = start_index

        if previous[end_index] is None:  # Defensive fallback for future policy changes.
            previous[end_index] = end_index - 1
            costs[end_index] = costs[end_index - 1] + _chunk_cost(word_counts[end_index - 1], target_words)

    boundaries: list[tuple[int, int]] = []
    end_index = len(sentence_list)
    while end_index:
        start_index = previous[end_index]
        if start_index is None:  # pragma: no cover - guarded above
            raise RuntimeError("目标长度优化未能找到连续句子边界。")
        boundaries.append((start_index, end_index))
        end_index = start_index
    boundaries.reverse()
    return [_chunk_from_boundary(value, sentence_list, word_counts, start, end) for start, end in boundaries]


def _chunk_cost(word_count: int, target_words: int) -> float:
    deviation = (word_count - target_words) / target_words
    return deviation * deviation


def _chunk_from_boundary(
    value: str,
    spans: Sequence[SentenceSpan],
    word_counts: Sequence[int],
    start_sentence: int,
    end_sentence: int,
) -> SentenceChunk:
    start = spans[start_sentence].start
    end = spans[end_sentence - 1].end
    return SentenceChunk(
        start_sentence=start_sentence,
        end_sentence=end_sentence,
        start=start,
        end=end,
        word_count=sum(word_counts[start_sentence:end_sentence]),
        text=value[start:end],
    )
