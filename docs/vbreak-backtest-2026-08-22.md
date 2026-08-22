# VBREAK teruggerekend — 22 augustus 2026

Twee onafhankelijke doorrekeningen (Fable 5 en Opus 5), allebei read-only op de
publieke Gate-API. Aanleiding: VBREAK draait sinds 21 augustus als shadow en
boekte in anderhalve dag +37,7% over 218 afgeronde trades. De vraag was of die
twee dagen typisch zijn, en of er al iets te verbeteren valt.

**Kort antwoord: nee en nee.** De strategie is buiten die twee dagen
verliesgevend, en elke geteste verbetering steunt volledig op diezelfde dagen.

## De opzet van beide metingen

| | Fable 5 | Opus 5 |
|---|---|---|
| Universum | top-420 op liquiditeit | 257 coins (alle top-100 + 1-op-4 uit de staart) |
| Historie | ~412 dagen, 1u-candles | ~375 dagen, 1u-candles |
| Trades | 1294 | 899 |
| Methode | live-regels uit `gate_shadow.py::run_vbreak` | idem, regel voor regel nagebouwd |

Opus koos bewust niet voor de bestaande `bt_vbreak()` in
`research/backtest_daytrade.py`: die wijkt op drie punten af van live (getrapte
kosten in plaats van vast 0,60%, andere exit-volgorde, en het
volumemediaan-venster bevat de dag zelf).

Beide replica's zijn gevalideerd tegen de echte shadow-dag van 21 augustus en
komen op hetzelfde teken en dezelfde orde van grootte uit.

## 1. Zijn 21 en 22 augustus typisch? Nee

Fable, per maand:

```
maand     trades  winrate  gem/trade    som%
2025-09      349    26,1%   -0,784%   -273,6
2025-10      508    23,4%   -0,216%   -109,8
2025-11       24    25,0%   -1,653%    -39,7
2026-08      413    67,8%   +3,329%  +1374,9
TOTAAL      1294    38,3%   +0,736%   +951,8
```

Opus komt op een ander universum tot hetzelfde beeld: 899 trades, 38,7%
winrate, +900,9% totaal, waarvan +1059,4% uit augustus 2026.

De twee cijfers die alles zeggen:

- **16 van de 51 handelsdagen was positief** (Opus).
- **Zonder de twee beste dagen staat de hele historie op −158,5%** in plaats van
  +900,9% (Opus). Fable komt op hetzelfde uit vanaf de andere kant: de vorige
  bull-fase, 881 trades in najaar 2025, verloor **−423** met winrate 24,5%.

Die 24,5% is vrijwel exact wat de live shadow op 22 augustus liet zien (24,3%
met een dikke minsom). **21 augustus is de uitzondering, 22 augustus is de
norm.** In Fable's meting was 21 augustus met +1008 over 228 trades de beste dag
in dertien maanden — tien keer beter dan de op twee na beste.

## 2. De scheefheid is structureel, geen toeval van deze week

Fable (1294 trades, totaal +952):

| Deel | Bijdrage | Rest |
|---|---|---|
| Beste 1% (13 trades) | +743 | +209 |
| Beste 5% (65 trades) | +1633 | **−681** |
| Beste 10% | +2185 | **−1233** |

Mediane trade: **−1,68%**.

Opus (899 trades, totaal +901): beste 5% = +1206,5%, oftewel **134% van het hele
resultaat**; zonder dat deel −305,6%. En het is niet één toevallige maand: in
drie van de vier maanden is de rest-na-top-5% negatief.

Dezelfde verhouding als in de live-periode ("zes trades +232, rest −194") zit dus
over de hele historie in de strategie ingebakken. VBREAK is per constructie een
loterij: heel veel briefjes van −3,6%, af en toe een jackpot.

## 3. De stop staat niet te strak

De 74% stop-ratio die live opviel was een verliesdag-effect. Over de historie is
het 42% (Fable) respectievelijk 37,5% (Opus).

Fable's varianten:

```
variant   winrate  gem/trade    som%   stopfreq
FIX2        31,1%   +0,440%   +569,1     57,2%
FIX3 (nu)   38,3%   +0,736%   +951,8     42,0%
FIX5        43,8%   +0,868%  +1123,1     23,6%
ATR x1,5    46,2%   +0,768%   +993,7      3,6%
GEEN STOP   46,5%   +0,732%   +946,9        -
```

Een ruimere stop koopt winrate maar betaalt met grotere klappen; per saldo
verandert er niets. Opus komt tot dezelfde conclusie én laat zien waarom een
"beste variant" hier niets betekent: **de twee vensters spreken elkaar tegen.**
In najaar 2025 is strakker juist beter (−158,5% bij 3% tegen −360,1% zonder
stop), in augustus 2026 is losser beter. Dat is geen edge, dat is één
trendrichting.

## 4. De uitgang op tijd kapt de winnaars niet af

Opus, langer aanhouden op dezelfde trade-set:

| Variant | Winrate | Som | tegen live 1 dag |
|---|---|---|---|
| 3 dagen | 15,7% | −345,0% | −186,5% |
| 5 dagen | 11,6% | −535,7% | −377,2% |
| 20 dagen | 4,6% | −815,9% | −657,4% |

De winrate stort in van 39% naar 4,6%. De dagexit doet zijn werk.

Trailing stops lijken op papier beter (Fable: +1883 tegen +952), maar houden
geen stand: **zonder 19 tot 21 augustus wordt trailing slechter dan de basis**
(−653 tegen −423). En Opus liet zien dat de trailing-varianten overfit zijn: ATR
×4 geeft −104, ATR ×5 geeft +389. Zulke sprongen tussen buurparameters betekenen
dat een handvol trades het cijfer maakt, geen mechanisme.

## 5. Het belangrijkste getal: de som is geen rendement

Op de piek stonden er **141 posities tegelijk open**, gemiddeld 8,4. Die
percentages kun je dus niet optellen — je zou 141 posities tegelijk moeten
financieren.

Opus rekende het door met echte positiegrootte:

| Max posities | Inzet/trade | Genomen | Gemist | Rendement | Max drawdown |
|---|---|---|---|---|---|
| kale som | — | 899 | 0 | **+900,9%** | — |
| 5 | 20% | 332 | 567 | +18,0% | 24,5% |
| **10** | **10%** | **495** | **404** | **+16,8%** | **16,6%** |
| 20 | 5% | 628 | 271 | +11,1% | 9,9% |
| 50 | 2% | 755 | 144 | +10,1% | 5,2% |

**+900,9% wordt +16,8%. De kale som overschat met een factor ~54.** En van die
+16,8% komt het 2025-deel op −1,4% uit: álles zit in twee dagen van 2026. De
crashdag van 22 augustus (live −530%) zit er niet eens in.

Dit geldt één op één voor de +37,7% die de shadow nu rapporteert.

## Wat deze meting niet kan zien

- **Survivorship bias.** Alleen coins die vandaag nog op Gate staan, tradable
  zijn en geen `st_tag` hebben. Gedelisteerde flops ontbreken volledig — en juist
  daar zou een uitbraakstrategie op stukgelopen zijn. Vertekent omhoog, het
  hardst bij microcaps, waar VBREAK het meest handelt.
- **Kosten te gunstig.** Met het getrapte spreadmodel uit `backtest_gate.py`
  (0,5 tot 1,2% per rondrit in plaats van vast 0,60%) zakt Fable's gemiddelde van
  +0,74% naar +0,25% per trade, en wordt alles buiten augustus dieper negatief.
- **22 augustus is onafrondbaar** in beide metingen: die trades hebben de
  dagcandle van 23 augustus nodig. Precies de dag die live −530% boekte.
- **Twee bull-vensters in een jaar**, waarvan er één wint. Feitelijk n=2 regimes.
- Entry exact op het uitbraakniveau negeert slippage bij snelle bewegingen. Live
  heeft dezelfde aanname, dus de replica is trouw — maar allebei te gunstig.

## Conclusie en advies

De eerlijkste steekproef die er is — 46 handelsdagen najaar 2025 — geeft
**−0,249% per trade na kosten**. Het enige positieve bewijs is twee dagen
euforie, en de dag daarna gaf de live shadow er −530% van terug.

**Niet doen:** de stop verruimen of op ATR zetten, langer aanhouden, of een
trailing stop toevoegen. Alle vier de "verbeteringen" rusten op twee tot drie
dagen in augustus 2026, maken de vorige bull-fase niet winstgevend, en trailing
maakt die zelfs slechter. Met 51 handelsdagen waarvan 16 positief is dat te
weinig om ook maar één parameter te verzetten.

**Wel overwegen — maar dit is Heins beslissing:** de shadow een
positiegrootte-model geven, met een vaste inzet per trade en een harde grens op
het aantal gelijktijdige posities (10 is een redelijk startpunt). Dat is een
*meetfout* herstellen, geen strategiewijziging: zolang de shadow een som van
percentages rapporteert, is elk vervolgbesluit gebaseerd op een getal dat een
factor 50 te hoog is. Het raakt de handelslogica niet aan, maar het verandert
wel de shadow-opzet — en die blijft ongemoeid tot Hein er expliciet ja op zegt.

**En verder gewoon laten doorlopen** tot er minstens één volledige bull-fase van
meerdere weken in zit. Dat is de enige meting die kan bewijzen of de
jackpotdagen de loterijbriefjes structureel betalen.
