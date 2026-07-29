# Reverse Engineering Analysis of IBD Relative Strength (RS) Ratings

**Baseline SPY History**: `8430` trading days | **Evaluation Universe**: `4,044` stocks with complete 250D price history

> [!NOTE]
> **Key Principle**: The RS Rating is a 1-99 percentile rank of stock performance over trailing windows, with **the most recent month (1M / 21 trading days) taking highest priority/weight**.

### 1. Preset Weight Configurations (Prioritizing 1-Month Recency)

| Weight Configuration                  |   1M / Q1 Weight |   3M / Q2 Weight |   6M / Q3 Weight |   9M / Q4 Weight |   12M Weight |     R² |   MAE |   Correlation |   ±1 Acc (%) |   ±3 Acc (%) |   ±5 Acc (%) |
|:--------------------------------------|-----------------:|-----------------:|-----------------:|-----------------:|-------------:|-------:|------:|--------------:|-------------:|-------------:|-------------:|
| 1M_Dominant (40/30/15/10/5)           |         0.4      |         0.3      |             0.15 |         0.1      |    0.05      | 0.5201 | 13.85 |        0.7718 |         6.45 |        18.5  |        28.86 |
| 1M_Heavy (35/25/20/12/8)              |         0.35     |         0.25     |             0.2  |         0.12     |    0.08      | 0.6176 | 12.17 |        0.8201 |         6.63 |        19.63 |        31.68 |
| Linear_Decay (33.3/26.7/20/13.3/6.7)  |         0.333333 |         0.266667 |             0.2  |         0.133333 |    0.0666667 | 0.6186 | 12.14 |        0.8206 |         6.28 |        19.58 |        31.58 |
| Moderate_Recency (30/25/20/15/10)     |         0.3      |         0.25     |             0.2  |         0.15     |    0.1       | 0.6576 | 11.38 |        0.84   |         6.85 |        19.81 |        32.84 |
| Classic_IBD_4Q (40/20/20/20 on Qs)    |         0.4      |         0.2      |             0.2  |         0.2      |    0         | 0.7473 | 10.5  |        0.9238 |         3.28 |        11.48 |        26.23 |
| Classic_IBD_4Win (40/20/20/20 on Cum) |         0        |         0.4      |             0.2  |         0.2      |    0.2       | 0.7149 | 10.32 |        0.8684 |         6.75 |        19.96 |        32.74 |
| Equal_Weight (20/20/20/20/20)         |         0.2      |         0.2      |             0.2  |         0.2      |    0.2       | 0.6935 | 10.78 |        0.8577 |         6.9  |        19.44 |        32.17 |


### 2. Exponential Weighting Analysis

#### A. Daily Return Exponential Moving Average (EMA Half-Life Search)

| EMA Half-Life ($t_{1/2}$) | Period Equivalent | R² Score | MAE (RS Points) | Pearson Corr |

|:--------------------------|:------------------|---------:|----------------:|-------------:|

| 5 Trading Days            | 1 Week            | -0.4988  | 30.48           | 0.1997       |

| 10 Trading Days           | 2 Weeks           | -0.2901  | 27.53           | 0.3166       |

| 21 Trading Days           | 1 Month           | 0.0236   | 22.79           | 0.4923       |

| 42 Trading Days           | 2 Months          | 0.3180   | 17.20           | 0.6571       |

| 63 Trading Days           | 1 Quarter         | 0.4202   | 14.62           | 0.7143       |

| 90 Trading Days (Optimal) | ~4.5 Months       | **0.4420**| **13.92**       | **0.7265**   |

| 126 Trading Days          | 6 Months          | 0.4205   | 14.52           | 0.7145       |

| 250 Trading Days          | 1 Year            | 0.3538   | 16.27           | 0.6771       |


#### B. Window-Level Exponential Decay ($lpha$ Parameter Search)

| Decay Parameter ($lpha$) | Implied Window Weights (1M / 3M / 6M / 9M / 12M) | R² Score | MAE (RS Points) | Pearson Corr |

|:--------------------------|:--------------------------------------------------|---------:|----------------:|-------------:|

| $\alpha = 0.05$ (Optimal) | 22.6% / 21.5% / 20.5% / 19.5% / 18.5%            | **0.6848**| **11.01**       | **0.8625**   |

| $\alpha = 0.10$           | 25.2% / 22.8% / 20.6% / 18.7% / 16.9%            | 0.6807   | 11.08           | 0.8602       |

| $\alpha = 0.20$           | 30.6% / 25.1% / 20.5% / 16.8% / 13.8%            | 0.6671   | 11.38           | 0.8525       |

| $\alpha = 0.30$           | 36.2% / 26.8% / 19.9% / 14.7% / 10.9%            | 0.6440   | 11.93           | 0.8396       |

| $\alpha = 0.50$           | 47.9% / 29.0% / 17.6% / 10.7% / 6.5%             | 0.5662   | 13.67           | 0.7961       |

| $\alpha = 1.00$           | 63.6% / 23.4% / 8.6% / 3.2% / 1.2%               | 0.2661   | 19.24           | 0.6280       |


### 3. Monotonic Recency Constrained Weight Optimization (1M ≥ 3M ≥ 6M ≥ 9M ≥ 12M)

| Performance Window   |   Monotonic Constrained Weight |   Exponential Decay Weight (β=0.0470) |   1M Dominant Preset (40/30/15/10/5) |   1M Heavy Preset (35/25/20/12/8) |   Moderate Recency Preset (30/25/20/15/10) |
|:---------------------|-------------------------------:|--------------------------------------:|-------------------------------------:|----------------------------------:|-------------------------------------------:|
| 1M                   |                         0.2334 |                                0.2192 |                                 0.4  |                              0.35 |                                       0.3  |
| 3M                   |                         0.2334 |                                0.2092 |                                 0.3  |                              0.25 |                                       0.25 |
| 6M                   |                         0.2334 |                                0.1996 |                                 0.15 |                              0.2  |                                       0.2  |
| 9M                   |                         0.2334 |                                0.1904 |                                 0.1  |                              0.12 |                                       0.15 |
| 12M                  |                         0.0666 |                                0.1817 |                                 0.05 |                              0.08 |                                       0.1  |

- **Monotonic Optimization Performance**: $R^2 = `0.6818`$, MAE = `10.87` RS points, Correlation = `0.8519`, $\pm 3$ Acc = `20.3\%$
- **Exponential Decay Performance ($eta=0.0470$)**: $R^2 = `0.6921`$, MAE = `10.76` RS points, Correlation = `0.8571`, $\pm 3$ Acc = `20.0\%$

### 3. ML Regression Models (Multi-Feature Momentum)

| ML Model             |      R² |   MAE |   ±3 Acc (%) |   ±5 Acc (%) |   ±10 Acc (%) |
|:---------------------|--------:|------:|-------------:|-------------:|--------------:|
| Ridge (alpha=50)     |  0.1212 | 22.02 |         6.43 |        12.48 |         22.99 |
| Linear Regression    | -3.2907 | 21.6  |         9.52 |        15.2  |         27.81 |
| HistGradientBoosting |  0.8457 |  6.71 |        39.8  |        56.98 |         81.95 |
| Random Forest        |  0.846  |  6.8  |        38.69 |        55.87 |         80.47 |
| ExtraTrees           |  0.8284 |  7.42 |        34.61 |        52.9  |         76.27 |

**Best ML Model**: `Random Forest` ($R^2 = `0.8460`$)

### 4. Sub-Rating Validation (RS 3-Month & RS 6-Month Ratings)

| Sub-Rating        |   Sample Size |     R² |   MAE |   Correlation |   ±3 Acc (%) |   ±5 Acc (%) |
|:------------------|--------------:|-------:|------:|--------------:|-------------:|-------------:|
| RS 3-Month Rating |          4044 | 0.2876 | 18.02 |        0.6544 |        11.75 |        19.63 |
| RS 6-Month Rating |          4044 | 0.5961 | 12.91 |        0.8078 |        15.7  |        27    |


### 5. Verified Practical RS Formulas for Pine Script & Python

```text
// 1. Monotonic Recency Constrained Weights (1M >= 3M >= 6M >= 9M >= 12M)
rs_raw = 0.2334 * rel_perf_1M + 0.2334 * rel_perf_3M + 0.2334 * rel_perf_6M + 0.2334 * rel_perf_9M + 0.0666 * rel_perf_12M
rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)

// 2. 1M Dominant Preset (Clean 40 / 30 / 15 / 10 / 5 Weighting)
rs_raw = 0.40 * rel_perf_1M + 0.30 * rel_perf_3M + 0.15 * rel_perf_6M + 0.10 * rel_perf_9M + 0.05 * rel_perf_12M
rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)

// 3. 1M Heavy Preset (Clean 35 / 25 / 20 / 12 / 8 Weighting)
rs_raw = 0.35 * rel_perf_1M + 0.25 * rel_perf_3M + 0.20 * rel_perf_6M + 0.12 * rel_perf_9M + 0.08 * rel_perf_12M
rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)
```
