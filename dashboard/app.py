import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import psycopg2
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
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))

# BELANGRIJK:
# om blank screen te voorkomen refreshen we snapshot NIET automatisch bij page load
AUTO_REFRESH_SNAPSHOT_ON_LOAD = (os.getenv("AUTO_REFRESH_SNAPSHOT_ON_LOAD", "0").strip() == "1")


# ==========================================================
# BASIC HELPERS
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


def safe_read_json(path: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"JSON leesfout ({path}): {e}"


def safe_read_csv(path: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"CSV niet gevonden: {path}"
        df = pd.read_csv(path)
        return df, None
    except Exception as e:
        return None, f"CSV leesfout ({path}): {e}"


def pnl_color(pnl: float, trade_type: str = "REAL") -> str:
    trade_type = safe_str(trade_type).upper()
    if trade_type == "SHADOW":
        if pnl > 0:
            return "#2ecc71"   # groen
        if pnl < 0:
            return "#ff8c42"   # oranje
        return "#b0b7c3"       # grijs
    else:
        if pnl > 0:
            return "#5aa2ff"   # blauw
        if pnl < 0:
            return "#ff5a5f"   # rood
        return "#b0b7c3"


def outcome_color(outcome: str, trade_type: str = "REAL") -> str:
    o = safe_str(outcome).upper()
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if o == "WIN":
            return "#2ecc71"
        if o == "LOSS":
            return "#ff8c42"
        return "#b0b7c3"

    if o == "WIN":
        return "#5aa2ff"
    if o == "LOSS":
        return "#ff5a5f"
    return "#b0b7c3"


def run_safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ==========================================================
# DB HELPERS
# ==========================================================
def db_ready() -> bool:
    return bool(DATABASE_URL)


def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=DB_CONNECT_TIMEOUT)


def table_exists(table_name: str, schema: str = "public") -> bool:
    if not db_ready():
        return False
    try:
        with get_db_conn() as conn:
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
                row = cur.fetchone()
                return bool(row[0]) if row else False
    except Exception:
        return False


def get_table_columns(table_name: str, schema: str = "public") -> List[str]:
    if not db_ready():
        return []
    try:
        with get_db_conn() as conn:
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
    if not db_ready():
        return pd.DataFrame([])
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame([])


def table_count(table_name: str, schema: str = "public") -> int:
    if not table_exists(table_name, schema):
        return 0
    df = read_sql_df(f"SELECT COUNT(*) AS n FROM {schema}.{table_name}")
    if df.empty or "n" not in df.columns:
        return 0
    return safe_int(df.iloc[0]["n"], 0)


# ==========================================================
# BITVAVO
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

    return snapshot


def maybe_auto_refresh_snapshot():
    meta = file_meta(SNAPSHOT_PATH)
    if not meta["exists"]:
        return None, "snapshot ontbreekt"

    mins = age_minutes(meta["modified_epoch"])
    if AUTO_REFRESH_SNAPSHOT_ON_LOAD and mins >= AUTO_REFRESH_MINUTES:
        try:
            snap = build_snapshot_with_eur_values()
            return snap, f"refreshed ({mins:.0f} min old)"
        except Exception as e:
            return None, f"refresh mislukt: {e}"

    return None, f"kept (age {mins:.0f} min)"


# ==========================================================
# POSTGRES LOADERS
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
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["score", "chance", "confidence", "entry", "stop", "target"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    return df


def load_real_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_trades"))
    wanted = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = false
        ORDER BY created_at DESC NULLS LAST
        LIMIT 500
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["entry", "stop", "target", "result_r", "score", "raw_score", "chance", "confidence"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["entry"] = pd.to_numeric(df.get("entry", 0.0), errors="coerce").fillna(0.0)
    df["result_r"] = pd.to_numeric(df.get("result_r", 0.0), errors="coerce").fillna(0.0)
    df["exit_price"] = pd.to_numeric(df.get("target", 0.0), errors="coerce").fillna(0.0)

    if "outcome" not in df.columns:
        df["outcome"] = df["result_r"].apply(lambda x: "WIN" if x > 0 else "LOSS" if x < 0 else "FLAT")
    else:
        df["outcome"] = df["outcome"].fillna("UNKNOWN").astype(str)

    df["datetime_raw"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
    df["datetime"] = df["datetime_raw"].apply(format_dt_short)
    df["trade_type"] = "REAL"
    df["entry_price"] = df["entry"]
    df["pnl"] = df["result_r"]
    return df


def load_shadow_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_trades"))
    wanted = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = true
        ORDER BY created_at DESC NULLS LAST
        LIMIT 500
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["entry", "stop", "target", "result_r", "score", "raw_score", "chance", "confidence"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["entry"] = pd.to_numeric(df.get("entry", 0.0), errors="coerce").fillna(0.0)
    df["result_r"] = pd.to_numeric(df.get("result_r", 0.0), errors="coerce").fillna(0.0)
    df["exit_price"] = pd.to_numeric(df.get("target", 0.0), errors="coerce").fillna(0.0)

    if "outcome" not in df.columns:
        df["outcome"] = df["result_r"].apply(lambda x: "WIN" if x > 0 else "LOSS" if x < 0 else "FLAT")
    else:
        df["outcome"] = df["outcome"].fillna("UNKNOWN").astype(str)

    df["datetime_raw"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
    df["datetime"] = df["datetime_raw"].apply(format_dt_short)
    df["trade_type"] = "SHADOW"
    df["entry_price"] = df["entry"]
    df["pnl"] = df["result_r"]
    return df


def load_positions_db() -> pd.DataFrame:
    for table_name in ["open_positions", "positions", "open_trades", "live_positions", "paper_positions"]:
        if table_exists(table_name):
            cols = get_table_columns(table_name)
            if cols:
                sql = f"SELECT {', '.join(cols)} FROM public.{table_name} LIMIT 200"
                df = read_sql_df(sql)
                if not df.empty:
                    return df
    return pd.DataFrame([])


def load_scoreboard_db() -> pd.DataFrame:
    if not table_exists("experience_scoreboard"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_scoreboard"))
    wanted = [
        "exchange", "timeframe", "setup_type", "regime",
        "n_total", "n_win", "n_loss", "winrate", "avg_r",
        "expectancy", "updated_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    order_col = "n_total" if "n_total" in selected else selected[0]
    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_scoreboard
        ORDER BY {order_col} DESC NULLS LAST
        LIMIT 100
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["n_total", "n_win", "n_loss", "winrate", "avg_r", "expectancy"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")

    return df


# ==========================================================
# BUILDERS
# ==========================================================
def build_activity_feed(orders_df: pd.DataFrame, real_df: pd.DataFrame, shadow_df: pd.DataFrame) -> List[Dict[str, Any]]:
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


def render_perf_card(title: str, value: str, color: str):
    st.markdown(
        f"""
        <div style="
            background:#111827;
            border:1px solid #1f2937;
            border-left:6px solid {color};
            border-radius:18px;
            padding:18px 18px 14px 18px;
            min-height:110px;
        ">
            <div style="color:#94a3b8;font-size:14px;font-weight:600;margin-bottom:10px;">{title}</div>
            <div style="color:{color};font-size:28px;font-weight:800;line-height:1.1;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_card(title: str, value: str, ok: bool = True):
    color = "#2ecc71" if ok else "#ff5a5f"
    st.markdown(
        f"""
        <div style="
            background:#0f172a;
            border:1px solid #1e293b;
            border-left:6px solid {color};
            border-radius:16px;
            padding:14px 16px;
            margin-bottom:10px;
        ">
            <div style="color:#94a3b8;font-size:13px;font-weight:600;">{title}</div>
            <div style="color:{color};font-size:20px;font-weight:800;margin-top:6px;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ==========================================================
# PAGE CONFIG + CSS
# ==========================================================
st.set_page_config(page_title="Crypto AI Terminal", layout="wide")

st.markdown("""
<style>
    .stApp { background:#0a0f18; color:#f8fafc; }
    .block-container { max-width:1800px; padding-top:1rem; padding-bottom:2rem; }
    div[data-testid="stMetric"] {
        background:#111827;
        border:1px solid #1f2937;
        border-radius:18px;
        padding:10px 14px;
    }
    div[data-testid="stMetric"] label { color:#94a3b8 !important; }
    div[data-testid="stMetricValue"] { color:#ffffff !important; }
    button[role="tab"] { color:#e5e7eb !important; font-weight:700 !important; }
    .terminal-box {
        background:#0f172a;
        border:1px solid #1e293b;
        border-radius:22px;
        padding:16px;
        height:100%;
        box-shadow:0 10px 30px rgba(0,0,0,0.25);
    }
    .section-title {
        font-size:20px;
        font-weight:800;
        color:#f8fafc;
        margin-bottom:14px;
    }
    .subtle { color:#94a3b8; font-size:13px; }
    .info-row {
        display:flex;
        justify-content:space-between;
        padding:8px 0;
        border-bottom:1px solid rgba(255,255,255,0.06);
        font-size:14px;
    }
    .info-left { color:#cbd5e1; font-weight:600; }
    .info-right { color:#ffffff; font-weight:700; text-align:right; }
    .legend-item {
        display:flex; align-items:center; gap:10px;
        color:#e5e7eb; font-size:13px; font-weight:600;
    }
    .dot {
        width:14px; height:14px; border-radius:50%; display:inline-block;
    }
    .deal-row {
        display:flex; justify-content:space-between; align-items:flex-start;
        border-bottom:1px solid rgba(255,255,255,0.05); padding:14px 4px;
    }
    .deal-left { width:68%; }
    .deal-main { font-size:20px; font-weight:800; color:#f8fafc; line-height:1.2; }
    .deal-sub { color:#cbd5e1; margin-top:4px; font-size:14px; }
    .deal-right { width:32%; text-align:right; }
    .deal-dt { color:#94a3b8; font-size:12px; margin-bottom:8px; }
    .deal-pnl { font-size:28px; font-weight:800; line-height:1; }
    .activity-item {
        border-bottom:1px solid rgba(255,255,255,0.05);
        padding:10px 0;
    }
    .activity-title { color:#f8fafc; font-weight:700; font-size:14px; }
    .activity-sub { color:#94a3b8; font-size:13px; margin-top:3px; }
</style>
""", unsafe_allow_html=True)


# ==========================================================
# UI FIRST
# ==========================================================
st.markdown("## Crypto AI Terminal")
status_placeholder = st.empty()
loading_box = st.empty()

status_placeholder.caption("Dashboard wordt veilig geladen...")

with loading_box.container():
    st.info("Bezig met laden van snapshot, Postgres en grafieken...")


# ==========================================================
# SAFE DATA LOAD
# ==========================================================
refresh_state = "unknown"

_ = run_safe(maybe_auto_refresh_snapshot, (None, "snapshot check skipped"))
if _:
    _, refresh_state = _

snapshot, snapshot_err = run_safe(lambda: safe_read_json(SNAPSHOT_PATH), (None, "snapshot read failed"))

positions_df = run_safe(load_positions_db, pd.DataFrame([]))
orders_df = run_safe(load_pending_orders_db, pd.DataFrame([]))
real_df = run_safe(load_real_trades_db, pd.DataFrame([]))
shadow_df = run_safe(load_shadow_trades_db, pd.DataFrame([]))
scoreboard_df = run_safe(load_scoreboard_db, pd.DataFrame([]))

feed = run_safe(lambda: build_activity_feed(orders_df, real_df, shadow_df), [])
chart_df = run_safe(lambda: build_main_chart_df(real_df, shadow_df), pd.DataFrame([]))

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_profit = float(pd.to_numeric(real_df["pnl"], errors="coerce").fillna(0).sum()) if not real_df.empty and "pnl" in real_df.columns else 0.0
shadow_profit = float(pd.to_numeric(shadow_df["pnl"], errors="coerce").fillna(0).sum()) if not shadow_df.empty and "pnl" in shadow_df.columns else 0.0

today_pnl = 0.0
if not real_df.empty and "datetime_raw" in real_df.columns:
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
if not shadow_df.empty and "pnl" in shadow_df.columns:
    pnl_series_shadow = pd.to_numeric(shadow_df["pnl"], errors="coerce").fillna(0.0)
    missed_good_count = int((pnl_series_shadow > 0).sum())
    missed_bad_count = int((pnl_series_shadow < 0).sum())
    total_shadow_decisions = int((pnl_series_shadow != 0).sum())
    shadow_winrate = (missed_good_count / total_shadow_decisions * 100.0) if total_shadow_decisions else 0.0

real_winrate = 0.0
if not real_df.empty and "pnl" in real_df.columns:
    pnl_series_real = pd.to_numeric(real_df["pnl"], errors="coerce").fillna(0.0)
    wins_real = int((pnl_series_real > 0).sum())
    valid_real = int((pnl_series_real != 0).sum())
    real_winrate = (wins_real / valid_real * 100.0) if valid_real else 0.0


# klaar met laden
loading_box.empty()
status_placeholder.caption(f"Snapshot status: {refresh_state} | Data source: {'Postgres' if db_ready() else 'Geen DATABASE_URL'}")


# ==========================================================
# METRICS
# ==========================================================
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
# TOP LAYOUT
# ==========================================================
col_left, col_center, col_right = st.columns([0.9, 2.2, 0.9], gap="large")

with col_left:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Panel</div>', unsafe_allow_html=True)

    if orders_df.empty:
        st.markdown('<div class="subtle">Geen pending Pre-BUY gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        best = orders_df.iloc[0]
        st.markdown(
            f"""
            <div style="
                background:#111827;
                border:1px solid #1f2937;
                border-left:6px solid #5aa2ff;
                border-radius:18px;
                padding:16px;
                margin-bottom:12px;
            ">
                <div style="font-size:26px;font-weight:900;color:#f8fafc;">{safe_str(best.get('symbol'), '-')}</div>
                <div style="margin-top:6px;color:#94a3b8;font-size:13px;font-weight:600;">
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

    lg1, lg2, lg3, lg4, lg5 = st.columns(5)
    lg1.markdown('<div class="legend-item"><span class="dot" style="background:#5aa2ff;"></span> Blauw = echte winst</div>', unsafe_allow_html=True)
    lg2.markdown('<div class="legend-item"><span class="dot" style="background:#ff5a5f;"></span> Rood = echte verlies</div>', unsafe_allow_html=True)
    lg3.markdown('<div class="legend-item"><span class="dot" style="background:#b0b7c3;"></span> Grijs = shadow winst/flat</div>', unsafe_allow_html=True)
    lg4.markdown('<div class="legend-item"><span class="dot" style="background:#2ecc71;"></span> Groen = gemiste trade goed</div>', unsafe_allow_html=True)
    lg5.markdown('<div class="legend-item"><span class="dot" style="background:#ff8c42;"></span> Oranje = gemiste trade slecht</div>', unsafe_allow_html=True)

    if chart_df.empty:
        st.info("Nog niet genoeg data uit Postgres voor de 5-lijnen grafiek.")
    else:
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=chart_df["ts"], y=chart_df["real_profit"],
            mode="lines", name="Echte winst",
            line=dict(color="#5aa2ff", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"], y=chart_df["real_loss"],
            mode="lines", name="Echte verlies",
            line=dict(color="#ff5a5f", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"], y=chart_df["shadow_profit"],
            mode="lines", name="Shadow profit",
            line=dict(color="#b0b7c3", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"], y=chart_df["missed_good"],
            mode="lines", name="Gemiste trade goed",
            line=dict(color="#2ecc71", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"], y=chart_df["missed_bad"],
            mode="lines", name="Gemiste trade slecht",
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
            yaxis=dict(title="Resultaat", gridcolor="rgba(255,255,255,0.07)", zerolinecolor="rgba(255,255,255,0.15)"),
        )
        st.plotly_chart(fig, use_container_width=True)

    perf1, perf2, perf3, perf4 = st.columns(4)
    with perf1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with perf2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with perf3:
        render_perf_card("Real Winrate", f"{real_winrate:.1f}%", "#5aa2ff")
    with perf4:
        render_perf_card("Shadow Winrate", f"{shadow_winrate:.1f}%", "#2ecc71" if shadow_winrate >= 50 else "#ff8c42")

    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Activity</div>', unsafe_allow_html=True)

    if not feed:
        st.markdown('<div class="subtle">Nog geen activity gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        for item in feed:
            kind = item.get("kind", "")
            color = "#f59e0b"
            if kind == "real":
                color = "#5aa2ff"
            elif kind == "shadow":
                sub_txt = safe_str(item.get("sub"), "").upper()
                if "WIN" in sub_txt:
                    color = "#2ecc71"
                elif "LOSS" in sub_txt:
                    color = "#ff8c42"
                else:
                    color = "#b0b7c3"

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

    render_status_card("DATABASE_URL", "OK" if db_ready() else "MIST", db_ready())
    render_status_card("pending_approvals", str(table_count("pending_approvals")) if table_exists("pending_approvals") else "MIST", table_exists("pending_approvals"))
    render_status_card("experience_trades", str(table_count("experience_trades")) if table_exists("experience_trades") else "MIST", table_exists("experience_trades"))
    render_status_card("experience_scoreboard", str(table_count("experience_scoreboard")) if table_exists("experience_scoreboard") else "MIST", table_exists("experience_scoreboard"))
    render_status_card("snapshot file", "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST", file_meta(SNAPSHOT_PATH).get("exists"))

    st.markdown("</div>", unsafe_allow_html=True)

st.divider()


# ==========================================================
# TABS
# ==========================================================
tabs = st.tabs(["Positions", "Orders", "Deals", "Shadow Trades", "Performance", "Settings"])

with tabs[0]:
    st.subheader("Open Positions")
    if positions_df.empty:
        st.info("Geen open positions gevonden in Postgres.")
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

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{outcome_color(outcome, 'REAL')};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pnl_color(pnl, 'REAL')};">{format_pnl(pnl)}</div>
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

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{outcome_color(outcome, 'SHADOW')};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pnl_color(pnl, 'SHADOW')};">{format_pnl(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[4]:
    st.subheader("Performance")

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with p2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with p3:
        render_perf_card("Missed Wins", str(missed_good_count), "#2ecc71")
    with p4:
        render_perf_card("Missed Losses", str(missed_bad_count), "#ff8c42")

    st.markdown("<br>", unsafe_allow_html=True)

    if scoreboard_df.empty:
        st.info("Geen experience_scoreboard data gevonden in Postgres.")
    else:
        st.markdown("#### Setup / Regime Scoreboard")
        st.dataframe(scoreboard_df, use_container_width=True, hide_index=True)

with tabs[5]:
    st.subheader("Settings / Controle")
    rows = [
        {"item": "DATABASE_URL", "status": "OK" if db_ready() else "MIST"},
        {"item": "pending_approvals", "status": f"OK ({table_count('pending_approvals')})" if table_exists("pending_approvals") else "MIST"},
        {"item": "experience_trades", "status": f"OK ({table_count('experience_trades')})" if table_exists("experience_trades") else "MIST"},
        {"item": "experience_scoreboard", "status": f"OK ({table_count('experience_scoreboard')})" if table_exists("experience_scoreboard") else "MIST"},
        {"item": "snapshot file", "status": "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST"},
        {"item": "portfolio_history.csv", "status": "OK" if file_meta(PORTFOLIO_HISTORY_CSV).get("exists") else "MIST"},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Snapshot preview")
    if snapshot_err:
        st.info(snapshot_err)
    else:
        st.json(snapshot)
