"""Pure strict-import checks for one source-bound translation or review result."""

from __future__ import annotations

from typing import Any, Mapping

from core.exceptions import PipelineError
from providers.base import ReviewResult, TranslationResult


def validate_translation_result(unit: Mapping[str, Any], result: TranslationResult) -> None:
    if result.unit_id != unit["id"]:
        raise PipelineError("翻译结果 unit_id 不匹配，已拒绝导入。")
    if result.source_sha256 != unit["source_sha256"]:
        raise PipelineError("翻译结果源文哈希不匹配，已拒绝导入。")
    if not result.translated_text or not result.translated_text.strip():
        raise PipelineError("翻译结果为空，已拒绝导入。")

def validate_review_result(unit: Mapping[str, Any], result: ReviewResult) -> None:
    if result.unit_id != unit["id"]:
        raise PipelineError("校验结果 unit_id 不匹配，已拒绝导入。")
    if result.source_sha256 != unit["source_sha256"]:
        raise PipelineError("校验结果源文哈希不匹配，已拒绝导入。")
    if result.verdict not in {"PASS", "FAIL"}:
        raise PipelineError("校验结果 verdict 无效，已拒绝导入。")
    if not isinstance(result.issues, list):
        raise PipelineError("校验结果 issues 必须是数组，已拒绝导入。")
    if not isinstance(result.metrics, dict):
        raise PipelineError("校验结果 metrics 必须是对象，已拒绝导入。")

    has_error = False
    required_issue_keys = {"rule", "severity", "block_id", "message", "evidence"}
    for index, issue in enumerate(result.issues):
        if not isinstance(issue, dict) or set(issue) != required_issue_keys:
            raise PipelineError(f"校验结果 issues[{index}] 结构无效，已拒绝导入。")
        if not isinstance(issue["rule"], str) or not issue["rule"].strip():
            raise PipelineError(f"校验结果 issues[{index}].rule 无效，已拒绝导入。")
        if issue["severity"] not in {"error", "warning"}:
            raise PipelineError(f"校验结果 issues[{index}].severity 无效，已拒绝导入。")
        if issue["block_id"] != unit["id"]:
            raise PipelineError(f"校验结果 issues[{index}].block_id 不匹配，已拒绝导入。")
        if not isinstance(issue["message"], str) or not issue["message"].strip():
            raise PipelineError(f"校验结果 issues[{index}].message 无效，已拒绝导入。")
        if not isinstance(issue["evidence"], dict):
            raise PipelineError(f"校验结果 issues[{index}].evidence 无效，已拒绝导入。")
        if issue["severity"] == "error":
            suggestion = issue["evidence"].get("suggestion")
            if not isinstance(suggestion, str) or not suggestion.strip():
                raise PipelineError(
                    f"校验结果 issues[{index}].evidence.suggestion 在 error issue 中必须是非空字符串，已拒绝导入。"
                )
        has_error = has_error or issue["severity"] == "error"

    if not result.issues and result.verdict != "PASS":
        raise PipelineError("没有 issue 时 verdict 必须是 PASS，已拒绝导入。")
    if has_error and result.verdict != "FAIL":
        raise PipelineError("包含 error issue 时 verdict 必须是 FAIL，已拒绝导入。")
    if result.verdict == "FAIL" and not has_error:
        raise PipelineError("verdict 为 FAIL 时必须包含 error issue，已拒绝导入。")

