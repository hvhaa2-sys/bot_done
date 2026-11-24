"""Режим наблюдения: бот молчит, записывает обращения клиентов и через сколько ответил менеджер."""
import asyncio
import shutil
import time
from dataclasses import replace

import pytest
from aiogram import Bot
from aiogram.types import Update
from tests.admin_client import AdminClient as TestClient

from app import db as D
from app.admin import create_app
from app.channels.telegram_business import build_dispatcher, build_summary
from app.config import ROOT, Settings
from app.services import Services

OWNER, CLIENT, CONN = 100, 555, "bc-watch"
NOW = int(time.time())


@pytest.fixture
def env(tmp_path):
    shutil.copytree(ROOT / "tests" / "fixtures" / "kb", tmp_path / "kb")
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=tmp_path / "kb", llm_provider="mock",
                       admin_token="secret", exam_files="")
    services = Services(settings)
    asyncio.run(services.update_settings({"merge_seconds": "0"}))
    dp, bot = build_dispatcher(services), Bot("42:TEST")
    sent, calls = [], []

    async def send_message(chat_id, text, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def fake_request(bot, method, timeout=None):  # message.answer и прочие вызовы API
        calls.append(method)
        return True
    bot.send_message = send_message  # type: ignore[method-assign]
    bot.session.make_request = fake_request  # type: ignore[method-assign]
    feed(dp, bot, {"business_connection": {
        "id": CONN, "user": {"id": OWNER, "is_bot": False, "first_name": "Manager"}, "user_chat_id": OWNER,
        "date": NOW, "is_enabled": True, "rights": {"can_reply": True}}})
    services.db.set_mode(D.WATCH, connection_id=CONN)
    services.privacy.set_allowed(CONN, CLIENT, True)
    sent.clear()
    return services, dp, bot, sent, calls


def feed(dp, bot, payload):
    asyncio.run(dp.feed_update(bot, Update.model_validate({"update_id": 1, **payload}, context={"bot": bot})))


def chat_message(text, *, from_id=CLIENT, date=NOW, mid=1, **extra):
    return {"business_message": {
        "message_id": mid, "date": date, "business_connection_id": CONN,
        "chat": {"id": CLIENT, "type": "private", "first_name": "Client"},
        "from": {"id": from_id, "is_bot": False, "first_name": "X"}, **({"text": text} if text else {}), **extra}}


def test_mode_command_switches_to_watch(env):
    services, dp, bot, _, calls = env
    services.db.set_mode("draft", connection_id=CONN)
    feed(dp, bot, {"message": {"message_id": 5, "date": NOW, "text": "/mode watch", "chat": {"id": OWNER, "type": "private"},
                               "from": {"id": OWNER, "is_bot": False, "first_name": "Manager"}}})
    assert services.db.get_connection(CONN).mode == D.OBSERVE, "«watch» — прежнее имя наблюдения"
    assert "Режим: наблюдение" in calls[-1].text and "не пишет клиентам" in calls[-1].text


def test_client_question_is_logged_silently_without_model(env, monkeypatch):
    services, dp, bot, sent, _ = env

    async def no_model(*args, **kwargs):
        raise AssertionError("в наблюдении модель не вызывается")
    monkeypatch.setattr(services, "answer", no_model)
    feed(dp, bot, chat_message("Сколько стоит экран в атриуме?", date=NOW - 900))
    feed(dp, bot, chat_message("И на фуд-корте?", date=NOW - 600, mid=2))
    assert sent == [], "ни клиенту, ни менеджеру ничего не уходит"
    rows = services.db.recent_dialogs(5)
    assert len(rows) == 1, "два сообщения до ответа менеджера — одно обращение"
    assert rows[0]["status"] == D.WATCH and rows[0]["question"] == "Сколько стоит экран в атриуме?\nИ на фуд-корте?"
    assert rows[0]["created_at"] == NOW - 900, "время — из сообщения, а не когда бот его прочитал"


def test_manager_reply_closes_request_with_wait_time_and_no_pause(env):
    services, dp, bot, sent, _ = env
    feed(dp, bot, chat_message("Сколько стоит экран в атриуме?", date=NOW - 900))
    feed(dp, bot, chat_message("Добрый день! Экран в атриуме — 30 000 ₽ в месяц.", from_id=OWNER, mid=2))
    feed(dp, bot, chat_message("Ролик до 15 секунд.", from_id=OWNER, mid=3))
    row = services.db.recent_dialogs(1)[0]
    assert row["reply_after_s"] == 900 and row["answer"].endswith("30 000 ₽ в месяц.\nРолик до 15 секунд.")
    assert not services.db.is_paused(CONN, CLIENT), "в наблюдении бот и так молчит — пауза не нужна"
    assert any("30 000" in c["answer"] for c in services.db.candidates()), "ответ менеджера — предложение в базу"
    feed(dp, bot, chat_message("А радио?", date=NOW + 60, mid=4))
    assert len(services.db.recent_dialogs(5)) == 2, "после ответа менеджера — новое обращение"
    assert sent == []


def test_manager_writes_first_and_media_requests(env):
    services, dp, bot, sent, _ = env
    feed(dp, bot, chat_message("Здравствуйте, напоминаю про макет", from_id=OWNER, date=NOW - 1200))
    assert services.db.recent_dialogs(5) == [], "менеджер написал первым — ожидания нет"
    feed(dp, bot, chat_message(None, date=NOW - 300, voice={"file_id": "v", "file_unique_id": "v", "duration": 3}))
    row = services.db.recent_dialogs(1)[0]
    assert row["question"] == "[голосовое сообщение]" and sent == []
    feed(dp, bot, chat_message(None, from_id=OWNER, mid=2, photo=[{"file_id": "p", "file_unique_id": "p",
                                                                    "width": 1, "height": 1}]))
    row = services.db.recent_dialogs(1)[0]
    assert row["reply_after_s"] == 300 and row["answer"] == "[фото]"


def test_watch_stays_out_of_bot_stats_and_feeds_baseline(env):
    services, dp, bot, _, _ = env
    for i, wait in enumerate((120, 600, 7200)):
        feed(dp, bot, chat_message(f"Вопрос {i}?", date=NOW - 10000 + i * 10, mid=i * 2 + 1))
        feed(dp, bot, chat_message("Ответ", from_id=OWNER, date=NOW - 10000 + i * 10 + wait, mid=i * 2 + 2))
    feed(dp, bot, chat_message("Без ответа?", date=NOW, mid=99))
    stats = services.db.stats()
    assert stats["real"] == 0 and stats["auto_share"] == 0.0, "наблюдение — не ответы бота"
    watch = services.watch_stats()
    assert watch["answered"] == 3 and watch["unanswered"] == 1 and watch["median_wait_min"] == 10.0
    assert watch["over_1_hour"] == round(1 / 3, 3)


def test_evening_summary_reports_observation(env):
    services, dp, bot, _, _ = env
    feed(dp, bot, chat_message("Сколько стоит экран в атриуме?", date=NOW - 1800))
    feed(dp, bot, chat_message("30 000 ₽ в месяц.", from_id=OWNER, mid=2, date=NOW - 600))
    text, markup = build_summary(services, services.db.get_connection(CONN), NOW - 86400)
    assert "Наблюдение за день: обращений клиентов — 1, вы ответили на 1, обычно через 20 мин" in text
    assert "Итоги дня" not in text and markup is None


def test_admin_shows_watch_and_offers_off_only_for_telegram(env):
    services, dp, bot, _, _ = env
    feed(dp, bot, chat_message("Сколько стоит экран в атриуме?", date=NOW - 1800))
    feed(dp, bot, chat_message("30 000 ₽ в месяц.", from_id=OWNER, mid=2, date=NOW - 600))
    services.db.upsert_connection("max:7", 7, 70, True, True, "draft")
    services.db.approve_connection("max:7")  # второй менеджер — после «Разрешить»
    client = TestClient(create_app(services))
    home = client.get("/?token=secret").text
    assert "Было → стало" in home and "20 мин" in home and "медиана за наблюдение" in home
    assert home.count('value="observe"') == 2, "наблюдение есть и в MAX: бот пересылает вопросы менеджеру"
    assert home.count('value="off"') == 1, "«Выключен» — только для Telegram"
    client.post("/mode", data={"connection_id": "max:7", "mode": "off"})
    assert services.db.get_connection("max:7").mode == "draft", "в MAX без бота клиенту никто не ответит"
    client.post("/mode", data={"connection_id": "max:7", "mode": "watch"})
    assert services.db.get_connection("max:7").mode == "observe"
    client.post("/mode", data={"connection_id": CONN, "mode": "auto"})
    assert services.db.get_connection(CONN).mode == "auto"
    journal = client.get("/dialogs?f=watch").text  # старая ссылка ведёт в раздел «Наблюдение»
    assert "<h1>Наблюдение</h1>" in journal and "ответ через 20 мин" in journal
    client.post("/settings", data={"default_mode": "watch"})
    assert services.current()["default_mode"] == "observe", "старое имя режима принимается и приводится к общему"


def watch_lines(services):
    import json
    return [json.loads(line) for line in services.watch_log.read_text(encoding="utf-8").splitlines()]


def test_whole_correspondence_is_saved_to_local_file(env):
    services, dp, bot, _, _ = env
    feed(dp, bot, chat_message("Добрый день, это Иван, напоминаю о себе", from_id=OWNER, date=NOW - 3000))
    feed(dp, bot, chat_message("Сколько стоит экран? Мой телефон +7 912 345-67-89", date=NOW - 900, mid=2))
    feed(dp, bot, chat_message("30 000 ₽ в месяц.", from_id=OWNER, date=NOW - 300, mid=3))
    lines = watch_lines(services)
    assert [m["from"] for m in lines] == ["manager", "client", "manager"], "и сообщения, где менеджер пишет первым"
    assert "+7 912" not in lines[1]["text"] and "[телефон]" in lines[1]["text"]
    assert lines[1]["chat"] == CLIENT and lines[1]["conn"] == CONN and "T" in lines[1]["date"]
    assert not {"name", "first_name", "username"} & set(lines[0]), "имён в файле нет"
    services.db.set_mode("draft", connection_id=CONN)
    services.db.pause_chat(CONN, CLIENT, 30)  # в черновиках бот молчит на паузе — нам важно только, что файл не растёт
    feed(dp, bot, chat_message("А радио?", mid=4))
    assert len(watch_lines(services)) == 3, "файл пишется только в наблюдении"


def test_draft_and_baseline_from_watch_file(env, tmp_path):
    from tools.import_telegram import run_import
    services, dp, bot, _, _ = env
    for i, wait in enumerate((120, 600, 1800)):
        feed(dp, bot, chat_message(f"Сколько стоит экран в атриуме, вариант {i}?", date=NOW - 10000 + i * 3000,
                                   mid=i * 2 + 1))
        feed(dp, bot, chat_message("Экран в атриуме — 30 000 ₽ в месяц.", from_id=OWNER,
                                   date=NOW - 10000 + i * 3000 + wait, mid=i * 2 + 2))
    r = run_import([services.watch_log], None, tmp_path / "import")
    assert r["chats"] == 1 and r["pairs"] == 3 and r["answered"] == 3
    assert r["median_wait_min"] == services.watch_stats()["median_wait_min"] == 10.0, "файл и журнал считают одинаково"
    assert "чат 1" in (tmp_path / "import" / "pairs.csv").read_text(encoding="utf-8"), "вместо имени — номер чата"


def test_admin_downloads_builds_draft_and_deletes_watch_file(env):
    services, dp, bot, _, _ = env
    feed(dp, bot, chat_message("Сколько стоит экран в атриуме?", date=NOW - 1800))
    feed(dp, bot, chat_message("Экран в атриуме — 30 000 ₽ в месяц.", from_id=OWNER, mid=2, date=NOW - 600))
    client = TestClient(create_app(services))
    page = client.get("/observe?token=secret").text
    assert "Файл переписки" in page and "2 сообщения в 1 чате" in page
    assert "Файл переписки" not in client.get("/kb").text, "файл переехал из «Базы знаний» в «Наблюдение»"
    download = client.get("/watch/file")
    assert download.status_code == 200 and "30 000" in download.text and "attachment" in download.headers["content-disposition"]
    assert "Черновик собран из наблюдения: чатов 1" in client.post("/watch/import").text
    assert (services.settings.db_path.parent / "import" / "faq_draft.md").exists()
    assert "файл переписки удалён" in client.post("/watch/clear").text.lower()
    assert not services.watch_log.exists() and client.get("/watch/file").status_code == 404
    assert services.db.recent_dialogs(1)[0]["status"] == D.WATCH, "замер времени ответа остаётся"
