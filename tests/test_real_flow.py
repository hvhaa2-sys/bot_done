"""Служебные сообщения и благодарности из живого потока, без внешней модели."""
import asyncio

import pytest

from app.core.real_flow import BusinessAssistant, business_operation
from tests.test_max import CONN, CLIENT_CHAT, MANAGER_CHAT, connect, feed, message, to
from tests.test_max import env as max_env  # noqa: F401


@pytest.mark.parametrize('text', ['Катя, принято, спасибо!', 'Иван, благодарю', 'принято, спасибо!',
                                 'спасибо, Анна!'])
def test_named_thanks_are_silent(env, text):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    before = len(api.sent)
    feed(bot, message(text))
    assert len(api.sent) == before
    assert services.db.recent_dialogs(10) == []


@pytest.mark.parametrize('text', ['отправила ролики на почту, поставь с понедельника',
                                 'дошло письмо?', 'договор согласован?', 'добавьте в счет',
                                 'по денежке порешать', 'эти баннеры свободны с 12-го?'])
def test_operations_reach_manager_without_any_client_answer(env, text):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)
    feed(bot, message(text))
    assert to(api, CLIENT_CHAT) == []
    assert text in to(api, MANAGER_CHAT)[-1]['text']


@pytest.mark.parametrize('text', ['Какое разрешение ролика?', 'Что входит в монтаж?', 'Цена экрана в Веере?',
                                 'Какие требования к материалам?'])
def test_general_reference_questions_are_not_operational(text):
    assert not business_operation(text)


def test_refusal_does_not_become_client_answer(env, monkeypatch):
    from app.core.assistant import Assistant, Reply
    services, _, _ = env

    async def answer(*args, **kwargs):
        return Reply('answer', 'Не могу предоставить эту информацию', 1, llm_called=True, tokens=12)
    monkeypatch.setattr(Assistant, 'answer', answer)
    reply = asyncio.run(BusinessAssistant(services.kb, services.llm).answer('Что с размещением?'))
    assert reply.decision == 'escalate' and reply.reason == 'model_unsure'
    assert reply.tokens == 12 and reply.llm_called


@pytest.fixture
def env(request):
    return request.getfixturevalue("max_env")


@pytest.mark.parametrize('text', ['Можно сократить стоимость и пересчитать на часы?',
                                 'Можем напечатать сами баннеры?',
                                 'Конструкция в эту вилку будет входить?',
                                 'Можно стать партнёром следующего дня рождения ДЕПО? Сколько это стоит?'])
def test_negotiation_and_production_permission_are_manager_decisions(env, text):
    services, _, _ = env
    reply = asyncio.run(services.answer(text))
    assert reply.decision == 'escalate' and not reply.llm_called
