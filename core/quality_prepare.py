"""Confirmed prepare coordination over the concrete quality domain owners."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

import mode2_common
from core import concept_automation, quality_prepare_plan, quality_prepare_record
from core.exceptions import ConflictError, PipelineError
from core.project_state import ProjectStateCell, ensure_open, validate_expected_project_id
from core.quality_batches import QualityBatchWorkflow
from core.quality_commit import PrepareCommit
from core.quality_limits import resolve_additional_work_limit, resolve_parallel_batches
from core.quality_lookup import PrepareLookup
from core.quality_prepare_state import PrepareState
from core.quality_prepare_views import PrepareViews
from core.quality_progress import PrepareProgress
from core.quality_queries import QualityQueries
from core.quality_recheck import PrepareRecheck
from core.quality_resolution import PrepareResolution
from core.quality_runtime import QualityRuntime
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import DEFAULT_SCAN_SOURCE_WORDS, normalize_quality_support, planned_batches
from providers.quality_provider import normalize_resolution


class PrepareCoordinator:
    def __init__(
        self,
        cell: ProjectStateCell,
        runtime: QualityRuntime,
        progress: PrepareProgress,
        prepare_state: PrepareState,
        queries: QualityQueries,
        views: PrepareViews,
        batches: QualityBatchWorkflow,
        recheck: PrepareRecheck,
        lookup: PrepareLookup,
        resolution: PrepareResolution,
        committer: PrepareCommit,
        clock: Callable[[], str],
        new_id: Callable[[], str],
        executor_factory: Callable[..., Any],
        completed_futures: Callable[..., Any],
    ) -> None:
        self.cell = cell
        self.runtime = runtime
        self.progress = progress
        self.prepare_state = prepare_state
        self.queries = queries
        self.views = views
        self.batches = batches
        self.recheck = recheck
        self.lookup = lookup
        self.resolution = resolution
        self.committer = committer
        self.clock = clock
        self.new_id = new_id
        self.executor_factory = executor_factory
        self.completed_futures = completed_futures

    def run(
        self,
        *,
        phase: str,
        plan: Mapping[str, Any] | None = None,
        unit_ids: list[str] | None = None,
        current_unit_id: str | None = None,
        max_source_words: int | None = None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None = None,
        expected_revision: int | None = None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Run one phase of the automatic reference preparation.

        ``plan``     is **read-only**: it freezes the scope into a preview and
                     makes **zero model calls** and zero writes;
        ``execute``  is the confirmed step: it freezes the single active prepare,
                     runs the bounded generation + check batches, judges the
                     related groups, then validates and stores the decisions in
                     one commit (model calls: yes);
        ``resolve`` / ``commit`` stay available for the frozen prepare as
                     separate steps (a page that already holds a plan id may
                     drive them one by one).

        Every phase binds the project, the global revision and the frozen
        ``prepare_id``; a stale binding is a 409 and never triggers a model call.
        """

        phase = str(phase or "").strip().casefold()
        plan_id = str((plan or {}).get("prepare_id") or "")
        work_limit = resolve_additional_work_limit(additional_work_limit)
        parallel_batches = resolve_parallel_batches(max_parallel_batches)
        if phase == "plan":
            return self.views.plan(
                unit_ids=unit_ids,
                current_unit_id=current_unit_id,
                max_source_words=max_source_words,
                max_parallel_batches=parallel_batches,
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
                additional_work_limit=work_limit,
            )
        if phase == "execute":
            return self.execute(
                plan={"prepare_id": plan_id} if plan_id else {},
                unit_ids=unit_ids,
                current_unit_id=current_unit_id,
                max_source_words=max_source_words,
                max_parallel_batches=parallel_batches,
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
                additional_work_limit=work_limit,
            )
        if phase == "resolve":
            return self.resolve(
                plan={"prepare_id": plan_id} if plan_id else {},
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
            )
        if phase == "commit":
            return self.committer.commit(
                plan={"prepare_id": plan_id} if plan_id else {},
                expected_project_id=expected_project_id,
                expected_revision=expected_revision,
            )
        raise PipelineError("prepare 的 phase 只能是 plan、execute、resolve 或 commit。")


    def execute(
        self,
        *,
        plan: Mapping[str, Any],
        unit_ids: list[str] | None,
        current_unit_id: str | None,
        max_source_words: int | None,
        max_parallel_batches: int | None = None,
        expected_project_id: str | None,
        expected_revision: int | None,
        additional_work_limit: int | None = None,
    ) -> dict[str, Any]:
        """Freeze the single active prepare and run the whole confirmed chain."""

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
            requested_id = str(plan.get("prepare_id") or "")
            if requested_id and isinstance(running, Mapping) and requested_id == str(running.get("prepare_id") or ""):
                raise ConflictError("这个准备计划已经执行过，请重新预览后再确认。")
            if self.runtime.prepare_inflight and requested_id not in self.runtime.prepare_inflight:
                # A concurrent execution must not start a second run: only the
                # already-frozen prepare may continue.
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
            unit_sources_live = quality_unit_sources(self.cell.state.get("units") or [])
            # Incremental scope: units whose previous generation + check finished
            # for the same source hash are not scanned again, and the judgments
            # whose frozen input still matches are handed over by the freeze step
            # below (the prior record is captured before it is replaced).
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
            prior_judgments = concept_automation.prior_group_judgments(automation.get("prepare"))
            prior_record = automation.get("prepare")
            # Executed lookups are handed over with the run: the confirmed
            # execution that follows must not pay again for a question this
            # project already asked with exactly the same content and evidence.
            prior_lookups = list((prior_record or {}).get("lookups") or [])
            prior_lookup_state = concept_automation.lookup_state_of(
                (prior_record or {}).get("lookup_state")
            )
            prepare_id = requested_id or f"prepare-{self.new_id()[:12]}"
            prepared = quality_prepare_plan.prepare_plan_payload(
                {
                    "prepare_id": prepare_id,
                    "scope": [str(unit.get("id")) for unit in targets],
                    "scope_fingerprint": concept_automation.prepare_fingerprint(targets),
                    "batches": [
                        {"batch_id": f"auto-{self.new_id()[:10]}", "unit_ids": batch["unit_ids"]}
                        for batch in plan_batches["batches"]
                    ],
                    "groups": [],
                    "reused_units": [
                        unit_id for unit_id, state in unit_states.items() if state["state"] == "reused"
                    ],
                    "unit_states": copy.deepcopy(unit_states),
                    "baseline_revision": int(support.get("revision") or 0),
                    "baseline_approved_version": int(support.get("approved_version") or 0),
                    "max_source_words": effective_source_words,
                    # Frozen like the batch size: the confirmed run executes with
                    # the parallelism its preview showed, not with a later edit.
                    "max_parallel_batches": resolve_parallel_batches(max_parallel_batches),
                    "additional_work_limit": additional_work_limit,
                }
            )
            automation["prepare"] = quality_prepare_record.new_prepare_record(
                prepared,
                clock=lambda: self.clock(),
                unit_states=unit_states,
                unit_sources=unit_sources_live,
                lookups=prior_lookups,
                lookup_state=prior_lookup_state,
            )
            support["automation"] = automation
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            try:
                commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)
            except Exception:
                self.prepare_state.finish(prepare_id)
                raise
            # Bind the guard only after the frozen record is in the live state,
            # so the identity later steps see is the one registered here.
            self.prepare_state.begin_locked(prepare_id)
            self.progress.begin_locked(
                prepare_id,
                prepared,
                automation.get("prepare") if isinstance(automation.get("prepare"), Mapping) else {},
            )

        try:
            batch_failures = self.run_batches(prepare_id, prepared, prior_judgments)
            if batch_failures and len(batch_failures) >= len(prepared["batches"]):
                # Every batch failed: this is a failed preparation, not a
                # zero-candidate success. The record already carries the real
                # per-unit failures; the caller gets the first error to show.
                # A run with nothing left to do has no batches and cannot fail
                # this way — reusing everything is a valid result.
                raise PipelineError(str(batch_failures[0]))
            self.resolution.resolve_pending(prepare_id)
            return self.committer.commit(
                plan={"prepare_id": prepare_id},
                expected_project_id=expected_project_id,
                expected_revision=None,  # the guard checked the live revision above
                internal=True,
            )
        except Exception as exc:
            # A failed execution stays visible for what it really was; the
            # frozen record is not rewritten into "complete" afterwards. An
            # invalidation (closed/replaced/changed input) is reported as
            # ``stale``: nothing of it may be adopted.
            with self.cell.lock:
                if not self.cell.closed:
                    self.prepare_state.mark_failed_locked(
                        prepare_id,
                        str(exc) or "准备执行中断或失败。",
                        status="stale" if isinstance(exc, ConflictError) else "failed",
                    )
            raise
        finally:
            self.prepare_state.finish(prepare_id)


    def run_batches(
        self,
        prepare_id: str,
        prepared: Mapping[str, Any],
        prior_judgments: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[str]:
        """Run the bounded generation + check batches with a bounded worker pool.

        The pool size is the parallelism the preview froze into the plan, never
        more than there are batches; the default 1 keeps the historical
        one-at-a-time behaviour (and then no pool is created at all). Every
        batch keeps its own identity while it runs: the model calls happen
        outside the project lock, the per-unit results and the failed-unit
        counter are written as they happen, and the request counters of all
        batches add up under the lock. A batch that fails for model reasons is
        recorded and its siblings keep going; a guard rejection (closed,
        superseded or mode-switched prepare) stops the scheduling and
        propagates, so a late result is never written.

        Returns the error of every batch that failed, in batch order; the
        per-unit results and the failed-unit counter are written as they
        happen, so a partly failed preparation can never be summarized as a
        completed one.
        """

        prior_judgments = prior_judgments or {}
        # No early exit for an empty batch list: a scope whose units are all
        # reused still has to freeze its groups and honour the bounded lookup.
        batches = list(prepared["batches"])
        # Batch errors are collected per position so the reported order does not
        # depend on which thread finished first; the A2 block below appends the
        # group-level failures to the same list, exactly as it always did.
        failures: list[str] = []
        batch_errors: list[str | None] = [None] * len(batches)

        def run_batch(index: int, batch: Mapping[str, Any]) -> None:
            """One batch, start to finish; the caller owns the thread."""

            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
            try:
                result = self.batches.scan_quality_batch(
                    batch_id=batch["batch_id"],
                    unit_ids=batch["unit_ids"],
                    expected_project_id=None,
                    mode="automatic",
                )
            except ConflictError:
                # A stale/superseded prepare is not a batch failure: it ends the
                # run, exactly as the serial loop did.
                raise
            except Exception as exc:
                batch_errors[index] = f"批次 {batch['batch_id']}：{exc}"
                with self.cell.lock:
                    self.prepare_state.guard_locked(prepare_id)
                    support = normalize_quality_support(self.cell.state.get("quality_support"))

                    def mutate_fail(record: dict[str, Any], batch=batch, exc=exc) -> None:
                        record["counts"]["failed_units"] = int(
                            record["counts"].get("failed_units") or 0
                        ) + len(batch["unit_ids"])
                        record["counts"]["skipped"] = int(record["counts"].get("skipped") or 0) + len(
                            batch["unit_ids"]
                        )
                        record["errors"].append(f"批次 {batch['batch_id']}：{exc}")
                        for row in record.get("unit_results") or []:
                            if row["unit_id"] in batch["unit_ids"]:
                                row["status"] = "failed"
                                row["reason"] = str(exc)[:300]

                    self.prepare_state.update_record_locked(support, prepare_id, mutate_fail)
                return
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                support = normalize_quality_support(self.cell.state.get("quality_support"))
                repair = result.get("repair") or {}
                generation_calls = int((repair.get("generate") or {}).get("api_calls") or 0)
                check_calls = int((repair.get("check") or {}).get("api_calls") or 0)
                if not generation_calls:
                    generation_calls = 1
                if not check_calls and result.get("candidate_count"):
                    check_calls = 1
                check_failed = str(result.get("check_status") or "") != "completed"
                saved = len(result.get("saved") or [])
                duplicates = int(result.get("duplicate_count") or 0)
                failed = len(result.get("failed") or [])

                def mutate_ok(
                    record: dict[str, Any],
                    batch=batch,
                    generation_calls=generation_calls,
                    check_calls=check_calls,
                    check_failed=check_failed,
                    saved=saved,
                    duplicates=duplicates,
                    failed=failed,
                ) -> None:
                    record["requests"]["generation"] = int(record["requests"].get("generation") or 0) + generation_calls
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + check_calls
                    for key, value in (("skipped", duplicates + failed), ("unresolved", 0)):
                        if value:
                            record["counts"][key] = int(record["counts"].get(key) or 0) + value
                    if not check_failed and not saved and not duplicates and not failed:
                        # A finished batch that produced no candidate at all is a
                        # real result, and it must be visible as "no candidates"
                        # rather than as a silent zero.
                        record["counts"]["no_candidate_units"] = int(
                            record["counts"].get("no_candidate_units") or 0
                        ) + len(batch["unit_ids"])
                    if check_failed:
                        record["counts"]["failed_units"] = int(record["counts"].get("failed_units") or 0) + len(
                            batch["unit_ids"]
                        )
                        record["errors"].append(
                            f"批次 {batch['batch_id']}：独立检查未完成，候选保留但不参与自动采用。"
                        )
                    for row in record.get("unit_results") or []:
                        if row["unit_id"] in batch["unit_ids"]:
                            row["status"] = "failed" if check_failed else "completed"
                            row["batch_id"] = batch["batch_id"]
                            row["reason"] = "独立检查未完成。" if check_failed else ""

                self.prepare_state.update_record_locked(support, prepare_id, mutate_ok)

        workers = max(
            1,
            min(
                resolve_parallel_batches(prepared.get("max_parallel_batches")),
                len(batches),
            ),
        )
        if workers == 1:
            for index, batch in enumerate(batches):
                run_batch(index, batch)
        else:
            with self.executor_factory(
                max_workers=workers, thread_name_prefix="prepare-batch"
            ) as pool:
                futures = [pool.submit(run_batch, index, batch) for index, batch in enumerate(batches)]
                for future in self.completed_futures(futures):
                    error = future.exception()
                    if error is not None:
                        # One guard rejection ends the whole confirmed run: stop
                        # whatever has not started and report it as the caller
                        # did before. Batches already in flight finish and are
                        # rejected by the guard when they try to write.
                        for other in futures:
                            other.cancel()
                        raise error
        # Merged before the A2 block: it appends the group-level failures and the
        # function keeps its single ``return failures`` exit point.
        failures.extend(item for item in batch_errors if item)

        # A2: the incrementally reused units keep their old cards, but an old
        # card is not automatically a usable one. Before the groups are frozen
        # from the live cards, the checks that cannot be adopted as is are
        # refreshed (same provider, same bounded repair loop, no new candidates)
        # and the run's single bounded lookup is spent on the questions that
        # asked for one. Both steps refuse to write anything their frozen
        # identity no longer matches.
        with self.cell.lock:
            self.prepare_state.guard_locked(prepare_id)
        self.recheck.refresh_checks(prepare_id, prepared)
        self.lookup.bounded_lookup(prepare_id)
        # Freeze the related groups only now: they are derived from the cards
        # the confirmed execution just produced, not from the empty preview.
        with self.cell.lock:
            self.prepare_state.guard_locked(prepare_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            groups = concept_automation.planned_groups(support, unit_sources=unit_sources)

            frozen_groups: list[dict[str, Any]] = []
            reused_outcomes: dict[str, dict[str, Any]] = {}
            for group in groups:
                members = quality_prepare_plan.prepare_group_members(support, group["card_ids"])
                fingerprint = concept_automation.group_input_fingerprint(
                    group["group_id"], members, support, unit_sources
                )
                frozen_groups.append(
                    {
                        "group_id": group["group_id"],
                        "card_ids": group["card_ids"],
                        "oversized": group["oversized"],
                        "members": members,
                        # Frozen with the members: the judgment stays usable only
                        # while the live cards and sources still match this.
                        "input_fingerprint": fingerprint,
                    }
                )
                prior = prior_judgments.get(str(group["group_id"])) or {}
                # An oversized group may carry a *local* outcome from the previous
                # run (A3): it is reused exactly like a whole-group judgment, so an
                # unchanged project still costs nothing. A group without one keeps
                # waiting; no member is ever cut to make it fit.
                reusable = not group["oversized"] or bool(
                    (prior.get("outcome") or {}).get("local")
                )
                if (
                    not reusable
                    or len(members) < 2
                    or not fingerprint
                    or str(prior.get("input_fingerprint") or "") != fingerprint
                ):
                    continue
                # A reused judgment is re-validated against the live members and
                # sources before it is trusted: a fingerprint match never skips
                # the local protocol checks.
                outcome = copy.deepcopy(dict(prior.get("outcome") or {}))
                try:
                    normalized = normalize_resolution(
                        outcome.get("payload") or {},
                        member_ids={str(member["card_id"]) for member in members},
                        group_id=str(group["group_id"]),
                        source_units=dict(unit_sources),
                    )
                except Exception:
                    continue
                outcome["payload"] = normalized
                outcome["relation"] = str(normalized.get("relation") or outcome.get("relation") or "")
                outcome["reused"] = True
                outcome["from_prepare_id"] = str(prior.get("prepare_id") or "")
                reused_outcomes[str(group["group_id"])] = outcome

            def mutate_groups(
                record: dict[str, Any],
                frozen=frozen_groups,
                reused=reused_outcomes,
            ) -> None:
                plan = record.get("plan") or {}
                plan["groups"] = copy.deepcopy(frozen)
                resolved = dict(plan.get("resolved_groups") or {})
                resolved.update(copy.deepcopy(reused))
                plan["resolved_groups"] = resolved
                record["counts"]["reused_groups"] = len(reused)
                record["plan"] = plan

            self.prepare_state.update_record_locked(support, prepare_id, mutate_groups)
        return failures


    def resolve(
        self,
        *,
        plan: Mapping[str, Any],
        expected_project_id: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """Resolve the pending groups of a frozen prepare (separate step)."""

        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            record = quality_prepare_record.prepare_record(support, plan)
        self.resolution.resolve_pending(record["prepare_id"])
        return self.views.view_locked(prepare_id=record["prepare_id"])

