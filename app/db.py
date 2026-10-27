"""Хранилище на SQLite: подключения, журнал диалогов, паузы.

Персональные данные минимизированы: храним только chat_id и текст вопроса/ответа,
без имён и телефонов клиентов.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .core.pii import mask_pd
from .core.facts import contains

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    owner_chat_id INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'draft',
    enabled INTEGER NOT NULL DEFAULT 1,
    can_reply INTEGER NOT NULL DEFAULT 1,
    approved INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dialogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT,
    chat_id INTEGER,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    confidence REAL,
    status TEXT NOT NULL,
    sources TEXT,
    latency_ms INTEGER,
    mode TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT,
    chat_id INTEGER,
    dialog_id INTEGER,
    source TEXT NOT NULL,
    client_question TEXT NOT NULL,
    manager_text TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS kb_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    activated_at REAL
);
CREATE TABLE IF NOT EXISTS pauses (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    until_ts REAL NOT NULL,
    PRIMARY KEY (connection_id, chat_id)
);
CREATE TABLE IF NOT EXISTS observed_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at REAL NOT NULL,
    reply_to_message_id INTEGER,
    UNIQUE (connection_id, chat_id, message_id)
);
"""

# Статусы диалога
SENT_AUTO = "sent_auto"          # бот ответил клиенту сам
DRAFT_PENDING = "draft_pending"  # черновик ждёт решения менеджера
DRAFT_SENT = "draft_sent"        # менеджер одобрил черновик
DRAFT_EDITED = "draft_edited"    # менеджер отправил свою правку черновика
DRAFT_REJECTED = "draft_rejected"
ESCALATED = "escalated"          # передано менеджеру, клиенту ушло «уточню»
SANDBOX = "sandbox"
# Режимы подключения: off — бот молчит и ничего не сохраняет; observe — молчит, сохраняет переписку, предлагает
# факты в базу и замеряет, как быстро отвечает менеджер; draft — черновики менеджеру; auto — отвечает клиенту сам.
# В off и observe клиенту и менеджеру бот не пишет. «watch» — прежнее имя наблюдения (ветка 27.10), принимаем как синоним
OBSERVE = "observe"
MODES = ("off", "observe", "draft", "auto")
MODE_ALIASES = {"watch": OBSERVE}
SILENT_MODES = frozenset({"off", OBSERVE})
OBSERVE_PAIR_HOURS = 24      # наблюдение: ответ менеджера на вопрос без явного «ответить» — в пределах суток
OBSERVE_PIECE_MINUTES = 30   # куски одного вопроса или ответа — не дальше получаса друг от друга
WATCH = "watch"              # статус журнала: обращение клиента в наблюдении и ответ менеджера (время ответа)
HISTORY_MINUTES = 180  # дольше трёх часов — уже другой разговор              # вопрос из песочницы админки


@dataclass
class Connection:
    id: str
    owner_user_id: int
    owner_chat_id: int
    mode: str
    enabled: bool
    can_reply: bool
    approved: bool = True  # чужой аккаунт, подключивший бота, ждёт разрешения в админке


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Миграция старых баз: отметка менеджера «бот ответил неверно»
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(dialogs)")}
        if "flagged" not in columns:
            self.conn.execute("ALTER TABLE dialogs ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0")
        # «Чему научить бота»: менеджер добавил ответ в базу или скрыл вопрос
        if "learned" not in columns:
            self.conn.execute("ALTER TABLE dialogs ADD COLUMN learned INTEGER NOT NULL DEFAULT 0")
        # Отметка «Ошибка» — сигнал к разбору; в экзамен попадает только подтверждённая разметка.
        review_columns = {"error_type": "TEXT NOT NULL DEFAULT ''", "expected_decision": "TEXT NOT NULL DEFAULT ''",
                          "corrected_answer": "TEXT NOT NULL DEFAULT ''", "must_contain": "TEXT NOT NULL DEFAULT ''",
                          "reviewed": "INTEGER NOT NULL DEFAULT 0"}
        for name, definition in review_columns.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE dialogs ADD COLUMN {name} {definition}")
        # По какой версии базы знаний бот отвечал: снимок версии хранится, источники ответа можно открыть
        if "kb_version" not in columns:
            self.conn.execute("ALTER TABLE dialogs ADD COLUMN kb_version INTEGER")
        # Наблюдение: через сколько секунд менеджер ответил клиенту сам (до бота)
        if "reply_after_s" not in columns:
            self.conn.execute("ALTER TABLE dialogs ADD COLUMN reply_after_s REAL")
        # Одобрение подключений: в старых базах подключения создавал сам владелец — считаем одобренными
        conn_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(connections)")}
        if "approved" not in conn_columns:
            self.conn.execute("ALTER TABLE connections ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
        # Наблюдение в ветке 27.10 называлось «watch»: переводим на общее имя, чтобы режим не пропал
        for alias, mode in MODE_ALIASES.items():
            self.conn.execute("UPDATE connections SET mode=? WHERE mode=?", (mode, alias))
            self.conn.execute("UPDATE settings SET value=? WHERE key='default_mode' AND value=?", (mode, alias))
        candidate_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(candidates)")}
        if "observation_id" not in candidate_columns:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN observation_id INTEGER")
        if "source_message_ids" not in candidate_columns:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN source_message_ids TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    # --- подключения Telegram Business ---
    def upsert_connection(self, id: str, owner_user_id: int, owner_chat_id: int,
                          enabled: bool, can_reply: bool, default_mode: str) -> Connection:
        """Новое подключение одобряется сразу, только если это первый менеджер или уже знакомый
        владелец. Остальные ждут кнопки «Разрешить» в админке:
        иначе любой, кто знает имя бота, подключил бы его к себе и тратил нашу модель."""
        approved_owners = {r[0] for r in self.conn.execute(
            "SELECT owner_user_id FROM connections WHERE approved=1")}
        approve = not approved_owners or owner_user_id in approved_owners
        self.conn.execute(
            """INSERT INTO connections (id, owner_user_id, owner_chat_id, mode, enabled, can_reply, approved, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                 owner_chat_id=excluded.owner_chat_id, enabled=excluded.enabled,
                 can_reply=excluded.can_reply, updated_at=excluded.updated_at""",
            (id, owner_user_id, owner_chat_id, default_mode, int(enabled), int(can_reply), int(approve), time.time()),
        )
        self.conn.commit()
        return self.get_connection(id)  # type: ignore[return-value]

    def get_connection(self, id: str) -> Connection | None:
        row = self.conn.execute("SELECT * FROM connections WHERE id=?", (id,)).fetchone()
        return self._conn(row) if row else None

    def connections(self) -> list[Connection]:
        rows = self.conn.execute("SELECT * FROM connections ORDER BY updated_at DESC").fetchall()
        return [self._conn(r) for r in rows]

    def set_mode(self, mode: str, *, connection_id: str | None = None,
                 owner_user_id: int | None = None) -> int:
        mode = MODE_ALIASES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if connection_id:
            cur = self.conn.execute("UPDATE connections SET mode=? WHERE id=?", (mode, connection_id))
        else:
            cur = self.conn.execute("UPDATE connections SET mode=? WHERE owner_user_id=? AND approved=1",
                                    (mode, owner_user_id))
        self.conn.commit()
        return cur.rowcount

    def approve_connection(self, id: str) -> None:
        self.conn.execute("UPDATE connections SET approved=1 WHERE id=?", (id,))
        self.conn.commit()

    def delete_connection(self, id: str) -> None:
        self.conn.execute("DELETE FROM connections WHERE id=?", (id,))
        self.conn.commit()

    def is_approved_owner(self, owner_user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM connections WHERE owner_user_id=? AND approved=1",
                                 (owner_user_id,)).fetchone() is not None

    @staticmethod
    def _conn(row: sqlite3.Row) -> Connection:
        return Connection(row["id"], row["owner_user_id"], row["owner_chat_id"], row["mode"],
                          bool(row["enabled"]), bool(row["can_reply"]), bool(row["approved"]))

    # --- расход модели и частота сообщений ---
    def add_usage(self, day: str, tokens: int) -> None:
        self.conn.execute(
            """INSERT INTO usage (day, calls, tokens) VALUES (?, 1, ?)
               ON CONFLICT(day) DO UPDATE SET calls=calls+1, tokens=tokens+excluded.tokens""", (day, tokens))
        self.conn.commit()

    def usage(self, day: str) -> tuple[int, int]:
        row = self.conn.execute("SELECT calls, tokens FROM usage WHERE day=?", (day,)).fetchone()
        return (row["calls"], row["tokens"]) if row else (0, 0)

    def count_recent(self, connection_id: str, chat_id: int, seconds: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM dialogs WHERE connection_id=? AND chat_id=? AND created_at>=?",
            (connection_id, chat_id, time.time() - seconds)).fetchone()[0]

    def holding_sent_recently(self, connection_id: str, chat_id: int, minutes: int) -> bool:
        """Клиенту уже писали «Уточню и вернусь» — второй раз подряд не повторяем."""
        return self.conn.execute(
            "SELECT 1 FROM dialogs WHERE connection_id=? AND chat_id=? AND status=? AND created_at>=? LIMIT 1",
            (connection_id, chat_id, ESCALATED, time.time() - minutes * 60)).fetchone() is not None

    # --- журнал диалогов ---
    def log_dialog(self, *, connection_id: str | None, chat_id: int | None, question: str,
                   answer: str, decision: str, reason: str, confidence: float, status: str,
                   sources: list[str], latency_ms: int, mode: str, kb_version: int | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO dialogs (connection_id, chat_id, question, answer, decision, reason,
                 confidence, status, sources, latency_ms, mode, created_at, kb_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (connection_id, chat_id, mask_pd(question), answer, decision, reason, confidence, status,
             json.dumps(sources, ensure_ascii=False), latency_ms, mode, time.time(), kb_version),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # --- наблюдение: бот молчит, записываем вопросы и как быстро ответил менеджер ---
    def watch_question(self, connection_id: str, chat_id: int, text: str, ts: float,
                       kb_version: int | None = None) -> int:
        """Сообщение клиента в режиме наблюдения. Пока менеджер не ответил, следующие сообщения клиента —
        то же обращение: дописываем к нему, ожидание считается от первого (как в импорте переписки)."""
        last = self._last_in_chat(connection_id, chat_id)
        if last and last["status"] == WATCH and not last["answer"]:
            self.conn.execute("UPDATE dialogs SET question=question || char(10) || ? WHERE id=?",
                              (mask_pd(text), last["id"]))
            self.conn.commit()
            return int(last["id"])
        cur = self.conn.execute(
            """INSERT INTO dialogs (connection_id, chat_id, question, answer, decision, reason, confidence, status,
                 sources, mode, created_at, kb_version) VALUES (?, ?, ?, '', 'watch', 'watch', 0, ?, '[]', 'watch', ?, ?)""",
            (connection_id, chat_id, mask_pd(text), WATCH, ts, kb_version))
        self.conn.commit()
        return int(cur.lastrowid)

    def watch_reply(self, connection_id: str, chat_id: int, text: str, ts: float) -> float | None:
        """Менеджер написал клиенту сам. Первое сообщение закрывает обращение и фиксирует время ответа,
        следующие (пока клиент молчит) дописываются к ответу. Возвращает секунды ожидания для первого."""
        last = self._last_in_chat(connection_id, chat_id)
        if not last or last["status"] != WATCH:
            return None  # менеджер пишет первым или обращение из другого режима — ожидания нет
        if last["answer"]:
            self.conn.execute("UPDATE dialogs SET answer=answer || char(10) || ? WHERE id=?",
                              (mask_pd(text), last["id"]))
            self.conn.commit()
            return None
        waited = max(0.0, ts - last["created_at"])
        self.conn.execute("UPDATE dialogs SET answer=?, reply_after_s=? WHERE id=?", (mask_pd(text), waited, last["id"]))
        self.conn.commit()
        return waited

    def watch_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT created_at, reply_after_s FROM dialogs WHERE status=? ORDER BY id", (WATCH,)).fetchall()

    def _last_in_chat(self, connection_id: str, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM dialogs WHERE connection_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
                                 (connection_id, chat_id)).fetchone()

    def get_dialog(self, dialog_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM dialogs WHERE id=?", (dialog_id,)).fetchone()

    def set_dialog_status(self, dialog_id: int, status: str, answer: str | None = None) -> None:
        if answer is None:
            self.conn.execute("UPDATE dialogs SET status=? WHERE id=?", (status, dialog_id))
        else:
            self.conn.execute("UPDATE dialogs SET status=?, answer=? WHERE id=?", (status, answer, dialog_id))
        self.conn.commit()

    def claim_dialog(self, dialog_id: int, status: str, answer: str | None = None) -> bool:
        """Закрыть черновик, если он ещё ждёт решения. Два быстрых нажатия «Отправить»
        не отправят клиенту ответ дважды: второе нажатие ничего не найдёт."""
        cur = self.conn.execute(
            "UPDATE dialogs SET status=?, answer=COALESCE(?, answer) WHERE id=? AND status IN (?, ?)",
            (status, answer, dialog_id, DRAFT_PENDING, ESCALATED))
        self.conn.commit()
        return cur.rowcount == 1

    def recent_dialogs(self, limit: int = 100, *, statuses: tuple[str, ...] = (),
                       flagged: bool = False, reviewed: bool = False, query: str = "") -> list[sqlite3.Row]:
        where, args = [], []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += statuses
        if flagged:
            where.append("flagged = 1")
        if reviewed:
            where.append("reviewed = 1")
        if query:
            where.append("(question LIKE ? OR answer LIKE ?)")
            args += [f"%{query}%", f"%{query}%"]
        # Bandit B608 — ложное срабатывание: в строку попадают только заготовленные условия и «?», значения идут параметрами
        sql = "SELECT * FROM dialogs" + (" WHERE " + " AND ".join(where) if where else "")  # nosec — см. комментарий выше
        return self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    def to_learn(self, limit: int = 500) -> list[sqlite3.Row]:
        """Вопросы, на которые у бота не нашлось ответа или он ответил неверно (отметка «Ошибка»).
        Стоп-темы (скидки, договор) и голосовые сюда не попадают: это работа менеджера, не пробел в базе.
        Сбои сети и модели — тоже: база тут ни при чём."""
        return self.conn.execute(
            """SELECT id, question, reason, created_at, flagged FROM dialogs
               WHERE learned = 0 AND (flagged = 1 OR (decision = 'escalate'
                     AND reason NOT LIKE 'stop_topic%' AND reason NOT LIKE 'llm_error%'
                     AND reason NOT IN ('media', 'greeting', 'small_talk')))
               ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()

    def mark_learned(self, ids: list[int]) -> None:
        self.conn.executemany("UPDATE dialogs SET learned=1 WHERE id=?", [(i,) for i in ids])
        self.conn.commit()

    def dialogs_since(self, connection_id: str, since_ts: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM dialogs WHERE connection_id=? AND created_at>=? ORDER BY id",
            (connection_id, since_ts)).fetchall()

    def flag_dialog(self, dialog_id: int, flagged: bool) -> None:
        if flagged:
            self.conn.execute("UPDATE dialogs SET flagged=1 WHERE id=?", (dialog_id,))
        else:
            self.conn.execute("""UPDATE dialogs SET flagged=0, reviewed=0, error_type='', expected_decision='',
                                 corrected_answer='', must_contain='' WHERE id=?""", (dialog_id,))
        self.conn.commit()

    def review_dialog(self, dialog_id: int, *, error_type: str, expected_decision: str,
                      corrected_answer: str = "", must_contain: str = "") -> bool:
        """Подтверждение менеджером: сохраняем правильное решение и проверяемый эталон."""
        dialog = self.get_dialog(dialog_id)
        if not dialog or not dialog["flagged"]:
            return False
        if error_type not in ("facts", "decision") or expected_decision not in ("answer", "escalate"):
            raise ValueError("Выберите тип ошибки и правильное решение.")
        if error_type == "facts" and expected_decision != "answer":
            raise ValueError("Для ошибки в фактах нужен исправленный ответ. Если нужен менеджер, выберите ошибку в решении.")
        if error_type == "decision" and expected_decision == dialog["decision"]:
            raise ValueError("При ошибке в решении выберите другое действие. Для неверной цены выберите ошибку в фактах.")
        if len(corrected_answer) > 4096 or len(must_contain) > 1000:
            raise ValueError("Слишком длинная правка: ответ — до 4096 символов, проверяемые факты — до 1000.")
        corrected_answer, must_contain = mask_pd(corrected_answer.strip()), mask_pd(must_contain.strip())
        if expected_decision == "answer":
            if not corrected_answer:
                raise ValueError("Напишите правильный ответ.")
            items = [item.strip() for item in must_contain.split(";") if item.strip()]
            if not any(not item.startswith("!") and item.replace("|", "").strip() for item in items):
                raise ValueError("Укажите хотя бы один факт, который должен быть в ответе: например, цену или формат.")
            for item in items:
                if not item.lstrip("!").replace("|", "").strip() or not contains(corrected_answer, item):
                    raise ValueError(f"Исправленный ответ не проходит проверку «{item}». Проверьте текст и факты.")
            must_contain = ";".join(items)
        else:
            corrected_answer, must_contain = "", ""
        cur = self.conn.execute("""UPDATE dialogs SET reviewed=1, error_type=?, expected_decision=?,
                                   corrected_answer=?, must_contain=? WHERE id=? AND flagged=1""",
                                (error_type, expected_decision, corrected_answer, must_contain, dialog_id))
        self.conn.commit()
        return cur.rowcount == 1

    # --- настройки из админки (поверх .env) ---
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, str]) -> None:
        self.conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(values.items()))
        self.conn.commit()

    def delete_settings(self, keys: list[str]) -> None:
        self.conn.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in keys])
        self.conn.commit()

    def chat_history(self, connection_id: str, chat_id: int, limit: int = 3,
                     minutes: int = HISTORY_MINUTES) -> list[tuple[str, str]]:
        """Последние пары «вопрос — ответ» в чате, которые реально ушли клиенту, за последние часы:
        разговор месячной давности к новому вопросу не подмешиваем."""
        rows = self.conn.execute(
            """SELECT question, answer FROM dialogs
               WHERE connection_id=? AND chat_id=? AND status IN (?, ?, ?) AND created_at>=?
               ORDER BY id DESC LIMIT ?""",
            (connection_id, chat_id, SENT_AUTO, DRAFT_SENT, DRAFT_EDITED, time.time() - minutes * 60, limit),
        ).fetchall()
        return [(r["question"], r["answer"]) for r in reversed(rows)]

    def last_question(self, connection_id: str, chat_id: int, minutes: int = HISTORY_MINUTES) -> str:
        """Прошлое сообщение клиента в любом статусе — для продолжений вроде «а в депо?» после эскалации."""
        row = self.conn.execute(
            """SELECT question FROM dialogs WHERE connection_id=? AND chat_id=? AND created_at>=?
               ORDER BY id DESC LIMIT 1""", (connection_id, chat_id, time.time() - minutes * 60)).fetchone()
        return row["question"] if row else ""

    def conversation_turns(self, connection_id: str, chat_id: int, limit: int = 60,
                           minutes: int = HISTORY_MINUTES) -> list[sqlite3.Row]:
        """Слова клиента для памяти выбора; отдельные чаты и только свежий разговор."""
        rows = self.conn.execute(
            """SELECT question, answer, decision, reason, status FROM dialogs
               WHERE connection_id=? AND chat_id=? AND created_at>=? ORDER BY id DESC LIMIT ?""",
            (connection_id, chat_id, time.time() - minutes * 60, limit)).fetchall()
        return list(reversed(rows))

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n, AVG(latency_ms) AS lat FROM dialogs GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        # Наблюдение — вопросы, на которые бот и не должен был отвечать: в доли ответов бота не входят
        real = total - by_status.get(SANDBOX, 0) - by_status.get(WATCH, 0)
        drafts_decided = sum(by_status.get(s, 0) for s in (DRAFT_SENT, DRAFT_EDITED, DRAFT_REJECTED))
        lat = self.conn.execute("SELECT AVG(latency_ms) FROM dialogs").fetchone()[0]
        flagged = self.conn.execute(
            "SELECT COUNT(*) FROM dialogs WHERE flagged=1 AND status != ?", (SANDBOX,)).fetchone()[0]
        escalated = self.conn.execute(
            "SELECT COUNT(*) FROM dialogs WHERE decision='escalate' AND status != ?", (SANDBOX,)).fetchone()[0]
        return {
            "total": total,
            "real": real,
            "by_status": by_status,
            "auto_share": (by_status.get(SENT_AUTO, 0) / real) if real else 0.0,
            "draft_accept_rate": (by_status.get(DRAFT_SENT, 0) / drafts_decided) if drafts_decided else None,
            "avg_latency_ms": int(lat or 0),
            # В режиме черновиков вопросы без ответа лежат как черновики — считаем по решению бота
            "escalated_share": (escalated / real) if real else 0.0,
            "flagged": flagged,
        }

    # --- база из переписки: кандидаты в факты ---
    def add_candidate(self, *, connection_id: str | None, chat_id: int | None, dialog_id: int | None, source: str,
                      client_question: str, manager_text: str, question: str, answer: str, note: str = "",
                      observation_id: int | None = None, source_message_ids: tuple[int, ...] = ()) -> int:
        cur = self.conn.execute(
            """INSERT INTO candidates (connection_id, chat_id, dialog_id, source, client_question, manager_text,
               question, answer, note, created_at, observation_id, source_message_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (connection_id, chat_id, dialog_id, source, mask_pd(client_question), mask_pd(manager_text),
             mask_pd(question), mask_pd(answer), note, time.time(), observation_id, json.dumps(source_message_ids)))
        self.conn.commit()
        return int(cur.lastrowid)

    # --- наблюдение: отдельно от ответов бота и их метрик ---
    def observe_message(self, *, connection_id: str, chat_id: int, message_id: int, role: str,
                        text: str, sent_at: float, reply_to_message_id: int | None = None) -> tuple[int, bool]:
        if role not in ("client", "manager"):
            raise ValueError("Unknown observation role")
        text = mask_pd(text)
        old = self.conn.execute("SELECT * FROM observed_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                                (connection_id, chat_id, message_id)).fetchone()
        if old and (old["text"], old["role"], old["reply_to_message_id"]) == (text, role, reply_to_message_id):
            return old["id"], False
        if old:
            self.invalidate_observed_candidates([old["id"]])
        self.conn.execute(
            """INSERT INTO observed_messages (connection_id, chat_id, message_id, role, text, sent_at, reply_to_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(connection_id, chat_id, message_id)
               DO UPDATE SET text=excluded.text, role=excluded.role, reply_to_message_id=excluded.reply_to_message_id""",
            (connection_id, chat_id, message_id, role, text, sent_at, reply_to_message_id))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM observed_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                                (connection_id, chat_id, message_id)).fetchone()
        return row["id"], True

    def observed_chats(self) -> list[sqlite3.Row]:
        """Чаты наблюдения: сколько сообщений и когда последнее — свежие сверху."""
        return self.conn.execute(
            """SELECT connection_id, chat_id, COUNT(*) AS n, MAX(sent_at) AS last_at FROM observed_messages
               GROUP BY connection_id, chat_id ORDER BY last_at DESC""").fetchall()

    def observed_chat(self, connection_id: str, chat_id: int, limit: int = 300) -> list[sqlite3.Row]:
        """Последние сообщения одного чата по порядку — как переписка в мессенджере."""
        rows = self.conn.execute(
            """SELECT * FROM observed_messages WHERE connection_id=? AND chat_id=?
               ORDER BY sent_at DESC, message_id DESC LIMIT ?""", (connection_id, chat_id, limit)).fetchall()
        return rows[::-1]

    def observations(self, limit: int = 300, *, query: str = "") -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM observed_messages WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                                 (f"%{query}%", limit)).fetchall()

    def observation_context(self, connection_id: str, chat_id: int, message_id: int):
        """Последний блок вопросов и ответ менеджера, включая сообщения, отправленные кусками."""
        rows = self.conn.execute(
            """SELECT * FROM observed_messages WHERE connection_id=? AND chat_id=? AND message_id<=?
               ORDER BY message_id DESC LIMIT 100""", (connection_id, chat_id, message_id)).fetchall()
        if not rows or rows[0]["role"] != "manager":
            return None
        manager, client = [], []
        i = 0
        target = rows[0]["reply_to_message_id"]
        while i < len(rows) and rows[i]["role"] == "manager":
            reply_to = rows[i]["reply_to_message_id"]
            if target and reply_to and reply_to != target:
                break
            if manager and manager[-1]["sent_at"] - rows[i]["sent_at"] > OBSERVE_PIECE_MINUTES * 60:
                break  # вчерашний ответ — не кусок сегодняшнего
            target = target or reply_to
            manager.append(rows[i])
            i += 1
        if target:
            client = [r for r in rows if r["message_id"] == target and r["role"] == "client"]
        else:
            while i < len(rows) and rows[i]["role"] == "client":
                if client and client[-1]["sent_at"] - rows[i]["sent_at"] > OBSERVE_PIECE_MINUTES * 60:
                    break  # «Здравствуйте» вчера и вопрос сегодня — разные блоки
                client.append(rows[i])
                i += 1
        # Явный ответ на сообщение связываем при любой задержке; без него — ответ в пределах суток:
        # живой менеджер отвечает и через полдня, и утром — такие пары самые ценные для базы
        if not client or (not target and manager[-1]["sent_at"] - client[0]["sent_at"] > OBSERVE_PAIR_HOURS * 3600):
            return None
        sources = tuple(r["id"] for r in reversed(client + manager))
        question = "\n".join(r["text"] for r in reversed(client))
        answer = "\n".join(r["text"] for r in reversed(manager))
        return manager[-1]["id"], question[-6000:], answer[-6000:], sources

    def invalidate_observed_candidates(self, message_ids: list[int]) -> None:
        changed = set(message_ids)
        rows = self.conn.execute("SELECT id, source_message_ids FROM candidates WHERE source='observe' AND status='new'")
        ids = [r["id"] for r in rows if changed.intersection(json.loads(r["source_message_ids"]))]
        self.conn.executemany("UPDATE candidates SET status='superseded', decided_at=? WHERE id=?",
                              [(time.time(), i) for i in ids])
        self.conn.commit()

    def delete_observations(self, connection_id: str, chat_id: int, message_ids: list[int]) -> None:
        for message_id in message_ids:
            row = self.conn.execute(
                "SELECT id FROM observed_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                (connection_id, chat_id, message_id)).fetchone()
            if row:
                self.invalidate_observed_candidates([row["id"]])
                self.conn.execute("DELETE FROM observed_messages WHERE id=?", (row["id"],))
        self.conn.commit()

    def observation_stats(self) -> dict:
        row = self.conn.execute("SELECT COUNT(*) AS messages, COUNT(DISTINCT connection_id || ':' || chat_id) AS chats, "
                                "MAX(sent_at) AS last_at FROM observed_messages").fetchone()
        n = self.conn.execute("SELECT COUNT(*) FROM candidates WHERE source='observe' AND status='new'").fetchone()[0]
        return {**dict(row), "candidates": n}

    def candidates(self, status: str = "new", limit: int = 300) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM candidates WHERE status=? ORDER BY id DESC LIMIT ?",
                                 (status, limit)).fetchall()

    def get_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()

    def decide_candidates(self, ids: list[int], status: str, question: str | None = None,
                          answer: str | None = None) -> int:
        """Утвердить или отклонить. Только новые: второе нажатие кнопки ничего не меняет."""
        marks = ",".join("?" * len(ids))
        if not ids:
            return 0
        if question is not None and answer is not None:
            self.conn.execute(f"UPDATE candidates SET question=?, answer=? WHERE id IN ({marks}) AND status='new'",  # nosec — только «?»
                              (question, answer, *ids))
        cur = self.conn.execute(
            f"UPDATE candidates SET status=?, decided_at=? WHERE id IN ({marks}) AND status='new'",  # nosec — только «?», см. выше
            (status, time.time(), *ids))
        self.conn.commit()
        return cur.rowcount

    def supersede_candidates(self, question: str) -> int:
        """Утвердили новый ответ на тот же вопрос — прежний утверждённый ответ больше не действует.
        Сравниваем в Python: lower() в SQLite не знает кириллицу."""
        def key(text: str) -> str:
            return " ".join(text.casefold().replace("ё", "е").strip(" ?").split())

        ids = [r["id"] for r in self.conn.execute("SELECT id, question FROM candidates WHERE status='approved'")
               if key(r["question"]) == key(question)]
        self.conn.executemany("UPDATE candidates SET status='superseded', decided_at=? WHERE id=?",
                              [(time.time(), i) for i in ids])
        self.conn.commit()
        return len(ids)

    # --- версии базы знаний ---
    def add_kb_version(self, source: str, fingerprint: str, report: str = "{}") -> int:
        cur = self.conn.execute("INSERT INTO kb_versions (source, fingerprint, report, created_at) VALUES (?, ?, ?, ?)",
                                (source, fingerprint, report, time.time()))
        self.conn.commit()
        return int(cur.lastrowid)

    def activate_kb_version(self, version_id: int) -> None:
        self.conn.execute("UPDATE kb_versions SET activated_at=? WHERE id=?", (time.time(), version_id))
        self.conn.commit()

    def active_kb_version(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM kb_versions WHERE activated_at IS NOT NULL "
                                 "ORDER BY activated_at DESC, id DESC LIMIT 1").fetchone()

    def get_kb_version(self, version_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM kb_versions WHERE id=?", (version_id,)).fetchone()

    def kb_versions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM kb_versions WHERE activated_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                                 (limit,)).fetchall()

    def growth(self, weeks: int = 8) -> list[dict]:
        """По неделям: сколько вопросов клиентов, сколько бот закрыл сам и сколько фактов утверждено —
        видно, как растёт база и как бот всё меньше зовёт менеджера."""
        week = 7 * 24 * 3600
        start = time.time() - weeks * week
        out = []
        for i in range(weeks):
            lo, hi = start + i * week, start + (i + 1) * week
            row = self.conn.execute(
                """SELECT COUNT(*) AS n, SUM(decision='answer') AS answered FROM dialogs
                   WHERE created_at>=? AND created_at<? AND status != ?""", (lo, hi, SANDBOX)).fetchone()
            facts = self.conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE status='approved' AND decided_at>=? AND decided_at<?",
                (lo, hi)).fetchone()[0]
            if row["n"] or facts:
                out.append({"start": lo, "questions": row["n"], "answered": row["answered"] or 0, "facts": facts})
        return out

    # --- пауза, когда менеджер пишет сам ---
    def pause_chat(self, connection_id: str, chat_id: int, minutes: float) -> None:
        self.conn.execute(
            """INSERT INTO pauses (connection_id, chat_id, until_ts) VALUES (?, ?, ?)
               ON CONFLICT(connection_id, chat_id) DO UPDATE SET until_ts=excluded.until_ts""",
            (connection_id, chat_id, time.time() + minutes * 60),
        )
        self.conn.commit()

    def active_pauses(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pauses WHERE until_ts > ? ORDER BY until_ts",
                                 (time.time(),)).fetchall()

    def resume_chats(self, connection_id: str | None = None) -> int:
        """Снять паузы: бот снова отвечает. Без connection_id — во всех подключениях."""
        if connection_id:
            cur = self.conn.execute("DELETE FROM pauses WHERE connection_id=?", (connection_id,))
        else:
            cur = self.conn.execute("DELETE FROM pauses")
        self.conn.commit()
        return cur.rowcount

    def is_paused(self, connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT until_ts FROM pauses WHERE connection_id=? AND chat_id=?", (connection_id, chat_id)
        ).fetchone()
        return bool(row and row["until_ts"] > time.time())
