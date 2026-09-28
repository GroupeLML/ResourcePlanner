@echo off
setlocal
cd /d "%~dp0"

echo OUTIL OPTIONNEL - donnees de demonstration SQLite uniquement.
echo Le demarrage normal de RessourcePlanner ne requiert pas ce script.
echo.

if not exist ".venv-web\Scripts\python.exe" (
    echo L'environnement Python Web .venv-web est introuvable.
    echo Lance Installer_Web.bat avant de charger les donnees demo.
    pause
    exit /b 1
)

set "RESOURCEPLANNER_DATABASE_URL=sqlite:///./resourceplanner_server.db"

echo Preparation de la base SQLite locale...
".venv-web\Scripts\python.exe" -m alembic upgrade head
if errorlevel 1 (
    echo.
    echo Echec des migrations Alembic.
    pause
    exit /b 1
)

echo.
echo Chargement des donnees de demonstration...
".venv-web\Scripts\python.exe" tools\seed_demo_data.py
set EXIT_CODE=%errorlevel%

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Le chargement des donnees demo a echoue avec le code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

echo.
echo Donnees demo pretes dans resourceplanner_server.db.
echo Elles ont ete chargees uniquement parce que cet outil a ete lance explicitement.
echo Le runtime normal peut etre lance sans donnees demo avec Lancer_Web.bat.
echo Pour le developpement React avec Vite, Lancer_Serveur.bat + npm run dev restent disponibles.
pause
exit /b 0
