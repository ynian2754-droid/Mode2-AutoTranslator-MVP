"""Read-only editorial suggestions bound to the saved unit and reference version."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core import project_state, quality_requests, unit_requests, unit_state, unit_validation
from core.exceptions import ConflictError, PipelineError
from core.project_state import ProjectStateCell
from core.provider_routing import ProviderRouter
from core.quality_support import normalize_quality_support, select_reference_cards
from providers.quality_provider import EditorialSuggestionRequest, QualityProviderError
from providers.repair_loop import ContentRepairExhausted, RepairControl


class EditorialWorkflow:
    def __init__(self, cell: ProjectStateCell, providers: ProviderRouter) -> None:
        self.cell = cell
        self.providers = providers

    def request_inputs_locked(
        self,
        unit: dict[str, Any],
    ) -> tuple[EditorialSuggestionRequest, tuple[str, str, int]]:
        unit_state.ensure_unit_feedback_fields(unit)
        context = unit_requests.unit_translation_context(self.cell.state, unit)
        adjacent = tuple(
            value
            for key in ("previous_context", "next_context")
            for value in [context.get(key)]
            if isinstance(value, str) and value.strip()
        )
        support = normalize_quality_support(self.cell.state.get("quality_support"))
        approved_expressions = quality_requests.approved_expressions(support)
        # Hand the editorial model the actual approved card content (meaning,
        # acceptable translations, ...) frozen at a concrete version, not just a
        # bare expression list. Selection is bounded by the same character
        # budget as translation/review reference injection.
        selection = select_reference_cards(
            support,
            source_text=str(unit.get("source") or ""),
            adjacent_texts=list(adjacent),
        )
        approved_cards = tuple(
            {
                "card_id": str(card.get("card_id") or ""),
                "card_revision": int(card.get("card_revision") or 0),
                "expressions": list(card.get("expressions") or []),
                "text": str(card.get("text") or ""),
            }
            for card in (selection.get("cards") or [])
        )
        request = EditorialSuggestionRequest(
            project_id=str(self.cell.state.get("project", {}).get("id") or ""),
            unit_id=unit["id"],
            source_text=unit["source"],
            source_sha256=unit["source_sha256"],
            translated_text=unit["translation"],
            translation_revision=unit["translation_revision"],
            adjacent_source=adjacent,
            approved_expressions=approved_expressions,
            approved_cards=approved_cards,
        )
        return request, (
            unit["source_sha256"],
            unit["source"],
            unit["translation_revision"],
        )

    def suggestions(
        self,
        unit_id: str,
        *,
        expected_project_id: str | None = None,
        expected_source_sha256: str | None = None,
        expected_translation_revision: int | None = None,
    ) -> dict[str, Any]:
        """Return optional local wording suggestions for a saved translation."""
        with self.cell.lock:
            project_state.ensure_open(self.cell)
            project_state.validate_expected_project_id(self.cell, expected_project_id)
            unit = unit_state.find_unit(self.cell.state, unit_id)
            if not isinstance(unit.get("translation"), str) or not unit["translation"].strip():
                raise PipelineError("只有已保存译文的单元才能请求表达建议。")
            unit_validation.validate_unit_write_guard(
                unit,
                expected_source_sha256=expected_source_sha256,
                expected_translation_revision=expected_translation_revision,
            )
            request, binding = self.request_inputs_locked(unit)

        _generation, _checker, editorial, _resolution = self.providers.quality_channels()

        def before_attempt(round_no: int, api_calls: int) -> None:
            # Bound to the same unit identity the result is validated against,
            # so a repair round is never sent for a stale translation.
            with self.cell.lock:
                if self.cell.closed:
                    raise PipelineError("项目已关闭，不再发起下一轮模型修正。")
                current = unit_state.find_unit(self.cell.state, unit_id)
                source_sha256, source_text, revision = binding
                if (
                    str(current.get("source_sha256") or "") != source_sha256
                    or str(current.get("source") or "") != source_text
                    or int(current.get("translation_revision") or 0) != revision
                ):
                    raise PipelineError("单元内容已经变化，不再发起下一轮模型修正。")

        request = replace(
            request,
            control=RepairControl(
                invocation_id=unit_id, kind="表达建议", before_attempt=before_attempt
            ),
        )
        try:
            result = editorial.suggest(request)
        except ContentRepairExhausted as exc:
            raise PipelineError(str(exc)) from exc
        except QualityProviderError as exc:
            raise PipelineError(f"表达建议失败：{exc}") from exc
        except Exception as exc:
            raise PipelineError(f"表达建议失败：{exc}") from exc

        with self.cell.lock:
            if self.cell.closed:
                raise ConflictError("当前项目管理器已关闭，不能返回表达建议。")
            project_state.validate_expected_project_id(self.cell, expected_project_id)
            current = unit_state.find_unit(self.cell.state, unit_id)
            source_sha256, source_text, revision = binding
            if (
                str(current.get("source_sha256") or "") != source_sha256
                or str(current.get("source") or "") != source_text
                or int(current.get("translation_revision") or 0) != revision
            ):
                raise ConflictError("单元内容已经变化，表达建议已失效，请重新请求。")
            return {
                "status": "ok",
                "project_id": request.project_id,
                "unit_id": result.unit_id,
                "source_sha256": result.source_sha256,
                "translation_revision": result.translation_revision,
                "provider": result.provider,
                "model": result.model,
                "suggestions": result.suggestions,
                "note": "建议仅供人工选择，不会自动修改译文。",
            }
