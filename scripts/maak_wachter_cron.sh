#!/bin/bash
# Maakt de Render-cron aan die de Gate V1-bot bewaakt (elke 15 minuten).
#
# JIJ draait dit, want het maakt een nieuwe Render-service aan en die kost geld
# (starter-cron, ongeveer een dollar per maand).
#
# De geheimen worden rechtstreeks van een bestaande crypto_ai-service
# overgenomen via de Render-API; ze komen nergens in beeld en staan niet in dit
# bestand.
set -e
BOT=~/dev/whatsapp-mkb-bot
KEY=$(grep -E '^(export )?RENDER_API_KEY=' $BOT/.env | tail -1 | sed 's/.*RENDER_API_KEY=//' | tr -d '"')
python3 ~/dev/crypto_ai/scripts/_maak_cron.py "$KEY"
