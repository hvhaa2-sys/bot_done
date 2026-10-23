from dataclasses import replace

from fastapi.testclient import TestClient

from app.admin import create_app
from app.config import ROOT, Settings
from app.services import Services


def make_client(tmp_path, login=True):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    for name in ("faq.md", "prices.csv", "stop_topics.txt"):
        (kb_dir / name).write_text((ROOT / "tests" / "fixtures" / "kb" / name).read_text(encoding="utf-8"), encoding="utf-8")
    settings = replace(Settings.load(), db_path=tmp_path / "a.db", kb_dir=kb_dir,
                       llm_provider="mock", admin_token="secret", exam_files="")
    services = Services(settings)
    client = TestClient(create_app(services))
    if login:  # как после формы входа: токен живёт в cookie, а не в адресе страницы
        client.cookies.set("admin_token", "secret")
    return client, services


def test_requires_token(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get("/").status_code == 401
    assert client.get("/?token=secret").status_code == 200
    assert client.get("/dialogs").status_code == 200  # токен запомнен в cookie


def test_sandbox_answers_and_logs(tmp_path):
    client, services = make_client(tmp_path)
    r = client.post("/sandbox", data={"question": "Сколько стоит экран в атриуме?"})
    assert r.status_code == 200 and "30 000" in r.text
    assert services.db.stats()["total"] == 1


def test_upload_kb(tmp_path):
    client, services = make_client(tmp_path)
    faq = "## Есть ли парковка?\nДа, бесплатная парковка на 300 мест.\n"
    r = client.post("/kb/upload", files={"faq": ("faq.md", faq.encode(), "text/markdown")})
    assert "Загружено: faq.md" in r.text
    assert len(services.kb.faq) == 1
    r = client.post("/sandbox", data={"question": "У вас есть парковка?"})
    assert "300 мест" in r.text


def test_settings_apply_live_and_persist(tmp_path):
    client, services = make_client(tmp_path)
    r = client.post("/settings", data={
        "model": "mock", "default_mode": "auto", "holding_message": "Секунду, уточню у коллег!",
        "greeting_message": "Добрый день!", "manager_pause_minutes": "45", "stale_message_minutes": "10",
        "confidence_threshold": "0,6", "retrieval_min_score": "1.0"})
    assert "Сохранено" in r.text
    assert services.assistant.holding_message == "Секунду, уточню у коллег!"
    assert services.settings.default_mode == "auto" and services.settings.manager_pause_minutes == 45
    assert services.assistant.confidence_threshold == 0.6
    # после перезапуска настройки остаются
    again = Services(services.base_settings)
    assert again.settings.manager_pause_minutes == 45 and again.holding_message.startswith("Секунду")
    # сброс возвращает .env
    client.post("/settings/reset")
    assert services.settings.manager_pause_minutes == services.base_settings.manager_pause_minutes


def test_settings_reject_bad_values(tmp_path):
    client, services = make_client(tmp_path)
    r = client.post("/settings", data={"model": "mock", "manager_pause_minutes": "много",
                                                    "holding_message": ""})
    assert "Не сохранено" in r.text
    assert services.db.get_settings() == {}


def test_flag_dialog_and_export(tmp_path):
    client, services = make_client(tmp_path)
    client.post("/sandbox", data={"question": "Сколько стоит экран в атриуме?"})
    dialog_id = services.db.recent_dialogs(1)[0]["id"]
    client.post(f"/dialogs/{dialog_id}/flag", data={"flagged": "1", "back": "/dialogs?f=flagged"})
    assert services.db.recent_dialogs(10, flagged=True)
    r = client.get("/dialogs?f=flagged")
    assert "Снять" in r.text and "атриуме" in r.text
    csv_text = client.get("/dialogs/flagged.csv").text
    assert "Сколько стоит экран в атриуме?" not in csv_text
    r = client.post(f"/dialogs/{dialog_id}/review", data={
        "error_type": "facts", "expected_decision": "answer",
        "corrected_answer": "Экран в атриуме стоит 30 000 ₽.", "must_contain": "30000"},
        follow_redirects=False)
    assert r.status_code == 303
    csv_text = client.get("/dialogs/flagged.csv").text
    assert "Сколько стоит экран в атриуме?,answer,30000" in csv_text


def test_edit_stop_topics_in_browser(tmp_path):
    client, services = make_client(tmp_path)
    assert services.kb.matched_stop_topic("Нужен акт сверки") is None
    r = client.post("/kb/save", data={"name": "stop_topics.txt", "text": "скидк\nакт\n"})
    assert "Сохранено" in r.text
    assert services.kb.matched_stop_topic("Нужен акт сверки") == "акт"
    assert client.get("/kb/file/stop_topics.txt").text == "скидк\nакт\n"
    assert client.get("/kb/file/../.env").status_code == 404


def test_exam_runs_on_current_model(tmp_path):
    import asyncio
    from app.admin import last_exam, run_exam
    _, services = make_client(tmp_path)
    state = {"running": True, "done": 0, "total": 0, "error": ""}
    asyncio.run(run_exam(services, state))
    assert not state["running"] and not state["error"] and state["done"] == state["total"] > 0
    exam = last_exam(services)
    assert exam["summary"]["questions"] == state["total"] and exam["model"] == "Заглушка"
    assert services.usage_today()[0] > 0, "экзамен учтён в расходе модели"


def test_pages_render(tmp_path):
    client, _ = make_client(tmp_path)
    for url in ("/?token=secret", "/dialogs", "/dialogs?f=auto&q=экран", "/kb", "/exam", "/settings"):
        assert client.get(url).status_code == 200, url


def test_schedule_off_hours(tmp_path):
    import asyncio
    import datetime as dt
    client, services = make_client(tmp_path)
    r = client.post("/settings", data={
        "auto_schedule": "off_hours", "work_start": "09:00", "work_end": "19:00",
        "work_day": ["1", "2", "3", "4", "5"]})
    assert "Сохранено" in r.text, r.text[:300]
    tue_noon, tue_night, sat_noon = (dt.datetime(2026, 10, 13, 12), dt.datetime(2026, 10, 13, 22),
                                     dt.datetime(2026, 10, 17, 12))
    assert not services.auto_allowed(tue_noon)
    assert services.auto_allowed(tue_night) and services.auto_allowed(sat_noon)
    assert services.schedule_text() == "Пн–Пт 09:00–19:00"
    r = client.post("/settings", data={"auto_schedule": "off_hours", "work_start": "9 утра"})
    assert "Не сохранено" in r.text
    asyncio.run(services.reset_settings())
    assert services.auto_allowed(tue_noon)


def test_import_chats_and_before_after_card(tmp_path):
    from tests.test_import import full_export
    client, services = make_client(tmp_path)
    export = full_export(tmp_path).read_bytes()
    r = client.post("/import", files={"files": ("result.json", export, "application/json")})
    assert "Импорт готов: чатов 2" in r.text and "повторяющиеся вопросы: 100%" in r.text
    assert "Черновик FAQ" in r.text
    assert "## " in client.get("/import/file/faq_draft.md").text
    home = client.get("/").text
    assert "Было → стало" in home and "3 мин" in home
    bad = client.post("/import", files={"files": ("x.json", b"not json", "application/json")})
    assert "Не получилось разобрать" in bad.text


def test_merge_and_summary_settings(tmp_path):
    client, services = make_client(tmp_path)
    r = client.post("/settings", data={"merge_seconds": "2,5", "summary_time": ""})
    assert "Сохранено" in r.text
    assert services.current()["merge_seconds"] == "2.5" and services.current()["summary_time"] == ""
    assert "Не сохранено" in client.post("/settings", data={"summary_time": "вечером"}).text


def log(services, question, decision="escalate", reason="no_context", status="escalated", flagged=False):
    i = services.db.log_dialog(connection_id="c", chat_id=1, question=question, answer="—", decision=decision,
                               reason=reason, confidence=0.0, status=status, sources=[], latency_ms=0, mode="auto")
    if flagged:
        services.db.flag_dialog(i, True)
    return i


def test_learn_groups_questions_and_teaches_bot(tmp_path):
    client, services = make_client(tmp_path)
    log(services, "Есть ли у вас парковка для клиентов?")
    log(services, "А парковка для клиентов есть?")
    log(services, "Дадите скидку?", reason="stop_topic:скидк")           # работа менеджера, не пробел в базе
    log(services, "Цена экрана в атриуме?", reason="llm_error:ConnectTimeout")  # сбой сети, база ни при чём
    log(services, "Сколько стоит лайтбокс?", decision="answer", reason="", status="sent_auto", flagged=True)
    page = client.get("/learn?token=secret").text
    assert "спрашивали 2 раза" in page and "скидку" not in page and "отмечено как ошибка" in page
    assert "атриуме" not in page

    ids = ",".join(str(r["id"]) for r in services.db.to_learn() if "парковка" in r["question"])
    bad = client.post("/learn/add", data={"ids": ids, "question": "Есть ли парковка?",
                                          "answer": "Есть, 5 555 ₽ в месяц."}).text
    assert "нет в прайсе" in bad and "5 555" not in (services.settings.kb_dir / "faq.md").read_text(encoding="utf-8")

    r = client.post("/learn/add", data={"ids": ids, "question": "Есть ли парковка?",
                                        "answer": "Да, бесплатная парковка на 300 мест."}).text
    assert "Добавлено в базу знаний" in r and "бот теперь отвечает сам" in r
    assert "## Есть ли парковка?" in (services.settings.kb_dir / "faq.md").read_text(encoding="utf-8")
    assert "парковка для клиентов" not in client.get("/learn").text

    lightbox = [r["id"] for r in services.db.to_learn()]
    client.post("/learn/hide", data={"ids": ",".join(map(str, lightbox))})
    assert "Учить пока нечему" in client.get("/learn").text


def test_health_without_token_and_daily_backup(tmp_path):
    import datetime as dt
    from app.backup import backup_dir, daily_backup
    client, services = make_client(tmp_path)
    assert client.get("/health").json() == {"ok": True}
    for day in range(1, 18):
        daily_backup(services, dt.date(2026, 10, day))
    assert daily_backup(services, dt.date(2026, 10, 17)) is None, "вторая копия за день не нужна"
    copies = sorted(p.name for p in backup_dir(services).glob("app-*.db"))
    assert len(copies) == 14 and copies[-1] == "app-2026-10-17.db"


def test_edit_add_delete_price_in_browser(tmp_path):
    client, services = make_client(tmp_path)
    first = services.kb.prices[0]
    assert client.get("/kb/price/0?token=secret").status_code == 200
    r = client.post("/kb/price/0", data={"format": first.format, "placement": first.placement,
                                         "period": first.period, "price_rub": "33 000", "notes": first.notes})
    assert "Сохранено" in r.text and services.kb.prices[0].price_rub == 33000
    assert 33000 in services.kb.allowed_amounts()
    n = len(services.kb.prices)
    client.post("/kb/price/new", data={"format": "Флаг", "placement": "ТЦ Тест — вход", "period": "1 месяц",
                                       "price_rub": "12000", "notes": ""})
    assert len(services.kb.prices) == n + 1 and services.kb.prices[-1].format == "Флаг"
    bad = client.post("/kb/price/new", data={"format": "", "placement": "", "price_rub": "0"})
    assert "Нужны формат" in bad.text and len(services.kb.prices) == n + 1
    client.post(f"/kb/price/{n}", data={"delete": "1"})
    assert len(services.kb.prices) == n
    assert client.get("/kb/price/999").status_code == 404


def test_login_form_and_logout(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    page = client.get("/")
    assert page.status_code == 401 and "Войти" in page.text
    assert "Неверный токен" in client.post("/login", data={"token": "nope"}).text
    r = client.post("/login", data={"token": "secret"}, follow_redirects=False)
    assert r.status_code == 303 and "admin_token" in r.headers["set-cookie"]
    assert client.get("/").status_code == 200
    client.get("/logout")
    assert client.get("/").status_code == 401
    assert client.get("/kb/price/999").status_code == 401  # без входа 404 не раскрываем


def test_bot_status_and_connect_guide(tmp_path):
    client, services = make_client(tmp_path)
    services.settings = replace(services.settings, telegram_bot_token="123:abc")
    services.bot_status = {"ok": True, "username": "sales_helper_bot", "business": False}
    page = client.get("/?token=secret").text
    assert "Как подключить бота" in page and "Business Mode" in page and "@sales_helper_bot" in page
    services.bot_status = {"ok": False, "error": "Telegram не принял токен"}
    assert "Telegram не принял токен" in client.get("/").text


def test_activity_feed_and_resume_button(tmp_path):
    client, services = make_client(tmp_path)
    services.db.pause_chat("c", 42, 30)
    services.chat_names[42] = "Иван"
    services.event("Иван: менеджер написал сам — бот молчит в этом чате 30 мин")
    page = client.get("/?token=secret").text
    assert "Что делает бот" in page and "Иван до" in page and "Снять паузы" in page
    client.post("/resume")
    assert not services.db.active_pauses()
    assert "Паузы сняты из админки (1)" in client.get("/").text


def test_stranger_connection_approve_and_reject(tmp_path):
    client, services = make_client(tmp_path)
    services.db.upsert_connection("own", 1, 1, True, True, "draft")       # первый менеджер — свой
    services.db.upsert_connection("alien", 2, 2, True, True, "draft")     # чужой — ждёт разрешения
    services.db.upsert_connection("alien2", 3, 3, True, True, "draft")
    assert not services.db.get_connection("alien").approved
    page = client.get("/?token=secret").text
    assert "Разрешить" in page and "Отклонить" in page
    client.post("/connections/alien/approve")
    client.post("/connections/alien2/reject")
    assert services.db.get_connection("alien").approved and services.db.get_connection("alien2") is None


def test_login_lockout_after_wrong_tokens(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    for _ in range(5):
        assert client.post("/login", data={"token": "guess"}).status_code == 401
    assert client.post("/login", data={"token": "secret"}).status_code == 429, "подбор остановлен"


def test_stale_cookie_does_not_lock_owner_out(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    client.cookies.set("admin_token", "old-token")  # ADMIN_TOKEN сменили, а cookie осталась
    for _ in range(10):
        assert client.get("/").status_code == 401
    assert client.get("/?token=secret").status_code == 200


def test_model_usage_on_overview(tmp_path):
    client, services = make_client(tmp_path)
    client.post("/sandbox", data={"question": "Сколько стоит экран в атриуме?"})
    assert services.usage_today()[0] == 1, "тестовый чат тоже расходует модель и считается"
    assert "Модель сегодня" in client.get("/").text


def test_notes_edit_upload_and_path_safety(tmp_path):
    client, services = make_client(tmp_path)
    client.get("/?token=secret")
    text = "---\nтц: ВЕЕР Молл\n---\n# Макеты\n\n## Требования к макету для экрана\nMP4, 10 секунд.\n"
    r = client.post("/kb/note", data={"path": "ВЕЕР/Макеты.md", "text": text})
    assert "Сохранено" in r.text and (tmp_path / "kb" / "notes" / "ВЕЕР" / "Макеты.md").exists()
    assert any("MP4" in c.text for c in services.kb.faq), "бот сразу знает новую заметку"
    assert "Заметки · 1" in client.get("/kb").text
    bad = client.post("/kb/note", data={"path": "../../etc/passwd.md", "text": text})
    assert "Имя файла" in bad.text and not (tmp_path / "etc").exists()
    up = client.post("/kb/notes/upload", files=[("files", ("Трафик.md", "## Трафик ВЕЕР?\n500 тысяч в месяц.\n".encode()))])
    assert "Загружено заметок: 1" in up.text
    client.post("/kb/note", data={"path": "ВЕЕР/Макеты.md", "text": text, "delete": "1"})
    assert not (tmp_path / "kb" / "notes" / "ВЕЕР" / "Макеты.md").exists()


def test_token_never_stays_in_address_or_access_log(tmp_path):
    import logging

    from app.__main__ import HideToken
    client, _ = make_client(tmp_path, login=False)
    r = client.get("/kb?f=1&token=secret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/kb?f=1", "в адресной строке токена не остаётся"
    assert "admin_token=secret" in r.headers["set-cookie"]
    (tmp_path / "other").mkdir()
    fresh, _ = make_client(tmp_path / "other", login=False)
    assert fresh.post("/sandbox?token=secret", data={"question": "Сколько стоит экран?"}).status_code == 401
    assert "Неверный токен" in fresh.get("/?token=guess").text
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", "/?token=secret", "1.1", 303), None)
    HideToken().filter(record)
    assert "secret" not in record.getMessage() and "token=***" in record.getMessage()
