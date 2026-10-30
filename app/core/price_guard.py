"""Защита от выдуманных цен и цифр.

Модели запрещено считать и придумывать суммы. После генерации проверяем ответ двумя способами:
1. Денежные суммы (с «₽», «руб.») сверяются с прайсом.
2. Любое число от 1000 должно встречаться в контексте, вопросе или истории диалога —
   так ловится цена, написанная без валюты («стоит 27 тысяч»), и выдуманная статистика.
Если найдено лишнее число, ответ не уходит клиенту, а передаётся менеджеру.
"""
from __future__ import annotations

import re

_MULTIPLIER = r"(тыс\.?|тысяч[а-яё]*|млн\.?|миллион[а-яё]*)"

# 25 000 ₽ / 25000 руб / 25 000 р. / 25 тыс. руб / 1,5 млн ₽
_AMOUNT_RE = re.compile(
    r"(\d[\d\s  ]*)(?:[.,](\d+))?\s*" + _MULTIPLIER + r"?\s*(?:₽|руб\.?|рублей|рубля|рубль|р\.)",
    re.IGNORECASE,
)

# Любое число: 25 000 / 25000 / 1,5 / 25 тыс. / 27к / 1,5 млн
_NUMBER_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d+))?(?:\s*" + _MULTIPLIER + r"|(к)(?![а-яё]))?",
    re.IGNORECASE,
)


def _value(whole: str, fraction: str | None, multiplier: str | None) -> int:
    digits = re.sub(r"\D", "", whole)
    value = float(f"{digits}.{fraction}") if fraction else float(digits)
    if multiplier:
        value *= 1_000_000 if multiplier.lower().startswith(("млн", "миллион")) else 1000
    return int(round(value))


def extract_amounts(text: str) -> list[int]:
    """Денежные суммы — числа с «₽», «руб.» и т. п."""
    amounts = []
    for m in _AMOUNT_RE.finditer(text):
        if re.sub(r"\D", "", m.group(1)):
            amounts.append(_value(m.group(1), m.group(2), m.group(3)))
    return amounts


def amount_spans(text: str) -> list[tuple[int, int, int]]:
    """Денежная сумма и её границы для проверки связанного с ней срока."""
    return [(_value(m.group(1), m.group(2), m.group(3)), m.start(), m.end()) for m in _AMOUNT_RE.finditer(text)
            if re.sub(r"\D", "", m.group(1))]


def extract_numbers(text: str) -> list[int]:
    """Все числа в тексте, с учётом «тыс.», «млн» и «к»."""
    return [_value(m.group(1), m.group(2), m.group(3) or m.group(4)) for m in _NUMBER_RE.finditer(text)]


def unknown_amounts(answer: str, allowed: set[int]) -> list[int]:
    """Суммы в ответе, которых нет в прайсе."""
    return [a for a in extract_amounts(answer) if a not in allowed]


def ungrounded_numbers(answer: str, seen: str, allowed: set[int] = frozenset(), min_value: int = 1000) -> list[int]:
    """Числа от min_value в ответе, которых нет ни в seen (контекст, вопрос, история), ни в прайсе."""
    known = set(extract_numbers(seen)) | set(allowed)
    return [n for n in extract_numbers(answer) if n >= min_value and n not in known]


def computed_totals(bad: list[int], seen: str, question: str = "", multipliers: dict[int, set[float]] | None = None) -> bool:
    """Все лишние числа — итоги из настоящих цен: цена × срок, который назвал клиент
    («на полгода»: 18 000 × 6 = 108 000), или цена + доплата (60 000 + 41 000 = 101 000).
    Это запрещённая арифметика, а не выдумка: её можно вырезать. Умножение на что-то другое
    («в декабре коэффициент 2» для машины) — неверная посылка, такой ответ уходит менеджеру."""
    known = {n for n in extract_numbers(seen) if n >= 1000}
    sums = {a + b for a in known for b in known if a != b}  # 280 000 + 280 000 — это ×2, а не цена + доплата
    sums |= {abs(a - b) for a in known for b in known if a != b}  # «разница 10 500 ₽» между двумя ценами
    terms = months_named(question)
    # «В декабре наценка 20%» у infodesk: 5 000 × 1,2 = 6 000 — посчитано по коэффициенту из той же строки
    seasonal = {round(price * m) for price, ms in (multipliers or {}).items() for m in ms}
    return bool(bad) and all(any(n == a * k for a in known for k in terms) or n in sums or n in seasonal for n in bad)


_COEF_RE = re.compile(r"коэффициент\w*\s+(\d+(?:[.,]\d+)?)|наценк\w*\s+(\d+)\s*%", re.IGNORECASE)


def row_multipliers(notes: str) -> set[float]:
    """Коэффициенты, записанные в строке прайса: «коэффициент 2» → 2, «наценка 20%» → 1,2."""
    out = set()
    for m in _COEF_RE.finditer(notes):
        out.add(float(m.group(1).replace(",", ".")) if m.group(1) else 1 + int(m.group(2)) / 100)
    return out


_MONTHS_RE = re.compile(r"(\d+)\s*(?:мес|месяц)|(полгода|пол года)|(квартал)|(\bгод\b)", re.IGNORECASE)


def months_named(question: str) -> set[int]:
    """Сроки из вопроса в месяцах: «на 3 месяца» → {3}, «на полгода» → {6}."""
    out = set()
    for m in _MONTHS_RE.finditer(question):
        out.add(int(m.group(1)) if m.group(1) else 6 if m.group(2) else 3 if m.group(3) else 12)
    return {k for k in out if k >= 2}


_SENTENCE_PARTS_RE = re.compile(r"[^.!?\n]+[.!?]*\s*")
_CLAUSE_RE = re.compile(r"[^,;:]+[,;:]?\s*")
_ARITHMETIC_RE = re.compile(r"умнож|сложи|итого|в сумме|[×*xх]\s*\d|=", re.IGNORECASE)


_TOTAL_BRACKET_RE = re.compile(r"(?<!\d)(?<!\d )(?<!\d\u00a0)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s*(?:₽|руб\w*\.?)\s*\(по\s+"
                               r"(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s*(?:₽|руб\w*\.?)([^()]*)\)")


def drop_bracket_totals(text: str) -> str:
    """«…на 2 дня стоит 160 000 ₽ (по 80 000 ₽ в день)» → «…80 000 ₽ в день»: итог — кратное цене в скобках."""
    def repl(m: re.Match) -> str:
        total, unit = (int(re.sub(r"\D", "", g)) for g in m.group(1, 2))
        return f"{m.group(2)} ₽{m.group(3)}" if unit and total > unit and total % unit == 0 else m.group(0)
    return _TOTAL_BRACKET_RE.sub(repl, text)


def drop_numbers(text: str, numbers: list[int]) -> str:
    """Убрать из ответа посчитанные итоги: «18 000 ₽ в месяц, итого 108 000 ₽.» → «18 000 ₽ в месяц.»
    Если от фразы не остаётся части с ценой, фраза уходит целиком."""
    bad, out = set(numbers), []
    for number in bad:
        # «94 500 рублей (по 31 500 рублей за месяц)» → «31 500 рублей за месяц»
        digits = r"[\s\u00a0]?".join(str(number))
        text = re.sub(rf"(?<!\d){digits}(?!\d)\s*(?:₽|руб\w*\.?)?\s*\((?:по|это)?\s*([^()]*\d[^()]*)\)", r"\1", text)
    for sentence in _SENTENCE_PARTS_RE.findall(text):
        if not set(extract_numbers(sentence)) & bad:
            out.append(sentence)
            continue
        kept = [c for c in _CLAUSE_RE.findall(sentence.rstrip())
                if not set(extract_numbers(c)) & bad and not _ARITHMETIC_RE.search(c)]
        if any(extract_amounts(c) for c in kept):
            ending = sentence[len(sentence.rstrip()):]
            out.append(re.sub(r"[\s,;:.]+$", "", "".join(kept)) + "." + (ending or " "))
    result = "".join(out).strip()
    # «/мес x 3 мес). печати…» — от фразы остался обрывок: лучше отдать менеджеру, чем такое клиенту
    return result if re.match(r"[А-ЯЁA-Z0-9«]", result) else ""


# «В стоимость включены печать и монтаж» — в прайсе они почти всегда оплачиваются отдельно.
# Цифры в таком ответе настоящие, поэтому проверка сумм его не ловит; ловим само «включено».
_INCLUDED_RE = re.compile(r"(?<!не )(?:включен\w*|включено|входит|входят|бесплатн\w*)|с уч[её]том|вместе с"
                          r"|платить не нужно|не нужно платить|без доплат|под ключ", re.IGNORECASE)
# «включает», «включая» — обещание, только если в той же фразе речь о цене:
# «заказчик обеспечивает оформление, включая монтаж» — не про деньги
_INCLUDES_RE = re.compile(r"(?<!не )включа\w*", re.IGNORECASE)
_PRICE_TALK_RE = re.compile(r"стоимост|цен[аеуы]|₽|руб|оплат", re.IGNORECASE)
_EXTRA_SERVICES = ("печат", "монтаж", "дизайн", "производств", "изготовл")
_SENTENCE_RE = re.compile(r"[^.!?\n]+")
# «Дополнительные расходы включают печать» — это про доплату, а не про «входит в цену»
# «Условия включают макет 1,99×1,5 м» — про условия; «но не включает печать» — честное «отдельно»
_CONDITIONS_RE = re.compile(r"услови\w* включа|\bне (?:включа\w*|входит|входят)")
# «Заказчик самостоятельно обеспечивает оформление, включая монтаж» — делает клиент, а не «входит в цену»
_SELF_PROVIDED_RE = re.compile(r"заказчик|самостоятельно|своими силами|вы сами|клиент сам")
# «НДС включён, ролик готовит рекламодатель, печать и монтаж не нужны» — «включён» дальше не действует.
# Но «печать — платить не нужно» — это как раз «входит в цену»
_NOT_NEEDED_RE = re.compile(r"(?<!платить )\bне (?:нужн|требу)\w*\b(?!\s+(?:платить|доплачивать|оплачивать))|\bнет\b"
                            r"|готовит рекламодател|не учитыва\w*|без учета|не входя\w*")
_SURCHARGE_RE = re.compile(r"(?:дополнительн|отдельн|доплат|сверх)\w*[^.!?\n]*?\bвключа|оплачива\w* (?:дополнительн|отдельн)"
                           r"|(?:дополнительн|отдельн)\w* оплачива|\bотдельно\b(?![^.!?\n]*включ)")


_WORKS_RE = re.compile(r"(\d[\d\s]*)₽[^\n]*?стоимость работ: ([^;\n]*)")


def _priced_works(answer: str, known: str) -> str:
    """«Производство бортов — 318 720 ₽. Стоимость работ: печать, монтаж»: работы и есть эта цена. Только если ответ
    называет лишь такие цены: у самих бортов за 1 500 000 ₽ производство и монтаж — доплата."""
    works = {int(re.sub(r"\D", "", m[1])): m[2] for m in _WORKS_RE.finditer(known)}
    named = set(extract_amounts(answer))
    return " ".join(works.values()) if named and named <= works.keys() else ""


def unsupported_inclusions(answer: str, seen: str) -> list[str]:
    """Услуги, которые ответ называет включёнными в цену, хотя в базе так не написано."""
    known = seen.lower()
    bad = []
    works = _priced_works(answer, known)
    for sentence in _SENTENCE_RE.findall(answer.lower().replace("ё", "е")):  # «включён»
        # По частям фразы: «Печать оплачивается дополнительно, монтаж включён в цену» — вторая часть врёт
        # «Включены НДС, печать и монтаж» — «включены» действует и на следующие части перечня,
        # пока не встретится «оплачивается отдельно»
        if _SELF_PROVIDED_RE.search(sentence):
            continue
        claiming = False
        for clause in re.split(r"[,;:]", sentence):
            if _SURCHARGE_RE.search(clause) or _CONDITIONS_RE.search(clause) or _NOT_NEEDED_RE.search(clause):
                claiming = False
                continue
            if _INCLUDED_RE.search(clause) or (_INCLUDES_RE.search(clause) and _PRICE_TALK_RE.search(answer)):
                claiming = True
                bad += [s for s in _unknown_services(clause, known, bad) if s not in works]
            elif claiming and not extract_amounts(clause) and not re.search(r"обойд|стоит|составля", clause):
                # «включены НДС, печать и монтаж» — продолжение перечня; «печать обойдётся в 7 950 ₽» — доплата
                bad += [s for s in _unknown_services(clause, known, bad) if s not in works]
            else:
                claiming = False
    return bad


def _unknown_services(clause: str, known: str, already: list[str]) -> list[str]:
    bad = []
    for service in _EXTRA_SERVICES:
        # «включает печать за 14 000 ₽ и монтаж за 16 800 ₽» — у услуги своя цена: это доплата, а не «входит»
        if re.search(rf"{service}\w*\s+(?:за|стоимостью|—|–|-|:)?\s*[1-9]", clause):  # «монтаж — 0 ₽» — это «входит»
            continue
        if service in clause and service not in already and not re.search(
                rf"{service}\w*\s+(?:\w+\s+)?(?:включ|вход)|(?:включ\w*|входит|входят)\s+(?:\w+\s+)?{service}",
                known):
            bad.append(service)
    return bad
