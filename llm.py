"""Pluggable LLM providers.

Providers share one interface: ``reply(system, history) -> str`` where
``history`` is a list of ``{"role": "user"|"assistant", "content": str}``.
Add a new provider by subclassing ``LLMProvider`` and registering it in
``build_provider``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


class LLMProvider:
    name = "base"

    async def reply(self, system: str, history: list[dict[str, str]]) -> str:
        raise NotImplementedError


class EchoProvider(LLMProvider):
    """Offline fallback. Proves the plumbing works with no API key."""

    name = "echo"

    async def reply(self, system: str, history: list[dict[str, str]]) -> str:
        last = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        return f"(echo) You said: {last}"


class OpenAIProvider(LLMProvider):
    """Any OpenAI-compatible chat completions endpoint (OpenAI, Azure, OpenRouter, vLLM)."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or OPENAI_DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    async def reply(self, system: str, history: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY is required for LLM_PROVIDER=openai")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *history],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        if response.status_code >= 400:
            raise LLMError(f"OpenAI error {response.status_code}: {response.text}")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"OpenAI returned no choices: {data}")
        return (choices[0].get("message") or {}).get("content", "").strip()


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-latest",
        base_url: str = "",
        timeout: float = 60.0,
        max_tokens: int = 1024,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or ANTHROPIC_DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    async def reply(self, system: str, history: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY is required for LLM_PROVIDER=anthropic")
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": history,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/messages",
                json=body,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
        if response.status_code >= 400:
            raise LLMError(f"Anthropic error {response.status_code}: {response.text}")
        blocks = response.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip()


def build_provider(
    provider: str,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> LLMProvider:
    """Instantiate a provider by name, falling back to echo when unknown."""
    key = (provider or "echo").strip().lower()
    if key == "openai":
        return OpenAIProvider(api_key=api_key, model=model or "gpt-4o-mini", base_url=base_url)
    if key == "anthropic":
        return AnthropicProvider(
            api_key=api_key, model=model or "claude-3-5-haiku-latest", base_url=base_url
        )
    if key != "echo":
        logger.warning("Unknown LLM_PROVIDER=%r, falling back to echo", provider)
    return EchoProvider()