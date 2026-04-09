import psycopg2, os
from datetime import datetime, timezone

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()

print('=' * 42)
print('           BOT STATUS CHECK')
print('=' * 42)

# INSTELLINGEN
cur.execute("SELECT key,value FROM bot_state WHERE key IN ('bot_running','shadow_trading_actief','score_drempel_bull') ORDER BY key")
print('\nINSTELLINGEN:')
for r in cur.fetchall():
    print(f'  {r[0]:<25} {r[1]}')

# LIVE TRADES
print('\n--- LIVE TRADES ---')
cur.execute("SELECT COUNT(*) FROM experience_trades WHERE source='LIVE' AND status='OPEN'")
print(f'Open:             {cur.fetchone()[0]}')
cur.execute("SELECT COUNT(*) FROM experience_trades WHERE source='LIVE' AND status='CLOSED' AND entry_time >= CURRENT_DATE")
print(f'Gesloten vandaag: {cur.fetchone()[0]}')
cur.execute("""SELECT COUNT(*) FILTER(WHERE pnl_pct>0), COUNT(*) FILTER(WHERE pnl_pct<=0),
               ROUND(AVG(pnl_pct)::numeric,2)
               FROM experience_trades WHERE source='LIVE' AND status='CLOSED' AND pnl_pct IS NOT NULL""")
r = cur.fetchone()
w, v, gem = r[0] or 0, r[1] or 0, r[2] or 0
wr = round(w/(w+v)*100,1) if (w+v) > 0 else 0
print(f'Win rate (totaal): {wr}% ({w}W/{v}V) gem PnL={gem}%')

# SHADOW TRADES
print('\n--- SHADOW TRADES ---')
cur.execute("SELECT COUNT(*) FROM experience_trades WHERE source='SHADOW' AND entry_time >= CURRENT_DATE AND status='OPEN'")
print(f'Open vandaag:     {cur.fetchone()[0]}')
cur.execute("SELECT COUNT(*) FROM experience_trades WHERE source='SHADOW' AND entry_time >= CURRENT_DATE AND status='CLOSED'")
print(f'Gesloten vandaag: {cur.fetchone()[0]}')
cur.execute("""SELECT COUNT(*) FILTER(WHERE pnl_pct>0), COUNT(*) FILTER(WHERE pnl_pct<=0),
               ROUND(AVG(pnl_pct)::numeric,2)
               FROM experience_trades WHERE source='SHADOW' AND status='CLOSED'
               AND pnl_pct IS NOT NULL AND entry_time >= CURRENT_DATE""")
r = cur.fetchone()
w, v, gem = r[0] or 0, r[1] or 0, r[2] or 0
wr = round(w/(w+v)*100,1) if (w+v) > 0 else 0
print(f'Win rate vandaag: {wr}% ({w}W/{v}V) gem PnL={gem}%')
cur.execute("""SELECT COUNT(*) FILTER(WHERE pnl_pct>0), COUNT(*) FILTER(WHERE pnl_pct<=0),
               ROUND(AVG(pnl_pct)::numeric,2)
               FROM experience_trades WHERE source='SHADOW' AND status='CLOSED' AND pnl_pct IS NOT NULL""")
r = cur.fetchone()
w, v, gem = r[0] or 0, r[1] or 0, r[2] or 0
wr = round(w/(w+v)*100,1) if (w+v) > 0 else 0
print(f'Win rate totaal:  {wr}% ({w}W/{v}V van {w+v} gesloten) gem PnL={gem}%')
cur.execute("""SELECT symbol, score, pnl_pct, setup_type, status
               FROM experience_trades WHERE source='SHADOW' AND entry_time >= CURRENT_DATE
               ORDER BY score DESC LIMIT 5""")
rows = cur.fetchall()
if rows:
    print('\nTop 5 shadow (score):')
    for r in rows:
        pnl = f'{r[2]}%' if r[2] is not None else 'open'
        print(f'  {r[0]:<12} score={r[1]} {str(r[3]):<20} {pnl}')

# SIM TRADES
print('\n--- SIM TRADES ---')
cur.execute("SELECT COUNT(*) FROM experience_trades WHERE source='SIM'")
print(f'Totaal: {cur.fetchone()[0]}')
cur.execute("""SELECT COUNT(*) FILTER(WHERE pnl_pct>0), COUNT(*) FILTER(WHERE pnl_pct<=0),
               ROUND(AVG(pnl_pct)::numeric,2)
               FROM experience_trades WHERE source='SIM' AND status='CLOSED' AND pnl_pct IS NOT NULL""")
r = cur.fetchone()
w, v, gem = r[0] or 0, r[1] or 0, r[2] or 0
wr = round(w/(w+v)*100,1) if (w+v) > 0 else 0
print(f'Win rate: {wr}% ({w}W/{v}V) gem PnL={gem}%')

# SERVICES
cur.execute('SELECT service, status, laatste_run::text FROM bot_health ORDER BY laatste_run DESC NULLS LAST')
print('\n--- SERVICES ---')
for r in cur.fetchall():
    ts = str(r[2])[11:16] if r[2] else 'nooit'
    icon = 'OK ' if r[1] == 'OK' else 'ERR'
    print(f'  [{icon}] {r[0]:<20} {ts}')

conn.close()
print('\n' + '=' * 42)
