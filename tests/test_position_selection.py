"""Номер места выбирает строки размещения, не соседние цены и не длительность."""
import asyncio
import json

import pytest

from app.core.assistant import ANSWER, ESCALATE, Assistant
from app.core.knowledge import KnowledgeBase, PriceRow, parse_faq, position_ranges
from app.llm.base import LLMProvider


@pytest.fixture
def kb():
    rows = [
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — место № 1", "1 месяц", 280000, ""),
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — места № 2, 3, 6", "1 месяц", 220000, ""),
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — места № 4, 7", "1 месяц", 200000, ""),
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — по согласованию", "1 месяц", 180000, ""),
        PriceRow("Экспонирование автомобиля", "ДЕПО Молл, Нижний Тагил — место № 1", "1 месяц", 150000, ""),
    ]
    return KnowledgeBase([], rows, [])


def prices(kb, question, **kwargs):
    return {hit.chunk.price for hit in kb.retrieve(question, **kwargs) if hit.chunk.kind == "price"}


def test_live_question_returns_price_of_the_named_position(kb):
    assert prices(kb, "выставить машину на месте 1 в веер молл сколько") == {280000}


@pytest.mark.parametrize("wording, expected", [
    ("место №1", 280000),
    ("на месте номер 1", 280000),
    ("на месте 2", 220000),
    ("места № 3", 220000),
    ("место № 6", 220000),
    ("место 4", 200000),
    ("место №7", 200000),
])
def test_number_can_select_an_entry_in_a_placement_list(kb, wording, expected):
    assert prices(kb, f"Экспонирование автомобиля в ВЕЕР, {wording}, сколько стоит?") == {expected}


def test_number_is_filtered_before_the_top_k_limit(kb):
    question = "Экспонирование автомобиля ВЕЕР, место 1, цена 220000 200000"
    assert prices(kb, question, top_k=1) == {280000}


@pytest.mark.parametrize("question", [
    "Выставить машину на месте 99 в ВЕЕР сколько?",
    "Самое дешёвое место 99 для автомобиля в ВЕЕР?",
    "Автомобиль на месте 99 в ВЕЕР, бюджет 300 тысяч",
])
def test_unknown_position_never_falls_back_to_another_position_price(kb, question):
    assert prices(kb, question) == set()


@pytest.mark.parametrize("question", [
    "Самое дешёвое размещение автомобиля в ВЕЕР на месте 1",
    "Автомобиль в ВЕЕР на месте 1, бюджет 300 тысяч",
    "Автомобиль в ВЕЕР на месте 1, бюджет 250 тысяч, подходит?",
    "Какие цены на автомобиль в ВЕЕР на месте 1? И на месяц?",
])
def test_budget_cheapest_and_additional_question_cannot_reintroduce_neighbouring_positions(kb, question):
    assert prices(kb, question) == {280000}


def test_faq_can_remain_when_specific_position_has_no_price(kb):
    faq = parse_faq("## Как выставить автомобиль в ВЕЕР?\nВыставить автомобиль можно в торговом центре.\n")
    knowledge = KnowledgeBase(faq, kb.prices, [])
    hits = knowledge.retrieve("Как выставить автомобиль в ВЕЕР на месте 99?")
    assert any(hit.chunk.kind == "faq" for hit in hits)
    assert not any(hit.chunk.kind in ("price", "catalog") for hit in hits)


@pytest.mark.parametrize("question", [
    "Экспонирование автомобиля в ВЕЕР на 1 месяц",
    "Экспонирование автомобиля в ВЕЕР, первый вариант",
    "Нужно место 1 месяц для автомобиля в ВЕЕР",
])
def test_period_or_ordinal_option_does_not_select_a_numbered_position(kb, question):
    assert {280000, 220000, 200000} <= prices(kb, question)


def test_placement_ranges_and_lists_are_matched_without_expanding_large_ranges():
    rows = [
        PriceRow("Флаги", "ВЕЕР Молл — места № 1–8, 10, 11", "1 месяц", 98000, ""),
        PriceRow("Флаги", "ВЕЕР Молл — места № 9 и 12", "1 месяц", 200000, ""),
    ]
    knowledge = KnowledgeBase([], rows, [])
    assert prices(knowledge, "Флаги в ВЕЕР на месте 3") == {98000}
    assert prices(knowledge, "Флаги в ВЕЕР на месте 12") == {200000}
    assert prices(knowledge, "Флаги в ВЕЕР на местах 10 и 12") == {98000, 200000}
    assert position_ranges("места 1–999999999999") == ((1, 999999999999),)


def test_budget_filters_position_before_listing_limit():
    rows = [PriceRow("Экспонирование автомобиля", f"ВЕЕР Молл — место № {number}",
                     "1 месяц", 100000 + number, "") for number in range(1, 31)]
    knowledge = KnowledgeBase([], rows, [])
    assert prices(knowledge, "Место 1 в ВЕЕР, бюджет 300 тысяч") == {100001}


@pytest.mark.parametrize("amount, decision", [("220 000", ESCALATE), ("280 000", ANSWER)])
def test_neighbour_position_price_in_faq_does_not_authorize_its_price_for_selected_position(amount, decision):
    class FakeLLM(LLMProvider):
        name = "fake"

        def __init__(self):
            self.calls = []

        async def complete(self, system, messages):
            self.calls.append((system, messages))
            return json.dumps({
                "answer": f"Экспонирование автомобиля на месте № 1 в ВЕЕР — {amount} ₽ за месяц.",
                "confidence": 1, "needs_manager": False,
            }, ensure_ascii=False)

    rows = [
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — место № 1", "1 месяц", 280000, ""),
        PriceRow("Экспонирование автомобиля", "ВЕЕР Молл, Екатеринбург — место № 2", "1 месяц", 220000, ""),
    ]
    faq = parse_faq("## Сколько стоит экспонирование автомобиля в ВЕЕР?\n"
                    "Экспонирование автомобиля на месте № 2 в ВЕЕР — 220 000 ₽ за месяц.\n")
    knowledge = KnowledgeBase(faq, rows, [])
    llm = FakeLLM()
    reply = asyncio.run(Assistant(knowledge, llm, min_score=0).answer(
        "Сколько стоит экспонирование автомобиля на месте 1 в ВЕЕР?"))
    assert "price-0" in reply.sources and "price-1" not in reply.sources
    assert any(source.startswith("faq-") for source in reply.sources)
    assert llm.calls and "220 000" in llm.calls[0][0], "Цена соседнего места действительно осталась в FAQ контекста"
    assert reply.decision == decision
    if decision == ESCALATE:
        assert reply.reason.startswith("price_mismatch")
    else:
        assert "280 000" in reply.text
