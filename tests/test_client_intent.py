"""Синтетические текущие реплики и replay; ни сети, ни рабочей БД."""
import asyncio
import json

import pytest

from app.core.assistant import ANSWER, ESCALATE, Assistant
from app.core.client_intent import active_request
from app.core.conversation import CLARIFICATIONS, MAX_CLARIFICATIONS, Conversation
from app.core.knowledge import KnowledgeBase, PriceRow, parse_budget
from app.llm.base import LLMProvider

CONFUSED = ("На рекламу в ДЕПО есть максимум 10 тысяч, это прям потолок за всё. "
            "Хотя на сам показ можем 25 выделить, а макет только чтоб в те первые 10 вошёл. На месяц хватит?")


class NoLLM(LLMProvider):
    name = "no-network"

    async def complete(self, system, messages):
        pytest.fail("Неоднозначный бюджет должен уточняться до вызова LLM")


@pytest.fixture
def kb():
    return KnowledgeBase([], [
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 15000, ""),
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — вход", "1 месяц", 20000, ""),
        PriceRow("Баннер", "ДЕПО Молл, Нижний Тагил — галерея", "1 месяц", 30000, ""),
    ], ["скидк", "брон", "договор", "оплат", "обещ", "гарант", "пришл", "фото", "медицин"])


@pytest.mark.parametrize("question", [
    "Ладно, скидку пока проехали. Обычный прайс на месяц в ДЕПО",
    "Скидка не нужна, сколько по прайсу?",
    "Скидку нам больше не нужно. Покажите обычный прайс",
    "Мне не нужна скидка, сколько по прайсу?",
    "Про скидку в ВЕЕР пока забудьте. Обычный прайс ДЕПО",
    "Я не прошу бронировать, покажите варианты",
    "Мы не хотим бронирование, интересуют цены",
    "Не бронируйте, покажите варианты",
    "Бронь пока не нужна. Какие варианты есть?",
    "Бронирование отменяю. Интересует прайс",
    "Забудьте про скидку. Интересует обычный прайс",
    "Про «дайте скидку в ВЕЕР» проехали. Обычный прайс в ДЕПО",
    "Забудьте «забронируйте экран в ВЕЕР». Покажите цены ДЕПО",
    "«Бронировать экран в ВЕЕР» забудьте. Покажите цены ДЕПО",
    "«Дайте скидку 70%» неактуально. Какой обычный прайс?",
])
def test_only_explicitly_abandoned_request_is_neutralized(kb, question):
    current = active_request(question)
    assert current != question
    assert kb.matched_stop_topic(current) is None
    assert active_request(current) == current
    assert "прайс" in current.lower() or "вариант" in current.lower() or "цен" in current.lower()


@pytest.mark.parametrize("question, topic", [
    ("Скидку не дадите?", "скидк"),
    ("Бронь невозможна?", "брон"),
    ("Скидка не нужна?", "скидк"),
    ("Скидка не нужна если цена ниже 10 тысяч", "скидк"),
    ("Я не прошу бронировать бесплатно, я заплачу", "брон"),
    ("Не делайте скидку 70%, дайте хотя бы 10%", "скидк"),
    ("Скидку пока проехали, дайте хотя бы 10%", "скидк"),
    ("Скидка не нужна, но можно 10%?", "скидк"),
    ("Скидка не нужна. А скидку для арендаторов дадите?", "скидк"),
    ("Я не прошу бронировать, но теперь забронируйте экран", "брон"),
    ("Скидка не нужна, как оплатить?", "оплат"),
    ("Я не прошу бронировать, договор пришлите", "договор"),
    ("Скидка не нужна, пришлите фото", "пришл"),
    ("Скидка не нужна, у нас медицинская реклама", "медицин"),
    ("Менеджер сказал: «Скидку согласовали». Сколько выйдет?", "скидк"),
    ("Менеджер сказал: «Бронь не нужна». Так экран наш?", "брон"),
    ("Менеджер сказал, бронь не нужна. Так экран наш?", "брон"),
    ("Менеджер сказал: «Скидка не нужна, договор подпишем». Какие цены?", "скидк"),
    ("Забудьте «менеджер обещал скидку». Но договор-то есть?", "скидк"),
    ("Про «бронь и договор» проехали. Какие цены?", "брон"),
    ("Про скидку и договор забудьте. Какие цены?", "скидк"),
    ("Про «скидку» забудьте если будет другая цена", "скидк"),
])
def test_active_stop_reason_is_preserved(kb, question, topic):
    assert kb.matched_stop_topic(active_request(question)) == topic


def test_withdrawal_keeps_each_other_independent_stop_reason():
    question = "Скидка не нужна, пришлите договор и фото, как оплатить?"
    for topic in ("пришл", "договор", "фото", "оплат"):
        assert KnowledgeBase([], [], [topic]).matched_stop_topic(active_request(question)) == topic


@pytest.mark.parametrize("topic, question", [
    ("рассроч", "Скидка в рассрочку не нужна, сколько по прайсу?"),
    ("кредит", "Бронь в кредит не нужна, покажите варианты"),
    ("ип!", "Скидка по ИП не нужна, сколько по прайсу?"),
    ("2027", "Скидка в 2027 не нужна, сколько по прайсу?"),
])
def test_cancellation_cannot_swallow_other_configured_topics(topic, question):
    assert KnowledgeBase([], [], [topic]).matched_stop_topic(active_request(question)) == topic


@pytest.mark.parametrize("question", [
    "Ладно, скидку в ВЕЕР пока проехали. Обычный прайс в ДЕПО на месяц",
    "Про «дайте скидку в ВЕЕР» проехали. Обычный прайс в ДЕПО на месяц",
])
def test_abandoned_venue_does_not_override_current_selection(kb, question):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    state.observe(kb, question)
    assert state.venue.startswith("ДЕПО") and state.changed
    assert not state.multiple_venues
    assert "веер" not in state.query(question).lower()
    assert "скидк" not in state.query(question).lower()
    assert "экран" in state.format
    assert "цена" in state.intent


@pytest.mark.parametrize("question", [
    "Ладно, скидку в ВЕЕР пока проехали. Обычный прайс на экран в ДЕПО на месяц",
    "Скидка не нужна, сколько по прайсу за экран в ДЕПО на месяц?",
    "Я не прошу бронировать, покажите варианты экрана в ДЕПО на месяц",
])
def test_assistant_returns_to_regular_price_after_escalation(kb, question):
    class FixedLLM(LLMProvider):
        name = "fixed"

        def __init__(self):
            self.calls = []

        async def complete(self, system, messages):
            self.calls.append((system, messages))
            return json.dumps({"answer": "Цифровой экран в ДЕПО — 20 000 ₽ за месяц.",
                               "confidence": 1, "needs_manager": False}, ensure_ascii=False)

    llm = FixedLLM()
    assistant, state = Assistant(kb, llm), Conversation()
    first = asyncio.run(assistant.answer("Скидку на экран в ВЕЕР дадите?", conversation=state))
    assert first.reason == "stop_topic:скидк" and not llm.calls
    reply = asyncio.run(assistant.answer(question, conversation=state))
    assert reply.decision == ANSWER and "20 000" in reply.text
    assert len(llm.calls) == 1
    assert "ВЕЕР" not in llm.calls[0][0]


@pytest.mark.parametrize("question", [
    CONFUSED,
    "Бюджет всего 10 тысяч, но только на размещение 25 тысяч. Макет входит в эти 10",
    "Общий бюджет 15 тысяч, а на показ можем 20 тысяч",
    "Всего 25 тысяч, на размещение 20 тысяч, на макет 10 тысяч",
    "На всё максимум 10000 рублей. На аренду 25000 рублей",
    "Бюджет 10 тысяч. Общий предел 25 тысяч",
])
def test_incompatible_budgets_ask_before_llm(kb, question):
    state = Conversation(budget=50000)
    reply = asyncio.run(Assistant(kb, NoLLM()).answer(question, conversation=state))
    assert reply.decision == ANSWER and reply.reason == "clarification:budget"
    assert reply.text == CLARIFICATIONS["budget"]
    assert not reply.llm_called
    assert state.budget is None and state.budget_ambiguous
    assert state.pending == "budget" and state.clarifications == 1
    assert "бюджет:" not in state.parameters()


@pytest.mark.parametrize("correction", [
    "Я криво написал: всего можем 25 тысяч, из них до 10 на подготовку",
    "Якривонаписал:всегоможем25тысяч,изнихдо10наподготовку",
    "Общий бюджет 25000 рублей, на подготовку макета до 10000 рублей",
    "Всего 25 тысяч, включая 10 тысяч на подготовку",
    "25 тысяч на всё, из них 10 тысяч на макет",
])
def test_corrected_total_replaces_conflict_and_drives_retrieval(kb, correction):
    state = Conversation()
    state.observe(kb, CONFUSED)
    state.finish(ANSWER, "clarification:budget")
    state.observe(kb, correction)
    assert state.budget == 25000 and state.preparation_budget == 10000
    assert not state.budget_ambiguous and state.pending == ""
    assert state.clarification(kb, correction) is None
    assert parse_budget(state.query(correction).lower()) == 25000
    state.observe(kb, "А на месяц?")
    assert state.budget == 25000 and not state.budget_ambiguous


@pytest.mark.parametrize("question, total", [
    ("Бюджет от 10 до 25 тысяч", 25000),
    ("На рекламу от 10 тысяч до 25 тысяч", 25000),
    ("Бюджет 10–25 тысяч", 25000),
    ("Бюджет 10 000–25 000 рублей", 25000),
    ("Не больше 25 тысяч", 25000),
    ("Не более 25 тысяч", 25000),
    ("На размещение 25 тысяч, на макет 10 тысяч", 35000),
    ("На размещение 15 тысяч, на подготовку 10 тысяч, всего 25 тысяч", 25000),
    ("Всего 25 тысяч, из них 10 на подготовку и 15 на размещение", 25000),
    ("25 тысяч включая 10 на подготовку", 25000),
    ("Бюджет 25 тысяч, включая 10 тысяч", 25000),
    ("Бюджет 25 тысяч, включая 10", 25000),
    ("На размещение до 15 тысяч, на макет до 10 тысяч", 25000),
    ("Всего 25 тысяч: 15 тысяч на размещение + 10 тысяч на подготовку", 25000),
    ("Бюджет 25 000 рублей, из них до 10 000 на подготовку", 25000),
])
def test_consistent_amounts_and_ranges_are_not_ambiguous(kb, question, total):
    state = Conversation()
    state.observe(kb, question)
    assert state.budget == total and not state.budget_ambiguous
    assert state.clarification(kb, question) is None
    assert parse_budget(state.query(question)) == total


def test_budget_retry_limit_and_partial_response_do_not_clear_conflict(kb):
    state = Conversation()
    assistant = Assistant(kb, NoLLM())
    replies = [asyncio.run(assistant.answer(q, conversation=state)) for q in (
        CONFUSED, "На подготовку до 10 тысяч", "Обычный прайс на месяц")]
    assert [r.reason for r in replies] == ["clarification:budget"] * MAX_CLARIFICATIONS + ["clarification_limit"]
    assert replies[-1].decision == ESCALATE
    assert state.clarifications == MAX_CLARIFICATIONS and state.budget_ambiguous
    state.observe(kb, "Всего 25 тысяч")
    assert state.budget == 25000 and not state.budget_ambiguous
    assert state.clarification(kb, "Всего 25 тысяч") is None


@pytest.mark.parametrize("status, count", [("sent_auto", 1), ("draft_sent", 1), ("sandbox", 1),
                                        ("draft_edited", 1), ("draft", 0)])
def test_replay_restores_pending_budget_only_for_delivered_questions(kb, status, count):
    turns = [{"question": CONFUSED, "answer": CLARIFICATIONS["budget"], "status": status,
              "decision": ANSWER, "reason": "clarification:budget"}]
    state = Conversation.replay(kb, turns)
    assert state.budget_ambiguous and state.budget is None
    assert state.clarifications == count
    assert state.pending == ("budget" if count else "")
    assert state.clarification(kb, "На месяц хватит?")[0] == "budget"


def test_replay_corrected_budget_does_not_resurrect_old_conflict(kb):
    turns = [
        {"question": CONFUSED, "answer": CLARIFICATIONS["budget"], "status": "sent_auto",
         "decision": ANSWER, "reason": "clarification:budget"},
        {"question": "Всего 25 тысяч, из них до 10 на подготовку", "answer": "Варианты по прайсу",
         "status": "sent_auto", "decision": ANSWER, "reason": ""},
    ]
    state = Conversation.replay(kb, turns)
    assert state.budget == 25000 and not state.budget_ambiguous
    assert state.clarifications == 0 and state.pending == ""
    assert parse_budget(state.query("А на месяц?")) == 25000


def test_budget_question_accepts_a_bare_amount(kb):
    state = Conversation()
    state.observe(kb, CONFUSED)
    state.finish(ANSWER, "clarification:budget")
    state.observe(kb, "25 тысяч")
    assert state.budget == 25000 and not state.budget_ambiguous
    assert state.clarification(kb, "25 тысяч") is None


def test_extra_allocation_preserves_known_total_and_can_reveal_conflict(kb):
    state = Conversation()
    state.observe(kb, "Общий бюджет 25 тысяч")
    state.observe(kb, "На макет до 10 тысяч")
    assert state.budget == 25000 and state.preparation_budget == 10000
    assert not state.budget_ambiguous
    state.observe(kb, "На размещение 20 тысяч")
    assert state.budget is None and state.budget_ambiguous
    assert state.clarification(kb, "На размещение 20 тысяч")[0] == "budget"


def test_quantities_are_not_budget_and_reset_clears_all_budget_fields(kb):
    state = Conversation()
    state.observe(kb, "Экран в ДЕПО на 3 месяца, 10 показов по 25 секунд")
    assert state.budget is None and not state.budget_ambiguous
    state.observe(kb, CONFUSED)
    state.finish(ANSWER, "clarification:budget")
    state.observe(kb, "Забудь, начнём сначала. Экран в ВЕЕР")
    assert state.budget is None and not state.budget_ambiguous
    assert state.placement_budget is state.preparation_budget is None
    assert state.pending == "" and state.clarifications == 0


@pytest.fixture
def format_kb():
    return KnowledgeBase([], [
        PriceRow("Видеоролик на экране infodesk", "Бутово Молл, Москва — 1 этаж", "1 месяц", 30000, ""),
        PriceRow("Полный брендинг ограждений траволатора", "Бутово Молл, Москва", "1 месяц", 60000, ""),
        PriceRow("Частичный брендинг ограждений траволатора", "Бутово Молл, Москва", "1 месяц", 50000, ""),
        PriceRow("Тейбл-тенты, одна сторона", "Бутово Молл, Москва — фуд-корт", "1 месяц", 36000, ""),
        PriceRow("Пакет: вариант 1, обычное предложение", "Бутово Молл, Москва", "1 месяц", 90000, ""),
        PriceRow("Баннер на перекрытии", "Бутово Молл, Москва", "1 месяц", 40000, ""),
    ], ["скидк"])


@pytest.mark.parametrize("question", [
    "Обычное предложение? Самый дешёвый вариант в Бутово на месяц",
    "Дайте одну нормальную цену в Бутово",
    "Полный ценник на месяц в Бутово",
    "Есть недорогие предложения в Бутово?",
    "Дорогой вариант не нужен, покажите дешёвый",
    "В Бутово Молле рекламу хочу на месяц. Только не впаривайте мне ваш самый дорогой пакет. "
    "Сразу скидку процентов 70 делайте, за полный ценник это никому не надо. Что предложите?",
])
def test_selection_and_adjectives_alone_do_not_choose_a_format(format_kb, question):
    state = Conversation()
    state.observe(format_kb, question)
    assert state.format == ""
    assert "формат:" not in state.parameters()


@pytest.mark.parametrize("question, selected", [
    ("Самый дешёвый экран в Бутово, одну нормальную цену", "экран"),
    ("Обычное предложение: пакет в Бутово на месяц", "пакет"),
    ("Самый дешёвый вариант баннера в Бутово", "баннера"),
    ("Полный брендинг в Бутово", "полный брендинг"),
    ("Частичный брендинг в Бутово", "частичный брендинг"),
    ("Полный ценник. Сколько стоит экран в Бутово?", "экран"),
])
def test_format_nouns_keep_only_their_own_modifiers(format_kb, question, selected):
    state = Conversation()
    state.observe(format_kb, question)
    assert state.format == selected


def test_replayed_bargaining_does_not_lock_regular_price_to_branding(format_kb):
    first = ("В Бутово Молле рекламу хочу на месяц. Только не впаривайте мне ваш самый дорогой пакет.\n"
             "Сразу скидку процентов 70 делайте, за полный ценник это никому не надо. Что предложите?")
    second = ("Да что там уточнять-то. Ладно, скидку пока проехали.\n"
              "Обычное предложение можете дать? Самый дешёвый вариант в Бутово на месяц, "
              "цена и что за неё получу. Без этого «вернусь».")
    state = Conversation.replay(format_kb, [
        {"question": first, "decision": ESCALATE, "reason": "stop_topic:скидк", "status": "sent_auto"}])
    assert state.format == ""
    state.observe(format_kb, second)
    assert state.format == ""
    hits = format_kb.retrieve(state.query(second))
    assert hits[0].chunk.price == 30000 and "экране" in hits[0].chunk.title
    assert "пакет" not in state.query(second).lower()


def test_a_price_count_does_not_add_a_table_tent_format(format_kb):
    question = ("Сколько стоит экран на инфодеске в Бутово на месяц по обычному прайсу?\n"
                "Одну нормальную цену напишите, я уже сам решу.")
    state = Conversation()
    state.observe(format_kb, question)
    assert state.format == "экран"
    assert "одну" not in state.query(question)
    assert "экране" in format_kb.retrieve(state.query(question))[0].chunk.title


@pytest.mark.parametrize("selected", ["Экран", "Полный брендинг", "Пакет", "Баннер"])
@pytest.mark.parametrize("follow_up", [
    "Самый дешёвый вариант", "Обычное предложение можете дать?", "А варианты подешевле?",
])
def test_generic_selection_preserves_an_existing_family(format_kb, selected, follow_up):
    state = Conversation()
    state.observe(format_kb, f"{selected} в Бутово")
    before = state.format
    state.observe(format_kb, follow_up)
    assert state.format == before and not state.changed


def test_explicit_reset_and_rejected_family_do_not_create_another_family(format_kb):
    state = Conversation()
    state.observe(format_kb, "Экран в Бутово")
    state.observe(format_kb, "Самый дешёвый вариант, только не пакет")
    assert state.format == "экран"
    state.observe(format_kb, "Не экран, покажите варианты")
    assert state.format == ""
    state.observe(format_kb, "Экран в Бутово")
    state.observe(format_kb, "Забудьте, начнём сначала. Самый дешёвый вариант в Бутово")
    assert state.format == ""


def test_ordinal_selection_and_duration_remain_available(format_kb):
    state = Conversation()
    state.observe(format_kb, "Экран в Бутово на один месяц")
    assert "один месяц" in state.query("Экран в Бутово на один месяц")
    state.observe(format_kb, "А первый вариант?")
    assert state.format == "экран"
    assert "первый вариант" in state.query("А первый вариант?")
    assert state.clarification(format_kb, "А первый вариант?")[0] == "position"
