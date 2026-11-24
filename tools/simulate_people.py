"""Синтетическая переписка агентов через обработчик наблюдения и настоящий RAG.

По умолчанию без сети. --allow-api включает настроенную модель только для извлечения
фактов и ответов бота. Telegram API всегда заглушён, рабочие SQLite/KB не открываются.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
from dataclasses import asdict, replace
from pathlib import Path

from aiogram import Bot
from aiogram.types import Update

from app import db as D
from app.channels.telegram_business import build_dispatcher
from app.config import ROOT, Settings
from app.core import candidates as C
from app.core.knowledge import PriceRow, prices_csv
from app.llm import make_provider
from app.llm.base import LLMProvider
from app.llm.mock import MockProvider
from app.services import Services

FIXTURES = ROOT / "eval/simulation"
CONNECTION, OWNER = "synthetic-people", 100
TOPICS = {"sound", "duration", "creator", "unknown"}


def load_people(folder: Path = FIXTURES) -> tuple[list[dict], dict]:
    clients = json.loads((folder / "clients.json").read_text(encoding="utf-8"))
    manager = json.loads((folder / "manager.json").read_text(encoding="utf-8"))
    if clients.get("synthetic") is not True or manager.get("synthetic") is not True:
        raise ValueError("Стенд принимает только явно синтетические сценарии")
    cases = clients["cases"]
    if len(cases) != 4 or {c["id"] for c in cases} != TOPICS or set(manager["replies"]) != TOPICS:
        raise ValueError("Нужны четыре разных сценария: sound, duration, creator, unknown")
    if {f["id"] for f in manager["facts"]} != TOPICS - {"unknown"}:
        raise ValueError("Эталон содержит только три общих факта")
    for case in cases:
        if not case["training"] or not case["probe"] or not manager["replies"][case["id"]]:
            raise ValueError("У каждого сценария нужны сообщения, ответ менеджера и новый вопрос")
    return cases, manager


def matches(topic: str, answer: str) -> bool:
    """Узкая проверка трёх заданных фактов; не заменяет оценку человеком."""
    text = " ".join(answer.casefold().replace("ё", "е").split())
    patterns = {
        "sound": r"без звука|звук.{0,20}(?:отсутств|не воспроизвод|не транслир)|не поддержива.{0,15}звук",
        "duration": r"(?:до|не более|максимум)\s*15\s*сек|"
                    r"максимальн\w*(?:\s+[а-яa-z-]+){0,15}\s*[:—-]?\s*15\s*сек",
        "creator": r"рекламодател\w*.{0,35}(?:предостав|готов|изготов)|"
                   r"(?:предостав|готов|изготов)\w*.{0,20}рекламодател|предоставляете вы|"
                   r"вы.{0,25}(?:предостав|готов|изготов)",
    }
    return bool(re.search(patterns[topic], text))


class ReplayLLM(LLMProvider):
    """Только проверка проводки без API: извлечение пар и первый фрагмент поиска."""
    name = "replay"

    async def complete(self, system: str, messages: list[dict]) -> str:
        if system == C.EXTRACT_PROMPT:
            question, answer = messages[-1]["content"].removeprefix("Вопрос клиента: ").split("\nОтвет менеджера: ", 1)
            return json.dumps({"facts": [asdict(f) for f in C.raw_facts(question, answer)]}, ensure_ascii=False)
        reply = json.loads(await MockProvider().complete(system, messages))
        reply["answer"] = reply["answer"].split("\n\n", 1)[0]
        return json.dumps(reply, ensure_ascii=False)


class LimitedLLM(LLMProvider):
    def __init__(self, inner: LLMProvider, limit: int):
        self.inner, self.limit = inner, limit
        self.name = inner.name
        self.records: list[dict] = []
        self.phase = ""
        self.blocked_calls = 0

    async def complete(self, system: str, messages: list[dict]) -> str:
        self.last_tokens = 0
        if len(self.records) >= self.limit:
            self.blocked_calls += 1
            raise RuntimeError("Исчерпан лимит симуляции")
        record = {"phase": self.phase, "tokens": 0, "error": ""}
        self.records.append(record)
        try:
            reply = await self.inner.complete(system, messages)
            record["tokens"] = self.last_tokens = self.inner.last_tokens
            return reply
        except Exception as exc:
            # Не сохраняем текст исключения: в нём могут оказаться адреса и заголовки запросов.
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            record["error"] = f"{type(exc).__name__}" + (f" HTTP {code}" if code else "")
            raise

    async def close(self) -> None:
        await self.inner.close()


async def run_simulation(folder: Path, *, allow_api: bool = False, max_calls: int = 24,
                         people_dir: Path = FIXTURES) -> dict:
    cases, manager = load_people(people_dir)
    folder = folder.resolve()
    if not 1 <= max_calls <= 50:
        raise ValueError("Лимит вызовов — от 1 до 50")
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Папка прогона должна быть пустой; существующие результаты не перезаписываем")
    folder.mkdir(parents=True, exist_ok=True)
    # Свой прайс без условий из живой базы. Цена вымышлена и в рабочее приложение не попадает.
    kb = folder / "kb"
    kb.mkdir()
    (kb / "prices.csv").write_text(prices_csv([
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 30000, "")
    ]), encoding="utf-8")
    (kb / "stop_topics.txt").write_text("бронирование\nбронь\n", encoding="utf-8")
    settings = replace(Settings.load(), telegram_bot_token="", db_path=folder / "app.db", kb_dir=kb,
                       exam_files="", default_mode="observe", admin_token="synthetic-local-only")
    if allow_api and settings.llm_provider == "mock":
        raise ValueError("--allow-api требует настроенного живого провайдера")
    llm = LimitedLLM(make_provider(settings) if allow_api else ReplayLLM(), max_calls)
    services = Services(settings, llm)
    bot = Bot("42:SYNTHETIC")
    outgoing: list[str] = []
    report = {"synthetic": True, "live_model": allow_api, "provider": llm.name,
              "model": settings.gigachat_model if settings.llm_provider == "gigachat" and allow_api else
                       settings.openai_model if allow_api else "replay",
              "roles": "Вопросы и ответы заранее подготовлены двумя отдельными агентами; сценарий фиксирован.",
              "limits": "Проверяет обработчики и ядро без доставки Telegram. Это не метрики реального пилота.",
              "transcript": [], "probes": [], "reviews": [], "checks": {},
              "candidates_snapshot": "before_review"}

    async def no_telegram(_bot, method, timeout=None):
        outgoing.append(type(method).__name__)
        raise RuntimeError("Telegram API в симуляции отключён")

    bot.session.make_request = no_telegram
    dispatcher = build_dispatcher(services)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    update_id = 0

    async def message(text: str, chat: int, mid: int, *, owner: bool = False):
        nonlocal update_id
        update_id += 1
        payload = {"update_id": update_id, "business_message": {
            "message_id": mid, "date": now + update_id, "business_connection_id": CONNECTION,
            "text": text, "chat": {"id": chat, "type": "private", "first_name": "Синтетический клиент"},
            "from": {"id": OWNER if owner else chat, "is_bot": False, "first_name": "Симуляция"}}}
        await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))
        report["transcript"].append({"chat_id": chat, "message_id": mid,
                                     "role": "manager" if owner else "client", "text": text})

    async def probe(phase: str):
        llm.phase = phase
        for i, case in enumerate(cases):
            reply = await services.answer(case["probe"])
            fact = next((f for f in manager["facts"] if f["id"] == case["id"]), None)
            correct = (not reply.is_answer if case["id"] == "unknown" else
                       reply.is_answer and matches(case["id"], reply.text) and not any(
                           t.casefold() in reply.text.casefold() for t in fact["forbidden_terms"]))
            report["probes"].append({"phase": phase, "id": case["id"], "question": case["probe"],
                                      **asdict(reply), "correct_by_pattern": correct, "version": services.kb_version})
            services.db.log_dialog(connection_id=CONNECTION, chat_id=900 + i, question=case["probe"],
                                   answer=reply.text, decision=reply.decision, reason=reply.reason,
                                   confidence=reply.confidence, status=D.SANDBOX, sources=reply.sources,
                                   latency_ms=reply.latency_ms, mode="sandbox", kb_version=services.kb_version)

    try:
        services.db.upsert_connection(CONNECTION, OWNER, OWNER, True, False, "observe")
        initial_version = services.kb_version
        await probe("before")
        llm.phase = "observe"
        for i, case in enumerate(cases):
            mid, chat = 0, 500 + i
            services.privacy.set_allowed(CONNECTION, chat, True)
            for text in case["training"]:
                mid += 1
                await message(text, chat, mid)
            for text in manager["replies"][case["id"]]:
                mid += 1
                await message(text, chat, mid, owner=True)
        for i, pair in enumerate(manager["noise"]):
            services.privacy.set_allowed(CONNECTION, 600 + i, True)
            await message(pair["client"], 600 + i, 1)
            await message(pair["manager"], 600 + i, 2, owner=True)
        checks = report["checks"]
        checks["candidates_not_published_automatically"] = services.kb_version == initial_version \
            and not services.draft_changed()
        pending = [dict(r) for r in services.db.candidates()]
        report["candidates"] = pending
        general_chats = {500 + i for i, case in enumerate(cases) if case["id"] != "unknown"}
        checks["no_private_or_greeting_candidates"] = all(r["chat_id"] in general_chats for r in pending)
        collected, approved = set(), set()
        for row in reversed(pending):
            case = cases[row["chat_id"] - 500] if 500 <= row["chat_id"] < 504 else None
            topic = case["id"] if case else "noise"
            fact = next((f for f in manager["facts"] if f["id"] == topic), None)
            valid = bool(fact and matches(topic, row["answer"]) and not any(
                t.casefold() in row["answer"].casefold() for t in fact["forbidden_terms"]))
            if valid:
                collected.add(topic)
                ok, detail = services.approve_candidates([row["id"]], row["question"], row["answer"], scope="ВЕЕР Молл")
                if ok:
                    approved.add(topic)
            else:
                services.db.decide_candidates([row["id"]], "rejected")
                ok, detail = False, "Сценарная проверка отклонила: нет общего подтверждённого факта"
            report["reviews"].append({"candidate_id": row["id"], "topic": topic, "approved": ok, "detail": detail})
        checks["all_three_facts_collected"] = collected == TOPICS - {"unknown"}
        checks["all_three_facts_approved"] = approved == TOPICS - {"unknown"} and not services.draft_changed()
        report["candidates_after_review"] = [dict(services.db.get_candidate(r["id"])) for r in pending]
        await probe("after")
        after = [r for r in report["probes"] if r["phase"] == "after"]
        checks["known_answers_after"] = all(r["correct_by_pattern"] for r in after if r["id"] != "unknown")
        checks["unknown_still_escalated"] = all(r["correct_by_pattern"] for r in after if r["id"] == "unknown")
        checks["telegram_calls_zero"] = not outgoing
        checks["no_model_errors"] = not llm.blocked_calls and not any(r["error"] for r in llm.records)
        checks["within_call_limit"] = len(llm.records) <= max_calls
        report.update(calls=llm.records, total_tokens=sum(r["tokens"] for r in llm.records),
                      call_limit=max_calls, blocked_calls=llm.blocked_calls, outgoing_telegram=outgoing,
                      observation=services.db.observation_stats(), active_version=services.kb_version,
                      passed=all(checks.values()))
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "Отчёт.md").write_text(markdown_report(report), encoding="utf-8")
        return report
    finally:
        await bot.session.close()
        await llm.close()
        services.db.conn.close()


def markdown_report(report: dict) -> str:
    lines = ["# Симуляция переписки", "", "Синтетические сообщения и учебные условия, не результаты пилота.",
             report["roles"], report["limits"], "",
             f"Модель: {report['model']}. Вызовов: {len(report['calls'])}/{report['call_limit']}; "
             f"токенов по API: {report['total_tokens']}. Telegram-вызовов: {len(report['outgoing_telegram'])}.",
             "Расход относится только к модели приложения; агенты подготовки и разбора расходуют лимит Codex отдельно.",
             "", "В offline используются заглушки; их ответы не измеряют качество нейросети.",
             "Утверждение в этом прогоне выполняет сценарная проверка по заданным фактам, не живой менеджер.",
             "Даже пустая учебная база содержит правила приложения: для цифровых экранов прайс уже сообщает, "
             "что ролик готовит рекламодатель. Поэтому часть ответов возможна до обучения.", "", "## Проверки", ""]
    if report.get("grading_note"):
        lines[2:2] = [report["grading_note"], ""]
    if report.get("independent_review"):
        lines[2:2] = ["**Независимый разбор:** " + report["independent_review"]["detail"], ""]
    names = {"candidates_not_published_automatically": "Кандидаты не попали в базу до утверждения",
             "no_private_or_greeting_candidates": "Личная бронь и приветствия не стали кандидатами",
             "all_three_facts_collected": "Извлечены все три общих факта",
             "all_three_facts_approved": "Все три факта прошли утверждение и публикацию",
             "known_answers_after": "Новые формулировки получили ответы по фактам (проверка шаблонами)",
             "unknown_still_escalated": "Индивидуальная бронь осталась у менеджера",
             "telegram_calls_zero": "Ни одного обращения к Telegram",
             "no_model_errors": "Нет ошибок модели или исчерпания бюджета",
             "within_call_limit": "Лимит вызовов соблюдён"}
    lines += [f"- {'✓' if ok else '✗'} {names[key]}" for key, ok in report["checks"].items()]
    lines += ["", "## Переписка", ""]
    for turn in report["transcript"]:
        role = "Менеджер" if turn["role"] == "manager" else "Клиент"
        lines += [f"**Чат {turn['chat_id']}, {role}:** {turn['text']}", ""]
    lines += ["## Извлечённые кандидаты", "", "Это снимок перед утверждением, поэтому исходный статус — new.", ""]
    for row in report["candidates"]:
        lines += [f"**#{row['id']}: {row['question']}**", row["answer"], ""]
    lines += ["## Решения сценарной проверки", ""]
    for review in report["reviews"]:
        lines += [f"- #{review['candidate_id']} ({review['topic']}): {review['detail']}"]
    for row in report.get("candidates_after_review", []):
        lines += [f"- Итоговый статус кандидата #{row['id']}: {row['status']}."]
    lines += ["", "## Вопросы новых клиентов: до и после", ""]
    for row in report["probes"]:
        phase = "До" if row["phase"] == "before" else "После"
        lines += [f"### {phase}: {row['id']}, версия {row['version']}", "", row["question"], "",
                  f"Решение: {row['decision']}; причина: {row['reason'] or '—'}.", "", row["text"], "",
                  f"Проверка по шаблонам: {'совпало' if row['correct_by_pattern'] else 'не совпало'}. "
                  "Несовпадение требует разбора ответа, это не автоматический диагноз ошибки факта.", ""]
    for call in report["calls"]:
        if call["error"]:
            lines += [f"Ошибка модели ({call['phase']}): {call['error']}", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--allow-api", action="store_true", help="живой провайдер из .env, расходует квоту")
    parser.add_argument("--max-calls", type=int, default=24, help="предел вызовов complete, включая корректирующие")
    parser.add_argument("--out", type=Path, help="новая пустая папка SQLite, базы и отчёта")
    args = parser.parse_args()
    folder = args.out or FIXTURES / "runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result = asyncio.run(run_simulation(folder, allow_api=args.allow_api, max_calls=args.max_calls))
    print(f"Отчёт: {folder.resolve() / 'Отчёт.md'}")
    print(f"Проверки: {sum(result['checks'].values())}/{len(result['checks'])}; "
          f"вызовов: {len(result['calls'])}; токенов: {result['total_tokens']}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
