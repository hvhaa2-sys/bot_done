"""Режим «выключен»: бот не отвечает, не пишет менеджеру и не сохраняет переписку."""
import asyncio
import tempfile
from pathlib import Path

import pytest
from tests.admin_client import AdminClient as TestClient

from app import db as D
from app.admin import create_app
from app.channels.telegram_business import maybe_send_summaries, send_to_client
from tests.test_observe import CLIENT, CONN, OWNER
from tests.test_observe import env as observe_env  # noqa: F401 — окружение aiogram без сети из тестов наблюдения


@pytest.fixture
def bot_env(request):
    return request.getfixturevalue("observe_env")


def switch(bot_env, mode: str) -> None:
    bot_env.db.set_mode(mode, connection_id=CONN)


def command(bot_env, text: str) -> None:
    bot_env.feed({"message": {"message_id": 50, "date": bot_env.now, "text": text,
                          "chat": {"id": OWNER, "type": "private"},
                          "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
                          "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}})


def answers(bot_env) -> list[str]:
    """Что бот ответил менеджеру в личном чате (message.answer идёт через session.make_request)."""
    return [m.text for m in bot_env.api_calls if getattr(m, "text", None)]


def test_off_keeps_nothing_and_sends_nothing(bot_env):
    switch(bot_env, "off")
    bot_env.msg("Какой длины ролик на экране в атриуме?", 1)
    bot_env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    bot_env.msg(None, 3, voice={"file_id": "v", "file_unique_id": "v", "duration": 3})
    bot_env.msg("Исправленный вопрос", 1, event="edited_business_message")
    asyncio.run(maybe_send_summaries(bot_env.bot, bot_env.services))
    assert not bot_env.sent and not bot_env.llm.calls
    assert bot_env.db.observation_stats() == {"messages": 0, "chats": 0, "last_at": None, "candidates": 0}
    assert bot_env.db.stats()["total"] == 0 and bot_env.db.candidates() == []
    assert not bot_env.db.is_paused(CONN, CLIENT), "в выключенном режиме сообщения менеджера бота не паузят"
    assert asyncio.run(send_to_client(bot_env.bot, bot_env.services, CONN, CLIENT, "текст")) is False


def test_mode_command_switches_off_and_back(bot_env):
    command(bot_env, "/mode off")
    assert bot_env.db.get_connection(CONN).mode == "off"
    assert any("выключен" in text for text in answers(bot_env))
    bot_env.msg("Сколько стоит экран?", 1)
    assert bot_env.db.observation_stats()["messages"] == 0
    command(bot_env, "/mode observe")
    bot_env.msg("Сколько стоит экран?", 2)
    assert bot_env.db.get_connection(CONN).mode == "observe" and bot_env.db.observation_stats()["messages"] == 1
    command(bot_env, "/mode sleep")
    assert any("/mode off" in text for text in answers(bot_env)), "подсказка называет и новый режим"


def test_old_draft_button_cannot_send_when_off(bot_env):
    switch(bot_env, "off")
    did = bot_env.db.log_dialog(connection_id=CONN, chat_id=CLIENT, question="Длина ролика?", answer="До 15 секунд.",
                            decision="answer", reason="", confidence=1, status=D.DRAFT_PENDING,
                            sources=[], latency_ms=0, mode="draft")
    bot_env.feed({"callback_query": {"id": "cb", "chat_instance": "c", "data": f"send:{did}",
                                 "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"}}})
    assert not bot_env.sent and bot_env.db.get_dialog(did)["status"] == D.DRAFT_PENDING


def test_admin_switches_off(bot_env):
    client = TestClient(create_app(bot_env.services))
    client.get("/?token=test-observe")
    client.post("/mode", data={"connection_id": CONN, "mode": "off"})
    assert bot_env.db.get_connection(CONN).mode == "off"
    page = client.get("/").text
    assert "1: выключен" in page and 'value="off" selected' in page


# --- наблюдение: вопрос и ответ связываются и через несколько часов ---

def pairing_db():
    db = D.Database(Path(tempfile.mkdtemp()) / "o.db")
    t = 1_800_000_000

    def add(mid, role, text, hours, reply=None):
        db.observe_message(connection_id="c", chat_id=1, message_id=mid, role=role, text=text,
                                 sent_at=t + hours * 3600, reply_to_message_id=reply)
    return db, add


def test_answer_hours_later_or_next_morning_is_still_paired():
    db, add = pairing_db()
    add(10, "client", "Какой длины ролик в атриуме ВЕЕР?", 0)
    add(11, "manager", "До 15 секунд, без звука.", 4)  # менеджер освободился через 4 часа
    assert db.observation_context("c", 1, 11)[1:3] == ("Какой длины ролик в атриуме ВЕЕР?", "До 15 секунд, без звука.")
    add(12, "client", "А кто делает макет?", 5)
    add(13, "manager", "Макет готовит рекламодатель.", 22, reply=12)  # явный ответ на следующее утро
    assert db.observation_context("c", 1, 13)[1:3] == ("А кто делает макет?", "Макет готовит рекламодатель.")
    db.conn.close()


def test_old_greeting_and_stale_answer_are_not_glued():
    db, add = pairing_db()
    add(20, "client", "Здравствуйте", 0)
    add(21, "client", "Сколько стоит экран на фуд-корте?", 20)
    add(22, "manager", "42 000 ₽ в месяц, с НДС.", 20.05)
    assert db.observation_context("c", 1, 22)[1] == "Сколько стоит экран на фуд-корте?"
    add(30, "client", "Есть ли звук?", 30)
    add(31, "manager", "Нет, без звука.", 60)  # через 30 часов и без «ответить» — уже не про этот вопрос
    assert db.observation_context("c", 1, 31) is None
    db.conn.close()


def test_launch_check_names_every_mode():
    from types import SimpleNamespace
    from app.check import connection_modes
    conns = [SimpleNamespace(mode=m) for m in ("off", "observe", "watch", "draft", "auto")]
    assert connection_modes(conns) == "выключен, наблюдение, наблюдение, черновики, автоответ"
