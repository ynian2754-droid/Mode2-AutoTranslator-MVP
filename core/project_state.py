"""The four shared project resources and their existing basic writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.exceptions import ConflictError
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


def ensure_open(cell: ProjectStateCell) -> None:
    if cell.closed:
        raise ConflictError("当前项目管理器已关闭，不能继续操作。")


def validate_expected_project_id(
    cell: ProjectStateCell,
    expected_project_id: str | None,
) -> None:
    """Reject a stale page binding while holding the manager lock."""
    if expected_project_id is not None and str(expected_project_id) != str(
        cell.state.get("project", {}).get("id") or ""
    ):
        raise ConflictError("请求绑定的项目与当前项目不一致，请刷新后重试。")

