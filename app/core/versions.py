"""Версии базы знаний: черновик → проверка → публикация, откат.

Менеджер правит базу (прайс, FAQ, заметки, утверждённые факты из переписки) — это черновик в data/kb.
Бот отвечает по опубликованной версии — неизменяемому снимку в data/kb_versions/<номер>. Перед
публикацией черновик сдаёт быстрый экзамен без модели (секунды и бесплатно):

- контрольные вопросы: для каждого поиск должен по-прежнему находить ожидаемые факты (цены, условия);
  вопрос, который раньше находил ответ, а теперь нет, — регрессия;
- каждый раздел FAQ и заметок должен находиться по своему заголовку: новый факт не должен заглушать старый;
- противоречия: две цены на одно и то же, сумма в заметке, которой нет в прайсе, похожие вопросы
  с разными числами в ответах. Считаются только новые — те, которых не было в опубликованной версии.

Регрессия или новое противоречие останавливают публикацию: бот продолжает отвечать по прошлой версии,
менеджер видит, что не так, и решает сам. Если вопрос перестал находить сумму, потому что менеджер
поменял цену в прайсе, это не регрессия, а ожидаемое изменение.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .assistant import search_query
from .facts import contains, norm
from .knowledge import LEARNED_NOTES, UNSCOPED_NOTE, KnowledgeBase, format_rub, parse_note, parse_prices, parse_stop_topics
from .price_guard import extract_amounts, extract_numbers
from .text import tokenize

KB_FILES = ("faq.md", "prices.csv", "stop_topics.txt")
NOTES = "notes"
SHOWN = 8  # сколько строк каждого списка показывать менеджеру
SAME_QUESTION = 0.8  # доля общих слов в вопросах, чтобы считать их одним вопросом


# --- файлы версии ---
def kb_files(kb_dir: Path) -> list[Path]:
    """Файлы базы относительно kb_dir: три основных и все заметки .md (папка .obsidian и картинки — не база)."""
    files = [Path(name) for name in KB_FILES if (kb_dir / name).is_file()]
    notes = kb_dir / NOTES
    if notes.is_dir():
        files += sorted(p.relative_to(kb_dir) for p in notes.rglob("*.md")
                        if not any(part.startswith(".") for part in p.relative_to(notes).parts))
    return files


def fingerprint(kb_dir: Path) -> str:
    digest = hashlib.sha256()
    for rel in kb_files(kb_dir):
        digest.update(str(rel).encode())
        digest.update((kb_dir / rel).read_bytes())
    return digest.hexdigest()


def copy_kb(src: Path, dest: Path) -> None:
    for rel in kb_files(src):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest / rel)


def replace_kb(src: Path, dest: Path) -> None:
    """Вернуть в черновик содержимое версии. Чужие файлы в папке заметок (.obsidian, картинки) не трогаем."""
    for rel in kb_files(dest):
        (dest / rel).unlink()
    copy_kb(src, dest)


# --- что изменилось ---
@dataclass
class Section:
    file: str
    title: str
    answer: str
    venue: frozenset[str]


def sections(kb_dir: Path) -> list[Section]:
    found = []
    for rel in kb_files(kb_dir):
        if rel.suffix != ".md":
            continue
        for chunk in parse_note((kb_dir / rel).read_text(encoding="utf-8"), rel.stem):
            found.append(Section(str(rel), chunk.title, chunk.text.split("\nОтвет: ", 1)[-1], chunk.venue))
    return found


def _prices(kb_dir: Path):
    path = kb_dir / "prices.csv"
    return parse_prices(path.read_text(encoding="utf-8")) if path.exists() else []


def _stops(kb_dir: Path) -> list[str]:
    path = kb_dir / "stop_topics.txt"
    return parse_stop_topics(path.read_text(encoding="utf-8")) if path.exists() else []


def _place(row) -> str:
    return f"{row.format}, {row.placement}" + (f", {row.period}" if row.period else "")


def changes(old_dir: Path, new_dir: Path) -> list[str]:
    """Изменения человеческим языком: «Цена: экран, ВЕЕР — 30 000 → 35 000 ₽», «Новый раздел: …»."""
    lines = []
    old = {(r.format, r.placement, r.period): r for r in _prices(old_dir)}
    new = {(r.format, r.placement, r.period): r for r in _prices(new_dir)}
    for key, row in new.items():
        before = old.get(key)
        if before is None:
            lines.append(f"Новая позиция прайса: {_place(row)} — {format_rub(row.price_rub)}")
        elif before.price_rub != row.price_rub:
            lines.append(f"Цена: {_place(row)} — {format_rub(before.price_rub)} → {format_rub(row.price_rub)}")
        elif before.notes != row.notes:
            lines.append(f"Примечание в прайсе: {_place(row)}")
    lines += [f"Убрано из прайса: {_place(row)}" for key, row in old.items() if key not in new]
    old_s = {(s.file, s.title): s for s in sections(old_dir)}
    new_s = {(s.file, s.title): s for s in sections(new_dir)}
    for key, s in new_s.items():
        if key not in old_s:
            lines.append(f"Новый раздел: «{s.title}» ({s.file})")
        elif old_s[key].answer != s.answer:
            lines.append(f"Изменён ответ: «{s.title}» ({s.file})")
    lines += [f"Удалён раздел: «{s.title}» ({s.file})" for key, s in old_s.items() if key not in new_s]
    old_t, new_t = set(_stops(old_dir)), set(_stops(new_dir))
    lines += [f"Новая стоп-тема: {t}" for t in sorted(new_t - old_t)]
    lines += [f"Убрана стоп-тема: {t}" for t in sorted(old_t - new_t)]
    return lines


# --- противоречия ---
def conflicts(kb_dir: Path, kb: KnowledgeBase) -> list[str]:
    found = []
    by_place: dict[tuple[str, str, str], set[int]] = {}
    for row in kb.prices:
        by_place.setdefault((row.format.lower(), row.placement.lower(), row.period.lower()), set()).add(row.price_rub)
    for (fmt, place, period), prices in by_place.items():
        if len(prices) > 1:
            listed = " и ".join(format_rub(p) for p in sorted(prices))
            found.append(f"В прайсе две цены на одно и то же: {fmt}, {place} {period} — {listed}".replace("  ", " "))
    allowed = kb.allowed_amounts()
    quarantine = str(Path(NOTES, LEARNED_NOTES, UNSCOPED_NOTE))
    all_sections = [s for s in sections(kb_dir) if s.file != quarantine]
    for s in all_sections:
        if bad := [a for a in extract_amounts(s.answer) if a not in allowed]:
            found.append(f"«{s.title}» ({s.file}): сумма {', '.join(format_rub(a) for a in bad)}, "
                         "которой нет в прайсе — бот её не назовёт")
    # Один и тот же вопрос с разными числами в ответе: «ролик до 15 секунд» в заметке и «до 20 секунд»
    # в факте из переписки. Вопрос почти дословно тот же, ТЦ и числа в вопросе те же («экран № 1» и «№ 2» —
    # разные вопросы), а числа ответа не вкладываются одно в другое («30 000 и 40 000» против «40 000» — не спор)
    prepared = []
    for s in all_sections:
        venue = s.venue | kb._venues_in(s.title)
        prepared.append((s, set(tokenize(s.title)) - venue, venue, set(extract_numbers(s.title)),
                         set(extract_numbers(s.answer))))
    for i, (a, a_words, a_venue, a_asked, a_numbers) in enumerate(prepared):
        for b, b_words, b_venue, b_asked, b_numbers in prepared[i + 1:]:
            if not (a_numbers and b_numbers) or a_numbers <= b_numbers or b_numbers <= a_numbers:
                continue
            if a_venue != b_venue or a_asked != b_asked or not (a_words and b_words):
                continue
            if len(a_words & b_words) / len(a_words | b_words) >= SAME_QUESTION:
                found.append(f"Разные ответы на один вопрос: «{a.title}» ({a.file}) и «{b.title}» ({b.file})")
    return found


# --- контрольные вопросы ---
@dataclass
class Control:
    key: str
    question: str        # запрос к поиску (продолжение диалога — вместе с прошлым вопросом)
    must: list[str]      # что должно найтись в найденных фрагментах
    shown: str = ""      # как показать менеджеру


def control_questions(csv_files: list[Path], reviewed=()) -> list[Control]:
    """Вопросы экзамена с ожидаемым ответом и проверенная разметка ошибок из журнала."""
    controls = []
    for path in csv_files:
        with path.open(encoding="utf-8-sig") as f:
            previous: dict[str, str] = {}
            for n, row in enumerate(csv.DictReader(f)):
                question, dialog = row.get("question", ""), row.get("dialog", "")
                query = search_query(question, previous.get(dialog, "")) if dialog else question
                if dialog:
                    previous[dialog] = question
                must = [i.strip() for i in row.get("must_contain", "").split(";") if i.strip() and not i.startswith("!")]
                if row.get("expected") == "answer" and must:
                    controls.append(Control(f"{path.name}:{n}", query, must, question))
    for r in reviewed:
        must = [i.strip() for i in (r["must_contain"] or "").split(";") if i.strip() and not i.startswith("!")]
        if r["expected_decision"] == "answer" and must:
            controls.append(Control(f"журнал:{r['id']}", r["question"], must, r["question"]))
    return controls


def exam(kb: KnowledgeBase, controls: list[Control]) -> dict[str, list[str]]:
    """Ключ проверки → чего не нашлось (пустой список — нашлось всё). Плюс разделы базы по своему заголовку."""
    result = {}
    for c in controls:
        context = "\n".join(h.chunk.text for h in kb.retrieve(c.question))
        result[c.key] = [item for item in c.must if not contains(context, item)]
    for chunk in kb.faq:
        found = any(h.chunk.id == chunk.id for h in kb.retrieve(chunk.title))
        result.setdefault(f"раздел:{chunk.title}", [] if found else [chunk.title])
    return result


def passed(result: dict[str, list[str]]) -> int:
    return sum(not missing for missing in result.values())


# --- итог проверки ---
@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)      # новые, которых не было в опубликованной версии
    regressions: list[str] = field(default_factory=list)    # раньше находилось, теперь нет
    expected: list[str] = field(default_factory=list)       # не находится старая цена — её поменяли в прайсе
    unfound: list[str] = field(default_factory=list)        # новый раздел не находится по своему заголовку
    passed: int = 0
    total: int = 0
    passed_before: int = 0
    total_before: int = 0

    @property
    def blocking(self) -> bool:
        return bool(self.conflicts or self.regressions)

    def summary(self) -> str:
        delta = self.passed - self.passed_before
        text = f"контрольные вопросы {self.passed}/{self.total}" + (f" ({delta:+d})" if delta else "")
        if self.conflicts:
            text += f", новых противоречий: {len(self.conflicts)}"
        if self.regressions:
            text += f", перестало находиться: {len(self.regressions)}"
        return text

    def reason(self) -> str:
        """Почему не опубликовано — первая причина для короткого сообщения."""
        if self.conflicts:
            return self.conflicts[0]
        if self.regressions:
            return f"поиск перестал находить ответ на «{self.regressions[0].split(' — ', 1)[0]}»"
        return ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "Report":
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def check(old_dir: Path, old_kb: KnowledgeBase, new_dir: Path, new_kb: KnowledgeBase, controls: list[Control],
          old_result: dict[str, list[str]] | None = None) -> tuple[Report, dict[str, list[str]]]:
    """Сравнить черновик с опубликованной версией. Возвращает (итог, результаты экзамена черновика) —
    результаты пригодятся как «до» для следующей проверки."""
    before = old_result if old_result is not None else exam(old_kb, controls)
    after = exam(new_kb, controls)
    shown = {c.key: c.shown or c.question for c in controls}
    report = Report(changes=changes(old_dir, new_dir), passed=passed(after), total=len(after),
                    passed_before=passed(before), total_before=len(before))
    old_conflicts = set(conflicts(old_dir, old_kb))
    report.conflicts = [c for c in conflicts(new_dir, new_kb) if c not in old_conflicts]
    old_allowed, new_allowed = old_kb.allowed_amounts(), new_kb.allowed_amounts()
    for key, missing in after.items():
        if not missing:
            continue
        if key not in before:
            if key.startswith("раздел:"):
                report.unfound.append(key.split(":", 1)[1])
            continue
        if before[key]:
            continue  # не находилось и раньше
        if key.startswith("раздел:"):
            report.regressions.append(f"раздел «{key.split(':', 1)[1]}» больше не находится по своему вопросу")
            continue
        line = f"{shown.get(key, key)} — нет «{'; '.join(missing)}»"
        if all(_removed_amount(item, old_allowed, new_allowed) for item in missing):
            report.expected.append(line)
        else:
            report.regressions.append(line)
    return report, after


def _removed_amount(item: str, old: set[int], new: set[int]) -> bool:
    """Ожидаемая сумма была в прайсе и пропала из него — менеджер поменял цену, это не поломка поиска."""
    digits = norm(item).replace(" ", "")
    return digits.isdigit() and int(digits) in old and int(digits) not in new
