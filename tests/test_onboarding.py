"""Мастер сверяется с живым состоянием: прошлые подтверждения не разрешают новый запуск."""
import asyncio
import json
from dataclasses import replace

import pytest
import httpx

from app import onboarding as O
from app.core.assistant import Reply
from app.services import Services
from tests.test_admin import make_client
from tests.test_import import full_export

CASES = {"question_0": "Какие требования к макету?", "expected_0": "answer", "fact_0": "150 dpi",
         "question_1": "Сколько стоит экран в атриуме на месяц?", "expected_1": "answer", "fact_1": "30 000",
         "question_2": "Дадите скидку?", "expected_2": "escalate", "fact_2": ""}


def connect(client, services, connection_id="ours", owner=1):
    services.bot_status = {"ok": True, "business": True, "username": "setup_demo_bot"}
    services.db.upsert_connection(connection_id, owner, owner, True, True, "off")
    return client.post("/onboarding/channel", data={"business_name": "Учебная студия", "connection_id": connection_id})


def prepare(client, services):
    connect(client, services)
    client.post("/onboarding/source", data={"source": "documents"})
    response = client.post("/onboarding/policy", data={
        "target_mode": "draft", "stop_topics": "скидк\nдоговор", "holding_message": "Передам вопрос менеджеру."})
    assert response.status_code == 200
    services.publish("Подготовка учебной базы")
    response = client.post("/onboarding/knowledge", data={"reviewed": "1", "context": O.knowledge_context(services)})
    assert "Проверить ответы" in response.text


def fake_answers(monkeypatch, services, *, real=False):
    if real:
        services.settings = replace(services.settings, llm_provider="gigachat")

    async def answer(question):
        if question == CASES["question_0"]:
            return Reply("answer", "Макет — PDF, 150 dpi.", 1)
        if question == CASES["question_1"]:
            return Reply("answer", "Экран стоит 30 000 ₽ за месяц.", 1)
        return Reply("escalate", "Передам вопрос менеджеру.", 1, reason="stop_topic:скидк")

    monkeypatch.setattr(services, "answer", answer)


def confirm(client, services, report=None):
    report = report or O.load(services)["report"]
    return client.post("/onboarding/review", data={"reviewed": "1", "context": report["context"], "report_id": report["id"]})


def test_five_steps_on_empty_install_without_model_calls(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    for name in ("faq.md", "prices.csv"):
        (services.settings.kb_dir / name).write_text("", encoding="utf-8")
    services.publish("Пустой бизнес", force=True)

    async def forbidden(*args, **kwargs):
        raise AssertionError("просмотр мастера не вызывает модель")

    monkeypatch.setattr(services, "answer", forbidden)
    state = O.progress(services)
    assert not state.ready and not any(s.done for s in state.steps)
    assert state.next_step == "channel"
    for key in (*O.TITLES, "nonexistent"):
        response = client.get("/onboarding", params={"step": key})
        assert response.status_code == 200 and "Внедрение в бизнес" in response.text
    assert "Продолжить внедрение" in client.get("/").text


@pytest.mark.parametrize("path", ["/onboarding", "/onboarding?step=check"])
def test_auth_required(tmp_path, path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get(path).status_code == 401
    assert client.post("/onboarding/source", data={"source": "documents"}).status_code == 401


def test_connection_must_be_enabled_approved_and_healthy(tmp_path):
    client, services = make_client(tmp_path)
    connect(client, services)
    assert O.progress(services).steps[0].done
    services.bot_status = {"ok": False}
    assert not O.progress(services).steps[0].done
    services.bot_status = {"ok": True, "business": True}
    services.db.conn.execute("UPDATE connections SET approved=0 WHERE id='ours'")
    services.db.conn.commit()
    assert not O.progress(services).steps[0].done
    assert "Подключение недоступно" in client.post("/onboarding/channel", data={
        "business_name": "Другая студия", "connection_id": "ours"}).text
    assert O.load(services)["business_name"] == "Учебная студия"


def test_choose_source_does_not_enable_observe(tmp_path):
    client, services = make_client(tmp_path)
    connect(client, services)
    response = client.post("/onboarding/source", data={"source": "observe"})
    assert "Ждём вопрос" in response.text
    assert services.db.connections()[0].mode == "off"
    services.db.observe_message(connection_id="elsewhere", chat_id=2, message_id=1, role="client", text="Вопрос", sent_at=1)
    services.db.observe_message(connection_id="elsewhere", chat_id=2, message_id=2, role="manager", text="Ответ", sent_at=2)
    assert not O.progress(services).steps[1].done
    for i, role in enumerate(("client", "manager"), 1):
        services.db.observe_message(connection_id="ours", chat_id=1, message_id=i, role=role, text=role, sent_at=i)
    assert O.progress(services).steps[1].done


def test_import_stays_in_wizard_and_does_not_approve_facts(tmp_path):
    client, services = make_client(tmp_path)
    connect(client, services)
    client.post("/onboarding/source", data={"source": "export"})
    before = O.knowledge_context(services)
    response = client.post("/import", data={"back": "onboarding"},
                           files={"files": ("result.json", full_export(tmp_path).read_bytes(), "application/json")})
    assert "Внедрение в бизнес" in response.text and "Импорт готов" in response.text
    assert O.progress(services).steps[1].done and not O.progress(services).steps[3].done
    assert O.knowledge_context(services) == before
    assert services.db.candidates("approved") == []


def test_upload_returns_to_wizard_and_validates_all_files_before_replacing(tmp_path):
    client, services = make_client(tmp_path)
    before = (services.settings.kb_dir / "faq.md").read_bytes()
    response = client.post("/kb/upload", data={"back": "onboarding"}, files={
        "faq": ("faq.md", "## Где вход?\nВход со двора.\n".encode(), "text/markdown"),
        "prices": ("prices.csv", b"not a price table", "text/csv")})
    assert "Внедрение в бизнес" in response.text and "не удалось разобрать" in response.text
    assert (services.settings.kb_dir / "faq.md").read_bytes() == before
    response = client.post("/kb/upload", data={"back": "onboarding"}, files={
        "faq": ("faq.md", b"\xff\xfe", "text/markdown")})
    assert "UTF-8" in response.text and (services.settings.kb_dir / "faq.md").read_bytes() == before
    response = client.post("/kb/upload", data={"back": "onboarding"}, files={
        "faq": ("faq.md", "## Где вход?\nВход со двора.\n".encode(), "text/markdown")})
    assert "Внедрение в бизнес" in response.text and "Загружено: faq.md" in response.text


def test_note_upload_returns_to_wizard(tmp_path):
    client, services = make_client(tmp_path)
    response = client.post("/kb/notes/upload", data={"back": "onboarding"}, files={
        "files": ("Вход.md", "## Где вход?\nВход со двора.\n".encode(), "text/markdown")})
    assert "Внедрение в бизнес" in response.text and "Загружено заметок: 1" in response.text
    assert (services.settings.kb_dir / "notes" / "Вход.md").exists()


def test_policy_saves_draft_without_mode_or_default_change(tmp_path):
    client, services = make_client(tmp_path)
    connect(client, services)
    initial = services.settings.default_mode
    response = client.post("/onboarding/policy", data={
        "target_mode": "auto", "stop_topics": "скидк\nвозврат", "holding_message": "Уточним у менеджера."})
    assert response.status_code == 200
    assert services.settings.default_mode == initial and services.db.connections()[0].mode == "off"
    assert "возврат" in services.draft_kb().stop_topics
    assert services.draft_changed() and O.progress(services).steps[2].done
    assert services.holding_message == "Уточним у менеджера."


@pytest.mark.parametrize("changes", [{"target_mode": "bad"}, {"stop_topics": "# только комментарий"}, {"holding_message": ""}])
def test_invalid_policy_is_not_partially_applied(tmp_path, changes):
    client, services = make_client(tmp_path)
    before = (services.settings.kb_dir / "stop_topics.txt").read_bytes()
    holding = services.holding_message
    response = client.post("/onboarding/policy", data={
        "target_mode": "draft", "stop_topics": "непубликуемая", "holding_message": "Изменённая фраза", **changes})
    assert "Не сохранено" in response.text
    assert before == (services.settings.kb_dir / "stop_topics.txt").read_bytes()
    assert services.holding_message == holding and O.load(services) == {}


def test_knowledge_receipt_requires_current_published_nonempty_data(tmp_path):
    client, services = make_client(tmp_path)
    old = O.knowledge_context(services)
    (services.settings.kb_dir / "faq.md").write_text("## Где вход?\nСо двора.\n", encoding="utf-8")
    response = client.post("/onboarding/knowledge", data={"reviewed": "1", "context": old})
    assert "Сначала проверьте" in response.text and not O.load(services).get("knowledge_review")
    services.publish("Новые материалы", force=True)
    response = client.post("/onboarding/knowledge", data={"reviewed": "1", "context": old})
    assert "Версия базы изменилась" in response.text
    client.post("/onboarding/knowledge", data={"reviewed": "1", "context": O.knowledge_context(services)})
    assert O.progress(services).steps[3].done


def test_publish_from_wizard_runs_existing_checks_and_returns_to_knowledge(tmp_path):
    client, services = make_client(tmp_path)
    connect(client, services)
    client.post("/onboarding/policy", data={"target_mode": "draft", "stop_topics": "скидк",
                                            "holding_message": "Передам менеджеру."})
    assert services.draft_changed()
    response = client.post("/kb/publish", data={"back": "onboarding"})
    assert "Внедрение в бизнес" in response.text and "Опубликована версия" in response.text
    assert not services.draft_changed() and not O.progress(services).steps[3].done


def test_concurrent_runs_are_rejected_without_duplicate_model_calls(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)

    async def check():
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(question):
            calls.append(question)
            started.set()
            await finish.wait()
            return Reply("escalate", "Менеджеру", 1)

        monkeypatch.setattr(services, "answer", slow)
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers={"Origin": "http://testserver"}, cookies={"admin_token": "secret"}) as web:
            first = asyncio.create_task(web.post("/onboarding/check", data=CASES))
            await asyncio.wait_for(started.wait(), timeout=2)
            second = await web.post("/onboarding/check", data=CASES)
            assert second.status_code == 409
            finish.set()
            assert (await first).status_code == 200
        assert len(calls) == 3

    asyncio.run(check())


def test_all_steps_with_human_review_and_no_delivery(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services, real=True)
    response = client.post("/onboarding/check", data=CASES)
    assert "Автопроверка пройдена" in response.text
    assert not O.progress(services).ready
    response = confirm(client, services)
    assert "Настройки для режима" in response.text and O.progress(services).ready
    assert services.db.stats()["total"] == 0, "проверка не имитирует отправку клиентам и не портит метрики"
    assert services.db.connections()[0].mode == "off"


def test_mock_can_be_rehearsed_but_never_proves_readiness(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services)
    client.post("/onboarding/check", data=CASES)
    confirm(client, services)
    state = O.progress(services)
    assert state.report["passed"] and not state.ready and not state.steps[-1].done
    assert "Проверена заглушка" in client.get("/onboarding?step=check").text


@pytest.mark.parametrize("change", ["draft", "publish", "model", "holding", "target", "question", "threshold"])
def test_changes_invalidate_checked_answers(tmp_path, monkeypatch, change):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services, real=True)
    client.post("/onboarding/check", data=CASES)
    old = O.load(services)["report"]
    confirm(client, services)
    assert O.progress(services).ready
    if change in ("draft", "publish"):
        path = services.settings.kb_dir / "faq.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n## Когда открыто?\nЕжедневно.\n", encoding="utf-8")
        if change == "publish":
            services.publish("Новая версия", force=True)
    elif change == "model":
        services.settings = replace(services.settings, gigachat_model="GigaChat-Max")
    elif change == "holding":
        services.overrides["holding_message"] = "Позовём человека."
    elif change == "target":
        O.save(services, target_mode="auto")
    elif change == "question":
        cases = O.load(services)["cases"]
        cases[0]["fact"] = "300 dpi"
        O.save(services, cases=cases)
    else:
        services.settings = replace(services.settings, confidence_threshold=0.99)
        services.overrides["confidence_threshold"] = "0.99"
    assert not O.progress(services).ready and not O.progress(services).report_current
    assert "Нужна успешная проверка" in confirm(client, services, old).text


def test_review_binds_to_specific_run_not_only_same_configuration(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services, real=True)
    client.post("/onboarding/check", data=CASES)
    previous = O.load(services)["report"]
    client.post("/onboarding/check", data=CASES)
    current = O.load(services)["report"]
    assert previous["context"] == current["context"] and previous["id"] != current["id"]
    assert "Нужна успешная проверка" in confirm(client, services, previous).text
    assert not O.progress(services).ready


def test_daily_limit_does_not_become_successful_escalation(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    services.overrides["daily_llm_limit"] = "1"
    services.db.add_usage(services.local_now().strftime("%Y-%m-%d"), 1)

    async def forbidden(*args, **kwargs):
        raise AssertionError("лимит должен остановить вызов")

    monkeypatch.setattr(services, "answer", forbidden)
    client.post("/onboarding/check", data=CASES)
    report = O.load(services)["report"]
    assert not report["passed"] and all(not r["passed"] and "лимит" in r["issue"] for r in report["rows"])


def test_model_error_is_not_a_correct_manager_handoff(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)

    async def error(question):
        return Reply("escalate", "Уточним.", 0, reason="llm_error:Timeout")

    monkeypatch.setattr(services, "answer", error)
    client.post("/onboarding/check", data=CASES)
    assert not any(r["passed"] for r in O.load(services)["report"]["rows"])


def test_failed_run_cannot_be_human_approved(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services, real=True)
    client.post("/onboarding/check", data={**CASES, "fact_1": "99999"})
    assert "Нужна успешная проверка" in confirm(client, services).text
    assert not O.progress(services).ready


def test_state_changed_during_model_call_invalidates_whole_run(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)

    async def changing_answer(question):
        O.save(services, business_name="Другое название")
        return Reply("answer", "150 dpi, 30 000", 1)

    monkeypatch.setattr(services, "answer", changing_answer)
    client.post("/onboarding/check", data=CASES)
    assert not O.load(services)["report"]["passed"] and not O.progress(services).report_current


@pytest.mark.parametrize("changes", [{"question_0": ""}, {"question_0": CASES["question_1"]},
                                      {"expected_0": "skip"}, {"fact_0": ""}, {"expected_2": "answer", "fact_2": "да"}])
def test_bad_exam_does_not_call_model(tmp_path, monkeypatch, changes):
    client, services = make_client(tmp_path)
    prepare(client, services)

    async def forbidden(*args, **kwargs):
        raise AssertionError("невалидная форма не вызывает модель")

    monkeypatch.setattr(services, "answer", forbidden)
    response = client.post("/onboarding/check", data={**CASES, **changes})
    assert response.status_code == 200 and not O.load(services).get("report")


def test_changing_business_clears_receipts_not_existing_materials(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    before = O.knowledge_context(services)
    client.post("/onboarding/channel", data={"business_name": "Другой бизнес", "connection_id": "ours"})
    assert O.knowledge_context(services) == before
    assert not O.progress(services).steps[2].done and not O.progress(services).steps[3].done


def test_state_survives_restart_and_bad_json_is_recoverable(tmp_path):
    client, services = make_client(tmp_path)
    prepare(client, services)
    again = Services(services.base_settings)
    assert O.load(again)["business_name"] == "Учебная студия"
    assert O.progress(again).steps[2].done and O.progress(again).steps[3].done
    services.db.set_settings({O.KEY: "[invalid"})
    assert client.get("/onboarding").status_code == 200 and O.load(services) == {}
    services.db.set_settings({O.KEY: json.dumps([])})
    assert O.load(services) == {}


def test_html_values_are_escaped_and_secret_errors_not_shown(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    O.save(services, business_name='<img src=x onerror="alert(1)">')
    response = client.get("/onboarding?step=channel")
    assert '<img src=x' not in response.text and "&lt;img" in response.text

    async def error(question):
        raise RuntimeError("SECRET_MUST_NOT_APPEAR")

    monkeypatch.setattr(services, "answer", error)
    response = client.post("/onboarding/check", data=CASES)
    assert "SECRET_MUST_NOT_APPEAR" not in response.text
    assert "Не удалось получить ответ" in response.text


def test_real_reply_pipeline_works_in_offline_rehearsal(tmp_path):
    client, services = make_client(tmp_path)
    prepare(client, services)
    response = client.post("/onboarding/check", data=CASES)
    assert response.status_code == 200
    report = O.load(services)["report"]
    assert len(report["rows"]) == 3 and report["mock"]
    assert report["rows"][2]["passed"]
    assert services.db.connections()[0].mode == "off"


def test_policy_external_change_requires_new_review(tmp_path):
    client, services = make_client(tmp_path)
    prepare(client, services)
    assert O.progress(services).steps[2].done
    (services.settings.kb_dir / "stop_topics.txt").write_text("другая тема\n", encoding="utf-8")
    assert not O.progress(services).steps[2].done


def test_missing_reply_permission_blocks_ready_state(tmp_path, monkeypatch):
    client, services = make_client(tmp_path)
    prepare(client, services)
    fake_answers(monkeypatch, services, real=True)
    client.post("/onboarding/check", data=CASES)
    confirm(client, services)
    services.db.conn.execute("UPDATE connections SET can_reply=0 WHERE id='ours'")
    services.db.conn.commit()
    state = O.progress(services)
    assert all(s.done for s in state.steps) and not state.ready
    assert any("нет права" in b for b in state.blockers)


def test_run_check_uses_independent_questions_not_customer_conversation(tmp_path, monkeypatch):
    _, services = make_client(tmp_path)
    seen = []

    async def answer(question, *args, **kwargs):
        seen.append((args, kwargs))
        return Reply("escalate", "Менеджеру", 1)

    monkeypatch.setattr(services, "answer", answer)
    asyncio.run(O.run_check(services, O.cases_from_form(CASES)))
    assert seen == [((), {})] * 3
