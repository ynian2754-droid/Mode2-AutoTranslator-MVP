"""Small stateless OpenAI-compatible chat-completions client."""

from __future__ import annotations

import json
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.api_settings import ApiConfig


class ProviderRequestError(RuntimeError):
    """A model call failed before a result could be imported."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_format_rejected: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_format_rejected = response_format_rejected


class TruncatedAnswerError(ProviderRequestError):
    """The endpoint stopped the answer at the output cap (``finish_reason=length``).

    This is not a content mistake the model can repair: the same request with
    the same output cap would be cut off again, so the bounded content-repair
    loop must not spend its rounds on it. Raising this instead of a repair
    error is what turns "连续 N 轮未通过本地内容协议校验" into a reason an
    operator can act on.
    """


_REASONING_CONTENT_TYPES = {"analysis", "reasoning", "thinking", "thought"}
_DEFAULT_USER_AGENT = "Mozilla/5.0"
_OC_GO_USER_AGENT = "mode2-auto-translator/1.0"
_RESPONSE_FORMAT_STATUS_CODES = {400, 422}
_RESPONSE_FORMAT_HINTS = (
    "unsupported",
    "not support",
    "invalid",
    "unknown",
    "unrecognized",
    "不支持",
    "无效",
    "未知",
)


def response_format_rejected(status_code: int | None, body: str) -> bool:
    """Return True only for explicit evidence that the parameter was rejected.

    A generic 400/500 (or any 5xx) must not be read as "this endpoint does not
    support response_format": the caller would then silently degrade a working
    request or, worse, treat a service failure as a repairable model error.
    """

    if status_code not in _RESPONSE_FORMAT_STATUS_CODES:
        return False
    text = str(body or "").casefold()
    if "response_format" not in text:
        return False
    return any(hint in text for hint in _RESPONSE_FORMAT_HINTS)


class OpenAICompatibleClient:
    """Send one isolated request using one immutable API configuration."""

    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        # Per-thread, because one client instance is shared by the providers and
        # a conversation's answer is read on the thread that asked for it.
        self._local = threading.local()

    @property
    def last_finish_reason(self) -> str:
        """How this thread's most recent answer ended (``""`` when unknown).

        ``length`` means the endpoint stopped at ``max_output_tokens``: the
        answer is incomplete by construction. Callers that need a complete
        protocol answer read this instead of guessing from a local parse
        failure. Test doubles that replace :meth:`chat` simply leave it empty.
        """

        return str(getattr(self._local, "finish_reason", "") or "")

    @property
    def model(self) -> str:
        return self.config.model

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        """Build headers without inventing an empty Bearer credential.

        Some OpenAI-compatible gateways intentionally expose public endpoints.
        An empty API key therefore means "no Authorization header", not
        ``Authorization: Bearer ``.  Authenticated providers retain exactly the
        same header behavior as before.
        """

        headers = {
            "User-Agent": (
                _OC_GO_USER_AGENT if self.config.oc_go_compatibility else _DEFAULT_USER_AGENT
            )
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.oc_go_compatibility and self.config.opencode_session_id:
            headers["X-Opencode-Session"] = self.config.opencode_session_id
        return headers

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, int]]:
        """One chat completion; :attr:`last_finish_reason` tells how it ended."""

        content, usage, finish_reason = self._chat_answer(
            messages, response_format=response_format
        )
        self._local.finish_reason = finish_reason
        return content, usage

    def _chat_answer(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, int], str]:

        endpoint = (
            self.config.base_url
            if self.config.base_url.endswith("/chat/completions")
            else f"{self.config.base_url}/chat/completions"
        )
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [dict(message) for message in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(json_body=True),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = self._read_error_body(exc)
            raise ProviderRequestError(
                self._http_error_message(exc, body),
                status_code=exc.code,
                response_format_rejected=(
                    response_format is not None
                    and response_format_rejected(exc.code, body)
                ),
            ) from exc
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            raise ProviderRequestError(f"Base URL 无法连接：{reason}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            if isinstance(exc, TimeoutError):
                raise ProviderRequestError("API 请求超时，请检查请求超时设置或网络连接。") from exc
            raise ProviderRequestError("API 返回的内容不是有效 JSON。") from exc
        choice: object = {}
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError("API 响应缺少 choices[0].message.content。") from exc
        finish_reason = ""
        if isinstance(choice, dict):
            finish_reason = str(choice.get("finish_reason") or "")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
                and str(item.get("type") or "").casefold() not in _REASONING_CONTENT_TYPES
            )
        usage = data.get("usage") or {}
        return (
            str(content),
            {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
            finish_reason,
        )

    def list_models(self) -> list[str]:
        """Fetch model IDs using only this client's API configuration."""
        base_url = self.config.base_url
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        endpoint = base_url if base_url.endswith("/models") else f"{base_url}/models"
        request = Request(
            endpoint,
            headers=self._headers(),
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = self._read_error_body(exc)
            raise ProviderRequestError(self._http_error_message(exc, body, resource="/models")) from exc
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            raise ProviderRequestError(f"Base URL 无法连接：{reason}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            if isinstance(exc, TimeoutError):
                raise ProviderRequestError("API 请求超时，请检查请求超时设置或网络连接。") from exc
            raise ProviderRequestError("API 返回的内容不是有效 JSON。") from exc

        candidates = data.get("data") if isinstance(data, dict) else data
        if isinstance(data, dict) and candidates is None:
            candidates = data.get("models")
        models: list[str] = []
        for item in candidates if isinstance(candidates, list) else []:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
            elif isinstance(item, str):
                model_id = item
            else:
                model_id = None
            model_id = str(model_id or "").strip()
            if model_id and model_id not in models:
                models.append(model_id)
        if not models:
            raise ProviderRequestError("没有获取到模型，请确认该 API 支持 /models。")
        return models

    def test_connection(self) -> dict[str, int]:
        """Use this configuration only, with a minimal chat-completions probe."""
        _content, usage = self.chat(
            [
                {"role": "system", "content": "Reply with OK."},
                {"role": "user", "content": "Connection test."},
            ]
        )
        return usage

    @staticmethod
    def _read_error_body(error: HTTPError) -> str:
        """Read an error response body exactly once (HTTPError is not re-readable)."""
        try:
            return error.read().decode("utf-8", errors="replace")
        except (OSError, UnicodeError):
            return ""

    @staticmethod
    def _http_error_message(
        error: HTTPError,
        raw_body: str = "",
        resource: str | None = None,
    ) -> str:
        body = ""
        try:
            data = json.loads(raw_body)
            detail = data.get("error", {}).get("message") if isinstance(data, dict) else None
            body = str(detail or raw_body).strip()
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
            body = ""
        if error.code in {401, 403}:
            return f"API Key 无效或无权限（HTTP {error.code}）" + (f"：{body}" if body else "。")
        if error.code == 404:
            if resource == "/models":
                return f"/models 接口不受支持或 Base URL 错误（HTTP 404）" + (f"：{body}" if body else "。")
            return f"Base URL 或 Model 不存在（HTTP 404）" + (f"：{body}" if body else "。")
        return f"API 返回错误（HTTP {error.code}）" + (f"：{body}" if body else "。")
