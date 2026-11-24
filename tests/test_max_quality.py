"""Регрессии нетипичных диалогов MAX: только вымышленные цены, без сети."""
import asyncio
import itertools
import json

import pytest

from app.core.assistant import Assistant
from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow, canonical_query
from app.core.price_options import fits_budget, option_cost
from app.core.real_flow import BusinessAssistant
from app.llm.base import LLMProvider


class Says(LLMProvider):
    def __init__(self, text="Экран в ВЕЕР — 30 000 ₽ за месяц."):
        self.text, self.calls = text, []

    async def complete(self, system, messages):
        self.calls.append((system, messages))
        return json.dumps({"answer": self.text, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


@pytest.fixture
def kb():
    return KnowledgeBase([], [
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — фуд-корт", "1 месяц", 30000,
                 "в ноябре–декабре 36 000 ₽ в месяц"),
        PriceRow("Видеоролик на экране infodesk", "Бутово Молл, Москва — 1 этаж", "1 месяц", 25000, ""),
        PriceRow("Аудиоролик на радио", "ВЕЕР Молл, Екатеринбург", "1 месяц при размещении от 6 месяцев", 10000, ""),
        PriceRow("Аудиоролик на радио", "ВЕЕР Молл, Екатеринбург", "1 месяц", 20000, ""),
        PriceRow("Видео на навигационных терминалах", "ВЕЕР Молл, Екатеринбург", "1 месяц", 22000, ""),
    ], ["оплат", "скидк"])


@pytest.mark.parametrize("text", [
    "Ок пока не меняйте старый ролик. Это отменяет мою последнюю просьбу.",
    "Ничего не меняйте сегодня, пусть старый ролик продолжает идти.",
    "Отмените вчерашнюю заявку на экран.",
    "Пожалуйста замените ролик сегодня вечером.",
    "Катя привет ролик поправили как вчера обсуждали можешь заменить сегодня?",
    "То есть за три дня возьмёте меньше 18000? Подтверди эту цену пожалуйста.",
])
def test_campaign_action_always_reaches_manager_without_model(kb, text):
    llm = Says("Хорошо, оставим всё без изменений.")
    reply = asyncio.run(BusinessAssistant(kb, llm).answer(text))
    assert reply.decision == "escalate" and reply.reason == "business_operation"
    assert not llm.calls


@pytest.mark.parametrize("text", ["Хорошо, оставим всё без изменений.",
                                 "Ролик заменили и запустили.",
                                 "Цена за неделю 20 000 ₽, за три дня потребуется чуть менее этой суммы."])
def test_model_cannot_confirm_action_or_discount(kb, text):
    reply = asyncio.run(BusinessAssistant(kb, Says(text)).answer("Радио в ВЕЕР на месяц сколько?"))
    assert reply.decision == "escalate"


def test_proration_request_never_calls_model(kb):
    llm = Says()
    reply = asyncio.run(BusinessAssistant(kb, llm).answer("На три дня раздели 30000 на 30 и умножь на 3"))
    assert reply.decision == "escalate" and not llm.calls


def test_external_client_keeps_explicit_mall(kb):
    state = Conversation()
    state.observe(kb, "Мы не арендаторы ВЕЕР Молла. Сколько стоит экран на фудкорте на месяц?")
    assert state.venue.startswith("ВЕЕР") and state.format == "экран"
    assert state.client_type == "внешние рекламодатели"


@pytest.mark.parametrize("text", ["инфодеск", "инфодеске", "инфодеска"])
def test_russian_infodesk_is_a_known_format(kb, text):
    state = Conversation()
    state.observe(kb, f"В Бутово на {text}. На месяц. Назови цену.")
    assert "infodesk" in state.format
    assert state.clarification(kb, "Назови цену") is None
    assert any(h.chunk.price == 25000 for h in kb.retrieve(state.query("Назови цену")))


def test_aliases_are_idempotent_and_keep_other_text(kb):
    for mall, desk, casing in itertools.product(("веермол", "веермолл", "веермолла", "веер молл"),
                                               ("инфодеск", "инфодеске", "infodesk"), (str.lower, str.upper)):
        text = casing(f"{mall}: {desk}; бюджет 15000, не менять ролик")
        normalized = canonical_query(text)
        assert canonical_query(normalized) == normalized
        assert "15000" in normalized and "не менять ролик" in normalized
        assert "веер" in kb.normalize(text) and "infodesk" in normalized


def test_mall_only_gets_format_question_and_wrong_infodesk_is_not_priced(kb):
    llm = Says("Экран infodesk в ВЕЕР стоит 30 000 ₽ за месяц.")
    assistant = Assistant(kb, llm)
    reply = asyncio.run(assistant.answer("Веермол"))
    assert reply.reason == "clarification:format" and not llm.calls
    reply = asyncio.run(assistant.answer("Инфодеск в ВЕЕР на месяц сколько?"))
    assert reply.reason == "clarification:position" and not llm.calls


def test_navigation_stands_are_terminals_not_promo_stands(kb):
    state = Conversation()
    state.observe(kb, "В ВЕЕР навигационные стойки на месяц. Сколько?")
    assert "терминалы" in state.format and "стойки" not in state.format
    hits = kb.retrieve(state.query("Сколько?"))
    assert any(h.chunk.price == 22000 for h in hits if h.chunk.kind == "price")
    llm = Says("Инфодеск отсутствует. Вот цена промоутера.")
    reply = asyncio.run(Assistant(kb, llm).answer("То есть инфодеска в ВЕЕР нет? А навигационные стойки это не оно?"))
    assert reply.reason == "navigation_format" and not llm.calls
    assert "22 000" in reply.text and "отсутствует" not in reply.text


def test_small_budget_does_not_offer_long_term_tier(kb):
    llm = Says("Предлагаю радио за 10 000 ₽.")
    reply = asyncio.run(Assistant(kb, llm).answer("Бюджет 15 тысяч на один месяц в ВЕЕР. Что можно?"))
    assert reply.reason == "no_budget_options" and not llm.calls
    assert "15 000" in reply.text and "10 000" not in reply.text
    other = asyncio.run(Assistant(kb, llm).answer("Есть 15 тысяч на месяц в ВЕЕР. Реально что-нибудь разместить?"))
    assert other.reason == "no_budget_options" and not llm.calls


def test_season_and_minimum_term_filter_before_generation(kb):
    hits = kb.retrieve("бюджет 32000 ₽ экран ВЕЕР на один месяц в ноябре")
    candidates = [h for h in hits if h.chunk.kind == "price"]
    # Конкретный дорогой экран может остаться для честного объяснения отказа,
    # но ни одна строка не должна считаться подходящей под заданные ограничения.
    assert not any(fits_budget(kb.prices[int(h.chunk.id.split('-')[1])],
                               "на один месяц в ноябре", 32000) for h in candidates)
    assert option_cost(kb.prices[0], "на месяц в октябре") == 30000
    assert option_cost(kb.prices[0], "на месяц в ноябре") == 36000
    assert not fits_budget(kb.prices[2], "на месяц", 15000)
    assert not fits_budget(kb.prices[2], "на 6 месяцев", 15000)
    assert fits_budget(kb.prices[2], "на 6 месяцев", 60000)
    assert fits_budget(kb.prices[2], "на 6 месяцев, бюджет 15000 в месяц", 15000)


def test_budget_eligibility_is_monotone_and_never_below_base_price():
    for base, extra in itertools.product((1, 999, 1000, 15000, 100000), (0, 1, 7000)):
        row = PriceRow("Баннер", "ВЕЕР Молл", "1 месяц", base,
                       f"печать {extra} ₽" if extra else "")
        budgets = sorted({0, base - 1, base, base + extra, base + extra + 1, 200000})
        flags = [fits_budget(row, "на месяц", budget) for budget in budgets]
        assert flags == sorted(flags), "Увеличение бюджета не должно исключать вариант"
        assert all(not fits_budget(row, "на месяц", b) for b in (0, base - 1))
        assert fits_budget(row, "на месяц", base + extra)


def test_acknowledged_payment_does_not_hide_new_stop_request(kb):
    assert kb.matched_stop_topic("Про оплату понял. А цену самого экрана на ноябрь можете сказать отдельно?") is None
    assert kb.matched_stop_topic("Про оплату понял. А цену экрана и оплату после размещения можно?") == "оплат"
    assert kb.matched_stop_topic("Можно платить после размещения? Оплата потом.") == "оплат"
