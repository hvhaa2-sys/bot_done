"""Раздел админки «Наблюдение»: чаты, переписка по порядку, время ответа менеджера, поиск."""
import pytest
from fastapi.testclient import TestClient

from app.admin import create_app
from tests.test_observe import CLIENT, CONN
from tests.test_observe import env as observe_env  # noqa: F401 — окружение aiogram без сети из тестов наблюдения


@pytest.fixture
def bot_env(request):
    return request.getfixturevalue("observe_env")


def test_observe_section_shows_chats_conversation_and_reply_time(bot_env):
    e = bot_env
    e.msg("Здравствуйте! Сколько стоит экран в атриуме ВЕЕР?", 1)
    e.msg("Экран в атриуме — 31 500 ₽ в месяц.", 2, owner=True)
    e.msg("Привет, это другой клиент", 3, chat_id=777)
    client = TestClient(create_app(e.services))
    client.get("/?token=test-observe")
    page = client.get("/observe").text
    assert 'href="/observe" class="on" aria-current="page">Наблюдение' in page, "отдельный пункт меню"
    assert "Идёт наблюдение" in page and "Клиентам ничего не отправляется" in page
    assert "Чаты · 2" in page
    chat = client.get(f"/observe?conn={CONN}&chat={CLIENT}").text
    assert "Привет, это другой клиент" not in chat, "в переписке только выбранный чат"
    assert chat.index("Сколько стоит экран") < chat.index("31 500"), "переписка идёт по порядку"
    assert "Вы · " in chat and "ответ через" in chat and "class='msg me'" in chat
    found = client.get("/observe?q=другой").text
    assert "Найдено: 1" in found and "Привет, это другой клиент" in found


def test_dialogs_no_longer_mix_in_observation(bot_env):
    e = bot_env
    e.msg("Сколько стоит экран?", 1)
    client = TestClient(create_app(e.services))
    client.get("/?token=test-observe")
    dialogs = client.get("/dialogs").text
    assert "Сколько стоит экран?" not in dialogs and ">Наблюдение</a>" not in dialogs.split('<main id="main">')[1]
    assert client.get("/dialogs?f=observe", follow_redirects=False).headers["location"] == "/observe"


def test_observe_section_says_how_to_turn_on(bot_env):
    e = bot_env
    e.db.set_mode("draft", connection_id=CONN)
    client = TestClient(create_app(e.services))
    client.get("/?token=test-observe")
    page = client.get("/observe").text
    assert "наблюдение выключено" in page and "/mode observe" in page and "Сообщений пока нет" in page
