"""Bounded concept extraction, independent concept checking and local editorial suggestions.

Three separate model-facing entries share the existing OpenAI-compatible
client, credentials, timeout and error sanitisation. None of them may:

- write a translation, a review verdict or an approval state;
- run automatically during import, translation or review;
- make a network call unless the user started the operation.

The deterministic fake provider exists so the whole UI can be exercised
offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from core import concept_automation as ca
from core.api_settings import ApiConfig
from core.quality_support import QualitySupportError, verify_evidence

from .api_client import OpenAICompatibleClient, ProviderRequestError, TruncatedAnswerError
from .prompts import (
    CONCEPT_CHECK_SYSTEM_PROMPT,
    CONCEPT_EXTRACTION_SYSTEM_PROMPT,
    CONCEPT_RESOLUTION_SYSTEM_PROMPT,
    EDITORIAL_SUGGESTION_SYSTEM_PROMPT,
)
from .repair_loop import ContentRepairError, RepairControl, run_content_repair_loop

CANDIDATE_KEYS = {
    "expressions",
    "meaning",
    "applies_when",
    "acceptable_translations",
    "confusions",
    "evidence",
    "open_questions",
    "priority",
}
# Keys that would let the model grant itself approval. They are rejected.
FORBIDDEN_CANDIDATE_KEYS = {
    "approved",
    "approved_revision",
    "approved_at",
    "approved_by",
    "status",
    "id",
}
SUGGESTION_KINDS = {"expression", "tone", "concept_risk"}


class QualityProviderError(ProviderRequestError):
    """The model responded, but its quality payload violated the local contract.

    Raised by the payload parsers themselves.  Inside a model call the parsers
    are wrapped so the same violation becomes a repairable
    :class:`ContentRepairError` instead (see ``_QualityClientMixin``); calling a
    parser directly still raises this type.
    """


@dataclass(frozen=True)
class ConceptUnitRef:
    unit_id: str
    source_text: str
    source_sha256: str


@dataclass(frozen=True)
class ConceptScanRequest:
    project_id: str
    batch_id: str
    units: tuple[ConceptUnitRef, ...]
    approved_expressions: tuple[str, ...] = ()
    max_cards: int = 12
    # Optional in-process execution hook (never serialized); ``None`` means the
    # provider runs its bounded repair loop without extra authorization checks.
    control: RepairControl | None = None


@dataclass(frozen=True)
class ConceptScanResult:
    batch_id: str
    candidates: list[dict[str, Any]]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    repair: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConceptCheckRequest:
    project_id: str
    batch_id: str
    units: tuple[ConceptUnitRef, ...]
    candidates: tuple[dict[str, Any], ...]
    control: RepairControl | None = None
    #: Read-only material for one bounded follow-up: the deterministic in-project
    #: hits of the expressions an earlier check asked to look up. It is context,
    #: never an authorization — the check may cite it but may not widen the
    #: scope, and every excerpt already comes from the project's own sources.
    lookup_evidence: Mapping[str, Sequence[Mapping[str, str]]] = field(default_factory=dict)
    #: ``(card_id, draft_revision, content_fingerprint)`` in candidate order. A
    #: re-check must prove it answers *these* frozen cards, so the answer has to
    #: echo the identity back; a missing or different identity is a protocol
    #: error rather than a silently mispaired write.
    expected: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ConceptCheckResult:
    batch_id: str
    checks: list[dict[str, Any]]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    repair: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConceptResolutionRequest:
    """One related-card group that needs a relation judgment.

    ``members`` carry the card id, its current content revision and the payload
    the model may read; ``unit_sources`` is the live source text of the units in
    scope, so a claimed evidence excerpt can be checked locally. The request is
    local to one group: nothing is shared across groups, units or projects.
    """

    project_id: str
    group_id: str
    members: tuple[dict[str, Any], ...]
    unit_sources: dict[str, tuple[str, str]] = field(default_factory=dict)
    input_fingerprint: str = ""
    control: RepairControl | None = None


@dataclass(frozen=True)
class ConceptResolutionResult:
    group_id: str
    relation: str
    payload: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    repair: dict[str, Any] | None = None


@dataclass(frozen=True)
class EditorialSuggestionRequest:
    project_id: str
    unit_id: str
    source_text: str
    source_sha256: str
    translated_text: str
    translation_revision: int
    adjacent_source: tuple[str, ...] = ()
    approved_expressions: tuple[str, ...] = ()
    # The actual approved card content and the version it was frozen at, so the
    # editorial model can read the meaning/acceptable translations rather than
    # only a bare expression list. ``approved_expressions`` is kept as a flat
    # fallback for older call sites.
    approved_cards: tuple[dict[str, Any], ...] = ()
    control: RepairControl | None = None


@dataclass(frozen=True)
class EditorialSuggestionResult:
    unit_id: str
    source_sha256: str
    translation_revision: int
    suggestions: list[dict[str, Any]]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    repair: dict[str, Any] | None = None


def _source_units_block(units: Sequence[ConceptUnitRef]) -> str:
    blocks = []
    for unit in units:
        blocks.append(
            "<source_unit>\n"
            f"unit_id: {unit.unit_id}\n"
            f"source_sha256: {unit.source_sha256}\n"
            "<source_text>\n"
            f"{unit.source_text}\n"
            "</source_text>\n"
            "</source_unit>"
        )
    return "\n".join(blocks)


def _strip_json_fence(raw: str) -> str:
    candidate = raw.strip()
    if not candidate.startswith("```"):
        return candidate
    lines = candidate.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return candidate
    if lines[0].strip().casefold() not in {"```", "```json"}:
        return candidate
    return "\n".join(lines[1:-1]).strip()


def _load_json_object(raw: str, *, what: str) -> dict[str, Any]:
    candidate = _strip_json_fence(raw)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise QualityProviderError(f"{what}返回的内容不是合法 JSON。") from exc
    if not isinstance(data, dict):
        raise QualityProviderError(f"{what}顶层必须是 JSON 对象。")
    return data


def _text_list(value: Any, *, limit: int, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise QualityProviderError(f"{field_name} 必须是数组。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in result:
            result.append(text)
    if len(result) > limit:
        raise QualityProviderError(f"{field_name} 超过 {limit} 项上限。")
    return result


def _bounded(value: Any, *, limit: int, field_name: str) -> str:
    if not isinstance(value, str):
        raise QualityProviderError(f"{field_name} 必须是字符串。")
    text = value.strip()
    if len(text) > limit:
        raise QualityProviderError(f"{field_name} 超过 {limit} 字符上限。")
    return text


def normalize_candidate_list(
    value: Any,
    *,
    unit_ids: set[str],
    max_cards: int,
) -> list[dict[str, Any]]:
    """Validate generated candidates structurally before anything is stored."""
    if not isinstance(value, list):
        raise QualityProviderError("candidates 必须是数组。")
    if len(value) > max_cards:
        raise QualityProviderError(f"概念候选数量超过 {max_cards} 项上限。")

    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise QualityProviderError(f"candidates[{index}] 必须是对象。")
        forbidden = FORBIDDEN_CANDIDATE_KEYS & set(item)
        if forbidden:
            raise QualityProviderError(
                f"candidates[{index}] 不允许包含模型自填的状态字段：{', '.join(sorted(forbidden))}。"
            )
        expressions = _text_list(item.get("expressions"), limit=8, field_name=f"candidates[{index}].expressions")
        if not expressions:
            raise QualityProviderError(f"candidates[{index}].expressions 至少需要一项。")
        acceptable = _text_list(
            item.get("acceptable_translations"),
            limit=8,
            field_name=f"candidates[{index}].acceptable_translations",
        )
        if not acceptable:
            raise QualityProviderError(f"candidates[{index}].acceptable_translations 至少需要一项。")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise QualityProviderError(f"candidates[{index}].evidence 至少需要一条。")
        if len(evidence) > 6:
            raise QualityProviderError(f"candidates[{index}].evidence 最多 6 条。")
        normalized_evidence: list[dict[str, Any]] = []
        for evidence_index, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                raise QualityProviderError(
                    f"candidates[{index}].evidence[{evidence_index}] 必须是对象。"
                )
            if set(entry) != {"unit_id", "source_sha256", "source_excerpt"}:
                raise QualityProviderError(
                    f"candidates[{index}].evidence[{evidence_index}] 必须且只能包含 "
                    "unit_id、source_sha256、source_excerpt。"
                )
            unit_id = str(entry["unit_id"]).strip()
            if unit_id not in unit_ids:
                raise QualityProviderError(
                    f"candidates[{index}].evidence[{evidence_index}].unit_id 不在本次扫描范围内。"
                )
            excerpt = _bounded(
                entry["source_excerpt"],
                limit=400,
                field_name=f"candidates[{index}].evidence[{evidence_index}].source_excerpt",
            )
            if not excerpt:
                raise QualityProviderError(
                    f"candidates[{index}].evidence[{evidence_index}].source_excerpt 不能为空。"
                )
            normalized_evidence.append(
                {
                    "unit_id": unit_id,
                    "source_sha256": str(entry["source_sha256"]).strip(),
                    "source_excerpt": excerpt,
                }
            )
        priority = item.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = 0
        candidates.append(
            {
                "expressions": expressions,
                "meaning": _bounded(item.get("meaning"), limit=1200, field_name=f"candidates[{index}].meaning"),
                "applies_when": _bounded(
                    item.get("applies_when"), limit=1200, field_name=f"candidates[{index}].applies_when"
                ),
                "acceptable_translations": acceptable,
                "confusions": _text_list(
                    item.get("confusions"), limit=8, field_name=f"candidates[{index}].confusions"
                ),
                "evidence": normalized_evidence,
                "open_questions": _text_list(
                    item.get("open_questions"), limit=8, field_name=f"candidates[{index}].open_questions"
                ),
                "priority": max(0, min(int(priority), 100)),
            }
        )
    return candidates


def normalize_check_list(
    value: Any,
    *,
    expected_count: int,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    unit_sources: Mapping[str, tuple[str, str]] | None = None,
    expected: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate the independent check payload and keep candidate order.

    ``candidates``/``unit_sources`` (the live input of this same request) enable
    the structured ``automation_assessment`` validation: question coverage,
    binding limits, the expression whitelist and evidence are checked against
    what the model was actually given. Without them the assessment is only
    shape-checked, which keeps legacy callers working.

    ``expected`` is the frozen card identity of a re-check. When it is given the
    answer must echo ``card_id``/``draft_revision``/``content_fingerprint`` for
    every result; anything else is a protocol error, so a result computed for
    another revision can never be stored as if it belonged to this one.
    """
    if not isinstance(value, list):
        raise QualityProviderError("results 必须是数组。")
    if len(value) != expected_count:
        raise QualityProviderError("独立检查结果数量必须与候选数量一致。")

    checks: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            raise QualityProviderError(f"results[{position}] 必须是对象。")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise QualityProviderError(f"results[{position}].index 必须是整数。")
        if index in seen_indices or not 0 <= index < expected_count:
            raise QualityProviderError(f"results[{position}].index 越界或重复。")
        seen_indices.add(index)
        forbidden = FORBIDDEN_CANDIDATE_KEYS & set(item)
        if forbidden:
            raise QualityProviderError(
                f"results[{position}] 不允许包含模型自填的状态字段：{', '.join(sorted(forbidden))}。"
            )
        verdict = item.get("verdict")
        if verdict not in {"supported", "disputed", "insufficient"}:
            raise QualityProviderError(
                f"results[{position}].verdict 必须是 supported、disputed 或 insufficient。"
            )
        reasons = _text_list(item.get("reasons"), limit=8, field_name=f"results[{position}].reasons")
        if not reasons:
            raise QualityProviderError(f"results[{position}].reasons 至少需要一项。")
        row = {
            "index": index,
            "expression": _bounded(
                item.get("expression"), limit=200, field_name=f"results[{position}].expression"
            ),
            "verdict": verdict,
            "reasons": reasons,
            "notes": _bounded(item.get("notes"), limit=1200, field_name=f"results[{position}].notes"),
        }
        if expected is not None:
            pair = expected[index] if index < len(expected) else {}
            card_id = _bounded(
                item.get("card_id"), limit=120, field_name=f"results[{position}].card_id"
            )
            revision = item.get("draft_revision")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise QualityProviderError(f"results[{position}].draft_revision 必须是整数。")
            fingerprint = _bounded(
                item.get("content_fingerprint"),
                limit=80,
                field_name=f"results[{position}].content_fingerprint",
            )
            if card_id != str(pair.get("card_id") or ""):
                raise QualityProviderError(
                    f"results[{position}] 回传的 card_id 与本次冻结的卡片不一致。"
                )
            if int(revision) != int(pair.get("draft_revision") or 0):
                raise QualityProviderError(
                    f"results[{position}] 回传的 draft_revision 与本次冻结的卡片不一致。"
                )
            if fingerprint != str(pair.get("content_fingerprint") or ""):
                raise QualityProviderError(
                    f"results[{position}] 回传的内容指纹与本次冻结的卡片不一致。"
                )
            row["card_id"] = card_id
            row["draft_revision"] = int(revision)
        assessment = item.get("automation_assessment")
        if assessment is not None:
            if candidates is not None and unit_sources is not None:
                candidate = candidates[index]
                try:
                    row["automation_assessment"] = ca.normalize_assessment(
                        assessment,
                        questions=list(candidate.get("open_questions") or []),
                        allowed_expressions=list(candidate.get("expressions") or []),
                        unit_sources=unit_sources,
                    )
                except ca.AutomationError as exc:
                    raise QualityProviderError(
                        f"results[{position}].automation_assessment：{exc}"
                    ) from exc
            elif isinstance(assessment, Mapping):
                row["automation_assessment"] = dict(assessment)
            else:
                raise QualityProviderError(
                    f"results[{position}].automation_assessment 必须是对象。"
                )
        checks.append(row)
    checks.sort(key=lambda item: item["index"])
    return checks


def normalize_suggestion_list(value: Any) -> list[dict[str, Any]]:
    """Validate local editorial suggestions; fewer is fine, never more than 5."""
    if not isinstance(value, list):
        raise QualityProviderError("suggestions 必须是数组。")
    if len(value) > 5:
        raise QualityProviderError("表达建议最多 5 条。")
    suggestions: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise QualityProviderError(f"suggestions[{index}] 必须是对象。")
        kind = item.get("kind")
        if kind not in SUGGESTION_KINDS:
            raise QualityProviderError(
                f"suggestions[{index}].kind 必须是 expression、tone 或 concept_risk。"
            )
        source_excerpt = _bounded(
            item.get("source_excerpt"), limit=400, field_name=f"suggestions[{index}].source_excerpt"
        )
        if not source_excerpt:
            raise QualityProviderError(f"suggestions[{index}].source_excerpt 不能为空。")
        suggested = _bounded(
            item.get("suggested_expression"),
            limit=400,
            field_name=f"suggestions[{index}].suggested_expression",
        )
        if not suggested:
            raise QualityProviderError(f"suggestions[{index}].suggested_expression 不能为空。")
        suggestions.append(
            {
                "kind": kind,
                "source_excerpt": source_excerpt,
                "translation_excerpt": _bounded(
                    item.get("translation_excerpt"),
                    limit=400,
                    field_name=f"suggestions[{index}].translation_excerpt",
                ),
                "suggested_expression": suggested,
                "reason": _bounded(
                    item.get("reason"), limit=1200, field_name=f"suggestions[{index}].reason"
                ),
            }
        )
    return suggestions


class _QualityClientMixin:
    def __init__(self, *, config: ApiConfig | None = None, api_key: str | None = None,
                 base_url: str | None = None, model: str | None = None) -> None:
        if config is None:
            raise QualityProviderError("质量辅助功能必须复用已有的翻译端或审校端配置。")
        if any(value is not None for value in (api_key, base_url, model)):
            raise QualityProviderError("不允许为质量辅助功能创建第三方 API 配置。")
        self.config = config
        self.client = OpenAICompatibleClient(config)
        self.model = config.model

    def _run_json_loop(
        self,
        system: str,
        user: str,
        *,
        kind: str,
        validate: Callable[[str], Any],
        control: RepairControl | None = None,
        invocation_id: str = "",
    ) -> tuple[Any, dict[str, int], dict[str, Any]]:
        """Run one JSON call through the shared bounded content repair loop.

        The concept calls get exactly the same contract as translation and
        review: a local protocol violation keeps the initial request and the
        previous visible answer, appends the controlled feedback and asks again,
        at most ``MAX_CONTENT_ROUNDS`` generations.  Network, auth, quota and
        rate-limit failures are not repaired, and an explicit ``response_format``
        rejection degrades once as an extra request, not as a repair round.

        A truncated answer (``finish_reason=length``) is deliberately **not**
        repairable: re-sending the same request with the same output cap would be
        cut off again, so one call ends the request with a reason an operator can
        act on instead of three identical rounds and a protocol message.
        """

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        output_cap = int(
            getattr(getattr(self.client, "config", None), "max_output_tokens", 0) or 0
        )

        def chat(history, response_format):
            if response_format is None:
                return self.client.chat(history)
            return self.client.chat(history, response_format=response_format)

        def guarded_validate(raw: str) -> Any:
            try:
                return validate(raw)
            except ContentRepairError as exc:
                # ``length`` describes the answer that was just read on this
                # thread; a client double that never sets it keeps the old
                # behavior, so only a real endpoint answer can trigger this.
                if str(getattr(self.client, "last_finish_reason", "") or "") == "length":
                    raise TruncatedAnswerError(
                        f"{kind}的输出被截断（finish_reason=length，输出上限 {output_cap} token）："
                        "本次回答没有完成可校验的 JSON，已停止，不用相同请求规模与上限重试；"
                        "请提高该通道的输出上限或缩小单次请求后重新准备。"
                    ) from exc
                raise

        value, outcome = run_content_repair_loop(
            kind=kind,
            messages=messages,
            chat=chat,
            validate=guarded_validate,
            control=control,
            invocation_id=invocation_id,
            response_format={"type": "json_object"},
        )
        return value, outcome.usage, outcome.payload(invocation_id=invocation_id)


def _validate_top_level_keys(data: dict[str, Any], expected: set[str], what: str) -> None:
    if set(data) != expected:
        only = "、".join(sorted(expected))
        raise QualityProviderError(f"{what}顶层字段必须且只能是 {only}。")


#: Relation vocabulary of one group judgment.
RESOLUTION_RELATIONS = {"equivalent", "distinct", "unresolved"}


def normalize_resolution(
    value: Any,
    *,
    member_ids: set[str],
    group_id: str,
    source_units: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate one model group judgment against the frozen member pool.

    The program owns the scope: the model may only name members that were sent,
    may not put one member into two conflicting relation lists, and may only
    claim adoption for cards whose own evidence exists in the listed units. Any
    invented card id, duplicated representative or unverifiable unit is a
    protocol error, so it becomes a repairable round rather than a silent write.
    """

    def _reason(raw: Any) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise QualityProviderError("理由必须是字符串。")
        return _bounded(raw, limit=400, field_name="reason")

    if not isinstance(value, dict):
        raise QualityProviderError("组辨析结果必须是对象。")
    _validate_top_level_keys(
        value, {"group_id", "members", "relation", "equivalent", "distinct", "unresolved", "applied_units"}, "组辨析"
    )
    if str(value.get("group_id") or "") != group_id:
        raise QualityProviderError("组辨析结果回传的 group_id 与请求不一致。")
    members = [str(item) for item in (value.get("members") or []) if str(item).strip()]
    if sorted(set(members)) != sorted(member_ids):
        raise QualityProviderError("组辨析结果没有覆盖本组全部成员，或包含未知成员。")
    relation = str(value.get("relation") or "").strip().casefold()
    if relation not in RESOLUTION_RELATIONS:
        raise QualityProviderError("组辨析关系只能是 equivalent、distinct 或 unresolved。")
    equivalent: list[dict[str, Any]] = []
    seen_representatives: set[str] = set()
    for item in value.get("equivalent") or []:
        if not isinstance(item, dict):
            raise QualityProviderError("equivalent 的每一项都必须是对象。")
        representative = str(item.get("representative_id") or item.get("representative") or "").strip()
        if not representative:
            raise QualityProviderError("equivalent 项缺少 representative_id。")
        if representative not in member_ids:
            raise QualityProviderError(f"equivalent 引用了本组之外的卡片 {representative}。")
        if representative in seen_representatives:
            raise QualityProviderError(f"代表卡 {representative} 在同一组里出现两次。")
        seen_representatives.add(representative)
        equivalent.append(
            {
                "representative_id": representative,
                "member_ids": [str(mid) for mid in (item.get("member_ids") or []) if str(mid).strip()],
                "reason": _reason(item.get("reason")),
            }
        )
    distinct: list[dict[str, Any]] = []
    for item in value.get("distinct") or []:
        if not isinstance(item, dict):
            raise QualityProviderError("distinct 的每一项都必须是对象。")
        card_id = str(item.get("card_id") or "").strip()
        if card_id not in member_ids:
            raise QualityProviderError(f"distinct 引用了本组之外的卡片 {card_id}。")
        distinct.append(
            {
                "card_id": card_id,
                "reason": _reason(item.get("reason")),
            }
        )
    unresolved = [str(item) for item in (value.get("unresolved") or []) if str(item).strip()]
    for card_id in unresolved:
        if card_id not in member_ids:
            raise QualityProviderError(f"unresolved 引用了本组之外的卡片 {card_id}。")
    # A member may not be claimed by two conflicting relation lists.
    equivalent_members = {member for item in equivalent for member in item["member_ids"]}
    distinct_members = {item["card_id"] for item in distinct}
    overlap = (equivalent_members & distinct_members) | (equivalent_members & set(unresolved)) | (
        distinct_members & set(unresolved)
    )
    if overlap:
        raise QualityProviderError(f"同一张卡出现在多个互斥结论里：{'、'.join(sorted(overlap))}。")
    applied_units: dict[str, list[str]] = {}
    if source_units is not None:
        for card_id, units in (value.get("applied_units") or {}).items():
            card_id = str(card_id)
            if card_id not in member_ids:
                raise QualityProviderError(f"applied_units 引用了本组之外的卡片 {card_id}。")
            rows: list[str] = []
            for unit_id in units or []:
                unit_id = str(unit_id)
                if unit_id not in source_units:
                    raise QualityProviderError(f"applied_units 引用了不存在的单元 {unit_id}。")
                rows.append(unit_id)
            applied_units[card_id] = rows
        for item in equivalent:
            units = applied_units.get(item["representative_id"])
            if relation == "equivalent" and not units:
                raise QualityProviderError(
                    f"代表卡 {item['representative_id']} 没有声明适用的单元，不能自动采用。"
                )
    return {
        "group_id": group_id,
        "members": sorted(set(members)),
        "relation": relation,
        "equivalent": equivalent,
        "distinct": distinct,
        "unresolved": unresolved,
        "applied_units": applied_units,
    }


def _as_repairable(error: QualityProviderError, location: str) -> ContentRepairError:
    return ContentRepairError(str(error), code="quality_protocol", location=location)


class OpenAICompatibleConceptGenerationProvider(_QualityClientMixin):
    name = "openai-compatible-quality-generation"

    def generate_candidates(self, request: ConceptScanRequest) -> ConceptScanResult:
        approved = "\n".join(f"- {item}" for item in request.approved_expressions) or "（无）"
        user = (
            "<task>\n"
            f"从下列原文中提出最多 {request.max_cards} 个可能有翻译风险的概念候选。\n"
            "只输出候选 JSON；批准由人工完成。\n"
            "</task>\n\n"
            "<approved_concepts>\n"
            f"{approved}\n"
            "</approved_concepts>\n\n"
            "<source_units>\n"
            f"{_source_units_block(request.units)}\n"
            "</source_units>"
        )
        unit_ids = {unit.unit_id for unit in request.units}
        unit_sources = {
            unit.unit_id: (unit.source_text, unit.source_sha256) for unit in request.units
        }

        def validate(raw: str) -> list[dict[str, Any]]:
            what = "概念候选生成端"
            try:
                data = _load_json_object(raw, what=what)
                _validate_top_level_keys(data, {"candidates"}, what)
                candidates = normalize_candidate_list(
                    data["candidates"],
                    unit_ids=unit_ids,
                    max_cards=request.max_cards,
                )
            except QualityProviderError as exc:
                raise _as_repairable(exc, "candidates") from exc
            # The storage layer verifies evidence again before a card is saved;
            # checking it here as well lets the model fix a fabricated excerpt
            # instead of losing the candidate.  Both use the same matcher, so a
            # candidate that passes here can never be rejected later for this.
            for index, candidate in enumerate(candidates):
                try:
                    verify_evidence(candidate.get("evidence"), unit_sources)
                except QualitySupportError as exc:
                    raise ContentRepairError(
                        str(exc),
                        code="unverified_evidence",
                        location=f"candidates[{index}].evidence",
                    ) from exc
            return candidates

        candidates, usage, repair = self._run_json_loop(
            CONCEPT_EXTRACTION_SYSTEM_PROMPT,
            user,
            kind="概念候选生成",
            validate=validate,
            control=request.control,
            invocation_id=request.batch_id,
        )
        return ConceptScanResult(
            batch_id=request.batch_id,
            candidates=candidates,
            provider=self.name,
            model=self.model,
            usage=usage,
            repair=repair,
        )


class OpenAICompatibleConceptCheckProvider(_QualityClientMixin):
    name = "openai-compatible-quality-check"

    def check_candidates(self, request: ConceptCheckRequest) -> ConceptCheckResult:
        user = (
            "<task>\n"
            "独立复核下列概念候选是否有本书原文依据、是否混淆不同概念、是否存在冲突。\n"
            "</task>\n\n"
            "<source_units>\n"
            f"{_source_units_block(request.units)}\n"
            "</source_units>\n\n"
            "<candidates>\n"
            f"{json.dumps(list(request.candidates), ensure_ascii=False)}\n"
            "</candidates>"
        )
        if request.expected:
            # A re-check answers specific frozen cards: the identity it must echo
            # is part of the request, never reconstructed after the answer.
            user += "\n\n<checks>\n" + json.dumps(
                [dict(item) for item in request.expected], ensure_ascii=False
            ) + "\n</checks>"
        if request.lookup_evidence:
            user += (
                "\n\n<lookup_evidence>\n"
                "下面这些命中是程序在本项目原文中做确定性检索得到的结果，只能作为只读查证材料：\n"
                "可以引用它们来回答疑问，但不得据此扩大范围、不得新增候选、不得改动条目之外的内容。\n"
                f"{json.dumps({key: list(value) for key, value in request.lookup_evidence.items()}, ensure_ascii=False)}\n"
                "</lookup_evidence>"
            )
        expected_count = len(request.candidates)
        live_sources = {
            str(unit.unit_id): (str(unit.source_text), str(unit.source_sha256))
            for unit in request.units
        }
        frozen_expected = tuple(dict(item) for item in request.expected)

        def validate(raw: str) -> list[dict[str, Any]]:
            what = "概念独立检查端"
            try:
                data = _load_json_object(raw, what=what)
                _validate_top_level_keys(data, {"results"}, what)
                return normalize_check_list(
                    data["results"],
                    expected_count=expected_count,
                    candidates=[dict(item) for item in request.candidates],
                    unit_sources=live_sources,
                    expected=frozen_expected or None,
                )
            except QualityProviderError as exc:
                raise _as_repairable(exc, "results") from exc

        checks, usage, repair = self._run_json_loop(
            CONCEPT_CHECK_SYSTEM_PROMPT,
            user,
            kind="概念独立检查",
            validate=validate,
            control=request.control,
            invocation_id=request.batch_id,
        )
        return ConceptCheckResult(
            batch_id=request.batch_id,
            checks=checks,
            provider=self.name,
            model=self.model,
            usage=usage,
            repair=repair,
        )


class OpenAICompatibleConceptResolutionProvider(_QualityClientMixin):
    """Group relation judgment over the existing concept (check) configuration.

    It reuses the project's concept channel and the same bounded 3-round content
    repair loop as the other concept calls. It never retries HTTP/auth/quota
    failures, and every message list is local to one group.
    """

    name = "openai-compatible-concept-resolution"

    def resolve_group(self, request: ConceptResolutionRequest) -> ConceptResolutionResult:
        members = list(request.members)
        member_ids = {str(item.get("card_id") or "") for item in members if str(item.get("card_id") or "")}
        unit_block = _source_units_block(
            [
                ConceptUnitRef(unit_id=unit_id, source_text=source[0], source_sha256=source[1])
                for unit_id, source in sorted(request.unit_sources.items())
            ]
        )
        user = (
            "<task>\n"
            "判断下面这组概念卡是同一概念、不同义项还是无法确定，并给出可适用的单元。\n"
            "只输出约定 JSON；是否采用由本地程序决定，你不需要也不得批准任何卡。\n"
            "</task>\n\n"
            f"<group>\ngroup_id: {request.group_id}\n"
            f"成员卡：{', '.join(sorted(member_ids))}\n</group>\n\n"
            "<cards>\n"
            f"{json.dumps(members, ensure_ascii=False)}\n"
            "</cards>\n\n"
            "<source_units>\n"
            f"{unit_block}\n"
            "</source_units>"
        )

        def validate(raw: str) -> dict[str, Any]:
            what = "概念组辨析端"
            try:
                data = _load_json_object(raw, what=what)
                return normalize_resolution(
                    data,
                    member_ids=member_ids,
                    group_id=request.group_id,
                    source_units=dict(request.unit_sources),
                )
            except QualityProviderError as exc:
                raise _as_repairable(exc, "resolution") from exc

        payload, usage, repair = self._run_json_loop(
            CONCEPT_RESOLUTION_SYSTEM_PROMPT,
            user,
            kind="概念组辨析",
            validate=validate,
            control=request.control,
            invocation_id=request.group_id,
        )
        return ConceptResolutionResult(
            group_id=request.group_id,
            relation=str(payload.get("relation") or "unresolved"),
            payload=payload,
            provider=self.name,
            model=self.model,
            usage=usage,
            repair=repair,
        )


class OpenAICompatibleEditorialSuggestionProvider(_QualityClientMixin):
    name = "openai-compatible-quality-editorial"

    def suggest(self, request: EditorialSuggestionRequest) -> EditorialSuggestionResult:
        adjacent = "\n\n".join(request.adjacent_source) or "（无）"
        if request.approved_cards:
            approved = "\n\n".join(
                f"[ref v{card.get('card_revision', 0)}] {card.get('text', '')}".strip()
                for card in request.approved_cards
            ) or "（无）"
        else:
            approved = "\n".join(f"- {item}" for item in request.approved_expressions) or "（无）"
        user = (
            "<task>\n"
            "对下面这个已保存译文单元提出最多 5 条局部表达建议；没有必要时返回空列表。\n"
            "</task>\n\n"
            "<source_text>\n"
            f"{request.source_text}\n"
            "</source_text>\n\n"
            "<current_translation>\n"
            f"{request.translated_text}\n"
            "</current_translation>\n\n"
            "<adjacent_source>\n"
            f"{adjacent}\n"
            "</adjacent_source>\n\n"
            "<approved_concepts>\n"
            f"{approved}\n"
            "</approved_concepts>"
        )
        def validate(raw: str) -> list[dict[str, Any]]:
            what = "表达建议端"
            try:
                data = _load_json_object(raw, what=what)
                _validate_top_level_keys(data, {"suggestions"}, what)
                return normalize_suggestion_list(data["suggestions"])
            except QualityProviderError as exc:
                raise _as_repairable(exc, "suggestions") from exc

        suggestions, usage, repair = self._run_json_loop(
            EDITORIAL_SUGGESTION_SYSTEM_PROMPT,
            user,
            kind="表达建议",
            validate=validate,
            control=request.control,
            invocation_id=request.unit_id,
        )
        return EditorialSuggestionResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            translation_revision=request.translation_revision,
            suggestions=suggestions,
            provider=self.name,
            model=self.model,
            usage=usage,
            repair=repair,
        )


class FakeQualityProvider:
    """Deterministic offline provider used by tests and local UI acceptance.

    It never performs a network call. The generated candidate always cites a
    real excerpt from the first scanned unit, so the evidence gate is
    exercised rather than bypassed.
    """

    name = "fake-quality"
    model = "fake-quality-1"

    def __init__(
        self,
        *,
        candidates: Sequence[dict[str, Any]] | None = None,
        resolutions: Any | None = None,
        question_kind: str = "preference",
        question_status: str = "resolved",
        assessments: Any | None = None,
    ) -> None:
        self._candidates = list(candidates) if candidates is not None else None
        self._resolutions = resolutions
        # How the offline double answers a draft's open questions. The default
        # states a naming preference, which is exactly the case the automatic
        # path is meant to settle without a human click. Set ``unresolved`` to
        # exercise the blocking path.
        self._question_kind = str(question_kind or "preference")
        self._question_status = str(question_status or "resolved")
        self._assessments = assessments
        self.calls: list[str] = []
        self.resolution_requests: list[ConceptResolutionRequest] = []

    def generate_candidates(self, request: ConceptScanRequest) -> ConceptScanResult:
        self.calls.append("generate")
        if self._candidates is not None:
            return ConceptScanResult(
                batch_id=request.batch_id,
                candidates=[dict(item) for item in self._candidates],
                provider=self.name,
                model=self.model,
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        units = list(request.units)
        if not units:
            return ConceptScanResult(
                batch_id=request.batch_id, candidates=[], provider=self.name, model=self.model
            )
        first = units[0]
        excerpt = _fake_excerpt(first.source_text)
        candidate = {
            "expressions": ["workforce attachment"],
            "meaning": "劳动力与就业之间的联结强度，不是情感依恋。",
            "applies_when": "讨论劳动力市场依附程度时。",
            "acceptable_translations": ["劳动力依附程度", "就业依附程度"],
            "confusions": ["情感依恋"],
            "evidence": [
                {
                    "unit_id": first.unit_id,
                    "source_sha256": first.source_sha256,
                    "source_excerpt": excerpt,
                }
            ],
            "open_questions": ["本书是否已有既定译名"],
            "priority": 5,
        }
        return ConceptScanResult(
            batch_id=request.batch_id,
            candidates=[candidate],
            provider=self.name,
            model=self.model,
            usage={"input_tokens": 0, "output_tokens": 0},
        )

    def _fake_assessment(self, request: ConceptCheckRequest, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """A structured answer to one candidate's open questions.

        It is built through the same validator the real provider uses, so the
        offline path cannot accept a shape the live path would reject.
        """

        sources = {
            str(unit.unit_id): (str(unit.source_text), str(unit.source_sha256))
            for unit in request.units
        }
        evidence = [
            dict(item)
            for item in (candidate.get("evidence") or [])
            if str(item.get("unit_id") or "") in sources
        ]
        questions = [str(item) for item in (candidate.get("open_questions") or []) if str(item).strip()]
        expressions = [str(item) for item in (candidate.get("expressions") or []) if str(item).strip()]
        preferred = str(((candidate.get("acceptable_translations") or [""]) or [""])[0])
        bindings = []
        if expressions:
            bindings.append(
                {
                    "binding_id": "b-1",
                    "expressions": expressions,
                    "kind": "term" if preferred else "distinction",
                    "preferred_translation": preferred,
                    "acceptable_variants": list(candidate.get("acceptable_translations") or [])[1:],
                    "guidance": "" if preferred else "离线替身：表达间需要区分，不给出单一译名。",
                    "applies_when": str(candidate.get("applies_when") or ""),
                    "evidence": evidence,
                }
            )
        assessment = {
            "schema_version": ca.ASSESSMENT_SCHEMA_VERSION,
            "question_results": [
                {
                    "question_index": index,
                    "kind": self._question_kind,
                    "status": self._question_status,
                    "answer": preferred if self._question_status == "resolved" else "",
                    "reason": "离线替身：在本次可见原文内作答。",
                    "evidence": evidence if self._question_status == "resolved" else [],
                    "affected_binding_ids": ["b-1"],
                }
                for index, _question in enumerate(questions)
            ],
            "bindings": bindings,
            "lookup_expressions": [],
        }
        return ca.normalize_assessment(
            assessment,
            questions=questions,
            allowed_expressions=expressions,
            unit_sources=sources,
        )

    def check_candidates(self, request: ConceptCheckRequest) -> ConceptCheckResult:
        self.calls.append("check")
        if callable(self._assessments):
            scripted = list(self._assessments(request))
        else:
            scripted = list(self._assessments or [])
        checks = []
        for index, candidate in enumerate(request.candidates):
            questions = [
                str(item)
                for item in (candidate.get("open_questions") or [])
                if str(item).strip()
            ]
            row = {
                "index": index,
                "expression": (candidate.get("expressions") or [""])[0],
                "verdict": "supported",
                "reasons": ["摘录存在于本次原文中。"],
                "notes": "",
            }
            if index < len(request.expected):
                # The double echoes the frozen identity exactly like the formal
                # provider must, so the pairing check is exercised offline too.
                pair = dict(request.expected[index])
                row["card_id"] = str(pair.get("card_id") or "")
                row["draft_revision"] = int(pair.get("draft_revision") or 0)
                row["content_fingerprint"] = str(pair.get("content_fingerprint") or "")
            if questions:
                if index < len(scripted) and scripted[index] is not None:
                    row["automation_assessment"] = scripted[index]
                elif self._question_status != "off":
                    row["automation_assessment"] = self._fake_assessment(request, candidate)
            checks.append(row)
        return ConceptCheckResult(
            batch_id=request.batch_id,
            checks=checks,
            provider=self.name,
            model=self.model,
            usage={"input_tokens": 0, "output_tokens": 0},
        )

    def resolve_group(self, request: ConceptResolutionRequest) -> ConceptResolutionResult:
        """Offline group judgment.

        A scripted payload (``resolutions``) or callable always wins. Otherwise
        the double answers deterministically from the input it was handed: cards
        that carry the same meaning text are equivalent (lowest card id becomes
        the representative), and anything else is unresolved. That keeps the
        double conservative — it never invents a relation the input does not
        show — while still exercising the real adoption rules.
        """

        self.calls.append("resolve")
        self.resolution_requests.append(request)
        if callable(self._resolutions):
            payload = self._resolutions(request)
        elif self._resolutions:
            payload = dict(self._resolutions)
        else:
            members = [dict(item) for item in request.members]
            card_ids = [str(item.get("card_id") or "") for item in members if str(item.get("card_id") or "")]
            meanings = {
                str((item.get("payload") or {}).get("meaning") or "") for item in members
            }
            applied: dict[str, list[str]] = {}
            if len(meanings) == 1 and card_ids:
                representative = sorted(card_ids)[0]
                for item in members:
                    evidence_units = [
                        str(evidence.get("unit_id") or "")
                        for evidence in ((item.get("payload") or {}).get("evidence") or [])
                        if isinstance(evidence, dict)
                    ]
                    applied[str(item.get("card_id") or "")] = [
                        unit for unit in evidence_units if unit in request.unit_sources
                    ]
                payload = {
                    "group_id": request.group_id,
                    "members": card_ids,
                    "relation": "equivalent",
                    "equivalent": [
                        {
                            "representative_id": representative,
                            "member_ids": card_ids,
                            "reason": "离线替身：含义文本一致。",
                        }
                    ],
                    "distinct": [],
                    "unresolved": [],
                    "applied_units": applied,
                }
            else:
                payload = {
                    "group_id": request.group_id,
                    "members": card_ids,
                    "relation": "unresolved",
                    "equivalent": [],
                    "distinct": [],
                    "unresolved": card_ids,
                    "applied_units": {},
                }
        normalized = normalize_resolution(
            payload,
            member_ids={str(item.get("card_id") or "") for item in request.members},
            group_id=request.group_id,
            source_units=dict(request.unit_sources),
        )
        return ConceptResolutionResult(
            group_id=request.group_id,
            relation=str(normalized.get("relation") or "unresolved"),
            payload=normalized,
            provider=self.name,
            model=self.model,
            usage={"input_tokens": 0, "output_tokens": 0},
        )

    def suggest(self, request: EditorialSuggestionRequest) -> EditorialSuggestionResult:
        self.calls.append("suggest")
        # Record what the editorial model actually received so tests can prove
        # the bound approved cards (content + version) were forwarded, not a
        # bare expression list.
        self.last_editorial_request = request
        excerpt = _fake_excerpt(request.source_text)
        translation_excerpt = _fake_excerpt(request.translated_text)
        suggestions = [
            {
                "kind": "expression",
                "source_excerpt": excerpt,
                "translation_excerpt": translation_excerpt,
                "suggested_expression": "劳动力依附程度",
                "reason": "原文指劳动力市场依附，不是情感依恋。",
            }
        ]
        return EditorialSuggestionResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            translation_revision=request.translation_revision,
            suggestions=suggestions,
            provider=self.name,
            model=self.model,
            usage={"input_tokens": 0, "output_tokens": 0},
        )


def _fake_excerpt(text: str, limit: int = 40) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:limit]
