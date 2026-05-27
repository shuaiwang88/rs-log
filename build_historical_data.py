#!/usr/bin/env python3
"""
Pipeline to extract and consolidate all historical RS data from git commits.
Each commit represents one day's RS calculation log.
"""

import subprocess
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
import io
from rs_pipeline_utils import REPO_DIR, get_file_from_commit, run_git_command, save_metadata, now_iso

def get_all_commits():
    """Get all commits with their hashes and dates"""
    result = run_git_command(['git', 'log', '--format=%H%n%ai', '--all'], cwd=REPO_DIR)
    
    commits = []
    lines = result.stdout.strip().split('\n')
    
    for i in range(0, len(lines), 2):
        if i + 1 < len(lines):
            commit_hash = lines[i]
            commit_date = lines[i + 1].split()[0]  # Extract just the date part
            commits.append({
                'hash': commit_hash,
                'date': commit_date
            })
    
    return commits

def get_csv_from_commit(commit_hash, filename):
    """Extract a specific CSV file from a git commit"""
    try:
        return get_file_from_commit(commit_hash, f'output/{filename}', repo_dir=REPO_DIR)
    except Exception as e:
        print(f"Error getting {filename} from {commit_hash}: {e}")
        return None

def build_historical_data():
    """Build consolidated historical dataset"""
    print("Extracting commits from git history...")
    commits = get_all_commits()
    print(f"Found {len(commits)} commits")
    
    all_data = []
    csv_files = ['rs_stocks.csv', 'rs_stocks_1.csv', 'rs_stocks_2.csv']
    
    for idx, commit in enumerate(commits):
        if idx % 100 == 0:
            print(f"Processing commit {idx}/{len(commits)} ({commit['date']})...")
        
        commit_date = commit['date']
        found_data = False
        
        # Try each CSV file
        for csv_file in csv_files:
            csv_content = get_csv_from_commit(commit['hash'], csv_file)
            
            if csv_content:
                try:
                    df = pd.read_csv(io.StringIO(csv_content))
                    # Add date column
                    df['date'] = commit_date
                    all_data.append(df)
                    found_data = True
                    break  # Found data for this commit, move to next
                except Exception as e:
                    print(f"Error parsing {csv_file} from {commit['hash']}: {e}")
        
        if not found_data:
            print(f"Warning: No RS data found for commit {commit['hash']} ({commit_date})")
    
    if all_data:
        # Combine all data
        historical_df = pd.concat(all_data, ignore_index=True)
        
        # Ensure date is datetime
        historical_df['date'] = pd.to_datetime(historical_df['date'])
        
        # Sort by date
        historical_df = historical_df.sort_values('date').reset_index(drop=True)
        
        # Save to CSV
        output_path = REPO_DIR / 'output' / 'rs_historical_all.csv'
        output_path.parent.mkdir(parents=True, exist_ok=True)
        historical_df.to_csv(output_path, index=False)
        
        print(f"\n✅ Successfully created historical dataset!")
        print(f"   Total records: {len(historical_df)}")
        print(f"   Date range: {historical_df['date'].min()} to {historical_df['date'].max()}")
        print(f"   Unique dates: {historical_df['date'].nunique()}")
        print(f"   Unique tickers: {historical_df['Ticker'].nunique()}")
        print(f"   Saved to: {output_path}")
        
        return historical_df
    else:
        print("❌ No data was extracted!")
        return None

if __name__ == '__main__':
    print("=" * 60)
    print("RS Historical Data Pipeline")
    print("=" * 60)
    df = build_historical_data()
    if df is not None:
        print("\n📊 Sample of data (first 5 rows):")
        print(df.head())
        print("\n📊 Data info:")
        print(f"Shape: {df.shape}")
        print(f"\nColumns: {df.columns.tolist()}")
