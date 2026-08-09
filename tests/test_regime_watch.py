"""Tests voor de regime-watcher (consistentie-logica)."""
from research.regime_watch import _norm, regimes_eens


def test_norm_verschillende_vocabulaires():
    assert _norm("ROOD") == "DALING"
    assert _norm("STORM") == "DALING"
    assert _norm("BEAR") == "DALING"
    assert _norm("GROEN") == "STIJGING"
    assert _norm("BULL") == "STIJGING"
    assert _norm("RANGE") == "RANGE"
    assert _norm("") == "ONBEKEND"


def test_eens_wanneer_zelfde_richting():
    # 4h zegt RANGE, bot_state zegt RANGE, plan_u niet gezet -> eens
    assert regimes_eens(["RANGE", "RANGE", None]) is True
    # verschillende vocabulaires, zelfde richting -> eens
    assert regimes_eens(["BEAR", "ROOD", "STORM"]) is True


def test_niet_eens_bij_conflict():
    # het conflict dat Hein flagde: plan_u ROOD terwijl bot_state RANGE
    assert regimes_eens(["RANGE", "RANGE", "ROOD"]) is False
    assert regimes_eens(["GROEN", "ROOD"]) is False


def test_leeg_is_niet_eens():
    assert regimes_eens([None, None, None]) is False
    assert regimes_eens([]) is False
