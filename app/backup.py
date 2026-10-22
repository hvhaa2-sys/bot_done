"""Ежедневная копия базы: журнал диалогов — это и метрики пилота, и материал для экзамена."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from pathlib import Path

from .services import Services

log = logging.getLogger(__name__)
KEEP = 14  # сколько последних копий хранить


def backup_dir(services: Services) -> Path:
    return services.settings.db_path.parent / "backups"


def daily_backup(services: Services, today: dt.date | None = None) -> Path | None:
    """Сделать копию базы за сегодня, если её ещё нет. Старые копии сверх KEEP удаляются."""
    today = today or services.local_now().date()
    folder = backup_dir(services)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"app-{today:%Y-%m-%d}.db"
    if target.exists():
        return None
    dest = sqlite3.connect(target)
    try:
        services.db.conn.backup(dest)  # штатное копирование SQLite, безопасно на ходу
    finally:
        dest.close()
    for old in sorted(folder.glob("app-*.db"))[:-KEEP]:
        old.unlink()
    return target


async def backup_loop(services: Services) -> None:
    while True:
        try:
            if (path := daily_backup(services)) is not None:
                log.info("Копия базы: %s", path)
        except Exception:  # noqa: BLE001 — копия не должна ронять бота
            log.exception("Не удалось сделать копию базы")
        await asyncio.sleep(3600)
