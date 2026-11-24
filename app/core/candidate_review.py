"""Локальные сигналы потери смысла, а не NLI и не доказательство истинности.

Сверяем утверждения ответа с подходящими фрагментами менеджера. Вопрос задаёт область,
но его посылки не считаются фактами. Независимые правила других ТЦ/форматов пропускаем.
Предупреждение требует чтения исходника человеком; отсутствие предупреждений не заменяет его.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import KnowledgeBase, _VENUE_ALIASES
from .text import tokenize

NOTE_PREFIX = "Сверка исходника: "


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


_QUOTES = _rx(r"«[^»]*»|“[^”]*”|„[^“]*“|\"[^\"\n]*\"|'[^'\n]*'")
_SENTENCES = _rx(r"[^.!?\n]+[.!?]*")
_CLAUSES = _rx(r"[,;]|\b(?:но|зато)\b")
_NEG = _rx(r"\bне\b|\bнет\b|\bбез\b|нельзя|запрещ|отсутств")
_CONDITION = _rx(r"\bесли\b|\bпри\s+(?:наличии|условии)|\bв случае\b")
_FALLBACK = _rx(r"иначе|в противном случае|если.{0,120}(?:не\b|нет\b)|без\s+согласован")
_DEFAULT = _rx(r"\bобычно\b|\bкак правило\b|\bпо умолчанию\b")
_CLEANUP_ACTION = _rx(r"\b(?:убрат|убир)\w*\b")
_ONLY = _rx(r"\bтолько\b|исключительно")
_EXCEPTION = _rx(r"\b(?:кроме|за исключением)\s+(.+)")
_ALLOW = _rx(r"допустим|допуска|разреш|\bнужен\b|\bнужна\b")
_FORMATS = {
    "медиа-куб": _rx(r"медиа[ -]?куб|\bна кубе(?=[^.!?\n]*(?:ролик|текст|гран))"),
    "атриум": _rx(r"атриум"),
    "экран": _rx(r"экран"),
    "баннер": _rx(r"баннер"),
    "инфостойка": _rx(r"инфостойк|инфодеск|infodesk"),
    "промоакция": _rx(r"промо"),
}
_PLACES = {"атриум"}
# Семейства помогают сопоставить перефразирования; полярность проверяем внутри фразы,
# поэтому «картинки не принимаются» не покрывает потерянное «ролик не производим».
_PROPERTIES = {
    "изготовление": _rx(r"изготавли|производ|\bдела(?:ем|ю|ет)|созда[её]|собира[её]"),
    "звук": _rx(r"звук|озвуч"),
    "картинки": _rx(r"картинк"),
    "ограждение": _rx(r"огражден"),
    "бегущий текст": _rx(r"бегущ"),
    "мигание": _rx(r"миган|мига[ею]"),
    "убрать": _rx(r"убрат|убир|убор"),
}
_DETAILS = {
    "письменное согласование": _rx(r"письмен"),
    "согласование": _rx(r"согласова"),
    "ночное хранение": _rx(r"хранен"),
    "согласованный контур": _rx(r"контур|границ|предел"),
    "целиком внутри контура": _rx(r"целиком|полностью|строго в (?:пределах|границах)"),
}
_NOISE = set(tokenize("можно нельзя нужно надо должен только если иначе нет без не это этот весь "
                      "правило ограничение размещение материал рекламный ролик реклама тц молл "
                      "разрешено запрещено допустимо допускается клиент рекламодатель"))
_SUBJECTS = _rx(r"ролик|стойк|макет|баннер|экран|хранен|креплен|подвес")


@dataclass
class _Statement:
    text: str
    venues: set[str]
    formats: set[str]


def _formats(text: str) -> set[str]:
    return {name for name, pattern in _FORMATS.items() if pattern.search(text)}


def _venues(kb: KnowledgeBase, text: str) -> set[str]:
    # Поиск KB исправляет опечатки по соседним словам. Для границ правила такое
    # приближение опасно: здесь используем только точные основы и известные алиасы.
    return {_VENUE_ALIASES.get(t, t) for t in tokenize(text)} & kb._all_venues


def _properties(text: str) -> set[str]:
    return {name for name, pattern in _PROPERTIES.items() if pattern.search(text)}


def fact_scope(kb: KnowledgeBase, question: str, answer: str) -> tuple[frozenset[str], frozenset[str]]:
    """Явная область пары Q/A для группировки независимых правил в очереди."""
    text = f"{question} {answer}"
    return frozenset(_venues(kb, text)), frozenset(_formats(text))


def _statements(kb: KnowledgeBase, text: str, context: str = "") -> list[_Statement]:
    venues = _venues(kb, context)
    formats = _formats(context)
    # Не наследуем несколько областей из перечисления в клиентском вопросе.
    if len(venues) > 1:
        venues, formats = set(), set()
    result = []
    sentences = []
    for match in _SENTENCES.finditer(text):
        # Независимые области встречаются и в одном предложении. «; иначе»
        # сохраняем вместе с условием, а «; в другом ТЦ» выделяем отдельно.
        pieces = re.split(r"[;,]\s*", match.group().strip())
        sentence = pieces[0]
        for piece in pieces[1:]:
            next_venues, next_formats = _venues(kb, piece), _formats(piece)
            current_venues, current_formats = _venues(kb, sentence), _formats(sentence)
            changed = (next_venues and current_venues and next_venues != current_venues) or (
                next_formats and current_formats and not next_formats & current_formats)
            if changed:
                sentences.append(sentence)
                sentence = piece
            else:
                sentence += "; " + piece
        sentences.append(sentence)
    for sentence in sentences:
        named = _venues(kb, sentence)
        located = _formats(sentence)
        if named:
            if named != venues:
                formats = set()
            venues = named
        if located:
            formats = located
        result.append(_Statement(sentence, venues.copy(), formats.copy()))
    return result


def _anchors(text: str, scope: set[str]) -> set[str]:
    return set(tokenize(text)) - _NOISE - scope - set(tokenize(" ".join(_FORMATS)))


def review_candidate(kb: KnowledgeBase, question: str, answer: str, manager_text: str,
                     client_question: str = "") -> list[str]:
    """Понятные предупреждения по текущему Q/A; ни модель, ни сеть не вызываются.

    Названия ТЦ берём из KB, форматы и смысловые признаки — из ограниченного словаря.
    Неизвестные перефразирования могут пройти незамеченными или потребовать ручной сверки.
    """
    source = _QUOTES.sub("", manager_text)
    target = _QUOTES.sub(lambda m: m.group()[1:-1], answer)
    named = _venues(kb, f"{question} {answer}")
    formats = _formats(f"{question} {answer}")
    target_anchors = _anchors(target, named)
    context_anchors = _anchors(question, named) | target_anchors
    target_properties = _properties(target)
    warnings = []
    statements = _statements(kb, source, client_question)

    def in_scope(statement: _Statement) -> bool:
        return (not statement.venues or not named or bool(statement.venues & named)) and (
            not statement.formats or not formats or bool(statement.formats & formats))

    scoped_properties = set().union(*(_properties(s.text) for s in statements if in_scope(s)))
    scoped_anchors = set().union(*(_anchors(s.text, s.venues) for s in statements if in_scope(s)))

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    previous_related = False
    for statement in statements:
        text = statement.text
        anchors = _anchors(text, statement.venues)
        overlap = anchors & target_anchors
        properties = _properties(text)
        same_scope = in_scope(statement)
        # Общий «ролик» сам по себе не повод переносить правило другого объекта.
        related = bool(anchors & context_anchors) or bool(properties & target_properties) or bool(
            set(_SUBJECTS.findall(text.lower())) & set(_SUBJECTS.findall(target.lower())))
        if previous_related and re.match(r"(?:их|его|её|ее|это)\s", text, re.I) and _ONLY.search(text):
            related = True
        previous_related = related and same_scope
        if not same_scope:
            shared = properties & target_properties
            if (shared and shared <= scoped_properties) or (len(overlap) >= 2 and overlap <= scoped_anchors):
                continue  # У своего ТЦ есть самостоятельное правило о том же свойстве.
            if len(overlap) >= 2 or properties & target_properties:
                warn(f"ТЦ/формат: правило могло перейти на другой объект. В исходнике: «{text}»")
            continue
        if not related:
            continue
        if exception := _EXCEPTION.search(text):
            retained = _EXCEPTION.search(target)
            if not retained or not _anchors(exception[1], named) <= _anchors(retained[1], named):
                warn(f"Исключение: проверьте, кому или чему правило не подходит: «{text}».")
        if statement.venues and (not named or not named <= statement.venues):
            warn(f"ТЦ: потеряна или расширена область правила. В исходнике: «{text}»")
        source_kinds, target_kinds = statement.formats - _PLACES, formats - _PLACES
        source_places, target_places = statement.formats & _PLACES, formats & _PLACES
        if (source_kinds and (not target_kinds or not target_kinds <= source_kinds)) or (
                source_places and not source_places & target_places):
            warn(f"Формат/место: потеряна или расширена область правила ({', '.join(sorted(statement.formats))}).")

        if _CONDITION.search(text):
            if not _CONDITION.search(target):
                warn(f"Условия: правило стало безусловным. В исходнике: «{text}»")
            if _ONLY.search(text) and not _ONLY.search(target):
                warn("Условия: потеряно «только» — разрешение может читаться шире исходного.")
            for label, pattern in _DETAILS.items():
                if pattern.search(text) and not pattern.search(target):
                    warn(f"Условия: проверьте, сохранено ли требование «{label}».")
            before_else = re.split(r"иначе|в противном случае", text, flags=re.I)[0]
            if re.search(r"\bи\b", before_else, re.I) and re.search(r"\bили\b", target, re.I):
                warn("Условия: совместные требования могли превратиться из «И» в «ИЛИ».")
        if _ONLY.search(text) and not _CONDITION.search(text) and not _ONLY.search(target):
            warn(f"Ограничение: потеряно «только». В исходнике: «{text}»")

        fallback = re.search(r"(?:иначе|в противном случае)(.+)", text, re.I)
        if fallback:
            branch = fallback.group(1)
            branch_properties = _properties(branch)
            # Маркер в другом предложении не делает «каждый день убрать» условным.
            linked = any(_FALLBACK.search(s.group()) and (
                bool(branch_properties & _properties(s.group())) or
                len(_anchors(branch, statement.venues) & _anchors(s.group(), named)) >= 2)
                for s in _SENTENCES.finditer(target))
            if not linked:
                warn(f"Иначе: потеряна условная связь или сама ветка «{branch.strip()}».")
            elif "убрать" in branch_properties:
                # Наличие верной ветки не покрывает другой безусловный дубль.
                # Узкий сигнал для «убрать/убирать»: не распространяем «иначе»
                # на новое предложение или самостоятельную часть после «;».
                for sentence in _SENTENCES.finditer(target):
                    sentence_venues = _venues(kb, sentence.group())
                    sentence_formats = _formats(sentence.group())
                    if statement.venues and sentence_venues and not statement.venues & sentence_venues:
                        continue
                    if statement.formats and sentence_formats and not statement.formats & sentence_formats:
                        continue
                    for part in re.split(r";|\b(?:но|однако|при этом)\b", sentence.group(), flags=re.I):
                        if (_CLEANUP_ACTION.search(part) and not _DEFAULT.search(part) and not _NEG.search(part)
                                and not _CONDITION.search(part) and not _FALLBACK.search(part)):
                            warn(f"Иначе: действие «убрать» повторено без условия: «{part.strip()}».")

        for clause in _CLAUSES.split(text):
            if not _NEG.search(clause):
                continue
            clause_properties = _properties(clause)
            for prop in clause_properties:
                # «На озвучку рассчитывать нельзя» уже покрыто «без звука».
                preserved = any(_PROPERTIES[prop].search(part) and _NEG.search(part)
                                for part in _CLAUSES.split(target))
                if not preserved:
                    warn(f"Отрицание ({prop}): проверьте ограничение «{clause.strip()}».")
            if not clause_properties:
                required = _anchors(clause, statement.venues)
                if required and not any(_NEG.search(part) and
                                        len(required & _anchors(part, named)) >= min(2, len(required))
                                        for part in _CLAUSES.split(target)):
                    warn(f"Отрицание/исключение: проверьте оговорку «{clause.strip()}».")

        if _NEG.search(text):
            for clause in _CLAUSES.split(text):
                if not _ALLOW.search(clause) or _NEG.search(clause):
                    continue
                required = _anchors(clause, statement.venues)
                if required and not required <= target_anchors:
                    # Уверенно совпавшие перефразирования пропускаем, остальные
                    # только направляем на сверку, не объявляем ложными.
                    if len(required & target_anchors) < min(2, len(required)):
                        warn(f"Альтернатива/требование: проверьте, сохранено ли «{clause.strip()}».")

    # Опровергнутая цитата — не положительное доказательство. Проверяем именно ответ,
    # а не ложную посылку, которая может законно остаться в вопросе кандидата.
    for quote in _QUOTES.finditer(manager_text):
        tail = manager_text[quote.end():quote.end() + 80]
        quoted = quote.group()[1:-1]
        rejected = bool(re.match(r"\s*[—–-]?\s*(?:нет\b|это неверно|неверно)", tail, re.I))
        quote_anchors = _anchors(quoted, _venues(kb, quoted))
        supported = any(len(quote_anchors & _anchors(s.text, s.venues)) >= 2 and not _NEG.search(s.text)
                        for s in statements if in_scope(s))
        for part in _SENTENCES.finditer(target):
            if len(quote_anchors & _anchors(part.group(), named)) < 2:
                continue
            if rejected and not _NEG.search(part.group()):
                warn(f"Цитата: отвергнутая менеджером фраза могла стать фактом: «{quoted}».")
            elif not rejected and not supported:
                warn(f"Цитата: сама по себе не подтверждает факт «{quoted}». Сверьте ответ менеджера.")
    return warnings
