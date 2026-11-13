"""Regressietests voor de hoofdscan, zonder netwerk of database."""

from analysis import multi_coin_score as scanner


def test_bear_blokkade_geeft_nul_signalen(monkeypatch):
    """Een bewuste BEAR-blokkade is een geslaagde scan met nul signalen."""
    monkeypatch.setattr(scanner, "is_bot_active", lambda _conn: True)
    monkeypatch.setattr(scanner, "is_bot_paused", lambda _conn: False)
    monkeypatch.setattr(scanner, "is_trading_hours", lambda: True)
    monkeypatch.setattr(scanner, "get_daily_pnl", lambda _conn: 0)
    monkeypatch.setattr(scanner, "get_open_live_count", lambda _conn: 0)
    monkeypatch.setattr(scanner, "get_prebuy_count_today", lambda _conn: 0)
    monkeypatch.setattr(scanner, "get_btc_regime", lambda _conn: "BEAR")
    monkeypatch.setattr(scanner, "get_btc_sterkte", lambda _conn: 11)
    monkeypatch.setattr(scanner, "get_btc_trend_richting", lambda _conn: "STABIEL")
    monkeypatch.setattr(scanner, "BTC_SKIP_BEAR", True)
    monkeypatch.setattr(scanner, "_SESSIE", {})

    assert scanner.scan_universe(object(), {}) == 0
