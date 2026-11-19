"""Проверки Origin, ранней авторизации и ограничения потокового тела без рабочего бота."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from app.admin import create_app
from app.admin_guard import AdminGuard
from tests.test_admin import make_client


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"}, {"Origin": "https://attacker.sslip.io"},
    {"Origin": "http://testserver.evil"},
    {"Origin": "https://attacker.sslip.io", "Referer": "http://testserver/settings"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_forged_or_missing_origin_cannot_change_settings(tmp_path, headers):
    _, services = make_client(tmp_path)
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", services.settings.admin_token)
    previous = services.greeting_message
    response = client.post("/settings", data={"greeting_message": "forged"}, headers=headers)
    assert response.status_code == 403 and services.greeting_message == previous


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {"Referer": "http://testserver/settings"}])
def test_own_form_still_saves_settings(tmp_path, headers):
    _, services = make_client(tmp_path)
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", services.settings.admin_token)
    assert client.post("/settings", data={"greeting_message": "safe"}, headers=headers).status_code == 200
    assert services.greeting_message == "safe"


def test_public_origin_allowlist_does_not_trust_arbitrary_host(tmp_path):
    _, services = make_client(tmp_path)
    services.settings = replace(services.settings, admin_allowed_origins="https://admin.example")
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", services.settings.admin_token)
    assert client.post("/settings", headers={"Host": "evil.example", "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/settings", headers={"Origin": "https://admin.example"}, data={"greeting_message": "safe"}).status_code == 200


def test_unauthenticated_file_is_rejected_before_multipart_parser(tmp_path, monkeypatch):
    _, services = make_client(tmp_path)
    client = TestClient(create_app(services))
    def fail(*args):
        pytest.fail("Неавторизованный запрос дошёл до парсера файлов")
    monkeypatch.setattr(MultiPartParser, "on_part_data", fail)
    assert client.post("/kb/notes/upload", files={"files": ("x.md", b"test")}).status_code == 401
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("length", [None, "999", "-1", "bad"])
def test_limit_applies_before_app_including_chunked(length):
    reached, statuses = [], []
    async def app(scope, receive, send):
        reached.append(True)
    guard = AdminGuard(app, token="secret", max_body=8)
    messages = iter([
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ])
    async def receive():
        return next(messages)
    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
    headers = [(b"host", b"testserver"), (b"origin", b"http://testserver")]
    if length is not None:
        headers.append((b"content-length", length.encode()))
    scope = {"type": "http", "method": "POST", "path": "/login", "raw_path": b"/login",
             "scheme": "http", "query_string": b"", "headers": headers, "server": ("testserver", 80)}
    asyncio.run(guard(scope, receive, send))
    assert not reached and statuses == [400 if length in ("-1", "bad") else 413]
