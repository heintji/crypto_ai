#!/usr/bin/env python3
"""
bot_health_helper.py — Heartbeat helper voor alle services.
Schrijft naar bot_health tabel zodat watchdog weet dat services leven.
"""
import os, psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def health_update(service: str, status: str = "OK", details: str = "", **kwargs) -> bool:
    """Update heartbeat voor een service in bot_health tabel."""
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        cur.execute("""
            UPDATE bot_health
            SET status = %s,
                laatste_run = NOW(),
                details = %s
            WHERE service = %s
        """, (status, str(details)[:200], service))
        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO bot_health (service, status, laatste_run, details)
                VALUES (%s, %s, NOW(), %s)
            """, (service, status, str(details)[:200]))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[HEALTH] {service} heartbeat fout: {e}")
        return False

def health_fout(service: str, fout: str = "") -> bool:
    """Registreer een fout voor een service."""
    return health_update(service, "FOUT", fout[:200])
