"""Изолированная учебная админка для снимков презентации. Без Telegram и запросов к модели."""
import asyncio
from dataclasses import replace
from pathlib import Path
import shutil

import uvicorn

from app.admin import create_app
from app.config import ROOT, Settings
from app.services import Services


async def main():
    folder = Path(__file__).parent / ".build" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    kb = folder / "kb"
    if not kb.exists():
        shutil.copytree(ROOT / "data/kb", kb)
    settings = replace(Settings.load(), telegram_bot_token="", llm_provider="mock", gigachat_auth_key="",
                       openai_api_key="", db_path=folder / "app.db", kb_dir=kb,
                       admin_token="presentation-demo-local", admin_host="127.0.0.1", admin_port=18782)
    services = Services(settings)
    if not services.db.candidates():
        for q, a in [
            ("На цифровых экранах ВЕЕР Молл есть звук?", "На цифровых экранах ВЕЕР Молл звук не воспроизводится."),
            ("Кто готовит ролик для размещения?", "Готовый ролик по техническим требованиям экрана предоставляет рекламодатель."),
        ]:
            services.db.add_candidate(connection_id="presentation-demo", chat_id=1, dialog_id=None, source="chat",
                                      client_question=q, manager_text=a, question=q, answer=a)
    try:
        await uvicorn.Server(uvicorn.Config(create_app(services), host="127.0.0.1", port=18782,
                                           log_level="warning")).serve()
    finally:
        await services.llm.close()
        services.db.conn.close()


if __name__ == "__main__":
    asyncio.run(main())
