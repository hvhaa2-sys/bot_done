"""Наблюдение за перепиской: сообщения и кандидаты, без генерации ответов клиенту."""
from __future__ import annotations

import logging

from aiogram.types import Message

from ..db import Connection
from ..services import Services

log = logging.getLogger(__name__)


async def observe(message: Message, services: Services, conn: Connection, text: str) -> bool:
    """Сохранить сообщение в журнал наблюдения и, если это ответ менеджера, собрать кандидатов.
    Возвращает, новое ли это сообщение (False — повторная доставка без изменений)."""
    if not services.privacy.allowed(conn.id, message.chat.id):
        return False
    # Модель может работать несколько секунд: порядок сообщений и пересборка кандидатов — под одним замком.
    async with services.chat_lock(conn.id, message.chat.id):
        manager = bool(message.from_user and message.from_user.id == conn.owner_user_id)
        _, changed = services.db.observe_message(
            connection_id=conn.id, chat_id=message.chat.id, message_id=message.message_id,
            role="manager" if manager else "client", text=text, sent_at=message.date.timestamp(),
            reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None)
        if not changed:
            return False
        services.event("Наблюдение: сохранено сообщение менеджера" if manager else "Наблюдение: сохранено сообщение клиента")
        if manager and (message.text or message.caption):
            await collect_observed(services, conn.id, message.chat.id, message.message_id)
        return True


async def collect_observed(services: Services, connection_id: str, chat_id: int, message_id: int) -> None:
    """Ответ менеджера из журнала наблюдения → предложения в базу знаний (вопрос клиента берём из журнала)."""
    if not services.privacy.allowed(connection_id, chat_id):
        return
    context = services.db.observation_context(connection_id, chat_id, message_id)
    if not context:
        return
    anchor, question, answer, sources = context
    services.db.invalidate_observed_candidates([anchor])
    try:
        await services.collect_candidates(source="observe", connection_id=connection_id, chat_id=chat_id,
                                          dialog_id=None, client_question=question, manager_text=answer,
                                          observation_id=anchor, source_message_ids=sources)
    except Exception:  # noqa: BLE001 — сообщение уже сохранено, сбор знаний не мешает переписке
        log.exception("Не удалось выделить кандидатов из наблюдения")
