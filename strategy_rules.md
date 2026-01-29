# CRYPTO_AI — PAPER_TRADER RULES (v1)

Dit document beschrijft exact wat `paper_trader.py` wel en niet doet.
Dit is de “execution engine”: hier zitten entries/exits, risk discipline en 100% trade closure.

Belangrijk:
- `multi_coin_score.py` = scanner + signalen
- `paper_trader.py` = uitvoering + exits (system for exit)

Als iets hier niet expliciet staat, doet `paper_trader.py` het niet.

---

## 0) Doel & Filosofie (Execution Engine)
- Doel: trades uitvoeren met maximale discipline.
- Nooit emotioneel. Nooit gokken. Nooit improviseren.
- Elke trade eindigt op exact **100% exit**.
- Geen restposities.
- Geen dubbele sells.
- Alles wordt gelogd (audit-proof).

---

## 1) Rol en Grenzen (Wat doet paper_trader WEL / NIET)
### WEL
- Orders uitvoeren (paper) op basis van goedgekeurde BUY’s.
- Posities beheren: SL / target / weak-sells.
- Exits uitvoeren volgens regels.
- Beschermen via hard-limits (daily/week stop, max open trades).
- Logging van elke actie + WHY.

### NIET
- Geen markt-analyse (geen SMA/RSI berekenen).
- Geen pre-buy selectie.
- Geen “signaal genereren”.
- Geen entry zonder menselijke goedkeuring (of expliciete AUTO_TRADE instelling).
- Geen SELL’s op basis van score/indicatoren uit multi_coin_score (die hoort alleen te scannen).

---

## 2) Data Model: Wat moet paper_trader bijhouden
paper_trader moet per open trade minimaal bijhouden:
- trade_id (uniek)
- symbol
- entry_price
- qty_total
- qty_remaining
- stop_loss_price (ATR-based)
- target_price
- weak_sell_1_price
- weak_sell_2_price
- flags:
  - ws1_done (True/False)
  - ws2_done (True/False)
  - closed (True/False)

Opslag:
- bij voorkeur persistente opslag (JSON in logs/ of eenvoudig CSV) zodat bot restart-safe is.

---

## 3) Entry Regels (BUY)
### Entry mag alleen als:
- BUY is goedgekeurd door mens (of AUTO_TRADE staat aan voor paper).
- Risk settings beschikbaar zijn vóór entry:
  - SL (ATR based)
  - Target
  - Weak-sell niveaus
- Max open trades wordt niet overschreden.
- Daily/week stop is niet getriggerd.

### Entry acties
Bij entry voert paper_trader uit:
1) Positie openen (paper)
2) SL + target + weak-sell levels berekenen/zetten
3) Trade state opslaan
4) Logging:
   - log_trade(action="ENTRY")
   - log_event(event_type="BUY_EXECUTED")

---

## 4) Stop Loss (Hard Stop, altijd 100% exit)
- Als prijs <= stop_loss_price:
  - Direct 100% exit op remaining_qty
  - trade.closed = True
  - Geen verdere acties toegestaan na sluiting

Logging:
- log_trade(action="STOPLOSS", qty=remaining_qty, reason="Hard stop loss hit")
- log_event("TRADE_CLOSED", ...)

---

## 5) Target (Power Exit)
- Als prijs >= target_price:
  - Direct 100% exit op remaining_qty
  - trade.closed = True

Logging:
- log_trade(action="TARGET", qty=remaining_qty, reason="Target hit")
- log_event("TRADE_CLOSED", ...)

---

## 6) Weak-Sell Management (alleen bij zwakte)
Doel:
- Verkopen gebeurt alleen bij zwakte als target niet gehaald wordt.

Niveaus:
- WEAK_SELL_1: verkoop 65% van totale positie
- WEAK_SELL_2: verkoop 35% van totale positie
Samen = 100% exit (geen restpositie)

### Regels
#### A) Weak Sell 1
Trigger:
- prijs valt onder weak_sell_1_price (zwakte signaal)
Actie:
- verkoop 65% van qty_total
- ws1_done = True

Logging:
- log_trade(action="WS1", qty=sold_qty, reason="Weak sell 1 triggered")
- log_event("SELL_EXECUTED", ...)

#### B) Weak Sell 2
Trigger:
- ws1_done = True
- prijs valt onder weak_sell_2_price
Actie:
- verkoop remaining_qty (of 35% van totaal)
- ws2_done = True
- trade.closed = True

Logging:
- log_trade(action="WS2", qty=remaining_qty, reason="Weak sell 2 triggered")
- log_event("TRADE_CLOSED", ...)

#### C) Terugval vanaf WS2 zonder WS1
Specifieke set-regel:
- Als prijs zó snel terugvalt dat WS2 geraakt wordt zonder dat WS1 “netjes” verwerkt is:
  - Forceer FULL EXIT (100%) op remaining_qty
  - trade.closed = True

Logging:
- log_trade(action="FULL_EXIT", qty=remaining_qty, reason="WS2 hit without WS1 -> force exit")

---

## 7) Geen dubbele sells / Geen restposities (Hard rule)
paper_trader mag nooit:
- WS1 2x uitvoeren
- WS2 2x uitvoeren
- SELL uitvoeren als trade.closed = True
- Remaining_qty negatief maken

Check:
- Voor elke sell:
  - if trade.closed: return/do nothing
  - if sold_qty <= 0: return/do nothing

---

## 8) Max open trades / Cooldown / Daily & Weekly stop
paper_trader moet harde grenzen afdwingen:
- MAX_OPEN_TRADES
- COOLDOWN per coin
- DAILY_STOP_LOSS_PCT
- WEEKLY_STOP_LOSS_PCT

Als daily/week stop triggert:
- Geen nieuwe entries meer.
- Alleen exits/risicoreductie toegestaan.

Logging:
- log_event("STOP_TRIGGERED", symbol="SYSTEM", message="Daily/Weekly stop triggered", extra={...})

---

## 9) Logging (Audit-proof)
paper_trader logt minimaal:
- ENTRY
- WS1 / WS2
- STOPLOSS
- TARGET
- FULL_EXIT (force close)
- TRADE_CLOSED

En altijd:
- trade_id
- symbol
- price
- qty
- reason (WHY)
- optioneel confidence (van scanner)
- optioneel pnl_pct (als beschikbaar)

---

## 10) Aanpassingen die in paper_trader.py moeten komen (Checklist)
Om maximale output te krijgen en “optimal” te werken moet `paper_trader.py` minimaal dit hebben:

1) Imports:
   - import config
   - from trade_logger import log_trade, log_event

2) Persistente trade state:
   - open_trades.json opslaan in logs/ zodat restart safe is

3) Eén centrale functie:
   - `update_positions(symbol, current_price)` die:
     - stoploss checkt
     - target checkt
     - weak-sell checks doet
     - en exact 100% closure garandeert

4) Flags en guards:
   - ws1_done / ws2_done / closed
   - prevent double sells

5) PnL berekening (optioneel maar sterk):
   - per exit loggen: pnl_pct

6) “System for exit”
   - multi_coin_score.py mag geen SELL meer doen
   - paper_trader beheert alle exits

---

## 11) Definitie: “Optimaal werkt”
paper_trader is optimaal als:
- Elke trade altijd eindigt op 100% exit
- Exits zijn deterministic (geen twijfel)
- Restart kan zonder state te verliezen
- Logs maken elke trade achteraf volledig reproduceerbaar
- Hard limits beschermen je account tegen slechte dagen

Einde.
