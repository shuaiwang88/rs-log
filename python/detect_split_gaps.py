"""Find price discontinuities in ticker_cache/ caused by unadjusted splits.

update_ticker_cache.py refreshes incrementally - it fetches period='5d' and concatenates onto
the existing parquet - so rows older than five days are never re-fetched. When a split occurs
Yahoo back-adjusts ITS history, but our cached rows keep their pre-split prices forever, and
the file ends up with a step change nothing in the pipeline knows about.

The damage is not confined to HTF. Any pattern whose window spans the split date is measured
across two different price scales: a "cup" would show a fabricated 90% depth, a base top would
sit at a level price never traded. BANL is the clearest case - $0.36 to $3.82 overnight on
LOWER volume (963K then 113K), a 1-for-10 reverse split on 2026-07-20 - which the HTF detector
read as a +1907% pole into a textbook 21.3% flag.

Detection is deliberately conservative and offline. A split shows three things together:
    1. an overnight ratio close to a common split factor (2, 3, 4, 5, 10, 20 and inverses),
    2. volume that does NOT confirm - a real 10x move trades enormous size, a split does not,
    3. a gap between the prior close and the next OPEN, not an intraday range expansion.
All three must agree, because genuine gaps do exist (biotech readouts, buyouts) and
back-adjusting a real move would destroy true price history.

`--verify` then checks candidates against yfinance's own split calendar, which is
authoritative and turns a heuristic into a fact. Only a handful of tickers reach that step, so
the network cost is small.

This script NEVER writes to the cache. It reports. Adjusting is a separate, explicit step -
see the note at the bottom on why that is not automatic.

Usage:
    python3 python/detect_split_gaps.py                 # scan the whole cache
    python3 python/detect_split_gaps.py --min-ratio 1.5 # widen the net
    python3 python/detect_split_gaps.py --verify        # confirm against yfinance splits
    python3 python/detect_split_gaps.py --sym BANL      # one ticker, with the bars
"""
import argparse
import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "ticker_cache"

# Ratios a split actually uses. Anything else is a real move until proven otherwise.
SPLIT_FACTORS = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 50, 100]
FACTOR_TOL = 0.08          # within 8% of a clean factor
VOL_CONFIRM = 2.0          # a real move of this size trades >= 2x its 20d average


def _nearest_factor(ratio):
    """Closest clean split factor to `ratio`, and the relative error, in either direction."""
    cands = []
    for f in SPLIT_FACTORS:
        for val, label in ((float(f), f"1-for-{f} reverse"), (1.0 / f, f"{f}-for-1 forward")):
            cands.append((abs(ratio - val) / val, val, label))
    err, val, label = min(cands, key=lambda t: t[0])
    return err, val, label


def scan_one(df, min_ratio=1.8, min_price=1.0):
    """Return candidate split bars in one frame."""
    if len(df) < 30:
        return []
    c = df['Close'].to_numpy(dtype=float)
    o = df['Open'].to_numpy(dtype=float)
    h_ = df['High'].to_numpy(dtype=float)
    l_ = df['Low'].to_numpy(dtype=float)
    v = df['Volume'].to_numpy(dtype=float)
    dates = [str(d)[:10] for d in df.index]
    v20 = pd.Series(v).rolling(20).mean().to_numpy()
    out = []
    for i in range(1, len(c)):
        if not (np.isfinite(c[i - 1]) and np.isfinite(c[i]) and np.isfinite(o[i])):
            continue                      # NaN rows manufacture phantom gaps (WLFC)
        if c[i - 1] <= 0 or c[i] <= 0 or o[i] <= 0:
            continue
        # Sub-penny prices are quoted to four decimals, so 0.0001 -> 0.0100 is "exactly"
        # 1-for-100 by rounding alone. At that scale the tick IS the signal and every ratio
        # looks like a clean split factor, so the test is meaningless. Require a real price
        # on at least one side of the gap.
        if max(c[i - 1], c[i]) < min_price:
            continue
        # Match on the OVERNIGHT GAP, open vs the prior close. That is precisely what a split
        # does; the close-to-close ratio also carries whatever the stock did during the day,
        # which is enough to miss one - BANL's close ratio is 11.56, 16% off a clean 10, while
        # its open ratio is 10.6 and lands inside tolerance.
        ratio = o[i] / c[i - 1]
        if min_ratio > ratio > 1.0 / min_ratio:
            continue
        err, val, label = _nearest_factor(ratio)
        if err > FACTOR_TOL:
            continue                      # not a clean split factor -> treat as a real move
        # The whole session must sit on the new scale, not just the open.
        if abs(c[i] / c[i - 1] - val) / val > FACTOR_TOL * 4:
            continue
        rv = v[i] / v20[i] if (i < len(v20) and v20[i] and np.isfinite(v20[i])) else np.nan
        if np.isfinite(rv) and rv >= VOL_CONFIRM:
            continue                      # volume confirms -> a real move, leave it alone
        # Stale quotes, not corporate actions. An illiquid listing that stops trading keeps
        # printing the same number - INDV held O=H=L=C=19.6420 for four sessions, dropped to
        # 3.13 on ZERO volume, then went straight back to 21.19. That is a bad tick, and
        # Yahoo confirms INDV never split. Both shapes are unambiguous and neither can be a
        # split, since a split gap is always accompanied by real trading.
        if v[i] == 0:
            continue
        p = i - 1
        if o[p] == h_[p] == l_[p] == c[p]:
            continue
        if not np.isfinite(rv):
            # No 20-day average yet, so the volume test - the strongest discriminator here -
            # could not run. Silently letting the gap through is how PTLE was reported as a
            # split: scanned in a 200-bar window the split bar sat at index 15, v20 was NaN,
            # the test was skipped, and a genuine 17.3x-volume spike to $30.96 was recorded as
            # a 1-for-2 reverse. With the full history it is correctly rejected. Refuse to
            # judge rather than guess.
            continue
        out.append({'date': dates[i], 'bar': i, 'ratio': round(float(ratio), 3),
                    'factor': val, 'kind': label,
                    'prev_close': round(float(c[i - 1]), 4),
                    'open': round(float(o[i]), 4), 'close': round(float(c[i]), 4),
                    'vol_x': round(float(rv), 2) if np.isfinite(rv) else None})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-ratio', type=float, default=1.8)
    ap.add_argument('--min-price', type=float, default=1.0,
                    help='ignore gaps where both sides are under this price')
    ap.add_argument('--verify', action='store_true', help='confirm against yfinance splits')
    ap.add_argument('--sym', default='', help='one ticker, printed with surrounding bars')
    a = ap.parse_args()

    files = ([CACHE / f"{a.sym.upper()}_1d.parquet"] if a.sym
             else sorted(glob.glob(str(CACHE / "*_1d.parquet"))))
    hits = []
    scanned = 0
    for fp in files:
        sym = os.path.basename(str(fp)).replace('_1d.parquet', '')
        try:
            df = pd.read_parquet(fp).sort_index()
        except Exception:
            continue
        scanned += 1
        for h in scan_one(df, a.min_ratio, a.min_price):
            h['sym'] = sym
            hits.append(h)

    print(f"scanned {scanned} parquets, {len(hits)} suspected split discontinuities "
          f"across {len({h['sym'] for h in hits})} tickers\n")
    print(f"  {'sym':<8}{'date':<12}{'prev_cl':>9}{'open':>9}{'close':>9}"
          f"{'ratio':>8}{'vol_x':>7}  implied")
    for h in sorted(hits, key=lambda h: -abs(np.log(h['ratio']))):
        print(f"  {h['sym']:<8}{h['date']:<12}{h['prev_close']:>9.4f}{h['open']:>9.4f}"
              f"{h['close']:>9.4f}{h['ratio']:>8.2f}"
              f"{(h['vol_x'] if h['vol_x'] is not None else float('nan')):>7.2f}  {h['kind']}")

    if a.sym and hits:
        df = pd.read_parquet(files[0]).sort_index()
        b = hits[0]['bar']
        print(f"\n  bars around {hits[0]['date']}:")
        sl = df.iloc[max(0, b - 4):b + 4]
        for i, r in sl.iterrows():
            print(f"    {str(i)[:10]}  O {r['Open']:>9.4f}  H {r['High']:>9.4f}  "
                  f"L {r['Low']:>9.4f}  C {r['Close']:>9.4f}  V {r['Volume']:>12,.0f}")

    if a.verify and hits:
        try:
            import yfinance as yf
        except ImportError:
            print("\n  yfinance not available - cannot verify")
            return
        print(f"\n  verifying {len({h['sym'] for h in hits})} tickers against yfinance splits")
        for sym in sorted({h['sym'] for h in hits}):
            try:
                sp = yf.Ticker(sym).splits
            except Exception as e:
                print(f"    {sym:<8} lookup failed: {e}")
                continue
            ours = [h for h in hits if h['sym'] == sym]
            if sp is None or len(sp) == 0:
                print(f"    {sym:<8} yfinance reports NO splits -> our {len(ours)} hit(s) "
                      f"are suspect, do not adjust")
                continue
            known = {str(d)[:10]: float(x) for d, x in sp.items()}
            for h in ours:
                near = [(d, r) for d, r in known.items() if abs(
                    (pd.to_datetime(d) - pd.to_datetime(h['date'])).days) <= 5]
                if near:
                    d, r = near[0]
                    print(f"    {sym:<8} {h['date']} CONFIRMED - yfinance split {r} on {d}")
                else:
                    print(f"    {sym:<8} {h['date']} not in yfinance's split list "
                          f"(has: {sorted(known)[-3:]})")


if __name__ == '__main__':
    main()

# Why this does not adjust the cache itself
# ------------------------------------------------------------------------------------------
# Back-adjusting means multiplying every row before the split, in place, in the user's price
# history - the input to every metric in this project. Get the factor or the date wrong and
# the corruption is silent, permanent and worse than the problem being fixed, because a
# fabricated-but-plausible price series produces confident wrong answers rather than obvious
# ones. The scan is cheap and reversible; the write is neither.
#
# The real repair is upstream anyway: update_ticker_cache.py fetches period='5d' and concats,
# so history is never revisited. Options, in order of preference:
#   1. re-fetch affected tickers with period='max' (authoritative, no arithmetic on our side),
#   2. use yfinance's split calendar to back-adjust only CONFIRMED splits,
#   3. flag affected tickers and have the scanner refuse to read across the discontinuity.
# (1) is the safest and needs no heuristics at all - it just costs a download.
