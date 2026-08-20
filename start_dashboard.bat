@echo off
cd /d "%~dp0"
echo ====================================================
echo   Portal Pi Dashboard
echo ====================================================
echo.
echo Iniciando servidor en http://localhost:8420 ...
echo.
start "" python scripts\run_dashboard.py
timeout /t 4 /nobreak >nul
start http://localhost:8420
echo.
echo El dashboard esta corriendo en http://localhost:8420
echo Cierra la ventana de Python para pararlo.
