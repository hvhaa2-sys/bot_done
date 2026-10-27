"""Веб-админка: обзор, журнал диалогов, база знаний, экзамен и настройки.

HTML собирается в f-строках без шаблонизатора; всё, что пришло от пользователя
или из базы, проходит через html.escape.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import re
import secrets
import tempfile
import time
from pathlib import Path
from html import escape, unescape as html_unescape
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db as D
from .channels.flow import human_reason
from .core.assistant import SKIP
from .core.conversation import Conversation
from .core.knowledge import PriceRow, format_rub, parse_faq, parse_note, parse_prices, parse_stop_topics, prices_csv
from .core.candidates import group as group_candidates
from .core.learning import faq_entry, group_questions
from .core.price_guard import unknown_amounts
from .core.versions import SHOWN as SHOWN_CHANGES, Report
from .services import DAY_NAMES, MODELS, Services, model_key, model_title

STATUS_NAMES = {
    D.SENT_AUTO: "Бот ответил сам",
    D.DRAFT_PENDING: "Черновик ждёт",
    D.DRAFT_SENT: "Черновик отправлен",
    D.DRAFT_EDITED: "Отправлена правка",
    D.DRAFT_REJECTED: "Черновик пропущен",
    D.ESCALATED: "Передано менеджеру",
    D.SANDBOX: "Тестовый чат",
}

# Фильтры журнала: ключ в адресе → (подпись, условия выборки)
FILTERS = {
    "": ("Все", {}),
    "auto": ("Ответил сам", {"statuses": (D.SENT_AUTO,)}),
    "manager": ("Передано менеджеру", {"statuses": (D.ESCALATED,)}),
    "drafts": ("Черновики", {"statuses": (D.DRAFT_PENDING, D.DRAFT_SENT, D.DRAFT_EDITED, D.DRAFT_REJECTED)}),
    "flagged": ("Ошибки", {"flagged": True}),
    "sandbox": ("Тестовый чат", {"statuses": (D.SANDBOX,)}),
}

KB_FILES = {"faq.md": "text/markdown", "prices.csv": "text/csv", "stop_topics.txt": "text/plain"}
IMPORT_FILES = {"faq_draft.md": "text/markdown", "questions_real.csv": "text/csv", "pairs.csv": "text/csv"}
EXAM_KEY = "exam_last"
# ₽ за 1000 токенов по тарифам GigaChat API для юрлиц (октябрь 2026) — для оценки расхода в админке
PRICE_PER_1K = {"GigaChat": 0.065, "GigaChat-Pro": 0.5, "GigaChat-Max": 0.65}
LOGIN_TRIES, LOGIN_WINDOW = 5, 600  # не больше 5 неверных токенов за 10 минут с одного адреса

CSS = """
:root{--bg:#f4f1ea;--card:#fbfaf6;--text:#12161f;--muted:#5f6370;--line:#ddd8cc;--accent:#c2410c;
--accent-bg:#fde9df;--ok:#15803d;--warn:#b45309;--ink:#12161f;--on-ink:#f4f1ea}
@media (prefers-color-scheme:dark){:root{--bg:#12161f;--card:#1c2230;--text:#f4f1ea;--muted:#b9bfcc;
--line:#2a3142;--accent:#ff6a3d;--accent-bg:#3a2419;--ok:#4ade80;--warn:#fbbf24;--ink:#f4f1ea;--on-ink:#12161f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{border-bottom:1px solid var(--line);background:var(--card)}
.bar{max-width:1080px;margin:0 auto;padding:12px 16px;display:flex;gap:6px 18px;flex-wrap:wrap;align-items:center}
.brand{font-weight:700;margin-right:auto}
nav a{color:var(--muted);text-decoration:none;padding:6px 2px;border-bottom:2px solid transparent}
nav a.on{color:var(--text);border-color:var(--accent)}
nav{display:flex;gap:16px;flex-wrap:wrap}
main{max-width:1080px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:26px;margin:0 0 16px}h2{font-size:18px;margin:0 0 12px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:16px}
.kpis .card{margin:0;padding:14px 16px}
.kpi{font-size:30px;font-weight:700;line-height:1.1}.muted{color:var(--muted);font-size:13px}
.cols{display:grid;grid-template-columns:1.5fr 1fr;gap:16px}@media(max-width:820px){.cols{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:13px}.scroll{overflow-x:auto}
textarea,input[type=text],input[type=number],input[type=search],input[type=time],select{width:100%;padding:10px 12px;
border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);font:inherit}
textarea.code{font:13px/1.5 ui-monospace,Menlo,monospace}
label{display:block;margin:14px 0 6px;font-weight:600}.hint{color:var(--muted);font-size:13px;margin-top:4px}
button,.btn{background:var(--ink);color:var(--on-ink);border:0;border-radius:10px;padding:10px 18px;font:inherit;
cursor:pointer;text-decoration:none;display:inline-block}
button.light,.btn.light{background:transparent;color:var(--text);border:1px solid var(--line)}
button.small{padding:4px 10px;font-size:13px}button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.days{display:flex;gap:6px;flex-wrap:wrap}.days label{margin:0;font-weight:400;border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer}.days input{margin-right:4px}
.time{display:flex;gap:10px;align-items:center}.time input{width:130px}
.ok{color:var(--ok)}.warn{color:var(--warn)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;border:1px solid var(--line);font-size:12px;white-space:nowrap}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.tabs a{padding:5px 12px;border-radius:99px;border:1px solid var(--line);text-decoration:none;color:var(--text);font-size:14px}
.tabs a.on{background:var(--ink);color:var(--on-ink);border-color:var(--ink)}
.status{list-style:none;margin:0;padding:0}.status li{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.status li:last-child{border:0}.dot{width:9px;height:9px;border-radius:50%;margin-top:7px;flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.bubble{background:var(--bg);border:1px solid var(--line);border-radius:14px 14px 14px 4px;padding:12px 14px;white-space:pre-wrap}
.note{background:var(--accent-bg);border-radius:10px;padding:10px 14px;margin-bottom:16px}
tr.flag td{background:var(--accent-bg)}details summary{cursor:pointer;color:var(--muted)}
pre{white-space:pre-wrap;margin:0;font:inherit}
"""

NAV = [("/", "Обзор"), ("/dialogs", "Диалоги"), ("/learn", "Обучение"), ("/kb", "База знаний"),
       ("/exam", "Экзамен"), ("/settings", "Настройки")]


def page(title: str, body: str, active: str = "/", refresh: int | None = None) -> HTMLResponse:
    nav = "".join(f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>' for href, name in NAV)
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>{escape(title)}</title>
<style>{CSS}</style></head><body><header><div class="bar"><span class="brand">ИИ-помощник менеджера</span>
<nav>{nav}<a href="/logout" class="muted">Выйти</a></nav></div></header><main>{body}</main></body></html>""")


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(24, "стоп-тема", "стоп-темы", "стоп-тем") → «24 стоп-темы»."""
    form = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {form}"


def login_page(msg: str = "", status: int = 401) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход</title><style>{CSS}</style></head>
<body><main style="max-width:420px;padding-top:12vh"><div class="card"><h1>ИИ-помощник менеджера</h1>
{note(msg)}<form method="post" action="/login"><label for="t">Токен админки</label>
<input id="t" type="text" name="token" autocomplete="current-password" required autofocus>
<p class="hint">Он записан в файле .env в строке ADMIN_TOKEN.</p><p><button>Войти</button></p></form></div></main>
</body></html>""", status_code=status)


def note(msg: str) -> str:
    return f'<div class="note">{escape(msg)}</div>' if msg else ""


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="AI Sales Assistant admin", docs_url=None, redoc_url=None)
    exam: dict = {"running": False, "done": 0, "total": 0, "error": ""}

    failures: dict[str, list[float]] = {}

    def locked(ip: str) -> bool:
        recent = [t for t in failures.get(ip, []) if time.time() - t < LOGIN_WINDOW]
        failures[ip] = recent
        return len(recent) >= LOGIN_TRIES

    def auth(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        token = request.query_params.get("token") or request.cookies.get("admin_token")
        if locked(ip):
            raise HTTPException(429, "Слишком много неверных попыток. Подождите 10 минут.")
        if not secrets.compare_digest(token or "", services.settings.admin_token):
            # Считаем только токен из ссылки: старая cookie после смены ADMIN_TOKEN не должна
            # запирать владельца, а просто «без входа» — не попытка подбора
            if request.query_params.get("token"):
                failures.setdefault(ip, []).append(time.time())
            raise HTTPException(401, "Нужен токен: откройте /?token=ВАШ_ADMIN_TOKEN")

    @app.exception_handler(StarletteHTTPException)
    async def login_on_401(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return login_page()
        if exc.status_code == 429:
            return login_page(exc.detail, status=429)
        return await http_exception_handler(request, exc)

    @app.post("/login")
    async def login(request: Request, token: str = Form("")) -> Response:
        ip = request.client.host if request.client else "?"
        if locked(ip):
            return login_page("Слишком много неверных попыток. Подождите 10 минут.", status=429)
        if not secrets.compare_digest(token.strip(), services.settings.admin_token):
            failures.setdefault(ip, []).append(time.time())
            return login_page("Неверный токен.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("admin_token", token.strip(), httponly=True, samesite="lax")
        return response

    @app.get("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("admin_token")
        return response

    @app.middleware("http")
    async def remember_token(request: Request, call_next):
        response = await call_next(request)
        token = request.query_params.get("token")
        if token and secrets.compare_digest(token, services.settings.admin_token):
            response.set_cookie("admin_token", token, httponly=True, samesite="lax")
        return response

    @app.get("/health")
    async def health() -> Response:
        """Для мониторинга на сервере: без токена, без подробностей."""
        services.db.conn.execute("SELECT 1").fetchone()
        return Response('{"ok": true}', media_type="application/json")

    # --- обзор и тестовый чат ---
    @app.get("/", dependencies=[Depends(auth)])
    async def dashboard() -> HTMLResponse:
        return page("Обзор", dashboard_html(services))

    @app.post("/sandbox", dependencies=[Depends(auth)])
    async def sandbox(request: Request, question: str = Form(...)) -> HTMLResponse:
        session = request.cookies.get("sandbox_session", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", session):
            session = secrets.token_urlsafe(24)
        connection_id = f"sandbox:{session}"
        turns = services.db.conversation_turns(connection_id, 0)
        history = [(r["question"], r["answer"]) for r in turns if r["decision"] == "answer"][-3:]
        conversation = Conversation.replay(services.kb, turns)
        reply = await services.answer(question, history, conversation=conversation)
        services.db.log_dialog(connection_id=connection_id, chat_id=0, question=question, answer=reply.text,
                               decision=reply.decision, reason=reply.reason, confidence=reply.confidence,
                               status=D.SANDBOX, sources=reply.sources, latency_ms=reply.latency_ms,
                               mode="sandbox", kb_version=services.kb_version)
        if reply.decision == SKIP:
            verdict = '<span class="ok">Бот промолчит: отвечать не нужно</span>'
        elif reply.reason.startswith("clarification:"):
            verdict = '<span class="ok">Бот уточнит у клиента</span>'
        elif reply.is_answer:
            verdict = '<span class="ok">Бот ответит сам</span>'
        else:
            verdict = f'<span class="warn">Передаст менеджеру: {escape(human_reason(reply.reason))}</span>'
        titles = {c.id: c.text for c in services.kb.chunks}
        sources = "".join(f"<li>{escape(titles.get(s, s)[:140])}</li>" for s in reply.sources[:4])
        shown = services.sign(reply.text) if reply.is_answer else reply.text
        result = f"""<div class="muted">Ответ</div><div class="bubble">{escape(shown) or "—"}</div>
<p>{verdict} <span class="muted">· {reply.latency_ms} мс</span></p>
{f'<details><summary>На что опирался бот</summary><ul class="muted">{sources}</ul></details>' if sources else ''}"""
        response = page("Обзор", dashboard_html(services, question, result))
        response.set_cookie("sandbox_session", session, max_age=10800, httponly=True, samesite="strict")
        return response

    @app.post("/sandbox/reset", dependencies=[Depends(auth)])
    async def reset_sandbox() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("sandbox_session")
        return response

    @app.post("/connections/{connection_id}/approve", dependencies=[Depends(auth)])
    async def approve(connection_id: str) -> RedirectResponse:
        services.db.approve_connection(connection_id)
        services.event("Подключение разрешено в админке — бот работает для этого менеджера")
        return RedirectResponse("/", status_code=303)

    @app.post("/connections/{connection_id}/reject", dependencies=[Depends(auth)])
    async def reject(connection_id: str) -> RedirectResponse:
        services.db.delete_connection(connection_id)
        services.event("Подключение отклонено в админке")
        return RedirectResponse("/", status_code=303)

    @app.post("/resume", dependencies=[Depends(auth)])
    async def resume() -> RedirectResponse:
        n = services.db.resume_chats()
        services.event(f"Паузы сняты из админки ({n}) — бот снова отвечает")
        return RedirectResponse("/", status_code=303)

    @app.post("/mode", dependencies=[Depends(auth)])
    async def set_mode(connection_id: str = Form(...), mode: str = Form(...)) -> RedirectResponse:
        if mode in ("draft", "auto"):
            services.db.set_mode(mode, connection_id=connection_id)
        return RedirectResponse("/", status_code=303)

    # --- журнал ---
    @app.get("/dialogs", dependencies=[Depends(auth)])
    async def dialogs(f: str = "", q: str = "") -> HTMLResponse:
        return page("Диалоги", dialogs_html(services, f, q), "/dialogs")

    @app.post("/dialogs/{dialog_id}/flag", dependencies=[Depends(auth)])
    async def flag(dialog_id: int, flagged: int = Form(...), back: str = Form("/dialogs")) -> RedirectResponse:
        if not services.db.get_dialog(dialog_id):
            raise HTTPException(404, "Диалог не найден.")
        services.db.flag_dialog(dialog_id, bool(flagged))
        back = back if back.startswith("/dialogs") else "/dialogs"
        target = f"/dialogs/{dialog_id}/review?" + urlencode({"back": back}) if flagged else back
        return RedirectResponse(target, status_code=303)

    @app.get("/dialogs/{dialog_id}/review", dependencies=[Depends(auth)])
    async def review_form(dialog_id: int, back: str = "/dialogs?f=flagged") -> HTMLResponse:
        dialog = services.db.get_dialog(dialog_id)
        if not dialog or not dialog["flagged"]:
            raise HTTPException(404, "Отмеченный диалог не найден.")
        return page("Разбор ответа", dialog_review_html(dialog, back), "/dialogs")

    @app.post("/dialogs/{dialog_id}/review", dependencies=[Depends(auth)])
    async def save_review(dialog_id: int, error_type: str = Form(""), expected_decision: str = Form(""),
                          corrected_answer: str = Form(""), must_contain: str = Form(""),
                          back: str = Form("/dialogs?f=flagged")) -> Response:
        values = {"error_type": error_type, "expected_decision": expected_decision,
                  "corrected_answer": corrected_answer, "must_contain": must_contain}
        try:
            saved = services.db.review_dialog(dialog_id, **values)
        except ValueError as exc:
            return page("Разбор ответа", dialog_review_html(
                services.db.get_dialog(dialog_id), back, str(exc), values), "/dialogs")
        if not saved:
            raise HTTPException(404, "Отмеченный диалог не найден.")
        return RedirectResponse(back if back.startswith("/dialogs") else "/dialogs?f=flagged", status_code=303)

    @app.get("/dialogs/flagged.csv", dependencies=[Depends(auth)])
    async def flagged_csv() -> Response:
        return Response(flagged_csv_text(services), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=questions_flagged.csv"})

    # --- обучение: вопросы без ответа → новые записи в FAQ ---
    @app.get("/learn", dependencies=[Depends(auth)])
    async def learn() -> HTMLResponse:
        return page("Обучение", learn_html(services), "/learn")

    @app.post("/learn/add", dependencies=[Depends(auth)])
    async def learn_add(ids: str = Form(...), question: str = Form(...), answer: str = Form(...)) -> HTMLResponse:
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return page("Обучение", learn_html(services, "Нужны и вопрос, и ответ."), "/learn")
        bad = unknown_amounts(answer, services.draft_kb().allowed_amounts())
        if bad:
            sums = ", ".join(format_rub(a) for a in bad)
            return page("Обучение", learn_html(
                services, f"В ответе сумма {sums}, которой нет в прайсе: бот не сможет её назвать. "
                          "Добавьте позицию в прайс или уберите сумму из ответа."), "/learn")
        path = services.settings.kb_dir / "faq.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(text.rstrip("\n") + "\n\n" + faq_entry(question, answer), encoding="utf-8")
        published = services.kb_changed(f"Обучение: ответ на «{question}»")
        services.db.mark_learned([int(i) for i in ids.split(",") if i.isdigit()])
        check = await answer_check(services, question, "Ответ добавлен")
        return page("Обучение", learn_html(services, f"Добавлено в базу знаний. {published}", check), "/learn")

    @app.post("/candidates/approve", dependencies=[Depends(auth)])
    async def candidates_approve(ids: str = Form(...), question: str = Form(...), answer: str = Form(...)) -> HTMLResponse:
        id_list = [int(i) for i in ids.split(",") if i.isdigit()]
        ok, msg = services.approve_candidates(id_list, question, answer)
        if not ok:
            return page("Обучение", learn_html(services, msg), "/learn")
        check = await answer_check(services, question.strip(), "Факт добавлен")
        return page("Обучение", learn_html(services, f"Факт утверждён. {msg}", check), "/learn")

    @app.post("/candidates/reject", dependencies=[Depends(auth)])
    async def candidates_reject(ids: str = Form(...)) -> RedirectResponse:
        services.db.decide_candidates([int(i) for i in ids.split(",") if i.isdigit()], "rejected")
        return RedirectResponse("/learn", status_code=303)

    @app.post("/learn/hide", dependencies=[Depends(auth)])
    async def learn_hide(ids: str = Form(...)) -> RedirectResponse:
        services.db.mark_learned([int(i) for i in ids.split(",") if i.isdigit()])
        return RedirectResponse("/learn", status_code=303)

    # --- база знаний ---
    @app.get("/kb", dependencies=[Depends(auth)])
    async def kb(msg: str = "") -> HTMLResponse:
        return page("База знаний", kb_html(services, msg), "/kb")

    @app.post("/kb/save", dependencies=[Depends(auth)])
    async def kb_save(name: str = Form(...), text: str = Form(...)) -> HTMLResponse:
        parsers = {"faq.md": parse_faq, "stop_topics.txt": parse_stop_topics}
        if name not in parsers:
            raise HTTPException(400, "Неизвестный файл")
        text = text.replace("\r\n", "\n")
        if not parsers[name](text):
            return page("База знаний", kb_html(services, f"{name}: не удалось разобрать, ничего не сохранено."), "/kb")
        (services.settings.kb_dir / name).write_text(text, encoding="utf-8")
        published = services.kb_changed(f"Правка {name}")
        return page("База знаний", kb_html(services, f"Сохранено: {name}. {published}"), "/kb")

    @app.get("/kb/price/{index}", dependencies=[Depends(auth)])
    async def price_form(index: str) -> HTMLResponse:
        rows = services.draft_kb().prices
        if index != "new" and not (index.isdigit() and int(index) < len(rows)):
            raise HTTPException(404, "Нет такой позиции")
        row = rows[int(index)] if index != "new" else PriceRow("", "", "1 месяц", 0, "")
        return page("Прайс", price_form_html(index, row), "/kb")

    @app.post("/kb/price/{index}", dependencies=[Depends(auth)])
    async def price_save(index: str, format: str = Form(""), placement: str = Form(""), period: str = Form(""),
                         price_rub: str = Form(""), notes: str = Form(""), delete: str = Form("")) -> HTMLResponse:
        rows = list(services.draft_kb().prices)
        if index != "new" and not (index.isdigit() and int(index) < len(rows)):
            raise HTTPException(404, "Нет такой позиции")
        if delete:
            removed = rows.pop(int(index))
            msg = f"Удалено из прайса: {removed.format}, {removed.placement}."
            source = f"Убрано из прайса: {removed.format}, {removed.placement}"
        else:
            digits = "".join(ch for ch in price_rub if ch.isdigit())
            row = PriceRow(format.strip(), placement.strip(), period.strip(), int(digits or 0), notes.strip())
            if not row.format or not row.placement or row.price_rub <= 0:
                return page("Прайс", price_form_html(index, row, "Нужны формат, место и цена больше нуля."), "/kb")
            if index == "new":
                rows.append(row)
            else:
                rows[int(index)] = row
            msg = f"Сохранено: {row.format}, {row.placement} — {format_rub(row.price_rub)}."
            source = f"Прайс: {row.format}, {row.placement}"
        (services.settings.kb_dir / "prices.csv").write_text(prices_csv(rows), encoding="utf-8")
        return page("База знаний", kb_html(services, f"{msg} {services.kb_changed(source)}"), "/kb")

    # --- заметки: папка notes/ (её можно открыть как хранилище Obsidian) ---
    @app.get("/kb/note", dependencies=[Depends(auth)])
    async def note_form(path: str = "") -> HTMLResponse:
        file = note_path(services, path) if path else None
        text = file.read_text(encoding="utf-8") if file and file.exists() else NOTE_TEMPLATE
        return page("Заметка", note_form_html(path, text), "/kb")

    @app.post("/kb/note", dependencies=[Depends(auth)])
    async def note_save(path: str = Form(...), text: str = Form(...), delete: str = Form("")) -> HTMLResponse:
        file = note_path(services, path)
        if file is None:
            return page("Заметка", note_form_html(path, text, "Имя файла: буквы, цифры, пробелы, папки через «/», на конце .md."), "/kb")
        if delete:
            file.unlink(missing_ok=True)
            published = services.kb_changed(f"Удалена заметка {path}")
            return page("База знаний", kb_html(services, f"Заметка удалена: {path}. {published}"), "/kb")
        text = text.replace("\r\n", "\n")
        if not parse_note(text, file.stem):
            return page("Заметка", note_form_html(path, text, "Не нашёл ни одного раздела «## …» с текстом — ничего не сохранено."), "/kb")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        published = services.kb_changed(f"Заметка {path}")
        return page("База знаний", kb_html(services, f"Сохранено: {path}. {published}"), "/kb")

    @app.post("/kb/notes/upload", dependencies=[Depends(auth)])
    async def notes_upload(files: list[UploadFile] = File(...)) -> HTMLResponse:
        saved, bad = [], []
        for upload in files:
            file = note_path(services, upload.filename or "")
            text = (await upload.read()).decode("utf-8-sig", errors="replace")
            if file is None or not parse_note(text, file.stem):
                bad.append(upload.filename or "?")
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8")
            saved.append(upload.filename)
        msg = (f"Загружено заметок: {len(saved)}. {services.kb_changed('Загружены заметки: ' + ', '.join(saved))}"
               if saved else "Ничего не загружено.") + \
              (f" Не подошли (нужны .md с разделами «## …»): {', '.join(bad)}." if bad else "")
        return page("База знаний", kb_html(services, msg), "/kb")

    @app.post("/kb/upload", dependencies=[Depends(auth)])
    async def kb_upload(faq: UploadFile | None = File(None), prices: UploadFile | None = File(None),
                        stop_topics: UploadFile | None = File(None)) -> HTMLResponse:
        files = {"faq.md": (faq, parse_faq), "prices.csv": (prices, parse_prices),
                 "stop_topics.txt": (stop_topics, parse_stop_topics)}
        saved = []
        for name, (upload, parser) in files.items():
            if upload is None or not upload.filename:
                continue
            text = (await upload.read()).decode("utf-8-sig")
            if not parser(text):
                return page("База знаний", kb_html(services, f"Файл {name}: не удалось разобрать, проверьте формат."), "/kb")
            services.settings.kb_dir.mkdir(parents=True, exist_ok=True)
            (services.settings.kb_dir / name).write_text(text, encoding="utf-8")
            saved.append(name)
        msg = (f"Загружено: {', '.join(saved)}. {services.kb_changed('Загружены файлы: ' + ', '.join(saved))}"
               if saved else "Файлы не выбраны.")
        return page("База знаний", kb_html(services, msg), "/kb")

    @app.post("/kb/publish", dependencies=[Depends(auth)])
    async def kb_publish(force: str = Form("")) -> HTMLResponse:
        version, report = services.publish("Публикация из админки", force=bool(force))
        msg = (f"Опубликована версия {version} ({report.summary()}) — бот уже отвечает по ней." if version
               else f"Не опубликовано: {report.reason()}.")
        return page("База знаний", kb_html(services, msg), "/kb")

    @app.post("/kb/rollback/{version_id}", dependencies=[Depends(auth)])
    async def kb_rollback(version_id: int) -> HTMLResponse:
        return page("База знаний", kb_html(services, services.rollback(version_id)), "/kb")

    @app.get("/kb/file/{name}", dependencies=[Depends(auth)])
    async def kb_file(name: str) -> Response:
        path = services.settings.kb_dir / name
        if name not in KB_FILES or not path.exists():
            raise HTTPException(404, "Нет такого файла")
        return Response(path.read_bytes(), media_type=f"{KB_FILES[name]}; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.post("/import", dependencies=[Depends(auth)])
    async def import_chats(files: list[UploadFile] = File(...), me: str = Form("")) -> HTMLResponse:
        from tools.import_telegram import run_import
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, upload in enumerate(files):
                if upload.filename:
                    path = Path(tmp) / f"{i}.json"
                    path.write_bytes(await upload.read())
                    paths.append(path)
            try:
                r = await asyncio.to_thread(run_import, paths, me.strip() or None, import_dir(services))
                msg = f"Импорт готов: чатов {r['chats']}, пар «вопрос — ответ» {r['pairs']}."
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                msg = f"Не получилось разобрать выгрузку: {exc}. Нужен экспорт Telegram Desktop в формате JSON."
        return page("База знаний", kb_html(services, msg), "/kb")

    @app.get("/import/file/{name}", dependencies=[Depends(auth)])
    async def import_file(name: str) -> Response:
        path = import_dir(services) / name
        if name not in IMPORT_FILES or not path.exists():
            raise HTTPException(404, "Нет такого файла")
        return Response(path.read_bytes(), media_type=f"{IMPORT_FILES[name]}; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    # --- экзамен ---
    @app.get("/exam", dependencies=[Depends(auth)])
    async def exam_page() -> HTMLResponse:
        return page("Экзамен", exam_html(services, exam), "/exam", refresh=2 if exam["running"] else None)

    @app.post("/exam/run", dependencies=[Depends(auth)])
    async def exam_run() -> RedirectResponse:
        if not exam["running"]:
            exam.update(running=True, done=0, total=0, error="")
            asyncio.get_running_loop().create_task(run_exam(services, exam))
        return RedirectResponse("/exam", status_code=303)

    # --- настройки ---
    @app.get("/settings", dependencies=[Depends(auth)])
    async def settings_page() -> HTMLResponse:
        return page("Настройки", settings_html(services), "/settings")

    @app.post("/settings", dependencies=[Depends(auth)])
    async def settings_save(request: Request) -> HTMLResponse:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "work_day"}
        if "auto_schedule" in form:  # отмеченные дни приходят списком
            values["work_days"] = "".join(sorted(set(map(str, form.getlist("work_day")))))
        errors = await services.update_settings(values)
        msg = "Не сохранено: " + " ".join(errors) if errors else "Сохранено. Изменения уже действуют."
        return page("Настройки", settings_html(services, msg, values if errors else None), "/settings")

    @app.post("/settings/reset", dependencies=[Depends(auth)])
    async def settings_reset() -> HTMLResponse:
        await services.reset_settings()
        return page("Настройки", settings_html(services, "Вернули значения из .env."), "/settings")

    return app


async def run_exam(services: Services, state: dict) -> None:
    """Прогнать экзамен (eval/questions.csv) на текущей модели и базе, итог сохранить в базу."""
    from eval.run_eval import QUESTIONS, evaluate, load_questions, parse_target

    def progress(done: int, total: int) -> None:
        state.update(done=done, total=total)

    try:
        questions = load_questions(QUESTIONS)
        state["total"] = len(questions)
        label, target = parse_target(model_key(services.settings), services.settings)
        summary, rows = await evaluate(label, target, questions, progress)
        day = services.local_now().strftime("%Y-%m-%d")
        for row in rows:  # экзамен тоже тратит модель — пусть это видно в «Модель сегодня»
            if row.get("llm_called"):
                services.db.add_usage(day, row.get("tokens", 0))
        record = {"at": dt.datetime.now().strftime("%d.%m.%Y %H:%M"), "model": model_title(services.settings),
                  "kb_version": services.kb_version, "summary": summary, "rows": rows}
        services.db.set_settings({EXAM_KEY: json.dumps(record, ensure_ascii=False)})
    except Exception as exc:  # сеть, ключ, файл вопросов
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["running"] = False


def import_dir(services: Services) -> Path:
    return services.settings.db_path.parent / "import"


def import_stats(services: Services) -> dict | None:
    path = import_dir(services) / "stats.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def minutes_text(m: float) -> str:
    if m < 1:
        return "меньше минуты"
    if m < 90:
        return f"{m:.0f} мин"
    return f"{m / 60:.1f} ч".replace(".", ",")


def last_exam(services: Services) -> dict | None:
    raw = services.db.get_settings().get(EXAM_KEY)
    return json.loads(raw) if raw else None


def dashboard_html(services: Services, question: str = "", result: str = "") -> str:
    s = services.db.stats()
    kpis = f"""<div class="kpis">
<div class="card"><div class="muted">Вопросов от клиентов</div><div class="kpi">{s['real']}</div></div>
<div class="card"><div class="muted">Бот ответил сам</div><div class="kpi">{s['auto_share']:.0%}</div></div>
<div class="card"><div class="muted">Передано менеджеру</div><div class="kpi">{s['escalated_share']:.0%}</div></div>
<div class="card"><div class="muted">Отмечено ошибок</div><div class="kpi">{s['flagged']}</div></div></div>"""

    sandbox = f"""<div class="card"><h2>Проверить бота</h2>
<p class="muted">Напишите вопрос как клиент — бот ответит здесь, клиенту ничего не уйдёт.</p>
<form method="post" action="/sandbox"><textarea name="question" rows="2" required
placeholder="Сколько стоит экран на фуд-корте в ВЕЕР Молл?">{escape(question)}</textarea>
<p class="row"><button>Спросить</button>
<button class="light" formaction="/sandbox/reset" formnovalidate>Новый разговор</button></p></form>{result}</div>"""

    return f"""<h1>Обзор</h1>{connect_guide_html(services)}{kpis}{before_after_html(services)}{growth_html(services)}<div class="cols">{sandbox}
<div class="card"><h2>Состояние</h2>{status_html(services)}</div></div>{activity_html(services)}{connections_html(services)}"""


def connect_guide_html(services: Services) -> str:
    """Пока ни один менеджер не подключил бота — короткая инструкция с настоящим именем бота."""
    bot = services.bot_status
    if not bot or not bot["ok"] or services.db.connections():
        return ""
    name = escape(bot["username"])
    business = "" if bot["business"] else (
        "<li>В @BotFather: /mybots → @" + name + " → Bot Settings → Secretary Mode (раньше Business Mode) → включить.</li>")
    return f"""<div class="card"><h2>Как подключить бота</h2><ol>{business}
<li>Откройте <a href="https://t.me/{name}">@{name}</a> и нажмите «Старт» — сюда будут приходить карточки и сводки.</li>
<li>Telegram → Настройки → Автоматизация чатов (раньше «Telegram для бизнеса → Чат-боты») → добавьте @{name},
разрешите отвечать на сообщения и выберите чаты.</li>
<li>Попросите кого-нибудь написать вам в личку — ответ бота появится в журнале.</li></ol>
<p class="muted">Режим по умолчанию — черновики: клиенту ничего не уходит, пока вы не нажмёте «Отправить».
Автоответ включается командой /mode auto в чате с ботом.</p></div>"""


def before_after_html(services: Services) -> str:
    """Главная цифра для пилота: сколько клиент ждал ответа до бота и сколько ждёт с ботом."""
    st = import_stats(services)
    if not st or st.get("median_wait_min") is None:
        return ""
    exam = last_exam(services)
    bot = (f"{exam['summary'].get('latency_answer_avg_ms', 0) / 1000:.1f} с".replace(".", ",")
           if exam else "пару секунд")
    off = (f"<div class='muted'>Вечером и в выходные — {st['off_hours_share']:.0%} обращений</div>"
           if st.get("off_hours_share") is not None else "")
    repeated = (f"<div class='muted'>Повторяющиеся вопросы — {st['repeated_share']:.0%}</div>"
                if st.get("repeated_share") is not None else "")
    return f"""<div class="card"><h2>Было → стало</h2><div class="kpis" style="margin:0">
<div><div class="muted">Клиент ждал ответа менеджера</div><div class="kpi">{minutes_text(st['median_wait_min'])}</div>
<div class='muted'>медиана по переписке {escape(st.get('period', ''))}</div></div>
<div><div class="muted">Бот отвечает за</div><div class="kpi ok">{bot}</div><div class='muted'>по последнему экзамену</div></div>
<div><div class="muted">Дольше часа ждали</div><div class="kpi">{st['over_1_hour']:.0%}</div>{off}{repeated}</div>
</div></div>"""


def status_html(services: Services) -> str:
    st, kb = services.settings, services.kb
    items = []

    def item(ok: bool, title: str, text: str) -> None:
        items.append(f'<li><span class="dot {"ok" if ok else "warn"}"></span><div><b>{title}</b>'
                     f'<div class="muted">{text}</div></div></li>')

    if st.llm_provider == "gigachat":
        has_key = bool(st.gigachat_auth_key)
        item(has_key, f"Модель: {escape(model_title(st))}",
             "ключ задан" if has_key else "нет ключа GIGACHAT_AUTH_KEY в .env — все вопросы уйдут менеджеру")
    else:
        item(st.llm_provider != "mock", f"Модель: {escape(model_title(st))}",
             "отвечает первым найденным фрагментом, без нейросети" if st.llm_provider == "mock" else "")
    bot = services.bot_status
    if not st.telegram_bot_token:
        item(False, "Telegram", "нет токена TELEGRAM_BOT_TOKEN в .env — бот не подключён")
    elif bot is None:
        item(None, "Telegram", "подключаюсь…")
    elif not bot["ok"]:
        item(False, "Telegram", escape(bot["error"]))
    elif not bot["business"]:
        item(False, f"Бот @{escape(bot['username'])}",
             "не включён бизнес-режим: @BotFather → /mybots → бот → Bot Settings → Secretary Mode")
    else:
        item(True, f"Бот @{escape(bot['username'])}", "запущен")
    if st.max_bot_token:
        mx = services.max_status
        if mx is None:
            item(None, "MAX", "подключаюсь…")
        elif not mx["ok"]:
            item(False, "MAX", escape(mx["error"]))
        else:
            item(True, f"Бот MAX {escape(mx['username'] or mx['name'])}",
                 "запущен" if any(c.id.startswith("max:") and c.approved for c in services.db.connections())
                 else "запущен; менеджеру — написать боту /manager")
    conns = [c for c in services.db.connections() if c.approved]
    pending = sum(not c.approved for c in services.db.connections())
    modes = ", ".join("автоответ" if c.mode == "auto" else "черновики" for c in conns)
    item(bool(conns) and not pending, "Подключения",
         (f"{len(conns)}: {modes}" if conns else "менеджер ещё не подключил бота")
         + (f" · {pending} ждёт разрешения ниже" if pending else ""))
    calls, tokens = services.usage_today()
    limit = int(services.current()["daily_llm_limit"])
    rub = tokens / 1000 * PRICE_PER_1K.get(st.gigachat_model, 0.5)
    item(not services.llm_limit_reached(), "Модель сегодня",
         f"{plural(calls, 'обращение', 'обращения', 'обращений')} · ~{tokens:,} токенов · ≈ {rub:.2f} ₽".replace(",", " ")
         + (f" · лимит {limit}" if limit else " · без лимита")
         + (" — исчерпан, вопросы уходят менеджеру" if services.llm_limit_reached() else ""))
    if services.current()["auto_schedule"] == "off_hours":
        now = "сейчас бот отвечает сам" if services.auto_allowed() else "сейчас рабочее время — бот готовит черновики"
        item(True, "Автоответ по расписанию", f"только вне рабочего времени ({services.schedule_text()}); {now}")
    draft = ' · <a href="/kb">есть неопубликованные изменения</a>' if services.draft_changed() else ""
    item(bool(kb.prices or kb.faq) and not draft, f"База знаний, версия {services.kb_version}",
         f"{plural(len(kb.prices), 'позиция', 'позиции', 'позиций')} прайса · "
         f"{plural(len(kb.faq), 'ответ', 'ответа', 'ответов')} · "
         f"{plural(len(kb.stop_topics), 'стоп-тема', 'стоп-темы', 'стоп-тем')}{draft}")
    groups = group_questions(services.db.to_learn())
    item(not groups, "Чему научить бота",
         f'<a href="/learn">{plural(len(groups), "вопрос ждёт", "вопроса ждут", "вопросов ждут")} ответа</a>'
         if groups else "новых вопросов без ответа нет")
    exam = last_exam(services)
    if exam:
        sm = exam["summary"]
        item(sm["unsafe_answers"] == 0, "Экзамен",
             f"{sm['decision_accuracy']:.0%} верных решений · {sm['unsafe_answers']} опасных · "
             f"{escape(exam['model'])}, {escape(exam['at'])}")
    else:
        item(False, "Экзамен", 'ещё не запускали — <a href="/exam">запустить</a>')
    return f'<ul class="status">{"".join(items)}</ul>'


def activity_html(services: Services) -> str:
    """Что бот делал последним: видно, почему он ответил, промолчал или передал вопрос."""
    pauses = services.db.active_pauses()
    if not services.events and not pauses:
        return ""
    paused = ""
    if pauses:
        names = ", ".join(
            f"{escape(services.chat_names.get(r['chat_id'], str(r['chat_id'])))} "
            f"до {dt.datetime.fromtimestamp(r['until_ts']).strftime('%H:%M')}" for r in pauses)
        paused = f"""<div class="note row" style="justify-content:space-between"><span>Бот молчит в чатах,
где менеджер отвечал сам: {names}</span><form method="post" action="/resume">
<button class="small">Снять паузы</button></form></div>"""
    rows = "".join(f"<tr><td class='muted' style='white-space:nowrap'>{t}</td><td>{escape(text)}</td></tr>"
                   for t, text in reversed(list(services.events)[-20:]))
    table = (f"<div class='scroll'><table>{rows}</table></div>" if rows
             else "<p class='muted'>Пока ничего не происходило.</p>")
    return f"""<div class="card"><h2>Что делает бот</h2>{paused}{table}
<p class="muted">Обновите страницу, чтобы увидеть новые события. Полный журнал — в «Диалогах».</p></div>"""


def connections_html(services: Services) -> str:
    conns = services.db.connections()
    if not conns:
        return ""

    def row(c) -> str:
        who = escape(services.owner_names.get(c.owner_user_id, f"id {c.owner_user_id}"))
        if c.id.startswith("max:") and not who.startswith("MAX"):
            who = f"MAX: {who}"
        if not c.approved:
            return f"""<tr class="flag"><td>{who}</td><td>ждёт разрешения</td><td class="row">
<form method="post" action="/connections/{escape(c.id)}/approve"><button class="small">Разрешить</button></form>
<form method="post" action="/connections/{escape(c.id)}/reject"><button class="small light">Отклонить</button></form>
</td></tr>"""
        return f"""<tr><td>{who}</td><td>{'включено' if c.enabled else 'выключено'}</td>
<td><form method="post" action="/mode" class="row"><input type="hidden" name="connection_id" value="{escape(c.id)}">
<select name="mode" style="width:auto"><option value="draft" {'selected' if c.mode == 'draft' else ''}>Черновики</option>
<option value="auto" {'selected' if c.mode == 'auto' else ''}>Автоответ</option></select>
<button class="small">Сохранить</button></form></td></tr>"""
    pending = sum(not c.approved for c in conns)
    hint = ("<p class='muted'>Кто-то подключил бота к своему аккаунту. Разрешайте только своим менеджерам: "
            "каждое подключение тратит лимит модели.</p>" if pending else "")
    title = "Подключения" if any(c.id.startswith("max:") for c in conns) else "Подключения Telegram"
    return f"""<div class="card"><h2>{title}</h2>{hint}<div class="scroll"><table>
<tr><th>Менеджер</th><th>Статус</th><th>Режим</th></tr>{"".join(row(c) for c in conns)}</table></div></div>"""


def learn_html(services: Services, msg: str = "", check: str = "") -> str:
    groups = group_questions(services.db.to_learn())
    intro = """<p class="muted">Здесь вопросы, на которые у бота не нашлось ответа, и ответы, отмеченные как ошибка.
Похожие собраны вместе, частые — сверху. Напишите ответ один раз, и в следующий раз бот ответит сам.
Цены в ответ не пишите: бот берёт их из прайса.</p>"""
    if not groups:
        body = '<div class="card ok">Учить пока нечему: бот ответил на всё, что знает база.</div>'
    else:
        cards = []
        for g in groups[:30]:
            ids = ",".join(map(str, g.ids))
            when = dt.datetime.fromtimestamp(g.last_ts).strftime("%d.%m %H:%M")
            why = "отмечено как ошибка" if g.flagged else escape(human_reason(g.reasons[0]))
            others = sorted(set(g.questions) - {g.question}, key=len)[:6]
            variants = ("<details><summary>Другие формулировки</summary><ul class='muted'>"
                        + "".join(f"<li>{escape(q)}</li>" for q in others) + "</ul></details>") if others else ""
            cards.append(f"""<div class="card"><h2>«{escape(g.question)}»</h2>
<p class="muted">спрашивали {plural(g.count, "раз", "раза", "раз")} · последний {when} · {why}</p>{variants}
<form method="post" action="/learn/add"><input type="hidden" name="ids" value="{ids}">
<label>Вопрос для базы</label><input type="text" name="question" value="{escape(g.question)}">
<label>Ответ</label><textarea name="answer" rows="3" required placeholder="Как бы вы ответили клиенту"></textarea>
<p class="row"><button>Добавить в базу</button></p></form>
<form method="post" action="/learn/hide"><input type="hidden" name="ids" value="{ids}">
<button class="light small">Скрыть — это не вопрос для бота</button></form></div>""")
        body = "".join(cards)
    return f"<h1>Чему научить бота</h1>{note(msg)}{check}{candidates_html(services)}{intro}{body}"


async def answer_check(services: Services, question: str, done: str) -> str:
    """Как бот теперь ответит на вопрос: по опубликованной версии или, если правка ждёт публикации, по черновику."""
    draft = services.draft_changed()
    reply = await services.answer(question, draft=draft)
    where = "по черновику бот ответит сам" if draft else "бот теперь отвечает сам"
    if reply.is_answer:
        return f'<p class="ok">Проверка: на «{escape(question)}» {where}.</p><div class="bubble">{escape(reply.text)}</div>'
    return (f'<p class="warn">{done}, но на «{escape(question)}» бот пока передаёт вопрос менеджеру: '
            f'{escape(human_reason(reply.reason))}. Попробуйте сформулировать вопрос так, как пишут клиенты.</p>')


def note_links(line: str) -> str:
    """«(notes/ВЕЕР/Радио ТРЦ.md)» в строке проверки — ссылка на заметку: противоречие правят в один клик."""
    return re.sub(r"\(notes/([^()]+?\.md)\)",
                  lambda m: f'(<a href="/kb/note?{urlencode({"path": html_unescape(m.group(1))})}">{m.group(1)}</a>)',
                  escape(line))


def versions_html(services: Services) -> str:
    """Версии базы: по какой отвечает бот, что ждёт публикации и чем закончилась проверка, история с откатом."""
    shown = SHOWN_CHANGES

    def items(lines: list[str]) -> str:
        more = f"<li>и ещё {len(lines) - shown}</li>" if len(lines) > shown else ""
        return "<ul>" + "".join(f"<li>{note_links(line)}</li>" for line in lines[:shown]) + more + "</ul>"

    def when(ts: float | None) -> str:
        return dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else ""

    active = services.db.get_kb_version(services.kb_version)
    pending = ""
    if services.draft_changed():
        report = services.check_draft()
        problems = ""
        if report.conflicts:
            problems += f'<p class="warn">Противоречия — бот может ответить неверно:</p>{items(report.conflicts)}'
        if report.regressions:
            problems += f'<p class="warn">Поиск перестал находить ответ:</p>{items(report.regressions)}'
        if report.expected:
            problems += (f"<details><summary>Ожидаемо после смены цен: {len(report.expected)}</summary>"
                         f"{items(report.expected)}</details>")
        if report.unfound:
            problems += ('<p class="muted">Новые разделы, которые поиск не находит по их же вопросу, — '
                         f"сформулируйте вопрос так, как пишут клиенты:</p>{items(report.unfound)}")
        button = ('<input type="hidden" name="force" value="1"><button class="light">Опубликовать всё равно</button>'
                  if report.blocking else "<button>Опубликовать</button>")
        pending = f"""<p><b>Изменения, которые бот ещё не видит</b></p>{items(report.changes) if report.changes
            else '<p class="muted">Правки без изменения смысла (порядок, пробелы).</p>'}
<p>Проверка: {escape(report.summary())}.</p>{problems}
<form method="post" action="/kb/publish" class="row">{button}</form>"""
    rows = []
    for row in services.db.kb_versions(10):
        report = Report.from_json(row["report"])
        check = escape(report.summary()) if report.total else ""
        action = ("<span class='ok'>отвечает бот</span>" if row["id"] == services.kb_version else
                  f"<form method='post' action='/kb/rollback/{row['id']}'><button class='light small'>Вернуть</button></form>")
        rows.append(f"<tr><td>{row['id']}</td><td style='white-space:nowrap'>{when(row['created_at'])}</td>"
                    f"<td>{escape(row['source'])}<div class='muted'>{check}</div></td><td>{action}</td></tr>")
    return f"""<div class="card"><h2>Версия базы: {services.kb_version}</h2>
<p class="muted">Вы правите черновик. Бот отвечает по опубликованной версии — опубликована {when(active["activated_at"])}.
Перед публикацией черновик проходит проверку без нейросети, любую прошлую версию можно вернуть.</p>{pending}
<details><summary>История версий</summary><div class="scroll"><table>
<tr><th>№</th><th>Когда</th><th>Что изменилось</th><th></th></tr>{''.join(rows)}</table></div>
<p class="hint">«Вернуть» отменяет и неопубликованные правки; они сохраняются в папке data/kb_versions.</p></details></div>"""


SOURCE_NAMES = {"card": "ответ на карточку бота", "chat": "ответ в чате клиента"}


def candidates_html(services: Services) -> str:
    """Факты из ответов менеджера клиентам: утвердить, поправить или отклонить. До утверждения бот их не видит."""
    groups = group_candidates(services.db.candidates())
    if not groups:
        return ""
    cards = []
    for items in groups[:30]:
        top = items[0]
        ids = ",".join(str(r["id"]) for r in items)
        when = dt.datetime.fromtimestamp(top["created_at"]).strftime("%d.%m %H:%M")
        warn = f'<p class="warn">{escape(top["note"])}</p>' if top["note"] else ""
        sources = "".join(
            f"<li><b>Клиент:</b> {escape(r['client_question'])}<br><b>Вы ({SOURCE_NAMES.get(r['source'], r['source'])}):</b> "
            f"{escape(r['manager_text'])}</li>" for r in items[:5])
        cards.append(f"""<div class="card"><h2>«{escape(top['question'])}»</h2>
<p class="muted">{plural(len(items), "источник", "источника", "источников")} · последний {when}</p>{warn}
<details><summary>Откуда взято</summary><ul class="muted">{sources}</ul></details>
<form method="post" action="/candidates/approve"><input type="hidden" name="ids" value="{ids}">
<label>Вопрос</label><input type="text" name="question" value="{escape(top['question'])}">
<label>Ответ для базы</label><textarea name="answer" rows="3">{escape(top['answer'])}</textarea>
<p class="row"><button>Утвердить</button></p></form>
<form method="post" action="/candidates/reject"><input type="hidden" name="ids" value="{ids}">
<button class="light small">Отклонить</button></form></div>""")
    return f"""<h2>Из переписки — {len(groups)}</h2>
<p class="muted">Факты из ваших ответов клиентам. Пока вы их не утвердите, бот их не использует.
Утверждённое попадает в заметки «Из переписки» в базе знаний.</p>{''.join(cards)}<h2>Вопросы без ответа</h2>"""


def growth_html(services: Services) -> str:
    """Как растёт база: по неделям — вопросы клиентов, доля закрытых ботом и утверждённые факты."""
    weeks = services.db.growth()
    if not weeks:
        return ""
    rows = "".join(
        f"<tr><td>{dt.datetime.fromtimestamp(w['start']).strftime('%d.%m')}</td><td>{w['questions']}</td>"
        f"<td>{(w['answered'] / w['questions'] if w['questions'] else 0):.0%}</td><td>{w['facts']}</td></tr>"
        for w in weeks)
    return f"""<div class="card"><h2>Как растёт база</h2>
<p class="muted">Чем больше утверждённых фактов, тем больше вопросов бот закрывает сам.</p>
<table><tr><th>Неделя с</th><th>Вопросов</th><th>Бот ответил сам</th><th>Утверждено фактов</th></tr>{rows}</table></div>"""


def dialogs_html(services: Services, f: str, q: str) -> str:
    f = f if f in FILTERS else ""
    tabs = "".join(
        f'<a href="/dialogs?f={key}" class="{"on" if key == f else ""}">{name}</a>' for key, (name, _) in FILTERS.items())
    back = escape("/dialogs?" + urlencode({"f": f, "q": q} if q else {"f": f}))
    rows = []
    for r in services.db.recent_dialogs(300, query=q.strip(), **FILTERS[f][1]):
        flagged = bool(r["flagged"])
        reason = escape(human_reason(r["reason"])) if r["reason"] else ""
        rows.append(
            f"<tr class='{'flag' if flagged else ''}'><td class='muted'>"
            f"{dt.datetime.fromtimestamp(r['created_at']).strftime('%d.%m %H:%M')}</td>"
            f"<td>{escape(r['question'])}</td><td>{escape(r['answer'])}</td>"
            f"<td><span class='pill'>{escape(STATUS_NAMES.get(r['status'], r['status']))}</span>"
            f"<div class='muted'>{reason}</div></td>"
            f"<td>{review_link(r)}<form method='post' action='/dialogs/{r['id']}/flag'>"
            f"<input type='hidden' name='flagged' value='{0 if flagged else 1}'>"
            f"<input type='hidden' name='back' value='{back}'>"
            f"<button class='small {'' if flagged else 'light'}'>{'Снять' if flagged else 'Ошибка'}</button>"
            f"</form></td></tr>")
    table = "".join(rows) or "<tr><td colspan=5 class='muted'>Пока пусто</td></tr>"
    export = ('<p><a class="btn light" href="/dialogs/flagged.csv">Скачать для экзамена (CSV)</a> '
              '<span class="muted">только проверенные примеры</span></p>'
              if f == "flagged" and rows else "")
    return f"""<h1>Диалоги</h1><div class="tabs">{tabs}</div>
<form method="get" action="/dialogs" class="row" style="margin-bottom:12px"><input type="hidden" name="f" value="{f}">
<input type="search" name="q" value="{escape(q)}" placeholder="Поиск по вопросам и ответам" style="flex:1">
<button class="light">Найти</button></form>{export}
<div class="card"><p class="muted">Нажмите «Ошибка», если бот ответил неверно или зря передал вопрос.
После разбора пример можно добавить в экзамен.</p><div class="scroll"><table>
<tr><th>Когда</th><th>Вопрос</th><th>Ответ</th><th>Решение</th><th></th></tr>{table}</table></div></div>"""


def flagged_csv_text(services: Services) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["question", "expected", "must_contain"])
    for r in services.db.recent_dialogs(10_000, flagged=True, reviewed=True):
        writer.writerow([r["question"], r["expected_decision"], r["must_contain"]])
    return "﻿" + out.getvalue()  # BOM, чтобы Excel открыл кириллицу


def review_link(dialog) -> str:
    if not dialog["flagged"]:
        return ""
    label = "Разбор сохранён" if dialog["reviewed"] else "Разобрать"
    return f'<p style="margin:0 0 6px"><a href="/dialogs/{dialog["id"]}/review">{label}</a></p>'


def dialog_review_html(dialog, back: str, msg: str = "", values: dict[str, str] | None = None) -> str:
    values = dict(dialog) if values is None else values
    back = back if back.startswith("/dialogs") else "/dialogs?f=flagged"

    def options(key: str, choices: tuple[tuple[str, str], ...]) -> str:
        return '<option value="">Выберите</option>' + "".join(
            f'<option value="{value}" {"selected" if values.get(key) == value else ""}>{label}</option>'
            for value, label in choices)

    types = options("error_type", (("facts", "Неверные факты"), ("decision", "Неверное решение")))
    decisions = options("expected_decision", (("answer", "Ответить клиенту"), ("escalate", "Передать менеджеру")))
    return f"""<h1>Разбор ответа</h1><p><a href="{escape(back)}">← Диалоги</a></p>{note(msg)}
<div class="card"><div class="muted">Вопрос клиента</div><div class="bubble">{escape(dialog['question'])}</div>
<div class="muted">Ответ бота</div><div class="bubble">{escape(dialog['answer'])}</div>
{f'<p class="muted">Бот отвечал по версии базы {dialog["kb_version"]}.</p>' if dialog["kb_version"] else ""}</div>
<div class="card"><form method="post" action="/dialogs/{dialog['id']}/review">
<input type="hidden" name="back" value="{escape(back)}">
<label for="error-type">Что было неверно</label><select id="error-type" name="error_type" required>{types}</select>
<label for="expected-decision">Как правильно поступить</label>
<select id="expected-decision" name="expected_decision" required>{decisions}</select>
<label for="corrected-answer">Правильный ответ</label>
<textarea id="corrected-answer" name="corrected_answer" rows="4" maxlength="4096">{escape(values.get('corrected_answer', ''))}</textarea>
<label for="must-contain">Что проверить в ответе</label>
<input id="must-contain" type="text" name="must_contain" maxlength="1000" value="{escape(values.get('must_contain', ''))}">
<p class="hint">Если бот должен ответить, укажите текст и факты через «;», например: 31500;экран.
Варианты — экран|монитор, запрет — !бесплатно.</p>
<p><button>Подтвердить для экзамена</button></p></form></div>"""


def kb_html(services: Services, msg: str = "") -> str:
    kb, kb_dir = services.draft_kb(), services.settings.kb_dir

    def raw_text(name: str) -> str:
        path = kb_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def raw(name: str) -> str:
        return escape(raw_text(name))

    prices = "".join(
        f"<tr><td><a href='/kb/price/{i}'>{escape(p.format)}</a></td><td>{escape(p.placement)}</td><td style='white-space:nowrap'>{escape(p.period)}</td>"
        f"<td style='white-space:nowrap'>{format_rub(p.price_rub)}</td><td class='muted'>{escape(p.notes)}</td></tr>"
        for i, p in enumerate(kb.prices))
    st = import_stats(services)
    if st:
        top = "".join(f"<tr><td style='white-space:nowrap'>{n} ×</td><td>{escape(q)}</td></tr>" for n, q in st.get("top", []))
        wait = (f"Клиент ждал первого ответа: медиана <b>{minutes_text(st['median_wait_min'])}</b>, "
                f"за 5 минут отвечено {st['within_5_min']:.0%}, дольше часа — {st['over_1_hour']:.0%}."
                if st.get("median_wait_min") is not None else "")
        imported = f"""<p>Чатов: {st['chats']} · пар «вопрос — ответ»: {st['pairs']} ·
повторяющиеся вопросы: {st['repeated_share']:.0%}</p><p>{wait}</p>
<p class="row"><a class="btn light" href="/import/file/faq_draft.md">Черновик FAQ</a>
<a class="btn light" href="/import/file/questions_real.csv">Вопросы для экзамена</a>
<a class="btn light" href="/import/file/pairs.csv">Все пары</a></p>
{f'<details><summary>Самые частые вопросы</summary><table>{top}</table></details>' if top else ''}""" if st.get("repeated_share") is not None else ""
    else:
        imported = ""
    import_card = f"""<div class="card"><h2>Импорт переписки из Telegram</h2>
<p class="muted">Telegram Desktop → Настройки → Продвинутые → Экспорт данных → «Личные чаты», формат JSON.
Бот найдёт частые вопросы и ответы менеджера, посчитает, сколько клиенты ждали ответа, и скроет телефоны, почты и карты.</p>
<form method="post" action="/import" enctype="multipart/form-data" class="row">
<input type="file" name="files" accept=".json" multiple required>
<input type="text" name="me" placeholder="Имя менеджера в Telegram (если выгружали отдельные чаты)" style="flex:1;min-width:240px">
<button>Импортировать</button></form>{imported}</div>"""
    return f"""<h1>База знаний</h1>{note(msg)}{versions_html(services)}
<div class="card"><h2>Прайс · {plural(len(kb.prices), "позиция", "позиции", "позиций")}</h2>
<p class="muted">Бот называет только эти цены. Нажмите на формат, чтобы поменять цену, или правьте весь прайс в Excel:
скачайте файл и загрузите обратно.</p>
<div class="row" style="margin-bottom:12px"><a class="btn" href="/kb/price/new">Добавить позицию</a>
<a class="btn light" href="/kb/file/prices.csv">Скачать prices.csv</a>
<form method="post" action="/kb/upload" enctype="multipart/form-data" class="row">
<input type="file" name="prices" accept=".csv" required><button>Загрузить</button></form></div>
<input type="search" placeholder="Найти в прайсе: фуд-корт, радио, ДЕПО…" oninput="filterRows(this.value)">
<div class="scroll" style="max-height:460px;margin-top:10px"><table id="prices">
<tr><th>Формат</th><th>Место</th><th>Срок</th><th>Цена</th><th>Примечание</th></tr>{prices}</table></div></div>

{notes_card(services)}
<div class="card"><h2>Частые вопросы · {len(parse_faq(raw_text("faq.md")))}</h2>
<p class="muted">Каждый вопрос начинается с «## », ответ — следующими строками. Цены сюда не пишите, они берутся из прайса.</p>
<form method="post" action="/kb/save"><input type="hidden" name="name" value="faq.md">
<textarea name="text" rows="16" class="code">{raw("faq.md")}</textarea>
<p class="row"><button>Сохранить</button><a href="/kb/file/faq.md">скачать faq.md</a></p></form></div>

<div class="card"><h2>Стоп-темы · {len(kb.stop_topics)}</h2>
<p class="muted">По одному слову или началу слова на строку. Вопрос с таким словом бот сразу передаёт менеджеру.
Строки с «#» — комментарии.</p>
<form method="post" action="/kb/save"><input type="hidden" name="name" value="stop_topics.txt">
<textarea name="text" rows="12" class="code">{raw("stop_topics.txt")}</textarea>
<p><button>Сохранить</button></p></form></div>
{import_card}
<script>function filterRows(q){{q=q.toLowerCase();for(const tr of document.querySelectorAll('#prices tr:not(:first-child)'))
tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none'}}</script>"""


NOTE_TEMPLATE = """---
тц: ВЕЕР Молл
источник:
---
# Название заметки

## Вопрос так, как его задаёт клиент?
Ответ: факты, которые бот может сказать клиенту. Цены пишите в прайс, не сюда.
"""
_NOTE_NAME_RE = re.compile(r"^[\w .,()«»№-]+(/[\w .,()«»№-]+)*\.md$")


def notes_dir(services: Services) -> Path:
    return services.settings.kb_dir / "notes"


def note_path(services: Services, name: str) -> Path | None:
    """Путь к заметке внутри notes/ — без «..» и абсолютных путей."""
    name = name.strip().lstrip("/")
    if not _NOTE_NAME_RE.match(name) or ".." in name:
        return None
    root = notes_dir(services).resolve()
    path = (root / name).resolve()
    return path if root in path.parents else None


def notes_card(services: Services) -> str:
    root = notes_dir(services)
    files = sorted(root.rglob("*.md")) if root.is_dir() else []
    rows = "".join(
        f"<tr><td><a href='/kb/note?{urlencode({'path': str(f.relative_to(root))})}'>{escape(str(f.relative_to(root)))}</a></td>"
        f"<td style='white-space:nowrap'>{len(parse_note(f.read_text(encoding='utf-8'), f.stem))}</td></tr>"
        for f in files)
    table = (f"<div class='scroll' style='max-height:360px'><table><tr><th>Заметка</th><th>Разделов</th></tr>{rows}</table></div>"
             if rows else "<p class='muted'>Заметок пока нет.</p>")
    return f"""<div class="card"><h2>Заметки · {len(files)}</h2>
<p class="muted">Условия, требования к макетам, трафик, правила ТЦ — по файлу на тему. Каждый раздел начинается с «## »
и находится ботом отдельно. Папку <code>data/kb/notes</code> можно открыть как хранилище Obsidian.</p>
<div class="row" style="margin-bottom:12px"><a class="btn" href="/kb/note">Новая заметка</a>
<form method="post" action="/kb/notes/upload" enctype="multipart/form-data" class="row">
<input type="file" name="files" accept=".md" multiple required><button>Загрузить .md</button></form></div>{table}</div>"""


def note_form_html(path: str, text: str, msg: str = "") -> str:
    delete = ("<button name=\"delete\" value=\"1\" class=\"light\" onclick=\"return confirm('Удалить заметку?')\">Удалить</button>"
              if path else "")
    return f"""<h1>Заметка</h1>{note(msg)}<div class="card"><form method="post" action="/kb/note">
<p><input type="text" name="path" value="{escape(path)}" placeholder="ВЕЕР/Требования к макетам.md" required style="width:100%"></p>
<textarea name="text" rows="24" class="code">{escape(text)}</textarea>
<p class="row"><button>Сохранить</button>{delete}<a href="/kb">назад</a></p></form></div>"""


def price_form_html(index: str, row: PriceRow, msg: str = "") -> str:
    title = "Новая позиция прайса" if index == "new" else "Позиция прайса"
    delete = ("" if index == "new" else
              f'<form method="post" action="/kb/price/{index}"><input type="hidden" name="delete" value="1">'
              '<button class="light" onclick="return confirm(\'Удалить позицию из прайса?\')">Удалить</button></form>')
    return f"""<h1>{title}</h1>{note(msg)}<div class="card"><form method="post" action="/kb/price/{index}">
<label for="f">Формат</label><input id="f" type="text" name="format" value="{escape(row.format)}" placeholder="Цифровой экран" required>
<label for="pl">Место</label><input id="pl" type="text" name="placement" value="{escape(row.placement)}"
placeholder="ВЕЕР Молл, Екатеринбург — фуд-корт" required>
<p class="hint">Сначала ТЦ, потом через « — » место: так бот не перепутает цены разных ТЦ.</p>
<label for="pe">Срок</label><input id="pe" type="text" name="period" value="{escape(row.period)}" placeholder="1 месяц">
<label for="pr">Цена, ₽</label><input id="pr" type="text" name="price_rub" inputmode="numeric"
value="{row.price_rub or ''}" placeholder="42000" required>
<label for="n">Примечание</label><textarea id="n" name="notes" rows="2" placeholder="с НДС 5%; ролик 10 сек">{escape(row.notes)}</textarea>
<p class="row"><button>Сохранить</button><a href="/kb">Отмена</a></p></form>{delete}</div>"""


def exam_html(services: Services, state: dict) -> str:
    title = escape(model_title(services.settings))
    if state["running"]:
        done, total = state["done"], state["total"] or "…"
        head = f"""<div class="card"><h2>Идёт экзамен: {done} из {total}</h2>
<p class="muted">Модель {title}. Страница обновится сама, это займёт около минуты.</p></div>"""
    else:
        err = note(f"Экзамен не прошёл: {state['error']}") if state["error"] else ""
        head = f"""{err}<div class="card"><h2>Проверить бота на экзамене</h2>
<p class="muted">Вопросы из eval/questions.csv: на одни бот должен ответить сам, на другие — передать менеджеру.
Модель сейчас: {title}. Клиентам ничего не уходит.</p>
<form method="post" action="/exam/run"><button>Запустить экзамен</button></form></div>"""

    exam = last_exam(services)
    if not exam:
        return f"<h1>Экзамен</h1>{head}"
    sm, rows = exam["summary"], exam["rows"]
    kpis = f"""<div class="kpis">
<div class="card"><div class="muted">Верные решения</div><div class="kpi">{sm['decision_accuracy']:.0%}</div></div>
<div class="card"><div class="muted">Опасные ответы</div><div class="kpi {'ok' if sm['unsafe_answers'] == 0 else 'warn'}">{sm['unsafe_answers']}</div></div>
<div class="card"><div class="muted">Нужные факты в ответе</div><div class="kpi">{sm['content_hit']:.0%}</div></div>
<div class="card"><div class="muted">Время, когда отвечает сам</div><div class="kpi">{sm.get('latency_answer_avg_ms', 0) / 1000:.1f} с</div></div></div>"""
    bad = [r for r in rows if not r["correct_decision"] or r["content_ok"] is False]
    errors = "".join(
        f"<tr><td>{escape(r['question'])}</td><td>{'ответил сам' if r['decision'] == 'answer' else 'передал менеджеру'}"
        f"{' · нет нужных фактов' if r['content_ok'] is False else ''}</td><td class='muted'>{escape(r['answer'])}</td></tr>"
        for r in bad)
    errors_html = (f"<div class='card'><h2>Ошибки · {len(bad)}</h2><div class='scroll'><table>"
                   f"<tr><th>Вопрос</th><th>Что сделал бот</th><th>Ответ</th></tr>{errors}</table></div></div>"
                   if bad else "<div class='card ok'>Ошибок нет: все решения верные.</div>")
    all_rows = "".join(
        f"<tr><td>{escape(r['question'])}</td><td>{'ответ' if r['expected'] == 'answer' else 'менеджеру'}</td>"
        f"<td class='{'ok' if r['correct_decision'] else 'warn'}'>{'ответ' if r['decision'] == 'answer' else 'менеджеру'}</td>"
        f"<td class='muted'>{escape(r['answer'])}</td></tr>" for r in rows)
    version = f", версия базы {exam['kb_version']}" if exam.get("kb_version") else ""
    return f"""<h1>Экзамен</h1>{head}<p class="muted">Последний прогон: {escape(exam['at'])}, {escape(exam['model'])}{version},
{plural(sm["questions"], "вопрос", "вопроса", "вопросов")}.</p>{kpis}{errors_html}
<div class="card"><details><summary>Все вопросы экзамена</summary><div class="scroll"><table>
<tr><th>Вопрос</th><th>Ожидали</th><th>Бот</th><th>Ответ</th></tr>{all_rows}</table></div></details></div>"""


def settings_html(services: Services, msg: str = "", form: dict | None = None) -> str:
    v = {**services.current(), **(form or {})}
    st = services.settings

    def text(key: str) -> str:
        return escape(str(v.get(key, "")))

    models = "".join(f'<option value="{key}" {"selected" if v["model"] == key else ""}>{escape(name)}</option>'
                     for key, name in MODELS.items())
    key_warn = ('<p class="hint warn">Ключ GigaChat не задан в .env — пока выберите заглушку.</p>'
                if not st.gigachat_auth_key else "")
    mode = v["default_mode"]
    days = "".join(f'<label><input type="checkbox" name="work_day" value="{i}" '
                   f'{"checked" if str(i) in v["work_days"] else ""}>{name}</label>'
                   for i, name in enumerate(DAY_NAMES, 1))
    return f"""<h1>Настройки</h1>{note(msg)}<form method="post" action="/settings">
<div class="card"><h2>Модель и режим</h2>
<label for="model">Нейросеть</label><select id="model" name="model">{models}</select>{key_warn}
<p class="hint">Lite отвечает быстрее, Pro точнее: на экзамене у Pro не было опасных ответов.</p>
<label for="mode">Режим для новых подключений</label>
<select id="mode" name="default_mode"><option value="draft" {"selected" if mode == "draft" else ""}>Черновики — ответ сначала видит менеджер</option>
<option value="auto" {"selected" if mode == "auto" else ""}>Автоответ — бот отвечает клиенту сам</option></select>
<p class="hint">У уже подключённых менеджеров режим меняется на странице «Обзор» или командой /mode в боте.</p></div>

<div class="card"><h2>Изменения базы знаний</h2>
<label for="publish">Публиковать изменения</label>
<select id="publish" name="kb_publish"><option value="auto" {"selected" if v["kb_publish"] == "auto" else ""}>Сразу, если проверка пройдена</option>
<option value="manual" {"selected" if v["kb_publish"] == "manual" else ""}>Вручную — кнопкой на странице «База знаний»</option></select>
<p class="hint">Перед публикацией бот проверяет черновик: находит ли он по-прежнему ответы на контрольные вопросы
и нет ли противоречий. Если проверка нашла проблему, изменения ждут вашего решения в любом режиме.</p></div>

<div class="card"><h2>Когда бот отвечает сам</h2>
<label for="sched">Автоответ</label>
<select id="sched" name="auto_schedule"><option value="always" {"selected" if v["auto_schedule"] == "always" else ""}>Всегда</option>
<option value="off_hours" {"selected" if v["auto_schedule"] == "off_hours" else ""}>Только в нерабочее время — днём бот готовит черновики</option></select>
<label>Рабочее время менеджера</label>
<div class="time">с <input type="time" name="work_start" value="{text("work_start")}"> до <input type="time" name="work_end" value="{text("work_end")}"></div>
<label>Рабочие дни</label><div class="days">{days}</div>
<p class="hint">Действует для подключений в режиме «Автоответ». Время екатеринбургское.</p></div>

<div class="card"><h2>Защита от лишних трат</h2>
<label for="daily">Обращений к модели в день, не больше</label>
<input id="daily" type="number" name="daily_llm_limit" min="0" max="100000" value="{text("daily_llm_limit")}">
<p class="hint">После лимита бот до полуночи передаёт вопросы менеджеру и не тратит токены. 0 — без лимита.
Один ответ ≈ 800 токенов, на GigaChat Pro ≈ 0,40 ₽.</p>
<label for="hourly">Сообщений от одного клиента в час, не больше</label>
<input id="hourly" type="number" name="chat_hourly_limit" min="0" max="1000" value="{text("chat_hourly_limit")}">
<p class="hint">Если клиент или чужой бот засыпает сообщениями, бот замолкает в этом чате на час и сообщает менеджеру.</p></div>

<div class="card"><h2>Вечерняя сводка</h2>
<label for="summary">Присылать менеджеру итоги дня в Telegram, время</label>
<div class="time"><input id="summary" type="time" name="summary_time" value="{text("summary_time")}"></div>
<p class="hint">Сколько вопросов было, на что бот ответил сам — и кнопки «Ошибка» под его ответами.
Очистите поле, чтобы не присылать.</p></div>

<div class="card"><h2>Тексты бота</h2>
<label for="addr">Обращение к клиентам</label>
<select id="addr" name="address"><option value="вы" {"selected" if v["address"] == "вы" else ""}>На «вы»</option>
<option value="ты" {"selected" if v["address"] == "ты" else ""}>На «ты»</option></select>
<label for="sign">Подпись под ответами бота</label>
<input id="sign" type="text" name="signature" maxlength="80" value="{text("signature")}" placeholder="Например: Анна, реклама в ТЦ">
<p class="hint">Добавляется в конце каждого сообщения клиенту. Пусто — без подписи.</p>
<label for="hold">Когда вопрос уходит менеджеру</label>
<textarea id="hold" name="holding_message" rows="2">{text("holding_message")}</textarea>
<label for="greet">Ответ на приветствие</label>
<textarea id="greet" name="greeting_message" rows="2">{text("greeting_message")}</textarea>
<p class="hint">Пишите так, как обычно пишет менеджер: на «вы» или на «ты», с подписью или без.</p></div>

<div class="card"><h2>Паузы</h2>
<label for="pause">Молчать в чате после того, как менеджер написал сам, минут</label>
<input id="pause" type="number" name="manager_pause_minutes" min="0" max="1440" value="{text("manager_pause_minutes")}">
<label for="stale">Не отвечать на сообщения старше, минут</label>
<input id="stale" type="number" name="stale_message_minutes" min="1" max="1440" value="{text("stale_message_minutes")}">
<p class="hint">Если бот был выключен, на старые сообщения мог уже ответить менеджер.</p>
<label for="merge">Ждать, пока клиент допишет, секунд</label>
<input id="merge" type="number" name="merge_seconds" min="0" max="30" step="0.5" value="{text("merge_seconds")}">
<p class="hint">Клиенты пишут кусками: «Здравствуйте» / «сколько стоит экран» / «на месяц». Бот ответит на всё сразу. 0 — отвечать на каждое сообщение.</p>
<details style="margin-top:14px"><summary>Тонкая настройка</summary>
<label for="conf">Порог уверенности модели, 0–1</label>
<input id="conf" type="text" name="confidence_threshold" value="{text("confidence_threshold")}">
<label for="score">Минимальная похожесть найденного фрагмента</label>
<input id="score" type="text" name="retrieval_min_score" value="{text("retrieval_min_score")}">
<p class="hint">Выше — бот чаще зовёт менеджера, ниже — чаще отвечает сам. После изменения запустите экзамен.</p>
</details></div>
<p class="row"><button>Сохранить</button></p></form>
<form method="post" action="/settings/reset"><p class="row"><button class="light">Вернуть значения из .env</button>
<span class="muted">Ключи, токены и адрес админки меняются только в файле .env.</span></p></form>"""
