"""«Добрый день» — не срок, место — не формат. Песочница MAX, GigaChat Pro, 04.11: в карточке «срок: день»
у вопроса «…в месяц» и «формат: экран атриуме; место: атриуме». Без сети."""
import asyncio
import json

import pytest

from app.config import ROOT
from app.core.answer_grounding import price_period_issues
from app.core.assistant import Assistant
from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow, SearchHit
from app.llm.base import LLMProvider


@pytest.fixture(scope="module")
def real_kb():
    return KnowledgeBase.load(ROOT / "data" / "kb")


def observe(kb, *questions):
    state = Conversation()
    for question in questions:
        state.observe(kb, question)
    return state


def test_greeting_does_not_become_period_and_is_not_inherited(real_kb):
    state = observe(real_kb, "добрый день есть что нибудь в депо до 30 тыс в месяц")
    assert state.period == "месяц"
    assert "срок: месяц" in state.parameters()
    state.observe(real_kb, "а на тележках для покупок рекламу можно разместить?")
    assert state.period == "месяц"
    assert "день" not in state.parameters()


@pytest.mark.parametrize("greeting", ["Добрый день", "доброго дня", "Хорошего дня", "хорошего вам дня", "день добрый",
                                      "Доброго денька", "отличной недели", "С Новым годом"])
def test_courtesy_gives_no_period(real_kb, greeting):
    state = observe(real_kb, f"{greeting}! Сколько стоит экран в депо?")
    assert state.period == ""
    assert "срок" not in state.parameters()
    assert "дня" not in state.query("x").split()


@pytest.mark.parametrize("question,period", [
    ("добрый день, экран в депо в месяц", "месяц"),
    ("Доброго дня! экран в депо на день", "на день"),
    ("Хорошего дня, а экран в депо на 2 дня?", "на 2 дня"),
    ("день добрый, на 2 недели экран в депо", "на 2 недели"),
])
def test_explicit_period_still_parsed_next_to_greeting(real_kb, question, period):
    assert observe(real_kb, question).period == period


def test_farewell_keeps_conversation_and_period(real_kb):
    state = observe(real_kb, "экран в депо на месяц", "спасибо, хорошего дня")
    assert (state.venue, state.format, state.period) == ("ДЕПО, Нижний Тагил", "экран", "на месяц")


def test_place_is_not_part_of_format(real_kb):
    state = observe(real_kb, "Сколько стоит экран в атриуме ВЕЕР Молл на месяц?")
    assert (state.format, state.place) == ("экран", "атриуме")
    assert "формат: экран; место: атриуме; срок: на месяц" in state.parameters()
    # «а в атриуме?» уточняет место выбранного экрана, а не меняет формат
    state = observe(real_kb, "Сколько стоит экран в ВЕЕР?", "а в атриуме?")
    assert (state.format, state.place) == ("экран", "атриуме")


@pytest.mark.parametrize("question,fmt,place", [
    ("флаг в атриуме веер", "флаг атриуме", ""),  # так называется строка прайса: «Флаг в атриуме»
    ("медиа-куб у эскалаторов в глобусе", "медиа-куб", "эскалаторов"),
    ("реклама в лифтах веер", "лифтах", ""),  # формат назван только местом — остаётся форматом
])
def test_place_words_inside_format_names(real_kb, question, fmt, place):
    state = observe(real_kb, question)
    assert (state.format, state.place) == (fmt, place)


@pytest.mark.parametrize("question,venue", [
    # «фудкорте» больше не в формате и не перевешивает в пользу тейбл-тентов Бутово — ТЦ нужен из слов клиента
    ("здравствуйте 👋 а в депошке экран со звуком на фудкорте почём", "ДЕПО, Нижний Тагил"),
    ("экран в академе сколько", "ТРЦ «Академический», Екатеринбург"),
    ("что есть в аутлете", "BrandStoriesOutlet, Екатеринбург"),
    ("экран в veer", "ВЕЕР Молл, Екатеринбург"),
])
def test_venue_aliases_work_in_conversation(real_kb, question, venue):
    state = observe(real_kb, question)
    assert state.venue == venue
    assert state.clarification(real_kb, question) is None


VEER_SCREEN = PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — центральный атриум", "1 месяц", 31500, "")


@pytest.mark.parametrize("answer", [
    "Добрый день, экран в атриуме ВЕЕР Молл стоит 31 500 ₽.",
    "Экран в атриуме ВЕЕР Молл стоит 31 500 ₽, хорошего дня!",
    "Доброго дня, экран в атриуме — 31 500 ₽ в месяц.",
])
def test_greeting_in_answer_is_not_billing_period(answer):
    kb = KnowledgeBase([], [VEER_SCREEN], [])
    hits = [SearchHit(c, 1.0) for c in kb.chunks]
    assert price_period_issues(kb, answer, hits, "добрый день, экран в атриуме веер на месяц") == []


def test_real_wrong_period_still_caught():
    kb = KnowledgeBase([], [VEER_SCREEN], [])
    hits = [SearchHit(c, 1.0) for c in kb.chunks]
    answer = "Добрый день, экран в атриуме ВЕЕР Молл стоит 31 500 ₽ в день."
    assert price_period_issues(kb, answer, hits, "добрый день, экран в атриуме веер") == [31500]


class GreetingLLM(LLMProvider):
    name = "fake"

    def __init__(self):
        self.systems = []

    async def complete(self, system, messages):
        self.systems.append(system)
        return json.dumps({"answer": "Добрый день, экран в атриуме ВЕЕР Молл стоит 31 500 ₽.", "confidence": 1,
                           "needs_manager": False}, ensure_ascii=False)


def test_greeting_answer_reaches_client_and_prompt_has_right_period():
    kb = KnowledgeBase([], [VEER_SCREEN], [])
    llm = GreetingLLM()
    reply = asyncio.run(Assistant(kb, llm, min_score=0).answer("Добрый день, сколько экран в атриуме веер в месяц?",
                                                                conversation=Conversation()))
    assert reply.is_answer, reply.reason  # раньше: period_mismatch — «день» из приветствия считался сроком цены
    assert "срок: месяц" in llm.systems[0] and "срок: день" not in llm.systems[0]
