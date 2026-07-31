"""
Joint (multi-parameter) search over the scanner's classification thresholds.

Why joint and not one-at-a-time: the thresholds interact, so greedy sequential tuning is
order-dependent. Observed directly in this codebase - the optimum for `hdRatio` moved from
0.80 to 0.70 once an unrelated Double-Bottom change landed. Every prior tuning pass on this
file was greedy, so the current 44.1% is a coordinate-descent local optimum, not a joint one.

Method: random search (beats grid in >4 dims for the same budget), evaluated with the
in-memory harness at ~15s/config, parallelised across cores.

Discipline: at n=177, p0=0.441, a one-sided binomial needs ~+11 events for p<0.05. Configs
scoring under that are reported but flagged as noise and must not be committed as wins.

Usage:
    python3 python/joint_search.py --smoke          # verify every knob applies
    python3 python/joint_search.py --n 240          # run the search
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

from fast_eval import FastEval, BASELINE_EXACT, N_EVENTS, PIVOT_BASELINE_EXACT   # noqa: E402

# Objective. 'pivot' scores what actually matters for trading: Flat Base / Consolidation /
# Cup Without Handle / Ascending Base all buy at the base top, so confusing them is free.
# Only Double Bottom and Cup With Handle carry a different pivot, so only those must be
# identified correctly. 'exact' is the old strict-label objective.
OBJECTIVE = 'macro_f1_x1000'

# baseline value + reporting scale per objective. Macro-F1 is per-mille (x1000) so the
# integer ranking/printing path works unchanged.
OBJ_BASE = {'exact': BASELINE_EXACT, 'pivot': PIVOT_BASELINE_EXACT, 'macro_f1_x1000': 508}
OBJ_DENOM = {'exact': N_EVENTS, 'pivot': N_EVENTS, 'macro_f1_x1000': 1000}

# Search space. `None` means "leave the scanner's default expression untouched".
# Cup+Handle ranges are widened because 47 of the 68 pivot-metric errors sit there (25 false
# positives, 22 false negatives). Ground truth says the true handle depth averages 10.3% while
# the scanner's up-to-25-bar window measures ~20.8%, so short handle windows and tight depth
# caps are the region most likely to matter and were barely sampled before.
# `cup_depLo_short` is retained but is expected to be INERT under the pivot objective: it only
# moves events between Cup and Consolidation, which are the same bucket.
SPACE = {
    'cuph_inTop':      [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    'cuph_hdRatio':    [0.45, 0.55, 0.65, 0.70, 0.75, 0.80, 0.85, 0.95],
    'cuph_bCountMin':  [15, 20, 25, 30, 40, 50, 65],
    'cuph_rDepGate':   [6, 8, 10, 12, 15, 18],
    'cuph_hDepLo':     [0.0, 2.0, 3.0, 5.0, 7.0],
    'cuph_hDepMax':    [None, 10.0, 12.0, 15.0, 18.0, 20.0, 25.0, 30.0],
    'cuph_handleLen':  [None, 6, 8, 10, 12, 15, 20, 25],
    'cup_depLo_short': [8.0, 10.0, 12.0, 15.0, 18.0],
    'flat_rDep':       [15.0, 18.0, 20.0, 22.0, 25.0],
    'flat_rDep25':     [12.0, 15.0, 18.0],
    'db_cA_lo':        [0.80, 0.85, 0.90, 0.94],
    'db_cE_lo':        [0.70, 0.75, 0.85, 0.90],
}

# The committed baseline expressed in this space, so the search includes the incumbent.
BASELINE_CFG = {
    'cuph_inTop': 0.70, 'cuph_hdRatio': 0.80, 'cuph_bCountMin': 20,
    'cuph_rDepGate': 12, 'cuph_hDepLo': 2.0, 'cuph_hDepMax': None,
    'cuph_handleLen': None, 'cup_depLo_short': 12.0, 'flat_rDep': 20.0,
    'flat_rDep25': 15.0, 'db_cA_lo': 0.85, 'db_cE_lo': 0.75,
}

_FE = None


def _init_worker():
    global _FE
    _FE = FastEval(verbose=False)


def _clean(cfg):
    return {k: v for k, v in cfg.items() if v is not None}


def _dump(path, results):
    ok = sorted([r for r in results if r.get('score', -1) >= 0], key=lambda x: -x['score'])
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(
        [{'score': r['score'], 'exact': r['exact'], 'pivot': r['pivot'],
          'macro_f1_x1000': r.get('macro_f1_x1000'), 'focus_f1_x1000': r.get('focus_f1_x1000'),
          'cuph_recall': r['cuph_recall'], 'cuph_prec': r['cuph_prec'],
          'db_recall': r['db_recall'], 'cfg': r['cfg']} for r in ok], indent=2))
    tmp.replace(path)   # atomic, so a kill mid-write cannot corrupt the file


def _score(cfg):
    global _FE
    try:
        r = _FE.run(_clean(cfg))
        return {'cfg': cfg, 'score': r[OBJECTIVE], 'exact': r['exact'], 'broad': r['broad'],
                'pivot': r['pivot'], 'macro_f1_x1000': r['macro_f1_x1000'],
                'focus_f1_x1000': r['focus_f1_x1000'], 'cuph_recall': r['cuph_recall'],
                'cuph_prec': r['cuph_prec'], 'db_recall': r['db_recall'], 'err': None}
    except Exception as e:
        return {'cfg': cfg, 'score': -1, 'exact': -1, 'broad': -1, 'pivot': -1,
                'macro_f1_x1000': -1, 'focus_f1_x1000': -1, 'cuph_recall': 0.0, 'cuph_prec': 0.0, 'db_recall': 0.0, 'err': repr(e)}


def smoke():
    """Apply each knob on its own so a bad regex fails loudly rather than silently no-opping."""
    fe = FastEval(verbose=False)
    print("knob smoke test (each applied alone, non-default value):")
    bad = []
    for knob, vals in SPACE.items():
        v = next((x for x in vals if x is not None and x != BASELINE_CFG.get(knob)), None)
        if v is None:
            print(f"  {knob:<18} SKIP (no alternative value)")
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


def _sample_local(rng, centre, max_dims=3):
    """Perturb only k dims of `centre`.

    Pure random search over 12 dims spends nearly all its budget far from the incumbent, so
    it cannot distinguish "no headroom near baseline" from "never looked near baseline".
    Coordinate-descent tuning already explored k=1; k in 2..3 covers the pairwise/triple
    interactions that greedy tuning provably misses, which is the whole point of this exercise.
    """
    c = dict(centre)
    k = rng.randint(1, max_dims)
    for knob in rng.sample(list(SPACE), k):
        alts = [v for v in SPACE[knob] if v != centre.get(knob)]
        if alts:
            c[knob] = rng.choice(alts)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=240, help='configs to sample')
    ap.add_argument('--workers', type=int, default=0, help='0 = cpu_count')
    ap.add_argument('--seed', type=int, default=17)
    ap.add_argument('--smoke', action='store_true')
    ap.add_argument('--mode', choices=['global', 'local', 'mixed'], default='mixed',
                    help="local = perturb <=3 knobs off baseline (finds interactions greedy "
                         "tuning misses); global = uniform over the whole space")
    ap.add_argument('--max-dims', type=int, default=3)
    ap.add_argument('--class-window', action='store_true',
                    help='evaluate with the re-anchored classification window')
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

    if args.class_window:
        for c in cfgs:
            c['use_class_window'] = True

    workers = args.workers or None
    _BASE = OBJ_BASE[OBJECTIVE]
    _DEN = OBJ_DENOM[OBJECTIVE]
    # F1 has no closed-form binomial bar; bootstrap the final candidate instead. Use a
    # deliberately demanding provisional bar so nothing marginal gets called a win.
    bar = (FastEval.pivot_significance_delta() if OBJECTIVE=='pivot'
           else FastEval.significance_delta() if OBJECTIVE=='exact' else 30)
    print(f"joint search [{args.mode}{', class-window' if args.class_window else ''}]: "
          f"{len(cfgs)} configs over {len(SPACE)} interacting knobs")
    print(f"objective={OBJECTIVE}  baseline {_BASE}/{_DEN} ({_BASE/_DEN*100:.1f}%);  bar +{bar}\n")

    out = ROOT / "python" / "joint_search_results.json"
    t0 = time.time()
    results = []
    best_seen = -1
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        for i, r in enumerate(ex.map(_score, cfgs, chunksize=1), 1):
            results.append(r)
            # Persist incrementally: a search that is interrupted (or killed for resources)
            # must not lose the winning config, which is exactly what happened once already.
            if r['score'] > best_seen:
                best_seen = r['score']
                _dump(out, results)
            if i % 20 == 0 or i == len(cfgs):
                el = time.time() - t0
                print(f"  {i:>4}/{len(cfgs)}  best so far {best_seen}/{_DEN}  "
                      f"({best_seen/_DEN*100:.1f}%)  [{el:.0f}s elapsed]", flush=True)
                _dump(out, results)

    errs = [r for r in results if r['err']]
    if errs:
        print(f"\n{len(errs)} config(s) errored, e.g. {errs[0]['err']}")

    ok = sorted([r for r in results if r['score'] >= 0], key=lambda x: -x['score'])
    print(f"\n{'='*78}\nTOP 15 CONFIGS\n{'='*78}")
    for r in ok[:15]:
        d = r['score'] - _BASE
        tag = "SIGNIFICANT" if d >= bar else ("noise" if d > 0 else "")
        diff = {k: v for k, v in r['cfg'].items()
                if k != 'use_class_window' and v != BASELINE_CFG.get(k)}
        print(f"  {r['score']:>4}/{_DEN} ({r['score']/_DEN*100:>5.1f}%)  "
              f"CupH r/p {r['cuph_recall']*100:>4.0f}/{r['cuph_prec']*100:<4.0f} DB {r['db_recall']*100:>4.0f}  "
              f"delta {d:>+3} {tag:<12} {diff if diff else '= baseline'}")

    out = ROOT / "python" / "joint_search_results.json"
    out.write_text(json.dumps(
        [{'score': r['score'], 'exact': r['exact'], 'pivot': r['pivot'],
          'macro_f1_x1000': r.get('macro_f1_x1000'), 'focus_f1_x1000': r.get('focus_f1_x1000'),
          'cuph_recall': r['cuph_recall'], 'cuph_prec': r['cuph_prec'],
          'db_recall': r['db_recall'], 'cfg': r['cfg']} for r in ok], indent=2))
    print(f"\nfull results -> {out}")

    best = ok[0]
    d = best['score'] - _BASE
    print(f"\nbest: {best['score']}/{_DEN} ({best['score']/_DEN*100:.1f}%), delta {d:+d}")
    if d < bar:
        print(f"VERDICT: below the +{bar} significance bar - this is threshold noise, "
              f"NOT a real improvement. Do not commit as a win.")
    else:
        print(f"VERDICT: clears the +{bar} bar. Re-verify with evaluate_breakaway_gap.py "
              f"before committing.")


if __name__ == '__main__':
    main()
