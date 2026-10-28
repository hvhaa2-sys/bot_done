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
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db as D
from . import onboarding
from .channels.flow import human_reason
from .core.assistant import SKIP
from .core.conversation import Conversation
from .core.knowledge import PriceRow, format_rub, parse_faq, parse_note, parse_prices, parse_stop_topics, prices_csv
from .core.candidates import group as group_candidates
from .core.candidate_review import NOTE_PREFIX
from .core.learning import faq_entry, group_questions
from .core.price_guard import unknown_amounts
from .core.versions import SHOWN as SHOWN_CHANGES, Report
from .password import verify_password
from .services import DAY_NAMES, MODELS, Services, model_key, model_title, validate

STATUS_NAMES = {
    D.SENT_AUTO: "Бот ответил сам",
    D.DRAFT_PENDING: "Черновик ждёт",
    D.DRAFT_SENT: "Черновик отправлен",
    D.DRAFT_EDITED: "Отправлена правка",
    D.DRAFT_REJECTED: "Черновик пропущен",
    D.ESCALATED: "Передано менеджеру",
    D.SANDBOX: "Тестовый чат",
    D.WATCH: "Наблюдение",
}
# Цвет решения в «Диалогах»: зелёный — бот или менеджер ответили клиенту, янтарный — вопрос ждёт менеджера
PILL_TONE = {D.SENT_AUTO: "ok", D.DRAFT_SENT: "ok", D.DRAFT_EDITED: "ok", D.ESCALATED: "warn", D.DRAFT_PENDING: "warn"}
MODE_TITLES = {"off": "выключен", "observe": "наблюдение", "draft": "черновики", "auto": "автоответ"}

# Фильтры журнала: ключ в адресе → (подпись, условия выборки)
FILTERS = {
    # Обращения из наблюдения — в разделе «Наблюдение»: здесь только разговоры, в которых участвует бот
    "": ("Все", {"statuses": tuple(s for s in STATUS_NAMES if s != D.WATCH)}),
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
# Поиск ⌘K: разделы и места, куда можно перейти, — название, адрес, слова, по которым их ищут
SEARCH_PLACES = [
    ("Обзор", "/", "главная неделя график статистика"),
    ("Проверить бота", "/#sandbox", "тестовый чат спросить песочница"),
    ("Подключения", "/#connections", "менеджеры разрешить режим черновики автоответ"),
    ("Диалоги", "/dialogs", "разговоры журнал переписка"),
    ("Черновики", "/dialogs?f=drafts", "ждут решения отправить"),
    ("Передано менеджеру", "/dialogs?f=manager", "эскалации"),
    ("Ошибки", "/dialogs?f=flagged", "отмеченные разбор экзамен"),
    ("Наблюдение", "/observe", "переписка менеджера время ответа"),
    ("Обучение", "/learn", "научить вопросы без ответа кандидаты факты"),
    ("Прайс", "/kb#prices", "цены позиции база знаний"),
    ("Заметки", "/kb#notes", "база знаний obsidian"),
    ("Частые вопросы", "/kb#faq", "faq база знаний"),
    ("Стоп-темы", "/kb#stops", "темы для менеджера скидки договор"),
    ("Импорт переписки", "/kb#import", "загрузить telegram выгрузка"),
    ("Экзамен", "/exam", "проверка точность опасные"),
    ("Настройки: модель и режим", "/settings#model", "gigachat нейросеть pro lite режим новых подключений"),
    ("Настройки: изменения базы знаний", "/settings#publish", "публикация версии"),
    ("Настройки: когда бот отвечает сам", "/settings#auto", "расписание рабочее время автоответ"),
    ("Настройки: защита от лишних трат", "/settings#limits", "лимит обращений в день в час"),
    ("Настройки: вечерняя сводка", "/settings#summary", "итоги дня время"),
    ("Настройки: тексты бота", "/settings#texts", "обращение вы ты подпись приветствие уточню и вернусь"),
    ("Настройки: паузы", "/settings#pauses", "менеджер пишет сам молчит"),
    ("Внедрение", "/onboarding", "мастер запуск бизнес"),
]
# Итог ответа клиенту с сайта (коды — Services.decide_draft): показываем над перепиской
DRAFT_RESULTS = {
    "sent": "Отправлено клиенту.",
    "skipped": "Черновик пропущен: клиенту ничего не ушло.",
    "handled": "Этот вопрос уже обработан — возможно, вы ответили в Telegram.",
    "no_conn": "Подключение не найдено или не разрешено.",
    "off": "Бот выключен: ответьте клиенту в его чате.",
    "observe": "Включено наблюдение: бот не пишет клиентам, ответьте в его чате.",
    "empty": "Напишите текст ответа.",
    "no_channel": "Бот сейчас не подключён к мессенджеру, поэтому отправить с сайта нельзя. Ответьте в чате.",
    "cant_reply": "У бота нет права отвечать: Telegram → Настройки → Автоматизация чатов → бот → «Отвечать на сообщения».",
    "failed": "Не отправлено: нет связи с мессенджером или клиент писал больше суток назад. Ответьте ему сами в чате.",
}
LIVE_SECONDS = 15  # раз в сколько секунд «Обзор», «Диалоги» и «Наблюдение» подтягивают новое без перезагрузки
LOGIN_TRIES, LOGIN_WINDOW = 5, 600  # не больше 5 неверных токенов за 10 минут с одного адреса

CSS = """
:root{--bg:#f5f2eb;--card:#fffdf9;--text:#14171f;--muted:#5d6270;--line:#e3ded3;--line-strong:#cbc4b5;
--accent:#c2410c;--accent-bg:#fdebe1;--ok:#15803d;--ok-bg:#e5f3e9;--warn:#a2470b;--warn-bg:#fcefdc;
--ink:#14171f;--on-ink:#f5f2eb;--on-accent:#fff;--radius:12px;--radius-sm:8px;--ease:cubic-bezier(.2,.7,.3,1)}
@media (prefers-color-scheme:dark){:root{--bg:#111419;--card:#191e28;--text:#eef0f4;--muted:#a9b0be;--line:#2a3140;
--line-strong:#3b4354;--accent:#ff7a45;--accent-bg:#3a2218;--ok:#4ade80;--ok-bg:#15301f;--warn:#fbbf24;--warn-bg:#382c11;
--ink:#eef0f4;--on-ink:#111419;--on-accent:#1b0f08}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.skip{position:absolute;left:-999px;top:8px;background:var(--ink);color:var(--on-ink);padding:8px 12px;border-radius:8px;z-index:30}
.skip:focus{left:8px}
/* меню слева: разделы с иконками и счётчиками того, что ждёт вас; на телефоне — строка сверху, листается вбок */
.shell{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh;min-height:100dvh}
.side{position:sticky;top:0;height:100vh;height:100dvh;overflow-y:auto;display:flex;flex-direction:column;gap:2px;
padding:16px 12px;background:var(--card);border-right:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;padding:4px 10px 14px;font-weight:700;font-size:14.5px;line-height:1.25;
color:var(--text);text-decoration:none}
.logo{display:grid;place-items:center;width:28px;height:28px;flex:none;border-radius:8px;background:var(--accent);
color:var(--on-accent);font-size:13px;font-weight:700}
nav{display:flex;flex-direction:column;gap:2px}
nav .grp{padding:16px 10px 6px;font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
nav a,.logout{display:flex;align-items:center;gap:10px;min-height:40px;padding:8px 10px;border-radius:var(--radius-sm);
color:var(--text);text-decoration:none;font-size:14.5px;transition:background-color .15s var(--ease),color .15s var(--ease)}
nav a::before,.logout::before{content:"";width:18px;height:18px;flex:none;background:currentColor;opacity:.7;
-webkit-mask:var(--icon) center/contain no-repeat;mask:var(--icon) center/contain no-repeat}
nav a:hover,.logout:hover{background:var(--bg)}
nav a.on{background:var(--accent-bg);color:var(--accent);font-weight:600}nav a.on::before{opacity:1}
nav .n{margin-left:auto;min-width:22px;padding:1px 7px;border-radius:99px;background:var(--warn-bg);color:var(--warn);
font-size:12px;font-weight:600;text-align:center;font-variant-numeric:tabular-nums}
.logout{color:var(--muted)}.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:6px}
.find{justify-content:flex-start;gap:10px;width:100%;min-height:38px;margin:0 0 8px;padding:6px 10px;
background:var(--bg);color:var(--muted);border:1px solid var(--line);font-size:14px;font-weight:400}
.find::before{content:"";width:16px;height:16px;flex:none;background:currentColor;-webkit-mask:var(--icon) center/contain no-repeat;
mask:var(--icon) center/contain no-repeat}
.find kbd{margin-left:auto;padding:0 5px;border:1px solid var(--line-strong);border-radius:4px;font:12px ui-monospace,Menlo,monospace}
.find:hover{opacity:1;color:var(--text);border-color:var(--line-strong)}
.notify{width:100%;min-height:38px;padding:6px 10px;background:transparent;color:var(--muted);border:1px dashed var(--line-strong);
font-size:13.5px;font-weight:400}.notify:hover{opacity:1;color:var(--text)}
main{width:100%;max-width:1160px;margin:0 auto;padding:28px 32px 56px;min-width:0}
main.wide{max-width:none}
@media(max-width:860px){.shell{display:block}
.side{z-index:20;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:0;padding:0 12px;border-right:0;
border-bottom:1px solid var(--line)}
.brand{flex:1;padding:12px 4px 6px}.side-foot{order:1;margin:0;flex-direction:row}.notify{display:none!important}
.find{order:0;width:auto;min-height:36px;margin:0 4px 0 0;padding:6px 10px}.find kbd{display:none}
.logout{min-height:36px;padding:6px 4px;font-size:14px}.logout::before{display:none}
nav{order:2;flex-direction:row;width:calc(100% + 24px);margin:0 -12px;padding:0 8px;overflow-x:auto;scrollbar-width:none;
-webkit-overflow-scrolling:touch}nav::-webkit-scrollbar{display:none}nav .grp{display:none}
nav a{flex:none;min-height:0;padding:10px 10px 12px;border-radius:0;border-bottom:2px solid transparent}
nav a::before{display:none}nav a:hover{background:none}
nav a.on{background:none;color:var(--text);border-color:var(--accent)}
main{padding:20px 16px 48px}}
h1{font-size:24px;line-height:1.25;margin:0 0 18px;letter-spacing:-.01em}h2{font-size:17px;margin:0 0 12px}
h3{font-size:15px;margin:16px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.card>:last-child{margin-bottom:0}.card>p:first-child,.card>h2:first-child{margin-top:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpis .card{margin:0;padding:14px 16px}
.kpi{font-size:28px;font-weight:700;line-height:1.15;margin-top:4px;font-variant-numeric:tabular-nums}
.muted{color:var(--muted);font-size:13px}
.cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:16px;align-items:start}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
/* таблицы */
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
td,th{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12.5px}tr:last-child td{border-bottom:0}
tbody tr:hover td,table tr:hover td{background:color-mix(in srgb,var(--bg) 60%,transparent)}
.scroll{overflow-x:auto}
@media(max-width:640px){.table-cards tr:first-child{display:none}.table-cards tr{display:block;padding:12px 0;
border-bottom:1px solid var(--line)}.table-cards td{display:block;border:0;padding:2px 0}
.table-cards td:empty{display:none}.table-cards td[data-label]::before{content:attr(data-label);display:block;font-size:12px;color:var(--muted)}
.table-cards tr:hover td{background:none}}
/* поля и кнопки: не ниже 40–44 px, видимый фокус */
textarea,input[type=text],input[type=password],input[type=email],input[type=number],input[type=search],
input[type=time],input[type=date],input[type=url],select{width:100%;min-height:42px;padding:10px 12px;
border:1px solid var(--line-strong);border-radius:var(--radius-sm);background:var(--card);color:var(--text);font:inherit;
transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
textarea:focus,input:focus,select:focus{outline:none;border-color:var(--accent);
box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
::placeholder{color:var(--muted);opacity:.8}
textarea.code{font:13px/1.55 ui-monospace,Menlo,monospace}
input[type=file]{font:inherit;font-size:14px;color:var(--muted);max-width:100%}
input[type=file]::file-selector-button{font:inherit;font-size:14px;margin-right:10px;padding:8px 14px;min-height:40px;
border:1px solid var(--line-strong);border-radius:var(--radius-sm);background:transparent;color:var(--text);cursor:pointer}
label{display:block;margin:16px 0 6px;font-weight:600}.hint{color:var(--muted);font-size:13px;margin-top:6px}
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:42px;padding:9px 18px;
background:var(--ink);color:var(--on-ink);border:1px solid var(--ink);border-radius:var(--radius-sm);font:inherit;
font-weight:500;cursor:pointer;text-decoration:none;transition:background-color .15s var(--ease),
border-color .15s var(--ease),opacity .15s var(--ease),transform .1s var(--ease)}
button:hover,.btn:hover{opacity:.88}button:active,.btn:active{transform:scale(.98)}
button.light,.btn.light{background:transparent;color:var(--text);border-color:var(--line-strong)}
button.light:hover,.btn.light:hover{opacity:1;background:var(--bg)}
button.small{min-height:34px;padding:5px 12px;font-size:13.5px}
button:disabled{opacity:.45;cursor:not-allowed;transform:none}
@media(pointer:coarse){button.small{min-height:40px}}
@media(max-width:640px){textarea,input,select{font-size:16px}main{padding:18px 12px 48px}.card{padding:16px}}
[hidden]{display:none!important}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.days{display:flex;gap:6px;flex-wrap:wrap}.days label{margin:0;font-weight:400;border:1px solid var(--line-strong);
border-radius:var(--radius-sm);padding:8px 12px;cursor:pointer;min-height:40px}.days input{margin-right:6px}
.time{display:flex;gap:10px;align-items:center}.time input{width:130px}
.ok{color:var(--ok)}.warn{color:var(--warn)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:12.5px;
white-space:nowrap;background:var(--bg)}
.pill.ok{background:var(--ok-bg);border-color:transparent;color:var(--ok)}
.pill.warn{background:var(--warn-bg);border-color:transparent;color:var(--warn)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.tabs a{display:inline-flex;align-items:center;min-height:36px;padding:6px 14px;border-radius:99px;
border:1px solid var(--line-strong);text-decoration:none;color:var(--text);font-size:14px;
transition:background-color .15s var(--ease)}
.tabs a:hover{background:var(--card)}.tabs a.on{background:var(--ink);color:var(--on-ink);border-color:var(--ink)}
@media(max-width:640px){.tabs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:0 -12px 14px;padding:0 12px}
.tabs a{flex:none}}
[id]{scroll-margin-top:20px}@media(max-width:860px){[id]{scroll-margin-top:110px}}
.status{list-style:none;margin:0;padding:0}.status li{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.status li:last-child{border:0}.dot{width:9px;height:9px;border-radius:50%;margin-top:7px;flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.bubble{background:var(--bg);border:1px solid var(--line);border-radius:14px 14px 14px 4px;padding:11px 14px;
white-space:pre-wrap;overflow-wrap:anywhere}
.note{background:var(--accent-bg);border-radius:var(--radius-sm);padding:12px 16px;margin-bottom:16px}
tr.flag td{background:var(--accent-bg)}details summary{cursor:pointer;color:var(--muted);padding:4px 0}
pre{white-space:pre-wrap;margin:0;font:inherit;overflow-wrap:anywhere}
.chat{display:flex;flex-direction:column;gap:12px}.chat .msg{max-width:85%}.chat .msg.me{align-self:flex-end}
.chat .msg .muted{margin-bottom:4px}.chat .msg.me .muted{text-align:right}
.chat .msg.me .bubble{background:var(--accent-bg);border-color:transparent;border-radius:14px 14px 4px 14px}
.chats a{display:block;padding:10px 12px;border-radius:var(--radius-sm);text-decoration:none;color:var(--text)}
.chats a:hover{background:var(--bg)}.chats a.on{background:var(--bg);font-weight:600}
.login{max-width:400px;margin:0 auto;padding-top:14vh}.login h1{font-size:20px}
.onboarding-banner,.onboarding-head{display:flex;justify-content:space-between;gap:16px;align-items:center}
.onboarding-banner{border-bottom:1px solid var(--line);padding:0 0 18px;margin:0 0 20px}
.onboarding-head{margin-bottom:24px}.onboarding-head h1{margin:0}.onboarding-head p{margin:4px 0 0}
.onboarding-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:28px;max-width:980px}
.onboarding-steps{list-style:none;margin:0;padding:0}.onboarding-steps li{margin-bottom:8px}
.onboarding-steps a{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--muted);padding:10px 0}
.onboarding-steps .current a{color:var(--text);font-weight:600}.step-mark{display:grid;place-items:center;
width:27px;height:27px;border:1px solid var(--line);border-radius:50%;font-size:12px;flex:none}
.current .step-mark{border-color:var(--accent);color:var(--accent)}.step-mark.done{color:var(--ok);border-color:var(--ok)}
.onboarding-panel{padding:26px}.onboarding-panel>p:first-child{margin-top:0}.onboarding-panel h2{font-size:23px}
.onboarding-next{margin-top:20px}.onboarding-next:empty{display:none}
.onboarding-choice{display:flex;gap:10px;align-items:baseline;font-weight:400}.onboarding-choice input{flex:none}
.onboarding-choices{display:flex;gap:8px 20px;flex-wrap:wrap}.onboarding-case{display:grid;grid-template-columns:1fr 1.5fr;gap:12px}
.onboarding-panel fieldset{border:0;border-top:1px solid var(--line);padding:12px 0 20px;margin:20px 0 0;min-width:0}
.onboarding-panel legend{padding:0 10px 0 0;font-weight:600}.onboarding-panel h3{font-size:18px}
.onboarding-result{padding:16px 0;border-top:1px solid var(--line)}.onboarding-result .bubble{margin:10px 0}
.onboarding-results,.onboarding-readiness{border-top:1px solid var(--line);padding-top:18px;margin-top:24px}
@media(max-width:680px){.onboarding-layout{grid-template-columns:1fr;gap:12px}.onboarding-steps{display:flex;
gap:8px;overflow-x:auto;padding-bottom:8px}.onboarding-steps li{flex:0 0 auto;margin:0}.onboarding-steps a{font-size:13px}
.onboarding-panel{padding:18px}.onboarding-case{grid-template-columns:1fr}.onboarding-banner{align-items:flex-start;flex-wrap:wrap}}
/* «Диалоги» и «Наблюдение» как ящик поддержки: слева разговоры, справа переписка; на телефоне — по очереди */
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin-bottom:14px}
.toolbar .tabs{margin:0}.toolbar form{display:flex;gap:8px;flex:1 1 280px;max-width:440px}
.tabs .c{opacity:.65;margin-left:6px;font-variant-numeric:tabular-nums}
.inbox{display:grid;grid-template-columns:minmax(260px,330px) minmax(0,1fr);height:max(520px,calc(100vh - 190px));
height:max(520px,calc(100dvh - 190px));background:var(--card);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.list{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line)}
.list-head{padding:12px 16px;border-bottom:1px solid var(--line);font-weight:600}
.items{position:relative;flex:1;overflow-y:auto}
.item{display:grid;grid-template-columns:36px minmax(0,1fr) auto;column-gap:10px;row-gap:1px;padding:12px 16px;
border-bottom:1px solid var(--line);color:var(--text);text-decoration:none}
.item:hover{background:var(--bg)}.item.on{background:var(--accent-bg)}
.av{grid-row:span 3;display:grid;place-items:center;width:36px;height:36px;border-radius:50%;font-size:12.5px;font-weight:650;
background:color-mix(in srgb,var(--hue) 24%,var(--card));color:var(--text)}
.item .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .tm{font-size:12px;color:var(--muted);white-space:nowrap;padding-top:2px}
.item .pv{grid-column:2/4;font-size:13.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .st{grid-column:2/4;display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}
.thread{display:flex;flex-direction:column;min-width:0;min-height:0}
.thread-head{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;padding:12px 20px;border-bottom:1px solid var(--line)}
.thread-head h2{margin:0;font-size:16px}
.thread-body{flex:1;overflow-y:auto;padding:20px 24px;background:var(--bg)}
.thread .bubble{background:var(--card)}
.thread-foot{padding:10px 20px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
.thread .chat{gap:24px}.turn{display:flex;flex-direction:column;gap:8px}
.chat .msg.me .bubble.draft{background:transparent;border:1px dashed var(--line-strong)}
.turn.flag .msg.me .bubble{box-shadow:0 0 0 2px var(--warn)}
.why{align-self:flex-end;max-width:85%;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;justify-content:flex-end;
font-size:13px;color:var(--muted);text-align:right}
.why form{margin:0}
.empty{padding:48px 24px;color:var(--muted);text-align:center}
.back{display:none;font-size:14px}
@media(max-width:860px){.inbox{grid-template-columns:1fr;height:auto}.list{border-right:0}
.items,.thread-body{overflow:visible}.inbox.open .list,.inbox:not(.open) .thread{display:none}
.back{display:inline-flex}.thread-body{padding:16px}.thread-head{padding:12px 16px}}
/* «Обзор»: неделя графиком против прошлой и три показателя с мини-графиками */
.week{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:32px}
.big{display:flex;align-items:center;flex-wrap:wrap;gap:10px;font-size:32px;font-weight:700;line-height:1.1;margin:4px 0 2px;
font-variant-numeric:tabular-nums}
.delta{padding:2px 8px;border-radius:6px;background:var(--bg);color:var(--muted);font-size:13px;font-weight:600}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.legend i{display:inline-block;width:18px;margin-right:6px;vertical-align:middle;border-top:2.4px solid var(--accent)}
.legend i.prev{border-top:2px dashed var(--muted);opacity:.7}
.chart{display:block;width:100%;height:auto;margin-top:14px}
.chart .grid{stroke:var(--line);stroke-width:1}
.chart .prev{fill:none;stroke:var(--muted);stroke-width:1.6;stroke-dasharray:5 5;opacity:.55}
.chart .area{fill:var(--accent);opacity:.1}
.chart .line{fill:none;stroke:var(--accent);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}
.chart circle{fill:var(--accent);stroke:var(--card);stroke-width:2}
.days7{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:12.5px;color:var(--muted);margin-top:6px}
.days7 b{display:block;color:var(--text);font-size:14px;font-variant-numeric:tabular-nums}
.stat{display:grid;grid-template-columns:1fr auto;align-items:end;column-gap:12px;padding:14px 0;border-bottom:1px solid var(--line)}
.stat:first-child{padding-top:0}.stat:last-child{border-bottom:0}
.stat .lbl{grid-column:1/3;font-size:13px;color:var(--muted)}
.stat .val{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}.stat .hint{grid-column:1/3;margin:2px 0 0}
.spark{width:88px;height:26px;fill:none;stroke:var(--accent);stroke-width:1.6;stroke-linejoin:round;stroke-linecap:round}
.todo{list-style:none;margin:0;padding:0}
.todo li{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;padding:10px 0;
border-bottom:1px solid var(--line)}
.todo li:last-child{border-bottom:0}
.todo a{display:flex;gap:8px;min-width:0;color:var(--text);text-decoration:none}.todo a:hover b{text-decoration:underline}
.todo b{flex:none;white-space:nowrap}
.todo .q{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media(max-width:860px){.week{grid-template-columns:1fr;gap:20px}}
/* движение: короткое и со смыслом — переходы между разделами, новое в списках, ход ожидания; без него всё на месте */
@view-transition{navigation:auto}
.side{view-transition-name:side}
::view-transition-old(root){animation:vt-out .12s ease-in both}
::view-transition-new(root){animation:vt-in .24s var(--ease) both}
@keyframes vt-out{to{opacity:0}}
@keyframes vt-in{from{opacity:0;transform:translateY(6px)}}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}
@keyframes swap{from{opacity:0;transform:translateY(4px)}}
@keyframes fade{from{opacity:0}}
@keyframes flash{from{background-color:var(--warn-bg)}}
@keyframes pop{0%{transform:scale(.6)}60%{transform:scale(1.15)}}
@keyframes draw{from{stroke-dashoffset:1}}
@keyframes spin{to{transform:rotate(1turn)}}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
@keyframes slide-in{from{opacity:0;transform:translateX(24px)}}
@keyframes slide-back{from{opacity:0;transform:translateX(-24px)}}
@keyframes grow{from{transform:scaleX(var(--from,0))}}
.item{transition:background-color .15s var(--ease)}
.item.fresh{animation:flash 1.8s var(--ease)}
.turn.fresh,.chat .msg.fresh,.todo li.fresh{animation:rise .35s var(--ease)}
.turn:target .msg.me .bubble{animation:flash 1.8s var(--ease)}
nav .n.fresh{animation:pop .4s var(--ease)}
.note{animation:rise .25s var(--ease)}
.inbox.to-thread .thread-body>.chat{animation:swap .2s var(--ease)}
@media(max-width:860px){.inbox.open.to-thread .thread{animation:slide-in .26s var(--ease)}
.inbox.to-list .list{animation:slide-back .26s var(--ease)}.inbox.to-thread .thread-body>.chat{animation:none}}
.chart .line,.spark polyline{stroke-dasharray:1;animation:draw 1s var(--ease) .15s both}
.spark polyline{animation-duration:.8s;animation-delay:.35s}
.chart .prev{animation:fade .6s ease-out .2s both}.chart .area{animation:fade .6s ease-out .6s both}
.chart circle{transform-box:fill-box;transform-origin:center;animation:pop .35s var(--ease) 1.05s both}
[data-replaced] .chart *,[data-replaced] .spark *{animation:none}
button[aria-busy="true"]{cursor:progress}button[aria-busy="true"]:disabled{opacity:.85}
button[aria-busy="true"]::before{content:"";width:14px;height:14px;flex:none;border-radius:50%;border:2px solid currentColor;
border-right-color:transparent;animation:spin .7s linear infinite}
.typing{display:inline-flex;align-items:center;gap:6px;margin-top:12px;padding:10px 14px;border:1px solid var(--line);
border-radius:14px 14px 14px 4px;background:var(--bg);color:var(--muted);font-size:14px;animation:rise .25s var(--ease)}
.typing i{width:6px;height:6px;border-radius:50%;background:currentColor;animation:blink 1.2s infinite both}
.typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s;margin-right:4px}
.progress{height:8px;margin:12px 0 4px;border-radius:99px;background:var(--bg);overflow:hidden}
.progress i{display:block;height:100%;background:var(--accent);transform-origin:left;transform:scaleX(var(--p));
animation:grow .5s var(--ease)}
.savebar{position:sticky;bottom:12px;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:12px;
margin-top:16px;padding:8px 8px 8px 18px;border-radius:var(--radius);background:var(--ink);color:var(--on-ink);
box-shadow:0 10px 28px -8px rgb(0 0 0/.35);animation:rise .2s var(--ease)}
.savebar button{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.keys{display:none;margin-left:8px;opacity:.8}@media(hover:hover) and (pointer:fine){.keys{display:inline}}
/* ответ клиенту и разбор ошибки — прямо в переписке */
.reply{align-self:flex-end;width:min(100%,560px);display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;align-items:flex-start}
.reply button,.reply summary.btn{min-height:36px;padding:6px 14px;font-size:14px}
.reply summary{list-style:none;cursor:pointer}.reply summary::-webkit-details-marker{display:none}
.reply details[open]{order:5;flex-basis:100%;text-align:right}
.reply details[open]>summary{min-height:0;padding:0 2px 6px;border:0;background:none;color:var(--muted);font-size:13px}
.reply .opened,.reply details[open] .closed{display:none}.reply details[open] .opened{display:inline}
.reply:has(details[open])>button{display:none}  /* правите текст — «Отправить черновик» убран, чтобы не нажать мимо */
.reply textarea{background:var(--card);text-align:left}.reply details button{margin-top:8px}
.review{align-self:flex-end;width:min(100%,560px);padding:4px 16px 12px;border:1px solid var(--line);border-radius:var(--radius);
background:var(--card)}
.review summary{padding:8px 0;cursor:pointer;font-weight:600;color:var(--warn)}.review[open] summary{margin-bottom:-6px}
.review label{margin-top:12px}.review p:last-child{margin-bottom:4px}
/* поиск ⌘K */
.palette{width:min(640px,calc(100vw - 32px));max-height:min(560px,calc(100dvh - 64px));margin:10vh auto auto;padding:0;
border:1px solid var(--line);border-radius:var(--radius);background:var(--card);color:var(--text);
box-shadow:0 24px 64px -16px rgb(0 0 0/.35);overflow:hidden;display:flex;flex-direction:column}
.palette:not([open]){display:none}.palette[open]{animation:pal-in .18s var(--ease)}
.palette::backdrop{background:rgb(0 0 0/.45)}
@keyframes pal-in{from{opacity:0;transform:translateY(-8px) scale(.98)}}
.palette input[type=search]{border:0;border-bottom:1px solid var(--line);border-radius:0;min-height:56px;padding:0 20px;
font-size:17px;background:var(--card)}.palette input[type=search]:focus{box-shadow:none;border-color:var(--line)}
.palette .results{overflow-y:auto;padding:6px 8px 8px;flex:1}
.palette .grp{padding:10px 12px 4px;font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.palette .hit{display:flex;flex-direction:column;gap:1px;padding:8px 12px;border-radius:var(--radius-sm);color:var(--text);
text-decoration:none}.palette .hit span{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.palette .hit.on{background:var(--accent-bg)}.palette .none{padding:18px 12px;color:var(--muted)}
.palette .keys{display:block;margin:0;padding:8px 16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}
::view-transition-group(*),::view-transition-old(*),::view-transition-new(*){animation:none!important}.typing i{opacity:.7}}
"""

NAV = [("/", "Обзор"), ("/dialogs", "Диалоги"), ("/observe", "Наблюдение"), ("/learn", "Обучение"), ("/kb", "База знаний"),
       ("/exam", "Экзамен"), ("/settings", "Настройки"), ("/onboarding", "Внедрение")]
NAV_GROUPS = {"/dialogs": "Разговоры", "/learn": "Бот"}  # подпись группы в меню перед этим пунктом
# Иконки меню: контур 16×16, рисуется маской цветом текста — одна и та же в светлой и тёмной теме
NAV_ICONS = {
    "/": '<path d="M2.5 7.2 8 2.6l5.5 4.6v6.3h-11z"/>',
    "/dialogs": '<path d="M2.5 3h11v8H7.2L4.5 13.5V11h-2z"/>',
    "/observe": '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>',
    "/learn": '<path d="M8 3 1.5 6 8 9l6.5-3zM4 7.3v3.2c1.1.9 2.5 1.4 4 1.4s2.9-.5 4-1.4V7.3"/>',
    "/kb": '<path d="M3 3.5A1.5 1.5 0 0 1 4.5 2H13v10H4.5A1.5 1.5 0 0 0 3 13.5zM3 13.5A1.5 1.5 0 0 0 4.5 15H13v-3"/>',
    "/exam": '<path d="M2.5 2.5h11v11h-11zM5.2 8.2l1.9 1.9 3.8-4.2"/>',
    "/settings": '<circle cx="8" cy="8" r="2.2"/><path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4'
                 'M3.5 12.5l1.4-1.4M11.1 4.9l1.4-1.4"/>',
    "/onboarding": '<path d="M3.5 14V2.5M3.5 3h8l-1.6 2.8 1.6 2.8h-8"/>',
    "/logout": '<path d="M6.5 2.5h-4v11h4M10.5 5l3 3-3 3M13.5 8H6"/>',
    "find": '<circle cx="7" cy="7" r="4.2"/><path d="m10.2 10.2 3.3 3.3"/>',
}


def _icon_rule(href: str, paths: str) -> str:
    selector = {"/logout": ".logout", "find": ".find"}.get(href, f'nav a[href="{href}"]')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" stroke="#000" stroke-width="1.4" '
           f'stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')
    return f'{selector}{{--icon:url("data:image/svg+xml,{quote(svg)}")}}'


CSS += "".join(_icon_rule(href, paths) for href, paths in NAV_ICONS.items())


# Поведение страниц без перезагрузки: живые блоки (data-live), переходы в ящиках, ожидание после отправки,
# Enter в проверке бота, клавиши, напоминание о несохранённых настройках. Без JavaScript всё работает ссылками и формами.
JS = r"""(() => {
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const narrow = () => matchMedia("(max-width: 860px)").matches;
const typing = () => document.activeElement?.matches("main input, main textarea, main select");
// Плавный переход между разделами браузер пропускает, если быстро нажать ещё раз, — это не ошибка
for (const type of ["pageswap", "pagereveal"]) addEventListener(type, e => e.viewTransition?.ready.catch(() => {}));

function toLatest(root = document) {
  // Переписка открывается на последних сообщениях, а ссылка с #d12 — на нужном ответе
  $$(".thread-body", root).forEach(e => e.scrollTop = e.scrollHeight);
  const target = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
  if (target && target.closest(".thread-body")) target.scrollIntoView({block: "center"});
}
function settle() {
  $("nav .on")?.scrollIntoView({inline: "center", block: "nearest"});
  toLatest();
  const on = $(".items .on");
  if (on) on.parentElement.scrollTop = on.offsetTop - 60;
}
// Начатый ответ или открытый разбор живое обновление не трогает
const changed = el => $$("input, textarea, select", el).some(f =>
  f.type === "checkbox" || f.type === "radio" ? f.checked !== f.defaultChecked
  : f.tagName === "SELECT" ? [...f.options].some(o => o.selected !== o.defaultSelected)
  : f.value !== f.defaultValue) || !!$("details[data-opened]", el);
document.addEventListener("toggle", e => {
  if (e.target.matches?.("main details")) e.target.toggleAttribute("data-opened", e.target.open);
}, true);
async function load(url) {
  const res = await fetch(url, {credentials: "same-origin"});
  if (res.status === 401 || res.status === 403) throw new Error("auth");
  if (!res.ok || new URL(res.url).pathname !== new URL(url, location.href).pathname) throw new Error("other");
  return new DOMParser().parseFromString(await res.text(), "text/html");
}
function markFresh(old, next) {
  const seen = new Set($$("[data-key]", old).map(e => e.dataset.key));
  $$("[data-key]", next).forEach(e => { if (!seen.has(e.dataset.key)) e.classList.add("fresh"); });
}
function keep(el) {
  const items = $(".items", el), body = $(".thread-body", el);
  return {items: items?.scrollTop, body: body?.scrollTop,
          bottom: !body || body.scrollHeight - body.scrollTop - body.clientHeight < 60};
}
function restore(el, s) {
  const items = $(".items", el), body = $(".thread-body", el);
  if (items && s.items != null) items.scrollTop = s.items;
  if (body) body.scrollTop = s.bottom ? body.scrollHeight : s.body;
}
function swapNav(doc) {
  const old = $("nav"), next = $("nav", doc);
  if (!old || !next || old.innerHTML === next.innerHTML) return;
  markFresh(old, next);
  old.replaceWith(document.adoptNode(next));
}

// Живое обновление: новые разговоры, сообщения и счётчики появляются сами, пока вкладка открыта
let timer, polling = false;
function schedule() {
  clearTimeout(timer);
  const s = Number($("main")?.dataset.poll);
  if (s) timer = setTimeout(poll, s * 1000);
}
async function poll() {
  if (polling) return;
  if (document.hidden || typing()) return schedule();
  polling = true;
  const url = location.href;
  let doc;
  try { doc = await load(url); } catch (e) { polling = false; if (e.message !== "auth") schedule(); return; }
  polling = false;
  if (url !== location.href || typing()) return schedule();
  const main = $("main"), next = $("main", doc);
  if (!main || !next) return;
  swapNav(doc);
  const keys = el => $$("[data-live]", el).map(e => e.dataset.live).join();
  if (!next.dataset.poll || keys(main) !== keys(next)) {  // экзамен закончился или появился новый блок
    if (changed(main)) return schedule();
    main.replaceWith(document.adoptNode(next));
    settle();
    return schedule();
  }
  for (const n of $$("[data-live]", next)) {
    const o = $(`[data-live="${n.dataset.live}"]`, main);
    if (!o || o.innerHTML === n.innerHTML || changed(o)) continue;
    const s = keep(o), bar = $("[data-progress]", o), nbar = $("[data-progress]", n);
    if (bar && nbar) nbar.style.setProperty("--from", bar.dataset.progress);
    markFresh(o, n);
    n.setAttribute("data-replaced", "");
    o.replaceWith(document.adoptNode(n));
    restore(n, s);
  }
  schedule();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && $("main")?.dataset.poll) poll(); });
document.addEventListener("animationend", e => e.target.classList?.remove("fresh"));

// Ящики «Диалогов» и «Наблюдения»: разговор открывается без перезагрузки, список остаётся на месте
async function go(url, push = true) {
  let doc;
  try { doc = await load(url); } catch { location.href = url; return; }
  const main = $("main"), next = $("main", doc);
  if (!main || !next) { location.href = url; return; }
  const was = $(".inbox")?.classList.contains("open"), s = keep(main), focused = main.contains(document.activeElement);
  if (push) history.pushState(null, "", url);
  document.title = titled(doc.title);
  swapNav(doc);
  main.replaceWith(document.adoptNode(next));
  const inbox = $(".inbox");
  if (inbox) {
    const items = $(".items", inbox), on = $(".items .on", inbox);
    if (items && s.items != null) items.scrollTop = s.items;
    if (on && (on.offsetTop < items.scrollTop || on.offsetTop + on.offsetHeight > items.scrollTop + items.clientHeight))
      items.scrollTop = on.offsetTop - 60;
    toLatest(inbox);
    const open = inbox.classList.contains("open");
    inbox.classList.add(open ? "to-thread" : "to-list");
    if (narrow() && was !== open) scrollTo({top: Math.max(0, inbox.getBoundingClientRect().top + scrollY - 120)});
    if (focused) (on || $("h1"))?.focus({preventScroll: true});
  }
  schedule();
}
document.addEventListener("click", e => {
  const a = e.target.closest("a[href]");
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (!a.closest(".inbox, .toolbar") || a.origin !== location.origin || a.pathname !== location.pathname) return;
  e.preventDefault();
  go(a.href);
});
addEventListener("popstate", () => go(location.href, false));

// Отправка форм: поиск — без перезагрузки, остальное — с ожиданием на кнопке и без двойного нажатия
document.addEventListener("submit", e => {
  const form = e.target;
  if (e.defaultPrevented) return;
  if (form.method === "get" && form.closest(".toolbar")) {
    const url = new URL(form.action);
    if (url.pathname === location.pathname) {
      e.preventDefault();
      url.search = new URLSearchParams(new FormData(form));
      go(url.href);
    }
    return;
  }
  if (form.method !== "post") return;
  if (form.dataset.sending) { e.preventDefault(); return; }
  form.dataset.sending = "1";
  const button = e.submitter || $("button:not([type=button])", form);
  if (button) {
    button.setAttribute("aria-busy", "true");
    setTimeout(() => { button.disabled = true; });
  }
  if (form.hasAttribute("data-typing") && !e.submitter?.hasAttribute("formaction"))
    form.insertAdjacentHTML("afterend", '<div class="typing" role="status"><i></i><i></i><i></i>Бот думает…</div>');
});
addEventListener("pageshow", e => {  // вернулись кнопкой «Назад» — кнопки снова доступны
  if (!e.persisted) return;
  $$("[aria-busy=true]").forEach(b => { b.removeAttribute("aria-busy"); b.disabled = false; });
  $$("form[data-sending]").forEach(f => delete f.dataset.sending);
  $$(".typing").forEach(t => t.remove());
});

// Клавиши: ⌘K — поиск по всему, Enter — спросить бота, ⌘Enter — отправить свой ответ клиенту,
// / — поиск на странице, ↑ ↓ — соседний разговор, Esc — к списку на телефоне
document.addEventListener("keydown", e => {
  const t = e.target;
  if ((e.metaKey || e.ctrlKey) && e.code === "KeyK") {
    e.preventDefault();
    palette.open ? palette.close() : openPalette();
    return;
  }
  if (t.matches?.(".reply textarea") && e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    t.form.requestSubmit($("[value=edit]", t.form));
    return;
  }
  if (t.matches?.("textarea[data-enter-send]") && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (t.value.trim()) t.form.requestSubmit();
    return;
  }
  if (t.closest?.("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") {
    const search = $("main input[type=search]");
    if (search) { e.preventDefault(); search.focus(); search.select(); }
  } else if (["ArrowDown", "ArrowUp", "j", "k"].includes(e.key) && $(".items")) {
    const items = $$(".items .item"), i = items.findIndex(x => x.classList.contains("on"));
    const next = items[i + (e.key === "ArrowDown" || e.key === "j" ? 1 : -1)];
    if (next) { e.preventDefault(); go(next.href); }
  } else if (e.key === "Escape") {
    const back = $(".inbox.open .back");
    if (back && narrow()) go(back.href);
  }
});

// Настройки: изменили и не сохранили — внизу появляется полоса «Сохранить», уйти без неё браузер не даст молча
$$("form[data-dirty]").forEach(form => {
  const bar = document.createElement("div");
  bar.className = "savebar";
  bar.hidden = true;
  bar.setAttribute("role", "status");
  bar.innerHTML = '<span>Есть несохранённые изменения</span><button type="button">Сохранить</button>';
  bar.querySelector("button").addEventListener("click", () => form.requestSubmit());
  form.append(bar);
  const state = () => new URLSearchParams(new FormData(form)).toString(), start = state();
  const check = () => { bar.hidden = state() === start; };
  form.addEventListener("input", check);
  form.addEventListener("change", check);
  addEventListener("beforeunload", e => { if (!bar.hidden && !form.dataset.sending) e.preventDefault(); });
});

// Уведомления: бот передал вопрос или подготовил черновик — всплывает уведомление браузера,
// а в заголовке вкладки видно, сколько вопросов ждёт
let waiting = 0, seen = null;
const titled = text => (waiting ? `(${waiting}) ` : "") + text.replace(/^\(\d+\) /, "");
try { const raw = sessionStorage.getItem("seen-waiting"); seen = raw ? new Set(JSON.parse(raw)) : null; } catch {}
async function watchWaiting() {
  try {
    const res = await fetch("/live/waiting", {credentials: "same-origin"});
    if (res.status === 401) return;
    const {items} = await res.json();
    waiting = items.length;
    document.title = titled(document.title);
    const fresh = seen ? items.filter(i => !seen.has(i.id)) : [];
    seen = new Set(items.map(i => i.id));
    try { sessionStorage.setItem("seen-waiting", JSON.stringify([...seen])); } catch {}
    if (fresh.length && "Notification" in window && Notification.permission === "granted"
        && (document.hidden || !document.hasFocus()))
      for (const i of fresh.slice(0, 3)) {
        const n = new Notification(i.kind === "Черновик" ? "Черновик ждёт решения" : "Бот передал вопрос вам",
                                   {body: `${i.name}: ${i.text}`, tag: `d${i.id}`});
        n.onclick = () => { window.focus(); location.href = i.url; n.close(); };
      }
  } catch {}
  setTimeout(watchWaiting, 30000);
}
const notify = $(".notify");
if (notify && "Notification" in window && Notification.permission === "default") {
  notify.hidden = false;
  notify.addEventListener("click", async () => {
    await Notification.requestPermission();
    notify.hidden = Notification.permission !== "default";
  });
}

// Поиск ⌘K: разделы и настройки, разговоры, переписка и база знаний — с клавиатуры, без мыши
const palette = document.createElement("dialog");
palette.className = "palette";
palette.setAttribute("aria-label", "Поиск по админке");
palette.innerHTML = '<input type="search" placeholder="Разговор, цена, раздел или настройка" aria-label="Что найти" '
  + 'autocomplete="off"><div class="results" role="listbox"></div>'
  + '<p class="keys">↑ ↓ — выбрать · Enter — открыть · Esc — закрыть</p>';
document.body.append(palette);
const field = $("input", palette), results = $(".results", palette);
let hits = [], current = 0, asked = null, findTimer;
function mark(i) {
  hits.forEach((h, j) => h.classList.toggle("on", j === i));
  current = i;
  hits[i]?.scrollIntoView({block: "nearest"});
}
async function find() {
  const q = field.value.trim();
  if (q === asked) return;
  asked = q;
  let data;
  try { data = await (await fetch("/search?" + new URLSearchParams({q}), {credentials: "same-origin"})).json(); }
  catch { asked = null; return; }
  if (q !== field.value.trim()) return;  // пока ждали ответ, набрали дальше
  results.replaceChildren();
  for (const group of data.groups) {
    const head = document.createElement("div");
    head.className = "grp";
    head.textContent = group.title;
    results.append(head);
    for (const item of group.items) {  // текст из переписки — только textContent, без разметки
      const a = document.createElement("a"), b = document.createElement("b");
      a.className = "hit";
      a.href = item.url;
      a.setAttribute("role", "option");
      b.textContent = item.title;
      a.append(b);
      if (item.sub) { const s = document.createElement("span"); s.textContent = item.sub; a.append(s); }
      results.append(a);
    }
  }
  hits = $$(".hit", results);
  if (!hits.length) results.innerHTML = '<div class="none">Ничего не нашлось</div>';
  mark(0);
}
function openPalette() {
  if (!palette.open) palette.showModal();
  field.select();
  find();
}
field.addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(find, 150); });
field.addEventListener("keydown", e => {
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && hits.length) {
    e.preventDefault();
    mark((current + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length);
  } else if (e.key === "Enter" && hits[current]) {
    e.preventDefault();
    hits[current].click();
  }
});
palette.addEventListener("click", e => { if (e.target === palette || e.target.closest(".hit")) palette.close(); });
$$("[data-palette]").forEach(b => b.addEventListener("click", openPalette));
if (!/mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform))
  $$("[data-palette] kbd").forEach(k => k.textContent = "Ctrl K");

settle();
schedule();
watchWaiting();
})();"""


def render_page(title: str, body: str, active: str = "/", refresh: int | None = None, *,
                wide: bool = False, badges: dict[str, int] | None = None, poll: int | None = None) -> HTMLResponse:
    """Страница админки: меню слева, справа раздел. wide — для ящиков «Диалогов» и «Наблюдения» во всю ширину.
    poll — раз в сколько секунд скрипт подтягивает блоки с data-live (новые сообщения, счётчики) без перезагрузки;
    refresh — то же для страниц, которые меняются целиком (экзамен), а без JavaScript — обычное обновление."""
    current = ' class="on" aria-current="page"'
    links = []
    for href, name in NAV:
        if href in NAV_GROUPS:
            links.append(f'<span class="grp">{NAV_GROUPS[href]}</span>')
        count = (badges or {}).get(href)
        badge = f'<span class="n" data-key="{href}:{count}" title="ждут вас">{count}</span>' if count else ""
        links.append(f'<a href="{href}"{current if href == active else ""}>{name}{badge}</a>')
    meta = f'<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>' if refresh else ""
    poll = poll or refresh
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>{escape(title)}</title>
<style>{CSS}</style></head><body><a class="skip" href="#main">К содержимому</a><div class="shell"><aside class="side">
<a class="brand" href="/"><span class="logo" aria-hidden="true">ИИ</span>Помощник менеджера</a>
<button type="button" class="find" data-palette aria-keyshortcuts="Meta+K Control+K">Поиск<kbd>⌘K</kbd></button>
<nav aria-label="Разделы" data-live="nav">{"".join(links)}</nav>
<div class="side-foot"><button type="button" class="notify" hidden>Уведомлять о новых вопросах</button>
<a class="logout" href="/logout">Выйти</a></div></aside>
<main id="main"{' class="wide"' if wide else ""}{f' data-poll="{poll}"' if poll else ""}>{body}</main></div>
<script>{JS}</script></body></html>""")


def nav_badges(services: Services) -> dict[str, int]:
    """Счётчики в меню: черновики ждут решения, вопросы без ответа и факты из переписки ждут проверки."""
    drafts = sum(r["status"] == D.DRAFT_PENDING for r in services.db.attention(limit=100))
    learn = len(group_questions(services.db.to_learn())) + len(services.db.candidates())
    return {"/dialogs": drafts, "/learn": learn}


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(24, "стоп-тема", "стоп-темы", "стоп-тем") → «24 стоп-темы»."""
    form = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {form}"


def login_page(msg: str = "", status: int = 401, with_password: bool = False) -> HTMLResponse:
    if with_password:
        fields = """<label for="l">Логин</label>
<input id="l" type="text" name="login" autocomplete="username" required autofocus>
<label for="p">Пароль</label><input id="p" type="password" name="password" autocomplete="current-password" required>
<p class="hint">Сменить: .venv/bin/python -m app.password</p>"""
    else:
        fields = """<label for="t">Токен админки</label>
<input id="t" type="text" name="token" autocomplete="current-password" required autofocus>
<p class="hint">Он записан в файле .env в строке ADMIN_TOKEN. Вместо токена можно задать логин и пароль:
.venv/bin/python -m app.password</p>"""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход</title><style>{CSS}</style></head>
<body><main class="login"><div class="card"><h1>ИИ-помощник менеджера</h1>
{note(msg)}<form method="post" action="/login">{fields}<p><button>Войти</button></p></form></div></main>
</body></html>""", status_code=status)


def note(msg: str) -> str:
    return f'<div class="note">{escape(msg)}</div>' if msg else ""


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="AI Sales Assistant admin", docs_url=None, redoc_url=None)
    exam: dict = {"running": False, "done": 0, "total": 0, "error": ""}
    onboarding_lock = asyncio.Lock()

    def page(title: str, body: str, active: str = "/", refresh: int | None = None, *,
             wide: bool = False, poll: int | None = None) -> HTMLResponse:
        return render_page(title, body, active, refresh, wide=wide, badges=nav_badges(services), poll=poll)

    failures: dict[str, list[float]] = {}

    def locked(ip: str) -> bool:
        recent = [t for t in failures.get(ip, []) if time.time() - t < LOGIN_WINDOW]
        failures[ip] = recent
        return len(recent) >= LOGIN_TRIES

    def auth(request: Request) -> None:
        """Только cookie после входа. Токен в адресе страницы оседает в истории браузера и журналах,
        поэтому ссылка с ?token= лишь входит и сразу уводит на адрес без него (`token_from_link`)."""
        ip = request.client.host if request.client else "?"
        if locked(ip):
            raise HTTPException(429, "Слишком много неверных попыток. Подождите 10 минут.")
        if not secrets.compare_digest(request.cookies.get("admin_token") or "", services.settings.admin_token):
            # Старая cookie после смены ADMIN_TOKEN не попытка подбора: просто покажем форму входа
            raise HTTPException(401, "Войдите: токен — строка ADMIN_TOKEN в файле .env")

    with_password = bool(services.settings.admin_login and services.settings.admin_password_hash)

    @app.exception_handler(StarletteHTTPException)
    async def login_on_401(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return login_page(with_password=with_password)
        if exc.status_code == 429:
            return login_page(exc.detail, status=429, with_password=with_password)
        return await http_exception_handler(request, exc)

    @app.post("/login")
    async def do_login(request: Request, token: str = Form(""), login: str = Form(""),
                       password: str = Form("")) -> Response:
        ip, st = (request.client.host if request.client else "?"), services.settings
        if locked(ip):
            return login_page("Слишком много неверных попыток. Подождите 10 минут.", status=429,
                              with_password=with_password)
        if with_password and not token:
            # Сначала проверяем пароль, потом логин: на неверный логин уходит то же время, что и на пароль
            ok = verify_password(password, st.admin_password_hash) & secrets.compare_digest(login.strip(), st.admin_login)
            error = "Неверный логин или пароль."
        else:
            ok, error = secrets.compare_digest(token.strip(), st.admin_token), "Неверный токен."
        if not ok:
            failures.setdefault(ip, []).append(time.time())
            return login_page(error, with_password=with_password)
        # Сессия — та же cookie, что и при входе по токену: проверка доступа не меняется
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("admin_token", st.admin_token, httponly=True, samesite="lax")
        return response

    @app.get("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("admin_token")
        return response

    @app.middleware("http")
    async def token_from_link(request: Request, call_next):
        """Старая ссылка вида /?token=…: вход и переход на тот же адрес без токена. Для POST токен
        в адресе не принимаем — там всегда есть форма входа."""
        token = request.query_params.get("token")
        if token is None:
            return await call_next(request)
        rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "token"])
        clean = request.url.path + (f"?{rest}" if rest else "")
        ip = request.client.host if request.client else "?"
        if request.method != "GET" or locked(ip):
            return login_page("Слишком много неверных попыток. Подождите 10 минут." if locked(ip) else "",
                              status=429 if locked(ip) else 401, with_password=with_password)
        if not secrets.compare_digest(token, services.settings.admin_token):
            failures.setdefault(ip, []).append(time.time())
            return login_page("Неверный токен в ссылке.", with_password=with_password)
        response = RedirectResponse(clean, status_code=303)
        response.set_cookie("admin_token", token, httponly=True, samesite="lax")
        return response

    @app.get("/health")
    async def health() -> Response:
        """Для мониторинга на сервере: без токена, без подробностей."""
        services.db.conn.execute("SELECT 1").fetchone()
        return Response('{"ok": true}', media_type="application/json")

    # --- внедрение бизнеса ---
    def onboarding_page(step: str = "", msg: str = "", form: dict | None = None) -> HTMLResponse:
        return page("Внедрение в бизнес", onboarding.render(services, step, msg, form), "/onboarding")

    @app.get("/onboarding", dependencies=[Depends(auth)])
    async def onboarding_start(step: str = "") -> HTMLResponse:
        return onboarding_page(step)

    @app.post("/onboarding/channel", dependencies=[Depends(auth)])
    async def onboarding_channel(business_name: str = Form(""), connection_id: str = Form("")) -> Response:
        name = business_name.strip()
        values = {"business_name": name, "connection_id": connection_id}
        if not name or len(name) > 120:
            return onboarding_page("channel", "Название бизнеса — от 1 до 120 символов.", values)
        if connection_id and not any(c.id == connection_id and c.enabled and c.approved for c in services.db.connections()):
            return onboarding_page("channel", "Подключение недоступно. Проверьте его на «Обзоре».", values)
        previous = onboarding.load(services)
        if any(previous.get(k) != v for k, v in values.items()):
            values.update(policy_review="", knowledge_review="", report={})
        onboarding.save(services, **values)
        return RedirectResponse("/onboarding", status_code=303)

    @app.post("/onboarding/source", dependencies=[Depends(auth)])
    async def onboarding_source(source: str = Form("")) -> Response:
        if source not in onboarding.SOURCES:
            return onboarding_page("source", "Выберите документы, экспорт переписки или наблюдение.")
        onboarding.save(services, source=source)
        return RedirectResponse("/onboarding?step=source", status_code=303)

    @app.post("/onboarding/policy", dependencies=[Depends(auth)])
    async def onboarding_policy(target_mode: str = Form("draft"), stop_topics: str = Form(""),
                                holding_message: str = Form("")) -> Response:
        values = {"target_mode": target_mode, "stop_topics": stop_topics, "holding_message": holding_message}
        _, errors = validate({"holding_message": holding_message})
        if target_mode not in onboarding.MODES:
            errors.append("Выберите режим запуска.")
        if len(stop_topics) > 10000 or not parse_stop_topics(stop_topics):
            errors.append("Укажите темы для менеджера, до 10 000 символов.")
        if errors:
            return onboarding_page("policy", "Не сохранено: " + " ".join(errors), values)
        await services.update_settings({"holding_message": holding_message})
        path = services.settings.kb_dir / "stop_topics.txt"
        text = stop_topics.strip() + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        onboarding.save(services, target_mode=target_mode)
        onboarding.save(services, policy_review=onboarding.policy_context(services, onboarding.load(services)))
        return RedirectResponse("/onboarding?step=knowledge", status_code=303)

    @app.post("/onboarding/knowledge", dependencies=[Depends(auth)])
    async def onboarding_knowledge(reviewed: bool = Form(False), context: str = Form("")) -> Response:
        if not reviewed or services.draft_changed() or not (services.kb.prices or services.kb.faq):
            return onboarding_page("knowledge", "Сначала проверьте и опубликуйте непустую базу, затем подтвердите сверку.")
        if context != onboarding.knowledge_context(services):
            return onboarding_page("knowledge", "Версия базы изменилась. Проверьте её ещё раз.")
        onboarding.save(services, knowledge_review=context)
        return RedirectResponse("/onboarding?step=check", status_code=303)

    @app.post("/onboarding/check", dependencies=[Depends(auth)])
    async def onboarding_check(request: Request) -> HTMLResponse:
        form = await request.form()
        try:
            cases = onboarding.cases_from_form(form)
        except ValueError as exc:
            return onboarding_page("check", str(exc), {"cases": onboarding.case_values(form)})
        state = onboarding.progress(services)
        if not all(s.done for s in state.steps if s.key in ("policy", "knowledge")):
            return onboarding_page("check", "Сначала задайте границы и подтвердите опубликованные знания.", {"cases": cases})
        if onboarding_lock.locked():
            raise HTTPException(409, "Проверка уже идёт. Дождитесь результата.")
        async with onboarding_lock:
            await onboarding.run_check(services, cases)
        return onboarding_page("check")

    @app.post("/onboarding/review", dependencies=[Depends(auth)])
    async def onboarding_review(reviewed: bool = Form(False), context: str = Form(""),
                                report_id: str = Form("")) -> HTMLResponse:
        state = onboarding.progress(services)
        if (not reviewed or not state.report_current or not state.report.get("passed")
                or context != state.report.get("context") or not report_id or report_id != state.report.get("id")):
            return onboarding_page("check", "Нужна успешная проверка текущей базы и сверка её результатов.")
        onboarding.save(services, report={**state.report, "reviewed": True})
        return onboarding_page("check", "Сверка сохранена. Режим подключения не изменился.")

    def upload_result(msg: str, back: str) -> HTMLResponse:
        return onboarding_page("source", msg) if back == "onboarding" else page("База знаний", kb_html(services, msg), "/kb")

    # --- обзор и тестовый чат ---
    @app.get("/", dependencies=[Depends(auth)])
    async def dashboard() -> HTMLResponse:
        return page("Обзор", dashboard_html(services), poll=LIVE_SECONDS)

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
            verdict = f'<span class="warn">Передаст менеджеру: {escape(human_reason(reply.reason))}</span>'  # nosemgrep: raw-html-format
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
        mode = D.MODE_ALIASES.get(mode, mode)
        # В MAX клиенты пишут боту напрямую: если он молчит, им никто не ответит — тихих режимов там нет
        if mode in D.MODES and not (mode in D.SILENT_MODES and connection_id.startswith("max:")):
            services.db.set_mode(mode, connection_id=connection_id)
        return RedirectResponse("/", status_code=303)

    # --- журнал ---
    @app.get("/dialogs", dependencies=[Depends(auth)])
    async def dialogs(f: str = "", q: str = "", conn: str = "", chat: str = "", done: str = "") -> Response:
        if f in ("observe", "watch"):  # раньше наблюдение жило вкладками «Диалогов»
            return RedirectResponse("/observe" + (f"?{urlencode({'q': q})}" if q else ""), status_code=303)
        return page("Диалоги", dialogs_html(services, f, q, conn, chat, DRAFT_RESULTS.get(done, "")), "/dialogs",
                    wide=True, poll=LIVE_SECONDS)

    @app.get("/observe", dependencies=[Depends(auth)])
    async def observe_page(conn: str = "", chat: str = "", q: str = "") -> HTMLResponse:
        return page("Наблюдение", observe_html(services, conn, chat, q), "/observe", wide=True, poll=LIVE_SECONDS)

    @app.post("/dialogs/{dialog_id}/flag", dependencies=[Depends(auth)])
    async def flag(dialog_id: int, flagged: int = Form(...), back: str = Form("/dialogs")) -> RedirectResponse:
        if not services.db.get_dialog(dialog_id):
            raise HTTPException(404, "Диалог не найден.")
        services.db.flag_dialog(dialog_id, bool(flagged))
        back = back if back.startswith("/dialogs") else "/dialogs"
        # Разбор открывается прямо под ответом в переписке; отдельная страница осталась для старых ссылок
        return RedirectResponse(f"{back}#d{dialog_id}", status_code=303)

    @app.get("/live/waiting", dependencies=[Depends(auth)])
    async def live_waiting() -> Response:
        """Что ждёт менеджера — для уведомлений браузера и числа в заголовке вкладки."""
        return Response(json.dumps(waiting_json(services), ensure_ascii=False), media_type="application/json")

    @app.get("/search", dependencies=[Depends(auth)])
    async def search(q: str = "") -> Response:
        """Поиск ⌘K: разделы и настройки, разговоры, переписка наблюдения и база знаний."""
        return Response(json.dumps(search_json(services, q[:200]), ensure_ascii=False), media_type="application/json")

    @app.post("/dialogs/{dialog_id}/draft", dependencies=[Depends(auth)])
    async def draft_decision(dialog_id: int, action: str = Form(...), text: str = Form(""),
                             back: str = Form("/dialogs")) -> RedirectResponse:
        """Ответ клиенту с сайта: отправить черновик, свой вариант или пропустить — как кнопки в Telegram."""
        result = await services.decide_draft(dialog_id, action, text[:4096])
        back = back if back.startswith("/dialogs") else "/dialogs"
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}done={result}#d{dialog_id}", status_code=303)

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
    async def candidates_approve(ids: str = Form(...), question: str = Form(...), answer: str = Form(...),
                                 source_reviewed: bool = Form(False)) -> HTMLResponse:
        parts = [i.strip() for i in ids.split(",")]
        id_list = [int(i) for i in parts] if all(re.fullmatch(r"[0-9]+", i) for i in parts) else []
        ok, msg = services.approve_candidates(id_list, question, answer, source_reviewed=source_reviewed)
        if not ok:
            return page("Обучение", learn_html(services, msg, candidate_edit=(id_list, question, answer)), "/learn")
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
    async def notes_upload(files: list[UploadFile] = File(...), back: str = Form("")) -> HTMLResponse:
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
        return upload_result(msg, back)

    @app.post("/kb/upload", dependencies=[Depends(auth)])
    async def kb_upload(faq: UploadFile | None = File(None), prices: UploadFile | None = File(None),
                        stop_topics: UploadFile | None = File(None), back: str = Form("")) -> HTMLResponse:
        files = {"faq.md": (faq, parse_faq), "prices.csv": (prices, parse_prices),
                 "stop_topics.txt": (stop_topics, parse_stop_topics)}
        prepared = {}
        for name, (upload, parser) in files.items():
            if upload is None or not upload.filename:
                continue
            try:
                text = (await upload.read()).decode("utf-8-sig")
            except UnicodeDecodeError:
                return upload_result(f"Файл {name}: нужна кодировка UTF-8.", back)
            if not parser(text):
                return upload_result(f"Файл {name}: не удалось разобрать, проверьте формат.", back)
            prepared[name] = text
        saved = []
        for name, text in prepared.items():
            services.settings.kb_dir.mkdir(parents=True, exist_ok=True)
            (services.settings.kb_dir / name).write_text(text, encoding="utf-8")
            saved.append(name)
        msg = (f"Загружено: {', '.join(saved)}. {services.kb_changed('Загружены файлы: ' + ', '.join(saved))}"
               if saved else "Файлы не выбраны.")
        return upload_result(msg, back)

    @app.post("/kb/publish", dependencies=[Depends(auth)])
    async def kb_publish(force: str = Form(""), back: str = Form("")) -> HTMLResponse:
        version, report = services.publish("Публикация из админки", force=bool(force))
        msg = (f"Опубликована версия {version} ({report.summary()}) — бот уже отвечает по ней." if version
               else f"Не опубликовано: {report.reason()}.")
        return onboarding_page("knowledge", msg) if back == "onboarding" else page("База знаний", kb_html(services, msg), "/kb")

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
    async def import_chats(files: list[UploadFile] = File(...), me: str = Form(""), back: str = Form("")) -> HTMLResponse:
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
        return upload_result(msg, back)

    # --- переписка из режима наблюдения: файл на этом компьютере ---
    @app.get("/watch/file", dependencies=[Depends(auth)])
    async def watch_file() -> Response:
        if not services.watch_log.exists():
            raise HTTPException(404, "Переписки из наблюдения пока нет")
        return Response(services.watch_log.read_bytes(), media_type="application/jsonl; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=watch-messages.jsonl"})

    @app.post("/watch/import", dependencies=[Depends(auth)])
    async def watch_import() -> HTMLResponse:
        """Черновик базы и «как было до бота» из переписки наблюдения — тем же импортом, что и выгрузка Telegram."""
        from tools.import_telegram import run_import
        if not services.watch_log.exists():
            return page("Наблюдение", observe_html(services, msg="Переписки из наблюдения пока нет."), "/observe", wide=True)
        try:
            r = await asyncio.to_thread(run_import, [services.watch_log], None, import_dir(services))
            msg = f"Черновик собран из наблюдения: чатов {r['chats']}, пар «вопрос — ответ» {r['pairs']}."
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            msg = f"Не получилось разобрать переписку наблюдения: {exc}"
        return page("Наблюдение", observe_html(services, msg=msg), "/observe", wide=True)

    @app.post("/watch/clear", dependencies=[Depends(auth)])
    async def watch_clear() -> HTMLResponse:
        services.watch_log.unlink(missing_ok=True)
        return page("Наблюдение", observe_html(services, msg="Файл переписки удалён. Журнал сообщений на этой странице остался."),
                    "/observe", wide=True)

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


def observe_html(services: Services, conn_id: str = "", chat_id: str = "", q: str = "", msg: str = "") -> str:
    """Раздел «Наблюдение»: включено ли, главные числа, чаты и переписка по порядку с временем ответа менеджера."""
    conns = [c for c in services.db.connections() if c.approved and not c.id.startswith("max:")]
    if any(c.mode == D.OBSERVE for c in conns):
        state = ('<p class="ok">Идёт наблюдение: бот молчит, сохраняет переписку и замеряет, через сколько вы отвечаете. '
                 "Клиентам ничего не отправляется.</p>")
    else:
        state = ('<p class="muted">Сейчас наблюдение выключено. Включить: «Обзор» → «Подключения» → «Наблюдение» '
                 "или /mode observe в чате с ботом.</p>")
    stats, waits = services.db.observation_stats(), services.watch_stats() or {}
    median = waits.get("median_wait_min")
    kpis = f"""<div class="kpis" data-live="kpis">
<div class="card"><div class="muted">Сообщений</div><div class="kpi">{stats['messages']}</div></div>
<div class="card"><div class="muted">Чатов</div><div class="kpi">{stats['chats']}</div></div>
<div class="card"><div class="muted">Обычно вы отвечаете через</div><div class="kpi">{minutes_text(median) if median is not None else "—"}</div></div>
<div class="card"><div class="muted">Кандидатов в базу</div><div class="kpi"><a href="/learn">{stats['candidates']}</a></div></div></div>"""
    search = f"""<div class="toolbar"><form method="get" action="/observe" role="search">
<input type="search" name="q" value="{escape(q)}" placeholder="Поиск по переписке" aria-label="Поиск по переписке">
<button class="light">Найти</button></form></div>"""

    def name(chat: int) -> str:
        return services.chat_names.get(chat) or f"Чат {chat}"

    def when(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")

    def link(c) -> str:
        return "/observe?" + urlencode({"conn": c["connection_id"], "chat": c["chat_id"]})

    chats = services.db.observed_chats()
    if q.strip():
        found = services.db.observations(query=q.strip())
        rows = "".join(
            f"<tr><td class='muted' style='white-space:nowrap'>{when(r['sent_at'])}</td>"
            f"<td><a href='{escape(link(r))}'>{escape(name(r['chat_id']))}</a></td>"
            f"<td>{'Вы' if r['role'] == 'manager' else 'Клиент'}</td><td><pre>{escape(r['text'])}</pre></td></tr>" for r in found)
        body = (f"<div class='card'><h2>Найдено: {len(found)}</h2><div class='scroll'><table>"
                f"<tr><th>Когда</th><th>Чат</th><th>Кто</th><th>Сообщение</th></tr>{rows}</table></div></div>"
                if found else "<div class='card muted'>Ничего не нашлось.</div>")
    elif not chats:
        body = '<div class="card muted">Сообщений пока нет. Они появятся, когда клиенты напишут в чаты, где подключён бот.</div>'
    else:
        chosen = next((c for c in chats if c["connection_id"] == conn_id and str(c["chat_id"]) == chat_id), None)
        picked = chosen or chats[0]
        items = "".join(
            f"<a class='item{' on' if c is picked else ''}' href='{escape(link(c))}' data-key='{c['chat_id']}:{c['n']}'>"
            f"{avatar(name(c['chat_id']), str(c['chat_id']))}<span class='nm'>{escape(name(c['chat_id']))}</span>"
            f"<span class='tm'>{short_time(c['last_at'])}</span>"
            f"<span class='pv'>{plural(c['n'], 'сообщение', 'сообщения', 'сообщений')}</span></a>"
            for c in chats)
        messages, waiting, lines = services.db.observed_chat(picked["connection_id"], picked["chat_id"]), None, []
        for m in messages:
            mine = m["role"] == "manager"
            note_text = ""
            if not mine and waiting is None:
                waiting = m["sent_at"]  # обращение — первое сообщение клиента после вашего ответа
            elif mine and waiting is not None:
                note_text, waiting = f" · ответ через {minutes_text((m['sent_at'] - waiting) / 60)}", None
            lines.append(f"<div class='msg{' me' if mine else ''}' data-key='{m['id']}'>"
                         f"<div class='muted'>{'Вы' if mine else 'Клиент'} · "
                         f"{when(m['sent_at'])}{note_text}</div><div class='bubble'>{escape(m['text'])}</div></div>")
        body = f"""<div class="inbox{' open' if chosen else ''}" data-live="inbox">
<div class="list"><div class="list-head">Чаты · {len(chats)}</div><div class="items">{items}</div></div>
<div class="thread"><div class="thread-head"><a class="back" href="/observe">← Все чаты</a>
<h2>{escape(name(picked['chat_id']))}</h2>
<span class="muted">{plural(picked['n'], 'сообщение', 'сообщения', 'сообщений')}</span></div>
<div class="thread-body"><div class="chat">{''.join(lines)}</div></div></div></div>"""
    file_card = watch_log_html(services)
    return f"""<h1>Наблюдение</h1>{note(msg)}<div class="card">{state}
<p class="hint">Телефоны, почты и номера карт скрыты. Голосовые и файлы сохраняются только как отметки.
Факты из ваших ответов — в <a href="/learn">«Обучении»</a>.</p></div>{kpis}{search}{body}
{f'<div class="card">{file_card}</div>' if file_card else ''}"""


def watch_log_html(services: Services) -> str:
    """Переписка, которую бот записал в режиме наблюдения: скачать, собрать из неё черновик базы или удалить."""
    path = services.watch_log
    if not path.exists():
        return ""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    chats = {(m.get("conn"), m.get("chat")) for m in map(json.loads, lines)}
    return f"""<h2>Файл переписки</h2>
<p class="muted">{plural(len(lines), "сообщение", "сообщения", "сообщений")} в {plural(len(chats), "чате", "чатах", "чатах")}.
Файл хранится только на этом компьютере: без имён, телефоны, почты и карты скрыты.</p>
<div class="row"><a class="btn light" href="/watch/file">Скачать</a>
<form method="post" action="/watch/import"><button>Собрать черновик базы</button></form>
<form method="post" action="/watch/clear"><button class="light" onclick="return confirm('Удалить файл переписки из наблюдения?')">Удалить файл</button></form></div>"""


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


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def chart_svg(week: list[int], before: list[int]) -> str:
    """Обращения по дням: эта неделя линией с заливкой, прошлая — пунктиром. Точки стоят по центрам семи колонок,
    поэтому подписи дней под графиком (сетка из 7 колонок) совпадают с ними при любой ширине."""
    w, top, base = 700, 12, 136
    peak = max(week + before + [1])

    def points(values: list[int]) -> list[tuple[float, float]]:
        return [((i + .5) * w / 7, base - v / peak * (base - top)) for i, v in enumerate(values)]

    now, prev = points(week), points(before)
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in now)
    area = f"M{now[0][0]:.1f} {base} L{line.replace(' ', ' L')} L{now[-1][0]:.1f} {base}Z"
    grid = "".join(f'<line class="grid" x1="0" x2="{w}" y1="{y}" y2="{y}"/>' for y in (top, (top + base) / 2, base))
    label = ", ".join(f"{WEEKDAYS[i]} {v}" for i, v in enumerate(week))
    return (f'<svg class="chart" viewBox="0 0 {w} 146" role="img" aria-label="Обращения за 7 дней по дням: {label}">'
            f'{grid}<polyline class="prev" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in prev)}"/>'
            f'<path class="area" d="{area}"/><polyline class="line" pathLength="1" points="{line}"/>'
            f'<circle cx="{now[-1][0]:.1f}" cy="{now[-1][1]:.1f}" r="4.5"/></svg>')


def spark_svg(values: list[float]) -> str:
    """Мини-график показателя по дням недели: только форма, без осей."""
    lo, hi, w, h = min(values), max(values), 88, 26
    span = (hi - lo) or 1
    pts = " ".join(f"{2 + i * (w - 4) / (len(values) - 1):.1f},{h - 3 - (v - lo) / span * (h - 6):.1f}"
                   for i, v in enumerate(values))
    return f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true"><polyline pathLength="1" points="{pts}"/></svg>'


def week_html(services: Services) -> str:
    """Главное на «Обзоре»: сколько обращений за неделю против прошлой и как с ними справился бот."""
    days = services.db.daily(14)
    before, week = days[:7], days[7:]
    total, was = sum(d["questions"] for d in week), sum(d["questions"] for d in before)
    if was and total != was:
        delta = f'<span class="delta">{f"{(total - was) / was:+.0%}".replace("-", "−")} к прошлой неделе</span>'
    else:
        delta = '<span class="delta">как на прошлой неделе</span>' if was else ""
    labels = "".join(f"<span><b>{d['questions']}</b>{'сегодня' if i == 6 else WEEKDAYS[d['day'].weekday()]}</span>"
                     for i, d in enumerate(week))
    bot, auto = sum(d["bot"] for d in week), sum(d["auto"] for d in week)
    escalated = sum(d["escalated"] for d in week)
    speed = [d["latency_ms"] for d in week if d["latency_ms"]]
    quiet = "" if bot else '<p class="muted hint">бот за неделю не отвечал клиентам</p>'
    stats = [("Бот ответил сам", f"{auto / bot:.0%}" if bot else "—",
              spark_svg([d["auto"] / d["bot"] if d["bot"] else 0 for d in week]), quiet),
             ("Передано менеджеру", str(escalated), spark_svg([d["escalated"] for d in week]), "")]
    if speed:
        stats.append(("Бот отвечает за", f"{sum(speed) / len(speed) / 1000:.1f} с".replace(".", ","),
                      spark_svg([d["latency_ms"] / 1000 for d in week]), ""))
    else:
        waits = services.watch_stats() or {}
        median = waits.get("median_wait_min")
        stats.append(("Вы отвечаете клиенту через", minutes_text(median) if median is not None else "—", "",
                      '<p class="muted hint">по наблюдению</p>' if median is not None else ""))
    side = "".join(f'<div class="stat"><div class="lbl">{name}</div><div class="val">{value}</div>{spark}{hint}</div>'
                   for name, value, spark, hint in stats)
    s = services.db.stats()
    return f"""<div class="card" data-live="week"><div class="week"><div>
<div class="muted">Обращения клиентов за 7 дней</div><div class="big">{total}{delta}</div>
<div class="legend"><span><i></i>эта неделя</span><span><i class="prev"></i>прошлая · {was}</span></div>
{chart_svg([d["questions"] for d in week], [d["questions"] for d in before])}<div class="days7">{labels}</div></div>
<div>{side}</div></div>
<p class="muted" style="margin:16px 0 0">За всё время: {plural(s['real'], 'вопрос', 'вопроса', 'вопросов')} клиентов ·
бот ответил сам {s['auto_share']:.0%} · передано менеджеру {s['escalated_share']:.0%} · отмечено ошибок {s['flagged']}</p></div>"""


def waiting_json(services: Services) -> dict:
    """Черновики и передачи менеджеру, которые ждут решения: номер, кто, вопрос, куда перейти."""
    items = [{"id": r["id"], "kind": "Черновик" if r["status"] == D.DRAFT_PENDING else "Менеджеру",
              "name": chat_title(services, r["connection_id"], r["chat_id"]), "text": r["question"][:160],
              "url": chat_link(r["connection_id"], r["chat_id"]) + f"#d{r['id']}"}
             for r in services.db.attention(limit=20) if r["status"] in (D.DRAFT_PENDING, D.ESCALATED)]
    return {"items": items}


def search_json(services: Services, q: str) -> dict:
    """Группы результатов для поиска ⌘K. Пустой запрос — все разделы, чтобы быстро перейти с клавиатуры."""
    words = q.casefold().split()
    places = [{"title": title, "sub": "", "url": url} for title, url, keys in SEARCH_PLACES
              if all(w in f"{title} {keys}".casefold() for w in words)]
    groups = [{"title": "Разделы", "items": places[:8 if words else len(places)]}] if places else []
    if not words:
        return {"groups": groups}
    query, seen, chats = q.strip(), set(), []
    for variant in dict.fromkeys((query, query.lower(), query[:1].upper() + query[1:])):  # LIKE в SQLite — регистр кириллицы
        for r in services.db.recent_dialogs(20, query=variant, **FILTERS[""][1]):
            key = (r["connection_id"] or "", r["chat_id"] or 0)
            if key not in seen and len(chats) < 6:
                seen.add(key)
                text = r["question"] if query.casefold() in r["question"].casefold() else r["answer"]
                chats.append({"title": chat_title(services, *key), "sub": text[:140],
                              "url": chat_link(*key) + f"#d{r['id']}"})
    observed, seen = [], set()
    for variant in dict.fromkeys((query, query.lower(), query[:1].upper() + query[1:])):
        for r in services.db.observations(20, query=variant):
            key = (r["connection_id"], r["chat_id"])
            if key not in seen and len(observed) < 4:
                seen.add(key)
                observed.append({"title": services.chat_names.get(r["chat_id"]) or f"Чат {r['chat_id']}",
                                 "sub": r["text"][:140],
                                 "url": "/observe?" + urlencode({"conn": r["connection_id"], "chat": r["chat_id"]})})
    kb, found = services.draft_kb(), []
    for hit in kb.search(query, 8):
        c = hit.chunk
        if c.kind == "price" and c.id.removeprefix("price-").isdigit():
            row = kb.prices[int(c.id.removeprefix("price-"))]
            found.append({"title": f"{row.format} · {row.placement}", "sub": f"{format_rub(row.price_rub)} · {row.period}",
                          "url": f"/kb/price/{c.id.removeprefix('price-')}"})
        elif c.kind == "faq":
            found.append({"title": c.title, "sub": " ".join(c.text.split())[:140], "url": "/kb#faq"})
    groups += [{"title": title, "items": items} for title, items in
               (("Разговоры с ботом", chats), ("Наблюдение", observed), ("База знаний", found[:6])) if items]
    return {"groups": groups}


def attention_html(services: Services) -> str:
    """Что ждёт человека: новые подключения, черновики, свежие передачи менеджеру и неразобранные ошибки."""
    rows = []
    for c in services.db.connections():
        if not c.approved:
            who = escape(services.owner_names.get(c.owner_user_id, f"id {c.owner_user_id}"))
            rows.append(f'<li data-key="conn:{escape(c.id)}"><span class="pill warn">Подключение</span>'
                        f'<a href="#connections"><b>{who}</b>'
                        f'<span class="q">ждёт разрешения</span></a><span></span></li>')
    for r in services.db.attention():
        if r["flagged"] and not r["reviewed"]:
            tag, href = "Ошибка", f"/dialogs/{r['id']}/review"
        else:
            tag = "Черновик" if r["status"] == D.DRAFT_PENDING else "Менеджеру"
            href = chat_link(r["connection_id"], r["chat_id"])
        rows.append(f'<li data-key="{r["id"]}:{tag}"><span class="pill warn">{tag}</span><a href="{escape(href)}">'
                    f'<b>{escape(chat_title(services, r["connection_id"], r["chat_id"]))}</b>'
                    f'<span class="q">{escape(r["question"])}</span></a>'
                    f'<span class="muted">{short_time(r["created_at"])}</span></li>')
    body = (f'<ul class="todo">{"".join(rows)}</ul>' if rows else
            '<p class="muted">Ничего не ждёт: черновиков без решения и передач за сутки нет.</p>')
    return f'<div class="card" data-live="todo"><h2>Требуют внимания</h2>{body}</div>'


def dashboard_html(services: Services, question: str = "", result: str = "") -> str:
    sandbox = f"""<div class="card" id="sandbox"><h2>Проверить бота</h2>
<p class="muted">Напишите вопрос как клиент — бот ответит здесь, клиенту ничего не уйдёт.</p>
<form method="post" action="/sandbox" data-typing><textarea name="question" rows="2" required data-enter-send
aria-label="Вопрос как от клиента"
placeholder="Сколько стоит экран на фуд-корте в ВЕЕР Молл?">{escape(question)}</textarea>
<p class="row"><button>Спросить</button>
<button class="light" formaction="/sandbox/reset" formnovalidate>Новый разговор</button></p></form>{result}</div>"""

    return f"""<h1>Обзор</h1>{onboarding.banner(services)}{connect_guide_html(services)}{week_html(services)}
<div class="cols"><div>{attention_html(services)}{sandbox}</div>
<div class="card" data-live="status"><h2>Состояние</h2>{status_html(services)}</div></div>{before_after_html(services)}{growth_html(services)}
{activity_html(services)}{connections_html(services)}"""


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
    # Наблюдение — живой замер на том же боте; выгрузка переписки — если наблюдения ещё не было
    watch = services.watch_stats()
    st = watch if watch and watch.get("median_wait_min") is not None else import_stats(services)
    if not st or st.get("median_wait_min") is None:
        return ""
    source = (f"медиана за наблюдение {st.get('period', '')}, обращений с ответом — {st['answered']}"
              if st.get("source") == "watch" else f"медиана по переписке {st.get('period', '')}")
    exam = last_exam(services)
    bot = (f"{exam['summary'].get('latency_answer_avg_ms', 0) / 1000:.1f} с".replace(".", ",")
           if exam else "пару секунд")
    off = (f"<div class='muted'>Вечером и в выходные — {st['off_hours_share']:.0%} обращений</div>"
           if st.get("off_hours_share") is not None else "")
    repeated = (f"<div class='muted'>Повторяющиеся вопросы — {st['repeated_share']:.0%}</div>"
                if st.get("repeated_share") is not None else "")
    return f"""<div class="card"><h2>Было → стало</h2><div class="kpis" style="margin:0">
<div><div class="muted">Клиент ждал ответа менеджера</div><div class="kpi">{minutes_text(st['median_wait_min'])}</div>
<div class='muted'>{escape(source)}</div></div>
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
    modes = ", ".join(MODE_TITLES.get(c.mode, c.mode) for c in conns)
    item(bool(conns) and not pending, "Подключения",
         (f"{len(conns)}: {modes}" if conns else "менеджер ещё не подключил бота")
         + (f" · {pending} ждёт разрешения ниже" if pending else ""))
    observed = services.db.observation_stats()
    if observed["messages"] or any(c.mode == "observe" for c in conns):
        item(True, "Наблюдение без ответов",
             f'<a href="/observe">Сообщений: {observed["messages"]} · чатов: {observed["chats"]}</a>'
             f' · <a href="/learn">кандидатов в базу: {observed["candidates"]}</a>')
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
    return f"""<div class="card" data-live="activity"><h2>Что делает бот</h2>{paused}{table}
<p class="muted">Новые события появляются сами, пока страница открыта. Полный журнал — в «Диалогах».</p></div>"""


def silent_options(c) -> str:
    """«Выключен» и «Наблюдение» — только для Telegram: в MAX клиентам, кроме бота, никто не ответит."""
    if c.id.startswith("max:"):
        return ""
    return (f'<option value="off" {"selected" if c.mode == "off" else ""}>Выключен</option>\n'
            f'<option value="observe" {"selected" if c.mode == D.OBSERVE else ""}>Наблюдение</option>\n')


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
<select name="mode" style="width:auto">{silent_options(c)}<option value="draft" {'selected' if c.mode == 'draft' else ''}>Черновики</option>
<option value="auto" {'selected' if c.mode == 'auto' else ''}>Автоответ</option></select>
<button class="small">Сохранить</button></form></td></tr>"""
    pending = sum(not c.approved for c in conns)
    hint = ("<p class='muted'>Кто-то подключил бота к своему аккаунту. Разрешайте только своим менеджерам: "
            "каждое подключение тратит лимит модели.</p>" if pending else "")
    title = "Подключения" if any(c.id.startswith("max:") for c in conns) else "Подключения Telegram"
    return f"""<div class="card" id="connections"><h2>{title}</h2>{hint}<div class="scroll"><table>
<tr><th>Менеджер</th><th>Статус</th><th>Режим</th></tr>{"".join(row(c) for c in conns)}</table></div></div>"""


def learn_html(services: Services, msg: str = "", check: str = "", *,
               candidate_edit: tuple[list[int], str, str] | None = None) -> str:
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
    return f"<h1>Чему научить бота</h1>{note(msg)}{check}{candidates_html(services, candidate_edit)}{intro}{body}"


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


SOURCE_NAMES = {"card": "ответ на карточку бота", "chat": "ответ в чате клиента", "observe": "наблюдение"}


def candidates_html(services: Services, candidate_edit: tuple[list[int], str, str] | None = None) -> str:
    """Факты из ответов менеджера клиентам: утвердить, поправить или отклонить. До утверждения бот их не видит."""
    groups = group_candidates(services.db.candidates(), services.kb)
    if not groups:
        return ""
    cards = []
    for items in groups[:30]:
        top = items[0]
        question, answer = top["question"], top["answer"]
        if candidate_edit and set(candidate_edit[0]) == {r["id"] for r in items}:
            _, question, answer = candidate_edit
        ids = ",".join(str(r["id"]) for r in items)
        when = dt.datetime.fromtimestamp(top["created_at"]).strftime("%d.%m %H:%M")
        warnings = services.candidate_warnings(items, question, answer)
        # Сохранённая сверка относится к исходному извлечению. После правки показываем
        # текущие сигналы, сохраняя прежние подсказки о прайсе и похожих фактах.
        notes = list(dict.fromkeys([line for r in items for line in r["note"].splitlines()
                                   if line and not line.startswith(NOTE_PREFIX)] + warnings))
        warn = "".join(f'<p class="warn">{escape(n)}</p>' for n in notes)
        reviewed = ('<label><input type="checkbox" name="source_reviewed" value="true"> '
                    'Сверил с исходным ответом</label>') if warnings else ""
        sources = "".join(
            f"<li><b>Клиент:</b> {escape(r['client_question'])}<br><b>Вы ({SOURCE_NAMES.get(r['source'], r['source'])}):</b> "
            f"{escape(r['manager_text'])}</li>" for r in items)
        cards.append(f"""<div class="card"><h2>«{escape(question)}»</h2>
<p class="muted">{plural(len(items), "источник", "источника", "источников")} · последний {when}</p>{warn}
<details{' open' if warnings else ''}><summary>Откуда взято</summary><ul class="muted">{sources}</ul></details>
<form method="post" action="/candidates/approve"><input type="hidden" name="ids" value="{ids}">
<label>Вопрос</label><input type="text" name="question" value="{escape(question)}">
<label>Ответ для базы</label><textarea name="answer" rows="3">{escape(answer)}</textarea>{reviewed}
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


AVATAR_HUES = ("#c2410c", "#2563eb", "#15803d", "#7c3aed", "#0e7490", "#b45309")


def chat_title(services: Services, connection_id: str | None, chat_id: int | None) -> str:
    """Имя чата в списках: тестовый чат админки, имя клиента из Telegram или номер чата."""
    if (connection_id or "").startswith("sandbox:"):
        return "Тестовый чат"
    return services.chat_names.get(chat_id) or f"Чат {chat_id}"


def avatar(name: str, key: str) -> str:
    """Кружок с буквами имени; цвет у чата постоянный, чтобы его было легко найти глазами."""
    letters = "".join(w[0] for w in re.findall(r"[^\W\d_]+", name)[:2]).upper() or "#"
    hue = AVATAR_HUES[sum(map(ord, key)) % len(AVATAR_HUES)]
    return f'<span class="av" style="--hue:{hue}" aria-hidden="true">{escape(letters)}</span>'


def short_time(ts: float) -> str:
    """12:04 — сегодня, 27.10 — раньше, как в мессенджере."""
    moment = dt.datetime.fromtimestamp(ts)
    return moment.strftime("%H:%M") if moment.date() == dt.date.today() else moment.strftime("%d.%m")


def chat_link(connection_id: str | None, chat_id: int | None, f: str = "", q: str = "") -> str:
    params = {"f": f, "q": q, "conn": connection_id or "", "chat": chat_id or 0}
    return "/dialogs?" + urlencode({k: v for k, v in params.items() if v != ""})


def status_pill(status: str) -> str:
    return f'<span class="pill {PILL_TONE.get(status, "")}">{escape(STATUS_NAMES.get(status, status))}</span>'


def turn_html(r, back: str, titles: dict[str, str]) -> str:
    """Вопрос клиента и что с ним сделал бот: ответ или черновик, на что опирался или почему передал, кнопка «Ошибка»."""
    flagged, status = bool(r["flagged"]), r["status"]
    when = dt.datetime.fromtimestamp(r["created_at"]).strftime("%d.%m %H:%M")
    draft = " draft" if status in (D.DRAFT_PENDING, D.DRAFT_REJECTED) else ""
    text = f"<div class='bubble{draft}'>{escape(r['answer'])}</div>" if r["answer"] else ""
    if r["decision"] == "answer":
        sources = json.loads(r["sources"] or "[]")
        source = " ".join(titles.get(sources[0], sources[0]).split()) if sources else ""
        why = f"Опора: {escape(source[:90])}{'…' if len(source) > 90 else ''}" if source else ""
    else:
        why = escape(human_reason(r["reason"])) if r["reason"] else ""
    flag_form = (f"<form method='post' action='/dialogs/{r['id']}/flag'>"
                 f"<input type='hidden' name='flagged' value='{0 if flagged else 1}'>"
                 f"<input type='hidden' name='back' value='{escape(back)}'>"
                 f"<button class='small {'' if flagged else 'light'}'>{'Снять' if flagged else 'Ошибка'}</button></form>")
    review = ""
    if flagged:  # разбор — под ответом; пока не сохранён, открыт
        label = "Разбор сохранён" if r["reviewed"] else "Разобрать ошибку"
        # Правильный ответ — с текстом бота: неверную цену проще поправить, чем набирать ответ заново
        values = {**dict(r), "corrected_answer": r["corrected_answer"] or r["answer"]}
        form = review_form_html(r, back, values, prefix=f"r{r['id']}-")
        review = f"<details class='review'{'' if r['reviewed'] else ' open'}><summary>{label}</summary>{form}</details>"
    return (f"<div class='turn{' flag' if flagged else ''}' id='d{r['id']}' data-key='{r['id']}:{status}:{int(flagged)}'>"
            f"<div class='msg'><div class='muted'>Клиент · {when}</div><div class='bubble'>{escape(r['question'])}</div></div>"
            f"<div class='msg me'><div class='muted'>Бот · {status_pill(status)}</div>{text}</div>"
            f"<div class='why'>{f'<span>{why}</span>' if why else ''}{flag_form}</div>{draft_form_html(r, back)}{review}</div>")


def draft_form_html(r, back: str) -> str:
    """Вопрос ждёт менеджера — ответить можно прямо здесь: отправить черновик, свой вариант или пропустить.
    В тестовом чате отвечать некому."""
    if r["status"] not in (D.DRAFT_PENDING, D.ESCALATED) or (r["connection_id"] or "").startswith("sandbox:"):
        return ""
    has_draft = r["status"] == D.DRAFT_PENDING and r["answer"]
    send = "<button name='action' value='send'>Отправить клиенту</button>" if has_draft else ""
    skip = "<button class='light' name='action' value='skip'>Пропустить</button>" if has_draft else ""
    return (f"<form class='reply' method='post' action='/dialogs/{r['id']}/draft' data-reply>"
            f"<input type='hidden' name='back' value='{escape(back)}'>{send}"
            f"<details{'' if has_draft else ' open'}><summary class='btn light'>"
            f"<span class='closed'>{'Исправить' if has_draft else 'Ответить клиенту'}</span>"
            f"<span class='opened'>{'Отменить правку' if has_draft else 'Свернуть'}</span></summary>"
            f"<textarea name='text' rows='3' maxlength='4096' aria-label='Ваш ответ клиенту' "
            f"placeholder='Ответ уйдёт клиенту от вашего имени'>{escape(r['answer']) if has_draft else ''}</textarea>"
            f"<button name='action' value='edit'>Отправить {'свой вариант' if has_draft else 'ответ'}</button></details>"
            f"{skip}</form>")


def dialogs_html(services: Services, f: str, q: str, conn: str = "", chat: str = "", msg: str = "") -> str:
    """«Диалоги» как ящик поддержки: слева разговоры по чатам, справа переписка выбранного с решениями бота."""
    f, q = (f if f in FILTERS else ""), q.strip()
    tabs = "".join(
        f'<a href="/dialogs?f={key}" class="{"on" if key == f else ""}">{name}'
        f'<span class="c">{services.db.count_dialogs(**cond)}</span></a>' for key, (name, cond) in FILTERS.items())
    search = f"""<form method="get" action="/dialogs" role="search"><input type="hidden" name="f" value="{f}">
<input type="search" name="q" value="{escape(q)}" placeholder="Поиск по вопросам и ответам" aria-label="Поиск по вопросам и ответам">
<button class="light">Найти</button></form>"""
    head = f'<h1>Диалоги</h1>{note(msg)}<div class="toolbar"><div class="tabs" data-live="tabs">{tabs}</div>{search}</div>'
    chats: dict[tuple[str, int], list] = {}
    for r in services.db.recent_dialogs(500, query=q, **FILTERS[f][1]):
        chats.setdefault((r["connection_id"] or "", r["chat_id"] or 0), []).append(r)
    if not chats:
        empty = ("Ничего не нашлось." if q else
                 "Здесь появятся разговоры, в которых участвует бот. Проверить его можно на «Обзоре».")
        return f'{head}<div class="card empty">{empty}</div>'
    chosen = next((k for k in chats if k[0] == conn and str(k[1]) == chat), None)
    picked = chosen or next(iter(chats))
    items = []
    for key, rows in chats.items():
        last, name = rows[0], chat_title(services, *key)
        mark = '<span class="pill warn">ошибка</span>' if any(r["flagged"] for r in rows) else ""
        items.append(
            f'<a class="item{" on" if key == picked else ""}" href="{escape(chat_link(*key, f, q))}" '
            f'data-key="{key[0]}:{key[1]}:{last["id"]}:{last["status"]}">'
            f'{avatar(name, f"{key[0]}:{key[1]}")}<span class="nm">{escape(name)}</span>'
            f'<span class="tm">{short_time(last["created_at"])}</span><span class="pv">{escape(last["question"])}</span>'
            f'<span class="st">{status_pill(last["status"])}{mark}</span></a>')
    turns = services.db.chat_dialogs(*picked, FILTERS[""][1]["statuses"])
    titles = {c.id: c.text for c in services.kb.chunks}
    back = chat_link(*picked, f, q)
    channel = ("тестовый чат админки" if picked[0].startswith("sandbox:")
               else "MAX" if picked[0].startswith("max:") else "Telegram")
    to_list = "/dialogs" + (f"?{urlencode({k: v for k, v in (('f', f), ('q', q)) if v})}" if f or q else "")
    export = ('<p><a class="btn light" href="/dialogs/flagged.csv">Скачать для экзамена (CSV)</a> '
              '<span class="muted">только проверенные примеры</span></p>' if f == "flagged" else "")
    return f"""{head}{export}<div class="inbox{" open" if chosen else ""}" data-live="inbox">
<div class="list"><div class="list-head">Разговоры · {len(chats)}</div><div class="items">{"".join(items)}</div></div>
<div class="thread"><div class="thread-head"><a class="back" href="{escape(to_list)}">← Все разговоры</a>
<h2>{escape(chat_title(services, *picked))}</h2>
<span class="muted">{channel} · {plural(len(turns), "вопрос", "вопроса", "вопросов")}</span></div>
<div class="thread-body"><div class="chat">{"".join(turn_html(r, back, titles) for r in turns)}</div></div>
<div class="thread-foot">«Ошибка» — если бот ответил неверно или зря передал вопрос. После разбора пример попадёт в экзамен.
<span class="keys">↑ ↓ — соседний разговор · / — поиск</span></div>
</div></div>"""


def flagged_csv_text(services: Services) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["question", "expected", "must_contain"])
    for r in services.db.recent_dialogs(10_000, flagged=True, reviewed=True):
        writer.writerow([r["question"], r["expected_decision"], r["must_contain"]])
    return "﻿" + out.getvalue()  # BOM, чтобы Excel открыл кириллицу


def dialog_review_html(dialog, back: str, msg: str = "", values: dict[str, str] | None = None) -> str:
    back = back if back.startswith("/dialogs") else "/dialogs?f=flagged"
    return f"""<h1>Разбор ответа</h1><p><a href="{escape(back)}">← Диалоги</a></p>{note(msg)}
<div class="card"><div class="muted">Вопрос клиента</div><div class="bubble">{escape(dialog['question'])}</div>
<div class="muted">Ответ бота</div><div class="bubble">{escape(dialog['answer'])}</div>
{f'<p class="muted">Бот отвечал по версии базы {dialog["kb_version"]}.</p>' if dialog["kb_version"] else ""}</div>
<div class="card">{review_form_html(dialog, back, values)}</div>"""


def review_form_html(dialog, back: str, values: dict | None = None, prefix: str = "") -> str:
    """Форма разбора ошибки: что было неверно, как правильно и что проверить. prefix — чтобы id полей
    не повторялись, когда в переписке открыто несколько разборов."""
    values = dict(dialog) if values is None else values

    def options(key: str, choices: tuple[tuple[str, str], ...]) -> str:
        return '<option value="">Выберите</option>' + "".join(
            f'<option value="{value}" {"selected" if values.get(key) == value else ""}>{label}</option>'
            for value, label in choices)

    types = options("error_type", (("facts", "Неверные факты"), ("decision", "Неверное решение")))
    decisions = options("expected_decision", (("answer", "Ответить клиенту"), ("escalate", "Передать менеджеру")))
    return f"""<form method="post" action="/dialogs/{dialog['id']}/review">
<input type="hidden" name="back" value="{escape(back)}">
<label for="{prefix}error-type">Что было неверно</label><select id="{prefix}error-type" name="error_type" required>{types}</select>
<label for="{prefix}expected-decision">Как правильно поступить</label>
<select id="{prefix}expected-decision" name="expected_decision" required>{decisions}</select>
<label for="{prefix}corrected-answer">Правильный ответ</label>
<textarea id="{prefix}corrected-answer" name="corrected_answer" rows="4" maxlength="4096">{escape(values.get('corrected_answer') or '')}</textarea>
<label for="{prefix}must-contain">Что проверить в ответе</label>
<input id="{prefix}must-contain" type="text" name="must_contain" maxlength="1000" value="{escape(values.get('must_contain') or '')}">
<p class="hint">Если бот должен ответить, укажите текст и факты через «;», например: 31500;экран.
Варианты — экран|монитор, запрет — !бесплатно.</p>
<p><button>Подтвердить для экзамена</button></p></form>"""


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
    import_card = f"""<div class="card" id="import"><h2>Импорт переписки из Telegram</h2>
<p class="muted">Telegram Desktop → Настройки → Продвинутые → Экспорт данных → «Личные чаты», формат JSON.
Бот найдёт частые вопросы и ответы менеджера, посчитает, сколько клиенты ждали ответа, и скроет телефоны, почты и карты.</p>
<form method="post" action="/import" enctype="multipart/form-data" class="row">
<input type="file" name="files" accept=".json" multiple required>
<input type="text" name="me" placeholder="Имя менеджера в Telegram (если выгружали отдельные чаты)" style="flex:1;min-width:240px">
<button>Импортировать</button></form>{imported}</div>"""
    jump = ('<div class="tabs"><a href="#prices">Прайс</a><a href="#notes">Заметки</a><a href="#faq">Частые вопросы</a>'
            '<a href="#stops">Стоп-темы</a><a href="#import">Импорт</a></div>')
    return f"""<h1>База знаний</h1>{note(msg)}{jump}{versions_html(services)}
<div class="card" id="prices"><h2>Прайс · {plural(len(kb.prices), "позиция", "позиции", "позиций")}</h2>
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
<div class="card" id="faq"><h2>Частые вопросы · {len(parse_faq(raw_text("faq.md")))}</h2>
<p class="muted">Каждый вопрос начинается с «## », ответ — следующими строками. Цены сюда не пишите, они берутся из прайса.</p>
<form method="post" action="/kb/save"><input type="hidden" name="name" value="faq.md">
<textarea name="text" rows="16" class="code">{raw("faq.md")}</textarea>
<p class="row"><button>Сохранить</button><a href="/kb/file/faq.md">скачать faq.md</a></p></form></div>

<div class="card" id="stops"><h2>Стоп-темы · {len(kb.stop_topics)}</h2>
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
    return f"""<div class="card" id="notes"><h2>Заметки · {len(files)}</h2>
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
        share = done / state["total"] if state["total"] else 0
        head = f"""<div class="card" data-live="exam"><h2>Идёт экзамен: {done} из {total}</h2>
<div class="progress" role="progressbar" aria-label="Ход экзамена" aria-valuemin="0" aria-valuemax="{state['total'] or 0}"
aria-valuenow="{done}" data-progress="{share:.3f}" style="--p:{share:.3f}"><i></i></div>
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
    return f"""<h1>Настройки</h1>{note(msg)}<form method="post" action="/settings" data-dirty>
<div class="card" id="model"><h2>Модель и режим</h2>
<label for="model">Нейросеть</label><select id="model" name="model">{models}</select>{key_warn}
<p class="hint">Lite отвечает быстрее, Pro точнее: на экзамене у Pro не было опасных ответов.</p>
<label for="mode">Режим для новых подключений</label>
<select id="mode" name="default_mode"><option value="observe" {"selected" if mode == "observe" else ""}>Наблюдение — бот молчит, сохраняет переписку и считает время ответа менеджера</option>
<option value="draft" {"selected" if mode == "draft" else ""}>Черновики — ответ сначала видит менеджер</option>
<option value="auto" {"selected" if mode == "auto" else ""}>Автоответ — бот отвечает клиенту сам</option></select>
<p class="hint">У уже подключённых менеджеров режим меняется на странице «Обзор» или командой /mode в боте.</p></div>

<div class="card" id="publish"><h2>Изменения базы знаний</h2>
<label for="publish">Публиковать изменения</label>
<select id="publish" name="kb_publish"><option value="auto" {"selected" if v["kb_publish"] == "auto" else ""}>Сразу, если проверка пройдена</option>
<option value="manual" {"selected" if v["kb_publish"] == "manual" else ""}>Вручную — кнопкой на странице «База знаний»</option></select>
<p class="hint">Перед публикацией бот проверяет черновик: находит ли он по-прежнему ответы на контрольные вопросы
и нет ли противоречий. Если проверка нашла проблему, изменения ждут вашего решения в любом режиме.</p></div>

<div class="card" id="auto"><h2>Когда бот отвечает сам</h2>
<label for="sched">Автоответ</label>
<select id="sched" name="auto_schedule"><option value="always" {"selected" if v["auto_schedule"] == "always" else ""}>Всегда</option>
<option value="off_hours" {"selected" if v["auto_schedule"] == "off_hours" else ""}>Только в нерабочее время — днём бот готовит черновики</option></select>
<label>Рабочее время менеджера</label>
<div class="time">с <input type="time" name="work_start" value="{text("work_start")}"> до <input type="time" name="work_end" value="{text("work_end")}"></div>
<label>Рабочие дни</label><div class="days">{days}</div>
<p class="hint">Действует для подключений в режиме «Автоответ». Время екатеринбургское.</p></div>

<div class="card" id="limits"><h2>Защита от лишних трат</h2>
<label for="daily">Обращений к модели в день, не больше</label>
<input id="daily" type="number" name="daily_llm_limit" min="0" max="100000" value="{text("daily_llm_limit")}">
<p class="hint">После лимита бот до полуночи передаёт вопросы менеджеру и не тратит токены. 0 — без лимита.
Один ответ ≈ 800 токенов, на GigaChat Pro ≈ 0,40 ₽.</p>
<label for="hourly">Сообщений от одного клиента в час, не больше</label>
<input id="hourly" type="number" name="chat_hourly_limit" min="0" max="1000" value="{text("chat_hourly_limit")}">
<p class="hint">Если клиент или чужой бот засыпает сообщениями, бот замолкает в этом чате на час и сообщает менеджеру.</p></div>

<div class="card" id="summary"><h2>Вечерняя сводка</h2>
<label for="summary">Присылать менеджеру итоги дня в Telegram, время</label>
<div class="time"><input id="summary" type="time" name="summary_time" value="{text("summary_time")}"></div>
<p class="hint">Сколько вопросов было, на что бот ответил сам — и кнопки «Ошибка» под его ответами.
Очистите поле, чтобы не присылать.</p></div>

<div class="card" id="texts"><h2>Тексты бота</h2>
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

<div class="card" id="pauses"><h2>Паузы</h2>
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
