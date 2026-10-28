"""Своя площадка, формат и период важнее совпадения отдельного числа или слова."""
import asyncio
import json

import pytest

from app.core.answer_grounding import budget_issues, price_period_issues, property_issues
from app.core.assistant import Assistant
from app.core.knowledge import KnowledgeBase, PriceRow, SearchHit, parse_faq
from app.llm.base import LLMProvider


@pytest.fixture
def kb():
    faq = parse_faq("""## Какие требования к видеороликам?
Экран со звуком на фуд-корте ДЕПО: ролик до 15 секунд, разрешение 672×392.
Infodesk в Бутово Молл: ролик 15 секунд, 1920×1080.
На экране в центральном атриуме ВЕЕР Молл ролик до 10 секунд, без звука.
## Кто готовит ролик?
Готовый ролик предоставляет рекламодатель.
""")
    return KnowledgeBase(faq, [
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — фуд-корт", "1 месяц", 26250, ""),
        PriceRow("Видеоролик на экране infodesk", "Бутово Молл, Москва — 1 этаж", "1 месяц", 30000, ""),
        PriceRow("Цифровой экран Инцентра", "ТРЦ Фан-Фан, Екатеринбург — главный вход", "1 месяц", 31500, ""),
        PriceRow("Промо-акция", "ТРЦ Фан-Фан, Екатеринбург — главный вход", "2 недели", 31500, ""),
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — центральный атриум", "1 месяц", 31500, ""),
        PriceRow("Цифровой медиа-куб", "ВЕЕР Молл, Екатеринбург — лифты", "1 месяц", 40000, ""),
    ], [])


def hits(kb):
    return [SearchHit(c, 10) for c in kb.chunks]


@pytest.mark.parametrize("question,answer,missing", [
    ("В Бутово экран со звуком?", "Экран со звуком расположен на infodesk Бутово Молл.", ["звук"]),
    ("А звук в ДЕПО?", "На экране фуд-корта ДЕПО Молл есть звук.", []),
    ("А звук в ВЕЕР?", "На экране в атриуме ВЕЕР Молл ролики идут без звука.", []),
    ("В ВЕЕР со звуком?", "Да, в ВЕЕР Молл экран со звуком.", ["звук"]),
    ("В Бутово есть звук?", "В Бутово Молл звук не подтверждён, нужно уточнить.", []),
    ("В Бутово есть звук?", "Экран Бутово со звуком, срок неизвестен.", ["звук"]),
    ("Для экрана ВЕЕР ролик до 15 секунд?", "На экране в атриуме ВЕЕР Молл ролик до 15 секунд.", ["длительность"]),
    ("Какая длина видео в Бутово?", "В Бутово Молл максимальная длительность ролика 15 секунд.", []),
    ("Вертикальный ролик для Бутово подойдёт?", "В Бутово Молл можно вертикальный и горизонтальный ролик.", ["ориентация"]),
    ("Какое разрешение видео в Бутово?", "Для Бутово Молл нужен ролик 672×392.", ["разрешение"]),
])
def test_technical_property_must_belong_to_requested_screen(kb, question, answer, missing):
    assert property_issues(kb, answer, hits(kb), question) == missing


def test_short_duration_answer_uses_title_without_treating_question_as_sound_fact(kb):
    kb.faq[0].text = "Вопрос: Есть ли звук и какая длина ролика для Бутово?\nОтвет: До 15 секунд."
    source = [SearchHit(kb.faq[0], 10)]
    assert property_issues(kb, "Ролик до 15 секунд.", source, "Длина ролика для Бутово?") == []
    assert property_issues(kb, "Да, звук есть.", source, "Звук для Бутово?") == ["звук"]


def test_screen_property_not_proven_by_radio_or_cube(kb):
    kb.faq[0].text = "Вопрос: Что известно?\nОтвет: На радио ВЕЕР Молл есть звук."
    assert property_issues(kb, "На медиа-кубе ВЕЕР Молл есть звук.", [SearchHit(kb.faq[0], 10)],
                           "Звук медиа-куба в ВЕЕР?") == ["звук"]


def test_bundle_does_not_transfer_radio_duration_to_screen(kb):
    kb.faq[0].text = ("Вопрос: Что в пакете ВЕЕР?\nОтвет: Экран ВЕЕР, ролик до 10 секунд; "
                      "радио ВЕЕР, аудиоролик до 25 секунд.")
    assert property_issues(kb, "На экране ВЕЕР ролик до 25 секунд.", [SearchHit(kb.faq[0], 10)],
                           "Длина ролика на экране ВЕЕР?") == ["длительность"]


def test_floor_and_nearby_lifts_do_not_erase_foodcourt_scope(kb):
    kb.faq[0].text = ("Вопрос: Требования к экрану ДЕПО?\nОтвет: Экран на фуд-корте, 2 уровень, у лифтов: "
                      "размер 6240×2240 мм, разрешение 2432×896 px. Ролик до 10 секунд.")
    assert property_issues(kb, "Экран фуд-корта ДЕПО: разрешение 2432×896, ролик до 10 секунд.",
                           [SearchHit(kb.faq[0], 10)], "Требования к экрану фуд-корта ДЕПО?") == []


def test_first_message_scoped_budget_uses_total_not_preparation_limit():
    kb = KnowledgeBase([], [PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — вход", "1 месяц", 20000, "")], [])

    class PriceLLM(LLMProvider):
        async def complete(self, system, messages):
            assert "20 000" in system
            return json.dumps({"answer": "Экран ДЕПО — 20 000 ₽ за месяц.", "confidence": 1, "needs_manager": False})

    reply = asyncio.run(Assistant(kb, PriceLLM()).answer(
        "Экран в ДЕПО на месяц. На подготовку до 10 тысяч, всего можем 25 тысяч. Что есть?"))
    assert reply.is_answer and "20 000" in reply.text


@pytest.mark.parametrize("question,answer,bad", [
    ("Экран Фан-Фан на две недели сколько?", "Экран Фан-Фан — 31 500 ₽ за 2 недели.", [31500]),
    ("Экран Фан-Фан на две недели сколько?", "Экран Фан-Фан — 31 500 руб. за 2 недели.", [31500]),
    ("Экран Фан-Фан на две недели сколько?", "Экран Фан-Фан — 31 500 ₽ за 1 месяц.", []),
    ("Экран Фан-Фан на две недели сколько?", "В Фан-Фан стоимость 31 500 ₽ за две недели.", [31500]),
    ("Экран Фан-Фан на две недели сколько?", "Промо-акция Фан-Фан — 31 500 ₽ за 2 недели.", []),
    ("Промо-акция Фан-Фан сколько?", "Промо-акция Фан-Фан — 31 500 ₽ за 14 дней.", []),
    ("Экран в ДЕПО сколько?", "Экран ДЕПО — 30 000 ₽ за месяц.", [30000]),
])
def test_same_number_from_different_service_does_not_prove_billing_period(kb, question, answer, bad):
    assert price_period_issues(kb, answer, hits(kb), question) == bad


def test_minimum_booking_term_not_used_as_price_billing_period(kb):
    kb.prices[2].period = "1 месяц при размещении от 6 месяцев"
    assert price_period_issues(kb, "Экран Фан-Фан — 31 500 ₽ за 6 месяцев.", hits(kb), "Экран Фан-Фан?") == [31500]


def test_same_venue_amount_from_other_static_format_not_accepted():
    kb = KnowledgeBase([], [
        PriceRow("Баннер", "ВЕЕР Молл, Екатеринбург — вход", "1 месяц", 50000, ""),
        PriceRow("Лайтбокс", "ВЕЕР Молл, Екатеринбург — вход", "1 месяц", 30000, ""),
    ], [])
    assert price_period_issues(kb, "Баннер в ВЕЕР — 30 000 ₽ за месяц.", hits(kb), "Сколько баннер ВЕЕР?") == [30000]


def test_quoting_client_budget_does_not_turn_it_into_price(kb):
    assert price_period_issues(kb, "Ваш бюджет 30 000 ₽. Экран Фан-Фан стоит 31 500 ₽ за месяц.",
                               hits(kb), "Экран Фан-Фан сколько?") == []


def test_monthly_price_does_not_authorize_unpriced_short_period_discount(kb):
    assert price_period_issues(kb, "Экран Фан-Фан стоит 31 500 ₽ в месяц, на 2 недели будет меньше.",
                               hits(kb), "Экран Фан-Фан на две недели?") == [31500]
    assert price_period_issues(kb, "Экран Фан-Фан стоит 31 500 ₽ в месяц. Тариф на две недели нужно уточнить.",
                               hits(kb), "Экран Фан-Фан на две недели?") == []


def test_fraction_quote_discovered_on_heldout_becomes_regression(kb):
    assert price_period_issues(kb, "Экран Фан-Фан — 31 500 ₽ за месяц. Если нужен показ на половину месяца, "
                               "стоимость будет примерно половиной указанной суммы.", hits(kb),
                               "Экран Фан-Фан с 3 по 16 число?") == [31500]


def test_abbreviated_currency_does_not_hide_invented_billing_period(kb):
    kb.prices[2].period = ""
    assert price_period_issues(kb, "Экран Фан-Фан — 31 500 руб. в месяц.", hits(kb), "Сколько экран Фан-Фан?") == [31500]


def test_total_fit_does_not_prove_fit_of_preparation_budget():
    kb = KnowledgeBase([], [PriceRow("Баннер", "ДЕПО Молл — вход", "1 месяц", 12000,
                                     "печать 3 200 ₽; монтаж 8 800 ₽")], [])
    text = "В рамках бюджета доступен баннер ДЕПО: 12 000 ₽ за месяц, печать 3 200 ₽, монтаж 8 800 ₽."
    assert budget_issues(kb, text, hits(kb), total=25000, placement=None, preparation=10000) == "бюджет подготовки"
    assert budget_issues(kb, text, hits(kb), total=25000, placement=None, preparation=12000) == ""
    assert budget_issues(kb, "Баннер ДЕПО — 12 000 ₽, печать 3 200 ₽, монтаж 8 800 ₽; "
                         "подготовка превышает ваш лимит.", hits(kb), total=25000, placement=None, preparation=10000) == ""


def test_same_price_of_other_format_does_not_invalidate_supported_budget():
    kb = KnowledgeBase([], [
        PriceRow("Баннер", "ДЕПО Молл — вход", "1 месяц", 12000, "печать 3 200 ₽; монтаж 8 800 ₽"),
        PriceRow("Лайтбокс", "ДЕПО Молл — фуд-корт", "1 месяц", 12000, "печать 2 000 ₽")
    ], [])
    assert budget_issues(kb, "В рамках бюджета подходит лайтбокс ДЕПО: 12 000 ₽ в месяц, печать 2 000 ₽.",
                         hits(kb), total=25000, placement=None, preparation=10000) == ""


class Says(LLMProvider):
    async def complete(self, system, messages):
        return json.dumps({"answer": "Экран со звуком расположен на infodesk в Бутово Молл.",
                           "confidence": 1, "needs_manager": False}, ensure_ascii=False)


def test_unguarded_model_assertion_does_not_reach_client(kb):
    reply = asyncio.run(Assistant(kb, Says()).answer("Экран на infodesk в Бутово Молл со звуком?"))
    assert not reply.is_answer and reply.reason.startswith("ungrounded")


def test_extras_paid_additionally_stay_with_the_same_format():
    """GigaChat Pro 28.10: «Печать и монтаж оплачиваются дополнительно: печать 2 550 ₽» уходило менеджеру —
    слово «дополнительно» из названия «Дополнительный пост охраны» меняло формат фразы."""
    kb = KnowledgeBase([], [
        PriceRow("Брендирование пола в лифте", "ВЕЕР Молл, Екатеринбург — 1 лифт", "1 месяц", 25000,
                 "печать 2 550 ₽; монтаж и демонтаж 3 550 ₽"),
        PriceRow("Дополнительный пост охраны на мероприятии", "ВЕЕР Молл, Екатеринбург", "1 день", 4000, ""),
    ], [])
    answer = ("Брендирование пола в одном лифте в ВЕЕР Молл стоит 25 000 ₽ за 1 месяц. Печать и монтаж оплачиваются "
              "дополнительно: печать - 2 550 ₽, монтаж и демонтаж - 3 550 ₽.")
    assert price_period_issues(kb, answer, hits(kb), "Сколько стоит брендирование пола в одном лифте в Веере?") == []


def test_not_included_in_price_is_not_the_entrance():
    """«Не входят в цену … печать 17 500 ₽» — глагол «входят», а не место «вход»: доплаты короба над банкоматами
    не должны проверяться как цены у входа."""
    kb = KnowledgeBase([], [
        PriceRow("Световой короб 2,8×2,2 м", "ДЕПО, Нижний Тагил — над банкоматами", "1 месяц", 25000,
                 "печать 17 500 ₽; монтаж 3 500 ₽"),
        PriceRow("Баннер", "ДЕПО, Нижний Тагил — главный вход", "1 месяц", 40000, ""),
    ], [])
    answer = ("Световой короб над банкоматами в ДЕПО стоит 25 000 ₽ за 1 месяц. Не входят в цену и оплачиваются "
              "дополнительно: печать - 17 500 ₽; монтаж - 3 500 ₽.")
    assert price_period_issues(kb, answer, hits(kb), "Сколько стоит световой короб над банкоматами в Депо?") == []
    entrance = "Баннер у главного входа в ДЕПО — 40 000 ₽ за неделю."
    assert price_period_issues(kb, entrance, hits(kb), "Баннер у входа в Депо?") == [40000], "место «вход» по-прежнему видно"


def test_sizes_one_item_and_lightbox_synonym_do_not_switch_format():
    """Pro 28.10: «печать одного флага» («одна сторона» у тейбл-тентов), «1,45×2,4 м» (метры), «световые коробы»
    вместо «лайтбокс» и «на входе» против «входной группы» уводили проверку к чужим строкам."""
    kb = KnowledgeBase([], [
        PriceRow("Подвесные флаги 4,5×2,5 м, 3 шт.", "ВЕЕР Молл, Екатеринбург — фуд-корт", "1 месяц", 115500,
                 "печать 14 000 ₽ за флаг; монтаж 16 800 ₽"),
        PriceRow("Тейбл-тенты на столах фуд-корта, 90 шт. А5, одна сторона", "ВЕЕР Молл, Екатеринбург — фуд-корт",
                 "1 месяц", 20000, ""),
        PriceRow("Лайтбокс 1,2×1,8 м", "Бутово Молл, Москва — парковка −1, зелёная зона перед входом", "1 месяц", 42000,
                 "носитель — световой короб с тканевым полотном"),
        PriceRow("Брендирование раздвижных дверей", "Бутово Молл, Москва — входная группа, 1 этаж", "1 месяц", 62000, ""),
    ], [])
    flags = "Три флага на фуд-корте ВЕЕР — 115 500 ₽ за месяц. Печать одного флага – 14 000 ₽, монтаж – 16 800 ₽."
    assert price_period_issues(kb, flags, hits(kb), "Флаг на фуд-корте в веере сколько стоит?") == []
    boxes = ("В Бутово Молл на парковке есть лайтбоксы. Это световые коробы с тканевым полотном 1,2х1,8 м: "
             "зелёная зона перед входом - 42 000 ₽.")
    assert price_period_issues(kb, boxes, hits(kb), "в бутово лайтбоксы на парковке есть?") == []
    doors = "Брендирование раздвижных дверей в Бутово Молле стоит 62 000 ₽."
    assert price_period_issues(kb, doors, hits(kb), "двери на входе в бутово брендировать почем?") == []


def test_period_label_before_price_belongs_to_it():
    kb = KnowledgeBase([], [
        PriceRow("Аудиореклама", "Бутово Молл, Москва — радио", "1 день", 6000, ""),
        PriceRow("Аудиореклама", "Бутово Молл, Москва — радио", "1 неделя", 18000, ""),
        PriceRow("Аудиореклама", "Бутово Молл, Москва — радио", "1 месяц", 42000, ""),
    ], [])
    rates = "Радио в Бутово: день - 6 000 ₽, неделя - 18 000 ₽, месяц - 42 000 ₽."
    assert price_period_issues(kb, rates, hits(kb), "радио Бутово: ставка за день, неделю и месяц?") == []
    assert price_period_issues(kb, "Радио в Бутово: неделя - 6 000 ₽.", hits(kb), "радио бутово неделя?") == [6000]


def test_price_row_property_belongs_to_the_whole_placement():
    """«траволатор, вход в ОКЕЙ» — одно место: «ролик 10 сек» из примечания подтверждает и экран на траволаторе."""
    kb = KnowledgeBase([], [
        PriceRow("Цифровой экран Инцентра", "ТРЦ «Радуга парк», Екатеринбург — траволатор, вход в ОКЕЙ", "1 месяц", 31500,
                 "ролик 10 сек, 144 выхода в день"),
        PriceRow("Цифровой экран Инцентра", "ТРЦ «Радуга парк», Екатеринбург — фуд-корт", "1 месяц", 31500,
                 "ролик 15 сек"),
    ], [])
    answer = "Экран на траволаторе в Радуга парк: ролик длительностью 10 секунд, 144 выхода в день."
    assert property_issues(kb, answer, hits(kb), "радуга экран на траволаторе у окея скок") == []
    wrong = "Экран на траволаторе в Радуга парк: ролик длительностью 15 секунд."
    assert property_issues(kb, wrong, hits(kb), "радуга экран на траволаторе у окея скок") == ["длительность"]


def test_place_word_with_another_stem_is_not_a_format():
    """«Галереи, места № 1–8: 98 175 ₽» — «галер» и «галере» одно место, а не формат."""
    kb = KnowledgeBase([], [
        PriceRow("Подвесные флаги 2,5×6 м, до 3 шт.", "ВЕЕР Молл, Екатеринбург — галерея, места № 1–8", "1 месяц", 98175,
                 "печать 14 000 ₽ за флаг"),
        PriceRow("Брендирование галереи", "ВЕЕР Молл, Екатеринбург — галерея 2 этажа", "1 месяц", 60000, ""),
    ], [])
    answer = ("В ВЕЕР Молл есть подвесные флаги в галереях.\n- Галереи, места № 1–8: 98 175 ₽ в месяц за 3 флага.\n"
              "Печать каждого флага – 14 000 ₽.")
    assert price_period_issues(kb, answer, hits(kb), "флаги в веере сколько?") == []
