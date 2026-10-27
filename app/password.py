"""Вход в админку по логину и паролю вместо токена.

Задать или сменить:  .venv/bin/python -m app.password
Команда спросит логин и пароль и запишет в .env строки ADMIN_LOGIN и ADMIN_PASSWORD_HASH. Сам пароль нигде
не хранится — только хеш PBKDF2-SHA256 со случайной солью. Ссылки с ?token= продолжают работать.
После смены перезапустите бота (deploy/mac-autostart.sh): настройки читаются при запуске.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

from .config import ROOT

ITERATIONS = 600_000  # ~0,3 с на вход: подбор пароля по украденному хешу становится дорогим
MIN_LENGTH = 8


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """«pbkdf2_sha256:итерации:соль:хеш» — без «$» и «=», чтобы строку не портили .env и docker compose."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, iterations, salt, digest = stored.split(":")
        if kind != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:  # пусто или строку повредили — не впускаем
        return False
    return hmac.compare_digest(check.hex(), digest)


def save_to_env(path: Path, login: str, password_hash: str) -> None:
    """Заменить или дописать ADMIN_LOGIN и ADMIN_PASSWORD_HASH, остальные строки .env не трогать."""
    values = {"ADMIN_LOGIN": login, "ADMIN_PASSWORD_HASH": password_hash}
    out = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        out.append(f"{key}={values.pop(key)}" if key in values else line)
    out += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)  # в .env ключи и токены: читать может только владелец


def main(env_path: Path = ROOT / ".env") -> int:
    login = input("Логин для админки: ").strip()
    if not login or " " in login:
        print("Логин не может быть пустым или с пробелами.")
        return 1
    password = getpass.getpass(f"Пароль (не меньше {MIN_LENGTH} символов, не отображается): ")
    if len(password) < MIN_LENGTH:
        print(f"Пароль короче {MIN_LENGTH} символов.")
        return 1
    if getpass.getpass("Повторите пароль: ") != password:
        print("Пароли не совпали.")
        return 1
    save_to_env(env_path, login, hash_password(password, iterations=ITERATIONS))
    print(f"Сохранено в {env_path}. Перезапустите бота (deploy/mac-autostart.sh) и входите в админку "
          f"по логину «{login}» и паролю.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
