"""Транслит не должен переставать работать из-за одного русского слова в сообщении."""
import pytest

from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow
from app.core.text import from_translit


def test_mixed_translit_keeps_requested_venue_and_format():
    text = from_translit("privet, nuzhna reklama на ekrane v veer, skolko stoit?")
    assert "нужна реклама на экране в веер" in text
    kb = KnowledgeBase([], [PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 30000, "")], [])
    state = Conversation()
    state.observe(kb, "privet, nuzhna reklama на ekrane v veer, skolko stoit?")
    assert "ВЕЕР" in state.venue and "экран" in state.format


def test_single_russian_aside_does_not_disable_latin_request():
    text = from_translit("privet, nuzhna reklama na ekranah incentra na 2 nedeli, ой раскладка")
    assert "инцентра" in text and "недели" in text and "ой раскладка" in text


@pytest.mark.parametrize("text", [
    "Нужен ролик MP4 1920x1080 для LED, в Full HD получится?",
    "Макет для Ozon и Wildberries, формат PDF или JPG?",
    "В ролике текст Summer Sale, это можно?",
    "screen please",
])
def test_foreign_names_and_file_formats_are_preserved(text):
    assert from_translit(text) == text


def test_recognized_translit_preserves_brands_and_file_format():
    text = from_translit("nuzhna reklama для Ozon, skolko stoit? ролик MP4")
    assert "нужна реклама" in text and "Ozon" in text and "MP4" in text
