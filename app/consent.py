"""Согласие клиента на обработку контакта (152-ФЗ, ст. 9): отдельный шаг перед тем, как бот MAX попросит телефон.

Пока в тексте остались [квадратные скобки] из черновика `docs/MAX-CONSENT.md`, согласие не оформлено и бот
контакт не просит вовсе: заявка уходит менеджеру без телефона, он отвечает клиенту в том же чате. Текст правится
в админке («Клиенты в MAX»), версия — по содержимому: изменили текст — прежние согласия к новой версии не относятся.
Журнал: номер чата, версия, решение, время. Ни имени, ни телефона.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass

SETTINGS_KEY = "consent_text"
CHANNEL = "max"
YES, NO, REVOKED = "yes", "no", "revoked"

DEFAULT_SHORT = ("Чтобы передать заявку менеджеру, нам нужны ваш контакт и текст обращения.\n"
                 "Оператор: [полное наименование или ФИО, адрес].")
DEFAULT_FULL = (
    "Я разрешаю [оператор, реквизиты и адрес] обрабатывать данные, которые я передаю через бота [имя бота]: "
    "идентификатор пользователя и чата MAX, имя профиля, номер телефона или электронную почту (если я их отправил), "
    "содержание обращения и сведения о выбранном размещении.\n\n"
    "Цель — принять моё обращение, связаться со мной, подготовить ответ или предложение по размещению рекламы.\n\n"
    "Данные хранятся [срок] в [место хранения в РФ]. К обработке по поручению оператора привлекаются [обработчики].\n\n"
    "Согласие действует [срок]. Отозвать его можно, написав в этот чат «отзываю согласие», или по адресу [контакт оператора].\n\n"
    "Согласие не включает рекламные рассылки, публикацию переписки и использование моих данных в общей базе знаний.")
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,}\]")


@dataclass(frozen=True)
class ConsentText:
    short: str
    full: str

    @property
    def version(self) -> str:
        return hashlib.sha256(f"{self.short}\n\n{self.full}".encode()).hexdigest()[:8]

    @property
    def placeholders(self) -> list[str]:
        return _PLACEHOLDER_RE.findall(f"{self.short}\n{self.full}")

    @property
    def ready(self) -> bool:
        """Можно показывать клиентам: оба текста заполнены и плейсхолдеров черновика не осталось."""
        return bool(self.short.strip() and self.full.strip()) and not self.placeholders


class Consents:
    def __init__(self, conn):
        self.conn = conn
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS consent_log (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, chat_id INTEGER NOT NULL, version TEXT NOT NULL,
                decision TEXT NOT NULL, at REAL NOT NULL)""")

    def text(self) -> ConsentText:
        raw = self.conn.execute("SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
        data = json.loads(raw[0]) if raw else {}
        return ConsentText(data.get("short", DEFAULT_SHORT), data.get("full", DEFAULT_FULL))

    def save_text(self, short: str, full: str) -> ConsentText:
        text = ConsentText(short.strip().replace("\r\n", "\n"), full.strip().replace("\r\n", "\n"))
        with self.conn:
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (SETTINGS_KEY, json.dumps({"short": text.short, "full": text.full}, ensure_ascii=False)))
        return text

    def record(self, chat_id: int, decision: str, version: str, channel: str = CHANNEL) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO consent_log (channel, chat_id, version, decision, at) VALUES (?, ?, ?, ?, ?)",
                              (channel, chat_id, version, decision, time.time()))

    def given(self, chat_id: int, version: str, channel: str = CHANNEL) -> bool:
        """Последнее решение клиента — «согласен» именно на эту версию текста; отзыв перекрывает любую версию."""
        row = self.conn.execute("SELECT version, decision FROM consent_log WHERE channel=? AND chat_id=? "
                                "ORDER BY id DESC LIMIT 1", (channel, chat_id)).fetchone()
        return bool(row) and row[1] == YES and row[0] == version

    def counts(self, version: str, channel: str = CHANNEL) -> dict[str, int]:
        """Сколько клиентов сейчас: дали согласие на эту версию, отказались, отозвали — по последнему решению."""
        rows = self.conn.execute(
            """SELECT version, decision FROM consent_log WHERE id IN (
                   SELECT MAX(id) FROM consent_log WHERE channel=? GROUP BY chat_id)""", (channel,)).fetchall()
        return {YES: sum(r[1] == YES and r[0] == version for r in rows), NO: sum(r[1] == NO for r in rows),
                REVOKED: sum(r[1] == REVOKED for r in rows)}
