# FIX-BACKLOG — interne crypto-bots

Samengevoegd + ontdubbeld uit de twee audits van 2026-06-13:
- `AUDIT_RAPPORT.md` (77 agents, 47 bevindingen — werkbots: scanner/uitvoering/monitor/sim/state)
- `INTERNE_TOEZICHT_RAPPORT.md` (102 agents, 59 bevindingen — toezichtlaag: wachters/watchdog/logboek)

**Prioritering hier is op ECHT risico, niet op de severity-labels van de audits:**
`P0` geld kwijt of stille storing nú mogelijk · `P1` kernlogica fout · `P2` meetdata onbetrouwbaar (sim) · `P3` toezichtlaag robuust maken · `P4` nieuwe wachters bouwen · `P5` hygiëne.

Status: `[ ]` open · `[~]` mee bezig · `[x]` klaar · `[-]` vervalt/niet doen.
Bron-codes: `A#` = AUDIT_RAPPORT severity-nummer, `T:X` = TOEZICHT cluster.

---

## P0 — GELD / STILLE STORING (eerst, meeste zijn kleine fixes)

- [ ] **P0-1 — `live_toegestaan` default-conflict (FALSE in DDL vs TRUE in schema-sync)** · `data/fix_experience_schema.py:328` vs `multi_coin_score.py:2383,2402` · *bron A-HOOG14*
  Als de kolom met DEFAULT TRUE ontstaat, kan een insert die de waarde niet zet → signalen die Plan-G NIET haalden tóch live verhandeld. **Direct geld-risico.** Fix: overal DEFAULT FALSE.

- [ ] **P0-2 — `live_ok` wordt nooit toegepast op `live_toegestaan`** · `analysis/multi_coin_score.py:2670-2704,2945` · *bron A-LAAG4 (onderschat)*
  max-open-trades / daily-stop / pauze / trading-hours / weekend worden berekend maar NIET in de gate gebruikt. Bot kan doorhandelen na daily-stop of boven MAX_OPEN. Fix: `live_toegestaan = live_ok and score_ok and plan_g_stop_ok and cluster!='ZOMBIE'`.

- [ ] **P0-3 — `config.py` is dode config; `PAPER_TRADING=True` wordt genegeerd** · `config.py` vs `live_trader.py:89-98` · *bron A-HOOG15*
  Je denkt paper/max-2/0.5%, maar bot handelt live €15/trade, tot 10 posities, 24/7. Fix: één bron + harde PAPER_TRADING-guard die `buy_eur()` blokkeert.

- [ ] **P0-4 — Geen kill-switch bij doorlopend verlies** · `live_trader.check_trading_limits` (logt alleen) · *bron A-voorstel "Kapitaal-Wachter", T:F*
  DAILY_STOP_LOSS_EUR en MAX_CONSECUTIVE_LOSSES worden bewust alleen gelogd ("bot stopt nooit automatisch"). Een kapotte strategie stapelt verlies tot jij het ziet. Fix: sticky `bot_paused=true` bij overschrijding (= P4 Kapitaal-Wachter, maar minimaal een harde stop in code).

- [ ] **P0-5 — Watchdog: NULL `laatste_run` telt als "0s geleden / vers"** · `process_watchdog.py:80,92,98-113` · *bron A-KRITIEK4, T:B*
  Exact de 13-dagen-bug: nooit-gedraaide/dode service lijkt kerngezond. Fix: NULL = KRITIEK (`COALESCE(...,999999999)` of expliciete None-check).

- [ ] **P0-6 — Watchdog: NULL `heartbeat_sec` crasht de hele ronde (TypeError)** · `process_watchdog.py:95,158-166` · *bron A-KRITIEK5, T:B*
  `None*2` → TypeError → gemaskeerd als "DB onbereikbaar" → geen enkele service meer gecheckt. Fix: `hb_sec = hb_sec or 3600` vóór de berekening.

- [ ] **P0-7 — Dubbele exchange-sell (3 oorzaken, samen één keten)** · *bron A-KRITIEK2/3 + A-HOOG1*
  (a) partial-/mode-guards niet gepersisteerd/teruggelezen → PARTIAL_40 herhaalt elke run (`trade_monitor.py:833,1203,1257-1281`);
  (b) NameError op `state` ná geslaagde Bitvavo-sell → gerapporteerd als mislukt → monitor verkoopt opnieuw (`live_trader.py:2092-2200`);
  (c) twee state-stores (monitor=DB, live_trader.sell=JSON) lopen uiteen na partial.
  Fix: één bron van waarheid (DB), persist alle guards, succes bepalen op order-resultaat.

- [ ] **P0-8 — `risk_gate` faalt OPEN (auto-GO bij Claude-fout / open circuit)** · `trading/risk_gate.py:126-128,168,191` · *bron A-MIDDEN3, T (poortwachter-bypass)*
  Bij API-uitval of circuit-open gaan ALLE trades door; default `go=True`. Fix: deterministische harde checks (R≥1.5, regime≠BEAR, F&G-grenzen) + default `go=False`.

- [ ] **P0-9 — Reconciliatie-pauze verloopt automatisch na 12u** · *bron T:C*
  Bot hervat LIVE-handel op niet-opgeloste positie-drift; werkbot heft zelf de pauze op. Mailtekst belooft "blijft staan tot jij ingrijpt" — klopt niet. Fix: sticky pauze voor KRITIEK, alleen jij/Jarvis heft op.

- [ ] **P0-10 — Stop-loss niet geëvalueerd bij prijs-fout (geen escalatie)** · `trade_monitor.py:1055-1058,1120-1125` · *bron A-HOOG3*
  Bij `get_price` None/≤0 wordt de SL-check stil overgeslagen; positie onbeschermd tijdens feed-storing, geen alert. Fix: N-fouten → WhatsApp-alert + fallback-bron / `price_stale`-markering. (Overlapt P4 Feed-Freshness-Wachter.)

---

## P1 — KERNLOGICA FOUT

- [ ] **P1-1 — `paper_trader.py` mist de volledige exit-engine** · `trading/paper_trader.py:1-335` · *A-KRITIEK1*
  Geen SL/target/weak-sell/100%-closure; paper-posities sluiten nooit. (Alleen relevant zodra paper-mode gebruikt wordt.)

- [ ] **P1-2 — `scan_universe` retourneert None in BEAR → TypeError `None>=2`** · `multi_coin_score.py:2719,3128,3150` · *A-HOOG4*
  Elke scan in BEAR crasht in de afrondingsfase; sessiestats + heartbeat niet bijgewerkt. Fix: `return 0`.

- [ ] **P1-3 — `experience_trades` mist `status`-kolom waar dashboard op filtert** · `dashboard/db.py` + `fix_experience_schema.py` · *A-HOOG13*
  Schrijvers gebruiken `outcome` én `status` door elkaar → lege/foute dashboard-queries, dubbele-trade-guard inconsistent. Fix: één statusbron kiezen.

- [ ] **P1-4 — `update_shadows.py` importeert niet-bestaande functies** · `scripts/update_shadows.py:12` · *A-HOOG14*
  ImportError elke run → shadows worden via dit script nooit bewaakt/gesloten. Fix: herschrijf op echte API of verwijder.

- [ ] **P1-5 — ZOMBIE-coin wordt gelogd als "shadow only" maar niet uit live gehaald** · `multi_coin_score.py:2828-2830,2945` · *A-MIDDEN5*
  Fix: bij ZOMBIE `live_toegestaan=False` forceren (valt samen met P0-2).

- [ ] **P1-6 — Whitelist-bonus (+5) duwt goede coins uit het 80-89 live-venster** · `multi_coin_score.py:2853-2854,2944` · *A-MIDDEN4*
  Fix: bonus vóór alle drempelchecks, of whitelist uitzonderen van bovengrens.

- [ ] **P1-7 — Daily-stop telt alleen gesloten trades; geen weekly-stop** · `multi_coin_score.py:2207-2220,2688-2692` · *A-MIDDEN6*
  Open verliezen tellen niet mee → daily-stop triggert te laat. Fix: unrealized PnL meenemen + weekly-stop.

- [ ] **P1-8 — Schema-gaten: `monitor_updated_at`, `buy_attempts` niet aangemaakt** · `dashboard/db.py:78,85,224` · *A-MIDDEN16*
  Dashboard-queries falen op verse DB. Fix: kolommen toevoegen aan schema-sync.

- [ ] **P1-9 — Weak-sell 65/35 (spec) ≠ code (40% partial)** · `trade_monitor.py:1203-1242` · *A-MIDDEN1*
  Gedocumenteerde 100%-closure bestaat niet. Fix: óf 65/35 implementeren, óf `strategy_rules.md` bijwerken naar werkelijk model.

---

## P2 — MEETDATA ONBETROUWBAAR (simulatoren — corrumpeert SIM→SHADOW→LIVE besluiten)

> Categorie van de death-by-fees bug: beslissingen op foute cijfers. Belangrijk vóór we de backtest-poort bouwen (#4).

- [ ] **P2-1 — Look-ahead: huidig BTC-regime op ALLE historische vensters** · `strategy_simulator.py:391,410-428` · *A-HOOG7*
- [ ] **P2-2 — Look-ahead: huidige F&G + BTC-koers in filter-eval van historie** · `strategy_simulator.py:391-392,465` · *A-HOOG8*
- [ ] **P2-3 — Equity/max-drawdown in coin-volgorde i.p.v. chronologisch** · `market_simulator.py:323-391` · *A-HOOG9*
- [ ] **P2-4 — `MAX_OPEN` genegeerd in sim → onbeperkte posities, PnL te hoog** · `market_simulator.py:30,116-193` · *A-HOOG10*
- [ ] **P2-5 — Fill tegen opgeslagen signaalprijs i.p.v. next-candle-open** · `market_simulator.py:121,132-133` + `signal_replay.py:84-95` · *A-HOOG11 + A-LAAG7*
- [ ] **P2-6 — Geen fees/slippage in plan_u_shadow / signal_replay / strategy_simulator** · `plan_u_shadow.py:148-151` e.a. · *A-MIDDEN12* — **directe verwant van death-by-fees**
- [ ] **P2-7 — WIN/LOSS-definitie inconsistent tussen simulators** · `market_simulator.py:203,220` (netto-PnL) vs target-hit elders · *A-MIDDEN11 + A-LAAG8*
- [ ] **P2-8 — Monte Carlo mengt ongelijke positiegroottes + negeert autocorrelatie** · `monte_carlo_forecast.py:48-101` · *A-HOOG12*
- [ ] **P2-9 — regime_labeler: slope/SMA over niet-uniform timestamp-raster** · `research/regime_labeler.py:363-388` · *A-MIDDEN13*

---

## P3 — TOEZICHTLAAG ROBUUST MAKEN (Fase 1+2 uit toezichtrapport)

> Kernconclusie audit: "de heartbeat verbergt stilstand" (6 varianten) én niemand bewaakt de wachters zelf.

**Fase 1 — kleine hoog-effect-fixes (na P0-5/P0-6):**
- [ ] **P3-1 — `health_update` krijgt `heartbeat_sec`-parameter + schema-DEFAULT** · *T:B (h1/h6)* — anders krijgt elke service NULL.
- [ ] **P3-2 — Watchdog schrijft eigen heartbeat alleen 'OK' bij geslaagde ronde** · `process_watchdog.py:158-166,220-233` · *A-MIDDEN7, T:B*
- [ ] **P3-3 — Werkbots (`live_trader`,`trade_monitor`) schrijven 'OK' alleen op succespad** · *T:B (h11/h14)* — nu blijft een gecrashte loop "levend".
- [ ] **P3-4 — Reconciliatie-vingerafdruk neemt OMVANG van drift mee** · `reconciliatie_wachter.py:247-263` · *A-HOOG6, T:G* — verergering nu 24u stil.

**Fase 2 — structureel:**
- [ ] **P3-5 — Alle wachters + `trade_monitor` schrijven heartbeat naar `bot_health`** · *T:A* — nu kan een dode wachter niet gezien worden.
- [ ] **P3-6 — `VERWACHTE_SERVICES`-allowlist in watchdog: ontbrekende service = KRITIEK** · *T:B (h4)* — nu alleen bestaande rijen zichtbaar.
- [ ] **P3-7 — Wachter-crons vastleggen in `render.yaml`/`scheduler.py`** · *T:A (k9/h21/h22)* — 9 services al gesuspend; bewaking hangt aan losse crons.
- [ ] **P3-8 — Auto-close in `trade_monitor.reconcilieer_trades` uitschakelen** · *T:E (h9/h18)* — sluit open LIVE-trades met `pnl_pct=0` → wist juist het bewijs dat de recon-wachter nodig heeft.
- [ ] **P3-9 — Data-Wachter kapotte checks repareren** · *T:G* — trigger-`enabled`-check, `bijgewerkt`-kolom wordt nooit geschreven, `=0` i.p.v. `IS NULL`.
- [ ] **P3-10 — WhatsApp-dedup: bericht uit `_te_sturen` opbouwen, timestamps gericht** · `process_watchdog.py:173-212` · *A-MIDDEN8* — nu kan een KRITIEK net-onderdrukt item helemaal niet verstuurd worden.
- [ ] **P3-11 — Data-Wachter: MEDIUM-bevindingen niet verliezen bij logboek-storing** · `data_wachter.py:48-68,141-150` · *A-MIDDEN9*
- [ ] **P3-12 — Reconciliatie: mislukte prijs-lookup ≠ "verwaarloosbaar"; guard `exp<=0`** · `reconciliatie_wachter.py:222-240` · *A-MIDDEN10*

---

## P4 — NIEUWE WACHTERS BOUWEN (volgorde uit toezichtrapport)

- [ ] **P4-1 — Wachter-van-de-Wachters (meta-heartbeat)** 🔴 · bewaakt of data_wachter/reconciliatie_wachter/trade_monitor/process_watchdog zélf nog draaien. Lost cluster A op.
- [ ] **P4-2 — Kapitaal-Wachter / kill-switch** 🔴 · cumulatieve+dagelijkse LIVE-PnL → sticky pauze. (= P0-4 volwaardig.)
- [ ] **P4-3 — Laag-3 Logboek-Lezer (Jarvis-brug)** 🟠 · leest `toezicht_logboek`, escaleert onverwerkte MEDIUM+, alarmeert bij "bevroren" logboek. Lost cluster D op.
- [ ] **P4-4 — Feed-Freshness / Stale-Price-Wachter** 🟠 · per open LIVE-positie: recente geldige prijs? (overlapt P0-10.)
- [ ] **P4-5 — Boekhouding-Drift-Wachter** 🟠 · paper/live/shadow/Plan-U-administraties tegen elkaar.
- [ ] **P4-6 — Order-Reconciliatie-Wachter** 🟡 · open/gedeeltelijke orders + fills bij Bitvavo (huidige recon = alleen eindsaldo).
- [ ] **P4-7 — Exposure-Wachter** 🟡 · som open LIVE-posities + concentratie per coin tegen plafond.
- [ ] **P4-8 — Config-Drift-Wachter** 🟡 · limiet-parameters consistent + binnen goedgekeurde grenzen.
- [ ] **P4-9 — API-Quota-/Rate-Limit-Wachter** 🟡 · Bitvavo rate-limit + Anthropic tokenverbruik.

---

## P5 — HYGIËNE / LATENTE BUGS

- [ ] **P5-1 — Taker-buy-ratio structureel 0.0 (Bitvavo levert veld niet)** · `multi_coin_score.py:933-935,1738-1943` · *A-HOOG5* — onterechte -3 malus op elke coin; VOLUME_ZONE_BOUNCE = dode code. Fix: 0.5 neutraal of feature verwijderen.
- [ ] **P5-2 — Funding-rate-bonus gebruikt ongedefinieerde `symbol` (dode code)** · `multi_coin_score.py:2014-2028` · *A-LAAG5*
- [ ] **P5-3 — `init_sessie` leunt op module-globale `drempels`** · `multi_coin_score.py:710-730` · *A-LAAG6*
- [ ] **P5-4 — Scheduler zonder overlap-/single-instance-bescherming** · `scripts/scheduler.py:8-35` · *A-MIDDEN15*
- [ ] **P5-5 — `db.py` interpoleert `source`/`days` in SQL (injectie-risico)** · `dashboard/db.py:141-171` · *A-LAAG9*
- [ ] **P5-6 — `account_snapshot.py` lekt key-lengtes + connectie-leak** · `account_snapshot.py:160-189` · *A-LAAG10*
- [ ] **P5-7 — `set_bot_val` race + wist hele cache** · `dashboard/db.py:198-216` · *A-LAAG11*
- [ ] **P5-8 — paper PnL negeert fees/slippage** · `paper_trader.py:298-299` · *A-LAAG1* (deels gedekt door P2-6)
- [ ] **P5-9 — `buy_eur` deelt door prijs zonder >0-guard** · `paper_trader.py:109-167` · *A-LAAG2*
- [ ] **P5-10 — monitor JSON-state wordt geschreven maar nooit teruggelezen** · `trade_monitor.py:1918-1932` · *A-LAAG3* (deels gedekt door P0-7c)

---

## Aanbevolen uitvoervolgorde

1. **P0 eerst** — grotendeels kleine fixes met direct geld-/storing-risico. Begin met P0-1, P0-2, P0-3, P0-5, P0-6 (samen <1 dag, hoogste hefboom).
2. **Per fix: eerst een test** (zie test-fundament-plan) die de invariant vastpint, dán de fix. Zo blijft het zitten.
3. **P3 Fase 1** parallel — herstelt dat de bestaande bewaking überhaupt wérkt.
4. **P2** vóór/tijdens het bouwen van de backtest-poort.
5. **P4 nieuwe wachters**: Wachter-van-Wachters + Kapitaal-Wachter eerst.
</content>
</invoke>
