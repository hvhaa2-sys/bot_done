"""Потери при остановке, повтор вебхука и работа при медленном разборе кандидатов."""
import asyncio

import pytest
from dataclasses import replace

import httpx

from app.admin import create_app
from app.channels.max_bot import MaxBot, handle_queue, drain
from app.max_inbox import MaxInbox
from tests.test_max import (BOT, CONN, CLIENT_CHAT, MANAGER_CHAT, connect, message, reply_to, to)
from tests.test_max import env as max_env  # noqa: F401 — изолированная БД и поддельный MAX


async def settled(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(.005)
    raise AssertionError('Обработчик не завершил работу')


def test_ack_is_durable_and_pending_events_survive_new_consumer(env):
    services, bot, api = env
    connect(bot)
    services.settings = replace(services.settings, max_webhook_secret='secret')
    services.max_updates = services.max_inbox
    update = message('Сколько стоит экран в атриуме?')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url='http://test') as c:
            headers = {'X-Max-Bot-Api-Secret': 'secret'}
            assert (await c.post('/max/webhook', json=update, headers=headers)).status_code == 200
        assert services.max_inbox.qsize() == 1
        queue = MaxInbox(services.db.conn)  # как новый процесс: никакой памяти старого обработчика
        new_bot = MaxBot(api, services, BOT)
        worker = asyncio.create_task(handle_queue(new_bot, queue))
        try:
            await asyncio.wait_for(queue.join(), 2)
            cards = len(to(api, MANAGER_CHAT))
            queue.put_nowait(update)
            assert queue.qsize() == 0
            await asyncio.sleep(.01)
            assert len(to(api, MANAGER_CHAT)) == cards
            assert services.db.conn.execute('SELECT payload FROM max_inbox').fetchone()[0] == ''
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_crash_in_merge_window_replays_all_events(env):
    services, bot, api = env
    connect(bot)

    async def scenario():
        await services.update_settings({'merge_seconds': '4'})
        q = services.max_inbox
        worker = asyncio.create_task(handle_queue(bot, q))
        for i in range(10):
            q.put_nowait(message(f'Сколько стоит экран в атриуме? {i}', user=800 + i, chat=8000 + i))
        await settled(lambda: len(bot.buffer.parts) == 10)
        assert q.qsize() == 10, 'попадание в буфер не считается обработкой'
        worker.cancel()
        for timer in bot.buffer.timers.values():
            timer.cancel()
        await asyncio.gather(worker, *bot.buffer.timers.values(), return_exceptions=True)
        await services.update_settings({'merge_seconds': '0'})
        new = MaxBot(api, services, BOT)
        restored = MaxInbox(services.db.conn)
        worker = asyncio.create_task(handle_queue(new, restored))
        try:
            await asyncio.wait_for(restored.join(), 3)
            rows = services.db.recent_dialogs(20)
            assert {r['chat_id'] for r in rows} == set(range(8000, 8010))
            assert len(rows) == 10
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_graceful_stop_flushes_merge_and_pending_queue(env):
    services, bot, _ = env
    connect(bot)

    async def scenario():
        await services.update_settings({'merge_seconds': '4'})
        q = services.max_inbox
        worker = asyncio.create_task(handle_queue(bot, q))
        q.put_nowait(message('Сколько стоит'))
        q.put_nowait(message('экран в атриуме?'))
        await settled(lambda: len(bot.receipts.get((CONN, CLIENT_CHAT), [])) == 2)
        q.put_nowait(message('Какие требования к макету?', user=900, chat=9000))
        bot.stopping = True
        await asyncio.wait_for(drain(bot, q, worker), 2)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        rows = services.db.recent_dialogs(10)
        assert {r['chat_id'] for r in rows} == {CLIENT_CHAT, 9000}
        assert any('Сколько стоит\nэкран' in r['question'] for r in rows)
        assert q.qsize() == 0
    asyncio.run(scenario())


def test_slow_candidate_does_not_block_manager_reply_or_next_client(env, monkeypatch):
    services, bot, api = env
    connect(bot)
    services.db.set_mode('observe', connection_id=CONN)
    services.privacy.set_allowed(CONN, 9000, True)

    async def scenario():
        slow = asyncio.Event()
        async def collect(*args, **kwargs):
            await slow.wait()
        monkeypatch.setattr(services, 'collect_candidates', collect)
        await bot.handle(message('Какие требования к макету?'))
        card = to(api, MANAGER_CHAT)[-1]['text']
        worker = asyncio.create_task(handle_queue(bot, services.max_inbox))
        try:
            services.max_inbox.put_nowait(reply_to(card, 'Макет PDF, ролик 15 секунд.'))
            services.max_inbox.put_nowait(message('Вопрос второго клиента', user=900, chat=9000))
            await asyncio.wait_for(services.max_inbox.join(), .5)
            assert to(api, CLIENT_CHAT)[-1]['text'] == 'Макет PDF, ролик 15 секунд.'
            assert 'второго клиента' in to(api, MANAGER_CHAT)[-1]['text']
        finally:
            slow.set()
            if services._background:
                await asyncio.gather(*services._background)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_health_fails_for_dead_max_worker_and_db_failure(env):
    services, _, _ = env
    services.settings = replace(services.settings, max_bot_token='fake', max_webhook_url='https://example.test')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url='http://test') as c:
            assert (await c.get('/health')).status_code == 503
            services.max_status = {'ok': True}
            services.max_updates = services.max_inbox
            task = asyncio.create_task(asyncio.Event().wait())
            services.max_worker = task
            assert (await c.get('/health')).status_code == 200
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert (await c.get('/health')).status_code == 503
    asyncio.run(scenario())


@pytest.fixture
def env(request):
    return request.getfixturevalue("max_env")


def test_failed_event_retries_without_starving_another_client(env, monkeypatch):
    services, bot, _ = env
    connect(bot)
    seen = []
    original = bot.handle

    async def handle(update):
        if update.get('bad'):
            raise RuntimeError('неисправное событие')
        seen.append(update)
        await original(update)
    monkeypatch.setattr(bot, 'handle', handle)

    async def scenario():
        q = services.max_inbox
        q.put_nowait({'bad': True})
        q.put_nowait(message('Вопрос второго клиента'))
        worker = asyncio.create_task(handle_queue(bot, q))
        try:
            await settled(lambda: bool(seen))
            assert q.qsize() == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('keep_other', [False, True])
def test_deleted_message_does_not_return_after_restart(env, keep_other):
    services, bot, api = env
    connect(bot)

    async def scenario():
        await services.update_settings({'merge_seconds': '20'})
        q = services.max_inbox
        worker = asyncio.create_task(handle_queue(bot, q))
        q.put_nowait(message('Удали этот текст', mid='removed'))
        if keep_other:
            q.put_nowait(message('Сколько стоит экран в атриуме?', mid='kept'))
        await settled(lambda: len(bot.receipts.get((CONN, CLIENT_CHAT), [])) == 1 + keep_other)
        q.put_nowait({'update_type': 'message_removed', 'chat_id': CLIENT_CHAT, 'message_id': 'removed'})
        await settled(lambda: q.qsize() == int(keep_other))
        worker.cancel()
        for task in bot.buffer.timers.values():
            task.cancel()
        await asyncio.gather(worker, *bot.buffer.timers.values(), return_exceptions=True)
        await services.update_settings({'merge_seconds': '0'})
        restored = MaxInbox(services.db.conn)
        worker = asyncio.create_task(handle_queue(MaxBot(api, services, BOT), restored))
        try:
            await asyncio.wait_for(restored.join(), 2)
            rows = services.db.recent_dialogs(10)
            assert len(rows) == int(keep_other)
            assert all('Удали' not in row['question'] for row in rows)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_ten_clients_wait_for_gigachat_without_429_or_lost_events(env):
    import json
    from app.llm.gigachat import GigaChatProvider
    services, bot, _ = env
    connect(bot)
    services.db.set_mode('auto', connection_id=CONN)

    async def scenario():
        inflight = maximum = calls = 0

        async def transport(request):
            nonlocal inflight, maximum, calls
            if 'oauth' in str(request.url):
                return httpx.Response(200, json={'access_token': 'fake'})
            inflight += 1
            maximum = max(maximum, inflight)
            calls += 1
            try:
                await asyncio.sleep(.01)
                assert inflight == 1, 'второй запрос freemium вернул бы 429'
                content = json.dumps({'answer': 'Уточните срок размещения.', 'confidence': 1, 'needs_manager': False})
                return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
            finally:
                inflight -= 1

        provider = GigaChatProvider('fake', 'scope', 'model', True, transport=httpx.MockTransport(transport))
        services.llm = provider
        services.assistant.llm = provider
        await services.update_settings({'merge_seconds': '0.02'})
        worker = asyncio.create_task(handle_queue(bot, services.max_inbox))
        try:
            for i in range(10):
                services.max_inbox.put_nowait(message('Сколько стоит экран в атриуме?', chat=8000 + i, user=800 + i))
            await asyncio.wait_for(services.max_inbox.join(), 3)
            assert calls == 10 and maximum == 1
            rows = services.db.recent_dialogs(20)
            assert {row['chat_id'] for row in rows} == set(range(8000, 8010))
            assert all(not row['reason'].startswith('llm_error') for row in rows)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await provider.close()
    asyncio.run(scenario())
