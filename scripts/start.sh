#!/bin/sh
# Démarre (ou redémarre) le serveur Alexa-YT en arrière-plan.
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PIDFILE="$ROOT/alexa-yt.pid"
LOG="$ROOT/alexa-yt.log"
export PATH="$ROOT/bin:$PATH"   # yt-dlp trouve ainsi Deno

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"; sleep 1
fi
# Garde le journal raisonnable (le NAS a peu de place en mémoire flash)
[ -f "$LOG" ] && tail -n 2000 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"

cd server
nohup python3 alexa_yt.py config.json >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "Alexa-YT démarré (pid $(cat "$PIDFILE")), journal : $LOG"
