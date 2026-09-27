"""Persistent, independent API settings for translation and inspection."""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal


ApiScope = Literal["inspection", "translation"]
API_SCOPES = ("inspection", "translation")


@dataclass(frozen=True)
class ApiConfig:
    """One complete OpenAI-compatible configuration owned by one workflow."""

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


class ApiSettingsStore:
    """Store two separately addressable API configurations in one local file."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.settings_path = self.runtime_dir / "api_settings.json"
        self.lock = threading.RLock()
        self._settings = self._load()

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

    def _load(self) -> dict[str, ApiConfig]:
        defaults = {scope: self._legacy_defaults() for scope in API_SCOPES}
        if not self.settings_path.is_file():
            return defaults
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        loaded = {}
        for scope in API_SCOPES:
            try:
                loaded[scope] = ApiConfig.from_mapping(raw.get(scope))
            except ValueError:
                loaded[scope] = defaults[scope]
        return loaded

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {scope: self._settings[scope].to_dict() for scope in API_SCOPES}

    def get(self, scope: ApiScope) -> ApiConfig:
        if scope not in API_SCOPES:
            raise ValueError(f"不支持的 API 配置范围：{scope}")
        with self.lock:
            return self._settings[scope]

    def update(self, scope: ApiScope, values: dict[str, Any]) -> ApiConfig:
        if scope not in API_SCOPES:
            raise ValueError(f"不支持的 API 配置范围：{scope}")
        config = ApiConfig.from_mapping(values)
        if config.oc_go_compatibility and not config.opencode_session_id:
            config = replace(config, opencode_session_id=uuid.uuid4().hex)
        with self.lock:
            self._settings[scope] = config
            self._save_locked()
            return config

    def set_oc_go_compatibility(self, enabled: bool) -> dict[str, dict[str, Any]]:
        """Toggle the OC Go request contract for both API scopes atomically."""
        with self.lock:
            previous = self._settings.copy()
            try:
                for scope in API_SCOPES:
                    config = self._settings[scope]
                    session_id = config.opencode_session_id
                    if enabled and not session_id:
                        session_id = uuid.uuid4().hex
                    self._settings[scope] = replace(
                        config,
                        oc_go_compatibility=enabled,
                        opencode_session_id=session_id,
                    )
                self._save_locked()
            except Exception:
                self._settings = previous
                raise
            return {scope: self._settings[scope].to_dict() for scope in API_SCOPES}

    def _save_locked(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".json.tmp")
        payload = {scope: self._settings[scope].to_dict() for scope in API_SCOPES}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.settings_path)
