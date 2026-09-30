"""Quality batch checking, repair guards, and failed-generation transactions."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation, quality_recovery, quality_requests
from core.exceptions import ConflictError
from core.project_state import ProjectStateCell, append_event, ensure_open
from core.provider_routing import ProviderRouter
from core.quality_progress import PrepareProgress
from core.quality_runtime import QualityRuntime
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import apply_check_result, normalize_quality_support, record_batch
from providers.quality_provider import ConceptCheckRequest
from providers.repair_loop import RepairControl


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
