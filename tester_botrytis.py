#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test du moteur botrytis.py (González-Domínguez et al. 2015) sur Reims, saisons 2011 à 2026.

Phénologie : débourrement par BRIN (dormance calculée sur l'automne précédent), puis stade BBCH par degrés-jours (phenologie.py,
table jusqu'à BBCH 89 : les fenêtres 1 (BBCH 53-73) et 2 (BBCH 79-89) sont couvertes). Pour 2026, les stades relevés dans les BSV
(stades_bsv_2026.csv du dépôt epidemio) recalent la phénologie.
Météo : archives Open-Meteo déjà en cache dans ~/archive_meteo/ (aucun appel, sauf année manquante).

    cd ~/Matuscore && set -a && source .env && set +a
    EPIDEMIO_PATH=~/epidemio venv/bin/python3 tester_botrytis.py
"""
import os
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import epidemio_pilot as ep
import caler_indice_chasmotheces as cal          # archive(annee) : cache ~/archive_meteo/

TZ = ZoneInfo("Europe/Paris")
DEBOURREMENT_OBSERVE = {2026: date(2026, 3, 28)}       # débourrement observé (note de transition, session 11)
# Début des vendanges observé (BBCH 87 en Champagne) : recale la fin de cycle. « Fin août » est pris au 28/08.
VENDANGES = {2022: "2022-08-28", 2025: "2025-08-28", 2026: "2026-08-15"}


def rows_annee(annee: int) -> list[dict]:
    """Lignes horaires de l'automne précédent (dormance BRIN) et de l'année, jusqu'au 31/12 20 h UTC."""
    lignes = cal.archive(annee - 1) + cal.archive(annee)
    return [r for r in ep.lignes_vers_rows(lignes)
            if datetime(annee - 1, 8, 1, tzinfo=timezone.utc) <= r["t"] < datetime(annee, 12, 31, 20, tzinfo=timezone.utc)]


def main() -> int:
    ep.charger_moteur()
    import botrytis as bo
    import phenologie as phen
    import phenologie_brin_gfv as pbg

    obs_2026 = {}
    chemin = os.path.join(ep.chemin_moteur(), "stades_bsv_2026.csv")
    if os.path.exists(chemin):
        obs_2026 = phen.lire_stades(chemin)

    print("Botrytis — Reims (49,25 N ; 3,96 E) — González-Domínguez et al. 2015\n")
    print("* : fin de cycle recalée sur la date de vendange observée\n")
    print("année  débourr.  BBCH53   BBCH73   BBCH79   BBCH87     SEV1    SEV2    SEV3   classe          faible inter. sévère")
    lignes_res = []
    for annee in range(2012, 2027):
        try:
            rows = rows_annee(annee)
        except Exception as e:                                          # noqa: BLE001
            print(f"{annee}  archive indisponible ({e})")
            continue
        obs = dict(obs_2026) if annee == 2026 else {}
        if annee in VENDANGES:
            obs[VENDANGES[annee]] = 87          # vendange champenoise : BBCH 86-87
        deb = pbg.brin(rows, TZ, annee, None)["debourrement"]
        deb = DEBOURREMENT_OBSERVE.get(annee, deb)
        if deb is None:
            print(f"{annee}  débourrement introuvable")
            continue
        if annee in VENDANGES and annee != 2026:
            obs[deb.isoformat()] = 9                # ancre de départ : les stades intermédiaires sont répartis entre débourrement et vendange
        bbch = phen.serie_bbch([r for r in rows if r["t"].year == annee], deb, TZ, obs or None)
        # on arrête la phénologie au premier jour à BBCH 87 (vendange champenoise)
        j89 = next((d for d in sorted(bbch) if bbch[d] >= 87), None)
        if j89:
            bbch = {d: v for d, v in bbch.items() if d <= j89}
        res = bo.calculer_saison([r for r in rows if r["t"].year == annee], bbch, TZ)

        def quand(seuil):
            j = next((d for d in sorted(bbch) if bbch[d] >= seuil), None)
            return j.strftime("%d/%m") if j else "  -  "
        cl = res["classification"] or {}
        pr = cl.get("probabilites") or {}
        print(f"{annee}{'*' if annee in VENDANGES else ' '}  {deb.strftime('%d/%m')}    {quand(53)}    {quand(73)}    {quand(79)}    {quand(87)}   "
              f"{res['sev1']:6.3f}  {res['sev2']:6.3f}  {res['sev3']:6.3f}   {cl.get('classe', '-'):<14}  "
              f"{pr.get('faible', 0):5.0%}  {pr.get('intermediaire', 0):5.0%}  {pr.get('severe', 0):5.0%}")
        for a in res["avertissements"]:
            print(f"        ! {a}")
        lignes_res.append((annee, res))

    if lignes_res and lignes_res[-1][0] == 2026:
        res = lignes_res[-1][1]
        print("\n2026 — jours de fenêtre 2 (BBCH 79-89) avec risque notable :")
        print("date    BBCH   T moy   HR   mouil.(h)   ris2      ris3")
        for d in res["jours"]:
            if d["fenetre"] == "2" and (d["ris2"] + d["ris3"]) > 0.005:
                print(f"{d['date'][5:]}   {d['bbch']:5.1f}  {d['tmoy']:5.1f}  {d['hr']:4.0f}   {d['wd']:5.1f}     {d['ris2']:.4f}    {d['ris3']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
