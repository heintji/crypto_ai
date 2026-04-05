"""
market_data.py - Fase 4: Externe Marktdata en Cross-Referencing
================================================================
Versie: 1.0

Databronnen (gratis, geen API key nodig):
  1. Fear and Greed Index  - alternative.me
  2. Binance Funding Rate  - fapi.binance.com
  3. Binance Open Interest - fapi.binance.com
  4. BTC Dominantie       - api.coingecko.com

Cross-referencing: score>=+2=GO, <=-2=STOP, anders=VOORZICHTIG
DB cache via market_signals tabel (max 1 API call per uur per bron)
"""
from __future__ import annotations
import os,json,requests,psycopg2
from datetime import datetime,timezone
from typing import Optional,Dict,List,Any

DATABASE_URL=os.environ.get("DATABASE_URL","")

def now_utc(): return datetime.now(timezone.utc)
def log(m): print(f"[MKTDATA {now_utc():%H:%M:%S}] {m}",flush=True)
def sf(v,d=0.0):
    try: return float(v or d)
    except: return d
def safe_rb(conn):
    try: conn.rollback()
    except: pass

class MarketData:
    """Centrale klasse voor externe marktdata met DB caching.
    Haalt data van externe APIs, cachet in market_signals tabel.
    Alle methoden hebben fallback bij API fout."""

    def __init__(self,conn): self.conn=conn

    def _lees_cache(self,bron,symbool="MARKET"):
        try:
            with self.conn.cursor() as cur:
                cur.execute("""SELECT waarde,label,richting,score FROM public.market_signals
                    WHERE bron=%s AND symbool=%s AND geldig_tot>NOW()
                    ORDER BY opgehaald_op DESC LIMIT 1""", (bron,symbool))
                r=cur.fetchone()
            if r: return {"bron":bron,"symbool":symbool,"waarde":r[0],
                "label":r[1],"richting":r[2],"score":r[3],"uit_cache":True}
        except Exception as e: log(f"Cache lees {bron}: {e}")
        return None

    def _schrijf_cache(self,bron,symbool,waarde,label,richting,score,raw=None,uur=1):
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.market_signals
                        (bron,symbool,waarde,label,richting,score,raw_json,opgehaald_op,geldig_tot)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),NOW()+%s*INTERVAL'1 hour')
                    ON CONFLICT(bron,symbool) DO UPDATE SET
                        waarde=EXCLUDED.waarde,label=EXCLUDED.label,
                        richting=EXCLUDED.richting,score=EXCLUDED.score,
                        raw_json=EXCLUDED.raw_json,opgehaald_op=NOW(),
                        geldig_tot=NOW()+%s*INTERVAL'1 hour'
                """, (bron,symbool,waarde,label,richting,score,
                    json.dumps(raw or {}),uur,uur))
            self.conn.commit()
        except Exception as e: log(f"Cache schrijf {bron}: {e}"); safe_rb(self.conn)

    def haal_fear_greed_op(self):
        """Fear and Greed Index 0-100. <20=Extreme Fear=BULLISH, >80=Extreme Greed=BEARISH. Cache 6u."""
        c=self._lees_cache("FEAR_GREED")
        if c: return c
        try:
            d=requests.get("https://api.alternative.me/fng/?limit=1",timeout=8).json()["data"][0]
            v=int(d["value"]); lbl=d["value_classification"]
            if v<=20: ri,sc="BULLISH",2
            elif v<=40: ri,sc="BULLISH",1
            elif v<=60: ri,sc="NEUTRAAL",0
            elif v<=80: ri,sc="BEARISH",-1
            else: ri,sc="BEARISH",-2
            label=f"F&G:{v} ({lbl})"; self._schrijf_cache("FEAR_GREED","MARKET",v,label,ri,sc,d,uur=6)
            log(f"Fear&Greed:{v}/100 -> {ri} ({sc:+d})")
            return {"bron":"FEAR_GREED","symbool":"MARKET","waarde":v,"label":label,"richting":ri,"score":sc}
        except Exception as e:
            log(f"F&G fout: {e}"); return {"bron":"FEAR_GREED","symbool":"MARKET","waarde":50,"label":"F&G:50","richting":"NEUTRAAL","score":0}

    def haal_funding_rates_op(self,symbolen=None):
        """Binance funding rates alle coins. Negatief=BULLISH, hoog positief=BEARISH. Slaat op in funding_rates tabel."""
        try:
            alle=requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",timeout=10).json()
            resultaten={}; batch=[]
            for item in alle:
                sym=item.get("symbol","")
                if not sym.endswith("USDT"): continue
                if symbolen and sym not in symbolen: continue
                fr=sf(item.get("lastFundingRate",0))*100
                if fr<-0.01: ri,sc="BULLISH",1
                elif fr>0.05: ri,sc="BEARISH",-1
                else: ri,sc="NEUTRAAL",0
                batch.append((sym,fr,0,ri))
                resultaten[sym]={"bron":"FUNDING_RATE","symbool":sym,"waarde":fr,
                    "label":f"FR:{fr:+.4f}%","richting":ri,"score":sc}
            if batch:
                with self.conn.cursor() as cur:
                    cur.executemany("""INSERT INTO public.funding_rates(symbool,funding_rate,open_interest,richting,opgehaald_op)
                        VALUES(%s,%s,%s,%s,NOW()) ON CONFLICT(symbool) DO UPDATE SET
                        funding_rate=EXCLUDED.funding_rate,richting=EXCLUDED.richting,opgehaald_op=NOW()""",batch)
                self.conn.commit()
            log(f"Funding rates: {len(resultaten)} coins"); return resultaten
        except Exception as e: log(f"Funding API fout: {e}"); return {}

    def haal_funding_rate_coin_op(self,symbool):
        """Funding rate 1 coin, eerst uit DB cache (2u) dan live via Binance API."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""SELECT funding_rate,richting FROM public.funding_rates
                    WHERE symbool=%s AND opgehaald_op>NOW()-INTERVAL'2 hours'""",(symbool,))
                r=cur.fetchone()
            if r:
                fr,ri=r; sc=1 if ri=="BULLISH" else (-1 if ri=="BEARISH" else 0)
                return {"bron":"FUNDING_RATE","symbool":symbool,"waarde":fr,
                    "richting":ri,"score":sc,"label":f"FR:{fr:+.4f}%","uit_cache":True}
        except Exception: pass
        try:
            d=requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbool}",timeout=5).json()
            fr=sf(d.get("lastFundingRate",0))*100
            if fr<-0.01: ri,sc="BULLISH",1
            elif fr>0.05: ri,sc="BEARISH",-1
            else: ri,sc="NEUTRAAL",0
            return {"bron":"FUNDING_RATE","symbool":symbool,"waarde":fr,"richting":ri,"score":sc,"label":f"FR:{fr:+.4f}%"}
        except Exception:
            return {"bron":"FUNDING_RATE","symbool":symbool,"waarde":0.0,"richting":"NEUTRAAL","score":0,"label":"FR:onbekend"}

    def haal_open_interest_op(self,symbool="BTCUSDT"):
        """BTC Open Interest via Binance. Stijgend >5pct=BULLISH, dalend <-5pct=BEARISH. Cache 1u."""
        c=self._lees_cache("OPEN_INTEREST",symbool)
        if c: return c
        try:
            d=requests.get(f"https://fapi.binance.com/fapi/v1/openInterestHist?symbol={symbool}&period=1d&limit=2",timeout=8).json()
            if len(d)>=2:
                oi_nu=sf(d[-1].get("sumOpenInterest",0)); oi_g=sf(d[-2].get("sumOpenInterest",0))
                pct=((oi_nu-oi_g)/oi_g*100) if oi_g>0 else 0
            else:
                oi_nu=sf(requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbool}",timeout=5).json().get("openInterest",0)); pct=0
            if pct>=5: ri,sc="BULLISH",1
            elif pct<=-5: ri,sc="BEARISH",-1
            else: ri,sc="NEUTRAAL",0
            label=f"OI:{oi_nu:,.0f}({pct:+.1f}%)"; self._schrijf_cache("OPEN_INTEREST",symbool,oi_nu,label,ri,sc,{"pct":pct})
            log(f"OI {symbool}: {label} -> {ri}")
            return {"bron":"OPEN_INTEREST","symbool":symbool,"waarde":oi_nu,"pct":pct,"label":label,"richting":ri,"score":sc}
        except Exception as e:
            log(f"OI fout: {e}"); return {"bron":"OPEN_INTEREST","symbool":symbool,"waarde":0,"richting":"NEUTRAAL","score":0,"label":"OI:onbekend"}

    def haal_btc_dominantie_op(self):
        """BTC marktdominantie CoinGecko. <40pct=BULLISH alts, >60pct=BEARISH alts. Cache 4u."""
        c=self._lees_cache("BTC_DOMINANTIE")
        if c: return c
        try:
            dom=sf(requests.get("https://api.coingecko.com/api/v3/global",timeout=8,headers={"User-Agent":"CryptoBot/1.0"}).json().get("data",{}).get("market_cap_percentage",{}).get("btc",50))
            if dom<40: ri,sc="BULLISH",1
            elif dom>60: ri,sc="BEARISH",-1
            else: ri,sc="NEUTRAAL",0
            label=f"BTC.D:{dom:.1f}%"; self._schrijf_cache("BTC_DOMINANTIE","MARKET",dom,label,ri,sc,{},uur=4)
            log(f"BTC.D:{dom:.1f}% -> {ri}")
            return {"bron":"BTC_DOMINANTIE","symbool":"MARKET","waarde":dom,"label":label,"richting":ri,"score":sc}
        except Exception as e:
            log(f"BTC.D fout: {e}"); return {"bron":"BTC_DOMINANTIE","symbool":"MARKET","waarde":50,"label":"BTC.D:onbekend","richting":"NEUTRAAL","score":0}

    def haal_alle_signalen_op(self):
        """Haalt alle markt signalen op. Gebruikt DB cache waar mogelijk."""
        return [self.haal_fear_greed_op(), self.haal_open_interest_op("BTCUSDT"), self.haal_btc_dominantie_op(), self.haal_nieuws_sentiment_op()]

    def cross_reference_advies(self):
        """Cross-referencing: alle signalen combineren tot 1 handelsadvies.
        Score systeem: BULLISH=+1tot+2, NEUTRAAL=0, BEARISH=-1tot-2.
        Totaal>=+2=GO (2+ bronnen bullish), <=-2=STOP (2+ bronnen bearish), anders=VOORZICHTIG.
        Geeft dict terug met advies, score, uitleg en alle signalen."""
        signalen=self.haal_alle_signalen_op()
        totaal=sum(s.get("score",0) for s in signalen)
        n_bull=sum(1 for s in signalen if s.get("richting")=="BULLISH")
        n_bear=sum(1 for s in signalen if s.get("richting")=="BEARISH")
        n_neut=sum(1 for s in signalen if s.get("richting")=="NEUTRAAL")
        if totaal>=2: advies,kleur="GO","GROEN"
        elif totaal<=-2: advies,kleur="STOP","ROOD"
        else: advies,kleur="VOORZICHTIG","ORANJE"
        uitleg=(f"{advies}|Score:{totaal:+d}|Bull:{n_bull} Bear:{n_bear} Neut:{n_neut}|"
            +" | ".join(s.get("label","?") for s in signalen))
        log(f"Cross-Ref: {uitleg}")
        return {"advies":advies,"kleur":kleur,"totaal_score":totaal,
            "n_bullish":n_bull,"n_bearish":n_bear,"n_neutraal":n_neut,
            "signalen":signalen,"uitleg":uitleg,"go":advies=="GO"}

    # RSS feeds: 4 grote crypto nieuwsbronnen
    RSS_FEEDS=[
        ("Cointelegraph","https://cointelegraph.com/rss"),
        ("Decrypt","https://decrypt.co/feed"),
        ("CoinDesk","https://coindesk.com/arc/outboundfeeds/rss/"),
        ("BitcoinMagazine","https://bitcoinmagazine.com/feed"),
    ]
    # FUD keywords: negatief nieuws = BEARISH signaal
    FUD_KEYWORDS=["ban","banned","hack","hacked","crash","crashing","sec","lawsuit",
        "fraud","scam","arrested","seized","collapse","insolvent","bankrupt",
        "warning","illegal","criminal","ponzi","rug","exploit","stolen"]
    # FOMO keywords: positief nieuws = BULLISH signaal
    FOMO_KEYWORDS=["etf","approved","approval","bullish","rally","ath","all-time high",
        "partnership","adoption","launch","upgrade","institutional","bitcoin reserve",
        "legal tender","nation","buy","accumulate","breakthrough","record"]

    def haal_nieuws_sentiment_op(self):
        """
        Analyseert nieuws van 4 grote crypto RSS feeds op sentiment.
        Telt FUD keywords (negatief) en FOMO keywords (positief) in
        de laatste 20 artikelen van elke bron.

        FUD woorden: ban, hack, crash, sec, fraud, scam, exploit etc.
        FOMO woorden: etf, approved, rally, ath, adoption, institutional etc.

        Score logica:
          FOMO > FUD*2 en FOMO >= 3  -> BULLISH (+1)
          FUD > FOMO*2 en FUD >= 3   -> BEARISH (-1)
          Anders                     -> NEUTRAAL (0)

        Cache: 30 minuten (nieuws verandert snel).
        """
        import xml.etree.ElementTree as ET
        c=self._lees_cache("NIEUWS_SENTIMENT")
        if c: return c

        fud_totaal=0; fomo_totaal=0; artikelen_gelezen=0; bronnen_ok=[]

        for naam,url in self.RSS_FEEDS:
            try:
                resp=requests.get(url,timeout=6,headers={"User-Agent":"CryptoBot/1.0"})
                if resp.status_code!=200: continue
                root=ET.fromstring(resp.text)
                items=root.findall(".//item")[:20]
                for item in items:
                    tekst=(item.findtext("title","")+" "+item.findtext("description","")).lower()
                    fud_totaal+=sum(1 for kw in self.FUD_KEYWORDS if kw in tekst)
                    fomo_totaal+=sum(1 for kw in self.FOMO_KEYWORDS if kw in tekst)
                    artikelen_gelezen+=1
                bronnen_ok.append(naam)
            except Exception as e:
                log(f"RSS {naam} fout: {e}")

        # Bepaal sentiment
        if fomo_totaal>fud_totaal*2 and fomo_totaal>=3:
            ri,sc="BULLISH",1
        elif fud_totaal>fomo_totaal*2 and fud_totaal>=3:
            ri,sc="BEARISH",-1
        else:
            ri,sc="NEUTRAAL",0

        label=f"Nieuws:FOMO={fomo_totaal}/FUD={fud_totaal}({artikelen_gelezen}art)"
        self._schrijf_cache("NIEUWS_SENTIMENT","MARKET",float(fomo_totaal-fud_totaal),
            label,ri,sc,{"fomo":fomo_totaal,"fud":fud_totaal,"bronnen":bronnen_ok},uur=0)

        # Sla 30 minuten op via aparte geldig_tot
        try:
            with self.conn.cursor() as cur:
                cur.execute("""UPDATE public.market_signals SET geldig_tot=NOW()+INTERVAL'30 minutes'
                    WHERE bron='NIEUWS_SENTIMENT' AND symbool='MARKET'""")
            self.conn.commit()
        except Exception: pass

        log(f"Nieuws sentiment: {label} -> {ri} (bronnen:{bronnen_ok})")
        return {"bron":"NIEUWS_SENTIMENT","symbool":"MARKET",
            "waarde":float(fomo_totaal-fud_totaal),"label":label,
            "richting":ri,"score":sc,"fomo":fomo_totaal,"fud":fud_totaal}

    def funding_rate_bonus(self,symbool):
        """Score bonus/malus voor 1 coin op basis van funding rate.
        Gebruikt door multi_coin_score om coin score aan te passen.
        Negatieve FR (+3): shorts betalen longs, BULLISH.
        Hoge FR (-3): te veel longs, overextended BEARISH."""
        fr=self.haal_funding_rate_coin_op(symbool).get("waarde",0)
        if fr<-0.01: return 3
        elif fr<0: return 2
        elif fr<0.02: return 0
        elif fr<0.05: return -2
        else: return -3
