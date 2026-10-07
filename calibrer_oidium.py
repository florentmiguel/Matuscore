#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Distribution des nouvelles colonies d'oïdium (moteur oidium.py v1) sur la saison 2026, pour caler la jauge de pression réelle.
Lecture seule : n'écrit rien en base.

    cd ~/Matuscore && set -a && source .env && set +a
    EPIDEMIO_PATH=~/epidemio venv/bin/python3 calibrer_oidium.py              # position régionale (Reims)
    EPIDEMIO_PATH=~/epidemio venv/bin/python3 calibrer_oidium.py 49.13 4.16   # autre position
"""
import math
import sys
from datetime import datetime, timezone

import epidemio_pilot as ep

lat, lon = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (49.25, 3.96)
now = datetime.now(timezone.utc)
lignes, infos = ep.serie_horaire(lat, lon, now)
res = ep.charger_oidium().calculer_saison(ep.lignes_vers_rows(lignes), ep.params_oidium(), now=now)
jours = [d for d in res["jours"] if d["date"].startswith("2026")]
print(f"Position {lat}, {lon} — {len(jours)} jours 2026 — données : {res['donnees']}")
print(f"Jalons : {res.get('jalons')}\n")

print("semaine (début)   nouv. colonies 7 j   log10   potentiel moy.   fraction malade fin")
sommes = []
for i in range(0, len(jours) - 6, 7):
    s = jours[i:i + 7]
    tot = sum(d["nouvelles_colonies"] for d in s)
    sommes.append(tot)
    pot = sum(d["potentiel_pct"] for d in s) / 7
    print(f"{s[0]['date']}        {tot:14.6g}   {math.log10(tot) if tot > 0 else float('-inf'):6.2f}   "
          f"{pot:8.1f}        {s[-1]['fraction_malade']:.6g}")

pos = sorted(x for x in sommes if x > 0)
if pos:
    def pct(q):
        return pos[min(len(pos) - 1, int(q * len(pos)))]
    print(f"\nSemaines > 0 : {len(pos)}/{len(sommes)}   P10 {pct(.10):.3g}   P50 {pct(.50):.3g}   P90 {pct(.90):.3g}   "
          f"P95 {pct(.95):.3g}   max {pos[-1]:.3g}")
