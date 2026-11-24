"""Версии базы знаний: черновик, проверка перед публикацией, противоречия, откат."""
from dataclasses import replace

from tests.admin_client import AdminClient as TestClient

from app.admin import create_app
from app.config import Settings
from app.core import versions as V
from app.services import Services

FAQ = """## Какие требования к макету?
Макет в PDF, 150 dpi.

## Где завозить материалы в ДЕПО?
Через служебный вход с улицы Космонавтов, с 7 до 9 утра.
"""
PRICES = ("format,placement,period,price_rub,notes\n"
          "Цифровой экран,\"ВЕЕР Молл, Екатеринбург — атриум\",1 месяц,31500,с НДС 5%\n"
          "Баннер,\"ДЕПО, Нижний Тагил — переход\",1 месяц,25000,печать 15 400 ₽\n")
EXAM = ("question,expected,must_contain\n"
        "Сколько стоит экран в атриуме ВЕЕР?,answer,31 500\n"
        "Как завезти материалы в ДЕПО?,answer,служебный\n"
        "Дадите скидку?,escalate,\n")


def make_services(tmp_path, publish: str = "auto") -> Services:
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "faq.md").write_text(FAQ, encoding="utf-8")
    (kb_dir / "prices.csv").write_text(PRICES, encoding="utf-8")
    (kb_dir / "stop_topics.txt").write_text("скидк\n", encoding="utf-8")
    (tmp_path / "exam.csv").write_text(EXAM, encoding="utf-8")
    settings = replace(Settings.load(), db_path=tmp_path / "data" / "app.db", kb_dir=kb_dir, llm_provider="mock",
                       exam_files=str(tmp_path / "exam.csv"), admin_token="secret")
    services = Services(settings)
    if publish != "auto":
        services.db.set_settings({"kb_publish": publish})
        services.overrides = {"kb_publish": publish}
    return services


def edit_faq(services: Services, text: str) -> None:
    (services.settings.kb_dir / "faq.md").write_text(text, encoding="utf-8")


def test_first_start_publishes_snapshot_and_bot_reads_it_not_the_draft(tmp_path):
    services = make_services(tmp_path)
    assert services.kb_version == 1
    snapshot = tmp_path / "data" / "kb_versions" / "0001"
    assert (snapshot / "faq.md").read_text(encoding="utf-8") == FAQ
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n")
    services.reload_kb()
    assert services.draft_changed()
    assert not any("2000 мест" in c.text for c in services.kb.chunks), "черновик без публикации бот не видит"


def test_auto_mode_publishes_checked_change_and_journal_keeps_version(tmp_path):
    services = make_services(tmp_path)
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n")
    msg = services.kb_changed("Новый вопрос про парковку")
    assert "Опубликована версия базы 2" in msg and services.kb_version == 2
    assert any("2000 мест" in c.text for c in services.kb.chunks)
    assert not services.draft_changed()
    row = services.db.get_kb_version(2)
    report = V.Report.from_json(row["report"])
    assert report.changes == ["Новый раздел: «Есть ли парковка?» (faq.md)"]
    assert report.passed == report.total == 5  # 2 контрольных вопроса + 3 раздела по своему заголовку
    dialog_id = services.db.log_dialog(connection_id="c", chat_id=1, question="q", answer="a", decision="answer",
                                       reason="", confidence=1, status="sent_auto", sources=[], latency_ms=1,
                                       mode="auto", kb_version=services.kb_version)
    assert services.db.get_dialog(dialog_id)["kb_version"] == 2
    assert services.kb_changed("Ещё раз") == "Изменений в базе нет."


def test_manual_mode_waits_for_publish(tmp_path):
    services = make_services(tmp_path, publish="manual")
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n")
    assert "Сохранено в черновик" in services.kb_changed("Парковка")
    assert services.kb_version == 1
    version, report = services.publish("Публикация из админки")
    assert version == 2 and not report.blocking


def test_lost_answer_blocks_publish_until_forced(tmp_path):
    services = make_services(tmp_path)
    edit_faq(services, "## Какие требования к макету?\nМакет в PDF, 150 dpi.\n")  # убрали раздел про завоз
    msg = services.kb_changed("Чистка FAQ")
    assert "не опубликовано" in msg and "Как завезти материалы в ДЕПО?" in msg
    assert services.kb_version == 1, "бот продолжает отвечать по прошлой версии"
    report = services.check_draft()
    assert report.regressions == ["Как завезти материалы в ДЕПО? — нет «служебный»"]
    assert report.changes == ["Удалён раздел: «Где завозить материалы в ДЕПО?» (faq.md)"]
    version, _ = services.publish("Решение менеджера", force=True)
    assert version == 2


def test_price_change_is_expected_but_stale_note_is_a_conflict(tmp_path):
    services = make_services(tmp_path)
    prices = services.settings.kb_dir / "prices.csv"
    prices.write_text(PRICES.replace("31500", "33000"), encoding="utf-8")
    report = services.check_draft()
    assert report.changes == ["Цена: Цифровой экран, ВЕЕР Молл, Екатеринбург — атриум, 1 месяц — 31 500 ₽ → 33 000 ₽"]
    assert report.expected and not report.regressions and not report.blocking
    # Заметка со старой ценой — то самое «документ с другим значением»: без решения менеджера не публикуем
    note = services.settings.kb_dir / "notes" / "ВЕЕР.md"
    note.parent.mkdir()
    note.write_text("---\nтц: ВЕЕР Молл\n---\n## Сколько стоит экран в атриуме?\nЭкран в атриуме — 31 500 ₽ в месяц.\n",
                    encoding="utf-8")
    report = services.check_draft()
    assert report.blocking and "сумма 31 500 ₽, которой нет в прайсе" in report.conflicts[0]


def test_conflicting_answers_to_one_question(tmp_path):
    services = make_services(tmp_path)
    notes = services.settings.kb_dir / "notes"
    notes.mkdir()
    (notes / "ВЕЕР требования.md").write_text(
        "---\nтц: ВЕЕР Молл\n---\n## Какой длины ролик для экранов ВЕЕР?\nДо 15 секунд, без звука.\n", encoding="utf-8")
    assert "Опубликована" in services.kb_changed("Заметка про ролики")
    (notes / "Из переписки").mkdir()
    (notes / "Из переписки" / "ВЕЕР Молл.md").write_text(
        "---\nтц: ВЕЕР Молл\n---\n## Какая длина ролика на экранах ВЕЕР?\nРолик до 20 секунд.\n", encoding="utf-8")
    report = services.check_draft()
    assert report.blocking and report.conflicts[0].startswith("Разные ответы на один вопрос")
    # Не спор: тот же ответ с подробностями
    (notes / "Из переписки" / "ВЕЕР Молл.md").write_text(
        "---\nтц: ВЕЕР Молл\n---\n## Какая длина ролика на экранах ВЕЕР?\nДо 15 секунд, mp4.\n", encoding="utf-8")
    assert not services.check_draft().conflicts


def test_rollback_restores_bot_and_draft_and_keeps_unpublished_edits(tmp_path):
    services = make_services(tmp_path)
    obsidian = services.settings.kb_dir / "notes" / ".obsidian"
    obsidian.mkdir(parents=True)
    (obsidian / "app.json").write_text("{}", encoding="utf-8")
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n")
    services.kb_changed("Парковка")
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n\n## Есть ли Wi-Fi?\nДа.\n")  # не опубликовано
    services.db.set_settings({"kb_publish": "manual"})
    services.overrides = {"kb_publish": "manual"}
    assert services.rollback(1) == "Бот снова отвечает по версии 1."
    assert services.kb_version == 1 and not services.draft_changed()
    assert not any("2000 мест" in c.text for c in services.kb.chunks)
    assert (services.settings.kb_dir / "faq.md").read_text(encoding="utf-8") == FAQ
    assert (obsidian / "app.json").exists(), "настройки Obsidian в папке заметок не трогаем"
    kept = list((tmp_path / "data" / "kb_versions").glob("черновик-до-отката-*/faq.md"))
    assert kept and "Wi-Fi" in kept[0].read_text(encoding="utf-8")
    assert services.rollback(2) == "Бот снова отвечает по версии 2."
    assert services.rollback(99) == "Такой версии нет."


def test_files_changed_while_bot_was_off_are_published_on_start(tmp_path):
    services = make_services(tmp_path)
    edit_faq(services, FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n")
    again = Services(services.base_settings)
    assert again.kb_version == 2 and any("2000 мест" in c.text for c in again.kb.chunks)


def test_new_answer_to_same_question_supersedes_old_one(tmp_path):
    services = make_services(tmp_path)
    ids = [services.db.add_candidate(connection_id="c", chat_id=1, dialog_id=None, source="chat", client_question="q",
                                     manager_text="m", question="Можно ли ролик со звуком?", answer=a)
           for a in ("Нет, экраны без звука.", "Нет, только без звука, звук выключен во всём ТЦ.")]
    assert services.approve_candidates([ids[0]], "Можно ли ролик со звуком?", "Нет, экраны без звука.", scope="business")[0]
    assert services.approve_candidates([ids[1]], "можно ли ролик со звуком", "Нет, звук выключен во всём ТЦ.",
                                       scope="business")[0]
    assert services.db.get_candidate(ids[0])["status"] == "superseded"
    assert services.db.get_candidate(ids[1])["status"] == "approved"
    assert services.kb_version == 3


def test_admin_shows_pending_changes_publishes_and_rolls_back(tmp_path):
    services = make_services(tmp_path, publish="manual")
    client = TestClient(create_app(services))
    page = client.get("/kb?token=secret").text
    assert "Версия базы: 1" in page and "Изменения, которые бот ещё не видит" not in page
    r = client.post("/kb/save", data={"name": "faq.md", "text": FAQ + "\n## Есть ли парковка?\nДа, на 2000 мест.\n"})
    assert "Сохранено в черновик" in r.text and "Изменения, которые бот ещё не видит" in r.text
    assert "Новый раздел: «Есть ли парковка?»" in r.text and "Опубликовать</button>" in r.text
    assert "есть неопубликованные изменения" in client.get("/").text
    r = client.post("/kb/publish")
    assert "Опубликована версия 2" in r.text and services.kb_version == 2
    r = client.post("/kb/rollback/1")
    assert "Бот снова отвечает по версии 1" in r.text and services.kb_version == 1
    # Поломка: кнопка честно называется «Опубликовать всё равно»
    client.post("/kb/save", data={"name": "faq.md", "text": "## Какие требования к макету?\nPDF.\n"})
    page = client.get("/kb").text
    assert "Поиск перестал находить ответ" in page and "Опубликовать всё равно" in page
    assert "Не опубликовано" in client.post("/kb/publish").text
    assert "Опубликована версия 3" in client.post("/kb/publish", data={"force": "1"}).text


def test_publish_mode_setting_is_validated(tmp_path):
    services = make_services(tmp_path)
    client = TestClient(create_app(services))
    client.cookies.set("admin_token", "secret")
    r = client.post("/settings", data={"kb_publish": "sometimes"})
    assert "Не сохранено" in r.text
    client.post("/settings", data={"kb_publish": "manual"})
    assert services.current()["kb_publish"] == "manual"


def test_conflict_links_to_the_note_and_deleting_it_clears_the_draft(tmp_path):
    services = make_services(tmp_path)
    notes = services.settings.kb_dir / "notes"
    (notes / "ВЕЕР").mkdir(parents=True)
    (notes / "ВЕЕР" / "Ролики.md").write_text(
        "---\nтц: ВЕЕР Молл\n---\n## Какой длины ролик для экранов ВЕЕР?\nДо 15 секунд.\n", encoding="utf-8")
    services.kb_changed("Ролики")
    (notes / "ВЕЕР" / "Старое КП.md").write_text(
        "---\nтц: ВЕЕР Молл\n---\n## Какой длины ролик для экранов ВЕЕР?\nДо 20 секунд.\n", encoding="utf-8")
    assert "не опубликовано" in services.kb_changed("Старое КП")
    client = TestClient(create_app(services))
    page = client.get("/kb?token=secret").text
    link = "/kb/note?path=%D0%92%D0%95%D0%95%D0%A0%2F%D0%A1%D1%82%D0%B0%D1%80%D0%BE%D0%B5+%D0%9A%D0%9F.md"
    assert f'href="{link}"' in page
    assert "До 20 секунд" in client.get(link).text
    r = client.post("/kb/note", data={"path": "ВЕЕР/Старое КП.md", "text": "старый текст", "delete": "1"})
    assert "Изменений в базе нет" in r.text and not services.draft_changed() and services.kb_version == 2
