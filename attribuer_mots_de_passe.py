#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Attribue un mot de passe à chaque client "admin" (tes vrais clients, pas les
inscriptions publiques) qui n'en a pas encore, pour qu'ils puissent se connecter
via /connexion avec leur email — exactement comme les comptes MatuScore.

Usage :
    python attribuer_mots_de_passe.py

À lancer une seule fois, depuis le dossier contenant vitisens.db (le même dossier
que serveur_vitisens.py). Sans risque : ne touche qu'aux clients qui n'ont pas
encore de mot de passe, ne modifie rien d'autre.

Le résultat (email + mot de passe en clair, à transmettre) est écrit dans
mots_de_passe_clients.csv à côté de ce script — à supprimer une fois les mots de
passe communiqués, puisqu'ils y sont en clair.
"""
import sqlite3, secrets, string, csv, os, sys

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("Erreur : werkzeug n'est pas installé (pip install flask).")
    sys.exit(1)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vitisens.db")
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mots_de_passe_clients.csv")

ALPHABET = string.ascii_lowercase + string.digits
ALPHABET = ALPHABET.replace('l', '').replace('1', '').replace('o', '').replace('0', '')  # évite les confusions

def generer_mot_de_passe(longueur=8):
    return ''.join(secrets.choice(ALPHABET) for _ in range(longueur))

def main():
    if not os.path.exists(DB_PATH):
        print(f"Erreur : {DB_PATH} introuvable. Lance ce script depuis le dossier du projet.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    clients = conn.execute("""
        SELECT id, exploitation, email FROM clients
        WHERE origine != 'inscription'
          AND (password_hash IS NULL OR password_hash = '')
          AND email IS NOT NULL AND email != ''
        ORDER BY exploitation
    """).fetchall()

    if not clients:
        print("Aucun client à traiter — tous ont déjà un mot de passe (ou pas d'email renseigné).")
        conn.close()
        return

    print(f"{len(clients)} client(s) vont recevoir un mot de passe :\n")

    resultats = []
    for c in clients:
        mdp = generer_mot_de_passe()
        conn.execute("UPDATE clients SET password_hash=? WHERE id=?",
                     (generate_password_hash(mdp), c['id']))
        resultats.append((c['exploitation'], c['email'], mdp))
        print(f"  {c['exploitation']:35s} {c['email']:35s} {mdp}")

    conn.commit()
    conn.close()

    with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Exploitation', 'Email', 'Mot de passe'])
        w.writerows(resultats)

    print(f"\n✅ {len(resultats)} mot(s) de passe créé(s).")
    print(f"📄 Liste enregistrée dans : {CSV_PATH}")
    print("⚠️  Ce fichier contient des mots de passe en clair — à supprimer une fois")
    print("   les identifiants communiqués à tes clients (ou gardés pour toi).")
    print("\nConnexion : https://portail.sasu-viti-sens.fr/connexion (ou ton domaine)")

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\n❌ Erreur : {e}")
    input("\nAppuie sur Entrée pour fermer cette fenêtre...")
