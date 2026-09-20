# INTERNE TOEZICHTPIRAMIDE — deep-dive auditrapport

Gegenereerd 2026-06-13 via multi-agent deep-dive (102 agents, 5-ronden-opzet).
Scope: **alleen de interne toezichtlaag** (wachters, centraal logboek, watchdog, escalatie naar Jarvis) — niet de externe werkbots (trading-logica).

> **Beperking:** door een sessielimiet draaiden alleen ronde 1–2 volledig (ronde 3–5 en de synthese-agents vielen uit). De 59 bevestigde bevindingen bevatten veel overlap; hieronder zijn ze geclusterd tot ~15 distincte problemen. De delen "Nieuwe interne bots" en "Routekaart" zijn daarna handmatig gesynthetiseerd uit de bevindingen.

Telling rauw: 59 bevestigd (9 kritiek, 22 hoog, 22 midden, 6 laag).

---

## De kernconclusie

De toezichtpiramide is goedbedoeld maar heeft één terugkerend, fataal patroon: **"de heartbeat verbergt stilstand"** — exact de 13-dagen-bug — komt in minstens **zes verschillende varianten** terug, én **niemand bewaakt de wachters zelf**. De laag die jou moet beschermen tegen een stille storing, kan zélf stil uitvallen zonder enig alarm.

---

## Distincte problemen (geclusterd)

### CLUSTER A — De toezichtlaag bewaakt zichzelf niet  *(kritiek)*
*Bevindingen: k1, k7, k8, k9, h19–h22, m(scheduler), m(trade_monitor heartbeat)*
- `data_wachter` en `reconciliatie_wachter` schrijven **geen heartbeat** naar `bot_health`. De watchdog kan een dode wachter principieel niet zien.
- `trade_monitor` (de stop-loss/exit-motor) staat **niet** in de watchdog-bewaking; crasht hij, dan blijven open posities onbeschermd zonder alarm.
- `scheduler.py` start **geen enkele wachter** — de hele toezichtlaag hangt aan losse, ongeziene Render-crons. Wordt er één gesuspend (is al met 9 services gebeurd), dan stopt bewaking geruisloos.
- **Niemand bewaakt de bewaker**: er is geen "wachter-van-de-wachters".

### CLUSTER B — Heartbeat verbergt stilstand (6 varianten)  *(kritiek)*
*Bevindingen: k5, k6, h1, h6, h11, h14, h4, m(NULL laatste_run), m(watchdog ON CONFLICT), l(klok vooruit)*
- `health_update` zet **nooit `heartbeat_sec`** → elke service krijgt NULL.
- NULL `heartbeat_sec` → `None*2` → **TypeError crasht de hele watchdog-ronde**, gemaskeerd als "DB onbereikbaar".
- NULL `laatste_run` telt als **"0s geleden / vers"** → nooit-gedraaide service lijkt gezond.
- Watchdog schrijft **zijn eigen heartbeat altijd 'OK'**, ook na een gecrashte ronde.
- Werkbots (`live_trader`, `trade_monitor`) schrijven **'OK' ook na een gecrashte loop-iteratie** → status-check vuurt nooit.
- Watchdog ziet alleen **bestaande rijen**: een service die nooit opstartte is onzichtbaar i.p.v. KRITIEK.

### CLUSTER C — De reconciliatie-noodrem is geen echte noodrem  *(kritiek)*
*Bevindingen: k3, k4, h10, h13, m(recovery)*
- `bot_paused_until` verloopt **automatisch na 12u** → de bot hervat LIVE-handel op niet-opgeloste positie-drift.
- De pauze wordt door de **werkbot zelf** opgeheven, niet door jou/Jarvis.
- De herstelmail belooft "blijft staan tot jij ingrijpt" — dat klopt niet met het gedrag.

### CLUSTER D — Laag 3 / Jarvis-keten ontbreekt  *(hoog)*
*Bevindingen: h2, h7*
- **Niets leest `toezicht_logboek`.** Alle MEDIUM-bevindingen (dubbeltellingen, lege MFE/MAE, "afwijking opgelost") verdwijnen in een put die niemand bekijkt. Er is ook geen staleness-detectie op het logboek zelf.

### CLUSTER E — Mechanismen werken tegen elkaar (data-vernietiging)  *(hoog)*
*Bevindingen: h9, h18*
- Een **tweede reconciliatie in `trade_monitor`** sluit open LIVE-trades automatisch met `pnl_pct=0` — dat wist juist het bewijsrecord dat de Reconciliatie-Wachter nodig heeft. De twee controles maskeren elkaars signaal.

### CLUSTER F — Onbewaakte werkbot-risico's (dekkingsgaten → nieuwe bots)  *(hoog/midden)*
*Bevindingen: h3, h5, h8, m(exposure), m(API-quota), m(order-recon)*
- Geen **kill-switch** bij doorlopend verlies; geen bewaking van cumulatieve PnL.
- Geen **feed-freshness**-bewaking: een bevroren prijsfeed laat stops onuitgevoerd zonder alarm.
- Geen bewaking van **paper/live/shadow/Plan-U-drift**, **totale exposure/concentratie**, **open/gedeeltelijke orders** of **API-quota**.

### CLUSTER G — Specifieke wachter-checks zijn kapot  *(hoog/midden)*
*Bevindingen: h15, h16, h17, m(check#4), m(vingerafdruk), m(24u-venster), m(stops alleen experience_trades)*
- Onveranderbaarheids-trigger wordt alleen op **bestaan** gecheckt, niet of hij **enabled** is.
- Check #5 leunt op kolom `bijgewerkt` die de resolver **nooit schrijft** → permanent kapot of permanent vals alarm.
- Check #4 gebruikt `= 0` i.p.v. `IS NULL` → vuurt nooit.
- Reconciliatie-vingerafdruk negeert de **omvang** van de drift → verergering blijft 24u stil.

---

## Nieuwe interne bots die nodig zijn

| # | Nieuwe bot | Laag | Bewaakt welk nu-onbewaakt risico | Prio |
|---|---|---|---|---|
| 1 | **Wachter-van-de-Wachters** (meta-heartbeat) | laag 3 | Of `data_wachter`, `reconciliatie_wachter`, `trade_monitor` en `process_watchdog` zélf nog draaien (heartbeat + `reconciliatie_last_check` vers). Lost cluster A op. | 🔴 kritiek |
| 2 | **Kapitaal-Wachter / kill-switch** | laag 2 | Cumulatieve & dagelijkse LIVE-PnL + consecutive losses → echte (sticky) pauze bij overschrijding. Lost h5 op. | 🔴 kritiek |
| 3 | **Laag-3 Logboek-Lezer (Jarvis-brug)** | laag 3 | Leest `toezicht_logboek`, escaleert onverwerkte MEDIUM+ naar jou/Jarvis, alarmeert als het logboek "bevroren" is. Lost cluster D op. | 🟠 hoog |
| 4 | **Feed-Freshness / Stale-Price-Wachter** | laag 2 | Per open LIVE-positie: is er recent een geldige prijs opgehaald? Alarmeert/pauzeert bij bevriezing. Lost h3 op. | 🟠 hoog |
| 5 | **Boekhouding-Drift-Wachter** | laag 2 | Vergelijkt paper/live/shadow/Plan-U-administraties op consistentie. Lost h8 op. | 🟠 hoog |
| 6 | **Order-Reconciliatie-Wachter** | laag 2 | Open/gedeeltelijke orders + fills bij Bitvavo naast de eigen administratie (de huidige recon kijkt alleen naar eindsaldo). | 🟡 midden |
| 7 | **Exposure-Wachter** | laag 2 | Som van open LIVE-posities + concentratie per coin tegen een plafond. | 🟡 midden |

---

## Routekaart — wat helpt vooruit

### Fase 1 — Snelle hoog-effect-fixes (klein, doe deze eerst)
1. **Watchdog NULL-guard**: `tolerantie = (hb_sec or 3600) * 2` + NULL `laatste_run` als KRITIEK behandelen (cluster B). *1–2 regels, herstelt de stilstand-detectie.*
2. **Watchdog eigen heartbeat** alleen 'OK' bij een geslaagde ronde (k6).
3. **`health_update` krijgt `heartbeat_sec`-parameter** + DEFAULT in het schema (h1/h6).
4. **Werkbots schrijven 'OK' alleen op het succespad**, anders `health_fout` (h11/h14).
5. **Reconciliatie-pauze sticky maken** (geen 12u auto-expiry voor KRITIEK; mailtekst kloppend) (cluster C).

### Fase 2 — Structureel herstel (middel)
6. **Alle wachters + `trade_monitor` laten heartbeat schrijven** naar `bot_health` (cluster A).
7. **`VERWACHTE_SERVICES`-allowlist** in de watchdog: ontbrekende verwachte service = KRITIEK (h4).
8. **Wachter-crons vastleggen in `render.yaml`/`scheduler.py`** zodat frequentie reviewbaar is (k9/h21/h22).
9. **Auto-close in `trade_monitor.reconcilieer_trades` uitschakelen** — laat pauzeren alleen aan de wachter over (cluster E).
10. **Data-Wachter-checks repareren**: trigger-`enabled`, `bijgewerkt`-kolom, `IS NULL`, vingerafdruk-met-omvang (cluster G).

### Fase 3 — Nieuwe wachters bouwen (groot)
11. Wachter-van-de-Wachters (#1) → Kapitaal-Wachter (#2) → Laag-3 Logboek-Lezer (#3) → Feed-Freshness (#4) → Boekhouding-Drift (#5).

**Volgorde-logica:** Fase 1 herstelt eerst dat de bestaande bewaking überhaupt wérkt (nu kan ze blind zijn zonder dat je het weet). Fase 2 maakt de laag robuust en zelf-bewakend. Fase 3 dicht de echte dekkingsgaten met nieuwe bots — waarvan de **Wachter-van-de-Wachters** en de **Kapitaal-Wachter** het meest urgent zijn.
