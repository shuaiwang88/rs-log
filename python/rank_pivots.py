"""Choose WHICH candidate becomes the reported buy point, and score the choice.

`pivot_audit.py` established the gap this closes: the scanner emits ~2.2 readings per event
and the truth pivot is within 1% of one of them on 140/171 events, but the one it actually
reports is right on only 93. Every point in between is available from ranking alone - no new
detector, no loosened gate, nothing that can inflate a one-sided metric, because picking a
different element of a fixed set cannot change what is in the set.

The scan is the expensive part (~171 events x full history), and a ranking rule is a pure
function of the candidates, so scan once into a cache and iterate rules against that.

Rules are deliberately dumb and structural. Anything that needs the truth to fit a parameter
is not a rule, it is a lookup, and it will not survive contact with a live chart.

Usage:
    python3 python/rank_pivots.py                 # score every rule
    python3 python/rank_pivots.py --refresh       # re-scan, then score
    python3 python/rank_pivots.py --show lowest   # per-event detail for one rule
"""
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval          # noqa: E402
from pivot_audit import reframe, headline_pivot   # noqa: E402

CACHE = ROOT / "python" / ".pivot_candidates.pkl"
BANDS = (1, 2, 3, 5, 10)

# Rules must be scored at LEAD TIME, not on the event date. The benchmark's event date IS
# the breakout, so on that bar the close sits just above the buy point by construction - any
# rule that leans on the close ("nearest to close") reads the answer off the evaluation date
# and scores +14 for nothing. Twenty sessions earlier the stock is mid-base and the close
# carries no such information, which is also when the user needs the number ("track distance
# to pivot, only consider entry within 15%"). A rule that only works on breakout day is
# worthless; T-20 is where the ranking has to earn it.
LEADS = (0, 5, 10, 20)

# IBD prices a base off a specific level, and the levels are not interchangeable: a handle
# high sits below the base top, a double-bottom middle peak below both. When two readings
# describe the same base, the more specific structure is the one whose buy point IBD quotes.
SPECIFICITY = {'Cup+Handle': 0, 'Dbl Bottom': 1, 'Cup': 2, 'Flat Base': 3, 'Consolidation': 4}


def collect(pad=1, back=0):
    """Scan every event once and record the full candidate set plus what was reported."""
    fe = FastEval(verbose=False)
    frames = reframe(fe, pad, back=back)
    saved = fe._frames
    fe._frames = frames
    out = []
    try:
        scan = fe._load_scanner({})
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
                continue
            reads = []
            for j, p in enumerate(res.get('patterns') or []):
                reads.append(dict(name=p['name'], pivot=float(p['pivot']), order=j,
                                  alt=bool(p.get('alt_base')),
                                  bars_ago=p.get('bars_ago'),
                                  base_len=p.get('base_len'), base_dep=p.get('base_dep'),
                                  also=list(p.get('also_reads_as') or [])))
            out.append(dict(key=key, sym=sym, btype=btype, truth=truth,
                            close=float(res.get('close') or 0.0),
                            prim_name=res.get('pattern_name'),
                            prim_on=bool(res.get('pattern_code')),
                            head=headline_pivot(res),
                            reads=reads))
    finally:
        fe._frames = saved
    return out


# ------------------------------------------------------------------ ranking rules
# Each takes (candidate list, event dict) and returns the chosen candidate.

def r_current(cs, e):
    """What ships today: the primary state machine's level, else the first reading."""
    return None            # scored from the recorded headline, not re-derived

def r_lowest(cs, e):
    """Nearest overhead resistance. Price must clear the lowest live pivot first."""
    return min(cs, key=lambda c: c['pivot'])

def r_highest(cs, e):
    return max(cs, key=lambda c: c['pivot'])

def r_nearest_close(cs, e):
    return min(cs, key=lambda c: abs(c['pivot'] - e['close']))

def r_nearest_above(cs, e):
    """Lowest pivot still above the close - the level a breakout would actually clear."""
    ab = [c for c in cs if c['pivot'] >= e['close']]
    return min(ab, key=lambda c: c['pivot']) if ab else max(cs, key=lambda c: c['pivot'])

def r_specific(cs, e):
    """Most specific structure wins; ties by lower pivot."""
    return min(cs, key=lambda c: (SPECIFICITY.get(c['name'], 9), c['pivot']))

def r_specific_then_recent(cs, e):
    return min(cs, key=lambda c: (SPECIFICITY.get(c['name'], 9), c['order']))

def r_primary_else_lowest(cs, e):
    if e['prim_on']:
        for c in cs:
            if c['name'] == e['prim_name']:
                return c
    return min(cs, key=lambda c: c['pivot'])

def r_newest_base(cs, e):
    """Readings come out newest-base-first, so order 0 is the tightest recent structure."""
    return min(cs, key=lambda c: c['order'])

def r_median(cs, e):
    ps = sorted(cs, key=lambda c: c['pivot'])
    return ps[len(ps) // 2]


RULES = [
    ('current (shipping)', r_current),
    ('lowest pivot', r_lowest),
    ('highest pivot', r_highest),
    ('nearest to close', r_nearest_close),
    ('lowest above close', r_nearest_above),
    ('most specific', r_specific),
    ('specific,then newest', r_specific_then_recent),
    ('primary else lowest', r_primary_else_lowest),
    ('newest base', r_newest_base),
    ('median pivot', r_median),
]


def apply_rule(fn, events):
    errs, picks = [], []
    for e in events:
        cs = e['reads']
        if fn is r_current or not cs:
            v = e['head']
        else:
            c = fn(cs, e)
            v = c['pivot'] if c else e['head']
        if not v:
            continue
        errs.append(abs(v - e['truth']) / e['truth'] * 100.0)
        picks.append((e, v))
    return np.array(errs), picks


def oracle(events):
    errs = []
    for e in events:
        vs = [c['pivot'] for c in e['reads']] + ([e['head']] if e['head'] else [])
        if not vs:
            continue
        errs.append(min(abs(v - e['truth']) / e['truth'] * 100.0 for v in vs))
    return np.array(errs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--pad', type=int, default=1)
    ap.add_argument('--show', default='')
    a = ap.parse_args()

    if a.refresh or not CACHE.exists():
        blob = {}
        for b in LEADS:
            print(f"scanning T-{b} ...", flush=True)
            blob[b] = collect(a.pad, back=b)
        with open(CACHE, 'wb') as f:
            pickle.dump(blob, f)
    else:
        with open(CACHE, 'rb') as f:
            blob = pickle.load(f)
    if not isinstance(blob, dict):
        blob = {0: blob}

    events = blob[0]
    print(f"{len(events)} events, "
          f"{np.mean([len(e['reads']) for e in events]):.1f} readings each")
    print("within-1% counts; T-0 is breakout day (close leaks the answer), "
          "T-20 is where the number is actually used\n")

    heads = f"  {'rule':<24}" + "".join(f"{f'T-{b}':>7}" for b in sorted(blob))
    print(heads + f"{'  T-20 median':>14}")
    base = {}
    for name, fn in RULES:
        cells, med20 = [], None
        for b in sorted(blob):
            errs, _ = apply_rule(fn, blob[b])
            n1 = int((errs <= 1).sum()) if len(errs) else 0
            cells.append(n1)
            if name.startswith('current'):
                base[b] = n1
            if b == max(blob):
                med20 = np.median(errs) if len(errs) else float('nan')
        line = f"  {name:<24}" + "".join(f"{c:>7}" for c in cells)
        if not name.startswith('current'):
            d = [cells[i] - base[b] for i, b in enumerate(sorted(blob))]
            line += f"{med20:>9.2f}%   " + " ".join(f"{x:+d}" for x in d)
        else:
            line += f"{med20:>9.2f}%"
        print(line)
    orow = [int((oracle(blob[b]) <= 1).sum()) for b in sorted(blob)]
    print(f"  {'ORACLE (best possible)':<24}" + "".join(f"{c:>7}" for c in orow))

    if a.show:
        events = blob[max(blob)] if a.show.endswith('@lead') else blob[0]
        a.show = a.show.replace('@lead', '')
        fn = dict((n, f) for n, f in RULES).get(a.show) or \
             next((f for n, f in RULES if a.show in n), None)
        if fn is None:
            print(f"\nno rule matching {a.show!r}")
            return
        errs, picks = apply_rule(fn, events)
        bad = sorted(((abs(v - e['truth']) / e['truth'] * 100, e, v) for e, v in picks),
                     key=lambda t: -t[0])[:25]
        print(f"\n  worst under {a.show!r}")
        print(f"  {'sym':<7}{'truth type':<20}{'truth':>9}{'pick':>9}{'err':>8}   candidates")
        for err, e, v in bad:
            cands = " ".join(f"{c['name']}@{c['pivot']:.2f}" for c in e['reads'])
            print(f"  {e['sym']:<7}{e['btype']:<20}{e['truth']:>9.2f}{v:>9.2f}{err:>7.1f}%   {cands}")


if __name__ == '__main__':
    main()


# ---------------------------------------------------------------- measured results
# Scored across all 171 events at four lead times, within-1% counts (2026-08-02):
#
#   rule                        T-0    T-5   T-10   T-20
#   current (shipping)           93     95     96     85
#   highest pivot                87     89     88     83
#   primary else lowest          80     86     91     82
#   median pivot                 81     79     76     79
#   newest base                  85     84     78     69
#   lowest above close          100     74     63     62
#   nearest to close            107     55     50     51
#   lowest pivot                 58     47     47     47
#   most specific                59     51     43     40
#   ORACLE                      140    135    123    118
#
# NEGATIVE, and the useful kind. Two things this settles:
#
# 1. RE-RANKING IS A DEAD END. The shipping choice already beats all nine alternatives at
#    every lead time. The 27-point oracle gap is real but no simple structural rule reaches
#    it - the information that would separate the right candidate from the wrong one is not
#    in {pivot, name, order}. Closing it needs a better LOCATOR, not a better chooser.
#
# 2. "nearest to close" IS LEAKAGE, and it is the trap this table exists to catch. It gains
#    +14 on breakout day and loses 40-46 everywhere else, because the benchmark's event date
#    is the breakout: on that bar the close sits just above the buy point by construction, so
#    the rule is reading the label off the evaluation date. Scored only at T-0 it looks like
#    the best idea of the session. Any future pivot rule must clear T-10/T-20 before it is
#    believed.
#
# Also worth keeping: the shipping pivot does NOT decay going back in time (93 -> 96 at
# T-10), so the reported buy point is stable through the base rather than something that
# only snaps into place at the breakout. That is the property the user's workflow needs.
