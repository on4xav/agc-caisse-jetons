# AGC — Caisse jetons

Caisse tactile (Flask) pour la **vente de jetons bleus et rouges** des concours de tir.
Elle reprend la présentation et le fonctionnement de la [caisse bar](https://github.com/on4xav/agc-caisse-bar)
et tourne sur le même POS Posiflex sous Ubuntu, en même temps qu'elle.

## Fonctions

- Deux grandes cartes 🔵 bleu / 🔴 rouge : boutons **+1 +2 +5 +10** et compteur **− / +**, sous-totaux et panier
- Gros boutons **Valider** / **Vider** (double appui pour vider)
- Paiement espèces : compte juste, billets 5/10/20/50 €, ou comptage de la monnaie reçue avec calcul du rendu ;
  montant à rendre affiché au centre de l'écran
- **Bancontact** par QR code (format EPC/SEPA, nécessite l'IBAN) et **Carte**, activables
- **Fond de caisse** et montant d'espèces attendu, avec calcul de l'écart en fin de journée
- Imprimante tickets ESC/POS : journal des ventes au fil de l'eau (sans coupe) et **rapport Z**
- **Tiroir-caisse** : ouverture automatique aux paiements en espèces ; le réglage est repris de la caisse bar
- Accès maintenance par **pavé numérique**, entrées / sorties imprimées sur le journal
- Le RESET de l'historique remet la numérotation des ventes à 1
- Mise à jour depuis GitHub en un bouton dans la configuration, redémarrage du POS
- Bouton **Fermer la fenêtre** (équivalent ALT + F4), la caisse continue de tourner
- Sauvegarde automatique de la base dans `backup/`

## Fichiers

| Fichier | Rôle |
|---|---|
| `caisse_jetons.py` | L'application (port 5001) |
| `installer.sh` | Installation complète sur le POS |
| `lancer_caisse_jetons.sh` | Démarre la caisse et ouvre Firefox en plein écran |
| `requirements.txt` | Dépendances Python |

## Installation sur le POS (Ubuntu)

À installer **à côté de la caisse bar** (`~/caisse_bar`), pour reprendre le réglage du tiroir :

```bash
git clone https://github.com/on4xav/agc-caisse-jetons.git ~/caisse_jetons
cd ~/caisse_jetons
bash installer.sh            # ajouter --auto pour l'ouvrir aussi au démarrage du POS
```

L'installateur crée l'environnement Python (`venv/`), donne les droits sur l'imprimante USB (`0416:5011`)
et le module tiroir (`/dev/tiroir`), puis crée l'icône **Caisse jetons**.
Par défaut, elle ne démarre pas toute seule, pour ne pas s'ouvrir par-dessus la caisse bar.

Si l'ancien `config.json` de ticket.py est copié dans le dossier avant le premier lancement,
le nom, l'IBAN et les prix sont repris.

## Mise à jour

**Depuis la caisse** : Config → **🔄 Mise à jour** → **Vérifier les mises à jour**, puis
**Installer et redémarrer la caisse**. La fenêtre se recharge toute seule.

**À la main** :

```bash
cd ~/caisse_jetons
git pull
pkill -f caisse_jetons.py
```

Puis relancer avec l'icône **Caisse jetons**. Les ventes et le code ne sont pas touchés
(ils ne sont pas dans le dépôt). La version installée s'affiche en haut à droite de la page **Config**
et sur le rapport Z.

## Premier démarrage

- Code de maintenance par défaut : **1234** (à changer dans Config → Changer mot de passe, chiffres uniquement).
- Saisir les prix des jetons, le nom et l'IBAN dans Config → Paramètres.
- Adresse de la caisse : http://127.0.0.1:5001 (la caisse bar reste sur le port 5003).
