"""Входящие MAX: подтверждаем HTTP только после commit, удаляем текст после обработки.

После аварии pending-события читаются снова. Отправка в MAX и SQLite не образуют
общую транзакцию: авария между отправкой и commit может дать повторную отправку.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time


def event_key(update: dict) -> str:
    kind = update.get("update_type")
    message = update.get("message") or {}
    mid = (message.get("body") or {}).get("mid")
    callback = (update.get("callback") or {}).get("callback_id")
    identity = [kind, mid] if kind == "message_created" and mid else (
        [kind, callback] if kind == "message_callback" and callback else update)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MaxInbox:
    def __init__(self, conn: sqlite3.Connection, maxsize: int = 1000):
        self.conn, self.maxsize = conn, maxsize
        self.active: set[str] = set()
        self.retry_at: dict[str, float] = {}
        self.changed = asyncio.Event()
        self.accepting = True
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS max_delivery (
                request_key TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, next_part INTEGER NOT NULL,
                receipt TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS max_inbox (
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL, chat_id INTEGER,
                done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)""")

    def full(self) -> bool:
        return self.qsize() >= self.maxsize

    def qsize(self) -> int:
        return self.conn.execute("SELECT count(*) FROM max_inbox WHERE done=0").fetchone()[0]

    def empty(self) -> bool:
        return self.qsize() == len(self.active)

    def put_nowait(self, update: dict) -> None:
        key = event_key(update)
        if self.conn.execute("SELECT 1 FROM max_inbox WHERE event_key=?", (key,)).fetchone():
            return
        if not self.accepting or self.full():
            raise asyncio.QueueFull
        chat_id = ((update.get("message") or {}).get("recipient") or {}).get("chat_id") or update.get("chat_id")
        with self.conn:
            self.conn.execute("INSERT INTO max_inbox(event_key,payload,chat_id,created_at) VALUES(?,?,?,?)",
                              (key, json.dumps(update, ensure_ascii=False), chat_id, time.time()))
        self.changed.set()

    async def get(self) -> dict:
        while True:
            self.changed.clear()
            for row in self.conn.execute("SELECT event_key,payload FROM max_inbox WHERE done=0 ORDER BY rowid"):
                if row[0] not in self.active and self.retry_at.get(row[0], 0) <= time.monotonic():
                    self.active.add(row[0])
                    return json.loads(row[1])
            due = [t - time.monotonic() for t in self.retry_at.values() if t > time.monotonic()]
            if due:
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=min(due))
                except TimeoutError:
                    pass
            else:
                await self.changed.wait()

    def complete(self, update: dict) -> None:
        key = event_key(update)
        with self.conn:
            self.conn.execute("UPDATE max_inbox SET done=1,payload='' WHERE event_key=?", (key,))
            # MAX повторяет доставку до восьми часов; оставляем только хеши за последние двое суток.
            self.conn.execute("DELETE FROM max_inbox WHERE done=1 AND created_at<?", (time.time() - 172800,))
        self.active.discard(key)
        self.retry_at.pop(key, None)
        self.changed.set()

    def retry(self, update: dict) -> None:
        key = event_key(update)
        self.active.discard(key)
        self.retry_at[key] = time.monotonic() + 1
        self.changed.set()

    async def join(self) -> None:
        while self.qsize():
            self.changed.clear()
            await self.changed.wait()
