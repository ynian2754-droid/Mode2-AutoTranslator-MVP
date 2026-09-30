"""Live prepare progress with the original project lock and quality runtime."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from core import concept_automation
from core.project_state import ProjectStateCell
from core.quality_runtime import QualityRuntime
from core.quality_support import normalize_quality_support
from providers.repair_loop import RepairProgress


class PrepareProgress:
    def __init__(
        self,
        cell: ProjectStateCell,
        runtime: QualityRuntime,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.runtime = runtime
        self.clock = clock

    @staticmethod
    def refresh_stage(stage: dict[str, Any]) -> None:
        total = stage.get("total")
        if total == 0:
            stage["pending"] = 0
            if stage.get("state") not in {"reused", "not_required"}:
                stage["state"] = "not_required"
            return
        if total is None:
            stage["pending"] = None
        else:
            accounted = sum(
                int(stage.get(key) or 0)
                for key in ("completed", "failed", "running", "reused", "not_required")
            )
            stage["pending"] = max(0, int(total) - accounted)
        if int(stage.get("running") or 0) > 0:
            stage["state"] = "running"
        elif stage.get("pending") is None:
            stage["state"] = "partial" if any(
                int(stage.get(key) or 0) for key in ("completed", "failed", "reused", "not_required")
            ) else "pending"
        elif int(stage.get("pending") or 0) > 0:
            stage["state"] = "partial" if any(
                int(stage.get(key) or 0) for key in ("completed", "failed", "reused", "not_required")
            ) else "pending"
        elif int(stage.get("failed") or 0):
            stage["state"] = "partial" if int(stage.get("completed") or 0) else "failed"
        elif int(stage.get("reused") or 0) and not int(stage.get("completed") or 0):
            stage["state"] = "reused"
        elif int(stage.get("not_required") or 0) == int(total or 0):
            stage["state"] = "not_required"
        else:
            stage["state"] = "complete"

    def begin_locked(
        self,
        prepare_id: str,
        prepared: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> None:
        batches = list(prepared.get("batches") or [])
        reused_units = len(prepared.get("reused_units") or [])
        stages: dict[str, dict[str, Any]] = {}
        for name, unit, total in (
            ("generation", "batch", len(batches)),
            ("check", "batch", len(batches)),
            ("recheck", "card", None),
            ("lookup", "request", None),
            ("group_resolution", "group", None),
            ("local_resolution", "unit", None),
            ("commit", "commit", 1),
        ):
            row: dict[str, Any] = {
                "state": "pending",
                "completed": 0,
                "total": total,
                "running": 0,
                "failed": 0,
                "reused": 0,
                "not_required": 0,
                "pending": total,
                "unit": unit,
            }
            if total == 0:
                row["state"] = "reused" if name in {"generation", "check"} and reused_units else "not_required"
            if name in {"generation", "check"}:
                row["reused_units"] = reused_units
            stages[name] = row
        stamp = str(record.get("started_at") or self.clock())
        self.runtime.progress = {
            "prepare_id": prepare_id,
            "project_id": str(self.cell.state.get("project", {}).get("id") or ""),
            "status": "running",
            "active": True,
            "progress_revision": 1,
            "started_at": stamp,
            "updated_at": stamp,
            "stages": stages,
            "_active": {},
            "_waiting_commit": {},
            "_item_states": {},
            "_recent_activity": [],
            "_errors": [],
            "provider_invocations": {"generation": 0, "check": 0, "resolution": 0},
        }

    def change(
        self,
        prepare_id: str,
        stage_name: str,
        item_id: str,
        event: str,
        *,
        unit: str = "item",
        weight: int = 1,
        total: int | None = None,
        label: str = "",
        metadata: Mapping[str, Any] | None = None,
        error: str = "",
        repair: RepairProgress | None = None,
        provider_channel: str = "",
    ) -> None:
        """Best-effort in-memory progress notification, never business state."""

        try:
            with self.cell.lock:
                progress = self.runtime.progress
                if (
                    not isinstance(progress, dict)
                    or str(progress.get("prepare_id") or "") != str(prepare_id)
                    or not progress.get("active")
                ):
                    return
                stages = progress.get("stages")
                if not isinstance(stages, dict):
                    return
                stage = stages.get(stage_name)
                if not isinstance(stage, dict):
                    stage = {
                        "state": "pending", "completed": 0, "total": None,
                        "running": 0, "failed": 0, "reused": 0,
                        "not_required": 0, "pending": None, "unit": unit,
                    }
                    stages[stage_name] = stage
                stage["unit"] = unit
                if total is not None:
                    stage["total"] = max(0, int(total))
                key = f"{stage_name}:{item_id}"
                active = progress["_active"]
                item_states = progress["_item_states"]
                recent = progress["_recent_activity"]
                stamp = self.clock()
                if event == "configure":
                    for name, value in (metadata or {}).items():
                        stage[name] = copy.deepcopy(value)
                elif event == "start":
                    if key in active:
                        return
                    if item_states.get(key) in {"complete", "failed", "reused", "not_required"}:
                        return
                    item_weight = max(0, int(weight))
                    activity = {
                        "id": str(item_id),
                        "stage": stage_name,
                        "status": "running",
                        "started_at": stamp,
                        "label": str(label or stage_name),
                        "unit": unit,
                        "affected_count": item_weight,
                    }
                    if metadata:
                        activity.update(copy.deepcopy(dict(metadata)))
                    active[key] = activity
                    item_states[key] = "running"
                    stage["running"] = int(stage.get("running") or 0) + item_weight
                    if provider_channel:
                        calls = progress["provider_invocations"]
                        calls[provider_channel] = int(calls.get(provider_channel) or 0) + 1
                    recent.append(dict(activity))
                    if len(recent) > 40:
                        del recent[:-40]
                elif event == "repair":
                    activity = active.get(key)
                    if activity is None or repair is None:
                        return
                    activity.update(
                        {
                            "attempt_round": int(repair.round),
                            "max_rounds": int(repair.max_rounds),
                            "api_calls_reported": int(repair.api_calls),
                        }
                    )
                    for row in reversed(recent):
                        if row.get("stage") == stage_name and row.get("id") == str(item_id):
                            row.update(
                                {
                                    "attempt_round": int(repair.round),
                                    "max_rounds": int(repair.max_rounds),
                                    "api_calls_reported": int(repair.api_calls),
                                }
                            )
                            break
                else:
                    was_active = key in active
                    activity = active.pop(key, None)
                    if activity is None:
                        activity = progress["_waiting_commit"].pop(key, None)
                    if activity is not None:
                        if was_active:
                            stage["running"] = max(
                                0,
                                int(stage.get("running") or 0)
                                - int(activity.get("affected_count") or 0),
                            )
                        amount = int(activity.get("affected_count") or 1)
                    else:
                        amount = max(0, int(weight))
                    if event in {"complete", "failed", "reused", "not_required"}:
                        count = event == "complete" and "completed" or event
                        stage[count] = int(stage.get(count) or 0) + amount
                        item_states[key] = event
                    elif event == "awaiting_commit":
                        if activity is not None:
                            activity["status"] = "awaiting_commit"
                            activity["updated_at"] = stamp
                            progress["_waiting_commit"][key] = activity
                        item_states[key] = event
                    elif event != "pending":
                        return
                    if activity is not None:
                        activity["status"] = event
                        activity["updated_at"] = stamp
                        if error:
                            activity["error"] = str(error)[:300]
                        for row in reversed(recent):
                            if row.get("stage") == stage_name and row.get("id") == str(item_id):
                                row.update(activity)
                                break
                    if error:
                        progress["_errors"].append(str(error)[:300])
                        if len(progress["_errors"]) > 20:
                            del progress["_errors"][:-20]
                self.refresh_stage(stage)
                progress["updated_at"] = stamp
                progress["progress_revision"] = int(progress.get("progress_revision") or 0) + 1
        except Exception:
            # A progress observer cannot change whether the actual preparation
            # succeeds, fails, or commits.
            return

    def repair_callback(
        self,
        prepare_id: str,
        stage: str,
        item_id: str,
    ):
        def notify(progress: RepairProgress) -> None:
            self.change(
                prepare_id, stage, item_id, "repair", repair=progress
            )

        return notify

    def finish_locked(self, prepare_id: str) -> None:
        progress = self.runtime.progress
        if not isinstance(progress, dict) or str(progress.get("prepare_id") or "") != str(prepare_id):
            return
        active = list((progress.get("_active") or {}).items())
        waiting = list((progress.get("_waiting_commit") or {}).items())
        for _key, activity in active:
            stage = progress.get("stages", {}).get(str(activity.get("stage") or ""))
            if isinstance(stage, dict):
                weight = max(0, int(activity.get("affected_count") or 0))
                stage["running"] = max(0, int(stage.get("running") or 0) - weight)
                stage["failed"] = int(stage.get("failed") or 0) + weight
                self.refresh_stage(stage)
        for _key, activity in waiting:
            stage = progress.get("stages", {}).get(str(activity.get("stage") or ""))
            if isinstance(stage, dict):
                weight = max(0, int(activity.get("affected_count") or 0))
                stage["failed"] = int(stage.get("failed") or 0) + weight
                self.refresh_stage(stage)
        if active:
            progress["_active"].clear()
        if waiting:
            progress["_waiting_commit"].clear()
        try:
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            current = concept_automation.automation_of(support).get("prepare")
            status = str(current.get("status") or "") if isinstance(current, Mapping) else ""
        except Exception:
            status = ""
        if status == "running":
            status = "interrupted" if self.cell.closed else "failed"
        progress["status"] = status or "interrupted"
        progress["active"] = False
        progress["updated_at"] = self.clock()
        progress["progress_revision"] = int(progress.get("progress_revision") or 0) + 1

