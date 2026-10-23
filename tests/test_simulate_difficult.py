"""Продолжение живого сценария сохраняет выбор клиента и общий бюджет вызовов."""
import asyncio
import json

import pytest

from app.core.knowledge import PriceRow, prices_csv
from app.core.versions import fingerprint
from tools.simulate_difficult import run_batch


@pytest.fixture
def kb(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "prices.csv").write_text(prices_csv([
        PriceRow("Цифровой экран", "ВЕЕР Молл, Екатеринбург — атриум", "1 месяц", 30000, ""),
        PriceRow("Цифровой экран", "ДЕПО Молл, Нижний Тагил — вход", "1 месяц", 50000, "")
    ]))
    return folder


def inputs(path, messages, **extra):
    case = {"id": "changing-client", "persona": "Спешащий клиент", "goal": "Узнать цену", "risk": "Смена ТЦ",
            "messages": messages, **extra}
    path.write_text(json.dumps({"synthetic": True, "cases": [case]}, ensure_ascii=False))
    return path


def test_resume_keeps_conversation_but_drops_other_venue_prices(tmp_path, kb):
    folder = tmp_path / "run"
    before = fingerprint(kb)
    first = inputs(tmp_path / "first.json", ["Сколько стоит цифровой экран в ВЕЕР на месяц?"])
    result = asyncio.run(run_batch(folder, first, kb_source=kb))
    first_calls = len(result["calls"])
    assert first_calls > 0
    second = inputs(tmp_path / "second.json", ["Нет, лучше экран в ДЕПО на месяц. Сколько стоит?"])
    result = asyncio.run(run_batch(folder, second, resume=True))
    assert len(result["turns"]) == 2 and len(result["calls"]) > first_calls
    turn = result["turns"][-1]
    assert "ДЕПО" in turn["state"]["venue"]
    assert not any("ВЕЕР" in text for text in turn["evidence"] if text.startswith("Прайс:"))
    assert result["kb_unchanged"] and fingerprint(kb) == before
    assert len(result["batches"]) == 2


def test_resume_does_not_reset_provider_call_budget(tmp_path, kb):
    folder = tmp_path / "run"
    first = inputs(tmp_path / "first.json", ["Сколько стоит цифровой экран в ВЕЕР на месяц?"])
    result = asyncio.run(run_batch(folder, first, kb_source=kb, max_calls=1))
    assert len(result["calls"]) == 1
    second = inputs(tmp_path / "second.json", ["А экран в ДЕПО на месяц сколько стоит?"])
    result = asyncio.run(run_batch(folder, second, resume=True, max_calls=1))
    assert len(result["calls"]) == 1 and result["blocked_calls"] >= 1
    assert result["turns"][-1]["decision"] == "escalate"


def test_repeated_batch_and_changed_budget_are_rejected_without_writes(tmp_path, kb):
    folder = tmp_path / "run"
    first = inputs(tmp_path / "first.json", ["Сколько стоит экран в ВЕЕР?"])
    asyncio.run(run_batch(folder, first, kb_source=kb))
    saved = (folder / "report.json").read_bytes()
    with pytest.raises(ValueError, match="уже обработан"):
        asyncio.run(run_batch(folder, first, resume=True))
    with pytest.raises(ValueError, match="общий лимит"):
        asyncio.run(run_batch(folder, first, resume=True, max_calls=50))
    assert (folder / "report.json").read_bytes() == saved


def test_learning_uses_observation_without_publishing_synthetic_conditions(tmp_path, kb):
    folder = tmp_path / "run"
    first = inputs(tmp_path / "learning.json", [
        {"role": "client", "text": "Кто делает ролик для экрана ВЕЕР?"},
        {"role": "manager", "text": "Для экрана ВЕЕР ролик готовит рекламодатель, менеджер его не изготавливает."}
    ], expected="Сохранить оба условия")
    result = asyncio.run(run_batch(folder, first, kb_source=kb, learning=True))
    assert len(result["learning"]) == 1 and len(result["learning"][0]["candidates"]) == 1
    assert result["learning"][0]["candidates"][0]["status"] == "new"
    assert result["kb_unchanged"] and len(result["turns"]) == 0
    assert not (folder / "kb/notes").exists()


def test_real_messages_are_not_accepted(tmp_path):
    data = tmp_path / "real.json"
    data.write_text('{"synthetic": false, "cases": []}')
    with pytest.raises(ValueError, match="синтетический"):
        asyncio.run(run_batch(tmp_path / "run", data))
    assert not (tmp_path / "run").exists()
