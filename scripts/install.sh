#!/bin/sh
# Installe Alexa-YT sur un Synology ARM64 (DS218play) ou toute machine Linux.
# Usage : sh scripts/install.sh   (depuis le dossier alexa-yt)
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

case "$(uname -m)" in
  aarch64|arm64) YTDLP=yt-dlp_linux_aarch64; DENO=deno-aarch64-unknown-linux-gnu.zip ;;
  x86_64)        YTDLP=yt-dlp_linux;         DENO=deno-x86_64-unknown-linux-gnu.zip ;;
  *) echo "Architecture non prise en charge : $(uname -m)"; exit 1 ;;
esac

command -v python3 >/dev/null || { echo "python3 introuvable : installe Python 3 depuis le Centre de paquets"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 8)' || { echo "Python 3.8 minimum requis"; exit 1; }

mkdir -p bin tmp
export TMPDIR="$ROOT/tmp"  # /tmp est en « noexec » sur DSM
echo "Téléchargement de yt-dlp ($YTDLP)..."
curl -fL --retry 3 -o bin/yt-dlp "https://github.com/yt-dlp/yt-dlp/releases/latest/download/$YTDLP"
chmod +x bin/yt-dlp

echo "Téléchargement de Deno (nécessaire à yt-dlp pour YouTube)..."
curl -fL --retry 3 -o bin/deno.zip "https://github.com/denoland/deno/releases/latest/download/$DENO"
python3 -m zipfile -e bin/deno.zip bin/ && rm bin/deno.zip && chmod +x bin/deno

if [ ! -f server/config.json ]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  sed "s/GENERE-AUTOMATIQUEMENT-PAR-INSTALL/$SECRET/" server/config.example.json > server/config.json
  chmod 600 server/config.json
  echo "server/config.json créé : complète skill_id, google_client_id et google_client_secret."
fi

echo
bin/yt-dlp --version && bin/deno --version | head -1
echo "Installation terminée dans $ROOT"
