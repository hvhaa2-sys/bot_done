"""MAX Bot API через REST (без SDK, на httpx — как GigaChat).

Документация: https://dev.max.ru/docs-api. Адреса и поля сверены 27.10.2026 с официальным клиентом
@maxhub/max-bot-api 1.0.2 и библиотекой maxapi 1.2.3: адрес platform-api2.max.ru (старый platform-api.max.ru
устарел), токен в заголовке Authorization без «Bearer», события — долгим опросом GET /updates с marker.
"""
from __future__ import annotations

import asyncio

import httpx

API_URL = "https://platform-api2.max.ru"
TEXT_LIMIT = 4000  # MAX принимает текст короче 4000 символов
POLL_TIMEOUT = 30  # столько секунд GET /updates ждёт новых событий, прежде чем вернуть пустой список
# Опрос висит до POLL_TIMEOUT секунд, остальные запросы — короткие
TIMEOUT = httpx.Timeout(POLL_TIMEOUT + 15, connect=8)
_RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 2.0, 4.0)
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError)

Buttons = list[list[tuple[str, str]]]  # ряды кнопок: (надпись, payload)
REQUEST_CONTACT = "\0contact"  # вместо payload: кнопка, по которой клиент отправляет свой контакт


class MaxError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        super().__init__(f"MAX API {status} {code}: {message}".strip(" :"))
        self.status, self.code = status, code


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", 0)), 10.0)
    except ValueError:
        return 0.0


def fit(text: str) -> str:
    return text if len(text) < TEXT_LIMIT else text[:TEXT_LIMIT - 2].rstrip() + "…"


def keyboard(buttons: Buttons) -> dict:
    """Кнопки под сообщением: при нажатии бот получает событие message_callback с этим payload."""
    def button(text: str, payload: str) -> dict:
        if payload == REQUEST_CONTACT:
            return {"type": "request_contact", "text": text}
        return {"type": "callback", "text": text, "payload": payload}

    return {"type": "inline_keyboard", "payload": {"buttons": [[button(*b) for b in row] for row in buttons]}}


class MaxApi:
    def __init__(self, token: str, base_url: str = "", transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise ValueError("Не задан MAX_BOT_TOKEN")
        self.client = httpx.AsyncClient(base_url=(base_url or API_URL).rstrip("/"), headers={"Authorization": token},
                                        timeout=TIMEOUT, transport=transport)

    async def me(self) -> dict:
        return await self.request("GET", "/me")

    async def updates(self, marker: int | None = None, timeout: int = POLL_TIMEOUT) -> dict:
        """{"updates": [...], "marker": N}: следующий запрос с этим marker вернёт только новые события."""
        return await self.request("GET", "/updates", params={"marker": marker, "timeout": timeout, "limit": 100})

    async def send(self, text: str, *, chat_id: int | None = None, user_id: int | None = None,
                   buttons: Buttons | None = None, forward: str | None = None, files: list[dict] | None = None) -> dict:
        """{"message": {...}} — отправленное сообщение. forward — mid сообщения, которое переслать
        (фото или голосовое клиента менеджеру); files — вложения по токенам из полученного сообщения,
        уходят от имени бота (файл менеджера клиенту). С ними text может быть пустым."""
        body: dict = {"text": fit(text)} if text or not (forward or files) else {}
        attachments = [*(files or []), *([keyboard(buttons)] if buttons else [])]
        if attachments:
            body["attachments"] = attachments
        if forward:
            body["link"] = {"type": "forward", "mid": forward}
        return await self.request("POST", "/messages", params={"chat_id": chat_id, "user_id": user_id}, json=body)

    async def answer(self, callback_id: str, *, notification: str | None = None, text: str | None = None,
                     buttons: Buttons | None = None) -> dict:
        """Ответ на нажатие кнопки: всплывающее уведомление и, если задан text, новый текст сообщения
        с кнопками. Пустой список вложений убирает кнопки."""
        body: dict = {}
        if notification:
            body["notification"] = notification
        if text is not None:
            body["message"] = {"text": fit(text), "attachments": [keyboard(buttons)] if buttons else []}
        return await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def subscriptions(self) -> list[dict]:
        return (await self.request("GET", "/subscriptions")).get("subscriptions") or []

    async def subscribe(self, url: str, secret: str, update_types: tuple[str, ...]) -> dict:
        """Вебхук: MAX шлёт события POST-запросом на url с секретом в заголовке X-Max-Bot-Api-Secret."""
        return await self.request("POST", "/subscriptions", json={"url": url, "secret": secret,
                                                                  "update_types": list(update_types)})

    async def unsubscribe(self, url: str) -> dict:
        return await self.request("DELETE", "/subscriptions", params={"url": url})

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._send(method, path, params, json)
        # Перегрузка или сбой на стороне MAX — повторяем с паузой 1, 2, 4 с (или сколько скажет Retry-After)
        for delay in RETRY_DELAYS:
            if resp.status_code not in _RETRY_STATUS:
                break
            await asyncio.sleep(_retry_after(resp) or delay)
            resp = await self._send(method, path, params, json)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            raise MaxError(resp.status_code, str(data.get("code", "")), str(data.get("message", "")))
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:  # шлюз или прокси вернул страницу вместо ответа API
            data = None
        if not isinstance(data, dict):
            raise MaxError(resp.status_code, "bad.response", "ответ не в формате API")
        return data

    async def _send(self, method: str, path: str, params: dict, json: dict | None) -> httpx.Response:
        try:
            return await self.client.request(method, path, params=params, json=json)
        except _RETRYABLE:  # связь оборвалась — ещё одна попытка
            return await self.client.request(method, path, params=params, json=json)

    async def close(self) -> None:
        await self.client.aclose()
