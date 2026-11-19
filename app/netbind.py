"""Прямой выход мимо VPN для российских сервисов (MAX, GigaChat), когда бот работает на Mac с включённым VPN.

На Mac пользователя весь трафик идёт через Happ с выходом за границей, а MAX и GigaChat с иностранного адреса
не отвечают. Настройки VPN не трогаем: бот сам указывает для своих соединений с MAX и GigaChat исходящий адрес
физического интерфейса (Wi-Fi, en0). На текущем Mac с Happ это позволяет подключиться к обоим API;
поведение с другими VPN зависит от их маршрутизации. Остальные клиенты приложения не меняются.
Проверено 13.11.2026: с адресом en0 MAX и GigaChat отвечают при включённом Happ.

Адрес Wi-Fi меняется при смене сети, поэтому транспорт перепроверяет его раз в 30 секунд и пересоздаётся.
DIRECT_INTERFACE=auto — первый en0…en9 с адресом IPv4, en0 — конкретный интерфейс, пусто — выключено.
Работает только на macOS; на сервере в РФ и в Docker не нужно и игнорируется.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


def _source(setting: str) -> tuple[str, str]:
    """Имя и IPv4 интерфейса; вызывается вне цикла обработки сообщений."""
    setting = (setting or "").strip()
    if sys.platform != "darwin" or not setting:
        return "", ""
    names = [f"en{i}" for i in range(10)] if setting == "auto" else [setting]
    for name in names:
        try:
            socket.if_nametoindex(name)
            result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", name], capture_output=True, text=True,
                                    timeout=3)
            address = ipaddress.IPv4Address(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and not (address.is_unspecified or address.is_loopback):
            return name, str(address)
    log.warning("DIRECT_INTERFACE=%s: интерфейс с адресом не найден — запросы пойдут как обычно", setting)
    return "", ""


def interface(setting: str) -> str:
    return _source(setting)[0]


def transport(setting: str, verify: bool | str | ssl.SSLContext = True, url: str = "") -> httpx.AsyncBaseTransport | None:
    """Транспорт httpx с исходящим адресом интерфейса. Локальные адреса (песочница MAX на 127.0.0.1) не трогаем."""
    host = urlparse(url).hostname or ""
    if _local(host):
        return None
    if sys.platform != "darwin" or not setting.strip():
        return None
    return DirectTransport(setting, verify)


class DirectTransport(httpx.AsyncBaseTransport):
    """Запросы с исходящим адресом интерфейса; адрес пропал (Wi-Fi выключили) — как обычно, через систему."""
    RECHECK_SECONDS = 30

    def __init__(self, setting: str, verify: bool | str | ssl.SSLContext):
        self.setting, self.verify = setting, verify
        self.address, self.checked = "", 0.0
        self.inner: httpx.AsyncHTTPTransport | None = None
        self.local: httpx.AsyncHTTPTransport | None = None
        self.lock = asyncio.Lock()
        self.users: dict[httpx.AsyncBaseTransport, int] = {}
        self.retired: set[httpx.AsyncBaseTransport] = set()
        self.closed = False

    def _current(self) -> str:
        return _source(self.setting)[1]

    async def _acquire(self, local: bool = False) -> httpx.AsyncBaseTransport:
        async with self.lock:
            if self.closed:
                raise RuntimeError('Транспорт закрыт')
            if local:
                if self.local is None:
                    self.local = httpx.AsyncHTTPTransport(verify=self.verify)
                    self.users[self.local] = 0
                self.users[self.local] += 1
                return self.local
            if self.inner is None or time.monotonic() - self.checked >= self.RECHECK_SECONDS:
                address = await asyncio.to_thread(self._current)
                self.checked = time.monotonic()
                if self.inner is None or address != self.address:
                    old = self.inner
                    self.inner = httpx.AsyncHTTPTransport(verify=self.verify, local_address=address or None)
                    self.users[self.inner] = 0
                    if old is not None:
                        if self.users[old]:
                            self.retired.add(old)
                        else:
                            await old.aclose()
                            self.users.pop(old)
                    if address != self.address:
                        log.info('MAX и GigaChat: исходящий адрес %s', address or 'системный (интерфейса нет)')
                    self.address = address
            self.users[self.inner] += 1
            return self.inner

    async def _release(self, inner: httpx.AsyncBaseTransport) -> None:
        async with self.lock:
            if inner not in self.users:
                return
            self.users[inner] -= 1
            if inner in self.retired and self.users[inner] == 0:
                self.retired.remove(inner)
                self.users.pop(inner)
                await inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inner = await self._acquire(_local(request.url.host))
        try:
            response = await inner.handle_async_request(request)
        except BaseException:
            await self._release(inner)
            raise
        response.stream = _Stream(response.stream, self, inner)
        return response

    async def aclose(self) -> None:
        async with self.lock:
            self.closed = True
            pools = list(self.users)
            self.users.clear()
            self.retired.clear()
        await asyncio.gather(*(pool.aclose() for pool in pools))


class _Stream(httpx.AsyncByteStream):
    """Старый пул живёт, пока не дочитан ответ, в том числе долгий опрос MAX."""
    def __init__(self, stream, owner, pool):
        self.stream, self.owner, self.pool = stream, owner, pool
        self.closed = False

    async def __aiter__(self):
        try:
            async for data in self.stream:
                yield data
        finally:
            await self.aclose()

    async def aclose(self):
        if not self.closed:
            self.closed = True
            try:
                await self.stream.aclose()
            finally:
                await self.owner._release(self.pool)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _local(host: str) -> bool:
    return host.lower().rstrip('.') == 'localhost' or (_is_ip(host) and ipaddress.ip_address(host).is_loopback)
