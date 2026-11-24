"""Строка прайса, где НДС в источнике не указан (радио «Глобуса» по таблице Инцентра, 24.11.2026).

Раньше оговорка терялась: примечание заканчивалось словами «подтверждает менеджер», и обрезка «хвоста
про менеджера» снимала всю фразу вместе с частотой выходов и длиной ролика. Итоговую сумму здесь подтверждает
менеджер, поэтому такой ответ уходит ему черновиком с оговоркой, а не клиенту.
"""
import asyncio
import csv
import json
from pathlib import Path

import pytest

from app.channels.flow import REASONS
from app.core.assistant import ANSWER, ESCALATE, R_TERMS, R_VAT_UNKNOWN, Assistant
from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow
from app.core.real_flow import BusinessAssistant
from app.llm.base import LLMProvider
from app.llm.mock import MockProvider

ROOT = Path(__file__).resolve().parent.parent
GLOBUS = "ТРЦ «Глобус», Екатеринбург (ул. Щербакова, 4)"
KIT = "ТРЦ «КИТ», Екатеринбург (ул. Амундсена, 65)"
NOTE = "НДС в прайсе не указан; 720 выходов в месяц; ролик до 25 сек; вещание 10:00–22:00"
OLD_NOTE = "НДС в прайсе не указан — итоговую сумму подтверждает менеджер; 720 выходов в месяц; ролик до 25 сек"
CAVEAT = "НДС в прайсе не указан"


class Says(LLMProvider):
    def __init__(self, draft):
        self.draft = draft

    async def complete(self, system, messages):
        return json.dumps({"answer": self.draft, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


def radio(place, price, notes, per_hour=2):
    return PriceRow(f"Аудиоролик на радио ТРЦ, {per_hour} выхода в час", place, "1 месяц", price, notes)


def ask(rows, question, draft):
    return asyncio.run(Assistant(KnowledgeBase([], rows, []), Says(draft), min_score=0).answer(question))


def test_price_without_vat_goes_to_manager_as_draft_with_caveat():
    reply = ask([radio(GLOBUS, 25200, NOTE)], "Сколько стоит радио в Глобусе?",
                "Аудиоролик на радио ТРЦ «Глобус», 2 выхода в час — 25 200 ₽ за 1 месяц.")
    assert reply.decision == ESCALATE and reply.reason == R_VAT_UNKNOWN
    assert "25 200 ₽" in reply.text and CAVEAT in reply.text  # черновик для менеджера, а не «Уточню и вернусь»


def test_caveat_is_not_repeated_when_model_kept_it():
    draft = "Радио в Глобусе, 2 выхода в час — 25 200 ₽ за 1 месяц. НДС в прайсе не указан."
    reply = ask([radio(GLOBUS, 25200, NOTE)], "Сколько стоит радио в Глобусе?", draft)
    assert reply.reason == R_VAT_UNKNOWN and reply.text.count(CAVEAT) == 1


def test_old_note_with_manager_words_no_longer_loses_caveat():
    # Модель пересказала строку целиком, как заглушка: последняя фраза упоминает менеджера
    draft = (f"Аудиоролик на радио ТРЦ, 2 выхода в час, {GLOBUS} — 25 200 ₽ за 1 месяц. "
             "НДС в прайсе не указан — итоговую сумму подтверждает менеджер; 720 выходов в месяц; ролик до 25 сек.")
    reply = ask([radio(GLOBUS, 25200, OLD_NOTE)], "Сколько стоит радио в Глобусе?", draft)
    assert reply.reason == R_VAT_UNKNOWN and CAVEAT in reply.text


def test_vat_included_claim_for_row_without_vat_is_rejected():
    reply = ask([radio(GLOBUS, 25200, NOTE)], "Сколько стоит радио в Глобусе?",
                "Радио в Глобусе, 2 выхода в час — 25 200 ₽ за 1 месяц, НДС включён.")
    assert reply.decision == ESCALATE and reply.reason == f"{R_TERMS}:ндс"


def test_vat_rate_claim_for_row_without_vat_is_not_sent():
    # Ставку «5%» соседних строк в контексте Глобуса не найти: её снимает ещё проверка чисел
    rows = [radio(GLOBUS, 25200, NOTE), radio(KIT, 15120, "с НДС 5%; 720 выходов в месяц")]
    reply = ask(rows, "Сколько стоит радио в Глобусе?", "Радио в Глобусе, 2 выхода в час — 25 200 ₽ за 1 месяц с НДС 5%.")
    assert reply.decision == ESCALATE


def test_answer_without_format_words_next_to_price_still_escalates():
    # «2 выхода — 25 200 ₽» без слова «радио» в той же фразе: строку по формату не определить, по сумме — можно
    rows = [radio(GLOBUS, 25200, NOTE), radio(GLOBUS, 50400, NOTE, per_hour=4)]
    reply = ask(rows, "Радио в Глобусе сколько?", "В Глобусе на радио два тарифа. 2 выхода — 25 200 ₽, 4 — 50 400 ₽.")
    assert reply.reason == R_VAT_UNKNOWN and CAVEAT in reply.text


def test_mixed_answer_names_venue_without_vat():
    rows = [radio(GLOBUS, 25200, NOTE), radio(KIT, 15120, "с НДС 5%; 720 выходов в месяц")]
    draft = "Радио в КИТ, 2 выхода в час — 15 120 ₽ за 1 месяц. Радио в Глобусе, 2 выхода в час — 25 200 ₽ за 1 месяц."
    reply = ask(rows, "Сколько стоит радио в КИТ и Глобусе?", draft)
    assert reply.reason == R_VAT_UNKNOWN
    assert "НДС в прайсе не указан для позиций: ТРЦ «Глобус»" in reply.text


def test_rows_with_known_vat_are_answered_as_before():
    reply = ask([radio(KIT, 15120, "с НДС 5%; 720 выходов в месяц")], "Сколько стоит радио в КИТ?",
                "Радио в КИТ, 2 выхода в час — 15 120 ₽ за 1 месяц.")
    assert reply.decision == ANSWER and CAVEAT not in reply.text


def test_comparison_without_model_also_goes_to_manager():
    # «Сравни» собирается кодом без модели — та же оговорка и тот же черновик менеджеру
    rows = [PriceRow("Экран", "Бутово Молл, Москва", "1 месяц", 31000, "НДС в прайсе не указан"),
            PriceRow("Экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 неделя", 9000, "")]
    bot, state = BusinessAssistant(KnowledgeBase([], rows, []), MockProvider()), Conversation()
    asyncio.run(bot.answer("Бутово экран на месяц", conversation=state))
    reply = asyncio.run(bot.answer("В ВЕЕРе такой же на неделю. Сколько оба?", conversation=state))
    assert reply.decision == ESCALATE and reply.reason == R_VAT_UNKNOWN, reply.reason
    assert "31 000" in reply.text and "9 000" in reply.text and CAVEAT in reply.text


def test_manager_card_explains_why():
    assert "НДС" in REASONS[R_VAT_UNKNOWN]


@pytest.mark.parametrize("path", [ROOT / "data" / "kb" / "prices.csv"])
def test_price_notes_do_not_mention_manager(path):
    # Фразу с «менеджером» обрезка хвоста снимает целиком — вместе с условиями строки
    with path.open(encoding="utf-8") as fh:
        bad = [f"{r['placement']}: {r['format']}" for r in csv.DictReader(fh) if "менеджер" in r["notes"].lower()]
    assert not bad
