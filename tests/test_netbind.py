import asyncio
import subprocess
import threading
from types import SimpleNamespace

import httpx
import pytest

from app import netbind


@pytest.mark.parametrize('platform,setting,url', [
    ('linux','auto','https://platform-api2.max.ru'),
    ('darwin','','https://platform-api2.max.ru'),
    ('darwin','auto','http://localhost:8000'),
    ('darwin','en0','http://127.0.0.1:8031'),
    ('darwin','en0','http://[::1]:8000'),
])
def test_disabled_or_local_transport_does_not_probe_interfaces(monkeypatch, platform, setting, url):
    monkeypatch.setattr(netbind.sys, 'platform', platform)
    monkeypatch.setattr(netbind, '_source', lambda _: pytest.fail('Не нужно проверять интерфейс'))
    assert netbind.transport(setting,url=url) is None


def test_interface_missing_at_start_can_recover(monkeypatch):
    monkeypatch.setattr(netbind.sys, 'platform', 'darwin')
    monkeypatch.setattr(netbind, '_source', lambda _: pytest.fail('Проверка должна быть при запросе'))
    assert isinstance(netbind.transport('auto'), netbind.DirectTransport)


def test_address_probe_handles_timeout_and_invalid_output(monkeypatch):
    monkeypatch.setattr(netbind.sys, 'platform', 'darwin')
    monkeypatch.setattr(netbind.socket, 'if_nametoindex', lambda _: 1)
    def run(args, **kwargs):
        assert args[0] == '/usr/sbin/ipconfig'
        if args[-1] == 'en0':
            raise subprocess.TimeoutExpired(args,3)
        return SimpleNamespace(returncode=0,stdout='172.20.10.5' if args[-1]=='en2' else 'not an IP')
    monkeypatch.setattr(netbind.subprocess, 'run', run)
    assert netbind._source('auto') == ('en2','172.20.10.5')


@pytest.fixture
def pools(monkeypatch):
    created=[]
    class Pool(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.options, self.closes, self.fail = kwargs, 0, False
            created.append(self)
        async def handle_async_request(self, request):
            if self.fail:
                raise httpx.ConnectError('connection failed')
            return httpx.Response(200,stream=httpx.ByteStream(b'ok'))
        async def aclose(self):
            self.closes += 1
    monkeypatch.setattr(netbind.httpx, 'AsyncHTTPTransport', Pool)
    return created


def test_refresh_keeps_active_response_and_closes_old_pool_after_read(monkeypatch, pools):
    direct=netbind.DirectTransport('auto',True)
    addresses=iter(['172.20.10.5','192.168.1.5'])
    monkeypatch.setattr(direct,'_current',lambda: next(addresses))
    async def scenario():
        request=httpx.Request('GET','https://platform-api2.max.ru/me')
        first=await direct.handle_async_request(request)
        direct.checked=-float('inf')
        second=await direct.handle_async_request(request)
        assert pools[0].closes == 0
        assert pools[1].options['local_address'] == '192.168.1.5'
        await first.aread()
        await first.aclose()
        assert pools[0].closes == 1
        await second.aclose()
        await direct.aclose()
        await direct.aclose()
        assert pools[1].closes == 1
    asyncio.run(scenario())


def test_wifi_loss_and_return_use_fresh_addresses(monkeypatch, pools):
    direct=netbind.DirectTransport('auto',True)
    addresses=iter(['','192.168.1.5',''])
    monkeypatch.setattr(direct,'_current',lambda: next(addresses))
    async def scenario():
        async with httpx.AsyncClient(transport=direct) as client:
            for expected in (None,'192.168.1.5',None):
                direct.checked=-float('inf')
                assert (await client.get('https://api.giga.chat/v1/models')).text == 'ok'
                assert pools[-1].options.get('local_address') == expected
        assert all(p.closes==1 for p in pools)
    asyncio.run(scenario())


def test_concurrent_requests_probe_once_without_blocking_loop(monkeypatch, pools):
    direct=netbind.DirectTransport('auto',True)
    started, release = threading.Event(), threading.Event()
    calls=[]
    def current():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return '192.168.1.5'
    monkeypatch.setattr(direct,'_current',current)
    async def scenario():
        async with httpx.AsyncClient(transport=direct) as client:
            tasks=[asyncio.create_task(client.get('https://api.giga.chat/v1/models')) for _ in range(5)]
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.005)
                assert started.is_set() and not any(t.done() for t in tasks)
            finally:
                release.set()
            await asyncio.gather(*tasks)
        assert calls == [1] and len(pools)==1
    asyncio.run(scenario())


def test_failed_request_releases_pool_without_retry(monkeypatch, pools):
    direct=netbind.DirectTransport('auto',True)
    monkeypatch.setattr(direct,'_current',lambda: '192.168.1.5')
    async def scenario():
        pool=await direct._acquire()
        await direct._release(pool)
        pool.fail=True
        with pytest.raises(httpx.ConnectError):
            await direct.handle_async_request(httpx.Request('POST','https://platform-api2.max.ru/messages'))
        assert direct.users[pool]==0
        await direct.aclose()
    asyncio.run(scenario())


def test_local_request_bypasses_binding_inside_shared_gigachat_client(monkeypatch, pools):
    direct=netbind.DirectTransport('auto',True)
    monkeypatch.setattr(direct,'_current',lambda: pytest.fail('Локальному запросу не нужен Wi-Fi'))
    async def scenario():
        async with httpx.AsyncClient(transport=direct) as client:
            assert (await client.get('http://localhost:8080/models')).text=='ok'
        assert 'local_address' not in pools[0].options
    asyncio.run(scenario())
