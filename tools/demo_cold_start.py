"""Демо холодного старта: новый бизнес подключает бота, а в базе знаний только прайс.

Сценарий для защиты, один полный цикл:
1. В базе только прайс — FAQ и заметок нет, бот публикует его как версию 1.
2. Клиенты спрашивают то, чего в прайсе нет, — бот передаёт вопросы менеджеру.
3. Менеджер отвечает клиентам как обычно, бот предлагает из его ответов факты для базы.
4. Менеджер утверждает факты, черновик базы проходит проверку и публикуется новой версией.
5. Новые клиенты спрашивают другими словами — бот отвечает сам.
6. Приходит документ с другим значением — проверка показывает противоречие, публикации нет,
   бот продолжает отвечать по проверенной версии.

Ответы менеджера взяты из настоящих заметок базы ВЕЕР Молл: видно, что базу можно было вырастить
из переписки, а не писать заранее.

Запуск:
    python -m tools.demo_cold_start                    # модель из .env (GigaChat)
    python -m tools.demo_cold_start --offline          # без сети: заглушка вместо модели, для проверки сценария
    python -m tools.demo_cold_start --keep demo-data   # оставить папку с базой демо
    python -m tools.demo_cold_start --admin 8010       # после сценария открыть админку демо (без Telegram)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db as D  # noqa: E402
from app.admin import plural  # noqa: E402
from app.channels.flow import human_reason  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core import candidates as C  # noqa: E402
from app.llm.base import LLMProvider  # noqa: E402
from app.llm.mock import MockProvider  # noqa: E402
from app.services import Services  # noqa: E402


@dataclass
class Dialog:
    client: str
    manager: str
    offline_facts: list[tuple[str, str]]  # что извлекла бы модель — только для --offline


DIALOGS = [
    Dialog("Здравствуйте! Есть ли звук на экранах в ВЕЕР Молл?",
           "Добрый день! Нет, экраны в ВЕЕР Молл работают без звука. Если нужен звук, есть аудиоролик на радио ТРЦ: "
           "MP3 до 25 секунд, 48 выходов в день. Пришлю вам примеры роликов.",
           [("Есть ли звук на экранах ВЕЕР Молл?",
             "Нет, экраны в ВЕЕР Молл работают без звука. Если нужен звук, есть аудиоролик на радио ТРЦ: "
             "MP3 до 25 секунд, 48 выходов в день.")]),
    Dialog("Подскажите, а ролик для экрана вы сами делаете?",
           "Готовый ролик по техническим требованиям экрана предоставляет рекламодатель, сами ролики мы не делаем.",
           [("Кто делает ролик для экранов?",
             "Готовый ролик по техническим требованиям экрана предоставляет рекламодатель, сами ролики мы не делаем.")]),
]
FOLLOW_UPS = ["а видео со звуком можно крутить в веере?", "ролик под экран вы монтируете или нам делать?"]
# Документ с другими цифрами: старое КП, где у радио другие условия
CONFLICT_NOTE = ("ВЕЕР/Радио ТРЦ.md", "---\nтц: ВЕЕР Молл\nисточник: КП 2024\n---\n# Радио ТРЦ\n\n"
                 "## Есть ли звук на экранах ВЕЕР Молл?\nНет. Звук только на радио ТРЦ: MP3 до 30 секунд, 60 выходов в день.\n")
PRICE_FORMATS = ("Цифровой экран", "Цифровой медиа-куб", "Видео на навигационных терминалах")


class OfflineLLM(LLMProvider):
    """Заглушка без сети: факты берёт из сценария, отвечает только по заметкам (прайса для этих вопросов мало,
    как и у настоящей модели), иначе зовёт менеджера."""
    name = "offline"

    def __init__(self):
        self.mock = MockProvider()

    async def complete(self, system: str, messages: list[dict]) -> str:
        if system == C.EXTRACT_PROMPT:
            text = messages[-1]["content"]
            facts = [{"question": q, "answer": a} for d in DIALOGS if d.manager in text for q, a in d.offline_facts]
            return json.dumps({"facts": facts}, ensure_ascii=False)
        if "Ответ:" not in system:
            return json.dumps({"answer": "", "confidence": 0.2, "needs_manager": True}, ensure_ascii=False)
        reply = json.loads(await self.mock.complete(system, messages))
        reply["answer"] = reply["answer"].split("\n\n", 1)[0]  # заглушка берёт блок контекста целиком
        return json.dumps(reply, ensure_ascii=False)


def demo_prices() -> str:
    """Прайс нового бизнеса: экраны ВЕЕР Молл из настоящего прайса."""
    with (ROOT / "data" / "kb" / "prices.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["format"] in PRICE_FORMATS and "ВЕЕР" in r["placement"]]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["format", "placement", "period", "price_rub", "notes"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def make_workspace(folder: Path) -> Path:
    kb = folder / "kb"
    kb.mkdir(parents=True)
    (kb / "prices.csv").write_text(demo_prices(), encoding="utf-8")
    shutil.copy(ROOT / "data" / "kb" / "stop_topics.txt", kb / "stop_topics.txt")
    return kb


def say(text: str) -> None:
    print(text, flush=True)  # при записи демо вывод идёт в файл или трубу — без буфера строки видны сразу


class Story:
    def __init__(self, out=say):
        self.out = out
        self.step = 0

    def head(self, title: str) -> None:
        self.step += 1
        self.out(f"\n━━ {self.step}. {title}")

    def line(self, text: str = "") -> None:
        self.out(text)


async def ask(services: Services, story: Story, question: str) -> bool:
    """Вопрос клиента: ответ бота или передача менеджеру, с записью в журнал, как в Telegram."""
    reply = await services.answer(question)
    status = D.SENT_AUTO if reply.is_answer else D.ESCALATED
    services.db.log_dialog(connection_id="demo", chat_id=1, question=question, answer=reply.text,
                           decision=reply.decision, reason=reply.reason, confidence=reply.confidence, status=status,
                           sources=reply.sources, latency_ms=reply.latency_ms, mode="auto",
                           kb_version=services.kb_version)
    story.line(f"Клиент: {question}")
    if reply.is_answer:
        story.line(f"Бот отвечает сам (версия базы {services.kb_version}): {reply.text}")
    else:
        story.line(f"Бот передаёт менеджеру: {human_reason(reply.reason)}")
    return reply.is_answer


async def run_demo(folder: Path, offline: bool, out=say) -> dict:
    kb_dir = make_workspace(folder)
    settings = replace(Settings.load(), db_path=folder / "app.db", kb_dir=kb_dir, exam_files="",
                       **({"llm_provider": "mock"} if offline else {}))
    services = Services(settings, llm=OfflineLLM() if offline else None)
    story = Story(out)
    try:
        story.head("Новый бизнес подключил бота")
        story.line(f"В базе только прайс ({plural(len(services.kb.prices), 'позиция', 'позиции', 'позиций')}), "
                   "FAQ и заметок нет. "
                   f"Опубликована версия базы {services.kb_version}.")

        story.head("Клиенты спрашивают то, чего нет в прайсе")
        for d in DIALOGS:
            await ask(services, story, d.client)
            story.line(f"Менеджер отвечает клиенту сам: {d.manager}")
            added = await services.collect_candidates(source="chat", connection_id="demo", chat_id=1, dialog_id=None,
                                                      client_question=d.client, manager_text=d.manager)
            story.line(f"→ бот предложил фактов для базы: {added}\n")

        story.head("Менеджер утверждает факты (админка → «Обучение» или кнопка ✓ в вечерней сводке)")
        for row in reversed(services.db.candidates()):
            story.line(f"«{row['question']}» → {row['answer']}")
            if row["note"]:
                story.line(f"  подсказка: {row['note']}")
            scope = "business" if row["manager_text"] == DIALOGS[1].manager else "ВЕЕР Молл"
            ok, msg = services.approve_candidates([row["id"]], row["question"], row["answer"], scope=scope)
            story.line(f"  {'✓' if ok else '✗'} {msg}")

        story.head("Новые клиенты спрашивают другими словами")
        answered = [await ask(services, story, q) for q in FOLLOW_UPS]

        story.head("Пришёл документ с другими цифрами")
        path, text = CONFLICT_NOTE
        note = kb_dir / "notes" / path
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(text, encoding="utf-8")
        story.line(f"Загружена заметка «{path}» из старого КП: радио — до 30 секунд, 60 выходов в день.")
        report = services.check_draft()
        for conflict in report.conflicts:
            story.line(f"Проверка: {conflict}")
        story.line(services.kb_changed(f"Заметка {path}"))

        story.head("Итог")
        questions = len(DIALOGS) + len(FOLLOW_UPS)
        facts = sum(w["facts"] for w in services.db.growth())
        story.line(f"Вопросов клиентов: {questions}, бот ответил сам: {sum(answered)} — все после утверждения фактов. "
                   f"Утверждено фактов: {facts}. Версий базы: {len(services.db.kb_versions())}, бот отвечает по версии "
                   f"{services.kb_version}.")
        return {"answered": sum(answered), "facts": facts, "version": services.kb_version,
                "conflicts": report.conflicts, "services": services}
    except BaseException:
        await services.llm.close()
        raise


async def run(args: argparse.Namespace, folder: Path) -> None:
    result = await run_demo(folder, args.offline)
    services = result["services"]
    try:
        if args.admin:
            import uvicorn

            from app.admin import create_app  # noqa: PLC0415
            services.settings = replace(services.settings, admin_token=secrets.token_urlsafe(12))
            say(f"\nАдминка демо: http://127.0.0.1:{args.admin}/kb?token={services.settings.admin_token}"
                "\nОстановить — Ctrl+C.")
            config = uvicorn.Config(create_app(services), host="127.0.0.1", port=args.admin, log_level="warning")
            await uvicorn.Server(config).serve()
    finally:
        await services.llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="без сети: заглушка вместо модели")
    parser.add_argument("--keep", type=Path, help="папка для базы демо (по умолчанию — временная, удаляется)")
    parser.add_argument("--admin", type=int, metavar="PORT", help="после сценария открыть админку демо на этом порту")
    args = parser.parse_args()
    if args.keep and args.keep.exists() and any(args.keep.iterdir()):
        parser.error(f"папка {args.keep} не пустая — демо начинается с чистой базы")
    folder = args.keep or Path(tempfile.mkdtemp(prefix="demo-cold-start-"))
    try:
        asyncio.run(run(args, folder))
    finally:
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
