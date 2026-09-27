"""Deterministic local providers used to demonstrate the complete workflow."""

from __future__ import annotations

import time
from typing import Any

import agent_quality
import mode2_common

from .base import (
    ReviewProvider,
    ReviewRequest,
    ReviewResult,
    TranslationProvider,
    TranslationRequest,
    TranslationResult,
)


class DemoTranslationProvider:
    name = "demo"
    model = "local-demo-translator"

    _PHRASES = {
        "Translation workflows become reliable when every unit has an immutable identifier.":
            "当每个单元都拥有不可变标识时，翻译流程才会可靠。",
        "Parallel workers can translate independent units while the controller preserves order.":
            "并行工作器可以处理相互独立的单元，同时由控制器保持原有顺序。",
        "A separate reviewer should inspect the source and target without inheriting hidden reasoning.":
            "独立校验端应当只检查源文和译文，不继承翻译端的隐藏推理。",
        "A failed review becomes a user decision instead of a silent overwrite.":
            "校验失败会转化为用户裁决，而不是被静默覆盖。",
        "Small corrections can return to review without rerunning the entire book.":
            "小范围修订可以重新进入校验，而不必重跑整本书。",
        "Every result keeps its source hash so stale work cannot enter the project.":
            "每个结果都保留源文哈希，因此过期结果不能进入项目。",
        "The local demo is intentionally transparent and is not a substitute for a real model.":
            "本地演示过程保持透明，不能替代真实模型。",
    }

    def translate(self, request: TranslationRequest) -> TranslationResult:
        # A small delay makes concurrent progress visible in the local UI.
        delay = 0.26 + (sum(ord(char) for char in request.unit_id) % 5) * 0.06
        time.sleep(delay)
        if request.context.get("force_review"):
            translated = request.source_text
        else:
            translated = self._PHRASES.get(request.source_text.strip())
            if translated is None:
                translated = (
                    "这是本地演示 Provider 生成的占位译文；接入真实模型后，"
                    "这里会替换为经过模型处理的中文内容。"
                )
        return TranslationResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            translated_text=translated,
            provider=self.name,
            model=self.model,
            usage={
                "input_tokens": max(1, len(request.source_text.split())),
                "output_tokens": max(1, len(translated.split())),
            },
        )


class DemoReviewProvider:
    name = "demo"
    model = "local-integrity-reviewer"

    _MESSAGES = {
        "source_copied_verbatim": "译文与源文完全相同。",
        "source_copy_similarity_high": "译文与源文相似度过高，疑似没有完成翻译。",
        "target_language_missing": "译文缺少目标语言内容。",
        "empty_translation": "译文为空。",
        "catastrophic_incomplete_translation": "检测到连续或大范围未翻译内容。",
        "eligible_word_accounting_mismatch": "源文词数核算与实际检查范围不一致。",
        "protected_anchor_substitution": "关键缩写或锚点发生异常替换。",
        "formula_identity_mismatch": "公式变量没有被完整保留。",
        "formula_fragment_mismatch": "公式片段顺序或内容发生变化。",
    }

    _SUGGESTIONS = {
        "source_copied_verbatim": "请重新翻译当前单元，不要直接复制源文内容。",
        "source_copy_similarity_high": "请重新检查当前源文，并输出真正的目标语言译文。",
        "target_language_missing": "请补充当前源文对应的目标语言译文，不要保留未翻译正文。",
        "empty_translation": "请根据当前源文生成非空译文。",
        "catastrophic_incomplete_translation": "请重新翻译当前单元并补齐明确缺失的内容。",
        "protected_anchor_substitution": "请保留当前源文中的关键缩写或锚点，并重新翻译其余正文。",
        "formula_identity_mismatch": "请重新核对公式变量，并按源文完整保留变量标识。",
        "formula_fragment_mismatch": "请重新核对公式片段的内容和顺序，不要改动其标识。",
    }

    def review(self, request: ReviewRequest) -> ReviewResult:
        blocks = [{"id": request.unit_id, "text": request.source_text}]
        roles = {request.unit_id: "translatable"}
        quality = agent_quality.validate_translation(
            blocks,
            [(request.unit_id, request.translated_text)],
            roles,
        )
        completeness = mode2_common.scan_translation_completeness(
            blocks,
            {request.unit_id: request.translated_text},
            {request.unit_id: {"role": "translate", "confidence": 1.0}},
        )

        issues: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*quality["hard_failures"], *completeness["hard_failures"]]:
            rule = str(item.get("rule") or "review_failure")
            block_id = str(item.get("block_id") or request.unit_id)
            key = (rule, block_id)
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                {
                    "rule": rule,
                    "severity": "error",
                    "block_id": block_id,
                    "message": self._MESSAGES.get(rule, "独立校验发现需要人工处理的问题。"),
                    "evidence": {
                        key: value
                        for key, value in item.items()
                        if key not in {"rule", "block_id"}
                    },
                }
            )
            issues[-1]["evidence"]["suggestion"] = self._SUGGESTIONS.get(
                rule,
                "请回到当前源文核对上述明确问题，并仅修正当前单元。",
            )
        for item in quality["advisories"]:
            rule = str(item.get("rule") or "advisory")
            issues.append(
                {
                    "rule": rule,
                    "severity": "warning",
                    "block_id": str(item.get("block_id") or request.unit_id),
                    "message": "存在提示性检查项，建议人工确认。",
                    "evidence": {
                        key: value
                        for key, value in item.items()
                        if key not in {"rule", "block_id"}
                    },
                }
            )
        verdict = "FAIL" if any(item["severity"] == "error" for item in issues) else "PASS"
        metrics = {
            "quality": quality["metrics"].get(request.unit_id, {}),
            "completeness": completeness["summary"],
            "hard_failure_count": sum(item["severity"] == "error" for item in issues),
            "warning_count": sum(item["severity"] == "warning" for item in issues),
        }
        return ReviewResult(
            unit_id=request.unit_id,
            source_sha256=request.source_sha256,
            verdict=verdict,
            issues=issues,
            metrics=metrics,
            provider=self.name,
            model=self.model,
        )
