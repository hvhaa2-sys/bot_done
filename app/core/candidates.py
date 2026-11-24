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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .candidate_review import fact_scope
from .knowledge import BUSINESS_NOTE, LEARNED_NOTES, KnowledgeBase, parse_faq, venue_tokens
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
RETRY_TEXT = 80         # ответ менеджера, ради которого стоит второй раз спросить модель
# Личное и разовое: это решает менеджер в каждом случае, в базу не переносим
_PERSONAL_RE = re.compile(r"скидк|бронь|брониру|забронир|свободн|занят|перезвон|пришлю|пришлём|уточню"
                          r"|договор(?!\w* с РАО)|счёт|счет\b|оплатит|реквизит|ваш\w* заказ", re.IGNORECASE)
# Сумма, посчитанная этому клиенту: «Для вас по трём экранам итого 189 000 ₽» — не прайс, а расчёт
_FOR_CLIENT_RE = re.compile(r"\b(?:итог\w*|для вас|вам|ваш\w*)\b", re.IGNORECASE)
# Обещание этому клиенту — отдельная фраза, которую можно убрать, не трогая общий факт рядом:
# «Экраны без звука. Пришлю вам примеры роликов.» → «Экраны без звука.»
_PROMISE_RE = re.compile(r"\b(?:пришл(?:ю|ём|ем)|перезвон\w*|уточн(?:ю|им)|напишу|скину|отправл(?:ю|им)|вышл(?:ю|ем))\b",
                         re.IGNORECASE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
_PD_MARK_RE = re.compile(r"\[(?:телефон|почта|карта)\]")
_GREETING_RE = re.compile(r"(?:здравствуйте|здрасьте|добрый (?:день|вечер)|доброе утро|привет\w*|спасибо(?: за вопрос)?)"
                          r"\b[!,.\s]*", re.IGNORECASE)
_STRING = r'"((?:[^"\\]|\\.)*)"'
_PAIR_RE = re.compile(r'\{\s*"question"\s*:\s*' + _STRING + r'\s*,\s*"answer"\s*:\s*' + _STRING + r"\s*\}")
# Лишняя кавычка внутри строки: «"answer": "Экраны без звука." Если нужен звук, …"}» — поле до «"}»
_LOOSE_PAIR_RE = re.compile(r'"question"\s*:\s*"(.*?)"\s*,\s*"answer"\s*:\s*"(.*?)"\s*\}', re.DOTALL)
SIMILARITY = 0.45
_COMMON_WORDS = frozenset({"молл", "тц", "трц", "торгов", "центр", "размест", "нужн", "подскаж"})
NOTES_FOLDER = LEARNED_NOTES
CASE_PREFIX = "[Только заявка] "


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
        strict = [(json.loads(f'"{q}"'), json.loads(f'"{a}"')) for q, a in _PAIR_RE.findall(match.group(0))]
        loose = [(_loose(q), _loose(a)) for q, a in _LOOSE_PAIR_RE.findall(match.group(0))]
        return joined([Fact(" ".join(q.split()), " ".join(a.split())) for q, a in max(strict, loose, key=len)]) or None
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    facts = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and isinstance(item.get("answer"), str):
            facts.append(Fact(" ".join(item["question"].split()), " ".join(item["answer"].split())))
    return joined(facts)


def joined(facts: list[Fact]) -> list[Fact]:
    """Продолжение без вопроса — к предыдущему факту: GigaChat делит один ответ на два,
    у второго {"question": ""} («Нет, на тележках нет.» + «Из похожего есть талоны…»)."""
    out: list[Fact] = []
    for f in facts:
        if not f.question and out:
            out[-1] = Fact(out[-1].question, f"{out[-1].answer} {f.answer}".strip())
        else:
            out.append(f)
    return out[:MAX_FACTS]


def _loose(text: str) -> str:
    """Поле с неэкранированной кавычкой внутри: кавычку убираем, остальное как в JSON."""
    text = re.sub(r'(?<!\\)"', "", text)
    try:
        return json.loads(f'"{text}"')
    except json.JSONDecodeError:
        return text.replace('\\"', '"')


def drop_promises(text: str) -> str:
    """Убрать фразы-обещания этому клиенту («Пришлю примеры.»): общий факт рядом остаётся."""
    return without_sentences(text, _PROMISE_RE.search) if _PROMISE_RE.search(text) else text


def without_sentences(text: str, drop) -> str:
    """Текст без фраз, для которых drop(фраза) истинно. Разделители остаются на месте: «1.5 м» не рвётся."""
    out, last = [], 0
    for m in _SENTENCE_RE.finditer(text):
        out.append(text[last:m.start()])
        if not drop(m.group()):
            out.append(m.group())
        last = m.end()
    return " ".join("".join(out + [text[last:]]).split())


def keep(fact: Fact, manager_text: str) -> bool:
    """Факт годится в кандидаты: не пустой, не личный и без сумм, которых не было у менеджера
    или которые он посчитал этому клиенту («для вас итого 189 000 ₽»)."""
    if len(fact.answer) < MIN_ANSWER or not fact.question:
        return False
    if _PERSONAL_RE.search(fact.answer) or _PERSONAL_RE.search(fact.question):
        return False
    amounts = set(extract_amounts(fact.answer))
    if any(amounts & set(extract_amounts(s)) and _FOR_CLIENT_RE.search(s) for s in _SENTENCE_RE.findall(manager_text)):
        return False
    return amounts <= set(extract_amounts(manager_text))


def raw_facts(client_question: str, manager_text: str) -> list[Fact]:
    """Без модели: пара «вопрос клиента → ответ менеджера», если она похожа на факт. Приветствия
    и фразы клиента со своими контактами («Мой номер [телефон]») в базу не переносим."""
    client_question, manager_text = mask_pd(client_question), mask_pd(manager_text)
    fact = Fact(tidy(client_question, drop_contacts=True), drop_promises(tidy(manager_text)))
    return [fact] if len(fact.answer) >= MIN_MANAGER_TEXT and keep(fact, manager_text) else []


def tidy(text: str, drop_contacts: bool = False) -> str:
    """«Здравствуйте! Какой длины ролик? Мой номер [телефон]» → «Какой длины ролик?»."""
    out = without_sentences(text, lambda s: drop_contacts and bool(_PD_MARK_RE.search(s)))
    while (m := _GREETING_RE.match(out)) and m.end():
        out = out[m.end():]
    return out  # одно приветствие — пустая строка: такой пары в базу нет


async def extract(llm, client_question: str, manager_text: str) -> tuple[list[Fact], int]:
    """(факты, токены). Без модели или при сбое — пара «вопрос клиента → ответ менеджера» как есть."""
    client_question, manager_text = mask_pd(client_question), mask_pd(manager_text)
    if len(manager_text) < MIN_MANAGER_TEXT:
        return [], 0
    tokens = 0
    prompt = [{"role": "user", "content": f"Вопрос клиента: {client_question}\nОтвет менеджера: {manager_text}"}]
    try:
        facts = None
        for _ in range(2):
            raw = await llm.complete(EXTRACT_PROMPT, prompt)
            tokens += getattr(llm, "last_tokens", 0) or (len(EXTRACT_PROMPT) + len(manager_text)) // 3
            facts = parse_facts(raw)
            if facts is None:
                raise ValueError("ответ модели не разобрать")
            facts = [f for f in (Fact(f.question, drop_promises(f.answer)) for f in facts) if keep(f, manager_text)]
            # GigaChat на том же ответе иногда не даёт ни одного годного факта (в прогоне песочницы 04.11 пропали 2 из 3):
            # содержательный ответ без личного спрашиваем ещё раз, короткое «получили, ставим» — нет
            if facts or sum(len(f.answer) for f in raw_facts(client_question, manager_text)) < RETRY_TEXT:
                break
    except Exception:  # noqa: BLE001 — без модели кандидат всё равно полезен менеджеру
        return raw_facts(client_question, manager_text), tokens
    return facts, tokens


def review_note(kb: KnowledgeBase, question: str, answer: str) -> str:
    """Подсказка менеджеру при утверждении: противоречие прайсу или похожий ответ, который уже есть."""
    if conflict := price_conflict(kb, question, answer):
        return conflict
    asked = topic_words(kb, question)
    for h in kb.retrieve(question):
        if h.chunk.kind != "faq":
            continue
        title = h.chunk.text.split("\n", 1)[0].split("Вопрос: ", 1)[-1]
        if same_topic(asked, topic_words(kb, title)):
            return f"Похожее уже есть в базе: {title}"
    return ""


def price_conflict(kb: KnowledgeBase, question: str, answer: str) -> str:
    """«В прайсе другая цена: …» — в ответе сумма, а у того же ТЦ и формата в прайсе другие. Такой факт
    не утверждается: та же сумма может быть у другой позиции прайса (12 000 ₽ — не цена фотозоны)."""
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
    return ""


def topic_words(kb: KnowledgeBase, text: str) -> set[str]:
    """Слова темы без ТЦ и общих слов: «реклама на тележках в ВЕЕР» не похожа на «вакансию в ВЕЕР»."""
    return set(tokenize(text)) - kb._all_venues - _COMMON_WORDS


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


def venue_names(kb: KnowledgeBase) -> list[str]:
    heads = set()
    for row in kb.prices:
        head = re.sub(r"\s*\(.*?\)", "", row.placement.split(" — ", 1)[0]).split(",")[0].strip()
        if head:
            heads.add(head)
    return sorted(heads)


def target_note(kb: KnowledgeBase, question: str, answer: str, scope: str = "") -> tuple[str, str]:
    """Возвращает путь, только если область действия известна."""
    names = venue_names(kb)
    mentioned = kb._venues_in(f"{question} {answer}")
    heads = [head for head in names if venue_tokens(head) & mentioned]
    if scope == "business" and not heads:
        return f"{NOTES_FOLDER}/{BUSINESS_NOTE}", ""
    if scope:
        heads = [scope] if scope in names and (not heads or heads == [scope]) else []
    if len(heads) == 1:
        name = re.sub(r"[^\w .-]", "", heads[0]).strip(" .")
        if name and name not in ("Общее", Path(BUSINESS_NOTE).stem):
            return f"{NOTES_FOLDER}/{name}.md", heads[0]
    return "", ""


def note_entries(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    return [(c.title, c.text.partition("\nОтвет: ")[2]) for c in parse_faq(path.read_text(encoding="utf-8"))]


def write_note(path: Path, text: str) -> None:
    """Оборванная запись не должна портить остальные утверждённые ответы."""
    out = tempfile.NamedTemporaryFile(dir=path.parent, encoding="utf-8", mode="w", delete=False)
    tmp = Path(out.name)
    try:
        with out:
            out.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


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
    write_note(path, text.rstrip("\n") + "\n")
