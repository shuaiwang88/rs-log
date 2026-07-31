"""Sweep key parameters of ibd_pattern_scanner.py and report best combinations."""
import subprocess, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "python" / "ibd_pattern_scanner.py"
BACKUP = ROOT / "python" / "ibd_pattern_scanner_sweep_backup.py"

# Save backup
import shutil
shutil.copy(SCANNER, BACKUP)

def apply_patch(params):
    """Replace parameter values in scanner file."""
    text = BACKUP.read_text()
    for old, new in params.items():
        text = text.replace(old, new)
    SCANNER.write_text(text)

def run_eval():
    """Run evaluation and return key metrics."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "python" / "evaluate_breakaway_gap.py"), "--target", "prod"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT)
    )
    m = {}
    for pat, key in [
        (r'Exact Pattern Match\s+:\s+(\d+)\s+/\s+(\d+)\s+\(([\d.]+)%\)', 'exact'),
        (r'Pivot-Safe Match[^:]*:\s+(\d+)\s+/\s+(\d+)\s+\(([\d.]+)%\)', 'pivot_safe'),
        (r'Broad Match\s+:\s+(\d+)\s+/\s+(\d+)\s+\(([\d.]+)%\)', 'broad'),
        (r'Cup With Handle\s+\|\s+\d+\s+\|\s+[\d.]+\s+\|\s+([\d.]+)', 'ch_exact'),
        (r'Double Bottom\s+\|\s+\d+\s+\|\s+[\d.]+\s+\|\s+([\d.]+)', 'db_exact'),
        (r'Flat Base\s+\|\s+\d+\s+\|\s+[\d.]+\s+\|\s+([\d.]+)', 'fb_exact'),
        (r'Consolidation\s+\|\s+\d+\s+\|\s+[\d.]+\s+\|\s+([\d.]+)', 'cons_exact'),
        (r'Cup With Handle\s+.*?\|\s+([\d.]+)%', 'ch_safe'),
        (r'Flat Base\s+.*?\|\s+([\d.]+)%', 'fb_safe'),
    ]:
        match = re.search(pat, r.stdout)
        if match:
            m[key] = float(match.group(1)) if '.' in match.group(1) else int(match.group(1))
    return m

# === PARAMETER COMBINATIONS TO TEST ===
# Each entry: (name, {old_string: new_string})
batches = [
    # Batch 0: baseline (no changes, just verify)
    ("BASELINE", {}),

    # Batch 1: Cup+Handle entry relaxation (rDepPct)
    ("CH_rDepPct_10", {'rDepPct > 12': 'rDepPct > 10'}),

    # Batch 2: Handle position relaxation (inTop)  
    ("CH_inTop_065", {'cupMid * 0.70': 'cupMid * 0.65'}),

    # Batch 3: Handle max depth relaxation  
    ("CH_maxDep_35", {'30.0\n                depOk_h': '35.0\n                depOk_h'}),

    # Batch 4: Combined handle position + entry
    ("CH_inTop065_rDep10", {
        'cupMid * 0.70': 'cupMid * 0.65',
        'rDepPct > 12': 'rDepPct > 10',
    }),

    # Batch 5: Base invalidation tighter
    ("Base_inval_135", {'bTop * 1.40': 'bTop * 1.35'}),

    # Batch 6: Base invalidation looser
    ("Base_inval_145", {'bTop * 1.40': 'bTop * 1.45'}),

    # Batch 7: DB symmetry relaxation
    ("DB_cA_080", {'fL * 0.85': 'fL * 0.80'}),

    # Batch 8: DB volume stronger
    ("DB_cVol_085", {'volumes[sLt] * 0.90': 'volumes[sLt] * 0.85'}),

    # Batch 9: DB tighter
    ("DB_tight", {
        'volumes[sLt] * 0.90': 'volumes[sLt] * 0.85',
        'dbMaxBars = 85': 'dbMaxBars = 75',
    }),

    # Batch 10: Handle tighter (small hdRatio)
    ("CH_hdRatio_050", {'hdRatio <= 0.55': 'hdRatio <= 0.50'}),

    # Batch 11: Guard lower
    ("CH_guard_22", {'bDepPct >= 25.0': 'bDepPct >= 22.0'}),

    # Batch 12: All conservative Cup+Handle changes
    ("CH_conservative", {
        'rDepPct > 12': 'rDepPct > 10',
        'cupMid * 0.70': 'cupMid * 0.65',
        'bDepPct >= 25.0': 'bDepPct >= 22.0',
    }),

    # Batch 13: DB + Base changes
    ("DB_Base_combo", {
        'volumes[sLt] * 0.90': 'volumes[sLt] * 0.85',
        'dbMaxBars = 85': 'dbMaxBars = 75',
        'bTop * 1.40': 'bTop * 1.38',
    }),

    # Batch 14: All changes together
    ("ALL_together", {
        'rDepPct > 12': 'rDepPct > 10',
        'cupMid * 0.70': 'cupMid * 0.65',
        'bDepPct >= 25.0': 'bDepPct >= 22.0',
        'volumes[sLt] * 0.90': 'volumes[sLt] * 0.85',
        'dbMaxBars = 85': 'dbMaxBars = 75',
        'bTop * 1.40': 'bTop * 1.38',
    }),
]

print(f"{'Batch':<25} {'Exact%':>8} {'PivSafe%':>10} {'Broad%':>8} {'CH%':>7} {'DB%':>7} {'FB%':>7}")
print("-" * 75)

results = []
for name, params in batches:
    apply_patch(params)
    m = run_eval()
    m['name'] = name
    results.append(m)
    print(f"{name:<25} {m.get('exact',0):>8.1f} {m.get('pivot_safe',0):>10.1f} {m.get('broad',0):>8.1f} {m.get('ch_exact',0):>7.1f} {m.get('db_exact',0):>7.1f} {m.get('fb_exact',0):>7.1f}")

# Restore backup
shutil.copy(BACKUP, SCANNER)
BACKUP.unlink()

# Find best
best = max(results, key=lambda r: r.get('pivot_safe', 0))
print(f"\n🏆 Best (pivot-safe): {best['name']} = {best.get('pivot_safe',0):.1f}%")
best_ex = max(results, key=lambda r: r.get('exact', 0))
print(f"🏆 Best (exact): {best_ex['name']} = {best_ex.get('exact',0):.1f}%")
best_ch = max(results, key=lambda r: r.get('ch_exact', 0))
print(f"🏆 Best (Cup+Handle): {best_ch['name']} = {best_ch.get('ch_exact',0):.1f}%")
