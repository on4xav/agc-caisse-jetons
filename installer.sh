#!/bin/bash
# Installation de la caisse jetons sur le POS (Ubuntu).
# À lancer une seule fois depuis le dossier caisse_jetons :   bash installer.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Dossier de la caisse jetons : $DIR"

echo "== Paquets système"
sudo apt update
sudo apt install -y python3-venv python3-pip git

echo "== Environnement Python"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

echo "== Imprimante tickets USB (0416:5011) et module tiroir (067b:23a3) : droits d'accès"
# (déjà fait si la caisse bar est installée : on le refait sans risque)
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0416", ATTR{idProduct}=="5011", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-imprimante-tickets.rules > /dev/null
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="23a3", SYMLINK+="tiroir", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-tiroir.rules > /dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG dialout "$USER"

echo "== Reprise de l'ancien ticket.py (config.json), s'il est dans ce dossier ou dans ~/ticket"
if [ ! -f "$DIR/config.json" ] && [ -f "$HOME/ticket/config.json" ]; then
    cp -v "$HOME/ticket/config.json" "$DIR/config.json"
fi

echo "== Raccourci « Caisse jetons »"
chmod +x "$DIR/lancer_caisse_jetons.sh"
RACCOURCI="[Desktop Entry]
Type=Application
Name=Caisse jetons
Comment=Vente de jetons bleus et rouges - concours de tir
Exec=$DIR/lancer_caisse_jetons.sh
Icon=accessories-calculator
Terminal=false
Categories=Office;"
mkdir -p "$HOME/.local/share/applications"
echo "$RACCOURCI" > "$HOME/.local/share/applications/caisse_jetons.desktop"

if [ "$1" = "--auto" ]; then
    mkdir -p "$HOME/.config/autostart"
    echo "$RACCOURCI" > "$HOME/.config/autostart/caisse_jetons.desktop"
    echo ">> Démarrage automatique activé."
fi

echo
echo "Installation terminée."
echo ">> Lancer la caisse jetons : icône « Caisse jetons » dans les applications."
echo ">> Démarrage automatique au boot (optionnel) :  bash installer.sh --auto"
echo ">> Le réglage du tiroir est repris de la caisse bar au premier lancement."
