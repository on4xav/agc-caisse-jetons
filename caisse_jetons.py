from flask import Flask, request, redirect, url_for, session, render_template_string, send_file
import sqlite3
import os
import json
import subprocess
import threading
import time
import shutil
import unicodedata
import sys
from io import BytesIO
from datetime import datetime

try:
    from escpos.printer import Usb
except Exception:
    Usb = None

try:
    import qrcode
except Exception:
    qrcode = None

# =========================================================
# CONFIG
# =========================================================

app = Flask(__name__)
app.secret_key = "change_moi_jetons"

PORT = 5001
URL_CAISSE = f"http://127.0.0.1:{PORT}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "caisse_jetons.db")
PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
ANCIEN_CONFIG = os.path.join(BASE_DIR, "config.json")   # config de l'ancien ticket.py
# Base de la caisse bar (dossier voisin) : on y reprend le réglage du tiroir-caisse
DB_BAR = os.path.join(os.path.dirname(BASE_DIR), "caisse_bar", "caisse_concours_tickets.db")

# Imprimante tickets USB (la même que la caisse bar)
POS_VENDOR_ID = 0x0416
POS_PRODUCT_ID = 0x5011
POS_INTERFACE = 0
POS_IN_EP = 0x82
POS_OUT_EP = 0x01

MACROS = [1, 2, 5, 10]                                    # boutons +1 +2 +5 +10
BILLETS = [5, 10, 20, 50]                                 # billets reçus (paiement en un geste)
MONNAIE = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000]   # comptage monnaie, en centimes

COULEURS = {
    "bleu": {"nom": "Jeton bleu", "emoji": "🔵"},
    "rouge": {"nom": "Jeton rouge", "emoji": "🔴"},
}

# Tous les montants sont stockés en CENTIMES (250 = 2,50 €)
DEFAUTS = {
    "nom_compte": "ASBL Fete Locale",
    "iban": "",
    "communication": "Caisse Jetons",
    "prix_bleu": "250",
    "prix_rouge": "300",
    "fond_caisse": "0",
}

OPTIONS = ("pay_bancontact", "pay_carte", "print_journal", "tiroir_actif")
OPTIONS_PAR_DEFAUT_NON = ("pay_carte", "tiroir_actif")   # désactivées tant qu'on ne les allume pas

# =========================================================
# SQLITE / PARAMETRES
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_param(cle, defaut=""):
    conn = db()
    row = conn.execute("SELECT valeur FROM parametres WHERE cle = ?", (cle,)).fetchone()
    conn.close()
    return row["valeur"] if row else defaut


def set_param(cle, valeur):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)", (cle, str(valeur)))
    conn.commit()
    conn.close()


def get_int(cle):
    try:
        return int(get_param(cle, DEFAUTS.get(cle, "0")))
    except ValueError:
        return 0


def option_active(cle):
    defaut = "0" if cle in OPTIONS_PAR_DEFAUT_NON else "1"
    return get_param(cle, defaut) == "1"


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ventes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            qte_bleu INTEGER DEFAULT 0,
            qte_rouge INTEGER DEFAULT 0,
            prix_bleu INTEGER DEFAULT 0,
            prix_rouge INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            paiement TEXT DEFAULT 'ESPECES',
            recu INTEGER DEFAULT 0,
            rendu INTEGER DEFAULT 0,
            annulee INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS parametres (cle TEXT PRIMARY KEY, valeur TEXT)")
    conn.commit()
    vide = conn.execute("SELECT COUNT(*) FROM parametres").fetchone()[0] == 0
    conn.close()

    if vide:
        importer_ancienne_config()
        importer_tiroir_du_bar()
    for cle, valeur in DEFAUTS.items():
        if get_param(cle, None) is None:
            set_param(cle, valeur)


def importer_ancienne_config():
    """Reprend nom, IBAN, prix... du config.json de l'ancien ticket.py s'il est dans le dossier."""
    if not os.path.exists(ANCIEN_CONFIG):
        return
    try:
        with open(ANCIEN_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print("config.json illisible :", e)
        return
    for cle in ("nom_compte", "iban", "communication"):
        if cfg.get(cle):
            set_param(cle, cfg[cle])
    prix_bleu = cfg.get("prix_jeton_bleu", cfg.get("prix_ticket"))
    if prix_bleu is not None:
        set_param("prix_bleu", parse_euros(prix_bleu))
    if "prix_jeton_rouge" in cfg:
        set_param("prix_rouge", parse_euros(cfg["prix_jeton_rouge"]))
    print("Ancienne configuration config.json importée")


def importer_tiroir_du_bar():
    """Même POS, même tiroir : on recopie le réglage trouvé par la caisse bar."""
    if not os.path.exists(DB_BAR):
        return
    try:
        src = sqlite3.connect(DB_BAR)
        rows = src.execute("""
            SELECT cle, valeur FROM parametres
            WHERE cle IN ('tiroir_methode', 'tiroir_port', 'tiroir_actif', 'tiroir_quand')
        """).fetchall()
        src.close()
        for cle, valeur in rows:
            set_param(cle, valeur)
        if rows:
            print("Réglage du tiroir-caisse repris de la caisse bar")
    except Exception as e:
        print("Reprise du tiroir de la caisse bar impossible :", e)

# =========================================================
# FORMATAGE MONTANTS
# =========================================================

@app.template_filter("euros")
def euros(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",") + " €"


@app.template_filter("euros_input")
def euros_input(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",")


def parse_euros(texte):
    try:
        t = str(texte).strip().replace("€", "").replace(",", ".")
        return int(round(float(t) * 100))
    except ValueError:
        return 0


def montant_court(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",")


def montant_txt(centimes):
    """Version imprimante thermique (pas de symbole €)."""
    return montant_court(centimes) + " EUR"


def sans_accents(texte):
    return unicodedata.normalize("NFKD", str(texte)).encode("ascii", "ignore").decode("ascii")

# =========================================================
# PASSWORD / BACKUP
# =========================================================

def read_password():
    if not os.path.exists(PASSWORD_FILE):
        write_password("1234")
        return "1234"
    with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
        pwd = f.read().strip()
    if not pwd.isdigit():
        # L'ancien ticket.py utilisait "admin" : le pavé numérique ne sait taper que des chiffres
        print("Mot de passe non numérique détecté : remis à 1234")
        write_password("1234")
        return "1234"
    return pwd


def write_password(new_password):
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(new_password.strip())


def backup_db():
    try:
        if not os.path.exists(DB_FILE):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, f"jetons_backup_{stamp}.db"))
    except Exception as e:
        print("Erreur backup :", e)

# =========================================================
# PANIER (quantités de jetons en session)
# =========================================================

def get_qte():
    q = session.get("qte") or {}
    return {"bleu": int(q.get("bleu", 0)), "rouge": int(q.get("rouge", 0))}


def save_qte(q):
    session["qte"] = q
    session.modified = True


def calcul(q):
    pb, pr = get_int("prix_bleu"), get_int("prix_rouge")
    sb, sr = q["bleu"] * pb, q["rouge"] * pr
    return {"prix_bleu": pb, "prix_rouge": pr, "sous_bleu": sb, "sous_rouge": sr, "total": sb + sr}

# =========================================================
# RESUME DES VENTES
# =========================================================

def resume_ventes():
    conn = db()
    g = conn.execute("""
        SELECT COUNT(*) AS nb, COALESCE(SUM(total), 0) AS total,
               COALESCE(SUM(qte_bleu), 0) AS bleus, COALESCE(SUM(qte_rouge), 0) AS rouges,
               COALESCE(SUM(qte_bleu * prix_bleu), 0) AS montant_bleus,
               COALESCE(SUM(qte_rouge * prix_rouge), 0) AS montant_rouges
        FROM ventes WHERE annulee = 0
    """).fetchone()
    par_paiement = conn.execute("""
        SELECT paiement, COUNT(*) AS nb, COALESCE(SUM(total), 0) AS total
        FROM ventes WHERE annulee = 0 GROUP BY paiement ORDER BY paiement DESC
    """).fetchall()
    especes = conn.execute("""
        SELECT COALESCE(SUM(total), 0) FROM ventes WHERE annulee = 0 AND paiement = 'ESPECES'
    """).fetchone()[0]
    conn.close()
    fond = get_int("fond_caisse")
    return {"g": g, "par_paiement": par_paiement, "fond": fond, "especes": especes,
            "attendu": fond + especes, "nom": get_param("nom_compte")}

# =========================================================
# IMPRIMANTE
# =========================================================

IMPRIMANTE_LOCK = threading.Lock()
ERREUR_JOURNAL = {"texte": ""}


def ouvrir_imprimante():
    if Usb is None:
        raise RuntimeError("Module python-escpos manquant (relancer installer.sh)")
    try:
        return Usb(POS_VENDOR_ID, POS_PRODUCT_ID, interface=POS_INTERFACE, in_ep=POS_IN_EP, out_ep=POS_OUT_EP)
    except TypeError:
        return Usb(POS_VENDOR_ID, POS_PRODUCT_ID)


def fermer_imprimante(printer):
    try:
        printer.close()
    except Exception:
        pass


def journal_actif():
    return option_active("print_journal")


def imprimer_journal(lignes, gras_premiere=True):
    """Imprime des lignes à la suite, sans couper le papier."""
    if not lignes:
        return
    with IMPRIMANTE_LOCK:
        try:
            printer = ouvrir_imprimante()
            try:
                for i, ligne in enumerate(lignes):
                    printer.set(align="left", bold=(gras_premiere and i == 0), width=1, height=1)
                    printer.text(sans_accents(ligne) + "\n")
            finally:
                fermer_imprimante(printer)
            ERREUR_JOURNAL["texte"] = ""
        except Exception as e:
            ERREUR_JOURNAL["texte"] = str(e)
            print("Erreur impression journal :", e)


def journal_en_arriere_plan(lignes):
    """La caisse n'attend pas l'imprimante."""
    threading.Thread(target=imprimer_journal, args=(lignes,), daemon=True).start()


def lignes_journal_vente(vente_id):
    conn = db()
    v = conn.execute("SELECT * FROM ventes WHERE id = ?", (vente_id,)).fetchone()
    conn.close()
    if not v:
        return []
    out = [f"JETONS #{v['id']:<4} {str(v['date'])[11:16]} {v['paiement']:>11}"[:32]]
    if v["qte_bleu"]:
        out.append(f"{'Bleu':<17} x{v['qte_bleu']:<3}{montant_court(v['qte_bleu'] * v['prix_bleu']):>9}"[:32])
    if v["qte_rouge"]:
        out.append(f"{'Rouge':<17} x{v['qte_rouge']:<3}{montant_court(v['qte_rouge'] * v['prix_rouge']):>9}"[:32])
    out.append(f"{'TOTAL':<20}{montant_court(v['total']):>8} EUR"[:32])
    if v["paiement"] == "ESPECES" and int(v["rendu"] or 0) > 0:
        out.append(f"Recu {montant_court(v['recu'])}  Rendu {montant_court(v['rendu'])}"[:32])
    out.append("-" * 32)
    return out


def imprimer_rapport_z():
    r = resume_ventes()
    with IMPRIMANTE_LOCK:
        printer = ouvrir_imprimante()
        try:
            _imprimer_bilan(printer, r)
        finally:
            fermer_imprimante(printer)


def _imprimer_bilan(printer, r):
    def p(texte):
        printer.text(sans_accents(texte))

    sep = "--------------------------------\n"
    g = r["g"]

    printer.set(align="center", bold=True, width=2, height=2)
    p("RAPPORT Z\n")
    printer.set(align="center", bold=False, width=1, height=1)
    p("VENTE DE JETONS\n")
    p(f"{r['nom']}\n")
    p(datetime.now().strftime("%d/%m/%Y %H:%M:%S") + "\n")
    p(sep)

    printer.set(align="left", bold=False)
    p(f"Nombre ventes : {g['nb']}\n")
    p(sep)
    printer.set(bold=True)
    p("JETONS VENDUS\n")
    printer.set(bold=False)
    p(f"{'Bleus':<10}x{g['bleus']:<5}{montant_txt(g['montant_bleus']):>16}\n")
    p(f"{'Rouges':<10}x{g['rouges']:<5}{montant_txt(g['montant_rouges']):>16}\n")
    p(sep)
    printer.set(bold=True)
    p("PAIEMENTS\n")
    printer.set(bold=False)
    for pp in r["par_paiement"]:
        p(f"{pp['paiement']:<12}{pp['nb']:>4}{montant_txt(pp['total']):>16}\n")
    p(sep)

    printer.set(bold=True)
    p("CAISSE ESPECES\n")
    printer.set(bold=False)
    p(f"{'Fond de caisse':<18}{montant_txt(r['fond']):>14}\n")
    p(f"{'+ Ventes especes':<18}{montant_txt(r['especes']):>14}\n")
    printer.set(bold=True)
    p(f"{'= ATTENDU':<18}{montant_txt(r['attendu']):>14}\n")
    printer.set(bold=False)
    p(f"{'Compte :':<18}{'..........':>14}\n")
    p(f"{'Ecart  :':<18}{'..........':>14}\n")
    p(sep)

    printer.set(align="center", bold=True, width=1, height=2)
    p(f"TOTAL : {montant_txt(g['total'])}\n")
    printer.set(align="center", bold=False, width=1, height=1)
    p("\n\n\n")
    try:
        printer.cut()
    except Exception:
        pass

# =========================================================
# NAVIGATEUR (Firefox kiosque, profil dédié) — comme la caisse bar
# =========================================================

NAVIGATEUR = {"proc": None}

# Profil Firefox propre à la caisse jetons (différent de celui de la caisse bar :
# les deux fenêtres peuvent être ouvertes en même temps et se fermer séparément).
PROFIL_FIREFOX = os.path.join(os.path.expanduser("~"), "caisse_jetons_firefox_profil")

PREFS_FIREFOX = """
user_pref("dom.allow_scripts_to_close_windows", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
"""


def preparer_profil_firefox():
    os.makedirs(PROFIL_FIREFOX, exist_ok=True)
    with open(os.path.join(PROFIL_FIREFOX, "user.js"), "w", encoding="utf-8") as f:
        f.write(PREFS_FIREFOX)


def fermer_navigateur():
    """Ferme la fenêtre de la caisse jetons (et seulement elle). Le serveur continue de tourner."""
    proc = NAVIGATEUR.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if os.name == "posix":
        for motif in (PROFIL_FIREFOX, f"127.0.0.1:{PORT}"):
            subprocess.run(["pkill", "-f", motif], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def envoyer_alt_f4_windows():
    try:
        import ctypes
        VK_MENU, VK_F4, KEYUP = 0x12, 0x73, 0x0002
        u = ctypes.windll.user32
        u.keybd_event(VK_MENU, 0, 0, 0)
        u.keybd_event(VK_F4, 0, 0, 0)
        u.keybd_event(VK_F4, 0, KEYUP, 0)
        u.keybd_event(VK_MENU, 0, KEYUP, 0)
        return True
    except Exception as e:
        print("ALT+F4 Windows impossible :", e)
        return False


def fermer_fenetre_plus_tard():
    # Le navigateur essaie d'abord de se fermer lui-même (window.close).
    # Si la fenêtre est toujours là après 1,5 s, le programme la ferme.
    time.sleep(1.5)
    if os.name == "nt":
        if not envoyer_alt_f4_windows():
            fermer_navigateur()
    else:
        fermer_navigateur()


def ouvrir_navigateur():
    time.sleep(2)
    url = URL_CAISSE
    try:
        preparer_profil_firefox()
    except Exception as e:
        print("Profil Firefox :", e)
    commandes = [
        ["firefox", "--kiosk", "--new-instance", "--profile", PROFIL_FIREFOX, url],
        ["chromium", "--kiosk", "--noerrdialogs", "--disable-infobars", url],
        ["chromium-browser", "--kiosk", url],
        ["google-chrome", "--kiosk", url],
        ["chrome", "--kiosk", url],
        ["msedge", "--kiosk", url],
    ]
    for commande in commandes:
        try:
            NAVIGATEUR["proc"] = subprocess.Popen(commande)
            print("Navigateur lancé :", " ".join(commande))
            return
        except FileNotFoundError:
            continue
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        print("Aucun navigateur trouvé")

# =========================================================
# TIROIR-CAISSE (même code que la caisse bar)
# =========================================================

METHODES_TIROIR = {
    "aucun": "Pas de tiroir",
    "imprimante": "Via l'imprimante tickets (port DK)",
    "serie_dtr": "Port COM — impulsion DTR",
    "serie_rts": "Port COM — impulsion RTS",
    "serie_octet": "Port COM — envoi d'octets",
    "parallele_data": "Port LPT — lignes de données",
    "parallele_ctrl": "Port LPT — lignes de contrôle",
}
CODE_ESCPOS_TIROIR = [b"\x1bp\x00\x19\xfa", b"\x1bp\x01\x19\xfa"]   # broches 2 et 5


def ports_series():
    """Ports série présents (module USB /dev/tiroir en premier, puis USB-série, puis COM)."""
    import glob
    import re
    trouves = []
    for chemin in glob.glob("/sys/class/tty/ttyS*"):
        nom = os.path.basename(chemin)
        try:
            with open(os.path.join(chemin, "type")) as f:
                if f.read().strip() == "0":
                    continue
        except OSError:
            continue
        trouves.append("/dev/" + nom)
    trouves.sort(key=lambda p: int(re.sub(r"\D", "", p) or 0))
    usb = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    fixe = ["/dev/tiroir"] if os.path.exists("/dev/tiroir") else []
    trouves = fixe + usb + trouves
    return trouves or [f"/dev/ttyS{i}" for i in range(0, 4)]


def ports_paralleles():
    import glob
    return sorted(glob.glob("/dev/parport*"))


def nom_com(port):
    """/dev/ttyS0 -> COM1 (nom imprimé à l'arrière du POS)."""
    if port.startswith("/dev/ttyS"):
        try:
            return f"COM{int(port[9:]) + 1}"
        except ValueError:
            pass
    if port.startswith("/dev/parport"):
        try:
            return f"LPT{int(port[12:]) + 1}"
        except ValueError:
            pass
    return port


def impulsion_parallele(port, mode):
    import fcntl
    import struct
    PPCLAIM, PPRELEASE = 0x708B, 0x708C
    PPWDATA, PPWCONTROL = 0x40017086, 0x40017084
    fd = os.open(port, os.O_RDWR)
    try:
        fcntl.ioctl(fd, PPCLAIM)
        try:
            if mode == "parallele_data":
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0x00)); time.sleep(0.05)
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0xFF)); time.sleep(0.2)
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0x00))
            else:
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04)); time.sleep(0.05)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x0B)); time.sleep(0.2)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04))
        finally:
            fcntl.ioctl(fd, PPRELEASE)
    finally:
        os.close(fd)


def ouvrir_tiroir(methode=None, port=None):
    """Envoie l'impulsion d'ouverture. Retourne (ok, message)."""
    methode = methode or get_param("tiroir_methode", "aucun")
    port = port or get_param("tiroir_port", ports_series()[0])
    try:
        if methode == "aucun":
            return False, "Aucun tiroir configuré"

        if methode == "imprimante":
            with IMPRIMANTE_LOCK:
                printer = ouvrir_imprimante()
                try:
                    for code in CODE_ESCPOS_TIROIR:
                        printer._raw(code)
                finally:
                    fermer_imprimante(printer)
            return True, "Commande envoyée à l'imprimante"

        if methode in ("parallele_data", "parallele_ctrl"):
            impulsion_parallele(port, methode)
            return True, f"Impulsion envoyée sur {nom_com(port)}"
        if methode not in ("serie_dtr", "serie_rts", "serie_octet"):
            return False, f"Méthode inconnue : {methode}"
        try:
            import serial
        except ImportError:
            return False, "Module pyserial manquant (relancer installer.sh)"
        s = serial.Serial(port, 9600, timeout=1)
        try:
            if methode == "serie_dtr":
                s.dtr = False; time.sleep(0.05)
                s.dtr = True;  time.sleep(0.2)
                s.dtr = False
            elif methode == "serie_rts":
                s.rts = False; time.sleep(0.05)
                s.rts = True;  time.sleep(0.2)
                s.rts = False
            else:
                for code in CODE_ESCPOS_TIROIR:
                    s.write(code)
                s.write(b"\x07")
                s.flush()
        finally:
            s.close()
        return True, f"Impulsion envoyée sur {nom_com(port)}"
    except Exception as e:
        texte = f"{type(e).__name__} : {e}"
        if "Permission denied" in texte:
            texte += " — ajouter l'utilisateur aux groupes dialout et lp puis se reconnecter"
        return False, texte


def ouvrir_tiroir_arriere_plan():
    def tache():
        try:
            ouvrir_tiroir()          # tiroir absent ou débranché : on ignore, la vente continue
        except Exception:
            pass
    threading.Thread(target=tache, daemon=True).start()


def tiroir_configure():
    return option_active("tiroir_actif") and get_param("tiroir_methode", "aucun") != "aucun"


def libelle_tiroir():
    methode = get_param("tiroir_methode", "aucun")
    if methode == "aucun":
        return "Méthode pas encore choisie : faire « Réglage / test du tiroir »"
    txt = METHODES_TIROIR.get(methode, methode)
    if methode.startswith("serie") or methode.startswith("parallele"):
        txt += f" sur {nom_com(get_param('tiroir_port', ports_series()[0]))}"
    quand = get_param("tiroir_quand", "especes")
    txt += " — s'ouvre " + ("à chaque paiement" if quand == "tous" else "pour les paiements en espèces")
    return txt

# =========================================================
# STYLE (identique à la caisse bar + cartes jetons)
# =========================================================

STYLE = """
<style>
* { box-sizing: border-box; -webkit-user-select: none; user-select: none; }
body { margin: 0; background: #111; color: white; font-family: Arial, sans-serif; }
header { position: relative; background: #222; padding: 15px; text-align: center; font-size: 34px; font-weight: bold; }
header.jetons { background: #1a3a5c; }
.config-link { position: absolute; top: 50%; right: 12px; transform: translateY(-50%); display: flex; align-items: center; gap: 8px;
               height: 58px; padding: 0 24px; border-radius: 14px; background: #555; color: white; font-size: 24px; font-weight: bold;
               text-decoration: none; box-shadow: 0 3px 8px rgba(0,0,0,.4); }
.config-link:active { background: #777; }
.container { display: flex; height: calc(100vh - 80px); }

.jetons-zone { flex: 3; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; overflow-y: auto; }
.jeton-card { background: #1d1d1d; border-radius: 18px; padding: 16px; display: flex; flex-direction: column; justify-content: space-between; gap: 14px; border-top: 10px solid #555; }
.jeton-card.bleu { border-top-color: #2d89ef; }
.jeton-card.rouge { border-top-color: #e74c3c; }
.jeton-titre { text-align: center; font-size: 32px; font-weight: bold; }
.jeton-prix { text-align: center; font-size: 22px; color: #aaa; margin-top: -8px; }
.macro-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.macro-btn { display: flex; align-items: center; justify-content: center; height: 110px; border-radius: 16px; font-size: 42px; font-weight: bold; color: white; text-decoration: none; box-shadow: 0 4px 10px rgba(0,0,0,.35); }
.bleu .macro-btn { background: #2d89ef; }
.rouge .macro-btn { background: #c0392b; }
.macro-btn:active { filter: brightness(1.3); }
.compteur { display: flex; align-items: center; justify-content: space-between; background: black; border-radius: 14px; padding: 10px; }
.compteur .qty-btn { width: 110px; font-size: 44px; padding: 16px 0; }
.compteur-chiffre { font-size: 72px; font-weight: bold; }
.bleu .compteur-chiffre { color: #3399ff; }
.rouge .compteur-chiffre { color: #ff4d4d; }
.sous-total { text-align: right; font-size: 24px; color: #ccc; }

.panier { flex: 2; background: #1d1d1d; padding: 12px; display: flex; flex-direction: column; }
.panier-list { flex: 1; overflow-y: auto; background: black; padding: 10px; font-size: 26px; border-radius: 12px; }
.panier-row { display: grid; grid-template-columns: 1fr 80px 140px; gap: 6px; align-items: center; padding: 14px 8px; border-bottom: 1px solid #333; }
.qty-btn { border: none; border-radius: 10px; padding: 14px; color: white; background: #555; font-size: 26px; font-weight: bold; text-decoration: none; text-align: center; }
.total { text-align: center; font-size: 40px; font-weight: bold; color: #00ff66; margin-top: 5px; }
.total-jetons-box { display: flex; justify-content: space-around; background: #222; border-radius: 10px; padding: 8px; margin: 5px 0; font-size: 22px; }
.jeton-r { color: #ff4d4d; font-weight: bold; }
.jeton-b { color: #3399ff; font-weight: bold; }

.btn { display: inline-block; text-decoration: none; text-align: center; border: none; border-radius: 14px; padding: 18px; margin: 5px; font-size: 24px; font-weight: bold; color: white; cursor: pointer; }
.btn:disabled { opacity: .3; cursor: default; }
.green { background: green; } .orange { background: orange; color: black; } .red { background: #c0392b; } .blue { background: #2d89ef; } .gray { background: #555; } .purple { background: #8e44ad; }
.actions { display: flex; flex-direction: column; gap: 22px; margin-top: 12px; }
.btn-valider, .btn-vider { display: block; width: 100%; margin: 0; padding: 0; border-radius: 20px; }
.btn-valider { height: 120px; font-size: 42px; background: #1e9e3e; }
.btn-vider { height: 80px; font-size: 28px; background: #c0392b; }
.btn-vider.armed { background: #ff2a2a; animation: clignote .5s infinite alternate; }
@keyframes clignote { from { opacity: 1; } to { opacity: .6; } }

.flash { position: fixed; top: 90px; left: 50%; transform: translateX(-50%); z-index: 100; background: #1e9e3e; color: white; font-size: 34px; font-weight: bold; text-align: center; padding: 20px 40px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0,0,0,.6); pointer-events: none; animation: flashout 2.5s forwards; }
.flash.flash-long { top: 50%; transform: translate(-50%, -50%); background: #d68910; padding: 40px 60px; font-size: 38px; border: 6px solid white; box-shadow: 0 0 0 100vmax rgba(0,0,0,.6); animation-duration: 6s; }
.flash .rendu { font-size: 64px; display: inline-block; margin: 10px 0; white-space: nowrap; }
@keyframes flashout { 0%, 70% { opacity: 1; } 100% { opacity: 0; } }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.88); z-index: 50; align-items: center; justify-content: center; }
.overlay-box { background: #1d1d1d; border-radius: 24px; padding: 25px; width: min(950px, 95vw); max-height: 96vh; overflow-y: auto; text-align: center; }
.overlay-total { font-size: 52px; font-weight: bold; color: #00ff66; margin-bottom: 20px; }
.pay-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 16px; margin-bottom: 18px; }
.pay-btn { grid-column: span 2; height: 105px; border: none; border-radius: 20px; font-size: 34px; font-weight: bold; background: #f39c12; color: black; cursor: pointer; }
.pay-btn.exact { background: #1e9e3e; color: white; }
.pay-btn.elec { grid-column: span 3; color: white; }
.pay-btn.elec.seul { grid-column: span 6; }
.pay-btn.bancontact { background: #005498; }
.pay-btn.carte { background: #2d89ef; }

.monnaie { background: #111; border-radius: 18px; padding: 15px; margin-bottom: 18px; }
.monnaie-titre { font-size: 22px; color: #aaa; margin-bottom: 10px; }
.monnaie-btns { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
.coin { width: 92px; height: 78px; border: none; border-radius: 40px; background: #7f8c8d; color: white; font-size: 24px; font-weight: bold; cursor: pointer; }
.coin.billet { border-radius: 12px; background: #1e8449; }
.coin.effacer { background: #b9770e; }
.monnaie-etat { display: flex; justify-content: space-around; align-items: center; font-size: 30px; margin-top: 12px; }

table { width: 100%; border-collapse: collapse; font-size: 22px; } td, th { border: 1px solid #555; padding: 12px; }
input, select { font-size: 24px; padding: 10px; max-width: 100%; }
.card { background: #1d1d1d; border-radius: 18px; padding: 20px; margin: 12px; }
.big-number { font-size: 58px; color: #00ff66; font-weight: bold; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; text-align: left; max-width: 900px; margin: auto; }
.form-grid label { display: block; font-size: 20px; color: #aaa; margin-bottom: 4px; }
.form-grid input { width: 100%; }
@media print { body { background: white; color: black; } .no-print { display: none; } header { background: white; color: black; } .config-link { display: none; } }
</style>
"""

BLOC_CAISSE_ESPECES = """
<div class="card">
    <h2>💶 Caisse espèces</h2>
    <table style="max-width:700px;margin:auto;font-size:28px;">
        <tr><td style="text-align:left;">Fond de caisse</td><td style="text-align:right;">{{ r.fond|euros }}</td></tr>
        <tr><td style="text-align:left;">+ Ventes en espèces</td><td style="text-align:right;">{{ r.especes|euros }}</td></tr>
        <tr><td style="text-align:left;"><b>= Attendu dans la caisse</b></td><td style="text-align:right;color:#00ff66;"><b>{{ r.attendu|euros }}</b></td></tr>
        <tr><td style="text-align:left;">Montant compté</td>
            <td style="text-align:right;"><input id="compte" inputmode="decimal" placeholder="0,00" style="width:170px;text-align:right;font-size:28px;" oninput="calculEcart()"> €</td></tr>
        <tr><td style="text-align:left;">Écart</td><td id="ecart" style="text-align:right;font-weight:bold;">—</td></tr>
    </table>
    <p style="color:#aaa;font-size:18px;">Bancontact et carte ne sont pas dans le tiroir : ils ne comptent pas dans l'attendu.</p>
</div>
<script>
function calculEcart() {
    const v = document.getElementById("compte").value.replace(",", ".").replace("€", "").trim();
    const td = document.getElementById("ecart");
    if (v === "" || isNaN(parseFloat(v))) { td.textContent = "—"; td.style.color = ""; return; }
    const ecart = Math.round(parseFloat(v) * 100) - {{ r.attendu }};
    const txt = (Math.abs(ecart) / 100).toFixed(2).replace(".", ",") + " €";
    if (ecart === 0) { td.textContent = "0,00 € ✔"; td.style.color = "#00ff66"; }
    else if (ecart > 0) { td.textContent = "+" + txt + " (en trop)"; td.style.color = "#f1c40f"; }
    else { td.textContent = "−" + txt + " (manquant)"; td.style.color = "#ff5555"; }
}
</script>
"""

# =========================================================
# ACCUEIL
# =========================================================

def bancontact_dispo():
    return option_active("pay_bancontact") and bool(get_param("iban").strip()) and qrcode is not None


@app.route("/")
def index():
    qte = get_qte()
    calc = calcul(qte)
    a_des_jetons = qte["bleu"] + qte["rouge"] > 0
    derniere = session.pop("derniere_vente", None)

    return render_template_string("""
    {{ style|safe }}
    {% if derniere %}
    <div class="flash {% if derniere.rendu %}flash-long{% endif %}">
        ✔ Vente n° {{ derniere.id }} — {{ derniere.total|euros }}{% if derniere.paiement != 'ESPECES' %} ({{ derniere.paiement|lower }}){% endif %}<br>
        {% if derniere.rendu %}<span class="rendu">À RENDRE : {{ derniere.rendu|euros }}</span><br>{% endif %}
        <span style="font-size:26px;">🔵 {{ derniere.bleus }} &nbsp; 🔴 {{ derniere.rouges }}</span>
    </div>
    {% endif %}
    {% if alerte_imprimante %}
    <div style="position:fixed;bottom:12px;left:12px;z-index:40;background:#c0392b;color:white;font-size:20px;font-weight:bold;padding:10px 18px;border-radius:12px;">⚠ Imprimante journal : erreur</div>
    {% endif %}

    <header class="jetons">VENTE DE JETONS — {{ nom }}<a class="config-link" href="/login">⚙ Config</a></header>
    <div class="container">
        <div class="jetons-zone">
            {% for c in ['bleu', 'rouge'] %}
            <div class="jeton-card {{ c }}">
                <div class="jeton-titre">{{ couleurs[c].emoji }} {{ couleurs[c].nom|upper }}</div>
                <div class="jeton-prix">{{ calc['prix_' ~ c]|euros }} / jeton</div>
                <div class="macro-grid">
                    {% for n in macros %}<a class="macro-btn" href="/plus/{{ c }}/{{ n }}">+{{ n }}</a>{% endfor %}
                </div>
                <div class="compteur">
                    <a class="qty-btn" href="/moins/{{ c }}">−</a>
                    <span class="compteur-chiffre">{{ qte[c] }}</span>
                    <a class="qty-btn" href="/plus/{{ c }}/1">+</a>
                </div>
                <div class="sous-total">Sous-total : {{ calc['sous_' ~ c]|euros }}</div>
            </div>
            {% endfor %}
        </div>

        <div class="panier">
            <h1 style="text-align:center; margin:5px 0;">PANIER</h1>
            <div class="panier-list">
                {% if not a_des_jetons %}<div style="text-align:center;color:#aaa;margin-top:40px;">Aucun jeton</div>{% endif %}
                {% for c in ['bleu', 'rouge'] if qte[c] %}
                <div class="panier-row">
                    <span class="{{ 'jeton-b' if c == 'bleu' else 'jeton-r' }}">{{ couleurs[c].emoji }} {{ couleurs[c].nom }}</span>
                    <span style="text-align:center;">x{{ qte[c] }}</span>
                    <span style="text-align:right;">{{ calc['sous_' ~ c]|euros }}</span>
                </div>
                {% endfor %}
            </div>

            <div class="total-jetons-box">
                <span>Total Bleus : <span class="jeton-b">{{ qte.bleu }}</span></span>
                <span>Total Rouges : <span class="jeton-r">{{ qte.rouge }}</span></span>
            </div>

            <div class="total">Total : {{ calc.total|euros }}</div>

            <div class="actions">
                <button type="button" class="btn btn-valider" {% if not a_des_jetons %}disabled{% endif %} onclick="ouvrirPaiement()">✔ VALIDER</button>
                <button type="button" class="btn btn-vider" {% if not a_des_jetons %}disabled{% endif %} onclick="vider(this)">✖ Vider</button>
            </div>
        </div>
    </div>

    {% if a_des_jetons %}
    <!-- ÉCRAN PAIEMENT -->
    <div id="paiement" class="overlay" style="display:none;">
        <div class="overlay-box">
            <div class="overlay-total">À payer : {{ calc.total|euros }}</div>
            <form method="post" action="/payer" class="pay-grid" onsubmit="if (this.dataset.envoye) return false; this.dataset.envoye = 1;">
                <button class="pay-btn exact" name="choix" value="EXACT">Compte juste</button>
                {% for b in billets if b * 100 > calc.total %}
                <button class="pay-btn" name="choix" value="{{ b * 100 }}">{{ b }} €</button>
                {% endfor %}
                {% if bancontact %}
                <button type="button" class="pay-btn elec bancontact {{ '' if carte else 'seul' }}" onclick="ouvrirQR()">📱 Bancontact (QR)</button>
                {% endif %}
                {% if carte %}
                <button class="pay-btn elec carte {{ '' if bancontact else 'seul' }}" name="choix" value="CARTE">💳 Carte</button>
                {% endif %}
            </form>

            <div class="monnaie">
                <div class="monnaie-titre">Ou compter la monnaie reçue :</div>
                <div class="monnaie-btns">
                    {% for m in monnaie %}
                    <button type="button" class="coin {{ 'billet' if m >= 500 else '' }}" onclick="ajoutMonnaie({{ m }})">{{ m|euros_input if m < 500 else (m // 100) ~ ' €' }}</button>
                    {% endfor %}
                    <button type="button" class="coin effacer" onclick="effaceMonnaie()">C</button>
                </div>
                <div class="monnaie-etat">
                    <span>Reçu : <b id="m-recu">0,00 €</b></span>
                    <span id="m-rendu" style="font-weight:bold;">—</span>
                    <form method="post" action="/payer" style="margin:0;" onsubmit="if (this.dataset.envoye) return false; this.dataset.envoye = 1;">
                        <input type="hidden" name="choix" id="m-valeur" value="0">
                        <button id="m-valider" class="btn green" style="margin:0;" disabled>Valider</button>
                    </form>
                </div>
            </div>

            <button type="button" class="btn gray" style="width:100%;height:80px;margin:0;" onclick="fermerPaiement()">Annuler</button>
        </div>
    </div>

    {% if bancontact %}
    <!-- ÉCRAN QR BANCONTACT -->
    <div id="qr" class="overlay" style="display:none;">
        <div class="overlay-box">
            <div style="font-size:34px;font-weight:bold;color:#4da3ff;">📱 PAIEMENT BANCONTACT</div>
            <div class="overlay-total" style="margin:10px 0;">{{ calc.total|euros }}</div>
            <div style="font-size:22px;color:#aaa;margin-bottom:10px;">🔵 {{ qte.bleu }} &nbsp; 🔴 {{ qte.rouge }}</div>
            <img id="qr-img" alt="QR code" style="width:min(48vh,420px);background:white;padding:12px;border-radius:14px;">
            <div style="display:flex;gap:16px;margin-top:18px;">
                <button type="button" class="btn red" style="flex:1;height:100px;margin:0;font-size:32px;" onclick="fermerQR()">✖ Annuler</button>
                <form method="post" action="/payer" style="flex:1;margin:0;" onsubmit="if (this.dataset.envoye) return false; this.dataset.envoye = 1;">
                    <button class="btn green" name="choix" value="BANCONTACT" style="width:100%;height:100px;margin:0;font-size:32px;">✔ Paiement reçu</button>
                </form>
            </div>
        </div>
    </div>
    {% endif %}
    {% endif %}

    <script>
    const TOTAL = {{ calc.total }};
    let recu = 0;
    function fmt(c) { return (c / 100).toFixed(2).replace(".", ",") + " €"; }
    function ouvrirPaiement() { effaceMonnaie(); document.getElementById("paiement").style.display = "flex"; }
    function fermerPaiement() { document.getElementById("paiement").style.display = "none"; }
    function ouvrirQR() {
        document.getElementById("qr-img").src = "/qr.png?t=" + Date.now();
        fermerPaiement();
        document.getElementById("qr").style.display = "flex";
    }
    function fermerQR() { document.getElementById("qr").style.display = "none"; }
    function ajoutMonnaie(c) { recu += c; majMonnaie(); }
    function effaceMonnaie() { recu = 0; majMonnaie(); }
    function majMonnaie() {
        const elRecu = document.getElementById("m-recu");
        if (!elRecu) return;
        elRecu.textContent = fmt(recu);
        const el = document.getElementById("m-rendu");
        const r = recu - TOTAL;
        if (recu === 0) { el.textContent = "—"; el.style.color = ""; }
        else if (r >= 0) { el.textContent = "À rendre : " + fmt(r); el.style.color = "#00ff66"; }
        else { el.textContent = "Manque : " + fmt(-r); el.style.color = "#ff5555"; }
        document.getElementById("m-valeur").value = recu;
        document.getElementById("m-valider").disabled = recu < TOTAL;
    }
    let videArme = null;
    function vider(b) {
        if (videArme) { clearTimeout(videArme); location.href = "/clear"; return; }
        b.classList.add("armed");
        b.textContent = "Toucher encore pour vider";
        videArme = setTimeout(() => {
            b.classList.remove("armed");
            b.textContent = "✖ Vider";
            videArme = null;
        }, 3000);
    }
    </script>
    """, style=STYLE, nom=get_param("nom_compte"), qte=qte, calc=calc, a_des_jetons=a_des_jetons,
        couleurs=COULEURS, macros=MACROS, billets=BILLETS, monnaie=MONNAIE, derniere=derniere,
        bancontact=bancontact_dispo(), carte=option_active("pay_carte"),
        alerte_imprimante=(journal_actif() and bool(ERREUR_JOURNAL["texte"])))


@app.route("/plus/<couleur>/<int:n>")
def plus(couleur, n):
    if couleur in COULEURS:
        q = get_qte()
        q[couleur] += max(0, n)
        save_qte(q)
    return redirect(url_for("index"))


@app.route("/moins/<couleur>")
def moins(couleur):
    if couleur in COULEURS:
        q = get_qte()
        q[couleur] = max(0, q[couleur] - 1)
        save_qte(q)
    return redirect(url_for("index"))


@app.route("/clear")
def clear():
    save_qte({"bleu": 0, "rouge": 0})
    return redirect(url_for("index"))

# =========================================================
# QR CODE BANCONTACT (format EPC / SEPA)
# =========================================================

@app.route("/qr.png")
def qr_png():
    total = calcul(get_qte())["total"]
    iban = get_param("iban").replace(" ", "").upper()
    if qrcode is None or not iban or total <= 0:
        return "QR indisponible", 404
    lignes = [
        "BCD", "002", "1", "SCT", "",
        get_param("nom_compte")[:70],
        iban,
        f"EUR{total / 100:.2f}",
        "", "",
        get_param("communication")[:140],
    ]
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data("\n".join(lignes))
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

# =========================================================
# VENTES / PAIEMENT
# =========================================================

def enregistrer_vente(paiement, recu, rendu):
    q = get_qte()
    c = calcul(q)
    if q["bleu"] + q["rouge"] == 0:
        return None
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ventes (date, qte_bleu, qte_rouge, prix_bleu, prix_rouge, total, paiement, recu, rendu, annulee)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), q["bleu"], q["rouge"],
          c["prix_bleu"], c["prix_rouge"], c["total"], paiement, recu, rendu))
    vente_id = cur.lastrowid
    conn.commit()
    conn.close()
    save_qte({"bleu": 0, "rouge": 0})
    backup_db()
    return vente_id


@app.route("/payer", methods=["POST"])
def payer():
    q = get_qte()
    total = calcul(q)["total"]
    if q["bleu"] + q["rouge"] == 0:
        return redirect(url_for("index"))

    choix = request.form.get("choix", "")
    if choix == "BANCONTACT":
        if not bancontact_dispo():
            return redirect(url_for("index"))
        paiement, recu = "BANCONTACT", total
    elif choix == "CARTE":
        if not option_active("pay_carte"):
            return redirect(url_for("index"))
        paiement, recu = "CARTE", total
    elif choix == "EXACT":
        paiement, recu = "ESPECES", total
    else:
        try:
            recu = int(choix)
        except ValueError:
            return redirect(url_for("index"))
        paiement = "ESPECES"
        recu = max(recu, total)

    rendu = recu - total
    vente_id = enregistrer_vente(paiement, recu, rendu)
    if tiroir_configure() and (paiement == "ESPECES" or get_param("tiroir_quand", "especes") == "tous"):
        ouvrir_tiroir_arriere_plan()
    if vente_id and journal_actif():
        journal_en_arriere_plan(lignes_journal_vente(vente_id))
    session["derniere_vente"] = {
        "id": vente_id, "paiement": paiement, "total": total, "rendu": rendu,
        "bleus": q["bleu"], "rouges": q["rouge"],
    }
    return redirect(url_for("index"))

# =========================================================
# ECRAN CLIENT
# =========================================================

@app.route("/client")
def client():
    qte = get_qte()
    calc = calcul(qte)
    return render_template_string("""
    {{ style|safe }}
    <meta http-equiv="refresh" content="2">
    <header class="jetons">{{ nom }}</header>
    <div style="padding:40px;text-align:center;">
        <h1>Votre commande</h1>
        <div style="max-width:800px;margin:auto;font-size:40px;">
            {% for c in ['bleu', 'rouge'] if qte[c] %}
            <div style="display:flex;justify-content:space-between;border-bottom:1px solid #444;padding:14px;">
                <span>{{ couleurs[c].emoji }} {{ couleurs[c].nom }} x{{ qte[c] }}</span>
                <span>{{ calc['sous_' ~ c]|euros }}</span>
            </div>
            {% endfor %}
        </div>
        <div style="margin-top:40px;"><div style="font-size:30px;">Total à payer</div><div class="big-number">{{ calc.total|euros }}</div></div>
    </div>
    """, style=STYLE, nom=get_param("nom_compte"), qte=qte, calc=calc, couleurs=COULEURS)

# =========================================================
# ADMIN : PAVE NUMERIQUE / LOGIN
# =========================================================

PAVE_HTML = """
<style>
.pave-wrap { display: flex; flex-direction: column; align-items: center; padding: 20px; }
.pave-code { width: 420px; height: 90px; margin-bottom: 18px; background: black; border: 3px solid #444; border-radius: 16px;
             font-size: 56px; letter-spacing: 18px; text-align: center; line-height: 84px; color: white; }
.pave-code.erreur { border-color: #ff3333; animation: secoue .4s; }
@keyframes secoue { 0%,100% { transform: translateX(0); } 25% { transform: translateX(-12px); } 75% { transform: translateX(12px); } }
.pave { display: grid; grid-template-columns: repeat(3, 130px); gap: 16px; }
.pave button { height: 100px; border: none; border-radius: 18px; font-size: 44px; font-weight: bold; color: white; background: #444; cursor: pointer; }
.pave button:active { background: #777; }
.pave .effacer { background: #b9770e; font-size: 34px; }
.pave .ok { background: {{ couleur }}; font-size: 30px; }
.pave-msg { color: #ff5555; font-size: 26px; font-weight: bold; height: 34px; margin-bottom: 6px; }
</style>
<div class="pave-wrap">
    <div class="pave-msg">{% if erreur %}Code incorrect{% endif %}</div>
    <div id="pave-code" class="pave-code {{ 'erreur' if erreur else '' }}">&nbsp;</div>
    <form method="post" id="pave-form">
        <input type="hidden" name="password" id="pave-valeur">
        <div class="pave">
            {% for n in [1,2,3,4,5,6,7,8,9] %}
            <button type="button" onclick="paveTouche('{{ n }}')">{{ n }}</button>
            {% endfor %}
            <button type="button" class="effacer" onclick="paveEfface()">⌫</button>
            <button type="button" onclick="paveTouche('0')">0</button>
            <button type="submit" class="ok">{{ bouton }}</button>
        </div>
    </form>
    <a class="btn gray" href="{{ retour }}" style="width:422px;margin-top:22px;">Retour</a>
</div>
<script>
let paveSaisie = "";
function paveAffiche() {
    document.getElementById("pave-code").innerHTML = paveSaisie ? "●".repeat(paveSaisie.length) : "&nbsp;";
    document.getElementById("pave-valeur").value = paveSaisie;
}
function paveTouche(c) { if (paveSaisie.length < 12) { paveSaisie += c; paveAffiche(); } }
function paveEfface() { paveSaisie = paveSaisie.slice(0, -1); paveAffiche(); }
document.addEventListener("keydown", e => {
    if (/^[0-9]$/.test(e.key)) paveTouche(e.key);
    else if (e.key === "Backspace") paveEfface();
    else if (e.key === "Enter") document.getElementById("pave-form").submit();
});
</script>
"""


def pave_numerique(bouton="Entrer", couleur="#1e9e3e", retour="/", erreur=False):
    return render_template_string(PAVE_HTML, bouton=bouton, couleur=couleur, retour=retour, erreur=erreur)


def admin_required():
    return session.get("admin") is True


def trace_maintenance(evenement):
    """Note sur le journal papier les entrées / sorties de maintenance."""
    if not journal_actif():
        return
    lignes = [f"*** JETONS {evenement} ***"[:32], datetime.now().strftime("%d/%m/%Y %H:%M:%S")]
    if evenement == "ENTREE MAINTENANCE":
        lignes.append(f"{'Especes attendues':<18}{montant_txt(resume_ventes()['attendu']):>14}"[:32])
    lignes.append("-" * 32)
    journal_en_arriere_plan(lignes)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password", "") == read_password():
            session["admin"] = True
            trace_maintenance("ENTREE MAINTENANCE")
            return redirect(url_for("config"))
        trace_maintenance("CODE FAUX")
        return redirect(url_for("login", err=1))
    return render_template_string("""
    {{ style|safe }}
    <header>CODE MAINTENANCE</header>
    {{ pave|safe }}
    """, style=STYLE, pave=pave_numerique(erreur=bool(request.args.get("err"))))


@app.route("/logout")
def logout():
    if session.get("admin"):
        trace_maintenance("SORTIE MAINTENANCE")
    session["admin"] = False
    return redirect(url_for("index"))

# =========================================================
# ADMIN : CONFIGURATION
# =========================================================

@app.route("/config")
def config():
    if not admin_required():
        return redirect(url_for("login"))
    p = {cle: get_param(cle) for cle in DEFAUTS}
    return render_template_string("""
    {{ style|safe }}
    <header>CONFIGURATION — VENTE DE JETONS</header>
    <div style="padding:20px;text-align:center;">
        <div class="card">
            <h2>📋 Paramètres</h2>
            <form method="post" action="/parametres">
                <div class="form-grid">
                    <div><label>Nom de l'événement / du compte</label><input name="nom_compte" value="{{ p.nom_compte }}"></div>
                    <div><label>IBAN (pour le QR Bancontact)</label><input name="iban" value="{{ p.iban }}"></div>
                    <div><label>🔵 Prix jeton bleu (€)</label><input name="prix_bleu" inputmode="decimal" value="{{ p.prix_bleu|euros_input }}"></div>
                    <div><label>🔴 Prix jeton rouge (€)</label><input name="prix_rouge" inputmode="decimal" value="{{ p.prix_rouge|euros_input }}"></div>
                    <div style="grid-column:1 / -1;"><label>Communication du virement (QR)</label><input name="communication" value="{{ p.communication }}"></div>
                </div>
                <button class="btn green" style="margin-top:18px;">💾 Enregistrer</button>
            </form>
        </div>

        <div class="card">
            <h2>🗄️ Tiroir-caisse</h2>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="tiroir_actif">
                <button class="btn {{ 'green' if tiroir_actif else 'gray' }}" style="min-width:300px;">🗄️ Tiroir : {{ 'OUI' if tiroir_actif else 'NON' }}</button>
            </form>
            {% if tiroir_actif %}<p style="font-size:22px;">{{ tiroir_libelle }}</p>{% endif %}
            {% if tiroir_ok %}
            <form method="post" action="/tiroir/ouvrir" style="display:inline;">
                <button class="btn orange">Ouvrir le tiroir</button>
            </form>
            {% endif %}
            {% if tiroir_actif %}<a class="btn blue" href="/tiroir">Réglage / test du tiroir</a>{% endif %}
        </div>

        <div class="card">
            <h2>💶 Fond de caisse</h2>
            <p style="color:#aaa;">Montant en espèces dans le tiroir au début de la journée</p>
            <form method="post" action="/fond_caisse">
                <input name="fond" inputmode="decimal" value="{{ r.fond|euros_input }}" style="width:180px;text-align:right;font-size:32px;"> €
                <button class="btn green">Enregistrer</button>
            </form>
            <div style="font-size:24px;margin-top:10px;">
                Fond {{ r.fond|euros }} + ventes espèces {{ r.especes|euros }}
                = <b style="color:#00ff66;">attendu {{ r.attendu|euros }}</b>
            </div>
        </div>

        <div class="card">
            <h2>Moyens de paiement</h2>
            <p style="color:#aaa;">💶 Espèces : toujours actif — toucher un bouton pour activer / désactiver</p>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="pay_bancontact">
                <button class="btn {{ 'blue' if bancontact else 'gray' }}">📱 Bancontact QR : {{ 'OUI' if bancontact else 'NON' }}</button>
            </form>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="pay_carte">
                <button class="btn {{ 'blue' if carte else 'gray' }}">💳 Carte : {{ 'OUI' if carte else 'NON' }}</button>
            </form>
            {% if bancontact and not p.iban %}<p style="color:#f1c40f;">⚠ Aucun IBAN : le bouton Bancontact reste caché.</p>{% endif %}
            {% if not qr_ok %}<p style="color:#f1c40f;">⚠ Module qrcode manquant : relancer installer.sh</p>{% endif %}
        </div>

        <div class="card">
            <h2>Impression au fil de l'eau</h2>
            <p style="color:#aaa;">Ventes, fond de caisse, annulations et accès à la maintenance sont imprimés à la suite, sans couper le papier</p>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="print_journal">
                <button class="btn {{ 'green' if journal else 'gray' }}">🧾 Journal : {{ 'OUI' if journal else 'NON' }}</button>
            </form>
            <form method="post" action="/avance_papier" style="display:inline;">
                <button class="btn gray">⇡ Avancer le papier</button>
            </form>
            {% if erreur_journal %}<p style="color:#ff5555;">Dernière erreur imprimante : {{ erreur_journal }}</p>{% endif %}
        </div>

        <a class="btn blue" href="/ventes">Historique ventes</a><br>
        <a class="btn green" href="/stats">Statistiques</a><br>
        <a class="btn green" href="/fin_service">Rapport Z + impression</a><br>
        <a class="btn purple" href="/change_password">Changer mot de passe</a><br>
        <a class="btn orange" href="/backup">Sauvegarde DB</a><br>
        <a class="btn red" href="/reset">RESET HISTORIQUE</a><br>
        <a class="btn gray" href="/logout">Retour caisse (quitter la maintenance)</a>
        <div class="card" style="margin-top:40px;">
            <button type="button" class="btn red" style="width:100%;max-width:600px;height:90px;font-size:30px;"
                    onclick="fermerFenetre()">✖ Fermer la fenêtre (ALT + F4)</button>
            <script>
            function fermerFenetre() {
                if (!confirm("Fermer la fenêtre de la caisse jetons ?")) return;
                // Prévient le serveur (trace + fermeture de secours), puis ferme la fenêtre comme ALT + F4
                fetch("/fermer_fenetre", {method: "POST"}).finally(() => {
                    window.close();
                    setTimeout(() => { document.body.innerHTML =
                        '<h1 style="text-align:center;margin-top:120px;">Fermeture de la fenêtre…</h1>'; }, 300);
                });
            }
            </script>
        </div>
    </div>
    """, style=STYLE, p=p, r=resume_ventes(),
        bancontact=option_active("pay_bancontact"), carte=option_active("pay_carte"),
        journal=journal_actif(), qr_ok=qrcode is not None, erreur_journal=ERREUR_JOURNAL["texte"],
        tiroir_ok=tiroir_configure(), tiroir_libelle=libelle_tiroir(),
        tiroir_actif=option_active("tiroir_actif"))


@app.route("/parametres", methods=["POST"])
def parametres():
    if not admin_required():
        return redirect(url_for("login"))
    f = request.form
    set_param("nom_compte", f.get("nom_compte", "").strip() or DEFAUTS["nom_compte"])
    set_param("iban", f.get("iban", "").strip())
    set_param("communication", f.get("communication", "").strip())
    set_param("prix_bleu", max(0, parse_euros(f.get("prix_bleu", "0"))))
    set_param("prix_rouge", max(0, parse_euros(f.get("prix_rouge", "0"))))
    save_qte({"bleu": 0, "rouge": 0})
    return redirect(url_for("config"))


@app.route("/fond_caisse", methods=["POST"])
def fond_caisse():
    if not admin_required():
        return redirect(url_for("login"))
    fond = max(0, parse_euros(request.form.get("fond", "0")))
    set_param("fond_caisse", fond)
    if journal_actif():
        journal_en_arriere_plan([
            f"JETONS FOND CAISSE {datetime.now().strftime('%d/%m %H:%M')}"[:32],
            f"{'Montant':<20}{montant_court(fond):>8} EUR",
            "-" * 32,
        ])
    return redirect(url_for("config"))


@app.route("/option", methods=["POST"])
def basculer_option():
    if not admin_required():
        return redirect(url_for("login"))
    cle = request.form.get("cle", "")
    if cle in OPTIONS:
        set_param(cle, "0" if option_active(cle) else "1")
    return redirect(url_for("config"))


@app.route("/avance_papier", methods=["POST"])
def avance_papier():
    if not admin_required():
        return redirect(url_for("login"))
    imprimer_journal([""] * 6, gras_premiere=False)
    return redirect(url_for("config"))

# =========================================================
# ADMIN : TIROIR-CAISSE
# =========================================================

@app.route("/tiroir/ouvrir", methods=["POST"])
def tiroir_ouvrir_manuel():
    if not admin_required():
        return redirect(url_for("login"))
    ok, msg = ouvrir_tiroir()
    if ok and journal_actif():
        journal_en_arriere_plan([
            "*** JETONS OUVERTURE TIROIR ***"[:32],
            datetime.now().strftime("%d/%m/%Y %H:%M:%S") + "  (manuel)",
            "-" * 32,
        ])
    return redirect(url_for("tiroir", msg=("✔ " if ok else "✖ ") + msg))


@app.route("/tiroir", methods=["GET", "POST"])
def tiroir():
    if not admin_required():
        return redirect(url_for("login"))
    msg = request.args.get("msg", "")
    if request.method == "POST":
        action = request.form.get("action")
        methode = request.form.get("methode", "aucun")
        port = request.form.get("port", "")
        if action == "tester":
            ok, m = ouvrir_tiroir(methode, port or None)
            msg = ("✔ " if ok else "✖ ") + m + (" — le tiroir s'est-il ouvert ?" if ok else "")
            return render_page_tiroir(msg, methode, port)
        if action == "choisir":
            set_param("tiroir_methode", methode)
            if port:
                set_param("tiroir_port", port)
            return redirect(url_for("tiroir", msg="Réglage enregistré : " + libelle_tiroir()))
        if action == "quand":
            set_param("tiroir_quand", "tous" if request.form.get("quand") == "tous" else "especes")
            return redirect(url_for("tiroir", msg="Réglage enregistré : " + libelle_tiroir()))
        if action == "reprendre_bar":
            importer_tiroir_du_bar()
            return redirect(url_for("tiroir", msg="Réglage enregistré : " + libelle_tiroir()))
    return render_page_tiroir(msg, None, None)


def render_page_tiroir(msg, dernier_methode, dernier_port):
    essais = [("imprimante", "")]
    for p in ports_paralleles():
        essais += [("parallele_data", p), ("parallele_ctrl", p)]
    for m in ("serie_dtr", "serie_rts", "serie_octet"):
        for p in ports_series():
            essais.append((m, p))
    return render_template_string("""
    {{ style|safe }}
    <header>TIROIR-CAISSE</header>
    <div style="padding:20px;text-align:center;">
        <div class="card">
            <h2>Réglage actuel</h2>
            <p style="font-size:24px;">{{ libelle }}</p>
            {% if msg %}<p style="font-size:26px;font-weight:bold;color:{{ '#00ff66' if msg.startswith('✔') or msg.startswith('Réglage') else '#ff5555' }};">{{ msg }}</p>{% endif %}
            <form method="post" style="margin-top:10px;">
                <input type="hidden" name="action" value="quand">
                <button class="btn {{ 'green' if quand == 'especes' else 'gray' }}" name="quand" value="especes">Ouvrir pour les espèces seulement</button>
                <button class="btn {{ 'green' if quand == 'tous' else 'gray' }}" name="quand" value="tous">Ouvrir à chaque paiement</button>
            </form>
            {% if bar_dispo %}
            <form method="post" style="margin-top:10px;">
                <button class="btn purple" name="action" value="reprendre_bar">Reprendre le réglage de la caisse bar</button>
            </form>
            {% endif %}
        </div>

        <div class="card">
            <h2>Trouver la bonne méthode</h2>
            <p style="color:#aaa;font-size:20px;">Touchez « Tester » ligne par ligne jusqu'à ce que le tiroir s'ouvre, puis « Utiliser ».</p>
            <table>
                <tr><th>Méthode</th><th>Port</th><th></th></tr>
                {% for m, p in essais %}
                <tr {% if m == dernier_methode and p == (dernier_port or '') %}style="background:#2c3e50;"{% endif %}>
                    <td style="text-align:left;">{{ methodes[m] }}</td>
                    <td>{{ nom_com(p) if p else '—' }}</td>
                    <td style="white-space:nowrap;">
                        <form method="post" style="display:inline;">
                            <input type="hidden" name="methode" value="{{ m }}"><input type="hidden" name="port" value="{{ p }}">
                            <button class="btn blue" name="action" value="tester">Tester</button>
                            <button class="btn green" name="action" value="choisir">Utiliser</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
                <tr>
                    <td style="text-align:left;">{{ methodes['aucun'] }}</td><td>—</td>
                    <td><form method="post"><input type="hidden" name="methode" value="aucun">
                        <button class="btn gray" name="action" value="choisir">Utiliser</button></form></td>
                </tr>
            </table>
        </div>
        <a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, msg=msg, libelle=libelle_tiroir(), quand=get_param("tiroir_quand", "especes"),
        essais=essais, methodes=METHODES_TIROIR, nom_com=nom_com, bar_dispo=os.path.exists(DB_BAR),
        dernier_methode=dernier_methode, dernier_port=dernier_port)

# =========================================================
# ADMIN : FERMER LA FENETRE
# =========================================================

@app.route("/fermer_fenetre", methods=["POST"])
def fermer_fenetre():
    """Ferme seulement la fenêtre du navigateur. Le serveur de caisse continue de tourner."""
    if not admin_required():
        return redirect(url_for("login"))
    trace_maintenance("SORTIE MAINTENANCE")
    session["admin"] = False
    threading.Thread(target=fermer_fenetre_plus_tard, daemon=True).start()
    return render_template_string("""
    {{ style|safe }}
    <header>FERMETURE…</header>
    <div style="padding:60px;text-align:center;font-size:30px;">Fermeture de la fenêtre…</div>
    """, style=STYLE)

# =========================================================
# ADMIN : HISTORIQUE / STATS / RAPPORT Z
# =========================================================

@app.route("/ventes")
def ventes():
    if not admin_required():
        return redirect(url_for("login"))
    conn = db()
    rows = conn.execute("SELECT * FROM ventes ORDER BY id DESC").fetchall()
    conn.close()
    return render_template_string("""
    {{ style|safe }}
    <header>HISTORIQUE VENTES</header>
    <div style="padding:20px;">
        <table>
            <tr><th>N°</th><th>Heure</th><th>🔵</th><th>🔴</th><th>Total</th><th>Paiement</th><th>Rendu</th><th>Statut</th><th>Action</th></tr>
            {% for v in rows %}
            <tr {% if v.annulee %}style="opacity:.5;"{% endif %}>
                <td>{{ v.id }}</td><td>{{ v.date[11:19] }}</td>
                <td class="jeton-b">{{ v.qte_bleu }}</td><td class="jeton-r">{{ v.qte_rouge }}</td>
                <td>{{ v.total|euros }}</td><td>{{ v.paiement }}</td>
                <td>{% if v.paiement == 'ESPECES' and v.rendu %}{{ v.rendu|euros }}{% endif %}</td>
                <td>{% if v.annulee %}<span style="color:red;">Annulée</span>{% else %}OK{% endif %}</td>
                <td>{% if not v.annulee %}<a class="btn red" href="/annuler_vente/{{ v.id }}" onclick="return confirm('Annuler la vente n° {{ v.id }} ?');">Annuler</a>{% endif %}</td>
            </tr>
            {% else %}
            <tr><td colspan="9" style="text-align:center;color:#aaa;">Aucune vente</td></tr>
            {% endfor %}
        </table><br><a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, rows=rows)


@app.route("/annuler_vente/<int:vente_id>")
def annuler_vente(vente_id):
    if not admin_required():
        return redirect(url_for("login"))
    conn = db()
    v = conn.execute("SELECT * FROM ventes WHERE id = ? AND annulee = 0", (vente_id,)).fetchone()
    conn.execute("UPDATE ventes SET annulee = 1 WHERE id = ?", (vente_id,))
    conn.commit()
    conn.close()
    backup_db()
    if v and journal_actif():
        journal_en_arriere_plan([
            f"JETONS ANNULATION #{v['id']} {datetime.now().strftime('%H:%M')}"[:32],
            f"{'Montant':<20}{'-' + montant_court(v['total']):>8} EUR"[:32],
            "-" * 32,
        ])
    return redirect(url_for("ventes"))


RESUME_HTML = """
{{ style|safe }}
<header>{{ titre }}</header>
<div style="padding:20px;text-align:center;">
    {% if imprime %}
        {% if message %}<h1 style="color:#00ff66;">{{ message }}</h1>
        {% else %}<h1 style="color:#ff5555;">Erreur impression</h1>
            <div class="card"><pre style="white-space:pre-wrap;font-size:20px;text-align:left;">{{ erreur }}</pre></div>
        {% endif %}
    {% endif %}

    <div style="display:flex;flex-wrap:wrap;">
        <div class="card" style="flex:1;"><h2>Total</h2><div class="big-number">{{ r.g.total|euros }}</div>
            <div>{{ r.g.nb }} vente{% if r.g.nb > 1 %}s{% endif %}</div></div>
        <div class="card" style="flex:1;"><h2>🔵 Bleus</h2><div class="big-number" style="color:#3399ff;">{{ r.g.bleus }}</div>
            <div>{{ r.g.montant_bleus|euros }}</div></div>
        <div class="card" style="flex:1;"><h2>🔴 Rouges</h2><div class="big-number" style="color:#ff4d4d;">{{ r.g.rouges }}</div>
            <div>{{ r.g.montant_rouges|euros }}</div></div>
    </div>

    <div class="card"><h2>Par type de paiement</h2>
        <table style="max-width:800px;margin:auto;">
            <tr><th>Paiement</th><th>Nombre</th><th>Total</th></tr>
            {% for pp in r.par_paiement %}<tr><td>{{ pp.paiement }}</td><td>{{ pp.nb }}</td><td>{{ pp.total|euros }}</td></tr>
            {% else %}<tr><td colspan="3" style="color:#aaa;">Aucune vente</td></tr>{% endfor %}
        </table>
    </div>

    {{ bloc_especes|safe }}

    {% if imprime %}<a class="btn green" href="/fin_service">Réimprimer</a>{% endif %}
    <a class="btn gray" href="/config">Retour configuration</a>
</div>
"""


@app.route("/stats")
def stats():
    if not admin_required():
        return redirect(url_for("login"))
    r = resume_ventes()
    return render_template_string(RESUME_HTML, style=STYLE, titre="STATISTIQUES", imprime=False, r=r,
                                  bloc_especes=render_template_string(BLOC_CAISSE_ESPECES, r=r))


@app.route("/fin_service")
def fin_service():
    if not admin_required():
        return redirect(url_for("login"))
    r = resume_ventes()
    message, erreur = "", ""
    try:
        imprimer_rapport_z()
        message = "Rapport Z imprimé correctement"
    except Exception as e:
        erreur = str(e)
    return render_template_string(RESUME_HTML, style=STYLE, titre="RAPPORT Z / FIN DE SERVICE", imprime=True,
                                  message=message, erreur=erreur, r=r,
                                  bloc_especes=render_template_string(BLOC_CAISSE_ESPECES, r=r))

# =========================================================
# ADMIN : MOT DE PASSE / BACKUP / RESET
# =========================================================

@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if not admin_required():
        return redirect(url_for("login"))
    message = ""
    if request.method == "POST":
        old = request.form.get("old", "")
        new1 = request.form.get("new1", "")
        new2 = request.form.get("new2", "")
        if old != read_password():
            message = "Ancien mot de passe incorrect"
        elif not new1:
            message = "Nouveau mot de passe vide"
        elif not new1.isdigit() or len(new1) > 12:
            message = "Le code doit contenir uniquement des chiffres (12 max)"
        elif new1 != new2:
            message = "Les deux mots de passe ne correspondent pas"
        else:
            write_password(new1)
            message = "Mot de passe modifié"
    return render_template_string("""
    {{ style|safe }}
    <header>CHANGER MOT DE PASSE</header>
    <div style="padding:30px;text-align:center;">
        {% if message %}<h2>{{ message }}</h2>{% endif %}
        <form method="post">
            <input type="password" name="old" inputmode="numeric" placeholder="Ancien code"><br><br>
            <input type="password" name="new1" inputmode="numeric" placeholder="Nouveau code (chiffres)"><br><br>
            <input type="password" name="new2" inputmode="numeric" placeholder="Confirmer"><br><br>
            <button class="btn green">Sauver</button><a class="btn gray" href="/config">Retour</a>
        </form>
    </div>
    """, style=STYLE, message=message)


@app.route("/backup")
def backup():
    if not admin_required():
        return redirect(url_for("login"))
    backup_db()
    return render_template_string("""
    {{ style|safe }}
    <header>SAUVEGARDE</header>
    <div style="padding:30px;text-align:center;"><h1>Sauvegarde effectuée</h1><p>Dossier : <b>{{ backup_dir }}</b></p><a class="btn gray" href="/config">Retour</a></div>
    """, style=STYLE, backup_dir=BACKUP_DIR)


@app.route("/reset", methods=["GET", "POST"])
def reset():
    if not admin_required():
        return redirect(url_for("login"))
    erreur = False
    if request.method == "POST":
        if request.form.get("password", "") == read_password():
            backup_db()
            conn = db()
            conn.execute("DELETE FROM ventes")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'ventes'")
            conn.commit()
            conn.close()
            if journal_actif():
                journal_en_arriere_plan([
                    "*** JETONS RESET HISTORIQUE ***"[:32],
                    datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "Prochaine vente : n 1",
                    "-" * 32,
                ])
            return redirect(url_for("config"))
        erreur = True
    return render_template_string("""
    {{ style|safe }}
    <header>RESET HISTORIQUE</header>
    <h2 style="text-align:center;color:#ff5555;margin:15px 0 0;">Attention : cela efface les ventes de jetons.</h2>
    {{ pave|safe }}
    """, style=STYLE, pave=pave_numerique(bouton="Confirmer reset", couleur="#c0392b", retour="/config", erreur=erreur))

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    init_db()
    read_password()
    try:
        preparer_profil_firefox()
    except Exception as e:
        print("Profil Firefox :", e)
    # lancer_caisse_jetons.sh ouvre lui-même Firefox : il passe --sans-navigateur
    if "--sans-navigateur" not in sys.argv:
        threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
