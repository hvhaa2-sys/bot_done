import asyncio

import pytest

from app import db as D
from app.channels import voice
from tests.test_max import CONN, CLIENT_CHAT, MANAGER_CHAT, connect, feed, message, reply_to, to
from tests.test_max import env as max_fixture  # noqa: F401
from tests.test_observe import env as telegram_fixture  # noqa: F401

AUDIO = [{'type':'audio','payload':{'url':'https://media.example.org/audio','token':'fake'}}]


def enable(services, monkeypatch, text='Сколько стоит экран в атриуме?'):
    services.speech.enabled = True
    async def transcribe(*args, **kwargs):
        return text
    monkeypatch.setattr(voice, 'max_text', transcribe)
    monkeypatch.setattr(voice, 'telegram_text', transcribe)


@pytest.mark.parametrize('mode', ['auto','draft'])
def test_max_voice_only_sends_draft_to_manager(max_env, monkeypatch, mode):
    services, bot, api = max_env
    connect(bot)
    services.db.set_mode(mode, connection_id=CONN)
    enable(services, monkeypatch)
    feed(bot, message('', attachments=AUDIO))
    assert to(api, CLIENT_CHAT) == []
    assert 'Голосовое' in to(api, MANAGER_CHAT)[-1]['text']
    assert services.db.recent_dialogs(1)[0]['status'] == D.DRAFT_PENDING


def test_max_voice_failure_uses_existing_media_route(max_env, monkeypatch):
    services, bot, api = max_env
    connect(bot)
    enable(services, monkeypatch)
    async def fail(*args):
        return None
    monkeypatch.setattr(voice, 'max_text', fail)
    feed(bot, message('', attachments=AUDIO))
    assert services.db.recent_dialogs(1)[0]['reason'] == 'media'
    assert to(api, MANAGER_CHAT)


def test_max_manager_audio_learns_after_sending_original(max_env, monkeypatch):
    services, bot, api = max_env
    connect(bot)
    enable(services, monkeypatch, 'Ролик должен быть 15 секунд')
    feed(bot, message('Какие требования к ролику?'))
    card = to(api, MANAGER_CHAT)[-1]['text']
    collected = []
    async def collect(**kwargs):
        collected.append(kwargs)
    monkeypatch.setattr(services, 'collect_candidates', collect)
    update = reply_to(card, '')
    update['message']['body']['attachments'] = AUDIO
    async def scenario():
        await bot.handle(update)
        await asyncio.gather(*services._background)
    asyncio.run(scenario())
    assert to(api, CLIENT_CHAT)[-1]['files'][0]['type'] == 'audio'
    assert collected[0]['manager_text'] == 'Ролик должен быть 15 секунд'
    assert to(api, CLIENT_CHAT)[-1]['text'] == ''


def test_telegram_observation_transcribes_both_sides_and_collects_candidates(telegram_env, monkeypatch):
    env = telegram_env
    enable(env.services, monkeypatch, 'Какая длительность ролика?')
    audio = {'file_id':'fake','file_unique_id':'u','duration':10}
    env.msg(None, 1, voice=audio)
    enable(env.services, monkeypatch, 'Ролик должен длиться 15 секунд.')
    env.msg(None, 2, owner=True, voice=audio)
    assert env.db.observation_stats()['messages'] == 2
    assert env.db.candidates()[0]['manager_text'] == 'Ролик должен длиться 15 секунд.'
    assert env.sent == []


def test_telegram_auto_voice_has_no_client_answer(telegram_env, monkeypatch):
    env = telegram_env
    from tests.test_observe import CONN as TCONN, OWNER
    from app.llm.mock import MockProvider
    env.services.db.set_mode('auto', connection_id=TCONN)
    env.services.assistant.llm = MockProvider()
    enable(env.services, monkeypatch)
    env.msg(None, 1, voice={'file_id':'fake','file_unique_id':'u','duration':10})
    assert env.sent and all(row['chat_id'] == OWNER for row in env.sent)
    assert 'Голосовое' in env.sent[-1]['text']


def test_revoked_observation_during_transcription_is_not_saved(telegram_env, monkeypatch):
    env = telegram_env
    from tests.test_observe import CONN as TCONN, CLIENT
    enable(env.services, monkeypatch)
    async def transcribe(*args):
        env.services.privacy.set_allowed(TCONN, CLIENT, False)
        return 'Личное сообщение'
    monkeypatch.setattr(voice, 'telegram_text', transcribe)
    env.msg(None, 1, voice={'file_id':'fake','file_unique_id':'u','duration':10})
    assert env.db.observation_stats()['messages'] == 0


@pytest.fixture
def max_env(request):
    return request.getfixturevalue('max_fixture')


@pytest.fixture
def telegram_env(request):
    return request.getfixturevalue('telegram_fixture')


def test_slow_voice_keeps_text_clients_moving_and_event_pending(max_env, monkeypatch):
    from app.channels.max_bot import handle_queue
    services, bot, api = max_env
    connect(bot)
    services.speech.enabled = True
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            started.set()
            await release.wait()
            return 'Сколько стоит экран в атриуме?'
        monkeypatch.setattr(voice, 'max_text', slow)
        queue = services.max_inbox
        worker = asyncio.create_task(handle_queue(bot, queue))
        try:
            queue.put_nowait(message('', attachments=AUDIO))
            await asyncio.wait_for(started.wait(), 1)
            queue.put_nowait(message('И ещё вопрос по этому ролику'))
            queue.put_nowait(message('Сколько стоит экран в атриуме?', chat=7770, user=777))
            for _ in range(100):
                if services.db.recent_dialogs(5):
                    break
                await asyncio.sleep(.005)
            assert services.db.recent_dialogs(5)[0]['chat_id'] == 7770
            assert queue.qsize() == 2
            release.set()
            await asyncio.wait_for(queue.join(), 2)
            dialogs = services.db.recent_dialogs(5)
            assert len(dialogs) == 3
            assert dialogs[0]['question'] == 'И ещё вопрос по этому ролику'
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, *bot.answer_tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_voice_cancel_keeps_event_for_restart(max_env, monkeypatch):
    from app.channels.max_bot import handle_queue
    from app.max_inbox import event_key
    services, bot, _ = max_env
    connect(bot)
    services.speech.enabled = True
    async def scenario():
        started = asyncio.Event()
        async def slow(*args):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(voice, 'max_text', slow)
        queue = services.max_inbox
        worker = asyncio.create_task(handle_queue(bot, queue))
        update = message('', attachments=AUDIO)
        queue.put_nowait(update)
        await asyncio.wait_for(started.wait(), 1)
        tasks = [worker, *bot.answer_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert queue.qsize() == 1 and not queue.active
        assert not bot.voice_tasks and not bot.answer_tasks
        assert event_key(update) not in bot.seen
    asyncio.run(scenario())


@pytest.mark.parametrize('channel', ['telegram', 'max'])
def test_disabled_connection_does_not_transcribe(channel, request, monkeypatch):
    async def forbidden(*args):
        pytest.fail('В выключенном подключении нельзя скачивать голосовое')
    monkeypatch.setattr(voice, 'telegram_text', forbidden)
    monkeypatch.setattr(voice, 'max_text', forbidden)
    if channel == 'telegram':
        from tests.test_observe import CONN as TCONN
        telegram_env = request.getfixturevalue('telegram_env')
        telegram_env.services.speech.enabled = True
        telegram_env.db.set_mode('off', connection_id=TCONN)
        telegram_env.msg(None, 1, voice={'file_id':'fake','file_unique_id':'u','duration':10})
    else:
        services, bot, _ = request.getfixturevalue('max_env')
        connect(bot)
        services.speech.enabled = True
        services.db.set_mode('off', connection_id=CONN)
        feed(bot, message('', attachments=AUDIO))
