"""
bot_invarianten.py — pure beslis-logica (GEEN imports, GEEN side-effects).

Hier staan de kleine, zuivere functies die de kern-veiligheidsregels van de
bots vastleggen. Ze hebben bewust geen DB-/netwerk-/SDK-afhankelijkheden, zodat
ze snel en betrouwbaar te unit-testen zijn (zie tests/test_invarianten.py) en
zodat de zware bot-modules ze kunnen importeren zonder kringverwijzingen.

Achtergrond: meerdere audit-bevindingen (2026-06-13) kwamen erop neer dat een
beslissing wél berekend werd maar verkeerd of helemaal niet werd toegepast.
Door die beslissingen hier te isoleren, kan een test ze afdwingen.
"""

# ── Watchdog: heartbeat-beoordeling ────────────────────────────────────────
# Audit P0-5/P0-6: NULL laatste_run werd als "0s geleden / vers" gelezen en
# een NULL heartbeat_sec liet de hele watchdog-ronde crashen (None * 2).

def beoordeel_heartbeat(sec_oud, hb_sec, default_hb_sec=3600):
    """Bepaal of een service-heartbeat een probleem is.

    Args:
        sec_oud: seconden sinds laatste_run. None == laatste_run is NULL
                 (service heeft NOOIT een heartbeat geschreven).
        hb_sec:  verwacht heartbeat-interval in seconden. Mag None zijn.
        default_hb_sec: fallback-interval als hb_sec ontbreekt.

    Returns:
        (is_probleem: bool, ernst: str|None, tolerantie_sec: int)
        ernst is 'KRITIEK', 'HOOG' of None (geen probleem).
    """
    # NULL/0 heartbeat_sec mag de berekening nooit laten crashen.
    hb = hb_sec if (hb_sec and hb_sec > 0) else default_hb_sec
    tolerantie = hb * 2

    # NOOIT gedraaid (NULL laatste_run) is altijd kritiek — niet "vers".
    if sec_oud is None:
        return True, "KRITIEK", tolerantie

    if sec_oud > tolerantie:
        ernst = "KRITIEK" if sec_oud > tolerantie * 3 else "HOOG"
        return True, ernst, tolerantie

    return False, None, tolerantie


# ── Scanner: live-handel-gate ──────────────────────────────────────────────
# Audit P0-2 + P1-5: live_toegestaan hing alleen op score+stopafstand. De
# scanner-hardlimieten (live_ok: max-open/daily-stop/pauze/trading-hours/
# weekend) en de ZOMBIE-classificatie werden NIET in de gate meegenomen.

def bepaal_live_toegestaan(score_ok, plan_g_stop_ok, live_ok, coin_cluster):
    """Eindbeslissing of een signaal LIVE verhandeld mag worden.

    Alle voorwaarden moeten waar zijn:
      - score_ok:        score binnen het toegestane venster (Plan G)
      - plan_g_stop_ok:  stopafstand binnen 2-5%
      - live_ok:         bot-brede hardlimieten staan handel toe
      - coin_cluster != 'ZOMBIE': geen als verliesgevend geclassificeerde coin
    """
    return bool(score_ok and plan_g_stop_ok and live_ok and coin_cluster != "ZOMBIE")


# ── Uitvoering: echte-order-veiligheidsschakelaar ──────────────────────────
# Audit P0-3: config.py PAPER_TRADING=True werd genegeerd; er was geen werkende
# schakelaar die echte Bitvavo-orders kon blokkeren.

def mag_echte_order_plaatsen(live_handel_aan):
    """True alleen als live-handel expliciet aan staat. Fail-safe: alles wat
    niet ondubbelzinnig waar is (None, '', 0, 'false') blokkeert echte orders."""
    return live_handel_aan is True
