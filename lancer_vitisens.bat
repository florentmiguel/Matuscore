@echo off
title VITI Sens - Serveur local
cd /d "%~dp0"

py -3.13 --version >nul 2>&1
if %errorlevel% neq 0 (set PYTHON=python) else (set PYTHON=py -3.13)

echo Installation des dependances...
%PYTHON% -m pip install flask python-docx requests openpyxl pdfplumber stripe --quiet 2>nul
%PYTHON% -m pip install weasyprint --quiet 2>nul
echo (Si les PDF Exploitation/Itineraire/Rendements restent en HTML a l'impression,
echo  c'est que weasyprint necessite le runtime GTK3 sur Windows - voir GUIDE_DEPLOIEMENT.md)

set DOMAIN=http://matuscore.localhost:5000
set ADMIN_PASSWORD=VitiSens2026Matu

echo.
echo Lancement du serveur VITI Sens...
echo Le navigateur va s'ouvrir automatiquement sur matuscore.localhost.
echo (Aucune configuration requise : les navigateurs resolvent automatiquement
echo  tout sous-domaine de .localhost vers ce PC.)
echo.
echo Acces admin : http://matuscore.localhost:5000/admin-connexion
echo Mot de passe admin : VitiSens2026Matu  (modifiable dans ce fichier .bat)
echo.
echo Pour arreter : fermez cette fenetre ou Ctrl+C
echo.

start http://matuscore.localhost:5000/admin-connexion
%PYTHON% serveur_vitisens.py

pause
