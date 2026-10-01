"""Read-only prepare previews, record summaries, and live status projections."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

import mode2_common
from core import concept_automation, quality_prepare_plan, quality_prepare_record
from core.exceptions import ConflictError, PipelineError
from core.project_state import ProjectStateCell, ensure_open, validate_expected_project_id
from core.quality_limits import DEFAULT_ADDITIONAL_WORK_LIMIT, resolve_parallel_batches
from core.quality_queries import QualityQueries
from core.quality_runtime import QualityRuntime
from core.quality_state import quality_unit_sources
from core.quality_support import DEFAULT_SCAN_SOURCE_WORDS, normalize_quality_support, planned_batches


class PrepareViews:
    def __init__(
        self,
        cell: ProjectStateCell,
        runtime: QualityRuntime,
        queries: QualityQueries,
        new_id: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.runtime = runtime
        self.queries = queries
        self.new_id = new_id

    def plan(
        self,
        *,
        unit_ids: list[str] | None,
        current_unit_id: str | None,
        max_source_words: int | None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None,
        expected_revision: int | None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Describe the preparation without any model call or write.

        The preview is deliberately honest about what is *not* known yet: it
        reports how many cards exist, how many are currently eligible, and how
        many groups would need a judgment. The generation/check results — and
        therefore the real candidate counts — only exist after ``execute``.
        """

        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            if concept_automation.reference_mode(self.cell.state.get("project")) != concept_automation.AUTOMATIC_MODE:
                raise PipelineError("当前项目是人工参考模式，请先切换为自动模式。")
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            automation = concept_automation.normalize_automation(support.get("automation"))
            running = automation.get("prepare")
            if isinstance(running, Mapping) and str(running.get("status") or "") == "running":
                raise ConflictError("已有一个准备任务在进行中，请先完成或等待它结束。")
            if self.runtime.prepare_inflight:
                raise ConflictError("已有一个准备任务正在进行中，请等待它结束。")
            if self.runtime.retry_inflight:
                raise ConflictError(
                    f"批次 {sorted(self.runtime.retry_inflight)[0]} 正在恢复中，请等待结束再准备。"
                )
            targets = self.queries.scan_units_locked(
                scope="selected" if unit_ids else ("current" if current_unit_id else "continue"),
                unit_ids=[str(item) for item in (unit_ids or [])],
                current_unit_id=current_unit_id,
            )
            if not targets:
                raise PipelineError("没有可准备的单元。")
            effective_source_words = (
                DEFAULT_SCAN_SOURCE_WORDS if max_source_words is None else int(max_source_words)
            )
            if effective_source_words < 100:
                raise PipelineError("每批源文词数至少为 100。")
            unit_states = quality_prepare_plan.prepare_unit_reuse(automation, targets)
            work_units = [unit for unit in targets if unit_states[str(unit.get("id"))]["state"] != "reused"]
            plan_batches = (
                planned_batches(
                    work_units,
                    max_source_words=effective_source_words,
                    word_counter=mode2_common.english_word_count,
                )
                if work_units
                else {"batches": [], "batch_count": 0}
            )
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            in_scope = [str(unit.get("id")) for unit in targets]
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)
            group_reuse = quality_prepare_plan.prepare_group_reuse(
                support, automation, unit_sources, unit_ids=in_scope
            )
            # Read-only eligibility preview over the cards that already exist.
            # Candidates created by the confirmed execution are judged then.
            existing_eligible = 0
            for card in (support.get("cards") or {}).values():
                if not isinstance(card, dict):
                    continue
                verdict, _reason = concept_automation.adoption_eligibility(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if verdict == "adopt":
                    existing_eligible += 1
            prepare_id = f"prepare-{self.new_id()[:12]}"
            dry_run_required = not bool((support.get("cards") or {}))
            reused_unit_ids = [unit_id for unit_id, state in unit_states.items() if state["state"] == "reused"]
            reused_group_ids = [group_id for group_id, row in group_reuse.items() if row["reused"]]
            local_units_pending = sum(
                int(row.get("local_units_pending") or 0) for row in group_reuse.values()
            )
            local_units_reused = sum(
                int(row.get("local_units_reused") or 0) for row in group_reuse.values()
            )
            pending_group_ids = [
                str(group["group_id"])
                for group in groups
                if str(group["group_id"]) not in reused_group_ids
                and (
                    (len(group["card_ids"]) >= 2 and not group["oversized"])
                    # An oversized group with units still owed is pending work
                    # just like a group that needs a fresh judgment.
                    or int(group_reuse.get(str(group["group_id"]), {}).get("local_units_pending") or 0) > 0
                )
            ]
            budget_limit = int(
                additional_work_limit
                if additional_work_limit is not None
                else DEFAULT_ADDITIONAL_WORK_LIMIT
            )
            return {
                "status": "ok",
                "phase": "plan",
                "prepare_id": prepare_id,
                "prepare_status": "planned",
                "plan": {
                    "prepare_id": prepare_id,
                    "scope": in_scope,
                    "scope_fingerprint": concept_automation.prepare_fingerprint(targets),
                    "batches": [
                        {"batch_id": f"auto-{self.new_id()[:10]}", "unit_ids": batch["unit_ids"]}
                        for batch in plan_batches["batches"]
                    ],
                    "groups": [
                        {
                            "group_id": group["group_id"],
                            "card_ids": group["card_ids"],
                            "oversized": group["oversized"],
                        }
                        for group in groups
                    ],
                    "resolved_groups": {},
                    "reused_units": reused_unit_ids,
                    "unit_states": copy.deepcopy(unit_states),
                    "baseline_revision": int(support.get("revision") or 0),
                    "baseline_approved_version": int(support.get("approved_version") or 0),
                    "max_source_words": effective_source_words,
                    "max_parallel_batches": resolve_parallel_batches(max_parallel_batches),
                    "additional_work_limit": additional_work_limit,
                },
                "preview": {
                    "unit_count": len(targets),
                    "batch_count": int(plan_batches["batch_count"]),
                    "existing_card_count": len(support.get("cards") or {}),
                    "existing_eligible_count": existing_eligible,
                    "group_count": len(groups),
                    "oversized_groups": [g["group_id"] for g in groups if g["oversized"]],
                    # The confirmed execution runs one generation + one check per
                    # frozen batch of *unfinished* units, plus one judgment per
                    # related group whose input is not reusable.
                    "expected_generation_calls": int(plan_batches["batch_count"]),
                    "expected_check_calls": int(plan_batches["batch_count"]),
                    "expected_group_calls_max": len(
                        [group_id for group_id in pending_group_ids if group_id not in {
                            str(group["group_id"]) for group in groups if group["oversized"]
                        }]
                    ),
                    # The oversized groups' remaining units are paid from the
                    # shared pool, so their number of calls this execution is
                    # bounded by that limit — never by the whole group.
                    "expected_local_calls_max": min(budget_limit, local_units_pending),
                    "runs_generation": bool(plan_batches["batch_count"]),
                    # One pool for the whole execution: bounded lookups and
                    # large-group local judgments are not separate budgets.
                    "budget": {
                        "limit": budget_limit,
                        "used": 0,
                        "by_kind": {},
                        "note": "额外逻辑请求总数（含补查与大组局部分辨），不是各阶段各一份。",
                    },
                    # What is already valid and what still needs work, with the
                    # reason, so the page can show the incremental scope instead
                    # of only a total.
                    "reuse": {
                        "reused_unit_count": len(reused_unit_ids),
                        "work_unit_count": len(work_units),
                        "reused_units": [
                            {"unit_id": unit_id, "reason": unit_states[unit_id]["reason"]}
                            for unit_id in sorted(reused_unit_ids)
                        ],
                        "work_units": [
                            {"unit_id": str(unit.get("id")), "reason": unit_states[str(unit.get("id"))]["reason"]}
                            for unit in targets
                            if unit_states[str(unit.get("id"))]["state"] != "reused"
                        ],
                        "reused_group_count": len(reused_group_ids),
                        "pending_group_count": len(pending_group_ids),
                        # Local per-unit work of the oversized groups: what an
                        # earlier confirmation already answered and what is still
                        # owed. Both are shown, so "unfinished" can never look
                        # like "done" on the page.
                        "local_units_reused": local_units_reused,
                        "local_units_pending": local_units_pending,
                        "reused_groups": [
                            {"group_id": group_id, "reason": group_reuse[group_id]["reason"]}
                            for group_id in sorted(reused_group_ids)
                        ],
                        "pending_groups": [
                            {"group_id": group_id, "reason": group_reuse.get(group_id, {}).get("reason") or ""}
                            for group_id in pending_group_ids
                        ],
                    },
                },
                "summary": None,
                "model_calls": 0,
                "note": (
                    "预览不调用模型、不写入任何决定；已完成的单元和仍有效的组辨析会被复用，"
                    "预算内未完成的局部辨析会在再次确认后继续，确认后只处理未完成或已失效的部分。"
                    "准备结束不会自动开始翻译。"
                ),
            }


    def view_locked(
        self,
        *,
        prepare_id: str,
        support: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        support = support or normalize_quality_support(self.cell.state.get("quality_support"))
        record = quality_prepare_record.prepare_record(support, {"prepare_id": prepare_id})
        return {
            "status": "ok",
            "prepare_id": record["prepare_id"],
            "prepare_status": record["status"],
            "summary": quality_prepare_record.prepare_summary_from_record(record),
        }


    def status(
        self,
        *,
        expected_project_id: str | None = None,
        prepare_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only, bounded view of the current prepare and its live progress."""

        with self.cell.lock:
            validate_expected_project_id(self.cell, expected_project_id)
            project_id = str(self.cell.state.get("project", {}).get("id") or "")
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            automation = concept_automation.normalize_automation(support.get("automation"))
            record = automation.get("prepare")
            record_id = str(record.get("prepare_id") or "") if isinstance(record, Mapping) else ""
            requested_id = str(prepare_id or "").strip()
            if requested_id and requested_id != record_id:
                raise ConflictError("准备任务已经被替换或不存在，请刷新项目后重试观察。")

            progress = self.runtime.progress
            if (
                not isinstance(progress, Mapping)
                or str(progress.get("prepare_id") or "") != record_id
                or str(progress.get("project_id") or "") != project_id
            ):
                progress = None
            active = bool(
                record_id
                and record_id in self.runtime.prepare_inflight
                and isinstance(progress, Mapping)
                and progress.get("active") is True
            )
            persisted_status = str(record.get("status") or "") if isinstance(record, Mapping) else ""
            status = persisted_status or None
            if status == "running" and not active:
                # A durable running record without an owner is never resumed by
                # this read. The manager normally normalizes it on open; this
                # defensive view remains read-only if ownership was lost in-proc.
                status = "interrupted"
            if isinstance(progress, Mapping) and not active and progress.get("status"):
                # A commit/write error can leave a durable record in its previous
                # state while this live process knows the execution failed.
                if status == "interrupted" and progress.get("status") in {"failed", "stale"}:
                    status = str(progress.get("status"))

            if isinstance(record, Mapping):
                summary = quality_prepare_record.prepare_summary_from_record(record)
                summary["status"] = status or summary.get("status")
                # The status route is polled: keep its embedded error view
                # bounded without changing the existing quality-support result
                # contract returned by other routes.
                summary["errors"] = [
                    str(item)[:300]
                    for item in (summary.get("errors") or [])
                    if str(item).strip()
                ][-20:]
            else:
                summary = None

            stage_names = (
                "generation", "check", "recheck", "lookup",
                "group_resolution", "local_resolution", "commit",
            )
            if isinstance(progress, Mapping):
                stages = {
                    name: copy.deepcopy(dict((progress.get("stages") or {}).get(name) or {}))
                    for name in stage_names
                }
            else:
                # After restart only the persisted business summary is known.
                # Do not reconstruct counters or pretend a phase ran.
                stages = {
                    name: {
                        "state": "unknown",
                        "completed": None,
                        "total": None,
                        "running": None,
                        "failed": None,
                        "reused": None,
                        "not_required": None,
                        "pending": None,
                        "unit": unit,
                    }
                    for name, unit in (
                        ("generation", "batch"), ("check", "batch"),
                        ("recheck", "card"), ("lookup", "request"),
                        ("group_resolution", "group"),
                        ("local_resolution", "group_unit"), ("commit", "commit"),
                    )
                }
            if not record_id:
                stages = {
                    name: {
                        "state": "not_required", "completed": 0, "total": 0,
                        "running": 0, "failed": 0, "reused": 0,
                        "not_required": 0,
                        "pending": 0, "unit": unit,
                    }
                    for name, unit in (
                        ("generation", "batch"), ("check", "batch"),
                        ("recheck", "card"), ("lookup", "request"),
                        ("group_resolution", "group"),
                        ("local_resolution", "group_unit"), ("commit", "commit"),
                    )
                }

            if isinstance(progress, Mapping):
                active_items = [
                    copy.deepcopy(dict(item))
                    for item in (progress.get("_active") or {}).values()
                    if isinstance(item, Mapping)
                ][-32:]
                recent_activity = [
                    copy.deepcopy(dict(item))
                    for item in (progress.get("_recent_activity") or [])
                    if isinstance(item, Mapping)
                ][-20:]
                progress_errors = [str(item) for item in (progress.get("_errors") or []) if str(item).strip()]
                provider_calls = copy.deepcopy(dict(progress.get("provider_invocations") or {}))
                progress_revision = int(progress.get("progress_revision") or 0)
                started_at = str(progress.get("started_at") or "") or None
                updated_at = str(progress.get("updated_at") or "") or None
            else:
                active_items = []
                recent_activity = []
                progress_errors = []
                provider_calls = None
                progress_revision = None
                started_at = str(record.get("started_at") or "") or None if isinstance(record, Mapping) else None
                updated_at = str(record.get("finished_at") or record.get("started_at") or "") or None if isinstance(record, Mapping) else None
            summary_errors = list((summary or {}).get("errors") or [])
            errors: list[str] = []
            for item in summary_errors + progress_errors:
                if item and item not in errors:
                    errors.append(item)
            errors = errors[-20:]
            return {
                "project_id": project_id,
                "prepare_id": record_id or None,
                "status": status,
                "persisted_status": persisted_status or None,
                "active": active,
                "progress_revision": progress_revision,
                "started_at": started_at,
                "updated_at": updated_at,
                "stages": stages,
                "active_items": active_items,
                "recent_activity": recent_activity,
                "provider_calls": provider_calls,
                # The product does not expose an authoritative aggregate HTTP
                # counter across all configured providers.
                "provider_http": None,
                "errors": errors,
                "prepare": summary,
                "reference_mode": concept_automation.reference_mode(self.cell.state.get("project")),
                "reference_revision": int(automation.get("reference_revision") or 0),
                "decisions": len(automation.get("decisions") or {}),
            }

