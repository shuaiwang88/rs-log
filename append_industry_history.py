#!/usr/bin/env python3
"""
Append new rs_industries data from recent commits to historical dataset.
Run this daily to keep the historical data updated.
"""

import subprocess
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from rs_pipeline_utils import REPO_DIR, get_file_from_commit, save_metadata, now_iso

def get_last_date_in_history():
    """Get the last date from the historical file"""
    try:
        historical_path = REPO_DIR / 'output' / 'rs_industries_historical.csv'
        df = pd.read_csv(historical_path, usecols=['date'], nrows=1)
        if 'date' in df.columns and not df['date'].isna().all():
            return pd.to_datetime(df['date'].iloc[0])
    except:
        pass
    return None

def get_commits_after_date(cutoff_date=None):
    """Get commits after a specific date"""
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H|%ad|%s", "--date=short"],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True
        )
        commits = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('|', 2)
                if len(parts) >= 2:
                    commit_date = pd.to_datetime(parts[1])
                    if cutoff_date is None or commit_date > cutoff_date:
                        commits.append({
                            'hash': parts[0],
                            'date': parts[1],
                            'message': parts[2] if len(parts) > 2 else ''
                        })
        return commits
    except Exception as e:
        print(f"Error getting commits: {e}")
        return []

# use shared `get_file_from_commit` from `rs_pipeline_utils`

def append_new_data():
    """Append new data from recent commits"""
    print("🔄 Checking for new rs_industries.csv commits...")
    
    historical_path = REPO_DIR / 'output' / 'rs_industries_historical.csv'
    
    if not historical_path.exists():
        print("❌ Historical file not found. Run build_industry_history.py first.")
        return False
    
    # Get last date in history
    last_date = get_last_date_in_history()
    print(f"Last date in history: {last_date.date() if last_date else 'None'}")
    
    # Get new commits
    new_commits = get_commits_after_date(last_date)
    
    if not new_commits:
        print("✅ No new commits found. Dataset is current.")
        return True
    
    print(f"Found {len(new_commits)} new commits to process")
    
    # Process new commits
    new_data = []
    processed = 0
    
    for commit in new_commits:
        commit_hash = commit['hash']
        commit_date = commit['date']
        
        file_content = get_file_from_commit(commit_hash, 'output/rs_industries.csv', repo_dir=REPO_DIR)
        
        if file_content:
            try:
                from io import StringIO
                df = pd.read_csv(StringIO(file_content))
                
                # Add date and commit info
                df['date'] = pd.to_datetime(commit_date)
                df['commit_hash'] = commit_hash
                
                # Calculate ranks
                rank_cols_map = {
                    '1M_RS_Percentile': '1M_RS_Rank',
                    '3M_RS_Percentile': '3M_RS_Rank',
                    '6M_RS_Percentile': '6M_RS_Rank'
                }
                
                for percentile_col, rank_col in rank_cols_map.items():
                    if percentile_col in df.columns:
                        df[rank_col] = 101 - df[percentile_col]
                
                new_data.append(df)
                processed += 1
                
            except Exception as e:
                print(f"  ✗ Error processing commit {commit_hash}: {e}")
    
    if new_data:
        # Read existing data
        df_existing = pd.read_csv(historical_path)
        df_existing['date'] = pd.to_datetime(df_existing['date'])
        
        # Combine with new data
        df_new = pd.concat(new_data, ignore_index=True)
        df_combined = pd.concat([df_new, df_existing], ignore_index=True)
        
        # Ensure date is datetime type
        df_combined['date'] = pd.to_datetime(df_combined['date'])
        
        # Sort by date descending
        df_combined = df_combined.sort_values('date', ascending=False)
        
        # Remove duplicates (keep newest)
        df_combined = df_combined.drop_duplicates(subset=['Industry', 'date'], keep='first')
        
        # Save
        df_combined.to_csv(historical_path, index=False)
        
        print(f"\n✅ Appended {processed} new records")
        print(f"   Total records now: {len(df_combined):,}")
        
        # Update metadata
        metadata = {
            'total_records': len(df_combined),
            'unique_dates': df_combined['date'].nunique(),
            'date_min': str(df_combined['date'].min()),
            'date_max': str(df_combined['date'].max()),
            'last_updated': datetime.now().isoformat()
        }
        
        metadata_path = REPO_DIR / 'output' / 'rs_industries_metadata.json'
        save_metadata(metadata_path, metadata)
        
        return True
    else:
        print("❌ No new rs_industries.csv found in commits")
        return False

if __name__ == "__main__":
    append_new_data()
