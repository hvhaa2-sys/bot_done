"""Логика обработки входящего сообщения, не зависящая от мессенджера.

Адаптер канала (Telegram Business, в будущем MAX) только доставляет сообщения,
а решение «ответить клиенту / прислать черновик менеджеру» принимается здесь.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import db as D
from ..core.assistant import R_GREETING, R_NO_CONTEXT, SKIP, Reply
from ..core.conversation import Conversation
from ..core.text import is_short_reaction
from ..db import Connection
from ..services import Services

REASONS = {
    "stop_topic": "тема для менеджера",
    "no_context": "ответа нет в базе знаний",
    "llm_error": "ошибка модели",
    "model_unsure": "в базе нет точного ответа",
    "low_confidence": "низкая уверенность",
    "price_mismatch": "число или сумма не из базы знаний",
    "terms_mismatch": "бот написал, что печать или монтаж входят в цену, а в прайсе этого нет",
    "venue_mismatch": "бот назвал цену другого ТЦ",
    "period_mismatch": "бот назвал цену для другого срока размещения",
    "foreign_words": "в ответе английские слова",
    "off_range": "такого формата у нас нет",
    "ungrounded": "клиент спрашивает о том, чего нет в базе знаний",
    "greeting": "приветствие",
    "small_talk": "отвечать не нужно",
    "media": "голосовое или файл — бот такие не разбирает",
    "limit": "исчерпан дневной лимит обращений к модели",
    "clarification": "бот уточняет параметры у клиента",
    "clarification_limit": "после двух уточнений не удалось определить запрос",
}

HOLDING_SENT = "Клиенту отправлено: «Уточню и вернусь»."
HOLDING_NOT_SENT = "Клиенту ничего не ушло: нет связи с Telegram или клиент писал больше 24 часов назад."
CARD_TEXT_LIMIT = 1500  # вопрос и черновик в карточке: номер #N в конце не должен обрезаться лимитом Telegram


@dataclass
class Outgoing:
    dialog_id: int                   # 0 — в журнал не пишем (приветствие, «спасибо»)
    to_client: str | None = None     # что отправить клиенту сейчас
    to_manager: str | None = None    # карточка для менеджера
    with_buttons: bool = False       # кнопки «Отправить / Пропустить»
    notice: str | None = None        # «клиент готов к заказу» — бот ответил сам, но менеджеру стоит подключиться


async def process_client_message(services: Services, conn: Connection, chat_id: int, text: str,
                                 client: str = "") -> Outgoing:
    """client — имя клиента для карточки менеджеру (в журнал не пишется)."""
    # Клиент пишет слишком часто (спам, бот, попытка выжечь лимит модели) — замолкаем в этом чате на час
    hourly = int(services.current()["chat_hourly_limit"])
    if hourly and services.db.count_recent(conn.id, chat_id, 3600) >= hourly:
        services.db.pause_chat(conn.id, chat_id, 60)
        who = client or str(chat_id)
        services.event(f"{who}: больше {hourly} сообщений за час — бот замолчал в этом чате на час")
        return Outgoing(0, to_manager=(f"Клиент: {who}\nПишет слишком часто: больше {hourly} сообщений за час. "
                                       "Бот замолчал в этом чате на час, чтобы не тратить лимит модели. "
                                       "Ответьте сами или снимите паузу: /resume"))
    history = services.db.chat_history(conn.id, chat_id)
    conversation = Conversation.replay(services.kb, services.db.conversation_turns(conn.id, chat_id))
    reply = await services.answer(text, history, conversation=conversation)
    # Расписание: в рабочее время менеджер на месте — бот в автоответе только готовит черновики
    mode = "draft" if conn.mode == "auto" and not services.auto_allowed() else conn.mode
    # Пока модель думала, менеджер мог ответить сам: клиенту ничего не шлём, ответ — черновиком менеджеру
    if services.db.is_paused(conn.id, chat_id):
        mode = "draft"

    # «Спасибо», «ок» — молчим; приветствие — здороваемся сами только в автоответе.
    # В журнал и статистику не пишем, чтобы не завышать долю ответов бота.
    if reply.decision == SKIP:
        return Outgoing(0)
    if reply.reason == R_GREETING:
        return Outgoing(0, to_client=services.sign(reply.text) if mode == "auto" else None)

    if mode == "auto" and reply.is_answer:
        status, to_client = D.SENT_AUTO, services.sign(reply.text)
    elif mode == "auto":
        # Бот не паузится после «Уточню и вернусь»: на то, что знает, отвечает дальше,
        # а на следующий трудный вопрос молча шлёт карточку менеджеру, не повторяя «уточню»
        # На реакцию без вопроса («Гениально 👏», «Ого, давайте») «уточню» тоже не пишем — только карточка
        quiet = services.db.holding_sent_recently(conn.id, chat_id, services.settings.manager_pause_minutes) \
            or (reply.reason == R_NO_CONTEXT and is_short_reaction(text))
        status, to_client = D.ESCALATED, None if quiet else services.sign(services.assistant.holding_message)
    else:
        status, to_client = D.DRAFT_PENDING, None

    dialog_id = services.db.log_dialog(
        connection_id=conn.id, chat_id=chat_id, question=text, answer=reply.text,
        decision=reply.decision, reason=reply.reason, confidence=reply.confidence,
        status=status, sources=reply.sources, latency_ms=reply.latency_ms, mode=mode,
        kb_version=services.kb_version,
    )

    # Что клиент уже назвал за разговор: ТЦ, формат, срок, бюджет — менеджеру не листать переписку
    summary = conversation.parameters()
    if status == D.SENT_AUTO:
        notice = None
        if is_ready_to_buy(text):
            who = f"{client} " if client else ""
            notice = (f"Клиент {who}готов к заказу:\n«{cut(text)}»\n" + (f"Что известно: {summary}\n" if summary else "")
                      + f"\nБот ответил:\n{cut(reply.text)}\n\n"
                      "Подключитесь к разговору, пока клиент не остыл.")
        return Outgoing(dialog_id, to_client=to_client, notice=notice)

    has_draft = reply.text != services.assistant.holding_message
    card = manager_card(dialog_id, text, reply, holding_sent=bool(to_client), has_draft=has_draft, client=client,
                        summary=summary)
    return Outgoing(dialog_id, to_client=to_client, to_manager=card,
                    with_buttons=has_draft or mode == "draft")


# «Берём», «давайте оформим», «хочу на месяц экран», «когда можно начать?» — уже не вопрос, а заказ
_READY_RE = re.compile(
    r"бронир|заброниру|оформ(?:им|ляем|ляйте|ите|ить)|\bбер[её]м\b|\bберу\b|возьм[её]м|\bвозьму\b"
    r"|готов[ыа]? (?:взять|заказать|разместить|начать|купить)|давайте (?:оформ|размест|запуск|начн|брать|возьм|закаж)"
    r"|когда можно (?:начать|запустить|разместить|выйти)|хоч\w* (?:на|с) (?:\d|месяц|недел|полгода|год|декабр|январ|ноябр)"
    r"|хоч\w* (?:заказать|забронировать|взять)", re.IGNORECASE)


_NOT_READY_RE = re.compile(r"\b(?:не|если|пока|подумаю|подумаем)\b[^.!?]*?(?:бер|возьм|оформ|запуск|соглас|устраива)",
                           re.IGNORECASE)
_READY_MORE_RE = re.compile(r"\bзапускаем\b|\bсогласн[ыа]\b|нас устраивает|\bоформляем\b", re.IGNORECASE)


def is_ready_to_buy(text: str) -> bool:
    text = text.replace("ё", "е")
    if _NOT_READY_RE.search(text):
        return False
    return bool(_READY_RE.search(text) or _READY_MORE_RE.search(text))


def human_reason(reason: str) -> str:
    key = reason.split(":", 1)[0]
    return REASONS.get(key, reason)


def manager_card(dialog_id: int, question: str, reply: Reply, *, holding_sent: bool, has_draft: bool,
                 client: str = "", summary: str = "") -> str:
    lines = [f"Клиент: {client}"] if client else []
    lines += [f"Вопрос клиента:\n«{cut(question)}»"]
    if summary:
        lines.append(f"Что известно: {summary}")
    lines.append("")
    if holding_sent:
        lines.append(HOLDING_SENT)
    if not reply.is_answer:
        lines.append(f"Почему бот не ответил: {human_reason(reply.reason)}.")
    if has_draft:
        lines += ["", "Черновик ответа:", cut(reply.text), "",
                  f"Черновик #{dialog_id}. Нажмите «Отправить» или ответьте на это сообщение своим текстом."]
    else:
        lines += ["", f"Вопрос #{dialog_id}. Ответьте клиенту в его чате или прямо здесь, "
                      "ответом на это сообщение — бот перешлёт ваш текст."]
    return "\n".join(lines)


def cut(text: str) -> str:
    return text if len(text) <= CARD_TEXT_LIMIT else text[:CARD_TEXT_LIMIT].rstrip() + "…"
