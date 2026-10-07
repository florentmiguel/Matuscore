#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Calcule chaque jour la jauge de pression (mildiou : mildiou_primaire.py, oïdium : oidium.py v1) de chaque client, pour SA commune, et l'enregistre dans la table
jauge_client que lit le portail. Aucune valeur régionale : un client non localisable n'a pas de score (le portail l'indique).

    cd ~/Matuscore && set -a && source .env && set +a
    venv/bin/python3 calculer_jauges.py           # calcule et enregistre
    venv/bin/python3 calculer_jauges.py --voir    # calcule et affiche, sans rien écrire

Tâche planifiée (crontab -e), tous les jours à 6 h :
    0 6 * * * cd /home/ubuntu/Matuscore && set -a && . ./.env && set +a && venv/bin/python3 calculer_jauges.py >> /home/ubuntu/jauges.log 2>&1
"""
import argparse
import sys
from datetime import datetime

import epidemio_pilot as ep
import serveur_vitisens as sv          # l'import crée la table jauge_client si elle manque

COLONNES = ("id_client", "commune", "source_position", "score_mildiou", "tendance_mildiou", "score_oidium", "tendance_oidium",
            "calcule_le")


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description="Jauge de pression par commune de client")
    a.add_argument("--voir", action="store_true", help="afficher sans enregistrer")
    args = a.parse_args(argv)

    debut = datetime.now()
    lignes = ep.jauges_clients(ep.lire_clients(sv.DB_PATH))
    for l in lignes:
        print(f"{l['id_client']:<12} {(l['commune'] or '?'):<24} "
              f"mildiou {l['score_mildiou'] if l['score_mildiou'] is not None else '—':>3} ({l['tendance_mildiou'] or '-'})  "
              f"oïdium {l['score_oidium'] if l['score_oidium'] is not None else '—':>3} ({l['tendance_oidium'] or '-'})"
              + ("" if l["source_position"] else "  NON LOCALISÉ : renseigner latitude et longitude dans la fiche"))
    if args.voir:
        return 0
    conn = sv.get_db()
    with conn:                                      # transaction : la table n'est jamais à moitié remplie
        conn.execute("DELETE FROM jauge_client")
        conn.executemany(f"INSERT INTO jauge_client ({', '.join(COLONNES)}) VALUES ({', '.join('?' * len(COLONNES))})",
                         [tuple(l[c] for c in COLONNES) for l in lignes])
    conn.close()
    print(f"{debut:%Y-%m-%d %H:%M} : {len(lignes)} client(s) enregistré(s) en {(datetime.now() - debut).seconds} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
