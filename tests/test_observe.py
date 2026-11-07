"""Наблюдение в настоящих обработчиках aiogram, без Telegram и платной модели."""
import asyncio
import datetime as dt
import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Update
from fastapi.testclient import TestClient

from app import db as D
from app.admin import create_app
from app.channels.flow import process_client_message
from app.channels.telegram_business import answer_client, build_dispatcher, maybe_send_summaries, send_to_client
from app.config import ROOT, Settings
from app.core.candidates import EXTRACT_PROMPT
from app.llm.base import LLMProvider
from app.services import Services

OWNER, CLIENT, CONN = 100, 555, "observe-test"


class Extractor(LLMProvider):
    name = "test-extractor"

    def __init__(self):
        self.calls = []

    async def complete(self, system, messages):
        assert system == EXTRACT_PROMPT, "В наблюдении нельзя генерировать ответы клиенту"
        text = messages[-1]["content"]
        self.calls.append(text)
        question, answer = text.removeprefix("Вопрос клиента: ").split("\nОтвет менеджера: ", 1)
        return json.dumps({"facts": [{"question": question, "answer": answer}]}, ensure_ascii=False)


@pytest.fixture
def env(tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(ROOT / "tests/fixtures/kb", kb)
    settings = replace(Settings.load(), db_path=tmp_path / "app.db", kb_dir=kb, llm_provider="mock",
                       default_mode="observe", admin_token="test-observe", exam_files="")
    llm = Extractor()
    services = Services(settings, llm)
    asyncio.run(services.update_settings({"merge_seconds": "0", "summary_time": "00:00"}))
    dp, bot, sent, api_calls = build_dispatcher(services), Bot("42:TEST"), [], []

    async def record(chat_id, text, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def request(bot, method, timeout=None):
        api_calls.append(method)
        return True

    bot.send_message, bot.session.make_request = record, request
    now = int(dt.datetime.now().timestamp())

    def feed(payload):
        update = Update.model_validate({"update_id": 1, **payload}, context={"bot": bot})
        asyncio.run(dp.feed_update(bot, update))

    def msg(text, mid, *, owner=False, chat_id=CLIENT, event="business_message", **extra):
        feed({event: {"message_id": mid, "date": now, "business_connection_id": CONN,
                      "text": text, "chat": {"id": chat_id, "type": "private", "first_name": "Test"},
                      "from": {"id": OWNER if owner else chat_id, "is_bot": False, "first_name": "Test"}, **extra}})

    feed({"business_connection": {"id": CONN, "user": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
                                   "user_chat_id": OWNER, "date": now, "is_enabled": True,
                                   "rights": {"can_reply": True}}})
    services.privacy.set_allowed(CONN, CLIENT, True)
    services.privacy.set_allowed(CONN, CLIENT + 1, True)
    services.privacy.set_allowed(CONN, 777, True)
    sent.clear()
    return SimpleNamespace(services=services, db=services.db, llm=llm, dp=dp, bot=bot, sent=sent,
                           api_calls=api_calls, feed=feed, msg=msg, now=now)


def test_observe_never_answers_or_generates_drafts(env):
    for mid, text in enumerate(("Здравствуйте", "Какие требования к ролику?", "Дадите скидку?", "Спасибо"), 1):
        env.msg(text, mid)
    env.msg(None, 5, voice={"file_id": "v", "file_unique_id": "v", "duration": 3})
    env.msg(None, 6, photo=[{"file_id": "p", "file_unique_id": "p", "width": 20, "height": 20}],
            caption="Требования к этому макету?")
    assert not env.sent and not env.llm.calls
    assert env.db.observation_stats()["messages"] == 6
    # В журнале только обращение для замера времени ответа менеджера — ни ответов бота, ни черновиков
    assert {r["status"] for r in env.db.recent_dialogs(50)} == {D.WATCH}


def test_collects_followups_even_on_existing_pause(env):
    before = env.services.kb_version
    env.db.pause_chat(CONN, CLIENT, 30)
    env.msg("Какой длины ролик в атриуме ВЕЕР?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.msg("А кто делает макет?", 3)
    env.msg("Макет готовит рекламодатель самостоятельно.", 4, owner=True)
    rows = env.db.candidates()
    assert len(rows) == 2 and len(env.llm.calls) == 2
    assert rows[0]["client_question"] == "А кто делает макет?"
    assert "самостоятельно" in rows[0]["answer"]
    assert env.db.observation_stats() == {"messages": 4, "chats": 1, "last_at": env.now, "candidates": 2}
    assert env.services.kb_version == before and not env.services.draft_changed()
    assert not env.sent


def test_groups_message_fragments_and_replaces_old_candidate(env):
    env.msg("Какой длины ролик", 1)
    env.msg("в атриуме ВЕЕР?", 2)
    env.msg("Ролик до 15 секунд на экране.", 3, owner=True)
    env.msg("Звук не воспроизводится.", 4, owner=True)
    row, = env.db.candidates()
    assert row["client_question"] == "Какой длины ролик\nв атриуме ВЕЕР?"
    assert "15 секунд" in row["answer"] and "Звук" in row["answer"]
    assert len(env.db.candidates("superseded")) == 1


def test_duplicate_update_does_not_duplicate_message_or_extraction(env):
    env.msg("Какой длины ролик?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    assert len(env.llm.calls) == 1 and len(env.db.candidates()) == 1
    assert env.db.observation_stats()["messages"] == 2


def test_questions_survive_restart_and_stale_queue(env):
    env.msg("Какой длины ролик?", 1, date=env.now - 3600)
    env.services = Services(env.services.base_settings, env.llm)
    env.dp["services"] = env.services
    env.db = env.services.db
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    assert len(env.db.candidates()) == 1 and not env.sent


def test_different_chats_do_not_mix_questions(env):
    env.msg("Какой длины ролик?", 1)
    env.msg("Дизайн делает рекламодатель самостоятельно.", 2, owner=True, chat_id=556)
    assert not env.db.candidates() and not env.llm.calls


def test_explicit_reply_uses_replied_question(env):
    env.msg("Какая длина ролика?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.msg("Кто делает макет?", 3)
    env.msg("На этом экране ролик до 15 секунд.", 4, owner=True, reply_to_message={
        "message_id": 1, "date": env.now, "text": "Какая длина ролика?",
        "chat": {"id": CLIENT, "type": "private"}})
    assert env.db.candidates()[0]["client_question"] == "Какая длина ролика?"


def test_unknown_explicit_reply_is_not_guessed(env):
    env.msg("Кто делает макет?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 3, owner=True, reply_to_message={
        "message_id": 2, "date": env.now, "chat": {"id": CLIENT, "type": "private"}})
    assert not env.db.candidates()


def test_two_explicit_replies_do_not_merge_answers(env):
    env.msg("Какая длина ролика?", 1)
    env.msg("Кто делает макет?", 2)
    for mid, target, text in ((3, 1, "Ролик до 15 секунд, без звука."),
                              (4, 2, "Макет готовит рекламодатель самостоятельно.")):
        env.msg(text, mid, owner=True, reply_to_message={
            "message_id": target, "date": env.now, "chat": {"id": CLIENT, "type": "private"}})
    row = env.db.candidates()[0]
    assert row["client_question"] == "Кто делает макет?" and "15 секунд" not in row["answer"]


def test_edited_answer_replaces_candidate_and_masks_contacts(env):
    env.msg("Какая длина ролика? Мой телефон +7 912 345-67-89", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.msg("Ролик до 10 секунд, без звука. Почта test@example.com", 2, owner=True, event="edited_business_message")
    row, = env.db.candidates()
    assert "10 секунд" in row["answer"] and "15 секунд" not in row["answer"]
    assert "912" not in row["client_question"] and "test@example.com" not in row["answer"]
    assert all("912" not in r["text"] and "test@example.com" not in r["text"] for r in env.db.observations())


def test_edited_question_invalidates_candidate(env):
    env.msg("Какая длина ролика?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.msg("Кто делает макет?", 1, event="edited_business_message")
    assert not env.db.candidates()


def test_delete_invalidates_source_candidate(env):
    env.msg("Какая длина ролика?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    env.feed({"deleted_business_messages": {"business_connection_id": CONN,
             "chat": {"id": CLIENT, "type": "private"}, "message_ids": [2]}})
    assert not env.db.candidates() and env.db.observation_stats()["messages"] == 1


def test_old_draft_button_and_manager_edit_cannot_send(env):
    did = env.db.log_dialog(connection_id=CONN, chat_id=CLIENT, question="Длина ролика?", answer="До 15 секунд.",
                           decision="answer", reason="", confidence=1, status=D.DRAFT_PENDING,
                           sources=[], latency_ms=0, mode="draft")
    env.feed({"callback_query": {"id": "cb", "chat_instance": "c", "data": f"send:{did}",
                                "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"}}})
    env.feed({"message": {"message_id": 20, "date": env.now, "text": "Моя правка ответа",
                          "chat": {"id": OWNER, "type": "private"},
                          "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
                          "reply_to_message": {"message_id": 19, "date": env.now, "text": f"Черновик #{did}",
                                               "chat": {"id": OWNER, "type": "private"}}}})
    assert not env.sent and env.db.get_dialog(did)["status"] == D.DRAFT_PENDING


def test_observe_summary_goes_only_to_owner(env):
    # Наблюдение (общий режим с 28.10): клиенту — ни слова, владельцу — итоги дня и время своего ответа
    env.msg("Какая длина ролика?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    asyncio.run(maybe_send_summaries(env.bot, env.services))
    assert env.sent and all(m["chat_id"] == OWNER for m in env.sent)
    assert "Наблюдение за день: обращений клиентов — 1, вы ответили на 1" in env.sent[0]["text"]


def test_observe_works_without_reply_permission(env):
    env.db.conn.execute("UPDATE connections SET can_reply=0 WHERE id=?", (CONN,))
    env.db.conn.commit()
    env.msg("Какая длина ролика?", 1)
    env.msg("Ролик до 15 секунд, без звука.", 2, owner=True)
    assert len(env.db.candidates()) == 1 and not env.sent


def test_manager_notification_retry_stops_after_switch_to_observe(env, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage
    import app.channels.telegram_business as tg

    env.db.set_mode("draft", connection_id=CONN)
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append(1)
        env.db.set_mode("observe", connection_id=CONN)
        raise TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="test"), message="offline")

    async def no_sleep(*args):
        pass

    env.bot.send_message = failed
    monkeypatch.setattr(tg.asyncio, "sleep", no_sleep)
    asyncio.run(tg.safe_send(env.bot, OWNER, "Черновик", services=env.services, connection_id=CONN))
    assert len(attempts) == 1


def test_pending_answer_and_flow_cannot_bypass_observe(env):
    asyncio.run(answer_client(env.bot, env.services, CONN, CLIENT, "Какая длина ролика?"))
    out = asyncio.run(process_client_message(env.services, env.db.get_connection(CONN), CLIENT, "Привет"))
    assert out.dialog_id == 0 and not out.to_client and not out.to_manager
    assert not env.sent and not env.llm.calls


def test_mode_changed_while_model_worked_drops_outgoing(env):
    env.db.set_mode("auto", connection_id=CONN)

    async def answer(*args, **kwargs):
        from app.core.assistant import Reply
        env.db.set_mode("observe", connection_id=CONN)
        return Reply(text="До 15 секунд.", confidence=1, decision="answer", reason="", sources=[])

    env.services.answer = answer
    asyncio.run(answer_client(env.bot, env.services, CONN, CLIENT, "Какая длина ролика?"))
    assert not env.sent


def test_retry_checks_current_mode(env, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage
    import app.channels.telegram_business as tg

    env.db.set_mode("auto", connection_id=CONN)
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append(1)
        env.db.set_mode("observe", connection_id=CONN)
        raise TelegramNetworkError(method=SendMessage(chat_id=CLIENT, text="test"), message="offline")

    async def no_sleep(*args):
        pass

    env.bot.send_message = failed
    monkeypatch.setattr(tg.asyncio, "sleep", no_sleep)
    assert not asyncio.run(send_to_client(env.bot, env.services, CONN, CLIENT, "До 15 секунд."))
    assert len(attempts) == 1


def test_unapproved_connection_and_bot_messages_not_collected(env):
    env.msg("Сообщение другого бота", 1, sender_business_bot={"id": 42, "is_bot": True, "first_name": "Bot"})
    env.msg("Автоответ Telegram", 2, is_from_offline=True)
    env.db.conn.execute("UPDATE connections SET approved=0 WHERE id=?", (CONN,))
    env.db.conn.commit()
    env.msg("Чужое подключение", 3)
    assert not env.db.observations() and not env.sent and not env.llm.calls


def test_admin_observe_journal_and_settings(env):
    client = TestClient(create_app(env.services))
    assert client.get("/dialogs?f=observe").status_code == 401
    client.get("/?token=test-observe")
    env.msg("<script>alert(1)</script> Какая длина ролика?", 1)
    r = client.get("/dialogs?f=observe")
    assert "&lt;script&gt;" in r.text and "<script>alert" not in r.text
    assert "Наблюдение" in r.text and "Клиентам ничего не отправляется" in r.text
    client.post("/mode", data={"connection_id": CONN, "mode": "draft"})
    client.post("/mode", data={"connection_id": CONN, "mode": "observe"})
    assert env.db.get_connection(CONN).mode == "observe"
    assert "Наблюдение без ответов" in client.get("/").text
    result = asyncio.run(env.services.update_settings({"default_mode": "observe"}))
    assert not result and env.services.settings.default_mode == "observe"
    assert 'value="observe" selected' in client.get("/settings").text


def test_access_table_names_chats_that_wait_for_permission(env):
    # 07.11: после обновления все чаты наблюдения стояли «не записывается», а в таблице были одни номера —
    # владелец не мог понять, какие из них рабочие. Имя берём только из памяти, в журнал оно не пишется.
    env.feed({"business_message": {"message_id": 1, "date": env.now, "business_connection_id": CONN,
                                   "text": "Сколько стоит экран?", "chat": {"id": 888, "type": "private",
                                                                          "first_name": "Агентство", "last_name": "Север"},
                                   "from": {"id": 888, "is_bot": False, "first_name": "Агентство"}}})
    assert env.db.observation_stats()["messages"] == 0
    client = TestClient(create_app(env.services))
    client.get("/?token=test-observe")
    page = client.get("/observe").text
    assert "Агентство Север" in page and "ждут решения 1" in page
    client.post("/observe/access", data={"connection_id": CONN, "chat_id": 888, "allowed": 1})
    env.msg("А на месяц?", 2, chat_id=888)
    assert env.db.observation_stats()["messages"] == 1
    assert "Агентство" not in json.dumps([dict(r) for r in env.db.observed_chat(CONN, 888)], ensure_ascii=False)
