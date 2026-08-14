# Live Breakout Picks — top 10 of 217 actionable

**Scan 2026-08-09T15:21:57** · universe 3,833 · **market: Bull (above 50 & 200) · SPY 773 vs SMA50 748/SMA200 703 (+10% vs 200) on 2026-08-10**

> Model = 29.6% baseline + backtest lifts (price, shape, depth, days, dis/acc ratios, cup, pre-BO signals). It ranks candidates; it is not a calibrated probability. In Base = close within ±5% of the pivot (will break out); Post-BO = ≤ 5 bars since the breakout.

| # | Ticker | Status | Pattern / Shape | Buy point | Entry | Model win % |
|---|---|---|---:|---:|---:|---:|
| 1 | **PINS** | Post-BO | Base / Consolidation | $24.71 | 3 bars ago · 4% below pivot (retest) | 46% |
| 2 | **IBCP** | Post-BO | Base / Consolidation | $39.17 | 4 bars ago · 3% below pivot (retest) | 43% |
| 3 | **WCC** | Post-BO | Base / Consolidation | $377.90 | 2 bars ago · 4% below pivot (retest) | 41% |
| 4 | **HL** | In Base | Base / Consolidation | $17.38 | 3% below pivot | 39% |
| 5 | **PTHS** | In Base | Base / Consolidation | $29.76 | 5% below pivot | 39% |
| 6 | **ENR** | In Base | Cup / Cup | $23.25 | 3% below pivot | 38% |
| 7 | **VOD** | In Base | Base / Consolidation | $16.61 | 2% below pivot | 38% |
| 8 | **NUE** | Post-BO | Cup / Cup | $270.90 | 3 bars ago · 1% above pivot | 37% |
| 9 | **PDEX** | In Base | Base / Consolidation | $68.99 | 5% below pivot | 37% |
| 10 | **MLI** | Post-BO | Cup / Cup | $70.95 | 1 bar ago · 3% below pivot (retest) | 37% |

## Why each one scored (drivers)

**1. PINS** (46%) — $25 price (+2.3); Consolidation (+4.5); depth 27% (+4.4); 62d (+1.5); dis-ratio 15% (+5.0); acc-ratio 29% (+0.5); RS new high before BO (+4.2); volume dry-up (-6.5)
**2. IBCP** (43%) — $39 price (-1.5); Consolidation (+4.5); depth 15% (+0.1); 46d (+1.6); dis-ratio 17% (+2.9); acc-ratio 33% (+4.3); RS new high before BO (+4.2); shakeout (-2.4)
**3. WCC** (41%) — $378 price (-4.8); Consolidation (+4.5); depth 21% (+2.2); 43d (+2.3); dis-ratio 16% (+2.9); acc-ratio 30% (+4.3)
**4. HL** (39%) — $17 price (+2.3); Consolidation (+4.5); depth 21% (+2.2); 35d (+2.3); dis-ratio 26% (-2.0); acc-ratio 29% (+0.5)
**5. PTHS** (39%) — $30 price (-1.5); Consolidation (+4.5); depth 21% (+2.2); 28d (+0.7); dis-ratio 18% (+2.9); acc-ratio 25% (+0.5)
**6. ENR** (38%) — $23 price (+2.3); Cup (-1.0); depth 18% (+0.1); 30d (+2.3); dis-ratio 13% (+5.0); acc-ratio 23% (-1.4); cup-bars 1 (+1.4)
**7. VOD** (38%) — $17 price (+2.3); Consolidation (+4.5); depth 22% (+2.2); 61d (+1.5); dis-ratio 23% (-0.4); acc-ratio 18% (-1.8)
**8. NUE** (37%) — $271 price (-4.8); Cup (-1.0); depth 21% (+2.2); 34d (+2.3); dis-ratio 18% (+2.9); acc-ratio 32% (+4.3); cup-bars 5 (+1.8)
**9. PDEX** (37%) — $69 price (-1.7); Consolidation (+4.5); depth 23% (+2.2); 46d (+1.6); dis-ratio 17% (+2.9); acc-ratio 30% (+4.3); volume dry-up (-6.5)
**10. MLI** (37%) — $71 price (-1.7); Cup (-1.0); depth 23% (+2.2); 35d (+2.3); dis-ratio 17% (+2.9); acc-ratio 29% (+0.5); cup-bars 6 (+1.8)

---
*Model caveats: in-sample lifts from `tv_pattern_history_backtest.py`; correlated dimensions double-count partially; the $<10 price bonus barely exists in the current live set (1 candidate). Post-BO picks trading **below** the pivot are retests — they have already broken out and pulled back under the buy point, so treat them as buy-on-reclaim entries, not fresh breakouts. Full scored set (with `dist_pct` per candidate) in `live_breakout_picks.csv`.*
