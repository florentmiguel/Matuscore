"""
VITI Sens — Module Suivi Maturité Vendange
==========================================
Serveur Flask autonome ou intégrable via Blueprint.

Usage standalone :
    python maturite_server.py

Intégration dans app existante :
    from maturite_server import maturite_bp
    app.register_blueprint(maturite_bp)

Accès portail client :
    /maturite/<slug>        → portail HTML
    /maturite/<slug>/api/*  → API JSON

Base de données : maturite.db (SQLite, même dossier)
"""

import sqlite3
import json
from datetime import date, datetime
from flask import Flask, Blueprint, request, jsonify, g, render_template_string

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_PATH = "maturite.db"
maturite_bp = Blueprint("maturite", __name__, url_prefix="/maturite")


# ─────────────────────────────────────────────
# BASE DE DONNÉES
# ─────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def dict_from_row(row):
    return dict(row) if row else None


def dicts_from_rows(rows):
    return [dict(r) for r in rows] if rows else []


def init_db():
    """Crée les tables. Appeler au démarrage."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id          TEXT PRIMARY KEY,
            exploitation TEXT NOT NULL,
            commune     TEXT,
            slug        TEXT UNIQUE,
            latitude    REAL DEFAULT 49.26,
            longitude   REAL DEFAULT 4.03,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS parcelles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            id_client   TEXT NOT NULL,
            nom         TEXT NOT NULL,
            commune     TEXT NOT NULL,
            cepage      TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (id_client) REFERENCES clients(id),
            UNIQUE (id_client, nom)
        );

        CREATE TABLE IF NOT EXISTS fiches_maturite (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            id_parcelle     INTEGER NOT NULL,
            id_client       TEXT NOT NULL,
            campagne        TEXT DEFAULT '2026',
            date_fiche      TEXT NOT NULL,
            degre           REAL,
            at              REAL,
            ratio_sat       REAL,
            couleur_pepins  TEXT,
            saveur_pulpe    TEXT,
            tanins          TEXT,
            etat_sanitaire  INTEGER DEFAULT 0,
            score_degre     INTEGER,
            score_ratio     INTEGER,
            score_pepins    INTEGER,
            score_pulpe     INTEGER,
            score_tanins    INTEGER,
            score_total     INTEGER,
            verdict         TEXT,
            alerte_sanitaire TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (id_parcelle) REFERENCES parcelles(id),
            FOREIGN KEY (id_client) REFERENCES clients(id)
        );

        -- Client démo
        INSERT OR IGNORE INTO clients (id, exploitation, commune, slug, latitude, longitude)
        VALUES ('demo', 'EARL Hotte Biffroteaux', 'Brimont', 'demo2026', 49.30, 3.96);
    """)
    conn.commit()
    conn.close()
    print("[maturite] Base initialisée.")


# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────
PEPINS_VALS  = {"Vert": 5, "Partiel": 10, "Brun": 15}
PULPE_VALS   = {"Végétale": 5, "Acidulée": 10, "Fruitée": 15, "Surmature": 20}
TANINS_VALS  = {"Astringents": 0, "Equilibrés": 5, "Légers": 10, "Fondus": 15}


def calc_score_degre(d):
    """
    < 8.5  → 0 pts (pas vendangeable)
    >= 8.5 → +1 pt par 0.1% vol
    9.5% = 10 pts (maturité techno), 10.5% = 20 pts (optimum), 11.5% = 30 pts (surmaturité)
    Opti affiché = 20 pts max
    """
    if d is None:
        return None, None, None
    d = float(d)
    if d < 8.5:
        return 0, 0, "Pas vendangeable"
    pts = min(30, round((d - 8.4) / 0.1))
    opti = min(pts, 20)
    if pts >= 30:
        label = "Surmaturité"
    elif pts >= 20:
        label = "Optimum"
    elif pts >= 10:
        label = "Maturité techno"
    else:
        label = "En cours"
    return pts, opti, label


def calc_score_ratio(d, at):
    """
    Ratio S/AT = (degré × 16.83) / AT
    < 20 → 0 pts | 20-30 → 2 pts/unité | > 30 → 20 pts
    Opti = 10 pts max
    """
    if d is None or at is None or float(at) <= 0:
        return None, None, None, None
    d, at = float(d), float(at)
    ratio = (d * 16.83) / at
    if ratio < 20:
        pts = 0
        label = "Trop acide"
    elif ratio <= 30:
        pts = min(20, round((ratio - 20) * 2))
        label = "Equilibre"
    else:
        pts = 20
        label = "Chute acidité"
    return round(ratio, 2), pts, min(pts, 10), label


def calc_alerte_sanitaire(san, d):
    """Règles sanitaires — ne joue pas sur le score total."""
    san = int(san or 0)
    d_val = float(d) if d else None
    if san <= 0:
        return None
    if san < 5:
        return "surveiller"
    if 5 <= san <= 10:
        if d_val is not None and d_val < 9.5:
            return "anti-botrytis curatif — vérifier AMM et respecter DAR"
        return "anti-botrytis biocontrôle ou anticiper la vendange"
    # > 10%
    return "anti-botrytis urgent ou vendangez sans attendre"


def calc_verdict(score100, pts_degre, techno, phenolo, san_alerte):
    """Verdict final."""
    if san_alerte and "urgent" in san_alerte:
        return "Anti-botrytis urgent ou vendangez"
    if pts_degre == 0:
        return "Pas vendangeable"
    if score100 < 35:
        return "Trop tôt"
    if san_alerte and "biocontrôle" in san_alerte:
        return "Biocontrôle ou anticiper vendange"
    if san_alerte and "curatif" in san_alerte:
        return "Anti-botrytis curatif — vérifier AMM"
    if score100 < 55:
        return "Surveiller"
    if techno and phenolo:
        return "Maturité phénologique"
    if techno and not phenolo:
        return "Maturité techno — attendre phénologique"
    return "En progression"


def calculer_fiche(d):
    """
    Calcule tous les scores depuis un dict de données.
    Retourne un dict enrichi prêt pour la BDD et le portail.
    """
    degre = d.get("degre")
    at    = d.get("at")
    san   = int(d.get("etat_sanitaire") or 0)
    pepins  = d.get("couleur_pepins")
    pulpe   = d.get("saveur_pulpe")
    tanins  = d.get("tanins")

    # Scores bruts
    pts_d, opti_d, label_d = calc_score_degre(degre)
    ratio, pts_r, opti_r, label_r = calc_score_ratio(degre, at)

    pts_p = PEPINS_VALS.get(pepins)
    opti_p = min(pts_p, 15) if pts_p is not None else None

    pts_s = PULPE_VALS.get(pulpe)
    opti_s = min(pts_s, 15) if pts_s is not None else None

    pts_t = TANINS_VALS.get(tanins)
    opti_t = min(pts_t, 5) if pts_t is not None else None

    # Score total ramené sur 100
    total = 0
    max_total = 0
    if opti_d is not None:
        total += opti_d; max_total += 20
    if opti_r is not None:
        total += opti_r; max_total += 10
    if opti_p is not None:
        total += opti_p; max_total += 15
    if opti_s is not None:
        total += opti_s; max_total += 15
    if opti_t is not None:
        total += opti_t; max_total += 5

    score100 = round(total / max_total * 100) if max_total > 0 else 0

    # Maturités
    techno  = (pts_d or 0) >= 10
    phenolo = (pts_p or 0) >= 10 and (pts_s or 0) >= 10

    alerte_san = calc_alerte_sanitaire(san, degre)
    verdict    = calc_verdict(score100, pts_d or 0, techno, phenolo, alerte_san)

    return {
        "score_degre":      opti_d,
        "score_ratio":      opti_r,
        "score_pepins":     opti_p,
        "score_pulpe":      opti_s,
        "score_tanins":     opti_t,
        "score_total":      score100,
        "ratio_sat":        ratio,
        "verdict":          verdict,
        "alerte_sanitaire": alerte_san,
        "techno":           techno,
        "phenolo":          phenolo,
        "label_degre":      label_d,
        "label_ratio":      label_r,
        "sans_at":          at is None or str(at).strip() == "",
    }


# ─────────────────────────────────────────────
# ROUTES API
# ─────────────────────────────────────────────

def get_client(slug):
    return dict_from_row(get_db().execute(
        "SELECT * FROM clients WHERE slug=?", (slug,)
    ).fetchone())


@maturite_bp.route("/<slug>/api/parcelles", methods=["GET"])
def api_get_parcelles(slug):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    rows = dicts_from_rows(get_db().execute(
        "SELECT * FROM parcelles WHERE id_client=? ORDER BY nom", (c["id"],)
    ).fetchall())
    return jsonify(rows)


@maturite_bp.route("/<slug>/api/parcelles", methods=["POST"])
def api_add_parcelle(slug):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    d = request.json or {}
    nom     = (d.get("nom") or "").strip()
    commune = (d.get("commune") or "").strip()
    cepage  = (d.get("cepage") or "").strip()
    if not nom or not commune or not cepage:
        return jsonify({"error": "nom, commune et cépage obligatoires"}), 400
    conn = get_db()
    # Vérifier si elle existe déjà
    existing = dict_from_row(conn.execute(
        "SELECT * FROM parcelles WHERE id_client=? AND LOWER(nom)=LOWER(?)",
        (c["id"], nom)
    ).fetchone())
    if existing:
        return jsonify({"id": existing["id"], "existing": True})
    cur = conn.execute(
        "INSERT INTO parcelles (id_client, nom, commune, cepage) VALUES (?,?,?,?)",
        (c["id"], nom, commune, cepage)
    )
    conn.commit()
    return jsonify({"id": cur.lastrowid, "existing": False})


@maturite_bp.route("/<slug>/api/fiches", methods=["GET"])
def api_get_fiches(slug):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    pid = request.args.get("parcelle_id")
    conn = get_db()
    if pid:
        rows = dicts_from_rows(conn.execute("""
            SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune
            FROM fiches_maturite f
            JOIN parcelles p ON f.id_parcelle=p.id
            WHERE f.id_client=? AND f.id_parcelle=?
            ORDER BY f.date_fiche DESC
        """, (c["id"], pid)).fetchall())
    else:
        rows = dicts_from_rows(conn.execute("""
            SELECT f.*, p.nom as parcelle_nom, p.cepage, p.commune
            FROM fiches_maturite f
            JOIN parcelles p ON f.id_parcelle=p.id
            WHERE f.id_client=?
            ORDER BY f.date_fiche DESC
        """, (c["id"],)).fetchall())
    return jsonify(rows)


@maturite_bp.route("/<slug>/api/fiches", methods=["POST"])
def api_add_fiche(slug):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    d = request.json or {}
    pid = d.get("id_parcelle")
    if not pid:
        return jsonify({"error": "id_parcelle obligatoire"}), 400

    # Vérifier que la parcelle appartient au client
    parc = dict_from_row(get_db().execute(
        "SELECT * FROM parcelles WHERE id=? AND id_client=?", (pid, c["id"])
    ).fetchone())
    if not parc:
        return jsonify({"error": "Parcelle introuvable"}), 404

    calc = calculer_fiche(d)
    date_fiche = d.get("date_fiche") or date.today().isoformat()

    conn = get_db()
    cur = conn.execute("""
        INSERT INTO fiches_maturite
            (id_parcelle, id_client, campagne, date_fiche,
             degre, at, ratio_sat,
             couleur_pepins, saveur_pulpe, tanins, etat_sanitaire,
             score_degre, score_ratio, score_pepins, score_pulpe, score_tanins,
             score_total, verdict, alerte_sanitaire)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        pid, c["id"], d.get("campagne", "2026"), date_fiche,
        d.get("degre") or None,
        d.get("at") or None,
        calc["ratio_sat"],
        d.get("couleur_pepins"), d.get("saveur_pulpe"), d.get("tanins"),
        int(d.get("etat_sanitaire") or 0),
        calc["score_degre"], calc["score_ratio"],
        calc["score_pepins"], calc["score_pulpe"], calc["score_tanins"],
        calc["score_total"], calc["verdict"], calc["alerte_sanitaire"]
    ))
    conn.commit()
    return jsonify({
        "id": cur.lastrowid,
        **{k: calc[k] for k in ["score_total", "verdict", "alerte_sanitaire",
                                  "techno", "phenolo", "ratio_sat",
                                  "label_degre", "label_ratio", "sans_at"]}
    })


@maturite_bp.route("/<slug>/api/fiches/<int:fid>", methods=["PUT"])
def api_update_fiche(slug, fid):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    # Vérifier que la fiche appartient au client
    existing = dict_from_row(get_db().execute(
        "SELECT * FROM fiches_maturite WHERE id=? AND id_client=?", (fid, c["id"])
    ).fetchone())
    if not existing:
        return jsonify({"error": "Fiche introuvable"}), 404
    d = request.json or {}
    calc = calculer_fiche(d)
    conn = get_db()
    conn.execute("""
        UPDATE fiches_maturite SET
            date_fiche=?, degre=?, at=?, ratio_sat=?,
            couleur_pepins=?, saveur_pulpe=?, tanins=?, etat_sanitaire=?,
            score_degre=?, score_ratio=?, score_pepins=?, score_pulpe=?, score_tanins=?,
            score_total=?, verdict=?, alerte_sanitaire=?
        WHERE id=? AND id_client=?
    """, (
        d.get("date_fiche") or existing["date_fiche"],
        d.get("degre") or None, d.get("at") or None, calc["ratio_sat"],
        d.get("couleur_pepins"), d.get("saveur_pulpe"), d.get("tanins"),
        int(d.get("etat_sanitaire") or 0),
        calc["score_degre"], calc["score_ratio"],
        calc["score_pepins"], calc["score_pulpe"], calc["score_tanins"],
        calc["score_total"], calc["verdict"], calc["alerte_sanitaire"],
        fid, c["id"]
    ))
    conn.commit()
    return jsonify({"ok": True, "score_total": calc["score_total"], "verdict": calc["verdict"]})


@maturite_bp.route("/<slug>/api/fiches/<int:fid>", methods=["DELETE"])
def api_delete_fiche(slug, fid):
    c = get_client(slug)
    if not c: return jsonify({"error": "Client introuvable"}), 404
    conn = get_db()
    conn.execute(
        "DELETE FROM fiches_maturite WHERE id=? AND id_client=?", (fid, c["id"])
    )
    conn.commit()
    return jsonify({"ok": True})


@maturite_bp.route("/<slug>/api/score", methods=["POST"])
def api_score_preview(slug):
    """Calcul score à la volée sans sauvegarde (pour affichage temps réel)."""
    d = request.json or {}
    calc = calculer_fiche(d)
    return jsonify(calc)


# ─────────────────────────────────────────────
# PORTAIL HTML
# ─────────────────────────────────────────────
@maturite_bp.route("/<slug>")
def portail(slug):
    c = get_client(slug)
    if not c:
        return "<h2>Lien invalide ou expiré.</h2>", 404
    return render_template_string(PORTAIL_HTML, client=c, slug=slug)


PORTAIL_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<title>Maturité 2026 — VITI Sens</title>
<style>
:root{
  --gd:#2D6A4F;--gm:#40916C;--gl:#B7E4C7;--gbg:#F0F7F0;
  --or:#E76F51;--rd:#C1121F;--gr:#6B7280;--bg:#f8faf8;
  --bdr:8px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:#1f2937;font-size:14px;padding-bottom:40px}
.header{background:var(--gd);padding:14px 16px;position:sticky;top:0;z-index:10}
.header h1{font-size:16px;font-weight:500;color:#fff}
.header p{font-size:11px;color:#9FE1CB;margin-top:2px}
.wrap{max-width:500px;margin:0 auto;padding:14px}
.card{background:#fff;border-radius:var(--bdr);padding:14px;margin-bottom:12px;border:.5px solid #e5e7eb}
.section-label{font-size:11px;font-weight:600;color:var(--gd);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
input,select,textarea{width:100%;padding:9px 10px;border:.5px solid #d1d5db;border-radius:var(--bdr);font-size:14px;background:#fff;color:#1f2937;margin-bottom:0}
input:focus,select:focus{border-color:var(--gd);outline:none}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
label{font-size:11px;color:var(--gr);display:block;margin-bottom:3px}
.opt{text-align:center;padding:8px 4px;border-radius:var(--bdr);border:.5px solid #e5e7eb;font-size:12px;cursor:pointer;transition:all .12s;user-select:none}
.opt.sel{background:#27500A;color:#C0DD97;border-color:#27500A;font-weight:600}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px}
.g2{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:10px}
.btn{display:block;width:100%;padding:13px;border-radius:var(--bdr);border:none;font-size:14px;font-weight:600;cursor:pointer}
.btn-primary{background:var(--gd);color:#fff}
.btn-outline{background:#fff;color:var(--gd);border:1.5px solid var(--gl)}
.btn-sm{padding:6px 12px;font-size:12px;border-radius:6px;border:none;cursor:pointer}
.badge{display:inline-block;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600}
.score-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.score-label{font-size:11px;color:var(--gr);width:72px;flex-shrink:0}
.score-bar-bg{flex:1;background:#e5e7eb;border-radius:3px;height:5px}
.score-bar-fill{height:5px;border-radius:3px;transition:width .3s}
.score-pts{font-size:11px;font-weight:600;width:32px;text-align:right}
.score-detail{font-size:10px;color:var(--gr);min-width:60px;text-align:right}
.alerte{padding:7px 10px;border-radius:6px;font-size:11px;margin-top:6px}
.al-info{background:#F3F4F6;color:#6B7280}
.al-warn{background:#FFF8E1;color:#F57F17}
.al-urgent{background:#FFF3E0;color:#E65100}
.al-critical{background:#FFEBEE;color:#C62828}
.al-ok{background:#E8F5E9;color:#2E7D32}
.al-techno{background:#FFFDE7;color:#F57F17}
.fiche-card{border:.5px solid #e5e7eb;border-radius:var(--bdr);padding:10px;margin-bottom:8px}
.fiche-actions{display:flex;gap:6px}
.ratio-line{background:#f9fafb;border-radius:6px;padding:6px 8px;font-size:11px;color:var(--gr);margin-top:8px}
.sugg-box{position:absolute;top:100%;left:0;right:0;background:#fff;border:.5px solid #e5e7eb;border-radius:var(--bdr);z-index:20;overflow:hidden;margin-top:3px}
.sugg-item{padding:10px 12px;cursor:pointer;font-size:13px;border-bottom:.5px solid #f3f4f6}
.sugg-item:hover{background:var(--gbg)}
.new-badge{font-size:10px;background:#FFF3E0;color:#E65100;padding:1px 5px;border-radius:4px;margin-left:4px}
</style>
</head>
<body>

<div class="header">
  <h1>Maturité 2026</h1>
  <p>{{ client.exploitation }}</p>
</div>

<div class="wrap">

  <!-- SAISIE FICHE -->
  <div class="card" id="card-saisie">
    <div class="section-label">Parcelle</div>

    <div style="position:relative;margin-bottom:10px">
      <input id="inp-parc" placeholder="Tapez le nom de la parcelle..." autocomplete="off"
             oninput="onTypeParc()" onblur="setTimeout(hideSugg,180)">
      <div id="sugg-box" class="sugg-box" style="display:none"></div>
    </div>

    <div id="new-fields" style="display:none;margin-bottom:10px">
      <div style="font-size:11px;color:var(--or);margin-bottom:6px">
        Nouvelle parcelle <span class="new-badge">sera créée à l'enregistrement</span>
      </div>
      <div class="grid2">
        <div><label>Commune *</label><input id="inp-com" placeholder="Louvois" oninput="checkNewParc()"></div>
        <div><label>Cépage *</label>
          <select id="sel-cep" onchange="checkNewParc()">
            <option value="">— Choisir —</option>
            <option>Chardonnay</option>
            <option>Pinot Noir</option>
            <option>Meunier</option>
          </select>
        </div>
      </div>
    </div>

    <div id="parc-recap" style="display:none;font-size:12px;color:#27500A;background:#EAF3DE;
         border-radius:6px;padding:6px 10px;margin-bottom:10px"></div>

    <!-- FORMULAIRE (verrouillé jusqu'à parcelle valide) -->
    <div id="form-mesures" style="opacity:.3;pointer-events:none">

      <div style="margin-bottom:12px">
        <label>Date du relevé</label>
        <input type="date" id="inp-date">
      </div>

      <!-- Mesures -->
      <div style="background:var(--gbg);border-radius:var(--bdr);padding:10px;margin-bottom:10px">
        <div class="section-label">Mesures</div>
        <div class="grid2">
          <div>
            <label>Degré probable (% vol.)</label>
            <input type="number" id="inp-degre" step="0.1">
          </div>
          <div>
            <label>AT g/L <span style="color:var(--or);font-size:10px">optionnel</span></label>
            <input type="number" id="inp-at" step="0.1">
          </div>
        </div>
        <div id="ratio-line" class="ratio-line" style="display:none"></div>
      </div>

      <!-- Dégustation -->
      <div style="background:var(--gbg);border-radius:var(--bdr);padding:10px;margin-bottom:10px">
        <div class="section-label">Dégustation</div>
        <label style="margin-bottom:5px">Couleur des pépins</label>
        <div class="g3" id="g-pepins">
          <div class="opt" data-g="pepins" data-v="Vert">Vert</div>
          <div class="opt" data-g="pepins" data-v="Partiel">Partiel</div>
          <div class="opt" data-g="pepins" data-v="Brun">Brun</div>
        </div>
        <label style="margin-bottom:5px">Saveur de la pulpe</label>
        <div class="g2" id="g-pulpe">
          <div class="opt" data-g="pulpe" data-v="Végétale">Végétale</div>
          <div class="opt" data-g="pulpe" data-v="Acidulée">Acidulée</div>
          <div class="opt" data-g="pulpe" data-v="Fruitée">Fruitée</div>
          <div class="opt" data-g="pulpe" data-v="Surmature">Surmature</div>
        </div>
        <label style="margin-bottom:5px">Tanins de la peau</label>
        <div class="g2" id="g-tanins">
          <div class="opt" data-g="tanins" data-v="Astringents">Astringents</div>
          <div class="opt" data-g="tanins" data-v="Equilibrés">Equilibrés</div>
          <div class="opt" data-g="tanins" data-v="Légers">Légers</div>
          <div class="opt" data-g="tanins" data-v="Fondus">Fondus</div>
        </div>
      </div>

      <!-- Etat sanitaire -->
      <div style="background:var(--gbg);border-radius:var(--bdr);padding:10px;margin-bottom:10px">
        <div class="section-label">Etat sanitaire</div>
        <div style="display:flex;align-items:center;gap:10px">
          <input type="range" id="inp-san" min="0" max="50" step="1" value="0" style="flex:1;margin:0">
          <span id="san-val" style="font-size:15px;font-weight:500;min-width:36px;text-align:right;color:var(--or)">0%</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--gr);margin-top:3px">
          <span>0%</span><span>25%</span><span>50%</span>
        </div>
      </div>

      <!-- Score temps réel -->
      <div id="score-card" style="display:none;border:.5px solid #e5e7eb;border-radius:var(--bdr);padding:12px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
          <div>
            <div style="font-size:11px;color:var(--gr)">Score de maturité</div>
            <div style="display:flex;align-items:baseline;gap:3px">
              <span id="sc-val" style="font-size:38px;font-weight:700;line-height:1">—</span>
              <span id="sc-base" style="font-size:13px;color:var(--gr)">/100</span>
            </div>
          </div>
          <span id="sc-badge" class="badge" style="margin-top:4px"></span>
        </div>
        <div style="background:#e5e7eb;border-radius:4px;height:7px;margin-bottom:10px">
          <div id="sc-bar" style="height:7px;border-radius:4px;transition:width .4s;width:0%"></div>
        </div>
        <div id="sc-detail"></div>
        <div id="sc-pheno" style="display:none" class="alerte"></div>
        <div id="sc-san" style="display:none" class="alerte"></div>
      </div>

      <button class="btn btn-primary" id="btn-save" onclick="saveFiche()">Enregistrer la fiche</button>
    </div>
  </div>

  <!-- HISTORIQUE FICHES -->
  <div class="card" id="card-fiches" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div class="section-label" style="margin:0" id="fiches-title">Fiches</div>
      <button class="btn-sm" style="background:var(--gbg);color:var(--gd)" onclick="showFiches(currentParcelleId)">Actualiser</button>
    </div>
    <div id="fiches-list"></div>
  </div>

</div>

<!-- MODAL édition -->
<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;padding:16px;overflow-y:auto">
  <div style="background:#fff;border-radius:12px;padding:18px;max-width:440px;margin:40px auto" id="modal-content"></div>
</div>

<script>
const SLUG = '{{ slug }}';
const API  = '/maturite/' + SLUG + '/api';

let PARCELLES = [];
let currentParcelleId = null;
let editFicheId = null;

// ── Initialisation ──────────────────────────────────
async function init(){
  document.getElementById('inp-date').value = today();
  await loadParcelles();
  setupOpts();
  setupInputs();
}

function today(){
  return new Date().toISOString().slice(0,10);
}

// ── Chargement parcelles ────────────────────────────
async function loadParcelles(){
  try{
    const r = await fetch(API + '/parcelles');
    PARCELLES = await r.json();
  } catch(e){ PARCELLES = []; }
}

// ── Autocomplete parcelle ───────────────────────────
function onTypeParc(){
  const val = document.getElementById('inp-parc').value.trim().toLowerCase();
  const box = document.getElementById('sugg-box');
  resetParcelle();
  if(!val){ box.style.display='none'; return; }
  const matches = PARCELLES.filter(p => p.nom.toLowerCase().startsWith(val));
  if(!matches.length){ box.style.display='none'; return; }
  box.innerHTML = matches.map(p =>
    `<div class="sugg-item" onclick="selectParcelle(${p.id})">
      <strong>${p.nom}</strong>
      <span style="color:var(--gr);font-size:11px"> ${p.cepage} · ${p.commune}</span>
    </div>`
  ).join('');
  box.style.display = 'block';
}

function hideSugg(){
  document.getElementById('sugg-box').style.display = 'none';
  const val = document.getElementById('inp-parc').value.trim();
  if(val && !currentParcelleId){
    document.getElementById('new-fields').style.display = 'block';
  }
}

function selectParcelle(id){
  const p = PARCELLES.find(x => x.id === id);
  if(!p) return;
  currentParcelleId = p.id;
  document.getElementById('inp-parc').value = p.nom;
  document.getElementById('sugg-box').style.display = 'none';
  document.getElementById('new-fields').style.display = 'none';
  setRecap(p.nom + ' · ' + p.cepage + ' · ' + p.commune);
  unlockForm();
  showFiches(p.id);
}

function checkNewParc(){
  const nom = document.getElementById('inp-parc').value.trim();
  const com = document.getElementById('inp-com').value.trim();
  const cep = document.getElementById('sel-cep').value;
  if(nom && com && cep){
    setRecap(nom + ' · ' + cep + ' · ' + com + ' — nouvelle parcelle');
    unlockForm();
  }
}

function resetParcelle(){
  currentParcelleId = null;
  document.getElementById('new-fields').style.display = 'none';
  document.getElementById('parc-recap').style.display = 'none';
  document.getElementById('card-fiches').style.display = 'none';
  lockForm();
}

function setRecap(txt){
  const r = document.getElementById('parc-recap');
  r.textContent = txt; r.style.display = 'block';
}

function unlockForm(){
  const f = document.getElementById('form-mesures');
  f.style.opacity = '1'; f.style.pointerEvents = 'auto';
  if(!document.getElementById('inp-date').value)
    document.getElementById('inp-date').value = today();
}

function lockForm(){
  const f = document.getElementById('form-mesures');
  f.style.opacity = '0.3'; f.style.pointerEvents = 'none';
}

// ── Sélection boutons dégustation ───────────────────
function setupOpts(){
  document.querySelectorAll('.opt').forEach(el => {
    el.addEventListener('click', function(){
      const g = this.dataset.g;
      document.querySelectorAll(`.opt[data-g="${g}"]`).forEach(o => o.classList.remove('sel'));
      this.classList.add('sel');
      calcScoreLive();
    });
  });
}

function getOpt(g){
  const el = document.querySelector(`.opt[data-g="${g}"].sel`);
  return el ? el.dataset.v : null;
}

function setOpt(g, val){
  document.querySelectorAll(`.opt[data-g="${g}"]`).forEach(o => {
    o.classList.toggle('sel', o.dataset.v === val);
  });
}

// ── Inputs mesures ──────────────────────────────────
function setupInputs(){
  ['inp-degre','inp-at'].forEach(id =>
    document.getElementById(id).addEventListener('input', calcScoreLive)
  );
  document.getElementById('inp-san').addEventListener('input', function(){
    document.getElementById('san-val').textContent = this.value + '%';
    calcScoreLive();
  });
}

// ── Calcul score live (appel API preview) ───────────
let scoreTimer;
function calcScoreLive(){
  clearTimeout(scoreTimer);
  scoreTimer = setTimeout(async () => {
    const d = getData();
    if(!d.degre && !d.couleur_pepins && !d.saveur_pulpe && !d.tanins && !d.etat_sanitaire){
      document.getElementById('score-card').style.display = 'none'; return;
    }
    try{
      const r = await fetch(API + '/score', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(d)
      });
      const sc = await r.json();
      renderScore(sc);
    }catch(e){}
  }, 250);
}

// ── Rendu score ─────────────────────────────────────
function barColor(pts, opti){
  if(pts >= opti) return '#2E7D32';
  if(pts > 0)     return '#FF9800';
  return '#EF5350';
}

function renderScore(sc){
  document.getElementById('score-card').style.display = 'block';

  // Ratio line
  const rl = document.getElementById('ratio-line');
  if(sc.ratio_sat){
    rl.style.display = 'block';
    rl.innerHTML = 'Ratio S/AT = <strong>' + sc.ratio_sat + '</strong> &nbsp; ' + (sc.label_ratio||'');
  } else rl.style.display = 'none';

  // Score global
  const s = sc.score_total;
  document.getElementById('sc-val').textContent = s;
  document.getElementById('sc-base').textContent = '/ 100' + (sc.sans_at ? ' (sans AT)' : '');
  const barC = s<35?'#EF5350':s<55?'#FF9800':s<75?'#FFC107':s<90?'#4CAF50':'#1E88E5';
  document.getElementById('sc-bar').style.width = s + '%';
  document.getElementById('sc-bar').style.background = barC;

  // Badge verdict
  const BADGE_STYLES = {
    'Pas vendangeable':           {bg:'#FFEBEE',col:'#C62828'},
    'Trop tôt':                   {bg:'#FFEBEE',col:'#C62828'},
    'Surveiller':                 {bg:'#FFF3E0',col:'#E65100'},
    'En progression':             {bg:'#FFFDE7',col:'#F57F17'},
    'Maturité techno — attendre phénologique': {bg:'#FFFDE7',col:'#F57F17'},
    'Maturité atteinte':          {bg:'#E8F5E9',col:'#2E7D32'},
    'Maturité phénologique':      {bg:'#E8F5E9',col:'#2E7D32'},
    'Biocontrôle ou anticiper vendange': {bg:'#FFF3E0',col:'#E65100'},
    'Anti-botrytis curatif — vérifier AMM': {bg:'#FFF8E1',col:'#F57F17'},
    'Anti-botrytis urgent ou vendangez':   {bg:'#FFEBEE',col:'#C62828'},
  };
  const bs = BADGE_STYLES[sc.verdict] || {bg:'#F3F4F6',col:'#6B7280'};
  const badge = document.getElementById('sc-badge');
  badge.textContent = sc.verdict;
  badge.style.background = bs.bg; badge.style.color = bs.col;

  // Lignes détail
  const rows = [
    {label:'Degré', pts:sc.score_degre, max:20, detail:sc.label_degre||'', opti:20},
    {label:'Ratio S/AT', pts:sc.score_ratio, max:10, detail:'', opti:10},
    {label:'Pépins', pts:sc.score_pepins, max:15, detail:'', opti:10},
    {label:'Pulpe', pts:sc.score_pulpe, max:15, detail:'', opti:10},
    {label:'Tanins', pts:sc.score_tanins, max:5, detail:'', opti:5},
  ].filter(r => r.pts !== null && r.pts !== undefined);

  let dh = '';
  rows.forEach(r => {
    const pct = r.max > 0 ? Math.round(r.pts/r.max*100) : 0;
    const bc  = barColor(r.pts, r.opti);
    dh += `<div class="score-row">
      <div class="score-label">${r.label}</div>
      <div class="score-bar-bg"><div class="score-bar-fill" style="background:${bc};width:${pct}%"></div></div>
      <div class="score-pts" style="color:${bc}">${r.pts}</div>
      <div class="score-detail">${r.detail}</div>
    </div>`;
  });
  document.getElementById('sc-detail').innerHTML = dh;

  // Alerte phénologique
  const ap = document.getElementById('sc-pheno');
  if(sc.techno && sc.phenolo){
    ap.style.display = 'block'; ap.className = 'alerte al-ok';
    ap.textContent = 'Maturité phénologique atteinte — pépins et pulpe au vert';
  } else if(sc.techno && !sc.phenolo && (sc.score_pepins!==null||sc.score_pulpe!==null)){
    ap.style.display = 'block'; ap.className = 'alerte al-techno';
    ap.textContent = 'Maturité technologique atteinte — attendre maturité phénologique';
  } else ap.style.display = 'none';

  // Alerte sanitaire
  const as = document.getElementById('sc-san');
  const san = sc.alerte_sanitaire;
  if(san){
    as.style.display = 'block';
    if(san.includes('urgent')) as.className='alerte al-critical';
    else if(san.includes('biocontrôle')) as.className='alerte al-urgent';
    else if(san.includes('curatif')) as.className='alerte al-warn';
    else as.className='alerte al-info';
    as.textContent = san;
  } else as.style.display = 'none';
}

// ── Collecte données formulaire ─────────────────────
function getData(){
  return {
    degre:           parseFloat(document.getElementById('inp-degre').value)||null,
    at:              parseFloat(document.getElementById('inp-at').value)||null,
    couleur_pepins:  getOpt('pepins'),
    saveur_pulpe:    getOpt('pulpe'),
    tanins:          getOpt('tanins'),
    etat_sanitaire:  parseInt(document.getElementById('inp-san').value)||0,
    date_fiche:      document.getElementById('inp-date').value,
  };
}

// ── Sauvegarde fiche ────────────────────────────────
async function saveFiche(){
  const btn = document.getElementById('btn-save');
  // Créer la parcelle si nouvelle
  let pid = currentParcelleId;
  if(!pid){
    const nom = document.getElementById('inp-parc').value.trim();
    const com = document.getElementById('inp-com').value.trim();
    const cep = document.getElementById('sel-cep').value;
    if(!nom||!com||!cep){ alert('Complétez les infos de la parcelle'); return; }
    const r = await fetch(API+'/parcelles',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({nom,commune:com,cepage:cep})
    });
    const resp = await r.json();
    if(resp.error){ alert(resp.error); return; }
    pid = resp.id;
    currentParcelleId = pid;
    await loadParcelles();
  }
  const d = getData();
  if(!d.degre && !d.couleur_pepins){
    alert('Renseignez au moins le degré probable ou les critères de dégustation'); return;
  }
  btn.textContent = 'Enregistrement...'; btn.disabled = true;
  try{
    const url = editFicheId ? `${API}/fiches/${editFicheId}` : `${API}/fiches`;
    const method = editFicheId ? 'PUT' : 'POST';
    const body = editFicheId ? d : {...d, id_parcelle:pid};
    const r = await fetch(url,{method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const resp = await r.json();
    if(resp.error){ alert(resp.error); return; }
    editFicheId = null;
    btn.textContent = 'Enregistrer la fiche';
    resetForm();
    showFiches(pid);
    // Toast
    showToast('✅ Fiche enregistrée — ' + resp.verdict + ' (' + resp.score_total + '/100)');
  }catch(e){ alert('Erreur: '+e.message); }
  finally{ btn.disabled = false; btn.textContent = 'Enregistrer la fiche'; }
}

function resetForm(){
  document.getElementById('inp-degre').value = '';
  document.getElementById('inp-at').value = '';
  document.getElementById('inp-san').value = '0';
  document.getElementById('san-val').textContent = '0%';
  document.getElementById('inp-date').value = today();
  document.querySelectorAll('.opt').forEach(o => o.classList.remove('sel'));
  document.getElementById('score-card').style.display = 'none';
  document.getElementById('ratio-line').style.display = 'none';
}

// ── Affichage fiches parcelle ───────────────────────
async function showFiches(pid){
  if(!pid) return;
  currentParcelleId = pid;
  const card = document.getElementById('card-fiches');
  const list = document.getElementById('fiches-list');
  card.style.display = 'block';
  list.innerHTML = '<div style="color:var(--gr);text-align:center;padding:20px">Chargement...</div>';

  const parc = PARCELLES.find(p => p.id === pid);
  if(parc) document.getElementById('fiches-title').textContent = parc.nom + ' — ' + parc.cepage;

  try{
    const r = await fetch(`${API}/fiches?parcelle_id=${pid}`);
    const fiches = await r.json();
    if(!fiches.length){
      list.innerHTML = '<p style="color:var(--gr);text-align:center;padding:16px">Aucune fiche pour cette parcelle.</p>';
      return;
    }
    const BADGE_COLORS = {
      'Maturité phénologique':    {bg:'#E8F5E9',col:'#2E7D32'},
      'Maturité atteinte':        {bg:'#E8F5E9',col:'#2E7D32'},
      'Surveiller':               {bg:'#FFF3E0',col:'#E65100'},
      'Trop tôt':                 {bg:'#FFEBEE',col:'#C62828'},
      'Pas vendangeable':         {bg:'#FFEBEE',col:'#C62828'},
    };
    list.innerHTML = fiches.map(f => {
      const bc = BADGE_COLORS[f.verdict] || {bg:'#F3F4F6',col:'#6B7280'};
      const dateStr = new Date(f.date_fiche+'T12:00:00').toLocaleDateString('fr-FR',{day:'2-digit',month:'long',year:'numeric'});
      const tags = [f.couleur_pepins, f.saveur_pulpe, f.tanins].filter(Boolean);
      return `<div class="fiche-card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
          <div style="font-size:11px;color:var(--gr)">${dateStr}</div>
          <div class="fiche-actions">
            <button class="btn-sm" style="background:var(--gbg);color:var(--gd)" onclick="editFiche(${f.id})">Modifier</button>
            <button class="btn-sm" style="background:#FFEBEE;color:var(--rd)" onclick="deleteFiche(${f.id})">Supprimer</button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
          <div style="background:var(--gbg);border-radius:6px;padding:7px">
            <div style="font-size:10px;color:var(--gr)">Degré</div>
            <div style="font-size:18px;font-weight:600">${f.degre != null ? f.degre+'%' : '—'}</div>
          </div>
          <div style="background:var(--gbg);border-radius:6px;padding:7px">
            <div style="font-size:10px;color:var(--gr)">AT / Score</div>
            <div style="font-size:18px;font-weight:600">${f.at != null ? f.at+' g/L' : '—'} <span style="font-size:12px;color:var(--gr)">/ ${f.score_total||0}</span></div>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px">
          ${tags.map(t=>`<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:#EAF3DE;color:#27500A">${t}</span>`).join('')}
          ${f.etat_sanitaire>0?`<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:#FFF3E0;color:#E65100">San. ${f.etat_sanitaire}%</span>`:''}
        </div>
        <span class="badge" style="font-size:11px;background:${bc.bg};color:${bc.col}">${f.verdict||'—'}</span>
        ${f.alerte_sanitaire?`<div style="font-size:10px;margin-top:5px;color:var(--or)">${f.alerte_sanitaire}</div>`:''}
      </div>`;
    }).join('');
  }catch(e){
    list.innerHTML = '<p style="color:var(--rd)">Erreur chargement</p>';
  }
}

// ── Edition fiche ───────────────────────────────────
async function editFiche(id){
  const r = await fetch(`${API}/fiches?parcelle_id=${currentParcelleId}`);
  const fiches = await r.json();
  const f = fiches.find(x => x.id === id);
  if(!f) return;
  editFicheId = id;
  document.getElementById('inp-date').value = f.date_fiche || today();
  document.getElementById('inp-degre').value = f.degre || '';
  document.getElementById('inp-at').value = f.at || '';
  document.getElementById('inp-san').value = f.etat_sanitaire || 0;
  document.getElementById('san-val').textContent = (f.etat_sanitaire||0) + '%';
  if(f.couleur_pepins) setOpt('pepins', f.couleur_pepins);
  if(f.saveur_pulpe)   setOpt('pulpe', f.saveur_pulpe);
  if(f.tanins)         setOpt('tanins', f.tanins);
  document.getElementById('btn-save').textContent = 'Mettre à jour la fiche';
  calcScoreLive();
  window.scrollTo({top:0, behavior:'smooth'});
}

// ── Suppression fiche ───────────────────────────────
async function deleteFiche(id){
  if(!confirm('Supprimer cette fiche ?')) return;
  await fetch(`${API}/fiches/${id}`, {method:'DELETE'});
  showFiches(currentParcelleId);
}

// ── Toast ───────────────────────────────────────────
function showToast(msg){
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1f2937;color:#fff;padding:10px 18px;border-radius:20px;font-size:13px;z-index:200;max-width:320px;text-align:center';
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

init();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# LANCEMENT STANDALONE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = Flask(__name__)
    app.register_blueprint(maturite_bp)

    @app.teardown_appcontext
    def close_db(e=None):
        db = g.pop("db", None)
        if db: db.close()

    with app.app_context():
        init_db()

    print("=" * 50)
    print("VITI Sens — Module Maturité")
    print("Portail démo : http://localhost:5001/maturite/demo2026")
    print("=" * 50)
    app.run(debug=True, port=5001)
