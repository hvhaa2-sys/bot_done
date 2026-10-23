"""Наблюдение за перепиской: сообщения и кандидаты, без генерации ответов клиенту."""
from __future__ import annotations

import logging

from aiogram.types import Message

from ..db import Connection
from ..services import Services

log = logging.getLogger(__name__)


async def observe(message: Message, services: Services, conn: Connection, text: str) -> None:
    # Модель может работать несколько секунд: порядок сообщений и пересборка кандидатов — под одним замком.
    async with services.chat_lock(conn.id, message.chat.id):
        manager = bool(message.from_user and message.from_user.id == conn.owner_user_id)
        _, changed = services.db.observe_message(
            connection_id=conn.id, chat_id=message.chat.id, message_id=message.message_id,
            role="manager" if manager else "client", text=text, sent_at=message.date.timestamp(),
            reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None)
        if not changed:
            return
        services.event("Наблюдение: сохранено сообщение менеджера" if manager else "Наблюдение: сохранено сообщение клиента")
        if not manager or not (message.text or message.caption):
            return
        context = services.db.observation_context(conn.id, message.chat.id, message.message_id)
        if context:
            anchor, question, answer, sources = context
            services.db.invalidate_observed_candidates([anchor])
            try:
                await services.collect_candidates(source="observe", connection_id=conn.id, chat_id=message.chat.id,
                                                  dialog_id=None, client_question=question, manager_text=answer,
                                                  observation_id=anchor, source_message_ids=sources)
            except Exception:  # noqa: BLE001 — сообщение уже сохранено, сбор знаний не мешает переписке
                log.exception("Не удалось выделить кандидатов из наблюдения")
