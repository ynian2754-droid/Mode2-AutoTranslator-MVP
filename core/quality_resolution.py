"""Whole-group and per-unit concept judgments for frozen prepare inputs."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from core import concept_automation, quality_prepare_plan, quality_prepare_record
from core.exceptions import ConflictError
from core.project_state import ProjectStateCell
from core.provider_routing import ProviderRouter
from core.quality_prepare_state import PrepareState
from core.quality_progress import PrepareProgress
from core.quality_state import quality_unit_sources
from core.quality_support import normalize_quality_support
from providers.quality_provider import ConceptResolutionRequest, normalize_resolution


def mark_local_units(
    record: dict[str, Any], *, oversized: int = 0, reused: int = 0
) -> None:
    """Record how an oversized group's units were accounted for this run.

    ``oversized`` is a unit whose complete contention set is larger than one
    request may carry (never truncated); ``reused`` is a unit an earlier
    confirmation already judged and this run hands over without asking
    again. Neither is work of this run, and neither may be reported as one.
    """

    counts = record.setdefault("counts", {})
    if oversized:
        counts["local_units_oversized"] = int(counts.get("local_units_oversized") or 0) + int(
            oversized
        )
    if reused:
        counts["local_units_reused"] = int(counts.get("local_units_reused") or 0) + int(reused)


class PrepareResolution:
    def __init__(
        self,
        cell: ProjectStateCell,
        prepare_state: PrepareState,
        progress: PrepareProgress,
        router: ProviderRouter,
    ) -> None:
        self.cell = cell
        self.prepare_state = prepare_state
        self.progress = progress
        self.router = router

    def resolve_pending(self, prepare_id: str) -> None:
        """Judge every pending related group through the resolution channel."""

        with self.cell.lock:
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            record = quality_prepare_record.prepare_record(support, {"prepare_id": prepare_id})
            stored_plan = quality_prepare_plan.prepare_plan_payload(record.get("plan") or {})
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            resolved = dict(stored_plan.get("resolved_groups") or {})
            scope_units = [str(unit_id) for unit_id in stored_plan.get("scope") or []]
            pending: list[dict[str, Any]] = []
            # A3: oversized groups are no longer skipped as a whole. Their local
            # contention sets are judged per unit, so a unit whose complete set
            # fits the call budget still gets a judgment while the rest keeps
            # waiting — nothing is truncated and no member is pre-filtered.
            local_pending: list[dict[str, Any]] = []
            local_oversized: set[str] = set()
            local_reused = 0
            local_total = 0
            for group in stored_plan.get("groups") or []:
                existing = resolved.get(group["group_id"])
                # A group that already carries a whole-group relation is done. An
                # oversized one is not: its outcome may be a per-unit partial
                # result, and the units it could not pay for are still owed a
                # judgment. Those are continued below instead of being reported as
                # finished, and the units already judged are handed over as is.
                if existing is not None and not group.get("oversized"):
                    continue
                # The frozen members are what the judgment may read. A group
                # frozen before this round may legitimately have no members yet.
                members = group.get("members")
                if not isinstance(members, list) or not members:
                    members = quality_prepare_plan.prepare_group_members(support, group.get("card_ids") or [])
                # The input was frozen right after the batches. If the live cards
                # or sources no longer match it, the judgment would be computed
                # on something the operator already changed: refuse, don't guess.
                frozen = str(group.get("input_fingerprint") or "")
                live = concept_automation.group_input_fingerprint(
                    group["group_id"], members, support, unit_sources
                )
                if frozen and frozen != live:
                    raise ConflictError(
                        "组辨析开始前，相关卡片或原文已经变化，本次准备结果已失效。"
                    )
                if group.get("oversized"):
                    # A3: judged whole is impossible, judged per unit is not. The
                    # contention set of one unit keeps every member with valid
                    # evidence for it — supported, disputed and undecided alike —
                    # so a local judgment can never be "conflict-free" because a
                    # member was filtered out first.
                    plans = concept_automation.local_contention_plans(
                        group["group_id"],
                        members,
                        unit_sources=unit_sources,
                        unit_ids=scope_units,
                    )
                    prior_units = concept_automation.local_judgment_payloads(existing)
                    local_total += len(plans["requests"]) + len(plans["oversized_units"]) + len(prior_units)
                    remaining = [
                        row for row in plans["requests"] if str(row["unit_id"]) not in prior_units
                    ]
                    local_oversized.update(plans["oversized_units"])
                    # Units judged by an earlier confirmation are handed over
                    # without a new request; they are counted as reused work, not
                    # as work of this run.
                    local_reused += len(prior_units)
                    if remaining:
                        local_pending.append(
                            {
                                **group,
                                "members": members,
                                "input_fingerprint": frozen or live,
                                "local": {
                                    "requests": remaining,
                                    "oversized_units": plans["oversized_units"],
                                },
                                "prior_units": prior_units,
                            }
                        )
                    continue
                if len(members) < 2:
                    resolved[group["group_id"]] = {
                        "relation": "skipped",
                        "reason": "组内没有两张可自动管理的卡片。",
                        "payload": {},
                    }
                    continue
                # A judgment can only matter when at least one member could still
                # be adopted: a card whose check failed, never existed, or whose
                # evidence is outside this scope is unadoptable before the group
                # is even read. Judging such a group spends a call that cannot
                # produce a committable reference (C2: 19 of the 21 groups that
                # needed a judgment were exactly this), while a group with one
                # usable member keeps being judged as before.
                cards_now = support.get("cards") or {}
                if not any(
                    concept_automation.adoption_eligibility(
                        cards_now.get(str(member.get("card_id") or "")) or {},
                        unit_sources=unit_sources,
                        unit_ids=scope_units,
                    )[0]
                    == "adopt"
                    for member in members
                ):
                    resolved[group["group_id"]] = {
                        "relation": "skipped",
                        "reason": (
                            "组内没有可用于自动采用的检查结论"
                            "（检查未完成、未通过或证据不在本次范围内），本次不辨析。"
                        ),
                        "payload": {},
                    }
                    continue
                pending.append({**group, "members": members, "input_fingerprint": frozen or live})
            if resolved != (stored_plan.get("resolved_groups") or {}):
                stored_plan["resolved_groups"] = resolved
                # persist the skipped ones so the plan stays the single source
                with self.cell.lock:
                    self.prepare_state.update_plan_locked(prepare_id, stored_plan)
            if local_oversized or local_reused:
                self.prepare_state.update_record_locked(
                    support,
                    prepare_id,
                    lambda record: mark_local_units(
                        record, oversized=len(local_oversized), reused=local_reused
                    ),
                )
            normal_groups = [
                group for group in stored_plan.get("groups") or [] if not group.get("oversized")
            ]
            normal_pending_ids = {str(group.get("group_id") or "") for group in pending}
            normal_resolved = dict(stored_plan.get("resolved_groups") or {})
            normal_reused = sum(
                1
                for group in normal_groups
                if isinstance(normal_resolved.get(str(group.get("group_id") or "")), Mapping)
                and normal_resolved[str(group.get("group_id") or "")].get("reused") is True
            )
            normal_failed = sum(
                1
                for group in normal_groups
                if str((normal_resolved.get(str(group.get("group_id") or "")) or {}).get("relation") or "")
                in {"failed", "unresolved"}
                and str(group.get("group_id") or "") not in normal_pending_ids
            )
            normal_not_required = sum(
                1
                for group in normal_groups
                if str((normal_resolved.get(str(group.get("group_id") or "")) or {}).get("relation") or "")
                == "skipped"
            )
            normal_completed = max(
                0,
                len(normal_groups)
                - len(normal_pending_ids)
                - normal_reused
                - normal_failed
                - normal_not_required,
            )
            self.progress.change(
                prepare_id,
                "group_resolution",
                "group-plan",
                "configure",
                unit="group",
                total=len(normal_groups),
                metadata={
                    "completed": normal_completed,
                    "failed": normal_failed,
                    "reused": normal_reused,
                    "not_required": normal_not_required,
                    "provider_group_count": len(pending),
                },
            )
            self.progress.change(
                prepare_id,
                "local_resolution",
                "local-plan",
                "configure",
                unit="group_unit",
                total=local_total,
                metadata={
                    "reused": local_reused,
                    "oversized_units": len(local_oversized),
                    "request_count": sum(
                        len(item.get("local", {}).get("requests") or []) for item in local_pending
                    ),
                },
            )
            if not pending and not local_pending:
                return

        _generation, _checker, _editorial, resolver = self.router.quality_channels()
        for group in pending:
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
            unit_refs = {}
            for member in group["members"]:
                for evidence in (member["payload"].get("evidence") or []):
                    unit_id = str(evidence.get("unit_id") or "")
                    if unit_id and unit_id in unit_sources:
                        unit_refs[unit_id] = unit_sources[unit_id]
            request = ConceptResolutionRequest(
                project_id=str(self.cell.state.get("project", {}).get("id") or ""),
                group_id=group["group_id"],
                members=tuple(group["members"]),
                unit_sources=unit_refs,
                input_fingerprint=group["input_fingerprint"],
                # Every model request of the judgment — the first and every
                # repair round — is authorized by the live prepare before it
                # is sent.
                control=self.prepare_state.group_control_locked(
                    prepare_id,
                    group["group_id"],
                    group["input_fingerprint"],
                    progress_stage="group_resolution",
                    progress_item_id=str(group["group_id"]),
                ),
            )
            group_id = str(group["group_id"])
            self.progress.change(
                prepare_id,
                "group_resolution",
                group_id,
                "start",
                unit="group",
                label="相关概念辨析",
                metadata={"group_id": group_id, "member_count": len(group["members"])},
                provider_channel="resolution",
            )
            request_error = ""
            repair_rounds = 0
            outcome: dict[str, Any]
            try:
                result = resolver.resolve_group(request)
                # A double that reports its own call count must be read once.
            except ConflictError:
                # The judgment was invalidated while it was in flight (or the
                # controller refused the next request): this is not a group
                # failure to record, it ends the whole run as stale.
                self.progress.change(
                    prepare_id,
                    "group_resolution",
                    group_id,
                    "failed",
                    unit="group",
                    error="组辨析输入身份已经变化。",
                )
                raise
            except Exception as exc:
                outcome = {"relation": "failed", "reason": f"组辨析失败：{exc}", "payload": {}}
                request_error = str(exc)
                # A failed judgment still spent its content rounds. The repair
                # summary travels on the exception, so the counter can report
                # them instead of a bare zero that hides real calls.
                spent = getattr(exc, "outcome", None)
                if spent is not None:
                    repair_rounds = max(0, int(getattr(spent, "round", 1) or 1) - 1)
            else:
                try:
                    validated = normalize_resolution(
                        result.payload,
                        member_ids={str(item["card_id"]) for item in group["members"]},
                        group_id=group["group_id"],
                        source_units=unit_refs,
                    )
                except Exception as exc:
                    outcome = {"relation": "unresolved", "reason": f"组辨析结果未通过本地校验：{exc}", "payload": {}}
                else:
                    outcome = {
                        "relation": str(validated["relation"]),
                        "reason": "",
                        "payload": validated,
                        "repair": result.repair,
                    }
                    # ``repair`` reports the round that produced the answer, so
                    # the repair rounds are the extra content rounds beyond the
                    # first attempt (round 1 means none were needed).
                    repair_rounds = max(0, int(((result.repair or {}).get("round") or 1)) - 1)
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                # The result may only be stored while the input is still the one
                # the model was shown. A formal human edit, an approval or a
                # changed source in the meantime makes this outcome stale.
                self.prepare_state.group_fresh_locked(
                    prepare_id, group["group_id"], group["input_fingerprint"]
                )
                support = normalize_quality_support(self.cell.state.get("quality_support"))

                def mutate_group(
                    record: dict[str, Any],
                    group=group,
                    outcome=outcome,
                    request_error=request_error,
                    repair_rounds=repair_rounds,
                ) -> None:
                    record["requests"]["resolution"] = int(record["requests"].get("resolution") or 0) + 1
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + repair_rounds
                    plan = record.get("plan") or {}
                    resolved = dict(plan.get("resolved_groups") or {})
                    resolved[group["group_id"]] = outcome
                    plan["resolved_groups"] = resolved
                    record["plan"] = plan
                    if request_error:
                        record["errors"].append(f"组 {group['group_id']}：{request_error}")

                self.prepare_state.update_record_locked(support, prepare_id, mutate_group)
            self.progress.change(
                prepare_id,
                "group_resolution",
                group_id,
                "failed" if request_error or str(outcome.get("relation") or "") == "unresolved" else "complete",
                unit="group",
                error=request_error or str(outcome.get("reason") or "")
                if request_error or str(outcome.get("relation") or "") == "unresolved"
                else "",
            )

        # A3: the per-unit local judgments of the oversized groups. Each request
        # carries the unit's **complete** contention set and only that unit's
        # source, so the answer can neither speak for another unit nor for a
        # member that was left out. Every request is paid for from the one shared
        # budget before it is sent; a refusal leaves the unit pending, never
        # judged by a smaller set.
        for item in local_pending:
            group = item
            prior_units = dict(item.get("prior_units") or {})
            judgements: list[dict[str, Any]] = []
            judged_units: list[str] = []
            pending_units: list[str] = []
            failures: list[str] = []
            local_completed_items: list[tuple[str, str]] = []
            for plan_row in item["local"]["requests"]:
                unit_id = str(plan_row["unit_id"])
                member_ids = {str(card_id) for card_id in plan_row["member_ids"]}
                members = [
                    member
                    for member in item["members"]
                    if str(member.get("card_id") or "") in member_ids
                ]
                with self.cell.lock:
                    self.prepare_state.guard_locked(prepare_id)
                    charged = False

                    def mutate_charge(record: dict[str, Any]) -> None:
                        nonlocal charged
                        charged = concept_automation.charge_budget(record, "local_group")

                    self.prepare_state.update_record_locked(support, prepare_id, mutate_charge)
                if not charged:
                    pending_units.append(unit_id)
                    continue
                if unit_id not in unit_sources:
                    pending_units.append(unit_id)
                    continue
                request = ConceptResolutionRequest(
                    project_id=str(self.cell.state.get("project", {}).get("id") or ""),
                    group_id=item["group_id"],
                    members=tuple(members),
                    unit_sources={unit_id: unit_sources[unit_id]},
                    input_fingerprint=item["input_fingerprint"],
                    control=self.prepare_state.group_control_locked(
                        prepare_id,
                        item["group_id"],
                        item["input_fingerprint"],
                        suffix=f":{unit_id}",
                        progress_stage="local_resolution",
                        progress_item_id=f"{item['group_id']}:{unit_id}",
                    ),
                )
                local_item_id = f"{item['group_id']}:{unit_id}"
                self.progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "start",
                    unit="group_unit",
                    label="大组单元局部辨析",
                    metadata={
                        "group_id": str(item["group_id"]),
                        "unit_id": unit_id,
                        "member_count": len(members),
                    },
                    provider_channel="resolution",
                )
                try:
                    result = resolver.resolve_group(request)
                except ConflictError:
                    self.progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error="局部辨析输入身份已经变化。",
                    )
                    raise
                except Exception as exc:
                    failures.append(f"局部辨析失败（{unit_id}）：{exc}")
                    self.progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error=str(exc),
                    )
                    continue
                try:
                    validated = normalize_resolution(
                        result.payload,
                        member_ids=member_ids,
                        group_id=item["group_id"],
                        source_units={unit_id: unit_sources[unit_id]},
                    )
                except Exception as exc:
                    failures.append(f"局部辨析结果未通过本地校验（{unit_id}）：{exc}")
                    self.progress.change(
                        prepare_id,
                        "local_resolution",
                        local_item_id,
                        "failed",
                        unit="group_unit",
                        error=str(exc),
                    )
                    continue
                judgements.append({"unit_id": unit_id, "payload": validated})
                judged_units.append(unit_id)
                local_completed_items.append((local_item_id, unit_id))
                self.progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "awaiting_commit",
                    unit="group_unit",
                )
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                self.prepare_state.group_fresh_locked(
                    prepare_id, item["group_id"], item["input_fingerprint"]
                )
                support = normalize_quality_support(self.cell.state.get("quality_support"))
                # The units judged by an earlier confirmation are merged back in
                # unchanged: this run pays only for the units it actually asked
                # about, and the group's conclusion stays the union of what every
                # per-unit judgment really said.
                new_units = {
                    str(row["unit_id"]): row["payload"]
                    for row in judgements
                    if str(row["unit_id"])
                }
                all_units = {**prior_units, **new_units}
                merged = concept_automation.merge_local_judgments(
                    item["group_id"],
                    judgements=[
                        {"unit_id": unit_id, "payload": payload}
                        for unit_id, payload in all_units.items()
                    ],
                    members=item["members"],
                )
                outcome = {
                    "relation": str((merged or {}).get("relation") or "oversized"),
                    "reason": (
                        ""
                        if merged
                        else "该组过大，只有部分单元能在预算内完成局部辨析；未判断的单元保持待确认。"
                    ),
                    "payload": merged or {},
                    "local": True,
                    # Per-unit judgments, kept apart so the next confirmation can
                    # continue exactly where this one stopped.
                    "units": copy.deepcopy(all_units),
                    "units_judged": sorted(all_units),
                    "units_oversized": sorted(item["local"]["oversized_units"]),
                    "units_pending": sorted(pending_units),
                }
                if not merged and not judged_units and not failures and not prior_units:
                    # Nothing was judged and nothing new is known: keep the group
                    # exactly as an oversized one instead of writing an outcome.
                    outcome = {}

                def mutate_local(
                    record: dict[str, Any],
                    outcome=outcome,
                    judged=judged_units,
                    pending_units=pending_units,
                    failures=failures,
                ) -> None:
                    if outcome:
                        plan = record.get("plan") or {}
                        resolved_now = dict(plan.get("resolved_groups") or {})
                        resolved_now[item["group_id"]] = copy.deepcopy(outcome)
                        plan["resolved_groups"] = resolved_now
                        record["plan"] = plan
                        record["counts"]["local_groups"] = int(
                            record["counts"].get("local_groups") or 0
                        ) + 1
                    if judged:
                        record["counts"]["local_units_judged"] = int(
                            record["counts"].get("local_units_judged") or 0
                        ) + len(judged)
                    if pending_units:
                        # An unpayable unit is recorded as unfinished work, even
                        # when no conclusion was written at all: a run that could
                        # not afford the judgment must not look like one that
                        # found nothing to judge.
                        record["counts"]["local_units_pending"] = int(
                            record["counts"].get("local_units_pending") or 0
                        ) + len(pending_units)
                        record["counts"]["budget_pending"] = int(
                            record["counts"].get("budget_pending") or 0
                        ) + len(pending_units)
                    if judged:
                        record["requests"]["resolution"] = int(
                            record["requests"].get("resolution") or 0
                        ) + len(judged)
                    for failure in failures:
                        record["errors"].append(failure)

                self.prepare_state.update_record_locked(support, prepare_id, mutate_local)
            for local_item_id, unit_id in local_completed_items:
                self.progress.change(
                    prepare_id,
                    "local_resolution",
                    local_item_id,
                    "complete",
                    unit="group_unit",
                    metadata={"unit_id": unit_id},
                )

