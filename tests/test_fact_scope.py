"""Область знания из переписки проверяется и при чтении старых версий."""
import pytest

from app.core import candidates
from app.core.candidate_review import review_candidate
from app.core.knowledge import KnowledgeBase
from tests.test_candidates import JsonLLM, make_kb, services_for


QUESTION = "Сколько метров отступить при монтаже?"
ANSWER = "Отступить пять метров от ориентира."


def add(services):
    return services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="observe",
                                     client_question=QUESTION, manager_text=ANSWER, question=QUESTION, answer=ANSWER)


def titles(kb, question):
    return [h.chunk.title for h in kb.retrieve(question)]


def test_old_published_fact_stays_out_of_all_searches(tmp_path):
    kb_dir = make_kb(tmp_path)
    note = kb_dir / "notes/Из переписки/Общее.md"
    candidates.add_to_note(note, "", QUESTION, ANSWER)
    kb = KnowledgeBase.load(kb_dir)
    assert QUESTION not in titles(kb, QUESTION)
    assert QUESTION not in titles(kb, "В ДЕПО сколько метров отступить при монтаже?")
    assert ANSWER in note.read_text()


def test_new_fact_without_scope_waits_for_manager(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    cid = add(services)
    assert not services.approve_candidates([cid], QUESTION, ANSWER, source_reviewed=True)[0]
    assert services.db.get_candidate(cid)["status"] == "new"
    assert services.kb_version == 1


def test_explicit_scope_separates_same_question_at_two_venues(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    for place, answer in [("ДЕПО", "Зелёный ориентир."), ("ВЕЕР Молл", "Красный ориентир.")]:
        assert services.approve_candidates([add(services)], QUESTION, answer, scope=place)[0]
    for place, present, absent in [("ДЕПО", "Зелёный", "Красный"), ("ВЕЕР", "Красный", "Зелёный"),
                                   ("", "", "ориентир.")]:
        text = " ".join(h.chunk.text for h in services.kb.retrieve(f"{QUESTION} {place}"))
        assert present in text and absent not in text
    assert len(services.db.candidates("approved")) == 2


@pytest.mark.parametrize("scope", ["case", "business"])
def test_legacy_review_checks_stale_forms_and_survives_reload(tmp_path, scope):
    services = services_for(tmp_path, JsonLLM())
    note = services.settings.kb_dir / "notes/Из переписки/Общее.md"
    candidates.add_to_note(note, "", QUESTION, ANSWER)
    candidates.add_to_note(note, "", "Как включить свет?", "Вызвать техника.")
    assert (QUESTION, ANSWER) in services.unscoped_facts()
    assert not services.review_unscoped_fact(QUESTION, "Чужой ответ", scope)[0]
    assert services.review_unscoped_fact(QUESTION, ANSWER, scope)[0]
    assert not services.review_unscoped_fact(QUESTION, ANSWER, scope)[0]
    assert services.unscoped_facts() == [("Как включить свет?", "Вызвать техника.")]
    if scope == "case":
        assert ANSWER in note.read_text(), "Частный ответ должен сохраниться даже без исходной записи в БД"
    kb = KnowledgeBase.load(services.version_dir(services.kb_version))
    assert (QUESTION in titles(kb, QUESTION)) == (scope == "business")


def test_case_specific_approval_preserves_source_but_never_enters_search(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    cid = add(services)
    assert services.approve_candidates([cid], QUESTION, ANSWER, scope="case")[0]
    record = services.db.get_candidate(cid)
    assert record["status"] == "case" and record["manager_text"] == ANSWER
    assert QUESTION not in titles(services.kb, QUESTION)


def test_rollback_to_legacy_version_returns_fact_to_review(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    note = services.settings.kb_dir / "notes/Из переписки/Общее.md"
    candidates.add_to_note(note, "", QUESTION, ANSWER)
    assert services.publish("Импорт старой версии")[0] == 2
    assert services.review_unscoped_fact(QUESTION, ANSWER, "business")[0]
    assert QUESTION in titles(services.kb, QUESTION)
    services.rollback(2)
    assert QUESTION not in titles(services.kb, QUESTION)
    assert services.unscoped_facts() == [(QUESTION, ANSWER)]


def test_legacy_review_can_show_exact_manager_source(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    cid = add(services)
    services.db.decide_candidates([cid], "approved", QUESTION, "Краткий пересказ.")
    assert services.unscoped_sources(QUESTION, "Краткий пересказ.") == [(QUESTION, ANSWER)]
    assert services.unscoped_sources(QUESTION, "Другая версия.") == []


def test_restoring_old_business_fact_checks_original_exception(tmp_path):
    services = services_for(tmp_path, JsonLLM())
    q, summary = "Можно ли ставить стенды?", "Стенды разрешены."
    source = "Стенды разрешены, кроме деревянных."
    cid = services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="observe",
                                    client_question=q, manager_text=source, question=q, answer=summary)
    services.db.decide_candidates([cid], "approved", q, summary)
    candidates.add_to_note(services.settings.kb_dir / "notes/Из переписки/Общее.md", "", q, summary)
    assert not services.review_unscoped_fact(q, summary, "business")[0]
    assert services.unscoped_facts() == [(q, summary)]
    assert services.review_unscoped_fact(q, summary, "business", answer=source)[0]
    assert any(source in h.chunk.text for h in services.kb.retrieve(q))


@pytest.mark.parametrize(("question", "answer", "source"), [
    ("Можно ли ставить стенды?", "Стенды разрешены.", "Стенды разрешены, кроме деревянных."),
    ("Можно ли поставить конструкцию?", "Такие конструкции запрещены.",
     "Такие конструкции запрещены. Их можно только участникам выставки."),
    ("Какие требования к ролику?", "Текст должен укладываться в грань.",
     "Ролики подходят. На кубе текст должен укладываться в грань."),
])
def test_manager_sees_missing_exception_or_format(tmp_path, question, answer, source):
    kb = KnowledgeBase.load(make_kb(tmp_path))
    assert review_candidate(kb, question, answer, source)
    assert not review_candidate(kb, question, source, source)
