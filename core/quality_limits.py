"""Shared prepare request limits with their original policy values."""

from typing import Any

from core.exceptions import PipelineError

#: One shared pool of *extra* logical requests per confirmed execution: the
#: bounded lookup and the large-group local judgments draw from the same number,
#: never one budget each.
DEFAULT_ADDITIONAL_WORK_LIMIT = 10

#: Cards one re-check request may carry. A reused card is re-checked only when
#: its stored check is legacy or its verification identity changed; the bound
#: keeps one execution from turning a large incremental scope into an unbounded
#: number of requests.
MAX_RECHECK_CARDS = 20
#: Units one bounded lookup may cite as evidence. The search itself is local and
#: free; the bound is on the material handed to the one follow-up request.
MAX_LOOKUP_UNITS_PER_EXPRESSION = 5
#: One request slot is not a licence for an unbounded payload: a re-check or a
#: lookup request is also bounded by the units it may cite and by the characters
#: of cards + cited sources it would carry. What does not fit is recorded as
#: unfinished (or carried by the next confirmation), never cut down to size.
MAX_CHECK_REQUEST_UNITS = 40
MAX_CHECK_REQUEST_CHARS = 60_000
MAX_LOOKUP_CARDS = 20


def resolve_parallel_batches(value: Any) -> int:
    """How many generation+check batches the confirmed execution may run at once.

    This is a *worker* count for one confirmed run, frozen into the preview
    like the batch size — never a server-wide queue or a rate limiter. The
    caller may bound it further by the number of batches it really has;
    ``None`` keeps the historical one-batch-at-a-time behaviour.
    """

    if value is None:
        return 1
    if isinstance(value, bool):
        raise PipelineError("并行批数必须是大于或等于 1 的整数。")
    try:
        parallel = int(value)
    except (TypeError, ValueError) as exc:
        raise PipelineError("并行批数必须是大于或等于 1 的整数。") from exc
    if parallel < 1:
        raise PipelineError("并行批数必须是大于或等于 1 的整数。")
    return parallel


def resolve_additional_work_limit(value: Any) -> int:
    """The extra logical request budget shared by every A-stage extra step.

    It is one pool for the whole confirmed execution — bounded lookups and
    large-group local judgments draw from the same number — never one pool
    per kind. ``None`` keeps the documented default.
    """

    if value is None:
        return DEFAULT_ADDITIONAL_WORK_LIMIT
    if isinstance(value, bool):
        raise PipelineError("额外请求预算必须是 0 到 10 之间的整数。")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise PipelineError("额外请求预算必须是 0 到 10 之间的整数。") from exc
    if limit < 0 or limit > DEFAULT_ADDITIONAL_WORK_LIMIT:
        raise PipelineError(
            f"额外请求预算必须在 0 到 {DEFAULT_ADDITIONAL_WORK_LIMIT} 之间。"
        )
    return limit
