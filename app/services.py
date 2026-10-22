"""Общий контейнер сервисов для бота и админки."""
from __future__ import annotations

import asyncio

import datetime as dt
import logging
import os
import re
import shutil
from collections import deque
from pathlib import Path
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from .config import ROOT, Settings
from .core.assistant import GREETING_MESSAGE, HOLDING_MESSAGE, Assistant
from .core import candidates, versions
from .core.knowledge import KnowledgeBase, format_rub
from .core.price_guard import unknown_amounts
from .db import Database
from .llm import make_provider
from .llm.base import LLMProvider

# Модели, которые можно выбрать в админке: значение поля → подпись
MODELS = {
    "gigachat:GigaChat-Pro": "GigaChat Pro — точнее, рекомендуем",
    "gigachat:GigaChat": "GigaChat Lite — быстрее и дешевле",
    "gigachat:GigaChat-Max": "GigaChat Max — самая сильная",
    "mock": "Заглушка — без нейросети, для проверки",
}
MODEL_NAMES = {"GigaChat": "GigaChat Lite", "GigaChat-Pro": "GigaChat Pro", "GigaChat-Max": "GigaChat Max"}


@dataclass(frozen=True)
class Field:
    kind: type
    low: float = 0
    high: float = 0


# Что менеджер может поменять в админке. Остальное (ключи, токены) — только в .env.
FIELDS = {
    "model": Field(str),
    "default_mode": Field(str),
    "manager_pause_minutes": Field(int, 0, 1440),
    "stale_message_minutes": Field(int, 1, 1440),
    "confidence_threshold": Field(float, 0, 1),
    "retrieval_min_score": Field(float, 0, 20),
    "holding_message": Field(str, 1, 500),
    "greeting_message": Field(str, 1, 500),
    "auto_schedule": Field(str),
    "work_start": Field(str),
    "work_end": Field(str),
    "work_days": Field(str),
    "merge_seconds": Field(float, 0, 30),
    "summary_time": Field(str),
    "address": Field(str),
    "signature": Field(str, 0, 80),
    "daily_llm_limit": Field(int, 0, 100_000),
    "chat_hourly_limit": Field(int, 0, 1000),
    "kb_publish": Field(str),
}
# Эти настройки живут в Services, а не в Settings из .env
EXTRA_KEYS = ("holding_message", "greeting_message", "auto_schedule", "work_start", "work_end", "work_days",
              "merge_seconds", "summary_time", "address", "signature", "daily_llm_limit", "chat_hourly_limit",
              "kb_publish")
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def model_key(settings: Settings) -> str:
    return f"gigachat:{settings.gigachat_model}" if settings.llm_provider == "gigachat" else settings.llm_provider


def model_title(settings: Settings) -> str:
    if settings.llm_provider == "gigachat":
        return MODEL_NAMES.get(settings.gigachat_model, settings.gigachat_model)
    if settings.llm_provider == "openai":
        return settings.openai_model or "OpenAI-совместимая модель"
    return "Заглушка"


def validate(values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Проверить значения из формы. Возвращает (чистые значения, ошибки)."""
    clean, errors = {}, []
    for key, raw in values.items():
        field = FIELDS.get(key)
        if field is None:
            continue
        raw = raw.strip().replace(",", ".") if field.kind is not str else raw.strip()
        if key == "model" and raw not in MODELS:
            errors.append("Неизвестная модель.")
        elif key == "auto_schedule" and raw not in ("always", "off_hours"):
            errors.append("Неизвестное расписание автоответа.")
        elif key == "address" and raw not in ("вы", "ты"):
            errors.append("Обращение — «вы» или «ты».")
        elif key == "summary_time" and raw and not _TIME_RE.match(raw):
            errors.append(f"Время сводки в формате ЧЧ:ММ, а не «{raw}».")
        elif key in ("work_start", "work_end") and not _TIME_RE.match(raw):
            errors.append(f"Время в формате ЧЧ:ММ, а не «{raw}».")
        elif key == "work_days" and not (raw and set(raw) <= set("1234567")):
            errors.append("Отметьте хотя бы один рабочий день.")
        elif key == "kb_publish" and raw not in ("auto", "manual"):
            errors.append("Публикация базы — «сразу после проверки» или «вручную».")
        elif key == "default_mode" and raw not in ("draft", "auto"):
            errors.append("Режим должен быть «Черновики» или «Автоответ».")
        elif field.kind is str and not (field.low <= len(raw) <= (field.high or 10_000)):
            errors.append("Текст не может быть пустым или длиннее 500 символов.")
        elif field.kind in (int, float):
            try:
                number = field.kind(raw)
            except ValueError:
                errors.append(f"Нужно число: {raw or 'пусто'}.")
                continue
            if not field.low <= number <= field.high:
                errors.append(f"Значение {raw} вне допустимых границ ({field.low:g}–{field.high:g}).")
                continue
            raw = f"{number:g}" if field.kind is float else str(number)
        clean[key] = raw
    return clean, errors


def apply(settings: Settings, values: dict[str, str]) -> Settings:
    """Наложить настройки из админки на настройки из .env."""
    changes = {}
    for key, raw in values.items():
        if key == "model":
            provider, _, model = raw.partition(":")
            changes["llm_provider"] = provider
            if model:
                changes["gigachat_model"] = model
        elif key in EXTRA_KEYS:
            continue  # тексты и расписание живут в Services, не в Settings
        elif key in FIELDS:
            changes[key] = FIELDS[key].kind(raw)
    return replace(settings, **changes)


class Services:
    def __init__(self, settings: Settings, llm: LLMProvider | None = None):
        self.base_settings = settings
        self.db = Database(settings.db_path)
        self.overrides = self._load_overrides()
        self.settings = apply(settings, self.overrides)
        self._llm_fixed = llm is not None  # в тестах модель подменена — не пересоздаём
        self.llm = llm or make_provider(self.settings)
        # База знаний: data/kb — черновик менеджера, бот отвечает по опубликованному снимку
        self.versions_dir = settings.db_path.parent / "kb_versions"
        self._exam_cache: tuple[int, frozenset[str], dict[str, list[str]]] | None = None
        self.kb_version = self._ensure_version()
        self.kb = KnowledgeBase.load(self.version_dir(self.kb_version))
        self.assistant = self._make_assistant()
        # Заполняется при запуске бота: {ok, username, business} или {ok: False, error}
        self.bot_status: dict | None = None
        self._chat_locks: dict[tuple[str, int], asyncio.Lock] = {}
        # Лента «что делает бот» для админки и имена чатов (только в памяти, в базу не пишутся)
        self.events: deque[tuple[str, str]] = deque(maxlen=80)
        self.chat_names: dict[int, str] = {}
        self.owner_names: dict[int, str] = {}
        if self.draft_changed():  # заметки правили в Obsidian или файлы заменили, пока бот был выключен
            logging.getLogger(__name__).info(self.kb_changed("Правка файлов базы вне админки"))

    def event(self, text: str) -> None:
        """Записать решение бота: видно в админке на «Обзоре» и в логе."""
        self.events.append((self.local_now().strftime("%H:%M:%S"), text))
        logging.getLogger("app.bot").info(text)

    def _load_overrides(self) -> dict[str, str]:
        return {k: v for k, v in self.db.get_settings().items() if k in FIELDS}

    @property
    def holding_message(self) -> str:
        return self.overrides.get("holding_message", HOLDING_MESSAGE)

    @property
    def greeting_message(self) -> str:
        return self.overrides.get("greeting_message", GREETING_MESSAGE)

    def _make_assistant(self) -> Assistant:
        return Assistant(self.kb, self.llm, min_score=self.settings.retrieval_min_score,
                         confidence_threshold=self.settings.confidence_threshold,
                         holding_message=self.holding_message, greeting_message=self.greeting_message,
                         address=self.current()["address"])

    async def collect_candidates(self, *, source: str, connection_id: str | None, chat_id: int | None,
                                 dialog_id: int | None, client_question: str, manager_text: str) -> int:
        """Ответ менеджера клиенту → кандидаты в базу знаний. До утверждения бот их не видит.
        Возвращает, сколько кандидатов добавлено."""
        if self.llm_limit_reached():  # дневной лимит модели — сохраняем пару как есть, менеджер разберёт
            facts, tokens = candidates.raw_facts(client_question, manager_text), 0
        else:
            facts, tokens = await candidates.extract(self.llm, client_question, manager_text)
        if tokens:
            self.db.add_usage(self.local_now().strftime("%Y-%m-%d"), tokens)
        for fact in facts:
            self.db.add_candidate(connection_id=connection_id, chat_id=chat_id, dialog_id=dialog_id, source=source,
                                  client_question=client_question, manager_text=manager_text,
                                  question=fact.question, answer=fact.answer,
                                  note=candidates.review_note(self.kb, fact.question, fact.answer))
        if facts:
            self.event(f"Из ответа менеджера предложено фактов для базы: {len(facts)} — утвердите в «Обучении»")
        return len(facts)

    def approve_candidates(self, ids: list[int], question: str, answer: str) -> tuple[bool, str]:
        """Утвердить кандидата (группу похожих) — записать в заметку черновика и опубликовать по правилам
        версий. Возвращает (утверждено ли, фраза для менеджера). Цены — только из прайса: сумму из переписки
        бот назвать всё равно не сможет."""
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return False, "Нужны и вопрос, и ответ."
        draft = self.draft_kb()
        if bad := unknown_amounts(answer, draft.allowed_amounts()):
            sums = ", ".join(format_rub(a) for a in bad)
            return False, (f"В ответе сумма {sums}, которой нет в прайсе: бот не сможет её назвать. "
                           "Добавьте позицию в прайс («База знаний») или уберите сумму из ответа.")
        path, venue = candidates.target_note(draft, question, answer)
        candidates.add_to_note(self.settings.kb_dir / "notes" / path, venue, question, answer)
        self.db.supersede_candidates(question)
        self.db.decide_candidates(ids, "approved", question, answer)
        return True, self.kb_changed(f"Утверждён факт из переписки: «{question}»")

    # --- версии базы знаний: черновик → проверка → публикация, откат ---
    def version_dir(self, version_id: int) -> Path:
        return self.versions_dir / f"{version_id:04d}"

    def _ensure_version(self) -> int:
        """Номер опубликованной версии. Первый запуск (или пропал снимок) — публикуем черновик как есть."""
        row = self.db.active_kb_version()
        if row is not None and self.version_dir(row["id"]).is_dir():
            return row["id"]
        return self._snapshot("Первая версия", versions.Report())

    def _snapshot(self, source: str, report: versions.Report) -> int:
        draft = self.settings.kb_dir
        version_id = self.db.add_kb_version(source, versions.fingerprint(draft), report.to_json())
        target = self.version_dir(version_id)
        shutil.rmtree(target, ignore_errors=True)
        versions.copy_kb(draft, target)
        self.db.activate_kb_version(version_id)
        return version_id

    def draft_changed(self) -> bool:
        row = self.db.get_kb_version(self.kb_version)
        return row is None or row["fingerprint"] != versions.fingerprint(self.settings.kb_dir)

    def control_questions(self) -> list[versions.Control]:
        """Экзамен перед публикацией: вопросы из EXAM_FILES (по умолчанию eval/questions*.csv)
        и проверенная разметка ошибок из журнала."""
        files = []
        for pattern in self.settings.exam_files.split(","):
            if pattern := pattern.strip():
                base = Path(pattern)
                root, rel = (Path(base.anchor), str(base.relative_to(base.anchor))) if base.is_absolute() else (ROOT, pattern)
                files += sorted(root.glob(rel))
        return versions.control_questions(files, self.db.recent_dialogs(10_000, flagged=True, reviewed=True))

    def check_draft(self) -> versions.Report:
        report, _ = self._check()
        return report

    def _check(self) -> tuple[versions.Report, dict[str, list[str]]]:
        """Итог проверки черновика и результаты его экзамена. Экзамен опубликованной версии запоминаем:
        он не меняется, пока не сменится версия или набор контрольных вопросов."""
        controls = self.control_questions()
        keys = frozenset(c.key for c in controls)
        cached = self._exam_cache[2] if self._exam_cache and self._exam_cache[:2] == (self.kb_version, keys) else None
        draft = KnowledgeBase.load(self.settings.kb_dir)
        report, result = versions.check(self.version_dir(self.kb_version), self.kb, self.settings.kb_dir, draft,
                                        controls, cached)
        return report, result

    def publish(self, source: str, force: bool = False) -> tuple[int | None, versions.Report]:
        """Проверить черновик и опубликовать. Регрессия или новое противоречие — не публикуем, если менеджер
        не подтвердил явно (force). Возвращает (номер новой версии или None, итог проверки)."""
        report, result = self._check()
        if report.blocking and not force:
            return None, report
        self.kb_version = self._snapshot(source, report)
        self._exam_cache = (self.kb_version, frozenset(c.key for c in self.control_questions()), result)
        self.reload_kb()
        self.event(f"Опубликована версия базы {self.kb_version}: {source.lower()}; {report.summary()}")
        return self.kb_version, report

    def kb_changed(self, source: str) -> str:
        """Черновик базы изменён в админке или из Telegram. В режиме «сразу после проверки» публикуем,
        если проверка пройдена. Возвращает фразу для менеджера."""
        if not self.draft_changed():
            return "Изменений в базе нет."
        if self.current()["kb_publish"] != "auto":
            return "Сохранено в черновик. Бот увидит изменение после публикации: «База знаний» → «Опубликовать»."
        version, report = self.publish(source)
        if version is not None:
            return f"Опубликована версия базы {version} ({report.summary()}) — бот уже отвечает по ней."
        return (f"Сохранено в черновик, но не опубликовано: {report.reason()}. "
                "Бот отвечает по прошлой версии; разберите на странице «База знаний».")

    def rollback(self, version_id: int) -> str:
        """Вернуть бота к прежней версии. Черновик тоже возвращается к ней, чтобы следующая правка
        не принесла обратно то, от чего откатились; неопубликованные правки сохраняются рядом."""
        row = self.db.get_kb_version(version_id)
        if row is None or not self.version_dir(version_id).is_dir():
            return "Такой версии нет."
        if self.draft_changed():
            keep = self.versions_dir / f"черновик-до-отката-{self.local_now():%Y-%m-%d-%H%M%S}"
            versions.copy_kb(self.settings.kb_dir, keep)
        versions.replace_kb(self.version_dir(version_id), self.settings.kb_dir)
        self.db.activate_kb_version(version_id)
        self.kb_version = version_id
        self.reload_kb()
        self.event(f"База знаний возвращена к версии {version_id}")
        return f"Бот снова отвечает по версии {version_id}."

    def reload_kb(self) -> None:
        """Перечитать опубликованную версию базы знаний."""
        self.kb = KnowledgeBase.load(self.version_dir(self.kb_version))
        self.assistant = self._make_assistant()

    def draft_kb(self) -> KnowledgeBase:
        """Черновик базы: его показывает и правит админка, по нему считаются разрешённые суммы."""
        return KnowledgeBase.load(self.settings.kb_dir)

    def draft_assistant(self) -> Assistant:
        """Ассистент на черновике: проверить ответ на новый факт до публикации."""
        return Assistant(self.draft_kb(), self.llm, min_score=self.settings.retrieval_min_score,
                         confidence_threshold=self.settings.confidence_threshold,
                         holding_message=self.holding_message, greeting_message=self.greeting_message,
                         address=self.current()["address"])

    async def update_settings(self, values: dict[str, str]) -> list[str]:
        """Сохранить настройки из админки и сразу применить. Возвращает ошибки."""
        clean, errors = validate(values)
        if errors:
            return errors
        # Совпадает с .env или текстом по умолчанию — не храним, чтобы правка .env снова работала
        defaults = self.defaults()
        keep = {k: v for k, v in clean.items() if v != defaults.get(k)}
        self.db.delete_settings([k for k in clean if k not in keep])
        self.db.set_settings(keep)
        await self._reapply()
        return []

    async def reset_settings(self) -> None:
        self.db.delete_settings(list(FIELDS))
        await self._reapply()

    def defaults(self) -> dict[str, str]:
        s = self.base_settings
        return {
            "model": model_key(s), "default_mode": s.default_mode,
            "manager_pause_minutes": str(s.manager_pause_minutes),
            "stale_message_minutes": str(s.stale_message_minutes),
            "confidence_threshold": f"{s.confidence_threshold:g}",
            "retrieval_min_score": f"{s.retrieval_min_score:g}",
            "holding_message": HOLDING_MESSAGE, "greeting_message": GREETING_MESSAGE,
            "auto_schedule": "always", "work_start": "09:00", "work_end": "19:00", "work_days": "12345",
            "merge_seconds": "4", "summary_time": "20:00", "address": "вы", "signature": "",
            "daily_llm_limit": "1000", "chat_hourly_limit": "30", "kb_publish": "auto",
        }

    def current(self) -> dict[str, str]:
        return {**self.defaults(), **self.overrides}

    # --- расписание: днём менеджер на месте, бот только готовит черновики ---
    def auto_allowed(self, now: dt.datetime | None = None) -> bool:
        """Можно ли сейчас отвечать клиентам самому (для подключений в режиме «Автоответ»)."""
        cur = self.current()
        if cur["auto_schedule"] != "off_hours":
            return True
        now = now or self.local_now()
        working_day = str(now.isoweekday()) in cur["work_days"]
        return not (working_day and cur["work_start"] <= now.strftime("%H:%M") < cur["work_end"])

    # --- ответ с учётом расхода: дневной лимит обращений к модели ---
    def chat_lock(self, connection_id: str, chat_id: int) -> asyncio.Lock:
        """Один ответ в чате за раз: следующее сообщение ждёт, пока первое не запишется в журнал."""
        return self._chat_locks.setdefault((connection_id, chat_id), asyncio.Lock())

    def usage_today(self) -> tuple[int, int]:
        return self.db.usage(self.local_now().strftime("%Y-%m-%d"))

    def llm_limit_reached(self) -> bool:
        limit = int(self.current()["daily_llm_limit"])
        return bool(limit) and self.usage_today()[0] >= limit

    async def answer(self, question: str, history: list[tuple[str, str]] | None = None, last_question: str = "",
                     conversation=None, draft: bool = False):
        """Ответ ассистента для бота и тестового чата (draft — по черновику базы, для проверки в админке).
        Каждое обращение к модели учитывается; после дневного лимита модель не вызывается."""
        limited = self.llm_limit_reached()
        assistant = self.draft_assistant() if draft else self.assistant
        reply = await assistant.answer(question, history, allow_llm=not limited, last_question=last_question,
                                            conversation=conversation)
        if reply.llm_called:
            self.db.add_usage(self.local_now().strftime("%Y-%m-%d"), reply.tokens)
            if self.llm_limit_reached():
                self.event(f"Дневной лимит модели ({self.current()['daily_llm_limit']} обращений) исчерпан — "
                           "до полуночи вопросы уходят менеджеру. Поменять: Настройки → Защита")
        return reply

    def sign(self, text: str) -> str:
        """Подпись менеджера под сообщением бота клиенту, если она задана в настройках."""
        signature = self.current()["signature"].strip()
        return f"{text}\n\n{signature}" if signature and text else text

    def local_now(self) -> dt.datetime:
        return dt.datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "Asia/Yekaterinburg")))

    def schedule_text(self) -> str:
        cur = self.current()
        days = cur["work_days"]
        names = "Пн–Пт" if days == "12345" else ", ".join(DAY_NAMES[int(d) - 1] for d in sorted(days))
        return f"{names} {cur['work_start']}–{cur['work_end']}"

    async def _reapply(self) -> None:
        old_key = model_key(self.settings)
        self.overrides = self._load_overrides()
        self.settings = apply(self.base_settings, self.overrides)
        if not self._llm_fixed and model_key(self.settings) != old_key:
            old, self.llm = self.llm, make_provider(self.settings)
            await old.close()
        self.assistant = self._make_assistant()
