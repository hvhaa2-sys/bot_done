#!/usr/bin/env bash
# Выкладка новой версии на сервере: бэкап данных → новая версия → проверка /health → при сбое откат кода и данных.
# Запускать на сервере из папки проекта (git-клон с настроенным .env):
#     ./deploy/release.sh v2026.11.07        # тег, ветка или коммит из origin
# Так же его вызывает по SSH workflow «Выкладка» (.github/workflows/deploy.yml). Инструкция — docs/CI-CD.md.
#
# Коды выхода: 0 — новая версия работает; 1 — новая версия не поднялась, вернули прошлую, она работает;
# 2 — не поднялась и прошлая: нужен человек (бэкап данных лежит в папке бэкапов, путь в журнале).
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
REF="${1:?Использование: deploy/release.sh <тег, ветка или коммит>}"
BACKUPS="${RELEASE_BACKUPS:-$ROOT/../backups}"
WAIT="${RELEASE_WAIT:-180}"
IMAGE=ai-sales-assistant:local
PREVIOUS=ai-sales-assistant:previous

compose() { docker compose -f "$ROOT/docker-compose.yml" "$@"; }
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$BACKUPS/releases.log"; }
healthy() {
  compose up -d --no-build --wait --wait-timeout "$WAIT" >/dev/null 2>&1 &&
    compose exec -T app python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" >/dev/null 2>&1
}

mkdir -p -- "$BACKUPS"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo 'В папке проекта на сервере есть правки в коде — выкладка остановлена, сначала разберите их.' >&2
  exit 1
fi
git fetch --quiet --tags origin
# Ветка — свежая из origin, тег и коммит — как есть
NEW="$(git rev-parse --verify --quiet "origin/$REF^{commit}" || git rev-parse --verify --quiet "$REF^{commit}")" ||
  { echo "Не нашёл «$REF» ни в тегах, ни в ветках origin." >&2; exit 1; }
OLD="$(git rev-parse HEAD)"

# 1. Бэкап данных старой версией: ./setup останавливает бота на время копии и запускает обратно
SNAP="$BACKUPS/before-${NEW:0:8}-$(date +%Y%m%d-%H%M%S).tar.gz"
./setup backup "$SNAP" >/dev/null
docker image inspect "$IMAGE" >/dev/null 2>&1 && docker image tag "$IMAGE" "$PREVIOUS"
log "выкладка $REF (${NEW:0:8}), было ${OLD:0:8}, бэкап $SNAP"

# 2. Новая версия
git checkout --quiet --detach "$NEW"
if compose build app >/dev/null && healthy; then
  log "готово: работает $REF (${NEW:0:8})"
  exit 0
fi

# 3. Откат: прошлый образ, прошлый код, данные из бэкапа (новая версия могла успеть изменить базу)
log "новая версия не прошла проверку — откат на ${OLD:0:8}"
compose stop app >/dev/null 2>&1 || true
git checkout --quiet --detach "$OLD"
if docker image inspect "$PREVIOUS" >/dev/null 2>&1; then
  docker image tag "$PREVIOUS" "$IMAGE"
else
  compose build app >/dev/null
fi
./setup restore "$SNAP" --yes >/dev/null
if healthy; then
  log "откат выполнен: работает ${OLD:0:8}, данные из $SNAP"
  exit 1
fi
log "ОТКАТ НЕ ПОДНЯЛСЯ — нужен человек. Код ${OLD:0:8}, бэкап данных $SNAP"
exit 2
