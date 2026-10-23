import csv
import io
import sqlite3
from dataclasses import replace
from html import escape

import pytest
from fastapi.testclient import TestClient

from app.admin import create_app
from app.config import ROOT, Settings
from app.db import Database
from app.services import Services
from eval.run_eval import contains, load_questions


@pytest.fixture
def review_client(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    for name in ("faq.md", "prices.csv", "stop_topics.txt"):
        (kb_dir / name).write_text(
            (ROOT / "tests" / "fixtures" / "kb" / name).read_text(encoding="utf-8"), encoding="utf-8")
    settings = replace(Settings.load(), db_path=tmp_path / "a.db", kb_dir=kb_dir,
                       llm_provider="mock", admin_token="secret")
    services = Services(settings)
    with TestClient(create_app(services)) as client:
        client.cookies.set("admin_token", "secret")
        yield client, services
    services.db.conn.close()


def add_dialog(services, *, question="Сколько стоит экран?", decision="answer", flagged=True):
    dialog_id = services.db.log_dialog(
        connection_id=None, chat_id=None, question=question, answer="Экран стоит 20 000 ₽.",
        decision=decision, reason="", confidence=0.9, status="sandbox", sources=[], latency_ms=1, mode="draft")
    if flagged:
        services.db.flag_dialog(dialog_id, True)
    return dialog_id


def facts_review(**changes):
    return {
        "error_type": "facts", "expected_decision": "answer",
        "corrected_answer": "Экран стоит 30 000 ₽.", "must_contain": "30000", **changes,
    }


def export_rows(client):
    response = client.get("/dialogs/flagged.csv?token=secret")
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_old_flagged_database_requires_review_and_migrates_once(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE dialogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT, chat_id INTEGER,
                question TEXT NOT NULL, answer TEXT NOT NULL, decision TEXT NOT NULL,
                reason TEXT, confidence REAL, status TEXT NOT NULL, sources TEXT,
                latency_ms INTEGER, mode TEXT, created_at REAL NOT NULL,
                flagged INTEGER NOT NULL DEFAULT 0, learned INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO dialogs (question, answer, decision, status, created_at, flagged)
            VALUES ('Сколько стоит экран?', '20 000 ₽', 'answer', 'sent_auto', 1, 1);
        """)
    for _ in range(2):
        db = Database(path)
        try:
            row = db.get_dialog(1)
            assert row["flagged"] == 1 and row["reviewed"] == 0
            assert row["decision"] == "answer" and row["answer"] == "20 000 ₽"
            for field in ("error_type", "expected_decision", "corrected_answer", "must_contain"):
                assert row[field] == ""
        finally:
            db.conn.close()


def test_unreviewed_flags_never_enter_exam(review_client):
    client, services = review_client
    for decision in ("answer", "escalate"):
        add_dialog(services, decision=decision)
    assert len(services.db.recent_dialogs(10, flagged=True)) == 2
    assert export_rows(client) == []


def test_wrong_price_stays_answer_and_exports_explicit_facts(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    assert services.db.review_dialog(dialog_id, **facts_review())
    row = services.db.get_dialog(dialog_id)
    assert row["reviewed"] == 1 and row["error_type"] == "facts"
    assert row["decision"] == row["expected_decision"] == "answer"
    assert row["answer"] == "Экран стоит 20 000 ₽."
    assert row["corrected_answer"] == "Экран стоит 30 000 ₽."
    assert export_rows(client) == [{
        "question": "Сколько стоит экран?", "expected": "answer", "must_contain": "30000"}]


def test_wrong_escalation_requires_explicit_answer_and_facts(review_client):
    client, services = review_client
    dialog_id = add_dialog(services, decision="escalate")
    assert services.db.review_dialog(dialog_id, **facts_review(error_type="decision"))
    assert services.db.get_dialog(dialog_id)["decision"] == "escalate"
    assert export_rows(client)[0]["expected"] == "answer"


def test_escalation_review_discards_answer_facts(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    assert services.db.review_dialog(dialog_id, **facts_review(
        error_type="decision", expected_decision="escalate"))
    row = services.db.get_dialog(dialog_id)
    assert row["corrected_answer"] == row["must_contain"] == ""
    assert export_rows(client)[0] == {
        "question": "Сколько стоит экран?", "expected": "escalate", "must_contain": ""}


@pytest.mark.parametrize("changes", [
    {"error_type": "other"},
    {"expected_decision": "any"},
    {"expected_decision": "escalate"},
    {"error_type": "decision"},
    {"corrected_answer": "   "},
    {"must_contain": "   "},
    {"must_contain": "!20000"},
    {"must_contain": "40000"},
    {"must_contain": "30000;!30000"},
    {"corrected_answer": "Экран стоит 131 500 ₽.", "must_contain": "31500"},
    {"corrected_answer": "30000" + "а" * 4092},
    {"must_contain": "30000;" + "а" * 995},
])
def test_invalid_review_does_not_confirm_flag(review_client, changes):
    _, services = review_client
    dialog_id = add_dialog(services)
    before = dict(services.db.get_dialog(dialog_id))
    with pytest.raises(ValueError):
        services.db.review_dialog(dialog_id, **facts_review(**changes))
    assert dict(services.db.get_dialog(dialog_id)) == before


def test_fact_alternatives_negations_and_number_spacing(review_client):
    _, services = review_client
    dialog_id = add_dialog(services)
    assert services.db.review_dialog(dialog_id, **facts_review(
        must_contain="30000|31000;!20000"))
    assert contains("Цена 30 000 ₽", "30000")
    assert not contains("Цена 131 500 ₽", "31500")


def test_review_masks_personal_data(review_client):
    _, services = review_client
    dialog_id = add_dialog(services)
    assert services.db.review_dialog(dialog_id, **facts_review(
        corrected_answer="Экран стоит 30 000 ₽. Пишите sales@example.com.",
        must_contain="30000;sales@example.com"))
    row = services.db.get_dialog(dialog_id)
    assert "sales@example.com" not in row["corrected_answer"]
    assert "[почта]" in row["corrected_answer"]
    assert row["must_contain"] == "30000;[почта]"


def test_repeated_flag_preserves_review_but_unflag_resets_it(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    services.db.review_dialog(dialog_id, **facts_review())
    before = dict(services.db.get_dialog(dialog_id))
    services.db.flag_dialog(dialog_id, True)
    assert dict(services.db.get_dialog(dialog_id)) == before
    services.db.flag_dialog(dialog_id, False)
    row = services.db.get_dialog(dialog_id)
    assert not row["flagged"] and not row["reviewed"]
    for field in ("error_type", "expected_decision", "corrected_answer", "must_contain"):
        assert row[field] == ""
    assert export_rows(client) == []
    services.db.flag_dialog(dialog_id, True)
    assert services.db.get_dialog(dialog_id)["reviewed"] == 0
    assert export_rows(client) == []


def test_missing_and_unflagged_dialogs_cannot_be_reviewed(review_client):
    client, services = review_client
    dialog_id = add_dialog(services, flagged=False)
    for target in (dialog_id, dialog_id + 1):
        assert not services.db.review_dialog(target, **facts_review())
        response = client.post(f"/dialogs/{target}/review", data=facts_review())
        assert response.status_code == 404


def test_review_and_csv_require_authentication(review_client):
    client, services = review_client
    client.cookies.clear()
    dialog_id = add_dialog(services)
    response = client.post(f"/dialogs/{dialog_id}/review", data=facts_review())
    assert response.status_code == 401
    assert client.get(f"/dialogs/{dialog_id}/review").status_code == 401
    assert client.get("/dialogs/flagged.csv").status_code == 401
    assert services.db.get_dialog(dialog_id)["reviewed"] == 0


def test_review_route_validates_and_preserves_original_decision(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    response = client.post(f"/dialogs/{dialog_id}/review", data=facts_review(must_contain="40000"))
    assert response.status_code == 200
    assert services.db.get_dialog(dialog_id)["reviewed"] == 0
    response = client.post(f"/dialogs/{dialog_id}/review", data={
        **facts_review(), "back": "/dialogs?f=flagged"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dialogs?f=flagged"
    row = services.db.get_dialog(dialog_id)
    assert row["reviewed"] == 1 and row["decision"] == "answer"


def test_review_route_handles_deep_negations_without_server_error(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    checks = "30000;" + "!" * 993 + "x"
    response = client.post(f"/dialogs/{dialog_id}/review", data=facts_review(
        must_contain=checks), follow_redirects=False)
    assert response.status_code in (200, 303)


def test_review_fields_render_as_text(review_client):
    client, services = review_client
    dialog_id = add_dialog(services)
    payload = 'Экран стоит 30 000 ₽. </textarea><script>alert("x")</script>'
    response = client.post(f"/dialogs/{dialog_id}/review", data=facts_review(
        corrected_answer=payload), follow_redirects=False)
    assert response.status_code == 303
    response = client.get(f"/dialogs/{dialog_id}/review")
    assert '<script>alert("x")</script>' not in response.text
    assert escape(payload) in response.text


def test_csv_bom_quotes_newlines_and_exam_loader_roundtrip(review_client, tmp_path):
    client, services = review_client
    question = 'Сколько стоит "экран",\nесли на месяц?'
    dialog_id = add_dialog(services, question=question)
    services.db.review_dialog(dialog_id, **facts_review(must_contain="30000;!20000"))
    response = client.get("/dialogs/flagged.csv?token=secret")
    assert response.content.startswith(b"\xef\xbb\xbf")
    expected = [{"question": question, "expected": "answer", "must_contain": "30000;!20000"}]
    assert list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))) == expected
    path = tmp_path / "questions_reviewed.csv"
    path.write_bytes(response.content)
    assert load_questions(path) == expected
