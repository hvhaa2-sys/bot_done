#!/bin/bash
# Автозапуск бота на Mac (launchd): стартует при входе в систему, сам поднимается после сбоя
# и не даёт ноутбуку уснуть от бездействия, пока работает. С закрытой крышкой Mac всё равно спит.
#
#   deploy/mac-autostart.sh        включить (и перезапустить бота после правок кода или .env)
#   deploy/mac-autostart.sh off    выключить
set -euo pipefail

LABEL=local.ai-sales-assistant
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ai-sales-assistant.log"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "off" ]; then
  rm -f "$PLIST"
  echo "Автозапуск выключен, бот остановлен."
  exit 0
fi
sleep 3  # старый экземпляр освобождает порт админки

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$DIR/.venv/bin/python</string><string>-m</string><string>app</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Бот запущен и дальше будет стартовать сам. Лог: $LOG"
