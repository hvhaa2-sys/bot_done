"""Импорт переписки менеджера из Telegram Desktop.

Как выгрузить: Telegram Desktop → Настройки → Продвинутые настройки →
Экспорт данных Telegram → отметить «Личные чаты», формат «Машиночитаемый JSON»,
медиа не нужны. Либо экспорт одного чата: ⋮ → Экспорт истории чата → JSON.

Запуск:
    python -m tools.import_telegram result.json
    python -m tools.import_telegram chat1.json chat2.json --me "Анна"
    python -m tools.import_telegram data/watch/messages.jsonl   # переписка, которую бот записал в режиме наблюдения

Что получится в data/import/:
- pairs.csv          — пары «вопрос клиента — ответ менеджера» (телефоны и почты скрыты);
- faq_draft.md       — черновик базы знаний: частые вопросы, сгруппированные по смыслу;
- questions_real.csv — тестовый набор для eval из реальных вопросов;
- stats.json         — как было до бота: сколько клиенты ждали первого ответа менеджера,
                       сколько обращений приходит вечером и в выходные, доля повторяющихся вопросов.

Черновики нужно просмотреть глазами: менеджер мог ответить неточно или устаревшей ценой.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.knowledge import format_rub  # noqa: E402
from app.core.pii import mask_pd  # noqa: E402,F401
from app.core.price_guard import extract_amounts  # noqa: E402
from app.core.text import tokenize  # noqa: E402
from app.core.waits import wait_summary  # noqa: E402

OUT_DIR = ROOT / "data" / "import"
MAX_GAP = dt.timedelta(hours=24)       # ответ позже суток не считаем ответом на вопрос
SIMILARITY = 0.45                      # порог похожести вопросов для группировки



@dataclass
class Msg:
    author: str
    date: dt.datetime
    text: str


@dataclass
class Pair:
    chat: str
    question: str
    answer: str
    date: dt.datetime


@dataclass
class Cluster:
    tokens: set[str]
    pairs: list[Pair] = field(default_factory=list)


def message_text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in raw)
    return ""


def load_chats(paths: list[Path]) -> tuple[list[dict], str | None]:
    """Вернуть список чатов и from_id владельца аккаунта (если известен из полного экспорта)."""
    chats, owner = [], None
    for path in paths:
        if path.suffix == ".jsonl":  # переписка из режима наблюдения: строка на сообщение
            chats += watch_chats(path)
            owner = "manager"
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if "chats" in data:  # полный экспорт аккаунта
            uid = data.get("personal_information", {}).get("user_id")
            owner = f"user{uid}" if uid else owner
            chats += [c for c in data["chats"].get("list", []) if c.get("type") == "personal_chat"]
        elif "messages" in data:  # экспорт одного чата
            chats.append(data)
    return chats, owner


def watch_chats(path: Path) -> list[dict]:
    """Файл наблюдения (data/watch/messages.jsonl) → чаты в формате выгрузки Telegram Desktop. Имён в файле нет:
    чаты называются «чат 1», «чат 2», автор — клиент или менеджер."""
    chats: dict[tuple[str, int], list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        m = json.loads(line)
        manager = m["from"] == "manager"
        chats.setdefault((m.get("conn", ""), m["chat"]), []).append(
            {"type": "message", "date": m["date"], "text": m["text"],
             "from": "Менеджер" if manager else "Клиент", "from_id": "manager" if manager else f"client{m['chat']}"})
    return [{"name": f"чат {i}", "type": "personal_chat", "messages": msgs}
            for i, msgs in enumerate(chats.values(), 1)]


def detect_owner(chats: list[dict], me: str | None, owner: str | None) -> set[str]:
    """Определить, какие сообщения написал менеджер: по from_id или по имени."""
    if owner:
        return {owner}
    authors_per_chat = [{m.get("from_id") for m in c["messages"] if m.get("type") == "message"} for c in chats]
    if me:
        ids = {m.get("from_id") for c in chats for m in c["messages"] if m.get("from") == me}
        if ids:
            return ids
        raise SystemExit(f"Не нашёл сообщений от «{me}». Проверьте имя, как оно записано в Telegram.")
    common = set.intersection(*authors_per_chat) if len(authors_per_chat) > 1 else set()
    common.discard(None)
    if len(common) == 1:
        return common
    raise SystemExit("Не удалось определить, где сообщения менеджера. Укажите имя: --me \"Имя\"")


def extract_pairs(chats: list[dict], owner_ids: set[str]) -> list[Pair]:
    pairs = []
    for chat in chats:
        msgs = [Msg(m.get("from_id", ""), dt.datetime.fromisoformat(m["date"]), message_text(m.get("text")).strip())
                for m in chat["messages"] if m.get("type") == "message"]
        msgs = [m for m in msgs if m.text]
        msgs.sort(key=lambda m: m.date)
        q_buf: list[Msg] = []
        a_buf: list[Msg] = []

        def flush() -> None:
            if q_buf and a_buf and a_buf[0].date - q_buf[-1].date <= MAX_GAP:
                pairs.append(Pair(chat.get("name") or "чат", mask_pd(" ".join(m.text for m in q_buf)),
                                  mask_pd("\n".join(m.text for m in a_buf)), q_buf[-1].date))

        for m in msgs:
            if m.author in owner_ids:
                if q_buf:
                    a_buf.append(m)
            else:
                if a_buf:
                    flush()
                    q_buf, a_buf = [], []
                q_buf.append(m)
        flush()
    return [p for p in pairs if is_meaningful(p)]


def is_meaningful(p: Pair) -> bool:
    """Отбросить «привет / спасибо / ок»: в вопросе должно быть хотя бы 2 значимых слова."""
    return len(tokenize(p.question)) >= 2 and len(p.answer.split()) >= 3


def cluster_pairs(pairs: list[Pair]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for p in sorted(pairs, key=lambda p: p.date):
        toks = set(tokenize(p.question))
        best, best_sim = None, 0.0
        for c in clusters:
            sim = len(toks & c.tokens) / (len(toks | c.tokens) or 1)
            if sim > best_sim:
                best, best_sim = c, sim
        if best and best_sim >= SIMILARITY:
            best.pairs.append(p)
            best.tokens |= toks
        else:
            clusters.append(Cluster(toks, [p]))
    clusters.sort(key=lambda c: len(c.pairs), reverse=True)
    return clusters


def representative(c: Cluster) -> tuple[str, str]:
    """Самый короткий из вопросов группы и самый свежий ответ менеджера."""
    question = min((p.question for p in c.pairs), key=len)
    answer = max(c.pairs, key=lambda p: p.date).answer
    return question, answer


def response_stats(chats: list[dict], owner_ids: set[str], work_hours: tuple[int, int] = (9, 19),
                   work_days: str = "12345") -> dict:
    """Как было до бота: сколько клиент ждал первого ответа менеджера.

    Обращение — первое сообщение клиента после ответа менеджера (или начала чата).
    Следующие сообщения клиента до ответа ожидание не сбрасывают. Время — как в выгрузке
    (местное время компьютера, на котором её сделали).
    """
    waits: list[tuple[dt.datetime, float]] = []   # (когда написал клиент, минут до ответа)
    unanswered = 0
    for chat in chats:
        msgs = sorted((dt.datetime.fromisoformat(m["date"]), m.get("from_id", ""))
                      for m in chat["messages"] if m.get("type") == "message")
        started = None
        for date, author in msgs:
            if author in owner_ids:
                if started is not None:
                    waits.append((started, (date - started).total_seconds() / 60))
                    started = None
            elif started is None:
                started = date
        unanswered += started is not None

    return wait_summary(waits, unanswered, f"{work_hours[0]:02d}:00", f"{work_hours[1]:02d}:00", work_days)


def run_import(files: list[Path], me: str | None = None, out_dir: Path = OUT_DIR) -> dict:
    """Весь импорт: пары, черновик FAQ, вопросы для экзамена и stats.json. Возвращает итог."""
    chats, owner = load_chats(files)
    if not chats:
        raise ValueError("В файлах нет личных чатов. Нужен экспорт в формате JSON.")
    owner_ids = detect_owner(chats, me, owner)
    pairs = extract_pairs(chats, owner_ids)
    clusters = cluster_pairs(pairs)
    write_outputs(pairs, clusters, out_dir)
    repeated = sum(len(c.pairs) for c in clusters if len(c.pairs) > 1)
    summary = {
        "chats": len(chats), "pairs": len(pairs), "groups": len(clusters),
        "repeated_share": round(repeated / len(pairs), 3) if pairs else None,
        "top": [[len(c.pairs), representative(c)[0]] for c in clusters[:10]],
        **response_stats(chats, owner_ids),
    }
    (out_dir / "stats.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_outputs(pairs: list[Pair], clusters: list[Cluster], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "pairs.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "chat", "question", "answer"])
        for p in pairs:
            w.writerow([p.date.strftime("%Y-%m-%d %H:%M"), p.chat, p.question, p.answer])

    lines = ["# ЧЕРНОВИК базы знаний из переписки. Проверьте каждый ответ,",
             "# уберите устаревшие цены (цены должны жить в prices.csv) и личные детали.", ""]
    for c in clusters:
        q, a = representative(c)
        lines += [f"# встречался {len(c.pairs)} раз(а)", f"## {q}", a, ""]
    (out_dir / "faq_draft.md").write_text("\n".join(lines), encoding="utf-8")

    with (out_dir / "questions_real.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question", "expected", "must_contain"])
        for p in pairs:
            amounts = ";".join(format_rub(a).replace(" ₽", "") for a in extract_amounts(p.answer))
            w.writerow([p.question, "answer", amounts])


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт переписки из Telegram Desktop (JSON)")
    parser.add_argument("files", nargs="+", type=Path, help="result.json или файлы экспорта чатов")
    parser.add_argument("--me", help="имя менеджера, как оно записано в Telegram")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    try:
        r = run_import(args.files, args.me, args.out)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"Чатов: {r['chats']}, пар вопрос-ответ: {r['pairs']}, групп похожих вопросов: {r['groups']}")
    print("Самые частые вопросы:")
    for n, question in r["top"]:
        print(f"  {n:>3} × {question[:90]}")
    # Цифры для защиты: как было до бота
    if r["repeated_share"] is not None:
        print(f"\nПовторяющиеся вопросы — {r['repeated_share']:.0%} всех обращений.")
    if r["median_wait_min"] is not None:
        print(f"Клиент ждал первого ответа: медиана {r['median_wait_min']} мин, "
              f"за 5 минут отвечено {r['within_5_min']:.0%}, дольше часа — {r['over_1_hour']:.0%}.")
        if r["off_hours_share"] is not None:
            print(f"Вечером и в выходные приходит {r['off_hours_share']:.0%} обращений"
                  + (f", медиана ожидания там {r['off_hours_median_wait_min']} мин." if r["off_hours_median_wait_min"] is not None else "."))
    print(f"Готово: {args.out}/pairs.csv, faq_draft.md, questions_real.csv, stats.json")


if __name__ == "__main__":
    main()
