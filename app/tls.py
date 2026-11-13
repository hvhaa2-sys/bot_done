"""Дополнительный CA только для официального MAX API, без изменения хранилища macOS."""
from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlsplit

import certifi


def max_verify(url: str) -> bool | ssl.SSLContext:
    if urlsplit(url).hostname not in {'platform-api2.max.ru', 'platform-api.max.ru'}:
        return True
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(Path(__file__).with_name('certs') / 'russian_trusted_root_ca.pem')
    return context
