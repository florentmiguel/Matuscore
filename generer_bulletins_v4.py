#!/usr/bin/env python3
"""
VITI Sens — Générateur de bulletins v4
======================================
Structure allégée :
- Bannière titre avec exploitation/commune/date/interlocuteur/certification
- Section 1 : Phénologie (texte rédigé depuis AV + données client)
- Section 2 : Mildiou (texte rédigé depuis AV + données client)
- Section 3 : Oïdium (texte rédigé depuis AV + données client)
- Section 4 : Météo 7 jours (Open-Meteo)
- Section 5 : Programme phytosanitaire (prescriptions)
- Section optionnelle : infos complémentaires (si renseignée)

Usage:
    python generer_bulletins_v4.py VITI_Sens_Base_Donnees_2026.xlsx
"""

import os, sys, re
from datetime import datetime
from openpyxl import load_workbook
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

COORDS = {"lat": 49.25, "lon": 3.96}
TODAY = datetime.now().strftime("%d/%m/%Y")

GD="2D6A4F"; GM="40916C"; GL="B7E4C7"; GBG="F0F7F0"
OR="E76F51"; OBG="FFF3E0"; RD="C1121F"; RBG="FFE0E0"
BL="2196F3"; BBG="E3F2FD"; GR="6B7280"; YBG="FFFDE7"
WH="FFFFFF"; BK="333333"

def rgb(h): return RGBColor(int(h[:2],16),int(h[2:4],16),int(h[4:],16))
def shade(cell, color):
    cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color}"/>'))
def styled_para(doc, text, bold=False, color=BK, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY, sa=4, sb=2, italic=False):
    p = doc.add_paragraph(); p.alignment = align
    p.paragraph_format.space_after = Pt(sa); p.paragraph_format.space_before = Pt(sb)
    run = p.add_run(text)
    run.font.name="Arial"; run.font.size=Pt(size); run.font.color.rgb=rgb(color)
    run.font.bold=bold; run.font.italic=italic
    return p
def multi_para(doc, runs):
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.space_after = Pt(4); p.paragraph_format.space_before = Pt(2)
    for r in runs:
        if isinstance(r, str):
            run = p.add_run(r); run.font.name="Arial"; run.font.size=Pt(10); run.font.color.rgb=rgb(BK)
        else:
            run = p.add_run(r.get("t","")); run.font.name="Arial"; run.font.size=Pt(r.get("s",10))
            run.font.color.rgb=rgb(r.get("c",BK)); run.font.bold=r.get("b",False); run.font.italic=r.get("i",False)
    return p
def section_heading(doc, num, text, color=GD):
    p = doc.add_paragraph(); p.paragraph_format.space_before=Pt(16); p.paragraph_format.space_after=Pt(8)
    pPr = p._p.get_or_add_pPr()
    pPr.append(parse_xml(f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="6" w:space="2" w:color="{color}"/></w:pBdr>'))
    run = p.add_run(f"{num}. {text}"); run.font.name="Arial"; run.font.size=Pt(13); run.font.bold=True; run.font.color.rgb=rgb(color)
def header_row(table, row_idx, texts, bg=GD):
    for i, t in enumerate(texts):
        c = table.cell(row_idx, i); c.text=""
        p = c.paragraphs[0]; p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(t); run.font.name="Arial"; run.font.size=Pt(9); run.font.bold=True; run.font.color.rgb=rgb(WH)
        shade(c, bg)
def body_cell(cell, text, bold=False, color=BK, size=9):
    cell.text=""
    run = cell.paragraphs[0].add_run(str(text) if text else "")
    run.font.name="Arial"; run.font.size=Pt(size); run.font.bold=bold; run.font.color.rgb=rgb(color)
def banner(doc, line1, line2, line3=None, bg=GD):
    t = doc.add_table(rows=1, cols=1)
    c = t.cell(0, 0); shade(c, bg)
    p1 = c.paragraphs[0]; p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = p1.add_run(line1); r1.font.name="Arial"; r1.font.size=Pt(14); r1.font.bold=True; r1.font.color.rgb=rgb(WH)
    p2 = c.add_paragraph(); p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = p2.add_run(line2); r2.font.name="Arial"; r2.font.size=Pt(11); r2.font.color.rgb=rgb(GL)
    if line3:
        p3 = c.add_paragraph(); p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r3 = p3.add_run(line3); r3.font.name="Arial"; r3.font.size=Pt(10); r3.font.color.rgb=rgb(GL)
def alert_box(doc, title, text, bg=RBG, tc=RD):
    t = doc.add_table(rows=1, cols=1)
    c = t.cell(0,0); shade(c, bg)
    r1 = c.paragraphs[0].add_run(title); r1.font.name="Arial"; r1.font.size=Pt(11); r1.font.bold=True; r1.font.color.rgb=rgb(tc)
    p2 = c.add_paragraph()
    r2 = p2.add_run(text); r2.font.name="Arial"; r2.font.size=Pt(9); r2.font.color.rgb=rgb(BK)


# ===== READERS =====
def read_clients(wb):
    ws = wb["Clients"]
    keys = ["id","exploitation","interlocuteur","commune","secteur","surface",
        "pct_chard","pct_pn","pct_meunier","pct_autre","certification","sencrop","id_station",
        "parcelles_mildiou","parcelles_oidium","historique_gel","cu_cumule","email","tel","notes"]
    return [dict(zip(keys, row)) for row in ws.iter_rows(min_row=3, max_col=20, values_only=True) if row[0] and row[1]]

def read_bulletin_hebdo(wb):
    """Lit l'onglet Bulletin Hebdo — données AV + zone libre"""
    ws = wb["Bulletin Hebdo"]
    data = {}
    for row in ws.iter_rows(min_row=1, max_col=4, values_only=True):
        if not row[0]: continue
        key = str(row[0]).strip()
        data[key] = {
            "val": row[1] if len(row) > 1 else None,
            "key2": row[2] if len(row) > 2 else None,
            "val2": row[3] if len(row) > 3 else None,
        }
    # Parse into structured dict
    av = {
        "numero": _get(data, "N° AV"),
        "date_av": _get(data, "Date AV"),
        "maturite": _get(data, "Maturité œufs mildiou"),
        "epi": _get(data, "EPI (Potentiel Infectieux)"),
        "risque_mildiou": _get(data, "Risque mildiou AV"),
        "reco_mildiou": _get(data, "Recommandation AV mildiou"),
        "risque_oidium": _get(data, "Risque oïdium AV"),
        "reco_oidium": _get(data, "Recommandation AV oïdium"),
        "gel": _get(data, "Gel — situation"),
        "mange_bourgeons": _get(data, "Mange-bourgeons"),
        "stade_chard": _get(data, "Chardonnay — stade régional"),
        "comment_chard": _get(data, "Commentaire phéno Chard."),
        "stade_pn": _get(data, "Pinot Noir — stade régional"),
        "comment_pn": _get(data, "Commentaire phéno PN"),
        "stade_meunier": _get(data, "Meunier — stade régional"),
        "comment_meunier": _get(data, "Commentaire phéno Meunier"),
        "avance": _get(data, "Avance phénologique"),
        "heterogeneite": _get(data, "Hétérogénéité gel"),
        "titre_complement": _get(data, "Titre section complémentaire"),
        "contenu_complement": _get(data, "Contenu (texte libre)"),
    }
    return av

def _get(data, key):
    if key in data: return data[key].get("val")
    for k, v in data.items():
        if key.lower() in k.lower(): return v.get("val")
        if v.get("key2") and key.lower() in str(v["key2"]).lower(): return v.get("val2")
    return None

def read_prescriptions(wb, client_id):
    ws = wb["Prescriptions"]
    prescriptions = []
    for row in ws.iter_rows(min_row=4, max_col=16, values_only=True):
        if not row[0] or str(row[0]) != str(client_id): continue
        prescriptions.append({
            "id_client": row[0], "exploitation": row[1], "cible": row[2],
            "passage": row[3], "id_produit": row[4], "nom": row[5],
            "sa": row[6], "type_cps": row[7], "dose_homologuee": row[8],
            "dose_prescrite": row[9], "volume_bouillie": row[10],
            "date_prevue": row[11], "observations": row[12],
            "applique": row[13], "date_reelle": row[14], "dose_reelle": row[15],
        })
    return prescriptions

def read_suivi(wb, client_id):
    ws = wb["Suivi campagne"]
    keys = ["date","id_client","exploitation","commune","stade_chard","stade_pn","stade_meunier",
        "degats_gel","pluie","temp","etat_sol","maturite","risque_mildiou","risque_oidium",
        "traitement","produit","dose","prochain","observations"]
    last = None
    for row in ws.iter_rows(min_row=3, max_col=19, values_only=True):
        if row[1] == client_id and row[0]: last = dict(zip(keys, row))
    return last

def fetch_meteo():
    if not HAS_REQUESTS: return None
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={COORDS['lat']}&longitude={COORDS['lon']}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe/Paris&forecast_days=7"
        r = requests.get(url, timeout=10); data = r.json()
        days = []
        d = data["daily"]
        for i in range(len(d["time"])):
            tmoy = round((d["temperature_2m_max"][i] + d["temperature_2m_min"][i]) / 2, 1)
            pluie = d["precipitation_sum"][i]
            if pluie < 2: risk = "Nul"
            elif tmoy < 11: risk = "Faible"
            elif pluie >= 10: risk = "Élevé"
            else: risk = "Modéré"
            days.append({"date": d["time"][i], "tmax": d["temperature_2m_max"][i],
                "tmin": d["temperature_2m_min"][i], "tmoy": tmoy, "pluie": pluie, "risk": risk})
        return days
    except Exception as e:
        print(f"   ⚠️  Erreur météo : {e}")
        return None


# ===== BUILD BULLETIN =====
def build_bulletin(client, av, prescriptions, suivi, meteo_days):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width=Cm(21); sec.page_height=Cm(29.7)
    sec.top_margin=Cm(1.2); sec.bottom_margin=Cm(1.2)
    sec.left_margin=Cm(1.5); sec.right_margin=Cm(1.5)
    style = doc.styles['Normal']; style.font.name='Arial'; style.font.size=Pt(10)

    certif = client["certification"] or "Conventionnel"
    presc_mildiou = [p for p in prescriptions if "mildiou" in (p["cible"] or "").lower()]
    presc_oidium = [p for p in prescriptions if "dium" in (p["cible"] or "").lower()]
    presc_botrytis = [p for p in prescriptions if "botrytis" in (p["cible"] or "").lower()]
    presc_autres = [p for p in prescriptions if p not in presc_mildiou + presc_oidium + presc_botrytis]

    # Cumul folpel
    cumul_folpel = 0.0
    nb_folpel = 0
    for p in prescriptions:
        sa = (p.get("sa") or "").lower(); nom = (p.get("nom") or "").lower()
        if "folpel" not in sa and "folpel" not in nom and "folpan" not in nom: continue
        nb_folpel += 1
        dose_str = str(p.get("dose_prescrite") or p.get("dose_homologuee") or "0")
        nums = re.findall(r'(\d+[.,]?\d*)', dose_str)
        if nums:
            val = float(nums[0].replace(",", "."))
            if "folpan" in nom and "l" in dose_str.lower(): val = val * 0.5
            cumul_folpel += val

    # ===== BANNIÈRE TITRE =====
    banner(doc,
        "FICHE DE CONSEIL TECHNIQUE INDIVIDUELLE",
        f"{client['exploitation']}  —  {client['commune'] or ''}  —  {TODAY}",
        f"{client['interlocuteur'] or ''}  |  {certif}")
    doc.add_paragraph()

    # ===== 1. PHÉNOLOGIE =====
    section_heading(doc, "1", "PHÉNOLOGIE")

    # Texte rédigé à partir des données AV + client
    avance = av.get("avance") or "≈ 14 jours d'avance"
    stade_c = av.get("stade_chard") or "non renseigné"
    stade_p = av.get("stade_pn") or "non renseigné"
    stade_m = av.get("stade_meunier") or "non renseigné"
    comment_c = av.get("comment_chard") or ""

    multi_para(doc, [
        {"t": f"Avertissement Viticole n°{av.get('numero') or '?'} du {av.get('date_av') or '?'} — ", "b": True, "s": 10, "c": GR},
        f"L'avance phénologique est estimée à {avance} sur la normale décennale. "
        f"{comment_c + '. ' if comment_c else ''}"
        f"Sur le vignoble champenois, le Chardonnay se situe au {stade_c}. "
        f"Le Pinot Noir est au {stade_p}. Le Meunier au {stade_m}."
    ])

    # Client-specific phenology from suivi
    if suivi:
        obs_txt = ""
        if suivi.get("stade_chard"):
            obs_txt += f"Sur votre exploitation, le Chardonnay est au stade {suivi['stade_chard']}. "
        if suivi.get("stade_pn"):
            obs_txt += f"Le Pinot Noir au stade {suivi['stade_pn']}. "
        if suivi.get("degats_gel"):
            obs_txt += f"Les dégâts de gel sont estimés à {suivi['degats_gel']}%. "
        if suivi.get("observations"):
            obs_txt += f"{suivi['observations']}. "
        if obs_txt:
            multi_para(doc, [{"t": "Sur vos parcelles : ", "b": True, "s": 10, "c": GD}, obs_txt])

    # Gel + mange-bourgeons
    gel_txt = av.get("gel")
    mb_txt = av.get("mange_bourgeons")
    het_txt = av.get("heterogeneite")
    if gel_txt or mb_txt:
        parts = []
        if gel_txt: parts.append(gel_txt)
        if het_txt: parts.append(het_txt)
        if mb_txt: parts.append(f"Mange-bourgeons : {mb_txt}")
        multi_para(doc, [{"t": "Gel et ravageurs : ", "b": True, "s": 10, "c": OR}, " ".join(parts)])

    # ===== 2. MILDIOU =====
    section_heading(doc, "2", "SITUATION MILDIOU", OR)

    matu = av.get("maturite") or "information non disponible"
    epi = av.get("epi") or ""
    risque = av.get("risque_mildiou") or ""
    reco = av.get("reco_mildiou") or ""

    multi_para(doc, [
        {"t": "Maturité des œufs d'hiver : ", "b": True}, f"{matu}. ",
        {"t": "Potentiel épidémique (EPI) : ", "b": True}, f"{epi}. " if epi else "",
    ])

    multi_para(doc, [
        {"t": "Analyse du risque : ", "b": True, "c": OR}, f"{risque} ",
    ])

    if reco:
        multi_para(doc, [{"t": "Recommandation Comité Champagne : ", "b": True, "c": GR, "i": True}, f"{reco}"])

    # Client-specific mildiou context from suivi
    if suivi:
        pluie = float(suivi.get("pluie", 0) or 0)
        temp = float(suivi.get("temp", 0) or 0)
        sol = suivi.get("etat_sol") or ""
        sc = suivi.get("stade_chard")
        recept = sc and str(sc) >= "06"

        txt = f"Sur votre exploitation : cumul pluviométrique de {pluie} mm, température moyenne de {temp}°C"
        if sol: txt += f", sol {sol.lower()}"
        txt += ". "
        if recept:
            txt += "La réceptivité du Chardonnay est acquise (stade ≥ 06). "
        if pluie >= 2 and temp >= 11:
            if "sec" in sol.lower():
                txt += "Les conditions de contamination sont partiellement réunies mais le sol sec atténue le risque."
            else:
                txt += "Les trois conditions de contamination primaire sont réunies. Vigilance maximale."
        elif pluie < 2:
            txt += "Pas de pluie significative sur la période — pas de risque de contamination à ce stade."
        else:
            txt += "Température insuffisante pour permettre la germination des spores."

        multi_para(doc, [{"t": "Situation parcellaire : ", "b": True, "c": GD}, txt])

    if client.get("parcelles_mildiou"):
        styled_para(doc, f"Parcelles sensibles à surveiller en priorité : {client['parcelles_mildiou']}", size=9, color=GR, italic=True)

    # ===== 3. OÏDIUM =====
    section_heading(doc, "3", "SITUATION OÏDIUM", GM)

    risque_o = av.get("risque_oidium") or "information non disponible"
    reco_o = av.get("reco_oidium") or ""

    multi_para(doc, [
        {"t": "Analyse régionale : ", "b": True}, f"{risque_o}. ",
    ])

    if reco_o:
        multi_para(doc, [{"t": "Recommandation Comité Champagne : ", "b": True, "c": GR, "i": True}, reco_o])

    if client.get("parcelles_oidium"):
        multi_para(doc, [{"t": "Parcelles à historique oïdium : ", "b": True, "c": GM},
            f"{client['parcelles_oidium']}. Surveillance renforcée sur ces parcelles."])

    # ===== 4. MÉTÉO 7 JOURS =====
    section_heading(doc, "4", "PRÉVISIONS MÉTÉO — 7 JOURS", BL)

    if meteo_days:
        styled_para(doc, f"Source : Open-Meteo (ECMWF) — Épernay — {TODAY}", size=8, color=GR, italic=True)
        t = doc.add_table(rows=8, cols=8)
        header_row(t, 0, ["", "J+1", "J+2", "J+3", "J+4", "J+5", "J+6", "J+7"], bg=BL)
        labels = ["Date","T° min (°C)","T° max (°C)","T° moy (°C)","Pluie (mm)","Risque gel","Risque contam."]
        for i, lbl in enumerate(labels):
            body_cell(t.cell(i+1, 0), lbl, bold=True, size=8); shade(t.cell(i+1, 0), BBG)
        for j, day in enumerate(meteo_days[:7]):
            d = datetime.strptime(day["date"], "%Y-%m-%d")
            vals = [d.strftime("%d/%m"), str(day["tmin"]), str(day["tmax"]), str(day["tmoy"]),
                str(day["pluie"]), "Oui" if day["tmin"]<=0 else "Non", day["risk"]]
            for i, v in enumerate(vals):
                body_cell(t.cell(i+1, j+1), v, size=8,
                    color=RD if (i==5 and v=="Oui") or (i==6 and "lev" in v.lower()) else BK)
                if i==6 and "lev" in v.lower(): shade(t.cell(i+1,j+1), RBG)
                elif i==6 and "odér" in v.lower(): shade(t.cell(i+1,j+1), OBG)

        doc.add_paragraph()
        risk_days = [d for d in meteo_days if d["risk"] in ("Modéré","Élevé","Très élevé")]
        if risk_days:
            alert_box(doc, f"⚠️ {len(risk_days)} jour(s) à risque de contamination",
                ", ".join([f"{datetime.strptime(d['date'],'%Y-%m-%d').strftime('%d/%m')} ({d['pluie']}mm, {d['tmoy']}°C)" for d in risk_days]),
                OBG, OR)
        else:
            alert_box(doc, "✅ Pas de risque mildiou sur 7 jours",
                "Aucun jour ne réunit pluie ≥ 2 mm + T° moy ≥ 11°C.", GBG, GM)
    else:
        styled_para(doc, "Météo non disponible — consultez le Dashboard VITI Sens.", bold=True, color=GR)

    # ===== 5. PROGRAMME PHYTO =====
    section_heading(doc, "5", "PROGRAMME PHYTOSANITAIRE", RD)
    styled_para(doc, f"Certification : {certif} — Cu cumulé : {client['cu_cumule'] or 0} g/ha — Folpel cumulé : {cumul_folpel:.1f} kg/ha ({nb_folpel} traitements)",
        bold=True, size=9, color=GR)

    if not prescriptions:
        alert_box(doc, "⚠️ Aucune prescription saisie",
            "Ouvrez la base Excel → onglet Prescriptions → ajoutez les produits et doses pour ce client.",
            YBG, OR)
    else:
        for cible_label, presc_list, bg_hdr in [
            ("MILDIOU", presc_mildiou, OR), ("OÏDIUM", presc_oidium, GM),
            ("BOTRYTIS", presc_botrytis, GR), ("AUTRES", presc_autres, "555555")]:
            if not presc_list: continue
            styled_para(doc, cible_label, bold=True, size=12, color=bg_hdr, sb=10)
            t = doc.add_table(rows=1+len(presc_list), cols=7)
            header_row(t, 0, ["Passage","Produit","Type","Dose homologuée","DOSE PRESCRITE","Vol. bouillie","Observations"], bg=bg_hdr)
            for i, p in enumerate(presc_list):
                ri = i+1
                dp = p["dose_prescrite"] or "[à renseigner]"
                dh = p["dose_homologuee"] or "—"
                custom = dp and dp != dh and "[" not in str(dp)
                body_cell(t.cell(ri,0), p["passage"] or "—", bold=True, size=9)
                body_cell(t.cell(ri,1), p["nom"] or "—", bold=True, size=9)
                body_cell(t.cell(ri,2), p["type_cps"] or "—", size=8)
                body_cell(t.cell(ri,3), dh, size=8, color=GR)
                body_cell(t.cell(ri,4), dp, bold=True, size=9, color=RD if custom else BK)
                shade(t.cell(ri,4), YBG)
                body_cell(t.cell(ri,5), f"{p['volume_bouillie']} L/ha" if p['volume_bouillie'] else "—", size=8)
                body_cell(t.cell(ri,6), p["observations"] or "", size=8, color=GR)
                if p.get("applique") == "Oui":
                    shade(t.cell(ri,0), GBG)
                    body_cell(t.cell(ri,0), f"{p['passage']} ✅", bold=True, size=9)

        # Bio copper tracking
        if certif == "Bio":
            cu = client["cu_cumule"] or 0; rest = 4000-cu
            col = RD if rest<1000 else OR if rest<2000 else GM
            doc.add_paragraph()
            alert_box(doc, f"🟤 Cuivre : {rest} g/ha restants",
                f"Cumulé : {cu} g/ha sur 4 000. {'⚠️ CRITIQUE !' if rest<1000 else 'Gérer avec attention.' if rest<2000 else 'Marge correcte.'}",
                RBG if rest<1000 else OBG if rest<2000 else GBG, col)

    # ===== SECTION COMPLÉMENTAIRE (optionnelle) =====
    titre_comp = av.get("titre_complement")
    contenu_comp = av.get("contenu_complement")
    if titre_comp and contenu_comp:
        section_heading(doc, "6", titre_comp.upper(), GD)
        styled_para(doc, contenu_comp, size=10)

    # ===== FOOTER =====
    doc.add_paragraph()
    styled_para(doc, "Ce document est établi par VITI Sens à titre de conseil technique individuel. "
        "La décision de traiter reste sous l'entière responsabilité du viticulteur.",
        size=7, color=GR, italic=True)
    styled_para(doc, "VITI Sens — Florent Miguel | florent.miguel@sasu-viti-sens.fr | Reims (51)", size=7, color=GR)

    return doc


# ===== MAIN =====
def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "VITI_Sens_Base_Donnees_2026.xlsx"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "bulletins_clients"
    if not os.path.exists(base): print(f"❌ {base} non trouvé"); sys.exit(1)
    os.makedirs(outdir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  VITI Sens — Génération bulletins v4")
    print(f"  Date : {TODAY}")
    print(f"{'='*60}")

    wb = load_workbook(base)
    clients = read_clients(wb)

    if "Bulletin Hebdo" not in wb.sheetnames:
        print("❌ Onglet 'Bulletin Hebdo' manquant."); sys.exit(1)
    if "Prescriptions" not in wb.sheetnames:
        print("❌ Onglet 'Prescriptions' manquant."); sys.exit(1)

    av = read_bulletin_hebdo(wb)
    print(f"\n📋 AV n°{av.get('numero') or '?'} du {av.get('date_av') or '?'}")
    print(f"   Maturité : {av.get('maturite') or '?'}")
    print(f"   Risque mildiou : {av.get('risque_mildiou') or '?'}")
    if av.get("titre_complement"):
        print(f"   📝 Section complémentaire : {av['titre_complement']}")
    print(f"   👥 {len(clients)} client(s)")

    meteo = fetch_meteo()
    if meteo: print(f"   🌤️ {len(meteo)} jours météo chargés")
    else: print("   ⚠️ Météo non disponible")

    for client in clients:
        certif = client["certification"] or "Conv."
        print(f"\n📋 {client['exploitation']} ({client['commune']}) — {certif}")
        prescriptions = read_prescriptions(wb, client["id"])
        suivi = read_suivi(wb, client["id"])
        print(f"   💊 {len(prescriptions)} prescription(s)")
        doc = build_bulletin(client, av, prescriptions, suivi, meteo)
        safe = client["exploitation"].replace(" ","_").replace(".","").replace("/","-")
        fname = f"{outdir}/Bulletin_{safe}_{datetime.now().strftime('%Y-%m-%d')}.docx"
        doc.save(fname)
        print(f"   ✅ {fname}")

    print(f"\n{'='*60}")
    print(f"  ✅ {len(clients)} bulletin(s) dans ./{outdir}/")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
