#!/usr/bin/env python3
"""
Obsidian Export — Exporteert brain_knowledge naar Obsidian vault.
================================================================
Maakt markdown bestanden met [[links]] zodat Obsidian's graph view
het volledige brein visualiseert.

Structuur:
  vault/
    README.md                    ← overzicht
    coins/AVAXUSDT.md           ← per coin
    setups/TREND_PULLBACK.md    ← per setup
    regimes/RANGE.md            ← per regime
    scores/85-89.md             ← per score range
    exit/efficiency.md          ← exit analyse
    decisions/20260412_1.md     ← brain beslissingen
    advice/daily_20260412.md    ← claude advies
    sources/LIVE.md             ← per trade source
    dashboard.md                ← links naar alles

Draait als script of cron. Overschrijft bestaande bestanden.
"""
import os, sys, json, psycopg2
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
VAULT_DIR = os.environ.get("OBSIDIAN_VAULT", os.path.join(PROJECT_ROOT, "obsidian_vault"))


def log(msg):
    print(f"[OBSIDIAN {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def write_file(path, content):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def topic_to_filename(topic):
    """coin/AVAXUSDT → coins/AVAXUSDT.md"""
    parts = topic.split("/", 1)
    if len(parts) == 2:
        category = parts[0] + "s" if not parts[0].endswith("s") else parts[0]
        name = parts[1].replace("/", "_").replace(" ", "_")
        return os.path.join(category, f"{name}.md")
    return f"{topic.replace('/', '_')}.md"


def topic_to_link(topic):
    """coin/AVAXUSDT → [[coins/AVAXUSDT]]"""
    parts = topic.split("/", 1)
    if len(parts) == 2:
        category = parts[0] + "s" if not parts[0].endswith("s") else parts[0]
        name = parts[1]
        return f"[[{category}/{name}]]"
    return f"[[{topic}]]"


def confidence_bar(conf):
    """Visuele confidence bar voor markdown."""
    filled = int(float(conf or 0) * 10)
    return "█" * filled + "░" * (10 - filled) + f" {float(conf or 0)*100:.0f}%"


def verdict_emoji(content):
    if "KOPEN" in content or "STERK" in content or "GOED" in content:
        return "🟢"
    if "VERMIJDEN" in content or "ZWAK" in content or "SLECHT" in content:
        return "🔴"
    return "🟡"


def run():
    log("=" * 60)
    log("Obsidian Export — Brain Knowledge → Vault")
    log(f"Vault: {VAULT_DIR}")
    log("=" * 60)

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    # Haal alle knowledge op
    cur.execute("""
        SELECT topic, category, content, confidence, evidence,
               win_rate, avg_r, total_pnl, links, metadata, updated_at::text
        FROM brain_knowledge ORDER BY category, topic
    """)
    rows = cur.fetchall()
    log(f"Topics: {len(rows)}")

    # Haal bot state op voor dashboard
    cur.execute("SELECT key, value FROM bot_state WHERE key IN ('bot_running','bot_active','btc_regime_huidig','position_size_eur','max_per_trade_eur','daily_stop_loss_eur','score_drempel_bull')")
    state = dict(cur.fetchall())

    # Haal live trade stats op
    cur.execute("""
        SELECT source, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               COUNT(*) FILTER(WHERE outcome='LOSS') as l,
               COUNT(*) FILTER(WHERE status='OPEN') as o
        FROM experience_trades WHERE source IN ('LIVE','SHADOW','REPLAY','SIM')
        GROUP BY source ORDER BY source
    """)
    source_stats = {r[0]: {"wins": r[1], "losses": r[2], "open": r[3]} for r in cur.fetchall()}

    # Groepeer per categorie
    by_category = {}
    for r in rows:
        cat = r[1]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(r)

    count = 0

    # Genereer bestanden per topic
    for r in rows:
        topic, cat, content, conf, evidence = r[0], r[1], r[2], r[3], r[4]
        wr, avg_r, pnl = r[5], r[6], r[7]
        links_arr = r[8] or []
        meta = r[9] if isinstance(r[9], dict) else (json.loads(r[9]) if r[9] else {})
        updated = r[10] or ""

        emoji = verdict_emoji(content)
        conf_bar = confidence_bar(conf)

        # Links naar gerelateerde topics
        link_section = ""
        if links_arr:
            link_section = "\n## Verbindingen\n\n"
            for link in links_arr:
                link_section += f"- {topic_to_link(link)}\n"

        # Metadata sectie
        meta_section = ""
        if meta:
            verdict = meta.get("verdict", "")
            mfe = meta.get("mfe", "")
            mae = meta.get("mae", "")
            gap = meta.get("gap", "")
            if verdict:
                meta_section += f"\n**Verdict:** {emoji} {verdict}\n"
            if mfe:
                meta_section += f"**MFE:** {mfe}R\n"
            if mae:
                meta_section += f"**MAE:** {mae}R\n"
            if gap:
                meta_section += f"**MFE-R Gap:** {gap}R\n"

        # Stats
        stats = ""
        if wr is not None:
            stats += f"| Win Rate | {wr}% |\n"
        if avg_r is not None:
            stats += f"| Avg R | {avg_r} |\n"
        if pnl is not None:
            stats += f"| PnL | EUR {pnl} |\n"
        if evidence:
            stats += f"| Trades | {evidence} |\n"

        stats_table = ""
        if stats:
            stats_table = f"\n## Statistieken\n\n| Metric | Waarde |\n|---|---|\n{stats}"

        md = f"""# {emoji} {topic}

> {content}

**Confidence:** {conf_bar}
**Laatste update:** {updated[:16]}
{meta_section}
{stats_table}
{link_section}
---
*Automatisch gegenereerd door Bot Brain*
"""
        filepath = os.path.join(VAULT_DIR, topic_to_filename(topic))
        write_file(filepath, md)
        count += 1

    # Dashboard / README
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    regime = state.get("btc_regime_huidig", "?")
    bot_active = state.get("bot_active", "false") == "true"

    # Top coins
    top_coins = sorted(
        [r for r in by_category.get("coin", []) if r[5] and float(r[5]) >= 55 and (r[4] or 0) >= 5],
        key=lambda x: float(x[5] or 0), reverse=True
    )[:10]

    bad_coins = sorted(
        [r for r in by_category.get("coin", []) if r[5] and float(r[5]) < 35 and (r[4] or 0) >= 5],
        key=lambda x: float(x[5] or 0)
    )[:10]

    top_section = "\n".join(
        f"- 🟢 {topic_to_link(r[0])} — {r[5]}% WR ({r[4]} trades)"
        for r in top_coins
    ) if top_coins else "Geen data"

    bad_section = "\n".join(
        f"- 🔴 {topic_to_link(r[0])} — {r[5]}% WR ({r[4]} trades)"
        for r in bad_coins
    ) if bad_coins else "Geen data"

    # Setups
    setup_section = "\n".join(
        f"- {verdict_emoji(r[2])} {topic_to_link(r[0])} — {r[5]}% WR ({r[4]} trades)"
        for r in by_category.get("setup", [])
    ) if "setup" in by_category else "Geen data"

    # Scores
    score_section = "\n".join(
        f"- {verdict_emoji(r[2])} {topic_to_link(r[0])} — {r[5]}% WR"
        for r in by_category.get("score", [])
    ) if "score" in by_category else "Geen data"

    # Sources
    source_section = ""
    for src, s in source_stats.items():
        wl = s["wins"] + s["losses"]
        wr = round(s["wins"] / wl * 100, 1) if wl > 0 else 0
        source_section += f"- {topic_to_link(f'source/{src}')} — {wr}% WR ({s['wins']}W/{s['losses']}L) | {s['open']} open\n"

    # Decisions
    decision_section = ""
    for r in by_category.get("decision", [])[:5]:
        decision_section += f"- {topic_to_link(r[0])} — {r[2][:60]}\n"
    if not decision_section:
        decision_section = "Nog geen beslissingen"

    readme = f"""# 🧠 Bot Brain — Trading Knowledge Base

> Laatste update: {now}

## Bot Status

| Setting | Waarde |
|---|---|
| Bot Actief | {"🟢 JA" if bot_active else "🔴 NEE"} |
| BTC Regime | {regime} |
| Trade Size | EUR {state.get('max_per_trade_eur', '?')} |
| Daily Stop | EUR {state.get('daily_stop_loss_eur', '?')} |
| Score Drempel | {state.get('score_drempel_bull', '?')} |

## 📊 Trade Bronnen

{source_section}

## 🟢 Top Coins (KOPEN)

{top_section}

## 🔴 Slechte Coins (VERMIJDEN)

{bad_section}

## ⚙️ Setup Analyse

{setup_section}

## 📈 Score Ranges

{score_section}

## 🧠 Recente Beslissingen

{decision_section}

## 📂 Categorieeen

| Categorie | Topics |
|---|---|
{chr(10).join(f"| {cat} | {len(items)} |" for cat, items in sorted(by_category.items()))}

---

### Hoe te gebruiken

1. Open deze vault in **Obsidian**
2. Ga naar **Graph View** (Ctrl+G)
3. Je ziet het complete brein met alle verbindingen
4. Klik op een node om de details te zien
5. Groene nodes = winstgevend, rode = verliesgevend

*Automatisch gegenereerd door `research/obsidian_export.py`*
"""

    write_file(os.path.join(VAULT_DIR, "README.md"), readme)

    # Obsidian config
    obsidian_config = os.path.join(VAULT_DIR, ".obsidian")
    ensure_dir(obsidian_config)
    write_file(os.path.join(obsidian_config, "app.json"), json.dumps({
        "theme": "obsidian",
        "baseFontSize": 15
    }))
    write_file(os.path.join(obsidian_config, "graph.json"), json.dumps({
        "collapse-filter": False,
        "search": "",
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": [
            {"query": "path:coins", "color": {"a": 1, "rgb": 1096020}},
            {"query": "path:setups", "color": {"a": 1, "rgb": 3381555}},
            {"query": "path:regimes", "color": {"a": 1, "rgb": 16098851}},
            {"query": "path:scores", "color": {"a": 1, "rgb": 8947848}},
            {"query": "path:decisions", "color": {"a": 1, "rgb": 3722547}},
        ],
        "collapse-display": False,
        "showArrow": True,
        "textFadeMultiplier": 0,
        "nodeSizeMultiplier": 1.5,
        "lineSizeMultiplier": 1,
        "collapse-forces": False,
        "centerStrength": 0.5,
        "repelStrength": 10,
        "linkStrength": 1,
        "linkDistance": 150,
    }))

    conn.close()

    log(f"\n{'=' * 60}")
    log(f"Export compleet: {count} bestanden naar {VAULT_DIR}")
    log(f"Open Obsidian → Open vault → {VAULT_DIR}")
    log(f"Druk Ctrl+G voor graph view")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    run()
