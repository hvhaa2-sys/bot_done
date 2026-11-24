"""12 опасных ответов 17.11.2026: бот отвечал сам там, где решает менеджер.

Наборы: eval/questions_real_style.csv (6), eval/questions_personas.csv (4), eval/questions_kb.csv (2).
Проверки точечные — обычные вопросы о цене, форматах и размещении не должны уходить менеджеру.
"""
import asyncio
import json

import pytest

from app.config import ROOT
from app.core.assistant import ESCALATE
from app.core.knowledge import Chunk, KnowledgeBase, SearchHit
from app.core.real_flow import BusinessAssistant, business_operation, manager_decision
from app.llm.base import LLMProvider


class Says(LLMProvider):
    name = "says"

    def __init__(self, answer: str = "Экран в атриуме — 31 500 ₽ за месяц."):
        self.answer, self.calls = answer, []

    async def complete(self, system, messages):
        self.calls.append((system, messages))
        return json.dumps({"answer": self.answer, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase.load(ROOT / "data" / "kb")


OPERATIONAL = "Доброе утро! В почту отправила все материалы по ноябрю"

MANAGER_ONLY = [
    ("а бывали ли случаи что машину которая стоит в ТРЦ могли поцарапать и т.д.?", "risk_or_liability"),
    ("1. видео на фуд-корте. Вижу, что это сцена. Будут ли какие-нибудь мероприятия с 14 ноября "
     "по 20 декабря? Хотим поучаствовать в розыгрышах", "event_participation"),
    ("номер, который вы дали, не доступен. как можно еще с ним связаться?", "client_contact"),
    ("к сожалению, по номеру телефона не могу найти его в тг, сообщите его ник, пожалуйста", "client_contact"),
    ("а можно глянуть как выглядит экран в атриуме? покажите плз", "visual_request"),
    ("в прошлом месяце наш ролик на экране в академе вообще не крутили!!! мы три дня подряд проверяли, "
     "что за дела", "client_complaint"),
    ("Ваша коллега вчера по телефону сказала, что медиа-куб в ВЕЕР для нас будет за полцены. "
     "Подтвердите, пожалуйста, что всё в силе", "discount_promise"),
    ("Кто ещё сейчас крутится на экранах в ВЕЕР Молл? Назовите бренды, хочу понять соседство", "foreign_brands"),
]

MISSING_FACT = [
    ("Добрый день! Подскажите, пожалуйста, сколько столов у вас на фудкорте?", "tables"),
    ("можно ли забрендировать карты для входа на каток веер? какой минимальный тираж?", "tirage"),
    ("В каком формате файла нужен ролик для экрана в ДЕПО, mp4 подойдёт?", "file_format"),
]

ALL_TWELVE = [OPERATIONAL, *(text for text, _ in MANAGER_ONLY), *(text for text, _ in MISSING_FACT)]


def test_operational_message_is_a_manager_task():
    assert business_operation(OPERATIONAL)


@pytest.mark.parametrize("text,reason", MANAGER_ONLY)
def test_manager_only_topics_are_detected(text, reason):
    assert manager_decision(text) == reason


@pytest.mark.parametrize("text,reason", MISSING_FACT)
def test_requested_fact_absent_from_context(kb, text, reason):
    assert kb.missing_requested_fact(text, kb.retrieve(text)) == reason


@pytest.mark.parametrize("text,reason", [
    ("не могу дозвониться по номеру, который вы дали", "client_contact"),
    ("если возьмём на год, будет дешевле в два раза?", "discount_promise"),
    ("какие бренды сейчас у вас на экранах?", "foreign_brands"),
    ("будут ли мероприятия с 14 ноября по 20 декабря?", "event_schedule"),
])
def test_narrow_variants_are_manager_only(text, reason):
    assert manager_decision(text) == reason


def test_narrow_fact_variants(kb):
    assert kb.missing_requested_fact("какое количество столов на фуд-корте?",
                                     kb.retrieve("какое количество столов на фуд-корте?")) == "tables"
    assert kb.missing_requested_fact("какой кодек нужен для ролика в ДЕПО?",
                                     kb.retrieve("какой кодек нужен для ролика в ДЕПО?")) == "file_format"


@pytest.mark.parametrize("text", ALL_TWELVE)
def test_all_twelve_reach_manager_without_calling_model(kb, text):
    llm = Says()
    reply = asyncio.run(BusinessAssistant(kb, llm).answer(text))
    assert reply.decision == ESCALATE
    assert not llm.calls, "опасный вопрос решается проверкой, а не ответом модели"


@pytest.mark.parametrize("text", [
    "Сколько стоит экран в атриуме ВЕЕР?",
    "Какие форматы рекламы есть в ВЕЕР Молл?",
    "Что входит в монтаж?",
    "Можно ли разместить рекламу на фуд-корте?",
    "Сколько стоит тейбл-тент на столах фуд-корта?",
    "Кто делает ролик для экранов ВЕЕР?",
    "Покажите цены на экраны в ВЕЕР.",
    "Что дешевле — радио или экран в атриуме?",
    "Можно ли забрендировать карты для входа на каток?",
    "Какой формат у экрана в ДЕПО?",
    "Какие мероприятия будут в ВЕЕР Молл в декабре?",
    "Сколько мероприятий проводит ВЕЕР в месяц?",
    "Можно ли провести розыгрыш на нашей промо-стойке?",
    "Сколько стоит место для машины номер 1 в ВЕЕР?",
])
def test_normal_questions_are_not_manager_only(text):
    assert not manager_decision(text)
    assert not business_operation(text)


def test_file_format_answer_is_found_when_context_has_it(kb):
    question = "ролик на медиакуб в веере какой длины и в каком формате делать?"
    assert kb.missing_requested_fact(question, kb.retrieve(question)) == ""


def test_requested_fact_is_found_when_context_has_it(kb):
    def hit(text: str) -> SearchHit:
        return SearchHit(Chunk("faq-test", "faq", "Тест", text, []), 1.0)

    assert kb.missing_requested_fact("какой минимальный тираж карт?", [hit("Тираж карт — от 1 000 штук.")]) == ""
    assert kb.missing_requested_fact("сколько столов на фуд-корте?", [hit("На фуд-корте 90 столов.")]) == ""
    assert kb.missing_requested_fact("mp4 подойдёт?", [hit("Ролик — файл MP4 до 50 МБ.")]) == ""
    # «Формат 16:9» — соотношение сторон, а не тип файла: ответа на вопрос о файле в нём нет
    assert kb.missing_requested_fact("mp4 подойдёт?", [hit("Формат 16:9.")]) == "file_format"
