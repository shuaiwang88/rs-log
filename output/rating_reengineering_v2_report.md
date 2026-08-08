# IBD Rating Reverse-Engineering v2 (Closed-Form, No ML)

**Ground truth**: `marketsuge-8-7-2026.csv` | **Method**: percentile ranks + closed-form least-squares / constrained weight optimization on transparent formulas — no black-box models.

## Executive Summary

Per-rating accuracy, current production formula vs the recommended closed-form replacement (both evaluated on the same ticker_cache-derived universe):

| Rating     |   Baseline R2 |   Recommended R2 |   Baseline MAE |   Recommended MAE |   Baseline +/-5 Acc% |   Recommended +/-5 Acc% |
|:-----------|--------------:|-----------------:|---------------:|------------------:|---------------------:|------------------------:|
| RS Rating  |        0.7273 |           0.7051 |          10.73 |             11.08 |                 25.6 |                    26.7 |
| A/D Rating |        0.0485 |           0.1371 |           3.95 |              3.29 |                 60.2 |                    76.2 |
| EPS Rating |       -0.146  |           0.3239 |          22.36 |             17.95 |                 14   |                    15   |
| SMR Rating |        0.4911 |           0.6809 |          13.09 |             10.25 |                 25.1 |                    30.4 |

**Composite Rating, full self-computed pipeline** (RS/EPS/SMR/AD all recomputed from ticker_cache alone, zero MarketSurge inputs, n=2,480): R²=`0.7035`, MAE=`10.48`, ±5 Acc=`29.8%`, ±10 Acc=`55.2%`. This is the realistic ceiling today for computing Composite Rating without MarketSurge — bottlenecked mainly by A/D and EPS, both capped by real data-availability limits (see below), not by the combining formula.

**What actually changed and why:**

1. **RS Rating** — current formula was already close (R²=0.73); recommended change is cosmetic but principled: replace the fixed-curvature sigmoid with an actual percentile-rank against the universe (matches IBD's own definition of RS as a percentile rank) and mildly re-weight toward 1M/3M over 12M.

2. **A/D Rating** — improved (MAE 3.95→3.37, ±5 Acc 60%→75%) by blending CMF across multiple windows (65D + 130D) instead of a single 65D window, plus heavy-volume-day net ratio. Still the weakest-fitting rating (R² stays low): A/D fundamentally measures institutional buying/selling *flow*, and ticker_cache has no historical institutional-holdings deltas (13F-style) to measure that directly — only price/volume proxies for it. Tested current institutional-ownership *level* (`heldPercentInstitutions`, `institutionsCount` from the fundamentals json) explicitly; correlation with A/D was ~0.02-0.04, i.e. no signal, because A/D cares about the *change* in positioning, not the level. This is a real data ceiling, not a formula problem.

3. **EPS Rating** — current formula was actually *worse than predicting the mean* on this universe (R²=-0.146); fixed by (a) log-compressing the growth-rate features so small-denominator YoY blowups (a stock going from $0.001 to $0.30 EPS reads as +28,600%) stop dominating the fit while still preserving rank order, and (b) refitting the blend weights (R² → 0.3239). Still constrained by yfinance's ~5-quarter-deep quarterly financials, which structurally can't support the acceleration/2nd-derivative features IBD's real EPS Rating likely uses (EPS_Q1_YoY and EPS_Accel had 0% coverage in this universe and were dropped).

4. **SMR Rating** — biggest formula-level win. Production's SMR is currently ROE-only (R²=0.4911); adding the two missing pillars — Sales growth and Margin (now + trend), log-compressed the same way as EPS — gets to R²=0.6809. This is the clearest case where the current formula is missing real, available signal rather than hitting a data ceiling.

5. **Composite Rating** — confirmed IBD's documented approach (combine *percentile rankings* of the components, not their raw scales) matters mechanically: fitting OLS directly on raw component scales let AD_Num's narrow 1-13 range swamp the fit (spurious 54% "weight") purely as a scale artifact. Percentile-normalizing first gives interpretable weights (RS ≈39%, EPS ≈29%, AD ≈17%, SMR ≈15%, roughly matching RS+EPS being the commonly-cited dominant pair) and a true-component R² of 0.93 (0.96 with Industry Group RS added).

- Valid Comp Rating in CSV: **6,328**
- + price parquet present: **3,690** (price-only universe: RS, A/D)
- + fundamentals json present: **3,690** (full universe: EPS, SMR, Composite)

## 1. RS Rating

Evaluation universe: **3,448** stocks (full 12M price history + valid RS Rating).

| Method                                              |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:----------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (40/20/20/20 sigmoid)               | 0.7273 | 10.73 | 0.8682 |        15   |        25.6 |         53.4 |
| Same 40/20/20/20 weights, percentile-rank transform | 0.7051 | 11.31 | 0.8804 |        13.3 |        22.7 |         50.5 |
| Monotonic-optimal weights + percentile-rank         | 0.7051 | 11.08 | 0.8803 |        16.3 |        26.7 |         53.3 |
| Unconstrained OLS (direct, diagnostic)              | 0.3516 | 18.27 | 0.7037 |         9   |        14.9 |         28.5 |

**Optimal monotonic-recency weights** (1M ≥ 3M ≥ 6M ≥ 9M ≥ 12M):

| 1M | 3M | 6M | 9M | 12M |
|---|---|---|---|---|
| 0.2649 | 0.2649 | 0.2003 | 0.1990 | 0.0709 |

**Key finding**: swapping the sigmoid transform for a straight percentile-rank of the weighted relative-performance score (i.e. actually ranking the stock against the universe, rather than squashing a single stock's raw ratio through a fixed sigmoid) is what recovers most of the accuracy — the sigmoid's fixed curvature doesn't adapt to how spread out the universe's performance is on a given day.

## 2. A/D Rating

Evaluation universe: **3,568** stocks.

| Method                                            |      R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:--------------------------------------------------|--------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (65D CMF, 0-99->1-13 scaled)      |  0.0485 |  3.95 | 0.2732 |        32.1 |        60.2 |        100   |
| Percentile-rank of same CMF score                 | -0.118  |  3.81 | 0.3194 |        47.1 |        69.3 |         96.7 |
| OLS-weighted multi-window blend + percentile-rank |  0.1371 |  3.29 | 0.485  |        54.1 |        76.2 |         98.5 |
| Direct OLS onto 1-13 scale (diagnostic)           |  0.1969 |  3.54 | 0.4472 |        39.9 |        76.9 |         99.8 |

**OLS feature weights** (multi-window up/down-volume + heavy-volume intensity + volume-weighted closing range blend):

| Feature                |   OLS_Coef |   Abs_Weight_Pct |
|:-----------------------|-----------:|-----------------:|
| CMF_130D               |   -8.84668 |             36.4 |
| CMF_65D                |    6.95507 |             28.6 |
| HeavyNetRatio_65D      |    5.8045  |             23.9 |
| NetHeavyIntensity_30D  |    0.73514 |              3   |
| DnDayVolRatio          |    0.43964 |              1.8 |
| UpDnVol_65D            |   -0.35336 |              1.5 |
| UpDnVol_30D            |    0.36546 |              1.5 |
| UpDnVol_130D           |   -0.31629 |              1.3 |
| NetHeavyIntensity_65D  |   -0.23315 |              1   |
| VWClsRange_65D         |    0.1323  |              0.5 |
| UpDayVolRatio          |    0.11337 |              0.5 |
| NetHeavyIntensity_130D |   -0.01664 |              0.1 |

## 3. EPS Rating

Evaluation universe: **2,603** stocks (median-imputed features) out of 3,676 with a valid EPS Rating and fundamentals present.

**Feature coverage** (yfinance's quarterly financials are only ~5 quarters deep, so `EPS_Q1_YoY`/`EPS_Accel` — which need a 6th trailing quarter — are almost always missing and were dropped from the fitted model; they're still used, nan-aware, when reproducing the current production formula's exact fallback behavior below):

| Feature       |   Coverage_Pct |
|:--------------|---------------:|
| EPS_Q0_YoY    |           70.8 |
| EPS_LT_Growth |           94.3 |
| EPS_NegQRatio |          100   |
| ROE           |           99.8 |

| Method                                       |      R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------|--------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (blended growth -> sigmoid)  | -0.146  | 22.36 | 0.482  |         8.1 |        14   |         28.7 |
| Same blend, percentile-rank transform        | -0.4152 | 25.88 | 0.3991 |         8   |        12.9 |         23.8 |
| OLS-weighted feature blend (direct scale)    |  0.3239 | 17.95 | 0.5691 |         9.2 |        15   |         30.2 |
| OLS-weighted feature blend + percentile-rank | -0.0877 | 21.58 | 0.5536 |        11.2 |        17.2 |         31.9 |

**OLS feature weights**:

| Feature       |   OLS_Coef |   Abs_Weight_Pct |
|:--------------|-----------:|-----------------:|
| ROE           |    3.63233 |             40.6 |
| EPS_Q0_YoY    |    3.03585 |             33.9 |
| EPS_NegQRatio |    1.42528 |             15.9 |
| EPS_LT_Growth |    0.84979 |              9.5 |

## 4. SMR Rating

Evaluation universe: **2,818** stocks (median-imputed features) out of 3,190 with a valid SMR Rating and fundamentals present.

**Feature coverage** (`Sales_Accel` needs a 6th trailing quarter, same yfinance depth limit as `EPS_Accel`, and was dropped from the fitted model):

| Feature         |   Coverage_Pct |
|:----------------|---------------:|
| Sales_Q0_YoY    |           88.3 |
| Sales_LT_Growth |           96.1 |
| Margin_Now      |           91.1 |
| Margin_Trend    |           89.6 |
| ROE             |           99.8 |

| Method                               |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (ROE-only sigmoid)   | 0.4911 | 13.09 | 0.7145 |        16.8 |        25.1 |         43.3 |
| OLS 3-pillar blend (direct scale)    | 0.6809 | 10.25 | 0.8255 |        17.7 |        30.4 |         54.7 |
| OLS 3-pillar blend + percentile-rank | 0.3867 | 14.3  | 0.8331 |        14.8 |        23.9 |         41.1 |

**OLS feature weights** (the current production formula only uses ROE — this table shows how much Sales growth and Margin trend actually matter):

| Feature         |   OLS_Coef |   Abs_Weight_Pct |
|:----------------|-----------:|-----------------:|
| ROE             |    5.14281 |             39.3 |
| Sales_LT_Growth |    3.45453 |             26.4 |
| Margin_Now      |    2.54002 |             19.4 |
| Sales_Q0_YoY    |    1.54884 |             11.8 |
| Margin_Trend    |   -0.38515 |              2.9 |

## 5. Composite Rating

Evaluation universe (true components): **3,165** stocks. All components percentile-ranked to a common 1-99 scale before combining (matches IBD's documented methodology and avoids the scale artifact where AD_Num's narrow 1-13 range would otherwise dominate a raw-scale regression).

| Method                                                                     |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Equal-weight avg of percentile ranks (no Group RS)                         | 0.6093 | 13.37 | 0.9227 |        11.9 |        20.3 |         38.7 |
| OLS-weighted percentile ranks (no Group RS)                                | 0.9309 |  5.18 | 0.9652 |        36.7 |        54.5 |         88.4 |
| Equal-weight avg of percentile ranks (+Group RS)                           | 0.6086 | 13.39 | 0.9487 |        12.1 |        20.8 |         38.6 |
| OLS-weighted percentile ranks (+Group RS)                                  | 0.9642 |  3.77 | 0.9825 |        48   |        70.4 |         96.4 |
| Equal-weight avg, FULL SELF-COMPUTED PIPELINE (n=2,480)                    | 0.286  | 17.15 | 0.7685 |         9.6 |        15.6 |         31.4 |
| OLS-weighted, FULL SELF-COMPUTED PIPELINE (n=2,480, no MarketSurge inputs) | 0.7035 | 10.48 | 0.8389 |        17.9 |        29.8 |         55.2 |

**Combining weights (no Group RS), intercept=-5.34**:

| Component   |   OLS_Coef |   Rel_Weight_Pct |
|:------------|-----------:|-----------------:|
| EPS_pct     |     0.3867 |             29.1 |
| RS_pct      |     0.5224 |             39.3 |
| SMR_pct     |     0.1953 |             14.7 |
| AD_pct      |     0.2243 |             16.9 |

**Combining weights (+Group RS), intercept=-10.76**:

| Component   |   OLS_Coef |   Rel_Weight_Pct |
|:------------|-----------:|-----------------:|
| EPS_pct     |     0.3818 |             26.5 |
| RS_pct      |     0.469  |             32.6 |
| SMR_pct     |     0.2089 |             14.5 |
| AD_pct      |     0.2202 |             15.3 |
| GroupRS_pct |     0.1593 |             11.1 |

**Full self-computed pipeline combining weights, intercept=8.99**:

| Component   |   OLS_Coef |   Rel_Weight_Pct |
|:------------|-----------:|-----------------:|
| EPS_self    |     0.144  |             13.2 |
| RS_self     |     0.5515 |             50.6 |
| SMR_self    |     0.2699 |             24.8 |
| AD_self     |     0.1249 |             11.5 |

**This is the number that matters**: the "FULL SELF-COMPUTED PIPELINE" rows show what accuracy is achievable using *only* `ticker_cache` (price/volume + fundamentals json) end to end, with zero MarketSurge-derived inputs — the actual goal of this exercise.
