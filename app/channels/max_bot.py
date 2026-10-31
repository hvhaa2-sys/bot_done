"""Адаптер MAX — второй канал: Telegram в России замедляют с февраля 2026.

Чем MAX отличается от Telegram Business:
- Отвечать от личного аккаунта менеджера бот не может. Бот работает от организации (ИП, юрлицо или самозанятый
  после верификации на business.max.ru), и клиенты пишут прямо в бот компании. Поэтому «менеджер написал клиенту
  сам» здесь не бывает: менеджер отвечает через карточку — кнопкой «Отправить» или ответом на неё своим текстом.
- Менеджер — тот, кто написал боту /manager. Первого менеджера бот одобряет сам, остальных — кнопка «Разрешить»
  в админке, как чужие подключения Telegram. Подключение — `max:<user_id менеджера>`; вопросы клиентов идут
  в последнее одобренное подключение.
- Окна в 24 часа, как в Telegram, нет.
- Своего чата с клиентом у менеджера в MAX нет: всё, на что бот не отвечает сам (наблюдение, чат на паузе, сообщения,
  пришедшие, пока бот был выключен), бот пересылает менеджеру, иначе сообщение клиента потеряется. Фото, голосовые
  и файлы пересылаются как есть, чтобы менеджер их увидел.
- Наблюдение в MAX: бот сам не отвечает и модель не тратит — пересылает вопросы менеджеру, а его ответы (ответом
  на карточку) клиенту. Переписка пишется в журнал наблюдения, время ответа замеряется, ответы менеджера становятся
  предложениями в базу знаний — как в Telegram. «Выключен» в MAX нет: клиентам, кроме бота, никто не ответит.
Логика режимов общая (`flow.process_client_message`): ядро и Telegram-адаптер не меняются.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as dt
import logging
import re
import time

import httpx

from .. import db as D
from ..core.assistant import R_CATALOG
from ..core.conversation import Conversation
from ..core.pii import find_phone
from ..services import Services
from .flow import HOLDING_SENT, process_client_message
from .observation import collect_observed
from .max_api import REQUEST_CONTACT, Buttons, MaxApi, MaxError
from .telegram_business import (_DRAFT_ID_RE, MEDIA_KINDS, MessageBuffer, build_candidates_message, build_summary,
                                collect_quietly, mode_name, short, wait_text)

log = logging.getLogger(__name__)

CONN_PREFIX = "max:"
SUMMARY_KEY = "summary_sent_max"  # у Telegram свой ключ: сводки каналов не мешают друг другу
HOLDING_NOT_SENT = "Клиенту ничего не ушло: нет связи с MAX."
# В карточке flow.py предлагает ответить «в его чате» — в MAX чата клиента у менеджера нет
_CARD_HINT = "Ответьте клиенту в его чате или прямо здесь, ответом на это сообщение"
_MAX_CARD_HINT = "Ответьте на это сообщение своим текстом"
_RELAY_HINT = "Ответьте на это сообщение — ответ уйдёт клиенту."
OBSERVE_NOTE = ("\nБот сам клиентам не отвечает и модель не тратит: пересылает вопросы вам, а ваш ответ на карточку "
                "отправляет клиенту. Запоминает, через сколько вы ответили, и предлагает ваши ответы в базу знаний. "
                "Когда база наберётся, включите /mode draft или /mode auto.")
MAX_MODES = ("observe", "draft", "auto")  # «Выключен» нет: клиентам в MAX, кроме бота, никто не ответит
# Вложения, которые бот не разбирает: как голосовое или файл в Telegram
_MEDIA = {"image": MEDIA_KINDS["photo"], "video": MEDIA_KINDS["video"], "audio": MEDIA_KINDS["audio"],
          "file": MEDIA_KINDS["document"], "contact": "контакт", "location": "геолокацию", "share": "ссылку"}
_FILES = ("image", "video", "audio", "file")  # можно отправить заново по токену из полученного сообщения
UPDATE_TYPES = ("message_created", "message_callback", "bot_started", "message_edited", "message_removed")
SUBSCRIPTION_CHECK_SECONDS = 300  # если подписка пропала, бот иначе так и молчал бы: проверяем и подписываемся снова
SEEN_LIMIT = 2000  # сколько последних событий помним: MAX может доставить одно и то же повторно
CLIENT_MENU: Buttons = [[("Форматы и цены", "menu:catalog")], [("Подобрать под бюджет", "menu:budget")],
                        [("Оставить заявку", "menu:lead")], [("Позвать менеджера", "menu:human")]]
AFTER_ANSWER: Buttons = [[("Оставить заявку", "menu:lead"), ("Позвать менеджера", "menu:human")]]
ASK_CONTACT: Buttons = [[("Отправить мой контакт", REQUEST_CONTACT)]]
BUDGET_ASK = ("Напишите бюджет на месяц, например «50 тысяч». Если уже знаете торговый центр или город, добавьте их — "
              "подберу варианты.")
LEAD_ASK = "Чтобы менеджер связался с вами, нажмите кнопку ниже или напишите телефон сообщением."
LEAD_DONE = "Спасибо! Передал заявку менеджеру, он свяжется с вами."
HUMAN_DONE = "Передал менеджеру, он ответит здесь, в этом чате."
HUMAN_AGAIN = "Менеджер уже получил вашу просьбу и ответит здесь."
NOT_DELIVERED = "Не получилось передать менеджеру. Попробуйте, пожалуйста, ещё раз чуть позже."
OBSERVE_MENU = "Напишите вопрос сообщением — менеджер ответит здесь."
WAITING_SECONDS = 1800   # столько бот считает, что следующее сообщение — ответ на кнопку
HUMAN_REPEAT_SECONDS = 600  # повторное нажатие «Позвать менеджера» не дёргает его ещё раз
_WANTS_HUMAN_RE = re.compile(r"\b(?:позов\w*|позвать|соедин\w*|переключ\w*|свяж\w*|хочу|нужен|нужна|дайте|можно)\b"
                             r".{0,25}\b(?:менеджер\w*|человек\w*|оператор\w*|сотрудник\w*|консультант\w*)", re.I)
_SOURCE_RE = re.compile(r"^[\w-]{1,64}$")
_VCF_TEL_RE = re.compile(r"^TEL[^:\r\n]*:\s*([+\d][\d\s()-]{5,})", re.I | re.M)
NO_MANAGER = "Менеджер сейчас не на связи. Напишите, пожалуйста, чуть позже."

HELP = ("Команды менеджера:\n"
        "/mode observe — наблюдение: бот не отвечает сам, пересылает вопросы вам и учится на ваших ответах\n"
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
    """Голосовое, фото, файл, контакт, геолокация — передаём менеджеру; стикеры молча пропускаем."""
    return next((_MEDIA[a.get("type")] for a in attachments if a.get("type") in _MEDIA), None)


def files_of(body: dict) -> list[dict]:
    """Вложения для повторной отправки: только с токеном, иначе MAX их не примет."""
    return [{"type": a["type"], "payload": {"token": a["payload"]["token"]}} for a in body.get("attachments") or []
            if a.get("type") in _FILES and (a.get("payload") or {}).get("token")]


def contact_phone(attachments: list[dict]) -> str:
    """Телефон из контакта, который клиент отправил кнопкой (формат vCard)."""
    for a in attachments:
        if a.get("type") == "contact":
            match = _VCF_TEL_RE.search((a.get("payload") or {}).get("vcf_info") or "")
            if match:
                return match.group(1).strip()
    return ""


def has_contact(attachments: list[dict]) -> bool:
    return any(a.get("type") == "contact" for a in attachments)


def wants_human(text: str) -> bool:
    return len(text) <= 80 and bool(_WANTS_HUMAN_RE.search(text))


def age_minutes(timestamp_ms: int | float | None) -> float:
    return (time.time() - (timestamp_ms or 0) / 1000) / 60 if timestamp_ms else 0.0


def for_max(card: str) -> str:
    return card.replace(_CARD_HINT, _MAX_CARD_HINT)


class MaxBot:
    """События MAX → решения бота. Транспорт — `MaxApi` (в тестах — подмена с тем же интерфейсом)."""

    def __init__(self, api: MaxApi, services: Services, me_id: int = 0):
        self.api, self.services, self.me_id = api, services, me_id
        self.buffer = MessageBuffer()
        self.seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.waiting: dict[int, tuple[str, float]] = {}  # чат → чего бот ждёт после кнопки: budget или lead
        self.called: dict[int, float] = {}               # чат → когда клиент последний раз звал менеджера

    # --- отправка ---
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None, forward: str | None = None,
                   files: list[dict] | None = None) -> bool:
        return await self.send_message(chat_id, text, buttons, forward, files) is not None

    async def show_media(self, chat_id: int, body: dict) -> None:
        """Показать менеджеру вложение клиента: переслать, а если MAX не даст — отправить заново по токену."""
        if not await self.send(chat_id, "", forward=body.get("mid")) and (files := files_of(body)):
            await self.send(chat_id, "", files=files)

    async def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None, forward: str | None = None,
                           files: list[dict] | None = None) -> dict | None:
        """Отправленное сообщение (нужен его номер для журнала наблюдения) или None, если не получилось."""
        try:
            # Файл должен быть единственным вложением в сообщении: остальные — отдельными сообщениями
            sent = await self.api.send(text, chat_id=chat_id, buttons=buttons, forward=forward, files=(files or [])[:1] or None)
            for extra in (files or [])[1:]:
                await self.api.send("", chat_id=chat_id, files=[extra])
            return (sent or {}).get("message") or {}
        except (MaxError, httpx.HTTPError) as exc:
            log.warning("MAX: не удалось отправить сообщение в чат %s: %s", chat_id, exc)
            return None

    async def answer(self, callback_id: str, note: str, text: str | None = None, buttons: Buttons | None = None) -> None:
        with contextlib.suppress(MaxError, httpx.HTTPError):
            await self.api.answer(callback_id, notification=note[:200], text=text, buttons=buttons)

    # --- события ---
    def repeated(self, update: dict) -> bool:
        """Вебхук и перезапуск могут доставить событие второй раз: второго ответа клиенту быть не должно."""
        kind = update.get("update_type")
        key = ((update.get("message") or {}).get("body") or {}).get("mid") if kind == "message_created" \
            else (update.get("callback") or {}).get("callback_id") if kind == "message_callback" else None
        if not key:
            return False
        if key in self.seen:
            return True
        self.seen[key] = None
        if len(self.seen) > SEEN_LIMIT:
            self.seen.popitem(last=False)
        return False

    async def handle(self, update: dict) -> None:
        if self.repeated(update):
            return
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
            return
        self.remember_source(update.get("chat_id"), update.get("payload"))
        await self.greet(update.get("chat_id"))

    def remember_source(self, chat_id: int, payload: str | None) -> None:
        """Метка из ссылки max.ru/<бот>?start=<метка>: откуда пришёл клиент. Только номер чата и метка."""
        if chat_id and payload and _SOURCE_RE.match(payload):
            self.services.db.set_client_source(chat_id, payload)

    async def greet(self, chat_id: int) -> None:
        services, conn = self.services, active_connection(self.services)
        if not conn:
            await self.send(chat_id, services.sign(NO_MANAGER))
            return
        # В наблюдении бот сам ничего не делает, поэтому и кнопок нет: вопрос просто уходит менеджеру
        menu = None if conn.mode in D.SILENT_MODES else CLIENT_MENU
        await self.send(chat_id, services.sign(services.assistant.greeting_message), menu)

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
        # Команды — менеджеру; клиенту только /start и /manager, остальное со слэшем — обычное сообщение
        if text.startswith("/") and (manager or text.split()[0].split("@")[0].lower() in ("/start", "/manager")):
            await self.on_command(text, sender, chat_id, manager)
            return
        if manager:
            await self.on_manager_message(message, manager, chat_id, text)
            return

        conn = active_connection(services)
        if not conn:
            services.event("MAX: пишет клиент, но менеджер ещё не подключён (/manager в боте)")
            await self.send(chat_id, services.sign(NO_MANAGER))
            return
        client = user_name(sender)
        services.chat_names[chat_id] = client
        attachments = body.get("attachments") or []
        if conn.mode == D.OBSERVE:
            await self.relay(conn, chat_id, message, client)
            return
        if conn.mode in D.SILENT_MODES:
            return  # «Выключен» в MAX из админки и командой не ставится — разве что прямо в базе
        if has_contact(attachments):  # контакт клиент отправляет только сам: это заявка
            self.waiting.pop(chat_id, None)
            await self.take_lead(conn, chat_id, client, contact_phone(attachments), body)
            return
        if age_minutes(message.get("timestamp")) > services.settings.stale_message_minutes:
            # Отвечать на сообщение многочасовой давности бот не будет, но и терять его нельзя
            await self.relay(conn, chat_id, message, client, "написал(а), пока бот был выключен")
            return
        paused = services.db.is_paused(conn.id, chat_id)
        if not text:
            kind = media_kind(body.get("attachments") or [])
            if kind and not paused:
                await self.hand_media_to_manager(conn, chat_id, kind, client, body)
            elif kind:  # следующие фото альбома: карточка уже ушла, пересылаем только сами фото
                await self.show_media(conn.owner_chat_id, body)
            return
        if paused:
            await self.relay(conn, chat_id, message, client, "пишет в чат на паузе — бот молчит, ответьте сами")
            return
        waiting = self.take_waiting(chat_id)
        if waiting == "lead" and (phone := find_phone(text)):
            await self.take_lead(conn, chat_id, client, phone)
            return
        if wants_human(text):
            await self.call_human(conn, chat_id, client)
            return
        if waiting == "budget" and any(c.isdigit() for c in text) and "бюджет" not in text.lower():
            text = f"Бюджет {text}"  # ответ на кнопку: без этого слова «50» — не бюджет
        if media_kind(body.get("attachments") or []):
            # Фото с подписью: бот отвечает на подпись, а само фото менеджер иначе не увидит
            await self.show_media(conn.owner_chat_id, body)

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
        if out.greeting:
            # В Telegram менеджер видит «Здравствуйте» в своём чате, а здесь клиент остался бы в тишине
            await self.send(chat_id, out.greeting, CLIENT_MENU)
            return
        if out.to_client:
            # Под ответом бота — выход к человеку; под «Уточню и вернусь» нет: менеджер и так получил вопрос
            sent = await self.send(chat_id, out.to_client, None if out.to_manager else AFTER_ANSWER)
            if not sent and out.dialog_id and not out.to_manager:
                # Автоответ не дошёл — вопрос забирает менеджер
                services.db.set_dialog_status(out.dialog_id, D.ESCALATED)
                await self.send(conn.owner_chat_id, f"Не получилось отправить ответ клиенту {client} через MAX.\n\n"
                                                    f"Вопрос клиента:\n«{text}»")
        if out.notice and sent:
            await self.send(conn.owner_chat_id, out.notice)
            self.waiting[chat_id] = ("lead", time.time())  # клиент готов к заказу — самое время попросить контакт
            await self.send(chat_id, services.sign(LEAD_ASK), ASK_CONTACT)
        if out.to_manager:
            card = out.to_manager if sent or not out.to_client else out.to_manager.replace(HOLDING_SENT, HOLDING_NOT_SENT)
            await self.send(conn.owner_chat_id, for_max(card), draft_buttons(out.dialog_id) if out.with_buttons else None)

    def take_waiting(self, chat_id: int) -> str:
        kind, since = self.waiting.pop(chat_id, ("", 0.0))
        return kind if time.time() - since < WAITING_SECONDS else ""

    def what_is_known(self, conn: D.Connection, chat_id: int) -> str:
        """ТЦ, формат, срок, бюджет из слов клиента и его последний вопрос — чтобы менеджер не листал переписку."""
        services = self.services
        known = Conversation.replay(services.kb, services.db.conversation_turns(conn.id, chat_id)).parameters()
        last = services.db.last_question(conn.id, chat_id)
        return ((f"Что известно: {known}\n" if known else "")
                + (f"Последний вопрос: «{short(last, 500)}»\n" if last and not last.startswith("[") else ""))

    async def take_lead(self, conn: D.Connection, chat_id: int, client: str, phone: str = "",
                        body: dict | None = None) -> None:
        """Заявка: готовая карточка менеджеру. Телефон — только в карточке, в журнал не пишем."""
        services = self.services
        known = self.what_is_known(conn, chat_id)
        dialog_id = services.db.log_dialog(
            connection_id=conn.id, chat_id=chat_id, question="[заявка]", answer="", decision="escalate", reason="lead",
            confidence=0.0, status=D.ESCALATED, sources=[], latency_ms=0, mode=conn.mode, kb_version=services.kb_version)
        services.event("MAX: клиент оставил заявку")
        if body and not phone:  # телефон не разобрали — пусть менеджер увидит сам контакт
            await self.show_media(conn.owner_chat_id, body)
        contact = f"Телефон: {phone}\n" if phone else ""
        sent = await self.send(conn.owner_chat_id, f"Заявка: {client}\n{contact}{known}\nВопрос #{dialog_id}. {_RELAY_HINT}")
        await self.send(chat_id, services.sign(LEAD_DONE if sent else NOT_DELIVERED))

    async def call_human(self, conn: D.Connection, chat_id: int, client: str) -> None:
        """Клиент просит человека. В автоответе бот замолкает, чтобы не перебивать менеджера."""
        services = self.services
        if time.time() - self.called.get(chat_id, 0.0) < HUMAN_REPEAT_SECONDS:
            await self.send(chat_id, services.sign(HUMAN_AGAIN))
            return
        known = self.what_is_known(conn, chat_id)
        dialog_id = services.db.log_dialog(
            connection_id=conn.id, chat_id=chat_id, question="[просит менеджера]", answer="", decision="escalate",
            reason="human", confidence=0.0, status=D.ESCALATED, sources=[], latency_ms=0, mode=conn.mode,
            kb_version=services.kb_version)
        services.event("MAX: клиент попросил менеджера")
        sent = await self.send(conn.owner_chat_id, f"{client} просит менеджера.\n{known}\nВопрос #{dialog_id}. {_RELAY_HINT}")
        if sent:
            self.called[chat_id] = time.time()
            if conn.mode == "auto":
                services.db.pause_chat(conn.id, chat_id, services.settings.manager_pause_minutes)
        await self.send(chat_id, services.sign(HUMAN_DONE if sent else NOT_DELIVERED))

    async def on_menu(self, callback_id: str, choice: str, user: dict, message: dict) -> None:
        """Кнопки клиента: без модели, ничего не обещают. Менеджеру в его чате они не нужны."""
        services = self.services
        chat_id = (message.get("recipient") or {}).get("chat_id")
        conn = active_connection(services)
        if not chat_id or services.db.get_connection(f"{CONN_PREFIX}{user.get('user_id')}"):
            await self.answer(callback_id, "Это кнопка для клиентов.")
            return
        if not conn:
            await self.answer(callback_id, NO_MANAGER)
            return
        if conn.mode in D.SILENT_MODES:
            await self.answer(callback_id, OBSERVE_MENU)
            return
        client = user_name(user)
        services.chat_names[chat_id] = client
        if choice == "catalog":
            await self.answer(callback_id, "Показываю форматы")
            reply = await services.answer("Хочу рекламу", [])  # именно эта фраза даёт каталог шаблоном, без модели
            await self.send(chat_id, services.sign(reply.text if reply.reason == R_CATALOG else services.assistant.greeting_message),
                            AFTER_ANSWER)
        elif choice == "budget":
            self.waiting[chat_id] = ("budget", time.time())
            await self.answer(callback_id, "Жду бюджет")
            await self.send(chat_id, services.sign(BUDGET_ASK))
        elif choice == "lead":
            self.waiting[chat_id] = ("lead", time.time())
            await self.answer(callback_id, "Оставьте контакт")
            await self.send(chat_id, services.sign(LEAD_ASK), ASK_CONTACT)
        elif choice == "human":
            await self.answer(callback_id, "Зову менеджера")
            await self.call_human(conn, chat_id, client)
        else:
            await self.answer(callback_id, "Кнопка устарела.")

    async def relay(self, conn: D.Connection, chat_id: int, message: dict, client: str, why: str = "") -> None:
        """Переслать сообщение клиента менеджеру без ответа бота: в наблюдении, на паузе, после простоя.
        Ответ менеджера на эту карточку бот отправит клиенту (`on_manager_message`)."""
        services, body = self.services, message.get("body") or {}
        text = (body.get("text") or "").strip()
        kind = media_kind(body.get("attachments") or [])
        if not text and not kind:
            return  # стикер, геолокация, контакт
        shown = text or f"[{kind}]"
        sent_at = (message.get("timestamp") or time.time() * 1000) / 1000
        if conn.mode == D.OBSERVE:
            # Как наблюдение в Telegram: журнал переписки, файл, замер времени ответа менеджера
            services.db.observe_message(connection_id=conn.id, chat_id=chat_id, message_id=message_seq(message),
                                        role="client", text=shown, sent_at=sent_at)
            services.log_watch_message(conn.id, chat_id, "client", shown, sent_at)
            dialog_id = services.db.watch_question(conn.id, chat_id, shown, sent_at, services.kb_version)
            services.event("MAX, наблюдение: сообщение клиента переслано менеджеру")  # в логе ни имён, ни текста
        else:
            dialog_id = services.db.log_dialog(
                connection_id=conn.id, chat_id=chat_id, question=shown, answer="", decision="escalate",
                reason="relayed", confidence=0.0, status=D.ESCALATED, sources=[], latency_ms=0, mode=conn.mode,
                kb_version=services.kb_version)
            services.event(f"MAX: клиент {why} — переслано менеджеру")
        if kind:
            await self.show_media(conn.owner_chat_id, body)
        head = f"{client} {why}" if why else client
        phone = contact_phone(body.get("attachments") or [])
        quote = f"«{short(text, 1500)}»" if text else f"прислал(а) {kind}" + (f": {phone}" if phone else "")
        await self.send(conn.owner_chat_id, f"{head}:\n{quote}\n\nВопрос #{dialog_id}. {_RELAY_HINT}")

    async def hand_media_to_manager(self, conn: D.Connection, chat_id: int, kind: str, client: str,
                                    body: dict | None = None) -> None:
        """Голосовое, фото без подписи, файл: бот их не разбирает — разговор забирает менеджер.
        Само сообщение пересылаем: своего чата с клиентом у менеджера в MAX нет."""
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
        if body:
            await self.show_media(conn.owner_chat_id, body)
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
            if command == "/start" and manager is None:
                self.remember_source(chat_id, args.strip())
                await self.greet(chat_id)
            elif manager is not None:
                await self.send(chat_id, "Запрос ждёт разрешения администратора: админка → Обзор → Подключения.")
        elif command == "/mode":
            mode = args.strip().lower()
            mode = D.MODE_ALIASES.get(mode, mode)  # «/mode watch» — прежнее имя наблюдения
            if mode not in MAX_MODES:
                await self.send(chat_id, "Укажите режим: /mode observe, /mode draft или /mode auto")
                return
            services.db.set_mode(mode, connection_id=manager.id)
            services.event(f"MAX: менеджер включил режим «{mode_name(mode)}»")
            await self.send(chat_id, f"Режим: {mode_name(mode)}." + (OBSERVE_NOTE if mode == D.OBSERVE else ""))
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
        files = files_of(message.get("body") or {})
        if link.get("type") != "reply" or not (text or files):
            await self.send(chat_id, HELP)
            return
        # Номер карточки — последний: в тексте вопроса клиента тоже может встретиться «Вопрос #5»
        ids = _DRAFT_ID_RE.findall(((link.get("message") or {}).get("text")) or "")
        if not ids:
            await self.send(chat_id, HELP)
            return
        services = self.services
        dialog = services.db.get_dialog(int(ids[-1]))
        if dialog and dialog["status"] == D.WATCH and dialog["connection_id"] == manager.id:
            await self.reply_observed(manager, chat_id, dialog, text, files)
            return
        if not dialog or dialog["status"] not in (D.DRAFT_PENDING, D.ESCALATED):
            await self.send(chat_id, "Этот черновик уже обработан.")
            return
        if dialog["connection_id"] != manager.id:
            return
        if not services.db.claim_dialog(dialog["id"], D.DRAFT_EDITED, answer=text or "[файл]"):
            await self.send(chat_id, "Этот черновик уже обработан.")
            return
        if await self.send(dialog["chat_id"], text, files=files):
            if manager.mode == "auto":
                # Менеджер взял разговор на себя: бот не перебивает, следующие сообщения идут ему (снять: /resume)
                services.db.pause_chat(manager.id, dialog["chat_id"], services.settings.manager_pause_minutes)
            await self.send(chat_id, "Ваш вариант отправлен клиенту.")
            if text:
                await collect_quietly(services, "card", manager.id, dialog["chat_id"], dialog["id"], dialog["question"], text)
        else:
            services.db.set_dialog_status(dialog["id"], dialog["status"], answer=dialog["answer"])
            await self.send(chat_id, "Не отправлено: нет связи с MAX. Попробуйте ещё раз.")

    async def reply_observed(self, manager: D.Connection, chat_id: int, dialog, text: str,
                             files: list[dict] | None = None) -> None:
        """Наблюдение: ответ менеджера на пересланный вопрос уходит клиенту; бот замеряет, через сколько
        менеджер ответил, и собирает из ответа предложения в базу знаний. На одну карточку можно ответить
        несколькими сообщениями — они допишутся к ответу."""
        services, client_chat = self.services, dialog["chat_id"]
        sent = await self.send_message(client_chat, text, files=files)
        if sent is None:
            await self.send(chat_id, "Не отправлено: нет связи с MAX. Попробуйте ещё раз.")
            return
        now = (sent.get("timestamp") or time.time() * 1000) / 1000
        shown = text or "[файл]"
        _, new = services.db.observe_message(connection_id=manager.id, chat_id=client_chat,
                                             message_id=message_seq(sent), role="manager", text=shown, sent_at=now)
        services.log_watch_message(manager.id, client_chat, "manager", shown, now)
        latest = services.db.last_in_chat(manager.id, client_chat)
        waited = services.db.watch_reply(manager.id, client_chat, shown, now, dialog["id"])
        services.event("MAX, наблюдение: менеджер ответил"
                       + (f" через {wait_text(waited / 60)}" if waited is not None else ""))
        await self.send(chat_id, "Отправлено клиенту.")
        # Ответ на старую карточку после нового вопроса нельзя сопоставить по порядку сообщений
        if text and new and latest and latest["id"] == dialog["id"]:
            await collect_observed(services, manager.id, client_chat, message_seq(sent))

    async def on_callback(self, callback: dict, message: dict) -> None:
        payload, callback_id = callback.get("payload") or "", callback.get("callback_id") or ""
        user_id = (callback.get("user") or {}).get("user_id")
        action, _, rest = payload.partition(":")
        services = self.services
        if action == "cand":
            await self.on_candidate(callback_id, user_id, rest)
            return
        if action == "menu":
            await self.on_menu(callback_id, rest, callback.get("user") or {}, message)
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
                ok = await self.send(dialog["chat_id"], services.sign(dialog["answer"]), AFTER_ANSWER)
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


def message_seq(message: dict) -> int:
    """Номер сообщения в чате для журнала наблюдения: у MAX mid — строка, а seq растёт по порядку."""
    seq = (message.get("body") or {}).get("seq")
    return int(seq) if isinstance(seq, int) or str(seq or "").isdigit() else int(time.time() * 1000)


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
    ready = {"ok": True, "username": me.get("username") or "", "name": me.get("first_name") or ""}
    services.max_status = ready
    log.info("Бот MAX %s запущен", me.get("username") or me.get("first_name") or me.get("user_id"))
    summary = asyncio.create_task(summary_loop(bot))
    marker, delay = None, 5
    try:
        if services.settings.max_webhook_url:
            await run_webhook(api, bot, services, ready)
            return
        # Вебхук отключили в настройках: пока подписка жива, MAX не отдаёт события опросом
        with contextlib.suppress(MaxError, httpx.HTTPError):
            for hook in await api.subscriptions():
                await api.unsubscribe(hook.get("url"))
                log.info("MAX: старый вебхук снят, бот на долгом опросе")
        while True:
            try:
                data = await api.updates(marker)
                delay = 5
                services.max_status = ready
            except MaxError as exc:
                if exc.status == 401:
                    services.max_status = {"ok": False, "error": "MAX отозвал токен — проверьте MAX_BOT_TOKEN в .env"}
                    log.error(services.max_status["error"])
                    return
                services.max_status = {"ok": False, "error": f"MAX ответил ошибкой {exc.status} — пробую снова"}
                log.warning("MAX: ошибка опроса %s — повтор через %d с", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            except httpx.HTTPError as exc:
                services.max_status = {"ok": False, "error": f"нет связи с MAX ({type(exc).__name__}) — пробую снова"}
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


async def run_webhook(api: MaxApi, bot: MaxBot, services: Services, ready: dict) -> None:
    """Рабочий режим: MAX шлёт события на сервер (`POST /max/webhook` в админке кладёт их в очередь).
    Ответить MAX нужно сразу, поэтому разбор отдельно. Подписку проверяем и при потере восстанавливаем сами. При остановке
    не снимаем: MAX повторяет доставку, пока не получит 200, но не дольше 8 часов, так что короткий простой не страшен."""
    url, secret = services.settings.max_webhook_url, services.settings.max_webhook_secret
    if not url.startswith("https://") or not secret:
        services.max_status = {"ok": False, "error": "для вебхука нужны MAX_WEBHOOK_URL с https:// и MAX_WEBHOOK_SECRET"}
        log.error(services.max_status["error"])
        return
    services.max_updates = asyncio.Queue(maxsize=1000)
    worker = asyncio.create_task(handle_queue(bot, services.max_updates))
    try:
        while True:
            try:
                hooks = await api.subscriptions()
                for other in hooks:  # чужая подписка перехватывала бы события
                    if other.get("url") != url:
                        await api.unsubscribe(other.get("url"))
                if not any(h.get("url") == url for h in hooks):
                    await api.subscribe(url, secret, UPDATE_TYPES)
                    log.info("MAX: вебхук подключён")
                services.max_status = ready
            except MaxError as exc:
                if exc.status == 401:
                    services.max_status = {"ok": False, "error": "MAX отозвал токен — проверьте MAX_BOT_TOKEN в .env"}
                    log.error(services.max_status["error"])
                    return
                services.max_status = {"ok": False, "error": f"вебхук не подключён: MAX ответил ошибкой {exc.status}"}
                log.warning("MAX: %s", services.max_status["error"])
            except httpx.HTTPError as exc:
                services.max_status = {"ok": False, "error": f"нет связи с MAX ({type(exc).__name__}) — пробую снова"}
                log.warning("MAX: %s", services.max_status["error"])
            await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS if services.max_status["ok"] else 30)
    finally:
        services.max_updates = None
        worker.cancel()


async def handle_queue(bot: MaxBot, queue: asyncio.Queue) -> None:
    while True:
        update = await queue.get()
        try:
            await bot.handle(update)
        except Exception:  # noqa: BLE001 — одно сломанное событие не должно останавливать бота
            log.exception("MAX: не удалось обработать событие %s", update.get("update_type"))


async def summary_loop(bot: MaxBot) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await bot.maybe_send_summaries()
        except Exception:  # noqa: BLE001 — сводка не должна ронять бота
            log.exception("MAX: не удалось отправить вечернюю сводку")
