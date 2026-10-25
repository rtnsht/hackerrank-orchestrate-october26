"""Score predictions against labelled answers.

Usage (from the repository root):
    python3 code/evaluation/main.py                                   # sample predictions vs dataset/sample_output.csv
    python3 code/evaluation/main.py --pred P.csv --gold G.csv -v      # any pair, per-case diffs
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)}


def key(step: dict) -> tuple:
    return tuple(step.get(f) for f in FIELDS)


def score(pred: dict, gold: dict, verbose: bool = False) -> dict:
    n = len(gold)
    status_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits = {f: 0 for f in FIELDS}
    field_total = 0
    failures = []
    for cid, (g_status, g_steps) in sorted(gold.items()):
        p_status, p_steps = pred.get(cid, ("MISSING", []))
        status_ok += p_status == g_status
        exact = [key(s) for s in p_steps] == [key(s) for s in g_steps]
        seq_ok += exact
        g_keys, p_keys = [key(s) for s in g_steps], [key(s) for s in p_steps]
        remaining = list(g_keys)
        for k in p_keys:
            if k in remaining:
                remaining.remove(k)
                tp += 1
            else:
                fp += 1
        fn += len(remaining)
        # field-level: align by position
        for i, gs in enumerate(g_steps):
            field_total += 1
            ps = p_steps[i] if i < len(p_steps) else {}
            for f in FIELDS:
                field_hits[f] += ps.get(f) == gs.get(f)
        if not exact or p_status != g_status:
            failures.append(cid)
            if verbose:
                print(f"--- {cid}: pred {p_status} / gold {g_status}")
                for k in p_keys:
                    if k not in g_keys:
                        print("   + extra   ", k)
                for k in g_keys:
                    if k not in p_keys:
                        print("   - missing ", k)
                if set(p_keys) == set(g_keys):
                    print("   ~ same steps, different order")
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "sequence_accuracy": seq_ok / n,
        "step_precision": prec,
        "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        "field_accuracy": {f: field_hits[f] / field_total for f in FIELDS} if field_total else {},
        "failures": failures,
    }


def run_custom(verbose: bool) -> dict:
    """Run the pipeline on participant-built mutations of the public samples and score them."""
    import sys
    sys.path.insert(0, str(REPO / "code"))
    import main as pipeline
    spec = json.loads((REPO / "code" / "evaluation" / "custom_cases.json").read_text(encoding="utf-8"))
    gold = load(REPO / "dataset" / "sample_output.csv")
    results = []
    for case in spec["cases"]:
        requests = pipeline.read_requests(pipeline.INPUTS["sample"])
        for r in requests:
            if r["case_id"] == case["base"]:
                r.update({k: pipeline.norm_ts(v) if k.endswith("_at") else v for k, v in case["override"].items()})
        row = next(r for r in pipeline.run(requests) if r["case_id"] == case["base"])
        exp = case["expected"]
        exp_status, exp_steps = gold[case["base"]] if exp == "same_as_gold" else (exp["deletion_status"], exp["steps"])
        got = (row["deletion_status"], json.loads(row["steps_json"]))
        passed = got[0] == exp_status and [key(s) for s in got[1]] == [key(s) for s in exp_steps]
        results.append({"id": case["id"], "passed": passed, "tests": case["tests"]})
        if verbose and not passed:
            print(case["id"], "expected", exp_status, [key(s) for s in exp_steps], "\n got", got[0],
                  [key(s) for s in got[1]])
    return {"custom_cases": len(results), "passed": sum(r["passed"] for r in results), "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--custom", action="store_true", help="run and score the participant-built custom cases")
    ap.add_argument("--pred", default=str(REPO / "code" / "evaluation" / "sample_predictions.csv"))
    ap.add_argument("--gold", default=str(REPO / "dataset" / "sample_output.csv"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.custom:
        print(json.dumps(run_custom(args.verbose), indent=2))
        return
    res = score(load(Path(args.pred)), load(Path(args.gold)), args.verbose)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
