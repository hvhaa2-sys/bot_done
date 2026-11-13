"""Проверка перед запуском: python -m app.check

Отвечает на вопрос «можно ли проверять бота в Telegram» и подсказывает, что поправить:
ключ GigaChat, токен бота, бизнес-режим в BotFather, подключения менеджеров, адрес админки.
"""
from __future__ import annotations

import asyncio
import sys

from . import db as D
from .config import Settings
from .services import Services, model_title

QUESTION = "Сколько стоит экран на фуд-корте в ВЕЕР Молл?"


def line(ok: bool | None, title: str, text: str = "") -> None:
    mark = {True: "✓", False: "✗", None: "·"}[ok]
    print(f" {mark} {title}" + (f": {text}" if text else ""))


async def check_model(services: Services) -> bool:
    if services.settings.llm_provider == "mock":
        line(None, "Модель", "заглушка без нейросети — для живой проверки поставьте LLM_PROVIDER=gigachat")
        return True
    reply = await services.answer(QUESTION)
    if reply.reason.startswith("llm_error"):
        line(False, f"Модель {model_title(services.settings)}",
             f"не отвечает ({reply.reason.split(':', 1)[-1]}) — проверьте GIGACHAT_AUTH_KEY и интернет")
        return False
    line(True, f"Модель {model_title(services.settings)}", f"ответила за {reply.latency_ms / 1000:.1f} с")
    print(f"     «{QUESTION}» → {reply.text[:160]}")
    return True


async def check_telegram(settings: Settings) -> bool:
    if not settings.telegram_bot_token:
        line(False, "Telegram", "нет TELEGRAM_BOT_TOKEN в .env — создайте бота в @BotFather (/newbot)")
        return False
    from aiogram.exceptions import TelegramUnauthorizedError

    from .channels.telegram_business import make_bot

    bot = make_bot(settings)
    try:
        me = await bot.get_me()
        hook = await bot.get_webhook_info()
    except TelegramUnauthorizedError:
        line(False, "Telegram", "токен не принят — скопируйте его из @BotFather заново")
        return False
    except Exception as exc:  # noqa: BLE001
        hint = "проверьте релей TELEGRAM_API_URL" if settings.telegram_api_url else \
            "проверьте интернет; с российского сервера нужен релей TELEGRAM_API_URL"
        line(False, "Telegram", f"нет связи ({type(exc).__name__}) — {hint}")
        return False
    finally:
        await bot.session.close()
    ok = True
    line(True, "Бот", f"@{me.username}")
    if me.can_connect_to_business:
        line(True, "Бизнес-режим", "включён")
    else:
        line(False, "Бизнес-режим", "выключен: @BotFather → /mybots → бот → Bot Settings → Secretary Mode")
        ok = False
    if hook.url:
        line(False, "Вебхук", f"у бота задан вебхук {hook.url} — он мешает. Удалите: "
                              f"https://api.telegram.org/bot<токен>/deleteWebhook")
        ok = False
    return ok


async def check_max(settings: Settings) -> bool:
    """MAX — второй канал и необязателен: без токена просто пропускаем."""
    if not settings.max_bot_token:
        line(None, "MAX", "MAX_BOT_TOKEN не задан — второй канал выключен")
        return True
    from .channels.max_api import MaxApi, MaxError

    api = MaxApi(settings.max_bot_token, settings.max_api_url, direct_interface=settings.direct_interface)
    try:
        me = await api.me()
        hooks = await api.subscriptions()
    except MaxError as exc:
        line(False, "MAX", "токен не принят — скопируйте его заново на business.max.ru" if exc.status == 401 else str(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        line(False, "MAX", f"нет связи ({type(exc).__name__}) — проверьте интернет; через зарубежный VPN MAX не открывается")
        return False
    finally:
        await api.close()
    line(True, "Бот MAX", me.get("username") or me.get("first_name") or str(me.get("user_id")))
    if settings.max_webhook_url:
        if not settings.max_webhook_url.startswith("https://") or not settings.max_webhook_secret:
            line(False, "Вебхук MAX", "нужны MAX_WEBHOOK_URL с https:// и MAX_WEBHOOK_SECRET")
            return False
        known = any(h.get("url") == settings.max_webhook_url for h in hooks)
        line(True if known else None, "Вебхук MAX", "подключён" if known else "бот подключит его при запуске")
        return True
    line(None, "Вебхук MAX", "не задан: бот на долгом опросе, для рабочего режима MAX просит вебхук")
    if hooks:
        # Бот получает события долгим опросом; подписка-вебхук, скорее всего, перехватит их
        line(False, "Вебхук MAX", f"у бота есть подписка {hooks[0].get('url')} — удалите её (DELETE /subscriptions)")
        return False
    return True


def connection_modes(conns) -> str:
    """Режимы подключений словами. Раньше «наблюдение» и «выключен» показывались как «черновики»."""
    names = {"off": "выключен", "observe": "наблюдение", "draft": "черновики", "auto": "автоответ"}
    return ", ".join(names.get(D.MODE_ALIASES.get(c.mode, c.mode), c.mode) for c in conns)


async def main() -> int:
    settings = Settings.load()
    services = Services(settings)
    print("Проверка перед запуском\n")
    ok = await check_model(services)
    ok = await check_telegram(settings) and ok
    ok = await check_max(settings) and ok
    conns = services.db.connections()
    if conns:
        line(True, "Подключения", f"{len(conns)} ({connection_modes(conns)})")
    else:
        line(None, "Подключения", "пока нет — после запуска добавьте бота: Настройки → Автоматизация чатов")
    kb = services.kb
    line(bool(kb.prices), "База знаний", f"{len(kb.prices)} позиций прайса, {len(kb.faq)} ответов")
    if settings.admin_token == "change-me":
        line(False, "Админка", "смените ADMIN_TOKEN в .env на свой")
    await services.llm.close()
    how = (f"вход по логину «{settings.admin_login}»" if settings.admin_login and settings.admin_password_hash
           else "токен для входа в .env (ADMIN_TOKEN)")
    print(f"\nАдминка после запуска: http://localhost:{settings.admin_port}/ — {how}")
    print("Запуск: .venv/bin/python -m app" if ok else "Сначала поправьте пункты с ✗.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
