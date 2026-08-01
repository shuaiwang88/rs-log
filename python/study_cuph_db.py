"""
Focused study of the two patterns that gate broad accuracy: Cup With Handle and Double
Bottom. Both accept ONLY their exact label under broad matching (their pivot differs from
the base top), so every miss is a broad miss - 32 of the 46 remaining broad failures.

Everything the scanner currently tests is derived from price geometry plus volume MAGNITUDE
(a 20-bar average ratio). MarketSmith's own pattern model exposes a different vocabulary:

    UpBars / DownBars / BlueBars / RedBars      bar direction counts
    StallBars / SupportBars                     bar CHARACTER at volume
    UpVolumeTotal / DownVolumeTotal             accumulation vs distribution
    cupLength / handleLength (vs baseLength)    base decomposed, not one span
    AvgVolumeRatePctOnPivot                     volume behaviour at the pivot

Volume DIRECTION is the IBD thesis - a sound base shows accumulation - and the scanner has
never used it. This script measures whether any of it separates the two patterns, before
any of it gets wired into detection.

Usage:
    python3 python/study_cuph_db.py            # separability report
    python3 python/study_cuph_db.py --events   # per-event dump
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval   # noqa: E402


def auc(a, b):
    """Rank-based AUC; 0.5 = no separation. Reported un-oriented so direction is visible."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 5 or len(b) < 5:
        return np.nan
    r = rankdata(np.r_[a, b])
    n1 = len(a)
    return (r[:n1].sum() - n1 * (n1 + 1) / 2) / (n1 * len(b))


def bar_character(o, h, l, c, v, vavg):
    """MarketSmith-style bar classification.

    up/down      : close vs open
    blue/red     : direction on ABOVE-average volume (institutional footprint)
    stall        : heavy volume, wide bar, close in the lower third - supply hitting bids
    support      : heavy volume, close in the upper third - demand absorbing supply
    """
    rng = np.maximum(h - l, 1e-9)
    pos = (c - l) / rng
    heavy = v > vavg
    return {
        'up': c > o,
        'down': c < o,
        'blue': (c > o) & heavy,
        'red': (c < o) & heavy,
        'stall': heavy & (pos < 0.34),
        'support': heavy & (pos > 0.66),
    }


def features_for(df, bstart, end):
    """Volume-direction and bar-character features over [bstart, end]."""
    o = df['Open'].values[bstart:end + 1]
    h = df['High'].values[bstart:end + 1]
    l = df['Low'].values[bstart:end + 1]
    c = df['Close'].values[bstart:end + 1]
    v = df['Volume'].values[bstart:end + 1]
    if len(c) < 10:
        return None
    vavg = v.mean()
    ch = bar_character(o, h, l, c, v, vavg)
    n = len(c)
    upv = v[ch['up']].sum()
    dnv = v[ch['down']].sum()
    f = {
        'up_bars_pct': ch['up'].mean() * 100,
        'blue_bars_pct': ch['blue'].mean() * 100,
        'red_bars_pct': ch['red'].mean() * 100,
        'stall_pct': ch['stall'].mean() * 100,
        'support_pct': ch['support'].mean() * 100,
        # the accumulation/distribution ratio - IBD's central claim about a sound base
        'up_dn_vol': upv / max(dnv, 1.0),
        'blue_red': ch['blue'].sum() / max(ch['red'].sum(), 1),
        'sup_stall': ch['support'].sum() / max(ch['stall'].sum(), 1),
    }
    # same measures restricted to the LAST QUARTER of the base - where a handle would live
    q = max(5, n // 4)
    chq = bar_character(o[-q:], h[-q:], l[-q:], c[-q:], v[-q:], vavg)
    f['q_up_bars_pct'] = chq['up'].mean() * 100
    f['q_stall_pct'] = chq['stall'].mean() * 100
    f['q_support_pct'] = chq['support'].mean() * 100
    f['q_vol_ratio'] = v[-q:].mean() / max(vavg, 1.0)
    f['q_up_dn_vol'] = v[-q:][chq['up']].sum() / max(v[-q:][chq['down']].sum(), 1.0)
    # cup vs handle decomposition: where in the base does the low sit, how far has the
    # right side recovered, and how tight is the tail
    lo_i = int(np.argmin(l))
    f['low_pos'] = lo_i / max(n - 1, 1)
    f['right_recov'] = (h[lo_i:].max() - l.min()) / max(h.max() - l.min(), 1e-9)
    f['tail_tight'] = float(np.mean((h[-q:] - l[-q:]) / np.maximum(h[-q:], 1e-9)) * 100)
    f['base_len'] = n
    return f


def collect():
    fe = FastEval(verbose=False)
    scan = fe._load_scanner({})
    rows = []
    for key, sym, btype in fe._events:
        try:
            res = scan(sym, key)
        except Exception:
            res = None
        if not res or not res.get('history'):
            continue
        st = res['history'][-1]
        bc = st.get('bCount')
        if not bc or bc < 20:
            continue
        df = fe._frames[key]
        end = len(df) - 1
        bstart = max(0, end - int(bc))
        f = features_for(df, bstart, end)
        if f is None:
            continue
        f.update({'symbol': sym, 'truth': btype, 'detected': st['pName']})
        rows.append(f)
    return pd.DataFrame(rows)


FEATS = ['up_bars_pct', 'blue_bars_pct', 'red_bars_pct', 'stall_pct', 'support_pct',
         'up_dn_vol', 'blue_red', 'sup_stall', 'q_up_bars_pct', 'q_stall_pct',
         'q_support_pct', 'q_vol_ratio', 'q_up_dn_vol', 'low_pos', 'right_recov',
         'tail_tight', 'base_len']


def report(d, target, label):
    a = d[d.truth == target]
    b = d[d.truth != target]
    print(f"\n{'=' * 78}\n{label}   (n={len(a)} target, {len(b)} other)\n{'=' * 78}")
    print(f"{'feature':<16}{'target':>10}{'other':>10}{'AUC':>8}   power")
    scored = []
    for c in FEATS:
        v = auc(a[c], b[c])
        if not np.isfinite(v):
            continue
        vv = max(v, 1 - v)
        scored.append((vv, c, v, a[c].median(), b[c].median()))
    for vv, c, v, ma, mb in sorted(scored, reverse=True):
        tag = 'none' if vv < 0.60 else ('weak' if vv < 0.70 else
                                        ('MODERATE' if vv < 0.80 else 'STRONG'))
        print(f'{c:<16}{ma:>10.2f}{mb:>10.2f}{v:>8.3f}   {tag}')
    return scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--events', action='store_true')
    args = ap.parse_args()
    d = collect()
    print(f'bases studied: {len(d)}')
    print(d.truth.value_counts().to_string())
    s1 = report(d, 'Cup With Handle', 'CUP WITH HANDLE vs everything else')
    s2 = report(d, 'Double Bottom', 'DOUBLE BOTTOM vs everything else')
    best1 = max((x[0] for x in s1), default=0)
    best2 = max((x[0] for x in s2), default=0)
    print(f'\nbest Cup+Handle feature AUC {best1:.3f}   (previous best from price geometry: 0.699)')
    print(f'best Double Bottom feature AUC {best2:.3f}')
    if args.events:
        out = ROOT / 'python' / 'study_cuph_db.csv'
        d.to_csv(out, index=False)
        print(f'\nper-event features -> {out}')


if __name__ == '__main__':
    main()
