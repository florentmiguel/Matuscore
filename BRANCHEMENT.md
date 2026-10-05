# Brancher le moteur épidémiologique sur Pilot

## Ce que ça change
* **Génération de texte du bulletin** (`/api/generer-texte-bulletin`) : pour le **mildiou**, le prompt reçoit maintenant la
  synthèse du nouveau moteur (infections primaires et secondaires récentes et prévues, sorties de taches attendues, nuits de
  fructification, potentiel des 7 prochains jours). L'**oïdium** et les **fenêtres de traitement** restent sur l'ancien modèle.
* **Nouvelle route** `GET /api/epidemio-moteur?lat=..&lon=..` (réservée à l'admin, comme les autres routes `/api/`).
  `&complet=1` ajoute la sortie brute du moteur.
* **Rien d'autre ne change** : pages, base de données, ancienne route `/api/epidemio`. Si le moteur est absent ou en panne,
  Pilot démarre et se comporte exactement comme avant.

## Installation
1. **Sur le PC**, copie `epidemio_pilot.py`, `brancher_epidemio.py` et `test_epidemio_pilot.py` dans le dossier du dépôt
   Matuscore (à côté de `serveur_vitisens.py`), puis :
   ```powershell
   git add .
   git commit -m "Pont moteur épidémiologique"
   git push
   ```
2. **Sur le VPS** :
   ```bash
   cd ~/Matuscore
   git pull
   echo 'EPIDEMIO_PATH=/home/ubuntu/epidemio' >> .env        # une seule fois : chemin explicite du moteur
   python3 brancher_epidemio.py
   sudo systemctl restart matuscore
   sudo systemctl status matuscore
   ```
   Le script vérifie que `serveur_vitisens.py` est bien la version attendue. S'il ne l'est pas, **il n'écrit rien** et te le dit.
   Il garde une copie `serveur_vitisens.py.avant_epidemio`.

## Vérifier
* Journal : `sudo journalctl -u matuscore -n 40 | grep epidemio` ne doit montrer **aucun** « moteur non branché ».
* Navigateur, connecté en admin : ouvre `/api/epidemio-moteur` sur le domaine de Pilot. La première réponse prend quelques
  secondes (téléchargement de la météo de la saison), les suivantes sont immédiates (cache d'une heure).
* Tests (dans le venv de Pilot, qui a Flask) :
  ```bash
  cd ~/Matuscore && EPIDEMIO_PATH=~/epidemio venv/bin/python3 test_epidemio_pilot.py
  ```
* Bulletin : lance la génération de texte et relis le résultat, comme d'habitude.

## Revenir en arrière
```bash
cd ~/Matuscore && python3 brancher_epidemio.py --retirer && sudo systemctl restart matuscore
```

## Réglages (fichier `.env`, tous facultatifs)
| Variable | Défaut | Rôle |
|---|---|---|
| `EPIDEMIO_PATH` | `~/epidemio` | dossier du dépôt epidemio |
| `EPIDEMIO_PROFIL` | `calage_2026` | profil de paramètres du moteur |
| `EPIDEMIO_TTL_S` | `3600` | durée de vie du cache météo, en secondes |
| `EPIDEMIO_CACHE_DIR` | `/tmp/epidemio_cache` | dossier du cache météo |
| `EPIDEMIO_LAT`, `EPIDEMIO_LON` | `49.25`, `3.96` | position par défaut |

## Limites à connaître
* **Une seule position** pour l'instant (celle de la requête, ou Reims par défaut), pas une position par client.
* Le moteur évalue un **danger théorique indépendant des traitements** ; le prompt demande à l'IA de ne jamais parler de
  contamination avérée. Le conseil reste celui du conseiller : relis toujours le texte avant de l'enregistrer.
* **Open-Meteo** : l'offre gratuite est réservée à un usage non commercial. Pilot est un service payant : à régulariser
  (offre commerciale ou autre source météo) avant de multiplier les positions.
* Le profil `calage_2026` est calé sur les observations 2026 en Champagne ; il reste à confirmer sur d'autres campagnes.
