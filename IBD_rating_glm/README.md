# IBD Rating Reverse-Engineering (non-ML, ticker_cache only)

Goal: reproduce IBD's five ratings — **Composite, RS, EPS, SMR, A/D** — using only local data in
`ticker_cache/` (price/volume parquets + fundamentals JSON) and the ticker→industry mapping in
`../IBD Industry Mapping.txt`, so you no longer need MarketSurge for rating-based filtering.

MarketSurge CSVs (`../IBD/marketsuge-8-7-2026.csv`, `../IBD/marketsurge.csv`) are used **only as
ground-truth labels** for fitting and validation. No MarketSurge-derived fields feed the model inputs.

All methods are transparent and interpretable — weighted return blends, percentile ranks, and
constrained OLS scalar fits. **No machine learning.**

---

## Results (two-week cross validation)

Snapshots used:
- **NEW** — `marketsuge-8-7-2026.csv`, data as-of **2026-08-07**
- **OLD** — `marketsurge.csv`, data as-of **2026-07-24** (file date 2026-07-29)

**Production params are fit on OLD and forward-validated on NEW** (no look-ahead): the models
train on the earlier week's snapshot and are then applied to the newest week's data — exactly how
the production scorer is used on live ticker_cache data. The NEW fit is archived alongside for
comparison (`output/fitted_params_fit_on_new.json`).

Price features for each snapshot are computed with history **truncated to that snapshot's as-of day**
(the file date's ticker_cache cutoff).

### Composite formula (the filter — most important output)

**Production params (fit on OLD 2026-07-24, forward-validated on NEW 2026-08-07):**

```
Comp Rating = -16.777
   + 0.3116 × EPS_self + 0.6662 × RS_self + 0.2522 × SMR_self + 0.2501 × AD_self
```

All four components are on a **common 1–99 scale** (SMR / A/D letters mapped A+=99 … E=1), so the
coefficients are directly comparable. Standardized importance (|coef × std(component)|, normalized):

| Component | OLS Coef | Importance % |
|:----------|---------:|-------------:|
| **RS**    |   0.6662 | **42.0** |
| **SMR**   |   0.2522 | **21.5** |
| **A/D**   |   0.2501 | **21.3** |
| **EPS**   |   0.3116 | **15.2** |

RS dominates the Composite; SMR and A/D are close seconds; EPS contributes the least (though log-
compressing its features lifted its weight from ~11% to ~15% as it got more accurate).

### Full self-computed pipeline accuracy (ticker_cache only → predicted vs MarketSurge)

Production params are **fit on OLD (2026-07-24)**:

| Rating | In-sample R² (OLD) | **Forward out-of-sample R² (NEW)** | Corr (NEW) |
|:-------|-------------------:|-----------------------------------:|-----------:|
| **Composite** | **0.734** | **0.724** | 0.858 |
| **RS**        | **0.849** | **0.834** | 0.940 |
| A/D (A+..E)   | 51.7% ±1 | 56.1% ±1 | 0.816 |
| SMR (A–E)     | 58.6% exact | **60.2% exact** | 0.783 |
| EPS        | 0.336 | 0.345 | 0.595 |

The Composite holds ~0.72 R² forward out-of-sample on the newest week it was not trained on — the
honest test of the production formula. RS is the strongest component (R²≈0.83–0.85, corr≈0.94–0.95).
SMR exact-letter accuracy ~59–60%, A/D within-one-letter ~52–56%.

> A/D and SMR are letter ratings, so their cells are letter-accuracy % (labeled ±1 / exact), not R².

---

## Methodology per rating

### RS Rating (strongest component)
- Features: absolute returns over 1M / 3M / 6M / 9M / 12M windows, relative to SPY (price features
  truncated to the snapshot's as-of day so the two-week test is clean).
- Model: **sigmoid on a weighted relative-performance sum** with monotonic window weights
  (scipy `minimize`), then mapped through a percentile score reference to the 1–99 scale.
- Production weights (fit on OLD): **3M 0.513, 9M 0.379, 6M 0.051, 12M 0.057** (1M ≈ 0).
- Sub-ratings **RS 3-Month** / **RS 6-Month** use the same sigmoid form on single windows.
- Best test R² 0.814 (NEW holdout), 0.841 (OLD holdout); cross-week stable (sigmoid form was chosen
  over plain percentile rank precisely because it transfers across weeks).

### A/D Rating (A+..E accumulation/distribution)
- Features: 30/65/130-day Chaikin Money Flow, up-day vs down-day volume ratios, net heavy-volume
  day intensity, distance from moving averages, % off 52-week high, institutional holder flows.
- Model: OLS blend on a numeric 1–13 scale (A+..E), letters calibrated by percentile transfer from
  the fit week. Cross-week letter stability verified in both directions.

### EPS Rating (hardest; data-limited)
- yfinance fund JSONs expose only ~5 quarterly rows and limited estimates, so deep YoY-acceleration
  features are unavailable. The model uses: earnings-beat rate, negative-quarter ratio, EPS stability
  (CV), ROE, long-term EPS growth, surprise mean, estimate growth (Q/Y), current-quarter YoY, revision
  trend.
- Model: direct OLS to the 1–99 scale (percentile ranking was tried and *hurts* EPS — R² negative).
  **Growth/level features (EPS QoQ YoY, long-term growth, ROE, surprise mean, revision trend,
  estimate growth Q/Y) are log-compressed** — same sign-preserving `log1p` treatment as SMR, which
  raised the forward out-of-sample R² from 0.265 to **0.345** (holdout test R² 0.309→0.379 on the
  OLD snapshot). Bounded 0–1 ratios (negative-quarter ratio, beat rate) and the 0–10 stability CV
  keep their clips. Transform frozen in `fitted_params.json` (`eps.log_features`), applied
  identically at fit and scoring time.
- Note: EPS is fundamentally the noisiest rating to reproduce from quarterly fundamentals alone; the
  Composite down-weights it (~15%), but its weight rose as log-compression made it more accurate.

### SMR Rating (A–E, quintile-ish)
- Features: profit margin, ROE, current margin, margin trend, long-term sales growth, quarterly sales
  YoY, revenue growth.
- Model: OLS 3-pillar blend on numeric 10–95 scale, calibrated quintile letters. **Features are
  log-compressed** (sign-preserving `log1p`) instead of hard-clipped: Margin_Now can blow up past
  -1,000,000% on near-zero revenue, and a hard clip collapsed all such rows to the same value and
  destroyed rank information. Log-compression raised SMR exact-letter accuracy from ~55.8% to 60.2%
  (forward) and the direct-scale holdout R² from ~0.19 to **0.66** (matches v2's in-sample 0.68,
  now measured on an honest 20% holdout). The transform is frozen in `fitted_params.json`
  (`log_features`) and applied identically at fit and scoring time.
- The direct-scale OLS is the diagnostic number to compare against v2; production still maps SMR to
  a 1–99 percentile for the Composite blend (all components share the scale).

### Composite Rating
- Self-computed EPS/RS/SMR/A/D on the common 1–99 scale → OLS against MarketSurge Comp Rating.
- Formula and importance table above.

---

## File layout

```
IBD_rating_glm/
├── common.py                    # shared: data loading, universe, feature extraction, letter calibration
├── analyze_rs_ratings.py        # RS model fitting (sigmoid monotonic weights) + RS 3M/6M sub-ratings
├── analyze_ad_ratings.py        # A/D model fitting (OLS accumulation blend + letters)
├── reverse_engineer_ratings.py  # main driver: EPS, SMR, Composite fits + cross-week validation report
├── reverse_engineer_ratings_v2.py  # earlier closed-form v2 experiment (kept for reference)
├── calc_ibd_ratings.py          # PRODUCTION scorer: computes all 5 ratings from ticker_cache only
├── output/
│   ├── fitted_params.json           # PRODUCTION params (fit on OLD, forward-validated on NEW)
│   ├── fitted_params_fit_on_new.json  # NEW-snapshot fit (archived for comparison)
│   ├── fitted_params_fit_on_old.json  # OLD-snapshot fit (= production)
│   └── rating_reengineering_report.md  # full per-method report (fit + cross-week)
└── README.md
```

## Usage

### 1. Re-run the full analysis (fits + two-week validation report)

```bash
cd IBD_rating_glm
python3 reverse_engineer_ratings.py              # production params = fit on OLD (default)
python3 reverse_engineer_ratings.py --production-snapshot new   # switch production to NEW fit
```

Writes `output/rating_reengineering_report.md`, `output/fitted_params.json` (selected snapshot),
and both archived fits. Run time ≈ 2–4 min (two feature extractions + two pipeline fits over
~3,200 tickers).

### 2. Score the whole cache with the production scorer (no MarketSurge)

```bash
cd IBD_rating_glm
python3 -c "from calc_ibd_ratings import score_all_cached; df = score_all_cached(); print(df.head(20))"
```

`score_all_cached()` scans every `ticker_cache/*_1d.parquet` (≈3,800 tickers), loads the fitted
params from `output/fitted_params.json`, and returns a DataFrame with:

`Symbol, RS Rating, RS 3M, RS 6M, EPS Rating, SMR Score, SMR Rating, A/D Score, A/D Rating,
Comp Rating, Group RS, % Off 52W High, Latest Price, Hist Days`

- **RS 3M / RS 6M** — our own sub-ratings (single-window sigmoid form, same as the RS research).
- **Group RS** — our own industry relative-strength rating (industry from `../IBD Industry Mapping.txt`,
  fund-json fallback). It is emitted as a diagnostic column but is **not** part of the Composite
  formula (adding it only nudges cross-week Composite R² from 0.69 to 0.71, so it was left out of
  the production blend for simplicity).

To score a specific list:

```python
from calc_ibd_ratings import score_universe
df = score_universe(['AAPL', 'MSFT', 'NVDA', 'LLY', 'DHI'])
```

### 3. How to use the Composite for filtering

```python
df = score_all_cached()
watchlist = df[(df['Comp Rating'] >= 80) & (df['RS Rating'] >= 80)].sort_values('Comp Rating', ascending=False)
```

Remember the fitted importance: **RS (42%) > SMR (22%) ≈ A/D (21%) > EPS (15%)** — for stock picking,
RS is the main Composite driver, with SMR and A/D close behind.

---

## Notes & caveats

- Only tickers with a valid (non-zero) MarketSurge Comp Rating **and** a price parquet **and** a fund
  JSON in `ticker_cache/` are used (3,199–3,201 per week).
- `Comp Rating = 0` is MarketSurge's "not rated" sentinel — excluded everywhere.
- MarketSurge's **SMR Rating is a single letter A–E** (unlike A/D which is A+..E), so SMR's numeric
  target has only 5 distinct levels — its letter accuracy is the production-relevant metric.
- Fundamentals are updated far less frequently than prices; EPS/SMR predictions are only as fresh as
  the last fundamentals pull.
- Letter calibrations (A/D, SMR) are fit on the OLD snapshot and forward-validated on NEW; if you
  re-run the pipeline when a new MarketSurge CSV arrives, the OLD/newest split shifts forward and
  production params stay look-ahead-free.
- No Machine Learning was used — every method is a weighted blend / rank / OLS fit, fully inspectable
  in the report and params.
