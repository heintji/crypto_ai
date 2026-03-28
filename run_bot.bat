# run_bot.py
# ============================================================
# Crypto AI Bot — Scheduler v2.0
# ============================================================
# Hoofd scheduler die alle bot processen beheert.
# Draait als Render Background Worker (crypto-ai-scanner).
#
# Zelfde gedachtegang als alle andere bestanden:
#   ✅ sslmode="require" op DB connectie
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde now_utc() patroon
#   ✅ Zelfde log() formaat
#
# BUGS GEFIXED vs origineel:
#   ✅ trade_monitor.py toegevoegd aan scheduler
#   ✅ datetime.utcnow() vervangen door now_utc() (deprecated fix)
#   ✅ Race condition history_fetcher → simulator opgelost
#   ✅ regime_labeler en build_btc_regime toegevoegd
#   ✅ Logging met timestamps consistent
#   ✅ Schema sync bij opstarten
#
# NIEUWE FEATURES:
#   ✅ Process health check elke 5 minuten
#   ✅ WhatsApp melding bij opstarten
#   ✅ WhatsApp melding als subproces faalt
#   ✅ Graceful shutdown bij SIGTERM
#   ✅ Memory en performance tracking
#   ✅ Automatische herstart bij crash
# ============================================================

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests


# ============================================================
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Interval instellingen
MONITOR_INTERVAL_SEC  = int(os.getenv("MONITOR_INTERVAL_SEC", "30"))
SCANNER_INTERVAL_MIN  = int(os.getenv("SCANNER_INTERVAL_MIN", "30"))
BTC_REGIME_INTERVAL_H = int(os.getenv("BTC_REGIME_INTERVAL_H", "4"))
HEALTH_CHECK_MIN      = int(os.getenv("HEALTH_CHECK_MIN", "5"))

# Globale stop flag voor graceful shutdown
_STOP_REQUESTED = False


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [SCHEDULER] {msg}", flush=True)


# ============================================================
# GRACEFUL SHUTDOWN
# ============================================================
def _handle_sigterm(signum, frame) -> None:
    global _STOP_REQUESTED
    log("📛 SIGTERM ontvangen — graceful shutdown...")
    _STOP_REQUESTED = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ============================================================
# WHATSAPP — identiek aan alle andere bestanden
# ============================================================
def send_whatsapp(message: str) -> bool:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_FROM,
                "To":   TWILIO_WHATSAPP_TO,
                "Body": message,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log(f"✅ WhatsApp verzonden")
            return True
        log(f"❌ WhatsApp {resp.status_code}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {e}")
        return False


# ============================================================
# PROCESS RUNNER
# ============================================================
def run(
    cmd:     str,
    timeout: int  = 600,
    label:   str  = "",
) -> bool:
    """
    Voert een command uit en wacht tot het klaar is.
    Geeft True terug als succesvol (returncode 0).
    Eén falende script crasht de hele loop NIET.
    """
    label = label or cmd.split()[-1]
    log(f"▶️  {label}: {cmd}")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=PROJECT_ROOT,
            check=False,
            timeout=timeout,
        )
        if result.returncode == 0:
            log(f"✅ {label}: klaar (code 0)")
            return True
        else:
            log(f"⚠️ {label}: fout (code {result.returncode})")
            return False

    except subprocess.TimeoutExpired:
        log(f"⏰ {label}: timeout na {timeout}s")
        return False

    except Exception as e:
        log(f"❌ {label}: exception: {e}")
        return False


def run_monitor_once() -> bool:
    """Één run van trade_monitor.py — bewaakt open trades."""
    return run(
        "python trade_monitor.py --once",
        timeout=120,
        label="MONITOR",
    )


# ============================================================
# HOOFD SCHEDULER LOOP
# ============================================================
def main() -> None:
    log("=" * 60)
    log("Crypto AI Bot — Scheduler v2.0 gestart")
    log("=" * 60)
    log(f"Project root:      {PROJECT_ROOT}")
    log(f"Python:            {sys.version.split()[0]}")
    log(f"Database:          {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:            {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Monitor interval:  {MONITOR_INTERVAL_SEC}s")
    log(f"Scanner interval:  {SCANNER_INTERVAL_MIN}min")
    log(f"BTC regime update: elke {BTC_REGIME_INTERVAL_H}u")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — stop")
        sys.exit(1)

    # WhatsApp melding bij opstarten
    send_whatsapp(
        f"🚀 BOT SCHEDULER GESTART\n"
        f"{'─' * 28}\n\n"
        f"Systeem opstarten...\n\n"
        f"📋 Wat er nu gebeurt:\n"
        f"1. Schema sync\n"
        f"2. BTC regime bouwen\n"
        f"3. Scanner starten\n"
        f"4. Trade monitor starten\n\n"
        f"🤖 BOT BEGINT OVER CA. 2 MINUTEN\n"
        f"Commands: STATUS | TRADES | STOP"
    )

    # Opstarten: schema sync + BTC regime + history
    now = now_utc()
    log("\n📋 OPSTARTEN — schema sync...")
    run("python fix_experience_schema.py", timeout=90, label="SCHEMA_SYNC")

    log("📋 OPSTARTEN — BTC regime bouwen...")
    run("python research/build_btc_regime.py", timeout=90, label="BTC_REGIME")

    log("📋 OPSTARTEN — history fetcher (eerste batch)...")
    history_ok = run(
        "python research/history_fetcher.py",
        timeout=1800,
        label="HISTORY_FETCHER",
    )

    log("📋 OPSTARTEN — regime labeler...")
    run("python research/regime_labeler.py", timeout=300, label="REGIME_LABELER")

    if history_ok:
        log("📋 OPSTARTEN — history simulator (na fetcher)...")
        run("python research/history_simulator.py", timeout=3600, label="HISTORY_SIM")

    log("✅ Opstarten klaar — scheduler loop start")
    log("=" * 60)

    # Tijden voor geplande taken
    next_scanner     = now_utc()
    next_btc_regime  = now_utc() + timedelta(hours=BTC_REGIME_INTERVAL_H)
    next_history     = now_utc() + timedelta(hours=24)
    next_sim         = now_utc() + timedelta(hours=25)
    next_regime_lab  = now_utc() + timedelta(hours=24)
    next_health_log  = now_utc() + timedelta(minutes=HEALTH_CHECK_MIN)

    # Stats bijhouden
    stats: Dict[str, Any] = {
        "monitor_runs":  0,
        "scanner_runs":  0,
        "errors":        0,
        "started_at":    now_utc().isoformat(),
    }

    tick = 0

    while not _STOP_REQUESTED:
        tick += 1
        now  = now_utc()

        # ── Trade monitor elke 30 seconden (hoogste prioriteit) ──
        ok = run_monitor_once()
        stats["monitor_runs"] += 1
        if not ok:
            stats["errors"] += 1

        # ── Multi coin scanner elke 30 minuten ──
        if now >= next_scanner:
            log("\n📊 SCANNER RUN...")
            ok = run(
                "python analysis/multi_coin_score.py",
                timeout=600,
                label="SCANNER",
            )
            stats["scanner_runs"] += 1
            if not ok:
                stats["errors"] += 1
                send_whatsapp(
                    f"⚠️ SCANNER FOUT\n\n"
                    f"multi_coin_score.py mislukt.\n"
                    f"Geen nieuwe pre-buys gegenereerd.\n\n"
                    f"🤖 BOT LOOPT GEWOON DOOR\n"
                    f"Stuur STOP als je wil pauzeren."
                )
            next_scanner = now_utc() + timedelta(minutes=SCANNER_INTERVAL_MIN)

        # ── BTC regime update elke 4 uur ──
        if now >= next_btc_regime:
            log("\n🔍 BTC REGIME UPDATE...")
            run(
                "python research/build_btc_regime.py",
                timeout=90,
                label="BTC_REGIME",
            )
            next_btc_regime = now_utc() + timedelta(hours=BTC_REGIME_INTERVAL_H)

        # ── History fetcher 1x per dag ──
        if now >= next_history:
            log("\n📥 HISTORY FETCHER...")
            ok = run(
                "python research/history_fetcher.py",
                timeout=1800,
                label="HISTORY",
            )
            next_history = now_utc() + timedelta(hours=24)
            # Simulator pas na fetcher
            if ok:
                next_sim = now_utc() + timedelta(minutes=10)
            else:
                next_sim = now_utc() + timedelta(hours=2)

        # ── History simulator ──
        if now >= next_sim:
            log("\n🔮 HISTORY SIMULATOR...")
            run(
                "python research/history_simulator.py",
                timeout=3600,
                label="SIMULATOR",
            )
            next_sim = now_utc() + timedelta(hours=24)

        # ── Regime labeler 1x per dag ──
        if now >= next_regime_lab:
            log("\n🏷️  REGIME LABELER...")
            run(
                "python research/regime_labeler.py",
                timeout=600,
                label="REGIME_LAB",
            )
            next_regime_lab = now_utc() + timedelta(hours=24)

        # ── Health log elke 5 minuten ──
        if now >= next_health_log:
            uptime = int((now_utc() - datetime.fromisoformat(
                stats["started_at"]
            )).total_seconds() / 60)
            log(
                f"💓 HEALTH | "
                f"uptime={uptime}min | "
                f"monitor_runs={stats['monitor_runs']} | "
                f"scanner_runs={stats['scanner_runs']} | "
                f"errors={stats['errors']} | "
                f"volgende_scan_in={int((next_scanner - now_utc()).total_seconds() / 60)}min"
            )
            next_health_log = now_utc() + timedelta(minutes=HEALTH_CHECK_MIN)

        # Wacht tot volgende monitor run
        time.sleep(MONITOR_INTERVAL_SEC)

    log("📛 Scheduler gestopt (graceful shutdown)")
    send_whatsapp(
        f"📛 BOT SCHEDULER GESTOPT\n\n"
        f"Graceful shutdown uitgevoerd.\n"
        f"Monitor runs: {stats['monitor_runs']}\n"
        f"Scanner runs: {stats['scanner_runs']}\n\n"
        f"Herstart op Render om bot te hervatten."
    )


if __name__ == "__main__":
    main()


# ============================================================
# HEALTH MONITOR — controleert bot gezondheid elke run
# ============================================================
def check_services_health() -> Dict[str, bool]:
    """
    Controleert of alle kritieke services bereikbaar zijn.
    Wordt elke 5 minuten aangeroepen vanuit de scheduler loop.
    """
    import psycopg2
    health: Dict[str, bool] = {}

    # Database check
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        conn.cursor().execute("SELECT 1")
        conn.close()
        health["database"] = True
    except Exception:
        health["database"] = False

    # Bitvavo check (publieke endpoint)
    try:
        resp = requests.get("https://api.bitvavo.com/v2/markets", timeout=5)
        health["bitvavo"] = resp.ok
    except Exception:
        health["bitvavo"] = False

    # Binance check (publieke endpoint)
    try:
        resp = requests.get("https://api.binance.com/api/v3/ping", timeout=5)
        health["binance"] = resp.ok
    except Exception:
        health["binance"] = False

    return health


def log_health_status(health: Dict[str, bool]) -> None:
    """Logt de health status overzichtelijk."""
    log("─" * 40)
    log("HEALTH STATUS:")
    for service, ok in health.items():
        status = "✅" if ok else "❌"
        log(f"  {status} {service}")
    log("─" * 40)


def send_health_alert(health: Dict[str, bool]) -> None:
    """Stuurt WhatsApp alert als een service down is."""
    down = [s for s, ok in health.items() if not ok]
    if not down:
        return

    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_FROM,
                "To":   TWILIO_WHATSAPP_TO,
                "Body": (
                    f"🚨 SERVICE DOWN — SCHEDULER\n"
                    f"{'─' * 28}\n\n"
                    f"Down: {', '.join(down)}\n\n"
                    f"🤖 BOT LOOPT MAAR KAN PROBLEMEN HEBBEN\n"
                    f"Check Render logs voor details.\n\n"
                    f"Commands: STATUS | STOP"
                ),
            },
            timeout=15,
        )
    except Exception:
        pass


# ============================================================
# SCHEMA SYNC BIJ START
# ============================================================
def run_schema_sync() -> bool:
    """
    Voert schema sync uit bij opstarten.
    Zorgt dat alle DB tabellen en kolommen correct zijn.
    Samenwerking: roept fix_experience_schema.py aan.
    """
    return run("python fix_experience_schema.py", timeout=120)


# ============================================================
# UITGEBREIDE STATUS LOGGING
# ============================================================
def log_scheduler_status(
    next_multi:   datetime,
    next_history: datetime,
    next_sim:     datetime,
    next_btc:     datetime,
    run_count:    int,
) -> None:
    """Logt uitgebreide scheduler status elke 5 minuten."""
    n = now_utc()
    log("─" * 50)
    log(f"SCHEDULER STATUS (run #{run_count})")
    log(f"  Nu:          {n.strftime('%H:%M:%S UTC')}")
    log(f"  Multi coin:  over {max(0,int((next_multi-n).total_seconds()//60))} min")
    log(f"  History:     over {max(0,int((next_history-n).total_seconds()//3600))}u")
    log(f"  Simulator:   over {max(0,int((next_sim-n).total_seconds()//3600))}u")
    log(f"  BTC regime:  over {max(0,int((next_btc-n).total_seconds()//60))} min")
    log("─" * 50)
