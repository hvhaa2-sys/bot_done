"""Админка как рабочее место: ответ клиенту с сайта, разбор ошибки в переписке, уведомления и поиск ⌘K."""
import asyncio

from app import db as D
from tests.test_admin import make_client
from tests.test_admin_layout import log

CONN = "tg:1"


def setup(tmp_path, mode="draft"):
    client, services = make_client(tmp_path)
    services.db.upsert_connection(CONN, 1, 1, True, True, mode)
    sent = []

    async def sender(conn_id, chat_id, text):
        sent.append((conn_id, chat_id, text))
        return True

    services.client_senders["telegram"] = sender
    return client, services, sent


def test_draft_is_sent_edited_or_skipped_from_admin_once(tmp_path):
    _, services, sent = setup(tmp_path)
    services.db.set_settings({"signature": "Анна"})
    draft = log(services, 5, "А ролик вы делаете?", D.DRAFT_PENDING, answer="Ролик нужен ваш, до 15 секунд.")
    assert asyncio.run(services.decide_draft(draft, "send")) == "sent"
    assert sent[-1][:2] == (CONN, 5) and sent[-1][2].startswith("Ролик нужен ваш"), "черновик уходит как в Telegram"
    assert services.db.get_dialog(draft)["status"] == D.DRAFT_SENT
    assert asyncio.run(services.decide_draft(draft, "send")) == "handled" and len(sent) == 1, "второе нажатие не шлёт"

    escalated = log(services, 6, "Можно скидку?", D.ESCALATED, "escalate", answer="Уточню и вернусь.")
    assert asyncio.run(services.decide_draft(escalated, "send")) == "empty", "у передачи нет черновика — нужен свой текст"
    assert asyncio.run(services.decide_draft(escalated, "edit", "  Скидка от 3 месяцев — 10%.  ")) == "sent"
    row = services.db.get_dialog(escalated)
    assert row["status"] == D.DRAFT_EDITED and row["answer"] == "Скидка от 3 месяцев — 10%." == sent[-1][2]

    skipped = log(services, 7, "Привет", D.DRAFT_PENDING, answer="Здравствуйте!")
    assert asyncio.run(services.decide_draft(skipped, "skip")) == "skipped" and len(sent) == 2
    assert services.db.get_dialog(skipped)["status"] == D.DRAFT_REJECTED


def test_draft_from_admin_respects_mode_channel_and_failures(tmp_path):
    _, services, sent = setup(tmp_path, mode="observe")
    draft = log(services, 5, "Сколько стоит экран?", D.DRAFT_PENDING, answer="30 000 ₽ в месяц.")
    assert asyncio.run(services.decide_draft(draft, "send")) == "observe" and not sent, "в наблюдении бот не пишет"
    services.db.set_mode("draft", connection_id=CONN)
    services.client_senders.clear()
    assert asyncio.run(services.decide_draft(draft, "send")) == "no_channel"

    async def broken(*_):
        return False

    services.client_senders["telegram"] = broken
    assert asyncio.run(services.decide_draft(draft, "send")) == "failed"
    row = services.db.get_dialog(draft)
    assert row["status"] == D.DRAFT_PENDING and row["answer"] == "30 000 ₽ в месяц.", "черновик вернулся, можно ещё раз"


def test_thread_offers_reply_and_reports_result(tmp_path):
    client, services, sent = setup(tmp_path)
    draft = log(services, 5, "А ролик вы делаете?", D.DRAFT_PENDING, answer="Ролик нужен ваш.")
    escalated = log(services, 6, "Можно скидку?", D.ESCALATED, "escalate", answer="Уточню и вернусь.")
    log(services, 0, "проверка", D.SANDBOX, conn="sandbox:x")

    page = client.get(f"/dialogs?conn={CONN}&chat=5").text.split('class="thread"')[1]
    assert f"action='/dialogs/{draft}/draft'" in page and "value='send'>Отправить клиенту" in page
    assert "value='skip'>Пропустить" in page and ">Ролик нужен ваш.</textarea>" in page, "исправить — с черновиком в поле"
    other = client.get(f"/dialogs?conn={CONN}&chat=6").text.split('class="thread"')[1]
    assert "Ответить клиенту" in other and "value='send'" not in other, "у передачи — только свой ответ"
    assert "/draft'" not in client.get("/dialogs?conn=sandbox%3Ax&chat=0").text, "в тестовом чате отвечать некому"

    r = client.post(f"/dialogs/{escalated}/draft", data={
        "action": "edit", "text": "Скидки обсуждает менеджер.", "back": f"/dialogs?conn={CONN}&chat=6"},
        follow_redirects=False)
    assert r.headers["location"] == f"/dialogs?conn={CONN}&chat=6&done=sent#d{escalated}"
    assert sent[-1][2] == "Скидки обсуждает менеджер." and "Отправлено клиенту." in client.get(r.headers["location"]).text
    assert "done=" not in client.post(f"/dialogs/{draft}/draft", data={"action": "send", "back": "https://evil"},
                                      follow_redirects=False).headers["location"].split("?")[0], "назад — только в «Диалоги»"


def test_flag_opens_review_right_under_the_answer(tmp_path):
    client, services, _ = setup(tmp_path)
    answer = log(services, 5, "Сколько стоит экран в атриуме?", D.SENT_AUTO, answer="Экран — 25 000 ₽.")
    back = f"/dialogs?conn={CONN}&chat=5"
    r = client.post(f"/dialogs/{answer}/flag", data={"flagged": "1", "back": back}, follow_redirects=False)
    assert r.headers["location"] == f"{back}#d{answer}"
    thread = client.get(back).text.split('class="thread"')[1]
    assert "<details class='review' open><summary>Разобрать ошибку" in thread
    assert f'id="r{answer}-error-type"' in thread and f"action=\"/dialogs/{answer}/review\"" in thread
    client.post(f"/dialogs/{answer}/review", data={"error_type": "facts", "expected_decision": "answer",
                                                   "corrected_answer": "Экран — 30 000 ₽.", "must_contain": "30000",
                                                   "back": back})
    assert "<details class='review'><summary>Разбор сохранён" in client.get(back).text, "сохранённый разбор свёрнут"


def test_waiting_feed_and_search(tmp_path):
    client, services, _ = setup(tmp_path)
    services.chat_names[5] = "Анна К."
    draft = log(services, 5, "А ролик вы делаете?", D.DRAFT_PENDING, answer="Ролик нужен ваш.")
    log(services, 0, "проверка", D.SANDBOX, conn="sandbox:x")
    items = client.get("/live/waiting").json()["items"]
    assert [(i["id"], i["kind"], i["name"]) for i in items] == [(draft, "Черновик", "Анна К.")]
    assert items[0]["url"] == f"/dialogs?conn=tg%3A1&chat=5#d{draft}"

    everything = client.get("/search").json()["groups"]
    assert everything[0]["title"] == "Разделы" and len(everything[0]["items"]) > 10, "пустой запрос — все разделы"
    signature = client.get("/search?q=подпись").json()["groups"][0]["items"]
    assert signature[0]["url"] == "/settings#texts" and 'id="texts"' in client.get("/settings").text
    found = {g["title"]: g["items"] for g in client.get("/search?q=ролик").json()["groups"]}
    assert found["Разговоры с ботом"][0]["title"] == "Анна К."
    prices = {g["title"]: g["items"] for g in client.get("/search?q=атриум").json()["groups"]}["База знаний"]
    assert any(i["url"].startswith("/kb/price/") and "30 000" in i["sub"] for i in prices)


def test_sidebar_has_search_and_notifications(tmp_path):
    client, _, _ = setup(tmp_path)
    side = client.get("/").text.split('<main id="main"')[0]
    assert 'class="find" data-palette' in side and "<kbd>⌘K</kbd>" in side
    assert '<button type="button" class="notify" hidden>' in side, "кнопку показывает скрипт, если браузер умеет"
