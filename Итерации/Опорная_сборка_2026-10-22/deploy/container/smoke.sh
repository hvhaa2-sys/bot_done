#!/usr/bin/env bash
# Отдельный Compose-проект, отдельный том, mock. Настоящие ключи и data/ не используются.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
export ASSISTANT_ENV_FILE="$TMP/smoke.env"
export ASSISTANT_PORT=0
PROJECT="assistant-smoke-$$"
compose() { docker compose -f "$ROOT/docker-compose.yml" --project-name "$PROJECT" --env-file "$ASSISTANT_ENV_FILE" "$@"; }
cleanup() { compose down --volumes --remove-orphans >/dev/null 2>&1 || true; rm -rf -- "$TMP"; }
trap cleanup EXIT
cat > "$ASSISTANT_ENV_FILE" <<'ENV'
LLM_PROVIDER=mock
TELEGRAM_BOT_TOKEN=
ADMIN_TOKEN=isolated-smoke-only
DEFAULT_MODE=draft
ENV

compose config --quiet
compose up -d --build --wait --wait-timeout 180
PORT="$(compose port app 8000)"
curl --fail --silent "http://$PORT/health" >/dev/null
compose exec -T app python -c 'from pathlib import Path; import sqlite3; p=Path("/app/data/smoke-state"); p.mkdir(); (p/"marker").write_text("original"); c=sqlite3.connect("/app/data/app.db"); c.execute("CREATE TABLE smoke(value TEXT)"); c.execute("INSERT INTO smoke VALUES (?)", ("original",)); c.commit()'

compose up -d --force-recreate --wait --wait-timeout 90
compose exec -T app python -c 'from pathlib import Path; assert Path("/app/data/smoke-state/marker").read_text()=="original"'
compose stop app
compose run --rm --no-deps -T app python -m tools.deploy backup --archive - > "$TMP/snapshot.tar.gz"
compose run --rm --no-deps -T app python -c 'from pathlib import Path; import sqlite3; Path("/app/data/smoke-state/marker").write_text("changed"); c=sqlite3.connect("/app/data/app.db"); c.execute("UPDATE smoke SET value=?", ("changed",)); c.commit(); Path("/app/data/after-snapshot").write_text("remove")'
compose run --rm --no-deps -T app python -m tools.deploy restore --archive - --yes < "$TMP/snapshot.tar.gz"
compose run --rm --no-deps -T app python -c 'from pathlib import Path; import sqlite3; assert Path("/app/data/smoke-state/marker").read_text()=="original"; assert not Path("/app/data/after-snapshot").exists(); c=sqlite3.connect("/app/data/app.db"); assert c.execute("SELECT value FROM smoke").fetchone()[0]=="original"; assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"'
compose up -d --wait --wait-timeout 90
PORT="$(compose port app 8000)"
curl --fail --silent "http://$PORT/health" >/dev/null
printf 'SMOKE OK: чистая установка, health, пересоздание контейнера, полный бэкап, восстановление SQLite и файлов.\n'
