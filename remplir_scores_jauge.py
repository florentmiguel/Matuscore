#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Calcule les scores de la jauge (mildiou, oïdium) et les écrit sur un bulletin déjà enregistré.

Utile une seule fois, pour le bulletin en cours au moment du déploiement : les bulletins suivants reçoivent leurs scores
automatiquement à l'enregistrement. Les scores sont calculés MAINTENANT (prévision du jour), pas à la date du bulletin.

    cd ~/Matuscore && set -a && source .env && set +a
    venv/bin/python3 remplir_scores_jauge.py            # dernier bulletin
    venv/bin/python3 remplir_scores_jauge.py --id 42    # un bulletin précis
    venv/bin/python3 remplir_scores_jauge.py --voir     # calcule et affiche, sans rien écrire
"""
import argparse
import sys

import serveur_vitisens as sv          # l'import applique aussi les migrations (colonnes score_mildiou / score_oidium)


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("--id", type=int, help="identifiant du bulletin (défaut : le dernier)")
    a.add_argument("--voir", action="store_true", help="afficher les scores sans les écrire")
    args = a.parse_args(argv)

    conn = sv.get_db()
    if args.id is None:
        row = conn.execute("SELECT id, date_av FROM bulletin_hebdo ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT id, date_av FROM bulletin_hebdo WHERE id=?", (args.id,)).fetchone()
    if not row:
        print("Aucun bulletin trouvé.")
        return 1
    bid, date_av = row[0], row[1]

    sm, so = sv._scores_jauge_bulletin()
    print(f"Bulletin {bid} ({date_av or 'sans date'}) : score mildiou = {sm}, score oïdium = {so}")
    if sm is None and so is None:
        print("Aucun score calculé (voir les messages [jauge] ci-dessus) : rien n'est écrit.")
        return 1
    if args.voir:
        return 0
    conn.execute("UPDATE bulletin_hebdo SET score_mildiou=?, score_oidium=? WHERE id=?", (sm, so, bid))
    conn.commit()
    conn.close()
    print("Écrit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
