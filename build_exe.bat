@echo off
echo ============================================
echo  Bygger SendSMS.exe
echo ============================================
echo.

:: Aktivera venv
call venv\Scripts\activate.bat

:: Installera PyInstaller om det saknas
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installerar PyInstaller...
    pip install pyinstaller
)

:: Bygg exe
echo.
echo Bygger...
pyinstaller SendSms.spec --noconfirm --clean

echo.
if exist "dist\SendSMS.exe" (
    echo ============================================
    echo  KLAR!  dist\SendSMS.exe ar redo.
    echo ============================================
    explorer dist
) else (
    echo BYGGFEL - kontrollera felmeddelandena ovan.
)
pause
