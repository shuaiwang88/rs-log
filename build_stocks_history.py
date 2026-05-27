#!/usr/bin/env python3
"""
Build historical rs_stocks data from git commits.
Combines all rs_stocks_1.csv and rs_stocks_2.csv snapshots into a full dataset.
"""

import subprocess
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
import os

def get_all_commits():
    """Get all commits with dates"""
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H|%ad|%s", "--date=short"],
            cwd="/Users/sw/Desktop/stock/rs-log",
            capture_output=True,
            text=True
        )
        commits = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('|', 2)
                if len(parts) >= 2:
                    commits.append({
                        'hash': parts[0],
                        'date': parts[1],
                        'message': parts[2] if len(parts) > 2 else ''
                    })
        return commits
    except Exception as e:
        print(f"Error getting commits: {e}")
        return []

def get_file_from_commit(commit_hash, file_path):
    """Get file content from a specific commit"""
    try:
        result = subprocess.run(
            ["git", "show", f"{commit_hash}:{file_path}"],
            cwd="/Users/sw/Desktop/stock/rs-log",
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception as e:
        print(f"Error getting file from commit {commit_hash}: {e}")
        return None

def build_historical_stocks_data():
    """Build historical dataset from all commits"""
    print("🔍 Scanning git history for rs_stocks*.csv...")
    
    commits = get_all_commits()
    print(f"Found {len(commits)} commits")
    
    all_data = []
    processed = 0
    errors = 0
    
    for commit in commits:
        commit_hash = commit['hash']
        commit_date = commit['date']
        
        # Try both rs_stocks_1.csv and rs_stocks_2.csv
        for file_num in [1, 2]:
            file_path = f'output/rs_stocks_{file_num}.csv'
            file_content = get_file_from_commit(commit_hash, file_path)
            
            if file_content:
                try:
                    from io import StringIO
                    df = pd.read_csv(StringIO(file_content))
                    
                    # Add date and commit info
                    df['date'] = pd.to_datetime(commit_date)
                    df['commit_hash'] = commit_hash
                    
                    # Calculate ranks for RS metrics if available
                    rank_cols_map = {
                        '1M_RS_Percentile': '1M_RS_Rank',
                        '3M_RS_Percentile': '3M_RS_Rank',
                        '6M_RS_Percentile': '6M_RS_Rank'
                    }
                    
                    for percentile_col, rank_col in rank_cols_map.items():
                        if percentile_col in df.columns:
                            df[rank_col] = 101 - df[percentile_col]
                    
                    all_data.append(df)
                    processed += 1
                    
                    if processed % 100 == 0:
                        print(f"  ✓ Processed {processed} stock files...")
                        
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        print(f"  ✗ Error parsing {file_path} from {commit_hash}: {e}")
    
    print(f"\n✓ Processed {processed} stock CSV files")
    
    if all_data:
        # Combine all data
        df_combined = pd.concat(all_data, ignore_index=True)
        
        # Sort by date descending
        df_combined = df_combined.sort_values('date', ascending=False)
        
        # Save combined dataset
        output_path = Path('/Users/sw/Desktop/stock/rs-log/output/rs_stocks_historical.csv')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        df_combined.to_csv(output_path, index=False)
        print(f"\n✅ Saved historical stock data: {len(df_combined)} records to {output_path}")
        print(f"   Date range: {df_combined['date'].min()} to {df_combined['date'].max()}")
        print(f"   Unique dates: {df_combined['date'].nunique()}")
        print(f"   Unique stocks: {df_combined['Ticker'].nunique() if 'Ticker' in df_combined.columns else 'N/A'}")
        
        # Save metadata
        metadata = {
            'total_records': len(df_combined),
            'unique_dates': df_combined['date'].nunique(),
            'unique_stocks': int(df_combined['Ticker'].nunique()) if 'Ticker' in df_combined.columns else None,
            'date_min': str(df_combined['date'].min()),
            'date_max': str(df_combined['date'].max()),
            'files_processed': processed,
            'last_updated': datetime.now().isoformat()
        }
        
        metadata_path = Path('/Users/sw/Desktop/stock/rs-log/output/rs_stocks_metadata.json')
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"\n📊 Dataset Summary:")
        print(f"   Total records: {metadata['total_records']:,}")
        print(f"   Unique dates: {metadata['unique_dates']}")
        print(f"   Unique stocks: {metadata['unique_stocks']:,}" if metadata['unique_stocks'] else "   Unique stocks: N/A")
        print(f"   Date range: {metadata['date_min']} to {metadata['date_max']}")
        
        return df_combined
    else:
        print("❌ No stock data found")
        return None

if __name__ == "__main__":
    build_historical_stocks_data()
