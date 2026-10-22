"""Ответ «да» на вопрос о свойстве, которого нет в базе: «Есть ли звук на экранах?» — «Звук присутствует»."""
import asyncio
import json

from app.config import ROOT
from app.core.assistant import ANSWER, ESCALATE, Assistant
from app.core.knowledge import KnowledgeBase
from app.llm.base import LLMProvider

KB = KnowledgeBase.load(ROOT / "tests" / "fixtures" / "kb")
SCREEN = "Прайс: Цифровой экран, Атриум — 30 000 ₽ за 1 месяц. Ролик до 15 сек; 1 выход в 3 минуты."


class Says(LLMProvider):
    name = "says"

    def __init__(self, answer: str):
        self.answer = answer

    async def complete(self, system, messages):
        return json.dumps({"answer": self.answer, "confidence": 0.95, "needs_manager": False}, ensure_ascii=False)


def ask(question: str, answer: str):
    return asyncio.run(Assistant(KB, Says(answer)).answer(question))


def test_invented_property_goes_to_manager():
    assert KB.ungrounded_focus("Здравствуйте! Есть ли звук на экранах?", "Да, звук на экранах есть.", SCREEN) == ["звук"]
    reply = ask("Есть ли подсветка у напольной графики в атриуме?", "Да, у напольной графики в атриуме есть подсветка.")
    assert reply.decision == ESCALATE and reply.reason == "ungrounded:['подсветк']"


def test_contradiction_is_out_of_reach_of_a_word_check():
    # «Без звука» в базе и «звук есть» в ответе — слово в контексте есть, противоречие лексикой не поймать
    assert KB.ungrounded_focus("Есть ли звук?", "Да, звук есть.", "ролик до 15 секунд без звука") == []


def test_grounded_or_refusing_or_open_questions_are_fine():
    # Свойство есть в контексте
    assert KB.ungrounded_focus("Можно ролик на 15 секунд?", "Да, ролик до 15 секунд.", SCREEN) == []
    # Честное «нет» — не обещание
    assert KB.ungrounded_focus("А можно рекламу на городском радио?", "Нет, мы размещаем рекламу только в ТЦ.", SCREEN) == []
    # Не вопрос «да или нет»
    assert KB.ungrounded_focus("Какое звуковое оборудование есть в атриуме?", "Звуковое оборудование: пульт.", SCREEN) == []
    assert KB.ungrounded_focus("В каких городах у вас есть площадки?", "В Екатеринбурге и Тагиле.", SCREEN) == []
    # Модель не повторяет слово клиента — проверять нечего
    assert KB.ungrounded_focus("Есть ли у вас скидки для кофеен?", "Цены одинаковые для всех.", SCREEN) == []


def test_synonym_from_question_counts_as_grounded():
    context = "Прайс: Брендирование свободных витрин магазинов, Бутово — 48 000 ₽."
    assert KB.ungrounded_focus("пустые витрины можно заклеить?", "Да, пустые витрины можно заклеить.", context) == []
    assert ask("Сколько стоит экран в атриуме?", "Экран в атриуме — 30 000 ₽ в месяц.").decision == ANSWER


def test_guess_after_admitting_no_data_goes_to_manager():
    reply = ask("Ролик для экрана в атриуме нужен в каком формате, mp4 подойдёт?",
                "Требования к формату файла точно не указаны, однако часто используется формат mp4.")
    assert reply.decision == ESCALATE

