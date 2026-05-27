#!/usr/bin/env python3
"""
Build historical rs_industries data from git commits.
Combines all rs_industries.csv snapshots and creates a full dataset.
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

def build_historical_industry_data():
    """Build historical dataset from all commits"""
    print("🔍 Scanning git history for rs_industries.csv...")
    
    commits = get_all_commits()
    print(f"Found {len(commits)} commits")
    
    all_data = []
    processed = 0
    errors = 0
    
    for commit in commits:
        commit_hash = commit['hash']
        commit_date = commit['date']
        
        # Try to get rs_industries.csv from this commit
        file_content = get_file_from_commit(commit_hash, 'output/rs_industries.csv')
        
        if file_content:
            try:
                # Parse CSV
                from io import StringIO
                df = pd.read_csv(StringIO(file_content))
                
                # Add date column
                df['date'] = pd.to_datetime(commit_date)
                df['commit_hash'] = commit_hash
                
                all_data.append(df)
                processed += 1
                
                if processed % 50 == 0:
                    print(f"  ✓ Processed {processed} commits...")
                    
            except Exception as e:
                errors += 1
                if errors <= 5:  # Only print first 5 errors
                    print(f"  ✗ Error parsing commit {commit_hash}: {e}")
    
    print(f"\n✓ Processed {processed} commits with rs_industries.csv")
    
    if all_data:
        # Combine all data
        df_combined = pd.concat(all_data, ignore_index=True)
        
        # Calculate ranks (inverse of percentiles - lower percentile = lower rank)
        # For ranks: 1 = strongest, higher number = weaker
        # Map percentile to rank: 99th percentile = rank 1, 1st percentile = rank 100
        rank_cols_map = {
            '1M_RS_Percentile': '1M_RS_Rank',
            '3M_RS_Percentile': '3M_RS_Rank',
            '6M_RS_Percentile': '6M_RS_Rank'
        }
        
        for percentile_col, rank_col in rank_cols_map.items():
            if percentile_col in df_combined.columns:
                # Percentile to rank: percentile 99 -> rank 1, percentile 50 -> rank 50, percentile 1 -> rank 99
                # Rank = 101 - percentile (assuming 1-100 scale)
                df_combined[rank_col] = 101 - df_combined[percentile_col]
        
        # Sort by date descending (most recent first)
        df_combined = df_combined.sort_values('date', ascending=False)
        
        # Save combined dataset
        output_path = Path('/Users/sw/Desktop/stock/rs-log/output/rs_industries_historical.csv')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        df_combined.to_csv(output_path, index=False)
        print(f"\n✅ Saved historical data: {len(df_combined)} records to {output_path}")
        print(f"   Date range: {df_combined['date'].min()} to {df_combined['date'].max()}")
        
        # Save metadata
        metadata = {
            'total_records': len(df_combined),
            'unique_dates': df_combined['date'].nunique(),
            'date_min': str(df_combined['date'].min()),
            'date_max': str(df_combined['date'].max()),
            'commits_processed': processed,
            'last_updated': datetime.now().isoformat()
        }
        
        metadata_path = Path('/Users/sw/Desktop/stock/rs-log/output/rs_industries_metadata.json')
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"\n📊 Dataset Summary:")
        print(f"   Total records: {metadata['total_records']:,}")
        print(f"   Unique dates: {metadata['unique_dates']}")
        print(f"   Date range: {metadata['date_min']} to {metadata['date_max']}")
        
        return df_combined
    else:
        print("❌ No data found")
        return None

if __name__ == "__main__":
    build_historical_industry_data()
