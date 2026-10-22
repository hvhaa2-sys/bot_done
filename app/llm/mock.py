"""Заглушка без сети: отвечает первым фрагментом из контекста.

Нужна для тестов, разработки без ключей и демонстрации конвейера.
"""
from __future__ import annotations

import json
import re

from .base import LLMProvider

_BLOCK_RE = re.compile(r"^\[(\d+)\] (.+?)(?=^\[\d+\] |\Z)", re.MULTILINE | re.DOTALL)


class MockProvider(LLMProvider):
    name = "mock"

    async def complete(self, system: str, messages: list[dict]) -> str:
        blocks = _BLOCK_RE.findall(system)
        if not blocks:
            return json.dumps({"answer": "", "confidence": 0.1, "needs_manager": True}, ensure_ascii=False)
        text = blocks[0][1].strip()
        if "Ответ:" in text:
            text = text.split("Ответ:", 1)[1].strip()
        elif text.startswith("Прайс:"):
            text = text[len("Прайс:"):].strip()
        return json.dumps({"answer": text, "confidence": 0.85, "needs_manager": False}, ensure_ascii=False)
