#!/usr/bin/env python3
"""C2-DIAGNOSE: waar gaat het mis + test verbeter-varianten. Haalt 4h-data 1x op
en draait meerdere C2-varianten + diagnostiek over dezelfde data. Read-only."""
import time, json, urllib.request
from datetime import datetime, timezone, timedelta

BITVAVO="https://api.bitvavo.com/v2"; TOP_N=40; DAYS_4H=400; DAYS_D=800; FEE_RT=0.50; FOUR_H_MS=4*3600*1000
MAJORS={"BTC","ETH","SOL","XRP","ADA","DOGE","LINK","AVAX","DOT","POL"}
SKIP={"EUR","USDT","USDC","EURT","DAI","TUSD","FDUSD","PYUSD","EURC"}

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
def rsi(cl,i,p=14):
    if i<p: return None
    g=l=0.0
    for k in range(i-p+1,i+1):
        d=cl[k]-cl[k-1]; g+=max(d,0); l+=max(-d,0)
    return 100-100/(1+g/l) if l>0 else 100.0
def cost_rt(b,v): return FEE_RT+(0.10 if b in MAJORS else (0.80 if v and 0<v<5_000_000 else 0.30))
def vol24(rows,i): return sum(rows[k][5]*rows[k][4] for k in range(max(0,i-5),i+1))

def run_c2(series,bear,drop_max=-0.10,drop_floor=-1.0,rsi_max=25.0,tp=0.03,sl=-0.02,max_hold=3,confirm=False):
    trades=[]
    for b,rows in series.items():
        if b=="BTC" or len(rows)<40: continue
        cl=[r[4] for r in rows]; i=13
        while i<len(rows):
            t,o,h,l,c,v=rows[i]
            drop=c/rows[i-12][4]-1 if i>=12 else 0
            r=rsi(cl,i)
            sig=bear(t) and drop_floor<=drop<=drop_max and r is not None and r<rsi_max
            if sig:
                ei=i
                if confirm:  # wacht op eerste groene 4h-candle (bounce bevestigd)
                    ei=None
                    for j in range(i+1,min(i+4,len(rows))):
                        if rows[j][4]>rows[j][1]: ei=j; break
                    if ei is None: i+=1; continue
                entry=rows[ei][4]; tgt=entry*(1+tp); stp=entry*(1+sl); vv=vol24(rows,ei); res=None; ex="TIME"
                for j in range(ei+1,min(ei+1+max_hold,len(rows))):
                    if rows[j][3]<=stp: res=sl*100; ex="STOP"; i=j; break
                    if rows[j][2]>=tgt: res=tp*100; ex="TARGET"; i=j; break
                if res is None:
                    j=min(ei+max_hold,len(rows)-1); res=(rows[j][4]-entry)/entry*100; i=j
                trades.append({"net":res-cost_rt(b,vv),"ex":ex,"drop":drop,"rsi":r})
            i+=1
    return trades

def summ(tr):
    if not tr: return "0 trades"
    nets=[x["net"] for x in tr]; w=[x for x in nets if x>0]
    return f"n={len(nets):<5} wr={100*len(w)/len(nets):>4.1f}%  /trade={sum(nets)/len(nets):>+6.2f}%  som={sum(nets):>+7.0f}"

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
    print("4h ophalen...",flush=True); series={}
    for b,mkt,_ in uni:
        r=hist_4h(mkt)
        if len(r)>=40: series[b]=r
    print(f"{len(series)} coins\n",flush=True)

    base=run_c2(series,bear)
    print("BASELINE (drop<=-10%, RSI<25, TP+3/SL-2, 12u):", summ(base))
    # diagnostiek exit-redenen
    from collections import Counter
    ex=Counter(x["ex"] for x in base)
    for k in ("TARGET","STOP","TIME"):
        sub=[x["net"] for x in base if x["ex"]==k]
        if sub: print(f"   exit {k:<7} n={len(sub):<5} ({100*len(sub)/len(base):>4.1f}%)  gem={sum(sub)/len(sub):+.2f}%")
    # winrate per drop-diepte
    print("   winrate per crash-diepte:")
    for lo,hi,lab in [(-0.15,-0.10,"-10..-15%"),(-0.25,-0.15,"-15..-25%"),(-1.0,-0.25,"<-25%")]:
        sub=[x for x in base if lo<=x["drop"]<hi]
        if sub: w=[x for x in sub if x["net"]>0]; print(f"     {lab:<10} n={len(sub):<5} wr={100*len(w)/len(sub):.1f}% /trade={sum(y['net'] for y in sub)/len(sub):+.2f}%")
    print("\nVARIANTEN:")
    print("  A confirm (wacht op groene candle):     ", summ(run_c2(series,bear,confirm=True)))
    print("  B matige dip only (-10..-18%):          ", summ(run_c2(series,bear,drop_floor=-0.18)))
    print("  C langer vasthouden (24u):              ", summ(run_c2(series,bear,max_hold=6)))
    print("  D RSI<20 (extremer oversold):           ", summ(run_c2(series,bear,rsi_max=20)))
    print("  E ruimere target TP+5/SL-3:             ", summ(run_c2(series,bear,tp=0.05,sl=-0.03)))
    print("  F strakke stop TP+3/SL-1.5:             ", summ(run_c2(series,bear,sl=-0.015)))
    print("  G confirm + matige dip + 24u:           ", summ(run_c2(series,bear,confirm=True,drop_floor=-0.18,max_hold=6)))

if __name__=="__main__": main()
