"""Демо холодного старта для защиты: сценарий проходит целиком и без сети."""
import asyncio

from tools.demo_cold_start import run_demo


def test_cold_start_demo_runs_offline(tmp_path):
    lines = []
    result = asyncio.run(run_demo(tmp_path / "demo", offline=True, out=lines.append))
    text = "\n".join(lines)
    assert "Бот передаёт менеджеру" in text and "бот предложил фактов для базы: 1" in text
    assert result["facts"] == 2 and result["answered"] == 2
    assert result["version"] == 3, "бот отвечает по проверенной версии, документ с противоречием не опубликован"
    assert result["conflicts"] and "Разные ответы на один вопрос" in result["conflicts"][0]
    assert "не опубликовано" in text
