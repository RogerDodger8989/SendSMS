@echo off
title SendSms - Aterstall PIN
color 0C

echo ============================================
echo   SendSms - Aterstall PIN-kod
echo ============================================
echo.
echo VARNING: Detta nollstaller din PIN-kod.
echo Du valjer en ny PIN nasta gang du oppnar appen.
echo Din historik och installningar beroras INTE.
echo.
set /p confirm="Ar du saker? Skriv JA och tryck Enter: "
if /i not "%confirm%"=="JA" (
    echo Avbrutet.
    pause
    exit /b 0
)

if not exist data\sms_logg.db (
    echo.
    echo [FEL] Databasen hittades inte.
    echo       Har du startat appen minst en gang?
    pause
    exit /b 1
)

call venv\Scripts\activate.bat 2>nul

python -c "import sqlite3; conn = sqlite3.connect('data/sms_logg.db'); conn.execute(\"UPDATE settings SET value = '' WHERE key = 'app_pin'\"); conn.commit(); conn.close(); print('[OK] PIN-koden ar nollstalld!')"

echo.
echo Starta appen igen - valj ny PIN pa inloggningsskärmen.
echo.
pause
