#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests du moteur botrytis (González-Domínguez et al. 2015). Attentes calculées à la main
depuis les équations de l'article, ou depuis les résultats publiés (valeurs moyennes par groupe)."""
import math
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import botrytis as b

UTC = timezone.utc
TZ = ZoneInfo("UTC")
P = b.PARAMS


def serie(debut: date, jours: int, temp=20.0, hr=75.0, pluie=0.0) -> list[dict]:
    rows, t = [], datetime(debut.year, debut.month, debut.day, tzinfo=UTC)
    for _ in range(jours * 24):
        rows.append({"t": t, "temp": temp, "hr": hr, "pluie": pluie})
        t += timedelta(hours=1)
    return rows


def bbch_fixe(debut: date, jours: int, valeur: float) -> dict:
    return {debut + timedelta(days=i): valeur for i in range(jours)}


class TestFonctionsDeBase(unittest.TestCase):

    def test_teq_bornes(self):
        self.assertEqual(b._teq(0.0, 0.0, 40.0), 0.0)
        self.assertEqual(b._teq(40.0, 0.0, 40.0), 0.0)
        self.assertAlmostEqual(b._teq(20.0, 0.0, 40.0), 0.5)
        self.assertEqual(b._teq(-5.0, 0.0, 40.0), 0.0)

    def test_teq_intermediaire(self):
        self.assertAlmostEqual(b._teq(10.0, 0.0, 40.0), 0.25)

    def test_taux_mygr_analytique(self):
        """T=20°C, Tmin=0, Tmax=40, Mf=1 : 3.78 × 0.5^0.9 × 0.5^0.475 × 1"""
        attendu = 3.78 * 0.5**0.9 * 0.5**0.475
        self.assertAlmostEqual(b.taux_mygr(20.0, 1.0, P), attendu, places=6)

    def test_taux_mygr_nul_hors_plage(self):
        self.assertEqual(b.taux_mygr(0.0, 1.0, P), 0.0)
        self.assertEqual(b.taux_mygr(40.0, 1.0, P), 0.0)
        self.assertEqual(b.taux_mygr(20.0, 0.0, P), 0.0)   # mf=0 : pas d'humidité

    def test_taux_mygr_croissant_avec_mf(self):
        m0 = b.taux_mygr(20.0, 0.5, P)
        m1 = b.taux_mygr(20.0, 1.0, P)
        self.assertAlmostEqual(m1, 2 * m0, places=6)

    def test_taux_spor_analytique(self):
        """T=20, HR=80 : Teq=(20/35), rh_term=3.595+0.097×80−0.0005×6400"""
        teq = 20 / 35
        rh_term = 3.595 + 0.097 * 80 - 0.0005 * 80**2
        attendu = 3.7 * teq**0.9 * (1 - teq)**10.493 / rh_term
        self.assertAlmostEqual(b.taux_spor(20.0, 80.0, P), attendu, places=8)

    def test_taux_spor_nul_hors_plage(self):
        self.assertEqual(b.taux_spor(0.0, 80.0, P), 0.0)
        self.assertEqual(b.taux_spor(35.0, 80.0, P), 0.0)

    def test_ciso_moyenne_glissante_7j(self):
        hist = [(1.0, 1.0)] * 7
        self.assertAlmostEqual(b.ciso_jour(hist), 1.0)
        hist2 = [(0.0, 0.0)] * 3 + [(1.0, 1.0)] * 4
        self.assertAlmostEqual(b.ciso_jour(hist2), 4.0 / 7.0, places=6)

    def test_ciso_vide(self):
        self.assertEqual(b.ciso_jour([]), 0.0)


class TestSusceptibilite(unittest.TestCase):

    def test_sus1_maximum_a_bbch_65_67(self):
        """Maximum de susceptibilité autour de la floraison (Ciliberti 2014)."""
        vals = {bbch: b.sus1(float(bbch)) for bbch in range(53, 74)}
        bbch_max = max(vals, key=vals.get)
        self.assertIn(bbch_max, (65, 66, 67))
        self.assertGreater(vals[bbch_max], 0.95)

    def test_sus1_decroissant_vers_les_bords(self):
        self.assertGreater(b.sus1(65), b.sus1(57))
        self.assertGreater(b.sus1(65), b.sus1(71))

    def test_sus1_nul_hors_fenetre(self):
        self.assertEqual(b.sus1(50.0), 0.0)
        self.assertEqual(b.sus1(30.0), 0.0)

    def test_sus1_positif_dans_la_fenetre(self):
        for bbch in range(53, 74):
            self.assertGreater(b.sus1(float(bbch)), 0.0, f"sus1({bbch}) <= 0")

    def test_sus2_exponentielle_croissante(self):
        self.assertLess(b.sus2(79.0), b.sus2(83.0))
        self.assertLess(b.sus2(83.0), b.sus2(89.0))
        self.assertAlmostEqual(b.sus2(79.0), 5e-17 * math.exp(0.4219 * 79), places=8)

    def test_sus3_lineaire_plafonnee(self):
        self.assertAlmostEqual(b.sus3(79.0), 0.0546 * 79 - 3.87, places=6)
        self.assertEqual(b.sus3(100.0), 1.0)
        # Valeur minimale non nulle à BBCH=79
        self.assertGreater(b.sus3(79.0), 0.0)

    def test_sus3_croissante_jusqu_a_1(self):
        v = [b.sus3(float(i)) for i in range(79, 90)]
        self.assertTrue(all(b >= a for a, b in zip(v, v[1:])))
        self.assertAlmostEqual(v[-1], 0.9894, delta=0.001)  # 0.0546×89-3.87 = 0.9894 < 1


class TestInfectionPeriode1(unittest.TestCase):

    def test_inf1_analytique(self):
        """T=20, WD=12, BBCH=65 : valeur analytique complète."""
        teq = 20 / 35
        wd_term = 1.0 + math.exp(1.85 - 0.19 * 12)
        attendu = 3.56 * teq**0.99 * (1 - teq)**0.71 / wd_term * b.sus1(65.0)
        self.assertAlmostEqual(b.inf1(20.0, 12.0, 65.0, P), attendu, places=8)

    def test_inf1_nul_hors_plage_thermique(self):
        self.assertEqual(b.inf1(0.0, 12.0, 65.0, P), 0.0)
        self.assertEqual(b.inf1(35.0, 12.0, 65.0, P), 0.0)

    def test_inf1_croissant_avec_wd(self):
        """Plus de mouillure → plus d'infection."""
        self.assertLess(b.inf1(20.0, 4.0, 65.0, P), b.inf1(20.0, 12.0, 65.0, P))
        self.assertLess(b.inf1(20.0, 12.0, 65.0, P), b.inf1(20.0, 24.0, 65.0, P))

    def test_inf1_sensibilite_maximale_a_la_floraison(self):
        inf_53 = b.inf1(20.0, 12.0, 53.0, P)
        inf_65 = b.inf1(20.0, 12.0, 65.0, P)
        inf_73 = b.inf1(20.0, 12.0, 73.0, P)
        self.assertGreater(inf_65, inf_53)
        self.assertGreater(inf_65, inf_73)


class TestInfectionPeriode2(unittest.TestCase):

    def test_inf2_analytique(self):
        """T=20, WD=12, BBCH=83."""
        teq = 20 / 35
        wd_term = math.exp(-2.3 * math.exp(-0.048 * 12))
        attendu = 6.416 * teq**1.292 * (1 - teq)**0.469 * wd_term * b.sus2(83.0)
        self.assertAlmostEqual(b.inf2(20.0, 12.0, 83.0, P), attendu, places=8)

    def test_inf2_croissant_avec_wd(self):
        self.assertLess(b.inf2(20.0, 4.0, 83.0, P), b.inf2(20.0, 12.0, 83.0, P))

    def test_inf2_croissant_avec_bbch(self):
        """Sensibilité croissante des baies vers la maturité (exponentielle)."""
        self.assertLess(b.inf2(20.0, 12.0, 79.0, P), b.inf2(20.0, 12.0, 89.0, P))

    def test_inf3_analytique(self):
        """T=20, HR=80, BBCH=83."""
        teq = 20 / 30
        rh_term = 1.0 + math.exp((35.364 - 0.26 * 80) / 100)   # signe + (courbe croissante en HR)
        attendu = 7.75 * teq**2.14 * (1 - teq)**0.469 / rh_term * b.sus3(83.0)
        self.assertAlmostEqual(b.inf3(20.0, 80.0, 83.0, P), attendu, places=8)

    def test_inf3_croissant_avec_hr(self):
        """HR élevée favorise légèrement l'infection de baie à baie (variation < 10 % sur 60→90 %)."""
        v60, v90 = b.inf3(20.0, 60.0, 83.0, P), b.inf3(20.0, 90.0, 83.0, P)
        self.assertGreater(v90, v60)
        self.assertLess((v90 - v60) / v60, 0.10)   # variation < 10 % : faible mais positive

    def test_inf3_nul_hors_plage_thermique(self):
        self.assertEqual(b.inf3(0.0, 80.0, 83.0, P), 0.0)
        self.assertEqual(b.inf3(30.0, 80.0, 83.0, P), 0.0)


class TestWDProxy(unittest.TestCase):

    def test_heures_comptees_si_hr_eleve(self):
        rows = [{"t": datetime(2026, 6, 1, h, tzinfo=UTC), "hr": 95.0 if h < 10 else 70.0, "pluie": 0.0}
                for h in range(24)]
        self.assertEqual(b.wd_depuis_horaires(rows, P), 10)

    def test_heures_comptees_si_pluie(self):
        rows = [{"t": datetime(2026, 6, 1, h, tzinfo=UTC), "hr": 70.0, "pluie": 0.5 if h < 3 else 0.0}
                for h in range(24)]
        self.assertEqual(b.wd_depuis_horaires(rows, P), 3)

    def test_cumul_hr_et_pluie_sans_doublon(self):
        """HR élevée ET pluie en même temps : compte 1 heure, pas 2."""
        rows = [{"t": datetime(2026, 6, 1, h, tzinfo=UTC), "hr": 95.0, "pluie": 0.5}
                for h in range(24)]
        self.assertEqual(b.wd_depuis_horaires(rows, P), 24)

    def test_mf_fraction(self):
        rows = [{"t": datetime(2026, 6, 1, h, tzinfo=UTC), "hr": 95.0 if h < 12 else 70.0, "pluie": 0.0}
                for h in range(24)]
        self.assertAlmostEqual(b.mf_depuis_horaires(rows, P), 0.5)


class TestClassificationDFA(unittest.TestCase):
    """Reproduction des valeurs moyennes publiées (Table résultats de l'article)."""

    def test_valeurs_moyennes_faibles(self):
        cl = b._discriminer(0.94, 0.15, 0.34, P)
        self.assertEqual(cl["classe"], "faible")
        self.assertGreater(cl["probabilites"]["faible"], 0.5)

    def test_valeurs_moyennes_intermediaires(self):
        cl = b._discriminer(2.46, 0.20, 0.37, P)
        self.assertEqual(cl["classe"], "intermediaire")
        self.assertGreater(cl["probabilites"]["intermediaire"], 0.5)

    def test_valeurs_moyennes_severes(self):
        cl = b._discriminer(1.81, 0.28, 0.76, P)
        self.assertEqual(cl["classe"], "severe")
        self.assertGreater(cl["probabilites"]["severe"], 0.4)

    def test_sev_nuls_donne_classe_faible(self):
        cl = b._discriminer(0.0, 0.0, 0.0, P)
        self.assertEqual(cl["classe"], "faible")

    def test_sev_tres_eleves_donne_severe(self):
        cl = b._discriminer(3.0, 1.0, 2.0, P)
        self.assertEqual(cl["classe"], "severe")

    def test_f1_et_f2_analytiques(self):
        """Vérification des valeurs F1 et F2 pour SEV1=0.94, SEV2=0.15, SEV3=0.34."""
        x1 = math.log(0.94 + 1)
        x2 = math.log(0.15 + 0.34 + 1)
        f1 = -4.191 + 4.180 * x1 - 0.310 * x2
        f2 = -2.090 + 0.595 * x1 + 3.067 * x2
        cl = b._discriminer(0.94, 0.15, 0.34, P)
        self.assertAlmostEqual(cl["F1"], round(f1, 4), places=3)
        self.assertAlmostEqual(cl["F2"], round(f2, 4), places=3)


class TestMoteurComplet(unittest.TestCase):
    DEB = date(2026, 4, 1)

    def _run(self, temp=20.0, hr=80.0, pluie=1.0, jours=60, bbch_val=65.0,
             fenetre=1, params=None):
        """Lance la simulation avec un BBCH fixe dans la fenêtre voulue."""
        if fenetre == 1:
            b_val = 65.0  # floraison, dans fenêtre 1 [53-73]
        else:
            b_val = 83.0  # véraison, dans fenêtre 2 [79-89]
        rows = serie(self.DEB, jours, temp, hr, pluie)
        bbch = bbch_fixe(self.DEB, jours, b_val)
        return b.calculer_saison(rows, bbch, TZ, params)

    def test_sev1_s_accumule_dans_fenetre_1(self):
        res = self._run(fenetre=1)
        self.assertGreater(res["sev1"], 0.0)
        self.assertEqual(res["sev2"], 0.0)
        self.assertEqual(res["sev3"], 0.0)

    def test_sev2_et_sev3_s_accumulent_dans_fenetre_2(self):
        res = self._run(fenetre=2)
        self.assertEqual(res["sev1"], 0.0)
        self.assertGreater(res["sev2"], 0.0)
        self.assertGreater(res["sev3"], 0.0)

    def test_pluie_augmente_wd_et_donc_sev1(self):
        """Pluie → plus d'heures mouillées → WD plus grand → SEV1 plus élevé."""
        sans = self._run(pluie=0.0, hr=60.0, fenetre=1)
        avec = self._run(pluie=2.0, hr=60.0, fenetre=1)
        self.assertGreater(avec["sev1"], sans["sev1"])

    def test_hr_elevee_augmente_sev3(self):
        """HR élevée favorise l'infection de baie à baie (inf3 ~ HR)."""
        bas = self._run(hr=60.0, fenetre=2)
        haut = self._run(hr=95.0, fenetre=2)
        self.assertGreater(haut["sev3"], bas["sev3"])

    def test_hors_fenetre_sev_restent_nuls(self):
        rows = serie(self.DEB, 60)
        bbch = bbch_fixe(self.DEB, 60, 45.0)   # BBCH 45 : avant la fenêtre 1
        res = b.calculer_saison(rows, bbch, TZ)
        self.assertEqual((res["sev1"], res["sev2"], res["sev3"]), (0.0, 0.0, 0.0))

    def test_serie_vide(self):
        res = b.calculer_saison([], {}, TZ)
        self.assertFalse(res["actif"] if "actif" in res else False)
        self.assertEqual((res["sev1"], res["sev2"], res["sev3"]), (0.0, 0.0, 0.0))

    def test_sans_phenologie_sev_nuls(self):
        rows = serie(self.DEB, 60)
        res = b.calculer_saison(rows, {}, TZ)
        self.assertEqual((res["sev1"], res["sev2"], res["sev3"]), (0.0, 0.0, 0.0))
        self.assertTrue(any("phénologie" in a for a in res["avertissements"]))

    def test_jours_contiennent_les_cumuls(self):
        res = self._run(fenetre=1)
        jours = {d["date"]: d for d in res["jours"]}
        derniere_date = max(jours)
        self.assertAlmostEqual(jours[derniere_date]["sev1"], res["sev1"], places=4)

    def test_les_cumuls_sont_monotones(self):
        res = self._run(fenetre=1)
        s = [d["sev1"] for d in res["jours"]]
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(s, s[1:])))

    def test_saison_chaude_et_humide_produit_epidemie_intermediaire_ou_severe(self):
        """20 °C, HR 90 %, pluie tous les jours sur toute la saison : épidémie non faible."""
        rows = serie(self.DEB, 60, temp=20.0, hr=90.0, pluie=3.0)
        bbch_f1 = {self.DEB + timedelta(days=i): 65.0 for i in range(30)}
        bbch_f2 = {self.DEB + timedelta(days=30+i): 83.0 for i in range(30)}
        res = b.calculer_saison(rows, {**bbch_f1, **bbch_f2}, TZ)
        self.assertNotEqual(res["classification"]["classe"], "faible")

    def test_saison_seche_et_fraiche_produit_epidemie_faible(self):
        """10 °C, HR 50 %, pas de pluie : WD = 0, ciso faible → SEV très faibles."""
        rows = serie(self.DEB, 60, temp=10.0, hr=50.0, pluie=0.0)
        bbch_f1 = {self.DEB + timedelta(days=i): 65.0 for i in range(30)}
        bbch_f2 = {self.DEB + timedelta(days=30+i): 83.0 for i in range(30)}
        res = b.calculer_saison(rows, {**bbch_f1, **bbch_f2}, TZ)
        self.assertEqual(res["classification"]["classe"], "faible")

    def test_parametres_originaux_intacts(self):
        avant = repr(b.PARAMS)
        self._run(params={"spor": {"tmax": 30.0}})
        self.assertEqual(repr(b.PARAMS), avant)


if __name__ == "__main__":
    unittest.main(verbosity=1)
