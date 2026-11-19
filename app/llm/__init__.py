"""Провайдеры языковых моделей за общим интерфейсом.

Модель — заменяемый компонент: ядро ассистента не знает, какая модель стоит за
`LLMProvider`. Это позволяет сравнивать модели на одном тестовом наборе.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .base import LLMProvider


def _gigachat_verify(settings) -> str | bool:
    """Проверка TLS GigaChat: явный bundle, иначе встроенный сертификат Минцифры при verify_ssl."""
    bundle = settings.gigachat_ca_bundle
    if settings.gigachat_verify_ssl and not bundle:
        default = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"
        if default.is_file():
            bundle = str(default)
    return bundle or settings.gigachat_verify_ssl


def make_provider(settings: Settings) -> LLMProvider:
    name = settings.llm_provider
    if name == "gigachat":
        from .. import netbind
        from .gigachat import GigaChatProvider
        verify = _gigachat_verify(settings)
        return GigaChatProvider(settings.gigachat_auth_key, settings.gigachat_scope,
                                settings.gigachat_model, settings.gigachat_verify_ssl,
                                transport=netbind.transport(settings.direct_interface, verify),
                                base_url=settings.gigachat_base_url,
                                ca_bundle=verify if isinstance(verify, str) else "")
    if name == "openai":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(settings.openai_base_url, settings.openai_api_key, settings.openai_model,
                                    settings.openai_project)
    if name == "mock":
        from .mock import MockProvider
        return MockProvider()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {name}")
