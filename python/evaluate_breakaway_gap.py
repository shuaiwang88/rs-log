"""
evaluate_breakaway_gap.py

Evaluates the accuracy of python/ibd_pattern_scanner.py against the ground truth patterns
and breakout event dates in IBD/Breakaway Gap.csv.
"""

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

import importlib.util
spec = importlib.util.spec_from_file_location('ibd_pattern_scanner_copy', str(ROOT_DIR / 'python' / 'ibd_pattern_scanner copy.py'))
ibd_pattern_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ibd_pattern_scanner)

def clean_date(d_str):
    d_str = str(d_str).strip()
    m = re.match(r'^(\d{2})/(\d{2})(\d{4})$', d_str)
    if m:
        return f'{m.group(1)}/{m.group(2)}/{m.group(3)}'
    return d_str

# Base type mapping
EXACT_NAME_MAP = {
    'Cup Without Handle': {'Cup'},
    'Cup With Handle': {'Cup+Handle'},
    'Flat Base': {'Flat Base', '6-Wk Flat'},
    'Consolidation': {'Consolidation'},
    'Double Bottom': {'Dbl Bottom'},
    'Ascending Base': {'Ascending Base'}
}

BROAD_NAME_MAP = {
    'Cup Without Handle': {'Cup', 'Base', 'Consolidation'},
    'Cup With Handle': {'Cup+Handle', 'Cup'},
    'Flat Base': {'Flat Base', '6-Wk Flat', 'Consolidation'},
    'Consolidation': {'Consolidation', 'Base', 'Flat Base', 'Cup'},
    'Double Bottom': {'Dbl Bottom', 'Base'},
    'Ascending Base': {'Ascending Base'}
}

def evaluate_all_events():
    csv_path = ROOT_DIR / "IBD" / "Breakaway Gap.csv"
    csv_df = pd.read_csv(csv_path)
    csv_df['Clean_Date'] = csv_df['Event Date'].apply(clean_date)
    csv_df['Parsed_Date'] = pd.to_datetime(csv_df['Clean_Date'], format='mixed')
    
    print(f"=======================================================")
    print(f" RUNNING IBD PATTERN SCANNER ACCURACY EVALUATION")
    print(f" Ground Truth: {csv_path.name} ({len(csv_df)} total events)")
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
            
            # Use pattern from the last bar of the cut (scanner's latest output)
            det_name = scan_res.get('pattern_name', 'None')
            det_status = scan_res.get('status', 'None')
            dist_pct = scan_res.get('dist_pct')
            if det_close and dist_pct is not None and (1.0 + dist_pct/100.0) != 0:
                det_pivot = det_close / (1.0 + dist_pct / 100.0)
                
        is_detected = (det_name != 'None')
        exact_match = (det_name in EXACT_NAME_MAP.get(target_btype, set()))
        broad_match = (det_name in BROAD_NAME_MAP.get(target_btype, set()))
        
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
            'pivot_err_pct': round(piv_err_pct, 2) if piv_err_pct is not None else None,
            'len_diff_bars': len_diff
        })
        
    res_df = pd.DataFrame(results)
    
    total = len(res_df)
    pattern_detected = res_df['is_detected'].sum()
    exact_matches = res_df['exact_match'].sum()
    broad_matches = res_df['broad_match'].sum()
    
    piv_errs = res_df['pivot_err_pct'].dropna()
    mean_piv_err = piv_errs.mean() if len(piv_errs) > 0 else 0
    median_piv_err = piv_errs.median() if len(piv_errs) > 0 else 0
    
    len_diffs = res_df['len_diff_bars'].dropna()
    mean_len_diff = len_diffs.mean() if len(len_diffs) > 0 else 0
    median_len_diff = len_diffs.median() if len(len_diffs) > 0 else 0
    
    print(f"OVERALL EVALUATION ACCURACY METRICS:")
    print(f"-------------------------------------------------------")
    print(f"Total Evaluated Events          : {total}")
    print(f"Pattern Detected on Event Date  : {pattern_detected} / {total} ({pattern_detected/total*100:.1f}%)")
    print(f"Exact Pattern Type Match        : {exact_matches} / {total} ({exact_matches/total*100:.1f}%)")
    print(f"Broad Pattern Type Match        : {broad_matches} / {total} ({broad_matches/total*100:.1f}%)")
    print(f"Pivot Price Error %             : Mean = {mean_piv_err:.2f}%, Median = {median_piv_err:.2f}%")
    print(f"Base Length Error (Bars)        : Mean = {mean_len_diff:.1f} bars, Median = {median_len_diff:.1f} bars\n")
    
    print(f"ACCURACY BREAKDOWN BY GROUND TRUTH DAILY BASE TYPE:")
    print(f"-----------------------------------------------------------------------------------------")
    print(f"{'CSV Base Type':<20} | {'Count':<5} | {'Detected %':<10} | {'Exact Match %':<13} | {'Broad Match %':<13} | {'Piv Err %':<10}")
    print(f"-----------------------------------------------------------------------------------------")
    
    summary_by_type = {}
    for btype, group in res_df.groupby('csv_base_type'):
        cnt = len(group)
        det_pct = group['is_detected'].mean() * 100
        exact_pct = group['exact_match'].mean() * 100
        broad_pct = group['broad_match'].mean() * 100
        piv_err = group['pivot_err_pct'].dropna().mean()
        
        print(f"{btype:<20} | {cnt:<5} | {det_pct:9.1f}% | {exact_pct:12.1f}% | {broad_pct:12.1f}% | {piv_err:9.1f}%")
        
        summary_by_type[btype] = {
            'count': int(cnt),
            'detection_rate_pct': float(round(det_pct, 1)),
            'exact_match_pct': float(round(exact_pct, 1)),
            'broad_match_pct': float(round(broad_pct, 1)),
            'mean_pivot_err_pct': float(round(piv_err, 2)) if pd.notna(piv_err) else None
        }
    print(f"-----------------------------------------------------------------------------------------\n")
    
    # Save CSV report
    out_csv = ROOT_DIR / "python" / "breakaway_gap_accuracy_results.csv"
    res_df.to_csv(out_csv, index=False)
    print(f"💾 Detailed event-by-event accuracy results saved to {out_csv}")
    
    # Save JSON summary report
    out_json = ROOT_DIR / "python" / "breakaway_gap_accuracy_summary.json"
    summary_data = {
        'total_events': int(total),
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
