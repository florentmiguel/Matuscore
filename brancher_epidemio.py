#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Branche le moteur épidémiologique sur Pilot (modifie serveur_vitisens.py)
=========================================================================

À lancer dans le dossier de Pilot (là où se trouve serveur_vitisens.py), une fois epidemio_pilot.py copié à côté :

    python3 brancher_epidemio.py            # branche
    python3 brancher_epidemio.py --retirer  # revient à la version d'avant (sauvegarde .avant_epidemio)

Trois modifications, chacune vérifiée avant d'écrire :
  1. avant la route /api/generer-texte-bulletin : import facultatif du module et enregistrement de la route
     /api/epidemio-moteur. Si l'import échoue, Pilot démarre quand même (repli sur l'ancien comportement) ;
  2. dans cette route : calcul de la synthèse du moteur ;
  3. dans son prompt : le bloc « SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE » devient celui du nouveau moteur pour le mildiou
     (l'oïdium et les fenêtres de traitement restent ceux de l'ancien modèle).

Si un point d'ancrage est introuvable (serveur_vitisens.py différent de la version attendue), RIEN n'est écrit.
Le script est idempotent : le relancer ne change rien. Le fichier modifié est recompilé avant d'être écrit.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

SAUVEGARDE = ".avant_epidemio"

ANCRE_ROUTE = "@app.route('/api/generer-texte-bulletin', methods=['GET'])\n"
ANCRE_APPEL = "        meteo_7j, synthese = _calculer_synthese_epidemio(lat, lon, maturite, receptive)\n"
ANCRE_PROMPT = "SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n{json.dumps(synthese, ensure_ascii=False, indent=2)}"

BLOC_IMPORT = '''# --- Moteur épidémiologique mildiou (dépôt epidemio) : facultatif, Pilot démarre sans lui ---
try:
    from epidemio_pilot import bp_epidemio, synthese_mildiou_pour_prompt, bloc_epidemio_prompt
    app.register_blueprint(bp_epidemio)
except Exception as _e_epidemio:
    print(f"[epidemio] moteur non branché : {_e_epidemio}")

    def synthese_mildiou_pour_prompt(lat, lon):
        return None

    def bloc_epidemio_prompt(synthese_ancienne, moteur):
        return "SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\\n" + json.dumps(synthese_ancienne, ensure_ascii=False, indent=2)

'''

LIGNES_APPEL = '''        moteur = synthese_mildiou_pour_prompt(lat, lon)  # None si le moteur est indisponible
        bloc_epidemio = bloc_epidemio_prompt(synthese, moteur)
'''


def modifier(src: str) -> str:
    """Source modifiée, ou ValueError si un point d'ancrage manque ou est ambigu."""
    if "epidemio_pilot" in src:
        return src
    for nom, ancre in (("route /api/generer-texte-bulletin", ANCRE_ROUTE), ("appel de la synthèse", ANCRE_APPEL),
                       ("bloc du prompt", ANCRE_PROMPT)):
        n = src.count(ancre)
        if n != 1:
            raise ValueError(f"point d'ancrage « {nom} » trouvé {n} fois (attendu : 1)")
    src = src.replace(ANCRE_ROUTE, BLOC_IMPORT + ANCRE_ROUTE, 1)
    src = src.replace(ANCRE_APPEL, ANCRE_APPEL + LIGNES_APPEL, 1)
    src = src.replace(ANCRE_PROMPT, "{bloc_epidemio}", 1)
    compile(src, "serveur_vitisens.py", "exec")                       # SyntaxError si la modification casse le fichier
    return src


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Branche le moteur épidémiologique sur Pilot")
    ap.add_argument("serveur", nargs="?", default="serveur_vitisens.py")
    ap.add_argument("--retirer", action="store_true", help="restaure la version d'avant le branchement")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.serveur):
        print(f"Introuvable : {a.serveur} (lance le script dans le dossier de Pilot)")
        return 1
    sauvegarde = a.serveur + SAUVEGARDE

    if a.retirer:
        if not os.path.isfile(sauvegarde):
            print(f"Pas de sauvegarde ({sauvegarde}) : rien à restaurer.")
            return 1
        shutil.copy2(sauvegarde, a.serveur)
        print(f"Restauré depuis {sauvegarde}. Redémarre le service pour appliquer.")
        return 0

    with open(a.serveur, encoding="utf-8", newline="") as f:
        src = f.read()
    if "epidemio_pilot" in src:
        print("Déjà branché : aucune modification.")
        return 0
    try:
        nouveau = modifier(src)
    except (ValueError, SyntaxError) as e:
        print(f"Branchement annulé, rien n'a été écrit : {e}")
        print("Le serveur diffère de la version attendue : envoie-moi serveur_vitisens.py pour adapter le script.")
        return 1
    if not os.path.exists(sauvegarde):
        shutil.copy2(a.serveur, sauvegarde)
    with open(a.serveur, "w", encoding="utf-8", newline="") as f:
        f.write(nouveau)
    print(f"Branché. Sauvegarde : {sauvegarde}")
    print("  + route GET /api/epidemio-moteur")
    print("  + génération de texte du bulletin alimentée par le moteur pour le mildiou")
    print("Redémarre le service : sudo systemctl restart matuscore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
