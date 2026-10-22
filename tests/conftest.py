"""Общее для тестов: каждая открытая в тесте база SQLite закрывается после него —
иначе сборщик мусора закрывает их в чужих тестах и засыпает вывод предупреждениями."""
import pytest

from app.db import Database


@pytest.fixture(autouse=True)
def close_databases(monkeypatch):
    opened = []
    original = Database.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", init)
    yield
    for db in opened:
        db.conn.close()
