@echo off
title SendSms - Server körs...
color 0B

echo ============================================
echo   SendSms - Startar server...
echo ============================================
echo.

if not exist venv (
    echo [FEL] Appen är inte installerad!
    echo Kör "Installera SendSms.bat" forst.
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    set "LOCAL_IP=%%a"
    goto :found_ip
)
:found_ip
set LOCAL_IP=%LOCAL_IP: =%

echo ============================================
echo   Servern ar igång!
echo ============================================
echo.
echo   Oppna på DENNA dator:
echo   http://localhost:5000
echo.
echo   Oppna på MOBIL (samma WiFi):
echo   http://%LOCAL_IP%:5000
echo.
echo ============================================
echo.
echo Stang detta fönster for att stoppa servern.
echo.

timeout /t 2 /nobreak >nul
start http://localhost:5000

python app.py
