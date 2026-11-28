"""Админка как рабочее место: ответ клиенту с сайта, разбор ошибки в переписке, уведомления и поиск ⌘K."""
import asyncio

import pytest

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
    assert asyncio.run(services.decide_draft(escalated, "send")) == "edit_required", "у передачи нужен свой текст"
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


@pytest.mark.parametrize("reason", ["price_mismatch:20000", "period_mismatch:сутки", "terms_mismatch:монтаж",
                                    "venue_mismatch:ДЕПО", "format_mismatch:экран", "vat_unknown"])
def test_old_rejected_draft_cannot_be_sent_directly(tmp_path, reason):
    _, services, sent = setup(tmp_path)
    draft = log(services, 5, "Какие условия?", D.DRAFT_PENDING, "escalate", reason=reason, answer="Ошибочный ответ")
    assert asyncio.run(services.decide_draft(draft, "send", "подмена")) == "edit_required"
    assert not sent and services.db.get_dialog(draft)["status"] == D.DRAFT_PENDING


@pytest.mark.parametrize("status", [D.DRAFT_PENDING, D.ESCALATED])
def test_old_rejected_draft_in_web_requires_written_reply(tmp_path, status):
    client, services, sent = setup(tmp_path)
    answer = "Лайтбокс стоит 20 000 ₽."
    draft = log(services, 5, "Сколько стоит лайтбокс?", status, "escalate",
                reason="price_mismatch:20000", answer=answer)
    back = f"/dialogs?conn={CONN}&chat=5"
    url = f"/dialogs/{draft}/draft"
    page = client.post(url, data={"action": "send", "back": back}).text
    assert not sent and services.db.get_dialog(draft)["status"] == status
    thread = page.split('class="thread"')[1]
    assert "value='send'" not in thread and f">{answer}</textarea>" not in thread
    assert "Написать свой ответ" in thread and "value='skip'" in thread
    unchanged = client.post(url, data={"action": "edit", "text": answer.replace("стоит ", "стоит\r\n")},
                            follow_redirects=False)
    assert "done=edit_required" in unchanged.headers["location"] and not sent
    client.post(url, data={"action": "edit", "text": "Лайтбокс стоит 25 000 ₽ за месяц."})
    assert sent == [(CONN, 5, "Лайтбокс стоит 25 000 ₽ за месяц.")]
    assert services.db.get_dialog(draft)["status"] == D.DRAFT_EDITED


@pytest.mark.parametrize(("question", "missing"), [
    ("С 9 декабря свободен ДП-Ф2?", "дату окончания"), ("Добрый день, свободен ДП-Ф2 по 15 декабря?", "дату начала"),
])
def test_sandbox_hands_incomplete_period_to_manager(tmp_path, question, missing):
    client, services, sent = setup(tmp_path)
    page = client.post("/sandbox", data={"question": question})
    row = services.db.recent_dialogs(1)[0]
    assert page.status_code == 200 and f"уточните у клиента {missing}" in page.text
    assert (row["decision"], row["answer"]) == ("escalate", services.assistant.holding_message)
    assert not sent


def test_thread_offers_reply_and_reports_result(tmp_path):
    client, services, sent = setup(tmp_path)
    draft = log(services, 5, "А ролик вы делаете?", D.DRAFT_PENDING, answer="Ролик нужен ваш.")
    escalated = log(services, 6, "Можно скидку?", D.ESCALATED, "escalate", answer="Уточню и вернусь.")
    log(services, 0, "проверка", D.SANDBOX, conn="sandbox:x")

    page = client.get(f"/dialogs?conn={CONN}&chat=5").text.split('class="thread"')[1]
    assert f"action='/dialogs/{draft}/draft'" in page and "value='send'>Отправить клиенту" in page
    assert "value='skip'>Пропустить" in page and ">Ролик нужен ваш.</textarea>" in page, "исправить — с черновиком в поле"
    other = client.get(f"/dialogs?conn={CONN}&chat=6").text.split('class="thread"')[1]
    assert "class='composer'" in other and "value='send'" not in other, "у передачи — только свой ответ, поле внизу"
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


def test_learn_is_a_queue_that_keeps_place_and_typed_answer(tmp_path):
    client, services, _ = setup(tmp_path)
    for q in ("Есть ли парковка?", "Можно ли с собакой?", "Работаете ли в праздники?"):
        log(services, 5, q, D.ESCALATED, "escalate", reason="no_context")
    page = client.get("/learn").text
    assert "1 из 3" in page and page.count('action="/learn/add"') == 1, "на экране один вопрос"
    assert page.count('class="qitem"') == 2 and "Пропустить пока" in page
    second = client.get("/learn?tab=questions&n=1").text
    assert "2 из 3" in second and 'name="n" value="1"' in second

    ids = ",".join(str(g) for g in [r["id"] for r in services.db.to_learn() if "собак" in r["question"]])
    bad = client.post("/learn/add", data={"ids": ids, "question": "Можно ли с собакой?", "answer": "Да, 7 777 ₽.",
                                          "n": "1"}).text
    assert "нет в прайсе" in bad and ">Да, 7 777 ₽.</textarea>" in bad, "написанный ответ не пропал"
    r = client.post("/learn/hide", data={"ids": ids, "n": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/learn?tab=questions&n=1"
    assert "2 из 2" in client.get(r.headers["location"]).text, "на место скрытого встал следующий"
    assert "Новых фактов из переписки нет" in client.get("/learn?tab=facts").text


def test_platforms_button_labels_repeats_and_one_reply_for_all(tmp_path):
    # 13.11, первый живой день MAX: «[просит менеджера]» вместо текста, три одинаковых вопроса — три открытые формы,
    # Telegram и MAX в одном списке
    client, services, sent = setup(tmp_path)
    services.db.upsert_connection("max:2", 2, 2, True, True, "draft")
    services.db.approve_connection("max:2")
    services.client_senders["max"] = services.client_senders["telegram"]
    log(services, 5, "Сколько стоит экран?", D.SENT_AUTO, answer="30 000 ₽.")
    button = log(services, 9, "[просит менеджера]", D.ESCALATED, "escalate", conn="max:2")
    first = log(services, 9, "Скинь кп по вееру", D.ESCALATED, "escalate", conn="max:2")
    log(services, 9, "Скинь кп по вееру", D.ESCALATED, "escalate", conn="max:2")
    last = log(services, 9, "Скинь кп по вееру", D.ESCALATED, "escalate", answer="Уточню и вернусь.", conn="max:2")

    everything = client.get("/dialogs").text
    assert ">Все<span class=\"c\">2</span>" in everything and ">Telegram<span class=\"c\">1</span>" in everything
    assert ">MAX<span class=\"c\">1</span>" in everything and 'class="ch ch-max">MAX</span>' in everything
    only_max = client.get("/dialogs?ch=max").text.split('class="items"')[1].split('class="thread"')[0]
    assert "Скинь кп" in only_max and "Сколько стоит экран" not in only_max

    thread = client.get("/dialogs?ch=max&conn=max%3A2&chat=9").text.split('class="thread"')[1]
    assert "Нажал(а) «Позвать менеджера»" in thread and "[просит менеджера]" not in thread
    assert thread.count(">Скинь кп по вееру<") == 1 and "написал(а) 3 раза подряд" in thread
    assert thread.count("class='composer'") == 1 and f"action='/dialogs/{last}/draft'" in thread
    assert "Ответ клиенту в MAX" in thread and "ждут ответа: 4" in thread

    client.post(f"/dialogs/{last}/draft", data={"action": "edit", "text": "Отправила КП на почту.", "close_older": "1",
                                                "back": "/dialogs?ch=max&conn=max%3A2&chat=9"})
    assert len(sent) == 1, "один ответ — одно сообщение клиенту"
    assert {services.db.get_dialog(i)["status"] for i in (button, first, last)} == {D.DRAFT_EDITED}
    assert "class='composer'" not in client.get("/dialogs?conn=max%3A2&chat=9").text, "ждущих больше нет"


def test_paused_chat_shows_label_and_resume_button_for_that_chat_only(tmp_path):
    client, services, _ = setup(tmp_path, "auto")
    log(services, 5, "Позови менеджера", D.ESCALATED, "escalate")
    log(services, 6, "Сколько стоит экран?", D.ESCALATED, "escalate")
    services.db.pause_chat(CONN, 5, 30)
    services.db.pause_chat(CONN, 6, 30)

    page = client.get(f"/dialogs?conn={CONN}&chat=5").text
    head = page.split('class="thread"')[1]
    assert "Бот молчит до" in head and "action='/dialogs/resume'" in head and "Вернуть бота" in head
    assert page.count("бот на паузе") == 2, "в списке пометка у обоих чатов на паузе"

    back = f"/dialogs?conn={CONN}&chat=5"
    res = client.post("/dialogs/resume", data={"connection_id": CONN, "chat_id": 5, "back": back}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == back
    assert not services.db.is_paused(CONN, 5) and services.db.is_paused(CONN, 6), "второй чат остаётся на паузе"
    assert "Бот молчит до" not in client.get(back).text.split('class="thread"')[1]


def test_resume_does_not_accept_foreign_back(tmp_path):
    client, services, _ = setup(tmp_path, "auto")
    log(services, 5, "Привет", D.ESCALATED, "escalate")
    services.db.pause_chat(CONN, 5, 30)
    res = client.post("/dialogs/resume", data={"connection_id": CONN, "chat_id": 5, "back": "https://evil.test/"},
                      follow_redirects=False)
    assert res.headers["location"] == "/dialogs" and not services.db.is_paused(CONN, 5)
