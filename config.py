"""
CRYPTO_AI - CONFIG
Alle vaste instellingen staan hier.
Geen logica hier, alleen waarden.
"""

# === BOT RUN SETTINGS ===
RUN_EVERY_MINUTES = 30

# === MARKET SETTINGS ===
BASE_QUOTE = "USDT"
TIMEFRAME_PREBUY = "4h"
TIMEFRAME_TRADE = "1h"

# === RISK SETTINGS ===
# Max risico per trade in % van je account (bijv. 0.5 = 0.5%)
MAX_RISK_PER_TRADE_PCT = 0.5

# Max aantal gelijktijdige open trades
MAX_OPEN_TRADES = 2

# Cooldown na een BUY (in minuten) voordat je opnieuw BUY mag doen op dezelfde coin
COOLDOWN_MINUTES = 60

# Dag- en weekrem (kapitaalbescherming)
# Bijv. -2.0 betekent: stop met nieuwe trades als je -2% dagverlies hebt
DAILY_STOP_LOSS_PCT = -2.0
WEEKLY_STOP_LOSS_PCT = -5.0

# === EXIT / SELL MANAGEMENT ===
# Weak-sell levels: percentages van de positie die verkocht worden
WEAK_SELL_1_PCT = 0.65   # 65%
WEAK_SELL_2_PCT = 0.35   # 35%

# === ATR STOP SETTINGS ===
# Stop Loss op basis van ATR:
# SL = entry - (ATR * ATR_MULTIPLIER) voor long trades
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0

# === RSI SETTINGS ===
RSI_PERIOD = 14
RSI_PREBUY_MIN = 40   # voorbeeldzone voor voorbereiding
RSI_PREBUY_MAX = 65

# === SMA SETTINGS ===
SMA_FAST = 20
SMA_SLOW = 50

# === LOGGING SETTINGS ===
LOG_DIR = "logs"
TRADES_LOG_FILE = "trades_log.csv"
EVENTS_LOG_FILE = "events_log.csv"

# === PAPER TRADING ===
PAPER_TRADING = True

# === WATCHLIST / COINS ===
# (Als je coins ergens anders beheert, mag deze lijst leeg blijven.)
DEFAULT_COINS = [
    "BTCUSDT",
    "ETHUSDT",
]
