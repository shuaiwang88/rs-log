"""
Fast in-memory evaluation harness for `ibd_pattern_scanner copy.py`.

The stock evaluator (`evaluate_breakaway_gap.py`) re-reads every ticker's full parquet and
writes+reads a temp parquet per event, so one run costs 2-4 minutes. That makes a joint
parameter search impossible. This harness:

  * slices the 177 event windows ONCE and keeps them in memory,
  * feeds them to the scanner through a pandas shim, so `scan_single_ticker` needs no edit,
  * loads scanner variants with exec() so a whole sweep runs in a single process,
  * reports the confusion matrix plus a PAIRED diff against a stored baseline.

Correctness contract: `run()` with no overrides must reproduce the committed baseline of
78/177 exactly. `self_test()` asserts this.

Significance: with n=177 and p0=0.441, a one-sided binomial needs ~+11 events for p<0.05.
Anything smaller is threshold noise and must not be booked as an improvement.
"""
import os
import re
import sys
import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SCANNER_SRC = ROOT / "python" / "ibd_pattern_scanner copy.py"
CACHE_PATH = ROOT / "python" / ".fast_eval_windows.pkl"
# Ascending Base excluded: only 5/177 ground-truth events, and its "not isCupH" guard
# let it steal bars from Cup/Cup+Handle/Consolidation once those got tightened. Detection
# is disabled in the scanner (isAscendingBase forced False) and excluded here to match.
EXCLUDED_TRUTH_TYPES = {'Ascending Base'}
# Baseline after the base-pivot correction: quote the base's HIGHEST HIGH (8-bar lag, no
# fudge factor) instead of the ratcheted bTop. Verified against evaluate_breakaway_gap.py
# --target copy: labels unchanged at 90/172 exact and 127 broad (the change is
# reporting-only), while the buy price improves at every band - within 1% 44 -> 101,
# within 3% 101 -> 121, median error 2.50% -> 0.02%, mean 5.38% -> 4.01%.
BASELINE_EXACT = 90
N_EVENTS = 172

# --- pivot-equivalence scoring -------------------------------------------------------
# The label only matters insofar as it sets the buy point. Flat Base, Consolidation and Cup
# Without Handle all pivot off the base top, so confusing them costs nothing. Double Bottom
# (middle peak) and Cup With Handle (handle high) pivot elsewhere, so mixing those up - in
# either direction - moves the entry price and IS an error.
STD_TRUTH = {'Flat Base', 'Consolidation', 'Cup Without Handle'}
STD_DET = {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup', 'Base'}
FOCUS_MAP = {'Double Bottom': 'Dbl Bottom', 'Cup With Handle': 'Cup+Handle'}
PIVOT_BASELINE_EXACT = 126
BROAD_BASELINE = 126


def bucket_truth(t):
    return FOCUS_MAP.get(t, 'StdPivot' if t in STD_TRUTH else t)


def bucket_det(d):
    return d if d in ('Dbl Bottom', 'Cup+Handle') else ('StdPivot' if d in STD_DET else d)


EXACT_NAME_MAP = {
    'Cup Without Handle': {'Cup'},
    'Cup With Handle': {'Cup+Handle'},
    'Flat Base': {'Flat Base', '6-Wk Flat'},
    'Consolidation': {'Consolidation'},
    'Double Bottom': {'Dbl Bottom'},
}
BROAD_NAME_MAP = {
    'Cup Without Handle': {'Cup', 'Base', 'Consolidation', 'Flat Base', '6-Wk Flat'},
    'Cup With Handle': {'Cup+Handle'},
    'Flat Base': {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup'},
    'Consolidation': {'Consolidation', 'Base', 'Flat Base', 'Cup', '6-Wk Flat'},
    'Double Bottom': {'Dbl Bottom'},
}


def _clean_date(d):
    d = str(d).strip()
    m = re.match(r'^(\d{2})/(\d{2})(\d{4})$', d)
    return f'{m.group(1)}/{m.group(2)}/{m.group(3)}' if m else d


_CLASS_WINDOW_CODE = '''
            # --- classification window (experiment; independent of the base state machine) ---
            # `bCount` spans several merged bases (median 2.15x the ground-truth Length) because
            # a base never closes on breakout, so `bDepPct` correlates only 0.44 with true Depth.
            # Re-anchor a window at the cup's left lip purely for CLASSIFICATION. Pivot price,
            # distPct, breakout detection and days_in_base keep using bTop/bLow/bCount.
            cTop, cLow, cLen = bTop, bLow, bCount
            if isBase and bTop:
                _lip = None
                for (_b, _p) in aHP_list:          # newest-first
                    if _b <= i - 10 and _p >= 0.97 * bTop:
                        _lip = _b
                    elif _lip is not None and _b < _lip and np.max(highs[_b:_lip]) > bTop * 1.02:
                        break
                if _lip is not None and i - _lip >= 20:
                    cTop = float(np.max(highs[_lip:i + 1]))
                    cLow = float(np.min(lows[_lip:i + 1]))
                    cLen = i - _lip + 1
            cDepPct = (cTop - cLow) / cTop * 100.0 if (cTop and cLow and cTop > 0) else None
'''

# Blocks whose depth/length gates should read the classification window instead of the base
# state machine. (start_anchor, end_anchor) - exclusive of the end anchor.
_CW_BLOCKS = [
    ("            # 6. Cup With Handle", "            # 5. Cup Without Handle"),
    ("            # 5. Cup Without Handle", "            # Flat Base (guarded by not isCupH)"),
    ("            # Flat Base (guarded by not isCupH)", "            isDeepBase = isBase"),
]


def _apply_handle_uphalf(src, tol, floor=None):
    """Webster's ACTUAL definition of "handle in the upper half of the base".

    From the phase-1 walkthrough: "You take the high of the base, the low of the base, you
    just take that average. Let's say that average is $50. Then you do the same thing for
    the handle. You take the high of the handle, the low of the handle. That average just
    has to be higher than that 50. Even if it's 50 and one penny, that's in the upper half...
    Sometimes it'll be in what visually looks like the lower half, but it's not."

    So the test is MIDPOINT of handle vs MIDPOINT of base. The scanner instead tests the
    handle's LOW against the cup midpoint (inTop = L12 >= cupMid * 0.95), which is a
    different and much harsher condition - a handle may dip below the midpoint as long as
    its average stays above. inTop rejects 12 of the 28 Cup+Handle misses, with L12/cupMid
    bunched just under 1.0 (p25 0.92, median 0.95), exactly where the two rules disagree.

    Note cupMid is already (bTop + bLow) / 2, so the base side needs no change.
    """
    anchor = "                inTop = (L12 >= cupMid * 0.95)"
    if anchor not in src:
        raise RuntimeError("handle_uphalf anchor not found in scanner source")
    cond = f"(H12 + L12) / 2.0 >= cupMid * {tol}"
    if floor is not None:
        cond += f" and L12 >= cupMid * {floor}"
    return src.replace(anchor, f"                inTop = ({cond})", 1)


def _add_orig_btop(src):
    """Track the base top as first established, alongside the ratcheting bTop."""
    if "origBTop = None" in src:
        return src
    src = src.replace("        lastBTop = None", "        lastBTop = None\n        origBTop = None", 1)
    return src.replace("                bStart = piv_idx",
                       "                bStart = piv_idx\n                origBTop = highs[piv_idx]", 1)


def _apply_bo_on_orig(src):
    """Fire the breakout off the FROZEN base top; keep the ratcheted bTop for the pivot.

    The ratchet does two jobs at once and they conflict. It keeps the reported pivot
    accurate for a still-forming base (snapshot piv3 101/164 - nothing else comes close),
    but because bTop follows price up, a slow grind never clears it and the breakout never
    fires: only 98 of 172 events see a breakout within +/-2 days of IBD's date. Disabling
    the ratchet fixes the trigger (142) and ruins the pivot (piv3 62).

    They are separable: trigger the breakout when price clears the base top as originally
    established, while pivRef keeps using the ratcheted bTop while in base.
    """
    src = _add_orig_btop(src)
    a = ("            active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) "
         "else (cupHandlePivot if (isCupH and cupHandlePivot is not None) else bTop)")
    if a not in src:
        raise RuntimeError("bo_on_orig anchor not found")
    return src.replace(a, ("            active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) "
                           "else (cupHandlePivot if (isCupH and cupHandlePivot is not None) "
                           "else (origBTop if origBTop is not None else bTop))"), 1)


def _apply_ratchet_cap(src, cap):
    """Let bTop ratchet, but never more than `cap` above the base top it started at.

    The uncapped 5%-per-bar ratchet lets the base top follow price indefinitely, so a slow
    grind never clears it and no breakout fires: the scanner registers a breakout within
    +/-2 days of IBD's date on only 98 of 172 events. Disabling the ratchet entirely fixes
    that (142) but freezes bTop at the base's FIRST pivot high, so the reported pivot for a
    still-forming base is too low (snapshot piv3 101 -> 62). Capping total drift keeps the
    base top adaptive early while guaranteeing price can still clear it.
    """
    src = _add_orig_btop(src)
    r = "                if bTop is not None and highs[i] > bTop and highs[i] <= bTop * 1.05:"
    if r not in src:
        raise RuntimeError("ratchet_cap ratchet anchor not found")
    return src.replace(r, "                if bTop is not None and highs[i] > bTop and highs[i] <= bTop * 1.05 "
                          f"and (origBTop is None or highs[i] <= origBTop * {cap}):", 1)


def _apply_independent(src):
    """Judge each pattern on its own merits, dropping the mutual-exclusion guards.

    isCup requires `not isCupH`, isFlatBase requires `not isCupH`, isDB requires
    `not isFlatBase and not isCupH`, and isConsolidation requires none of the others. So the
    detectors can never disagree - the taxonomy is enforced by construction rather than
    measured. That makes the "two valid readings" Webster describes unrepresentable.
    """
    reps = [
        ("            isCup = False\n            if isBase and bTop and bLow and not isCupH:",
         "            isCup = False\n            if isBase and bTop and bLow:"),
        ("            isFlatBase = isBase and (rDepPct <= 20.0) and (20 <= bCount <= 130) and not isCupH and not isLikelyConsolidation",
         "            isFlatBase = isBase and (rDepPct <= 20.0) and (20 <= bCount <= 130) and not isLikelyConsolidation"),
        ("            if isBase and not isFlatBase and not isCupH and not isLikelyConsolidation and (bDepPct is not None and 15.0 <= bDepPct <= 40.0)",
         "            if isBase and not isLikelyConsolidation and (bDepPct is not None and 15.0 <= bDepPct <= 40.0)"),
        ("                (bCount > 200 and not isCup and not isCupH) or",
         "                (bCount > 200) or"),
        ("                (bDepPct is not None and 5.0 <= bDepPct <= 35.0 and not isCup and not isCupH and not isFlatBase and not isDB and not isAscendingBase)",
         "                (bDepPct is not None and 5.0 <= bDepPct <= 35.0)"),
    ]
    for a, b in reps:
        if a not in src:
            raise RuntimeError(f"independent anchor missing: {a[:60]}")
        src = src.replace(a, b, 1)
    return _apply_multilabel(src)


def _apply_multilabel(src):
    """Expose every pattern flag that is simultaneously true, not just the priority winner.

    Webster is explicit that a base can carry more than one valid reading - "we could both
    look at the same chart and see it differently, and Bill would agree we were both right"
    - and lists layering them as a later phase. The measured ambiguity backs this: IBD's own
    Depth+Length recover their own labels only 43.6% of the time. The scanner already
    computes isCup / isCupH / isDB / isFlatBase / isConsolidation every bar and then discards
    all but the highest-priority one.
    """
    anchor = "                'isCupH': isCupH,"
    if anchor not in src:
        raise RuntimeError("multilabel anchor not found")
    return src.replace(anchor, anchor + ("\n                'isCup': isCup,"
                                         "\n                'isConsol': isConsolidation,"), 1)


def _apply_db_close_match(src, tol_lo, tol_hi, undercut_on_close):
    """Match the two bottoms on CLOSES rather than wicks.

    The Patternsmart double-bottom detector exposes a "use High Low" switch - i.e. whether
    the two bottoms are compared on wicks or on open/close. Our scanner matches pivot LOWS,
    which are the noisiest points on the bar: a single intraday spike moves the comparison.
    Closes are where the market actually settled.
    """
    a = "                            cA = (sL <= fL * 1.04) and (sL >= fL * 0.94)"
    if a not in src:
        raise RuntimeError("db_close_match cA anchor not found")
    src = src.replace(a, f"                            cA = (closes[sLt] <= closes[fLt] * {tol_hi}) and (closes[sLt] >= closes[fLt] * {tol_lo})", 1)
    if undercut_on_close:
        b = "                            second_leg_undercut = sL < fL"
        if b not in src:
            raise RuntimeError("db_close_match undercut anchor not found")
        src = src.replace(b, "                            second_leg_undercut = closes[sLt] < closes[fLt]", 1)
    return src


def _apply_db_priority(src):
    """Let a fully-qualified Double Bottom outrank Flat Base.

    DBX satisfies every Double Bottom condition - both lows, the middle peak, the undercut,
    the timing and the volume asymmetry - and is still reported as a Flat Base, because the
    DB block is guarded by `not isFlatBase` AND Flat Base sits above it in the priority
    chain. A W that qualifies is more specific than a flat range and prices off a different
    level (the middle peak), so it should win.
    """
    g = "if isBase and not isFlatBase and not isCupH and not isLikelyConsolidation"
    if g not in src:
        raise RuntimeError("db_priority guard anchor not found")
    src = src.replace(g, "if isBase and not isCupH and not isLikelyConsolidation", 1)
    for a, b in [
        ("            if isAscendingBase: currPName, currPCode = 'Ascending Base', 8\n"
         "            elif is6WkFlat: currPName, currPCode = '6-Wk Flat', 7\n"
         "            elif isFlatBase: currPName, currPCode = 'Flat Base', 2\n"
         "            elif isDB: currPName, currPCode = 'Dbl Bottom', 5",
         "            if isAscendingBase: currPName, currPCode = 'Ascending Base', 8\n"
         "            elif isDB: currPName, currPCode = 'Dbl Bottom', 5\n"
         "            elif is6WkFlat: currPName, currPCode = '6-Wk Flat', 7\n"
         "            elif isFlatBase: currPName, currPCode = 'Flat Base', 2"),
        ("                elif is6WkFlat: pName, pCode, pOn = '6-Wk Flat', 7, True\n"
         "                elif isFlatBase: pName, pCode, pOn = 'Flat Base', 2, True\n"
         "                elif isDB: pName, pCode, pOn = 'Dbl Bottom', 5, True",
         "                elif isDB: pName, pCode, pOn = 'Dbl Bottom', 5, True\n"
         "                elif is6WkFlat: pName, pCode, pOn = '6-Wk Flat', 7, True\n"
         "                elif isFlatBase: pName, pCode, pOn = 'Flat Base', 2, True")]:
        if a not in src:
            raise RuntimeError("db_priority chain anchor not found")
        src = src.replace(a, b, 1)
    return src


def _apply_asc_tight(src, lo, hi, pbmin, pbmax):
    """Enable Ascending Base with IBD's actual definition: 9-16 weeks, three pullbacks.

    The scanner's block never constrained the pattern's LENGTH, so it fired on any three
    stair-steps inside a 90-bar window - 22 times across 177 events, catching 3 of 5 real
    ones and costing 14 broad matches. IBD is specific: 9 to 16 weeks (45-80 daily bars).
    """
    a = "            if False and isBase and not isCupH and not isLikelyConsolidation"
    if a not in src:
        raise RuntimeError("asc_tight anchor not found")
    src = src.replace(a, "            if isBase and not isCupH and not isLikelyConsolidation", 1)
    b = "                    if t_spaced and hh and hl and pb_ok:"
    if b not in src:
        raise RuntimeError("asc_tight gate anchor not found")
    new = ("                    _span = recent_hps[2][0] - recent_hps[0][0]\n"
           f"                    _lenOk = {lo} <= bCount <= {hi}\n"
           f"                    _pbOk2 = all({pbmin} <= _p <= {pbmax} for _p in [pb1, pb2, pb3])\n"
           "                    if t_spaced and hh and hl and pb_ok and _lenOk and _pbOk2:")
    return src.replace(b, new, 1)


def _apply_enable_asc(src):
    """Re-enable Ascending Base detection (disabled early as 'stealing bars' from Cup/CupH).

    That verdict predates the handle-window trim, the drift gate and the base-pivot rewrite,
    so it is worth re-testing against the current scanner rather than inherited.
    """
    a = "            if False and isBase and not isCupH and not isLikelyConsolidation"
    if a not in src:
        raise RuntimeError("enable_asc anchor not found")
    src = src.replace(a, "            if isBase and not isCupH and not isLikelyConsolidation", 1)
    b = "            isAscendingBase = False\n"
    return src.replace(b, "            isAscendingBase = False\n", 1)


def _apply_label_promote(src, k, targets):
    """Promote a SPECIFIC pattern seen in the last k bars over a generic current label.

    The correct label sits somewhere in [event-20, +5] for 141 of 172 events but only 126 at
    event+5. Majority voting fails because a base is generically labelled while immature, so
    the vote is dominated by the pre-formation phase. Specificity is the better tie-break: a
    Cup+Handle that was recognised days ago does not stop being one because the handle
    window has since swallowed the breakout thrust.
    """
    anchor = "        latest = history_state[-1]"
    if anchor not in src:
        raise RuntimeError("label_promote anchor not found")
    code = (f"\n        _T = {targets!r}\n"
            f"        _w = [st for st in history_state[-{k}:] if st['pOn']]\n"
            "        if latest['pOn'] and latest['pName'] not in _T:\n"
            "            for _t in _T:\n"
            "                _m = [st for st in _w if st['pName'] == _t]\n"
            "                if _m:\n"
            "                    latest['pName'] = _t\n"
            "                    latest['pCode'] = _m[-1]['pCode']\n"
            "                    break\n")
    return src.replace(anchor, anchor + code, 1)


def _apply_label_lag(src, lag):
    """Read the pattern label from `lag` bars back, where the base is complete but not yet
    distorted by the breakout thrust.

    IBD's Event Date is the day the pattern FINISHES, so the structure is fully formed a few
    bars earlier. On the breakout bar itself a trailing window spans the thrust and reads as
    a handle - 9 of 15 lost labels flip into Cup+Handle exactly there. Sampling slightly
    earlier should recover them. The pivot is left on the current bar; only the label moves.
    """
    anchor = "        latest = history_state[-1]"
    if anchor not in src:
        raise RuntimeError("label_lag anchor not found")
    code = (f"\n        if len(history_state) > {lag}:\n"
            f"            _lb = history_state[-1 - {lag}]\n"
            "            if _lb['pOn'] and _lb['pName'] != 'None' and latest['pOn']:\n"
            "                latest['pName'] = _lb['pName']\n"
            "                latest['pCode'] = _lb['pCode']\n")
    return src.replace(anchor, anchor + code, 1)


def _apply_label_stability(src, mode, k):
    """Report the label the base HELD, not the one on the final bar.

    The ground truth's Event Date is the breakout - the day the pattern finishes, not the
    day it forms. The correct label is present somewhere in [event-20, event+5] for 141 of
    172 events but only 126 at event+5, and 9 of the 15 losses are bases that flip INTO
    Cup+Handle on the breakout itself (a trailing window spanning the thrust reads as a
    handle). The instantaneous label is least reliable exactly where the evaluation samples.

    mode 'vote' : most frequent label over the last k bars the pattern was on
    mode 'first': the first label that held for k consecutive bars in this base
    """
    anchor = "        latest = history_state[-1]"
    if anchor not in src:
        raise RuntimeError("label_stability anchor not found")
    if mode == 'vote':
        code = (
            "\n        _h = [st for st in history_state[-%d:] if st['pOn'] and st['pName'] != 'None']\n" % k +
            "        if _h and latest['pOn']:\n"
            "            from collections import Counter as _C\n"
            "            _cnt = _C(st['pName'] for st in _h)\n"
            "            _mx = max(_cnt.values())\n"
            "            _ties = [nm for nm, c in _cnt.items() if c == _mx]\n"
            "            _best = latest['pName'] if latest['pName'] in _ties else _ties[0]\n"
            "            if _best != latest['pName']:\n"
            "                _s = next(st for st in reversed(_h) if st['pName'] == _best)\n"
            "                latest['pName'] = _best\n"
            "                latest['pCode'] = _s['pCode']\n")
    else:
        code = (
            "\n        _run = 0; _prev = None; _lock = None\n"
            "        for st in history_state:\n"
            "            if not st['pOn'] or st['pName'] == 'None':\n"
            "                _run = 0; _prev = None; continue\n"
            "            _run = _run + 1 if st['pName'] == _prev else 1\n"
            "            _prev = st['pName']\n"
            f"            if _lock is None and _run >= {k}:\n"
            "                _lock = (st['pName'], st['pCode'])\n"
            "        if _lock and latest['pOn'] and _lock[0] != latest['pName']:\n"
            "            latest['pName'], latest['pCode'] = _lock\n")
    return src.replace(anchor, anchor + code, 1)


def _apply_second_field(src, gap):
    """ADD conservative_pivot as an extra output. pivRef is untouched, so no metric moves."""
    anchor = "                'rs_nh': bool(latest['rsNH']),"
    if anchor not in src:
        raise RuntimeError("second_field anchor not found")
    return src.replace(anchor, anchor + "\n                'conservative_pivot': None,\n                'pivot_ambiguity_pct': None,", 1)


def _apply_pivot_conservative(src, gap):
    """Lean the pivot DOWN when the top two candidate highs disagree.

    Forward-return test over 94 events with a genuine breakout entry (5 closes below the
    level, then a cross), comparing our pivot against IBD's:
        pivot too LOW  (>3%)  n=11   +6.88pp return,  drawdown -11.4%,  1 bar earlier
        accurate              n=71   +0.30pp
        pivot too HIGH (>3%)  n=12  -12.89pp return,  drawdown -17.6%,  6 bars LATE
    The loss is strongly asymmetric - quoting above the real buy point means chasing an
    extended move, quoting below means entering slightly early at a better price. Symmetric
    price error is therefore the wrong objective.

    The IBD pivot is the highest swing high 51% of the time and the second-highest 48%, and
    nothing separates them. So when those two are more than `gap`% apart - i.e. the coin
    flip is expensive - take the LOWER one; otherwise keep the max.
    """
    anchor = "                    _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop"
    if anchor not in src:
        raise RuntimeError("pivot_conservative anchor not found")
    new = ("                    _c = sorted([pp for (bb, pp) in aHP_list\n"
           "                                 if bStart is not None and bStart <= bb < _e], reverse=True)\n"
           "                    _rawmax = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop\n"
           "                    if len(_c) > 1 and _c[0] > 0 and (_c[0] - _c[1]) / _c[0] * 100.0 > "
           f"{gap}:\n"
           "                        _bp = float(_c[1])\n"
           "                    else:\n"
           "                        _bp = _rawmax")
    return src.replace(anchor, new, 1)


def _apply_pivot_blend(src, mode):
    """Estimate the base pivot from the top swing highs instead of taking the maximum.

    Measured over 1320 candidate swing highs in 119 bases, the IBD pivot is the highest
    swing high 51% of the time and the SECOND highest 48% - a near coin-flip. Conditional on
    rank 1-3, no feature separates them: volume-above AUC 0.408, volume-at 0.477, touches
    0.427, recency 0.494, upper-wick rejection 0.479 - all chance. The volume signal that
    looked strong unconditionally (AUC 0.871) was purely a proxy for height.

    If the choice is irreducible, a blend beats a rule: it minimises expected error rather
    than being exactly right half the time and badly wrong the other half.
    """
    anchor = "                    _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop"
    if anchor not in src:
        raise RuntimeError("pivot_blend anchor not found")
    pick = {
        'max':     "float(max(_c))",
        'rank2':   "float(sorted(_c, reverse=True)[1]) if len(_c) > 1 else float(max(_c))",
        'mean2':   "float(np.mean(sorted(_c, reverse=True)[:2]))",
        'mean3':   "float(np.mean(sorted(_c, reverse=True)[:3]))",
        'median3': "float(np.median(sorted(_c, reverse=True)[:3]))",
        'wmean2':  "float(0.6 * sorted(_c, reverse=True)[0] + 0.4 * sorted(_c, reverse=True)[1]) if len(_c) > 1 else float(max(_c))",
    }[mode]
    new = ("                    _c = [pp for (bb, pp) in aHP_list\n"
           "                          if bStart is not None and bStart <= bb < _e]\n"
           "                    if _c:\n"
           f"                        _bp = {pick}\n"
           "                    else:\n"
           "                        _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop")
    return src.replace(anchor, new, 1)


def _apply_handle_candidate(src, lo, hi, pick):
    """Find the handle high as a SWING HIGH matching the ground-truth signature.

    Reverse-engineered from IBD's own Pivot Price + handle depth (n=41): the handle high
    sits at p25 0.920 / median 0.950 / p75 0.965 of the base high, and forms after the cup's
    low. The scanner instead takes the max of a trailing 15-bar window, which is a different
    object - it can land anywhere and is why gating it by that ratio fails.

    This replaces the window max with a candidate search over confirmed swing highs in the
    band, taking either the most recent or the highest.
    """
    anchor = "                H12 = np.max(highs[w12_start:end_h_idx + 1]) if end_h_idx >= w12_start else highs[i]"
    if anchor not in src:
        raise RuntimeError("handle_candidate anchor not found")
    new = ("                _bh_e = i + 1 - 8\n"
           "                _bh = float(np.max(highs[bStart:_bh_e])) if (bStart is not None and _bh_e > bStart) else bTop\n"
           "                _loOff = int(np.argmin(lows[bStart:i + 1])) if (bStart is not None and i > bStart) else 0\n"
           "                _cands = [(bb, pp) for (bb, pp) in aHP_list\n"
           "                          if bStart is not None and bStart + _loOff < bb <= end_h_idx\n"
           f"                          and _bh and {lo} <= pp / _bh <= {hi}]\n"
           "                if _cands:\n"
           + ("                    H12 = float(_cands[0][1])\n" if pick == 'recent'
              else "                    H12 = float(max(pp for _, pp in _cands))\n") +
           "                    w12_start = min(bb for bb, _ in _cands)\n"
           "                else:\n"
           "                    H12 = np.max(highs[w12_start:end_h_idx + 1]) if end_h_idx >= w12_start else highs[i]")
    return src.replace(anchor, new, 1)


def _apply_handle_vs_basehigh(src, lo, hi):
    """Gate the handle high against the CORRECTED base high, not the ratcheted bTop.

    Reverse-engineered from the ground truth's own Pivot Price + handle depth columns
    (n=41 of 46 Cup With Handle events), the handle high sits at:
        min 0.635  p25 0.920  median 0.950  p75 0.965  max 0.997   of the base high
    Only 1 of 41 handles forms AT the cup high - a handle is a swing high a few percent
    BELOW it. The scanner's only bound is `H12 < bTop * 1.02`, and because bTop runs ~5%
    under the true base high that bound has been landing near 0.967 by accident, with no
    lower bound at all.
    """
    anchor = "                if inTop and depOk_h and volOk_h and slopeOk_h and H12 < bTop * 1.02:"
    if anchor not in src:
        raise RuntimeError("handle_vs_basehigh anchor not found")
    pre = ("                _bh_e = i + 1 - 8\n"
           "                _bh = float(np.max(highs[bStart:_bh_e])) if (bStart is not None and _bh_e > bStart) else bTop\n"
           f"                hiOk = bool(_bh and {lo} <= H12 / _bh <= {hi})\n")
    new = "                if inTop and depOk_h and volOk_h and slopeOk_h and hiOk:"
    return src.replace(anchor, pre + new, 1)


def _apply_bo_pivot_fix(src, lag=8):
    """Latch the post-breakout pivot off the base high too, not the stale ratcheted bTop.

    pivRef for an in-base bar now uses the base's highest high, but once a breakout is
    registered the reported pivot switches to boPivot - which is still `active_pivot`, i.e.
    the ratcheted bTop. So post-breakout events keep quoting the old, too-low level
    (GM -27.2%, QTTB -23.7%, both too low).

    The breakout TRIGGER is deliberately left on bTop: changing it would change which
    patterns are detected. Only the recorded pivot is corrected.
    """
    anchor = ("            if isBase and active_pivot is not None and highs[i] > active_pivot:\n"
              "                isBase = False\n"
              "                boPivot = active_pivot")
    if anchor not in src:
        raise RuntimeError("bo_pivot_fix anchor not found")
    new = ("            if isBase and active_pivot is not None and highs[i] > active_pivot:\n"
           "                isBase = False\n"
           "                boPivot = active_pivot\n"
           "                if active_pivot is bTop or (bTop is not None and active_pivot == bTop):\n"
           f"                    _be = i + 1 - {lag}\n"
           "                    if bStart is not None and _be > bStart:\n"
           "                        boPivot = float(np.max(highs[bStart:_be]))")
    return src.replace(anchor, new, 1)


def _apply_handle_locator(src, min_age, lo_frac, hi_frac, pick):
    """Locate the handle high as a confirmed swing high, instead of a trailing-window max.

    The scanner takes H12 = max of a trailing ~15-bar window. Against MarketSmith
    progressions that is often NOT the handle IBD identified: every one of the 41
    ground-truth Cup+Handles has its handle high at least 7 sessions before the breakout
    (median 47), yet forcing that age onto our H12 collapses detections 19 -> 9. So our
    correct labels are frequently reached through a recent high that is not the handle -
    which also explains CLMT (36.94 vs 36.63), hDep correlating only 0.57-0.71 with IBD's
    recorded handle depth, and the true-handle signature making detection worse when gated.

    So find it structurally: a confirmed swing high (already in aHP_list, so it has bars on
    both sides), at least `min_age` sessions old, sitting in the upper part of the base -
    the ground truth puts handle high / base high at p25 0.920, median 0.950. The handle low
    is then the lowest low SINCE that high, which is what IBD's handle depth measures.
    """
    anchor = "                w12_start = max(0, end_h_idx - handle_len)"
    if anchor not in src:
        raise RuntimeError("handle_locator anchor not found")
    sel = "_hc[0]" if pick == 'recent' else "max(_hc, key=lambda t: t[1])"
    new = (
        "                _bh_e = i + 1 - 8\n"
        "                _bh = float(np.max(highs[bStart:_bh_e])) if (bStart is not None and _bh_e > bStart) else bTop\n"
        "                _loB = (bStart + int(np.argmin(lows[bStart:i + 1]))) if (bStart is not None and i > bStart) else 0\n"
        "                _hc = [(bb, pp) for (bb, pp) in aHP_list\n"
        "                       if bStart is not None and bb > _loB and (i - bb) >= %d\n"
        "                       and _bh and %s <= pp / _bh <= %s]\n" % (min_age, lo_frac, hi_frac) +
        "                if _hc:\n"
        f"                    _hb, _hp = {sel}\n"
        "                    w12_start = _hb\n"
        "                    end_h_idx = i - 1\n"
        "                else:\n"
        "                    w12_start = max(0, end_h_idx - handle_len)")
    return src.replace(anchor, new, 1)


def _apply_handle_age(src, min_age):
    """Require the handle HIGH to be at least `min_age` sessions old.

    Derived from MarketSmith progressions supplied for real tickers, where the Cup+Handle
    label appears a consistent 6-9 sessions AFTER the handle high forms - never on it:
        CLMT high 5/06 -> labelled 5/14 (6)   GIII high 7/17 -> 7/27 (7)
        BTSG high 5/16 -> 5/28 (8)            BTSG high 8/22 -> 9/05 (9)
    A high needs bars on its right to prove it is a swing point, and the handle needs to
    actually drift down from it.

    Checked against all 41 ground-truth Cup+Handle events: the handle high precedes the
    breakout by a MINIMUM of 7 sessions (median 47). Not one exception - so this gate cannot
    cost recall, only remove premature detections. GIII fired Cup+Handle on 7/06, eleven
    sessions BEFORE its handle high existed.
    """
    anchor = "                if inTop and depOk_h and volOk_h and slopeOk_h and H12 < bTop * 1.02:"
    if anchor not in src:
        raise RuntimeError("handle_age anchor not found")
    pre = ("                _hi_rel = int(np.argmax(highs[w12_start:end_h_idx + 1])) if end_h_idx >= w12_start else 0\n"
           f"                ageOk = bool((i - (w12_start + _hi_rel)) >= {min_age})\n")
    return src.replace(anchor, pre +
        "                if inTop and depOk_h and volOk_h and slopeOk_h and ageOk and H12 < bTop * 1.02:", 1)


def _apply_old_pivot(src):
    """Restore the pre-session pivot (ratcheted bTop x 0.975) for exact A/B comparison."""
    a = ("                    _e = i + 1 - 8\n"
         "                    _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop\n"
         "                    pivRef = _bp if _bp else bTop")
    if a not in src:
        raise RuntimeError("old_pivot anchor not found")
    return src.replace(a, "                    pivRef = bTop * 0.975 if bTop else bTop", 1)


def _apply_base_pivot_swing(src, adj):
    """Quote the base pivot off the highest CONFIRMED SWING HIGH in the base.

    The max-in-base fix removed the systematic bias (median error 2.50% -> 0.02%), but 34
    events remain >5% off, 19 of them too HIGH. A raw maximum can be set by a single-bar
    spike or gap, whereas IBD's pivot is a swing high - a level price actually turned at.
    aHP_list already holds confirmed pivot highs (pivLen bars either side), so restrict the
    maximum to those, falling back to the raw lagged max when the base has none yet.
    """
    anchor = "                    _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop"
    if anchor not in src:
        raise RuntimeError("base_pivot_swing anchor not found")
    new = ("                    _sw = [pp for (bb, pp) in aHP_list\n"
           "                           if bStart is not None and bStart <= bb < _e]\n"
           "                    if _sw:\n"
           "                        _bp = float(max(_sw))\n"
           "                    else:\n"
           "                        _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop")
    src = src.replace(anchor, new, 1)
    if adj != 1.0:
        src = src.replace("                    pivRef = _bp if _bp else bTop",
                          f"                    pivRef = _bp * {adj} if _bp else bTop", 1)
    return src


def _apply_base_pivot_max(src, adj, lag=0):
    """Report the base pivot as the highest high INSIDE the base, not the ratcheted bTop.

    Diagnosis at lead 20, over base-top patterns whose pivot is >5% too low (28 events):
        true pivot / max high inside our base  = 1.000  (median)
        our bTop   / max high inside our base  = 0.948
    IBD's pivot IS the base's highest high; bTop sits 5.2% under it. The ratchet only
    absorbs moves up to 5% above bTop - a larger jump fires a breakout instead, and the
    base then resurrects (see _apply_close_base_on_bo) still carrying the stale low bTop.
    So the quoted buy point is a level price has already traded through.

    Reporting-only: bTop still drives breakout detection and every pattern gate, so this
    cannot change which patterns are found.
    """
    anchor = "                else: pivRef = bTop * 0.975 if bTop else bTop"
    if anchor not in src:
        raise RuntimeError("base_pivot_max anchor not found in scanner source")
    new = ("                else:\n"
           f"                    _e = i + 1 - {lag}\n"
           "                    _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop\n"
           f"                    pivRef = _bp * {adj} if _bp else bTop")
    return src.replace(anchor, new, 1)


def _apply_handle_pivot_proximity(src, maxpct):
    """Report the handle pivot only when price is actually near it; else use the base top.

    Purely a REPORTING rule - label, gates and breakout detection are untouched.

    Rationale: the handle pivot is right at the breakout (median -1.6%) but 8.8% low 20 bars
    before it, when no handle has formed and the window grabs an earlier swing. Using bTop
    in that regime is far better (11/18 within 3% vs 3/18). Static gates cannot separate the
    two regimes, but proximity can: a handle high 25% above price is not a buy point you are
    working, it is an artefact.
    """
    anchor = "                elif isCupH and cupHandlePivot is not None: pivRef = cupHandlePivot"
    if anchor not in src:
        raise RuntimeError("handle_pivot_proximity anchor not found")
    new = ("                elif (isCupH and cupHandlePivot is not None\n"
           f"                      and abs(closes[i] - cupHandlePivot) / cupHandlePivot * 100.0 <= {maxpct}):\n"
           "                    pivRef = cupHandlePivot")
    return src.replace(anchor, new, 1)


def _apply_right_lip(src, tol):
    """A handle can only form after the cup's RIGHT LIP - the recovery to near the old high.

    The handle pivot runs a median 8.8% low 20 bars before the breakout (3/18 within 3%, vs
    11/18 if bTop were used) because no handle exists yet and the window picks up an earlier,
    lower swing. A static floor on H12/bTop fixes that but also rejects genuine deep handles
    near the breakout, so it merely moves accuracy earlier rather than adding any.

    The real distinction is structural: a handle is the pause AFTER the right side climbs
    back to the cup's high. So require a bar between the cup's low and the start of the
    handle window that reached `tol` of bTop. Before that recovery there is no lip, hence no
    handle, and the pivot correctly falls back to bTop.
    """
    anchor = "                if inTop and depOk_h and volOk_h and slopeOk_h and H12 < bTop * 1.02:"
    if anchor not in src:
        raise RuntimeError("right_lip anchor not found in scanner source")
    pre = (
        "                lipOk = False\n"
        "                if bStart is not None and w12_start > bStart:\n"
        "                    _loOff = int(np.argmin(lows[bStart:w12_start + 1]))\n"
        "                    _lipHi = np.max(highs[bStart + _loOff:w12_start + 1]) if (bStart + _loOff) <= w12_start else 0.0\n"
        f"                    lipOk = bool(_lipHi >= bTop * {tol})\n")
    new = ("                if inTop and depOk_h and volOk_h and slopeOk_h and lipOk "
           "and H12 < bTop * 1.02:")
    return src.replace(anchor, pre + new, 1)


def _apply_cup_bands(src, mid, hi):
    """Re-scale the Cup length bands.

    The 25/130/250-bar bands were fitted while bCount ran a median 1.84x the true base
    Length. Once the base actually closes on breakout, bCount lands at 1.20x, so every band
    edge (and the Consolidation/Flat cutoffs) is measuring a different thing than when it
    was tuned. This exposes the Cup edges so they can be refitted to the corrected scale.
    """
    a1 = "                    if (25 <= bCount <= 130):"
    a2 = "                    elif (130 < bCount <= 250):"
    a3 = "                    elif (bCount > 250):"
    for a in (a1, a2, a3):
        if a not in src:
            raise RuntimeError(f"cup_bands anchor not found: {a.strip()}")
    src = src.replace(a1, f"                    if (25 <= bCount <= {mid}):", 1)
    src = src.replace(a2, f"                    elif ({mid} < bCount <= {hi}):", 1)
    src = src.replace(a3, f"                    elif (bCount > {hi}):", 1)
    return src


def _apply_new_base_after_bo(src, drop_uptrend=None):
    """Let the next base start off the post-breakout high (Webster's left-side-high rule).

    "It's a 13-week high OR it has already broken out of a base... and the highest point
    after the breakout can qualify for a high as well... unless it undercuts the prior
    base's low."

    Needed as the partner to _apply_close_base_on_bo: closing the base on breakout is right
    (41 events improve their buy price by >1pp vs 23 worsening) but on its own it strands 18
    events with no base at all, because nothing restarts one. This supplies the restart.
    """
    init = "        lastBTop = None"
    if init not in src:
        raise RuntimeError("new_base_after_bo init anchor not found")
    src = src.replace(init, init + "\n        lastBLow = None", 1)

    bo = ("            if isBase and active_pivot is not None and highs[i] > active_pivot:\n"
          "                isBase = False")
    if bo not in src:
        raise RuntimeError("new_base_after_bo breakout anchor not found")
    src = src.replace(bo, bo + "\n                lastBLow = bLow", 1)

    bph = "                bPH = (piv_h > H65s) or (bTop is not None and piv_h > bTop)"
    if bph not in src:
        raise RuntimeError("new_base_after_bo bPH anchor not found")
    src = src.replace(bph, bph + (
        "\n                if not bPH and boBar is not None and piv_idx > boBar:"
        "\n                    _pbHi = np.max(highs[boBar:piv_idx + 1])"
        "\n                    _pbLo = np.min(lows[boBar:piv_idx + 1])"
        "\n                    bPH = (piv_h >= _pbHi) and (lastBLow is None or _pbLo >= lastBLow)"), 1)

    if drop_uptrend is not None:
        up = "                lUp = (L103 * 1.20 <= piv_h)"
        if up not in src:
            raise RuntimeError("new_base_after_bo lUp anchor not found")
        src = src.replace(up, f"                lUp = (L103 * {drop_uptrend} <= piv_h)", 1)
    return src


def _apply_close_base_on_bo(src, also_none=False):
    """Actually close a base when it breaks out, instead of resurrecting it next bar.

    Mechanism: the breakout sets `isBase = False`, but `inBase` (line ~658) is the COMPOSITE
    `isBase or isDB or isCup or isCupH or ...`, and those pattern flags were computed before
    the breakout check, so `inBase` is still True on the breakout bar. The next bar reads
    `prev_isBase = history_state[-1]['inBase']` and hits `elif not newBase and prev_isBase:
    isBase = True`, reviving the base with a stale bTop/bLow/bCount/bStart.

    Consequence, measured on the ground truth's repeat symbols: the scanner is near-perfect
    on the FIRST base of a sequence (92% of buy prices within 5%, median error 1.10%) and
    collapses on later ones (67%, 3.52%) while reporting a base 3.34x longer than truth. It
    merges base-on-base into one span. AUPH's 3rd base is the clearest case - true pivot
    9.19 after a -45% decline, scanner reported 16.70, exactly the 2nd base's pivot.

    Fix: carry the raw state-machine flag through the history and gate the revival on that.
    `also_none` additionally lets the no-nesting test read the raw flag.
    """
    anchor = "                'isCupH': isCupH,"
    if anchor not in src:
        raise RuntimeError("close_base_on_bo state anchor not found")
    src = src.replace(anchor, "                'isBaseRaw': isBase,\n" + anchor, 1)

    rev = "            prev_isBase = history_state[-1]['inBase'] if history_state else False"
    if rev not in src:
        raise RuntimeError("close_base_on_bo prev_isBase anchor not found")
    src = src.replace(
        rev,
        "            prev_isBase = history_state[-1]['isBaseRaw'] if history_state else False", 1)

    if also_none:
        nn = "                    noNe = not history_state[i - pivLag]['inBase']"
        if nn not in src:
            raise RuntimeError("close_base_on_bo noNe anchor not found")
        src = src.replace(nn, "                    noNe = not history_state[i - pivLag]['isBaseRaw']", 1)
    return src


def _apply_nested_bases(src):
    """Webster's "Microsoft problem": let a new base start while already inside a base.

    From the MarketSmith pattern-recognition rebuild interview: "back in the '90s there was
    a time where Microsoft had base next to base next to base, and your eyes could see it...
    but I couldn't get my code to recognize it. Where you used to see a consolidation, now
    you see two bases. You've got a cup with handle next to a flat base." Also: "that large
    cup... within there, there were two smaller cups", and the base count is measured from
    the inner base.

    The scanner forbids this outright via `noNe`, which is why bCount runs a median 2.12x
    the ground-truth Length and why long spans collapse to one 'Consolidation' label.
    """
    anchor = ("                noNe = True\n"
              "                if len(history_state) >= pivLag:\n"
              "                    noNe = not history_state[i - pivLag]['inBase']")
    if anchor not in src:
        raise RuntimeError("nested_bases anchor not found in scanner source")
    return src.replace(anchor, "                noNe = True   # nested bases allowed", 1)


def _apply_lock_handle(src):
    """Webster's phase-2 rule: lock the FIRST qualifying handle, don't keep re-picking.

    "In the past I had it pick the best handle on a base... it would change along the way...
    What we decided to do with this version is just do the very first handle. If it breaks
    out from there, we just lock it in."

    The scanner recomputes a trailing 15-bar handle window every bar, so the handle floats
    forward and, on a slow grind into the pivot, ends up spanning the cup's right side
    instead of a handle - measured handle depth 14.4% vs the ground truth's own 9.0%.
    Locking the first qualifying handle for the life of the base is both IBD-correct and
    fixes the drift at its source.
    """
    init_anchor = "        history_state = []"
    if init_anchor not in src:
        raise RuntimeError("lock_handle init anchor not found")
    src = src.replace(init_anchor, init_anchor + "\n        lockedHandlePivot = None", 1)

    # Reset on a fresh base and whenever the base is invalidated.
    new_anchor = "                bCount = pivLag\n                isBase = True"
    if new_anchor not in src:
        raise RuntimeError("lock_handle newBase anchor not found")
    src = src.replace(new_anchor, new_anchor + "\n                lockedHandlePivot = None", 1)

    inval_anchor = ("                if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB "
                    "or closes[i] > bTop * 1.40:\n                    isBase = False")
    if inval_anchor not in src:
        raise RuntimeError("lock_handle invalidation anchor not found")
    src = src.replace(inval_anchor, inval_anchor + "\n                    lockedHandlePivot = None", 1)

    # Freeze only the CHOICE of handle, not the cup qualification: the base-level gates
    # still have to hold every bar. Locking past those gates instead makes every base that
    # ever showed a handle window report Cup+Handle forever (recall 28 but 52 false
    # positives) - which is not what "lock in the first handle" means.
    set_anchor = "                        isCupH = True\n                        cupHandlePivot = H12"
    if set_anchor not in src:
        raise RuntimeError("lock_handle set anchor not found")
    return src.replace(set_anchor,
                       "                        if lockedHandlePivot is None:\n"
                       "                            lockedHandlePivot = H12\n"
                       "                if lockedHandlePivot is not None:\n"
                       "                    isCupH = True\n"
                       "                    cupHandlePivot = lockedHandlePivot", 1)


def _apply_cup_first(src, pos_lo, pos_hi, rec_min):
    """Webster: a handle only counts once the base is ALREADY recognised as a cup.

    "Qualifying handle just means the program has already recognized it as a cup, and then
    the handle is in the upper portion of the base. That was a very important thing that
    Bill wanted, and so we kept that."

    The scanner's handle block is explicitly "independent of cup detection", so locking the
    first handle (Webster's other rule) latches onto bases that were never cups - recall
    18->28 but false positives 8->52. This adds the missing precondition as actual cup
    geometry rather than another depth threshold: the base's low must sit in the MIDDLE of
    the base (not at either edge), and the right side must have recovered a real fraction of
    the decline. That is what makes a cup a cup.
    """
    anchor = "            # 6. Cup With Handle"
    if anchor not in src:
        raise RuntimeError("cup_first anchor not found in scanner source")
    block = (
        "            # Cup geometry: rounded bottom with the low in the middle and a\n"
        "            # recovered right side (Webster: handle only qualifies on a real cup).\n"
        "            cupShapeOk = False\n"
        "            if isBase and bTop and bLow and bStart is not None and bTop > bLow and i > bStart:\n"
        "                _lo_off = int(np.argmin(lows[bStart:i + 1]))\n"
        "                _pos = _lo_off / float(i - bStart)\n"
        "                _rightRec = (np.max(highs[bStart + _lo_off:i + 1]) - bLow) / (bTop - bLow)\n"
        f"                cupShapeOk = ({pos_lo} <= _pos <= {pos_hi}) and (_rightRec >= {rec_min})\n\n"
    )
    src = src.replace(anchor, block + anchor, 1)

    gate = "            elif isBase and bTop and bLow and cupMid and bCount >= 20 and cupH_allowed"
    if gate in src:      # lock_handle already rewrote the gate into an elif chain
        return src.replace(gate, gate + " and cupShapeOk", 1)
    gate = "            if isBase and bTop and bLow and cupMid and bCount >= 20 and cupH_allowed"
    if gate not in src:
        raise RuntimeError("cup_first gate anchor not found")
    return src.replace(gate, gate + " and cupShapeOk", 1)


def _apply_handle_anchor(src, maxspan, minlen):
    """Start the handle window at the cup's right lip instead of a fixed 15 bars back.

    Validated against the ground truth's own `handle depth` column: the scanner measures a
    median handle depth of 14.4% where IBD records 9.0% (corr only 0.57), i.e. it
    over-measures by ~60%. Cause: a fixed 15-bar window opens BEFORE the handle begins, so
    L12 picks up the cup's right side rather than the handle's low.

    That bias is why `hdRatio <= 0.45` had to be set so tight - and the tightness is not
    survivable, because the ground truth's own handle-depth/Depth ratio exceeds 0.45 on 14
    of 46 real events (p75 0.48, max 0.88). The gate was compensating for the measurement.

    IBD defines the handle as beginning at the cup's right lip, a local peak, which the
    scanner already tracks in aHP_list (newest-first, confirmed with a 5-bar lag).
    """
    anchor = "                w12_start = max(0, end_h_idx - handle_len)"
    if anchor not in src:
        raise RuntimeError("handle_anchor anchor not found in scanner source")
    new = (f"                _lip = next((b for (b, p) in aHP_list\n"
           f"                             if end_h_idx - {maxspan} <= b <= end_h_idx - {minlen}), None)\n"
           f"                w12_start = _lip if _lip is not None else max(0, end_h_idx - handle_len)")
    return src.replace(anchor, new, 1)


def _apply_flat_depth(src, thresh, metric):
    """Key Flat Base off whole-base depth instead of the recent-window depth.

    Ground truth (IBD/Breakaway Gap.csv, no scanner involved) separates Flat Base from
    Consolidation by base Depth with AUC 0.884 - a single cut at Depth<=15.5 is 93.3%
    accurate. Flat Base median Depth is 13 vs Consolidation 23.5. The scanner instead tests
    `rDepPct` (a 20-65 bar trailing window), which is a different quantity. Pine used whole-
    base depth at 15.0. This swaps the metric so the gate tests what the labels key off.
    """
    pat = r"isFlatBase = isBase and \(rDepPct <= [0-9.]+\)"
    new = f"isFlatBase = isBase and ({metric} is not None and {metric} <= {thresh})"
    src, n = re.subn(pat, new, src, count=1)
    if n != 1:
        raise RuntimeError(f"flat_depth swap did not match scanner source (got {n} subs)")
    return src


def _apply_class_window(src):
    """Insert the classification window and point the three classification gates at it."""
    anchor = "            # Preliminary Consolidation check (before other patterns, for guard use)"
    if anchor not in src:
        raise RuntimeError("class-window anchor not found in scanner source")
    src = src.replace(anchor, _CLASS_WINDOW_CODE + anchor, 1)

    for start, end in _CW_BLOCKS:
        si = src.index(start)
        ei = src.index(end, si + len(start))
        block = src[si:ei]
        patched = block.replace('bDepPct', 'cDepPct').replace('bCount', 'cLen')
        src = src[:si] + patched + src[ei:]
    return src


class _PandasShim:
    """Forwards everything to real pandas except read_parquet, which serves cached frames.

    `scan_single_ticker` resolves `pd` from its module globals, so swapping this in lets us
    hand it a pre-sliced DataFrame without touching the scanner source.
    """

    def __init__(self, frames):
        self._frames = frames

    def read_parquet(self, key, *a, **kw):
        return self._frames[str(key)]

    def __getattr__(self, name):
        return getattr(pd, name)


class FastEval:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self._src = SCANNER_SRC.read_text()
        self._events = []          # (key, symbol, csv_base_type)
        self._frames = {}          # key -> pre-sliced DataFrame
        self._load_events()
        self._truth_pivots = self._load_truth_pivots()
        self._truth_dates = self._load_truth_dates()

    def _load_truth_dates(self):
        """Ground-truth breakout date per event, keyed like `_events`."""
        try:
            csv = pd.read_csv(ROOT / "IBD" / "Breakaway Gap.csv")
        except Exception:
            return {}
        out = {}
        for idx, row in csv.iterrows():
            try:
                d = pd.to_datetime(_clean_date(row['Event Date']), format='mixed')
            except Exception:
                continue
            out[f"{row['Symbol']}::{idx}"] = d.strftime('%Y-%m-%d')
        return out

    def formed_window(self, overrides=None, back=20, fwd=5, label=None):
        """Credit the pattern if it is identified anywhere in [event-back, event+fwd].

        IBD's Event Date is the BREAKOUT - the day the base finishes, not the day it forms.
        A scanner that names the pattern while it is still forming is behaving correctly,
        and on a live scan that is the only time the call is actionable. Scoring solely at
        event+5 also samples at the least stable moment: 9 of 15 lost labels flip into
        Cup+Handle on the breakout bar, when the trailing window spans the thrust.
        """
        scan = self._load_scanner(overrides)
        n = ex = br = 0
        for key, sym, btype in self._events:
            n += 1
            ed = self._truth_dates.get(key)
            if not ed:
                continue
            try:
                res = scan(sym, key)
            except Exception:
                res = None
            if not res or not res.get('history'):
                continue
            h = res['history']
            ie = next((k for k, st in enumerate(h) if st['date'] == ed), None)
            if ie is None:
                ie = max((k for k, st in enumerate(h) if st['date'] <= ed), default=None)
            if ie is None:
                continue
            names = {h[k]['pName'] for k in range(max(0, ie - back), min(len(h), ie + fwd + 1))
                     if h[k]['pOn']}
            ex += bool(names & EXACT_NAME_MAP.get(btype, set()))
            br += bool(names & BROAD_NAME_MAP.get(btype, set()))
        return {'label': label or f'window -{back}..+{fwd}', 'n': n, 'exact': ex, 'broad': br}

    def lead_time(self, overrides=None, lead=0, band=15.0, label=None):
        """Score the PIVOT as it stood `lead` bars BEFORE IBD's breakout date.

        The workflow this scanner serves is: watch a base form, track distance to the pivot,
        and enter on volume dry-up / MA test - both before the breakout and after it. So the
        pivot must be right while the base is still forming, which is weeks before the event
        date every other metric here scores at.

        Reports, at that earlier bar: whether a pattern was on at all (coverage), how close
        the reported pivot was to IBD's, and whether the "within `band`%" watchlist decision
        agreed with the truth - which is the call that actually gates an entry.
        """
        scan = self._load_scanner(overrides)
        n = cov = p3 = p5 = agree = both_in = scored = 0
        for key, sym, btype in self._events:
            n += 1
            edate, tv = self._truth_dates.get(key), self._truth_pivots.get(key)
            if not edate or not tv:
                continue
            try:
                res = scan(sym, key)
            except Exception:
                res = None
            if not res or not res.get('history'):
                continue
            h = res['history']
            ie = next((k for k, st in enumerate(h) if st['date'] == edate), None)
            if ie is None:
                ie = max((k for k, st in enumerate(h) if st['date'] <= edate), default=None)
            if ie is None:
                continue
            j = ie - lead
            if j < 0:
                continue
            st = h[j]
            true_in = abs((st['close'] - tv) / tv * 100.0) <= band
            if not st.get('pOn') or st.get('distPct') is None:
                agree += (not true_in)      # no signal == "not on the watchlist"
                continue
            cov += 1
            piv = st['close'] / (1.0 + st['distPct'] / 100.0)
            err = abs(piv - tv) / tv * 100.0
            scored += 1
            p3 += err <= 3.0
            p5 += err <= 5.0
            ours_in = abs(st['distPct']) <= band
            agree += (ours_in == true_in)
            both_in += (ours_in and true_in)
        return {'label': label or f'lead-{lead}', 'n': n, 'lead': lead, 'coverage': cov,
                'scored': scored, 'piv3': p3, 'piv5': p5, 'band_agree': agree, 'both_in': both_in}

    def bo_window(self, overrides=None, tol=2, label=None):
        """Score on BREAKOUT-DATE tolerance instead of a single snapshot.

        An event counts when the scanner registered a breakout within +/-`tol` bars of IBD's
        event date carrying a correct pattern. The snapshot scoring in run() asks "what was
        the label exactly at event_date+5", which structurally penalises a scanner that finds
        the same pattern breaking out a few bars early or late - and penalises correct
        EARLIER bases hardest, which is the tradeoff Webster describes ("more entry points
        and earlier entry points").
        """
        scan = self._load_scanner(overrides)
        n = exact = broad = piv3 = piv5 = detected = 0
        for key, sym, btype in self._events:
            n += 1
            edate = self._truth_dates.get(key)
            if not edate:
                continue
            try:
                res = scan(sym, key)
            except Exception:
                res = None
            if not res or not res.get('history'):
                continue
            h = res['history']
            ie = next((k for k, st in enumerate(h) if st['date'] == edate), None)
            if ie is None:                       # event date not a trading bar in the slice
                ie = max((k for k, st in enumerate(h) if st['date'] <= edate), default=None)
            if ie is None:
                continue
            lo, hi = max(0, ie - tol), min(len(h) - 1, ie + tol)
            hit_e = hit_b = False
            best = None
            for k in range(lo, hi + 1):
                st = h[k]
                if st.get('boBar') != st.get('bar'):
                    continue                     # no breakout registered on this bar
                name = st.get('boPatternName', 'None')
                if name in EXACT_NAME_MAP.get(btype, set()):
                    hit_e = True
                if name in BROAD_NAME_MAP.get(btype, set()):
                    hit_b = True
                pv, tv = st.get('boPivot'), self._truth_pivots.get(key)
                if pv and tv:
                    e = abs(pv - tv) / tv * 100.0
                    best = e if best is None else min(best, e)
            detected += 1 if best is not None else 0
            exact += hit_e
            broad += hit_b
            if best is not None:
                piv3 += best <= 3.0
                piv5 += best <= 5.0
        return {'label': label or f'tol+/-{tol}', 'n': n, 'tol': tol, 'bo_found': detected,
                'exact': exact, 'broad': broad, 'piv3': piv3, 'piv5': piv5}
    def _load_truth_pivots(self):
        """Ground-truth buy price per event, keyed like `_events` ("SYM::csv_row_index").

        Read straight from the CSV rather than the window cache so existing caches stay
        valid. Enables scoring on the actual pivot PRICE instead of label agreement - see
        the `piv3`/`piv5` metrics in run(), and the note on why that matters.
        """
        try:
            csv = pd.read_csv(ROOT / "IBD" / "Breakaway Gap.csv")
        except Exception:
            return {}
        return {f"{row['Symbol']}::{idx}": float(row['Pivot Price'])
                for idx, row in csv.iterrows()
                if pd.notna(row.get('Pivot Price'))}

    # ---------------------------------------------------------------- loading
    def _cache_is_fresh(self):
        """True when the window cache is newer than every ticker parquet feeding it."""
        try:
            cache_mtime = CACHE_PATH.stat().st_mtime
            newest = max((p.stat().st_mtime for p in (ROOT / "ticker_cache").glob("*_1d.parquet")),
                         default=0)
            csv = ROOT / "IBD" / "Breakaway Gap.csv"
            if csv.exists():
                newest = max(newest, csv.stat().st_mtime)
            if newest > cache_mtime:
                if self.verbose:
                    print("[fast_eval] ticker data is newer than the window cache - rebuilding")
                return False
            return True
        except Exception:
            return False

    def _load_events(self):
        # Slicing 177 windows means opening 177 parquets, some with decades of history.
        # Under a process pool every worker would repeat that, so cache the sliced result.
        # The cache must not outlive the data it was sliced from. update_ticker_cache.py
        # rewrites the parquets in place, and a stale pickle silently scores every run
        # against old bars - which once produced a two-hour-old baseline that disagreed with
        # evaluate_breakaway_gap.py by a full event. Compare mtimes and rebuild if older.
        if CACHE_PATH.exists() and self._cache_is_fresh():
            try:
                with open(CACHE_PATH, 'rb') as f:
                    blob = pickle.load(f)
                self._events, self._frames = blob['events'], blob['frames']
                if self.verbose:
                    print(f"[fast_eval] loaded {len(self._events)} windows from cache")
                return
            except Exception:
                pass   # corrupt or stale pickle -> fall through and rebuild

        self._build_events_from_parquet()
        try:
            with open(CACHE_PATH, 'wb') as f:
                pickle.dump({'events': self._events, 'frames': self._frames}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass

    def _build_events_from_parquet(self):
        csv = pd.read_csv(ROOT / "IBD" / "Breakaway Gap.csv")
        csv['Parsed'] = pd.to_datetime(csv['Event Date'].apply(_clean_date), format='mixed')

        for idx, row in csv.iterrows():
            sym, tdate, btype = row['Symbol'], row['Parsed'], row['Daily Base Type']
            if btype in EXCLUDED_TRUTH_TYPES:
                continue
            fp = ROOT / "ticker_cache" / f"{sym}_1d.parquet"
            if not fp.exists():
                continue
            df = pd.read_parquet(fp)
            if df.empty or len(df) < 60:
                continue
            df = df.sort_index()
            dates = [str(d)[:10] for d in df.index]
            tstr = tdate.strftime('%Y-%m-%d')
            if tstr in dates:
                eidx = dates.index(tstr)
            else:
                dt = pd.to_datetime(dates)
                sub = dt[dt <= tdate]
                if len(sub) == 0:
                    continue
                eidx = dt.get_loc(sub[-1])

            cut = df.iloc[:min(len(df), eidx + 6)]
            # The scanner keeps only the last 1500 bars; pre-trimming here is equivalent
            # and avoids carrying decades of history for long-lived tickers.
            if len(cut) > 1500:
                cut = cut.iloc[-1500:]
            key = f"{sym}::{idx}"
            self._frames[key] = cut
            self._events.append((key, sym, btype))

        if self.verbose:
            print(f"[fast_eval] cached {len(self._events)} event windows in memory")

    # ------------------------------------------------------------ parameterise
    # Each knob maps to a regex over the scanner source plus a replacement template.
    # Anchored tightly so a miss raises rather than silently no-opping.
    KNOBS = {
        'cuph_inTop':      (r"inTop = \(L12 >= cupMid \* [0-9.]+\)",
                            "inTop = (L12 >= cupMid * {v})"),
        'cuph_hdRatio':    (r"if hdRatio <= [0-9.]+:",
                            "if hdRatio <= {v}:"),
        'cuph_bCountMin':  (r"cupMid and bCount >= \d+ and",
                            "cupMid and bCount >= {v} and"),
        'cuph_rDepGate':   (r"and rDepPct > [0-9.]+:",
                            "and rDepPct > {v}:"),
        'cuph_hDepLo':     (r"depOk_h = \([0-9.]+ <= hDep <= max_hDep\)",
                            "depOk_h = ({v} <= hDep <= max_hDep)"),
        'cuph_hDepMax':    (r"max_hDep = [0-9.]+ if bCount > \d+ else [0-9.]+",
                            "max_hDep = {v}"),
        # Accepts both the original bCount-derived expression and the fixed-width form it
        # was replaced with, so the knob keeps working after that change was applied.
        'cuph_handleLen':  (r"handle_len = (?:min\(\d+, max\(\d+, bCount // \d+\)\)|\d+)",
                            "handle_len = {v}"),
        # Metric name is captured because the class-window rewrite renames bDepPct -> cDepPct
        # inside this block; hard-coding 'bDepPct' made the two changes uncomposable.
        'cup_depLo_short': (r"depOk = \(([bc]DepPct) is not None and [0-9.]+ <= \1 <= 50\.0\)\n(\s+)if depOk and not isLikelyConsolidation",
                            r"depOk = (\1 is not None and {v} <= \1 <= 50.0)\n\2if depOk and not isLikelyConsolidation"),
        'flat_rDep':       (r"isFlatBase = isBase and \(rDepPct <= [0-9.]+\)",
                            "isFlatBase = isBase and (rDepPct <= {v})"),
        'flat_rDep25':     (r"isFlatBase = \(rDep25 <= [0-9.]+\)",
                            "isFlatBase = (rDep25 <= {v})"),
        'db_cA_lo':        (r"cA = \(sL <= fL \* [0-9.]+\) and \(sL >= fL \* ([0-9.]+)\)",
                            "cA = (sL <= fL * 1.04) and (sL >= fL * {v})"),
        'db_cE_lo':        (r"cE = \(sH <= fH \* [0-9.]+\) and \(sH >= fH \* ([0-9.]+)\)",
                            "cE = (sH <= fH * 1.08) and (sH >= fH * {v})"),
        # --- additional Cup+Handle knobs (previously hard-coded) ---
        'cuph_bDepLo':     (r"cupH_allowed and \(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+)\)",
                            r"cupH_allowed and (bDepPct is not None and {v} <= bDepPct <= \1)"),
        'cuph_bDepHi':     (r"cupH_allowed and \(bDepPct is not None and ([0-9.]+) <= bDepPct <= [0-9.]+\)",
                            r"cupH_allowed and (bDepPct is not None and \1 <= bDepPct <= {v})"),
        'cuph_flatGuard':  (r"cupH_allowed = \(not prevIsFlatBase or not isFlatBase\) or \(bDepPct is not None and bDepPct >= [0-9.]+\)",
                            "cupH_allowed = (not prevIsFlatBase or not isFlatBase) or (bDepPct is not None and bDepPct >= {v})"),
        'cuph_h12Cap':     (r"and H12 < bTop \* [0-9.]+:",
                            "and H12 < bTop * {v}:"),
        # Lower bound on the handle high vs the cup high. A real handle is the pause AFTER
        # the right side recovers to near the old high; 20 bars before the breakout the
        # window instead picks up an earlier, much lower high, dragging the buy point 8.8%
        # low (only 3/18 within 3%, vs 11/18 if bTop were used).
        'cuph_h12Floor':   (r"and H12 < bTop \* ([0-9.]+):",
                            r"and bTop * {v} <= H12 < bTop * \1:"),
        # --- additional Double Bottom knobs (previously hard-coded) ---
        'db_bDepLo':       (r"isLikelyConsolidation and \(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+)\) and len\(aHP_list\)",
                            r"isLikelyConsolidation and (bDepPct is not None and {v} <= bDepPct <= \1) and len(aHP_list)"),
        'db_bDepHi':       (r"isLikelyConsolidation and \(bDepPct is not None and ([0-9.]+) <= bDepPct <= [0-9.]+\) and len\(aHP_list\)",
                            r"isLikelyConsolidation and (bDepPct is not None and \1 <= bDepPct <= {v}) and len(aHP_list)"),
        'db_maxBars':      (r"dbMaxBars = \d+",
                            "dbMaxBars = {v}"),
        'db_cA_hi':        (r"cA = \(sL <= fL \* [0-9.]+\) and \(sL >= fL \* ([0-9.]+)\)",
                            r"cA = (sL <= fL * {v}) and (sL >= fL * \1)"),
        'db_cE_hi':        (r"cE = \(sH <= fH \* [0-9.]+\) and \(sH >= fH \* ([0-9.]+)\)",
                            r"cE = (sH <= fH * {v}) and (sH >= fH * \1)"),
        'db_cC':           (r"cC = \(sL <= peak \* [0-9.]+\)",
                            "cC = (sL <= peak * {v})"),
        'db_cD':           (r"cD = \(sH >= fL \+ \(fH - fL\) \* [0-9.]+\) and \(sH >= sL \+ \(peak - sL\) \* [0-9.]+\)",
                            "cD = (sH >= fL + (fH - fL) * {v}) and (sH >= sL + (peak - sL) * {v})"),
        'db_cPT':          (r"cPT = \(fH >= prL250 \* [0-9.]+\) or \(sH >= prL250 \* [0-9.]+\)",
                            "cPT = (fH >= prL250 * {v}) or (sH >= prL250 * {v})"),
        'db_cSh':          (r"cSh = \(highest_since_2nd_low <= sH \* [0-9.]+\)",
                            "cSh = (highest_since_2nd_low <= sH * {v})"),
        'db_cTC':          (r"cTC = \(sLt - fH_t >= \d+\)",
                            "cTC = (sLt - fH_t >= {v})"),
        'db_undercut':     (r"second_leg_undercut = sL < fL",
                            "second_leg_undercut = {v}"),
        # --- Cup Without Handle / Consolidation / Flat Base separation knobs ---
        # (Cup vs Consolidation is the dominant EXACT-match error: 23 events.)
        'cup_uShapeThr':   (r"has_u_shape = \(second_half_high >= bTop \* [0-9.]+\)",
                            "has_u_shape = (second_half_high >= bTop * {v})"),
        'cup_uShapeMin':   (r"if isBase and bTop and bLow and bCount > \d+:\n(\s+)mid_bar",
                            r"if isBase and bTop and bLow and bCount > {v}:\n\1mid_bar"),
        'cup_uGateBars':   (r"cup_ok = has_u_shape if bCount > \d+ else True",
                            "cup_ok = has_u_shape if bCount > {v} else True"),
        'cup_shortLo':     (r"if \(\d+ <= bCount <= \d+\):\n(\s+)depOk = \(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+)\)",
                            r"if (25 <= bCount <= 130):\n\1depOk = (bDepPct is not None and {v} <= bDepPct <= \2)"),
        'cup_shortHi':     (r"if \(\d+ <= bCount <= \d+\):\n(\s+)depOk = \(bDepPct is not None and ([0-9.]+) <= bDepPct <= [0-9.]+\)",
                            r"if (25 <= bCount <= 130):\n\1depOk = (bDepPct is not None and \2 <= bDepPct <= {v})"),
        'cup_midLo':       (r"elif \(\d+ < bCount <= \d+\):\n(\s+)depOk = \(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+)\)",
                            r"elif (130 < bCount <= 250):\n\1depOk = (bDepPct is not None and {v} <= bDepPct <= \2)"),
        'cup_midHi':       (r"elif \(\d+ < bCount <= \d+\):\n(\s+)depOk = \(bDepPct is not None and ([0-9.]+) <= bDepPct <= [0-9.]+\)",
                            r"elif (130 < bCount <= 250):\n\1depOk = (bDepPct is not None and \2 <= bDepPct <= {v})"),
        'cup_longLo':      (r"depOk = \(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+) and not \(bDepPct",
                            r"depOk = (bDepPct is not None and {v} <= bDepPct <= \1 and not (bDepPct"),
        'likelyConsol':    (r"isLikelyConsolidation = isBase and bCount > \d+",
                            "isLikelyConsolidation = isBase and bCount > {v}"),
        'consol_depLo':    (r"\(bDepPct is not None and [0-9.]+ <= bDepPct <= ([0-9.]+) and not isCup and not isCupH and not isFlatBase",
                            r"(bDepPct is not None and {v} <= bDepPct <= \1 and not isCup and not isCupH and not isFlatBase"),
        'consol_depHi':    (r"\(bDepPct is not None and ([0-9.]+) <= bDepPct <= [0-9.]+ and not isCup and not isCupH and not isFlatBase",
                            r"(bDepPct is not None and \1 <= bDepPct <= {v} and not isCup and not isCupH and not isFlatBase"),
        'consol_longBars': (r"\(bCount > \d+ and not isCup and not isCupH\)",
                            "(bCount > {v} and not isCup and not isCupH)"),
        'flat_bCountHi':   (r"isFlatBase = isBase and \(rDepPct <= [0-9.]+\) and \(\d+ <= bCount <= \d+\)",
                            r"isFlatBase = isBase and (rDepPct <= 20.0) and (20 <= bCount <= {v})"),
        'flat_rDep25Hi':   (r"isFlatBase = \(rDep25 <= [0-9.]+\) and \(\d+ <= bCount <= \d+\)",
                            r"isFlatBase = (rDep25 <= 15.0) and (20 <= bCount <= {v})"),
        'flat_6wkLo':      (r"is6WkFlat = isFlatBase and \(\d+ <= recent_win <= (\d+)\)",
                            r"is6WkFlat = isFlatBase and ({v} <= recent_win <= \1)"),
        'flat_6wkHi':      (r"is6WkFlat = isFlatBase and \((\d+) <= recent_win <= \d+\)",
                            r"is6WkFlat = isFlatBase and (\1 <= recent_win <= {v})"),
        # Handle must drift down, not still be advancing (IBD). Inline in the scanner.
        'cuph_slopeMax':   (r"slopeOk_h = \(_hsl <= [0-9.]+\)", "slopeOk_h = (_hsl <= {v})"),
        # The handle-window trim is now inline in the scanner; tune it as a normal knob.
        'cuph_trimMax':    (r"_trim < \d+ and", "_trim < {v} and"),
        'cuph_trimLook':   (r"highs\[max\(0, end_h_idx - \d+\):end_h_idx\]",
                            "highs[max(0, end_h_idx - {v}):end_h_idx]"),
        'cuph_volRatio':   (r"volOk_h = \(ref_vol is None or ref_vol <= 0\) or \(handle_avg_vol < ref_vol \* [0-9.]+\)",
                            "volOk_h = (ref_vol is None or ref_vol <= 0) or (handle_avg_vol < ref_vol * {v})"),
        'db_volRatio':     (r"cVol = volumes\[fLt\] >= volumes\[sLt\] \* [0-9.]+",
                            "cVol = volumes[fLt] >= volumes[sLt] * {v}"),
        # --- base state-machine structure (not reachable by any earlier search) ---
        # The ratchet drags bTop up with price, so a slow grind into the pivot never fires a
        # breakout and the handle window ends up spanning the cup's right side. Setting this
        # to 1.0 disables the ratchet (the condition can never hold).
        'base_ratchet':    (r"if bTop is not None and highs\[i\] > bTop and highs\[i\] <= bTop \* [0-9.]+:",
                            "if bTop is not None and highs[i] > bTop and highs[i] <= bTop * {v}:"),
        'base_invalClose': (r"closes\[i\] > bTop \* [0-9.]+:",
                            "closes[i] > bTop * {v}:"),
        # Empirical correction for the ratchet's systematic overshoot. If the ratchet is
        # disabled this should return to 1.0.
        'pivref_adj':      (r"pivRef = _bp(?: \* [0-9.]+)? if _bp else bTop",
                            "pivRef = _bp * {v} if _bp else bTop"),
        'pivref_lag':      (r"_e = i \+ 1 - \d+", "_e = i + 1 - {v}"),
        'pivLen':          (r"pivLen = \d+", "pivLen = {v}"),
        'pivLag':          (r"pivLag = \d+", "pivLag = {v}"),
        'bdF':             (r"bdF = [0-9.]+", "bdF = {v}"),
        'bLenB':           (r"bLenB = \d+", "bLenB = {v}"),
        'L103_bars':       (r"w103_start = max\(0, i - \d+ \+ 1\)", "w103_start = max(0, i - {v} + 1)"),
        'H65_bars':        (r"w65_start = max\(0, shift_idx - \d+ \+ 1\)", "w65_start = max(0, shift_idx - {v} + 1)"),
        'newbase_npiv':    (r"if len\(aHP_list\) >= \d+ and i >= pivLag", "if len(aHP_list) >= {v} and i >= pivLag"),
        'cupMid_frac':     (r"cupMid = bLow \+ \(bTop - bLow\) \* [0-9.]+", "cupMid = bLow + (bTop - bLow) * {v}"),
        'postbo_win':      (r"barsSBO is not None and barsSBO <= \d+", "barsSBO is not None and barsSBO <= {v}"),
        'uptrend_bars':    (r"w103_start = max\(0, i - \d+ \+ 1\)",
                            "w103_start = max(0, i - {v} + 1)"),
        'uptrend_ratio':   (r"lUp = \(L103 \* [0-9.]+ <= piv_h\)",
                            "lUp = (L103 * {v} <= piv_h)"),
    }

    def _build_source(self, overrides):
        overrides = dict(overrides or {})
        use_win = overrides.pop('use_class_window', False)
        flat_depth = overrides.pop('flat_depth', None)
        anch_span = overrides.pop('handle_anchor_span', None)
        anch_min = overrides.pop('handle_anchor_min', 5)
        uphalf = overrides.pop('handle_uphalf', None)
        uphalf_floor = overrides.pop('handle_uphalf_floor', None)
        bo_orig = overrides.pop('bo_on_orig', False)
        indep = overrides.pop('independent', False)
        multi = overrides.pop('multilabel', False)
        dbclose = overrides.pop('db_close_match', None)
        dbprio = overrides.pop('db_priority', False)
        atight = overrides.pop('asc_tight', None)
        easc = overrides.pop('enable_asc', False)
        lprom = overrides.pop('label_promote', None)
        llag = overrides.pop('label_lag', None)
        lstab = overrides.pop('label_stability', None)
        secfld = overrides.pop('second_field', None)
        cons = overrides.pop('pivot_conservative', None)
        blend = overrides.pop('pivot_blend', None)
        hcand = overrides.pop('handle_candidate', None)
        hvb = overrides.pop('handle_vs_basehigh', None)
        bofix = overrides.pop('bo_pivot_fix', None)
        hloc = overrides.pop('handle_locator', None)
        hage = overrides.pop('handle_age', None)
        oldpiv = overrides.pop('old_pivot', False)
        bpswing = overrides.pop('base_pivot_swing', None)
        bpmax = overrides.pop('base_pivot_max', None)
        bplag = overrides.pop('base_pivot_lag', 0)
        hprox = overrides.pop('handle_prox', None)
        lip = overrides.pop('right_lip', None)
        rcap = overrides.pop('ratchet_cap', None)
        sbo_win = overrides.pop('sbo_window', None)
        cup_mid = overrides.pop('cup_bandMid', None)
        cup_hi = overrides.pop('cup_bandHi', None)
        nb_after = overrides.pop('new_base_after_bo', False)
        nb_uptrend = overrides.pop('nb_uptrend', None)
        close_bo = overrides.pop('close_base_on_bo', False)
        close_bo_none = overrides.pop('close_base_on_bo_none', False)
        nested = overrides.pop('nested_bases', False)
        if close_bo or close_bo_none:
            src_close = True
        else:
            src_close = False
        lock_h = overrides.pop('lock_handle', False)
        cup_first = overrides.pop('cup_first', False)
        cf_pos_lo = overrides.pop('cf_pos_lo', 0.25)
        cf_pos_hi = overrides.pop('cf_pos_hi', 0.75)
        cf_rec = overrides.pop('cf_rec', 0.60)
        src = self._src
        if indep:
            src = _apply_independent(src)
        if multi:
            src = _apply_multilabel(src)
        if dbclose is not None:
            src = _apply_db_close_match(src, *dbclose)
        if dbprio:
            src = _apply_db_priority(src)
        if atight is not None:
            src = _apply_asc_tight(src, *atight)
        if easc:
            src = _apply_enable_asc(src)
        if lprom is not None:
            src = _apply_label_promote(src, lprom[0], lprom[1])
        if llag is not None:
            src = _apply_label_lag(src, llag)
        if lstab is not None:
            src = _apply_label_stability(src, lstab[0], lstab[1])
        if secfld is not None:
            src = _apply_second_field(src, secfld)
        if cons is not None:
            src = _apply_pivot_conservative(src, cons)
        if blend is not None:
            src = _apply_pivot_blend(src, blend)
        if hcand is not None:
            src = _apply_handle_candidate(src, hcand[0], hcand[1], hcand[2])
        if hvb is not None:
            src = _apply_handle_vs_basehigh(src, hvb[0], hvb[1])
        if bofix is not None:
            src = _apply_bo_pivot_fix(src, bofix)
        if hloc is not None:
            src = _apply_handle_locator(src, *hloc)
        if hage is not None:
            src = _apply_handle_age(src, hage)
        if oldpiv:
            src = _apply_old_pivot(src)
        if bpswing is not None:
            src = _apply_base_pivot_swing(src, bpswing)
        if bpmax is not None:
            src = _apply_base_pivot_max(src, bpmax, bplag)
        if hprox is not None:
            src = _apply_handle_pivot_proximity(src, hprox)
        if lip is not None:
            src = _apply_right_lip(src, lip)
        if rcap is not None:
            src = _apply_ratchet_cap(src, rcap)
        if bo_orig:
            src = _apply_bo_on_orig(src)
        if uphalf is not None:
            src = _apply_handle_uphalf(src, uphalf, uphalf_floor)
        if src_close:
            src = _apply_close_base_on_bo(src, also_none=close_bo_none)
        if nb_after or nb_uptrend is not None:
            src = _apply_new_base_after_bo(src, nb_uptrend)
        if cup_mid is not None or cup_hi is not None:
            src = _apply_cup_bands(src, cup_mid or 130, cup_hi or 250)
        if sbo_win is not None:
            src = src.replace('barsSBO is not None and barsSBO <= 15', f'barsSBO is not None and barsSBO <= {sbo_win}')
        if nested:
            src = _apply_nested_bases(src)
        if lock_h:
            src = _apply_lock_handle(src)
        if cup_first:
            src = _apply_cup_first(src, cf_pos_lo, cf_pos_hi, cf_rec)
        if use_win:
            src = _apply_class_window(src)
        if anch_span:
            src = _apply_handle_anchor(src, anch_span, anch_min)
        if flat_depth is not None:
            # After the window rewrite the Flat Base block already reads cDepPct, so point
            # the swapped gate at whichever depth metric is actually in scope.
            src = _apply_flat_depth(src, flat_depth, 'cDepPct' if use_win else 'bDepPct')
        for name, val in overrides.items():
            if name not in self.KNOBS:
                raise KeyError(f"unknown knob {name!r}; valid: {sorted(self.KNOBS)}")
            pat, tmpl = self.KNOBS[name]
            new = tmpl.format(v=val)
            src, n = re.subn(pat, new, src, count=1)
            if n != 1:
                raise RuntimeError(f"knob {name!r} did not match scanner source (got {n} subs)")
        return src

    def _load_scanner(self, overrides):
        src = self._build_source(overrides)
        ns = {'__name__': '_fe_scanner', '__file__': str(SCANNER_SRC)}
        exec(compile(src, str(SCANNER_SRC), 'exec'), ns)
        ns['pd'] = _PandasShim(self._frames)   # swap AFTER exec so imports resolved normally
        return ns['scan_single_ticker']

    # -------------------------------------------------------------------- run
    def run(self, overrides=None, label=None):
        scan = self._load_scanner(overrides)
        recs = []
        for key, sym, btype in self._events:
            try:
                res = scan(sym, key)
            except Exception:
                res = None
            det = res.get('pattern_name', 'None') if res else 'None'
            tb, pb = bucket_truth(btype), bucket_det(det)
            # Actual buy price the scanner would report, reconstructed the same way
            # evaluate_breakaway_gap.py does it: close / (1 + distPct/100).
            piv_err = None
            if res:
                c, dp = res.get('close'), res.get('dist_pct')
                truth = self._truth_pivots.get(key)
                if c and dp is not None and truth and (1.0 + dp / 100.0) != 0:
                    piv_err = abs(c / (1.0 + dp / 100.0) - truth) / truth * 100.0
            recs.append({
                'symbol': sym,
                'csv_type': btype,
                'detected': det,
                'piv_err': piv_err,
                'exact': det in EXACT_NAME_MAP.get(btype, set()),
                'broad': det in BROAD_NAME_MAP.get(btype, set()),
                'truth_bucket': tb,
                'det_bucket': pb,
                'pivot_ok': tb == pb,
            })
        df = pd.DataFrame(recs)
        cuph = df[df['truth_bucket'] == 'Cup+Handle']
        dbl = df[df['truth_bucket'] == 'Dbl Bottom']
        cuph_pred = df[df['det_bucket'] == 'Cup+Handle']

        # Macro-F1 over the three pivot buckets. Plain pivot accuracy is dominated by
        # StdPivot (124 of 177 events), so it is maximised by never predicting Cup+Handle or
        # Double Bottom - the search found exactly that degenerate solution. Macro-F1 gives a
        # class that is never predicted an F1 of 0, so suppressing the focus patterns is
        # penalised rather than rewarded.
        f1s = {}
        for b in ('StdPivot', 'Cup+Handle', 'Dbl Bottom'):
            tp = int(((df['truth_bucket'] == b) & (df['det_bucket'] == b)).sum())
            fp = int(((df['truth_bucket'] != b) & (df['det_bucket'] == b)).sum())
            fn = int(((df['truth_bucket'] == b) & (df['det_bucket'] != b)).sum())
            f1s[b] = (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else 0.0
        macro_f1 = sum(f1s.values()) / 3.0
        focus_f1 = (f1s['Cup+Handle'] + f1s['Dbl Bottom']) / 2.0

        # Buy-point accuracy on the actual price. The label-bucket `pivot` score is only a
        # proxy for this and a weak one: 34 of 125 bucket-"safe" events miss the real pivot
        # by >5%, while 27 of 39 bucket-"unsafe" events land within 5%. Search objectives
        # should prefer piv3/piv5 - they score the number the trader actually uses.
        pe = df['piv_err'].dropna()
        return {
            'label': label or (str(overrides) if overrides else 'baseline'),
            'n': len(df),
            'exact': int(df['exact'].sum()),
            'broad': int(df['broad'].sum()),
            'pivot': int(df['pivot_ok'].sum()),
            'piv2': int((pe <= 2.0).sum()),
            'piv3': int((pe <= 3.0).sum()),
            'piv5': int((pe <= 5.0).sum()),
            'piv_err_med': float(pe.median()) if len(pe) else float('nan'),
            'piv_scored': int(len(pe)),
            # scaled to an int so search ranking/printing stays integer-based
            'macro_f1_x1000': int(round(macro_f1 * 1000)),
            'focus_f1_x1000': int(round(focus_f1 * 1000)),
            'f1_std': f1s['StdPivot'], 'f1_cuph': f1s['Cup+Handle'], 'f1_db': f1s['Dbl Bottom'],
            'cuph_recall': float(cuph['pivot_ok'].mean()) if len(cuph) else 0.0,
            'cuph_prec': float((cuph_pred['truth_bucket'] == 'Cup+Handle').mean()) if len(cuph_pred) else 0.0,
            'db_recall': float(dbl['pivot_ok'].mean()) if len(dbl) else 0.0,
            'df': df,
        }

    # ------------------------------------------------------------- reporting
    @staticmethod
    def pivot_significance_delta(n=N_EVENTS, p0=PIVOT_BASELINE_EXACT / N_EVENTS):
        se = math.sqrt(p0 * (1 - p0) / n)
        return math.ceil(1.645 * se * n)

    @staticmethod
    def significance_delta(n=N_EVENTS, p0=BASELINE_EXACT / N_EVENTS, alpha=0.05):
        """Extra correct events needed for a one-sided binomial to clear alpha."""
        se = math.sqrt(p0 * (1 - p0) / n)
        return math.ceil(1.645 * se * n)   # z(0.95)

    @staticmethod
    def summarize(res, baseline=None):
        n, ex = res['n'], res['exact']
        line = f"{res['label']:<52} exact {ex:>3}/{n} ({ex/n*100:>5.1f}%)  broad {res['broad']:>3} ({res['broad']/n*100:.1f}%)"
        if baseline is not None:
            d = ex - baseline['exact']
            bar = FastEval.significance_delta()
            flag = "SIGNIFICANT" if d >= bar else ("noise" if d > 0 else "")
            line += f"   delta {d:>+4}  {flag}"
        print(line)

    @staticmethod
    def paired_diff(res, baseline):
        """Which specific events flipped. With n=177 one event is 0.56%."""
        a = baseline['df'].set_index(['symbol', 'csv_type'])['exact']
        b = res['df'].set_index(['symbol', 'csv_type'])['exact']
        j = pd.DataFrame({'base': a, 'new': b}).dropna()
        gained = j[(~j['base'].astype(bool)) & (j['new'].astype(bool))]
        lost = j[(j['base'].astype(bool)) & (~j['new'].astype(bool))]
        return gained, lost

    @staticmethod
    def confusion(res):
        return pd.crosstab(res['df']['csv_type'], res['df']['detected'])


def self_test():
    fe = FastEval()
    base = fe.run(label='baseline (no overrides)')
    FastEval.summarize(base)
    assert base['n'] == N_EVENTS, f"expected {N_EVENTS} events, got {base['n']}"
    assert base['exact'] == BASELINE_EXACT, (
        f"HARNESS MISMATCH: expected {BASELINE_EXACT} exact, got {base['exact']}. "
        "The in-memory path is not equivalent to evaluate_breakaway_gap.py."
    )
    print(f"[fast_eval] self-test PASSED - reproduces committed baseline {BASELINE_EXACT}/{N_EVENTS}")
    print(f"[fast_eval] significance bar: need >= +{FastEval.significance_delta()} events for p<0.05")
    return fe, base


if __name__ == '__main__':
    import time
    t0 = time.time()
    fe, base = self_test()
    print(f"[fast_eval] one full evaluation took {time.time() - t0:.1f}s (incl. one-time data load)")
    t1 = time.time()
    fe.run(label='timing probe')
    print(f"[fast_eval] subsequent evaluation: {time.time() - t1:.1f}s")
