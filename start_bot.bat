@echo off
echo ===============================
echo STARTING CRYPTO BOT
echo ===============================

REM Start WhatsApp webhook
start "Webhook" cmd /k python whatsapp_webhook.py

REM Start ngrok
start "Ngrok" cmd /k C:\ngrok\ngrok.exe http 5000

REM Start multi coin scanner
start "Scanner" cmd /k python analysis\multi_coin_score.py

echo ===============================
echo BOT IS LIVE
echo ===============================
pause
