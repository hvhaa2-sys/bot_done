"""Локальное распознавание в отдельном процессе, с ограничениями ресурсов."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .core.pii import mask_pd

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 120


class SpeechError(Exception):
    """Запись передаётся менеджеру обычным способом, без автоматического ответа."""


class AudioBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise SpeechError('Запись больше 20 МБ')
        return super().write(data)


async def download_audio(url: str) -> bytes:
    """Внешний URL вложения MAX. Авторизация бота сюда не передаётся."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.port not in (None, 443):
        raise SpeechError('Недопустимый адрес аудио')
    addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise SpeechError('Недопустимый адрес аудио')
    with AudioBuffer() as target:
        address = addresses[0][4][0]
        target_url = httpx.URL(url).copy_with(host=address)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream('GET', target_url, headers={'Host': parsed.hostname},
                                     extensions={'sni_hostname': parsed.hostname}) as response:
                if response.status_code != 200:
                    raise SpeechError('Не удалось скачать аудио')
                async for chunk in response.aiter_bytes():
                    target.write(chunk)
        return target.getvalue()


class SpeechRecognizer:
    def __init__(self, enabled=False, python='', timeout=180):
        self.enabled = enabled
        self.python = python or sys.executable
        self.timeout = timeout
        self.gate = asyncio.Semaphore(1)
        self.waiting = 0

    async def transcribe(self, audio: bytes, duration: float = 0) -> str:
        if not self.enabled:
            raise SpeechError('Распознавание выключено')
        if not audio or len(audio) > MAX_BYTES or duration > MAX_SECONDS:
            raise SpeechError('Запись пустая или превышает 2 минуты / 20 МБ')
        if self.waiting >= 4:
            raise SpeechError('Очередь распознавания заполнена')
        self.waiting += 1
        try:
            async with asyncio.timeout(self.timeout), self.gate:
                return await self._run(audio)
        except TimeoutError as exc:
            raise SpeechError('Истекло время распознавания') from exc
        finally:
            self.waiting -= 1

    async def _run(self, audio: bytes) -> str:
        with tempfile.TemporaryDirectory(prefix='sales-voice-') as directory:
            source = Path(directory) / 'audio.bin'
            source.write_bytes(audio)
            worker = Path(__file__).with_name('speech_worker.py')
            process = await asyncio.create_subprocess_exec(
                self.python, str(worker), str(source), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                stdout, _ = await process.communicate()
            finally:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if process.returncode:
                raise SpeechError('Не удалось распознать аудио')
            try:
                text = json.loads(stdout)['text']
            except (ValueError, KeyError, TypeError) as exc:
                raise SpeechError('Некорректный результат распознавания') from exc
            if not isinstance(text, str) or not text.strip():
                raise SpeechError('Речь не распознана')
            return mask_pd(text.strip())
