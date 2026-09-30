"""Unit rules and mutations; snapshots refresh unit stats then copy the whole project."""

from __future__ import annotations

from core import project_state
from core.exceptions import PipelineError
from core.project_state import ProjectStateCell

import copy
from typing import Any, Callable

ACTIVE_STATUSES = {"translating", "reviewing"}
WAITING_STATUSES = {"waiting_translation", "waiting_review"}
PROCESSING_STATUSES = ACTIVE_STATUSES | WAITING_STATUSES
TRANSLATION_PROCESSING_STATUSES = {"waiting_translation", "translating"}
ACTION_STATUSES = {"needs_action"}
EDITABLE_TRANSLATION_STATUSES = {"needs_action", "passed", "user_modified", "accepted_risk"}
# Accepting a risk is a user override, not an AI review result.  It remains
# editable and can be sent through the normal translate->review retry flow,
# but it must not expose a direct review operation of the already accepted
# version.
REVIEWABLE_TRANSLATION_STATUSES = {"needs_action", "passed", "user_modified"}
CANCELLABLE_START_STATUSES = {"pending", "cancelled"}

TECHNICAL_REVIEW_PROVIDER = "controller"


def retained_previous_draft(unit: dict[str, Any]) -> dict[str, Any] | None:
    """The last successfully saved draft as one group: revision + text + review.

        The group carries the revision it was saved at, so a repeated failure, a
        cancellation or a restart can never re-bind it to the attempt that
        failed, and a technical failure is never reported as its review. It is
        replaced only when a *later* draft is saved — the commit consumes the
        one-shot record at that moment.
        """

    feedback = unit.get("pending_translation_feedback")
    if isinstance(feedback, dict):
        revision = feedback.get("source_revision")
        text = feedback.get("previous_translation")
        if (
            isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            and isinstance(text, str)
            and text.strip()
        ):
            review = feedback.get("previous_review")
            return {
                "revision": revision,
                "translation": text,
                "review": copy.deepcopy(review) if isinstance(review, dict) else None,
                "suggestions": normalize_suggestions(feedback.get("suggestions")),
            }
    text = unit.get("translation")
    if not isinstance(text, str) or not text.strip():
        return None
    revision = unit.get("translation_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        revision = 0
    review = unit.get("review")
    if (
        not isinstance(review, dict)
        or review.get("translation_revision") != revision
        or str(review.get("provider") or "") == TECHNICAL_REVIEW_PROVIDER
    ):
        review = None
    suggestions = extract_review_suggestions(review) if review else []
    if not suggestions:
        suggestions = normalize_suggestions(unit.get("review_suggestions"))
    return {
        "revision": revision,
        "translation": text,
        "review": copy.deepcopy(review) if isinstance(review, dict) else None,
        "suggestions": suggestions,
    }

def normalize_suggestions(value: Any) -> list[str]:
    """Keep only useful, ordered, unique suggestion strings."""
    if not isinstance(value, list):
        return []
    suggestions: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        suggestion = item.strip()
        if not suggestion or suggestion in seen:
            continue
        seen.add(suggestion)
        suggestions.append(suggestion)
    return suggestions

def extract_review_suggestions(review_or_issues: Any) -> list[str]:
    """Extract only evidence.suggestion values from a review payload."""
    if isinstance(review_or_issues, dict):
        issues = review_or_issues.get("issues", [])
    else:
        issues = review_or_issues
    if not isinstance(issues, list):
        return []

    suggestions: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        evidence = issue.get("evidence")
        if not isinstance(evidence, dict):
            continue
        suggestion = evidence.get("suggestion")
        if not isinstance(suggestion, str):
            continue
        suggestion = suggestion.strip()
        if not suggestion or suggestion in seen:
            continue
        seen.add(suggestion)
        suggestions.append(suggestion)
    return suggestions

def ensure_unit_feedback_fields(unit: dict[str, Any]) -> None:
    """Backfill feedback fields without changing the existing unit schema."""
    user_edited_translation = unit.get("user_edited_translation")
    if user_edited_translation is not None and not isinstance(user_edited_translation, str):
        user_edited_translation = None
    if isinstance(user_edited_translation, str):
        user_edited_translation = user_edited_translation.strip() or None
    unit["user_edited_translation"] = user_edited_translation

    revision = unit.setdefault("translation_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        unit["translation_revision"] = 0

    review = unit.get("review")
    if "review_suggestions" not in unit:
        unit["review_suggestions"] = extract_review_suggestions(review)
    else:
        unit["review_suggestions"] = normalize_suggestions(unit.get("review_suggestions"))

    if "pending_translation_feedback" not in unit:
        unit["pending_translation_feedback"] = None
    else:
        feedback = unit.get("pending_translation_feedback")
        if isinstance(feedback, dict):
            source_revision = feedback.get("source_revision")
            if (
                isinstance(source_revision, bool)
                or not isinstance(source_revision, int)
                or source_revision < 0
            ):
                unit["pending_translation_feedback"] = None
            else:
                feedback["source_revision"] = source_revision
                feedback["suggestions"] = normalize_suggestions(feedback.get("suggestions"))
                previous_translation = feedback.get("previous_translation")
                if previous_translation is not None and not isinstance(previous_translation, str):
                    feedback.pop("previous_translation", None)
                # Frozen with the rest of the record: the review that belonged
                # to the draft above. Absent in older projects and simply
                # dropped when it is not a mapping.
                if "previous_review" in feedback and not isinstance(
                    feedback.get("previous_review"), dict
                ):
                    feedback.pop("previous_review", None)
        elif feedback is not None:
            unit["pending_translation_feedback"] = None

    if isinstance(review, dict):
        review.setdefault("translation_revision", unit["translation_revision"])

    # Optional, backward-compatible summary of the last bounded model-repair
    # execution per kind.  Legacy units simply get an empty dict; a restart
    # never keeps an in-flight round looking alive.
    repair = unit.get("model_repair")
    normalized: dict[str, Any] = {}
    if isinstance(repair, dict):
        for kind in ("translation", "review"):
            entry = normalize_repair_entry(repair.get(kind))
            if entry is not None:
                normalized[kind] = entry
    unit["model_repair"] = normalized

def normalize_repair_errors(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        errors.append(
            {
                "code": code.strip()[:80],
                "location": str(item.get("location") or "response")[:120],
                "detail": str(item.get("detail") or "")[:400],
            }
        )
    return errors[-3:]

def normalize_repair_entry(value: Any) -> dict[str, Any] | None:
    """Keep only the documented summary keys, and never claim a live session."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if status not in {"running", "repairing", "succeeded", "failed", "cancelled"}:
        status = "failed"
    errors = normalize_repair_errors(value.get("errors"))
    if value.get("status") in {"running", "repairing"}:
        # A restart cannot resume an in-flight model session.
        status = "failed"
        errors = (errors + [
            {
                "code": "interrupted",
                "location": "invocation",
                "detail": "应用重启后该次模型执行已中断。",
            }
        ])[-3:]
    entry: dict[str, Any] = {
        "invocation_id": str(value.get("invocation_id") or ""),
        "status": status,
        "round": max(0, int(value.get("round") or 0)),
        "max_rounds": max(0, int(value.get("max_rounds") or 0)),
        "api_calls": max(0, int(value.get("api_calls") or 0)),
        "success_round": None,
        "errors": errors,
    }
    success_round = value.get("success_round")
    if status == "succeeded" and isinstance(success_round, int) and not isinstance(success_round, bool):
        entry["success_round"] = success_round
    source_sha256 = value.get("source_sha256")
    if isinstance(source_sha256, str) and source_sha256:
        entry["source_sha256"] = source_sha256
    translation_revision = value.get("translation_revision")
    if isinstance(translation_revision, int) and not isinstance(translation_revision, bool):
        entry["translation_revision"] = translation_revision
    return entry



def find_unit(state: dict[str, Any], unit_id: str) -> dict[str, Any]:
    for unit in state["units"]:
        if unit["id"] == unit_id:
            return unit
    raise PipelineError(f"找不到翻译单元：{unit_id}")


def mark_cancelled(cell: ProjectStateCell, unit_id: str, message: str = "用户已停止当前流水线。", *, clock: Callable[[], str]) -> None:
    unit = find_unit(cell.state, unit_id)
    if unit.get("status") == "cancelled":
        return
    unit["status"] = "cancelled"
    unit["last_error"] = message
    unit["updated_at"] = clock()
    project_state.append_event(cell, "unit_cancelled", message, unit_id, {}, clock)


def mark_failure(cell: ProjectStateCell, unit_id: str, rule: str, message: str, *, clock: Callable[[], str]) -> None:
    unit = find_unit(cell.state, unit_id)
    ensure_unit_feedback_fields(unit)
    unit["status"] = "needs_action"
    unit["last_error"] = message
    unit["review_suggestions"] = []
    unit["review_issues"] = [
        {
            "rule": rule,
            "severity": "error",
            "block_id": unit_id,
            "message": message,
            "evidence": {},
        }
    ]
    unit["review"] = {
        "verdict": "FAIL",
        "issues": unit["review_issues"],
        "metrics": {},
        "provider": "controller",
        "model": "strict-import-gate",
        "translation_revision": unit["translation_revision"],
        "at": clock(),
    }
    unit["updated_at"] = clock()
    project_state.append_event(cell, "unit_failed", message, unit_id, {'rule': rule}, clock)


def repair_summary_payload(
    *,
    invocation_id: str,
    status: str,
    round_no: Any,
    max_rounds: Any,
    api_calls: Any,
    success_round: Any,
    errors: Any,
    source_sha256: str | None = None,
    translation_revision: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "invocation_id": str(invocation_id or ""),
        "status": status,
        "round": max(0, int(round_no or 0)),
        "max_rounds": max(0, int(max_rounds or 0)),
        "api_calls": max(0, int(api_calls or 0)),
        "success_round": success_round if status == "succeeded" else None,
        "errors": normalize_repair_errors(errors),
    }
    if source_sha256:
        payload["source_sha256"] = str(source_sha256)
    if isinstance(translation_revision, int) and not isinstance(translation_revision, bool):
        payload["translation_revision"] = translation_revision
    return payload


def record_repair_failure(
    state: dict[str, Any],
    unit_id: str,
    kind: str,
    invocation_id: str,
    error: BaseException,
    *,
    source_sha256: str,
    translation_revision: int | None,
    status: str = "failed",
) -> None:
    """Record a terminal failure without leaking raw exception text."""
    unit = find_unit(state, unit_id)
    existing = unit.setdefault("model_repair", {}).get(kind)
    outcome = getattr(error, "outcome", None)
    if outcome is not None:
        payload = repair_summary_payload(
            invocation_id=invocation_id,
            status=status,
            round_no=getattr(outcome, "round", 0),
            max_rounds=getattr(outcome, "max_rounds", 0),
            api_calls=getattr(outcome, "api_calls", 0),
            success_round=None,
            errors=list(getattr(outcome, "errors", ()) or ()),
            source_sha256=source_sha256,
            translation_revision=translation_revision,
        )
    else:
        saved = existing if isinstance(existing, dict) else {}
        payload = repair_summary_payload(
            invocation_id=invocation_id,
            status=status,
            round_no=saved.get("round", 0),
            max_rounds=saved.get("max_rounds", 0),
            api_calls=saved.get("api_calls", 0),
            success_round=None,
            errors=[
                {
                    "code": "provider_error" if status == "failed" else "cancelled",
                    "location": "request",
                    "detail": "模型调用未完成，未进行内容修正。"
                    if status == "failed"
                    else "该次模型执行已被取消。",
                }
            ],
            source_sha256=source_sha256,
            translation_revision=translation_revision,
        )
    unit["model_repair"][kind] = payload


def restore_unit_commit(
    state: dict[str, Any],
    unit_id: str,
    unit_snapshot: dict[str, Any],
    events_snapshot: list[Any],
) -> None:
    """Roll one failed result commit back to its pre-commit in-memory state.

        A model result is not business state until it is on disk.  Without this,
        a failed save would leave the half-committed unit (and its success
        event) in memory for the next save to publish, even though the user was
        told the commit failed.  Only the two result-commit paths use this; it
        is not a storage layer or a general transaction mechanism.
        """
    unit = find_unit(state, unit_id)
    unit.clear()
    unit.update(unit_snapshot)
    state["events"] = list(events_snapshot)


def mark_repair_save_failed(
    unit: dict[str, Any],
    kind: str,
    repair: Any = None,
) -> None:
    """A failed save must never leave a success claim behind.

        ``repair`` is the completed model summary when the caller still holds
        it; otherwise the entry already on the unit is downgraded.
        """
    existing = (unit.get("model_repair") or {}).get(kind)
    saved = existing if isinstance(existing, dict) else {}
    data = repair if isinstance(repair, dict) else saved
    errors = list(data.get("errors") or saved.get("errors") or [])
    errors.append(
        {
            "code": "save_failed",
            "location": "commit",
            "detail": "结果落盘失败，未记录为成功。",
        }
    )
    unit.setdefault("model_repair", {})[kind] = repair_summary_payload(
        invocation_id=data.get("invocation_id") or saved.get("invocation_id") or "",
        status="failed",
        round_no=data.get("round", saved.get("round")),
        max_rounds=data.get("max_rounds", saved.get("max_rounds")),
        api_calls=data.get("api_calls", saved.get("api_calls")),
        success_round=None,
        errors=errors,
        source_sha256=unit.get("source_sha256"),
        translation_revision=unit.get("translation_revision")
        if kind == "review"
        else None,
    )


def record_repair_terminal(
    unit: dict[str, Any],
    kind: str,
    repair: Any,
    *,
    status: str,
) -> None:
    """Attach a terminal summary inside the same commit as the result."""
    data = repair if isinstance(repair, dict) else {}
    unit.setdefault("model_repair", {})[kind] = repair_summary_payload(
        invocation_id=data.get("invocation_id") or "",
        status=status,
        round_no=data.get("round"),
        max_rounds=data.get("max_rounds"),
        api_calls=data.get("api_calls"),
        success_round=data.get("success_round"),
        errors=data.get("errors"),
        source_sha256=unit.get("source_sha256"),
        translation_revision=unit.get("translation_revision")
        if kind == "review"
        else None,
    )


def recompute_unit_stats(state: dict[str, Any]) -> None:
    units = state.get("units", [])
    counts = {
        "total": len(units),
        "pending": sum(item.get("status") == "pending" for item in units),
        "active": sum(item.get("status") in ACTIVE_STATUSES for item in units),
        "waiting": sum(item.get("status") in WAITING_STATUSES for item in units),
        "cancelled": sum(item.get("status") == "cancelled" for item in units),
        "passed": sum(item.get("status") == "passed" for item in units),
        "user_modified": sum(item.get("status") == "user_modified" for item in units),
        "needs_action": sum(item.get("status") == "needs_action" for item in units),
        "accepted_risk": sum(item.get("status") == "accepted_risk" for item in units),
        "failed": sum(item.get("status") == "failed" for item in units),
    }
    counts["done"] = counts["passed"] + counts["user_modified"] + counts["accepted_risk"]
    counts["progress_percent"] = round((counts["done"] / counts["total"]) * 100, 1) if counts["total"] else 0
    state["stats"] = counts


def snapshot_with_unit_stats(state: dict[str, Any]) -> dict[str, Any]:
    recompute_unit_stats(state)
    return copy.deepcopy(state)
