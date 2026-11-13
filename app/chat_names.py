"""Имена собеседников для админки — в базе, чтобы после перезапуска в «Диалогах» было «Катя Крюкова»,
а не «Чат 695343992» (просьба пользователя 13.11). Только имя профиля из мессенджера, без телефона.

Имя из ещё не разрешённого чата наблюдения — только в памяти (`remember`): по нему владелец решает, рабочий ли
это чат, а личные контакты в базу не попадают. «Удалить данные клиента» стирает и имя (`pop`).
"""
from __future__ import annotations

import time


class ChatNames(dict):
    """Словарь «номер чата → имя», как раньше, но присвоение сохраняет имя в таблицу `chat_names`."""

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_names (
                chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL, updated_at REAL NOT NULL)""")
        self._saved = {row[0]: row[1] for row in conn.execute("SELECT chat_id, name FROM chat_names")}
        super().update(self._saved)

    def __setitem__(self, chat_id: int, name: str) -> None:
        super().__setitem__(chat_id, name)
        if name and self._saved.get(chat_id) != name:  # пишем, только если имя новое или сменилось
            with self.conn:
                self.conn.execute("""INSERT INTO chat_names (chat_id, name, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
                                  (chat_id, name, time.time()))
            self._saved[chat_id] = name

    def remember(self, chat_id: int, name: str) -> None:
        """Только в памяти: чат ещё не разрешён для наблюдения."""
        if chat_id not in self._saved:
            super().__setitem__(chat_id, name)

    def pop(self, chat_id: int, default=None):
        if chat_id in self._saved:
            with self.conn:
                self.conn.execute("DELETE FROM chat_names WHERE chat_id=?", (chat_id,))
            del self._saved[chat_id]
        return super().pop(chat_id, default)
