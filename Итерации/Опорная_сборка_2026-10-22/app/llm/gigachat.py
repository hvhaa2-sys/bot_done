"""GigaChat через REST API (без SDK).

Документация: https://developers.sber.ru/docs/ru/gigachat/api/overview
"""
from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from .base import LLMProvider

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
# GigaChat-3-Ultra (freemium с июля 2026) — на новом адресе; с зарубежного IP он не отвечает
ULTRA_BASE_URL = "https://api.giga.chat/v1"
# Клиент в мессенджере ждёт: долго не висим, при сбое сети пробуем ещё раз
TIMEOUT = httpx.Timeout(20, connect=8)
# Перегрузка или сбой на стороне API — повторяем с паузой 1, 2, 4 с (или сколько скажет Retry-After)
_RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 2.0, 4.0)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", 0)), 10.0)
    except ValueError:
        return 0.0


_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError)


class GigaChatProvider(LLMProvider):
    name = "gigachat"

    def __init__(self, auth_key: str, scope: str, model: str, verify_ssl: bool,
                 transport: httpx.AsyncBaseTransport | None = None, base_url: str = ""):
        if not auth_key:
            raise ValueError("Не задан GIGACHAT_AUTH_KEY")
        self.auth_key, self.scope, self.model = auth_key, scope, model
        self.api_url = f"{base_url.rstrip('/')}/chat/completions" if base_url else API_URL
        self.client = httpx.AsyncClient(verify=verify_ssl, timeout=TIMEOUT, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = await self._post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {self.auth_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": self.scope},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = data.get("expires_at", 0) / 1000 or time.time() + 1500
        return self._token

    async def complete(self, system: str, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0.2,
            "max_tokens": 600,
        }
        resp = await self._chat(body)
        if resp.status_code == 401:  # токен отозвали раньше срока — берём новый
            self._token = None
            resp = await self._chat(body)
        # На бесплатном тарифе один запрос за раз: второй клиент получает 429 — ждём и повторяем
        for delay in RETRY_DELAYS:
            if resp.status_code not in _RETRY_STATUS:
                break
            await asyncio.sleep(_retry_after(resp) or delay)
            resp = await self._chat(body)
        resp.raise_for_status()
        data = resp.json()
        self.last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0)  # для учёта расхода
        return data["choices"][0]["message"]["content"]

    async def _chat(self, body: dict) -> httpx.Response:
        token = await self._get_token()
        return await self._post(self.api_url, json=body,
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        try:
            return await self.client.post(url, **kwargs)
        except _RETRYABLE:
            return await self.client.post(url, **kwargs)

    async def close(self) -> None:
        await self.client.aclose()
