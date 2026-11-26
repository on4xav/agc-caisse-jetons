#!/bin/bash
# Lance la caisse jetons : démarre le programme s'il ne tourne pas, puis ouvre la fenêtre plein écran.
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:5001"
PROFIL="$HOME/caisse_jetons_firefox_profil"

serveur_ok() {
    "$DIR/venv/bin/python" -c "import urllib.request; urllib.request.urlopen('$URL', timeout=1)" 2>/dev/null
}

if ! serveur_ok; then
    cd "$DIR"
    nohup "$DIR/venv/bin/python" "$DIR/caisse_jetons.py" --sans-navigateur >> "$DIR/caisse_jetons.log" 2>&1 &
    for i in $(seq 1 30); do serveur_ok && break; sleep 0.5; done
fi

# Fenêtre déjà ouverte ? On ne l'ouvre pas une deuxième fois.
if pgrep -f "caisse_jetons_firefox_profil" > /dev/null; then
    exit 0
fi

exec firefox --kiosk --new-instance --profile "$PROFIL" "$URL"
