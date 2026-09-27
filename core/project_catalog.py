"""Project directory discovery and the active project session."""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.api_settings import ApiSettingsStore
from core.exceptions import ConflictError, PipelineError
from core.project_factory import ProjectFactory
from core.storage import ProjectStore
from pipeline import PipelineManager


class ProjectCatalogError(PipelineError):
    """A project directory cannot be listed, created, or opened."""


class ProjectSelectionRequired(ProjectCatalogError):
    """The editor was accessed before a project was selected."""


class ProjectCatalog:
    """Scan and create valid project runtimes under one book root."""

    _invalid_name = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    _reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }

    def __init__(self, book_root: Path | str) -> None:
        self.book_root = Path(book_root).resolve()
        self.book_root.mkdir(parents=True, exist_ok=True)

    def list_projects(self) -> list[dict[str, Any]]:
        if not self.book_root.is_dir():
            return []
        projects: list[dict[str, Any]] = []
        for path in self.book_root.iterdir():
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            state = self._read_state(path)
            if state is not None:
                projects.append(self._summary(path, state))
        projects.sort(key=lambda item: item["updated_at"], reverse=True)
        return projects

    def get_project(self, project_id: str) -> dict[str, Any]:
        path = self._project_path(project_id)
        state = self._read_state(path)
        if state is None:
            raise ProjectCatalogError("项目数据无效，缺少可识别的 project.json。")
        return self._summary(path, state)

    def project_path(self, project_id: str) -> Path:
        path = self._project_path(project_id)
        if self._read_state(path) is None:
            raise ProjectCatalogError("项目数据无效，缺少可识别的 project.json。")
        return path

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete one explicitly selected, catalog-managed project directory.

        The catalog deliberately validates the state before removing anything.
        This prevents the delete endpoint from becoming a general-purpose
        directory removal primitive for arbitrary children of ``book_root``.
        """

        # Check the un-resolved entry first.  ``_project_path`` resolves the
        # candidate for traversal protection, so checking only the resolved
        # path would miss a symlink that points at another in-root project.
        candidate = self.book_root / str(project_id or "")
        if candidate.is_symlink():
            raise ProjectCatalogError("不能删除符号链接项目。")
        path = self._project_path(project_id)
        if path == self.book_root or path.parent != self.book_root or path.is_symlink() or path.name.startswith("."):
            raise ProjectCatalogError("只能删除 book 目录内由项目目录管理的项目。")
        if self._read_state(path) is None:
            raise ProjectCatalogError("项目数据无效，缺少可识别的 project.json。")
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise ProjectCatalogError(f"项目删除失败：{exc}") from exc
        if path.exists():
            raise ProjectCatalogError("项目删除失败：目标目录仍然存在。")
        return {"id": str(project_id), "path": str(path), "deleted": True}

    def create_project(self, name: str) -> dict[str, Any]:
        clean_name = self._validate_name(name)
        if any(path.name.casefold() == clean_name.casefold() for path in self.book_root.iterdir()):
            raise ProjectCatalogError("同名项目已经存在，请换一个项目名称。")
        project_path = (self.book_root / clean_name).resolve()
        if project_path.parent != self.book_root:
            raise ProjectCatalogError("项目路径必须位于 book 目录内。")
        try:
            project_path.mkdir()
            (project_path / "sources").mkdir()
            (project_path / "output").mkdir()
            state = ProjectFactory().create_empty_state(clean_name)
            ProjectStore(project_path).save(state)
        except OSError as exc:
            raise ProjectCatalogError(f"项目目录创建失败：{exc}") from exc
        return self._summary(project_path, state)

    def migrate_legacy_project(self, legacy_runtime: Path | str) -> dict[str, Any] | None:
        """Copy the old single-runtime project into book once, without API keys."""
        if self.list_projects():
            return None
        legacy_path = Path(legacy_runtime).resolve()
        state = self._read_state(legacy_path)
        if state is None:
            return None
        project = state.get("project") or {}
        source_file = project.get("source_file") or {}
        preferred_name = project.get("name") or Path(str(source_file.get("name") or "")).stem or "旧版项目"
        try:
            base_name = self._validate_name(preferred_name)
        except ProjectCatalogError:
            base_name = "旧版项目"
        target = self.book_root / base_name
        suffix = 2
        while target.exists():
            target = self.book_root / f"{base_name}-{suffix}"
            suffix += 1
        try:
            target.mkdir()
            shutil.copy2(legacy_path / "project.json", target / "project.json")
            sources = legacy_path / "sources"
            if sources.is_dir():
                shutil.copytree(sources, target / "sources")
            else:
                (target / "sources").mkdir()
            (target / "output").mkdir()
        except OSError as exc:
            raise ProjectCatalogError(f"旧项目迁移失败：{exc}") from exc
        migrated = self._read_state(target)
        return self._summary(target, migrated) if migrated is not None else None

    def _project_path(self, project_id: str) -> Path:
        project_id = str(project_id or "")
        if not project_id or project_id in {".", ".."} or "/" in project_id or "\\" in project_id:
            raise ProjectCatalogError("项目标识无效。")
        path = (self.book_root / project_id).resolve()
        if path.parent != self.book_root or not path.is_dir():
            raise ProjectCatalogError("找不到指定项目。")
        return path

    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ProjectCatalogError("项目名称不能为空。")
        if cls._invalid_name.search(name) or name in {".", ".."}:
            raise ProjectCatalogError('项目名称包含 Windows 不允许的字符：< > : " / \\ | ? *')
        if name != name.rstrip(" ."):
            raise ProjectCatalogError("项目名称不能以空格或句点结尾。")
        if len(name) > 100:
            raise ProjectCatalogError("项目名称不能超过 100 个字符。")
        if name.split(".", 1)[0].upper() in cls._reserved_names:
            raise ProjectCatalogError("该项目名称是 Windows 保留名称，请换一个名称。")
        return name

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any] | None:
        state_path = path / "project.json"
        if not state_path.is_file():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        if state.get("schema_version") != 1:
            return None
        if not isinstance(state.get("project"), dict):
            return None
        if not isinstance(state.get("config"), dict) or not isinstance(state.get("units"), list):
            return None
        return state

    @staticmethod
    def _summary(path: Path, state: dict[str, Any]) -> dict[str, Any]:
        project = state.get("project") or {}
        stats = state.get("stats") or {}
        units = state.get("units") or []
        run = state.get("run") or {}
        try:
            updated_at = datetime.fromtimestamp(path.joinpath("project.json").stat().st_mtime, timezone.utc).replace(
                microsecond=0
            ).isoformat()
        except OSError:
            updated_at = ""
        return {
            "id": path.name,
            "name": str(project.get("name") or path.name),
            "path": str(path),
            "updated_at": updated_at,
            "status": str(run.get("status") or "ready"),
            "unit_count": len(units),
            "source_name": (project.get("source_file") or {}).get("name"),
            "progress_percent": stats.get("progress_percent", 0),
        }


class ProjectSession:
    """Route existing pipeline operations to the selected project runtime."""

    def __init__(
        self,
        book_root: Path | str,
        *,
        settings_dir: Path | str,
        legacy_runtime: Path | str | None = None,
    ) -> None:
        self.catalog = ProjectCatalog(book_root)
        if legacy_runtime is not None:
            self.catalog.migrate_legacy_project(legacy_runtime)
        self.api_settings = ApiSettingsStore(settings_dir)
        self.lock = threading.RLock()
        self.current_project_id: str | None = None
        self._manager: PipelineManager | None = None

    def list_projects(self) -> list[dict[str, Any]]:
        with self.lock:
            projects = self.catalog.list_projects()
            for project in projects:
                project["is_current"] = project["id"] == self.current_project_id
            return projects

    def project_detail(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            return self.catalog.get_project(project_id)

    def create_named_project(self, name: str) -> dict[str, Any]:
        with self.lock:
            self._ensure_switch_allowed()
            project = self.catalog.create_project(name)
            self._select_locked(project["id"])
            return self.catalog.get_project(project["id"])

    def select_project(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            project = self.catalog.get_project(project_id)
            if self.current_project_id != project_id:
                self._ensure_switch_allowed()
                self._select_locked(project_id)
            return self.catalog.get_project(project_id)

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete a project only after all of its live work has stopped."""

        with self.lock:
            project = self.catalog.get_project(project_id)
            if project.get("status") in {"running", "stopping"}:
                raise ConflictError("流水线仍在运行，不能删除项目。")

            is_current = project_id == self.current_project_id
            manager = self._manager if is_current else None
            if manager is not None:
                # The persisted run flag is not sufficient after the stop
                # grace timeout: an old provider worker may still be alive in a
                # retired executor.  Inspect all scheduler-owned live sets
                # while holding the manager lock, then keep that lock through
                # delete/close so a concurrent start cannot slip in.
                with manager.lock:
                    run = manager.state.get("run") or {}
                    if (
                        run.get("running")
                        or run.get("status") == "stopping"
                        or getattr(manager, "_active_unit_ids", None)
                        or getattr(manager, "_active_task_meta", None)
                        or getattr(manager, "_retired_executors", None)
                    ):
                        raise ConflictError("流水线仍有任务未完全收尾，不能删除项目。")
                    # Delete first while both lifecycle locks are held.  If
                    # the filesystem operation fails, the manager remains open
                    # and this session can continue to serve the project.
                    deleted = self.catalog.delete_project(project_id)
                    # No worker can be active at this point, so closing the
                    # manager cannot block on a provider request.  The close
                    # marks the old object unusable before the session drops
                    # its reference to it.
                    manager.close()
            else:
                deleted = self.catalog.delete_project(project_id)

            if is_current:
                self._manager = None
                self.current_project_id = None
            return {"status": "ok", "project_id": str(project_id), "deleted": bool(deleted.get("deleted"))}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            manager = self._require_manager()
            state = manager.snapshot()
            state["current_project"] = self.catalog.get_project(self.current_project_id or "")
            return state

    def _select_locked(self, project_id: str) -> None:
        project_path = self.catalog.project_path(project_id)
        if self._manager is not None:
            self._manager.close()
        self._manager = PipelineManager(project_path, api_settings=self.api_settings)
        self.current_project_id = project_id

    def _ensure_switch_allowed(self) -> None:
        if self._manager is not None and self._manager.state.get("run", {}).get("running"):
            raise ConflictError("当前项目流水线仍在运行，完成后才能切换项目。")

    def _require_manager(self) -> PipelineManager:
        if self._manager is None or self.current_project_id is None:
            raise ProjectSelectionRequired("请先从项目选择页面进入一个项目。")
        return self._manager

    def _call_current_manager(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Resolve and use the selected manager under one lifecycle lock.

        The lock intentionally covers both the lookup and the call.  A caller
        must never retain a manager reference while project deletion or project
        switching can replace it.
        """

        with self.lock:
            manager = self._require_manager()
            return getattr(manager, method_name)(*args, **kwargs)

    def _call_quality_manager_without_session_lock(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a quality-provider request without pinning the session lock.

        Quality scanning and editorial suggestions both wait on an external
        provider.  Keep the selected manager/project identity as a lease,
        release the session lock for that wait, then reject a result if a
        concurrent project switch or deletion replaced the lease.
        """

        with self.lock:
            manager = self._require_manager()
            project_id = self.current_project_id
        result = getattr(manager, method_name)(*args, **kwargs)
        with self.lock:
            if self._manager is not manager or self.current_project_id != project_id:
                raise ConflictError("项目已切换，当前请求结果已失效，请刷新后重试。")
        return result

    def get_unit(self, unit_id: str) -> dict[str, Any]:
        return self._call_current_manager("get_unit", unit_id)

    def create_project(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("create_project", *args, **kwargs)

    def import_source_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("import_source_file", *args, **kwargs)

    def start(self, unit_ids: list[str] | None = None) -> dict[str, Any]:
        return self._call_current_manager("start", unit_ids)

    def stop(self) -> dict[str, Any]:
        return self._call_current_manager("stop")

    def concurrency_settings(self) -> dict[str, Any]:
        return self._call_current_manager("concurrency_settings")

    def update_concurrency_settings(self, max_concurrency: Any) -> dict[str, Any]:
        return self._call_current_manager("update_concurrency_settings", max_concurrency)

    def decide(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("decide", *args, **kwargs)

    def retry_quality_batch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Forward one confirmed failed-batch recovery to the current project.

        A recovery waits on an external provider, so it takes the same lease as
        scanning and preparation: the session lock is released for the model
        call, and a result that arrives after a project switch or deletion is
        rejected instead of being written into the wrong project.
        """

        return self._call_quality_manager_without_session_lock(
            "retry_quality_batch", *args, **kwargs
        )

    def save_translation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("save_translation", *args, **kwargs)

    def review_unit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("review_unit", *args, **kwargs)

    def retranslate_unit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("retranslate_unit", *args, **kwargs)

    def quality_support(self, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("quality_support", **kwargs)

    def plan_quality_scan(self, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("plan_quality_scan", **kwargs)

    def scan_quality_batch(self, **kwargs: Any) -> dict[str, Any]:
        return self._call_quality_manager_without_session_lock("scan_quality_batch", **kwargs)

    def update_quality_card(self, card_id: str, action: str, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("update_quality_card", card_id, action, **kwargs)

    def batch_quality_card_action(self, action: str, items: Any, **kwargs: Any) -> dict[str, Any]:
        # A single atomic write per request: no external provider is awaited, so
        # the session lock may simply cover the call like the other card writes.
        return self._call_current_manager("batch_quality_card_action", action, items, **kwargs)

    def quality_affected_units(self, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("quality_affected_units", **kwargs)

    def set_reference_mode(self, mode: str, **kwargs: Any) -> dict[str, Any]:
        # A pure local mode switch: no provider is awaited, so the session lock
        # may cover the call like the other card writes.
        return self._call_current_manager("set_reference_mode", mode, **kwargs)

    def quality_prepare(self, **kwargs: Any) -> dict[str, Any]:
        # Model calls happen inside the manager, outside its own lock; the
        # session lock must not be held across a network request.
        return self._call_quality_manager_without_session_lock("quality_prepare", **kwargs)

    def quality_prepare_status(self, **kwargs: Any) -> dict[str, Any]:
        return self._call_current_manager("quality_prepare_status", **kwargs)

    def editorial_suggestions(self, unit_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._call_quality_manager_without_session_lock(
            "editorial_suggestions", unit_id, **kwargs
        )

    def segmentation_settings(self) -> dict[str, Any]:
        with self.lock:
            settings = self._require_manager().segmentation_settings()
            settings["project_name"] = self.catalog.get_project(self.current_project_id or "")["name"]
            return settings

    def update_segmentation_settings(self, max_words: Any) -> dict[str, Any]:
        with self.lock:
            settings = self._require_manager().update_segmentation_settings(max_words)
            settings["project_name"] = self.catalog.get_project(self.current_project_id or "")["name"]
            return settings

    def translation_context_settings(self) -> dict[str, Any]:
        with self.lock:
            settings = self._require_manager().translation_context_settings()
            settings["project_name"] = self.catalog.get_project(self.current_project_id or "")["name"]
            return settings

    def update_translation_context_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = self._require_manager().update_translation_context_settings(payload)
            settings["project_name"] = self.catalog.get_project(self.current_project_id or "")["name"]
            return settings

    def resegment_source(self, *, confirm_reset: bool = False) -> dict[str, Any]:
        with self.lock:
            return self._require_manager().resegment_source(confirm_reset=confirm_reset)

    def output_status(self) -> dict[str, Any]:
        with self.lock:
            return self._require_manager().output_status()

    def generate_output(self, output_format: str | None = None) -> dict[str, Any]:
        with self.lock:
            return self._require_manager().generate_output(output_format)

    def output_file_path(self, output_format: str | None = None) -> Path:
        with self.lock:
            return self._require_manager().output_file_path(output_format)
