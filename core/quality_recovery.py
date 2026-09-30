"""Batch recovery records and matching prepare-row updates in a supplied snapshot."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation
from core.quality_support import normalize_batch_retry


def batch_row_copy(
    support: Mapping[str, Any],
    batch_id: str,
) -> dict[str, Any] | None:
    """A detached copy of one stored batch row, or ``None`` when absent.

    Read-only callers use this so a later write to the snapshot cannot
    change what they already inspected. A writer that must mutate the row
    in place reads the live row itself.
    """

    return next(
        (
            dict(item)
            for item in (support.get("batches") or [])
            if str(item.get("batch_id")) == str(batch_id)
        ),
        None,
    )


def batch_retry_record(
    *,
    clock: Callable[[], str],
    stage: str,
    mode: str,
    prepare_id: str,
    units: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]] = (),
    state: str,
    attempt_count: int,
    last_error: str,
) -> dict[str, Any]:
    """The recovery identity frozen onto one batch record.

    Only what a later hand retry needs: which step failed, which mode and
    prepare produced it, the source binding it was made for, and (for a
    failed check) the card revisions it was made for. No prompt, source text
    or model answer is copied here.
    """

    return {
        "stage": str(stage),
        "mode": str(mode),
        "prepare_id": str(prepare_id or ""),
        "source_bindings": [
            {
                "unit_id": str(unit.get("id") or ""),
                "source_sha256": str(unit.get("source_sha256") or ""),
            }
            for unit in units
        ],
        "card_bindings": [
            {
                "card_id": str(card.get("id") or ""),
                "draft_revision": int(card.get("draft_revision") or 0),
                "content_fingerprint": concept_automation._content_fingerprint(card),
            }
            for card in cards
        ],
        "state": str(state),
        "attempt_count": max(0, int(attempt_count or 0)),
        "last_error": str(last_error or "")[:400],
        "updated_at": clock(),
    }


def retry_record_update(
    frozen: Mapping[str, Any],
    *,
    clock: Callable[[], str],
    state: str,
    attempt_count: int,
    last_error: str,
    stage: str = "",
) -> dict[str, Any]:
    """The stored shape of one recovery record after an attempt ended."""

    hint = str(last_error or "").strip()
    return {
        **frozen,
        "stage": str(frozen.get("stage") or stage or "check"),
        "state": str(state),
        "attempt_count": int(attempt_count),
        # A finished recovery clears the current error; the original one
        # stays in the record this update started from (and in the events).
        "last_error": hint
        or ("" if state == "completed" else str(frozen.get("last_error") or "")),
        "updated_at": clock(),
    }


def apply_retry_close(
    support: dict[str, Any],
    *,
    clock: Callable[[], str],
    batch_id: str,
    close: Mapping[str, Any],
    check_status: str,
    check_error: str,
) -> bool:
    """Write the hand-retry record — and its prepare sync — into one snapshot.

    The caller runs this in the very locked block that stores the recovered
    result, so the verdict, the recovery state and the prepare rows it
    recovered share one save. A failed check promotes nothing and keeps the
    concrete reason; only a completed one promotes the frozen units.
    Returns whether the batch row was there to update.
    """

    target = next(
        (
            item
            for item in (support.get("batches") or [])
            if str(item.get("batch_id")) == batch_id
        ),
        None,
    )
    if target is None:
        return False
    frozen = normalize_batch_retry(target.get("retry")) or {}
    completed = str(check_status or "") == "completed"
    target["retry"] = retry_record_update(
        frozen,
        clock=clock,
        state="completed" if completed else "failed",
        attempt_count=int(close.get("attempt_count") or 0),
        last_error="" if completed else (str(check_error or "").strip() or "独立检查未完成。"),
        stage=str(close.get("stage") or ""),
    )
    prepare_id = str(frozen.get("prepare_id") or "")
    units = [str(unit_id) for unit_id in close.get("units") or []]
    if completed and prepare_id and units:
        sync_prepare_rows(
            support, batch_id=batch_id, units=units, prepare_id=prepare_id, clock=clock
        )
    return True


def sync_prepare_rows(
    support: dict[str, Any],
    *,
    clock: Callable[[], str],
    batch_id: str,
    units: Sequence[str],
    prepare_id: str,
) -> bool:
    """Promote the matching prepare rows inside one support snapshot.

    Only the current prepare's rows for exactly these units are touched, and
    only while they are still ``failed``: the original error entries stay as
    history, the group judgments, decisions and frozen references are
    untouched, and the record is never rewritten into ``complete``.
    ``reference_refresh_required`` only asks the operator to re-preview.
    Returns whether anything changed.
    """

    record = concept_automation.automation_of(support).get("prepare")
    if not isinstance(record, Mapping) or str(record.get("prepare_id") or "") != str(
        prepare_id
    ):
        # The prepare was replaced meanwhile: resuming across records is out
        # of scope, and the recovery itself is still valid.
        return False
    batch_unit_ids = {str(unit_id) for unit_id in units}
    current = copy.deepcopy(dict(record))
    touched = False
    for row in current.get("unit_results") or []:
        if str(row.get("unit_id")) not in batch_unit_ids:
            continue
        if str(row.get("status") or "") != "failed":
            continue
        row["status"] = "completed"
        row["batch_id"] = batch_id
        row["reason"] = ""
        touched = True
    if not touched:
        return False
    counts = current.setdefault("counts", {})
    counts["failed_units"] = max(0, int(counts.get("failed_units") or 0) - len(batch_unit_ids))
    # A run whose every failure has now been recovered by hand stops being
    # "failed" — otherwise the next preview would refuse to reuse rows that
    # are genuinely finished. It is never promoted to "complete": the record
    # keeps its history and the operator still has to re-preview, which is
    # what the marker below says.
    if not int(counts.get("failed_units") or 0) and str(
        current.get("status") or ""
    ) not in concept_automation.REUSABLE_PREPARE_STATUSES:
        current["status"] = "partial"
    current["reference_refresh_required"] = True
    current["reference_refresh_required_at"] = clock()
    automation = concept_automation.automation_of(support)
    automation["prepare"] = current
    support["automation"] = automation
    return True
