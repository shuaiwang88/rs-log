#!/usr/bin/env python3
"""
Helper utilities for RS log pipelines.
Provides repo-local paths and small convenience wrappers.
"""
import subprocess
import json
from pathlib import Path
from datetime import datetime
import logging

REPO_DIR = Path(__file__).parent

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)


def run_git_command(cmd, cwd=None, timeout=None):
    cwd = Path(cwd) if cwd else REPO_DIR
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def get_file_from_commit(commit_hash, file_path, repo_dir=None):
    repo_dir = Path(repo_dir) if repo_dir else REPO_DIR
    try:
        r = run_git_command(["git", "show", f"{commit_hash}:{file_path}"], cwd=repo_dir)
        if r.returncode == 0:
            return r.stdout
    except subprocess.TimeoutExpired:
        log.warning("git show timed out for %s:%s", commit_hash, file_path)
    return None


def save_metadata(output_path, metadata):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(metadata, f, indent=2)


def now_iso():
    return datetime.now().isoformat()
