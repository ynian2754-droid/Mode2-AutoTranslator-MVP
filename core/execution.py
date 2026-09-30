"""Run scheduling, cooperative stopping and scoped worker completion."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from core import pipeline_output, project_state, unit_state
from core.exceptions import ConflictError, PipelineError
from core.execution_runtime import ExecutionRuntime
from core.project_state import ProjectStateCell
from core.unit_state import (
    CANCELLABLE_START_STATUSES,
    PROCESSING_STATUSES,
    REVIEWABLE_TRANSLATION_STATUSES,
    TRANSLATION_PROCESSING_STATUSES,
)
from core.unit_workflow import UnitWorkflow

STOP_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ExecutionFactories:
    executor: Callable[..., Any]
    event: Callable[[], Any]
    timer: Callable[..., Any]
    run_id: Callable[[], str]
    stop_grace: Callable[[], float]


def derived_run_status(state: dict[str, Any]) -> str:
    stats = state.get("stats") or {}
    if stats.get("needs_action", 0):
        return "needs_action"
    if stats.get("pending", 0) or stats.get("waiting", 0) or stats.get("active", 0):
        return "ready"
    if stats.get("cancelled", 0):
        return "ready"
    if stats.get("total", 0) and stats.get("done", 0) == stats.get("total", 0):
        return "completed"
    return "ready"


class ExecutionScheduler:
    def __init__(self, cell: ProjectStateCell, runtime: ExecutionRuntime,
                 workflow: UnitWorkflow, clock: Callable[[], str],
                 factories: ExecutionFactories) -> None:
        self.cell = cell
        self.runtime = runtime
        self.workflow = workflow
        self.clock = clock
        self.factories = factories

    def ensure_executor_locked(self) -> ThreadPoolExecutor:
        project_state.ensure_open(self.cell)
        run = self.cell.state.get("run") or {}
        configured = run.get("max_concurrency") if run.get("running") else None
        max_workers = int(configured or self.cell.state["config"].get("max_concurrency") or 3)
        if self.runtime.executor is not None and self.runtime.executor_max_concurrency != max_workers:
            if self.runtime.active_unit_ids:
                raise ConflictError("当前流水线正在运行，不能切换任务调度器的并发数。")
            self.runtime.executor.shutdown(wait=True)
            self.runtime.executor = None
            self.runtime.executor_max_concurrency = None
        if self.runtime.executor is None:
            self.runtime.executor = self.factories.executor(
                max_workers=max_workers,
                thread_name_prefix="mode2-worker",
            )
            self.runtime.executor_max_concurrency = max_workers
        return self.runtime.executor


    def close_executor_locked(self) -> None:
        if self.runtime.active_unit_ids:
            raise ConflictError("当前项目仍有单元在运行，不能关闭任务调度器。")
        for timer in self.runtime.stop_timers.values():
            timer.cancel()
        self.runtime.stop_timers.clear()
        self.runtime.run_cancel_events.clear()
        executor = self.runtime.executor
        self.runtime.executor = None
        self.runtime.executor_max_concurrency = None
        self.runtime.active_futures.clear()
        if executor is not None:
            executor.shutdown(wait=True)
        retired_executors = list(self.runtime.retired_executors.values())
        self.runtime.retired_executors.clear()
        for retired_executor in retired_executors:
            retired_executor.shutdown(wait=False)


    def begin_run_locked(self, unit_count: int, mode: str) -> str:
        project_state.ensure_open(self.cell)
        run = self.cell.state["run"]
        if not run.get("running"):
            run_id = f"run-{self.factories.run_id()[:10]}"
            cancel_event = self.factories.event()
            self.runtime.run_cancel_events[run_id] = cancel_event
            run.update(
                {
                    "run_id": run_id,
                    "status": "running",
                    "running": True,
                    "max_concurrency": int(self.cell.state["config"].get("max_concurrency") or 3),
                    "started_at": self.clock(),
                    "completed_at": None,
                    "unit_ids": [],
                    "completed_unit_ids": [],
                    "cancel_requested": False,
                    "stop_requested_at": None,
                    "cancelled_at": None,
                    "stop_timeout_at": None,
                }
            )
            project_state.append_event(
                self.cell,
                "run_started",
                f"开始处理 {unit_count} 个单元，并发数 {run['max_concurrency']}。",
                None,
                {"mode": mode},
                self.clock,
            )
            return run_id
        if run.get("cancel_requested"):
            raise ConflictError("当前流水线正在停止，请等待停止完成后再提交任务。")
        run_id = str(run.get("run_id") or f"run-{self.factories.run_id()[:10]}")
        run["run_id"] = run_id
        self.runtime.run_cancel_events.setdefault(run_id, self.factories.event())
        return run_id


    def has_active_tasks_locked(self, run_id: str) -> bool:
        return any(
            task_meta.get("run_id") == run_id
            for task_meta in self.runtime.active_task_meta.values()
        )


    def cancel_stop_timer_locked(self, run_id: str) -> None:
        timer = self.runtime.stop_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()


    def shutdown_retired_executor_locked(self, run_id: str) -> None:
        executor = self.runtime.retired_executors.pop(run_id, None)
        if executor is not None:
            # This is called by a worker's done callback.  Waiting here would
            # make the worker wait for its own executor to shut down.
            executor.shutdown(wait=False)


    def finish_run_if_idle_locked(self, run_id: str) -> None:
        run = self.cell.state["run"]
        if run.get("run_id") != run_id or self.has_active_tasks_locked(run_id):
            return
        expected_unit_ids = {str(unit_id) for unit_id in run.get("unit_ids") or []}
        completed_unit_ids = {str(unit_id) for unit_id in run.get("completed_unit_ids") or []}
        if expected_unit_ids and not expected_unit_ids.issubset(completed_unit_ids):
            return
        if not run.get("running"):
            return
        run["running"] = False
        run["completed_at"] = self.clock()
        if run.get("cancel_requested"):
            run["status"] = "cancelled"
            run["cancelled_at"] = run["completed_at"]
        else:
            run["status"] = derived_run_status(self.cell.state)
        project_state.append_event(
            self.cell,
            "run_finished",
            f"当前任务集合结束：{run['status']}。",
            None,
            {"counts": self.cell.state["stats"]},
            self.clock,
        )
        self.cancel_stop_timer_locked(run_id)
        self.runtime.run_cancel_events.pop(run_id, None)
        self.shutdown_retired_executor_locked(run_id)


    def force_finish_stopping_run(self, run_id: str) -> None:
        """Close the controller state if a provider ignores cooperative stop.

        Python threads cannot be safely killed.  The per-run cancellation event
        remains set so a late provider response is discarded, while the UI and
        scheduler are allowed to move on to a new run for other units.
        """
        with self.cell.lock:
            run = self.cell.state.get("run") or {}
            if run.get("run_id") != run_id or not run.get("running") or not run.get("cancel_requested"):
                return
            active_unit_ids = [
                unit_id
                for unit_id, task_meta in self.runtime.active_task_meta.items()
                if task_meta.get("run_id") == run_id
            ]
            if not active_unit_ids:
                expected_unit_ids = {
                    str(unit_id) for unit_id in run.get("unit_ids") or []
                }
                completed_unit_ids = run.setdefault("completed_unit_ids", [])
                for unit_id in expected_unit_ids.difference(completed_unit_ids):
                    unit = unit_state.find_unit(self.cell.state, unit_id)
                    if unit.get("status") in PROCESSING_STATUSES or unit.get("status") == "pending":
                        unit_state.mark_cancelled(self.cell, unit_id, "停止请求已收尾，未启动的任务已取消。", clock=self.clock)
                    completed_unit_ids.append(unit_id)
                unit_state.recompute_unit_stats(self.cell.state)
                self.finish_run_if_idle_locked(run_id)
                project_state.save_project(self.cell)
                return
            for unit_id in active_unit_ids:
                unit_state.mark_cancelled(self.cell, unit_id, "停止等待超时，已放弃等待该请求。", clock=self.clock)
            completed_at = self.clock()
            run["running"] = False
            run["status"] = "cancelled"
            run["completed_at"] = completed_at
            run["cancelled_at"] = completed_at
            run["stop_timeout_at"] = completed_at
            unit_state.recompute_unit_stats(self.cell.state)
            # Do not let a provider that ignores cancellation occupy the
            # executor needed by the next Run.  Its old workers remain
            # isolated and can only discard their late results.
            if self.runtime.executor is not None:
                self.runtime.retired_executors[run_id] = self.runtime.executor
                self.runtime.executor = None
            project_state.append_event(
                self.cell,
                "run_finished",
                "停止等待超时，流水线已结束；迟到的请求结果将被丢弃。",
                None,
                {"active_unit_count": len(active_unit_ids)},
                self.clock,
            )
            self.cancel_stop_timer_locked(run_id)
            project_state.save_project(self.cell)


    def queue_unit_locked(self, unit_id: str, mode: str, run_id: str) -> None:
        project_state.ensure_open(self.cell)
        if unit_id in self.runtime.active_unit_ids:
            raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
        if mode not in {"translation", "review"}:
            raise PipelineError(f"不支持的任务模式：{mode}")

        unit = unit_state.find_unit(self.cell.state, unit_id)
        unit_state.ensure_unit_feedback_fields(unit)
        previous_revision: int | None = None
        if mode == "translation":
            if unit.get("status") not in CANCELLABLE_START_STATUSES:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新翻译。")
            previous_revision = unit["translation_revision"]
            unit["translation_revision"] = previous_revision + 1
            unit["status"] = "waiting_translation"
            unit["translation_attempts"] += 1
            unit["updated_at"] = self.clock()
            project_state.append_event(self.cell, "translation_queued", "翻译任务已进入共享调度器。", unit_id, {}, self.clock)
        else:
            if unit.get("status") not in {"reviewing", *REVIEWABLE_TRANSLATION_STATUSES}:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新校验。")
            unit["status"] = "waiting_review"
            unit["updated_at"] = self.clock()
            project_state.append_event(self.cell, "review_queued", "校验任务已进入共享调度器。", unit_id, {}, self.clock)

        self.runtime.active_unit_ids.add(unit_id)
        self.cell.state["run"].setdefault("unit_ids", [])
        if unit_id not in self.cell.state["run"]["unit_ids"]:
            self.cell.state["run"]["unit_ids"].append(unit_id)
        self.runtime.active_task_meta[unit_id] = {
            "mode": mode,
            "translation_revision": unit.get("translation_revision"),
            "run_id": run_id,
        }
        executor = self.ensure_executor_locked()
        try:
            future = executor.submit(self.workflow.execute, unit_id, mode, run_id)
            self.runtime.active_futures[unit_id] = future
            future.add_done_callback(
                lambda completed, unit_id=unit_id, run_id=run_id: self.task_finished(
                    unit_id,
                    run_id,
                    completed,
                )
            )
        except Exception as exc:
            self.runtime.active_unit_ids.discard(unit_id)
            self.runtime.active_futures.pop(unit_id, None)
            self.runtime.active_task_meta.pop(unit_id, None)
            if previous_revision is not None:
                unit["translation_revision"] = previous_revision
            unit_state.mark_failure(self.cell, unit_id, "scheduler_error", f"任务入队失败：{exc}", clock=self.clock)
            raise PipelineError(f"任务入队失败：{exc}") from exc


    def start_job_locked(self, unit_ids: list[str], mode: str) -> dict[str, Any]:
        project_state.ensure_open(self.cell)
        unit_ids = list(dict.fromkeys(str(unit_id) for unit_id in unit_ids))
        if not unit_ids:
            return unit_state.snapshot_with_unit_stats(self.cell.state)
        for unit_id in unit_ids:
            unit = unit_state.find_unit(self.cell.state, unit_id)
            if unit_id in self.runtime.active_unit_ids:
                raise ConflictError(f"翻译单元 {unit_id} 正在处理中，请等待当前任务结束。")
            if mode == "translation" and unit.get("status") not in CANCELLABLE_START_STATUSES:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新翻译。")
            if mode == "review" and unit.get("status") not in {"reviewing", *REVIEWABLE_TRANSLATION_STATUSES}:
                raise PipelineError(f"翻译单元 {unit_id} 当前不能重新校验。")
        pipeline_output.invalidate_output(self.cell.state)
        was_running = bool(self.cell.state["run"].get("running"))
        run_id = self.begin_run_locked(len(unit_ids), mode)
        # Freeze the scope before submitting any Future.  A completion callback
        # must never be able to finish a run while the remaining units are
        # still being submitted.
        if not was_running:
            self.cell.state["run"]["unit_ids"] = list(unit_ids)
            self.cell.state["run"]["completed_unit_ids"] = []
        else:
            existing_unit_ids = self.cell.state["run"].setdefault("unit_ids", [])
            for unit_id in unit_ids:
                if unit_id not in existing_unit_ids:
                    existing_unit_ids.append(unit_id)
        for unit_id in unit_ids:
            self.queue_unit_locked(unit_id, mode, run_id)
        project_state.save_project(self.cell)
        return unit_state.snapshot_with_unit_stats(self.cell.state)


    def start(self, unit_ids: list[str] | None = None) -> dict[str, Any]:
        with self.cell.lock:
            project_state.ensure_open(self.cell)
            if self.cell.state["run"].get("running"):
                raise ConflictError("当前流水线仍在运行，请等待结束或先停止。")
            if unit_ids is None:
                selected_ids = [
                    unit["id"]
                    for unit in self.cell.state["units"]
                    if unit.get("status") in CANCELLABLE_START_STATUSES
                    and unit["id"] not in self.runtime.active_unit_ids
                ]
            else:
                selected_ids = list(dict.fromkeys(str(unit_id) for unit_id in unit_ids))
                if not selected_ids:
                    raise PipelineError("请至少选择一个可翻译的处理单元。")
            return self.start_job_locked(selected_ids, "translation")


    def stop(self) -> dict[str, Any]:
        """Request a cooperative stop for the current run.

        Provider calls already in flight are allowed to return, but their
        results are discarded and no following pipeline stage is started.
        """
        with self.cell.lock:
            project_state.ensure_open(self.cell)
            run = self.cell.state["run"]
            if not run.get("running"):
                return unit_state.snapshot_with_unit_stats(self.cell.state)
            if not run.get("cancel_requested"):
                run["cancel_requested"] = True
                run["status"] = "stopping"
                run["stop_requested_at"] = self.clock()
                run_id = str(run.get("run_id") or "")
                cancel_event = self.runtime.run_cancel_events.get(run_id)
                if cancel_event is not None:
                    cancel_event.set()
                project_state.append_event(self.cell, "run_stop_requested", "用户请求停止当前流水线。", None, {}, self.clock)
                timer = self.factories.timer(
                    self.factories.stop_grace(),
                    self.force_finish_stopping_run,
                    args=(run_id,),
                )
                timer.daemon = True
                self.runtime.stop_timers[run_id] = timer
                timer.start()

            # Mark every active unit first. This prevents a provider callback
            # from committing a result while the stop request is being handled.
            for unit_id in list(run.get("unit_ids") or []):
                if unit_id not in self.runtime.active_unit_ids:
                    continue
                unit = unit_state.find_unit(self.cell.state, unit_id)
                task_meta = self.runtime.active_task_meta.get(unit_id) or {}
                if (
                    task_meta.get("mode") == "translation"
                    and unit.get("status") in TRANSLATION_PROCESSING_STATUSES
                    and unit.get("translation_revision") == task_meta.get("translation_revision")
                ):
                    unit["translation_revision"] = max(0, int(unit["translation_revision"]) - 1)
                if unit.get("status") in PROCESSING_STATUSES:
                    unit_state.mark_cancelled(self.cell, unit_id, clock=self.clock)

            # Futures which have not started never enter provider code.
            for future in list(self.runtime.active_futures.values()):
                future.cancel()

            unit_state.recompute_unit_stats(self.cell.state)
            self.finish_run_if_idle_locked(str(run.get("run_id") or ""))
            project_state.save_project(self.cell)
            return unit_state.snapshot_with_unit_stats(self.cell.state)


    def task_finished(self, unit_id: str, run_id: str, future: Future[Any]) -> None:
        with self.cell.lock:
            self.runtime.active_unit_ids.discard(unit_id)
            self.runtime.active_futures.pop(unit_id, None)
            self.runtime.active_task_meta.pop(unit_id, None)
            run = self.cell.state.get("run") or {}
            if run.get("run_id") == run_id:
                completed_unit_ids = run.setdefault("completed_unit_ids", [])
                if unit_id not in completed_unit_ids:
                    completed_unit_ids.append(unit_id)
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - final safety net
                try:
                    unit = unit_state.find_unit(self.cell.state, unit_id)
                except PipelineError:
                    unit = None
                if unit is not None and unit.get("status") in PROCESSING_STATUSES:
                    unit_state.mark_failure(self.cell, unit_id, "worker_error", f"工作器异常：{exc}", clock=self.clock)

            unit_state.recompute_unit_stats(self.cell.state)
            self.finish_run_if_idle_locked(run_id)
            current_run = self.cell.state.get("run") or {}
            if (
                not self.has_active_tasks_locked(run_id)
                and (
                    current_run.get("run_id") != run_id
                    or not current_run.get("running")
                )
            ):
                self.cancel_stop_timer_locked(run_id)
                self.runtime.run_cancel_events.pop(run_id, None)
                self.shutdown_retired_executor_locked(run_id)
            project_state.save_project(self.cell)
