"""Run the independent Decimal oracle in separate disposable processes.

python scripts/accounting_repair_sequences.py --workers 4 --seeds 100 --steps 200
Workers run disjoint seeds; this is process isolation, not database contention.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
import time

import accounting_audit as audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output", type=Path, default=audit.ROOT / "docs/accounting-repair-sequences.json")
    args = parser.parse_args()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ledger-sequences-") as directory:
        temp = Path(directory)
        if args.worker:
            audit.setup(temp / "synthetic.sqlite3")
            audit.sequences(args.seeds, args.steps, args.first_seed)
            result = audit.RESULTS["sequences"]
            result["first_seed"] = args.first_seed
            from django.db import connections
            connections.close_all()
        else:
            hashes = {str(p.relative_to(audit.ROOT)): audit.digest(p) for p in (audit.ROOT / "ledger").rglob("*.py")}
            ranges = [(args.first_seed + i * args.seeds // args.workers,
                       (i + 1) * args.seeds // args.workers - i * args.seeds // args.workers) for i in range(args.workers)]
            def run(segment):
                first, count = segment
                output = temp / f"seed-{first}.json"
                child = subprocess.run([sys.executable, __file__, "--worker", "--first-seed", str(first), "--seeds", str(count), "--steps", str(args.steps), "--output", str(output)], cwd=audit.ROOT)
                assert child.returncode == 0
                return json.loads(output.read_text(encoding="utf-8"))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                workers = list(pool.map(run, ranges))
            result = {"seeds": args.seeds, "steps_per_seed": args.steps, "workers": workers,
                      "source_sha256": hashes, "source_unchanged_during_run": all(audit.digest(audit.ROOT / p) == h for p, h in hashes.items())}
            for key in ("completed_operations", "balance_mismatches", "history_mismatches", "stale_snapshot_observations", "relationship_violations"):
                result[key] = sum(worker[key] for worker in workers)
            result["relationship_note"] = "Active payments referencing reversed bills represent supported supplier credit."
        result["wall_seconds"] = round(time.perf_counter() - started, 3)
        args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        assert all(result[key] == 0 for key in ("balance_mismatches", "history_mismatches", "stale_snapshot_observations")), result
    print(f"Verified seeds {args.first_seed}..{args.first_seed + args.seeds - 1}: {args.output}", flush=True)


if __name__ == "__main__":
    main()
