"""OpenAI-compatible adapters kept separate from the local demo providers."""

from __future__ import annotations

import os
import json
import re
from collections import Counter
from dataclasses import replace

from core.api_settings import ApiConfig
from core.quality_support import terminology_mismatches

from .api_client import OpenAICompatibleClient, ProviderRequestError
from .base import (
    ReviewRequest,
    ReviewResult,
    TranslationRequest,
    TranslationResult,
)
from .prompts import REVIEW_SYSTEM_PROMPT, TRANSLATION_SYSTEM_PROMPT
from .repair_loop import (
    ContentRepairError,
    RepairControl,
    run_content_repair_loop,
)


class ReviewPayloadError(ProviderRequestError):
    """The model responded, but its review payload violated the local contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "review_payload",
        location: str = "payload",
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.location = str(location)


_TRANSLATION_REASONING_BLOCK_RE = re.compile(
    r"<(?P<tag>think|analysis|reasoning)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TRANSLATION_REASONING_MARKER_RE = re.compile(
    r"<(?P<closing>/)?(?P<tag>think|analysis|reasoning)\b[^>]*>",
    re.IGNORECASE,
)


def _reasoning_marker_signature(text: str) -> Counter[tuple[str, bool]]:
    return Counter(
        (
            match.group("tag").casefold(),
            bool(match.group("closing")),
        )
        for match in _TRANSLATION_REASONING_MARKER_RE.finditer(text)
    )


def _remove_reasoning_block(match: re.Match[str]) -> str:
    before = match.string[: match.start()]
    after = match.string[match.end() :]
    if (
        before
        and after
        and before[-1].isascii()
        and before[-1].isalnum()
        and after[0].isascii()
        and after[0].isalnum()
    ):
        return " "
    return ""


def _clean_translation_output(
    raw: str, source_text: str, term_rules: list[dict[str, object]] | None = None
) -> str:
    """Remove explicit model-reasoning blocks before a result can be stored.

    Content violations raise :class:`ContentRepairError` so the bounded repair
    loop can feed the specific violation back to the model instead of failing
    the unit outright.
    """
    candidate = str(raw or "").strip()
    if not candidate:
        raise ContentRepairError(
            "翻译端返回为空。", code="empty_output", location="response"
        )

    source_signature = _reasoning_marker_signature(source_text)
    if source_signature:
        if _reasoning_marker_signature(candidate) != source_signature:
            raise ContentRepairError(
                "翻译端返回的受保护标记与源文不一致。",
                code="protected_marker_mismatch",
                location="response",
            )
    else:
        candidate = _TRANSLATION_REASONING_BLOCK_RE.sub(_remove_reasoning_block, candidate).strip()
        if _TRANSLATION_REASONING_MARKER_RE.search(candidate):
            raise ContentRepairError(
                "翻译端返回包含未闭合的思维过程标记。",
                code="unclosed_reasoning_marker",
                location="response",
            )

    if not candidate:
        raise ContentRepairError(
            "翻译端清理后没有可保存的译文。",
            code="empty_after_cleanup",
            location="response",
        )
    source_replacement_count = str(source_text or "").count("\uFFFD")
    translated_replacement_count = candidate.count("\uFFFD")
    if translated_replacement_count > source_replacement_count:
        raise ContentRepairError(
            "译文新增了 Unicode 替代字符 U+FFFD，表示有字符可能在生成时丢失；"
            "请根据完整源文重新生成，不要猜测或扩散该标记。",
            code="replacement_character_introduced",
            location="response",
        )
    mismatches = terminology_mismatches(term_rules or [], candidate)
    if mismatches:
        first = mismatches[0]
        raise ContentRepairError(
            f"原文术语 {first['expression']} 的统一译名是「{first['canonical']}」；"
            f"当前译文{first['reason']}。请只修改该术语的译法并保持原文语义。",
            code="terminology_mismatch",
            location="translated_text",
        )
    return candidate


def _translation_feedback(context: dict[str, object]) -> tuple[list[str], str | None]:
    """Return only the allow-listed, non-empty feedback for one translation unit."""
    raw_suggestions = context.get("validation_suggestions")
    suggestions: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_suggestions, list):
        for raw_suggestion in raw_suggestions:
            if not isinstance(raw_suggestion, str):
                continue
            suggestion = raw_suggestion.strip()
            if suggestion and suggestion not in seen:
                suggestions.append(suggestion)
                seen.add(suggestion)

    if not suggestions:
        return [], None

    previous_translation = context.get("previous_translation")
    if not isinstance(previous_translation, str):
        previous_translation = None
    else:
        previous_translation = previous_translation.strip() or None
    return suggestions, previous_translation


def _translation_context_text(context: dict[str, object], key: str) -> str | None:
    """Read one request-local source context block without accepting arbitrary fields."""

    value = context.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _context_block(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def _terminology_rule_block(context: dict[str, object]) -> str:
    rows = context.get("terminology_rules")
    if not isinstance(rows, list):
        return ""
    lines = [
        f"{str(row['expression']).strip()} → {str(row['canonical']).strip()}"
        for row in rows if isinstance(row, dict)
        and str(row.get("expression") or "").strip()
        and str(row.get("canonical") or "").strip()
    ]
    return _context_block("terminology_rules", "\n".join(lines)) if lines else ""


def _build_translation_user_message(request: TranslationRequest) -> str:
    """Build one isolated request with explicit source-context boundaries."""
    context = request.context if isinstance(request.context, dict) else {}
    sections = [
        f"将以下 {request.source_language} 内容翻译为 {request.target_language}。",
        f"unit_id: {request.unit_id}",
    ]

    # Put every non-output datum before the target block.  In particular, the
    # next context must not be the last natural-language block the model sees;
    # otherwise a model can mistake it for a continuation of the unit.
    reference_sections: list[str] = []
    previous_context = _translation_context_text(context, "previous_context")
    if previous_context is not None:
        reference_sections.append(_context_block("previous_context", previous_context))
    next_context = _translation_context_text(context, "next_context")
    if next_context is not None:
        reference_sections.append(_context_block("next_context", next_context))

    user_edited_translation = _translation_context_text(context, "user_edited_translation")
    if user_edited_translation is not None:
        reference_sections.append(
            _context_block("user_edited_translation", user_edited_translation)
            + "\n这只是上一版已保存人工译文的参考，不是新的源文，也不是必须照抄的答案。"
        )

    suggestions, previous_translation = _translation_feedback(context)
    if previous_translation is not None:
        reference_sections.append(
            f"[Previous Translation]\n{previous_translation}"
        )
    if suggestions:
        reference_sections.append(
            "[Validation Suggestions]\n"
            + "\n".join(f"- {suggestion}" for suggestion in suggestions)
        )
        reference_sections.append(
            "[Task]\n"
            "请重新检查源文和必要的上一版译文，核实上述建议后定向修复当前单元。保留其他正确内容，修复必要的语法衔接，输出完整单元而非补丁。"
            "校验建议只是参考意见，不是需要翻译的内容；最终输出仍只能是当前单元的译文。"
        )

    concept_references = context.get("concept_references")
    if isinstance(concept_references, list) and concept_references:
        cards: list[str] = []
        for item in concept_references:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text:
                cards.append(text)
        if cards:
            block = "\n\n".join(
                f"[{index + 1}] {card}" for index, card in enumerate(cards)
            )
            reference_sections.append(
                _context_block("concept_reference", block)
                + "\n这些是本项目提供的概念参考，来源以各条标注为准，自动采用不等于人工批准。它们只帮助确定词义；"
                "原文语义优先。不要把卡片内容写进译文，也不要当作待翻译正文。"
            )
    term_block = _terminology_rule_block(context)
    if term_block:
        reference_sections.append(
            term_block + "\n这些规则只对应当前单元原文中明确命中的术语；同一义项必须使用所列唯一译名，不得轮换其他候选。"
        )
    if isinstance(context.get("concept_reference_version"), int):
        reference_sections.append(
            f"[Concept Reference Version]\n{context['concept_reference_version']}"
        )

    if reference_sections:
        sections.append(_context_block("reference_material", "\n\n".join(reference_sections)))

    # Keep this as the final substantial content block.  The model must have
    # no later source-like text to continue translating.
    sections.append(_context_block("text_to_translate", request.source_text))
    return "\n\n".join(sections)


def _build_review_user_message(request: ReviewRequest) -> str:
    """Build the review message with the same reference snapshot the translation used.

    Only the current unit's source and translation plus the explicitly allowed
    reference blocks are sent. The translator's hidden reasoning is never part
    of this message.
    """
    context = request.context if isinstance(request.context, dict) else {}
    sections = [
        f"检查 unit_id={request.unit_id} 的译文是否完整、忠实、符合目标语言。",
    ]
    reference_sections: list[str] = []

    adjacent = context.get("review_source_context")
    if isinstance(adjacent, dict):
        for name in ("previous", "next", "current"):
            value = adjacent.get(name)
            if isinstance(value, str) and value.strip():
                reference_sections.append(_context_block(f"{name}_source", value.strip()))
    elif isinstance(adjacent, list):
        # Legacy flat-list shape: infer direction by position. Kept only so an
        # older snapshot does not break rendering; new requests always carry a
        # direction-preserving dict.
        for index, value in enumerate(adjacent[:2]):
            if not isinstance(value, str) or not value.strip():
                continue
            name = "previous_source" if index == 0 else "next_source"
            reference_sections.append(
                _context_block(name, value.strip())
            )

    concept_references = context.get("concept_references")
    if isinstance(concept_references, list) and concept_references:
        cards: list[str] = []
        for item in concept_references:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text:
                cards.append(text)
        if cards:
            block = "\n\n".join(f"[{index + 1}] {card}" for index, card in enumerate(cards))
            reference_sections.append(
                _context_block("concept_reference", block)
                + "\n这些是本项目提供的概念参考，来源以各条标注为准，自动采用不等于人工批准。"
            )
    term_block = _terminology_rule_block(context)
    if term_block:
        reference_sections.append(
            term_block + "\n只检查与当前源文义项相符的规则；偏离统一译名属于术语问题，不要将其他候选当成等价替换。"
        )
    if isinstance(context.get("concept_reference_version"), int):
        label = "concept_reference_version"
        if context.get("concept_reference_is_new"):
            label = "concept_reference_version (本次单独审校重新冻结的参考版本)"
        reference_sections.append(f"[{label}]\n{context['concept_reference_version']}")

    role = context.get("structure_role")
    if isinstance(role, str) and role.strip():
        source = context.get("structure_role_source")
        origin = source if isinstance(source, str) and source.strip() else "document_layer"
        reference_sections.append(
            _context_block(
                "structure_role",
                f"{role.strip()}\n来源：{origin}",
            )
            + "\n结构角色只是提示；它不是判定错误的依据，也不能代替当前源文证据。"
        )

    if reference_sections:
        sections.append(_context_block("reference_material", "\n\n".join(reference_sections)))

    sections.append(_context_block("source_text", request.source_text))
    sections.append(_context_block("translated_text", request.translated_text))
    return "\n\n".join(sections)


class _OpenAICompatible:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        config: ApiConfig | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        if config is None:
            config = ApiConfig.from_mapping(
                {
                    "api_key": api_key if api_key is not None else os.getenv("AUTOTRANSLATOR_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
                    "base_url": base_url or os.getenv("AUTOTRANSLATOR_BASE_URL") or "https://api.openai.com/v1",
                    "model": model or os.getenv("AUTOTRANSLATOR_MODEL") or "gpt-4o-mini",
                    "timeout_seconds": os.getenv("AUTOTRANSLATOR_TIMEOUT", "90"),
                    "temperature": 0.1,
                }
            )
        elif any(value is not None for value in (api_key, base_url, model)):
            config = replace(
                config,
                api_key=config.api_key if api_key is None else api_key,
                base_url=config.base_url if base_url is None else base_url,
                model=config.model if model is None else model,
            )
        self.config = config
        self.client = OpenAICompatibleClient(config)
        self.model = config.model

    def _chat(
        self,
        system: str,
        user: str,
        *,
        response_format: dict[str, object] | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> tuple[str, dict[str, int]]:
        payload = messages if messages is not None else [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self.client.chat(
            [dict(message) for message in payload],
            response_format=response_format,
        )

    @staticmethod
    def _last_user_text(history: list[dict[str, str]]) -> str:
        return next(
            (message["content"] for message in reversed(history) if message["role"] == "user"),
            "",
        )


class OpenAICompatibleTranslationProvider(_OpenAICompatible):
    name = "openai-compatible"

    def translate(self, request: TranslationRequest) -> TranslationResult:
        system = TRANSLATION_SYSTEM_PROMPT
        user = _build_translation_user_message(request)
        control: RepairControl | None = getattr(request, "control", None)
        invocation_id = str(getattr(control, "invocation_id", "") or "")
        initial = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        def chat(
            history: list[dict[str, str]],
            response_format: dict[str, object] | None,
        ) -> tuple[str, dict[str, int]]:
            return self._chat(
                history[0]["content"],
                self._last_user_text(history),
                messages=history,
                **({} if response_format is None else {"response_format": response_format}),
            )

        translated_text, outcome = run_content_repair_loop(
            kind="translation",
            messages=initial,
            chat=chat,
            validate=lambda raw: _clean_translation_output(
                raw,
                request.source_text,
                request.context.get("terminology_rules")
                if isinstance(request.context, dict)
                and isinstance(request.context.get("terminology_rules"), list)
                else None,
            ),
            control=control,
            invocation_id=invocation_id,
        )
        return TranslationResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            translated_text=translated_text,
            provider=self.name,
            model=self.model,
            usage=outcome.usage,
            repair=outcome.payload(invocation_id=invocation_id),
        )


class OpenAICompatibleReviewProvider(_OpenAICompatible):
    name = "openai-compatible-review"

    _REVIEW_KEYS = {"verdict", "issues", "metrics"}
    _ISSUE_KEYS = {"rule", "severity", "block_id", "message", "evidence"}

    @staticmethod
    def _strip_leading_think_block(raw: str) -> str:
        candidate = raw.strip()
        if not candidate.startswith("<think>"):
            return candidate
        closing = candidate.find("</think>")
        if closing < 0:
            return candidate
        return candidate[closing + len("</think>") :].strip()

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        candidate = raw.strip()
        if not candidate.startswith("```"):
            return candidate
        lines = candidate.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return candidate
        opening = lines[0].strip().casefold()
        if opening not in {"```", "```json"}:
            return candidate
        return "\n".join(lines[1:-1]).strip()

    @classmethod
    def _parse_review_payload(cls, raw: str, unit_id: str) -> dict[str, object]:
        candidate = cls._strip_json_fence(cls._strip_leading_think_block(raw))
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ReviewPayloadError(
                "校验端返回的内容不是合法 JSON。",
                code="invalid_json",
                location="response",
            ) from exc

        if not isinstance(data, dict):
            raise ReviewPayloadError(
                "校验结果顶层必须是 JSON 对象。",
                code="payload_not_object",
                location="response",
            )
        if set(data) != cls._REVIEW_KEYS:
            raise ReviewPayloadError(
                "校验结果顶层字段必须且只能是 verdict、issues、metrics。",
                code="unexpected_top_level_fields",
                location="response",
            )

        verdict = data["verdict"]
        issues = data["issues"]
        metrics = data["metrics"]
        if not isinstance(verdict, str) or verdict not in {"PASS", "FAIL"}:
            raise ReviewPayloadError(
                "校验结果 verdict 必须是 PASS 或 FAIL。",
                code="invalid_verdict",
                location="verdict",
            )
        if not isinstance(issues, list):
            raise ReviewPayloadError(
                "校验结果 issues 必须是 JSON 数组。",
                code="invalid_issues",
                location="issues",
            )
        if not isinstance(metrics, dict):
            raise ReviewPayloadError(
                "校验结果 metrics 必须是 JSON 对象。",
                code="invalid_metrics",
                location="metrics",
            )

        has_error = False
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}] 必须是 JSON 对象。",
                    code="invalid_issue",
                    location=f"issues[{index}]",
                )
            if set(issue) != cls._ISSUE_KEYS:
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}] 必须且只能包含 rule、severity、block_id、message、evidence。",
                    code="unexpected_issue_fields",
                    location=f"issues[{index}]",
                )
            if not isinstance(issue["rule"], str) or not issue["rule"].strip():
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}].rule 必须是非空字符串。",
                    code="invalid_rule",
                    location=f"issues[{index}].rule",
                )
            severity = issue["severity"]
            if severity not in {"error", "warning"}:
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}].severity 必须是 error 或 warning。",
                    code="invalid_severity",
                    location=f"issues[{index}].severity",
                )
            if issue["block_id"] != unit_id:
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}].block_id 与当前 unit_id 不匹配。",
                    code="block_id_mismatch",
                    location=f"issues[{index}].block_id",
                )
            if not isinstance(issue["message"], str) or not issue["message"].strip():
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}].message 必须是非空字符串。",
                    code="invalid_message",
                    location=f"issues[{index}].message",
                )
            if not isinstance(issue["evidence"], dict):
                raise ReviewPayloadError(
                    f"校验结果 issues[{index}].evidence 必须是 JSON 对象。",
                    code="invalid_evidence",
                    location=f"issues[{index}].evidence",
                )
            if severity == "error":
                suggestion = issue["evidence"].get("suggestion")
                if not isinstance(suggestion, str) or not suggestion.strip():
                    raise ReviewPayloadError(
                        f"校验结果 issues[{index}].evidence.suggestion 在 error issue 中必须是非空字符串。",
                        code="missing_error_suggestion",
                        location=f"issues[{index}].evidence.suggestion",
                    )
            has_error = has_error or severity == "error"

        if not issues and verdict != "PASS":
            raise ReviewPayloadError(
                "没有 issue 时 verdict 必须是 PASS。",
                code="verdict_conflict",
                location="verdict",
            )
        if has_error and verdict != "FAIL":
            raise ReviewPayloadError(
                "包含 error issue 时 verdict 必须是 FAIL。",
                code="verdict_conflict",
                location="verdict",
            )
        if verdict == "FAIL" and not has_error:
            raise ReviewPayloadError(
                "verdict 为 FAIL 时至少需要一个 error issue。",
                code="verdict_conflict",
                location="verdict",
            )
        return data

    def _review_validate(self, raw: str, unit_id: str) -> dict[str, object]:
        """Adapt the review contract error into the loop's repairable error."""
        try:
            return self._parse_review_payload(raw, unit_id)
        except ReviewPayloadError as exc:
            raise ContentRepairError(
                str(exc), code=exc.code, location=exc.location
            ) from exc

    def review(self, request: ReviewRequest) -> ReviewResult:
        system = REVIEW_SYSTEM_PROMPT
        user = _build_review_user_message(request)
        control: RepairControl | None = getattr(request, "control", None)
        invocation_id = str(getattr(control, "invocation_id", "") or "")
        initial = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        def chat(
            history: list[dict[str, str]],
            response_format: dict[str, object] | None,
        ) -> tuple[str, dict[str, int]]:
            return self._chat(
                history[0]["content"],
                self._last_user_text(history),
                messages=history,
                **({} if response_format is None else {"response_format": response_format}),
            )

        data, outcome = run_content_repair_loop(
            kind="review",
            messages=initial,
            chat=chat,
            validate=lambda raw: self._review_validate(raw, request.unit_id),
            control=control,
            invocation_id=invocation_id,
            response_format={"type": "json_object"},
        )
        return ReviewResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            verdict=data["verdict"],
            issues=data["issues"],
            metrics=data["metrics"],
            provider=self.name,
            model=self.model,
            repair=outcome.payload(invocation_id=invocation_id),
        )
