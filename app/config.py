"""Настройки приложения из переменных окружения и файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env без внешних зависимостей."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        # Переменная окружения важнее .env, но пустая (например, «export TOKEN=» в профиле)
        # не должна перекрывать заполненное значение из файла
        if not os.environ.get(key):
            os.environ[key] = value


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    telegram_bot_token: str
    llm_provider: str
    gigachat_auth_key: str
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_ssl: bool
    gigachat_base_url: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    openai_project: str
    default_mode: str
    retrieval_min_score: float
    confidence_threshold: float
    manager_pause_minutes: int
    stale_message_minutes: int
    admin_token: str
    admin_host: str
    admin_port: int
    db_path: Path
    kb_dir: Path
    exam_files: str = "eval/questions*.csv"  # контрольные вопросы перед публикацией базы; пусто — без них
    max_bot_token: str = ""  # второй канал — бот организации в MAX; пусто — MAX выключен
    max_api_url: str = ""    # пусто — штатный адрес MAX Bot API
    direct_interface: str = ""  # Mac с VPN: MAX и GigaChat напрямую через этот интерфейс (auto, en0), app/netbind.py
    max_webhook_url: str = ""     # https://домен/max/webhook; пусто — долгий опрос (для проверки, MAX просит вебхук)
    max_webhook_secret: str = ""  # MAX шлёт его в заголовке, чужие запросы отбрасываем
    telegram_api_url: str = ""  # свой релей к Telegram: с российских серверов api.telegram.org не отвечает
    admin_login: str = ""          # вход в админку по логину и паролю (python -m app.password); пусто — по токену
    speech_enabled: bool = False
    speech_python: str = ""
    admin_password_hash: str = ""
    admin_allowed_origins: str = ""
    gigachat_ca_bundle: str = ""  # путь к корневому CA Минцифры; пусто — проверка по GIGACHAT_VERIFY_SSL

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        env = os.environ.get

        def path(key: str, default: str) -> Path:
            p = Path(env(key, default))
            return p if p.is_absolute() else ROOT / p

        return cls(
            telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
            llm_provider=env("LLM_PROVIDER", "mock").lower(),
            gigachat_auth_key=env("GIGACHAT_AUTH_KEY", ""),
            gigachat_scope=env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_model=env("GIGACHAT_MODEL", "GigaChat"),
            gigachat_verify_ssl=_bool(env("GIGACHAT_VERIFY_SSL", "false")),
            gigachat_base_url=env("GIGACHAT_BASE_URL", ""),
            openai_base_url=env("OPENAI_BASE_URL", ""),
            openai_api_key=env("OPENAI_API_KEY", ""),
            openai_model=env("OPENAI_MODEL", ""),
            openai_project=env("OPENAI_PROJECT", ""),
            default_mode=env("DEFAULT_MODE", "draft"),
            retrieval_min_score=float(env("RETRIEVAL_MIN_SCORE", "1.0")),
            confidence_threshold=float(env("CONFIDENCE_THRESHOLD", "0.7")),
            manager_pause_minutes=int(env("MANAGER_PAUSE_MINUTES", "30")),
            stale_message_minutes=int(env("STALE_MESSAGE_MINUTES", "10")),
            admin_token=env("ADMIN_TOKEN", "change-me"),
            admin_host=env("ADMIN_HOST", "127.0.0.1"),
            admin_port=int(env("ADMIN_PORT", "8000")),
            db_path=path("DB_PATH", "data/app.db"),
            kb_dir=path("KB_DIR", "data/kb"),
            exam_files=env("EXAM_FILES", "eval/questions*.csv"),
            max_bot_token=env("MAX_BOT_TOKEN", ""),
            max_api_url=env("MAX_API_URL", ""),
            direct_interface=env("DIRECT_INTERFACE", ""),
            max_webhook_url=env("MAX_WEBHOOK_URL", ""),
            max_webhook_secret=env("MAX_WEBHOOK_SECRET", ""),
            telegram_api_url=env("TELEGRAM_API_URL", ""),
            admin_login=env("ADMIN_LOGIN", ""),
            speech_enabled=_bool(env("SPEECH_ENABLED", "false")),
            speech_python=env("SPEECH_PYTHON", ""),
            admin_password_hash=env("ADMIN_PASSWORD_HASH", ""),
            admin_allowed_origins=env("ADMIN_ALLOWED_ORIGINS", ""),
            gigachat_ca_bundle=env("GIGACHAT_CA_BUNDLE", ""),
        )
