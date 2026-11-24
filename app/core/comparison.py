"""Справочное сравнение двух выбранных клиентом позиций прайса."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .answer_grounding import billing_periods, check_price_periods, periods
from .assistant import ANSWER, ESCALATE, R_NO_CONTEXT, Assistant, Reply
from .conversation import (CLARIFICATIONS, Conversation, Placement, _ADDITION, _BOTH,
                           _DURATION_MODIFIER, _ITEM_NUMBER, _SEASON)
from .knowledge import KnowledgeBase, SearchHit, canonical_query, format_rub
from .price_guard import extract_amounts
from .price_options import months, option_cost


# Только цена и короткое уточнение позиции. Ответ по двум тарифам не закрывает
# просьбу о действии, даже когда её формулировки нет среди стоп-тем.
_PRICE_WORDS = re.compile(
    r"\b(?:сколько|стоят|стоить|стоит|будет|будут|обойд[еу]тся|стоимость|цен[ауы]|какая|какие|"
    r"за|в|во|на|у|и|а|по|для|тц|трц|вариант\w*|размещени\w*|вместе|подскажите|пожалуйста)\b")
_PRICE_FOLLOW_UP = re.compile(r"(?:а\s+)?(?:сколько(?:\s+(?:стоят|вместе))?|какие цены|какая стоимость)[?!. ]*")


def _simple_request(kb: KnowledgeBase, text: str, items: tuple[Placement, ...]) -> bool:
    for item in items:
        if item.period:
            text = re.sub(r"\b" + re.escape(item.period) + r"\b", " ", text)
    remainder = _PRICE_WORDS.sub(" ", _ITEM_NUMBER.sub(" ", _BOTH.sub(" ", _ADDITION.sub(" ", text))))
    known = {t for item in items for t in kb.normalize(item.description())}
    for word in re.findall(r"[\w-]+", remainder):
        tokens = kb.normalize(word)
        if not tokens or not set(tokens) <= known:
            return False
    return True


def _duration(text: str) -> tuple[str, int] | None:
    # Только полный срок. «Месяц при размещении от полугода» не месячный тариф.
    text = text.lower().removeprefix("на ").strip()
    terms = periods(text)
    if len(terms) == 1 and terms[0][1:] == (0, len(text)):
        return terms[0][0]
    return None


@dataclass(frozen=True)
class _Quote:
    text: str
    source: str = ""
    prompt: str = ""
    needs_manager: bool = False


def _quote(kb: KnowledgeBase, item: Placement) -> _Quote:
    label = item.description()
    for value, slot in ((item.venue, "venue"), (item.format, "format"), (item.period, "period")):
        if not value:
            return _Quote(label, prompt=CLARIFICATIONS[slot])
    term = _duration(_SEASON.sub("", item.period).strip())
    fmt, place = set(kb.normalize(item.format)), set(kb.normalize(item.place))
    matches = []
    for chunk in kb.chunks:
        if chunk.kind != "price" or chunk.group != item.venue or not fmt <= chunk.fmt or not place <= chunk.place:
            continue
        row = kb.prices[int(chunk.id.split("-")[1])]
        if "для арендаторов" in row.format.lower() and item.client_type != "мы арендаторы":
            continue
        if row.price_rub <= 0 or any(not periods(part) for part in row.parts()["term"]):
            continue
        if term and _duration(row.period) == term and option_cost(row, item.period) is not None:
            matches.append((chunk, row))
    if len(matches) > 1:
        return _Quote(label, prompt=CLARIFICATIONS["position"])
    if not matches:
        return _Quote(f"{label} — цену на этих условиях нужно уточнить.", needs_manager=True)
    chunk, row = matches[0]
    parts = row.parts()
    wanted = months(item.period)
    season = [s for s in parts["season"] if not wanted or months(s) & wanted]
    if wanted and season and any(unit != term for s in season for unit, _, _ in billing_periods(s)):
        return _Quote(f"{label} — сезонный тариф на этот срок нужно уточнить.", needs_manager=True)
    rates = list(season)
    if not (wanted and season):
        base = f"{format_rub(row.price_rub)} ({row.period})"
        rates.insert(0, f"базовая цена {base}" if season else base)
    details = [*rates, *parts["vat"], *parts["term"], *parts["info"]]
    needs_manager = False
    for extra in parts["extra"]:
        details.append(extra)
        if not extract_amounts(extra) and not re.search(r"бесплатно|не требу\w*|не нуж\w*", extra, re.I):
            needs_manager = True
    if needs_manager:
        details.append("Размер доплаты нужно уточнить")
    line = f"{row.placement}, {row.format} — " + ". ".join(s.rstrip(".") for s in details) + "."
    line, wrong = check_price_periods(kb, line, [SearchHit(chunk, 0.0)], label)
    if wrong:
        return _Quote(f"{label} — срок тарифа нужно уточнить.", needs_manager=True)
    return _Quote(line, chunk.id, needs_manager=needs_manager)


def comparison_reply(assistant: Assistant, state: Conversation, question: str) -> Reply | None:
    kb = assistant.kb
    text = _DURATION_MODIFIER.sub("", canonical_query(question))
    pending = state.pending.startswith("comparison_") or state.pending == "other_placement"
    both = _BOTH.search(text)
    if not (both or pending or (state.placements and _PRICE_FOLLOW_UP.fullmatch(text))):
        return None
    if state.multiple_venues:
        return None
    items = state.placements or (state._placement(),)
    has_budget = state.budget_ambiguous or state.budget or state.preparation_budget or state.placement_budget
    if has_budget or not _simple_request(kb, text, items):
        if both and state.placements:
            return Reply(ESCALATE, assistant.holding_message, 0.0, R_NO_CONTEXT)
        return None
    if len(items) == 1:
        if not items[0].description():
            return None
        prompt = f"{items[0].description()}. {CLARIFICATIONS['other_placement']}"
        return assistant._clarify(state, ("other_placement", prompt))
    quotes = [_quote(kb, item) for item in items]
    lines = [quote.text for quote in quotes]
    sources = [quote.source for quote in quotes if quote.source]
    # Отсутствующий тариф подтверждает менеджер. В карточке остаются условия
    # обеих позиций и черновик с теми ценами, для которых есть источник.
    if any(quote.needs_manager for quote in quotes):
        return Reply(ESCALATE, "\n".join(lines) if sources else assistant.holding_message, 0.0, R_NO_CONTEXT, sources)
    for index, quote in enumerate(quotes):
        if not quote.prompt:
            continue
        lines[index] += f". {quote.prompt}"
        draft = "\n".join(lines)
        reply = assistant._clarify(state, (f"comparison_{index}", draft))
        if sources:
            reply.text, reply.sources = draft, sources
        return reply
    return Reply(ANSWER, "\n".join(lines), 1.0, "comparison", sources)
