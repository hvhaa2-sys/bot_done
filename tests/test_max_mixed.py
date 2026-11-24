"""Смешанный запрос приходит как обычное событие MAX. SQLite и оба транспорта изолированы."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from app import db as D
from app.channels.max_bot import MaxBot
from app.config import Settings
from app.llm.gigachat import GigaChatProvider
from app.services import Services
from tests.test_max import BOT, CLIENT_CHAT, CONN, MANAGER_CHAT, FakeApi, connect, feed, message, reply_to, to

QUESTION = "Какие требования к ролику для медиа-куба ВЕЕР?"
STATUS = "И уже заменили наш радиоролик?"
RADIO = "И подскажите, удалось ролик заменить на радио?"
TEXT = f"{QUESTION} {STATUS}"
SPEC = "Файл MP4, 3072×640 px на четыре грани, до 10 секунд без звука."
TITLE = "Какие требования к ролику для медиа-куба ВЕЕР Молл?"


@pytest.fixture
def route(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config._load_dotenv", lambda path: None)
    kb = tmp_path / "kb"
    notes = kb / "notes"
    notes.mkdir(parents=True)
    note = notes / "Экраны.md"
    note.write_text(f"---\nтц: ВЕЕР Молл\n---\n## {TITLE}\n{SPEC}\n", encoding="utf-8")
    requests = []

    def model(request):
        requests.append(request.url.path)
        return httpx.Response(403)

    provider = GigaChatProvider("fake", "scope", "test", True, transport=httpx.MockTransport(model))
    settings = replace(Settings.load(), kb_dir=kb, db_path=tmp_path / "test.db", llm_provider="mock",
                       telegram_bot_token="", max_bot_token="", speech_enabled=False, exam_files="")
    services = Services(settings, llm=provider)
    asyncio.run(services.update_settings({"merge_seconds": "0", "auto_schedule": "always"}))
    api = FakeApi()
    bot = MaxBot(api, services, BOT)
    connect(bot)
    services.db.set_mode("auto", connection_id=CONN)
    api.sent.clear()
    yield bot, api, requests
    asyncio.run(provider.close())


@pytest.mark.parametrize(("text", "remaining"), [
    (TEXT, STATUS),
    ("Замените наш радиоролик. " + QUESTION, "Замените наш радиоролик."),
    (QUESTION + " Выставьте счёт.", "Выставьте счёт."),
    ("Можно ТТ на куб в ВЕЕРе? " + RADIO, RADIO),
    ("ТТ для медиакуба ВЕЕРа? " + RADIO, RADIO),
    ("Технические требования к кубу в ВЕЕР Молле? На радио уже заменили ролик?", "На радио уже заменили ролик?"),
], ids=["status", "action_first", "invoice", "report", "genitive", "mall_prepositional"])
def test_mixed_request_keeps_one_open_card_with_published_spec(route, text, remaining):
    bot, api, requests = route
    # Редактирование ещё не опубликовано. В карточку должен попасть прежний ответ.
    (bot.services.settings.kb_dir / "notes" / "Экраны.md").write_text("", encoding="utf-8")
    feed(bot, message(text))
    card, = api.sent
    assert card["chat_id"] == MANAGER_CHAT and card["buttons"] is None
    assert f"«{text}»" in card["text"]
    assert f"Менеджеру: «{remaining}»" in card["text"]
    assert SPEC in card["text"] and TITLE in card["text"]
    assert requests == []
    row, = bot.services.db.recent_dialogs()
    assert (row["question"], row["status"], row["answer"]) == (text, D.ESCALATED, "")
    assert bot.services.db.chat_history(CONN, CLIENT_CHAT) == []

    feed(bot, reply_to(card["text"], "Проверяю замену."))
    assert to(api, CLIENT_CHAT)[0]["text"] == "Проверяю замену."
    assert bot.services.db.chat_history(CONN, CLIENT_CHAT) == [(text, "Проверяю замену.")]


@pytest.mark.parametrize("text", [
    STATUS,
    "Какие требования к ролику? " + STATUS,
    "Какие требования к ролику для медиа-куба ДЕПО? " + STATUS,
    QUESTION + " Есть ли звук? " + STATUS,
    "Можно ТТ на куб? " + RADIO,
    "Можно ТТ на куб в ВЕЕРе и ДЕПО? " + RADIO,
    "Можно ТТ на напольный куб в ВЕЕРе? " + RADIO,
    "Выставьте счёт на размещение.",
])
def test_ordinary_and_ambiguous_requests_relay_only_original(route, text):
    bot, api, requests = route
    feed(bot, message(text))
    card, = api.sent
    assert card["chat_id"] == MANAGER_CHAT and f"«{text}»" in card["text"]
    assert SPEC not in card["text"] and "Менеджеру: «" not in card["text"]
    assert requests == []


@pytest.mark.parametrize("mode", ["draft", "observe", "off", "paused", "erased", "no_consent", "stale", "work_hours"])
def test_other_routes_keep_their_delivery_and_history(route, mode, monkeypatch):
    bot, api, requests = route
    services = bot.services
    if mode in {"draft", "observe", "off"}:
        services.db.set_mode(mode, connection_id=CONN)
    elif mode == "paused":
        services.db.pause_chat(CONN, CLIENT_CHAT, 30)
    elif mode == "erased":
        services.privacy.erase(CLIENT_CHAT, services.watch_log)
    elif mode == "no_consent":
        services.db.set_mode(D.OBSERVE, connection_id=CONN)
        services.privacy.set_allowed(CONN, CLIENT_CHAT, False)
    elif mode == "work_hours":
        monkeypatch.setattr(services, "auto_allowed", lambda: False)
    feed(bot, message(TEXT, age_s=86400 if mode == "stale" else 0))
    if mode in {"off", "erased", "no_consent"}:
        assert api.sent == [] and services.db.recent_dialogs() == []
    else:
        card, = api.sent
        assert card["chat_id"] == MANAGER_CHAT and TEXT in card["text"] and SPEC not in card["text"]
        row, = services.db.recent_dialogs()
        assert row["status"] == (D.WATCH if mode == "observe" else D.ESCALATED) and row["question"] == TEXT
    assert requests == []


def test_manager_delivery_failure_sends_nothing_to_client(route):
    bot, api, requests = route
    api.fail = True
    feed(bot, message(TEXT))
    assert set(bot.last_send_error) == {MANAGER_CHAT}
    assert api.sent == [] and requests == []
    assert not any("переслано менеджеру" in event for _, event in bot.services.events)
    row, = bot.services.db.recent_dialogs()
    assert row["question"] == TEXT and row["answer"] == "" and row["status"] == D.ESCALATED


def test_hourly_limit_keeps_normal_pause_and_forward(route, monkeypatch):
    bot, api, requests = route
    asyncio.run(bot.services.update_settings({"chat_hourly_limit": "1"}))
    feed(bot, message("Выставьте счёт на размещение."))
    api.sent.clear()
    # Предыдущее обращение выходит из часового окна сразу после подсчёта.
    db = bot.services.db
    count_recent = db.count_recent

    def count_then_expire(*args):
        count = count_recent(*args)
        with db.conn:
            db.conn.execute("UPDATE dialogs SET created_at=created_at-3600 WHERE chat_id=?", (CLIENT_CHAT,))
        return count

    monkeypatch.setattr(db, "count_recent", count_then_expire)
    feed(bot, message(TEXT))
    assert bot.services.db.is_paused(CONN, CLIENT_CHAT)
    card, = api.sent
    assert card["chat_id"] == MANAGER_CHAT and TEXT in card["text"] and SPEC not in card["text"]
    assert requests == []


@pytest.mark.parametrize("problem", ["ambiguous", "unscoped", "wrong_venue", "confirmation", "lookup_error", "stop_topic"])
def test_missing_reliable_spec_keeps_original_handoff(route, problem, monkeypatch):
    bot, api, requests = route
    services = bot.services
    kb_dir = services.settings.kb_dir
    note = kb_dir / "notes" / "Экраны.md"
    if problem == "lookup_error":
        def unavailable(question):
            raise RuntimeError("FAQ недоступен")
        monkeypatch.setattr(services.kb, "matched_stop_topic", unavailable)
    else:
        if problem == "ambiguous":
            (note.parent / "Другие требования.md").write_text(
                f"---\nтц: ВЕЕР Молл\n---\n## {TITLE}\nФайл AVI.\n", encoding="utf-8")
        elif problem == "confirmation":
            note.write_text(f"---\nтц: ВЕЕР Молл\n---\n## {TITLE}\nРолик заменили.\n", encoding="utf-8")
        elif problem == "stop_topic":
            (kb_dir / "stop_topics.txt").write_text("требован\n", encoding="utf-8")
        else:
            header = "---\nтц: ДЕПО\n---\n" if problem == "wrong_venue" else ""
            note.write_text(f"{header}## {TITLE}\n{SPEC}\n", encoding="utf-8")
        services.publish("Тестовая версия", force=True)
    feed(bot, message(TEXT))
    card, = api.sent
    assert card["chat_id"] == MANAGER_CHAT and f"«{TEXT}»" in card["text"]
    assert "Справка из базы" not in card["text"] and requests == []


@pytest.mark.parametrize("venue", ["ВЕЕР Молл", "ДЕПО"])
def test_short_cube_request_requires_one_technical_document_at_that_venue(route, venue):
    bot, api, requests = route
    kb_dir = bot.services.settings.kb_dir
    (kb_dir / "notes" / "Напольный куб.md").write_text(
        f"---\nтц: {venue}\n---\n## Технические требования к напольному кубу\nМакет в PDF.\n", encoding="utf-8")
    bot.services.publish("Тестовая версия", force=True)
    text = "Можно ТТ на куб в ВЕЕРе? " + RADIO
    feed(bot, message(text))
    card, = api.sent
    assert card["chat_id"] == MANAGER_CHAT and f"«{text}»" in card["text"] and requests == []
    assert (SPEC in card["text"]) == (venue == "ДЕПО")
    assert "Макет в PDF." not in card["text"]
