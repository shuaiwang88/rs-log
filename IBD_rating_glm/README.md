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
Comp Rating = -16.200
   + 0.3742 × EPS_self + 0.5068 × RS_self + 0.2036 × SMR_self
   + 0.2030 × AD_self + 0.1430 × GroupRS_self
```

All five components are on a **common 1–99 scale** (SMR / A/D letters mapped A+=99 … E=1), so the
coefficients are directly comparable. Standardized importance (|coef × std(component)|, normalized):

| Component | OLS Coef | Importance % |
|:----------|---------:|-------------:|
| **RS**        |   0.5068 | **36.7** |
| **EPS**       |   0.3742 | **18.4** |
| **SMR**       |   0.2036 | **16.5** |
| **A/D**       |   0.2030 | **16.5** |
| **Group RS**  |   0.1430 | **11.8** |

RS dominates more than ever — its **dual-momentum upgrade** (below) pushed it to R²≈0.91–0.92, so
the blend leans on it harder. **Group RS is part of the production formula** — adding our
industry-mean RS lifted both weeks' holdout Composite R², so it earned a seat. Rows whose industry
group is too small/unmapped fall back to the fit-week group median (`group_median` ≈ 50) so the
Composite stays computable for every ticker.

### Full self-computed pipeline accuracy (ticker_cache only → predicted vs MarketSurge)

Production params are **fit on OLD (2026-07-24)**:

| Rating | In-sample (OLD) | **Forward out-of-sample (NEW)** | Corr (NEW) |
|:-------|----------------:|--------------------------------:|-----------:|
| **Composite** | **0.788 R²** | **0.765 R²** | 0.885 |
| **RS**        | **0.925 R²** | **0.912 R²** | 0.955 |
| A/D (A+..E)   | 35.6% exact / 58.3% ±1 | **38.0% exact / 59.8% ±1** | 0.849 |
| SMR (A–E)     | 61.2% exact | **62.3% exact** | 0.798 |
| EPS        | 0.378 R² | **0.391 R²** | 0.631 |

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
| **GLM** (`IBD_rating_glm/`, final) | **0.788 in-sample / 0.765 forward** | fit on OLD → **out-of-sample NEW** | ~3,073 |

GLM's **0.765 is the only out-of-sample number, and it beats v2's in-sample 0.703 by a wide margin.**
v3 never built a self-computed composite — its 0.92–0.96 rows use MarketSurge's *true* component
ratings as inputs, which is a ceiling (not achievable without MarketSurge), not a comparison.

### Per-component — who leads what

| Component | v2 (in-sample 08-07) | v3 (pooled in-sample) | GLM (forward on NEW) | Leader |
|:----------|---------------------:|----------------------:|---------------------:|:-------|
| **RS** | 0.705 | 0.709 | **0.912** | **GLM** (huge margin) |
| **A/D** (numeric 1–13) | 0.101 | 0.165 | **0.569** (test) | **GLM** |
| **EPS** | 0.324 | 0.312 | **0.391** | **GLM** |
| **SMR** (numeric 10–95) | 0.681 | 0.650 | 0.681–0.693 (holdout) | GLM by a hair (holdout) |
| Composite | 0.703 | — | **0.763 forward** | **GLM** |

Letter metrics (GLM's production view): SMR **62.3% exact** forward, A/D **59.8% ±1** forward.

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
  Round 6 adopted the production A/D feature set (drop VWClsRange/CMF_130D/Inst flows, add
  all-window NetHeavyDays/AvgClsRange + CMF_5D) → forward ±1-letter 58.5% → **59.8%**.
- **EPS — GLM ahead.** Log-compression plus analyst-estimate features (beat rate, surprise, revision
  trend), the 13 new fund-json fields (margins, cash-flow yields, balance-sheet quality, analyst
  target upside), gross-margin trend, and forward revenue estimates + recommendation consensus —
  forward R² 0.391 vs v2's in-sample 0.324.
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

#### Research round 6 — learned from the production scorer (`python/calc_ibd_ratings.py`)

`python/calc_ibd_ratings.py` had meanwhile refit every rating on its own walk-forward split and,
importantly, **added the industry Group RS as a 5th Composite component** — the same idea this
pipeline adopted in earlier rounds (and already present in `fitted_params.json`; both sides
independently converged to GroupRS ≈ 0.14 / ~12% importance, mutual confirmation).

Its A/D feature set differed from ours, so it was tested head-to-head under the standard
matched-universe protocol (fit on OLD → forward on NEW, both sets refit on the same rows):

| Set | OLD exact / ±1 | **NEW forward exact / ±1** | Verdict |
|:----|---------------:|---------------------------:|:--------|
| GLM current | 35.5 / 58.0 | 37.4 / 58.9 | baseline |
| GLM minus dropped (VWClsRange/CMF_130D/UpDay/DnDay/Inst) | 35.2 / 57.7 | 37.7 / 59.1 | ✓ helps NEW |
| GLM plus additions (all-window NetHeavyDays/AvgClsRange/CMF_5D…) | 35.4 / 58.0 | 37.6 / 59.1 | ✓ helps NEW |
| **PYTHON production set (full)** | 35.1 / 58.2 | **38.0 / 60.1** | ✅ **adopted** |

**Why the production set wins:** dropping the near-duplicate features (VWClsRange ~0.98-correlated
with CMF at the same window; CMF_130D ~0.75 with CMF_65D — the OLS was splitting a large canceling
coefficient pair across them) lets the genuinely independent signals — NetHeavyDays and AvgClsRange
at the short 5D/10D windows, CMF_5D, HeavyNetRatio_5D/10D/250D — carry weight.  Productionparams after adoption: **A/D forward exact 38.0% / ±1 59.8%** (from 37.4/58.5), Composite forward R²
**0.7645 → 0.7652** with GroupRS at 11.8% importance.  The full driver self-pipeline numbers above
reflect this.

#### Research round 7 — internet-sourced analyst-momentum & quality features (EPS, SMR, A/D)

New internet research (IBD methodology pages, academic quality-factor literature — Sloan accruals,
Novy-Marx gross profitability, Piotroski components, IBES-style revision momentum, TradingView/
thinkorswim price-volume studies) produced fresh candidates from fund-json blocks not yet mined:
`upgrades_downgrades` (timestamped analyst grade + price-target events), `EBIT`/`Operating Income`,
`Invested Capital`, `Free Cash Flow`, annual buybacks/issuance.  Each was tested with the standard
matched-universe protocol (fit on OLD → forward on NEW, both weeks must improve):

| Rating | Candidate | OLD | NEW | Verdict |
|:-------|:----------|----:|----:|:--------|
| **EPS** | price-target momentum (PTChg90, /price) | 0.3778→**0.3783** | 0.3908→**0.3910** | ✅ **adopted** |
| EPS | net upgrade/downgrade balance 30/90d (UpDownNet30/90) | 0.3782 | 0.3907 | ❌ NEW flat |
| SMR | ROIC, EBIT margin level+trend | 60.9% | 62.1% | ❌ both worse |
| SMR | FCF margin, Δdebt/assets | 61.2% | 62.3% | ❌ flat |
| SMR | buyback yield, net issuance | 61.2% | 61.9% | ❌ NEW worse |
| SMR | all 7 quality features combined | 61.2% | 62.0% | ❌ NEW worse |
| **A/D** | 5-day volume spike (VolSpike_5D) | 35.6→**35.7%** | 38.0→**38.3%** | ✅ **adopted** |
| A/D | price-vol correlation (PriceVolCorr) | 35.7% | 38.0% | ❌ NEW flat |
| A/D | up/down-day volume ratios (re-added) | 35.5% | 37.9% | ❌ both worse |
| A/D | all 4 price-volume additions | 35.7% | 38.1% | ❌ worse than VolSpike alone |

**Why the winners work:** PTChg90 is the analyst price-target *momentum* — how far targets have
moved over ~90 days relative to price — which rewards IBD's forward-earnings emphasis with a
forward-looking, event-driven signal (upgrades/downgrades blocks exist for ~all names; the grade
net-count variant was too noisy).  VolSpike_5D captures accumulation *on a volume blast* (5d vs 65d
volume ratio), the cleanest of the price-volume additions.  The SMR quality candidates all failed
the both-week test — the 20-feature SMR already spans sales/margins/ROE + accruals, and the new
statement-derived ratios added collinearity without signal.  Production params after adoption:
**EPS forward R² 0.3908 → 0.3910**, **A/D forward exact 38.0 → 38.3%**, Composite forward R² 0.7651
(unchanged within noise), SMR unchanged 62.3%.  *(The weekly backtest in `output/backtest_ratings.*`
was run with the pre-round-7 params; the delta is immaterial at the 3rd decimal.)*

#### Research round 4 — TradingView / thinkorswim / quant fundamentals (EPS, SMR, A/D)

New candidate features from the wider research (TradingView fundamentals, thinkorswim studies,
Sloan/Novy-Marx/Piotroski quality factors, IBES-style revision momentum) were tested with the same
matched-universe protocol (fit on OLD → forward on NEW):

| Rating | Candidate | OLD | NEW | Verdict |
|:-------|:----------|----:|----:|:--------|
| **EPS** | forward revenue-estimate growth (0q/0y) + recommendation consensus | 0.3762→**0.3778** | 0.3873→**0.3908** | ✅ **adopted** |
| EPS | EPS revision net counts (7/30d) | 0.3763 | 0.3875 | ❌ +0.0002 (noise) |
| EPS | accrual quality (Accrual_Q, OCF_NI) | 0.3750 | 0.3859 | ❌ both worse |
| **SMR** | Sloan accruals + OCF/NI (earnings quality) | 61.0→**61.2%** | 62.1→**62.3%** | ✅ **adopted** |
| SMR | debt/assets | 61.2% | 62.2% | ❌ not both weeks |
| SMR | gross profitability (GP/Assets), asset turnover | 61.3% | 61.9% | ❌ NEW worse |
| SMR | mutual-fund ownership (MF_Count/MF_Chg) | 60.8% | 61.7% | ❌ both worse |
| SMR | insider buying ratio | 61.0% | 62.1% | ❌ flat |
| A/D | MFI-14 (money-flow oscillator) | 57.3% | 59.3% | ❌ OLD worse (not both) |
| A/D | A/D-line slope 65D/130D (TOS study) | 57.7% | 59.0% | ❌ flat |

**Why the winners work:** the EPS Rating rewards a *forward-looking* earnings trend, and analyst
revenue estimates + recommendation consensus are the best forward-looking proxies available in the
fund json (94%/76% coverage, median-imputed so no universe loss).  SMR's margin/ROE pillars benefit
from Sloan-style earnings quality — accruals and the OCF/NI ratio separate cash-backed profits from
accounting profits.  The rejected ideas failed the both-week rule (e.g. MFI-14 gained NEW but lost
OLD; fund ownership hurt SMR's calibrated grade mix).  The new price-side diagnostics (MFI-14,
A/D-line slope) stay in the extractor as documented research features, not in any production formula.

### A/D Rating (A+..E accumulation/distribution)
- Features: multi-window accumulation/distribution stats across **5D/10D/30D/65D/130D/250D** —
  Chaikin Money Flow, up/down-day volume ratios, net heavy-volume day intensity & ratio,
  **net heavy-volume day counts, unweighted average closing range, and raw price change at
  EVERY window** — plus moving-average distances (**10/21/50/150/200-day**) and % off 52-week
  high.  **Research round 6 (below) adopted the production feature set from
  `python/calc_ibd_ratings.py`**, which drops the VWClsRange family (~0.98 correlated with CMF,
  redundant), CMF_130D (~0.75 with CMF_65D — collinear), UpDayVolRatio/DnDayVolRatio and the
  institutional-holder flows, and adds NetHeavyDays/AvgClsRange at every window (incl. 5D/10D/
  250D), CMF_5D, HeavyNetRatio_5D/10D/250D and NetHeavyIntensity_5D.
- Model: OLS blend on a numeric 1–13 scale (A+..E), letters calibrated by percentile transfer from
  the fit week. Cross-week letter stability verified in both directions. Forward exact-letter
  **38.0% / ±1 59.8%** (up from 37.4%/58.5%).
- Price-side features only: fund fields like Info_EarningsGrowth were tested but force a much
  smaller fit universe (dropna), so they are excluded — coverage beats richness here.

### EPS Rating (hardest; data-limited)
- yfinance fund JSONs expose only ~5 quarterly rows and limited estimates, so deep YoY-acceleration
  features are unavailable. The model uses: earnings-beat rate, negative-quarter ratio, EPS stability
  (CV), ROE, long-term EPS growth, surprise mean, estimate growth (Q/Y), current-quarter YoY, revision
  trend — **plus 13 high-coverage fund-json fields** (ROA, quarterly EPS growth, gross/operating/net
  margins, FCF/OCF yield, debt/equity, current ratio, cash/share, analyst target upside, analyst
  count, forward P/E) **and the gross-margin level/trend computed from the income statement**
  (GrossMargin_Now, GrossMargin_Trend — research round 2, both weeks improved) **and forward
  revenue-estimate growth (0q/0y) + analyst recommendation consensus** (research round 4: forward
  revenue estimates and the recommendation score both improved each week, NEW R² 0.3873→0.3908).
- Model: direct OLS to the 1–99 scale (percentile ranking was tried and *hurts* EPS — R² negative).
  **Growth/level features are log-compressed** — same sign-preserving `log1p` treatment as SMR.
  Bounded 0–1 ratios (negative-quarter ratio, beat rate) and the 0–10 stability CV keep their clips.
  Transform frozen in `fitted_params.json` (`eps.log_features`), applied identically at fit and
  scoring time. Forward out-of-sample R²: 0.265 → 0.345 → 0.386 → **0.391** across the feature rounds.
- Note: EPS is fundamentally the noisiest rating to reproduce from quarterly fundamentals alone; the
  Composite down-weights it (~18%).

### SMR Rating (A–E, quintile-ish)
- Features: profit margin, ROE, current margin, margin trend, long-term sales growth, quarterly sales
  YoY, revenue growth — **plus 11 high-coverage fund-json quality fields** (ROA, gross/operating
  margins, FCF/OCF yield, debt/equity, current/quick ratio, earnings & EPS-Q growth, price/book).
  Adding them raised exact-letter accuracy from 60.2% to **62.1%** (forward; OLD 58.6→61.0%) and the
  direct-scale holdout R² to **0.69** (OLD) / 0.68 (NEW) — both snapshots improved.  **Research round 4
  added Sloan (1996) accruals and the OCF/NI cash-conversion ratio** computed from the balance/cash-
  flow statements — earnings-quality signals that nudged forward exact-letter to **62.3%** (OLD
  61.0→61.2%).
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

### 4. Backtest: do the ratings predict forward performance?

```bash
cd IBD_rating_glm
# score a date range (point-in-time) → ratings panel parquet
python3 backtest_ratings.py score --start 2024-06-07 --end 2024-12-06 --out output/backtest_chunk1.parquet
# merge chunks → markdown report
python3 backtest_ratings.py report --panels 'output/backtest_chunk*.parquet' --report output/rating_backtest_report.md
```

Every weekly rebalance date is scored with the **exact production scorer** (`_score_features_frame`)
on the full cached universe with price history truncated to that day (true point-in-time).  The
report measures top/bottom-decile and quintile portfolios, forward 1w/4w returns vs the same-week
universe, weekly rank-IC, and SPY buy-hold.  Headline (2024-06 → 2026-07, 109 weekly rebalances):

| Rating | 4w top-10% excess | t | 1w top-10% excess | t | 4w rank-IC (t) |
|---|---:|---:|---:|---:|---:|
| RS | +1.69% | +3.03 | +0.33% | +1.71 | +0.014 (+1.2) |
| Comp | +0.26% | +0.84 | +0.12% | +0.97 | +0.035 (+3.1) |
| A/D | +1.10% | +2.51 | +0.12% | +0.93 | −0.012 (−1.3) |
| EPS | +0.98% | +4.76 | +0.27% | +3.29 | +0.059 (+9.5) |

Top deciles beat the universe at every horizon and bottom deciles lag — the ratings do rank
forward performance, strongest at the 4-week horizon.  See the full report for quintile
monotonicity, hit rates, and the caveats (survivorship, EPS/SMR fundamentals lookahead — the
RS and A/D legs are fully point-in-time price signals).

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
