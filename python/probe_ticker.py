"""
Ad-hoc probe: run the research scanner on ONE ticker as of ONE date, and dump both the
final reading and the internal base state, so a MarketSmith ground-truth progression can be
compared bar by bar against what the scanner actually saw.

The fast_eval harness only ever asks about the 172 benchmark events. Ground truth supplied
by hand ("DVA became a Double Bottom on 2/2/2024, middle peak 1/12") lives outside that set,
and diagnosing it needs the state machine's view, not just the label.

Usage:
    python3 python/probe_ticker.py DVA 2024-02-02
    python3 python/probe_ticker.py DVA 2024-02-02 --trace 2023-12-01 2024-02-05
    python3 python/probe_ticker.py DVA 2024-02-02 --bars 2023-12-20 2024-01-30
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fast_eval import FastEval  # noqa: E402


def load(sym, asof, pad=1):
    """Frame trimmed to `asof`, plus `pad` bars.

    pad=1 (ending ON `asof`) is what a live scan sees and is the ONLY honest setting for
    checking a hand-supplied progression. fast_eval builds its benchmark windows with pad=6
    so breakout-window metrics have bars to measure into; borrowing that here leaks the
    future - locate_handle counts `min_age` back from the last bar, so five extra bars slide
    the handle search forward and it picks a different swing high. That artifact made LASR
    @2024-08-30 read 13.16 (9.5% off) when the real as-of answer is 12.02, exact.
    """
    fp = ROOT / "ticker_cache" / f"{sym}_1d.parquet"
    if not fp.exists():
        raise SystemExit(f"no parquet for {sym}")
    df = pd.read_parquet(fp).sort_index()
    dates = [str(d)[:10] for d in df.index]
    if asof in dates:
        e = dates.index(asof)
    else:
        dt = pd.to_datetime(dates)
        sub = dt[dt <= pd.to_datetime(asof)]
        if len(sub) == 0:
            raise SystemExit(f"{sym} has no bars on/before {asof}")
        e = dt.get_loc(sub[-1])
    cut = df.iloc[:min(len(df), e + pad)]
    return cut.iloc[-1500:] if len(cut) > 1500 else cut


def run(sym, frame, overrides=None):
    fe = FastEval(verbose=False)
    key = f"{sym}::probe"
    fe._frames[key] = frame
    scan = fe._load_scanner(overrides or {})
    return scan(sym, key)


def show(res, asof):
    if not res:
        print("  scanner returned None")
        return
    st = (res.get('history') or [None])[-1]
    print(f"  primary : {res.get('pattern_name')!r:22} pivot {res.get('pivot')}")
    if st:
        print(f"  state   : pName={st.get('pName')!r} bCount={st.get('bCount')} "
              f"bTop={st.get('bTop')} bLow={st.get('bLow')}")
    pats = res.get('patterns') or []
    if pats:
        print(f"  readings ({len(pats)}):")
        for p in pats:
            print(f"    - {p.get('name'):<12} pivot {p.get('pivot'):<10} "
                  f"dist {p.get('dist_pct')}%  bars_ago {p.get('bars_ago')}")
    else:
        print("  readings: NONE")


def trace(sym, lo, hi, overrides=None):
    """Re-run the scanner as of every session in [lo, hi] - what a live watcher would see."""
    fp = ROOT / "ticker_cache" / f"{sym}_1d.parquet"
    df = pd.read_parquet(fp).sort_index()
    dates = [str(d)[:10] for d in df.index]
    sel = [d for d in dates if lo <= d <= hi]
    print(f"\n{'date':<12}{'label':<14}{'pivot':>9}{'bCount':>8}{'bTop':>9}{'bLow':>9}   readings")
    prev = None
    for d in sel:
        try:
            res = run(sym, load(sym, d, pad=1), overrides)
        except Exception as exc:
            print(f"{d:<12}ERR {exc}")
            continue
        st = (res.get('history') or [None])[-1] if res else None
        name = (res.get('pattern_name') if res else None) or '-'
        piv = res.get('pivot') if res else None
        bc = st.get('bCount') if st else None
        bt = st.get('bTop') if st else None
        bl = st.get('bLow') if st else None
        reads = ' '.join(f"{p['name']}@{p['pivot']}" for p in (res.get('patterns') or [])) if res else ''
        star = ' <<<' if name != prev else ''
        print(f"{d:<12}{name:<14}{str(piv):>9}{str(bc):>8}{str(bt):>9}{str(bl):>9}   {reads}{star}")
        prev = name


def bars(sym, lo, hi):
    fp = ROOT / "ticker_cache" / f"{sym}_1d.parquet"
    df = pd.read_parquet(fp).sort_index()
    d = df[(df.index >= lo) & (df.index <= hi)]
    v20 = df['Volume'].rolling(20).mean()
    print(f"\n{'date':<12}{'open':>9}{'high':>9}{'low':>9}{'close':>9}{'vol':>12}{'v/v20':>7}")
    for i, r in d.iterrows():
        k = str(i)[:10]
        rv = r['Volume'] / v20.loc[i] if pd.notna(v20.loc[i]) and v20.loc[i] else float('nan')
        print(f"{k:<12}{r['Open']:>9.2f}{r['High']:>9.2f}{r['Low']:>9.2f}{r['Close']:>9.2f}"
              f"{r['Volume']:>12,.0f}{rv:>7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol')
    ap.add_argument('asof')
    ap.add_argument('--trace', nargs=2, metavar=('LO', 'HI'))
    ap.add_argument('--bars', nargs=2, metavar=('LO', 'HI'))
    a = ap.parse_args()
    sym = a.symbol.upper()
    print(f"\n=== {sym} as of {a.asof} ===")
    show(run(sym, load(sym, a.asof)), a.asof)
    if a.bars:
        bars(sym, a.bars[0], a.bars[1])
    if a.trace:
        trace(sym, a.trace[0], a.trace[1])


if __name__ == '__main__':
    main()
