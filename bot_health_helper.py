"""
bot_health_helper.py
====================
Elke service importeert dit en roept health_update() aan.
Dan weet de watchdog precies of alles draait.

Gebruik:
    from bot_health_helper import health_update, health_fout
    
    # Begin van run:
    health_update('live_trader', 'RUNNING', 'Bezig met scannen...')
    
    # Na succesvolle actie:
    health_update('live_trader', 'OK', '6 trades open, 2 vandaag', live=6)
    
    # Bij fout:
    health_fout('live_trader', 'DB verbinding verloren', fout=str(e))
"""

import os, psycopg2
from datetime import datetime, timezone

DATABASE_URL = os.getenv('DATABASE_URL', '')

def _conn():
    return psycopg2.connect(DATABASE_URL)

def health_update(
    service: str,
    status: str = 'OK',
    details: str = '',
    live: int = 0,
    shadow: int = 0,
    sim: int = 0,
    extra: dict = None,
) -> None:
    """
    Schrijf een heartbeat naar bot_health.
    
    status: 'OK' | 'RUNNING' | 'WARN' | 'ERROR'
    details: wat de service nu doet
    live/shadow/sim: aantal trades van elk type
    """
    try:
        conn = _conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO bot_health (
                service, status, laatste_run, details,
                fout, live_count, shadow_count, sim_count, extra
            ) VALUES (%s, %s, NOW(), %s, NULL, %s, %s, %s, %s)
            ON CONFLICT (service) DO UPDATE SET
                status       = EXCLUDED.status,
                laatste_run  = EXCLUDED.laatste_run,
                details      = EXCLUDED.details,
                fout         = NULL,
                live_count   = EXCLUDED.live_count,
                shadow_count = EXCLUDED.shadow_count,
                sim_count    = EXCLUDED.sim_count,
                extra        = EXCLUDED.extra
        """, (
            service, status, details or '',
            live, shadow, sim,
            __import__('json').dumps(extra) if extra else None
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        # Nooit crashen door health update
        print(f'[HEALTH] update fout ({service}): {e}', flush=True)

def health_fout(service: str, details: str, fout: str = '') -> None:
    """Registreer een fout voor een service."""
    try:
        conn = _conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO bot_health (service, status, laatste_run, details, fout)
            VALUES (%s, 'ERROR', NOW(), %s, %s)
            ON CONFLICT (service) DO UPDATE SET
                status      = 'ERROR',
                laatste_run = NOW(),
                details     = EXCLUDED.details,
                fout        = EXCLUDED.fout
        """, (service, details, fout[:500] if fout else ''))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[HEALTH] fout update mislukt ({service}): {e}', flush=True)
