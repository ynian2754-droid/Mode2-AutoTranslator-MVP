"""HTTP routes; business rules stay in PipelineManager."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.api_settings import ApiConfig
from core.exceptions import ConflictError, PipelineError
try:
    from core.segmenter import DEFAULT_TARGET_WORDS, validate_target_words
except ImportError:  # Backward compatibility until the segmenter API is upgraded.
    from core.segmenter import validate_max_words as validate_target_words

    DEFAULT_TARGET_WORDS = 500
from providers.api_client import OpenAICompatibleClient, ProviderRequestError
from web.schemas import (
    ApiSettingsRequest,
    ConcurrencySettingsRequest,
    DecisionRequest,
    EditorialSuggestionRequest,
    ManualTranslationRequest,
    OcGoCompatibilityRequest,
    OutputFormat,
    OutputRequest,
    PipelineStartRequest,
    ProjectCreateRequest,
    ProjectRequest,
    QualityBatchRetryRequest,
    QualityCardBatchItem,
    QualityCardBatchRequest,
    QualityCardRequest,
    QualityPrepareRequest,
    QualityReferenceModeRequest,
    QualityScanBatchRequest,
    QualityScanPlanRequest,
    RetranslateUnitRequest,
    ResegmentProjectRequest,
    SegmentationSettingsRequest,
    TranslationContextSettingsRequest,
    UnitActionRequest,
    _segmentation_values_match,
)


ApiScope = Literal["inspection", "translation"]

def _settings_values(payload: ApiSettingsRequest) -> dict:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()

def _segmentation_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose both names during the API transition without writing state."""
    response = dict(payload)
    default_target = response.get("default_target_words", DEFAULT_TARGET_WORDS)
    try:
        default_target = validate_target_words(default_target)
    except (TypeError, ValueError):
        default_target = DEFAULT_TARGET_WORDS

    current_target = response.get("target_words")
    if current_target is None:
        current_target = response.get("max_words", default_target)

    response.update(
        {
            "target_words": current_target,
            "max_words": current_target,
            "default_target_words": default_target,
            "default_max_words": default_target,
        }
    )
    return response

def _error_response(exc: Exception) -> HTTPException:
    status = 409 if isinstance(exc, ConflictError) else 400
    return HTTPException(status_code=status, detail=str(exc))

def create_api_router(manager) -> APIRouter:
    """Build routes against an injected manager for simple testing."""
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "mode2-auto-translator-mvp"}

    @router.get("/project")
    def project() -> dict:
        try:
            return manager.snapshot()
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/segmentation-settings")
    def segmentation_settings() -> dict:
        try:
            return _segmentation_response(manager.segmentation_settings())
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/concurrency-settings")
    def concurrency_settings() -> dict:
        try:
            return manager.concurrency_settings()
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/translation-context-settings")
    def translation_context_settings() -> dict:
        try:
            return manager.translation_context_settings()
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/output-status")
    def output_status() -> dict:
        try:
            return manager.output_status()
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/output")
    def generate_output(payload: OutputRequest | None = None) -> dict:
        try:
            return manager.generate_output(payload.format if payload is not None else None)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/output/latest")
    def latest_output(format: OutputFormat | None = None) -> FileResponse:
        try:
            path = manager.output_file_path(format)
        except PipelineError as exc:
            raise _error_response(exc) from exc
        media_type = {
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".pdf": "application/pdf",
            ".epub": "application/epub+zip",
        }.get(path.suffix.casefold(), "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @router.put("/project/segmentation-settings")
    def update_segmentation_settings(payload: SegmentationSettingsRequest) -> dict:
        try:
            return _segmentation_response(manager.update_segmentation_settings(payload.resolved_words()))
        except (PipelineError, ValueError) as exc:
            raise _error_response(exc) from exc

    @router.put("/project/concurrency-settings")
    def update_concurrency_settings(payload: ConcurrencySettingsRequest) -> dict:
        try:
            return manager.update_concurrency_settings(payload.max_concurrency)
        except (PipelineError, ValueError) as exc:
            raise _error_response(exc) from exc

    @router.put("/project/translation-context-settings")
    def update_translation_context_settings(payload: TranslationContextSettingsRequest) -> dict:
        try:
            return manager.update_translation_context_settings(payload.resolved())
        except (PipelineError, ValueError) as exc:
            raise _error_response(exc) from exc

    @router.post("/project/resegment")
    def resegment_project(payload: ResegmentProjectRequest) -> dict:
        try:
            return manager.resegment_source(confirm_reset=payload.confirm_reset)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/projects")
    def projects() -> dict:
        return {"projects": manager.list_projects()}

    @router.post("/projects")
    def create_named_project(payload: ProjectCreateRequest) -> dict:
        try:
            project = manager.create_named_project(payload.name)
            return {"status": "ok", "project": project}
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.delete("/projects/{project_id}")
    def delete_project(project_id: str) -> dict:
        try:
            delete = getattr(manager, "delete_project", None)
            if not callable(delete):
                raise PipelineError("当前运行模式不支持删除项目。")
            return delete(project_id)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/projects/{project_id}")
    def project_detail(project_id: str) -> dict:
        try:
            return manager.project_detail(project_id)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/projects/{project_id}/select")
    def select_project(project_id: str) -> dict:
        try:
            project = manager.select_project(project_id)
            return {"status": "ok", "project": project}
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/settings")
    def settings() -> dict:
        return manager.api_settings.snapshot()

    @router.put("/settings/oc-go")
    def update_oc_go_compatibility(payload: OcGoCompatibilityRequest) -> dict:
        try:
            settings = manager.api_settings.set_oc_go_compatibility(payload.enabled)
            return {
                "status": "ok",
                "enabled": payload.enabled,
                "settings": settings,
            }
        except (OSError, ValueError) as exc:
            raise _error_response(exc) from exc

    @router.put("/settings/{scope}")
    def update_settings(scope: ApiScope, payload: ApiSettingsRequest) -> dict:
        try:
            config = manager.api_settings.update(scope, _settings_values(payload))
            return {"scope": scope, "config": config.to_dict()}
        except ValueError as exc:
            raise _error_response(exc) from exc

    @router.post("/settings/{scope}/test")
    def test_settings(scope: ApiScope, payload: ApiSettingsRequest | None = None) -> dict:
        try:
            config = (
                ApiConfig.from_mapping(_settings_values(payload))
                if payload is not None
                else manager.api_settings.get(scope)
            )
            usage = OpenAICompatibleClient(config).test_connection()
            return {
                "status": "ok",
                "scope": scope,
                "model": config.model,
                "message": "API 连接成功。",
                "usage": usage,
            }
        except (ValueError, ProviderRequestError) as exc:
            status = 400 if isinstance(exc, ValueError) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.post("/settings/{scope}/models")
    def list_models(scope: ApiScope, payload: ApiSettingsRequest | None = None) -> dict:
        try:
            if payload is None:
                config = manager.api_settings.get(scope)
            else:
                values = _settings_values(payload)
                # `/models` does not use a model ID.  Keep the normal model
                # requirement for save/test/translation, but allow discovery
                # before the user knows which model names the gateway exposes.
                if not str(values.get("model") or "").strip():
                    values["model"] = "model-list-placeholder"
                config = ApiConfig.from_mapping(values)
            models = OpenAICompatibleClient(config).list_models()
            return {"status": "ok", "scope": scope, "models": models}
        except (ValueError, ProviderRequestError) as exc:
            status = 400 if isinstance(exc, ValueError) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/units/{unit_id}")
    def unit(unit_id: str) -> dict:
        try:
            return manager.get_unit(unit_id)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.put("/units/{unit_id}/translation")
    def save_translation(unit_id: str, payload: ManualTranslationRequest) -> dict:
        try:
            return manager.save_translation(
                unit_id,
                payload.translation,
                expected_source_sha256=payload.source_sha256,
                expected_translation_revision=payload.expected_translation_revision,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/units/{unit_id}/review")
    def review_unit(unit_id: str, payload: UnitActionRequest | None = None) -> dict:
        try:
            return manager.review_unit(
                unit_id,
                expected_source_sha256=payload.source_sha256 if payload is not None else None,
                expected_translation_revision=(
                    payload.expected_translation_revision if payload is not None else None
                ),
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/units/{unit_id}/retranslate")
    def retranslate_unit(unit_id: str, payload: RetranslateUnitRequest | None = None) -> dict:
        try:
            return manager.retranslate_unit(
                unit_id,
                expected_project_id=payload.expected_project_id if payload is not None else None,
                expected_source_sha256=payload.source_sha256 if payload is not None else None,
                expected_translation_revision=(
                    payload.expected_translation_revision if payload is not None else None
                ),
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project")
    def create_project(payload: ProjectRequest) -> dict:
        try:
            return manager.create_project(
                payload.source_text,
                demo_mode=False,
                max_concurrency=payload.max_concurrency,
                provider="openai-compatible",
                source_language=payload.source_language,
                target_language=payload.target_language,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/projects/import")
    async def import_project(
        file: UploadFile = File(...),
        max_concurrency: int = Form(3),
        source_language: str = Form("English"),
        target_language: str = Form("简体中文"),
    ) -> dict:
        try:
            content = await file.read()
            return manager.import_source_file(
                file.filename or "source",
                content,
                demo_mode=False,
                max_concurrency=max_concurrency,
                provider="openai-compatible",
                source_language=source_language,
                target_language=target_language,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/pipeline/start")
    def start_pipeline(payload: PipelineStartRequest | None = None) -> dict:
        try:
            return manager.start(payload.unit_ids if payload is not None else None)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/pipeline/stop")
    def stop_pipeline() -> dict:
        try:
            return manager.stop()
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/quality-support")
    def quality_support(expected_project_id: str | None = None) -> dict:
        """Read concept cards and scan coverage. Never triggers a model call."""
        try:
            return manager.quality_support(expected_project_id=expected_project_id)
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.put("/project/quality-support/reference-mode")
    def set_reference_mode(payload: QualityReferenceModeRequest) -> dict:
        """Switch the project between manual and automatic reference mode."""
        try:
            return manager.set_reference_mode(
                payload.reference_mode,
                expected_project_id=payload.expected_project_id,
                expected_revision=payload.expected_revision,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/quality-support/prepare")
    def prepare_quality_support(payload: QualityPrepareRequest) -> dict:
        """Run one phase of the automatic reference preparation."""
        try:
            return manager.quality_prepare(
                phase=payload.phase,
                plan={"prepare_id": payload.prepare_id} if payload.prepare_id else None,
                unit_ids=payload.unit_ids,
                current_unit_id=payload.current_unit_id,
                max_source_words=payload.max_source_words,
                max_parallel_batches=payload.max_parallel_batches,
                expected_project_id=payload.expected_project_id,
                expected_revision=payload.expected_revision,
                additional_work_limit=payload.additional_work_limit,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/quality-support/prepare/status")
    def prepare_quality_status(
        expected_project_id: str,
        prepare_id: str | None = None,
    ) -> dict:
        """Read the current prepare state and in-memory progress without writes."""
        try:
            return manager.quality_prepare_status(
                expected_project_id=expected_project_id,
                prepare_id=prepare_id,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/quality-support/plan")
    def plan_quality_scan(payload: QualityScanPlanRequest | None = None) -> dict:
        """Describe a bounded scan. Never triggers a model call."""
        try:
            return manager.plan_quality_scan(
                scope=payload.scope if payload is not None else "selected",
                unit_ids=payload.unit_ids if payload is not None else None,
                current_unit_id=payload.current_unit_id if payload is not None else None,
                expected_project_id=(
                    payload.expected_project_id if payload is not None else None
                ),
                max_parallel_batches=(
                    payload.max_parallel_batches if payload is not None else 1
                ),
                max_source_words=payload.max_source_words if payload is not None else None,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/quality-support/scan")
    def scan_quality_batch(payload: QualityScanBatchRequest) -> dict:
        """Run one bounded generate+check batch and store candidates only."""
        try:
            return manager.scan_quality_batch(
                batch_id=payload.batch_id,
                unit_ids=payload.unit_ids,
                expected_project_id=payload.expected_project_id,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.put("/project/quality-support/cards/{card_id}")
    def update_quality_card(card_id: str, payload: QualityCardRequest) -> dict:
        try:
            return manager.update_quality_card(
                card_id,
                payload.action,
                content=payload.content,
                expected_project_id=payload.expected_project_id,
                expected_revision=payload.expected_revision,
                expected_draft_revision=payload.expected_draft_revision,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/quality-support/cards/batch-action")
    def batch_quality_card_action(payload: QualityCardBatchRequest) -> dict:
        """Approve, defer or reject several pending cards in one atomic write."""
        try:
            return manager.batch_quality_card_action(
                payload.action,
                [item.model_dump() for item in payload.items],
                expected_revision=payload.expected_revision,
                expected_project_id=payload.expected_project_id,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/project/quality-support/batches/{batch_id}/retry")
    def retry_quality_batch(batch_id: str, payload: QualityBatchRetryRequest) -> dict:
        """Retry exactly one failed generation/check batch, after confirmation.

        HTTP success only means the recovery ran and was saved; the response
        carries the structured outcome, so the page never has to read a model
        verdict out of an HTTP status.
        """
        try:
            return manager.retry_quality_batch(
                batch_id,
                expected_project_id=payload.expected_project_id,
                expected_revision=payload.expected_revision,
                allow_parallel=payload.allow_parallel,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.get("/project/quality-support/affected-units")
    def quality_affected_units(expected_project_id: str | None = None) -> dict:
        try:
            return manager.quality_affected_units(
                expected_project_id=expected_project_id
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/units/{unit_id}/editorial-suggestions")
    def editorial_suggestions(unit_id: str, payload: EditorialSuggestionRequest | None = None) -> dict:
        try:
            return manager.editorial_suggestions(
                unit_id,
                expected_project_id=payload.expected_project_id if payload is not None else None,
                expected_source_sha256=payload.source_sha256 if payload is not None else None,
                expected_translation_revision=(
                    payload.expected_translation_revision if payload is not None else None
                ),
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    @router.post("/units/{unit_id}/decision")
    def decide(unit_id: str, payload: DecisionRequest) -> dict:
        try:
            return manager.decide(
                unit_id,
                payload.decision,
                translation=payload.translation,
                expected_source_sha256=payload.source_sha256,
            )
        except PipelineError as exc:
            raise _error_response(exc) from exc

    return router
