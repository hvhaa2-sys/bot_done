"""Расшифровка и черновики голосовых: только менеджеру."""
from __future__ import annotations

import asyncio
import logging

from .. import db as D
from ..core.assistant import Reply, ESCALATE
from ..speech import AudioBuffer, MAX_BYTES, MAX_SECONDS, SpeechError, download_audio
from .flow import manager_card

log = logging.getLogger(__name__)


def allowed(services, connection_id, chat_id):
    conn = services.db.get_connection(connection_id)
    if not conn or not conn.enabled or not conn.approved or conn.mode == 'off' or chat_id in services.privacy.erased:
        return None
    if conn.mode == D.OBSERVE and not services.privacy.allowed(connection_id, chat_id):
        return None
    return conn


async def telegram_text(services, bot, message):
    voice = message.voice or message.audio
    if not services.speech.enabled or not voice:
        return None
    try:
        if voice.duration > MAX_SECONDS or (voice.file_size or 0) > MAX_BYTES:
            raise SpeechError('Превышен лимит записи')
        file = await bot.get_file(voice.file_id)
        with AudioBuffer() as target:
            await bot.download_file(file.file_path, destination=target, timeout=30)
            return await services.speech.transcribe(target.getvalue(), voice.duration)
    except Exception as exc:  # noqa: BLE001 — обычная передача аудио остаётся доступной
        log.warning('Голосовое Telegram: %s', type(exc).__name__)
        return None


async def max_text(services, body):
    audio = next((a for a in body.get('attachments') or [] if a.get('type') == 'audio'), None)
    if not services.speech.enabled or not audio:
        return None
    try:
        async with asyncio.timeout(30):
            data = await download_audio((audio.get('payload') or {}).get('url', ''))
        return await services.speech.transcribe(data)
    except Exception as exc:  # noqa: BLE001
        log.warning('Голосовое MAX: %s', type(exc).__name__)
        return None


async def client_draft(services, connection_id, chat_id, text, client=''):
    """Нормальный путь ядра и учёта токенов, но всегда ручная отправка менеджером."""
    async with services.chat_lock(connection_id, chat_id):
        conn = allowed(services, connection_id, chat_id)
        if not conn or conn.mode == D.OBSERVE:
            return None
        paused = services.db.is_paused(connection_id, chat_id)
        services.db.pause_chat(connection_id, chat_id, services.settings.manager_pause_minutes)
        reply = (Reply(ESCALATE, '', 0, 'media') if paused else
                 await services.answer(text, services.db.chat_history(connection_id, chat_id)))
        conn = allowed(services, connection_id, chat_id)
        if not conn or conn.mode == D.OBSERVE:
            return None
        question = '[Голосовое]\n' + text
        dialog_id = services.db.log_dialog(
            connection_id=connection_id, chat_id=chat_id, question=question, answer=reply.text,
            decision=reply.decision, reason='voice_review', confidence=reply.confidence,
            status=D.DRAFT_PENDING if reply.is_answer else D.ESCALATED, sources=reply.sources,
            latency_ms=reply.latency_ms, mode=conn.mode, kb_version=services.kb_version)
        card = manager_card(dialog_id, question, reply, holding_sent=False, has_draft=reply.is_answer, client=client)
        card = 'Проверьте расшифровку, особенно названия и суммы.\n\n' + card
        return dialog_id, card, reply.is_answer


async def manager_learning(services, conn_id, client_chat, dialog_id, question, body, notify):
    """Оригинальное аудио уже доставлено клиенту. Модель извлекает только кандидатов."""
    if not allowed(services, conn_id, client_chat):
        return
    text = await max_text(services, body)
    conn = allowed(services, conn_id, client_chat)
    if text and conn and services.db.get_dialog(dialog_id):
        await services.collect_candidates(source='observe' if conn.mode == D.OBSERVE else 'card', connection_id=conn_id, chat_id=client_chat,
                                          dialog_id=dialog_id, client_question=question, manager_text=text)
        if allowed(services, conn_id, client_chat):
            await notify('Расшифровка вашего голосового (проверьте перед утверждением фактов):\n' + text)
