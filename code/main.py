"""Ghost Data Still Exists - deletion-audit agent.

Usage (from the repository root):
    python3 code/main.py                    # audit dataset/deletion_requests.csv -> ./output.csv
    python3 code/main.py --cases sample     # audit the 32 public samples -> code/evaluation/sample_predictions.csv
    python3 code/main.py --debug case_11    # print linked resources and reconstructed state for one case
    python3 code/main.py --extract          # (re)extract screenshots/PDFs with Claude vision into code/cache/

Claude is used for intent classification and multimodal extraction when ANTHROPIC_API_KEY is set
(environment or an uncommitted .env); cached results make re-runs deterministic. GHOST_OFFLINE=1
forces the cached/rule-based path.
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
from ghost.extract import extract_all  # noqa: E402
from ghost.intent import classify, save_cache  # noqa: E402
from ghost.llm import USAGE  # noqa: E402
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


VALID = {
    "FINDING": {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"},
    "REMEDIATION": {"DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
                    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED"},
    "VERIFICATION": {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
                     "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                     "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"},
}


def validate(rows: list[dict], requests: list[dict]) -> list[str]:
    """Contract checks for the submission file; returns a list of problems (empty = valid)."""
    problems = []
    if [r["case_id"] for r in rows] != sorted(q["case_id"] for q in requests):
        problems.append("case ids missing, duplicated or unsorted")
    phases = list(VALID)
    for r in rows:
        steps = json.loads(r["steps_json"])
        if r["deletion_status"] == "NOT_APPLICABLE" and steps:
            problems.append(f"{r['case_id']}: NOT_APPLICABLE must have []")
        last_phase = 0
        for i, s in enumerate(steps, 1):
            if set(s) != {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}:
                problems.append(f"{r['case_id']}: bad keys in step {i}")
            if s["step_no"] != i:
                problems.append(f"{r['case_id']}: step_no not consecutive")
            if VALID.get(s["phase"], {}).get(s["operation"]) != s["state"]:
                problems.append(f"{r['case_id']}: invalid phase/operation/state {s}")
            if phases.index(s["phase"]) < last_phase:
                problems.append(f"{r['case_id']}: phases out of order")
            last_phase = phases.index(s["phase"])
            if r["deletion_status"] == "INCOMPLETE" and s["phase"] != "FINDING" and s["evidence_id"]:
                problems.append(f"{r['case_id']}: future step cites evidence")
        if r["deletion_status"] == "COMPLETE" and any(s["phase"] != "VERIFICATION" for s in steps):
            problems.append(f"{r['case_id']}: COMPLETE with non-verification steps")
    return problems


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
    ap.add_argument("--extract", action="store_true", help="run Claude vision extraction for uncached PNG/PDF evidence")
    args = ap.parse_args()
    if args.extract:
        extract_all()
    requests = read_requests(INPUTS[args.cases])
    rows = run(requests, args.debug)
    save_cache()
    problems = validate(rows, requests)
    if problems:
        print("VALIDATION FAILED:\n  " + "\n  ".join(problems[:20]))
        sys.exit(1)
    out = Path(args.out) if args.out else OUTPUTS[args.cases]
    write_output(rows, out)
    if args.cases == "eval":
        USAGE.save(len(requests))
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"wrote {len(rows)} rows to {out} {counts}")


if __name__ == "__main__":
    main()
