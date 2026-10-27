"""Вход в админку по логину и паролю: хеш в .env, форма входа, токен по-прежнему работает."""
from dataclasses import replace

from fastapi.testclient import TestClient

from app import password as P
from app.admin import create_app
from app.config import ROOT, Settings
from app.services import Services


def make_client(tmp_path, **extra):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    for name in ("faq.md", "prices.csv", "stop_topics.txt"):
        (kb_dir / name).write_text((ROOT / "tests" / "fixtures" / "kb" / name).read_text(encoding="utf-8"), encoding="utf-8")
    # Логин и пароль — только из теста: в настоящем .env они могут быть заданы, и форма стала бы другой
    extra = {"admin_login": "", "admin_password_hash": "", **extra}
    settings = replace(Settings.load(), db_path=tmp_path / "a.db", kb_dir=kb_dir, llm_provider="mock",
                       admin_token="secret", exam_files="", **extra)
    return TestClient(create_app(Services(settings)))


def test_hash_is_salted_and_verifiable():
    first, second = P.hash_password("пароль-123", iterations=1000), P.hash_password("пароль-123", iterations=1000)
    assert first != second, "соль разная — одинаковые пароли дают разные хеши"
    assert "пароль" not in first and "$" not in first and "=" not in first
    assert P.verify_password("пароль-123", first) and not P.verify_password("пароль-124", first)
    assert not P.verify_password("пароль-123", "") and not P.verify_password("x", "md5:1:aa:bb")


def test_login_with_password(tmp_path):
    client = make_client(tmp_path, admin_login="gleb", admin_password_hash=P.hash_password("верный-пароль", iterations=1000))
    form = client.get("/").text
    assert 'name="login"' in form and 'name="password"' in form and 'name="token"' not in form
    wrong = client.post("/login", data={"login": "gleb", "password": "не тот"})
    assert "Неверный логин или пароль" in wrong.text and client.get("/dialogs").status_code == 401
    assert "Неверный логин или пароль" in client.post("/login", data={"login": "anna", "password": "верный-пароль"}).text
    ok = client.post("/login", data={"login": " gleb ", "password": "верный-пароль"}, follow_redirects=False)
    assert ok.status_code == 303 and client.get("/dialogs").status_code == 200


def test_token_link_still_works_with_password(tmp_path):
    client = make_client(tmp_path, admin_login="gleb", admin_password_hash=P.hash_password("верный-пароль", iterations=1000))
    assert client.get("/?token=secret").status_code == 200


def test_without_password_form_asks_for_token(tmp_path):
    client = make_client(tmp_path)
    form = client.get("/").text
    assert 'name="token"' in form and "app.password" in form
    assert client.post("/login", data={"token": "secret"}, follow_redirects=False).status_code == 303


def test_command_saves_login_and_hash_to_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nADMIN_LOGIN=old\n# комментарий\n", encoding="utf-8")
    answers = iter(["новый-пароль", "новый-пароль"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "gleb")
    monkeypatch.setattr(P.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(P, "ITERATIONS", 1000)
    assert P.main(env) == 0
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "TELEGRAM_BOT_TOKEN=abc" and lines[1] == "ADMIN_LOGIN=gleb" and lines[2] == "# комментарий"
    stored = next(line.split("=", 1)[1] for line in lines if line.startswith("ADMIN_PASSWORD_HASH="))
    assert P.verify_password("новый-пароль", stored) and "новый-пароль" not in env.read_text(encoding="utf-8")
    assert env.stat().st_mode & 0o777 == 0o600


def test_command_rejects_short_or_mismatched_password(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr("builtins.input", lambda prompt="": "gleb")
    monkeypatch.setattr(P.getpass, "getpass", lambda prompt="": "short")
    assert P.main(env) == 1 and not env.exists()
    answers = iter(["длинный-пароль", "другой-пароль"])
    monkeypatch.setattr(P.getpass, "getpass", lambda prompt="": next(answers))
    assert P.main(env) == 1 and not env.exists()
