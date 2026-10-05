# Moteur épidémiologique dans Pilot — version avec risque par commune et nouvelle forme des analyses

Cette version **met à niveau** une installation déjà branchée et fonctionne aussi sur un Pilot jamais branché.

## Ce que ça change
1. **Forme des analyses mildiou et oïdium** : un titre calculé (« RISQUE MILDIOU — PRESSION EN HAUSSE », « RISQUE OÏDIUM — PRESSION
   MODÉRÉE »…), puis 3 ou 4 paragraphes d'un ton de conseiller, plus longs et moins scientifiques. Le titre n'est pas laissé à l'IA :
   il vient d'une règle (charge d'infection des 7 prochains jours comparée à celle des 7 derniers ; hausse à partir de x1,25, baisse
   à partir de x0,75, faible sous 50 °C·h). Les textes restent à relire avant d'être enregistrés, comme avant.
2. **Risque par commune de client** : chaque bulletin client reçoit un paragraphe « Situation sur votre commune (…), d'après la météo »,
   calculé avec la météo de SA commune. Ce paragraphe est rédigé par programme, sans IA : il ne fait que dire les faits du moteur,
   donc il n'y a rien à relire. Dans « Situation parcellaire », la conclusion fruste (« les trois conditions sont réunies, vigilance
   maximale ») disparaît quand ce paragraphe existe ; les faits de l'exploitation (pluie, température, sol) restent.
3. **Rendu** : le bulletin Word/PDF, l'aperçu du tableau de bord et le portail du vigneron respectent les paragraphes.
4. **Tableau « Prévisions météo — 7 jours »** : sa ligne « Risque contam. » vient du moteur, avec la météo de la commune du client, jour par
   jour (Nul < 50 °C·h ≤ Faible ; Modéré dès 50 ; Élevé dès 100 ; Très élevé dès 200 : mêmes mots et mêmes couleurs qu'avant). L'encadré
   sous le tableau (« N jour(s) à risque de contamination » ou « Pas de risque mildiou sur 7 jours ») suit ces libellés ; sa phrase
   « pluie ≥ 2 mm + T° ≥ 11 °C » est remplacée par « Aucune infection significative n'est attendue sur votre commune d'après la météo ».
5. **Libellé** : « Recommandation » (et non plus « Recommandation Comité Champagne ») au-dessus de tes recommandations mildiou et oïdium.
6. **EPI** : si la génération de texte ne renvoie pas d'EPI (serveur à 4 champs, ou IA qui l'oublie), le moteur le calcule : une courte phrase sans
   point final, par exemple « modéré et en hausse, avec une infection attendue autour du 07/10 ». Un EPI écrit par l'IA est respecté.
7. **Cache du navigateur** : `sw.js` passe en version 2. Le portail et le tableau de bord sont servis « cache d'abord » : sans cela, un navigateur
   qui a déjà visité ces pages continue d'afficher l'ancienne version, même après la mise à jour du serveur.

**Garantie** : sans retour à la ligne dans les textes, et si le moteur est absent ou en panne, un bulletin est rendu comme avant, paragraphes
et tableaux compris (vérifié sur une copie de ta base) ; seul le libellé « Recommandation » change. Le tableau retombe alors sur l'ancien
modèle, jour par jour.

## Installation (depuis une version déjà branchée ou non)
1. **PC** : copie `epidemio_pilot.py`, `brancher_epidemio.py` et `test_epidemio_pilot.py` dans le dossier du dépôt Matuscore, puis
   `git add .`, `git commit -m "Risque par commune et nouvelle forme des analyses"`, `git push`.
2. **VPS** :
   ```bash
   cd ~/Matuscore && git pull
   python3 brancher_epidemio.py          # branche ou met à niveau les 4 fichiers, et dit ce qu'il a fait pour chacun
   sudo systemctl restart matuscore
   ```
   Le script traite chaque fichier en « tout ou rien » : si l'un diffère de la version attendue, il n'est pas touché et le script te le
   dit, sans bloquer les autres. Les sauvegardes `*.avant_epidemio` sont les fichiers d'origine.
3. **Premier remplissage par commune** (une seule fois ; environ 20 unités d'appel Open-Meteo par position) :
   ```bash
   cd ~/Matuscore && set -a && source .env && set +a
   venv/bin/python3 epidemio_pilot.py communes
   ```
   La commande calcule chaque commune, affiche pour chacune la tendance et la prochaine infection, et liste les clients qu'elle ne sait
   pas situer. Sans ce remplissage, la première génération de bulletins en lot ferait ce travail, plus lentement.

## Vérifier
* `venv/bin/python3 epidemio_pilot.py communes` : une ligne par commune. Une ligne « SANS POSITION » indique un client à localiser :
  renseigne latitude et longitude dans sa fiche.
* Navigateur, connecté en admin : `/api/epidemio-moteur/communes` (même contenu), `/api/epidemio-moteur/etat` (appels consommés).
* Génère un bulletin client et regarde le paragraphe « Situation sur votre commune ».
* Tests : `cd ~/Matuscore && EPIDEMIO_PATH=~/epidemio venv/bin/python3 test_epidemio_pilot.py`.

## « Je ne vois rien de nouveau dans le portail ou dans le PDF du portail »
Ce que voit un client ne vient pas de l'onglet admin tant que tu n'as pas fait ces deux gestes :
1. **Sauvegarder** le bulletin (bouton de l'onglet bulletin). Le portail affiche le dernier bulletin *enregistré* : un texte généré mais non
   sauvegardé n'existe que dans le formulaire.
2. **📁 Générer et stocker les bulletins** (choisis un client, ou aucun pour tous). **Les PDF du portail sont des fichiers stockés** : ils
   gardent le contenu de la dernière fois et ne se mettent pas à jour tout seuls.
Pour tester sans attendre, ouvre le PDF d'un client depuis l'admin (il est généré à la demande) : tu dois y voir « Recommandation : », le
paragraphe « Situation sur votre commune » et le tableau de prévisions piloté par le moteur. Côté navigateur, recharge la page du portail une
fois (ou deux) après la mise à jour.

## Dépannage
* **« No module named 'historique_meteo' »** : le dépôt `epidemio` du VPS n'a pas le nouveau fichier. Pousse la nouvelle version depuis le PC
  (`git add .`, `git commit -m "..."`, `git push` dans le dossier du dépôt epidemio), puis `cd ~/epidemio && git pull` et `ls historique_meteo.py`.
  Un `git pull` qui répond « Already up to date » alors que le fichier manque signifie que rien n'a été poussé depuis le PC.
  **Tant que ce fichier manque, Pilot fonctionne mais le mildiou de la génération de texte retombe sur l'ancien modèle et les bulletins
  n'ont pas le paragraphe par commune.** Mets d'abord à jour `epidemio`, puis Pilot.
* **« REFUSÉ, rien n'a été écrit »** : le fichier cité diffère de la version attendue. Il n'a pas été touché, les autres le sont. La ligne
  contient le texte trouvé à la place : copie-la, elle suffit en général pour adapter le script.
* **Le script est sans danger à relancer** : il ne refait que ce qui manque.

## Revenir en arrière
```bash
cd ~/Matuscore && python3 brancher_epidemio.py --retirer && sudo systemctl restart matuscore
```

## Comment une commune est située
Ordre : coordonnées de la fiche client (si plausibles), sinon géocodage du nom de la commune (France uniquement, résultat accepté seulement
dans la zone Champagne, la Marne en premier, mémorisé dans la base), sinon le client garde l'analyse régionale. Un nom ambigu peut être mal
résolu : vérifie la sortie de la commande et corrige par les coordonnées de la fiche.

## Réglages (fichier `.env`, tous facultatifs)
| Variable | Défaut | Rôle |
|---|---|---|
| `EPIDEMIO_PATH` | `~/epidemio` | dossier du dépôt epidemio |
| `EPIDEMIO_DATA_DIR` | `~/epidemio_data` | historique météo et géocodages (à garder) |
| `EPIDEMIO_ARCHIVE` | `1` | `0` : jamais d'API historique (plan Standard d'Open-Meteo) |
| `EPIDEMIO_TTL_S` | `3600` | durée avant un nouveau rafraîchissement de la prévision |
| `EPIDEMIO_PROFIL` | `calage_2026` | profil du moteur |

## Limites à connaître
* **Résolution de la météo** : les communes proches (quelques kilomètres) tombent souvent dans la même maille de la réanalyse ; leurs
  résultats diffèrent surtout par la prévision à court terme. L'écart se voit surtout les jours d'orage.
* Dans le tableau des prévisions, les colonnes de météo (températures, pluie) viennent toujours de l'ancienne récupération ; seule la ligne
  « Risque contam. » et l'encadré viennent du moteur. La ligne « Source : Open-Meteo — Épernay » est écrite en dur dans le générateur : elle
  indique Épernay même pour un client d'une autre commune.
* Open-Meteo : offre gratuite réservée à un usage non commercial (voir le guide précédent pour les limites et les plans).
