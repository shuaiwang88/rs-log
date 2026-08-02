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

# --- base-top preference -------------------------------------------------------------
# Cup+Handle and Double Bottom price off a level INSIDE the base (handle high, middle peak);
# Cup / Flat Base / Consolidation price off the base top. Cup+Handle is claimed on 85 of 172
# events when only 46 are true, so every spurious handle drags the quoted buy point below the
# level price actually has to clear - which is the dangerous direction. The signed-error dump
# shows the correct base-top reading sitting unused in the candidate list on CLFD (Cup@46.76
# vs quoted 33.66), APOG (Flat Base@49.99 vs 37.43), SKM (Cup@23.80 vs 20.55), ORA and GOLF.
BASETOP = {'Cup', 'Flat Base', 'Consolidation'}

def r_basetop_first(cs, e):
    """Prefer a base-top reading; fall back to the sub-structure ones only if there is none."""
    bt = [c for c in cs if c['name'] in BASETOP]
    return min(bt, key=lambda c: c['order']) if bt else min(cs, key=lambda c: c['order'])

def r_basetop_highest(cs, e):
    bt = [c for c in cs if c['name'] in BASETOP]
    return max(bt or cs, key=lambda c: c['pivot'])

def r_least_specific(cs, e):
    """Inverse of r_specific: the widest structure wins, ties by lower pivot."""
    return min(cs, key=lambda c: (-SPECIFICITY.get(c['name'], -1), c['pivot']))

def r_head_or_basetop(cs, e):
    """Keep the shipping pivot unless it is a sub-structure level and a base top is on offer.

    The narrowest possible change: only the events where a handle/middle-peak quote is
    overriding an available base top move at all.
    """
    if e['prim_on'] and e['prim_name'] in BASETOP:
        return None
    bt = [c for c in cs if c['name'] in BASETOP]
    return min(bt, key=lambda c: c['order']) if bt else None


def _mk_samebase(max_gap):
    """Override only when the base top is far ABOVE the sub-structure quote.

    The blanket override costs 12 true Cup+Handle hits at T-0, because when the handle is
    real and well located the handle high IS the buy point and the base top is several
    percent late. Splitting the two cases needs no fitted threshold, only IBD's own geometry:
    a handle is a shallow drift just under the base top - depth typically 8-12%, and IBD
    rejects one deeper than about 15%. So a base-top candidate sitting WITHIN ~15% of the
    handle quote is the same base, and the handle is plausible. One sitting 30-40% higher is
    not the same base at all - the "handle" was located on a small recent structure while the
    real frame is the much larger base, which is precisely the over-claim that produces
    dangerously low buy points (CLFD +39%, APOG +33%, SKM +16%).

    Same-base cases are LEFT ALONE; only the different-base ones are corrected.
    """
    def f(cs, e):
        if e['prim_on'] and e['prim_name'] in BASETOP:
            return None
        cur = e['head']
        if not cur:
            return None
        bt = [c for c in cs if c['name'] in BASETOP and
              (c['pivot'] - cur) / cur * 100.0 > max_gap]
        return max(bt, key=lambda c: c['pivot']) if bt else None
    return f


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
    ('base-top first', r_basetop_first),
    ('base-top highest', r_basetop_highest),
    ('least specific', r_least_specific),
    ('head, base-top override', r_head_or_basetop),
    ('same-base gap >12%', _mk_samebase(12)),
    ('same-base gap >15%', _mk_samebase(15)),
    ('same-base gap >20%', _mk_samebase(20)),
    ('same-base gap >25%', _mk_samebase(25)),
]


def apply_rule(fn, events):
    """Return SIGNED errors. The sign carries the risk: a buy point quoted below the truth
    says price has cleared a level it has not, so the scanner calls a breakout into overhead
    supply. Quoted high, the trade is merely missed. Judging these rules on |error| alone
    would rate those two outcomes the same."""
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
        errs.append((v - e['truth']) / e['truth'] * 100.0)
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

    ks = sorted(blob)
    print(f"  {'':<24}{'--- within 1% ---':^28}   {'-- quoted LOW by >3% --':^28}")
    print(f"  {'rule':<24}" + "".join(f"{f'T-{b}':>7}" for b in ks)
          + "   " + "".join(f"{f'T-{b}':>7}" for b in ks))
    base, baselow = {}, {}
    for name, fn in RULES:
        hits, lows = [], []
        for b in ks:
            errs, _ = apply_rule(fn, blob[b])
            hits.append(int((np.abs(errs) <= 1).sum()) if len(errs) else 0)
            lows.append(int((errs < -3).sum()) if len(errs) else 0)
        if name.startswith('current'):
            base, baselow = dict(zip(ks, hits)), dict(zip(ks, lows))
        line = (f"  {name:<24}" + "".join(f"{c:>7}" for c in hits)
                + "   " + "".join(f"{c:>7}" for c in lows))
        if not name.startswith('current'):
            dh = sum(hits) - sum(base.values())
            dl = sum(lows) - sum(baselow.values())
            line += f"   hits {dh:+d}  danger {dl:+d}"
        print(line)
    orow = [int((np.abs(oracle(blob[b])) <= 1).sum()) for b in ks]
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
# Within-1% counts over 171 events at four lead times, scored BEFORE the base-top override
# was adopted, so `current` here is the old shipping rule (2026-08-02):
#
#   rule                        T-0    T-5   T-10   T-20     quoted low by >3% (T-0/T-20)
#   current (old)                93     95     96     85          29 / 34
#   head, base-top override      97    100     98     95          18 / 19   <- ADOPTED
#   base-top first               97    101     98     91          18 / 24
#   highest pivot                87     89     88     83          14 / 12
#   primary else lowest          80     86     91     82          49 / 41
#   median pivot                 81     79     76     79          24 / 23
#   newest base                  85     84     78     69          46 / 60
#   lowest above close          100     74     63     62          22 / 73
#   nearest to close            107     55     50     51          27 / 92
#   lowest pivot                 58     47     47     47          88 / 96
#   most specific                59     51     43     40          79 / 88
#   ORACLE                      140    135    123    118
#
# Three things this settles.
#
# 1. THE ADOPTED RULE. Preferring a base-top reading over a sub-structure quote wins on both
#    axes at every lead time and is now in the scanner, so `current` reproduces its row and
#    `head, base-top override` scores +0. Rationale and costs are documented at the change
#    site in the scanner.
#
# 2. PURE RE-RANKING IS OTHERWISE A DEAD END. Every remaining rule loses. The oracle gap that
#    survives the override (98 vs 123 at T-10) is real but is not reachable from
#    {pivot, name, order} - closing it needs a better LOCATOR, not a better chooser.
#
# 3. "nearest to close" IS LEAKAGE, and it is the trap this table exists to catch. It gains
#    +14 on breakout day and loses 40-46 everywhere else, because the benchmark's event date
#    is the breakout: on that bar the close sits just above the buy point by construction, so
#    the rule is reading the answer off the evaluation date. Scored only at T-0 it looks like
#    the best idea of the session. Any future pivot rule must clear T-10/T-20 to be believed.
#
# Also worth keeping: the reported pivot does NOT decay going back in time (97 at T-0, 98 at
# T-10), so it is usable while a base is still forming rather than snapping into place only
# at the breakout. That is the property the user's workflow needs.
#
# OPEN, not adopted: re-running the same-base gap rules ON TOP of the override still shows
# ~15% fewer dangerous quotes for +1 hit (danger 18/17/18/19 -> 16/16/15/15 at gap >12%).
# Small, and it stacks a second override on the first, so the mechanism is muddy. Worth a
# look only if the dangerous-low count becomes the binding constraint.
