"""Настройка .env и полный переносимый бэкап data/. Docker вызывает ./setup."""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import getpass
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 512 * 1024 * 1024


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#") and "=" in line)


def init_env(path: Path, example: Path, *, demo: bool = False, force: bool = False,
             auth_key: str = "", bot_token: str = "", admin_token: str = "") -> bool:
    if path.exists() and not force:
        raise ValueError("Файл настроек уже есть. Он не изменён. Для новой конфигурации укажите другой --env-file")
    values = {"LLM_PROVIDER": "mock" if demo else "gigachat", "GIGACHAT_AUTH_KEY": auth_key,
              "TELEGRAM_BOT_TOKEN": bot_token, "ADMIN_TOKEN": admin_token or secrets.token_urlsafe(32)}
    if not demo and not auth_key:
        raise ValueError("Для GigaChat нужен ключ. Для проверки без сети используйте --demo")
    if any("\n" in v or "\r" in v for v in values.values()):
        raise ValueError("Значение настройки должно занимать одну строку")
    if values["ADMIN_TOKEN"] == "change-me":
        raise ValueError("Задайте собственный ADMIN_TOKEN")
    lines, seen = [], set()
    for line in example.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            line = key + "=" + values[key]
            seen.add(key)
        lines.append(line)
    lines.extend(k + "=" + v for k, v in values.items() if k not in seen)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(name, 0o600)
        if force:
            os.replace(name, path)
        else:
            os.link(name, path)  # не затирает файл, появившийся пока шёл мастер
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def _sqlite_copy(source: Path, target: Path) -> None:
    # backup() переносит в основной файл и подтверждённые транзакции из WAL.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite не прошёл integrity_check: " + source.name)


def create_backup(data: Path, archive: Path) -> dict:
    data, archive = data.resolve(), archive.resolve()
    if not data.is_dir():
        raise ValueError("Папка data не найдена")
    if archive.is_relative_to(data):
        raise ValueError("Архив должен находиться вне data/, иначе попадёт в собственную копию")
    if archive.exists():
        raise ValueError("Архив уже существует: выберите новое имя")
    with tempfile.TemporaryDirectory(prefix="assistant-backup-") as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        paths = sorted(data.rglob("*"))
        for src in paths:
            if src.is_symlink():
                raise ValueError("Символические ссылки в data/ не поддерживаются: " + str(src.relative_to(data)))
            if not src.is_file():
                if not src.is_dir():
                    raise ValueError("В data/ найден специальный файл")
                (stage / src.relative_to(data)).mkdir(parents=True, exist_ok=True)
                continue
            if src.name.endswith(("-wal", "-shm", "-journal")):
                base = src.with_name(src.name.rsplit("-", 1)[0])
                if base.is_file():
                    with base.open("rb") as f:
                        if f.read(16) == b"SQLite format 3\x00":
                            continue
            dst = stage / src.relative_to(data)
            dst.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as f:
                is_db = f.read(16) == b"SQLite format 3\x00"
            if is_db:
                _sqlite_copy(src, dst)
            else:
                shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
        files = [{"path": str(p.relative_to(stage)), "sha256": digest(p), "size": p.stat().st_size}
                 for p in sorted(stage.rglob("*")) if p.is_file()]
        if sum(f["size"] for f in files) > MAX_BYTES:
            raise ValueError("Снимок превышает лимит 512 МБ")
        manifest = {"format": 1, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files}
        archive.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".backup-", dir=archive.parent)
        os.close(fd)
        try:
            with tarfile.open(name, "w:gz") as out:
                content = json.dumps(manifest, ensure_ascii=False).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode = len(content), 0o600
                out.addfile(info, io.BytesIO(content))
                out.add(stage, arcname="data", recursive=True)
            os.chmod(name, 0o600)
            os.link(name, archive)
        finally:
            Path(name).unlink(missing_ok=True)
    return manifest


def _unpack(archive: Path, stage: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name.rstrip("/") for m in members]
        if len(set(names)) != len(names):
            raise ValueError("Повторяющиеся пути в архиве")
        if sum(m.size for m in members) > MAX_BYTES:
            raise ValueError("Архив превышает лимит распаковки 512 МБ")
        for m in members:
            path = PurePosixPath(m.name)
            if path.is_absolute() or ".." in path.parts or "\\" in m.name:
                raise ValueError("Небезопасный путь в архиве")
            if m.name != "manifest.json" and (not path.parts or path.parts[0] != "data"):
                raise ValueError("Неизвестный файл в корне архива")
            if not (m.isfile() or m.isdir()):
                raise ValueError("Ссылки и специальные файлы в архиве запрещены")
            dst = stage / path
            if m.isdir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, dst.open("wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dst, 0o600)
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Неизвестный формат бэкапа")
    root = stage / "data"
    expected = {}
    for f in manifest["files"]:
        rel = PurePosixPath(f["path"])
        if rel.is_absolute() or ".." in rel.parts or str(rel) in expected:
            raise ValueError("Неверный список файлов в манифесте")
        expected[str(rel)] = f
    actual = {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}
    if set(expected) != set(actual):
        raise ValueError("Список файлов архива не совпадает с манифестом")
    for name, p in actual.items():
        if p.stat().st_size != expected[name]["size"] or digest(p) != expected[name]["sha256"]:
            raise ValueError("Контрольная сумма не совпала: " + name)
        with p.open("rb") as f:
            is_db = f.read(16) == b"SQLite format 3\x00"
        if is_db:
            with closing(sqlite3.connect(p)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Повреждённый SQLite в архиве")
    if not root.is_dir():
        raise ValueError("В архиве нет data/")
    return manifest


def restore_backup(data: Path, archive: Path, *, yes: bool = False) -> dict:
    if not yes:
        raise ValueError("Восстановление заменит data/. Остановите приложение и добавьте --yes")
    data = data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    # Всё проверяется до первого изменения рабочих файлов.
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=data) as tmp:
        stage = Path(tmp)
        manifest = _unpack(archive, stage)
        old = stage / "previous"
        old.mkdir()
        moved_old, moved_new = [], []
        try:
            for p in list(data.iterdir()):
                if p == stage:
                    continue
                p.rename(old / p.name)
                moved_old.append(p.name)
            for p in (stage / "data").iterdir():
                p.rename(data / p.name)
                moved_new.append(p.name)
        except BaseException:
            for name in moved_new:
                p = data / name
                p.rename(stage / "data" / name)
            for name in moved_old:
                (old / name).rename(data / name)
            raise
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Подготовить .env, ключи не выводятся")
    init.add_argument("--env-file", type=Path, default=Path(".env"))
    init.add_argument("--example", type=Path, default=Path(".env.example"))
    init.add_argument("--demo", action="store_true")
    init.add_argument("--non-interactive", action="store_true")
    for command in ["backup", "restore"]:
        p = sub.add_parser(command)
        p.add_argument("--data-dir", type=Path, default=Path("/app/data"))
        p.add_argument("--archive", required=True, help="Путь к архиву или - для stdin/stdout")
        if command == "restore":
            p.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            key = bot = admin = ""
            if not args.demo:
                if args.non_interactive:
                    parser.error("Для настройки без диалога доступен только --demo")
                key = getpass.getpass("Ключ авторизации GigaChat: ").strip()
                bot = getpass.getpass("Токен Telegram (Enter — только админка): ").strip()
            if not args.non_interactive:
                admin = getpass.getpass("Токен админки (Enter — создать случайный): ").strip()
            init_env(args.env_file, args.example, demo=args.demo, auth_key=key, bot_token=bot, admin_token=admin)
            print("Настройки сохранены с правами 600. ADMIN_TOKEN находится в выбранном .env.")
        else:
            with tempfile.TemporaryDirectory(prefix="assistant-archive-") as tmp:
                path = Path(tmp) / "snapshot.tar.gz" if args.archive == "-" else Path(args.archive)
                if args.action == "restore" and args.archive == "-":
                    with path.open("wb") as out:
                        shutil.copyfileobj(sys.stdin.buffer, out)
                result = (create_backup(args.data_dir, path) if args.action == "backup"
                          else restore_backup(args.data_dir, path, yes=args.yes))
                if args.action == "backup" and args.archive == "-":
                    with path.open("rb") as src:
                        shutil.copyfileobj(src, sys.stdout.buffer)
                print(f"{args.action}: файлов {len(result['files'])}, проверка пройдена", file=sys.stderr)
    except (ValueError, OSError, sqlite3.DatabaseError, tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
