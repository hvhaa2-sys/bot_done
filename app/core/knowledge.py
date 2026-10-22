"""База знаний: FAQ, прайс и стоп-темы, плюс поиск BM25."""
from __future__ import annotations

import csv
import difflib
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .price_guard import extract_amounts, months_named
from .text import STOPWORDS, fix_mixed_script, stem, tokenize


@dataclass
class Chunk:
    id: str
    kind: str          # "faq" | "price"
    title: str
    text: str          # то, что попадёт в контекст модели
    tokens: list[str] = field(default_factory=list)
    venue: frozenset[str] = frozenset()  # слова названия ТЦ (у позиций прайса и каталога)
    fmt: frozenset[str] = frozenset()    # слова формата (у позиций прайса): «экран», «флаг»…
    group: str = ""                      # площадка из каталога: «ВЕЕР Молл, Екатеринбург», «экраны Инцентра»
    place: frozenset[str] = frozenset()  # слова места внутри ТЦ: «парковка», «фуд-корт»
    price: int = 0                       # у позиций прайса — для подбора по бюджету
    period: str = ""


@dataclass
class PriceRow:
    format: str
    placement: str
    period: str
    price_rub: int
    notes: str

    def render(self) -> str:
        """Строка для модели. Примечания разложены по смыслу: в одной куче «с НДС 5%», «ролик 10 сек»
        и «печать 14 000 ₽» модель путала — называла НДС доплатой, печать приписывала экранам."""
        notes = self.parts()
        line = f"{self.format}, {self.placement} — {format_rub(self.price_rub)}"
        # Пустой срок (Бутово Молл) модель называла «в месяц» — пишем прямо, что срока нет
        period = re.sub(r"от 6 месяцев", "от 6 месяцев (полгода)", re.sub(r"на 12 месяцев", "на 12 месяцев (год)", self.period))
        line += f" за {period}" if period else " (срок в прайсе не указан)"
        line += f" ({', '.join(notes['vat'])}, уже в цене)." if notes["vat"] else "."
        for key, title in (("season", "Сезонная цена"), ("term", "Минимальный срок"),
                           ("extra", "Не входит в цену, доплата"), ("info", "Подробности")):
            if notes[key]:
                line += f" {title}: {'; '.join(notes[key]).rstrip('.')}."
        if not notes["extra"] and _DIGITAL_RE.search(self.format):
            # «Печати и монтажа нет» сразу после «с НДС, уже в цене» модель склеивала в «НДС включён в цену печати»
            line += " Ролик готовит рекламодатель, печать и монтаж для экрана не нужны."
        return line

    def parts(self) -> dict[str, list[str]]:
        """Примечания по смыслу: vat — НДС в цене, season — сезонная цена, term — минимальный срок,
        extra — доплаты (печать, монтаж, производство), info — всё остальное."""
        out: dict[str, list[str]] = {"vat": [], "season": [], "term": [], "extra": [], "info": []}
        for part in (p.strip() for p in self.notes.split(";")):
            low = part.lower()
            if not part:
                continue
            if re.fullmatch(r"с ндс(?: \d+%)?", low):
                out["vat"].append(part)
            elif re.search(r"ноябр|декабр|январ", low) and re.search(r"₽|цен|коэффициент|наценк", low) \
                    and "маркетплейс" not in low:
                # «ёлка стоит с декабря по февраль» — не сезонная цена
                out["season"].append(part)
            elif re.search(r"минимальн\w* срок", low):
                out["term"].append(part)
            elif self.format.lower().startswith("производство") and "стоимость работ" in low:
                out["info"].append(part)  # это состав самой цены, а не доплата к ней
            elif re.search(r"печат|монтаж|изготовлен|производств|стойк\w* считается|считаются отдельно|оплачива\w* отдельно",
                           low) and "включ" not in low:
                out["extra"].append(part)
            else:
                out["info"].append(part)
        if m := re.search(r"при размещении от .+", self.period):
            out["term"].insert(0, m.group(0))
        return out

    def extras_total(self) -> int:
        """Доплаты разово, для подбора по бюджету: печать + монтаж. Варианты в одной части
        («6 макетов — 41 000 ₽, 4 макета — 18 000 ₽») — берём меньший."""
        return sum(min(amounts) for part in self.parts()["extra"] if (amounts := extract_amounts(part)))


_DIGITAL_RE = re.compile(r"экран|медиа-куб|видео|ролик перед", re.IGNORECASE)


def format_rub(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


@dataclass
class SearchHit:
    chunk: Chunk
    score: float


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.avgdl = (sum(len(d) for d in docs) / len(docs)) if docs else 0.0
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def score(self, query: list[str], i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in set(query):
            if t not in tf:
                continue
            f = tf[t]
            s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
        return s


class KnowledgeBase:
    def __init__(self, faq: list[Chunk], prices: list[PriceRow], stop_topics: list[str]):
        self.faq = faq
        self.prices = prices
        self.stop_topics = [t.replace("ё", "е") for t in stop_topics]  # «расчёт» из админки
        self.chunks: list[Chunk] = list(faq)
        for i, row in enumerate(prices):
            text = f"Прайс: {row.render()}"
            self.chunks.append(Chunk(f"price-{i}", "price", row.format, text,
                                     # формат весит больше: длинные примечания не должны его заглушать
                                     tokenize(row.format) * 2
                                     + tokenize(f"{row.placement} {row.notes} цена стоимость прайс"),
                                     venue_tokens(row.placement),
                                     frozenset(tokenize(row.format)) - _GENERIC_VENUE_WORDS,
                                     catalog_entry(row)[0], place_tokens(row.placement), row.price_rub, row.period))
        self._bm25 = BM25([c.tokens for c in self.chunks])
        self._all_venues = frozenset().union(*(c.venue for c in self.chunks))
        self._format_tokens = frozenset().union(*(c.fmt for c in self.chunks))
        self._place_tokens = frozenset().union(*(c.place for c in self.chunks))
        self.catalog = build_catalog(prices)
        self._tenant = False

    # --- загрузка ---
    @classmethod
    def load(cls, kb_dir: Path) -> "KnowledgeBase":
        faq_path, price_path, stop_path = kb_dir / "faq.md", kb_dir / "prices.csv", kb_dir / "stop_topics.txt"
        faq = parse_faq(faq_path.read_text(encoding="utf-8")) if faq_path.exists() else []
        # Заметки: папка notes/ (её можно открыть как хранилище Obsidian) — по файлу на тему или ТЦ
        for note in sorted((kb_dir / "notes").rglob("*.md")) if (kb_dir / "notes").is_dir() else []:
            faq += parse_note(note.read_text(encoding="utf-8"), note.stem, start=len(faq))
        prices = parse_prices(price_path.read_text(encoding="utf-8")) if price_path.exists() else []
        stops = parse_stop_topics(stop_path.read_text(encoding="utf-8")) if stop_path.exists() else []
        return cls(faq, prices, stops)

    # --- поиск ---
    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        q = expand_query([_VENUE_ALIASES.get(t, t) for t in self.normalize(query)])
        if not q:
            return []
        hits = [SearchHit(c, self._bm25.score(q, i)) for i, c in enumerate(self.chunks)]
        hits = [h for h in hits if h.score > 0]
        # Назван ТЦ — позиции прайса других ТЦ в контекст не берём, чтобы модель
        # не назвала цену чужого ТЦ. FAQ остаётся.
        named = self._all_venues.intersection(q)
        if named:
            hits = [h for h in hits if h.chunk.kind != "price" or h.chunk.venue & named]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def retrieve(self, query: str, top_k: int = 8, min_score: float = 0.0) -> list[SearchHit]:
        """Фрагменты для ответа модели.

        Обычно это лучшие по BM25. Но когда клиент спрашивает, что вообще есть («а экраны по разным
        ТРЦ есть?», «давайте ВЕЕР рассмотрим», «хочу рекламу»), восьми строк мало: модель перечислит
        их как полный список, и клиент не узнает про остальное. Тогда добавляем все подходящие строки
        прайса (если их не больше LIST_LIMIT) или каталог форматов площадки.
        """
        ranked = self._for_position(self._for_client_type(self.search(query, top_k=len(self.chunks)), query), query)
        low = query.lower().replace("ё", "е")
        tokens = {_VENUE_ALIASES.get(t, t) for t in self.normalize(query)}
        named = self._all_venues & tokens
        if malls := named - _CITY_TOKENS:
            # «Сколько народу в аутлете?» — заметки других ТЦ про посещаемость не нужны: модель брала цифру ВЕЕР
            ranked = [h for h in ranked if h.chunk.kind != "faq" or not h.chunk.venue or h.chunk.venue & malls]
        else:
            # ТЦ не назван — общий ответ выше заметки одного ТЦ: на «минимальный срок какой?» первой шла заметка ДЕПО
            ranked.sort(key=lambda h: h.score * (VENUE_NOTE_WEIGHT if h.chunk.kind == "faq" and h.chunk.venue else 1),
                        reverse=True)
        pool = [h for h in ranked if h.score >= min_score]
        # Разделов заметок сотни: без предела они занимали все 8 мест, и строка прайса с ценой не попадала
        faqs = {h.chunk.id for h in [h for h in pool if h.chunk.kind == "faq"][:FAQ_TOP_K]}
        hits = [h for h in pool if h.chunk.kind != "faq" or h.chunk.id in faqs][:top_k]
        fmt = tokens & self._format_tokens
        placed = tokens & self._place_tokens
        # «Какие требования к ролику?» — на это отвечает FAQ, перечень экранов не нужен
        best = {kind: max((h.score for h in ranked if h.chunk.kind == kind), default=0.0) for kind in ("faq", "price")}
        faq_answers = best["faq"] >= 5 and best["faq"] >= 2 * best["price"]
        listing = bool(_LIST_RE.search(low)) and not faq_answers
        city = _CITY_RE.search(low)
        if _CHEAPEST_RE.search(low) and (named or fmt or city):
            # «Самая дешёвая реклама в ВЕЕР?» — смотрим весь прайс площадки, а не 8 строк: иначе модель
            # назовёт самой дешёвой ту, что случайно попала в выдачу. Сравниваем только месячные цены
            rows = self._for_position(self._in_city(self._price_hits(named), city), query)
            rows = [h for h in rows if h.chunk.fmt & fmt] or rows  # «цветочный магазин» — не формат «витрины магазинов»
            monthly = [h for h in rows if "месяц" in h.chunk.period or not h.chunk.period]
            rows = sorted(monthly or rows, key=lambda h: h.chunk.price)
            if rows:
                # Только прайс по возрастанию: с заметками впереди модель называла самой дешёвой вторую строку
                return self._for_position(rows[:10], query)
        if budget := parse_budget(low):
            # «Бюджет 30 тысяч, что можно в ВЕЕР?» — все месячные позиции не дороже бюджета
            # Цена за месяц или без срока (Бутово Молл: срок в прайсе не указан); «за 1 день» в бюджет месяца не сравниваем
            rows = self._for_position(
                [h for h in self._in_city(self._price_hits(named), city)
                 if h.chunk.price + self.prices[int(h.chunk.id.split("-")[1])].extras_total() <= budget
                 and ("месяц" in h.chunk.period or not h.chunk.period)], query)
            rows.sort(key=lambda h: h.chunk.price, reverse=True)
            # «Бюджет 25 000 — подходит экран у входа?» — сам экран дороже бюджета, но он нужен, чтобы ответить «нет»
            asked = [h for h in hits if h.chunk.kind == "price" and h.chunk.fmt & fmt][:3] if fmt else []
            picked = {h.chunk.id for h in asked}
            return self._for_position(
                [h for h in hits if h.chunk.kind != "price"][:LIST_FAQ_LIMIT] + asked
                + [h for h in spread_by_venue(rows) if h.chunk.id not in picked][:LIST_LIMIT], query)
        if listing and (fmt or placed):
            rows = self._rows_with_format(ranked, fmt | placed)
            if len(rows) <= LIST_LIMIT:
                # Модель перечисляет первые строки, поэтому ТЦ чередуем: в «примеры» попадут разные
                # Из прайса — только строки нужного формата: «промо-зона на катке» в списке экранов лишняя
                # Перечень по всем ТЦ — заметки одного ТЦ («кто делает ролик для экранов ВЕЕР») не нужны
                notes = [h for h in ranked if h.chunk.kind == "faq" and (malls or not h.chunk.venue)]
                hits = notes[:LIST_FAQ_LIMIT] + spread_by_venue(self._in_city(rows, city))
        if named and (listing or not fmt):
            hits += [SearchHit(c, 0.0) for c in self.catalog if c.venue & named]
        elif not named and not fmt and not faq_answers and (_INTENT_RE.search(low) or (listing and not tokens)):
            # «Хочу рекламу», «что у вас есть?» — да; «какая сегодня погода?» — нет: там есть слова не про нас
            hits += [SearchHit(c, 0.0) for c in self.catalog]
        if _OFF_RANGE_RE.search(low) and not any(h.chunk.kind == "catalog" for h in hits):
            hits += [SearchHit(c, 0.0) for c in self.catalog]
        return self._for_position(hits + self._parts_hits(query, hits, named, min_score), query)

    def _for_position(self, hits: list[SearchHit], query: str) -> list[SearchHit]:
        """Явный номер места ограничивает прайс: цена соседнего места не подтверждает ответ."""
        requested = position_ranges(query)
        if not requested:
            return hits
        selected = []
        for hit in hits:
            if hit.chunk.kind == "faq":
                selected.append(hit)
            elif hit.chunk.kind == "price":
                placement = self.prices[int(hit.chunk.id.split("-")[1])].placement
                available = position_ranges(placement)
                if any(max(a, c) <= min(b, d) for a, b in requested for c, d in available):
                    selected.append(hit)
        return selected

    def street_screens(self) -> list[PriceRow]:
        return [r for r in self.prices if "уличный экран" in r.placement.lower()]

    def off_range_only(self, question: str) -> bool:
        """«Билборд на трассе сколько?», «таргет делаете?» — спрашивают только то, чего у нас нет."""
        low = question.lower().replace("ё", "е")
        tokens = {_VENUE_ALIASES.get(t, t) for t in self.normalize(question)}
        return bool(_OFF_RANGE_RE.search(low)) and not (tokens & (self._all_venues | self._format_tokens))

    def _parts_hits(self, query: str, hits: list[SearchHit], named: set[str], min_score: float) -> list[SearchHit]:
        """«Экран на фуд-корте и радио, мы не арендаторы», «1) экран 2) радио 3) промоутер» — восемь лучших
        строк по всему сообщению достаются одной теме, остальные части модель не видит и зовёт менеджера.
        Ищем по каждой части отдельно (с ТЦ из всего сообщения) и добавляем недостающее."""
        parts = [p for p in _PARTS_RE.split(query) if len(self.normalize(p)) >= 1]
        if len(parts) < 2:
            return []
        seen = {h.chunk.id for h in hits}
        extra: list[SearchHit] = []
        for part in parts:
            ranked_part = self.search(part, top_k=len(self.chunks))
            found = [h for h in ranked_part if h.chunk.kind == "price"
                     and not self._tenant_only(h.chunk) and (not named or h.chunk.venue & named)][:PART_TOP_K]
            found += [h for h in ranked_part if h.chunk.kind == "faq"
                      and (not named or not h.chunk.venue or h.chunk.venue & named)][:1]
            for h in found:
                if h.score >= min_score and h.chunk.id not in seen:
                    seen.add(h.chunk.id)
                    extra.append(h)
        return extra[:PART_EXTRA_LIMIT]

    def wrong_tier_amounts(self, answer: str, hits: list[SearchHit], question: str) -> list[int]:
        """«Пакет на полгода — 179 520 ₽»: это цена «от 3 месяцев», на полгода — 168 300 ₽ («от 6 месяцев»).
        Клиент назвал срок, у строки есть ступени по сроку, а в ответе только цена чужой ступени."""
        months = months_named(question)
        if len(months) != 1:
            return []
        m, said = next(iter(months)), set(extract_amounts(answer))
        groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for h in hits:
            if h.chunk.kind == "price":
                row = self.prices[int(h.chunk.id.split("-")[1])]
                if t := _TIER_RE.search(row.period):
                    low = 1 if t.group(1) == "до" else int(t.group(2))
                    groups.setdefault((row.format, row.placement), []).append((low, row.price_rub))
        wrong: set[int] = set()
        for tiers in groups.values():
            fit = sorted(t for t in tiers if t[0] <= m)  # ступень срока — с самой большой нижней границей
            if fit and fit[-1][1] not in said:
                wrong |= said & {price for _, price in tiers if price != fit[-1][1]}
        return sorted(wrong)

    def mismatched_amounts(self, answer: str, hits: list[SearchHit], question: str = "") -> list[int]:
        """Суммы, которые стоят рядом с одним ТЦ, а в контексте есть только в строках другого ТЦ.
        Ловит «экран в ДЕПО — 31 500 ₽» (это цена ВЕЕР) — проверка «сумма есть в прайсе» такое пропускала.
        ТЦ ищется в той же фразе, иначе в предыдущих фразах ответа, иначе в вопросе."""
        rows = [h.chunk for h in hits if h.chunk.kind == "price"]
        if not rows:
            return []
        owners: dict[int, list[Chunk]] = {}
        for c in rows:
            row = self.prices[int(c.id.split("-")[1])]
            for amount in {row.price_rub, *extract_amounts(row.notes)}:
                owners.setdefault(amount, []).append(c)
        venues = self._venues_in(question)
        bad: list[int] = []
        for clause in re.findall(r"[^.!?\n,;—–]+", answer):
            venues = self._venues_in(clause) or venues
            for amount in extract_amounts(clause):
                found = owners.get(amount)
                if found and venues and not any(c.venue & venues for c in found) and amount not in bad:
                    bad.append(amount)
        return bad

    def rows_in_answer(self, answer: str, hits: list[SearchHit]) -> list[PriceRow]:
        """Строки контекста, чью цену называет ответ: цена и формат в той же фразе. Одного ТЦ мало —
        к «промо в ДЕПО, все выходные — 25 000 ₽» дописывались условия баннера ДЕПО за те же 25 000 ₽."""
        found: list[PriceRow] = []
        for sentence in re.findall(r"[^.!?\n]+", answer):
            words = {_VENUE_ALIASES.get(t, t) for t in self.normalize(sentence)}
            for amount in set(extract_amounts(sentence)):
                matches = [self.prices[int(h.chunk.id.split("-")[1])] for h in hits
                           if h.chunk.kind == "price" and h.chunk.price == amount and _format_words(h.chunk.fmt) & words]
                # два баннера по 25 000 ₽ — не угадываем, чьи условия дописать
                if len(matches) == 1 and matches[0] not in found:
                    found.append(matches[0])
        return found

    def client_summary(self, texts: list[str]) -> str:
        """Что клиент уже назвал — строка для карточки менеджеру: «ТЦ: ВЕЕР Молл · формат: Цифровой экран ·
        срок: 3 мес. · бюджет: 50 000 ₽ · внешний рекламодатель». Собирается кодом, без модели."""
        joined = " ".join(texts)
        low = joined.lower().replace("ё", "е")
        tokens = {_VENUE_ALIASES.get(t, t) for t in self.normalize(joined)}
        venues, formats = [], []
        for row in self.prices:
            head = re.sub(r"\s*\(.*?\)", "", row.placement.split(" — ", 1)[0]).split(",")[0].strip()
            if venue_tokens(row.placement) - _CITY_TOKENS & tokens and head not in venues:
                venues.append(head)
            family = format_family(row.format)
            if _format_words(frozenset(tokenize(family))) - _SUMMARY_SKIP & tokens and family not in formats:
                formats.append(family)
        parts = []
        if venues:
            parts.append("ТЦ: " + ", ".join(venues[:3]))
        elif city := _CITY_RE.search(low):
            parts.append("город: " + _CITY_TITLES[next(k for k, v in city.groupdict().items() if v)])
        if formats:
            parts.append("формат: " + ", ".join(formats[:2]) + (" и др." if len(formats) > 2 else ""))
        if months := months_named(joined):
            parts.append("срок: " + ", ".join(f"{m} мес." for m in sorted(months)))
        if budget := parse_budget(low):
            parts.append(f"бюджет: {format_rub(budget)}")
        if _NOT_TENANT_RE.search(low) or "внешн" in low:
            parts.append("внешний рекламодатель")
        elif re.search(r"(?<!не )арендатор", low):
            parts.append("арендатор")
        return " · ".join(parts)

    def _venues_in(self, text: str) -> frozenset[str]:
        return frozenset(_VENUE_ALIASES.get(t, t) for t in self.normalize(text)) & self._all_venues

    @staticmethod
    def _in_city(rows: list[SearchHit], city: re.Match | None) -> list[SearchHit]:
        if not city:
            return rows
        name = _CITY_NAMES[next(k for k, v in city.groupdict().items() if v)]
        return [h for h in rows if name in h.chunk.text.lower()] or rows

    def _price_hits(self, named: set[str]) -> list[SearchHit]:
        return [SearchHit(c, 0.0) for c in self.chunks
                if c.kind == "price" and (not named or c.venue & named) and not self._tenant_only(c)]

    def _tenant_only(self, chunk: Chunk) -> bool:
        return chunk.kind == "price" and "для арендаторов" in chunk.title.lower() and not self._tenant

    def _for_client_type(self, hits: list[SearchHit], query: str) -> list[SearchHit]:
        """Цены «для арендаторов» — только если клиент сказал, что он арендатор ТЦ. Иначе модель
        случайно брала их для всех: промоутер на месяц 45 000 вместо 150 000."""
        low = query.lower().replace("ё", "е")
        self._tenant = bool(re.search(r"(?<!не )(?<!не являемся )арендатор", low)) and not _NOT_TENANT_RE.search(low)
        return [h for h in hits if not self._tenant_only(h.chunk)]

    @staticmethod
    def _rows_with_format(ranked: list[SearchHit], fmt: set[str]) -> list[SearchHit]:
        """Все строки прайса с этими словами в формате. Слишком общие слова («брендирование» —
        в десятках строк) не считаем: перечень из сорока позиций клиенту не поможет."""
        rows: dict[str, SearchHit] = {}
        for token in fmt:
            matched = [h for h in ranked if h.chunk.kind == "price" and (token in h.chunk.fmt or token in h.chunk.place)]
            if len(matched) <= LIST_LIMIT:
                rows.update((h.chunk.id, h) for h in matched)
        return sorted(rows.values(), key=lambda h: h.score, reverse=True)

    def normalize(self, query: str) -> list[str]:
        """Токены запроса, приведённые к словам базы: клиенты пишут как попало.

        - синонимы предметной области: «кино» → кинотеатр, «машина» → автомобиль;
        - слитное написание: «фудкорт», «медиакуб» → фуд корт, медиа куб;
        - опечатка в слове той же длины: «экарн» → экран. Слова другой длины не трогаем,
          иначе «метро» стало бы «метр», и бот нашёл бы ответ на то, чего в базе нет.
        """
        vocab = self._bm25.idf
        out: list[str] = []
        # «не» — стоп-слово, и «мы не арендаторы» превращалось в «арендаторы»: находились не те цены
        query = _NOT_TENANT_RE.sub("внешние рекламодатели", query.lower().replace("ё", "е"))
        for word in _WORD_RE.findall(query):
            if word in STOPWORDS:
                continue
            token = stem(word)
            if token in _SYNONYMS:
                out += _SYNONYMS[token]
            elif token in vocab or token.isdigit() or len(word) < 5:
                out.append(token)
            elif split := self._split_compound(word):
                out += split
            else:
                out.append(self._fix_typo(token) or token)
        return out

    def _split_compound(self, word: str) -> list[str] | None:
        vocab = self._bm25.idf
        for i in range(3, len(word) - 2):
            head, tail = stem(word[:i]), stem(word[i:])
            if head in vocab and tail in vocab:
                return [head, tail]
        return None

    def _fix_typo(self, token: str) -> str | None:
        same_length = [t for t in self._bm25.idf if len(t) == len(token)]
        match = difflib.get_close_matches(token, same_length, n=1, cutoff=0.8)
        return match[0] if match else None

    def matched_stop_topic(self, question: str) -> str | None:
        low = fix_mixed_script(question).lower().replace("ё", "е")  # «cкидка» с латинской c — тоже скидка
        low = _STOP_EXCEPTIONS_RE.sub(" ", low)
        for topic in self.stop_topics:
            # «Фото» в названии бизнеса и «коммерческие форматы» не означают просьбу прислать материал.
            # Исключение только для широких стандартных начал: точные стоп-темы менеджера остаются в силе.
            candidate = _STOP_CONTEXT_EXCEPTIONS[topic].sub(" ", low) if topic in _STOP_CONTEXT_EXCEPTIONS else low
            # начало слова, но не вторая часть через дефис: «промо-акция» — формат рекламы, не скидка.
            # «отс!» — только целое слово: «ОТС у экрана», но не «отсутствует»
            pattern = re.escape(topic[:-1]) + r"\b" if topic.endswith("!") else re.escape(topic)
            if re.search(r"(?<![\w-])" + pattern, candidate):
                return topic
        return None

    def ungrounded_focus(self, question: str, answer: str, context: str) -> list[str]:
        """Слова, о которых клиент спросил «да или нет», модель ответила утвердительно, а в найденном
        контексте их нет. «Есть ли звук на экранах?» при базе, где про звук ни слова, — «звук присутствует»
        выдумано. Числа и цены ловят другие проверки, здесь — свойства без чисел. Отказ («нет, у нас только…»)
        не трогаем: опасно обещание, а не честное «нет»."""
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", question) if s.strip()]
        asked = next((s for s in reversed(sentences) if _YES_NO_RE.search(s) and not _WH_RE.match(s)), "")
        head = " ".join(re.split(r"(?<=[.!?])\s+", answer.strip())[:2])
        if not asked or _REFUSAL_RE.search(head):
            return []
        focus = set(self.normalize(asked)) - self._all_venues - _FOCUS_SKIP
        said, seen = set(tokenize(answer)), set(tokenize(context))
        seen |= {full for short, full in _ABBREVIATIONS.items() if short in seen}  # «ролик до 15 сек»
        missing = []
        for word in sorted(focus):
            if len(word) < 3 or word.isdigit() or word not in said:
                continue
            if word in seen or set(_SYNONYMS.get(word, [])) & seen:
                continue
            # «забрендировать» при «брендирование», «размещение» при «разместить» — одно и то же
            if len(word) >= 5 and any(len(w) >= 5 and (word[:5] in w or w[:5] in word) for w in seen):
                continue
            missing.append(word)
        return missing

    def allowed_amounts(self) -> set[int]:
        """Суммы, которые бот может называть: цены прайса и суммы из примечаний
        к позициям (печать, монтаж, цена в высокий сезон)."""
        amounts = {row.price_rub for row in self.prices}
        for row in self.prices:
            amounts.update(extract_amounts(row.notes))
        return amounts


# Название ТЦ — часть места до « — »: «ВЕЕР Молл, Екатеринбург — фуд-корт» → {веер}.
# Общие слова и город Екатеринбург не различают ТЦ, поэтому их не учитываем.
_GENERIC_VENUE_WORDS = {stem(w) for w in "тц трц молл екатеринбург нижний ул пр им".split()}
# Как ТЦ называют в переписке: «аутлет» — BrandStoriesOutlet, «Академ» — ТРЦ «Академический»
_VENUE_ALIASES = {"veer": stem("веер"), "depo": stem("депо"), "outlet": "brandstoriesoutlet",
                  # Snowball режет падежи короткого слова по-разному («академ» → акад, «академе» → академ)
                  **{stem(w): "brandstoriesoutlet" for w in "аутлет аутлете аутлета аутлеты".split()},
                  **{stem(w): stem("академический") for w in "академ академе академа академу".split()},
                  # «ФанФан» слитно, в прайсе «Фан-Фан»
                  **{stem(w): "фан" for w in "фанфан фанфане фанфана".split()}}


def _format_words(fmt: frozenset[str]) -> frozenset[str]:
    """Слова формата без размеров и номеров: у «Баннер № 1, 7,4×4 м» — только «баннер»."""
    return frozenset(t for t in fmt if not t.isdigit() and len(t) > 2)


def venue_tokens(placement: str) -> frozenset[str]:
    # Адрес в скобках — не название: иначе «Ясно, а экран?» находило Фан-Фан (ул. Ясная),
    # а «экран на Космонавтов» — только Омегу (ВЕЕР тоже на Космонавтов)
    head = re.sub(r"\s*\(.*?\)", "", placement.split(" — ", 1)[0])
    return frozenset(t for t in tokenize(head) if t not in _GENERIC_VENUE_WORDS and not t.isdigit())


def place_tokens(placement: str) -> frozenset[str]:
    """Место внутри ТЦ — часть после « — »: «Бутово Молл, Москва — парковка −1» → {парковк}."""
    _, _, place = placement.partition(" — ")
    return frozenset(t for t in tokenize(place) if not t.isdigit() and len(t) > 2) - _GENERIC_VENUE_WORDS


_POSITION_RE = re.compile(
    r"\bмест(?:о|е|а|у|ом|ам|ах)\s*(?:№\s*|номер(?:ом|а)?\s*)?"
    r"(?P<numbers>\d+(?:\s*[-–—]\s*\d+)?"
    r"(?:\s*(?:,|\bи\b|\bили\b)\s*(?:№\s*)?\d+(?:\s*[-–—]\s*\d+)?)*)\b", re.I)
_POSITION_PERIOD_RE = re.compile(r"\s*(?:месяц\w*|мес\.?|недел\w*|день|дня|дней|сут\w*|год\w*)\b", re.I)


def position_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Только номера после «место»: списки и диапазоны без разворачивания диапазона в память."""
    positions = []
    for match in _POSITION_RE.finditer(fix_mixed_script(text)):
        if not re.search(r"№|номер", match.group(0), re.I) and _POSITION_PERIOD_RE.match(text[match.end():]):
            continue  # «Нужно место 1 месяц» — срок, а не место № 1.
        for item in re.findall(r"\d+(?:\s*[-–—]\s*\d+)?", match.group("numbers")):
            ends = [int(value) for value in re.split(r"\s*[-–—]\s*", item)]
            positions.append((min(ends), max(ends)))
    return tuple(positions)


# «1 месяц при размещении от 6 месяцев»: до N — от месяца, от N и на N — от N месяцев
_TIER_RE = re.compile(r"при размещении (до|от|на) (\d+) месяц")
LIST_LIMIT = 20
LIST_FAQ_LIMIT = 3  # в перечне — немного разделов заметок, иначе модель теряет строки прайса
SMALL_VENUE_FORMATS = 3
VENUE_NOTE_WEIGHT = 0.8
FAQ_TOP_K = 5  # из 8 мест выдачи — не больше 5 разделов FAQ и заметок
PART_TOP_K, PART_EXTRA_LIMIT = 3, 9  # для сообщений из нескольких вопросов
_PARTS_RE = re.compile(r"\s*(?:\d\)|;|\n|\?|\bа также\b|\s+или\s+|\s+и\s+(?=(?:ещ[её]\s+)?\w+\s+\w+))\s*", re.IGNORECASE)  # больше строк прайса за раз — уже не перечень, а повод спросить, что интересует

# Клиент спрашивает, что вообще есть, — нужен полный перечень, а не лучшие совпадения
_LIST_RE = re.compile(r"\b(?:как(?:ие|их)|есть|варианты?|список|перечисл\w*|разн(?:ых|ые)|"
                      r"други[ех]|еще|где|форматы?|предлагаете|ассортимент|выбор|всех|сетк\w*|распиш\w*|по точкам)\b")
# Хочет рекламу, но не назвал ни ТЦ, ни формат — покажем каталог, а не «Уточню и вернусь»
_INTENT_RE = re.compile(r"реклам|размест|купить|куплю|заказ|приобр|сотруднич|предлож|формат|вариант|услуг|прайс"
                        r"|делаете|занимаетесь|оказываете|есть ли у вас|у вас есть")
# Чего у нас нет: каталог нужен всегда, чтобы честно сказать «такого нет, зато есть в ТЦ…»
_OFF_RANGE_RE = re.compile(r"\bметро\b|телевиден|\bтв\b|перв\w* канал|таргет|радиостанц|растяжк|инстаграм"
                           r"|вконтакт|директ|\bсмм\b|блогер|газет|журнал")
# Формат без размеров и номеров: «Подвесные флаги, до 3 шт. 2,5×6 м» → «Подвесные флаги»
_FAMILY_CUT_RE = re.compile(r"\s*(?:[,(:№]|\d|\bдо\b|\bдля\b).*$")
_TRAILING_PREP_RE = re.compile(r"\s+(?:с|на|в|до|для)$")


_BUDGET_RE = re.compile(r"\b(?:бюджет\w*(?:\s+на\s+рекламу)?|до|в пределах|не дороже|не больше|максимум|на|дешевле)"
                        r"[\s:—–-]+(?:(?:около|примерно|порядка|где-то|в районе|до)\s+)?"
                        r"(?:\d[\d \u00a0\u202f]*\s*[-–—]\s*)?(\d[\d \u00a0\u202f]*(?:[.,]\d+)?)[ \u00a0]*"
                        r"(млн\w*|тыс\w*|т\.?р\.?|к\b|k\b|руб\w*|₽|р\.?(?!\w))?", re.IGNORECASE)
# «на 40 тысяч талонов», «на 1488 выходов» — количество, а не деньги
_COUNT_AFTER_RE = re.compile(r"\s*(?:талон|выход|показ|шт|экземпляр|макет|человек|мес|дн|нед|год|м²|м\b|кв)")


_CITY_TITLES = {"ekb": "Екатеринбург", "nt": "Нижний Тагил", "msk": "Москва", "orb": "Оренбург"}
# Слова, которые есть в названиях форматов, но о формате не говорят: «для внешних рекламодателей»
_SUMMARY_SKIP = frozenset(stem(w) for w in "внешних рекламодателей арендаторов трц тц цифровой".split())
_CITY_TOKENS = frozenset(stem(w) for w in "москва тагил оренбург екатеринбург".split())
_CITY_RE = re.compile(r"(?P<ekb>екатеринбург\w*|\bекб\b|\bебург\w*)|(?P<nt>тагил\w*)|(?P<msk>москв\w*)|(?P<orb>оренбург\w*)")
_CITY_NAMES = {"ekb": "екатеринбург", "nt": "тагил", "msk": "москва", "orb": "оренбург"}
_CHEAPEST_RE = re.compile(r"сам\w* (?:дешев|недорог|бюджетн|доступн)|минимальн\w* (?:цен|стоимост)|\b(?:по)?дешевле\b")


def parse_budget(text: str) -> int | None:
    """«бюджет 30 тысяч», «до 50 000 ₽», «на 30к» → сумма в рублях. Без денег рядом — не бюджет:
    «на 3 месяца», «до 12 м²» не считаются."""
    for m in _BUDGET_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").strip()
        # без денег рядом это не бюджет: «цена 25 000 ₽» не бюджет (нет триггера), «на 2027 год» — тоже
        if not unit and "бюджет" not in text[max(0, m.start() - 20):m.end()]:
            continue
        if _COUNT_AFTER_RE.match(text, m.end()):
            continue
        number = float(re.sub(r"\s", "", raw).replace(",", "."))
        value = int(number * (1_000_000 if unit.startswith("млн") else 1000 if unit.startswith(("тыс", "т", "к", "k")) else 1))
        if value >= 1000:
            return value
    return None


def spread_by_venue(rows: list[SearchHit]) -> list[SearchHit]:
    """Без повторов, по одной строке от каждой площадки по кругу: А1, Б1, В1, А2, Б2…"""
    groups: dict[str, list[SearchHit]] = {}
    seen: set[str] = set()
    for h in rows:
        if h.chunk.id not in seen:
            seen.add(h.chunk.id)
            groups.setdefault(h.chunk.group, []).append(h)
    out: list[SearchHit] = []
    while any(groups.values()):
        for group in groups.values():
            if group:
                out.append(group.pop(0))
    return out


def format_family(fmt: str) -> str:
    return _TRAILING_PREP_RE.sub("", _FAMILY_CUT_RE.sub("", fmt)).strip() or fmt


def catalog_entry(row: PriceRow) -> tuple[str, str]:
    """(площадка, что там есть) для строки прайса."""
    head = re.sub(r"\s*\(.*?\)", "", row.placement.split(" — ", 1)[0]).strip()
    if "инцентр" in row.format.lower():
        # Экраны Инцентра стоят в разных ТЦ города — одна площадка со списком ТЦ
        return "цифровые экраны Инцентра в ТЦ Екатеринбурга", head.replace(", Екатеринбург", "")
    return head, format_family(row.format)


def build_catalog(prices: list[PriceRow]) -> list[Chunk]:
    """Каталог: какие форматы есть на каждой площадке, без цен. Строится из прайса сам,
    поэтому правка prices.csv в админке сразу меняет и каталог."""
    items: dict[str, list[str]] = {}
    venues: dict[str, frozenset[str]] = {}
    for row in prices:
        key, item = catalog_entry(row)
        known = items.setdefault(key, [])
        if tuple(tokenize(item)) not in {tuple(tokenize(k)) for k in known}:
            known.append(item)
        venues[key] = venues.get(key, frozenset()) | venue_tokens(row.placement)
    # Площадки с парой форматов из прайса Инцентра (промо, радио) — одной строкой на город: из 14 строк каталога
    # модель на «хочу рекламу» не перечисляла ничего и спрашивала город
    small: dict[str, list[str]] = {}
    for key, found in items.items():
        name, _, city = key.rpartition(", ")
        if name and len(found) <= SMALL_VENUE_FORMATS:
            small.setdefault(city, []).append(key)
    for city, keys in small.items():
        if len(keys) < 2:
            continue
        formats = list(dict.fromkeys(f[:1].lower() + f[1:] for k in keys for f in items[k]))
        names = ", ".join(k.rpartition(", ")[0] for k in keys)
        group = f"{', '.join(formats)} — {names} ({city})"
        items[group] = [f"{k.rpartition(', ')[0]} — {', '.join(items.pop(k))}" for k in keys]
        venues[group] = frozenset().union(*(venues.pop(k) for k in keys))
    return [Chunk(f"catalog-{i}", "catalog", key, f"Все форматы — {key}: {'; '.join(found)}.",
                  venue=venues[key])
            for i, (key, found) in enumerate(items.items())]


# Как клиенты называют то, что в прайсе записано иначе (ключи и значения — основы слов)
_SYNONYMS = {stem(k): [stem(w) for w in v.split()] for k, v in {
    "кино": "кинотеатр", "кинозал": "кинотеатр зал", "фильм": "кинотеатр фильм",
    "машина": "автомобиль", "машину": "автомобиль", "тачка": "автомобиль", "авто": "автомобиль",
    "телик": "экран", "монитор": "экран", "плазма": "экран",
    "вывеска": "лайтбокс",
    "билборд": "уличный экран фасад", "билборды": "уличный экран фасад", "наружка": "уличный экран фасад",
    "наружная": "уличный экран фасад", "наружную": "уличный экран фасад",
    # Расширяем на два слова, не на всю группу: иначе короткий раздел «Откуда берутся цифры посещаемости»
    # обгонял «Сколько людей в будни и в выходные» на вопрос «сколько посетителей в выходные»
    **{w: "посещаемость трафик людей" for w in "проходимость проходимости посещаемость посещаемости народу народ людей".split()},
    **{w: "трафик посещаемость людей" for w in "трафик трафика".split()},
    **{w: "посетители посещаемость трафик людей" for w in "посетителей посетители".split()},
    "напечатаете": "печать", "напечатать": "печать", "напечатаем": "печать",
    "смонтируете": "монтаж", "смонтировать": "монтаж", "смонтируем": "монтаж",
    "возраст": "возраст лет требования",
    **{w: "пакет" for w in "пакетное пакетный пакетного пакетные пакетом пакетно".split()},
    "пару": "2",  # «на пару дней» — это срок: включает поиск по прайсу
    **{w: "кинотеатр фильм сеанс" for w in "мультик мультики мультиков мультиками мультфильм мультфильмы".split()},
    **{w: "брендирование" for w in "заклеить оклеить обклеить наклеить заклеим оклеим".split()},
    **{w: "свободных" for w in "пустые пустых пустую пустой".split()},
}.items()}
_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
# Вопрос «да или нет»: «есть ли…», «можно ли…», «а со звуком можно?»; «какое оборудование есть?» — не такой
_YES_NO_RE = re.compile(r"(?:^|[\s,.!?])(?:есть|можно|бывает|будет|делаете|предоставляете|входит|включ\w*|разрешен\w*"
                        r"|допускается|работают|поддерживают|умеете)\s+ли\b|\b(?:можно|есть|разрешено)\b[^.!?]*\?",
                        re.IGNORECASE)
_WH_RE = re.compile(r"^\W*(?:а\s+|и\s+|здравствуйте\W+)?(?:(?:в|на|из|для|с|о|об|по|за|у|до|от)\s+)?"
                    r"(?:как\w*|что|чем|где|когда|сколько|кто|куда|почему|зачем|чего|кому|котор\w*|чей|чья|чьи)\b",
                    re.IGNORECASE)
_REFUSAL_RE = re.compile(r"^(?:нет\b|к сожалению)|\bу нас нет\b|отсутству|\bне (?:размеща|работа|предлага|дела|занима"
                         r"|осуществля|оказыва|продаём|продаем)|нет возможности|невозможно|нельзя|только (?:внутри|в торгов)",
                         re.IGNORECASE)
_ABBREVIATIONS = {"сек": stem("секунд"), "мин": stem("минут"), "мес": stem("месяц"), "шт": stem("штук")}
_FOCUS_SKIP = {stem(w) for w in (
    "есть можно ли реклама рекламу рекламы рекламный размещение разместить размещать стоит стоимость сколько цена "
    "цены ваш вы мы нас наш наша наши свой тц трц молл здравствуйте добрый день подскажите скажите пожалуйста хочу "
    "нужно надо интересует интересно вариант варианты место места делаете делать предложение услуга условия месяц "
    "неделя день год который которая которые котором разные разных такой такие этот эта это тоже ещё еще").split()}
_NOT_TENANT_RE = re.compile(r"\bне\s+(?:являемся\s+)?арендатор\w*")

# Синонимы для запросов про деньги: «сколько стоит» должно находить прайс
_PRICE_WORDS = {stem(w) for w in "сколько стоит стоимость цена цены прайс расценки почем тариф бюджет".split()}
_PRICE_EXPANSION = [stem(w) for w in ("цена", "стоимость", "прайс")]
_PERIOD_WORDS = {stem(w) for w in "день дня дней неделя недели недель неделю месяц месяца месяцев сутки суток".split()}


def expand_query(tokens: list[str]) -> list[str]:
    # «промоутеры в депо на 3 дня» — тоже вопрос о цене, хоть и без «сколько стоит»
    if any(t in _PRICE_WORDS for t in tokens) or (any(t.isdigit() for t in tokens)
                                                  and any(t in _PERIOD_WORDS for t in tokens)):
        return tokens + _PRICE_EXPANSION
    return tokens


def parse_faq(text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    title, body = None, []

    def flush() -> None:
        if title and any(line.strip() for line in body):
            answer = "\n".join(body).strip()
            chunks.append(Chunk(f"faq-{len(chunks)}", "faq", title,
                                f"Вопрос: {title}\nОтвет: {answer}",
                                tokenize(title) * 2 + tokenize(answer)))  # заголовок весит больше

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            title, body = line[3:].strip(), []
        elif line.startswith("#"):
            continue  # комментарии и заголовок файла
        elif title is not None:
            body.append(line)
    flush()
    return chunks


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_note(text: str, name: str, start: int = 0) -> list[Chunk]:
    """Заметка: разделы «## вопрос или тема», над ними можно шапку Obsidian с полем «тц:».
    ТЦ из шапки дописывается к каждому разделу — чтобы «требования к макету» нашлись для нужного ТЦ."""
    venue = ""
    if m := _FRONTMATTER_RE.match(text):
        meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        venue = meta.get("тц", "").strip()
        text = text[m.end():]
    chunks = parse_faq(text)
    for i, c in enumerate(chunks):
        c.id = f"faq-{start + i}"
        if venue:
            c.text = f"{venue}. {c.text}"
            c.tokens += tokenize(venue) * 2
            c.venue = frozenset(_VENUE_ALIASES.get(t, t) for t in venue_tokens(venue))
    return chunks


def parse_prices(text: str) -> list[PriceRow]:
    rows = []
    for r in csv.DictReader(text.splitlines()):
        try:
            price = int(re.sub(r"\D", "", r["price_rub"]))
        except (KeyError, ValueError):
            continue
        rows.append(PriceRow(r.get("format", "").strip(), r.get("placement", "").strip(),
                             r.get("period", "").strip(), price, r.get("notes", "").strip()))
    return rows


def prices_csv(rows: list[PriceRow]) -> str:
    """Обратно в prices.csv — для правки прайса из админки."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["format", "placement", "period", "price_rub", "notes"])
    for r in rows:
        writer.writerow([r.format, r.placement, r.period, r.price_rub, r.notes])
    return out.getvalue()


def parse_stop_topics(text: str) -> list[str]:
    return [l.strip().lower().replace("ё", "е") for l in text.splitlines() if l.strip() and not l.startswith("#")]


_STOP_CONTEXT_EXCEPTIONS = {
    "фото": re.compile(
        r"(?<![\w-])(?:фото-?(?:студи|салон|магазин)\w*"
        r"|фотограф(?:а|у|ом|е|ы|ов|ам|ами|ах)?)\b"),
    "коммерческ": re.compile(r"(?<![\w-])коммерческ\w*\s+формат\w*\b"),
}


# Похоже на стоп-тему, но это вопрос про наш товар: «промо акция с промоутером» — формат из прайса,
# «за чей счёт печать?» — не про счёт на оплату, «оплата печати отдельно?» отвечает FAQ
_STOP_EXCEPTIONS_RE = re.compile(
    r"промо[\s-]*акци\w*|акци\w* с (?:\d+ )?промоутер\w*"
    r"|за (?:чей |наш |свой |ваш )?сч[её]т\w*|сч[её]тчик\w*"
    r"|оплат\w* (?:печат|монтаж|производств)\w*|(?:печат|монтаж|производств)\w* оплачива\w*"
    r"|оплачива\w* отдельно"
    r"|пришл(?:а|о)\b|приш[её]л\b|фотозон\w*|физическ\w* (?:размер|габарит)\w*|кпп\b|ипотек\w*"
    r"|индивидуальн\w* (?:макет|размер|дизайн|конструкц|формат)\w*|охват(?:ить|им|ывать)\b"
    r"|проходимост\w* (?:у |в )?кино\w*|перев\w* (?:ролик|макет|текст)\w*|без скид\w*")
