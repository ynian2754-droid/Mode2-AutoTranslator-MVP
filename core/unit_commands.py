"""Explicit user commands over persisted translation drafts and decisions."""

from __future__ import annotations

import copy
from typing import Any, Callable

from core import execution, pipeline_output, project_state, unit_state, unit_validation
from core.exceptions import ConflictError, PipelineError
from core.execution_runtime import ExecutionRuntime
from core.project_state import ProjectStateCell
from core.unit_state import (
    ACTION_STATUSES, EDITABLE_TRANSLATION_STATUSES, REVIEWABLE_TRANSLATION_STATUSES,
)


class UnitCommands:
    """Own command transactions; enqueue uses the scheduler's narrow locked entry."""

    def __init__(self, cell: ProjectStateCell, runtime: ExecutionRuntime,
                 start_job_locked: Callable[[list[str], str], dict[str, Any]],
                 clock: Callable[[], str]) -> None:
        self.cell = cell
        self.runtime = runtime
        self.start_job_locked = start_job_locked
        self.clock = clock

    def save_translation(
        self,
        unit_id: str,
        translation: str,
        *,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist one user-edited translation without invoking either provider."""

        if not isinstance(translation, str) or not translation.strip():
            raise PipelineError("人工译文不能为空。")
        with self.cell.lock:
            project_state.ensure_open(self.cell)
            if unit_id in self.runtime.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.cell.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许编辑正式译文。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            pipeline_output.invalidate_output(self.cell.state)
            clean_translation = translation.strip()
            unit["translation"] = clean_translation
            unit["user_edited_translation"] = clean_translation
            unit["translation_revision"] += 1
            # Saving is deliberately independent from review.  Keep the last
            # review payload for traceability, but its revision now makes it
            # clear that it is not a review of this newly saved version.
            unit["pending_translation_feedback"] = None
            unit["user_decision"] = "edit"
            if unit.get("status") == "passed":
                unit["status"] = "user_modified"
                unit["last_error"] = None
            elif unit.get("status") == "user_modified":
                unit["last_error"] = None
            elif unit.get("status") == "accepted_risk":
                # Keep the explicit user risk-acceptance state visible after
                # a later manual edit.  The revision on the retained review
                # payload makes the old review inapplicable to this text;
                # retranslation is the supported path to obtain a new review.
                unit["last_error"] = None
            unit["updated_at"] = self.clock()
            project_state.append_event(self.cell, "user_translation_saved", "人工译文已保存，等待用户明确复检。", unit_id, {}, self.clock)
            unit_state.recompute_unit_stats(self.cell.state)
            project_state.save_project(self.cell)
            return copy.deepcopy(unit)


    def review_unit(
        self,
        unit_id: str,
        *,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit review of the persisted formal translation."""

        with self.cell.lock:
            project_state.ensure_open(self.cell)
            if unit_id in self.runtime.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.cell.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in REVIEWABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新校验。")
            if not isinstance(unit.get("translation"), str) or not unit["translation"].strip():
                raise PipelineError("没有可供校验的正式译文。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            self.start_job_locked([unit_id], "review")
            return copy.deepcopy(unit_state.find_unit(self.cell.state, unit_id))


    def retranslate_unit(
        self,
        unit_id: str,
        *,
        expected_project_id: str | None = None,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Queue an explicit translation retry using only persisted feedback."""

        with self.cell.lock:
            project_state.ensure_open(self.cell)
            project_state.validate_expected_project_id(self.cell, expected_project_id)
            if unit_id in self.runtime.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.cell.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in EDITABLE_TRANSLATION_STATUSES:
                raise PipelineError("当前状态不允许重新翻译。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            current_revision = unit["translation_revision"]
            # The draft this retry replaces, kept with the revision it belongs to
            # and the review that judged it. Retrying again after a failure
            # carries the *same* group forward: the unit's own `translation` /
            # `review` fields describe the attempt (which the queue renumbers
            # before the model runs), so they must never overwrite it.
            # One-shot: a successful commit drops the whole record; network
            # errors, cancellation, a late result and a failed save all keep it.
            retained = unit_state.retained_previous_draft(unit)
            unit["pending_translation_feedback"] = {
                "source_revision": retained["revision"] if retained else current_revision,
                "suggestions": retained["suggestions"] if retained else [],
                "previous_translation": retained["translation"] if retained else None,
                "previous_review": retained["review"] if retained else None,
            }
            unit["status"] = "pending"
            unit["translation"] = ""
            unit["review"] = None
            unit["review_issues"] = []
            unit["review_suggestions"] = []
            unit["user_decision"] = "retry"
            unit["last_error"] = None
            unit["updated_at"] = self.clock()
            project_state.append_event(self.cell, "user_requested_retry", "用户要求重新翻译并复检。", unit_id, {}, self.clock)
            self.start_job_locked([unit_id], "translation")
            return copy.deepcopy(unit_state.find_unit(self.cell.state, unit_id))


    def decide(
        self,
        unit_id: str,
        decision: str,
        *,
        translation: str | None = None,
        expected_source_sha256: str | None = None,
    ) -> dict[str, Any]:
        decision = decision.strip().casefold()
        if decision not in {"edit", "accept-risk", "retry"}:
            raise PipelineError("裁决只能是 edit、accept-risk 或 retry。")
        with self.cell.lock:
            project_state.ensure_open(self.cell)
            if unit_id in self.runtime.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            unit = unit_state.find_unit(self.cell.state, unit_id)
            if unit.get("status") not in ACTION_STATUSES:
                raise PipelineError("只有待裁决的单元才能进行人工裁决。")
            if expected_source_sha256 and expected_source_sha256 != unit["source_sha256"]:
                raise PipelineError("源文已经变化，请刷新后再提交裁决。")
            pipeline_output.invalidate_output(self.cell.state)
            if decision == "accept-risk":
                unit["status"] = "accepted_risk"
                unit["user_decision"] = "accept-risk"
                unit["last_error"] = None
                unit["updated_at"] = self.clock()
                project_state.append_event(self.cell, "user_accepted_risk", "用户选择直接通过并接受当前校验风险。", unit_id, {}, self.clock)
                unit_state.recompute_unit_stats(self.cell.state)
                self.cell.state["run"]["status"] = execution.derived_run_status(self.cell.state)
                project_state.save_project(self.cell)
                return copy.deepcopy(unit)
            if decision == "retry":
                unit_state.ensure_unit_feedback_fields(unit)
                current_revision = unit["translation_revision"]
                review = unit.get("review")
                review_revision = review.get("translation_revision") if isinstance(review, dict) else None
                if review_revision != current_revision:
                    raise PipelineError("当前校验结果与译文版本不一致，不能据此重新翻译。")
                # Same frozen record as the explicit retranslate entry: the
                # previous draft keeps its own revision and its own review, and
                # a repeated failure carries that same group forward instead of
                # re-binding it to this attempt.
                retained = unit_state.retained_previous_draft(unit)
                unit["pending_translation_feedback"] = {
                    "source_revision": retained["revision"] if retained else current_revision,
                    "suggestions": retained["suggestions"] if retained else [],
                    "previous_translation": retained["translation"] if retained else None,
                    "previous_review": retained["review"] if retained else None,
                }
                unit["status"] = "pending"
                unit["translation"] = ""
                unit["review"] = None
                unit["review_issues"] = []
                unit["review_suggestions"] = []
                unit["user_decision"] = "retry"
                unit["last_error"] = None
                unit["updated_at"] = self.clock()
                project_state.append_event(self.cell, "user_requested_retry", "用户要求重新翻译并复检。", unit_id, {}, self.clock)
                self.start_job_locked([unit_id], "translation")
                return copy.deepcopy(unit)
            if not translation or not translation.strip():
                raise PipelineError("修改后的译文不能为空。")
            unit_state.ensure_unit_feedback_fields(unit)
            unit["translation"] = translation.strip()
            unit["translation_revision"] += 1
            unit["pending_translation_feedback"] = None
            unit["review_suggestions"] = []
            unit["status"] = "reviewing"
            unit["user_decision"] = "edit"
            unit["review"] = None
            unit["review_issues"] = []
            unit["last_error"] = None
            unit["updated_at"] = self.clock()
            project_state.append_event(self.cell, "user_submitted_edit", "用户修改译文，重新进入独立校验。", unit_id, {}, self.clock)
            self.start_job_locked([unit_id], "review")
            return copy.deepcopy(unit)

