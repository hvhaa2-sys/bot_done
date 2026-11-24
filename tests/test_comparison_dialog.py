"""Сравнение запрошенных размещений через тот же ассистент, что у каналов."""
import asyncio
import json
import os
import tarfile
from pathlib import Path

import pytest

from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow
from app.core.real_flow import BusinessAssistant
from app.llm.mock import MockProvider


FIRST = "Бутово экран на месяц"
SECOND = "В ВЕЕРе такой же на неделю"
BOTH = "Сколько оба?"
BUTOVO = "Бутово Молл, Москва"
VEER = "ВЕЕР Молл, Екатеринбург"
ONLY_WEEK = "А в ВЕЕРе такой же, только на неделю. Сколько оба?"


def assistant(week="1 неделя", extra=(), *, month="1 месяц", notes=""):
    rows = [PriceRow("Экран", BUTOVO, month, 31000, notes),
            PriceRow("Экран", VEER + " — атриум", week, 9000, ""),
            PriceRow("Баннер", VEER, "1 месяц", 14000, ""), *extra]
    return BusinessAssistant(KnowledgeBase([], rows, ["скидк", "брон"]), MockProvider())


def ask(bot, question, **kwargs):
    return asyncio.run(bot.answer(question, **kwargs))


def assert_quotes(reply):
    assert reply.is_answer, reply.reason
    lines = reply.text.lower().splitlines()
    assert any("бутово" in s and "экран" in s and "месяц" in s and "31 000" in s for s in lines), reply.text
    assert any("веер" in s and "экран" in s and "недел" in s and "9 000" in s for s in lines), reply.text
    assert set(reply.sources) == {"price-0", "price-1"}
    assert "40 000" not in reply.text


def test_two_requested_rates_survive_venue_change():
    bot, state = assistant(), Conversation()
    ask(bot, FIRST, conversation=state)
    assert_quotes(ask(bot, SECOND + ". " + BOTH, conversation=state))
    assert BUTOVO in state.parameters() and VEER in state.parameters()


@pytest.mark.parametrize("second", [
    ONLY_WEEK,
    "Такой же в ВЕЕРе только на неделю, сколько оба?",
    "А в ВЕЕРе только на неделю такой же. Сколько оба?",
])
def test_only_the_second_duration_changes(second):
    bot, state = assistant(), Conversation()
    ask(bot, "В Бутово экран на месяц", conversation=state)
    assert_quotes(ask(bot, second, conversation=state))
    assert [(p.venue, p.period, p.format) for p in state.placements] == [
        (BUTOVO, "на месяц", "экран"), (VEER, "на неделю", "экран")]


def test_separate_price_question_replays_client_words_from_legacy_history():
    bot = assistant()
    history = [(FIRST, "По этой заявке 99 000 ₽."), (SECOND, "Предлагаю баннер.")]
    assert_quotes(ask(bot, BOTH, history=history))
    turns = [dict(question=q, answer=a, decision="answer", reason="", status="sent_auto") for q, a in history]
    assert_quotes(ask(bot, BOTH, conversation=Conversation.replay(bot.kb, turns)))


@pytest.mark.parametrize("week", ["1 месяц", ""])
def test_unlisted_week_is_not_quoted_as_a_weekly_rate(week):
    bot = assistant(week)
    reply = ask(bot, SECOND + ". " + BOTH, last_question=FIRST)
    assert reply.decision == "escalate" and reply.sources == ["price-0"]
    assert "31 000" in reply.text and BUTOVO in reply.text
    assert "недел" in reply.text.lower() and VEER in reply.text
    assert "9 000" not in reply.text and "40 000" not in reply.text


def test_specific_place_resolves_ambiguity_for_one_of_the_two():
    another = PriceRow("Экран", VEER + " — парковка", "1 неделя", 7000, "")
    bot, state = assistant(extra=[another]), Conversation()
    ask(bot, FIRST, conversation=state)
    reply = ask(bot, SECOND + ". " + BOTH, conversation=state)
    assert reply.reason.startswith("clarification:") and VEER in reply.text and BUTOVO in reply.text
    assert "9 000" not in reply.text and "7 000" not in reply.text
    assert_quotes(ask(bot, "В атриуме", conversation=state))


def test_replacement_and_duration_clarification_leave_one_selection():
    bot, state = assistant(), Conversation()
    ask(bot, FIRST, conversation=state)
    ask(bot, SECOND + ". " + BOTH, conversation=state)
    ask(bot, "Нет, только баннер в ВЕЕРе", conversation=state)
    ask(bot, "На месяц", conversation=state)
    summary = state.parameters()
    assert VEER in summary and "баннер" in summary and "месяц" in summary
    assert BUTOVO not in summary and "недел" not in summary


@pytest.mark.parametrize("separator", ["\n", ", и "])
def test_two_placements_in_a_merged_message(separator):
    assert_quotes(ask(assistant(), FIRST + separator + SECOND + ". " + BOTH))


def test_comparison_after_a_single_selection_asks_for_the_other_placement():
    bot, state = assistant(), Conversation()
    ask(bot, "Экран в Бутово", conversation=state)
    ask(bot, "Ещё уточню, на месяц", conversation=state)
    reply = ask(bot, BOTH, conversation=state)
    assert reply.reason.startswith("clarification:") and BUTOVO in reply.text
    assert "втор" in reply.text.lower() and "месяц" in reply.text
    assert not state.placements


def test_ordinal_changes_only_the_first_period():
    bot, state = assistant(), Conversation()
    ask(bot, FIRST, conversation=state)
    ask(bot, SECOND + ". " + BOTH, conversation=state)
    reply = ask(bot, "Первый на два месяца. Сколько оба?", conversation=state)
    assert reply.decision == "escalate" and BUTOVO in reply.text and "два месяца" in reply.text
    assert "31 000" not in reply.text and "9 000" in reply.text and VEER in reply.text


def test_legacy_history_restores_which_placement_needed_a_place():
    bot = assistant(extra=[PriceRow("Экран", BUTOVO + " — парковка", "1 месяц", 23000, ""),
                           PriceRow("Экран", BUTOVO + " — у лифта", "1 месяц", 22000, "")])
    second = SECOND + ". " + BOTH
    clarify = ask(bot, second, last_question=FIRST)
    assert clarify.reason.startswith("clarification:")
    reply = ask(bot, "На парковке", history=[(FIRST, ""), (second, clarify.text)])
    assert reply.is_answer and "23 000" in reply.text and "9 000" in reply.text
    assert "22 000" not in reply.text and "31 000" not in reply.text


def test_clarifying_missing_first_venue_keeps_the_second():
    bot, state = assistant(), Conversation()
    ask(bot, "Экран на месяц", conversation=state)
    clarify = ask(bot, SECOND + ". " + BOTH, conversation=state)
    assert clarify.reason.startswith("clarification:") and VEER in clarify.text
    assert_quotes(ask(bot, "В Бутово", conversation=state))


def test_second_format_is_preserved_for_a_short_price_follow_up():
    bot, state = assistant(), Conversation()
    ask(bot, FIRST, conversation=state)
    ask(bot, "И ещё баннер в ВЕЕРе на месяц", conversation=state)
    reply = ask(bot, "А сколько стоят?", conversation=state)
    assert reply.is_answer and {BUTOVO, VEER} <= {item.venue for item in state.placements}
    assert "31 000" in reply.text and "14 000" in reply.text and "баннер" in reply.text.lower()
    assert "9 000" not in reply.text


def test_instead_of_named_venue_replaces_the_first_placement():
    bot, state = assistant(), Conversation()
    ask(bot, FIRST, conversation=state)
    ask(bot, "Вместо Бутово в ВЕЕРе такой же на неделю", conversation=state)
    assert not state.placements
    assert (state.venue, state.format, state.period) == (VEER, "экран", "на неделю")


@pytest.mark.parametrize(("request_text", "reason"), [
    ("Замените ещё и ролик", "business_operation"),
    ("Выставьте счёт", "business_operation"),
    ("Оба свободны?", "business_operation"),
    ("Можно скидку?", "stop_topic:"),
])
def test_price_question_with_an_operation_keeps_original_route(request_text, reason):
    bot = assistant()
    reply = ask(bot, SECOND + ". " + BOTH + " " + request_text, last_question=FIRST)
    assert not reply.is_answer and reply.reason.startswith(reason)
    assert not reply.llm_called


def test_extra_status_request_cannot_be_answered_just_with_a_comparison():
    reply = ask(assistant(), SECOND + ". " + BOTH + " Ключи от стойки уже передали?", last_question=FIRST)
    assert reply.decision == "escalate"
    assert "₽" not in reply.text


def test_no_confirmed_rate_uses_standard_handoff_without_a_draft():
    bot, state = assistant(week="", month=""), Conversation()
    ask(bot, FIRST, conversation=state)
    reply = ask(bot, SECOND + ". " + BOTH, conversation=state)
    assert (reply.decision, reply.text, reply.sources) == ("escalate", bot.holding_message, [])
    assert BUTOVO in state.parameters() and VEER in state.parameters()


def test_partial_comparison_reaches_manager_card_and_dialog_log(tmp_path, monkeypatch):
    from app import config
    from app.channels.flow import process_client_message
    from app.core.knowledge import prices_csv
    from app.services import Services

    with monkeypatch.context() as local:
        local.setattr(config, "ROOT", tmp_path)
        local.setattr(config.os, "environ", {"KB_DIR": "kb", "DB_PATH": "chat.db", "EXAM_FILES": ""})
        settings = config.Settings.load()
    settings.kb_dir.mkdir()
    (settings.kb_dir / "prices.csv").write_text(prices_csv(assistant(week="").kb.prices), encoding="utf-8")
    services = Services(settings, MockProvider())
    conn = services.db.upsert_connection("test", 1, 2, True, True, "auto")
    asyncio.run(process_client_message(services, conn, 3, FIRST))
    out = asyncio.run(process_client_message(services, conn, 3, SECOND + ". " + BOTH))
    assert out.to_manager and "31 000" in out.to_manager and BUTOVO in out.to_manager
    assert VEER in out.to_manager and "недел" in out.to_manager and "9 000" not in out.to_manager
    entry = services.db.get_dialog(out.dialog_id)
    assert entry["status"] == "escalated" and json.loads(entry["sources"]) == ["price-0"]


def test_requested_season_and_extra_charge_stay_with_their_placement():
    bot = assistant(notes="в декабре 37 200 ₽; монтаж оплачивается отдельно 2 500 ₽")
    reply = ask(bot, SECOND + ". " + BOTH, last_question=FIRST + " в декабре")
    assert reply.is_answer
    first, second = reply.text.lower().splitlines()
    assert "бутово" in first and "37 200" in first and "декабр" in first and "месяц" in first
    assert "отдельно" in first and "2 500" in first and "9 000" not in first
    assert "9 000" in second and "2 500" not in second


@pytest.mark.parametrize("notes", ["минимальный срок 3 месяца", "минимальный срок по согласованию",
                                   "в декабре сезонная наценка 20%", "в декабре 37 200 ₽ за 2 недели"])
def test_unconfirmed_or_inapplicable_rate_keeps_other_quote_in_handoff(notes):
    bot = assistant(notes=notes)
    reply = ask(bot, SECOND + ". " + BOTH, last_question=FIRST + " в декабре")
    assert reply.decision == "escalate" and reply.sources == ["price-1"]
    assert "9 000" in reply.text and BUTOVO in reply.text and "декабр" in reply.text.lower()
    assert "31 000" not in reply.text and "37 200" not in reply.text


def test_clarification_limit_keeps_verified_price_in_manager_draft():
    another = PriceRow("Экран", VEER + " — парковка", "1 неделя", 7000, "")
    bot, state = assistant(extra=[another]), Conversation()
    ask(bot, FIRST, conversation=state)
    ask(bot, SECOND + ". " + BOTH, conversation=state)
    ask(bot, BOTH, conversation=state)
    reply = ask(bot, BOTH, conversation=state)
    assert reply.decision == "escalate" and reply.reason == "clarification_limit"
    assert reply.sources == ["price-0"] and "31 000" in reply.text and VEER in reply.text


def test_missing_rate_and_ambiguous_second_placement_need_manager():
    another = PriceRow("Экран", VEER + " — парковка", "1 неделя", 7000, "")
    bot = assistant(month="", extra=[another])
    reply = ask(bot, SECOND + ". " + BOTH, last_question=FIRST)
    assert (reply.decision, reply.text, reply.sources) == ("escalate", bot.holding_message, [])


def test_unpriced_surcharge_keeps_both_base_rates_in_manager_draft():
    bot = assistant(notes="монтаж оплачивается отдельно")
    reply = ask(bot, SECOND + ". " + BOTH, last_question=FIRST)
    assert reply.decision == "escalate" and set(reply.sources) == {"price-0", "price-1"}
    first, second = reply.text.lower().splitlines()
    assert "31 000" in first and "монтаж" in first and "уточн" in first
    assert "9 000" in second and "монтаж" not in second


@pytest.mark.skipif(not os.environ.get("SALES_REPLAY_ARCHIVE"), reason="нужен локальный архив Buff")
def test_comparison_on_published_kb_0007(tmp_path):
    prefix = "data/kb_versions/0007/"
    with tarfile.open(os.environ["SALES_REPLAY_ARCHIVE"], "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            rel = Path(member.name.removeprefix(prefix))
            if rel.is_absolute() or ".." in rel.parts:
                continue
            note = rel.parts[0] == "notes" and rel.suffix == ".md"
            if str(rel) not in {"prices.csv", "faq.md", "stop_topics.txt"} and not note:
                continue
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    bot, state = BusinessAssistant(KnowledgeBase.load(tmp_path), MockProvider()), Conversation()
    first = "В Бутово экран на месяц"
    ask(bot, first, conversation=state)
    reply = ask(bot, ONLY_WEEK, conversation=state)
    assert [(p.venue, p.format, p.period) for p in state.placements] == [
        (BUTOVO, "экран", "на месяц"), (VEER, "экран", "на неделю")]
    assert reply.decision == "escalate" and reply.reason == "no_context"
    butovo, veer = reply.text.lower().splitlines()
    assert "бутово" in butovo and "30 000" in butovo and "36 000" in butovo and "месяц" in butovo
    assert "веер" in veer and "недел" in veer and "₽" not in veer
    assert reply.sources == ["price-96"]
