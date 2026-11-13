"""Имена собеседников переживают перезапуск; неразрешённые чаты наблюдения и удалённые клиенты в базу не попадают."""
from dataclasses import replace

from app.config import ROOT, Settings
from app.services import Services
from tests.test_observe import CONN, CLIENT
from tests.test_observe import env as observe_env  # noqa: F401


def make(tmp_path):
    settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=ROOT / "tests" / "fixtures" / "kb",
                       llm_provider="mock", exam_files="")
    return Services(settings)


def test_name_survives_restart_and_erase_removes_it(tmp_path):
    services = make(tmp_path)
    services.chat_names[695343992] = "Катя Крюкова (@katya)"
    services.chat_names.remember(42, "Мама")
    again = make(tmp_path)
    assert again.chat_names.get(695343992) == "Катя Крюкова (@katya)" and 42 not in again.chat_names
    again.chat_names.pop(695343992)
    assert 695343992 not in make(tmp_path).chat_names
    # Имена из базы по-прежнему скрываются в ленте событий
    services.event("Катя Крюкова (@katya): клиент попросил менеджера")
    assert "Катя" not in str(services.events)


def test_blocked_observe_chat_name_stays_in_memory(request):
    env = request.getfixturevalue("observe_env")
    env.feed({"business_message": {"message_id": 1, "date": env.now, "business_connection_id": CONN, "text": "Привет",
                                   "chat": {"id": 888, "type": "private", "first_name": "Мама"},
                                   "from": {"id": 888, "is_bot": False, "first_name": "Мама"}}})
    env.msg("Сколько стоит экран?", 2, chat_id=CLIENT)
    rows = dict(env.services.db.conn.execute("SELECT chat_id, name FROM chat_names").fetchall())
    assert env.services.chat_names.get(888) == "Мама" and 888 not in rows, "личный контакт — только в памяти"
    assert rows.get(CLIENT) == "Test", "разрешённый рабочий чат — в базе"
