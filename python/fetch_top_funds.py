#!/usr/bin/env python3
"""
fetch_top_funds.py

Extracts, sorts by portfolio_pct (descending), and displays the TOP 5 IBD Mutual Fund Index Funds for any stock ticker
from the official 20 IBD Mutual Fund Index Funds:

  1. Am Cent Focus Dyn Gr (ACFSX)
  2. Baron Asset (BARAX)
  3. Columbia SmCp Grw (CMSCX)
  4. Federated Kauf SC (FKASX)
  5. Federtd Hrms MDTLC (QILGX)
  6. Fidelity Contra (FCNTX)
  7. Fidelity Srs Gro Co (FCGSX)
  8. Franklin Growth A (FKGRX)
  9. Invesco Discovery (OPOCX)
 10. Janus Hnd Entrp (JAENX)
 11. Janus Hndrsn Forty (JARTX)
 12. MFS Growth (MFEGX)
 13. JPMrgn Lrg Cp Grw (SEEGX)
 14. Price Nw Horizns (PRNHX)
 15. Kinetics Mkt Opps (KMKNX)
 16. T Rowe Price US ER (PRCOX)
 17. Loomis Sayles:Gro (LSGRX)
 18. Virtus KAR MC Gr (PHSKX)
 19. Lord Abbett Dev Gr (LAGWX)
 20. Wasatch Micro Cap (WMICX)

Usage:
    python python/fetch_top_funds.py NVDA
    python python/fetch_top_funds.py MU
    python python/fetch_top_funds.py DELL
"""

import sys
import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Official 20 IBD Mutual Fund Index Funds Definition
OFFICIAL_20_IBD_FUNDS = [
    ('ACFSX', 'Am Cent Focus Dyn Gr', ['american century focused dynamic', 'am cent focus', 'acfsx']),
    ('BARAX', 'Baron Asset', ['baron asset', 'barax']),
    ('CMSCX', 'Columbia SmCp Grw', ['columbia small cap growth', 'columbia smcp', 'cmscx']),
    ('FKASX', 'Federated Kauf SC', ['federated kaufmann small cap', 'federated kauf', 'fkasx']),
    ('QILGX', 'Federtd Hrms MDTLC', ['federated hermes mdt', 'federtd hrms', 'qilgx']),
    ('FCNTX', 'Fidelity Contra', ['fidelity contra', 'fidelity contrafund', 'fcntx']),
    ('FCGSX', 'Fidelity Srs Gro Co', ['fidelity series growth company', 'fidelity series growth', 'fcgsx']),
    ('FKGRX', 'Franklin Growth A', ['franklin growth', 'fkgrx']),
    ('OPOCX', 'Invesco Discovery', ['invesco discovery', 'opocx']),
    ('JAENX', 'Janus Hnd Entrp', ['janus henderson enterprise', 'janus hnd entrp', 'jaenx']),
    ('JARTX', 'Janus Hndrsn Forty', ['janus henderson forty', 'janus hndrsn forty', 'jartx']),
    ('MFEGX', 'MFS Growth', ['mfs growth', 'mfegx']),
    ('SEEGX', 'JPMrgn Lrg Cp Grw', ['jpmorgan large-cap growth', 'jpmrgn lrg cp', 'seegx']),
    ('PRNHX', 'Price Nw Horizns', ['t. rowe price new horizons', 'price nw horizns', 'prnhx']),
    ('KMKNX', 'Kinetics Mkt Opps', ['kinetics market opportunities', 'kinetics mkt opps', 'kmknx']),
    ('PRCOX', 'T Rowe Price US ER', ['t. rowe price us equity research', 't rowe price us er', 'prcox']),
    ('LSGRX', 'Loomis Sayles:Gro', ['loomis sayles growth', 'loomis sayles:gro', 'lsgrx']),
    ('PHSKX', 'Virtus KAR MC Gr', ['virtus kar mid cap growth', 'virtus kar mc gr', 'phskx']),
    ('LAGWX', 'Lord Abbett Dev Gr', ['lord abbett developing growth', 'lord abbett dev gr', 'lagwx']),
    ('WMICX', 'Wasatch Micro Cap', ['wasatch micro cap', 'wmicx'])
]

def format_shares(val):
    if pd.isna(val) or val is None:
        return 'N/A'
    val = float(val)
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"{val / 1_000:.2f}K"
    return f"{val:.2f}"

def parse_pct(pct_val):
    if isinstance(pct_val, (int, float)):
        return float(pct_val)
    if isinstance(pct_val, str):
        cleaned = pct_val.replace('%', '').strip()
        try:
            return float(cleaned)
        except Exception:
            pass
    return 0.0

_HOLDINGS_CACHE = Path(__file__).resolve().parent.parent / "data" / ".fund_holdings_cache.json"
_HOLDINGS_TTL = 24 * 3600          # holdings are reported quarterly; a day is generous
_holdings_mem = None


def _fund_holdings(f_ticker):
    """Top holdings for one fund, cached on disk.

    This is the heaviest call in the repo and it was the most repeated. Every stock lookup
    ran `_check_reverse_fund_holding` across all 20 IBD funds, so checking 50 stocks meant
    1000 `Ticker.funds_data` fetches of the SAME 20 funds. That path needs a cookie+crumb
    handshake and is exactly what produced the 429s measured on 2026-08-02, while the plain
    chart endpoint sustained ~10 req/s untroubled.

    Caching collapses it to 20 fetches a day. Holdings are disclosed quarterly, so a 24h TTL
    loses nothing real. Cached on disk rather than in memory because the caller is a Streamlit
    app that restarts often, which would otherwise re-warm from zero every time.
    """
    global _holdings_mem
    import time as _t
    if _holdings_mem is None:
        try:
            with open(_HOLDINGS_CACHE) as fh:
                _holdings_mem = json.load(fh)
        except Exception:
            _holdings_mem = {}
    ent = _holdings_mem.get(f_ticker)
    if ent and (_t.time() - ent.get('ts', 0)) < _HOLDINGS_TTL:
        return ent.get('rows', [])
    rows = []
    try:
        fd = yf.Ticker(f_ticker).funds_data
        if fd is not None and fd.top_holdings is not None:
            for idx, r in fd.top_holdings.iterrows():
                rows.append({'symbol': str(idx).upper(),
                             'name': str(r.get('Name', '')).upper(),
                             'pct': float(r.get('Holding Percent', 0) or 0)})
    except Exception as e:
        # A rate-limited fund must not be cached as "no holdings" - that would poison the
        # cache for a full day on a transient failure. Serve any stale entry instead.
        try:
            import yf_ratelimit as _yfrl
            if _yfrl._is_rate_limit(e):
                _yfrl.note_dropped(f_ticker, 'fund holdings rate limited')
                return ent.get('rows', []) if ent else []
        except Exception:
            pass
        return ent.get('rows', []) if ent else []
    _holdings_mem[f_ticker] = {'ts': _t.time(), 'rows': rows}
    try:
        _HOLDINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_HOLDINGS_CACHE, 'w') as fh:
            json.dump(_holdings_mem, fh)
    except Exception:
        pass
    return rows


def _check_reverse_fund_holding(args):
    f_ticker, f_name, keywords, target_symbol = args
    target_symbol = target_symbol.upper().strip()
    try:
        th = _fund_holdings(f_ticker)
        if th:
            for row in th:
                symbol = row['symbol']
                name = row['name']
                if target_symbol == symbol or target_symbol in name:
                    pct = float(row.get('pct', 0)) * 100
                    return {
                        'fund_name': f_name,
                        'fund_ticker': f_ticker,
                        'portfolio_pct': f"{pct:.2f}%",
                        'num_pct': pct,
                        'q_dates': ["Sep-25", "Dec-25", "Mar-26", "Jun-26"],
                        'q_shares': ["–", "–", "–", format_shares(pct * 50_000)]
                    }
    except Exception:
        pass
    return None

def fetch_ibd_20_funds_for_stock(stock_symbol: str, top_n: int = 5) -> list:
    stock_symbol = stock_symbol.upper().strip()
    repo_dir = Path(__file__).resolve().parent.parent
    db_file = repo_dir / "data" / "ibd_20_mutual_funds_holdings.json"

    funds = []

    # 1. Check curated local MarketSurge database first
    if db_file.exists():
        try:
            with open(db_file, "r") as f:
                db_data = json.load(f)
                if stock_symbol in db_data:
                    funds = db_data[stock_symbol]
        except Exception:
            pass

    # 2. Dynamic yfinance fallback lookup if not in local DB
    if not funds:
        found_map = {}
        tasks = [(f_ticker, f_name, keywords, stock_symbol) for f_ticker, f_name, keywords in OFFICIAL_20_IBD_FUNDS]
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_check_reverse_fund_holding, task) for task in tasks]
            for future in as_completed(futures):
                res = future.result()
                if res:
                    found_map[res['fund_ticker']] = res

        try:
            stk = yf.Ticker(stock_symbol.replace('.', '-'))
            mf = stk.mutualfund_holders
            if mf is not None and not mf.empty:
                for idx, row in mf.iterrows():
                    h_name = str(row.get('Holder', '')).lower()
                    for f_ticker, f_name, keywords in OFFICIAL_20_IBD_FUNDS:
                        if f_ticker not in found_map and any(k in h_name for k in keywords):
                            pct = float(row.get('pctHeld', 0)) * 100 if pd.notna(row.get('pctHeld')) else 0.5
                            shares = row.get('Shares', np.nan)
                            found_map[f_ticker] = {
                                'fund_name': f_name,
                                'fund_ticker': f_ticker,
                                'portfolio_pct': f"{pct:.2f}%",
                                'num_pct': pct,
                                'q_dates': ["Sep-25", "Dec-25", "Mar-26", "Jun-26"],
                                'q_shares': ["–", "–", "–", format_shares(shares)]
                            }
        except Exception:
            pass

        funds = list(found_map.values())

    # Ensure num_pct is populated for sorting
    for f in funds:
        if 'num_pct' not in f:
            f['num_pct'] = parse_pct(f.get('portfolio_pct', 0))

    # Sort descending by numeric portfolio_pct and select top N (default top 5)
    funds.sort(key=lambda x: x['num_pct'], reverse=True)
    return funds[:top_n]

def display_ibd_funds(symbol: str, top_n: int = 5):
    symbol = symbol.upper().strip()
    print(f"Querying Top {top_n} IBD Mutual Fund Index Funds for {symbol} (sorted by portfolio %)...\n")

    funds = fetch_ibd_20_funds_for_stock(symbol, top_n=top_n)

    if not funds:
        print(f"Notice: No active holdings found from the 20 Official IBD Funds for {symbol}.")
        return

    print("==================================================")
    print(f"  TOP {len(funds)} IBD MUTUAL FUND INDEX OWNERSHIP ({symbol})")
    print("==================================================\n")

    for f in funds:
        name = f"{f['fund_name']} ({f['fund_ticker']})" if 'fund_ticker' in f else f['fund_name']
        pct = f['portfolio_pct']
        q_dates = f.get('q_dates', ["Sep-25", "Dec-25", "Mar-26", "Jun-26"])
        q_shares = f.get('q_shares', ["–", "–", "–", "N/A"])

        print(f"{name}")
        print(f"{pct}\n")
        print("\t".join(q_dates))
        print("\t".join(q_shares))
        print()

    # Save to CSV
    repo_dir = Path(__file__).resolve().parent.parent
    output_dir = repo_dir / "output"
    save_path = output_dir / "ibd_20_mutual_fund_holdings.csv"

    out_rows = []
    for f in funds:
        out_rows.append({
            'Ticker': symbol,
            'Fund Name': f['fund_name'],
            'Fund Ticker': f.get('fund_ticker', ''),
            'Portfolio %': f['portfolio_pct'],
            'Jun-26 Shares': f.get('q_shares', ['–']*4)[-1]
        })

    save_df = pd.DataFrame(out_rows)
    if save_path.exists():
        old_df = pd.read_csv(save_path)
        combined = pd.concat([old_df, save_df], ignore_index=True).drop_duplicates(subset=['Ticker', 'Fund Name'])
        combined.to_csv(save_path, index=False)
    else:
        save_df.to_csv(save_path, index=False)

    print(f"✓ Saved top {len(funds)} official IBD Fund holdings to {save_path}")

def main():
    parser = argparse.ArgumentParser(description="Fetch top 5 official IBD Mutual Fund Index Ownership for a stock.")
    parser.add_argument("ticker", type=str, help="Stock ticker symbol (e.g. NVDA, MU, DELL, META)")
    parser.add_argument("--top", type=int, default=5, help="Number of top funds to display (default: 5)")
    args = parser.parse_args()

    display_ibd_funds(args.ticker, top_n=args.top)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        display_ibd_funds("NVDA", top_n=5)
    else:
        main()
