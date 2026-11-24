"""Выбор области и проверка старых правил через формы /learn."""
from unittest.mock import patch

import pytest
from lxml import html

from app import config
from app.admin import create_app
from tests.admin_client import AdminClient
from tests.test_candidate_review import VIDEO_LOSS, VIDEO_Q, VIDEO_SOURCE
from tests.test_candidates import JsonLLM, services_for


OLD = ('Можно ли использовать «макет»?', 'Нужен PDF & исходник.\nРазмер <А3>.')
VENUES = ["ВЕЕР Молл", "ДЕПО"]


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    with patch.dict("os.environ", clear=True):
        services = services_for(tmp_path, JsonLLM())
    services.settings.admin_token = "test"
    notes = services.settings.kb_dir / "notes" / "Из переписки"
    notes.mkdir(parents=True)
    (notes / "Общее.md").write_text(f"## {OLD[0]}\n{OLD[1]}\n", encoding="utf-8")
    with AdminClient(create_app(services)) as client:
        client.cookies.set("admin_token", "test")
        yield client, services


def candidate(services, question="Как подать макет?", answer="Нужен PDF."):
    return services.db.add_candidate(connection_id="test", chat_id=1, dialog_id=None, source="chat",
                                     client_question="В ДЕПО?", manager_text="Для ДЕПО нужен PDF.",
                                     question=question, answer=answer)


@pytest.mark.parametrize(("question", "answer", "selected"), [
    ("Как подать макет?", "Нужен PDF.", ""),
    ("Как подать макет?", "В ВЕЕР нужен PDF.", "ВЕЕР Молл"),
    ("В ВЕЕР нужен PDF?", "Да, как и в ДЕПО.", ""),
])
def test_candidate_suggests_only_a_single_venue_in_the_fact(panel, question, answer, selected):
    client, services = panel
    candidate(services, question, answer)
    page = html.fromstring(client.get("/learn").text)
    form = next(f for f in page.forms if f.action == "/candidates/approve")
    scope = form.inputs["scope"]
    assert form.method == "POST" and "required" in scope.attrib
    assert set(scope.value_options) == {"", "business", "case", *VENUES}
    assert scope.value == selected
    assert page.xpath('//a[contains(@href, "tab=unscoped")]')
    if not selected:
        assert "перед утверждением" in form.text_content()


def test_case_approval_reports_service_result_without_checking_the_bot(panel, monkeypatch):
    client, services = panel
    cid = candidate(services)
    monkeypatch.setattr("app.admin.answer_check", lambda *args: pytest.fail("Факт вне поиска"))
    response = client.post("/candidates/approve", data={"ids": cid, "question": "Вопрос?", "answer": "Ответ.",
                                                        "scope": "case", "source_reviewed": "true"})
    assert response.status_code == 200 and "только для этой заявки" in response.text
    assert services.db.get_candidate(cid)["status"] == "case"


def test_failed_approval_keeps_the_selected_scope_and_edit(panel):
    client, services = panel
    cid = candidate(services)
    response = client.post("/candidates/approve", data={"ids": cid, "question": "Правка?", "answer": "7 777 ₽.",
                                                        "scope": "ДЕПО"})
    form = next(f for f in html.fromstring(response.text).forms if f.action == "/candidates/approve")
    assert services.db.get_candidate(cid)["status"] == "new"
    assert form.fields["scope"] == "ДЕПО" and form.fields["question"] == "Правка?"


@pytest.mark.parametrize("scope", ["business", "ДЕПО", "case"])
def test_old_rule_can_be_reviewed_without_candidates(panel, scope):
    client, services = panel
    doc = html.fromstring(client.get("/learn").text)
    assert OLD[0] in doc.text_content() and OLD[1] in doc.text_content()
    form = next(f for f in doc.forms if "original_answer" in f.fields)
    assert form.method == "POST" and form.fields["scope"] == ""
    assert "required" in form.inputs["scope"].attrib
    data = dict(form.form_values())
    assert (data["question"], data["original_answer"]) == OLD
    assert form.inputs["answer"].tag == "textarea" and data["answer"] == OLD[1]
    data["answer"] = "" if scope == "case" else "Обязательно приложите PDF с полями."
    response = client.post(form.action, data={**data, "original_answer": OLD[1].replace("\n", "\r\n"), "scope": scope})
    assert response.status_code == 200 and not services.unscoped_facts()
    notes = services.settings.kb_dir / "notes" / "Из переписки"
    if scope == "case":
        assert all(OLD[1] not in chunk.text for chunk in services.draft_kb().chunks)
    else:
        name = "Для всех площадок" if scope == "business" else scope
        saved = (notes / f"{name}.md").read_text(encoding="utf-8")
        assert data["answer"] in saved and OLD[1] not in saved


@pytest.mark.parametrize("bad", [{"scope": ""}, {"scope": "чужая площадка"}, {"question": " "},
                                 {"original_answer": ""}, {"scope": "business", "answer": " "}])
def test_old_rule_rejects_invalid_form_before_saving(panel, monkeypatch, bad):
    client, services = panel
    monkeypatch.setattr(services, "review_unscoped_fact", lambda *args, **kwargs: pytest.fail("Неполная форма"))
    response = client.post("/learn/review", data={"question": OLD[0], "original_answer": OLD[1], "scope": "case", **bad})
    page = html.fromstring(response.text)
    assert page.xpath('//*[contains(@class,"note")]/text()')
    assert OLD[0] in page.text_content()


def test_stale_review_shows_conflict_and_current_answer(panel):
    client, services = panel
    response = client.post("/learn/review", data={"question": OLD[0], "original_answer": "Старый ответ.", "scope": "case"})
    assert "изменился" in response.text and OLD[1] in html.fromstring(response.text).text_content()
    assert services.unscoped_facts() == [OLD]


def test_warning_keeps_old_answer_for_retry_and_manager_can_confirm_the_edit(panel):
    client, services = panel
    old_file = services.settings.kb_dir / "notes" / "Из переписки" / "Общее.md"
    with old_file.open("a", encoding="utf-8") as f:
        f.write(f"\n## {VIDEO_Q}\n{VIDEO_LOSS}\n")
    cid = services.db.add_candidate(connection_id="test", chat_id=2, dialog_id=None, source="chat",
                                    client_question=VIDEO_Q, manager_text=VIDEO_SOURCE, question=VIDEO_Q, answer=VIDEO_LOSS)
    services.db.decide_candidates([cid], "approved")
    changed = VIDEO_LOSS + " Нужен готовый файл."
    payload = {"question": VIDEO_Q, "original_answer": VIDEO_LOSS, "scope": "ВЕЕР Молл", "answer": changed}
    response = client.post("/learn/review", data=payload)
    form = next(f for f in html.fromstring(response.text).forms if "original_answer" in f.fields)
    assert form.fields["original_answer"] == VIDEO_LOSS and form.fields["answer"] == changed
    assert form.fields["scope"] == "ВЕЕР Молл" and "source_reviewed" in form.fields
    assert VIDEO_SOURCE in response.text and VIDEO_LOSS in old_file.read_text(encoding="utf-8")
    response = client.post(form.action, data={**dict(form.form_values()), "source_reviewed": "true"})
    assert response.status_code == 200 and services.unscoped_facts() == [OLD]
    assert changed in old_file.with_name("ВЕЕР Молл.md").read_text(encoding="utf-8")


def test_old_rule_shows_all_original_questions_and_full_answers_as_text(panel):
    client, services = panel
    sources = [('А <b>мой</b> макет подойдёт?', 'Да.\nКроме макетов <i>без полей</i>.'),
               ('А для формата A & B?', 'Есть исключение для «А & Б».')]
    for i, (question, answer) in enumerate(sources, start=1):
        cid = services.db.add_candidate(connection_id="test", chat_id=i, dialog_id=None, source="chat",
                                        client_question=question, manager_text=answer, question=OLD[0], answer=OLD[1])
        services.db.decide_candidates([cid], "approved")
    doc = html.fromstring(client.get("/learn").text)
    details, = doc.xpath('//main//details[summary="Откуда взято"]')
    items = [item.text_content() for item in details.xpath(".//li")]
    for question, answer in sources:
        assert any(question in item and answer in item for item in items)
    assert "open" in details.attrib


def test_old_rule_without_sources_offers_manual_check_and_hide(panel):
    client, _ = panel
    doc = html.fromstring(client.get("/learn").text)
    assert not doc.xpath('//main//details[summary="Откуда взято"]')
    assert "сверьте" in doc.xpath("//main")[0].text_content().lower()
    assert doc.xpath('//select[@name="scope"]/option[@value="case"]')


def test_review_requested_by_service_can_be_confirmed_after_post(panel, monkeypatch):
    client, services = panel
    cid = services.db.add_candidate(connection_id="test", chat_id=1, dialog_id=None, source="chat",
                                    client_question=OLD[0], manager_text=OLD[1], question=OLD[0], answer=OLD[1])
    services.db.decide_candidates([cid], "approved")
    monkeypatch.setattr(services, "review_unscoped_fact", lambda *args, **kwargs: (False, "Нужна сверка с исходником."))
    response = client.post("/learn/review", data={"question": OLD[0], "original_answer": OLD[1],
                                                  "answer": OLD[1], "scope": "business"})
    assert "Нужна сверка с исходником." in response.text
    assert html.fromstring(response.text).xpath('//form//input[@name="source_reviewed"]')
