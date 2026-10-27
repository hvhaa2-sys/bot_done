"""Импорт документов компании: пароли и доступы не должны оставаться на диске в data/import."""
import csv

from tools.import_docs import WITHHELD, run_import, without_secrets


def test_section_with_password_is_withheld_whole():
    text = ("=== Лист: Площадки\nВЕЕР | экран у входа | 31 500\n"
            "=== Лист: Доступы\nПлощадка | Логин | Пароль\nВЕЕР | veer-admin | Qwe!2026\n"
            "=== Лист: Сроки\nМакет за 3 дня\n")
    saved, removed = without_secrets(text)
    assert removed == 1 and "Qwe!2026" not in saved and "veer-admin" not in saved
    assert "=== Лист: Доступы\n" + WITHHELD in saved, "видно, что лист был и почему его нет"
    assert "ВЕЕР | экран у входа | 31 500" in saved and "Макет за 3 дня" in saved


def test_pdf_pages_and_plain_text():
    saved, removed = without_secrets("Цены на 2026 год\fВход в систему: login manager, password 12345\fСроки")
    assert removed == 1 and "12345" not in saved and saved.count("\f") == 2 and saved.startswith("Цены")
    assert without_secrets("Экран 31 500 ₽ в месяц") == ("Экран 31 500 ₽ в месяц", 0)


def test_import_keeps_prices_but_not_passwords(tmp_path):
    import openpyxl
    book = openpyxl.Workbook()
    book.active.title = "VM"
    book.active.append(["Экран у входа", 31500])
    access = book.create_sheet("Доступы")
    access.append(["Площадка", "Логин", "Пароль"])
    access.append(["ВЕЕР", "veer-admin", "Qwe!2026"])
    source = tmp_path / "Инструкция.xlsx"
    book.save(source)

    rows = run_import([source], tmp_path / "out")
    saved = (tmp_path / "out" / "Инструкция.xlsx.txt").read_text(encoding="utf-8")
    assert "Qwe!2026" not in saved and "veer-admin" not in saved and "Экран у входа | 31500" in saved
    assert "пароли/доступы" in rows[0]["sensitive"] and "не сохранено: 1" in rows[0]["info"]
    with (tmp_path / "out" / "index.csv").open(encoding="utf-8") as f:
        assert "пароли/доступы" in next(csv.DictReader(f))["sensitive"]
