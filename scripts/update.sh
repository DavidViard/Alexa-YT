#!/bin/sh
# Met à jour yt-dlp (YouTube change souvent) puis redémarre le serveur.
cd "$(dirname "$0")/.."
export TMPDIR="$(pwd)/tmp"
mkdir -p "$TMPDIR"
bin/yt-dlp -U
sh scripts/start.sh
