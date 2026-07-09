@echo off
title Afiliados Cleantech
cd /d "%~dp0"
echo.
echo   Iniciando Afiliados Cleantech en http://localhost:5003
echo   Admin: http://localhost:5003/admin?clave=admin123
echo.
python app.py
pause
