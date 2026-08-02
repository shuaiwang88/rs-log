"""Audit the PIVOT (buy point) rather than the label - per event, worst error first.

The label metrics are one-sided and largely saturated; the pivot is the number that gets
traded, so it deserves its own instrument. Three things this measures that `score_layers.py`
does not:

1. STRUCTURAL ATTRIBUTION. For every miss, find the bar whose high sits closest to the truth
   pivot and report how far back it is. A miss where the truth price never appears as a high
   in the window is a base-boundary problem; a miss where it appears 30 bars back as a clean
   swing high is a locator problem. Different fixes.

2. PAD SENSITIVITY. fast_eval builds windows with `pad=6` - five bars PAST the breakout date -
   so breakout-window metrics have room to measure into. `locate_handle` counts `min_age` back
   from the last bar, so those five bars slide the handle search forward. Any pivot number
   quoted off the benchmark frames is therefore partly hindsight. `--pad 1` ends on the event
   date, which is what a live scan sees.

3. LEAD TIME. The user's workflow needs the pivot BEFORE the breakout ("track distance to
   pivot, only consider entry within 15%"). A pivot that is only correct on breakout day is
   worth little. `--lead 5,10,20` re-scans as of N sessions earlier and reports how the error
   decays going back in time.

Usage:
    python3 python/pivot_audit.py                      # benchmark frames (pad=6)
    python3 python/pivot_audit.py --pad 1              # honest as-of frames
    python3 python/pivot_audit.py --pad 1 --lead 5,10,20
    python3 python/pivot_audit.py --pad 1 --worst 40   # show more rows
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval  # noqa: E402

BANDS = (1, 2, 3, 5, 10)
BREAKOUT_GUARD = 3   # bars at the right edge excluded from the "was it on the chart" search


def _clean(s):
    return str(s).strip()


def truth_rows():
    """CSV row per event key, so misses can be read against Length / Depth / handle depth."""
    csv = pd.read_csv(ROOT / "IBD" / "Breakaway Gap.csv")
    out = {}
    for idx, row in csv.iterrows():
        out[f"{row['Symbol']}::{idx}"] = row
    return out


def reframe(fe, pad, back=0):
    """Rebuild every event frame ending `back` sessions before the event date, plus `pad`.

    fast_eval's cached windows already end at `event_idx + 6`. Re-slicing from that cached
    frame is exact: dropping the last (6 - pad + back) bars lands on the same bars the
    original parquet would have given, because the cut was taken from a sorted frame.
    """
    drop = 6 - pad + back
    if drop <= 0:
        return dict(fe._frames)
    return {k: v.iloc[:-drop] for k, v in fe._frames.items() if len(v) > drop + 60}


def best_pivot(res):
    """Every price the scanner is willing to call a buy point, plus which reading gave it."""
    cands = []
    for p in (res.get('patterns') or []):
        cands.append((p['pivot'], p['name']))
    c, dp = res.get('close'), res.get('dist_pct')
    if c and dp is not None and (1.0 + dp / 100.0) != 0:
        cands.append((c / (1.0 + dp / 100.0), res.get('pattern_name') or 'primary'))
    return cands


def headline_pivot(res):
    """The ONE buy point the scanner reports - `pivot`, reconstructed the way callers do.

    This is the metric that cannot be gamed. `best over all readings` is a min over
    candidates, so emitting one more reading can only ever lower it - the same one-sided
    trap that let the label metrics drift until precision was added. A trader acts on a
    single number; that number is this one. Report both: headline is accuracy, best is
    availability (does the machinery hold the right answer anywhere?), and the gap between
    them is how much a better ranker could win without any new detection at all.
    """
    c, dp = res.get('close'), res.get('dist_pct')
    if c and dp is not None and (1.0 + dp / 100.0) != 0:
        return c / (1.0 + dp / 100.0)
    return res.get('pivot')


def nearest_high(frame, truth, length=None, slack=15):
    """Closest daily high to the truth pivot INSIDE the base MarketSmith says is there.

    Bounding this matters. Searched over 400 bars, almost any price a wide-ranging stock has
    ever traded matches some high, so 'the truth price is on the chart' comes out true by
    accident - AUPH's 9.19 matches a high 256 sessions back, which is nowhere near the base
    the CSV describes. The CSV gives `Length` in sessions, so search only
    [event - Length - slack, event] and the answer means something: within that window the
    truth pivot either is a daily high the scanner could have chosen, or it is not.

    A truth pivot with NO high within a couple of percent of it in its own base is almost
    always a split/adjustment mismatch between the CSV price and the cached parquet, not a
    detector failure - those events cannot be won and should not drive tuning.
    """
    h = frame['High'].to_numpy(dtype=float)
    if len(h) == 0:
        return None, None
    # Drop the last few bars. The event date IS the breakout, so that bar's range sweeps
    # straight through the pivot and matches it to within rounding every single time. Left
    # in, it makes every miss look 'winnable at bars_ago 0' - which is how CCSI, CYTK, HWM,
    # TSN and FOXF first showed up as locator problems when the truth price is nowhere in
    # the base proper. A buy point has to be visible BEFORE the breakout to be worth
    # anything, so the breakout bar is exactly the bar that must not count.
    h = h[:-BREAKOUT_GUARD] if len(h) > BREAKOUT_GUARD else h
    if len(h) == 0:
        return None, None
    n = int(length) + slack if length and np.isfinite(length) else 400
    h = h[-n:] if len(h) > n else h
    err = np.abs(h - truth) / truth * 100.0
    j = int(np.argmin(err))
    return float(err[j]), int(len(h) - 1 - j) + BREAKOUT_GUARD


def score(fe, frames, label, rows):
    scan = fe._load_scanner({})
    saved = fe._frames
    fe._frames = frames
    try:
        scan = fe._load_scanner({})
        recs = []
        for key, sym, btype in fe._events:
            if key not in frames:
                continue
            truth = fe._truth_pivots.get(key)
            if not truth:
                continue
            try:
                res = scan(sym, key)
            except Exception:
                res = None
            if not res:
                recs.append(dict(key=key, sym=sym, btype=btype, truth=truth,
                                 err=None, got=None, via=None, n_read=0))
                continue
            cands = best_pivot(res)
            if not cands:
                recs.append(dict(key=key, sym=sym, btype=btype, truth=truth,
                                 err=None, got=None, via=None, n_read=0))
                continue
            errs = [(abs(v - truth) / truth * 100.0, v, nm) for v, nm in cands]
            e, v, nm = min(errs, key=lambda t: t[0])
            hp = headline_pivot(res)
            he = abs(hp - truth) / truth * 100.0 if hp else None
            recs.append(dict(key=key, sym=sym, btype=btype, truth=truth, err=e, got=v,
                             via=nm, n_read=len(cands), all=cands,
                             head=hp, head_err=he))
    finally:
        fe._frames = saved
    return recs


def summarise(recs, label):
    have = [r for r in recs if r['err'] is not None]
    n = len(recs)
    print(f"\n--- {label}  (n={n}, priced {len(have)}) ---")
    if not have:
        return
    errs = np.array([r['err'] for r in have])
    heads = np.array([r['head_err'] for r in have if r['head_err'] is not None])
    for tag, a in (('HEADLINE (the one reported)', heads), ('best over all readings', errs)):
        if len(a) == 0:
            continue
        parts = "  ".join(f"<={k}% {int((a <= k).sum()):>3} ({(a <= k).mean() * 100:>5.1f}%)"
                          for k in BANDS)
        print(f"  {tag:<28}{parts}")
        print(f"  {'':<28}median {np.median(a):.2f}%   mean {a.mean():.2f}%   "
              f"p90 {np.percentile(a, 90):.2f}%   max {a.max():.1f}%")
    by = {}
    for r in have:
        by.setdefault(r['btype'], []).append(r['err'])
    print(f"  {'truth type':<22}{'n':>4}{'<=1%':>7}{'<=3%':>7}{'median':>9}")
    for bt, es in sorted(by.items(), key=lambda kv: -len(kv[1])):
        a = np.array(es)
        print(f"  {bt:<22}{len(a):>4}{(a <= 1).sum():>7}{(a <= 3).sum():>7}"
              f"{np.median(a):>8.2f}%")


def _len_of(rows, key):
    row = rows.get(key)
    if row is None:
        return None
    try:
        v = float(row.get('Length'))
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def worst(recs, frames, rows, k, band=1.0):
    have = sorted((r for r in recs if r['err'] is not None and r['err'] > band),
                  key=lambda r: -r['err'])
    print(f"\n  {len(have)} events miss the {band:g}% band; worst {min(k, len(have))} shown")
    print(f"  {'sym':<7}{'truth type':<20}{'truth':>9}{'got':>9}{'err':>8}{'via':<14}"
          f"{'inbase':>8}{'barsago':>8}  len/dep/hdep")
    for r in have[:k]:
        f = frames.get(r['key'])
        L = _len_of(rows, r['key'])
        oc, ba = nearest_high(f, r['truth'], L) if f is not None else (None, None)
        row = rows.get(r['key'])
        meta = f"{row.get('Length')}/{row.get('Depth')}/{row.get('handle depth')}" if row is not None else ''
        print(f"  {r['sym']:<7}{r['btype']:<20}{r['truth']:>9.2f}{r['got']:>9.2f}"
              f"{r['err']:>7.1f}%{'  ' + str(r['via']):<14}"
              f"{(f'{oc:.1f}%' if oc is not None else '-'):>8}"
              f"{(str(ba) if ba is not None else '-'):>8}  {meta}")
    return have


def attribute(have, frames, rows):
    """Split the misses into winnable (truth pivot is a high in its own base) and not."""
    winnable, unreachable = [], []
    for r in have:
        f = frames.get(r['key'])
        if f is None:
            continue
        L = _len_of(rows, r['key'])
        oc, ba = nearest_high(f, r['truth'], L)
        (winnable if oc is not None and oc <= 1.0 else unreachable).append((r, oc, ba))
    print(f"\n  ATTRIBUTION of {len(have)} misses, searching only the CSV's own base window")
    print(f"    truth pivot IS a daily high there (<=1%): {len(winnable):>3}  -> locator problem")
    print(f"    truth pivot is NOT (no high within 1%):   {len(unreachable):>3}  -> base drawn "
          f"wrong, or CSV/parquet price mismatch")
    if unreachable:
        print("    unreachable: " + ", ".join(
            f"{r['sym']}({oc:.0f}%)" for r, oc, _ in sorted(unreachable, key=lambda t: -t[1])[:20]))
    if winnable:
        c = Counter(r['btype'] for r, _, _ in winnable)
        print("    winnable by truth type: " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
        ages = [ba for _, _, ba in winnable if ba is not None]
        if ages:
            print(f"    the correct high sits {int(np.median(ages))} sessions back (median), "
                  f"range {min(ages)}-{max(ages)}")
    return winnable, unreachable


def lead(fe, pads, rows, backs):
    print("\n=== lead time: pivot error as of N sessions BEFORE the event date ===")
    print(f"  {'as-of':<12}{'priced':>8}" + "".join(f"{f'<={k}%':>8}" for k in BANDS)
          + f"{'median':>9}")
    for b in backs:
        frames = reframe(fe, 1, back=b)
        recs = score(fe, frames, f'T-{b}', rows)
        have = [r['err'] for r in recs if r['err'] is not None]
        if not have:
            print(f"  {'T-' + str(b):<12}{0:>8}")
            continue
        a = np.array(have)
        print(f"  {'T-' + str(b):<12}{len(a):>8}"
              + "".join(f"{int((a <= k).sum()):>8}" for k in BANDS)
              + f"{np.median(a):>8.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pad', type=int, default=6,
                    help='bars past the event date (6 = fast_eval benchmark, 1 = honest as-of)')
    ap.add_argument('--worst', type=int, default=25)
    ap.add_argument('--lead', default='', help='comma list, e.g. 5,10,20')
    ap.add_argument('--compare-pad', action='store_true',
                    help='score pad=6 and pad=1 side by side')
    a = ap.parse_args()

    fe = FastEval(verbose=False)
    rows = truth_rows()

    pads = [6, 1] if a.compare_pad else [a.pad]
    last = None
    for p in pads:
        frames = reframe(fe, p)
        recs = score(fe, frames, f'pad={p}', rows)
        summarise(recs, f'pad={p}' + ('  (benchmark, leaks 5 post-breakout bars)' if p == 6
                                      else '  (as-of the event date)'))
        last = (recs, frames)
    if last:
        have = worst(last[0], last[1], rows, a.worst)
        attribute(have, last[1], rows)
    if a.lead:
        lead(fe, a.pad, rows, [int(x) for x in a.lead.split(',') if x.strip()])


if __name__ == '__main__':
    main()
