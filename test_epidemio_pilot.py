#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests du pont Pilot / moteur épidémiologique. Lancer : python3 test_epidemio_pilot.py

Variable utile : EPIDEMIO_PATH = dossier du dépôt epidemio (défaut ~/epidemio)."""
import contextlib
import io
import json
import math
import os
import random
import shutil
import sqlite3
import tempfile
import types
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("EPIDEMIO_PATH", os.path.expanduser("~/epidemio"))
import brancher_epidemio as br
import epidemio_pilot as ep

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)          # fenêtre : 21/09 au 12/10


def saison(jusqu_a_h=6816):
    """Saison synthétique déterministe (lignes au format Open-Meteo)."""
    random.seed(42)
    t0, lignes, pr = datetime(2026, 1, 1, tzinfo=UTC), [], 0
    for h in range(jusqu_a_h):
        t, j = t0 + timedelta(hours=h), h / 24
        temp = 10.5 - 9.0 * math.cos(2 * math.pi * (j - 15) / 365) + 4.5 * math.sin(2 * math.pi * ((h % 24) - 9) / 24) + random.gauss(0, 1.0)
        if pr <= 0 and random.random() < 0.035:
            pr = random.randint(2, 9)
        pluie = round(random.uniform(0.1, 2.5), 1) if pr > 0 else 0.0
        pr -= 1
        hr = min(100, max(30, 80 - 20 * math.sin(2 * math.pi * ((h % 24) - 9) / 24) + (12 if pluie > 0 else 0) + random.gauss(0, 6)))
        lignes.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "temperature_2m": round(temp, 1),
                       "relative_humidity_2m": round(hr), "dew_point_2m": None, "precipitation": pluie})
    return lignes


class FauxOpenMeteo:
    """Open-Meteo simulé à partir de la saison synthétique : archive (00 h-23 h) et prévision (past_days / forecast_days)."""

    def __init__(self, lignes, maintenant, panne=False):
        self.par_heure = {r["time"]: r for r in lignes}
        self.maintenant, self.panne, self.appels = maintenant, panne, []

    def __call__(self, url):
        if self.panne:
            raise RuntimeError("Open-Meteo injoignable")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "archive-api" in url:
            debut = datetime.fromisoformat(q["start_date"]).replace(tzinfo=UTC)
            fin = datetime.fromisoformat(q["end_date"]).replace(tzinfo=UTC) + timedelta(hours=23)
            self.appels.append("archive")
        else:
            minuit = datetime.combine(self.maintenant.date(), datetime.min.time(), tzinfo=UTC)
            debut = minuit - timedelta(days=int(q["past_days"]))
            fin = minuit + timedelta(days=int(q["forecast_days"])) - timedelta(hours=1)
            self.appels.append("prevision")
        heures, t = [], debut
        while t <= fin:
            heures.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(hours=1)
        lignes = [self.par_heure.get(h) for h in heures]
        col = lambda v: [l[v] if l else None for l in lignes]                        # noqa: E731
        return {"hourly": {"time": heures, "temperature_2m": col("temperature_2m"), "relative_humidity_2m": col("relative_humidity_2m"),
                           "dew_point_2m": col("dew_point_2m"), "precipitation": col("precipitation")}}


def ev(date, force, prev=False, taches=None, tprev=False):
    inf = {"t": f"{date}T10:00Z"}
    if prev:
        inf["previsionnel"] = True
    out = {"infection": inf, "force_dh": force}
    if taches:
        out["taches"] = {"t": f"{taches}T09:00Z", **({"previsionnel": True} if tprev else {})}
    return out


def faux_resultat():
    cycles = [
        {"id": 1, **ev("2026-09-25", 120.0, taches="2026-10-03"), "sporulations": ["2026-09-30T22:00Z", "2026-09-10T22:00Z"]},
        {"id": 2, **ev("2026-10-02", 45.0, taches="2026-10-10", tprev=True)},
        {"id": 3, **ev("2026-10-07", 210.0, prev=True, taches="2026-10-15", tprev=True)},      # taches au-delà de la fenêtre
        {"id": 4, **ev("2026-08-01", 60.0, taches="2026-08-09")},                                # hors fenêtre
        {"id": 5, **ev("2026-09-18", 70.0, taches="2026-10-08", tprev=True)},                    # infection avant la fenêtre
    ]
    secondaires = [{"id": i, "generation": 1, **ev(f"2026-09-{22 + i:02d}", 100.0 + i, taches=f"2026-10-{1 + i:02d}")}
                   for i in range(8)]                                                              # 8 constatées (22 au 29/09)
    secondaires += [{"id": 10 + i, "generation": 2, **ev(f"2026-10-{6 + i % 6:02d}", 150.0, prev=True)} for i in range(8)]   # 8 prévues
    secondaires[0]["sporulation"] = {"t": "2026-09-26T23:00Z"}
    jours = [{"date": f"2026-10-{d:02d}", "tmoy": 12.0, "force_infection_dh": float(d), "force_secondaire_dh": 0.5 * d}
             for d in range(1, 16)]
    return {"maturation": {"date_maturite": "2026-04-23", "pct": 100, "statut": "maturité acquise"},
            "cycles": cycles, "secondaires": secondaires, "jours": jours, "avertissements": [], "cycles_actifs": []}


class Base(unittest.TestCase):
    """Chaque test a son propre dossier de données (historique SQLite) et ses propres variables d'environnement."""
    VARIABLES = ("EPIDEMIO_DATA_DIR", "EPIDEMIO_ARCHIVE", "EPIDEMIO_TTL_S")

    def setUp(self):
        self.donnees = tempfile.mkdtemp()
        self.anciennes = {k: os.environ.get(k) for k in self.VARIABLES}
        os.environ["EPIDEMIO_DATA_DIR"] = self.donnees
        os.environ.pop("EPIDEMIO_ARCHIVE", None)
        os.environ.pop("EPIDEMIO_TTL_S", None)
        ep._HISTORIQUES.clear()
        ep._PANNE_JUSQU = 0.0
        self.lignes = saison()
        self.faux = FauxOpenMeteo(self.lignes, NOW)

    def tearDown(self):
        for k, v in self.anciennes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ep._HISTORIQUES.clear()
        ep._PANNE_JUSQU = 0.0
        shutil.rmtree(self.donnees, ignore_errors=True)


class TestConversion(unittest.TestCase):
    def test_lignes_vers_rows(self):
        rows = ep.lignes_vers_rows([
            {"time": "2026-05-01T01:00", "temperature_2m": 10.5, "relative_humidity_2m": 80, "dew_point_2m": 7.0, "precipitation": 0.0},
            {"time": "2026-05-01T00:00", "temperature_2m": "9.5", "relative_humidity_2m": None, "dew_point_2m": None, "precipitation": ""},
        ])
        self.assertEqual([r["t"].hour for r in rows], [0, 1])                      # triées
        self.assertEqual(rows[0]["t"].tzinfo, UTC)
        self.assertEqual((rows[0]["temp"], rows[0]["hr"], rows[0]["pluie"]), (9.5, None, None))
        self.assertEqual((rows[1]["temp"], rows[1]["hr"], rows[1]["rosee"]), (10.5, 80.0, 7.0))
        self.assertIsNone(rows[1]["mouille"])

    def test_heure_avec_fuseau_ramenee_en_utc(self):
        rows = ep.lignes_vers_rows([{"time": "2026-05-01T02:00+02:00", "temperature_2m": 10}])
        self.assertEqual(rows[0]["t"], datetime(2026, 5, 1, 0, 0, tzinfo=UTC))


class TestHistoriqueLocal(Base):
    def test_demarrage_a_froid_puis_rafraichissement_economique(self):
        lignes, infos = ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.assertEqual(len(lignes), 6816)
        self.assertEqual(self.faux.appels, ["archive", "prevision"])
        self.assertEqual(infos["unites_open_meteo"], 20.43)
        self.assertEqual((infos["meteo_perimee"], infos["meteo_trous"]), (False, 0))
        self.assertEqual(infos["meteo_age_s"], 0)
        # 2 h plus tard : une seule requête de prévision, environ 20 fois moins d'unités
        self.faux.appels.clear()
        _, infos = ep.serie_horaire(49.25, 3.96, NOW + timedelta(hours=2), self.faux)
        self.assertEqual(self.faux.appels, ["prevision"])
        self.assertEqual(infos["unites_open_meteo"], 1.07)

    def test_dans_la_duree_de_vie_aucun_appel(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.faux.appels.clear()
        _, infos = ep.serie_horaire(49.25, 3.96, NOW + timedelta(minutes=20), self.faux)
        self.assertEqual((self.faux.appels, infos["unites_open_meteo"]), ([], 0.0))
        self.assertEqual(infos["meteo_age_s"], 20 * 60)

    def test_la_duree_de_vie_se_regle_dans_l_environnement(self):
        os.environ["EPIDEMIO_TTL_S"] = "0"
        ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.faux.appels.clear()
        ep.serie_horaire(49.25, 3.96, NOW + timedelta(minutes=5), self.faux)
        self.assertEqual(self.faux.appels, ["prevision"])

    def test_panne_du_service_meteo_l_historique_stocke_sert_quand_meme(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.faux.panne = True
        lignes, infos = ep.serie_horaire(49.25, 3.96, NOW + timedelta(hours=3), self.faux)
        self.assertEqual(len(lignes), 6816)
        self.assertTrue(infos["meteo_perimee"])
        self.assertIn("injoignable", infos["erreur_meteo"])
        self.assertEqual(infos["meteo_age_s"], 3 * 3600)

    def test_panne_sans_aucune_donnee_stockee(self):
        self.faux.panne = True
        with self.assertRaises(ep.MoteurIndisponible) as e:
            ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.assertIn("injoignable", str(e.exception))

    def test_mode_sans_archive_exige_un_amorcage(self):
        os.environ["EPIDEMIO_ARCHIVE"] = "0"
        with self.assertRaises(ep.MoteurIndisponible) as e:
            ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.assertIn("ne commence pas le 1er janvier", str(e.exception))
        self.assertNotIn("archive", self.faux.appels)                              # l'API historique n'a JAMAIS été appelée

    def test_mode_sans_archive_apres_import_ne_fait_que_de_la_prevision(self):
        os.environ["EPIDEMIO_ARCHIVE"] = "0"
        ep.historique().importer_lignes(49.25, 3.96, self.lignes, now=NOW)
        lignes, infos = ep.serie_horaire(49.25, 3.96, NOW, self.faux)
        self.assertEqual(len(lignes), 6816)
        self.assertEqual(self.faux.appels, ["prevision"])
        self.assertEqual(infos["unites_open_meteo"], 1.07)


class TestSynthese(unittest.TestCase):
    def setUp(self):
        self.s = ep.synthese_mildiou(faux_resultat(), now=NOW)

    def test_niveaux(self):
        self.assertEqual([ep.niveau(x) for x in (None, 0, 49.9, 50, 99.9, 100, 199.9, 200, 500)],
                         [None, "faible", "faible", "modérée", "modérée", "forte", "forte", "très forte", "très forte"])

    def test_json_serialisable_et_fenetre(self):
        json.dumps(self.s)
        self.assertEqual(self.s["fenetre"], {"du": "2026-09-21", "au": "2026-10-12"})
        self.assertIn("INDÉPENDANT des traitements", self.s["avertissement"])

    def test_infections_primaires_dans_la_fenetre(self):
        dates = [e["date"] for e in self.s["infections_primaires"]["evenements"]]
        self.assertEqual(dates, ["2026-09-25", "2026-10-02", "2026-10-07"])         # 01/08 et 18/09 exclues
        self.assertEqual(self.s["infections_primaires"]["nombre_sur_la_fenetre"], 3)

    def test_champs_faux_omis_et_vrais_conserves(self):
        a, b, c = self.s["infections_primaires"]["evenements"]
        self.assertNotIn("previsionnel", a)
        self.assertNotIn("sortie_taches_previsionnelle", a)
        self.assertTrue(c["previsionnel"])
        self.assertTrue(b["sortie_taches_previsionnelle"])
        self.assertEqual((a["niveau"], b["niveau"], c["niveau"]), ("forte", "faible", "très forte"))

    def test_sorties_de_taches_attendues_seulement_a_venir_et_dans_la_fenetre(self):
        dates = [x["date"] for x in self.s["sorties_taches_attendues"]]
        self.assertIn("2026-10-10", dates)                                          # cycle 2
        self.assertIn("2026-10-08", dates)                                          # cycle 5, infection avant la fenêtre
        self.assertNotIn("2026-10-03", dates)                                       # déjà passée
        self.assertNotIn("2026-10-15", dates)                                       # au-delà de la fenêtre
        self.assertEqual(dates, sorted(dates))

    def test_listes_bornees_avec_total(self):
        sec = self.s["infections_secondaires"]
        self.assertEqual(sec["nombre_sur_la_fenetre"], 16)
        passes = [e for e in sec["evenements"] if not e.get("previsionnel")]
        futurs = [e for e in sec["evenements"] if e.get("previsionnel")]
        self.assertEqual((len(passes), len(futurs)), (5, 6))
        self.assertEqual(passes[-1]["date"], "2026-09-29")                          # les plus récentes constatées
        self.assertTrue(all("generation" in e for e in sec["evenements"]))

    def test_nuits_de_fructification_recentes(self):
        self.assertEqual(self.s["nuits_fructification_recentes"], ["2026-09-26", "2026-09-30"])   # 10/09 hors fenêtre

    def test_potentiel_journalier_a_sept_jours(self):
        j = self.s["potentiel_journalier"]
        self.assertEqual([x["date"] for x in j], [f"2026-10-{d:02d}" for d in range(5, 13)])
        self.assertEqual(j[0], {"date": "2026-10-05", "tmoy": 12.0, "infection_primaire_dh": 5.0, "infection_secondaire_dh": 2.5})

    def test_alerte_si_meteo_perimee(self):
        s = ep.synthese_mildiou(faux_resultat(), now=NOW, meta={"profil": "p", "meteo_perimee": True, "meteo_age_s": 7300})
        self.assertIn("depuis 2 h", s["alerte_meteo"])
        self.assertNotIn("alerte_meteo", self.s)

    def test_alerte_si_heures_manquantes(self):
        s = ep.synthese_mildiou(faux_resultat(), now=NOW, meta={"profil": "p", "meteo_trous": 5})
        self.assertIn("5 heure(s) manquante(s)", s["alerte_meteo"])
        s = ep.synthese_mildiou(faux_resultat(), now=NOW, meta={"profil": "p", "meteo_perimee": True, "meteo_age_s": 3600,
                                                                 "meteo_trous": 2})
        self.assertIn(" ; ", s["alerte_meteo"])                                    # les deux alertes se cumulent


class TestMoteurReel(Base):
    """Avec le vrai moteur (dépôt epidemio) sur une saison synthétique."""

    def test_calcul_et_synthese(self):
        try:
            res, meta = ep.calculer(49.25, 3.96, now=NOW, get=self.faux)
        except ep.MoteurIndisponible as e:
            self.skipTest(str(e))
        self.assertEqual(meta["profil"], "calage_2026")
        self.assertFalse(meta["meteo_perimee"])
        self.assertEqual(meta["unites_open_meteo"], 20.43)
        self.assertGreater(len(res["cycles"]), 0)
        s = ep.synthese_mildiou(res, now=NOW, meta=meta)
        json.dumps(s)
        self.assertIn("infections_primaires", s)
        self.assertEqual(s["maturation"]["statut"], "maturité acquise")

    def test_repli_si_le_moteur_est_indisponible(self):
        ancien = ep.charger_moteur
        try:
            def panne():
                raise ep.MoteurIndisponible("absent")
            ep.charger_moteur = panne
            self.assertIsNone(ep.synthese_mildiou_pour_prompt(49.25, 3.96))
        finally:
            ep.charger_moteur = ancien


class TestPrompt(unittest.TestCase):
    ANCIENNE = {"contaminations_mildiou": [{"date": "x"}], "texte_mildiou": "ANCIEN", "nb_contaminations": 1,
                "risques_oidium": [], "texte_oidium": "OIDIUM", "fenetres_traitement": ["d"], "nb_fenetres": 1}

    def test_sans_moteur_ancien_contenu_inchange(self):
        b = ep.bloc_epidemio_prompt(self.ANCIENNE, None)
        self.assertTrue(b.startswith("SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n"))
        donnees = b.split("\n", 1)[1].split("\n\nCONSIGNE DE RÉDACTION")[0]
        self.assertEqual(json.loads(donnees), self.ANCIENNE)                          # les données restent celles de l'ancien modèle
        self.assertIn("CONSIGNE DE RÉDACTION", b)                                      # mais la forme demandée est la nouvelle

    def test_avec_moteur_le_mildiou_vient_du_moteur_et_l_oidium_de_l_ancien(self):
        b = ep.bloc_epidemio_prompt(self.ANCIENNE, ep.synthese_mildiou(faux_resultat(), now=NOW))
        self.assertIn("MILDIOU (nouveau moteur horaire)", b)
        self.assertIn("Règles de rédaction pour le mildiou", b)
        self.assertIn("ne parle jamais de contamination avérée", b)
        self.assertNotIn("ANCIEN", b)
        self.assertNotIn("contaminations_mildiou", b)
        self.assertIn("OIDIUM", b)
        self.assertIn("fenetres_traitement", b)


class TestRoute(Base):
    def setUp(self):
        super().setUp()
        try:
            from flask import Flask
        except ImportError:
            self.skipTest("Flask absent")
        app = Flask(__name__)
        app.register_blueprint(ep.bp_epidemio)
        self.client = app.test_client()
        self.calculer_orig = ep.calculer
        ep.calculer = lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "calage_2026", "meteo_perimee": False,
                                                                              "meteo_age_s": 0})
        self.synth_ancienne = ep.synthese_mildiou
        ep.synthese_mildiou = lambda res, now=None, **k: self.synth_ancienne(res, now=NOW, **k)

    def tearDown(self):
        ep.calculer, ep.synthese_mildiou = self.calculer_orig, self.synth_ancienne
        super().tearDown()

    def test_reponse_normale(self):
        r = self.client.get("/api/epidemio-moteur?lat=49.25&lon=3.96")
        self.assertEqual(r.status_code, 200)
        self.assertIn("infections_primaires", r.get_json())
        self.assertNotIn("moteur_complet", r.get_json())

    def test_sortie_complete_sur_demande(self):
        r = self.client.get("/api/epidemio-moteur?complet=1")
        self.assertEqual(set(r.get_json()["moteur_complet"]), {"maturation", "cycles", "secondaires", "jours", "avertissements"})

    def test_moteur_indisponible_donne_503(self):
        def panne(*a, **k):
            raise ep.MoteurIndisponible("absent")
        ep.calculer = panne
        r = self.client.get("/api/epidemio-moteur")
        self.assertEqual((r.status_code, r.get_json()["error"]), (503, "absent"))

    def test_parametre_invalide_donne_400(self):
        def invalide(lat, lon, **k):
            return float(lat), None
        ep.calculer = invalide
        self.assertEqual(self.client.get("/api/epidemio-moteur?lat=abc").status_code, 400)

    def test_route_etat_de_l_historique_et_des_appels(self):
        h = ep.historique()
        h.importer_lignes(49.25, 3.96, self.lignes_courtes(), now=NOW)
        r = self.client.get("/api/epidemio-moteur/etat?lat=49.25&lon=3.96")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["historique"]["heures"], 48)
        self.assertEqual(d["usage"]["unites"], 0.0)
        self.assertTrue(d["archive_autorisee"])
        self.assertEqual(d["limites_offre_gratuite"]["par_jour"], 10000)
        self.assertEqual(self.client.get("/api/epidemio-moteur/etat?lat=abc").status_code, 400)

    def lignes_courtes(self):
        return [r for r in saison(48)]

    def test_autre_panne_donne_502(self):
        def panne(*a, **k):
            raise RuntimeError("Open-Meteo")
        ep.calculer = panne
        self.assertEqual(self.client.get("/api/epidemio-moteur").status_code, 502)


class TestTendance(unittest.TestCase):
    def evts(self, recente=0.0, prevue=0.0):
        e = []
        if recente:
            e.append({"date": "2026-10-01", "force_dh": recente})                       # dans les 7 derniers jours
        if prevue:
            e.append({"date": "2026-10-07", "force_dh": prevue})                        # dans les 7 prochains
        return e

    def libelle(self, recente, prevue):
        return ep._tendance(self.evts(recente, prevue), NOW)["libelle"]

    def test_les_cinq_libelles(self):
        self.assertEqual(self.libelle(0, 0), "PRESSION FAIBLE")
        self.assertEqual(self.libelle(30, 40), "PRESSION FAIBLE")                        # les deux sous 50 °C·h
        self.assertEqual(self.libelle(100, 150), "PRESSION EN HAUSSE")
        self.assertEqual(self.libelle(100, 100), "PRESSION STABLE")
        self.assertEqual(self.libelle(100, 60), "PRESSION EN BAISSE")
        self.assertEqual(self.libelle(0, 80), "PRESSION EN HAUSSE")                      # rien avant, quelque chose après
        self.assertEqual(self.libelle(200, 0), "PRESSION EN BAISSE")

    def test_bornes_des_rapports(self):
        self.assertEqual(self.libelle(100, 125), "PRESSION EN HAUSSE")                  # exactement x1,25
        self.assertEqual(self.libelle(100, 124), "PRESSION STABLE")
        self.assertEqual(self.libelle(100, 75), "PRESSION EN BAISSE")                   # exactement x0,75
        self.assertEqual(self.libelle(100, 76), "PRESSION STABLE")

    def test_fenetres_de_comptage(self):
        e = [{"date": "2026-09-27", "force_dh": 500}, {"date": "2026-09-28", "force_dh": 10},       # J-8 exclu, J-7 inclus
             {"date": "2026-10-05", "force_dh": 20}, {"date": "2026-10-12", "force_dh": 30},        # J et J+7 inclus
             {"date": "2026-10-13", "force_dh": 700}]                                                 # J+8 exclu
        r = ep._tendance(e, NOW)
        self.assertEqual((r["charge_recente_dh"], r["charge_prevue_dh"]), (10.0, 50.0))

    def test_dans_la_synthese_le_titre_reprend_le_libelle(self):
        s = ep.synthese_mildiou(faux_resultat(), now=NOW)
        self.assertEqual(s["tendance"]["libelle"], "PRESSION EN HAUSSE")
        self.assertEqual((s["tendance"]["charge_recente_dh"], s["tendance"]["charge_prevue_dh"]), (258.0, 1410.0))
        self.assertEqual(s["titre"], "RISQUE MILDIOU — PRESSION EN HAUSSE")
        self.assertIn("regle", s["tendance"])


class TestTitreOidium(unittest.TestCase):
    def jours(self, *niv_scores):
        return {"risques_oidium": [{"risque": n, "score": s} for n, s in niv_scores]}

    def test_niveaux(self):
        self.assertEqual(ep.titre_oidium(self.jours(*[("Nul", 0)] * 7)), "RISQUE OÏDIUM — PRESSION NULLE")
        self.assertEqual(ep.titre_oidium(self.jours(*[("Faible", 20)] * 7)), "RISQUE OÏDIUM — PRESSION FAIBLE")
        self.assertEqual(ep.titre_oidium(self.jours(*[("Modéré", 35)] * 7)), "RISQUE OÏDIUM — PRESSION MODÉRÉE")
        self.assertEqual(ep.titre_oidium(self.jours(("Modéré", 35), *[("Élevé", 55)] * 6)), "RISQUE OÏDIUM — PRESSION ÉLEVÉE")

    def test_tendance_sur_les_trois_premiers_et_trois_derniers_jours(self):
        montee = self.jours(("Faible", 10), ("Faible", 10), ("Faible", 10), ("Faible", 20), ("Modéré", 40), ("Modéré", 40), ("Modéré", 40))
        self.assertEqual(ep.titre_oidium(montee), "RISQUE OÏDIUM — PRESSION MODÉRÉE ET EN HAUSSE")
        descente = self.jours(("Modéré", 40), ("Modéré", 40), ("Modéré", 40), ("Faible", 20), ("Faible", 10), ("Faible", 10), ("Faible", 10))
        self.assertEqual(ep.titre_oidium(descente), "RISQUE OÏDIUM — PRESSION MODÉRÉE ET EN BAISSE")
        stable = self.jours(*[("Modéré", 35)] * 3, *[("Modéré", 38)] * 4)
        self.assertEqual(ep.titre_oidium(stable), "RISQUE OÏDIUM — PRESSION MODÉRÉE")      # écart de 3 points : pas de tendance

    def test_pas_de_tendance_sur_une_serie_trop_courte_ou_sans_risque(self):
        self.assertEqual(ep.titre_oidium(self.jours(("Faible", 5), ("Faible", 50), ("Faible", 60))), "RISQUE OÏDIUM — PRESSION FAIBLE")
        self.assertEqual(ep.titre_oidium(self.jours(*[("Nul", 0)] * 3, *[("Nul", 40)] * 4)), "RISQUE OÏDIUM — PRESSION NULLE")

    def test_sans_donnees(self):
        self.assertIsNone(ep.titre_oidium(None))
        self.assertIsNone(ep.titre_oidium({"risques_oidium": []}))


class TestScoresJauge(unittest.TestCase):
    def test_score_mildiou_normalise_sur_400(self):
        self.assertEqual(ep.score_mildiou({"tendance": {"charge_prevue_dh": 274.0}}), 69)   # 274/400 = 68,5 -> 69
        self.assertEqual(ep.score_mildiou({"tendance": {"charge_prevue_dh": 0.0}}), 0)
        self.assertEqual(ep.score_mildiou({"tendance": {"charge_prevue_dh": 437.0}}), 100)  # plafonné

    def test_score_mildiou_sans_moteur(self):
        self.assertIsNone(ep.score_mildiou(None))
        self.assertIsNone(ep.score_mildiou({}))



def _res_oidium(colonies, debut="2026-10-05"):
    d0 = date.fromisoformat(debut)
    return {"jours": [{"date": (d0 + timedelta(days=k)).isoformat(), "nouvelles_colonies": v} for k, v in enumerate(colonies)]}


class TestScoreOidiumMoteur(unittest.TestCase):
    def test_vent_et_rayonnement_transmis_au_moteur(self):
        r = ep.lignes_vers_rows([{"time": "2026-10-05T12:00", "temperature_2m": 15, "relative_humidity_2m": 70, "dew_point_2m": 9,
                                  "precipitation": 0, "wind_speed_10m": 3.2, "shortwave_radiation": 410}])[0]
        self.assertEqual((r["vent"], r["rayonnement"]), (3.2, 410.0))

    def test_echelle_logarithmique(self):
        self.assertEqual(ep.score_oidium(_res_oidium([0.001] * 7), NOW), 0)              # <= 0,01 : 0
        self.assertEqual(ep.score_oidium(_res_oidium([10.0] * 7), NOW), 100)             # 70 : 100
        self.assertEqual(ep.score_oidium(_res_oidium([0.5, 0.5, 0, 0, 0, 0, 0]), NOW), 52)  # 1 colonie : 100 x 2 / 3,845
        self.assertEqual(ep.score_oidium(_res_oidium([9, 1, 2, 3, 4, 5, 6, 0, 99], debut="2026-10-04"), NOW),  # hier et J+7 exclus
                         ep.score_oidium(_res_oidium([1, 2, 3, 4, 5, 6, 0]), NOW))

    def test_sans_donnees(self):
        self.assertIsNone(ep.score_oidium(None, NOW))
        self.assertIsNone(ep.score_oidium({"jours": []}, NOW))

    def test_tendance(self):
        self.assertEqual(ep.tendance_oidium(_res_oidium([1, 1, 1, 1, 2, 2, 2]), NOW), "en hausse")
        self.assertEqual(ep.tendance_oidium(_res_oidium([3, 3, 3, 2, 1, 1, 1]), NOW), "en baisse")
        self.assertIsNone(ep.tendance_oidium(_res_oidium([1] * 7), NOW))
        self.assertIsNone(ep.tendance_oidium(_res_oidium([0] * 7), NOW))


class TestIndiceChasmotheces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = os.path.join(self.tmp, "t.db")
        self.appels = []

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._env

    def archive(self, temp_aut=20.0, pluie_h=334.0 / 151 / 24):
        def f(lat, lon, d0, d1):
            self.appels.append((d0, d1))
            out, t = [], datetime(d0.year, d0.month, d0.day)
            while t.date() <= d1:
                hiver = t.month in (11, 12, 1, 2, 3)
                out.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "temperature_2m": 5.0 if hiver else (temp_aut if t.hour > 2 else 12.0),
                            "relative_humidity_2m": 80, "dew_point_2m": 3, "precipitation": pluie_h if hiver else 0.0})
                t += timedelta(hours=1)
            return out
        return f

    def test_formule_et_cache(self):
        maintenant = datetime(2027, 5, 1, tzinfo=UTC)
        i1 = ep.indice_chasmotheces(49.25, 3.96, 2027, now=maintenant, archive=self.archive())
        self.assertGreater(i1, 0)
        self.assertEqual(self.appels, [(date(2026, 8, 15), date(2027, 3, 31))])        # un seul appel, hiver complet
        i2 = ep.indice_chasmotheces(49.25, 3.96, 2027, now=maintenant, archive=self.archive())
        self.assertEqual((i2, len(self.appels)), (i1, 1))                                # définitif : relu en base, aucun appel

    def test_hiver_pluvieux_reduit_l_indice(self):
        m = datetime(2027, 5, 1, tzinfo=UTC)
        sec = ep.indice_chasmotheces(49.25, 3.96, 2027, now=m, archive=self.archive(pluie_h=0.05))
        humide = ep.indice_chasmotheces(48.00, 4.00, 2027, now=m, archive=self.archive(pluie_h=0.2))
        self.assertAlmostEqual(sec / humide, 4.0, delta=0.05)

    def test_automne_froid_reduit_l_indice(self):
        m = datetime(2027, 5, 1, tzinfo=UTC)
        chaud = ep.indice_chasmotheces(49.25, 3.96, 2027, now=m, archive=self.archive(temp_aut=20.0))
        froid = ep.indice_chasmotheces(48.00, 4.00, 2027, now=m, archive=self.archive(temp_aut=11.0))
        self.assertLess(froid, chaud / 3)

    def test_hiver_en_cours_provisoire_puis_recalcule(self):
        jan = datetime(2027, 1, 20, tzinfo=UTC)
        ep.indice_chasmotheces(49.25, 3.96, 2027, now=jan, archive=self.archive())
        self.assertEqual(self.appels[-1][1], date(2027, 1, 13))                          # archive jusqu'à aujourd'hui - 7 j
        ep.indice_chasmotheces(49.25, 3.96, 2027, now=jan + timedelta(days=2), archive=self.archive())
        self.assertEqual(len(self.appels), 1)                                            # provisoire récent : relu
        ep.indice_chasmotheces(49.25, 3.96, 2027, now=jan + timedelta(days=8), archive=self.archive())
        self.assertEqual(len(self.appels), 2)                                            # provisoire > 7 j : recalculé

    def test_archive_indisponible_sans_valeur_par_defaut(self):
        def panne(*a):
            raise RuntimeError("réseau")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(ep.indice_chasmotheces(49.25, 3.96, 2027, now=datetime(2027, 5, 1, tzinfo=UTC), archive=panne))


class TestJaugesClients(unittest.TestCase):
    def setUp(self):
        self._orig = (ep.serie_horaire, ep.charger_moteur, ep.charger_oidium, ep.synthese_mildiou, ep.resoudre_position,
                      ep.indice_chasmotheces)
        self.indice = 80.0
        ep.indice_chasmotheces = lambda lat, lon, annee, now=None, archive=None: self.indice
        self.appels = []

        def serie(lat, lon, now=None, get=None):
            self.appels.append((lat, lon))
            return [], {}
        ep.serie_horaire = serie

        class MP:
            calculer_saison = staticmethod(lambda rows, lat, lon, params=None, now=None: {})
            charger_profil = staticmethod(lambda nom: {})
        ep.charger_moteur = lambda: (MP, None)
        self.oidium_ok = True

        test = self
        class OI:
            @staticmethod
            def calculer_saison(rows, params=None, now=None):
                if not test.oidium_ok:
                    raise RuntimeError("panne")
                test.params_recus = params
                return _res_oidium([0.1, 0.1, 0.1, 0.2, 0.3, 0.3, 0.3])
        ep.charger_oidium = lambda: OI
        ep.synthese_mildiou = lambda res, now=None, meta=None: {"tendance": {"libelle": "PRESSION EN BAISSE", "charge_prevue_dh": 260.0}}
        pos = {"c1": {"lat": 49.13, "lon": 4.16, "source": "client", "commune": "Verzenay"},
               "c2": {"lat": 49.131, "lon": 4.161, "source": "geocodage", "commune": "Verzenay"}}
        ep.resoudre_position = lambda cl, get_json=None, now=None: pos.get(cl["id"])

    def tearDown(self):
        (ep.serie_horaire, ep.charger_moteur, ep.charger_oidium, ep.synthese_mildiou, ep.resoudre_position,
         ep.indice_chasmotheces) = self._orig

    def test_scores_locaux_et_calcul_partage_par_position(self):
        lignes = ep.jauges_clients([{"id": "c1", "commune": "Verzenay"}, {"id": "c2", "commune": "Verzenay"}], now=NOW)
        self.assertEqual(len(self.appels), 1)
        l = lignes[0]
        self.assertEqual((l["score_mildiou"], l["tendance_mildiou"]), (65, "en baisse"))
        self.assertEqual((l["score_oidium"], l["tendance_oidium"]), (56, "en hausse"))
        self.assertEqual(self.params_recus, {"primaire": {"indice_chasmotheces": 80.0}})
        self.assertEqual(l["indice_chasmotheces"], 80.0)
        self.assertEqual(lignes[1]["source_position"], "geocodage")

    def test_client_non_localise_sans_score(self):
        l = ep.jauges_clients([{"id": "c9", "commune": "Inconnue"}], now=NOW)[0]
        self.assertIsNone(l["score_mildiou"]); self.assertIsNone(l["score_oidium"])
        self.assertEqual(self.appels, [])

    def test_sans_indice_pas_de_score_oidium(self):
        self.indice = None
        with contextlib.redirect_stderr(io.StringIO()):
            l = ep.jauges_clients([{"id": "c1", "commune": "Verzenay"}], now=NOW)[0]
        self.assertEqual(l["score_mildiou"], 65)
        self.assertIsNone(l["score_oidium"])

    def test_oidium_en_echec_n_empeche_pas_le_mildiou(self):
        self.oidium_ok = False
        with contextlib.redirect_stderr(io.StringIO()):
            l = ep.jauges_clients([{"id": "c1", "commune": "Verzenay"}], now=NOW)[0]
        self.assertEqual(l["score_mildiou"], 65)
        self.assertIsNone(l["score_oidium"])


class TestSyntheseBotrytis(unittest.TestCase):
    def test_synthese_sur_serie_reelle_du_moteur(self):
        """Série horaire synthétique d'une saison : la synthèse sort un classement, un stade et les jours des 7 prochains jours."""
        import math as m
        t0, maintenant = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 7, 20, 12, tzinfo=UTC)
        lignes = []
        for h in range(int((maintenant - t0).total_seconds() // 3600) + 24 * 8):
            t = t0 + timedelta(hours=h); doy = t.timetuple().tm_yday
            T = 11 + 9 * m.sin((doy - 110) / 365 * 2 * m.pi) + 5 * m.sin((t.hour - 9) / 24 * 2 * m.pi)
            lignes.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "temperature_2m": round(T, 1), "relative_humidity_2m": 96 if t.hour < 8 else 70,
                           "dew_point_2m": round(T - 3, 1), "precipitation": 0.5 if doy % 5 == 0 and t.hour < 3 else 0.0})
        orig = ep.serie_horaire
        ep.serie_horaire = lambda lat, lon, now=None, get=None: (lignes, {"meteo_perimee": False})
        try:
            s = ep.synthese_botrytis(49.25, 3.96, now=maintenant)
        finally:
            ep.serie_horaire = orig
        self.assertIn(s["classe_saison"], ("faible", "intermediaire", "severe"))
        self.assertIsNotNone(s["stade_bbch"])
        self.assertGreater(s["sev1_floraison"], 0)
        for j in s["jours_a_risque_7j"]:
            self.assertTrue("2026-07-20" <= j["date"] <= "2026-07-26")
            self.assertGreaterEqual(j["risque"], ep.SEUIL_JOUR_BOTRYTIS)


class TestConsigne(unittest.TestCase):
    def test_forme_et_titres_imposes(self):
        c = ep.consigne_redaction({"mildiou": "RISQUE MILDIOU — PRESSION EN HAUSSE", "oidium": "RISQUE OÏDIUM — PRESSION FAIBLE"})
        self.assertIn("« RISQUE MILDIOU — PRESSION EN HAUSSE »", c)
        self.assertIn("« RISQUE OÏDIUM — PRESSION FAIBLE »", c)
        self.assertIn("3 ou 4 paragraphes", c)
        self.assertIn("100 à 150 mots", c)
        self.assertIn("sans style télégraphique", c)
        self.assertIn("N'écris jamais « modèle »", c)
        self.assertIn("s'écrivent \\n", c)

    def test_sans_titre_l_ia_le_deduit(self):
        c = ep.consigne_redaction({})
        self.assertIn("que tu déduis des données", c)
        self.assertNotIn("RISQUE MILDIOU — PRESSION", c)

    def test_le_bloc_avec_moteur_impose_les_deux_titres_calcules(self):
        ancienne = {"risques_oidium": [{"risque": "Modéré", "score": 35}] * 7, "texte_oidium": "x"}
        b = ep.bloc_epidemio_prompt(ancienne, ep.synthese_mildiou(faux_resultat(), now=NOW))
        self.assertIn("« RISQUE MILDIOU — PRESSION EN HAUSSE »", b)
        self.assertIn("« RISQUE OÏDIUM — PRESSION MODÉRÉE »", b)
        self.assertTrue(b.rstrip().endswith("s'écrivent \\n."))                          # la consigne est la dernière chose lue


class TestGeocodage(Base):
    def geo(self, resultats=None, panne=False, seulement_avec_tiret=False):
        appels = []

        def get_json(url):
            appels.append(url)
            if panne:
                raise OSError("réseau")
            nom = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0]
            if seulement_avec_tiret and "-" not in nom:
                return {}
            return {"results": resultats or []}
        get_json.appels = appels
        return get_json

    AY_MOSELLE = {"name": "Ay", "latitude": 49.1, "longitude": 6.3, "admin1": "Grand Est", "admin2": "Moselle"}
    AY_MARNE = {"name": "Aÿ-Champagne", "latitude": 49.05, "longitude": 4.0, "admin1": "Grand Est", "admin2": "Marne"}

    def test_choisit_la_marne_et_ecarte_les_homonymes_hors_zone(self):
        g = self.geo([self.AY_MOSELLE, self.AY_MARNE])
        self.assertEqual(ep.geocoder_commune("AY", g, NOW), (49.05, 4.0))
        self.assertIn("countryCode=FR", g.appels[0])                                    # recherche limitée à la France

    def test_resultat_hors_zone_champagne_refuse(self):
        self.assertIsNone(ep.geocoder_commune("AY", self.geo([self.AY_MOSELLE]), NOW))

    def test_a_defaut_de_la_marne_le_grand_est_dans_la_zone(self):
        aube = {"name": "Bar-sur-Seine", "latitude": 48.11, "longitude": 4.37, "admin1": "Grand Est", "admin2": "Aube"}
        self.assertEqual(ep.geocoder_commune("Bar sur Seine", self.geo([aube]), NOW), (48.11, 4.37))

    def test_reussite_memorisee_pour_toujours(self):
        ep.geocoder_commune("AY", self.geo([self.AY_MARNE]), NOW)
        panne = self.geo(panne=True)
        self.assertEqual(ep.geocoder_commune("ay", panne, NOW + timedelta(days=400)), (49.05, 4.0))
        self.assertEqual(panne.appels, [])                                              # aucun appel au géocodeur

    def test_echec_memorise_sept_jours(self):
        ep.geocoder_commune("INCONNUE", self.geo([]), NOW)
        autre = self.geo([self.AY_MARNE])
        self.assertIsNone(ep.geocoder_commune("INCONNUE", autre, NOW + timedelta(days=6)))
        self.assertEqual(autre.appels, [])
        self.assertEqual(ep.geocoder_commune("INCONNUE", autre, NOW + timedelta(days=8)), (49.05, 4.0))

    def test_une_panne_reseau_n_est_pas_memorisee(self):
        self.assertIsNone(ep.geocoder_commune("AY", self.geo(panne=True), NOW))
        self.assertEqual(ep.geocoder_commune("AY", self.geo([self.AY_MARNE]), NOW), (49.05, 4.0))

    def test_essaie_le_nom_avec_tirets(self):
        g = self.geo([self.AY_MARNE], seulement_avec_tiret=True)
        self.assertEqual(ep.geocoder_commune("SAINT MARTIN D'ABLOIS", g, NOW), (49.05, 4.0))
        self.assertEqual(len(g.appels), 2)

    def test_cle_commune(self):
        self.assertEqual(ep.cle_commune("Saint-Martin d'Ablois"), ep.cle_commune("SAINT MARTIN D'ABLOIS"))
        self.assertEqual(ep.cle_commune(None), "")


class TestPosition(Base):
    MARNE = {"name": "Reims", "latitude": 49.26, "longitude": 4.03, "admin1": "Grand Est", "admin2": "Marne"}

    def test_coordonnees_du_client_prioritaires(self):
        def jamais(url):
            raise AssertionError("le géocodeur ne doit pas être appelé")
        p = ep.resoudre_position({"commune": "BRIMONT", "latitude": 49.336, "longitude": 4.023}, jamais, NOW)
        self.assertEqual((p["lat"], p["lon"], p["source"]), (49.336, 4.023, "client"))

    def test_sans_coordonnees_geocodage_de_la_commune(self):
        g = lambda url: {"results": [self.MARNE]}                                       # noqa: E731
        for vide in ({}, {"latitude": None, "longitude": None}, {"latitude": 0, "longitude": 0}, {"latitude": "", "longitude": ""}):
            ep._HISTORIQUES.clear()
            p = ep.resoudre_position({"commune": "REIMS", **vide}, g, NOW)
            self.assertEqual((p["source"], p["lat"]), ("geocodage", 49.26), vide)

    def test_coordonnees_aberrantes_ignorees(self):
        g = lambda url: {"results": [self.MARNE]}                                       # noqa: E731
        p = ep.resoudre_position({"commune": "REIMS", "latitude": 40.7, "longitude": -74.0}, g, NOW)
        self.assertEqual(p["source"], "geocodage")

    def test_rien_pour_situer_le_client(self):
        self.assertIsNone(ep.resoudre_position({"commune": "", "latitude": None}, lambda u: {}, NOW))
        self.assertIsNone(ep.resoudre_position({"commune": "INTROUVABLE"}, lambda u: {"results": []}, NOW))


class TestPhraseCommune(unittest.TestCase):
    def syn(self):
        return ep.synthese_mildiou(faux_resultat(), now=NOW)

    def test_liste_dates_fr(self):
        self.assertEqual(ep.liste_dates_fr([]), "")
        self.assertEqual(ep.liste_dates_fr(["2026-10-06"]), "le 06/10")
        self.assertEqual(ep.liste_dates_fr(["2026-10-10", "2026-10-06", "2026-10-09"]), "les 06, 09 et 10/10")
        self.assertEqual(ep.liste_dates_fr(["2026-09-30", "2026-10-02", "2026-10-05"]), "les 30/09, 02/10 et 05/10")
        self.assertEqual(ep.liste_dates_fr(["2026-10-06", "2026-10-06"]), "le 06/10")

    def test_infection_attendue_taches_et_tendance(self):
        t = ep.phrase_commune(self.syn(), NOW)
        self.assertIn("Les conditions favorables à une infection très forte sont attendues autour du 07/10.", t)
        self.assertIn("Des sorties de taches sont attendues les 05, 06, 07 et 08/10.", t)
        self.assertTrue(t.endswith("La pression est en hausse : la vigilance reste de mise."))

    def test_jamais_de_contamination_averee(self):
        t = ep.phrase_commune(self.syn(), NOW).lower()
        for mot in ("contamination avérée", "dégâts", "moteur", "modèle"):
            self.assertNotIn(mot, t)

    def test_rien_d_attendu(self):
        syn = {"infections_primaires": {"evenements": []}, "infections_secondaires": {"evenements": []},
               "sorties_taches_attendues": [], "tendance": {"libelle": "PRESSION FAIBLE"}}
        self.assertEqual(ep.phrase_commune(syn, NOW), "Aucune infection significative n'est attendue dans les 7 prochains jours. "
                                                      "La pression reste faible à ce stade.")

    def test_infection_passee_seulement(self):
        syn = {"infections_primaires": {"evenements": [{"date": "2026-10-03", "niveau": "modérée", "force_dh": 90.0}]},
               "infections_secondaires": {"evenements": []}, "sorties_taches_attendues": [{"date": "2026-10-11"}],
               "tendance": {"libelle": "PRESSION EN BAISSE"}}
        t = ep.phrase_commune(syn, NOW)
        self.assertEqual(t, "Les conditions favorables à une infection modérée ont été réunies le 03/10. "
                            "Des sorties de taches sont attendues le 11/10. La pression diminue progressivement.")

    def test_une_infection_de_plus_de_trois_jours_n_est_plus_citee(self):
        syn = {"infections_primaires": {"evenements": [{"date": "2026-09-30", "niveau": "forte", "force_dh": 150.0}]},
               "infections_secondaires": {"evenements": []}, "sorties_taches_attendues": [],
               "tendance": {"libelle": "PRESSION STABLE"}}
        self.assertTrue(ep.phrase_commune(syn, NOW).startswith("Aucune infection significative"))


class TestMessageMoteurIntrouvable(unittest.TestCase):
    def test_indique_comment_mettre_a_jour_quand_historique_meteo_manque(self):
        m = ep.message_moteur_introuvable("/home/ubuntu/epidemio", ModuleNotFoundError("No module named 'historique_meteo'"))
        self.assertIn("No module named 'historique_meteo'", m)
        self.assertIn("cd ~/epidemio && git pull", m)

    def test_autre_erreur_sans_conseil(self):
        m = ep.message_moteur_introuvable("/x", ModuleNotFoundError("No module named 'autre'"))
        self.assertNotIn("git pull", m)


class TestEpi(Base):
    def syn(self, evenements=None, tendance="PRESSION EN HAUSSE", sorties=()):
        evts = evenements if evenements is not None else []
        return {"infections_primaires": {"evenements": evts}, "infections_secondaires": {"evenements": []},
                "sorties_taches_attendues": list(sorties), "tendance": {"libelle": tendance}}

    def e(self, date, niveau, force):
        return {"date": date, "niveau": niveau, "force_dh": force}

    def test_sur_le_jeu_de_reference(self):
        syn = ep.synthese_mildiou(faux_resultat(), now=NOW)
        self.assertEqual(ep.epi_depuis_synthese(syn, NOW), "très fort et en hausse, avec une infection attendue autour du 07/10")

    def test_infection_attendue_avec_chaque_tendance(self):
        evt = [self.e("2026-10-07", "modérée", 90.0)]
        self.assertEqual(ep.epi_depuis_synthese(self.syn(evt, "PRESSION EN HAUSSE"), NOW), "modéré et en hausse, avec une infection attendue autour du 07/10")
        self.assertEqual(ep.epi_depuis_synthese(self.syn(evt, "PRESSION EN BAISSE"), NOW), "modéré et en baisse, avec une infection attendue autour du 07/10")
        self.assertEqual(ep.epi_depuis_synthese(self.syn([self.e("2026-10-07", "forte", 150.0)], "PRESSION STABLE"), NOW),
                         "fort et stable, avec une infection attendue autour du 07/10")
        self.assertEqual(ep.epi_depuis_synthese(self.syn([self.e("2026-10-07", "faible", 20.0)], "PRESSION FAIBLE"), NOW),
                         "faible, avec une infection attendue autour du 07/10")

    def test_la_plus_forte_des_infections_attendues_est_citee(self):
        evt = [self.e("2026-10-06", "modérée", 80.0), self.e("2026-10-09", "forte", 160.0), self.e("2026-10-08", "faible", 10.0)]
        self.assertIn("autour du 09/10", ep.epi_depuis_synthese(self.syn(evt), NOW))

    def test_infection_recente_seulement(self):
        evt = [self.e("2026-10-03", "modérée", 90.0)]
        self.assertEqual(ep.epi_depuis_synthese(self.syn(evt, "PRESSION EN BAISSE"), NOW), "modéré, infection récente le 03/10 et pression en baisse")
        self.assertEqual(ep.epi_depuis_synthese(self.syn(evt, "PRESSION FAIBLE"), NOW), "modéré, infection récente le 03/10")

    def test_une_infection_de_plus_de_sept_jours_n_est_plus_citee(self):
        self.assertTrue(ep.epi_depuis_synthese(self.syn([self.e("2026-09-27", "forte", 150.0)]), NOW).startswith("faible, aucune infection"))

    def test_rien_d_attendu(self):
        self.assertEqual(ep.epi_depuis_synthese(self.syn([], "PRESSION FAIBLE"), NOW), "faible, aucune infection significative attendue dans les 7 prochains jours")

    def test_jamais_de_point_final_ni_de_jargon_du_moteur(self):
        for evt, tend in (([], "PRESSION FAIBLE"), ([self.e("2026-10-07", "forte", 150.0)], "PRESSION STABLE"), ([self.e("2026-10-03", "modérée", 90.0)], "PRESSION EN BAISSE")):
            texte = ep.epi_depuis_synthese(self.syn(evt, tend), NOW)
            self.assertFalse(texte.endswith("."), texte)                       # le bulletin ajoute lui-même le point
            for mot in ("moteur", "modèle", "°C"):
                self.assertNotIn(mot, texte)

    def test_epi_si_absent(self):
        orig, orig_s = ep.calculer, ep.synthese_mildiou
        try:
            ep.calculer = lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "p"})
            ep.synthese_mildiou = lambda res, now=None, **k: orig_s(res, now=NOW, **k)
            base_ia = {"risque_mildiou": "a", "reco_mildiou": "b"}
            r = ep.epi_si_absent(base_ia, 49.25, 3.96, now=NOW)
            self.assertEqual(r, {**base_ia, "epi": "très fort et en hausse, avec une infection attendue autour du 07/10"})
            for epi_vide in ("", "   ", None):
                self.assertIn("epi", ep.epi_si_absent({**base_ia, "epi": epi_vide}, 49.25, 3.96, now=NOW))
            self.assertIsNone(ep.epi_si_absent({**base_ia, "epi": "déjà là"}, 49.25, 3.96, now=NOW))      # l'EPI de l'IA est respecté
            self.assertIsNone(ep.epi_si_absent("pas un dictionnaire", 49.25, 3.96, now=NOW))
        finally:
            ep.calculer, ep.synthese_mildiou = orig, orig_s

    def test_position_par_defaut_quand_elle_n_est_pas_fournie(self):
        vues = []
        orig, orig_s = ep.calculer, ep.synthese_mildiou
        try:
            ep.calculer = lambda lat, lon, now=None, get=None: (vues.append((lat, lon)) or (faux_resultat(), {"profil": "p"}))
            ep.synthese_mildiou = lambda res, now=None, **k: orig_s(res, now=NOW, **k)
            ep.epi_si_absent({"risque_mildiou": "a"}, None, "", now=NOW)
            ep.epi_si_absent({"risque_mildiou": "a"}, 49.1, 4.0, now=NOW)
        finally:
            ep.calculer, ep.synthese_mildiou = orig, orig_s
        self.assertEqual(vues, [(ep.COORDS_DEFAUT["lat"], ep.COORDS_DEFAUT["lon"]), (49.1, 4.0)])

    def test_epi_si_absent_sans_moteur_ne_change_rien(self):
        orig = ep.calculer

        def panne(*a, **k):
            raise ep.MoteurIndisponible("absent")
        try:
            ep.calculer = panne
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(ep.epi_si_absent({"risque_mildiou": "a"}, 49.25, 3.96, now=NOW))
        finally:
            ep.calculer = orig


class TestRisqueJour(unittest.TestCase):
    def test_libelles_et_seuils(self):
        attendu = [(0, "Nul"), (None, "Nul"), (0.1, "Faible"), (49.9, "Faible"), (50, "Modéré"), (99.9, "Modéré"),
                   (100, "Élevé"), (199.9, "Élevé"), (200, "Très élevé"), (900, "Très élevé")]
        for force, libelle in attendu:
            self.assertEqual(ep.risque_jour(force), libelle, force)

    def test_memes_mots_que_l_ancien_modele(self):
        """Le tableau colore selon ces mots : « lev » en rouge, « odér » en orange."""
        mots = {ep.risque_jour(f) for f in (0, 10, 60, 150, 300)}
        self.assertEqual(mots, {"Nul", "Faible", "Modéré", "Élevé", "Très élevé"})


class TestBlocCommune(Base):
    def setUp(self):
        super().setUp()
        self.orig, self.orig_s = ep.calculer, ep.synthese_mildiou
        ep.calculer = lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "p", "meteo_perimee": False})
        ep.synthese_mildiou = lambda res, now=None, **k: self.orig_s(res, now=NOW, **k)

    def tearDown(self):
        ep.calculer, ep.synthese_mildiou = self.orig, self.orig_s
        super().tearDown()

    def test_bloc_complet(self):
        b = ep.bloc_commune_pour_client({"commune": "BRIMONT", "latitude": 49.336, "longitude": 4.023}, now=NOW)
        self.assertEqual(b["titre"], "Situation sur votre commune (Brimont), d'après la météo : ")
        self.assertIn("autour du 07/10", b["texte"])
        self.assertEqual((b["tendance"], b["source_position"]), ("PRESSION EN HAUSSE", "client"))

    def test_risque_de_chaque_jour_pour_le_tableau(self):
        res = faux_resultat()
        for jour in res["jours"]:
            jour["force_infection_dh"], jour["force_secondaire_dh"] = {5: (0, 0), 6: (30, 10), 7: (60, 0), 8: (80, 40), 9: (150, 100)}.get(
                int(jour["date"][-2:]), (0, 0))
        ep.calculer = lambda lat, lon, now=None, get=None: (res, {"profil": "p", "meteo_perimee": False})
        b = ep.bloc_commune_pour_client({"commune": "BRIMONT", "latitude": 49.336, "longitude": 4.023}, now=NOW)
        rj = b["risque_jours"]
        self.assertEqual([rj[f"2026-10-{d:02d}"] for d in (5, 6, 7, 8, 9)], ["Nul", "Faible", "Modéré", "Élevé", "Très élevé"])
        self.assertEqual(sorted(rj), [f"2026-10-{d:02d}" for d in range(5, 13)])         # aujourd'hui et les 7 jours suivants

    def test_client_non_localisable_garde_son_bulletin_inchange(self):
        self.assertIsNone(ep.bloc_commune_pour_client({"commune": "", "latitude": None}, now=NOW, get_json=lambda u: {}))

    def test_ne_leve_jamais(self):
        def panne(*a, **k):
            raise ep.MoteurIndisponible("absent")
        ep.calculer = panne
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(ep.bloc_commune_pour_client({"commune": "X", "latitude": 49.3, "longitude": 4.0}, now=NOW))


class TestApercuCommunes(Base):
    def setUp(self):
        super().setUp()
        self.orig, self.orig_s = ep.calculer, ep.synthese_mildiou
        self.appels = []

        def faux_calculer(lat, lon, now=None, get=None):
            self.appels.append((lat, lon))
            return faux_resultat(), {"profil": "p", "meteo_perimee": False, "unites_open_meteo": 20.43}
        ep.calculer = faux_calculer
        ep.synthese_mildiou = lambda res, now=None, **k: self.orig_s(res, now=NOW, **k)
        self.clients = [{"id": "A", "commune": "TOUR SUR MARNE", "latitude": 49.049, "longitude": 4.117},
                        {"id": "B", "commune": "Tour-sur-Marne", "latitude": 49.049, "longitude": 4.117},
                        {"id": "C", "commune": "BRIMONT", "latitude": 49.336, "longitude": 4.023},
                        {"id": "D", "commune": "", "latitude": None, "longitude": None}]

    def tearDown(self):
        ep.calculer, ep.synthese_mildiou = self.orig, self.orig_s
        super().tearDown()

    def test_une_ligne_par_position_et_clients_regroupes(self):
        r = ep.apercu_communes(self.clients, now=NOW, get_json=lambda u: {})
        self.assertEqual((r["clients"], len(r["positions"])), (4, 2))
        tour = next(p for p in r["positions"] if p["position"] == "49.05_4.12")
        self.assertEqual((tour["clients"], tour["communes"]), (2, ["TOUR SUR MARNE"]))        # orthographes fusionnées
        self.assertEqual(len(self.appels), 2)                                                 # un calcul par position, pas par client
        self.assertEqual((tour["calcule"], tour["tendance"]), (True, "PRESSION EN HAUSSE"))
        self.assertEqual(tour["prochaine_infection"], {"date": "2026-10-07", "niveau": "très forte", "force_dh": 210.0})
        self.assertEqual(r["unites_open_meteo"], 40.86)

    def test_clients_sans_position_signales_avec_la_marche_a_suivre(self):
        r = ep.apercu_communes(self.clients, now=NOW, get_json=lambda u: {})
        self.assertEqual(r["non_resolus"][0]["id_client"], "D")
        self.assertIn("renseigne latitude et longitude", r["non_resolus"][0]["raison"])

    def test_budget_de_temps(self):
        r = ep.apercu_communes(self.clients, now=NOW, get_json=lambda u: {}, budget_s=-1)
        self.assertTrue(all(not p["calcule"] and "délai" in p["raison"] for p in r["positions"]))
        self.assertEqual(self.appels, [])

    def test_une_position_en_erreur_n_empeche_pas_les_autres(self):
        precedent = ep.calculer

        def un_peu_casse(lat, lon, now=None, get=None):
            if lat > 49.3:
                raise ep.MoteurIndisponible("pas de météo pour ce point")
            return precedent(lat, lon, now, get)
        ep.calculer = un_peu_casse
        r = ep.apercu_communes(self.clients, now=NOW, get_json=lambda u: {})
        etats = {p["communes"][0]: p["calcule"] for p in r["positions"]}
        self.assertEqual(etats, {"BRIMONT": False, "TOUR SUR MARNE": True})
        self.assertIn("pas de météo", next(p for p in r["positions"] if not p["calcule"])["raison"])

    def test_lire_clients_ne_lit_que_le_necessaire(self):
        db = os.path.join(self.donnees, "pilot.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE clients (id TEXT, exploitation TEXT, commune TEXT, email TEXT, telephone TEXT, latitude REAL, longitude REAL)")
        c.execute("INSERT INTO clients VALUES ('A','EARL Secrète','BRIMONT','a@b.fr','0600000000',49.3,4.0)")
        c.commit()
        c.close()
        r = ep.lire_clients(db)
        self.assertEqual(r, [{"id": "A", "commune": "BRIMONT", "latitude": 49.3, "longitude": 4.0}])   # ni nom, ni e-mail, ni téléphone


class TestDisjoncteur(Base):
    def test_apres_une_panne_on_n_insiste_pas_pendant_deux_minutes(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.faux)                                   # remplit l'historique
        compteur = []

        def panne(url):
            compteur.append(url)
            raise RuntimeError("Open-Meteo injoignable")
        _, i1 = ep.serie_horaire(49.25, 3.96, NOW + timedelta(hours=2), panne)
        _, i2 = ep.serie_horaire(49.25, 3.96, NOW + timedelta(hours=3), panne)
        self.assertEqual(len(compteur), 1)                                              # la 2e demande n'a pas rappelé l'API
        self.assertTrue(i1["meteo_perimee"] and i2["meteo_perimee"])
        self.assertIn("en pause", i2["erreur_meteo"])
        ep._PANNE_JUSQU = 0.0
        _, i3 = ep.serie_horaire(49.25, 3.96, NOW + timedelta(hours=4), self.faux)
        self.assertFalse(i3["meteo_perimee"])                                           # la reprise est automatique


class TestRoutesCommunes(Base):
    def setUp(self):
        super().setUp()
        try:
            from flask import Flask
        except ImportError:
            self.skipTest("Flask absent")
        app = Flask(__name__)
        app.register_blueprint(ep.bp_epidemio)
        self.client = app.test_client()
        self.orig = (ep.lire_clients, ep.apercu_communes)

    def tearDown(self):
        ep.lire_clients, ep.apercu_communes = self.orig
        super().tearDown()

    def test_route_communes(self):
        ep.lire_clients = lambda: [{"id": "A", "commune": "BRIMONT"}]
        vu = {}
        ep.apercu_communes = lambda clients, budget_s=None, **k: (vu.update(budget=budget_s, clients=clients) or {"positions": [], "non_resolus": [], "clients": 1})
        r = self.client.get("/api/epidemio-moteur/communes")
        self.assertEqual((r.status_code, r.get_json()["clients"]), (200, 1))
        self.assertEqual(vu["budget"], 80)                                              # le délai du serveur est protégé

    def test_route_communes_moteur_indisponible(self):
        def panne():
            raise ep.MoteurIndisponible("absent")
        ep.lire_clients = panne
        self.assertEqual(self.client.get("/api/epidemio-moteur/communes").status_code, 503)


class TestLigneDeCommande(Base):
    def test_communes(self):
        orig, orig_s, orig_l = ep.calculer, ep.synthese_mildiou, ep.lire_clients
        try:
            ep.calculer = lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "p", "unites_open_meteo": 1.07})
            ep.synthese_mildiou = lambda res, now=None, **k: orig_s(res, now=NOW, **k)
            ep.lire_clients = lambda db=None: [{"id": "A", "commune": "BRIMONT", "latitude": 49.336, "longitude": 4.023},
                                               {"id": "B", "commune": "", "latitude": None, "longitude": None}]
            s = io.StringIO()
            with contextlib.redirect_stdout(s):
                code = ep.main(["communes"])
        finally:
            ep.calculer, ep.synthese_mildiou, ep.lire_clients = orig, orig_s, orig_l
        sortie = s.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("2 clients, 1 positions, 1.07 unité(s)", sortie)
        self.assertIn("BRIMONT", sortie)
        self.assertIn("PRESSION EN HAUSSE", sortie)
        self.assertIn("SANS POSITION : client B", sortie)


def silencieux(fonction, *args):
    """Exécute fonction en masquant ses messages (le script de branchement parle beaucoup)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fonction(*args)


STUB_SERVEUR = """import json
from flask import Flask, jsonify, request
app = Flask(__name__)

@app.route('/api/autre')
def autre():
    return jsonify({"x": 1})

# from generer_bulletins_v4 import build_bulletin as _build_bulletin

def _build_bulletin(client, av, prescriptions, suivi, meteo):
    return ("BULLETIN", client)

def _calculer_synthese_epidemio(lat, lon, m, r):
    return [], {"texte_mildiou": "ANCIEN-MILDIOU", "texte_oidium": "OIDIUM-ANCIEN"}

@app.route('/api/generer-texte-bulletin', methods=['GET'])
def generer_texte_bulletin():
    lat, lon, maturite, receptive = 49.25, 3.96, True, True
    try:
        meteo_7j, synthese = _calculer_synthese_epidemio(lat, lon, maturite, receptive)

        prompt = f'''Voici la météo.

SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :
{json.dumps(synthese, ensure_ascii=False, indent=2)}

Rédige, à partir de CES données précises (pas de généralités), un texte court pour chacun des 5 champs suivants, dans le style suivant : phrases courtes, techniques, factuelles, sans emphase ni formules commerciales. Le risque explique.'''
        return prompt
    except Exception as e:
        return str(e)
"""

STUB_GENERATEUR = """BK, OR, GD, GR, BL, OBG, GBG, GM = "000000", "AA6600", "113322", "666666", "0000FF", "OBG", "GBG", "GM"


class WD_ALIGN_PARAGRAPH:
    LEFT, JUSTIFY = "left", "justify"


def multi_para(doc, runs):
    doc.append(("multi", runs))


def styled_para(doc, text, bold=False, color=BK, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY, sa=4, sb=2, italic=False):
    doc.append(("para", text, bold, color, align))


def section_heading(doc, num, text, color=GD):
    doc.append(("heading", num, text))


def alert_box(doc, title, text, bg=None, tc=None):
    doc.append(("alert", title, text))


def build_bulletin(client, av, prescriptions, suivi, meteo_days):
    doc = []
    risque = av.get("risque_mildiou") or ""
    risque_o = av.get("risque_oidium") or "information non disponible"
    reco = av.get("reco_mildiou") or ""
    reco_o = av.get("reco_oidium") or ""
    multi_para(doc, [
        {"t": "Analyse du risque : ", "b": True, "c": OR}, f"{risque} ",
    ])
    if reco:
        multi_para(doc, [{"t": "Recommandation Comité Champagne : ", "b": True, "c": GR, "i": True}, f"{reco}"])
    if suivi:
        pluie, temp, txt = suivi["pluie"], suivi["temp"], "faits. "
        if pluie >= 2 and temp >= 11:
            txt += "CONCLUSION-CRUE"
        elif pluie < 2:
            txt += "PAS-DE-PLUIE"
        else:
            txt += "TEMP-INSUFFISANTE"
        multi_para(doc, [{"t": "Situation parcellaire : ", "b": True, "c": GD}, txt])

    if client.get("parcelles_mildiou"):
        styled_para(doc, "Parcelles sensibles", size=9)
    multi_para(doc, [
        {"t": "Analyse régionale : ", "b": True}, f"{risque_o}. ",
    ])
    if reco_o:
        multi_para(doc, [{"t": "Recommandation Comité Champagne : ", "b": True, "c": GR, "i": True}, reco_o])
    section_heading(doc, "4", "PRÉVISIONS MÉTÉO — 7 JOURS", BL)
    if meteo_days:
        risk_days = [d for d in meteo_days if d["risk"] in ("Modéré","Élevé","Très élevé")]
        if risk_days:
            alert_box(doc, f"⚠️ {len(risk_days)} jour(s) à risque de contamination", ", ".join(d["date"] for d in risk_days), OBG, OR)
        else:
            alert_box(doc, "✅ Pas de risque mildiou sur 7 jours",
                "Aucun jour ne réunit pluie ≥ 2 mm + T° moy ≥ 11°C.", GBG, GM)
        for day in meteo_days[:7]:
            multi_para(doc, [f"LIGNE {day['date']} {day['risk']}"])
    return doc
"""

STUB_DASHBOARD = ("zone.innerHTML=`<strong>Mildiou</strong> — ${b.risque_mildiou||'—'}<br>${b.reco_mildiou||''}<br><br>\n"
                  "<strong>Oïdium</strong> — ${b.risque_oidium||'—'}<br>`;\n"
                  "h+=`<p>${av.risque_mildiou||'?'}</p>`;h+=`<p>${av.risque_oidium||'?'}</p>`;\n")
STUB_PORTAIL = ("h+=`<div class=\"alert\"><strong>Mildiou</strong><br>${b.risque_mildiou}</div>`;\n"
                "h+=`<div class=\"alert\"><strong>Oïdium</strong><br>${b.risque_oidium}</div>`;\n")


def executer(src):
    mod = types.ModuleType("stub")
    exec(compile(src, "stub", "exec"), mod.__dict__)
    return mod


class TestBrancherServeur(unittest.TestCase):
    def test_trois_etapes_compilent_et_sont_idempotentes(self):
        nouveau, etapes = br.modifier_serveur(STUB_SERVEUR)
        self.assertEqual(etapes, ["moteur", "style", "communes", "epi"])
        compile(nouveau, "stub", "exec")
        self.assertEqual(br.modifier_serveur(nouveau), (nouveau, []))
        self.assertIn("{bloc_epidemio}", nouveau)
        self.assertIn(br.S_STYLE_NOUVEAU, nouveau)
        self.assertNotIn(br.S_STYLE, nouveau)

    def test_mise_a_niveau_d_un_serveur_branche_avec_la_version_precedente(self):
        v2 = (STUB_SERVEUR.replace(br.S_ROUTE, br.BLOC_MOTEUR + br.S_ROUTE).replace(br.S_APPEL, br.S_APPEL + br.LIGNES_APPEL)
              .replace(br.S_PROMPT, "{bloc_epidemio}"))
        nouveau, etapes = br.modifier_serveur(v2)
        self.assertEqual(etapes, ["style", "communes", "epi"])
        self.assertEqual(nouveau, br.modifier_serveur(STUB_SERVEUR)[0])                # converge vers un branchement à neuf

    def test_point_d_ancrage_manquant_ou_ambigu_refuse(self):
        for ancre in (br.S_ROUTE, br.S_APPEL, br.S_PROMPT, br.S_STYLE):
            with self.assertRaises(ValueError):
                br.modifier_serveur(STUB_SERVEUR.replace(ancre, "# retiré\n"))
            with self.assertRaises(ValueError):
                br.modifier_serveur(STUB_SERVEUR + "\n" + ancre)
        with self.assertRaises(ValueError):
            br.modifier_serveur(STUB_SERVEUR.replace("build_bulletin as _build_bulletin", "autre"))

    def prompt_et_bulletin(self, calculer):
        mod = executer(br.modifier_serveur(STUB_SERVEUR)[0])
        ancien = ep.calculer
        try:
            ep.calculer = calculer
            return mod.generer_texte_bulletin(), mod._build_bulletin({"commune": "BRIMONT", "latitude": 49.3, "longitude": 4.0},
                                                                     {}, [], None, None)
        finally:
            ep.calculer = ancien

    def test_serveur_modifie_avec_moteur(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        prompt, (nom, client) = self.prompt_et_bulletin(lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "p"}))
        self.assertIn("MILDIOU (nouveau moteur horaire)", prompt)
        self.assertIn("CONSIGNE DE RÉDACTION (elle remplace", prompt)
        self.assertIn("en respectant la CONSIGNE DE RÉDACTION ci-dessus", prompt)
        self.assertNotIn("phrases courtes, techniques", prompt)                        # l'ancien style est parti
        self.assertNotIn("ANCIEN-MILDIOU", prompt)
        self.assertIn("OIDIUM-ANCIEN", prompt)
        self.assertIn("_epidemio_commune", client)                                     # le bulletin reçoit le bloc commune
        self.assertTrue(client["_epidemio_commune"]["titre"].startswith("Situation sur votre commune (Brimont)"))
        self.assertEqual(client["commune"], "BRIMONT")                                 # le reste du client est intact

    def test_serveur_modifie_sans_moteur_se_comporte_comme_avant(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")

        def panne(*a, **k):
            raise ep.MoteurIndisponible("absent")
        with contextlib.redirect_stderr(io.StringIO()):
            prompt, (nom, client) = self.prompt_et_bulletin(panne)
        self.assertIn("ANCIEN-MILDIOU", prompt)                                        # ancien contenu
        self.assertNotIn("nouveau moteur horaire", prompt)
        self.assertIn("CONSIGNE DE RÉDACTION", prompt)                                 # mais nouvelle forme demandée
        self.assertEqual(client, {"commune": "BRIMONT", "latitude": 49.3, "longitude": 4.0})   # client strictement inchangé

    def test_un_bloc_commune_defaillant_n_empeche_jamais_le_bulletin(self):
        mod = executer(br.modifier_serveur(STUB_SERVEUR)[0])

        def casse(client):
            raise RuntimeError("boum")
        mod._bloc_commune_pour_client = casse
        with contextlib.redirect_stdout(io.StringIO()):
            nom, client = mod._build_bulletin({"commune": "X"}, {}, [], None, None)
        self.assertEqual((nom, client), ("BULLETIN", {"commune": "X"}))


    def test_variante_a_quatre_champs_sans_epi(self):
        v = STUB_SERVEUR.replace("chacun des 5 champs", "chacun des 4 champs")
        nouveau, etapes = br.modifier_serveur(v)
        self.assertIn("un texte pour chacun des 4 champs suivants, en respectant la CONSIGNE DE RÉDACTION ci-dessus.", nouveau)
        self.assertNotIn("phrases courtes, techniques", nouveau)
        self.assertEqual(etapes, ["moteur", "style", "communes", "epi"])

    def test_style_reformule_toujours_reconnu(self):
        v = STUB_SERVEUR.replace("phrases courtes, techniques, factuelles, sans emphase ni formules commerciales.",
                                 "ton sobre, phrases courtes, sans jargon.")
        self.assertIn("en respectant la CONSIGNE DE RÉDACTION ci-dessus.", br.modifier_serveur(v)[0])

    def test_sans_les_mots_un_texte_court_seule_la_clause_de_style_est_remplacee(self):
        v = STUB_SERVEUR.replace("un texte court pour chacun", "un texte pour chacun")
        nouveau, _ = br.modifier_serveur(v)
        self.assertIn("un texte pour chacun des 5 champs suivants, en respectant la CONSIGNE DE RÉDACTION ci-dessus.", nouveau)

    def test_un_refus_montre_ce_que_contient_le_fichier(self):
        v = STUB_SERVEUR.replace(br.S_STYLE, "un texte libre pour chaque champ, comme tu veux.")
        with self.assertRaises(ValueError) as e:
            br.modifier_serveur(v)
        self.assertIn("consigne de style du prompt", str(e.exception))
        self.assertIn("Rédige, à partir de CES données", str(e.exception))                 # la ligne trouvée à la place est citée

    def test_deux_clauses_de_style_isolees_sont_ambigues(self):
        v = STUB_SERVEUR.replace("un texte court pour chacun des 5 champs suivants, ", "") + "\n# dans le style suivant : autre chose."
        with self.assertRaises(ValueError):
            br.modifier_serveur(v)

    def test_une_clause_isolee_en_plus_de_la_phrase_complete_n_empeche_rien(self):
        nouveau, _ = br.modifier_serveur(STUB_SERVEUR + "\n# dans le style suivant : autre chose.")
        self.assertIn("en respectant la CONSIGNE DE RÉDACTION ci-dessus.", nouveau)
        self.assertIn("# dans le style suivant : autre chose.", nouveau)                      # le commentaire n'est pas touché

    def serveur_epi(self, reponse_ia, statut=200):
        """Serveur factice branché dont la route de génération renvoie reponse_ia (JSON)."""
        from flask import jsonify
        mod = executer(br.modifier_serveur(STUB_SERVEUR)[0])
        mod.app.view_functions["generer_texte_bulletin"] = lambda: (jsonify(reponse_ia), statut)
        return mod.app.test_client()

    def avec_moteur(self, fonction):
        orig, orig_s = ep.calculer, ep.synthese_mildiou
        try:
            ep.calculer = lambda lat, lon, now=None, get=None: (faux_resultat(), {"profil": "p"})
            ep.synthese_mildiou = lambda res, now=None, **k: orig_s(res, now=NOW, **k)
            return fonction()
        finally:
            ep.calculer, ep.synthese_mildiou = orig, orig_s

    def test_le_serveur_complete_l_epi_absent_de_la_reponse(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        ia = {"risque_mildiou": "RISQUE MILDIOU — X\n\nPara.", "reco_mildiou": "b", "risque_oidium": "c", "reco_oidium": "d"}
        r = self.avec_moteur(lambda: self.serveur_epi(ia).get("/api/generer-texte-bulletin"))
        d = r.get_json()
        self.assertEqual(d["epi"], "très fort et en hausse, avec une infection attendue autour du 07/10")
        self.assertEqual({k: d[k] for k in ia}, ia)                                    # les autres champs sont intacts, retours à la ligne compris
        self.assertEqual(int(r.headers["Content-Length"]), len(r.data))               # la réponse reste bien formée
        self.assertEqual(r.mimetype, "application/json")

    def test_le_serveur_respecte_un_epi_deja_present(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        ia = {"epi": "potentiel modéré", "risque_mildiou": "a"}
        d = self.avec_moteur(lambda: self.serveur_epi(ia).get("/api/generer-texte-bulletin")).get_json()
        self.assertEqual(d, ia)

    def test_le_serveur_ne_touche_ni_aux_erreurs_ni_aux_autres_routes(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        erreur = self.avec_moteur(lambda: self.serveur_epi({"error": "clé manquante"}, 400).get("/api/generer-texte-bulletin"))
        self.assertEqual((erreur.status_code, erreur.get_json()), (400, {"error": "clé manquante"}))
        autre = self.avec_moteur(lambda: self.serveur_epi({"x": 1}).get("/api/autre"))
        self.assertEqual(autre.get_json(), {"x": 1})

    def test_le_serveur_sans_moteur_renvoie_la_reponse_telle_quelle(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        orig = ep.calculer

        def panne(*a, **k):
            raise ep.MoteurIndisponible("absent")
        try:
            ep.calculer = panne
            with contextlib.redirect_stderr(io.StringIO()):
                r = self.serveur_epi({"risque_mildiou": "a"}).get("/api/generer-texte-bulletin")
        finally:
            ep.calculer = orig
        self.assertEqual((r.status_code, r.get_json()), (200, {"risque_mildiou": "a"}))

    def test_etape_epi_ajoutee_a_un_serveur_deja_a_jour_sur_le_reste(self):
        complet = br.modifier_serveur(STUB_SERVEUR)[0]
        sans_epi = complet.replace(br.BLOC_EPI, "")
        nouveau, etapes = br.modifier_serveur(sans_epi)
        self.assertEqual((etapes, nouveau), (["epi"], complet))


class TestBrancherGenerateur(unittest.TestCase):
    def setUp(self):
        self.avant = executer(STUB_GENERATEUR)
        self.apres = executer(br.modifier_generateur(STUB_GENERATEUR)[0])

    def meteo(self):
        return [{"date": "2026-10-06", "risk": "Faible"}, {"date": "2026-10-07", "risk": "Élevé"}, {"date": "2026-10-08", "risk": "Modéré"}]

    def commune(self, **risque_jours):
        return {"_epidemio_commune": {"titre": "Situation : ", "texte": "Bloc.", "risque_jours": risque_jours}}

    def test_les_quatre_etapes_compilent_et_sont_idempotentes(self):
        nouveau, etapes = br.modifier_generateur(STUB_GENERATEUR)
        self.assertEqual(etapes, ["paragraphes", "commune", "libellé", "tableau"])
        self.assertEqual(br.modifier_generateur(nouveau), (nouveau, []))

    def test_mise_a_niveau_d_un_generateur_branche_avec_la_version_precedente(self):
        v3 = (STUB_GENERATEUR.replace(br.G_DEF, br.G_HELPER + br.G_DEF)
              .replace(br.G_MILDIOU, '    analyse_en_paragraphes(doc, "Analyse du risque : ", risque, OR)\n')
              .replace(br.G_OIDIUM, '    analyse_en_paragraphes(doc, "Analyse régionale : ", risque_o, None, ". ")\n')
              .replace(br.G_CONCLUSION, br.G_CONCLUSION_NOUVELLE).replace(br.G_PARCELLES, br.G_BLOC_COMMUNE + br.G_PARCELLES))
        nouveau, etapes = br.modifier_generateur(v3)
        self.assertEqual(etapes, ["libellé", "tableau"])
        self.assertEqual(nouveau, br.modifier_generateur(STUB_GENERATEUR)[0])           # converge vers un branchement à neuf

    def test_ancrage_manquant_ou_ambigu_refuse(self):
        for ancre in (br.G_DEF, br.G_MILDIOU, br.G_OIDIUM, br.G_CONCLUSION, br.G_PARCELLES, br.G_SECTION4, br.G_PAS_DE_RISQUE):
            with self.assertRaises(ValueError):
                br.modifier_generateur(STUB_GENERATEUR.replace(ancre, "# retiré\n"))
            with self.assertRaises(ValueError):
                br.modifier_generateur(STUB_GENERATEUR + "\n" + ancre)

    def test_libelle_attendu_deux_fois(self):
        with self.assertRaises(ValueError):
            br.modifier_generateur(STUB_GENERATEUR + "\n# " + br.G_LIBELLE)              # une 3e occurrence : version inattendue

    def test_texte_sans_retour_a_la_ligne_rendu_exactement_comme_avant(self):
        for av in ({"risque_mildiou": "Infection modérée le 07/10.", "risque_oidium": "Risque modéré"}, {}, {"risque_oidium": ""}):
            for suivi in (None, {"pluie": 5, "temp": 14}, {"pluie": 0, "temp": 14}, {"pluie": 5, "temp": 8}):
                for client in ({}, {"parcelles_mildiou": "Les Vignes"}):
                    for meteo in (None, self.meteo()):
                        self.assertEqual(self.avant.build_bulletin(client, av, [], suivi, meteo),
                                         self.apres.build_bulletin(client, av, [], suivi, meteo), (av, suivi, client))

    def test_libelle_recommandation_sans_comite(self):
        av = {"reco_mildiou": "Rester vigilant.", "reco_oidium": "Surveiller."}
        avant = self.avant.build_bulletin({}, av, [], None, None)
        apres = self.apres.build_bulletin({}, av, [], None, None)
        self.assertIn("Recommandation Comité Champagne : ", str(avant))
        self.assertNotIn("Comité", str(apres))
        self.assertEqual(str(apres).count("Recommandation : "), 2)                       # mildiou et oïdium
        self.assertIn("Rester vigilant.", str(apres))                                     # le texte lui-même est intact

    def test_texte_a_plusieurs_lignes_titre_puis_paragraphes(self):
        av = {"risque_mildiou": "RISQUE MILDIOU — PRESSION EN HAUSSE\n\nPremier paragraphe.\n\nSecond paragraphe.",
              "risque_oidium": "RISQUE OÏDIUM — PRESSION FAIBLE\nUn seul paragraphe."}
        doc = self.apres.build_bulletin({}, av, [], None, None)
        self.assertEqual(doc[0], ("para", "RISQUE MILDIOU — PRESSION EN HAUSSE", True, "AA6600", "left"))      # titre en gras, coloré
        self.assertEqual(doc[1][:2], ("para", "Premier paragraphe."))
        self.assertEqual(doc[2][:2], ("para", "Second paragraphe."))
        self.assertEqual(doc[3], ("para", "RISQUE OÏDIUM — PRESSION FAIBLE", True, "000000", "left"))
        self.assertEqual(doc[4][:2], ("para", "Un seul paragraphe."))                                           # pas de « . » ajouté

    def test_sans_titre_tout_est_paragraphe(self):
        doc = self.apres.build_bulletin({}, {"risque_mildiou": "Une phrase complète qui finit par un point.\nUne autre."}, [], None, None)
        self.assertEqual([d[1] for d in doc[:2]], ["Une phrase complète qui finit par un point.", "Une autre."])
        self.assertTrue(all(d[2] is False for d in doc[:2]))

    def test_bloc_commune_remplace_la_conclusion_fruste(self):
        client = {"_epidemio_commune": {"titre": "Situation sur votre commune (Brimont) : ", "texte": "Texte du moteur."}}
        doc = self.apres.build_bulletin(client, {"risque_mildiou": "x"}, [], {"pluie": 5, "temp": 14}, None)
        textes = " ".join(str(d) for d in doc)
        self.assertNotIn("CONCLUSION-CRUE", textes)                                    # la règle fruste ne conclut plus
        self.assertIn("faits. ", textes)                                               # mais les faits de l'exploitation restent
        self.assertIn("Texte du moteur.", textes)
        self.assertLess(textes.index("Situation parcellaire"), textes.index("Texte du moteur."))

    def test_bloc_commune_avant_les_parcelles_sensibles(self):
        client = {"parcelles_mildiou": "Les Vignes", "_epidemio_commune": {"titre": "T : ", "texte": "Bloc."}}
        doc = self.apres.build_bulletin(client, {}, [], None, None)
        textes = [str(d) for d in doc]
        i_bloc = next(i for i, d in enumerate(textes) if "Bloc." in d)
        i_parc = next(i for i, d in enumerate(textes) if "Parcelles sensibles" in d)
        self.assertLess(i_bloc, i_parc)

    def test_sans_bloc_commune_la_conclusion_ancienne_est_conservee(self):
        doc = self.apres.build_bulletin({}, {}, [], {"pluie": 5, "temp": 14}, None)
        self.assertIn("CONCLUSION-CRUE", " ".join(str(d) for d in doc))

    def test_le_tableau_prend_le_risque_du_moteur_date_par_date(self):
        client = self.commune(**{"2026-10-06": "Très élevé", "2026-10-07": "Nul"})
        doc = self.apres.build_bulletin(client, {}, [], None, self.meteo())
        lignes = [d[1][0] for d in doc if d[0] == "multi" and str(d[1][0]).startswith("LIGNE")]
        self.assertEqual(lignes, ["LIGNE 2026-10-06 Très élevé", "LIGNE 2026-10-07 Nul",
                                  "LIGNE 2026-10-08 Modéré"])                          # date inconnue du moteur : ancien libellé
        alerte = next(d for d in doc if d[0] == "alert")
        self.assertIn("2 jour(s)", alerte[1])                                           # le résumé suit les nouveaux libellés
        self.assertIn("2026-10-06", alerte[2])
        self.assertNotIn("2026-10-07", alerte[2])                                       # l'ancien « Élevé » du 07 est corrigé en « Nul »

    def test_phrase_pas_de_risque_selon_la_source(self):
        faible = self.commune(**{"2026-10-06": "Nul", "2026-10-07": "Faible", "2026-10-08": "Nul"})
        meteo = self.meteo()
        doc = self.apres.build_bulletin(faible, {}, [], None, meteo)
        alerte = next(d for d in doc if d[0] == "alert")
        self.assertEqual(alerte[1], "✅ Pas de risque mildiou sur 7 jours")
        self.assertEqual(alerte[2], "Aucune infection significative n'est attendue sur votre commune d'après la météo.")
        calme = [{"date": "2026-10-06", "risk": "Faible"}]                              # sans moteur : ancienne phrase
        doc = self.apres.build_bulletin({}, {}, [], None, calme)
        self.assertIn("pluie ≥ 2 mm", next(d for d in doc if d[0] == "alert")[2])

    def test_la_meteo_partagee_entre_clients_n_est_pas_modifiee(self):
        meteo = self.meteo()
        copie = [dict(d) for d in meteo]
        self.apres.build_bulletin(self.commune(**{"2026-10-06": "Très élevé"}), {}, [], None, meteo)
        self.assertEqual(meteo, copie)                                                   # le client suivant voit la météo d'origine

    def test_bloc_commune_sans_risque_jours_laisse_le_tableau_comme_avant(self):
        client = {"_epidemio_commune": {"titre": "T : ", "texte": "Bloc."}}
        doc = self.apres.build_bulletin(client, {}, [], None, self.meteo())
        lignes = [d[1][0] for d in doc if d[0] == "multi" and str(d[1][0]).startswith("LIGNE")]
        self.assertEqual(lignes, ["LIGNE 2026-10-06 Faible", "LIGNE 2026-10-07 Élevé", "LIGNE 2026-10-08 Modéré"])


class TestBrancherHtml(unittest.TestCase):
    def test_dashboard_et_portail(self):
        d, e = br.modifier_dashboard(STUB_DASHBOARD)
        self.assertEqual(e, ["retours à la ligne"])
        self.assertEqual(d.count(".replace(/\\n/g,'<br>')"), 4)
        self.assertIn("${(b.risque_mildiou||'—').replace(/\\n/g,'<br>')}", d)
        self.assertEqual(br.modifier_dashboard(d), (d, []))
        p, e = br.modifier_portail(STUB_PORTAIL)
        self.assertEqual(p.count(".replace(/\\n/g,'<br>')"), 2)
        self.assertIn("<br>${(b.risque_oidium||'').replace(/\\n/g,'<br>')}</div>", p)
        self.assertEqual(br.modifier_portail(p), (p, []))

    def test_ancrage_manquant_refuse(self):
        with self.assertRaises(ValueError):
            br.modifier_dashboard(STUB_DASHBOARD.replace("${av.risque_mildiou||'?'}", "AUTRE"))
        with self.assertRaises(ValueError):
            br.modifier_portail(STUB_PORTAIL + STUB_PORTAIL)                            # ancres en double : ambigu


STUB_DASHBOARD_EPI = ("<input id=\"bh_ep\" value=\"\">\n"
                      "async function gen(){\n  const d={};\n  document.getElementById('bh_rm').value=d.risque_mildiou||'';\n}\n")


class TestBrancherDashboardEpi(unittest.TestCase):
    def test_ligne_epi_ajoutee_si_elle_manque(self):
        src = STUB_DASHBOARD + STUB_DASHBOARD_EPI
        nouveau, etapes = br.modifier_dashboard(src)
        self.assertEqual(etapes, ["retours à la ligne", "champ EPI"])
        self.assertIn("  document.getElementById('bh_ep').value=d.epi||'';\n  document.getElementById('bh_rm')", nouveau)
        self.assertEqual(br.modifier_dashboard(nouveau), (nouveau, []))                # idempotent

    def test_rien_si_la_ligne_existe_deja(self):
        src = STUB_DASHBOARD + STUB_DASHBOARD_EPI.replace("  document.getElementById('bh_rm')", "  document.getElementById('bh_ep').value=d.epi||'';\n  document.getElementById('bh_rm')")
        self.assertEqual(br.modifier_dashboard(src)[1], ["retours à la ligne"])

    def test_pas_de_refus_si_le_formulaire_n_a_pas_de_champ_epi_ou_si_l_ancre_differe(self):
        self.assertEqual(br.modifier_dashboard(STUB_DASHBOARD)[1], ["retours à la ligne"])
        autre_indent = STUB_DASHBOARD + STUB_DASHBOARD_EPI.replace("  document.getElementById('bh_rm')", "    document.getElementById('bh_rm')")
        self.assertEqual(br.modifier_dashboard(autre_indent)[1], ["retours à la ligne"])      # on n'invente pas : on ne fait rien


class TestBrancherServiceWorker(unittest.TestCase):
    SW = "// VITI Sens — Service Worker\nconst CACHE_NAME = 'vitisens-v1';\nconst ASSETS = [];\n"

    def test_version_du_cache_renouvelee(self):
        nouveau, etapes = br.modifier_sw(self.SW)
        self.assertEqual(etapes, ["cache du navigateur renouvelé"])
        self.assertIn("const CACHE_NAME = 'vitisens-v2';", nouveau)
        self.assertNotIn("vitisens-v1", nouveau)
        self.assertEqual(br.modifier_sw(nouveau), (nouveau, []))                       # idempotent

    def test_une_version_deja_superieure_n_est_pas_touchee(self):
        for v in (2, 3, 12):
            src = self.SW.replace("v1", f"v{v}")
            self.assertEqual(br.modifier_sw(src), (src, []))

    def test_ligne_introuvable_refusee(self):
        with self.assertRaises(ValueError):
            br.modifier_sw("const AUTRE = 1;\n")


class TestBrancherLigneDeCommande(unittest.TestCase):
    def dossier(self, **remplacements):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fichiers = {"serveur_vitisens.py": STUB_SERVEUR, "generer_bulletins_v4.py": STUB_GENERATEUR,
                    "dashboard_v2.html": STUB_DASHBOARD, "portail_client.html": STUB_PORTAIL}
        fichiers.update(remplacements)
        for nom, contenu in fichiers.items():
            if contenu is not None:
                with open(os.path.join(d, nom), "w", encoding="utf-8", newline="") as f:
                    f.write(contenu)
        return d

    def lire(self, d, nom):
        with open(os.path.join(d, nom), encoding="utf-8", newline="") as f:
            return f.read()

    def test_branche_les_quatre_fichiers_puis_retire(self):
        d = self.dossier()
        self.assertEqual(silencieux(br.main, [d]), 0)
        for nom in ("serveur_vitisens.py", "generer_bulletins_v4.py", "dashboard_v2.html", "portail_client.html"):
            self.assertTrue(os.path.exists(os.path.join(d, nom + br.SAUVEGARDE)), nom)
        branche = {n: self.lire(d, n) for n in os.listdir(d) if not n.endswith(br.SAUVEGARDE)}
        self.assertEqual(silencieux(br.main, [d]), 0)                                  # idempotent
        self.assertEqual({n: self.lire(d, n) for n in branche}, branche)
        self.assertEqual(silencieux(br.main, [d, "--retirer"]), 0)
        self.assertEqual(self.lire(d, "serveur_vitisens.py"), STUB_SERVEUR)            # retour à l'identique
        self.assertEqual(self.lire(d, "generer_bulletins_v4.py"), STUB_GENERATEUR)

    def test_un_fichier_different_est_refuse_sans_bloquer_les_autres(self):
        d = self.dossier(**{"dashboard_v2.html": "<html>autre version</html>"})
        self.assertEqual(silencieux(br.main, [d]), 1)
        self.assertEqual(self.lire(d, "dashboard_v2.html"), "<html>autre version</html>")           # intact
        self.assertFalse(os.path.exists(os.path.join(d, "dashboard_v2.html" + br.SAUVEGARDE)))     # et sans sauvegarde
        self.assertIn("epidemio_pilot", self.lire(d, "serveur_vitisens.py"))                       # les autres sont traités
        self.assertIn("analyse_en_paragraphes", self.lire(d, "generer_bulletins_v4.py"))

    def test_la_synthese_nomme_le_fichier_refuse(self):
        d = self.dossier(**{"serveur_vitisens.py": STUB_SERVEUR.replace(br.S_STYLE, "texte libre.")})
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            code = br.main([d])
        texte = sortie.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("serveur_vitisens.py        REFUSÉ", texte)
        self.assertIn("Fichier(s) refusé(s), non modifié(s) : serveur_vitisens.py.", texte)
        self.assertIn("Copie-moi la ligne « REFUSÉ »", texte)
        self.assertIn("analyse_en_paragraphes", self.lire(d, "generer_bulletins_v4.py"))        # les autres sont traités

    def test_fichiers_absents_ignores_serveur_obligatoire(self):
        d = self.dossier(**{"dashboard_v2.html": None, "portail_client.html": None})
        self.assertEqual(silencieux(br.main, [d]), 0)
        d2 = self.dossier(**{"serveur_vitisens.py": None})
        self.assertEqual(silencieux(br.main, [d2]), 1)

    def test_accepte_aussi_le_chemin_du_serveur(self):
        d = self.dossier()
        self.assertEqual(silencieux(br.main, [os.path.join(d, "serveur_vitisens.py")]), 0)
        self.assertIn("epidemio_pilot", self.lire(d, "serveur_vitisens.py"))

    def test_retirer_sans_sauvegarde_ne_fait_rien(self):
        d = self.dossier()
        self.assertEqual(silencieux(br.main, [d, "--retirer"]), 0)
        self.assertEqual(self.lire(d, "serveur_vitisens.py"), STUB_SERVEUR)


if __name__ == "__main__":
    unittest.main(verbosity=1)
