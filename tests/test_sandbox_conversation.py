"""Тестовый чат проходит обычный конвейер, сохраняя выбор только в своей браузерной сессии."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.admin import create_app
from app.config import Settings
from app.core.knowledge import PriceRow, prices_csv
from app.llm.base import LLMProvider
from app.services import Services


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self):
        self.answer = "Экран в ВЕЕР — 30 000 ₽ за месяц."
        self.calls = []

    async def complete(self, system, messages):
        self.calls.append((system, messages))
        return json.dumps({"answer": self.answer, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


@pytest.fixture
def sandbox_app(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    rows = [
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 30000, ""),
        PriceRow("Баннер", "ВЕЕР Молл, Екатеринбург — парковка", "1 месяц", 40000, ""),
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — вход", "1 месяц", 50000, ""),
        PriceRow("Баннер", "ДЕПО Молл, Нижний Тагил — галерея", "1 месяц", 60000, ""),
    ]
    (kb_dir / "prices.csv").write_text(prices_csv(rows), encoding="utf-8")
    settings = replace(Settings.load(), db_path=tmp_path / "sandbox.db", kb_dir=kb_dir,
                       llm_provider="mock", admin_token="secret", telegram_bot_token="",
                       gigachat_auth_key="", openai_api_key="")
    llm = FakeLLM()
    services = Services(settings, llm=llm)
    yield create_app(services), services, llm
    services.db.conn.close()


def ask(client, question):
    response = client.post("/sandbox?token=secret", data={"question": question})
    assert response.status_code == 200
    return response


def turns(services, session):
    return services.db.conversation_turns(f"sandbox:{session}", 0)


def test_sandbox_clarifies_venue_then_format_and_answers(sandbox_app):
    app, services, llm = sandbox_app
    with TestClient(app) as client:
        first = ask(client, "Сколько стоит размещение?")
        assert "В каком торговом центре" in first.text and "Бот уточнит у клиента" in first.text
        session = client.cookies.get("sandbox_session")
        assert session and llm.calls == []

        second = ask(client, "В ВЕЕР")
        assert "Какой формат рекламы" in second.text and "Бот уточнит у клиента" in second.text
        assert client.cookies.get("sandbox_session") == session and llm.calls == []

        third = ask(client, "Экран")
        assert "30 000" in third.text and "Бот ответит сам" in third.text
        assert len(llm.calls) == 1
        system, messages = llm.calls[0]
        assert "ТЦ: ВЕЕР Молл, Екатеринбург" in system and "формат: экран" in system
        assert messages[-1]["content"] == "Экран"
        saved = turns(services, session)
        assert [row["reason"] for row in saved] == ["clarification:venue", "clarification:format", ""]
        assert all(row["status"] == "sandbox" for row in saved)
        assert services.db.stats()["real"] == 0


def test_browser_sessions_keep_selections_and_clarification_limits_separate(sandbox_app):
    app, services, llm = sandbox_app
    with TestClient(app) as first, TestClient(app) as second:
        assert "30 000" in ask(first, "Сколько стоит экран в ВЕЕР?").text
        first_session = first.cookies.get("sandbox_session")
        assert len(llm.calls) == 1

        response = ask(second, "Сколько стоит?")
        assert "В каком торговом центре" in response.text
        second_session = second.cookies.get("sandbox_session")
        assert second_session != first_session and len(llm.calls) == 1

        assert "Какой формат рекламы" in ask(second, "В ДЕПО").text
        llm.answer = "Экран в ДЕПО — 50 000 ₽ за месяц."
        assert "50 000" in ask(second, "Экран").text
        second_system, second_messages = llm.calls[-1]
        assert "ТЦ: ДЕПО Молл, Нижний Тагил" in second_system
        assert "ВЕЕР" not in second_system
        assert not any("ВЕЕР" in message["content"] for message in second_messages)

        llm.answer = "Экран в ВЕЕР — 30 000 ₽ за месяц."
        assert "30 000" in ask(first, "А на месяц?").text
        first_system, first_messages = llm.calls[-1]
        assert "ТЦ: ВЕЕР Молл, Екатеринбург" in first_system
        assert "ДЕПО" not in first_system
        assert not any("ДЕПО" in message["content"] for message in first_messages)
        assert len(turns(services, first_session)) == 2
        assert len(turns(services, second_session)) == 3


def test_reset_discards_selection_and_keeps_previous_test_log(sandbox_app):
    app, services, llm = sandbox_app
    with TestClient(app) as client:
        ask(client, "Сколько стоит экран в ВЕЕР?")
        old_session = client.cookies.get("sandbox_session")
        reset = client.post("/sandbox/reset", follow_redirects=False)
        assert reset.status_code == 303 and reset.headers["location"] == "/"
        assert client.cookies.get("sandbox_session") is None

        response = ask(client, "А сколько стоит?")
        assert "В каком торговом центре" in response.text
        new_session = client.cookies.get("sandbox_session")
        assert new_session != old_session
        assert len(llm.calls) == 1
        assert len(turns(services, old_session)) == len(turns(services, new_session)) == 1
        assert services.db.stats()["total"] == 2


def test_reset_restores_clarification_budget_without_resetting_other_browser(sandbox_app):
    app, services, llm = sandbox_app
    with TestClient(app) as first, TestClient(app) as second:
        ask(first, "Сколько стоит экран?")
        ask(first, "Не знаю")
        ask(second, "Сколько стоит экран?")
        second_session = second.cookies.get("sandbox_session")
        first.post("/sandbox/reset", follow_redirects=False)

        response = ask(first, "Сколько стоит экран?")
        assert "В каком торговом центре" in response.text and "Передаст менеджеру" not in response.text
        assert "В каком торговом центре" in ask(first, "Не знаю").text

        assert "В каком торговом центре" in ask(second, "Не знаю").text
        exhausted = ask(second, "Пока не решил")
        assert "Передаст менеджеру" in exhausted.text
        assert turns(services, second_session)[-1]["reason"] == "clarification_limit"
        assert llm.calls == []
