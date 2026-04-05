"""strategy_simulator.py - Fase 3: 5 strategieen x 7 filters"""
from __future__ import annotations
import os,sys,json,time,math
from datetime import datetime,timezone
from typing import Optional,List,Tuple
PROJECT_ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path: sys.path.insert(0,PROJECT_ROOT)
from db import db_connect
SIM_MIN_TRADES=50;SIM_MIN_WINRATE=66.0
SHADOW_MIN_TRADES=30;SHADOW_MIN_WINRATE=68.0
F_FEAR_GREED=1;F_REGIME_BULL=2;F_VOLUME_50M=4;F_SCORE_82=8
F_BTC_MA200=16;F_EU_US_SESSION=32;F_TAKER_55=64
def now_utc(): return datetime.now(timezone.utc)
def log(m): print(f"[SIM {now_utc():%H:%M:%S}] {m}",flush=True)
def sf(v,d=0.0):
    try: return float(v or d)
    except: return d
def _ema(p,n):
    if len(p)<n: return p[-1] if p else 0.0
    k=2/(n+1);e=sum(p[:n])/n
    for x in p[n:]: e=x*k+e*(1-k)
    return e
def _rsi(c,n=14):
    if len(c)<n+1: return 50.0
    g,l=[],[]
    for i in range(1,len(c)):
        d=c[i]-c[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g[-n:])/n;al=sum(l[-n:])/n
    return 100.0 if al==0 else 100-(100/(1+ag/al))
def _bb(c,n=20):
    if len(c)<n: return c[-1],c[-1],c[-1]
    w=c[-n:];m=sum(w)/n;s=math.sqrt(sum((x-m)**2 for x in w)/n)
    return m-2*s,m,m+2*s
def _vwap(h,l,c,v):
    tp=[(a+b+d)/3 for a,b,d in zip(h,l,c)];tv=sum(v)
    return sum(t*x for t,x in zip(tp,v))/tv if tv else c[-1]
def _candles(conn,sym,tf="1h",n=55):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT open,high,low,close,volume,taker_buy_base,quote_volume FROM public.candles WHERE symbol=%s AND timeframe=%s AND exchange='binance' ORDER BY open_time DESC LIMIT %s",(sym,tf,n))
            r=cur.fetchall()
        return [{"o":sf(x[0]),"h":sf(x[1]),"l":sf(x[2]),"c":sf(x[3]),"v":sf(x[4]),"t":sf(x[5]),"q":sf(x[6])} for x in reversed(r)]
    except: return []
def _regime(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regime,close,ema200 FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 1")
            r=cur.fetchone()
        return (r[0] if r else "RANGE",sf(r[1] if r else 0),sf(r[2] if r else 0))
    except: return "RANGE",0.0,0.0
def _fng():
    try:
        import requests as rq
        return int(rq.get("https://api.alternative.me/fng/?limit=1",timeout=6).json()["data"][0]["value"])
    except: return 50
def _filters(sig,reg,bc,be,fng,c1h):
    fl=0;lb=[]
    if fng>30: fl|=F_FEAR_GREED;lb.append("FG")
    if reg in("BULL","RANGE"): fl|=F_REGIME_BULL;lb.append("RB")
    qv=sum(x["q"] for x in c1h[-24:]) if len(c1h)>=24 else 0
    if qv>50000000: fl|=F_VOLUME_50M;lb.append("V5")
    if sig.get("sc",0)>=82: fl|=F_SCORE_82;lb.append("S8")
    if bc>0 and be>0 and bc>be: fl|=F_BTC_MA200;lb.append("MA")
    if 8<=now_utc().hour<22: fl|=F_EU_US_SESSION;lb.append("EU")
    if c1h:
        x=c1h[-1];tr=x["t"]/x["v"] if x["v"]>0 else 0
        if tr>0.55: fl|=F_TAKER_55;lb.append("T5")
    return fl,",".join(lb)
def _s1(c,reg):
    if len(c)<25: return None
    cl=[x["c"] for x in c];r=_rsi(cl);bl,bm,_=_bb(cl);p=cl[-1]
    if r>35 or p>bl*1.01 or reg=="BEAR": return None
    e=p;st=p*0.96;tg=bm;rv=(tg-e)/(e-st) if e>st else 0
    if rv<1.5: return None
    return {"id":1,"nm":"MEAN_REVERSION","e":e,"s":st,"t":tg,"sc":int(min(90,60+(35-r))),"r":round(rv,2)}
def _s2(c,reg):
    if len(c)<22: return None
    cl=[x["c"] for x in c];hi=[x["h"] for x in c];vo=[x["v"] for x in c]
    h20=max(hi[-21:-1]);av=sum(vo[-21:-1])/20;p=cl[-1]
    if p<h20*1.005 or vo[-1]<av*1.8 or reg=="BEAR": return None
    e=p;st=h20*0.99;tg=e+(e-st)*2;rv=(tg-e)/(e-st) if e>st else 0
    if rv<1.8: return None
    return {"id":2,"nm":"BREAKOUT","e":e,"s":st,"t":tg,"sc":70+min(30,int((vo[-1]/av-1)*15)),"r":round(rv,2)}
def _s3(c1,c4,reg):
    if len(c4)<51 or len(c1)<10: return None
    cl4=[x["c"] for x in c4];cl1=[x["c"] for x in c1]
    m9=_ema(cl4,9);m50=_ema(cl4,50);r=_rsi(cl1);p=cl1[-1]
    if m9<=m50 or not(45<=r<=70) or reg=="BEAR": return None
    e=p;st=m9*0.985;tg=e+(e-st)*2.2;rv=(tg-e)/(e-st) if e>st else 0
    if rv<1.8: return None
    return {"id":3,"nm":"TREND_FOLLOWING","e":e,"s":st,"t":tg,"sc":68+min(25,int((m9/m50-1)*500)),"r":round(rv,2)}
def _s4(c,reg):
    if len(c)<20: return None
    cl=[x["c"] for x in c];h=[x["h"] for x in c];lo=[x["l"] for x in c]
    v=[x["v"] for x in c];tk=[x["t"] for x in c]
    vw=_vwap(h[-20:],lo[-20:],cl[-20:],v[-20:]);r=_rsi(cl);p=cl[-1]
    tr=tk[-1]/v[-1] if v[-1]>0 else 0.5;tp=tk[-2]/v[-2] if v[-2]>0 else 0.5
    if r>38 or p>vw or p<vw*0.97 or tr<tp or reg=="BEAR": return None
    e=p;st=p*0.965;tg=vw*1.02;rv=(tg-e)/(e-st) if e>st else 0
    if rv<1.5: return None
    return {"id":4,"nm":"VWAP_BOUNCE","e":e,"s":st,"t":tg,"sc":int(70+(38-r)+(tr-0.5)*20),"r":round(rv,2)}
def _s5(c1,c4,reg):
    if len(c1)<25 or len(c4)<51: return None
    cl1=[x["c"] for x in c1];cl4=[x["c"] for x in c4]
    r=_rsi(cl1);m9=_ema(cl4,9);m50=_ema(cl4,50)
    bl,bm,_=_bb(cl1);p=cl1[-1];sc=60
    if reg=="BULL":
        if m9>m50: sc+=15
        if 55<=r<=75: sc+=10
        if p>bm: sc+=5
        st=p*0.96;tg=p*1.08
    elif reg=="RANGE":
        if r<35: sc+=20
        if p<bl*1.01: sc+=10
        st=p*0.965;tg=bm
    else: return None
    if sc<72: return None
    e=p;rv=(tg-e)/(e-st) if e>st else 0
    if rv<1.5: return None
    return {"id":5,"nm":"REGIME_ADAPTIVE","e":e,"s":st,"t":tg,"sc":min(95,sc),"r":round(rv,2)}
def _save(conn,sym,sig,reg,oc,pr,fl,lb):
    try:
        tk=f"SIM_{sig['id']}_{sym}_{int(time.time()*1000)}"
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.experience_trades(trade_key,source,coin,timestamp,entry_time,exit_time,setup_type,market_regime,entry,stop,target,outcome,pnl_eur,pnl_r,result_r,bot_confidence,strategy_id,strategy_name,filter_flags,filter_label,created_at,updated_at) VALUES(%s,'SIM',%s,NOW(),NOW(),NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT(trade_key) DO NOTHING""",
            (tk,sym,sig["nm"],reg,sig["e"],sig["s"],sig["t"],oc,pr*5,pr,pr,sig["sc"],sig["id"],sig["nm"],fl,lb))
        conn.commit();return True
    except Exception as ex:
        log(f"Save err:{ex}")
        try: conn.rollback()
        except: pass
        return False
def _promo(conn,sid,sn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*),ROUND(AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END)*100,1) FROM public.experience_trades WHERE strategy_id=%s AND UPPER(source)='SIM' AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')", (sid,))
            r=cur.fetchone();n,wr=(r[0] or 0),sf(r[1])
            cur.execute("SELECT status FROM public.strategy_status WHERE strategy_id=%s",(sid,))
            row=cur.fetchone()
        if not row: return
        if row[0]=="SIM" and n>=SIM_MIN_TRADES and wr>=SIM_MIN_WINRATE:
            log(f"PROMOTIE: {sn} SIM->SHADOW ({n},{wr}%)")
            with conn.cursor() as cur:
                cur.execute("UPDATE public.strategy_status SET status='SHADOW',sim_winrate=%s,sim_trades=%s,updated_at=NOW(),promoted_at=NOW() WHERE strategy_id=%s",(wr,n,sid))
            conn.commit()
    except Exception as ex:
        log(f"Promo err:{ex}")
        try: conn.rollback()
        except: pass
def main():
    log("=== Simulator gestart ===")
    conn=db_connect()
    try:
        reg,bc,be=_regime(conn);fng=_fng()
        log(f"Regime:{reg} BTC:{bc:.0f}/MA:{be:.0f} F&G:{fng}")
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT coin FROM public.experience_trades WHERE timestamp>NOW()-INTERVAL '7 days' AND UPPER(source) IN ('SHADOW','LIVE','SIM') LIMIT 30")
            coins=[r[0] for r in cur.fetchall()]
        if not coins: coins=["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT"]
        tot=0
        for sym in coins:
            c1=_candles(conn,sym,"1h",55);c4=_candles(conn,sym,"4h",55)
            if len(c1)<25: continue
            for sig in [_s1(c1,reg),_s2(c1,reg),_s3(c1,c4,reg),_s4(c1,reg),_s5(c1,c4,reg)]:
                if not sig: continue
                fl,lb=_filters(sig,reg,bc,be,fng,c1)
                if len(c1)<2: continue
                nx=c1[-1]["c"];e=sig["e"];st=sig["s"];tg=sig["t"]
                if tg>e:
                    if nx>=tg: oc,pr="WIN",(tg-e)/(e-st)
                    elif nx<=st: oc,pr="LOSS",-1.0
                    else: continue
                if _save(conn,sym,sig,reg,oc,pr,fl,lb): tot+=1
        log(f"Totaal {tot} SIM trades")
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_id,strategy_name FROM public.strategy_status WHERE status IN ('SIM','SHADOW')")
            for sid,sn in cur.fetchall(): _promo(conn,sid,sn)
    finally: conn.close()
    log("=== Klaar ===")
if __name__=="__main__": main()
