"""Адаптер MAX — второй канал: Telegram в России замедляют с февраля 2026.

Чем MAX отличается от Telegram Business:
- Отвечать от личного аккаунта менеджера бот не может. Бот работает от организации (ИП, юрлицо или самозанятый
  после верификации на dev.max.ru), и клиенты пишут прямо в бот компании. Поэтому «менеджер написал клиенту
  сам» здесь не бывает: менеджер отвечает через карточку — кнопкой «Отправить» или ответом на неё своим текстом.
- Менеджер — тот, кто написал боту /manager. Первого менеджера бот одобряет сам, остальных — кнопка «Разрешить»
  в админке, как чужие подключения Telegram. Подключение — `max:<user_id менеджера>`; вопросы клиентов идут
  в последнее одобренное подключение.
- Окна в 24 часа, как в Telegram, нет.
Логика режимов общая (`flow.process_client_message`): ядро и Telegram-адаптер не меняются.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time

import httpx

from .. import db as D
from ..services import Services
from .flow import HOLDING_SENT, process_client_message
from .max_api import Buttons, MaxApi, MaxError
from .telegram_business import (_DRAFT_ID_RE, MEDIA_KINDS, MessageBuffer, build_candidates_message, build_summary,
                                collect_quietly, mode_name, short)

log = logging.getLogger(__name__)

CONN_PREFIX = "max:"
SUMMARY_KEY = "summary_sent_max"  # у Telegram свой ключ: сводки каналов не мешают друг другу
HOLDING_NOT_SENT = "Клиенту ничего не ушло: нет связи с MAX."
# В карточке flow.py предлагает ответить «в его чате» — в MAX чата клиента у менеджера нет
_CARD_HINT = "Ответьте клиенту в его чате или прямо здесь, ответом на это сообщение"
_MAX_CARD_HINT = "Ответьте на это сообщение своим текстом"
# Вложения, которые бот не разбирает: как голосовое или файл в Telegram
_MEDIA = {"image": MEDIA_KINDS["photo"], "video": MEDIA_KINDS["video"], "audio": MEDIA_KINDS["audio"],
          "file": MEDIA_KINDS["document"]}

HELP = ("Команды менеджера:\n"
        "/mode draft — черновики: ответ сначала приходит вам\n"
        "/mode auto — автоответ: бот отвечает клиенту сам\n"
        "/status — статистика\n"
        "/resume — снять паузы в чатах клиентов\n\n"
        "Чтобы ответить клиенту, ответьте на его карточку своим текстом.")


def draft_buttons(dialog_id: int) -> Buttons:
    return [[("Отправить", f"send:{dialog_id}"), ("Пропустить", f"skip:{dialog_id}")]]


def active_connection(services: Services) -> D.Connection | None:
    """Подключение, в которое идут вопросы клиентов: последний одобренный и включённый менеджер MAX."""
    return next((c for c in services.db.connections()
                 if c.id.startswith(CONN_PREFIX) and c.approved and c.enabled), None)


def user_name(user: dict) -> str:
    """«Иван Петров (@ivan)» — чтобы менеджер понял, о ком карточка."""
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("name") or "клиент"
    return f"{name} (@{user['username']})" if user.get("username") else name


def media_kind(attachments: list[dict]) -> str | None:
    """Голосовое, фото, файл — передаём менеджеру; стикеры, геолокацию, контакт молча пропускаем."""
    return next((_MEDIA[a.get("type")] for a in attachments if a.get("type") in _MEDIA), None)


def age_minutes(timestamp_ms: int | float | None) -> float:
    return (time.time() - (timestamp_ms or 0) / 1000) / 60 if timestamp_ms else 0.0


def for_max(card: str) -> str:
    return card.replace(_CARD_HINT, _MAX_CARD_HINT)


class MaxBot:
    """События MAX → решения бота. Транспорт — `MaxApi` (в тестах — подмена с тем же интерфейсом)."""

    def __init__(self, api: MaxApi, services: Services, me_id: int = 0):
        self.api, self.services, self.me_id = api, services, me_id
        self.buffer = MessageBuffer()

    # --- отправка ---
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> bool:
        try:
            await self.api.send(text, chat_id=chat_id, buttons=buttons)
            return True
        except (MaxError, httpx.HTTPError) as exc:
            log.warning("MAX: не удалось отправить сообщение в чат %s: %s", chat_id, exc)
            return False

    async def answer(self, callback_id: str, note: str, text: str | None = None, buttons: Buttons | None = None) -> None:
        with contextlib.suppress(MaxError, httpx.HTTPError):
            await self.api.answer(callback_id, notification=note[:200], text=text, buttons=buttons)

    # --- события ---
    async def handle(self, update: dict) -> None:
        kind = update.get("update_type")
        if kind == "message_created":
            await self.on_message(update.get("message") or {})
        elif kind == "message_callback":
            await self.on_callback(update.get("callback") or {}, update.get("message") or {})
        elif kind == "bot_started":
            await self.on_started(update)
        elif kind == "message_edited":
            self.on_edited(update.get("message") or {})
        elif kind == "message_removed":
            self.on_removed(update)

    async def on_started(self, update: dict) -> None:
        """Клиент нажал «Начать» в боте компании — здороваемся шаблоном, модель не тратим."""
        user = update.get("user") or {}
        if self.services.db.get_connection(f"{CONN_PREFIX}{user.get('user_id')}"):
            await self.send(update.get("chat_id"), HELP)
        elif active_connection(self.services):
            await self.send(update.get("chat_id"), self.services.sign(self.services.assistant.greeting_message))

    async def on_message(self, message: dict) -> None:
        sender, recipient, body = message.get("sender") or {}, message.get("recipient") or {}, message.get("body") or {}
        user_id = sender.get("user_id")
        # Свои сообщения и другие боты: два бота не должны переписываться бесконечно
        if not user_id or sender.get("is_bot") or user_id == self.me_id:
            return
        if recipient.get("chat_type") != "dialog":
            return  # бота добавили в групповой чат — там не отвечаем
        chat_id, text = recipient.get("chat_id"), (body.get("text") or "").strip()
        services = self.services
        manager = services.db.get_connection(f"{CONN_PREFIX}{user_id}")
        if text.startswith("/"):
            await self.on_command(text, sender, chat_id, manager)
            return
        if manager:
            await self.on_manager_message(message, manager, chat_id, text)
            return

        conn = active_connection(services)
        if not conn:
            services.event("MAX: пишет клиент, но менеджер ещё не подключён (/manager в боте) — пропускаю")
            return
        client = user_name(sender)
        services.chat_names[chat_id] = client
        if age_minutes(message.get("timestamp")) > services.settings.stale_message_minutes:
            services.event(f"{client}: старое сообщение (бот был выключен) — пропускаю")
            return
        if not text:
            kind = media_kind(body.get("attachments") or [])
            if kind and not services.db.is_paused(conn.id, chat_id):
                await self.hand_media_to_manager(conn, chat_id, kind, client)
            return
        if services.db.is_paused(conn.id, chat_id):
            services.event(f"{client}: «{short(text, 60)}» — чат на паузе, бот молчит (снять: /resume)")
            return

        key = (conn.id, chat_id)
        self.buffer.parts.setdefault(key, []).append((body.get("mid"), text))
        self.buffer.names[key] = client
        delay = float(services.current()["merge_seconds"])
        if delay <= 0:
            await self.answer_client(conn.id, chat_id, self.buffer.take(key), self.buffer.names.pop(key, ""))
            return
        # Клиент ещё печатает: ждём паузу и отвечаем на всё разом
        if (old := self.buffer.timers.pop(key, None)) is not None:
            old.cancel()
        self.buffer.timers[key] = asyncio.create_task(self.answer_later(key, delay))

    def on_edited(self, message: dict) -> None:
        """Клиент поправил сообщение, пока бот ждал паузы, — отвечаем на исправленный текст."""
        body, chat_id = message.get("body") or {}, (message.get("recipient") or {}).get("chat_id")
        conn = active_connection(self.services)
        key = (conn.id if conn else "", chat_id)
        if key in self.buffer.parts and body.get("text"):
            self.buffer.parts[key] = [(mid, body["text"] if mid == body.get("mid") else old)
                                      for mid, old in self.buffer.parts[key]]

    def on_removed(self, update: dict) -> None:
        """Клиент удалил сообщение, пока бот ждал паузы, — на удалённое не отвечаем."""
        conn = active_connection(self.services)
        key = (conn.id if conn else "", update.get("chat_id"))
        if key in self.buffer.parts:
            self.buffer.parts[key] = [(mid, t) for mid, t in self.buffer.parts[key] if mid != update.get("message_id")]
            if not self.buffer.parts[key]:
                self.buffer.parts.pop(key)
                if (timer := self.buffer.timers.pop(key, None)) is not None:
                    timer.cancel()

    async def answer_later(self, key: tuple[str, int], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # пришло ещё сообщение — ответим на всё вместе позже
        self.buffer.timers.pop(key, None)
        text, client = self.buffer.take(key), self.buffer.names.pop(key, "")
        if not text:
            return
        try:
            await self.answer_client(*key, text, client)
        except Exception:  # noqa: BLE001 — фоновая задача: ошибку только в лог
            log.exception("MAX: не удалось ответить в чате %s", key[1])

    async def answer_client(self, connection_id: str, chat_id: int, text: str, client: str = "") -> None:
        async with self.services.chat_lock(connection_id, chat_id):
            await self._answer_client(connection_id, chat_id, text, client)

    async def _answer_client(self, connection_id: str, chat_id: int, text: str, client: str) -> None:
        services = self.services
        conn = services.db.get_connection(connection_id)
        if not conn or not conn.enabled:
            return
        out = await process_client_message(services, conn, chat_id, text, client)
        outcome = ("бот ответил сам" if out.to_client and not out.to_manager
                   else "клиенту «Уточню и вернусь», вопрос — менеджеру" if out.to_client
                   else "черновик менеджеру" if out.with_buttons
                   else "вопрос — менеджеру" if out.to_manager else "отвечать не нужно — молчит")
        services.event(f"MAX, {client or chat_id}: «{short(text, 60)}» — {outcome}")
        sent = False
        if out.to_client:
            sent = await self.send(chat_id, out.to_client)
            if not sent and out.dialog_id and not out.to_manager:
                # Автоответ не дошёл — вопрос забирает менеджер
                services.db.set_dialog_status(out.dialog_id, D.ESCALATED)
                await self.send(conn.owner_chat_id, f"Не получилось отправить ответ клиенту {client} через MAX.\n\n"
                                                    f"Вопрос клиента:\n«{text}»")
        if out.notice and sent:
            await self.send(conn.owner_chat_id, out.notice)
        if out.to_manager:
            card = out.to_manager if sent or not out.to_client else out.to_manager.replace(HOLDING_SENT, HOLDING_NOT_SENT)
            await self.send(conn.owner_chat_id, for_max(card), draft_buttons(out.dialog_id) if out.with_buttons else None)

    async def hand_media_to_manager(self, conn: D.Connection, chat_id: int, kind: str, client: str) -> None:
        """Голосовое, фото без подписи, файл: бот их не разбирает — разговор забирает менеджер."""
        services = self.services
        # Пауза — до первого await: альбом приходит несколькими сообщениями сразу, а «уточню» и карточка нужны одни
        services.db.pause_chat(conn.id, chat_id, services.settings.manager_pause_minutes)
        auto = conn.mode == "auto" and services.auto_allowed()
        holding = services.assistant.holding_message
        dialog_id = services.db.log_dialog(
            connection_id=conn.id, chat_id=chat_id, question=f"[{kind}]", answer=holding if auto else "",
            decision="escalate", reason="media", confidence=0.0, status=D.ESCALATED, sources=[], latency_ms=0,
            mode=conn.mode, kb_version=services.kb_version)
        sent = auto and await self.send(chat_id, services.sign(holding))
        services.event(f"MAX, {client}: {kind} — бот такие не разбирает, передал менеджеру")
        await self.send(conn.owner_chat_id,
                        f"{client} прислал(а) {kind} — бот такие не разбирает.\n"
                        f"{HOLDING_SENT + ' ' if sent else ''}"
                        f"Вопрос #{dialog_id}. {_MAX_CARD_HINT} — бот перешлёт его клиенту.")

    # --- менеджер ---
    async def on_command(self, text: str, sender: dict, chat_id: int, manager: D.Connection | None) -> None:
        command, _, args = text.partition(" ")
        command = command.split("@")[0].lower()
        services = self.services
        if command == "/manager":
            await self.register_manager(sender, chat_id)
        elif manager is None or not manager.approved:
            # Клиенту команды не нужны; /start — то же, что кнопка «Начать»
            if command == "/start" and manager is None and active_connection(services):
                await self.send(chat_id, services.sign(services.assistant.greeting_message))
            elif manager is not None:
                await self.send(chat_id, "Запрос ждёт разрешения администратора: админка → Обзор → Подключения.")
        elif command == "/mode":
            mode = args.strip().lower()
            if mode not in {"draft", "auto"}:
                await self.send(chat_id, "Укажите режим: /mode draft или /mode auto")
                return
            services.db.set_mode(mode, connection_id=manager.id)
            services.event(f"MAX: менеджер включил режим «{mode_name(mode)}»")
            await self.send(chat_id, f"Режим: {mode_name(mode)}.")
        elif command == "/status":
            s = services.db.stats()
            accept = f"{s['draft_accept_rate']:.0%}" if s["draft_accept_rate"] is not None else "—"
            await self.send(chat_id, f"Всего вопросов: {s['real']}\nБот ответил сам: {s['auto_share']:.0%}\n"
                                     f"Передал вам: {s['escalated_share']:.0%}\nЧерновики приняты без правок: {accept}\n"
                                     f"Отмечено ошибок: {s['flagged']}\nСреднее время ответа: {s['avg_latency_ms']} мс")
        elif command == "/resume":
            n = services.db.resume_chats(manager.id)
            services.event(f"MAX: менеджер снял паузы командой /resume ({n})")
            await self.send(chat_id, f"Паузы сняты ({n}). Бот снова отвечает клиентам." if n
                            else "Пауз не было — бот и так отвечает.")
        else:
            await self.send(chat_id, HELP)

    async def register_manager(self, sender: dict, chat_id: int) -> None:
        services, user_id = self.services, sender["user_id"]
        conn = services.db.upsert_connection(f"{CONN_PREFIX}{user_id}", user_id, chat_id, True, True,
                                             services.settings.default_mode)
        services.owner_names[user_id] = f"MAX: {user_name(sender)}"
        if not conn.approved:
            # Бот публичный: /manager может написать любой. Чужие ждут разрешения, иначе тратили бы нашу модель
            services.event(f"MAX: {user_name(sender)} (id {user_id}) просит доступ менеджера — "
                           "ждёт разрешения: Обзор → Подключения")
            await self.send(chat_id, "Запрос отправлен администратору. Когда он разрешит доступ, "
                                     "сюда начнут приходить вопросы клиентов.")
            return
        services.event(f"MAX: менеджер {user_name(sender)} подключён, режим: {mode_name(conn.mode)}")
        await self.send(chat_id, f"Готово: сюда будут приходить вопросы клиентов и черновики. "
                                 f"Режим: {mode_name(conn.mode)}.\n\n{HELP}")

    async def on_manager_message(self, message: dict, manager: D.Connection, chat_id: int, text: str) -> None:
        link = message.get("link") or {}
        if not manager.approved:
            await self.send(chat_id, "Запрос ждёт разрешения администратора: админка → Обзор → Подключения.")
            return
        if link.get("type") != "reply" or not text:
            await self.send(chat_id, HELP)
            return
        # Номер карточки — последний: в тексте вопроса клиента тоже может встретиться «Вопрос #5»
        ids = _DRAFT_ID_RE.findall(((link.get("message") or {}).get("text")) or "")
        if not ids:
            await self.send(chat_id, HELP)
            return
        services = self.services
        dialog = services.db.get_dialog(int(ids[-1]))
        if not dialog or dialog["status"] not in (D.DRAFT_PENDING, D.ESCALATED):
            await self.send(chat_id, "Этот черновик уже обработан.")
            return
        if dialog["connection_id"] != manager.id:
            return
        if not services.db.claim_dialog(dialog["id"], D.DRAFT_EDITED, answer=text):
            await self.send(chat_id, "Этот черновик уже обработан.")
            return
        if await self.send(dialog["chat_id"], text):
            await self.send(chat_id, "Ваш вариант отправлен клиенту.")
            await collect_quietly(services, "card", manager.id, dialog["chat_id"], dialog["id"], dialog["question"], text)
        else:
            services.db.set_dialog_status(dialog["id"], dialog["status"], answer=dialog["answer"])
            await self.send(chat_id, "Не отправлено: нет связи с MAX. Попробуйте ещё раз.")

    async def on_callback(self, callback: dict, message: dict) -> None:
        payload, callback_id = callback.get("payload") or "", callback.get("callback_id") or ""
        user_id = (callback.get("user") or {}).get("user_id")
        action, _, rest = payload.partition(":")
        services = self.services
        if action == "cand":
            await self.on_candidate(callback_id, user_id, rest)
            return
        if action not in {"send", "skip", "flag"} or not rest.isdigit():
            return
        dialog = services.db.get_dialog(int(rest))
        conn = services.db.get_connection(dialog["connection_id"]) if dialog else None
        if not conn or not conn.id.startswith(CONN_PREFIX) or user_id != conn.owner_user_id:
            await self.answer(callback_id, "Нет доступа.")
            return
        if action == "flag":
            services.db.flag_dialog(dialog["id"], True)
            await self.answer(callback_id, "Отметил как ошибку. Спасибо!")
            return
        if dialog["status"] not in (D.DRAFT_PENDING, D.ESCALATED):
            await self.answer(callback_id, "Черновик уже обработан.")
            return
        ok = True
        if action == "skip":
            claimed, note = services.db.claim_dialog(dialog["id"], D.DRAFT_REJECTED), "Пропущено."
        else:
            # Сначала закрываем черновик, потом шлём: второе нажатие, пока идёт отправка, ничего не сделает
            claimed = services.db.claim_dialog(dialog["id"], D.DRAFT_SENT)
            note = ""
            if claimed:
                ok = await self.send(dialog["chat_id"], services.sign(dialog["answer"]))
                note = "Отправлено клиенту." if ok else "Не отправлено: нет связи с MAX. Попробуйте ещё раз."
                if not ok:  # вернуть черновик: менеджер сможет нажать ещё раз
                    services.db.set_dialog_status(dialog["id"], dialog["status"])
        if not claimed:
            await self.answer(callback_id, "Черновик уже обработан.")
            return
        card = ((message.get("body") or {}).get("text")) or ""
        # Кнопки убираем, только если всё получилось; при сбое оставляем, чтобы нажать ещё раз
        await self.answer(callback_id, note, text=f"{card}\n\n{note}" if card else None,
                          buttons=None if ok else draft_buttons(dialog["id"]))

    async def on_candidate(self, callback_id: str, user_id: int, rest: str) -> None:
        """Предложение в базу знаний из вечерней сводки: ✓ утвердить, ✗ отклонить."""
        action, _, candidate_id = rest.partition(":")
        services = self.services
        row = services.db.get_candidate(int(candidate_id)) if candidate_id.isdigit() else None
        conn = services.db.get_connection(row["connection_id"]) if row and row["connection_id"] else None
        owners = {conn.owner_user_id} if conn else {c.owner_user_id for c in services.db.connections()
                                                    if c.approved and c.id.startswith(CONN_PREFIX)}
        if not row or user_id not in owners:
            await self.answer(callback_id, "Нет доступа.")
        elif row["status"] != "new":
            await self.answer(callback_id, "Уже разобрано.")
        elif action == "no":
            services.db.decide_candidates([row["id"]], "rejected")
            await self.answer(callback_id, "Отклонено.")
        else:
            _, msg = services.approve_candidates([row["id"]], row["question"], row["answer"])
            await self.answer(callback_id, msg)

    # --- вечерняя сводка ---
    async def maybe_send_summaries(self, now: dt.datetime | None = None) -> int:
        """Раз в день после заданного времени — итоги и предложения в базу менеджерам MAX (у Telegram свой ключ)."""
        services = self.services
        when = services.current()["summary_time"]
        now = now or services.local_now()
        today = now.strftime("%Y-%m-%d")
        if not when or now.strftime("%H:%M") < when or services.db.get_settings().get(SUMMARY_KEY) == today:
            return 0
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        sent = 0
        for conn in services.db.connections():
            if not conn.id.startswith(CONN_PREFIX) or not conn.enabled or not conn.approved:
                continue
            if summary := build_summary(services, conn, start):
                await self.send(conn.owner_chat_id, summary[0], buttons_of(summary[1]))
                sent += 1
            if proposals := build_candidates_message(services, conn):
                await self.send(conn.owner_chat_id, proposals[0], buttons_of(proposals[1]))
        services.db.set_settings({SUMMARY_KEY: today})
        return sent


def buttons_of(markup) -> Buttons | None:
    """Кнопки сводки строит Telegram-адаптер (aiogram) — переносим надписи и данные в формат MAX."""
    if markup is None:
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


async def run_max(api: MaxApi, services: Services) -> None:
    """Слушать MAX долгим опросом. Связи нет — пробуем снова (5 с, 10 с … до минуты);
    окончательно останавливаемся, только если MAX не принял токен."""
    delay = 5
    while True:
        try:
            me = await api.me()
            break
        except MaxError as exc:
            if exc.status == 401:
                services.max_status = {"ok": False, "error": "MAX не принял токен — проверьте MAX_BOT_TOKEN в .env"}
                log.error("%s. MAX не работает.", services.max_status["error"])
                return
            services.max_status = {"ok": False, "error": f"MAX ответил ошибкой {exc.status} — пробую снова"}
        except httpx.HTTPError as exc:
            services.max_status = {"ok": False, "error": f"нет связи с MAX ({type(exc).__name__}) — пробую снова"}
        log.warning("MAX: %s, повтор через %d с", services.max_status["error"], delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

    bot = MaxBot(api, services, me.get("user_id", 0))
    services.client_senders["max"] = lambda _conn_id, chat_id, text: bot.send(chat_id, text)
    services.max_status = {"ok": True, "username": me.get("username") or "", "name": me.get("first_name") or ""}
    log.info("Бот MAX %s запущен", me.get("username") or me.get("first_name") or me.get("user_id"))
    summary = asyncio.create_task(summary_loop(bot))
    marker, delay = None, 5
    try:
        while True:
            try:
                data = await api.updates(marker)
                delay = 5
            except MaxError as exc:
                if exc.status == 401:
                    services.max_status = {"ok": False, "error": "MAX отозвал токен — проверьте MAX_BOT_TOKEN в .env"}
                    log.error(services.max_status["error"])
                    return
                log.warning("MAX: ошибка опроса %s — повтор через %d с", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            except httpx.HTTPError as exc:
                log.warning("MAX: нет связи (%s) — повтор через %d с", type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            marker = data.get("marker", marker)
            for update in data.get("updates") or []:
                try:
                    await bot.handle(update)
                except Exception:  # noqa: BLE001 — одно сломанное событие не должно останавливать бота
                    log.exception("MAX: не удалось обработать событие %s", update.get("update_type"))
    finally:
        summary.cancel()


async def summary_loop(bot: MaxBot) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await bot.maybe_send_summaries()
        except Exception:  # noqa: BLE001 — сводка не должна ронять бота
            log.exception("MAX: не удалось отправить вечернюю сводку")
