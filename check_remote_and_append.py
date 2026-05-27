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

def remote_ahead(branch):
    # Fetch remote refs
    run_cmd(["git", "fetch", "origin"], cwd=REPO_DIR)
    # Compare local..origin/branch
    r = run_cmd(["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"])
    if r.returncode != 0:
        return False
    out = r.stdout.strip()
    try:
        left, right = [int(x) for x in out.split()]
        # right = commits in origin ahead of local
        return right > 0
    except Exception:
        return False

def run_append_scripts():
    print("Running append scripts...")
    import sys
    py = sys.executable
    run_cmd([py, str(REPO_DIR / 'append_industry_history.py')], cwd=REPO_DIR)
    run_cmd([py, str(REPO_DIR / 'append_stocks_history.py')], cwd=REPO_DIR)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    if args.force:
        run_append_scripts()
        sys.exit(0)

    branch = get_current_branch()
    if remote_ahead(branch):
        print("Remote ahead — running append scripts")
        run_append_scripts()
    else:
        print("No new upstream commits")
