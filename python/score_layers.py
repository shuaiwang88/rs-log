"""
Score BOTH the primary label and the layered `patterns` list against the 172 benchmark
events, in one pass, so a change can be judged on the metric it actually targets.

`fast_eval.run()` scores only `pattern_name` - the single label the state machine settles
on. The scanner now also emits `patterns`: every defensible reading of the base, each with
the pivot it prices off. A change that adds a reading cannot move the primary number, so
fast_eval reports it as a no-op even when it recovers an event outright.

Metrics:
    primary exact/broad     `pattern_name` vs truth, via fast_eval's own name maps
    layered exact/broad     ANY reading matches (this is the number the user reads)
    pivot within N%         best reading's buy point vs the ground truth Pivot Price
    recovered               events with no primary pattern that a reading rescues

Usage:
    python3 python/score_layers.py                  # baseline
    python3 python/score_layers.py --emit-readings  # with the readings-only result fix
    python3 python/score_layers.py --diff           # both, side by side
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval, EXACT_NAME_MAP, BROAD_NAME_MAP  # noqa: E402

# Emit the result whenever there are layered readings, even if the single-base state machine
# has no active pattern. See the note in the scanner for why this is not a scoring change.
EMIT_ANCHOR = "        if latest['pOn'] and (latest['pCode'] > 0):"
EMIT_PATCH = "        if (latest['pOn'] and (latest['pCode'] > 0)) or patterns:"


def _apply_emit_readings(src):
    if EMIT_ANCHOR not in src:
        raise RuntimeError("emit_readings anchor not found")
    return src.replace(EMIT_ANCHOR, EMIT_PATCH, 1)


def score(fe, patch=None, label=''):
    scan = fe._load_scanner({}, extra_patch=patch) if patch else fe._load_scanner({})
    n = 0
    p_exact = p_broad = l_exact = l_broad = 0
    within = {1: 0, 2: 0, 3: 0, 5: 0}
    have_truth = 0
    errs = []
    recovered = []
    no_read = []
    for key, sym, btype in fe._events:
        try:
            res = scan(sym, key)
        except Exception:
            res = None
        n += 1
        det = res.get('pattern_name', 'None') if res else 'None'
        ex, br = EXACT_NAME_MAP.get(btype, set()), BROAD_NAME_MAP.get(btype, set())
        prim_on = bool(res and res.get('pattern_code'))
        if det in ex:
            p_exact += 1
        if det in br:
            p_broad += 1

        names = {det} if prim_on else set()
        pats = (res.get('patterns') or []) if res else []
        for p in pats:
            names.add(p['name'])
            names.update(p.get('also_reads_as') or [])
        le, lb = bool(names & ex), bool(names & br)
        l_exact += le
        l_broad += lb
        if not prim_on and lb:
            recovered.append((sym, btype, det, [f"{p['name']}@{p['pivot']}" for p in pats]))
        if not prim_on and not pats:
            no_read.append((sym, btype))

        truth = fe._truth_pivots.get(key)
        if truth and res:
            cands = [p['pivot'] for p in pats]
            c, dp = res.get('close'), res.get('dist_pct')
            if c and dp is not None and (1.0 + dp / 100.0) != 0:
                cands.append(c / (1.0 + dp / 100.0))
            if cands:
                e = min(abs(v - truth) / truth * 100.0 for v in cands)
                have_truth += 1
                errs.append(e)
                for k in within:
                    if e <= k:
                        within[k] += 1
    return {
        'label': label, 'n': n,
        'p_exact': p_exact, 'p_broad': p_broad,
        'l_exact': l_exact, 'l_broad': l_broad,
        'within': within, 'have_truth': have_truth,
        'median_err': float(np.median(errs)) if errs else None,
        'recovered': recovered, 'no_read': no_read,
    }


def show(r):
    n = r['n']
    pct = lambda x: f"{x:>4} ({x / n * 100:>5.1f}%)"
    print(f"\n--- {r['label']} ---")
    print(f"  primary exact  {pct(r['p_exact'])}      primary broad  {pct(r['p_broad'])}")
    print(f"  layered exact  {pct(r['l_exact'])}      layered broad  {pct(r['l_broad'])}")
    h = r['have_truth']
    w = r['within']
    print(f"  best-reading pivot vs truth (n={h}): "
          f"<=1% {w[1]}  <=2% {w[2]}  <=3% {w[3]}  <=5% {w[5]}   median {r['median_err']:.2f}%")
    if r['recovered']:
        print(f"  recovered by a reading (no primary pattern): {len(r['recovered'])}")
        for s, bt, det, ps in r['recovered']:
            print(f"    {s:<7}{bt:<20}{' | '.join(ps)}")
    if r['no_read']:
        print(f"  still no reading at all: {len(r['no_read'])}  "
              f"{', '.join(s for s, _ in r['no_read'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--emit-readings', action='store_true')
    ap.add_argument('--diff', action='store_true')
    a = ap.parse_args()
    fe = FastEval(verbose=False)
    if a.diff:
        b = score(fe, None, 'baseline')
        p = score(fe, _apply_emit_readings, 'emit readings-only results')
        show(b)
        show(p)
        print(f"\n  DELTA  primary exact {p['p_exact'] - b['p_exact']:+d}  "
              f"primary broad {p['p_broad'] - b['p_broad']:+d}  "
              f"layered exact {p['l_exact'] - b['l_exact']:+d}  "
              f"layered broad {p['l_broad'] - b['l_broad']:+d}  "
              f"pivot<=1% {p['within'][1] - b['within'][1]:+d}")
    else:
        show(score(fe, _apply_emit_readings if a.emit_readings else None,
                   'emit readings' if a.emit_readings else 'baseline'))


if __name__ == '__main__':
    main()
