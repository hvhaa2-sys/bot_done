"""«Чему научить бота»: вопросы, которые бот передал менеджеру или на которых ошибся.

Похожие вопросы собираются в группы, самые частые — сверху. Менеджер пишет ответ
один раз, он добавляется в faq.md, и в следующий раз бот отвечает сам.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .text import tokenize

SIMILARITY = 0.45  # как в импорте переписки: доля общих значимых слов


@dataclass
class Group:
    tokens: set[str]
    ids: list[int] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    last_ts: float = 0.0
    flagged: bool = False

    @property
    def question(self) -> str:
        """Самая короткая формулировка — обычно самая общая."""
        return min(self.questions, key=len)

    @property
    def count(self) -> int:
        return len(self.ids)


def group_questions(rows) -> list[Group]:
    """rows — строки журнала (id, question, reason, created_at, flagged), новые первыми."""
    groups: list[Group] = []
    for r in rows:
        toks = set(tokenize(r["question"]))
        if not toks:
            continue
        best, best_sim = None, 0.0
        for g in groups:
            sim = len(toks & g.tokens) / len(toks | g.tokens)
            if sim > best_sim:
                best, best_sim = g, sim
        if best is None or best_sim < SIMILARITY:
            best = Group(set())
            groups.append(best)
        best.tokens |= toks
        best.ids.append(r["id"])
        best.questions.append(r["question"])
        best.reasons.append(r["reason"] or "")
        best.last_ts = max(best.last_ts, r["created_at"])
        best.flagged = best.flagged or bool(r["flagged"])
    groups.sort(key=lambda g: (g.count, g.last_ts), reverse=True)
    return groups


def faq_entry(question: str, answer: str) -> str:
    """Запись для faq.md: вопрос одной строкой после «## », ответ — следующими строками."""
    question = " ".join(question.replace("#", " ").split())
    answer = "\n".join(line.rstrip() for line in answer.strip().splitlines())
    return f"## {question}\n{answer}\n"
