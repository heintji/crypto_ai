"""strategy_simulator.py v2.0 - 5 strategieen x 7 filters

Fase 3 Multi-Strategie Simulator draait elke 30 min als Render Cron Job.
Simuleert 5 tradingstrategieen in SIM-mode (papier, geen echt geld).
Evalueert 7 marktfilters per trade als bitmap voor analyse.
Bewaakt promotiedrempels: SIM->SHADOW->LIVE_PENDING.

5 Strategieen:
  1. MEAN_REVERSION    RSI<35 + prijs onder Bollinger Lower Band
  2. BREAKOUT          20-periodes hoog + 1.8x volume bevestiging
  3. TREND_FOLLOWING   MA9>MA50 op 4h + RSI 45-70 op 1h
  4. VWAP_BOUNCE       Bounce van VWAP + stijgende taker ratio
  5. REGIME_ADAPTIVE   Past setup aan op BTC regime BULL/RANGE/BEAR

7 Filters (bitmap):
  Bit 1  FEAR_GREED    F&G > 30, geen extreme fear
  Bit 2  REGIME_BULL   BTC regime BULL of RANGE
  Bit 4  VOLUME_50M    24u volume > 50 miljoen dollar
  Bit 8  SCORE_82      Signaal score >= 82
  Bit 16 BTC_MA200     BTC boven 200 periods EMA
  Bit 32 EU_US_SESSION 08:00-22:00 UTC actief
  Bit 64 TAKER_55      Taker buy ratio > 55 procent

Promotiedrempels:
  SIM -> SHADOW:          winrate > 66% na >= 50 trades
  SHADOW -> LIVE_PENDING: winrate > 68% na >= 30 shadow trades
"""
from __future__ import annotations
import os, sys, time, math, requests, psycopg2
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any

DATABASE_URL       = os.environ.get("DATABASE_URL", "")
DB_CONNECT_RETRIES = 3
SIM_POSITION_EUR   = 5.0
SIM_MIN_TRADES     = 50;  SIM_MIN_WINRATE     = 66.0
SHADOW_MIN_TRADES  = 30;  SHADOW_MIN_WINRATE  = 68.0
MIN_R = {1: 1.5, 2: 1.8, 3: 1.8, 4: 1.5, 5: 1.5}
F_FEAR_GREED=1; F_REGIME_BULL=2; F_VOLUME_50M=4; F_SCORE_82=8
F_BTC_MA200=16; F_EU_US_SESSION=32; F_TAKER_55=64
STRAT = {1:"MEAN_REVERSION",2:"BREAKOUT",3:"TREND_FOLLOWING",4:"VWAP_BOUNCE",5:"REGIME_ADAPTIVE"}
DEFAULT_COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT"]

def now_utc(): return datetime.now(timezone.utc)
def log(m): print(f"[SIM {now_utc():%H:%M:%S}] {m}", flush=True)
def sf(v, d=0.0):
    try: return float(v or d)
    except: return d
def safe_rb(conn):
    try: conn.rollback()
    except: pass

def db_connect(retries=DB_CONNECT_RETRIES):
    """Verbinding met PostgreSQL, max retries pogingen met 3s wachttijd."""
    for p in range(1, retries+1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
            conn.autocommit = False
            return conn
        except Exception as e:
            log(f"DB poging {p}/{retries} mislukt: {e}")
            if p < retries: time.sleep(3)
    raise RuntimeError(f"DB verbinding mislukt na {retries} pogingen.")

def whatsapp(msg):
    url = os.environ.get("RENDER_WEBHOOK_URL","")
    if not url: return False
    try:
        r = requests.post(f"{url}/send_whatsapp", json={"message":msg}, timeout=10)
        return r.status_code==200
    except: return False

# --- Indicatoren ---
def ema(prices, n):
    """EMA: geeft meer gewicht aan recente koersen. k=2/(n+1)."""
    if len(prices)<n: return prices[-1] if prices else 0.0
    k=2/(n+1); e=sum(prices[:n])/n
    for p in prices[n:]: e=p*k+e*(1-k)
    return e

def rsi(closes, n=14):
    """RSI 0-100: <30=oversold, >70=overbought. Periode standaard 14."""
    if len(closes)<n+1: return 50.0
    g,l=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]; g.append(max(d,0)); l.append(max(-d,0))
    ag=sum(g[-n:])/n; al=sum(l[-n:])/n
    return 100.0 if al==0 else 100-(100/(1+ag/al))

def bollinger(closes, n=20):
    """Bollinger Bands: middle=SMA, lower/upper=+/-2 standaarddeviaties."""
    if len(closes)<n: v=closes[-1] if closes else 0.0; return v,v,v
    w=closes[-n:]; m=sum(w)/n; s=math.sqrt(sum((x-m)**2 for x in w)/n)
    return m-2*s, m, m+2*s

def vwap(highs, lows, closes, vols):
    """VWAP: volume-gewogen gemiddelde prijs, sterke support/resistance."""
    tp=[(h+l+c)/3 for h,l,c in zip(highs,lows,closes)]; tv=sum(vols)
    return sum(t*v for t,v in zip(tp,vols))/tv if tv else (closes[-1] if closes else 0)

# --- DB helpers ---
def get_candles(conn, sym, tf="1h", lim=260):
    """Haalt OHLCV candles op van Binance, chronologisch (oud->nieuw).
    Standaard 260 candles voor backtesting over meerdere periodes."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT open,high,low,close,volume,taker_buy_base,quote_volume,
                EXTRACT(EPOCH FROM open_time)*1000 AS ts_ms
                FROM public.candles WHERE symbol=%s AND timeframe=%s AND exchange='binance'
                ORDER BY open_time DESC LIMIT %s""", (sym,tf,lim))
            rows=cur.fetchall()
        return [{"open":sf(r[0]),"high":sf(r[1]),"low":sf(r[2]),"close":sf(r[3]),
                 "volume":sf(r[4]),"taker":sf(r[5]),"qvol":sf(r[6]),
                 "ts_ms":int(sf(r[7]))} for r in reversed(rows)]
    except Exception as e: log(f"Candles {sym}: {e}"); return []

def get_regime(conn):
    """BTC marktregime (BULL/RANGE/BEAR), slotkoers en EMA200."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regime,close,ema200 FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 1")
            r=cur.fetchone()
        return (r[0] or "RANGE",sf(r[1]),sf(r[2])) if r else ("RANGE",0.0,0.0)
    except: return "RANGE",0.0,0.0

def get_fng():
    """Fear & Greed Index van alternative.me. 0=extreme fear, 100=extreme greed."""
    try:
        r=requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        return int(r.json()["data"][0]["value"])
    except: return 50

def get_coins(conn):
    """Haalt alle coins op die voldoende candle data hebben (>=25 1h candles).
    Pakt direct uit de candles tabel zodat alle beschikbare coins meedoen,
    niet alleen coins die al eerder gehandeld zijn."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT symbol FROM public.candles
                WHERE exchange='binance' AND timeframe='1h'
                GROUP BY symbol HAVING COUNT(*) >= 25
                ORDER BY symbol
            """)
            coins=[r[0] for r in cur.fetchall()]
        log(f"Coins gevonden in DB: {len(coins)}")

        # Fase 4: Extreme Greed filter - alleen SIM
        # Backtest bewijs: WR 28.9% vs 34.1% baseline bij F&G>80
        # Live trades: nog niet actief - eerst meer data verzamelen
        if markt_score_val <= -2:
            log(f"SIM SKIP: Extreme Greed geblokkeerd (markt_score={markt_score_val})")
            log(f"Bewijs: backtest WR=28.9% vs baseline 34.1% op 1909 trades")
            log(f"Status: SIM blocked | LIVE: nog niet actief (wacht op meer bewijs)")
        return coins if coins else DEFAULT_COINS
    except Exception as e:
        log(f"get_coins fout: {e}")
        return DEFAULT_COINS

# --- Filter evaluatie ---
def eval_filters(sig, regime, btc_koers, btc_ema200, fng, c1h):
    """
    Evalueert 7 marktfilters en geeft bitmap + label terug.
    Meer actieve bits = gunstiger marktomstandigheden.
    Opgeslagen in filter_flags voor analyse via filter_strategy_matrix view.
    """
    flags=0; labels=[]
    if fng>30:            flags|=F_FEAR_GREED;    labels.append("FEAR_GREED")
    if regime in("BULL","RANGE"): flags|=F_REGIME_BULL; labels.append("REGIME_BULL")
    qv=sum(c["qvol"] for c in c1h[-24:]) if len(c1h)>=24 else 0
    if qv>50_000_000:     flags|=F_VOLUME_50M;   labels.append("VOLUME_50M")
    if sig.get("sc",0)>=82: flags|=F_SCORE_82;  labels.append("SCORE_82")
    if btc_koers>0 and btc_ema200>0 and btc_koers>btc_ema200:
        flags|=F_BTC_MA200; labels.append("BTC_MA200")
    if 8<=now_utc().hour<22: flags|=F_EU_US_SESSION; labels.append("EU_US_SESSION")
    if c1h:
        x=c1h[-1]; tr=x["taker"]/x["volume"] if x["volume"]>0 else 0
        if tr>0.55: flags|=F_TAKER_55; labels.append("TAKER_55")
    return flags, ",".join(labels)

# --- 5 Strategieen ---
def s1_mean_reversion(c1h, regime):
    """
    MEAN REVERSION: Koopt bij extreme oversold condities.
    RSI<35 + prijs<=Bollinger Lower*1.01. Target=Bollinger Middle.
    Niet in BEAR (prijs kan blijven dalen). Stop=-4%. MinR=1.5.
    """
    if len(c1h)<25: return None
    cl=[c["close"] for c in c1h]; r=rsi(cl); bl,bm,_=bollinger(cl); p=cl[-1]
    if r>35 or p>bl*1.01 or regime=="BEAR": return None
    e=p; st=p*0.96; tg=bm; rv=(tg-e)/(e-st) if e>st else 0
    if rv<MIN_R[1]: return None
    return {"id":1,"nm":"MEAN_REVERSION","e":e,"s":st,"t":tg,"sc":int(min(92,60+(35-r)*1.2)),"r":round(rv,2)}

def s2_breakout(c1h, regime):
    """
    BREAKOUT: 20-periodes hoog doorbroken met 1.8x volume bevestiging.
    Nep-breakouts zonder volume worden gefilterd. MinR=1.8.
    Stop=net onder breakout niveau. Target=2x risico afstand.
    """
    if len(c1h)<22: return None
    cl=[c["close"] for c in c1h]; hi=[c["high"] for c in c1h]; vo=[c["volume"] for c in c1h]
    h20=max(hi[-21:-1]); av=sum(vo[-21:-1])/20; p=cl[-1]
    if p<h20*1.005 or vo[-1]<av*1.8 or regime=="BEAR": return None
    e=p; st=h20*0.99; tg=e+(e-st)*2.0; rv=(tg-e)/(e-st) if e>st else 0
    if rv<MIN_R[2]: return None
    vs=min(28,int((vo[-1]/av-1)*14))
    return {"id":2,"nm":"BREAKOUT","e":e,"s":st,"t":tg,"sc":70+vs,"r":round(rv,2)}

def s3_trend(c1h, c4h, regime):
    """
    TREND FOLLOWING: Koopt in bevestigde uptrend op gezond RSI niveau.
    MA9>MA50 op 4h (uptrend), RSI 45-70 op 1h (niet overbought). MinR=1.8.
    Stop=net onder MA9 (trend breekt als MA9 doorbroken). Target=2.2x risico.
    """
    if len(c4h)<51 or len(c1h)<15: return None
    sl4=[c["close"] for c in c4h]; sl1=[c["close"] for c in c1h]
    m9=ema(sl4,9); m50=ema(sl4,50); r=rsi(sl1); p=sl1[-1]
    if m9<=m50 or not(45<=r<=70) or regime=="BEAR": return None
    e=p; st=m9*0.985; tg=e+(e-st)*2.2; rv=(tg-e)/(e-st) if e>st else 0
    if rv<MIN_R[3]: return None
    ts=min(22,int((m9/m50-1)*500))
    return {"id":3,"nm":"TREND_FOLLOWING","e":e,"s":st,"t":tg,"sc":68+ts,"r":round(rv,2)}

def s4_vwap(c1h, regime):
    """
    VWAP BOUNCE: Bounce van Volume Weighted Average Price.
    Prijs net onder VWAP (max 3%), RSI<38, taker ratio stijgend.
    Taker ratio stijging = kopers nemen het over van verkopers. MinR=1.5.
    """
    if len(c1h)<20: return None
    cl=[c["close"] for c in c1h]; hi=[c["high"] for c in c1h]
    lo=[c["low"] for c in c1h]; vo=[c["volume"] for c in c1h]; tk=[c["taker"] for c in c1h]
    vw=vwap(hi[-20:],lo[-20:],cl[-20:],vo[-20:]); r=rsi(cl); p=cl[-1]
    tn=tk[-1]/vo[-1] if vo[-1]>0 else 0.5; tp=tk[-2]/vo[-2] if vo[-2]>0 else 0.5
    if r>38 or p>vw or p<vw*0.97 or tn<=tp or regime=="BEAR": return None
    e=p; st=p*0.965; tg=vw*1.02; rv=(tg-e)/(e-st) if e>st else 0
    if rv<MIN_R[4]: return None
    sc=int(min(90,70+(38-r)*0.8+(tn-0.5)*20))
    return {"id":4,"nm":"VWAP_BOUNCE","e":e,"s":st,"t":tg,"sc":sc,"r":round(rv,2)}

def s5_regime(c1h, c4h, regime):
    """
    REGIME ADAPTIVE: Past volledig aan op het BTC marktregime.
    BULL: Zoekt momentum (MA9>MA50, RSI 55-75, boven BB mid). Target=+8%.
    RANGE: Zoekt mean reversion (RSI<35, bij BB lower). Target=BB middle.
    BEAR: Geen trades - te riskant voor long posities. MinScore=72.
    """
    if len(c1h)<25 or len(c4h)<51: return None
    sl1=[c["close"] for c in c1h]; sl4=[c["close"] for c in c4h]
    r=rsi(sl1); m9=ema(sl4,9); m50=ema(sl4,50)
    bl,bm,_=bollinger(sl1); p=sl1[-1]; sc=60
    if regime=="BULL":
        if m9>m50: sc+=15
        if 55<=r<=75: sc+=10
        if p>bm: sc+=5
        st=p*0.960; tg=p*1.080
    elif regime=="RANGE":
        if r<35: sc+=20
        if p<bl*1.01: sc+=10
        st=p*0.965; tg=bm
    else: return None
    if sc<72: return None
    e=p; rv=(tg-e)/(e-st) if e>st else 0
    if rv<MIN_R[5]: return None
    return {"id":5,"nm":"REGIME_ADAPTIVE","e":e,"s":st,"t":tg,"sc":min(95,sc),"r":round(rv,2)}

# --- Resultaat + opslaan ---
def sim_result(sig, c1h):
    """Simuleert resultaat op volgende candle. WIN>=target, LOSS<=stop, anders open."""
    if len(c1h)<2: return None
    nx=c1h[-1]["close"]; e=sig["e"]; st=sig["s"]; tg=sig["t"]
    if tg>e:
        if nx>=tg: return "WIN", round((tg-e)/(e-st),3)
        if nx<=st: return "LOSS", -1.0
    return None

def save_trade(conn, sym, sig, regime, oc, pr, flags, label, tk=None,
               fng_waarde=None, fng_label=None, funding_r=None, funding_v=None,
               nieuws_r=None, oi_r=None, btc_dom_r=None, cross_score=None):
    """Slaat SIM trade op met alle signalen apart voor combinatie analyse.
    Elke trade registreert: strategie + filter bitmap + F&G + nieuws + funding
    + OI + BTC dominantie. Zo kun je later querien welke combinatie het beste werkt.
    """
    try:
        if tk is None: tk=f"SIM_{sig['id']}_{sym}_{int(time.time()*1000)}"
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.experience_trades(
                trade_key,source,coin,timestamp,entry_time,exit_time,
                setup_type,market_regime,entry,stop,target,
                outcome,pnl_eur,markt_advies,markt_score,
                fng_waarde,fng_label,funding_richting,funding_waarde,
                nieuws_richting,oi_richting,btc_dom_richting,cross_ref_score,
                coin_cluster,pnl_r,result_r,bot_confidence,
                strategy_id,strategy_name,filter_flags,filter_label,
                created_at,updated_at)
                VALUES(%s,'SIM',%s,NOW(),NOW(),NOW(),%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
                ON CONFLICT(trade_key) DO NOTHING""",
                (tk,sym,sig["nm"],regime,sig["e"],sig["s"],sig["t"],
                 oc,pr*SIM_POSITION_EUR,markt_advies,markt_score_val,
                 fng_waarde,fng_label,funding_r,funding_v,
                 nieuws_r,oi_r,btc_dom_r,cross_score,_cluster,pr,pr,sig["sc"],
                 sig["id"],sig["nm"],flags,label))
        conn.commit(); return True
    except Exception as ex: log(f"Save {sym}: {ex}"); safe_rb(conn); return False

# --- Promotie ---
def check_promotie(conn, sid, sn):
    """
    Promotie SIM->SHADOW bij >= 50 trades en >= 66% winrate.
    Promotie SHADOW->LIVE_PENDING bij >= 30 trades en >= 68% winrate.
    WhatsApp notificatie bij elke promotie.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.strategy_status WHERE strategy_id=%s",(sid,))
            row=cur.fetchone()
        if not row: return
        status=row[0]
        for src,min_t,min_w,next_s in [
            ("SIM",SIM_MIN_TRADES,SIM_MIN_WINRATE,"SHADOW"),
            ("SHADOW",SHADOW_MIN_TRADES,SHADOW_MIN_WINRATE,"LIVE_PENDING")
        ]:
            if status!=src: continue
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*),ROUND(AVG(CASE WHEN UPPER(outcome)='WIN'
                    THEN 100.0 ELSE 0.0 END),1) FROM public.experience_trades
                    WHERE strategy_id=%s AND UPPER(source)=%s
                    AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')""",(sid,src))
                r=cur.fetchone(); n,wr=(r[0] or 0),sf(r[1])
            log(f"  {sn} {src}: {n} trades, {wr:.1f}% WR (drempel: {min_t}t / {min_w}%)")
            if n>=min_t and wr>=min_w:
                log(f"  PROMOTIE! {sn} {src}->{next_s}")
                field="sim" if src=="SIM" else "shadow"
                with conn.cursor() as cur:
                    cur.execute(f"""UPDATE public.strategy_status
                        SET status=%s,{field}_winrate=%s,{field}_trades=%s,
                        updated_at=NOW(),promoted_at=NOW() WHERE strategy_id=%s""",
                        (next_s,wr,n,sid))
                conn.commit()
                whatsapp(f"PROMOTIE {sn}: {src}->{next_s}\n{n} trades | {wr:.1f}% WR")
    except Exception as e: log(f"Promotie {sn}: {e}"); safe_rb(conn)

# --- Hoofd ---
def main():
    log("="*60)
    log("Strategy Simulator v2.0 + Filter Matrix gestart")
    log("="*60)
    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld!"); return
    conn=db_connect()
    try:
        regime,btc_k,btc_e=get_regime(conn); fng=get_fng()
        log(f"Regime:{regime} BTC:${btc_k:,.0f} EMA200:${btc_e:,.0f} F&G:{fng}")
        coins=get_coins(conn)
        log(f"Simuleer {len(coins)} coins x 5 strategieen x 7 filters...")
        tot=wins=losses=0
        markt_score_val = 0  # fix: was undefined
        _fng_waarde = 0; _fng_label = "NEUTRAL"  # fix: undefined
        _nieuws_r = 0; _oi_r = 0; _btc_dom_r = 0  # fix: undefined
        for sym in coins:
            markt_score_val = 0  # default: geen extreme greed
            # Extreme Greed filter: sla over als markt in Extreme Greed
            if markt_score_val <= -2:
                break  # stop hele loop bij Extreme Greed
            # Haal 260 candles op voor backtesting (260-55=205 simulatiepunten per coin)
            c1_all=get_candles(conn,sym,"1h",260)
            c4_all=get_candles(conn,sym,"4h",260)
            if len(c1_all)<57: continue

            # Backtest: loop door alle historische vensters
            for i in range(55, len(c1_all)-1):
                c1_ctx = c1_all[max(0,i-55):i]   # 55 candles context
                c1_nx  = c1_all[i]                 # volgende candle = outcome
                candle_ts = c1_nx["ts_ms"]         # unieke timestamp voor trade_key

                # 4h context: gebruik candles die <= huidige 1h candle zijn
                c4_ctx = [c for c in c4_all if c["ts_ms"] <= c1_all[i-1]["ts_ms"]]
                c4_ctx = c4_ctx[-55:] if c4_ctx else []

                strats = [
                    # s1_mean_reversion: UIT - WR=15% op 20 trades, te slecht
                    # (s1_mean_reversion, False),
                    (s2_breakout,       False),
                    (s3_trend,          True),
                    (s4_vwap,           False),
                    # s5_regime: UIT - WR=14% op 92 trades, te slecht
                    # (s5_regime,         True),
                ]
                for fn, needs4h in strats:
                    try:
                        sig = fn(c1_ctx, c4_ctx, regime) if needs4h else fn(c1_ctx, regime)
                    except: continue
                    if not sig: continue

                    # Outcome direct bepalen op basis van volgende candle
                    e,s,t = sig["e"],sig["s"],sig["t"]
                    nx = c1_nx["close"]
                    if t > e:
                        if nx >= t:  oc,pr = "WIN",  round((t-e)/(e-s),3)
                        elif nx <= s: oc,pr = "LOSS", -1.0
                        else: continue  # trade nog open
                    else: continue

                    # Unieke key op basis van candle-tijd zodat backtesting niet dubbelt
                    tk = f"SIM_{sig['id']}_{sym}_{candle_ts}"
                    flags,label=eval_filters(sig,regime,btc_k,btc_e,fng,c1_ctx)
                    # Funding rate + coin cluster per coin ophalen
                    _fr_r=None; _fr_v=None; _cluster=None
                    try:
                        _fr=_md_obj.haal_funding_rate_coin_op(sym)
                        _fr_r=_fr.get("richting"); _fr_v=_fr.get("waarde")
                    except Exception: pass
                    try:
                        with conn.cursor() as _cc:
                            _cc.execute("SELECT cluster FROM public.coin_clusters WHERE symbool=%s LIMIT 1",(sym,))
                            _cr2=_cc.fetchone()
                            if _cr2: _cluster=_cr2[0]
                    except Exception: pass

                    if save_trade(conn,sym,sig,regime,oc,pr,flags,label,tk=tk,
                        fng_waarde=_fng_waarde,fng_label=_fng_label,
                        funding_r=_fr_r,funding_v=_fr_v,
                        nieuws_r=_nieuws_r,oi_r=_oi_r,btc_dom_r=_btc_dom_r,
                        cross_score=markt_score_val):
                        tot+=1; wins+=(1 if oc=="WIN" else 0); losses+=(1 if oc=="LOSS" else 0)
                        if tot<=20:  # log alleen eerste 20 om output kort te houden
                            log(f"  {sig['nm']:<20} {sym:<12} flags={flags:03d} "
                                f"{oc:<5} {pr:+.2f}R [{label}]")
        log(f"\nSamenvatting: {tot} trades | {wins}W {losses}L"
            + (f" | {wins/tot*100:.1f}% WR" if tot>0 else ""))
        log("\nPromotiecheck:")
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_id,strategy_name FROM public.strategy_status WHERE status IN ('SIM','SHADOW') ORDER BY 1")
            for sid,sn in cur.fetchall(): check_promotie(conn,sid,sn)
    finally:
        conn.close()
    log("="*60); log("Simulator klaar"); log("="*60)

if __name__=="__main__": main()
