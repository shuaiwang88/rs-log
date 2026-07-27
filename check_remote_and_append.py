#!/usr/bin/env python3
"""
Check remote for new commits and run append scripts when needed.

Usage:
  python3 check_remote_and_append.py [--force]

If --force is provided the append scripts will always run.
Otherwise the script will 'git fetch' and check whether origin/<branch>
has new commits ahead of local HEAD. If so it runs the append scripts.
"""
import subprocess
import argparse
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent

def run_cmd(cmd, cwd=REPO_DIR, check=False):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)

def get_current_branch():
    r = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if r.returncode == 0:
        return r.stdout.strip()
    return "main"

def check_and_sync_upstream(branch):
    print("Fetching remotes (origin and upstream)...")
    run_cmd(["git", "fetch", "upstream"], cwd=REPO_DIR)
    run_cmd(["git", "fetch", "origin"], cwd=REPO_DIR)
    
    # Check if upstream/branch has commits that HEAD does not have
    r = run_cmd(["git", "rev-list", "--count", f"HEAD..upstream/{branch}"], cwd=REPO_DIR)
    if r.returncode != 0:
        print("Failed to run rev-list check.")
        return False
        
    try:
        count = int(r.stdout.strip())
        if count == 0:
            print(f"No new commits on upstream/{branch} compared to local HEAD.")
            return False
            
        print(f"Upstream has {count} new commits. Merging upstream/{branch}...")
        # Merge with strategy option 'theirs' to resolve conflicting CSV modifications in favor of upstream
        merge_res = run_cmd(["git", "merge", "-X", "theirs", "--no-edit", f"upstream/{branch}"], cwd=REPO_DIR)
        if merge_res.returncode != 0:
            print("Merge failed! Aborting merge...")
            run_cmd(["git", "merge", "--abort"], cwd=REPO_DIR)
            print(merge_res.stderr)
            return False
            
        print("Merge successful.")
        return True
    except Exception as e:
        print(f"Error during remote check/merge: {e}")
        return False

def push_to_origin(branch):
    print(f"Pushing updated local commits to origin/{branch}...")
    r = run_cmd(["git", "push", "origin", branch], cwd=REPO_DIR)
    if r.returncode == 0:
        print("Pushed to origin successfully.")
    else:
        print("Initial push rejected; performing git pull --rebase origin and pushing...")
        run_cmd(["git", "pull", "--rebase", "origin", branch], cwd=REPO_DIR)
        r2 = run_cmd(["git", "push", "origin", branch], cwd=REPO_DIR)
        if r2.returncode == 0:
            print("Pushed to origin successfully after rebase.")
        else:
            print(f"Failed to push to origin:\n{r2.stderr}")

def run_append_scripts():
    print("Running daily pipeline: derive OHLCV + technical columns, then append history...")
    import sys
    py = sys.executable

    # Step 1: Derive Open/High/Low/Close/Volume + all technical, ATR, volume ratio, fund & fundamental columns
    print("\n[1/3] Deriving 51-column OHLCV + technical + fundamental schema...")
    r = run_cmd([py, str(REPO_DIR / 'python' / 'derive_marketsurge_technical_columns.py')], cwd=REPO_DIR)
    if r.returncode != 0:
        print(f"  ⚠ derive_marketsurge_technical_columns.py exited with error:\n{r.stderr}")
    else:
        print("  ✓ Technical derivation complete.")

    # Step 2: Append industry history
    print("\n[2/3] Appending industry history...")
    r = run_cmd([py, str(REPO_DIR / 'append_industry_history.py')], cwd=REPO_DIR)
    if r.returncode != 0:
        print(f"  ⚠ append_industry_history.py error:\n{r.stderr}")
    else:
        print("  ✓ Industry history appended.")

    # Step 3: Append stocks history
    print("\n[3/3] Appending stocks history...")
    r = run_cmd([py, str(REPO_DIR / 'append_stocks_history.py')], cwd=REPO_DIR)
    if r.returncode != 0:
        print(f"  ⚠ append_stocks_history.py error:\n{r.stderr}")
    else:
        print("  ✓ Stocks history appended.")

    print("\n✅ Daily pipeline complete.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    if args.force:
        run_append_scripts()
        sys.exit(0)

    branch = get_current_branch()
    if check_and_sync_upstream(branch):
        print("Successfully synced with upstream — running append scripts")
        run_append_scripts()
        push_to_origin(branch)
    else:
        print("No updates applied.")
