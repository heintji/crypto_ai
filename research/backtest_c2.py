#!/usr/bin/env python3
"""BACKTEST C2_BOUNCE (crash-bounce) op ~400d 4h-data (benadering; C2 gebruikt live
1h-RSI + 12u-hold). Signaal: bear-regime + coin -10% in 48u + RSI14<25 -> long,
target +3%, stop -2%, max 12u (3x4h). Realistische kosten. Read-only Bitvavo-API.
"""
import time, json, urllib.request
from datetime import datetime, timezone, timedelta

BITVAVO = "https://api.bitvavo.com/v2"
TOP_N = 40; DAYS_4H = 400; DAYS_D = 800; FEE_RT = 0.50; FOUR_H_MS = 4*3600*1000
MAJORS = {"BTC","ETH","SOL","XRP","ADA","DOGE","LINK","AVAX","DOT","POL"}
SKIP = {"EUR","USDT","USDC","EURT","DAI","TUSD","FDUSD","PYUSD","EURC"}
DROP_48H = -0.10; RSI_MAX = 25.0; TARGET = 0.03; STOP = -0.02; MAX_HOLD = 3  # 3x4h=12u

def get(p):
    for _ in range(3):
        try: return json.load(urllib.request.urlopen(f"{BITVAVO}{p}",timeout=20))
        except Exception: time.sleep(1)
    return None
def now_ms(): return int(datetime.now(timezone.utc).timestamp()*1000)
def daily(m,days=DAYS_D):
    st=int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp()*1000)
    d=get(f"/{m}/candles?interval=1d&limit=1000&start={st}") or []
    rows=sorted([(c[0],float(c[4])) for c in d],key=lambda x:x[0])
    if rows and now_ms()-rows[-1][0]<86400000: rows.pop()
    return rows
def hist_4h(m,days=DAYS_4H):
    start=int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp()*1000); acc={}; end=now_ms()
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
    rows=[acc[k] for k in sorted(acc)]
    if rows and now_ms()-rows[-1][0]<FOUR_H_MS: rows.pop()
    return rows
def sma(xs,p,i): return sum(xs[i-p+1:i+1])/p if i>=p-1 else None
def rsi(closes,i,p=14):
    if i<p: return None
    g=l=0.0
    for k in range(i-p+1,i+1):
        d=closes[k]-closes[k-1]; g+=max(d,0); l+=max(-d,0)
    return 100-100/(1+g/l) if l>0 else 100.0
def cost_rt(base,v):
    return FEE_RT + (0.10 if base in MAJORS else (0.80 if v and 0<v<5_000_000 else 0.30))
def vol24(rows,i): return sum(rows[k][5]*rows[k][4] for k in range(max(0,i-5),i+1))

def main():
    data=get("/ticker/24h") or []
    eur=[]
    for x in data:
        mkt=x.get("market","");
        if not mkt.endswith("-EUR"): continue
        b=mkt.split("-")[0]
        if b in SKIP: continue
        try: qv=float(x.get("volumeQuote") or 0)
        except: qv=0.0
        eur.append((b,mkt,qv))
    eur.sort(key=lambda z:-z[2]); uni=eur[:TOP_N]
    btc=daily("BTC-EUR"); bclose=[c for _,c in btc]
    reg={}
    for i in range(len(btc)):
        s=sma(bclose,200,i); d=datetime.fromtimestamp(btc[i][0]/1000,tz=timezone.utc).date().isoformat()
        reg[d]=(s is not None and bclose[i]>s)
    def bear(ts): return not reg.get(datetime.fromtimestamp(ts/1000,tz=timezone.utc).date().isoformat(),False)
    print(f"top-{len(uni)} coins · 4h-historie ophalen...",flush=True)
    trades=[]
    for b,mkt,_ in uni:
        rows=hist_4h(mkt)
        if len(rows)<40: continue
        closes=[r[4] for r in rows]; i=13
        while i<len(rows):
            t,o,h,l,c,v=rows[i]
            drop=c/rows[i-12][4]-1 if i>=12 else 0
            r=rsi(closes,i)
            if bear(t) and drop<=DROP_48H and r is not None and r<RSI_MAX:
                entry=c; tgt=entry*(1+TARGET); stp=entry*(1+STOP); vv=vol24(rows,i); done=False
                for j in range(i+1,min(i+1+MAX_HOLD,len(rows))):
                    hh,ll,cc=rows[j][2],rows[j][3],rows[j][4]
                    if ll<=stp: trades.append({"net":STOP*100-cost_rt(b,vv)}); i=j; done=True; break
                    if hh>=tgt: trades.append({"net":TARGET*100-cost_rt(b,vv)}); i=j; done=True; break
                if not done:
                    j=min(i+MAX_HOLD,len(rows)-1)
                    trades.append({"net":(rows[j][4]-entry)/entry*100-cost_rt(b,vv)}); i=j
            i+=1
    nets=[x["net"] for x in trades]; wins=[x for x in nets if x>0]
    if not nets: print("C2: 0 signalen in de periode"); return
    print(f"\nC2_BOUNCE backtest (~{DAYS_4H}d, 4h-benadering):")
    print(f"  trades={len(nets)} | W={len(wins)}/L={len(nets)-len(wins)} | winrate={100*len(wins)/len(nets):.1f}%")
    print(f"  som%={sum(nets):.1f} | per trade={sum(nets)/len(nets):.3f}%")
    print(f"  gem.win={sum(wins)/len(wins):.2f}% | gem.verlies={sum(x for x in nets if x<=0)/max(1,len(nets)-len(wins)):.2f}%")
    print("\nKosten fee 0,5%+spread rt. Survivorship-bias. 4h-benadering (live=1h-RSI).")

if __name__=="__main__": main()
