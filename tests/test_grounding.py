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



def test_condition_on_topic_absent_from_base_goes_to_manager():
    # Экзамен обучения 04.11, база из одного прайса: строка про пост охраны пришита к вопросу о музыке
    context = "Прайс: Дополнительный пост охраны на мероприятии, ВЕЕР Молл — 8 000 ₽ за 1 пост."
    question = "на нашем мероприятии в ВЕЕР будет музыка, что для этого оформить?"
    answer = "Для организации музыки на мероприятии вам потребуется оформить дополнительный пост охраны за 8 000 ₽."
    assert KB.absent_topic_claims(question, answer, context) == ["музык"]
    # Слово клиента после условия — подробность ответа, а не тема: контакт бухгалтера в базе есть
    assert KB.absent_topic_claims("и акты куда отправлять, бухгалтеру напрямую?",
                                  "Вам необходимо связаться с бухгалтером напрямую: office@example.test.",
                                  "Ответ: по актам обращайтесь к бухгалтеру, office@example.test.") == []
    # Тема в базе есть — условие по ней законно; без условия («подойдёт») — тоже не трогаем
    assert KB.absent_topic_claims("Нужно ли согласовывать макет?", "Да, макет нужно согласовать до печати.",
                                  "Вопрос: Нужно ли согласовывать макет? Ответ: макет согласуется с ТРЦ до печати.") == []
    assert KB.absent_topic_claims("Мы кофейня, что посоветуете?", "Для кофейни подойдёт экран в атриуме — 30 000 ₽.",
                                  SCREEN) == []


def test_sending_client_to_mall_administration_goes_to_manager():
    reply = ask("Через какой вход заходить в атриум утром до открытия?",
                "Вам лучше обратиться напрямую к администрации торгового центра. Нужна помощь с контактами?")
    assert reply.decision == ESCALATE and reply.reason == "ungrounded:['администрация']"
    # Если в базе про администрацию написано, ответ по ней законен
    assert KB.absent_topic_claims("Кто согласует макет?", "Макет согласует администрация ТРЦ.",
                                  "Ответ: макет согласует администрация ТРЦ за 2 дня.") == []
    # Честный отказ не трогаем
    assert KB.absent_topic_claims("А музыку можно?", "Нет, у нас нет звука на экранах, администрация не разрешает.",
                                  SCREEN) == []


def test_usually_placed_and_may_depend_are_guesses():
    reply = ask("Где ставят автомобиль для экспонирования?",
                "Автомобиль обычно размещается на первом этаже у входа. Точное место может зависеть от планировки.")
    assert reply.decision == ESCALATE


def test_instruction_to_send_list_to_manager_is_an_answer_not_a_handoff():
    # real_style 07.11: «нужен пропуск на ночной монтаж» — модель ответила по заметке, needs_manager=false,
    # а слово «менеджеру» в середине ответа отправляло всё менеджеру
    context = ("Вопрос: Что нужно организатору для монтажа? Ответ: Минимум за 3 дня до монтажа нужно прислать список "
               "сотрудников — менеджер оформит разрешение. С собой паспорт.")
    raw = lambda answer: json.dumps({"answer": answer, "confidence": 1, "needs_manager": False}, ensure_ascii=False)  # noqa: E731
    assistant = Assistant(KB, Says(""))
    reply = assistant._decide(raw("Минимум за 3 дня до монтажа отправьте список сотрудников менеджеру. С собой паспорт."),
                              "что от нас надо для ночного монтажа?", [], context, [])
    assert reply.decision == ANSWER and "менеджеру" in reply.text
    # Обещание менеджера рядом — по-прежнему к менеджеру; и без опоры в базе — тоже
    promise = assistant._decide(raw("Отправьте макет менеджеру, он уточнит сроки."), "что от нас надо?", [], context, [])
    assert promise.decision == ESCALATE
    bare = assistant._decide(raw("Отправьте список сотрудников менеджеру, и всё."), "что от нас надо?", [],
                             "Ответ: за 3 дня нужен список сотрудников.", [])
    assert bare.decision == ESCALATE


def test_all_period_tiers_of_one_item_come_together():
    # sales 07.11: «экран в атриуме и радио в веере, сколько стоит?» — в выдаче две ступени пакета из четырёх,
    # остальные суммы модель брала из заметки, и ответ уходил менеджеру по проверке цен
    from app.core.knowledge import PriceRow
    pack = "Пакет: экраны + радио"
    rows = [PriceRow(pack, "ВЕЕР Молл, Екатеринбург", f"1 месяц при размещении {p}", price, "без НДС")
            for p, price in (("на 12 месяцев", 154275), ("до 3 месяцев", 224400), ("от 6 месяцев", 168300),
                             ("от 3 месяцев", 179520))]
    rows += [PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 31500, ""),
             PriceRow(pack, "ДЕПО, Нижний Тагил", "1 месяц при размещении от 3 месяцев", 99000, "")]
    kb = KnowledgeBase([], rows, [])
    hits = [h for h in kb.retrieve("пакет экраны и радио в веере на 12 месяцев", top_k=1) if h.chunk.kind == "price"]
    assert [h.chunk.price for h in hits] == [224400, 179520, 168300, 154275], "все ступени, по порядку срока"
    assert 99000 not in {h.chunk.price for h in kb.retrieve("пакет экраны и радио в веере", top_k=1)}, "чужой ТЦ не тянем"
