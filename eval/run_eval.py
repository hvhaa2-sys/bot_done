"""Стенд оценки качества ответов.

Запуск:
    python -m eval.run_eval                       # модель из .env
    python -m eval.run_eval --provider mock       # без сети
    python -m eval.run_eval --compare mock,gigachat
    python -m eval.run_eval --compare gigachat:GigaChat,gigachat:GigaChat-Pro   # модели одного провайдера

Метрики:
- decision_accuracy — бот правильно решил, отвечать самому или звать менеджера;
- unsafe_answers — бот ответил сам там, где должен был позвать менеджера (самая опасная ошибка);
- missed — позвал менеджера, хотя ответ есть в базе;
  expected=any — годится и честное «такого у нас нет», и передача менеджеру (метро, билборды, ролик под ключ);
- content_hit — в ответах есть ожидаемые факты (цены, форматы);
- price_guard_trips — сколько раз защита цен поймала выдуманную сумму;
- latency — среднее и 95-й перцентиль, мс; latency_answer_avg_ms — среднее только по ответам бота
  (передачи менеджеру по стоп-темам занимают 0 мс и занижают общее среднее).

Результаты по вопросам — в eval/results_<провайдер>[_<модель>][_<набор>].csv.
Наборы: eval/questions.csv (основной экзамен), eval/questions_live.csv (живые сообщения:
опечатки, сленг, слитное написание) и eval/questions_stress.csv (большой прогон по всему прайсу
и диалоги: строки с одинаковым dialog идут подряд и видят историю):
python -m eval.run_eval --questions eval/questions_live.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.core.real_flow import BusinessAssistant as Assistant  # noqa: E402
from app.core.conversation import Conversation  # noqa: E402
from app.core.facts import contains, norm  # noqa: E402, F401
from app.core.knowledge import KnowledgeBase, SearchHit  # noqa: E402
from app.core.price_guard import extract_amounts, extract_numbers  # noqa: E402
from app.llm import make_provider  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.csv"


def load_questions(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fact_issues(kb: KnowledgeBase, answer: str, question: str, all_prices: list) -> str:
    """Факт-чек ответа по всему прайсу: сумма не из прайса или из строки другого ТЦ."""
    allowed = kb.allowed_amounts()
    asked = set(extract_amounts(question))
    stray = [a for a in extract_amounts(answer) if a not in allowed and a not in asked]
    wrong = [a for a in kb.mismatched_amounts(answer, all_prices, question) if a not in set(extract_numbers(question))]
    return "; ".join(filter(None, [f"нет в прайсе: {stray}" if stray else "", f"чужой ТЦ: {wrong}" if wrong else ""]))


def parse_target(spec: str, settings: Settings) -> tuple[str, Settings]:
    """«gigachat:GigaChat-Pro» → (метка для отчёта, настройки с этой моделью)."""
    name, _, model = spec.strip().partition(":")
    settings = replace(settings, llm_provider=name)
    if model and name == "gigachat":
        settings = replace(settings, gigachat_model=model)
    elif model and name == "openai":
        settings = replace(settings, openai_model=model)
    model = {"gigachat": settings.gigachat_model, "openai": settings.openai_model}.get(name, "")
    return (f"{name}_{model}" if model else name), settings


async def evaluate(label: str, settings: Settings, questions: list[dict],
                   progress=None, *, llm=None, kb=None) -> tuple[dict, list[dict]]:
    """progress(сделано, всего) — необязательный колбэк, им пользуется админка."""
    owns_llm = llm is None
    llm = llm or make_provider(settings)
    kb = kb or KnowledgeBase.load(settings.kb_dir)
    assistant = Assistant(kb, llm, min_score=settings.retrieval_min_score,
                          confidence_threshold=settings.confidence_threshold)
    rows = []
    histories: dict[str, list[tuple[str, str]]] = {}  # диалоги: строки с одним dialog идут подряд, с историей
    last: dict[str, str] = {}                           # прошлое сообщение клиента в диалоге, в любом статусе
    conversations: dict[str, Conversation] = {}
    all_prices = [SearchHit(c, 0.0) for c in kb.chunks if c.kind == "price"]
    try:
        for q in questions:
            dialog = (q.get("dialog") or "").strip()
            history = histories.setdefault(dialog, []) if dialog else []
            state = conversations.setdefault(dialog, Conversation()) if dialog else None
            reply = await assistant.answer(q["question"], history[-3:], last_question=last.get(dialog, ""),
                                           conversation=state)
            if dialog:
                last[dialog] = q["question"]
                if reply.is_answer and reply.reason != "greeting":
                    history.append((q["question"], reply.text))
            expected = q["expected"].strip()
            must = [m.strip() for m in (q.get("must_contain") or "").split(";") if m.strip()]
            content_ok = fact = None
            if expected in ("answer", "any") and reply.is_answer:
                content_ok = all(contains(reply.text, m) for m in must) if must else (True if expected == "answer" else None)
                fact = fact_issues(kb, reply.text, q["question"], all_prices)
            rows.append({
                "dialog": dialog, "question": q["question"], "expected": expected, "decision": reply.decision,
                "correct_decision": expected == "any" or reply.decision == expected, "content_ok": content_ok,
                "reason": reply.reason, "confidence": round(reply.confidence, 2),
                "latency_ms": reply.latency_ms, "answer": reply.text,
                "llm_called": reply.llm_called, "tokens": reply.tokens, "fact_issues": fact or "",
            })
            if progress:
                progress(len(rows), len(questions))
    finally:
        if owns_llm:
            await llm.close()
    return summarize(label, rows), rows


def summarize(label: str, rows: list[dict]) -> dict:
    n = len(rows) or 1
    lat = sorted(r["latency_ms"] for r in rows)
    content = [r["content_ok"] for r in rows if r["content_ok"] is not None]
    answered = [r["latency_ms"] for r in rows if r["decision"] == "answer"]
    summary = {
        "provider": label,
        "questions": n,
        "decision_accuracy": sum(r["correct_decision"] for r in rows) / n,
        "unsafe_answers": sum(1 for r in rows if r["expected"] == "escalate" and r["decision"] == "answer"),
        "missed": sum(1 for r in rows if r["expected"] == "answer" and r["decision"] == "escalate"),
        "content_hit": (sum(content) / len(content)) if content else 0.0,
        "price_guard_trips": sum(1 for r in rows if r["reason"].startswith("price_mismatch")),
        "fact_errors": sum(1 for r in rows if r.get("fact_issues")),
        "latency_avg_ms": int(statistics.mean(lat)) if lat else 0,
        "latency_p95_ms": lat[int(0.95 * (n - 1))] if lat else 0,
        "latency_answer_avg_ms": int(statistics.mean(answered)) if answered else 0,
    }
    return summary


def print_summary(summaries: list[dict]) -> None:
    keys = ["provider", "questions", "decision_accuracy", "unsafe_answers", "missed",
            "content_hit", "fact_errors", "price_guard_trips", "latency_avg_ms", "latency_p95_ms",
            "latency_answer_avg_ms"]
    fmt = lambda v: f"{v:.0%}" if isinstance(v, float) else str(v)  # noqa: E731
    print("| " + " | ".join(keys) + " |")
    print("|" + "---|" * len(keys))
    for s in summaries:
        print("| " + " | ".join(fmt(s[k]) for k in keys) + " |")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Оценка качества ответов ассистента")
    parser.add_argument("--provider", help="mock | gigachat | openai (по умолчанию из .env)")
    parser.add_argument("--compare", help="несколько провайдеров через запятую, модель через двоеточие")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--repeat", type=int, default=1, help="прогнать набор N раз и показать плавающие решения")
    args = parser.parse_args()

    settings = Settings.load()
    providers = (args.compare.split(",") if args.compare else [args.provider or settings.llm_provider])
    questions = load_questions(args.questions)

    summaries = []
    for spec in providers:
        name, target = parse_target(spec, settings)
        rows = []
        for run in range(args.repeat):
            _, run_rows = await evaluate(name, target, questions)
            rows += [{**r, "run": run + 1} for r in run_rows]
        summary = summarize(name, rows)
        summaries.append(summary)
        if args.repeat > 1:
            decisions: dict[tuple[str, str], set[str]] = {}
            for r in rows:
                decisions.setdefault((r["dialog"], r["question"]), set()).add(r["decision"])
            flaky = [q for (_, q), d in decisions.items() if len(d) > 1]
            print(f"\nПлавающие решения [{name}]: {len(flaky)}" + "".join(f"\n  ~ {q}" for q in flaky))
        # Свой файл на модель и набор вопросов: основной экзамен и «живые» сообщения не затирают друг друга
        suffix = "" if args.questions == QUESTIONS else f"_{args.questions.stem}"
        out = ROOT / "eval" / f"results_{name}{suffix}.csv"
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        errors = [r for r in rows if not r["correct_decision"] or r["content_ok"] is False or r.get("fact_issues")]
        if errors:
            print(f"\nОшибки [{name}]:")
            for r in errors:
                print(f"  - {r['question']} → {r['decision']} ({r['reason'] or 'ok'}); ожидалось {r['expected']}"
                      + ("; нет нужных фактов в ответе" if r["content_ok"] is False else "")
                      + (f"; факт-чек: {r['fact_issues']}" if r.get("fact_issues") else ""))
    print()
    print_summary(summaries)


if __name__ == "__main__":
    asyncio.run(main())
