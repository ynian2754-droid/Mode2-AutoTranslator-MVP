"""Persistent API presets and the choice of preset for each AI task.

Users keep reusable presets.  Four groups each select one preset; six tasks
follow their group unless they select a preset of their own.  The effective
rule has exactly two layers: a task's own choice wins, otherwise its group's.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from core.exceptions import ConflictError
from providers.prompts import REVIEW_SYSTEM_PROMPT, TRANSLATION_SYSTEM_PROMPT


ApiGroup = Literal["translation", "review", "concept_create", "concept_verify"]
ApiTask = Literal[
    "unit_translation",
    "expression",
    "unit_review",
    "concept_generation",
    "concept_check",
    "concept_disambiguation",
]
PromptTask = Literal["unit_translation", "unit_review"]
API_GROUPS: tuple[str, ...] = ("translation", "review", "concept_create", "concept_verify")
# Task -> owning group.  Each task is one provider channel in the pipeline.
API_TASKS: dict[str, str] = {
    "unit_translation": "translation",
    "expression": "translation",
    "unit_review": "review",
    "concept_generation": "concept_create",
    "concept_check": "concept_verify",
    "concept_disambiguation": "concept_verify",
}
GROUP_NAMES = {
    "translation": "单元翻译",
    "review": "单元校验",
    "concept_create": "概念解析创造",
    "concept_verify": "概念解析检验",
}
TASK_NAMES = {
    "unit_translation": "单元翻译",
    "expression": "表达建议",
    "unit_review": "单元校验",
    "concept_generation": "概念生成",
    "concept_check": "概念检查",
    "concept_disambiguation": "概念辨析",
}
SETTINGS_VERSION = 2
MAX_PRESETS = 50
MAX_NAME_LENGTH = 40
LEGACY_BACKUP_NAME = "api_settings.v1-backup.json"

# System-prompt presets live in the same file but are a separate library from
# API presets.  Only these two tasks expose their system prompt; concept work
# and expression suggestions keep the built-in text.
PROMPT_TASKS: tuple[str, ...] = ("unit_translation", "unit_review")
DEFAULT_PROMPT_ID = "default"
BUILTIN_PROMPTS: dict[str, str] = {
    "unit_translation": TRANSLATION_SYSTEM_PROMPT,
    "unit_review": REVIEW_SYSTEM_PROMPT,
}
MAX_PROMPT_PRESETS = 20
MAX_PROMPT_LENGTH = 200_000


@dataclass(frozen=True)
class ApiConfig:
    """One complete OpenAI-compatible configuration."""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    reasoning_effort: str = ""
    temperature: float = 0.7
    max_output_tokens: int = 2000
    timeout_seconds: float = 90.0
    oc_go_compatibility: bool = False
    opencode_session_id: str = ""

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None = None) -> "ApiConfig":
        values = values or {}
        base_value = values.get("base_url", cls.base_url)
        base_url = str("" if base_value is None else base_value).strip().rstrip("/")
        api_key = str(values.get("api_key") or "").strip()
        model_value = values.get("model", cls.model)
        model = str("" if model_value is None else model_value).strip()
        reasoning_effort = str(values.get("reasoning_effort") or "").strip()
        oc_go_value = values.get("oc_go_compatibility", False)
        if isinstance(oc_go_value, bool):
            oc_go_compatibility = oc_go_value
        else:
            oc_go_compatibility = str(oc_go_value or "").strip().casefold() in {
                "1",
                "true",
                "yes",
                "on",
            }
        opencode_session_id = str(values.get("opencode_session_id") or "").strip()
        if len(opencode_session_id) > 128 or any(
            ord(character) < 32 or ord(character) == 127 for character in opencode_session_id
        ):
            raise ValueError("OC Go 会话 ID 必须是 128 个字符以内且不含控制字符。")
        try:
            temperature = float(values.get("temperature", cls.temperature))
            max_output_tokens = int(values.get("max_output_tokens", cls.max_output_tokens))
            timeout_seconds = float(values.get("timeout_seconds", cls.timeout_seconds))
        except (TypeError, ValueError) as exc:
            raise ValueError("Temperature、最大输出 Token 和请求超时必须是数字。") from exc
        if not base_url:
            raise ValueError("API Base URL 不能为空。")
        if not model:
            raise ValueError("Model 不能为空。")
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("Temperature 必须在 0 到 2 之间。")
        if not 1 <= max_output_tokens <= 1_000_000:
            raise ValueError("最大输出 Token 必须在 1 到 1000000 之间。")
        if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 600:
            raise ValueError("请求超时必须在 1 到 600 秒之间。")
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            oc_go_compatibility=oc_go_compatibility,
            opencode_session_id=opencode_session_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "oc_go_compatibility": self.oc_go_compatibility,
            "opencode_session_id": self.opencode_session_id,
        }


@dataclass(frozen=True)
class ApiPreset:
    id: str
    name: str
    config: ApiConfig

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, **self.config.to_dict()}


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("预设名称不能为空。")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"预设名称不能超过 {MAX_NAME_LENGTH} 个字符。")
    return name


def _check_group(group: str) -> None:
    if group not in API_GROUPS:
        raise ValueError(f"不支持的任务分组：{group}")


def _check_task(task: str) -> None:
    if task not in API_TASKS:
        raise ValueError(f"不支持的任务：{task}")


def _check_prompt_task(task: str) -> None:
    if task not in PROMPT_TASKS:
        raise ValueError(f"该任务不开放提示词设置：{task}")


def _clean_prompt_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("提示词内容不能为空。")
    if len(text) > MAX_PROMPT_LENGTH:
        raise ValueError(f"提示词不能超过 {MAX_PROMPT_LENGTH} 个字符。")
    return text


class ApiSettingsStore:
    """Presets plus group/task assignments, stored in one local JSON file."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.settings_path = self.runtime_dir / "api_settings.json"
        self.lock = threading.RLock()
        self._legacy_loaded = False
        self._presets: list[ApiPreset] = []
        self._groups: dict[str, str] = {}
        self._overrides: dict[str, str] = {}
        self._prompts: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------- loading ----------
    @staticmethod
    def _legacy_defaults() -> ApiConfig:
        """Keep the old environment-based setup usable on first launch."""
        return ApiConfig.from_mapping(
            {
                "base_url": os.getenv("AUTOTRANSLATOR_BASE_URL") or "https://api.openai.com/v1",
                "api_key": os.getenv("AUTOTRANSLATOR_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
                "model": os.getenv("AUTOTRANSLATOR_MODEL") or "gpt-4o-mini",
                "timeout_seconds": os.getenv("AUTOTRANSLATOR_TIMEOUT", "90"),
            }
        )

    def _use_single(self, preset: ApiPreset) -> None:
        self._presets = [preset]
        self._groups = {group: preset.id for group in API_GROUPS}
        self._overrides = {}

    def _load(self) -> None:
        default = ApiPreset("default", "默认 API", self._legacy_defaults())
        raw: Any = None
        if self.settings_path.is_file():
            try:
                raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
        self._prompts = self._load_prompts(raw if isinstance(raw, dict) else {})
        if not isinstance(raw, dict):
            self._use_single(default)
            return
        if isinstance(raw.get("presets"), list):
            self._load_presets(raw, default)
        else:
            self._load_legacy(raw, default)

    @staticmethod
    def _load_prompts(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Read the prompts section; anything missing or invalid means the
        built-in default stays selected, so existing installs need no
        migration and untouched files never record prompt text."""
        state = {task: {"selected": DEFAULT_PROMPT_ID, "custom": []} for task in PROMPT_TASKS}
        section = raw.get("prompts") if isinstance(raw.get("prompts"), dict) else {}
        for task in PROMPT_TASKS:
            entry = section.get(task) if isinstance(section.get(task), dict) else {}
            custom: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            seen_names: set[str] = set()
            items = entry.get("custom") if isinstance(entry.get("custom"), list) else []
            for item in items[:MAX_PROMPT_PRESETS]:
                if not isinstance(item, dict):
                    continue
                try:
                    preset_id = str(item.get("id") or "").strip()
                    name = _clean_name(item.get("name"))
                    text = _clean_prompt_text(item.get("text"))
                except ValueError:
                    continue
                if (
                    not preset_id
                    or preset_id == DEFAULT_PROMPT_ID
                    or preset_id in seen_ids
                    or name.casefold() in seen_names
                ):
                    continue
                seen_ids.add(preset_id)
                seen_names.add(name.casefold())
                custom.append({"id": preset_id, "name": name, "text": text})
            selected = str(entry.get("selected") or "")
            if selected != DEFAULT_PROMPT_ID and selected not in seen_ids:
                selected = DEFAULT_PROMPT_ID
            state[task] = {"selected": selected, "custom": custom}
        return state

    def _load_presets(self, raw: dict[str, Any], default: ApiPreset) -> None:
        presets: list[ApiPreset] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for item in raw["presets"][:MAX_PRESETS]:
            if not isinstance(item, dict):
                continue
            try:
                preset_id = str(item.get("id") or "").strip()
                name = _clean_name(item.get("name"))
                config = ApiConfig.from_mapping(item)
            except ValueError:
                continue
            if not preset_id or preset_id in seen_ids or name.casefold() in seen_names:
                continue
            seen_ids.add(preset_id)
            seen_names.add(name.casefold())
            presets.append(ApiPreset(preset_id, name, config))
        if not presets:
            self._use_single(default)
            return
        self._presets = presets
        groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        fallback = presets[0].id
        self._groups = {
            group: groups.get(group) if groups.get(group) in seen_ids else fallback
            for group in API_GROUPS
        }
        self._overrides = {
            task: overrides[task]
            for task in API_TASKS
            if overrides.get(task) in seen_ids
        }

    def _load_legacy(self, raw: dict[str, Any], default: ApiPreset) -> None:
        """Turn the old translation/inspection pair into two presets.

        The assignment reproduces the old routing exactly: translation also
        served concept generation and expression suggestions; inspection also
        served concept checks and disambiguation.
        """

        configs: dict[str, ApiConfig] = {}
        for scope in ("translation", "inspection"):
            try:
                configs[scope] = ApiConfig.from_mapping(raw.get(scope))
            except ValueError:
                configs[scope] = default.config
        self._legacy_loaded = True
        if configs["translation"] == configs["inspection"]:
            self._use_single(ApiPreset("default", "默认 API", configs["translation"]))
            return
        self._presets = [
            ApiPreset("translation", "翻译 API", configs["translation"]),
            ApiPreset("inspection", "检验 API", configs["inspection"]),
        ]
        self._groups = {
            "translation": "translation",
            "review": "inspection",
            "concept_create": "translation",
            "concept_verify": "inspection",
        }
        self._overrides = {}

    # ---------- reading ----------
    def _preset_locked(self, preset_id: str) -> ApiPreset:
        for preset in self._presets:
            if preset.id == preset_id:
                return preset
        raise ValueError("找不到该 API 预设，请刷新设置页后重试。")

    def _oc_go_locked(self) -> dict[str, bool]:
        flags = [preset.config.oc_go_compatibility for preset in self._presets]
        enabled = bool(flags) and all(flags)
        return {"enabled": enabled, "partial": any(flags) and not enabled}

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "presets": [preset.to_dict() for preset in self._presets],
            "groups": dict(self._groups),
            "overrides": dict(self._overrides),
            "oc_go": self._oc_go_locked(),
            "prompts": {
                task: {
                    "selected": state["selected"],
                    "builtin": {"id": DEFAULT_PROMPT_ID, "text": BUILTIN_PROMPTS[task]},
                    "custom": copy.deepcopy(state["custom"]),
                }
                for task, state in self._prompts.items()
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def config_for_task(self, task: str) -> ApiConfig:
        """The configuration a task really uses: own choice, else its group's."""
        _check_task(task)
        with self.lock:
            preset_id = self._overrides.get(task) or self._groups[API_TASKS[task]]
            return self._preset_locked(preset_id).config

    def request_config(self, values: dict[str, Any], preset_id: str | None = None) -> ApiConfig:
        """Build a one-off config (connection test, model list) from editor
        values, carrying the saved OC Go contract so tests match real requests."""
        config = ApiConfig.from_mapping(
            {key: value for key, value in values.items() if key not in {"oc_go_compatibility", "opencode_session_id"}}
        )
        with self.lock:
            existing = next((p for p in self._presets if p.id == preset_id), None)
            if existing is not None:
                return replace(
                    config,
                    oc_go_compatibility=existing.config.oc_go_compatibility,
                    opencode_session_id=existing.config.opencode_session_id,
                )
            if self._oc_go_locked()["enabled"]:
                return replace(config, oc_go_compatibility=True, opencode_session_id=uuid.uuid4().hex)
        return config

    # ---------- prompt presets (separate from API presets) ----------
    def _prompt_preset_locked(self, task: str, preset_id: str) -> dict[str, str]:
        for preset in self._prompts[task]["custom"]:
            if preset["id"] == preset_id:
                return preset
        raise ValueError("找不到该提示词预设，请刷新设置页后重试。")

    def prompt_for_task(self, task: str) -> str:
        """The system prompt a new request starts with: the selected custom
        text, or the built-in default.  Callers bind the returned string to
        one request, so in-flight work is unaffected by later changes."""
        _check_prompt_task(task)
        with self.lock:
            state = self._prompts[task]
            if state["selected"] != DEFAULT_PROMPT_ID:
                for preset in state["custom"]:
                    if preset["id"] == state["selected"]:
                        return preset["text"]
            return BUILTIN_PROMPTS[task]

    def _unique_prompt_name_locked(self, task: str, name: str, own_id: str | None = None) -> str:
        clean = _clean_name(name)
        if any(p["name"].casefold() == clean.casefold() and p["id"] != own_id for p in self._prompts[task]["custom"]):
            raise ValueError(f"已有名为「{clean}」的提示词预设，请换一个名称。")
        return clean

    def create_prompt_preset(self, task: str, name: str, text: str) -> dict[str, Any]:
        def change() -> str:
            _check_prompt_task(task)
            custom = self._prompts[task]["custom"]
            if len(custom) >= MAX_PROMPT_PRESETS:
                raise ValueError(f"每项任务最多只能保存 {MAX_PROMPT_PRESETS} 个提示词预设。")
            preset = {
                "id": f"pp-{uuid.uuid4().hex[:12]}",
                "name": self._unique_prompt_name_locked(task, name),
                "text": _clean_prompt_text(text),
            }
            custom.append(preset)
            return preset["id"]

        return self._mutate(change, result_key="prompt_id")

    def update_prompt_preset(self, task: str, preset_id: str, name: str, text: str) -> dict[str, Any]:
        def change() -> str:
            _check_prompt_task(task)
            if preset_id == DEFAULT_PROMPT_ID:
                raise ValueError("内置默认提示词为只读，可复制为自定义预设后修改。")
            preset = self._prompt_preset_locked(task, preset_id)
            preset["name"] = self._unique_prompt_name_locked(task, name, own_id=preset_id)
            preset["text"] = _clean_prompt_text(text)
            return preset_id

        return self._mutate(change, result_key="prompt_id")

    def delete_prompt_preset(self, task: str, preset_id: str) -> dict[str, Any]:
        def change() -> None:
            _check_prompt_task(task)
            if preset_id == DEFAULT_PROMPT_ID:
                raise ValueError("内置默认提示词为只读，不能删除。")
            preset = self._prompt_preset_locked(task, preset_id)
            if self._prompts[task]["selected"] == preset_id:
                raise ConflictError(f"提示词预设「{preset['name']}」正在使用，请先改用其他预设再删除。")
            self._prompts[task]["custom"].remove(preset)

        return self._mutate(change)

    def select_prompt(self, task: str, preset_id: str) -> dict[str, Any]:
        def change() -> None:
            _check_prompt_task(task)
            if preset_id != DEFAULT_PROMPT_ID:
                self._prompt_preset_locked(task, preset_id)
            self._prompts[task]["selected"] = preset_id

        return self._mutate(change)

    # ---------- mutations (each one is saved immediately and atomically) ----------
    def _mutate(self, change, result_key: str = "preset_id") -> dict[str, Any]:
        with self.lock:
            previous = (
                list(self._presets),
                dict(self._groups),
                dict(self._overrides),
                copy.deepcopy(self._prompts),
            )
            try:
                result = change()
                self._save_locked()
            except Exception:
                self._presets, self._groups, self._overrides, self._prompts = previous
                raise
            snapshot = self._snapshot_locked()
            if result is not None:
                snapshot[result_key] = result
            return snapshot

    def _unique_name_locked(self, name: str, own_id: str | None = None) -> str:
        clean = _clean_name(name)
        if any(p.name.casefold() == clean.casefold() and p.id != own_id for p in self._presets):
            raise ValueError(f"已有名为「{clean}」的预设，请换一个名称。")
        return clean

    def create_preset(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        def change() -> str:
            if len(self._presets) >= MAX_PRESETS:
                raise ValueError(f"最多只能保存 {MAX_PRESETS} 个预设。")
            clean = self._unique_name_locked(name)
            config = ApiConfig.from_mapping(values)
            oc_go = self._oc_go_locked()["enabled"]
            config = replace(
                config,
                oc_go_compatibility=oc_go,
                opencode_session_id=uuid.uuid4().hex if oc_go else "",
            )
            preset = ApiPreset(f"p-{uuid.uuid4().hex[:12]}", clean, config)
            self._presets.append(preset)
            return preset.id

        return self._mutate(change)

    def update_preset(self, preset_id: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
        def change() -> str:
            existing = self._preset_locked(preset_id)
            clean = self._unique_name_locked(name, own_id=preset_id)
            config = replace(
                ApiConfig.from_mapping(values),
                oc_go_compatibility=existing.config.oc_go_compatibility,
                opencode_session_id=existing.config.opencode_session_id,
            )
            index = self._presets.index(existing)
            self._presets[index] = ApiPreset(preset_id, clean, config)
            return preset_id

        return self._mutate(change)

    def preset_usage(self, preset_id: str) -> list[str]:
        with self.lock:
            places = [f"分组「{GROUP_NAMES[g]}」" for g in API_GROUPS if self._groups[g] == preset_id]
            places += [f"任务「{TASK_NAMES[t]}」" for t in API_TASKS if self._overrides.get(t) == preset_id]
            return places

    def delete_preset(self, preset_id: str) -> dict[str, Any]:
        def change() -> None:
            preset = self._preset_locked(preset_id)
            places = self.preset_usage(preset_id)
            if places:
                raise ConflictError(f"预设「{preset.name}」正在被{'、'.join(places)}使用，请先改用其他预设再删除。")
            self._presets.remove(preset)

        return self._mutate(change)

    def set_group(self, group: str, preset_id: str) -> dict[str, Any]:
        def change() -> None:
            _check_group(group)
            self._preset_locked(preset_id)
            self._groups[group] = preset_id

        return self._mutate(change)

    def set_task(self, task: str, preset_id: str | None) -> dict[str, Any]:
        def change() -> None:
            _check_task(task)
            if preset_id:
                self._preset_locked(preset_id)
                self._overrides[task] = preset_id
            else:
                self._overrides.pop(task, None)

        return self._mutate(change)

    def clear_overrides(self) -> dict[str, Any]:
        def change() -> None:
            self._overrides = {}

        return self._mutate(change)

    def apply_to_all(self, preset_id: str) -> dict[str, Any]:
        """Shortcut only: set every group and clear task choices. No extra layer."""

        def change() -> None:
            self._preset_locked(preset_id)
            self._groups = {group: preset_id for group in API_GROUPS}
            self._overrides = {}

        return self._mutate(change)

    def set_oc_go_compatibility(self, enabled: bool) -> dict[str, Any]:
        """Toggle the OC Go request contract for every preset atomically."""

        def change() -> None:
            self._presets = [
                replace(
                    preset,
                    config=replace(
                        preset.config,
                        oc_go_compatibility=enabled,
                        opencode_session_id=preset.config.opencode_session_id
                        or (uuid.uuid4().hex if enabled else ""),
                    ),
                )
                for preset in self._presets
            ]

        return self._mutate(change)

    def _save_locked(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self._legacy_loaded:
            backup = self.runtime_dir / LEGACY_BACKUP_NAME
            if self.settings_path.is_file() and not backup.exists():
                shutil.copy2(self.settings_path, backup)
            self._legacy_loaded = False
        payload = copy.deepcopy(self._snapshot_locked())
        payload.pop("oc_go", None)
        # The file records choices and custom text only; the built-in prompts
        # stay read-only constants in providers.prompts and are never stored.
        for task_state in payload["prompts"].values():
            task_state.pop("builtin", None)
        temporary = self.settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.settings_path)
