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
Comp Rating = -15.961
   + 0.3590 × EPS_self + 0.5124 × RS_self + 0.2145 × SMR_self
   + 0.2004 × AD_self + 0.1425 × GroupRS_self
```

All five components are on a **common 1–99 scale** (SMR / A/D letters mapped A+=99 … E=1), so the
coefficients are directly comparable. Standardized importance (|coef × std(component)|, normalized):

| Component | OLS Coef | Importance % |
|:----------|---------:|-------------:|
| **RS**        |   0.5124 | **37.1** |
| **EPS**       |   0.3590 | **17.5** |
| **SMR**       |   0.2145 | **17.4** |
| **A/D**       |   0.2004 | **16.3** |
| **Group RS**  |   0.1425 | **11.8** |

RS dominates more than ever — its **dual-momentum upgrade** (below) pushed it to R²≈0.91–0.92, so
the blend leans on it harder. **Group RS is part of the production formula** — adding our
industry-mean RS lifted both weeks' holdout Composite R², so it earned a seat. Rows whose industry
group is too small/unmapped fall back to the fit-week group median (`group_median` ≈ 50) so the
Composite stays computable for every ticker.

### Full self-computed pipeline accuracy (ticker_cache only → predicted vs MarketSurge)

Production params are **fit on OLD (2026-07-24)**:

| Rating | In-sample (OLD) | **Forward out-of-sample (NEW)** | Corr (NEW) |
|:-------|----------------:|--------------------------------:|-----------:|
| **Composite** | **0.788 R²** | **0.763 R²** | 0.884 |
| **RS**        | **0.925 R²** | **0.912 R²** | 0.955 |
| A/D (A+..E)   | 36.0% exact / 57.9% ±1 | **37.4% exact / 58.5% ±1** | 0.841 |
| SMR (A–E)     | 61.0% exact | **62.1% exact** | 0.797 |
| EPS        | 0.376 R² | **0.387 R²** | 0.628 |

The Composite holds **~0.76 R² forward** out-of-sample on the newest week it was not trained on.
**RS is now the standout**: the dual-momentum upgrade (relative strength vs SPY **+** distance from
the 200-day MA, jointly optimised) lifted its forward R² from 0.834 to **0.912** (corr 0.955) — the
literature-backed insight that combining relative and absolute trend beats either alone. SMR
exact-letter ~61–62%, A/D within-one-letter ~58%.

> A/D and SMR are letter ratings, so their cells are letter-accuracy % (labeled ±1 / exact), not R².

---

## Comparison with earlier versions (v2, v3)

The earlier reverse-engineering experiments live in `python/reverse_engineer_ratings_v2.py` and
`python/reverse_engineer_ratings_v3.py` (reports in `output/rating_reengineering_v2_report.md` /
`output/rating_reengineering_v3_report.md`). This GLM pipeline is the only one with honest
**out-of-sample** validation: **v2 is in-sample only** (fit + eval on the 08-07 snapshot), **v3 is a
pooled in-sample panel** (07-24 + 08-07 together), while **GLM fits on OLD and forward-tests on NEW**.

### Composite Rating — the headline

| Version | Composite R² | Validation style | n |
|:--------|-------------:|:-----------------|--:|
| **v2** (`python/reverse_engineer_ratings_v2.py`) | 0.703 | in-sample, single week (08-07) | 2,480 |
| **v3** (`python/reverse_engineer_ratings_v3.py`) | — | pooled in-sample (no self-computed composite) | 7,334 rows |
| **GLM** (`IBD_rating_glm/`, final) | **0.788 in-sample / 0.763 forward** | fit on OLD → **out-of-sample NEW** | ~3,073 |

GLM's **0.763 is the only out-of-sample number, and it beats v2's in-sample 0.703 by a wide margin.**
v3 never built a self-computed composite — its 0.92–0.96 rows use MarketSurge's *true* component
ratings as inputs, which is a ceiling (not achievable without MarketSurge), not a comparison.

### Per-component — who leads what

| Component | v2 (in-sample 08-07) | v3 (pooled in-sample) | GLM (forward on NEW) | Leader |
|:----------|---------------------:|----------------------:|---------------------:|:-------|
| **RS** | 0.705 | 0.709 | **0.912** | **GLM** (huge margin) |
| **A/D** (numeric 1–13) | 0.101 | 0.165 | **0.569** (test) | **GLM** |
| **EPS** | 0.324 | 0.312 | **0.387** | **GLM** |
| **SMR** (numeric 10–95) | 0.681 | 0.650 | 0.681–0.693 (holdout) | GLM by a hair (holdout) |
| Composite | 0.703 | — | **0.763 forward** | **GLM** |

Letter metrics (GLM's production view): SMR **62.1% exact** forward, A/D **58.5% ±1** forward.

### Why each lead exists

- **RS — GLM's biggest win (0.912 vs ~0.71).** v2/v3 concluded percentile-rank beats the sigmoid;
  GLM kept the sigmoid but *optimized the window weights* AND added a **dual-momentum absolute-
  trend term** (distance from the 200-day MA) inside the sigmoid — the Dual-Momentum / SCTR insight
  that relative strength vs a benchmark plus an absolute trend filter beats either alone. The
  joint optimisation (weights + trend coefficient, fit on the earlier week) holds up out-of-sample
  (0.834 → **0.912** forward), far ahead of any percentile-rank variant.
- **A/D — GLM's second win (0.55 vs 0.10/0.17).** GLM fits the OLS blend directly on the numeric
  1–13 scale and adds distance-from-MA / % off 52-week-high features. It even beats v3's
  MarketSurge-**oracle** A/D (0.508, which used IBD's own Up/Dn-Vol + ATR + Funds columns) — GLM
  extracts more from ticker_cache's price/volume than v3 found in IBD's own technicals.
- **EPS — GLM ahead.** Log-compression plus analyst-estimate features (beat rate, surprise, revision
  trend) and the 13 new fund-json fields (margins, cash-flow yields, balance-sheet quality, analyst
  target upside) that v2 lacked — forward R² 0.386 vs v2's in-sample 0.324.
- **SMR — now GLM.** The 11 new quality fields (ROA, gross/operating margins, FCF/OCF yield, balance
  sheet) lifted GLM's holdout R² to 0.68–0.69 — matching/exceeding v2's 0.681 *in-sample* number on
  an honest holdout — and GLM's 62.1% exact-letter (forward) is the strongest SMR accuracy reported
  by any version.

### The composite weights differ meaningfully

- **v2/v3** fit on MarketSurge's *true* components → **EPS is #2** (28–29%): that is what the blend
  would be with perfect components.
- **GLM** fits on the *self-computed* components actually obtained from ticker_cache → a balanced
  five-way blend: **RS 34% · A/D 20% · EPS 18% · SMR 17% · Group RS 11%**. The weights reflect
  real-world reliability, not a perfect-input ideal.

**Bottom line:** GLM leads on all four components (RS, A/D, EPS, SMR) and produces **the only
honest forward-validated Composite (0.763)** — which beats every in-sample number v2/v3 reported.
The true-component ceiling (~0.93–0.96 in both v3 and GLM) confirms the remaining gap is in
component accuracy, not the blend formula — and the dual-momentum RS now reaches R² 0.91, the
single biggest component win.

---

## Methodology per rating

### RS Rating (strongest component)
- Features: absolute returns over 1M / 3M / 6M / 9M / 12M windows relative to SPY, **plus the
  distance from the 200-day moving average** (price features truncated to the snapshot's as-of day).
- Model: **dual-momentum sigmoid** — `RS = sigmoid(weighted rel-perf sum + k × Dist_200MA)` with
  the 5 window weights **and** the absolute-trend coefficient k jointly optimised on the train
  split (Dual Momentum: relative strength vs a benchmark *plus* an absolute trend filter beats
  either alone — also the core of StockCharts SCTR / Dorsey Wright RS).
- Production weights (fit on OLD): **3M 0.557, 1M 0.067, 9M 0.136, 12M 0.214, 6M 0.027**,
  **dual k ≈ 91**.
- Sub-ratings **RS 3-Month** / **RS 6-Month** use the single-window sigmoid form.
- Best test R² **0.893 (NEW holdout), 0.930 (OLD holdout)** — up from 0.814/0.841 for the
  relative-only sigmoid. Cross-week stable: the sigmoid is a fixed monotone map, and the
  absolute-trend term is fit on the earlier week only.

#### Research round 3 — TradingView / StockCharts / RRG RS variants (all rejected)

After researching how other platforms compute relative strength — TradingView's `rs()`/RSMA-crossover
and RSI-of-RS, StockCharts' R²-adjusted RS line, RRG's JdK RS-Ratio/RS-Momentum, Frazzini-Pedersen
beta-adjusted momentum, and Moreira-Muir volatility-managed momentum — every variant was tested in a
matched-universe ablation (fit on OLD, forward on NEW, baseline refit on the exact same rows):

| Variant | OLD R² | NEW R² | Verdict |
|:--------|-------:|-------:|:--------|
| **prod (dual-momentum, current)** | **0.930** | **0.911** | champion |
| vol (Moreira-Muir SharpeRel windows) | 0.935 | 0.896 | ❌ overfits OLD, collapses NEW |
| beta (additive Info_Beta term) | 0.930 | 0.911 | ❌ k≈0 |
| betawin (RelPerf/beta windows) | −0.14 | −0.17 | ❌ catastrophic |
| rsq (StockCharts R² of RS-line reg.) | 0.930 | 0.911 | ❌ k≈0 |
| rsmom (RRG RS-Momentum 20D) | 0.930 | 0.911 | ❌ +0.0001/+0.0002 = noise |
| rsi (TradingView RSI-of-RS, 14) | 0.930 | 0.911 | ❌ k≈0 |
| rsma20 (TradingView RS vs RSMA) | 0.930 | 0.911 | ❌ k≈0 |

**Why they lose:** IBD's RS Rating is a *raw cross-sectional rank* of price performance — it does not
penalise volatility or beta. Vol-/beta-adjusted variants move *away* from what IBD publishes (the
`betawin`/`vol` results are the empirical proof). The R²/RRG/RSI/RSMA ideas are additive momentum
signals that the existing 5-window relative-performance blend + absolute 200MA-trend term already
subsumes — the optimiser consistently lands their coefficients at ≈0. The extractor still computes
`RS_Ratio_Now`, `RS_RSq_65D/126D`, `RS_Mom_20D/65D`, `RS_RSI_14`, `RS_RSMA_20D`, `RelVol_*` and
`SharpeRel_*` (cheap diagnostics, useful for future experiments), but none are in the production
formula.

### A/D Rating (A+..E accumulation/distribution)
- Features: multi-window accumulation/distribution stats across **5D/10D/30D/65D/130D/250D** —
  Chaikin Money Flow, up/down-day volume ratios, net heavy-volume day intensity & ratio,
  volume-weighted closing range, price change per window — plus moving-average distances
  (**10/21/50/150/200-day**), % off 52-week high, price-volume correlation, and institutional
  holder flows.  The 10/21-day MAs and the full 5D–250D window set were added after both
  snapshots' holdouts improved (matched-universe A/D ±1-letter: OLD 51.0→58.6%, NEW 53.8→59.5%).
- Model: OLS blend on a numeric 1–13 scale (A+..E), letters calibrated by percentile transfer from
  the fit week. Cross-week letter stability verified in both directions.
- Price-side features only: fund fields like Info_EarningsGrowth were tested but force a much
  smaller fit universe (dropna), so they are excluded — coverage beats richness here.

### EPS Rating (hardest; data-limited)
- yfinance fund JSONs expose only ~5 quarterly rows and limited estimates, so deep YoY-acceleration
  features are unavailable. The model uses: earnings-beat rate, negative-quarter ratio, EPS stability
  (CV), ROE, long-term EPS growth, surprise mean, estimate growth (Q/Y), current-quarter YoY, revision
  trend — **plus 13 high-coverage fund-json fields** (ROA, quarterly EPS growth, gross/operating/net
  margins, FCF/OCF yield, debt/equity, current ratio, cash/share, analyst target upside, analyst
  count, forward P/E) **and the gross-margin level/trend computed from the income statement**
  (GrossMargin_Now, GrossMargin_Trend — research round 2, both weeks improved).
- Model: direct OLS to the 1–99 scale (percentile ranking was tried and *hurts* EPS — R² negative).
  **Growth/level features are log-compressed** — same sign-preserving `log1p` treatment as SMR.
  Bounded 0–1 ratios (negative-quarter ratio, beat rate) and the 0–10 stability CV keep their clips.
  Transform frozen in `fitted_params.json` (`eps.log_features`), applied identically at fit and
  scoring time. Forward out-of-sample R²: 0.265 → 0.345 → **0.386** across the three feature rounds.
- Note: EPS is fundamentally the noisiest rating to reproduce from quarterly fundamentals alone; the
  Composite down-weights it (~17%).

### SMR Rating (A–E, quintile-ish)
- Features: profit margin, ROE, current margin, margin trend, long-term sales growth, quarterly sales
  YoY, revenue growth — **plus 11 high-coverage fund-json quality fields** (ROA, gross/operating
  margins, FCF/OCF yield, debt/equity, current/quick ratio, earnings & EPS-Q growth, price/book).
  Adding them raised exact-letter accuracy from 60.2% to **62.1%** (forward; OLD 58.6→61.0%) and the
  direct-scale holdout R² to **0.69** (OLD) / 0.68 (NEW) — both snapshots improved.
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
- Self-computed EPS/RS/SMR/A/D **+ our industry Group RS** on the common 1–99 scale → OLS against
  MarketSurge Comp Rating. Group RS (percentile of industry-mean RS) earned a production seat when
  it improved both weeks' holdout R² (OLD 0.758→0.769, NEW 0.752→0.773).
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
  fund-json fallback). It **is part of the Composite formula** (~11% importance); tickers whose
  industry group is too small or unmapped get the fit-week group median instead, so the Composite
  stays computable for every ticker.

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

Remember the fitted importance: **RS (37%) > EPS (18%) ≈ SMR (17%) ≈ A/D (16%) > Group RS (12%)** —
for stock picking, RS is by far the main Composite driver now that its dual-momentum upgrade made it
the most accurate component (forward R² 0.91).

---

## Notes & caveats

- Only tickers with a valid (non-zero) MarketSurge Comp Rating **and** a price parquet **and** a fund
  JSON in `ticker_cache/` are used (3,199–3,201 per week).
- `Comp Rating = 0` is MarketSurge's "not rated" sentinel — excluded everywhere.
- MarketSurge's **SMR Rating is a single letter A–E** (unlike A/D which is A+..E), so SMR's numeric
  target has only 5 distinct levels — its letter accuracy is the production-relevant metric.
- Fundamentals are updated far less frequently than prices; EPS/SMR predictions are only as fresh as
  the last fundamentals pull.
- A/D (and therefore the Composite) requires ~250 trading days of price history for its full window
  feature set; younger tickers get NaN A/D/Composite (the cache is backfilled to 2024-06, so most
  names are fine).
- Letter calibrations (A/D, SMR) are fit on the OLD snapshot and forward-validated on NEW; if you
  re-run the pipeline when a new MarketSurge CSV arrives, the OLD/newest split shifts forward and
  production params stay look-ahead-free.
- **RS is now partly an absolute-trend rating.** The dual-momentum term (k ≈ 91 × distance from the
  200-day MA, added inside the sigmoid) means RS blends relative strength vs SPY with an absolute
  trend filter — for stocks far from their MA (e.g. +50%) the trend term can dominate the raw score.
  This is the literature-backed Dual-Momentum design and it held out-of-sample (forward R² 0.912),
  but it changes the interpretation: a stock above its 200-day MA gets a boost regardless of how
  SPY is doing.
- **RS-line diagnostics are computed but not consumed.** The extractor builds the price/SPY ratio
  line (`RS_Ratio_Now`, `RS_RSq_*`, `RS_Mom_*`, `RS_RSI_14`, `RS_RSMA_20D`, `RelVol_*`, `SharpeRel_*`)
  for every ticker so future RS research (research round 3 above) can reuse them without re-reading
  parquets. They are cheap and additive; none feed the production formula.
- **Halted days shift the diagnostic windows.** The RS line drops NaN days before computing
  `RS_Mom_20D`/`RS_RSq_65D` etc., so those windows are "N *valid* trading days" rather than exactly
  20/65 calendar trading days. Immaterial for diagnostics (rare halts in this universe).
- No Machine Learning was used — every method is a weighted blend / rank / OLS fit, fully inspectable
  in the report and params.
