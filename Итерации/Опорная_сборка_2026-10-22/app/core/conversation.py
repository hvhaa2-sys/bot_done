"""Параметры запроса из слов клиента; без цен и предположений модели."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import (KnowledgeBase, SearchHit, _CITY_NAMES, _CITY_RE, _CITY_TOKENS, _NOT_TENANT_RE,
                        _VENUE_ALIASES, format_rub, parse_budget, position_ranges, venue_tokens)
from .text import from_translit, small_talk, stem

MAX_CLARIFICATIONS = 2
CLARIFICATIONS = {
    "position": "Подскажите название или место размещения нужного варианта — так я назову точную цену.",
    "venue": "В каком торговом центре рассматриваете размещение?",
    "format": "Какой формат рекламы вас интересует?",
    "missing": "Подскажите название формата или место размещения, которое вас интересует.",
}
_WORDS = re.compile(r"[а-яёa-z0-9-]+", re.IGNORECASE)
_PRICE = re.compile(r"сколько\s+(?:это\s+)?сто|стоимост|\bцен(?:а|ы|у|е|ой|ник|ов)\w*|поч[её]м|прайс|расцен|тариф", re.IGNORECASE)
_REFERS = re.compile(r"^\s*(?:а\b|и\b|ну\b|на\b)|\b(?:этот|эта|это|там|него|неё|нее|вариант|тот же)\b", re.I)
_ORDINAL = re.compile(r"\b(?:первый|второй|третий|последний)\s+вариант\b", re.I)
_RESET = re.compile(r"забуд\w*|начн[её]м сначала|новая тема|другая тема", re.I)
_NEGATED = re.compile(r"\bне\s+(?!(?:подскаж\w*|зна\w*|помн\w*|дороже|больше|меньше|менее)\b)"
                      r"[^.!?;]+?(?:,?\s+(?:а|лучше|давайте|тогда)\s+|(?=[.!?;]|$))", re.I)
_PERIOD = re.compile(r"\b(?:на\s+)?(?:(?:\d{1,2}|один|два|две|три|четыре|пять|шесть|пару)\s+)?"
                     r"(?:месяц\w*|недел\w*|день|дня|дней|сутк\w*|полгода|год\w*)\b", re.I)
_SEASON = re.compile(r"\b(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йея]|июн\w*|июл\w*|август\w*|"
                     r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b", re.I)
_OTHER_VENUES = re.compile(r"\b(?:друг\w*|вс(?:е|ех|ем|еми)|люб\w*|еще)\s+т[р]?ц\b|\bкакие\s+еще\s+т[р]?ц\b", re.I)
_BUSINESS = re.compile(r"реклам|для\s+(?:магазин|бизнес|компани|салон)|\bнаш\w*\s+(?:магазин|бизнес|компани|салон)", re.I)
_GENERAL_PRICE = re.compile(r"\bндс\b|налог|вход(?:ит|ят)|включен|какие|прайс|расценки|вариант|перечисл|подбер|подбор", re.I)
_FORMAT_SKIP = {stem(w) for w in ("цифровой рекламный реклама внешний размещение арендатор тц трц молл "
                                "цена стоимость магазин рекламодатель материал часть например шт час").split()}
_EXTRAS = {stem(w) for w in "печать монтаж производство дизайн макет".split()}
_PLACE_SKIP = {stem(w) for w in "экран высота место вид сторона направление".split()}
_FOCUS = ((re.compile(r"печат|напечата", re.I), "печать"),
          (re.compile(r"монтаж|смонтир", re.I), "монтаж"),
          (re.compile(r"требован|макет|ролик|видео", re.I), "требования к макету ролику"),
          (re.compile(r"проходим|посещаем|трафик|посетител|народу", re.I), "посещаемость трафик"),
          (re.compile(r"минимальн\w* срок|от скольки|минимум", re.I), "минимальный срок"),
          (re.compile(r"размер|высот|ширин", re.I), "размеры"),
          (re.compile(r"выход\w* в|показ\w*|как часто|секунд", re.I), "показы ролика"))


@dataclass
class Conversation:
    venue: str = ""
    city: str = ""
    format: str = ""
    place: str = ""
    period: str = ""
    budget: int | None = None
    client_type: str = ""
    intent: str = ""
    pending: str = ""
    clarifications: int = 0
    changed: bool = False
    multiple_venues: bool = False

    @classmethod
    def replay(cls, kb: KnowledgeBase, turns) -> Conversation:
        state = cls()
        for turn in turns:
            state.observe(kb, turn["question"])
            # Черновик вопроса менеджеру ещё не был задан клиенту.
            if turn["status"] in ("sent_auto", "draft_sent", "draft_edited", "sandbox"):
                reason = turn["reason"] or ""
                if turn["status"] == "draft_edited" and reason.startswith("clarification:"):
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

    def observe(self, kb: KnowledgeBase, question: str) -> None:
        self.changed = False
        if small_talk(question):
            return
        text = from_translit(question).lower().replace("ё", "е")
        if _RESET.search(text):
            self.__dict__.update(Conversation().__dict__)
            self.changed = True
        positive = _NEGATED.sub(" ", text)
        tokens = set(kb.normalize(positive))
        venues = [name for name, keys in self.venues(kb).items() if keys & tokens]
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
        formats = list(dict.fromkeys(w for w in words if not w.isdigit() and len(w) > 2
                                     and set(kb.normalize(w)) & format_tokens))
        if formats:
            selected = " ".join(formats)
            if self.format and not set(kb.normalize(selected)) & set(kb.normalize(self.format)):
                self.place = ""
                self.changed = True
            self.format = selected
        elif _NEGATED.search(text) and set(kb.normalize(text)) & format_tokens:
            self.changed = self.changed or bool(self.format or self.place)
            self.format, self.place = "", ""
        places = list(dict.fromkeys(w for w in words if set(kb.normalize(w)) & (kb._place_tokens - _PLACE_SKIP)))
        old_place = self.place
        if places:
            self.place = " ".join(places)
        if position := position_ranges(positive):
            self.place = "место № " + ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in position)
        if old_place and self.place != old_place:
            self.changed = True
        period, season = _PERIOD.search(positive), _SEASON.search(positive)
        if period or season:
            duration = period or _PERIOD.search(self.period)
            month = season or _SEASON.search(self.period)
            self.period = " ".join(m.group(0) for m in (duration, month) if m)
        if budget := parse_budget(positive):
            self.budget = budget
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
                  or _REFERS.search(text) or _BUSINESS.search(text)
                  or self.pending or kb.matched_stop_topic(text)) and len(words) > 2:
            self.__dict__.update(Conversation().__dict__)
            self.changed = True

    def query(self, question: str) -> str:
        fields = (self.venue, self.city, self.format, self.place, self.period, self.client_type,
                  f"бюджет {self.budget} ₽" if self.budget else "", self.intent)
        current = _NEGATED.sub(" ", from_translit(question).lower().replace("ё", "е"))
        if self.multiple_venues and not self.client_type:
            # Статус арендатора одного ТЦ не даёт льготную цену в соседнем.
            current = re.sub(r"\bарендатор\w*", "внешние рекламодатели", current)
        return " ".join(filter(None, (current, *fields)))

    def parameters(self) -> str:
        return "; ".join(f"{name}: {value}" for name, value in (
            ("ТЦ", self.venue), ("город", self.city), ("формат", self.format), ("место", self.place),
            ("срок", self.period), ("бюджет", format_rub(self.budget) if self.budget else ""),
            ("клиент", self.client_type)) if value)

    def clarification(self, kb: KnowledgeBase, question: str, *, no_context: bool = False,
                      hits: list[SearchHit] | None = None) -> tuple[str, str] | None:
        text = question.lower()
        if _ORDINAL.search(text):
            return "position", CLARIFICATIONS["position"]
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
