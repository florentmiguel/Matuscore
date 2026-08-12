# VITI Sens — Déploiement Railway + Stripe

Guide pas à pas pour mettre en ligne la partie **Vendanges** (essai gratuit + accès payant)
sur Railway. Ton serveur Windows local continue de fonctionner comme avant pour les
bulletins de conseil de tes 18 clients — les deux sont indépendants.

---

## 1. Créer le compte Stripe et les produits

1. Crée un compte sur [stripe.com](https://stripe.com) si ce n'est pas déjà fait (ton entreprise
   SASU VITI Sens peut être utilisée directement).
2. Dans le dashboard Stripe → **Produits** → **Ajouter un produit**, crée **deux prix** :
   - **Tarif normal** : `MatuScore — Accès complet` — `89 €`, récurrence **Annuelle**
   - **Offre de lancement** : `MatuScore — Accès complet (lancement)` — `59 €`, récurrence **Annuelle**
     (tu peux les mettre sur le même produit, avec deux prix différents, ou deux produits séparés — les deux marchent)
3. Une fois créés, note les **deux Price ID** (commencent par `price_...`) — tu en auras besoin.
4. Dans **Développeurs → Clés API**, note :
   - `Clé secrète` (commence par `sk_live_...` en production, `sk_test_...` en test)
5. Tu créeras le **Webhook** à l'étape 5, une fois l'URL Railway connue.

**Comment ça marche côté appli** : les 100 premiers comptes inscrits (réglable via la variable
`LIMITE_OFFRE_LANCEMENT`) voient automatiquement le tarif à 59 €/an, sur la page d'inscription,
dans la bannière d'essai, et lors du paiement. Les inscrits suivants voient directement 89 €/an —
pas d'action de ta part une fois configuré, la bascule est automatique.

**Conseil** : commence en mode **Test** (clés `sk_test_...`) pour vérifier que tout
fonctionne avant de basculer en mode live.

---

## 2. Installer et lancer Railway

```bash
# Installer le CLI Railway (une seule fois)
npm install -g @railway/cli

# Se connecter
railway login

# Depuis le dossier du projet (celui qui contient serveur_vitisens.py)
railway init
```

Choisis "Empty project" quand demandé, donne-lui un nom (ex. `vitisens-vendanges`).

---

## 3. Ajouter un volume persistant (pour la base SQLite)

Sans ça, la base de données (tous les comptes vignerons inscrits) serait effacée à
chaque redéploiement.

```bash
railway volume add
```
- Point de montage : `/data`

---

## 4. Configurer les variables d'environnement

Sur le dashboard Railway (railway.app) → ton projet → **Variables**, ajoute :

| Variable | Valeur |
|---|---|
| `DB_PATH` | `/data/vitisens.db` |
| `ADMIN_PASSWORD` | *(choisis un mot de passe fort pour ton dashboard admin)* |
| `FLASK_SECRET_KEY` | *(une chaîne aléatoire longue, ex. générée avec `python -c "import secrets;print(secrets.token_hex(32))"`)* |
| `SMTP_USER` | `florent.miguel@sasu-viti-sens.fr` |
| `SMTP_PASSWORD` | *(le mot de passe de cette boîte mail OVH)* |
| `STRIPE_SECRET_KEY` | *(ta clé secrète Stripe, `sk_test_...` pour commencer)* |
| `STRIPE_PRICE_ID` | *(le Price ID du produit à 89 €/an)* |
| `STRIPE_PRICE_ID_LANCEMENT` | *(le Price ID du produit à 59 €/an, offre de lancement)* |
| `LIMITE_OFFRE_LANCEMENT` | `100` *(nombre de comptes éligibles au tarif de lancement — optionnel, 100 par défaut)* |
| `STRIPE_WEBHOOK_SECRET` | *(voir étape 5, à ajouter après le premier déploiement)* |
| `DOMAIN` | `https://vitisens-vendanges.up.railway.app` *(ou ton domaine perso une fois branché)* |

---

## 5. Déployer

```bash
railway up
```

Railway détecte automatiquement `requirements.txt` et `Procfile` et démarre le serveur.
Une fois en ligne, récupère l'URL publique (railway.app → **Settings → Domains → Generate Domain**).

Mets à jour la variable `DOMAIN` avec cette URL exacte, puis redéploie :
```bash
railway up
```

---

## 6. Créer le webhook Stripe

Maintenant que tu as l'URL Railway :

1. Stripe dashboard → **Développeurs → Webhooks → Ajouter un endpoint**
2. URL : `https://TON-URL-RAILWAY/api/stripe/webhook`
3. Événements à écouter :
   - `checkout.session.completed`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Une fois créé, copie le **Signing secret** (`whsec_...`) et ajoute-le comme variable
   Railway `STRIPE_WEBHOOK_SECRET`, puis redéploie (`railway up`).

---

## 7. Tester le parcours complet

1. Va sur `https://TON-URL/inscription` → crée un compte test
2. Vérifie que tu es bien redirigé vers ton portail (`/portail/<token>`)
3. Ajoute 3 parcelles → la 4ᵉ doit être bloquée avec le message d'essai
4. Clique sur "Passer à l'accès complet" → tu dois arriver sur la page Stripe Checkout
5. Utilise une [carte de test Stripe](https://stripe.com/docs/testing) (`4242 4242 4242 4242`,
   n'importe quelle date future, n'importe quel CVC) pour valider un paiement
6. Vérifie que le compte passe bien en statut "payant" (le quota ne bloque plus)
7. Va sur `https://TON-URL/admin-vitisens` → vérifie qu'il te demande le mot de passe admin

Une fois ces tests validés, repasse les clés Stripe en mode **live** (`sk_live_...`,
nouveau webhook en mode live) pour ouvrir aux vraies inscriptions.

---

## Rappels importants

- **Bulletins de conseil (tes 18 clients)** : continuent de fonctionner uniquement sur ton
  PC Windows local (`lancer_vitisens.bat`), car la génération PDF utilise Word — non
  disponible sur Railway (Linux). Les deux serveurs sont indépendants et ont chacun leur
  propre base de données.
- **Export PDF exploitation/itinéraire/rendements** (partie Vendanges) : utilise `weasyprint`
  pour produire de vrais PDF (pas une impression d'écran). Sur Railway, le fichier
  `nixpacks.toml` fourni installe automatiquement les bibliothèques système nécessaires —
  rien à faire de plus. Si jamais ça ne suffit pas, ajoute cette variable d'environnement
  sur Railway : `NIXPACKS_PKGS` = `libgobject-2.0-0 libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`
  (solution alternative documentée pour ce cas précis sur Railway).
- **Export PDF en local sur Windows** : `weasyprint` a besoin du runtime GTK3, qui n'est pas
  toujours présent sur Windows par défaut. `lancer_vitisens.bat` tente de l'installer, mais
  si les PDF continuent à s'ouvrir en HTML imprimable (`Ctrl+P`) en local, c'est ce runtime
  qui manque — installe-le depuis [ce lien](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases)
  et redémarre ton PC. Ce n'est utile que pour tester en local : une fois déployé sur
  Railway, les PDF sont corrects sans rien installer côté Windows.
- **Mot de passe admin** : ne le partage avec personne, c'est ce qui protège les données de
  tes clients existants.
