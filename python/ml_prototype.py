"""
Prototype: can a learned classifier (trained directly on numeric base features) beat the
72.7% broad-match ceiling that ~900 hand-tuned threshold configs plateaued at?

Rationale: every threshold-based lever (depth caps, ratios, priority order, uptrend window)
has been tried and exhausted - see broad_search_results / session notes. A rule-of-the-form
"X <= threshold" can't capture interactions between features; a classifier trained on the
same numeric quantities directly might. This is a pre-integration prototype: it extracts
features independently of the scanner's branching logic, cross-validates honestly (5-fold,
stratified, n=172 is small so overfitting risk is real), and only recommends integration if
the out-of-fold broad-match score clearly beats 125/172.

Not wired into the scanner. Output-only research script.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from fast_eval import FastEval, _clean_date, EXACT_NAME_MAP, BROAD_NAME_MAP  # noqa: E402

fe = FastEval(verbose=False)


def extract_features(sym, key, btype):
    df = fe._frames[key]
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    volumes = df['Volume'].values
    n = len(closes)
    if n < 30:
        return None

    scan = fe._load_scanner(None)
    res = scan(sym, key)
    if not res:
        return None
    last = res['history'][-1]
    bCount = last['bCount']
    bDepPct = last['bDepPct']
    if bCount is None or bDepPct is None:
        return None

    bar_i = last['bar']
    base_start = max(0, bar_i - bCount)
    window_h = highs[base_start:bar_i + 1]
    window_l = lows[base_start:bar_i + 1]
    window_v = volumes[base_start:bar_i + 1]
    if len(window_l) < 5:
        return None

    low_pos = np.argmin(window_l) / len(window_l)

    half = len(window_h) // 2
    first_top, first_low = np.max(window_h[:half]) if half else window_h[0], np.min(window_l[:half]) if half else window_l[0]
    second_top, second_low = np.max(window_h[half:]), np.min(window_l[half:])
    first_dep = (first_top - first_low) / first_top * 100.0 if first_top > 0 else 0.0
    second_dep = (second_top - second_low) / second_top * 100.0 if second_top > 0 else 0.0

    win12 = min(12, len(window_h))
    win25 = min(25, len(window_h))
    dep12 = (np.max(window_h[-win12:]) - np.min(window_l[-win12:])) / np.max(window_h[-win12:]) * 100.0
    dep25 = (np.max(window_h[-win25:]) - np.min(window_l[-win25:])) / np.max(window_h[-win25:]) * 100.0

    recent20_vol = np.mean(window_v[-20:]) if len(window_v) >= 20 else np.mean(window_v)
    prior_vol = np.mean(window_v[:-20]) if len(window_v) > 20 else recent20_vol
    vol_dryup_ratio = recent20_vol / prior_vol if prior_vol > 0 else 1.0

    bTop = last['bTop']
    near_top_count = np.sum(closes[base_start:bar_i + 1] >= bTop * 0.95) / len(window_h) if bTop else 0.0

    recent_win = min(len(window_h), max(20, min(bCount, 65)))
    rTop = np.max(window_h[-recent_win:])
    rLow = np.min(window_l[-recent_win:])
    rDepPct = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 0.0

    return {
        'symbol': sym, 'csv_type': btype,
        'bDepPct': bDepPct, 'bCount': bCount, 'rDepPct': rDepPct,
        'low_pos': low_pos, 'first_dep': first_dep, 'second_dep': second_dep,
        'dep12': dep12, 'dep25': dep25, 'vol_dryup_ratio': vol_dryup_ratio,
        'near_top_frac': near_top_count,
    }


rows = []
for key, sym, btype in fe._events:
    feat = extract_features(sym, key, btype)
    if feat:
        rows.append(feat)

data = pd.DataFrame(rows)
print(f"n={len(data)} events with extracted features (of {len(fe._events)} total)")
print(data['csv_type'].value_counts())

feature_cols = ['bDepPct', 'bCount', 'rDepPct', 'low_pos', 'first_dep', 'second_dep',
                'dep12', 'dep25', 'vol_dryup_ratio', 'near_top_frac']
X = data[feature_cols].values
y = data['csv_type'].values

CANONICAL_OUT = {
    'Consolidation': 'Consolidation', 'Cup With Handle': 'Cup+Handle',
    'Cup Without Handle': 'Cup', 'Double Bottom': 'Dbl Bottom', 'Flat Base': 'Flat Base',
}

skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

for name, clf in [
    ('RandomForest', RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=4, random_state=42, class_weight='balanced')),
    ('LogisticRegression', LogisticRegression(max_iter=2000, class_weight='balanced')),
]:
    preds = cross_val_predict(clf, X, y, cv=skf)
    det = pd.Series(preds).map(CANONICAL_OUT).values
    truth = data['csv_type'].values
    exact = sum(1 for t, d in zip(truth, det) if d in EXACT_NAME_MAP.get(t, set()))
    broad = sum(1 for t, d in zip(truth, det) if d in BROAD_NAME_MAP.get(t, set()))
    n = len(data)
    print(f"\n{name}: cross-validated (5-fold, out-of-fold predictions)")
    print(f"  exact={exact}/{n} ({exact/n*100:.1f}%)  broad={broad}/{n} ({broad/n*100:.1f}%)")
    print(f"  confusion:\n{pd.crosstab(pd.Series(truth, name='truth'), pd.Series(preds, name='pred'))}")
