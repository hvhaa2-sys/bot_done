"""Параметры запроса из слов клиента; без цен и предположений модели."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .client_intent import active_request
from .knowledge import (KnowledgeBase, SearchHit, _CITY_NAMES, _CITY_RE, _CITY_TOKENS, _NOT_TENANT_RE,
                        _VENUE_ALIASES, canonical_query, format_rub, parse_budget, position_ranges, venue_tokens)
from .text import drop_courtesy, from_translit, small_talk, stem

MAX_CLARIFICATIONS = 2
CLARIFICATIONS = {
    "position": "Подскажите название или место размещения нужного варианта — так я назову точную цену.",
    "venue": "В каком торговом центре рассматриваете размещение?",
    "format": "Какой формат рекламы вас интересует?",
    "period": "На какой срок планируете размещение?",
    "other_placement": "Какое второе размещение сравнить? Подскажите площадку, формат и срок.",
    "missing": "Подскажите название формата или место размещения, которое вас интересует.",
    "budget": "Какой бюджет на всё и сколько из него на размещение и подготовку макета?",
}
_WORDS = re.compile(r"[а-яёa-z0-9-]+", re.IGNORECASE)
_PRICE = re.compile(r"сколько\s+(?:это\s+)?сто|стоимост|\bцен(?:а|ы|у|е|ой|ник|ов)\w*|поч[её]м|прайс|расцен|тариф", re.IGNORECASE)
_REFERS = re.compile(r"^\s*(?:а\b|и\b|ну\b|на\b)|\b(?:этот|эта|это|там|него|неё|нее|вариант|тот же)\b", re.I)
_ORDINAL = re.compile(r"\b(?:первый|второй|третий|последний)\s+вариант\b", re.I)
_RESET = re.compile(r"забуд\w*|начн[её]м сначала|новая тема|другая тема", re.I)
_NEGATED = re.compile(r"\bне\s+(?!(?:подскаж\w*|зна\w*|помн\w*|дороже|больше|меньше|менее|более)\b)"
                      r"[^.!?;]+?(?:,?\s+(?:а|лучше|давайте|тогда)\s+|(?=[.!?;]|$))", re.I)
_PERIOD = re.compile(r"\b(?:на\s+)?(?:(?:\d{1,2}|один|два|две|три|четыре|пять|шесть|пару)\s+)?"
                     r"(?:месяц\w*|недел\w*|день|дня|дней|сутк\w*|полгода|год\w*)\b", re.I)
_SEASON = re.compile(r"\b(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йея]|июн\w*|июл\w*|август\w*|"
                     r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b", re.I)
_OTHER_VENUES = re.compile(r"\b(?:друг\w*|вс(?:е|ех|ем|еми)|люб\w*|еще)\s+т[р]?ц\b|\bкакие\s+еще\s+т[р]?ц\b", re.I)
_BUSINESS = re.compile(r"реклам|для\s+(?:магазин|бизнес|компани|салон)|\bнаш\w*\s+(?:магазин|бизнес|компани|салон)", re.I)
_GENERAL_PRICE = re.compile(r"\bндс\b|налог|вход(?:ит|ят)|включен|какие|прайс|расценки|вариант|перечисл|подбер|подбор", re.I)
_FORMAT_SELECTION_WORDS = {stem(w) for w in (
    "вариант предложение обычный нормальный самый дешевый дорогой недорогой доступный бюджетный "
    "один одна одно одну два две три четыре пять шесть семь восемь девять десять "
    "первый второй третий четвертый пятый последний").split()}
_FORMAT_SKIP = {stem(w) for w in ("цифровой рекламный реклама внешний размещение арендатор тц трц молл "
                                "цена стоимость ценник магазин рекламодатель материал часть например шт час").split()}
_FORMAT_SKIP |= _FORMAT_SELECTION_WORDS
# «бюджет 30 тыс нужен экран» — деньги, а не формат, хотя в прайсе есть «партия 40 тыс. шт.»
_FORMAT_SKIP |= {stem(w) for w in ("тыс тысяча тыща тыщ млн миллион млрд миллиард лям косарь руб рубль "
                                   "рублей").split()}
_FORMAT_MODIFIER = re.compile(r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ом|ем|ую|юю|ых|их|ыми|ими)$", re.I)
_SELECTION = re.compile(r"\b(?:вариант\w*|предложен\w*|подбор\w*|дешев\w*|недорог\w*|доступн\w*|бюджетн\w*)", re.I)
_EXTRAS = {stem(w) for w in "печать монтаж производство дизайн макет".split()}
_PLACE_SKIP = {stem(w) for w in "экран высота место вид сторона направление".split()}
_LOCATIVE = re.compile(r"\b(?:в|во|на|у|возле|около|над|под|перед|вдоль)\s+$", re.I)
_FOCUS = ((re.compile(r"печат|напечата", re.I), "печать"),
          (re.compile(r"монтаж|смонтир", re.I), "монтаж"),
          (re.compile(r"требован|макет|ролик|видео", re.I), "требования к макету ролику"),
          (re.compile(r"проходим|посещаем|трафик|посетител|народу", re.I), "посещаемость трафик"),
          (re.compile(r"минимальн\w* срок|от скольки|минимум", re.I), "минимальный срок"),
          (re.compile(r"размер|высот|ширин", re.I), "размеры"),
          (re.compile(r"выход\w* в|показ\w*|как часто|секунд", re.I), "показы ролика"))


def _selected_formats(kb: KnowledgeBase, text: str, format_tokens: set[str], known: bool = False) -> list[str]:
    # Семейство задаёт существительное. Прилагательное уточняет соседнее семейство,
    # если они вместе встречаются в прайсе: «полный брендинг», но не «полный ценник».
    words = list(_WORDS.finditer(text))
    candidates = {i: set(kb.normalize(m[0])) & format_tokens for i, m in enumerate(words)
                  if len(m[0]) > 2 and not m[0].isdigit() and stem(m[0]) not in _FORMAT_SKIP}
    candidates = {i: tokens for i, tokens in candidates.items() if tokens}
    # «Экран в атриуме»: атриум есть в названии «Флаг в атриуме», но с экраном в прайсе не встречается — это место.
    # «Брендинг катка» — формат: в прайсе есть «Брендирование бортов катка».
    places = kb._place_tokens - _PLACE_SKIP
    placed = {i for i, tokens in candidates.items() if tokens <= places and not any(
        chunk.kind == "price" and chunk.fmt & tokens and chunk.fmt & candidates[j]
        for j in candidates if j != i for chunk in kb.chunks)}
    if not candidates.keys() - placed:
        # Формат назван только словами мест: место — слово после предлога («медиа-куб у эскалаторов»),
        # а «реклама в лифтах» без известного формата остаётся форматом. Известный формат «а в атриуме?» не заменяет.
        placed = {i for i in placed if _LOCATIVE.search(text[:words[i].start()])}
    if known or candidates.keys() - placed:
        candidates = {i: tokens for i, tokens in candidates.items() if i not in placed}
    modifiers = {i for i in candidates if _FORMAT_MODIFIER.search(words[i][0])
                 and not re.search(r"(?:[ае]ние|тие)$", words[i][0])}
    anchors = candidates.keys() - modifiers
    selected = set(anchors)
    for i in modifiers:
        for j in (i - 1, i + 1):
            if j not in anchors or text[min(words[i].end(), words[j].end()):
                                       max(words[i].start(), words[j].start())].strip():
                continue
            if any(chunk.kind == "price" and chunk.fmt & candidates[i] and chunk.fmt & candidates[j]
                   for chunk in kb.chunks):
                selected.add(i)
                break
    found = list(dict.fromkeys(words[i][0] for i in sorted(selected)))
    if any(stem(w) == stem("экран") for w in found):
        found = [w for w in found if w != "infodesk"]
    return found


def _format_search_text(kb: KnowledgeBase, text: str, format_tokens: set[str], selected: list[str]) -> str:
    # Иначе отброшенное «одну» всё ещё поднимает «одна сторона» через сырой запрос.
    protected = [(m.start(), m.end()) for pattern in (_PERIOD, _SEASON, _ORDINAL)
                 for m in pattern.finditer(text)]

    def replace(match: re.Match) -> str:
        word = match[0]
        if word in selected or any(start <= match.start() < end for start, end in protected):
            return word
        tokens = set(kb.normalize(word))
        if not tokens & format_tokens or tokens & kb._place_tokens:
            return word
        if stem(word) in _FORMAT_SELECTION_WORDS or _FORMAT_MODIFIER.search(word):
            # Сохраняем смысл подбора, не используя слова из названий пакетов.
            if stem(word) == stem("вариант"):
                return "форматы"
            if stem(word) == stem("предложение"):
                return "прайс"
            if re.search(r"дешев|недорог|бюджетн|доступн", word):
                return "подешевле"
            return " "
        return word

    return _WORDS.sub(replace, text)


_MONEY = re.compile(r"(?<![\d.,])(\d(?:[\d \u00a0\u202f]*\d)?(?:[.,]\d+)?)\s*"
                    r"(млн\w*|тыс\w*|т\.?р\.?|[кk](?!\w)|руб\w*|₽|р\.?(?!\w))?", re.I)
_MONEY_COUNT = re.compile(r"\s*(?:%|процент|месяц|недел|дн|день|год|секунд|показ|выход|штук|шт\b|м[²2]|макетов)", re.I)
_TOTAL_BUDGET = re.compile(r"всего|общ(?:ий|его|ая|ую|ем)|итого|суммарн\w*|за\s*все|на\s*все|под\s*ключ", re.I)
_PLACEMENT_BUDGET = re.compile(r"размещ\w*|показ\w*|аренд\w*|трансляц\w*|эфир\w*", re.I)
_PREPARATION_BUDGET = re.compile(r"подготов\w*|макет\w*|печат\w*|монтаж\w*|дизайн\w*|изготов\w*|производств\w*", re.I)
_BUDGET_CONTEXT = re.compile(r"бюджет|можем|выдел|залож|потолок|потрат|улож|максимум|всего|итого|"
                             r"не\s*(?:больше|более|дороже)|на\s*(?:размещ|макет|подготов|показ)", re.I)
_BUDGET_PART = re.compile(r"из\s*них|включая|в\s*том\s*числе|вход\w*|вош\w*|войд\w*", re.I)
_BUDGET_BREAK = re.compile(r"[,;!?:+\n]|(?<!\d)\.(?!\d)|\b(?:и|а|но|хотя|включая|из них)\b", re.I)


@dataclass
class _Budget:
    total: int | None = None
    placement: int | None = None
    preparation: int | None = None
    ambiguous: bool = False


def _budget_request(text: str) -> _Budget | None:
    """Различает общий предел и его части, не выбирая первую из противоречащих сумм."""
    fallback = parse_budget(text)
    if not (fallback or _BUDGET_CONTEXT.search(text) or _BUDGET_PART.search(text)):
        return None
    matches = list(_MONEY.finditer(text))
    scales = {m.start(): 1_000_000 if m[2].startswith("млн") else
              1000 if m[2].startswith(("тыс", "т", "к", "k")) else 1 for m in matches if m[2]}
    amounts: dict[str, list[int]] = {key: [] for key in ("total", "placement", "preparation", "part")}
    for i, match in enumerate(matches):
        if _MONEY_COUNT.match(text, match.end()):
            continue
        # «от 10 до 25 тысяч» и «10–25 тысяч» — один верхний предел.
        if i + 1 < len(matches) and re.fullmatch(r"\s*(?:до|[-–—])\s*", text[match.end():matches[i + 1].start()]):
            continue
        start = matches[i - 1].end() if i else 0
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        before = _BUDGET_BREAK.split(text[start:match.start()])[-1]
        after = _BUDGET_BREAK.split(text[match.end():end])[0]
        roles = [(m.start(), role) for role, pattern in (("total", _TOTAL_BUDGET),
                 ("placement", _PLACEMENT_BUDGET), ("preparation", _PREPARATION_BUDGET))
                 for m in pattern.finditer(before)]
        role = max(roles)[1] if roles else next((role for role, pattern in (
            ("preparation", _PREPARATION_BUDGET), ("placement", _PLACEMENT_BUDGET), ("total", _TOTAL_BUDGET))
            if pattern.search(after)), "part" if _BUDGET_PART.search(text[start:match.start()]) else "total")
        unit = match[2]
        if not unit and not (roles or _BUDGET_CONTEXT.search(before) or
                             _PLACEMENT_BUDGET.search(after) or _PREPARATION_BUDGET.search(after) or
                             _BUDGET_PART.search(text[start:end]) or fallback):
            continue
        value = float(re.sub(r"\s", "", match[1]).replace(",", "."))
        scale = scales.get(match.start(), 1)
        if not unit and value < 1000 and scales:
            # «на показ можем 25, макет до 10 тысяч» — единица общая для соседних сумм.
            scale = scales[min(scales, key=lambda pos: abs(pos - match.start()))]
        value = int(value * scale)
        if value >= 1000:
            amounts[role].append(value)
    if not any(amounts.values()):
        return _Budget(fallback) if fallback else None
    total = max(amounts["total"], default=None)
    placement = max(amounts["placement"], default=None)
    preparation = max(amounts["preparation"], default=None)
    parts = (placement or 0) + (preparation or 0)
    ambiguous = len(set(amounts["total"])) > 1 or bool(total and (
        parts > total or max(amounts["part"], default=0) > total))
    # Без общего предела две названные статьи складываются; одна статья не означает бюджет на всё.
    if total is None and placement and preparation:
        total = parts
    return _Budget(None if ambiguous else total, placement, preparation, ambiguous)


_BOTH = re.compile(r"\b(?:оба|обе|обоих|обеих|обоим|обеим|сравни(?:те|ть)?|сравним|два варианта|две позиции)\b")
_ADDITION = re.compile(r"\b(?:еще|(?:такой|такая|такое|такую|тот|та|то) же|второй вариант|также|плюс)\b")
_REPLACEMENT = re.compile(r"\b(?:вместо|только|лучше|не надо|передумал\w*)\b|^нет\b")
_DURATION_MODIFIER = re.compile(r"\b(?:только|лучше)\s+(?=" + _PERIOD.pattern + ")", re.I)
_INSTEAD = re.compile(r"\bвместо\s+(?:т[р]?ц\s+)?[«\"]?[\w-]+(?:\s+молл\w*)?[»\"]?")
_ITEM_BREAK = re.compile(r"\n+|[.;]\s+|,?\s+\b(?:и|а)\b\s+")
_ITEM_NUMBER = re.compile(r"\b(первый|первого|первому|второй|второго|второму)\b")


@dataclass(frozen=True)
class Placement:
    venue: str
    city: str
    format: str
    period: str
    place: str
    client_type: str

    def description(self) -> str:
        return ", ".join(filter(None, (self.venue, self.format, self.place, self.period)))


@dataclass
class Conversation:
    venue: str = ""
    city: str = ""
    format: str = ""
    place: str = ""
    period: str = ""
    budget: int | None = None
    placement_budget: int | None = None
    preparation_budget: int | None = None
    budget_ambiguous: bool = False
    client_type: str = ""
    intent: str = ""
    pending: str = ""
    clarifications: int = 0
    changed: bool = False
    multiple_venues: bool = False
    placements: tuple[Placement, ...] = ()

    def _placement(self) -> Placement:
        return Placement(self.venue, self.city, self.format, self.period, self.place, self.client_type)

    def _comparison_target(self, venues: list[str], text: str) -> int:
        named = [i for i, item in enumerate(self.placements) if item.venue in venues]
        if len(named) == 1:
            return named[0]
        if number := _ITEM_NUMBER.search(text):
            return int(number[1].startswith("втор"))
        if self.pending in ("comparison_0", "comparison_1"):
            return int(self.pending[-1])
        return self.placements.index(self._placement())

    def _sent_comparison_prompt(self, answer: str) -> str:
        for i, item in enumerate(self.placements):
            if any(f"{item.description()}. {q}" in answer for q in CLARIFICATIONS.values()):
                return f"clarification:comparison_{i}"
        if CLARIFICATIONS["other_placement"] in answer:
            return "clarification:other_placement"
        return ""

    @classmethod
    def replay(cls, kb: KnowledgeBase, turns) -> Conversation:
        state = cls()
        for turn in turns:
            state.observe(kb, turn["question"])
            # Черновик вопроса менеджеру ещё не был задан клиенту.
            if turn["status"] in ("sent_auto", "draft_sent", "draft_edited", "sandbox"):
                reason = turn["reason"] or ""
                if turn["reason"] is None and "answer" in turn.keys():
                    reason = state._sent_comparison_prompt(turn["answer"] or "")
                elif turn["status"] == "draft_edited" and reason.startswith("clarification:comparison_"):
                    reason = state._sent_comparison_prompt(turn["answer"])
                elif turn["status"] == "draft_edited" and reason.startswith("clarification:"):
                    # Менеджер мог заменить уточнение полноценным ответом.
                    if not any(text in turn["answer"] for text in CLARIFICATIONS.values()):
                        reason = ""
                state.finish(turn["decision"], reason)
        return state

    @staticmethod
    def venues(kb: KnowledgeBase) -> dict[str, frozenset[str]]:
        found = {}
        for row in kb.prices:
            head = re.sub(r"\s*\(.*?\)", "", row.placement.split(" — ", 1)[0]).strip()
            tokens = venue_tokens(row.placement) - _CITY_TOKENS
            if tokens and (" — " in row.placement or "," in head or re.search(r"\bт[р]?ц\b|молл", head, re.I)
                           or tokens & set(_VENUE_ALIASES.values())):
                found[head] = tokens
        return found

    def observe(self, kb: KnowledgeBase, question: str, *, additional: bool = False) -> None:
        self.changed = False
        if small_talk(question):
            return
        text = canonical_query(active_request(from_translit(question)))
        text = _DURATION_MODIFIER.sub("", text)
        if _RESET.search(text):
            self.__dict__.update(Conversation().__dict__)
            self.changed = True
        positive = _NEGATED.sub(" ", _NOT_TENANT_RE.sub("внешние рекламодатели", text))
        positive = _INSTEAD.sub(" ", positive)
        # Псевдонимы те же, что в поиске: «в академе», «в депошке», «veer»
        tokens = {_VENUE_ALIASES.get(t, t) for t in kb.normalize(positive)}
        venues = [name for name, keys in self.venues(kb).items() if keys & tokens]
        previous = self._placement()
        comparing = bool(additional or self.pending == "other_placement"
                         or _BOTH.search(positive) or _ADDITION.search(positive))
        replacing = bool(_REPLACEMENT.search(text) or _NEGATED.search(text))
        if len(venues) == 2 and comparing and not replacing:
            for boundary in _ITEM_BREAK.finditer(text):
                left, right = text[:boundary.start()], text[boundary.end():]
                left_venues = kb._venues_in(left) - _CITY_TOKENS
                right_venues = kb._venues_in(right) - _CITY_TOKENS
                if left_venues and right_venues and left_venues.isdisjoint(right_venues):
                    self.placements = ()
                    self.observe(kb, left)
                    self.observe(kb, right, additional=True)
                    return
        adding = not self.placements and comparing and not replacing and bool(previous.venue or previous.format)
        target = None
        if self.placements:
            target = self._comparison_target(venues, positive)
            item = self.placements[target]
            self.venue, self.city = item.venue, item.city
            self.format, self.period = item.format, item.period
            self.place, self.client_type = item.place, item.client_type
            if replacing or (item.venue and any(v != item.venue for v in venues)):
                self.placements = ()
        if adding and len(venues) == 1 and venues[0] != previous.venue:
            self.period = ""  # «такой же экран» не задаёт срок на второй площадке
        city = _CITY_RE.search(positive)
        new_city = _CITY_NAMES[next(k for k, v in city.groupdict().items() if v)] if city else ""
        if new_city and self.city and new_city != self.city:
            self.venue, self.place, self.client_type = "", "", ""
            self.changed = True
        if len(venues) == 1:
            if self.venue and self.venue != venues[0]:
                self.place, self.client_type = "", ""
                self.changed = True
            self.venue = venues[0]
            self.multiple_venues = False
            venue_city = _CITY_RE.search(self.venue.lower())
            if venue_city:
                new_city = _CITY_NAMES[next(k for k, v in venue_city.groupdict().items() if v)]
        elif venues or _OTHER_VENUES.search(positive):
            self.venue, self.place, self.client_type = "", "", ""
            self.city = ""
            self.multiple_venues = True
            self.changed = True
        elif _NEGATED.search(text) and kb._venues_in(text) - _CITY_TOKENS:
            self.changed = bool(self.venue or self.place or self.client_type)
            self.venue, self.place, self.client_type = "", "", ""
        if new_city:
            self.city = new_city
        format_tokens = kb._format_tokens - _FORMAT_SKIP - (_EXTRAS if self.format else set())
        if self.format and re.search(r"требован|макет|\bготов\w*", text):
            format_tokens -= {stem(w) for w in ("ролик видео видеоролик").split()}
        words = _WORDS.findall(positive)
        formats = _selected_formats(kb, positive, format_tokens, known=bool(self.format))
        # «Добрый день, …в месяц»: «день» из приветствия не срок и не слово для поиска
        plain = drop_courtesy(positive)
        self._format_query = (question, _format_search_text(kb, plain, kb._format_tokens, formats))
        if formats:
            selected = " ".join(formats)
            if self.format and not set(kb.normalize(selected)) & set(kb.normalize(self.format)):
                self.place = ""
                self.changed = True
            self.format = selected
        elif any(set(kb.normalize(" ".join(_selected_formats(kb, m[0], format_tokens))))
                 & set(kb.normalize(self.format)) for m in _NEGATED.finditer(text)):
            self.changed = self.changed or bool(self.format or self.place)
            self.format, self.place = "", ""
        # «флаг в атриуме»: флаг есть и среди мест Бутово («флаг 1»), но здесь это уже формат
        places = list(dict.fromkeys(w for w in words if w not in formats
                                    and set(kb.normalize(w)) & (kb._place_tokens - _PLACE_SKIP)))
        old_place = self.place
        if places:
            self.place = " ".join(places)
        if position := position_ranges(positive):
            self.place = "место № " + ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in position)
        if old_place and self.place != old_place:
            self.changed = True
        found_periods = list(_PERIOD.finditer(plain))
        period, season = found_periods[-1] if found_periods else None, _SEASON.search(plain)
        if period or season:
            duration = period or _PERIOD.search(self.period)
            month = season or _SEASON.search(self.period)
            self.period = " ".join(m.group(0) for m in (duration, month) if m)
        budget = _budget_request(("бюджет " if self.pending == "budget" or self.budget_ambiguous else "") + positive)
        if budget:
            if budget.total is None and not budget.ambiguous and self.budget is not None:
                budget.total = self.budget
                budget.placement = budget.placement if budget.placement is not None else self.placement_budget
                budget.preparation = budget.preparation if budget.preparation is not None else self.preparation_budget
                budget.ambiguous = (budget.placement or 0) + (budget.preparation or 0) > budget.total
                if budget.ambiguous:
                    budget.total = None
            self.changed = (self.changed or self.budget_ambiguous or
                            (self.budget is not None and self.budget != budget.total))
            self.budget, self.placement_budget = budget.total, budget.placement
            self.preparation_budget = budget.preparation
            self.budget_ambiguous = budget.ambiguous or (self.budget_ambiguous and budget.total is None)
            if self.pending == "budget" and not budget.ambiguous and budget.total is not None:
                self.pending = ""
        if _NOT_TENANT_RE.search(text) or re.search(r"внешн\w* рекламодател", text):
            self.client_type = "внешние рекламодатели"
        elif not self.multiple_venues and re.search(r"\bарендатор", positive):
            self.client_type = "мы арендаторы"
        focus = [value for pattern, value in _FOCUS if pattern.search(text)]
        if _PRICE.search(text):
            focus.insert(0, "цена")
        if focus:
            self.intent = " ".join(focus)
        elif not (venues or _OTHER_VENUES.search(text) or formats or places or position or period or season or budget or new_city
                  or _REFERS.search(text) or _BUSINESS.search(text) or _SELECTION.search(text)
                  or self.pending or comparing or _NOT_TENANT_RE.search(text)
                  or kb.matched_stop_topic(text)) and len(_WORDS.findall(plain)) > 2:
            # «Спасибо, хорошего дня» — не новая тема: вежливые слова не считаем
            self.__dict__.update(Conversation().__dict__)
            self.changed = True
        current = self._placement()
        # Дополнение пустого поля («ещё уточню, на месяц») не создаёт вторую позицию.
        distinct = any(a and b and a != b for a, b in (
            (previous.venue, current.venue), (previous.format, current.format),
            (previous.period, current.period), (previous.place, current.place)))
        if adding and distinct and not self.multiple_venues:
            self.placements = (previous, current)
        elif self.placements and target is not None:
            items = list(self.placements)
            items[target] = current
            self.placements = tuple(items) if not self.multiple_venues and items[0] != items[1] else ()

    def query(self, question: str) -> str:
        fields = (self.venue, self.city, self.format, self.place, self.period, self.client_type, self.intent)
        current = _NEGATED.sub(" ", _NOT_TENANT_RE.sub("внешние рекламодатели",
                              canonical_query(active_request(from_translit(question)))))
        observed, filtered = getattr(self, "_format_query", (None, ""))
        if observed == question:
            current = filtered
        if self.multiple_venues and not self.client_type:
            # Статус арендатора одного ТЦ не даёт льготную цену в соседнем.
            current = re.sub(r"\bарендатор\w*", "внешние рекламодатели", current)
        # Поиск KB читает первый бюджет. Сумма на макет в реплике не заменяет общий предел.
        budget = f"бюджет {self.budget} ₽" if self.budget else ""
        return " ".join(filter(None, (budget, current, *fields)))

    def parameters(self) -> str:
        if self.placements:
            selected = tuple((f"Размещение {i}", item.description()) for i, item in enumerate(self.placements, 1))
        else:
            selected = (("ТЦ", self.venue), ("город", self.city), ("формат", self.format),
                        ("место", self.place), ("срок", self.period))
        return "; ".join(f"{name}: {value}" for name, value in (
            *selected, ("бюджет", format_rub(self.budget) if self.budget else ""),
            ("на размещение", format_rub(self.placement_budget) if self.placement_budget else ""),
            ("на подготовку", format_rub(self.preparation_budget) if self.preparation_budget else ""),
            ("клиент", self.client_type)) if value)

    def clarification(self, kb: KnowledgeBase, question: str, *, no_context: bool = False,
                      hits: list[SearchHit] | None = None) -> tuple[str, str] | None:
        text = active_request(question).lower()
        if self.budget_ambiguous or (self.pending == "budget" and self.budget is None):
            return "budget", CLARIFICATIONS["budget"]
        if _ORDINAL.search(text):
            return "position", CLARIFICATIONS["position"]
        named = set(kb.normalize(question))
        if self.venue and not (self.format or self.place) and named and named <= set(kb.normalize(self.venue)):
            return "format", CLARIFICATIONS["format"]
        if self.budget or _GENERAL_PRICE.search(text):
            return None
        price = _PRICE.search(text) or "цена" in self.intent or self.pending
        if not price:
            return None
        if not self.venue and not self.multiple_venues and len(self.venues(kb)) > 1:
            relevant = hits if hits is not None else kb.retrieve(self.query(question))
            selected, place = set(kb.normalize(self.format)), set(kb.normalize(self.place))
            priced = [h.chunk for h in relevant if h.chunk.kind == "price"]
            overlap = {c.id: 2 * len(selected & c.fmt) + len(place & c.place) for c in priced}
            best = max(overlap.values(), default=0)
            groups = {c.group for c in priced if overlap[c.id] == best}
            if not (self.format or self.place) or len(groups) != 1:
                return "venue", CLARIFICATIONS["venue"]
        if not self.format and not self.place:
            return "format", CLARIFICATIONS["format"]
        if no_context and self.pending:
            return "position", CLARIFICATIONS["missing"]
        return None

    def finish(self, decision: str, reason: str) -> None:
        if reason.startswith("clarification:"):
            self.pending = reason.partition(":")[2]
            self.clarifications += 1
        elif decision == "answer" and reason not in ("greeting", "small_talk"):
            self.pending, self.clarifications = "", 0
        elif decision == "escalate":
            self.pending = ""
