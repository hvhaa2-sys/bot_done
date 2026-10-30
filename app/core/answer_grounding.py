"""Проверки конкретных утверждений: свойство своей площадки и период своей цены.

Проверяется ограниченный набор структурных фактов, не произвольная истинность текста.
Источник — только найденные документы, без прошлых ответов бота и слов клиента.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import KnowledgeBase, SearchHit, _CITY_TOKENS, parse_budget
from .price_guard import amount_spans, extract_amounts
from .text import tokenize

_SENTENCES = re.compile(r"(?<!руб\.)(?<!р\.)(?<!сек\.)(?<!мес\.)(?<=[.!?])\s+|\n+", re.I)
_UNKNOWN = re.compile(r"не (?:указан|извест|подтвержд)|нет (?:данных|информации)|не могу (?:подтверд|сказа)|"
                      r"нужно (?:уточнить|проверить)|уточню|неизвест", re.I)
_SOUND_NO = re.compile(r"без звука|звука нет|звук\w*[^.!?]{0,18}(?:не воспроизвод|не транслир|выключ|отсутств)|"
                       r"не (?:поддержива|воспроизвод)\w*[^.!?]{0,12}звук", re.I)
_SOUND_YES = re.compile(r"со звуком|звук\w*[^.!?]{0,18}(?:есть|включ|воспроизвод|присутств|будет|слыш)|"
                        r"(?:есть|будет)\s+звук|слыш\w*\s+(?:речь|озвучк)", re.I)
_DURATION = re.compile(r"(\d{1,3})\s*(?:секунд\w*|сек\.?)(?!\w)", re.I)
_RESOLUTION = re.compile(r"(\d{3,4})\s*[×xх]\s*(\d{3,4})", re.I)
_ORIENTATION = re.compile(r"горизонталь\w*|вертикаль\w*", re.I)
_FAMILIES = (
    ("cube", re.compile(r"медиа[ -]?куб", re.I)),
    ("screen", re.compile(r"экран|infodesk|инфодеск", re.I)),
    ("navigation", re.compile(r"навигацион\w* (?:стой|термин)|видео.*терминал", re.I)),
    ("radio", re.compile(r"радио|аудиоролик", re.I)),
    ("promo", re.compile(r"промо[ -]?акци|промоутер", re.I)),
    ("banner", re.compile(r"баннер|брендирован|лайтбокс|светов\w* короб", re.I)),
)
_PLACE_SKIP = set(tokenize("центральный главный этаж место размещение экран инцентр выход показ сторона шт медиа куб"))
_PRIMARY_PLACES = set(tokenize("атриум вход фуд корт парковка фасад эскалатор галерея витрина"))
_FMT_SKIP = set(tokenize("цифровой рекламный размещение реклама ролик видеоролик видео макет стоимость цена "
                         "печатный печать монтаж производство изготовление для арендатор месяц день неделя "
                         # Слова из названий строк, которые о формате ничего не говорят и уводили проверку к чужому
                         # формату (GigaChat Pro, 28.10): «оплачиваются дополнительно» («Дополнительный пост охраны»),
                         # «печать одного флага» («…, одна сторона» у тейбл-тентов), «1,45×2,4 м» (метры в размерах),
                         # «за 5 граней» («колонна, 4 грани»), «полный месяц» (срок, а не формат)
                         "дополнительный один одна одно одного одной одну м см мм шт штука грань грани граней полный "
                         "например"))
# (?<!20\d\d\s): «по 28 февраля 2027 года» — дата, а не срок «год»
_PERIOD = re.compile(r"(?<!\w)(?<!20\d\d\s)(?:(\d{1,2}|один|одна|одну|два|две|три|четыре|пять|шесть)\s*)?"
                     r"(месяц\w*|мес\.?|недел\w*|день|дня|дней|сут\w*|год\w*)\b|полгода|пол года|квартал", re.I)
_COUNTS = {"один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6}
_PRORATE = re.compile(r"(?:будет|выйдет|обойдется|получится)\s+(?:меньше|дешевле)|"
                       r"пропорциональн|в (?:два|2) раза (?:меньше|дешевле)|"
                       r"половин\w*(?:\s+\w+){0,3}\s+(?:цен|стоимост|сумм)\w*|"
                       r"(?:цен|стоимост|сумм)\w*[^.!?]{0,25}пополам", re.I)
_BUDGET_FIT = re.compile(r"(?:в рамках|в пределах|под|ваш\w*)[^.!?]{0,35}бюджет|укладыва|вписыва|"
                         r"(?:за|на)\s*\d[\d\s]*(?:₽|руб\w*)[^.!?]{0,45}возможн", re.I)
_OVER_BUDGET = re.compile(r"не (?:укладыва|вписыва)|превыш|выше (?:бюджет|лимит)|недостат", re.I)


def families(text: str) -> frozenset[str]:
    return frozenset(name for name, pattern in _FAMILIES if pattern.search(text))


@dataclass(frozen=True)
class Scope:
    venues: frozenset[str] = frozenset()
    formats: frozenset[str] = frozenset()
    places: frozenset[str] = frozenset()
    format_words: frozenset[str] = frozenset()


_LIGHTBOX = frozenset(tokenize("лайтбокс короб"))
# «Не входят в цену», «входит в стоимость» — глагол, а основа та же, что у места «вход»: без этого доплаты
# к коробу над банкоматами проверялись как цены у входа (GigaChat Pro, 28.10)
_ENTER_VERB = re.compile(r"\bвход(?:ит|ят|ить|ил[аио]?)\b", re.I)


def scope(kb: KnowledgeBase, text: str, previous: Scope = Scope()) -> Scope:
    text = _ENTER_VERB.sub(" ", text)
    venues = kb._venues_in(text) - _CITY_TOKENS
    formats = families(text)
    # «на входе» и «входная группа» — одно место, хотя основы разные («вход» и «входн»)
    places = frozenset("вход" if p.startswith("вход") else p
                       for p in frozenset(kb.normalize(text)) & kb._place_tokens - _PLACE_SKIP)
    format_words = frozenset(w for w in kb.normalize(text) if not w.isdigit()) & kb._format_tokens \
        - _FMT_SKIP - kb._place_tokens - kb._all_venues
    # «Галереи» → «галер», «в галереях» → «галере»: разные основы одного места — это место, а не формат
    format_words = frozenset(w for w in format_words if len(w) < 5 or not any(
        p.startswith(w) or w.startswith(p) for p in kb._place_tokens if len(p) >= 5))
    if format_words & _LIGHTBOX:  # «световой короб с тканевым полотном» и «лайтбокс» — один формат
        format_words |= _LIGHTBOX
    changed = bool(venues and venues != previous.venues)
    if places and not places & _PRIMARY_PLACES and not changed:
        places |= previous.places  # «фуд-корт, 2 уровень, у лифтов» остаётся одной площадкой
    return Scope(venues or previous.venues, formats or (frozenset() if changed else previous.formats),
                 places or (frozenset() if changed else previous.places),
                 format_words or (frozenset() if changed else previous.format_words))


def compatible(wanted: Scope, evidence: Scope, *, price: bool = False) -> bool:
    pairs = [(wanted.venues, evidence.venues), (wanted.formats, evidence.formats), (wanted.places, evidence.places)]
    if price:
        pairs.append((wanted.format_words, evidence.format_words))
    return all(not a or not b or bool(a & b) for a, b in pairs)


def clauses(kb: KnowledgeBase, text: str, current: Scope):
    for sentence in _SENTENCES.split(text):
        # Название ТЦ может идти после утверждения: «экран со звуком, расположен в Бутово».
        current = scope(kb, sentence, current)
        for part in re.split(r";|,(?!\d)", sentence):
            current = scope(kb, part, current)
            yield part, current


def properties(text: str, subject: str = "") -> set[tuple[str, object]]:
    if _UNKNOWN.search(text) or text.strip().endswith("?"):
        return set()
    out: set[tuple[str, object]] = set()
    if _SOUND_NO.search(text):
        out.add(("звук", False))
    elif _SOUND_YES.search(text):
        out.add(("звук", True))
    if re.search(r"ролик|видео|длитель|хронометр", text + " " + subject, re.I):
        out |= {("длительность", int(m.group(1))) for m in _DURATION.finditer(text)}
    out |= {("разрешение", tuple(map(int, m.groups()))) for m in _RESOLUTION.finditer(text)
            if not re.match(r"\s*(?:мм|см|м)(?!\w)", text[m.end():], re.I)}
    for m in _ORIENTATION.finditer(text):
        before = text[max(0, m.start() - 8):m.start()].lower()
        if not re.search(r"\bне\s*$", before):
            out.add(("ориентация", "горизонтальная" if m.group().lower().startswith("гориз") else "вертикальная"))
    return out


def property_issues(kb: KnowledgeBase, answer: str, hits: list[SearchHit], question: str) -> list[str]:
    evidence: list[tuple[Scope, tuple[str, object]]] = []
    for hit in hits:
        chunk = hit.chunk
        if chunk.kind == "catalog":
            # «Все форматы — ДЕПО: Цифровой экран со звуком»: название формата, остальные свойства каталог не подтверждает
            if chunk.venue and "со звуком" in chunk.text.lower():
                evidence.append((Scope(chunk.venue - _CITY_TOKENS), ("звук", True)))
            continue
        title, sep, body = chunk.text.partition("\nОтвет: ")
        current = scope(kb, title if sep else "")
        if chunk.venue:
            current = Scope(chunk.venue - _CITY_TOKENS, current.formats, current.places, current.format_words)
        if chunk.kind == "price":
            # Строка прайса — одно место целиком: «траволатор, вход в ОКЕЙ … ролик 10 сек» — про траволатор тоже.
            # По кусочкам через запятую к длительности относился только «вход» (GigaChat Pro, 28.10)
            evidence += [(scope(kb, chunk.text), value) for value in properties(chunk.text)]
            continue
        for part, current in clauses(kb, body if sep else chunk.text, current):
            evidence += [(current, value) for value in properties(part, title if sep else "")]
    issues = []
    current = scope(kb, question)
    for part, current in clauses(kb, answer, current):
        for value in properties(part, question):
            if not any(value == known and compatible(current, source) for source, known in evidence):
                issues.append(value[0])
    return sorted(set(issues))


def periods(text: str) -> list[tuple[tuple[str, int], int, int]]:
    out = []
    for match in _PERIOD.finditer(text):
        raw, unit = match.group().lower(), (match.group(2) or "").lower()
        if raw.startswith(("полгода", "пол года", "квартал")):
            value = ("month", 3 if raw == "квартал" else 6)
        else:
            number = match.group(1) or "1"
            count = int(number) if number.isdigit() else _COUNTS[number]
            value = ("month", count) if unit.startswith("мес") else ("month", count * 12) \
                if unit.startswith("год") else ("day", count * 7) if unit.startswith("нед") else ("day", count)
        out.append((value, match.start(), match.end()))
    return out


_LIST_ITEM = re.compile(r"\s*(?:[-–—•*]|\d+[.)])\s")


def _scoped_parts(kb: KnowledgeBase, answer: str, current: Scope):
    """Фраза ответа и то, о чём она. Пункт списка наследует формат и место заголовка, а не соседнего пункта:
    после «- Вход: статичное перекрытие» баннер в следующем пункте считался перекрытием (GigaChat Pro, 30.10)."""
    heading = current
    for line in answer.split("\n"):
        item = _LIST_ITEM.match(line)
        if item:
            current = heading
        for part in _SENTENCES.split(line):
            current = scope(kb, part, current)
            yield part, current
        if not item and line.strip():
            heading = current


def price_period_issues(kb: KnowledgeBase, answer: str, hits: list[SearchHit], question: str) -> list[int]:
    rows = [kb.prices[int(h.chunk.id.split("-")[1])] for h in hits if h.chunk.kind == "price"]
    if not rows:
        return []
    budget = parse_budget(question.lower())  # бюджет клиента, повторённый в ответе, — не цена формата
    bad = []
    for part, current in _scoped_parts(kb, answer, scope(kb, question)):
        spans = amount_spans(part)
        mentioned = periods(part)
        if _PRORATE.search(part):
            requested = {p for p, _, _ in mentioned}
            fitting = [r for r in rows if compatible(current, scope(kb, f"{r.format}, {r.placement}"), price=True)]
            available = {periods(r.period)[0][0] for r in fitting if periods(r.period)}
            fractional = re.search(r"половин|пополам", part, re.I)
            if fractional or requested - available:
                bad += [amount for amount, _, _ in spans] or [r.price_rub for r in fitting[:1]]
        for i, (amount, start, end) in enumerate(spans):
            right = spans[i + 1][1] if i + 1 < len(spans) else len(part)
            left = spans[i - 1][2] if i else 0
            if amount == budget or re.search(r"(?:бюджет|лимит|потолок|располагаете)[^,;:.]{0,40}$",
                                             part[max(left, start - 60):start], re.I):
                continue  # повторение бюджета клиента не является ценой услуги
            after = [(p, a - end) for p, a, b in mentioned if end <= a < right]
            before = [(p, start - b) for p, a, b in mentioned if left <= b <= start]
            # «день — 6 000 ₽, неделя — 18 000 ₽»: срок подписью перед ценой относится к ней, а не к следующей.
            # Но «на полгода — 18 000 ₽ за месяц»: срок сразу после цены в той же части фразы важнее подписи
            near = [x for x in after if not re.search(r"[,;]", part[end:end + x[1]])]
            label = [p for p, a, b in mentioned if left <= b <= start and re.fullmatch(r"\s*[-–—:]\s*", part[b:start])]
            billing = min(near, key=lambda x: x[1])[0] if near else label[0] if label else \
                min(after, key=lambda x: x[1])[0] if after else min(before, key=lambda x: x[1])[0] if before else None
            # Чужой формат с такой же суммой не подтверждает цену запрошенного экрана.
            candidates = [r for r in rows if amount == r.price_rub or amount in extract_amounts(r.notes)]
            matching = [r for r in candidates if compatible(current, scope(kb, f"{r.format}, {r.placement}"), price=True)]
            if candidates and not matching:
                bad.append(amount)
            elif billing and matching:
                main = [r for r in matching if amount == r.price_rub]
                # Печать и монтаж разовые: их проверяет существующая защита дополнительных расходов.
                if main and not any(periods(r.period) and billing == periods(r.period)[0][0] for r in main):
                    bad.append(amount)
    return sorted(set(bad))


def budget_issues(kb: KnowledgeBase, answer: str, hits: list[SearchHit], *, total: int | None,
                  placement: int | None, preparation: int | None) -> str:
    """Подтверждённые строки нельзя обещать в бюджет, если известные расходы выше лимита.

    Считаем только для внутренней проверки. Новый итог в ответ клиенту не генерируем.
    Честный перечень цен или явное предупреждение о превышении не являются обещанием.
    """
    if not any((total, placement, preparation)):
        return ""
    positive = [s for s in _SENTENCES.split(answer) if _BUDGET_FIT.search(s) and not _OVER_BUDGET.search(s)]
    if not positive:
        return ""
    amounts = set(extract_amounts(answer))
    wanted = scope(kb, answer)
    grouped = {}
    for hit in hits:
        if hit.chunk.kind == "price":
            row = kb.prices[int(hit.chunk.id.split("-")[1])]
            if row.price_rub in amounts and compatible(wanted, scope(kb, f"{row.format}, {row.placement}"), price=True):
                grouped.setdefault(row.price_rub, []).append(row)
    for rows in grouped.values():
        failures = []
        for row in rows:
            costs = row.extras_total()
            failures.append("общий бюджет" if total and row.price_rub + costs > total else
                            "бюджет размещения" if placement and row.price_rub > placement else
                            "бюджет подготовки" if preparation and costs > preparation else "")
        if all(failures):
            return failures[0]
    return ""
