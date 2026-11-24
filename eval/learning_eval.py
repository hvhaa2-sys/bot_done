"""Экзамен обучения: из ответов менеджера — факты для базы, по ним — ответы на новые формулировки.

Набор `eval/learning_pairs.csv` синтетический: вопросы и ответы написаны по заметкам базы в стиле
переписки (приветствия, обещания, личное рядом с общим), одно условное правило ДЕПО придумано для проверки
«только если … иначе». Колонки: id, kind, client, manager, must, followup, followup_must.
kind: fact — из ответа должен получиться факт, где есть всё из must; none — личное и разовое, фактов быть
не должно; any — фактов может не быть, но если есть, то с must. must — как в экзаменах: «a|b», «!x», число целиком.

Ход как у живого бота, в отдельной папке: база — только прайс и стоп-темы (холодный старт); новые формулировки
спрашиваются до обучения; ответ менеджера → `Services.collect_candidates`; менеджер утверждает только верные
факты (`approve_candidates`, сверку с источником отмечает сам), факт с потерянным смыслом правит по своему ответу;
версия публикуется по обычным правилам; затем те же новые формулировки ещё раз через `Services.answer`.

    python -m eval.learning_eval --offline     # без сети: заглушка, пара «вопрос → ответ» как есть
    python -m eval.learning_eval               # модель из .env (GigaChat), запросы по одному
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.core import candidates as C  # noqa: E402
from app.core.facts import contains  # noqa: E402
from app.core.price_guard import extract_amounts  # noqa: E402
from app.llm import make_provider  # noqa: E402
from app.services import Services  # noqa: E402

PAIRS = ROOT / "eval" / "learning_pairs.csv"


class Counting:
    """Обёртка модели: сколько запросов и токенов ушло на экзамен."""

    def __init__(self, llm):
        self.llm, self.calls, self.tokens = llm, 0, 0

    async def complete(self, system: str, messages: list[dict]) -> str:
        self.calls += 1
        try:
            return await self.llm.complete(system, messages)
        finally:
            self.tokens += getattr(self.llm, "last_tokens", 0) or 0

    def __getattr__(self, name):
        return getattr(self.llm, name)


def load(path: Path = PAIRS) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def items(text: str) -> list[str]:
    return [m.strip() for m in (text or "").split(";") if m.strip()]


def passes(text: str, must: str) -> bool:
    return all(contains(text, m) for m in items(must))


def workspace(folder: Path) -> Path:
    kb = folder / "kb"
    kb.mkdir(parents=True)
    for name in ("prices.csv", "stop_topics.txt"):
        shutil.copy(ROOT / "data" / "kb" / name, kb / name)
    return kb


async def ask(services: Services, question: str, must: str) -> dict:
    reply = await services.answer(question)
    return {"answered": reply.is_answer, "ok": reply.is_answer and passes(reply.text, must),
            "text": reply.text if reply.is_answer else "", "reason": "" if reply.is_answer else reply.reason}


def approve(services: Services, row, answer: str, example: dict) -> tuple[bool, str]:
    # Область берём из эталонного диалога, до извлечения факта моделью.
    _, scope = C.target_note(services.kb, example["client"], example["manager"])
    if not scope and example["id"] in {"mockup_approval", "designer", "radio_where", "music_rao", "extend_monthly"}:
        scope = "business"
    ok, msg = services.approve_candidates([row["id"]], row["question"], answer, scope=scope)
    if not ok and "сверка" in msg:  # менеджер сверил с исходным ответом и подтвердил
        ok, msg = services.approve_candidates([row["id"]], row["question"], answer, scope=scope, source_reviewed=True)
    return ok, msg


async def run(folder: Path, offline: bool, pairs: list[dict]) -> tuple[list[dict], dict]:
    kb_dir = workspace(folder)
    settings = replace(Settings.load(), db_path=folder / "app.db", kb_dir=kb_dir, exam_files="",
                       **({"llm_provider": "mock"} if offline else {}))
    counter = Counting(make_provider(settings))
    services = Services(settings, llm=counter)
    rows: list[dict] = []
    try:
        for p in pairs:
            if p["followup"]:
                p["before"] = await ask(services, p["followup"], p["followup_must"])
        for n, p in enumerate(pairs, 1):
            seen = {r["id"] for r in services.db.candidates()}
            await services.collect_candidates(source="chat", connection_id="exam", chat_id=n, dialog_id=None,
                                              client_question=p["client"], manager_text=p["manager"])
            new = [r for r in services.db.candidates() if r["id"] not in seen]
            good = [r for r in new if passes(r["answer"], p["must"])]
            invented = [r for r in new if not set(extract_amounts(r["answer"])) <= set(extract_amounts(p["manager"]))]
            todo = [(r, r["answer"]) for r in good] if p["kind"] != "none" else []
            if p["kind"] == "fact" and new and not good:  # смысл потерян — менеджер правит факт по своему ответу
                todo = [(new[0], C.drop_promises(C.tidy(p["manager"])))]
            approved, fixed, blocked, unpublished = 0, 0, [], []
            for r, answer in todo:
                ok, msg = approve(services, r, answer, p)
                approved += ok
                fixed += ok and not good
                if not ok:
                    blocked.append(msg)
                elif not msg.startswith("Опубликована"):
                    unpublished.append(msg)
            rows.append({**p, "candidates": [{"question": r["question"], "answer": r["answer"], "note": r["note"]}
                                             for r in new],
                         "good": len(good), "invented": len(invented), "approved": approved, "fixed": fixed,
                         "blocked": blocked, "unpublished": unpublished})
        for row in rows:
            if row["followup"]:
                row["after"] = await ask(services, row["followup"], row["followup_must"])
        summary = summarize(rows)
        summary.update(calls=counter.calls, tokens=counter.tokens, versions=services.kb_version,
                       provider=settings.llm_provider + (f":{counter.model}" if hasattr(counter.llm, "model") else ""))
        return rows, summary
    finally:
        await services.llm.close()


def summarize(rows: list[dict]) -> dict:
    facts = [r for r in rows if r["kind"] == "fact"]
    none = [r for r in rows if r["kind"] == "none"]
    other = [r for r in rows if r["kind"] == "any"]
    follow = [r for r in rows if r["followup"]]
    return {
        "pairs": len(rows),
        "fact_pairs": len(facts),
        "fact_found": sum(r["good"] > 0 for r in facts),
        "fact_lost_meaning": sum(r["good"] == 0 and bool(r["candidates"]) for r in facts),
        "fact_missed": sum(not r["candidates"] for r in facts),
        "personal_pairs": len(none),
        "personal_leaked": sum(bool(r["candidates"]) for r in none),
        "quote_bad": sum(len(r["candidates"]) > r["good"] for r in other),
        "candidates": sum(len(r["candidates"]) for r in rows),
        "invented_amounts": sum(r["invented"] for r in rows),
        "approved": sum(r["approved"] for r in rows),
        "fixed_by_manager": sum(r["fixed"] for r in rows),
        "blocked": sum(len(r["blocked"]) for r in rows),
        "unpublished": sum(len(r["unpublished"]) for r in rows),
        "followups": len(follow),
        "before_answered": sum(r["before"]["answered"] for r in follow),
        "before_ok": sum(r["before"]["ok"] for r in follow),
        "after_answered": sum(r["after"]["answered"] for r in follow),
        "after_ok": sum(r["after"]["ok"] for r in follow),
        "after_ok_of_learned": sum(r["after"]["ok"] for r in follow if r["approved"]),
        "learned_with_followup": sum(bool(r["approved"]) for r in follow),
    }


def report(s: dict) -> str:
    return "\n".join([
        f"Экзамен обучения, {s['provider']}: пар {s['pairs']}, запросов к модели {s['calls']}, токенов {s['tokens']}.",
        f"Общие факты: найдено {s['fact_found']} из {s['fact_pairs']}, смысл потерян {s['fact_lost_meaning']}, "
        f"пропущено {s['fact_missed']}.",
        f"Личное и разовое: протекло в кандидаты {s['personal_leaked']} из {s['personal_pairs']}; "
        f"ложная цитата — плохих кандидатов {s['quote_bad']}.",
        f"Кандидатов всего {s['candidates']}, суммы не из ответа менеджера — {s['invented_amounts']}. "
        f"Утверждено {s['approved']} (из них менеджер поправил {s['fixed_by_manager']}), не прошло утверждение "
        f"{s['blocked']}, утверждено без публикации {s['unpublished']}, бот отвечает по версии базы {s['versions']}.",
        f"Новые формулировки ({s['followups']}): до обучения бот ответил сам на {s['before_answered']} "
        f"(верно {s['before_ok']}); после — на {s['after_answered']} (верно {s['after_ok']}; по утверждённым фактам "
        f"верно {s['after_ok_of_learned']} из {s['learned_with_followup']}).",
    ])


def write(rows: list[dict], summary: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "candidates", "good", "approved", "fixed", "blocked", "before", "before_text",
                    "after", "after_text", "facts", "notes"])
        for r in rows:
            b, a = r.get("before") or {}, r.get("after") or {}
            state = lambda x: "" if not x else ("верно" if x["ok"] else "ответ" if x["answered"] else x["reason"])  # noqa: E731
            w.writerow([r["id"], r["kind"], len(r["candidates"]), r["good"], r["approved"], r["fixed"],
                        " | ".join(r["blocked"] + r["unpublished"]), state(b), b.get("text", ""), state(a),
                        a.get("text", ""), " || ".join(f"{c['question']} → {c['answer']}" for c in r["candidates"]),
                        " || ".join(c["note"] for c in r["candidates"] if c["note"])])
    out.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="без сети: заглушка вместо модели")
    parser.add_argument("--pairs", type=Path, default=PAIRS)
    parser.add_argument("--out", type=Path, help="CSV с результатами (рядом — .json со сводкой)")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="learning-eval-"))
    try:
        rows, summary = asyncio.run(run(folder, args.offline, load(args.pairs)))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    name = "mock" if args.offline else summary["provider"].replace(":", "_")
    write(rows, summary, args.out or ROOT / "eval" / f"results_learning_{name}.csv")
    print(report(summary))


if __name__ == "__main__":
    main()
