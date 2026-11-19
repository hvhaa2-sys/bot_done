"""Допустимость строки прайса по сроку и бюджету до генерации ответа."""
from __future__ import annotations

import re

from .price_guard import extract_amounts

_MONTHS = ("январ", "феврал", "март", "апрел", "ма[йея]", "июн", "июл", "август",
           "сентябр", "октябр", "ноябр", "декабр")


def months(text: str) -> set[int]:
    found = sorted((m.start(), m.end(), i) for i, name in enumerate(_MONTHS, 1)
                   for m in re.finditer(r"\b" + name + r"\w*", text, re.I))
    out = {i for _, _, i in found}
    for (_, end, first), (start, _, last) in zip(found, found[1:]):
        if re.fullmatch(r"\s*(?:[-–—]|по|до)\s*", text[end:start], re.I):
            out.update(range(first, last + 1) if first <= last else (*range(first, 13), *range(1, last + 1)))
    return out


def option_cost(row, query: str) -> int | None:
    """Цена за период строки, без пересчёта для клиента. Неизвестная наценка — не подходящий вариант."""
    from .answer_grounding import periods

    duration_query = re.sub(r"\b(?:в|за)\s+(?:(?:один|1)\s+)?месяц\b", " ", query, flags=re.I)
    requested = periods(duration_query)
    term = requested[-1][0] if requested else ("month", 1)
    quoted = periods(row.period)
    if quoted and term[0] != quoted[0][0][0]:
        return None
    if quoted and term[1] < quoted[0][0][1]:
        return None
    minimum = [p for part in row.parts()["term"] for p, _, _ in periods(part)]
    if any(unit != term[0] or count > term[1] for unit, count in minimum):
        return None
    wanted = months(query)
    cost = row.price_rub
    for part in row.parts()["season"]:
        affected = months(part)
        if wanted and affected and not wanted & affected:
            continue
        values = extract_amounts(part)
        if not values:
            return None
        cost = max(cost, *values)
    return cost


def fits_budget(row, query: str, budget: int) -> bool:
    from .answer_grounding import periods

    cost = option_cost(row, query)
    if cost is None:
        return False
    requested, billing = periods(query), periods(row.period)
    # Бюджет на несколько периодов — общий; явно ежемесячный предел сравниваем с месячной ценой.
    factor = 1
    if requested and billing and not re.search(r"(?:в|за)\s+(?:один\s+|1\s+)?месяц|ежемесяч", query, re.I):
        units, count = requested[-1][0]
        bill_units, bill_count = billing[0][0]
        if units == bill_units:
            factor = (count + bill_count - 1) // bill_count
    return cost * factor + row.extras_total() <= budget
