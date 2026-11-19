"""Потери логики, область факта и явная сверка исходников перед публикацией."""
import asyncio
import json
from dataclasses import replace

import pytest
from tests.admin_client import AdminClient as TestClient

from app.admin import create_app
from app.config import ROOT, Settings
from app.core.candidate_review import review_candidate
from app.llm.base import LLMProvider
from app.services import Services


VIDEO_SOURCE = ("Не, тут я криво объяснила раньше: ролик для медиа-куба в ВЕЕР Молле мы не делаем. "
                "Готовый ролик должен предоставить сам рекламодатель, картинки вместо него не подойдут.")
VIDEO_Q = "Что делать, если для медиа-куба в ВЕЕР Молле есть только картинки?"
VIDEO_LOSS = ("Ролик для медиа-куба в ВЕЕР Молле предоставляет рекламодатель, "
              "готовые картинки не принимаются.")
STORAGE_Q = "Можно ли оставить стойку на ночь в атриуме ДЕПО Молла?"
STORAGE_SOURCE = ("Обычно убирают. Оставить стойку на ночь в атриуме ДЕПО Молла можно только если "
                  "администрация письменно согласовала именно ночное хранение и стойка целиком стоит "
                  "внутри согласованного контура; иначе после каждого дня её надо убрать. "
                  "Согласование самой акции тут не считается, я про отдельное согласование хранения.")
STORAGE_LOSS = ("Оставить стойку на ночь возможно только при наличии письменного согласования администрации, "
                "специально оформленного для ночного хранения. Стоika должна находиться строго в пределах "
                "согласованного контура. После каждого дня использования стойку необходимо убирать. "
                "Согласование самой акции не включает автоматическое разрешение на ночное хранение.")
SCOPED_SOURCE = ("Нет, погодите, по трём местам отдельно: в атриуме ВЕЕР Молла подвес допускается только "
                 "к штатным точкам, к ограждению крепить нельзя. На медиа-кубе ВЕЕР Молла нельзя мелкий "
                 "бегущий текст, нужен неподвижный текст. А на медиа-кубе ДЕПО Молла запрещено мигание, "
                 "плавные переходы допустимы. Последнее именно про ДЕПО, не про все три.")
SCOPED_FACTS = [
    ("Какие ограничения в атриуме ВЕЕР Молла?",
     "Подвес допускается только к штатным точкам, к ограждению крепить нельзя."),
    ("Какие ограничения на медиа-кубе ВЕЕР Молла?",
     "Нельзя мелкий бегущий текст, нужен неподвижный текст."),
    ("Какие ограничения на медиа-кубе ДЕПО Молла?", "Запрещено мигание, плавные переходы допустимы."),
]
QUOTE_Q = ("На медиа-кубе ВЕЕР Молла ведь всегда включён звук, "
           "значит можно смысл рекламы оставить только в озвучке?")
QUOTE_SOURCE = ("«На медиа-кубе ВЕЕР Молла всегда включён звук» — нет, это неверно. "
                "На этом медиа-кубе рекламные ролики идут без звука. Всё важное должно быть понятно "
                "из изображения и текста, на озвучку рассчитывать нельзя.")


class LocalLLM(LLMProvider):
    name = "local-test"

    def __init__(self):
        self.facts = []
        self.calls = 0

    async def complete(self, system, messages):
        self.calls += 1
        from app.core.candidates import EXTRACT_PROMPT
        if system == EXTRACT_PROMPT:
            return json.dumps({"facts": self.facts}, ensure_ascii=False)
        return '{"answer":"", "confidence":1, "needs_manager":true}'


@pytest.fixture
def services(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "faq.md").write_text("", encoding="utf-8")
    (kb / "stop_topics.txt").write_text("", encoding="utf-8")
    (kb / "prices.csv").write_text(
        "format,placement,period,price_rub,notes\n"
        "Медиа-куб,ВЕЕР Молл — атриум,1 месяц,31500,\n"
        "Медиа-куб,ДЕПО Молл — атриум,1 месяц,25000,\n"
        "Инфостойка,Бутово Молл — вход,1 месяц,10000,\n", encoding="utf-8")
    return Services(replace(Settings.load(), db_path=tmp_path / "test.db", kb_dir=kb,
                            llm_provider="mock", admin_token="test", exam_files=""), llm=LocalLLM())


def add(services, question=VIDEO_Q, answer=VIDEO_LOSS, source=VIDEO_SOURCE):
    return services.db.add_candidate(connection_id="test", chat_id=1, dialog_id=None, source="observe",
                                     client_question=question, manager_text=source, question=question, answer=answer)


def client_for(services):
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", "test")
    return client


def test_production_negation_is_not_covered_by_another_negation(services):
    warnings = review_candidate(services.kb, VIDEO_Q, VIDEO_LOSS, VIDEO_SOURCE)
    assert any("изготовление" in w for w in warnings)
    repaired = "Мы не изготавливаем ролик. " + VIDEO_LOSS
    assert review_candidate(services.kb, VIDEO_Q, repaired, VIDEO_SOURCE) == []


def test_fallback_must_be_conditional_even_when_all_condition_words_survive(services):
    warnings = review_candidate(services.kb, STORAGE_Q, STORAGE_LOSS, STORAGE_SOURCE)
    assert any(w.startswith("Иначе:") for w in warnings)
    fixed = STORAGE_LOSS.replace("После каждого дня использования", "Иначе после каждого дня использования")
    assert review_candidate(services.kb, STORAGE_Q, fixed, STORAGE_SOURCE) == []


@pytest.mark.parametrize("answer", [
    STORAGE_SOURCE + " " + STORAGE_LOSS,
    STORAGE_LOSS + " " + STORAGE_SOURCE,
    STORAGE_SOURCE + " Ежедневно стойку надо убирать.",
    STORAGE_SOURCE.replace("её надо убрать.", "её надо убрать; после каждого дня стойку необходимо убирать."),
], ids=["source-then-loss", "loss-then-source", "separate-obligation", "semicolon-obligation"])
def test_linked_fallback_does_not_cover_an_unconditional_duplicate(services, answer):
    warnings = review_candidate(services.kb, STORAGE_Q, answer, STORAGE_SOURCE)
    assert any(w.startswith("Иначе:") and "повторено без условия" in w for w in warnings)


@pytest.mark.parametrize("answer", [
    STORAGE_SOURCE,
    STORAGE_SOURCE.replace("Обычно убирают.", "Как правило, стойку убирают."),
    STORAGE_LOSS.replace("После каждого дня использования", "Иначе после каждого дня использования"),
    STORAGE_LOSS.replace("После каждого дня использования", "В противном случае после каждого дня использования"),
    STORAGE_SOURCE + " Стойку надо убирать после каждого дня, если нет согласования.",
    STORAGE_SOURCE + " Ежедневно убирать стойку не требуется.",
], ids=["source", "default", "otherwise", "otherwise-paraphrase", "postposed-condition", "no-obligation"])
def test_default_cleanup_and_conditional_paraphrases_do_not_warn(services, answer):
    assert review_candidate(services.kb, STORAGE_Q, answer, STORAGE_SOURCE) == []


def test_service_blocks_appended_loss_despite_a_correct_fallback(services):
    cid = add(services, STORAGE_Q, STORAGE_SOURCE, source=STORAGE_SOURCE)
    before = services.kb_version
    ok, message = services.approve_candidates([cid], STORAGE_Q, STORAGE_SOURCE + " " + STORAGE_LOSS,
                                               source_reviewed=False)
    assert not ok and "повторено без условия" in message
    assert services.db.get_candidate(cid)["status"] == "new"
    assert services.db.get_candidate(cid)["answer"] == STORAGE_SOURCE
    assert services.kb_version == before and not (services.settings.kb_dir / "notes").exists()
    assert services.llm.calls == 0


@pytest.mark.parametrize("answer, marker", [
    ("Стойку можно оставлять на ночь.", "Условия:"),
    ("Стойку можно оставить только если согласовано хранение и она внутри контура; иначе убрать.", "письменное"),
    ("Стойку можно оставить только если есть письменное согласование хранения; иначе убрать.", "контур"),
    ("Стойку можно оставить только если письменно согласовано хранение или она целиком внутри контура; иначе убрать.",
     "ИЛИ"),
])
def test_conditions_and_joint_requirements_are_not_just_presence_of_negation(services, answer, marker):
    assert any(marker in w for w in review_candidate(services.kb, STORAGE_Q, answer, STORAGE_SOURCE))


@pytest.mark.parametrize("question, answer", SCOPED_FACTS)
def test_each_fact_needs_only_its_own_scope(services, question, answer):
    assert review_candidate(services.kb, question, answer, SCOPED_SOURCE) == []


@pytest.mark.parametrize("question", [
    "Какие ограничения на медиа-кубе ВЕЕР Молла?",
    "Какие ограничения у инфостойки Бутово Молла?",
    "Какие ограничения на всех медиа-кубах?",
])
def test_scope_transfer_and_generalization_warn(services, question):
    warnings = review_candidate(services.kb, question, SCOPED_FACTS[2][1], SCOPED_SOURCE)
    assert any("ТЦ" in w or "формат" in w.lower() for w in warnings)


def test_same_venue_different_format_is_not_same_rule(services):
    warnings = review_candidate(services.kb, "Мигает ли инфостойка в ДЕПО Молле?",
                                "Запрещено мигание, плавные переходы допустимы.", SCOPED_SOURCE)
    assert any("формат" in w.lower() for w in warnings)


@pytest.mark.parametrize("separator", ["; ", ", а "])
def test_independent_rules_in_one_sentence_keep_their_own_scopes(services, separator):
    source = ("На медиа-кубе ВЕЕР Молла ролики идут без звука" + separator +
              "на медиа-кубе ДЕПО Молла ролики идут со звуком.")
    assert review_candidate(services.kb, "Есть звук на медиа-кубе ДЕПО Молла?",
                            "Ролики идут со звуком.", source) == []
    assert review_candidate(services.kb, "Есть звук на медиа-кубе ВЕЕР Молла?",
                            "Ролики идут без звука.", source) == []


def test_missing_exception_and_allowed_alternative_need_review(services):
    lost_exception = STORAGE_SOURCE.split("Согласование самой акции")[0]
    warnings = review_candidate(services.kb, STORAGE_Q, lost_exception, STORAGE_SOURCE)
    assert any("исключение" in w for w in warnings)
    warnings = review_candidate(services.kb, SCOPED_FACTS[2][0], "Запрещено мигание.", SCOPED_SOURCE)
    assert any("Альтернатива" in w for w in warnings)


def test_source_condition_does_not_become_condition_just_from_question(services):
    question = "Можно оставить стойку в атриуме ДЕПО Молла, если есть письменное согласование и контур?"
    warnings = review_candidate(services.kb, question, "Да, стойку можно оставить на ночь.", STORAGE_SOURCE)
    assert any(w.startswith("Условия:") for w in warnings)


def test_wrong_production_claim_is_not_repaired_by_negating_images(services):
    warnings = review_candidate(services.kb, VIDEO_Q,
                                "Мы изготавливаем ролик, картинки не принимаются.", VIDEO_SOURCE)
    assert any("Отрицание (изготовление)" in w for w in warnings)


@pytest.mark.parametrize("left, right", [("«", "»"), ('"', '"'), ("'", "'"), ("“", "”")])
def test_unverified_client_quote_alone_is_not_confirmation(services, left, right):
    claim = "На медиа-кубе ВЕЕР Молла всегда включён звук"
    source = f"Клиент написал {left}{claim}{right}. Я ещё не проверяла, так ли это."
    warnings = review_candidate(services.kb, QUOTE_Q, claim + ".", source, QUOTE_Q)
    assert any(w.startswith("Цитата:") for w in warnings)


def test_rule_cannot_expand_to_additional_formats(services):
    warnings = review_candidate(services.kb, "Правила медиа-куба и инфостойки ДЕПО Молла?",
                                "Запрещено мигание, плавные переходы допустимы.", SCOPED_SOURCE)
    assert any(w.startswith("Формат/место:") for w in warnings)


def test_ui_keeps_similar_questions_of_different_scopes_in_separate_forms(services):
    for question, answer in SCOPED_FACTS:
        add(services, question, answer, source=SCOPED_SOURCE)
    response = client_for(services).get("/learn")
    # «Обучение» — очередь: один факт с формой, остальные — строками справа; группы не склеились в одну
    assert "1 из 3" in response.text and response.text.count('class="qitem"') == 2
    assert 'name="source_reviewed"' not in response.text


def test_false_premise_in_question_is_not_answer_and_quote_is_not_evidence(services):
    correct = ("На медиа-кубе ВЕЕР Молла рекламные ролики идут без звука. "
               "Все важные моменты должны быть понятны из изображения и текста.")
    assert review_candidate(services.kb, QUOTE_Q, correct, QUOTE_SOURCE, QUOTE_Q) == []
    wrong = "На медиа-кубе ВЕЕР Молла всегда включён звук."
    warnings = review_candidate(services.kb, QUOTE_Q, wrong, QUOTE_SOURCE, QUOTE_Q)
    assert any("Цитата:" in w for w in warnings)
    assert any("Отрицание (звук)" in w for w in warnings)


def test_collect_keeps_old_note_and_adds_local_warning(services, monkeypatch):
    monkeypatch.setattr("app.core.candidates.review_note", lambda *args: "Похожее уже есть в базе: ролик")
    services.privacy.set_allowed("test", 1, True)
    services.llm.facts = [{"question": VIDEO_Q, "answer": VIDEO_LOSS}]
    assert asyncio.run(services.collect_candidates(source="observe", connection_id="test", chat_id=1,
                                                   dialog_id=None, client_question=VIDEO_Q,
                                                   manager_text=VIDEO_SOURCE)) == 1
    row = services.db.candidates()[0]
    assert "Отрицание (изготовление)" in row["note"]
    assert "Похожее уже есть в базе:" in row["note"]
    assert row["manager_text"] == VIDEO_SOURCE
    assert row["status"] == "new" and services.llm.calls == 1
    assert not any(VIDEO_LOSS in c.text for c in services.kb.chunks)


def test_approve_rechecks_actual_edits_not_saved_note_or_saved_answer(services):
    cid = add(services, answer="Мы не изготавливаем ролик. " + VIDEO_LOSS)
    before = services.kb_version
    ok, message = services.approve_candidates([cid], VIDEO_Q, VIDEO_LOSS)
    assert not ok and "Сверил с исходным ответом" in message
    assert services.kb_version == before and services.db.get_candidate(cid)["status"] == "new"
    assert not (services.settings.kb_dir / "notes").exists()
    ok, _ = services.approve_candidates([cid], VIDEO_Q, "Мы не изготавливаем ролик. " + VIDEO_LOSS)
    assert ok and services.db.get_candidate(cid)["status"] == "approved"
    assert services.llm.calls == 0  # локальная проверка и публикация не используют модель


def test_human_review_allows_warning_but_never_unknown_price(services):
    cid = add(services)
    ok, message = services.approve_candidates([cid], VIDEO_Q, VIDEO_LOSS + " Цена 33 000 ₽.", source_reviewed=True)
    assert not ok and "нет в прайсе" in message
    assert services.db.get_candidate(cid)["status"] == "new"
    ok, _ = services.approve_candidates([cid], VIDEO_Q, VIDEO_LOSS, source_reviewed=True)
    assert ok and services.db.get_candidate(cid)["status"] == "approved"


@pytest.mark.parametrize("status", ["new", "rejected", "approved", "superseded"])
def test_invalid_selection_is_atomic_before_any_note_write(services, status):
    cid = add(services)
    if status != "new":
        services.db.decide_candidates([cid], status)
    before = services.kb_version
    for ids in ([], [999999], [cid, 999999], [0], [-1], [True]):
        ok, _ = services.approve_candidates(ids, VIDEO_Q, VIDEO_LOSS, source_reviewed=True)
        assert not ok
    if status != "new":
        assert not services.approve_candidates([cid], VIDEO_Q, VIDEO_LOSS, source_reviewed=True)[0]
    assert services.db.get_candidate(cid)["status"] == status
    assert services.kb_version == before and not (services.settings.kb_dir / "notes").exists()


def test_group_checks_every_source_and_rejects_mixed_status(services):
    first = add(services, source=VIDEO_LOSS)
    second = add(services)
    assert not services.approve_candidates([first, second], VIDEO_Q, VIDEO_LOSS)[0]
    services.db.decide_candidates([second], "rejected")
    assert not services.approve_candidates([first, second], VIDEO_Q, VIDEO_LOSS, source_reviewed=True)[0]
    assert services.db.get_candidate(first)["status"] == "new"


def test_edited_question_cannot_silently_move_a_rule_to_another_mall(services):
    cid = add(services, *SCOPED_FACTS[2], source=SCOPED_SOURCE)
    wrong_q = SCOPED_FACTS[2][0].replace("ДЕПО", "ВЕЕР")
    assert not services.approve_candidates([cid], wrong_q, SCOPED_FACTS[2][1])[0]


def test_route_block_preserves_edit_and_shows_source_and_review_checkbox(services):
    cid = add(services, answer="Мы не изготавливаем ролик. " + VIDEO_LOSS)
    client = client_for(services)
    assert 'name="source_reviewed"' not in client.get("/learn").text
    payload = {"ids": str(cid), "question": VIDEO_Q, "answer": VIDEO_LOSS}
    response = client.post("/candidates/approve", data=payload)
    assert response.status_code == 200 and "Нужна сверка" in response.text
    assert 'name="source_reviewed"' in response.text and "Сверил с исходным ответом" in response.text
    assert "<details open>" in response.text and VIDEO_SOURCE in response.text
    assert f'<textarea name="answer" rows="3">{VIDEO_LOSS}</textarea>' in response.text
    assert services.db.get_candidate(cid)["status"] == "new"
    assert "Факт утверждён" in client.post("/candidates/approve", data={**payload, "source_reviewed": "true"}).text
    assert services.db.get_candidate(cid)["status"] == "approved"


def test_ui_checks_all_group_sources_and_escapes_them(services):
    add(services, source=VIDEO_LOSS)
    add(services, source=VIDEO_SOURCE + ' <script>alert("test")</script>')
    response = client_for(services).get("/learn")
    assert 'name="source_reviewed"' in response.text and 'class="warn"' in response.text
    assert "&lt;script&gt;" in response.text and '<script>alert("test")</script>' not in response.text


def test_ui_does_not_duplicate_stored_warnings_or_show_them_after_repair(services):
    services.privacy.set_allowed("test", 1, True)
    services.llm.facts = [{"question": VIDEO_Q, "answer": VIDEO_LOSS}]
    asyncio.run(services.collect_candidates(source="observe", connection_id="test", chat_id=1,
                                           dialog_id=None, client_question=VIDEO_Q, manager_text=VIDEO_SOURCE))
    cid = services.db.candidates()[0]["id"]
    client = client_for(services)
    assert client.get("/learn").text.count("Отрицание (изготовление)") == 1
    # Ошибка ID заставляет вернуть форму, но текущий исправленный Q/A снова оценивается.
    from app.admin import candidates_html
    repaired = "Мы не изготавливаем ролик. " + VIDEO_LOSS
    html = candidates_html(services, ([cid], VIDEO_Q, repaired))
    assert "Отрицание (изготовление)" not in html and 'name="source_reviewed"' not in html


@pytest.mark.parametrize("raw", ["", "abc", "999999", "{id},abc", "{id},999999", "{id},"])
def test_route_rejects_entire_malformed_selection(services, raw):
    cid = add(services)
    response = client_for(services).post("/candidates/approve", data={
        "ids": raw.format(id=cid), "question": VIDEO_Q, "answer": VIDEO_LOSS, "source_reviewed": "true"})
    assert "Факт утверждён" not in response.text
    assert services.db.get_candidate(cid)["status"] == "new"
    assert not (services.settings.kb_dir / "notes").exists()


def test_saved_synthetic_outputs_match_independent_audit(services):
    folder = ROOT / "eval/simulation/runs/difficult-live-2026-10-23"
    if not (folder / "learning_for_review.json").exists():
        pytest.skip("Локальный синтетический прогон отсутствует; его регрессии покрыты выше.")
    learning = json.loads((folder / "learning_for_review.json").read_text())
    audit = json.loads((folder / "learning_audit.json").read_text())
    verdicts = {case["id"]: case["verdict"] for case in audit["cases"]}
    for case in learning["learning"]:
        for candidate in case["candidates"]:
            warnings = review_candidate(services.kb, candidate["question"], candidate["answer"],
                                        candidate["manager_text"], candidate["client_question"])
            assert bool(warnings) == (verdicts[case["id"]] != "ok"), (case["id"], warnings)
