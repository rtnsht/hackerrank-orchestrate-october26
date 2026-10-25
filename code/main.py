"""Ghost Data Still Exists - deletion-audit agent.

Usage (from the repository root):
    python3 code/main.py                    # audit dataset/deletion_requests.csv -> ./output.csv
    python3 code/main.py --cases sample     # audit the 32 public samples -> code/evaluation/sample_predictions.csv
    python3 code/main.py --debug case_11    # print linked resources and reconstructed state for one case
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost.attribution import Attribution  # noqa: E402
from ghost.audit import audit_case  # noqa: E402
from ghost.identity import IdentityGraph  # noqa: E402
from ghost.intent import classify  # noqa: E402
from ghost.loaders import load_all  # noqa: E402
from ghost.policy import Policy  # noqa: E402
from ghost.util import CODE_DIR, DATASET_DIR, REPO_ROOT, norm_ts  # noqa: E402

INPUTS = {"eval": DATASET_DIR / "deletion_requests.csv", "sample": DATASET_DIR / "sample_deletion_requests.csv"}
OUTPUTS = {"eval": REPO_ROOT / "output.csv", "sample": CODE_DIR / "evaluation" / "sample_predictions.csv"}


def read_requests(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("requested_at", "reported_at", "audit_at"):
            r[k] = norm_ts(r[k])
    return sorted(rows, key=lambda r: r["case_id"])


def run(requests: list[dict], debug: str | None = None) -> list[dict]:
    store, graph, policy = load_all(), IdentityGraph(), Policy()
    by_res = store.by_resource()
    # attribution is global: every known deletion request competes for the same activity stream
    known = read_requests(INPUTS["eval"]) + read_requests(INPUTS["sample"])
    deletion_cases = [r for r in known if classify(r["request_text"])[0]]
    attribution = Attribution(store, graph, deletion_cases)
    out = []
    for req in requests:
        authorized, reason = classify(req["request_text"])
        if not authorized:
            out.append({"case_id": req["case_id"], "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"})
            if debug == req["case_id"]:
                print("intent:", reason)
            continue
        res = audit_case(req, graph.resolve(req["email"]), store, by_res, policy, attribution)
        if debug == req["case_id"]:
            print("intent:", reason)
            print(json.dumps(res.debug, indent=1, default=str))
            for s in res.steps:
                print(s)
        out.append({"case_id": req["case_id"], "deletion_status": res.status,
                    "steps_json": json.dumps(res.steps, ensure_ascii=False, separators=(",", ":"))})
    return out


def write_output(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", choices=["eval", "sample"], default="eval")
    ap.add_argument("--debug", help="case_id to print diagnostics for")
    ap.add_argument("--out", help="override output path")
    args = ap.parse_args()
    rows = run(read_requests(INPUTS[args.cases]), args.debug)
    out = Path(args.out) if args.out else OUTPUTS[args.cases]
    write_output(rows, out)
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"wrote {len(rows)} rows to {out} {counts}")


if __name__ == "__main__":
    main()
