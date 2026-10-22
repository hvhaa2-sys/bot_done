"""Проверки фактов для экзамена и разметки ответов менеджером."""
from __future__ import annotations

import re


def contains(answer: str, item: str) -> bool:
    """Число целиком, «a|b» — любой вариант, «!x» — запрет."""
    plain = item.lstrip("!")
    negate = (len(item) - len(plain)) % 2
    text = norm(answer)
    found = False
    for option in filter(None, (o.strip() for o in plain.split("|"))):
        option = norm(option)
        if option.isdigit():
            if re.search(rf"(?<!\d){option}(?!\d)", text):
                found = True
                break
        elif option in text:
            found = True
            break
    return not found if negate else found


def norm(s: str) -> str:
    s = s.lower().replace("\u00a0", " ").replace("\u202f", " ").replace("ё", "е")
    return re.sub(r"(?<=\d) (?=\d{3}\b)", "", s)
