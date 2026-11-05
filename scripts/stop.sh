#!/bin/sh
# Arrête le serveur Alexa-YT.
cd "$(dirname "$0")/.."
PIDFILE="$(pwd)/alexa-yt.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"
  # Laisse yt-dlp finir proprement (jusqu'à 10 s) avant de forcer
  i=0
  while kill -0 "$(cat "$PIDFILE")" 2>/dev/null && [ $i -lt 10 ]; do sleep 1; i=$((i+1)); done
  kill -9 "$(cat "$PIDFILE")" 2>/dev/null
  echo "Alexa-YT arrêté."
else
  echo "Alexa-YT ne tournait pas."
fi
rm -f "$PIDFILE"
# Au cas où un exemplaire aurait été lancé à la main
pkill -f alexa_yt.py 2>/dev/null
exit 0
