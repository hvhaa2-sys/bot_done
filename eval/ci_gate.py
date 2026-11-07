"""Экзамены без сети для CI: все наборы `eval/questions*.csv` на заглушке и экзамен обучения `--offline`.

Итог сравнивается с эталоном `eval/ci_baseline.json`. Проверка падает, если стало хуже хоть в одном наборе:
меньше верных решений, больше опасных ответов или ошибок фактов, меньше найденных фактов или больше личного
в кандидатах. Лучше стало — эталон обновляют осознанно, отдельной правкой:

    python -m eval.ci_gate             # сравнить с эталоном (так запускает CI)
    python -m eval.ci_gate --update    # записать текущий итог как эталон

Заглушка (`mock`) отдаёт первый найденный фрагмент базы, поэтому цифры ниже настоящей модели. Эталон ловит
другое: правка поиска, стоп-тем, проверок или базы, которая незаметно сломала уже работавшие ответы.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from eval import learning_eval  # noqa: E402
from eval.run_eval import evaluate, load_questions  # noqa: E402

BASELINE = ROOT / "eval" / "ci_baseline.json"
REAL_NOTES = "Из переписки"
# Что считаем ухудшением: (показатель, +1 — больше лучше, -1 — меньше лучше)
EXAM_KEYS = (("decision_accuracy", 1), ("unsafe_answers", -1), ("fact_errors", -1), ("content_hit", 1))
LEARNING_KEYS = (("fact_found", 1), ("personal_leaked", -1), ("invented_amounts", -1), ("after_ok", 1))


async def exams(settings: Settings) -> dict:
    out = {}
    for path in sorted((ROOT / "eval").glob("questions*.csv")):
        summary, _ = await evaluate("mock", settings, load_questions(path))
        out[path.name] = {key: round(summary[key], 4) for key, _ in EXAM_KEYS} | {"questions": summary["questions"]}
    return out


def learning() -> dict:
    folder = Path(tempfile.mkdtemp(prefix="ci-learning-"))
    try:
        _, summary = asyncio.run(learning_eval.run(folder, True, learning_eval.load()))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return {key: summary[key] for key, _ in LEARNING_KEYS} | {"pairs": summary["pairs"]}


def current() -> dict:
    """База — как в git: без фактов из настоящей переписки (`notes/Из переписки` в .gitignore), иначе локальный
    итог расходится с CI и `--update` записал бы эталон, который на GitHub не повторить."""
    folder = Path(tempfile.mkdtemp(prefix="ci-kb-"))
    try:
        kb = folder / "kb"
        shutil.copytree(ROOT / "data" / "kb", kb, ignore=shutil.ignore_patterns(REAL_NOTES, ".obsidian"))
        settings = replace(Settings.load(), llm_provider="mock", kb_dir=kb)
        return {"exams": asyncio.run(exams(settings)), "learning": learning()}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def worse(now: dict, base: dict) -> list[str]:
    problems = []
    for name, base_row in base["exams"].items():
        row = now["exams"].get(name)
        if row is None:
            problems.append(f"{name}: набора больше нет")
            continue
        problems += [f"{name}: {key} {base_row[key]} → {row[key]}" for key, sign in EXAM_KEYS
                     if (row[key] - base_row[key]) * sign < -1e-9]
    problems += [f"обучение: {key} {base['learning'][key]} → {now['learning'][key]}" for key, sign in LEARNING_KEYS
                 if (now["learning"][key] - base["learning"][key]) * sign < 0]
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="записать текущий итог как эталон")
    args = parser.parse_args()
    now = current()
    for name, row in now["exams"].items():
        print(f"{name}: {row['questions']} вопросов, решений {row['decision_accuracy']:.0%}, "
              f"опасных {row['unsafe_answers']}, ошибок фактов {row['fact_errors']}")
    learned = now["learning"]
    print(f"обучение: фактов {learned['fact_found']}, личного в кандидатах {learned['personal_leaked']}, "
          f"верно после обучения {learned['after_ok']} (из {learned['pairs']} пар)")
    if args.update:
        BASELINE.write_text(json.dumps(now, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Эталон записан: {BASELINE.relative_to(ROOT)}")
        return
    if not BASELINE.exists():
        sys.exit("Нет эталона eval/ci_baseline.json — запустите с --update")
    if problems := worse(now, json.loads(BASELINE.read_text(encoding="utf-8"))):
        print("\nСтало хуже, чем в эталоне:\n" + "\n".join(f"- {p}" for p in problems))
        sys.exit(1)
    added = sorted(set(now["exams"]) - set(json.loads(BASELINE.read_text(encoding="utf-8"))["exams"]))
    print("\nНе хуже эталона." + (f" Новые наборы без эталона: {', '.join(added)}." if added else ""))


if __name__ == "__main__":
    main()
