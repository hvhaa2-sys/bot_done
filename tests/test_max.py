"""Адаптер MAX без сети: транспорт подменяем, события подаём словарями в формате MAX Bot API."""
import asyncio
import itertools
import json
import shutil
import time
from dataclasses import replace

import httpx
import pytest
from tests.admin_client import AdminClient as TestClient

from app import db as D
from app.admin import create_app
from app.channels.max_api import MaxApi, MaxError, keyboard
from app.channels.max_bot import (AFTER_ANSWER, ASK_CONTACT, BUDGET_ASK, CLIENT_MENU, CONN_PREFIX, CONSENT_BUTTONS,
                                  CONSENT_NO, HELP, HUMAN_AGAIN, HUMAN_DONE, LEAD_ASK, LEAD_DONE, LEAD_IN_CHAT, NO_MANAGER,
                                  NOT_DELIVERED, OBSERVE_MENU, REVOKED_DONE, MaxBot, handle_queue, run_max, run_webhook)
from app.config import ROOT, Settings
from app.services import Services
from tests.test_core import FakeLLM

BOT, MANAGER, MANAGER_CHAT, CLIENT, CLIENT_CHAT = 1, 100, 1000, 555, 5550
CONN = f"{CONN_PREFIX}{MANAGER}"
SEQ = itertools.count(1)  # у MAX номер сообщения в чате растёт по порядку


class FakeApi:
    """Тот же интерфейс, что у MaxApi: запоминает, что бот отправил."""
    def __init__(self):
        self.sent, self.answers, self.fail, self.no_forward = [], [], False, False

    async def send(self, text, *, chat_id=None, user_id=None, buttons=None, forward=None, files=None):
        if self.fail or (forward and self.no_forward):
            raise MaxError(503, "service.unavailable")
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "forward": forward, "files": files})
        seq = 10_000 + len(self.sent)  # как у MAX: номер сообщения растёт по порядку
        return {"message": {"body": {"mid": f"bot{seq}", "seq": seq, "text": text},
                            "timestamp": int(time.time() * 1000)}}

    async def subscriptions(self):
        return []

    async def answer(self, callback_id, *, notification=None, text=None, buttons=None):
        self.answers.append({"callback_id": callback_id, "notification": notification, "text": text, "buttons": buttons})
        return {}


@pytest.fixture
def env(tmp_path):
    # Копия базы: утверждённые предложения пишутся в заметки, общий пример в tests/fixtures не трогаем
    shutil.copytree(ROOT / "tests" / "fixtures" / "kb", tmp_path / "kb")
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=tmp_path / "kb", llm_provider="mock")
    services = Services(settings)
    asyncio.run(services.update_settings({"merge_seconds": "0"}))  # склейку проверяем отдельно
    api = FakeApi()
    return services, MaxBot(api, services, BOT), api


def feed(bot, update):
    asyncio.run(bot.handle(update))


def message(text, user=CLIENT, chat=CLIENT_CHAT, *, attachments=None, link=None, age_s=0, chat_type="dialog",
            is_bot=False, mid=None):
    seq = next(SEQ)
    mid = mid or f"auto{seq}"  # у каждого сообщения MAX свой mid, повторы бот отбрасывает
    msg = {"sender": {"user_id": user, "first_name": "Иван", "last_name": "Петров", "username": "ivan",
                      "is_bot": is_bot},
           "recipient": {"chat_id": chat, "chat_type": chat_type, "user_id": None},
           "timestamp": int((time.time() - age_s) * 1000),
           "body": {"mid": mid, "seq": seq, "text": text, "attachments": attachments}}
    if link:
        msg["link"] = link
    return {"update_type": "message_created", "timestamp": msg["timestamp"], "message": msg}


def callback(payload, card="", user=MANAGER, callback_id=None):
    callback_id = callback_id or f"cb{next(SEQ)}"  # у каждого нажатия свой номер
    return {"update_type": "message_callback", "callback": {"callback_id": callback_id, "payload": payload,
                                                             "user": {"user_id": user}},
            "message": {"body": {"mid": "card", "seq": 1, "text": card}}}


def reply_to(card, text):
    return message(text, MANAGER, MANAGER_CHAT, link={"type": "reply", "message": {"mid": "card", "seq": 1, "text": card}})


def connect(bot):
    feed(bot, message("/manager", MANAGER, MANAGER_CHAT))
    bot.services.db.approve_connection(CONN)
    feed(bot, message("/manager", MANAGER, MANAGER_CHAT))
    bot.services.privacy.set_allowed(CONN, CLIENT_CHAT, True)


def to(api, chat):
    return [m for m in api.sent if m["chat_id"] == chat]


def test_approved_manager_connects_and_stranger_waits(env):
    services, bot, api = env
    connect(bot)
    conn = services.db.get_connection(CONN)
    assert conn.approved and conn.owner_chat_id == MANAGER_CHAT and conn.mode == "draft"
    assert to(api, MANAGER_CHAT)[-1]["text"].startswith("Готово")
    feed(bot, message("/manager", 777, 7770))
    assert not services.db.get_connection(f"{CONN_PREFIX}777").approved, "второй — только после «Разрешить» в админке"
    assert "отправлен администратору" in to(api, 7770)[-1]["text"]
    feed(bot, message("/mode auto", 777, 7770))
    assert services.db.get_connection(f"{CONN_PREFIX}777").mode == "draft", "неодобренный режим не меняет"


def test_client_without_manager_is_told_and_model_is_not_spent(env):
    services, bot, api = env
    feed(bot, message("Сколько стоит экран в атриуме?"))
    assert [m["text"] for m in to(api, CLIENT_CHAT)] == [NO_MANAGER], "не молчим: клиент поймёт, что написать позже"
    assert services.db.recent_dialogs(5) == []


def test_draft_card_goes_to_manager_with_buttons(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Какие требования к макету?"))
    card = to(api, MANAGER_CHAT)[-1]
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    assert f"Черновик #{dialog_id}" in card["text"] and "Иван Петров (@ivan)" in card["text"]
    assert card["buttons"] == [[("Отправить", f"send:{dialog_id}"), ("Пропустить", f"skip:{dialog_id}")]]
    assert to(api, CLIENT_CHAT) == [], "в черновиках клиенту ничего не уходит"


def test_auto_mode_answers_client(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to(api, CLIENT_CHAT)[-1]["text"]
    assert services.db.recent_dialogs(1)[0]["status"] == D.SENT_AUTO


def test_escalation_card_asks_to_reply_here_not_in_client_chat(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("Дадите скидку?"))
    assert to(api, CLIENT_CHAT)[-1]["text"] == services.assistant.holding_message
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert "Вопрос #" in card and "в его чате" not in card and "Ответьте на это сообщение своим текстом" in card


def test_send_button_sends_draft_once(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Какие требования к макету?"))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    feed(bot, callback(f"send:{dialog_id}", card))
    assert "PDF" in to(api, CLIENT_CHAT)[-1]["text"]
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_SENT
    answer = api.answers[-1]
    assert answer["notification"] == "Отправлено клиенту." and "Отправлено клиенту." in answer["text"]
    assert answer["buttons"] is None, "кнопки убраны"
    feed(bot, callback(f"send:{dialog_id}", card, callback_id="cb2"))
    assert len(to(api, CLIENT_CHAT)) == 1 and api.answers[-1]["notification"] == "Черновик уже обработан."


def test_failed_send_keeps_draft_and_buttons(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Какие требования к макету?"))
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    api.fail = True
    feed(bot, callback(f"send:{dialog_id}", "карточка"))
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_PENDING, "можно нажать ещё раз"


@pytest.mark.parametrize("mode", ["auto", "draft"])
def test_rejected_price_draft_requires_manager_reply(env, mode):
    services, bot, api = env
    connect(bot)
    services.db.set_mode(mode, connection_id=CONN)
    services.assistant.llm = FakeLLM("Лайтбокс стоит 20 000 ₽.")
    feed(bot, message("Сколько стоит лайтбокс у главного входа?"))
    row = services.db.recent_dialogs(1)[0]
    assert row["reason"].startswith("price_mismatch")
    card = to(api, MANAGER_CHAT)[-1]
    before = list(to(api, CLIENT_CHAT))
    expected = [services.assistant.holding_message] if mode == "auto" else []
    assert [msg["text"] for msg in before] == expected
    feed(bot, callback(f"send:{row['id']}", card["text"]))
    assert to(api, CLIENT_CHAT) == before
    assert services.db.get_dialog(row["id"])["status"] == row["status"]
    assert card["buttons"] == [[("Пропустить", f"skip:{row['id']}")]]
    assert "20 000" in card["text"] and "исправ" in card["text"].lower() and "Нажмите «Отправить»" not in card["text"]
    assert "исправ" in api.answers[-1]["notification"].lower()
    feed(bot, reply_to(card["text"], row["answer"]))
    assert to(api, CLIENT_CHAT) == before
    feed(bot, reply_to(card["text"], "Лайтбокс стоит 25 000 ₽ за месяц."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Лайтбокс стоит 25 000 ₽ за месяц."
    assert services.db.get_dialog(row["id"])["status"] == D.DRAFT_EDITED


def test_skip_button_and_stranger_button(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Какие требования к макету?"))
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    feed(bot, callback(f"send:{dialog_id}", user=CLIENT))
    assert api.answers[-1]["notification"] == "Нет доступа." and to(api, CLIENT_CHAT) == []
    feed(bot, callback(f"skip:{dialog_id}"))
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_REJECTED and to(api, CLIENT_CHAT) == []


def test_manager_reply_to_card_goes_to_client(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Дадите скидку?"))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    feed(bot, reply_to(card, "Скидка 5% при размещении от 3 месяцев."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Скидка 5% при размещении от 3 месяцев."
    dialog = services.db.recent_dialogs(1)[0]
    assert dialog["status"] == D.DRAFT_EDITED and to(api, MANAGER_CHAT)[-1]["text"] == "Ваш вариант отправлен клиенту."
    feed(bot, reply_to(card, "ещё раз"))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "ещё раз"


def test_manager_text_without_card_gets_help(env):
    _, bot, api = env
    connect(bot)
    feed(bot, message("а как ответить клиенту?", MANAGER, MANAGER_CHAT))
    assert to(api, MANAGER_CHAT)[-1]["text"] == HELP


def test_media_goes_to_manager_and_pauses_chat(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("", attachments=[{"type": "audio", "payload": {"url": "x"}}]))
    assert to(api, CLIENT_CHAT)[-1]["text"] == services.assistant.holding_message
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert "аудио" in card and "Вопрос #" in card
    assert services.db.is_paused(CONN, CLIENT_CHAT)
    feed(bot, message("Сколько стоит экран в атриуме?", mid="m2"))
    assert len(to(api, CLIENT_CHAT)) == 1, "чат на паузе — бот молчит, пока менеджер не ответит"
    feed(bot, reply_to(card, "Послушал, перезвоню."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Послушал, перезвоню."


def test_sticker_bot_and_group_messages_are_ignored(env):
    services, bot, api = env
    connect(bot)
    before = len(api.sent)
    feed(bot, message("", attachments=[{"type": "sticker", "payload": {}}]))
    feed(bot, message("Сколько стоит экран?", user=999, is_bot=True))
    feed(bot, message("Сколько стоит экран?", user=BOT))
    feed(bot, message("Сколько стоит экран?", chat_type="chat"))
    assert len(api.sent) == before and services.db.recent_dialogs(5) == []


def test_nothing_from_client_is_lost_when_bot_does_not_answer(env):
    """В MAX у менеджера нет своего чата с клиентом: старое сообщение и сообщение в чат на паузе
    бот не отвечает, но пересылает менеджеру, а ответ на эту карточку уходит клиенту."""
    services, bot, api = env
    connect(bot)
    feed(bot, message("Сколько стоит экран?", age_s=3600))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert "пока бот был выключен" in card and "«Сколько стоит экран?»" in card and to(api, CLIENT_CHAT) == []
    stale = services.db.recent_dialogs(1)[0]
    assert (stale["status"], stale["reason"]) == (D.ESCALATED, "relayed")
    assert stale["id"] not in [r["id"] for r in services.db.to_learn()], "бот не пытался ответить — это не пробел базы"
    feed(bot, reply_to(card, "Экран — 30 000 ₽ в месяц."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Экран — 30 000 ₽ в месяц."

    feed(bot, message("", attachments=[{"type": "image", "payload": {}}], mid="photo1"))
    assert [m["forward"] for m in to(api, MANAGER_CHAT)[-2:]] == ["photo1", None], "фото менеджер видит само"
    feed(bot, message("", attachments=[{"type": "image", "payload": {}}], mid="photo2"))
    assert to(api, MANAGER_CHAT)[-1]["forward"] == "photo2", "второе фото альбома — только пересылка, без карточки"
    feed(bot, message("Это макет, подойдёт?", mid="m3"))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert "чат на паузе" in card and "«Это макет, подойдёт?»" in card and len(to(api, CLIENT_CHAT)) == 1
    feed(bot, reply_to(card, "Подойдёт."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Подойдёт."


def test_observe_relays_both_ways_measures_and_learns(env, monkeypatch):
    """Наблюдение в MAX: модель не тратится, вопросы — менеджеру, его ответы — клиенту,
    переписка в журнале наблюдения, время ответа замерено, ответ — предложение в базу знаний."""
    services, bot, api = env
    asyncio.run(services.update_settings({"default_mode": "observe"}))
    connect(bot)
    assert services.db.get_connection(CONN).mode == D.OBSERVE, "режим новых подключений — из настроек админки"

    async def no_model(*_, **__):
        raise AssertionError("в наблюдении модель не вызывается")

    monkeypatch.setattr(services, "answer", no_model)
    feed(bot, message("Здравствуйте", age_s=120, mid="m1"))
    feed(bot, message("Делаете ли вы макет для лайтбокса?", age_s=90, mid="m2"))
    cards = to(api, MANAGER_CHAT)[-2:]
    watch = services.db.recent_dialogs(1)[0]
    assert all(f"Вопрос #{watch['id']}" in c["text"] for c in cards), "куски одного вопроса — одна запись"
    assert watch["status"] == D.WATCH and watch["question"] == "Здравствуйте\nДелаете ли вы макет для лайтбокса?"
    assert to(api, CLIENT_CHAT) == [], "бот клиенту сам ничего не пишет"

    feed(bot, reply_to(cards[-1]["text"], "Да, макет делает наш дизайнер, от трёх месяцев бесплатно."))
    assert to(api, CLIENT_CHAT)[-1]["text"].startswith("Да, макет делает") and \
        to(api, MANAGER_CHAT)[-1]["text"] == "Отправлено клиенту."
    row = services.db.get_dialog(watch["id"])
    assert row["answer"].startswith("Да, макет") and row["reply_after_s"] >= 80
    roles = [m["role"] for m in services.db.observed_chat(CONN, CLIENT_CHAT)]
    assert roles == ["client", "client", "manager"]
    assert services.db.candidates(), "ответ менеджера — предложение в базу знаний"
    lines = [json.loads(x) for x in services.watch_log.read_text(encoding="utf-8").splitlines()]
    assert [x["from"] for x in lines] == ["client", "client", "manager"] and "Иван" not in str(lines)

    feed(bot, message("", attachments=[{"type": "audio", "payload": {}}], mid="voice"))
    assert to(api, MANAGER_CHAT)[-2]["forward"] == "voice" and "прислал(а) аудио" in to(api, MANAGER_CHAT)[-1]["text"]


def test_max_modes_from_command_and_admin(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode watch", MANAGER, MANAGER_CHAT))
    assert services.db.get_connection(CONN).mode == D.OBSERVE
    feed(bot, message("/mode off", MANAGER, MANAGER_CHAT))
    assert services.db.get_connection(CONN).mode == D.OBSERVE and "/mode observe" in to(api, MANAGER_CHAT)[-1]["text"]
    from app.admin import create_app
    from tests.admin_client import AdminClient as TestClient
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", services.settings.admin_token)
    page = client.get("/").text
    assert '<option value="observe" selected>Наблюдение</option>' in page and '<option value="off"' not in page
    client.post("/mode", data={"connection_id": CONN, "mode": "draft"})
    client.post("/mode", data={"connection_id": CONN, "mode": "off"})
    assert services.db.get_connection(CONN).mode == "draft", "«Выключен» для MAX не ставится"
    client.post("/mode", data={"connection_id": CONN, "mode": "observe"})
    assert services.db.get_connection(CONN).mode == D.OBSERVE


def test_start_greets_client_and_helps_manager(env):
    services, bot, api = env
    connect(bot)
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}})
    assert to(api, CLIENT_CHAT)[-1]["text"] == services.assistant.greeting_message
    feed(bot, {"update_type": "bot_started", "chat_id": MANAGER_CHAT, "user": {"user_id": MANAGER}})
    assert to(api, MANAGER_CHAT)[-1]["text"] == HELP
    feed(bot, message("/mode auto"))
    assert services.db.get_connection(CONN).mode == "draft", "клиент режим не меняет"


def test_status_and_resume_commands(env):
    services, bot, api = env
    connect(bot)
    services.db.pause_chat(CONN, CLIENT_CHAT, 30)
    feed(bot, message("/resume", MANAGER, MANAGER_CHAT))
    assert not services.db.is_paused(CONN, CLIENT_CHAT) and "Паузы сняты (1)" in to(api, MANAGER_CHAT)[-1]["text"]
    feed(bot, message("/status", MANAGER, MANAGER_CHAT))
    assert to(api, MANAGER_CHAT)[-1]["text"].startswith("Всего вопросов")


def test_messages_in_a_row_are_merged(env):
    services, bot, api = env
    connect(bot)
    asyncio.run(services.update_settings({"merge_seconds": "0.05"}))

    async def scenario():
        await bot.handle(message("Здравствуйте", mid="a"))
        await bot.handle(message("Какие требования", mid="b"))
        await bot.handle(message("к макету?", mid="c"))
        await bot.handle({"update_type": "message_removed", "message_id": "a", "chat_id": CLIENT_CHAT})
        await asyncio.sleep(0.2)
    asyncio.run(scenario())
    dialogs = services.db.recent_dialogs(5)
    assert len(dialogs) == 1 and dialogs[0]["question"] == "Какие требования\nк макету?"


def test_evening_summary_is_sent_by_max_and_skipped_by_telegram(env):
    from aiogram import Bot

    from app.channels.telegram_business import maybe_send_summaries
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("Сколько стоит экран в атриуме?"))
    late = services.local_now().replace(hour=23, minute=59)
    tg = Bot("42:TEST")
    assert asyncio.run(maybe_send_summaries(tg, services, late)) == 0, "Telegram не шлёт менеджеру MAX"
    assert asyncio.run(bot.maybe_send_summaries(late)) == 1
    summary = to(api, MANAGER_CHAT)[-1]
    assert "Итоги дня" in summary["text"] and summary["buttons"][0][0][0] == "Ошибка 1"
    assert asyncio.run(bot.maybe_send_summaries(late)) == 0, "второй раз за день не шлём"
    feed(bot, callback(summary["buttons"][0][0][1]))
    assert services.db.stats()["flagged"] == 1


def test_run_max_stops_on_bad_token(env):
    services, _, _ = env

    class BadToken:
        async def me(self):
            raise MaxError(401, "verify.token", "Invalid access_token")
    asyncio.run(run_max(BadToken(), services))
    assert services.max_status["ok"] is False and "MAX_BOT_TOKEN" in services.max_status["error"]


def test_run_max_polls_with_marker(env):
    services, _, _ = env
    services.db.upsert_connection(CONN, MANAGER, MANAGER_CHAT, True, True, "auto")

    class Api(FakeApi):
        def __init__(self):
            super().__init__()
            self.markers = []

        async def me(self):
            return {"user_id": BOT, "username": "shop_bot", "is_bot": True}

        async def updates(self, marker=None):
            self.markers.append(marker)
            if len(self.markers) == 1:
                return {"updates": [{"update_type": "unknown"}, message("Сколько стоит экран в атриуме?")],
                        "marker": 42}
            raise asyncio.CancelledError  # остановить опрос в тесте
    api = Api()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_max(api, services))
    assert api.markers == [None, 42] and services.max_status["username"] == "shop_bot"
    assert "30 000" in to(api, CLIENT_CHAT)[-1]["text"]


# --- HTTP-клиент ---
def test_api_sends_token_text_and_keyboard():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"message": {}})
    api = MaxApi("TOKEN", transport=httpx.MockTransport(handler))
    asyncio.run(api.send("Привет" * 1000, chat_id=5, buttons=[[("Отправить", "send:1")]]))
    req = calls[0]
    assert req.headers["Authorization"] == "TOKEN" and req.url.host == "platform-api2.max.ru"
    assert req.url.path == "/messages" and dict(req.url.params) == {"chat_id": "5"}
    body = json.loads(req.content)
    assert len(body["text"]) < 4000 and body["attachments"] == [keyboard([[("Отправить", "send:1")]])]
    assert body["attachments"][0]["payload"]["buttons"][0][0] == {"type": "callback", "text": "Отправить",
                                                                  "payload": "send:1"}


def test_api_answer_removes_buttons_and_retries_overload(monkeypatch):
    from app.channels import max_api
    monkeypatch.setattr(max_api, "RETRY_DELAYS", (0, 0, 0))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, json={"success": True})
    api = MaxApi("TOKEN", transport=httpx.MockTransport(handler))
    asyncio.run(api.answer("cb1", notification="Отправлено.", text="Карточка"))
    assert len(calls) == 2 and calls[1].url.path == "/answers" and dict(calls[1].url.params) == {"callback_id": "cb1"}
    assert json.loads(calls[1].content) == {"notification": "Отправлено.",
                                            "message": {"text": "Карточка", "attachments": []}}


def test_api_error_carries_status_and_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "verify.token", "message": "Invalid access_token"})
    api = MaxApi("TOKEN", transport=httpx.MockTransport(handler))
    with pytest.raises(MaxError) as err:
        asyncio.run(api.me())
    assert err.value.status == 401 and err.value.code == "verify.token"
    with pytest.raises(ValueError):
        MaxApi("")


def test_knowledge_proposals_are_decided_from_max(env):
    services, bot, api = env
    connect(bot)
    late = services.local_now().replace(hour=23, minute=59)
    keep = services.db.add_candidate(connection_id=CONN, chat_id=CLIENT_CHAT, dialog_id=None, source="card",
                                     client_question="Дизайн делаете?", manager_text="Да, макет готовит наш дизайнер.",
                                     question="Делаете ли вы дизайн баннера?", answer="Да, макет готовит наш дизайнер.")
    drop = services.db.add_candidate(connection_id=CONN, chat_id=CLIENT_CHAT, dialog_id=None, source="card",
                                     client_question="q", manager_text="m", question="Скидка будет?", answer="Да.")
    asyncio.run(bot.maybe_send_summaries(late))
    proposals = to(api, MANAGER_CHAT)[-1]
    assert "Предложения в базу знаний" in proposals["text"]
    payloads = {payload for row in proposals["buttons"] for _, payload in row}
    assert {f"cand:ok:{keep}", f"cand:no:{keep}", f"cand:ok:{drop}", f"cand:no:{drop}"} == payloads
    feed(bot, callback(f"cand:ok:{drop}", user=CLIENT))
    assert services.db.get_candidate(drop)["status"] == "new" and api.answers[-1]["notification"] == "Нет доступа."
    feed(bot, callback(f"cand:no:{drop}"))
    assert services.db.get_candidate(drop)["status"] == "rejected"
    feed(bot, callback(f"cand:ok:{keep}"))
    assert services.db.get_candidate(keep)["status"] == "new"
    assert "Обучении" in api.answers[-1]["notification"]
    assert not any("макет готовит наш дизайнер" in c.text for c in services.kb.chunks)
    row = services.db.get_candidate(keep)
    assert services.approve_candidates([keep], row["question"], row["answer"], scope="business")[0]
    assert services.db.get_candidate(keep)["status"] == "approved"
    assert any("макет готовит наш дизайнер" in c.text for c in services.kb.chunks), "бот уже отвечает по новому факту"
    feed(bot, callback(f"cand:ok:{keep}"))
    assert api.answers[-1]["notification"] == "Уже разобрано."

    with (services.settings.kb_dir / "prices.csv").open("a", encoding="utf-8") as prices:
        prices.write("Видеоэкран,ВЕЕР Молл,1 месяц,10000,\n")
    scoped = services.db.add_candidate(connection_id=CONN, chat_id=CLIENT_CHAT, dialog_id=None, source="card",
                                      client_question="Есть ли звук в ВЕЕР?", manager_text="В ВЕЕР экраны без звука.",
                                      question="Есть ли звук в ВЕЕР?", answer="В ВЕЕР экраны без звука.")
    feed(bot, callback(f"cand:ok:{scoped}"))
    assert services.db.get_candidate(scoped)["status"] == "approved"


def test_edited_message_while_waiting_is_answered_as_edited(env):
    services, bot, api = env
    connect(bot)
    asyncio.run(services.update_settings({"merge_seconds": "0.05"}))

    async def scenario():
        await bot.handle(message("Сколько стоит экран в атриуме?", mid="a"))
        edited = message("Какие требования к макету?", mid="a")["message"]
        await bot.handle({"update_type": "message_edited", "message": edited})
        await asyncio.sleep(0.2)
    asyncio.run(scenario())
    assert services.db.recent_dialogs(1)[0]["question"] == "Какие требования к макету?"


def test_launch_check_reports_max(monkeypatch, capsys):
    from app import check
    from app.channels import max_api

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": BOT, "username": "shop_bot"})
        return httpx.Response(200, json={"subscriptions": [{"url": "https://example.org/hook"}]})
    real = max_api.MaxApi.__init__
    monkeypatch.setattr(max_api.MaxApi, "__init__",
                        lambda self, token, base_url="", **kwargs:
                        real(self, token, base_url, httpx.MockTransport(handler), **kwargs))
    settings = replace(Settings.load(), max_bot_token="T")
    assert asyncio.run(check.check_max(settings)) is False, "вебхук мешает опросу"
    out = capsys.readouterr().out
    assert "shop_bot" in out and "example.org/hook" in out
    assert asyncio.run(check.check_max(replace(settings, max_bot_token=""))) is True, "без токена MAX не обязателен"


def test_greeting_in_drafts_is_answered_and_manager_is_not_bothered(env):
    services, bot, api = env
    connect(bot)
    before = len(to(api, MANAGER_CHAT))
    feed(bot, message("Здравствуйте"))
    assert to(api, CLIENT_CHAT)[-1]["text"] == services.assistant.greeting_message, "иначе клиент в полной тишине"
    assert len(to(api, MANAGER_CHAT)) == before


def test_caption_contact_location_and_slash_text_reach_manager(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("вот макет, подойдёт?", attachments=[{"type": "image", "payload": {}}], mid="photo"))
    sent = to(api, MANAGER_CHAT)
    assert any(m["forward"] == "photo" for m in sent) and "вот макет" in sent[-1]["text"], "и фото, и черновик по подписи"
    assert not services.db.is_paused(CONN, CLIENT_CHAT), "подпись — обычный вопрос, бот отвечает дальше"

    feed(bot, message("/ сколько стоит экран в атриуме?", mid="slash"))
    assert "сколько стоит экран" in to(api, MANAGER_CHAT)[-1]["text"], "текст со слэшем у клиента — не команда"
    feed(bot, message("/mode auto", mid="fake"))
    assert services.db.get_connection(CONN).mode == "draft", "клиент режим не меняет"

    feed(bot, message("", attachments=[{"type": "contact", "payload": {}}], mid="phone"))
    assert to(api, MANAGER_CHAT)[-2]["forward"] == "phone" and "Заявка" in to(api, MANAGER_CHAT)[-1]["text"], "номер не разобрали"
    assert not services.db.is_paused(CONN, CLIENT_CHAT), "заявка — не повод молчать на следующие вопросы"
    feed(bot, message("", attachments=[{"type": "location", "payload": {}}], mid="geo"))
    assert to(api, MANAGER_CHAT)[-2]["forward"] == "geo" and "геолокацию" in to(api, MANAGER_CHAT)[-1]["text"]
    assert services.db.is_paused(CONN, CLIENT_CHAT), "разговор забирает менеджер"


def test_observe_keeps_names_and_text_out_of_log_and_photo_with_caption_is_forwarded(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode observe", MANAGER, MANAGER_CHAT))
    services.events.clear()  # имя менеджера при подключении — можно, его данные это не клиентские
    feed(bot, message("секретный вопрос", attachments=[{"type": "image", "payload": {}}], mid="photo"))
    assert to(api, MANAGER_CHAT)[-2]["forward"] == "photo" and "«секретный вопрос»" in to(api, MANAGER_CHAT)[-1]["text"]
    assert not any("секретный" in text or "Иван" in text for _, text in services.events)


def test_reply_to_old_card_does_not_close_newer_question(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode observe", MANAGER, MANAGER_CHAT))
    feed(bot, message("вопрос один"))
    first = to(api, MANAGER_CHAT)[-1]["text"]
    feed(bot, reply_to(first, "ответ один"))
    feed(bot, message("вопрос два"))
    candidates = len(services.db.candidates())
    feed(bot, reply_to(first, "дополнение к первому"))
    rows = {r["question"]: r for r in services.db.recent_dialogs(5)}
    assert rows["вопрос один"]["answer"] == "ответ один\nдополнение к первому" and not rows["вопрос два"]["answer"]
    assert len(services.db.candidates()) == candidates, "нельзя связать дополнение с вопросом два"


def test_reply_from_admin_works_while_max_observes(env):
    services, bot, _ = env
    connect(bot)
    services.client_senders["max"] = lambda _conn, chat, text: bot.send(chat, text)
    services.db.set_mode("observe", connection_id=CONN)
    dialog = services.db.log_dialog(connection_id=CONN, chat_id=CLIENT_CHAT, question="скидка?", answer="",
                                    decision="escalate", reason="stop_topic:скид", confidence=0, status=D.ESCALATED,
                                    sources=[], latency_ms=0, mode="draft")
    assert asyncio.run(services.decide_draft(dialog, "edit", "Обсудим.")) == "sent"


def test_max_user_with_same_number_as_telegram_owner_is_not_approved(env):
    services, _, _ = env
    services.db.upsert_connection("tg-conn", 777, 1, True, True, "off")
    assert not services.db.upsert_connection(f"{CONN_PREFIX}777", 777, 2, True, True, "draft").approved
    services.db.approve_connection(f"{CONN_PREFIX}777")
    assert services.db.upsert_connection(f"{CONN_PREFIX}777", 777, 2, True, True, "draft").approved, "знакомый в MAX"


def test_run_max_survives_bad_answer_and_shows_lost_connection(env, monkeypatch):
    services, _, _ = env
    calls = []

    real_sleep = asyncio.sleep

    async def handler(request):
        await real_sleep(0)  # подмена транспорта не отдаёт управление сама, а настоящая сеть — отдаёт
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": 1, "first_name": "Бот"})
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"subscriptions": []})
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(200, text="<html>шлюз</html>")
        if len(calls) == 2:
            raise httpx.ConnectError("нет сети")
        return httpx.Response(200, json={"updates": [], "marker": 1})

    states = []

    async def fast(_):
        states.append(services.max_status["ok"])
        await real_sleep(0)

    monkeypatch.setattr("app.channels.max_bot.asyncio.sleep", fast)

    async def scenario():
        api = MaxApi("t", "http://x", transport=httpx.MockTransport(handler))
        task = asyncio.create_task(run_max(api, services))
        while len(calls) < 5:
            await real_sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await api.close()

    asyncio.run(scenario())
    assert False in states, "пока связи нет, админка это показывает"
    assert not services.max_status["ok"], "после остановки статус не должен оставаться хорошим"


FILE = {"type": "file", "payload": {"url": "https://x/1", "token": "tok1"}, "filename": "kp.pdf", "size": 1}


def test_manager_sends_file_to_client_from_the_bot(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Пришлите КП"))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    update = reply_to(card, "")
    update["message"]["body"]["attachments"] = [FILE]
    feed(bot, update)
    sent = to(api, CLIENT_CHAT)[-1]
    assert sent["files"] == [{"type": "file", "payload": {"token": "tok1"}}] and sent["forward"] is None, "от имени бота"
    assert services.db.recent_dialogs(1)[0]["answer"] == "[файл]"

    feed(bot, message("/mode observe", MANAGER, MANAGER_CHAT))
    feed(bot, message("А макет?", mid="q2"))
    update = reply_to(to(api, MANAGER_CHAT)[-1]["text"], "Вот пример")
    update["message"]["body"]["attachments"] = [FILE]
    update["message"]["body"]["mid"] = "m-file"
    feed(bot, update)
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Вот пример" and to(api, CLIENT_CHAT)[-1]["files"]


def test_media_falls_back_to_token_when_forward_is_refused(env):
    _, bot, api = env
    connect(bot)
    api.no_forward = True
    feed(bot, message("", attachments=[{"type": "image", "payload": {"url": "u", "token": "photo-token"}}], mid="p1"))
    assert any(m["files"] == [{"type": "image", "payload": {"token": "photo-token"}}] for m in to(api, MANAGER_CHAT))


def test_manager_reply_pauses_bot_in_auto_only(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Дадите скидку?", mid="d1"))
    feed(bot, reply_to(to(api, MANAGER_CHAT)[-1]["text"], "Обсудим."))
    assert not services.db.is_paused(CONN, CLIENT_CHAT), "в черновиках следующий вопрос и так идёт менеджеру"
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("А ещё скидку?", mid="d2"))
    card = to(api, MANAGER_CHAT)[-1]["text"]
    feed(bot, reply_to(card, "Обсудим, позвоню."))
    assert services.db.is_paused(CONN, CLIENT_CHAT), "менеджер взял разговор — бот не перебивает"
    answers = len(to(api, CLIENT_CHAT))
    feed(bot, message("Сколько стоит экран в атриуме?", mid="d3"))
    assert len(to(api, CLIENT_CHAT)) == answers and "чат на паузе" in to(api, MANAGER_CHAT)[-1]["text"]


def test_repeated_events_do_not_answer_twice(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT, mid="cmd"))
    update = message("Сколько стоит экран в атриуме?", mid="same")
    feed(bot, update)
    feed(bot, update)
    assert len(to(api, CLIENT_CHAT)) == 1 and len(services.db.recent_dialogs(5)) == 1


def test_start_without_manager_is_told(env):
    _, bot, api = env
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}})
    assert to(api, CLIENT_CHAT)[-1]["text"] == NO_MANAGER


def admin_client(services):
    from tests.admin_client import AdminClient as TestClient

    from app.admin import create_app
    return TestClient(create_app(services))


def test_webhook_endpoint_accepts_only_secret_and_queues(env):
    services, _, _ = env
    client = admin_client(services)
    url = "/max/webhook"
    assert client.post(url, json={}).status_code == 404, "секрет не настроен — как будто адреса нет"
    services.settings = replace(services.settings, max_webhook_secret="s3cret")
    assert client.post(url, json={}, headers={"X-Max-Bot-Api-Secret": "wrong"}).status_code == 404
    good = {"X-Max-Bot-Api-Secret": "s3cret"}
    assert client.post(url, json={"update_type": "x"}, headers=good).status_code == 503, "бот не готов — MAX повторит"
    services.max_updates = asyncio.Queue(maxsize=1)
    assert client.post(url, content="не json", headers=good).status_code == 400
    assert client.post(url, json=[1], headers=good).status_code == 400
    assert client.post(url, json={"update_type": "x"}, headers=good).status_code == 200
    assert services.max_updates.get_nowait() == {"update_type": "x"}
    client.post(url, json={"a": 1}, headers=good)
    assert client.post(url, json={"a": 2}, headers=good).status_code == 503, "очередь полна — не теряем, MAX повторит"
    assert client.post(url, content="x" * 2_000_001, headers=good).status_code == 413
    assert client.get(url).status_code == 405


def test_run_webhook_subscribes_replaces_foreign_and_restores_lost(env, monkeypatch):
    services, bot, _ = env
    services.settings = replace(services.settings, max_webhook_url="https://bot.test/max/webhook", max_webhook_secret="s")
    hooks = [{"url": "https://old.test"}]
    calls = []

    class Api:
        async def subscriptions(self):
            return list(hooks)

        async def subscribe(self, url, secret, types):
            calls.append(("sub", url, secret, "message_callback" in types))
            hooks.append({"url": url})

        async def unsubscribe(self, url):
            calls.append(("del", url))
            hooks[:] = [h for h in hooks if h["url"] != url]

    real_sleep = asyncio.sleep
    rounds = []

    async def fake_sleep(_):
        rounds.append(1)
        if len(rounds) == 1:
            hooks.clear()  # MAX снял вебхук, пока сервер был недоступен
        if len(rounds) == 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("app.channels.max_bot.asyncio.sleep", fake_sleep)
    ready = {"ok": True, "username": "", "name": ""}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_webhook(Api(), bot, services, ready))
    assert calls == [("del", "https://old.test"), ("sub", "https://bot.test/max/webhook", "s", True),
                     ("sub", "https://bot.test/max/webhook", "s", True)]
    assert not services.max_status["ok"] and services.max_updates is None


def test_run_webhook_refuses_insecure_settings(env):
    services, bot, _ = env
    services.settings = replace(services.settings, max_webhook_url="http://bot.test/x", max_webhook_secret="s")
    asyncio.run(run_webhook(None, bot, services, {"ok": True}))
    assert not services.max_status["ok"] and "https" in services.max_status["error"]


def test_queue_worker_survives_broken_event(env):
    services, _, _ = env
    seen = []

    class Bot:
        async def handle(self, update):
            seen.append(update)
            if update.get("bad"):
                raise ValueError("сломанное событие")

    async def scenario():
        queue = asyncio.Queue()
        worker = asyncio.create_task(handle_queue(Bot(), queue))
        queue.put_nowait({"bad": True})
        queue.put_nowait({"ok": True})
        await asyncio.sleep(0.05)
        worker.cancel()

    asyncio.run(scenario())
    assert len(seen) == 2


def test_admin_shows_client_link(env):
    services, _, _ = env
    services.settings = replace(services.settings, max_bot_token="t", admin_token="adm")
    services.max_status = {"ok": True, "username": "reklama_bot", "name": "Реклама"}
    client = admin_client(services)
    client.cookies.set("admin_token", "adm")
    assert "max.ru/reklama_bot" in client.get("/").text


def test_polling_removes_leftover_webhook(env):
    services, _, _ = env
    removed = []

    class Api(FakeApi):
        async def me(self):
            return {"user_id": BOT}

        async def subscriptions(self):
            return [{"url": "https://old.test/max/webhook"}]

        async def unsubscribe(self, url):
            removed.append(url)

        async def updates(self, marker=None):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_max(Api(), services))
    assert removed == ["https://old.test/max/webhook"], "иначе MAX не отдаёт события опросом, и бот молчит"


def press(bot, payload, user=CLIENT, chat=CLIENT_CHAT):
    feed(bot, {"update_type": "message_callback",
               "callback": {"callback_id": f"cb{next(SEQ)}", "payload": payload,
                            "user": {"user_id": user, "first_name": "Иван", "last_name": "Петров", "username": "ivan"}},
               "message": {"recipient": {"chat_id": chat, "chat_type": "dialog"}, "body": {"mid": "x", "seq": 1, "text": "меню"}}})


def consent_ready(services, short="Чтобы передать заявку, нужен ваш контакт. Оператор: ИП Тест, Екатеринбург."):
    """Текст согласия заполнен юристом — без плейсхолдеров; бот просит контакт только после «Согласен»."""
    return services.consents.save_text(short, "Я разрешаю ИП Тест обрабатывать мой контакт для ответа на заявку.")


def agree(bot, services):
    consent_ready(services)
    press(bot, "menu:lead")
    press(bot, "consent:yes")


def contact(phone="+7 999 000-11-22", mid=None):
    vcf = f"BEGIN:VCARD\nVERSION:3.0\nFN:Иван\nTEL;TYPE=cell:{phone}\nEND:VCARD"
    return message("", attachments=[{"type": "contact", "payload": {"vcf_info": vcf, "max_info": {}, "hash": "h"}}], mid=mid)


def test_menu_on_start_and_greeting_but_not_in_observe(env):
    services, bot, api = env
    connect(bot)
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}})
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CLIENT_MENU
    feed(bot, message("Здравствуйте"))
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CLIENT_MENU
    feed(bot, message("/start"))
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CLIENT_MENU
    feed(bot, message("/mode observe", MANAGER, MANAGER_CHAT))
    before = len(to(api, CLIENT_CHAT))
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}})
    assert len(to(api, CLIENT_CHAT)) == before, "в наблюдении бот не здоровается сам"
    before = len(api.sent)
    press(bot, "menu:catalog")
    assert api.answers[-1]["notification"] == OBSERVE_MENU and len(api.sent) == before, "старая кнопка в наблюдении"



def test_catalog_button_needs_no_model_and_works_in_drafts(env, monkeypatch):
    services, bot, api = env
    connect(bot)

    async def no_model(*_):
        raise AssertionError("кнопка каталога не должна звать модель")

    monkeypatch.setattr(services.assistant.llm, "complete", no_model)
    press(bot, "menu:catalog")
    sent = to(api, CLIENT_CHAT)[-1]
    assert sent["buttons"] == AFTER_ANSWER and sent["text"].startswith("Мы размещаем рекламу") and api.answers[-1]["notification"]
    assert services.db.recent_dialogs(5) == [], "это не вопрос клиента"


def test_catalog_button_on_real_knowledge_base(tmp_path, monkeypatch):
    """Учебная база показывает каталог почти на любую фразу, поэтому проверяем и на настоящей."""
    shutil.copytree(ROOT / "data" / "kb", tmp_path / "kb", ignore=shutil.ignore_patterns(".obsidian"))
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=tmp_path / "kb", llm_provider="mock", exam_files="")
    services = Services(settings)
    api = FakeApi()
    bot = MaxBot(api, services, BOT)
    connect(bot)
    press(bot, "menu:catalog")
    text = to(api, CLIENT_CHAT)[-1]["text"]
    assert text.startswith("Мы размещаем рекламу") and "ВЕЕР Молл" in text


def test_budget_button_turns_next_number_into_budget(env):
    services, bot, api = env
    connect(bot)
    press(bot, "menu:budget")
    assert to(api, CLIENT_CHAT)[-1]["text"] == BUDGET_ASK
    feed(bot, message("50 тысяч"))
    assert services.db.recent_dialogs(1)[0]["question"] == "Бюджет 50 тысяч"
    feed(bot, message("70 тысяч"))
    assert services.db.recent_dialogs(1)[0]["question"] == "70 тысяч", "только сразу после кнопки"


def test_contact_button_makes_lead_card_with_context_and_no_phone_in_journal(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("Сколько стоит экран в атриуме?"))
    agree(bot, services)
    assert to(api, CLIENT_CHAT)[-1]["text"] == LEAD_ASK and to(api, CLIENT_CHAT)[-1]["buttons"] == ASK_CONTACT
    feed(bot, contact())
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert card.startswith("Заявка: Иван Петров") and "Телефон: +7 999 000-11-22" in card and "Вопрос #" in card
    assert "экран" in card and "Сколько стоит экран в атриуме?" in card, "не только номер, но и о чём речь"
    assert to(api, CLIENT_CHAT)[-1]["text"] == LEAD_DONE
    row = services.db.recent_dialogs(1)[0]
    assert (row["question"], row["reason"], row["status"]) == ("[заявка]", "lead", D.ESCALATED)
    assert "999" not in str(dict(row)) and not any("999" in t for _, t in services.events), "телефона нет ни в журнале, ни в логе"
    assert row["id"] not in [r["id"] for r in services.db.to_learn()]
    feed(bot, reply_to(card, "Спасибо, позвоню сегодня."))
    assert to(api, CLIENT_CHAT)[-1]["text"] == "Спасибо, позвоню сегодня."


def test_phone_by_text_after_lead_button_and_not_otherwise(env):
    services, bot, api = env
    connect(bot)
    agree(bot, services)
    feed(bot, message("мой номер 8 912 345-67-89"))
    assert "Телефон: 8 912 345-67-89" in to(api, MANAGER_CHAT)[-1]["text"]
    assert services.db.recent_dialogs(1)[0]["question"] == "[заявка]\nмой номер [телефон]"
    press(bot, "menu:lead")
    feed(bot, message("а сначала скажите, сколько стоит экран в атриуме?"))
    assert "Заявка" not in to(api, MANAGER_CHAT)[-1]["text"], "вопрос вместо телефона — обычный вопрос"


def test_lead_failure_is_honest(env):
    _, bot, api = env
    connect(bot)
    real = api.send

    async def manager_down(text, *, chat_id=None, **kw):
        if chat_id == MANAGER_CHAT:
            raise MaxError(503, "down")
        return await real(text, chat_id=chat_id, **kw)

    api.send = manager_down
    feed(bot, contact())
    assert to(api, CLIENT_CHAT)[-1]["text"] == NOT_DELIVERED, "нельзя сказать «передал», если не передали"


def test_call_human_by_button_and_by_words(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("Сколько стоит экран в атриуме?"))
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == AFTER_ANSWER, "под ответом бота есть выход к человеку"
    before = len(to(api, MANAGER_CHAT))
    press(bot, "menu:human")
    assert to(api, CLIENT_CHAT)[-1]["text"] == HUMAN_DONE and services.db.is_paused(CONN, CLIENT_CHAT)
    assert "просит менеджера" in to(api, MANAGER_CHAT)[-1]["text"] and len(to(api, MANAGER_CHAT)) == before + 1
    press(bot, "menu:human")
    assert to(api, CLIENT_CHAT)[-1]["text"] == HUMAN_AGAIN and len(to(api, MANAGER_CHAT)) == before + 1, "не дёргаем дважды"

    services.db.resume_chats(CONN)
    bot.called.clear()
    for text in ("позовите менеджера", "соедините с человеком", "Хочу поговорить с оператором"):
        bot.called.clear()
        services.db.resume_chats(CONN)
        feed(bot, message(text))
        assert to(api, CLIENT_CHAT)[-1]["text"] == HUMAN_DONE, text
    services.db.resume_chats(CONN)
    feed(bot, message("а ваш менеджер обещал скидку"))
    assert "просит менеджера" not in to(api, MANAGER_CHAT)[-1]["text"], "упоминание менеджера — ещё не просьба позвать"


def test_ready_to_buy_asks_for_contact(env):
    services, bot, api = env
    connect(bot)
    consent_ready(services)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    feed(bot, message("Хочу на месяц экран в атриуме, берём"))
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CONSENT_BUTTONS, "сначала согласие, потом кнопка контакта"
    press(bot, "consent:yes")
    assert to(api, CLIENT_CHAT)[-1]["text"] == LEAD_ASK and to(api, CLIENT_CHAT)[-1]["buttons"] == ASK_CONTACT
    feed(bot, contact())
    assert to(api, MANAGER_CHAT)[-1]["text"].startswith("Заявка:")


def test_menu_for_stranger_manager_and_no_manager(env):
    services, bot, api = env
    press(bot, "menu:human")
    assert api.answers[-1]["notification"] == NO_MANAGER
    connect(bot)
    press(bot, "menu:human", user=MANAGER, chat=MANAGER_CHAT)
    assert api.answers[-1]["notification"] == "Это кнопка для клиентов."
    press(bot, "menu:unknown")
    assert api.answers[-1]["notification"] == "Кнопка устарела."


def test_source_from_link_is_kept_first_and_counted(env):
    services, bot, _ = env
    connect(bot)
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}, "payload": "card"})
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}, "payload": "site"})
    feed(bot, contact())
    feed(bot, {"update_type": "bot_started", "chat_id": 7777, "user": {"user_id": 777}, "payload": "bad payload!"})
    feed(bot, message("Сколько стоит экран в атриуме?", user=888, chat=8880))
    stats = services.db.source_stats()
    assert stats["card"] == {"clients": 1, "leads": 1} and "site" not in stats and "bad payload!" not in stats
    assert stats[""]["clients"] == 1, "написал без ссылки"
    feed(bot, message("/start site", user=999, chat=9990))
    assert services.db.source_stats()["site"]["clients"] == 1


def test_several_files_go_one_by_one(env):
    _, bot, api = env
    connect(bot)
    feed(bot, message("Пришлите КП"))
    update = reply_to(to(api, MANAGER_CHAT)[-1]["text"], "Держите")
    second = {"type": "file", "payload": {"url": "u", "token": "tok2"}}
    update["message"]["body"]["attachments"] = [FILE, second]
    feed(bot, update)
    sent = to(api, CLIENT_CHAT)[-2:]
    assert sent[0]["text"] == "Держите" and sent[0]["files"] == [{"type": "file", "payload": {"token": "tok1"}}]
    assert sent[1]["files"] == [{"type": "file", "payload": {"token": "tok2"}}], "MAX: файл — единственное вложение"


def test_keyboard_has_contact_button():
    assert keyboard([[("Контакт", "\0contact"), ("Меню", "menu:x")]])["payload"]["buttons"] == [[
        {"type": "request_contact", "text": "Контакт"}, {"type": "callback", "text": "Меню", "payload": "menu:x"}]]


def test_invite_page_links_qr_stats_and_new_source(env):
    services, bot, _ = env
    services.settings = replace(services.settings, max_bot_token="t", admin_token="adm")
    client = admin_client(services)
    client.cookies.set("admin_token", "adm")
    assert "Ссылки и QR-коды появятся" in client.get("/invite").text
    assert client.get("/invite/qr/site.svg").status_code == 404
    services.max_status = {"ok": True, "username": "reklama_bot", "name": "Реклама"}
    connect(bot)
    feed(bot, {"update_type": "bot_started", "chat_id": CLIENT_CHAT, "user": {"user_id": CLIENT}, "payload": "card"})
    feed(bot, contact())
    page = client.get("/invite").text
    assert 'href="/invite"' in page, "раздел в меню, когда MAX подключён"
    assert "max.ru/reklama_bot?start=site" in page and "max.ru/reklama_bot?start=card" in page
    assert "1 клиент · 1 заявка" in page and "max.ru/reklama_bot?start=old" in page.split('id="old-text"')[1]
    svg = client.get("/invite/qr/card.svg")
    assert svg.headers["content-type"].startswith("image/svg") and b"<svg" in svg.content
    assert "attachment" in client.get("/invite/qr/card.svg?download=1").headers["content-disposition"]
    assert client.get("/invite/qr/nope.svg").status_code == 404
    client.post("/invite/source", data={"title": "  Листовка <b>в ТЦ</b> "})
    client.post("/invite/source", data={"title": "листовка <b>в тц</b>"})
    page = client.get("/invite").text
    assert "start=s1" in page and "start=s2" not in page and "&lt;b&gt;" in page and "<b>в ТЦ" not in page
    assert client.get("/invite/qr/s1.svg").status_code == 200
    services.settings = replace(services.settings, max_bot_token="")
    assert 'href="/invite"' not in client.get("/").text, "без MAX раздела в меню нет"
    client.cookies.clear()
    assert client.get("/invite", follow_redirects=False).status_code != 200, "только после входа"



@pytest.mark.parametrize('mode', ['draft', 'observe'])
def test_regression_long_and_forwarded_client_text_reaches_manager(env, mode):
    services, bot, api = env
    connect(bot)
    services.db.set_mode(mode, connection_id=CONN)
    text = 'Нужно размещение. ' * 145 + 'ВАЖНО: запуск после согласования'
    feed(bot, message(text))
    assert text in '\n'.join(m['text'] for m in to(api, MANAGER_CHAT))
    feed(bot, message('', link={'type': 'forward', 'message': {'text': 'Пересланное ТЗ'}}))
    assert 'Пересланное ТЗ' in to(api, MANAGER_CHAT)[-1]['text']


def test_regression_unapproved_manager_can_still_be_client(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message('/manager'))
    assert not services.db.get_connection(f'max:{CLIENT}').approved
    feed(bot, message('Сколько стоит экран в атриуме?'))
    assert 'атриуме' in to(api, MANAGER_CHAT)[-1]['text']
    press(bot, 'menu:catalog')
    assert api.answers[-1]['notification'] != 'Это кнопка для клиентов.'


def test_regression_notice_number_cannot_route_to_another_client(env):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    feed(bot, message('Дадите скидку?', user=666, chat=6660))
    wrong_id = services.db.recent_dialogs(1)[0]['id']
    before = len(to(api, 6660))
    feed(bot, message(f'Берём экран на месяц. Вопрос #{wrong_id}'))
    notice = next(m['text'] for m in reversed(to(api, MANAGER_CHAT)) if 'готов к заказу' in m['text'])
    feed(bot, reply_to(notice, 'Отвечаю на заявку'))
    assert to(api, CLIENT_CHAT)[-1]['text'] == 'Отвечаю на заявку'
    assert len(to(api, 6660)) == before
    feed(bot, reply_to(f'Текст клиента: Вопрос #{wrong_id}', 'Подложная карточка'))
    assert len(to(api, 6660)) == before


def test_regression_lead_on_pause_keeps_full_contact_message(env):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    press(bot, 'menu:human')
    agree(bot, services)
    text = '+7 999 123-45-67, звонить после 18, спросить Ольгу'
    feed(bot, message(text))
    card = to(api, MANAGER_CHAT)[-1]['text']
    assert text in card
    assert services.db.recent_dialogs(1)[0]['reason'] == 'lead'
    feed(bot, reply_to(card, 'Первый ответ'))
    feed(bot, reply_to(card, 'Второй ответ'))
    assert [m['text'] for m in to(api, CLIENT_CHAT)][-2:] == ['Первый ответ', 'Второй ответ']


def test_regression_failed_auto_answer_card_has_own_number(env):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    original = api.send

    async def fail_client(text, *, chat_id=None, **kwargs):
        if chat_id == CLIENT_CHAT:
            raise MaxError(503, 'down')
        return await original(text, chat_id=chat_id, **kwargs)

    api.send = fail_client
    feed(bot, message('Сколько стоит экран в атриуме?'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    api.send = original
    feed(bot, reply_to(card, 'Мой ответ'))
    assert to(api, CLIENT_CHAT)[-1]['text'] == 'Мой ответ'


def test_first_max_manager_also_requires_admin_approval(env):
    services, bot, api = env
    feed(bot, message('/manager', MANAGER, MANAGER_CHAT))
    assert not services.db.get_connection(CONN).approved
    feed(bot, message('Вопрос другого клиента'))
    assert not any('другого клиента' in m['text'] for m in to(api, MANAGER_CHAT))


def test_followup_to_card_after_send_button_keeps_recipient(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message('Какие требования к макету?'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    dialog_id = services.db.recent_dialogs(1)[0]['id']
    feed(bot, callback(f'send:{dialog_id}', card))
    feed(bot, reply_to(api.answers[-1]['text'], 'И ещё одно уточнение'))
    assert to(api, CLIENT_CHAT)[-1]['text'] == 'И ещё одно уточнение'


def test_partial_files_retry_only_unsent_attachment(env):
    _, bot, api = env
    connect(bot)
    feed(bot, message('Пришлите КП'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    original = api.send
    attempts = 0

    async def flaky(text, *, chat_id=None, **kwargs):
        nonlocal attempts
        if chat_id == CLIENT_CHAT:
            attempts += 1
            if attempts == 2:
                raise MaxError(503, 'temporary')
        return await original(text, chat_id=chat_id, **kwargs)

    api.send = flaky
    files = [{'type': 'file', 'payload': {'token': token}} for token in ('first', 'second')]
    for _ in range(2):
        update = reply_to(card, 'Документы')
        update['message']['body']['attachments'] = files
        feed(bot, update)
    sent = to(api, CLIENT_CHAT)
    assert [m['text'] for m in sent] == ['Документы', '']
    assert [m['files'][0]['payload']['token'] for m in sent] == ['first', 'second']


def test_rate_limit_preserves_text_without_spamming_manager(env):
    services, bot, api = env
    connect(bot)
    asyncio.run(services.update_settings({'chat_hourly_limit': '3'}))
    for i in range(4):
        feed(bot, message(f'Непонятный вопрос {i}'))
    count = len(to(api, MANAGER_CHAT))
    for i in range(50):
        feed(bot, message(f'Ещё сообщение {i}'))
    assert len(to(api, MANAGER_CHAT)) - count <= 5
    assert services.db.recent_dialogs(1)[0]['question'] == 'Ещё сообщение 49'


def test_caption_album_has_no_second_holding(env):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    for i in range(3):
        feed(bot, message('Сколько стоит экран в атриуме?' if i == 0 else '',
                          attachments=[{'type': 'image', 'payload': {'token': str(i)}}]))
    assert len(to(api, CLIENT_CHAT)) == 1
    assert sum(bool(m['forward']) for m in to(api, MANAGER_CHAT)) == 3


# --- согласие на обработку контакта ---

def test_without_filled_consent_bot_never_asks_for_contact(env):
    # Черновик согласия с [плейсхолдерами] клиентам не показываем: заявка без телефона, ответ — в этом чате
    services, bot, api = env
    connect(bot)
    assert not services.consents.text().ready
    press(bot, "menu:lead")
    assert to(api, CLIENT_CHAT)[-1]["text"] == LEAD_IN_CHAT
    assert all(m.get("buttons") != ASK_CONTACT for m in to(api, CLIENT_CHAT))
    assert to(api, MANAGER_CHAT)[-1]["text"].startswith("Заявка: Иван Петров") and "Телефон" not in to(api, MANAGER_CHAT)[-1]["text"]
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    before = len(to(api, MANAGER_CHAT))
    feed(bot, message("Хочу на месяц экран в атриуме, берём"))
    assert all(m.get("buttons") not in (ASK_CONTACT, CONSENT_BUTTONS) for m in to(api, CLIENT_CHAT))
    assert len(to(api, MANAGER_CHAT)) == before + 1, "готовность к заказу — один сигнал менеджеру, без второй заявки"


def test_consent_step_text_refusal_and_new_version(env):
    services, bot, api = env
    connect(bot)
    text = consent_ready(services)
    press(bot, "menu:lead")
    shown = to(api, CLIENT_CHAT)[-1]
    assert text.short in shown["text"] and shown["buttons"] == CONSENT_BUTTONS
    press(bot, "consent:text")
    assert to(api, CLIENT_CHAT)[-2]["text"] == text.full and to(api, CLIENT_CHAT)[-1]["buttons"] == CONSENT_BUTTONS
    press(bot, "consent:no")
    assert to(api, CLIENT_CHAT)[-1]["text"] == CONSENT_NO
    assert to(api, MANAGER_CHAT)[-1]["text"].startswith("Заявка:") and "Телефон" not in to(api, MANAGER_CHAT)[-1]["text"]
    press(bot, "menu:lead")
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CONSENT_BUTTONS, "отказ не согласие: спрашиваем снова"
    press(bot, "consent:yes")
    assert services.consents.given(CLIENT_CHAT, text.version)
    new = consent_ready(services, short="Чтобы передать заявку, нужен ваш контакт. Оператор: ИП Тест, г. Екатеринбург.")
    assert new.version != text.version and not services.consents.given(CLIENT_CHAT, new.version)
    press(bot, "menu:lead")
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CONSENT_BUTTONS, "текст поменялся — нужно новое согласие"
    rows = services.db.conn.execute("SELECT * FROM consent_log").fetchall()
    assert [r["decision"] for r in rows] == ["no", "yes"] and all("Иван" not in str(dict(r)) for r in rows)


def test_revoking_consent_tells_manager_what_to_delete(env):
    services, bot, api = env
    connect(bot)
    agree(bot, services)
    feed(bot, message("Отзываю согласие на обработку данных"))
    assert to(api, CLIENT_CHAT)[-1]["text"] == REVOKED_DONE
    assert "отозвал(а) согласие" in to(api, MANAGER_CHAT)[-1]["text"] and str(CLIENT_CHAT) in to(api, MANAGER_CHAT)[-1]["text"]
    assert not services.consents.given(CLIENT_CHAT, services.consents.text().version)
    press(bot, "menu:lead")
    assert to(api, CLIENT_CHAT)[-1]["buttons"] == CONSENT_BUTTONS
    press(bot, "consent:yes", user=MANAGER, chat=MANAGER_CHAT)
    assert api.answers[-1]["notification"] == "Это кнопка для клиентов."


def test_admin_shows_consent_state_and_saves_text(env):
    services, _, _ = env
    services.settings = services.settings.__class__(**{**vars(services.settings), "admin_token": "test-secret"})
    client = TestClient(create_app(services))
    client.get("/?token=test-secret")
    page = client.get("/invite").text
    assert "Согласие на обработку данных" in page and "бот не просит контакт" in page
    client.post("/invite/consent", data={"short": "Нужен ваш контакт. Оператор: ИП Тест.", "full": "Я разрешаю ИП Тест…"})
    text = services.consents.text()
    assert text.ready and text.version in client.get("/invite").text



def test_human_card_in_auto_says_bot_is_silent_until_time(env):
    services, bot, api = env
    connect(bot)
    feed(bot, message("/mode auto", MANAGER, MANAGER_CHAT))
    press(bot, "menu:human")
    card = to(api, MANAGER_CHAT)[-1]["text"]
    assert "Бот молчит в этом чате до" in card and "/resume" in card and "«Вернуть бота»" in card
    assert services.db.is_paused(CONN, CLIENT_CHAT)


def test_human_card_in_drafts_has_no_pause_line(env):
    services, bot, api = env
    connect(bot)
    press(bot, "menu:human")
    assert "Бот молчит" not in to(api, MANAGER_CHAT)[-1]["text"]
    assert not services.db.is_paused(CONN, CLIENT_CHAT)
