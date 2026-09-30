"""Reuse-check selection and bounded refreshes of frozen prepare inputs."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation, quality_requests
from core.exceptions import ConflictError
from core.project_state import ProjectStateCell
from core.provider_routing import ProviderRouter
from core.quality_limits import (
    MAX_RECHECK_CARDS,
    MAX_CHECK_REQUEST_UNITS,
    MAX_CHECK_REQUEST_CHARS,
)
from core.quality_prepare_state import PrepareState
from core.quality_progress import PrepareProgress
from core.quality_state import quality_unit_sources
from core.quality_support import (
    PROMPT_VERSION,
    normalize_quality_support,
    refresh_check_result,
)
from providers.quality_provider import ConceptCheckRequest


def stale_check_cards(
    support: Mapping[str, Any],
    unit_sources: Mapping[str, tuple[str, str]],
    scope: Sequence[str],
    *,
    model: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Cards whose stored check cannot be used by the automatic path as is.

    Three independent reasons, none of them "has open questions":

    * ``legacy`` — the check predates the structured protocol, so there is no
      verifiable conclusion to adopt;
    * ``unverified`` — a structured assessment is present but no longer
      verifies (its evidence or its recorded content identity changed);
    * ``identity`` — it verifies, but it was produced under a different
      prompt version, a different check model or against different source
      hashes than the live ones.

    Only cards of this frozen scope, still pending review and not human-owned,
    are returned; the caller decides what to do when it cannot pay for them.
    """

    scope_set = {str(unit_id) for unit_id in scope}
    counts = {"legacy": 0, "unverified": 0, "identity": 0}
    items: list[dict[str, Any]] = []
    for card_id in sorted((support.get("cards") or {}).keys()):
        card = (support.get("cards") or {}).get(card_id)
        if not isinstance(card, dict) or str(card_id) != str(card.get("id") or card_id):
            continue
        if str(card.get("status") or "") != "pending_review":
            continue
        if concept_automation.is_manual_protected(card):
            continue
        draft = card.get("draft")
        if not isinstance(draft, Mapping):
            continue
        unit_ids = sorted(
            {
                str(item.get("unit_id") or "")
                for item in (draft.get("evidence") or [])
                if isinstance(item, Mapping) and str(item.get("unit_id") or "")
            }
        )
        if not unit_ids or not (set(unit_ids) & scope_set):
            continue
        check = card.get("check") if isinstance(card.get("check"), Mapping) else None
        revision = int(card.get("draft_revision") or 0)
        if not isinstance(check, Mapping) or int(check.get("draft_revision") or 0) != revision:
            # A missing or older-revision check cannot be re-verified: it
            # never described this draft, so refreshing it would fake support.
            continue
        if str(check.get("verdict") or "") == "unchecked":
            continue
        questions = [
            str(item).strip() for item in (draft.get("open_questions") or []) if str(item).strip()
        ]
        reason = ""
        if concept_automation.assessment_of(card, unit_sources=unit_sources) is None:
            if not isinstance(check.get("automation_assessment"), Mapping) and not questions:
                # Nothing to upgrade: a card that never raised a question keeps
                # the historical draft path, so an older check is still exactly
                # as usable as it was. Re-checking it would spend a request to
                # change behaviour nobody asked to change.
                continue
            reason = (
                "legacy"
                if not isinstance(check.get("automation_assessment"), Mapping)
                else "unverified"
            )
        else:
            context = (
                check.get("assessment_context")
                if isinstance(check.get("assessment_context"), Mapping)
                else {}
            )
            if str(context.get("prompt_version") or "") != PROMPT_VERSION:
                reason = "identity"
            elif str(context.get("model") or "") != str(model or ""):
                reason = "identity"
            else:
                hashes = (
                    context.get("source_hashes")
                    if isinstance(context.get("source_hashes"), Mapping)
                    else {}
                )
                if any(
                    str(hashes.get(unit_id) or "") != (unit_sources.get(unit_id) or ("", ""))[1]
                    for unit_id in unit_ids
                ):
                    reason = "identity"
        if not reason:
            continue
        fingerprint = quality_requests.assessment_context_payload(
            dict(draft), unit_sources=unit_sources, model=model
        )["content_fingerprint"]
        if not fingerprint:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        items.append(
            {
                "card_id": str(card_id),
                "draft_revision": revision,
                "content_fingerprint": fingerprint,
                "unit_ids": unit_ids,
                "draft": copy.deepcopy(dict(draft)),
                "reason": reason,
            }
        )
    return items, counts


class PrepareRecheck:
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

    def refresh_checks(self, prepare_id: str, prepared: Mapping[str, Any]) -> None:
        """Re-check the reused cards whose stored check cannot be used as is.

        One bounded request per execution (``MAX_RECHECK_CARDS`` cards), issued
        through the same check provider and its bounded repair loop. Candidates
        are never regenerated: the request carries the frozen drafts and the
        page's own candidates stay exactly as they are.
        """

        scope = [str(unit_id) for unit_id in prepared.get("scope") or []]
        with self.cell.lock:
            support = normalize_quality_support(self.cell.state.get("quality_support"))
            unit_sources = quality_unit_sources(self.cell.state.get("units") or [])
            _generation, checker, _editorial, _resolution = self.router.quality_channels()
            self.prepare_state.guard_locked(prepare_id)
            items, reasons = stale_check_cards(
                support,
                unit_sources,
                scope,
                model=str(getattr(checker, "model", "") or ""),
            )
            if not items:
                self.progress.change(
                    prepare_id,
                    "recheck",
                    "recheck-none",
                    "configure",
                    unit="card",
                    total=0,
                    metadata={"request_count": 0, "stale_cards": 0},
                )
                return
            items, over_limit = quality_requests.bound_check_request(
                items,
                unit_sources,
                max_cards=MAX_RECHECK_CARDS,
                max_units=MAX_CHECK_REQUEST_UNITS,
                max_chars=MAX_CHECK_REQUEST_CHARS,
            )
            recheck_total = len(items) + over_limit
            self.progress.change(
                prepare_id,
                "recheck",
                "recheck-plan",
                "configure",
                unit="card",
                total=recheck_total,
                metadata={
                    "request_count": 1,
                    "selected_cards": len(items),
                    "over_limit_cards": over_limit,
                },
            )
            # ``over_limit`` cards keep their unusable check (and are therefore
            # not adopted) instead of being silently dropped, truncated inside a
            # request or answered in an unbounded run: they are recorded as work
            # the next confirmation carries.
            unit_ids = sorted({unit_id for item in items for unit_id in item["unit_ids"]})
            refs = quality_requests.concept_unit_refs_from_sources(unit_ids, unit_sources)
            project_id = str(self.cell.state.get("project", {}).get("id") or "")
            frozen = {
                item["card_id"]: (item["draft_revision"], item["content_fingerprint"])
                for item in items
            }
            frozen_hashes = {
                unit_id: str((unit_sources.get(unit_id) or ("", ""))[1]) for unit_id in unit_ids
            }
            request = ConceptCheckRequest(
                project_id=project_id,
                batch_id=f"recheck-{prepare_id}"[:120],
                units=refs,
                candidates=tuple(copy.deepcopy(item["draft"]) for item in items),
                # The frozen cards and sources this request reads are part of its
                # authorization: every round, repairs included, re-checks them.
                control=self.prepare_state.guard_control_locked(
                    prepare_id,
                    "recheck",
                    cards=frozen,
                    unit_hashes=frozen_hashes,
                    progress_stage="recheck",
                    progress_item_id=f"recheck:{prepare_id}",
                ),
                expected=tuple(
                    {
                        "card_id": item["card_id"],
                        "draft_revision": item["draft_revision"],
                        "content_fingerprint": item["content_fingerprint"],
                    }
                    for item in items
                ),
            )
        recheck_item_id = f"recheck:{prepare_id}"
        self.progress.change(
            prepare_id,
            "recheck",
            recheck_item_id,
            "start",
            unit="card",
            weight=len(items),
            label="复用卡片身份重查",
            metadata={"card_count": len(items), "over_limit_cards": over_limit},
            provider_channel="check",
        )
        try:
            result = checker.check_candidates(request)
            checks = list(result.checks)
            repair = result.repair
        except ConflictError:
            # A control refusal is not a failed task: the frozen input changed or
            # the run was closed, so this execution is stale and must end as one.
            self.progress.change(
                prepare_id,
                "recheck",
                recheck_item_id,
                "failed",
                unit="card",
                weight=len(items),
                error="重查输入身份已经变化。",
            )
            raise
        except Exception as exc:
            with self.cell.lock:
                self.prepare_state.guard_locked(prepare_id)

                def mutate_failed(record: dict[str, Any], exc: Exception = exc) -> None:
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + len(items) + over_limit
                    record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                    record["errors"].append(f"复用单元重查失败：{str(exc)[:200]}")

                self.prepare_state.update_record_locked(support, prepare_id, mutate_failed)
            self.progress.change(
                prepare_id,
                "recheck",
                recheck_item_id,
                "failed",
                unit="card",
                weight=len(items),
                error=f"复用单元重查失败：{str(exc)[:240]}",
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
                    mismatch = "重查结果缺少对象。"
                    break
                card = cards_now.get(item["card_id"])
                if card is None:
                    mismatch = "重查结果指向的卡片已经不存在。"
                    break
                if int(card.get("draft_revision") or 0) != int(item["draft_revision"]):
                    mismatch = "重查结果对应的草稿版本已经变化。"
                    break
                live_fingerprint = quality_requests.assessment_context_payload(
                    dict(card.get("draft") or {}), unit_sources=unit_sources_now, model=""
                )["content_fingerprint"]
                if not live_fingerprint or live_fingerprint != item["content_fingerprint"]:
                    mismatch = "重查结果对应的卡片内容已经变化。"
                    break
                for unit_id in frozen_hashes:
                    if str((unit_sources_now.get(unit_id) or ("", ""))[1]) != frozen_hashes[unit_id]:
                        mismatch = "重查所依据的原文已经变化。"
                        break
                if mismatch:
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
                if len(updated) != len(items):
                    mismatch = "重查结果未能写入（卡片已被裁决、保护或版本不一致）。"
            strict_failed = bool(mismatch) or len(updated) != len(items)

            def mutate_refreshed(
                record: dict[str, Any],
                updated=len(updated),
                over_limit=over_limit,
                reasons=reasons,
                repair=repair,
                strict_failed=strict_failed,
                mismatch=mismatch,
            ) -> None:
                record["requests"]["check"] = int(record["requests"].get("check") or 0) + 1
                if repair:
                    record["requests"]["repair_rounds"] = int(
                        record["requests"].get("repair_rounds") or 0
                    ) + max(0, int(repair.get("round") or 1) - 1)
                if strict_failed:
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + over_limit + (0 if mismatch else len(items))
                    record["errors"].append(f"复用单元重查未写入：{mismatch or '身份或写入保护拒绝'}")
                else:
                    record["counts"]["refreshed_checks"] = int(
                        record["counts"].get("refreshed_checks") or 0
                    ) + updated
                    record["counts"]["recheck_pending"] = int(
                        record["counts"].get("recheck_pending") or 0
                    ) + over_limit
                    by_reason = dict(record["counts"].get("recheck_reasons") or {})
                    for key, value in (reasons or {}).items():
                        if value:
                            by_reason[key] = int(by_reason.get(key) or 0) + int(value)
                    record["counts"]["recheck_reasons"] = by_reason

            self.prepare_state.update_record_locked(support, prepare_id, mutate_refreshed)
        self.progress.change(
            prepare_id,
            "recheck",
            recheck_item_id,
            "failed" if strict_failed else "complete",
            unit="card",
            weight=len(items),
            error=mismatch or "重查结果未能写入。" if strict_failed else "",
        )
