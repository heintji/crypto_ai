import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
import plotly.graph_objects as go


# ==========================================================
# CONFIG
# ==========================================================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")
DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = os.getenv("BITVAVO_ACCESS_WINDOW_MS", "10000")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", "/data/portfolio_history.csv")

AUTO_REFRESH_MINUTES = int(os.getenv("AUTO_REFRESH_MINUTES", "15"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))


# ==========================================================
# HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def parse_dt(x: Any) -> pd.Timestamp:
    try:
        return pd.to_datetime(x, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def format_dt_short(x: Any) -> str:
    dt = parse_dt(x)
    if pd.isna(dt):
        return "-"
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def format_money(x: Any) -> str:
    v = safe_float(x, 0.0)
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_pnl(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def age_minutes(epoch_seconds: float) -> float:
    return (time.time() - float(epoch_seconds)) / 60.0


def file_meta(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"exists": False, "path": path}
    stat = os.stat(path)
    return {
        "exists": True,
        "path": path,
        "size_kb": round(stat.st_size / 1024, 2),
        "modified_epoch": stat.st_mtime,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def safe_read_json(path: str):
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"JSON leesfout ({path}): {e}"


def safe_read_csv(path: str):
    try:
        if not os.path.exists(path):
            return None, f"CSV niet gevonden: {path}"
        df = pd.read_csv(path)
        return df, None
    except Exception as e:
        return None, f"CSV leesfout ({path}): {e}"


def side_color(side: str) -> str:
    s = safe_str(side).lower()
    if s == "buy":
        return "#2ecc71"
    if s == "sell":
        return "#ff5a5f"
    return "#e5e7eb"


def pnl_color(pnl: float) -> str:
    return "#5aa2ff" if pnl >= 0 else "#ff5a5f"


def outcome_color(outcome: str) -> str:
    o = safe_str(outcome).upper()
    if o == "WIN":
        return "#2ecc71"
    if o == "LOSS":
        return "#ff8c42"
    return "#cbd5e1"


# ==========================================================
# POSTGRES
# ==========================================================
@st.cache_resource(show_spinner=False)
def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def db_ready() -> bool:
    return bool(DATABASE_URL)


def table_exists(table_name: str, schema: str = "public") -> bool:
    conn = get_db_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema=%s AND table_name=%s
                )
                """,
                (schema, table_name),
            )
            return bool(cur.fetchone()[0])
    except Exception:
        return False


def get_table_columns(table_name: str, schema: str = "public") -> List[str]:
    conn = get_db_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position
                """,
                (schema, table_name),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def read_sql_df(sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    conn = get_db_conn()
    if conn is None:
        return pd.DataFrame([])
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame([])


# ==========================================================
# BITVAVO PRIVATE REQUEST
# ==========================================================
def bitvavo_request(method: str, path: str, body: str = ""):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreken.")

    method_u = method.upper()
    timestamp = str(int(time.time() * 1000))
    body = body or ""

    message = f"{timestamp}{method_u}{path}{body}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": ACCESS_WINDOW_MS,
        "Content-Type": "application/json",
    }

    url = f"{BASE_URL}{path}"

    if method_u == "GET":
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    elif method_u == "POST":
        r = requests.post(url, headers=headers, data=body, timeout=HTTP_TIMEOUT)
    else:
        raise ValueError("Alleen GET/POST ondersteund.")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")

    return r.json()


# ==========================================================
# PUBLIC PRICES
# ==========================================================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_all_market_prices():
    url = f"{BASE_URL}/v2/ticker/price"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    prices = {}
    for row in data:
        market = row.get("market")
        price = row.get("price")
        if market and price is not None:
            try:
                prices[market] = float(price)
            except Exception:
                pass
    return prices


def price_in_eur(symbol: str, prices: dict):
    if symbol == "EUR":
        return 1.0, "EUR"

    direct = f"{symbol}-EUR"
    if direct in prices:
        return prices[direct], direct

    a = f"{symbol}-USDT"
    b = "USDT-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    a = f"{symbol}-BTC"
    b = "BTC-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    return None, "NO_ROUTE"


# ==========================================================
# SNAPSHOT (mag nog via file)
# ==========================================================
def build_snapshot_with_eur_values():
    balances = bitvavo_request("GET", "/v2/balance")
    prices = fetch_all_market_prices()

    assets = []
    eur_available = 0.0

    for row in balances:
        symbol = row.get("symbol")
        available = float(row.get("available", 0) or 0)
        in_order = float(row.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total > 0:
            p_eur, route = price_in_eur(symbol, prices)
            eur_value = None
            if p_eur is not None:
                eur_value = float(total) * float(p_eur)

            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total,
                "price_eur": p_eur,
                "eur_value": eur_value,
                "price_route": route,
            })

    crypto_assets_eur = 0.0
    for a in assets:
        if a["symbol"] != "EUR" and a.get("eur_value") is not None:
            crypto_assets_eur += float(a["eur_value"])

    total_portfolio_eur = float(eur_available) + float(crypto_assets_eur)

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": float(eur_available),
        "crypto_assets_eur": float(crypto_assets_eur),
        "total_portfolio_eur": float(total_portfolio_eur),
        "assets": sorted(assets, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    ensure_parent_dir(PORTFOLIO_HISTORY_CSV)
    row = {
        "ts": snapshot["ts"],
        "eur_available": snapshot["eur_available"],
        "crypto_assets_eur": snapshot["crypto_assets_eur"],
        "total_portfolio_eur": snapshot["total_portfolio_eur"],
    }

    if os.path.exists(PORTFOLIO_HISTORY_CSV):
        hist = pd.read_csv(PORTFOLIO_HISTORY_CSV)
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])

    hist = hist.drop_duplicates(subset=["ts"], keep="last")
    hist.to_csv(PORTFOLIO_HISTORY_CSV, index=False)

    return snapshot


def maybe_auto_refresh_snapshot():
    meta = file_meta(SNAPSHOT_PATH)
    if not meta["exists"]:
        return build_snapshot_with_eur_values(), "created"

    mins = age_minutes(meta["modified_epoch"])
    if mins >= AUTO_REFRESH_MINUTES:
        return build_snapshot_with_eur_values(), f"refreshed ({mins:.0f} min old)"
    return None, f"kept (age {mins:.0f} min)"


# ==========================================================
# LOADERS UIT POSTGRES
# ==========================================================
def load_pending_orders_db() -> pd.DataFrame:
    if not table_exists("pending_approvals"):
        return pd.DataFrame([])

    cols = set(get_table_columns("pending_approvals"))

    wanted = [
        "id", "symbol", "status", "setup_type", "regime",
        "score", "chance", "confidence", "entry", "stop", "target",
        "timeframe", "created_at", "expires_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.pending_approvals
        WHERE COALESCE(status, 'PENDING') IN ('PENDING', 'APPROVED')
        ORDER BY COALESCE(chance, 0) DESC, COALESCE(score, 0) DESC, created_at DESC NULLS LAST
        LIMIT 100
    """
    return read_sql_df(sql)


def load_real_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_trades"))

    wanted = [
        "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "exit", "exit_price",
        "result_r", "outcome", "is_shadow",
        "created_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    where_shadow = "AND COALESCE(is_shadow, false) = false" if "is_shadow" in cols else ""
    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        WHERE 1=1
        {where_shadow}
        ORDER BY created_at DESC NULLS LAST
        LIMIT 500
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    if "exit_price" not in df.columns and "exit" in df.columns:
        df["exit_price"] = df["exit"]
    if "entry" not in df.columns:
        df["entry"] = 0.0

    df["datetime_raw"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
    df["datetime"] = df["datetime_raw"].apply(format_dt_short)
    df["pnl"] = pd.to_numeric(df.get("result_r"), errors="coerce").fillna(0.0)

    # omzetting van R naar euro-achtige lijn kan later preciezer
    # voorlopig 1R = 1 eenheid in performancegrafiek, maar visueel wel bruikbaar
    # als exit/entry en position size later beschikbaar zijn kan dat exact in € worden gemaakt
    if "outcome" not in df.columns:
        df["outcome"] = df["pnl"].apply(lambda x: "WIN" if x > 0 else "LOSS" if x < 0 else "FLAT")

    df["trade_type"] = "REAL"
    df["side"] = df["outcome"].apply(lambda x: "buy" if safe_str(x).upper() == "WIN" else "sell")
    df["qty"] = 1.0
    df["entry_price"] = pd.to_numeric(df.get("entry"), errors="coerce").fillna(0.0)
    df["exit_price"] = pd.to_numeric(df.get("exit_price"), errors="coerce").fillna(0.0)
    return df


def load_shadow_trades_db() -> pd.DataFrame:
    if table_exists("experience_trades"):
        cols = set(get_table_columns("experience_trades"))
        wanted = [
            "symbol", "setup_type", "timeframe", "regime", "label",
            "score", "raw_score", "chance", "confidence",
            "entry", "stop", "target", "exit", "exit_price",
            "result_r", "outcome", "is_shadow",
            "created_at"
        ]
        selected = [c for c in wanted if c in cols]
        if selected:
            where_shadow = "AND COALESCE(is_shadow, false) = true" if "is_shadow" in cols else ""
            sql = f"""
                SELECT {", ".join(selected)}
                FROM public.experience_trades
                WHERE 1=1
                {where_shadow}
                ORDER BY created_at DESC NULLS LAST
                LIMIT 500
            """
            df = read_sql_df(sql)
            if not df.empty:
                if "exit_price" not in df.columns and "exit" in df.columns:
                    df["exit_price"] = df["exit"]
                if "entry" not in df.columns:
                    df["entry"] = 0.0

                df["datetime_raw"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
                df["datetime"] = df["datetime_raw"].apply(format_dt_short)
                df["pnl"] = pd.to_numeric(df.get("result_r"), errors="coerce").fillna(0.0)

                if "outcome" not in df.columns:
                    df["outcome"] = df["pnl"].apply(lambda x: "WIN" if x > 0 else "LOSS" if x < 0 else "FLAT")

                df["trade_type"] = "SHADOW"
                df["entry_price"] = pd.to_numeric(df.get("entry"), errors="coerce").fillna(0.0)
                df["exit_price"] = pd.to_numeric(df.get("exit_price"), errors="coerce").fillna(0.0)
                return df

    if table_exists("shadow_trades"):
        cols = get_table_columns("shadow_trades")
        sql = f"SELECT {', '.join(cols)} FROM public.shadow_trades ORDER BY created_at DESC NULLS LAST LIMIT 500"
        df = read_sql_df(sql)
        if not df.empty:
            df["datetime_raw"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
            df["datetime"] = df["datetime_raw"].apply(format_dt_short)
            df["pnl"] = pd.to_numeric(df.get("result_r"), errors="coerce").fillna(0.0)
            df["outcome"] = df.get("outcome", pd.Series(["FLAT"] * len(df)))
            df["trade_type"] = "SHADOW"
            df["entry_price"] = pd.to_numeric(df.get("entry"), errors="coerce").fillna(0.0)
            df["exit_price"] = pd.to_numeric(df.get("exit_price"), errors="coerce").fillna(0.0)
            return df

    return pd.DataFrame([])


def load_positions_db() -> pd.DataFrame:
    # Robuuste poging; als positie-tabel niet bestaat blijft dit leeg
    candidate_tables = [
        "open_positions",
        "positions",
        "open_trades",
        "live_positions",
        "paper_positions",
    ]

    for table_name in candidate_tables:
        if table_exists(table_name):
            cols = get_table_columns(table_name)
            if not cols:
                continue
            sql = f"SELECT {', '.join(cols)} FROM public.{table_name} ORDER BY created_at DESC NULLS LAST LIMIT 200"
            df = read_sql_df(sql)
            if not df.empty:
                return df

    return pd.DataFrame([])


def load_scoreboard_db() -> pd.DataFrame:
    if not table_exists("experience_scoreboard"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_scoreboard"))
    wanted = ["setup_type", "regime", "timeframe", "n", "avg_r", "win_rate", "avg_win_r", "avg_loss_r", "updated_at"]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_scoreboard
        ORDER BY n DESC NULLS LAST
        LIMIT 100
    """
    return read_sql_df(sql)


def build_activity_feed(
    orders_df: pd.DataFrame,
    real_df: pd.DataFrame,
    shadow_df: pd.DataFrame,
) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []

    if not orders_df.empty:
        for _, row in orders_df.head(5).iterrows():
            feed.append({
                "ts": parse_dt(row.get("created_at") or row.get("expires_at")),
                "title": f"PRE-BUY {safe_str(row.get('symbol'), '-')}",
                "sub": f"Chance {safe_int(row.get('chance'), 0)} | Score {safe_int(row.get('score'), 0)} | {safe_str(row.get('regime'), '-')}",
                "kind": "prebuy",
            })

    if not real_df.empty:
        for _, row in real_df.head(8).iterrows():
            pnl = safe_float(row.get("pnl"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} REAL",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | {format_pnl(pnl)}",
                "kind": "real",
            })

    if not shadow_df.empty:
        for _, row in shadow_df.head(8).iterrows():
            pnl = safe_float(row.get("pnl"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} SHADOW",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | Potential {format_pnl(pnl)}",
                "kind": "shadow",
            })

    def _sort_key(x):
        ts = x.get("ts")
        if ts is None or pd.isna(ts):
            return pd.Timestamp.min.tz_localize("UTC")
        return ts

    return sorted(feed, key=_sort_key, reverse=True)[:15]


def build_main_chart_df(real_df: pd.DataFrame, shadow_df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    if not real_df.empty:
        for _, row in real_df.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl"), 0.0)
            rows.append({
                "ts": ts,
                "real_profit": max(pnl, 0.0),
                "real_loss": min(pnl, 0.0),
                "shadow_profit": 0.0,
                "missed_good": 0.0,
                "missed_bad": 0.0,
            })

    if not shadow_df.empty:
        for _, row in shadow_df.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl"), 0.0)
            rows.append({
                "ts": ts,
                "real_profit": 0.0,
                "real_loss": 0.0,
                "shadow_profit": pnl,
                "missed_good": max(pnl, 0.0),
                "missed_bad": min(pnl, 0.0),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    if df.empty:
        return df

    for col in ["real_profit", "real_loss", "shadow_profit", "missed_good", "missed_bad"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).cumsum()

    return df


# ==========================================================
# UI STYLE
# ==========================================================
st.set_page_config(page_title="Crypto AI Terminal", layout="wide")

st.markdown("""
<style>
    .stApp {
        background: #0a0f18;
        color: #f8fafc;
    }
    .block-container {
        max-width: 1800px;
        padding-top: 1rem;
        padding-bottom: 2rem;
    }
    div[data-testid="stMetric"] {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 18px;
        padding: 10px 14px;
    }
    div[data-testid="stMetric"] label {
        color: #94a3b8 !important;
    }
    div[data-testid="stMetricValue"] {
        color: #ffffff !important;
    }
    button[role="tab"] {
        color: #e5e7eb !important;
        font-weight: 700 !important;
    }
    .terminal-box {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 22px;
        padding: 16px;
        height: 100%;
    }
    .section-title {
        font-size: 18px;
        font-weight: 800;
        color: #f8fafc;
        margin-bottom: 12px;
    }
    .subtle {
        color: #94a3b8;
        font-size: 13px;
    }
    .info-row {
        display: flex;
        justify-content: space-between;
        padding: 8px 0;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        font-size: 14px;
    }
    .info-left {
        color: #cbd5e1;
        font-weight: 600;
    }
    .info-right {
        color: #ffffff;
        font-weight: 700;
        text-align: right;
    }
    .legend-box {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 18px;
        padding: 12px 14px;
    }
    .legend-item {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 8px;
        color: #e5e7eb;
        font-size: 14px;
        font-weight: 600;
    }
    .dot {
        width: 14px;
        height: 14px;
        border-radius: 50%;
        display: inline-block;
    }
    .deal-row {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        border-bottom: 1px solid rgba(255,255,255,0.05);
        padding: 14px 4px;
    }
    .deal-left {
        width: 68%;
    }
    .deal-main {
        font-size: 20px;
        font-weight: 800;
        color: #f8fafc;
        line-height: 1.2;
    }
    .deal-sub {
        color: #cbd5e1;
        margin-top: 4px;
        font-size: 14px;
    }
    .deal-right {
        width: 32%;
        text-align: right;
    }
    .deal-dt {
        color: #94a3b8;
        font-size: 12px;
        margin-bottom: 8px;
    }
    .deal-pnl {
        font-size: 28px;
        font-weight: 800;
        line-height: 1;
    }
    .activity-item {
        border-bottom: 1px solid rgba(255,255,255,0.05);
        padding: 10px 0;
    }
    .activity-title {
        color: #f8fafc;
        font-weight: 700;
        font-size: 14px;
    }
    .activity-sub {
        color: #94a3b8;
        font-size: 13px;
        margin-top: 3px;
    }
    .small-box {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 16px;
        padding: 12px;
    }
</style>
""", unsafe_allow_html=True)


# ==========================================================
# DATA LOAD
# ==========================================================
try:
    _, refresh_state = maybe_auto_refresh_snapshot()
except Exception as e:
    refresh_state = f"auto-refresh faalde: {e}"

snapshot, snapshot_err = safe_read_json(SNAPSHOT_PATH)
portfolio_hist, portfolio_hist_err = safe_read_csv(PORTFOLIO_HISTORY_CSV)

positions_df = load_positions_db()
orders_df = load_pending_orders_db()
real_df = load_real_trades_db()
shadow_df = load_shadow_trades_db()
scoreboard_df = load_scoreboard_db()

feed = build_activity_feed(orders_df, real_df, shadow_df)
chart_df = build_main_chart_df(real_df, shadow_df)

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_profit = float(pd.to_numeric(real_df["pnl"], errors="coerce").fillna(0).sum()) if not real_df.empty else 0.0
shadow_profit = float(pd.to_numeric(shadow_df["pnl"], errors="coerce").fillna(0).sum()) if not shadow_df.empty else 0.0

today_pnl = 0.0
if not real_df.empty:
    tmp_real = real_df.copy()
    tmp_real["date_only"] = tmp_real["datetime_raw"].dt.date
    if not tmp_real["date_only"].isna().all():
        last_day = tmp_real["date_only"].dropna().max()
        today_pnl = float(pd.to_numeric(tmp_real.loc[tmp_real["date_only"] == last_day, "pnl"], errors="coerce").fillna(0).sum())

open_trades_count = len(positions_df) if not positions_df.empty else 0
pending_count = len(orders_df) if not orders_df.empty else 0
missed_count = len(shadow_df) if not shadow_df.empty else 0

missed_good_count = 0
missed_bad_count = 0
shadow_winrate = 0.0
if not shadow_df.empty:
    pnl_series_shadow = pd.to_numeric(shadow_df["pnl"], errors="coerce").fillna(0.0)
    missed_good_count = int((pnl_series_shadow > 0).sum())
    missed_bad_count = int((pnl_series_shadow < 0).sum())
    total_shadow_decisions = int((pnl_series_shadow != 0).sum())
    shadow_winrate = (missed_good_count / total_shadow_decisions * 100.0) if total_shadow_decisions else 0.0

real_winrate = 0.0
if not real_df.empty:
    pnl_series_real = pd.to_numeric(real_df["pnl"], errors="coerce").fillna(0.0)
    wins_real = int((pnl_series_real > 0).sum())
    valid_real = int((pnl_series_real != 0).sum())
    real_winrate = (wins_real / valid_real * 100.0) if valid_real else 0.0


# ==========================================================
# HEADER METRICS
# ==========================================================
st.markdown("## Crypto AI Terminal")
st.caption(f"Snapshot status: {refresh_state} | Data source: {'Postgres' if db_ready() else 'Geen DATABASE_URL'}")

top_metrics_1 = st.columns(4)
top_metrics_1[0].metric("Balance", format_money(eur_available))
top_metrics_1[1].metric("Crypto", format_money(crypto_assets_eur))
top_metrics_1[2].metric("Total", format_money(total_portfolio_eur))
top_metrics_1[3].metric("Profit", format_pnl(real_profit))

top_metrics_2 = st.columns(4)
top_metrics_2[0].metric("Open Trades", str(open_trades_count))
top_metrics_2[1].metric("Pending Orders", str(pending_count))
top_metrics_2[2].metric("Missed Trades", str(missed_count))
top_metrics_2[3].metric("Today PnL", format_pnl(today_pnl))

st.divider()


# ==========================================================
# TOP LAYOUT: SMALLER LEFT / BIGGER CENTER / SMALLER RIGHT
# ==========================================================
col_left, col_center, col_right = st.columns([0.8, 2.2, 0.8], gap="large")

with col_left:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Panel</div>', unsafe_allow_html=True)

    if orders_df.empty:
        st.markdown('<div class="subtle">Geen pending Pre-BUY gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        best = orders_df.iloc[0]

        st.markdown(
            f"""
            <div class="small-box" style="margin-bottom:12px;">
                <div style="font-size:24px;font-weight:800;color:#f8fafc;">{safe_str(best.get('symbol'), '-')}</div>
                <div style="margin-top:4px;color:#94a3b8;font-size:13px;">
                    {safe_str(best.get('setup_type'), '-')} • {safe_str(best.get('regime'), '-')}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        info_rows = [
            ("Chance", str(safe_int(best.get("chance"), 0))),
            ("Score", str(safe_int(best.get("score"), 0))),
            ("Confidence", str(safe_int(best.get("confidence"), 0))),
            ("Entry", f"{safe_float(best.get('entry'), 0.0):.6f}"),
            ("Stop", f"{safe_float(best.get('stop'), 0.0):.6f}"),
            ("Target", f"{safe_float(best.get('target'), 0.0):.6f}"),
            ("Timeframe", safe_str(best.get("timeframe"), "-")),
            ("Status", safe_str(best.get("status"), "PENDING")),
            ("Expires", format_dt_short(best.get("expires_at"))),
        ]

        for left_label, right_val in info_rows:
            st.markdown(
                f"""
                <div class="info-row">
                    <div class="info-left">{left_label}</div>
                    <div class="info-right">{right_val}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)

with col_center:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Performance Overview</div>', unsafe_allow_html=True)

    # groter zoals Bitvavo: brede grafiek, kleine legenda erboven
    lg1, lg2, lg3, lg4, lg5 = st.columns(5)
    lg1.markdown('<div class="legend-item"><span class="dot" style="background:#5aa2ff;"></span> Blauw = echte winst</div>', unsafe_allow_html=True)
    lg2.markdown('<div class="legend-item"><span class="dot" style="background:#ff5a5f;"></span> Rood = echte verlies</div>', unsafe_allow_html=True)
    lg3.markdown('<div class="legend-item"><span class="dot" style="background:#b0b7c3;"></span> Grijs = shadow winst</div>', unsafe_allow_html=True)
    lg4.markdown('<div class="legend-item"><span class="dot" style="background:#2ecc71;"></span> Groen = gemiste trade goed</div>', unsafe_allow_html=True)
    lg5.markdown('<div class="legend-item"><span class="dot" style="background:#ff8c42;"></span> Oranje = gemiste trade slecht</div>', unsafe_allow_html=True)

    if chart_df.empty:
        st.info("Nog niet genoeg data uit Postgres voor de 5-lijnen grafiek.")
    else:
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_profit"],
            mode="lines",
            name="Echte winst",
            line=dict(color="#5aa2ff", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_loss"],
            mode="lines",
            name="Echte verlies",
            line=dict(color="#ff5a5f", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["shadow_profit"],
            mode="lines",
            name="Mogelijke winst shadow trades",
            line=dict(color="#b0b7c3", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_good"],
            mode="lines",
            name="Gemiste trade was goed",
            line=dict(color="#2ecc71", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_bad"],
            mode="lines",
            name="Gemiste trade was slecht",
            line=dict(color="#ff8c42", width=4),
        ))

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0f172a",
            plot_bgcolor="#0f172a",
            font=dict(color="#f8fafc"),
            height=620,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
            xaxis=dict(title="Tijd", gridcolor="rgba(255,255,255,0.07)"),
            yaxis=dict(title="Resultaat", gridcolor="rgba(255,255,255,0.07)"),
        )
        st.plotly_chart(fig, use_container_width=True)

    perf1, perf2, perf3, perf4 = st.columns(4)
    perf1.metric("Real Profit", format_pnl(real_profit))
    perf2.metric("Shadow Profit", format_pnl(shadow_profit))
    perf3.metric("Real Winrate", f"{real_winrate:.1f}%")
    perf4.metric("Shadow Winrate", f"{shadow_winrate:.1f}%")

    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Activity</div>', unsafe_allow_html=True)

    if not feed:
        st.markdown('<div class="subtle">Nog geen activity gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        for item in feed:
            kind = item.get("kind", "")
            if kind == "real":
                color = "#5aa2ff"
            elif kind == "shadow":
                color = "#2ecc71"
            else:
                color = "#f59e0b"

            st.markdown(
                f"""
                <div class="activity-item">
                    <div class="activity-title" style="color:{color};">{safe_str(item.get('title'), '-')}</div>
                    <div class="activity-sub">{safe_str(item.get('sub'), '-')}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Health</div>', unsafe_allow_html=True)

    health_rows = [
        ("DATABASE_URL", "OK" if db_ready() else "MIST"),
        ("pending_approvals", "OK" if table_exists("pending_approvals") else "MIST"),
        ("experience_trades", "OK" if table_exists("experience_trades") else "MIST"),
        ("experience_scoreboard", "OK" if table_exists("experience_scoreboard") else "MIST"),
        ("snapshot file", "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST"),
    ]
    for left_label, right_val in health_rows:
        st.markdown(
            f"""
            <div class="info-row">
                <div class="info-left">{left_label}</div>
                <div class="info-right">{right_val}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)


st.divider()

tabs = st.tabs(["Positions", "Orders", "Deals", "Shadow Trades", "Performance", "Settings"])

with tabs[0]:
    st.subheader("Open Positions")
    if positions_df.empty:
        st.info("Geen open positions gevonden in Postgres. Dit betekent meestal dat open trades nog niet in een positie-tabel worden opgeslagen.")
    else:
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Orders / Pending Pre-BUY")
    if orders_df.empty:
        st.info("Geen pending orders gevonden in Postgres.")
    else:
        show = orders_df.copy()
        if "expires_at" in show.columns:
            show["expires_at"] = pd.to_datetime(show["expires_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")
        if "created_at" in show.columns:
            show["created_at"] = pd.to_datetime(show["created_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")
        st.dataframe(show, use_container_width=True, hide_index=True)

with tabs[2]:
    st.subheader("Deals / Echte Trades")
    if real_df.empty:
        st.info("Nog geen echte trades gevonden in Postgres.")
    else:
        for _, row in real_df.head(60).iterrows():
            sym = safe_str(row.get("symbol"), "-")
            outcome = safe_str(row.get("outcome"), "-").upper()
            entry_price = safe_float(row.get("entry_price"), 0.0)
            exit_price = safe_float(row.get("exit_price"), 0.0)
            pnl = safe_float(row.get("pnl"), 0.0)
            dt_txt = safe_str(row.get("datetime"), "-")
            sc = "#2ecc71" if outcome == "WIN" else "#ff5a5f" if outcome == "LOSS" else "#cbd5e1"
            pc = pnl_color(pnl)

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{sc};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pc};">{format_pnl(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[3]:
    st.subheader("Shadow Trades / Niet genomen trades")
    if shadow_df.empty:
        st.info("Nog geen shadow trades gevonden in Postgres.")
    else:
        for _, row in shadow_df.head(60).iterrows():
            sym = safe_str(row.get("symbol"), "-")
            outcome = safe_str(row.get("outcome"), "-").upper()
            entry_price = safe_float(row.get("entry_price"), 0.0)
            exit_price = safe_float(row.get("exit_price"), 0.0)
            pnl = safe_float(row.get("pnl"), 0.0)
            dt_txt = safe_str(row.get("datetime"), "-")
            oc = outcome_color(outcome)
            pc = pnl_color(pnl)

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{oc};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pc};">{format_pnl(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[4]:
    st.subheader("Performance")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Real Profit", format_pnl(real_profit))
    p2.metric("Shadow Profit", format_pnl(shadow_profit))
    p3.metric("Missed Wins", str(missed_good_count))
    p4.metric("Missed Losses", str(missed_bad_count))

    if scoreboard_df.empty:
        st.info("Geen experience_scoreboard data gevonden in Postgres.")
    else:
        st.markdown("#### Setup / Regime Scoreboard")
        st.dataframe(scoreboard_df, use_container_width=True, hide_index=True)

with tabs[5]:
    st.subheader("Settings / Controle")
    rows = [
        {"item": "DATABASE_URL", "status": "OK" if db_ready() else "MIST"},
        {"item": "pending_approvals", "status": "OK" if table_exists("pending_approvals") else "MIST"},
        {"item": "experience_trades", "status": "OK" if table_exists("experience_trades") else "MIST"},
        {"item": "experience_scoreboard", "status": "OK" if table_exists("experience_scoreboard") else "MIST"},
        {"item": "snapshot file", "status": "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST"},
        {"item": "portfolio_history.csv", "status": "OK" if file_meta(PORTFOLIO_HISTORY_CSV).get("exists") else "MIST"},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Snapshot preview")
    if snapshot_err:
        st.info(snapshot_err)
    else:
        st.json(snapshot)
