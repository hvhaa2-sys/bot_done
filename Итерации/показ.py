"""Пять изолированных репетиций. По умолчанию без сети, ключей и изменения рабочего проекта."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
# Все пять репетиций используют тот же набор файлов, что и в октябре.
_demo_dir = tempfile.TemporaryDirectory(prefix="sales-demo-october-")
SNAPSHOT = Path(_demo_dir.name)
tree = "5a7376505d6589fca058d549c7ffe97466908ed2:Итерации/Опорная_сборка_2026-10-22"
try:
    archive = subprocess.check_output(["git", "archive", "--format=tar", tree], cwd=HERE.parent)
except subprocess.CalledProcessError as error:
    raise SystemExit("Для репетиции нужна локальная история Git с коммитом 5a73765 (22.10.2026).") from error
with tarfile.open(fileobj=io.BytesIO(archive)) as source:
    source.extractall(SNAPSHOT, filter="data")
sys.dont_write_bytecode = True
sys.path.insert(0, str(SNAPSHOT))

from app.config import Settings  # noqa: E402
from app.core.knowledge import KnowledgeBase  # noqa: E402
from app.llm.mock import MockProvider  # noqa: E402
from app.services import Services  # noqa: E402
from tools.demo_cold_start import run_demo  # noqa: E402
from tools.deploy import create_backup, restore_backup  # noqa: E402

PRICES = ('format,placement,period,price_rub,notes\n'
          'Цифровой экран,"ВЕЕР Молл, Екатеринбург — атриум",1 месяц,31500,с НДС 5%\n'
          'Баннер,"ДЕПО, Нижний Тагил — переход",1 месяц,25000,печать 15 400 ₽\n')
FAQ = '## Какой макет баннера нужен для ДЕПО?\nМакет баннера в PDF, 150 dpi.\n'


def make_services(folder: Path, llm=None):
    kb = folder / "kb"
    kb.mkdir(parents=True)
    (kb / "prices.csv").write_text(PRICES, encoding="utf-8")
    (kb / "faq.md").write_text(FAQ, encoding="utf-8")
    (kb / "stop_topics.txt").write_text("скидк\nброн\nдоговор\n", encoding="utf-8")
    settings = replace(Settings.load(), db_path=folder / "app.db", kb_dir=kb, llm_provider="mock",
                       telegram_bot_token="", gigachat_auth_key="", openai_api_key="", exam_files="")
    return Services(settings, llm=llm or MockProvider())


async def first(folder):
    s = make_services(folder)
    try:
        scenarios = [("Сколько стоит цифровой экран в атриуме ВЕЕР Молл?", "answer"),
                     ("Какой макет баннера нужен для ДЕПО?", "answer"),
                     ("Можно скидку на размещение?", "escalate"),
                     ("Какова несущая способность перекрытия в новом павильоне?", "escalate")]
        for question, expected in scenarios:
            reply = await s.answer(question)
            print(f"\nКлиент: {question}\nРешение: {reply.decision} ({reply.reason})\nОтвет: {reply.text}")
            print("Источники: " + "; ".join(reply.sources))
            assert reply.decision == expected, f"ожидали {expected}, получили {reply.decision}"
            if question.startswith("Сколько"):
                assert "31 500" in reply.text and "ВЕЕР" in reply.text and reply.sources
        return {"cases": 4, "state": "passed"}
    finally:
        await s.llm.close()
        s.db.conn.close()


async def second(folder):
    s = make_services(folder)
    question = "На цифровых экранах ВЕЕР Молл есть звук?"
    answer = "На цифровых экранах ВЕЕР Молл звук не воспроизводится."
    try:
        added = await s.collect_candidates(source="chat", connection_id="meeting-demo", chat_id=1, dialog_id=None,
                                            client_question=question, manager_text=answer)
        assert added == 1
        candidate = s.db.candidates()[0]
        assert not any(answer in c.text for c in s.kb.chunks)
        print("1. Сохранён кандидат с исходным вопросом и ответом менеджера.")
        print("2. До утверждения в рабочей базе этого факта нет.")
        ok, msg = s.approve_candidates([candidate["id"]], question, answer)
        assert ok and any(answer in c.text for c in s.kb.chunks)
        print("3. Менеджер утвердил факт: " + msg)
        print("4. Новый факт найден: " + answer)
        hits = s.kb.retrieve("А на экранах ВЕЕР Молл звук воспроизводится?")
        assert any(answer in hit.chunk.text for hit in hits)
        return {"candidates": added, "approved": 1, "state": "passed"}
    finally:
        await s.llm.close()
        s.db.conn.close()


async def third(folder):
    result = await run_demo(folder, offline=True)
    s = result["services"]
    try:
        assert result["facts"] == 2 and result["answered"] == 2 and result["conflicts"]
        old = s.kb_version
        print("\nДополнительно проверяем откат: " + s.rollback(1))
        assert s.kb_version == 1 and len(s.kb.chunks) < len(KnowledgeBase.load(s.version_dir(old)).chunks)
        return {"facts": 2, "answers_after_approval": 2, "conflict_blocked": True, "rollback": 1, "state": "passed"}
    finally:
        await s.llm.close()
        s.db.conn.close()


def fourth(folder):
    print("Запускаем только BM25. Сеть отключена, embeddings/RRF не измерены.", flush=True)
    out = folder / "bm25"
    subprocess.run([sys.executable, "-m", "eval.retrieval_eval", "--methods", "bm25", "--out", str(out)],
                   cwd=SNAPSHOT, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    row = next(r for r in report["summary"] if r["dataset"] == "ALL")
    print(f"Размечено {report['judged_queries']} из {report['questions']}. "
          f"Recall@8={row['recall_at_k']:.4f}, MRR={row['mrr']:.4f}")
    print("Предварительная разметка разработчика. Для вывода нужна независимая проверка менеджера.")
    return {"state": "bm25-passed-comparison-pending", "judged": report["judged_queries"],
            "recall_at_8": row["recall_at_k"], "mrr": row["mrr"], "api_requests": report["api_requests"]}


def fifth(folder):
    data = folder / "data"
    (data / "kb").mkdir(parents=True)
    (data / "kb_versions/0001").mkdir(parents=True)
    (data / "kb/fact.md").write_text("подтверждённый факт", encoding="utf-8")
    (data / "kb_versions/0001/fact.md").write_text("снимок версии 1", encoding="utf-8")
    with closing(sqlite3.connect(data / "app.db")) as db:
        db.execute("CREATE TABLE demo(value TEXT)")
        db.execute("INSERT INTO demo VALUES ('original')")
        db.commit()
    backup = create_backup(data, folder / "backup.tar.gz")
    (data / "kb/fact.md").write_text("ошибочная правка", encoding="utf-8")
    (data / "after-snapshot.txt").write_text("лишний файл", encoding="utf-8")
    with closing(sqlite3.connect(data / "app.db")) as db:
        db.execute("UPDATE demo SET value='changed'")
        db.commit()
    restore_backup(data, folder / "backup.tar.gz", yes=True)
    assert (data / "kb/fact.md").read_text(encoding="utf-8") == "подтверждённый факт"
    assert (data / "kb_versions/0001/fact.md").read_text(encoding="utf-8") == "снимок версии 1"
    assert not (data / "after-snapshot.txt").exists()
    with closing(sqlite3.connect(data / "app.db")) as db:
        assert db.execute("SELECT value FROM demo").fetchone()[0] == "original"
    print("Восстановлены SQLite, знания и версия. Более поздний файл удалён.")
    print("Это репетиция файлового восстановления. Независимый Docker smoke выполняется отдельно.")
    return {"state": "passed", "backup": backup, "docker_checked_in_this_run": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    args = parser.parse_args()
    print(f"Показ {args.stage}. Одна опорная сборка от 22.10.2026. Учебные данные, mock, внешних запросов нет.", flush=True)
    with tempfile.TemporaryDirectory(prefix="sales-meeting-") as tmp:
        folder = Path(tmp)
        if args.stage <= 3:
            result = asyncio.run({1: first, 2: second, 3: third}[args.stage](folder))
        else:
            result = {4: fourth, 5: fifth}[args.stage](folder)
        print("\nРЕЗУЛЬТАТ: " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
