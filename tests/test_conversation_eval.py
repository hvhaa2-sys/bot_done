"""Только offline: фиксированные ответы, miniKB, история и общий бюджет."""
import asyncio
import json
import socket
import sqlite3

import pytest

from eval import conversation_eval as E


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Eval-тест не должен обращаться к сети")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def kb(tmp_path):
    # Структура miniKB совпадает с существующим стендом, производственные данные не читаются.
    from app.core.knowledge import PriceRow, prices_csv

    folder = tmp_path / "mini-kb"
    folder.mkdir()
    (folder / "prices.csv").write_text(prices_csv([
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 30000, ""),
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — вход", "1 месяц", 50000, ""),
    ]), encoding="utf-8")
    return folder


@pytest.fixture
def isolated_settings(monkeypatch, kb, tmp_path):
    from app.config import Settings
    from tools import simulate_difficult as D

    # Не читаем .env и рабочие пути даже при загрузке настроек стендом.
    settings = Settings(
        db_path=tmp_path / "unused.db", kb_dir=kb, llm_provider="mock", telegram_bot_token="",
        gigachat_auth_key="", gigachat_scope="GIGACHAT_API_PERS", gigachat_model="GigaChat-Pro",
        gigachat_verify_ssl=True, gigachat_base_url="https://unused.invalid", openai_base_url="https://unused.invalid",
        openai_api_key="", openai_model="unused", openai_project="", default_mode="auto", retrieval_min_score=0.01,
        confidence_threshold=0.7, manager_pause_minutes=30, stale_message_minutes=60,
        admin_token="synthetic-only", admin_host="127.0.0.1", admin_port=0,
    )
    monkeypatch.setattr(D.Settings, "load", lambda: settings)

    def denied(*args, **kwargs):
        raise AssertionError("Без --allow-api создавать живой provider запрещено")

    monkeypatch.setattr(D, "make_provider", denied)
    monkeypatch.setattr(D, "published_kb", denied)
    return D


@pytest.fixture
def mini_suite(monkeypatch):
    suite = {"schema_version": 1, "synthetic": True, "suite": "regression", "cases": [
        {"id": "mini-client", "persona": "Спешит", "goal": "Узнать тариф", "risk": "Смена ТЦ", "turns": [
            {"messages": ["Сколько стоит цифровой экран в ВЕЕР на месяц?"], "checks": ["uncertain_terms"]},
            {"messages": ["Нет, лучше цифровой экран в ДЕПО на месяц. Сколько стоит?"], "checks": ["uncertain_terms"]},
            {"messages": ["А в ДЕПО цифровой экран на месяц 50000, правильно?"], "checks": ["uncertain_terms"]},
        ]},
    ], "learning": [{"id": "ready_video_two_constraints", "messages": [
        {"role": "client", "text": "Кто готовит ролик для ВЕЕР?"},
        {"role": "manager", "text": "Для экрана ВЕЕР ролик готовит рекламодатель, мы его не изготавливаем."},
    ], "expected": "Сохранить отрицание и поставщика", "risk": "Потеря отрицания"}]}
    monkeypatch.setattr(E, "load_suite", lambda name: json.loads(json.dumps(suite)))
    return suite


def turn(reply, decision="answer", reason=""):
    return {"visible_reply": reply, "decision": decision, "reason": reason}


@pytest.mark.parametrize(("kind", "reply", "decision", "reason", "status"), [
    ("sound_scope", "Экран со звуком расположен в Бутово Молл.", "answer", "", "fail"),
    ("sound_scope", "В ДЕПО экран со звуком.", "answer", "", "manual_review"),
    ("sound_scope", "Не могу подтвердить, что экран со звуком.", "answer", "", "manual_review"),
    ("sound_scope", "Экран не со звуком.", "answer", "", "manual_review"),
    ("sound_scope", None, "escalate", "model_unsure", "manual_review"),
    ("screen_period", "В Фан-Фан стоимость 31 500 ₽ за 2 недели.", "answer", "", "fail"),
    ("screen_period", "Фан-Фан: 31.5к за две недели.", "answer", "", "fail"),
    ("screen_period", "31 500 за месяц; стоимость двух недель уточню.", "escalate", "", "pass"),
    ("screen_period", "31 500 — это за месяц, не за 2 недели.", "answer", "", "manual_review"),
    ("budget_promise", "За 10 000 ₽ возможно размещение на арке. Цена 22 000 без печати.", "answer", "", "fail"),
    ("budget_promise", "Да, уложимся!", "answer", "", "manual_review"),
    ("budget_promise", "В 10 тысяч уложимся. Но размещение 22 000 ₽, плюс производство.", "answer", "", "fail"),
    ("screen_period", "В другом ТЦ стоимость 31 500 ₽ за 2 недели.", "answer", "", "manual_review"),
    ("budget_promise", "Не уложимся в 10 тысяч, один только показ дороже.", "answer", "", "manual_review"),
    ("regular_price_return", None, "escalate", "stop_topic:скидк", "fail"),
    ("regular_price_return", None, "escalate", "model_unsure", "fail"),
    ("regular_price_return", "Стоимость экрана за месяц — 30 000 ₽.", "answer", "", "pass"),
    ("regular_price_return", "На какой месяц нужен экран?", "clarify", "", "pass"),
    ("regular_price_return", "Уточню и вернусь.", "escalate", "", "manual_review"),
    ("regular_price_return", "Хорошо!", "answer", "", "manual_review"),
    ("uncertain_terms", "Гарантируем запуск.", "answer", "", "fail"),
    ("uncertain_terms", "Не гарантируем запуск.", "answer", "", "manual_review"),
    ("uncertain_terms", "Нужны точные даты, какой месяц рассматриваете?", "answer", "", "pass"),
])
def test_narrow_checks_do_not_treat_uncertainty_as_success(kind, reply, decision, reason, status):
    assert E.check_turn(kind, turn(reply, decision, reason))[0] == status


def test_frozen_regression_has_three_rounds_without_reading_mutable_inputs(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Regression не читает изменяемые входные JSON")

    monkeypatch.setattr(E, "read_json", denied)
    suite = E.load_suite("regression")
    assert sum(len(c["turns"]) for c in suite["cases"]) == 24
    assert len(suite["learning"]) == 6
    batches = E.input_sets(suite)
    assert [len(b["payload"]["cases"]) for b in batches] == [10, 10, 4, 6]
    assert [b["learning"] for b in batches] == [False, False, False, True]
    before = E.digest(suite)
    suite["cases"][0]["turns"][0]["messages"][0] = "изменено вызывающим"
    assert E.digest(E.load_suite("regression")) == before


def test_heldout_is_separate_six_new_cases():
    suite = E.load_suite("heldout")
    regression = E.load_suite("regression")
    assert len(suite["cases"]) == 6 and not suite.get("learning")
    assert all(1 <= len(c["turns"]) <= 2 for c in suite["cases"])
    assert {c["id"] for c in suite["cases"]}.isdisjoint(c["id"] for c in regression["cases"])
    base_questions = {m for c in regression["cases"] for t in c["turns"] for m in t["messages"]}
    assert not any(m in base_questions for c in suite["cases"] for t in c["turns"] for m in t["messages"])
    assert all(t["checks"] for c in suite["cases"] for t in c["turns"])


def test_known_saved_responses_reveal_the_four_failures():
    if not E.KNOWN_BASELINE.exists():  # прогоны в eval/simulation/runs/ живут только на машине, где их делали
        pytest.skip("нет сохранённого прогона eval/simulation/runs — он не входит в git")
    report = E.read_json(E.KNOWN_BASELINE)
    findings = {r["id"]: r for r in E.evaluate(report, E.load_suite("regression"))}
    expected = ["many_production_questions/round-1/sound_scope",
                "translit_keyboard_mix/round-2/screen_period",
                "budget_confusion/round-1/budget_promise",
                "rude_discount/round-2/regular_price_return"]
    assert all(findings[key]["status"] == "fail" for key in expected)
    assert findings["rude_discount/round-3/regular_price_return"]["status"] == "pass"
    assert findings["learning/ready_video_two_constraints"]["status"] == "manual_review"
    assert findings["learning/conditional_exception_with_fallback"]["status"] == "manual_review"


def test_learning_empty_general_fact_is_failure_but_personal_abstention_is_success():
    for case in E.load_suite("regression")["learning"]:
        status, _ = E.check_learning(case, {"candidates": []})
        assert status == ("pass" if "no_general_facts" in case["id"] else "fail")
        assert E.check_learning(case, None)[0] == "manual_review"


def test_negation_in_question_does_not_count_as_preserved_negation():
    case = E.load_suite("regression")["learning"][0]
    actual = {"candidates": [{"question": "Мы не производим ролик?", "answer": "Ролик предоставляет рекламодатель."}]}
    assert E.check_learning(case, actual)[0] == "manual_review"
    assert E.check_learning(case, {"candidates": [{"answer": ""}]})[0] == "fail"


def test_matching_case_id_with_a_different_question_is_not_comparable():
    suite = E.load_suite("regression")
    report = {"turns": [{"case_id": "budget_confusion", "question": "Другой вопрос", **turn("Да, уложимся.")} ]}
    findings = E.evaluate(report, suite)
    row = next(r for r in findings if r["id"] == "budget_confusion/round-1/budget_promise")
    assert row["status"] == "manual_review"


def test_comparison_uses_stable_check_ids_not_answer_fraction():
    before = [E.finding("one", "fail", ""), E.finding("two", "pass", "")]
    after = [E.finding("two", "fail", "ошибка"), E.finding("one", "pass", "исправлено"),
             E.finding("three", "manual_review", "нет ответа")]
    rows = E.compare(before, after)
    assert [r["change"] for r in rows] == ["regressed_narrow_check", "fixed_narrow_check", "changed"]
    assert rows[-1]["before"] == "not_run"


def test_three_rounds_share_history_budget_and_kb(tmp_path, kb, isolated_settings, mini_suite, monkeypatch):
    real = isolated_settings.run_batch
    visits = []

    async def spy(folder, inputs, **kwargs):
        result = await real(folder, inputs, **kwargs)
        visits.append((folder, kwargs["resume"], kwargs["learning"], kwargs["max_calls"], len(result["turns"]),
                       len(result["calls"])))
        return result

    monkeypatch.setattr(isolated_settings, "run_batch", spy)
    original = E.tree_fingerprint(kb)
    out = tmp_path / "eval"
    result = asyncio.run(E.run_evaluation(out, kb=kb, baseline=tmp_path / "absent.json", max_calls=32))
    assert result["execution_status"] == "complete", result
    assert [v[1] for v in visits] == [False, True, True, True]
    assert [v[2] for v in visits] == [False, False, False, True]
    assert {v[0] for v in visits} == {out / "run"}
    assert {v[3] for v in visits} == {32}
    assert [v[4] for v in visits] == [1, 2, 3, 3]
    assert visits[1][5] > visits[0][5] > 0
    raw = E.read_json(out / "run/report.json")
    assert "ДЕПО" in raw["turns"][1]["state"]["venue"]
    assert not any("ВЕЕР" in t for t in raw["turns"][1]["evidence"] if t.startswith("Прайс:"))
    with sqlite3.connect(out / "run/app.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert E.tree_fingerprint(kb) == original == result["metadata"]["copied_kb_fingerprint"]
    assert result["metadata"]["suite_fingerprint"] == E.digest(mini_suite)
    assert len(result["metadata"]["input_sets"]) == 4
    assert (out / "comparison.md").is_file() and (out / "evaluation.json").is_file()
    for item in result["metadata"]["input_sets"]:
        assert E.digest(E.read_json(out / item["path"])) == item["fingerprint"]


def test_exhausted_budget_never_resets_between_rounds_or_learning(tmp_path, kb, isolated_settings, mini_suite):
    out = tmp_path / "bounded"
    result = asyncio.run(E.run_evaluation(out, kb=kb, baseline=tmp_path / "absent.json", max_calls=1))
    assert result["calls"] == 1
    assert result["blocked_calls"] > 0
    assert result["execution_status"] == "incomplete_or_invalid"
    report = E.read_json(out / "run/report.json")
    assert len(report["turns"]) == 3
    assert len(report["learning"]) == 1
    assert len(report["calls"]) == 1


def test_cli_defaults_offline_without_provider_or_network(tmp_path, kb, isolated_settings, mini_suite, capsys):
    out = tmp_path / "cli"
    code = E.main(["--out", str(out), "--kb", str(kb), "--baseline", str(tmp_path / "absent.json")])
    assert code in {0, 1}
    result = E.read_json(out / "evaluation.json")
    assert result["mode"] == "offline_replay"
    assert result["execution_status"] == "complete"
    assert E.read_json(out / "run/report.json")["live_model"] is False
    assert "offline_replay" in capsys.readouterr().out


@pytest.mark.parametrize("suite_name,expected_turns,expected_learning", [("regression", 24, 6), ("heldout", 11, 0)])
def test_full_fixed_suites_run_offline_on_mini_kb(tmp_path, kb, isolated_settings, suite_name,
                                               expected_turns, expected_learning):
    out = tmp_path / suite_name
    result = asyncio.run(E.run_evaluation(out, kb=kb, suite_name=suite_name))
    assert result["execution_status"] == "complete", result
    assert result["mode"] == "offline_replay" and result["calls"] <= 48
    report = E.read_json(out / "run/report.json")
    assert len(report["turns"]) == expected_turns
    assert len(report["learning"]) == expected_learning
    assert report["live_model"] is False and report["kb_unchanged"]
    assert result["comparison"]["status"] == "manual_review_missing_cases_or_mode_or_kb_mismatch"


def test_live_flag_is_explicit_and_forwarded_for_all_batches(tmp_path, kb, isolated_settings, mini_suite, monkeypatch):
    real = isolated_settings.run_batch
    seen = []

    async def no_api_spy(folder, inputs, **kwargs):
        seen.append(kwargs["allow_api"])
        # Проверяем контракт передачи флага без настоящего API.
        kwargs["allow_api"] = False
        return await real(folder, inputs, **kwargs)

    monkeypatch.setattr(isolated_settings, "run_batch", no_api_spy)
    E.main(["--out", str(tmp_path / "opt-in"), "--kb", str(kb), "--allow-api"])
    assert seen == [True, True, True, True]


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_invalid_budget_creates_no_output(tmp_path, limit):
    out = tmp_path / "invalid"
    with pytest.raises(ValueError, match="max-calls"):
        asyncio.run(E.run_evaluation(out, max_calls=limit))
    assert not out.exists()


def test_nonempty_out_is_preserved(tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    marker = out / "report.json"
    marker.write_text("не трогать", encoding="utf-8")
    with pytest.raises(ValueError, match="пустой"):
        asyncio.run(E.run_evaluation(out))
    assert marker.read_text(encoding="utf-8") == "не трогать"


def test_missing_synthetic_kb_never_reads_published_working_kb(tmp_path):
    out = tmp_path / "no-kb"
    with pytest.raises(ValueError, match="учебной KB"):
        asyncio.run(E.run_evaluation(out, kb=tmp_path / "missing"))
    assert not out.exists()


def test_non_synthetic_baseline_is_rejected_before_run(tmp_path, kb):
    baseline = tmp_path / "not-synthetic.json"
    E.write_json(baseline, {"synthetic": False, "turns": []})
    with pytest.raises(ValueError, match="синтетическим"):
        asyncio.run(E.run_evaluation(tmp_path / "eval", kb=kb, baseline=baseline))


def test_fingerprints_change_on_content_not_mtime(tmp_path):
    path = tmp_path / "kb"
    path.mkdir()
    file = path / "faq.md"
    file.write_text("первый", encoding="utf-8")
    first = E.tree_fingerprint(path)
    file.touch()
    assert E.tree_fingerprint(path) == first
    file.write_text("второй", encoding="utf-8")
    assert E.tree_fingerprint(path) != first


def test_symlinked_kb_cannot_pull_in_outside_data(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = tmp_path / "kb"
    folder.mkdir()
    (folder / "notes").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Символьные ссылки"):
        E.tree_fingerprint(folder)


def test_failed_batch_saves_partial_machine_and_text_results(tmp_path, kb, isolated_settings, mini_suite, monkeypatch):
    real = isolated_settings.run_batch
    attempts = 0

    async def fail_second(folder, inputs, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("sensitive provider details must not be copied")
        return await real(folder, inputs, **kwargs)

    monkeypatch.setattr(isolated_settings, "run_batch", fail_second)
    out = tmp_path / "failed"
    result = asyncio.run(E.run_evaluation(out, kb=kb))
    assert attempts == 2 and result["error"] == "RuntimeError"
    assert result["execution_status"] == "incomplete_or_invalid"
    assert len(E.read_json(out / "run/report.json")["turns"]) == 1
    assert "sensitive provider" not in (out / "evaluation.json").read_text()
    assert (out / "comparison.md").is_file()
    assert any(not r["observed"] for r in result["checks"])
