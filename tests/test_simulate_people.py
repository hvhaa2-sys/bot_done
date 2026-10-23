"""Изолированный прогон проходит обработчики, не открывает Telegram и соблюдает бюджет."""
import asyncio
import json

import httpx
import pytest

from app.llm.base import LLMProvider
from tools.simulate_people import LimitedLLM, load_people, matches, run_simulation


def test_simulated_people_use_real_observation_and_publication(tmp_path):
    folder = tmp_path / "run"
    result = asyncio.run(run_simulation(folder))
    assert result["synthetic"] and not result["live_model"]
    assert result["checks"]["candidates_not_published_automatically"]
    assert result["checks"]["no_private_or_greeting_candidates"]
    assert result["checks"]["all_three_facts_collected"]
    assert result["checks"]["all_three_facts_approved"]
    assert result["checks"]["unknown_still_escalated"]
    assert result["outgoing_telegram"] == []
    assert result["observation"]["messages"] == len(result["transcript"]) == 13
    assert result["observation"]["chats"] == 5
    assert len(result["candidates"]) == 3 and result["active_version"] == 4
    assert result["candidates_snapshot"] == "before_review"
    assert all(r["status"] == "approved" for r in result["candidates_after_review"])
    assert [r["phase"] for r in result["probes"]] == ["before"] * 4 + ["after"] * 4
    saved = json.loads((folder / "report.json").read_text())
    assert saved == result
    assert "не результаты пилота" in (folder / "Отчёт.md").read_text()
    assert (folder / "kb_versions/0004").is_dir()


def test_existing_results_are_not_overwritten(tmp_path):
    (tmp_path / "existing.md").write_text("Сохранённый результат")
    with pytest.raises(ValueError, match="не перезаписываем"):
        asyncio.run(run_simulation(tmp_path))
    assert (tmp_path / "existing.md").read_text() == "Сохранённый результат"
    assert not (tmp_path / "app.db").exists()


def test_unmarked_real_dialogs_are_rejected(tmp_path):
    cases, manager = load_people()
    (tmp_path / "clients.json").write_text(json.dumps({"synthetic": False, "cases": cases}))
    (tmp_path / "manager.json").write_text(json.dumps(manager))
    with pytest.raises(ValueError, match="только явно синтетические"):
        load_people(tmp_path)


class CountingLLM(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def complete(self, system, messages):
        self.calls += 1
        self.last_tokens = 123
        return "{}"


def test_hard_budget_blocks_inner_provider():
    inner = CountingLLM()
    wrapper = LimitedLLM(inner, 1)
    asyncio.run(wrapper.complete("", []))
    with pytest.raises(RuntimeError, match="Исчерпан лимит"):
        asyncio.run(wrapper.complete("", []))
    assert inner.calls == 1 and len(wrapper.records) == 1
    assert wrapper.blocked_calls == 1 and wrapper.last_tokens == 0
    assert wrapper.records[0]["tokens"] == 123


def test_api_failure_is_recorded_without_credentials():
    class FailingLLM(LLMProvider):
        async def complete(self, system, messages):
            request = httpx.Request("POST", "https://example.invalid/?token=private-placeholder")
            response = httpx.Response(402, request=request)
            raise httpx.HTTPStatusError("private-placeholder", request=request, response=response)

    wrapper = LimitedLLM(FailingLLM(), 2)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(wrapper.complete("", []))
    assert wrapper.records[0]["error"] == "HTTPStatusError HTTP 402"
    assert "private-placeholder" not in json.dumps(wrapper.records)


def test_duration_grading_accepts_paraphrase_without_accepting_wrong_limit():
    assert matches("duration", "Максимальная длительность видеоролика для экрана в атриуме ТЦ ВЕЕР Молл "
                   "составляет 15 секунд.")
    assert matches("duration", "Ролик не более 15 секунд.")
    assert not matches("duration", "Максимальная длительность составляет 30 секунд.")
    assert not matches("duration", "15 секунд — удобный вариант; ограничение длительности неизвестно.")
