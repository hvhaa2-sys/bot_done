"""Память выбора и ограниченные уточнения: только синтетические диалоги, без сети."""
import asyncio
import json
import time

import pytest

from app import db as D
from app.config import ROOT
from app.core.assistant import ANSWER, ESCALATE, SKIP, Assistant
from app.core.conversation import Conversation
from app.core.knowledge import KnowledgeBase, PriceRow, parse_faq
from app.llm.base import LLMProvider

VEER = "ВЕЕР Молл, Екатеринбург"
DEPO = "ДЕПО Молл, Нижний Тагил"


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self):
        self.calls = []

    async def complete(self, system, messages):
        self.calls.append((system, messages))
        return json.dumps({"answer": "Ролик готовит рекламодатель.", "confidence": 1,
                           "needs_manager": False}, ensure_ascii=False)


@pytest.fixture
def kb():
    rows = [
        PriceRow("Цифровой экран", VEER + " — атриум", "1 месяц", 30000, ""),
        PriceRow("Баннер", VEER + " — парковка", "1 месяц", 40000, ""),
        PriceRow("Аудиоролик на радио", VEER, "1 месяц", 18000, ""),
        PriceRow("Промо-акция с промоутером для арендаторов", VEER, "полный месяц", 45000, ""),
        PriceRow("Промо-акция с промоутером для внешних рекламодателей", VEER,
                 "полный месяц", 150000, ""),
        PriceRow("Цифровой экран", DEPO + " — вход", "1 месяц", 50000, ""),
        PriceRow("Баннер", DEPO + " — галерея", "1 месяц", 60000, ""),
        PriceRow("Аудиоролик на радио", DEPO, "1 месяц", 20000, ""),
        PriceRow("Промо-акция с промоутером для арендаторов", DEPO, "полный месяц", 35000, ""),
        PriceRow("Промо-акция с промоутером для внешних рекламодателей", DEPO,
                 "полный месяц", 120000, ""),
    ]
    faq = parse_faq("## Кто готовит ролик для экрана?\nРолик готовит рекламодатель.\n"
                    "## Печать для экрана нужна?\nДля цифрового экрана печать не нужна.\n")
    return KnowledgeBase(faq, rows, ["скидк", "договор", "пришл"])


@pytest.fixture
def db(tmp_path):
    database = D.Database(tmp_path / "conversation.db")
    yield database
    database.conn.close()


def answer(assistant, state, question, history=None):
    return asyncio.run(assistant.answer(question, history, conversation=state))


def save(db, question, *, connection="first", chat=100, status=D.SENT_AUTO,
         decision=ANSWER, reason="", text="Ролик готовит рекламодатель."):
    return db.log_dialog(connection_id=connection, chat_id=chat, question=question, answer=text,
                         decision=decision, reason=reason, confidence=1, status=status,
                         sources=[], latency_ms=0, mode="auto")


def prices(kb, query):
    return [hit.chunk.price for hit in kb.retrieve(query) if hit.chunk.kind == "price"]


def test_selection_survives_more_than_three_follow_ups(kb):
    llm = FakeLLM()
    assistant = Assistant(kb, llm)
    state = Conversation()
    questions = ["Экран в ВЕЕР", "А печать?", "На три месяца", "Бюджет 80 тысяч", "А монтаж?"]
    for question in questions:
        reply = answer(assistant, state, question)
        assert reply.decision == ANSWER
        assert not reply.reason.startswith("clarification")
    assert state.venue == VEER
    assert state.format == "экран"
    assert "три месяца" in state.period
    assert state.budget == 80000
    assert "монтаж" in state.intent
    assert len(llm.calls) == len(questions)
    assert VEER in llm.calls[-1][0]
    assert "80000" in state.query(questions[-1])


def test_new_venue_replaces_old_and_forgets_place(kb):
    state = Conversation()
    state.observe(kb, "Сколько стоит экран в атриуме ВЕЕР?")
    assert state.place == "атриуме"
    state.observe(kb, "А в ДЕПО?")
    assert state.venue == DEPO
    assert state.place == ""
    assert state.format == "экран"
    assert "веер" not in state.query("А в ДЕПО?").lower()
    assert 30000 not in prices(kb, state.query("А в ДЕПО?"))
    assert 50000 in prices(kb, state.query("А в ДЕПО?"))


def test_new_format_forgets_old_place(kb):
    state = Conversation()
    state.observe(kb, "Баннер на парковке ВЕЕР")
    assert state.place == "парковке"
    state.observe(kb, "Лучше радио")
    assert state.format == "радио"
    assert state.place == ""
    assert state.venue == VEER


@pytest.mark.parametrize("question", ["Не ВЕЕР, а ДЕПО", "Не ВЕЕР, лучше ДЕПО"])
def test_rejected_venue_is_not_saved(kb, question):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    state.observe(kb, question)
    assert state.venue == DEPO
    assert "веер" not in state.query(question).lower()


def test_rejected_format_is_not_saved(kb):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    state.observe(kb, "Не экран, лучше радио")
    assert state.format == "радио"
    assert "экран" not in state.query("Не экран, лучше радио")


def test_tenant_price_does_not_follow_client_into_another_venue(kb):
    state = Conversation()
    state.observe(kb, "Мы арендаторы ВЕЕР, промоутер на месяц")
    assert state.client_type == "мы арендаторы"
    assert 45000 in prices(kb, state.query("Промоутер на месяц"))
    state.observe(kb, "А в ДЕПО?")
    assert state.client_type == ""
    assert 35000 not in prices(kb, state.query("А в ДЕПО?"))
    assert 120000 in prices(kb, state.query("А в ДЕПО?"))


def test_explicit_external_client_overrides_old_tenant(kb):
    state = Conversation()
    state.observe(kb, "Мы арендаторы ВЕЕР, промоутер на месяц")
    state.observe(kb, "Нет, мы не арендаторы")
    assert state.client_type == "внешние рекламодатели"
    assert 45000 not in prices(kb, state.query("Промоутер на месяц"))
    assert 150000 in prices(kb, state.query("Промоутер на месяц"))


def test_two_clarifications_then_escalation_without_llm(kb):
    llm = FakeLLM()
    assistant = Assistant(kb, llm)
    state = Conversation()
    first = answer(assistant, state, "Сколько стоит экран?")
    second = answer(assistant, state, "Не знаю")
    third = answer(assistant, state, "Пока не решил")
    assert first.reason == second.reason == "clarification:venue"
    assert first.decision == second.decision == ANSWER
    assert third.decision == ESCALATE and third.reason == "clarification_limit"
    assert state.clarifications == 2
    assert llm.calls == []


def test_thanks_and_greeting_do_not_reset_clarification_limit(kb):
    assistant = Assistant(kb, FakeLLM())
    state = Conversation()
    answer(assistant, state, "Сколько стоит экран?")
    assert answer(assistant, state, "Спасибо").decision == SKIP
    answer(assistant, state, "Здравствуйте")
    assert state.clarifications == 1
    assert answer(assistant, state, "Не знаю").reason.startswith("clarification:")
    assert answer(assistant, state, "Пока не решил").reason == "clarification_limit"


def test_stop_topic_is_not_replaced_with_a_clarifying_question(kb):
    llm = FakeLLM()
    assistant = Assistant(kb, llm)
    reply = answer(assistant, Conversation(), "Сколько стоит экран со скидкой?")
    assert reply.decision == ESCALATE and reply.reason.startswith("stop_topic:")
    assert llm.calls == []


def test_selection_is_available_after_escalation_but_stop_topic_is_not_replayed(kb):
    llm = FakeLLM()
    assistant = Assistant(kb, llm)
    state = Conversation()
    first = answer(assistant, state, "Экран в ВЕЕР, дадите скидку?")
    assert first.decision == ESCALATE
    second = answer(assistant, state, "А печать?")
    assert second.decision == ANSWER
    assert state.venue == VEER and state.format == "экран"
    assert "скидк" not in state.query("А печать?")
    assert len(llm.calls) == 1


@pytest.mark.parametrize("status", [D.DRAFT_PENDING, D.DRAFT_REJECTED, D.ESCALATED])
def test_unseen_draft_does_not_establish_a_second_option(kb, db, status):
    save(db, "Экран в ВЕЕР", status=status,
         text="Первый вариант — экран в атриуме, второй вариант — баннер на парковке.")
    state = Conversation.replay(kb, db.conversation_turns("first", 100))
    llm = FakeLLM()
    reply = answer(Assistant(kb, llm), state, "А второй вариант сколько стоит?")
    assert reply.reason == "clarification:position"
    assert state.format == "экран" and state.place == ""
    assert llm.calls == []


@pytest.mark.parametrize("status", [D.DRAFT_PENDING, D.DRAFT_REJECTED, D.ESCALATED])
def test_unsent_clarification_does_not_spend_the_limit(kb, db, status):
    save(db, "Сколько стоит экран?", reason="clarification:venue")
    save(db, "Не знаю", status=status, reason="clarification:venue")
    state = Conversation.replay(kb, db.conversation_turns("first", 100))
    assert state.clarifications == 1
    reply = answer(Assistant(kb, FakeLLM()), state, "Пока не решил")
    assert reply.reason == "clarification:venue"
    assert state.clarifications == 2


@pytest.mark.parametrize("status", [D.SENT_AUTO, D.DRAFT_SENT, D.DRAFT_EDITED])
def test_delivered_clarification_spends_the_limit(kb, db, status):
    save(db, "Сколько стоит экран?", status=status, reason="clarification:venue",
         text="В каком торговом центре рассматриваете размещение?")
    state = Conversation.replay(kb, db.conversation_turns("first", 100))
    assert state.clarifications == 1 and state.pending == "venue"


def test_database_replay_restores_selection_and_clarification_after_restart(kb, tmp_path):
    path = tmp_path / "restart.db"
    db = D.Database(path)
    save(db, "Экран в ВЕЕР")
    save(db, "А печать?")
    save(db, "На три месяца")
    save(db, "Бюджет 80 тысяч")
    save(db, "А второй вариант?", reason="clarification:position")
    db.conn.close()

    reopened = D.Database(path)
    try:
        state = Conversation.replay(kb, reopened.conversation_turns("first", 100))
        assert state.venue == VEER and state.format == "экран"
        assert "три месяца" in state.period and state.budget == 80000
        assert state.pending == "position" and state.clarifications == 1
    finally:
        reopened.conn.close()


def test_database_state_expires_with_history_window(kb, db):
    dialog_id = save(db, "Мы арендаторы ВЕЕР, экран на три месяца, бюджет 80 тысяч",
                     reason="clarification:position")
    db.conn.execute("UPDATE dialogs SET created_at=? WHERE id=?", (time.time() - 4 * 3600, dialog_id))
    db.conn.commit()
    assert db.conversation_turns("first", 100) == []
    assert Conversation.replay(kb, db.conversation_turns("first", 100)) == Conversation()


def test_database_state_is_isolated_by_chat_and_connection(kb, db):
    save(db, "Мы арендаторы ВЕЕР, промоутер на месяц, бюджет 80 тысяч", chat=100)
    save(db, "Экран в ДЕПО", chat=200)
    save(db, "Баннер в ДЕПО", connection="second", chat=100)
    first = Conversation.replay(kb, db.conversation_turns("first", 100))
    other_chat = Conversation.replay(kb, db.conversation_turns("first", 200))
    other_connection = Conversation.replay(kb, db.conversation_turns("second", 100))
    assert first.venue == VEER and first.client_type == "мы арендаторы" and first.budget == 80000
    assert other_chat.venue == DEPO and other_chat.format == "экран" and other_chat.client_type == ""
    assert other_connection.venue == DEPO and other_connection.format == "баннер"
    assert other_connection.budget is None and other_connection.client_type == ""


def test_explicit_reset_starts_a_new_selection(kb):
    state = Conversation()
    state.observe(kb, "Мы арендаторы ВЕЕР, экран на месяц, бюджет 80 тысяч")
    state.finish(ANSWER, "clarification:position")
    state.observe(kb, "Начнем сначала, баннер в ДЕПО")
    assert state.venue == DEPO and state.format == "баннер"
    assert state.client_type == "" and state.budget is None and state.period == ""
    assert state.pending == "" and state.clarifications == 0


def test_unrelated_question_drops_old_price_intent_instead_of_clarifying(kb):
    assistant = Assistant(kb, FakeLLM())
    state = Conversation()
    answer(assistant, state, "Сколько стоит экран в ВЕЕР?")
    reply = answer(assistant, state, "Как выращивать фиалки?")
    assert reply.decision == ESCALATE and reply.reason == "no_context"
    assert state.venue == "" and state.format == "" and state.intent == ""


def test_comparison_does_not_choose_one_venue_for_the_client(kb):
    state = Conversation()
    state.observe(kb, "Сколько стоит экран в ВЕЕР или ДЕПО?")
    assert state.venue == "" and state.multiple_venues
    assert state.clarification(kb, "Сколько стоит экран в ВЕЕР или ДЕПО?") is None
    assert {30000, 50000} <= set(prices(kb, state.query("Сколько стоит экран в ВЕЕР или ДЕПО?")))


def test_seasonal_month_survives_a_follow_up(kb):
    state = Conversation()
    state.observe(kb, "Сколько стоит экран в ВЕЕР на декабрь?")
    state.observe(kb, "А печать?")
    assert "декабр" in state.period
    assert "декабр" in state.query("А печать?")


@pytest.mark.parametrize("question", ["Не подскажете, сколько стоит экран в ВЕЕР?",
                                      "Не знаю, сколько стоит экран в ВЕЕР?"])
def test_polite_or_uncertain_intro_does_not_reject_named_selection(kb, question):
    llm = FakeLLM()
    state = Conversation()
    reply = answer(Assistant(kb, llm), state, question)
    assert state.venue == VEER and state.format == "экран"
    assert reply.decision == ANSWER and not reply.reason.startswith("clarification:")
    assert len(llm.calls) == 1


def test_tenant_status_scoped_to_one_venue_is_not_applied_to_another_in_same_message(kb):
    state = Conversation()
    question = "Мы арендаторы ВЕЕР, а сколько стоит промоутер в ДЕПО?"
    state.observe(kb, question)
    assert 35000 not in prices(kb, state.query(question))


def test_explicit_place_number_survives_follow_ups(kb):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР на месте 1")
    state.observe(kb, "А на декабрь?")
    assert state.place == "место № 1"
    assert "место № 1" in state.query("А печать?")
    state.observe(kb, "Лучше место № 2")
    assert state.place == "место № 2" and state.changed


def test_duration_after_place_word_does_not_invent_a_position_number(kb):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    state.observe(kb, "Нужно место 1 месяц")
    assert state.place == "" and "1 месяц" in state.period
    assert "№" not in state.query("А печать?")


@pytest.mark.parametrize("question", [
    "Свободен ли экран в атриуме ВЕЕР на ноябрь?",
    "Экран в ВЕЕР занят?", "Есть свободные места в ВЕЕР?",
    "Какие свободные даты у экрана ВЕЕР?", "Занятость экрана в ВЕЕР на декабрь",
])
def test_availability_goes_to_manager_even_if_model_could_reply_with_price(kb, question):
    llm = FakeLLM()
    reply = answer(Assistant(kb, llm), Conversation(), question)
    assert reply.decision == ESCALATE and reply.reason == "stop_topic:занятость"
    assert llm.calls == []


def test_free_shop_windows_are_a_format_not_availability(kb):
    llm = FakeLLM()
    reply = answer(Assistant(kb, llm), Conversation(), "Брендирование свободных витрин в ВЕЕР")
    assert reply.reason != "stop_topic:занятость"


@pytest.mark.parametrize("question", ["Сколько стоит эксклюзивное брендирование катка и ёлки?",
                                      "брендинг катка и елки сколько стоит"])
def test_unique_combination_of_formats_does_not_require_venue_with_real_kb(question):
    kb = KnowledgeBase.load(ROOT / "data" / "kb")
    state = Conversation()
    state.observe(kb, question)
    assert state.clarification(kb, question) is None


def test_dimensions_do_not_replace_selected_place_with_real_kb():
    kb = KnowledgeBase.load(ROOT / "data" / "kb")
    state = Conversation()
    state.observe(kb, "Экран в центральном атриуме ВЕЕР")
    selected = state.place
    assert "атриуме" in selected
    state.observe(kb, "Какая высота экрана?")
    assert state.place == selected


def test_price_from_old_venue_is_not_authorized_on_third_turn(kb):
    class PriceLLM(FakeLLM):
        async def complete(self, system, messages):
            self.calls.append((system, messages))
            replies = ["Экран в ВЕЕР — 30 000 ₽ в месяц.", "Экран в ДЕПО — 50 000 ₽ в месяц.",
                       "Размещение — 30 000 ₽ в месяц."]
            text = replies[min(len(self.calls) - 1, len(replies) - 1)]
            return json.dumps({"answer": text, "confidence": 1, "needs_manager": False}, ensure_ascii=False)

    assistant = Assistant(kb, PriceLLM())
    state = Conversation()
    first_question = "Сколько стоит экран в ВЕЕР?"
    first = answer(assistant, state, first_question)
    assert first.decision == ANSWER and "30 000" in first.text
    second_question = "А в ДЕПО?"
    second = answer(assistant, state, second_question, [(first_question, first.text)])
    assert second.decision == ANSWER and "50 000" in second.text
    history = [(first_question, first.text), (second_question, second.text)]
    third = answer(assistant, state, "А на месяц?", history)
    assert state.venue == DEPO
    assert third.decision == ESCALATE, "Цена прежнего ТЦ не должна проходить через старый ответ в истории"


def test_repeated_rejection_of_venue_exhausts_clarification_limit(kb):
    llm = FakeLLM()
    assistant = Assistant(kb, llm)
    state = Conversation()
    first = answer(assistant, state, "Сколько стоит экран?")
    second = answer(assistant, state, "Не ВЕЕР")
    third = answer(assistant, state, "Не ВЕЕР")
    assert first.reason == second.reason == "clarification:venue"
    assert third.decision == ESCALATE and third.reason == "clarification_limit"
    assert state.clarifications == 2
    assert llm.calls == []


@pytest.mark.parametrize("question", ["Нужна реклама для магазина", "Требования к ролику"])
def test_business_and_creative_details_keep_the_selected_screen_with_real_kb(question):
    kb = KnowledgeBase.load(ROOT / "data" / "kb")
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    selected_format, selected_venue = state.format, state.venue
    assert selected_format == "экран" and "ВЕЕР" in selected_venue
    state.observe(kb, question)
    assert state.format == selected_format, "Описание бизнеса или ролика не выбирает новый рекламный формат"
    assert state.venue == selected_venue


@pytest.mark.parametrize("question", ["А в других ТЦ?", "А во всех ТЦ?"])
def test_other_or_all_venues_drop_old_selection_and_tenant_status(kb, question):
    state = Conversation()
    state.observe(kb, "Мы арендаторы ВЕЕР, сколько стоит экран?")
    assert state.venue == VEER and state.client_type == "мы арендаторы"
    state.observe(kb, question)
    assert state.venue == "" and state.client_type == ""
    assert state.clarification(kb, question) is None
    assert {30000, 50000} <= set(prices(kb, state.query(question)))


@pytest.mark.parametrize("question", ["Ролик у нас готов", "Уже есть готовое видео"])
def test_ready_creative_does_not_replace_selected_screen_with_real_kb(question):
    kb = KnowledgeBase.load(ROOT / "data" / "kb")
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    selected_venue = state.venue
    state.observe(kb, question)
    assert state.format == "экран" and state.venue == selected_venue
    assert not state.changed


def test_unrelated_question_resets_selection_after_all_venues(kb):
    state = Conversation()
    state.observe(kb, "Экран в ВЕЕР")
    state.observe(kb, "А во всех ТЦ?")
    assert state.multiple_venues and state.format == "экран"
    llm = FakeLLM()
    reply = answer(Assistant(kb, llm), state, "Какая сегодня погода?")
    assert reply.decision == ESCALATE and reply.reason == "no_context"
    assert state.venue == "" and state.format == "" and state.intent == ""
    assert not state.multiple_venues
    assert llm.calls == []


def test_full_manager_edits_do_not_spend_clarification_limit(kb, db):
    for question in ("Сколько стоит экран?", "А цена экрана?"):
        save(db, question, status=D.DRAFT_EDITED, reason="clarification:venue",
             text="Экран в ВЕЕР стоит 30 000 рублей в месяц.")
    state = Conversation.replay(kb, db.conversation_turns("first", 100))
    assert state.pending == "" and state.clarifications == 0
    reply = answer(Assistant(kb, FakeLLM()), state, "А печать?")
    assert reply.reason != "clarification_limit"


def test_manager_edit_that_keeps_actual_clarification_still_spends_limit(kb, db):
    for question in ("Сколько стоит экран?", "Не знаю"):
        save(db, question, status=D.DRAFT_EDITED, reason="clarification:venue",
             text="В каком торговом центре рассматриваете размещение?")
    state = Conversation.replay(kb, db.conversation_turns("first", 100))
    assert state.pending == "venue" and state.clarifications == 2
    reply = answer(Assistant(kb, FakeLLM()), state, "Пока не решил")
    assert reply.decision == ESCALATE and reply.reason == "clarification_limit"
