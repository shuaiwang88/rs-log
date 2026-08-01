"""
Unified search across ALL pattern knobs at once (Cup+Handle, Double Bottom, Cup Without
Handle, Consolidation, Flat Base).

The per-group searches (focus_search.py for the pivot-critical pair, dc_search.py for the
bTop group) each optimised their own cluster while holding the other fixed. The patterns
interact through the priority chain, so a change that looks neutral inside one group can
unlock or block detections in another. This runs the combined space to catch exactly those
cross-group interactions.

Objective: maximise EXACT, with a floor on BROAD (pivot-safe) so exact gains can't be
bought by giving back buy-point accuracy.

Usage:
    python3 python/unified_search.py --smoke
    python3 python/unified_search.py --n 600
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

from fast_eval import FastEval, N_EVENTS          # noqa: E402
import focus_search as FS                          # noqa: E402
import dc_search as DC                             # noqa: E402

SPACE = {**FS.SPACE, **DC.SPACE}
BASELINE_CFG = {**FS.BASELINE_CFG, **DC.BASELINE_CFG}

_FE = None


def _init_worker():
    global _FE
    _FE = FastEval(verbose=False)


def _score(cfg):
    global _FE
    try:
        r = _FE.run({k: v for k, v in cfg.items() if v is not None})
        df = r['df']
        per = {}
        for t, lab in [('Consolidation', 'consol'), ('Cup Without Handle', 'cupw'),
                       ('Flat Base', 'flat'), ('Cup With Handle', 'cuph'),
                       ('Double Bottom', 'db')]:
            per[lab] = int(df[df['csv_type'] == t]['exact'].sum())
        return {'cfg': cfg, 'exact': r['exact'], 'broad': r['broad'], 'pivot': r['pivot'],
                **per, 'err': None}
    except Exception as e:
        return {'cfg': cfg, 'exact': -1, 'broad': -1, 'pivot': -1, 'consol': 0, 'cupw': 0,
                'flat': 0, 'cuph': 0, 'db': 0, 'err': repr(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=600)
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--seed', type=int, default=3)
    ap.add_argument('--max-dims', type=int, default=5)
    ap.add_argument('--broad-floor', type=int, default=125)
    ap.add_argument('--smoke', action='store_true')
    args = ap.parse_args()

    if args.smoke:
        fe = FastEval(verbose=False)
        r = _score.__wrapped__ if False else None
        global _FE
        _FE = fe
        base = _score(dict(BASELINE_CFG))
        print(f"combined knobs: {len(SPACE)}")
        print(f"baseline reproduces: exact={base['exact']} broad={base['broad']} "
              f"| Consol {base['consol']}/36 CupW {base['cupw']}/59 Flat {base['flat']}/24 "
              f"CupH {base['cuph']}/46 DB {base['db']}/7")
        return

    rng = random.Random(args.seed)
    knobs = list(SPACE)
    seen = {tuple(sorted(map(str, BASELINE_CFG.items())))}
    cfgs = [dict(BASELINE_CFG)]
    guard = 0
    while len(cfgs) < args.n and guard < args.n * 200:
        guard += 1
        c = dict(BASELINE_CFG)
        for knob in rng.sample(knobs, rng.randint(1, args.max_dims)):
            alts = [v for v in SPACE[knob] if v != BASELINE_CFG.get(knob)]
            if alts:
                c[knob] = rng.choice(alts)
        key = tuple(sorted(map(str, c.items())))
        if key not in seen:
            seen.add(key)
            cfgs.append(c)

    print(f"unified search: {len(cfgs)} configs over {len(SPACE)} knobs (all patterns)")
    print(f"baseline exact 89 / broad 125;  broad floor {args.broad_floor}\n")

    out = ROOT / "python" / "unified_search_results.json"
    t0, results, best = time.time(), [], -1
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
        print(f"\n{len(errs)} errored, e.g. {errs[0]['err']}")

    ok = [r for r in results if r['exact'] >= 0 and r['broad'] >= args.broad_floor]
    ok.sort(key=lambda x: (-x['exact'], -x['broad']))
    print(f"\n{'='*115}\nTOP 20 by EXACT (broad >= {args.broad_floor})\n{'='*115}")
    for r in ok[:20]:
        diff = {k: v for k, v in r['cfg'].items() if v != BASELINE_CFG.get(k)}
        print(f"  exact={r['exact']:>3} broad={r['broad']:>3} | Consol {r['consol']:>2} CupW {r['cupw']:>2} "
              f"Flat {r['flat']:>2} CupH {r['cuph']:>2} DB {r['db']} | {diff}")

    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'err'} for r in ok], indent=2))
    print(f"\nfull results -> {out}")


if __name__ == '__main__':
    main()
