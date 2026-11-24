"""Период у каждой предложенной цены в ответе, который получает клиент."""
import asyncio
import json
import os
import re
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.core.answer_grounding import check_price_periods
from app.core.assistant import Assistant
from app.core.knowledge import KnowledgeBase, PriceRow, SearchHit
from app.llm.base import LLMProvider


class Says(LLMProvider):
    def __init__(self, draft):
        self.draft = draft

    async def complete(self, system, messages):
        return json.dumps({"answer": self.draft, "confidence": 1, "needs_manager": False}, ensure_ascii=False)


def ask(rows, question, draft):
    return asyncio.run(Assistant(KnowledgeBase([], rows, []), Says(draft), min_score=0).answer(question))


@pytest.mark.parametrize("base,notes", [(45360, ""), (37800, "в ноябре–декабре 45 360 ₽")])
def test_budget_list_qualifies_all_four_radio_rates_and_the_unknown_lightbox_term(base, notes):
    malls = ["ВЕЕР", "Глобус", "Омега", "ДЕПО"]
    rows = [PriceRow("Радио", mall, "1 месяц", base, notes) for mall in malls]
    rows.append(PriceRow("Лайтбокс", "Бутово", "", 48000, ""))
    draft = "\n".join(["Ваш бюджет — 50 000 ₽.", "Лайтбокс в Бутово — 48 000 ₽.",
                       *(f"Радио в {mall} — 45 360 ₽." for mall in malls)])
    reply = ask(rows, "Что можно разместить? Бюджет 50 тысяч.", draft)
    assert reply.is_answer and reply.llm_called, reply.reason
    lines = reply.text.splitlines()
    assert lines[0] == "Ваш бюджет — 50 000 ₽."
    assert "не указан" in lines[1] and "месяц" not in lines[1]
    for mall, line in zip(malls, lines[2:], strict=True):
        assert mall in line and re.search(r"45 360 ₽ (?:за |в )(?:1 )?месяц", line)


@pytest.mark.parametrize("separator", ["; ", ", ", "\n"])
def test_equal_prices_keep_each_venues_own_term(separator):
    rows = [PriceRow("Радио", "ДЕПО", "1 месяц", 45360, ""),
            PriceRow("Радио", "ВЕЕР", "2 недели", 45360, "")]
    reply = ask(rows, "Сколько стоит радио в ДЕПО и ВЕЕР?",
                f"Радио в ДЕПО — 45 360 ₽{separator}радио в ВЕЕР — 45 360 рублей.")
    assert reply.is_answer and reply.llm_called, reply.reason
    assert re.search(r"ДЕПО — 45 360 ₽ (?:за |в )(?:1 )?месяц", reply.text)
    assert "ВЕЕР — 45 360 рублей за 2 недели" in reply.text


@pytest.mark.parametrize("period", ["", "1 месяц"])
def test_wrong_model_term_is_rejected_even_at_the_budget_limit(period):
    row = PriceRow("Радио", "ДЕПО", period, 45360, "")
    reply = ask([row], "Радио в ДЕПО, бюджет 45 360 ₽.", "Радио в ДЕПО — 45 360 ₽ в неделю.")
    assert reply.reason == "period_mismatch:[45360]"


def test_budget_and_printing_are_not_placement_rates_even_at_the_same_amount():
    row = PriceRow("Баннер", "ДЕПО", "1 месяц", 12000, "печать 12 000 ₽")
    draft = "Баннер в ДЕПО — 12 000 ₽. Печать — 12 000 ₽."
    reply = ask([row], "Сколько стоит баннер в ДЕПО?", draft)
    assert reply.is_answer and "12 000 ₽ за 1 месяц" in reply.text, reply.reason
    assert reply.text.endswith("Печать — 12 000 ₽.")
    reply = ask([PriceRow("Радио", "ДЕПО", "1 месяц", 12000, "")], "Бюджет 12 000 ₽.",
                "Ваш бюджет — 12 000 ₽. Радио в ДЕПО — 12 000 ₽.")
    assert reply.is_answer, reply.reason
    assert reply.text.startswith("Ваш бюджет — 12 000 ₽.")
    assert "Радио в ДЕПО — 12 000 ₽ за 1 месяц" in reply.text


def test_existing_billing_label_is_preserved():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")
    for draft in ("Радио в ДЕПО — 45 360 рублей в месяц.", "Радио в ДЕПО за месяц — 45 360 ₽."):
        reply = ask([row], "Сколько стоит радио в ДЕПО?", draft)
        assert reply.is_answer and reply.text == draft


def test_ambiguous_rate_does_not_take_the_first_period():
    rows = [PriceRow("Баннер", "ДЕПО — атриум", "1 месяц", 48000, ""),
            PriceRow("Баннер", "ДЕПО — вход", "1 неделя", 48000, "")]
    reply = ask(rows, "Сколько стоит баннер в ДЕПО?", "Баннер в ДЕПО — 48 000 ₽.")
    assert reply.is_answer and "уточнить" in reply.text
    assert "месяц" not in reply.text and "недел" not in reply.text


def test_already_explicit_unknown_is_not_repeated():
    draft = "Лайтбокс в Бутово — 48 000 ₽ (срок в прайсе не указан)."
    reply = ask([PriceRow("Лайтбокс", "Бутово", "", 48000, "")], "Сколько стоит лайтбокс в Бутово?", draft)
    assert reply.is_answer and reply.text == draft


def test_unknown_term_in_separate_sentence_applies_only_to_preceding_price():
    rows = [PriceRow("Лайтбокс", "Бутово", "", 48000, ""), PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")]
    draft = "Лайтбокс в Бутово — 48 000 ₽. Срок не указан. Радио в ДЕПО — 45 360 ₽."
    reply = ask(rows, "Что есть на бюджет 50 тысяч?", draft)
    assert reply.is_answer and reply.text.startswith("Лайтбокс в Бутово — 48 000 ₽. Срок не указан.")
    assert "45 360 ₽ за 1 месяц" in reply.text


def test_budget_and_surcharge_named_after_the_amount_get_no_period():
    row = PriceRow("Баннер", "ДЕПО", "1 месяц", 12000, "монтаж 12 000 ₽")
    draft = "12 000 ₽ — за монтаж. Баннер в ДЕПО — 12 000 ₽."
    reply = ask([row], "Сколько стоит баннер в ДЕПО?", draft)
    assert reply.is_answer and reply.text.startswith("12 000 ₽ — за монтаж.")
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 12000, "")
    draft = "12 000 ₽ — ваш бюджет. Радио в ДЕПО — 12 000 ₽."
    reply = ask([row], "Бюджет 12 000 ₽.", draft)
    assert reply.is_answer and reply.text.startswith("12 000 ₽ — ваш бюджет.")


def test_wrongly_declared_unknown_is_retried_instead_of_sent():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")
    draft = "Радио в ДЕПО — 45 360 ₽ (срок не указан)."
    reply = ask([row], "Сколько стоит радио в ДЕПО?", draft)
    assert reply.reason == "period_mismatch:[45360]"


def test_generated_season_price_keeps_its_billing_period():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 37800, "в ноябре–декабре 45 360 ₽")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", "Радио в ДЕПО — 37 800 ₽.")
    assert reply.is_answer and "45 360 ₽ за 1 месяц" in reply.text


def test_explicit_season_rate_uses_the_unit_from_the_season_note():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 37800, "в декабре 45 360 ₽ за неделю")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", "В декабре радио в ДЕПО — 45 360 ₽.")
    assert reply.is_answer and "45 360 ₽ за неделю" in reply.text


@pytest.mark.parametrize("draft", ["Радио в ДЕПО — 45 360 ₽ за час.", "Радио в ДЕПО — 45 360 ₽ за сезон."])
def test_other_named_billing_unit_is_not_overwritten(draft):
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", draft)
    assert reply.reason == "period_mismatch:[45360]" and reply.text == draft


def test_playback_frequency_is_not_the_billing_period():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "144 выхода в день")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", "Радио в ДЕПО — 45 360 ₽, 144 выхода в день.")
    assert reply.is_answer and "45 360 ₽ за 1 месяц, 144 выхода в день" in reply.text


def test_radio_frequency_in_list_title_is_not_the_price_period():
    title = "Аудиоролик на радио ТРЦ, 4 выхода в час"
    malls = ["Север", "Юг", "Запад", "Восток"]
    rows = [PriceRow(title, f"ТРЦ «{mall}», Екатеринбург", "1 месяц", 45360, "") for mall in malls]
    rows.append(PriceRow("Лайтбокс", "Бутово", "", 48000, ""))
    lines = [f"- {title}, ТРЦ «{mall}», Екатеринбург — 45 360 ₽" for mall in malls]
    draft = "\n".join(["Варианты размещения:", "- Лайтбокс в Бутово — 48 000 ₽", *lines])
    reply = ask(rows, "Бюджет 50 тысяч, какие есть варианты?", draft)
    assert reply.is_answer and reply.llm_called, reply.reason
    assert reply.text.splitlines()[-4:] == [line + " за 1 месяц" for line in lines]
    assert "48 000 ₽ (срок в прайсе не указан)" in reply.text


def test_radio_frequency_does_not_hide_explicit_hourly_price():
    title = "Аудиоролик на радио ТРЦ, 4 выхода в час"
    row = PriceRow(title, "ДЕПО", "1 месяц", 45360, "")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", f"{title}, ДЕПО — 45 360 ₽ за час.")
    assert reply.reason == "period_mismatch:[45360]"


def test_placement_term_before_price_still_rejects_a_wrong_week():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")
    draft = "Размещение на неделю — 45 360 ₽."
    reply = ask([row], "Сколько стоит радио в ДЕПО?", draft)
    assert reply.reason == "period_mismatch:[45360]"


def test_season_label_is_not_a_rate_for_the_entire_season():
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 37800, "в декабре 45 360 ₽")
    draft = "Радио в ДЕПО — 37 800 ₽ в месяц, в сезон — 45 360 ₽."
    reply = ask([row], "Сколько стоит радио в ДЕПО?", draft)
    assert reply.is_answer and "в сезон — 45 360 ₽ за 1 месяц" in reply.text, reply.reason


@pytest.mark.parametrize("price", ["45 360", "45,36 тыс.", "45360"])
def test_listed_price_without_currency_still_gets_its_term(price):
    row = PriceRow("Радио", "ДЕПО", "1 месяц", 45360, "")
    reply = ask([row], "Сколько стоит радио в ДЕПО?", f"Радио в ДЕПО — {price}")
    assert reply.is_answer and f"{price} за 1 месяц" in reply.text


def test_list_completed_from_price_rows_marks_missing_period():
    rows = [PriceRow("Баннер", f"ДЕПО — место № {i}", "1 месяц" if i < 5 else "", i * 10000, "")
            for i in range(1, 6)]
    reply = ask(rows, "Какие баннеры есть в ДЕПО?", "Баннер № 1 — 10 000 ₽. Баннер № 2 — 20 000 ₽.")
    assert reply.is_answer and reply.llm_called, reply.reason
    assert re.search(r"50 000 ₽[^.;]*не указан", reply.text)


@pytest.mark.skipif(not os.environ.get("SALES_REPLAY_ARCHIVE"), reason="для архивного повтора укажите SALES_REPLAY_ARCHIVE")
def test_archived_max_257_price_terms():
    with tarfile.open(os.environ["SALES_REPLAY_ARCHIVE"], "r:gz") as archive, TemporaryDirectory() as temp:
        db_bytes = bytearray(archive.extractfile("data/app.db").read())
        db_bytes[18:20] = bytes([1, 1])  # deserialize читает автономный снимок без WAL.
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(db_bytes)
            question, answer, sources, version = db.execute(
                "SELECT question, answer, sources, kb_version FROM dialogs WHERE id = 257 AND connection_id LIKE 'max:%'"
            ).fetchone()
        assert version == 7
        kb_path = "data/kb_versions/0007"
        files = [m for m in archive.getmembers() if m.isfile() and m.name.startswith(kb_path + "/")]
        archive.extractall(temp, members=files, filter="data")
        kb = KnowledgeBase.load(Path(temp) / kb_path)

    chunks = {c.id: c for c in [*kb.chunks, *kb.catalog]}
    hits = [SearchHit(chunks[source], 1.0) for source in json.loads(sources)]
    updated, bad = check_price_periods(kb, answer, hits, question)
    # Сравниваем только счётчики, чтобы pytest не вывел переписку при падении.
    radio = r"45\s?360\s*₽"
    rates = len(re.findall(radio, updated))
    monthly = len(re.findall(radio + r"\s+за 1 месяц\b", updated))
    lightboxes = len(re.findall(r"48\s?000\s*₽\s*\(срок в прайсе не указан\)", updated))
    assert (rates, monthly, lightboxes, bad) == (4, 4, 1, [])
