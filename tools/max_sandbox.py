"""Песочница MAX: свой маленький MAX на компьютере — проверить и показать бота, пока нет токена организации.

Поднимает поддельный MAX Bot API (те же адреса и поля, что у platform-api2.max.ru: /me, /updates с marker,
/messages, /answers) и страницу с чатами: клиенты пишут боту компании, менеджер получает карточки, жмёт кнопки
и отвечает на них. Бот настоящий — `run_max` через `MaxApi` и та же база знаний (копия `data/kb`), но SQLite
временная, а в Telegram и настоящий MAX ничего не уходит.

Запуск:
    python -m tools.max_sandbox                # модель из .env (GigaChat), чаты — http://127.0.0.1:8030
    python -m tools.max_sandbox --offline      # заглушка вместо модели, без сети
    python -m tools.max_sandbox --admin 8031   # ещё и админка песочницы, ключ для входа — sandbox
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import logging
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "sandbox"
ADMIN_KEY = "sandbox"  # вход в админку песочницы: она только на 127.0.0.1 и с временной базой, секрета тут нет
BOT = {"user_id": 1, "first_name": "Помощник", "username": "sales_helper_bot", "is_bot": True}
# Кто есть в песочнице: менеджер и два клиента, у каждого свой диалог с ботом
PEOPLE = {
    "manager": {"user_id": 101, "first_name": "Менеджер", "last_name": "", "username": "", "chat_id": 9101},
    "anna": {"user_id": 201, "first_name": "Анна", "last_name": "К.", "username": "anna_k", "chat_id": 9201},
    "ildar": {"user_id": 202, "first_name": "Ильдар", "last_name": "", "username": "", "chat_id": 9202},
}
MEDIA = {"image": "фото", "audio": "голосовое", "file": "файл"}
log = logging.getLogger("max_sandbox")


def user(who: str) -> dict:
    p = PEOPLE[who]
    return {k: p[k] for k in ("user_id", "first_name", "last_name", "username")} | {"is_bot": False}


class FakeMax:
    """Состояние поддельного MAX: сообщения по чатам, очередь событий для бота, уведомления от кнопок."""

    def __init__(self):
        self.messages: list[dict] = []  # всё, что видно в чатах, по порядку
        self.updates: list[dict] = []   # события для бота; marker — номер следующего
        self.notes: list[dict] = []     # всплывающие уведомления после нажатия кнопки
        self.callbacks: dict[str, tuple[str, str]] = {}  # callback_id → (mid сообщения, кто нажал)
        self.seq = itertools.count(1)
        self.hooks: dict[str, str] = {}  # подписки: адрес вебхука → секрет
        self.delivered = 0               # сколько событий уже отправлено на вебхук
        self.version = 0
        self.changed = asyncio.Condition()

    async def touch(self) -> None:
        self.version += 1
        await self.deliver()
        async with self.changed:
            self.changed.notify_all()

    async def deliver(self) -> None:
        """Подписка есть — события уходят на вебхук с секретом в заголовке, опросу не достаются."""
        while self.hooks and self.delivered < len(self.updates):
            update = self.updates[self.delivered]
            self.delivered += 1
            async with httpx.AsyncClient(timeout=10) as client:
                for url, secret in self.hooks.items():
                    await client.post(url, json=update, headers={"X-Max-Bot-Api-Secret": secret})

    def find(self, mid: str) -> dict:
        found = next((m for m in self.messages if m["body"]["mid"] == mid), None)
        if found is None:
            raise HTTPException(404, {"code": "not.found", "message": f"message {mid} not found"})
        return found

    def chat_of(self, chat_id: int) -> str:
        return next(who for who, p in PEOPLE.items() if p["chat_id"] == chat_id)

    def new_message(self, sender: dict, chat_id: int, text: str, attachments: list | None = None,
                    link: dict | None = None) -> dict:
        seq = next(self.seq)
        msg = {"sender": sender, "recipient": {"chat_id": chat_id, "chat_type": "dialog", "user_id": None},
               "timestamp": int(time.time() * 1000), "body": {"mid": f"mid.{seq:06d}", "seq": seq, "text": text,
                                                             "attachments": attachments or []}}
        if link:
            msg["link"] = link
        self.messages.append(msg)
        return msg

    def linked(self, kind: str, mid: str) -> dict:
        """Ответ или пересылка: MAX кладёт в сообщение тело исходного и его автора."""
        original = self.find(mid)
        return {"type": kind, "sender": original["sender"], "chat_id": original["recipient"]["chat_id"],
                "message": original["body"]}

    # --- со стороны людей (страница песочницы) ---
    async def user_writes(self, who: str, text: str = "", media: str = "", reply_to: str = "") -> dict:
        token = hashlib.sha256(f"{who}{next(self.seq)}".encode()).hexdigest()[:16]
        attachments = [{"type": media, "payload": {"url": f"https://example.invalid/{media}", "token": token}}] if media else []
        link = self.linked("reply", reply_to) if reply_to else None
        msg = self.new_message(user(who), PEOPLE[who]["chat_id"], text, attachments, link)
        self.updates.append({"update_type": "message_created", "timestamp": msg["timestamp"], "message": msg,
                             "user_locale": "ru"})
        await self.touch()
        return msg

    async def user_starts(self, who: str) -> None:
        self.updates.append({"update_type": "bot_started", "timestamp": int(time.time() * 1000),
                             "chat_id": PEOPLE[who]["chat_id"], "user": user(who), "payload": None})
        await self.touch()

    async def user_presses(self, who: str, mid: str, payload: str) -> None:
        msg = self.find(mid)
        callback_id = f"cb.{next(self.seq)}"
        self.callbacks[callback_id] = (mid, who)
        self.updates.append({"update_type": "message_callback", "timestamp": int(time.time() * 1000),
                             "callback": {"callback_id": callback_id, "payload": payload, "user": user(who),
                                          "timestamp": int(time.time() * 1000)},
                             "message": msg})
        await self.touch()

    # --- со стороны бота (MAX Bot API) ---
    async def bot_sends(self, chat_id: int, body: dict) -> dict:
        if chat_id not in {p["chat_id"] for p in PEOPLE.values()}:
            raise HTTPException(404, {"code": "chat.not.found", "message": f"chat {chat_id} not found"})
        text = body.get("text") or ""
        if len(text) >= 4000:
            raise HTTPException(400, {"code": "text.too.long", "message": "text must be less than 4000 characters"})
        link = body.get("link")
        if link:
            link = self.linked(link["type"], link["mid"])
        msg = self.new_message(BOT, chat_id, text, body.get("attachments"), link)
        await self.touch()
        return {"message": msg}

    async def bot_answers(self, callback_id: str, body: dict) -> dict:
        if callback_id not in self.callbacks:
            raise HTTPException(404, {"code": "callback.not.found", "message": "callback not found"})
        mid, who = self.callbacks[callback_id]
        if body.get("notification"):
            self.notes.append({"who": who, "text": body["notification"], "at": time.time()})
        if body.get("message") is not None:
            msg = self.find(mid)
            msg["body"]["text"] = body["message"].get("text") or ""
            msg["body"]["attachments"] = body["message"].get("attachments") or []
        await self.touch()
        return {"success": True}

    async def wait_updates(self, marker: int | None, timeout: int, limit: int) -> dict:
        if self.hooks:  # как в MAX: при активной подписке опрос ничего не получает
            return {"updates": [], "marker": marker or 0}
        start = marker or 0
        if len(self.updates) <= start and timeout > 0:
            async with self.changed:
                try:
                    await asyncio.wait_for(self.changed.wait_for(lambda: len(self.updates) > start), timeout)
                except TimeoutError:
                    pass
        batch = self.updates[start:start + limit]
        return {"updates": batch, "marker": start + len(batch)}

    # --- для страницы ---
    def state(self) -> dict:
        def view(m: dict) -> dict:
            body, link = m["body"], m.get("link")
            buttons = [[{"text": b["text"], "payload": b["payload"]} for b in row]
                       for a in body.get("attachments") or [] if a.get("type") == "inline_keyboard"
                       for row in a["payload"]["buttons"]]
            media = next((MEDIA.get(a["type"], a["type"]) for a in body.get("attachments") or []
                          if a.get("type") in MEDIA), "")
            linked = None
            if link:
                lb = link["message"]
                lmedia = next((MEDIA.get(a["type"], a["type"]) for a in lb.get("attachments") or []
                               if a.get("type") in MEDIA), "")
                author = link["sender"]
                linked = {"type": link["type"], "text": lb.get("text") or (f"[{lmedia}]" if lmedia else ""),
                          "from": " ".join(filter(None, (author.get("first_name"), author.get("last_name"))))}
            return {"mid": body["mid"], "bot": m["sender"].get("is_bot", False), "text": body.get("text") or "",
                    "media": media, "buttons": buttons, "link": linked,
                    "time": time.strftime("%H:%M", time.localtime(m["timestamp"] / 1000))}

        chats = {who: [view(m) for m in self.messages if m["recipient"]["chat_id"] == p["chat_id"]]
                 for who, p in PEOPLE.items()}
        fresh = [n for n in self.notes if time.time() - n["at"] < 6]
        return {"version": self.version, "chats": chats, "notes": fresh}


def create_app(fake: FakeMax) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def max_error(_: Request, exc: HTTPException) -> JSONResponse:
        # MAX отвечает на ошибки так: {"code": "...", "message": "..."} — без обёртки FastAPI {"detail": ...}
        body = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse(body, status_code=exc.status_code)

    def auth(request: Request) -> None:
        if request.headers.get("Authorization") != TOKEN:
            raise HTTPException(401, {"code": "verify.token", "message": "Invalid access_token"})

    # MAX Bot API — ровно то, чем пользуется бот
    @app.get("/me")
    async def me(request: Request) -> dict:
        auth(request)
        return BOT

    @app.get("/subscriptions")
    async def subscriptions(request: Request) -> dict:
        auth(request)
        return {"subscriptions": [{"url": url, "time": 0, "update_types": []} for url in fake.hooks]}

    @app.post("/subscriptions")
    async def subscribe(request: Request) -> dict:
        auth(request)
        body = await request.json()
        fake.hooks[body["url"]] = body.get("secret") or ""
        fake.delivered = len(fake.updates)
        return {"success": True}

    @app.delete("/subscriptions")
    async def unsubscribe(request: Request, url: str) -> dict:
        auth(request)
        fake.hooks.pop(url, None)
        return {"success": True}

    @app.get("/updates")
    async def updates(request: Request, marker: int | None = None, timeout: int = 30, limit: int = 100) -> dict:
        auth(request)
        return await fake.wait_updates(marker, min(timeout, 90), limit)

    @app.post("/messages")
    async def messages(request: Request, chat_id: int | None = None, user_id: int | None = None) -> dict:
        auth(request)
        if chat_id is None and user_id is not None:
            chat_id = next((p["chat_id"] for p in PEOPLE.values() if p["user_id"] == user_id), None)
        return await fake.bot_sends(chat_id, await request.json())

    @app.post("/answers")
    async def answers(request: Request, callback_id: str) -> dict:
        auth(request)
        return await fake.bot_answers(callback_id, await request.json())

    # Страница песочницы
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PAGE

    @app.get("/ui/state")
    async def ui_state() -> dict:
        return fake.state()

    @app.post("/ui/send")
    async def ui_send(request: Request) -> dict:
        data = await request.json()
        who = data.get("who")
        if who not in PEOPLE:
            raise HTTPException(400, "unknown person")
        if data.get("start"):
            await fake.user_starts(who)
        elif data.get("payload"):
            await fake.user_presses(who, data.get("mid", ""), data["payload"])
        elif (text := (data.get("text") or "").strip()) or data.get("media") in MEDIA:
            await fake.user_writes(who, text, data.get("media") or "", data.get("reply_to") or "")
        return {"ok": True}

    return app


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Песочница MAX</title>
<style>
:root{--bg:#eef0ed;--card:#fff;--text:#1d2021;--muted:#6b7071;--line:#d7dad6;--bot:#f4f5f2;--me:#1d2021;--accent:#ffd23f}
@media (prefers-color-scheme:dark){:root{--bg:#161819;--card:#1f2223;--text:#e8eae7;--muted:#9a9f9c;--line:#33383a;
--bot:#272b2c;--me:#e8eae7}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
header{padding:14px 20px;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:17px;margin:0}header p{margin:0;color:var(--muted);font-size:13px}
main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:0 20px 20px}
@media (max-width:760px){main{grid-template-columns:minmax(0,1fr);padding:0 12px 12px}}
section{background:var(--card);border:1px solid var(--line);border-radius:4px;display:flex;flex-direction:column;
height:calc(100dvh - 90px);min-height:420px}
.top{display:flex;gap:6px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.top b{margin-right:auto}.tab{border:1px solid var(--line);background:none;color:inherit;padding:4px 10px;border-radius:2px;
cursor:pointer;font:inherit;font-size:13px}.tab.on{background:var(--accent);color:#1d2021;border-color:var(--accent)}
.log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:85%;padding:8px 10px;border-radius:4px;white-space:pre-wrap;word-wrap:break-word;position:relative}
.msg.bot{background:var(--bot);align-self:flex-start}.msg.me{background:var(--me);color:var(--card);align-self:flex-end}
.msg .t{font-size:11px;color:var(--muted);margin-top:4px}.msg.me .t{color:inherit;opacity:.7}
.msg .ln{font-size:12px;border-left:2px solid var(--accent);padding-left:6px;margin-bottom:6px;opacity:.85}
.msg .kb{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}.msg .kb button{font:inherit;font-size:13px;padding:6px 10px;
border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:2px;cursor:pointer;min-height:36px}
.msg .re{font-size:12px;background:none;border:0;color:var(--muted);text-decoration:underline;cursor:pointer;padding:0;
margin-top:4px}
.note{margin:0 12px 8px;padding:6px 10px;background:var(--accent);color:#1d2021;border-radius:2px;font-size:13px}
form{display:flex;gap:6px;padding:10px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
form input{flex:1;min-width:160px;font:inherit;font-size:16px;padding:8px 10px;border:1px solid var(--line);border-radius:2px;
background:var(--card);color:var(--text)}
form button{font:inherit;padding:8px 12px;border:1px solid var(--text);background:var(--text);color:var(--card);
border-radius:2px;cursor:pointer;min-height:40px}form button.light{background:none;color:var(--text);border-color:var(--line)}
.replying{width:100%;font-size:12px;color:var(--muted)}.replying button{min-height:0;padding:0 6px;border:0;background:none;
color:var(--muted);text-decoration:underline}
.empty{color:var(--muted);font-size:13px;margin:auto;text-align:center;max-width:300px}
</style></head><body>
<header><h1>Песочница MAX</h1><p>Бот настоящий, MAX — поддельный: сообщения никуда не уходят.
Сначала менеджер пишет боту /manager.</p></header>
<main>
<section data-side="client"><div class="top"><b>Клиент</b>
<button class="tab on" data-who="anna">Анна К.</button><button class="tab" data-who="ildar">Ильдар</button></div>
<div class="log"></div><div class="notes"></div>
<form><input placeholder="Сообщение боту компании" autocomplete="off"><button>Отправить</button>
<button type="button" class="light" data-start>Начать</button>
<button type="button" class="light" data-media="image">Фото</button>
<button type="button" class="light" data-media="audio">Голосовое</button></form></section>
<section data-side="manager"><div class="top"><b>Менеджер</b></div>
<div class="log"></div><div class="notes"></div>
<form><div class="replying" hidden></div><input placeholder="/manager, /mode observe, или ответ на карточку"
autocomplete="off"><button>Отправить</button>
<button type="button" class="light" data-file>Файл</button></form></section>
</main>
<script>
const client = {who: "anna"}, reply = {mid: "", text: ""};
let version = -1, data = null;
const post = (body) => fetch("/ui/send", {method: "POST", headers: {"Content-Type": "application/json"},
                                          body: JSON.stringify(body)}).then(() => refresh());
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

function bubble(m, who) {
  const mine = !m.bot, link = m.link ? `<div class="ln">${m.link.type === "forward" ? "Переслано от " : "Ответ "}
${esc(m.link.from)}: ${esc(m.link.text.slice(0, 160))}</div>` : "";
  const text = m.text ? esc(m.text) : m.media ? `[${esc(m.media)}]` : "";
  const kb = m.buttons.length ? `<div class="kb">${m.buttons.flat().map((b) =>
    `<button data-mid="${m.mid}" data-payload="${esc(b.payload)}">${esc(b.text)}</button>`).join("")}</div>` : "";
  const re = who === "manager" && m.bot && m.text ? `<button class="re" data-reply="${m.mid}">Ответить</button>` : "";
  return `<div class="msg ${mine ? "me" : "bot"}">${link}${text}${kb}<div class="t">${m.time}</div>${re}</div>`;
}

function render() {
  for (const side of ["client", "manager"]) {
    const box = document.querySelector(`[data-side=${side}]`), who = side === "client" ? client.who : "manager";
    const log = box.querySelector(".log"), stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const msgs = data.chats[who];
    log.innerHTML = msgs.length ? msgs.map((m) => bubble(m, who)).join("")
      : `<p class="empty">${side === "client" ? "Нажмите «Начать» или напишите вопрос." :
         "Напишите /manager — бот запомнит вас менеджером."}</p>`;
    if (stick) log.scrollTop = log.scrollHeight;
    box.querySelector(".notes").innerHTML = data.notes.filter((n) => n.who === who)
      .map((n) => `<p class="note">${esc(n.text)}</p>`).join("");
  }
  const r = document.querySelector(".replying");
  r.hidden = !reply.mid;
  r.innerHTML = reply.mid ? `Ответ на: ${esc(reply.text.slice(0, 80))}… <button type="button">отменить</button>` : "";
}

async function refresh() {
  const next = await fetch("/ui/state").then((r) => r.json());
  if (next.version !== version || next.notes.length || (data && data.notes.length)) {
    data = next; version = next.version; render();
  }
}

document.addEventListener("click", (e) => {
  const t = e.target;
  if (t.matches(".tab")) {
    client.who = t.dataset.who;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b === t));
    render();
  } else if (t.dataset.payload) {
    post({who: t.closest("[data-side]").dataset.side === "client" ? client.who : "manager", mid: t.dataset.mid,
          payload: t.dataset.payload});
  } else if (t.dataset.reply) {
    reply.mid = t.dataset.reply;
    reply.text = data.chats.manager.find((m) => m.mid === reply.mid).text;
    render();
    document.querySelector("[data-side=manager] input").focus();
  } else if (t.closest(".replying") && t.matches("button")) {
    reply.mid = ""; render();
  } else if (t.dataset.start !== undefined) {
    post({who: client.who, start: true});
  } else if (t.dataset.file !== undefined) {
    if (reply.mid) { post({who: "manager", media: "file", reply_to: reply.mid}); reply.mid = ""; }
  } else if (t.dataset.media) {
    post({who: client.who, media: t.dataset.media});
  }
});

document.querySelectorAll("form").forEach((f) => f.addEventListener("submit", (e) => {
  e.preventDefault();
  const input = f.querySelector("input"), text = input.value.trim(), side = f.closest("[data-side]").dataset.side;
  if (!text) return;
  input.value = "";
  const body = {who: side === "client" ? client.who : "manager", text};
  if (side === "manager" && reply.mid) { body.reply_to = reply.mid; reply.mid = ""; }
  post(body);
}));

refresh();
setInterval(refresh, 700);
</script></body></html>"""


async def run(args: argparse.Namespace, folder: Path) -> None:
    import uvicorn

    from app.admin import create_app as create_admin
    from app.channels.max_api import MaxApi
    from app.channels.max_bot import run_max
    from app.config import Settings
    from app.services import Services

    shutil.copytree(ROOT / "data" / "kb", folder / "kb", ignore=shutil.ignore_patterns(".obsidian"))
    base = Settings.load()
    settings = replace(base, db_path=folder / "app.db", kb_dir=folder / "kb", telegram_bot_token="",
                       max_bot_token=TOKEN, max_api_url=f"http://127.0.0.1:{args.port}", admin_token=ADMIN_KEY,
                       admin_login="", admin_password_hash="", exam_files="",
                       llm_provider="mock" if args.offline else base.llm_provider)
    services = Services(settings)
    fake = FakeMax()
    servers = [uvicorn.Server(uvicorn.Config(create_app(fake), host="127.0.0.1", port=args.port, log_level="warning"))]
    if args.admin:
        servers.append(uvicorn.Server(uvicorn.Config(create_admin(services), host="127.0.0.1", port=args.admin,
                                                     log_level="warning")))
    api = MaxApi(TOKEN, settings.max_api_url)
    log.info("Песочница MAX: http://127.0.0.1:%d/  (модель: %s, база — копия data/kb, журнал во временной папке)",
             args.port, "заглушка" if args.offline else settings.llm_provider)
    if args.admin:
        log.info("Админка песочницы: http://127.0.0.1:%d — ключ для входа «%s»", args.admin, ADMIN_KEY)
    log.info("Остановить — Ctrl+C.")
    bot = asyncio.create_task(run_max(api, services))
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        bot.cancel()
        await api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8030, help="порт поддельного MAX и страницы с чатами")
    parser.add_argument("--admin", type=int, default=0, help="порт админки песочницы (0 — без неё)")
    parser.add_argument("--offline", action="store_true", help="заглушка вместо модели, без сети")
    args = parser.parse_args()
    # Решения бота видно в консоли, как в логе живого бота; запросы долгого опроса — нет
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout,
                        force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    folder = Path(tempfile.mkdtemp(prefix="max-sandbox-"))
    try:
        asyncio.run(run(args, folder))
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
