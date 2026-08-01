"""
Divide-and-conquer search over the bTop-pivot group (Cup Without Handle / Consolidation /
Flat Base) to lift EXACT match, without giving back the BROAD/pivot-safe gains already won
on Cup+Handle and Double Bottom.

Failure map that motivated the split (172 events, current state):
  EXACT-only errors (broad OK, label wrong) - 40 events, dominated by:
      Consolidation -> Cup .............. 23   <- single biggest exact-match loss
      Flat Base     -> Cup ..............  4
      Cup Without H -> Consol/Flat/Base .  9
  BROAD errors - 51 events, dominated by:
      Cup With Handle missed ............ 28
      StdPivot -> Cup+Handle (false pos) .  8
      missed entirely (None) ............ 11

Cup and Consolidation share the bTop pivot, so their confusion is free under BROAD but
costs a full point under EXACT - which is why this search optimises exact while holding
broad at a floor.

Groups (`--group`):
    cupconsol : Cup vs Consolidation separation      (targets the 23-event cluster)
    flat      : Flat Base / 6-Wk Flat boundaries
    all       : every knob in the bTop group

Usage:
    python3 python/dc_search.py --smoke
    python3 python/dc_search.py --group cupconsol --n 400
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

GROUPS = {
    'cupconsol': [
        'cup_uShapeThr', 'cup_uShapeMin', 'cup_uGateBars',
        'cup_shortLo', 'cup_shortHi', 'cup_midLo', 'cup_midHi', 'cup_longLo',
        'likelyConsol', 'consol_depLo', 'consol_depHi', 'consol_longBars',
    ],
    'flat': [
        'flat_rDep', 'flat_rDep25', 'flat_bCountHi', 'flat_rDep25Hi',
        'flat_6wkLo', 'flat_6wkHi',
    ],
}
GROUPS['all'] = GROUPS['cupconsol'] + GROUPS['flat']

SPACE = {
    # Cup shape / depth
    'cup_uShapeThr':   [0.80, 0.85, 0.90, 0.93, 0.96],
    'cup_uShapeMin':   [40, 60, 80, 100, 130],
    'cup_uGateBars':   [40, 60, 80, 100, 130, 999],
    'cup_shortLo':     [8.0, 10.0, 12.0, 15.0, 18.0, 20.0],
    'cup_shortHi':     [35.0, 40.0, 45.0, 50.0, 55.0],
    'cup_midLo':       [10.0, 15.0, 18.0, 22.0, 26.0],
    'cup_midHi':       [35.0, 40.0, 45.0, 50.0],
    'cup_longLo':      [15.0, 20.0, 25.0, 30.0],
    # Consolidation
    'likelyConsol':    [120, 150, 180, 220, 250, 300],
    'consol_depLo':    [5.0, 8.0, 10.0, 12.0, 15.0],
    'consol_depHi':    [35.0, 40.0, 45.0, 50.0, 60.0],
    'consol_longBars': [150, 200, 250, 300],
    # Flat Base
    'flat_rDep':       [12.0, 15.0, 18.0, 20.0, 24.0],
    'flat_rDep25':     [10.0, 12.0, 15.0, 18.0],
    'flat_bCountHi':   [90, 110, 130, 160, 200],
    'flat_rDep25Hi':   [150, 200, 250, 300],
    'flat_6wkLo':      [15, 20, 25, 30],
    'flat_6wkHi':      [30, 35, 40, 50],
}

BASELINE_CFG = {
    'cup_uShapeThr': 0.90, 'cup_uShapeMin': 100, 'cup_uGateBars': 999,
    'cup_shortLo': 8.0, 'cup_shortHi': 55.0, 'cup_midLo': 15.0, 'cup_midHi': 45.0,
    'cup_longLo': 20.0,
    'likelyConsol': 250, 'consol_depLo': 5.0, 'consol_depHi': 35.0, 'consol_longBars': 200,
    'flat_rDep': 20.0, 'flat_rDep25': 15.0, 'flat_bCountHi': 130, 'flat_rDep25Hi': 300,
    'flat_6wkLo': 25, 'flat_6wkHi': 35,
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
        df = r['df']
        per = {}
        for t, label in [('Consolidation', 'consol'), ('Cup Without Handle', 'cupw'),
                         ('Flat Base', 'flat'), ('Cup With Handle', 'cuph'),
                         ('Double Bottom', 'db')]:
            sub = df[df['csv_type'] == t]
            per[label] = int(sub['exact'].sum())
        return {'cfg': cfg, 'exact': r['exact'], 'broad': r['broad'], 'pivot': r['pivot'],
                **per, 'err': None}
    except Exception as e:
        return {'cfg': cfg, 'exact': -1, 'broad': -1, 'pivot': -1,
                'consol': 0, 'cupw': 0, 'flat': 0, 'cuph': 0, 'db': 0, 'err': repr(e)}


def smoke():
    fe = FastEval(verbose=False)
    print("knob smoke test (each applied alone):")
    bad = []
    for knob in GROUPS['all']:
        v = next((x for x in SPACE[knob] if x != BASELINE_CFG.get(knob)), None)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', choices=list(GROUPS), default='cupconsol')
    ap.add_argument('--n', type=int, default=400)
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--seed', type=int, default=5)
    ap.add_argument('--smoke', action='store_true')
    ap.add_argument('--max-dims', type=int, default=4)
    ap.add_argument('--broad-floor', type=int, default=127,
                    help='reject configs whose broad/pivot-safe count drops below this')
    args = ap.parse_args()

    if args.smoke:
        sys.exit(smoke())

    knobs = GROUPS[args.group]
    rng = random.Random(args.seed)
    base = {k: BASELINE_CFG[k] for k in knobs}
    seen = {tuple(sorted(map(str, base.items())))}
    cfgs = [dict(base)]
    guard = 0
    while len(cfgs) < args.n and guard < args.n * 200:
        guard += 1
        c = dict(base)
        for knob in rng.sample(knobs, rng.randint(1, min(args.max_dims, len(knobs)))):
            alts = [v for v in SPACE[knob] if v != base.get(knob)]
            if alts:
                c[knob] = rng.choice(alts)
        key = tuple(sorted(map(str, c.items())))
        if key not in seen:
            seen.add(key)
            cfgs.append(c)

    print(f"divide-and-conquer search [group={args.group}]: {len(cfgs)} configs over {len(knobs)} knobs")
    print(f"baseline: exact 90, broad 127 (floor {args.broad_floor})")
    print(f"per-pattern exact baseline: Consol 7/36, CupWithoutH 39/59, Flat 15/24, CupH 20/46, DB 2/7\n")

    out = ROOT / "python" / f"dc_search_{args.group}.json"
    t0 = time.time()
    results, best = [], -1
    with ProcessPoolExecutor(max_workers=args.workers or None, initializer=_init_worker) as ex:
        for i, r in enumerate(ex.map(_score, cfgs, chunksize=1), 1):
            results.append(r)
            if r['broad'] >= args.broad_floor and r['exact'] > best:
                best = r['exact']
            if i % 20 == 0 or i == len(cfgs):
                print(f"  {i:>4}/{len(cfgs)}  best exact (broad>={args.broad_floor}): {best}  "
                      f"[{time.time()-t0:.0f}s]", flush=True)

    errs = [r for r in results if r['err']]
    if errs:
        print(f"\n{len(errs)} config(s) errored, e.g. {errs[0]['err']}")

    ok = [r for r in results if r['exact'] >= 0 and r['broad'] >= args.broad_floor]
    ok.sort(key=lambda x: (-x['exact'], -x['broad']))
    print(f"\n{'='*110}\nTOP 20 by EXACT (broad >= {args.broad_floor})\n{'='*110}")
    for r in ok[:20]:
        diff = {k: v for k, v in r['cfg'].items() if v != BASELINE_CFG.get(k)}
        print(f"  exact={r['exact']:>3} broad={r['broad']:>3} | Consol {r['consol']:>2}/36 "
              f"CupW {r['cupw']:>2}/59 Flat {r['flat']:>2}/24 CupH {r['cuph']:>2}/46 DB {r['db']}/7 | {diff}")

    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'err'} for r in ok], indent=2))
    print(f"\nfull results -> {out}")


if __name__ == '__main__':
    main()
