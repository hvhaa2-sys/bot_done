"""Регрессии перед обновлением рабочего контейнера."""

import asyncio
from pathlib import Path
import ssl

import pytest

from app.config import Settings
from app.core.knowledge import KnowledgeBase, parse_faq
from app.core.real_flow import BusinessAssistant
from app.llm import make_provider
from app.llm.mock import MockProvider


@pytest.mark.parametrize(("question", "reason"), [
    ("Наш ролик вчера вообще не показывали", "client_complaint"),
    ("А если машину поцарапают во время выставки?", "risk_or_liability"),
    ("Скажите, какой тираж парковочных талонов?", "missing_fact:tirage"),
])
def test_server_handoffs_survive_the_release(question, reason):
    faq = parse_faq("## Парковочные талоны\nРеклама на талоне стоит 300 рублей.")
    bot = BusinessAssistant(KnowledgeBase(faq, [], []), MockProvider())
    reply = asyncio.run(bot.answer(question))
    assert reply.reason == reason
    assert not reply.is_answer and not reply.llm_called


def test_gigachat_uses_configured_server_ca_bundle(monkeypatch):
    monkeypatch.setattr("app.config._load_dotenv", lambda _: None)
    ca = Path(__file__).resolve().parents[1] / "app/certs/russian_trusted_root_ca.pem"
    monkeypatch.setenv("GIGACHAT_CA_BUNDLE", str(ca))
    monkeypatch.setenv("GIGACHAT_VERIFY_SSL", "false")
    monkeypatch.setenv("GIGACHAT_AUTH_KEY", "fake")
    monkeypatch.setenv("DIRECT_INTERFACE", "")
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    provider = make_provider(Settings.load())
    try:
        context = provider.client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert len(context.get_ca_certs(binary_form=True)) >= 1
    finally:
        asyncio.run(provider.close())
