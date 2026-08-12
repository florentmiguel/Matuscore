#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parseur de la "Fiche vendange" (Comité Champagne) au format PDF.

La fiche liste, par mode de faire-valoir (fermage/métayage) puis par commune,
les parcelles cadastrales du domaine : lieu-dit, référence cadastrale, cépage,
année de plantation, écartements ("Densité Rang"/"Densité Pied", en cm) et
superficie plantée (colonnes Ha / A / Ca).

Ce module :
  1. Extrait chaque ligne cadastrale (commune, lieu-dit, cépage, écartements,
     surface) en se basant sur la position (x, y) des mots dans le PDF plutôt
     que sur le texte brut, car le document ne trace aucune bordure de cellule
     et les valeurs de commune/lieu-dit/référence cadastrale ne sont répétées
     qu'à leur première occurrence (report implicite pour les lignes suivantes,
     y compris à travers un saut de page).
  2. Fusionne les lignes cadastrales contiguës qui appartiennent au même
     lieu-dit (valeur non ré-affichée = report) ET au même cépage : c'est ce
     que le vigneron considère comme "une parcelle", même si elle est
     composée de plusieurs références cadastrales. Le détail cadastral n'est
     pas conservé après fusion (uniquement gardé en `refs_cadastrales` pour
     information/debug).

Utilisation :
    from fiche_vendange_parser import parser_fiche_vendange
    parcelles = parser_fiche_vendange("/chemin/vers/fiche.pdf")
    # -> liste de dicts: commune, lieu_dit, nom, cepage, surface_ha,
    #    ecart_rangs, ecart_ceps, nb_pieds_ha, refs_cadastrales
"""
import re

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

ANNEE_RE = re.compile(r'^(19|20)\d{2}$')
NUM_RE = re.compile(r'^\d{1,3}$')
CODE_COMMUNE_RE = re.compile(r'^5\d{4}$')

# Bornes de colonnes (en points PDF), déterminées empiriquement sur le
# gabarit standard de la fiche vendange Comité Champagne (page en paysage,
# 841x595 pt). Robuste à quelques points de jitter typographique.
COL_CODE_COMMUNE = (0, 45)
COL_COMMUNE = (45, 195)
COL_LIEU_DIT = (195, 305)
COL_REF_CAD = (305, 375)
COL_SURFACE = (375, 417)
COL_CEPAGE = (430, 515)
COL_ANNEE = (515, 535)
COL_RANG = (605, 630)
COL_PIED = (630, 658)

TOP_MIN, TOP_MAX = 195, 580  # exclut l'en-tête de page et le pied de page


def _cluster_lines(words, tol=1.6):
    """Regroupe les mots en lignes visuelles par proximité verticale (top)."""
    ws = sorted(words, key=lambda w: (w['top'], w['x0']))
    lines, cur, cur_top = [], [], None
    for w in ws:
        if cur and abs(w['top'] - cur_top) > tol:
            lines.append(cur)
            cur = []
        cur.append(w)
        cur_top = sum(x['top'] for x in cur) / len(cur)
    if cur:
        lines.append(cur)
    return lines


def _in(x0, bounds):
    return bounds[0] <= x0 < bounds[1]


def _extract_raw_rows(pdf_path):
    """Passe 1 : identifie les lignes-données ("ancres") et leurs colonnes
    numériques/texte simples (commune, référence cadastrale, cépage, année,
    écartements, surface). Le lieu-dit est traité séparément (passe 2) car
    il peut être réparti sur plusieurs lignes visuelles autour de l'ancre."""
    all_rows = []
    with pdfplumber.open(pdf_path) as pdf:
        skip_mode = False  # à l'intérieur d'un bloc "Total ... / nom bailleur"
        for pageno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
            words = [w for w in words if TOP_MIN < w['top'] < TOP_MAX]
            for line in _cluster_lines(words):
                line_sorted = sorted(line, key=lambda w: w['x0'])
                texts_by_x = [(w['x0'], w['text']) for w in line_sorted]

                if line_sorted[0]['text'] == 'Total':
                    skip_mode = True
                    continue

                has_code_commune = any(CODE_COMMUNE_RE.match(t) and x0 < COL_CODE_COMMUNE[1]
                                        for x0, t in texts_by_x)
                has_anchor = any(ANNEE_RE.match(t) and _in(x0, COL_ANNEE) for x0, t in texts_by_x)

                if skip_mode:
                    if has_code_commune or has_anchor:
                        skip_mode = False
                    else:
                        continue

                if not has_anchor:
                    continue  # ligne de report (porte-greffe multi-lignes, etc.)

                code_commune_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_CODE_COMMUNE))
                commune_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_COMMUNE))
                ref_cad_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_REF_CAD))
                surf_nums = [t for x0, t in texts_by_x if _in(x0, COL_SURFACE) and NUM_RE.match(t)]
                cepage_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_CEPAGE))
                rang_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_RANG))
                pied_txt = ' '.join(t for x0, t in texts_by_x if _in(x0, COL_PIED))

                all_rows.append({
                    'page': pageno, 'top': line_sorted[0]['top'],
                    'code_commune_raw': code_commune_txt or None,
                    'commune_raw': commune_txt or None,
                    'ref_cad_raw': ref_cad_txt or None,
                    'surf_nums': surf_nums,
                    'cepage': cepage_txt,
                    'rang': rang_txt,
                    'pied': pied_txt,
                })
    return all_rows


def _attach_lieu_dit(pdf_path, all_rows):
    """Passe 2 : le lieu-dit peut s'étaler sur 2 lignes visuelles centrées
    autour de la ligne-donnée (ex : porte-greffe sur 2 lignes qui pousse le
    texte du lieu-dit au-dessus ET en dessous de l'ancre). On rattache donc
    chaque mot de la colonne "lieu-dit" à l'ancre la plus proche verticalement
    plutôt qu'à une ligne précise."""
    rows_by_page = {}
    for r in all_rows:
        rows_by_page.setdefault(r['page'], []).append(r)

    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            anchors = rows_by_page.get(pageno, [])
            if not anchors:
                continue
            words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
            words = [w for w in words if TOP_MIN < w['top'] < TOP_MAX and _in(w['x0'], COL_LIEU_DIT)]
            anchor_tops = [a['top'] for a in anchors]
            for a in anchors:
                a['_lieu_dit_frag'] = []
            for w in words:
                best_i, best_d = None, 1e9
                for i, t in enumerate(anchor_tops):
                    d = abs(w['top'] - t)
                    if d < best_d:
                        best_d, best_i = d, i
                if best_d < 20:  # au-delà : probablement un bloc "Total" voisin
                    anchors[best_i]['_lieu_dit_frag'].append((w['top'], w['x0'], w['text']))

    for r in all_rows:
        frags = sorted(r.pop('_lieu_dit_frag', []), key=lambda f: (f[0], f[1]))
        r['lieu_dit_raw'] = ' '.join(f[2] for f in frags) or None
    return all_rows


def _forward_fill_and_parse(all_rows):
    """Report des valeurs (commune / lieu-dit / référence cadastrale) sur les
    lignes où elles ne sont pas ré-affichées, y compris à travers les sauts
    de page ; conversion de la surface (Ha/A/Ca) en hectares décimaux."""
    cur_commune = cur_lieu_dit = cur_ref_cad = None
    parsed = []
    for r in all_rows:
        if r['commune_raw']:
            cur_commune = r['commune_raw']
        if r['lieu_dit_raw']:
            cur_lieu_dit = r['lieu_dit_raw']
        if r['ref_cad_raw']:
            cur_ref_cad = r['ref_cad_raw']

        nums = r['surf_nums']
        if len(nums) >= 2:
            ha, a, ca = (int(nums[-3]) if len(nums) >= 3 else 0), int(nums[-2]), int(nums[-1])
        elif len(nums) == 1:
            ha, a, ca = 0, 0, int(nums[0])
        else:
            ha, a, ca = 0, 0, 0
        surface_ha = ha + a / 100 + ca / 10000

        parsed.append({
            'commune': cur_commune,
            'lieu_dit_explicit': bool(r['lieu_dit_raw']),
            'lieu_dit': cur_lieu_dit,
            'ref_cad': cur_ref_cad,
            'cepage': r['cepage'],
            'surface_ha': round(surface_ha, 4),
            'rang_cm': float(r['rang']) if r['rang'] else None,
            'pied_cm': float(r['pied']) if r['pied'] else None,
        })
    return parsed


def _merge_parcelles(rows):
    """Fusionne les lignes cadastrales contiguës appartenant au même
    lieu-dit (valeur reportée, non ré-affichée) et au même cépage. Une
    nouvelle parcelle démarre si le lieu-dit est explicitement ré-affiché,
    si la commune change, ou si le cépage diffère."""
    parcelles, cur = [], None
    for r in rows:
        starts_new = (
            cur is None
            or r['lieu_dit_explicit']
            or r['commune'] != cur['commune']
            or r['cepage'] != cur['cepage']
        )
        if starts_new:
            if cur:
                parcelles.append(cur)
            cur = {
                'commune': r['commune'], 'lieu_dit': r['lieu_dit'], 'cepage': r['cepage'],
                'surface_ha': 0.0, '_wsum': [], 'refs_cadastrales': [],
            }
        cur['surface_ha'] += r['surface_ha']
        cur['_wsum'].append((r['surface_ha'], r['rang_cm'], r['pied_cm']))
        if r['ref_cad'] not in cur['refs_cadastrales']:
            cur['refs_cadastrales'].append(r['ref_cad'])
    if cur:
        parcelles.append(cur)

    for p in parcelles:
        tot_s = sum(x[0] for x in p['_wsum']) or 1
        rang = sum(x[0] * x[1] for x in p['_wsum'] if x[1] is not None) / tot_s
        pied = sum(x[0] * x[2] for x in p['_wsum'] if x[2] is not None) / tot_s
        p['ecart_rangs'] = round(rang / 100, 2) if rang else None
        p['ecart_ceps'] = round(pied / 100, 2) if pied else None
        p['nb_pieds_ha'] = (round(10000 / (p['ecart_rangs'] * p['ecart_ceps']))
                             if p['ecart_rangs'] and p['ecart_ceps'] else None)
        p['surface_ha'] = round(p['surface_ha'], 4)
        p['nom'] = p['lieu_dit']
        del p['_wsum']
    return parcelles


def parser_fiche_vendange(pdf_path):
    """Point d'entrée : parse une fiche vendange Comité Champagne (PDF) et
    retourne la liste des parcelles fusionnées, prêtes à être proposées à
    l'import (commune, lieu_dit, nom, cepage, surface_ha, ecart_rangs,
    ecart_ceps, nb_pieds_ha, refs_cadastrales)."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber n'est pas installé (pip install pdfplumber)")
    rows = _extract_raw_rows(pdf_path)
    rows = _attach_lieu_dit(pdf_path, rows)
    rows = _forward_fill_and_parse(rows)
    return _merge_parcelles(rows)


if __name__ == '__main__':
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python fiche_vendange_parser.py chemin.pdf")
        sys.exit(1)
    result = parser_fiche_vendange(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n{len(result)} parcelles extraites — surface totale : "
          f"{sum(p['surface_ha'] for p in result):.4f} ha")
