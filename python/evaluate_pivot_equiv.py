"""
Pivot-equivalence evaluation of the committed scanner.

Rationale: for trading, the label only matters insofar as it sets the BUY POINT.
Flat Base, Consolidation, Cup Without Handle and Ascending Base all pivot off the base
top, so confusing them with each other costs nothing - same entry price. Two patterns
carry a genuinely different pivot and must be identified correctly:

    Double Bottom   -> pivot is the middle peak of the W, not the base top
    Cup With Handle -> pivot is the handle high, not the base top

So this scores a 3-class problem: {StdPivot, Dbl Bottom, Cup+Handle}. Mixing a StdPivot
pattern up with DB or Cup+H (either direction) IS an error, because the entry moves.

Runs against the committed scanner unchanged ("the original best model").
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from fast_eval import FastEval, N_EVENTS   # noqa: E402

# Ground-truth classes that share the base-top pivot.
STD_TRUTH_WITH_ASC = {'Flat Base', 'Consolidation', 'Cup Without Handle', 'Ascending Base'}
STD_TRUTH_NO_ASC = {'Flat Base', 'Consolidation', 'Cup Without Handle'}

# Detected labels that resolve to the base-top pivot.
STD_DET_WITH_ASC = {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup', 'Base', 'Ascending Base'}
STD_DET_NO_ASC = {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup', 'Base'}

FOCUS = {'Double Bottom': 'Dbl Bottom', 'Cup With Handle': 'Cup+Handle'}


def bucket_truth(t, std_truth):
    if t in FOCUS:
        return FOCUS[t]
    return 'StdPivot' if t in std_truth else t


def bucket_det(d, std_det):
    if d in ('Dbl Bottom', 'Cup+Handle'):
        return d
    return 'StdPivot' if d in std_det else d


def score(df, std_truth, std_det, title):
    d = df.copy()
    d['t'] = d['csv_type'].apply(lambda x: bucket_truth(x, std_truth))
    d['p'] = d['detected'].apply(lambda x: bucket_det(x, std_det))
    d['ok'] = d['t'] == d['p']

    n = len(d)
    print(f"\n{'='*74}\n{title}\n{'='*74}")
    print(f"OVERALL (pivot-equivalent): {d['ok'].sum()}/{n} = {d['ok'].mean()*100:.1f}%")

    print(f"\n{'bucket':<14} {'n':>4} {'correct':>8} {'recall':>8} {'precision':>10}")
    print('-' * 48)
    for b in ['StdPivot', 'Cup+Handle', 'Dbl Bottom']:
        sub = d[d['t'] == b]
        pred = d[d['p'] == b]
        rec = sub['ok'].mean() * 100 if len(sub) else float('nan')
        prec = (pred['t'] == b).mean() * 100 if len(pred) else float('nan')
        print(f"{b:<14} {len(sub):>4} {int(sub['ok'].sum()):>8} {rec:>7.1f}% {prec:>9.1f}%")

    print(f"\nconfusion (rows = truth bucket, cols = detected bucket):")
    print(pd.crosstab(d['t'], d['p']).to_string())
    return d


def main():
    fe = FastEval(verbose=False)
    res = fe.run(label='committed baseline')
    df = res['df']
    print(f"scanner: committed baseline, strict-label score {res['exact']}/{N_EVENTS} "
          f"({res['exact']/N_EVENTS*100:.1f}%)")

    a = score(df, STD_TRUTH_WITH_ASC, STD_DET_WITH_ASC,
              "A. Ascending Base INSIDE the shared-pivot group")
    score(df, STD_TRUTH_NO_ASC, STD_DET_NO_ASC,
          "B. Ascending Base scored separately")

    # ---- empirically check the premise: does confusing StdPivot patterns keep the pivot? ----
    full = pd.read_csv(ROOT / 'python' / 'breakaway_gap_accuracy_results.csv')
    full['t'] = full['csv_base_type'].apply(lambda x: bucket_truth(x, STD_TRUTH_WITH_ASC))
    full['p'] = full['detected_pattern'].apply(lambda x: bucket_det(x, STD_DET_WITH_ASC))
    e = full.dropna(subset=['pivot_err_pct'])

    within = e[(e['t'] == 'StdPivot') & (e['p'] == 'StdPivot') &
               (e['csv_base_type'] != e['detected_pattern'])]
    across = e[(e['t'] == 'StdPivot') & (e['p'] != 'StdPivot')]
    exact_lbl = e[e['csv_base_type'] == e['detected_pattern']]

    print(f"\n{'='*74}\nPREMISE CHECK - is the pivot really preserved within the group?\n{'='*74}")
    print(f"{'case':<44} {'n':>4} {'median pivot err':>18}")
    print('-' * 68)
    print(f"{'label matched exactly':<44} {len(exact_lbl):>4} {exact_lbl['pivot_err_pct'].median():>17.2f}%")
    print(f"{'confused WITHIN shared-pivot group':<44} {len(within):>4} {within['pivot_err_pct'].median():>17.2f}%")
    print(f"{'StdPivot truth -> DB / Cup+H (pivot moves)':<44} {len(across):>4} {across['pivot_err_pct'].median():>17.2f}%")
    print("\nIf the middle row is close to the top row, the shared-pivot grouping is sound:")
    print("swapping labels inside the group does not move the entry price.")


if __name__ == '__main__':
    main()
