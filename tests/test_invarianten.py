"""
Tests voor de kern-veiligheidsinvarianten (bot_invarianten.py).

Elke test pint een audit-bevinding (2026-06-13) vast zodat de fix blijft zitten.
Bewust dependency-vrij — draait in elke Python zonder DB/netwerk/SDK.
"""
import bot_invarianten as inv


# ── P0-5/P0-6: watchdog heartbeat ──────────────────────────────────────────

class TestBeoordeelHeartbeat:
    def test_verse_heartbeat_is_geen_probleem(self):
        probleem, ernst, _ = inv.beoordeel_heartbeat(sec_oud=30, hb_sec=3600)
        assert probleem is False
        assert ernst is None

    def test_null_laatste_run_is_kritiek_niet_vers(self):
        # P0-5: NULL laatste_run werd als "0s geleden / vers" gelezen.
        probleem, ernst, _ = inv.beoordeel_heartbeat(sec_oud=None, hb_sec=3600)
        assert probleem is True
        assert ernst == "KRITIEK"

    def test_null_heartbeat_sec_crasht_niet(self):
        # P0-6: None * 2 gooide een TypeError die de hele ronde brak.
        probleem, ernst, tolerantie = inv.beoordeel_heartbeat(sec_oud=30, hb_sec=None)
        assert probleem is False          # 30s < 2 * default(3600)
        assert tolerantie == 7200

    def test_te_oud_is_hoog(self):
        probleem, ernst, _ = inv.beoordeel_heartbeat(sec_oud=8000, hb_sec=3600)
        assert probleem is True
        assert ernst == "HOOG"

    def test_veel_te_oud_is_kritiek(self):
        # > 3x tolerantie (3x7200 = 21600) → KRITIEK
        probleem, ernst, _ = inv.beoordeel_heartbeat(sec_oud=22000, hb_sec=3600)
        assert probleem is True
        assert ernst == "KRITIEK"

    def test_nul_heartbeat_sec_valt_terug_op_default(self):
        _, _, tolerantie = inv.beoordeel_heartbeat(sec_oud=10, hb_sec=0)
        assert tolerantie == 7200


# ── P0-2 + P1-5: live-handel-gate ───────────────────────────────────────────

class TestBepaalLiveToegestaan:
    def test_alles_ok_is_live(self):
        assert inv.bepaal_live_toegestaan(True, True, True, "STABLE") is True

    def test_live_ok_false_blokkeert(self):
        # P0-2: scanner-hardlimieten (live_ok) werden genegeerd in de gate.
        assert inv.bepaal_live_toegestaan(True, True, False, "STABLE") is False

    def test_zombie_blokkeert_ook_als_rest_ok(self):
        # P1-5: ZOMBIE werd gelogd maar niet uit live gehaald.
        assert inv.bepaal_live_toegestaan(True, True, True, "ZOMBIE") is False

    def test_score_of_stop_niet_ok_blokkeert(self):
        assert inv.bepaal_live_toegestaan(False, True, True, "STABLE") is False
        assert inv.bepaal_live_toegestaan(True, False, True, "STABLE") is False

    def test_star_coin_mag_live(self):
        assert inv.bepaal_live_toegestaan(True, True, True, "STAR") is True


# ── P0-3: echte-order-schakelaar ────────────────────────────────────────────

class TestMagEchteOrderPlaatsen:
    def test_expliciet_aan(self):
        assert inv.mag_echte_order_plaatsen(True) is True

    def test_fail_safe_default(self):
        # Alles wat niet ondubbelzinnig True is, blokkeert echte orders.
        for waarde in (None, False, "", "false", "true", 0, 1):
            assert inv.mag_echte_order_plaatsen(waarde) is False
