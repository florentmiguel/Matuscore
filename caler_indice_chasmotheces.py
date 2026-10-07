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
from datetime import date, datetime, timedelta, timezone

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
    fin = min(date(annee, 12, 31), date.today() - timedelta(days=7))
    data = rm._get_json(rm.url_archive(LAT, LON, date(annee, 1, 1), fin))
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


def pression_periode(res: dict, debut: str, fin: str, sensibilite: bool) -> float:
    """Somme du potentiel journalier (0-1) entre deux dates MM-JJ, pondérée ou non par la sensibilité du feuillage."""
    total = 0.0
    for d in res["jours"]:
        if debut <= d["date"][5:] <= fin:
            f = (d.get("sens_feuilles") or 0.0) if sensibilite else 1.0
            total += d["potentiel_pct"] * f / 100.0
    return total


def hiver(lignes_saison: list[dict], lignes_suivante: list[dict], saison: int) -> tuple[float, float]:
    """(pluie mm, heures sous -5 °C) du 1er novembre de la saison au 31 mars de l'année suivante."""
    pluie, gel = 0.0, 0
    for l in lignes_saison + lignes_suivante:
        t = l["time"][:10]
        if f"{saison}-11-01" <= t <= f"{saison + 1}-03-31":
            pluie += l.get("precipitation") or 0.0
            gel += (l.get("temperature_2m") is not None and l["temperature_2m"] < -5.0)
    return pluie, float(gel)


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
        try:
            suivante = archive(saison + 1)
        except Exception as e:                                          # noqa: BLE001
            print(f"  {saison + 1} : archive indisponible ({e}), hiver ignoré")
            suivante = []
        ph, gh = hiver(lignes, suivante, saison)
        lignes_tab.append({"indice": annee_indice, "saison": saison, "cible": cible, "c1": c1, "c2": c2,
                           "p_jul_sep": pression_periode(res, "07-01", "09-30", True),
                           "p_aou_oct": pression_periode(res, "08-01", "10-31", False),
                           "pluie_hiver": ph, "gel_hiver": gh})
    ok = [l for l in lignes_tab if l["c2"] > 0]
    ref = [l["cible"] for l in ok]
    print(f"\nPosition {LAT}, {LON} — {len(ok)} saisons\n")
    print("Classement (Spearman) de chaque facteur avec l'indice de référence (hiver : on attend un signe négatif)")
    for k in ("c1", "p_jul_sep", "p_aou_oct", "c2", "pluie_hiver", "gel_hiver"):
        print(f"  {k:<12} {spearman([l[k] for l in ok], ref):+.2f}")
    combis = {
        "C2 x P(jul-sep)": lambda l: l["c2"] * l["p_jul_sep"],
        "C2 x P(aou-oct)": lambda l: l["c2"] * l["p_aou_oct"],
        "C2 / pluie hiver": lambda l: l["c2"] / max(1.0, l["pluie_hiver"]),
        "C2 x P(aou-oct) / pluie hiver": lambda l: l["c2"] * l["p_aou_oct"] / max(1.0, l["pluie_hiver"]),
        "C2 x P(jul-sep) / pluie hiver": lambda l: l["c2"] * l["p_jul_sep"] / max(1.0, l["pluie_hiver"]),
    }
    print("\nCombinaisons :")
    for nom, f in combis.items():
        print(f"  {nom:<32} {spearman([f(l) for l in ok], ref):+.2f}")
    print("\nindice saison  réf.   C1    P_jul-sep P_aou-oct   C2   pluie_hiv gel_hiv")
    for l in ok:
        print(f"{l['indice']}   {l['saison']}  {l['cible']:4.0f}  {l['c1']:6.1f}  {l['p_jul_sep']:7.1f}  {l['p_aou_oct']:7.1f}  "
              f"{l['c2']:6.1f}  {l['pluie_hiver']:7.0f}  {l['gel_hiver']:6.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
