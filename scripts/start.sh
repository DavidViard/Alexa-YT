#!/bin/sh
# Démarre (ou redémarre) le serveur Alexa-YT en arrière-plan.
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PIDFILE="$ROOT/alexa-yt.pid"
LOG="$ROOT/alexa-yt.log"
export PATH="$ROOT/bin:$PATH"   # yt-dlp trouve ainsi Deno
# yt-dlp (binaire autonome) se décompresse dans TMPDIR ; /tmp est en « noexec » sur DSM
export TMPDIR="$ROOT/tmp"
mkdir -p "$TMPDIR"
# Supprime les restes de yt-dlp interrompus (dossiers _MEI… de plus d'une heure)
find "$TMPDIR" -maxdepth 1 -name "_MEI*" -mmin +60 -exec rm -rf {} + 2>/dev/null

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"; sleep 1
fi
# Garde le journal raisonnable (le NAS a peu de place en mémoire flash)
[ -f "$LOG" ] && tail -n 2000 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"

cd server
nohup python3 alexa_yt.py config.json >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "Alexa-YT démarré (pid $(cat "$PIDFILE")), journal : $LOG"
