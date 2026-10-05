#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Branche le moteur épidémiologique sur Pilot
===========================================

À lancer dans le dossier de Pilot (là où se trouvent serveur_vitisens.py, generer_bulletins_v4.py, dashboard_v2.html et
portail_client.html), une fois epidemio_pilot.py copié à côté :

    python3 brancher_epidemio.py            # branche (ou met à niveau une version déjà branchée)
    python3 brancher_epidemio.py --retirer  # revient à la version d'avant (sauvegardes .avant_epidemio)

Ce que le script modifie, fichier par fichier :
  serveur_vitisens.py     1. « moteur » : route /api/epidemio-moteur et mildiou du prompt de l'IA tiré du moteur ;
                          2. « style »  : la forme des analyses est dictée par epidemio_pilot.py (titre, paragraphes) ;
                          3. « communes » : chaque bulletin client reçoit un bloc « situation sur votre commune ».
  generer_bulletins_v4.py  rend l'analyse en titre + paragraphes (si elle contient des retours à la ligne), ajoute le bloc
                          commune, nomme la recommandation « Recommandation » (et non plus « Recommandation Comité Champagne »)
                          et tire du moteur le risque du tableau « Prévisions météo ». Sans retour à la ligne ni moteur,
                          le rendu historique est strictement conservé (hors libellé).
  dashboard_v2.html       aperçus du bulletin : les retours à la ligne des analyses sont respectés.
  portail_client.html     idem pour le portail du vigneron.

Garanties :
  * chaque fichier est TOUT OU RIEN : si un point d'ancrage manque ou est ambigu (fichier différent de la version attendue),
    ce fichier n'est pas touché et le script le dit ; les autres fichiers sont traités ;
  * idempotent : le relancer ne change rien ; il met aussi à niveau un serveur branché avec une version précédente ;
  * les fichiers Python modifiés sont recompilés avant d'être écrits ;
  * sans retour à la ligne dans les textes et sans bloc commune, les bulletins sont rendus comme avant (seul le libellé change).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

SAUVEGARDE = ".avant_epidemio"

# ---------------------------------------------------------------------------
# serveur_vitisens.py
# ---------------------------------------------------------------------------
S_ROUTE = "@app.route('/api/generer-texte-bulletin', methods=['GET'])\n"
S_APPEL = "        meteo_7j, synthese = _calculer_synthese_epidemio(lat, lon, maturite, receptive)\n"
S_PROMPT = "SYNTHÈSE DU MODÈLE ÉPIDÉMIOLOGIQUE :\n{json.dumps(synthese, ensure_ascii=False, indent=2)}"
S_STYLE = ("un texte court pour chacun des 5 champs suivants, dans le style suivant : phrases courtes, techniques, factuelles, "
           "sans emphase ni formules commerciales.")
S_STYLE_NOUVEAU = "un texte pour chacun des 5 champs suivants, en respectant la CONSIGNE DE RÉDACTION ci-dessus."

MARQUEUR_MOTEUR = "from epidemio_pilot import bp_epidemio"
MARQUEUR_STYLE = "CONSIGNE DE RÉDACTION ci-dessus"
MARQUEUR_COMMUNES = "_build_bulletin_sans_commune"

BLOC_MOTEUR = '''# --- Moteur épidémiologique mildiou (dépôt epidemio) : facultatif, Pilot démarre sans lui ---
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

BLOC_COMMUNES = '''# --- Risque par commune de client : bloc ajouté à chaque bulletin. Sans moteur, le bulletin reste exactement tel qu'avant. ---
try:
    from epidemio_pilot import bloc_commune_pour_client as _bloc_commune_pour_client
except Exception as _e_epidemio_communes:
    _bloc_commune_pour_client = None
_build_bulletin_sans_commune = _build_bulletin


def _build_bulletin(client, av, prescriptions, suivi, meteo):
    bloc = None
    if _bloc_commune_pour_client:
        try:
            bloc = _bloc_commune_pour_client(client)
        except Exception as _e:
            print(f"[epidemio] bloc commune ignoré : {_e}")
    if bloc:
        client = {**client, "_epidemio_commune": bloc}
    return _build_bulletin_sans_commune(client, av, prescriptions, suivi, meteo)

'''


def _une_fois(src: str, ancre: str, nom: str):
    n = src.count(ancre)
    if n != 1:
        raise ValueError(f"point d'ancrage « {nom} » trouvé {n} fois (attendu : 1)")


def modifier_serveur(src: str) -> tuple[str, list[str]]:
    etapes = []
    if MARQUEUR_MOTEUR not in src:
        for ancre, nom in ((S_ROUTE, "route /api/generer-texte-bulletin"), (S_APPEL, "appel de la synthèse"),
                           (S_PROMPT, "bloc du prompt")):
            _une_fois(src, ancre, nom)
        src = src.replace(S_ROUTE, BLOC_MOTEUR + S_ROUTE, 1).replace(S_APPEL, S_APPEL + LIGNES_APPEL, 1)
        src = src.replace(S_PROMPT, "{bloc_epidemio}", 1)
        etapes.append("moteur")
    if MARQUEUR_STYLE not in src:
        _une_fois(src, S_STYLE, "consigne de style du prompt")
        src = src.replace(S_STYLE, S_STYLE_NOUVEAU, 1)
        etapes.append("style")
    if MARQUEUR_COMMUNES not in src:
        _une_fois(src, S_ROUTE, "route /api/generer-texte-bulletin")
        _une_fois(src, "build_bulletin as _build_bulletin", "import de build_bulletin")
        src = src.replace(S_ROUTE, BLOC_COMMUNES + S_ROUTE, 1)
        etapes.append("communes")
    if etapes:
        compile(src, "serveur_vitisens.py", "exec")
    return src, etapes


# ---------------------------------------------------------------------------
# generer_bulletins_v4.py
# ---------------------------------------------------------------------------
G_DEF = "def build_bulletin(client, av, prescriptions, suivi, meteo_days):\n"
G_MILDIOU = '    multi_para(doc, [\n        {"t": "Analyse du risque : ", "b": True, "c": OR}, f"{risque} ",\n    ])\n'
G_OIDIUM = '    multi_para(doc, [\n        {"t": "Analyse régionale : ", "b": True}, f"{risque_o}. ",\n    ])\n'
G_CONCLUSION = "        if pluie >= 2 and temp >= 11:\n"
G_PARCELLES = '    if client.get("parcelles_mildiou"):\n'
MARQUEUR_GENERATEUR = "def analyse_en_paragraphes("
G_LIBELLE = "Recommandation Comité Champagne : "
G_LIBELLE_NOUVEAU = "Recommandation : "
G_SECTION4 = '    section_heading(doc, "4", "PRÉVISIONS MÉTÉO — 7 JOURS", BL)\n'
G_PAS_DE_RISQUE = '"Aucun jour ne réunit pluie ≥ 2 mm + T° moy ≥ 11°C.", GBG, GM)'
MARQUEUR_TABLEAU = '.get("risque_jours")'
G_RISQUE_JOURS = '''    _rj = (client.get("_epidemio_commune") or {}).get("risque_jours")
    if _rj and meteo_days:
        meteo_days = [dict(d, risk=_rj.get(d["date"], d["risk"])) for d in meteo_days]     # copie : la météo est partagée entre clients
'''
G_PAS_DE_RISQUE_NOUVEAU = ('"Aucune infection significative n\'est attendue sur votre commune d\'après la météo." if _rj '
                           'else "Aucun jour ne réunit pluie ≥ 2 mm + T° moy ≥ 11°C.", GBG, GM)')

G_HELPER = '''def analyse_en_paragraphes(doc, etiquette, texte, couleur, suffixe=" "):
    """Texte d'analyse du bulletin. Sans retour à la ligne : rendu historique, étiquette en tête. Avec retours à la ligne : la
    première ligne est le titre (courte, sans point final), chaque ligne suivante est un paragraphe."""
    blocs = [b.strip() for b in (texte or "").replace("\\r", "").split("\\n") if b.strip()]
    if len(blocs) <= 1:
        etiq = {"t": etiquette, "b": True}
        if couleur:
            etiq["c"] = couleur                          # pas de couleur indiquée = couleur par défaut, comme avant
        multi_para(doc, [etiq, f"{texte}{suffixe}"])
        return
    titre, paragraphes = blocs[0], blocs[1:]
    if len(titre) > 90 or titre.endswith("."):          # pas de titre : tout est paragraphe
        titre, paragraphes = None, blocs
    if titre:
        styled_para(doc, titre, bold=True, color=couleur or BK, size=10, align=WD_ALIGN_PARAGRAPH.LEFT, sb=4, sa=2)
    for b in paragraphes:
        styled_para(doc, b, size=10)


'''
G_CONCLUSION_NOUVELLE = ('        if client.get("_epidemio_commune"):\n'
                         '            pass  # la conclusion vient du risque calculé pour la commune, plus bas\n'
                         '        elif pluie >= 2 and temp >= 11:\n')
G_BLOC_COMMUNE = '''    bloc_commune = client.get("_epidemio_commune")
    if bloc_commune:
        multi_para(doc, [{"t": bloc_commune["titre"], "b": True, "c": GD}, bloc_commune["texte"]])

'''


def modifier_generateur(src: str) -> tuple[str, list[str]]:
    """Étapes indépendantes (chacune a son marqueur) : « paragraphes » et « commune » (version précédente), puis « libellé »
    (« Recommandation » sans « Comité Champagne ») et « tableau » (risque du tableau des prévisions tiré du moteur)."""
    etapes = []
    if MARQUEUR_GENERATEUR not in src:
        for ancre, nom in ((G_DEF, "def build_bulletin"), (G_MILDIOU, "analyse mildiou"), (G_OIDIUM, "analyse oïdium"),
                           (G_CONCLUSION, "conclusion de la situation parcellaire"), (G_PARCELLES, "parcelles sensibles")):
            _une_fois(src, ancre, nom)
        src = src.replace(G_DEF, G_HELPER + G_DEF, 1)
        src = src.replace(G_MILDIOU, '    analyse_en_paragraphes(doc, "Analyse du risque : ", risque, OR)\n', 1)
        src = src.replace(G_OIDIUM, '    analyse_en_paragraphes(doc, "Analyse régionale : ", risque_o, None, ". ")\n', 1)
        src = src.replace(G_CONCLUSION, G_CONCLUSION_NOUVELLE, 1)
        src = src.replace(G_PARCELLES, G_BLOC_COMMUNE + G_PARCELLES, 1)
        etapes += ["paragraphes", "commune"]
    if G_LIBELLE in src:
        n = src.count(G_LIBELLE)
        if n != 2:
            raise ValueError(f"libellé « {G_LIBELLE.strip(' :')} » trouvé {n} fois (attendu : 2, mildiou et oïdium)")
        src = src.replace(G_LIBELLE, G_LIBELLE_NOUVEAU)
        etapes.append("libellé")
    if MARQUEUR_TABLEAU not in src:
        _une_fois(src, G_SECTION4, "titre de la section prévisions météo")
        _une_fois(src, G_PAS_DE_RISQUE, "phrase « pas de risque mildiou »")
        src = src.replace(G_SECTION4, G_SECTION4 + G_RISQUE_JOURS, 1)
        src = src.replace(G_PAS_DE_RISQUE, G_PAS_DE_RISQUE_NOUVEAU, 1)
        etapes.append("tableau")
    if etapes:
        compile(src, "generer_bulletins_v4.py", "exec")
    return src, etapes


# ---------------------------------------------------------------------------
# dashboard_v2.html et portail_client.html : respecter les retours à la ligne des analyses
# ---------------------------------------------------------------------------
BR = ".replace(/\\n/g,'<br>')"
H_DASHBOARD = (("${b.risque_mildiou||'—'}", "${(b.risque_mildiou||'—')%s}" % BR),
               ("${b.risque_oidium||'—'}", "${(b.risque_oidium||'—')%s}" % BR),
               ("${av.risque_mildiou||'?'}", "${(av.risque_mildiou||'?')%s}" % BR),
               ("${av.risque_oidium||'?'}", "${(av.risque_oidium||'?')%s}" % BR))
H_PORTAIL = (("<br>${b.risque_mildiou}</div>", "<br>${(b.risque_mildiou||'')%s}</div>" % BR),
             ("<br>${b.risque_oidium}</div>", "<br>${(b.risque_oidium||'')%s}</div>" % BR))


def _modifier_html(src: str, remplacements) -> tuple[str, list[str]]:
    if all(nouveau in src for _, nouveau in remplacements):
        return src, []
    for ancien, _ in remplacements:
        _une_fois(src, ancien, ancien)
    for ancien, nouveau in remplacements:
        src = src.replace(ancien, nouveau, 1)
    return src, ["retours à la ligne"]


def modifier_dashboard(src: str) -> tuple[str, list[str]]:
    return _modifier_html(src, H_DASHBOARD)


def modifier_portail(src: str) -> tuple[str, list[str]]:
    return _modifier_html(src, H_PORTAIL)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
FICHIERS = (("serveur_vitisens.py", modifier_serveur), ("generer_bulletins_v4.py", modifier_generateur),
            ("dashboard_v2.html", modifier_dashboard), ("portail_client.html", modifier_portail))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Branche le moteur épidémiologique sur Pilot")
    ap.add_argument("dossier", nargs="?", default=".", help="dossier de Pilot (défaut : dossier courant)")
    ap.add_argument("--retirer", action="store_true", help="restaure les versions d'avant le branchement")
    a = ap.parse_args(argv)
    dossier = os.path.dirname(os.path.abspath(a.dossier)) if os.path.isfile(a.dossier) else a.dossier

    if not os.path.isfile(os.path.join(dossier, "serveur_vitisens.py")):
        print(f"Introuvable : {os.path.join(dossier, 'serveur_vitisens.py')} (lance le script dans le dossier de Pilot)")
        return 1

    code = 0
    for nom, modifier in FICHIERS:
        chemin = os.path.join(dossier, nom)
        sauvegarde = chemin + SAUVEGARDE
        if not os.path.isfile(chemin):
            print(f"  {nom:<26} absent : ignoré")
            continue
        if a.retirer:
            if os.path.isfile(sauvegarde):
                shutil.copy2(sauvegarde, chemin)
                print(f"  {nom:<26} restauré")
            else:
                print(f"  {nom:<26} pas de sauvegarde : inchangé")
            continue
        with open(chemin, encoding="utf-8", newline="") as f:
            src = f.read()
        try:
            nouveau, etapes = modifier(src)
        except (ValueError, SyntaxError) as e:
            print(f"  {nom:<26} REFUSÉ, rien n'a été écrit : {e}")
            code = 1
            continue
        if not etapes:
            print(f"  {nom:<26} déjà à jour")
            continue
        if not os.path.exists(sauvegarde):
            shutil.copy2(chemin, sauvegarde)
        with open(chemin, "w", encoding="utf-8", newline="") as f:
            f.write(nouveau)
        print(f"  {nom:<26} modifié ({', '.join(etapes)})")

    if code:
        print("\nUn fichier diffère de la version attendue : envoie-moi-le pour adapter le script. Les autres sont à jour.")
    elif a.retirer:
        print("\nRedémarre le service pour appliquer : sudo systemctl restart matuscore")
    else:
        print("\nRedémarre le service : sudo systemctl restart matuscore")
    return code


if __name__ == "__main__":
    sys.exit(main())
