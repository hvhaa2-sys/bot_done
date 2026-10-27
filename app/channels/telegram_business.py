"""Адаптер Telegram Business.

Как это работает:
1. Менеджер подключает бота в Telegram: Настройки → Автоматизация чатов
   (до мая 2026: Telegram для бизнеса → Чат-боты) → добавляет бота. В BotFather у бота
   должен быть включён Secretary Mode (раньше Business Mode). Premium с Bot API 10.0 не нужен.
2. Telegram присылает business_connection — сохраняем подключение.
3. Сообщения клиентов в личных чатах менеджера приходят как business_message.
4. Ответ клиенту уходит от имени менеджера через business_connection_id.

Ограничение Telegram: ответить можно только в чатах, где клиент писал
за последние 24 часа.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
import statistics

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (TelegramAPIError, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BusinessConnection, BusinessMessagesDeleted, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .. import db as D
from ..services import Services
from .flow import HOLDING_NOT_SENT, HOLDING_SENT, process_client_message

log = logging.getLogger(__name__)

_DRAFT_ID_RE = re.compile(r"(?:Черновик|Вопрос) #(\d+)")


class MessageBuffer:
    """Склейка сообщений клиента, присланных подряд: «Здравствуйте» / «сколько стоит экран» / «на месяц».

    Бот ждёт паузу в несколько секунд и отвечает на всё сразу, а не на каждый кусок отдельно.
    """
    def __init__(self) -> None:
        # (id сообщения, текст): клиент может поправить или удалить сообщение, пока бот ждёт
        self.parts: dict[tuple[str, int], list[tuple[int, str]]] = {}
        self.names: dict[tuple[str, int], str] = {}  # имя клиента для карточки менеджеру
        self.timers: dict[tuple[str, int], asyncio.Task] = {}

    def take(self, key: tuple[str, int]) -> str:
        return "\n".join(text for _, text in self.parts.pop(key, []))


def draft_keyboard(dialog_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить", callback_data=f"send:{dialog_id}"),
        InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{dialog_id}"),
    ]])


# --- подключение бота к аккаунту менеджера ---
def save_connection(bc: BusinessConnection, services: Services) -> D.Connection:
    can_reply = bool(bc.rights.can_reply) if bc.rights else bool(bc.can_reply)
    return services.db.upsert_connection(
        bc.id, bc.user.id, bc.user_chat_id, bc.is_enabled, can_reply, services.settings.default_mode,
    )


async def on_business_connection(bc: BusinessConnection, bot: Bot, services: Services) -> None:
    conn = save_connection(bc, services)
    services.owner_names[bc.user.id] = bc.user.full_name + (f" (@{bc.user.username})" if bc.user.username else "")
    can_reply = conn.can_reply
    if not conn.approved:
        # Бот публичный: подключить его может любой, кто знает имя. Чужие аккаунты ждут разрешения,
        # иначе их клиенты тратили бы нашу модель
        services.event(f"{bc.user.full_name} (id {bc.user.id}) подключил бота к своему аккаунту — "
                       "ждёт разрешения: Обзор → Подключения")
        if bc.is_enabled:
            await safe_send(bot, bc.user_chat_id, "Этот бот работает только для менеджеров компании. "
                                                  "Запрос на подключение отправлен администратору.")
        return
    services.event("Менеджер " + ("отключил бота" if not bc.is_enabled else
                   "подключил бота без права отвечать" if not can_reply else f"подключил бота, режим: {mode_name(conn.mode)}"))
    if not bc.is_enabled:
        text = "Бот отключён от вашего аккаунта."
    elif not can_reply:
        text = "Бот подключён, но без права отвечать. Разрешите ему отвечать: Настройки → Автоматизация чатов."
    else:
        text = (f"Бот подключён. Режим: {mode_name(conn.mode)}.\n"
                "Сменить режим: /mode draft (черновики), /mode auto (автоответ) или /mode watch (наблюдение).\n\n"
                "Совет: в «Автоматизации чатов» исключите контакты — тогда бот не будет "
                "отвечать друзьям и семье, только новым клиентам.")
    await safe_send(bot, bc.user_chat_id, text)


# --- сообщения в личных чатах менеджера ---
async def on_business_message(message: Message, bot: Bot, services: Services, buffer: MessageBuffer) -> None:
    conn = services.db.get_connection(message.business_connection_id or "")
    if conn is None and message.business_connection_id:
        # Подключение было, пока бот не работал (или база пересоздана) — спросим у Telegram
        try:
            conn = save_connection(await bot.get_business_connection(message.business_connection_id), services)
        except TelegramAPIError:
            conn = None
    if not conn or not conn.enabled:
        services.event("Пришло сообщение, но бот не подключён к этому аккаунту — пропускаю")
        return
    if not conn.approved:
        return  # чужой аккаунт ещё не одобрен в админке — модель не тратим
    chat_id = message.chat.id
    chat = chat_title(message)
    services.chat_names[chat_id] = chat
    # Наш же ответ: Telegram присылает его обратно от имени менеджера. Без этой
    # проверки бот после каждого своего ответа ставил бы себя на паузу.
    if message.sender_business_bot:
        return
    # Приветствие и «нет на месте» Telegram шлёт сам от имени менеджера. Это не менеджер за клавиатурой:
    # пауза здесь заглушила бы бота ровно тогда, когда менеджера нет
    if message.is_from_offline:
        services.event(f"{chat}: автоответ Telegram (приветствие или «нет на месте») — не мешаю. "
                       "Совет: выключите их, пока работает автоответ бота")
        return
    # С Bot API 10.0 бизнес-бот может отвечать другим ботам — не даём двум ботам переписываться бесконечно
    if message.from_user and message.from_user.is_bot:
        return
    # Пишет сам менеджер (текст, фото, голосовое — что угодно): бот замолкает в этом чате
    if message.from_user and message.from_user.id == conn.owner_user_id:
        if conn.mode == D.WATCH:
            # Наблюдение: бот и так молчит — паузу не ставим, а фиксируем, через сколько менеджер ответил
            said = message.text or message.caption or f"[{media_kind(message) or 'сообщение'}]"
            services.log_watch_message(conn.id, chat_id, "manager", said, message.date.timestamp())
            waited = services.db.watch_reply(conn.id, chat_id, said, message.date.timestamp())
            if waited is not None:
                services.event(f"{chat}: наблюдение — менеджер ответил через {wait_text(waited / 60)}")
        # Пауза считается от времени сообщения: после простоя (сон ноутбука, перезапуск) старые
        # ответы менеджера приходят пачкой и не должны заново глушить бота на полчаса
        left = services.settings.manager_pause_minutes - age_minutes(message.date)
        if left > 0 and conn.mode != D.WATCH:
            services.db.pause_chat(conn.id, chat_id, left)
            services.event(f"{chat}: менеджер написал сам — бот молчит в этом чате "
                           f"{max(1, round(left))} мин (снять: /resume)")
        # Ответ менеджера на свежий вопрос клиента — кандидат в базу знаний (утверждает менеджер)
        question = services.db.last_question(conn.id, chat_id)
        if message.text and question and not is_stale(message.date, services.settings.stale_message_minutes):
            await collect_quietly(services, "chat", conn.id, chat_id, None, question, message.text)
        return
    # Подпись к фото или файлу — тот же вопрос, что и текст
    text = message.text or message.caption
    if conn.mode == D.WATCH:
        # Наблюдение: бот не отвечает, не шлёт карточек и не тратит модель — только записывает обращение.
        # Время — из сообщения: после простоя бота пачка старых сообщений не сдвигает замер
        kind = media_kind(message)
        if text or kind:
            services.log_watch_message(conn.id, chat_id, "client", text or f"[{kind}]", message.date.timestamp())
            services.db.watch_question(conn.id, chat_id, text or f"[{kind}]", message.date.timestamp(),
                                       services.kb_version)
            services.event(f"{chat}: «{short(text or kind, 60)}» — наблюдение, бот молчит")
        return
    if not text:
        kind = media_kind(message)
        if kind and not is_stale(message.date, services.settings.stale_message_minutes) \
                and not services.db.is_paused(conn.id, chat_id):
            await hand_media_to_manager(bot, services, conn, chat_id, kind, client_name(message))
        return  # стикеры и гифки молча пропускаем
    # Сообщение пролежало в очереди, пока бот был выключен: менеджер мог уже ответить сам
    if is_stale(message.date, services.settings.stale_message_minutes):
        services.event(f"{chat}: старое сообщение (бот был выключен) — пропускаю, менеджер мог уже ответить")
        return
    if services.db.is_paused(conn.id, chat_id):
        services.event(f"{chat}: «{short(text, 60)}» — чат на паузе, бот молчит (снять: /resume)")
        return

    key = (conn.id, chat_id)
    buffer.parts.setdefault(key, []).append((message.message_id, text))
    buffer.names[key] = client_name(message)
    delay = float(services.current()["merge_seconds"])
    if delay <= 0:
        await answer_client(bot, services, conn.id, chat_id, buffer.take(key), buffer.names.pop(key, ""))
        return
    # Клиент ещё печатает: откладываем ответ, пока он не замолчит на delay секунд
    if (old := buffer.timers.pop(key, None)) is not None:
        old.cancel()
    buffer.timers[key] = asyncio.create_task(answer_later(bot, services, buffer, key, delay))


async def on_edited_business_message(message: Message, buffer: MessageBuffer) -> None:
    """Клиент поправил сообщение, пока бот ждал паузы, — отвечаем на исправленный текст."""
    key = (message.business_connection_id or "", message.chat.id)
    text = message.text or message.caption
    if key in buffer.parts and text:
        buffer.parts[key] = [(mid, text if mid == message.message_id else old) for mid, old in buffer.parts[key]]


async def on_deleted_business_messages(event: BusinessMessagesDeleted, buffer: MessageBuffer) -> None:
    """Клиент удалил сообщение, пока бот ждал паузы, — на удалённое не отвечаем."""
    key = (event.business_connection_id, event.chat.id)
    if key in buffer.parts:
        buffer.parts[key] = [(mid, t) for mid, t in buffer.parts[key] if mid not in set(event.message_ids)]
        if not buffer.parts[key]:
            buffer.parts.pop(key)
            if (timer := buffer.timers.pop(key, None)) is not None:
                timer.cancel()


MEDIA_KINDS = {"voice": "голосовое сообщение", "video_note": "видеосообщение", "audio": "аудио",
               "photo": "фото", "video": "видео", "document": "файл"}


def chat_title(message: Message) -> str:
    """Имя собеседника в личном чате менеджера — для ленты событий в админке."""
    return getattr(message.chat, "full_name", "") or str(message.chat.id)


def client_name(message: Message) -> str:
    """«Иван Петров (@ivan)» — чтобы менеджер понял, в каком чате отвечать."""
    user = message.from_user or message.chat
    name = getattr(user, "full_name", "") or "клиент"
    return f"{name} (@{user.username})" if getattr(user, "username", None) else name


def media_kind(message: Message) -> str | None:
    if getattr(message, "animation", None):
        return None  # у гифки Telegram заполняет и document — это не файл, молча пропускаем
    return next((name for attr, name in MEDIA_KINDS.items() if getattr(message, attr, None)), None)


async def hand_media_to_manager(bot: Bot, services: Services, conn: D.Connection, chat_id: int, kind: str,
                                client: str = "клиент") -> None:
    """Голосовое, фото без подписи, файл: бот их не разбирает — разговор забирает менеджер."""
    # Пауза — до первого await: альбом из пяти фото приходит пятью сообщениями сразу,
    # а «Уточню и вернусь» и карточка менеджеру нужны одни
    services.db.pause_chat(conn.id, chat_id, services.settings.manager_pause_minutes)
    auto = conn.mode == "auto" and services.auto_allowed()
    holding = services.assistant.holding_message
    services.db.log_dialog(connection_id=conn.id, chat_id=chat_id, question=f"[{kind}]",
                           answer=holding if auto else "", decision="escalate", reason="media",
                           confidence=0.0, status=D.ESCALATED, sources=[], latency_ms=0, mode=conn.mode,
                           kb_version=services.kb_version)
    sent = auto and conn.can_reply and await send_to_client(bot, conn.id, chat_id, services.sign(holding))
    services.event(f"{client}: {kind} — бот такие не разбирает, передал менеджеру")
    await safe_send(bot, conn.owner_chat_id,
                    f"{client} прислал(а) {kind} — бот такие не разбирает, ответьте сами.\n"
                    f"{HOLDING_SENT + ' ' if sent else ''}"
                    f"Бот молчит в этом чате {services.settings.manager_pause_minutes} мин.")


async def answer_later(bot: Bot, services: Services, buffer: MessageBuffer, key: tuple[str, int],
                       delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # пришло ещё сообщение — ответим на всё вместе позже
    buffer.timers.pop(key, None)
    text, client = buffer.take(key), buffer.names.pop(key, "")
    connection_id, chat_id = key
    if not text:
        return
    # Пока ждали, менеджер мог ответить сам — тогда молчим
    if services.db.is_paused(connection_id, chat_id):
        services.event(f"{services.chat_names.get(chat_id, chat_id)}: «{short(text, 60)}» — пока бот ждал, "
                       "чат встал на паузу (менеджер ответил сам) — молчу")
        return
    try:
        await answer_client(bot, services, connection_id, chat_id, text, client)
    except Exception:  # noqa: BLE001 — фоновая задача: ошибку только в лог
        log.exception("Не удалось ответить в чате %s", chat_id)


async def answer_client(bot: Bot, services: Services, connection_id: str, chat_id: int, text: str,
                        client: str = "") -> None:
    async with services.chat_lock(connection_id, chat_id):
        await _answer_client(bot, services, connection_id, chat_id, text, client)


async def _answer_client(bot: Bot, services: Services, connection_id: str, chat_id: int, text: str,
                         client: str = "") -> None:
    conn = services.db.get_connection(connection_id)
    if not conn or not conn.enabled:
        return
    out = await process_client_message(services, conn, chat_id, text, client)
    outcome = ("бот ответил сам" if out.to_client and not out.to_manager
               else "клиенту «Уточню и вернусь», вопрос — менеджеру" if out.to_client
               else "черновик менеджеру" if out.with_buttons
               else "вопрос — менеджеру" if out.to_manager else "отвечать не нужно — молчит")
    services.event(f"{services.chat_names.get(chat_id, chat_id)}: «{short(text, 60)}» — {outcome}")

    sent = False
    if out.to_client:
        sent = conn.can_reply and await send_to_client(bot, conn.id, chat_id, out.to_client)
        if not sent and out.dialog_id and not out.to_manager:
            # Автоответ не дошёл (нет прав, прошло 24 часа) — вопрос забирает менеджер
            services.db.set_dialog_status(out.dialog_id, D.ESCALATED)
            services.event(f"{services.chat_names.get(chat_id, chat_id)}: ответ не ушёл клиенту (нет связи "
                           "с Telegram или прошло 24 часа) — передан менеджеру")
            await safe_send(bot, conn.owner_chat_id,
                            f"Не получилось отправить ответ клиенту {client}, ответьте сами.\n\nВопрос клиента:\n«{text}»")
    if out.notice and sent:
        services.event(f"{services.chat_names.get(chat_id, chat_id)}: клиент готов к заказу — сообщил менеджеру")
        await safe_send(bot, conn.owner_chat_id, out.notice)
    if out.to_manager:
        card = out.to_manager if sent or not out.to_client else out.to_manager.replace(HOLDING_SENT, HOLDING_NOT_SENT)
        markup = draft_keyboard(out.dialog_id) if out.with_buttons else None
        await safe_send(bot, conn.owner_chat_id, card, reply_markup=markup)


# --- решения менеджера по черновикам ---
async def on_draft_decision(cb: CallbackQuery, bot: Bot, services: Services) -> None:
    action, dialog_id = cb.data.split(":")
    dialog = services.db.get_dialog(int(dialog_id))
    if not dialog or dialog["status"] not in (D.DRAFT_PENDING, D.ESCALATED):
        await cb.answer("Черновик уже обработан.")
        return
    conn = services.db.get_connection(dialog["connection_id"])
    if not conn or cb.from_user.id != conn.owner_user_id:
        await cb.answer("Нет доступа.")
        return

    ok = True
    if action == "skip":
        claimed = services.db.claim_dialog(dialog["id"], D.DRAFT_REJECTED)
        note = "Пропущено."
    elif not conn.can_reply:
        claimed, ok, note = True, False, send_failure(conn)
    else:
        # Сначала закрываем черновик, потом шлём: второе нажатие, пока идёт отправка, ничего не сделает
        claimed = services.db.claim_dialog(dialog["id"], D.DRAFT_SENT)
        if claimed:
            ok = await send_to_client(bot, conn.id, dialog["chat_id"], services.sign(dialog["answer"]))
            note = "Отправлено клиенту." if ok else send_failure(conn)
            if not ok:  # вернуть черновик: менеджер сможет нажать ещё раз
                services.db.set_dialog_status(dialog["id"], dialog["status"])
    if not claimed:
        await cb.answer("Черновик уже обработан.")
        return
    await cb.answer(note[:200])
    # Сообщение старше 48 часов Telegram отдаёт без текста (InaccessibleMessage) — его не правим
    if isinstance(cb.message, Message) and cb.message.text:
        with contextlib.suppress(TelegramAPIError):
            await cb.message.edit_text(fit(f"{cb.message.text}\n\n{note}"),
                                       reply_markup=None if ok else cb.message.reply_markup)


async def on_manager_edit(message: Message, bot: Bot, services: Services) -> None:
    """Менеджер ответил на карточку своим текстом — отправляем его клиенту."""
    # Номер карточки — последний: в тексте вопроса клиента тоже может встретиться «Вопрос #5»
    ids = _DRAFT_ID_RE.findall(message.reply_to_message.text or "")
    if not ids:
        return
    dialog = services.db.get_dialog(int(ids[-1]))
    if not dialog or dialog["status"] not in (D.DRAFT_PENDING, D.ESCALATED):
        await message.answer("Этот черновик уже обработан.")
        return
    conn = services.db.get_connection(dialog["connection_id"])
    if not conn or message.from_user.id != conn.owner_user_id:
        return
    if not conn.can_reply:
        await message.answer(send_failure(conn))
        return
    if not services.db.claim_dialog(dialog["id"], D.DRAFT_EDITED, answer=message.text):
        await message.answer("Этот черновик уже обработан.")
        return
    if await send_to_client(bot, conn.id, dialog["chat_id"], message.text):
        await message.answer("Ваш вариант отправлен клиенту.")
        await collect_quietly(services, "card", conn.id, dialog["chat_id"], dialog["id"], dialog["question"],
                              message.text)
    else:
        services.db.set_dialog_status(dialog["id"], dialog["status"], answer=dialog["answer"])
        await message.answer(send_failure(conn))


# --- команды в личном чате с ботом ---
async def on_start(message: Message, services: Services) -> None:
    await message.answer(
        "Привет! Я ИИ-помощник менеджера по продажам: отвечаю клиентам в ваших личных чатах "
        "по прайсу и базе знаний, а сложное передаю вам.\n\n"
        "Подключите меня: Настройки → Автоматизация чатов → добавьте бота и разрешите отвечать. "
        "Контакты лучше исключить — друзьям и семье бот отвечать не будет.\n\n"
        "Команды:\n"
        "/mode draft — черновики: ответ сначала приходит вам\n"
        "/mode auto — автоответ: бот отвечает клиенту сам\n"
        "/mode watch — наблюдение: бот молчит и только запоминает вопросы, ваши ответы и время ответа\n"
        "/status — статистика\n"
        f"/resume — снять паузу. Когда вы пишете клиенту сами, бот молчит в этом чате "
        f"{services.settings.manager_pause_minutes} мин."
    )


async def on_mode(message: Message, command: CommandObject, services: Services) -> None:
    mode = (command.args or "").strip().lower()
    if mode not in D.MODES:
        await message.answer("Укажите режим: /mode draft, /mode auto или /mode watch")
        return
    n = services.db.set_mode(mode, owner_user_id=message.from_user.id)
    if n:
        services.event(f"Менеджер включил режим: {mode_name(mode)}")
        await message.answer(f"Режим: {mode_name(mode)}." + (WATCH_NOTE if mode == D.WATCH else ""))
    else:
        await message.answer("Сначала подключите бота: Настройки → Автоматизация чатов.")


# --- вечерняя сводка: что бот ответил сам, с кнопками «Ошибка» ---
def build_summary(services: Services, conn: D.Connection, since_ts: float) -> tuple[str, InlineKeyboardMarkup | None] | None:
    rows = [r for r in services.db.dialogs_since(conn.id, since_ts) if r["status"] != D.SANDBOX]
    watched = [r for r in rows if r["status"] == D.WATCH]
    rows = [r for r in rows if r["status"] != D.WATCH]
    if not rows and not watched:
        return None
    auto = [r for r in rows if r["status"] == D.SENT_AUTO]
    escalated = sum(r["status"] == D.ESCALATED for r in rows)
    drafts = len(rows) - len(auto) - escalated
    lines = [f"Итоги дня: вопросов от клиентов — {len(rows)}.",
             f"Бот ответил сам — {len(auto)}, передал вам — {escalated}, черновиков — {drafts}."] if rows else []
    if watched:
        waits = [r["reply_after_s"] / 60 for r in watched if r["reply_after_s"] is not None]
        median = f", обычно через {wait_text(statistics.median(waits))} (медиана)" if waits else ""
        lines.append(f"Наблюдение за день: обращений клиентов — {len(watched)}, вы ответили на {len(waits)}{median}.")
    shown = auto[-10:]
    if shown:
        lines += ["", "Ответы бота:"]
        for i, r in enumerate(shown, 1):
            lines.append(f"{i}. «{short(r['question'], 80)}» → {short(r['answer'], 140)}")
        lines += ["", "Если ответ неверный, нажмите его номер: отмечу как ошибку, и бот будет на нём проверяться."]
    buttons = [InlineKeyboardButton(text=f"Ошибка {i}", callback_data=f"flag:{r['id']}") for i, r in enumerate(shown, 1)]
    markup = InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)]) if buttons else None
    return "\n".join(lines), markup


async def maybe_send_summaries(bot: Bot, services: Services, now: dt.datetime | None = None) -> int:
    """Раз в день после заданного времени прислать менеджерам итоги. Возвращает число сводок."""
    when = services.current()["summary_time"]
    now = now or services.local_now()
    today = now.strftime("%Y-%m-%d")
    if not when or now.strftime("%H:%M") < when or services.db.get_settings().get("summary_sent") == today:
        return 0
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sent = 0
    for conn in services.db.connections():
        if conn.id.startswith("max:"):
            continue  # менеджерам MAX сводку шлёт их адаптер (max_bot.py)
        summary = build_summary(services, conn, start) if conn.enabled else None
        if summary:
            await safe_send(bot, conn.owner_chat_id, summary[0], reply_markup=summary[1])
            sent += 1
        if conn.enabled and (proposals := build_candidates_message(services, conn)):
            await safe_send(bot, conn.owner_chat_id, proposals[0], reply_markup=proposals[1])
    services.db.set_settings({"summary_sent": today})
    return sent


CANDIDATES_IN_SUMMARY = 5


def build_candidates_message(services: Services, conn: D.Connection) -> tuple[str, InlineKeyboardMarkup] | None:
    """Предложения в базу знаний из ответов менеджера: утвердить или отклонить прямо из Telegram."""
    rows = [r for r in services.db.candidates() if r["connection_id"] in (conn.id, None)]
    if not rows:
        return None
    shown = rows[:CANDIDATES_IN_SUMMARY]
    lines = [f"Предложения в базу знаний из ваших ответов клиентам — {len(rows)}.",
             "Утверждённое бот начнёт использовать в ответах. Остальные — в админке, раздел «Обучение».", ""]
    for i, r in enumerate(shown, 1):
        lines.append(f"{i}. «{short(r['question'], 90)}» → {short(r['answer'], 180)}")
        if r["note"]:
            lines.append(f"   ⚠ {short(r['note'], 140)}")
    buttons = [[InlineKeyboardButton(text=f"✓ {i}", callback_data=f"cand:ok:{r['id']}"),
                InlineKeyboardButton(text=f"✗ {i}", callback_data=f"cand:no:{r['id']}")] for i, r in enumerate(shown, 1)]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def on_candidate_decision(cb: CallbackQuery, services: Services) -> None:
    _, action, candidate_id = cb.data.split(":")
    row = services.db.get_candidate(int(candidate_id))
    conn = services.db.get_connection(row["connection_id"]) if row and row["connection_id"] else None
    owners = {conn.owner_user_id} if conn else {c.owner_user_id for c in services.db.connections() if c.approved}
    if not row or cb.from_user.id not in owners:
        await cb.answer("Нет доступа.")
        return
    if row["status"] != "new":
        await cb.answer("Уже разобрано.")
        return
    if action == "no":
        services.db.decide_candidates([row["id"]], "rejected")
        await cb.answer("Отклонено.")
        return
    _, msg = services.approve_candidates([row["id"]], row["question"], row["answer"])
    await cb.answer(msg[:200])


async def collect_quietly(services: Services, source: str, connection_id: str, chat_id: int, dialog_id: int | None,
                          question: str, manager_text: str) -> None:
    """Сбор кандидатов не должен мешать переписке: сбой модели или базы — только запись в лог."""
    try:
        await services.collect_candidates(source=source, connection_id=connection_id, chat_id=chat_id,
                                          dialog_id=dialog_id, client_question=question, manager_text=manager_text)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось собрать кандидатов в базу знаний")


async def summary_loop(bot: Bot, services: Services) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await maybe_send_summaries(bot, services)
        except Exception:  # noqa: BLE001 — сводка не должна ронять бота
            log.exception("Не удалось отправить вечернюю сводку")


async def on_flag(cb: CallbackQuery, services: Services) -> None:
    dialog = services.db.get_dialog(int(cb.data.split(":")[1]))
    conn = services.db.get_connection(dialog["connection_id"]) if dialog else None
    if not conn or cb.from_user.id != conn.owner_user_id:
        await cb.answer("Нет доступа.")
        return
    services.db.flag_dialog(dialog["id"], True)
    await cb.answer("Отметил как ошибку. Спасибо!")


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


async def on_resume(message: Message, services: Services) -> None:
    """Снять паузы во всех чатах менеджера: бот снова отвечает клиентам."""
    conns = [c for c in services.db.connections() if c.owner_user_id == message.from_user.id and c.approved]
    if not conns:
        await message.answer("Сначала подключите бота: Настройки → Автоматизация чатов.")
        return
    n = sum(services.db.resume_chats(c.id) for c in conns)
    services.event(f"Менеджер снял паузы командой /resume ({n})")
    await message.answer(f"Паузы сняты ({n}). Бот снова отвечает клиентам." if n else "Пауз не было — бот и так отвечает.")


async def on_status(message: Message, services: Services) -> None:
    if not services.db.is_approved_owner(message.from_user.id):
        await message.answer("Статистика доступна только менеджеру, который подключил бота.")
        return
    s = services.db.stats()
    accept = f"{s['draft_accept_rate']:.0%}" if s["draft_accept_rate"] is not None else "—"
    await message.answer(
        f"Всего вопросов: {s['real']}\n"
        f"Бот ответил сам: {s['auto_share']:.0%}\n"
        f"Передал вам: {s['escalated_share']:.0%}\n"
        f"Черновики приняты без правок: {accept}\n"
        f"Отмечено ошибок: {s['flagged']}\n"
        f"Среднее время ответа: {s['avg_latency_ms']} мс"
    )


def age_minutes(sent_at: dt.datetime) -> float:
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - sent_at).total_seconds() / 60


def is_stale(sent_at: dt.datetime, minutes: int) -> bool:
    return age_minutes(sent_at) > minutes


def send_failure(conn: D.Connection) -> str:
    if not conn.can_reply:
        return ("Не отправлено: у бота нет права отвечать. Включите его: Настройки → Автоматизация чатов "
                "→ бот → «Отвечать на сообщения».")
    return ("Не отправлено: нет связи с Telegram или клиент писал больше 24 часов назад. "
            "Ответьте ему сами в чате.")


def mode_name(mode: str) -> str:
    return {"auto": "автоответ", D.WATCH: "наблюдение"}.get(mode, "черновики для менеджера")


WATCH_NOTE = ("\nБот ничего не пишет клиентам и не присылает карточек: запоминает вопросы, ваши ответы и через сколько "
              "вы ответили. Ваши ответы попадут в предложения для базы знаний. Через неделю-две включите /mode auto.")


def wait_text(minutes: float) -> str:
    if minutes < 1:
        return "меньше минуты"
    return f"{minutes:.0f} мин" if minutes < 90 else f"{minutes / 60:.1f} ч".replace(".", ",")


TELEGRAM_LIMIT = 4096  # длиннее Telegram не принимает


def fit(text: str) -> str:
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1].rstrip() + "…"


async def send_to_client(bot: Bot, connection_id: str, chat_id: int, text: str) -> bool:
    try:
        await with_retry(bot.send_message, chat_id, fit(text), business_connection_id=connection_id)
        return True
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить клиенту: %s", exc)
        return False


async def with_retry(send, *args, **kwargs):
    """Связь с Telegram бывает нестабильной: при сетевом сбое пробуем ещё раз через пару секунд."""
    try:
        return await send(*args, **kwargs)
    except TelegramRetryAfter as exc:  # Telegram просит подождать: много сообщений подряд
        await asyncio.sleep(min(exc.retry_after, 30))
        return await send(*args, **kwargs)
    except TelegramNetworkError:
        await asyncio.sleep(2)
        return await send(*args, **kwargs)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await with_retry(bot.send_message, chat_id, fit(text), **kwargs)
    except TelegramForbiddenError:
        log.warning("Менеджер заблокировал бота — карточки и сводки до него не дойдут (чат %s)", chat_id)
    except TelegramAPIError as exc:
        log.warning("Не удалось написать менеджеру (нет связи с Telegram?): %s", exc)


def make_router() -> Router:
    router = Router()
    router.business_connection.register(on_business_connection)
    router.business_message.register(on_business_message)
    router.edited_business_message.register(on_edited_business_message)
    router.deleted_business_messages.register(on_deleted_business_messages)
    router.callback_query.register(on_draft_decision, F.data.regexp(r"^(send|skip):(\d+)$"))
    router.callback_query.register(on_flag, F.data.regexp(r"^flag:(\d+)$"))
    router.callback_query.register(on_candidate_decision, F.data.regexp(r"^cand:(ok|no):(\d+)$"))
    router.message.register(on_manager_edit, F.chat.type == "private", F.reply_to_message, F.text, ~F.text.startswith("/"))
    router.message.register(on_start, CommandStart())
    router.message.register(on_mode, Command("mode"))
    router.message.register(on_status, Command("status"))
    router.message.register(on_resume, Command("resume"))
    return router


def build_dispatcher(services: Services) -> Dispatcher:
    dp = Dispatcher()
    dp["services"] = services
    dp["buffer"] = MessageBuffer()
    dp.include_router(make_router())
    return dp
