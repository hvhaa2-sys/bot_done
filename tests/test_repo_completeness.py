"""Свежий клон и Docker-сборка не должны терять нужные файлы и настройки.

Проверки только читают файлы репозитория: сеть, Docker и git не нужны.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Импортируются приложением или тестами, но однажды могли выпасть из git как untracked.
REQUIRED_FILES = (
    "app/admin_guard.py",
    "app/core/price_options.py",
    "app/certs/russian_trusted_root_ca.pem",
    "app/certs/README.md",
    "app/tls.py",
    "tests/admin_client.py",
    "tests/test_admin_guard.py",
    "tests/test_max_quality.py",
    "tests/test_tls.py",
    "deploy/container/entrypoint.py",
    ".env.example",
    ".dockerignore",
    "Dockerfile",
)

# Настройки, которые читает не только Settings: сервисы, голос и Docker Compose.
RUNTIME_ENV = (
    "TIMEZONE",
    "SPEECH_MODEL_DIR",
    "ASSISTANT_PORT",
    "ASSISTANT_ENV_FILE",
)

CONFIG_ENV_RE = re.compile(r"""(?:env|path)\(\s*["']([A-Z][A-Z0-9_]*)["']""")
EXAMPLE_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_required_files_exist():
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    assert not missing, "В репозитории нет нужных файлов: " + ", ".join(missing)


def test_russian_root_ca_is_not_ignored():
    pem = ROOT / "app/certs/russian_trusted_root_ca.pem"
    assert pem.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")
    ignore = _read(".gitignore").splitlines()
    assert "*.pem" in ignore
    assert "!app/certs/*.pem" in ignore, (
        "Сертификат Минцифры нужен в чистом клоне, но правило *.pem его исключает. "
        "Добавьте в .gitignore строку !app/certs/*.pem"
    )
    dockerignore = _read(".dockerignore").splitlines()
    assert "!app/certs/*.pem" in dockerignore, (
        "Сертификат не попадёт в Docker-образ: в .dockerignore нужно !app/certs/*.pem"
    )
    dockerfile = _read("Dockerfile")
    assert "COPY app ./app" in dockerfile
    assert "test -f /app/app/certs/russian_trusted_root_ca.pem" in dockerfile, (
        "Dockerfile должен проверять, что сертификат Минцифры попал в образ"
    )


def test_env_example_covers_settings():
    expected = set(CONFIG_ENV_RE.findall(_read("app/config.py"))) | set(RUNTIME_ENV)
    example = set(EXAMPLE_LINE_RE.findall(_read(".env.example")))
    missing = sorted(expected - example)
    assert not missing, "В .env.example нет переменных, которые читает приложение: " + ", ".join(missing)


def test_deploy_bundle_keeps_certificate():
    from tools.deploy import BUNDLE_CODE

    assert "app/certs/russian_trusted_root_ca.pem" in BUNDLE_CODE, (
        "tools.deploy bundle собирает код через git archive: добавьте сертификат в BUNDLE_CODE"
    )
