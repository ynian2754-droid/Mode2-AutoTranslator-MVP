"""Quality batch checking, repair guards, and failed-generation transactions."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation, quality_recovery, quality_requests
from core.exceptions import ConflictError, PipelineError
from core.project_state import (
    ProjectStateCell, append_event, ensure_open, validate_expected_project_id,
)
from core.provider_routing import ProviderRouter
from core.quality_progress import PrepareProgress
from core.quality_runtime import QualityRuntime
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import (
    QualitySupportError, apply_check_result, batch_retry_state,
    normalize_quality_support, record_batch, upsert_candidate,
)
from providers.quality_provider import ConceptCheckRequest, ConceptScanRequest
from providers.repair_loop import ContentRepairExhausted, RepairControl


class QualityBatchWorkflow:
    def __init__(
        self,
        cell: ProjectStateCell,
        runtime: QualityRuntime,
        progress: PrepareProgress,
        router: ProviderRouter,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.runtime = runtime
        self.progress = progress
        self.router = router
        self.clock = clock

    def repair_control(
        self,
        batch_id: str,
        signature: str,
        kind: str,
        *,
        frozen_cards: Mapping[str, Mapping[str, Any]] | None = None,
        frozen_units: Sequence[Mapping[str, Any]] | None = None,
        frozen_mode: str = "",
        prepare_id: str = "",
        progress_stage: str = "",
    ) -> RepairControl:
        """Authorize every concept repair request immediately before it is sent.

        A batch has no run to cancel, so the gate answers two questions: is this
        still the in-flight execution of a live project, and is the material the
        model is answering for still exactly what was frozen? A closed manager, a
        superseded batch, a changed source, a project that changed mode, or a card
        a human took over, edited or re-versioned stops the next round instead of
        paying for it. A refusal here is a conflict: a late result must not be
        written.
        """

        def verify() -> None:
            with self.cell.lock:
                if self.cell.closed:
                    raise ConflictError("项目已关闭，不再发起下一轮模型修正。")
                if self.runtime.batch_inflight.get(batch_id) != signature:
                    raise ConflictError("该批次已被更新的执行取代，不再发起下一轮模型修正。")
                if frozen_mode and concept_automation.reference_mode(
                    self.cell.state.get("project")
                ) != frozen_mode:
                    raise ConflictError("项目参考模式已经切换，本次批次结果不再适用。")
                if frozen_units is not None:
                    unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
                    for unit in frozen_units:
                        unit_id = str(unit.get("id") or "")
                        live = unit_sources.get(unit_id)
                        if live is None:
                            raise ConflictError(
                                f"单元 {unit_id} 已经不存在，本次批次结果不再适用。"
                            )
                        if str(live[1]) != str(unit.get("source_sha256") or "") or len(
                            str(live[0])
                        ) != len(str(unit.get("source") or "")):
                            raise ConflictError(
                                f"单元 {unit_id} 的源文已经变化，本次批次结果不再适用。"
                            )
                if frozen_cards is None:
                    return
                support = normalize_quality_support(self.cell.state.get("quality_support"))
                for card_id, expected in frozen_cards.items():
                    card = (support.get("cards") or {}).get(card_id)
                    if not isinstance(card, Mapping):
                        raise ConflictError(f"卡片 {card_id} 已经不存在，本次批次结果不再适用。")
                    if str(card.get("status") or "") != "pending_review":
                        raise ConflictError(f"卡片 {card_id} 已被人工处理，本次批次结果不再适用。")
                    if concept_automation.is_manual_protected(card):
                        raise ConflictError(f"卡片 {card_id} 已被人工接管，本次批次结果不再适用。")
                    if int(card.get("draft_revision") or 0) != int(
                        expected.get("draft_revision") or 0
                    ) or concept_automation._content_fingerprint(card) != str(
                        expected.get("content_fingerprint") or ""
                    ):
                        raise ConflictError(f"卡片 {card_id} 的内容已经变化，本次批次结果不再适用。")

        return RepairControl(
            invocation_id=batch_id,
            kind=kind,
            before_attempt=lambda round_no, api_calls: verify(),
            on_progress=(
                self.progress.repair_callback(prepare_id, progress_stage, batch_id)
                if prepare_id and progress_stage
                else None
            ),
        )

    def record_failed_generation_locked(
        self,
        *,
        batch_id: str,
        units: Sequence[Mapping[str, Any]],
        mode: str,
        prepare_id: str,
        error: BaseException,
    ) -> None:
        """Register a failed generation as a recovery record and persist it.

        It is written through ``record_batch`` with ``touch_coverage=False``: the
        units produced no scan result, so this must never move
        ``scanned_unit_ids`` (that would make "continue" skip them forever).
        """

        if self.cell.closed:
            return
        old_support = copy.deepcopy(self.cell.state.get("quality_support"))
        old_events = copy.deepcopy(self.cell.state.get("events") or [])
        support = normalize_quality_support(self.cell.state.get("quality_support"))
        existing = quality_recovery.batch_row_copy(support, batch_id) or {}
        record = quality_recovery.batch_retry_record(
            clock=self.clock,
            stage="generation",
            mode=mode,
            prepare_id=prepare_id,
            units=units,
            state="failed",
            attempt_count=int((existing.get("retry") or {}).get("attempt_count") or 0),
            last_error=str(error),
        )
        batch = {**existing, "batch_id": str(batch_id)}
        # Display fields: the record has to be findable, and its failure reason
        # must be the real one rather than an empty string.
        batch.setdefault("unit_ids", [str(unit.get("id") or "") for unit in units])
        batch.setdefault("status", "failed")
        batch["check_status"] = "not_run"
        batch["failed_count"] = len(batch.get("unit_ids") or [])
        batch["at"] = self.clock()
        batch["retry"] = record
        record_batch(support, batch, touch_coverage=False)
        append_event(
            self.cell,
            "quality_scan_generation_failed",
            f"概念候选批次 {batch_id} 生成失败，已登记为可重试批次。",
            None,
            {"batch_id": str(batch_id)},
            self.clock,
        )
        commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)

    def retry_check(
        self,
        batch_id: str,
        selected: list[dict[str, Any]],
        *,
        retry_close: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-run only the independent check of one already-saved batch.

        The generation request is never repeated: its candidates are already
        persisted. Model call happens outside the lock; the unit bindings are
        re-validated before anything is written. ``retry_close`` is set only by
        ``retry_quality_batch`` and folds the hand-retry record and its prepare
        sync into the same commit as the verdict itself.
        """
        # Re-capture the retry input under the manager lock, then release it
        # before the independent checker call.
        with self.cell.lock:
            ensure_open(self.cell)
            selected = copy.deepcopy(selected)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            signatures = [
                (unit["id"], unit["source_sha256"], len(str(unit.get("source") or "")))
                for unit in selected
            ]
            signature = repr(signatures)
            refs = quality_requests.concept_unit_refs(selected)
            cards = [
                card
                for card in support.get("cards", {}).values()
                if isinstance(card, dict)
                and str((card.get("origin") or {}).get("batch_id") or "") == batch_id
                and card.get("status") == "pending_review"
                and isinstance(card.get("draft"), dict)
                and not concept_automation.is_manual_protected(card)
            ]
            cards.sort(key=lambda card: str(card.get("id") or ""))
            candidates = tuple(card["draft"] for card in cards)  # type: ignore[misc]
            # The identity the model is about to answer for: card id, content
            # version, content fingerprint and the project/mode it belongs to.
            # Every repair request and the write that follows re-check it, so a
            # card a human took over mid-flight never receives a late verdict.
            frozen_cards = {
                str(card.get("id") or ""): {
                    "draft_revision": int(card.get("draft_revision") or 0),
                    "content_fingerprint": concept_automation._content_fingerprint(card),
                }
                for card in cards
            }
            frozen_mode = concept_automation.reference_mode(self.cell.state.get("project"))
            frozen_batch_ids = set(frozen_cards)
            project_id = str(self.cell.state.get("project", {}).get("id") or "")
            # A batch whose candidates were all exact duplicates has no card of
            # its own left to check. Retrying the check must not spend a model
            # call on an empty candidate list, and it must never reach for a card
            # this batch does not own: the retry only ever touches cards whose
            # origin is this batch_id.
            should_call = bool(candidates)
            if should_call:
                self.runtime.batch_inflight[batch_id] = signature

        _generation, checker, _editorial, _resolution = self.router.quality_channels()
        check_repair: dict[str, Any] | None = None
        check_error = ""
        checked_candidates = len(candidates)
        check_calls = 0
        if not should_call:
            checks = []
            check_status = "completed"
            checked_candidates = 0
        else:
            check_calls = 1
            try:
                check_result = checker.check_candidates(
                    ConceptCheckRequest(
                        project_id=project_id,
                        batch_id=batch_id,
                        units=refs,
                        candidates=candidates,
                        control=self.repair_control(
                            batch_id,
                            signature,
                            "概念独立检查",
                            frozen_cards=frozen_cards,
                            frozen_units=selected,
                            frozen_mode=frozen_mode,
                        ),
                    )
                )
                # The stored check must carry the same program-written identity
                # the first-time scan writes: without it the automatic path reads
                # this result as a legacy check and pays for another one. The
                # context is built here, from the live sources — never trusted
                # from the model.
                unit_sources = quality_unit_sources(
                    [unit for unit in selected if isinstance(unit, Mapping)]
                )
                checks = [
                    (
                        quality_requests.stored_check_payload(
                            check,
                            candidates[index],
                            unit_sources=unit_sources,
                            model=str(getattr(checker, "model", "") or ""),
                        )
                        if isinstance(check, Mapping) and index < len(candidates)
                        else check
                    )
                    for index, check in enumerate(check_result.checks)
                ]
                check_repair = check_result.repair
                check_status = "completed"
            except ConflictError:
                # A guard refusal (closed, superseded, mode switch, human takeover
                # or a card whose content moved on) is not a batch failure: it must
                # reach the caller as a conflict and must not be written.
                with self.cell.lock:
                    self.runtime.batch_inflight.pop(batch_id, None)
                raise
            except Exception as exc:  # noqa: BLE001 - reported, never silently emptied
                with self.cell.lock:
                    self.runtime.batch_inflight.pop(batch_id, None)
                checks = []
                check_status = "failed"
                check_error = str(exc)

        with self.cell.lock:
            self.runtime.batch_inflight.pop(batch_id, None)
            if self.cell.closed:
                raise ConflictError("当前项目管理器已关闭，不能保存检查结果。")
            if frozen_mode and concept_automation.reference_mode(
                self.cell.state.get("project")
            ) != frozen_mode:
                raise ConflictError("项目参考模式已经切换，检查结果未保存。")
            units_now = {
                str(unit.get("id")): unit
                for unit in (self.cell.state.get("units") or [])
                if isinstance(unit, dict)
            }
            for unit_id, source_sha256, length in signatures:
                current = units_now.get(unit_id)
                if current is None:
                    raise ConflictError("项目单元已经变化，检查结果未保存。")
                if str(current.get("source_sha256") or "") != str(source_sha256) or len(
                    str(current.get("source") or "")
                ) != length:
                    raise ConflictError("源文已经变化，检查结果未保存。")
            # The identity is re-checked once more at the write boundary: the model
            # answered for material that must still be exactly the frozen one,
            # otherwise the verdict is dropped instead of applied.
            live_support = normalize_quality_support(self.cell.state.get("quality_support"))
            for card_id, expected in frozen_cards.items():
                card = (live_support.get("cards") or {}).get(card_id)
                if not isinstance(card, Mapping) or str(card.get("status") or "") != "pending_review":
                    raise ConflictError(f"卡片 {card_id} 已被人工处理，检查结果未保存。")
                if concept_automation.is_manual_protected(card):
                    raise ConflictError(f"卡片 {card_id} 已被人工接管，检查结果未保存。")
                if int(card.get("draft_revision") or 0) != int(
                    expected.get("draft_revision") or 0
                ) or concept_automation._content_fingerprint(card) != str(
                    expected.get("content_fingerprint") or ""
                ):
                    raise ConflictError(f"卡片 {card_id} 的内容已经变化，检查结果未保存。")
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if checks:
                apply_check_result(
                    support,
                    batch_id=batch_id,
                    checks=checks,
                    now_iso_value=self.clock(),
                )
            stored_batch = quality_recovery.batch_row_copy(support, batch_id)
            if stored_batch is not None:
                stored_batch["check_status"] = check_status
                if check_status == "completed":
                    stored_batch["status"] = (
                        "completed" if not stored_batch.get("failed_count") else "partial"
                    )
                if check_repair is not None:
                    stored_batch["repair"] = {
                        **dict(stored_batch.get("repair") or {}),
                        "check": dict(check_repair),
                    }
                record_batch(support, stored_batch)
            append_event(
                self.cell,
                "quality_check_retried",
                f"概念批次 {batch_id} 的独立检查已重试，结果：{check_status}。",
                None,
                {"batch_id": batch_id},
                self.clock,
            )
            # Hand-retry bookkeeping is written by the same commit that stores
            # the verdict: a partial success — verdict saved, recovery state or
            # prepare rows missing — is not expressible here.
            retry_recorded = (
                quality_recovery.apply_retry_close(
                    support,
                    clock=self.clock,
                    batch_id=batch_id,
                    close=retry_close,
                    check_status=check_status,
                    check_error=check_error,
                )
                if retry_close is not None
                else False
            )
            commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)
            return {
                "status": "ok",
                "batch_id": batch_id,
                "candidate_count": len(candidates),
                "saved": [],
                "failed": [],
                "check_status": check_status,
                "check_error": check_error,
                "checked_candidates": checked_candidates,
                # Invocations counted at the call site: the checker is only
                # called when there is at least one candidate to check.
                "provider_calls": {"generation": 0, "check": check_calls},
                "retry_recorded": retry_recorded,
                "repair": {"check": dict(check_repair)} if check_repair else {},
                "revision": support["revision"],
                "approved_version": support["approved_version"],
                "retry": "check-only",
            }

    def scan_quality_batch(
        self,
        *,
        batch_id: str,
        unit_ids: list[str],
        expected_project_id: str | None = None,
        mode: str = "manual",
        retry_close: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one bounded generate+check batch and store only candidates.

        Model calls happen outside the project lock. Nothing is written until
        the source bindings are re-validated after the calls return. A batch
        that was already saved with a failed independent check can be retried
        with the same batch_id; the retry re-runs only the check, never the
        paid generation.

        ``retry_close`` is set only by ``retry_quality_batch``: it makes the
        hand-retry record and its prepare sync part of this batch's own commit,
        so a stored result is never visible without the recovery state that
        describes it. A scan may not start while another batch is being
        recovered by hand; the recovery's own batch is allowed here because
        generation recovery runs through this very path.
        """
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise PipelineError("缺少批次标识。")
        wanted = [str(unit_id) for unit_id in unit_ids or []]
        if not wanted:
            raise PipelineError("本批次没有指定单元。")

        retry_inputs: list[dict[str, Any]] | None = None
        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            if self.runtime.retry_inflight and batch_id not in self.runtime.retry_inflight:
                raise ConflictError(
                    f"批次 {sorted(self.runtime.retry_inflight)[0]} 正在恢复中，请等待结束再扫描。"
                )
            units = {
                str(unit.get("id")): unit
                for unit in (self.cell.state.get("units") or [])
                if isinstance(unit, dict)
            }
            selected: list[dict[str, Any]] = []
            for unit_id in wanted:
                unit = units.get(unit_id)
                if unit is None:
                    raise PipelineError(f"单元 {unit_id} 不存在。")
                selected.append(unit)
            signatures = [
                (unit["id"], unit["source_sha256"], len(str(unit.get("source") or "")))
                for unit in selected
            ]
            signature = repr(signatures)
            previous = self.runtime.batch_inflight.get(batch_id)
            if previous == signature:
                raise ConflictError(f"批次 {batch_id} 正在处理中，请勿重复提交。")
            if previous is not None and previous != signature:
                raise ConflictError(f"批次 {batch_id} 的输入已经变化，请重新规划扫描。")
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            # Freeze the project's actual reference mode for the whole call.
            # ``mode`` is the scan/prepare execution lane; it is not a
            # substitute for the project's current reference-mode identity.
            frozen_mode = concept_automation.reference_mode(self.cell.state.get("project"))
            stored_batch = quality_recovery.batch_row_copy(support, batch_id)
            if stored_batch is not None:
                self.runtime.batch_inflight.pop(batch_id, None)
                if [str(item) for item in stored_batch.get("unit_ids") or []] != wanted:
                    raise ConflictError(f"批次 {batch_id} 的单元与已保存记录不一致，请重新规划。")
                stored_stage = str((stored_batch.get("retry") or {}).get("stage") or "")
                stored_state = batch_retry_state(stored_batch)
                if stored_stage == "generation" and stored_state != "completed":
                    # The record only says "generation failed": nothing was ever
                    # saved, so this is the first real generation for this batch
                    # rather than a duplicate submit. Its units still get no scan
                    # coverage from the failed attempt.
                    self.runtime.batch_inflight[batch_id] = signature
                    project_id = str(self.cell.state.get("project", {}).get("id") or "")
                    prepare_record = concept_automation.automation_of(
                        normalize_quality_support(self.cell.state.get("quality_support"))
                    ).get("prepare")
                    owning_prepare = (
                        str(prepare_record.get("prepare_id") or "")
                        if mode == "automatic" and isinstance(prepare_record, Mapping)
                        else ""
                    )
                    approved_expressions = quality_requests.approved_expressions(support)
                    retry_inputs = None
                else:
                    if str(stored_batch.get("check_status") or "") != "failed":
                        raise ConflictError(f"批次 {batch_id} 已经保存过，不要重复提交。")
                    # Do not call the checker while this entry lock is held.  The
                    # retry path is the same network boundary as first-time scan.
                    retry_inputs = copy.deepcopy(selected)
            else:
                self.runtime.batch_inflight[batch_id] = signature
                project_id = str(self.cell.state.get("project", {}).get("id") or "")
                # Which prepare owns this batch, captured *before* the model call
                # so a later failure is never attributed to a new generation.
                prepare_record = concept_automation.automation_of(
                    normalize_quality_support(self.cell.state.get("quality_support"))
                ).get("prepare")
                owning_prepare = (
                    str(prepare_record.get("prepare_id") or "")
                    if mode == "automatic" and isinstance(prepare_record, Mapping)
                    else ""
                )
                approved_expressions = quality_requests.approved_expressions(support)

        if retry_inputs is not None:
            return self.retry_check(
                batch_id, retry_inputs, retry_close=retry_close
            )

        refs = quality_requests.concept_unit_refs(selected)
        generation, checker, _editorial, _resolution = self.router.quality_channels()
        # Counted at the call site, never inferred from the outcome: these are
        # the provider invocations this request really made. A provider that
        # reports its own transport usage adds the HTTP counts in ``repair``.
        generation_calls = 1
        progress_prepare_id = owning_prepare if mode == "automatic" else ""
        if progress_prepare_id:
            self.progress.change(
                progress_prepare_id,
                "generation",
                batch_id,
                "start",
                unit="batch",
                label="生成候选",
                metadata={"batch_id": batch_id, "unit_count": len(selected)},
                provider_channel="generation",
            )
        try:
            scan_result = generation.generate_candidates(
                ConceptScanRequest(
                    project_id=project_id,
                    batch_id=batch_id,
                    units=refs,
                    approved_expressions=approved_expressions,
                    control=self.repair_control(
                        batch_id,
                        signature,
                        "概念候选生成",
                        frozen_units=selected,
                        frozen_mode=frozen_mode,
                        prepare_id=progress_prepare_id,
                        progress_stage="generation",
                    ),
                )
            )
        except ContentRepairExhausted as exc:
            if progress_prepare_id:
                self.progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error=str(exc),
                )
                self.progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成失败，本批不进入独立检查",
                )
            with self.cell.lock:
                self.runtime.batch_inflight.pop(batch_id, None)
            # A first-run exhaustion still needs a durable generation retry
            # record. A hand retry already has its running record; its outer
            # recovery path closes that record with this concrete error.
            if retry_close is None:
                with self.cell.lock:
                    self.record_failed_generation_locked(
                        batch_id=batch_id,
                        units=selected,
                        mode=mode,
                        prepare_id=owning_prepare,
                        error=exc,
                    )
            # The controlled message already says what happened and how many
            # rounds were tried; wrapping it again would only duplicate it.
            raise PipelineError(str(exc)) from exc
        except ConflictError:
            if progress_prepare_id:
                self.progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error="准备输入身份已变化。",
                )
                self.progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成未完成，本批不进入独立检查",
                )
            # A frozen-input or mode guard failure is not a generation failure.
            # Do not create a retry record for a result that was never valid for
            # this project; the caller must receive the conflict unchanged.
            with self.cell.lock:
                self.runtime.batch_inflight.pop(batch_id, None)
            raise
        except Exception as exc:
            if progress_prepare_id:
                self.progress.change(
                    progress_prepare_id, "generation", batch_id, "failed",
                    unit="batch", error=str(exc),
                )
                self.progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "not_required",
                    unit="batch",
                    label="候选生成失败，本批不进入独立检查",
                )
            with self.cell.lock:
                self.runtime.batch_inflight.pop(batch_id, None)
                # The failure has to survive the request: without a record the
                # operator could never find this batch again. It is written as a
                # recovery record only, so scan coverage does not move.
                self.record_failed_generation_locked(
                    batch_id=batch_id,
                    units=selected,
                    mode=mode,
                    prepare_id=owning_prepare,
                    error=exc,
                )
            raise PipelineError(f"概念候选生成失败：{exc}") from exc
        if progress_prepare_id:
            self.progress.change(
                progress_prepare_id, "generation", batch_id, "complete", unit="batch"
            )

        check_status = "completed"
        checks: list[dict[str, Any]] = []
        check_repair: dict[str, Any] | None = None
        check_error = ""
        check_calls = 0
        if scan_result.candidates:
            check_calls = 1
            if progress_prepare_id:
                self.progress.change(
                    progress_prepare_id,
                    "check",
                    batch_id,
                    "start",
                    unit="batch",
                    label="独立检查",
                    metadata={"batch_id": batch_id, "candidate_count": len(scan_result.candidates)},
                    provider_channel="check",
                )
            try:
                check_result = checker.check_candidates(
                    ConceptCheckRequest(
                        project_id=project_id,
                        batch_id=batch_id,
                        units=refs,
                        candidates=tuple(scan_result.candidates),
                        control=self.repair_control(
                            batch_id,
                            signature,
                            "概念独立检查",
                            frozen_units=selected,
                            frozen_mode=frozen_mode,
                            prepare_id=progress_prepare_id,
                            progress_stage="check",
                        ),
                    )
                )
                checks = check_result.checks
                check_repair = check_result.repair
            except ConflictError:
                if progress_prepare_id:
                    self.progress.change(
                        progress_prepare_id, "check", batch_id, "failed",
                        unit="batch", error="准备输入身份已变化。",
                    )
                with self.cell.lock:
                    self.runtime.batch_inflight.pop(batch_id, None)
                raise
            except Exception as exc:  # noqa: BLE001 - the concrete reason is kept
                # A failed independent check must never be presented as
                # "evidence supported"; candidates are still kept as drafts.
                # The real reason travels with the batch record and the
                # response instead of being flattened into a generic sentence.
                check_status = "failed"
                check_error = str(exc)
                checks = []
                if progress_prepare_id:
                    self.progress.change(
                        progress_prepare_id, "check", batch_id, "failed",
                        unit="batch", error=str(exc),
                    )
            else:
                if progress_prepare_id:
                    self.progress.change(
                        progress_prepare_id, "check", batch_id, "complete", unit="batch"
                    )
        elif progress_prepare_id:
            self.progress.change(
                progress_prepare_id,
                "check",
                batch_id,
                "start",
                unit="batch",
                label="独立检查无需执行",
                metadata={"batch_id": batch_id, "candidate_count": 0},
            )
            self.progress.change(
                progress_prepare_id, "check", batch_id, "not_required", unit="batch"
            )

        with self.cell.lock:
            self.runtime.batch_inflight.pop(batch_id, None)
            if self.cell.closed:
                raise ConflictError("当前项目管理器已关闭，不能保存概念候选。")
            if frozen_mode and concept_automation.reference_mode(
                self.cell.state.get("project")
            ) != frozen_mode:
                raise ConflictError("项目参考模式已经切换，概念候选未保存。")
            units_now = {
                str(unit.get("id")): unit
                for unit in (self.cell.state.get("units") or [])
                if isinstance(unit, dict)
            }
            for unit_id, source_sha256, length in signatures:
                current = units_now.get(unit_id)
                if current is None:
                    raise ConflictError("项目单元已经变化，概念候选未保存。")
                if str(current.get("source_sha256") or "") != str(source_sha256) or len(
                    str(current.get("source") or "")
                ) != length:
                    raise ConflictError("源文已经变化，概念候选未保存。")
            unit_sources = quality_unit_sources(list(units_now.values()))
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            saved: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            duplicate_skipped: list[dict[str, Any]] = []
            for index, candidate in enumerate(scan_result.candidates):
                # The check result stays bound to the candidate index: skipping
                # a duplicate never shifts what the other candidates receive.
                check = checks[index] if index < len(checks) else None
                try:
                    # The automatic prepare path must not overwrite a card a
                    # human owns; the manual scan path keeps the historical
                    # writer untouched.
                    if mode == "automatic":
                        # The formal human-edit path must also exit automatic
                        # management: a saved manual edit is detected from its
                        # own marker and protected before any write happens.
                        target_id = concept_automation.candidate_card_id(candidate)
                        target = (support.get("cards") or {}).get(target_id)
                        if isinstance(target, dict):
                            concept_automation.mark_human_owned_card(target)
                        outcome = concept_automation.upsert_automatic_draft(
                            support,
                            candidate,
                            unit_sources=unit_sources,
                            prepare_id=batch_id,
                            unit_ids=[ref.unit_id for ref in refs],
                            now_iso_value=self.clock(),
                            check=(
                                quality_requests.stored_check_payload(
                                    check,
                                    candidate,
                                    unit_sources=unit_sources,
                                    model=str(getattr(checker, "model", "") or ""),
                                )
                                if check is not None
                                else None
                            ),
                        )
                    else:
                        outcome = upsert_candidate(
                            support,
                            candidate,
                            unit_sources=unit_sources,
                            batch_id=batch_id,
                            unit_ids=[ref.unit_id for ref in refs],
                            now_iso_value=self.clock(),
                            check=(
                                quality_requests.stored_check_payload(
                                    check,
                                    candidate,
                                    unit_sources=unit_sources,
                                    model=str(getattr(checker, "model", "") or ""),
                                )
                                if check is not None
                                else None
                            ),
                        )
                except QualitySupportError as exc:
                    failed.append(
                        {
                            "expressions": candidate.get("expressions") or [],
                            "reason": str(exc),
                        }
                    )
                    continue
                if outcome["outcome"] == "duplicate":
                    # The batch's own check for this candidate is dropped: it
                    # belongs to a candidate this project already holds, and the
                    # matched card keeps its origin, status and conclusion.
                    duplicate_skipped.append(
                        {
                            "index": index,
                            "card_id": outcome["card_id"],
                            "reason": outcome["reason"],
                        }
                    )
                    continue
                saved.append(
                    {
                        "card_id": outcome["card_id"],
                        "expressions": candidate["expressions"],
                        "outcome": outcome["outcome"],
                    }
                )
            repair_summary: dict[str, Any] = {}
            if scan_result.repair is not None:
                repair_summary["generate"] = dict(scan_result.repair)
            if check_repair is not None:
                repair_summary["check"] = dict(check_repair)
            created_count = sum(1 for row in saved if row["outcome"] == "created")
            updated_count = sum(1 for row in saved if row["outcome"] == "updated")
            retry_record = (
                quality_recovery.batch_retry_record(
                    clock=self.clock,
                    stage="check",
                    mode=mode,
                    prepare_id=owning_prepare,
                    units=selected,
                    cards=[
                        support["cards"][row["card_id"]]
                        for row in saved
                        if row["card_id"] in (support.get("cards") or {})
                    ],
                    state="failed",
                    attempt_count=0,
                    last_error=(
                        check_error.strip()
                        or "独立检查未完成，候选保留但不参与自动采用。"
                    ),
                )
                if check_status != "completed"
                else None
            )
            batch_record = {
                "batch_id": batch_id,
                "unit_ids": [ref.unit_id for ref in refs],
                **({"retry": retry_record} if retry_record is not None else {}),
                "status": "completed" if not failed and check_status == "completed" else "partial",
                "candidate_count": len(scan_result.candidates),
                "saved_count": len(saved),
                "created_count": created_count,
                "updated_count": updated_count,
                "duplicate_count": len(duplicate_skipped),
                "duplicate_skipped": duplicate_skipped,
                "failed_count": len(failed),
                "check_status": check_status,
                "model": scan_result.model,
                "usage": dict(scan_result.usage or {}),
                # Bounded-repair bookkeeping: which round succeeded and how
                # many HTTP requests the two calls really sent.
                "repair": repair_summary,
                "at": self.clock(),
            }
            # A successful generation recovery has no new retry record to put
            # in this result, but the running retry object carries the owning
            # prepare_id needed by _apply_retry_close_locked. Keep it through
            # this replacement of the batch row; otherwise record_batch would
            # erase the identity before the shared close can sync prepare rows.
            if retry_record is None and retry_close is not None and stored_batch is not None:
                previous_retry = copy.deepcopy(stored_batch.get("retry"))
                if previous_retry:
                    batch_record["retry"] = previous_retry
            record_batch(support, batch_record)
            append_event(
                self.cell,
                "quality_scan_batch_saved",
                f"概念候选批次 {batch_id} 已保存 {len(saved)} 张（新增 {created_count}、"
                f"更新 {updated_count}），完全重复跳过 {len(duplicate_skipped)} 张，失败 {len(failed)} 张。",
                None,
                {"batch_id": batch_id},
                self.clock,
            )
            # A hand retry closes its own record — and promotes the prepare rows
            # it recovered — inside this same commit.
            retry_recorded = (
                quality_recovery.apply_retry_close(
                    support,
                    clock=self.clock,
                    batch_id=batch_id,
                    close=retry_close,
                    check_status=check_status,
                    check_error=check_error,
                )
                if retry_close is not None
                else False
            )
            commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)
            return {
                "status": "ok",
                "batch_id": batch_id,
                "candidate_count": len(scan_result.candidates),
                "saved": saved,
                "duplicate_skipped": duplicate_skipped,
                "created_count": created_count,
                "updated_count": updated_count,
                "duplicate_count": len(duplicate_skipped),
                "failed": failed,
                "check_status": check_status,
                "check_error": check_error,
                # Invocations counted where they happened; "0" means the provider
                # was never called, not that the outcome is unknown.
                "provider_calls": {
                    "generation": generation_calls,
                    "check": check_calls,
                },
                "retry_recorded": retry_recorded,
                "repair": repair_summary,
                "revision": support["revision"],
                "approved_version": support["approved_version"],
            }
