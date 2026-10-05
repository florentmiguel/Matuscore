# -*- coding: utf-8 -*-
"""
Pont entre Pilot et le moteur épidémiologique (dépôt « epidemio »)
===================================================================

À placer à côté de serveur_vitisens.py. Rien n'est copié du moteur : il est chargé depuis le dépôt epidemio
(variable d'environnement EPIDEMIO_PATH, défaut ~/epidemio). S'il est absent ou en panne, Pilot démarre et
fonctionne comme avant (voir MoteurIndisponible et synthese_mildiou_pour_prompt).

Ce module fournit :
  * serie_horaire()           météo horaire du 1er janvier à la fin de la prévision, avec cache disque
  * calculer()                lance le moteur (profil EPIDEMIO_PROFIL, défaut « calage_2026 »)
  * synthese_mildiou()        résumé JSON compact pour le tableau de bord et pour le prompt de l'IA
  * bloc_epidemio_prompt()    bloc de texte du prompt de /api/generer-texte-bulletin
  * bp_epidemio               blueprint Flask : GET /api/epidemio-moteur?lat=..&lon=..[&complet=1]

Le moteur évalue un DANGER théorique, indépendant des traitements : ce n'est ni une contamination constatée, ni
une consigne de traitement. Le conseil reste celui du conseiller.

NB : l'API gratuite d'Open-Meteo est réservée à un usage non commercial (offre payante pour un usage commercial).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
PROFIL = os.environ.get("EPIDEMIO_PROFIL", "calage_2026")
COORDS_DEFAUT = {"lat": float(os.environ.get("EPIDEMIO_LAT", "49.25")), "lon": float(os.environ.get("EPIDEMIO_LON", "3.96"))}

# Bandes d'intensité de l'infection (°C·h), reprises des seuils de la carte de risque de Plasmopy : 50 / 100 / 200
SEUILS_NIVEAU = (50.0, 100.0, 200.0)
CLES_MILDIOU_ANCIEN = ("contaminations_mildiou", "texte_mildiou", "nb_contaminations")

AVERTISSEMENT = ("Danger épidémiologique théorique calculé sur la météo horaire depuis le 1er janvier, INDÉPENDANT des "
                 "traitements réalisés ; ce n'est ni une contamination constatée ni une consigne de traitement.")


class MoteurIndisponible(RuntimeError):
    """Le dépôt epidemio est introuvable ou ne s'importe pas."""


# ---------------------------------------------------------------------------
# Chargement du moteur
# ---------------------------------------------------------------------------
def chemin_moteur() -> str:
    return os.path.expanduser(os.environ.get("EPIDEMIO_PATH", "~/epidemio"))


def charger_moteur():
    """(mildiou_primaire, recuperer_meteo_horaire). Le dossier est AJOUTÉ en fin de sys.path : il ne peut pas masquer
    un module de Pilot."""
    chemin = chemin_moteur()
    if chemin not in sys.path:
        sys.path.append(chemin)
    try:
        import mildiou_primaire as mp
        import recuperer_meteo_horaire as rm
    except Exception as e:                                          # noqa: BLE001 : toute panne d'import est « indisponible »
        raise MoteurIndisponible(f"moteur introuvable ou inutilisable dans {chemin} : {e}") from e
    return mp, rm


# ---------------------------------------------------------------------------
# Météo : conversion et cache disque
# ---------------------------------------------------------------------------
def _flottant(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() in ("nan", "none", "null") else float(s)


def lignes_vers_rows(lignes: list[dict]) -> list[dict]:
    """Lignes d'Open-Meteo (time, temperature_2m, relative_humidity_2m, dew_point_2m, precipitation) vers les lignes
    du moteur. Mêmes règles que le chargement d'un CSV : heures en UTC, valeurs absentes = None."""
    rows = []
    for r in lignes:
        t = datetime.fromisoformat(str(r["time"]).strip())
        t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
        rows.append({"t": t, "temp": _flottant(r.get("temperature_2m")), "hr": _flottant(r.get("relative_humidity_2m")),
                     "pluie": _flottant(r.get("precipitation")), "rosee": _flottant(r.get("dew_point_2m")), "mouille": None})
    rows.sort(key=lambda x: x["t"])
    return rows


def dossier_cache() -> str:
    d = os.environ.get("EPIDEMIO_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "epidemio_cache")
    os.makedirs(d, exist_ok=True)
    return d


def serie_horaire(lat, lon, now: datetime | None = None, recuperer=None, ttl_s: int | None = None) -> tuple[list, int, bool]:
    """(lignes, âge du cache en secondes, périmée). Cache disque par position (2 décimales) et par année, durée de vie
    EPIDEMIO_TTL_S (défaut 1 h). Si la récupération échoue et qu'un cache périmé existe, on s'en sert (périmée = True) ;
    sinon l'erreur remonte."""
    now = now or datetime.now(UTC)
    ttl_s = int(os.environ.get("EPIDEMIO_TTL_S", "3600")) if ttl_s is None else ttl_s
    if recuperer is None:
        recuperer = charger_moteur()[1].recuperer
    chemin = os.path.join(dossier_cache(), f"meteo_{float(lat):.2f}_{float(lon):.2f}_{now.year}.json")

    def lire():
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)

    age = None
    if os.path.exists(chemin):
        age = int(time.time() - os.path.getmtime(chemin))
        if age <= ttl_s:
            return lire(), age, False
    try:
        lignes = recuperer(float(lat), float(lon))
    except Exception:                                               # noqa: BLE001
        if age is not None:
            return lire(), age, True
        raise
    tmp = f"{chemin}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lignes, f)
    os.replace(tmp, chemin)                                         # écriture atomique : deux workers ne se marchent pas dessus
    return lignes, 0, False


def calculer(lat, lon, now: datetime | None = None, recuperer=None) -> tuple[dict, dict]:
    """(résultat complet du moteur, métadonnées)."""
    mp, rm = charger_moteur()
    now = now or datetime.now(UTC)
    lignes, age, perimee = serie_horaire(lat, lon, now, recuperer or rm.recuperer)
    res = mp.calculer_saison(lignes_vers_rows(lignes), float(lat), float(lon), params=mp.charger_profil(PROFIL), now=now)
    return res, {"profil": PROFIL, "meteo_age_s": age, "meteo_perimee": perimee, "calcule_le": now.strftime("%Y-%m-%dT%H:%MZ")}


# ---------------------------------------------------------------------------
# Synthèse pour le tableau de bord et pour le prompt
# ---------------------------------------------------------------------------
def niveau(force_dh) -> str | None:
    """Intensité d'une infection : faible (< 50 °C·h), modérée, forte, très forte (>= 200)."""
    if force_dh is None:
        return None
    a, b, c = SEUILS_NIVEAU
    return "faible" if force_dh < a else "modérée" if force_dh < b else "forte" if force_dh < c else "très forte"


def _evenement(inf: dict, force, taches: dict | None, extra: dict | None = None) -> dict:
    """Événement compact : les indicateurs « faux » sont omis pour garder le prompt court."""
    out = {"date": inf["t"][:10], "niveau": niveau(force), "force_dh": force}
    if inf.get("previsionnel"):
        out["previsionnel"] = True
    if taches:
        out["sortie_taches"] = taches["t"][:10]
        if taches.get("previsionnel"):
            out["sortie_taches_previsionnelle"] = True
    out.update(extra or {})
    return out


def _borner(evenements: list[dict], max_passe: int = 5, max_futur: int = 6) -> dict:
    """Les max_passe derniers événements constatés et les max_futur premiers prévus ; le total reste indiqué."""
    tries = sorted(evenements, key=lambda x: x["date"])
    passes = [e for e in tries if not e.get("previsionnel")]
    futurs = [e for e in tries if e.get("previsionnel")]
    return {"nombre_sur_la_fenetre": len(tries), "evenements": passes[-max_passe:] + futurs[:max_futur]}


def synthese_mildiou(res: dict, now: datetime | None = None, passe_j: int = 14, futur_j: int = 7, meta: dict | None = None) -> dict:
    """Résumé compact (JSON) : infections récentes et prévues, sorties de taches attendues, nuits de fructification,
    potentiel journalier à 7 jours. Dates en AAAA-MM-JJ (UTC pour les événements, heure locale pour les jours)."""
    now = now or datetime.now(UTC)
    aujourd = now.strftime("%Y-%m-%d")
    debut = (now - timedelta(days=passe_j)).strftime("%Y-%m-%d")
    fin = (now + timedelta(days=futur_j)).strftime("%Y-%m-%d")

    primaires, secondaires, sorties, nuits = [], [], [], set()
    for c in res["cycles"]:
        inf, tach = c.get("infection"), c.get("taches")
        if inf and debut <= inf["t"][:10] <= fin:
            primaires.append(_evenement(inf, c.get("force_dh"), tach))
        if tach and aujourd <= tach["t"][:10] <= fin:
            sorties.append({"date": tach["t"][:10], "origine": "primaire", "infection": inf["t"][:10],
                            **({"previsionnelle": True} if tach.get("previsionnel") else {})})
        nuits.update(s[:10] for s in c.get("sporulations", []) if debut <= s[:10] <= aujourd)
    for e in res.get("secondaires", []):
        inf, tach = e["infection"], e.get("taches")
        if debut <= inf["t"][:10] <= fin:
            secondaires.append(_evenement(inf, e.get("force_dh"), tach, {"generation": e["generation"]}))
        if tach and aujourd <= tach["t"][:10] <= fin:
            sorties.append({"date": tach["t"][:10], "origine": f"secondaire g{e['generation']}", "infection": inf["t"][:10],
                            **({"previsionnelle": True} if tach.get("previsionnel") else {})})
        spor = e.get("sporulation")
        if spor and debut <= spor["t"][:10] <= aujourd:
            nuits.add(spor["t"][:10])

    jours = [{"date": d["date"], "tmoy": d["tmoy"], "infection_primaire_dh": d["force_infection_dh"],
              "infection_secondaire_dh": d.get("force_secondaire_dh", 0.0)}
             for d in res["jours"] if aujourd <= d["date"] <= fin]
    mat = res["maturation"]
    sortie = {
        "avertissement": AVERTISSEMENT,
        "profil": (meta or {}).get("profil", PROFIL),
        "fenetre": {"du": debut, "au": fin},
        "maturation": {"date_maturite": mat.get("date_maturite"), "pourcentage": mat.get("pct"), "statut": mat.get("statut")},
        "infections_primaires": _borner(primaires),
        "infections_secondaires": _borner(secondaires),
        "sorties_taches_attendues": sorted(sorties, key=lambda x: x["date"])[:10],
        "nuits_fructification_recentes": sorted(nuits)[-8:],
        "potentiel_journalier": jours,
        "niveaux": "faible < 50, modérée 50-100, forte 100-200, très forte >= 200 (°C·h)",
    }
    if meta and meta.get("meteo_perimee"):
        sortie["alerte_meteo"] = f"météo en cache périmé ({meta['meteo_age_s'] // 3600} h) : le service météo n'a pas répondu"
    return sortie


def synthese_mildiou_pour_prompt(lat, lon) -> dict | None:
    """Synthèse du moteur, ou None si le moteur ne peut pas répondre (Pilot retombe alors sur l'ancien modèle)."""
    try:
        res, meta = calculer(lat, lon)
        return synthese_mildiou(res, meta=meta)
    except Exception as e:                                          # noqa: BLE001
        print(f"[epidemio] moteur indisponible, repli sur l'ancien modèle : {e}", file=sys.stderr)
        return None


REGLES_REDACTION = (
    "Règles de rédaction pour le mildiou :\n"
    "- Les niveaux (faible, modérée, forte, très forte) qualifient l'intensité d'une infection POSSIBLE d'après la météo ; "
    "ne parle jamais de contamination avérée ni de dégâts constatés.\n"
    "- Le moteur ignore les traitements : ne dis rien de la protection en place ni de ce qui a été appliqué.\n"
    "- Les éléments « previsionnel » dépendent de la météo annoncée : formule-les au conditionnel.\n"
    "- Une infection secondaire suppose des taches sporulantes : ne l'évoque que si « infections_secondaires » contient "
    "des événements (« nombre_sur_la_fenetre » > 0).\n"
    "- Les listes sont bornées aux événements les plus récents et aux prochains ; « nombre_sur_la_fenetre » donne le total.\n"
    "- Les sorties de taches sont des dates attendues d'après la température, pas des observations."
)


def bloc_epidemio_prompt(synthese_ancienne: dict | None, moteur: dict | None) -> str:
    """Bloc « SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE » du prompt. Sans moteur : l'ancien contenu, inchangé. Avec moteur : le
    mildiou vient du moteur, l'oïdium et les fenêtres de traitement de l'ancien modèle journalier."""
    if not moteur:
        return "SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n" + json.dumps(synthese_ancienne, ensure_ascii=False, indent=2)
    autres = {k: v for k, v in (synthese_ancienne or {}).items() if k not in CLES_MILDIOU_ANCIEN}
    return ("SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n\n"
            "MILDIOU (nouveau moteur horaire) :\n" + json.dumps(moteur, ensure_ascii=False, indent=2) + "\n\n"
            + REGLES_REDACTION + "\n\n"
            "OÏDIUM ET FENÊTRES DE TRAITEMENT (modèle journalier) :\n" + json.dumps(autres, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Route Flask
# ---------------------------------------------------------------------------
try:
    from flask import Blueprint, jsonify, request
except ImportError:                                                 # tests sans Flask
    Blueprint = None

if Blueprint is not None:
    bp_epidemio = Blueprint("epidemio_moteur", __name__)

    @bp_epidemio.route("/api/epidemio-moteur", methods=["GET"])
    def get_epidemio_moteur():
        """Synthèse du moteur mildiou pour une position. ?complet=1 renvoie en plus la sortie brute du moteur."""
        try:
            lat = request.args.get("lat", COORDS_DEFAUT["lat"])
            lon = request.args.get("lon", COORDS_DEFAUT["lon"])
            res, meta = calculer(lat, lon)
            out = synthese_mildiou(res, meta=meta)
            if request.args.get("complet") == "1":
                out["moteur_complet"] = {k: res[k] for k in ("maturation", "cycles", "secondaires", "jours", "avertissements")}
            return jsonify(out)
        except MoteurIndisponible as e:
            return jsonify({"error": str(e)}), 503
        except (ValueError, TypeError) as e:
            return jsonify({"error": f"paramètres invalides : {e}"}), 400
        except Exception as e:                                      # noqa: BLE001
            return jsonify({"error": f"erreur du moteur épidémiologique : {e}"}), 502
