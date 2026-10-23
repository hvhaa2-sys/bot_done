"""Явно отменённые просьбы в текущей реплике; история и слова менеджера не авторитетны."""
from __future__ import annotations

import re

from .text import fix_mixed_script

_TOPIC = r"(?:скидк\w*|бронь|брони|бронирован\w*|бронирова\w*|брониру\w*)"
_PAUSE = r"(?:(?:пока|уже|сейчас|больше|мне|нам|вообще|совсем|точно)\s+)*"
_REFUSAL = rf"(?:{_PAUSE}не\s+(?:нуж\w*|надо|требу\w*)|{_PAUSE}(?:проехали|забуд\w*|отменяю|отказываюсь))"
# Между темой и отказом допускаем место, но не вторую просьбу или условие.
_CONTEXT = (r"(?:\s+(?:в|по)\s+т[р]?ц\s+[\w«»\"-]+(?:\s+молл)?"
            r"|\s+в\s+(?-i:[А-ЯЁA-Z][\w«»\"-]*)(?:\s+молл)?)?")
_CANCEL = re.compile(
    rf"\b(?:про\s+)?{_TOPIC}{_CONTEXT}\s+{_REFUSAL}\b"
    rf"|\b(?:я\s+|мы\s+)?не\s+(?:прошу|просим|хочу|хотим|надо|нуж(?:на|ны|но|ен))\s+{_TOPIC}{_CONTEXT}\b"
    rf"|\bне\s+бронируйте\b"
    rf"|\b(?:забуд\w*|отменяю)\s+(?:про\s+)?{_TOPIC}\b", re.I)
_QUOTED = r'''(?:«[^»]*»|"[^"\n]*"|“[^”]*”|„[^“]*“|‘[^’]*’|'[^'\n]*')'''
_QUOTES = re.compile(_QUOTED)
_CANCEL_QUOTE = (
    re.compile(rf"(?:\bпро\s+)?(?P<quote>{_QUOTED})\s+(?:пока\s+)?(?:забуд\w*|проехали|неактуальн\w*)\b", re.I),
    re.compile(rf"\bзабуд\w*\s+(?:про\s+)?(?P<quote>{_QUOTED})", re.I),
)
_QUOTE_GUARD = re.compile(r"договор|оплат|обещ|гарант|подтверд|согласова|забронирова(?:л|н)|пришл|присл|скинь|фото|"
                          r"\bкп\b|коммерческ|жалоб|медицин", re.I)
_OTHER_REQUEST = re.compile(r"договор|оплат|обещ|гарант|подтверд|пришл|присл|скинь|фото|\bкп\b|"
                            r"коммерческ|жалоб|медицин|\d|\b(?:и|но|если|когда|хотя|дайте|сделайте)\b", re.I)
_CONTINUED_BARGAIN = re.compile(r"\b(?:дайте|сделайте|оставьте|хотя\s+бы|хотябы)\b[^.!?;]*\d"
                                r"|\b(?:но|а|лучше|можно|давайте)\b[^.!?;]*\d\s*(?:%|процент)", re.I)
_QUALIFIED_REFUSAL = re.compile(r"\s*(?:\?|если\b|бесплатно\b|пока\s+не\b|при\s+условии\b)", re.I)
_REPORTED = re.compile(r"менеджер|сказа\w*|написа\w*|сообщи\w*|цитат\w*", re.I)


def active_request(text: str) -> str:
    """Убирает только явный отказ от скидки/брони в одной реплике.

    Не понимает произвольные отрицания и не удаляет цитаты по факту цитирования.
    Сохраняет остальные просьбы, в том числе вопросы «не дадите?» и новый торг.
    """
    normalized = fix_mixed_script(text)
    if _CONTINUED_BARGAIN.search(_QUOTES.sub(" ", normalized)):
        return text

    def discard_quote(match: re.Match) -> str:
        quoted = match["quote"]
        if not re.search(_TOPIC, quoted, re.I) or _QUOTE_GUARD.search(quoted):
            return match.group(0)
        if _QUALIFIED_REFUSAL.match(normalized, match.end()):
            return match.group(0)
        return " "

    for pattern in _CANCEL_QUOTE:
        normalized = pattern.sub(discard_quote, normalized)
    quotes = [(m.start(), m.end()) for m in _QUOTES.finditer(normalized)]

    def discard(match: re.Match) -> str:
        if any(start <= match.start() < end for start, end in quotes):
            return match.group(0)
        if _REPORTED.search(re.split(r"[.!?;\n]", normalized[:match.start()])[-1]):
            return match.group(0)
        # Слова внутри названия места не должны прятать независимое стоп-основание.
        if _OTHER_REQUEST.search(match.group(0)):
            return match.group(0)
        if _QUALIFIED_REFUSAL.match(normalized, match.end()):
            return match.group(0)
        return " "

    result = _CANCEL.sub(discard, normalized)
    if re.search(r"\bзаброни", result, re.I):
        # Короткая стоп-тема «брон» может не поймать глагол с приставкой «за».
        return text
    return re.sub(r"\s+", " ", result).strip() if result != fix_mixed_script(text) else text
