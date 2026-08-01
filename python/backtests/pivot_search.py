"""
Joint parameter search targeting PIVOT-EQUIVALENCE accuracy: Flat Base, Consolidation and
Cup Without Handle share the same buy point (base top), so confusing them with each other
costs nothing. Cup With Handle (handle high) and Double Bottom (middle peak) have a
genuinely LOWER pivot - if a true Cup+Handle/Double Bottom gets labeled as one of the
base-top three, the trader is told to wait for the wrong (too high) price. That direction
dominates the current error: Cup+Handle recall is 19.6% (34/46 mislabeled as StdPivot) and
Double Bottom recall is 28.6% (4/7 same issue), while the reverse direction (StdPivot
mislabeled as CupH/DB) only affects 9 events. This search optimizes fast_eval's 'pivot'
field (the overall pivot-equivalent match count) directly, which weighs fixing that
recall gap far more than 'broad' does.

Usage:
    python3 python/pivot_search.py --smoke
    python3 python/pivot_search.py --n 400
"""
import argparse
import json
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval, N_EVENTS   # noqa: E402

SPACE = {
    'cuph_inTop':      [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0],
    'cuph_hdRatio':    [0.45, 0.55, 0.65, 0.70, 0.75, 0.80, 0.85, 0.95],
    'cuph_bCountMin':  [15, 20, 25, 30, 40, 50, 65, 80],
    'cuph_rDepGate':   [6, 8, 10, 12, 15, 18],
    'cuph_hDepLo':     [0.0, 2.0, 3.0, 5.0, 7.0],
    'cuph_hDepMax':    [None, 10.0, 12.0, 15.0, 18.0, 20.0, 25.0, 30.0],
    'cuph_handleLen':  [None, 6, 8, 10, 12, 15, 20, 25],
    'cuph_volRatio':   [0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0, 1.1, 1.2],
    'cup_depLo_short': [8.0, 10.0, 12.0, 15.0, 18.0],
    'flat_rDep':       [15.0, 18.0, 20.0, 22.0, 25.0],
    'flat_rDep25':     [12.0, 15.0, 18.0],
    'db_cA_lo':        [0.80, 0.85, 0.90, 0.94],
    'db_cE_lo':        [0.70, 0.75, 0.85, 0.90],
    'db_volRatio':     [0.9, 1.0, 1.05, 1.1, 1.2, 1.3],
    'uptrend_bars':    [65, 75, 90, 100, 115, 130, 150, 200],
    'uptrend_ratio':   [1.05, 1.10, 1.15, 1.18, 1.20, 1.22, 1.25],
}

BASELINE_CFG = {
    'cuph_inTop': 0.95, 'cuph_hdRatio': 0.55, 'cuph_bCountMin': 20,
    'cuph_rDepGate': 12, 'cuph_hDepLo': 5.0, 'cuph_hDepMax': 15.0,
    'cuph_handleLen': 15, 'cuph_volRatio': 1.0, 'cup_depLo_short': 8.0,
    'flat_rDep': 20.0, 'flat_rDep25': 15.0, 'db_cA_lo': 0.94, 'db_cE_lo': 0.75,
    'db_volRatio': 1.0, 'uptrend_bars': 130, 'uptrend_ratio': 1.20,
}

_FE = None


def _init_worker():
    global _FE
    _FE = FastEval(verbose=False)


def _clean(cfg):
    return {k: v for k, v in cfg.items() if v is not None}


def _score(cfg):
    global _FE
    try:
        r = _FE.run(_clean(cfg))
        return {'cfg': cfg, 'pivot': r['pivot'], 'broad': r['broad'], 'exact': r['exact'],
                'macro_f1_x1000': r['macro_f1_x1000'], 'focus_f1_x1000': r['focus_f1_x1000'],
                'cuph_recall': r['cuph_recall'], 'cuph_prec': r['cuph_prec'],
                'db_recall': r['db_recall'], 'err': None}
    except Exception as e:
        return {'cfg': cfg, 'pivot': -1, 'broad': -1, 'exact': -1, 'macro_f1_x1000': -1,
                'focus_f1_x1000': -1, 'cuph_recall': 0.0, 'cuph_prec': 0.0,
                'db_recall': 0.0, 'err': repr(e)}


def smoke():
    fe = FastEval(verbose=False)
    print("knob smoke test:")
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


def _sample_global(rng):
    return {k: rng.choice(v) for k, v in SPACE.items()}


def _sample_local(rng, centre, max_dims=4):
    c = dict(centre)
    k = rng.randint(1, max_dims)
    for knob in rng.sample(list(SPACE), k):
        alts = [v for v in SPACE[knob] if v != centre.get(knob)]
        if alts:
            c[knob] = rng.choice(alts)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=300)
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--seed', type=int, default=17)
    ap.add_argument('--smoke', action='store_true')
    ap.add_argument('--mode', choices=['global', 'local', 'mixed'], default='mixed')
    ap.add_argument('--max-dims', type=int, default=4)
    args = ap.parse_args()

    if args.smoke:
        sys.exit(smoke())

    rng = random.Random(args.seed)
    seen = set()
    cfgs = [dict(BASELINE_CFG)]
    seen.add(tuple(sorted(BASELINE_CFG.items())))
    guard = 0
    while len(cfgs) < args.n and guard < args.n * 200:
        guard += 1
        if args.mode == 'local':
            c = _sample_local(rng, BASELINE_CFG, args.max_dims)
        elif args.mode == 'global':
            c = _sample_global(rng)
        else:
            c = (_sample_local(rng, BASELINE_CFG, args.max_dims)
                 if rng.random() < 0.75 else _sample_global(rng))
        key = tuple(sorted(c.items()))
        if key not in seen:
            seen.add(key)
            cfgs.append(c)

    workers = args.workers or None
    PIVOT_BASELINE = 105
    print(f"pivot-equivalence search [{args.mode}]: {len(cfgs)} configs over {len(SPACE)} knobs")
    print(f"baseline pivot = {PIVOT_BASELINE}/{N_EVENTS} ({PIVOT_BASELINE/N_EVENTS*100:.1f}%)\n")

    out = ROOT / "python" / "backtests" / "pivot_search_results.json"
    t0 = time.time()
    results = []
    best_seen = -1
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        for i, r in enumerate(ex.map(_score, cfgs, chunksize=1), 1):
            results.append(r)
            if r['pivot'] > best_seen:
                best_seen = r['pivot']
            if i % 20 == 0 or i == len(cfgs):
                el = time.time() - t0
                print(f"  {i:>4}/{len(cfgs)}  best so far {best_seen}/{N_EVENTS} "
                      f"({best_seen/N_EVENTS*100:.1f}%)  [{el:.0f}s elapsed]", flush=True)

    errs = [r for r in results if r['err']]
    if errs:
        print(f"\n{len(errs)} config(s) errored, e.g. {errs[0]['err']}")

    ok = sorted([r for r in results if r['pivot'] >= 0], key=lambda x: (-x['pivot'], -x['broad']))
    print(f"\n{'='*90}\nTOP 15 CONFIGS BY PIVOT-EQUIVALENCE\n{'='*90}")
    for r in ok[:15]:
        d = r['pivot'] - PIVOT_BASELINE
        diff = {k: v for k, v in r['cfg'].items() if v != BASELINE_CFG.get(k)}
        print(f"  pivot={r['pivot']:>3}/{N_EVENTS} ({r['pivot']/N_EVENTS*100:>5.1f}%)  broad={r['broad']:>3}  exact={r['exact']:>3}  "
              f"CupH r/p {r['cuph_recall']*100:>4.0f}/{r['cuph_prec']*100:<4.0f} DB {r['db_recall']*100:>4.0f}  "
              f"delta {d:>+3}  {diff if diff else '= baseline'}")

    out.write_text(json.dumps(
        [{'pivot': r['pivot'], 'broad': r['broad'], 'exact': r['exact'], 'macro_f1_x1000': r['macro_f1_x1000'],
          'focus_f1_x1000': r['focus_f1_x1000'], 'cuph_recall': r['cuph_recall'],
          'cuph_prec': r['cuph_prec'], 'db_recall': r['db_recall'], 'cfg': r['cfg']}
         for r in ok], indent=2))
    print(f"\nfull results -> {out}")


if __name__ == '__main__':
    main()
