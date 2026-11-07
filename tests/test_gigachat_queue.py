"""Общий лимит GigaChat для разных потребителей и экземпляров провайдера, без сети."""
import asyncio
import time

import httpx

from app.llm.gigachat import GigaChatProvider


def test_all_consumers_wait_for_one_slot_and_reuse_token():
    async def scenario():
        inflight = maximum = oauth = 0

        async def transport(request):
            nonlocal inflight, maximum, oauth
            if 'oauth' in str(request.url):
                oauth += 1
                return httpx.Response(200, json={'access_token': 'fake', 'expires_at': (time.time() + 1800) * 1000})
            inflight += 1
            maximum = max(maximum, inflight)
            await asyncio.sleep(.005)
            inflight -= 1
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ответ'}}]})

        a = GigaChatProvider('fake', 'scope', 'model', True, transport=httpx.MockTransport(transport))
        b = GigaChatProvider('fake', 'scope', 'model2', True, transport=httpx.MockTransport(transport))
        try:
            replies = await asyncio.gather(*(p.complete('', []) for p in [a, b, a, b, a, a, a, a]))
            assert replies == ['ответ'] * 8
            assert maximum == 1 and oauth == 2
        finally:
            await a.close()
            await b.close()
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_take_or_leak_slot():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def transport(request):
            nonlocal calls
            if 'oauth' in str(request.url):
                return httpx.Response(200, json={'access_token': 'fake'})
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

        p = GigaChatProvider('fake', 'scope', 'model', True, transport=httpx.MockTransport(transport))
        try:
            first = asyncio.create_task(p.complete('', []))
            await started.wait()
            cancelled = asyncio.create_task(p.complete('', []))
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            release.set()
            await first
            assert await p.complete('', []) == 'ok'
            assert calls == 2
        finally:
            await p.close()
    asyncio.run(scenario())


def test_admin_exam_uses_shared_provider_and_published_snapshot(tmp_path, monkeypatch):
    from app.admin import run_exam, last_exam
    from tests.test_admin import make_client
    _, services = make_client(tmp_path)
    original_version, original_kb, original_llm = services.kb_version, services.kb, services.llm

    async def evaluate(label, settings, questions, progress, *, llm, kb):
        assert llm is original_llm and kb is original_kb
        services.kb_version += 1  # другой запрос опубликовал новую версию во время экзамена
        return {'questions': 1}, []
    monkeypatch.setattr('eval.run_eval.evaluate', evaluate)
    state = {'running': True}
    asyncio.run(run_exam(services, state))
    assert 'error' not in state
    assert last_exam(services)['kb_version'] == original_version
