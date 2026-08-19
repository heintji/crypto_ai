"""Strategie-race — alle shadow-strategieen naast elkaar.

Toont: (1) overzicht per strategie (winrate + netto winst), (2) de OPEN trades
die nu lopen met live Bitvavo-koers + onrealised %, (3) per trade te volgen
(filterbaar per strategie). Puur read-only op de bestaande strategie-tabellen.
"""
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
import requests

from dashboard.db import run_query

# Strategieen die net live zijn en (in bear-regime) nog 0 trades kunnen hebben,
# maar wel altijd getoond moeten worden.
STRATS_NIEUW = ["FABER", "ROTATIE", "DONCHIAN"]

UNION_SQL = """
SELECT strategie, coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, pnl_net_pct FROM strat_shadow_trades
UNION ALL SELECT 'mr_trail', coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, pnl_net_pct FROM mr_trail_trades
UNION ALL SELECT 'mr_shadow', coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, NULL::double precision FROM mr_shadow_trades
UNION ALL SELECT 'mr_ultimate', coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, NULL::double precision FROM mr_ultimate_trades
UNION ALL SELECT 'mr_vermijd_extreem', coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, NULL::double precision FROM mr_vermijd_extreem
UNION ALL SELECT 'mr_hoog_vertrouwen', coin, entry_ts, entry, stop, target, status, exit_ts, exit_prijs, pnl_pct, NULL::double precision FROM mr_hoog_vertrouwen
UNION ALL SELECT strategie, markt, entry_ts, entry_prijs, initial_stop, initial_target, status, exit_ts, exit_prijs, pnl_pct, NULL::double precision FROM plan_u_trades
"""

CLOSED = ("WIN", "LOSS", "TIME")


@st.cache_data(ttl=20)
def _load():
    df = run_query(UNION_SQL)
    if not df.empty:
        df["pnl"] = df["pnl_net_pct"].fillna(df["pnl_pct"])
    return df


@st.cache_data(ttl=30)
def _prices():
    try:
        data = requests.get("https://api.bitvavo.com/v2/ticker/price", timeout=10).json()
        return {d["market"]: float(d["price"]) for d in data if d.get("price")}
    except Exception:
        return {}


def _market(coin):
    if not coin:
        return None
    if coin.endswith("USDT"):
        return coin[:-4] + "-EUR"
    if "-" in coin:
        return coin
    return coin + "-EUR"


def render():
    c1, c2 = st.columns([5, 1])
    c1.header("🏁 Strategie-race — shadow")
    if c2.button("🔄 Ververs", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(
        "Alle shadow-strategieen naast elkaar · open trades live · per trade te volgen · "
        f"laatst ververst {datetime.now(timezone.utc):%H:%M:%S UTC} (data max 20s oud)"
    )

    df = _load()
    if df.empty:
        st.info("Nog geen strategie-data.")
        return

    closed = df[df["status"].isin(CLOSED)].copy()
    opens = df[df["status"] == "OPEN"].copy()

    # ---- 1. Overzicht per strategie ----
    st.subheader("Overzicht per strategie")
    if not closed.empty:
        agg = closed.groupby("strategie").agg(
            win=("pnl", lambda s: int((s > 0).sum())),
            los=("pnl", lambda s: int((s <= 0).sum())),
            winrate=("pnl", lambda s: round(100 * (s > 0).mean(), 1)),
            winst_pct=("pnl", lambda s: round(s.sum(), 1)),
            per_trade=("pnl", lambda s: round(s.mean(), 3)),
        )
    else:
        agg = pd.DataFrame(columns=["win", "los", "winrate", "winst_pct", "per_trade"])
    opn = opens.groupby("strategie").size().rename("open")
    tab = agg.join(opn, how="outer")
    for s in STRATS_NIEUW:
        if s not in tab.index:
            tab.loc[s] = {"win": 0, "los": 0, "winrate": None, "winst_pct": None, "per_trade": None, "open": 0}
    for col in ("open", "win", "los"):
        tab[col] = tab[col].fillna(0).astype(int)
    tab = tab.sort_values("winst_pct", ascending=False, na_position="last").reset_index()
    tab = tab.rename(columns={"index": "strategie"})
    st.dataframe(
        tab[["strategie", "open", "win", "los", "winrate", "winst_pct", "per_trade"]],
        use_container_width=True, hide_index=True,
        column_config={
            "open": "Open",
            "win": "Wins",
            "los": "Losses",
            "winrate": st.column_config.NumberColumn("Winrate %", format="%.1f"),
            "winst_pct": st.column_config.NumberColumn("Winst % (netto)", format="%.1f"),
            "per_trade": st.column_config.NumberColumn("Per trade %", format="%.3f"),
        },
    )
    st.caption("Winst/per-trade = netto waar beschikbaar (fees+spread), anders bruto. "
               "Winst % = optelsom van de netto trade-percentages (relatieve maat; bij €100/trade ≈ dat bedrag in €). "
               "FABER/ROTATIE/DONCHIAN draaien mee maar wachten op BTC > 200d-SMA (bear = cash).")

    # ---- 2. Open trades — lopend, live ----
    st.subheader(f"🔴 Open trades — lopend ({len(opens)})")
    if opens.empty:
        st.info("Geen open trades op dit moment.")
    else:
        pr = _prices()
        rows = []
        for _, t in opens.iterrows():
            mkt = _market(t["coin"])
            nu = pr.get(mkt)
            entry = t["entry"]
            onreal = round((nu / entry - 1) * 100, 2) if (nu and entry) else None
            rows.append({
                "strategie": t["strategie"],
                "coin": t["coin"],
                "sinds": pd.to_datetime(t["entry_ts"]).strftime("%d-%m %H:%M") if pd.notna(t["entry_ts"]) else "",
                "entry": entry,
                "nu": nu,
                "onreal_%": onreal,
                "stop": t["stop"],
                "target": t["target"],
            })
        odf = pd.DataFrame(rows).sort_values("onreal_%", ascending=False, na_position="last")
        st.dataframe(odf, use_container_width=True, hide_index=True)
        st.caption("nu = live Bitvavo-koers (max 60s oud). onreal_% = onrealised winst/verlies t.o.v. entry.")

    # ---- 3. Per trade volgen ----
    st.subheader("🔎 Per trade")
    c1, c2 = st.columns([2, 1])
    keuze = c1.selectbox("Strategie", ["(alle)"] + sorted(df["strategie"].unique()))
    alleen_open = c2.checkbox("Alleen open", value=False)
    d = df.copy()
    if keuze != "(alle)":
        d = d[d["strategie"] == keuze]
    if alleen_open:
        d = d[d["status"] == "OPEN"]
    d = d.sort_values("entry_ts", ascending=False)
    show = d[["strategie", "coin", "status", "entry_ts", "entry", "exit_ts", "exit_prijs", "pnl", "stop", "target"]].head(500)
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption(f"{len(d)} trades · toont laatste 500. pnl = netto% waar beschikbaar.")
