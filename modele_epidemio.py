"""
VITI Sens — Modèle épidémiologique mildiou & oïdium vigne
==========================================================
Modèle simplifié basé sur les publications scientifiques :
- Maturité des œufs : modèle de Hill (somme thermique base 8°C, seuil ~170 DJ)
- Contamination primaire : règle des 3 conditions (maturité + pluie ≥ 2mm + T° ≥ 11°C)
- Durée d'incubation : courbe de Goidanich (f(T°))
- Contamination secondaire (repiquage) : pluie ou humectation après fin d'incubation
- Oïdium : T° 15-28°C + condensation nocturne + projections ascospores

Ce modèle n'est PAS le Potentiel Système IFV (propriétaire, calibré sur 30 ans).
C'est un indicateur fiable pour le conseil, à croiser avec les AV et le terrain.
"""

from datetime import datetime, timedelta

MOIS_FR = ["","janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]

def date_fr(d):
    """Convertit '2026-05-03' en '03/05' ou '3 mai'"""
    if not d or len(d) < 10: return d or "?"
    try:
        dt = datetime.strptime(d[:10], "%Y-%m-%d")
        return dt.strftime("%d/%m")
    except:
        return d[-5:] if d else "?"

def date_fr_long(d):
    """Convertit '2026-05-03' en '3 mai'"""
    if not d or len(d) < 10: return d or "?"
    try:
        dt = datetime.strptime(d[:10], "%Y-%m-%d")
        return f"{dt.day} {MOIS_FR[dt.month]}"
    except:
        return d or "?" 
import math


# ===== MILDIOU — MATURITÉ DES ŒUFS =====

def calc_maturite_oeufs(meteo_historique):
    """
    Calcul de la maturité des œufs d'hiver (oospores).
    
    Principe : somme de degrés-jours base 8°C à partir du 1er janvier.
    Le seuil de maturité est ~170 DJ (variable selon les régions, 
    150-200 pour la Champagne compte tenu du climat frais).
    
    Args:
        meteo_historique: liste de dicts {"date", "tmoy"} depuis le 1er janvier
    
    Returns:
        dict avec cumul_dj, pct_maturite, date_maturite, statut
    """
    BASE = 8.0  # T° de base pour le calcul
    SEUIL = 170  # Degrés-jours pour maturité complète (Champagne)
    
    cumul_dj = 0.0
    date_maturite = None
    
    for day in meteo_historique:
        tmoy = day.get("tmoy", 0) or 0
        if tmoy > BASE:
            cumul_dj += (tmoy - BASE)
        if cumul_dj >= SEUIL and date_maturite is None:
            date_maturite = day.get("date")
    
    pct = min(100, round(cumul_dj / SEUIL * 100))
    
    if pct >= 100:
        statut = "Acquise"
    elif pct >= 80:
        statut = "Imminente"
    elif pct >= 50:
        statut = "En cours"
    else:
        statut = "Non acquise"
    
    return {
        "cumul_dj": round(cumul_dj, 1),
        "seuil_dj": SEUIL,
        "pct_maturite": pct,
        "date_maturite": date_maturite,
        "statut": statut,
    }


# ===== MILDIOU — DÉTECTION DES CONTAMINATIONS =====

def duree_incubation(tmoy):
    """
    Durée d'incubation du mildiou en fonction de la température moyenne.
    Courbe de Goidanich simplifiée (sources: BASF, IFV, Phyteis).
    
    T° 12°C → 14 jours
    T° 16°C → 8 jours
    T° 20°C → 6 jours
    T° 24°C → 4-5 jours (optimum)
    T° 28°C → 5-6 jours (ralentissement)
    
    Formule approchée : y = 330 / (T - 5.5)^1.15  (ajustée sur les données publiées)
    """
    if tmoy is None or tmoy < 11:
        return None  # Pas de développement en dessous de 11°C
    
    # Table de référence calibrée sur données publiées (BASF, Phyteis, IFV)
    # T°C : 12→14j, 14→10j, 16→8j, 18→7j, 20→6j, 22→5j, 24→4j, 26→5j, 28→5j
    table = {12:14, 13:12, 14:10, 15:9, 16:8, 17:7, 18:7, 19:6, 20:6, 21:5, 22:5, 23:4, 24:4, 25:5, 26:5, 27:5, 28:6}
    t_int = max(12, min(28, round(tmoy)))
    if t_int in table:
        return table[t_int]
    return 10  # Valeur par défaut


def detecter_contaminations_mildiou(meteo_jours, maturite_acquise=True, vigne_receptive=True):
    """
    Détecte les événements de contamination mildiou sur une série météo.
    
    Conditions de contamination primaire (règle classique) :
    1. Maturité des œufs acquise
    2. Vigne réceptive (≥ stade 06, ~2-3 feuilles étalées)
    3. Pluie ≥ 2 mm sur sol humide (ou ≥ 5 mm sur sol sec)
    4. T° moyenne ≥ 11°C pendant l'épisode pluvieux
    
    Contamination secondaire (repiquage) :
    - Après fin d'incubation d'une contamination précédente
    - Pluie ou humectation foliaire suffisante
    - T° ≥ 11°C
    
    Args:
        meteo_jours: liste de dicts avec au minimum {"date", "tmoy", "pluie"}
                     optionnel: {"hr", "leaf_wetness", "soil_moisture", "soil_moisture_txt"}
        maturite_acquise: bool
        vigne_receptive: bool
    
    Returns:
        liste de contaminations détectées
    """
    if not maturite_acquise or not vigne_receptive:
        return []
    
    contaminations = []
    incubations_en_cours = []  # liste de {"date_contam", "date_sortie_prevue", "type"}
    
    for i, day in enumerate(meteo_jours):
        tmoy = day.get("tmoy", 0) or 0
        pluie = day.get("pluie", 0) or 0
        hr = day.get("hr")
        lw = day.get("leaf_wetness")
        sol = day.get("soil_moisture_txt", "")
        date = day.get("date", "")
        
        # Vérifier la fin d'incubations en cours
        for inc in incubations_en_cours:
            if date >= inc["date_sortie_prevue"] and inc.get("statut") != "terminée":
                inc["statut"] = "terminée"
                inc["sporulation"] = True
        
        # Conditions de contamination
        contam_possible = False
        type_contam = None
        intensite = "faible"
        
        if tmoy >= 11:
            # Seuil de pluie dépend de l'état du sol
            seuil_pluie = 5.0 if "sec" in (sol or "").lower() else 2.0
            
            if pluie >= seuil_pluie:
                contam_possible = True
                type_contam = "primaire" if not contaminations else "secondaire (repiquage)"
                
                # Intensité basée sur le volume de pluie et les conditions
                if pluie >= 15:
                    intensite = "très forte"
                elif pluie >= 10:
                    intensite = "forte"
                elif pluie >= 5:
                    intensite = "modérée"
                else:
                    intensite = "faible"
                
                # Humidité du sol amplifie
                if sol and ("humide" in sol.lower() or "satur" in sol.lower()):
                    if intensite == "modérée": intensite = "forte"
                    elif intensite == "faible": intensite = "modérée"
            
            # Contamination possible sans pluie mais avec forte humectation
            elif lw is not None and lw >= 70 and hr is not None and hr >= 90:
                # Contamination par rosée/brouillard (repiquage uniquement)
                if any(inc.get("sporulation") for inc in incubations_en_cours):
                    contam_possible = True
                    type_contam = "secondaire (rosée/brouillard)"
                    intensite = "faible"
        
        if contam_possible:
            # Calculer la durée d'incubation
            duree = duree_incubation(tmoy)
            if duree:
                try:
                    dt = datetime.strptime(date, "%Y-%m-%d")
                    date_sortie = (dt + timedelta(days=duree)).strftime("%Y-%m-%d")
                except:
                    date_sortie = f"+{duree}j"
                
                contam = {
                    "date": date,
                    "type": type_contam,
                    "pluie": pluie,
                    "tmoy": tmoy,
                    "intensite": intensite,
                    "duree_incubation": duree,
                    "date_sortie_prevue": date_sortie,
                    "hr": hr,
                    "leaf_wetness": lw,
                    "sol": sol,
                    "statut": "en incubation",
                    "sporulation": False,
                }
                contaminations.append(contam)
                incubations_en_cours.append(contam)
    
    return contaminations


# ===== OÏDIUM — MODÈLE SIMPLIFIÉ =====

def calc_risque_oidium_journalier(tmoy, tmin, hr, dewpoint, pluie=0):
    """
    Risque oïdium journalier basé sur les facteurs biologiques connus.
    
    L'oïdium (Erysiphe necator) se développe différemment du mildiou :
    - Il N'A PAS BESOIN d'eau libre (contrairement au mildiou)
    - Optimum thermique : 25°C (plage 15-28°C)
    - L'humidité relative > 40% suffit (pas besoin de pluie)
    - La condensation nocturne favorise la germination
    - Les pluies battantes DÉFAVORISENT l'oïdium (lave les spores)
    - La lumière directe (UV) freine le développement
    
    Sources : IFV, CIVC, note technique résistances
    """
    if tmoy is None:
        return {"risque": "—", "score": 0, "detail": "Données insuffisantes"}
    
    score = 0
    details = []
    
    # Température
    if 20 <= tmoy <= 28:
        score += 35
        details.append(f"T° optimale ({tmoy}°C)")
    elif 15 <= tmoy < 20:
        score += 20
        details.append(f"T° favorable ({tmoy}°C)")
    elif 28 < tmoy <= 32:
        score += 10
        details.append(f"T° limite haute ({tmoy}°C)")
    elif tmoy < 12 or tmoy > 35:
        return {"risque": "Nul", "score": 0, "detail": f"T° défavorable ({tmoy}°C)"}
    else:
        score += 5
    
    # Humidité relative
    if hr is not None:
        if hr >= 70:
            score += 20
            details.append(f"HR élevée ({hr:.0f}%)")
        elif hr >= 40:
            score += 10
            details.append(f"HR suffisante ({hr:.0f}%)")
        else:
            score -= 10
            details.append(f"HR basse ({hr:.0f}%)")
    
    # Condensation nocturne (T° min proche du point de rosée)
    if dewpoint is not None and tmin is not None:
        ecart = tmin - dewpoint
        if ecart < 1:
            score += 25
            details.append("Condensation nocturne certaine")
        elif ecart < 2:
            score += 15
            details.append("Condensation nocturne probable")
        elif ecart < 3:
            score += 5
    
    # Pluie (défavorable pour l'oïdium !)
    if pluie is not None and pluie > 5:
        score -= 15
        details.append(f"Pluie lessivante ({pluie}mm)")
    
    # Interprétation
    if score >= 50:
        risque = "Élevé"
    elif score >= 30:
        risque = "Modéré"
    elif score >= 15:
        risque = "Faible"
    else:
        risque = "Nul"
    
    return {
        "risque": risque,
        "score": min(100, max(0, score)),
        "detail": " · ".join(details) if details else "—",
    }


def calc_indice_sortie_hiver_oidium(meteo_historique_hiver):
    """
    Indice de sortie d'hiver pour l'oïdium.
    
    L'oïdium se conserve sous forme de cléistothèces (reproduction sexuée) 
    et de mycélium dans les bourgeons (reproduction asexuée).
    
    Un hiver doux et humide favorise la survie et la production d'ascospores.
    Un hiver froid et sec réduit l'inoculum.
    
    Indicateurs :
    - Nombre de jours avec T° min < -5°C (défavorable à l'oïdium)
    - Nombre de jours doux (T° moy > 5°C) en jan-fév
    - Pluviométrie hivernale (favorable si > 200mm)
    
    Args:
        meteo_historique_hiver: données déc-fév {"date", "tmin", "tmoy", "pluie"}
    
    Returns:
        dict avec indice (0-100), niveau, interpretation
    """
    if not meteo_historique_hiver:
        return {"indice": 50, "niveau": "Modéré (données insuffisantes)", "interpretation": "Pas de données hivernales"}
    
    jours_gel_severe = sum(1 for d in meteo_historique_hiver if (d.get("tmin") or 0) < -5)
    jours_doux = sum(1 for d in meteo_historique_hiver if (d.get("tmoy") or 0) > 5)
    pluie_totale = sum(d.get("pluie", 0) or 0 for d in meteo_historique_hiver)
    nb_jours = len(meteo_historique_hiver)
    
    # Score composite
    score = 50  # Base neutre
    
    # Gel sévère = défavorable
    if jours_gel_severe > 15:
        score -= 25
    elif jours_gel_severe > 8:
        score -= 15
    elif jours_gel_severe > 3:
        score -= 5
    
    # Jours doux = favorable
    pct_doux = jours_doux / max(nb_jours, 1) * 100
    if pct_doux > 60:
        score += 25
    elif pct_doux > 40:
        score += 15
    elif pct_doux > 20:
        score += 5
    
    # Pluie = favorable
    if pluie_totale > 300:
        score += 15
    elif pluie_totale > 200:
        score += 10
    elif pluie_totale < 100:
        score -= 10
    
    score = max(0, min(100, score))
    
    if score >= 65:
        niveau = "Élevé"
        interpretation = f"Hiver doux ({jours_doux}j > 5°C) et peu de gel sévère ({jours_gel_severe}j < -5°C). Inoculum probablement important."
    elif score >= 35:
        niveau = "Modéré"
        interpretation = f"Conditions hivernales moyennes. Gel sévère : {jours_gel_severe}j, douceur : {jours_doux}j."
    else:
        niveau = "Faible"
        interpretation = f"Hiver rigoureux ({jours_gel_severe}j < -5°C). Inoculum probablement réduit."
    
    return {
        "indice": score,
        "niveau": niveau,
        "interpretation": interpretation,
        "jours_gel_severe": jours_gel_severe,
        "jours_doux": jours_doux,
        "pluie_totale_mm": round(pluie_totale, 1),
    }


# ===== SYNTHÈSE RISQUE GLOBAL =====

def synthese_risque_7j(meteo_7j, maturite_acquise=True, vigne_receptive=True):
    """
    Produit une synthèse du risque mildiou et oïdium sur 7 jours.
    
    Returns:
        dict avec analyse textuelle et données structurées
    """
    # Mildiou
    contaminations = detecter_contaminations_mildiou(meteo_7j, maturite_acquise, vigne_receptive)
    
    # Oïdium jour par jour
    risques_oidium = []
    for day in meteo_7j:
        ro = calc_risque_oidium_journalier(
            day.get("tmoy"), day.get("tmin"), day.get("hr"), 
            day.get("dewpoint"), day.get("pluie"))
        risques_oidium.append({**ro, "date": day.get("date")})
    
    # Fenêtres de traitement
    fenetres = []
    for day in meteo_7j:
        wind = day.get("wind")
        pluie = day.get("pluie", 0) or 0
        if wind is not None and wind < 19 and pluie < 1:
            fenetres.append(day.get("date"))
    
    # Texte synthèse mildiou
    if not maturite_acquise:
        txt_mildiou = "Maturité des œufs d'hiver non acquise — pas de risque de contamination primaire à ce stade."
    elif not vigne_receptive:
        txt_mildiou = "Vigne non encore réceptive (stade < 06) — pas de risque de contamination."
    elif contaminations:
        nb = len(contaminations)
        dates = ", ".join([date_fr(c["date"]) for c in contaminations])
        intensites = [c["intensite"] for c in contaminations]
        max_int = "très forte" if "très forte" in intensites else "forte" if "forte" in intensites else "modérée" if "modérée" in intensites else "faible"
        
        txt_mildiou = f"{nb} contamination(s) détectée(s) sur 7 jours ({dates}). "
        txt_mildiou += f"Intensité maximale : {max_int}. "
        
        # Incubation
        for c in contaminations:
            txt_mildiou += f"Contamination du {date_fr_long(c['date'])} ({c['pluie']}mm, {c['tmoy']}°C) : "
            txt_mildiou += f"sortie des taches prévue vers le {date_fr_long(c['date_sortie_prevue'])} ({c['duree_incubation']}j d'incubation). "
    else:
        txt_mildiou = "Aucune contamination détectée sur les 7 prochains jours. Les conditions ne réunissent pas les 3 critères (pluie ≥ 2mm + T° ≥ 11°C + sol humide)."
    
    # Texte synthèse oïdium
    jours_eleve = [r for r in risques_oidium if r["risque"] == "Élevé"]
    jours_modere = [r for r in risques_oidium if r["risque"] == "Modéré"]
    
    if jours_eleve:
        txt_oidium = f"Risque oïdium élevé {len(jours_eleve)} jour(s) sur 7. "
        txt_oidium += "Conditions favorables : " + jours_eleve[0]["detail"] + "."
    elif jours_modere:
        txt_oidium = f"Risque oïdium modéré {len(jours_modere)} jour(s). Surveillance recommandée."
    else:
        txt_oidium = "Risque oïdium faible à nul sur les 7 prochains jours."
    
    # Fenêtres
    if fenetres:
        txt_fenetre = f"{len(fenetres)} fenêtre(s) de traitement identifiée(s) : {', '.join([date_fr(f) for f in fenetres])}."
    else:
        txt_fenetre = "Aucune fenêtre de traitement favorable sur 7 jours (vent > 19 km/h ou pluie)."
    
    return {
        "contaminations_mildiou": contaminations,
        "risques_oidium": risques_oidium,
        "fenetres_traitement": fenetres,
        "texte_mildiou": txt_mildiou,
        "texte_oidium": txt_oidium,
        "texte_fenetre": txt_fenetre,
        "nb_contaminations": len(contaminations),
        "nb_jours_oidium_eleve": len(jours_eleve),
        "nb_fenetres": len(fenetres),
    }
