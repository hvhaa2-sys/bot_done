"""Сложные диалоги: продолжение по фактическому ответу и проверка извлечения ограничений.

Изолированная копия опубликованной KB, новая SQLite, без Telegram. По умолчанию без API.
--resume продолжает диалоги и общий бюджет, а не начинает их заново.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from aiogram.types import Message

from app.channels.flow import process_client_message
from app.channels.observation import observe
from app.config import ROOT, Settings
from app.core import versions
from app.core.conversation import Conversation
from app.llm import make_provider
from app.services import Services
from tools.simulate_people import LimitedLLM, ReplayLLM

FIXTURES = ROOT / "eval/simulation"


def load_cases(path: Path) -> tuple[list[dict], str]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    cases = data.get("cases", [])
    if data.get("synthetic") is not True or not 1 <= len(cases) <= 16:
        raise ValueError("Нужен синтетический набор из 1–16 сценариев")
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("ID сценариев должны различаться")
    for case in cases:
        if not 1 <= len(case["messages"]) <= 5:
            raise ValueError("В реплике от 1 до 5 сообщений")
    return cases, hashlib.sha256(text.encode()).hexdigest()


def published_kb(settings: Settings) -> Path:
    """Только чтение метаданных; рабочую Database (с миграциями) не создаём."""
    path = settings.db_path.resolve()
    if path.is_file():
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id FROM kb_versions WHERE activated_at IS NOT NULL "
                               "ORDER BY activated_at DESC, id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row:
            snapshot = path.parent / "kb_versions" / f"{row[0]:04d}"
            if snapshot.is_dir():
                return snapshot
    raise ValueError("Нет опубликованной базы: укажите --kb с отдельной учебной базой")


def write_report(folder: Path, report: dict) -> None:
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Неидеальные клиенты: симуляция", "", "Синтетические диалоги на копии базы; Telegram не подключён.",
             f"Провайдер: {report['model']}. Вызовы модели: {len(report['calls'])}/{report['max_calls']}, "
             f"токены по API: {sum(c['tokens'] for c in report['calls'])}.",
             "Это расход модели приложения; агенты подготовки и разбора используют лимит Codex отдельно.",
             "Цены и условия ответов требуют проверки по сохранённым источникам; доля answer не равна качеству.",
             "Сбор кандидатов здесь проверяется без утверждения и публикации.", ""]
    for case_id, info in report["cases"].items():
        lines += [f"## {case_id}", "", info["persona"], "", f"Цель: {info['goal']}", ""]
        for turn in (t for t in report["turns"] if t["case_id"] == case_id):
            lines += [f"**Клиент:** {turn['question']}", "", f"**Бот:** {turn['visible_reply'] or 'Не отправляет сообщение'}",
                      "", f"Решение: {turn['decision']}; причина: {turn['reason'] or '—'}. "
                      f"Карточка менеджеру: {'да' if turn['manager_card'] else 'нет'}.", ""]
    if report["learning"]:
        lines += ["## Что извлечено из сложных ответов менеджера", ""]
        for case in report["learning"]:
            lines += [f"### {case['id']}", "", f"Ожидание для независимой оценки: {case['expected']}", ""]
            for msg in case["messages"]:
                lines += [f"**{msg['role']}:** {msg['text']}", ""]
            for fact in case["candidates"]:
                lines += [f"**Кандидат:** {fact['question']}", fact["answer"], ""]
            if not case["candidates"]:
                lines += ["Кандидатов нет.", ""]
    errors = [c for c in report["calls"] if c["error"]]
    if errors or report["blocked_calls"]:
        lines += ["## Ошибки и бюджет", "", f"Заблокировано попыток сверх лимита: {report['blocked_calls']}", ""]
        lines += [f"- {c['phase']}: {c['error']}" for c in errors]
    lines += ["", f"Черновик и опубликованная KB не изменились: {report['kb_unchanged']}.", ""]
    (folder / "Отчёт.md").write_text("\n".join(lines), encoding="utf-8")


async def run_batch(folder: Path, inputs: Path, *, allow_api: bool = False, resume: bool = False,
                    learning: bool = False, max_calls: int = 48, kb_source: Path | None = None) -> dict:
    cases, batch_hash = load_cases(inputs)
    folder = folder.resolve()
    base = Settings.load()
    if not 1 <= max_calls <= 100:
        raise ValueError("Бюджет от 1 до 100 вызовов на весь прогон")
    if resume:
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        if report["live_model"] != allow_api or report["max_calls"] != max_calls:
            raise ValueError("При продолжении провайдер и общий лимит должны остаться прежними")
        if batch_hash in report["batches"]:
            raise ValueError("Этот пакет уже обработан; повторно его не добавляем")
        if not learning and any(c["id"] not in report["cases"] for c in cases):
            raise ValueError("В продолжении используйте ID начальных диалогов")
    else:
        if folder.exists() and any(folder.iterdir()):
            raise ValueError("Нужна новая пустая папка; для продолжения используйте --resume")
        source = kb_source or published_kb(base)
        if not source.is_dir():
            raise ValueError("Нет папки исходной KB")
        folder.mkdir(parents=True, exist_ok=True)
        versions.copy_kb(source, folder / "kb")
        report = {"synthetic": True, "live_model": allow_api, "max_calls": max_calls,
                  "source_kb": str(source.resolve()), "kb_fingerprint": versions.fingerprint(folder / "kb"),
                  "cases": {}, "turns": [], "learning": [], "calls": [], "blocked_calls": 0, "batches": []}
    settings = replace(base, db_path=folder / "app.db", kb_dir=folder / "kb", telegram_bot_token="",
                       admin_token="isolated-difficult-only", default_mode="auto", exam_files="")
    if allow_api and settings.llm_provider == "mock":
        raise ValueError("--allow-api требует живого провайдера")
    inner = make_provider(settings) if allow_api else ReplayLLM()
    llm = LimitedLLM(inner, max_calls)
    llm.records = report["calls"]
    llm.blocked_calls = report["blocked_calls"]
    services = Services(settings, llm)
    report["model"] = (settings.gigachat_model if settings.llm_provider == "gigachat" else
                       settings.openai_model) if allow_api else "replay"
    conn = services.db.upsert_connection("difficult-clients", 100, 100, True, True, "auto")
    try:
        if learning:
            conn = services.db.upsert_connection("difficult-observe", 100, 100, True, False, "observe")
            now = dt.datetime.now(dt.timezone.utc)
            for i, case in enumerate(cases):
                llm.phase = "learning:" + case["id"]
                chat_id = 5000 + len(report["learning"])
                for mid, item in enumerate(case["messages"], 1):
                    message = Message.model_validate({
                        "message_id": mid, "date": now + dt.timedelta(seconds=mid), "text": item["text"],
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": 100 if item["role"] == "manager" else chat_id,
                                 "is_bot": False, "first_name": "Синтетика"}})
                    await observe(message, services, conn, item["text"])
                facts = [dict(r) for r in services.db.candidates() if r["chat_id"] == chat_id]
                report["learning"].append({**case, "candidates": facts})
        else:
            for case in cases:
                case_id = case["id"]
                if case_id not in report["cases"]:
                    report["cases"][case_id] = {"chat_id": 1000 + len(report["cases"]),
                                                "persona": case["persona"], "goal": case["goal"], "risk": case["risk"]}
                chat = report["cases"][case_id]["chat_id"]
                question = "\n".join(case["messages"])
                llm.phase = "dialog:" + case_id
                outgoing = await process_client_message(services, conn, chat, question)
                row = services.db.get_dialog(outgoing.dialog_id) if outgoing.dialog_id else None
                source_ids = json.loads(row["sources"]) if row else []
                chunks = {c.id: c.text for c in services.kb.chunks + services.kb.catalog}
                state = Conversation.replay(services.kb, services.db.conversation_turns(conn.id, chat))
                report["turns"].append({"case_id": case_id, "messages": case["messages"], "question": question,
                                        "visible_reply": outgoing.to_client, "manager_card": outgoing.to_manager,
                                        "notice": outgoing.notice, "decision": row["decision"] if row else "skip_or_greeting",
                                        "reason": row["reason"] if row else "", "sources": source_ids,
                                        "evidence": [chunks[s] for s in source_ids if s in chunks],
                                        "state": asdict(state)})
        report["batches"].append(batch_hash)
        report["blocked_calls"] = llm.blocked_calls
        report["kb_unchanged"] = versions.fingerprint(settings.kb_dir) == report["kb_fingerprint"] \
            and not services.draft_changed()
        write_report(folder, report)
        return report
    finally:
        report["blocked_calls"] = llm.blocked_calls
        report["kb_unchanged"] = versions.fingerprint(settings.kb_dir) == report["kb_fingerprint"] \
            and not services.draft_changed()
        write_report(folder, report)
        await llm.close()
        services.db.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, default=FIXTURES / "difficult_clients.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--kb", type=Path, help="исходная учебная KB вместо копии опубликованной")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--learning", action="store_true")
    parser.add_argument("--allow-api", action="store_true")
    parser.add_argument("--max-calls", type=int, default=48)
    args = parser.parse_args()
    result = asyncio.run(run_batch(args.out, args.inputs, allow_api=args.allow_api, resume=args.resume,
                                  learning=args.learning, max_calls=args.max_calls, kb_source=args.kb))
    print(f"Реплик: {len(result['turns'])}; сценариев извлечения: {len(result['learning'])}; "
          f"вызовов: {len(result['calls'])}/{result['max_calls']}; "
          f"токенов: {sum(c['tokens'] for c in result['calls'])}")
    print(f"Отчёт: {args.out.resolve() / 'Отчёт.md'}")
    if any(c["error"] for c in result["calls"]) or result["blocked_calls"] or not result["kb_unchanged"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
