"""Unit feedback and persisted repair-summary normalization rules."""

from __future__ import annotations

import copy
from typing import Any

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

