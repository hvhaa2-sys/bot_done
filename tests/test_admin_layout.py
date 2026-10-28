"""Админка по выбранным референсам: меню слева со счётчиками, «Диалоги» ящиком, «Обзор» с неделей графиком."""
import time

from app import db as D
from tests.test_admin import make_client


def log(services, chat_id, question, status, decision="answer", *, conn="tg:1", reason="", ago=0.0, answer="Ответ"):
    dialog_id = services.db.log_dialog(connection_id=conn, chat_id=chat_id, question=question, answer=answer,
                                       decision=decision, reason=reason, confidence=1, status=status, sources=[],
                                       latency_ms=900, mode="auto")
    if ago:
        services.db.conn.execute("UPDATE dialogs SET created_at=? WHERE id=?", (time.time() - ago, dialog_id))
        services.db.conn.commit()
    return dialog_id


def test_sidebar_groups_sections_and_counts_waiting_drafts(tmp_path):
    client, services = make_client(tmp_path)
    log(services, 5, "Можно скидку?", D.DRAFT_PENDING, "escalate", reason="stop_topic:скидк")
    page = client.get("/kb").text
    side = page.split('<main id="main"')[0]
    assert '<span class="grp">Разговоры</span>' in side and '<span class="grp">Бот</span>' in side
    assert 'href="/kb" class="on" aria-current="page">База знаний' in side
    assert 'href="/dialogs">Диалоги<span class="n" data-key="/dialogs:1" title="ждут вас">1</span>' in side, \
        "черновик ждёт решения"
    assert 'nav a[href="/observe"]{--icon:url("data:image/svg+xml,' in page, "у каждого раздела своя иконка"


def test_dialogs_group_turns_by_chat_and_open_picked_conversation(tmp_path):
    client, services = make_client(tmp_path)
    services.chat_names.update({5: "Анна К.", 6: "Ильдар"})
    log(services, 5, "Сколько стоит экран в атриуме?", D.SENT_AUTO, ago=120)
    log(services, 6, "Нужны три ТЦ на ноябрь", D.ESCALATED, "escalate", reason="no_context", answer="")
    log(services, 5, "А на два месяца?", D.SENT_AUTO)

    page = client.get("/dialogs").text
    assert "Разговоры · 2" in page and page.count('<a class="item') == 2, "один пункт на чат, а не на вопрос"
    assert page.index("Анна К.") < page.index("Ильдар"), "свежий разговор сверху"
    assert 'class="inbox"' in page, "без выбора на телефоне виден список"

    anna = client.get("/dialogs?conn=tg:1&chat=5").text
    thread = anna.split('class="thread"')[1]
    assert thread.index("Сколько стоит экран") < thread.index("А на два месяца?"), "переписка по порядку"
    assert "Нужны три ТЦ" not in thread and 'class="inbox open"' in anna
    assert "value='/dialogs?conn=tg%3A1&amp;chat=5'" in thread, "после отметки ошибки вернёмся в тот же разговор"

    ildar = client.get("/dialogs?conn=tg:1&chat=6").text.split('class="thread"')[1]
    assert "ответа нет в базе знаний" in ildar, "видно, почему бот передал вопрос"


def test_dialogs_folder_and_search_filter_conversations(tmp_path):
    client, services = make_client(tmp_path)
    log(services, 5, "Сколько стоит экран?", D.SENT_AUTO)
    log(services, 6, "Можно скидку?", D.ESCALATED, "escalate", reason="stop_topic:скидк")
    manager = client.get("/dialogs?f=manager").text
    assert "Разговоры · 1" in manager and "Можно скидку?" in manager and "Сколько стоит экран?" not in manager
    assert ">Передано менеджеру<span class=\"c\">1</span>" in manager
    assert "Ничего не нашлось" in client.get("/dialogs?q=парковка").text


def test_overview_shows_week_chart_and_what_needs_attention(tmp_path):
    client, services = make_client(tmp_path)
    services.chat_names[7] = "Салон «Лён»"
    for _ in range(3):
        log(services, 5, "Сколько стоит экран?", D.SENT_AUTO)
    log(services, 6, "Сколько стоит экран?", D.SENT_AUTO, ago=8 * 86400)
    log(services, 7, "А ролик вы делаете?", D.DRAFT_PENDING)
    log(services, 8, "проверка", D.SANDBOX, conn="sandbox:x")

    page = client.get("/").text
    assert "Обращения клиентов за 7 дней" in page and '<div class="big">4<span class="delta">+300% к прошлой неделе' in page
    assert 'class="chart"' in page and "<b>4</b>сегодня" in page, "тестовый чат в обращения не входит"
    assert "Бот ответил сам" in page and ">75%<" in page
    todo = page.split("Требуют внимания")[1].split("</ul>")[0]
    assert "Черновик" in todo and "Салон «Лён»" in todo and "conn=tg%3A1&amp;chat=7" in todo


def test_daily_counts_by_local_day_without_sandbox(tmp_path):
    _, services = make_client(tmp_path)
    log(services, 5, "Вопрос", D.SENT_AUTO)
    log(services, 5, "Вопрос", D.ESCALATED, "escalate")
    log(services, 5, "Вопрос в наблюдении", D.WATCH, "watch", answer="")
    log(services, 5, "Тест", D.SANDBOX, conn="sandbox:x")
    log(services, 5, "Вчера", D.SENT_AUTO, ago=86400)
    days = services.db.daily(7)
    assert len(days) == 7 and days[-1]["day"] > days[0]["day"]
    today = days[-1]
    assert (today["questions"], today["bot"], today["auto"], today["escalated"]) == (3, 2, 1, 1)
    assert days[-2]["questions"] == 1 and today["latency_ms"] == 900


def test_live_pages_poll_and_tag_blocks_that_update_themselves(tmp_path):
    client, services = make_client(tmp_path)
    dialog_id = log(services, 5, "Сколько стоит экран?", D.SENT_AUTO)
    home = client.get("/").text
    assert 'data-poll="15"' in home and 'data-live="nav"' in home
    assert all(f'data-live="{key}"' in home for key in ("week", "todo", "status")), "блоки «Обзора» обновляются сами"
    dialogs = client.get("/dialogs").text
    assert 'data-poll="15"' in dialogs and 'data-live="inbox"' in dialogs and 'data-live="tabs"' in dialogs
    assert f"data-key='{dialog_id}:{D.SENT_AUTO}:0'" in dialogs, "по ключу скрипт подсвечивает только новое"
    assert 'data-poll' not in client.get("/kb").text, "страницы с формами не обновляются под рукой"
    answered = client.post("/sandbox", data={"question": "Сколько стоит экран в атриуме?"}).text
    assert 'data-poll' not in answered, "ответ тестового чата пришёл POST-запросом — его не перезапрашиваем"


def test_pages_wire_waiting_keys_and_unsaved_settings(tmp_path):
    from app.admin import exam_html
    client, services = make_client(tmp_path)
    home = client.get("/").text
    assert 'action="/sandbox" data-typing' in home and "data-enter-send" in home, "Enter спрашивает, видно «Бот думает…»"
    assert '<form method="post" action="/settings" data-dirty>' in client.get("/settings").text
    running = exam_html(services, {"running": True, "done": 3, "total": 12, "error": ""})
    assert 'data-progress="0.250"' in running and 'aria-valuenow="3"' in running
    assert "prefers-reduced-motion:reduce" in home and "@view-transition" in home, "движение отключается по настройке"
