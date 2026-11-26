"""Сценарии адаптера Telegram Business без сети: подменяем отправку сообщений."""
import asyncio
import datetime as dt
from dataclasses import replace

import pytest
from aiogram import Bot
from aiogram.types import Update

from app import db as D
from app.channels.telegram_business import build_dispatcher
from app.config import ROOT, Settings
from app.services import Services
from tests.test_core import FakeLLM

OWNER, CLIENT, CONN = 100, 555, "bc-test"
NOW = int(dt.datetime.now().timestamp())


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


@pytest.fixture
def env(tmp_path):
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=ROOT / "tests" / "fixtures" / "kb", llm_provider="mock")
    services = Services(settings)
    asyncio.run(services.update_settings({"merge_seconds": "0"}))  # склейку проверяем отдельно
    dp = build_dispatcher(services)
    bot = Bot("42:TEST")
    rec = Recorder()
    bot.send_message = rec  # type: ignore[method-assign]

    async def fake_request(bot, method, timeout=None):  # остальные вызовы API (answer, edit_text)
        return True
    bot.session.make_request = fake_request  # type: ignore[method-assign]
    return services, dp, bot, rec


def feed(dp, bot, payload):
    asyncio.run(dp.feed_update(bot, Update.model_validate({"update_id": 1, **payload}, context={"bot": bot})))


def connect(dp, bot):
    feed(dp, bot, {"business_connection": {
        "id": CONN, "user": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
        "user_chat_id": OWNER, "date": NOW, "is_enabled": True,
        "rights": {"can_reply": True},
    }})


def client_message(text, from_id=CLIENT):
    return {"business_message": {
        "message_id": 1, "date": NOW, "business_connection_id": CONN, "text": text,
        "chat": {"id": CLIENT, "type": "private", "first_name": "Client"},
        "from": {"id": from_id, "is_bot": False, "first_name": "X"},
    }}


def test_update_types_include_business(env):
    _, dp, _, _ = env
    types = dp.resolve_used_update_types()
    assert {"business_connection", "business_message", "callback_query"} <= set(types)


def test_connection_saved_and_manager_notified(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    assert services.db.get_connection(CONN).mode == "draft"
    assert rec.sent[-1]["chat_id"] == OWNER and "подключён" in rec.sent[-1]["text"]


def test_draft_mode_sends_card_to_manager(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    feed(dp, bot, client_message("Какие требования к макету?"))
    card = rec.sent[-1]
    assert card["chat_id"] == OWNER and "Черновик #" in card["text"] and card.get("reply_markup")
    assert all(m.get("business_connection_id") is None for m in rec.sent)  # клиенту ничего


def test_auto_mode_replies_to_client(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    services.db.set_mode("auto", connection_id=CONN)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    msg = rec.sent[-1]
    assert msg["chat_id"] == CLIENT and msg["business_connection_id"] == CONN and "30 000" in msg["text"]


def test_manager_message_pauses_bot(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    services.db.set_mode("auto", connection_id=CONN)
    before = len(rec.sent)
    feed(dp, bot, client_message("Я сам отвечу", from_id=OWNER))
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert len(rec.sent) == before  # бот молчит
    assert services.db.stats()["total"] == 0


def test_send_draft_via_button(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    feed(dp, bot, client_message("Какие требования к макету?"))
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    button = rec.sent[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"send:{dialog_id}"
    feed(dp, bot, {"callback_query": {
        "id": "cb1", "chat_instance": "x", "data": button.callback_data,
        "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
    }})
    msg = [m for m in rec.sent if m.get("business_connection_id")][-1]
    assert msg["chat_id"] == CLIENT and "PDF" in msg["text"]
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_SENT


@pytest.mark.parametrize("mode", ["auto", "draft"])
def test_rejected_price_draft_requires_manager_reply(env, mode):
    services, dp, bot, rec = env
    connect(dp, bot)
    services.db.set_mode(mode, connection_id=CONN)
    services.assistant.llm = FakeLLM("Лайтбокс стоит 20 000 ₽.")
    feed(dp, bot, client_message("Сколько стоит лайтбокс у главного входа?"))
    row = services.db.recent_dialogs(1)[0]
    assert row["reason"].startswith("price_mismatch")
    card = rec.sent[-1]
    before = list(to_client(rec))
    expected = [services.assistant.holding_message] if mode == "auto" else []
    assert [msg["text"] for msg in before] == expected
    feed(dp, bot, {"callback_query": {
        "id": "old", "chat_instance": "x", "data": f"send:{row['id']}",
        "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
    }})
    assert to_client(rec) == before
    assert services.db.get_dialog(row["id"])["status"] == row["status"]
    assert [[b.callback_data for b in line] for line in card["reply_markup"].inline_keyboard] == [[f"skip:{row['id']}"]]
    assert "20 000" in card["text"] and "исправ" in card["text"].lower() and "Нажмите «Отправить»" not in card["text"]
    feed(dp, bot, manager_reply(card["text"], row["answer"]))
    assert to_client(rec) == before
    feed(dp, bot, manager_reply(card["text"], "Лайтбокс стоит 25 000 ₽ за месяц."))
    assert to_client(rec)[-1]["text"] == "Лайтбокс стоит 25 000 ₽ за месяц."
    assert services.db.get_dialog(row["id"])["status"] == D.DRAFT_EDITED


def auto_env(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    services.db.set_mode("auto", connection_id=CONN)
    return services, dp, bot, rec


def to_client(rec):
    return [m for m in rec.sent if m.get("business_connection_id")]


def test_bot_own_message_does_not_pause(env):
    services, dp, bot, rec = auto_env(env)
    own = client_message("Цифровой экран — 30 000 ₽", from_id=OWNER)
    own["business_message"]["sender_business_bot"] = {"id": 42, "is_bot": True, "first_name": "Bot"}
    feed(dp, bot, own)  # Telegram возвращает боту его же ответ от имени менеджера
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"]


def test_owner_photo_pauses_bot(env):
    services, dp, bot, rec = auto_env(env)
    photo = client_message("", from_id=OWNER)
    del photo["business_message"]["text"]
    photo["business_message"]["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    feed(dp, bot, photo)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert to_client(rec) == []


def test_stale_message_ignored(env):
    services, dp, bot, rec = auto_env(env)
    old = client_message("Сколько стоит экран в атриуме?")
    old["business_message"]["date"] = NOW - 3600
    feed(dp, bot, old)
    assert to_client(rec) == [] and services.db.stats()["total"] == 0


def test_holding_message_is_not_repeated_but_bot_keeps_answering(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Дадите скидку?"))
    feed(dp, bot, client_message("А договор пришлёте?"))
    assert len(to_client(rec)) == 1  # «Уточню и вернусь» — один раз
    cards = [m["text"] for m in rec.sent if m["chat_id"] == OWNER and "Вопрос #" in m["text"]]
    assert len(cards) == 2 and "Уточню и вернусь" not in cards[1], "второй вопрос — менеджеру молча"
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"], "после «уточню» бот не замолкает на то, что знает"


def test_greeting_and_thanks(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Здравствуйте!"))
    feed(dp, bot, client_message("Спасибо 👍"))
    sent = to_client(rec)
    assert len(sent) == 1 and sent[0]["text"].startswith("Здравствуйте")
    assert services.db.stats()["total"] == 0 and services.db.is_paused(CONN, CLIENT) is False


def test_status_only_for_owner(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    calls = []

    async def fake_request(bot, method, timeout=None):
        calls.append(method)
        return True
    bot.session.make_request = fake_request  # type: ignore[method-assign]
    feed(dp, bot, {"message": {"message_id": 5, "date": NOW, "text": "/status",
                               "chat": {"id": 777, "type": "private"},
                               "from": {"id": 777, "is_bot": False, "first_name": "Stranger"}}})
    assert "только менеджеру" in calls[-1].text


def test_failed_auto_reply_goes_to_manager(env):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    services, dp, bot, rec = auto_env(env)

    async def failing(chat_id, text, **kwargs):
        if kwargs.get("business_connection_id"):
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="Forbidden")
        rec.sent.append({"chat_id": chat_id, "text": text, **kwargs})
    bot.send_message = failing  # type: ignore[method-assign]
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "Не получилось отправить" in rec.sent[-1]["text"]
    assert services.db.recent_dialogs(1)[0]["status"] == D.ESCALATED


def test_working_hours_turn_auto_into_drafts(env):
    services, dp, bot, rec = auto_env(env)
    services.auto_allowed = lambda now=None: False  # рабочее время по расписанию
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert not to_client(rec), "днём бот не пишет клиенту сам"
    card = [m for m in rec.sent if m["chat_id"] == OWNER][-1]
    assert "reply_markup" in card and services.db.recent_dialogs(1)[0]["status"] == "draft_pending"


def test_messages_in_a_row_are_merged(env):
    services, dp, bot, rec = auto_env(env)
    asyncio.run(services.update_settings({"merge_seconds": "0.2"}))

    async def scenario():
        for text in ("Здравствуйте", "сколько стоит экран", "в атриуме?"):
            update = Update.model_validate({"update_id": 1, **client_message(text)}, context={"bot": bot})
            await dp.feed_update(bot, update)
        assert not to_client(rec), "пока клиент печатает, бот молчит"
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert len(to_client(rec)) == 1
    question = services.db.recent_dialogs(1)[0]["question"]
    assert "Здравствуйте" in question and "атриуме" in question


def test_evening_summary_with_error_buttons(env):
    from app.channels.telegram_business import maybe_send_summaries
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    feed(dp, bot, client_message("Дадите скидку?"))
    late = services.local_now().replace(hour=23, minute=59)
    assert asyncio.run(maybe_send_summaries(bot, services, late)) == 1
    summary = rec.sent[-1]
    assert summary["chat_id"] == OWNER and "Итоги дня" in summary["text"]
    assert "ответил сам — 1, передал вам — 1" in summary["text"]
    button = summary["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Ошибка 1"
    assert asyncio.run(maybe_send_summaries(bot, services, late)) == 0, "второй раз за день не шлём"

    feed(dp, bot, {"callback_query": {
        "id": "cb2", "chat_instance": "x", "data": button.callback_data,
        "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
    }})
    assert services.db.stats()["flagged"] == 1


def test_summary_waits_for_its_time(env):
    from app.channels.telegram_business import maybe_send_summaries
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    early = services.local_now().replace(hour=8, minute=0)
    assert asyncio.run(maybe_send_summaries(bot, services, early)) == 0


def test_voice_message_goes_to_manager(env):
    services, dp, bot, rec = auto_env(env)
    voice = client_message("")
    del voice["business_message"]["text"]
    voice["business_message"]["voice"] = {"file_id": "v1", "file_unique_id": "u1", "duration": 4}
    feed(dp, bot, voice)
    assert to_client(rec)[-1]["text"] == services.assistant.holding_message
    manager = [m for m in rec.sent if m["chat_id"] == OWNER][-1]
    assert "голосовое сообщение" in manager["text"]
    assert services.db.is_paused(CONN, CLIENT)
    assert services.db.recent_dialogs(1)[0]["reason"] == "media"


def test_photo_caption_is_a_question(env):
    services, dp, bot, rec = auto_env(env)
    photo = client_message("")
    del photo["business_message"]["text"]
    photo["business_message"]["photo"] = [{"file_id": "p1", "file_unique_id": "u2", "width": 10, "height": 10}]
    photo["business_message"]["caption"] = "Сколько стоит экран в атриуме?"
    feed(dp, bot, photo)
    assert "30 000" in to_client(rec)[-1]["text"]


def test_signature_and_address_from_settings(env):
    services, dp, bot, rec = auto_env(env)
    asyncio.run(services.update_settings({"signature": "Анна, реклама в ТЦ", "address": "ты"}))
    assert services.assistant.address == "ты"
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert to_client(rec)[-1]["text"].endswith("\n\nАнна, реклама в ТЦ")


def test_manager_card_names_the_client(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    msg = client_message("Какие требования к макету?")
    msg["business_message"]["from"].update(first_name="Иван", last_name="Петров", username="ivan_p")
    feed(dp, bot, msg)
    card = [m for m in rec.sent if m["chat_id"] == OWNER][-1]["text"]
    assert card.startswith("Клиент: Иван Петров (@ivan_p)")
    assert "Иван" not in services.db.recent_dialogs(1)[0]["question"]


def test_long_messages_fit_telegram_limit(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    feed(dp, bot, client_message("Какие требования к макету? " + "очень подробно " * 400))
    card = [m for m in rec.sent if m["chat_id"] == OWNER][-1]["text"]
    # Вопрос обрезан в самой карточке, а номер черновика в конце уцелел — на карточку можно ответить
    assert len(card) <= 4096 and "…»" in card and card.endswith("своим текстом.") and "Черновик #1." in card


def manager_reply(card_text, text):
    return {"message": {
        "message_id": 50, "date": NOW, "text": text,
        "chat": {"id": OWNER, "type": "private", "first_name": "Manager"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
        "reply_to_message": {"message_id": 49, "date": NOW, "text": card_text,
                             "chat": {"id": OWNER, "type": "private", "first_name": "Manager"}},
    }}


def test_manager_answers_question_card_and_edits_draft(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Дадите скидку?"))                     # стоп-тема: черновика нет
    card = [m for m in rec.sent if m["chat_id"] == OWNER][-1]["text"]
    assert "Вопрос #" in card and "ответом на это сообщение" in card
    feed(dp, bot, manager_reply(card, "Скидка 10% при оплате за полгода"))
    assert to_client(rec)[-1]["text"] == "Скидка 10% при оплате за полгода"
    dialog = services.db.recent_dialogs(1)[0]
    assert dialog["status"] == D.DRAFT_EDITED and dialog["answer"].startswith("Скидка 10%")
    # повторный ответ на ту же карточку не отправляется второй раз
    feed(dp, bot, manager_reply(card, "ещё раз"))
    assert to_client(rec)[-1]["text"] != "ещё раз"


def test_manager_message_pauses_and_resume_command(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Привет, сейчас всё расскажу", from_id=OWNER))   # менеджер пишет сам
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert not to_client(rec), "чат на паузе — бот молчит"
    assert any("на паузе" in text for _, text in services.events)
    feed(dp, bot, {"message": {"message_id": 7, "date": NOW, "text": "/resume",
                               "chat": {"id": OWNER, "type": "private", "first_name": "Manager"},
                               "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"}}})
    assert not services.db.active_pauses()
    assert any("снял паузы командой /resume (1)" in text for _, text in services.events)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"]
    assert any("бот ответил сам" in text for _, text in services.events)


# --- защита от лишних трат и гонок ---
STRANGER = 777


def put(dp, bot, payload, n=1):
    return dp.feed_update(bot, Update.model_validate({"update_id": n, **payload}, context={"bot": bot}))


def slow_sender(bot, rec):
    """Отправка с задержкой, как в жизни: пока одно сообщение уходит, успевает прийти следующее."""
    async def send(chat_id, text, **kwargs):
        await asyncio.sleep(0.05)
        rec.sent.append({"chat_id": chat_id, "text": text, **kwargs})
    bot.send_message = send  # type: ignore[method-assign]


def command(text, user=OWNER):
    return {"message": {"message_id": 7, "date": NOW, "text": text,
                        "chat": {"id": user, "type": "private", "first_name": "U"},
                        "from": {"id": user, "is_bot": False, "first_name": "U"}}}


def test_stranger_connection_waits_for_approval(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, {"business_connection": {
        "id": "bc-stranger", "user": {"id": STRANGER, "is_bot": False, "first_name": "Stranger"},
        "user_chat_id": STRANGER, "date": NOW, "is_enabled": True, "rights": {"can_reply": True},
    }})
    assert not services.db.get_connection("bc-stranger").approved
    assert rec.sent[-1]["chat_id"] == STRANGER and "только для менеджеров" in rec.sent[-1]["text"]
    msg = client_message("Сколько стоит экран в атриуме?")
    msg["business_message"]["business_connection_id"] = "bc-stranger"
    feed(dp, bot, msg)
    assert services.usage_today() == (0, 0) and services.db.stats()["total"] == 0, "модель не тратим"
    feed(dp, bot, command("/mode auto", STRANGER))
    assert services.db.get_connection("bc-stranger").mode == "draft"


def test_daily_model_limit_hands_questions_to_manager(env):
    services, dp, bot, rec = auto_env(env)
    asyncio.run(services.update_settings({"daily_llm_limit": "1"}))
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"]
    feed(dp, bot, client_message("Какие требования к макету?"))
    assert to_client(rec)[-1]["text"] == services.assistant.holding_message
    assert services.db.recent_dialogs(1)[0]["reason"] == "limit"
    assert services.usage_today()[0] == 1, "после лимита модель не вызывается"
    assert any("лимит модели" in text for _, text in services.events)


def test_chatty_client_is_muted_for_an_hour(env):
    services, dp, bot, rec = auto_env(env)
    asyncio.run(services.update_settings({"chat_hourly_limit": "2"}))
    for _ in range(4):
        feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert len(to_client(rec)) == 2 and services.usage_today()[0] == 2
    assert services.db.is_paused(CONN, CLIENT)
    cards = [m["text"] for m in rec.sent if m["chat_id"] == OWNER and "слишком часто" in m["text"]]
    assert len(cards) == 1, "менеджеру одно предупреждение, дальше чат на паузе"


def test_telegram_away_message_does_not_pause(env):
    services, dp, bot, rec = auto_env(env)
    away = client_message("Сейчас не на месте, отвечу позже", from_id=OWNER)
    away["business_message"]["is_from_offline"] = True
    feed(dp, bot, away)
    assert not services.db.is_paused(CONN, CLIENT)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"]


def test_other_bots_are_ignored(env):
    services, dp, bot, rec = auto_env(env)
    other = client_message("Сколько стоит экран в атриуме?", from_id=999)
    other["business_message"]["from"]["is_bot"] = True
    feed(dp, bot, other)
    assert not to_client(rec) and services.usage_today()[0] == 0


def test_double_click_sends_draft_once(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    feed(dp, bot, client_message("Какие требования к макету?"))
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    slow_sender(bot, rec)

    def click(n):
        return {"callback_query": {"id": f"cb{n}", "chat_instance": "x", "data": f"send:{dialog_id}",
                                   "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"}}}

    async def two_clicks():
        await asyncio.gather(put(dp, bot, click(1), 1), put(dp, bot, click(2), 2))
    asyncio.run(two_clicks())
    assert len(to_client(rec)) == 1
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_SENT


def test_button_on_old_card_does_not_crash(env):
    services, dp, bot, rec = env
    connect(dp, bot)
    feed(dp, bot, client_message("Какие требования к макету?"))
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    feed(dp, bot, {"callback_query": {  # карточка старше 48 часов: Telegram отдаёт её без текста
        "id": "cb1", "chat_instance": "x", "data": f"send:{dialog_id}",
        "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
        "message": {"message_id": 5, "date": 0, "chat": {"id": OWNER, "type": "private", "first_name": "M"}},
    }})
    assert services.db.get_dialog(dialog_id)["status"] == D.DRAFT_SENT


def media(n, **fields):
    m = client_message("")
    del m["business_message"]["text"]
    m["business_message"].update(message_id=10 + n, **fields)
    return m


def test_album_gets_one_holding_message(env):
    services, dp, bot, rec = auto_env(env)
    slow_sender(bot, rec)
    photo = [{"file_id": "p", "file_unique_id": "u", "width": 1, "height": 1}]

    async def album():
        await asyncio.gather(*(put(dp, bot, media(n, photo=photo, media_group_id="g1"), n) for n in range(3)))
    asyncio.run(album())
    assert len(to_client(rec)) == 1
    assert len([m for m in rec.sent if m["chat_id"] == OWNER and "фото" in m["text"]]) == 1


def test_gif_is_ignored(env):
    services, dp, bot, rec = auto_env(env)
    doc = {"file_id": "a", "file_unique_id": "ua"}
    feed(dp, bot, media(1, animation={**doc, "width": 1, "height": 1, "duration": 1}, document=doc))
    assert not to_client(rec) and not services.db.is_paused(CONN, CLIENT)


def test_edited_and_deleted_messages_while_waiting(env):
    services, dp, bot, rec = auto_env(env)
    asyncio.run(services.update_settings({"merge_seconds": "0.2"}))
    first, second = client_message("Сколько стоит эран"), client_message("ой, не туда")
    second["business_message"]["message_id"] = 2

    async def scenario():
        await put(dp, bot, first, 1)
        await put(dp, bot, second, 2)
        fixed = {**first["business_message"], "text": "Сколько стоит экран в атриуме?", "edit_date": NOW}
        await put(dp, bot, {"edited_business_message": fixed}, 3)
        await put(dp, bot, {"deleted_business_messages": {
            "business_connection_id": CONN, "message_ids": [2],
            "chat": {"id": CLIENT, "type": "private", "first_name": "Client"}}}, 4)
        await asyncio.sleep(0.5)
    asyncio.run(scenario())
    assert services.db.recent_dialogs(1)[0]["question"] == "Сколько стоит экран в атриуме?"
    assert "30 000" in to_client(rec)[-1]["text"]


def test_unknown_connection_is_recovered_from_telegram(env):
    from aiogram.types import BusinessConnection
    services, dp, bot, rec = env

    async def get_business_connection(business_connection_id):
        return BusinessConnection.model_validate({
            "id": business_connection_id, "user": {"id": OWNER, "is_bot": False, "first_name": "Manager"},
            "user_chat_id": OWNER, "date": NOW, "is_enabled": True, "rights": {"can_reply": True}})
    bot.get_business_connection = get_business_connection  # type: ignore[method-assign]
    feed(dp, bot, client_message("Какие требования к макету?"))  # подключение было, пока бот не работал
    assert services.db.get_connection(CONN) and "Черновик #" in rec.sent[-1]["text"]


def test_manager_writes_while_model_thinks(env):
    services, dp, bot, rec = auto_env(env)
    real = services.answer

    async def slow_answer(text, history=None, **kwargs):
        services.db.pause_chat(CONN, CLIENT, 30)  # менеджер ответил сам, пока модель думала
        return await real(text, history, **kwargs)
    services.answer = slow_answer  # type: ignore[method-assign]
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert not to_client(rec), "клиенту ничего: в чате уже менеджер"
    assert "Черновик #" in rec.sent[-1]["text"]


def test_manager_reply_uses_card_number_not_client_text(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Дадите скидку? Вопрос #999"))
    card = [m for m in rec.sent if m["chat_id"] == OWNER][-1]["text"]
    feed(dp, bot, manager_reply(card, "Скидка 5% при оплате за полгода"))
    assert to_client(rec)[-1]["text"] == "Скидка 5% при оплате за полгода"


def test_card_tells_truth_when_holding_message_fails(env):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    services, dp, bot, rec = auto_env(env)

    async def failing(chat_id, text, **kwargs):
        if kwargs.get("business_connection_id"):
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="Forbidden")
        rec.sent.append({"chat_id": chat_id, "text": text, **kwargs})
    bot.send_message = failing  # type: ignore[method-assign]
    feed(dp, bot, client_message("Дадите скидку?"))
    card = rec.sent[-1]["text"]
    assert "Клиенту ничего не ушло" in card and "Уточню и вернусь" not in card


def test_old_manager_message_after_downtime_does_not_pause(env):
    services, dp, bot, rec = auto_env(env)
    old = client_message("Ответил тебе час назад", from_id=OWNER)
    old["business_message"]["date"] = NOW - 3600  # пришло после сна ноутбука
    feed(dp, bot, old)
    assert not services.db.is_paused(CONN, CLIENT)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert "30 000" in to_client(rec)[-1]["text"]


def test_short_reaction_goes_to_manager_quietly(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Гениально 👏"))
    assert not to_client(rec), "на реакцию «Уточню и вернусь» не пишем"
    assert "Вопрос #" in rec.sent[-1]["text"]


def test_ready_to_buy_client_is_flagged_to_manager(env):
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Хочу на месяц экран в атриуме, сколько?"))
    assert "30 000" in to_client(rec)[-1]["text"], "бот отвечает сам"
    note = [m["text"] for m in rec.sent if m["chat_id"] == OWNER][-1]
    assert "готов к заказу" in note and "Подключитесь" in note
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert [m["text"] for m in rec.sent if m["chat_id"] == OWNER][-1] == note, "обычный вопрос — без тревоги"


@pytest.mark.parametrize("text,ready", [
    ("Пока не берём, дорого", False), ("Если возьмём на 3 месяца, сколько выйдет?", False),
    ("Согласны, запускаем", True), ("Берём экран в атриуме", True),
])
def test_ready_to_buy_respects_negation(text, ready):
    from app.channels.flow import is_ready_to_buy
    assert is_ready_to_buy(text) is ready


def test_old_chat_history_is_not_used(env):
    import time
    services, dp, bot, rec = auto_env(env)
    feed(dp, bot, client_message("Сколько стоит экран в атриуме?"))
    assert services.db.chat_history(CONN, CLIENT) and services.db.last_question(CONN, CLIENT)
    services.db.conn.execute("UPDATE dialogs SET created_at=?", (time.time() - 4 * 3600,))
    assert services.db.chat_history(CONN, CLIENT) == [] and services.db.last_question(CONN, CLIENT) == ""


def test_two_quick_messages_get_one_holding_message(env):
    services, dp, bot, rec = auto_env(env)
    slow_sender(bot, rec)

    async def both():
        await asyncio.gather(put(dp, bot, client_message("Дадите скидку?"), 1),
                             put(dp, bot, client_message("И договор пришлёте?"), 2))
    asyncio.run(both())
    assert len(to_client(rec)) == 1, "второе сообщение ждёт первое и видит, что «уточню» уже было"


def test_objection_is_not_ready_to_buy():
    from app.channels.flow import is_ready_to_buy
    assert not is_ready_to_buy("Не согласны с такой ценой") and not is_ready_to_buy("Нас не устраивает")
