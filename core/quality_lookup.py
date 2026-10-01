"""Bounded prepare lookup execution, display history, and per-card dedup state."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation, quality_prepare_plan, quality_prepare_record, quality_requests
from core.exceptions import ConflictError
from core.project_state import ProjectStateCell
from core.provider_routing import ProviderRouter
from core.quality_limits import MAX_LOOKUP_UNITS_PER_EXPRESSION
from core.quality_prepare_state import PrepareState
from core.quality_progress import PrepareProgress
from core.quality_state import quality_unit_sources
from core.quality_support import normalize_quality_support, refresh_check_result
from providers.quality_provider import ConceptCheckRequest


def append_lookup_ledger(
    record: dict[str, Any],
    *,
    identity: str,
    status: str,
    card_ids: Sequence[str],
    expressions: Sequence[str],
    clock: Callable[[], str],
) -> None:
    """Add one line to the bounded **display** log of executed lookups.

    The log is what the page reads: the request-level identity of what was
    asked and how it ended (completed / partial / failed / rejected). It is
    trimmed to ``MAX_LOOKUP_LEDGER`` rows, and it deliberately holds no
    decision: whether a question has already been answered is decided by the
    per-card dedup state, so trimming this log can never make the project pay
    for the same work twice. A refused request is never logged — it was not
    executed.
    """

    if not identity:
        return
    rows = [
        row
        for row in (record.get("lookups") or [])
        if isinstance(row, Mapping) and str(row.get("identity") or "") != str(identity)
    ]
    rows = rows[-(concept_automation.MAX_LOOKUP_LEDGER - 1) :]
    rows.append(
        {
            "identity": str(identity),
            "status": str(status or "failed"),
            "cards": [str(item) for item in card_ids][:40],
            "expressions": [str(item) for item in expressions][:16],
            "prepare_id": str(record.get("prepare_id") or ""),
            "at": clock(),
        }
    )
    record["lookups"] = rows


def record_lookup_state(
    record: dict[str, Any],
    *,
    pairs: Sequence[Mapping[str, Any]],
    live_cards: Sequence[str],
    clock: Callable[[], str],
) -> None:
    """Record which card spent its one bounded lookup, and how it ended.

    This is the state that stops a lookup from being paid for twice — not the
    display log: the log is trimmed for the page, this map keeps one row per
    card and content version, so an answer that rewrites its own conclusion or
    proposes new expressions never mints a new allowance, while a changed
    draft, source, read material or formally replaced conclusion still does.
    Rows of cards that no longer exist are dropped.
    """

    state = concept_automation.lookup_state_of(record.get("lookup_state"))
    stamp = clock()
    for pair in pairs:
        concept_automation.lookup_state_set(
            state,
            str(pair.get("card_id") or ""),
            version=str(pair.get("version") or ""),
            check=str(pair.get("check") or ""),
            expressions=[str(item) for item in (pair.get("expressions") or [])],
            units=[
                dict(item)
                for item in (pair.get("units") or [])
                if isinstance(item, Mapping)
            ],
            status=str(pair.get("status") or "failed"),
            at=stamp,
        )
    record["lookup_state"] = concept_automation.lookup_state_pruned(state, live_cards)


class PrepareLookup:
    def __init__(
        self,
        cell: ProjectStateCell,
        prepare_state: PrepareState,
        progress: PrepareProgress,
        router: ProviderRouter,
        clock: Callable[[], str],
    ) -> None:
        self.cell = cell
        self.prepare_state = prepare_state
        self.progress = progress
        self.router = router
        self.clock = clock

    def bounded_lookup(self, prepare_id: str) -> None:
        """Spend at most one extra request on the deterministic lookup of this run.

        The search itself is local and free: it reads the frozen scope's own
        sources with the project's orthographic rules and returns excerpts sliced
        out of the original text. The scope of this run is chosen **before** the
        request is built: a card whose exact question (content version, evidence,
        current conclusion and the material its hits were read from, keyed per
        card) was already executed is counted as settled and never asked again,
        while the rest are
        packed into one request inside the plan's input bounds (10 cards, 10
        source units, 4000 English words, 24000 characters — whichever is reached
        first). A card that does not fit is recorded as unfinished and the scan
        continues, so one oversized card cannot starve the cards behind it, and
        no evidence is ever trimmed to make a request look complete.

        The one request is charged to the shared budget; a refusal leaves every
        card pending without a single write. The answer is written only while the
        frozen identity still matches, and the state each answer leaves behind is
        recorded as "this exact question was answered" — so a lookup can never
        re-trigger itself through its own conclusion. Nothing here adopts a card,
        widens the scope or retries a failure.
        """

        with self.cell.lock:
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            record = quality_prepare_record.prepare_record(support, {"prepare_id": prepare_id})
            state = concept_automation.lookup_state_of(record.get("lookup_state"))
            stored_plan = quality_prepare_plan.prepare_plan_payload(record.get("plan") or {})
            scope = [str(unit_id) for unit_id in stored_plan.get("scope") or []]
            self.prepare_state.guard_locked(prepare_id)
            scope_set = set(scope)
            candidates: list[dict[str, Any]] = []
            wanted: list[str] = []
            for card_id in sorted((support.get("cards") or {}).keys()):
                card = (support.get("cards") or {}).get(card_id)
                if not isinstance(card, dict) or str(card.get("status") or "") != "pending_review":
                    continue
                if concept_automation.is_manual_protected(card):
                    continue
                draft = card.get("draft")
                if not isinstance(draft, Mapping):
                    continue
                unit_ids = [
                    str(item.get("unit_id") or "")
                    for item in (draft.get("evidence") or [])
                    if isinstance(item, Mapping) and str(item.get("unit_id") or "")
                ]
                if not unit_ids or not (set(unit_ids) & scope_set):
                    continue
                assessment = concept_automation.assessment_of(card, unit_sources=unit_sources)
                if assessment is None or not assessment.get("lookup_expressions"):
                    continue
                check = card.get("check") if isinstance(card.get("check"), Mapping) else None
                fingerprint = quality_requests.assessment_context_payload(
                    dict(draft), unit_sources=unit_sources, model=""
                )["content_fingerprint"]
                if not fingerprint:
                    continue
                expressions = [
                    str(item) for item in assessment["lookup_expressions"] if str(item).strip()
                ]
                if not expressions:
                    continue
                candidates.append(
                    {
                        "card_id": str(card_id),
                        "draft_revision": int(card.get("draft_revision") or 0),
                        "content_fingerprint": fingerprint,
                        # The conclusion the card holds right now is what the
                        # lookup would ask about; it is recorded only to name the
                        # question, never as a licence for another request.
                        "assessment_fingerprint": concept_automation.assessment_fingerprint(
                            assessment
                        ),
                        # "One bounded lookup per content version": the version is
                        # the draft + its cited sources, and the check digest tells
                        # the lookup's own write apart from a formal re-check.
                        "version": concept_automation.lookup_content_version(
                            {
                                "card_id": card_id,
                                "draft_revision": int(card.get("draft_revision") or 0),
                                "content_fingerprint": fingerprint,
                                "unit_ids": unit_ids,
                            },
                            unit_sources=unit_sources,
                        ),
                        "check": concept_automation.check_fingerprint(check),
                        "draft": copy.deepcopy(dict(draft)),
                        "unit_ids": unit_ids,
                        "expressions": expressions,
                        "verdict": str((check or {}).get("verdict") or ""),
                    }
                )
                for expression in expressions:
                    if expression not in wanted:
                        wanted.append(expression)
        if not candidates:
            self.progress.change(
                prepare_id,
                "lookup",
                "lookup-none",
                "configure",
                unit="request",
                total=0,
                metadata={"candidate_cards": 0, "request_count": 0},
            )
            return
        hits = concept_automation.lookup_occurrences(
            wanted,
            unit_sources=unit_sources,
            unit_ids=scope,
            max_units_per_expression=MAX_LOOKUP_UNITS_PER_EXPRESSION,
        )
        material = {expression: rows for expression, rows in hits.items() if rows}
        # Which cards still owe a lookup, which were already answered and which
        # asked for an expression the project's own text has no occurrence of.
        settled: list[str] = []
        misses: list[str] = []
        pending: list[dict[str, Any]] = []
        for candidate in candidates:
            asked = [
                expression for expression in candidate["expressions"] if expression in material
            ]
            if not asked:
                misses.append(candidate["card_id"])
                continue
            candidate["asked"] = asked
            # The material this card's lookup reads: the excerpts found for its
            # own expressions, including the hit units its evidence never
            # mentioned. Recording it is what makes a later edit of one of those
            # sources a changed question instead of a settled one.
            candidate["material_units"] = concept_automation.lookup_material_units(
                asked, material
            )
            # "疑问每个内容版本最多一次额外补查"：额度只看内容版本、检查摘要与
            # 真正读过的材料——补查自己的回答（改 guidance、轮换或新增表达）不再
            # 产生新额度。
            if concept_automation.lookup_allowance_spent(
                state,
                candidate["card_id"],
                version=candidate["version"],
                check=candidate["check"],
                unit_sources=unit_sources,
            ):
                settled.append(candidate["card_id"])
            else:
                pending.append(candidate)
        if settled or misses:
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                support = normalize_quality_support(self.cell.state.get("quality_support"))

                def mutate_classified(
                    record: dict[str, Any], settled=len(settled), misses=len(misses)
                ) -> None:
                    if settled:
                        record["counts"]["lookup_settled"] = (
                            int(record["counts"].get("lookup_settled") or 0) + settled
                        )
                    if misses:
                        record["counts"]["lookup_misses"] = (
                            int(record["counts"].get("lookup_misses") or 0) + misses
                        )

                self.prepare_state.update_record_locked(support, prepare_id, mutate_classified)
        if not pending:
            self.progress.change(
                prepare_id,
                "lookup",
                "lookup-none-pending",
                "configure",
                unit="request",
                total=0,
                metadata={
                    "candidate_cards": len(candidates),
                    "settled_cards": len(settled),
                    "no_hit_cards": len(misses),
                    "request_count": 0,
                },
            )
            return
        selection = concept_automation.lookup_batch(
            pending,
            unit_sources=unit_sources,
            material=material,
            limits=concept_automation.LOOKUP_REQUEST_LIMITS,
        )
        items: list[dict[str, Any]] = selection["cards"]
        deferred = [row["card_id"] for row in selection["deferred"]]
        if not items:
            # Nothing fits this execution's input bounds: the work stays
            # unfinished and is retried by a later confirmation — never cut down
            # to size and never sent as a partial question.
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                support = normalize_quality_support(self.cell.state.get("quality_support"))

                def mutate_deferred(record: dict[str, Any], count=len(pending)) -> None:
                    record["counts"]["lookup_deferred"] = (
                        int(record["counts"].get("lookup_deferred") or 0) + int(count)
                    )

                self.prepare_state.update_record_locked(support, prepare_id, mutate_deferred)
            self.progress.change(
                prepare_id,
                "lookup",
                "lookup-pending",
                "configure",
                unit="request",
                total=1,
                metadata={
                    "candidate_cards": len(pending),
                    "deferred_cards": len(deferred),
                    "request_count": 0,
                },
            )
            return
        # Every unit this request will cite: the cards' own evidence units plus
        # the units the read-only hits were quoted from. A hit whose source is not
        # cited could not be used by the answer, so both travel together.
        cited_units = {unit for item in items for unit in item["unit_ids"]}
        for item in items:
            for expression in item["asked"]:
                for row in material[expression]:
                    cited_units.add(str(row.get("unit_id") or ""))
        request_units = [
            unit_id
            for unit_id in scope
            if unit_id in cited_units and unit_id in unit_sources
        ]
        request_expressions = sorted(
            {expression for item in items for expression in item["asked"]}
        )
        request_material = {
            expression: material[expression] for expression in request_expressions
        }
        # The request-level identity of the display log: what this one request
        # asked. It never decides anything (the per-card state does).
        request_identity = concept_automation.lookup_identity(
            request_expressions, items, request_material
        )
        with self.cell.lock:
            self.prepare_state.guard_locked(prepare_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            _generation, checker, _editorial, _resolution = self.router.quality_channels()
            charged = False

            def mutate_charge(record: dict[str, Any]) -> None:
                nonlocal charged
                charged = concept_automation.charge_budget(record, "lookup")

            self.prepare_state.update_record_locked(support, prepare_id, mutate_charge)
            if not charged:
                # Zero-modification refusal: the record only learns why the work
                # is still pending, exactly like an unpaid large-group judgment.
                # Nothing was executed, so nothing is recorded as asked.

                def mutate_unpaid(
                    record: dict[str, Any], count=len(pending), deferred=len(deferred)
                ) -> None:
                    record["counts"]["budget_pending"] = (
                        int(record["counts"].get("budget_pending") or 0) + int(count)
                    )
                    if deferred:
                        record["counts"]["lookup_deferred"] = (
                            int(record["counts"].get("lookup_deferred") or 0) + int(deferred)
                        )

                self.prepare_state.update_record_locked(support, prepare_id, mutate_unpaid)
                self.progress.change(
                    prepare_id,
                    "lookup",
                    "lookup-pending",
                    "configure",
                    unit="request",
                    total=1,
                    metadata={
                        "candidate_cards": len(pending),
                        "selected_cards": len(items),
                        "deferred_cards": len(deferred),
                        "budget_refused": True,
                        "request_count": 0,
                    },
                )
                return
            refs = quality_requests.concept_unit_refs_from_sources(request_units, unit_sources)
            project_id = str(self.cell.state.get("project", {}).get("id") or "")
            frozen_hashes = {
                unit_id: str((unit_sources.get(unit_id) or ("", ""))[1])
                for unit_id in request_units
            }
            frozen_cards = {
                item["card_id"]: (item["draft_revision"], item["content_fingerprint"])
                for item in items
            }
            request = ConceptCheckRequest(
                project_id=project_id,
                batch_id=f"lookup-{prepare_id}"[:120],
                units=refs,
                candidates=tuple(copy.deepcopy(item["draft"]) for item in items),
                # The cards and the hit sources this lookup quotes are part of its
                # authorization: a change to either stops the next round before it
                # is sent, and stops the write even after the answer arrives.
                control=self.prepare_state.guard_control_locked(
                    prepare_id, "lookup", cards=frozen_cards, unit_hashes=frozen_hashes
                ),
                lookup_evidence=copy.deepcopy(request_material),
                expected=tuple(
                    {
                        "card_id": item["card_id"],
                        "draft_revision": item["draft_revision"],
                        "content_fingerprint": item["content_fingerprint"],
                    }
                    for item in items
                ),
            )
        lookup_item_id = f"lookup:{prepare_id}"
        self.progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "configure",
            unit="request",
            total=1,
            metadata={
                "candidate_cards": len(pending),
                "selected_cards": len(items),
                "deferred_cards": len(deferred),
                "request_count": 1,
            },
        )
        self.progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "start",
            unit="request",
            label="核实候选疑问",
            metadata={
                "card_count": len(items),
                "unit_count": len(request_units),
                "expression_count": len(request_expressions),
            },
            provider_channel="check",
        )
        asked_pairs = [
            {
                "card_id": item["card_id"],
                "version": item["version"],
                "check": item["check"],
                "expressions": list(item["asked"]),
                "units": list(item["material_units"]),
                "status": "failed",
            }
            for item in items
        ]
        try:
            result = checker.check_candidates(request)
            checks = list(result.checks)
            repair = result.repair
        except ConflictError:
            # A control refusal is not a failed task: the frozen input changed or
            # the run was closed, so this execution is stale and must end as one.
            self.progress.change(
                prepare_id,
                "lookup",
                lookup_item_id,
                "failed",
                unit="request",
                error="补查输入身份已经变化。",
            )
            raise
        except Exception as exc:
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)
                support = normalize_quality_support(self.cell.state.get("quality_support"))

                def mutate_failed(
                    record: dict[str, Any],
                    exc: Exception = exc,
                    pairs=asked_pairs,
                    cards=[item["card_id"] for item in items],
                    expressions=request_expressions,
                    deferred=len(deferred),
                    request_identity=request_identity,
                ) -> None:
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                    record["counts"]["lookup_failed"] = int(
                        record["counts"].get("lookup_failed") or 0
                    ) + len(cards)
                    record["errors"].append(f"有界补查失败（不再重试）：{str(exc)[:200]}")
                    # The failure was *executed*: it is recorded with its identity
                    # so the identical input does not turn into an endless retry.
                    # A changed content or evidence identity is a new question and
                    # may be looked up again.
                    record_lookup_state(
                        record,
                        pairs=pairs,
                        live_cards=[
                            str(card.get("id"))
                            for card in (support.get("cards") or {}).values()
                            if isinstance(card, dict)
                        ],
                        clock=self.clock,
                    )
                    if deferred:
                        record["counts"]["lookup_deferred"] = (
                            int(record["counts"].get("lookup_deferred") or 0) + int(deferred)
                        )
                    append_lookup_ledger(
                        record,
                        identity=request_identity,
                        status="failed",
                        card_ids=cards,
                        expressions=expressions,
                        clock=self.clock,
                    )

                self.prepare_state.update_record_locked(support, prepare_id, mutate_failed)
            self.progress.change(
                prepare_id,
                "lookup",
                lookup_item_id,
                "failed",
                unit="request",
                error=f"有界补查失败：{str(exc)[:240]}",
            )
            return
        with self.cell.lock:
            self.prepare_state.guard_locked(prepare_id)
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            unit_sources_now = quality_unit_sources(self.cell.state.get("units") or [])
            cards_now = {
                str(card.get("id")): card
                for card in (support.get("cards") or {}).values()
                if isinstance(card, dict)
            }
            pairs: list[dict[str, Any]] = []
            rows: list[dict[str, Any]] = []
            mismatch = ""
            for item, check in zip(items, checks):
                if not isinstance(check, Mapping):
                    mismatch = "补查结果缺少对象。"
                    break
                card = cards_now.get(item["card_id"])
                if card is None or int(card.get("draft_revision") or 0) != int(
                    item["draft_revision"]
                ):
                    mismatch = "补查期间草稿版本已经变化。"
                    break
                live_fingerprint = quality_requests.assessment_context_payload(
                    dict(card.get("draft") or {}), unit_sources=unit_sources_now, model=""
                )["content_fingerprint"]
                if not live_fingerprint or live_fingerprint != item["content_fingerprint"]:
                    mismatch = "补查期间卡片内容已经变化。"
                    break
                if any(
                    str((unit_sources_now.get(unit_id) or ("", ""))[1]) != frozen_hashes[unit_id]
                    for unit_id in frozen_hashes
                ):
                    mismatch = "补查所依据的原文已经变化。"
                    break
                pairs.append(
                    {"card_id": item["card_id"], "draft_revision": item["draft_revision"]}
                )
                rows.append(
                    {
                        **dict(check),
                        "assessment_context": quality_requests.assessment_context_payload(
                            dict(card.get("draft") or {}),
                            unit_sources=unit_sources_now,
                            model=str(getattr(checker, "model", "") or ""),
                        ),
                    }
                )
            updated: list[dict[str, Any]] = []
            if not mismatch and len(rows) == len(items):
                updated = refresh_check_result(
                    support,
                    cards=pairs,
                    checks=rows,
                    now_iso_value=self.clock(),
                    is_protected=concept_automation.is_manual_protected,
                )
            written = {str(card.get("id")) for card in updated}
            # Every card of this request gets a recorded state: the written ones
            # under the identity their *new* conclusion defines (that is what
            # makes the lookup recognise its own answer next time), the rest under
            # the identity they were asked with (executed, but not written).
            state_updates: list[dict[str, Any]] = []
            for item in items:
                card_id = item["card_id"]
                settled_check = item["check"]
                if card_id in written:
                    card = cards_now.get(card_id) or {}
                    settled_check = concept_automation.check_fingerprint(
                        card.get("check") if isinstance(card.get("check"), Mapping) else None
                    )
                state_updates.append(
                    {
                        "card_id": card_id,
                        "version": item["version"],
                        "check": settled_check,
                        "expressions": list(item["asked"]),
                        # The material this execution read, from the sources the
                        # request was built on — an answer cannot rewrite what it
                        # was quoted from, so this stays external to its write.
                        "units": list(item["material_units"]),
                        "status": "completed" if card_id in written else "rejected",
                    }
                )

            def mutate_lookup(
                record: dict[str, Any],
                updates=state_updates,
                written=len(written),
                target=len(items),
                material=request_material,
                mismatch=mismatch,
                repair=repair,
                deferred=len(deferred),
                expressions=request_expressions,
                card_ids=[item["card_id"] for item in items],
                live=[str(card.get("id")) for card in cards_now.values()],
                request_identity=request_identity,
            ) -> None:
                record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                if repair:
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + max(0, int(repair.get("round") or 1) - 1)
                record["counts"]["lookup_rounds"] = int(
                    record["counts"].get("lookup_rounds") or 0
                ) + 1
                record["counts"]["lookup_hits"] = int(
                    record["counts"].get("lookup_hits") or 0
                ) + sum(len(rows) for rows in material.values())
                if deferred:
                    record["counts"]["lookup_deferred"] = int(
                        record["counts"].get("lookup_deferred") or 0
                    ) + int(deferred)
                record_lookup_state(record, pairs=updates, live_cards=live, clock=self.clock)
                if written:
                    record["counts"]["lookup_refreshed"] = int(
                        record["counts"].get("lookup_refreshed") or 0
                    ) + int(written)
                if target - written:
                    # The answer arrived but these cards could not take it
                    # (decided, protected or changed meanwhile): executed, not
                    # written, and never reported as refreshed.
                    record["counts"]["lookup_rejected"] = int(
                        record["counts"].get("lookup_rejected") or 0
                    ) + (target - written)
                if mismatch:
                    record["errors"].append(f"有界补查未写入：{mismatch}")
                status_now = (
                    "completed" if written == target else ("rejected" if not written else "partial")
                )
                append_lookup_ledger(
                    record,
                    identity=request_identity,
                    status=status_now,
                    card_ids=card_ids,
                    expressions=expressions,
                    clock=self.clock,
                )

            self.prepare_state.update_record_locked(support, prepare_id, mutate_lookup)
        self.progress.change(
            prepare_id,
            "lookup",
            lookup_item_id,
            "failed" if mismatch else "complete",
            unit="request",
            error=f"有界补查未写入：{mismatch}" if mismatch else "",
        )
