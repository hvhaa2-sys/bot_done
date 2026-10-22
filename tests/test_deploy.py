"""Полный перенос состояния и отказ от повреждённых архивов без изменения данных."""
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools.deploy import create_backup, init_env, read_env, restore_backup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path):
    data = tmp_path / "data"
    for path, text in {"kb/faq.md": "## Срок?\nСогласовать с менеджером", "kb_versions/v1/faq.md": "old",
                       "kb_versions/active.json": '{"version":"v1"}', "backups/readme.txt": "keep"}.items():
        p = data / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    with sqlite3.connect(data / "app.db") as db:
        db.execute("CREATE TABLE facts (text TEXT)")
        db.execute("INSERT INTO facts VALUES ('подтверждено')")
    return data


def test_full_backup_restores_db_kb_versions_and_removes_later_files(state, tmp_path):
    archive = tmp_path / "backup.tar.gz"
    original = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file() and p.suffix != ".db"}
    create_backup(state, archive)
    (state / "kb/faq.md").write_text("изменено")
    (state / "new.txt").write_text("после снимка")
    with sqlite3.connect(state / "app.db") as db:
        db.execute("DELETE FROM facts")
    restore_backup(state, archive, yes=True)
    assert not (state / "new.txt").exists()
    assert {k: (state / k).read_bytes() for k in original} == original
    with sqlite3.connect(state / "app.db") as db:
        assert db.execute("SELECT text FROM facts").fetchall() == [("подтверждено",)]
    assert archive.stat().st_mode & 0o777 == 0o600


def test_backup_includes_committed_wal(state, tmp_path):
    db = sqlite3.connect(state / "app.db")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO facts VALUES ('WAL')")
        db.commit()
        archive = tmp_path / "backup.tar.gz"
        create_backup(state, archive)
    finally:
        db.close()
    target = tmp_path / "restored"
    restore_backup(target, archive, yes=True)
    with sqlite3.connect(target / "app.db") as conn:
        assert conn.execute("SELECT text FROM facts ORDER BY rowid").fetchall()[-1] == ("WAL",)
    assert not (target / "app.db-wal").exists()


def test_requires_explicit_restore_and_rejects_recursive_archive(state):
    with pytest.raises(ValueError, match="--yes"):
        restore_backup(state, state / "missing.tar.gz")
    with pytest.raises(ValueError, match="вне data"):
        create_backup(state, state / "backups/recursive.tar.gz")


def write_tar(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.linkname = tarfile.SYMTYPE, "/tmp/escape"
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "data/../../escape", "data/link"])
def test_unsafe_archive_does_not_change_state(state, tmp_path, name):
    archive = tmp_path / "unsafe.tar.gz"
    write_tar(archive, [(name, None if name.endswith("link") else b"bad")])
    before = (state / "kb/faq.md").read_bytes()
    with pytest.raises(ValueError):
        restore_backup(state, archive, yes=True)
    assert (state / "kb/faq.md").read_bytes() == before


def test_corrupted_payload_does_not_change_working_data(state, tmp_path):
    archive = tmp_path / "corrupt.tar.gz"
    manifest = {"format": 1, "files": [{"path": "kb/faq.md", "size": 3, "sha256": "wrong"}]}
    write_tar(archive, [("manifest.json", json.dumps(manifest).encode()), ("data/kb/faq.md", b"bad")])
    before = (state / "kb/faq.md").read_bytes()
    with pytest.raises(ValueError, match="Контрольная сумма"):
        restore_backup(state, archive, yes=True)
    assert (state / "kb/faq.md").read_bytes() == before


def test_init_env_preserves_secret_characters_and_never_overwrites(tmp_path):
    path = tmp_path / ".env"
    init_env(path, ROOT / ".env.example", auth_key="key$with#symbols=", admin_token="admin-safe")
    assert read_env(path)["GIGACHAT_AUTH_KEY"] == "key$with#symbols="
    before = path.read_bytes()
    with pytest.raises(ValueError, match="уже есть"):
        init_env(path, ROOT / ".env.example", demo=True)
    assert path.read_bytes() == before and path.stat().st_mode & 0o777 == 0o600


def test_setup_demo_has_no_network_or_real_keys(tmp_path):
    path = tmp_path / ".env.demo"
    result = subprocess.run([str(ROOT / "setup"), "--demo", "--non-interactive", "--env-file", str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    env = read_env(path)
    assert env["LLM_PROVIDER"] == "mock" and not env["TELEGRAM_BOT_TOKEN"] and not env["GIGACHAT_AUTH_KEY"]
    assert env["ADMIN_TOKEN"] != "change-me" and env["ADMIN_TOKEN"] not in result.stdout


def test_container_seeds_kb_once_and_honors_intentionally_empty_kb(tmp_path):
    spec = importlib.util.spec_from_file_location("container_entry", ROOT / "deploy/container/entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "faq.md").write_text("seed")
    data = tmp_path / "new"
    module.initialize(data, seed)
    (data / "kb/faq.md").write_text("edited")
    module.initialize(data, seed)
    assert (data / "kb/faq.md").read_text() == "edited"
    empty = tmp_path / "empty"
    (empty / "kb").mkdir(parents=True)
    module.initialize(empty, seed)
    assert list((empty / "kb").iterdir()) == []


def test_backup_stdout_can_be_restored_from_stdin(state, tmp_path):
    proc = subprocess.run([os.sys.executable, "-m", "tools.deploy", "backup", "--data-dir", str(state),
                           "--archive", "-"], cwd=ROOT, capture_output=True)
    assert proc.returncode == 0 and proc.stdout.startswith(b"\x1f\x8b"), proc.stderr
    target = tmp_path / "stdin-restore"
    restored = subprocess.run([os.sys.executable, "-m", "tools.deploy", "restore", "--data-dir", str(target),
                               "--archive", "-", "--yes"], input=proc.stdout, cwd=ROOT, capture_output=True)
    assert restored.returncode == 0, restored.stderr
    assert (target / "kb_versions/active.json").read_text() == '{"version":"v1"}'


def test_setup_restarts_previously_running_app_when_backup_fails(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls"
    docker = fake_bin / "docker"
    docker.write_text('''#!/bin/bash
if [[ "$1" == info ]]; then exit 0; fi
if [[ "$*" == *"version --short"* ]]; then echo 2.30.0; exit 0; fi
if [[ "$*" == *"ps --status running"* ]]; then echo app; exit 0; fi
echo "$*" >> "$FAKE_LOG"
if [[ "$*" == *"run --rm"* ]]; then echo "backup failed" >&2; exit 1; fi
exit 0
''')
    docker.chmod(0o755)
    target = tmp_path / "backup.tar.gz"
    env = {**os.environ, "PATH": str(fake_bin) + ":" + os.environ["PATH"], "FAKE_LOG": str(log)}
    result = subprocess.run([str(ROOT / "setup"), "backup", str(target)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    calls = log.read_text()
    assert "stop app" in calls and "start app" in calls
    assert not target.exists() and not list(tmp_path.glob("*.partial.*"))
