# IBD Rating Reverse-Engineering (Non-ML, ticker_cache only)

**Snapshots**: `marketsuge-8-7-2026.csv` (as-of **2026-08-07**) + `marketsurge.csv` (as-of **2026-07-24**) — different weeks, both used for testing.

**Universe**: new `3,201` | old `3,199` stocks (valid Comp + price parquet + fund json)

> Every model input comes from `ticker_cache` (parquet + fund json) and `IBD Industry Mapping.txt`.  MarketSurge supplies only ground-truth labels.  Price features for each snapshot are computed with history truncated to that snapshot's as-of day (old snapshot file date 2026-07-29, data as-of 2026-07-24).  All methods are transparent linear blends / percentile ranks / constrained scalar-weight fits — no machine learning.

## Executive summary — Composite Rating formula (the filter)

> **Production params: fit on OLD (2026-07-24, `marketsurge.csv`), validated on NEW (forward, out-of-sample) — no look-ahead. Both snapshot fits are archived below.

`Comp Rating = -16.777
 + 0.3116 × EPS_self + 0.6662 × RS_self + 0.2522 × SMR_self + 0.2501 × AD_self`

| Component   |   OLS_Coef |     Std |   Importance % |
|:------------|-----------:|--------:|---------------:|
| EPS_self    |   0.311558 | 16.3084 |           15.2 |
| RS_self     |   0.66617  | 21.1303 |           42   |
| SMR_self    |   0.252227 | 28.5452 |           21.5 |
| AD_self     |   0.25013  | 28.4957 |           21.3 |

**Importance %** = |coef × std(component)| normalised to 100 — the effective weight of each rating inside the Composite.


**Composite accuracy of the full self-computed pipeline** (ticker_cache only, no MarketSurge inputs) with the production params:

#### In-sample (fit on OLD, 2026-07-24)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8494 |   8.27 | 0.9561 |        17.3 |        29.8 |         63.3 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3358 |  18.13 | 0.5879 |         8.9 |        15.4 |         31.4 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.7802 |       nan   |       nan   |        nan   |         32.1 |        51.7 |             1.93 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7809 |       nan   |       nan   |        nan   |         58.6 |        58.6 |             9.73 |
| Composite (self pipeline)         |   0.7336 |   9.84 | 0.8566 |        23.6 |        35.4 |         61.6 |        nan   |       nan   |           nan    |

#### Out-of-sample (NEW (forward, out-of-sample))

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8336 |   8.27 | 0.9404 |        20.3 |        31.8 |         64.3 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3448 |  17.97 | 0.5952 |         9.4 |        15.9 |         32.1 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8159 |       nan   |       nan   |        nan   |         35   |        56.1 |             1.81 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7832 |       nan   |       nan   |        nan   |         60.2 |        60.2 |             9.45 |
| Composite (self pipeline)         |   0.724  |   9.96 | 0.8575 |        24.1 |        36.5 |         61.4 |        nan   |       nan   |           nan    |

## A. Fit on NEW snapshot (fit, analysis)

### A1. Component models (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Sigmoid on weighted rel-perf sum (opt weights) |    0.8141 |       8.4  |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.5524 |       2.27 |
| EPS      | OLS direct scale (diagnostic)                  |    0.4087 |      16.34 |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6506 |      10.51 |

### A2. Self-computed pipeline vs NEW ground truth (full universe)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8353 |   8.2  | 0.9395 |        20.3 |        32.7 |         65   |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3539 |  17.72 | 0.6042 |        10   |        16.5 |         33.6 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8026 |       nan   |       nan   |        nan   |         33.5 |        54.1 |             1.9  |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7897 |       nan   |       nan   |        nan   |         60.7 |        60.7 |             9.27 |
| Composite (self pipeline)         |   0.7311 |   9.95 | 0.8551 |        23.2 |        34.9 |         61   |        nan   |       nan   |           nan    |

### A3. Detailed per-rating tables

#### RS

| Method                                               |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-----------------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (40/20/20/20 vs SPY, sigmoid)        | 0.7811 |  9.4  | 0.9094 |        15.9 |        26.6 |         58.2 |
| Monotonic weights on absolute returns + pct-rank     | 0.6982 | 11.08 | 0.9237 |        13   |        23.2 |         48.1 |
| Monotonic weights on relative perf vs SPY + pct-rank | 0.6934 | 11.14 | 0.9217 |        13.5 |        24.1 |         49.1 |
| Equal-weight 5-window avg + pct-rank                 | 0.6515 | 12.06 | 0.9009 |        12.5 |        19.3 |         44.4 |
| OLS blend of window returns + pct-rank               | 0.5874 | 12.45 | 0.8816 |        16.5 |        25.9 |         49.6 |
| Sigmoid on weighted rel-perf sum (opt weights)       | 0.8141 |  8.4  | 0.9251 |        19.8 |        33.7 |         65.3 |
| Sigmoid on 40/20/20/20 rel-perf sum (prod-style)     | 0.788  |  9.22 | 0.9139 |        16   |        27.9 |         58.5 |

#### A/D

| Method                                                   |   Exact Acc% |   +/-1 Acc% |   Corr |   MAE(grade pts) |       R2 |    MAE |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------------------|-------------:|------------:|-------:|-----------------:|---------:|-------:|------------:|------------:|-------------:|
| Current formula (65D CMF) + calibrated letters           |         17.6 |        30.3 | 0.3088 |             4.06 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS multi-window accumulation blend + calibrated letters |         28.6 |        47.4 | 0.7476 |             2.17 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS blend numeric (1-13 scale)                           |        nan   |       nan   | 0.7469 |           nan    |   0.5524 |   2.27 |        70.4 |        91.4 |         99.5 |

#### EPS

| Method                                   |      R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-----------------------------------------|--------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (blended growth sigmoid) | -0.3041 | 23.58 | 0.4364 |         7   |        12.3 |         23.6 |
| OLS feature blend + percentile rank      |  0.0024 | 20.27 | 0.6238 |        12.1 |        18.5 |         34.3 |
| OLS direct scale (diagnostic)            |  0.4087 | 16.34 | 0.6403 |        10.3 |        17   |         35.3 |

#### SMR

| Method                                            |   Exact Acc% |   +/-1 Acc% |   Corr |   MAE(grade pts) |       R2 |    MAE |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:--------------------------------------------------|-------------:|------------:|-------:|-----------------:|---------:|-------:|------------:|------------:|-------------:|
| Current formula (ROE-only) + calibrated quintiles |         50.1 |        50.1 | 0.6692 |            11.89 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS 3-pillar blend + calibrated quintiles         |         64.1 |        64.1 | 0.814  |             7.83 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS 3-pillar blend numeric (10-95)                |        nan   |       nan   | 0.8272 |           nan    |   0.3643 |  14.22 |        15.6 |        26.2 |         41.3 |
| OLS 3-pillar blend direct scale (diagnostic)      |        nan   |       nan   | 0.807  |           nan    |   0.6506 |  10.51 |        17.2 |        28.4 |         53.9 |

#### Composite (fit on NEW)

| Method                                     |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| True components, OLS (no group)            | 0.9429 |  4.78 | 0.9715 |        38.1 |        58.9 |         90.8 |
| True components + MS group RS (diagnostic) | 0.9768 |  3.04 | 0.9885 |        57.3 |        79.9 |         99.2 |
| FULL SELF-COMPUTED pipeline (n=3,073)      | 0.6993 | 10.2  | 0.8402 |        18.5 |        30.2 |         58.7 |
| Self-computed + our group RS               | 0.7374 |  9.84 | 0.8588 |        21.3 |        31.6 |         58.1 |

## B. Cross-week validation (fit on NEW -> test on OLD)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8472 |   8.32 | 0.9534 |        17.1 |        29.9 |         62.4 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3204 |  18.26 | 0.5768 |         9.2 |        15.6 |         31.7 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.7485 |       nan   |       nan   |        nan   |         30.3 |        49.4 |             2.06 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7862 |       nan   |       nan   |        nan   |         58.9 |        58.9 |             9.6  |
| Composite (self pipeline)         |   0.7057 |  10.44 | 0.8481 |        22.3 |        33.1 |         58.1 |        nan   |       nan   |           nan    |

## C. Reverse direction (fit on OLD -> test on OLD and NEW)

### C1. Component models fit on OLD (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Sigmoid on weighted rel-perf sum (opt weights) |    0.8413 |       8.56 |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.4947 |       2.37 |
| EPS      | OLS direct scale (diagnostic)                  |    0.3786 |      16.95 |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6644 |      10.36 |

### C2. Self pipeline vs OLD ground truth (in-sample)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8494 |   8.27 | 0.9561 |        17.3 |        29.8 |         63.3 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3358 |  18.13 | 0.5879 |         8.9 |        15.4 |         31.4 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.7802 |       nan   |       nan   |        nan   |         32.1 |        51.7 |             1.93 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7809 |       nan   |       nan   |        nan   |         58.6 |        58.6 |             9.73 |
| Composite (self pipeline)         |   0.7336 |   9.84 | 0.8566 |        23.6 |        35.4 |         61.6 |        nan   |       nan   |           nan    |

### C3. Self pipeline vs NEW ground truth (cross-week)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.8336 |   8.27 | 0.9404 |        20.3 |        31.8 |         64.3 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3448 |  17.97 | 0.5952 |         9.4 |        15.9 |         32.1 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8159 |       nan   |       nan   |        nan   |         35   |        56.1 |             1.81 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7832 |       nan   |       nan   |        nan   |         60.2 |        60.2 |             9.45 |
| Composite (self pipeline)         |   0.724  |   9.96 | 0.8575 |        24.1 |        36.5 |         61.4 |        nan   |       nan   |           nan    |

## D. Production formula parameters (fit on OLD snapshot — marketsurge.csv, as-of 2026-07-24)

### D1. RS — sigmoid monotonic weights

| 1M | 3M | 6M | 9M | 12M |
|---|---|---|---|---|
| 0.0000 | 0.5134 | 0.0505 | 0.3791 | 0.0569 |

### D2. AD OLS feature weights

| Feature                |   OLS_Coef |   Abs_Weight_Pct |
|:-----------------------|-----------:|-----------------:|
| CMF_65D                |  14.7335   |             45.7 |
| CMF_30D                |   6.44615  |             20   |
| CMF_130D               |  -5.42077  |             16.8 |
| HeavyNetRatio_65D      |   1.39981  |              4.3 |
| DnDayVolRatio          |   0.951905 |              3   |
| UpDayVolRatio          |  -0.696924 |              2.2 |
| UpDnVol_65D            |   0.632282 |              2   |
| UpDnVol_130D           |   0.473714 |              1.5 |
| NetHeavyIntensity_30D  |  -0.322009 |              1   |
| VWClsRange_65D         |  -0.269355 |              0.8 |
| Dist_50MA              |   0.196452 |              0.6 |
| Dist_150MA             |   0.163387 |              0.5 |
| Dist_200MA             |  -0.165821 |              0.5 |
| UpDnVol_30D            |  -0.118354 |              0.4 |
| AvgClsRange_65D        |   0.143425 |              0.4 |
| PriceChg_5D            |  -0.055079 |              0.2 |
| NetHeavyDays_65D       |   0.023023 |              0.1 |
| PctOff52WHigh          |  -0.015061 |              0   |
| InstTop5Pct            |  -0.003858 |              0   |
| UpDnVol_10D            |   0.000572 |              0   |
| NetHeavyIntensity_130D |  -0.000359 |              0   |
| NetHeavyIntensity_65D  |   0.003666 |              0   |
| InstAvgChg             |   2e-06    |              0   |

### D2. EPS OLS feature weights

| Feature         |   OLS_Coef |   Abs_Weight_Pct |
|:----------------|-----------:|-----------------:|
| EpsBeatRate     |  24.9909   |             66.6 |
| EPS_NegQRatio   |  -3.80286  |             10.1 |
| ROE             |   2.75876  |              7.3 |
| EpsSurpriseMean |  -1.69323  |              4.5 |
| EPS_LT_Growth   |   1.42791  |              3.8 |
| EPS_Q0_YoY      |   1.34892  |              3.6 |
| EstEPSGrowth_Q  |   0.744264 |              2   |
| EPS_StabilityCV |  -0.463657 |              1.2 |
| EpsRevTrend     |   0.27511  |              0.7 |
| EstEPSGrowth_Y  |   0.043704 |              0.1 |

### D2. SMR OLS feature weights

| Feature           |   OLS_Coef |   Abs_Weight_Pct |
|:------------------|-----------:|-----------------:|
| Sales_LT_Growth   |   3.85825  |             27.4 |
| ROE               |   2.99329  |             21.2 |
| Info_ProfitMargin |   2.87592  |             20.4 |
| Margin_Now        |   2.09399  |             14.9 |
| Margin_Trend      |  -1.04349  |              7.4 |
| Sales_Q0_YoY      |   0.781821 |              5.5 |
| Info_RevGrowth    |   0.446713 |              3.2 |

### D3. Composite combining weights (all components 1-99 scale)

`Comp = -16.777` + 0.3116*EPS_self + 0.6662*RS_self + 0.2522*SMR_self + 0.2501*AD_self

| Component   |   OLS_Coef |     Std |   Importance % |
|:------------|-----------:|--------:|---------------:|
| EPS_self    |   0.311558 | 16.3084 |           15.2 |
| RS_self     |   0.66617  | 21.1303 |           42   |
| SMR_self    |   0.252227 | 28.5452 |           21.5 |
| AD_self     |   0.25013  | 28.4957 |           21.3 |

Importance % = |coef × std| normalised — the effective weight of each rating in the Composite.


**The rows that matter**: the out-of-sample rows above are the honest test of the production params — fit on one week, applied to the other.  A2 shows the fit-on-NEW analysis, C2/C3 the fit-on-OLD analysis; the production file uses the selected snapshot (see `fitted_params.json` → `fit_snapshot`).
