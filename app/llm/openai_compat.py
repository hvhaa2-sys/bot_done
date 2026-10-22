"""Любой API, совместимый с OpenAI Chat Completions: YandexGPT и открытые модели в Yandex AI Studio
(base_url https://ai.api.cloud.yandex.net/v1, project — ID каталога), Cloud.ru или локальная модель через Ollama."""
from __future__ import annotations

import asyncio

import httpx

from .base import LLMProvider
from .gigachat import RETRY_DELAYS, _RETRY_STATUS, _retry_after


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, project: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not base_url or not model:
            raise ValueError("Не заданы OPENAI_BASE_URL и OPENAI_MODEL")
        self.base_url, self.model = base_url.rstrip("/"), model
        headers = {"Authorization": f"Bearer {api_key}"}
        if project:
            headers["OpenAI-Project"] = project  # Yandex AI Studio: каталог, в котором считается расход
        self.client = httpx.AsyncClient(timeout=60, headers=headers, transport=transport)

    async def complete(self, system: str, messages: list[dict]) -> str:
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages],
                "temperature": 0.2, "max_tokens": 600}
        resp = await self.client.post(f"{self.base_url}/chat/completions", json=body)
        for delay in RETRY_DELAYS:  # перегрузка или лимит частоты — ждём и повторяем, как у GigaChat
            if resp.status_code not in _RETRY_STATUS:
                break
            await asyncio.sleep(_retry_after(resp) or delay)
            resp = await self.client.post(f"{self.base_url}/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        self.last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        await self.client.aclose()
