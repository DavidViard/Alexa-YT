#!/bin/sh
# Met à jour yt-dlp (YouTube change souvent) puis redémarre le serveur.
cd "$(dirname "$0")/.."
bin/yt-dlp -U
sh scripts/start.sh
