"""Адаптер MAX без сети: транспорт подменяем, события подаём словарями в формате MAX Bot API."""
import asyncio
import itertools
import json
import shutil
import time
from dataclasses import replace

import httpx
import pytest

from app import db as D
from app.channels.max_api import MaxApi, MaxError, keyboard
from app.channels.max_bot import CONN_PREFIX, HELP, NO_MANAGER, MaxBot, run_max
from app.config import ROOT, Settings
from app.services import Services

BOT, MANAGER, MANAGER_CHAT, CLIENT, CLIENT_CHAT = 1, 100, 1000, 555, 5550
CONN = f"{CONN_PREFIX}{MANAGER}"
SEQ = itertools.count(1)  # у MAX номер сообщения в чате растёт по порядку


class FakeApi:
    """Тот же интерфейс, что у MaxApi: запоминает, что бот отправил."""
    def __init__(self):
        self.sent, self.answers, self.fail = [], [], False

    async def send(self, text, *, chat_id=None, user_id=None, buttons=None, forward=None):
        if self.fail:
            raise MaxError(503, "service.unavailable")
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "forward": forward})
        seq = 10_000 + len(self.sent)  # как у MAX: номер сообщения растёт по порядку
        return {"message": {"body": {"mid": f"bot{seq}", "seq": seq, "text": text},
                            "timestamp": int(time.time() * 1000)}}

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
            is_bot=False, mid="m1"):
    msg = {"sender": {"user_id": user, "first_name": "Иван", "last_name": "Петров", "username": "ivan",
                      "is_bot": is_bot},
           "recipient": {"chat_id": chat, "chat_type": chat_type, "user_id": None},
           "timestamp": int((time.time() - age_s) * 1000),
           "body": {"mid": mid, "seq": next(SEQ), "text": text, "attachments": attachments}}
    if link:
        msg["link"] = link
    return {"update_type": "message_created", "timestamp": msg["timestamp"], "message": msg}


def callback(payload, card="", user=MANAGER, callback_id="cb1"):
    return {"update_type": "message_callback", "callback": {"callback_id": callback_id, "payload": payload,
                                                             "user": {"user_id": user}},
            "message": {"body": {"mid": "card", "seq": 1, "text": card}}}


def reply_to(card, text):
    return message(text, MANAGER, MANAGER_CHAT, link={"type": "reply", "message": {"mid": "card", "seq": 1, "text": card}})


def connect(bot):
    feed(bot, message("/manager", MANAGER, MANAGER_CHAT))


def to(api, chat):
    return [m for m in api.sent if m["chat_id"] == chat]


def test_first_manager_is_approved_and_stranger_waits(env):
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
    assert answer["notification"] == "Отправлено клиенту." and answer["text"].endswith("Отправлено клиенту.")
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
    assert to(api, MANAGER_CHAT)[-1]["text"] == "Этот черновик уже обработан."


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
    from fastapi.testclient import TestClient
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
    assert services.db.get_candidate(keep)["status"] == "approved"
    assert any("макет готовит наш дизайнер" in c.text for c in services.kb.chunks), "бот уже отвечает по новому факту"
    feed(bot, callback(f"cand:ok:{keep}"))
    assert api.answers[-1]["notification"] == "Уже разобрано."


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
                        lambda self, token, base_url="": real(self, token, base_url, httpx.MockTransport(handler)))
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
    assert to(api, MANAGER_CHAT)[-2]["forward"] == "phone" and "контакт" in to(api, MANAGER_CHAT)[-1]["text"]
    assert services.db.is_paused(CONN, CLIENT_CHAT), "разговор забирает менеджер"
    feed(bot, message("", attachments=[{"type": "location", "payload": {}}], mid="geo"))
    assert to(api, MANAGER_CHAT)[-1]["forward"] == "geo"


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
        calls.append(request.url.path)
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": 1, "first_name": "Бот"})
        if len(calls) == 2:
            return httpx.Response(200, text="<html>шлюз</html>")
        if len(calls) == 3:
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
    assert services.max_status["ok"], "связь вернулась — статус снова хороший"
