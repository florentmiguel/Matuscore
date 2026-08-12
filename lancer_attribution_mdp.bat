@echo off
title MatuScore - Attribution des mots de passe
cd /d "%~dp0"

py -3.13 --version >nul 2>&1
if %errorlevel% neq 0 (set PYTHON=python) else (set PYTHON=py -3.13)

echo Verification de Flask/Werkzeug...
%PYTHON% -m pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo Installation de Flask ^(necessaire pour ce script^)...
    %PYTHON% -m pip install flask --quiet
)

echo.
%PYTHON% attribuer_mots_de_passe.py
pause >nul
