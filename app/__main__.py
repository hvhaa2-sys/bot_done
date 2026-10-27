"""Запуск: python -m app — админка и (если заданы токены) боты Telegram и MAX в одном процессе."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import sys

import uvicorn

from .admin import create_app
from .backup import backup_loop
from .config import Settings
from .services import Services

log = logging.getLogger("app")


async def run_bot(bot, dp, services: Services) -> None:
    """Подключиться к Telegram и слушать сообщения.

    Связь с Telegram бывает нестабильной: если при запуске его не достать, не сдаёмся, а пробуем
    снова (5 с, 10 с … до минуты). Окончательно останавливаемся, только если Telegram не принял токен.
    """
    from aiogram.exceptions import TelegramUnauthorizedError

    from .channels.telegram_business import summary_loop

    delay = 5
    while True:
        try:
            me = await bot.me()
            break
        except TelegramUnauthorizedError:
            services.bot_status = {"ok": False, "error": "Telegram не принял токен — проверьте TELEGRAM_BOT_TOKEN в .env"}
            log.error("%s. Работает только админка.", services.bot_status["error"])
            return
        except Exception as exc:  # noqa: BLE001 — сеть, блокировки
            services.bot_status = {"ok": False, "error": f"нет связи с Telegram ({type(exc).__name__}) — пробую снова"}
            log.warning("Нет связи с Telegram (%s) — пробую снова через %d с", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    services.bot_status = {"ok": True, "username": me.username, "business": bool(me.can_connect_to_business)}
    log.info("Бот @%s запущен", me.username)
    if not me.can_connect_to_business:
        log.warning("У бота не включён бизнес-режим: @BotFather → /mybots → бот → Bot Settings → Secretary Mode")
    # Вебхук и опрос не работают вместе: если вебхук где-то остался, бот «живой», но сообщений не получает
    with contextlib.suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=False)
    summary = asyncio.create_task(summary_loop(bot, services))
    try:
        await dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
    finally:
        summary.cancel()


class AdminServer(uvicorn.Server):
    """Ctrl+C ловим сами: штатный uvicorn после остановки посылает сигнал заново,
    и уборка (остановка бота, закрытие соединений) прерывается на полпути."""

    def capture_signals(self):
        return contextlib.nullcontext()


def port_busy(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) == 0


def _log_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Бот остановился с ошибкой: %r. Админка продолжает работать.", task.exception())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    if port_busy(settings.admin_host, settings.admin_port):
        # Второй экземпляр: Telegram не даст двум копиям бота читать сообщения, а админке некуда встать
        log.error("Порт %d занят — бот, похоже, уже запущен (в другом терминале или в приложении Claude). "
                  "Остановите его или задайте другой ADMIN_PORT в .env.", settings.admin_port)
        sys.exit(1)
    services = Services(settings)
    log.info("Модель: %s, база знаний: %d вопросов, %d позиций прайса",
             services.llm.name, len(services.kb.faq), len(services.kb.prices))

    server = AdminServer(uvicorn.Config(create_app(services), host=settings.admin_host,
                                        port=settings.admin_port, log_level="info"))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
    tasks = [asyncio.create_task(backup_loop(services))]
    bot = dp = polling = None
    if settings.telegram_bot_token:
        from aiogram import Bot

        from .channels.telegram_business import build_dispatcher

        bot, dp = Bot(settings.telegram_bot_token), build_dispatcher(services)
        polling = asyncio.create_task(run_bot(bot, dp, services))
        polling.add_done_callback(_log_crash)
        tasks.append(polling)
    else:
        log.warning("TELEGRAM_BOT_TOKEN не задан — работает только админка и тестовый чат")
    max_api = None
    if settings.max_bot_token:
        from .channels.max_api import MaxApi
        from .channels.max_bot import run_max

        max_api = MaxApi(settings.max_bot_token, settings.max_api_url)
        max_task = asyncio.create_task(run_max(max_api, services))
        max_task.add_done_callback(_log_crash)
        tasks.append(max_task)

    if settings.admin_token == "change-me":
        log.warning("ADMIN_TOKEN по умолчанию: задайте свой в .env, иначе любой сайт, открытый в браузере, "
                    "может слать запросы в админку")
    if settings.admin_login and settings.admin_password_hash:
        # Вход по логину и паролю: токен в лог не пишем
        log.info("Админка: http://localhost:%d — вход по логину «%s»", settings.admin_port, settings.admin_login)
    else:
        log.info("Админка: http://localhost:%d/?token=%s", settings.admin_port, settings.admin_token)
    try:
        await server.serve()  # работает до Ctrl+C
    finally:
        if dp:  # штатная остановка опроса Telegram: иначе aiogram сыплет ошибками оборванных запросов
            with contextlib.suppress(RuntimeError):  # опрос ещё не начался (нет связи)
                await dp.stop_polling()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(polling), timeout=5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot:
            await bot.session.close()
        if max_api:
            await max_api.close()
        await services.llm.close()
        log.info("Остановлено")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
