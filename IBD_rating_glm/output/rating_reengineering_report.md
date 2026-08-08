# IBD Rating Reverse-Engineering (Non-ML, ticker_cache only)

**Snapshots**: `marketsuge-8-7-2026.csv` (as-of **2026-08-07**) + `marketsurge.csv` (as-of **2026-07-24**) — different weeks, both used for testing.

**Universe**: new `3,201` | old `3,199` stocks (valid Comp + price parquet + fund json)

> Every model input comes from `ticker_cache` (parquet + fund json) and `IBD Industry Mapping.txt`.  MarketSurge supplies only ground-truth labels.  Price features for each snapshot are computed with history truncated to that snapshot's as-of day (old snapshot file date 2026-07-29, data as-of 2026-07-24).  All methods are transparent linear blends / percentile ranks / constrained scalar-weight fits — no machine learning.

## Executive summary — Composite Rating formula (the filter)

> **Production params: fit on OLD (2026-07-24, `marketsurge.csv`), validated on NEW (forward, out-of-sample) — no look-ahead. Both snapshot fits are archived below.

`Comp Rating = -16.297
 + 0.3741 × EPS_self + 0.5092 × RS_self + 0.2043 × SMR_self + 0.2018 × AD_self + 0.1434 × GroupRS_self`

| Component    |   OLS_Coef |     Std |   Importance % |
|:-------------|-----------:|--------:|---------------:|
| EPS_self     |   0.374075 | 17.2799 |           18.4 |
| RS_self      |   0.509209 | 25.4424 |           36.9 |
| SMR_self     |   0.204329 | 28.4195 |           16.5 |
| AD_self      |   0.201754 | 28.5365 |           16.4 |
| GroupRS_self |   0.143382 | 29.0153 |           11.8 |

**Importance %** = |coef × std(component)| normalised to 100 — the effective weight of each rating inside the Composite.


**Composite accuracy of the full self-computed pipeline** (ticker_cache only, no MarketSurge inputs) with the production params:

#### In-sample (fit on OLD, 2026-07-24)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9248 |   5.13 | 0.9664 |        34.5 |        56.3 |         90.9 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3783 |  17.44 | 0.6216 |         9.8 |        16.7 |         33.4 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8335 |       nan   |       nan   |        nan   |         36.1 |        58.4 |             1.62 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7949 |       nan   |       nan   |        nan   |         61.2 |        61.2 |             9.16 |
| Composite (self pipeline)         |   0.7884 |   8.67 | 0.888  |        28   |        40.9 |         67   |        nan   |       nan   |           nan    |

#### Out-of-sample (NEW (forward, out-of-sample))

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9116 |   5.22 | 0.9551 |        36.9 |        59   |         89.1 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.391  |  17.22 | 0.6316 |        10.2 |        17.1 |         34.6 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8506 |       nan   |       nan   |        nan   |         38.4 |        59.7 |             1.61 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7982 |       nan   |       nan   |        nan   |         62.3 |        62.3 |             8.93 |
| Composite (self pipeline)         |   0.7659 |   9.07 | 0.8853 |        26.8 |        40.8 |         66.8 |        nan   |       nan   |           nan    |

## A. Fit on NEW snapshot (fit, analysis)

### A1. Component models (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Dual-momentum sigmoid (rel-perf + 200MA trend) |    0.9264 |       5.13 |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.6791 |       1.91 |
| EPS      | OLS direct scale (diagnostic)                  |    0.4507 |      15.63 |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6848 |       9.94 |

### A2. Self-computed pipeline vs NEW ground truth (full universe)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9155 |   5.08 | 0.9568 |        38   |        59.9 |         90.9 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.4162 |  16.76 | 0.6514 |        10.2 |        18.6 |         36.3 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8565 |       nan   |       nan   |        nan   |         37.5 |        59.6 |             1.58 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.8004 |       nan   |       nan   |        nan   |         62.6 |        62.6 |             8.85 |
| Composite (self pipeline)         |   0.7913 |   8.68 | 0.8896 |        26.6 |        40.4 |         67.7 |        nan   |       nan   |           nan    |

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
| Current formula (65D CMF) + calibrated letters           |         19.5 |        32.6 | 0.3815 |              3.8 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS multi-window accumulation blend + calibrated letters |         34.4 |        57.3 | 0.8337 |              1.7 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS blend numeric (1-13 scale)                           |        nan   |       nan   | 0.8322 |            nan   |   0.6791 |   1.91 |        79.2 |        93.8 |         99.7 |

#### EPS

| Method                                   |      R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-----------------------------------------|--------:|------:|-------:|------------:|------------:|-------------:|
| Current formula (blended growth sigmoid) | -0.3041 | 23.58 | 0.4364 |         7   |        12.3 |         23.6 |
| OLS feature blend + percentile rank      |  0.1003 | 19.11 | 0.6636 |        13.5 |        20.5 |         37.4 |
| OLS direct scale (diagnostic)            |  0.4507 | 15.63 | 0.672  |        10.7 |        19.1 |         37.6 |

#### SMR

| Method                                            |   Exact Acc% |   +/-1 Acc% |   Corr |   MAE(grade pts) |       R2 |    MAE |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:--------------------------------------------------|-------------:|------------:|-------:|-----------------:|---------:|-------:|------------:|------------:|-------------:|
| Current formula (ROE-only) + calibrated quintiles |         50.1 |        50.1 | 0.6692 |            11.89 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS 3-pillar blend + calibrated quintiles         |         67.5 |        67.5 | 0.8347 |             7.07 | nan      | nan    |       nan   |       nan   |        nan   |
| OLS 3-pillar blend numeric (10-95)                |        nan   |       nan   | 0.8427 |           nan    |   0.4077 |  13.7  |        14.5 |        26.2 |         42.2 |
| OLS 3-pillar blend direct scale (diagnostic)      |        nan   |       nan   | 0.8278 |           nan    |   0.6848 |   9.94 |        18.5 |        31.2 |         58   |

#### Composite (fit on NEW)

| Method                                     |     R2 |   MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |
|:-------------------------------------------|-------:|------:|-------:|------------:|------------:|-------------:|
| True components, OLS (no group)            | 0.9429 |  4.78 | 0.9715 |        38.1 |        58.9 |         90.8 |
| True components + MS group RS (diagnostic) | 0.9768 |  3.04 | 0.9885 |        57.3 |        79.9 |         99.2 |
| FULL SELF-COMPUTED pipeline (n=3,082)      | 0.7843 |  8.93 | 0.8869 |        22.9 |        35.5 |         64   |
| Self-computed + our group RS               | 0.8012 |  8.76 | 0.8965 |        23.1 |        36.6 |         63.6 |

## B. Cross-week validation (fit on NEW -> test on OLD)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9242 |   5.24 | 0.9674 |        32.8 |        54.5 |         91.2 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3683 |  17.5  | 0.6148 |        10.2 |        17.2 |         35   |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.817  |       nan   |       nan   |        nan   |         35.4 |        56.2 |             1.7  |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7949 |       nan   |       nan   |        nan   |         61.1 |        61.1 |             9.17 |
| Composite (self pipeline)         |   0.7609 |   9.47 | 0.8831 |        22.8 |        35.5 |         63.5 |        nan   |       nan   |           nan    |

## C. Reverse direction (fit on OLD -> test on OLD and NEW)

### C1. Component models fit on OLD (test set, 20% holdout)

| Rating   | Best test method                               |   Test R2 |   Test MAE |
|:---------|:-----------------------------------------------|----------:|-----------:|
| RS       | Dual-momentum sigmoid (rel-perf + 200MA trend) |    0.913  |       5.28 |
| A/D      | OLS blend numeric (1-13 scale)                 |    0.6122 |       2.09 |
| EPS      | OLS direct scale (diagnostic)                  |    0.4141 |      16.24 |
| SMR      | OLS 3-pillar blend direct scale (diagnostic)   |    0.6989 |       9.81 |

### C2. Self pipeline vs OLD ground truth (in-sample)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9248 |   5.13 | 0.9664 |        34.5 |        56.3 |         90.9 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.3783 |  17.44 | 0.6216 |         9.8 |        16.7 |         33.4 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8335 |       nan   |       nan   |        nan   |         36.1 |        58.4 |             1.62 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7949 |       nan   |       nan   |        nan   |         61.2 |        61.2 |             9.16 |
| Composite (self pipeline)         |   0.7884 |   8.67 | 0.888  |        28   |        40.9 |         67   |        nan   |       nan   |           nan    |

### C3. Self pipeline vs NEW ground truth (cross-week)

| Method                            |       R2 |    MAE |   Corr |   +/-3 Acc% |   +/-5 Acc% |   +/-10 Acc% |   Exact Acc% |   +/-1 Acc% |   MAE(grade pts) |
|:----------------------------------|---------:|-------:|-------:|------------:|------------:|-------------:|-------------:|------------:|-----------------:|
| RS Rating (self pipeline)         |   0.9116 |   5.22 | 0.9551 |        36.9 |        59   |         89.1 |        nan   |       nan   |           nan    |
| EPS Rating (self pipeline)        |   0.391  |  17.22 | 0.6316 |        10.2 |        17.1 |         34.6 |        nan   |       nan   |           nan    |
| A/D Rating (self pipeline, A+..E) | nan      | nan    | 0.8506 |       nan   |       nan   |        nan   |         38.4 |        59.7 |             1.61 |
| SMR Rating (self pipeline, A-E)   | nan      | nan    | 0.7982 |       nan   |       nan   |        nan   |         62.3 |        62.3 |             8.93 |
| Composite (self pipeline)         |   0.7659 |   9.07 | 0.8853 |        26.8 |        40.8 |         66.8 |        nan   |       nan   |           nan    |

## D. Production formula parameters (fit on OLD snapshot — marketsurge.csv, as-of 2026-07-24)

### D1. RS — dual_sigmoid monotonic weights

| 1M | 3M | 6M | 9M | 12M |
|---|---|---|---|---|
| 0.0673 | 0.5565 | 0.0267 | 0.1358 | 0.2136 |

### D2. AD OLS feature weights

| Feature                |   OLS_Coef |   Abs_Weight_Pct |
|:-----------------------|-----------:|-----------------:|
| CMF_250D               |  -3.65789  |             16.5 |
| HeavyNetRatio_250D     |  -3.50214  |             15.8 |
| CMF_65D                |   3.48361  |             15.7 |
| CMF_10D                |   3.12341  |             14.1 |
| NetHeavyIntensity_5D   |  -1.26497  |              5.7 |
| HeavyNetRatio_65D      |   1.15393  |              5.2 |
| HeavyNetRatio_30D      |   1.04113  |              4.7 |
| CMF_5D                 |  -1.04183  |              4.7 |
| HeavyNetRatio_10D      |   0.552583 |              2.5 |
| UpDnVol_65D            |   0.396185 |              1.8 |
| AvgClsRange_250D       |  -0.331487 |              1.5 |
| NetHeavyIntensity_10D  |   0.340307 |              1.5 |
| UpDnVol_130D           |   0.195004 |              0.9 |
| VolSpike_5D            |  -0.174975 |              0.8 |
| Dist_200MA             |  -0.169949 |              0.8 |
| Dist_150MA             |   0.182711 |              0.8 |
| UpDnVol_10D            |   0.149055 |              0.7 |
| Dist_50MA              |   0.153424 |              0.7 |
| NetHeavyDays_5D        |   0.145659 |              0.7 |
| AvgClsRange_30D        |   0.139471 |              0.6 |
| UpDnVol_250D           |   0.134571 |              0.6 |
| Dist_21MA              |   0.116047 |              0.5 |
| HeavyNetRatio_5D       |   0.079808 |              0.4 |
| NetHeavyIntensity_30D  |  -0.081374 |              0.4 |
| AvgClsRange_130D       |   0.080836 |              0.4 |
| AvgClsRange_65D        |   0.094646 |              0.4 |
| PriceChg_10D           |  -0.056897 |              0.3 |
| PriceChg_30D           |  -0.035896 |              0.2 |
| UpDnVol_5D             |  -0.054287 |              0.2 |
| NetHeavyDays_10D       |  -0.043429 |              0.2 |
| PriceChg_5D            |   0.018341 |              0.1 |
| NetHeavyIntensity_130D |  -0.014284 |              0.1 |
| AvgClsRange_5D         |   0.015446 |              0.1 |
| Dist_10MA              |  -0.02698  |              0.1 |
| AvgClsRange_10D        |   0.014254 |              0.1 |
| NetHeavyDays_65D       |   0.017548 |              0.1 |
| NetHeavyDays_130D      |   0.020454 |              0.1 |
| NetHeavyDays_250D      |  -0.016669 |              0.1 |
| NetHeavyDays_30D       |  -0.005616 |              0   |
| PriceChg_130D          |   0.002391 |              0   |
| PriceChg_250D          |   0.00071  |              0   |
| PctOff52WHigh          |  -0.006649 |              0   |
| UpDnVol_30D            |  -0.005968 |              0   |
| NetHeavyIntensity_250D |  -0.000663 |              0   |
| NetHeavyIntensity_65D  |  -0.007826 |              0   |
| PriceChg_65D           |   0.002724 |              0   |

### D2. EPS OLS feature weights

| Feature           |   OLS_Coef |   Abs_Weight_Pct |
|:------------------|-----------:|-----------------:|
| EpsBeatRate       |  22.2658   |             44.4 |
| RecScore          |   5.21216  |             10.4 |
| Info_ROA          |   3.29274  |              6.6 |
| EPS_NegQRatio     |  -3.16246  |              6.3 |
| GrossMargin_Now   |   2.24798  |              4.5 |
| EpsSurpriseMean   |  -1.36523  |              2.7 |
| Info_GrossMargin  |  -1.30711  |              2.6 |
| Info_ProfitMargin |   1.19524  |              2.4 |
| EPS_LT_Growth     |   1.22191  |              2.4 |
| EPS_Q0_YoY        |   1.14742  |              2.3 |
| GrossMargin_Trend |   0.920681 |              1.8 |
| Info_FwdPE        |   0.860028 |              1.7 |
| RevEstGrowth_Y    |   0.785134 |              1.6 |
| ROE               |   0.705483 |              1.4 |
| Info_OCFYield     |  -0.649814 |              1.3 |
| Info_TotalCashPS  |   0.597587 |              1.2 |
| Info_TargetUpside |  -0.552551 |              1.1 |
| EPS_StabilityCV   |  -0.429898 |              0.9 |
| EstEPSGrowth_Q    |   0.411721 |              0.8 |
| Info_FCFYield     |  -0.275817 |              0.6 |
| RevEstGrowth_Q    |   0.30572  |              0.6 |
| Info_NumAnalysts  |   0.316129 |              0.6 |
| EpsRevTrend       |   0.260213 |              0.5 |
| Info_DebtEquity   |  -0.210251 |              0.4 |
| PTChg90           |  -0.15953  |              0.3 |
| Info_EPSQGrowth   |   0.111877 |              0.2 |
| EstEPSGrowth_Y    |  -0.110365 |              0.2 |
| Info_CurrentRatio |  -0.063785 |              0.1 |
| Info_OpMargin     |  -0.001617 |              0   |

### D2. SMR OLS feature weights

| Feature             |   OLS_Coef |   Abs_Weight_Pct |
|:--------------------|-----------:|-----------------:|
| Sales_LT_Growth     |   3.70355  |             13.8 |
| Info_ProfitMargin   |   2.94899  |             11   |
| Info_PriceBook      |   2.75395  |             10.2 |
| ROE                 |   2.16451  |              8   |
| Info_CurrentRatio   |  -2.08525  |              7.8 |
| Margin_Now          |   1.78779  |              6.6 |
| Info_QuickRatio     |   1.58803  |              5.9 |
| Info_ROA            |   1.38732  |              5.2 |
| Accrual_Q           |  -1.27485  |              4.7 |
| Info_DebtEquity     |  -0.949737 |              3.5 |
| Margin_Trend        |  -0.897889 |              3.3 |
| Info_GrossMargin    |  -0.896778 |              3.3 |
| Info_EarningsGrowth |  -0.812437 |              3   |
| Info_OpMargin       |   0.742334 |              2.8 |
| Sales_Q0_YoY        |   0.724159 |              2.7 |
| Info_EPSQGrowth     |   0.6955   |              2.6 |
| Info_FCFYield       |  -0.707853 |              2.6 |
| OCF_NI              |  -0.542949 |              2   |
| Info_RevGrowth      |   0.175391 |              0.7 |
| Info_OCFYield       |  -0.061297 |              0.2 |

### D3. Composite combining weights (all components 1-99 scale)

`Comp = -16.297` + 0.3741*EPS_self + 0.5092*RS_self + 0.2043*SMR_self + 0.2018*AD_self + 0.1434*GroupRS_self

| Component    |   OLS_Coef |     Std |   Importance % |
|:-------------|-----------:|--------:|---------------:|
| EPS_self     |   0.374075 | 17.2799 |           18.4 |
| RS_self      |   0.509209 | 25.4424 |           36.9 |
| SMR_self     |   0.204329 | 28.4195 |           16.5 |
| AD_self      |   0.201754 | 28.5365 |           16.4 |
| GroupRS_self |   0.143382 | 29.0153 |           11.8 |

Importance % = |coef × std| normalised — the effective weight of each rating in the Composite.


**The rows that matter**: the out-of-sample rows above are the honest test of the production params — fit on one week, applied to the other.  A2 shows the fit-on-NEW analysis, C2/C3 the fit-on-OLD analysis; the production file uses the selected snapshot (see `fitted_params.json` → `fit_snapshot`).
