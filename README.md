# AGC — Caisse jetons

Caisse tactile Flask pour la **vente de jetons bleus et rouges** lors des concours de tir.
Elle est construite comme la caisse bar ([agc-caisse-bar](https://github.com/on4xav/agc-caisse-bar)) et tourne sur le même POS Posiflex (Ubuntu + Firefox).

## Fonctions
- Cartes 🔵 bleu / 🔴 rouge avec boutons +1 +2 +5 +10 et compteur −/+
- Paiement espèces : compte juste, billet reçu, ou comptage de la monnaie avec calcul du rendu
- Bancontact par QR code (EPC/SEPA, nécessite l'IBAN) et option Carte
- Tiroir-caisse (réglage repris automatiquement de la caisse bar)
- Journal papier au fil de l'eau, fond de caisse, rapport Z imprimé (imprimante USB 0416:5011)
- Maintenance protégée par code (pavé numérique), bouton « Fermer la fenêtre » (équivalent ALT+F4)

## Installation sur le POS
Placer le dossier à côté de `caisse_bar` (même dossier parent), puis :

```bash
cd agc-caisse-jetons
bash installer.sh            # ajouter --auto pour le démarrage automatique
```

Lancer ensuite l'icône **Caisse jetons**. Adresse : http://127.0.0.1:5001
Code de maintenance par défaut : **1234** (à changer dans Configuration).

## Fichiers
| Fichier | Rôle |
|---|---|
| `caisse_jetons.py` | Application Flask (port 5001) |
| `installer.sh` | Installation : venv, dépendances, droits USB, raccourci |
| `lancer_caisse_jetons.sh` | Démarre le serveur si besoin et ouvre Firefox en kiosque |

Les données (`caisse_jetons.db`, `password.txt`, `backup/`) restent sur le POS et ne sont pas versionnées.
