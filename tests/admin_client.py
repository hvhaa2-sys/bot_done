"""Клиент админки с Origin браузерной формы; отрицательные проверки используют сырой TestClient."""
from fastapi.testclient import TestClient


class AdminClient(TestClient):
    def __init__(self, app, *args, **kwargs):
        headers = {"Origin": kwargs.get("base_url", "http://testserver").rstrip("/"), **kwargs.pop("headers", {})}
        super().__init__(app, *args, headers=headers, **kwargs)
