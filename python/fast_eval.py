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
PIVOT_BASELINE_EXACT = 127
BROAD_BASELINE = 127


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
    def _load_events(self):
        # Slicing 177 windows means opening 177 parquets, some with decades of history.
        # Under a process pool every worker would repeat that, so cache the sliced result.
        if CACHE_PATH.exists():
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
        bofix = overrides.pop('bo_pivot_fix', None)
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
        if bofix is not None:
            src = _apply_bo_pivot_fix(src, bofix)
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
