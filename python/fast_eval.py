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
BASELINE_EXACT = 78
N_EVENTS = 177

# --- pivot-equivalence scoring -------------------------------------------------------
# The label only matters insofar as it sets the buy point. Flat Base, Consolidation, Cup
# Without Handle and Ascending Base all pivot off the base top, so confusing them costs
# nothing. Double Bottom (middle peak) and Cup With Handle (handle high) pivot elsewhere,
# so mixing those up - in either direction - moves the entry price and IS an error.
STD_TRUTH = {'Flat Base', 'Consolidation', 'Cup Without Handle', 'Ascending Base'}
STD_DET = {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup', 'Base', 'Ascending Base'}
FOCUS_MAP = {'Double Bottom': 'Dbl Bottom', 'Cup With Handle': 'Cup+Handle'}
PIVOT_BASELINE_EXACT = 109


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
    'Ascending Base': {'Ascending Base'},
}
BROAD_NAME_MAP = {
    'Cup Without Handle': {'Cup', 'Base', 'Consolidation'},
    'Cup With Handle': {'Cup+Handle', 'Cup'},
    'Flat Base': {'Flat Base', '6-Wk Flat', 'Consolidation'},
    'Consolidation': {'Consolidation', 'Base', 'Flat Base', 'Cup'},
    'Double Bottom': {'Dbl Bottom', 'Base'},
    'Ascending Base': {'Ascending Base'},
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
        'cuph_handleLen':  (r"handle_len = min\(\d+, max\(\d+, bCount // \d+\)\)",
                            "handle_len = {v}"),
        # Metric name is captured because the class-window rewrite renames bDepPct -> cDepPct
        # inside this block; hard-coding 'bDepPct' made the two changes uncomposable.
        'cup_depLo_short': (r"depOk = \(([bc]DepPct) is not None and [0-9.]+ <= \1 <= 50\.0\)\n(\s+)if depOk and not isLikelyConsolidation",
                            r"depOk = (\1 is not None and {v} <= \1 <= 50.0)\n\2if depOk and not isLikelyConsolidation"),
        'flat_rDep':       (r"isFlatBase = isBase and \(rDepPct <= [0-9.]+\)",
                            "isFlatBase = isBase and (rDepPct <= {v})"),
        'flat_rDep25':     (r"isFlatBase = \(rDep25 <= [0-9.]+\)",
                            "isFlatBase = (rDep25 <= {v})"),
        'db_cA_lo':        (r"cA = \(sL <= fL \* [0-9.]+\) and \(sL >= fL \* [0-9.]+\)",
                            "cA = (sL <= fL * 1.04) and (sL >= fL * {v})"),
        'db_cE_lo':        (r"cE = \(sH <= fH \* [0-9.]+\) and \(sH >= fH \* [0-9.]+\)",
                            "cE = (sH <= fH * 1.08) and (sH >= fH * {v})"),
    }

    def _build_source(self, overrides):
        overrides = dict(overrides or {})
        use_win = overrides.pop('use_class_window', False)
        flat_depth = overrides.pop('flat_depth', None)
        src = self._src
        if use_win:
            src = _apply_class_window(src)
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
            recs.append({
                'symbol': sym,
                'csv_type': btype,
                'detected': det,
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

        return {
            'label': label or (str(overrides) if overrides else 'baseline'),
            'n': len(df),
            'exact': int(df['exact'].sum()),
            'broad': int(df['broad'].sum()),
            'pivot': int(df['pivot_ok'].sum()),
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
