# AUDITRAPPORT — INTERNE crypto-bots
Gegenereerd 2026-06-13 via multi-agent audit (77 agents). Scope: alleen interne bots (scanner, uitvoering, monitor, wachters, simulatie, state). Externe koppelingen (Bitvavo/Twilio/Claude) buiten beschouwing.

Totaal interne bevindingen: 47 (kritiek 5, hoog 15, midden 16, laag 11).

## Samenvatting per interne bot

### uitvoering
De feitelijke uitvoeringsmotor is gefragmenteerd en wijkt fundamenteel af van strategy_rules.md. paper_trader.py is GEEN execution engine: het bevat alleen buy_eur() en een naïeve sell(), maar GEEN enkele exit-logica (geen stop-loss, geen target, geen weak-sell, geen update_positions), geen closed/ws1_done/ws2_done guards, en importeert trade_logger niet. Alle echte exit-logica zit in trade_monitor.py + live_trader.py, en die operationele keten bevat meerdere kritieke fouten: (1) twee onderling INCONSISTENTE state-stores — trade_monitor leest posities uit de DB, live_trader.sell() leest/schrijft een JSON-bestand; (2) de double-sell guards (partial_sold_40, STRUCTUUR-mode, target_reached_notified, below_1r_count) leven alleen in-memory of in JSON en worden NOOIT teruggelezen door de monitor (load_state SELECT mist deze kolommen), dus na elke 30s-run/herstart resetten ze → herhaalde partial-sells en niet-deterministische exits; (3) bij een mislukte/onverwachte state-load nadat de Bitvavo-sell al is geplaatst rapporteert sell() ok:False, waarna de monitor opnieuw verkoopt → echte dubbele sell; (4) de WhatsApp-webhook roept een niet-bestaande functie 'buy' aan op paper_trader en unpackt de dict-return als tuple → paper-buys werken niet. De strategy_rules.md weak-sell 65/35 specificatie is nergens geïmplementeerd (de code doet 40% partial), dus de gedocumenteerde 100%-closure-garantie bestaat niet in code.

### scanner-risk
De scanner berekent indicatoren correct (Wilder RSI/ATR/SMA/Keltner kloppen wiskundig), maar er zitten meerdere ernstige fouten in de RISK-GATING en data-integriteit. Het zwaarste probleem: de berekende `live_ok` (die max-open-trades, daily-stop, pauze, trading-hours en weekend afdwingt) wordt NOOIT gebruikt bij het bepalen van `live_toegestaan` — die hangt alleen van score+stopafstand af. Daardoor worden pending-rijen met live_toegestaan=True weggeschreven terwijl de scanner-side hardlimieten juist LIVE zouden moeten blokkeren; afhankelijk van of live_trader opnieuw checkt is dit een omzeilbare max-open-trades / daily-stop. Daarnaast: `scan_universe` retourneert None in BEAR wat in main() een TypeError op `None >= 2` veroorzaakt; de taker-buy-ratio is altijd 0.0 (Bitvavo levert taker_buy_base niet) waardoor elke coin een -3 malus krijgt en VOLUME_ZONE_BOUNCE nooit vuurt; risk_gate faalt OPEN (auto-GO) bij Claude-fout/circuit-open; en de daily-trade-limiet is hard uitgeschakeld (if False). De scanner genereert geen SELL-signalen (conform strategy_rules), dat is in orde.

### wachters
De wachters zijn defensief opgezet (brede excepts, conn-cleanup, anti-spam), maar bevatten meerdere fouten die juist het kernrisico terugbrengen waarvoor ze zijn gebouwd: een heartbeat die stilstand verbergt. De ernstigste is dat process_watchdog een service die NOOIT een laatste_run schreef (NULL) als '0s geleden / vers' beoordeelt — exact het patroon van de eerdere 13-dagen-bug. Daarnaast crasht de hele watchdog-lus stil op een NULL heartbeat_sec, verfrist bot_health_helper de hartslag ook bij ERROR (zodat een vastlopende service 'levend' blijft), en schrijft de watchdog zijn eigen OK-hartslag zelfs nadat de DB-ronde faalde. De reconciliatie-wachter mist drift-escalatie omdat de vingerafdruk alleen het asset-symbool hasht, niet de omvang van de afwijking. De WhatsApp-dedup verstuurt altijd het volledige bericht met ALLE problemen ook als er maar één nieuw is, en markeert tegelijk onderdrukte services als 'verzonden' waardoor die dubbel onderdrukt worden.

### research-sim
De simulatie-laag bevat meerdere ernstige look-ahead biases en boekhoud-fouten die de gerapporteerde win-rates, PnL en drawdowns onbetrouwbaar maken. De zwaarste: strategy_simulator.py evalueert ALLE historische candle-vensters tegen het HUIDIGE BTC-regime, de HUIDIGE Fear&Greed-index en de HUIDIGE BTC-koers (klassieke look-ahead). market_simulator.py vult tegen de opgeslagen signaalprijs (niet de eerstvolgende open), negeert MAX_OPEN volledig, bouwt de equity-curve in coin-gegroepeerde i.p.v. chronologische volgorde (drawdown is fictie), en classificeert WIN/LOSS op netto-PnL waardoor target-hits met hoge kosten als LOSS tellen. monte_carlo_forecast.py mengt PnL uit bronnen met verschillende positiegroottes en resampelt onafhankelijk (negeert autocorrelatie/regime-clustering). signal_replay.py vult op stored entry i.p.v. open van eerste candle. Het regime-systeem zelf (build_btc_regime / regime_labeler) is grotendeels solide maar berekent slope op een niet-uniform timestamp-raster en heeft een silent-fallback die UNKNOWN als laatste regime kan vastzetten.

### state-config
Kritieke inconsistenties tussen schema, state en code: experience_trades mist een status-kolom waar het dashboard overal op filtert; update_shadows.py crasht op niet-bestaande imports; config.py is dode config terwijl de trader veel ruimere env-defaults gebruikt; live_trader_busy is geen echte lock; tegenstrijdige live_toegestaan-default; account_snapshot lekt key-lengtes en DB-connecties; niet-geparametriseerde SQL en ontbrekende kolommen in db.py.

## Bevestigde bevindingen

### KRITIEK (5)

**1. [uitvoering/logica] paper_trader.py mist de volledige exit-engine (geen SL/target/weak-sell/100%-closure)**  
- plek: `trading/paper_trader.py:1-335` (vertrouwen: hoog)  
- beschrijving: strategy_rules.md beschrijft paper_trader.py expliciet als de 'execution engine' met stop-loss (sectie 4), target (5), weak-sell 65/35 (6), guards closed/ws1_done/ws2_done (2,7) en een centrale update_positions(symbol, current_price) (10.3). Geen enkel van deze elementen bestaat in paper_trader.py. Het bestand bevat alleen buy_eur() en een generieke sell(symbol, fraction). Er is geen prijs-check, geen SL-trigger, geen target-trigger, geen weak-sell-niveau, geen closed-vlag, geen ws1_done/ws2_done, en geen import van trade_logger (sectie 10.1 verplicht 'from trade_logger import log_trade, log_event').  
- impact: Het bestand dat volgens de specificatie de discipline en 100%-closure moet garanderen, voert in werkelijkheid GEEN exits uit. Een paper-positie geopend via buy_eur() wordt door paper_trader nooit gesloten op SL of target; closure hangt volledig af van trade_monitor.py/live_trader.py die de paper-positie niet eens kennen (paper schrijft naar paper_state.json, monitor leest uit DB). Paper-trades blijven dus open hangen → geen audit-proof, geen risk discipline, restposities.  
- fix: Implementeer in paper_trader.py de centrale functie update_positions(symbol, current_price) conform strategy_rules.md sectie 4-7: hard stop (prijs<=stop_loss → 100% exit, closed=True), target (prijs>=target → 100% exit), weak-sell 1 (65%, ws1_done), weak-sell 2 (35%/remaining, ws2_done, closed), en WS2-zonder-WS1 force full exit. Voeg per trade de velden qty_total, qty_remaining, ws1_done, ws2_done, closed toe en importeer/gebruik trade_logger.log_trade/log_event.  

**2. [uitvoering/race-condition] Double-sell/niet-deterministische exit: partial- en mode-guards worden niet gepersisteerd en niet teruggelezen**  
- plek: `trade_monitor.py:833, 1257-1281, 1203, 1128, 1221-1242` (vertrouwen: hoog)  
- beschrijving: process_live_trade muteert in-memory vlaggen op het trade-dict: target_reached_notified (1129), mode='STRUCTUUR'+structuur_high (1130-1131), partial_sold_40 (1208), below_1r_count (1209,1232). De DB-persist (1257-1281) schrijft ALLEEN max_price_seen, min_price_seen, mfe_r, mae_r, had_over_1r, had_over_2r terug. De velden partial_sold_40, mode, structuur_high, target_reached_notified, below_1r_count worden NIET weggeschreven. Bovendien SELECT load_state() (regel 833) alleen had_over_1r/had_over_2r — niet partial_sold_40 en niet mode. Elke 30s-run (en elke herstart) bouwt het trade-dict opnieuw op zonder deze vlaggen.  
- impact: De guard 'not trade.get("partial_sold_40")' op regel 1203 is volgende run weer False → de bot voert de PARTIAL_40 sell (40%) telkens opnieuw uit zolang R<1 → herhaalde dubbele/driedubbele partial sells en uitholling van de positie. target_reached_notified/mode resetten ook, waardoor de STRUCTUUR-trailing-exit niet deterministisch is. below_1r_count reset → de '3 candles <1R'-exit komt nooit tot 3. Dit schendt sectie 7 (geen dubbele sells) en sectie 11 (deterministic, restart-safe).  
- fix: Voeg kolommen partial_sold_40, monitor_mode, structuur_high, target_reached_notified, below_1r_count, last_candle_check_ts toe aan experience_trades, schrijf ze mee in het UPDATE-blok (1262-1280) en lees ze mee in load_state() SELECT (833). Pas alternatief: maak deze vlaggen onderdeel van één enkele JSON/DB-state die zowel gelezen als geschreven wordt.  

**3. [uitvoering/bug] Echte dubbele exchange-sell: NameError op 'state' na geslaagde Bitvavo-sell wordt als mislukking gerapporteerd**  
- plek: `trading/live_trader.py:2092-2102, 2182-2200, 2043-2045` (vertrouwen: hoog)  
- beschrijving: In _sell_inner wordt 'state' alleen binnen de try op regel 2093 toegekend (state = load_state()); een exception daar wordt op 2101-2102 stil opgevangen (pass) en 'state' blijft ongebonden. De positie kan vervolgens via de DB-fallback (2105-2123) gevuld worden, waarna place_market_sell de echte order plaatst (2150). Daarna gebruikt regel 2184/2194 'state["positions"]' — bij ongebonden 'state' volgt een NameError, die door de buitenste try in sell() (2043) wordt gevangen en als {'ok': False, 'reason': ...} teruggegeven.  
- impact: De coin is dan WEL verkocht op Bitvavo, maar de functie meldt ok:False. trade_monitor._finalize_trade ziet ok:False, behandelt het als mislukte sell, verhoogt sell_pogingen en laat de trade als OPEN in de DB staan → bij de volgende run verkoopt de monitor opnieuw → daadwerkelijke dubbele sell op de exchange (verkoopt restbalans/verkeerde qty) en foutieve PnL/outcome. Schending sectie 7 (geen dubbele sells).  
- fix: Initialiseer state veilig: 'state = {"positions": {}, "open_trades": []}' vóór de try, en omring de state-mutatie op 2182-2200 met een guard. Belangrijker: bepaal succes op basis van het ordder-resultaat (ok van place_market_sell) en sluit de trade in de DB ook af als de lokale state-bewerking faalt; gooi geen exception na een geslaagde exchange-order.  

**4. [wachters/bug] NULL laatste_run wordt als verse heartbeat (0s geleden) gelezen — stilstand verborgen**  
- plek: `/Users/hein/dev/crypto_ai/process_watchdog.py:80, 92, 98-113` (vertrouwen: hoog)  
- beschrijving: De query berekent seconden_geleden = EXTRACT(EPOCH FROM (NOW() - laatste_run)). Als laatste_run NULL is (service heeft NOOIT een heartbeat geschreven, of de rij is net aangemaakt zonder laatste_run) levert dit SQL NULL op. Op regel 92 wordt dit gemaakt tot sec_oud = int(sec_oud or 0) = 0. De heartbeat-check op regel 106 (sec_oud > tolerantie) is dan 0 > tolerantie = False, en het tijdlabel toont '0s geleden'. Een service die helemaal nooit draait wordt dus als kerngezond gerapporteerd.  
- impact: Dit is exact het patroon van de eerdere bug waarbij de heartbeat 13 dagen geen-update verborg. Een dode of nooit-gestarte service (cron stuk, deploy mislukt, rij handmatig aangemaakt) wordt nooit gealarmeerd; de bot kan dagen stilstaan zonder WhatsApp.  
- fix: Behandel NULL laatste_run expliciet als KRITIEK i.p.v. als 0. Bijv. in Python: if laatste_run is None: probleem 'NOOIT GEDRAAID' toevoegen en continue. Of in SQL: COALESCE(EXTRACT(EPOCH FROM (NOW()-laatste_run)), 999999999) zodat NULL als oneindig oud telt. Combineer met een sanity-check dat sec_oud None-veilig is.  

**5. [wachters/bug] NULL heartbeat_sec laat de hele watchdog-lus stil crashen (TypeError)**  
- plek: `/Users/hein/dev/crypto_ai/process_watchdog.py:95, 158-166` (vertrouwen: hoog)  
- beschrijving: Regel 95: tolerantie = hb_sec * 2. Als hb_sec (kolom heartbeat_sec) NULL is — wat elders in de codebase blijkbaar voorkomt, zie dashboard/page_health.py:23 en dashboard/page_overview.py:134 die expliciet 'heartbeat_sec or 60' gebruiken — dan is hb_sec None en gooit None * 2 een TypeError. Die wordt gevangen door de brede except op regel 158 die het wegschrijft als probleem 'DATABASE / DB ONBEREIKBAAR'. Alle per-service-checks erna worden overgeslagen.  
- impact: Eén service met een NULL heartbeat_sec breekt de complete watchdog-ronde: geen enkele service wordt meer gecontroleerd en de oorzaak wordt verkeerd gerapporteerd als 'DB onbereikbaar'. De echte stilstand-detectie valt volledig uit.  
- fix: Gebruik dezelfde guard als de rest van de codebase: hb_sec = hb_sec or 60 (of een veilige default per service) direct na het uitpakken op regel 88, voordat tolerantie wordt berekend.  

### HOOG (15)

**1. [uitvoering/data-integriteit] Twee inconsistente state-stores: monitor leest DB, live_trader.sell leest/schrijft JSON**  
- plek: `trade_monitor.py:827-855 vs trading/live_trader.py:895-924, 2092-2200` (vertrouwen: hoog)  
- beschrijving: trade_monitor.load_state() (regel 827-839) bouwt posities op uit de Postgres-tabel experience_trades (SELECT ... WHERE status='OPEN'). live_trader._sell_inner() (regel 2092-2200) leest echter de positie uit live_state.json via live_trader.load_state() (regel 895) en schrijft de geüpdatete/resterende positie via save_state() terug naar diezelfde JSON. De monitor leest die JSON nooit terug. De partial-sell vlag wordt door live_trader op de JSON-positie gezet (regel 2197-2198 partial_sold_40=True), maar de monitor werkt op het DB-record.  
- impact: Na een partial sell weet de monitor (DB) niet dat er al 40% verkocht is: de DB-qty blijft de volledige qty, terwijl op de exchange en in JSON nog maar 60% staat. Bij de volgende 100%-exit verkoopt place_market_sell qty*1.0 op basis van de oude DB-qty → te grote sell-order / errorCode / verkeerde PnL. Tegelijk lopen de twee stores blijvend uit elkaar; herstart leest DB en negeert wat in JSON gebeurde.  
- fix: Kies één bron van waarheid (DB). Laat live_trader._sell_inner() na een partial sell de resterende qty en de partial-vlag direct in experience_trades updaten (UPDATE qty=remaining, partial_sold_40=TRUE) i.p.v. (of naast) JSON, en laat trade_monitor.load_state() die kolommen meelezen. Verwijder de JSON-state of synchroniseer beide atomisch in één transactie.  

**2. [uitvoering/bug] WhatsApp-webhook roept niet-bestaande paper_trader.buy aan en unpackt dict als tuple**  
- plek: `whatsapp_webhook.py:819-825` (vertrouwen: hoog)  
- beschrijving: Regel 819: 'from trading.paper_trader import buy as paper_buy'. paper_trader.py definieert echter geen 'buy' — alleen 'buy_eur' (regel 122). Dit levert een ImportError die op regel 824-825 wordt opgevangen met 'paper_trader niet gevonden'. Zelfs als de naam gecorrigeerd zou worden: regel 821 doet 'ok, msg = paper_buy(...)' (tuple-unpack), maar buy_eur() retourneert een dict (regel 263-270), niet (ok, msg).  
- impact: In TRADER_MODE paper of auto worden goedgekeurde BUY's nooit als paper-trade uitgevoerd; de webhook valt stil terug op 'Geen trader beschikbaar'. Na een naam-fix zou de tuple-unpack van een dict de keys teruggeven ('ok','msg' als strings) en verkeerd gedrag/crashes veroorzaken.  
- fix: Wijzig de import naar 'from trading.paper_trader import buy_eur as paper_buy' en verwerk de dict-return: 'res = paper_buy(symbol, amount_eur=amount_eur, meta=meta); ok = res.get("ok"); msg = res.get("reason") or symbol'.  

**3. [uitvoering/risico] Stop-loss op valse prijs: get_price-fout valt terug op laatste prijs/0 zonder verkoop**  
- plek: `trade_monitor.py:1055-1058, 1120-1125` (vertrouwen: hoog)  
- beschrijving: process_live_trade haalt current = get_price(symbol, market). Bij None/<=0 wordt simpelweg 'return False, False' gedaan (skip). De stop-loss-check op 1120 (current <= stop) wordt dan deze run overgeslagen. Er is geen teller/escalatie voor herhaald falende prijs-ophaling.  
- impact: Als de prijsbron (Bitvavo/Binance) tijdelijk faalt of een coin gedelist/illiquide is, wordt de stop-loss niet geëvalueerd; een positie kan tijdens een crash onbeschermd blijven zolang de prijsfeed faalt. Geen alert. Schending sectie 4 (hard stop, altijd 100% exit).  
- fix: Bij N opeenvolgende prijs-fouten op een open positie een WhatsApp-alert sturen en/of een fallback-prijsbron proberen; overweeg een veiligheids-exit of expliciete markering 'price_stale' zodat de stop niet stilzwijgend genegeerd wordt.  

**4. [scanner-risk/bug] scan_universe retourneert None in BEAR -> TypeError op 'None >= 2' in main()**  
- plek: `analysis/multi_coin_score.py:2719, 3128, 3150` (vertrouwen: hoog)  
- beschrijving: Bij BTC BEAR + BTC_SKIP_BEAR doet de functie een bare `return` (regel 2719) i.p.v. `return 0`. In main() wordt `n = scan_universe(...)` toegekend (regel 3128) en daarna `if n >= 2 or _SESSIE[...]` geevalueerd (regel 3150). `None >= 2` gooit in Python 3 een TypeError.  
- impact: Elke scan-run tijdens een BEAR-regime crasht in de afrondingsfase: de TypeError wordt gevangen in `except Exception` van __main__ en gerapporteerd als KRITIEKE fout (WhatsApp-alarm), sla_sessie_op/sla_sessie_naar_tabel en heartbeat-update worden overgeslagen, exit code 1. Sessiestatistieken en scanner_last_run worden niet bijgewerkt, wat health-monitoring verstoort.  
- fix: Vervang de bare `return` op regel 2719 door `return 0`.  

**5. [scanner-risk/data-integriteit] Taker buy ratio is structureel 0.0 (Bitvavo levert taker_buy_base niet) -> verkeerde malus + setup vuurt nooit**  
- plek: `analysis/multi_coin_score.py:933-935, 1218-1235, 1738-1740, 1941-1943` (vertrouwen: hoog)  
- beschrijving: fetch_candles zet taker_buy_base en quote_volume hardcoded op 0.0 (regel 933-935) omdat Bitvavo deze velden niet teruggeeft. bereken_taker_buy_ratio sommeert dan taker_koop=0 met totaal_vol>0 en retourneert `round(0/totaal_vol,4)` = 0.0 (NIET de 0.5 neutrale fallback, want totaal_vol>0). Gevolg: taker_ratio is altijd 0.0.  
- impact: 1) In calculate_score triggert `taker_ratio < 0.40` (regel 1941) op ELKE coin een onterechte -3 score-malus, wat scores systematisch verlaagt en signalen kan wegfilteren. 2) De VOLUME_ZONE_BOUNCE-setup vereist `taker_ratio > 0.52` (regel 1740) en kan dus NOOIT vuren — die hele setup is dode code. De 'koopdruk'-feature is feitelijk een ruisbron.  
- fix: Detecteer of taker-data beschikbaar is; zo niet, retourneer 0.5 (neutraal) i.p.v. 0.0, bijv. `if taker_koop <= 0: return 0.5`. Of haal taker-data uit een bron die het wel levert, of verwijder de taker-malus/-setup zolang de data ontbreekt.  

**6. [wachters/data-integriteit] Reconciliatie-vingerafdruk hasht alleen asset-symbool, niet de omvang — drift-escalatie wordt 24u gemist**  
- plek: `/Users/hein/dev/crypto_ai/wachters/reconciliatie_wachter.py:247-249, 257-263, 272` (vertrouwen: hoog)  
- beschrijving: De vingerafdruk wordt berekend als sha256 van de gesorteerde asset-symbolen (a.split(':')[0]) — dus uitsluitend WELKE assets afwijken, niet HOEVEEL. nieuw_alarm = vingerafdruk != vorige_vinger. Als de afwijking op dezelfde asset escaleert van bijv. 2% naar 80% (of van een paar euro naar honderden euro's), blijft de vingerafdruk identiek, dus nieuw_alarm = False, en herinnering_nodig pas na 24u (regel 260).  
- impact: Een verergerende drift op een reeds-bekende asset triggert geen nieuw KRITIEK-alarm; Hein hoort het pas bij de 24-uurs-herinnering. De wachter mist daarmee precies de gevaarlijke escalatie die hij hoort te vangen.  
- fix: Neem de omvang mee in de vingerafdruk, bijv. per asset het afgeronde afwijkingspercentage of een bucket (5%/10%/25%/50%) in de hash opnemen, zodat een materiële verandering in de drift als nieuw alarm telt. Overweeg ook een absolute drempel-escalatie (bijv. >25%) die ALTIJD direct mailt ongeacht vingerafdruk.  

**7. [research-sim/logica] Look-ahead bias: huidig BTC-regime toegepast op ALLE historische backtest-vensters**  
- plek: `trading/strategy_simulator.py:391, 410-428` (vertrouwen: hoog)  
- beschrijving: regime,btc_k,btc_e = get_regime(conn) wordt EEN keer aan het begin opgehaald (de meest recente rij uit btc_regime_4h, ORDER BY open_time DESC LIMIT 1). In de backtest-loop (for i in range(55, len(c1_all)-1)) wordt elk historisch venster geevalueerd met fn(c1_ctx, c4_ctx, regime) — dus een candle van maanden geleden wordt beoordeeld met het regime van vandaag. Alle strategieen filteren hard op regime (bv. 'if ...or regime=="BEAR": return None').  
- impact: Elke historische trade 'weet' in welk regime de markt vandaag zit. In een BULL-markt worden alle verleden BEAR-perioden alsnog getraded (en vice versa). De gerapporteerde win-rate en promotiedrempels (SIM->SHADOW->LIVE) zijn gebaseerd op onmogelijke informatie; een strategie kan ten onrechte gepromoot worden naar LIVE_PENDING.  
- fix: Bepaal het regime per backtest-tijdstip: lees btc_regime_4h-rij met open_time <= c1_all[i].ts_ms (of herbereken regime uit BTC-candles tot index i). Geef dat point-in-time regime mee aan de strategie-functies i.p.v. het globale 'regime'.  

**8. [research-sim/logica] Look-ahead bias: huidige Fear&Greed en huidige BTC-koers in filter-evaluatie van historische trades**  
- plek: `trading/strategy_simulator.py:391-392, 465, 181-199` (vertrouwen: hoog)  
- beschrijving: fng=get_fng() haalt de F&G-index van NU op (api.alternative.me limit=1). btc_k/btc_e zijn de huidige BTC-koers en EMA200. Beide gaan in eval_filters(sig,regime,btc_k,btc_e,fng,c1_ctx) voor IEDER historisch venster. F_FEAR_GREED, F_BTC_MA200 en F_EU_US_SESSION (now_utc().hour, regel 195) zijn dus allemaal gebaseerd op het heden, niet op het candle-tijdstip.  
- impact: De filter_flags die in experience_trades worden opgeslagen en later via filter_strategy_matrix worden geanalyseerd ('welke filter-combinatie werkt het best') zijn vervuild met toekomstdata. Conclusies als 'F&G>30 verhoogt WR' zijn statistisch waardeloos. EU_US_SESSION is voor alle backtest-rijen identiek (het uur van de cron-run).  
- fix: Bewaar F&G historisch per candle-tijd of laat de filter weg in backtest. Bereken BTC_MA200-flag uit de BTC-candle op tijdstip i. Bepaal EU_US_SESSION uit datetime van c1_nx['ts_ms'] i.p.v. now_utc().  

**9. [research-sim/logica] Equity-curve en max-drawdown berekend in coin-gegroepeerde i.p.v. chronologische volgorde**  
- plek: `research/market_simulator.py:323-361, 386-391` (vertrouwen: hoog)  
- beschrijving: De buitenste loop is 'for sym, sym_signals in signals_by_sym.items()': eerst worden ALLE trades van coin A verwerkt en aan equity[] toegevoegd, dan coin B, enz. capital wordt sequentieel opgeteld in die volgorde. De drawdown-loop (regel 388) itereert equity[] in exact die insertion-volgorde. equity wordt nergens op ts gesorteerd.  
- impact: De equity-curve en 'Max drawdown' weerspiegelen niet de werkelijke tijdlijn. Een verliesreeks van coin A in 2024 wordt na een winstreeks van coin Z in 2025 geplaatst. De gerapporteerde max_dd en het equity-grafiekje op het dashboard zijn betekenisloos en kunnen risico fors onderschatten.  
- fix: Verzamel alle closed trades, sorteer op open_ts (of close_ts), en bouw daarna pas de equity-curve en drawdown op in chronologische volgorde.  

**10. [research-sim/logica] MAX_OPEN wordt genegeerd: onbeperkt overlappende posities, capital/sizing fictief**  
- plek: `research/market_simulator.py:30, 116-193, 340-361` (vertrouwen: hoog)  
- beschrijving: MAX_OPEN (default 10) is gedefinieerd maar wordt nergens gebruikt. Elke trade krijgt POSITION_EUR (15 EUR) ongeacht beschikbaar capital of aantal reeds open posities. Trades overlappen vrij in de tijd; er is geen positie-boekhouding die concurrente posities of capital-allocatie afdwingt.  
- impact: De live bot heeft een max aantal posities en beperkt kapitaal; de sim doet alsof elke signaal-trade altijd vol kan worden ingenomen. Hierdoor zijn totale PnL en het aantal trades systematisch te hoog en niet representatief voor de echte bot. Promotie-/forecast-beslissingen op basis hiervan zijn misleidend.  
- fix: Houd open posities bij met entry/exit-tijd, weiger nieuwe entries als len(open)>=MAX_OPEN of als capital < POSITION_EUR, en reken positiegrootte op basis van actueel capital.  

**11. [research-sim/logica] Fill tegen opgeslagen signaalprijs i.p.v. eerstvolgende candle-open (onrealistische entry)**  
- plek: `research/market_simulator.py:121, 132-133` (vertrouwen: hoog)  
- beschrijving: entry = signal['entry'] * (1+SPREAD+SLIP). De candles worden gefilterd op c['ts_ms'] > open_ts, maar de fill-prijs is de in pending_approvals opgeslagen entry — een prijs die het signaal-algoritme koos op signaalmoment. Er wordt niet gecontroleerd of die prijs in de eerstvolgende candle daadwerkelijk verhandelbaar was (geen check of low<=entry<=high van de eerste future-candle).  
- impact: Als de markt na het signaal weg-gapte, wordt toch tegen de oude (gunstige) entry gevuld. Dit geeft een optimistische bias: trades die in werkelijkheid nooit op die prijs gevuld zouden zijn, tellen mee — vaak juist de trades die meteen de goede kant op gingen.  
- fix: Vul op de open van de eerste candle na het signaal (future[0]['open']) en pas daarop spread/slippage toe; of sla de entry over als de gewenste limietprijs niet binnen de eerste candle-range valt.  

**12. [research-sim/logica] Monte Carlo mengt PnL van bronnen met verschillende positiegroottes en resampelt onafhankelijk**  
- plek: `research/monte_carlo_forecast.py:48-60, 69, 98-101` (vertrouwen: hoog)  
- beschrijving: get_historical_trades pakt MARKETSIM (POSITION_EUR=15), REPLAY (amount_eur=kelly_grootte, vaak 5) en SHADOW door elkaar. run_monte_carlo trekt random.choice(pnls) — absolute EUR-bedragen uit ongelijke positiegroottes worden in een pot gegooid en onafhankelijk getrokken met teruglegging.  
- impact: De verdeling van getrokken PnL is een mix van incompatibele schalen; verwacht_pnl, percentielen en drawdown zijn niet interpreteerbaar als 'komende 100 trades a POSITION_EUR'. Onafhankelijk trekken negeert bovendien autocorrelatie/regime-clustering (verliesreeksen in BEAR), waardoor loss-streak- en drawdown-schattingen te gunstig zijn.  
- fix: Resample op R-multiples (result_r) en vermenigvuldig met een vaste positiegrootte/risico, OF normaliseer alle pnl_eur naar dezelfde positiegrootte. Overweeg block-bootstrap om streaks te behouden. Filter op een enkele bron of een homogene definitie.  

**13. [state-config/data-integriteit] experience_trades mist status-kolom maar dashboard filtert erop**  
- plek: `/Users/hein/dev/crypto_ai/dashboard/db.py:79 86 107 129 150 164 175 274` (vertrouwen: hoog)  
- beschrijving: db.py filtert experience_trades op status OPEN of CLOSED. fix_experience_schema.py EXPERIENCE_TRADES_COLUMNS bevat geen status-kolom (regel 314 status hoort bij PENDING_APPROVALS_COLUMNS). De schrijvers gebruiken outcome als statusveld: multi_coin_score.py 3014 (outcome=OPEN), live_trader.py 2376 (status=OPEN) en shadow log_shadow_to_db 723+ (alleen outcome) door elkaar.  
- impact: Bestaat de kolom niet dan falen alle dashboard-queries (lege DataFrame). Bestaat hij wel via handmatige psql, dan loopt status niet synchroon met outcome zodat win-rate, equity en open-posities leeg of fout zijn. live_trader checkt status=OPEN en multi_coin checkt outcome=OPEN voor de duplicate-guard, dus dubbele trades op dezelfde coin zijn mogelijk.  
- fix: Kies een statusbron: voeg status toe aan EXPERIENCE_TRADES_COLUMNS afgeleid van outcome, of vervang in db.py alle status-filters door outcome-semantiek (OPEN versus WIN LOSS GHOST). Maak duplicate-checks in live_trader en multi_coin identiek.  

**14. [state-config/bug] update_shadows.py importeert niet-bestaande load_shadows en save_shadows**  
- plek: `/Users/hein/dev/crypto_ai/scripts/update_shadows.py:12` (vertrouwen: hoog)  
- beschrijving: Doet from trading.shadow_trades import load_shadows, save_shadows. shadow_trades.py definieert alleen load_shadow_state (549) en save_shadow_state (619); load_shadows en save_shadows bestaan nergens in de repo. Het script verwacht bovendien een LIJST (range(len(shadows)), shadow.get(status)) terwijl shadow_trades.py een DICT met positions opslaat.  
- impact: ImportError direct bij start. Shadow-trades worden dus NOOIT bewaakt op stop-loss, target of expiry, sluiten nooit en het experience-scoreboard wordt niet opgebouwd. Een cron of scheduler die dit aanroept faalt elke run stil.  
- fix: Herschrijf op de echte API: load_shadow_state(), itereer over state positions, gebruik evaluate_shadow_exit, update_shadow_tracking en close_shadow_trade. Of verwijder dit verouderde script zodat er een exit-logica bestaat.  

**15. [state-config/config] config.py is dode config; trader gebruikt veel ruimere env-defaults**  
- plek: `/Users/hein/dev/crypto_ai/config.py:8 17 20 27 56` (vertrouwen: hoog)  
- beschrijving: config.py: MAX_OPEN_TRADES=2, MAX_RISK_PER_TRADE_PCT=0.5, DAILY_STOP_LOSS_PCT=-2.0, PAPER_TRADING=True. Alleen trade_logger.py importeert config. live_trader.py leest env-vars met afwijkende defaults: regel 89 MAX_PER_TRADE_EUR=15, 91 MAX_OPEN_REAL_TRADES=10, 94 DAILY_STOP_LOSS_EUR=5, 97-98 TRADING_HOURS 0 tot 24. PAPER_TRADING wordt genegeerd.  
- impact: De gebruiker denkt dat config stuurt (paper, max 2 trades, 0.5 procent), maar de bot handelt live met 15 euro per trade, tot 10 open posities, 24/7 en een daglimiet in euro in plaats van procent. PAPER_TRADING=True biedt geen bescherming, dus risico op ongewild echt-geld handelen; limieten 5 tot 30 keer ruimer.  
- fix: Maak config.py de enige bron of verwijder hem en documenteer de env-vars. Laat live_trader expliciet uit config lezen met een harde PAPER_TRADING-guard die buy_eur() blokkeert.  

### MIDDEN (16)

**1. [uitvoering/logica] Weak-sell 65/35 uit strategy_rules.md is niet geïmplementeerd; code gebruikt 40% partial**  
- plek: `trade_monitor.py:1203-1242` (vertrouwen: hoog)  
- beschrijving: strategy_rules.md sectie 6 schrijft WEAK_SELL_1=65% en WEAK_SELL_2=35% (samen exact 100%) met triggers op weak_sell_1_price/weak_sell_2_price en de WS2-zonder-WS1 force-exit-regel. De code implementeert in plaats daarvan een PARTIAL_40 (verkoop 0.40 bij terugval <1R, regel 1205) gevolgd door 'verkoop rest na 3 candles <1R' (regel 1239). Er zijn geen weak_sell_1_price/weak_sell_2_price niveaus, geen ws1_done/ws2_done vlaggen, en de WS2-zonder-WS1 force-exit ontbreekt.  
- impact: De gedocumenteerde, deterministische 100%-exit via 65/35 bestaat niet. Als de '3 candles <1R'-conditie nooit gehaald wordt (bv. prijs blijft net boven stop maar onder 1R, of below_1r_count reset elke run zie andere finding), blijft 60% van de positie open hangen → restpositie en afwijking van spec. Risk discipline wijkt af van wat als 'optimaal' gedocumenteerd staat.  
- fix: Implementeer expliciet weak_sell_1_price/weak_sell_2_price bij entry (in buy_eur/open) en de 65/35-exit met ws1_done/ws2_done guards en de WS2-zonder-WS1 force-full-exit conform sectie 6, of werk strategy_rules.md bij naar het feitelijke 40%/trailing-model zodat spec en code overeenkomen.  

**2. [uitvoering/config] trade_logger wordt niet gebruikt door de uitvoeringsmotor; audit-trail incompleet**  
- plek: `trade_logger.py:49-88 (en ontbrekende imports in trading/paper_trader.py)` (vertrouwen: hoog)  
- beschrijving: strategy_rules.md sectie 9-10 verplicht log_trade(action=ENTRY/WS1/WS2/STOPLOSS/TARGET/FULL_EXIT) en log_event(TRADE_CLOSED/SELL_EXECUTED). trade_logger.py biedt deze functies, maar paper_trader.py importeert ze niet (gebruikt eigen _log_row CSV met andere kolommen) en de live/monitor-keten logt via DB/eigen CSV's. De gestandaardiseerde audit-acties (WS1/WS2/FULL_EXIT) worden nergens via trade_logger geschreven.  
- impact: De in de spec beloofde 'audit-proof' en 'volledig reproduceerbare' trade-log (sectie 11) bestaat niet centraal; reconstructie van een trade vereist het samenvoegen van paper_trades.csv, live JSON, DB en bot_state. WS1/WS2/FULL_EXIT-acties zijn niet traceerbaar omdat ze niet geïmplementeerd zijn (zie weak-sell finding).  
- fix: Laat de execution engine (na implementatie van de exits) elke actie via trade_logger.log_trade/log_event schrijven met trade_id, symbol, price, qty, reason en pnl_pct, conform sectie 9.  

**3. [scanner-risk/risico] risk_gate faalt OPEN: Claude-fout en open circuit leveren auto-GO op**  
- plek: `trading/risk_gate.py:126-128, 168, 181-191` (vertrouwen: hoog)  
- beschrijving: Bij open circuit breaker geeft beoordeel_trade `{go: True}` terug (regel 127-128). Bij elke exception in de Claude-call wordt eveneens `{go: True, reden: 'Claude fout (auto-GO)'}` geretourneerd (regel 191). Bovendien is de JSON-parse-default `bool(result.get('go', True))` (regel 168) — ontbreekt 'go' in het antwoord, dan GO. De enige harde blokkade is Fear&Greed<20 + bearish nieuws (regel 115).  
- impact: De poortwachter laat bij API-uitval, parse-problemen of na 3 fouten (circuit open, 1u lang) alle trades door. De R-ratio<1.5-regel en regime/FOMO-checks staan alleen in de prompt en worden niet in code afgedwongen, dus bij Claude-uitval is er geen enkele risicofilter behalve het extreme F&G-geval. Dit ondermijnt het doel van de risk-gate juist op de momenten dat bescherming nodig is.  
- fix: Implementeer een script-mode met deterministische harde checks (R-ratio>=1.5, regime!=BEAR, F&G-grenzen) i.p.v. blind auto-GO. Default `result.get('go', False)` zodat ontbrekend veld NO-GO is. Overweeg fail-closed (NO-GO) bij circuit open, of minimaal een geclampte fallback met de bovenstaande regels.  

**4. [scanner-risk/logica] Whitelist-bonus (+5) na de score-check kan goede coins boven MAX_SCORE_TO_TRADE duwen en live blokkeren**  
- plek: `analysis/multi_coin_score.py:2853-2854, 2944` (vertrouwen: hoog)  
- beschrijving: De whitelist +5 bonus wordt toegepast op regel 2853-2854 (na update_sessie en na de shadow-drempelcheck) en capt op 100. live_toegestaan vereist `score <= MAX_SCORE_TO_TRADE` (=89, regel 2944). Een whitelisted coin met score 87 wordt 92 en valt daarmee buiten de 80-89 sweet spot, waardoor live_toegestaan False wordt.  
- impact: Juist de best-presterende (whitelisted) coins kunnen door de bonus uit het live-venster geduwd worden — tegengesteld aan de bedoeling van de whitelist. Inconsistent: de bonus helpt de shadow-drempel halen maar saboteert de live-bovengrens.  
- fix: Pas de whitelist-bonus toe vóór alle drempelchecks, of sluit whitelist-coins uit van de MAX_SCORE_TO_TRADE-bovengrens, of cap de bonus zodat score binnen het sweet-spot-venster blijft.  

**5. [scanner-risk/logica] ZOMBIE-coin wordt wel gelogd als 'shadow only' maar niet uit live gehaald**  
- plek: `analysis/multi_coin_score.py:2828-2830, 2945` (vertrouwen: hoog)  
- beschrijving: Bij een ZOMBIE-cluster (edge decay of WR<35%) logt de code 'ZOMBIE coin — shadow only' (regel 2830) maar doet geen `continue` en zet live_toegestaan niet op False. live_toegestaan blijft `score_ok and plan_g_stop_ok` (regel 2945). De -10 score-malus voor ZOMBIE kan onvoldoende zijn om onder de drempel te komen.  
- impact: Een coin die als verliesgevend (ZOMBIE) is geclassificeerd kan alsnog met live_toegestaan=True in pending_approvals belanden, terwijl de logregel het tegenovergestelde suggereert. Misleidend en risicovol.  
- fix: Maak de intentie waar: bij ZOMBIE `live_toegestaan = False` forceren (of `continue` naar shadow), niet alleen loggen.  

**6. [scanner-risk/logica] daily/weekly stop telt alleen gesloten trades (exit_time), open verliezen tellen niet mee**  
- plek: `analysis/multi_coin_score.py:2207-2220, 2688-2692` (vertrouwen: hoog)  
- beschrijving: get_daily_pnl somt alleen rijen met outcome WIN/LOSS gefilterd op DATE(exit_time/updated_at). Open (lopende) verliesposities tellen niet mee in de daily-stop-evaluatie (regel 2690 `if dagpnl <= -DAILY_STOP_LOSS_EUR`). Een weekly stop ontbreekt volledig in de scanner, terwijl strategy_rules sectie 8 WEEKLY_STOP_LOSS vereist.  
- impact: De daily stop kan te laat triggeren: meerdere openstaande, fors verliezende posities verlagen de gerealiseerde PnL niet, dus de scanner blijft nieuwe trades toestaan tot verliezen daadwerkelijk gerealiseerd zijn. Een weekly stop bestaat niet op scanner-niveau.  
- fix: Neem unrealized PnL van open live-posities mee in de daily-stop-check, en implementeer een weekly-stop (of bevestig dat live_trader dit afdwingt). Documenteer welke laag verantwoordelijk is.  

**7. [wachters/logica] Watchdog schrijft eigen OK-heartbeat ook nadat de DB-ronde faalde**  
- plek: `/Users/hein/dev/crypto_ai/process_watchdog.py:158-166, 220-233` (vertrouwen: hoog)  
- beschrijving: Als de hoofd-DB-query op regel 64-156 faalt, wordt de exception gevangen (regel 158) en een 'DATABASE ONBEREIKBAAR'-probleem toegevoegd, maar de uitvoering loopt door naar regel 220-233 waar via een NIEUWE connectie de eigen heartbeat 'watchdog','OK',NOW() wordt weggeschreven. Als de tweede connectie wél lukt (bijv. transiente fout in de eerste), staat de watchdog zelf op OK terwijl hij zijn ronde niet heeft kunnen doen.  
- impact: Een meta-wachter (laag 3) die de watchdog bewaakt ziet 'watchdog OK' en concludeert dat alle bewaking draait, terwijl de watchdog in werkelijkheid geen enkele service heeft kunnen controleren. Blinde vlek bovenop een blinde vlek.  
- fix: Schrijf de eigen heartbeat met status='OK' alleen weg als de ronde compleet zonder fatale fout is gelopen; zet anders status='ERROR' met de oorzaak in details, zodat de eigen heartbeat de gefaalde ronde reflecteert.  

**8. [wachters/logica] WhatsApp-dedup verstuurt altijd het volledige bericht en markeert onderdrukte services als verzonden**  
- plek: `/Users/hein/dev/crypto_ai/process_watchdog.py:173-185, 191-212` (vertrouwen: hoog)  
- beschrijving: Het bericht (regel 173-185) wordt opgebouwd uit ALLE problemen. De dedup-lus (191-208) bepaalt per service of die binnen 4u al gemeld is en vult _te_sturen alleen met nieuwe services, maar verstuurt op regel 211-212 simpelweg het ongefilterde 'bericht' zodra _te_sturen niet leeg is. Tegelijk wordt voor elke service in _te_sturen de timestamp bijgewerkt — maar services die WEL onderdrukt waren (niet in _te_sturen) verschijnen toch in het verzonden bericht, terwijl andere onderdrukte items dubbel onderdrukt blijven.  
- impact: Twee gevolgen: (1) Hein krijgt in het bericht problemen te zien die net als 'al gemeld' onderdrukt waren — dedup werkt niet zoals bedoeld. (2) Belangrijker: als alleen een laag-prioriteit nieuw probleem _te_sturen vult, wordt een KRITIEK probleem dat toevallig binnen 4u al gemeld was nogmaals getoond (goed), maar een KRITIEK dat net onderdrukt is kan in een ronde waarin niets nieuw is HELEMAAL niet verstuurd worden terwijl het nog actief is. Inconsistente alarmering.  
- fix: Bouw het verzonden bericht op uit _te_sturen i.p.v. alle problemen, en update de bot_state-timestamp alleen voor services die daadwerkelijk in het verstuurde bericht staan. Overweeg KRITIEK uit te zonderen van de 4u-dedup of een kortere dedup-window voor KRITIEK te hanteren.  

**9. [wachters/race-condition] data_wachter: bevindingen worden geescaleerd naar HOOG-mail maar logboek-schrijven kan stil falen zonder fallback**  
- plek: `/Users/hein/dev/crypto_ai/wachters/data_wachter.py:48-68, 141-150` (vertrouwen: hoog)  
- beschrijving: logboek() vangt alle fouten stil af (regel 61-62: 'logboek onbereikbaar (genegeerd)'). In main() worden eerst alle bevindingen weggeschreven naar het logboek (142-143) en daarna pas de HOOG-mail verstuurd (144-149). Als het logboek onbereikbaar is, gaat de HOOG-mail nog wel uit (goed), maar MEDIUM-bevindingen verdwijnen volledig zonder enig spoor (geen mail, logboek genegeerd). De mailtekst verwijst bovendien naar 'Overige bevindingen staan in het Centrale Logboek' terwijl die er bij een logboek-storing niet staan.  
- impact: Bij een storing op de logboek-DB gaan alle MEDIUM-bevindingen (dubbeltellingen, lege MFE/MAE) geruisloos verloren; Hein krijgt een mail die naar een leeg logboek verwijst. Stille data-verlies van bewakingssignalen.  
- fix: Detecteer of logboek() slaagde (laat het True/False teruggeven) en neem, als het logboek faalde, ook de MEDIUM-bevindingen op in de HOOG-mail (of stuur altijd een korte samenvatting van alle bevindingen mee, niet alleen HOOG). Pas de mailtekst aan zodat hij niet onterecht naar het logboek verwijst.  

**10. [wachters/data-integriteit] reconciliatie: afwijking bij verwacht=0 wordt geforceerd op 100% i.p.v. echte verhouding; ongeregistreerde positie onder MIN_EUR glipt door**  
- plek: `/Users/hein/dev/crypto_ai/wachters/reconciliatie_wachter.py:222-240` (vertrouwen: hoog)  
- beschrijving: Regel 224: afw_pct = abs(werkelijk-exp)/exp*100 if exp>0 else 100.0 — exp komt uit SUM(qty) van open LIVE-trades en kan in principe 0 of negatief zijn als qty-administratie raar is; bij exp<=0 wordt het altijd 100% (alarm), wat ruis geeft. Belangrijker is regel 236: ongeregistreerde assets bij Bitvavo worden alleen gemeld als asset_waarde_eur >= MIN_EUR (default 5). asset_waarde_eur faalt stil naar 0.0 als de publieke ticker faalt (regel 175-177), waardoor een onbekende, mogelijk waardevolle positie als 0 euro telt en NOOIT gemeld wordt.  
- impact: Een onbekende restpositie waarvan de prijs-lookup faalt (delisted markt, API-hik, asset zonder -EUR markt) wordt op 0 euro geschat en glipt onder de MIN_EUR-drempel door: echte drift gemist (false-negative). Daarnaast genereert exp<=0 onnodige 100%-alarmen.  
- fix: Behandel een mislukte prijs-lookup (return 0.0) NIET als 'verwaarloosbaar': log/meld onbekende assets met onbekende waarde apart als LAAG/MEDIUM i.p.v. ze te negeren. Voeg een guard toe voor exp<=0 (aparte melding 'negatieve/0 geregistreerde qty') i.p.v. blind 100%.  

**11. [research-sim/data-integriteit] WIN/LOSS geclassificeerd op netto-PnL i.p.v. exit-reden; kosten draaien target-hits om naar LOSS**  
- plek: `research/market_simulator.py:203, 220` (vertrouwen: hoog)  
- beschrijving: outcome = 'WIN' if net_pnl > 0 else 'LOSS'. net_pnl trekt entry_cost en exit_cost (fees+slip+spread) af. Een trade die de target raakte (exit_reden=STRUCTUUR_BREAK of een kleine plus) maar waar de kosten groter zijn dan de bruto winst, wordt als LOSS gelabeld. Dit is inconsistent met strategy_simulator/signal_replay waar WIN op target-hit wordt bepaald.  
- impact: Win-rate is niet vergelijkbaar tussen de drie simulators die allemaal in experience_trades schrijven. monte_carlo_forecast aggregeert ze door elkaar (zelfde 'outcome'-veld), dus de gemengde WR is inconsistent gedefinieerd.  
- fix: Definieer WIN/LOSS consistent op basis van exit-reden/bruto-resultaat (exit_price vs entry) over alle simulators, en rapporteer netto-PnL apart. Documenteer een eenduidige outcome-definitie.  

**12. [research-sim/logica] Geen slippage/fees in plan_u_shadow, signal_replay en strategy_simulator R-berekening**  
- plek: `research/plan_u_shadow.py:148-151` (vertrouwen: hoog)  
- beschrijving: plan_u_shadow rekent pnl puur op (exit-entry)/entry zonder fees/slippage/spread. signal_replay (replay_signal/save_replay_trade, regel 169-171) rekent result_r en pnl zonder kosten. strategy_simulator sim-pnl is pr*SIM_POSITION_EUR zonder kosten. Alleen market_simulator past kosten toe.  
- impact: C2 BOUNCE target is +3% / stop -2%; bij Bitvavo taker fees (~0.25% per kant) plus spread/slippage verdwijnt een groot deel van de edge. De gerapporteerde positieve verwachting is structureel te optimistisch, en de forecast die deze trades meeneemt overschat de winstkans.  
- fix: Trek per trade een vaste kostenmarge af (bv. 2x fee + spread) op entry en exit, of corrigeer target/stop met kosten voordat WIN/LOSS wordt bepaald.  

**13. [research-sim/data-integriteit] regime_labeler: slope/SMA over niet-uniform timestamp-raster zonder gap-controle**  
- plek: `research/regime_labeler.py:363-388, 412-413, 567-614` (vertrouwen: midden)  
- beschrijving: fetch_closes_and_ts haalt de laatste LOOKBACK closes op ORDER BY open_time DESC en draait om, maar controleert niet op ontbrekende candles/tijdgaten. linear_regression_slope gebruikt index i (0..n-1) als x-as, aannemend dat candles equidistant zijn. sma() neemt simpelweg de laatste N. Bij gaten in de candle-historie (gestopte fetcher, missing 4h-bars) wordt de slope/SMA over een vertekend tijdsbestek berekend.  
- impact: Het per-coin regime (BULL/BEAR/RANGE) dat multi_coin_score als filter gebruikt kan fout zijn bij datagaten — een coin krijgt een BULL/BEAR-label op basis van een raster dat in werkelijkheid weken overspant. Dit beinvloedt welke signalen door de bot worden gegenereerd en dus indirect de hele backtest-keten.  
- fix: Valideer dat de opgehaalde candles aaneengesloten zijn (verwachte stap == timeframe) of bereken slope op basis van werkelijke timestamps (x = open_time). Markeer regime als UNKNOWN bij te grote gaten.  

**14. [state-config/config] Tegenstrijdige live_toegestaan-default (FALSE in DDL versus TRUE in schema-sync)**  
- plek: `/Users/hein/dev/crypto_ai/data/fix_experience_schema.py:328` (vertrouwen: hoog)  
- beschrijving: fix_experience_schema.py regel 328 voegt pending_approvals.live_toegestaan toe met DEFAULT TRUE. multi_coin_score.py regel 2383 en 2402 maken dezelfde kolom met DEFAULT FALSE. Beide gebruiken IF NOT EXISTS, dus de eerste aanmaker bepaalt de default.  
- impact: live_trader.py regel 2355 koopt live op live_toegestaan is TRUE. Als de kolom met DEFAULT TRUE ontstaat en een insert de waarde niet expliciet zet, worden signalen die de PLAN_G-filter NIET haalden (regel 2945 live_toegestaan False) toch live verhandeld. Dat is een direct geld-risico.  
- fix: Maak de default overal FALSE (veilige default voor een live-handelsvlag). Pas regel 328 aan naar DEFAULT FALSE; insert_pending geeft de waarde al expliciet mee op regel 2489.  

**15. [state-config/race-condition] Scheduler zonder overlap-bescherming; jobs stapelen, dubbele instances mogelijk**  
- plek: `/Users/hein/dev/crypto_ai/scripts/scheduler.py:8-35` (vertrouwen: hoog)  
- beschrijving: subprocess.run wordt synchroon aangeroepen; next_multi wordt op now plus 30 min gezet met now bovenaan de loop. Als multi_coin_score langer dan 30 min draait ligt next_multi in het verleden en draait de job meteen opnieuw. Er is geen lockfile of PID-guard tegen twee scheduler-instances, terwijl de jobs dezelfde DB-tabellen schrijven.  
- impact: Bij trage runs of dubbele schedulers draaien multi_coin_score en history_fetcher dubbel en gelijktijdig, met dubbele pending_approvals, candle-upsert-races en verspilde API-calls. De 30-minuten cadans is niet gegarandeerd.  
- fix: Plan next_multi op basis van de geplande tijd (next_multi telkens plus 30 min, achterlopende runs overslaan) of gebruik cron of APScheduler met misfire-handling. Voeg een single-instance guard toe (advisory lock of lockfile).  

**16. [state-config/data-integriteit] db.py queryt monitor_updated_at en buy_attempts die het schema nooit aanmaakt**  
- plek: `/Users/hein/dev/crypto_ai/dashboard/db.py:78 85 224` (vertrouwen: hoog)  
- beschrijving: get_open_trades selecteert experience_trades.monitor_updated_at (78 en 85); get_pending_signals selecteert pending_approvals.buy_attempts (224). Geen van beide kolommen staat in fix_experience_schema.py; buy_attempts wordt alleen gelezen in multi_coin_score.py regel 2176 en nergens aangemaakt.  
- impact: Als deze kolommen niet handmatig via psql zijn toegevoegd falen de betreffende dashboard-queries volledig (geen open trades, geen pending signals zichtbaar). Het schema-sync-script claimt idempotent alle kolommen te beheren maar dekt deze niet; een verse DB mist ze.  
- fix: Voeg monitor_updated_at (TIMESTAMPTZ) toe aan EXPERIENCE_TRADES_COLUMNS en buy_attempts (INTEGER DEFAULT 0) aan PENDING_APPROVALS_COLUMNS, of verwijder de verwijzingen in db.py. Neem ze op in verify_schema.  

### LAAG (11)

**1. [uitvoering/data-integriteit] PnL in paper_trader negeert fees/slippage; inconsistent met live/shadow**  
- plek: `trading/paper_trader.py:298-299` (vertrouwen: hoog)  
- beschrijving: paper sell berekent pnl = (price - entry) * qty zonder fees of slippage. live_trader gebruikt bereken_pnl_nauwkeurig met fee_buy+fee_sell (regel 1377+, BITVAVO_FEE_PCT 0.0025 + SLIPPAGE_PCT 0.001), en shadow_trades trekt fee_entry+fee_exit af (regel 1245-1247). De paper-PnL is dus structureel te optimistisch.  
- impact: Paper-resultaten overschatten winst en wijken systematisch af van live/shadow, waardoor edge-decay-vergelijkingen (sim vs live, trade_monitor.check_edge_decay) en leeranalyses vertekenen. Een strategie kan in paper winstgevend lijken maar na fees verliesgevend zijn.  
- fix: Pas dezelfde fee/slippage-aftrek toe als shadow_trades/live_trader: net_pnl = (price-entry)*qty - entry*qty*TOTAL_COST_PCT - price*qty*TOTAL_COST_PCT, en log pnl_pct conform strategy_rules.md sectie 9.  

**2. [uitvoering/bug] buy_eur/sell gebruiken EUR-ticker maar entry kan 0/None opleveren → deling door nul**  
- plek: `trading/paper_trader.py:109-114, 166-167` (vertrouwen: hoog)  
- beschrijving: _bitvavo_price() doet symbol.replace('USDT','-EUR').replace('BUSD','-EUR') en leest r.json()['price']. Er is geen guard op price<=0 of op een symbool dat niet op die manier converteert (bv. een coin zonder USDT-suffix wordt niet geconverteerd en faalt bij Bitvavo). buy_eur() doet direct qty = amount_eur / price (regel 167) zonder price>0 check.  
- impact: Bij een 0/onverwachte prijs of mislukte conversie volgt ZeroDivisionError of een onzinnige qty; de exception in buy_eur wordt niet afgevangen (geen try/except rond _bitvavo_price), waardoor de aanroeper crasht of een 500 krijgt. Geen positie maar mogelijk wel verwarrende state.  
- fix: Valideer market-conversie en price>0 vóór de deling: 'if price <= 0: return {"ok": False, "reason": "PRICE_INVALID"}', en vang requests-fouten af met een nette foutretour.  

**3. [uitvoering/data-integriteit] open_trades-lijst en positions kunnen uiteenlopen bij gefaalde save**  
- plek: `trade_monitor.py:1918-1932` (vertrouwen: hoog)  
- beschrijving: Na process_live_trade verwijdert run_monitor_once de symbol uit state['positions'] en state['open_trades'] bij sold=True, maar save_state(state) (regel 1932) schrijft alleen naar live_state.json — terwijl load_state() (volgende run) de waarheid uit de DB haalt. De JSON-state die hier wordt opgebouwd/weggeschreven wordt dus niet door de monitor zelf teruggelezen.  
- impact: De pop/filter-bewerkingen op de in-memory state hebben geen blijvend effect voor de monitor (DB is leidend), wat verwarrend is en de schijn van restart-safety wekt zonder dat te zijn. Bij een halve sell (live_changed maar sold-status onbetrouwbaar door eerdere bugs) kan de JSON afwijken van de DB.  
- fix: Verwijder de JSON-save uit de monitor of laat de afsluiting van een trade uitsluitend via de DB-UPDATE (status='CLOSED') verlopen, zodat er één consistente bron is.  

**4. [scanner-risk/risico] live_ok (max-open-trades, daily-stop, pauze, weekend, trading-hours) wordt nooit toegepast op live_toegestaan**  
- plek: `analysis/multi_coin_score.py:2670-2704, 2945, 2969, 3025` (vertrouwen: hoog)  
- beschrijving: scan_universe berekent live_ok door achtereenvolgens bot_active, pauze, trading-hours, weekend, daily-stop (regel 2688-2692), daglimiet en max-open-trades (regel 2700-2704) te checken. Maar live_toegestaan wordt op regel 2945 berekend als `score_ok and plan_g_stop_ok` — live_ok komt hier NIET in voor. live_ok wordt alleen in logregels (2727, 2880) en bij een ZOMBIE-logregel (2828) gebruikt. Een pending_approval wordt dus weggeschreven met live_toegestaan=True ook al staat de bot op pauze, is de daily-stop geraakt, of zijn er al MAX_OPEN_REAL_TRADES posities open.  
- impact: Alle scanner-side hardlimieten zijn effectief tandeloos: bij bereikte max-open-trades, geraakte daily-stop, pauze of buiten trading-hours blijft de scanner signalen markeren als live-toegestaan. Als live_trader/whatsapp_webhook deze limieten niet nog eens onafhankelijk afdwingt, kan de bot meer dan MAX_OPEN_REAL_TRADES posities openen en doorhandelen na de daily stop-loss — precies wat strategy_rules.md sectie 8 verbiedt.  
- fix: Neem live_ok mee in de gate: `live_toegestaan = live_ok and score_ok and plan_g_stop_ok and coin_cluster != 'ZOMBIE'`. En/of: schrijf bij `not live_ok` de pending weg met live_toegestaan=False. Verifieer bovendien dat live_trader de limieten zelf opnieuw checkt als defense-in-depth.  

**5. [scanner-risk/bug] Funding-rate bonus in calculate_score gebruikt ongedefinieerde variabele 'symbol' (dode code, stil gefaald)**  
- plek: `analysis/multi_coin_score.py:2014-2028` (vertrouwen: hoog)  
- beschrijving: Binnen calculate_score wordt `MarketData(_conn).funding_rate_bonus(symbol)` aangeroepen (regel 2021), maar `symbol` is geen parameter van calculate_score (zie signatuur regel 1769-1781). Dit gooit een NameError die door de brede `except Exception: pass` (regel 2027) stil wordt opgeslokt. Bovendien opent het per coin een nieuwe DB-connectie en injecteert het een hardcoded sys.path.  
- impact: De funding-rate-bonus uit market_data.py wordt nooit toegepast (altijd stil overgeslagen). Daarnaast onnodige DB-connecties en sys.path-manipulatie per coin-scan; bij trage DB kan dit de scan vertragen. Geen crash, maar de feature is non-functioneel.  
- fix: Geef symbol als parameter door aan calculate_score, of verwijder dit blok. Vervang de blinde `except Exception: pass` door gerichte logging zodat zulke fouten zichtbaar worden.  

**6. [scanner-risk/bug] init_sessie leunt op module-globale 'drempels' i.p.v. parameter (verborgen koppeling)**  
- plek: `analysis/multi_coin_score.py:710-730, 722, 3113-3116` (vertrouwen: hoog)  
- beschrijving: init_sessie() verwijst op regel 722 naar `drempels.get('min_score', ...)` maar krijgt drempels niet als argument. Het werkt alleen omdat main() de globale naam `drempels` toevallig op regel 3113 toekent vóór init_sessie() op 3116. Wordt init_sessie ooit eerder of vanuit een andere context aangeroepen, dan volgt een NameError.  
- impact: Fragiele impliciete afhankelijkheid van een globale variabele; verandert de aanroepvolgorde in main() of hergebruikt iemand init_sessie, dan crasht het. Geen actueel falen maar een latente bug.  
- fix: Geef drempels expliciet door: `def init_sessie(drempels): ...` en roep aan als `init_sessie(drempels)`.  

**7. [research-sim/logica] signal_replay vult op stored entry en exit_time = created_at + hold (entry-timing onrealistisch)**  
- plek: `research/signal_replay.py:84-95, 113-145, 188-189` (vertrouwen: hoog)  
- beschrijving: get_candles_after pakt candles met open_time >= created_at, maar replay_signal vult op de doorgegeven 'entry' (stored signaalprijs), niet op de open van candles[0]. Er is geen check dat entry binnen candles[0]-range lag. exit_time wordt berekend als created_at + hold_candles uur, los van de werkelijke candle-timestamps.  
- impact: Net als market_simulator: fills tegen mogelijk onbereikbare prijzen geven optimistische bias. Bovendien kan de eerste candle de candle ZIJN waarin het signaal ontstond (>= i.p.v. >), waardoor de stop/target geraakt kan worden door beweging die al voor het signaal plaatsvond — look-ahead.  
- fix: Filter candles strikt op open_time > created_at, vul op candles[0]['open'] met kosten, en gebruik de echte open_time van de exit-candle als exit_time.  

**8. [research-sim/data-integriteit] strategy_simulator: TIMEOUT-uitkomst telt elke afsluiting boven entry als WIN ongeacht R**  
- plek: `trading/strategy_simulator.py:452-461` (vertrouwen: hoog)  
- beschrijving: Bij geen stop/target-hit binnen LOOKAHEAD wordt outcome 'WIN' als end_close>e, anders 'LOSS', met pr = (end_close-e)/(e-s). Een afsluiting 0,1% boven entry telt als volwaardige WIN in de win-rate-statistiek (en promotiedrempel), ook al is de R bijna nul.  
- impact: Win-rate wordt opgeblazen door marginale timeouts. Omdat promotie SIM->SHADOW->LIVE puur op win-rate stuurt, kan een strategie met veel near-zero timeouts ten onrechte promoveren.  
- fix: Definieer WIN alleen bij R boven een minimumdrempel, of rapporteer timeouts als aparte categorie en stuur promotie op verwachte R i.p.v. ruwe win-rate.  

**9. [state-config/bug] db.py interpoleert source en days direct in SQL (injectie/format-risico)**  
- plek: `/Users/hein/dev/crypto_ai/dashboard/db.py:141 151 160 171` (vertrouwen: hoog)  
- beschrijving: get_pnl_per_day, get_outcome_distribution en get_pnl_histogram bouwen de WHERE met f-strings (source direct in de tekst en INTERVAL days) terwijl de rest van db.py psycopg2 parameters gebruikt. source is een vrije string.  
- impact: Bij vaste interne source-waarden beperkt risico, maar niet-geparametriseerd: een quote breekt de query of laat SQL-injectie toe zodra deze functies een door de gebruiker beinvloedbare waarde krijgen. Inconsistent met de veilige patronen elders.  
- fix: Gebruik overal parameters: AND source = parameter, en days via een interval-parameter. Verwijder alle f-string-interpolatie van waarden in SQL.  

**10. [state-config/data-integriteit] account_snapshot.py: DB-connectie zonder retry/finally en lekt key-lengtes**  
- plek: `/Users/hein/dev/crypto_ai/account_snapshot.py:160-161 172-189` (vertrouwen: hoog)  
- beschrijving: save_snapshot_to_db opent psycopg2.connect zonder sslmode of retry en sluit zonder finally, dus de connectie lekt bij een exception. write_snapshot schrijft bij API-fout de lengtes api_key_len en api_secret_len in het snapshot-JSON (regel 160-161), dat in een DB-tabel en op de Render-disk belandt.  
- impact: Connectie-leak kan bij herhaalde fouten de DB-pool uitputten. Het loggen van sleutel-lengtes lekt secret-metadata (aanwezigheid en exacte lengte) naar een gedeeld snapshot of dashboard.  
- fix: Gebruik dezelfde db_connect met sslmode require en retries, met try/finally conn.close(). Verwijder api_key_len en api_secret_len of vervang door een boolean keys_present.  

**11. [state-config/race-condition] set_bot_val gebruikt gedeelde cached connectie en wist hele data-cache; race met andere schrijvers**  
- plek: `/Users/hein/dev/crypto_ai/dashboard/db.py:198-216` (vertrouwen: hoog)  
- beschrijving: set_bot_val schrijft via de cache_resource gedeelde connectie en doet daarna cache_data.clear() dat ALLE cached data wist. De live_trader schrijft parallel naar dezelfde bot_state via eigen connecties. Reads gaan via get_bot_state met ttl 10.  
- impact: bot_state loopt tot circa 10 seconden achter in het dashboard; bij gelijktijdig zetten van bijvoorbeeld bot_paused vanuit dashboard en trader kan een waarde verloren gaan (last-write-wins, geen rij-locking). cache_data.clear invalideert onnodig alle dashboarddata.  
- fix: Invalideer gerichter (alleen get_bot_state). Gebruik voor kritieke gedeelde keys (bot_paused, bot_active) een single-writer-conventie of een updated_at-vergelijking. Verlaag de ttl of voeg een refresh toe.  

## Ontbrekende interne wachters (voorstellen)

**[KRITIEK] Kapitaal-Wachter (kill-switch bij abnormaal verlies)**  
- doel: Bewaakt de dagelijkse en cumulatieve LIVE-PnL en het totale gerealiseerde verlies; bij overschrijding van een drempel zet hij bot_paused=true (echte noodrem) i.p.v. alleen te loggen, met KRITIEK-alarm.  
- waarom nodig: In live_trader.check_trading_limits worden DAILY_STOP_LOSS_EUR (default EUR 5) en MAX_CONSECUTIVE_LOSSES (default 3) bewust ALLEEN gelogd: de code zegt expliciet 'bot gaat door (jij beslist via STOP)' en 'Bot stopt NOOIT automatisch'. Er is dus geen enkele geautomatiseerde noodrem bij doorlopend verlies of een verliescascade; een kapotte strategie of foute markt kan ongeremd dag na dag verlies stapelen tot Hein het handmatig ziet. De reconciliatie-pauze dekt alleen positie-drift, niet verlies.  

**[HOOG] Exposure-Wachter (totale-blootstelling/positielimiet)**  
- doel: Berekent de som van alle open LIVE-posities in EUR (en per coin) en alarmeert/pauzeert wanneer de totale blootstelling of de concentratie in 1 coin een plafond overschrijdt.  
- waarom nodig: Er bestaat alleen een per-trade-cap (MAX_PER_TRADE_EUR, default EUR 15) en een AANTAL-cap (MAX_OPEN_REAL_TRADES, default 10). Nergens in trading/ of whatsapp_webhook wordt de SOM van amount_eur over open posities bewaakt (grep op total/totale/max exposure gaf 0 hits). 10 trades zonder aggregaat-plafond, plus mogelijke herhaalde buys op dezelfde coin, kan een veel grotere of geconcentreerdere blootstelling opleveren dan bedoeld zonder dat iets alarmeert.  

**[HOOG] State-Drift-Wachter (live_state.json vs experience_trades)**  
- doel: Vergelijkt de open posities in /data/live_state.json met de open LIVE-rijen in experience_trades (DB) en alarmeert bij verschil in aantal/coins/qty.  
- waarom nodig: live_trader.get_open_real_trades_count vertrouwt EERST het JSON-statebestand (load_state) en valt alleen terug op de DB als het bestand leeg is. De limietcheck (max open) hangt dus aan een los bestand op de /data-disk dat kan divergeren van de DB die het dashboard en de Reconciliatie-Wachter gebruiken. Niemand bewaakt of deze twee boekhoudingen gelijk lopen; bij drift kan de bot meer posities openen dan toegestaan of een sell missen. De Reconciliatie-Wachter kijkt naar Bitvavo vs DB, niet naar JSON vs DB.  

**[HOOG] Boekhouding-Drift-Wachter (paper vs live vs shadow vs Plan U)**  
- doel: Controleert per administratie (paper_state.json, shadow_trades.json/experience_trades SHADOW, live experience_trades, plan_u_trades) of de tellingen, statussen en PnL-totalen consistent en plausibel zijn, en of de aparte ledgers niet stilletjes uiteenlopen.  
- waarom nodig: Paper (JSON+CSV), shadow (JSON + DB source=SHADOW), live (JSON + DB source=LIVE) en Plan U (eigen DB-tabellen) worden door verschillende scripts bijgehouden zonder een wachter die ze tegen elkaar legt. De Data-Wachter dekt alleen Plan U-integriteit en experience_trades-anomalieen; drift tussen de boekhoudingen (bv. shadow_trades.json zegt OPEN maar experience_trades zegt CLOSED, of paper-CSV loopt achter) blijft onbewaakt en vervuilt de winratio-vergelijking waarop besluiten worden genomen.  

**[HOOG] Slippage-/Executie-Wachter**  
- doel: Vergelijkt de werkelijke fill-prijs van elke LIVE BUY/SELL (uit Bitvavo fills) met de verwachte/signaalprijs en alarmeert bij structureel hoge slippage of bij fills die ver van de verwachte prijs liggen.  
- waarom nodig: place_market_buy_eur/place_market_sell in live_trader berekenen de fill-prijs uit de fills maar vergelijken die NERGENS met de signaalprijs of de aanname SLIPPAGE_PCT (0.1%). PnL en R worden berekend met de aangenomen kostenfractie, niet met de echte slippage. Abnormale uitvoering (dunne order book, flash-move, verkeerde markt) wordt dus niet gedetecteerd terwijl het direct geld kost en de meetdata vertekent.  

**[HOOG] Stale-Price-/Data-Freshness-Wachter (beslis-pad)**  
- doel: Bewaakt dat de koers/candle-data die multi_coin_score en de monitor gebruiken vers is (recente candle-timestamps, ticker niet bevroren) voordat er op gehandeld wordt, en alarmeert/pauzeert bij verouderde of bevroren prijzen.  
- waarom nodig: Op het beslis-pad wordt op publieke Bitvavo-ticker/candles gehandeld zonder freshness-check; alleen trade_monitor heeft een interne health-check (BTC-data <5u) die NIET alarmeert (de WA-melding is uitgecommentarieerd: 'if health[score]<50 and False'). Een bevroren of vertraagde feed kan zo tot buys/sells op stale prijzen leiden zonder dat iemand het merkt. Geen enkele wachter monitort prijs-/data-leeftijd of feed-latency.  

**[MIDDEN] Order-Reconciliatie-Wachter (open orders + recente fills bij Bitvavo)**  
- doel: Trekt openstaande orders en recente fills op bij Bitvavo (/v2/orders, /v2/trades) en legt die naast de eigen administratie: hangende/niet-uitgevoerde orders, fills zonder bijbehorende DB-trade, en gedeeltelijke fills.  
- waarom nodig: De Reconciliatie-Wachter vergelijkt alleen eind-saldi (/v2/balance) per asset. Een blijvend openstaande order, een fill die de bot niet verwerkte (bv. crash tussen placeOrder en DB-insert), of een gedeeltelijke fill wordt zo niet gezien zolang het saldo per asset toevallig binnen 1% blijft. Er is geen bewaking op orderniveau, terwijl juist daar geld 'verdwijnt' of dubbel geboekt raakt.  

**[MIDDEN] Config-Drift-Wachter (limiet-parameters)**  
- doel: Controleert dat de kritieke limiet-parameters (MAX_PER_TRADE_EUR, MAX_OPEN_REAL_TRADES, MAX_REAL_TRADES_PER_DAY, DAILY_STOP_LOSS_EUR, MAX_CONSECUTIVE_LOSSES, fee/slippage) consistent zijn tussen de bestanden EN gelijk aan de door Hein goedgekeurde referentiewaarden, en dat bot_state-overrides (bv. max_open_trades, daily_stop_loss_eur) binnen veilige grenzen blijven.  
- waarom nodig: Dezelfde limieten staan los gedefinieerd in live_trader.py, whatsapp_webhook.py en (volgens de comments) 'alle andere bestanden', en sommige kunnen via bot_state worden overschreven (get_bot_state('max_open_trades'...), 'daily_stop_loss_eur'). Niets bewaakt of die kopieen synchroon blijven of dat een per ongeluk/kwaadwillend gewijzigde env-var of bot_state-waarde de risicogrenzen stilletjes verruimt. Drift hierin ondermijnt alle andere limieten zonder zichtbaar alarm.  

**[MIDDEN] API-Quota-/Rate-Limit-Wachter (Bitvavo + Anthropic)**  
- doel: Bewaakt het resterende Bitvavo rate-limit-budget (x-bitvavo-ratelimit-remaining headers) en het Claude/Anthropic-tokenverbruik (agent_logs.tokens_used) en alarmeert bij naderende uitputting of een piek aan 429/limiet-fouten.  
- waarom nodig: Er is geen enkele bewaking van Bitvavo-quota of -weight (grep op ratelimit/429/quota gaf buiten Twilio-dedup geen hits in het trading-pad). Bij uitputting van het Bitvavo-budget kunnen buys/sells/price-calls falen midden in trade-beheer (stops worden dan niet uitgevoerd!). Daarnaast logt risk_gate tokens en faalt OPEN bij Claude-fouten; een quota-/kostenexplosie of een stille degradatie naar 'auto-GO' wordt door niemand gevolgd.  

**[LAAG] Poortwachter-Bypass-Wachter (risk_gate auto-GO)**  
- doel: Telt hoe vaak risk_gate in script_mode/auto-GO valt (Claude-fout of open circuit) via agent_logs en alarmeert wanneer een ongebruikelijk deel van de trades de inhoudelijke risk-beoordeling overslaat.  
- waarom nodig: risk_gate.beoordeel_trade geeft bij elke Claude-fout en bij open circuit-breaker bewust {go: true, script_mode: true} terug ('bot mag niet stoppen'). Dat betekent dat trades zonder enige risk-beoordeling door kunnen, en geen enkele wachter signaleert wanneer de poortwachter feitelijk uit staat. Een aanhoudende Claude-/API-storing maakt zo stil de belangrijkste pre-trade-control onschadelijk.  
