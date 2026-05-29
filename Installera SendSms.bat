@echo off
title SendSms - Installation
color 0A

echo ============================================
echo   SendSms - Installation
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [FEL] Python hittades inte!
    echo.
    echo Ladda ner Python 3.11+ fran: https://www.python.org/downloads/
    echo VIKTIGT: Kryssa i "Add Python to PATH" vid installationen!
    echo.
    pause
    start https://www.python.org/downloads/
    exit /b 1
)

echo [OK] Python hittades!
python --version
echo.

echo Skapar virtuell miljo...
if exist venv (
    echo [OK] Virtuell miljo finns redan.
) else (
    python -m venv venv
    echo [OK] Virtuell miljo skapad!
)
echo.

echo Installerar Python-beroenden...
call venv\Scripts\activate.bat
pip install -r requirements.txt --quiet
echo [OK] Beroenden installerade!
echo.

if not exist data mkdir data
echo [OK] Data-mapp kontrollerad.
echo.

echo Skapar brandvaggsregel...
netsh advfirewall firewall delete rule name="SendSms" >nul 2>&1
netsh advfirewall firewall add rule name="SendSms" dir=in action=allow protocol=TCP localport=5000 >nul 2>&1
if errorlevel 1 (
    echo [VARNING] Brandvaggsregel misslyckades - kör som Administratör for mobil-atkomst.
) else (
    echo [OK] Brandvaggsregel skapad!
)
echo.

echo ============================================
echo   Installation klar!
echo ============================================
echo.
echo Kör "Starta SendSms.bat" for att starta appen.
echo.
pause
