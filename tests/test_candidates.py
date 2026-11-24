"""База знаний из переписки: кандидаты из ответов менеджера, проверка и утверждение."""
import asyncio
import json
import shutil
from dataclasses import replace

import pytest
from tests.admin_client import AdminClient as TestClient

from app.admin import create_app
from app.config import ROOT, Settings
from app.core import candidates as C
from app.core.knowledge import KnowledgeBase
from app.llm.base import LLMProvider
from app.services import Services


def run(coro):
    return asyncio.run(coro)


class JsonLLM(LLMProvider):
    """Отдаёт заданный JSON фактов на просьбу извлечь, на остальное — пустой ответ."""
    name = "json"

    def __init__(self, facts=None, raw=None):
        self.raw = raw if raw is not None else json.dumps({"facts": facts or []}, ensure_ascii=False)
        self.calls = []

    async def complete(self, system, messages):
        self.calls.append(messages[-1]["content"])
        if system == C.EXTRACT_PROMPT:
            return self.raw
        return json.dumps({"answer": "", "confidence": 1, "needs_manager": True})


def make_kb(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "faq.md").write_text("## Какие требования к макету?\nМакет в PDF, 150 dpi.\n", encoding="utf-8")
    (kb_dir / "prices.csv").write_text(
        "format,placement,period,price_rub,notes\n"
        "Цифровой экран,\"ВЕЕР Молл, Екатеринбург — атриум\",1 месяц,31500,с НДС 5%\n"
        "Баннер,\"ДЕПО, Нижний Тагил — переход\",1 месяц,25000,печать 15 400 ₽\n", encoding="utf-8")
    (kb_dir / "stop_topics.txt").write_text("скидк\n", encoding="utf-8")
    return kb_dir


def services_for(tmp_path, llm):
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=make_kb(tmp_path), llm_provider="mock",
                       exam_files="")
    return Services(settings, llm=llm)


# --- извлечение ---

def test_facts_are_parsed_and_personal_or_invented_ones_dropped():
    manager = "Экран в атриуме ВЕЕР стоит 31 500 ₽ в месяц, ролик до 15 секунд. Для вас забронирую на ноябрь."
    llm = JsonLLM([{"question": "Сколько стоит экран в атриуме ВЕЕР?", "answer": "31 500 ₽ в месяц, ролик до 15 секунд."},
                   {"question": "Можно забронировать на ноябрь?", "answer": "Да, забронирую для вас на ноябрь."},
                   {"question": "А со скидкой?", "answer": "Со скидкой будет 25 000 ₽ в месяц, это выгодно."}])
    facts, _ = run(C.extract(llm, "Сколько стоит экран в атриуме?", manager))
    assert [f.question for f in facts] == ["Сколько стоит экран в атриуме ВЕЕР?"]  # бронь и выдуманная сумма — нет


def test_short_or_greeting_answers_give_nothing_and_bad_json_falls_back_to_the_pair():
    assert run(C.extract(JsonLLM([]), "Здравствуйте", "Добрый день!")) == ([], 0)
    facts, _ = run(C.extract(JsonLLM(raw="не JSON"), "Есть экран на фуд-корте?",
                             "Да, на фуд-корте ДЕПО есть экран, ролик до 10 секунд."))
    assert facts == [C.Fact("Есть экран на фуд-корте?", "Да, на фуд-корте ДЕПО есть экран, ролик до 10 секунд.")]


def test_broken_json_from_gigachat_still_gives_facts():
    # Так GigaChat Pro ответил на демо холодного старта: лишняя скобка в конце
    raw = '{"facts": [{"question": "Делаете ли вы ролик для экрана?", "answer": "Ролик \\"под ключ\\" не делаем, его даёт рекламодатель."}])}'
    assert C.parse_facts(raw) == [C.Fact("Делаете ли вы ролик для экрана?", 'Ролик "под ключ" не делаем, его даёт рекламодатель.')]
    assert C.parse_facts('{"facts": []}') == [] and C.parse_facts("нет фактов") is None
    facts, _ = run(C.extract(JsonLLM(raw='{"facts": [oops'), "Делаете ролик?", "Готовый ролик даёт рекламодатель, мы не делаем."))
    assert facts == [C.Fact("Делаете ролик?", "Готовый ролик даёт рекламодатель, мы не делаем.")]


def test_stray_quote_inside_answer_and_promise_sentence_do_not_lose_the_fact():
    # Так GigaChat Pro 04.11 трижды подряд ответил на демо холодного старта: кавычка после первой фразы
    raw = ('{"facts": [{"question": "Есть ли звук на экранах в ВЕЕР Молл?", "answer": "Экраны в ВЕЕР Молл работают '
           'без звука." Если нужен звук, есть аудиоролик на радио ТРЦ: MP3 до 25 секунд."}])}')
    assert C.parse_facts(raw) == [C.Fact("Есть ли звук на экранах в ВЕЕР Молл?",
                                         "Экраны в ВЕЕР Молл работают без звука. Если нужен звук, есть аудиоролик "
                                         "на радио ТРЦ: MP3 до 25 секунд.")]
    # …и так же 04.11: ответ разбит на два факта, у второго пустой вопрос, а в первом лишняя кавычка
    raw = ('{"facts": [{"question": "Можно ли рекламу на тележках в ВЕЕР Молл?", "answer": "Нет, на тележках нет.""}, '
           '{"question": "", "answer": "Из похожего есть талоны: поле 5×8 см."}]}')
    assert C.parse_facts(raw) == [C.Fact("Можно ли рекламу на тележках в ВЕЕР Молл?",
                                         "Нет, на тележках нет. Из похожего есть талоны: поле 5×8 см.")]
    manager = "Добрый день! Нет, экраны ВЕЕР работают без звука, ролик до 1.5 минут. Пришлю вам примеры роликов."
    facts, _ = run(C.extract(JsonLLM([{"question": "Есть звук на экранах ВЕЕР?", "answer": manager}]), "Есть звук?", manager))
    assert [f.answer for f in facts] == ["Добрый день! Нет, экраны ВЕЕР работают без звука, ролик до 1.5 минут."]
    assert C.raw_facts("Есть звук?", manager) == [C.Fact("Есть звук?", "Нет, экраны ВЕЕР работают без звука, ролик до 1.5 минут.")]
    assert C.raw_facts("Есть звук?", "Сейчас уточню и напишу вам.") == []
    assert C.drop_promises("Размер 1.5 м, без звука.") == "Размер 1.5 м, без звука."
    assert C.tidy("Здравствуйте!\nЭкран без звука\nПришлю примеры") == "Экран без звука Пришлю примеры"
    assert C.drop_promises("Экран без звука\nПришлю примеры\nРолик 1.5 мин.") == "Экран без звука Ролик 1.5 мин."


def test_empty_answer_of_the_model_is_asked_again_only_for_a_substantive_reply():
    # Песочница 04.11: на тот же ответ про тележки GigaChat то давал факт, то {"facts": []}
    class Seq(LLMProvider):
        name = "seq"

        def __init__(self, *raws):
            self.raws, self.calls = list(raws), 0

        async def complete(self, system, messages):
            self.calls += 1
            return self.raws.pop(0)

    fact = json.dumps({"facts": [{"question": "Есть реклама на тележках в ВЕЕР?", "answer": "Нет, рекламы на тележках нет."}]},
                      ensure_ascii=False)
    llm = Seq('{"facts": []}', fact)
    long = ("Добрый день! Нет, рекламы на тележках в ВЕЕР Молл нет. Из похожего есть брендирование парковочных "
            "талонов: поле 5×8 см. Пришлю вам пример талона.")
    facts, _ = run(C.extract(llm, "А на тележках можно рекламу?", long))
    assert llm.calls == 2 and [f.question for f in facts] == ["Есть реклама на тележках в ВЕЕР?"]
    llm = Seq('{"facts": []}', fact)
    assert run(C.extract(llm, "Ролики отправили, поставите?", "Получили, всё в порядке, с понедельника ставим ваши ролики."))[0] == []
    assert llm.calls == 1


def test_total_for_this_client_is_personal_but_third_party_contract_is_a_fact():
    # Экзамен обучения 04.11: «итого для вас» стал кандидатом, а «договор с РАО/ВОИС» выпадал как личный
    total = "Для вас по трём экранам на 2 месяца итого 189 000 ₽, расчёт сделаю завтра утром."
    fact = C.Fact("Сколько стоят 3 экрана на 2 месяца?", "Размещение на 3 экранах на 2 месяца — 189 000 ₽.")
    assert not C.keep(fact, total)
    price = "Экран в атриуме ВЕЕР — 31 500 ₽ в месяц. Вам сделаю скидку 10% на три месяца."
    assert C.keep(C.Fact("Сколько стоит экран в атриуме ВЕЕР?", "31 500 ₽ в месяц."), price)
    rao = "Если на мероприятии звучат популярные треки, организатору нужно заключить договор с РАО/ВОИС."
    assert C.raw_facts("Что оформить для музыки на промо?", rao) == [C.Fact("Что оформить для музыки на промо?", rao)]
    assert C.raw_facts("Договор готов?", "Договор согласовали, подписанный скан отправила на почту.") == []


def test_pair_without_model_drops_greetings_and_client_contacts():
    # Наблюдение на дневном лимите модели: пара берётся как есть — без «Здравствуйте» и без номера клиента
    facts = C.raw_facts("Здравствуйте! Какой длины ролик на экране в атриуме ВЕЕР? Мой номер +7 912 345-67-89",
                        "Добрый день! Ролик до 15 секунд, без звука, файл MP4 до 50 МБ.")
    assert facts == [C.Fact("Какой длины ролик на экране в атриуме ВЕЕР?", "Ролик до 15 секунд, без звука, файл MP4 до 50 МБ.")]
    assert C.raw_facts("Здравствуйте! Мой номер 89123456789", "Добрый день! Ролик до 15 секунд, без звука.") == []
    assert C.tidy("Спасибо!") == "" and C.tidy("Спасибо за вопрос! Макет делаете вы.") == "Макет делаете вы."


def test_phone_from_manager_answer_never_reaches_the_candidate():
    facts = C.raw_facts("Как с вами связаться по рекламе?", "Пишите в этот чат или звоните +7 912 345-67-89, отвечаем днём.")
    assert facts and "912" not in facts[0].answer and "[телефон]" in facts[0].answer


# --- сверка с базой ---

def test_review_note_shows_price_conflict_and_similar_answer(tmp_path):
    kb = KnowledgeBase.load(make_kb(tmp_path))
    note = C.review_note(kb, "Сколько стоит цифровой экран в атриуме ВЕЕР?", "Экран в атриуме ВЕЕР — 33 000 ₽ в месяц.")
    assert note.startswith("В прайсе другая цена") and "31 500" in note
    assert C.review_note(kb, "Сколько стоит экран в атриуме ВЕЕР?", "31 500 ₽ в месяц.") == ""
    assert C.review_note(kb, "Какие требования к макету?", "Макет в PDF.").startswith("Похожее уже есть")


def test_similar_hint_ignores_mall_names_and_common_words():
    # Песочница 04.11: «реклама на тележках в ВЕЕР Молл» получила подсказку «похоже на вакансию в ВЕЕР Молл»
    kb = KnowledgeBase.load(ROOT / "data" / "kb")
    assert C.review_note(kb, "Можно ли разместить рекламу на тележках для покупок в ВЕЕР Молл?",
                         "Нет, рекламы на тележках в ВЕЕР Молл нет.") == ""
    assert C.review_note(kb, "Есть ли звук на экранах в ВЕЕР Молл?", "Нет, экраны без звука.").startswith("Похожее")


def test_approved_fact_goes_to_the_mall_note_and_replaces_same_question(tmp_path):
    kb = KnowledgeBase.load(make_kb(tmp_path))
    path, venue = C.target_note(kb, "Сколько ролик на экране в ВЕЕР?", "До 15 секунд.")
    assert (path, venue) == ("Из переписки/ВЕЕР Молл.md", "ВЕЕР Молл")
    assert C.target_note(kb, "Делаете дизайн?", "Нет, макет готовит клиент.") == ("", "")
    note = tmp_path / "notes" / path
    C.add_to_note(note, venue, "Какой длины ролик на экране в ВЕЕР", "До 15 секунд.")
    C.add_to_note(note, venue, "Какой длины ролик на экране в ВЕЕР?", "До 10 секунд.")
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\nтц: ВЕЕР Молл\n") and text.count("## Какой длины ролик") == 1 and "До 10 секунд." in text


def test_similar_candidates_are_grouped():
    rows = [{"question": "Сколько стоит экран в атриуме?"}, {"question": "сколько стоит экран атриум ВЕЕР"},
            {"question": "Есть ли парковка?"}]
    groups = C.group(rows)
    assert [len(g) for g in groups] == [2, 1]


# --- полный цикл ---

def test_candidate_is_invisible_until_approved_then_bot_answers(tmp_path):
    fact = {"question": "Какой длины ролик на экране в атриуме ВЕЕР?", "answer": "Ролик до 15 секунд, без звука."}
    services = services_for(tmp_path, JsonLLM([fact]))
    added = run(services.collect_candidates(source="chat", connection_id="c", chat_id=1, dialog_id=None,
                                            client_question="а ролик какой длины?",
                                            manager_text="В атриуме ВЕЕР ролик до 15 секунд, без звука."))
    assert added == 1 and services.db.candidates()[0]["question"] == fact["question"]
    assert not any("15 секунд" in c.text for c in services.kb.chunks)  # до утверждения бот не видит
    cid = services.db.candidates()[0]["id"]
    ok, msg = services.approve_candidates([cid], fact["question"], fact["answer"])
    assert ok and "Опубликована версия базы 2" in msg and services.kb_version == 2
    assert services.db.candidates() == [] and services.db.get_candidate(cid)["status"] == "approved"
    hit = services.kb.retrieve("какой длины ролик в атриуме веер?")[0]
    assert "15 секунд" in hit.chunk.text and hit.chunk.text.startswith("ВЕЕР Молл.")


def test_amount_not_from_price_list_blocks_approval(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    cid = services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="chat", client_question="q",
                                    manager_text="m", question="Сколько стоит экран в атриуме ВЕЕР?", answer="33 000 ₽ в месяц.")
    ok, error = services.approve_candidates([cid], "Сколько стоит экран в атриуме ВЕЕР?", "33 000 ₽ в месяц.")
    assert not ok and "нет в прайсе" in error and services.db.get_candidate(cid)["status"] == "new"


def test_amount_of_another_price_row_blocks_approval(tmp_path):
    # Песочница 04.11: «фотозона у ёлки ДЕПО — 12 000 ₽» утвердилась, потому что 12 000 ₽ есть у другой позиции
    services = services_for(tmp_path, JsonLLM())
    cid = services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="chat", client_question="q",
                                    manager_text="m", question="Сколько стоит экран в атриуме ВЕЕР?",
                                    answer="Экран в атриуме ВЕЕР — 25 000 ₽ в месяц.")
    ok, error = services.approve_candidates([cid], "Сколько стоит экран в атриуме ВЕЕР?", "Экран в атриуме ВЕЕР — 25 000 ₽ в месяц.")
    assert not ok and error.startswith("В прайсе другая цена") and "31 500" in error
    assert services.db.get_candidate(cid)["status"] == "new"


def test_admin_shows_candidates_and_approves_or_rejects(tmp_path):
    fact = {"question": "Делаете ли вы дизайн баннера?", "answer": "Дизайн не делаем, макет готовит рекламодатель."}
    services = services_for(tmp_path, JsonLLM([fact]))
    services.settings = replace(services.settings, admin_token="secret")
    run(services.collect_candidates(source="card", connection_id="c", chat_id=1, dialog_id=7,
                                    client_question="а дизайн сделаете?",
                                    manager_text="Дизайн не делаем, макет готовит рекламодатель."))
    run(services.collect_candidates(source="card", connection_id="c", chat_id=2, dialog_id=8,
                                    client_question="где посмотреть требования?",
                                    manager_text="Дизайн не делаем, макет готовит рекламодатель, требования пришлю."))
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", "secret")
    page = client.get("/learn", params={"token": "secret"}).text
    assert "Из переписки" in page and "Делаете ли вы дизайн баннера?" in page and "Откуда взято" in page
    ids = ",".join(str(r["id"]) for r in services.db.candidates())
    resp = client.post("/candidates/approve",
                       data={"ids": ids, "question": fact["question"], "answer": fact["answer"], "scope": "business"})
    assert resp.status_code == 200 and "Факт утверждён" in resp.text and services.db.candidates() == []
    note = services.settings.kb_dir / "notes" / "Из переписки" / "Для всех площадок.md"
    assert fact["answer"] in note.read_text(encoding="utf-8")


def test_growth_counts_weeks_with_questions_and_approved_facts(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    services.db.log_dialog(connection_id="c", chat_id=1, question="q", answer="a", decision="answer", reason="",
                           confidence=1.0, status="sent_auto", sources=[], latency_ms=10, mode="auto")
    cid = services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="chat", client_question="q",
                                    manager_text="m", question="Q?", answer="A")
    services.db.decide_candidates([cid], "approved")
    last = services.db.growth()[-1]
    assert last["questions"] == 1 and last["answered"] == 1 and last["facts"] == 1


@pytest.fixture
def kb_copy(tmp_path):
    dst = tmp_path / "kb"
    shutil.copytree(ROOT / "tests" / "fixtures" / "kb", dst)
    return dst


# --- Telegram: ответы менеджера и кнопки в сводке ---

OWNER, CLIENT, CONN = 100, 555, "bc-cand"


def tg_env(tmp_path, llm):
    import datetime as dt

    from aiogram import Bot

    from app.channels.telegram_business import build_dispatcher
    services = services_for(tmp_path, llm)
    run(services.update_settings({"merge_seconds": "0"}))
    dp, bot, sent = build_dispatcher(services), Bot("42:TEST"), []

    async def record(chat_id, text, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, **kwargs})
    bot.send_message = record  # type: ignore[method-assign]

    async def fake_request(bot, method, timeout=None):
        return True
    bot.session.make_request = fake_request  # type: ignore[method-assign]
    now = int(dt.datetime.now().timestamp())

    def feed(payload):
        from aiogram.types import Update
        run(dp.feed_update(bot, Update.model_validate({"update_id": 1, **payload}, context={"bot": bot})))

    def message(text, from_id=CLIENT):
        return {"business_message": {"message_id": 1, "date": now, "business_connection_id": CONN, "text": text,
                                     "chat": {"id": CLIENT, "type": "private", "first_name": "Client"},
                                     "from": {"id": from_id, "is_bot": False, "first_name": "X"}}}
    feed({"business_connection": {"id": CONN, "user": {"id": OWNER, "is_bot": False, "first_name": "M"},
                                  "user_chat_id": OWNER, "date": now, "is_enabled": True, "rights": {"can_reply": True}}})
    return services, feed, message, sent, now


def test_manager_answer_in_client_chat_becomes_candidate(tmp_path):
    fact = {"question": "Какой длины ролик на экране в атриуме ВЕЕР?", "answer": "Ролик до 15 секунд, без звука."}
    services, feed, message, sent, _ = tg_env(tmp_path, JsonLLM([fact]))
    feed(message("какой длины ролик на экране в атриуме веер?"))
    feed(message("В атриуме ВЕЕР ролик до 15 секунд, без звука.", from_id=OWNER))
    rows = services.db.candidates()
    assert len(rows) == 1 and rows[0]["source"] == "chat" and rows[0]["client_question"].startswith("какой длины")
    assert services.db.is_paused(CONN, CLIENT)  # пауза, как и раньше, — бот не перебивает менеджера


def test_summary_offers_candidates_and_button_approves(tmp_path):
    from app.channels.telegram_business import build_candidates_message
    fact = {"question": "Делаете ли вы дизайн баннера в ВЕЕР?", "answer": "Дизайн не делаем, макет готовит рекламодатель."}
    services, feed, message, sent, now = tg_env(tmp_path, JsonLLM([fact]))
    feed(message("а дизайн в ВЕЕР сделаете?"))
    feed(message("Дизайн не делаем, макет готовит рекламодатель.", from_id=OWNER))
    text, markup = build_candidates_message(services, services.db.get_connection(CONN))
    assert "Предложения в базу знаний" in text and fact["question"] in text
    ok = markup.inline_keyboard[0][0].callback_data
    feed({"callback_query": {"id": "1", "chat_instance": "x", "data": ok,
                             "from": {"id": OWNER, "is_bot": False, "first_name": "M"}}})
    assert services.db.candidates() == []
    assert any("макет готовит рекламодатель" in c.text for c in services.kb.chunks)
    # чужой человек нажать не может
    cid = services.db.add_candidate(connection_id=CONN, chat_id=CLIENT, dialog_id=None, source="chat",
                                    client_question="q", manager_text="m", question="Q?", answer="Ответ для базы знаний.")
    feed({"callback_query": {"id": "2", "chat_instance": "x", "data": f"cand:ok:{cid}",
                             "from": {"id": 999, "is_bot": False, "first_name": "Чужой"}}})
    assert services.db.get_candidate(cid)["status"] == "new"
