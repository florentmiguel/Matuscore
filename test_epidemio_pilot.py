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
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

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
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.ancien = os.environ.get("EPIDEMIO_CACHE_DIR")
        os.environ["EPIDEMIO_CACHE_DIR"] = self.cache

    def tearDown(self):
        if self.ancien is None:
            os.environ.pop("EPIDEMIO_CACHE_DIR", None)
        else:
            os.environ["EPIDEMIO_CACHE_DIR"] = self.ancien
        shutil.rmtree(self.cache, ignore_errors=True)


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


class TestCache(Base):
    def setUp(self):
        super().setUp()
        self.appels = 0

    def recup(self, lat, lon):
        self.appels += 1
        return [{"time": "2026-01-01T00:00", "temperature_2m": 1.0}]

    def test_deuxieme_appel_vient_du_cache(self):
        _, age, perimee = ep.serie_horaire(49.25, 3.96, NOW, self.recup)
        self.assertEqual((self.appels, perimee), (1, False))
        _, age, perimee = ep.serie_horaire(49.25, 3.96, NOW, self.recup)
        self.assertEqual((self.appels, perimee), (1, False))
        self.assertGreaterEqual(age, 0)

    def test_cache_expire_apres_la_duree_de_vie(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.recup, ttl_s=3600)
        ep.serie_horaire(49.25, 3.96, NOW, self.recup, ttl_s=-1)                    # tout âge dépasse -1 s : on recharge
        self.assertEqual(self.appels, 2)

    def test_une_position_differente_a_son_propre_cache(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.recup)
        ep.serie_horaire(49.10, 3.80, NOW, self.recup)
        self.assertEqual(self.appels, 2)

    def test_panne_du_service_meteo_avec_cache_perime(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.recup)

        def panne(lat, lon):
            raise OSError("Open-Meteo injoignable")
        lignes, age, perimee = ep.serie_horaire(49.25, 3.96, NOW, panne, ttl_s=-1)
        self.assertTrue(perimee)
        self.assertEqual(len(lignes), 1)

    def test_panne_sans_cache_remonte_l_erreur(self):
        def panne(lat, lon):
            raise OSError("Open-Meteo injoignable")
        with self.assertRaises(OSError):
            ep.serie_horaire(49.25, 3.96, NOW, panne)

    def test_aucun_fichier_temporaire_ne_reste(self):
        ep.serie_horaire(49.25, 3.96, NOW, self.recup)
        self.assertEqual([f for f in os.listdir(self.cache) if f.endswith(".tmp")], [])


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
        self.assertIn("2 h", s["alerte_meteo"])
        self.assertNotIn("alerte_meteo", self.s)


class TestMoteurReel(Base):
    """Avec le vrai moteur (dépôt epidemio) sur une saison synthétique."""

    def test_calcul_et_synthese(self):
        try:
            res, meta = ep.calculer(49.25, 3.96, now=NOW, recuperer=lambda a, b: saison())
        except ep.MoteurIndisponible as e:
            self.skipTest(str(e))
        self.assertEqual(meta["profil"], "calage_2026")
        self.assertFalse(meta["meteo_perimee"])
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
        self.assertEqual(json.loads(b.split("\n", 1)[1]), self.ANCIENNE)

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
        ep.calculer = lambda lat, lon, now=None, recuperer=None: (faux_resultat(), {"profil": "calage_2026", "meteo_perimee": False,
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

    def test_autre_panne_donne_502(self):
        def panne(*a, **k):
            raise RuntimeError("Open-Meteo")
        ep.calculer = panne
        self.assertEqual(self.client.get("/api/epidemio-moteur").status_code, 502)


STUB = '''import json
from flask import Flask
app = Flask(__name__)

def _calculer_synthese_epidemio(lat, lon, m, r):
    return [], {"texte_mildiou": "ANCIEN-MILDIOU", "texte_oidium": "OIDIUM-ANCIEN"}

@app.route('/api/generer-texte-bulletin', methods=['GET'])
def generer_texte_bulletin():
    lat, lon, maturite, receptive = 49.25, 3.96, True, True
    try:
        meteo_7j, synthese = _calculer_synthese_epidemio(lat, lon, maturite, receptive)

        prompt = f"""Voici la météo.

SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :
{json.dumps(synthese, ensure_ascii=False, indent=2)}

Rédige."""
        return prompt
    except Exception as e:
        return str(e)
'''


def silencieux(fonction, *args):
    """Exécute fonction en masquant ses messages (le script de branchement parle beaucoup)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fonction(*args)


class TestBrancher(unittest.TestCase):
    def test_modification_compile_et_est_idempotente(self):
        nouveau = br.modifier(STUB)
        compile(nouveau, "stub", "exec")
        self.assertEqual(br.modifier(nouveau), nouveau)                              # 2e passage : inchangé
        self.assertIn("{bloc_epidemio}", nouveau)
        self.assertNotIn("{json.dumps(synthese, ensure_ascii=False, indent=2)}", nouveau)
        self.assertEqual(nouveau.count("epidemio_pilot"), 1)

    def test_point_d_ancrage_manquant_ou_ambigu_refuse(self):
        for ancre in (br.ANCRE_ROUTE, br.ANCRE_APPEL, br.ANCRE_PROMPT):
            with self.assertRaises(ValueError):
                br.modifier(STUB.replace(ancre, "# retiré\n"))
            with self.assertRaises(ValueError):
                br.modifier(STUB + "\n" + ancre)                                      # doublon : ambigu

    def executer(self, src):
        mod = types.ModuleType("stub_serveur")
        exec(compile(src, "stub", "exec"), mod.__dict__)
        return mod

    def test_serveur_modifie_utilise_le_moteur_puis_replie(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask absent")
        mod = self.executer(br.modifier(STUB))
        ancien = ep.calculer
        try:
            ep.calculer = lambda lat, lon, now=None, recuperer=None: (faux_resultat(), {"profil": "calage_2026"})
            avec = mod.generer_texte_bulletin()
            self.assertIn("MILDIOU (nouveau moteur horaire)", avec)
            self.assertNotIn("ANCIEN-MILDIOU", avec)
            self.assertIn("OIDIUM-ANCIEN", avec)

            def panne(*a, **k):
                raise ep.MoteurIndisponible("absent")
            ep.calculer = panne
            sans = mod.generer_texte_bulletin()
            self.assertIn("ANCIEN-MILDIOU", sans)
            self.assertNotIn("nouveau moteur horaire", sans)
        finally:
            ep.calculer = ancien

    def test_ligne_de_commande_branche_sauvegarde_et_retire(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = os.path.join(d, "serveur_vitisens.py")
            with open(chemin, "w", encoding="utf-8", newline="") as f:
                f.write(STUB)
            self.assertEqual(silencieux(br.main, [chemin]), 0)
            self.assertTrue(os.path.exists(chemin + br.SAUVEGARDE))
            with open(chemin, encoding="utf-8") as f:
                branche = f.read()
            self.assertIn("epidemio_pilot", branche)
            self.assertEqual(silencieux(br.main, [chemin]), 0)                                    # idempotent
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(f.read(), branche)
            self.assertEqual(silencieux(br.main, [chemin, "--retirer"]), 0)
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(f.read(), STUB)                                      # retour à l'identique

    def test_echec_n_ecrit_rien(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = os.path.join(d, "serveur_vitisens.py")
            autre = "app = None\n# serveur différent de la version attendue\n"
            with open(chemin, "w", encoding="utf-8") as f:
                f.write(autre)
            self.assertEqual(silencieux(br.main, [chemin]), 1)
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(f.read(), autre)
            self.assertFalse(os.path.exists(chemin + br.SAUVEGARDE))
            self.assertEqual(silencieux(br.main, [os.path.join(d, "absent.py")]), 1)
            self.assertEqual(silencieux(br.main, [chemin, "--retirer"]), 1)                       # pas de sauvegarde


if __name__ == "__main__":
    unittest.main(verbosity=1)
