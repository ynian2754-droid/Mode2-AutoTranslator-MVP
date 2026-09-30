"""Shared prepare request limits with their original policy values."""

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
