"""Внедрение одного бизнеса: прогресс берём из данных, подтверждения привязываем к версии базы."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from html import escape

from .core import versions
from .core.facts import contains
from .db import Connection
from .services import Services, model_title

KEY = "onboarding"
SOURCES = {"documents": "Документы", "export": "Экспорт переписки", "observe": "Наблюдение"}
MODES = {"observe": "Наблюдение", "draft": "Черновики", "auto": "Автоответ"}
TITLES = {"channel": "Подключить канал", "source": "Добавить материалы", "policy": "Задать границы",
          "knowledge": "Утвердить знания", "check": "Проверить ответы"}
CASE_COUNT = 3


def load(services: Services) -> dict:
    try:
        value = json.loads(services.db.get_settings().get(KEY, "{}"))
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def save(services: Services, **values) -> None:
    services.db.set_settings({KEY: json.dumps({**load(services), **values}, ensure_ascii=False)})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def policy_context(services: Services, profile: dict) -> str:
    return digest({"mode": profile.get("target_mode", "draft"), "stop_topics": services.draft_kb().stop_topics,
                   "holding_message": services.holding_message})


def knowledge_context(services: Services) -> str:
    return digest({"version": services.kb_version,
                   "files": versions.fingerprint(services.version_dir(services.kb_version))})


def check_context(services: Services, profile: dict) -> str:
    current = services.current()
    return digest({"business": profile.get("business_name"), "connection": profile.get("connection_id"),
                   "knowledge": knowledge_context(services),
                   "draft": versions.fingerprint(services.settings.kb_dir),
                   "settings": {k: v for k, v in current.items() if k not in ("daily_llm_limit", "summary_time")},
                   "policy": policy_context(services, profile), "cases": profile.get("cases"),
                   "provider": services.settings.llm_provider, "model": model_title(services.settings),
                   "endpoint": (services.settings.gigachat_base_url, services.settings.openai_base_url)})


def case_values(form) -> list[dict]:
    return [{"question": str(form.get(f"question_{i}", "")).strip(),
             "expected": str(form.get(f"expected_{i}", "")).strip(),
             "fact": str(form.get(f"fact_{i}", "")).strip()} for i in range(CASE_COUNT)]


def cases_from_form(form) -> list[dict]:
    cases = case_values(form)
    if any(not c["question"] or len(c["question"]) > 1000 for c in cases):
        raise ValueError("Введите три вопроса, каждый не длиннее 1000 символов.")
    if any(c["expected"] not in ("answer", "escalate") for c in cases):
        raise ValueError("Выберите ожидаемое действие для каждого вопроса.")
    if any(len(c["fact"]) > 300 or (c["expected"] == "answer" and not c["fact"]) for c in cases):
        raise ValueError("Для ответа укажите проверяемый факт или короткую фразу, до 300 символов.")
    if len({c["question"].casefold() for c in cases}) != CASE_COUNT:
        raise ValueError("Для проверки нужны три разных вопроса.")
    if {c["expected"] for c in cases} != {"answer", "escalate"}:
        raise ValueError("Добавьте и вопрос с ответом из базы, и вопрос для передачи менеджеру.")
    return cases


@dataclass(frozen=True)
class Step:
    key: str
    done: bool
    detail: str


@dataclass
class Progress:
    profile: dict
    steps: list[Step]
    connection: Connection | None
    ready: bool
    blockers: list[str]
    report: dict
    report_current: bool

    @property
    def next_step(self) -> str:
        return next((s.key for s in self.steps if not s.done), "check")


def progress(services: Services) -> Progress:
    profile = load(services)
    connection = next((c for c in services.db.connections() if c.id == profile.get("connection_id")), None)
    bot = services.bot_status or {}
    # У MAX нет бизнес-режима: достаточно, что бот на связи
    channel_up = ((services.max_status or {}).get("ok") if connection and connection.id.startswith("max:")
                  else bot.get("ok") and bot.get("business"))
    connected = bool(connection and connection.enabled and connection.approved and channel_up)
    named = bool(profile.get("business_name"))
    channel_detail = ("Подключение найдено." if connected else "Нужно действующее разрешённое подключение Telegram.")
    if not named:
        channel_detail = "Укажите название бизнеса и выберите подключение."

    draft = services.draft_kb()
    material = bool(draft.prices or draft.faq)
    source = profile.get("source")
    if source == "documents":
        source_done = material
        source_detail = "Материалы добавлены в черновик." if material else "Загрузите прайс, ответы или заметки."
    elif source == "export":
        path = services.settings.db_path.parent / "import" / "stats.json"
        try:
            stats = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            source_done = isinstance(stats, dict) and stats.get("pairs", 0) > 0
        except (ValueError, TypeError, OSError):
            source_done = False
        source_detail = "Переписка разобрана. Ответы ещё нужно проверить и перенести в базу." if source_done else \
                        "Загрузите экспорт, в котором есть вопросы клиентов и ответы менеджера."
    elif source == "observe":
        rows = services.db.conn.execute(
            "SELECT role, COUNT(*) AS n FROM observed_messages WHERE connection_id=? GROUP BY role",
            (profile.get("connection_id", ""),)).fetchall()
        counts = {r["role"]: r["n"] for r in rows}
        source_done = bool(counts.get("client") and counts.get("manager"))
        source_detail = ("Получены сообщения клиента и менеджера." if source_done else
                         "Ждём вопрос клиента и ответ менеджера в выбранном подключении.")
    else:
        source_done, source_detail = False, "Выберите, с чего начнётся база знаний."

    policy_done = bool(profile.get("policy_review") and profile["policy_review"] == policy_context(services, profile))
    policy_detail = (f"Выбран режим: {MODES.get(profile.get('target_mode'), 'Черновики')}." if policy_done else
                     "Выберите режим и темы, которые должен решать менеджер.")
    published = bool(services.kb.prices or services.kb.faq)
    knowledge_done = bool(published and not services.draft_changed()
                          and profile.get("knowledge_review") == knowledge_context(services))
    knowledge_detail = (f"Проверена опубликованная версия {services.kb_version}." if knowledge_done else
                        "Проверьте факты и цены, опубликуйте черновик и подтвердите сверку.")
    report = profile.get("report") if isinstance(profile.get("report"), dict) else {}
    report_current = bool(report and report.get("context") == check_context(services, profile))
    check_done = bool(report_current and report.get("passed") and report.get("reviewed") and not report.get("mock"))
    check_detail = "Задайте три вопроса своего бизнеса и проверьте ответы."
    if report:
        if not report_current:
            check_detail = "База, настройки или вопросы изменились — повторите проверку."
        elif report.get("mock"):
            check_detail = "Проверена заглушка. Для готовности нужен прогон с рабочей моделью."
        elif not report.get("passed"):
            check_detail = "Есть ошибки — исправьте базу или настройки и повторите проверку."
        elif not report.get("reviewed"):
            check_detail = "Автопроверка пройдена. Осталось сверить смысл ответов."
        else:
            check_detail = "Ответы проверены автоматически и подтверждены менеджером."
    steps = [Step("channel", connected and named, channel_detail), Step("source", bool(source_done), source_detail),
             Step("policy", policy_done, policy_detail), Step("knowledge", knowledge_done, knowledge_detail),
             Step("check", check_done, check_detail)]
    blockers = [TITLES[s.key] + ": " + s.detail for s in steps if not s.done]
    if profile.get("target_mode", "draft") != "observe" and connection and not connection.can_reply:
        blockers.append("У Telegram-подключения нет права отвечать на сообщения.")
    if services.settings.llm_provider == "mock":
        blockers.append("Выберите рабочую модель в настройках.")
    if services.llm_limit_reached():
        blockers.append("Дневной лимит модели исчерпан.")
    return Progress(profile, steps, connection, not blockers, blockers, report, report_current)


async def run_check(services: Services, cases: list[dict]) -> dict:
    save(services, cases=cases)
    profile = load(services)
    context = check_context(services, profile)
    version, model, mock = services.kb_version, model_title(services.settings), services.settings.llm_provider == "mock"
    rows = []
    for case in cases:
        if services.llm_limit_reached():
            rows.append({**case, "answer": "", "passed": False, "issue": "Дневной лимит модели исчерпан."})
            continue
        try:
            reply = await services.answer(case["question"])
            technical = reply.reason.startswith(("llm_error", "llm_limit"))
            ok = reply.decision == case["expected"] and not technical
            if case["expected"] == "answer":
                ok = ok and not reply.reason.startswith("clarification:") and contains(reply.text, case["fact"])
            rows.append({**case, "answer": reply.text, "decision": reply.decision, "reason": reply.reason,
                         "passed": bool(ok), "issue": "" if ok else "Действие или проверяемый факт не совпали."})
        except Exception:  # noqa: BLE001 — сообщение провайдера может содержать ключ или адрес запроса
            rows.append({**case, "answer": "", "passed": False, "issue": "Не удалось получить ответ модели."})
    unchanged = context == check_context(services, load(services))
    report = {"id": secrets.token_urlsafe(16), "context": context, "rows": rows,
              "passed": unchanged and all(r["passed"] for r in rows), "reviewed": False, "mock": mock,
              "at": services.local_now().strftime("%d.%m.%Y %H:%M"), "kb_version": version, "model": model}
    save(services, report=report)
    return report


def banner(services: Services) -> str:
    state = progress(services)
    count = sum(s.done for s in state.steps)
    name = escape(str(state.profile.get("business_name") or "Внедрение в бизнес"))
    text = "Настройки проверены" if state.ready else f"Готово {count} из 5 шагов"
    return f'''<div class="onboarding-banner"><div><b>{name}</b><div class="muted">{text}</div></div>
<a class="btn light" href="/onboarding">{'Открыть' if state.ready else 'Продолжить внедрение'}</a></div>'''


def render(services: Services, step: str = "", msg: str = "", form: dict | None = None) -> str:
    state = progress(services)
    step = step if step in TITLES else state.next_step
    profile = {**state.profile, **(form or {})}
    count = sum(s.done for s in state.steps)
    links = "".join(f'<li class="{"current" if s.key == step else ""}"><a href="/onboarding?step={s.key}"'
                    f'{" aria-current=step" if s.key == step else ""}><span class="step-mark {"done" if s.done else ""}">'
                    f'{"✓" if s.done else i}</span>{TITLES[s.key]}</a></li>' for i, s in enumerate(state.steps, 1))
    body = {"channel": channel_form, "source": source_form, "policy": policy_form,
            "knowledge": knowledge_form, "check": check_form}[step](services, state, profile)
    index = list(TITLES).index(step)
    next_key = list(TITLES)[min(index + 1, 4)]
    current = next(s for s in state.steps if s.key == step)
    footer = (f'<a class="btn" href="/onboarding?step={next_key}">Дальше</a>' if current.done and step != "check" else "")
    alert = f'<div class="note" role="status">{escape(msg)}</div>' if msg else ""
    return f'''<div class="onboarding-head"><div><h1>Внедрение в бизнес</h1>
<p class="muted">{escape(str(profile.get('business_name') or 'От первого подключения до проверенных ответов'))}</p></div>
<span class="pill">{count} из 5</span></div>{alert}<div class="onboarding-layout">
<ol class="onboarding-steps">{links}</ol><div><div class="card onboarding-panel"><p class="muted">Шаг {index + 1}</p>
<h2>{TITLES[step]}</h2>{body}<div class="row onboarding-next">{footer}</div></div>
<p class="muted">Мастер не включает ответы клиентам. Текущий режим подключения: {escape(mode_text(state.connection))}.</p>
</div></div>'''


def mode_text(connection) -> str:
    return ({"off": "выключен", "observe": "наблюдение", "draft": "черновики", "auto": "автоответ"}
            .get(connection.mode, connection.mode) if connection else "не подключено")


def channel_form(services: Services, state: Progress, profile: dict) -> str:
    conns = [c for c in services.db.connections() if c.approved and c.enabled]
    options = '<option value="">Выберите подключение</option>' + "".join(
        f'<option value="{escape(c.id, quote=True)}" {"selected" if c.id == profile.get("connection_id") else ""}>'
        f'{escape(services.owner_names.get(c.owner_user_id, str(c.owner_user_id)))} · {escape(mode_text(c))}</option>'
        for c in conns)
    bot = services.bot_status or {}
    name = str(bot.get("username") or "")
    link = (f'<a href="https://t.me/{escape(name, quote=True)}">@{escape(name)}</a>' if name else "бота")
    guide = (f'<p>В Telegram откройте {link} и нажмите «Старт». Затем: Настройки → Автоматизация чатов → '
             'добавить бота → выбрать рабочие чаты.</p>')
    if services.settings.max_bot_token and not services.settings.telegram_bot_token:
        guide = ('<p>Менеджер открывает бота компании в MAX и пишет ему /manager. Если подключения уже есть, разрешите '
                 'новое в <a href="/">«Обзоре»</a>.</p>')
    elif not services.settings.telegram_bot_token:
        guide = '<p class="warn">Сначала задайте токен Telegram в настройках установки и перезапустите приложение.</p>'
    elif bot and not bot.get("ok"):
        guide = '<p class="warn">Нет связи с Telegram. Проверьте подключение в «Обзоре».</p>'
    if services.settings.default_mode == "auto":
        guide += ('<p class="warn">Новые подключения сейчас начинают с автоответа. Перед подключением выберите '
                  '«Наблюдение» в <a href="/settings">настройках</a>.</p>')
    return f'''<form method="post" action="/onboarding/channel"><label for="business-name">Название бизнеса</label>
<input id="business-name" type="text" name="business_name" maxlength="120" required
value="{escape(str(profile.get('business_name', '')), quote=True)}" placeholder="Например, студия рекламы">
<p class="hint">Материалы этой установки общие для всех подключённых менеджеров.</p>
{guide}<label for="connection-id">Подключение менеджера</label><select id="connection-id" name="connection_id">{options}</select>
<p class="hint">Новое подключение появится здесь после подключения в мессенджере и разрешения на «Обзоре».</p>
<div class="row onboarding-next"><button>Сохранить и проверить</button><a href="/">Открыть подключения</a></div></form>'''


def source_form(services: Services, state: Progress, profile: dict) -> str:
    source = profile.get("source", "")
    choices = "".join(f'<label class="onboarding-choice"><input type="radio" name="source" value="{key}" '
                      f'{"checked" if source == key else ""} required><span>{title}</span></label>'
                      for key, title in SOURCES.items())
    content = ""
    if source == "documents":
        content = '''<p>Загрузите подготовленные ответы или прайс. Для заметок — файл Markdown с вопросами и ответами.</p>
<form method="post" action="/kb/upload" enctype="multipart/form-data"><input type="hidden" name="back" value="onboarding">
<label for="setup-faq">Ответы на частые вопросы · .md</label><input id="setup-faq" type="file" name="faq" accept=".md">
<label for="setup-prices">Прайс · .csv</label><input id="setup-prices" type="file" name="prices" accept=".csv">
<p class="hint">Выбранный файл заменит соответствующий FAQ или прайс. Перед загрузкой проверьте текущую базу.</p>
<p><button>Загрузить</button></p></form>
<details><summary>Добавить заметки без замены FAQ</summary><form method="post" action="/kb/notes/upload" enctype="multipart/form-data">
<input type="hidden" name="back" value="onboarding"><p><input type="file" name="files" accept=".md" multiple required></p>
<button class="light">Добавить заметки</button></form></details>
<p><a href="/kb">Посмотреть материалы и формат файлов</a></p>'''
    elif source == "export":
        content = '''<p>Экспортируйте переписку из Telegram Desktop в JSON. Импорт подготовит пары вопросов и ответов для сверки.</p>
<form method="post" action="/import" enctype="multipart/form-data"><input type="hidden" name="back" value="onboarding">
<label for="setup-export">Экспорт переписки · .json</label><input id="setup-export" type="file" name="files" accept=".json" multiple required>
<label for="setup-me">Имя менеджера в экспорте</label><input id="setup-me" type="text" name="me">
<p class="hint">Нужно при экспорте отдельных чатов. Для полного экспорта можно оставить пустым.</p><p><button>Разобрать переписку</button></p></form>
<p><a href="/kb">Посмотреть результат импорта и перенести проверенные ответы</a></p>'''
    elif source == "observe":
        content = '''<p>Менеджер общается с клиентами как обычно. Бот сохраняет переписку и предлагает факты для утверждения.</p>
<p>Включите «Наблюдение» у выбранного подключения на «Обзоре». Затем получите вопрос клиента и ответьте на него.</p>
<p class="row"><a class="btn light" href="/">Настроить подключение</a><a href="/dialogs?f=observe">Посмотреть переписку</a></p>'''
    detail = next(s.detail for s in state.steps if s.key == "source")
    return f'''<form method="post" action="/onboarding/source"><div class="onboarding-choices">{choices}</div>
<p><button class="light">Выбрать способ</button></p></form>{content}<p class="hint">{escape(detail)}</p>'''


def policy_form(services: Services, state: Progress, profile: dict) -> str:
    target = profile.get("target_mode", "draft")
    options = "".join(f'<option value="{key}" {"selected" if key == target else ""}>{value}</option>'
                      for key, value in MODES.items())
    topics = str(profile.get("stop_topics", "\n".join(services.draft_kb().stop_topics)))
    holding = str(profile.get("holding_message", services.holding_message))
    return f'''<p>Бот отвечает по утверждённой базе. Неизвестные вопросы и выбранные темы решает менеджер.</p>
<form method="post" action="/onboarding/policy"><label for="setup-mode">С какого режима начнём</label>
<select id="setup-mode" name="target_mode">{options}</select>
<p class="hint">Наблюдение собирает знания. Черновики требуют подтверждения. Автоответ отправляет ответы сам.
Это план запуска; текущий режим подключения не изменится.</p>
<label for="setup-stops">Какие темы всегда передавать менеджеру</label>
<textarea id="setup-stops" name="stop_topics" rows="5" maxlength="10000" required>{escape(topics)}</textarea>
<p class="hint">По одному слову или началу слова на строку. Например: скидк, договор, возврат.</p>
<label for="setup-holding">Что сказать клиенту при передаче менеджеру</label>
<textarea id="setup-holding" name="holding_message" rows="2" maxlength="500" required>{escape(holding)}</textarea>
<p class="hint">Темы сохранятся в черновик базы; фраза передачи начнёт действовать после сохранения.</p>
<p><button>Сохранить границы</button></p></form>'''


def knowledge_form(services: Services, state: Progress, profile: dict) -> str:
    kb = services.draft_kb()
    pending = services.db.conn.execute("SELECT COUNT(*) FROM candidates WHERE status='new'").fetchone()[0]
    changed = services.draft_changed()
    status = ("Есть неопубликованные изменения." if changed else f"Опубликована версия {services.kb_version}.")
    publish = ('''<form method="post" action="/kb/publish"><input type="hidden" name="back" value="onboarding">
<p><button>Проверить и опубликовать черновик</button></p></form>''' if changed else "")
    return f'''<p>Проверьте материалы своего бизнеса: актуальность, цены, ограничения и условия.</p>
<p class="muted">Позиций прайса: {len(kb.prices)} · ответов и разделов: {len(kb.faq)}
· кандидатов на рассмотрение: {pending}</p><p>{status}</p>
<p class="row"><a class="btn light" href="/learn">Разобрать кандидатов</a><a href="/kb">Проверить и опубликовать базу</a></p>
{publish}
<form method="post" action="/onboarding/knowledge"><input type="hidden" name="context" value="{knowledge_context(services)}">
<label class="onboarding-choice">
<input type="checkbox" name="reviewed" value="1" required><span>Я проверил опубликованные факты и цены своего бизнеса</span></label>
<p><button{' disabled' if changed or not (services.kb.prices or services.kb.faq) else ''}>
Подтвердить сверку</button></p></form><p class="hint">Подтверждение относится к текущей версии. После изменения базы нужна новая сверка.</p>'''


def check_form(services: Services, state: Progress, profile: dict) -> str:
    cases = profile.get("cases") if isinstance(profile.get("cases"), list) else []
    fields = []
    for i in range(CASE_COUNT):
        case = cases[i] if i < len(cases) and isinstance(cases[i], dict) else {}
        expected = case.get("expected", "escalate" if i == CASE_COUNT - 1 else "answer")
        options = "".join(f'<option value="{key}" {"selected" if key == expected else ""}>{label}</option>'
                          for key, label in (("answer", "Ответить"), ("escalate", "Передать менеджеру")))
        fields.append(f'''<fieldset><legend>Вопрос {i + 1}</legend><label for="setup-q-{i}">Что спросит клиент</label>
<input id="setup-q-{i}" type="text" name="question_{i}" maxlength="1000" required value="{escape(str(case.get('question', '')), quote=True)}">
<div class="onboarding-case"><div><label for="setup-expected-{i}">Что должен сделать бот</label>
<select id="setup-expected-{i}" name="expected_{i}">{options}</select></div><div>
<label for="setup-fact-{i}">Факт или фраза в ответе</label><input id="setup-fact-{i}" type="text" name="fact_{i}" maxlength="300"
value="{escape(str(case.get('fact', '')), quote=True)}" placeholder="Для передачи менеджеру оставьте пустым"></div></div></fieldset>''')
    report_html = ""
    if state.report:
        rows = []
        for row in state.report.get("rows", []):
            verdict = "Совпало с ожиданием" if row.get("passed") else str(row.get("issue") or "Не совпало")
            expected = "ответить" if row.get("expected") == "answer" else "передать менеджеру"
            fact = f" · проверяем: {escape(str(row['fact']))}" if row.get("fact") else ""
            rows.append(f'<div class="onboarding-result"><b>{escape(str(row.get("question", "")))}</b>'
                        f'<p class="muted">Ожидание: {expected}{fact}</p>'
                        f'<p class="bubble">{escape(str(row.get("answer", ""))) or "Ответ не получен"}</p>'
                        f'<p class="{"ok" if row.get("passed") else "warn"}">{escape(verdict)}</p></div>')
        detail = next(s.detail for s in state.steps if s.key == "check")
        confirm = ""
        if state.report_current and state.report.get("passed") and not state.report.get("reviewed"):
            confirm = f'''<form method="post" action="/onboarding/review">
<input type="hidden" name="context" value="{escape(str(state.report.get('context', '')), quote=True)}"><label class="onboarding-choice">
<input type="hidden" name="report_id" value="{escape(str(state.report.get('id', '')), quote=True)}">
<input type="checkbox" name="reviewed" value="1" required><span>Сверил ответы: факты, условия и передачи менеджеру верны</span></label>
<p><button>Подтвердить результаты</button></p></form>'''
        report_html = f'''<div class="onboarding-results"><h3>Последняя проверка</h3><p class="muted">
{escape(str(state.report.get('at', '')))} · {escape(str(state.report.get('model', '')))} · версия {escape(str(state.report.get('kb_version', '')))}</p>
<p class="{'ok' if state.ready else 'warn'}">{escape(detail)}</p>{''.join(rows)}{confirm}</div>'''
    blockers = "".join(f'<li>{escape(b)}</li>' for b in state.blockers)
    readiness = (f'<p class="ok"><b>Настройки для режима «{MODES.get(profile.get("target_mode"), "Черновики")}» проверены.</b></p>'
                 '<p>Режим можно включить отдельно на «Обзоре». Мастер его не менял.</p><a href="/">Открыть подключения</a>'
                 if state.ready else f'<details><summary>Что осталось до запуска</summary><ul>{blockers}</ul></details>')
    questions = f'''<p>Возьмите два обычных вопроса своего бизнеса и один, который должен решать менеджер.</p>
<form id="onboarding-questions" method="post" action="/onboarding/check">{''.join(fields)}
<p class="hint">Проверятся три вопроса на текущей модели, расход учитывается в дневном лимите. Ответы появятся только здесь.</p>
<p><button>Проверить ответы</button></p></form>'''
    if state.report:
        return (f'<div class="onboarding-readiness">{readiness}</div>{report_html}'
                '<p><button class="light" form="onboarding-questions">Повторить проверку</button></p>'
                f'<details><summary>Изменить вопросы проверки</summary>{questions}</details>')
    return questions + f'<div class="onboarding-readiness">{readiness}</div>'
