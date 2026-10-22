"""Уточнения нужны для неоднозначного выбора, а не вместо общего ответа или подбора."""
import asyncio
import json

import pytest

from app.core.assistant import ANSWER, Assistant
from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow, parse_faq
from app.llm.base import LLMProvider


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def complete(self, system, messages):
        self.calls.append((system, messages))
        return json.dumps({"answer": self.answer, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


@pytest.fixture
def kb():
    rows = [
        PriceRow("Реклама на катке", "ВЕЕР Молл, Екатеринбург — каток", "1 месяц", 30000, "с НДС 5%"),
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — атриум", "1 месяц", 50000, "с НДС 5%"),
    ]
    return KnowledgeBase([], rows, [])


def answer(kb, question, response):
    llm = FakeLLM(response)
    state = Conversation()
    reply = asyncio.run(Assistant(kb, llm).answer(question, conversation=state))
    assert reply.decision == ANSWER
    assert not reply.reason.startswith("clarification")
    assert state.clarifications == 0
    return reply, llm, state


@pytest.mark.parametrize("question, response", [
    ("Цены указаны с НДС?", "Цены указаны с НДС, налог уже учтён в цене."),
    ("Печать и монтаж входят в стоимость?", "Печать и монтаж оплачиваются отдельно."),
])
def test_general_price_conditions_answer_from_faq_without_selecting_venue(kb, question, response):
    faq = parse_faq("## Цены указаны с НДС?\nЦены указаны с НДС, налог уже учтён в цене.\n"
                    "## Печать и монтаж входят в стоимость?\nПечать и монтаж оплачиваются отдельно.\n")
    knowledge = KnowledgeBase(faq, kb.prices, [])
    reply, llm, state = answer(knowledge, question, response)
    assert len(llm.calls) == 1 and reply.text == response
    assert any(source.startswith("faq-") for source in reply.sources)
    assert state.venue == ""


def test_question_about_supported_malls_is_not_a_price_clarification(kb):
    reply, _, state = answer(
        kb, "В каких торговых центрах можно разместить рекламу?",
        "Размещаем рекламу в ВЕЕР Молл и ДЕПО Молл.")
    assert "ВЕЕР" in reply.text and "ДЕПО" in reply.text
    assert state.venue == ""


@pytest.mark.parametrize("question, response, source, venue", [
    ("Сколько стоит реклама на катке?", "Реклама на катке в ВЕЕР — 30 000 ₽ за месяц.", "price-0", "ВЕЕР"),
    ("Сколько стоит цифровой экран?", "Цифровой экран в ДЕПО — 50 000 ₽ за месяц.", "price-1", "ДЕПО"),
])
def test_format_available_in_one_mall_does_not_require_client_to_name_that_mall(
        kb, question, response, source, venue):
    reply, llm, state = answer(kb, question, response)
    assert len(llm.calls) == 1
    assert source in reply.sources and venue in reply.text
    assert state.venue == "", "Доступная строка прайса не означает, что клиент сам выбрал ТЦ"
    assert state.format


def test_budget_question_selects_affordable_price_without_first_asking_for_a_mall(kb):
    reply, llm, state = answer(
        kb, "Сколько стоит реклама? Бюджет 40 тысяч.",
        "Реклама на катке в ВЕЕР — 30 000 ₽ за месяц.")
    assert state.budget == 40000 and state.venue == ""
    assert len(llm.calls) == 1
    assert "30 000" in reply.text and "ВЕЕР" in reply.text
    assert "price-0" in reply.sources and "price-1" not in reply.sources


@pytest.mark.parametrize("question", [
    "Какие цены на форматы рекламы?",
    "Покажите прайс по всем ТЦ",
])
def test_general_price_list_is_not_blocked_by_a_venue_clarification(kb, question):
    reply, _, state = answer(
        kb, question, "В ВЕЕР размещаем рекламу на катке, в ДЕПО — на цифровом экране.")
    assert "ВЕЕР" in reply.text and "ДЕПО" in reply.text
    assert state.venue == ""
