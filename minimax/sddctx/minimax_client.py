"""Minimal MiniMax text client used to drive code generation.

Only the public request and response metadata is kept.  The API key is never
written to disk and the model's private reasoning trace (``reasoning_content``)
is deliberately discarded before a response is returned, so that the recorded
artefacts stay verifiable without storing a chain of thought.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "MiniMaxConfig",
    "MiniMaxError",
    "MiniMaxResponse",
    "MiniMaxClient",
    "resolve_api_key",
]

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3"
FALLBACK_MODELS = ("MiniMax-M3.1-Flash", "MiniMax-M3")


class MiniMaxError(RuntimeError):
    """Raised when the MiniMax API rejects a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.http_status = http_status

    def to_json(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "error_code": self.status_code,
            "http_status": self.http_status,
            "error_payload": self.payload,
        }


@dataclass(frozen=True)
class MiniMaxConfig:
    """Connection settings for the MiniMax text API."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = 180.0
    api_style: str = "openai"

    @classmethod
    def from_env(cls, *, model: str | None = None, base_url: str | None = None) -> "MiniMaxConfig":
        key = resolve_api_key()
        return cls(
            api_key=key,
            base_url=base_url or os.environ.get("MINIMAX_BASE_URL", DEFAULT_BASE_URL),
            model=model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL),
            timeout=float(os.environ.get("MINIMAX_TIMEOUT", "180")),
            api_style=os.environ.get("MINIMAX_API_STYLE", "openai"),
        )


@dataclass(frozen=True)
class MiniMaxResponse:
    """The verifiable part of a MiniMax answer."""

    model: str
    content: str
    finish_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    api_status: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "content": self.content,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "request_id": self.request_id,
            "api_status": self.api_status,
        }


def resolve_api_key() -> str:
    """Read the MiniMax API key from the environment or the Windows user registry.

    The value is never logged or persisted by this package.
    """
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, "MINIMAX_API_KEY")
        except OSError:
            value = ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise MiniMaxError(
        "MINIMAX_API_KEY is not configured; set it in the environment or as a Windows user variable"
    )


class MiniMaxClient:
    """Call the MiniMax text API with either the OpenAI or Anthropic style."""

    def __init__(self, config: MiniMaxConfig) -> None:
        self._config = config

    @property
    def config(self) -> MiniMaxConfig:
        return self._config

    async def complete(self, system: str, prompt: str) -> MiniMaxResponse:
        """Return the model's answer for ``prompt`` under the ``system`` instruction."""
        if self._config.api_style == "anthropic":
            return await self._complete_anthropic(system, prompt)
        return await self._complete_openai(system, prompt)

    async def _complete_openai(self, system: str, prompt: str) -> MiniMaxResponse:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        headers = {"Authorization": f"Bearer {self._config.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._config.timeout) as client:
            response = await client.post(url, json=body, headers=headers)
        payload = _safe_json(response)
        if response.status_code >= 400:
            raise _error_from_payload(response.status_code, payload)
        business_error = _business_error(payload, response.status_code)
        if business_error is not None:
            raise business_error
        choices = payload.get("choices") or []
        if not choices:
            raise MiniMaxError("MiniMax returned no choices", status_code=response.status_code, payload=payload)
        message = choices[0].get("message") or {}
        return MiniMaxResponse(
            model=str(payload.get("model") or self._config.model),
            content=str(message.get("content") or ""),
            finish_reason=choices[0].get("finish_reason"),
            usage=payload.get("usage") or {},
            request_id=response.headers.get("x-request-id"),
            api_status=response.status_code,
        )

    async def _complete_anthropic(self, system: str, prompt: str) -> MiniMaxResponse:
        base = self._config.base_url.rstrip("/")
        url = f"{base}/v1/messages" if base.endswith("/anthropic") else f"{base}/messages"
        body = {
            "model": self._config.model,
            "max_tokens": 8192,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._config.timeout) as client:
            response = await client.post(url, json=body, headers=headers)
        payload = _safe_json(response)
        if response.status_code >= 400:
            raise _error_from_payload(response.status_code, payload)
        business_error = _business_error(payload, response.status_code)
        if business_error is not None:
            raise business_error
        blocks = payload.get("content") or []
        content = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
        return MiniMaxResponse(
            model=str(payload.get("model") or self._config.model),
            content=content,
            finish_reason=payload.get("stop_reason"),
            usage=payload.get("usage") or {},
            request_id=response.headers.get("x-request-id"),
            api_status=response.status_code,
        )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text[:2000]}
    return payload if isinstance(payload, dict) else {"raw": payload}


def _error_from_payload(status: int, payload: dict[str, Any]) -> MiniMaxError:
    base = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
    code = base.get("status_code") or payload.get("code") or payload.get("error_code")
    message = base.get("status_msg")
    if not message:
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        elif isinstance(error, str):
            message = error
    if not message:
        message = payload.get("message") or f"HTTP {status}"
    if code in (None, "", 0):
        code = _code_from_message(str(message))
    return MiniMaxError(str(message), status_code=code, payload=payload, http_status=status)


def _code_from_message(message: str) -> int | None:
    """Read the business error code that MiniMax embeds in the message, e.g. ``(1008)``."""
    match = re.search(r"\((\d{3,6})\)", message)
    return int(match.group(1)) if match else None


def _business_error(payload: dict[str, Any], http_status: int) -> MiniMaxError | None:
    """Return an error when the API reported a failure inside a successful HTTP response."""
    base = payload.get("base_resp")
    if not isinstance(base, dict):
        return None
    code = base.get("status_code")
    if code in (None, 0, "0"):
        return None
    return MiniMaxError(
        str(base.get("status_msg") or "MiniMax reported a business error"),
        status_code=code,
        payload=payload,
        http_status=http_status,
    )


def dump_request_log(entry: dict[str, Any], path: str) -> None:
    """Write a request log that is guaranteed to carry no credential."""
    if "api_key" in entry:
        entry["api_key"] = "<redacted>"
    for key in ("authorization", "x-api-key"):
        if key in entry:
            entry[key] = "<redacted>"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(entry, handle, ensure_ascii=False, indent=2)
