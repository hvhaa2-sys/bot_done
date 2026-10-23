"""База знаний из переписки: «кандидаты» в факты.

Менеджер отвечает клиенту — на карточку бота или прямо в чате. Модель выделяет из ответа общие
факты («вопрос клиента → ответ»), пригодные и другим клиентам. Они копятся как кандидаты; менеджер
раз в день или в неделю утверждает их в админке или кнопками в сводке, и утверждённое попадает
в заметки базы знаний. Так новый бизнес может начать с пустой базой: бот пишет черновики,
менеджер отвечает, база растёт сама.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .candidate_review import fact_scope
from .knowledge import KnowledgeBase, venue_tokens
from .pii import mask_pd
from .price_guard import extract_amounts
from .text import tokenize

EXTRACT_PROMPT = """Ты помогаешь собрать базу знаний менеджера по продаже рекламы в торговых центрах.
Тебе дают вопрос клиента и ответ менеджера. Выдели из ответа менеджера общие факты, которые пригодятся
и другим клиентам: цены, условия, сроки, требования к макетам и роликам, места размещения, правила ТЦ.

Правила:
- Бери только то, что написал менеджер. Ничего не добавляй и не пересчитывай.
- Сохраняй смысл целиком: отрицания, обязанности, условия, исключения и последствия «иначе».
  «Клиент предоставляет ролик» не заменяет «мы не изготавливаем ролик»: сохрани оба ограничения.
  «Можно только если А И Б; иначе убрать» нельзя сокращать до «всегда убрать», терять А или Б,
  менять И на ИЛИ. Условная ветка должна остаться условной, даже в отдельном предложении.
- Сохраняй границы ТЦ, места и формата в паре вопрос/ответ. Разные правила разных ТЦ/форматов
  выделяй отдельно; каждый факт не обязан повторять остальные независимые правила ответа.
- Цитата клиента в ответе менеджера и предположение в вопросе не являются подтверждением.
  При опровержении извлекай исправление менеджера, а не ложную цитату. Не выводи из
  «ещё не проверяли» доступность, недоступность или постоянное правило.
- Не бери личное и разовое: имена, телефоны, даты брони этого клиента, «место свободно», скидки
  этому клиенту, обещания «пришлю», «перезвоню», «уточню».
- Вопрос пиши так, как его задал бы клиент, ответ — 1–3 предложения от лица менеджера.
- Если общих фактов нет (приветствие, «сейчас уточню», личная договорённость) — верни пустой список.

Верни ТОЛЬКО JSON: {"facts": [{"question": "...", "answer": "..."}]}"""

MAX_FACTS = 3
MIN_MANAGER_TEXT = 20   # «Добрый день!», «Сейчас уточню» — фактов там нет
MIN_ANSWER = 15
# Личное и разовое: это решает менеджер в каждом случае, в базу не переносим
_PERSONAL_RE = re.compile(r"скидк|бронь|брониру|забронир|свободн|занят|перезвон|пришлю|пришлём|уточню|договор"
                          r"|счёт|счет\b|оплатит|реквизит|ваш\w* заказ", re.IGNORECASE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
_PD_MARK_RE = re.compile(r"\[(?:телефон|почта|карта)\]")
_GREETING_RE = re.compile(r"(?:здравствуйте|здрасьте|добрый (?:день|вечер)|доброе утро|привет\w*|спасибо(?: за вопрос)?)"
                          r"\b[!,.\s]*", re.IGNORECASE)
_STRING = r'"((?:[^"\\]|\\.)*)"'
_PAIR_RE = re.compile(r'\{\s*"question"\s*:\s*' + _STRING + r'\s*,\s*"answer"\s*:\s*' + _STRING + r"\s*\}")
SIMILARITY = 0.45
NOTES_FOLDER = "Из переписки"


@dataclass
class Fact:
    question: str
    answer: str


def parse_facts(raw: str) -> list[Fact] | None:
    """JSON модели → факты. Пустой список — модель сказала, что общих фактов нет; None — ответ не разобрать.
    GigaChat иногда ломает собственный JSON («…}])}»), тогда достаём пары «вопрос — ответ» по полям."""
    match = _JSON_RE.search(raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        pairs = [(json.loads(f'"{q}"'), json.loads(f'"{a}"')) for q, a in _PAIR_RE.findall(match.group(0))]
        return [Fact(" ".join(q.split()), " ".join(a.split())) for q, a in pairs][:MAX_FACTS] or None
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    facts = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and isinstance(item.get("answer"), str):
            facts.append(Fact(" ".join(item["question"].split()), " ".join(item["answer"].split())))
    return facts[:MAX_FACTS]


def keep(fact: Fact, manager_text: str) -> bool:
    """Факт годится в кандидаты: не пустой, не личный и без сумм, которых не было у менеджера."""
    if len(fact.answer) < MIN_ANSWER or not fact.question:
        return False
    if _PERSONAL_RE.search(fact.answer) or _PERSONAL_RE.search(fact.question):
        return False
    return set(extract_amounts(fact.answer)) <= set(extract_amounts(manager_text))


def raw_facts(client_question: str, manager_text: str) -> list[Fact]:
    """Без модели: пара «вопрос клиента → ответ менеджера», если она похожа на факт. Приветствия
    и фразы клиента со своими контактами («Мой номер [телефон]») в базу не переносим."""
    client_question, manager_text = mask_pd(client_question), mask_pd(manager_text)
    fact = Fact(tidy(client_question, drop_contacts=True), tidy(manager_text))
    return [fact] if len(fact.answer) >= MIN_MANAGER_TEXT and keep(fact, manager_text) else []


def tidy(text: str, drop_contacts: bool = False) -> str:
    """«Здравствуйте! Какой длины ролик? Мой номер [телефон]» → «Какой длины ролик?»."""
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if drop_contacts:
        sentences = [s for s in sentences if not _PD_MARK_RE.search(s)]
    out = " ".join(" ".join(sentences).split())
    while (m := _GREETING_RE.match(out)) and m.end():
        out = out[m.end():]
    return out  # одно приветствие — пустая строка: такой пары в базу нет


async def extract(llm, client_question: str, manager_text: str) -> tuple[list[Fact], int]:
    """(факты, токены). Без модели или при сбое — пара «вопрос клиента → ответ менеджера» как есть."""
    client_question, manager_text = mask_pd(client_question), mask_pd(manager_text)
    if len(manager_text) < MIN_MANAGER_TEXT:
        return [], 0
    tokens = 0
    try:
        raw = await llm.complete(EXTRACT_PROMPT, [{"role": "user", "content":
                                                   f"Вопрос клиента: {client_question}\nОтвет менеджера: {manager_text}"}])
        tokens = getattr(llm, "last_tokens", 0) or (len(EXTRACT_PROMPT) + len(manager_text)) // 3
        facts = parse_facts(raw)
        if facts is None:
            raise ValueError("ответ модели не разобрать")
    except Exception:  # noqa: BLE001 — без модели кандидат всё равно полезен менеджеру
        return raw_facts(client_question, manager_text), tokens
    return [f for f in facts if keep(f, manager_text)], tokens


def review_note(kb: KnowledgeBase, question: str, answer: str) -> str:
    """Подсказка менеджеру при утверждении: противоречие прайсу или похожий ответ, который уже есть."""
    hits = kb.retrieve(question)
    amounts = set(extract_amounts(answer))
    words = set(tokenize(f"{question} {answer}"))
    named = kb._venues_in(f"{question} {answer}")
    rows = [kb.prices[int(h.chunk.id.split("-")[1])] for h in hits
            if h.chunk.kind == "price" and (not named or h.chunk.venue & named) and h.chunk.fmt & words]
    if amounts and rows and not amounts & {r.price_rub for r in rows}:
        listed = "; ".join(f"{r.format}, {r.placement.split(' — ')[0]} — {r.price_rub:,} ₽".replace(",", " ")
                           for r in rows[:3])
        return f"В прайсе другая цена: {listed}"
    asked = set(tokenize(question))
    for h in hits:
        if h.chunk.kind != "faq":
            continue
        title = h.chunk.text.split("\n", 1)[0].split("Вопрос: ", 1)[-1]
        if same_topic(asked, set(tokenize(title))):
            return f"Похожее уже есть в базе: {title}"
    return ""


def same_topic(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= SIMILARITY


def group(rows, kb: KnowledgeBase | None = None) -> list[list]:
    """Похожие кандидаты вместе: менеджер видит «клиенты спрашивали 4 раза» и утверждает один раз."""
    groups: list[tuple[set[str], list]] = []
    for row in rows:
        tokens = set(tokenize(row["question"]))
        for gtokens, items in groups:
            if kb is not None:
                scope = fact_scope(kb, row["question"], row["answer"])
                other = fact_scope(kb, items[0]["question"], items[0]["answer"])
                if any(a and b and a != b for a, b in zip(scope, other)):
                    continue
            if same_topic(tokens, gtokens):
                items.append(row)
                break
        else:
            groups.append((tokens, [row]))
    return sorted((items for _, items in groups), key=len, reverse=True)


def target_note(kb: KnowledgeBase, question: str, answer: str) -> tuple[str, str]:
    """(путь заметки внутри notes/, ТЦ). Один ТЦ в факте — его файл, иначе общий."""
    mentioned = kb._venues_in(f"{question} {answer}")
    heads = []
    for row in kb.prices:
        head = re.sub(r"\s*\(.*?\)", "", row.placement.split(" — ", 1)[0]).split(",")[0].strip()
        if venue_tokens(row.placement) & mentioned and head not in heads:
            heads.append(head)
    if len(heads) == 1:
        name = re.sub(r"[^\w .-]", "", heads[0]).strip() or "Общее"
        return f"{NOTES_FOLDER}/{name}.md", heads[0]
    return f"{NOTES_FOLDER}/Общее.md", ""


def add_to_note(path: Path, venue: str, question: str, answer: str) -> None:
    """Дописать раздел «## вопрос» в заметку (тот же вопрос — заменить ответ)."""
    question = question.strip().rstrip("?") + "?"
    section = f"## {question}\n{answer.strip()}\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(rf"^## {re.escape(question)}\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
        text = pattern.sub(lambda _: section + "\n", text, count=1) if pattern.search(text) \
            else text.rstrip("\n") + "\n\n" + section
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        head = f"тц: {venue}\n" if venue else ""
        title = venue or "общие вопросы"
        text = (f"---\n{head}источник: переписка с клиентами, утверждено менеджером\n---\n"
                f"# Из переписки: {title}\n\n{section}")
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
