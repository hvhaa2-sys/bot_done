"""Границы справочных ответов на сообщения действующих клиентов.

Отдельный слой поверх ядра: текущее размещение, письма и согласования ведёт
менеджер. Его включают и приложение, и стенд оценки.
"""
from __future__ import annotations

import re
from dataclasses import replace

from .assistant import Assistant, ESCALATE, SKIP, Reply
from .knowledge import SearchHit
from .conversation import Conversation

OPERATIONS = "business_operation"
_THANKS = re.compile(
    r"^(?:[А-ЯЁ][а-яё]+[,! ]+)?(?:спасибо|благодарю|принято)"
    r"(?:[,! ]+(?:спасибо|благодарю|принято|[А-ЯЁ][а-яё]+))*[.! 🙏👍❤️]*$"
)
_OPERATIONS = re.compile(
    r"\b(?:поставь|поставьте|запустите|запусти|продлеваем|по денежке|порешать|дошло письмо|"
    r"получили письмо|договор согласован|все согласовано|всё согласовано|выставьте сч[её]т|"
    r"добавить в сч[её]т|добавьте в сч[её]т|отправил[аи]? (?:ролик|материал|письмо)|"
    r"в почту отправил|файлы в полном разрешении|лайтбоксы свободные)\b|"
    r"https?://|\b(?:эти|оба|баннеры|места|флаги|лайтбоксы)\b.{0,55}\bсвободн|"
    r"\b(?:стоимость готовы обсуждать|вместо денег|бартера|бартером)\b|"
    r"\b(?:сократ\w*|сниз\w*|пересчита\w*)\b.{0,60}\b(?:стоимост\w*|цен\w*|час\w*)|"
    r"\b(?:напечатать|печатать|изготовить|производить)\b.{0,25}\b(?:сами|самостоятельно)\b|"
    r"\b(?:сами|самостоятельно)\b.{0,25}\b(?:напечатать|печатать|изготовить)\b|"
    r"\bконструкци\w*\b.{0,35}\bвилку\b.{0,25}\bвходить\b|"
    r"\b(?:стать|выступить|быть)\s+партн[её]р\w*\b",
    re.I,
)
_REFUSAL = re.compile(
    r"\b(?:не могу (?:предоставить|проверить|подтвердить|ответить)|не располагаю|"
    r"ваш запрос принят|приступим к обработке|чтобы я мог проверить)\b", re.I,
)


def named_thanks(text: str) -> bool:
    return bool(_THANKS.fullmatch(text.strip()))


def business_operation(text: str) -> bool:
    return bool(_OPERATIONS.search(text))


class BusinessAssistant(Assistant):
    async def answer(self, question: str, history: list[tuple[str, str]] | None = None,
                     allow_llm: bool = True, last_question: str = "",
                     conversation: Conversation | None = None) -> Reply:
        if named_thanks(question):
            return Reply(SKIP, "", 1.0, "small_talk")
        if business_operation(question):
            if conversation is not None:
                conversation.observe(self.kb, question)
                conversation.finish(ESCALATE, OPERATIONS)
            return Reply(ESCALATE, self.holding_message, 0.0, OPERATIONS)
        reply = await super().answer(question, history, allow_llm, last_question, conversation)
        if reply.is_answer:
            prices = [SearchHit(c, 0.0) for c in self.kb.chunks if c.kind == "price"]
            if self.kb.mismatched_amounts(reply.text, prices, question):
                reply = replace(reply, decision=ESCALATE, text=self.holding_message, reason="venue_mismatch")
        if reply.is_answer and _REFUSAL.search(reply.text):
            reply = replace(reply, decision=ESCALATE, text=self.holding_message, reason="model_unsure")
        if conversation is not None and reply.decision == ESCALATE:
            conversation.finish(reply.decision, reply.reason)
        return reply
