from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    name: str = "base"
    last_tokens: int = 0  # расход последнего запроса по данным API (0 — неизвестно)

    @abstractmethod
    async def complete(self, system: str, messages: list[dict]) -> str:
        """Вернуть текст ответа модели.

        messages — список {"role": "user"|"assistant", "content": str}.
        """

    async def close(self) -> None:  # pragma: no cover
        pass
