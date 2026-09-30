"""Quality-support transactions over the shared project state cell.

Callers hold the cell's original lock. This module owns the quality-support
and event-log rollback boundary; it neither creates locks nor calls models.
"""

from __future__ import annotations

import copy
from typing import Any

from core.project_state import ProjectStateCell, save_project


def quality_unit_sources(units: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    return {
        str(unit.get("id")): (str(unit.get("source") or ""), str(unit.get("source_sha256") or ""))
        for unit in units
        if isinstance(unit, dict) and unit.get("id")
    }


def commit_quality_support(
    cell: ProjectStateCell,
    support: dict[str, Any],
    *,
    old_support: Any,
    old_events: list[Any],
) -> None:
    """Swap in the mutated support and persist it atomically.

    If ``ProjectStore.save`` fails, the in-memory state and the event log
    are rolled back to the last committed version so memory, disk, the
    effective reference version and every later read stay consistent.
    """

    cell.state["quality_support"] = support
    try:
        save_project(cell)
    except Exception:
        cell.state["quality_support"] = copy.deepcopy(old_support)
        events = cell.state.get("events")
        if isinstance(events, list):
            # _event_locked keeps a bounded log and may have trimmed the
            # oldest event before save() failed.  Truncating by the old
            # length therefore cannot restore the real pre-transaction
            # state; replace the full list instead.
            events[:] = copy.deepcopy(old_events)
        else:
            cell.state["events"] = copy.deepcopy(old_events)
        raise

