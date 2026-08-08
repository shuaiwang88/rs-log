# IBD Rating Reverse-Engineering (Non-ML, ticker_cache only)

**Snapshots**: `marketsuge-8-7-2026.csv` (as-of **2026-08-07**) + `marketsurge.csv` (as-of **2026-07-24**) — different weeks, both used for testing.

**Universe**: new `3,201` | old `3,199` stocks (valid Comp + price parquet + fund json)

> Every model input comes from `ticker_cache` (parquet + fund json) and `IBD Industry Mapping.txt`.  MarketSurge supplies only ground-truth labels.  Price features for each snapshot are computed with history truncated to that snapshot's as-of day (old snapshot file date 2026-07-29, data as-of 2026-07-24).  All methods are transparent linear blends / percentile ranks / constrained scalar-weight fits — no machine learning.

## Executive summary — Composite Rating formula (the filter)

> **Production params: fit on OLD (2026-07-24, `marketsurge.csv`), validated on NEW (forward, out-of-sample) — no look-ahead. Both snapshot fits are archived below.

`Comp Rating = -15.961
 + 0.3590 × EPS_self + 0.5124 × RS_self + 0.2145 × SMR_self + 0.2004 × AD_self + 0.1425 × GroupRS_self`

| Component    |   OLS_Coef |     Std |   Importance % |
|:-------------|-----------:|--------:|---------------:|
| EPS_self     |   0.358953 | 17.1247 |           17.5 |
| RS_self      |   0.512389 | 25.4487 |           37.1 |
| SMR_self     |   0.214503 | 28.4828 |           17.4 |
| AD_self      |   0.200356 | 28.5609 |           16.3 |
| GroupRS_self |   0.142548 | 29.0175 |           11.8 |

**Importance %** = |coef × std(component)| normalised to 100 — the effective weight of each rating inside the Composite.


**Composite accuracy of the full self-computed pipeline** (ticker_cache only, no MarketSurge inputs) with the production params:

#### In-sample (fit on OLD, 2026-07-24)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9248 |   5.13 | 0.9664 |        34.5 |        56.3 |         90.9 |          nan |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3762 |  17.5  | 0.6196 |        10.3 |        16.8 |         33.1 |          nan |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8334 |       nan   |       nan   |        nan   |           36 |        57.9 |             1.63 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7944 |       nan   |       nan   |        nan   |           61 |        61   |             9.21 |
| Composite (self pipeline)         |   0.7881 |   8.66 | 0.8878 |        28.2 |        40.7 |         67   |          nan |       nan   |           nan    |

#### Out-of-sample (NEW (forward, out-of-sample))

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9116 |   5.22 | 0.9551 |        36.9 |        59   |         89.1 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3873 |  17.33 | 0.6284 |        10.6 |        17.1 |         33.1 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8413 |       nan   |       nan   |        nan   |         37.4 |        58.5 |             1.66 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7973 |       nan   |       nan   |        nan   |         62.1 |        62.1 |             9    |
| Composite (self pipeline)         |   0.7631 |   9.13 | 0.884  |        27.1 |        40.8 |         66.4 |        nan   |       nan   |           nan    |

## A. Fit on NEW snapshot (fit, analysis)

### A1. Component models (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Dual-momentum sigmoid (rel-perf + 200MA trend) |    0.9264 |       5.13 |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.6996 |       1.94 |
| EPS      | OLS direct scale (diagnostic)                  |    0.4371 |      15.8  |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6807 |      10    |

### A2. Self-computed pipeline vs NEW ground truth (full universe)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9155 |   5.08 | 0.9568 |        38   |        59.9 |         90.9 |        nan   |       nan   |            nan   |
| EPS Rating (self pipeline)        |   0.4132 |  16.88 | 0.6487 |        10.8 |        18   |         35.4 |        nan   |       nan   |            nan   |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8537 |       nan   |       nan   |        nan   |         37.3 |        58.5 |              1.6 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7998 |       nan   |       nan   |        nan   |         62.4 |        62.4 |              8.9 |
| Composite (self pipeline)         |   0.7902 |   8.72 | 0.889  |        25.9 |        39.8 |         68.1 |        nan   |       nan   |            nan   |

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
| Dual-momentum sigmoid (rel-perf + 200MA trend)       | 0.9264 |  5.13 | 0.9626 |        36.1 |        60   |         89   |

#### A/D

| Method                                                   |   Exact Acc% |   +/-1 Acc% |   Corr |   MAE(grade pts) |       R2 |    MAE |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:---------------------------------------------------------|-------------:|------------:|-------:|-----------------:|---------:|-------:|------------:|------------:|-------------:|
| Current formula (65D CMF) + calibrated letters           |         19.5 |        30.7 | 0.3506 |             3.88 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS multi-window accumulation blend + calibrated letters |         35.3 |        59.5 | 0.8567 |             1.59 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS blend numeric (1-13 scale)                           |        nan   |       nan   | 0.8494 |           nan    |   0.6996 |   1.94 |        78.7 |        95.3 |         99.8 |

#### EPS

| Method                                   |      R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-----------------------------------------|--------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (blended growth sigmoid) | -0.3041 | 23.58 | 0.4364 |         7   |        12.3 |         23.6 |
| OLS feature blend + percentile rank      |  0.0599 | 19.51 | 0.6523 |        12.3 |        20.7 |         37.8 |
| OLS direct scale (diagnostic)            |  0.4371 | 15.8  | 0.6622 |        11.7 |        18.9 |         37   |

#### SMR

| Method                                            |   Exact Acc% |   +/-1 Acc% |   Corr |   MAE(grade pts) |       R2 |   MAE |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:--------------------------------------------------|-------------:|------------:|-------:|-----------------:|---------:|------:|------------:|------------:|-------------:|
| Current formula (ROE-only) + calibrated quintiles |         50.1 |        50.1 | 0.6692 |            11.89 | nan      | nan   |       nan   |       nan   |        nan   |
| OLS 3-pillar blend + calibrated quintiles         |         67   |        67   | 0.8308 |             7.18 | nan      | nan   |       nan   |       nan   |        nan   |
| OLS 3-pillar blend numeric (10-95)                |        nan   |       nan   | 0.8419 |           nan    |   0.4067 |  13.7 |        15.4 |        25.1 |         42.7 |
| OLS 3-pillar blend direct scale (diagnostic)      |        nan   |       nan   | 0.8253 |           nan    |   0.6807 |  10   |        17.1 |        29.8 |         56.2 |

#### Composite (fit on NEW)

| Method                                     |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| True components, OLS (no group)            | 0.9429 |  4.78 | 0.9715 |        38.1 |        58.9 |         90.8 |
| True components + MS group RS (diagnostic) | 0.9768 |  3.04 | 0.9885 |        57.3 |        79.9 |         99.2 |
| FULL SELF-COMPUTED pipeline (n=3,073)      | 0.7503 |  9.29 | 0.868  |        21.3 |        35.4 |         62.9 |
| Self-computed + our group RS               | 0.7704 |  9.12 | 0.8777 |        22.1 |        35   |         63.7 |

## B. Cross-week validation (fit on NEW -> test on OLD)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9242 |   5.24 | 0.9674 |        32.8 |        54.5 |         91.2 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3681 |  17.53 | 0.6139 |        10.6 |        17.5 |         34.2 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8098 |       nan   |       nan   |        nan   |         34.7 |        54.7 |             1.75 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7944 |       nan   |       nan   |        nan   |         60.9 |        60.9 |             9.21 |
| Composite (self pipeline)         |   0.7616 |   9.42 | 0.8832 |        22.6 |        35.6 |         63.9 |        nan   |       nan   |           nan    |

## C. Reverse direction (fit on OLD -> test on OLD and NEW)

### C1. Component models fit on OLD (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Dual-momentum sigmoid (rel-perf + 200MA trend) |    0.913  |       5.28 |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.569  |       2.21 |
| EPS      | OLS direct scale (diagnostic)                  |    0.406  |      16.29 |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6932 |       9.86 |

### C2. Self pipeline vs OLD ground truth (in-sample)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9248 |   5.13 | 0.9664 |        34.5 |        56.3 |         90.9 |          nan |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3762 |  17.5  | 0.6196 |        10.3 |        16.8 |         33.1 |          nan |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8334 |       nan   |       nan   |        nan   |           36 |        57.9 |             1.63 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7944 |       nan   |       nan   |        nan   |           61 |        61   |             9.21 |
| Composite (self pipeline)         |   0.7881 |   8.66 | 0.8878 |        28.2 |        40.7 |         67   |          nan |       nan   |           nan    |

### C3. Self pipeline vs NEW ground truth (cross-week)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9116 |   5.22 | 0.9551 |        36.9 |        59   |         89.1 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3873 |  17.33 | 0.6284 |        10.6 |        17.1 |         33.1 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8413 |       nan   |       nan   |        nan   |         37.4 |        58.5 |             1.66 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7973 |       nan   |       nan   |        nan   |         62.1 |        62.1 |             9    |
| Composite (self pipeline)         |   0.7631 |   9.13 | 0.884  |        27.1 |        40.8 |         66.4 |        nan   |       nan   |           nan    |

## D. Production formula parameters (fit on OLD snapshot — marketsurge.csv, as-of 2026-07-24)

### D1. RS — dual_sigmoid monotonic weights

| 1M | 3M | 6M | 9M | 12M |
|---|---|---|---|---|
| 0.0673 | 0.5565 | 0.0267 | 0.1358 | 0.2136 |

### D2. AD OLS feature weights

| Feature                |   OLS_Coef |   Abs_Weight_Pct |
|:-----------------------|-----------:|-----------------:|
| CMF_130D               |  83.5542   |             36.5 |
| CMF_250D               | -79.586    |             34.7 |
| CMF_65D                | -27.5924   |             12   |
| CMF_30D                |  24.5062   |             10.7 |
| CMF_10D                |   3.22193  |              1.4 |
| VWClsRange_130D        |  -1.64169  |              0.7 |
| VWClsRange_250D        |   1.47944  |              0.6 |
| UpDayVolRatio          |  -1.12167  |              0.5 |
| HeavyNetRatio_65D      |   0.827363 |              0.4 |
| HeavyNetRatio_30D      |   0.84495  |              0.4 |
| DnDayVolRatio          |   0.880678 |              0.4 |
| VWClsRange_65D         |   0.592348 |              0.3 |
| UpDnVol_65D            |   0.723427 |              0.3 |
| VWClsRange_30D         |  -0.463057 |              0.2 |
| Dist_21MA              |   0.123547 |              0.1 |
| AvgClsRange_30D        |   0.122357 |              0.1 |
| AvgClsRange_65D        |   0.116832 |              0.1 |
| NetHeavyIntensity_10D  |   0.131883 |              0.1 |
| Dist_50MA              |   0.153413 |              0.1 |
| Dist_150MA             |   0.19495  |              0.1 |
| Dist_200MA             |  -0.181922 |              0.1 |
| AvgClsRange_250D       |  -0.339974 |              0.1 |
| UpDnVol_10D            |  -0.001124 |              0   |
| AvgClsRange_130D       |   0.100101 |              0   |
| PriceChg_10D           |  -0.034948 |              0   |
| PriceChg_30D           |  -0.03981  |              0   |
| PriceChg_65D           |   0.002593 |              0   |
| PriceChg_130D          |   0.002873 |              0   |
| NetHeavyIntensity_250D |  -0.00021  |              0   |
| InstAvgChg             |   2e-06    |              0   |
| NetHeavyDays_130D      |  -0.014848 |              0   |
| NetHeavyDays_30D       |   0.028679 |              0   |
| UpDnVol_250D           |   0.111933 |              0   |
| UpDnVol_5D             |  -0        |              0   |
| Dist_10MA              |  -0.029005 |              0   |
| UpDnVol_30D            |  -0.07     |              0   |
| InstTop5Pct            |  -0.003863 |              0   |
| PriceChg_5D            |   0.000388 |              0   |
| PctOff52WHigh          |  -0.00367  |              0   |
| NetHeavyIntensity_30D  |  -0.04953  |              0   |
| NetHeavyIntensity_130D |  -0.008526 |              0   |
| NetHeavyIntensity_65D  |  -0.0339   |              0   |
| NetHeavyDays_65D       |   0.021555 |              0   |
| UpDnVol_130D           |   0.11087  |              0   |
| PriceChg_250D          |   0.000711 |              0   |

### D2. EPS OLS feature weights

| Feature           |   OLS_Coef |   Abs_Weight_Pct |
|:------------------|-----------:|-----------------:|
| EpsBeatRate       |  22.8157   |             50.9 |
| EPS_NegQRatio     |  -3.18479  |              7.1 |
| Info_ROA          |   3.11191  |              6.9 |
| GrossMargin_Now   |   2.25857  |              5   |
| EPS_LT_Growth     |   1.27787  |              2.9 |
| EpsSurpriseMean   |  -1.24819  |              2.8 |
| Info_GrossMargin  |  -1.19965  |              2.7 |
| Info_ProfitMargin |   1.22749  |              2.7 |
| EPS_Q0_YoY        |   1.13128  |              2.5 |
| GrossMargin_Trend |   0.988114 |              2.2 |
| Info_FwdPE        |   0.9288   |              2.1 |
| ROE               |   0.817988 |              1.8 |
| Info_OCFYield     |  -0.728059 |              1.6 |
| Info_TotalCashPS  |   0.660436 |              1.5 |
| EstEPSGrowth_Q    |   0.622974 |              1.4 |
| Info_NumAnalysts  |   0.518377 |              1.2 |
| EPS_StabilityCV   |  -0.459421 |              1   |
| Info_FCFYield     |  -0.435099 |              1   |
| EpsRevTrend       |   0.316461 |              0.7 |
| Info_DebtEquity   |  -0.332532 |              0.7 |
| Info_EPSQGrowth   |   0.20265  |              0.5 |
| Info_CurrentRatio |  -0.203734 |              0.5 |
| Info_OpMargin     |   0.073412 |              0.2 |
| Info_TargetUpside |   0.04703  |              0.1 |
| EstEPSGrowth_Y    |   0.000346 |              0   |

### D2. SMR OLS feature weights

| Feature             |   OLS_Coef |   Abs_Weight_Pct |
|:--------------------|-----------:|-----------------:|
| Sales_LT_Growth     |   3.75236  |             14.5 |
| Info_ProfitMargin   |   2.97305  |             11.5 |
| Info_PriceBook      |   2.76923  |             10.7 |
| Info_CurrentRatio   |  -2.69413  |             10.4 |
| ROE                 |   2.11114  |              8.1 |
| Info_QuickRatio     |   2.05492  |              7.9 |
| Margin_Now          |   1.50764  |              5.8 |
| Info_ROA            |   1.39651  |              5.4 |
| Info_DebtEquity     |  -0.945913 |              3.6 |
| Margin_Trend        |  -0.929496 |              3.6 |
| Info_EarningsGrowth |  -0.827474 |              3.2 |
| Info_OpMargin       |   0.83831  |              3.2 |
| Info_GrossMargin    |  -0.817477 |              3.2 |
| Info_EPSQGrowth     |   0.727644 |              2.8 |
| Sales_Q0_YoY        |   0.692325 |              2.7 |
| Info_FCFYield       |  -0.637862 |              2.5 |
| Info_RevGrowth      |   0.19904  |              0.8 |
| Info_OCFYield       |   0.057295 |              0.2 |

### D3. Composite combining weights (all components 1-99 scale)

`Comp = -15.961` + 0.3590*EPS_self + 0.5124*RS_self + 0.2145*SMR_self + 0.2004*AD_self + 0.1425*GroupRS_self

| Component    |   OLS_Coef |     Std |   Importance % |
|:-------------|-----------:|--------:|---------------:|
| EPS_self     |   0.358953 | 17.1247 |           17.5 |
| RS_self      |   0.512389 | 25.4487 |           37.1 |
| SMR_self     |   0.214503 | 28.4828 |           17.4 |
| AD_self      |   0.200356 | 28.5609 |           16.3 |
| GroupRS_self |   0.142548 | 29.0175 |           11.8 |

Importance % = |coef × std| normalised — the effective weight of each rating in the Composite.


**The rows that matter**: the out-of-sample rows above are the honest test of the production params — fit on one week, applied to the other.  A2 shows the fit-on-NEW analysis, C2/C3 the fit-on-OLD analysis; the production file uses the selected snapshot (see `fitted_params.json` → `fit_snapshot`).
