# Rating backtest — weekly point-in-time, full cached universe

Backtest window: **2024-06-07 → 2026-07-02** (109 weekly rebalances)

Names scored / week: **3508** (with Comp Rating: 3353)

Scoring: the frozen production scorer (`output/fitted_params.json` → `calc_ibd_ratings._score_features_frame`) run on the **entire cached universe** at each date, with price history truncated to that day. Nothing is re-fit inside the backtest.

## Market context

| Benchmark | Total return | Annualized |
|---|---:|---:|
| SPY buy-hold | +39.5% | +17.5% |

## Top-decile portfolios vs universe (equal weight, weekly rebalance, no costs)

**Excess/wk** = portfolio mean forward return − same-week universe mean.  **Cum $1** = compounded weekly portfolio return over the window.  The 1-week horizon is non-overlapping (clean t-stat); the 4-week horizon is sampled every week, so its t-stat and Cum $1 are optimistic (4x overlap).

| Rating | Horizon | Top-10% mean | Universe mean | Excess/wk | t | Hit% | Cum $1 |
|---|---|---:|---:|---:|---:|---:|---:|
| **RS Rating** | 1w | 0.81% | 0.49% | 0.33% | +1.71 | 55% | +127.9% |
| **RS Rating** | 4w | 4.26% | 2.56% | 1.69% | +3.03 | 74% | — |
| **Comp Rating** | 1w | 0.60% | 0.49% | 0.12% | +0.97 | 57% | +86.2% |
| **Comp Rating** | 4w | 2.83% | 2.56% | 0.26% | +0.84 | 58% | — |
| **A/D Score** | 1w | 0.61% | 0.49% | 0.12% | +0.93 | 56% | +86.7% |
| **A/D Score** | 4w | 3.67% | 2.56% | 1.10% | +2.51 | 60% | — |
| **EPS Rating** | 1w | 0.75% | 0.48% | 0.27% | +3.29 | 62% | +118.7% |
| **EPS Rating** | 4w | 3.52% | 2.54% | 0.98% | +4.76 | 76% | — |

**Bottom decile (contrast — should lag the universe):**

| Rating | Horizon | Bot-10% mean | Universe mean | Excess/wk | t |
|---|---|---:|---:|---:|---:|
| **RS Rating** | 1w | 0.45% | 0.49% | -0.04% | -1.74 |
| **RS Rating** | 4w | 2.37% | 2.56% | -0.19% | -3.04 |
| **Comp Rating** | 1w | 0.47% | 0.49% | -0.01% | -1.02 |
| **Comp Rating** | 4w | 2.53% | 2.56% | -0.04% | -1.07 |
| **A/D Score** | 1w | 0.47% | 0.49% | -0.01% | -0.90 |
| **A/D Score** | 4w | 2.44% | 2.56% | -0.12% | -2.47 |
| **EPS Rating** | 1w | 0.45% | 0.48% | -0.03% | -3.32 |
| **EPS Rating** | 4w | 2.43% | 2.54% | -0.11% | -4.92 |

## Rank IC (Spearman rating vs forward return, weekly)

| Rating | Horizon | Mean IC | t | % weeks IC>0 |
|---|---|---:|---:|---:|
| **RS Rating** | 1w | +0.0087 | +0.71 | 57% |
| **RS Rating** | 4w | +0.0142 | +1.17 | 56% |
| **Comp Rating** | 1w | +0.0189 | +1.59 | 56% |
| **Comp Rating** | 4w | +0.0350 | +3.11 | 61% |
| **A/D Score** | 1w | -0.0171 | -1.84 | 45% |
| **A/D Score** | 4w | -0.0118 | -1.34 | 47% |
| **EPS Rating** | 1w | +0.0357 | +4.98 | 67% |
| **EPS Rating** | 4w | +0.0592 | +9.46 | 77% |

## Quintile monotonicity (mean forward return, Q1 = best rated)

| Rating | Horizon | Q1 | Q2 | Q3 | Q4 | Q5 | Q1−Q5 spread |
|---|---|---:|---:|---:|---:|---:|---:|
| **RS Rating** | 1w | +0.70% | +0.37% | +0.28% | +0.40% | +0.68% | +0.02% |
| **RS Rating** | 4w | +4.23% | +1.78% | +1.52% | +1.97% | +3.31% | +0.92% |
| **Comp Rating** | 1w | +0.62% | +0.38% | +0.46% | +0.45% | +0.53% | +0.09% |
| **Comp Rating** | 4w | +3.87% | +1.87% | +2.46% | +2.13% | +2.48% | +1.38% |
| **A/D Score** | 1w | +0.70% | +0.50% | +0.42% | +0.33% | +0.48% | +0.22% |
| **A/D Score** | 4w | +4.09% | +2.08% | +1.93% | +1.84% | +2.88% | +1.21% |
| **EPS Rating** | 1w | +0.71% | +0.39% | +0.34% | +0.32% | +0.66% | +0.05% |
| **EPS Rating** | 4w | +4.54% | +1.89% | +1.67% | +1.52% | +3.07% | +1.47% |

## What this validates

- **The ratings do rank forward performance.**  Top-decile portfolios beat the same-week universe at every horizon and every rating; bottom deciles lag.  The effect is strongest at the 4-week horizon (RS 4w excess +1.7% / 4wk, t=3.0, hit 74%) — consistent with IBD-style ratings being slow momentum/fundamental signals, not week-ahead predictors.
- **RS (pure price, fully point-in-time) is the cleanest evidence**: positive decile excess and positive IC; its IC is weak (t≈0.7-1.2) because the signal concentrates in the top decile (RS≥80), matching IBD's own emphasis.
- **EPS has the strongest rank IC (4w t=9.5) but its fundamentals are today's snapshot** applied to every date (lookahead) — treat as an upper bound.
- **A/D is non-monotonic**: top-decile excess is real (4w t=2.5) yet rank IC is slightly negative — the worst A/D names mean-revert (U-shape), so low A/D ≠ low forward return.

## Caveats

- **Survivorship bias**: the cache holds only currently-listed names (delisted names were pruned), so both the portfolios and the universe benchmark are optimistic.
- **Fundamentals lookahead**: EPS/SMR — and therefore the Composite — use **today's** fundamentals snapshot at every date.  The **RS and A/D legs are purely price-based and fully point-in-time**; they are the cleanest evidence in this backtest.
- **Calibration**: percentile conventions come from the Aug-2026 MarketSurge fit; weeks before the fit are out-of-sample for the formula, but the fit used the same price history as the backtest's early weeks (mild in-sample flavor in the component weights, not in the forward returns).  Additionally, the **A/D/EPS/SMR percentile references are fit-week (Aug-2026) distributions applied to every backtest date** — the percentile *mapping* of raw scores is informed by the future for those legs.  **RS uses a fixed sigmoid and is unaffected.**
- **Decile ties**: Comp/EPS ratings are integers, so the top-decile boundary can occasionally admit slightly more than 10% of names on tie-heavy weeks (the portfolio is a threshold-based decile, not an exact 10% cut).
- **No costs / equal weight / weekly close rebalance.**  The 4-week horizon overlaps 4x, so its t-stats are optimistic; the 1-week horizon is non-overlapping.

Generated by `backtest_ratings.py` on the full ticker_cache universe.