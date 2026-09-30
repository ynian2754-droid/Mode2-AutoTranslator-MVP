"""Prepare ownership guards and record transactions over shared project resources."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from core import concept_automation, quality_prepare_plan, quality_prepare_record, quality_requests
from core.exceptions import ConflictError
from core.project_state import ProjectStateCell, ensure_open
from core.quality_progress import PrepareProgress
from core.quality_runtime import QualityRuntime
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import normalize_quality_support
from providers.repair_loop import RepairControl


class PrepareState:
    def __init__(
        self,
        cell: ProjectStateCell,
        runtime: QualityRuntime,
        progress: PrepareProgress,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.runtime = runtime
        self.progress = progress
        self.clock = clock

    def signature_locked(self) -> tuple[str, str, str]:
        """The identity a running prepare is bound to: project, mode, record id."""

        support = normalize_quality_support(self.cell.state.get("quality_support"))
        automation = concept_automation.normalize_automation(support.get("automation"))
        record = automation.get("prepare")
        return (
            str(self.cell.state.get("project", {}).get("id") or ""),
            concept_automation.reference_mode(self.cell.state.get("project")),
            str((record or {}).get("prepare_id") or ""),
        )

    def guard_locked(self, prepare_id: str) -> None:
        """Authorize one more model round for the live prepare.

        A close, a project switch, a mode change or a superseded/replaced prepare
        record all invalidate the run: late results are discarded instead of
        being written. The stored global revision is not part of the identity —
        this run's own commits move it — but a *foreign* writer replaces the
        prepare record or switches the mode, which is what the guard detects.
        """

        ensure_open(self.cell)
        bound = self.runtime.prepare_inflight.get(prepare_id)
        if bound is None:
            raise ConflictError("准备任务已经结束或被取代，迟到的结果不再写入。")
        if bound != self.signature_locked():
            raise ConflictError("项目、模式或准备任务已经变化，准备结果不再写入。")

    def group_fresh_locked(
        self, prepare_id: str, group_id: str, fingerprint: str
    ) -> None:
        """Refuse a group judgment whose frozen input is no longer live.

        The identity guard above only sees the project, the mode and the prepare
        id, so a formal human edit or an approval leaves it untouched. Here the
        live cards and sources are compared against the fingerprint frozen with
        the members, which is what actually makes the old judgment unusable.
        """

        ensure_open(self.cell)
        support = normalize_quality_support(self.cell.state.get("quality_support"))
        record = quality_prepare_record.prepare_record(support, {"prepare_id": prepare_id})
        stored_plan = quality_prepare_plan.prepare_plan_payload(record.get("plan") or {})
        group = next(
            (item for item in stored_plan.get("groups") or [] if item["group_id"] == group_id),
            None,
        )
        if group is None:
            raise ConflictError("准备计划已经变化，组辨析结果不再写入。")
        members = group.get("members")
        if not isinstance(members, list) or not members:
            members = quality_prepare_plan.prepare_group_members(support, group.get("card_ids") or [])
        live = concept_automation.group_input_fingerprint(
            group_id,
            members,
            support,
            quality_unit_sources(self.cell.state.get("units") or []),
        )
        if not fingerprint or live != fingerprint:
            raise ConflictError("组辨析期间相关卡片或原文已经变化，本次准备结果已失效。")

    def group_control_locked(
        self,
        prepare_id: str,
        group_id: str,
        fingerprint: str,
        *,
        suffix: str = "",
        progress_stage: str = "",
        progress_item_id: str = "",
    ) -> RepairControl:
        """Authorize every request of one group judgment, repairs included.

        ``suffix`` only distinguishes the invocation identity of the several
        local judgments of one oversized group; the authorization itself is
        always the whole group's frozen input, because that is what all of them
        read.
        """

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.cell.lock:
                self.guard_locked(prepare_id)
                self.group_fresh_locked(prepare_id, group_id, fingerprint)

        return RepairControl(
            invocation_id=f"{prepare_id}:{group_id}{suffix}",
            kind="concept-resolution",
            before_attempt=before_attempt,
            on_progress=(
                self.progress.repair_callback(
                    prepare_id,
                    progress_stage,
                    progress_item_id or f"{group_id}{suffix}",
                )
                if progress_stage
                else None
            ),
        )

    def begin_locked(self, prepare_id: str) -> tuple[str, str, str]:
        for other_id in list(self.runtime.prepare_inflight):
            if other_id != prepare_id:
                raise ConflictError("已有一个准备任务正在进行中，请等待它结束。")
        signature = self.signature_locked()
        self.runtime.prepare_inflight[prepare_id] = signature
        return signature

    def finish(self, prepare_id: str) -> None:
        with self.cell.lock:
            self.runtime.prepare_inflight.pop(prepare_id, None)
            self.progress.finish_locked(prepare_id)

    def update_record_locked(
        self,
        support: dict[str, Any],
        prepare_id: str,
        mutate: Any,
    ) -> None:
        """Mutate the frozen prepare record in place and persist it once."""

        automation = concept_automation.normalize_automation(support.get("automation"))
        record = automation.get("prepare")
        if not isinstance(record, Mapping) or str(record.get("prepare_id") or "") != prepare_id:
            raise ConflictError("准备任务已经变化，本次结果不再写入。")
        record = copy.deepcopy(dict(record))
        mutate(record)
        automation["prepare"] = record
        support["automation"] = automation
        old_support = copy.deepcopy(self.cell.state.get("quality_support"))
        old_events = copy.deepcopy(self.cell.state.get("events") or [])
        commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)

    def mark_failed_locked(
        self, prepare_id: str, reason: str, *, status: str = "failed"
    ) -> None:
        support = normalize_quality_support(self.cell.state.get("quality_support"))
        try:
            def mutate(record: dict[str, Any]) -> None:
                record["status"] = status if status in concept_automation.PREPARE_STATUSES else "failed"
                record["finished_at"] = self.clock()
                if reason and reason not in record.get("errors", []):
                    record.setdefault("errors", []).append(reason)

            self.update_record_locked(support, prepare_id, mutate)
        except Exception:  # pragma: no cover - best effort while unwinding
            pass

    def cards_stale_locked(
        self,
        cards: Mapping[str, tuple[int, str]],
        unit_hashes: Mapping[str, str],
    ) -> str:
        """Why a request's frozen input is no longer live, or "" when it still is.

        The lifecycle guard only knows the project, the mode and the prepare id,
        so a formal human edit, a protection change or a re-imported source
        leaves it untouched. Every request that may be followed by a repair round
        therefore re-checks here, immediately before each round, the card ids and
        draft revisions it was shown, their content fingerprints, their live
        protection state and the hash of every source it read.
        """

        support = normalize_quality_support(self.cell.state.get("quality_support"))
        live_sources = quality_unit_sources(self.cell.state.get("units") or [])
        cards_now = (support.get("cards") or {}) if isinstance(support, Mapping) else {}
        for card_id, pair in cards.items():
            revision, fingerprint = (int(pair[0]), str(pair[1]))
            card = cards_now.get(str(card_id))
            if not isinstance(card, dict):
                return f"请求所依据的卡片 {card_id} 已经不存在。"
            if concept_automation.is_manual_protected(card):
                return f"卡片 {card_id} 已经有人工内容，自动结果不再写入。"
            if int(card.get("draft_revision") or 0) != revision:
                return f"卡片 {card_id} 的草稿版本已经变化。"
            live_fingerprint = quality_requests.assessment_context_payload(
                dict(card.get("draft") or {}),
                unit_sources=live_sources,
                model="",
            )["content_fingerprint"]
            if not live_fingerprint or live_fingerprint != fingerprint:
                return f"卡片 {card_id} 的内容已经变化。"
        for unit_id, expected in unit_hashes.items():
            if str((live_sources.get(str(unit_id)) or ("", ""))[1]) != str(expected):
                return f"原文 {unit_id} 已经变化。"
        return ""

    def guard_control_locked(
        self,
        prepare_id: str,
        kind: str,
        *,
        cards: Mapping[str, tuple[int, str]] | None = None,
        unit_hashes: Mapping[str, str] | None = None,
        progress_stage: str = "",
        progress_item_id: str = "",
    ) -> RepairControl:
        """Authorize every round of one prepare request that is not a group judgment.

        A re-check or a lookup reads specific cards of the frozen scope, so the
        lifecycle identity alone is not enough: a formal human edit, a protection
        change or a re-imported source leaves that identity untouched while the
        answer the next round would produce is already about content nobody asked
        about any more. Before the first round and before **every** repair round,
        the frozen card identities (id, draft revision, content fingerprint), their
        live protection state and the hash of every source the request read are
        compared with the live project. A refusal raises :class:`ConflictError`,
        which the callers must propagate instead of recording it as a failed task.
        """

        frozen_cards = dict(cards or {})
        frozen_hashes = dict(unit_hashes or {})

        def before_attempt(round_no: int, api_calls: int) -> None:
            with self.cell.lock:
                self.guard_locked(prepare_id)
                stale = self.cards_stale_locked(frozen_cards, frozen_hashes)
                if stale:
                    raise ConflictError(f"{stale}本次请求已失效，结果不再写入。")

        return RepairControl(
            invocation_id=f"{prepare_id}:{kind}",
            kind=kind,
            before_attempt=before_attempt,
            on_progress=(
                self.progress.repair_callback(
                    prepare_id, progress_stage, progress_item_id
                )
                if progress_stage and progress_item_id
                else None
            ),
        )

    def update_plan_locked(self, prepare_id: str, plan: Mapping[str, Any]) -> None:
        support = normalize_quality_support(self.cell.state.get("quality_support"))

        def mutate(record: dict[str, Any]) -> None:
            record["plan"] = dict(plan)

        self.update_record_locked(support, prepare_id, mutate)
