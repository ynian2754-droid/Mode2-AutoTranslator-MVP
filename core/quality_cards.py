"""Card decisions and reference-mode transactions for one project.

The owner receives only the shared state cell and a clock supplier. Each
public operation retains its original complete lock and persistence boundary.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation
from core.exceptions import ConflictError, PipelineError
from core.project_state import (
    ProjectStateCell,
    append_event,
    ensure_open,
    validate_expected_project_id,
)
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import (
    MAX_BATCH_CARD_ACTIONS,
    QualitySupportError,
    apply_card_decision,
    batch_approval_problems,
    normalize_quality_support,
    summarize_counts,
)

QUALITY_CARD_ACTIONS = {"edit", "approve", "defer", "reject"}
QUALITY_BATCH_ACTIONS = {"approve", "defer", "reject"}
QUALITY_ACTION_LABELS = {"approve": "批准", "defer": "暂缓", "reject": "驳回", "edit": "编辑"}


class QualityCards:
    def __init__(self, cell: ProjectStateCell, clock: Callable[[], str]) -> None:
        self.cell = cell
        self.clock = clock

    def update_quality_card(
        self,
        card_id: str,
        action: str,
        *,
        content: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        expected_draft_revision: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit, approve, defer or reject one card under the project lock."""
        action = str(action or "").strip().casefold()
        if action not in QUALITY_CARD_ACTIONS:
            raise PipelineError("卡片操作只能是 edit、approve、defer 或 reject。")
        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            try:
                card = apply_card_decision(
                    support,
                    str(card_id),
                    action,
                    content=content,
                    unit_sources=unit_sources,
                    expected_revision=expected_revision,
                    expected_draft_revision=expected_draft_revision,
                    now_iso_value=self.clock(),
                )
            except QualitySupportError as exc:
                raise PipelineError(str(exc)) from exc
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            if action == "approve":
                append_event(
                    self.cell,
                    "quality_card_approved",
                    f"概念卡 {card['id']} 已人工批准生效。",
                    None, {}, self.clock,
                )
            commit_quality_support(
                self.cell, support, old_support=old_support, old_events=old_events
            )
            return {
                "status": "ok",
                "card": copy.deepcopy(card),
                "revision": support["revision"],
                "approved_version": support["approved_version"],
                "counts": summarize_counts(support),
            }

    def batch_quality_card_action(
        self,
        action: str,
        items: Sequence[Mapping[str, Any]],
        *,
        expected_revision: int | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Approve, defer or reject several pending cards in one atomic write.

        Every card is validated first and all decisions are applied to a copy of
        the concept data; only then is that copy committed with a single save.
        A stale project or global revision, a card that is not batch-approvable,
        a stale card draft revision or a failed save therefore leaves the stored
        state exactly as it was — there is no per-card write to compensate and no
        partially applied group.  Local unsaved browser edits are not visible
        here; the page must not send such cards.
        """

        action = str(action or "").strip().casefold()
        if action not in QUALITY_BATCH_ACTIONS:
            raise PipelineError("批量操作只能是 approve、defer 或 reject。")
        label = QUALITY_ACTION_LABELS[action]
        raw_items = list(items or [])
        if not raw_items:
            raise PipelineError("批量操作至少需要一张概念卡。")
        if len(raw_items) > MAX_BATCH_CARD_ACTIONS:
            raise PipelineError(f"一次最多处理 {MAX_BATCH_CARD_ACTIONS} 张概念卡。")
        wanted: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise PipelineError("批量操作的每一项都必须是对象。")
            card_id = str(item.get("card_id") or "").strip()
            if not card_id:
                raise PipelineError("批量操作缺少 card_id。")
            if card_id in seen:
                raise PipelineError(f"批量操作包含重复的概念卡 {card_id}。")
            seen.add(card_id)
            revision = item.get("expected_draft_revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise PipelineError(f"概念卡 {card_id} 缺少有效的 expected_draft_revision。")
            wanted.append((card_id, revision))

        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(
                support.get("revision") or 0
            ):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            work = copy.deepcopy(support)
            cards = work.get("cards") or {}
            for card_id, revision in wanted:
                card = cards.get(card_id)
                if not isinstance(card, dict):
                    raise PipelineError(f"找不到概念卡 {card_id}，批量操作未执行。")
                if action == "approve":
                    problems = batch_approval_problems(card, unit_sources=unit_sources)
                    if problems:
                        raise PipelineError(f"概念卡 {card_id} 不能批量批准：{'；'.join(problems)}")
                elif str(card.get("status") or "") != "pending_review" or not isinstance(
                    card.get("draft"), dict
                ):
                    raise PipelineError(f"概念卡 {card_id} 不是待复检草稿，批量操作未执行。")
                if int(card.get("draft_revision") or 0) != revision:
                    raise ConflictError(f"概念卡 {card_id} 的草稿版本已经变化，请刷新后重试。")
                try:
                    apply_card_decision(
                        work,
                        card_id,
                        action,
                        unit_sources=unit_sources,
                        expected_draft_revision=revision,
                        now_iso_value=self.clock(),
                    )
                except QualitySupportError as exc:
                    raise PipelineError(f"概念卡 {card_id} 未能{label}：{exc}") from exc
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            append_event(
                self.cell,
                f"quality_cards_batch_{action}",
                f"批量{label} {len(wanted)} 张待复检概念卡。",
                None, {}, self.clock,
            )
            commit_quality_support(
                self.cell, work, old_support=old_support, old_events=old_events
            )
            return {
                "status": "ok",
                "action": action,
                "card_ids": [card_id for card_id, _revision in wanted],
                "count": len(wanted),
                "revision": int(work.get("revision") or 0),
                "approved_version": int(work.get("approved_version") or 0),
                "counts": summarize_counts(work),
            }

    def set_reference_mode(
        self,
        mode: str,
        *,
        expected_project_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Switch the project between manual and automatic reference mode.

        Switching back to manual retires every stored automatic decision (they
        stay in storage but stop applying to new requests); already frozen
        snapshots are per unit and are never rewritten. Enabling automatic mode
        does not run anything by itself.
        """

        wanted = str(mode or "").strip().casefold()
        if wanted not in concept_automation.REFERENCE_MODES:
            raise PipelineError("参考模式只能是 manual 或 automatic。")
        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            project = self.cell.state.setdefault("project", {})
            previous = concept_automation.reference_mode(project)
            if previous == wanted:
                return {
                    "status": "ok",
                    "reference_mode": wanted,
                    "changed": False,
                    "revision": support["revision"],
                }
            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            project["reference_mode"] = wanted
            # Switching back to manual retires the automatic references for
            # *new* requests only: the stored decisions are kept so switching
            # back to automatic restores exactly the same verified set, and the
            # per-unit frozen snapshots are never rewritten either way.
            append_event(
                self.cell,
                "quality_reference_mode",
                f"参考模式已切换为 {wanted}。",
                None, {"mode": wanted, "previous": previous}, self.clock,
            )
            commit_quality_support(
                self.cell, support, old_support=old_support, old_events=old_events
            )
            return {
                "status": "ok",
                "reference_mode": wanted,
                "changed": True,
                "revision": support["revision"],
            }
