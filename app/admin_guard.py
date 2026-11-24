"""Защита админки до разбора форм: вход, источник запроса и ограниченный размер тела."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

MAX_BODY = 16 * 1024 * 1024


def origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


class AdminGuard:
    def __init__(self, app, token, allowed_origins="", max_body=MAX_BODY):
        self.app, self.token, self.max_body = app, token, max_body
        self.allowed = {origin(v.strip()) for v in allowed_origins.split(",") if v.strip()}
        if None in self.allowed:
            raise ValueError("ADMIN_ALLOWED_ORIGINS: нужен полный http(s) адрес")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        webhook = request.url.path == "/max/webhook"
        if unsafe and not webhook:
            if request.url.path != "/login" and not secrets.compare_digest(
                request.cookies.get("admin_token", "").encode(), self.token.encode()
            ):
                return await JSONResponse({"detail": "Нужен вход в админку"}, 401)(scope, receive, send)
            allowed = self.allowed or {origin(str(request.base_url))}
            given = request.headers.get("origin")
            # Origin:null нельзя заменять Referer. Старые браузеры без Origin проверяем по Referer.
            supplied = origin(given) if given is not None else origin(request.headers.get("referer", ""))
            if supplied is None or supplied not in allowed:
                return await JSONResponse({"detail": "Недопустимый источник запроса"}, 403)(scope, receive, send)
        length = request.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                return await JSONResponse({"detail": "Неверный размер запроса"}, 400)(scope, receive, send)
            if size < 0:
                return await JSONResponse({"detail": "Неверный размер запроса"}, 400)(scope, receive, send)
            if size > self.max_body:
                return await JSONResponse({"detail": "Запрос больше 16 МиБ"}, 413)(scope, receive, send)
        # Ограничиваем и chunked-запросы, прежде чем FastAPI начнёт создавать временные файлы.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body:
                return await JSONResponse({"detail": "Запрос больше 16 МиБ"}, 413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
