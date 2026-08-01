"""
Focused joint search on the two PIVOT-CRITICAL patterns: Cup+Handle and Double Bottom.

Why only these two: Flat Base / Consolidation / Cup Without Handle / 6-Wk Flat all buy at
bTop, so confusing them costs nothing. Cup+Handle (handle high) and Double Bottom (middle
peak) buy LOWER - mislabeling them either way reports the wrong entry price. They are also
the worst-performing detectors: Cup+Handle 28% recall / 52% precision, Double Bottom 29%
recall / 20% precision (fires wrongly more often than rightly).

Every hard-coded threshold inside both detection blocks is exposed as a knob here (many
were previously unreachable by the earlier searches, which only covered ~6 of them).

Objective `focus` = Cup+Handle exact + Double Bottom exact, with a guard: configs that
reduce overall pivot-safe accuracy below baseline are rejected, so we can't "win" by
trading away the well-behaved bTop group.

Usage:
    python3 python/focus_search.py --smoke
    python3 python/focus_search.py --n 400
"""
import argparse
import json
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval, N_EVENTS   # noqa: E402

# Every tunable in the Cup+Handle and Double Bottom blocks.
SPACE = {
    # ---- Cup + Handle ----
    'cuph_bCountMin':  [15, 20, 25, 30, 40, 50],
    'cuph_bDepLo':     [12.0, 15.0, 18.0, 20.0, 22.0, 25.0],
    'cuph_bDepHi':     [40.0, 45.0, 50.0, 55.0, 60.0],
    'cuph_flatGuard':  [15.0, 20.0, 25.0, 30.0, 99.0],
    'cuph_rDepGate':   [6, 8, 10, 12, 15, 18],
    'cuph_handleLen':  [None, 8, 10, 12, 15, 20, 25],
    'cuph_inTop':      [0.55, 0.65, 0.70, 0.80, 0.90, 0.95, 1.0],
    'cuph_hDepLo':     [0.0, 2.0, 3.0, 5.0, 7.0, 10.0],
    'cuph_hDepMax':    [None, 12.0, 15.0, 18.0, 20.0, 25.0, 30.0],
    'cuph_hdRatio':    [0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95],
    'cuph_volRatio':   [0.8, 0.9, 0.95, 1.0, 1.05, 1.15, 1.3],
    'cuph_h12Cap':     [0.98, 1.00, 1.02, 1.05, 1.08, 1.12],
    # Handle-window trim (now inline in the scanner): how far the window end walks back
    # past bars setting new short-term highs, so it stops at the handle's end.
    'cuph_slopeMax':   [0.30, 0.45, 0.60, 0.80, 1.00, 99.0],
    'cuph_trimMax':    [0, 5, 10, 15, 20],
    'cuph_trimLook':   [5, 10, 15],
    # ---- Double Bottom ----
    'db_bDepLo':       [8.0, 10.0, 12.0, 15.0, 18.0, 20.0],
    'db_bDepHi':       [30.0, 35.0, 40.0, 45.0, 50.0],
    'db_maxBars':      [55, 65, 75, 85, 100, 120],
    'db_cA_lo':        [0.80, 0.85, 0.90, 0.94, 0.97],
    'db_cA_hi':        [1.00, 1.02, 1.04, 1.08, 1.12],
    'db_cE_lo':        [0.70, 0.75, 0.85, 0.90, 0.95],
    'db_cE_hi':        [1.02, 1.05, 1.08, 1.12, 1.18],
    'db_cC':           [0.90, 0.93, 0.95, 0.97],
    'db_cD':           [0.20, 0.25, 0.30, 0.40, 0.50],
    'db_cPT':          [1.05, 1.10, 1.15, 1.20],
    'db_cSh':          [1.03, 1.06, 1.10, 1.15, 1.25],
    'db_cTC':          [3, 5, 8, 12, 18],
    'db_volRatio':     [0.7, 0.8, 0.9, 1.0, 1.1, 1.25],
    'db_undercut':     ['(sL < fL)', 'True'],
}

# Current committed state of `ibd_pattern_scanner copy.py`.
BASELINE_CFG = {
    'cuph_bCountMin': 20, 'cuph_bDepLo': 20.0, 'cuph_bDepHi': 50.0,
    'cuph_flatGuard': 25.0, 'cuph_rDepGate': 12, 'cuph_handleLen': 15,
    'cuph_inTop': 0.95, 'cuph_hDepLo': 5.0, 'cuph_hDepMax': None,
    'cuph_hdRatio': 0.45, 'cuph_volRatio': 1.15, 'cuph_h12Cap': 1.02,
    'db_bDepLo': 15.0, 'db_bDepHi': 40.0, 'db_maxBars': 55,
    'db_cA_lo': 0.94, 'db_cA_hi': 1.04, 'db_cE_lo': 0.75, 'db_cE_hi': 1.02,
    'db_cC': 0.95, 'db_cD': 0.30, 'db_cPT': 1.10, 'db_cSh': 1.10,
    'db_cTC': 5, 'db_volRatio': 0.90, 'db_undercut': '(sL < fL)',
    'cuph_slopeMax': 0.60, 'cuph_trimMax': 10, 'cuph_trimLook': 10,
}

_FE = None


def _init_worker():
    global _FE
    _FE = FastEval(verbose=False)


def _clean(cfg):
    return {k: v for k, v in cfg.items() if v is not None}


def _score(cfg):
    """Return per-pattern exact counts plus the overall pivot guard."""
    global _FE
    try:
        r = _FE.run(_clean(cfg))
        df = r['df']
        cuph_hit = int(((df['csv_type'] == 'Cup With Handle') & (df['detected'] == 'Cup+Handle')).sum())
        db_hit = int(((df['csv_type'] == 'Double Bottom') & (df['detected'] == 'Dbl Bottom')).sum())
        cuph_fp = int(((df['csv_type'] != 'Cup With Handle') & (df['detected'] == 'Cup+Handle')).sum())
        db_fp = int(((df['csv_type'] != 'Double Bottom') & (df['detected'] == 'Dbl Bottom')).sum())
        return {'cfg': cfg, 'focus': cuph_hit + db_hit, 'cuph_hit': cuph_hit, 'db_hit': db_hit,
                'cuph_fp': cuph_fp, 'db_fp': db_fp, 'pivot': r['pivot'], 'broad': r['broad'],
                'exact': r['exact'], 'piv2': r['piv2'], 'piv3': r['piv3'], 'piv5': r['piv5'],
                'err': None}
    except Exception as e:
        return {'cfg': cfg, 'focus': -1, 'cuph_hit': 0, 'db_hit': 0, 'cuph_fp': 0, 'db_fp': 0,
                'pivot': -1, 'broad': -1, 'exact': -1, 'piv2': -1, 'piv3': -1, 'piv5': -1,
                'err': repr(e)}


def smoke():
    fe = FastEval(verbose=False)
    print("knob smoke test (each applied alone):")
    bad = []
    for knob, vals in SPACE.items():
        v = next((x for x in vals if x is not None and x != BASELINE_CFG.get(knob)), None)
        if v is None:
            print(f"  {knob:<18} SKIP")
            continue
        try:
            fe._build_source({knob: v})
            print(f"  {knob:<18} ok  ({BASELINE_CFG.get(knob)} -> {v})")
        except Exception as e:
            print(f"  {knob:<18} FAIL {e}")
            bad.append(knob)
    if bad:
        print(f"\n{len(bad)} knob(s) broken: {bad}")
        return 1
    print("\nall knobs apply cleanly")
    return 0


def _sample_local(rng, centre, max_dims=4):
    c = dict(centre)
    for knob in rng.sample(list(SPACE), rng.randint(1, max_dims)):
        alts = [v for v in SPACE[knob] if v != centre.get(knob)]
        if alts:
            c[knob] = rng.choice(alts)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=400)
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--seed', type=int, default=11)
    ap.add_argument('--smoke', action='store_true')
    ap.add_argument('--max-dims', type=int, default=4)
    ap.add_argument('--pivot-floor', type=int, default=127,
                    help='reject configs whose overall pivot-safe count drops below this')
    # The label-bucket score is a weak proxy for the buy point: 34 of 125 bucket-"safe"
    # events miss the real pivot by >5%, and 27 of 39 bucket-"unsafe" ones land within 5%.
    # It rejected handle_trim_max=10, which improves the actual price on +5 events. Default
    # to optimising the price the trader actually pays.
    ap.add_argument('--objective', choices=['piv3', 'piv2', 'piv5', 'focus'], default='piv3',
                    help="'piv3' = events whose BUY PRICE is within 3%% (default); "
                         "'focus' = the old label-agreement count")
    args = ap.parse_args()
    if args.objective != 'focus':
        args.pivot_floor = 0   # the price metric is the guard; the bucket proxy would fight it

    if args.smoke:
        sys.exit(smoke())

    rng = random.Random(args.seed)
    seen = {tuple(sorted(map(str, BASELINE_CFG.items())))}
    cfgs = [dict(BASELINE_CFG)]
    guard = 0
    while len(cfgs) < args.n and guard < args.n * 200:
        guard += 1
        c = (_sample_local(rng, BASELINE_CFG, args.max_dims) if rng.random() < 0.8
             else {k: rng.choice(v) for k, v in SPACE.items()})
        key = tuple(sorted(map(str, c.items())))
        if key not in seen:
            seen.add(key)
            cfgs.append(c)

    print(f"focus search: {len(cfgs)} configs over {len(SPACE)} knobs "
          f"(all Cup+Handle & Double Bottom tunables)")
    print(f"baseline: Cup+H 20/46 fp9 + DB 2/7; broad 127; piv3 101/164;  pivot floor {args.pivot_floor}\n")

    out = ROOT / "python" / "focus_search_results.json"
    t0 = time.time()
    results = []
    best = -1
    with ProcessPoolExecutor(max_workers=args.workers or None, initializer=_init_worker) as ex:
        for i, r in enumerate(ex.map(_score, cfgs, chunksize=1), 1):
            results.append(r)
            if r[args.objective] > best and r['pivot'] >= args.pivot_floor:
                best = r[args.objective]
            if i % 20 == 0 or i == len(cfgs):
                print(f"  {i:>4}/{len(cfgs)}  best focus (pivot>={args.pivot_floor}): {best}  "
                      f"[{time.time()-t0:.0f}s]", flush=True)

    errs = [r for r in results if r['err']]
    if errs:
        print(f"\n{len(errs)} config(s) errored, e.g. {errs[0]['err']}")

    obj = args.objective
    ok = [r for r in results if r[obj] >= 0 and r['pivot'] >= args.pivot_floor]
    ok.sort(key=lambda x: (-x[obj], -x['piv5'], -x['pivot']))
    print(f"\n{'='*104}\nTOP 20 by {obj} (buy price within {obj[-1]}%), pivot-safe floor {args.pivot_floor}\n{'='*104}")
    for r in ok[:20]:
        diff = {k: v for k, v in r['cfg'].items() if v != BASELINE_CFG.get(k)}
        print(f"  piv2={r['piv2']:>3} piv3={r['piv3']:>3} piv5={r['piv5']:>3} | "
              f"CupH {r['cuph_hit']:>2}/46 fp{r['cuph_fp']:>2} DB {r['db_hit']}/7 | "
              f"bucket={r['pivot']:>3} exact={r['exact']:>3}  {diff}")

    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'err'} for r in ok], indent=2))
    print(f"\nfull results -> {out}")


if __name__ == '__main__':
    main()
