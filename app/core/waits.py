"""Сколько клиент ждал первого ответа менеджера — общая сводка для импорта переписки и режима наблюдения.

Обращение — первое сообщение клиента после ответа менеджера (или начала чата); следующие сообщения клиента
до ответа ожидание не сбрасывают. Одна функция на оба источника, чтобы «как было до бота» по выгрузке
и по наблюдению считалось одинаково и их можно было сравнивать.
"""
from __future__ import annotations

import datetime as dt
import statistics


def wait_summary(waits: list[tuple[dt.datetime, float]], unanswered: int, work_start: str = "09:00",
                 work_end: str = "19:00", work_days: str = "12345") -> dict:
    """waits — (когда написал клиент, минут до ответа) в местном времени; unanswered — обращения без ответа."""
    def off_hours(d: dt.datetime) -> bool:
        return str(d.isoweekday()) not in work_days or not work_start <= d.strftime("%H:%M") < work_end

    minutes = [w for _, w in waits]
    off = [w for d, w in waits if off_hours(d)]
    return {
        "requests": len(waits) + unanswered,
        "answered": len(waits),
        "unanswered": unanswered,
        "median_wait_min": round(statistics.median(minutes), 1) if minutes else None,
        "within_5_min": round(sum(w <= 5 for w in minutes) / len(minutes), 3) if minutes else None,
        "over_1_hour": round(sum(w > 60 for w in minutes) / len(minutes), 3) if minutes else None,
        "off_hours_share": round(len(off) / len(waits), 3) if waits else None,
        "off_hours_median_wait_min": round(statistics.median(off), 1) if off else None,
        "period": (f"{min(d for d, _ in waits):%d.%m.%Y}–{max(d for d, _ in waits):%d.%m.%Y}" if waits else ""),
    }
