# TV Pattern History Backtest — Target Outcome Characteristics

**Generated 2026-08-09 22:04** · history `2026-08-09T15:21:57` · universe 3,833 tickers · hold window 60 bars

> Scoring (identical to the dashboard's History tab): a breakout **Target** = touched **+20%** above its pivot before touching −8%, within 60 bars, first touch wins (a bar that spans both levels scores a stop). **Win rate** = Target / (Target + Stop) over **resolved** breakouts only; Open bases are reported but never counted either way. CIs are Wilson 95%.

## 1 · Headline

|  | n |
|---|---:|---:
| Bases recorded | 27,091 |
| — breakouts | 19,722 |
| — breakdowns (depth / length) | 7,369 |
| Resolved breakouts (Target + Stop) | 14,741 |
| **Target** (+20% first) | **4,364** (22.1% of all breakouts) |
| Stop (−8% first) | 10,377 |
| Still open after 60 bars | 4,981 |
| **Resolved win rate (baseline)** | **29.6%** |

### Win rate by breakout year (regime check)

| Year | Breakouts | Resolved | Target | Win % | CI |
|---|---:|---:|---:|---:|---|
| 2020 | 640 | 573 | 329 | 57.4% | 53.3%–61.4% |
| 2021 | 4,046 | 2,892 | 793 | 27.4% | 25.8%–29.1% |
| 2022 | 1,945 | 1,645 | 297 | 18.1% | 16.3%–20.0% |
| 2023 | 3,205 | 2,303 | 668 | 29.0% | 27.2%–30.9% |
| 2024 | 3,982 | 2,949 | 901 | 30.6% | 28.9%–32.2% |
| 2025 | 3,248 | 2,527 | 762 | 30.2% | 28.4%–32.0% |
| 2026 | 2,656 | 1,852 | 614 | 33.2% | 31.0%–35.3% |

*Coverage note: the ~6-year bar window means only tickers with longer cached history contribute 2020 breakouts, so 2020 is the smallest, most survivorship-biased slice — treat its 57% as a COVID-recovery regime reading, not a norm.*

## 2 · By pattern

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| Base | 13,338 | 8,840 | 6,530 | 2,019 | 30.9% | 29.8%–32.1% | 1.3 | 22 | 21.7 | -1.9 |
| Cup | 13,725 | 10,854 | 8,191 | 2,340 | 28.6% | 27.6%–29.6% | -1 | 22 | 21.5 | -1.9 |

## 3 · By base shape

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| Consolidation | 8,734 | 4,238 | 3,574 | 1,220 | 34.1% | 32.6%–35.7% | 4.5 | 17 | 21.9 | -1.8 |
| (none) | 13,753 | 10,882 | 8,211 | 2,345 | 28.6% | 27.6%–29.5% | -1 | 22 | 21.5 | -1.8 |
| Flat Base | 4,604 | 4,602 | 2,956 | 799 | 27.0% | 25.5%–28.7% | -2.6 | 31 | 21.3 | -2.1 |

## 4 · By base depth %

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 25-30 | 2,641 | 2,333 | 2,008 | 683 | 34.0% | 32.0%–36.1% | 4.4 | 13 | 21.9 | -1.6 |
| 30-35 | 1,989 | 1,610 | 1,414 | 463 | 32.7% | 30.3%–35.2% | 3.1 | 12 | 22.5 | -1.6 |
| 20-25 | 3,492 | 3,364 | 2,766 | 879 | 31.8% | 30.1%–33.5% | 2.2 | 21 | 21.8 | -2.2 |
| 15-20 | 4,513 | 4,473 | 3,408 | 1,012 | 29.7% | 28.2%–31.3% | 0.1 | 23.5 | 21.4 | -1.9 |
| 10-15 | 5,067 | 5,058 | 3,526 | 918 | 26.0% | 24.6%–27.5% | -3.6 | 30 | 21.3 | -1.9 |
| 0-10 | 2,881 | 2,877 | 1,613 | 407 | 25.2% | 23.2%–27.4% | -4.4 | 33 | 21.1 | -2 |

## 5 · By base length (days)

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 30-45 | 7,110 | 5,760 | 4,473 | 1,427 | 31.9% | 30.6%–33.3% | 2.3 | 20 | 21.6 | -2.1 |
| 45-60 | 4,275 | 3,345 | 2,579 | 806 | 31.3% | 29.5%–33.1% | 1.6 | 20 | 21.8 | -1.9 |
| 60-90 | 5,218 | 3,754 | 2,769 | 862 | 31.1% | 29.4%–32.9% | 1.5 | 27 | 21.3 | -1.7 |
| 0-30 | 2,647 | 2,253 | 1,782 | 540 | 30.3% | 28.2%–32.5% | 0.7 | 21 | 21.7 | -2.1 |
| 120-160 | 1,762 | 1,200 | 833 | 205 | 24.6% | 21.8%–27.6% | -5 | 29 | 21.3 | -1.7 |
| 90-120 | 2,805 | 1,987 | 1,381 | 338 | 24.5% | 22.3%–26.8% | -5.1 | 26 | 21.6 | -1.7 |
| 160-200 | 1,061 | 631 | 431 | 95 | 22.0% | 18.4%–26.2% | -7.6 | 21 | 21.2 | -1.8 |
| >= 200 | 2,213 | 792 | 493 | 91 | 18.5% | 15.3%–22.1% | -11.1 | 33 | 21.3 | -1.8 |

## 6 · By accumulation days

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 0-5 | 856 | 322 | 253 | 81 | 32.0% | 26.6%–38.0% | 2.4 | 20 | 21.6 | -2.7 |
| 10-15 | 6,510 | 5,266 | 4,062 | 1,284 | 31.6% | 30.2%–33.1% | 2 | 20 | 21.7 | -1.8 |
| 15-20 | 3,893 | 3,023 | 2,304 | 705 | 30.6% | 28.8%–32.5% | 1 | 24 | 21.4 | -1.8 |
| 5-10 | 7,411 | 5,717 | 4,441 | 1,355 | 30.5% | 29.2%–31.9% | 0.9 | 21 | 21.7 | -2.1 |
| 20-30 | 3,787 | 2,844 | 1,976 | 560 | 28.3% | 26.4%–30.4% | -1.3 | 26.5 | 21.5 | -1.9 |
| >= 30 | 4,634 | 2,550 | 1,705 | 379 | 22.2% | 20.3%–24.3% | -7.4 | 28 | 21.3 | -1.6 |

## 7 · By neutral days

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 15-25 | 8,672 | 7,033 | 5,453 | 1,731 | 31.7% | 30.5%–33.0% | 2.1 | 20 | 21.7 | -2.1 |
| 0-15 | 1,963 | 1,680 | 1,324 | 408 | 30.8% | 28.4%–33.4% | 1.2 | 20 | 21.6 | -1.9 |
| 25-40 | 6,691 | 5,064 | 3,891 | 1,197 | 30.8% | 29.3%–32.2% | 1.2 | 23 | 21.5 | -1.7 |
| 40-60 | 4,398 | 3,089 | 2,160 | 598 | 27.7% | 25.8%–29.6% | -1.9 | 27 | 21.4 | -1.7 |
| >= 60 | 5,367 | 2,856 | 1,913 | 430 | 22.5% | 20.7%–24.4% | -7.1 | 26 | 21.4 | -1.8 |

## 8 · By distribution days

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 0-5 | 548 | 533 | 437 | 146 | 33.4% | 29.1%–38.0% | 3.8 | 19 | 22.1 | -2.4 |
| 5-10 | 6,692 | 6,135 | 4,795 | 1,541 | 32.1% | 30.8%–33.5% | 2.5 | 19 | 21.8 | -2.1 |
| 15-20 | 3,828 | 2,729 | 2,039 | 634 | 31.1% | 29.1%–33.1% | 1.5 | 26 | 21.3 | -1.6 |
| 10-15 | 6,255 | 4,812 | 3,689 | 1,110 | 30.1% | 28.6%–31.6% | 0.5 | 21 | 21.6 | -1.8 |
| 20-30 | 4,419 | 2,923 | 2,067 | 561 | 27.1% | 25.3%–29.1% | -2.5 | 26 | 21.5 | -1.8 |
| >= 30 | 5,349 | 2,590 | 1,714 | 372 | 21.7% | 19.8%–23.7% | -7.9 | 30 | 21.3 | -1.8 |

## 9 · By accumulation ratio

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| >= 30% | 2,389 | 2,354 | 1,818 | 616 | 33.9% | 31.7%–36.1% | 4.3 | 20 | 21.7 | -1.9 |
| < 15% | 1,665 | 530 | 412 | 131 | 31.8% | 27.5%–36.4% | 2.2 | 23 | 21.8 | -2.3 |
| 25-30% | 7,289 | 6,640 | 4,988 | 1,504 | 30.2% | 28.9%–31.4% | 0.5 | 21 | 21.5 | -1.9 |
| 20-25% | 10,897 | 7,625 | 5,602 | 1,579 | 28.2% | 27.0%–29.4% | -1.4 | 24 | 21.5 | -1.9 |
| 15-20% | 4,851 | 2,573 | 1,921 | 534 | 27.8% | 25.8%–29.8% | -1.8 | 23 | 21.7 | -1.8 |

## 10 · By distribution ratio

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| < 15% | 877 | 837 | 648 | 224 | 34.6% | 31.0%–38.3% | 5 | 19 | 22.1 | -2.1 |
| 15-20% | 3,145 | 3,011 | 2,318 | 753 | 32.5% | 30.6%–34.4% | 2.9 | 20 | 21.9 | -2 |
| >= 30% | 4,432 | 1,941 | 1,475 | 452 | 30.6% | 28.3%–33.0% | 1 | 25 | 21.5 | -2.1 |
| 20-25% | 9,271 | 7,797 | 5,819 | 1,698 | 29.2% | 28.0%–30.4% | -0.4 | 21 | 21.5 | -1.9 |
| 25-30% | 9,366 | 6,136 | 4,481 | 1,237 | 27.6% | 26.3%–28.9% | -2 | 25 | 21.4 | -1.7 |

## 11 · By acc − dis (net)

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| >= 10 | 700 | 609 | 411 | 130 | 31.6% | 27.3%–36.3% | 2 | 21.5 | 22 | -1.3 |
| 0-5 | 8,710 | 7,956 | 6,047 | 1,883 | 31.1% | 30.0%–32.3% | 1.5 | 20 | 21.6 | -1.9 |
| 5-10 | 2,849 | 2,663 | 2,027 | 618 | 30.5% | 28.5%–32.5% | 0.9 | 22 | 21.5 | -2 |
| -5-0 | 8,389 | 6,250 | 4,651 | 1,316 | 28.3% | 27.0%–29.6% | -1.3 | 24 | 21.5 | -1.7 |
| -10--5 | 4,139 | 1,764 | 1,252 | 331 | 26.4% | 24.1%–28.9% | -3.2 | 27 | 21.5 | -2.3 |
| < -10 | 2,304 | 480 | 353 | 86 | 24.4% | 20.2%–29.1% | -5.2 | 26.5 | 21.5 | -1.9 |

## 12 · By cup strength (cup_bars)

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 5-10 | 3,072 | 2,506 | 1,943 | 610 | 31.4% | 29.4%–33.5% | 1.8 | 20 | 21.6 | -1.8 |
| 1-5 | 4,153 | 3,266 | 2,573 | 799 | 31.1% | 29.3%–32.9% | 1.4 | 20 | 21.5 | -2 |
| 0-1 | 13,350 | 8,852 | 6,537 | 2,021 | 30.9% | 29.8%–32.0% | 1.3 | 22 | 21.7 | -1.9 |
| 10-20 | 3,565 | 2,898 | 2,195 | 580 | 26.4% | 24.6%–28.3% | -3.2 | 23 | 21.5 | -1.9 |
| >= 20 | 2,951 | 2,200 | 1,493 | 354 | 23.7% | 21.6%–25.9% | -5.9 | 28 | 21.4 | -1.7 |

## 13 · By cup right/left balance

| Bucket | Patterns | Breakouts | Resolved | Target | Win % | CI | Lift (pp) | Med bars→tgt | Med gain % | Med DD % |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| < 0.5x | 4,481 | 2,059 | 1,565 | 513 | 32.8% | 30.5%–35.1% | 3.2 | 22 | 21.5 | -1.8 |
| (no cup) | 13,402 | 8,904 | 6,583 | 2,034 | 30.9% | 29.8%–32.0% | 1.3 | 22 | 21.7 | -1.9 |
| 1.5-2x | 1,280 | 1,220 | 907 | 269 | 29.7% | 26.8%–32.7% | 0.1 | 21 | 21.6 | -2 |
| >= 2x | 3,240 | 3,051 | 2,189 | 612 | 28.0% | 26.1%–29.9% | -1.6 | 24.5 | 21.6 | -1.7 |
| 0.5-1x | 2,739 | 2,582 | 2,010 | 543 | 27.0% | 25.1%–29.0% | -2.6 | 21 | 21.5 | -1.9 |
| 1-1.5x | 1,949 | 1,906 | 1,487 | 393 | 26.4% | 24.3%–28.7% | -3.2 | 20 | 21.5 | -2 |

## 14 · What a Target looks like (speed & path)

**Speed to +20%** (`bars_to_outcome`, n=4,364 targets): median **22 bars**, p25 10, p75 39. Share within 5 bars **14%**, within 10 **26%**, within 20 **47%**.

| Bars to +20% | Targets | Share |
|---|---:|---:
| 1-5 | 630 | 14% |
| 6-10 | 500 | 11% |
| 11-20 | 925 | 21% |
| 21-30 | 710 | 16% |
| 31-40 | 610 | 14% |
| 41-60 | 989 | 23% |

**Final gain at target** (`max_gain_pct`, n=4,364): median **21.6%**, p90 27.7%.

**Drawdown before the target** (`max_drawdown_pct`, n=4,364): median -1.9%. **20%** of winners dipped at least −5% below the pivot before recovering, and **1%** came within a whisker of the −8% stop (≤ −7.9%) without touching it — winners routinely draw down before paying off, so being stopped at −8% is not proof the trade was wrong.

## 15 · Pre-BO signals — fired during the base, within ±20% of pivot

> Recomputed from ticker_cache with the scanner's own definitions (see script header). A signal counts if it fired on any bar of the base whose close sat within 20% of the pivot — the Pine's `near_pivot` gate (approximated with the recorded base window; the scanner's own gate also includes its separate higher-timeframe flag, which can fire outside the base and is not recorded in the history). `vol_spike` = a day above 1.5× the 50-day average volume (the scanner's breakout-volume multiplier); it is added here because `vol_dry_up` alone only captures the quiet side. Win rates are Target / (Target + Stop); the CSV carries the 'with' rows and the 'Δ vs w/o' column compares against the without group computed here.

*Signals that fire on almost every base (pocket_pivot, ma_touch, upside_reversal) have tiny 'without' groups, so their Δ is less meaningful than for the rarer signals.*

| Signal (base window) | Breakouts w/ | Resolved w/ | Target w/ | Win % w/ | Win % w/o | Δ vs w/o (pp) | Med bars→tgt w/ |
|---|---:|---:|---:|---:|---:|---:|---:|
| **pocket_pivot** | 19,696 | 14,720 | 4,357 | 29.6% | — | — | 22 |
| **shakeout** | 10,076 | 7,378 | 2,094 | 28.4% | 30.8% | -2.4 | 23 |
| **ma_touch** | 19,721 | 14,740 | 4,364 | 29.6% | — | — | 22 |
| **vol_dry_up** | 18,066 | 13,539 | 3,936 | 29.1% | 35.6% | -6.5 | 22 |
| **rs_new_high** | 14,261 | 10,832 | 3,328 | 30.7% | 26.5% | +4.2 | 21 |
| **upside_reversal** | 19,617 | 14,674 | 4,334 | 29.5% | 44.8% | -15.2 | 22 |
| **vol_spike** | 19,088 | 14,220 | 4,191 | 29.5% | 33.2% | -3.7 | 22 |

## 16 · Pre-BO signals — last 20 bars before the breakout

| Signal (last 20 bars) | Breakouts w/ | Resolved w/ | Target w/ | Win % w/ | Win % w/o | Δ vs w/o (pp) | Med bars→tgt w/ |
|---|---:|---:|---:|---:|---:|---:|---:|
| **pocket_pivot** | 19,493 | 14,570 | 4,313 | 29.6% | 29.8% | -0.2 | 22 |
| **shakeout** | 4,857 | 3,680 | 1,058 | 28.7% | 29.9% | -1.1 | 22 |
| **ma_touch** | 19,572 | 14,613 | 4,314 | 29.5% | 39.1% | -9.5 | 22 |
| **vol_dry_up** | 14,377 | 10,899 | 3,244 | 29.8% | 29.2% | +0.6 | 22 |
| **rs_new_high** | 9,055 | 7,025 | 2,351 | 33.5% | 26.1% | +7.4 | 20 |
| **upside_reversal** | 19,246 | 14,442 | 4,260 | 29.5% | 34.8% | -5.3 | 22 |
| **vol_spike** | 16,065 | 12,055 | 3,657 | 30.3% | 26.3% | +4.0 | 22 |

## 17 · How many pre-BO signals fired (base window)

*(Counts 0-3 are omitted from the table — they are rarer than the minimum resolved-count cut; all eight counts are in the CSV.)*

| Signals | Breakouts | Resolved | Target | Win % | Lift (pp) |
|---|---:|---:|---:|---:|---:|
| 4 | 530 | 386 | 116 | 30.1% | +0.4 |
| 5 | 3,560 | 2,618 | 776 | 29.6% | +0.0 |
| 6 | 8,718 | 6,597 | 1,999 | 30.3% | +0.7 |
| 7 | 6,889 | 5,117 | 1,462 | 28.6% | -1.0 |

## 18 · Pre-BO signal combinations

**Greedy chain** (each rule must add lift on ≥ 200 resolved remaining):

*(no signal rule improved win rate by ≥ 2.0pp)*

**Top pairwise combos:**

| Pair | Resolved | Target | Win % | Lift (pp) |
|---|---:|---:|---:|---:|
| pocket_pivot + rs_new_high | 10,819 | 3,325 | 30.7% | +1.1 |
| ma_touch + rs_new_high | 10,832 | 3,328 | 30.7% | +1.1 |
| rs_new_high + upside_reversal | 10,789 | 3,307 | 30.7% | +1.0 |
| rs_new_high + vol_spike | 10,496 | 3,206 | 30.5% | +0.9 |
| vol_dry_up + rs_new_high | 9,959 | 2,993 | 30.1% | +0.4 |
| pocket_pivot + ma_touch | 14,719 | 4,357 | 29.6% | -0.0 |
| ma_touch + upside_reversal | 14,673 | 4,334 | 29.5% | -0.1 |
| pocket_pivot + upside_reversal | 14,653 | 4,327 | 29.5% | -0.1 |
| ma_touch + vol_spike | 14,219 | 4,191 | 29.5% | -0.1 |
| pocket_pivot + vol_spike | 14,202 | 4,185 | 29.5% | -0.1 |
| upside_reversal + vol_spike | 14,159 | 4,164 | 29.4% | -0.2 |
| shakeout + rs_new_high | 5,602 | 1,642 | 29.3% | -0.3 |

## 19 · Signals × strongest structural buckets

Win rate WITH the signal vs WITHOUT, inside each bucket — does the signal add anything on top of the structural edge?

| Bucket | Signal | Resolved | Win % w/ | Win % w/o | Δ (pp) |
|---|---|---:|---:|---:|---:|
| Consolidation | shakeout | 1,774 | 32.6% | 35.7% | -3.1 |
| Consolidation | vol_dry_up | 3,192 | 33.7% | 38.0% | -4.3 |
| Consolidation | rs_new_high | 2,719 | 36.3% | 27.1% | +9.2 |
| depth 25-35% | shakeout | 1,565 | 32.7% | 34.2% | -1.5 |
| depth 25-35% | vol_dry_up | 3,132 | 32.6% | 42.8% | -10.1 |
| depth 25-35% | rs_new_high | 2,961 | 33.9% | 31.0% | +2.9 |
| dis ratio < 15% | shakeout | 278 | 31.7% | 36.8% | -5.1 |
| days 30-90 | shakeout | 4,596 | 30.2% | 32.7% | -2.5 |
| days 30-90 | vol_dry_up | 8,970 | 30.7% | 39.7% | -9.0 |
| days 30-90 | rs_new_high | 6,753 | 33.7% | 26.8% | +6.9 |
| days 30-90 | vol_spike | 9,481 | 31.4% | 35.9% | -4.5 |
| any signal fired | shakeout | 7,378 | 28.4% | 30.8% | -2.4 |
| any signal fired | vol_dry_up | 13,539 | 29.1% | 35.6% | -6.5 |
| any signal fired | rs_new_high | 10,832 | 30.7% | 26.5% | +4.2 |
| any signal fired | vol_spike | 14,220 | 29.5% | 33.2% | -3.7 |

## 20 · Market regime at the breakout (SPY vs critical MAs)

> SPY close vs its 50-day and 200-day SMA on the breakout's end date, and how far from the 200-day it sat. Baseline is 29.6%.

| Regime bucket | Breakouts | Resolved | Target | Win % | CI | Lift (pp) |
|---|---:|---:|---:|---:|---|---:|
| SPY above SMA50 | 16,937 | 12,581 | 3,771 | 30.0% | 29.2%–30.8% | +0.4 |
| SPY below SMA50 | 2,785 | 2,160 | 593 | 27.5% | 25.6%–29.4% | -2.2 |
| SPY above SMA200 | 17,739 | 13,131 | 3,973 | 30.3% | 29.5%–31.0% | +0.7 |
| SPY below SMA200 | 1,983 | 1,610 | 391 | 24.3% | 22.3%–26.4% | -5.3 |
| SMA50 > SMA200 (golden) | 17,356 | 12,887 | 3,948 | 30.6% | 29.8%–31.4% | +1.0 |
| SMA50 < SMA200 (death) | 2,366 | 1,854 | 416 | 22.4% | 20.6%–24.4% | -7.2 |
| >10% below 200 | 898 | 730 | 164 | 22.5% | 19.6%–25.6% | -7.1 |
| 3-10% below 200 | 910 | 750 | 191 | 25.5% | 22.5%–28.7% | -4.1 |
| 0-3% below 200 | 1,059 | 832 | 245 | 29.4% | 26.5%–32.6% | -0.2 |
| 0-3% above 200 | 8,624 | 6,309 | 1,914 | 30.3% | 29.2%–31.5% | +0.7 |
| 3-10% above 200 | 8,056 | 5,990 | 1,814 | 30.3% | 29.1%–31.5% | +0.7 |

*Regime known for 19,722 of 19,722 breakouts (100%)*

*Lift here is vs the 29.6% baseline; the takeaways and §24 use the same vs-baseline convention, while the signal tables in §15/§16 show Δ vs the without-group instead — the two are different measures.*

## 21 · Stock price at the breakout (buy point / pivot)

> Buckets on the base's `pivot` — the actual buy point a trader pays, which is the price that matters for position sizing, not the close.

| Price bucket | Breakouts | Resolved | Target | Win % | CI | Lift (pp) |
|---|---:|---:|---:|---:|---|---:|
| $0-10 | 1,072 | 952 | 415 | 43.6% | 40.5%–46.8% | +14.0 |
| $10-25 | 4,766 | 3,705 | 1,182 | 31.9% | 30.4%–33.4% | +2.3 |
| $25-50 | 5,139 | 3,871 | 1,086 | 28.1% | 26.7%–29.5% | -1.5 |
| $50-100 | 4,158 | 2,992 | 834 | 27.9% | 26.3%–29.5% | -1.7 |
| $100-250 | 3,319 | 2,339 | 628 | 26.8% | 25.1%–28.7% | -2.8 |
| $>= 250 | 1,268 | 882 | 219 | 24.8% | 22.1%–27.8% | -4.8 |

*Caveat: the $0-10 bucket is the smallest, and it is where the most marginal, highest-volatility names live — the +14 pp win rate is real but likely comes with wider drawdowns and more noise; the $10-25 bucket is the cheapest one with institutional quality still plausible.*

## 22 · Scenario matrix — market regime × stock price

Every cell is a real scenario (regime of the day it broke out × the buy point it broke out from), sorted by win rate. Cells that fall below the minimum resolved count are omitted.

| Scenario (regime × price) | Resolved | Target | Win % | Lift (pp) |
|---|---:|---:|---:|---:|
| Bull × $0-10 | 746 | 336 | 45.0% | +15.4 |
| Mixed × $10-25 | 467 | 155 | 33.2% | +3.6 |
| Bull × $10-25 | 3,012 | 966 | 32.1% | +2.5 |
| Bull × $50-100 | 2,435 | 706 | 29.0% | -0.6 |
| Bull × $25-50 | 3,018 | 870 | 28.8% | -0.8 |
| Bull × $100-250 | 1,923 | 541 | 28.1% | -1.5 |
| Mixed × $25-50 | 570 | 154 | 27.0% | -2.6 |
| Bear × $10-25 | 226 | 61 | 27.0% | -2.6 |
| Bull × $>= 250 | 753 | 192 | 25.5% | -4.1 |
| Bear × $25-50 | 283 | 62 | 21.9% | -7.7 |
| Mixed × $50-100 | 369 | 78 | 21.1% | -8.5 |
| Mixed × $100-250 | 314 | 64 | 20.4% | -9.2 |

## 23 · Scenario combinations — regime × structure / signals

Does the structural edge survive in a bear market? These rows cross each regime with the strongest structural buckets (and with 'any pre-BO signal fired' when the signal pass ran).

| Scenario (regime × structure) | Resolved | Target | Win % | Lift (pp) |
|---|---:|---:|---:|---:|
| Bull × Consolidation | 2,878 | 1,009 | 35.1% | +5.5 |
| Bull × depth 25-35% | 2,654 | 912 | 34.4% | +4.8 |
| Mixed × depth 25-35% | 511 | 166 | 32.5% | +2.9 |
| Bull × days 30-90 | 7,990 | 2,570 | 32.2% | +2.6 |
| Mixed × Consolidation | 442 | 142 | 32.1% | +2.5 |
| Bull × any pre-BO signal | 11,887 | 3,611 | 30.4% | +0.8 |
| Mixed × days 30-90 | 1,245 | 360 | 28.9% | -0.7 |
| Bear × days 30-90 | 586 | 165 | 28.2% | -1.4 |
| Bear × Consolidation | 254 | 69 | 27.2% | -2.4 |
| Mixed × any pre-BO signal | 1,938 | 522 | 26.9% | -2.7 |
| Bear × depth 25-35% | 257 | 68 | 26.5% | -3.1 |
| Bear × any pre-BO signal | 916 | 231 | 25.2% | -4.4 |

## 24 · Top single-bucket filters

| Rank | Filter | Breakouts | Resolved | Target | Win % | Lift (pp) |
|---|---|---:|---:|---:|---:|---:|
| 1 | *breakout_year*: 2020 | 640 | 573 | 329 | 57.4% | +27.8 |
| 2 | *market.price*: $0-10 | 1,072 | 952 | 415 | 43.6% | +14.0 |
| 3 | *dis_ratio*: < 15% | 837 | 648 | 224 | 34.6% | +5.0 |
| 4 | *base_shape*: Consolidation | 4,238 | 3,574 | 1,220 | 34.1% | +4.5 |
| 5 | *depth_pct*: 25-30 | 2,333 | 2,008 | 683 | 34.0% | +4.4 |
| 6 | *acc_ratio*: >= 30% | 2,354 | 1,818 | 616 | 33.9% | +4.3 |
| 7 | *prebo.last20*: rs_new_high | 9,055 | 7,025 | 2,351 | 33.5% | +3.9 |
| 8 | *dis_days*: 0-5 | 533 | 437 | 146 | 33.4% | +3.8 |
| 9 | *breakout_year*: 2026 | 2,656 | 1,852 | 614 | 33.2% | +3.5 |
| 10 | *cup_right_left*: < 0.5x | 2,059 | 1,565 | 513 | 32.8% | +3.2 |
| 11 | *depth_pct*: 30-35 | 1,610 | 1,414 | 463 | 32.7% | +3.1 |
| 12 | *dis_ratio*: 15-20% | 3,011 | 2,318 | 753 | 32.5% | +2.9 |
| 13 | *dis_days*: 5-10 | 6,135 | 4,795 | 1,541 | 32.1% | +2.5 |
| 14 | *acc_days*: 0-5 | 322 | 253 | 81 | 32.0% | +2.4 |
| 15 | *market.price*: $10-25 | 4,766 | 3,705 | 1,182 | 31.9% | +2.3 |

*The 2020 row carries the year-table caveat: it is the smallest, most coverage-biased slice (COVID-recovery regime) — the non-year rows below it (structure, pre-BO signals, market/price) are the ones a trader can screen on.*

## 25 · Greedy rule chains (each rule adds lift on ≥ 200 resolved remaining)

### 25a · With the regime dimension (what the data shows)

| Step | Rule | Resolved left | Target | Win % | Lift vs prior (pp) |
|---|---|---:|---:|---:|---:|
| 1 | *breakout_year*: 2020 | 573 | 329 | 57.4% | +27.8 |
| 2 | *acc_days*: 10-15 | 215 | 140 | 65.1% | +7.7 |

**Final filtered population:** 65.1% win rate (215 resolved breakouts, 140 targets) vs 29.6% baseline.

### 25b · Actionable only, year excluded (what you can screen on)

*`breakout_year` is dropped so the chain reflects base characteristics, pre-BO signals, market regime and price buckets a trader can screen on today, rather than the year the breakout happened to occur in (2020's +27.8 pp is a COVID-recovery regime, and 2020 only reaches back through the ~6-year bar window, so it is also the smallest and most coverage-biased year).*

| Step | Rule | Resolved left | Target | Win % | Lift vs prior (pp) |
|---|---|---:|---:|---:|---:|
| 1 | *market.price*: $0-10 | 952 | 415 | 43.6% | +14.0 |
| 2 | *prebo.last20*: rs_new_high | 459 | 229 | 49.9% | +6.3 |
| 3 | *cup_bars*: 0-1 | 201 | 109 | 54.2% | +4.3 |

**Final filtered population:** 54.2% win rate (201 resolved breakouts, 109 targets) vs 29.6% baseline.

## 26 · Curated cross-tabs (the pairs a trader screens on)

**depth × days**

| Combo | Resolved | Target | Win % | Lift (pp) |
|---|---|---:|---:|---:|
| depth20-30 · len<90d | 3,370 | 1,227 | 36.4% | +6.8 |
| depth<30 · len<150d | 12,547 | 3,747 | 29.9% | +0.3 |
| depth<25 · len<90d | 9,552 | 2,835 | 29.7% | +0.1 |
| depth<25 · len<120d | 10,396 | 3,030 | 29.1% | -0.5 |
| depth<25 · len<150d | 10,817 | 3,121 | 28.9% | -0.8 |
| depth<20 · len<90d | 7,460 | 2,114 | 28.3% | -1.3 |
| depth<20 · len<60d | 6,000 | 1,696 | 28.3% | -1.3 |
| depth>=30 · len>=90d | 647 | 171 | 26.4% | -3.2 |
| depth<15 · len<90d | 4,539 | 1,199 | 26.4% | -3.2 |
| depth<15 · len<60d | 3,725 | 978 | 26.3% | -3.3 |

**depth × pattern**

| Combo | Resolved | Target | Win % | Lift (pp) |
|---|---|---:|---:|---:|
| depth>=25 · Base | 971 | 386 | 39.8% | +10.1 |
| depth>=25 · Cup | 2,453 | 761 | 31.0% | +1.4 |
| depth<25 · Base | 5,559 | 1,633 | 29.4% | -0.2 |
| depth<20 · Base | 4,529 | 1,277 | 28.2% | -1.4 |
| depth<25 · Cup | 5,738 | 1,579 | 27.5% | -2.1 |
| depth<15 · Base | 2,956 | 799 | 27.0% | -2.6 |
| depth<20 · Cup | 4,003 | 1,056 | 26.4% | -3.2 |
| depth<15 · Cup | 2,176 | 525 | 24.1% | -5.5 |

**dis-ratio × depth**

| Combo | Resolved | Target | Win % | Lift (pp) |
|---|---|---:|---:|---:|
| dis<15% · depth<20 | 404 | 135 | 33.4% | +3.8 |
| dis<15% · depth<25 | 521 | 168 | 32.2% | +2.6 |
| dis<20% · depth<25 | 2,328 | 715 | 30.7% | +1.1 |
| dis<20% · depth<20 | 1,777 | 523 | 29.4% | -0.2 |
| dis>=25% (all) | 5,956 | 1,689 | 28.4% | -1.2 |
| dis>=25% · depth<20 | 3,695 | 981 | 26.5% | -3.1 |

**shape × days**

| Combo | Resolved | Target | Win % | Lift (pp) |
|---|---|---:|---:|---:|
| Consolidation · len<60d | 2,555 | 906 | 35.5% | +5.9 |
| Consolidation · len<90d | 3,111 | 1,097 | 35.3% | +5.7 |
| Flat · len<60d | 2,408 | 673 | 27.9% | -1.7 |
| Flat · len<90d | 2,776 | 769 | 27.7% | -1.9 |
| Flat Base (all) | 2,956 | 799 | 27.0% | -2.6 |
| Consolidation · len>=90d | 463 | 123 | 26.6% | -3.0 |

## 27 · Key takeaways

**Strongest Target characteristics** (win-rate lift over baseline):

- **market_price $0-10**: 43.6% win (952 resolved, 415 targets) = **+14.0 pp** vs 29.6% baseline
- **dis_ratio < 15%**: 34.6% win (648 resolved, 224 targets) = **+5.0 pp** vs 29.6% baseline
- **base_shape Consolidation**: 34.1% win (3,574 resolved, 1,220 targets) = **+4.5 pp** vs 29.6% baseline
- **depth_pct 25-30**: 34.0% win (2,008 resolved, 683 targets) = **+4.4 pp** vs 29.6% baseline
- **acc_ratio >= 30%**: 33.9% win (1,818 resolved, 616 targets) = **+4.3 pp** vs 29.6% baseline
- **prebo_signal rs_new_high (last20)**: 33.5% win (7,025 resolved, 2,351 targets) = **+3.9 pp** vs 29.6% baseline
- **dis_days 0-5**: 33.4% win (437 resolved, 146 targets) = **+3.8 pp** vs 29.6% baseline
- **cup_right_left < 0.5x**: 32.8% win (1,565 resolved, 513 targets) = **+3.2 pp** vs 29.6% baseline

**Weakest (below baseline):**

- **market_regime SMA50 < SMA200 (death)**: 22.4% win (1,854 resolved) = **-7.2 pp**
- **acc_days >= 30**: 22.2% win (1,705 resolved) = **-7.4 pp**
- **days 160-200**: 22.0% win (431 resolved) = **-7.6 pp**
- **dis_days >= 30**: 21.7% win (1,714 resolved) = **-7.9 pp**
- **days >= 200**: 18.5% win (493 resolved) = **-11.1 pp**

---
*Script: `python/backtests/tv_pattern_history_backtest.py` · input `tv_pattern_history.json` (27,091 bases, 19,722 breakouts, 14,741 resolved).*

*Caveat: every lift here is **in-sample** — the same 27k recorded bases were used to find the buckets and to measure them, and correlated dimensions (e.g. `dis_ratio`, `dis_days`, `acc_days`) are the same signal measured twice. Wilson CIs cover sampling noise on a single bucket; they do not cover the multiple-comparison or overfitting risk of the greedy chain. Treat the §25 chain as a hypothesis to validate on a holdout, not as a backtested edge.*
