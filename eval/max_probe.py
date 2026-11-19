"""Синтетические диалоги через MAX-адаптер; своя БД, копия KB, отправки только в память.

python -m eval.max_probe --questions eval/simulation/max_unusual.csv --out eval/simulation/runs/max_probe
Живая модель включается только с --allow-api. --resume сохраняет историю диалогов и бюджет.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from app.channels.max_bot import MaxBot
from app.config import Settings
from app.core import versions
from app.core.knowledge import SearchHit
from app.llm import make_provider
from app.services import Services, apply
from eval.run_eval import fact_issues
from tools.simulate_difficult import published_kb
from tools.simulate_people import LimitedLLM


class Sink:
    """Нет токена, сетевого клиента, опроса событий или реальных получателей."""
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})
        return {"message": {"body": {"mid": f"reply{len(self.sent)}", "seq": len(self.sent), "text": text}}}


async def run(args):
    out = args.out.resolve()
    base = Settings.load()
    with sqlite3.connect(f"file:{base.db_path}?mode=ro", uri=True) as db:
        base = apply(base, dict(db.execute("SELECT key,value FROM settings")))
    source = published_kb(base)
    before = versions.fingerprint(source)
    draft_before = versions.fingerprint(base.kb_dir)
    if args.resume:
        report = json.loads((out / "report.json").read_text())
        assert report["source_fingerprint"] == before, "Опубликованная база изменилась"
        assert report["allow_api"] == args.allow_api and report["max_calls"] == args.max_calls
    else:
        if out.exists() and any(out.iterdir()):
            raise ValueError("Нужна пустая папка или --resume")
        out.mkdir(parents=True, exist_ok=True)
        versions.copy_kb(source, out / "kb")
        report = {"allow_api": args.allow_api, "max_calls": args.max_calls, "source_fingerprint": before,
                  "model": base.gigachat_model, "calls": [], "turns": [], "dialogs": {}}
    settings = replace(base, db_path=out / "app.db", kb_dir=out / "kb", telegram_bot_token="",
                       max_bot_token="", max_webhook_url="", max_webhook_secret="", speech_enabled=False,
                       admin_token="isolated-max-probe", default_mode="auto", exam_files="",
                       llm_provider=base.llm_provider if args.allow_api else "mock")
    llm = LimitedLLM(make_provider(settings), args.max_calls)
    llm.records = report["calls"]
    services = Services(settings, llm)
    await services.update_settings({"merge_seconds": "0", "auto_schedule": "always", "chat_hourly_limit": "0",
                                    "daily_llm_limit": "0"})
    sink = Sink()
    bot = MaxBot(sink, services, 1)
    conn = services.db.upsert_connection("max:100", 100, 1000, True, True, "auto")
    services.db.approve_connection(conn.id)
    prices = [SearchHit(c, 0.0) for c in services.kb.chunks if c.kind == "price"]
    questions = list(csv.DictReader(args.questions.open(encoding="utf-8")))
    try:
        for q in questions:
            dialog = q["dialog"]
            if dialog not in report["dialogs"]:
                report["dialogs"][dialog] = 5000 + len(report["dialogs"])
            chat = report["dialogs"][dialog]
            llm.phase = f"{dialog}:{len(report['turns']) + 1}"
            previous = services.db.conn.execute("SELECT coalesce(max(id),0) FROM dialogs").fetchone()[0]
            start = len(sink.sent)
            seq = len(report["turns"]) + 1
            await bot.handle({"update_type": "message_created", "message": {
                "sender": {"user_id": chat, "first_name": "Тестовый клиент", "is_bot": False},
                "recipient": {"chat_id": chat, "chat_type": "dialog"}, "timestamp": int(time.time() * 1000),
                "body": {"mid": f"probe{seq}", "seq": seq, "text": q["question"]}}})
            row = services.db.conn.execute("SELECT * FROM dialogs WHERE id>? ORDER BY id DESC LIMIT 1", (previous,)).fetchone()
            client = "\n".join(m["text"] for m in sink.sent[start:] if m.get("chat_id") == chat)
            manager = any(m.get("chat_id") == 1000 for m in sink.sent[start:])
            decision = row["decision"] if row else ("answer" if client else "skip")
            turn = {**q, "decision": decision, "reason": row["reason"] if row else "", "answer": client,
                    "manager_card": manager, "correct_decision": q["expected"] in ("any", decision),
                    "fact_issues": fact_issues(services.kb, client, q["question"], prices) if decision == "answer" else ""}
            chunks = {c.id: c.text for c in services.kb.chunks + services.kb.catalog}
            turn["evidence"] = [chunks[key] for key in json.loads(row["sources"]) if key in chunks] if row else []
            report["turns"].append(turn)
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"{seq}: {dialog}: {decision} {turn['reason']}", flush=True)
    finally:
        report["blocked_calls"] = llm.blocked_calls
        report["source_unchanged"] = versions.fingerprint(source) == before
        report["draft_unchanged"] = versions.fingerprint(base.kb_dir) == draft_before
        report["copy_unchanged"] = versions.fingerprint(out / "kb") == before
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        await llm.close()
        services.db.conn.close()
    assert report["source_unchanged"] and report["draft_unchanged"] and report["copy_unchanged"]
    assert not llm.blocked_calls and not any(c["error"] for c in llm.records)
    print(f"Turns: {len(report['turns'])}; calls: {len(llm.records)}; tokens: {sum(c['tokens'] for c in llm.records)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-api", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-calls", type=int, default=80)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 100:
        parser.error("Бюджет от 1 до 100 вызовов")
    asyncio.run(run(args))
