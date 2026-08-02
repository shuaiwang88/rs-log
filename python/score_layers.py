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
    precision/recall        per pattern name, over the reading set

Precision matters because every other metric here is ONE-SIDED. `layered broad` counts an
event as correct if ANY reading matches, so emitting more readings can only raise it - a
detector that named every pattern on every base would score 100%. The same is true of the
pivot bands, which take the best reading. Optimising those alone silently rewards
over-claiming, and it hid a real problem: Cup+Handle is claimed on 85 of 172 events when
only 46 are truly Cup With Handle (precision 36.5%, recall 67.4%). Any change that adds
handle detections has to be read against this column, not just the broad count.

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
    # name -> [true positives, false positives, false negatives] over the reading set
    pr = {}
    TRUTH_OF = {'Cup+Handle': 'Cup With Handle', 'Dbl Bottom': 'Double Bottom',
                'Cup': 'Cup Without Handle', 'Flat Base': 'Flat Base',
                'Consolidation': 'Consolidation'}
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

        for nm, tname in TRUTH_OF.items():
            d = pr.setdefault(nm, [0, 0, 0])
            claim, is_true = nm in names, (btype == tname)
            d[0] += claim and is_true
            d[1] += claim and not is_true
            d[2] += (not claim) and is_true

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
        'recovered': recovered, 'no_read': no_read, 'pr': pr,
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
    print(f"  {'reading':<14}{'claimed':>8}{'truth':>7}{'TP':>5}{'FP':>5}{'FN':>5}"
          f"{'prec':>8}{'recall':>8}{'F1':>7}")
    for nm, (tp, fp, fn) in sorted(r['pr'].items(), key=lambda kv: -kv[1][1]):
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 200 * p * rc / (p + rc) if p + rc else 0.0
        print(f"  {nm:<14}{tp + fp:>8}{tp + fn:>7}{tp:>5}{fp:>5}{fn:>5}"
              f"{p * 100:>7.1f}%{rc * 100:>7.1f}%{f1:>7.1f}")
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
