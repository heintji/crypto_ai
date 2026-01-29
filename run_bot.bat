@echo off
setlocal

REM === GA NAAR JE PROJECT MAP ===
cd /d C:\Users\heint\crypto_ai

REM === (OPTIONEEL) VENV ACTIVEREN ALS DIE BESTAAT ===
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"

REM === START JOUW BOT SCRIPT ===
REM Kies hier 1 bestand om automatisch te starten:

python .\analysis\multi_coin_score.py

REM (Als jij juist multi_coin_log wil starten, gebruik dan deze en zet de andere uit:)
REM python .\multi_coin_log.py

echo.
echo Bot is gestopt. Druk op een toets om af te sluiten...
pause >nul
endlocal
