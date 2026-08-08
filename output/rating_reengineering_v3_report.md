# IBD Rating Reverse-Engineering v3 (2-Week Pooled Panel + MarketSurge Oracle Features)

Extends `rating_reengineering_v2_report.md`. Still closed-form only (percentile ranks + `numpy.linalg.lstsq` + constrained scalar-weight optimization) — no black-box ML.

**Snapshots used**: 2026-07-24, 2026-08-07 (confirmed via price-matching against ticker_cache, exactly 2 weeks apart).

**Pooled panel size**: 7,334 (ticker, date) rows across 3,701 unique tickers.

## 1. RS Rating (Pooled 2-Date)

| Method                                                      |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Pooled 2-date monotonic-optimal + percentile-rank (n=6,943) | 0.7087 | 10.99 | 0.8813 |        15.7 |        26.8 |         54.8 |
| -> same weights, 2026-07-24 only (n=3,495)                  | 0.7145 | 10.89 | 0.8835 |        14.8 |        25.8 |         56.4 |
| -> same weights, 2026-08-07 only (n=3,448)                  | 0.7043 | 11.12 | 0.88   |        16.2 |        26.5 |         52.8 |

**Pooled-optimal monotonic-recency weights**: 1M=0.2539, 3M=0.2538, 6M=0.2340, 9M=0.1939, 12M=0.0644

Per-date breakdown uses the SAME weights fit on the pooled panel — the point is to check the formula isn't secretly overfit to one week's particular market regime. (v2 single-date-only result was R²=0.71 on 2026-08-07 alone.)

## 2. A/D Rating (Pooled 2-Date + Oracle Diagnostic)

| Method                                                       |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Self-computed (price/volume only), pooled 2-date (n=7,177)   | 0.1651 |  3.51 | 0.4078 |        40.1 |        77.3 |         99.8 |
| ORACLE: MarketSurge's own Up/Dn-Vol+ATR+Funds cols (n=7,177) | 0.5076 |  2.43 | 0.713  |        65.6 |        89.3 |         99.8 |
| CEILING: last week's own A/D Rating alone (n=7,177)          | 0.5897 |  1.84 | 0.7993 |        81.1 |        92.3 |         99.9 |
| Self-computed + oracle combined (n=7,177)                    | 0.5478 |  2.32 | 0.7411 |        68.1 |        90.7 |         99.9 |

**Oracle feature weights** (MarketSurge's own columns — diagnostic only, not a production input since it still depends on MarketSurge):

| Feature             |   OLS_Coef |   Abs_Weight_Pct |
|:--------------------|-----------:|-----------------:|
| Price vs 50-Day     |     1.495  |             31.9 |
| Up/Down Vol         |     1.3219 |             28.2 |
| 21 Day ATR %        |     0.8759 |             18.7 |
| Funds % Increase    |    -0.3537 |              7.5 |
| Funds %             |    -0.3214 |              6.9 |
| Number of Funds     |     0.1631 |              3.5 |
| Daily Closing Range |     0.0938 |              2   |
| Vol % Chg vs 50-Day |     0.0361 |              0.8 |
| Price vs 10-Day     |     0.0264 |              0.6 |

**Reading this table**: the CEILING row (last week's own A/D Rating predicting this week's) shows how persistent/autocorrelated A/D naturally is — that's the bar any same-week formula effectively competes against. If the ORACLE row (IBD's own volume/ATR/Funds numbers) beats our self-computed price/volume version by a wide margin, that confirms the v2 finding: the gap is institutional Funds-flow data we don't have, not the combining formula. If it doesn't beat it by much, our price/volume proxy is already close to what's extractable from technicals alone.

## 3. EPS Rating — Oracle vs Self-Computed Ceiling

| Method                                                               |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| ORACLE: MarketSurge's own EPS growth/ROE cols (n=5,443)              | 0.5306 | 14.07 | 0.7286 |          15 |        24.8 |         47.3 |
| Self-computed (ticker_cache fund json only), pooled 2-date (n=5,211) | 0.3123 | 18.18 | 0.5588 |           9 |        15.4 |         30.7 |

**EPS oracle feature weights** (diagnostic, MarketSurge-sourced — NOT usable in a MarketSurge-free production formula):

| Feature                  |   OLS_Coef |   Abs_Weight_Pct |
|:-------------------------|-----------:|-----------------:|
| ROE 5-Yr Avg             |     3.2676 |             22.1 |
| EPS % Growth 5 Yr        |     2.5527 |             17.2 |
| EPS % Growth 1 Yr        |     2.1868 |             14.8 |
| EPS % Chg Last Qtr (-/+) |     1.951  |             13.2 |
| EPS % Chg Lst Yr         |     1.452  |              9.8 |
| EPS Surprise             |     1.292  |              8.7 |
| Avg EPS % Chg 2Q         |     1.1203 |              7.6 |
| Avg EPS % Chg 4Q         |    -0.4357 |              2.9 |
| ROE                      |    -0.4279 |              2.9 |
| EPS % Growth 3 Yr        |     0.1183 |              0.8 |

## 4. SMR Rating — Oracle vs Self-Computed Ceiling

| Method                                                               |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| ORACLE: MarketSurge's own Sales/Margin cols (n=6,332)                | 0.7206 | 10.26 | 0.853  |        20.1 |        31.6 |         56.3 |
| Self-computed (ticker_cache fund json only), pooled 2-date (n=5,638) | 0.6497 | 10.68 | 0.8064 |        18.4 |        29.5 |         52.7 |

**SMR oracle feature weights** (diagnostic, MarketSurge-sourced):

| Feature             |   OLS_Coef |   Abs_Weight_Pct |
|:--------------------|-----------:|-----------------:|
| Pre-tax Margins     |     4.2951 |             30.9 |
| AT Margin           |     2.2185 |             16   |
| Sales Growth 3 Yr   |     2.0583 |             14.8 |
| Avg Sales % Chg 4Q  |     1.933  |             13.9 |
| Sales Growth 5 Yr   |     1.6651 |             12   |
| Sales % Chg Lst Yr  |     1.2803 |              9.2 |
| Sales % Chg Lst Qtr |     0.3481 |              2.5 |
| Avg Sales % Chg 2Q  |     0.1092 |              0.8 |

**How to read both tables**: the ORACLE row uses IBD's own fundamentals (nearly 100% coverage, clean point-in-time data) — it's the ceiling if our yfinance-based fundamentals extraction were perfect. The gap between ORACLE and "Self-computed" quantifies how much of EPS/SMR's remaining error is yfinance data-quality/coverage (shallow ~5-quarter window, ~70-95% coverage) vs the combining formula itself.

## 5. COMPOSITE RATING — Combining Weights (Pooled 2-Date)

**This is the rating actually used for filtering — here are its weights, front and center.** All 4 (5 with Group RS) component ratings are percentile-ranked to a common 1-99 scale within this universe before fitting (matches IBD's documented "combines the percentile rankings" methodology; fitting on raw scales instead lets AD_Num's narrow 1-13 range fake an oversized coefficient purely from scale, not real importance).

| Method                                               |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-----------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| OLS-weighted percentile ranks, no Group RS (n=6,324) | 0.9224 |  5.47 | 0.9608 |        34.7 |        53.2 |         85.3 |
| OLS-weighted percentile ranks, +Group RS (n=6,316)   | 0.9613 |  3.9  | 0.9811 |        46.7 |        69.1 |         96.2 |

### Composite Rating ≈ -5.03 + 0.3719 × [EPS percentile-rank] + 0.5308 × [RS percentile-rank] + 0.2047 × [SMR percentile-rank] + 0.2222 × [AD percentile-rank]

| Component | Weight (relative %) |
|---|---|
| RS | **39.9%** |
| EPS | **28.0%** |
| AD | **16.7%** |
| SMR | **15.4%** |

### With Industry Group RS included, Composite Rating ≈ -10.41 + 0.3753 × [EPS] + 0.4686 × [RS] + 0.2086 × [SMR] + 0.2119 × [AD] + 0.1747 × [GroupRS]

| Component | Weight (relative %) |
|---|---|
| RS | **32.6%** |
| EPS | **26.1%** |
| AD | **14.7%** |
| SMR | **14.5%** |
| GroupRS | **12.1%** |

**Takeaway**: RS and EPS together account for roughly two-thirds of Composite Rating's variance; SMR and A/D matter but are secondary. Industry Group RS pulls weight away from the individual-stock RS component (since it's correlated with it) while meaningfully improving fit — a stock's own RS plus its group's RS together explain more than either alone.

## 6. Rating Changes (Δ), 2026-07-24 → 2026-08-07

Tickers present with valid ratings in both snapshots: **3,154**. Targets are the raw point change in each rating over the 2 weeks; predictors are the CHANGE in each technical feature over the same window plus the realized 2-week return — information that doesn't exist in a single cross-section.

| Target   | Method                                                              |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------|:--------------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| dRS      | RS_old + realized-return deltas (n=3,154)                           | 0.6465 |  6.62 | 0.804  |        29.7 |        48.6 |         78.4 |
| dAD      | Self-computed: AD_old + technical/volume deltas (n=3,117)           | 0.5462 |  2.15 | 0.7391 |        74.3 |        93.6 |         99.9 |
| dAD      | ORACLE: AD_old + Up/Dn-Vol+Funds-flow deltas (n=3,154)              | 0.478  |  2.31 | 0.6914 |        71   |        91.9 |         99.9 |
| dAD      | Self-computed + oracle combined (n=3,117)                           | 0.56   |  2.1  | 0.7483 |        75   |        94.3 |         99.8 |
| dEPS     | EPS_old + analyst-estimate-revision deltas (n=3,154)                | 0.0527 |  7.03 | 0.2296 |        40.5 |        61.5 |         76.8 |
| dSMR     | SMR_old only, no fundamentals-delta predictor available (n=3,154)   | 0.0198 |  2.5  | 0.1409 |        90.6 |        90.6 |         90.6 |
| dComp    | (a) Formula check: dComp ~ dRS+dEPS+dSMR+dAD (n=3,154)              | 0.8827 |  3.87 | 0.9395 |        49.7 |        72.9 |         93.5 |
| dComp    | (b) Practical: dComp ~ raw price/volume/funds deltas only (n=3,153) | 0.5967 |  7.16 | 0.7725 |        29.9 |        46.1 |         74.1 |

**Oracle Δ(A/D) feature weights** (institutional Funds-flow deltas, diagnostic only):

| Feature            |   OLS_Coef |   Abs_Weight_Pct |
|:-------------------|-----------:|-----------------:|
| d_Up/Down Vol      |     2.419  |             77.8 |
| AD_old             |    -0.2882 |              9.3 |
| d_21 Day ATR %     |    -0.1764 |              5.7 |
| d_Price vs 50-Day  |     0.1756 |              5.6 |
| d_Funds %          |    -0.0414 |              1.3 |
| d_Funds % Increase |    -0.0101 |              0.3 |

**What this section shows:**

- **dRS** fits well by construction — RS Rating is a rolling-window function of price returns, so a realized 2-week return plus the already-existing relative-performance deltas should (and do) explain most of the change. This is a sanity check, not a new finding.

- **dAD is the interesting reversal.** For the LEVEL (Section 2), MarketSurge's own Funds/volume columns crushed our self-computed price/volume features (oracle R²=0.51 vs self-computed R²=0.17). For the CHANGE, that flips: self-computed technical deltas alone reach R²=0.55, actually *beating* the oracle Funds-flow deltas at R²=0.48 (combining both only adds a little more, R²=0.56). The likely reason: `Funds %`/`Funds % Increase` come from 13F-style institutional holdings that get reported quarterly and barely move within any given 2-week window, so they're excellent at explaining the accumulated LEVEL but nearly flat as a 2-week DELTA signal — while price/volume directly reflects exactly the trading that happened in that window. Practical upshot: for estimating the current A/D *level* from scratch, MarketSurge-grade Funds data would help a lot; for tracking near-term A/D *momentum* (which is arguably more actionable for screening), ticker_cache's own price/volume deltas are already close to as good as anything MarketSurge itself reports.

- **dEPS/dSMR** are, as expected, close to unpredictable from anything other than the starting level (mean reversion) — company fundamentals just don't move enough in 2 weeks for growth-rate deltas to mean anything; the only quasi-fundamental thing that DOES move week to week is analyst estimate revisions, which have a small but real relationship with dEPS.

- **dComp**, check (a): fitting Δ(Composite) on the actual Δ(RS)/Δ(EPS)/Δ(SMR)/Δ(A-D) nearly perfectly reproduces it (R²=0.883) — strong confirmation that the SAME linear combining formula derived from rating LEVELS (Section 5) also governs rating CHANGES, i.e. Composite really is just a stable linear recombination of its components, not something with extra path-dependent behavior.

- **dComp**, check (b): trying to shortcut straight from raw price/volume/Funds deltas to Δ(Composite) without going through the component ratings first (R²=0.597) works far less well — confirming there's no shortcut around computing the component ratings properly; Composite's structure is genuinely hierarchical.
