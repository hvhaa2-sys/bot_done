"""Проверки конкретных утверждений: свойство своей площадки и период своей цены.

Проверяется ограниченный набор структурных фактов, не произвольная истинность текста.
Источник — только найденные документы, без прошлых ответов бота и слов клиента.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import KnowledgeBase, PriceRow, SearchHit, _CITY_TOKENS, parse_budget
from .price_guard import _NUMBER_RE, amount_spans, extract_amounts, extract_numbers
from .text import drop_courtesy, tokenize

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
    # «Добрый день, экран — 31 500 ₽» — в приветствии нет срока цены
    for match in _PERIOD.finditer(drop_courtesy(text)):
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
_PRICE_SEPARATOR = re.compile(r"[,;](?!\d)|\s+(?:и|или)\s+", re.I)
_BUDGET_LABEL = re.compile(r"(?:бюджет\w*|лимит\w*|потолок|располагаете)(?:\s+на\s+(?:размещение|рекламу|подготовку))?"
                           r"(?:\s+до)?[\s:—–-]*$", re.I)
_BUDGET_OFFER = re.compile(r"\s*(?:(?:в|на) месяц\s+)?(?:доступн|можно|подойд)|[\s—–-]*(?:ваш\s+)?(?:бюджет|лимит)", re.I)
_EXTRA = r"(?:печат|монтаж|демонтаж|изготовлен|производств|доплат)\w*"
_EXTRA_LABEL = re.compile(rf"\b{_EXTRA}[^,;:]{{0,35}}[:—–-]?\s*$", re.I)
_EXTRA_AFTER = re.compile(rf"[\s—–-]*(?:за |на ){_EXTRA}", re.I)
_UNKNOWN_TERM = re.compile(r"[\s(.,;—–-]*(?:срок|период)(?: размещения)?\s+(?:в прайсе\s+)?"
                           r"(?:не указан|неизвест|нужно уточнить)", re.I)
_PRICE_LABEL = re.compile(r"(?:[—–:-]|\bстоит|\bстоят|\bстоимость|\bцена)\s*$", re.I)
_FREQUENCY = re.compile(r"\b(?:(?:выход|показ)(?:а|ов|ы)?|раза?)\s+(?:в|за|на)\s*$", re.I)
_OTHER_UNIT = re.compile(r"\b(?:(\d+)\s+)?(час(?:а|ов)?|сезон(?:а|ов)?)\b", re.I)


def price_term_suffix(period: str) -> str:
    return f" за {period}" if period else " (срок в прайсе не указан)"


def _price_spans(text: str) -> list[tuple[int, int, int]]:
    spans = {}
    for amount, start, end in amount_spans(text):
        # Общий парсер сумм возвращает начало «руб» и для полного слова «рублей».
        ending = re.match(r"(?:лей|ля|ль)\b", text[end:], re.I)
        spans[start] = (amount, start, end + (ending.end() if ending else 0))
    for match in _NUMBER_RE.finditer(text):
        if match.start() in spans or not _PRICE_LABEL.search(text[:match.start()]):
            continue
        if re.match(r"\s*(?:$|[.,;!?)]|/|\b(?:в|за|на|при)\b)", text[match.end():], re.I):
            amount, = extract_numbers(match[0])
            if amount >= 1000:
                spans[match.start()] = (amount, match.start(), match.end())
    return sorted(spans.values(), key=lambda span: span[1])


def billing_periods(text: str) -> list[tuple[tuple[str, int], int, int]]:
    found = periods(text)
    for match in _OTHER_UNIT.finditer(text):
        unit = "hour" if match[2].lower().startswith("час") else "season"
        if unit == "season" and re.search(r"\bв\s+$", text[:match.start()], re.I) and re.match(
                r"\s*[-–—:]?\s*\d", text[match.end():]):
            continue  # «В сезон — 45 360 ₽» называет сезонную цену, не срок размещения.
        found.append(((unit, int(match[1] or 1)), match.start(), match.end()))
    return [(value, start, end) for value, start, end in sorted(found, key=lambda item: item[1])
            if not _FREQUENCY.search(text[:start])]


def _price_parts(sentence: str):
    """Не переносить ТЦ или срок через границу двух цен в одном предложении."""
    spans = _price_spans(sentence)
    start = 0
    for (_, _, end), (_, following, _) in zip(spans, spans[1:]):
        if separator := _PRICE_SEPARATOR.search(sentence, end, following):
            yield sentence[start:separator.start()]
            start = separator.end()
    yield sentence[start:]


def _scoped_parts(kb: KnowledgeBase, answer: str, current: Scope):
    """Фраза ответа и то, о чём она. Пункт списка наследует формат и место заголовка, а не соседнего пункта:
    после «- Вход: статичное перекрытие» баннер в следующем пункте считался перекрытием (GigaChat Pro, 30.10)."""
    heading = current
    for line in answer.split("\n"):
        item = _LIST_ITEM.match(line)
        if item:
            current = heading
        for sentence in _SENTENCES.split(line):
            for part in _price_parts(sentence):
                current = scope(kb, part, current)
                yield part, current
        if not item and line.strip():
            heading = current


def price_period_issues(kb: KnowledgeBase, answer: str, hits: list[SearchHit], question: str) -> list[int]:
    return check_price_periods(kb, answer, hits, question)[1]


def _billing_for_amount(part, mentioned, left, start, end, right):
    after = [(p, a) for p, a, _ in mentioned if end <= a < right]
    before = [(p, b) for p, _, b in mentioned if left <= b <= start]
    # В «на полгода — 18 000 ₽ за месяц» подпись после цены важнее срока кампании перед ней.
    for period, at in after:
        if not re.search(r"[,;]", part[end:at]):
            return period
    for period, at in reversed(before):
        if re.fullmatch(r"\s*[-–—:]\s*", part[at:start]):
            return period
    return after[0][0] if after else before[-1][0] if before else None


def _placement_terms(rows: list[PriceRow], amount: int) -> set[str]:
    terms = {row.period for row in rows if row.price_rub == amount}
    for row in rows:
        for note in row.parts()["season"]:
            for part in _price_parts(note):
                if amount not in extract_amounts(part):
                    continue
                term = row.period
                if quoted := billing_periods(part):
                    unit, start, end = quoted[0]
                    base = billing_periods(row.period)
                    if not base or unit != base[0][0]:
                        term = part[start:end]
                terms.add(term)
    return terms


def check_price_periods(kb: KnowledgeBase, answer: str, hits: list[SearchHit], question: str) -> tuple[str, list[int]]:
    """Проверить названный срок и подписать пропущенный у каждой цены размещения."""
    rows = [kb.prices[int(h.chunk.id.split("-")[1])] for h in hits if h.chunk.kind == "price"]
    if not rows:
        return answer, []
    budget = parse_budget(question.lower())
    insertions = []
    bad = []
    offset = 0
    for part, current in _scoped_parts(kb, answer, scope(kb, question)):
        pos = answer.index(part, offset)
        offset = pos + len(part)
        spans = _price_spans(part)
        mentioned = billing_periods(part)
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
            prefix, tail = part[left:start], part[end:right]
            if _BUDGET_LABEL.search(prefix) or (amount == budget and
                    (re.search(r"\bдо\s*$", prefix, re.I) or _BUDGET_OFFER.match(tail))):
                continue  # повторение бюджета клиента не является ценой услуги
            # Чужой формат с такой же суммой не подтверждает цену запрошенного экрана.
            candidates = [r for r in rows if amount == r.price_rub or amount in extract_amounts(r.notes)]
            matching = [r for r in candidates if compatible(current, scope(kb, f"{r.format}, {r.placement}"), price=True)]
            if not matching:
                if candidates:
                    bad.append(amount)
                continue
            if (_EXTRA_LABEL.search(prefix) or _EXTRA_AFTER.match(tail)) and any(
                    amount in extract_amounts(note) for r in matching for note in r.parts()["extra"]):
                continue
            terms = _placement_terms(matching, amount)
            if not terms:  # Только сумма из примечания, например печать за один флаг.
                continue
            if billing := _billing_for_amount(part, mentioned, left, start, end, right):
                if not any(quoted and billing == quoted[0][0] for quoted in map(billing_periods, terms)):
                    bad.append(amount)
            elif _UNKNOWN_TERM.match(answer, pos + end):
                if len(terms) == 1 and "" not in terms:
                    bad.append(amount)
            elif not any(term and term.casefold() in part.casefold() for term in terms):
                suffix = price_term_suffix(next(iter(terms))) if len(terms) == 1 else " (срок нужно уточнить)"
                insertions.append((pos + end, suffix))
    for end, suffix in reversed(insertions):
        answer = answer[:end] + suffix + answer[end:]
    return answer, sorted(set(bad))


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
