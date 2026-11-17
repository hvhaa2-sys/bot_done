"""Наблюдение только с разрешения, удаление клиента и отсутствие текста в ленте."""
import asyncio

import pytest
import json

import httpx

from app.admin import create_app
from tests.test_max import CONN, CLIENT_CHAT, MANAGER_CHAT, connect, feed, message, to
from tests.test_max import env as max_env  # noqa: F401


def test_unknown_chat_does_not_write_text_or_name_in_observe(env):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('observe', connection_id=CONN)
    before = len(to(api, MANAGER_CHAT))
    feed(bot, message('Личная переписка', user=777, chat=7770))
    assert services.db.recent_dialogs(10) == []
    assert services.db.observation_stats()['messages'] == 0
    assert not services.watch_log.exists()
    assert len(to(api, MANAGER_CHAT)) == before
    assert not services.privacy.allowed(CONN, 7770)
    services.privacy.set_allowed(CONN, 7770, True)
    feed(bot, message('Теперь рабочий вопрос', user=777, chat=7770))
    assert services.db.observation_stats()['messages'] == 1
    services.privacy.set_allowed(CONN, 7770, False)
    feed(bot, message('Снова нельзя', user=777, chat=7770))
    assert services.db.observation_stats()['messages'] == 1


def test_erase_clears_all_client_tables_and_watch_file_but_keeps_other_client(env):
    services, bot, _ = env
    connect(bot)
    services.db.set_mode('observe', connection_id=CONN)
    services.privacy.set_allowed(CONN, 9000, True)
    feed(bot, message('Первый клиент'))
    feed(bot, message('Второй клиент', user=900, chat=9000))
    services.db.pause_chat(CONN, CLIENT_CHAT, 10)
    services.db.set_client_source(CLIENT_CHAT, 'website')
    services.max_inbox.put_nowait(message('Ещё не обработано'))
    services.privacy.erase(CLIENT_CHAT, services.watch_log)
    for sql in ('SELECT count(*) FROM dialogs WHERE chat_id=?',
                'SELECT count(*) FROM candidates WHERE chat_id=?',
                'SELECT count(*) FROM observed_messages WHERE chat_id=?',
                'SELECT count(*) FROM pauses WHERE chat_id=?',
                'SELECT count(*) FROM client_sources WHERE chat_id=?',
                'SELECT count(*) FROM max_inbox WHERE chat_id=?',
                'SELECT count(*) FROM observation_access WHERE chat_id=?'):
        assert services.db.conn.execute(sql, (CLIENT_CHAT,)).fetchone()[0] == 0
    assert services.db.observation_stats()['messages'] == 1
    assert all(json.loads(line)['chat'] == 9000 for line in services.watch_log.read_text().splitlines())
    assert services.privacy.erase(CLIENT_CHAT, services.watch_log) == 0


def test_access_and_erase_require_auth_and_explicit_confirmation(env):
    services, _, _ = env
    services.settings = services.settings.__class__(**{**vars(services.settings), 'admin_token': 'test-secret'})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url='http://test', headers={'Origin': 'http://test'}) as c:
            assert (await c.post('/observe/erase', data={'chat_id': CLIENT_CHAT})).status_code in (401, 403)
            c.cookies.set('admin_token', 'test-secret')
            assert (await c.post('/observe/erase', data={'chat_id': CLIENT_CHAT})).status_code == 400
            assert (await c.post('/observe/erase', data={'chat_id': CLIENT_CHAT,
                                                       'confirm_chat_id': str(CLIENT_CHAT)})).status_code == 303
    asyncio.run(scenario())


def test_events_mask_contact_names_and_message_quotes(env, caplog):
    services, _, _ = env
    services.chat_names[1] = 'Иван Петров (@ivan)'
    with caplog.at_level('INFO', logger='app.bot'):
        services.event('Иван Петров (@ivan): «Личное сообщение» +7 999 123-45-67 test@example.test — бот молчит')
    saved = str(services.events) + caplog.text
    assert all(s not in saved for s in ['Иван', 'Петров', '@ivan', 'Личное сообщение', '999', 'test@example.test'])


@pytest.fixture
def env(request):
    return request.getfixturevalue("max_env")


def test_reply_to_old_card_after_revocation_is_sent_but_not_recorded(env):
    from tests.test_max import reply_to
    services, bot, api = env
    connect(bot)
    services.db.set_mode('observe', connection_id=CONN)
    feed(bot, message('Какой ролик нужен?'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    services.privacy.set_allowed(CONN, CLIENT_CHAT, False)
    feed(bot, reply_to(card, 'Ответ менеджера после запрета'))
    assert to(api, CLIENT_CHAT)[-1]['text'] == 'Ответ менеджера после запрета'
    assert services.db.observation_stats()['messages'] == 1
    assert 'Ответ менеджера после запрета' not in services.watch_log.read_text()


def test_erase_removes_queued_manager_reply_with_client_quote(env):
    from tests.test_max import reply_to, callback
    services, bot, api = env
    connect(bot)
    feed(bot, message('Дадите скидку?'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    dialog_id = services.db.recent_dialogs(1)[0]['id']
    services.max_inbox.put_nowait(reply_to(card, 'Ответ'))
    services.max_inbox.put_nowait(callback(f'send:{dialog_id}', card))
    services.privacy.erase(CLIENT_CHAT, services.watch_log)
    assert services.max_inbox.qsize() == 0
