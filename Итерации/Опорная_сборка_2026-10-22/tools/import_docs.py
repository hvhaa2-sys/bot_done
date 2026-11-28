"""Тексты из документов компании (КП, прайсы, презентации, регламенты) — для пополнения базы знаний.

Запуск:
    python -m tools.import_docs ~/Downloads/Инцентр
    python -m tools.import_docs КП_ВЕЕР.pdf прайс.xlsx --out data/import/docs

Что получится в data/import/docs/:
- <имя файла>.txt — текст документа (таблицы — строками через «|», слайды и листы подписаны);
- index.csv       — список файлов: сколько текста, страниц/листов, есть ли что-то, что нельзя брать в базу.

Дальше факты из текстов переносятся руками (или вручную) в data/kb: цены — в prices.csv,
остальное — заметками в notes/. Сам текст боту не отдаётся: в документах бывают пароли, внутренние
данные, устаревшие цены и черновики.

Нужно: pdftotext (brew install poppler) для PDF; python-docx, openpyxl, python-pptx для Office —
pip install python-docx openpyxl python-pptx. Чего нет — те файлы пропускаются с подсказкой.
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.config import ROOT

# Что нельзя переносить в базу знаний бота: пароли, доступы, личные данные (152-ФЗ)
_SENSITIVE = {
    "пароли/доступы": re.compile(r"парол|password|логин|login\b|токен|api[ _-]?key", re.IGNORECASE),
    "телефоны": re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"),
    "почты": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "паспорт/ИНН/счета": re.compile(r"паспорт|\bИНН\b|р/с|расч[её]тн\w* сч[её]т|\bБИК\b", re.IGNORECASE),
}


def from_pdf(path: Path) -> tuple[str, str]:
    if not shutil.which("pdftotext"):
        raise RuntimeError("нет pdftotext: brew install poppler")
    text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=120).stdout
    pages = max(text.count("\f"), 1)
    return text, f"{pages} стр." + (" мало текста — похоже на картинки, смотреть глазами" if len(text.strip()) < 150 * pages else "")


def from_xlsx(path: Path) -> tuple[str, str]:
    import openpyxl
    book = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for sheet in book.worksheets:
        out.append(f"=== Лист: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out), f"{len(book.worksheets)} листов"


def from_docx(path: Path) -> tuple[str, str]:
    import docx
    document = docx.Document(path)
    out = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        out.append("=== Таблица")
        out += [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
    return "\n".join(out), f"{len(document.tables)} таблиц"


def from_pptx(path: Path) -> tuple[str, str]:
    import pptx
    deck = pptx.Presentation(path)
    out = []
    for i, slide in enumerate(deck.slides, 1):
        out.append(f"=== Слайд {i}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                out += [" | ".join(c.text.strip() for c in row.cells) for row in shape.table.rows]
    text = "\n".join(out)
    return text, f"{len(deck.slides)} слайдов" + (" мало текста — слайды-картинки" if len(text) < 80 * len(deck.slides) else "")


def from_txt(path: Path) -> tuple[str, str]:
    return path.read_text(encoding="utf-8", errors="replace"), ""


READERS = {".pdf": from_pdf, ".xlsx": from_xlsx, ".xlsm": from_xlsx, ".docx": from_docx, ".pptx": from_pptx,
           ".txt": from_txt, ".md": from_txt, ".csv": from_txt}


def sensitive(text: str) -> list[str]:
    return [name for name, rx in _SENSITIVE.items() if rx.search(text)]


def collect(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        files += sorted(f for f in p.rglob("*") if f.suffix.lower() in READERS) if p.is_dir() else [p]
    return files


def run_import(paths: list[Path], out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in collect(paths):
        reader = READERS.get(path.suffix.lower())
        if not reader:
            continue
        try:
            text, info = reader(path)
        except ImportError as exc:
            text, info = "", f"нет библиотеки {exc.name}: pip install python-docx openpyxl python-pptx"
        except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять весь импорт
            text, info = "", f"ошибка: {type(exc).__name__}: {exc}"[:200]
        dest = out / f"{path.stem}{path.suffix}.txt"
        dest.write_text(text, encoding="utf-8")
        rows.append({"file": str(path), "chars": len(text), "info": info, "sensitive": ", ".join(sensitive(text))})
    with (out / "index.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "chars", "info", "sensitive"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", type=Path, help="файлы или папки с документами")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "import" / "docs")
    args = parser.parse_args(argv)
    rows = run_import(args.paths, args.out)
    print(f"Документов: {len(rows)}, текста: {sum(r['chars'] for r in rows)} символов → {args.out}")
    for r in rows:
        found = r["sensitive"].split(", ") if r["sensitive"] else []
        notes = [r["info"],
                 "НЕ ПЕРЕНОСИТЬ В БАЗУ: пароли/доступы" if "пароли/доступы" in found else "",
                 "проверить перед переносом: " + ", ".join(f for f in found if f != "пароли/доступы")
                 if [f for f in found if f != "пароли/доступы"] else ""]
        if note := "; ".join(filter(None, notes)):
            print(f"  {Path(r['file']).name}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
