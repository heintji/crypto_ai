#!/usr/bin/env python3
"""C2-VERBETERD (Kimi P1+P2+P3) backtest — meet of de gefilterde crash-bounce wel
winstgevend is. P1: alleen liquide coins + lage kosten (0,6% rt). P2: kapitulatie-
bevestiging (volume-climax + herstel-wick + BTC-stabiliteit). P3: ATR-exits
(stop 1,2xATR, target 1,5R, 48u). Vergelijkt met de oude C2. Read-only."""
import time, json, urllib.request
from datetime import datetime, timezone, timedelta

BITVAVO="https://api.bitvavo.com/v2"; TOP_N=40; DAYS_4H=400; DAYS_D=800; FOUR_H_MS=4*3600*1000
MAJORS={"BTC","ETH","SOL","XRP","ADA","DOGE","LINK","AVAX","DOT","POL"}
SKIP={"EUR","USDT","USDC","EURT","DAI","TUSD","FDUSD","PYUSD","EURC"}
LIQ_MIN=2_000_000.0          # P1: alleen coins met >=2M 24h-volume (liquide)
COST_RT=0.60                 # P1: limit-maker + smalle spread
CLIMAX_VOL=3.0; WICK_MIN=0.60; BTC_FLOOR=-0.02
ATR_STOP=1.2; RR=1.5; MAX_HOLD=12   # P3: 12x4h=48u

def get(p):
    for _ in range(3):
        try: return json.load(urllib.request.urlopen(f"{BITVAVO}{p}",timeout=20))
        except Exception: time.sleep(1)
    return None
def now_ms(): return int(datetime.now(timezone.utc).timestamp()*1000)
def daily(m):
    st=int((datetime.now(timezone.utc)-timedelta(days=DAYS_D)).timestamp()*1000)
    d=get(f"/{m}/candles?interval=1d&limit=1000&start={st}") or []
    r=sorted([(c[0],float(c[4])) for c in d],key=lambda x:x[0])
    if r and now_ms()-r[-1][0]<86400000: r.pop()
    return r
def hist_4h(m):
    start=int((datetime.now(timezone.utc)-timedelta(days=DAYS_4H)).timestamp()*1000); acc={}; end=now_ms()
    for _ in range(12):
        d=get(f"/{m}/candles?interval=4h&limit=1000&end={end}")
        if not d: break
        ts=[]
        for c in d:
            t=c[0]; ts.append(t)
            if t>=start: acc[t]=(t,float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5]))
        o=min(ts)
        if o<=start or len(d)<1000: break
        end=o-1; time.sleep(0.12)
    r=[acc[k] for k in sorted(acc)]
    if r and now_ms()-r[-1][0]<FOUR_H_MS: r.pop()
    return r
def sma(xs,p,i): return sum(xs[i-p+1:i+1])/p if i>=p-1 else None
def median(xs):
    s=sorted(xs); n=len(s); return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2
def rsi(cl,i,p=14):
    if i<p: return None
    g=l=0.0
    for k in range(i-p+1,i+1):
        d=cl[k]-cl[k-1]; g+=max(d,0); l+=max(-d,0)
    return 100-100/(1+g/l) if l>0 else 100.0
def atr(rows,i,p=14):
    if i<p: return None
    s=0.0
    for k in range(i-p+1,i+1):
        h,lo,pc=rows[k][2],rows[k][3],rows[k-1][4]; s+=max(h-lo,abs(h-pc),abs(lo-pc))
    return s/p
def vol24(rows,i): return sum(rows[k][5]*rows[k][4] for k in range(max(0,i-5),i+1))

def run(series,btc4,bear):
    trades=[]
    for b,rows in series.items():
        if b=="BTC" or len(rows)<40: continue
        cl=[r[4] for r in rows]; i=20
        while i<len(rows):
            t,o,h,l,c,v=rows[i]
            drop=c/rows[i-12][4]-1 if i>=12 else 0
            r=rsi(cl,i); a=atr(rows,i)
            vmed=median([rows[k][5] for k in range(i-20,i)])
            wick=(c-l)/(h-l) if h>l else 0
            btc_ret=btc4.get(t,0)
            liq=vol24(rows,i)
            climax = v>=CLIMAX_VOL*vmed and wick>=WICK_MIN
            if (bear(t) and drop<=-0.10 and r is not None and r<30 and a and a>0
                    and liq>=LIQ_MIN and climax and btc_ret>=BTC_FLOOR):
                entry=c; stopd=ATR_STOP*a; stp=entry-stopd; tgt=entry+RR*stopd; res=None
                for j in range(i+1,min(i+1+MAX_HOLD,len(rows))):
                    if rows[j][3]<=stp: res=(stp-entry)/entry*100; i=j; break
                    if rows[j][2]>=tgt: res=(tgt-entry)/entry*100; i=j; break
                if res is None:
                    j=min(i+MAX_HOLD,len(rows)-1); res=(rows[j][4]-entry)/entry*100; i=j
                trades.append(res-COST_RT)
            i+=1
    return trades

def rep(name,nets):
    if not nets: print(f"{name}: 0 trades (filters te streng)"); return
    w=[x for x in nets if x>0]
    print(f"{name}: n={len(nets)} | winrate={100*len(w)/len(nets):.1f}% | /trade={sum(nets)/len(nets):+.2f}% | som={sum(nets):+.0f}")
    if w: print(f"   gem.win={sum(w)/len(w):+.2f}% | gem.verlies={sum(x for x in nets if x<=0)/max(1,len(nets)-len(w)):+.2f}% | breakeven-wr={100*(-sum(x for x in nets if x<=0)/max(1,len(nets)-len(w)))/((sum(w)/len(w))+(-sum(x for x in nets if x<=0)/max(1,len(nets)-len(w)))):.0f}%")

def main():
    data=get("/ticker/24h") or []; eur=[]
    for x in data:
        mkt=x.get("market","")
        if not mkt.endswith("-EUR"): continue
        bb=mkt.split("-")[0]
        if bb in SKIP: continue
        try: qv=float(x.get("volumeQuote") or 0)
        except: qv=0.0
        eur.append((bb,mkt,qv))
    eur.sort(key=lambda z:-z[2]); uni=eur[:TOP_N]
    btc=daily("BTC-EUR"); bc=[c for _,c in btc]; reg={}
    for i in range(len(btc)):
        s=sma(bc,200,i); dd=datetime.fromtimestamp(btc[i][0]/1000,tz=timezone.utc).date().isoformat()
        reg[dd]=(s is not None and bc[i]>s)
    bear=lambda ts: not reg.get(datetime.fromtimestamp(ts/1000,tz=timezone.utc).date().isoformat(),False)
    print("4h ophalen...",flush=True)
    series={}; b4=hist_4h("BTC-EUR")
    btc4={b4[i][0]:(b4[i][4]/b4[i-1][4]-1) for i in range(1,len(b4))}
    for b,mkt,q in uni:
        r=hist_4h(mkt)
        if len(r)>=40: series[b]=r
    print(f"{len(series)} coins · {len([1 for b,_,q in uni if q>=LIQ_MIN])} coins >= EUR2M (P1-universum)\n",flush=True)
    rep("C2-VERBETERD (P1+P2+P3, Kimi)", run(series,btc4,bear))
    print(f"\nKosten {COST_RT}% rt (limit-maker+liquide) · stop {ATR_STOP}xATR · target {RR}R · 48u ·")
    print("climax=vol>=3x mediaan + wick>=60% · BTC 4u>=-2% · liquiditeit>=EUR2M. Survivorship-bias.")

if __name__=="__main__": main()
