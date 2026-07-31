#!/usr/bin/env python3
"""
run_pipeline.py

Consolidated pipeline script for rs-log:
1. Syncs with upstream/main (merging remote changes).
2. Derives OHLCV, moving averages, ATR, volume ratios, technical, fund, and fundamental metrics.
3. Updates the same-day 'Volume' column via yfinance.
4. Appends industry history (rs_industries_historical.csv).
5. Appends stock history (rs_stocks_historical.csv).
"""

import sys
import os
import argparse
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
VENV_PYTHON = REPO_DIR.parent / "venv311" / "bin" / "python"
PYTHON_EXE = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

def run_cmd(cmd, cwd=REPO_DIR, check=False):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)

def get_current_branch():
    r = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if r.returncode == 0:
        return r.stdout.strip()
    return "main"

def configure_git_merge_driver():
    run_cmd(["git", "config", "merge.keep_theirs.name", "Always keep upstream/remote version of generated CSVs"])
    run_cmd(["git", "config", "merge.keep_theirs.driver", "cp %B %A"])

def sync_upstream(branch="main"):
    print("🔄 Fetching remotes (origin and upstream)...")
    run_cmd(["git", "fetch", "upstream"])
    run_cmd(["git", "fetch", "origin"])

    # Check if upstream/branch has commits that HEAD does not have
    r = run_cmd(["git", "rev-list", "--count", f"HEAD..upstream/{branch}"])
    if r.returncode != 0:
        print("⚠ Failed to check upstream rev-list.")
        return False

    try:
        count = int(r.stdout.strip())
        if count == 0:
            print(f"ℹ No new commits on upstream/{branch} compared to local HEAD.")
            return False

        print(f"📥 Upstream has {count} new commits. Merging upstream/{branch}...")

        # Stash any uncommitted changes (e.g. from prior pipeline runs) before merging
        stash_res = run_cmd(["git", "stash", "--include-untracked"])
        stashed = stash_res.returncode == 0 and "No local changes" not in stash_res.stdout

        merge_res = run_cmd(["git", "merge", "-X", "theirs", "--no-edit", f"upstream/{branch}"])
        if merge_res.returncode != 0:
            print("❌ Merge failed! Aborting merge...")
            run_cmd(["git", "merge", "--abort"])
            if stashed:
                run_cmd(["git", "stash", "pop"])
            print(merge_res.stderr)
            return False

        # Drop stash after successful merge (upstream's version wins via -X theirs)
        if stashed:
            run_cmd(["git", "stash", "drop"])

        print("✅ Merge successful.")
        return True
    except Exception as e:
        print(f"❌ Error during remote check/merge: {e}")
        return False

def push_to_origin(branch="main"):
    print(f"📤 Pushing updated local commits to origin/{branch}...")
    r = run_cmd(["git", "push", "origin", branch])
    if r.returncode == 0:
        print("✅ Pushed to origin successfully.")
    else:
        print("⚠ Push rejected; attempting pull --rebase and retry...")
        run_cmd(["git", "pull", "--rebase", "origin", branch])
        r2 = run_cmd(["git", "push", "origin", branch])
        if r2.returncode == 0:
            print("✅ Pushed to origin successfully after rebase.")
        else:
            print(f"❌ Failed to push to origin:\n{r2.stderr}")

def execute_consolidated_pipeline():
    print("\n=======================================================")
    print("🚀 Running Consolidated RS Data Pipeline")
    print("=======================================================\n")

    # Step 1: Derive OHLCV + Technical + Fundamental columns
    print("[1/4] Deriving 51-column OHLCV + technical + fundamental schema...")
    derive_script = REPO_DIR / 'python' / 'derive_marketsurge_technical_columns.py'
    r1 = run_cmd([PYTHON_EXE, str(derive_script)])
    if r1.returncode != 0:
        print(f"  ⚠ derive_marketsurge_technical_columns.py warning/error:\n{r1.stderr}")
    else:
        print("  ✓ Technical derivation complete.")

    # Step 2: Update same-day Volume column
    print("\n[2/4] Updating same-day Volume column via yfinance...")
    volume_script = REPO_DIR / 'python' / 'update_volume_column.py'
    r2 = run_cmd([PYTHON_EXE, str(volume_script)])
    if r2.returncode != 0:
        print(f"  ⚠ update_volume_column.py warning/error:\n{r2.stderr}")
    else:
        print("  ✓ Volume column update complete.")

    # Step 3: Append industry history
    print("\n[3/4] Appending industry history...")
    ind_script = REPO_DIR / 'append_industry_history.py'
    r3 = run_cmd([PYTHON_EXE, str(ind_script)])
    if r3.returncode != 0:
        print(f"  ⚠ append_industry_history.py warning/error:\n{r3.stderr}")
    else:
        print("  ✓ Industry history appended.")

    # Step 4: Append stocks history
    print("\n[4/5] Appending stocks history...")
    stock_script = REPO_DIR / 'append_stocks_history.py'
    r4 = run_cmd([PYTHON_EXE, str(stock_script)])
    if r4.returncode != 0:
        print(f"  ⚠ append_stocks_history.py warning/error:\n{r4.stderr}")
    else:
        print("  ✓ Stocks history appended.")

    # Step 5: Update ticker_cache parquets
    print("\n[5/5] Updating ticker_cache daily parquet files...")
    cache_script = REPO_DIR / 'python' / 'update_ticker_cache.py'
    r5 = run_cmd([PYTHON_EXE, str(cache_script)])
    if r5.returncode != 0:
        print(f"  ⚠ update_ticker_cache.py warning/error:\n{r5.stderr}")
    else:
        print("  ✓ Ticker cache parquets updated.")

    print("\n=======================================================")
    print("✅ Consolidated Pipeline Execution Finished!")
    print("=======================================================\n")

    # Auto-commit pipeline output so working tree is clean for next upstream merge
    run_cmd(["git", "add",
             "output/rs_stocks.csv", "output/rs_stocks_1.csv", "output/rs_stocks_2.csv",
             "output/rs_stocks_historical.csv", "output/rs_industries_historical.csv",
             "output/rs_stocks_metadata.json", "output/rs_industries_metadata.json"])
    run_cmd(["git", "commit", "-m", "chore: pipeline auto-update derived columns and history"])

def main():
    configure_git_merge_driver()
    parser = argparse.ArgumentParser(description="Run consolidated RS log pipeline.")
    parser.add_argument('--force', action='store_true', help="Force pipeline execution even if no new upstream commits.")
    parser.add_argument('--no-sync', action='store_true', help="Skip git fetch/merge from upstream.")
    args = parser.parse_args()

    branch = get_current_branch()

    if args.no_sync:
        execute_consolidated_pipeline()
        sys.exit(0)

    synced = sync_upstream(branch)
    if synced or args.force:
        execute_consolidated_pipeline()
        push_to_origin(branch)
    else:
        print("ℹ Local branch is already up to date with upstream. Use --force to run pipeline anyway.")

if __name__ == '__main__':
    main()
