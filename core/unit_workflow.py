"""Unit translation/review workflow with its original lock and commit boundaries."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Callable, Protocol

from core import project_state, unit_requests, unit_state, unit_validation
from core.exceptions import PipelineError
from core.project_state import ProjectStateCell
from core.provider_routing import ProviderRouter
from core.unit_requests import UnitRequests
from providers.repair_loop import RepairControl, RepairProgress


class InvocationPort(Protocol):
    def cancel_requested_locked(self, run_id: str) -> bool: ...
    def begin_locked(self, unit_id: str, kind: str) -> str: ...
    def end_locked(self, unit_id: str, kind: str, invocation_id: str) -> None: ...
    def is_current_locked(self, unit_id: str, kind: str, invocation_id: str) -> bool: ...


class UnitWorkflow:
    def __init__(self, cell: ProjectStateCell, requests: UnitRequests,
                 providers: ProviderRouter, invocations: InvocationPort,
                 clock: Callable[[], str]) -> None:
        self.cell = cell
        self.requests = requests
        self.providers = providers
        self.invocations = invocations
        self.clock = clock

    def execute(self, unit_id: str, mode: str, run_id: str) -> None:
        with self.cell.lock:
            if self.invocations.cancel_requested_locked(run_id):
                unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                project_state.save_project(self.cell)
                return
        if mode == "translation":
            self.translate_and_review(unit_id, run_id)
        else:
            self.review(unit_id, run_id)


    def translate_and_review(self, unit_id: str, _run_id: str) -> None:
        with self.cell.lock:
            unit = unit_state.find_unit(self.cell.state, unit_id)
            if unit.get("status") != "waiting_translation":
                return
            if self.invocations.cancel_requested_locked(_run_id):
                unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                project_state.save_project(self.cell)
                return
            unit["status"] = "translating"
            unit["updated_at"] = self.clock()
            request, snapshot = self.requests.translation_locked(unit)
            source_sha256 = unit["source_sha256"]
            invocation_id = self.invocations.begin_locked(unit_id, "translation")
            request = replace(
                request,
                control=self._repair_control_locked(
                    unit_id,
                    "translation",
                    invocation_id,
                    _run_id,
                    source_sha256=source_sha256,
                ),
            )
            project_state.append_event(self.cell, "translation_started", "翻译端已接收单元。", unit_id, {}, self.clock)
            project_state.save_project(self.cell)
        try:
            translator, _reviewer = self.providers.unit_pair()
            result = translator.translate(request)
            with self.cell.lock:
                if self.invocations.cancel_requested_locked(_run_id):
                    self.invocations.end_locked(unit_id, "translation", invocation_id)
                    unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                    project_state.save_project(self.cell)
                    return
            unit_validation.validate_translation_result(unit, result)
        except Exception as exc:
            with self.cell.lock:
                self.invocations.end_locked(unit_id, "translation", invocation_id)
                cancelled = self.invocations.cancel_requested_locked(_run_id)
                if cancelled:
                    unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                else:
                    unit_state.mark_failure(self.cell, unit_id, "translation_error", str(exc), clock=self.clock)
                unit_state.record_repair_failure(
                    self.cell.state,
                    unit_id,
                    "translation",
                    invocation_id,
                    exc,
                    source_sha256=source_sha256,
                    translation_revision=None,
                    status="cancelled" if cancelled else "failed",
                )
                project_state.save_project(self.cell)
            return

        with self.cell.lock:
            unit = unit_state.find_unit(self.cell.state, unit_id)
            self.invocations.end_locked(unit_id, "translation", invocation_id)
            if self.invocations.cancel_requested_locked(_run_id):
                unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                project_state.save_project(self.cell)
                return
            commit_snapshot = copy.deepcopy(unit)
            events_snapshot = list(self.cell.state.get("events") or [])
            unit["translation"] = result.translated_text
            # Feedback is one-shot: a successful translation consumes it before review.
            unit["pending_translation_feedback"] = None
            # The new AI result supersedes any saved manual reference.  Keep
            # this mutation in the same commit as the translation so failures
            # and cancellations leave the old reference intact.
            unit["user_edited_translation"] = None
            unit["translation_provider"] = result.provider
            unit["translation_model"] = result.model
            unit["usage"] = result.usage
            unit["status"] = "waiting_review"
            unit["last_error"] = None
            unit["updated_at"] = self.clock()
            # The snapshot is bound to the revision this translation created.
            unit_requests.store_reference(unit, snapshot, kind="translation", clock=self.clock)
            if result.repair is not None:
                # Same protected commit as the translation itself: success is
                # never recorded before the result is safely stored.
                unit_state.record_repair_terminal(
                    unit, "translation", result.repair, status="succeeded"
                )
            project_state.append_event(self.cell, "translation_imported", "翻译结果已通过严格导入，进入独立校验。", unit_id, {}, self.clock)
            try:
                project_state.save_project(self.cell)
            except Exception as exc:
                # The result was never stored: roll the unit back to its
                # pre-commit business state (previous translation, manual
                # reference and quality reference) and report the commit
                # failure through the existing failure path.  No model re-call.
                unit_state.restore_unit_commit(self.cell.state, unit_id, commit_snapshot, events_snapshot)
                unit_state.mark_failure(self.cell, unit_id, "save_error", f"翻译结果保存失败：{exc}", clock=self.clock)
                unit_state.mark_repair_save_failed(unit, "translation", result.repair)
                return
        self.review(unit_id, _run_id, reference=snapshot)


    def review(
        self,
        unit_id: str,
        _run_id: str,
        reference: dict[str, Any] | None = None,
    ) -> None:
        with self.cell.lock:
            unit = unit_state.find_unit(self.cell.state, unit_id)
            unit_state.ensure_unit_feedback_fields(unit)
            if unit.get("status") not in {"waiting_review", "needs_action"}:
                return
            if self.invocations.cancel_requested_locked(_run_id):
                unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                project_state.save_project(self.cell)
                return
            if not unit.get("translation"):
                unit_state.mark_failure(self.cell, unit_id, "empty_translation", "没有可供校验的译文。", clock=self.clock)
                project_state.save_project(self.cell)
                return
            unit["status"] = "reviewing"
            unit["updated_at"] = self.clock()
            request = self.requests.review_locked(unit, reference)
            review_revision = unit["translation_revision"]
            source_sha256 = unit["source_sha256"]
            invocation_id = self.invocations.begin_locked(unit_id, "review")
            request = replace(
                request,
                control=self._repair_control_locked(
                    unit_id,
                    "review",
                    invocation_id,
                    _run_id,
                    source_sha256=source_sha256,
                    translation_revision=review_revision,
                ),
            )
            project_state.append_event(self.cell, "review_started", "独立校验端已接收译文。", unit_id, {}, self.clock)
            project_state.save_project(self.cell)
        try:
            _translator, reviewer = self.providers.unit_pair()
            result = reviewer.review(request)
            with self.cell.lock:
                if self.invocations.cancel_requested_locked(_run_id):
                    self.invocations.end_locked(unit_id, "review", invocation_id)
                    unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                    project_state.save_project(self.cell)
                    return
                current = unit_state.find_unit(self.cell.state, unit_id)
                if current.get("translation_revision") != review_revision:
                    raise PipelineError("校验结果对应的译文版本已经变化，已拒绝导入。")
                unit_validation.validate_review_result(current, result)
        except Exception as exc:
            with self.cell.lock:
                self.invocations.end_locked(unit_id, "review", invocation_id)
                cancelled = self.invocations.cancel_requested_locked(_run_id)
                if cancelled:
                    unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                else:
                    unit_state.mark_failure(self.cell, unit_id, "review_error", str(exc), clock=self.clock)
                unit_state.record_repair_failure(
                    self.cell.state,
                    unit_id,
                    "review",
                    invocation_id,
                    exc,
                    source_sha256=source_sha256,
                    translation_revision=review_revision,
                    status="cancelled" if cancelled else "failed",
                )
                project_state.save_project(self.cell)
            return

        with self.cell.lock:
            unit = unit_state.find_unit(self.cell.state, unit_id)
            self.invocations.end_locked(unit_id, "review", invocation_id)
            if self.invocations.cancel_requested_locked(_run_id):
                unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)
                project_state.save_project(self.cell)
                return
            commit_snapshot = copy.deepcopy(unit)
            events_snapshot = list(self.cell.state.get("events") or [])
            review_suggestions = unit_state.extract_review_suggestions(result.issues)
            unit["review_attempts"] += 1
            unit["review"] = {
                "verdict": result.verdict,
                "issues": result.issues,
                "metrics": result.metrics,
                "provider": result.provider,
                "model": result.model,
                "translation_revision": unit["translation_revision"],
                "at": self.clock(),
            }
            unit["review_issues"] = result.issues
            unit["review_suggestions"] = review_suggestions
            unit["last_error"] = None if result.verdict == "PASS" else "独立校验未通过，等待用户裁决。"
            unit["status"] = "passed" if result.verdict == "PASS" else "needs_action"
            unit["updated_at"] = self.clock()
            if result.repair is not None:
                # A valid FAIL still completes the model execution normally; the
                # verdict is never rewritten to PASS by the repair loop.
                unit_state.record_repair_terminal(
                    unit, "review", result.repair, status="succeeded"
                )
            if result.verdict == "PASS":
                project_state.append_event(self.cell, "review_passed", "独立校验通过。", unit_id, {}, self.clock)
            else:
                project_state.append_event(self.cell, "review_failed", "独立校验未通过，已交给用户处理。", unit_id, {}, self.clock)
            try:
                project_state.save_project(self.cell)
            except Exception as exc:
                # Same rule as the translation commit: an unstored verdict —
                # including a valid PASS — is not a result.  Roll back and
                # report through the existing failure path instead of leaving
                # a "passed" unit that the next save would publish.
                unit_state.restore_unit_commit(self.cell.state, unit_id, commit_snapshot, events_snapshot)
                unit_state.mark_failure(self.cell, unit_id, "save_error", f"校验结果保存失败：{exc}", clock=self.clock)
                unit_state.mark_repair_save_failed(unit, "review", result.repair)
                return


    def _repair_control_locked(
        self,
        unit_id: str,
        kind: str,
        invocation_id: str,
        run_id: str,
        *,
        source_sha256: str,
        translation_revision: int | None = None,
    ) -> RepairControl:
        """Bind one execution's hook; every check re-reads live state under the lock."""

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.cell.lock:
                unit = unit_state.find_unit(self.cell.state, unit_id)
                if self.invocations.cancel_requested_locked(run_id):
                    raise PipelineError("已取消，不再发起下一轮模型修正。")
                if not self.invocations.is_current_locked(unit_id, kind, invocation_id):
                    raise PipelineError("该次执行已被更新的执行取代，不再发起下一轮模型修正。")
                if unit.get("source_sha256") != source_sha256:
                    raise PipelineError("源文已变化，不再发起下一轮模型修正。")
                if translation_revision is not None and unit.get("translation_revision") != translation_revision:
                    raise PipelineError("译文版本已变化，不再发起下一轮模型修正。")

        def on_progress(progress: RepairProgress) -> None:
            with self.cell.lock:
                self._apply_repair_progress_locked(
                    unit_id,
                    kind,
                    invocation_id,
                    run_id,
                    source_sha256=source_sha256,
                    translation_revision=translation_revision,
                    progress=progress,
                )

        return RepairControl(
            invocation_id=invocation_id,
            kind=kind,
            before_attempt=before_attempt,
            on_progress=on_progress,
        )


    def _apply_repair_progress_locked(
        self,
        unit_id: str,
        kind: str,
        invocation_id: str,
        run_id: str,
        *,
        source_sha256: str,
        translation_revision: int | None,
        progress: RepairProgress,
    ) -> None:
        """Publish an in-flight round only when it still belongs to this execution."""
        if progress.kind != kind or progress.invocation_id != invocation_id:
            return
        if not self.invocations.is_current_locked(unit_id, kind, invocation_id):
            return
        if self.invocations.cancel_requested_locked(run_id):
            return
        unit = unit_state.find_unit(self.cell.state, unit_id)
        if unit.get("source_sha256") != source_sha256:
            return
        if translation_revision is not None and unit.get("translation_revision") != translation_revision:
            return
        # In-memory only: a transient round must never be persisted, so a restart
        # cannot display it as still running.
        unit.setdefault("model_repair", {})[kind] = unit_state.repair_summary_payload(
            invocation_id=invocation_id,
            status=progress.status if progress.status in {"running", "repairing"} else "failed",
            round_no=progress.round,
            max_rounds=progress.max_rounds,
            api_calls=progress.api_calls,
            success_round=None,
            errors=progress.errors,
            source_sha256=source_sha256,
            translation_revision=translation_revision,
        )
