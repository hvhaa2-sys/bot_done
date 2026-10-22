import pytest

from app.core.knowledge import KnowledgeBase
from app.core.text import from_translit


@pytest.fixture
def kb():
    return KnowledgeBase([], [], [
        "фото", "фотк", "коммерческ", "кп", "пришл", "присл", "скинь",
        "скидк", "договор", "оплат", "жалоб", "медицин", "2027",
    ])


@pytest.mark.parametrize("question", [
    "У нас фотостудия. Сколько стоит экран в ВЕЕР?",
    "Для фотостудии нужен экран в ВЕЕР, сколько стоит?",
    "Открываем фото-студию. Какие форматы рекламы есть?",
    "Я фотограф, хочу рекламу в ТЦ",
    "Для фотографа какие варианты размещения есть?",
    "Нужно прорекламировать услуги фотографов",
    "У нас фотосалон, сколько стоит экран?",
    "Реклама фотомагазина в ВЕЕР сколько стоит?",
    "Какие коммерческие форматы рекламы у вас есть?",
    "Расскажите о коммерческом формате размещения на экране",
    "У нас фoтостудия. Какие кoммерчеcкие фoрматы рекламы есть?",
])
def test_business_description_does_not_request_material(kb, question):
    assert kb.matched_stop_topic(question) is None


@pytest.mark.parametrize("question, topic", [
    ("Есть фото?", "фото"),
    ("Фото экрана есть?", "фото"),
    ("Есть фотографии места размещения?", "фото"),
    ("Можно фотографию экрана?", "фото"),
    ("Покажите фотографии", "фото"),
    ("Нужна фотка экрана", "фотк"),
    ("Есть фoтo?", "фото"),
    ("КП есть?", "кп"),
    ("Можно коммерческое предложение?", "коммерческ"),
    ("Есть коммерческое предложение по форматам рекламы?", "коммерческ"),
    ("У нас фотостудия, есть фото экрана?", "фото"),
    ("Я фотограф. Есть фотографии места размещения?", "фото"),
    ("Какие коммерческие форматы есть? И КП можно?", "кп"),
    ("Пришлите варианты коммерческих форматов", "пришл"),
    ("У нас фотосалон, пришлите материалы", "пришл"),
])
def test_material_requests_still_stop(kb, question, topic):
    assert kb.matched_stop_topic(question) == topic


@pytest.mark.parametrize("suffix, topic", [
    ("дадите скидку?", "скидк"),
    ("как оплатить?", "оплат"),
    ("нужен договор", "договор"),
    ("у меня жалоба", "жалоб"),
    ("будет медицинская реклама", "медицин"),
    ("какие цены на 2027 год?", "2027"),
])
def test_business_exceptions_keep_other_stop_topics(kb, suffix, topic):
    question = f"У нас фотостудия, интересуют коммерческие форматы. И {suffix}"
    assert kb.matched_stop_topic(question) == topic


def test_mixed_message_keeps_each_independent_reason():
    question = "У нас фотостудия, пришлите фото и дайте скидку"
    for topic in ("пришл", "фото", "скидк"):
        assert KnowledgeBase([], [], [topic]).matched_stop_topic(question) == topic


@pytest.mark.parametrize("topic, question", [
    ("фотостуди", "У нас фотостудия, сколько стоит экран?"),
    ("фотограф!", "Я фотограф, хочу рекламу в ТЦ"),
    ("фотосалон", "Нужна реклама фотосалона"),
    ("коммерческие форматы", "Какие коммерческие форматы рекламы есть?"),
    ("формат", "Какие коммерческие форматы рекламы есть?"),
])
def test_manager_can_still_stop_specific_business_or_format(topic, question):
    kb = KnowledgeBase([], [], ["фото", "коммерческ", topic])
    assert kb.matched_stop_topic(question) == topic


@pytest.mark.parametrize("question, topic", [
    ("u nas fotostudiya skolko stoit ekran v veer", None),
    ("ya fotograf hochu reklamu v tc", None),
    ("kakie kommercheskie formaty reklamy u vas est", None),
    ("u nas fotostudiya est foto ekrana", "фото"),
    ("mozhno kommercheskoe predlozhenie po ekranam", "коммерческ"),
])
def test_same_context_rules_after_existing_translit_normalization(kb, question, topic):
    assert kb.matched_stop_topic(from_translit(question)) == topic
