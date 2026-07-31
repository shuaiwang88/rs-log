"""
evaluate_breakaway_gap.py

Evaluates pattern-detection accuracy against the ground truth patterns and breakout event
dates in IBD/Breakaway Gap.csv. Works against either scanner file:

    python3 python/evaluate_breakaway_gap.py            # copy.py (research/default)
    python3 python/evaluate_breakaway_gap.py --target copy
    python3 python/evaluate_breakaway_gap.py --target prod   # ibd_pattern_scanner.py
"""

import argparse
import os
import sys
import glob
import re
import json
from pathlib import Path
import pandas as pd
import numpy as np

# Set project root
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR / "python"))

_ap = argparse.ArgumentParser(add_help=True)
_ap.add_argument('--target', choices=['copy', 'prod'], default='copy',
                  help="'copy' = ibd_pattern_scanner copy.py (research file, default); "
                       "'prod' = ibd_pattern_scanner.py (production file)")
_args, _ = _ap.parse_known_args()
_SCANNER_FILENAME = 'ibd_pattern_scanner copy.py' if _args.target == 'copy' else 'ibd_pattern_scanner.py'

import importlib.util
spec = importlib.util.spec_from_file_location('ibd_pattern_scanner_eval', str(ROOT_DIR / 'python' / _SCANNER_FILENAME))
ibd_pattern_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ibd_pattern_scanner)

def clean_date(d_str):
    d_str = str(d_str).strip()
    m = re.match(r'^(\d{2})/(\d{2})(\d{4})$', d_str)
    if m:
        return f'{m.group(1)}/{m.group(2)}/{m.group(3)}'
    return d_str

# Base type mapping — exact match requires specific pattern name
EXACT_NAME_MAP = {
    'Cup Without Handle': {'Cup'},
    'Cup With Handle': {'Cup+Handle'},
    'Flat Base': {'Flat Base', '6-Wk Flat'},
    'Consolidation': {'Consolidation'},
    'Double Bottom': {'Dbl Bottom'}
}

# Pivot groups: patterns that share the same pivot type
#   bTop-based  = Cup, Flat Base, 6-Wk Flat, Consolidation  (all use bTop as pivot)
#   lower-based = Cup+Handle (handle high), Dbl Bottom (middle pivot)
PIVOT_BTOP_NAMES = {'Cup', 'Flat Base', '6-Wk Flat', 'Consolidation', 'Base'}
PIVOT_LOWER_NAMES = {'Cup+Handle', 'Dbl Bottom'}

# Broad match: bTop-based patterns are interchangeable (same pivot).
# Cup+Handle and Double Bottom must match EXACTLY - wrong pivot otherwise.
BROAD_NAME_MAP = {
    'Cup Without Handle': {'Cup', 'Base', 'Consolidation', 'Flat Base', '6-Wk Flat'},
    'Cup With Handle': {'Cup+Handle'},
    'Flat Base': {'Flat Base', '6-Wk Flat', 'Consolidation', 'Cup'},
    'Consolidation': {'Consolidation', 'Base', 'Flat Base', 'Cup', '6-Wk Flat'},
    'Double Bottom': {'Dbl Bottom'}
}

def evaluate_all_events():
    csv_path = ROOT_DIR / "IBD" / "Breakaway Gap.csv"
    csv_df = pd.read_csv(csv_path)
    csv_df['Clean_Date'] = csv_df['Event Date'].apply(clean_date)
    csv_df['Parsed_Date'] = pd.to_datetime(csv_df['Clean_Date'], format='mixed')
    
    # Exclude rare patterns (Ascending Base) from evaluation
    EXCLUDED_PATTERNS = {'Ascending Base'}
    original_count = len(csv_df)
    csv_df = csv_df[~csv_df['Daily Base Type'].isin(EXCLUDED_PATTERNS)]
    excluded_count = original_count - len(csv_df)
    
    print(f"=======================================================")
    print(f" RUNNING IBD PATTERN SCANNER ACCURACY EVALUATION")
    print(f" Scanner: {_SCANNER_FILENAME} (--target {_args.target})")
    print(f" Ground Truth: {csv_path.name} ({original_count} total events)")
    if excluded_count > 0:
        print(f" Excluded {excluded_count} rare pattern events: {', '.join(sorted(EXCLUDED_PATTERNS))}")
    print(f" Evaluating {len(csv_df)} events")
    print(f"=======================================================\n")
    
    results = []
    
    for idx, row in csv_df.iterrows():
        sym = row['Symbol']
        target_date = row['Parsed_Date']
        target_btype = row['Daily Base Type']
        target_pivot = float(row['Pivot Price'])
        target_length = int(row['Length'])
        target_depth_str = str(row['Depth'])
        target_hdepth = float(row['handle depth']) if pd.notna(row['handle depth']) else None
        
        file_path = ROOT_DIR / "ticker_cache" / f"{sym}_1d.parquet"
        if not file_path.exists():
            continue
            
        df = pd.read_parquet(file_path)
        if df.empty or len(df) < 60:
            continue
            
        df = df.sort_index()
        dates = [str(d)[:10] for d in df.index]
        target_str = target_date.strftime('%Y-%m-%d')
        
        # Find exact bar index or nearest bar on/before target date
        event_bar_idx = None
        if target_str in dates:
            event_bar_idx = dates.index(target_str)
        else:
            dt_series = pd.to_datetime(dates)
            sub = dt_series[dt_series <= target_date]
            if len(sub) > 0:
                event_bar_idx = dt_series.get_loc(sub[-1])
                
        if event_bar_idx is None:
            continue
            
        # Cut dataframe at Event Date + 5 bars (simulating running scanner on breakout day)
        cut_df = df.iloc[:min(len(df), event_bar_idx + 6)]
        
        # Create a temporary parquet file to feed to scan_single_ticker
        tmp_parquet = ROOT_DIR / "python" / f"_eval_tmp_{sym}_{idx}.parquet"
        cut_df.to_parquet(tmp_parquet)
        
        scan_res = ibd_pattern_scanner.scan_single_ticker(sym, str(tmp_parquet))
        
        if tmp_parquet.exists():
            os.remove(tmp_parquet)
            
        det_name = 'None'
        det_status = 'None'
        det_close = None
        det_pivot = None
        det_length = None
        det_score = 0
        
        if scan_res:
            det_close = scan_res.get('close')
            det_score = scan_res.get('composite_score', 0)
            det_length = scan_res.get('days_in_base')
            
            # Use pattern from the last bar of the cut (scanner's latest output)
            det_name = scan_res.get('pattern_name', 'None')
            det_status = scan_res.get('status', 'None')
            dist_pct = scan_res.get('dist_pct')
            if det_close and dist_pct is not None and (1.0 + dist_pct/100.0) != 0:
                det_pivot = det_close / (1.0 + dist_pct / 100.0)
                
        is_detected = (det_name != 'None')
        exact_match = (det_name in EXACT_NAME_MAP.get(target_btype, set()))
        broad_match = (det_name in BROAD_NAME_MAP.get(target_btype, set()))
        
        # Pivot-safe match: detected pattern shares the same pivot type as ground truth.
        # bTop-based (Cup/Flat/Consolidation) detected as another bTop-based → OK (same pivot).
        # Cup+Handle detected as Cup+Handle → OK. Dbl Bottom detected as Dbl Bottom → OK.
        target_is_btop = target_btype != 'Cup With Handle' and target_btype != 'Double Bottom'
        det_is_btop = det_name in PIVOT_BTOP_NAMES
        
        if target_is_btop:
            pivot_safe = det_is_btop  # any bTop-based detection is safe
        else:
            pivot_safe = exact_match  # Cup+Handle/DB must match exactly
        
        # Pivot-critical: these patterns have lower pivots — exact match matters
        pivot_critical_target = target_btype in ('Cup With Handle', 'Double Bottom')
        
        piv_err_pct = abs(det_pivot - target_pivot) / target_pivot * 100.0 if (det_pivot and target_pivot > 0) else None
        len_diff = abs(det_length - target_length) if (det_length and target_length > 0) else None
        
        results.append({
            'row_id': idx + 1,
            'symbol': sym,
            'event_date': target_str,
            'csv_base_type': target_btype,
            'csv_pivot': target_pivot,
            'csv_length': target_length,
            'detected_pattern': det_name,
            'detected_status': det_status,
            'detected_pivot': round(det_pivot, 2) if det_pivot else None,
            'detected_length': det_length,
            'composite_score': det_score,
            'is_detected': is_detected,
            'exact_match': exact_match,
            'broad_match': broad_match,
            'pivot_safe': pivot_safe,
            'pivot_critical': pivot_critical_target,
            'pivot_err_pct': round(piv_err_pct, 2) if piv_err_pct is not None else None,
            'len_diff_bars': len_diff
        })
        
    res_df = pd.DataFrame(results)
    
    total = len(res_df)
    pattern_detected = res_df['is_detected'].sum()
    exact_matches = res_df['exact_match'].sum()
    broad_matches = res_df['broad_match'].sum()
    pivot_safe_matches = res_df['pivot_safe'].sum()
    
    # Pivot-critical metrics: Cup+Handle + Double Bottom exact match
    pivot_critical = res_df[res_df['pivot_critical'] == True]
    pc_total = len(pivot_critical)
    pc_exact = pivot_critical['exact_match'].sum()
    
    piv_errs = res_df['pivot_err_pct'].dropna()
    mean_piv_err = piv_errs.mean() if len(piv_errs) > 0 else 0
    median_piv_err = piv_errs.median() if len(piv_errs) > 0 else 0
    
    len_diffs = res_df['len_diff_bars'].dropna()
    mean_len_diff = len_diffs.mean() if len(len_diffs) > 0 else 0
    median_len_diff = len_diffs.median() if len(len_diffs) > 0 else 0
    
    # Pivot-safe by type
    pc_ch = pivot_critical[pivot_critical['csv_base_type'] == 'Cup With Handle']
    pc_db = pivot_critical[pivot_critical['csv_base_type'] == 'Double Bottom']
    
    print(f"🎯 PIVOT-CRITICAL ACCURACY (Cup+Handle & Double Bottom)")
    print(f"-------------------------------------------------------")
    print(f"Pivot-Critical Events            : {pc_total}")
    print(f"  Cup+Handle Exact               : {pc_ch['exact_match'].sum()} / {len(pc_ch)} ({pc_ch['exact_match'].mean()*100:.1f}%)")
    print(f"  Double Bottom Exact            : {pc_db['exact_match'].sum()} / {len(pc_db)} ({pc_db['exact_match'].mean()*100:.1f}%)")
    print(f"  Combined                       : {pc_exact} / {pc_total} ({pc_exact/pc_total*100:.1f}%)")
    if len(pc_ch) > 0:
        print(f"  Cup+Handle Pivot Err % (mean)  : {pc_ch['pivot_err_pct'].dropna().mean():.2f}%")
    if len(pc_db) > 0:
        print(f"  Double Bottom Pivot Err % (mean): {pc_db['pivot_err_pct'].dropna().mean():.2f}%")
    print()
    
    print(f"📊 OVERALL ACCURACY")
    print(f"-------------------------------------------------------")
    print(f"Total Evaluated Events           : {total}")
    print(f"Pattern Detected                 : {pattern_detected} / {total} ({pattern_detected/total*100:.1f}%)")
    print(f"Exact Pattern Match              : {exact_matches} / {total} ({exact_matches/total*100:.1f}%)")
    print(f"Pivot-Safe Match (same pivot)    : {pivot_safe_matches} / {total} ({pivot_safe_matches/total*100:.1f}%)")
    print(f"Broad Match                      : {broad_matches} / {total} ({broad_matches/total*100:.1f}%)")
    print(f"Pivot Price Error (mean/median)  : {mean_piv_err:.2f}% / {median_piv_err:.2f}%")
    print(f"Base Length Error (mean/median)  : {mean_len_diff:.1f} / {median_len_diff:.1f} bars\n")
    
    print(f"ACCURACY BREAKDOWN BY GROUND TRUTH DAILY BASE TYPE:")
    print(f"--------------------------------------------------------------------------------------------------------------")
    print(f"{'CSV Base Type':<20} | {'Count':<5} | {'Detect %':<9} | {'Exact %':<8} | {'Pivot-Safe %':<12} | {'Broad %':<8} | {'Piv Err %':<9}")
    print(f"--------------------------------------------------------------------------------------------------------------")
    
    summary_by_type = {}
    for btype, group in res_df.groupby('csv_base_type'):
        cnt = len(group)
        det_pct = group['is_detected'].mean() * 100
        exact_pct = group['exact_match'].mean() * 100
        safe_pct = group['pivot_safe'].mean() * 100
        broad_pct = group['broad_match'].mean() * 100
        piv_err = group['pivot_err_pct'].dropna().mean()
        is_pc = "🔴" if btype in ('Cup With Handle', 'Double Bottom') else "  "
        
        print(f"{is_pc} {btype:<18} | {cnt:<5} | {det_pct:8.1f}% | {exact_pct:7.1f}% | {safe_pct:11.1f}% | {broad_pct:7.1f}% | {piv_err:8.1f}%")
        
        summary_by_type[btype] = {
            'count': int(cnt),
            'detection_rate_pct': float(round(det_pct, 1)),
            'exact_match_pct': float(round(exact_pct, 1)),
            'pivot_safe_pct': float(round(safe_pct, 1)),
            'broad_match_pct': float(round(broad_pct, 1)),
            'mean_pivot_err_pct': float(round(piv_err, 2)) if pd.notna(piv_err) else None
        }
    print(f"--------------------------------------------------------------------------------------------------------------")
    print(f"  🔴 = pivot-critical pattern (lower pivot — exact match matters)")
    print()
    
    # Save CSV report. 'copy' keeps the original unsuffixed filenames (other tooling, e.g.
    # evaluate_pivot_equiv.py, reads these directly); 'prod' gets its own so the two targets
    # never clobber each other's results.
    suffix = '' if _args.target == 'copy' else '_prod'
    out_csv = ROOT_DIR / "python" / f"breakaway_gap_accuracy_results{suffix}.csv"
    res_df.to_csv(out_csv, index=False)
    print(f"💾 Detailed event-by-event accuracy results saved to {out_csv}")

    # Save JSON summary report
    out_json = ROOT_DIR / "python" / f"breakaway_gap_accuracy_summary{suffix}.json"
    summary_data = {
        'total_events': int(total),
        'pivot_critical_total': int(pc_total),
        'pivot_critical_exact': int(pc_exact),
        'pivot_critical_exact_pct': float(round(pc_exact/pc_total*100, 1)),
        'cup_handle_exact': int(pc_ch['exact_match'].sum()),
        'cup_handle_total': int(len(pc_ch)),
        'cup_handle_exact_pct': float(round(pc_ch['exact_match'].mean()*100, 1)),
        'double_bottom_exact': int(pc_db['exact_match'].sum()),
        'double_bottom_total': int(len(pc_db)),
        'double_bottom_exact_pct': float(round(pc_db['exact_match'].mean()*100, 1)),
        'pivot_safe_count': int(pivot_safe_matches),
        'pivot_safe_pct': float(round(pivot_safe_matches/total*100, 1)),
        'pattern_detected_count': int(pattern_detected),
        'pattern_detected_pct': float(round(pattern_detected/total*100, 1)),
        'exact_match_count': int(exact_matches),
        'exact_match_pct': float(round(exact_matches/total*100, 1)),
        'broad_match_count': int(broad_matches),
        'broad_match_pct': float(round(broad_matches/total*100, 1)),
        'mean_pivot_error_pct': float(round(mean_piv_err, 2)),
        'median_pivot_error_pct': float(round(median_piv_err, 2)),
        'mean_length_error_bars': float(round(mean_len_diff, 1)),
        'median_length_error_bars': float(round(median_len_diff, 1)),
        'breakdown_by_base_type': summary_by_type
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)
    print(f"💾 Summary metrics saved to {out_json}")

if __name__ == "__main__":
    evaluate_all_events()
