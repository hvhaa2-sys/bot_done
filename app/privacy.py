"""Доступ к наблюдению и удаление переписки клиента из рабочей базы."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


class ChatPrivacy:
    def __init__(self, conn):
        self.conn = conn
        conn.execute("PRAGMA secure_delete=ON")
        self.erased: set[int] = set()
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS observation_access (
                connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, allowed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(connection_id,chat_id))""")

    def allowed(self, connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute("SELECT allowed FROM observation_access WHERE connection_id=? AND chat_id=?",
                                (connection_id, chat_id)).fetchone()
        if row is None:
            with self.conn:
                self.conn.execute("INSERT INTO observation_access(connection_id,chat_id) VALUES(?,?)",
                                  (connection_id, chat_id))
        return bool(row and row[0]) and chat_id not in self.erased

    def set_allowed(self, connection_id: str, chat_id: int, allowed: bool) -> None:
        with self.conn:
            self.conn.execute("""INSERT INTO observation_access(connection_id,chat_id,allowed) VALUES(?,?,?)
                ON CONFLICT(connection_id,chat_id) DO UPDATE SET allowed=excluded.allowed""",
                              (connection_id, chat_id, int(allowed)))
        if allowed:
            self.erased.discard(chat_id)

    def rows(self):
        return self.conn.execute("SELECT * FROM observation_access ORDER BY connection_id,chat_id").fetchall()

    def erase(self, chat_id: int, watch_log: Path) -> int:
        """Удаление по chat_id во всех каналах. Бэкапы и утверждённые общие знания здесь не меняются."""
        content = watch_log.read_text(encoding="utf-8") if watch_log.exists() else None
        kept = []
        for line in (content or "").splitlines(keepends=True):
            if line.strip() and str(json.loads(line).get("chat")) != str(chat_id):
                kept.append(line)
        dialog_ids = {str(r[0]) for r in self.conn.execute("SELECT id FROM dialogs WHERE chat_id=?", (chat_id,))}
        linked_events = []
        for key, raw in self.conn.execute("SELECT event_key,payload FROM max_inbox WHERE payload != ''"):
            update = json.loads(raw)
            message = update.get("message") or {}
            linked = (message.get("link") or {}).get("message") or {}
            card = linked.get("text") or (linked.get("body") or {}).get("text") or ""
            match = re.search(r"(?:^|\n)(?:Черновик|Вопрос) #(\d+)\. [^\n]+\s*$", card)
            payload = (update.get("callback") or {}).get("payload") or ""
            action, _, target = payload.partition(":")
            if (match and match[1] in dialog_ids) or (action in {"send", "skip", "flag"} and target in dialog_ids):
                linked_events.append(key)
        temporary = None
        if content is not None:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=watch_log.parent, delete=False) as f:
                f.write("".join(kept))
                f.flush()
                os.fsync(f.fileno())
                temporary = Path(f.name)
        count = 0
        try:
            with self.conn:
                for key in linked_events:
                    count += self.conn.execute("DELETE FROM max_inbox WHERE event_key=?", (key,)).rowcount
                for sql in (
                    "DELETE FROM candidates WHERE chat_id=?",
                    "DELETE FROM observed_messages WHERE chat_id=?",
                    "DELETE FROM dialogs WHERE chat_id=?",
                    "DELETE FROM pauses WHERE chat_id=?",
                    "DELETE FROM client_sources WHERE chat_id=?",
                    "DELETE FROM max_inbox WHERE chat_id=?",
                    "DELETE FROM max_delivery WHERE chat_id=?",
                    "DELETE FROM observation_access WHERE chat_id=?",
                ):
                    count += self.conn.execute(sql, (chat_id,)).rowcount
                if temporary:
                    os.replace(temporary, watch_log)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        self.erased.add(chat_id)
        return count
