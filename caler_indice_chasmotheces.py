#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Calage de l'indice chasmothèces sur l'échelle des indices de début de saison 2012-2026 (référence de calage uniquement).

L'indice de début de saison N reflète la saison N-1. Pour chaque saison météo 2011-2025 (position régionale), deux composantes
purement climatiques, indépendantes de l'épidémie simulée sans traitement :
  C1  pression climatique de la saison : somme, de la floraison (BBCH >= 61) au 31 août, du potentiel d'infection journalier
      du moteur oidium.py (0-100) x sensibilité du feuillage au stade
  C2  conditions de formation des chasmothèces : à partir du 15 août, initiation après 8 h cumulées sous 13 °C, puis intégrale de
      la favorabilité thermique de Legler (courbe bêta du moteur) jusqu'au premier gel (T < 0 °C) ou au 15 novembre
Ajustement log-linéaire : log(indice) = k + a.log(C1) + b.log(C2), comparé aux modèles C1 seul et C2 seul.

L'archive Open-Meteo est téléchargée UNE fois (un appel par année) et gardée dans ~/archive_meteo/ : relancer ne coûte rien.

    cd ~/Matuscore && set -a && source .env && set +a
    EPIDEMIO_PATH=~/epidemio venv/bin/python3 caler_indice_chasmotheces.py
"""
import csv
import json
import math
import os
import sys
from datetime import date, datetime, timezone

import epidemio_pilot as ep

LAT, LON = 49.25, 3.96
DOSSIER = os.path.expanduser("~/archive_meteo")

# Indices de début de saison (lecture du graphe, échelle 0-140, 2026 = 95) : année de l'indice -> valeur
REFERENCE = {2012: 126, 2014: 36, 2015: 102, 2016: 77, 2017: 16, 2018: 68, 2019: 72, 2020: 72, 2021: 117, 2022: 68,
             2023: 77, 2024: 18, 2026: 95}


def archive(annee: int) -> list[dict]:
    """Lignes horaires de l'année (format Open-Meteo), depuis le cache local ou l'archive (un appel)."""
    os.makedirs(DOSSIER, exist_ok=True)
    chemin = os.path.join(DOSSIER, f"reims_{annee}.json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    ep.charger_moteur()
    import recuperer_meteo_horaire as rm
    data = rm._get_json(rm.url_archive(LAT, LON, date(annee, 1, 1), date(annee, 12, 31)))
    lignes = [{"time": t, **v} for t, v in sorted(rm._lignes(data).items())]
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(lignes, f)
    print(f"  archive {annee} téléchargée ({len(lignes)} heures)")
    return lignes


def pression_saison(res: dict, annee: int) -> float:
    """C1 : somme floraison -> 31 août du potentiel journalier x sensibilité du feuillage."""
    fin = date(annee, 8, 31).isoformat()
    total = 0.0
    for d in res["jours"]:
        if d["date"] <= fin and d.get("bbch") is not None and d["bbch"] >= 61:
            total += d["potentiel_pct"] * (d.get("sens_feuilles") or 1.0) / 100.0
    return total


def formation_automne(rows: list[dict], annee: int, oi) -> float:
    """C2 : intégrale de Legler après initiation par le froid, du 15 août au premier gel ou au 15 novembre."""
    p = ep.charger_oidium().PARAMS
    pc = p["chasmotheces"]
    debut = datetime(annee, 8, 15, tzinfo=timezone.utc)
    fin = datetime(annee, 11, 15, tzinfo=timezone.utc)
    froid, initie, integ = 0, False, 0.0
    for r in rows:
        if not (debut <= r["t"] < fin) or r["temp"] is None:
            continue
        T = r["temp"]
        if r["t"].month >= 10 and T < 0.0:                    # premier gel d'automne : chute des feuilles
            break
        if not initie:
            froid += T < pc["temperature_seuil_froid_c"]
            initie = froid >= pc["seuil_heures_froid"]
            continue
        integ += oi.taux_chasmotheces(T, p) / 24.0
    return integ


def resoudre(A, y):
    """Moindres carrés (équations normales) sans numpy."""
    n = len(A[0])
    M = [[sum(a[i] * a[j] for a in A) for j in range(n)] for i in range(n)]
    v = [sum(a[i] * yy for a, yy in zip(A, y)) for i in range(n)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[piv], v[i], v[piv] = M[piv], M[i], v[piv], v[i]
        for r in range(n):
            if r != i:
                f = M[r][i] / M[i][i]
                M[r] = [a - f * b for a, b in zip(M[r], M[i])]
                v[r] -= f * v[i]
    return [v[i] / M[i][i] for i in range(n)]


def rangs(x):
    ordre = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and x[ordre[j + 1]] == x[ordre[i]]:
            j += 1
        for k in range(i, j + 1):
            r[ordre[k]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x, y):
    rx, ry = rangs(x), rangs(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def main() -> int:
    oi = ep.charger_oidium()
    lignes_tab = []
    for annee_indice, cible in sorted(REFERENCE.items()):
        saison = annee_indice - 1
        try:
            lignes = archive(saison)
        except Exception as e:                                          # noqa: BLE001
            print(f"  {saison} : archive indisponible ({e})")
            continue
        # la série s'arrête le 31/12 à 20 h UTC : au-delà, l'heure de Paris bascule sur l'année suivante et le moteur prendrait
        # la mauvaise année de saison (débourrement introuvable)
        rows = [r for r in ep.lignes_vers_rows(lignes) if r["t"] < datetime(saison, 12, 31, 20, tzinfo=timezone.utc)]
        res = oi.calculer_saison(rows, {"primaire": {"indice_chasmotheces": 0}},
                                 now=datetime(saison, 12, 31, tzinfo=timezone.utc))
        c1, c2 = pression_saison(res, saison), formation_automne(rows, saison, oi)
        lignes_tab.append({"indice": annee_indice, "saison": saison, "cible": cible, "c1": c1, "c2": c2})
    ok = [l for l in lignes_tab if l["c1"] > 0 and l["c2"] > 0]
    if len(ok) < 5:
        print("Pas assez d'années exploitables.")
        return 1

    y = [math.log(l["cible"]) for l in ok]
    modeles = {
        "C1 seul": [[1.0, math.log(l["c1"])] for l in ok],
        "C2 seul": [[1.0, math.log(l["c2"])] for l in ok],
        "C1 x C2": [[1.0, math.log(l["c1"]), math.log(l["c2"])] for l in ok],
    }
    print(f"\nPosition {LAT}, {LON} — {len(ok)} saisons\n")
    print("Spearman (classement) avec l'indice de référence :")
    print(f"  C1 {spearman([l['c1'] for l in ok], [l['cible'] for l in ok]):+.2f}   "
          f"C2 {spearman([l['c2'] for l in ok], [l['cible'] for l in ok]):+.2f}   "
          f"C1xC2 {spearman([l['c1'] * l['c2'] for l in ok], [l['cible'] for l in ok]):+.2f}\n")
    preds = {}
    for nom, A in modeles.items():
        coef = resoudre(A, y)
        pr = [math.exp(sum(c * a for c, a in zip(coef, row))) for row in A]
        preds[nom] = pr
        eam = sum(abs(p - l["cible"]) for p, l in zip(pr, ok)) / len(ok)
        print(f"{nom:<8} coefficients {['%.3f' % c for c in coef]}   écart absolu moyen {eam:.1f} points   "
              f"Spearman {spearman(pr, [l['cible'] for l in ok]):+.2f}")
    print("\nindice  saison   référence      C1        C2     C1 seul  C2 seul  C1xC2")
    for i, l in enumerate(ok):
        print(f"{l['indice']}    {l['saison']}    {l['cible']:5.0f}   {l['c1']:8.2f}  {l['c2']:8.2f}   "
              f"{preds['C1 seul'][i]:6.0f}   {preds['C2 seul'][i]:6.0f}   {preds['C1 x C2'][i]:6.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
