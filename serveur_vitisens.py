#!/usr/bin/env python3
"""
VITI Sens — Serveur local
==========================
Application web locale pour gérer les bulletins de conseil viticole.
Lance un serveur sur http://localhost:5000

Usage:
    python serveur_vitisens.py

Prérequis:
    pip install flask python-docx requests openpyxl
"""

import os, sys, re, json, sqlite3, io, tempfile, math, secrets, smtplib, shutil
from datetime import datetime, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import stripe
except ImportError:
    stripe = None

# Modèle épidémiologique
from modele_epidemio import (
    calc_maturite_oeufs, detecter_contaminations_mildiou, duree_incubation,
    calc_risque_oidium_journalier, calc_indice_sortie_hiver_oidium, synthese_risque_7j
)

# Word generation
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml

try:
    import requests as req_lib
except ImportError:
    req_lib = None

try:
    from fiche_vendange_parser import parser_fiche_vendange
except ImportError:
    parser_fiche_vendange = None

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)

DB_PATH = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "vitisens.db")
DOCS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')
os.makedirs(DOCS_DIR, exist_ok=True)
COORDS = {"lat": 49.25, "lon": 3.96}

# ===== SaaS : essai gratuit / paiement =====
LIMITE_JOURS_ESSAI = 3
PRIX_ANNUEL = "89 €/an"
PRIX_LANCEMENT = "59 €/an"
LIMITE_OFFRE_LANCEMENT = int(os.environ.get("LIMITE_OFFRE_LANCEMENT", "100"))  # nb de comptes éligibles
DOMAIN = os.environ.get("DOMAIN", "http://localhost:5000")

SMTP_HOST = os.environ.get("SMTP_HOST", "ssl0.ovh.net")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "florent.miguel@sasu-viti-sens.fr")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")                    # 89 €/an, tarif normal
STRIPE_PRICE_ID_LANCEMENT = os.environ.get("STRIPE_PRICE_ID_LANCEMENT", "")  # 59 €/an, offre de lancement
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Mot de passe de l'espace admin (Florent) — obligatoire en production
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Chemins publics (pas de mot de passe requis) : inscription, connexion, portail vigneron, PWA
_PUBLIC_PREFIXES = (
    '/api/portail/', '/api/portail-s/', '/portail/', '/portail-s/',
    '/api/inscription', '/inscription', '/api/connexion', '/connexion',
    '/mon-espace', '/deconnexion', '/api/stripe/webhook', '/demo',
    '/admin-connexion', '/api/admin-connexion',
    '/mot-de-passe-oublie', '/api/mot-de-passe-oublie',
    '/reinitialiser-mot-de-passe', '/api/reinitialiser-mot-de-passe',
    '/manifest.json', '/sw.js', '/favicon.ico', '/.well-known/',
    '/icon-', '/apple-touch-icon',
)

@app.before_request
def proteger_admin():
    path = request.path
    if path == '/' or path.startswith(_PUBLIC_PREFIXES):
        return None
    if path.startswith('/api/') or path.startswith('/admin') or path == '/notice-vendanges':
        if not ADMIN_PASSWORD:
            # Pas de mot de passe configuré (ex. dev local) : on n'entrave rien
            return None
        if not session.get('is_admin'):
            if path.startswith('/api/'):
                return jsonify({"error": "Non autorisé — connectez-vous sur /admin-connexion"}), 401
            return redirect('/admin-connexion')
    return None


_ROUTE_ID_RE = re.compile(r'^/api/portail(?:-s)?/([^/]+)(/.*)?$')
_FONCTIONS_ACCES_COMPLET = (
    '/itineraire', '/itineraire/date-optimale', '/itineraire/export-pdf',
    '/rendements/export-csv', '/rendements/export-pdf',
    '/exploitation/export-csv', '/exploitation/export-pdf',
    '/carnet-vendange/export-pdf', '/carnet-vendange/export-pdf-total',
)

@app.before_request
def proteger_acces_essai():
    """Comptes SaaS (inscription publique) uniquement — jamais les clients admin de
    Florent. Deux règles, indépendantes du statut du jour :
    1) Itinéraire de récolte + tous les exports PDF/CSV : réservés à l'accès complet,
       du premier au dernier jour de l'essai.
    2) Après {LIMITE_JOURS_ESSAI} jours, toute écriture (créer/modifier/supprimer) est
       bloquée — la lecture reste possible pour ne pas donner l'impression de perdre
       ses données."""
    m = _ROUTE_ID_RE.match(request.path)
    if not m:
        return None
    client = get_client_by_token_or_slug(m.group(1))
    if not client or (client.get('statut_compte') or 'essai') == 'payant':
        return None
    if not _est_compte_saas(client):
        return None  # client créé depuis l'admin Florent : jamais bridé

    suffix = m.group(2) or ''
    if suffix in _FONCTIONS_ACCES_COMPLET:
        return jsonify({
            "error": "fonction_acces_complet",
            "message": "L'itinéraire de récolte et les exports PDF/CSV sont réservés à l'accès complet."
        }), 403

    if request.method in ('POST', 'PUT', 'DELETE'):
        jours_restants = _jours_restants_essai(client)
        if jours_restants is not None and jours_restants <= 0:
            return jsonify({
                "error": "essai_expire",
                "message": f"Votre essai gratuit de {LIMITE_JOURS_ESSAI} jours est terminé. "
                           f"Passez à l'accès complet ({PRIX_ANNUEL}) pour continuer à modifier vos données."
            }), 403
    return None


@app.route('/demo')
def demo_redirect():
    # Adresse à communiquer pour la démo : envoie vers l'inscription, avec chargement
    # automatique des données d'exemple juste après la création du compte.
    return redirect('/inscription?demo=1')


_ADMIN_LOGIN_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VITI Sens — Admin</title>
<style>
body{font-family:-apple-system,sans-serif;background:#f8faf8;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.1);width:100%;max-width:340px}
h1{font-size:18px;color:#2D6A4F;margin-bottom:18px;text-align:center}
input{width:100%;padding:11px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:14px;margin-bottom:12px;box-sizing:border-box}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#2D6A4F;color:#fff;font-weight:700;cursor:pointer;font-size:14px}
.err{color:#C1121F;font-size:13px;margin-top:10px;text-align:center}
</style></head><body>
<div class="card">
<h1>🔒 Espace admin VITI Sens</h1>
<form id="f"><input type="password" id="pwd" placeholder="Mot de passe admin" autofocus>
<button type="submit">Entrer</button><div class="err" id="err"></div></form>
</div>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const r = await fetch('/api/admin-connexion', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('pwd').value})});
  if(r.ok){ window.location.href = '/admin-vitisens'; }
  else { document.getElementById('err').textContent = 'Mot de passe incorrect.'; }
});
</script></body></html>"""

@app.route('/admin-connexion')
def page_admin_connexion():
    return _ADMIN_LOGIN_HTML

@app.route('/api/admin-connexion', methods=['POST'])
def api_admin_connexion():
    d = request.json or {}
    if ADMIN_PASSWORD and d.get('password') == ADMIN_PASSWORD:
        session.permanent = True
        session['is_admin'] = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "Mot de passe incorrect"}), 401
METEO_DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum,relative_humidity_2m_mean,dewpoint_2m_min,dewpoint_2m_max,et0_fao_evapotranspiration,wind_speed_10m_max,sunshine_duration,leaf_wetness_probability_mean,precipitation_probability_max"
METEO_HOURLY = "soil_moisture_0_to_1cm,soil_temperature_6cm"

import math

def calc_dewpoint(temp, hr):
    """Point de rosée (formule de Magnus)"""
    if hr is None or hr <= 0 or temp is None: return None
    a = 17.27; b = 237.7
    gamma = (a * temp / (b + temp)) + math.log(hr / 100.0)
    return round(b * gamma / (a - gamma), 1)

def calc_risk_oidium(tmoy, tmin, hr, dewpoint):
    """Risque oïdium basé sur T°, humidité et point de rosée"""
    # Conditions favorables: T° 15-28°C, HR > 40%, condensation nocturne
    if tmoy is None: return "—"
    # Condensation nocturne si T° min proche du point de rosée (< 2°C d'écart)
    condensation = dewpoint is not None and tmin is not None and (tmin - dewpoint) < 2
    if tmoy >= 20 and tmoy <= 25 and (hr is None or hr > 60):
        return "Élevé" if condensation else "Élevé"
    if tmoy >= 15 and tmoy <= 28 and (hr is None or hr > 40):
        return "Élevé" if condensation else "Modéré"
    if tmoy >= 12 and tmoy <= 30:
        return "Modéré" if condensation else "Faible"
    return "Nul"

def calc_leaf_wetness(tmin, dewpoint, hr, pluie):
    """Estimation humectation foliaire (0-100%)
    Croise: HR>90%, T° min proche point de rosée, pluie récente
    Pas de capteur leaf wetness dans Open-Meteo, mais cette estimation
    est fiable pour le calcul de risque mildiou."""
    if hr is None: return None
    score = 0
    # HR élevée = condensation probable
    if hr >= 95: score += 40
    elif hr >= 90: score += 25
    elif hr >= 80: score += 10
    # T° min proche du point de rosée = rosée matinale
    if dewpoint is not None and tmin is not None:
        ecart = tmin - dewpoint
        if ecart < 1: score += 35
        elif ecart < 2: score += 20
        elif ecart < 3: score += 10
    # Pluie = humectation directe
    if pluie >= 5: score += 25
    elif pluie >= 2: score += 15
    elif pluie > 0: score += 5
    return min(score, 100)

def interpret_soil_moisture(sm):
    """Convertit l'indice d'humidité du sol en texte"""
    if sm is None: return "—"
    if sm <= 0.15: return "Très sec"
    if sm <= 0.25: return "Sec"
    if sm <= 0.35: return "Frais"
    if sm <= 0.45: return "Humide"
    return "Saturé"

def calc_risk_mildiou_avance(pluie, tmoy, hr, soil_moisture, leaf_wetness):
    """Risque mildiou enrichi avec paramètres agricoles"""
    if pluie < 2 and (leaf_wetness is None or leaf_wetness < 30):
        return "Nul"
    if tmoy < 11:
        return "Faible"
    # Soil moisture influence
    sol_sec = soil_moisture is not None and soil_moisture < 0.20
    sol_humide = soil_moisture is not None and soil_moisture > 0.35
    # Leaf wetness influence
    lw_ok = leaf_wetness is not None and leaf_wetness >= 50
    if pluie >= 10 and sol_humide:
        return "Très élevé"
    if pluie >= 10:
        return "Élevé"
    if pluie >= 2 and sol_sec:
        return "Modéré" if lw_ok else "Faible"
    if pluie >= 2 and hr is not None and hr >= 85:
        return "Élevé"
    if pluie >= 2:
        return "Modéré"
    # Contamination possible sans pluie si forte humectation
    if lw_ok and hr is not None and hr >= 90:
        return "Modéré"
    return "Faible"

# ===== DATABASE =====
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY,
        exploitation TEXT NOT NULL,
        interlocuteur TEXT,
        commune TEXT,
        secteur TEXT,
        surface REAL,
        pct_chard REAL DEFAULT 0,
        pct_pn REAL DEFAULT 0,
        pct_meunier REAL DEFAULT 0,
        pct_autre REAL DEFAULT 0,
        certification TEXT DEFAULT 'Conventionnel',
        sencrop TEXT,
        id_station TEXT,
        parcelles_mildiou TEXT,
        parcelles_oidium TEXT,
        historique_gel TEXT,
        cu_cumule REAL DEFAULT 0,
        email TEXT,
        telephone TEXT,
        latitude REAL,
        longitude REAL,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS catalogue (
        id TEXT PRIMARY KEY,
        nom TEXT NOT NULL,
        categorie TEXT DEFAULT 'Phyto',
        substance_active TEXT,
        famille TEXT,
        code_frac TEXT,
        mode_action TEXT,
        partenaire TEXT,
        cible TEXT,
        type_cps TEXT,
        dose_homologuee TEXT,
        nb_max_appli TEXT,
        dar INTEGER,
        dre INTEGER,
        znt INTEGER,
        dsppr INTEGER,
        compatible_bio INTEGER DEFAULT 0,
        compatible_hve INTEGER DEFAULT 1,
        compatible_vdc TEXT DEFAULT 'Oui',
        biocontrole INTEGER DEFAULT 0,
        position_strategie TEXT,
        option_abc TEXT,
        prix TEXT,
        concentration_ma REAL DEFAULT 0,
        unite_concentration TEXT DEFAULT 'g/kg',
        -- MFSC specific fields
        type_mfsc TEXT,
        composition_npk TEXT,
        oligo_elements TEXT,
        matiere_organique TEXT,
        ph TEXT,
        stade_application TEXT,
        objectif TEXT,
        amm_mfsc TEXT,
        norme TEXT,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS prescriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_client TEXT NOT NULL,
        cible TEXT,
        passage TEXT,
        id_produit TEXT,
        nom_produit TEXT,
        substance_active TEXT,
        type_cps TEXT,
        dose_homologuee TEXT,
        dose_prescrite TEXT,
        volume_bouillie TEXT,
        date_prevue TEXT,
        observations TEXT,
        applique TEXT DEFAULT 'Non',
        date_reelle TEXT,
        dose_reelle TEXT,
        FOREIGN KEY (id_client) REFERENCES clients(id)
    );

    CREATE TABLE IF NOT EXISTS bulletin_hebdo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_saisie TEXT DEFAULT CURRENT_TIMESTAMP,
        numero_av TEXT,
        date_av TEXT,
        maturite TEXT,
        epi TEXT,
        risque_mildiou TEXT,
        reco_mildiou TEXT,
        risque_oidium TEXT,
        reco_oidium TEXT,
        gel TEXT,
        mange_bourgeons TEXT,
        stade_chard TEXT,
        comment_chard TEXT,
        stade_pn TEXT,
        comment_pn TEXT,
        stade_meunier TEXT,
        comment_meunier TEXT,
        avance TEXT,
        heterogeneite TEXT,
        titre_complement TEXT,
        contenu_complement TEXT,
        texte_phenologie TEXT
    );

    CREATE TABLE IF NOT EXISTS suivi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_visite TEXT,
        id_client TEXT,
        stade_chard TEXT,
        stade_pn TEXT,
        stade_meunier TEXT,
        degats_gel REAL,
        pluie REAL,
        temp REAL,
        etat_sol TEXT,
        maturite TEXT,
        risque_mildiou TEXT,
        risque_oidium TEXT,
        traitement TEXT,
        produit TEXT,
        dose TEXT,
        prochain TEXT,
        observations TEXT,
        FOREIGN KEY (id_client) REFERENCES clients(id)
    );

    CREATE TABLE IF NOT EXISTS parcelles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_client TEXT NOT NULL,
        nom TEXT NOT NULL,
        lieu_dit TEXT,
        cepage TEXT,
        surface_cadastrale REAL,
        nb_pieds_ha INTEGER,
        commune TEXT,
        notes TEXT,
        FOREIGN KEY (id_client) REFERENCES clients(id)
    );

    CREATE TABLE IF NOT EXISTS rendements (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        id_parcelle     INTEGER NOT NULL,
        id_client       TEXT NOT NULL,
        campagne        TEXT DEFAULT '2026',
        date_releve     TEXT NOT NULL,
        nb_grappes_pied REAL NOT NULL,
        poids_moyen_g   REAL NOT NULL,
        nb_pieds_ha     INTEGER,
        rendement_kgha  REAL,
        rendement_reel_kgha REAL,
        observations    TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (id_parcelle) REFERENCES parcelles(id),
        FOREIGN KEY (id_client)   REFERENCES clients(id)
    );


    CREATE TABLE IF NOT EXISTS maturite_fiches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_parcelle INTEGER NOT NULL,
        id_client TEXT NOT NULL,
        campagne TEXT DEFAULT '2026',
        date_fiche TEXT NOT NULL,
        degre_probable REAL,
        AT REAL,
        couleur_pepins INTEGER,
        saveur_pulpe INTEGER,
        tanins INTEGER,
        poids_grappe REAL,
        grappes_pied REAL,
        rendement_kgha REAL,
        score_total INTEGER,
        recommandation TEXT,
        observations TEXT,
        saisie_par TEXT DEFAULT 'client',
        FOREIGN KEY (id_parcelle) REFERENCES parcelles(id),
        FOREIGN KEY (id_client) REFERENCES clients(id)
    );
    """)
    # Migrations pour bases existantes
    for alter in [
        "ALTER TABLE bulletin_hebdo ADD COLUMN passage_en_cours TEXT",
        "ALTER TABLE bulletin_hebdo ADD COLUMN no_presc_motif TEXT",
        "ALTER TABLE catalogue ADD COLUMN partenaire TEXT",
        "ALTER TABLE clients ADD COLUMN latitude REAL",
        "ALTER TABLE clients ADD COLUMN portail_slug TEXT",
        "ALTER TABLE clients ADD COLUMN longitude REAL",
        "ALTER TABLE clients ADD COLUMN portail_token TEXT",
        "ALTER TABLE maturite_fiches ADD COLUMN etat_sanitaire REAL DEFAULT 0",
        "ALTER TABLE maturite_fiches ADD COLUMN gelee INTEGER DEFAULT 0",
        "ALTER TABLE maturite_fiches ADD COLUMN grele INTEGER DEFAULT 0",
        "ALTER TABLE maturite_fiches ADD COLUMN nb_pieds REAL",
        "ALTER TABLE maturite_fiches ADD COLUMN surface_ha REAL",
        "ALTER TABLE maturite_fiches ADD COLUMN reco_niveau INTEGER",
        "ALTER TABLE maturite_fiches ADD COLUMN verdict TEXT",
        "ALTER TABLE maturite_fiches ADD COLUMN alerte_sanitaire TEXT",
        "ALTER TABLE maturite_fiches ADD COLUMN ratio_sat REAL",
        "ALTER TABLE maturite_fiches ADD COLUMN score_degre INTEGER",
        "ALTER TABLE maturite_fiches ADD COLUMN score_ratio INTEGER",
        "ALTER TABLE maturite_fiches ADD COLUMN score_pepins INTEGER",
        "ALTER TABLE maturite_fiches ADD COLUMN score_pulpe INTEGER",
        "ALTER TABLE maturite_fiches ADD COLUMN score_tanins INTEGER",
        "ALTER TABLE parcelles ADD COLUMN commune TEXT",
        "ALTER TABLE prescriptions ADD COLUMN statut TEXT DEFAULT 'prévu'",
        "ALTER TABLE prescriptions ADD COLUMN motif_annulation TEXT",
        "ALTER TABLE parcelles ADD COLUMN rendement_ref_kgha REAL",
        "ALTER TABLE rendements ADD COLUMN rendement_reel_kgha REAL",
        "ALTER TABLE rendements ADD COLUMN kg_recoltes_total REAL DEFAULT 0",
        "ALTER TABLE rendements ADD COLUMN recolte_complete INTEGER DEFAULT 0",
        "ALTER TABLE parcelles ADD COLUMN ecart_rangs REAL",
        "ALTER TABLE parcelles ADD COLUMN ecart_ceps REAL",
        "ALTER TABLE parcelles ADD COLUMN surface_cadastrale REAL",
        "ALTER TABLE parcelles ADD COLUMN refs_cadastrales TEXT",
        "ALTER TABLE clients ADD COLUMN password_hash TEXT",
        "ALTER TABLE clients ADD COLUMN statut_compte TEXT DEFAULT 'essai'",
        "ALTER TABLE clients ADD COLUMN date_inscription TEXT",
        "ALTER TABLE clients ADD COLUMN origine TEXT DEFAULT 'admin'",
        "ALTER TABLE clients ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE clients ADD COLUMN stripe_subscription_id TEXT",
        "ALTER TABLE clients ADD COLUMN date_paiement TEXT",
        "ALTER TABLE clients ADD COLUMN is_demo INTEGER DEFAULT 0",
        "ALTER TABLE clients ADD COLUMN numero_inscription INTEGER",
        "ALTER TABLE clients ADD COLUMN reset_token TEXT",
        "ALTER TABLE clients ADD COLUMN reset_token_expire TEXT",
        "ALTER TABLE parcelles ADD COLUMN est_exemple INTEGER DEFAULT 0",
        "ALTER TABLE parcelles ADD COLUMN derogation_ouverture INTEGER DEFAULT 0",
        "ALTER TABLE maturite_fiches ADD COLUMN est_exemple INTEGER DEFAULT 0",
        "ALTER TABLE rendements ADD COLUMN est_exemple INTEGER DEFAULT 0",
    ]:
        try: conn.execute(alter)
        except: pass
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cuvees_parcellaires (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            id_client     TEXT    NOT NULL,
            saison        INTEGER NOT NULL DEFAULT 2026,
            nom           TEXT    NOT NULL,
            destination   TEXT,
            date_recolte  TEXT,
            parcelles_json TEXT   NOT NULL DEFAULT '[]',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reseau_matu (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            id_client   TEXT    NOT NULL,
            saison      INTEGER NOT NULL DEFAULT 2026,
            petite_region TEXT  NOT NULL,
            cepage      TEXT    NOT NULL,
            date_releve TEXT    NOT NULL,
            degre_probable REAL NOT NULL,
            dyn_degre   REAL    DEFAULT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(id_client, saison, petite_region, cepage, date_releve)
        )
    ''')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS dates_ouverture (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne       TEXT    NOT NULL DEFAULT '2026',
            commune        TEXT    NOT NULL,
            cepage         TEXT,
            date_ouverture TEXT    NOT NULL,
            source         TEXT    DEFAULT 'manuel',
            created_at     TEXT    DEFAULT (datetime('now')),
            UNIQUE(campagne, commune, cepage)
        )
    ''')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS itineraire_sauvegarde (
            id_client   TEXT NOT NULL,
            campagne    TEXT NOT NULL DEFAULT '2026',
            data_json   TEXT NOT NULL,
            updated_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (id_client, campagne)
        )
    ''')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS parametres_appellation (
            id_client                  TEXT NOT NULL,
            campagne                   TEXT NOT NULL DEFAULT '2026',
            rendement_appellation_kgha REAL,
            depassement_bloque_kgha    REAL,
            depassement_vo_kgha        REAL,
            updated_at                 TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (id_client, campagne)
        )
    ''')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS carnet_vendange (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            id_client    TEXT NOT NULL,
            campagne     TEXT NOT NULL DEFAULT '2026',
            date         TEXT NOT NULL,
            caisses      REAL,
            poids_total  REAL,
            poids_moyen  REAL,
            note         TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS carnet_vendange_parcelles (
            id_carnet    INTEGER NOT NULL,
            id_parcelle  INTEGER NOT NULL,
            PRIMARY KEY (id_carnet, id_parcelle)
        );
    ''')
    conn.commit()
    conn.close()

def _charger_donnees_exemple(id_client, conn):
    """Insère un jeu de données d'exemple réaliste (12 parcelles réparties sur 4 communes
    × 3 cépages, 2 prélèvements de maturité chacune, un rendement estimé) sur le compte
    donné, marquées est_exemple=1. Idempotent : les données d'exemple précédentes de ce
    compte sont d'abord effacées."""
    _vider_donnees_exemple(id_client, conn, commit=False)

    # 4 communes (avec un léger décalage de maturité pour simuler des micro-terroirs
    # différents) × 3 cépages = 12 parcelles, pour un itinéraire de récolte étalé et
    # réaliste plutôt qu'une poignée de parcelles toutes prêtes le même jour.
    communes_demo = [
        ("AY", 0.0), ("MAREUIL-SUR-AY", 0.2), ("DIZY", -0.15), ("TOURS-SUR-MARNE", 0.35),
    ]
    fiches_base = {
        "Chardonnay": [("2026-08-25", 9.2, 9.8), ("2026-09-01", 9.9, 8.9)],
        "Pinot Noir": [("2026-08-25", 9.5, 9.2), ("2026-09-01", 10.2, 8.4)],
        "Meunier":    [("2026-08-25", 9.0, 10.1), ("2026-09-01", 9.7, 9.3)],
    }
    noms_parcelle = {
        ("AY", "Chardonnay"): "Les Vignes Blanches", ("AY", "Pinot Noir"): "Clos du Moulin", ("AY", "Meunier"): "La Côte Rouge",
        ("MAREUIL-SUR-AY", "Chardonnay"): "Les Crayères", ("MAREUIL-SUR-AY", "Pinot Noir"): "Le Clos Notre-Dame", ("MAREUIL-SUR-AY", "Meunier"): "Les Bermonts",
        ("DIZY", "Chardonnay"): "Terres Blanches", ("DIZY", "Pinot Noir"): "Les Grouttes", ("DIZY", "Meunier"): "La Justice",
        ("TOURS-SUR-MARNE", "Chardonnay"): "Les Faucherets", ("TOURS-SUR-MARNE", "Pinot Noir"): "Le Grand Clos", ("TOURS-SUR-MARNE", "Meunier"): "Les Trois Sillons",
    }
    rendement_par_cepage = {"Chardonnay": (11, 145), "Pinot Noir": (12, 155), "Meunier": (13, 135)}
    surface_base = {"Chardonnay": 0.85, "Pinot Noir": 1.05, "Meunier": 0.70}
    couleurs_pepins = {"Chardonnay": "marron", "Pinot Noir": "marron", "Meunier": "brun clair"}

    i = 0
    nb_parcelles = 0
    for commune, offset in communes_demo:
        for cepage in ("Chardonnay", "Pinot Noir", "Meunier"):
            i += 1
            nb_pieds_ha = 8800 + (i % 5) * 100
            surface = round(surface_base[cepage] + (i % 3) * 0.08, 2)
            cur = conn.execute(
                "INSERT INTO parcelles (id_client, nom, cepage, commune, surface_cadastrale, nb_pieds_ha, est_exemple) VALUES (?,?,?,?,?,?,1)",
                (id_client, noms_parcelle[(commune, cepage)], cepage, commune, surface, nb_pieds_ha))
            pid = cur.lastrowid
            nb_parcelles += 1

            for date_fiche, degre, at in fiches_base[cepage]:
                d = {
                    'id_parcelle': pid, 'campagne': '2026', 'date_fiche': date_fiche,
                    'degre': round(degre + offset, 1), 'AT': round(at - offset * 0.4, 1),
                    'couleur_pepins': couleurs_pepins[cepage],
                    'saveur_pulpe': 'sucrée' if offset >= -0.15 else 'acidulée',
                    'tanins': 'doux' if offset >= -0.15 else 'verts',
                    'etat_sanitaire': 95 + (i % 4),
                    'observations': "Donnée d'exemple", 'saisie_par': 'exemple', 'est_exemple': 1,
                }
                _insert_fiche(conn, d, id_client)

            grappes, poids_g = rendement_par_cepage[cepage]
            rdt_kgha = round(grappes * poids_g * nb_pieds_ha / 1000)
            conn.execute("""INSERT INTO rendements
                (id_parcelle, id_client, campagne, date_releve, nb_grappes_pied, poids_moyen_g, nb_pieds_ha, rendement_kgha, observations, est_exemple)
                VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (pid, id_client, '2026', '2026-08-25', grappes, poids_g, nb_pieds_ha, rdt_kgha, "Donnée d'exemple"))

    return nb_parcelles

def _vider_donnees_exemple(id_client, conn, commit=True):
    """Supprime toutes les données d'exemple (est_exemple=1) d'un compte — utilisé pour
    recharger un jeu propre, pour le bouton 'Vider les données d'exemple', et
    automatiquement à la souscription (le compte payant démarre vierge)."""
    ids_parcelles = [r[0] for r in conn.execute(
        "SELECT id FROM parcelles WHERE id_client=? AND est_exemple=1", (id_client,)).fetchall()]
    if ids_parcelles:
        qmarks = ",".join("?" * len(ids_parcelles))
        conn.execute(f"DELETE FROM maturite_fiches WHERE id_parcelle IN ({qmarks})", ids_parcelles)
        conn.execute(f"DELETE FROM rendements WHERE id_parcelle IN ({qmarks})", ids_parcelles)
        conn.execute(f"DELETE FROM parcelles WHERE id IN ({qmarks})", ids_parcelles)
    # Filet de sécurité : toute ligne est_exemple=1 orpheline (ne devrait pas arriver)
    conn.execute("DELETE FROM maturite_fiches WHERE id_client=? AND est_exemple=1", (id_client,))
    conn.execute("DELETE FROM rendements WHERE id_client=? AND est_exemple=1", (id_client,))
    if commit:
        conn.commit()
    return len(ids_parcelles)

def dict_from_row(row):
    if row is None: return None
    return dict(row)

def dicts_from_rows(rows):
    return [dict(r) for r in rows]


# ===== IMPORT FROM EXCEL =====
def import_from_excel(filepath):
    """Import data from existing VITI Sens Excel base"""
    from openpyxl import load_workbook
    # data_only=True lit les valeurs mises en cache par Excel, pas les formules
    # Important pour les cellules avec VLOOKUP/INDEX dans l'onglet Prescriptions
    wb = load_workbook(filepath, data_only=True)
    conn = get_db()

    # Clients
    if "Clients" in wb.sheetnames:
        ws = wb["Clients"]
        for row in ws.iter_rows(min_row=3, max_col=20, values_only=True):
            if not row[0] or not row[1]: continue
            conn.execute("""INSERT OR REPLACE INTO clients
                (id,exploitation,interlocuteur,commune,secteur,surface,
                pct_chard,pct_pn,pct_meunier,pct_autre,certification,
                sencrop,id_station,parcelles_mildiou,parcelles_oidium,
                historique_gel,cu_cumule,email,telephone,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row[:20])

    # Catalogue
    if "Catalogue Phyto" in wb.sheetnames:
        ws = wb["Catalogue Phyto"]
        # Read header to map columns dynamically
        headers = []
        for row in ws.iter_rows(min_row=2, max_row=2, max_col=25, values_only=True):
            headers = [str(h).strip().lower() if h else "" for h in row]
        
        def col_idx(keywords):
            """Find column index matching any keyword"""
            for i, h in enumerate(headers):
                for kw in keywords:
                    if kw in h: return i
            return None
        
        # Map columns
        c_id = col_idx(["id produit","id"]) or 0
        c_nom = col_idx(["nom commercial","nom"]) or 1
        c_sa = col_idx(["substance","sa"]) or 2
        c_famille = col_idx(["famille"]) 
        c_frac = col_idx(["frac","irac"])
        c_mode = col_idx(["mode d'action","mode_action","mode action"]) or 4
        c_cible = col_idx(["cible"]) or 5
        c_type = col_idx(["type"]) or 6
        c_dose = col_idx(["dose homol","dose"]) or 7
        c_nbmax = col_idx(["nb max","nb_max","nombre max"])
        c_dar = col_idx(["dar"]) or 8
        c_dre = col_idx(["dre"])
        c_znt = col_idx(["znt"]) or 9
        c_dsppr = col_idx(["dsppr"])
        c_bio = col_idx(["bio","compatible bio"]) or 10
        c_hve = col_idx(["hve","compatible hve"]) or 11
        c_vdc = col_idx(["vdc","compatible vdc"]) or 12
        c_biocontrole = col_idx(["biocontr"])
        c_position = col_idx(["position","stratégie","strategie"]) or 13
        c_option = col_idx(["option"]) or 14
        c_prix = col_idx(["prix"]) or 15
        c_notes = col_idx(["notes","remarque"]) or 18
        
        def g(row, idx, default=None):
            if idx is None: return default
            return row[idx] if idx < len(row) else default
        
        for row in ws.iter_rows(min_row=3, max_col=25, values_only=True):
            if not row[c_id] or not row[c_nom]: continue
            conn.execute("""INSERT OR REPLACE INTO catalogue
                (id,nom,substance_active,famille,code_frac,mode_action,cible,type_cps,
                dose_homologuee,nb_max_appli,dar,dre,znt,dsppr,
                compatible_bio,compatible_hve,compatible_vdc,biocontrole,
                position_strategie,option_abc,prix,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (g(row,c_id), g(row,c_nom), g(row,c_sa), g(row,c_famille),
                 g(row,c_frac), g(row,c_mode), g(row,c_cible), g(row,c_type),
                 g(row,c_dose), g(row,c_nbmax),
                 g(row,c_dar), g(row,c_dre),
                 g(row,c_znt), g(row,c_dsppr),
                 1 if g(row,c_bio)=="Oui" else 0,
                 1 if g(row,c_hve)=="Oui" else 0,
                 g(row,c_vdc,"Oui"),
                 1 if g(row,c_biocontrole)=="Oui" else 0,
                 g(row,c_position), g(row,c_option), g(row,c_prix), g(row,c_notes)))

    # Prescriptions
    if "Prescriptions" in wb.sheetnames:
        ws = wb["Prescriptions"]
        for row in ws.iter_rows(min_row=4, max_col=16, values_only=True):
            if not row[0]: continue
            conn.execute("""INSERT INTO prescriptions
                (id_client,cible,passage,id_produit,nom_produit,substance_active,
                type_cps,dose_homologuee,dose_prescrite,volume_bouillie,
                date_prevue,observations,applique,date_reelle,dose_reelle)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row[0], row[2], row[3], row[4], row[5], row[6],
                 row[7], row[8], row[9], row[10], row[11], row[12],
                 row[13], row[14], row[15]))

    # Bulletin Hebdo
    if "Bulletin Hebdo" in wb.sheetnames:
        ws = wb["Bulletin Hebdo"]
        data = {}
        for row in ws.iter_rows(min_row=1, max_col=4, values_only=True):
            if row[0]: data[str(row[0]).strip()] = row
        def g(key):
            for k, v in data.items():
                if key.lower() in k.lower(): return v[1] if len(v)>1 else None
                if len(v)>2 and v[2] and key.lower() in str(v[2]).lower(): return v[3] if len(v)>3 else None
            return None
        conn.execute("""INSERT INTO bulletin_hebdo
            (numero_av,date_av,maturite,epi,risque_mildiou,reco_mildiou,
            risque_oidium,reco_oidium,gel,mange_bourgeons,
            stade_chard,comment_chard,stade_pn,comment_pn,stade_meunier,comment_meunier,
            avance,heterogeneite,titre_complement,contenu_complement)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (g("N° AV"), g("Date AV"), g("Maturité"), g("EPI"),
             g("Risque mildiou"), g("Recommandation AV mildiou"),
             g("Risque oïdium"), g("Recommandation AV oïdium"),
             g("Gel"), g("Mange-bourgeons"),
             g("Chardonnay"), g("Commentaire phéno Chard"),
             g("Pinot Noir"), g("Commentaire phéno PN"),
             g("Meunier"), g("Commentaire phéno Meunier"),
             g("Avance"), g("Hétérogénéité"),
             g("Titre section"), g("Contenu")))

    conn.commit()
    conn.close()
    return True


# ===== API ROUTES =====

# --- Clients ---
@app.route('/api/clients', methods=['GET'])
def get_clients():
    conn = get_db()
    rows = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
    conn.close()
    return jsonify(dicts_from_rows(rows))

@app.route('/api/clients', methods=['POST'])
def create_client():
    d = request.json
    conn = get_db()
    conn.execute("""INSERT INTO clients (id,exploitation,interlocuteur,commune,secteur,surface,
        pct_chard,pct_pn,pct_meunier,pct_autre,certification,sencrop,id_station,
        parcelles_mildiou,parcelles_oidium,historique_gel,cu_cumule,email,telephone,latitude,longitude,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('id'), d.get('exploitation'), d.get('interlocuteur'), d.get('commune'),
         d.get('secteur'), d.get('surface'), d.get('pct_chard',0), d.get('pct_pn',0),
         d.get('pct_meunier',0), d.get('pct_autre',0), d.get('certification','Conventionnel'),
         d.get('sencrop'), d.get('id_station'), d.get('parcelles_mildiou'),
         d.get('parcelles_oidium'), d.get('historique_gel'), d.get('cu_cumule',0),
         d.get('email'), d.get('telephone'), d.get('latitude'), d.get('longitude'), d.get('notes')))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/clients/<cid>', methods=['PUT'])
def update_client(cid):
    d = request.json
    conn = get_db()
    fields = ["exploitation","interlocuteur","commune","secteur","surface",
        "pct_chard","pct_pn","pct_meunier","pct_autre","certification",
        "sencrop","id_station","parcelles_mildiou","parcelles_oidium",
        "historique_gel","cu_cumule","email","telephone","latitude","longitude","notes"]
    sets = ", ".join([f"{f}=?" for f in fields if f in d])
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn.execute(f"UPDATE clients SET {sets} WHERE id=?", vals + [cid])
        conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/clients/<cid>', methods=['DELETE'])
def delete_client(cid):
    conn = get_db()
    conn.execute("DELETE FROM clients WHERE id=?", (cid,))
    conn.execute("DELETE FROM prescriptions WHERE id_client=?", (cid,))
    conn.execute("DELETE FROM suivi WHERE id_client=?", (cid,))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

# --- Catalogue ---
@app.route('/api/catalogue', methods=['GET'])
def get_catalogue():
    conn = get_db()
    cible = request.args.get('cible')
    certif = request.args.get('certification')
    categorie = request.args.get('categorie')
    q = "SELECT * FROM catalogue WHERE 1=1"
    params = []
    if categorie and categorie != 'all':
        q += " AND categorie=?"; params.append(categorie)
    if cible and cible != 'all':
        q += " AND (LOWER(cible) LIKE ? OR LOWER(objectif) LIKE ?)"; params.extend([f"%{cible.lower()}%", f"%{cible.lower()}%"])
    if certif == 'Bio':
        q += " AND compatible_bio=1"
    elif certif == 'HVE':
        q += " AND compatible_hve=1"
    q += " ORDER BY categorie, id"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(dicts_from_rows(rows))

@app.route('/api/catalogue', methods=['POST'])
def create_produit():
    d = request.json
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO catalogue
        (id,nom,categorie,substance_active,famille,code_frac,mode_action,cible,type_cps,
        dose_homologuee,nb_max_appli,dar,dre,znt,dsppr,
        compatible_bio,compatible_hve,compatible_vdc,biocontrole,
        position_strategie,option_abc,prix,concentration_ma,unite_concentration,
        type_mfsc,composition_npk,oligo_elements,matiere_organique,ph,
        stade_application,objectif,amm_mfsc,norme,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('id'), d.get('nom'), d.get('categorie','Phyto'),
         d.get('substance_active'), d.get('famille'),
         d.get('code_frac'), d.get('mode_action'), d.get('cible'), d.get('type_cps'),
         d.get('dose_homologuee'), d.get('nb_max_appli'), d.get('dar'), d.get('dre'),
         d.get('znt'), d.get('dsppr'),
         1 if d.get('compatible_bio') else 0, 1 if d.get('compatible_hve',True) else 0,
         d.get('compatible_vdc','Oui'), 1 if d.get('biocontrole') else 0,
         d.get('position_strategie'), d.get('option_abc'), d.get('prix'),
         d.get('concentration_ma',0), d.get('unite_concentration','g/kg'),
         d.get('type_mfsc'), d.get('composition_npk'), d.get('oligo_elements'),
         d.get('matiere_organique'), d.get('ph'),
         d.get('stade_application'), d.get('objectif'), d.get('amm_mfsc'), d.get('norme'),
         d.get('notes'))
    )
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/catalogue/<pid>', methods=['DELETE'])
def delete_produit(pid):
    conn = get_db()
    conn.execute("DELETE FROM catalogue WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/catalogue/familles', methods=['GET'])
def get_familles():
    """Retourne la liste des familles, molécules et partenaires pour le formulaire en cascade"""
    conn = get_db()
    cible = request.args.get('cible', '')
    certif = request.args.get('certification', '')
    q = "SELECT * FROM catalogue WHERE categorie='Phyto'"
    p = []
    if cible:
        q += " AND LOWER(cible) LIKE ?"; p.append(f"%{cible.lower()}%")
    if certif == 'Bio':
        q += " AND compatible_bio=1"
    elif certif == 'HVE':
        q += " AND compatible_hve=1"
    rows = conn.execute(q + " ORDER BY famille, substance_active, nom", p).fetchall()
    conn.close()
    prods = dicts_from_rows(rows)
    # Extraire les familles distinctes
    familles = sorted(set(pr.get("famille") or "Non classé" for pr in prods))
    # Extraire les molécules distinctes
    molecules = sorted(set(pr.get("substance_active") or "?" for pr in prods))
    # Extraire les partenaires distincts
    partenaires = sorted(set(pr.get("partenaire") or "" for pr in prods if pr.get("partenaire")))
    return jsonify({"familles": familles, "molecules": molecules, "partenaires": partenaires, "produits": prods})

# --- Prescriptions ---
@app.route('/api/prescriptions', methods=['GET'])
def get_prescriptions():
    cid = request.args.get('client')
    conn = get_db()
    if cid:
        rows = conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM prescriptions ORDER BY id_client, id").fetchall()
    conn.close()
    return jsonify(dicts_from_rows(rows))

@app.route('/api/prescriptions', methods=['POST'])
def create_prescription():
    d = request.json
    conn = get_db()
    conn.execute("""INSERT INTO prescriptions
        (id_client,cible,passage,id_produit,nom_produit,substance_active,type_cps,
        dose_homologuee,dose_prescrite,volume_bouillie,date_prevue,observations,applique)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('id_client'), d.get('cible'), d.get('passage'), d.get('id_produit'),
         d.get('nom_produit'), d.get('substance_active'), d.get('type_cps'),
         d.get('dose_homologuee'), d.get('dose_prescrite'), d.get('volume_bouillie'),
         d.get('date_prevue'), d.get('observations'), d.get('applique','Non')))
    conn.commit(); conn.close()
    return jsonify({"status": "ok", "id": conn.execute("SELECT last_insert_rowid()").fetchone()})

@app.route('/api/prescriptions/<int:pid>', methods=['PUT'])
def update_prescription(pid):
    d = request.json
    conn = get_db()
    fields = ["id_client","cible","passage","id_produit","nom_produit","substance_active",
        "type_cps","dose_homologuee","dose_prescrite","volume_bouillie",
        "date_prevue","observations","applique","date_reelle","dose_reelle"]
    sets = ", ".join([f"{f}=?" for f in fields if f in d])
    vals = [d[f] for f in fields if f in d]
    if sets:
        conn.execute(f"UPDATE prescriptions SET {sets} WHERE id=?", vals + [pid])
        conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/prescriptions/<int:pid>', methods=['DELETE'])
def delete_prescription(pid):
    conn = get_db()
    conn.execute("DELETE FROM prescriptions WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

# --- Bulletin Hebdo ---
@app.route('/api/bulletin-hebdo', methods=['GET'])
def get_bulletin_hebdo():
    conn = get_db()
    row = conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return jsonify(dict_from_row(row) if row else {})

@app.route('/api/bulletin-hebdo', methods=['POST'])
def save_bulletin_hebdo():
    d = request.json
    conn = get_db()
    conn.execute("""INSERT INTO bulletin_hebdo
        (numero_av,date_av,maturite,epi,risque_mildiou,reco_mildiou,
        risque_oidium,reco_oidium,gel,mange_bourgeons,
        stade_chard,comment_chard,stade_pn,comment_pn,stade_meunier,comment_meunier,
        avance,heterogeneite,titre_complement,contenu_complement,texte_phenologie,passage_en_cours,no_presc_motif)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('numero_av'), d.get('date_av'), d.get('maturite'), d.get('epi'),
         d.get('risque_mildiou'), d.get('reco_mildiou'),
         d.get('risque_oidium'), d.get('reco_oidium'),
         d.get('gel'), d.get('mange_bourgeons'),
         d.get('stade_chard'), d.get('comment_chard'),
         d.get('stade_pn'), d.get('comment_pn'),
         d.get('stade_meunier'), d.get('comment_meunier'),
         d.get('avance'), d.get('heterogeneite'),
         d.get('titre_complement'), d.get('contenu_complement'),
         d.get('texte_phenologie'), d.get('passage_en_cours'), d.get('no_presc_motif')))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

# --- Météo ---
@app.route('/api/meteo', methods=['GET'])
def get_meteo():
    if not req_lib:
        return jsonify({"error": "requests non installé"}), 500
    try:
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        commune = request.args.get('commune')
        # Géocodage si commune fournie sans coordonnées
        if not lat and commune:
            glat, glon = geocode_commune(commune)
            if glat: lat = glat; lon = glon
        lat = lat or COORDS['lat']
        lon = lon or COORDS['lon']
        days = fetch_meteo_cached(float(lat), float(lon))
        if days:
            return jsonify(days)
        return jsonify({"error": "Météo non disponible"}), 500
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,relative_humidity_2m_mean,dewpoint_2m_min,dewpoint_2m_max,et0_fao_evapotranspiration,wind_speed_10m_max,sunshine_duration,leaf_wetness_probability_mean,precipitation_probability_max&hourly=soil_moisture_0_to_1cm,soil_temperature_6cm&timezone=Europe/Paris&forecast_days=7"
        r = req_lib.get(url, timeout=10)
        data = r.json()
        days = []
        d = data["daily"]
        for i in range(len(d["time"])):
            tmoy = round((d["temperature_2m_max"][i] + d["temperature_2m_min"][i]) / 2, 1)
            pluie = d["precipitation_sum"][i]
            hr = d.get("relative_humidity_2m_mean", [None]*7)[i]
            tmin_val = d["temperature_2m_min"][i]
            dp_min = d.get("dewpoint_2m_min", [None]*7)[i] if "dewpoint_2m_min" in d else None
            dp_max = d.get("dewpoint_2m_max", [None]*7)[i] if "dewpoint_2m_max" in d else None
            dp = round((dp_min + dp_max) / 2, 1) if dp_min is not None and dp_max is not None else calc_dewpoint(tmoy, hr)
            # Nouveaux paramètres agricoles
            et0 = d.get("et0_fao_evapotranspiration", [None]*7)[i]
            wind = d.get("wind_speed_10m_max", [None]*7)[i]
            sunshine = d.get("sunshine_duration", [None]*7)[i]
            # Leaf wetness from API (probability 0-100)
            lw_api = d.get("leaf_wetness_probability_mean", [None]*7)[i]
            precip_prob = d.get("precipitation_probability_max", [None]*7)[i]
            # Soil moisture & temp from hourly data (averaged per day)
            soil_m = None; soil_t = None
            hourly = data.get("hourly", {})
            if "soil_moisture_0_to_1cm" in hourly and "time" in hourly:
                day_date = d["time"][i]
                sm_vals = [hourly["soil_moisture_0_to_1cm"][h] for h in range(len(hourly["time"])) if hourly["time"][h].startswith(day_date) and hourly["soil_moisture_0_to_1cm"][h] is not None]
                if sm_vals: soil_m = round(sum(sm_vals)/len(sm_vals), 3)
            if "soil_temperature_6cm" in hourly and "time" in hourly:
                st_vals = [hourly["soil_temperature_6cm"][h] for h in range(len(hourly["time"])) if hourly["time"][h].startswith(day_date) and hourly["soil_temperature_6cm"][h] is not None]
                if st_vals: soil_t = round(sum(st_vals)/len(st_vals), 1)
            # Leaf wetness estimé
            lw = int(lw_api) if lw_api is not None else calc_leaf_wetness(tmin_val, dp, hr, pluie)
            # Risque mildiou enrichi
            risk = calc_risk_mildiou_avance(pluie, tmoy, hr, soil_m, lw)
            risk_o = calc_risk_oidium(tmoy, tmin_val, hr, dp)
            # Sol interprété
            sol_txt = interpret_soil_moisture(soil_m)
            # Fenêtre traitement (vent < 19 km/h et pas de pluie)
            fenetre = "Oui" if (wind is not None and wind < 19 and pluie < 1) else "Non" if wind is not None else "—"
            days.append({"date": d["time"][i], "tmax": d["temperature_2m_max"][i],
                "tmin": tmin_val, "tmoy": tmoy, "pluie": pluie,
                "hr": hr, "dewpoint": dp, "et0": et0, "wind": wind,
                "sunshine": round(sunshine/3600, 1) if sunshine else None,
                "soil_moisture": soil_m, "soil_moisture_txt": sol_txt,
                "soil_temp": soil_t, "leaf_wetness": lw,
                "risk": risk, "risk_oidium": risk_o, "fenetre_traitement": fenetre})
        return jsonify(days)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Analyse épidémiologique ---
@app.route('/api/epidemio', methods=['GET'])
def get_epidemio():
    """Analyse épidémiologique mildiou + oïdium basée sur la météo 7 jours"""
    try:
        # Fetch meteo
        lat = request.args.get('lat', COORDS['lat'])
        lon = request.args.get('lon', COORDS['lon'])
        maturite = request.args.get('maturite', 'true').lower() == 'true'
        receptive = request.args.get('receptive', 'true').lower() == 'true'
        
        if not req_lib:
            return jsonify({"error": "requests non installé"}), 500
        
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,relative_humidity_2m_mean,dewpoint_2m_min,dewpoint_2m_max,wind_speed_10m_max,leaf_wetness_probability_mean&hourly=soil_moisture_0_to_1cm&timezone=Europe/Paris&forecast_days=7"
        r = req_lib.get(url, timeout=10)
        data = r.json(); d = data["daily"]
        
        meteo_7j = []
        for i in range(len(d["time"])):
            tmoy = round((d["temperature_2m_max"][i]+d["temperature_2m_min"][i])/2, 1)
            tmin = d["temperature_2m_min"][i]
            pluie = d["precipitation_sum"][i]
            hr = d.get("relative_humidity_2m_mean", [None]*7)[i]
            dp_min = d.get("dewpoint_2m_min", [None]*7)[i] if "dewpoint_2m_min" in d else None
            dp_max = d.get("dewpoint_2m_max", [None]*7)[i] if "dewpoint_2m_max" in d else None
            dp = round((dp_min+dp_max)/2,1) if dp_min and dp_max else calc_dewpoint(tmoy, hr)
            lw_api = d.get("leaf_wetness_probability_mean", [None]*7)[i]
            lw = int(lw_api) if lw_api is not None else calc_leaf_wetness(tmin, dp, hr, pluie)
            wind = d.get("wind_speed_10m_max", [None]*7)[i]
            sol_txt = ""
            hourly = data.get("hourly", {})
            if "soil_moisture_0_to_1cm" in hourly and "time" in hourly:
                sm_vals = [hourly["soil_moisture_0_to_1cm"][h] for h in range(len(hourly["time"])) if hourly["time"][h].startswith(d["time"][i]) and hourly["soil_moisture_0_to_1cm"][h] is not None]
                if sm_vals:
                    sm = round(sum(sm_vals)/len(sm_vals), 3)
                    sol_txt = interpret_soil_moisture(sm)
            meteo_7j.append({"date":d["time"][i],"tmoy":tmoy,"tmin":tmin,"pluie":pluie,"hr":hr,"dewpoint":dp,"leaf_wetness":lw,"wind":wind,"soil_moisture_txt":sol_txt})
        
        synthese = synthese_risque_7j(meteo_7j, maturite, receptive)
        return jsonify(synthese)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Conversion DOCX → PDF ---
def docx_to_pdf(docx_buf):
    """Convertit un buffer DOCX en buffer PDF."""
    import subprocess
    tmp_dir = tempfile.mkdtemp()
    docx_path = os.path.join(tmp_dir, "bulletin.docx")
    pdf_path = os.path.join(tmp_dir, "bulletin.pdf")
    
    try:
        docx_buf.seek(0)
        with open(docx_path, 'wb') as f:
            f.write(docx_buf.read())
        
        # Fermer Word s'il est ouvert
        try:
            subprocess.run(["taskkill", "/f", "/im", "WINWORD.EXE"], capture_output=True, timeout=5)
            import time; time.sleep(1)
        except: pass
        
        # Méthode 1 : COM direct (plus fiable que docx2pdf)
        try:
            import pythoncom
            pythoncom.CoInitialize()
            import win32com.client
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False
            doc = word.Documents.Open(os.path.abspath(docx_path))
            doc.SaveAs(os.path.abspath(pdf_path), FileFormat=17)  # 17 = wdFormatPDF
            doc.Close()
            word.Quit()
            pythoncom.CoUninitialize()
            if os.path.exists(pdf_path):
                print("   ✅ PDF converti via Word COM")
                with open(pdf_path, 'rb') as f:
                    return io.BytesIO(f.read())
        except ImportError:
            print("   ⚠️ pywin32 non installé")
        except Exception as e:
            print(f"   ⚠️ Word COM erreur: {e}")
            try: word.Quit()
            except: pass
            try: pythoncom.CoUninitialize()
            except: pass
        
        # Méthode 2 : docx2pdf
        try:
            import pythoncom
            pythoncom.CoInitialize()
            from docx2pdf import convert
            convert(docx_path, pdf_path)
            pythoncom.CoUninitialize()
            if os.path.exists(pdf_path):
                print("   ✅ PDF converti via docx2pdf")
                with open(pdf_path, 'rb') as f:
                    return io.BytesIO(f.read())
        except Exception as e:
            print(f"   ⚠️ docx2pdf erreur: {e}")
            try: pythoncom.CoUninitialize()
            except: pass
        
        # Méthode 3 : LibreOffice CLI
        for lo_path in [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "soffice", "libreoffice",
        ]:
            try:
                subprocess.run([lo_path, "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, docx_path],
                    capture_output=True, timeout=30)
                if os.path.exists(pdf_path):
                    print(f"   ✅ PDF converti via LibreOffice")
                    with open(pdf_path, 'rb') as f:
                        return io.BytesIO(f.read())
            except FileNotFoundError: continue
            except: continue
        
        print("   ❌ Aucun convertisseur PDF disponible.")
        return None
    finally:
        for f in [docx_path, pdf_path]:
            try: os.unlink(f)
            except: pass
        try: os.rmdir(tmp_dir)
        except: pass

# --- Génération bulletin Word ---
@app.route('/api/generer/<cid>', methods=['GET'])
def generer_bulletin(cid):
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
    if not client:
        conn.close(); return jsonify({"error": "Client non trouvé"}), 404

    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av_row = conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()
    av = dict_from_row(av_row) if av_row else {}
    suivi_row = conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone()
    suivi = dict_from_row(suivi_row) if suivi_row else None
    conn.close()

    meteo = fetch_meteo_for_client(client)

    doc = _build_bulletin(client, av, prescriptions, suivi, meteo)

    fmt = request.args.get('format', 'pdf')
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")

    if fmt == 'docx':
        fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    else:
        # Convertir en PDF
        pdf_buf = docx_to_pdf(buf)
        if pdf_buf:
            fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
            return send_file(pdf_buf, as_attachment=True, download_name=fname, mimetype="application/pdf")
        else:
            # Fallback : renvoyer le docx si la conversion PDF échoue
            buf.seek(0)
            fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx"
            return send_file(buf, as_attachment=True, download_name=fname,
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.route('/api/generer-tous', methods=['GET'])
def generer_tous():
    """Génère tous les bulletins dans un zip"""
    import zipfile
    conn = get_db()
    clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY id").fetchall())
    av_row = conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()
    av = dict_from_row(av_row) if av_row else {}

    meteo = fetch_meteo_cached()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for client in clients:
            prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (client['id'],)).fetchall())
            suivi_row = conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (client['id'],)).fetchone()
            suivi = dict_from_row(suivi_row) if suivi_row else None
            client_meteo = fetch_meteo_for_client(client)
            doc = _build_bulletin(client, av, prescriptions, suivi, client_meteo)
            doc_buf = io.BytesIO(); doc.save(doc_buf); doc_buf.seek(0)
            safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
            # Convertir en PDF
            pdf_buf = docx_to_pdf(doc_buf)
            if pdf_buf:
                    zf.writestr(f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.pdf", pdf_buf.getvalue())
            else:
                doc_buf.seek(0)
                zf.writestr(f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx", doc_buf.getvalue())

    conn.close()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"Bulletins_VITI_Sens_{datetime.now().strftime('%Y-%m-%d')}.zip",
                     mimetype="application/zip")

# --- Preview ---
@app.route('/api/preview/<cid>', methods=['GET'])
def preview_bulletin(cid):
    """Returns JSON preview of the bulletin content for display in dashboard"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
    if not client:
        conn.close(); return jsonify({"error": "Client non trouvé"}), 404
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    # Enrich prescriptions with DAR/DRE/ZNT from catalogue
    for p in prescriptions:
        if p.get("id_produit"):
            cat = conn.execute("SELECT dar,dre,znt FROM catalogue WHERE id=?", (p["id_produit"],)).fetchone()
            if cat:
                p["dar"] = cat[0]; p["dre"] = cat[1]; p["znt"] = cat[2]
    av_row = conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()
    av = dict_from_row(av_row) if av_row else {}
    suivi_row = conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone()
    suivi = dict_from_row(suivi_row) if suivi_row else None
    conn.close()

    certif = client.get("certification", "Conventionnel")
    sc = suivi.get("stade_chard") if suivi else None
    pluie = float(suivi.get("pluie",0) or 0) if suivi else 0
    temp = float(suivi.get("temp",0) or 0) if suivi else 0
    sol = suivi.get("etat_sol","") if suivi else ""

    # Cumul MA (Cu + Folpel) avec debug
    conn3 = get_db()
    cumul_cu = 0.0; cumul_folpel = 0.0; debug_cumuls = []
    for p in prescriptions:
        sa = (p.get("substance_active") or "").lower()
        nom = (p.get("nom_produit") or "").lower()
        dose_str = str(p.get("dose_prescrite") or p.get("dose_homologuee") or "0")
        nums = re.findall(r'(\d+[.,]?\d*)', dose_str)
        if not nums: continue
        dose_val = float(nums[0].replace(",", "."))
        conc = 0
        if p.get("id_produit"):
            cr = conn3.execute("SELECT concentration_ma FROM catalogue WHERE id=?", (p["id_produit"],)).fetchone()
            if cr and cr[0]: conc = float(cr[0])
        if conc == 0 and nom:
            cr = conn3.execute("SELECT concentration_ma FROM catalogue WHERE LOWER(nom)=LOWER(?)", (p.get("nom_produit",""))).fetchone()
            if cr and cr[0]: conc = float(cr[0])
        ma = dose_val * conc if conc > 0 else 0
        is_cu = "cuivre" in sa or "hydroxyde" in sa or "sulfate" in sa or "oxyde" in sa or "bouillie" in nom or "kocide" in nom or "nordox" in nom
        is_fol = "folpel" in sa or "folpel" in nom or "folpan" in nom
        if is_cu: cumul_cu += ma
        if is_fol: cumul_folpel += ma
        debug_cumuls.append({"produit": p.get("nom_produit","?"), "dose": dose_val, "conc": conc, "ma": ma, "cu": is_cu, "fol": is_fol})
    conn3.close()
    cu_deja = client.get('cu_cumule', 0) or 0

    return jsonify({
        "client": client, "av": av, "prescriptions": prescriptions, "suivi": suivi,
        "cumul_cu_ma": round(cumul_cu, 1), "cumul_cu_total": round(cu_deja + cumul_cu, 1),
        "cumul_cu_reste": round(4000 - cu_deja - cumul_cu, 1),
        "cumul_folpel_ma": round(cumul_folpel, 1),
        "debug_cumuls": debug_cumuls, "nb_prescriptions": len(prescriptions),
    })

# --- Import Excel ---
@app.route('/api/import-excel', methods=['POST'])
def import_excel():
    if 'file' not in request.files:
        return jsonify({"error": "Pas de fichier"}), 400
    f = request.files['file']
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    f.save(tmp.name)
    try:
        import_from_excel(tmp.name)
        return jsonify({"status": "ok", "message": "Import réussi"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp.name)

# --- Upload et extraction PDF (Avertissements Viticoles) ---
@app.route('/api/upload-pdf', methods=['POST'])
def upload_pdf():
    """Upload un PDF, extrait le texte, retourne les infos structurées."""
    if 'file' not in request.files:
        return jsonify({"error": "Pas de fichier"}), 400
    f = request.files['file']
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    tmp_path = tmp.name
    tmp.close()  # Fermer AVANT de sauvegarder (Windows file locking)
    f.save(tmp_path)
    
    try:
        texte = ""
        try:
            import fitz
            doc = fitz.open(tmp_path)
            for page in doc:
                texte += page.get_text() + "\n"
            doc.close()
        except ImportError:
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t: texte += t + "\n"
            except ImportError:
                return jsonify({"error": "Installez pymupdf ou pdfplumber"}), 500
        
        if not texte.strip():
            return jsonify({"error": "PDF vide ou impossible à lire (scan ?)"}), 400
        
        # Extraction automatique des informations clés
        result = _extraire_infos_bulletin(texte)
        result["texte_brut"] = texte[:5000]  # Limiter à 5000 car pour affichage
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

# --- Documents complémentaires (PDF) ---
COMPL_PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs_complementaires")

@app.route('/api/upload-compl-pdf', methods=['POST'])
def upload_compl_pdf():
    if 'file' not in request.files: return jsonify({"error": "Pas de fichier"}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'): return jsonify({"error": "Seuls les PDF sont acceptés"}), 400
    os.makedirs(COMPL_PDF_DIR, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9àâäéèêëïîôùûüçÀÂÄÉÈÊËÏÎÔÙÛÜÇ._\- ]', '', f.filename)
    f.save(os.path.join(COMPL_PDF_DIR, safe_name))
    return jsonify({"ok": True, "name": safe_name})

@app.route('/api/compl-pdfs', methods=['GET'])
def list_compl_pdfs():
    os.makedirs(COMPL_PDF_DIR, exist_ok=True)
    files = [{"name": f, "size": os.path.getsize(os.path.join(COMPL_PDF_DIR, f))} for f in sorted(os.listdir(COMPL_PDF_DIR)) if f.lower().endswith('.pdf')]
    return jsonify(files)

@app.route('/api/compl-pdfs/<name>', methods=['DELETE'])
def delete_compl_pdf(name):
    path = os.path.join(COMPL_PDF_DIR, name)
    if os.path.exists(path): os.unlink(path)
    return jsonify({"ok": True})

@app.route('/api/compl-pdfs', methods=['DELETE'])
def clear_compl_pdfs():
    os.makedirs(COMPL_PDF_DIR, exist_ok=True)
    for f in os.listdir(COMPL_PDF_DIR):
        try: os.unlink(os.path.join(COMPL_PDF_DIR, f))
        except: pass
    return jsonify({"ok": True})

def merge_pdfs(main_pdf_buf):
    """Fusionne le PDF principal avec les PDFs complémentaires"""
    if not os.path.exists(COMPL_PDF_DIR): return main_pdf_buf
    compl_files = sorted([f for f in os.listdir(COMPL_PDF_DIR) if f.lower().endswith('.pdf')])
    if not compl_files: return main_pdf_buf
    try:
        import fitz
        main_doc = fitz.open(stream=main_pdf_buf.getvalue(), filetype="pdf")
        for cf in compl_files:
            compl_doc = fitz.open(os.path.join(COMPL_PDF_DIR, cf))
            main_doc.insert_pdf(compl_doc)
            compl_doc.close()
        result = io.BytesIO(); main_doc.save(result); main_doc.close(); result.seek(0)
        print(f"   📎 {len(compl_files)} PDF complémentaire(s) fusionné(s)")
        return result
    except Exception as e:
        print(f"   ⚠️ Erreur fusion PDF: {e}")
        return main_pdf_buf

def _extraire_infos_bulletin(texte):
    """
    Parse le texte brut d'un bulletin viticole et extrait les infos structurées.
    Adapté au format des Avertissements Viticoles du Comité Champagne.
    """
    # Nettoyer le texte : joindre les lignes coupées
    lines = texte.split('\n')
    clean = []
    for line in lines:
        l = line.strip()
        if not l: 
            clean.append('')
        else:
            clean.append(l)
    txt = ' '.join(clean)
    # Supprimer les doubles espaces
    while '  ' in txt: txt = txt.replace('  ', ' ')
    txt_lower = txt.lower()
    
    result = {}
    
    # --- Numéro et date ---
    m = re.search(r'N[°o]\s*(\d{2,4})', txt)
    if m: result["numero_av"] = m.group(1)
    
    mois_fr = {"janvier":"01","février":"02","mars":"03","avril":"04","mai":"05","juin":"06",
               "juillet":"07","août":"08","septembre":"09","octobre":"10","novembre":"11","décembre":"12"}
    m = re.search(r'(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})', txt_lower)
    if m: result["date_av"] = f"{m.group(1).zfill(2)}/{mois_fr.get(m.group(2),'??')}/{m.group(3)}"
    
    # --- Découper par sections majuscules ---
    # Les sections du bulletin sont : PHÉNOLOGIE, MILDIOU, OÏDIUM, TORDEUSES, FLAVESCENCE DORÉE, etc.
    sections = {}
    section_names = ["PHÉNOLOGIE", "PHENOLOGIE", "PLANTATION", "MILDIOU", "OÏDIUM", "OIDIUM", 
                     "TORDEUSES", "FLAVESCENCE", "CONCOURS", "A RETENIR", "A retenir"]
    
    for sn in section_names:
        idx = txt.find(sn)
        if idx >= 0:
            # Trouver la fin de cette section (début de la section suivante)
            end = len(txt)
            for sn2 in section_names:
                if sn2 == sn: continue
                idx2 = txt.find(sn2, idx + len(sn) + 5)
                if 0 < idx2 < end: end = idx2
            # Aussi couper au "PROCHAIN BULLETIN"
            idx_pb = txt.find("PROCHAIN BULLETIN", idx + 10)
            if 0 < idx_pb < end: end = idx_pb
            sections[sn] = txt[idx+len(sn):end].strip()
    
    # --- PHÉNOLOGIE ---
    pheno = sections.get("PHÉNOLOGIE") or sections.get("PHENOLOGIE") or ""
    if pheno:
        # Construire un texte propre
        pheno_clean = pheno.strip()
        # Enlever les phrases institutionnelles du Comité
        for rm in ["Comité Champagne", "Comité Interprofessionnel", "extranet"]:
            idx_rm = pheno_clean.lower().find(rm.lower())
            if idx_rm >= 0:
                # Couper la phrase contenant cette mention
                dot_before = pheno_clean.rfind('.', 0, idx_rm)
                dot_after = pheno_clean.find('.', idx_rm)
                if dot_before >= 0 and dot_after >= 0:
                    pheno_clean = pheno_clean[:dot_before+1] + pheno_clean[dot_after+1:]
        
        result["texte_phenologie"] = pheno_clean[:600]
        
        # Extraire les stades par cépage
        for pattern, key in [
            (r'[Cc]hardonnay\s*:\s*([^.]+\.)', 'stade_chard'),
            (r'[Pp]inot\s+[Nn]oir\s*:\s*([^.]+\.)', 'stade_pn'),
            (r'[Mm]eunier\s*:\s*([^.]+\.)', 'stade_meunier'),
        ]:
            m = re.search(pattern, pheno)
            if m: result[key] = m.group(1).strip()
    
    # --- "A RETENIR" → extraire les puces ---
    retenir = sections.get("A RETENIR") or sections.get("A retenir") or ""
    
    # --- MILDIOU ---
    mildiou = sections.get("MILDIOU") or ""
    if mildiou:
        # Extraire la synthèse mildiou depuis "A retenir" si disponible
        m_ret = re.search(r'[Mm]ildiou\s*:\s*([^•\n]+)', retenir)
        synthese_mildiou = m_ret.group(1).strip() if m_ret else ""
        
        # EPI
        m_epi = re.search(r"(?:l')?EPI[^.]*(?:bas|faible|modéré|élevé|hausse|baisse)[^.]*\.", mildiou, re.IGNORECASE)
        if m_epi: result["epi"] = m_epi.group(0).strip()
        
        # Maturité
        m_mat = re.search(r'maturité[^.]*\.', mildiou, re.IGNORECASE)
        if m_mat: result["maturite"] = m_mat.group(0).strip()
        # Si pas trouvé dans MILDIOU, chercher dans le texte global
        if "maturite" not in result:
            m_mat = re.search(r'maturité[^.]*acquise[^.]*\.', txt, re.IGNORECASE)
            if m_mat: result["maturite"] = m_mat.group(0).strip()
        
        # Recommandations mildiou
        reco_idx = mildiou.lower().find("recommandation")
        if reco_idx >= 0:
            reco_txt = mildiou[reco_idx:]
            # Prendre jusqu'à la fin de la section
            result["reco_mildiou"] = reco_txt.strip()[:400]
        
        # Risque global
        risque_parts = []
        if synthese_mildiou:
            risque_parts.append(synthese_mildiou)
        # Extraire les phrases clés du corps
        for pattern in [
            r'[Ll]e risque mildiou[^.]*\.',
            r'[Rr]isque mildiou[^.]*\.',
            r'[Aa]ucun symptôme[^.]*\.',
            r'[Pp]remiers symptômes[^.]*\.',
        ]:
            m = re.search(pattern, mildiou)
            if m and m.group(0) not in ' '.join(risque_parts):
                risque_parts.append(m.group(0).strip())
        result["risque_mildiou"] = ' '.join(risque_parts)[:500] if risque_parts else mildiou[:300]
    
    # --- OÏDIUM ---
    oidium = sections.get("OÏDIUM") or sections.get("OIDIUM") or ""
    if oidium:
        # Synthèse depuis "A retenir"
        m_ret = re.search(r'[Oo]ïdium\s*:\s*([^•\n]+)', retenir)
        synthese_oidium = m_ret.group(1).strip() if m_ret else ""
        
        risque_parts = []
        if synthese_oidium:
            risque_parts.append(synthese_oidium)
        
        # Phrases clés
        for pattern in [
            r'[Pp]remiers symptômes[^.]*\.',
            r'[Ss]ymptômes sur feuilles[^.]*\.',
            r'[Rr]isque épidémique[^.]*\.',
            r'[Pp]résence d.ADN[^.]*\.',
            r'[Aa]ucune projection[^.]*\.',
        ]:
            m = re.search(pattern, oidium)
            if m and m.group(0) not in ' '.join(risque_parts):
                risque_parts.append(m.group(0).strip())
        
        # Recommandations oïdium
        reco_idx = oidium.lower().find("recommandation")
        if reco_idx >= 0:
            reco_txt = oidium[reco_idx:]
            result["reco_oidium"] = reco_txt.strip()[:400]
        
        result["risque_oidium"] = ' '.join(risque_parts)[:500] if risque_parts else oidium[:300]
    
    # --- GEL ---
    # Chercher dans phénologie ou dans le texte global
    gel_patterns = [
        r'[Gg]el[^.]*dégâts[^.]*\.',
        r'[Gg]el[^.]*épisode[^.]*\.',
        r'[Ss]ecteurs gélifs[^.]*\.',
        r'[Hh]étérogénéité[^.]*gel[^.]*\.',
        r'[Cc]onséquence des épisodes de gel[^.]*\.',
    ]
    gel_parts = []
    for pat in gel_patterns:
        m = re.search(pat, txt)
        if m: gel_parts.append(m.group(0).strip())
    if gel_parts:
        result["gel"] = ' '.join(gel_parts)[:300]
    
    # --- TORDEUSES ---
    tordeuses = sections.get("TORDEUSES") or ""
    if tordeuses:
        m_ret = re.search(r'[Tt]ordeuses\s*:\s*([^•\n]+)', retenir)
        if m_ret: result["tordeuses"] = m_ret.group(1).strip()
    
    # --- AVANCE PHÉNO ---
    m_av = re.search(r'(?:avance|retard)[^.]*(?:dizaine|quinzaine|\d+\s*jours)[^.]*\.', txt, re.IGNORECASE)
    if m_av: result["avance"] = m_av.group(0).strip()
    
    # Supprimer les mentions du Comité Champagne dans tous les champs
    for key in list(result.keys()):
        if isinstance(result[key], str):
            result[key] = re.sub(r'[^.]*Comité\s+Champagne[^.]*\.?\s*', '', result[key]).strip()
            result[key] = re.sub(r'[^.]*extranet[^.]*\.?\s*', '', result[key]).strip()
    
    return result

# --- Static files ---
@app.route('/api/email-config', methods=['GET'])
def email_config_get():
    """Retourne la config email actuelle (sans le mot de passe)"""
    return jsonify({
        "imap_server": app.config.get("IMAP_SERVER", "ssl0.ovh.net"),
        "imap_port": app.config.get("IMAP_PORT", 993),
        "email_from": app.config.get("EMAIL_FROM", "florent.miguel@sasu-viti-sens.fr"),
        "configured": bool(app.config.get("EMAIL_PASSWORD")),
    })

@app.route('/api/email-config', methods=['POST'])
def email_config_set():
    """Configure les paramètres email"""
    j = request.json
    app.config["IMAP_SERVER"] = j.get("imap_server", "ssl0.ovh.net")
    app.config["IMAP_PORT"] = int(j.get("imap_port", 993))
    app.config["EMAIL_FROM"] = j.get("email_from", "florent.miguel@sasu-viti-sens.fr")
    app.config["EMAIL_PASSWORD"] = j.get("password", "")
    # Tester la connexion
    try:
        import imaplib
        imap = imaplib.IMAP4_SSL(app.config["IMAP_SERVER"], app.config["IMAP_PORT"])
        imap.login(app.config["EMAIL_FROM"], app.config["EMAIL_PASSWORD"])
        imap.logout()
        return jsonify({"ok": True, "message": "Connexion IMAP réussie"})
    except Exception as e:
        app.config["EMAIL_PASSWORD"] = ""
        return jsonify({"error": f"Échec connexion : {str(e)}"}), 400

@app.route('/api/email-draft/<cid>', methods=['GET'])
def email_draft(cid):
    """Crée un brouillon email via IMAP avec PDF en pièce jointe"""
    if not app.config.get("EMAIL_PASSWORD"):
        return jsonify({"error": "Email non configuré. Allez dans l'onglet Générer et configurez vos identifiants."}), 400
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Client non trouvé"}), 404
    if not client.get("email"): conn.close(); return jsonify({"error": "Pas d'email pour ce client"}), 400
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    suivi = dict_from_row(conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone())
    conn.close()

    # Générer le bulletin
    meteo = fetch_meteo_for_client(client)
    doc = _build_bulletin(client, av, prescriptions, suivi, meteo)
    doc_buf = io.BytesIO(); doc.save(doc_buf); doc_buf.seek(0)
    pdf_buf = docx_to_pdf(doc_buf)
    safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
    if pdf_buf:
        fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
        attach_data = pdf_buf.getvalue()
        attach_mime = "application/pdf"
    else:
        fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx"
        doc_buf.seek(0); attach_data = doc_buf.read()
        attach_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # Construire l'email
    try:
        result = _create_imap_draft(client, fname, attach_data, attach_mime)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/email-draft-tous', methods=['GET'])
def email_draft_tous():
    """Crée les brouillons pour les clients sélectionnés — conversion PDF en batch"""
    if not app.config.get("EMAIL_PASSWORD"):
        return jsonify({"error": "Email non configuré"}), 400
    conn = get_db()
    # Filtrer les clients sélectionnés
    client_ids = request.args.get('clients', '')
    if client_ids:
        id_list = [cid.strip() for cid in client_ids.split(',') if cid.strip()]
        clients = [dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()) for cid in id_list]
        clients = [c for c in clients if c]
    else:
        clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY id").fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}

    # Phase 1 : Générer tous les docx dans un dossier temporaire
    import time
    tmp_dir = tempfile.mkdtemp()
    client_files = []  # liste de (client, docx_path, safe_name)

    for client in clients:
        if not client.get("email"):
            client_files.append((client, None, None, "skip"))
            continue
        prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (client['id'],)).fetchall())
        suivi = dict_from_row(conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (client['id'],)).fetchone())
        client_meteo = fetch_meteo_for_client(client)
        doc = _build_bulletin(client, av, prescriptions, suivi, client_meteo)
        safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
        docx_path = os.path.join(tmp_dir, f"Bulletin_{safe}.docx")
        doc.save(docx_path)
        client_files.append((client, docx_path, safe, "pending"))

    # Phase 2 : Fermer Word puis convertir en batch
    import subprocess
    try:
        subprocess.run(["taskkill", "/f", "/im", "WINWORD.EXE"], capture_output=True, timeout=5)
        time.sleep(1)
    except: pass
    try:
        import pythoncom
        pythoncom.CoInitialize()
        from docx2pdf import convert
        convert(tmp_dir, tmp_dir)
        print(f"   ✅ Batch PDF : {len([f for f in client_files if f[3]!='skip'])} fichiers")
        time.sleep(2)
        pythoncom.CoUninitialize()
    except ImportError:
        print("   ⚠️ docx2pdf non installé — envoi en docx")
    except Exception as e:
        print(f"   ⚠️ Batch PDF erreur : {e}")
        try: pythoncom.CoUninitialize()
        except: pass
        # Fallback : conversion individuelle (docx_to_pdf gère CoInitialize)
        for client, docx_path, safe, status in client_files:
            if docx_path and status != "skip":
                pdf_buf = docx_to_pdf(io.BytesIO(open(docx_path, 'rb').read()))
                if pdf_buf:
                    pdf_path = docx_path.replace('.docx', '.pdf')
                    with open(pdf_path, 'wb') as f: f.write(pdf_buf.getvalue())

    # Phase 3 : Créer les brouillons IMAP
    results = []
    for client, docx_path, safe, status in client_files:
        if status == "skip":
            results.append({"client": client["exploitation"], "status": "skip", "message": "Pas d'email"})
            continue
        pdf_path = docx_path.replace('.docx', '.pdf') if docx_path else None
        if pdf_path and os.path.exists(pdf_path):
            fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
            with open(pdf_path, 'rb') as f: pdf_data = io.BytesIO(f.read())
            attach_data = pdf_data.getvalue()
            attach_mime = "application/pdf"
            # Archiver dans le portail
            _save_bulletin_to_portail(client, io.BytesIO(attach_data), fname)
        elif docx_path and os.path.exists(docx_path):
            fname = f"Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx"
            with open(docx_path, 'rb') as f: attach_data = f.read()
            attach_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            # Archiver en docx si pas de PDF
            _save_bulletin_to_portail(client, io.BytesIO(attach_data), fname)
        else:
            results.append({"client": client["exploitation"], "status": "error", "message": "Fichier non généré"})
            continue
        try:
            result = _create_imap_draft(client, fname, attach_data, attach_mime)
            results.append({"client": client["exploitation"], "status": "ok", "message": result.get("message","OK")})
        except Exception as e:
            results.append({"client": client["exploitation"], "status": "error", "message": str(e)})

    # Nettoyage
    for f in os.listdir(tmp_dir):
        try: os.unlink(os.path.join(tmp_dir, f))
        except: pass
    try: os.rmdir(tmp_dir)
    except: pass

    conn.close()
    return jsonify({"results": results})

def _create_imap_draft(client, fname, attach_data, attach_mime):
    """Crée un brouillon via IMAP avec pièce jointe"""
    import imaplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    import time

    email_from = app.config["EMAIL_FROM"]
    email_to = client["email"].replace(";", ", ")
    date_str = datetime.now().strftime("%d/%m/%Y")
    prenom = (client.get("interlocuteur") or "").split(" ")[-1] if client.get("interlocuteur") else ""

    msg = MIMEMultipart()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = f"VITI Sens - Bulletin de conseil technique du {date_str}"

    body = f"""Bonjour {prenom},

Veuillez trouver ci-joint votre bulletin de conseil technique individuel pour la semaine du {date_str}.

Ce bulletin contient :
- La situation phénologique sur vos parcelles
- L'analyse du risque mildiou et oïdium
- Les prévisions météo à 7 jours
- Votre programme phytosanitaire personnalisé

N'hésitez pas à me contacter si vous avez des questions ou si vous souhaitez ajuster le programme.

Pensez à me signaler les traitements réalisés pour le suivi des cumuls.

Cordialement,

Florent Miguel
VITI Sens - Conseil viticole
florent.miguel@sasu-viti-sens.fr"""

    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Pièce jointe principale (bulletin)
    if fname.endswith('.pdf'):
        part = MIMEBase("application", "pdf")
    else:
        part = MIMEBase("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
    part.set_payload(attach_data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=fname)
    msg.attach(part)

    # Pièces jointes complémentaires
    if os.path.exists(COMPL_PDF_DIR):
        for cf in sorted(os.listdir(COMPL_PDF_DIR)):
            if cf.lower().endswith('.pdf'):
                cf_path = os.path.join(COMPL_PDF_DIR, cf)
                with open(cf_path, 'rb') as f:
                    part2 = MIMEBase("application", "pdf")
                    part2.set_payload(f.read())
                    encoders.encode_base64(part2)
                    part2.add_header("Content-Disposition", "attachment", filename=cf)
                    msg.attach(part2)

    # Déposer dans les brouillons via IMAP
    imap = imaplib.IMAP4_SSL(app.config["IMAP_SERVER"], app.config["IMAP_PORT"])
    imap.login(app.config["EMAIL_FROM"], app.config["EMAIL_PASSWORD"])
    # Chercher le dossier Brouillons (peut s'appeler Drafts, Brouillons, INBOX.Drafts...)
    draft_folder = None
    for folder_name in ["Drafts", "INBOX.Drafts", "Brouillons", "INBOX.Brouillons", "&BBoEPgRABDcEOAQ5BDQEOQQ6-"]:
        status, _ = imap.select(folder_name)
        if status == "OK":
            draft_folder = folder_name
            break
    if not draft_folder:
        # Lister les dossiers pour trouver le bon
        _, folders = imap.list()
        for f in (folders or []):
            f_decoded = f.decode() if isinstance(f, bytes) else f
            if "draft" in f_decoded.lower() or "brouillon" in f_decoded.lower():
                parts = f_decoded.split('"')
                draft_folder = parts[-2] if len(parts) >= 2 else parts[-1].strip()
                break
    if not draft_folder:
        draft_folder = "Drafts"  # Fallback

    imap.select(draft_folder)
    imap.append(draft_folder, "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    imap.logout()

    return {"ok": True, "message": f"Brouillon créé → {email_to} (avec {fname})"}

def parse_meteo_response(data):
    """Parse la réponse Open-Meteo en données viticoles enrichies"""
    d = data.get("daily", {})
    hourly = data.get("hourly", {})
    days = []
    for i in range(len(d.get("time", []))):
        tmax = d["temperature_2m_max"][i]; tmin = d["temperature_2m_min"][i]
        tmoy = round((tmax + tmin) / 2, 1)
        pluie = d["precipitation_sum"][i]
        hr = d.get("relative_humidity_2m_mean", [None]*7)[i]
        dp_min = d.get("dewpoint_2m_min", [None]*7)[i]
        dp_max = d.get("dewpoint_2m_max", [None]*7)[i]
        dp = round((dp_min+dp_max)/2, 1) if dp_min is not None and dp_max is not None else calc_dewpoint(tmoy, hr)
        et0 = d.get("et0_fao_evapotranspiration", [None]*7)[i]
        wind = d.get("wind_speed_10m_max", [None]*7)[i]
        sunshine = d.get("sunshine_duration", [None]*7)[i]
        lw_api = d.get("leaf_wetness_probability_mean", [None]*7)[i]
        precip_prob = d.get("precipitation_probability_max", [None]*7)[i]
        soil_m = None; soil_t = None
        day_date = d["time"][i]
        if "soil_moisture_0_to_1cm" in hourly and "time" in hourly:
            sm_vals = [hourly["soil_moisture_0_to_1cm"][h] for h in range(len(hourly["time"])) if hourly["time"][h].startswith(day_date) and hourly["soil_moisture_0_to_1cm"][h] is not None]
            if sm_vals: soil_m = round(sum(sm_vals)/len(sm_vals), 3)
        if "soil_temperature_6cm" in hourly and "time" in hourly:
            st_vals = [hourly["soil_temperature_6cm"][h] for h in range(len(hourly["time"])) if hourly["time"][h].startswith(day_date) and hourly["soil_temperature_6cm"][h] is not None]
            if st_vals: soil_t = round(sum(st_vals)/len(st_vals), 1)
        lw = int(lw_api) if lw_api is not None else calc_leaf_wetness(tmin, dp, hr, pluie)
        risk = calc_risk_mildiou_avance(pluie, tmoy, hr, soil_m, lw)
        risk_o = calc_risk_oidium(tmoy, tmin, hr, dp)
        sol_txt = interpret_soil_moisture(soil_m)
        fenetre = "Oui" if (wind is not None and wind < 19 and pluie < 1) else "Non" if wind is not None else "—"
        days.append({
            "date": day_date, "tmin": tmin, "tmax": tmax, "tmoy": tmoy,
            "pluie": pluie, "hr": hr, "dewpoint": dp, "et0": et0,
            "wind": wind, "sunshine": round(sunshine/3600, 1) if sunshine else None,
            "soil_moisture": soil_m, "soil_moisture_txt": sol_txt,
            "soil_temp": soil_t, "leaf_wetness": lw,
            "risk": risk, "risk_oidium": risk_o, "fenetre_traitement": fenetre,
        })
    return days

# Cache météo
_geocode_cache = {}
def geocode_commune(commune):
    """Convertit un nom de commune en coordonnées GPS via Open-Meteo Geocoding API"""
    if not commune: return None, None
    commune = commune.strip()
    if commune in _geocode_cache:
        return _geocode_cache[commune]
    if req_lib:
        try:
            r = req_lib.get(f"https://geocoding-api.open-meteo.com/v1/search?name={commune}&count=1&language=fr&format=json", timeout=5)
            data = r.json()
            if data.get("results"):
                lat = data["results"][0]["latitude"]
                lon = data["results"][0]["longitude"]
                _geocode_cache[commune] = (lat, lon)
                print(f"   📍 {commune} → {lat}, {lon}")
                return lat, lon
        except:
            pass
    _geocode_cache[commune] = (None, None)
    return None, None

_meteo_cache = {}
def fetch_meteo_cached(lat=None, lon=None):
    """Fetch météo avec cache par coordonnées (5 min)"""
    lat = lat or COORDS["lat"]
    lon = lon or COORDS["lon"]
    cache_key = f"{lat:.2f},{lon:.2f}"
    now = datetime.now()
    if cache_key in _meteo_cache:
        cached = _meteo_cache[cache_key]
        if cached["data"] and cached["timestamp"] and (now - cached["timestamp"]).seconds < 300:
            return cached["data"]
    meteo = None
    if req_lib:
        try:
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily={METEO_DAILY}&hourly={METEO_HOURLY}&timezone=Europe/Paris&forecast_days=7"
            r = req_lib.get(url, timeout=15)
            data = r.json()
            if "daily" in data:
                meteo = parse_meteo_response(data)
        except Exception as e:
            print(f"   ❌ Erreur météo: {e}")
    _meteo_cache[cache_key] = {"data": meteo, "timestamp": now}
    return meteo

def fetch_meteo_for_client(client):
    """Récupère la météo localisée : GPS client > géocodage commune > défaut Reims"""
    # 1. Coordonnées GPS du client
    if client.get("latitude") and client.get("longitude"):
        return fetch_meteo_cached(float(client["latitude"]), float(client["longitude"]))
    # 2. Géocodage de la commune
    commune = client.get("commune", "")
    if commune:
        lat, lon = geocode_commune(commune)
        if lat and lon:
            return fetch_meteo_cached(lat, lon)
    # 3. Coordonnées par défaut
    return fetch_meteo_cached()

@app.route('/api/sms-config', methods=['GET'])
def sms_config_get():
    return jsonify({
        "configured": bool(app.config.get("SMS_AK")),
        "service": app.config.get("SMS_SERVICE", ""),
        "sender": app.config.get("SMS_SENDER", "VITISENS"),
    })

@app.route('/api/sms-config', methods=['POST'])
def sms_config_post():
    j = request.json
    app.config["SMS_AK"] = j.get("ak", "")
    app.config["SMS_AS"] = j.get("as_", "")
    app.config["SMS_CK"] = j.get("ck", "")
    app.config["SMS_SERVICE"] = j.get("service", "")
    app.config["SMS_SENDER"] = j.get("sender", "VITISENS")
    # Tester en récupérant le crédit restant
    try:
        credits = _ovh_sms_get(f"/sms/{app.config['SMS_SERVICE']}")
        return jsonify({"ok": True, "credits": credits.get("creditsLeft", "?"), "message": f"Connexion OK — {credits.get('creditsLeft','?')} crédits restants"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/sms-send', methods=['POST'])
def sms_send():
    """Envoie un SMS à un ou plusieurs clients"""
    j = request.json
    message = j.get("message", "")
    client_ids = j.get("clients", [])
    if not message: return jsonify({"error": "Message vide"}), 400
    if not app.config.get("SMS_AK"): return jsonify({"error": "SMS non configuré"}), 400

    conn = get_db()
    results = []
    for cid in client_ids:
        client = dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
        if not client: results.append({"client": cid, "status": "error", "message": "Client non trouvé"}); continue
        tel = (client.get("telephone") or "").strip().replace(" ", "").replace(".", "")
        if not tel: results.append({"client": client["exploitation"], "status": "skip", "message": "Pas de téléphone"}); continue
        # Formater le numéro en +33
        if tel.startswith("0"): tel = "+33" + tel[1:]
        elif not tel.startswith("+"): tel = "+33" + tel
        # Personnaliser le message
        prenom = (client.get("interlocuteur") or "").split(" ")[-1] if client.get("interlocuteur") else ""
        msg = message.replace("{prenom}", prenom).replace("{exploitation}", client.get("exploitation", ""))
        try:
            resp = _ovh_sms_send(tel, msg)
            results.append({"client": client["exploitation"], "status": "ok", "message": f"SMS envoyé à {tel}"})
        except Exception as e:
            results.append({"client": client["exploitation"], "status": "error", "message": str(e)})
    conn.close()
    return jsonify({"results": results})

@app.route('/api/sms-bulletin', methods=['POST'])
def sms_bulletin_notify():
    """Envoie un SMS de notification aux clients sélectionnés"""
    if not app.config.get("SMS_AK"): return jsonify({"error": "SMS non configuré"}), 400
    j = request.json or {}
    client_ids = j.get("clients", [])
    conn = get_db()
    if client_ids:
        clients = [dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()) for cid in client_ids]
        clients = [c for c in clients if c]
    else:
        clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY id").fetchall())
    conn.close()
    date_str = datetime.now().strftime("%d/%m/%Y")
    results = []
    for client in clients:
        tel = (client.get("telephone") or "").strip().replace(" ", "").replace(".", "")
        if not tel: results.append({"client": client["exploitation"], "status": "skip", "message": "Pas de téléphone"}); continue
        if tel.startswith("0"): tel = "+33" + tel[1:]
        elif not tel.startswith("+"): tel = "+33" + tel
        prenom = (client.get("interlocuteur") or "").split(" ")[-1] if client.get("interlocuteur") else ""
        msg = f"Bonjour {prenom}, votre bulletin de conseil technique VITI Sens du {date_str} est disponible dans votre boite mail. Bonne lecture ! Florent Miguel"
        try:
            _ovh_sms_send(tel, msg)
            results.append({"client": client["exploitation"], "status": "ok", "message": f"→ {tel}"})
        except Exception as e:
            results.append({"client": client["exploitation"], "status": "error", "message": str(e)})
    return jsonify({"results": results})

# --- OVH SMS helpers ---
def _ovh_sms_sign(method, url, body, timestamp):
    import hashlib
    ak = app.config["SMS_AK"]; as_ = app.config["SMS_AS"]; ck = app.config["SMS_CK"]
    to_sign = f"{as_}+{ck}+{method}+https://eu.api.ovh.com/1.0{url}+{body}+{timestamp}"
    return "$1$" + hashlib.sha1(to_sign.encode('utf-8')).hexdigest()

def _ovh_sms_get(path):
    import time
    ts = str(int(time.time()))
    sig = _ovh_sms_sign("GET", path, "", ts)
    headers = {
        "X-Ovh-Application": app.config["SMS_AK"],
        "X-Ovh-Consumer": app.config["SMS_CK"],
        "X-Ovh-Timestamp": ts,
        "X-Ovh-Signature": sig,
    }
    r = req_lib.get(f"https://eu.api.ovh.com/1.0{path}", headers=headers, timeout=10)
    if r.status_code != 200: raise Exception(f"OVH API {r.status_code}: {r.text[:200]}")
    return r.json()

def _ovh_sms_send(to_number, message):
    import time
    service = app.config["SMS_SERVICE"]
    path = f"/sms/{service}/jobs"
    body = json.dumps({
        "charset": "UTF-8",
        "receivers": [to_number],
        "message": message,
        "noStopClause": True,
        "priority": "high",
        "sender": app.config.get("SMS_SENDER", "VITISENS"),
        "senderForResponse": False,
    })
    ts = str(int(time.time()))
    sig = _ovh_sms_sign("POST", path, body, ts)
    headers = {
        "X-Ovh-Application": app.config["SMS_AK"],
        "X-Ovh-Consumer": app.config["SMS_CK"],
        "X-Ovh-Timestamp": ts,
        "X-Ovh-Signature": sig,
        "Content-Type": "application/json",
    }
    r = req_lib.post(f"https://eu.api.ovh.com/1.0{path}", headers=headers, data=body, timeout=10)
    if r.status_code not in (200, 201): raise Exception(f"OVH SMS {r.status_code}: {r.text[:200]}")
    return r.json()

@app.route('/api/tracabilite/<cid>', methods=['GET'])
def tracabilite_client(cid):
    """Génère un tableau de traçabilité des traitements pour un client"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Client non trouvé"}), 404
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    conn.close()

    doc = Document()
    for section in doc.sections:
        section.top_margin = Cm(1.5); section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.5); section.right_margin = Cm(1.5)
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = Cm(29.7); section.page_height = Cm(21.0)

    # Titre
    t = doc.add_table(rows=1, cols=1); c = t.cell(0, 0); _shade(c, GD2)
    p1 = c.paragraphs[0]; p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = p1.add_run("TABLEAU DE TRAÇABILITÉ DES TRAITEMENTS PHYTOSANITAIRES")
    r1.bold = True; r1.font.size = Pt(14); r1.font.color.rgb = RGBColor.from_string("FFFFFF")

    doc.add_paragraph()
    _mp(doc, [{"t": "Exploitation : ", "b": True}, client["exploitation"],
              {"t": "   Commune : ", "b": True}, client.get("commune", ""),
              {"t": "   Certification : ", "b": True}, client.get("certification", ""),
              {"t": "   Surface : ", "b": True}, f"{client.get('surface', '')} ha"])
    _sp(doc, f"Campagne {datetime.now().year} — Édité le {datetime.now().strftime('%d/%m/%Y')}", size=9, color=GR2, italic=True)
    doc.add_paragraph()

    if not prescriptions:
        _sp(doc, "Aucune prescription enregistrée pour cette campagne.", size=11, color=GR2)
    else:
        # Tableau principal
        headers = ["Passage", "Cible", "Produit", "Substance active", "Type", "Dose homol.", "Dose prescrite", "Vol.", "Date prévue", "Date réelle", "Appliqué", "Observations"]
        t = doc.add_table(rows=1 + len(prescriptions), cols=len(headers))
        t.style = 'Table Grid'
        _hr(t, 0, headers, GD2)
        for i, p in enumerate(prescriptions):
            vals = [p.get("passage",""), p.get("cible",""), p.get("nom_produit",""),
                    p.get("substance_active",""), p.get("type_cps",""),
                    p.get("dose_homologuee",""), p.get("dose_prescrite",""),
                    str(p.get("volume_bouillie",""))+" L", p.get("date_prevue",""),
                    p.get("date_reelle",""), p.get("applique","Non"), p.get("observations","")]
            for j, v in enumerate(vals):
                _bc(t.cell(i+1, j), str(v or ""), size=8)

        doc.add_paragraph()

        # Cumuls
        conn2 = get_db()
        cumul_cu = 0.0; cumul_fol = 0.0
        for p in prescriptions:
            sa = (p.get("substance_active") or "").lower()
            nom = (p.get("nom_produit") or "").lower()
            dose_str = str(p.get("dose_prescrite") or "0")
            nums = re.findall(r'(\d+[.,]?\d*)', dose_str)
            if not nums: continue
            dose_val = float(nums[0].replace(",", "."))
            conc = 0
            if p.get("id_produit"):
                cr = conn2.execute("SELECT concentration_ma FROM catalogue WHERE id=?", (p["id_produit"],)).fetchone()
                if cr and cr[0]: conc = float(cr[0])
            if conc == 0 and p.get("nom_produit"):
                cr = conn2.execute("SELECT concentration_ma FROM catalogue WHERE LOWER(nom)=LOWER(?)", (p["nom_produit"],)).fetchone()
                if cr and cr[0]: conc = float(cr[0])
            ma = dose_val * conc if conc > 0 else 0
            if "cuivre" in sa or "hydroxyde" in sa or "sulfate" in sa or "bouillie" in nom: cumul_cu += ma
            if "folpel" in sa or "folpel" in nom: cumul_fol += ma
        conn2.close()
        cu_deja = client.get('cu_cumule', 0) or 0
        _mp(doc, [{"t": "Nombre de traitements : ", "b": True}, str(len(prescriptions)),
                   {"t": "   Cu métal cumulé : ", "b": True}, f"{cu_deja + cumul_cu:.0f} g/ha",
                   {"t": "   Folpel cumulé : ", "b": True}, f"{cumul_fol:.0f} g MA/ha"])

    # Signature
    doc.add_paragraph()
    doc.add_paragraph()
    _sp(doc, "Florent Miguel — VITI Sens — Conseil viticole", bold=True, size=10, color=GD2, align=WD_ALIGN_PARAGRAPH.LEFT)

    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")

    fmt = request.args.get('format', 'pdf')
    if fmt == 'pdf':
        pdf_buf = docx_to_pdf(buf)
        if pdf_buf:
            return send_file(pdf_buf, as_attachment=True,
                download_name=f"Tracabilite_{safe}_{datetime.now().year}.pdf", mimetype="application/pdf")
    buf.seek(0)
    return send_file(buf, as_attachment=True,
        download_name=f"Tracabilite_{safe}_{datetime.now().year}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.route('/api/stocker-bulletins', methods=['POST'])
def stocker_bulletins():
    """Génère et stocke les bulletins PDF dans le portail pour les clients sélectionnés"""
    j = request.json or {}
    client_ids = j.get('clients', [])
    conn = get_db()
    if client_ids:
        clients = [dict_from_row(conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()) for cid in client_ids]
        clients = [c for c in clients if c]
    else:
        clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY id").fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    results = []
    date_str = datetime.now().strftime('%Y-%m-%d')
    for client in clients:
        try:
            cid = client["id"]
            prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
            suivi = dict_from_row(conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone())
            meteo = fetch_meteo_for_client(client)
            doc = _build_bulletin(client, av, prescriptions, suivi, meteo)
            buf = io.BytesIO(); doc.save(buf); buf.seek(0)
            safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
            pdf_buf = docx_to_pdf(buf)
            if pdf_buf:
                fname = f"Bulletin_{safe}_{date_str}.pdf"
                _save_bulletin_to_portail(client, pdf_buf, fname)
            else:
                buf.seek(0)
                fname = f"Bulletin_{safe}_{date_str}.docx"
                _save_bulletin_to_portail(client, buf, fname)
            results.append({"client": client["exploitation"], "status": "ok", "file": fname})
        except Exception as e:
            results.append({"client": client.get("exploitation","?"), "status": "error", "message": str(e)})
    conn.close()
    return jsonify({"results": results})

def _save_bulletin_to_portail(client, pdf_buf_or_docx, fname):
    """Sauvegarde le bulletin dans docs_portail/clients/<id>/bulletins/"""
    try:
        cid = client["id"]
        bulletins_dir = os.path.join(DOCS_DIR, "clients", cid, "bulletins")
        os.makedirs(bulletins_dir, exist_ok=True)
        path = os.path.join(bulletins_dir, fname)
        if hasattr(pdf_buf_or_docx, 'seek'):
            pdf_buf_or_docx.seek(0)
            with open(path, 'wb') as f:
                f.write(pdf_buf_or_docx.read())
            pdf_buf_or_docx.seek(0)
        else:
            with open(path, 'wb') as f:
                f.write(pdf_buf_or_docx)
        # Garder uniquement les 10 derniers bulletins par client
        files = sorted([f for f in os.listdir(bulletins_dir) if f.endswith('.pdf') or f.endswith('.docx')])
        for old in files[:-10]:
            try: os.unlink(os.path.join(bulletins_dir, old))
            except: pass
        print(f"   📁 Bulletin archivé : {fname}")
    except Exception as e:
        print(f"   ⚠️ Archivage portail erreur : {e}")

def make_slug(exploitation):
    """Convertit un nom d'exploitation en slug URL-safe"""
    import unicodedata
    # Normalisation unicode → suppression des accents
    nfkd = unicodedata.normalize('NFKD', exploitation.lower())
    ascii_str = ''.join(c for c in nfkd if not unicodedata.combining(c))
    # Remplacer tout ce qui n'est pas alphanumérique par un tiret
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_str)
    slug = slug.strip('-')
    return slug or "client"

@app.route('/api/portail/generer-tokens', methods=['POST'])
def generer_tokens():
    """Génère un token et un slug pour chaque client qui n'en a pas"""
    import secrets
    conn = get_db()
    clients = dicts_from_rows(conn.execute("SELECT id, exploitation, portail_token, portail_slug FROM clients ORDER BY id").fetchall())
    # Collecter les slugs existants pour éviter les doublons
    existing_slugs = {c["portail_slug"] for c in clients if c.get("portail_slug")}
    results = []
    for c in clients:
        token = c.get("portail_token")
        slug = c.get("portail_slug")
        changed = False
        if not token:
            token = secrets.token_urlsafe(16)
            changed = True
        if not slug:
            base_slug = make_slug(c["exploitation"])
            slug = base_slug
            counter = 2
            while slug in existing_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            existing_slugs.add(slug)
            changed = True
        if changed:
            conn.execute("UPDATE clients SET portail_token=?, portail_slug=? WHERE id=?", (token, slug, c["id"]))
        results.append({"id": c["id"], "exploitation": c["exploitation"], "token": token, "slug": slug})
    conn.commit(); conn.close()
    return jsonify({"results": results})

@app.route('/api/portail/liens', methods=['GET'])
def portail_liens():
    """Liste les liens portail de tous les clients"""
    conn = get_db()
    clients = dicts_from_rows(conn.execute("SELECT id, exploitation, commune, certification, telephone, interlocuteur, portail_token, portail_slug FROM clients ORDER BY id").fetchall())
    conn.close()
    return jsonify(clients)

def _normaliser_commune(s):
    """Normalise un nom de commune pour comparaison : ignore accents, tirets,
    espaces multiples et casse. 'Mont-Saint-Père' et 'mont saint pere' doivent
    être reconnus comme la même commune, quelle que soit la façon dont chacun
    a été saisi (import PDF officiel vs saisie manuelle d'un vigneron)."""
    import unicodedata
    if not s: return ''
    nfkd = unicodedata.normalize('NFKD', s)
    sans_accents = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r'[-\s]+', ' ', sans_accents.upper()).strip()


CEPAGES_CANONIQUES = ['Chardonnay', 'Pinot Noir', 'Meunier', 'Voltis',
                      'Pinot Blanc', 'Pinot Gris', 'Arbane', 'Petit Meslier']

def _normaliser_cepage(s):
    """Nettoie une saisie de cépage : retire le code couleur officiel parfois
    accolé sur les documents administratifs (N=noir, B=blanc, G=gris — ex.
    'MEUNIER N', 'CHARDONNAY B', 'PINOT NOIR N' tel qu'on le voit sur un CVI ou
    une déclaration de récolte), puis fait correspondre à l'orthographe standard
    déjà utilisée dans l'appli si le cépage est connu. Une saisie inconnue est
    simplement nettoyée (espaces), jamais rejetée."""
    if not s: return s
    nettoye = re.sub(r'\s+[NBGnbg]$', '', s.strip()).strip()
    if not nettoye: return s.strip()
    cible = _normaliser_commune(nettoye)  # même normalisation (accents/casse/espaces)
    for c in CEPAGES_CANONIQUES:
        if _normaliser_commune(c) == cible:
            return c
    return nettoye


def get_client_by_token_or_slug(identifier):
    """Récupère un client par token ou par slug"""
    conn = get_db()
    # Essayer d'abord par slug
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_slug=?", (identifier,)).fetchone())
    if not client:
        # Puis par token
        client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (identifier,)).fetchone())
    conn.close()
    return client


def _parcelles_avec_etat_recolte(id_client):
    """Renvoie les parcelles avec l'état de récolte réellement enregistré en base
    (campagne 2026) — recolte_complete et kg_recoltes_total — plutôt que la seule
    mémoire éphémère du navigateur. Nécessaire pour qu'un recalcul d'itinéraire
    (nouvel appareil, autre membre de l'équipe, après fermeture de l'appli...)
    exclue correctement les parcelles déjà vendangées. Inclut aussi la date
    d'ouverture qui s'applique à chaque parcelle (spécifique au cépage si elle
    existe, sinon celle commune à tous les cépages de la commune).

    La correspondance commune/cépage se fait en Python après normalisation
    (accents, tirets, casse ignorés) plutôt qu'en égalité SQL stricte — un
    vigneron qui tape 'Mont Saint Pere' doit retrouver la date enregistrée pour
    'MONT-SAINT-PERE' importée depuis le PDF officiel."""
    conn = get_db()
    rows = dicts_from_rows(conn.execute("""
        SELECT p.*, r.recolte_complete, r.kg_recoltes_total
        FROM parcelles p
        LEFT JOIN (
            SELECT id_parcelle, recolte_complete, kg_recoltes_total,
                   ROW_NUMBER() OVER (PARTITION BY id_parcelle ORDER BY id DESC) rn
            FROM rendements WHERE campagne='2026'
        ) r ON r.id_parcelle = p.id AND r.rn = 1
        WHERE p.id_client=? ORDER BY p.nom
    """, (id_client,)).fetchall())
    dates = dicts_from_rows(conn.execute(
        "SELECT * FROM dates_ouverture WHERE campagne='2026'"
    ).fetchall())
    conn.close()

    dates_norm = [dict(d, _commune_n=_normaliser_commune(d['commune']),
                        _cepage_n=_normaliser_commune(d['cepage']) if d['cepage'] else None)
                  for d in dates]
    for p in rows:
        commune_n = _normaliser_commune(p.get('commune'))
        cepage_n = _normaliser_commune(p.get('cepage')) if p.get('cepage') else None
        specifique = next((d for d in dates_norm if d['_commune_n'] == commune_n and d['_cepage_n'] == cepage_n), None)
        generique = next((d for d in dates_norm if d['_commune_n'] == commune_n and d['_cepage_n'] is None), None)
        match = specifique or generique
        p['date_ouverture'] = match['date_ouverture'] if match else None
    return rows


# ===== Dates d'ouverture (ban des vendanges) =====

@app.route('/api/dates-ouverture', methods=['GET'])
def admin_dates_ouverture_liste():
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    rows = dicts_from_rows(conn.execute(
        "SELECT * FROM dates_ouverture WHERE campagne=? ORDER BY commune, cepage", (campagne,)
    ).fetchall())
    conn.close()
    return jsonify(rows)

@app.route('/api/dates-ouverture', methods=['POST'])
def admin_dates_ouverture_ajouter():
    return _ajouter_date_ouverture(request.json or {}, source='admin')

def _ajouter_date_ouverture(d, source='admin'):
    campagne = d.get('campagne') or '2026'
    commune = (d.get('commune') or '').strip().upper()
    cepage = (d.get('cepage') or '').strip() or None
    date_ouv = d.get('date_ouverture')
    if not commune or not date_ouv:
        return jsonify({"error": "Commune et date d'ouverture requises."}), 400
    conn = get_db()
    # Recherche par commune/cépage NORMALISÉS (accents, tirets, casse ignorés) pour
    # ne pas créer un doublon si la commune existe déjà sous une autre forme
    # d'écriture (ex. 'MONT SAINT PERE' saisi à la main vs 'MONT-SAINT-PERE' importé
    # du PDF officiel) — on garde alors l'orthographe déjà enregistrée, on ne met à
    # jour que la date.
    commune_n = _normaliser_commune(commune)
    cepage_n = _normaliser_commune(cepage) if cepage else None
    candidats = conn.execute(
        "SELECT * FROM dates_ouverture WHERE campagne=?", (campagne,)).fetchall()
    existing = next((c for c in candidats
                      if _normaliser_commune(c['commune']) == commune_n
                      and (_normaliser_commune(c['cepage']) if c['cepage'] else None) == cepage_n), None)
    if existing:
        conn.execute("UPDATE dates_ouverture SET date_ouverture=?, source=? WHERE id=?",
                     (date_ouv, source, existing['id']))
    else:
        conn.execute(
            "INSERT INTO dates_ouverture (campagne, commune, cepage, date_ouverture, source) VALUES (?,?,?,?,?)",
            (campagne, commune, cepage, date_ouv, source))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/dates-ouverture/<int:did>', methods=['DELETE'])
def admin_dates_ouverture_supprimer(did):
    conn = get_db()
    conn.execute("DELETE FROM dates_ouverture WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/dates-ouverture/import-pdf', methods=['POST'])
def admin_dates_ouverture_import_pdf():
    """Lit un PDF officiel du Comité Champagne ('Dates d'ouverture de la vendange')
    et propose une extraction commune/cépage/date à VÉRIFIER — rien n'est enregistré
    à cette étape, ce sont des dates réglementaires, une erreur de lecture ne doit
    jamais passer en silence. L'admin corrige puis confirme via /import-confirmer.

    Structure attendue (validée sur le document réel 2026 du Comité Champagne) :
    un tableau par page/section avec 2 blocs commune côte à côte, chaque bloc =
    [Commune, Chardonnay, renvoi, Pinot noir, renvoi, Meunier, renvoi]. Les dates
    sont en JJ/MM sans année (l'année vient du titre du document, ou de la
    campagne en cours à défaut). Une commune dont les 3 cépages ont la même date
    est regroupée en une seule ligne 'tous cépages' ; sinon une ligne par cépage
    concerné (les cépages sans date, ex. Pinot noir absent d'une commune, sont
    ignorés). Toute ligne portant un renvoi de note (ex. '(4)') est signalée en
    confiance basse plutôt que d'essayer d'interpréter la note automatiquement."""
    if 'fichier' not in request.files:
        return jsonify({"error": "Aucun fichier reçu."}), 400
    f = request.files['fichier']
    try:
        import pdfplumber
    except ImportError:
        return jsonify({"error": "pdfplumber n'est pas installé sur le serveur."}), 500

    try:
        premiere_page_texte = ''
        lignes_tables = []
        with pdfplumber.open(f) as pdf:
            if pdf.pages:
                premiere_page_texte = pdf.pages[0].extract_text() or ''
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    lignes_tables.extend(table)
    except Exception as e:
        return jsonify({"error": f"Impossible de lire ce PDF : {e}"}), 400

    if not lignes_tables:
        return jsonify({"error": "Aucun tableau détecté dans ce PDF — structure inattendue, vérifiez le fichier."}), 400

    m_annee = re.search(r'vendange[s]?\s+(\d{4})', premiere_page_texte, re.IGNORECASE)
    annee = m_annee.group(1) if m_annee else '2026'

    propositions = []
    for row in lignes_tables:
        if not row or len(row) < 15:
            continue
        if row[0] and row[0].strip() == 'Crus':
            continue  # ligne d'en-tête
        for base in (0, 8):  # deux blocs commune côte à côte par ligne
            commune = row[base]
            if not commune or not commune.strip():
                continue
            commune = commune.strip().upper()
            cepages = {
                'Chardonnay': (row[base+1], row[base+2]),
                'Pinot Noir': (row[base+3], row[base+4]),
                'Meunier':    (row[base+5], row[base+6]),
            }
            valides = {c: ((d or '').strip(), (n or '').strip()) for c, (d, n) in cepages.items() if d and d.strip()}
            if not valides:
                continue
            dates_distinctes = {d for d, n in valides.values()}
            a_renvoi = any(n for d, n in valides.values())

            def _vers_iso(jjmm):
                m = re.match(r'^(\d{1,2})/(\d{1,2})$', jjmm)
                if not m: return None
                j, mo = m.groups()
                try: return f"{int(annee):04d}-{int(mo):02d}-{int(j):02d}"
                except Exception: return None

            if len(dates_distinctes) == 1 and len(valides) == 3:
                date_iso = _vers_iso(list(dates_distinctes)[0])
                if not date_iso: continue
                propositions.append({
                    "commune": commune, "cepage": "", "date_ouverture": date_iso,
                    "confiance": "basse" if a_renvoi else "haute",
                    "note": "renvoi de note à vérifier sur le PDF original" if a_renvoi else "",
                })
            else:
                for cepage, (date, note) in valides.items():
                    date_iso = _vers_iso(date)
                    if not date_iso: continue
                    propositions.append({
                        "commune": commune, "cepage": cepage, "date_ouverture": date_iso,
                        "confiance": "basse" if note else "haute",
                        "note": f"renvoi {note} à vérifier sur le PDF original" if note else "",
                    })

    return jsonify({
        "propositions": propositions,
        "annee_detectee": annee,
        "nb_lignes_analysees": len(lignes_tables),
        "avertissement": "Vérifiez notamment les lignes en confiance basse (renvois de note) avant de confirmer — elles peuvent concerner un zonage particulier non capturé automatiquement."
    })

@app.route('/api/dates-ouverture/import-confirmer', methods=['POST'])
def admin_dates_ouverture_import_confirmer():
    lignes = (request.json or {}).get('lignes', [])
    n_ok, erreurs = 0, []
    for l in lignes:
        r = _ajouter_date_ouverture(l, source='import_pdf')
        status = r[1] if isinstance(r, tuple) else r.status_code
        if status == 200:
            n_ok += 1
        else:
            erreurs.append(l)
    return jsonify({"status": "ok", "nb_importees": n_ok, "erreurs": erreurs})



def generer_token_slug_pour_client(cid, exploitation, conn):
    """Génère un token + slug uniques pour un client tout juste inscrit."""
    token = secrets.token_urlsafe(16)
    base_slug = make_slug(exploitation)
    existing = {r[0] for r in conn.execute(
        "SELECT portail_slug FROM clients WHERE portail_slug IS NOT NULL").fetchall()}
    slug = base_slug
    counter = 2
    while slug in existing:
        slug = f"{base_slug}-{counter}"
        counter += 1
    conn.execute("UPDATE clients SET portail_token=?, portail_slug=? WHERE id=?", (token, slug, cid))
    return token, slug


def send_email_auto(to_email, subject, html_body):
    """Envoi SMTP direct (contrairement aux brouillons IMAP utilisés ailleurs) —
    utilisé pour les emails automatiques (bienvenue, confirmation paiement)."""
    if not SMTP_PASSWORD:
        print(f"[email] SMTP_PASSWORD non configuré — email à {to_email} ignoré")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[email] Erreur envoi à {to_email}: {e}")
        return False


def _est_compte_saas(client):
    """True uniquement pour les comptes issus de l'inscription publique — jamais pour
    les clients créés par Florent depuis son admin (ses 18 clients conseil), qui ne
    doivent jamais être soumis à l'essai ni au paiement."""
    return client.get('origine') == 'inscription'


def _jours_restants_essai(client):
    """None = pas de limite de temps applicable (compte admin, ou date d'inscription
    inconnue). Sinon le nombre de jours restants (peut être négatif si expiré)."""
    if not _est_compte_saas(client) or not client.get('date_inscription'):
        return None
    try:
        d_ins = datetime.fromisoformat(client['date_inscription'])
    except Exception:
        return None
    return LIMITE_JOURS_ESSAI - (datetime.now() - d_ins).days


def _prix_client(client):
    """Retourne (libellé prix affiché, price_id Stripe) pour ce client. Les
    LIMITE_OFFRE_LANCEMENT premiers comptes inscrits bénéficient du tarif de
    lancement, à condition que le price_id correspondant soit configuré."""
    numero = client.get('numero_inscription')
    eligible = (
        _est_compte_saas(client)
        and numero is not None
        and numero <= LIMITE_OFFRE_LANCEMENT
        and bool(STRIPE_PRICE_ID_LANCEMENT)
    )
    if eligible:
        return PRIX_LANCEMENT, STRIPE_PRICE_ID_LANCEMENT
    return PRIX_ANNUEL, STRIPE_PRICE_ID


def _compte_statut(client):
    conn = get_db()
    n_parc = conn.execute(
        "SELECT COUNT(*) c FROM parcelles WHERE id_client=? AND (est_exemple IS NULL OR est_exemple=0)",
        (client['id'],)).fetchone()['c']
    n_parc_exemple = conn.execute(
        "SELECT COUNT(*) c FROM parcelles WHERE id_client=? AND est_exemple=1", (client['id'],)).fetchone()['c']
    conn.close()
    jours_restants = _jours_restants_essai(client)
    prix, _ = _prix_client(client)
    # Tes clients conseil (créés depuis l'admin) ne sont jamais bridés — le statut
    # renvoyé au front doit refléter ça, sinon l'écran de verrouillage s'affiche
    # même quand l'accès réel n'est pas bloqué côté serveur.
    statut_effectif = 'payant' if not _est_compte_saas(client) else (client.get('statut_compte') or 'essai')
    return jsonify({
        "statut": statut_effectif,
        "nb_parcelles": n_parc,
        "a_des_donnees_exemple": n_parc_exemple > 0,
        "jours_restants": jours_restants,
        "essai_expire": jours_restants is not None and jours_restants <= 0,
        "duree_essai_jours": LIMITE_JOURS_ESSAI,
        "prix": prix,
        "offre_lancement": prix == PRIX_LANCEMENT
    })


def _creer_session_stripe(client, return_path):
    _, price_id = _prix_client(client)
    if not stripe or not STRIPE_SECRET_KEY or not price_id:
        return jsonify({"error": "Le paiement n'est pas encore configuré. Contactez le support MatuScore."}), 500
    try:
        checkout = stripe.checkout.Session.create(
            mode='subscription',
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            customer_email=client.get('email'),
            client_reference_id=client['id'],
            success_url=f"{DOMAIN}{return_path}?paiement=succes",
            cancel_url=f"{DOMAIN}{return_path}?paiement=annule",
        )
        return jsonify({"url": checkout.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== Inscription publique / connexion vigneron =====

@app.route('/inscription')
def page_inscription():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'inscription.html')


@app.route('/api/inscription', methods=['POST'])
def api_inscription():
    d = request.json or {}
    exploitation = (d.get('exploitation') or '').strip()
    email = (d.get('email') or '').strip().lower()
    password = d.get('password') or ''
    commune = (d.get('commune') or '').strip().upper() or None
    telephone = (d.get('telephone') or '').strip() or None

    if not exploitation or not email or not password:
        return jsonify({"error": "Merci de renseigner l'exploitation, l'email et un mot de passe."}), 400
    if not commune:
        return jsonify({"error": "Merci de renseigner votre commune."}), 400
    if len(password) < 6:
        return jsonify({"error": "Le mot de passe doit contenir au moins 6 caractères."}), 400
    if '@' not in email:
        return jsonify({"error": "Adresse email invalide."}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM clients WHERE lower(email)=?", (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Un compte existe déjà avec cet email. Connectez-vous plutôt."}), 409

    numero = (conn.execute("SELECT COUNT(*) c FROM clients WHERE origine='inscription'").fetchone()['c']) + 1
    cid = secrets.token_hex(6)
    conn.execute("""INSERT INTO clients
        (id, exploitation, email, telephone, commune, password_hash, statut_compte, date_inscription, origine, certification, numero_inscription)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, exploitation, email, telephone, commune, generate_password_hash(password),
         'essai', datetime.now().isoformat(timespec='seconds'), 'inscription', 'Conventionnel', numero))
    token, slug = generer_token_slug_pour_client(cid, exploitation, conn)
    conn.commit()
    conn.close()

    session.permanent = True
    session['client_id'] = cid

    prix_msg, _ = _prix_client({'numero_inscription': numero, 'origine': 'inscription'})
    offre_txt = (f"Passez à l'accès complet à tout moment pour {prix_msg} "
                 f"(offre de lancement — tarif normal {PRIX_ANNUEL} au-delà des {LIMITE_OFFRE_LANCEMENT} premiers inscrits)."
                 if prix_msg == PRIX_LANCEMENT else
                 f"Passez à l'accès complet à tout moment pour {prix_msg}.")

    send_email_auto(email, "Bienvenue sur MatuScore 🍇", f"""
        <p>Bonjour,</p>
        <p>Votre compte <strong>{exploitation}</strong> est créé sur MatuScore.</p>
        <p>Vous pouvez y accéder à tout moment ici : <a href="{DOMAIN}/connexion">{DOMAIN}/connexion</a></p>
        <p>Votre essai gratuit de {LIMITE_JOURS_ESSAI} jours vous donne accès à toutes les fonctionnalités
        (parcelles, maturité, rendements), sauf l'itinéraire de récolte et les exports PDF/CSV.
        Vous pouvez aussi charger des données d'exemple pour découvrir l'appli tout de suite.
        {offre_txt}</p>
        <p>À bientôt,<br>Florent — MatuScore (VITI Sens)</p>
    """)

    return jsonify({"status": "ok", "redirect": f"/portail/{token}"})


@app.route('/connexion')
def page_connexion():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'connexion.html')


def _get_client_by_email(email):
    """Certaines fiches client (créées avant MatuScore) ont plusieurs adresses dans
    le même champ, séparées par ';' — n'importe laquelle doit être reconnue, pas
    seulement une correspondance exacte du champ entier."""
    email = (email or '').strip().lower()
    conn = get_db()
    client = dict_from_row(conn.execute(
        "SELECT * FROM clients WHERE lower(email)=? "
        "OR ';'||lower(email)||';' LIKE '%;'||?||';%'",
        (email, email)).fetchone())
    conn.close()
    return client


@app.route('/api/connexion', methods=['POST'])
def api_connexion():
    d = request.json or {}
    email = (d.get('email') or '').strip().lower()
    password = d.get('password') or ''
    client = _get_client_by_email(email)
    if not client or not client.get('password_hash') or not check_password_hash(client['password_hash'], password):
        return jsonify({"error": "Email ou mot de passe incorrect."}), 401
    session.permanent = True
    session['client_id'] = client['id']
    return jsonify({"status": "ok", "redirect": f"/portail/{client['portail_token']}"})


@app.route('/mot-de-passe-oublie')
def page_mdp_oublie():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'mot-de-passe-oublie.html')


@app.route('/api/mot-de-passe-oublie', methods=['POST'])
def api_mdp_oublie():
    d = request.json or {}
    email_saisi = (d.get('email') or '').strip().lower()
    client = _get_client_by_email(email_saisi)
    # Toujours la même réponse, que l'email existe ou non — on ne révèle jamais
    # quels emails sont enregistrés.
    reponse = {"status": "ok", "message": "Si un compte existe avec cet email, un lien de réinitialisation vient d'être envoyé."}
    if client and client.get('email'):
        token = secrets.token_urlsafe(32)
        expire = (datetime.now() + timedelta(hours=1)).isoformat(timespec='seconds')
        conn = get_db()
        conn.execute("UPDATE clients SET reset_token=?, reset_token_expire=? WHERE id=?",
                     (token, expire, client['id']))
        conn.commit()
        conn.close()
        lien = f"{DOMAIN}/reinitialiser-mot-de-passe?token={token}"
        # Envoyer à l'adresse SAISIE par la personne (pas au champ email complet,
        # qui peut contenir plusieurs adresses séparées par ';')
        send_email_auto(email_saisi, "Réinitialiser votre mot de passe MatuScore 🍇", f"""
            <p>Bonjour,</p>
            <p>Une demande de réinitialisation de mot de passe a été faite pour le compte
            <strong>{client['exploitation']}</strong> sur MatuScore.</p>
            <p><a href="{lien}">Cliquez ici pour choisir un nouveau mot de passe</a></p>
            <p style="font-size:12px;color:#888">Ce lien expire dans 1 heure. Si vous n'êtes pas à
            l'origine de cette demande, ignorez cet email — rien ne change.</p>
            <p>À bientôt,<br>Florent — MatuScore (VITI Sens)</p>
        """)
    return jsonify(reponse)


@app.route('/reinitialiser-mot-de-passe')
def page_reinitialiser_mdp():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'reinitialiser-mot-de-passe.html')


@app.route('/api/reinitialiser-mot-de-passe', methods=['POST'])
def api_reinitialiser_mdp():
    d = request.json or {}
    token = d.get('token') or ''
    nouveau = d.get('nouveau_mot_de_passe') or ''
    if len(nouveau) < 6:
        return jsonify({"error": "Le mot de passe doit contenir au moins 6 caractères."}), 400
    if not token:
        return jsonify({"error": "Lien invalide."}), 400

    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE reset_token=?", (token,)).fetchone())
    if not client:
        conn.close()
        return jsonify({"error": "Lien invalide ou déjà utilisé."}), 400
    try:
        expire = datetime.fromisoformat(client['reset_token_expire'])
    except Exception:
        expire = None
    if not expire or datetime.now() > expire:
        conn.close()
        return jsonify({"error": "Ce lien a expiré. Refaites une demande de réinitialisation."}), 400

    conn.execute("UPDATE clients SET password_hash=?, reset_token=NULL, reset_token_expire=NULL WHERE id=?",
                 (generate_password_hash(nouveau), client['id']))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/mon-espace')
def mon_espace():
    cid = session.get('client_id')
    if session.get('is_admin'):
        # Toi, en tant qu'admin : une page qui liste tous les clients, avec accès
        # direct à chacun — un seul identifiant (le tien) donne accès à tout,
        # plutôt qu'un mot de passe par client.
        conn = get_db()
        clients = dicts_from_rows(conn.execute(
            "SELECT id, exploitation, commune, portail_token FROM clients "
            "WHERE portail_token IS NOT NULL ORDER BY exploitation").fetchall())
        conn.close()
        items = "".join(
            f'<a href="/portail/{c["portail_token"]}" style="display:block;padding:14px 16px;'
            f'border-bottom:1px solid #eee;text-decoration:none;color:#1f2937">'
            f'<div style="font-weight:600;font-size:14px">{c["exploitation"]}</div>'
            f'<div style="font-size:12px;color:#6B7280">{c["commune"] or ""}</div></a>'
            for c in clients
        )
        return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="MatuScore Admin">
        <link rel="manifest" href="/manifest.json?start=/mon-espace">
        <link rel="apple-touch-icon" href="/icon-180.png">
        <title>MatuScore — Mes clients</title>
        <style>body{{font-family:-apple-system,sans-serif;margin:0;background:#f8faf8}}
        .header{{background:#2D6A4F;color:#fff;padding:20px;font-weight:700;font-size:16px}}</style>
        </head><body>
        <div class="header">🍇 Mes clients ({len(clients)})</div>
        {items}
        </body></html>"""
    if not cid:
        return redirect('/connexion')
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT portail_token FROM clients WHERE id=?", (cid,)).fetchone())
    conn.close()
    if not client or not client.get('portail_token'):
        return redirect('/connexion')
    return redirect(f"/portail/{client['portail_token']}")


@app.route('/deconnexion')
def deconnexion():
    session.clear()
    return redirect('/connexion')


# ===== Statut essai / upgrade Stripe =====

@app.route('/api/portail/<token>/compte-statut')
def portail_compte_statut(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _compte_statut(client)


@app.route('/api/portail-s/<slug>/compte-statut')
def portail_compte_statut_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _compte_statut(client)


# ===== Données d'exemple (à charger/vider sur son propre compte) =====

@app.route('/api/portail/<token>/exemple/charger', methods=['POST'])
def portail_exemple_charger(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _exemple_charger(client)

@app.route('/api/portail-s/<slug>/exemple/charger', methods=['POST'])
def portail_exemple_charger_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _exemple_charger(client)

def _exemple_charger(client):
    conn = get_db()
    nb = _charger_donnees_exemple(client['id'], conn)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "nb_parcelles": nb})

@app.route('/api/portail/<token>/exemple/vider', methods=['POST'])
def portail_exemple_vider(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _exemple_vider(client)

@app.route('/api/portail-s/<slug>/exemple/vider', methods=['POST'])
def portail_exemple_vider_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _exemple_vider(client)

def _exemple_vider(client):
    conn = get_db()
    nb = _vider_donnees_exemple(client['id'], conn)
    conn.close()
    return jsonify({"status": "ok", "nb_supprimees": nb})


@app.route('/api/portail/<token>/mot-de-passe', methods=['POST'])
def portail_changer_mdp(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _changer_mot_de_passe(client)

@app.route('/api/portail-s/<slug>/mot-de-passe', methods=['POST'])
def portail_changer_mdp_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _changer_mot_de_passe(client)

def _changer_mot_de_passe(client):
    d = request.json or {}
    actuel = d.get('mot_de_passe_actuel') or ''
    nouveau = d.get('nouveau_mot_de_passe') or ''

    if not client.get('email'):
        return jsonify({"error": "Aucun email n'est associé à ce compte — contactez VITI Sens pour définir un mot de passe."}), 400
    if len(nouveau) < 6:
        return jsonify({"error": "Le nouveau mot de passe doit contenir au moins 6 caractères."}), 400
    # Un mot de passe déjà en place doit être confirmé avant d'en définir un nouveau ;
    # un compte qui n'en a encore aucun (lien direct jamais utilisé pour se connecter)
    # peut en définir un directement.
    if client.get('password_hash'):
        if not check_password_hash(client['password_hash'], actuel):
            return jsonify({"error": "Mot de passe actuel incorrect."}), 401

    conn = get_db()
    conn.execute("UPDATE clients SET password_hash=? WHERE id=?",
                 (generate_password_hash(nouveau), client['id']))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/portail/<token>/upgrade', methods=['POST'])
def portail_upgrade(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _creer_session_stripe(client, f"/portail/{token}")


@app.route('/api/portail-s/<slug>/upgrade', methods=['POST'])
def portail_upgrade_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _creer_session_stripe(client, f"/portail-s/{slug}")


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook non configuré"}), 500
    payload = request.data
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    conn = get_db()
    if event['type'] == 'checkout.session.completed':
        s = event['data']['object']
        cid = s.get('client_reference_id')
        if cid:
            conn.execute("""UPDATE clients SET statut_compte='payant', stripe_customer_id=?,
                stripe_subscription_id=?, date_paiement=? WHERE id=?""",
                (s.get('customer'), s.get('subscription'), datetime.now().isoformat(timespec='seconds'), cid))
            # Le compte payant démarre vierge : les données d'exemple éventuelles sont effacées.
            _vider_donnees_exemple(cid, conn, commit=False)
    elif event['type'] in ('customer.subscription.deleted', 'invoice.payment_failed'):
        obj = event['data']['object']
        sub_id = obj.get('id') if event['type'] == 'customer.subscription.deleted' else obj.get('subscription')
        if sub_id:
            conn.execute("UPDATE clients SET statut_compte='essai' WHERE stripe_subscription_id=?", (sub_id,))
    conn.commit()
    conn.close()
    return jsonify({"received": True})


@app.route('/portail-s/<slug>')
def portail_client_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client:
        return "<h1>Lien invalide</h1><p>Ce lien n'est pas valide. Contactez votre conseiller.</p>", 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'portail_client.html')

@app.route('/api/portail-s/<slug>/data')
def portail_data_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    cid = client["id"]
    conn = get_db()
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    conn.close()
    # Prochain passage : réutilise le numéro d'un passage annulé si disponible
    conn2 = get_db()
    prochain_passage = _prochain_passage_disponible(cid, conn2)
    conn2.close()
    presc_courantes = [p for p in prescriptions if p.get("applique") != "Oui" and p.get("statut") != "annulé"]
    meteo = fetch_meteo_for_client(client)
    client_safe = {k: v for k, v in client.items() if k not in ("portail_token", "portail_slug", "email", "telephone", "notes")}
    return jsonify({"client": client_safe, "bulletin": {"date": av.get("date_saisie",""), "phenologie": av.get("texte_phenologie",""), "risque_mildiou": av.get("risque_mildiou",""), "reco_mildiou": av.get("reco_mildiou",""), "risque_oidium": av.get("risque_oidium",""), "reco_oidium": av.get("reco_oidium",""), "gel": av.get("gel",""), "epi": av.get("epi","")}, "dernier_passage": prochain_passage, "prescriptions": presc_courantes, "all_prescriptions": prescriptions, "meteo": meteo or []})

@app.route('/api/portail-s/<slug>/valider', methods=['POST'])
def portail_valider_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    j = request.json
    presc_ids = j.get("prescription_ids") or ([j.get("prescription_id")] if j.get("prescription_id") else [])
    dates = j.get("dates") or ([j.get("date_reelle")] if j.get("date_reelle") else [])
    # Stocker dates multiples : JSON si plusieurs, string si une seule
    import json as _json
    date_str = _json.dumps(dates, ensure_ascii=False) if len(dates) > 1 else (dates[0] if dates else datetime.now().strftime("%Y-%m-%d"))
    conn = get_db()
    for pid in presc_ids:
        p = conn.execute("SELECT id FROM prescriptions WHERE id=? AND id_client=?", (pid, client["id"])).fetchone()
        if not p: continue
        conn.execute("UPDATE prescriptions SET applique='Oui', date_reelle=?, dose_reelle=?, observations=? WHERE id=?",
            (date_str, j.get("dose_reelle",""), j.get("observations",""), pid))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/portail-s/<slug>/docs')
def portail_docs_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    cid = client["id"]
    docs = []
    if os.path.exists(COMPL_PDF_DIR):
        for f in sorted(os.listdir(COMPL_PDF_DIR)):
            if f.lower().endswith('.pdf'):
                docs.append({"name": f, "size": os.path.getsize(os.path.join(COMPL_PDF_DIR, f)), "url": f"/api/portail-s/{slug}/docs/download/complementaires/{f}", "label": "📎 Bulletin"})
    commun_dir = os.path.join(DOCS_DIR, "communs")
    if os.path.exists(commun_dir):
        for f in sorted(os.listdir(commun_dir)):
            if f.lower().endswith('.pdf'):
                docs.append({"name": f, "size": os.path.getsize(os.path.join(commun_dir, f)), "url": f"/api/portail-s/{slug}/docs/download/communs/{f}", "label": "📄 Document"})
    client_dir = os.path.join(DOCS_DIR, "clients", cid)
    if os.path.exists(client_dir):
        bulletins_dir = os.path.join(client_dir, "bulletins")
        if os.path.exists(bulletins_dir):
            for f in sorted(os.listdir(bulletins_dir), reverse=True):
                if f.lower().endswith(('.pdf', '.docx')):
                    docs.append({"name": f, "size": os.path.getsize(os.path.join(bulletins_dir, f)), "url": f"/api/portail-s/{slug}/docs/download/{cid}/bulletins/{f}", "label": "📋 Bulletin technique"})
        for f in sorted(os.listdir(client_dir)):
            if f.lower().endswith('.pdf') and os.path.isfile(os.path.join(client_dir, f)):
                docs.append({"name": f, "size": os.path.getsize(os.path.join(client_dir, f)), "url": f"/api/portail-s/{slug}/docs/download/{cid}/{f}", "label": "📁 Personnel"})
    return jsonify(docs)

@app.route('/api/portail-s/<slug>/docs/download/<scope>/<path:name>')
def portail_docs_download_slug(slug, scope, name):
    client = get_client_by_token_or_slug(slug)
    if not client: return "Accès refusé", 403
    if scope == "complementaires": path = os.path.join(COMPL_PDF_DIR, name)
    elif scope == "communs": path = os.path.join(DOCS_DIR, "communs", name)
    else:
        if scope != client["id"]: return "Accès refusé", 403
        path = os.path.join(DOCS_DIR, "clients", scope, name)
    if not os.path.exists(path): return "Fichier non trouvé", 404
    return send_file(path, as_attachment=False, download_name=name, mimetype="application/pdf")

@app.route('/api/portail-s/<slug>/bulletin-pdf')
def portail_bulletin_pdf_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return "Accès refusé", 403
    cid = client["id"]
    conn = get_db()
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    suivi = dict_from_row(conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone())
    conn.close()
    meteo = fetch_meteo_for_client(client)
    doc = _build_bulletin(client, av, prescriptions, suivi, meteo)
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    pdf_buf = docx_to_pdf(buf)
    safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
    if pdf_buf:
        return send_file(pdf_buf, as_attachment=True, download_name=f"Bulletin_{safe}.pdf", mimetype="application/pdf")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"Bulletin_{safe}.docx", mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.route('/api/portail-s/<slug>/modifier-prescription', methods=['POST'])
def portail_modifier_prescription_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    j = request.json
    presc_id = j.get("prescription_id"); new_id_produit = j.get("id_produit"); new_dose = j.get("dose_prescrite","")
    conn = get_db()
    p = conn.execute("SELECT * FROM prescriptions WHERE id=? AND id_client=?", (presc_id, client["id"])).fetchone()
    if not p: conn.close(); return jsonify({"error": "Prescription non trouvée"}), 404
    cat = dict_from_row(conn.execute("SELECT * FROM catalogue WHERE id=?", (new_id_produit,)).fetchone())
    if not cat: conn.close(); return jsonify({"error": "Produit non trouvé"}), 404
    conn.execute("""UPDATE prescriptions SET id_produit=?, nom_produit=?, substance_active=?, type_cps=?, dose_homologuee=?, dose_prescrite=? WHERE id=?""",
        (cat["id"], cat["nom"], cat.get("substance_active",""), cat.get("type_cps",""), cat.get("dose_homologuee",""), new_dose or cat.get("dose_homologuee",""), presc_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/portail-s/<slug>/catalogue')
def portail_catalogue_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    certif = client.get("certification", "Conventionnel")
    q = "SELECT id, nom, cible, substance_active, famille, type_cps, dose_homologuee, dar, dre, znt, option_abc FROM catalogue WHERE categorie='Phyto'"
    if certif == "Bio": q += " AND compatible_bio=1"
    elif certif == "HVE": q += " AND compatible_hve=1"
    conn = get_db()
    prods = dicts_from_rows(conn.execute(q + " ORDER BY cible, famille, nom").fetchall())
    conn.close()
    return jsonify({"produits": prods, "certification": certif})

# --- PORTAIL CLIENT ---
@app.route('/portail/<token>')
def portail_client(token):
    """Page portail client — accès via token unique"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    conn.close()
    if not client:
        return "<h1>Lien invalide</h1><p>Ce lien d'accès n'est pas valide. Contactez votre conseiller.</p>", 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'portail_client.html')

@app.route('/api/portail/<token>/data')
def portail_data(token):
    """Données du portail : client + prescriptions + bulletin + météo"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client:
        conn.close(); return jsonify({"error": "Token invalide"}), 404
    cid = client["id"]
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    conn.close()

    # Dernier passage
    # Prochain passage : réutilise le numéro d'un passage annulé si disponible
    conn2 = get_db()
    prochain_passage = _prochain_passage_disponible(client["id"], conn2)
    conn2.close()
    # Prescriptions en attente — exclure les annulées
    presc_courantes = [p for p in prescriptions if p.get("applique") != "Oui" and p.get("statut") != "annulé"]

    meteo = fetch_meteo_for_client(client)
    client_safe = {k: v for k, v in client.items() if k not in ("portail_token", "email", "telephone", "notes")}

    return jsonify({
        "client": client_safe,
        "bulletin": {
            "date": av.get("date_saisie", ""),
            "phenologie": av.get("texte_phenologie", ""),
            "risque_mildiou": av.get("risque_mildiou", ""),
            "reco_mildiou": av.get("reco_mildiou", ""),
            "risque_oidium": av.get("risque_oidium", ""),
            "reco_oidium": av.get("reco_oidium", ""),
            "gel": av.get("gel", ""),
            "epi": av.get("epi", ""),
        },
        "dernier_passage": prochain_passage,
        "prescriptions": presc_courantes,
        "all_prescriptions": prescriptions,
        "meteo": meteo or [],
    })

@app.route('/api/portail/<token>/catalogue')
def portail_catalogue(token):
    """Catalogue filtré pour le portail client — produits compatibles avec la certification"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Token invalide"}), 404
    certif = client.get("certification", "Conventionnel")
    q = "SELECT id, nom, cible, substance_active, famille, type_cps, dose_homologuee, dar, dre, znt, option_abc FROM catalogue WHERE categorie='Phyto'"
    if certif == "Bio": q += " AND compatible_bio=1"
    elif certif == "HVE": q += " AND compatible_hve=1"
    prods = dicts_from_rows(conn.execute(q + " ORDER BY cible, famille, nom").fetchall())
    conn.close()
    return jsonify({"produits": prods, "certification": certif})

@app.route('/api/portail/<token>/modifier-prescription', methods=['POST'])
def portail_modifier_prescription(token):
    """Le client modifie le produit d'une prescription (changement de stratégie)"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Token invalide"}), 404
    j = request.json
    presc_id = j.get("prescription_id")
    new_id_produit = j.get("id_produit")
    new_dose = j.get("dose_prescrite", "")
    p = conn.execute("SELECT * FROM prescriptions WHERE id=? AND id_client=?", (presc_id, client["id"])).fetchone()
    if not p: conn.close(); return jsonify({"error": "Prescription non trouvée"}), 404
    cat = dict_from_row(conn.execute("SELECT * FROM catalogue WHERE id=?", (new_id_produit,)).fetchone())
    if not cat: conn.close(); return jsonify({"error": "Produit non trouvé"}), 404
    certif = client.get("certification", "Conventionnel")
    if certif == "Bio" and not cat.get("compatible_bio"):
        conn.close(); return jsonify({"error": "Ce produit n'est pas compatible Bio"}), 400
    conn.execute("""UPDATE prescriptions SET id_produit=?, nom_produit=?, substance_active=?,
        type_cps=?, dose_homologuee=?, dose_prescrite=? WHERE id=?""",
        (cat["id"], cat["nom"], cat.get("substance_active",""), cat.get("type_cps",""),
         cat.get("dose_homologuee",""), new_dose or cat.get("dose_homologuee",""), presc_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": f"Produit modifié → {cat['nom']}"})


def _annuler_passage(client_id, passage, motif, conn):
    """
    Marque tous les produits d'un passage comme annulés.
    Si des passages actifs Tx suivants existent (x > num annulé),
    les re-numérote en décalant d'un cran vers le bas (T8→T7, T9→T8...).
    """
    # Numéro du passage annulé
    def num(p):
        if p and p[0] == 'T' and p[1:].isdigit(): return int(p[1:])
        return None

    n_annule = num(passage)

    # Annuler le passage
    conn.execute("""
        UPDATE prescriptions
        SET statut='annulé', motif_annulation=?, applique='Non'
        WHERE id_client=? AND passage=? AND (statut IS NULL OR statut != 'annulé')
    """, (motif, client_id, passage))

    # Re-numéroter les passages Tx actifs strictement supérieurs
    if n_annule is not None:
        # Récupérer les passages actifs Tx > n_annule, triés par ordre croissant
        actifs_suivants = sorted(set(
            r['passage'] for r in conn.execute("""
                SELECT DISTINCT passage FROM prescriptions
                WHERE id_client=? AND (statut IS NULL OR statut NOT IN ('annulé'))
                  AND passage IS NOT NULL
            """, (client_id,)).fetchall()
            if num(r['passage']) is not None and num(r['passage']) > n_annule  # type: ignore
        ), key=lambda p: num(p))  # type: ignore

        # Décaler chaque passage d'un cran vers le bas dans l'ordre croissant
        for p in actifs_suivants:
            n = num(p)
            nouveau = f"T{n - 1}"
            conn.execute("""
                UPDATE prescriptions SET passage=?
                WHERE id_client=? AND passage=? AND (statut IS NULL OR statut != 'annulé')
            """, (nouveau, client_id, p))

    return passage


def _prochain_passage_disponible(client_id, conn):
    """
    Retourne le prochain numéro de passage à utiliser.
    Règle : si un passage est annulé et qu'aucun passage actif n'a le même numéro → réutiliser ce numéro.
    Sinon → incrémenter le dernier passage actif.
    """
    # Passages annulés sans équivalent actif
    annules = [r['passage'] for r in dicts_from_rows(conn.execute("""
        SELECT DISTINCT passage FROM prescriptions
        WHERE id_client=? AND statut='annulé' AND passage IS NOT NULL
    """, (client_id,)).fetchall())]

    actifs = set(r['passage'] for r in dicts_from_rows(conn.execute("""
        SELECT DISTINCT passage FROM prescriptions
        WHERE id_client=? AND (statut IS NULL OR statut != 'annulé') AND passage IS NOT NULL
    """, (client_id,)).fetchall()))

    # Passages annulés sans actif correspondant, triés numériquement
    def num(p):
        if p and p[0]=='T' and p[1:].isdigit(): return int(p[1:])
        return 999
    recuperables = sorted([p for p in annules if p not in actifs], key=num)
    if recuperables:
        return recuperables[0]

    # Sinon : T(max_actif + 1)
    nums_actifs = [num(p) for p in actifs if num(p) < 999]
    next_n = max(nums_actifs) + 1 if nums_actifs else 1
    return f"T{next_n}"


@app.route('/api/portail-s/<slug>/annuler-passage', methods=['POST'])
def portail_annuler_passage_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    j = request.json or {}
    passage = j.get("passage")
    motif = (j.get("motif") or "").strip()
    if not passage: return jsonify({"error": "Passage requis"}), 400
    if not motif: return jsonify({"error": "Motif d'annulation requis"}), 400
    conn = get_db()
    _annuler_passage(client["id"], passage, motif, conn)
    prochain = _prochain_passage_disponible(client["id"], conn)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "passage_annule": passage, "prochain_passage": prochain})


@app.route('/api/portail/<token>/annuler-passage', methods=['POST'])
def portail_annuler_passage_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    j = request.json or {}
    passage = j.get("passage")
    motif = (j.get("motif") or "").strip()
    if not passage: return jsonify({"error": "Passage requis"}), 400
    if not motif: return jsonify({"error": "Motif d'annulation requis"}), 400
    conn = get_db()
    _annuler_passage(client["id"], passage, motif, conn)
    prochain = _prochain_passage_disponible(client["id"], conn)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "passage_annule": passage, "prochain_passage": prochain})


@app.route('/api/portail-s/<slug>/prochain-passage')
def portail_prochain_passage_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    conn = get_db()
    prochain = _prochain_passage_disponible(client["id"], conn)
    conn.close()
    return jsonify({"prochain_passage": prochain})



@app.route('/api/portail/<token>/valider', methods=['POST'])
def portail_valider(token):
    """Le client valide qu'un traitement a été effectué — supporte dates multiples"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Token invalide"}), 404
    j = request.json
    presc_ids = j.get("prescription_ids") or ([j.get("prescription_id")] if j.get("prescription_id") else [])
    dates = j.get("dates") or ([j.get("date_reelle")] if j.get("date_reelle") else [])
    import json as _json
    date_str = _json.dumps(dates, ensure_ascii=False) if len(dates) > 1 else (dates[0] if dates else datetime.now().strftime("%Y-%m-%d"))
    for pid in presc_ids:
        p = conn.execute("SELECT id FROM prescriptions WHERE id=? AND id_client=?", (pid, client["id"])).fetchone()
        if not p: continue
        conn.execute("UPDATE prescriptions SET applique='Oui', date_reelle=?, dose_reelle=?, observations=? WHERE id=?",
            (date_str, j.get("dose_reelle",""), j.get("observations",""), pid))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": "Traitement validé"})
@app.route('/api/portail/<token>/bulletin-pdf')
def portail_bulletin_pdf(token):
    """Téléchargement du dernier bulletin PDF"""
    conn = get_db()
    client = dict_from_row(conn.execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: conn.close(); return jsonify({"error": "Token invalide"}), 404
    cid = client["id"]
    prescriptions = dicts_from_rows(conn.execute("SELECT * FROM prescriptions WHERE id_client=? ORDER BY id", (cid,)).fetchall())
    av = dict_from_row(conn.execute("SELECT * FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()) or {}
    suivi = dict_from_row(conn.execute("SELECT * FROM suivi WHERE id_client=? ORDER BY date_visite DESC LIMIT 1", (cid,)).fetchone())
    conn.close()
    meteo = fetch_meteo_for_client(client)
    doc = _build_bulletin(client, av, prescriptions, suivi, meteo)
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    pdf_buf = docx_to_pdf(buf)
    safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
    if pdf_buf:
        return send_file(pdf_buf, as_attachment=True, download_name=f"Bulletin_{safe}.pdf", mimetype="application/pdf")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"Bulletin_{safe}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


# ===== MODULE MATURITÉ =====

PEPINS_VALS  = {"Vert": 5, "Partiel": 10, "Brun": 15}
PULPE_VALS   = {"Végétale": 5, "Acidulée": 10, "Fruitée": 15, "Surmature": 20}
TANINS_VALS  = {"Astringents": 0, "Equilibrés": 5, "Légers": 10, "Fondus": 15}

def _score_degre(d):
    """< 8.5 → 0 pts | >= 8.5 → +1pt/0.1% | opti=20 | max=30"""
    if d is None: return None, None, None
    d = float(d)
    if d < 8.5: return 0, 0, "Pas vendangeable"
    pts = min(30, round((d - 8.4) / 0.1))
    opti = min(pts, 20)
    if pts >= 30: label = "Surmaturité"
    elif pts >= 20: label = "Optimum"
    elif pts >= 10: label = "Maturité techno"
    else: label = "En cours"
    return pts, opti, label

def _score_ratio(d, at):
    """Ratio S/AT = (degré×16.83)/AT | <20→0 | 20-30→2pts/unité | >30→20 | opti=10"""
    if d is None or at is None or float(at) <= 0: return None, None, None, None
    d, at = float(d), float(at)
    ratio = (d * 16.83) / at
    if ratio < 20: pts = 0; label = "Trop acide"
    elif ratio <= 30: pts = min(20, round((ratio - 20) * 2)); label = "Equilibre"
    else: pts = 20; label = "Chute acidité"
    return round(ratio, 2), pts, min(pts, 10), label

def _alerte_sanitaire(san, d):
    san = int(san or 0); d_val = float(d) if d else None
    if san <= 0: return None
    if san < 5: return "surveiller"
    if 5 <= san <= 10:
        if d_val is not None and d_val < 9.5:
            return "anti-botrytis curatif — vérifier AMM et respecter DAR"
        return "anti-botrytis biocontrôle ou anticiper la vendange"
    return "anti-botrytis urgent ou vendangez sans attendre"

def calc_score_matu(d):
    """
    Nouveau scoring validé — retourne (score, verdict, niveau, alertes).
    Degré : +1pt/0.1% depuis 8.5%, opti 20pts, max 30pts.
    Ratio S/AT : remplace score AT seul, opti 10pts.
    Pépins /15, Pulpe /15 (opti 10), Tanins /5.
    Maturité phénologique = pépins>=10 ET pulpe>=10.
    Sanitaire : règles décisionnelles sans impact sur le score.
    """
    degre = d.get("degre") or d.get("degre_probable")
    at    = d.get("at") or d.get("AT") or None
    san   = int(d.get("etat_sanitaire") or 0)
    pepins  = d.get("couleur_pepins")
    pulpe   = d.get("saveur_pulpe")
    tanins  = d.get("tanins")

    # Scores
    pts_d, opti_d, label_d = _score_degre(degre)
    ratio, pts_r, opti_r, label_r = _score_ratio(degre, at)
    pts_p = PEPINS_VALS.get(pepins); opti_p = min(pts_p, 15) if pts_p is not None else None
    pts_s = PULPE_VALS.get(pulpe);   opti_s = min(pts_s, 15) if pts_s is not None else None
    pts_t = TANINS_VALS.get(tanins); opti_t = min(pts_t, 5)  if pts_t is not None else None

    total = 0; max_total = 0
    if opti_d is not None: total += opti_d; max_total += 20
    if opti_r is not None: total += opti_r; max_total += 10
    if opti_p is not None: total += opti_p; max_total += 15
    if opti_s is not None: total += opti_s; max_total += 15
    if opti_t is not None: total += opti_t; max_total += 5
    score = round(total / max_total * 100) if max_total > 0 else 0

    techno  = (pts_d or 0) >= 10
    phenolo = (pts_p or 0) >= 10 and (pts_s or 0) >= 10
    alerte_san = _alerte_sanitaire(san, degre)

    # Verdict
    if alerte_san and "urgent" in alerte_san: verdict = "Anti-botrytis urgent ou vendangez"; niveau = 3
    elif pts_d == 0: verdict = "Pas vendangeable"; niveau = 0
    elif score < 35: verdict = "Trop tôt"; niveau = 0
    elif alerte_san and "biocontrôle" in alerte_san: verdict = "Biocontrôle ou anticiper vendange"; niveau = 2
    elif alerte_san and "curatif" in alerte_san: verdict = "Anti-botrytis curatif — vérifier AMM"; niveau = 1
    elif score < 55: verdict = "Surveiller"; niveau = 1
    elif techno and phenolo: verdict = "Maturité phénologique"; niveau = 2
    elif techno and not phenolo: verdict = "Maturité techno — attendre phénologique"; niveau = 1
    else: verdict = "En progression"; niveau = 1

    alertes = []
    if alerte_san: alertes.append(alerte_san)
    if techno and phenolo: alertes.append("Maturité phénologique atteinte — pépins et pulpe au vert")
    elif techno and not phenolo and (pts_p is not None or pts_s is not None):
        alertes.append("Maturité technologique atteinte — attendre maturité phénologique")

    return score, verdict, niveau, alertes

# --- Parcelles ---
@app.route('/api/parcelles', methods=['GET'])
def get_parcelles():
    cid = request.args.get('client_id')
    conn = get_db()
    if cid:
        rows = dicts_from_rows(conn.execute("SELECT * FROM parcelles WHERE id_client=? ORDER BY nom", (cid,)).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("SELECT * FROM parcelles ORDER BY id_client, nom").fetchall())
    conn.close()
    return jsonify(rows)

@app.route('/api/parcelles', methods=['POST'])
def add_parcelle():
    d = request.json
    conn = get_db()
    cur = conn.execute("""INSERT INTO parcelles (id_client,nom,lieu_dit,cepage,surface_cadastrale,nb_pieds_ha,commune,notes)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d['id_client'], d['nom'], d.get('lieu_dit'), _normaliser_cepage(d.get('cepage')),
         d.get('surface_cadastrale'), d.get('nb_pieds_ha'), d.get('commune'), d.get('notes')))
    conn.commit(); conn.close()
    return jsonify({"id": cur.lastrowid})

@app.route('/api/parcelles/<int:pid>', methods=['PUT'])
def update_parcelle(pid):
    d = request.json
    conn = get_db()
    conn.execute("""UPDATE parcelles SET nom=?,lieu_dit=?,cepage=?,surface_cadastrale=?,nb_pieds_ha=?,commune=?,notes=? WHERE id=?""",
        (d['nom'], d.get('lieu_dit'), _normaliser_cepage(d.get('cepage')),
         d.get('surface_cadastrale'), d.get('nb_pieds_ha'), d.get('commune'), d.get('notes'), pid))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/parcelles/<int:pid>', methods=['DELETE'])
def delete_parcelle(pid):
    conn = get_db()
    conn.execute("DELETE FROM maturite_fiches WHERE id_parcelle=?", (pid,))
    conn.execute("DELETE FROM parcelles WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# --- Fiches maturité ---
@app.route('/api/maturite', methods=['GET'])
def get_maturite():
    cid = request.args.get('client_id')
    pid = request.args.get('parcelle_id')
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    if pid:
        rows = dicts_from_rows(conn.execute("SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id WHERE f.id_parcelle=? AND f.campagne=? ORDER BY f.date_fiche", (pid, campagne)).fetchall())
    elif cid:
        rows = dicts_from_rows(conn.execute("SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id WHERE f.id_client=? AND f.campagne=? ORDER BY p.nom,f.date_fiche", (cid, campagne)).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("SELECT f.*,p.nom as parcelle_nom,p.cepage,c.exploitation FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id JOIN clients c ON f.id_client=c.id WHERE f.campagne=? ORDER BY f.score_total DESC", (campagne,)).fetchall())
    conn.close()
    return jsonify(rows)


def _insert_fiche(conn, d, client_id):
    """Insère une fiche maturité avec le nouveau scoring."""
    score, verdict, niveau, alertes = calc_score_matu(d)
    ratio, _, _, _ = _score_ratio(d.get("degre") or d.get("degre_probable"), d.get("at") or d.get("AT"))
    _, opti_d, _ = _score_degre(d.get("degre") or d.get("degre_probable"))
    pts_p = PEPINS_VALS.get(d.get("couleur_pepins"))
    pts_s = PULPE_VALS.get(d.get("saveur_pulpe"))
    pts_t = TANINS_VALS.get(d.get("tanins"))
    alerte_san = _alerte_sanitaire(d.get("etat_sanitaire") or 0, d.get("degre") or d.get("degre_probable"))
    cur = conn.execute("""INSERT INTO maturite_fiches
        (id_parcelle,id_client,campagne,date_fiche,degre_probable,AT,
         couleur_pepins,saveur_pulpe,tanins,etat_sanitaire,
         ratio_sat,score_degre,score_ratio,score_pepins,score_pulpe,score_tanins,
         score_total,verdict,alerte_sanitaire,reco_niveau,observations,saisie_par,est_exemple)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d['id_parcelle'], client_id, d.get('campagne','2026'),
         d.get('date_fiche') or __import__('datetime').date.today().isoformat(),
         d.get('degre') or d.get('degre_probable') or None,
         d.get('at') or d.get('AT') or None,
         d.get('couleur_pepins'), d.get('saveur_pulpe'), d.get('tanins'),
         int(d.get('etat_sanitaire') or 0),
         ratio, opti_d,
         min(PEPINS_VALS.get(d.get('couleur_pepins'),0) if d.get('couleur_pepins') else 0,10) if d.get('couleur_pepins') else None,
         min(pts_p,15) if pts_p is not None else None,
         min(pts_s,15) if pts_s is not None else None,
         min(pts_t,5) if pts_t is not None else None,
         score, verdict, alerte_san, niveau,
         d.get('observations'), d.get('saisie_par','client'), int(d.get('est_exemple') or 0)))
    return cur.lastrowid, score, verdict, niveau, alertes

@app.route('/api/maturite/calcul-score', methods=['POST'])
def calcul_score_ajax():
    """Score AJAX en temps réel — nouveau scoring validé"""
    d = request.json or {}
    score, verdict, niveau, alertes = calc_score_matu(d)
    ratio, pts_r, opti_r, label_r = _score_ratio(d.get("degre") or d.get("degre_probable"), d.get("at") or d.get("AT"))
    _, opti_d, label_d = _score_degre(d.get("degre") or d.get("degre_probable"))
    pts_p = PEPINS_VALS.get(d.get("couleur_pepins"))
    pts_s = PULPE_VALS.get(d.get("saveur_pulpe"))
    pts_t = TANINS_VALS.get(d.get("tanins"))
    techno  = (opti_d or 0) >= 10
    phenolo = (pts_p or 0) >= 10 and (pts_s or 0) >= 10
    return jsonify({
        "score_total": score, "verdict": verdict, "niveau": niveau, "alertes": alertes,
        "ratio_sat": ratio, "label_ratio": label_r, "label_degre": label_d,
        "score_degre": opti_d, "score_ratio": opti_r,
        "score_pepins": min(pts_p,15) if pts_p is not None else None,
        "score_pulpe":  min(pts_s,15) if pts_s is not None else None,
        "score_tanins": min(pts_t,5)  if pts_t is not None else None,
        "techno": techno, "phenolo": phenolo,
        "sans_at": not (d.get("at") or d.get("AT")),
        "alerte_sanitaire": _alerte_sanitaire(d.get("etat_sanitaire") or 0, d.get("degre") or d.get("degre_probable"))
    })

@app.route('/api/maturite', methods=['POST'])
def add_maturite():
    d = request.json
    conn = get_db()
    fid, score, verdict, niveau, alertes = _insert_fiche(conn, d, d['id_client'])
    conn.commit(); conn.close()
    return jsonify({"id": fid, "score_total": score, "verdict": verdict, "niveau": niveau, "alertes": alertes})

@app.route('/api/maturite/<int:fid>', methods=['DELETE'])
def delete_maturite(fid):
    conn = get_db()
    conn.execute("DELETE FROM maturite_fiches WHERE id=?", (fid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# --- Export itinéraire ---
@app.route('/api/maturite/export-csv')
def export_maturite_csv():
    campagne = request.args.get('campagne', '2026')
    cid = request.args.get('client_id')
    conn = get_db()
    if cid:
        rows = dicts_from_rows(conn.execute("""SELECT c.exploitation,p.nom,p.cepage,p.commune,
            f.date_fiche,f.degre_probable,f.AT,f.rendement_kgha,f.score_total,f.recommandation,f.observations
            FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id JOIN clients c ON f.id_client=c.id
            WHERE f.id_client=? AND f.campagne=? ORDER BY f.score_total DESC""", (cid, campagne)).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("""SELECT c.exploitation,p.nom,p.cepage,p.commune,
            f.date_fiche,f.degre_probable,f.AT,f.rendement_kgha,f.score_total,f.recommandation,f.observations
            FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id JOIN clients c ON f.id_client=c.id
            WHERE f.campagne=? ORDER BY f.score_total DESC""", (campagne,)).fetchall())
    conn.close()
    import csv, io as sio
    buf = sio.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    buf.seek(0)
    return buf.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f"attachment; filename=Itineraire_Maturite_{campagne}.csv"}

# --- Routes portail maturité (token) ---
@app.route('/api/portail/<token>/parcelles', methods=['GET'])
def portail_parcelles(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return jsonify(_parcelles_avec_etat_recolte(client['id']))

@app.route('/api/portail/<token>/parcelles', methods=['POST'])
def portail_add_parcelle(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    d = request.json; d['id_client'] = client['id']
    return add_parcelle()

@app.route('/api/portail/<token>/maturite', methods=['GET'])
def portail_get_maturite(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    pid = request.args.get('parcelle_id')
    conn = get_db()
    if pid:
        rows = dicts_from_rows(conn.execute("SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id WHERE f.id_parcelle=? AND f.id_client=? ORDER BY f.date_fiche", (pid, client['id'])).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id WHERE f.id_client=? ORDER BY p.nom,f.date_fiche", (client['id'],)).fetchall())
    conn.close()
    return jsonify(_enrichir_fiches(rows))

@app.route('/api/portail/<token>/maturite', methods=['POST'])
def portail_add_maturite(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    d = request.json or {}
    d['id_client'] = client['id']; d['saisie_par'] = 'client'
    request._cached_json = (d, d)
    return add_maturite()

# Routes slug maturité
@app.route('/api/portail-s/<slug>/parcelles', methods=['GET'])
def portail_parcelles_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return jsonify(_parcelles_avec_etat_recolte(client['id']))

@app.route('/api/portail-s/<slug>/parcelles', methods=['POST'])
def portail_add_parcelle_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    d = request.json; d['id_client'] = client['id']
    conn = get_db()
    ecart_rangs = d.get('ecart_rangs')
    ecart_ceps  = d.get('ecart_ceps')
    nb_pieds_ha = d.get('nb_pieds_ha')
    if ecart_rangs and ecart_ceps and not nb_pieds_ha:
        try:
            nb_pieds_ha = round(10000 / (float(ecart_rangs) * float(ecart_ceps)))
        except: pass
    # Surface cadastrale en ares → convertir en ha pour le stockage
    surf_cad_ares = d.get('surface_cadastrale')
    surf_cad_ha = round(float(surf_cad_ares) / 100, 4) if surf_cad_ares else None
    # Commune obligatoirement en majuscules
    commune = (d.get('commune') or '').strip().upper() or None
    cur = conn.execute("""INSERT INTO parcelles
        (id_client,nom,lieu_dit,cepage,surface_cadastrale,
         nb_pieds_ha,commune,notes,ecart_rangs,ecart_ceps)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d['id_client'], d['nom'], d.get('lieu_dit'), d.get('cepage'),
         surf_cad_ha,
         nb_pieds_ha, commune, d.get('notes'),
         ecart_rangs, ecart_ceps))
    conn.commit(); conn.close()
    return jsonify({
        "id": cur.lastrowid,
        "nb_pieds_ha": nb_pieds_ha,
        "nb_pieds_ha_calcule": bool(ecart_rangs and ecart_ceps and not d.get('nb_pieds_ha'))
    })


# ── Import parcelles depuis la Fiche Vendange (Comité Champagne, PDF) ────────

@app.route('/api/portail-s/<slug>/parcelles/import-pdf', methods=['POST'])
def portail_parcelles_import_pdf(slug):
    """Analyse une fiche vendange PDF et renvoie un aperçu des parcelles
    détectées (fusionnées par lieu-dit + cépage), sans rien écrire en base.
    Chaque parcelle est signalée 'deja_existante' si une parcelle avec la
    même commune + lieu-dit + cépage existe déjà pour ce client."""
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    if parser_fiche_vendange is None:
        return jsonify({"error": "pdfplumber n'est pas installé sur le serveur"}), 500
    if 'file' not in request.files:
        return jsonify({"error": "Pas de fichier"}), 400
    f = request.files['file']
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    tmp_path = tmp.name
    tmp.close()
    f.save(tmp_path)
    try:
        parcelles = parser_fiche_vendange(tmp_path)
    except Exception as e:
        return jsonify({"error": f"Lecture PDF impossible : {e}"}), 400
    finally:
        try: os.unlink(tmp_path)
        except: pass

    # Deux parcelles peuvent partager le même lieu-dit + cépage + commune tout
    # en étant physiquement distinctes (ex : deux "LES CLOS" issus de baux
    # différents). Les références cadastrales (uniques par construction) sont
    # donc incluses dans la clé d'identité pour éviter les faux doublons.
    conn = get_db()
    existantes = conn.execute(
        "SELECT commune, lieu_dit, cepage, refs_cadastrales FROM parcelles WHERE id_client=?", (client['id'],)
    ).fetchall()
    conn.close()
    cles_existantes = {
        ((r['commune'] or '').strip().upper(), (r['lieu_dit'] or '').strip().upper(),
         (r['cepage'] or '').strip().upper(), (r['refs_cadastrales'] or '').strip().upper())
        for r in existantes
    }
    # Repère les groupes commune+lieu-dit+cépage qui apparaissent plusieurs
    # fois dans la fiche (parcelles distinctes de même nom) pour proposer un
    # nom d'affichage désambiguïsé (référence cadastrale en suffixe).
    from collections import Counter
    groupes = Counter((p['commune'], p['lieu_dit'], p['cepage']) for p in parcelles)
    for p in parcelles:
        refs_key = ','.join(p.get('refs_cadastrales') or [])
        cle = ((p['commune'] or '').strip().upper(), (p['lieu_dit'] or '').strip().upper(),
               (p['cepage'] or '').strip().upper(), refs_key.strip().upper())
        p['deja_existante'] = cle in cles_existantes
        if groupes[(p['commune'], p['lieu_dit'], p['cepage'])] > 1 and p.get('refs_cadastrales'):
            p['nom'] = f"{p['lieu_dit']} ({p['refs_cadastrales'][0]})"

    return jsonify({
        "parcelles": parcelles,
        "nb_total": len(parcelles),
        "surface_totale_ha": round(sum(p['surface_ha'] for p in parcelles), 4),
        "nb_deja_existantes": sum(1 for p in parcelles if p['deja_existante']),
    })


@app.route('/api/portail-s/<slug>/parcelles/import-confirmer', methods=['POST'])
def portail_parcelles_import_confirmer(slug):
    """Insère en base la liste de parcelles validée par l'utilisateur
    (issue de l'aperçu /import-pdf, potentiellement éditée côté client).
    Ignore silencieusement les parcelles dont la clé commune+lieu-dit+cépage
    existe déjà, pour rester idempotent si l'utilisateur relance l'import."""
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    body = request.get_json() or {}
    parcelles = body.get('parcelles', [])
    if not isinstance(parcelles, list) or not parcelles:
        return jsonify({"error": "Aucune parcelle à importer"}), 400

    conn = get_db()
    existantes = conn.execute(
        "SELECT commune, lieu_dit, cepage, refs_cadastrales FROM parcelles WHERE id_client=?", (client['id'],)
    ).fetchall()
    cles_existantes = {
        ((r['commune'] or '').strip().upper(), (r['lieu_dit'] or '').strip().upper(),
         (r['cepage'] or '').strip().upper(), (r['refs_cadastrales'] or '').strip().upper())
        for r in existantes
    }

    inserees, ignorees = 0, 0
    for p in parcelles:
        commune = (p.get('commune') or '').strip().upper() or None
        lieu_dit = (p.get('lieu_dit') or p.get('nom') or '').strip() or None
        cepage = (p.get('cepage') or '').strip() or None
        nom = (p.get('nom') or lieu_dit or '').strip()
        refs = p.get('refs_cadastrales') or []
        refs_key = ','.join(refs)
        cle = ((commune or ''), (lieu_dit or '').upper(), (cepage or '').upper(), refs_key.strip().upper())
        if cle in cles_existantes or not nom:
            ignorees += 1
            continue
        ecart_rangs = p.get('ecart_rangs')
        ecart_ceps = p.get('ecart_ceps')
        nb_pieds_ha = p.get('nb_pieds_ha')
        if ecart_rangs and ecart_ceps and not nb_pieds_ha:
            try: nb_pieds_ha = round(10000 / (float(ecart_rangs) * float(ecart_ceps)))
            except: pass
        surface_ha = p.get('surface_ha')
        notes = ('Import fiche vendange · réf. cadastrales : ' + ', '.join(refs)) if refs else 'Import fiche vendange'
        conn.execute("""INSERT INTO parcelles
            (id_client,nom,lieu_dit,cepage,surface_cadastrale,nb_pieds_ha,commune,notes,ecart_rangs,ecart_ceps,refs_cadastrales)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (client['id'], nom, lieu_dit, cepage, surface_ha, nb_pieds_ha, commune, notes, ecart_rangs, ecart_ceps, refs_key))
        cles_existantes.add(cle)
        inserees += 1
    conn.commit(); conn.close()
    return jsonify({"ok": True, "inserees": inserees, "ignorees": ignorees})


@app.route('/api/portail-s/<slug>/parcelles/fusionner', methods=['POST'])
def portail_parcelles_fusionner(slug):
    """Fusionne plusieurs parcelles déjà enregistrées en une seule.
    La première parcelle de la liste `ids` (convention : la plus grande
    surface, triée côté client) est conservée comme 'survivante' — ses
    champs sont écrasés par les valeurs fusionnées — et reçoit les fiches
    de maturité / relevés de rendement des parcelles supprimées, afin de ne
    perdre aucun historique."""
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    d = request.get_json() or {}
    ids = d.get('ids') or []
    if not isinstance(ids, list) or len(ids) < 2:
        return jsonify({"error": "Il faut au moins 2 parcelles à fusionner"}), 400
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "Identifiants invalides"}), 400

    conn = get_db()
    placeholders = ','.join('?' * len(ids))
    trouvees = conn.execute(
        f"SELECT id FROM parcelles WHERE id IN ({placeholders}) AND id_client=?",
        (*ids, client['id'])
    ).fetchall()
    if len(trouvees) != len(ids):
        conn.close()
        return jsonify({"error": "Une ou plusieurs parcelles n'appartiennent pas à ce compte"}), 400

    survivor_id = ids[0]
    autres_ids = ids[1:]

    ecart_rangs = d.get('ecart_rangs')
    ecart_ceps = d.get('ecart_ceps')
    nb_pieds_ha = d.get('nb_pieds_ha')
    if ecart_rangs and ecart_ceps and not nb_pieds_ha:
        try: nb_pieds_ha = round(10000 / (float(ecart_rangs) * float(ecart_ceps)))
        except: pass
    commune = (d.get('commune') or '').strip().upper() or None

    conn.execute("""UPDATE parcelles SET nom=?, lieu_dit=?, cepage=?, surface_cadastrale=?,
        nb_pieds_ha=?, ecart_rangs=?, ecart_ceps=?, commune=?, notes=?
        WHERE id=? AND id_client=?""",
        (d.get('nom'), d.get('lieu_dit'), d.get('cepage'), d.get('surface_cadastrale'),
         nb_pieds_ha, ecart_rangs, ecart_ceps, commune,
         d.get('notes') or 'Fusion de parcelles',
         survivor_id, client['id']))

    ph_autres = ','.join('?' * len(autres_ids))
    cur_m = conn.execute(
        f"UPDATE maturite_fiches SET id_parcelle=? WHERE id_parcelle IN ({ph_autres}) AND id_client=?",
        (survivor_id, *autres_ids, client['id']))
    cur_r = conn.execute(
        f"UPDATE rendements SET id_parcelle=? WHERE id_parcelle IN ({ph_autres}) AND id_client=?",
        (survivor_id, *autres_ids, client['id']))
    conn.execute(
        f"DELETE FROM parcelles WHERE id IN ({ph_autres}) AND id_client=?",
        (*autres_ids, client['id']))
    conn.commit()
    maturites_reassignees = cur_m.rowcount
    rendements_reassignes = cur_r.rowcount
    conn.close()
    return jsonify({
        "ok": True, "survivor_id": survivor_id, "supprimees": autres_ids,
        "maturites_reassignees": maturites_reassignees,
        "rendements_reassignes": rendements_reassignes,
    })


@app.route('/api/admin/fix-communes', methods=['GET', 'POST'])
def admin_fix_communes():
    """Passe toutes les communes en majuscules dans la BDD."""
    conn = get_db()
    conn.execute("UPDATE parcelles SET commune = UPPER(TRIM(commune)) WHERE commune IS NOT NULL")
    conn.execute("UPDATE parcelles SET nom = UPPER(TRIM(nom)) WHERE nom IS NOT NULL")
    conn.execute("UPDATE clients SET commune = UPPER(TRIM(commune)) WHERE commune IS NOT NULL")
    conn.commit()
    n_parc = conn.execute("SELECT COUNT(*) FROM parcelles").fetchone()[0]
    conn.close()
    return jsonify({"ok": True, "message": f"Communes et noms de parcelles convertis en majuscules ({n_parc} parcelles)"})


@app.route('/api/admin/fix-cepages', methods=['GET', 'POST'])
def admin_fix_cepages():
    """Nettoie rétroactivement les cépages déjà enregistrés — retire les codes
    couleur officiels ('MEUNIER N', 'CHARDONNAY B', 'PINOT NOIR N'...) et fait
    correspondre à l'orthographe standard, pour fusionner ce qui était compté
    comme des cépages distincts par erreur (ex. dans les dates cibles par cépage)."""
    conn = get_db()
    rows = conn.execute("SELECT id, cepage FROM parcelles WHERE cepage IS NOT NULL AND cepage != ''").fetchall()
    n_modifiees = 0
    for r in rows:
        nettoye = _normaliser_cepage(r['cepage'])
        if nettoye != r['cepage']:
            conn.execute("UPDATE parcelles SET cepage=? WHERE id=?", (nettoye, r['id']))
            n_modifiees += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": f"{n_modifiees} parcelle(s) sur {len(rows)} corrigée(s)."})


@app.route('/api/admin/reset-rendements-reels', methods=['POST'])
def admin_reset_rendements_reels():
    """Efface tous les rendements réels et kg cumulés — admin uniquement."""
    j = request.json or {}
    cid = j.get('id_client')
    conn = get_db()
    if cid:
        conn.execute("UPDATE rendements SET rendement_reel_kgha=NULL, kg_recoltes_total=0, recolte_complete=0 WHERE id_client=?", (cid,))
    else:
        conn.execute("UPDATE rendements SET rendement_reel_kgha=NULL, kg_recoltes_total=0, recolte_complete=0")
    n = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit(); conn.close()
    return jsonify({"ok": True, "updated": n})


@app.route('/api/admin/portails')
def admin_portails():
    conn = get_db()
    clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY exploitation").fetchall())
    stats_map = {}
    for s in dicts_from_rows(conn.execute("""
        SELECT f.id_client, MAX(f.date_fiche) as date_dernier,
               ROUND(AVG(f.degre_probable),2) as degre_moyen,
               ROUND(AVG(f.score_total),0) as score_moyen,
               GROUP_CONCAT(DISTINCT f.alerte_sanitaire) as alertes
        FROM maturite_fiches f
        WHERE f.campagne='2026'
          AND f.id=(SELECT MAX(f2.id) FROM maturite_fiches f2
                    WHERE f2.id_parcelle=f.id_parcelle AND f2.id_client=f.id_client AND f2.campagne='2026')
        GROUP BY f.id_client
    """).fetchall()):
        alertes=[a for a in (s['alertes'] or '').split(',') if a and a!='None']
        stats_map[s['id_client']]={'date':s['date_dernier'],'degre_moyen':s['degre_moyen'],
            'score_moyen':int(s['score_moyen']) if s['score_moyen'] else None,
            'alertes':alertes,'urgent':any('urgent' in a for a in alertes)}
    nb_parc={r['id_client']:r['nb'] for r in dicts_from_rows(conn.execute(
        "SELECT id_client,COUNT(DISTINCT id_parcelle) as nb FROM maturite_fiches WHERE campagne='2026' GROUP BY id_client").fetchall())}
    conn.close()
    result=[]
    for c in clients:
        cid=c['id']; m=stats_map.get(cid); score=m['score_moyen'] if m else -1
        result.append({"id":cid,"exploitation":c['exploitation'],"certification":c.get('certification',''),
            "portail_url":f"/portail-s/{c.get('slug') or c.get('portail_token') or cid}",
            "nb_parcelles":nb_parc.get(cid,0),"maturite":m,
            "_urgence":(0 if (m and m['urgent']) else 1 if score<40 else 2,-score)})
    result.sort(key=lambda x:x['_urgence'])
    for r in result: del r['_urgence']
    return jsonify(result)

@app.route('/api/admin/portails-phyto')
def admin_portails_phyto():
    conn = get_db()
    clients = dicts_from_rows(conn.execute("SELECT * FROM clients ORDER BY exploitation").fetchall())
    derniers = dicts_from_rows(conn.execute("""
        SELECT id_client,passage,MAX(COALESCE(date_prevue,date_reelle,'')) as date_max
        FROM prescriptions WHERE passage IS NOT NULL AND passage!=''
        GROUP BY id_client,passage ORDER BY id_client,date_max DESC
    """).fetchall())
    dernier_par_client={}
    for r in derniers:
        if r['id_client'] not in dernier_par_client: dernier_par_client[r['id_client']]=r['passage']
    produits_map={}
    for p in dicts_from_rows(conn.execute(
        "SELECT id_client,passage,nom_produit,substance_active,dose_prescrite,date_prevue,date_reelle,applique,cible FROM prescriptions WHERE nom_produit IS NOT NULL AND nom_produit!='' ORDER BY id_client,id ASC").fetchall()):
        cid=p['id_client']
        if dernier_par_client.get(cid)!=p['passage']: continue
        if cid not in produits_map:
            produits_map[cid]={"passage":p['passage'],"date":p['date_reelle'] or p['date_prevue'],"applique":p['applique'],"produits":[]}
        produits_map[cid]['produits'].append({"nom":p['nom_produit'],"sa":p['substance_active'],"dose":p['dose_prescrite'],"cible":p['cible'],"date_reelle":p['date_reelle']})
    conn.close()
    return jsonify([{"id":c['id'],"exploitation":c['exploitation'],"certification":c.get('certification',''),
        "portail_url":f"/portail-s/{c.get('slug') or c.get('portail_token') or c['id']}","phyto":produits_map.get(c['id'])}
        for c in clients])


def _delete_parcelle(client_id, pid, cascade=True):
    """Supprime une parcelle et optionnellement ses fiches et relevés."""
    conn = get_db()
    p = dict_from_row(conn.execute(
        "SELECT * FROM parcelles WHERE id=? AND id_client=?", (pid, client_id)
    ).fetchone())
    if not p: conn.close(); return None
    if cascade:
        conn.execute("DELETE FROM maturite_fiches WHERE id_parcelle=? AND id_client=?", (pid, client_id))
        conn.execute("DELETE FROM rendements WHERE id_parcelle=? AND id_client=?", (pid, client_id))
    conn.execute("DELETE FROM parcelles WHERE id=? AND id_client=?", (pid, client_id))
    conn.commit(); conn.close()
    return p

@app.route('/api/portail-s/<slug>/parcelles/<int:pid>', methods=['DELETE'])
def portail_delete_parcelle_slug(slug, pid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    cascade = request.args.get('cascade', 'true').lower() == 'true'
    p = _delete_parcelle(client['id'], pid, cascade)
    if not p: return jsonify({"error": "Parcelle introuvable"}), 404
    return jsonify({"ok": True, "nom": p['nom']})

@app.route('/api/portail-s/<slug>/parcelles/<int:pid>', methods=['PUT'])
def portail_update_parcelle_slug(slug, pid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return _update_parcelle(client, pid)

@app.route('/api/portail/<token>/parcelles/<int:pid>', methods=['DELETE'])
def portail_delete_parcelle_token(token, pid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    p = _delete_parcelle(client['id'], pid)
    if not p: return jsonify({"error": "Parcelle introuvable"}), 404
    return jsonify({"ok": True, "nom": p['nom']})

@app.route('/api/portail/<token>/parcelles/<int:pid>', methods=['PUT'])
def portail_update_parcelle_token(token, pid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return _update_parcelle(client, pid)

def _update_parcelle(client, pid):
    client_id = client['id']
    d = request.json or {}
    conn = get_db()
    p = dict_from_row(conn.execute("SELECT * FROM parcelles WHERE id=? AND id_client=?", (pid, client_id)).fetchone())
    if not p: conn.close(); return jsonify({"error": "Parcelle introuvable"}), 404

    # Une parcelle d'exemple qu'on modifie devient une vraie parcelle (elle ne sera plus
    # effacée par "Vider les données d'exemple", et perd son badge "exemple").
    devient_reelle = bool(p.get('est_exemple'))

    # Calcul densité si écartements fournis
    ecart_rangs = d.get('ecart_rangs') or p.get('ecart_rangs')
    ecart_ceps  = d.get('ecart_ceps')  or p.get('ecart_ceps')
    nb_pieds_ha = d.get('nb_pieds_ha') or p.get('nb_pieds_ha')
    if ecart_rangs and ecart_ceps:
        try: nb_pieds_ha = round(10000 / (float(ecart_rangs) * float(ecart_ceps)))
        except: pass
    surf_cad = d.get('surface_cadastrale')
    surf_cad_ha = round(float(surf_cad)/100, 4) if surf_cad else p.get('surface_cadastrale')
    commune = (d.get('commune') or p.get('commune') or '').strip().upper() or None
    conn.execute("""UPDATE parcelles SET
        nom=?, cepage=?, commune=?, surface_cadastrale=?,
        nb_pieds_ha=?, ecart_rangs=?, ecart_ceps=?, notes=?, est_exemple=?
        WHERE id=? AND id_client=?""",
        (d.get('nom') or p['nom'],
         _normaliser_cepage(d.get('cepage')) or p.get('cepage'),
         commune, surf_cad_ha, nb_pieds_ha,
         ecart_rangs, ecart_ceps,
         d.get('notes') or p.get('notes'),
         0 if devient_reelle else p.get('est_exemple', 0),
         pid, client_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "nb_pieds_ha": nb_pieds_ha, "devenue_reelle": devient_reelle})



@app.route('/api/portail-s/<slug>/reset-campagne', methods=['POST'])
def portail_reset_campagne_slug(slug):
    """Archive puis remet à zéro les fiches maturité et relevés rendement."""
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return _archiver_et_reset_campagne(client)

@app.route('/api/portail/<token>/reset-campagne', methods=['POST'])
def portail_reset_campagne_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return _archiver_et_reset_campagne(client)

def _archiver_et_reset_campagne(client):
    from datetime import date
    j = request.json or {}
    campagne = j.get('campagne', '2026')
    mode = j.get('mode', 'export')  # 'export' = juste exporter, 'reset' = archiver + supprimer

    conn = get_db()
    cid = client['id']

    # Collecter toutes les données
    fiches = dicts_from_rows(conn.execute("""
        SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune
        FROM maturite_fiches f JOIN parcelles p ON f.id_parcelle=p.id
        WHERE f.id_client=? AND f.campagne=? ORDER BY p.commune, p.nom, f.date_fiche
    """, (cid, campagne)).fetchall())

    rendements = dicts_from_rows(conn.execute("""
        SELECT r.*, p.nom as parcelle_nom, p.cepage, p.commune, p.surface_cadastrale
        FROM rendements r JOIN parcelles p ON r.id_parcelle=p.id
        WHERE r.id_client=? AND r.campagne=? ORDER BY p.commune, p.nom, r.date_releve
    """, (cid, campagne)).fetchall())

    parcelles = dicts_from_rows(conn.execute(
        "SELECT * FROM parcelles WHERE id_client=? ORDER BY commune, nom", (cid,)
    ).fetchall())

    import io, csv as _csv

    # Supprimer si mode reset
    supprimes = {}
    if mode == 'reset':
        n_matu = conn.execute("DELETE FROM maturite_fiches WHERE id_client=? AND campagne=?",
                              (cid, campagne)).rowcount
        n_rdt  = conn.execute("DELETE FROM rendements WHERE id_client=? AND campagne=?",
                              (cid, campagne)).rowcount
        conn.commit()
        supprimes = {"maturite": n_matu, "rendements": n_rdt}
    conn.close()

    safe = client['exploitation'].replace(' ','_').replace('/','_')
    today_str = date.today().isoformat()

    # Construire CSV multi-sections
    buf = io.StringIO()
    writer = _csv.writer(buf, delimiter=';')

    # En-tête
    writer.writerow([f"Archive VITI Sens — {client['exploitation']} — Campagne {campagne} — {today_str}"])
    writer.writerow([])

    # Section maturité
    writer.writerow(["=== SUIVI MATURITÉ ==="])
    writer.writerow(["Commune","Parcelle","Cépage","Date relevé","Degré (%vol)","AT (g/L)",
                     "Pépins","Pulpe","Sanitaire (%)","Score","Verdict","Observations"])
    for f in fiches:
        PEPINS={5:'Vert',10:'Partiel',15:'Brun'}
        PULPE={5:'Végétale',10:'Acidulée',15:'Fruitée',20:'Surmature'}
        writer.writerow([
            f.get('commune',''), f.get('parcelle_nom',''), f.get('cepage',''),
            f.get('date_fiche',''), f.get('degre_probable',''), f.get('AT',''),
            PEPINS.get(f.get('couleur_pepins'),''), PULPE.get(f.get('saveur_pulpe'),''),
            f.get('etat_sanitaire',''), f.get('score_total',''), f.get('verdict',''),
            f.get('observations','')
        ])

    writer.writerow([])

    # Section rendements
    writer.writerow(["=== RELEVÉS RENDEMENT ==="])
    writer.writerow(["Commune","Parcelle","Cépage","Surface (a)","Date relevé",
                     "Grappes/pied","Poids moyen (g)","Nb pieds/ha",
                     "Rdt théorique (kg/ha)","Rdt réel (kg/ha)","Kg récoltés total","Récolte complète"])
    for r in rendements:
        surf = r.get('surface_cadastrale')
        surf_str = str(round(float(surf)*100,2)) if surf else ''
        writer.writerow([
            r.get('commune',''), r.get('parcelle_nom',''), r.get('cepage',''),
            surf_str, r.get('date_releve',''),
            r.get('nb_grappes_pied',''), r.get('poids_moyen_g',''), r.get('nb_pieds_ha',''),
            r.get('rendement_kgha',''), r.get('rendement_reel_kgha',''),
            r.get('kg_recoltes_total',''), 'Oui' if r.get('recolte_complete')==1 else 'Non'
        ])

    writer.writerow([])

    # Section parcelles
    writer.writerow(["=== PARCELLAIRE ==="])
    writer.writerow(["Commune","Nom","Cépage","Surface cadastrale (a)","Nb pieds/ha",
                     "Écart rangs (m)","Écart ceps (m)","Notes"])
    for p in parcelles:
        surf = p.get('surface_cadastrale')
        writer.writerow([
            p.get('commune',''), p.get('nom',''), p.get('cepage',''),
            str(round(float(surf)*100,2)) if surf else '',
            p.get('nb_pieds_ha',''), p.get('ecart_rangs',''), p.get('ecart_ceps',''),
            p.get('notes','')
        ])

    if mode == 'reset':
        writer.writerow([])
        writer.writerow([f"Supprimé : {supprimes.get('maturite',0)} fiche(s) maturité, {supprimes.get('rendements',0)} relevé(s) rendement"])

    csv_bytes = ('﻿' + buf.getvalue()).encode('utf-8')
    filename = f"Archive_{safe}_{campagne}_{today_str}.csv"
    return csv_bytes, 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f"attachment; filename={filename}",
        "X-Supprimes-Maturite": str(supprimes.get('maturite', 0)),
        "X-Supprimes-Rendements": str(supprimes.get('rendements', 0)),
    }


def _date_recolte_probable(degre, at, date_fiche, dyn_degre=0.15, dyn_at=-0.20):
    """
    Calcule la date de récolte probable depuis un relevé donné.
    Fenêtre cible : 10.2 → 10.5 % vol
    """
    from datetime import date, timedelta
    try:
        if degre is None or not date_fiche:
            return None
        degre = float(degre)
        d0 = date.fromisoformat(str(date_fiche)[:10])

        def jours_vers(cible):
            if dyn_degre <= 0: return None
            j = (cible - degre) / dyn_degre
            return round(j, 1) if j >= 0 else None

        j102 = jours_vers(10.2)
        j105 = jours_vers(10.5)
        date_102 = (d0 + timedelta(days=int(j102))).isoformat() if j102 is not None else None
        date_105 = (d0 + timedelta(days=int(j105))).isoformat() if j105 is not None else None
        at_val = float(at) if at else None
        at_102 = round(at_val + dyn_at * j102, 1) if at_val and j102 is not None else None
        at_105 = round(at_val + dyn_at * j105, 1) if at_val and j105 is not None else None

        if degre >= 10.5:
            resume = "Fenetre depassee"
        elif degre >= 10.2:
            resume = "Dans la fenetre de recolte"
        elif date_102:
            from datetime import date as dclass
            jours = (dclass.fromisoformat(date_102) - dclass.today()).days
            resume = "Recolte estimee dans " + str(max(0,jours)) + " j" if jours >= 0 else "Fenetre atteinte"
        else:
            resume = "Progression insuffisante"

        return {
            "date_102": date_102, "jours_102": j102,
            "date_105": date_105, "jours_105": j105,
            "at_102": at_102, "at_105": at_105,
            "resume": resume
        }
    except Exception as e:
        print(f"[date_recolte] erreur: {e}")
        return None

def _enrichir_fiches(fiches, dyn_degre=0.15, dyn_at=-0.20):
    """Ajoute date_recolte_probable à chaque fiche."""
    try:
        for f in fiches:
            f['date_recolte'] = _date_recolte_probable(
                f.get('degre_probable'), f.get('AT'),
                f.get('date_fiche'), dyn_degre, dyn_at
            )
    except Exception as e:
        print(f"[enrichir_fiches] erreur: {e}")
    return fiches


# ── Cuvées parcellaires ───────────────────────────────────────────────────────

@app.route('/api/portail-s/<slug>/cuvees-parcellaires', methods=['GET'])
def cuvees_parc_get(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    saison = request.args.get('saison', 2026, type=int)
    conn = get_db(); cursor = conn.cursor()
    cursor.execute('''
        SELECT id, nom, destination, date_recolte, parcelles_json, created_at
        FROM cuvees_parcellaires
        WHERE id_client=? AND saison=?
        ORDER BY id
    ''', (client['id'], saison))
    rows = []
    for r in cursor.fetchall():
        d = dict_from_row(r)
        try: d['parcelles'] = json.loads(d.pop('parcelles_json', '[]'))
        except: d['parcelles'] = []
        rows.append(d)
    conn.close()
    return jsonify(rows)

@app.route('/api/portail-s/<slug>/cuvees-parcellaires', methods=['POST'])
def cuvees_parc_save(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    body = request.get_json()
    nom          = (body.get('nom') or '').strip()
    destination  = (body.get('destination') or '').strip()
    date_recolte = (body.get('date_recolte') or '').strip() or None
    parcelles    = body.get('parcelles', [])
    saison       = body.get('saison', 2026)
    cuvee_id     = body.get('id')  # si présent → update
    if not nom: return jsonify({'error': 'Nom requis'}), 400
    parcelles_json = json.dumps(parcelles, ensure_ascii=False)
    conn = get_db(); cursor = conn.cursor()
    if cuvee_id:
        cursor.execute('''
            UPDATE cuvees_parcellaires
            SET nom=?, destination=?, date_recolte=?, parcelles_json=?
            WHERE id=? AND id_client=?
        ''', (nom, destination, date_recolte, parcelles_json, cuvee_id, client['id']))
    else:
        cursor.execute('''
            INSERT INTO cuvees_parcellaires (id_client, saison, nom, destination, date_recolte, parcelles_json)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (client['id'], saison, nom, destination, date_recolte, parcelles_json))
        cuvee_id = cursor.lastrowid
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': cuvee_id})

@app.route('/api/portail-s/<slug>/cuvees-parcellaires/<int:cid>', methods=['DELETE'])
def cuvees_parc_delete(slug, cid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    conn = get_db(); cursor = conn.cursor()
    cursor.execute('DELETE FROM cuvees_parcellaires WHERE id=? AND id_client=?', (cid, client['id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Réseau maturité Comité Champagne ─────────────────────────────────────────

PETITES_REGIONS = [
    'Barséquanais', 'Bar-sur-Aubois', 'Canton de Condé-en-Brie',
    'Côteaux du Petit Morin', 'Côte des Blancs', 'Est de Château-Thierry',
    'Grande Vallée de la Marne', 'Massif de St Thierry',
    'Ouest de Château-Thierry', 'Région de Bouzy Ambonnay',
    'Région de Chigny-les-Roses', "Région d'Ecueil", "Région d'Epernay",
    'Région de Sézanne', 'Région de Trépail-Nogent l\'Abbesse',
    'Région de Verzenay', 'Région de Vitry-le-François',
    'Vallée de la Marne (RD)', 'Vallée de la Marne (RG)', "Vallée de l'Ardre"
]

@app.route('/api/portail-s/<slug>/reseau-matu', methods=['GET'])
def reseau_matu_get(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    saison = request.args.get('saison', 2026, type=int)
    conn = get_db(); cursor = conn.cursor()
    cursor.execute('''
        SELECT id, petite_region, cepage, date_releve, degre_probable, dyn_degre, created_at
        FROM reseau_matu
        WHERE id_client=? AND saison=?
        ORDER BY cepage, date_releve
    ''', (client['id'], saison))
    rows = [dict_from_row(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/portail-s/<slug>/reseau-matu', methods=['POST'])
def reseau_matu_save(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    body = request.get_json()
    region   = body.get('petite_region','').strip()
    cepage   = body.get('cepage','').strip()
    date_rel = body.get('date_releve','').strip()
    degre    = body.get('degre_probable')
    saison   = body.get('saison', 2026)
    if not all([region, cepage, date_rel, degre is not None]):
        return jsonify({'error': 'Champs manquants'}), 400

    conn = get_db(); cursor = conn.cursor()

    # Calculer la dynamique si >= 2 relevés pour ce cépage
    cursor.execute('''
        SELECT date_releve, degre_probable FROM reseau_matu
        WHERE id_client=? AND saison=? AND cepage=? AND date_releve < ?
        ORDER BY date_releve DESC LIMIT 1
    ''', (client['id'], saison, cepage, date_rel))
    prev = cursor.fetchone()
    dyn = None
    if prev:
        from datetime import datetime
        d1 = datetime.fromisoformat(prev['date_releve'])
        d2 = datetime.fromisoformat(date_rel)
        nb_jours = (d2 - d1).days
        if nb_jours > 0:
            dyn = round((float(degre) - float(prev['degre_probable'])) / nb_jours, 4)

    try:
        cursor.execute('''
            INSERT INTO reseau_matu (id_client, saison, petite_region, cepage, date_releve, degre_probable, dyn_degre)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_client, saison, petite_region, cepage, date_releve)
            DO UPDATE SET degre_probable=excluded.degre_probable, dyn_degre=excluded.dyn_degre
        ''', (client['id'], saison, region, cepage, date_rel, float(degre), dyn))
        conn.commit()

        # Mettre à jour la dynamique du relevé précédent si besoin
        # et recalculer les suivants
        cursor.execute('''
            UPDATE reseau_matu SET dyn_degre=? WHERE id_client=? AND saison=? AND cepage=? AND date_releve=?
        ''', (dyn, client['id'], saison, cepage, date_rel))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

    conn.close()
    return jsonify({'ok': True, 'dyn_degre': dyn})

@app.route('/api/portail-s/<slug>/reseau-matu/<int:rid>', methods=['DELETE'])
def reseau_matu_delete(slug, rid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({'error': 'Client non trouvé'}), 404
    conn = get_db(); cursor = conn.cursor()
    cursor.execute('DELETE FROM reseau_matu WHERE id=? AND id_client=?', (rid, client['id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/portail-s/<slug>/reseau-matu/regions', methods=['GET'])
def reseau_matu_regions(slug):
    return jsonify(PETITES_REGIONS)

@app.route('/api/portail-s/<slug>/maturite', methods=['GET'])
def portail_get_maturite_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    pid = request.args.get('parcelle_id')
    conn = get_db()
    if pid:
        rows = dicts_from_rows(conn.execute(
            "SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f "
            "JOIN parcelles p ON f.id_parcelle=p.id "
            "WHERE f.id_parcelle=? AND f.id_client=? ORDER BY f.date_fiche DESC",
            (pid, client['id'])).fetchall())
    else:
        rows = dicts_from_rows(conn.execute(
            "SELECT f.*,p.nom as parcelle_nom,p.cepage FROM maturite_fiches f "
            "JOIN parcelles p ON f.id_parcelle=p.id "
            "WHERE f.id_client=? ORDER BY p.nom, f.date_fiche DESC", (client['id'],)).fetchall())
    conn.close()
    return jsonify(_enrichir_fiches(rows))

@app.route('/api/portail-s/<slug>/maturite', methods=['POST'])
def portail_add_maturite_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    d = request.json or {}
    d['id_client'] = client['id']; d['saisie_par'] = 'client'
    conn = get_db()
    fid, score, verdict, niveau, alertes = _insert_fiche(conn, d, client['id'])
    conn.commit(); conn.close()
    return jsonify({"id":fid,"score_total":score,"verdict":verdict,"niveau":niveau,"alertes":alertes})

@app.route('/api/portail-s/<slug>/maturite/<int:fid>', methods=['DELETE'])
def portail_delete_maturite_slug(slug, fid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    conn = get_db()
    conn.execute("DELETE FROM maturite_fiches WHERE id=? AND id_client=?", (fid, client['id']))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/portail-s/<slug>/maturite/<int:fid>', methods=['PUT'])
def portail_update_maturite_slug(slug, fid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    existing = dict_from_row(get_db().execute(
        "SELECT * FROM maturite_fiches WHERE id=? AND id_client=?", (fid, client['id'])
    ).fetchone())
    if not existing: return jsonify({"error":"Fiche introuvable"}), 404
    d = request.json; d['id_parcelle'] = existing['id_parcelle']
    score, verdict, niveau, alertes = calc_score_matu(d)
    ratio, _, _, _ = _score_ratio(d.get("degre") or d.get("degre_probable"), d.get("at") or d.get("AT"))
    conn = get_db()
    conn.execute("""UPDATE maturite_fiches SET
        date_fiche=?, degre_probable=?, AT=?, ratio_sat=?,
        couleur_pepins=?, saveur_pulpe=?, tanins=?, etat_sanitaire=?,
        score_total=?, verdict=?, alerte_sanitaire=?, reco_niveau=?
        WHERE id=? AND id_client=?""",
        (d.get('date_fiche') or existing['date_fiche'],
         d.get('degre') or d.get('degre_probable') or None,
         d.get('at') or d.get('AT') or None, ratio,
         d.get('couleur_pepins'), d.get('saveur_pulpe'), d.get('tanins'),
         int(d.get('etat_sanitaire') or 0),
         score, verdict, _alerte_sanitaire(d.get('etat_sanitaire') or 0, d.get('degre') or d.get('degre_probable')),
         niveau, fid, client['id']))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "score_total": score, "verdict": verdict, "niveau": niveau})

@app.route('/api/portail/<token>/maturite/<int:fid>', methods=['DELETE'])
def portail_delete_maturite_token(token, fid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    conn = get_db()
    conn.execute("DELETE FROM maturite_fiches WHERE id=? AND id_client=?", (fid, client['id']))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/portail/<token>/maturite/<int:fid>', methods=['PUT'])
def portail_update_maturite_token(token, fid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    existing = dict_from_row(get_db().execute(
        "SELECT * FROM maturite_fiches WHERE id=? AND id_client=?", (fid, client['id'])
    ).fetchone())
    if not existing: return jsonify({"error":"Fiche introuvable"}), 404
    d = request.json; d['id_parcelle'] = existing['id_parcelle']
    score, verdict, niveau, alertes = calc_score_matu(d)
    ratio, _, _, _ = _score_ratio(d.get("degre") or d.get("degre_probable"), d.get("at") or d.get("AT"))
    conn = get_db()
    conn.execute("""UPDATE maturite_fiches SET
        date_fiche=?, degre_probable=?, AT=?, ratio_sat=?,
        couleur_pepins=?, saveur_pulpe=?, tanins=?, etat_sanitaire=?,
        score_total=?, verdict=?, alerte_sanitaire=?, reco_niveau=?
        WHERE id=? AND id_client=?""",
        (d.get('date_fiche') or existing['date_fiche'],
         d.get('degre') or d.get('degre_probable') or None,
         d.get('at') or d.get('AT') or None, ratio,
         d.get('couleur_pepins'), d.get('saveur_pulpe'), d.get('tanins'),
         int(d.get('etat_sanitaire') or 0),
         score, verdict, _alerte_sanitaire(d.get('etat_sanitaire') or 0, d.get('degre') or d.get('degre_probable')),
         niveau, fid, client['id']))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "score_total": score, "verdict": verdict})


# ===== PRÉVISION MATURITÉ =====

def _fetch_meteo_7j(lat, lon):
    """Open-Meteo prévision 7 jours — retourne (t_moy, pluie_cumul, jours)"""
    import urllib.request, json as _json
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
               f"&forecast_days=7&timezone=Europe/Paris")
        with urllib.request.urlopen(url, timeout=8) as r:
            d = _json.loads(r.read())
        jours = []
        pluie_cumul = 0.0
        t_sum = 0.0
        for i, date_j in enumerate(d['daily']['time']):
            tmax = d['daily']['temperature_2m_max'][i] or 0
            tmin = d['daily']['temperature_2m_min'][i] or 0
            pluie = d['daily']['precipitation_sum'][i] or 0
            tmoy = (tmax + tmin) / 2
            pluie_cumul += pluie
            t_sum += tmoy
            jours.append({"date": date_j, "tmax": round(tmax,1),
                          "tmin": round(tmin,1), "tmoy": round(tmoy,1),
                          "pluie": round(pluie,1)})
        t_moy = round(t_sum / len(jours), 1) if jours else 18
        return t_moy, round(pluie_cumul, 1), jours
    except Exception as e:
        print(f"[meteo 7j] erreur: {e}")
        return 18.0, 0.0, []

def _dynamique_degre(fiches_triees):
    """
    Calcule la dynamique de progression du degré (%vol/jour).
    - Si 1 relevé : hypothèse 0.15%/jour
    - Si >= 2 relevés avec degré : dynamique sur les 2 derniers
    - Retourne (dynamique, nb_pts_utilises)
    """
    from datetime import date
    pts = [(f['date_fiche'], f['degre_probable']) for f in fiches_triees
           if f.get('degre_probable') is not None]
    if len(pts) < 2:
        return 0.15, 1
    # Deux derniers
    d1, v1 = pts[-2]
    d2, v2 = pts[-1]
    dt = (date.fromisoformat(d2) - date.fromisoformat(d1)).days
    if dt <= 0:
        return 0.15, 1
    dyn = round((v2 - v1) / dt, 4)
    return max(0.0, dyn), len(pts)

def _dynamique_at(fiches_triees):
    """
    Calcule la dynamique AT (g/L/jour, négatif = baisse).
    - Si < 2 relevés avec AT : hypothèse -0.20 g/L/jour
    - Sinon : calcul sur les 2 derniers relevés avec AT
    """
    from datetime import date
    pts = [(f['date_fiche'], f['AT']) for f in fiches_triees
           if f.get('AT') is not None]
    if len(pts) < 2:
        return -0.20, False
    d1, v1 = pts[-2]
    d2, v2 = pts[-1]
    dt = (date.fromisoformat(d2) - date.fromisoformat(d1)).days
    if dt <= 0:
        return -0.20, False
    return round((v2 - v1) / dt, 4), True

def _estimer_fenetre(degre_actuel, dyn_degre, at_actuel, dyn_at, date_dernier):
    """
    Estime les dates de la fenêtre de récolte.
    Fenêtre degré : 10.2 → 10.5 % vol
    Fenêtre AT : 7 → 12 g/L (objectif 8-12)
    Retourne un dict avec dates et valeurs estimées.
    """
    from datetime import date, timedelta
    d0 = date.fromisoformat(date_dernier)

    def jours_pour_degre(cible):
        if dyn_degre <= 0: return None
        j = (cible - degre_actuel) / dyn_degre
        return round(j, 1) if j >= 0 else None

    j_102 = jours_pour_degre(10.2)
    j_105 = jours_pour_degre(10.5)

    date_102 = (d0 + timedelta(days=j_102)).isoformat() if j_102 is not None else None
    date_105 = (d0 + timedelta(days=j_105)).isoformat() if j_105 is not None else None

    # AT à la fenêtre
    at_a_102 = at_a_105 = None
    if at_actuel is not None:
        if j_102 is not None:
            at_a_102 = round(at_actuel + dyn_at * j_102, 2)
        if j_105 is not None:
            at_a_105 = round(at_actuel + dyn_at * j_105, 2)

    # Fenêtre croisée : date où degré ET AT sont dans la cible
    # AT cible : 7-12 g/L, objectif 8-12
    croise = None
    if at_actuel is not None and dyn_degre > 0:
        # Chercher jour où degré in [10.2,10.5] ET AT in [7,12]
        for j in range(0, 60):
            deg_j = degre_actuel + dyn_degre * j
            at_j  = at_actuel  + dyn_at  * j
            if 10.2 <= deg_j <= 10.5 and 7.0 <= at_j <= 12.0:
                croise = {
                    "date":  (d0 + timedelta(days=j)).isoformat(),
                    "degre": round(deg_j, 2),
                    "at":    round(at_j, 2),
                    "jours": j,
                    "objectif_at": "optimal" if 8 <= at_j <= 12 else "limite"
                }
                break

    return {
        "date_102": date_102, "jours_102": j_102,
        "date_105": date_105, "jours_105": j_105,
        "at_a_102": at_a_102, "at_a_105": at_a_105,
        "fenetre_croisee": croise
    }

def _route_prevision(client, parcelle_id):
    from datetime import date
    conn = get_db()
    fiches = dicts_from_rows(conn.execute("""
        SELECT f.date_fiche, f.degre_probable, f.AT, f.score_total, f.verdict
        FROM maturite_fiches f
        WHERE f.id_parcelle=? AND f.id_client=?
        ORDER BY f.date_fiche ASC
    """, (parcelle_id, client['id'])).fetchall())
    conn.close()

    if not fiches:
        return jsonify({"error": "Aucune fiche disponible"}), 404

    # Météo 7 jours
    lat = client.get('latitude') or 49.26
    lon = client.get('longitude') or 4.03
    t_moy, pluie_7j, meteo_jours = _fetch_meteo_7j(lat, lon)

    # Choix de la dynamique selon météo
    # Favorable : T moy >= 18°C ET pluie < 15mm
    meteo_favorable = (t_moy >= 18.0 and pluie_7j < 15.0)
    meteo_label = "Climat favorable — dynamique récente retenue" if meteo_favorable \
                  else "Météo défavorable — tendance prudente retenue (+0.15%/j)"

    dyn_degre_recent, nb_pts = _dynamique_degre(fiches)
    dyn_at, at_mesure = _dynamique_at(fiches)

    # Si météo défavorable et plusieurs points → prendre moyenne globale (plus prudente)
    if not meteo_favorable and nb_pts > 1:
        pts = [(f['date_fiche'], f['degre_probable']) for f in fiches if f.get('degre_probable')]
        if len(pts) >= 2:
            d1, v1 = pts[0]; d2, v2 = pts[-1]
            dt = (date.fromisoformat(d2) - date.fromisoformat(d1)).days
            dyn_degre = round((v2 - v1) / dt, 4) if dt > 0 else 0.15
        else:
            dyn_degre = 0.15
    else:
        dyn_degre = dyn_degre_recent

    # Dernier relevé
    dernier = fiches[-1]
    degre_actuel = dernier.get('degre_probable')
    at_actuel = dernier.get('AT')
    date_dernier = dernier['date_fiche']

    fenetre = None
    if degre_actuel is not None:
        fenetre = _estimer_fenetre(
            float(degre_actuel), dyn_degre,
            float(at_actuel) if at_actuel else None, dyn_at,
            date_dernier
        )

    # Série pour graphique
    series_degre = [{"date": f['date_fiche'], "valeur": f['degre_probable']}
                    for f in fiches if f.get('degre_probable') is not None]
    series_at    = [{"date": f['date_fiche'], "valeur": f['AT']}
                    for f in fiches if f.get('AT') is not None]

    return jsonify({
        "series_degre":    series_degre,
        "series_at":       series_at,
        "dynamique_degre": dyn_degre,
        "dynamique_at":    dyn_at,
        "meteo_favorable": meteo_favorable,
        "meteo_label":     meteo_label,
        "t_moy_7j":        t_moy,
        "pluie_7j":        pluie_7j,
        "meteo_jours":     meteo_jours,
        "fenetre":         fenetre,
        "degre_actuel":    degre_actuel,
        "at_actuel":       at_actuel,
        "date_dernier":    date_dernier,
        "nb_fiches":       len(fiches),
        "at_mesure":       at_mesure,
    })

@app.route('/api/portail-s/<slug>/prevision-maturite/<int:pid>')
def portail_prevision_slug(slug, pid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return _route_prevision(client, pid)

@app.route('/api/portail/<token>/prevision-maturite/<int:pid>')
def portail_prevision_token(token, pid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return _route_prevision(client, pid)

# ===== RENDEMENTS =====

def _calc_rendement(nb_grappes, poids_g, nb_pieds_ha):
    """nb_grappes/pied × poids_g/1000 × nb_pieds_ha = kg/ha"""
    try:
        return round(float(nb_grappes) * float(poids_g) / 1000 * float(nb_pieds_ha))
    except:
        return None

def _route_rendements_get(client, pid=None):
    conn = get_db()
    if pid:
        rows = dicts_from_rows(conn.execute("""
            SELECT r.*, p.nom as parcelle_nom, p.cepage, p.commune, p.surface_cadastrale,
                   p.nb_pieds_ha as nb_pieds_ha_parc,
                   p.ecart_rangs, p.ecart_ceps
            FROM rendements r
            JOIN parcelles p ON r.id_parcelle=p.id
            WHERE r.id_client=? AND r.id_parcelle=?
            ORDER BY r.date_releve DESC
        """, (client['id'], pid)).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("""
            SELECT r.*, p.nom as parcelle_nom, p.cepage, p.commune, p.surface_cadastrale,
                   p.nb_pieds_ha as nb_pieds_ha_parc,
                   p.ecart_rangs, p.ecart_ceps
            FROM rendements r
            JOIN parcelles p ON r.id_parcelle=p.id
            WHERE r.id_client=?
            ORDER BY p.nom, r.date_releve DESC
        """, (client['id'],)).fetchall())
    conn.close()
    return jsonify(rows)

def _route_rendements_post(client):
    d = request.json or {}
    pid = d.get('id_parcelle')
    if not pid: return jsonify({"error": "id_parcelle requis"}), 400
    conn = get_db()
    parc = dict_from_row(conn.execute(
        "SELECT * FROM parcelles WHERE id=? AND id_client=?", (pid, client['id'])
    ).fetchone())
    if not parc: conn.close(); return jsonify({"error": "Parcelle introuvable"}), 404
    nb_pieds = d.get('nb_pieds_ha') or parc.get('nb_pieds_ha')
    if not nb_pieds: conn.close(); return jsonify({"error": "Densité (pieds/ha) manquante — renseignez-la sur la parcelle"}), 400
    nb_grappes = d.get('nb_grappes_pied')
    poids_g    = d.get('poids_moyen_g')
    if not nb_grappes or not poids_g:
        conn.close(); return jsonify({"error": "nb_grappes_pied et poids_moyen_g requis"}), 400
    rdt = _calc_rendement(nb_grappes, poids_g, nb_pieds)
    cur = conn.execute("""
        INSERT INTO rendements
            (id_parcelle, id_client, campagne, date_releve,
             nb_grappes_pied, poids_moyen_g, nb_pieds_ha, rendement_kgha, observations)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (pid, client['id'], d.get('campagne','2026'),
          d.get('date_releve') or __import__('datetime').date.today().isoformat(),
          nb_grappes, poids_g, nb_pieds, rdt, d.get('observations','')))
    # Mettre à jour rendement_kgha sur la parcelle (utilisé par l'itinéraire)
    if rdt:
        conn.execute("UPDATE parcelles SET rendement_ref_kgha=? WHERE id=?", (rdt, pid))
    conn.commit(); conn.close()
    return jsonify({"id": cur.lastrowid, "rendement_kgha": rdt})

def _route_rendements_delete(client, rid):
    conn = get_db()
    conn.execute("DELETE FROM rendements WHERE id=? AND id_client=?", (rid, client['id']))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/portail-s/<slug>/rendements', methods=['GET'])
def portail_rendements_get_slug(slug):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    pid = request.args.get('parcelle_id')
    return _route_rendements_get(c, pid)

@app.route('/api/portail-s/<slug>/rendements', methods=['POST'])
def portail_rendements_post_slug(slug):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    return _route_rendements_post(c)

@app.route('/api/portail-s/<slug>/rendements/<int:rid>', methods=['DELETE'])
def portail_rendements_delete_slug(slug, rid):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    return _route_rendements_delete(c, rid)

@app.route('/api/portail-s/<slug>/rendements/<int:rid>', methods=['PATCH'])
def portail_rendements_patch_slug(slug, rid):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    return _route_rendements_patch(c, rid)

@app.route('/api/portail/<token>/rendements', methods=['GET'])
def portail_rendements_get_token(token):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _route_rendements_get(c, request.args.get('parcelle_id'))

@app.route('/api/portail/<token>/rendements', methods=['POST'])
def portail_rendements_post_token(token):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _route_rendements_post(c)

@app.route('/api/portail/<token>/rendements/<int:rid>', methods=['DELETE'])
def portail_rendements_delete_token(token, rid):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _route_rendements_delete(c, rid)

@app.route('/api/portail/<token>/rendements/<int:rid>', methods=['PATCH'])
def portail_rendements_patch_token(token, rid):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _route_rendements_patch(c, rid)

def _route_rendements_patch(client, rid):
    """Saisie du rendement réel après vendange."""
    d = request.json or {}
    reel = d.get('rendement_reel_kgha')
    if reel is None: return jsonify({"error": "rendement_reel_kgha requis"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE rendements SET rendement_reel_kgha=? WHERE id=? AND id_client=?",
        (float(reel), rid, client['id'])
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "rendement_reel_kgha": float(reel)})


def _get_tous_rendements(client_id):
    """Tous les relevés rendement triés commune → parcelle → date."""
    conn = get_db()
    rows = dicts_from_rows(conn.execute("""
        SELECT r.*, p.nom as parcelle_nom, p.cepage, p.commune, p.surface_cadastrale
        FROM rendements r
        JOIN parcelles p ON r.id_parcelle=p.id
        WHERE r.id_client=? AND r.campagne='2026'
        ORDER BY p.commune, p.nom, r.date_releve ASC
    """, (client_id,)).fetchall())
    conn.close()
    return rows


@app.route('/api/portail-s/<slug>/rendements/export-csv')
def portail_rendements_csv_slug(slug):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    return _export_rendements_csv(c)

@app.route('/api/portail/<token>/rendements/export-csv')
def portail_rendements_csv_token(token):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _export_rendements_csv(c)

def _export_rendements_csv(client):
    import csv, io as sio
    rows = _get_tous_rendements(client['id'])
    buf = sio.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Commune","Parcelle","Cépage","Surface cadastrale (ares)",
                "Date relevé","Nb grappes/pied","Poids moyen (g)",
                "Densité (pieds/ha)","Rendement théorique (kg/ha)",
                "Rendement réel (kg/ha)","Écart (kg/ha)","Observations"])
    current_parc = None
    for r in rows:
        parc_key = (r.get('parcelle_nom'), r.get('cepage'))
        if current_parc and current_parc != parc_key:
            w.writerow([])
        current_parc = parc_key
        theo = r.get('rendement_kgha')
        reel = r.get('rendement_reel_kgha')
        ecart = round(reel - theo) if theo and reel else ''
        surf_ha = r.get('surface_cadastrale') or r.get('surface_cadastrale')
        surf_ares = round(float(surf_ha)*100, 2) if surf_ha else ''
        w.writerow([
            r.get('commune',''), r.get('parcelle_nom',''), r.get('cepage',''),
            surf_ares, r.get('date_releve',''),
            r.get('nb_grappes_pied',''), r.get('poids_moyen_g',''),
            r.get('nb_pieds_ha',''), theo or '', reel or '', ecart,
            r.get('observations','')
        ])
    buf.seek(0)
    safe = client['exploitation'].replace(' ','_').replace('/','-')
    return "\ufeff" + buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f"attachment; filename=Rendements_{safe}_2026.csv"
    }


@app.route('/api/portail-s/<slug>/rendements/export-pdf')
def portail_rendements_pdf_slug(slug):
    c = get_client_by_token_or_slug(slug)
    if not c: return jsonify({"error":"Lien invalide"}), 404
    return _export_rendements_pdf(c)

@app.route('/api/portail/<token>/rendements/export-pdf')
def portail_rendements_pdf_token(token):
    c = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not c: return jsonify({"error":"Token invalide"}), 404
    return _export_rendements_pdf(c)

def _export_rendements_pdf(client):
    from datetime import date
    appel = float(request.args.get('appellation') or 0)
    rows = _get_tous_rendements(client['id'])
    # Synthèse par parcelle (dernier relevé)
    synthese = {}
    for r in rows:
        key = (r.get('commune',''), r.get('parcelle_nom',''), r.get('cepage',''))
        synthese[key] = r

    # Surface totale en ha
    total_surface_ha = sum(float(r.get('surface_cadastrale') or 0) for r in synthese.values())

    # Rendement moyen PONDÉRÉ par surface
    rows_avec_surf = [r for r in synthese.values() if r.get('rendement_kgha') and r.get('surface_cadastrale')]
    total_w = sum(float(r['surface_cadastrale']) for r in rows_avec_surf)
    if total_w > 0:
        moy_rdt = round(sum(r['rendement_kgha'] * float(r['surface_cadastrale']) for r in rows_avec_surf) / total_w)
    elif synthese:
        rdts = [r['rendement_kgha'] for r in synthese.values() if r.get('rendement_kgha')]
        moy_rdt = round(sum(rdts)/len(rdts)) if rdts else 0
    else:
        moy_rdt = 0

    # Rendement réel exploitation (pondéré)
    total_kg = sum(float(r.get('kg_recoltes_total') or 0) for r in synthese.values())
    rdt_reel_exploit = round(total_kg / total_surface_ha) if total_surface_ha > 0 and total_kg > 0 else None
    tout_vendange = all(r.get('recolte_complete') == 1 for r in synthese.values() if r.get('kg_recoltes_total'))

    # Appellation passée en paramètre GET

    def rdt_col(v):
        return '#333'  # pas de seuil de couleur sur les rendements bruts

    def rdt_reel_col(v, appel):
        if not v: return '#999'
        if appel and appel > 0:
            return '#2E7D32' if v >= appel else '#C62828'
        return '#333'

    rows_html = ""
    current_com = None
    for r in rows:
        if r.get('commune') != current_com:
            current_com = r.get('commune','')
            rows_html += f'<tr><td colspan="9" style="background:#EAF3DE;font-weight:700;font-size:11px;padding:5px 8px;color:#2D6A4F">{current_com}</td></tr>'
        rdt      = r.get('rendement_kgha')
        rdt_reel = r.get('rendement_reel_kgha')
        surf_ha  = float(r.get('surface_cadastrale') or 0)
        surf_str = f"{round(surf_ha * 100, 2)} a" if surf_ha else '—'
        ecart    = round(rdt_reel - rdt) if rdt and rdt_reel else None
        if ecart is not None:
            ecart_str = (f'+{ecart:,}' if ecart >= 0 else f'{ecart:,}').replace(',', ' ')
            ecart_col = '#1565C0' if ecart > 0 else '#C62828' if ecart < 0 else '#666'
        else:
            ecart_str = '—'; ecart_col = '#666'
        prov = r.get('recolte_complete') == 0 and r.get('kg_recoltes_total', 0) > 0
        rdt_reel_str = f"{rdt_reel:,} kg/ha".replace(',', ' ') if rdt_reel else '—'
        if prov: rdt_reel_str += ' *'
        rows_html += f"""<tr>
            <td>{r.get('parcelle_nom','')}</td>
            <td>{r.get('cepage','')}</td>
            <td style="text-align:center">{surf_str}</td>
            <td style="text-align:center">{r.get('date_releve','')}</td>
            <td style="text-align:center">{r.get('nb_grappes_pied','—')}</td>
            <td style="text-align:center">{r.get('poids_moyen_g','—')} g</td>
            <td style="text-align:center;font-weight:700;color:{rdt_col(rdt)}">{f"{int(rdt):,} kg/ha".replace(",", " ") if rdt else "—"}</td>
            <td style="text-align:center;font-weight:700;color:{rdt_reel_col(rdt_reel, appel)}">{rdt_reel_str}</td>
            <td style="text-align:center;color:{ecart_col}">{ecart_str}</td>
        </tr>"""

    rdt_reel_resume = ''
    if rdt_reel_exploit:
        prov_str = ' (provisoire)' if not tout_vendange else ''
        rdt_reel_resume = f'<div><div class="val" style="color:{"#E65100" if not tout_vendange else "#2D6A4F"}">{rdt_reel_exploit:,} kg/ha</div><div class="lbl">Rdt réel{prov_str}</div></div>'

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
 body{{font-family:Arial,sans-serif;font-size:12px;margin:20px;color:#1a1a1a}}
 h1{{color:#2D6A4F;font-size:18px;margin-bottom:4px}}
 .sub{{color:#666;font-size:11px;margin-bottom:16px}}
 table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
 th{{background:#2D6A4F;color:#fff;padding:7px 8px;font-size:10px;text-align:left}}
 td{{padding:6px 8px;border-bottom:.5px solid #e8e6e1;vertical-align:top;font-size:11px}}
 tr:nth-child(even) td{{background:#f9f8f6}}
 .resume{{display:flex;gap:20px;background:#EAF3DE;padding:10px 14px;border-radius:8px;margin-bottom:12px;flex-wrap:wrap}}
 .resume div{{text-align:center;min-width:80px}}
 .resume .val{{font-size:18px;font-weight:700;color:#2D6A4F}}
 .resume .lbl{{font-size:10px;color:#666}}
 .legende{{font-size:10px;color:#666;margin-bottom:12px}}
 .footer{{color:#999;font-size:10px;margin-top:24px;border-top:.5px solid #e0ddd8;padding-top:8px}}
</style></head><body>
<h1>Suivi des rendements — Exploitation 2026</h1>
<p class="sub">{client['exploitation']} · Généré le {date.today().strftime('%d/%m/%Y')} · MatuScore</p>
<div class="resume">
 <div><div class="val">{len(synthese)}</div><div class="lbl">Parcelles</div></div>
 <div><div class="val">{round(total_surface_ha*100,1)} a</div><div class="lbl">Surface totale</div></div>
 <div><div class="val">{moy_rdt:,} kg/ha</div><div class="lbl">Rdt théo. moyen pondéré</div></div>
 {rdt_reel_resume}
</div>
<p class="legende">Écart vs appellation : <span style="color:#2E7D32">■</span> rdt réel ≥ appellation &nbsp; <span style="color:#C62828">■</span> rdt réel &lt; appellation &nbsp; * = provisoire</p>
<table>
 <thead><tr>
  <th>Parcelle</th><th>Cépage</th><th>Surface</th>
  <th>Date</th><th>Grappes/pied</th><th>Poids moyen</th>
  <th>Rdt théorique</th><th>Rdt réel</th><th>Écart</th>
 </tr></thead>
 <tbody>{rows_html}</tbody>
</table>
<p class="footer">MatuScore — by VITI Sens Conseil viticole — Document indicatif.</p>
</body></html>"""

    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        safe = client['exploitation'].replace(' ','_').replace('/','-')
        return pdf, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": f"attachment; filename=Rendements_{safe}_2026.pdf"
        }
    except Exception as e:
        print(f"[pdf] weasyprint indisponible ou en erreur ({e}) — repli HTML imprimable")
        return html + "<script>window.print()</script>", 200, {"Content-Type": "text/html; charset=utf-8"}

    rows_html = ""
    current_com = None
    for r in rows:
        if r.get('commune') != current_com:
            current_com = r.get('commune','')
            rows_html += f'<tr><td colspan="7" style="background:#EAF3DE;font-weight:700;font-size:11px;padding:5px 8px;color:#2D6A4F">{current_com}</td></tr>'
        rdt = r.get('rendement_kgha')
        rdt_reel = r.get('rendement_reel_kgha')
        rdt_col = "#2E7D32" if rdt and rdt < 10000 else "#E65100" if rdt else "#666"
        rdt_reel_col = "#2E7D32" if rdt_reel and rdt_reel < 10000 else "#E65100" if rdt_reel else "#999"
        ecart = round(rdt_reel - rdt) if rdt and rdt_reel else None
        ecart_str = (f'+{ecart:,}' if ecart and ecart >= 0 else f'{ecart:,}').replace(',', ' ') if ecart is not None else '—'
        ecart_col = "#E65100" if ecart and ecart > 0 else "#1565C0" if ecart and ecart < 0 else "#666"
        rows_html += f"""<tr>
            <td>{r.get('parcelle_nom','')}</td>
            <td>{r.get('cepage','')}</td>
            <td style="text-align:center">{round(float(r.get('surface_cadastrale') or r.get('surface_cadastrale') or 0)*100, 2) or '—'} a</td>
            <td style="text-align:center">{r.get('date_releve','')}</td>
            <td style="text-align:center">{r.get('nb_grappes_pied','—')}</td>
            <td style="text-align:center">{r.get('poids_moyen_g','—')} g</td>
            <td style="text-align:center;font-weight:700;color:{rdt_col}">{f"{rdt:,} kg/ha".replace(","," ") if rdt else '—'}</td>
            <td style="text-align:center;font-weight:700;color:{rdt_reel_col}">{f"{rdt_reel:,} kg/ha".replace(","," ") if rdt_reel else '—'}</td>
            <td style="text-align:center;color:{ecart_col}">{ecart_str}</td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
 body{{font-family:Arial,sans-serif;font-size:12px;margin:20px;color:#1a1a1a}}
 h1{{color:#2D6A4F;font-size:18px;margin-bottom:4px}}
 .sub{{color:#666;font-size:11px;margin-bottom:16px}}
 table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
 th{{background:#2D6A4F;color:#fff;padding:7px 8px;font-size:10px;text-align:left}}
 td{{padding:6px 8px;border-bottom:.5px solid #e8e6e1;vertical-align:top;font-size:11px}}
 tr:nth-child(even) td{{background:#f9f8f6}}
 .resume{{display:flex;gap:20px;background:#EAF3DE;padding:10px 14px;border-radius:8px;margin-bottom:16px}}
 .resume div{{text-align:center}}
 .resume .val{{font-size:20px;font-weight:700;color:#2D6A4F}}
 .resume .lbl{{font-size:10px;color:#666}}
 .footer{{color:#999;font-size:10px;margin-top:24px;border-top:.5px solid #e0ddd8;padding-top:8px}}
</style></head><body>
<h1>Suivi des rendements — Exploitation 2026</h1>
<p class="sub">{client['exploitation']} · Généré le {date.today().strftime('%d/%m/%Y')} · MatuScore</p>
<div class="resume">
 <div><div class="val">{len(synthese)}</div><div class="lbl">Parcelles</div></div>
 <div><div class="val">{total_surface:.2f} ha</div><div class="lbl">Surface totale</div></div>
 <div><div class="val">{moy_rdt:,} kg/ha</div><div class="lbl">Rendement moyen</div></div>
 <div><div class="val">{len(rows)}</div><div class="lbl">Relevés</div></div>
</div>
<table>
 <thead><tr>
  <th>Parcelle</th><th>Cépage</th><th>Surface (ha)</th>
  <th>Date</th><th>Grappes/pied</th><th>Poids moyen</th>
  <th>Rdt théorique</th><th>Rdt réel</th><th>Écart</th>
 </tr></thead>
 <tbody>{rows_html}</tbody>
</table>
<p class="footer">MatuScore — by VITI Sens Conseil viticole — Document indicatif.</p>
</body></html>"""

    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        safe = client['exploitation'].replace(' ','_').replace('/','-')
        return pdf, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": f"attachment; filename=Rendements_{safe}_2026.pdf"
        }
    except Exception as e:
        print(f"[pdf] weasyprint indisponible ou en erreur ({e}) — repli HTML imprimable")
        return html + "<script>window.print()</script>", 200, {"Content-Type": "text/html; charset=utf-8"}


# ===== EXPORT EXPLOITATION =====


def _get_synthese_exploitation(client_id):
    """
    Retourne la dernière fiche par parcelle pour toute l'exploitation,
    triée par score décroissant, enrichie avec date de récolte.
    """
    conn = get_db()
    rows = dicts_from_rows(conn.execute("""
        SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune, p.nb_pieds_ha
        FROM maturite_fiches f
        JOIN parcelles p ON f.id_parcelle = p.id
        WHERE f.id_client = ? AND f.campagne = '2026'
          AND f.id = (
              SELECT MAX(id) FROM maturite_fiches f2
              WHERE f2.id_parcelle = f.id_parcelle
                AND f2.id_client = f.id_client
                AND f2.campagne = '2026'
          )
        ORDER BY f.score_total DESC NULLS LAST
    """, (client_id,)).fetchall())
    conn.close()
    return _enrichir_fiches(rows)

def _get_series_exploitation(client_id):
    """Retourne les séries moyennes groupées par commune × cépage pour le graphique."""
    conn = get_db()
    rows = dicts_from_rows(conn.execute("""
        SELECT f.date_fiche, f.degre_probable, f.AT,
               p.commune, p.cepage
        FROM maturite_fiches f
        JOIN parcelles p ON f.id_parcelle = p.id
        WHERE f.id_client = ? AND f.campagne = '2026'
          AND f.degre_probable IS NOT NULL
        ORDER BY p.commune, p.cepage, f.date_fiche ASC
    """, (client_id,)).fetchall())
    conn.close()

    # Grouper par commune × cépage × date → calculer moyenne
    from collections import defaultdict
    groupes = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r['commune'] or '', r['cepage'] or '')
        groupes[key][r['date_fiche']].append(float(r['degre_probable']))

    series = []
    for (commune, cepage), dates_vals in groupes.items():
        serie = []
        for date_f, vals in sorted(dates_vals.items()):
            moy = round(sum(vals) / len(vals), 2)
            serie.append({"date": date_f, "valeur": moy, "n": len(vals)})
        series.append({
            "commune": commune,
            "cepage":  cepage,
            "label":   commune + " – " + cepage,
            "serie":   serie
        })

    # Trier par commune puis cépage
    series.sort(key=lambda x: (x['commune'], x['cepage']))
    return series

@app.route('/api/portail-s/<slug>/exploitation/graphique')
def portail_graphique_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return jsonify({
        "series": _get_series_exploitation(client['id']),
        "synthese": _get_synthese_exploitation(client['id'])
    })

@app.route('/api/portail/<token>/exploitation/graphique')
def portail_graphique_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return jsonify({
        "series": _get_series_exploitation(client['id']),
        "synthese": _get_synthese_exploitation(client['id'])
    })

@app.route('/api/portail-s/<slug>/exploitation/export-csv')
def portail_export_csv_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return _export_csv_exploitation(client)

@app.route('/api/portail/<token>/exploitation/export-csv')
def portail_export_csv_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return _export_csv_exploitation(client)

def _export_csv_exploitation(client):
    import csv, io as sio
    conn = get_db()
    # Toutes les fiches, toutes les parcelles, triées par parcelle puis date
    fiches = dicts_from_rows(conn.execute("""
        SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune, p.nb_pieds_ha
        FROM maturite_fiches f
        JOIN parcelles p ON f.id_parcelle = p.id
        WHERE f.id_client = ? AND f.campagne = '2026'
        ORDER BY p.commune, p.nom, p.cepage, f.date_fiche ASC
    """, (client['id'],)).fetchall())
    conn.close()
    fiches = _enrichir_fiches(fiches)

    buf = sio.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Commune","Parcelle","Cépage","Surface (ha)",
                "Date relevé","Degré (% vol.)","AT (g/L)","Ratio S/AT",
                "Pépins","Pulpe","Tanins","Sanitaire (%)",
                "Score /100","Verdict",
                "Récolte estimée (début 10.2%)","Récolte estimée (fin 10.5%)",
                "AT estimée début","AT estimée fin"])
    current_parc = None
    for f in fiches:
        parc_key = (f.get('parcelle_nom'), f.get('cepage'))
        # Ligne vide entre parcelles pour lisibilité
        if current_parc and current_parc != parc_key:
            w.writerow([])
        current_parc = parc_key
        dr = f.get('date_recolte') or {}
        w.writerow([
            f.get('commune',''),
            f.get('parcelle_nom',''),
            f.get('cepage',''),
            f.get('surface_cadastrale',''),
            f.get('date_fiche',''),
            f.get('degre_probable',''),
            f.get('AT',''),
            round(f.get('ratio_sat'), 2) if f.get('ratio_sat') else '',
            f.get('couleur_pepins',''),
            f.get('saveur_pulpe',''),
            f.get('tanins',''),
            f.get('etat_sanitaire', 0),
            f.get('score_total',''),
            f.get('verdict',''),
            dr.get('date_102',''),
            dr.get('date_105',''),
            dr.get('at_102',''),
            dr.get('at_105','')
        ])
    buf.seek(0)
    safe = client['exploitation'].replace(' ','_').replace('/','-')
    return "\ufeff" + buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f"attachment; filename=Maturite_{safe}_2026.csv"
    }

@app.route('/api/portail-s/<slug>/exploitation/export-pdf')
def portail_export_pdf_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error":"Lien invalide"}), 404
    return _export_pdf_exploitation(client)

@app.route('/api/portail/<token>/exploitation/export-pdf')
def portail_export_pdf_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error":"Token invalide"}), 404
    return _export_pdf_exploitation(client)

def _export_pdf_exploitation(client):
    from datetime import date
    conn = get_db()
    fiches = dicts_from_rows(conn.execute("""
        SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune
        FROM maturite_fiches f
        JOIN parcelles p ON f.id_parcelle = p.id
        WHERE f.id_client = ? AND f.campagne = '2026'
        ORDER BY p.commune, p.nom, p.cepage, f.date_fiche ASC
    """, (client['id'],)).fetchall())
    conn.close()
    fiches = _enrichir_fiches(fiches)
    nb_parcelles = len(set((f['parcelle_nom'], f['cepage']) for f in fiches))
    scores = [f.get('score_total') or 0 for f in fiches]
    score_moyen = round(sum(scores) / len(scores)) if scores else 0
    html = _html_pdf_exploitation(client, fiches, score_moyen, nb_parcelles)
    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        safe = client['exploitation'].replace(' ','_').replace('/','-')
        return pdf, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": f"attachment; filename=Maturite_{safe}_2026.pdf"
        }
    except Exception as e:
        print(f"[pdf] weasyprint indisponible ou en erreur ({e}) — repli HTML imprimable")
        return html + "<script>window.print()</script>", 200, {"Content-Type": "text/html; charset=utf-8"}

def _html_pdf_exploitation(client, fiches, score_moyen, nb_parcelles=None):
    from datetime import date
    if nb_parcelles is None:
        nb_parcelles = len(set((f.get('parcelle_nom'), f.get('cepage')) for f in fiches))
    rows_html = ""
    current_parc = None
    for i, f in enumerate(fiches, start=1):
        parc_key = (f.get('parcelle_nom'), f.get('cepage'))
        # Ligne de séparation entre parcelles
        if current_parc and current_parc != parc_key:
            rows_html += '<tr><td colspan="9" style="background:#f0f7f0;height:4px;padding:0"></td></tr>'
        current_parc = parc_key
        dr = f.get('date_recolte') or {}
        sc = f.get('score_total') or 0
        col = "#2E7D32" if sc >= 70 else "#E65100" if sc >= 40 else "#C62828"
        rows_html += f"""<tr>
            <td>{i}</td>
            <td><strong>{f.get('parcelle_nom','')}</strong><br>
                <span style="font-size:10px;color:#666">{f.get('cepage','')} · {f.get('commune','')}</span></td>
            <td>{f.get('date_fiche','')}</td>
            <td style="font-size:15px;font-weight:700">{f.get('degre_probable') or '—'}{'% ' if f.get('degre_probable') else ''}</td>
            <td>{f.get('AT') or '—'}{'g/L' if f.get('AT') else ''}</td>
            <td>{f.get('etat_sanitaire',0)}%</td>
            <td><span style="background:{col}22;color:{col};padding:2px 8px;border-radius:10px;font-weight:700">{sc}/100</span></td>
            <td style="font-size:11px">{f.get('verdict','—')}</td>
            <td style="font-size:11px;color:#2D6A4F">{dr.get('date_102','—') if dr else '—'}<br>→ {dr.get('date_105','') if dr else ''}</td>
        </tr>"""
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:Arial,sans-serif;font-size:12px;margin:20px;color:#1a1a1a}}
  h1{{color:#2D6A4F;font-size:18px;margin-bottom:4px}}
  .sub{{color:#666;font-size:11px;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
  th{{background:#2D6A4F;color:#fff;padding:7px 8px;font-size:10px;text-align:left}}
  td{{padding:7px 8px;border-bottom:.5px solid #e8e6e1;vertical-align:top}}
  tr:nth-child(even) td{{background:#f9f8f6}}
  .resume{{display:flex;gap:20px;background:#EAF3DE;padding:10px 14px;border-radius:8px;margin-bottom:16px}}
  .resume div{{text-align:center}}
  .resume .val{{font-size:20px;font-weight:700;color:#2D6A4F}}
  .resume .lbl{{font-size:10px;color:#666}}
  .footer{{color:#999;font-size:10px;margin-top:24px;border-top:.5px solid #e0ddd8;padding-top:8px}}
</style></head><body>
<h1>Suivi de maturité — Exploitation 2026</h1>
<p class="sub">{client['exploitation']} · Généré le {date.today().strftime('%d/%m/%Y')} · MatuScore</p>
<div class="resume">
  <div><div class="val">{nb_parcelles}</div><div class="lbl">Parcelles</div></div>
  <div><div class="val">{score_moyen}/100</div><div class="lbl">Score moyen</div></div>
  <div><div class="val">{sum(1 for f in fiches if (f.get('score_total') or 0) >= 70)}</div><div class="lbl">Prêtes</div></div>
  <div><div class="val">{sum(1 for f in fiches if (f.get('etat_sanitaire') or 0) > 5)}</div><div class="lbl">Alerte sanitaire</div></div>
</div>
<table>
  <thead><tr>
    <th>#</th><th>Parcelle</th><th>Date</th><th>Degré</th><th>AT</th>
    <th>Sanitaire</th><th>Score</th><th>Verdict</th><th>Récolte estimée</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p class="footer">MatuScore — by VITI Sens Conseil viticole — Document indicatif.</p>
</body></html>"""


    """
    Calcule les DJC cumulés depuis le 20 mars 2026 (base 10°C, méthode Winkler).
    Utilise Open-Meteo historical + forecast.
    Retourne (djc_cumule, djc_journaliers_prevision_7j)
    """
    import urllib.request, json
    from datetime import date, timedelta
    debut = date(2026, 3, 20)
    today = date.today()
    # Historique depuis début jusqu'à hier
    djc_cumule = 0.0
    djc_hist = []
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max,temperature_2m_min"
               f"&start_date={debut.isoformat()}&end_date={today.isoformat()}"
               f"&timezone=Europe/Paris")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        dates = data['daily']['time']
        tmax = data['daily']['temperature_2m_max']
        tmin = data['daily']['temperature_2m_min']
        for i, d in enumerate(dates):
            tmoy = (tmax[i] + tmin[i]) / 2
            djc = max(0, tmoy - 10)
            djc_cumule += djc
            djc_hist.append({"date": d, "djc": round(djc, 1)})
    except Exception as e:
        print(f"   ⚠️ DJC historique erreur : {e}")
    # Prévision 7 jours
    djc_prev = []
    try:
        url2 = (f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}"
                f"&daily=temperature_2m_max,temperature_2m_min"
                f"&forecast_days=7&timezone=Europe/Paris")
        with urllib.request.urlopen(url2, timeout=8) as r:
            data2 = json.loads(r.read())
        for i, d in enumerate(data2['daily']['time']):
            tmoy = (data2['daily']['temperature_2m_max'][i] + data2['daily']['temperature_2m_min'][i]) / 2
            djc_prev.append({"date": d, "djc": round(max(0, tmoy - 10), 1)})
    except Exception as e:
        print(f"   ⚠️ DJC prévision erreur : {e}")
    return round(djc_cumule, 1), djc_prev

def estimer_date_recolte(djc_cumule, djc_prev, seuil=1250):
    """
    Estime la date de récolte en extrapolant les DJC prévisionnels.
    Retourne (date_str, djc_restants, methode)
    """
    from datetime import date, timedelta
    restants = seuil - djc_cumule
    if restants <= 0:
        return date.today().isoformat(), 0, "seuil_atteint"
    # Calculer avec les prévisions disponibles
    cumul = 0.0
    for prev in djc_prev:
        cumul += prev["djc"]
        if cumul >= restants:
            return prev["date"], round(restants, 1), "prevision_7j"
    # Extrapolation au-delà des 7j : moyenne des 7 derniers jours
    if djc_prev:
        moy_djc = sum(p["djc"] for p in djc_prev) / len(djc_prev)
        if moy_djc > 0:
            jours_sup = int((restants - cumul) / moy_djc) + 1
            last_date = date.fromisoformat(djc_prev[-1]["date"])
            date_estimee = last_date + timedelta(days=jours_sup)
            return date_estimee.isoformat(), round(restants, 1), "extrapolation"
    return None, round(restants, 1), "inconnu"

@app.route('/api/portail/<token>/vendange-info')
def portail_vendange_info(token):
    """DJC cumulés + date récolte estimée pour les parcelles en surveillance"""
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _vendange_info(client)

@app.route('/api/portail-s/<slug>/vendange-info')
def portail_vendange_info_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _vendange_info(client)

def _vendange_info(client):
    lat = client.get("latitude") or 49.26
    lon = client.get("longitude") or 4.03
    djc_cumule, djc_prev = fetch_djc_depuis_mars(lat, lon)
    date_recolte, djc_restants, methode = estimer_date_recolte(djc_cumule, djc_prev)
    return jsonify({
        "djc_cumule": djc_cumule,
        "djc_restants": djc_restants,
        "seuil": 1250,
        "date_recolte_estimee": date_recolte,
        "methode": methode,
        "djc_prevision": djc_prev[:7]
    })

@app.route('/api/portail/<token>/itineraire', methods=['POST'])
def portail_itineraire(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _calc_itineraire(client)

@app.route('/api/portail-s/<slug>/itineraire', methods=['POST'])
def portail_itineraire_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _calc_itineraire(client)

@app.route('/api/portail-s/<slug>/itineraire/date-optimale', methods=['GET'])
def portail_date_optimale_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _calc_date_optimale(client)

@app.route('/api/portail/<token>/itineraire/date-optimale', methods=['GET'])
def portail_date_optimale_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _calc_date_optimale(client)

@app.route('/api/portail-s/<slug>/itineraire/export-pdf', methods=['POST'])
def portail_itineraire_pdf_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _export_itineraire_pdf(client)

@app.route('/api/portail/<token>/itineraire/export-pdf', methods=['POST'])
def portail_itineraire_pdf_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _export_itineraire_pdf(client)

def _export_itineraire_pdf(client):
    from datetime import date
    j = request.json or {}
    itin = j.get('itineraire', [])
    total_kg = j.get('total_kg', 0)
    nb_jours = j.get('nb_jours', 0)
    kg_jour = j.get('kg_par_jour', 0)
    if not itin:
        return jsonify({"error": "Pas de données"}), 400

    JOURS_FR = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche']
    MOIS_FR  = ['','janvier','février','mars','avril','mai','juin',
                'juillet','août','septembre','octobre','novembre','décembre']

    def fmt_d(s):
        try:
            d2 = date.fromisoformat(s)
            return f"{JOURS_FR[d2.weekday()]} {d2.day} {MOIS_FR[d2.month]} {d2.year}"
        except: return s

    rows_html = ""
    for j_item in itin:
        date_str = fmt_d(j_item['date'])
        kg_tot = j_item.get('kg_total')
        rows_html += f'<tr style="background:#EAF3DE"><td colspan="6" style="padding:6px 8px;font-weight:700;color:#2D6A4F">{date_str} — Jour {j_item["jour"]}{(" — "+str(kg_tot)+" kg estimés") if kg_tot else ""}</td></tr>'
        for p in j_item.get('parcelles', []):
            de = p.get('degre_estime')
            de_col = "#C62828" if de and de < 9 else "#E65100" if de and de < 9.5 else "#2E7D32"
            avant = " ⚠️" if p.get('avant_fenetre') else ""
            rows_html += f"""<tr>
                <td style="padding:5px 8px">{p['nom']}{avant}</td>
                <td style="padding:5px 8px">{p['cepage']}</td>
                <td style="padding:5px 8px">{p['commune']}</td>
                <td style="padding:5px 8px;text-align:center">{p['surface_ares']} a</td>
                <td style="padding:5px 8px;text-align:center;color:{de_col}">{de}% vol</td>
                <td style="padding:5px 8px;text-align:right;border:.5px solid #ccc;min-width:80px">&nbsp;</td>
            </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
 body{{font-family:Arial,sans-serif;font-size:11px;margin:20px;color:#1a1a1a}}
 h1{{color:#2D6A4F;font-size:16px;margin-bottom:4px}}
 .sub{{color:#666;font-size:10px;margin-bottom:16px}}
 table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
 th{{background:#2D6A4F;color:#fff;padding:6px 8px;font-size:10px;text-align:left}}
 td{{padding:5px 8px;border-bottom:.5px solid #e8e6e1;font-size:10px}}
 tr:nth-child(even) td{{background:#f9f8f6}}
 .resume{{display:flex;gap:16px;background:#EAF3DE;padding:10px 14px;border-radius:8px;margin-bottom:16px}}
 .resume div{{text-align:center}}
 .resume .val{{font-size:18px;font-weight:700;color:#2D6A4F}}
 .resume .lbl{{font-size:9px;color:#666}}
 .footer{{color:#999;font-size:9px;margin-top:24px;border-top:.5px solid #e0ddd8;padding-top:8px}}
</style></head><body>
<h1>Itinéraire de vendange 2026 — {client['exploitation']}</h1>
<p class="sub">Généré le {date.today().strftime('%d/%m/%Y')} · MatuScore</p>
<div class="resume">
 <div><div class="val">{total_kg:,} kg</div><div class="lbl">Volume total</div></div>
 <div><div class="val">{nb_jours} j</div><div class="lbl">Durée</div></div>
 <div><div class="val">{kg_jour:,} kg/j</div><div class="lbl">Objectif/jour</div></div>
</div>
<table>
 <thead><tr>
  <th>Parcelle</th><th>Cépage</th><th>Commune</th>
  <th>Surface</th><th>Degré estimé</th><th>Récolté (kg)</th>
 </tr></thead>
 <tbody>{rows_html}</tbody>
</table>
<p class="footer">MatuScore · by VITI Sens · Document indicatif</p>
</body></html>"""

    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        safe = client['exploitation'].replace(' ','_').replace('/','-')
        return pdf, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": f"attachment; filename=Itineraire_{safe}_2026.pdf"
        }
    except Exception as e:
        print(f"[pdf] weasyprint indisponible ou en erreur ({e}) — repli HTML imprimable")
        return html + "<script>window.print()</script>", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route('/api/portail-s/<slug>/recolte-jour', methods=['POST'])
def portail_recolte_jour_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _saisir_recolte_jour(client)

@app.route('/api/portail/<token>/recolte-jour', methods=['POST'])
def portail_recolte_jour_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _saisir_recolte_jour(client)


@app.route('/api/portail-s/<slug>/recolte-jour/reinitialiser', methods=['POST'])
def portail_recolte_reinitialiser_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _reinitialiser_recolte(client)

@app.route('/api/portail/<token>/recolte-jour/reinitialiser', methods=['POST'])
def portail_recolte_reinitialiser_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _reinitialiser_recolte(client)

def _reinitialiser_recolte(client):
    """Efface les volumes récoltés saisis (kg_recoltes_total, rendement réel,
    statut 'terminé') pour la campagne en cours — utilisé par le bouton
    'Réinitialiser' de l'itinéraire quand la saisie s'est mal déroulée. Ne touche
    pas au rendement théorique (rendement_kgha), seulement aux données réelles.
    Efface aussi l'itinéraire sauvegardé, puisqu'il va être recalculé de zéro."""
    j = request.json or {}
    campagne = j.get('campagne', '2026')
    conn = get_db()
    conn.execute("""UPDATE rendements
        SET kg_recoltes_total=0, rendement_reel_kgha=NULL, recolte_complete=0
        WHERE id_client=? AND campagne=?""", (client['id'], campagne))
    conn.execute("DELETE FROM itineraire_sauvegarde WHERE id_client=? AND campagne=?",
                 (client['id'], campagne))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== Carnet de vendange (caisses/poids par parcelle, jour par jour) =========

@app.route('/api/portail/<token>/carnet-vendange', methods=['GET'])
def portail_carnet_get_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_get(client)

@app.route('/api/portail-s/<slug>/carnet-vendange', methods=['GET'])
def portail_carnet_get_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_get(client)

def _carnet_get(client):
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    parcelles = dicts_from_rows(conn.execute("""
        SELECT p.id, p.nom, p.lieu_dit, p.cepage, p.commune, p.surface_cadastrale,
            (SELECT r.recolte_complete FROM rendements r WHERE r.id_parcelle=p.id AND r.campagne=? ORDER BY r.id DESC LIMIT 1) AS recolte_complete,
            (SELECT r.kg_recoltes_total FROM rendements r WHERE r.id_parcelle=p.id AND r.campagne=? ORDER BY r.id DESC LIMIT 1) AS kg_recoltes_total
        FROM parcelles p WHERE p.id_client=? ORDER BY p.commune, p.nom
    """, (campagne, campagne, client['id'])).fetchall())
    entrees = dicts_from_rows(conn.execute(
        "SELECT * FROM carnet_vendange WHERE id_client=? AND campagne=? ORDER BY date, id",
        (client['id'], campagne)).fetchall())
    for e in entrees:
        e['parcelles'] = [r['id_parcelle'] for r in conn.execute(
            "SELECT id_parcelle FROM carnet_vendange_parcelles WHERE id_carnet=?", (e['id'],)).fetchall()]
    conn.close()
    return jsonify({"parcelles": parcelles, "entrees": entrees})


def _carnet_recalculer_rendements(client, id_parcelles, conn):
    """Le carnet de vendange est désormais la SEULE source des volumes récoltés — le
    tableau rendement (kg_recoltes_total, rendement_reel_kgha) se recalcule à partir
    de TOUTES les saisies du carnet pour chaque parcelle concernée, tous jours
    confondus. Une entrée fusionnée répartit son poids au prorata de la surface des
    parcelles membres. Ne touche jamais à recolte_complete (géré séparément par la
    case à cocher 'Parcelle terminée')."""
    if not id_parcelles: return
    campagne = '2026'
    toutes_parc = {p['id']: p for p in dicts_from_rows(conn.execute(
        "SELECT id, surface_cadastrale FROM parcelles WHERE id_client=?", (client['id'],)).fetchall())}
    entrees = dicts_from_rows(conn.execute(
        "SELECT * FROM carnet_vendange WHERE id_client=? AND campagne=?", (client['id'], campagne)).fetchall())

    totaux = {pid: 0.0 for pid in id_parcelles}
    for e in entrees:
        if not e.get('poids_total'): continue
        membres = [r['id_parcelle'] for r in conn.execute(
            "SELECT id_parcelle FROM carnet_vendange_parcelles WHERE id_carnet=?", (e['id'],)).fetchall()]
        concernes = [m for m in membres if m in totaux]
        if not concernes: continue
        if len(membres) == 1:
            totaux[membres[0]] += e['poids_total']
        else:
            surf_totale = sum((toutes_parc.get(m, {}).get('surface_cadastrale') or 0) for m in membres)
            for m in concernes:
                part = (toutes_parc.get(m, {}).get('surface_cadastrale') or 0) / surf_totale if surf_totale else 1/len(membres)
                totaux[m] += e['poids_total'] * part

    for pid, kg in totaux.items():
        surf_ha = toutes_parc.get(pid, {}).get('surface_cadastrale')
        rdt = round(kg / surf_ha) if surf_ha and kg else None
        existing = conn.execute(
            "SELECT id FROM rendements WHERE id_parcelle=? AND id_client=? AND campagne=? ORDER BY id DESC LIMIT 1",
            (pid, client['id'], campagne)).fetchone()
        if existing:
            conn.execute("UPDATE rendements SET kg_recoltes_total=?, rendement_reel_kgha=? WHERE id=?",
                         (round(kg, 1) if kg else 0, rdt, existing['id']))
        elif kg:
            conn.execute("""INSERT INTO rendements (id_parcelle, id_client, campagne, date_releve,
                nb_grappes_pied, poids_moyen_g, kg_recoltes_total, rendement_reel_kgha, recolte_complete)
                VALUES (?,?,?,date('now'),0,0,?,?,0)""",
                (pid, client['id'], campagne, round(kg, 1), rdt))


@app.route('/api/portail/<token>/carnet-vendange', methods=['POST'])
def portail_carnet_save_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_save(client)

@app.route('/api/portail-s/<slug>/carnet-vendange', methods=['POST'])
def portail_carnet_save_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_save(client)

def _carnet_save(client):
    """Enregistre une saisie du carnet. Deux cas, distingués par la présence d'un
    'id' explicite dans la requête :
    - Avec 'id' : correction d'une ligne EXISTANTE (l'utilisateur s'est trompé et
      modifie sa saisie) — mise à jour de cette ligne précise, jamais de doublon.
    - Sans 'id' : NOUVELLE livraison — insère toujours une nouvelle ligne, même si
      une saisie existe déjà pour la même parcelle le même jour (plusieurs
      livraisons par jour, ou sur plusieurs jours, doivent toutes être conservées
      sans écraser les précédentes tant que la parcelle n'est pas 'terminée')."""
    d = request.json or {}
    campagne = d.get('campagne') or '2026'
    date_j = d.get('date')
    ids_parcelles = sorted(set(int(x) for x in (d.get('id_parcelles') or [])))
    entry_id = d.get('id')
    if not date_j or not ids_parcelles:
        return jsonify({"error": "date et id_parcelles requis"}), 400
    caisses = d.get('caisses')
    poids_total = d.get('poids_total')
    poids_moyen = d.get('poids_moyen')
    note = d.get('note')
    vide = not caisses and not poids_total and not poids_moyen and not note

    conn = get_db()

    if entry_id:
        row = conn.execute("SELECT id FROM carnet_vendange WHERE id=? AND id_client=?",
                            (entry_id, client['id'])).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Entrée introuvable"}), 404
        if vide:
            conn.execute("DELETE FROM carnet_vendange_parcelles WHERE id_carnet=?", (entry_id,))
            conn.execute("DELETE FROM carnet_vendange WHERE id=?", (entry_id,))
            _carnet_recalculer_rendements(client, ids_parcelles, conn)
            conn.commit(); conn.close()
            return jsonify({"status": "ok", "id": None, "deleted": True})
        conn.execute("""UPDATE carnet_vendange SET date=?, caisses=?, poids_total=?, poids_moyen=?, note=?,
            updated_at=datetime('now') WHERE id=?""",
            (date_j, caisses, poids_total, poids_moyen, note, entry_id))
        _carnet_recalculer_rendements(client, ids_parcelles, conn)
        conn.commit(); conn.close()
        return jsonify({"status": "ok", "id": entry_id})

    if vide:
        conn.close()
        return jsonify({"status": "ok", "id": None})

    cur = conn.execute("""INSERT INTO carnet_vendange (id_client, campagne, date, caisses, poids_total, poids_moyen, note)
        VALUES (?,?,?,?,?,?,?)""", (client['id'], campagne, date_j, caisses, poids_total, poids_moyen, note))
    cid = cur.lastrowid
    for pid in ids_parcelles:
        conn.execute("INSERT INTO carnet_vendange_parcelles (id_carnet, id_parcelle) VALUES (?,?)", (cid, pid))
    _carnet_recalculer_rendements(client, ids_parcelles, conn)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "id": cid})


def portail_carnet_delete_token(token, cid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_delete(client, cid)

@app.route('/api/portail-s/<slug>/carnet-vendange/<int:cid>', methods=['DELETE'])
def portail_carnet_delete_slug(slug, cid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_delete(client, cid)

def _carnet_delete(client, cid):
    conn = get_db()
    row = conn.execute("SELECT id FROM carnet_vendange WHERE id=? AND id_client=?", (cid, client['id'])).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Entrée introuvable"}), 404
    ids_parcelles = [r['id_parcelle'] for r in conn.execute(
        "SELECT id_parcelle FROM carnet_vendange_parcelles WHERE id_carnet=?", (cid,)).fetchall()]
    conn.execute("DELETE FROM carnet_vendange_parcelles WHERE id_carnet=?", (cid,))
    conn.execute("DELETE FROM carnet_vendange WHERE id=?", (cid,))
    _carnet_recalculer_rendements(client, ids_parcelles, conn)
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/portail/<token>/carnet-vendange/parcelle-terminee', methods=['POST'])
def portail_carnet_terminee_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_marquer_terminee(client)

@app.route('/api/portail-s/<slug>/carnet-vendange/parcelle-terminee', methods=['POST'])
def portail_carnet_terminee_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_marquer_terminee(client)

def _carnet_marquer_terminee(client):
    """Bascule manuellement le statut 'terminée' d'une ou plusieurs parcelles (une
    fusion marque tous ses membres à la fois) — c'est ce statut, pas un calcul
    automatique, qui exclut une parcelle du recalcul de l'itinéraire."""
    d = request.json or {}
    ids_parcelles = [int(x) for x in (d.get('id_parcelles') or [])]
    terminee = bool(d.get('terminee'))
    campagne = d.get('campagne') or '2026'
    if not ids_parcelles:
        return jsonify({"error": "id_parcelles requis"}), 400
    conn = get_db()
    for pid in ids_parcelles:
        existing = conn.execute(
            "SELECT id FROM rendements WHERE id_parcelle=? AND id_client=? AND campagne=? ORDER BY id DESC LIMIT 1",
            (pid, client['id'], campagne)).fetchone()
        if existing:
            conn.execute("UPDATE rendements SET recolte_complete=? WHERE id=?", (1 if terminee else 0, existing['id']))
        else:
            conn.execute("""INSERT INTO rendements (id_parcelle, id_client, campagne, date_releve,
                nb_grappes_pied, poids_moyen_g, recolte_complete) VALUES (?,?,?,date('now'),0,0,?)""",
                (pid, client['id'], campagne, 1 if terminee else 0))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})


def _carnet_donnees_export(client, campagne='2026', date_filtre=None):
    """Rassemble les entrées du carnet pour construire un export — journalier si
    date_filtre est fourni (une ligne par livraison de ce jour), sinon toute la
    campagne (une seule ligne par parcelle/groupe fusionné, cumulant toutes ses
    livraisons quel que soit le nombre de jours).

    Le rendement affiché est TOUJOURS celui cumulé de la parcelle sur l'ensemble de
    la campagne (jamais celui d'une seule livraison isolée sur la surface totale,
    qui n'a pas de sens) — marqué "provisoire" tant que la parcelle n'est pas
    cochée terminée.

    L'encadré de synthèse (surface vendangée, volume récolté, volume restant,
    rendement d'exploitation) reflète TOUJOURS la situation globale de la campagne
    à l'instant présent — identique sur l'export journalier et sur l'export total,
    puisque ce sont des indicateurs d'exploitation, pas des chiffres du jour :
    - Surface vendangée et rendement : uniquement les parcelles cochées terminées,
      chacune comptée une seule fois. Une parcelle en cours n'ajoute jamais sa
      surface (impossible de savoir quelle fraction correspond à ce qui est
      déjà pesé) — elle est simplement listée à part, par son nom.
    - Volume récolté : tout ce qui a été réellement pesé, terminée ou non — c'est
      du raisin physiquement hors de la vigne, peu importe l'état de la case.
    - Volume restant à récolter : plafond autorisé (rendement d'appellation ×
      surface totale de l'exploitation) moins le volume récolté ci-dessus."""
    conn = get_db()
    toutes_entrees = dicts_from_rows(conn.execute(
        "SELECT * FROM carnet_vendange WHERE id_client=? AND campagne=? ORDER BY date, id",
        (client['id'], campagne)).fetchall())
    for e in toutes_entrees:
        e['ids'] = [r['id_parcelle'] for r in conn.execute(
            "SELECT id_parcelle FROM carnet_vendange_parcelles WHERE id_carnet=?", (e['id'],)).fetchall()]

    toutes_parcelles = dicts_from_rows(conn.execute(
        "SELECT id, nom, lieu_dit, cepage, commune, surface_cadastrale FROM parcelles WHERE id_client=?",
        (client['id'],)).fetchall())
    parc_par_id = {p['id']: p for p in toutes_parcelles}

    def est_terminee(ids):
        for pid in ids:
            row = conn.execute(
                "SELECT recolte_complete FROM rendements WHERE id_parcelle=? AND id_client=? AND campagne=? ORDER BY id DESC LIMIT 1",
                (pid, client['id'], campagne)).fetchone()
            if not row or not row['recolte_complete']:
                return False
        return True

    groupes = {}
    for e in toutes_entrees:
        if not e['ids']: continue
        cle = tuple(sorted(e['ids']))
        groupes.setdefault(cle, {'ids': list(cle), 'entries': []})['entries'].append(e)

    for cle, g in groupes.items():
        membres = [parc_par_id[pid] for pid in g['ids'] if pid in parc_par_id]
        g['surface_ares'] = sum((m.get('surface_cadastrale') or 0) * 100 for m in membres)
        g['kg_cumule'] = sum(e.get('poids_total') or 0 for e in g['entries'])
        g['caisses_cumule'] = sum(e.get('caisses') or 0 for e in g['entries'])
        g['terminee'] = est_terminee(g['ids'])
        g['rendement_cumule'] = round(g['kg_cumule'] * 100 / g['surface_ares']) if g['surface_ares'] and g['kg_cumule'] else None
        g['noms'] = " + ".join(m['nom'] for m in membres)
        g['commune'] = ", ".join(sorted(set(m.get('commune') or '' for m in membres)))
        g['cepage'] = ", ".join(sorted(set(m.get('cepage') or '' for m in membres)))

    lignes = []
    if date_filtre:
        for cle, g in groupes.items():
            for e in g['entries']:
                if e['date'] != date_filtre: continue
                lignes.append({
                    "date": e['date'], "nom": g['noms'], "commune": g['commune'], "cepage": g['cepage'],
                    "surface_ares": round(g['surface_ares'], 2),
                    "caisses": e.get('caisses'), "poids_total": e.get('poids_total'), "poids_moyen": e.get('poids_moyen'),
                    "rendement": g['rendement_cumule'], "provisoire": not g['terminee'], "note": e.get('note'),
                })
    else:
        for cle, g in groupes.items():
            if not g['kg_cumule']: continue
            dates = sorted(set(e['date'] for e in g['entries']))
            date_txt = dates[0] if len(dates) == 1 else f"{dates[0]} → {dates[-1]}"
            poids_moyen_global = round(g['kg_cumule'] / g['caisses_cumule'], 1) if g['caisses_cumule'] else None
            notes = "; ".join(e['note'] for e in g['entries'] if e.get('note'))
            lignes.append({
                "date": date_txt, "nom": g['noms'], "commune": g['commune'], "cepage": g['cepage'],
                "surface_ares": round(g['surface_ares'], 2),
                "caisses": g['caisses_cumule'], "poids_total": g['kg_cumule'], "poids_moyen": poids_moyen_global,
                "rendement": g['rendement_cumule'], "provisoire": not g['terminee'], "note": notes or None,
            })

    # Situation globale de la campagne — identique quel que soit le type d'export
    kg_recolte_total = sum(g['kg_cumule'] for g in groupes.values())
    kg_termine = sum(g['kg_cumule'] for g in groupes.values() if g['terminee'])
    surf_termine = sum(g['surface_ares'] for g in groupes.values() if g['terminee'])
    rendement_exploitation = round(kg_termine * 100 / surf_termine) if surf_termine else None
    en_cours = sorted(g['noms'] for g in groupes.values() if g['kg_cumule'] and not g['terminee'])
    nb_parcelles_renseignees = len(set(pid for g in groupes.values() if g['kg_cumule'] for pid in g['ids']))

    parametres = conn.execute(
        "SELECT * FROM parametres_appellation WHERE id_client=? AND campagne=?",
        (client['id'], campagne)).fetchone()
    volume_autorise_kg = None
    if parametres:
        total_kgha = (parametres['rendement_appellation_kgha'] or 0) + (parametres['depassement_bloque_kgha'] or 0) + (parametres['depassement_vo_kgha'] or 0)
        surf_totale_ha = sum((p.get('surface_cadastrale') or 0) for p in toutes_parcelles)
        if total_kgha and surf_totale_ha:
            volume_autorise_kg = round(total_kgha * surf_totale_ha)
    volume_restant_kg = max(0, round(volume_autorise_kg - kg_recolte_total)) if volume_autorise_kg is not None else None

    conn.close()
    return {
        "lignes": lignes,
        "surface_vendangee_ares": round(surf_termine, 2),
        "en_cours": en_cours,
        "volume_recolte_kg": round(kg_recolte_total),
        "volume_restant_kg": volume_restant_kg,
        "rendement_exploitation": rendement_exploitation,
        "nb_parcelles_renseignees": nb_parcelles_renseignees, "nb_parcelles_total": len(toutes_parcelles),
    }


def _html_pdf_carnet(client, donnees, titre, sous_titre):
    from datetime import date as _date
    d = donnees
    rows_html = ""
    commune_actuelle = None
    for l in sorted(d['lignes'], key=lambda x: (x['commune'], x['date'], x['nom'])):
        if l['commune'] != commune_actuelle:
            rows_html += f'<tr><td colspan="8" style="background:#EAF3DE;font-weight:700;color:#2D6A4F;padding:6px 8px">{l["commune"] or "—"}</td></tr>'
            commune_actuelle = l['commune']
        rendement_txt = f"{l['rendement']} kg/ha" if l['rendement'] is not None else '—'
        if l.get('provisoire') and l['rendement'] is not None:
            rendement_txt += ' <span style="color:#B7791F;font-size:9px">(provisoire)</span>'
        rows_html += f"""<tr>
            <td>{l['date']}</td>
            <td><strong>{l['nom']}</strong></td>
            <td style="font-size:10px;color:#666">{l['cepage'] or '—'}</td>
            <td>{l['surface_ares']} a</td>
            <td>{(f"{l['caisses']:g}" if l['caisses'] is not None else '—')}</td>
            <td>{l['poids_total']:.0f} kg</td>
            <td>{l['poids_moyen']:.1f} kg/caisse</td>
            <td>{rendement_txt}</td>
        </tr>""" if l.get('poids_total') and l.get('poids_moyen') else f"""<tr>
            <td>{l['date']}</td>
            <td><strong>{l['nom']}</strong></td>
            <td style="font-size:10px;color:#666">{l['cepage'] or '—'}</td>
            <td>{l['surface_ares']} a</td>
            <td>{(f"{l['caisses']:g}" if l['caisses'] is not None else '—')}</td>
            <td colspan="3" style="color:#999">Pas encore pesée</td>
        </tr>"""
        if l.get('note'):
            rows_html += f'<tr><td></td><td colspan="7" style="font-size:10px;color:#666;font-style:italic;padding-top:0">Remarque : {l["note"]}</td></tr>'

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:Arial,sans-serif;font-size:11.5px;margin:20px;color:#1a1a1a}}
  h1{{color:#2D6A4F;font-size:18px;margin-bottom:4px}}
  .sub{{color:#666;font-size:11px;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
  th{{background:#2D6A4F;color:#fff;padding:6px 8px;font-size:10px;text-align:left}}
  td{{padding:5px 8px;border-bottom:.5px solid #e8e6e1;vertical-align:top}}
  .resume{{display:flex;gap:16px;background:#EAF3DE;padding:10px 14px;border-radius:8px;margin-bottom:8px;flex-wrap:wrap}}
  .resume div{{text-align:center}}
  .resume .val{{font-size:18px;font-weight:700;color:#2D6A4F}}
  .resume .lbl{{font-size:9.5px;color:#666}}
  .encours{{font-size:10px;color:#8a5a1a;margin-bottom:16px}}
  .footer{{color:#999;font-size:10px;margin-top:24px;border-top:.5px solid #e0ddd8;padding-top:8px}}
</style></head><body>
<h1>{titre}</h1>
<p class="sub">{client['exploitation']} · {sous_titre} · Généré le {_date.today().strftime('%d/%m/%Y')} · MatuScore</p>
<div class="resume">
  <div><div class="val">{d['surface_vendangee_ares']} a</div><div class="lbl">Surface vendangée (terminée)</div></div>
  <div><div class="val">{d['volume_recolte_kg']:.0f} kg</div><div class="lbl">Volume récolté</div></div>
  <div><div class="val">{(f"{d['volume_restant_kg']:.0f} kg" if d['volume_restant_kg'] is not None else '—')}</div><div class="lbl">Volume restant à récolter</div></div>
  <div><div class="val">{d['rendement_exploitation'] if d['rendement_exploitation'] is not None else '—'} kg/ha</div><div class="lbl">Rendement exploitation (provisoire)</div></div>
</div>
{f'<div class="encours">En cours : ' + ', '.join(n + ' - en cours' for n in d['en_cours']) + '</div>' if d['en_cours'] else ''}
<table>
  <thead><tr><th>Date</th><th>Parcelle(s)</th><th>Cépage</th><th>Surface</th><th>Caisses</th><th>Poids total</th><th>Poids moyen</th><th>Rendement</th></tr></thead>
  <tbody>{rows_html if rows_html else '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">Aucune saisie</td></tr>'}</tbody>
</table>
<div class="footer">Carnet de vendange — VITI Sens / MatuScore</div>
</body></html>"""


def _carnet_export_pdf_response(client, donnees, titre, sous_titre, nom_fichier):
    html = _html_pdf_carnet(client, donnees, titre, sous_titre)
    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        return pdf, 200, {"Content-Type": "application/pdf", "Content-Disposition": f"attachment; filename={nom_fichier}"}
    except Exception as e:
        print(f"[pdf] weasyprint indisponible ou en erreur ({e}) — repli HTML imprimable")
        return html + "<script>window.print()</script>", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route('/api/portail/<token>/carnet-vendange/export-pdf')
def portail_carnet_export_pdf_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_export_pdf(client)

@app.route('/api/portail-s/<slug>/carnet-vendange/export-pdf')
def portail_carnet_export_pdf_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_export_pdf(client)

def _carnet_export_pdf(client):
    date_j = request.args.get('date')
    from datetime import date as _date
    if not date_j:
        date_j = _date.today().isoformat()
    donnees = _carnet_donnees_export(client, date_filtre=date_j)
    safe = client['exploitation'].replace(' ', '_').replace('/', '-')
    return _carnet_export_pdf_response(client, donnees, "Carnet de vendange — Journée",
        f"Journée du {date_j}", f"Carnet_{safe}_{date_j}.pdf")


@app.route('/api/portail/<token>/carnet-vendange/export-pdf-total')
def portail_carnet_export_total_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _carnet_export_total(client)

@app.route('/api/portail-s/<slug>/carnet-vendange/export-pdf-total')
def portail_carnet_export_total_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _carnet_export_total(client)

def _carnet_export_total(client):
    donnees = _carnet_donnees_export(client)
    safe = client['exploitation'].replace(' ', '_').replace('/', '-')
    return _carnet_export_pdf_response(client, donnees, "Carnet de vendange — Récapitulatif complet",
        "Campagne 2026", f"Carnet_{safe}_2026_total.pdf")


# ===== Paramètres d'appellation (rendement autorisé, dépassements) — persistés
# indépendamment de l'itinéraire, pour que le carnet puisse calculer le volume
# restant à récolter même si l'itinéraire n'a jamais été calculé =====

@app.route('/api/portail/<token>/appellation-params', methods=['GET'])
def portail_appellation_get_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _appellation_params_get(client)

@app.route('/api/portail-s/<slug>/appellation-params', methods=['GET'])
def portail_appellation_get_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _appellation_params_get(client)

def _appellation_params_get(client):
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM parametres_appellation WHERE id_client=? AND campagne=?",
        (client['id'], campagne)).fetchone()
    conn.close()
    if not row:
        return jsonify({"rendement_appellation_kgha": None, "depassement_bloque_kgha": None, "depassement_vo_kgha": None})
    return jsonify(dict_from_row(row))


@app.route('/api/portail/<token>/appellation-params', methods=['POST'])
def portail_appellation_save_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _appellation_params_save(client)

@app.route('/api/portail-s/<slug>/appellation-params', methods=['POST'])
def portail_appellation_save_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _appellation_params_save(client)

def _appellation_params_save(client):
    d = request.json or {}
    campagne = d.get('campagne') or '2026'
    conn = get_db()
    conn.execute("""INSERT INTO parametres_appellation
        (id_client, campagne, rendement_appellation_kgha, depassement_bloque_kgha, depassement_vo_kgha, updated_at)
        VALUES (?,?,?,?,?, datetime('now'))
        ON CONFLICT(id_client, campagne) DO UPDATE SET
            rendement_appellation_kgha=excluded.rendement_appellation_kgha,
            depassement_bloque_kgha=excluded.depassement_bloque_kgha,
            depassement_vo_kgha=excluded.depassement_vo_kgha,
            updated_at=excluded.updated_at""",
        (client['id'], campagne, d.get('rendement_appellation_kgha'), d.get('depassement_bloque_kgha'), d.get('depassement_vo_kgha')))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== Sauvegarde serveur de l'itinéraire (persiste entre appareils) =====

@app.route('/api/portail/<token>/itineraire/sauvegarde', methods=['GET'])
def portail_itineraire_get_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _itineraire_get(client)

@app.route('/api/portail-s/<slug>/itineraire/sauvegarde', methods=['GET'])
def portail_itineraire_get_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _itineraire_get(client)

def _itineraire_get(client):
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    row = conn.execute(
        "SELECT data_json, updated_at FROM itineraire_sauvegarde WHERE id_client=? AND campagne=?",
        (client['id'], campagne)).fetchone()
    conn.close()
    if not row:
        return jsonify({"data": None})
    return jsonify({"data": json.loads(row['data_json']), "updated_at": row['updated_at']})


@app.route('/api/portail/<token>/itineraire/sauvegarde', methods=['POST'])
def portail_itineraire_save_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _itineraire_save(client)

@app.route('/api/portail-s/<slug>/itineraire/sauvegarde', methods=['POST'])
def portail_itineraire_save_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _itineraire_save(client)

def _itineraire_save(client):
    j = request.json or {}
    campagne = j.get('campagne', '2026')
    data = j.get('data')
    if data is None:
        return jsonify({"error": "data manquant"}), 400
    conn = get_db()
    conn.execute("""INSERT INTO itineraire_sauvegarde (id_client, campagne, data_json, updated_at)
        VALUES (?,?,?, datetime('now'))
        ON CONFLICT(id_client, campagne) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at""",
        (client['id'], campagne, json.dumps(data)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/portail/<token>/itineraire/sauvegarde', methods=['DELETE'])
def portail_itineraire_delete_token(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _itineraire_delete(client)

@app.route('/api/portail-s/<slug>/itineraire/sauvegarde', methods=['DELETE'])
def portail_itineraire_delete_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _itineraire_delete(client)

def _itineraire_delete(client):
    campagne = request.args.get('campagne', '2026')
    conn = get_db()
    conn.execute("DELETE FROM itineraire_sauvegarde WHERE id_client=? AND campagne=?", (client['id'], campagne))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== Dates d'ouverture — consultation et saisie par exploitation =====

@app.route('/api/portail/<token>/dates-ouverture', methods=['GET'])
def portail_dates_ouverture(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _dates_ouverture_exploitation(client)

@app.route('/api/portail-s/<slug>/dates-ouverture', methods=['GET'])
def portail_dates_ouverture_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _dates_ouverture_exploitation(client)

def _dates_ouverture_exploitation(client):
    """Les couples commune/cépage réellement présents dans les parcelles de ce
    compte, avec la date d'ouverture connue (si renseignée) — pour n'afficher que
    ce qui concerne cette exploitation, pas tout le référentiel Champagne.
    Correspondance normalisée (accents/tirets/casse ignorés), pas égalité stricte."""
    conn = get_db()
    combos = dicts_from_rows(conn.execute("""
        SELECT DISTINCT commune, cepage FROM parcelles
        WHERE id_client=? AND commune IS NOT NULL AND commune != ''
    """, (client['id'],)).fetchall())
    dates = dicts_from_rows(conn.execute(
        "SELECT * FROM dates_ouverture WHERE campagne='2026'"
    ).fetchall())
    conn.close()

    dates_norm = [dict(d, _commune_n=_normaliser_commune(d['commune']),
                        _cepage_n=_normaliser_commune(d['cepage']) if d['cepage'] else None)
                  for d in dates]

    def date_pour(commune, cepage):
        commune_n = _normaliser_commune(commune)
        cepage_n = _normaliser_commune(cepage) if cepage else None
        specifique = next((d for d in dates_norm if d['_commune_n'] == commune_n and d['_cepage_n'] == cepage_n), None)
        if specifique: return specifique
        return next((d for d in dates_norm if d['_commune_n'] == commune_n and d['_cepage_n'] is None), None)

    resultat = []
    for c in combos:
        d = date_pour(c['commune'], c['cepage'])
        resultat.append({
            "commune": c['commune'], "cepage": c['cepage'],
            "date_ouverture": d['date_ouverture'] if d else None,
            "source": d['source'] if d else None,
        })
    return jsonify(resultat)

@app.route('/api/portail/<token>/dates-ouverture', methods=['POST'])
def portail_ajouter_date_ouverture(token):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _ajouter_date_ouverture(request.json or {}, source='vigneron')

@app.route('/api/portail-s/<slug>/dates-ouverture', methods=['POST'])
def portail_ajouter_date_ouverture_slug(slug):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _ajouter_date_ouverture(request.json or {}, source='vigneron')


@app.route('/api/portail/<token>/parcelles/<int:pid>/derogation', methods=['POST'])
def portail_derogation_token(token, pid):
    client = dict_from_row(get_db().execute("SELECT * FROM clients WHERE portail_token=?", (token,)).fetchone())
    if not client: return jsonify({"error": "Token invalide"}), 404
    return _basculer_derogation(client, pid)

@app.route('/api/portail-s/<slug>/parcelles/<int:pid>/derogation', methods=['POST'])
def portail_derogation_slug(slug, pid):
    client = get_client_by_token_or_slug(slug)
    if not client: return jsonify({"error": "Lien invalide"}), 404
    return _basculer_derogation(client, pid)

def _basculer_derogation(client, pid):
    d = request.json or {}
    active = bool(d.get('derogation'))
    conn = get_db()
    parc = conn.execute("SELECT id FROM parcelles WHERE id=? AND id_client=?", (pid, client['id'])).fetchone()
    if not parc:
        conn.close()
        return jsonify({"error": "Parcelle introuvable"}), 404
    conn.execute("UPDATE parcelles SET derogation_ouverture=? WHERE id=?", (1 if active else 0, pid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "derogation": active})


def _saisir_recolte_jour(client):
    """
    Saisie récolte journalière — accumule les kg coupés par parcelle par défaut,
    ou remplace le total existant si mode='remplacer' (correction d'une saisie
    erronée, sans revenir en arrière manuellement dans le tableau rendement).
    Calcule le rendement réel provisoire = total_kg / surface_ha.
    Si recolte_complete=True, le rendement est définitif.
    """
    d = request.json or {}
    pid          = d.get('id_parcelle')
    kg_jour      = float(d.get('kg_recolte') or 0)
    date_recolte = d.get('date_recolte')
    surf_prov_ares = float(d.get('surface_provisoire_ares') or 0)
    mode = d.get('mode') or 'ajouter'  # 'ajouter' (défaut) ou 'remplacer'
    # is_fraction=True → parcelle fractionnée sur plusieurs jours → provisoire
    is_fraction  = bool(d.get('is_fraction', False))
    recolte_complete = not is_fraction  # complet si pas de fraction

    if not pid or kg_jour <= 0:
        return jsonify({"error": "id_parcelle et kg_recolte requis"}), 400

    conn = get_db()
    parc = dict_from_row(conn.execute(
        "SELECT * FROM parcelles WHERE id=? AND id_client=?", (pid, client['id'])
    ).fetchone())
    if not parc: conn.close(); return jsonify({"error": "Parcelle introuvable"}), 404

    surf_ha = parc.get('surface_cadastrale')  # surface totale en ha

    # Chercher ou créer l'entrée rendement pour cette parcelle
    existing = dict_from_row(conn.execute(
        "SELECT * FROM rendements WHERE id_parcelle=? AND id_client=? AND campagne='2026' ORDER BY id DESC LIMIT 1",
        (pid, client['id'])
    ).fetchone())

    if existing:
        if mode == 'remplacer':
            total_kg = kg_jour
            obs = (existing.get('observations') or '').split(' [')[0] + f' [remplacé par {kg_jour} kg le {date_recolte}]'
        else:
            total_kg = float(existing.get('kg_recoltes_total') or 0) + kg_jour
            obs = (existing.get('observations') or '') + f' [+{kg_jour} kg le {date_recolte}]'
        # Rendement réel = total_kg / surface_ha
        rdt_reel = round(total_kg / float(surf_ha)) if surf_ha and float(surf_ha) > 0 else None
        conn.execute(
            "UPDATE rendements SET kg_recoltes_total=?, rendement_reel_kgha=?, recolte_complete=?, observations=? WHERE id=?",
            (total_kg, rdt_reel, 1 if recolte_complete else existing.get('recolte_complete', 0), obs, existing['id'])
        )
        rid = existing['id']
    else:
        total_kg = kg_jour
        rdt_reel = round(total_kg / float(surf_ha)) if surf_ha and float(surf_ha) > 0 else None
        cur = conn.execute("""INSERT INTO rendements
            (id_parcelle, id_client, campagne, date_releve, nb_grappes_pied,
             poids_moyen_g, nb_pieds_ha, rendement_kgha, rendement_reel_kgha,
             kg_recoltes_total, recolte_complete, observations)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, client['id'], '2026',
             date_recolte or __import__('datetime').date.today().isoformat(),
             0, 0, parc.get('nb_pieds_ha'), None, rdt_reel,
             total_kg, 1 if recolte_complete else 0,
             f'Récolte {date_recolte}: {kg_jour} kg'))
        rid = cur.lastrowid

    # Rendement provisoire sur la surface du jour (si fournie)
    rdt_provisoire = None
    if surf_prov_ares > 0:
        rdt_provisoire = round((kg_jour * 100) / surf_prov_ares)  # kg/ha

    conn.commit(); conn.close()
    return jsonify({
        "ok": True,
        "total_kg": total_kg,
        "rendement_reel_kgha": rdt_reel,
        "rendement_provisoire_kgha": rdt_provisoire,
        "recolte_complete": recolte_complete,
        "provisoire": not recolte_complete
    })

    conn.commit(); conn.close()
    return jsonify({"ok": True, "rendement_reel_kgha": rdt_reel_kgha})


def _calc_date_optimale(client):
    """
    Date de début de vendange optimale.
    Cible : moyenne pondérée 10.2% vol (idéal 10.5%).
    Règle absolue : aucune parcelle ne peut descendre sous 9.5% vol.
    AT intégré si disponible (dynamique réelle, sinon -0.20 g/L/jour).
    Retourne le premier jour où :
      - moy_ponderee >= 10.2% (cible)
      - aucune parcelle < 9.0% (plancher absolu par parcelle)
    Si impossible → retourne le meilleur compromis.
    """
    from datetime import date, timedelta

    cid  = client['id']
    conn = get_db()

    parcelles = dicts_from_rows(conn.execute(
        "SELECT * FROM parcelles WHERE id_client=? ORDER BY commune, nom", (cid,)
    ).fetchall())
    if not parcelles:
        conn.close()
        return jsonify({"error": "Aucune parcelle"}), 404

    fiches_last = dicts_from_rows(conn.execute("""
        SELECT f.* FROM maturite_fiches f
        WHERE f.id_client=? AND f.campagne='2026'
          AND f.id=(SELECT MAX(id) FROM maturite_fiches f2
                    WHERE f2.id_parcelle=f.id_parcelle
                      AND f2.id_client=f.id_client AND f2.campagne='2026')
    """, (cid,)).fetchall())

    toutes_fiches = dicts_from_rows(conn.execute("""
        SELECT id_parcelle, date_fiche, degre_probable, AT
        FROM maturite_fiches
        WHERE id_client=? AND campagne='2026'
        ORDER BY id_parcelle, date_fiche ASC
    """, (cid,)).fetchall())
    conn.close()

    fiches_by_pid     = {f['id_parcelle']: f for f in fiches_last}
    fiches_all_by_pid = {}
    for f in toutes_fiches:
        fiches_all_by_pid.setdefault(f['id_parcelle'], []).append(f)

    profils = []
    for p in parcelles:
        pid   = p['id']
        fiche = fiches_by_pid.get(pid)
        if not fiche: continue
        degre   = fiche.get('degre_probable')
        at_val  = fiche.get('AT')
        d_fiche = fiche.get('date_fiche')
        if degre is None or not d_fiche: continue

        toutes  = fiches_all_by_pid.get(pid, [])
        dyn_d, _ = _dynamique_degre(toutes) if len(toutes) >= 2 else (0.15, 1)
        dyn_at, has_at = _dynamique_at(toutes) if len(toutes) >= 2 else (-0.20, False)
        surf    = float(p.get('surface_cadastrale') or 0)

        # hL = rendement_kgha * surf / 160 (160 kg/hL, règle Champagne)
        # On récupère le dernier relevé rendement si dispo
        profils.append({
            'pid':    pid,
            'nom':    p['nom'],
            'cepage': p.get('cepage',''),
            'commune':p.get('commune',''),
            'degre':  float(degre),
            'at':     float(at_val) if at_val else None,
            'd_fiche':d_fiche,
            'dyn_d':  dyn_d,
            'dyn_at': dyn_at,
            'has_at': has_at or (at_val is not None),
            'surf':   surf,
            'hl':     0,  # sera rempli après récupération des rendements
        })

    if not profils:
        return jsonify({"error": "Aucune parcelle avec relevé de maturité"}), 404

    # Récupérer les rendements pour pondération hL
    conn2 = get_db()
    rdts = dicts_from_rows(conn2.execute(
        "SELECT id_parcelle, rendement_kgha FROM rendements WHERE id_client=? AND campagne='2026'",
        (cid,)
    ).fetchall())
    conn2.close()
    rdt_by_pid = {r['id_parcelle']: float(r['rendement_kgha']) for r in rdts if r.get('rendement_kgha')}
    # Rendement moyen exploitation pour les parcelles sans relevé
    moy_rdt_exploit = (sum(rdt_by_pid.values()) / len(rdt_by_pid)) if rdt_by_pid else 8800.0
    for p in profils:
        rdt = rdt_by_pid.get(p['pid'], moy_rdt_exploit)
        p['hl'] = max(0.01, rdt * p['surf'] / 160.0)  # 160 kg = 1 hL Champagne

    today = date.today()

    def proj(profil, d):
        """Projette degré et AT à la date d."""
        jours = max(0, (d - date.fromisoformat(profil['d_fiche'][:10])).days)
        deg   = round(float(profil['degre']) + profil['dyn_d'] * jours, 2)
        at_p  = None
        if profil['at'] is not None:
            at_p = round(float(profil['at']) + profil['dyn_at'] * jours, 1)
        return deg, at_p

    def score_jour(d):
        """
        Score global à la date d.
        - moy_deg : moyenne pondérée degré
        - min_deg : minimum parcelle (plancher 9.0%)
        - moy_at  : moyenne pondérée AT (si dispo)
        """
        degs = []
        ats  = []
        for p in profils:
            deg, at_p = proj(p, d)
            w = p['hl']  # pondéré par hL produits (rdt × surf / 160 kg/hL)
            degs.append((deg, w))
            if at_p is not None: ats.append((at_p, w))

        total_w  = sum(w for _,w in degs) or len(degs)
        moy_deg  = sum(deg*w for deg,w in degs) / total_w
        min_deg  = min(deg for deg,_ in degs)
        moy_at   = None
        if ats:
            total_w_at = sum(w for _,w in ats) or len(ats)
            moy_at = round(sum(at*w for at,w in ats) / total_w_at, 1)
        return round(moy_deg, 2), round(min_deg, 2), moy_at

    CIBLE_DEG    = 10.2   # cible optimale
    IDEAL_DEG    = 10.5   # idéal
    PLANCHER_DEG = 9.0    # plancher bas — aucune parcelle en dessous
    PLAFOND_DEG  = 11.0   # plafond haut — moyenne cuverie prioritaire
    PLANCHER_ABS = 9.0

    date_opt    = None
    date_ideal  = None
    date_plafond= None    # premier jour où moy > 11% (limite haute)
    best_score  = None
    best_date   = None
    resultats   = []

    for delta in range(61):
        d         = today + timedelta(days=delta)
        moy, mini, moy_at = score_jour(d)
        resultats.append({
            "date": d.isoformat(),
            "moy_degre": moy,
            "min_degre": mini,
            "moy_at":    moy_at
        })

        # Plafond haut : premier jour où moy dépasse 11%
        if date_plafond is None and moy > PLAFOND_DEG:
            date_plafond = d

        # Plancher absolu respecté ET plafond non dépassé
        plancher_ok = mini >= PLANCHER_DEG
        plafond_ok  = moy <= PLAFOND_DEG

        # Premier jour cible (>= 10.2%) avec plancher OK et sous le plafond
        if date_opt is None and plancher_ok and plafond_ok and moy >= CIBLE_DEG:
            date_opt = d

        # Premier jour idéal (>= 10.5%) avec plancher OK et sous le plafond
        if date_ideal is None and plancher_ok and plafond_ok and moy >= IDEAL_DEG:
            date_ideal = d

        # Meilleur compromis
        if best_score is None or (plancher_ok and plafond_ok and abs(moy - CIBLE_DEG) < abs(best_score - CIBLE_DEG)):
            best_score = moy
            best_date  = d

    # Si le plafond est atteint avant la cible → vendanger avant
    if date_plafond and (date_opt is None or date_plafond < date_opt):
        # La date limite haute est prioritaire
        date_retenue = date_plafond - timedelta(days=1)  # jour avant le dépassement
    else:
        date_retenue = date_ideal or date_opt or best_date

    moy_ret, mini_ret, moy_at_ret = score_jour(date_retenue)
    plancher_ok   = mini_ret >= PLANCHER_DEG

    # Bilan par parcelle à la date retenue
    bilan = []
    for p in profils:
        deg, at_p = proj(p, date_retenue)
        if deg < PLANCHER_DEG:
            statut, color = "risque", "#C62828"
        elif deg < CIBLE_DEG:
            statut, color = "acceptable", "#E65100"
        elif deg <= IDEAL_DEG:
            statut, color = "optimal", "#2E7D32"
        else:
            statut, color = "surmaturité", "#7B1FA2"
        bilan.append({
            "nom":          p['nom'],
            "cepage":       p['cepage'],
            "commune":      p['commune'],
            "degre_actuel": p['degre'],
            "degre_estime": deg,
            "at_estime":    at_p,
            "dyn_degre":    p['dyn_d'],
            "has_at":       p['has_at'],
            "statut":       statut,
            "color":        color,
        })

    ordre = {"surmaturité": 0, "optimal": 1, "acceptable": 2, "risque": 3}
    bilan.sort(key=lambda x: ordre[x['statut']])

    return jsonify({
        "date_optimale":      date_retenue.isoformat(),
        "date_cible":         date_opt.isoformat() if date_opt else None,
        "date_ideale":        date_ideal.isoformat() if date_ideal else None,
        "date_plafond":       date_plafond.isoformat() if date_plafond else None,
        "plafond_prioritaire":bool(date_plafond and (date_opt is None or date_plafond <= date_opt)),
        "moy_degre_cuverie":  moy_ret,
        "moy_at_cuverie":     moy_at_ret,
        "min_degre":          mini_ret,
        "plancher_respecte":  plancher_ok,
        "nb_parcelles":       len(profils),
        "bilan":              bilan,
        "courbe":             resultats,
        "atteint_cible":      moy_ret >= CIBLE_DEG,
        "atteint_ideal":      moy_ret >= IDEAL_DEG,
    })



def _calc_itineraire(client):
    """
    Itinéraire de vendange — toutes les parcelles.
    Vendange continue : une fois lancée, on ne s'arrête plus.
    Tri par date_theo croissante (ordre des fenêtres).
    """
    from datetime import date, timedelta
    import math

    j            = request.json or {}
    objectif     = float(j.get('objectif_kg_jour', 5000))
    date_debut_str  = j.get('date_debut')
    rdt_estime   = j.get('rdt_moyen_estime')
    use_cep_dates= j.get('use_cepage_dates', False)
    dates_cibles_cepage = j.get('dates_cibles_cepage', {})  # dates validées par l'utilisateur
    cid          = client['id']

    POIDS_DEFAULT  = {'Meunier': 125, 'Pinot Noir': 135, 'Chardonnay': 145}
    DENSITE_DEFAULT= 9000
    CEP_ORDER      = {'Chardonnay': 0, 'Pinot Noir': 1, 'Meunier': 2}

    conn = get_db()
    parcelles = dicts_from_rows(conn.execute(
        "SELECT * FROM parcelles WHERE id_client=? ORDER BY commune, nom", (cid,)
    ).fetchall())
    if not parcelles:
        conn.close()
        return jsonify({'error': 'Aucune parcelle enregistrée'}), 404

    fiches_raw = dicts_from_rows(conn.execute("""
        SELECT f.* FROM maturite_fiches f
        WHERE f.id_client=? AND f.campagne='2026'
          AND f.id=(SELECT MAX(id) FROM maturite_fiches f2
                    WHERE f2.id_parcelle=f.id_parcelle AND f2.id_client=f.id_client
                    AND f2.campagne='2026')
    """, (cid,)).fetchall())
    fiches_raw    = _enrichir_fiches(fiches_raw)
    fiches_by_pid = {f['id_parcelle']: f for f in fiches_raw}

    toutes_fiches = dicts_from_rows(conn.execute("""
        SELECT id_parcelle, date_fiche, degre_probable
        FROM maturite_fiches
        WHERE id_client=? AND campagne='2026'
        ORDER BY id_parcelle, date_fiche ASC
    """, (cid,)).fetchall())
    fiches_by_pid_all = {}
    for f in toutes_fiches:
        fiches_by_pid_all.setdefault(f['id_parcelle'], []).append(f)

    rdts = dicts_from_rows(conn.execute("""
        SELECT r.id_parcelle, r.rendement_kgha, p.commune, p.cepage
        FROM rendements r JOIN parcelles p ON r.id_parcelle=p.id
        WHERE r.id_client=? AND r.campagne='2026' AND r.rendement_kgha IS NOT NULL
    """, (cid,)).fetchall())
    kg_recoltes_by_pid = {r[0]: r[1] for r in conn.execute("""
        SELECT id_parcelle, kg_recoltes_total FROM rendements
        WHERE id_client=? AND campagne='2026' AND kg_recoltes_total IS NOT NULL
    """, (cid,)).fetchall()}
    conn.close()

    rdt_by_pid    = {}
    rdt_by_comcep = {}
    for r in rdts:
        rdt_by_pid.setdefault(r['id_parcelle'], []).append(r['rendement_kgha'])
        rdt_by_comcep.setdefault((r['commune'], r['cepage']), []).append(r['rendement_kgha'])
    moy_exploitation = round(sum(r['rendement_kgha'] for r in rdts)/len(rdts)) if rdts else None

    # Index dates théoriques par (commune, cépage) et par cépage
    dates_by_comcep = {}
    dates_by_cep    = {}
    for f in fiches_raw:
        dr = f.get('date_recolte') or {}
        d102 = dr.get('date_102')
        if not d102: continue
        pid = f['id_parcelle']
        parc = next((p for p in parcelles if p['id']==pid), None)
        if not parc: continue
        com = (parc.get('commune') or '').strip().upper()
        cep = parc.get('cepage') or ''
        dates_by_comcep.setdefault((com, cep), []).append(d102)
        dates_by_cep.setdefault(cep, []).append(d102)

    def moy_date(date_list):
        if not date_list: return None
        import datetime
        ts = [datetime.date.fromisoformat(d).toordinal() for d in date_list]
        return datetime.date.fromordinal(round(sum(ts)/len(ts))).isoformat()

    if not rdts and not rdt_estime:
        return jsonify({'needs_estimate': True,
                        'nb_parcelles': len(parcelles),
                        'cepages': list(set(p.get('cepage','') for p in parcelles if p.get('cepage')))})

    sans_fiche = [p for p in parcelles if p['id'] not in fiches_by_pid]
    if sans_fiche:
        needs_confirm = []
        needs_date    = []
        for p in sans_fiche:
            com = (p.get('commune') or '').strip().upper()
            cep = p.get('cepage') or ''
            if dates_by_comcep.get((com, cep)):
                pass
            elif dates_by_cep.get(cep) and not use_cep_dates:
                needs_confirm.append({'nom': p['nom'], 'cepage': cep, 'commune': com})
            elif not dates_by_cep.get(cep) and not date_debut_str:
                needs_date.append({'nom': p['nom'], 'cepage': cep, 'commune': com})

        if needs_confirm:
            examples = {}
            for p in needs_confirm:
                cep = p['cepage']
                if cep not in examples and dates_by_cep.get(cep):
                    examples[cep] = moy_date(dates_by_cep[cep])
            return jsonify({'needs_cepage_confirm': True, 'parcelles': needs_confirm, 'cepage_dates': examples})

        if needs_date:
            return jsonify({'needs_date': True,
                            'parcelles': [p['nom'] for p in needs_date],
                            'message': 'Aucune référence de maturité disponible pour certaines parcelles.'})

    if date_debut_str:
        date_debut = date.fromisoformat(date_debut_str)
    else:
        all_102 = [d for ds in dates_by_comcep.values() for d in ds]
        date_debut = date.fromisoformat(min(all_102)) if all_102 else date.today()

    parcelles_file = []
    for p in parcelles:
        pid    = p['id']
        cepage = p.get('cepage') or ''
        commune= (p.get('commune') or '').strip().upper()
        fiche  = fiches_by_pid.get(pid)

        if rdt_by_pid.get(pid):
            rdt_kgha   = round(sum(rdt_by_pid[pid])/len(rdt_by_pid[pid]))
            rdt_source = 'terrain'
        elif rdt_by_comcep.get((commune, cepage)):
            vals = rdt_by_comcep[(commune, cepage)]
            rdt_kgha   = round(sum(vals)/len(vals))
            rdt_source = f'commune ({commune})'
        elif moy_exploitation:
            rdt_kgha   = moy_exploitation
            rdt_source = 'moyenne exploitation'
        elif rdt_estime:
            rdt_kgha   = float(rdt_estime)
            rdt_source = 'estimation'
        else:
            poids    = POIDS_DEFAULT.get(cepage, 130)
            rdt_kgha = round(poids/1000 * DENSITE_DEFAULT * 1.5)
            rdt_source = f'défaut ({cepage})'

        surf_ha  = p.get('surface_cadastrale')
        kg_total = round(float(rdt_kgha) * float(surf_ha)) if surf_ha and float(surf_ha) > 0 else None

        if fiche:
            dr       = fiche.get('date_recolte') or {}
            date_102 = dr.get('date_102')
            degre    = fiche.get('degre_probable')
            if date_102:
                date_theo = date_102
            elif degre and float(degre) >= 10.2:
                date_theo = fiche.get('date_fiche') or date_debut.isoformat()
            else:
                date_theo = date_debut.isoformat()
        else:
            ref_comcep = moy_date(dates_by_comcep.get((commune, cepage), []))
            ref_cep    = moy_date(dates_by_cep.get(cepage, []))
            date_theo  = ref_comcep or (ref_cep if use_cep_dates else None) or date_debut.isoformat()

        # Dynamique réelle
        toutes = fiches_by_pid_all.get(pid, [])
        dyn, _ = _dynamique_degre(toutes) if len(toutes) >= 2 else (0.15, 1)
        degre_actuel   = float(fiche.get('degre_probable') or 0) if fiche else 0
        date_fiche_str = fiche.get('date_fiche') if fiche else None
        degre_estime_recolte = None
        if fiche and degre_actuel > 0 and date_fiche_str and date_theo:
            try:
                jours = (date.fromisoformat(date_theo) - date.fromisoformat(date_fiche_str[:10])).days
                if jours >= 0:
                    degre_estime_recolte = round(degre_actuel + dyn * jours, 1)
            except: pass

        parcelles_file.append({
            'id':                   pid,
            'nom':                  p['nom'],
            'cepage':               cepage,
            'commune':              commune,
            'surface_ha':           surf_ha,
            'degre':                fiche.get('degre_probable') if fiche else None,
            'degre_estime_recolte': degre_estime_recolte,
            'dyn_degre':            dyn,
            'date_fiche_dernier':   date_fiche_str,
            'score':                fiche.get('score_total') if fiche else None,
            'verdict':              fiche.get('verdict','') if fiche else '',
            'date_theo':            date_theo,
            'rdt_kgha':             rdt_kgha,
            'rdt_source':           rdt_source,
            'kg_total':             kg_total,
            'kg_restants':          kg_total,
        })

    # ── Dates cibles par cépage : validées par l'utilisateur ou calculées ────────
    cepages_uniq = list({p['cepage'] for p in parcelles_file})
    date_cible_cepage = {}
    for cep in cepages_uniq:
        if cep in dates_cibles_cepage and dates_cibles_cepage[cep]:
            # Date validée par l'utilisateur dans le modal
            date_cible_cepage[cep] = dates_cibles_cepage[cep]
        else:
            # Calcul automatique : moy pondérée hL >= 10.2%
            pp_cep = [p for p in parcelles_file if p['cepage'] == cep]
            tw = sum(max(0.01, p.get('hl', p.get('surf', 0) or 0.01)) for p in pp_cep)
            for delta in range(90):
                d_test = date_debut + timedelta(days=delta)
                moy = sum(
                    (float(p.get('degre') or 0) + p.get('dyn_d', 0.15) * max(0, (d_test - date.fromisoformat(str(p.get('d_fiche', d_test.isoformat()))[:10])).days))
                    * max(0.01, p.get('hl', p.get('surf', 0) or 0.01))
                    for p in pp_cep
                ) / tw if tw > 0 else 0
                if moy >= 10.2:
                    date_cible_cepage[cep] = d_test.isoformat()
                    break
            if cep not in date_cible_cepage:
                date_cible_cepage[cep] = (date_debut + timedelta(days=60)).isoformat()

    # Ajuster date_theo de chaque parcelle au max(date_theo, date_cible_cepage)
    for p in parcelles_file:
        dc = date_cible_cepage.get(p['cepage'], p['date_theo'])
        if p['date_theo'] < dc:
            p['date_theo'] = dc

    # ── Communes triées par degré moyen décroissant (plus mûre en premier) ─────
    commune_degres = {}
    for p in parcelles_file:
        com = p['commune']
        if com not in commune_degres:
            commune_degres[com] = []
        if p.get('degre'):
            commune_degres[com].append(p['degre'])
    communes_ordre = sorted(
        commune_degres.keys(),
        key=lambda c: -(sum(commune_degres[c]) / len(commune_degres[c]) if commune_degres[c] else 0)
    )

    # ── File ordonnée : commune(deg max) → cépage(date cible) → degré parcelle ─
    file_ordonnee = []
    for com in communes_ordre:
        ceps_com = sorted(
            {p['cepage'] for p in parcelles_file if p['commune'] == com},
            key=lambda cep: date_cible_cepage.get(cep, '9999-99-99')
        )
        for cep in ceps_com:
            parcelles_com_cep = sorted(
                [p for p in parcelles_file if p['commune'] == com and p['cepage'] == cep],
                key=lambda p: p['date_theo']
            )
            file_ordonnee.extend(parcelles_com_cep)

    idx_map = {p['id']: i for i, p in enumerate(file_ordonnee)}
    parcelles_file.sort(key=lambda p: idx_map.get(p['id'], 999))

    # ── Plafond "volume à vendanger" selon l'appellation ────────────────────
    # Rendement autorisé + dépassement toléré (réserve bloquée + VO), en kg/ha.
    # Au-delà, le volume n'est pas inclus dans l'itinéraire (pas de débouché).
    rendement_appellation_kgha = float(j.get('rendement_appellation_kgha') or 0)
    depassement_bloque_kgha    = float(j.get('depassement_bloque_kgha') or 0)
    depassement_vo_kgha        = float(j.get('depassement_vo_kgha') or 0)
    surface_totale = sum(float(p['surface_ha']) for p in parcelles_file if p.get('surface_ha'))

    volume_autorise_kg   = None
    volume_non_recolte_kg = 0
    kg_exploitation_brut = sum(p['kg_total'] for p in parcelles_file if p['kg_total'])

    if rendement_appellation_kgha > 0 and surface_totale > 0:
        volume_autorise_kg = round(
            (rendement_appellation_kgha + depassement_bloque_kgha + depassement_vo_kgha) * surface_totale
        )
        if kg_exploitation_brut > volume_autorise_kg:
            restant_autorise = volume_autorise_kg
            parcelles_gardees = []
            for p in parcelles_file:
                if not p['kg_total']:
                    parcelles_gardees.append(p)  # pas de surface connue → laissé tel quel (déjà hors calcul de tonnage)
                    continue
                if restant_autorise <= 0:
                    continue  # totalement exclue du plafond : on ne la garde pas dans l'itinéraire
                if p['kg_total'] > restant_autorise:
                    p['kg_total'] = round(restant_autorise)
                    p['kg_restants'] = p['kg_total']
                    restant_autorise = 0
                else:
                    restant_autorise -= p['kg_total']
                parcelles_gardees.append(p)
            parcelles_file = parcelles_gardees
            volume_non_recolte_kg = round(kg_exploitation_brut - volume_autorise_kg)

    kg_exploitation  = sum(p['kg_total'] for p in parcelles_file if p['kg_total'])
    if kg_exploitation > 0:
        nb_jours_total   = max(1, math.ceil(kg_exploitation / objectif))
        kg_reel_par_jour = objectif  # objectif fixe chaque jour, solde absorbé le dernier jour
    else:
        nb_jours_total   = len(parcelles_file)
        kg_reel_par_jour = objectif

    def surf_prorata(kg_tranche, p):
        if p['surface_ha'] and p['kg_total'] and p['kg_total'] > 0:
            return round(float(p['surface_ha']) * kg_tranche / p['kg_total'] * 100, 2)
        return None

    def _estime_a_date(p, date_str):
        degre = p.get('degre')
        df    = p.get('date_fiche_dernier')
        dyn   = p.get('dyn_degre', 0.15)
        if degre is None or not df or not date_str:
            return p.get('degre_estime_recolte')
        try:
            j = (date.fromisoformat(date_str) - date.fromisoformat(df[:10])).days
            return float(degre) if j < 0 else round(float(degre) + dyn * j, 1)
        except:
            return p.get('degre_estime_recolte')

    itineraire   = []
    file_p       = [dict(p) for p in parcelles_file]
    jour_courant = 0
    parcelles_jour = []  # journée précédente (pour la règle de continuité commune/cépage) — vide au départ

    jour_vendange_effectif = 0  # compteur jours de vendange réels (hors repos)

    while file_p and jour_courant < 60:
        date_jour     = date_debut + timedelta(days=jour_courant)
        # Décret 2024-780 : repos dominical obligatoire à partir du 8ème jour de vendange
        # (les 7 premiers jours, le dimanche est autorisé)
        if date_jour.weekday() == 6 and jour_vendange_effectif >= 7:
            jour_courant += 1
            continue
        date_jour_str = date_jour.isoformat()
        en_recolte    = len(itineraire) > 0
        disponibles   = list(file_p) if en_recolte else [p for p in file_p if p['date_theo'] <= date_jour_str]

        if not disponibles:
            jour_courant += 1
            continue

        # Règle commune : si journée débutée → finir commune+cépage avant de changer
        if parcelles_jour and len(disponibles) > 1:
            communes_en_cours = {p['commune'] for p in parcelles_jour}
            cepages_en_cours  = {p['cepage']  for p in parcelles_jour}
            # P1 : même commune + même cépage
            p1 = [p for p in disponibles if p['commune'] in communes_en_cours and p['cepage'] in cepages_en_cours]
            # P2 : même commune, autre cépage (changer de cépage sur place)
            p2 = [p for p in disponibles if p['commune'] in communes_en_cours and p['cepage'] not in cepages_en_cours]
            # P3 : autre commune
            p3 = [p for p in disponibles if p['commune'] not in communes_en_cours]
            if p1 or p2:
                disponibles = p1 + p2 + p3

        kg_jour = 0.0
        parcelles_jour = []
        i = 0

        while i < len(disponibles) and kg_jour < kg_reel_par_jour - 0.01:
            p     = disponibles[i]
            dispo = kg_reel_par_jour - kg_jour

            if p['kg_restants'] is None:
                parcelles_jour.append({
                    'nom': p['nom'], 'cepage': p['cepage'], 'commune': p['commune'],
                    'surface_ares': round(float(p['surface_ha'])*100,2) if p['surface_ha'] else None,
                    'degre': p['degre'], 'degre_estime': _estime_a_date(p, date_jour_str),
                    'kg': None, 'fraction': False, 'rdt_source': p['rdt_source'],
                    'date_theo': p['date_theo'], 'avant_fenetre': p['date_theo'] > date_jour_str,
                    'id': p['id']
                })
                p['kg_restants'] = 0
            elif p['kg_restants'] <= dispo + 0.01:
                kg_r = p['kg_restants']
                parcelles_jour.append({
                    'nom': p['nom'], 'cepage': p['cepage'], 'commune': p['commune'],
                    'surface_ares': surf_prorata(kg_r, p),
                    'degre': p['degre'], 'degre_estime': _estime_a_date(p, date_jour_str),
                    'kg': round(kg_r), 'fraction': False, 'rdt_source': p['rdt_source'],
                    'date_theo': p['date_theo'], 'avant_fenetre': p['date_theo'] > date_jour_str,
                    'id': p['id']
                })
                kg_jour += kg_r; p['kg_restants'] = 0
            else:
                reste_apres = p['kg_restants'] - dispo
                # Absorber si micro-fraction (proportionnelle à la taille de la journée,
                # pas un seuil fixe qui serait disproportionné pour une petite récolte)
                # ou dernier jour.
                est_dernier_jour = sum(1 for pp in file_p if pp.get('kg_restants') and pp['kg_restants'] > 0.5) <= 1
                seuil_micro = min(500, kg_reel_par_jour * 0.15)
                if 0 < reste_apres < seuil_micro or est_dernier_jour:
                    # Prendre tout (légère surcharge acceptée pour éviter micro-fraction)
                    kg_r = p['kg_restants']
                    parcelles_jour.append({
                        'nom': p['nom'], 'cepage': p['cepage'], 'commune': p['commune'],
                        'surface_ares': surf_prorata(kg_r, p),
                        'degre': p['degre'], 'degre_estime': _estime_a_date(p, date_jour_str),
                        'kg': round(kg_r), 'fraction': False, 'rdt_source': p['rdt_source'],
                        'date_theo': p['date_theo'], 'avant_fenetre': p['date_theo'] > date_jour_str,
                        'id': p['id']
                    })
                    kg_jour += kg_r; p['kg_restants'] = 0
                else:
                    parcelles_jour.append({
                        'nom': p['nom'], 'cepage': p['cepage'], 'commune': p['commune'],
                        'surface_ares': surf_prorata(dispo, p),
                        'degre': p['degre'], 'degre_estime': _estime_a_date(p, date_jour_str),
                        'kg': round(dispo), 'fraction': True, 'rdt_source': p['rdt_source'],
                        'date_theo': p['date_theo'], 'avant_fenetre': p['date_theo'] > date_jour_str,
                        'id': p['id']
                    })
                    kg_jour += dispo; p['kg_restants'] -= dispo; break
            i += 1

        file_p = [p for p in file_p if p['kg_restants'] is None or p['kg_restants'] > 0.5]
        if parcelles_jour:
            jour_vendange_effectif += 1
            itineraire.append({
                'date': date_jour_str, 'jour': len(itineraire)+1,
                'parcelles': parcelles_jour,
                'kg_total': round(kg_jour) if kg_jour > 0 else None,
                'repos_dominical': date_jour.weekday() == 6 and jour_vendange_effectif <= 7
            })
        jour_courant += 1

    alertes_degre = []
    for p in parcelles_file:
        de = p.get('degre_estime_recolte')
        if de is not None and de < 9.0:
            alertes_degre.append({
                'nom': p['nom'], 'cepage': p['cepage'], 'commune': p['commune'],
                'date_theo': p['date_theo'], 'degre_estime': de,
                'degre_actuel': p.get('degre'), 'dyn_degre': p.get('dyn_degre', 0.15)
            })

    return jsonify({
        'itineraire': itineraire, 'total_kg': round(kg_exploitation),
        'solde_a_vendanger': round(sum(
            max(0, (
                (round(sum(rdt_by_pid[p['id']]) / len(rdt_by_pid[p['id']])) if rdt_by_pid.get(p['id']) else
                 (round(sum(v) / len(v)) if (v := rdt_by_comcep.get(((p.get('commune') or '').strip().upper(), p.get('cepage') or ''))) else
                  (moy_exploitation or 0)))
                * (p.get('surface_cadastrale') or 0)
                - kg_recoltes_by_pid.get(p['id'], 0)
            ))
            for p in parcelles
        )),
        'nb_jours': nb_jours_total, 'kg_par_jour': round(kg_reel_par_jour),
        'date_debut': date_debut.isoformat(), 'nb_parcelles': len(parcelles_file),
        'volume_autorise_kg': volume_autorise_kg,
        'volume_non_recolte_kg': volume_non_recolte_kg,
        'rendement_appellation_kgha': rendement_appellation_kgha or None,
        'depassement_bloque_kgha': depassement_bloque_kgha or None,
        'depassement_vo_kgha': depassement_vo_kgha or None,
        'nb_avec_tonnage': sum(1 for p in parcelles_file if p['kg_total']),
        'sans_tonnage': any(p['kg_total'] is None for p in parcelles_file),
        'rdt_source_summary': list(set(p['rdt_source'] for p in parcelles_file)),
        'alertes_degre': alertes_degre
    })



@app.route('/notice-vendanges')
def notice_vendanges():
    """Sert la notice PDF de l'onglet Vendanges."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'Notice_Vendanges_VITISens.pdf')


# ===== DASHBOARD ADMIN =====
@app.route('/admin-vitisens')
def admin_dashboard():
    response = send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'dashboard_v2.html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/admin-dates-ouverture')
def page_admin_dates_ouverture():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'admin-dates-ouverture.html')

@app.route('/admin-upload-db')
def page_admin_upload_db():
    return """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MatuScore — Import base de données</title>
    <style>
    body{font-family:-apple-system,sans-serif;background:#f8faf8;display:flex;align-items:center;
    justify-content:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}
    .card{background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.1);width:100%;max-width:440px}
    h1{font-size:17px;color:#2D6A4F;margin-bottom:10px}
    p{font-size:13px;color:#444;line-height:1.5;margin-bottom:16px}
    .warn{background:#FFF3CD;color:#664d03;border-radius:8px;padding:10px 12px;font-size:12px;margin-bottom:16px}
    input[type=file]{width:100%;padding:11px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:13px;margin-bottom:14px;box-sizing:border-box}
    button{width:100%;padding:12px;border:none;border-radius:8px;background:#2D6A4F;color:#fff;font-weight:700;cursor:pointer;font-size:14px}
    button:disabled{opacity:.6;cursor:not-allowed}
    .msg{margin-top:12px;padding:10px;border-radius:8px;font-size:13px;display:none}
    .msg.ok{background:#E8F5E9;color:#2D6A4F;display:block}
    .msg.err{background:#FFEBEE;color:#C1121F;display:block}
    </style></head><body>
    <div class="card">
    <h1>📦 Importer une base de données</h1>
    <p>Remplace la base actuelle de ce serveur par le fichier <code>vitisens.db</code> envoyé ci-dessous — utile pour transférer tes données depuis ton PC local vers ce déploiement.</p>
    <div class="warn">⚠️ Ceci écrase toutes les données actuellement sur ce serveur. Une copie de sécurité de la base actuelle est faite automatiquement avant l'écrasement, mais vérifie bien le fichier que tu envoies.</div>
    <form id="f">
      <input type="file" id="dbfile" accept=".db" required>
      <button type="submit" id="btn">Importer et remplacer</button>
    </form>
    <div id="msg" class="msg"></div>
    </div>
    <script>
    document.getElementById('f').addEventListener('submit', async (e)=>{
      e.preventDefault();
      const btn=document.getElementById('btn'), msg=document.getElementById('msg');
      const file=document.getElementById('dbfile').files[0];
      if(!file){return;}
      btn.disabled=true; btn.textContent='Envoi en cours...';
      const fd=new FormData(); fd.append('fichier', file);
      try{
        const r=await fetch('/api/admin/upload-db',{method:'POST', body:fd});
        const d=await r.json();
        if(!r.ok){ msg.className='msg err'; msg.textContent=d.error||'Erreur.'; btn.disabled=false; btn.textContent='Importer et remplacer'; return; }
        msg.className='msg ok'; msg.textContent='✓ Base importée avec succès ('+d.taille_octets+' octets). Recharge /admin-vitisens pour vérifier.';
        btn.textContent='Importé ✓';
      }catch(err){ msg.className='msg err'; msg.textContent='Erreur de connexion.'; btn.disabled=false; btn.textContent='Importer et remplacer'; }
    });
    </script></body></html>"""

@app.route('/api/admin/upload-db', methods=['POST'])
def api_admin_upload_db():
    if 'fichier' not in request.files:
        return jsonify({"error": "Aucun fichier reçu."}), 400
    f = request.files['fichier']
    if not f.filename.endswith('.db'):
        return jsonify({"error": "Le fichier doit être un .db"}), 400
    data = f.read()
    if len(data) < 1024 or data[:16] != b'SQLite format 3\x00':
        return jsonify({"error": "Ce fichier ne ressemble pas à une base SQLite valide — import refusé par sécurité."}), 400
    try:
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, DB_PATH + '.bak')
        with open(DB_PATH, 'wb') as out:
            out.write(data)
        init_db()  # applique les migrations manquantes si la base importée est plus ancienne
    except Exception as e:
        return jsonify({"error": f"Échec de l'import : {e}"}), 500
    return jsonify({"status": "ok", "taille_octets": len(data)})

@app.route('/.well-known/appspecific/com.chrome.devtools.json')
def chrome_devtools():
    return jsonify({}), 200

@app.route('/favicon.ico')
def favicon():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favicon.ico')
    if os.path.exists(path):
        return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'favicon.ico')
    return '', 204

@app.route('/manifest.json')
def manifest_json():
    """Le manifest est dynamique pour permettre à un raccourci "Ajouter à l'écran
    d'accueil" créé depuis le lien direct d'un client (/portail-s/<slug> ou
    /portail/<token>) de rouvrir toujours CE lien précis, plutôt qu'une page de
    connexion générique qui ne sait rien de la session du navigateur mobile."""
    start = request.args.get('start') or '/mon-espace'
    if not re.match(r'^/portail(-s)?/[A-Za-z0-9_\-]+$', start) and start != '/mon-espace':
        start = '/mon-espace'
    manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manifest.json')
    with open(manifest_path, encoding='utf-8') as f:
        data = json.load(f)
    data['start_url'] = start
    resp = jsonify(data)
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp

@app.route('/sw.js')
def service_worker():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'sw.js', mimetype='application/javascript')

@app.route('/icon-<path:size>.png')
def pwa_icon(size):
    fname = f'icon-{size}.png'
    folder = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(folder, fname)):
        return '', 404
    return send_from_directory(folder, fname)

@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    folder = os.path.dirname(os.path.abspath(__file__))
    fname = 'icon-180.png' if os.path.exists(os.path.join(folder, 'icon-180.png')) else 'icon-192.png'
    return send_from_directory(folder, fname)

@app.route('/')
def index():
    return redirect('/connexion')


# ===== DÉMARRAGE =====
# Appelé au chargement du module (nécessaire sous gunicorn, qui n'exécute pas
# le bloc __main__ ci-dessous).
init_db()

if __name__ == '__main__':
    print("\n" + "="*60)
    print("  🍇 VITI Sens — Serveur local (admin + MatuScore)")
    print("="*60)
    print(f"  MatuScore — inscription publique : http://localhost:5000/inscription")
    print(f"  Dashboard admin (VITI Sens) : http://localhost:5000/admin-vitisens")
    if not ADMIN_PASSWORD:
        print("  ⚠️  ADMIN_PASSWORD non défini — l'espace admin n'est PAS protégé.")
    print("="*60 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
