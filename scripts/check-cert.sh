#!/bin/sh
# Vérifie que le certificat HTTPS servi par le NAS est bien celui envoyé à Amazon.
#
#   sh check-cert.sh           vérifie ; code de sortie 1 = il faut renvoyer le certificat
#   sh check-cert.sh --envoye  à lancer après avoir envoyé cert/actuel.pem dans la console Alexa
#
# Prévu pour une tâche planifiée DSM quotidienne : en cas de problème, une notification DSM
# est envoyée (si lancé en root) et la tâche se termine en erreur (e-mail DSM possible).
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR="$ROOT/cert"
ACTUEL="$DIR/actuel.pem"
ENVOYE="$DIR/envoye-a-amazon.pem"
PORT=${CERT_PORT:-443}
ALERTE_JOURS=${CERT_ALERTE_JOURS:-14}

HOST=${CERT_HOST:-$(sed -n 's|.*"public_base_url"[^"]*"https://\([^/":]*\).*|\1|p' \
    "$ROOT/server/config.json" 2>/dev/null | head -n 1)}
if [ -z "$HOST" ]; then
    echo "public_base_url introuvable dans server/config.json"
    exit 2
fi

mkdir -p "$DIR"

notify() {
    echo "$1"
    if [ "$(id -u)" = 0 ] && [ -x /usr/syno/bin/synodsmnotify ]; then
        /usr/syno/bin/synodsmnotify @administrators "Alexa-YT" "$1" >/dev/null 2>&1 || true
    fi
}

fin() {
    # Lancé en root par le planificateur : rendre les fichiers à leur propriétaire.
    if [ "$(id -u)" = 0 ]; then
        chown -R "$(stat -c %u:%g "$ROOT")" "$DIR" 2>/dev/null || true
    fi
    exit "$1"
}

empreinte() {
    openssl x509 -in "$1" -noout -fingerprint -sha256 | sed 's/.*=//'
}

expiration() {
    openssl x509 -in "$1" -noout -enddate | sed 's/.*=//'
}

# Certificat réellement présenté par le serveur web du NAS
echo | openssl s_client -connect "127.0.0.1:$PORT" -servername "$HOST" 2>/dev/null \
    | openssl x509 > "$ACTUEL.tmp" 2>/dev/null
if [ ! -s "$ACTUEL.tmp" ]; then
    rm -f "$ACTUEL.tmp"
    notify "Impossible de lire le certificat HTTPS de $HOST (serveur web arrêté ?)"
    fin 1
fi
mv "$ACTUEL.tmp" "$ACTUEL"

if [ "${1:-}" = "--envoye" ]; then
    cp "$ACTUEL" "$ENVOYE"
    echo "Noté : le certificat actuel (expire le $(expiration "$ENVOYE")) est celui d'Amazon."
    fin 0
fi

if [ ! -f "$ENVOYE" ]; then
    # Premier lancement : on suppose que la skill fonctionne avec le certificat actuel.
    cp "$ACTUEL" "$ENVOYE"
    echo "Référence enregistrée : certificat actuel (expire le $(expiration "$ENVOYE"))."
    echo "Si la skill ne répond pas, envoie $ACTUEL à Amazon puis relance avec --envoye."
    fin 0
fi

if [ "$(empreinte "$ACTUEL")" != "$(empreinte "$ENVOYE")" ]; then
    notify "Certificat renouvelé : la skill Alexa ne répond plus. Envoie alexa-yt/cert/actuel.pem dans la console Alexa (Build > Endpoint), puis lance : sh ~/alexa-yt/scripts/check-cert.sh --envoye"
    fin 1
fi

if ! openssl x509 -in "$ACTUEL" -noout -checkend $((ALERTE_JOURS * 86400)) >/dev/null; then
    notify "Le certificat de $HOST expire le $(expiration "$ACTUEL") et n'a pas encore été renouvelé. Vérifie Panneau de configuration > Sécurité > Certificat."
    fin 1
fi

echo "OK : Amazon a le bon certificat (expire le $(expiration "$ACTUEL"))."
fin 0
