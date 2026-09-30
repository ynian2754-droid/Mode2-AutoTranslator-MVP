"""Structured concept-check data rules extracted from concept_automation.

The module is pure validation and identity logic.  It does not persist state,
call providers, or import its compatibility façade.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from core import concept_content as cc


ASSESSMENT_SCHEMA_VERSION = 1
QUESTION_KINDS = ("preference", "evidence", "meaning")
QUESTION_STATUSES = ("resolved", "unresolved")
BINDING_KINDS = ("term", "distinction")
MAX_ASSESSMENT_BINDINGS = 8
MAX_LOOKUP_EXPRESSIONS = 8
_QUESTION_KIND_LABELS = {"preference": "偏好", "evidence": "证据", "meaning": "含义"}
class AutomationError(ValueError):
    """The requested automation data or action violates the V1 contract."""
def _expression_key(value: Any) -> str:
    """The project's canonical key for one expression (width/case folded)."""

    return cc._orthographic_expression_key(value)


def _assessment_text(value: Any, *, limit: int, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > limit:
        raise AutomationError(f"{field} 超过 {limit} 个字符。")
    return text


def _assessment_evidence(
    value: Any,
    *,
    unit_sources: Mapping[str, tuple[str, str]],
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise AutomationError(f"{field} 至少需要一条可核验的原文证据。")
    try:
        return cc.verify_evidence(value, unit_sources)
    except cc.QualitySupportError as exc:
        raise AutomationError(str(exc)) from exc


def normalize_assessment(
    value: Any,
    *,
    questions: Sequence[Any],
    allowed_expressions: Iterable[Any],
    unit_sources: Mapping[str, tuple[str, str]],
    content_revision: int = 0,
    content_fingerprint: str = "",
) -> dict[str, Any]:
    """Validate the structured answer to one draft's open questions.

    Every structural failure is a repairable content error: the caller's bounded
    repair loop may ask once more, and an exhausted loop fails instead of storing
    a half-validated result. Nothing here relaxes reliability — a question that
    stays unresolved keeps blocking exactly the entries it names, and a resolved
    semantic question must carry source evidence that still verifies today.
    """

    if not isinstance(value, Mapping):
        raise AutomationError("automation_assessment 必须是对象。")
    try:
        schema_version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise AutomationError("automation_assessment.schema_version 必须是整数 1。") from exc
    if schema_version != ASSESSMENT_SCHEMA_VERSION:
        raise AutomationError(
            f"automation_assessment.schema_version 必须是 {ASSESSMENT_SCHEMA_VERSION}。"
        )
    question_texts = [str(item).strip() for item in questions if str(item).strip()]
    whitelist = {_expression_key(item) for item in allowed_expressions if str(item or "").strip()}

    bindings_raw = value.get("bindings")
    if not isinstance(bindings_raw, list) or not bindings_raw:
        raise AutomationError("automation_assessment.bindings 至少需要一条表达—译名条目。")
    if len(bindings_raw) > MAX_ASSESSMENT_BINDINGS:
        raise AutomationError(
            f"automation_assessment.bindings 最多 {MAX_ASSESSMENT_BINDINGS} 条，超限不截断。"
        )

    bindings: list[dict[str, Any]] = []
    seen_binding_ids: set[str] = set()
    for index, item in enumerate(bindings_raw):
        field = f"bindings[{index}]"
        if not isinstance(item, Mapping):
            raise AutomationError(f"{field} 必须是对象。")
        binding_id = str(item.get("binding_id") or "").strip()
        if not binding_id:
            raise AutomationError(f"{field}.binding_id 不能为空。")
        if binding_id in seen_binding_ids:
            raise AutomationError(f"{field}.binding_id 重复：{binding_id}。")
        seen_binding_ids.add(binding_id)
        expressions = [
            str(entry).strip()
            for entry in (item.get("expressions") or [])
            if str(entry or "").strip()
        ]
        if not expressions:
            raise AutomationError(f"{field}.expressions 至少需要一个表达。")
        outside = [entry for entry in expressions if _expression_key(entry) not in whitelist]
        if outside:
            raise AutomationError(
                f"{field}.expressions 含输入卡之外的表达：{'、'.join(outside)}。"
            )
        kind = str(item.get("kind") or "").strip()
        if kind not in BINDING_KINDS:
            raise AutomationError(f"{field}.kind 只能是 {'、'.join(BINDING_KINDS)}。")
        preferred = _assessment_text(
            item.get("preferred_translation"),
            limit=200,
            field=f"{field}.preferred_translation",
        )
        guidance = _assessment_text(item.get("guidance"), limit=600, field=f"{field}.guidance")
        if kind == "term" and not preferred:
            raise AutomationError(f"{field} 是 term，必须给出明确对应的首选译名。")
        if kind == "distinction" and not guidance:
            raise AutomationError(f"{field} 是 distinction，必须用 guidance 说明区别。")
        bindings.append(
            {
                "binding_id": binding_id,
                "expressions": expressions,
                "kind": kind,
                "preferred_translation": preferred,
                "acceptable_variants": [
                    str(entry).strip()
                    for entry in (item.get("acceptable_variants") or [])
                    if str(entry or "").strip()
                ][:MAX_ASSESSMENT_BINDINGS],
                "guidance": guidance,
                "applies_when": _assessment_text(
                    item.get("applies_when"),
                    limit=600,
                    field=f"{field}.applies_when",
                ),
                "evidence": _assessment_evidence(
                    item.get("evidence"), unit_sources=unit_sources, field=field
                ),
            }
        )

    results_raw = value.get("question_results")
    if results_raw is None:
        results_raw = []
    if not isinstance(results_raw, list):
        raise AutomationError("automation_assessment.question_results 必须是数组。")
    binding_ids = {item["binding_id"] for item in bindings}
    results: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for index, item in enumerate(results_raw):
        field = f"question_results[{index}]"
        if not isinstance(item, Mapping):
            raise AutomationError(f"{field} 必须是对象。")
        question_index = item.get("question_index")
        if isinstance(question_index, bool) or not isinstance(question_index, int):
            raise AutomationError(f"{field}.question_index 必须是整数。")
        if question_index in seen_indices or not 0 <= question_index < len(question_texts):
            raise AutomationError(
                f"{field}.question_index 越界或重复：每个问题只能作答一次，不能漏答或凭空多答。"
            )
        seen_indices.add(question_index)
        kind = str(item.get("kind") or "").strip()
        if kind not in QUESTION_KINDS:
            raise AutomationError(f"{field}.kind 只能是 {'、'.join(QUESTION_KINDS)}。")
        status = str(item.get("status") or "").strip()
        if status not in QUESTION_STATUSES:
            raise AutomationError(f"{field}.status 只能是 {'、'.join(QUESTION_STATUSES)}。")
        affected_raw = item.get("affected_binding_ids")
        if affected_raw is None:
            affected_raw = []
        if not isinstance(affected_raw, list):
            raise AutomationError(
                f"{field}.affected_binding_ids 必须是数组（无法隔离时用空数组表示全部条目）。"
            )
        affected = [
            str(entry).strip() for entry in affected_raw if str(entry or "").strip()
        ]
        unknown = [entry for entry in affected if entry not in binding_ids]
        if unknown:
            raise AutomationError(
                f"{field}.affected_binding_ids 指向不存在的条目：{'、'.join(unknown)}。"
            )
        raw_evidence = item.get("evidence")
        if not isinstance(raw_evidence, list):
            raise AutomationError(f"{field}.evidence 必须是数组。")
        if status == "resolved" and kind in {"evidence", "meaning"}:
            evidence = _assessment_evidence(
                raw_evidence, unit_sources=unit_sources, field=field
            )
        elif raw_evidence:
            evidence = _assessment_evidence(
                raw_evidence, unit_sources=unit_sources, field=field
            )
        else:
            evidence = []
        results.append(
            {
                "question_index": question_index,
                "kind": kind,
                "status": status,
                "answer": _assessment_text(item.get("answer"), limit=600, field=f"{field}.answer"),
                "reason": _assessment_text(item.get("reason"), limit=600, field=f"{field}.reason"),
                "evidence": evidence,
                "affected_binding_ids": affected,
            }
        )
    missing = sorted(set(range(len(question_texts))) - seen_indices)
    if missing:
        raise AutomationError(
            "独立检查没有覆盖全部待确认问题：缺少第 "
            + "、".join(str(item) for item in missing)
            + " 个。"
        )
    results.sort(key=lambda row: int(row["question_index"]))

    lookup_raw = value.get("lookup_expressions")
    if lookup_raw is None:
        lookup_raw = []
    if not isinstance(lookup_raw, list):
        raise AutomationError("automation_assessment.lookup_expressions 必须是数组。")
    if len(lookup_raw) > MAX_LOOKUP_EXPRESSIONS:
        raise AutomationError(
            f"automation_assessment.lookup_expressions 最多 {MAX_LOOKUP_EXPRESSIONS} 个，超限不截断。"
        )
    lookups = [str(entry).strip() for entry in lookup_raw if str(entry or "").strip()]
    outside = [entry for entry in lookups if _expression_key(entry) not in whitelist]
    if outside:
        raise AutomationError(
            f"automation_assessment.lookup_expressions 含输入卡之外的表达：{'、'.join(outside)}。"
        )

    return {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "content_revision": int(content_revision or 0),
        "content_fingerprint": str(content_fingerprint or ""),
        "questions": question_texts,
        "question_results": results,
        "bindings": bindings,
        "lookup_expressions": lookups,
    }


def assessment_fingerprint(assessment: Mapping[str, Any]) -> str:
    """A stable identity over the *content* of one structured assessment.

    Answers and bindings decide what a reference says; metadata (protocol
    version, model id, the content revision it was bound to) does not. A refresh
    that produces the same answers therefore keeps the same fingerprint and must
    not be treated as a reference change.
    """

    if not isinstance(assessment, Mapping):
        return ""
    payload = {
        "schema_version": int(assessment.get("schema_version") or 0),
        "question_results": [
            {
                "question_index": int(item.get("question_index") or 0),
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                "answer": str(item.get("answer") or ""),
                "reason": str(item.get("reason") or ""),
                "evidence": [
                    {
                        "unit_id": str(entry.get("unit_id") or ""),
                        "source_sha256": str(entry.get("source_sha256") or ""),
                        "source_excerpt": str(entry.get("source_excerpt") or ""),
                    }
                    for entry in (item.get("evidence") or [])
                    if isinstance(entry, Mapping)
                ],
                "affected_binding_ids": [str(entry) for entry in (item.get("affected_binding_ids") or [])],
            }
            for item in (assessment.get("question_results") or [])
            if isinstance(item, Mapping)
        ],
        "bindings": [
            {
                "binding_id": str(item.get("binding_id") or ""),
                "expressions": [str(entry) for entry in (item.get("expressions") or [])],
                "kind": str(item.get("kind") or ""),
                "preferred_translation": str(item.get("preferred_translation") or ""),
                "acceptable_variants": [str(entry) for entry in (item.get("acceptable_variants") or [])],
                "guidance": str(item.get("guidance") or ""),
                "applies_when": str(item.get("applies_when") or ""),
                # The complete evidence identity: swapping the source basis of a
                # binding changes what the reference relies on, so it must change
                # the fingerprint even when both excerpts verify verbatim.
                "evidence": [
                    {
                        "unit_id": str(entry.get("unit_id") or ""),
                        "source_sha256": str(entry.get("source_sha256") or ""),
                        "source_excerpt": str(entry.get("source_excerpt") or ""),
                    }
                    for entry in (item.get("evidence") or [])
                    if isinstance(entry, Mapping)
                ],
            }
            for item in (assessment.get("bindings") or [])
            if isinstance(item, Mapping)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _recorded_content_fingerprint(check: Mapping[str, Any]) -> str:
    """The content version the program recorded when it stored this check."""

    context = check.get("assessment_context")
    if isinstance(context, Mapping) and str(context.get("content_fingerprint") or ""):
        return str(context["content_fingerprint"])
    return str(check.get("content_fingerprint") or "")


def assessment_of(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> dict[str, Any] | None:
    """The validated structured assessment stored on one card's check.

    Re-validating on read is deliberate: an assessment whose evidence no longer
    verifies against the live sources is not a usable result, and a check that
    predates the structured protocol simply has none.
    """

    check = card.get("check") if isinstance(card.get("check"), Mapping) else None
    if not isinstance(check, Mapping):
        return None
    raw = check.get("automation_assessment")
    if not isinstance(raw, Mapping):
        return None
    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    recorded = _recorded_content_fingerprint(check)
    if recorded:
        # The program wrote that fingerprint when the check was stored. A
        # mismatch means the card (or the record) changed under it: refuse the
        # result instead of re-deriving an identity that would hide it.
        try:
            live = cc.content_signature(cc.normalize_card_content(draft, unit_sources=unit_sources))
        except Exception:
            live = ""
        if not live or live != recorded:
            return None
    try:
        return normalize_assessment(
            raw,
            questions=list((draft or {}).get("open_questions") or []),
            allowed_expressions=list((draft or {}).get("expressions") or []),
            unit_sources=unit_sources,
            content_revision=int(check.get("draft_revision") or 0),
            content_fingerprint=str(check.get("content_fingerprint") or ""),
        )
    except AutomationError:
        return None


def question_block(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> tuple[bool, set[str], str]:
    """How one draft's open questions still block adoption.

    Returns ``(blocks_all, blocked_binding_ids, reason)``. A question without a
    structured, verifiable assessment blocks the whole card — model prose saying
    "已解决" is no longer a release. An unresolved question that names concrete
    bindings blocks only those entries.
    """

    draft = card.get("draft") if isinstance(card.get("draft"), Mapping) else {}
    questions = [
        str(item).strip() for item in (draft.get("open_questions") or []) if str(item).strip()
    ]
    if not questions:
        return False, set(), ""
    assessment = assessment_of(card, unit_sources=unit_sources)
    if assessment is None:
        return (
            True,
            set(),
            "旧检查没有结构化疑问结论，文字说明不能放行；仍有关键待确认问题，未解决前不自动采用。",
        )
    blocked: set[str] = set()
    unresolved_kinds: set[str] = set()
    for row in assessment["question_results"]:
        if row["status"] != "unresolved":
            continue
        label = _QUESTION_KIND_LABELS.get(row["kind"], row["kind"])
        unresolved_kinds.add(label)
        if not row["affected_binding_ids"]:
            return (
                True,
                set(),
                f"仍有未解决的{label}问题，且无法隔离到具体条目，整张卡不自动采用。",
            )
        blocked.update(row["affected_binding_ids"])
    binding_ids = {item["binding_id"] for item in assessment["bindings"]}
    if binding_ids and blocked >= binding_ids:
        return True, set(), "未解决的问题影响全部条目，整张卡不自动采用。"
    if blocked:
        return (
            False,
            blocked,
            f"仍有未解决的{'、'.join(sorted(unresolved_kinds))}问题，只排除受影响条目。",
        )
    return False, set(), ""


def adoptable_bindings(
    card: Mapping[str, Any],
    *,
    unit_sources: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Bindings one card may inject right now.

    Cards without a structured assessment (including legacy checks) return an
    empty list: the caller then keeps its previous behaviour for cards that never
    raised a question, and nothing releases a questioned card.
    """

    assessment = assessment_of(card, unit_sources=unit_sources)
    if assessment is None:
        return []
    blocks_all, blocked, _reason = question_block(card, unit_sources=unit_sources)
    if blocks_all:
        return []
    return [item for item in assessment["bindings"] if item["binding_id"] not in blocked]
