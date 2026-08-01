"""
Gate-attribution diagnostic for the Cup+Handle detector.

Cup+Handle is the dominant remaining pivot error: recall 18/46, and every miss reports the
base-top pivot instead of the (lower) handle high, so the buy point is wrong. Random sweeps
over its 12 knobs can't say WHY a given event was rejected - they only report the aggregate.

This instruments the Cup+Handle block so every gate is evaluated and recorded on every bar,
then reports, for the 46 ground-truth Cup With Handle events, which gate did the rejecting.
Semantics are unchanged: the instrumented block still sets isCupH only when the full
original conjunction holds, and `verify()` asserts the instrumented scanner reproduces the
committed 89/172 exact + 126 pivot-safe baseline before any numbers are reported.

Usage:
    python3 python/cuph_diagnose.py            # gate attribution over the 46 events
    python3 python/cuph_diagnose.py --verify   # equivalence check only
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval, BASELINE_EXACT, PIVOT_BASELINE_EXACT   # noqa: E402

# The production block, matched verbatim so a scanner edit makes this fail loudly instead
# of silently diagnosing stale logic.
_ORIG_START = "            # 6. Cup With Handle (independent of cup detection"
_ORIG_END = "            # 5. Cup Without Handle"

_INSTRUMENTED = '''
            # 6. Cup With Handle -- INSTRUMENTED (isCupH semantics identical to production)
            cupH_allowed = (not prevIsFlatBase or not isFlatBase) or (bDepPct is not None and bDepPct >= 25.0)
            _g_struct = bool(isBase and bTop and bLow and cupMid)
            _g_bCount = bool(bCount >= 20)
            _g_flatGuard = bool(cupH_allowed)
            _g_bDep = bool(bDepPct is not None and 20.0 <= bDepPct <= 50.0)
            _g_rDep = bool(rDepPct > 12)
            cuphDiag = {
                'struct': _g_struct, 'bCount': _g_bCount, 'flatGuard': _g_flatGuard,
                'bDep': _g_bDep, 'rDep': _g_rDep,
                'bDepPct': float(bDepPct) if bDepPct is not None else None,
                'rDepPct': float(rDepPct), 'bCountVal': int(bCount),
            }
            if _g_struct:
                handle_len = 15
                is_cuph_bo = (boPatternName == 'Cup+Handle')
                end_h_idx = max(1, min(i - 1, boBar - 1 if (boBar is not None and i - boBar <= 10 and is_cuph_bo) else i - 1))
                _trim = 0
                while end_h_idx > 1 and _trim < 10 and highs[end_h_idx] >= np.max(highs[max(0, end_h_idx - 10):end_h_idx]):
                    end_h_idx -= 1
                    _trim += 1
                w12_start = max(0, end_h_idx - handle_len)
                H12 = np.max(highs[w12_start:end_h_idx + 1]) if end_h_idx >= w12_start else highs[i]
                L12 = np.min(lows[w12_start:end_h_idx + 1])
                hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999.0
                inTop = (L12 >= cupMid * 0.95)
                max_hDep = 20.0 if bCount > 250 else 30.0
                depOk_h = (5.0 <= hDep <= max_hDep)
                ref_vol = sma20_vol[w12_start] if (w12_start < len(sma20_vol) and not np.isnan(sma20_vol[w12_start])) else None
                handle_avg_vol = np.mean(volumes[w12_start:end_h_idx + 1])
                volOk_h = (ref_vol is None or ref_vol <= 0) or (handle_avg_vol < ref_vol * 1.15)
                slopeOk_h = True
                _hw2 = highs[w12_start:end_h_idx + 1]
                if len(_hw2) >= 4 and H12 > 0:
                    _hx2 = np.arange(len(_hw2), dtype=float)
                    _hxm2 = _hx2 - _hx2.mean()
                    _hsl2 = float((_hxm2 * (_hw2 - _hw2.mean())).sum() / (_hxm2 * _hxm2).sum()) / H12 * 100.0
                    slopeOk_h = (_hsl2 <= 0.60)
                _g_h12Cap = bool(H12 < bTop * 1.02)
                hdRatio = hDep / bDepPct if bDepPct and bDepPct > 0 else 1.0
                _g_hdRatio = bool(hdRatio <= 0.45)
                cuphDiag.update({
                    'inTop': bool(inTop), 'depOk_h': bool(depOk_h), 'volOk_h': bool(volOk_h),
                    'slopeOk_h': bool(slopeOk_h),
                    'h12Cap': _g_h12Cap, 'hdRatio_ok': _g_hdRatio,
                    'hDep': float(hDep), 'hdRatio': float(hdRatio),
                    'L12_over_cupMid': float(L12 / cupMid) if cupMid else None,
                    'H12_over_bTop': float(H12 / bTop) if bTop else None,
                    'volRatio': float(handle_avg_vol / ref_vol) if (ref_vol and ref_vol > 0) else None,
                })
                # --- candidate NEW features (not used by production; separability probe) ---
                # IBD: a sound handle DRIFTS DOWN along its lows and dries up on volume; a
                # handle that wedges upward is faulty. The scanner computes neither.
                _w = highs[w12_start:end_h_idx + 1]
                _wl = lows[w12_start:end_h_idx + 1]
                _wv = volumes[w12_start:end_h_idx + 1]
                _wc = closes[w12_start:end_h_idx + 1]
                if len(_wl) >= 4 and H12 > 0:
                    _x = np.arange(len(_wl), dtype=float)
                    _slope_lo = float(np.polyfit(_x, _wl, 1)[0]) / H12 * 100.0
                    _slope_hi = float(np.polyfit(_x, _w, 1)[0]) / H12 * 100.0
                    _h = len(_wv) // 2
                    _vtrend = float(np.mean(_wv[_h:]) / np.mean(_wv[:_h])) if np.mean(_wv[:_h]) > 0 else None
                    cuphDiag.update({
                        'h_slope_lo': _slope_lo,        # %/bar drift of the handle's lows
                        'h_slope_hi': _slope_hi,        # %/bar drift of the handle's highs
                        'h_vol_trend': _vtrend,         # 2nd half vol / 1st half vol
                        'h_close_pos': float((_wc[-1] - L12) / (H12 - L12)) if H12 > L12 else None,
                        'h_tightness': float(np.mean((_w - _wl) / _w) * 100.0),
                    })
                if (_g_bCount and _g_flatGuard and _g_bDep and _g_rDep
                        and inTop and depOk_h and volOk_h and slopeOk_h and _g_h12Cap and _g_hdRatio):
                    isCupH = True
                    cupHandlePivot = H12

'''

# Gate order = evaluation order, so "first failing gate" is well defined.
GATE_ORDER = ['struct', 'bCount', 'flatGuard', 'bDep', 'rDep',
              'inTop', 'depOk_h', 'volOk_h', 'slopeOk_h', 'h12Cap', 'hdRatio_ok']


def instrumented_source(fe):
    src = fe._src
    si = src.index(_ORIG_START)
    ei = src.index(_ORIG_END, si)
    src = src[:si] + _INSTRUMENTED + src[ei:]
    # Carry the per-bar diagnostic out through the state record.
    anchor = "                'isCupH': isCupH,"
    if anchor not in src:
        raise RuntimeError("state-dict anchor not found in scanner source")
    return src.replace(anchor, anchor + "\n                'cuphDiag': cuphDiag,", 1)


def make_fe():
    fe = FastEval(verbose=False)
    fe._src = instrumented_source(fe)
    return fe


def verify(fe):
    """Instrumentation must not change any detection."""
    r = fe.run(label='instrumented')
    ok = (r['exact'] == BASELINE_EXACT and r['pivot'] == PIVOT_BASELINE_EXACT)
    print(f"equivalence check: exact {r['exact']}/{BASELINE_EXACT}  "
          f"pivot-safe {r['pivot']}/{PIVOT_BASELINE_EXACT}  -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("instrumented scanner diverged from production; diagnosis aborted")
    return r


def decision_bar(res):
    """The bar whose classification produced the reported label.

    In-base events are classified on the last bar; post-breakout events had their label
    latched at the breakout bar, so that is where the gates actually ran.
    """
    latest = res['history'][-1]
    if latest['inBase'] or latest['boBar'] is None:
        return len(res['history']) - 1
    return latest['boBar']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verify', action='store_true')
    args = ap.parse_args()

    fe = make_fe()
    base = verify(fe)
    if args.verify:
        return

    scan = fe._load_scanner({})
    det = base['df'].set_index('symbol')['detected'].to_dict()

    rows = []
    for key, sym, btype in fe._events:
        if btype != 'Cup With Handle':
            continue
        try:
            res = scan(sym, key)
        except Exception:
            res = None
        if res is None:
            rows.append({'symbol': sym, 'detected': 'None', 'hit': False, 'first_fail': 'undetected'})
            continue
        d = res['history'][decision_bar(res)].get('cuphDiag') or {}
        first_fail = next((g for g in GATE_ORDER if not d.get(g, False)), None)
        rows.append({
            'symbol': sym, 'detected': res['pattern_name'],
            'hit': res['pattern_name'] == 'Cup+Handle',
            'first_fail': first_fail or 'PASS',
            **{g: d.get(g) for g in GATE_ORDER},
            **{k: d.get(k) for k in ('bDepPct', 'rDepPct', 'bCountVal', 'hDep', 'hdRatio',
                                     'L12_over_cupMid', 'H12_over_bTop', 'volRatio')},
        })

    df = pd.DataFrame(rows)
    hits, miss = df[df['hit']], df[~df['hit']]
    print(f"\nCup With Handle ground truth: {len(df)} events, {len(hits)} detected, {len(miss)} missed\n")

    print("=" * 72)
    print("FIRST GATE TO FAIL, over the misses  (evaluation order)")
    print("=" * 72)
    for g, c in Counter(miss['first_fail']).most_common():
        print(f"  {g:<14} {c:>3}   ({c/len(miss)*100:.0f}% of misses)")

    print("\n" + "=" * 72)
    print("EVERY gate that fails, over the misses (a miss can fail several)")
    print("=" * 72)
    for g in GATE_ORDER:
        if g in miss.columns:
            c = int((miss[g] == False).sum())  # noqa: E712  (None must not count as False)
            print(f"  {g:<14} {c:>3}   ({c/len(miss)*100:.0f}% of misses)")

    print("\n" + "=" * 72)
    print("MARGIN ANALYSIS - how far off are the misses that reach the handle gates?")
    print("=" * 72)
    reach = miss[miss['inTop'].notna()]
    print(f"  {len(reach)}/{len(miss)} misses reach the handle-window gates")
    for col, thr, sense in [('L12_over_cupMid', 0.95, 'ge'), ('hdRatio', 0.45, 'le'),
                            ('hDep', 5.0, 'ge'), ('H12_over_bTop', 1.02, 'lt'),
                            ('volRatio', 1.15, 'lt')]:
        s = reach[col].dropna()
        if len(s):
            print(f"  {col:<17} thr {thr:<6} ({sense})  misses: "
                  f"min {s.min():.2f} p25 {s.quantile(.25):.2f} med {s.median():.2f} "
                  f"p75 {s.quantile(.75):.2f} max {s.max():.2f}")
        h = hits[col].dropna()
        if len(h):
            print(f"  {'':17}                    hits:   "
                  f"min {h.min():.2f} p25 {h.quantile(.25):.2f} med {h.median():.2f} "
                  f"p75 {h.quantile(.75):.2f} max {h.max():.2f}")

    out = ROOT / "python" / "cuph_diagnosis.csv"
    df.to_csv(out, index=False)
    print(f"\nper-event detail -> {out}")


if __name__ == '__main__':
    main()
