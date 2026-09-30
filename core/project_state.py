"""The four shared project resources and their existing basic writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.storage import ProjectStore


@dataclass
class ProjectStateCell:
    state: dict[str, Any]
    lock: Any  # The original threading.RLock instance, shared by every domain.
    store: ProjectStore
    closed: bool


def save_project(cell: ProjectStateCell) -> None:
    # A worker callback can arrive after the session has detached this
    # manager. It may finish in-memory cleanup, but it must never recreate
    # a deleted runtime directory or write project.json again.
    if cell.closed:
        return
    cell.store.save(cell.state)


def append_event(
    cell: ProjectStateCell,
    event_type: str,
    message: str,
    unit_id: str | None,
    details: dict[str, Any],
    clock: Callable[[], str],
) -> None:
    event: dict[str, Any] = {
        "at": clock(),
        "type": event_type,
        "message": message,
    }
    if unit_id:
        event["unit_id"] = unit_id
    if details:
        event["details"] = details
    cell.state.setdefault("events", []).append(event)
    cell.state["events"] = cell.state["events"][-160:]

