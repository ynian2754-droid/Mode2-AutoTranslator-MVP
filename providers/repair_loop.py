"""Bounded, request-local content-repair loop shared by the model adapters.

The loop is deliberately small and generic: the translation adapter and the
review adapter each keep their own validator and feedback text, and only the
mechanics of "ask again with the previous visible answer and a concrete,
controlled error" live here.  There is no plugin registry, no shared cache and
no cross-invocation state: the message history is a local list that dies with
the call.

Only locally-detected content/protocol violations are repairable.  Transport or
service failures (network, auth, quota, 429, 5xx, damaged response envelope) and
controller-side rejections (cancel, project switch, stale revision) must never
be turned into model repair rounds, so the loop only catches
:class:`ContentRepairError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .api_client import ProviderRequestError

MAX_CONTENT_ROUNDS = 3
HISTORY_CHAR_BUDGET = 64_000
MAX_RECORDED_ERRORS = 3

_REASONING_BLOCK_RE = re.compile(
    r"<(?P<tag>think|analysis|reasoning)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_REASONING_OPEN_RE = re.compile(
    r"<(?P<tag>think|analysis|reasoning)\b[^>]*>",
    re.IGNORECASE,
)

DROPPED_CANDIDATE_PLACEHOLDER = (
    "（上一轮回答因包含无法安全分离的思维过程标记而未进入下一轮，具体错误见下。）"
)


class ContentRepairError(RuntimeError):
    """A locally detected content/protocol violation the model may fix.

    The three public attributes are the controlled vocabulary that may be sent
    back as feedback: a stable code, a field path, and a short reason.  Raw
    exception text, stack traces, local paths and credentials must never be
    passed here.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: str = "content_error",
        location: str = "response",
    ) -> None:
        super().__init__(str(detail))
        self.code = str(code)
        self.location = str(location)
        self.detail = str(detail)

    def as_record(self) -> dict[str, str]:
        return {"code": self.code, "location": self.location, "detail": self.detail}

    def feedback(self) -> str:
        return (
            "[本地内容协议校验未通过]\n"
            f"错误代码：{self.code}\n"
            f"位置：{self.location}\n"
            f"原因：{self.detail}\n"
            "请只修正当前任务的输出格式与约束后重新提交本次结果；"
            "不要解释、不要复述本条反馈，也不要为了通过校验而改变真实的判断结论。"
        )


class ContentRepairExhausted(ProviderRequestError):
    """All content-repair rounds were used without a protocol-valid answer."""

    def __init__(self, message: str, *, outcome: "RepairOutcome") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class RepairOutcome:
    round: int
    max_rounds: int
    api_calls: int
    errors: tuple[dict[str, str], ...] = ()
    usage: dict[str, int] = field(default_factory=dict)

    def payload(self, *, invocation_id: str) -> dict[str, Any]:
        """The minimal, serializable per-execution summary (never raw messages)."""
        return {
            "invocation_id": invocation_id,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "api_calls": self.api_calls,
            "success_round": self.round,
            "errors": list(self.errors[-MAX_RECORDED_ERRORS:]),
        }


@dataclass
class RepairProgress:
    kind: str
    invocation_id: str
    status: str
    round: int
    max_rounds: int
    api_calls: int
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class RepairControl:
    """Optional execution hook carried on a request; ``None`` means no hook.

    Calls are bound to one invocation identity so the controller can reject a
    stale notification.  It is a plain in-process object: it must never be
    serialized into the model context or persisted with the project.
    """

    invocation_id: str
    kind: str
    before_attempt: Callable[[int, int], None] | None = None
    on_progress: Callable[[RepairProgress], None] | None = None

    def check(self, round_no: int, api_calls: int) -> None:
        if self.before_attempt is not None:
            self.before_attempt(round_no, api_calls)

    def notify(self, progress: RepairProgress) -> None:
        if self.on_progress is not None:
            self.on_progress(progress)


def sanitize_visible_candidate(raw: str) -> tuple[str | None, bool]:
    """Return the part of a candidate that is safe to show the model again.

    Explicit reasoning wrappers are removed.  An unclosed wrapper is treated as
    "everything from the opening marker on is reasoning" and dropped, so the
    visible prefix can still be forwarded.  Nothing is ever invented here.
    """

    text = str(raw or "")
    had_reasoning = bool(_REASONING_OPEN_RE.search(text))
    cleaned = _REASONING_BLOCK_RE.sub(" ", text)
    unclosed = _REASONING_OPEN_RE.search(cleaned)
    if unclosed:
        cleaned = cleaned[: unclosed.start()]
    cleaned = cleaned.strip()
    return (cleaned or None), had_reasoning


def run_content_repair_loop(
    *,
    kind: str,
    messages: list[dict[str, str]],
    chat: Callable[[list[dict[str, str]], dict[str, object] | None], tuple[str, dict[str, int]]],
    validate: Callable[[str], Any],
    control: RepairControl | None = None,
    invocation_id: str = "",
    response_format: dict[str, object] | None = None,
    max_rounds: int = MAX_CONTENT_ROUNDS,
    history_budget: int = HISTORY_CHAR_BUDGET,
) -> tuple[Any, RepairOutcome]:
    """Ask up to ``max_rounds`` content generations, repairing on protocol errors.

    Returns ``(value, outcome)`` on success.  Raises the original
    :class:`ProviderRequestError` for non-repairable failures, or
    :class:`ContentRepairExhausted` when the rounds run out.
    """

    rounds = max(1, int(max_rounds))
    history: list[dict[str, str]] = [dict(message) for message in messages]
    errors: list[dict[str, str]] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    extra_chars = 0
    api_calls = 0
    pending_format = dict(response_format) if response_format is not None else None
    degraded = False

    def notify(status: str, round_no: int) -> None:
        if control is None:
            return
        control.notify(
            RepairProgress(
                kind=kind,
                invocation_id=control.invocation_id or invocation_id,
                status=status,
                round=round_no,
                max_rounds=rounds,
                api_calls=api_calls,
                errors=list(errors[-MAX_RECORDED_ERRORS:]),
            )
        )

    for round_no in range(1, rounds + 1):
        notify("running" if round_no == 1 else "repairing", round_no)
        # A parameter-rejection retry is an extra HTTP request but the same
        # content round: it never consumes a round and never re-reads config.
        while True:
            # Every HTTP request — including that retry — is authorized by the
            # controller immediately before it is sent, so a cancellation or a
            # superseded execution cannot buy one more call.
            if control is not None:
                control.check(round_no, api_calls)
            api_calls += 1
            try:
                raw, usage = chat(history, pending_format)
            except ProviderRequestError as exc:
                if (
                    pending_format is not None
                    and not degraded
                    and bool(getattr(exc, "response_format_rejected", False))
                ):
                    degraded = True
                    pending_format = None
                    continue
                raise
            else:
                for key in usage_total:
                    usage_total[key] += int(usage.get(key) or 0)

            try:
                value = validate(raw)
            except ContentRepairError as error:
                errors.append(error.as_record())
                if round_no >= rounds:
                    outcome = RepairOutcome(
                        round=round_no,
                        max_rounds=rounds,
                        api_calls=api_calls,
                        errors=tuple(errors),
                        usage=dict(usage_total),
                    )
                    notify("failed", round_no)
                    raise ContentRepairExhausted(
                        f"{kind} 连续 {rounds} 轮未通过本地内容协议校验，已停止。",
                        outcome=outcome,
                    ) from error

                visible, _had_reasoning = sanitize_visible_candidate(raw)
                assistant_text = visible or DROPPED_CANDIDATE_PLACEHOLDER
                feedback_text = error.feedback()
                addition = len(assistant_text) + len(feedback_text)
                if extra_chars + addition > history_budget:
                    budget_error = ContentRepairError(
                        "追加的修正上下文已达到 64,000 字符上限，未截断原文或回答。",
                        code="history_budget_exceeded",
                        location="history",
                    )
                    errors.append(budget_error.as_record())
                    outcome = RepairOutcome(
                        round=round_no,
                        max_rounds=rounds,
                        api_calls=api_calls,
                        errors=tuple(errors),
                        usage=dict(usage_total),
                    )
                    notify("failed", round_no)
                    raise ContentRepairExhausted(
                        "修正上下文预算不足，已停止内容修正。", outcome=outcome
                    )
                history.append({"role": "assistant", "content": assistant_text})
                history.append({"role": "user", "content": feedback_text})
                extra_chars += addition
                notify("repairing", round_no)
                break

            outcome = RepairOutcome(
                round=round_no,
                max_rounds=rounds,
                api_calls=api_calls,
                errors=tuple(errors),
                usage=dict(usage_total),
            )
            return value, outcome

    raise AssertionError("content repair loop exited without a result")  # pragma: no cover


__all__ = [
    "ContentRepairError",
    "ContentRepairExhausted",
    "DROPPED_CANDIDATE_PLACEHOLDER",
    "HISTORY_CHAR_BUDGET",
    "MAX_CONTENT_ROUNDS",
    "MAX_RECORDED_ERRORS",
    "RepairControl",
    "RepairOutcome",
    "RepairProgress",
    "run_content_repair_loop",
    "sanitize_visible_candidate",
]
