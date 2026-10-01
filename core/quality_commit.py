"""Automatic concept adoption and final prepare persistence."""

from __future__ import annotations

import copy
import json
from typing import Any, Callable, Mapping

from core import concept_automation, quality_prepare_plan, quality_prepare_record
from core.exceptions import ConflictError
from core.project_state import (
    ProjectStateCell,
    append_event,
    ensure_open,
    validate_expected_project_id,
)
from core.quality_prepare_state import PrepareState
from core.quality_progress import PrepareProgress
from core.quality_state import commit_quality_support, quality_unit_sources
from core.quality_support import normalize_quality_support


def same_automatic_decision(
    stored: Mapping[str, Any] | None,
    fresh: Mapping[str, Any] | None,
) -> bool:
    """Whether two decisions say the same thing about the same card.

    The run id and the decision timestamp describe *when* a decision was
    written, not what it allows. Re-running a preparation over unchanged
    input must therefore keep the existing record verbatim instead of
    re-stamping it, so the reference revision and the frozen snapshot's
    decision id stay valid."""

    if not isinstance(stored, Mapping) or not isinstance(fresh, Mapping):
        return False
    ignored = {"prepare_id", "decided_at"}
    for key in (set(stored) | set(fresh)) - ignored:
        left, right = stored.get(key), fresh.get(key)
        if key in ("allowed_unit_ids", "member_ids", "evidence"):
            if json.dumps(left, sort_keys=True, ensure_ascii=False) != json.dumps(
                right, sort_keys=True, ensure_ascii=False
            ):
                return False
        elif left != right:
            return False
    return True


class PrepareCommit:
    def __init__(
        self,
        cell: ProjectStateCell,
        prepare_state: PrepareState,
        progress: PrepareProgress,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.prepare_state = prepare_state
        self.progress = progress
        self.clock = clock

    def commit(
        self,
        *,
        plan: Mapping[str, Any],
        expected_project_id: str | None,
        expected_revision: int | None,
        internal: bool = False,
    ) -> dict[str, Any]:
        """Validate the finished preparation and store the automatic decisions."""

        with self.cell.lock:
            ensure_open(self.cell)
            validate_expected_project_id(self.cell, expected_project_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            if expected_revision is not None and int(expected_revision) != int(support.get("revision") or 0):
                raise ConflictError("概念数据已经变化，请刷新后重试。")
            record = quality_prepare_record.prepare_record(support, plan)
            if str(record.get("mode") or "") != concept_automation.AUTOMATIC_MODE:
                raise ConflictError("准备任务已经变化，请重新开始准备。")
            stored_plan = quality_prepare_plan.prepare_plan_payload(record.get("plan") or {})
            self.progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "configure",
                unit="commit",
                total=1,
                metadata={"save_count": 1},
            )
            self.progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "start",
                unit="commit",
                label="提交参考与最终摘要",
            )
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            # R6: a judgment is only committed while the input it was computed on
            # is still live. A human edit, an approval or a changed source
            # between the judgment and this commit makes the run stale instead
            # of adopting a relation the operator may already have contradicted.
            # A record that already committed keeps its result: a later edit is
            # handled where it belongs, by the injection-time checks.
            stale_groups = [
                str(group["group_id"])
                for group in stored_plan.get("groups") or []
                if record.get("committed") is not True
                and str(group.get("group_id") or "") in (stored_plan.get("resolved_groups") or {})
                and str(group.get("input_fingerprint") or "")
                and concept_automation.group_input_fingerprint(
                    str(group["group_id"]),
                    group.get("members") if isinstance(group.get("members"), list) else [],
                    support,
                    unit_sources,
                )
                != str(group.get("input_fingerprint") or "")
            ]
            if stale_groups:
                self.prepare_state.mark_failed_locked(
                    record["prepare_id"],
                    "组辨析所依据的卡片或原文已经变化，本次准备结果已失效。",
                    status="stale",
                )
                raise ConflictError("组辨析所依据的卡片或原文已经变化，本次准备结果已失效。")
            units = [unit for unit in (self.cell.state.get("units") or []) if isinstance(unit, dict)]
            in_scope = [str(unit.get("id")) for unit in units if str(unit.get("id")) in set(stored_plan.get("scope") or [])]
            cards = support.get("cards") or {}
            resolved_groups = stored_plan.get("resolved_groups") or {}
            # A formal human edit is materialized as the explicit protection
            # marker before any automatic decision is taken. This must also
            # happen when this run reuses already-finished units (V2), so the
            # marker never depends on whether a model call happened to touch the
            # card in this particular run.
            for card in cards.values():
                if isinstance(card, dict):
                    concept_automation.mark_human_owned_card(card)

            # A group outcome decides which member may represent the group in
            # which unit (V2-H). Every card of a group is constrained by *all*
            # of its groups by intersecting the assigned units, so the result
            # cannot depend on the order in which the groups were processed, and
            # a card that one group cannot judge is never adopted through
            # another one. A group that only blocks its own cards never blocks
            # the other groups or units.
            group_allowed: dict[str, set[str]] = {}
            group_reasons: dict[str, str] = {}
            group_conflicts: list[dict[str, Any]] = []
            group_cards: set[str] = set()
            representatives: set[str] = set()
            unresolved_cards: set[str] = set()
            for group in stored_plan.get("groups") or []:
                members = group.get("members")
                if not isinstance(members, list) or not members:
                    members = quality_prepare_plan.prepare_group_members(support, group.get("card_ids") or [])
                outcome = resolved_groups.get(group["group_id"]) or {}
                if not isinstance(outcome, Mapping) or not outcome:
                    outcome = {
                        "relation": "oversized" if group.get("oversized") else "unresolved",
                        "payload": {},
                    }
                assignment = concept_automation.group_unit_assignments(
                    str(group["group_id"]),
                    outcome,
                    members=members,
                    unit_sources=unit_sources,
                    unit_ids=in_scope,
                )
                representatives.update(str(item) for item in assignment.get("representatives") or [])
                unresolved_cards.update(str(item) for item in assignment.get("unresolved") or [])
                group_conflicts.extend(assignment.get("conflicts") or [])
                # A3: a locally judged oversized group knows *why* a member has no
                # unit — the unit it claims was never judged (over the pool or over
                # one request). Saying that is honest where a generic "no unit
                # assigned" would hide unfinished work as a decision.
                pending_units = {str(item) for item in (outcome.get("units_pending") or [])}
                oversized_units = {str(item) for item in (outcome.get("units_oversized") or [])}
                for member in members:
                    card_id = str(member.get("card_id") or "")
                    if not card_id:
                        continue
                    group_cards.add(card_id)
                    assigned = set(assignment["assignments"].get(card_id) or [])
                    current = group_allowed.get(card_id)
                    group_allowed[card_id] = assigned if current is None else (current & assigned)
                    if not assigned and card_id not in (assignment.get("excluded") or {}):
                        reason = "组辨析没有给这张卡分配任何单元，本次不采用。"
                        claims = {
                            str(item.get("unit_id") or "")
                            for item in ((member.get("payload") or {}).get("evidence") or [])
                            if isinstance(item, Mapping)
                        }
                        if claims & pending_units:
                            reason = "该单元未在本次的额外请求预算内完成局部辨析，留待下次确认。"
                        elif claims & oversized_units:
                            reason = "该单元的争用集合超过单次请求上限，本次不采用。"
                        group_reasons.setdefault(card_id, reason)
                for card_id, reason in (assignment.get("excluded") or {}).items():
                    group_cards.add(str(card_id))
                    group_allowed[str(card_id)] = set()
                    group_reasons.setdefault(str(card_id), str(reason))

            adopted: list[dict[str, Any]] = []
            skipped: list[tuple[str, str]] = []
            for card_id, card in sorted(cards.items()):
                if not isinstance(card, dict):
                    continue
                if card_id in group_cards:
                    assigned = group_allowed.get(card_id) or set()
                    if not assigned:
                        skipped.append(
                            (card_id, group_reasons.get(card_id) or "该卡所属的相关组没有可用的辨析结论，本次不采用。")
                        )
                        continue
                if concept_automation.is_manual_protected(card):
                    skipped.append((card_id, "有人工决定或批准内容，自动准备不采用。"))
                    continue
                verdict, reason = concept_automation.adoption_eligibility(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if verdict != "adopt":
                    skipped.append((card_id, reason))
                    continue
                allowed = concept_automation.applicable_unit_ids(
                    card, unit_sources=unit_sources, unit_ids=in_scope
                )
                if card_id in group_cards:
                    # The judgment owns the relation, the card's own evidence owns
                    # the scope: both must allow the unit.
                    allowed = [unit for unit in allowed if unit in (group_allowed.get(card_id) or set())]
                if not allowed:
                    skipped.append((card_id, "这张卡没有可自动采用的适用单元。"))
                    continue
                adopted.append(
                    concept_automation.make_decision(
                        card,
                        verdict="adopt",
                        reason="内容、证据与检查结论均有效，可自动采用。",
                        allowed_unit_ids=allowed,
                        unit_sources=unit_sources,
                        prepare_id=record["prepare_id"],
                        representative_id=card_id if card_id in representatives else "",
                    )
                )

            automation = concept_automation.normalize_automation(support.get("automation"))
            previous_prepare = automation.get("prepare")
            # Idempotency: committing the same plan twice returns the same result
            # and must not bump versions or re-write decisions.
            if (
                isinstance(previous_prepare, Mapping)
                and str(previous_prepare.get("prepare_id") or "") == record["prepare_id"]
                and bool(previous_prepare.get("committed"))
            ):
                return {
                    "status": "ok",
                    "phase": "commit",
                    "idempotent": True,
                    "prepare_id": record["prepare_id"],
                    "adopted": int((previous_prepare.get("counts") or {}).get("adopted") or 0),
                    "reference_revision": int(automation.get("reference_revision") or 0),
                    "revision": int(support.get("revision") or 0),
                    "summary": quality_prepare_record.prepare_summary_from_record(previous_prepare),
                }

            old_support = copy.deepcopy(self.cell.state.get("quality_support"))
            old_events = copy.deepcopy(self.cell.state.get("events") or [])
            decisions = automation.get("decisions") or {}
            decisions_before = copy.deepcopy(dict(decisions))
            # R4 + C2: this commit owns the whole scope, but "not adopted this
            # round" is not by itself evidence against a card. A decision is
            # revoked when the live data really invalidates it, or when a group
            # judgment that **finished** answered for its cards and still left the
            # card unadopted. A request that failed, was truncated, or was never
            # paid for proves nothing about the card, so its still-valid decision
            # stays and is recorded as unrefreshed instead of silently dropping a
            # reference that the sources still support.
            scope_set = set(stored_plan.get("scope") or [])
            adopted_ids = {decision["card_id"] for decision in adopted}
            answered_group_cards: set[str] = set()
            for group in stored_plan.get("groups") or []:
                outcome = resolved_groups.get(str(group.get("group_id"))) or {}
                if str(outcome.get("relation") or "") in {"equivalent", "distinct"}:
                    answered_group_cards.update(str(card) for card in group.get("card_ids") or [])
            # A decision is kept only when this round really could not answer for
            # the card: a recorded check/judgment failure (a failed or truncated
            # request, a stopped run, a lookup that could not run). A run that
            # finished cleanly and still left the card unadopted did so for a
            # reason of its own, and keeps the R4 behavior.
            round_failure = str(record.get("errors") and record["errors"][0] or "").strip()
            revoked: list[str] = []
            kept_unrefreshed: list[dict[str, str]] = []
            for card_id, card in cards.items():
                decision = decisions.get(card_id)
                if not isinstance(decision, Mapping) or str(decision.get("verdict") or "") != "adopt":
                    continue
                touches_scope = bool(set(str(u) for u in decision.get("allowed_unit_ids") or []) & scope_set)
                if not touches_scope or card_id in adopted_ids:
                    continue
                problems = concept_automation.decision_problems(
                    decision, card, unit_sources=unit_sources
                )
                if problems or card_id in answered_group_cards or not round_failure:
                    decisions.pop(card_id, None)
                    revoked.append(card_id)
                    continue
                kept_unrefreshed.append(
                    {
                        "card_id": card_id,
                        "reason": (
                            "本轮没能完成该卡的重新确认（调用失败或未支付，非内容失效）；"
                            "卡片内容、证据与身份仍然有效，保留旧决定。本轮首个失败："
                            f"{round_failure[:120]}"
                        ),
                    }
                )
            for decision in adopted:
                card_id = decision["card_id"]
                fresh = {key: value for key, value in decision.items() if key != "draft"}
                stored = decisions_before.get(card_id)
                # Unchanged input keeps the original decision record (and its
                # run id) instead of re-stamping it as if something changed.
                decisions[card_id] = (
                    copy.deepcopy(dict(stored))
                    if same_automatic_decision(stored, fresh)
                    else fresh
                )
            automation["decisions"] = decisions
            # The revision counts real reference changes: a run that reuses
            # everything and adopts the same cards again must not move it.
            changed_decisions = sum(
                1
                for card_id in set(decisions_before) | set(decisions)
                if decisions_before.get(card_id) != decisions.get(card_id)
            )
            automation["reference_revision"] = (
                int(automation.get("reference_revision") or 0) + changed_decisions
            )
            failures = int(record["counts"].get("failed_units") or 0)
            plan_status = str(record.get("status") or "")
            scope_size = len(stored_plan.get("scope") or [])
            group_failures = sum(
                1
                for outcome in resolved_groups.values()
                if str((outcome or {}).get("relation") or "") == "failed"
            )
            if failures and failures >= scope_size:
                # Every planned unit failed: this is a failure, not a completion.
                final_status = "failed"
            elif failures or group_failures or plan_status in ("partial", "failed", "interrupted", "stale"):
                # A failed group call is a partial result: the groups that could
                # be judged still apply, and the summary must say so.
                final_status = "partial"
            elif (
                int(record["counts"].get("local_units_pending") or 0)
                or int(record["counts"].get("budget_pending") or 0)
                or int(record["counts"].get("recheck_pending") or 0)
                or int(record["counts"].get("lookup_deferred") or 0)
            ):
                # Work this run could not finish (an unpaid unit, a re-check that
                # did not fit, a deferred lookup) is not a completion. The result
                # is still usable — that is what "partial" means — but it must
                # never be reported as "everything is done".
                final_status = "partial"
            else:
                final_status = "complete"

            reused_units = sum(
                1 for row in record.get("unit_results") or [] if str(row.get("status") or "") == "reused"
            )
            processed_units = sum(
                1
                for row in record.get("unit_results") or []
                if str(row.get("status") or "") in ("completed", "failed")
            )
            reused_groups = sum(
                1
                for outcome in resolved_groups.values()
                if isinstance(outcome, Mapping) and outcome.get("reused") is True
            )
            judged_groups = sum(
                1
                for outcome in resolved_groups.values()
                if isinstance(outcome, Mapping)
                and not outcome.get("reused")
                and str(outcome.get("relation") or "") in concept_automation.REUSABLE_RELATIONS
            )
            # A5 split: an answered question is not the same thing as an adopted
            # card, and an unanswered one is the work the page has to show. Both
            # are counted on the live cards, adopted and not adopted separately.
            ai_resolved_cards = 0
            resolved_questions = 0
            remaining_questions = 0
            for decision in adopted:
                card = cards.get(decision["card_id"])
                if not isinstance(card, dict):
                    continue
                draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
                if not [q for q in (draft.get("open_questions") or []) if str(q).strip()]:
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None:
                    continue
                ai_resolved_cards += 1
                resolved_questions += sum(
                    1 for row in assessment["question_results"] if row["status"] == "resolved"
                )
            for card_id, _reason in skipped:
                card = cards.get(card_id)
                if not isinstance(card, dict):
                    continue
                draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
                questions = [q for q in (draft.get("open_questions") or []) if str(q).strip()]
                if not questions:
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None:
                    # No usable structured answer: every question is still open.
                    remaining_questions += len(questions)
                    continue
                remaining_questions += sum(
                    1 for row in assessment["question_results"] if row["status"] == "unresolved"
                )

            reason_counts: dict[str, int] = {}
            for _card_id, reason in skipped:
                text = str(reason or "").strip()
                if text:
                    reason_counts[text] = reason_counts.get(text, 0) + 1
            not_adopted_reasons = [
                {"reason": reason, "count": count}
                for reason, count in sorted(reason_counts.items(), key=lambda row: (-row[1], row[0]))[:8]
            ]

            def finalize(record: dict[str, Any]) -> None:
                record["counts"]["adopted"] = len(adopted)
                record["counts"]["skipped"] = len(skipped) + int(record["counts"].get("skipped") or 0)
                record["counts"]["unresolved"] = sum(
                    1 for card_id in unresolved_cards if isinstance(cards.get(card_id), dict)
                )
                record["counts"]["protected"] = sum(
                    1
                    for card_id, card in cards.items()
                    if isinstance(card, dict) and concept_automation.is_manual_protected(card)
                )
                record["counts"]["ineligible"] = len(skipped)
                record["counts"]["uncovered_units"] = sum(
                    1
                    for unit_id in in_scope
                    if not any(str(unit_id) in (d.get("allowed_unit_ids") or []) for d in adopted)
                )
                record["counts"]["ai_resolved_cards"] = ai_resolved_cards
                record["counts"]["resolved_questions"] = resolved_questions
                record["counts"]["remaining_questions"] = remaining_questions
                record["counts"]["reused_units"] = reused_units
                record["counts"]["processed_units"] = processed_units
                record["counts"]["reused_groups"] = reused_groups
                record["counts"]["judged_groups"] = judged_groups
                record["not_adopted_reasons"] = copy.deepcopy(not_adopted_reasons)
                record["group_conflicts"] = copy.deepcopy(group_conflicts[:20])
                record["status"] = final_status
                record["committed"] = True
                record["revoked_decisions"] = revoked
                record["kept_unrefreshed_decisions"] = copy.deepcopy(kept_unrefreshed)
                record["finished_at"] = self.clock()

            finalize(record)
            automation["prepare"] = record
            support["automation"] = automation
            append_event(
                self.cell,
                "quality_prepare_committed",
                f"自动参考准备完成：采用 {len(adopted)} 张，跳过 {len(skipped)} 张；"
                f"未采用项不阻塞翻译。",
                None,
                {"prepare_id": record["prepare_id"]},
                self.clock,
            )
            try:
                commit_quality_support(self.cell, support, old_support=old_support, old_events=old_events)
            except Exception as exc:
                self.progress.change(
                    str(record.get("prepare_id") or ""),
                    "commit",
                    "commit",
                    "failed",
                    unit="commit",
                    error=f"最终保存失败：{str(exc)[:240]}",
                )
                raise
            self.progress.change(
                str(record.get("prepare_id") or ""),
                "commit",
                "commit",
                "complete",
                unit="commit",
            )
            return {
                "status": "ok",
                "phase": "commit",
                "idempotent": False,
                "prepare_id": record["prepare_id"],
                "adopted": len(adopted),
                "revoked": revoked,
                "skipped": [{"card_id": card_id, "reason": reason} for card_id, reason in skipped],
                "reference_revision": int(automation.get("reference_revision") or 0),
                "revision": int(support.get("revision") or 0),
                "summary": quality_prepare_record.prepare_summary_from_record(record),
            }

