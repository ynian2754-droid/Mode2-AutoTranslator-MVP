"""In-memory execution resources and invocation ownership for one project."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from core.project_state import ProjectStateCell


@dataclass
class ExecutionRuntime:
    executor: ThreadPoolExecutor | None = None
    executor_max_concurrency: int | None = None
    active_unit_ids: set[str] = field(default_factory=set)
    active_futures: dict[str, Future[Any]] = field(default_factory=dict)
    active_task_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    run_cancel_events: dict[str, threading.Event] = field(default_factory=dict)
    stop_timers: dict[str, threading.Timer] = field(default_factory=dict)
    retired_executors: dict[str, ThreadPoolExecutor] = field(default_factory=dict)
    # In-memory invocation ownership rejects superseded notifications.
    active_invocations: dict[tuple[str, str], str] = field(default_factory=dict)

    def has_live_tasks(self) -> bool:
        return bool(self.active_unit_ids or self.active_task_meta or self.retired_executors)


class InvocationTracker:
    """Caller-held project lock guards these four narrow operations."""

    def __init__(self, cell: ProjectStateCell, runtime: ExecutionRuntime,
                 invocation_id_factory: Callable[[], str]) -> None:
        self.cell = cell
        self.runtime = runtime
        self.invocation_id_factory = invocation_id_factory

    def cancel_requested_locked(self, run_id: str) -> bool:
        run = self.cell.state.get("run") or {}
        if run.get("run_id") == run_id and run.get("cancel_requested"):
            return True
        cancel_event = self.runtime.run_cancel_events.get(run_id)
        return bool(cancel_event and cancel_event.is_set())

    def begin_locked(self, unit_id: str, kind: str) -> str:
        invocation_id = self.invocation_id_factory()
        self.runtime.active_invocations[(str(unit_id), kind)] = invocation_id
        return invocation_id

    def end_locked(self, unit_id: str, kind: str, invocation_id: str) -> None:
        key = (str(unit_id), kind)
        if self.runtime.active_invocations.get(key) == invocation_id:
            self.runtime.active_invocations.pop(key, None)

    def is_current_locked(self, unit_id: str, kind: str, invocation_id: str) -> bool:
        return self.runtime.active_invocations.get((str(unit_id), kind)) == invocation_id

