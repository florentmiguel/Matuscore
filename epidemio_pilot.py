# -*- coding: utf-8 -*-
"""
Pont entre Pilot et le moteur épidémiologique (dépôt « epidemio »)
===================================================================

À placer à côté de serveur_vitisens.py. Rien n'est copié du moteur : il est chargé depuis le dépôt epidemio
(variable d'environnement EPIDEMIO_PATH, défaut ~/epidemio). S'il est absent ou en panne, Pilot démarre et
fonctionne comme avant (voir MoteurIndisponible et synthese_mildiou_pour_prompt).

Ce module fournit :
  * serie_horaire()           météo horaire du 1er janvier à la fin de la prévision, depuis l'HISTORIQUE LOCAL (SQLite) :
                              le passé est gardé sur disque, seuls les jours manquants sont demandés à Open-Meteo
  * calculer()                lance le moteur (profil EPIDEMIO_PROFIL, défaut « calage_2026 »)
  * synthese_mildiou()        résumé JSON compact pour le tableau de bord et pour le prompt de l'IA
  * bloc_epidemio_prompt()    bloc de texte du prompt de /api/generer-texte-bulletin
  * bp_epidemio               blueprint Flask : GET /api/epidemio-moteur?lat=..&lon=..[&complet=1]
                                                GET /api/epidemio-moteur/etat  (état de l'historique et appels consommés)

Le moteur évalue un DANGER théorique, indépendant des traitements : ce n'est ni une contamination constatée, ni
une consigne de traitement. Le conseil reste celui du conseiller.

NB : l'API gratuite d'Open-Meteo est réservée à un usage non commercial (offre payante pour un usage commercial).
L'attribution à Open-Meteo (licence CC BY 4.0) est requise.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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


def message_moteur_introuvable(chemin: str, erreur: Exception) -> str:
    msg = f"moteur introuvable ou inutilisable dans {chemin} : {erreur}"
    if "historique_meteo" in str(erreur):
        msg += " — le dépôt epidemio n'est pas à jour : cd ~/epidemio && git pull (après avoir poussé la nouvelle version depuis le PC)"
    return msg


def charger_moteur():
    """(mildiou_primaire, historique_meteo). Le dossier est AJOUTÉ en fin de sys.path : il ne peut pas masquer un module
    de Pilot."""
    chemin = chemin_moteur()
    if chemin not in sys.path:
        sys.path.append(chemin)
    try:
        import mildiou_primaire as mp
        import historique_meteo as hm
    except Exception as e:                                          # noqa: BLE001 : toute panne d'import est « indisponible »
        raise MoteurIndisponible(message_moteur_introuvable(chemin, e)) from e
    return mp, hm


# ---------------------------------------------------------------------------
# Météo : historique local, conversion
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


_HISTORIQUES: dict = {}
_PANNE_JUSQU = 0.0                       # disjoncteur : après une panne météo, on n'insiste pas pendant 2 minutes
DELAI_PANNE_S = 120


def _get_json_court(url: str, delai: int = 30) -> dict:
    """Appel HTTP avec un délai court (30 s) : un bulletin ne doit pas rester bloqué sur une API qui ne répond pas."""
    req = urllib.request.Request(url, headers={"User-Agent": "VITI-Sens-Pilot/1"})
    try:
        with urllib.request.urlopen(req, timeout=delai) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Open-Meteo a répondu {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Open-Meteo injoignable : {e.reason}") from e


def historique(chemin: str | None = None):
    """L'historique météo local (un objet par fichier de base, partagé par les threads du processus)."""
    mp, hm = charger_moteur()
    chemin = chemin or hm.chemin_base()
    if chemin not in _HISTORIQUES:
        _HISTORIQUES[chemin] = hm.Historique(chemin)
    return _HISTORIQUES[chemin]


def archive_autorisee() -> bool:
    """EPIDEMIO_ARCHIVE=0 : ne jamais appeler l'API historique d'Open-Meteo (plan Standard, qui ne l'inclut pas)."""
    return os.environ.get("EPIDEMIO_ARCHIVE", "1") != "0"


def serie_horaire(lat, lon, now: datetime | None = None, get=None) -> tuple[list, dict]:
    """(lignes, infos météo). Complète l'historique local avec le minimum d'appels (≈ 1 unité par rafraîchissement, durée de
    vie EPIDEMIO_TTL_S, défaut 1 h), puis le relit. Si Open-Meteo ne répond pas, l'historique déjà stocké sert quand même
    (meteo_perimee = True) ; s'il est vide ou ne commence pas le 1er janvier, MoteurIndisponible."""
    global _PANNE_JUSQU
    mp, hm = charger_moteur()
    now = now or datetime.now(UTC)
    h = historique()
    rapport, erreur = None, None
    if time.time() < _PANNE_JUSQU:
        erreur = "service météo en pause après une panne récente"
    else:
        try:
            rapport = h.mettre_a_jour(lat, lon, now=now, get=get or _get_json_court, archive_autorisee=archive_autorisee(),
                                      ttl_prevision_s=int(os.environ.get("EPIDEMIO_TTL_S", "3600")))
        except hm.HistoriqueIncomplet as e:
            raise MoteurIndisponible(str(e)) from e
        except Exception as e:                                      # noqa: BLE001 : réseau, quota, réponse inattendue...
            erreur = str(e)
            _PANNE_JUSQU = time.time() + DELAI_PANNE_S
    lignes = h.serie(lat, lon, now.year)
    if not lignes:
        raise MoteurIndisponible(f"aucune météo disponible pour {lat}, {lon} : {erreur or 'historique vide'}")
    etat = h.etat(lat, lon, now)
    maj = etat["maj_prevision"]
    age = int((now - datetime.strptime(maj, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)).total_seconds()) if maj else None
    return lignes, {"meteo_perimee": erreur is not None, "meteo_age_s": age, "meteo_trous": etat["heures_manquantes"],
                    "unites_open_meteo": rapport["unites"] if rapport else 0.0, "erreur_meteo": erreur}


def calculer(lat, lon, now: datetime | None = None, get=None) -> tuple[dict, dict]:
    """(résultat complet du moteur, métadonnées)."""
    mp, hm = charger_moteur()
    now = now or datetime.now(UTC)
    lignes, infos = serie_horaire(lat, lon, now, get)
    res = mp.calculer_saison(lignes_vers_rows(lignes), float(lat), float(lon), params=mp.charger_profil(PROFIL), now=now)
    return res, {"profil": PROFIL, "calcule_le": now.strftime("%Y-%m-%dT%H:%MZ"), **infos}


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


SEUIL_TENDANCE_DH = 50.0                  # en dessous, la charge d'infection est négligeable : pression « faible »
RAPPORT_HAUSSE, RAPPORT_BAISSE = 1.25, 0.75


def _charge(evenements: list[dict], debut: str, fin: str) -> float:
    return round(sum(e.get("force_dh") or 0.0 for e in evenements if debut <= e["date"] <= fin), 1)


def _tendance(evenements: list[dict], now: datetime) -> dict:
    """Pression d'infection : charge cumulée (°C·h) des 7 prochains jours comparée à celle des 7 derniers.
    Règle déterministe, pour que le titre de l'analyse ne dépende pas de l'humeur de l'IA."""
    def jour(n):
        return (now + timedelta(days=n)).strftime("%Y-%m-%d")
    recente, prevue = _charge(evenements, jour(-7), jour(-1)), _charge(evenements, jour(0), jour(7))
    if max(recente, prevue) < SEUIL_TENDANCE_DH:
        libelle = "PRESSION FAIBLE"
    elif prevue >= RAPPORT_HAUSSE * recente:
        libelle = "PRESSION EN HAUSSE"
    elif prevue <= RAPPORT_BAISSE * recente:
        libelle = "PRESSION EN BAISSE"
    else:
        libelle = "PRESSION STABLE"
    return {"libelle": libelle, "charge_recente_dh": recente, "charge_prevue_dh": prevue,
            "regle": "charge des 7 prochains jours comparée à celle des 7 derniers (hausse >= x1,25 ; baisse <= x0,75 ; "
                     "faible si les deux sont sous 50 °C·h)"}


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
    tendance = _tendance(primaires + secondaires, now)                     # sur les listes COMPLÈTES, avant bornage
    sortie = {
        "avertissement": AVERTISSEMENT,
        "profil": (meta or {}).get("profil", PROFIL),
        "fenetre": {"du": debut, "au": fin},
        "maturation": {"date_maturite": mat.get("date_maturite"), "pourcentage": mat.get("pct"), "statut": mat.get("statut")},
        "tendance": tendance,
        "titre": f"RISQUE MILDIOU — {tendance['libelle']}",
        "infections_primaires": _borner(primaires),
        "infections_secondaires": _borner(secondaires),
        "sorties_taches_attendues": sorted(sorties, key=lambda x: x["date"])[:10],
        "nuits_fructification_recentes": sorted(nuits)[-8:],
        "potentiel_journalier": jours,
        "niveaux": "faible < 50, modérée 50-100, forte 100-200, très forte >= 200 (°C·h)",
    }
    alertes = []
    if meta and meta.get("meteo_perimee"):
        age = meta.get("meteo_age_s")
        alertes.append("météo non rafraîchie" + (f" depuis {age // 3600} h" if age is not None else "")
                       + " : le service météo n'a pas répondu")
    if meta and meta.get("meteo_trous"):
        alertes.append(f"météo incomplète : {meta['meteo_trous']} heure(s) manquante(s) dans la saison")
    if alertes:
        sortie["alerte_meteo"] = " ; ".join(alertes)
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


def titre_oidium(synthese_ancienne: dict | None) -> str | None:
    """Titre de l'analyse oïdium, calculé sur les 7 jours de l'ancien modèle : niveau le plus haut, et tendance si les 3 derniers
    jours se distinguent nettement des 3 premiers (écart de score >= 10 sur 100)."""
    jours = (synthese_ancienne or {}).get("risques_oidium") or []
    niveaux = {"Nul": 0, "Faible": 1, "Modéré": 2, "Élevé": 3}
    valeurs = [niveaux.get(j.get("risque"), 0) for j in jours]
    if not valeurs:
        return None
    haut = max(valeurs)
    libelle = {0: "PRESSION NULLE", 1: "PRESSION FAIBLE", 2: "PRESSION MODÉRÉE", 3: "PRESSION ÉLEVÉE"}[haut]
    scores = [j.get("score") or 0 for j in jours]
    if haut > 0 and len(scores) >= 6:
        ecart = sum(scores[-3:]) / 3 - sum(scores[:3]) / 3
        libelle += " ET EN HAUSSE" if ecart >= 10 else " ET EN BAISSE" if ecart <= -10 else ""
    return f"RISQUE OÏDIUM — {libelle}"


def consigne_redaction(titres: dict) -> str:
    """Forme et ton des analyses du bulletin. Elle vit ici (et non dans le prompt du serveur) pour qu'on puisse l'ajuster sans
    retoucher serveur_vitisens.py."""
    def titre(cle, defaut):
        t = titres.get(cle)
        return f"« {t} »" if t else defaut
    t_mildiou = titre("mildiou", "« RISQUE MILDIOU — » suivi d'un libellé de pression que tu déduis des données")
    t_oidium = titre("oidium", "« RISQUE OÏDIUM — » suivi d'un libellé de pression que tu déduis des données")
    return (
        "CONSIGNE DE RÉDACTION (elle remplace toute indication de style donnée plus bas) :\n"
        "Les champs « risque_mildiou » et « risque_oidium » sont des analyses rédigées, de cette forme :\n"
        "- 1re ligne : le titre imposé ci-dessous, recopié exactement ;\n"
        "- puis 3 ou 4 paragraphes courts séparés par une ligne vide (100 à 150 mots en tout) : (1) l'événement principal à venir "
        "ou en cours et son contexte météo ; (2) les sorties de taches attendues et les infections dont elles proviennent ; "
        "(3) ce qui peut encore se produire et ce qui le favorise ; (4) la conclusion : où en est la dynamique et sur quelle "
        "période rester vigilant. Si les données n'ont pas de matière pour un paragraphe, supprime-le plutôt que de le remplir.\n"
        f"Titre imposé pour le mildiou : {t_mildiou}.\n"
        f"Titre imposé pour l'oïdium : {t_oidium}.\n"
        "Ton : celui d'un conseiller qui explique la situation à un vigneron, en phrases complètes et fluides, sans style télégraphique "
        "ni jargon inutile. Garde les chiffres utiles (dates au format JJ/MM, millimètres de pluie, pourcentage d'humectation, et "
        "l'intensité en °C·h pour la seule infection principale) sans les multiplier. N'écris jamais « modèle », « moteur » ni « données ».\n"
        "Les champs « reco_mildiou » et « reco_oidium » : 2 à 3 phrases concrètes et proportionnées au risque, sans titre.\n"
        "Si le champ « epi » (potentiel épidémique du mildiou) est demandé, c'est UNE seule courte phrase d'une quinzaine de mots, qui qualifie "
        "le niveau de potentiel infectieux de la semaine et sa raison principale, sans point final (le bulletin ajoute le sien).\n"
        "Dans le JSON, les retours à la ligne s'écrivent \\n."
    )


def bloc_epidemio_prompt(synthese_ancienne: dict | None, moteur: dict | None) -> str:
    """Bloc « SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE » du prompt, suivi de la consigne de rédaction. Sans moteur : l'ancien contenu
    pour les données, mais la même consigne de forme. Avec moteur : le mildiou vient du moteur, l'oïdium et les fenêtres de
    traitement de l'ancien modèle journalier."""
    consigne = consigne_redaction({"mildiou": (moteur or {}).get("titre"), "oidium": titre_oidium(synthese_ancienne)})
    if not moteur:
        return ("SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n" + json.dumps(synthese_ancienne, ensure_ascii=False, indent=2)
                + "\n\n" + consigne)
    autres = {k: v for k, v in (synthese_ancienne or {}).items() if k not in CLES_MILDIOU_ANCIEN}
    return ("SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n\n"
            "MILDIOU (nouveau moteur horaire) :\n" + json.dumps(moteur, ensure_ascii=False, indent=2) + "\n\n"
            + REGLES_REDACTION + "\n\n"
            "OÏDIUM ET FENÊTRES DE TRAITEMENT (modèle journalier) :\n" + json.dumps(autres, ensure_ascii=False, indent=2)
            + "\n\n" + consigne)


# ---------------------------------------------------------------------------
# Risque par commune de client
# ---------------------------------------------------------------------------
BOITE_CLIENT = (47.5, 50.0, 2.5, 5.5)          # latitude min/max, longitude min/max : au-delà, des coordonnées sont suspectes
BOITE_CHAMPAGNE = (48.0, 49.7, 2.8, 5.2)       # zone dans laquelle un résultat de géocodage est accepté
URL_GEOCODAGE = "https://geocoding-api.open-meteo.com/v1/search"
ESSAI_GEOCODAGE_J = 7                          # une commune introuvable n'est pas redemandée avant 7 jours


def _dans(boite, lat, lon) -> bool:
    return boite[0] <= lat <= boite[1] and boite[2] <= lon <= boite[3]


def _nombre(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x else None                    # 0 = champ non renseigné


def cle_commune(nom: str) -> str:
    """SAINT-MARTIN D'ABLOIS et Saint Martin d'Ablois donnent la même clé."""
    return " ".join((nom or "").upper().replace("-", " ").replace("'", " ").replace("’", " ").split())


def _base_donnees() -> sqlite3.Connection:
    """Même fichier SQLite que l'historique météo : une table de plus pour mémoriser les géocodages."""
    c = sqlite3.connect(historique().chemin, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS geocodage (cle TEXT PRIMARY KEY, lat REAL, lon REAL, trouve INTEGER NOT NULL, maj TEXT NOT NULL)")
    return c


def _choisir_resultat(resultats: list[dict]) -> dict | None:
    """Parmi les résultats du géocodeur : d'abord la Marne, puis le reste du Grand Est, et toujours dans la zone Champagne.
    Un nom de commune est souvent partagé avec d'autres régions (ex. « Ay »)."""
    dans = [r for r in resultats if _dans(BOITE_CHAMPAGNE, r["latitude"], r["longitude"])]
    for r in dans:
        if (r.get("admin2") or "").lower() == "marne":
            return r
    for r in dans:
        if (r.get("admin1") or "") == "Grand Est":
            return r
    return None


def geocoder_commune(commune: str, get_json=None, now: datetime | None = None) -> tuple[float, float] | None:
    """(latitude, longitude) d'une commune de Champagne, ou None. Réponse mémorisée dans la base (succès : définitivement ;
    échec : 7 jours), de sorte qu'un bulletin ne rappelle jamais le géocodeur pour une commune déjà vue."""
    now = now or datetime.now(UTC)
    cle = cle_commune(commune)
    if not cle:
        return None
    c = _base_donnees()
    try:
        ligne = c.execute("SELECT lat, lon, trouve, maj FROM geocodage WHERE cle=?", (cle,)).fetchone()
        if ligne:
            lat, lon, trouve, maj = ligne
            if trouve:
                return lat, lon
            if (now - datetime.strptime(maj, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)).days < ESSAI_GEOCODAGE_J:
                return None
        get_json = get_json or _get_json_court
        nom = " ".join(commune.split()).title()
        trouve_res = None
        for essai in (nom, nom.replace(" ", "-")):                  # « Saint Martin » puis « Saint-Martin »
            q = urllib.parse.urlencode({"name": essai, "count": 10, "language": "fr", "format": "json", "countryCode": "FR"})
            try:
                trouve_res = _choisir_resultat((get_json(f"{URL_GEOCODAGE}?{q}") or {}).get("results") or [])
            except Exception:                                       # noqa: BLE001 : panne réseau : on ne mémorise pas l'échec
                return None
            if trouve_res:
                break
        c.execute("INSERT INTO geocodage(cle, lat, lon, trouve, maj) VALUES (?,?,?,?,?) ON CONFLICT(cle) DO UPDATE SET "
                  "lat=excluded.lat, lon=excluded.lon, trouve=excluded.trouve, maj=excluded.maj",
                  (cle, trouve_res["latitude"] if trouve_res else None, trouve_res["longitude"] if trouve_res else None,
                   1 if trouve_res else 0, now.strftime("%Y-%m-%dT%H:%M")))
        c.commit()
        return (trouve_res["latitude"], trouve_res["longitude"]) if trouve_res else None
    finally:
        c.close()


def resoudre_position(client: dict, get_json=None, now: datetime | None = None) -> dict | None:
    """Position météo d'un client : ses coordonnées si elles sont renseignées et plausibles, sinon le géocodage de sa commune.
    None si rien ne permet de situer le client (il garde alors l'analyse régionale)."""
    commune = (client.get("commune") or "").strip()
    lat, lon = _nombre(client.get("latitude")), _nombre(client.get("longitude"))
    if lat and lon and _dans(BOITE_CLIENT, lat, lon):
        return {"lat": lat, "lon": lon, "source": "client", "commune": commune}
    if commune:
        r = geocoder_commune(commune, get_json, now)
        if r:
            return {"lat": r[0], "lon": r[1], "source": "geocodage", "commune": commune}
    return None


def liste_dates_fr(dates: list[str]) -> str:
    """['2026-10-06', '2026-10-09', '2026-10-10'] -> « les 06, 09 et 10/10 » ; sur deux mois : « les 30/09 et 02/10 »."""
    ds = sorted(set(dates))
    if not ds:
        return ""
    if len(ds) == 1:
        return f"le {ds[0][8:10]}/{ds[0][5:7]}"
    if len({d[5:7] for d in ds}) == 1:
        jours = [d[8:10] for d in ds]
        return f"les {', '.join(jours[:-1])} et {jours[-1]}/{ds[0][5:7]}"
    items = [f"{d[8:10]}/{d[5:7]}" for d in ds]
    return f"les {', '.join(items[:-1])} et {items[-1]}"


def phrase_commune(syn: dict, now: datetime | None = None) -> str:
    """Texte clair (2 à 3 phrases) sur la situation d'une commune, entièrement déterministe : il ne fait que dire les faits du moteur,
    sans aucune part d'invention, donc sans relecture nécessaire. Toujours au conditionnel des « conditions favorables »."""
    now = now or datetime.now(UTC)
    aujourd = now.strftime("%Y-%m-%d")
    evts = syn["infections_primaires"]["evenements"] + syn["infections_secondaires"]["evenements"]
    prevus = [e for e in evts if e["date"] >= aujourd]
    recents = [e for e in evts if (now - timedelta(days=3)).strftime("%Y-%m-%d") <= e["date"] < aujourd]
    phrases = []
    if prevus:
        e = max(prevus, key=lambda x: x.get("force_dh") or 0)
        phrases.append(f"Les conditions favorables à une infection {e['niveau']} sont attendues autour du {e['date'][8:10]}/{e['date'][5:7]}.")
    elif recents:
        e = max(recents, key=lambda x: x.get("force_dh") or 0)
        phrases.append(f"Les conditions favorables à une infection {e['niveau']} ont été réunies le {e['date'][8:10]}/{e['date'][5:7]}.")
    else:
        phrases.append("Aucune infection significative n'est attendue dans les 7 prochains jours.")
    sorties = [s["date"] for s in syn.get("sorties_taches_attendues", [])]
    if sorties:
        phrases.append(f"Des sorties de taches sont attendues {liste_dates_fr(sorties[:4])}.")
    phrases.append({"PRESSION EN HAUSSE": "La pression est en hausse : la vigilance reste de mise.",
                    "PRESSION EN BAISSE": "La pression diminue progressivement.",
                    "PRESSION STABLE": "La pression reste stable.",
                    "PRESSION FAIBLE": "La pression reste faible à ce stade."}[syn["tendance"]["libelle"]])
    return " ".join(phrases)


NIVEAU_EPI = {"faible": "faible", "modérée": "modéré", "forte": "fort", "très forte": "très fort"}
TENDANCE_EPI = {"PRESSION EN HAUSSE": "en hausse", "PRESSION EN BAISSE": "en baisse", "PRESSION STABLE": "stable", "PRESSION FAIBLE": None}


def epi_depuis_synthese(syn: dict, now: datetime | None = None) -> str:
    """Potentiel épidémique (EPI) du bulletin : une courte phrase SANS point final (le bulletin ajoute le sien), entièrement déterministe,
    tirée des mêmes événements que le reste de l'analyse. Exemples : « modéré et en hausse, avec une infection attendue autour du 07/10 »,
    « faible, aucune infection significative attendue dans les 7 prochains jours »."""
    now = now or datetime.now(UTC)
    aujourd = now.strftime("%Y-%m-%d")
    semaine = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    evts = syn["infections_primaires"]["evenements"] + syn["infections_secondaires"]["evenements"]
    prevus = [e for e in evts if e["date"] >= aujourd]
    recents = [e for e in evts if semaine <= e["date"] < aujourd]
    tendance = TENDANCE_EPI[syn["tendance"]["libelle"]]

    def jj(e):
        return f"{e['date'][8:10]}/{e['date'][5:7]}"
    if prevus:
        e = max(prevus, key=lambda x: x.get("force_dh") or 0)
        return f"{NIVEAU_EPI[e['niveau']]}{' et ' + tendance if tendance else ''}, avec une infection attendue autour du {jj(e)}"
    if recents:
        e = max(recents, key=lambda x: x.get("force_dh") or 0)
        return f"{NIVEAU_EPI[e['niveau']]}, infection récente le {jj(e)}{' et pression ' + tendance if tendance else ''}"
    return "faible, aucune infection significative attendue dans les 7 prochains jours"


def epi_pour_requete(lat, lon, now: datetime | None = None, get=None) -> str | None:
    """EPI calculé par le moteur pour une position (la position par défaut si elle n'est pas fournie), ou None si le moteur ne peut pas
    répondre. NE LÈVE JAMAIS."""
    try:
        lat = COORDS_DEFAUT["lat"] if lat in (None, "") else lat
        lon = COORDS_DEFAUT["lon"] if lon in (None, "") else lon
        res, meta = calculer(lat, lon, now=now, get=get)
        return epi_depuis_synthese(synthese_mildiou(res, now=now, meta=meta), now)
    except Exception as e:                                          # noqa: BLE001
        print(f"[epidemio] EPI non calculé : {e}", file=sys.stderr)
        return None


def epi_si_absent(donnees, lat, lon, now: datetime | None = None, get=None) -> dict | None:
    """Pour la réponse de /api/generer-texte-bulletin : si elle n'a pas d'« epi » (serveur à 4 champs, ou IA qui l'oublie), renvoie la
    réponse complétée par l'EPI du moteur ; sinon None (rien à changer). Les autres champs ne sont jamais touchés."""
    if not isinstance(donnees, dict) or (donnees.get("epi") or "").strip():
        return None
    epi = epi_pour_requete(lat, lon, now=now, get=get)
    return {**donnees, "epi": epi} if epi else None


def risque_jour(force_dh: float) -> str:
    """Libellé d'un jour pour le tableau « Prévisions météo » du bulletin : mêmes mots (donc mêmes couleurs) que l'ancien modèle,
    avec les seuils du moteur (faible < 50, modéré 50-100, élevé 100-200, très élevé >= 200 °C·h)."""
    if not force_dh:
        return "Nul"
    a, b, c = SEUILS_NIVEAU
    return "Faible" if force_dh < a else "Modéré" if force_dh < b else "Élevé" if force_dh < c else "Très élevé"


def bloc_commune_pour_client(client: dict, now: datetime | None = None, get=None, get_json=None) -> dict | None:
    """Bloc « situation sur votre commune » d'un bulletin client (texte, et risque de chacun des 7 jours pour le tableau des
    prévisions), ou None. NE LÈVE JAMAIS : un bulletin ne doit pas échouer à
    cause du moteur ; sans bloc, il reste exactement tel qu'avant."""
    try:
        pos = resoudre_position(client, get_json, now)
        if not pos:
            return None
        res, meta = calculer(pos["lat"], pos["lon"], now=now, get=get)
        syn = synthese_mildiou(res, now=now, meta=meta)
        nom = pos["commune"].title() if pos["commune"] else "votre commune"
        risque_jours = {j["date"]: risque_jour(j["infection_primaire_dh"] + j["infection_secondaire_dh"])
                        for j in syn["potentiel_journalier"]}
        return {"titre": f"Situation sur votre commune ({nom}), d'après la météo : ", "texte": phrase_commune(syn, now),
                "tendance": syn["tendance"]["libelle"], "source_position": pos["source"], "risque_jours": risque_jours}
    except Exception as e:                                          # noqa: BLE001
        print(f"[epidemio] bloc commune indisponible : {e}", file=sys.stderr)
        return None


def lire_clients(db_path: str | None = None) -> list[dict]:
    """Colonnes strictement nécessaires de la table clients (aucune donnée de contact)."""
    chemin = db_path or os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "vitisens.db")
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT id, commune, latitude, longitude FROM clients ORDER BY id")]
    finally:
        c.close()


def apercu_communes(clients: list[dict], now: datetime | None = None, get=None, get_json=None, budget_s: float | None = None) -> dict:
    """Une ligne par position (plusieurs clients d'une même commune partagent un calcul) : tendance, prochaine infection, sorties de
    taches. Un budget de temps évite de dépasser le délai du serveur au premier calcul (≈ 20 unités d'appel par position)."""
    now = now or datetime.now(UTC)
    debut = time.monotonic()
    groupes, non_resolus = {}, []
    for cl in clients:
        pos = resoudre_position(cl, get_json, now)
        if not pos:
            non_resolus.append({"id_client": cl.get("id"), "commune": cl.get("commune"),
                                "raison": "commune non localisable : renseigne latitude et longitude dans la fiche client"})
            continue
        cle = f"{pos['lat']:.2f}_{pos['lon']:.2f}"
        g = groupes.setdefault(cle, {"position": cle, "lat": pos["lat"], "lon": pos["lon"], "source": pos["source"],
                                     "communes": set(), "clients": 0})
        g["communes"].add(cle_commune(pos["commune"]))
        g["clients"] += 1
    aujourd = now.strftime("%Y-%m-%d")
    lignes = []
    for cle in sorted(groupes):
        g = groupes[cle]
        ligne = {"position": cle, "communes": sorted(g["communes"]), "clients": g["clients"], "source_position": g["source"]}
        if budget_s is not None and time.monotonic() - debut > budget_s:
            ligne.update({"calcule": False, "raison": "délai dépassé : relance pour poursuivre"})
        else:
            try:
                res, meta = calculer(g["lat"], g["lon"], now=now, get=get)
                syn = synthese_mildiou(res, now=now, meta=meta)
                evts = [e for e in syn["infections_primaires"]["evenements"] + syn["infections_secondaires"]["evenements"]
                        if e["date"] >= aujourd]
                prochaine = max(evts, key=lambda x: x.get("force_dh") or 0) if evts else None
                ligne.update({"calcule": True, "tendance": syn["tendance"]["libelle"],
                              "prochaine_infection": ({"date": prochaine["date"], "niveau": prochaine["niveau"],
                                                       "force_dh": prochaine["force_dh"]} if prochaine else None),
                              "sorties_taches": [s["date"] for s in syn["sorties_taches_attendues"]][:6],
                              "unites_open_meteo": meta["unites_open_meteo"]})
                if syn.get("alerte_meteo"):
                    ligne["alerte_meteo"] = syn["alerte_meteo"]
            except Exception as e:                                  # noqa: BLE001
                ligne.update({"calcule": False, "raison": str(e)})
        lignes.append(ligne)
    return {"positions": lignes, "non_resolus": non_resolus, "clients": len(clients),
            "unites_open_meteo": round(sum(l.get("unites_open_meteo", 0) for l in lignes), 2)}


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

    @bp_epidemio.route("/api/epidemio-moteur/communes", methods=["GET"])
    def get_epidemio_moteur_communes():
        """Risque par commune de client : une ligne par position, avec les communes qui la partagent. Le premier appel peut
        calculer plusieurs positions (≈ 20 unités d'appel chacune) : un budget de 80 s évite de dépasser le délai du serveur ;
        relancer pour poursuivre. Préférer la commande « python3 epidemio_pilot.py communes » pour le premier remplissage."""
        try:
            return jsonify(apercu_communes(lire_clients(), budget_s=80))
        except MoteurIndisponible as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:                                      # noqa: BLE001
            return jsonify({"error": f"erreur du calcul par commune : {e}"}), 502

    @bp_epidemio.route("/api/epidemio-moteur/etat", methods=["GET"])
    def get_epidemio_moteur_etat():
        """État de l'historique météo local d'une position et appels Open-Meteo consommés (30 jours, aujourd'hui)."""
        try:
            lat = float(request.args.get("lat", COORDS_DEFAUT["lat"]))
            lon = float(request.args.get("lon", COORDS_DEFAUT["lon"]))
            h = historique()
            return jsonify({"historique": h.etat(lat, lon), "usage": h.usage(),
                            "archive_autorisee": archive_autorisee(),
                            "limites_offre_gratuite": {"par_minute": 600, "par_heure": 5000, "par_jour": 10000,
                                                       "par_mois": 300000}})
        except MoteurIndisponible as e:
            return jsonify({"error": str(e)}), 503
        except (ValueError, TypeError) as e:
            return jsonify({"error": f"paramètres invalides : {e}"}), 400
        except Exception as e:                                      # noqa: BLE001
            return jsonify({"error": f"erreur de l'historique météo : {e}"}), 502


# ---------------------------------------------------------------------------
# Ligne de commande : python3 epidemio_pilot.py communes [--db chemin]
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Moteur épidémiologique de Pilot : risque par commune de client")
    ap.add_argument("action", choices=("communes",))
    ap.add_argument("--db", help="base de Pilot (défaut : $DB_PATH, puis vitisens.db à côté de ce fichier)")
    a = ap.parse_args(argv)
    try:
        r = apercu_communes(lire_clients(a.db))
    except MoteurIndisponible as e:
        print(f"Moteur indisponible : {e}")
        return 1
    print(f"{r['clients']} clients, {len(r['positions'])} positions, {r['unites_open_meteo']} unité(s) d'appel Open-Meteo consommée(s)\n")
    for p in r["positions"]:
        noms = ", ".join(p["communes"])
        if not p["calcule"]:
            print(f"  {noms:<42} NON CALCULÉ : {p['raison']}")
            continue
        pi = p["prochaine_infection"]
        suite = f"{pi['date']} {pi['niveau']} ({pi['force_dh']:.0f} °C·h)" if pi else "aucune"
        print(f"  {noms:<42} {p['tendance']:<20} prochaine infection : {suite}")
    for n in r["non_resolus"]:
        print(f"  SANS POSITION : client {n['id_client']} (commune « {n['commune']} ») — {n['raison']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
