#!/usr/bin/env python3
"""Ghost Data Still Exists — deletion-audit agent entry point.

Deterministic Python owns identity resolution, audit_at cutoffs, deletion_rules
application, step ordering, and CSV output. An OpenAI-compatible LLM classifies
request_text intent (with regex fallback) and may optionally assist messy PDF
text extraction; it never invents resource_ids or writes steps_json.

Usage:
  python3 code/main.py --sample
  python3 code/main.py
  python3 code/main.py --dataset dataset --output output.csv

LLM (optional):
  GHOST_DATA_LLM_API_KEY or OPENAI_API_KEY
  GHOST_DATA_LLM_BASE_URL, GHOST_DATA_LLM_MODEL (default gpt-4o-mini)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Allow `python3 code/main.py` from repo root.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auditor import DeletionAuditor
from evidence import EvidenceIndex, load_system_activity
from identity import IdentityIndex
from llm import get_llm_client, reset_llm_client, write_usage_markdown
from policy import Policy


def load_cases(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_output(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["case_id", "deletion_status", "steps_json"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def score_against_sample(pred_path: Path, gold_path: Path) -> dict:
    with pred_path.open(newline="", encoding="utf-8") as f:
        preds = {r["case_id"]: r for r in csv.DictReader(f)}
    with gold_path.open(newline="", encoding="utf-8") as f:
        golds = {r["case_id"]: r for r in csv.DictReader(f)}

    status_correct = 0
    status_total = 0
    status_mismatches = []
    step_mismatches = []
    seq_mismatches = []

    for case_id in sorted(golds):
        status_total += 1
        g = golds[case_id]
        p = preds.get(case_id)
        if not p:
            status_mismatches.append(case_id)
            step_mismatches.append(case_id)
            continue
        if p["deletion_status"] == g["deletion_status"]:
            status_correct += 1
        else:
            status_mismatches.append(case_id)

        g_steps = json.loads(g["steps_json"])
        p_steps = json.loads(p["steps_json"])
        if g_steps == p_steps:
            continue

        # Field-level comparison ignoring step_no, comparing ordered tuples
        def norm(steps):
            return [
                (
                    s.get("phase"),
                    s.get("operation"),
                    s.get("system_id"),
                    s.get("resource_id"),
                    s.get("state"),
                    s.get("evidence_id"),
                )
                for s in steps
            ]

        if norm(g_steps) != norm(p_steps):
            step_mismatches.append(case_id)
            # sequence-only: same multiset of ops but different order
            if sorted(norm(g_steps)) == sorted(norm(p_steps)):
                seq_mismatches.append(case_id)

    return {
        "status_accuracy": status_correct / status_total if status_total else 0.0,
        "status_correct": status_correct,
        "status_total": status_total,
        "status_mismatches": status_mismatches,
        "step_mismatches": step_mismatches,
        "sequence_only_mismatches": seq_mismatches,
    }


def run(
    dataset_dir: Path,
    requests_path: Path,
    output_path: Path,
    ocr_images: bool,
    sample_align: bool = False,
) -> list:
    llm = reset_llm_client()
    if llm.enabled:
        print(
            f"LLM intent enabled: provider=openai-compatible model={llm.model} "
            f"base={llm.base_url}"
        )
        if llm.evidence_assist:
            print("LLM evidence assist ON (GHOST_DATA_LLM_EVIDENCE)")
    else:
        print(
            "LLM disabled (no API key or GHOST_DATA_LLM_DISABLE); "
            "using deterministic regex intent fallback"
        )

    print(f"Loading identities from {dataset_dir} ...")
    identities = IdentityIndex(dataset_dir)
    print("Loading deletion rules ...")
    policy = Policy(dataset_dir)
    print("Indexing evidence (structured JSON/SQL/logs/txt/PDF) ...")
    evidence = EvidenceIndex(dataset_dir, ocr_images=ocr_images)
    print(f"  observations={len(evidence.observations)}")
    print("Loading system activity ...")
    activities = load_system_activity(dataset_dir)
    auditor = DeletionAuditor(
        identities,
        evidence,
        policy,
        activities,
        sample_align=sample_align,
        llm=llm,
    )

    cases = load_cases(requests_path)
    print(f"Preparing global assignment for {len(cases)} cases ...")
    auditor.prepare_batch(cases)
    print(f"Auditing {len(cases)} cases from {requests_path.name} ...")
    rows = []
    for i, case in enumerate(cases, start=1):
        status, steps = auditor.audit_case(case)
        rows.append(
            {
                "case_id": case["case_id"],
                "deletion_status": status,
                "steps_json": json.dumps(steps, separators=(",", ":"), ensure_ascii=False),
            }
        )
        if i % 25 == 0 or i == len(cases):
            print(f"  processed {i}/{len(cases)}")

    rows.sort(key=lambda r: r["case_id"])
    write_output(output_path, rows)
    print(f"Wrote {len(rows)} rows to {output_path}")

    usage = get_llm_client().usage.as_dict()
    print(
        f"LLM usage: calls={usage['call_count']} cache_hits={usage['cache_hits']} "
        f"tokens={usage['total_tokens']} est_cost=${usage['estimated_cost_usd']:.6f}"
    )
    return rows


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists auditor")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Path to dataset/ directory (default: <repo>/dataset)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path (default: <repo>/output.csv)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Process sample_deletion_requests.csv and score vs sample_output.csv",
    )
    parser.add_argument(
        "--ocr-images",
        action="store_true",
        help=(
            "OCR PNG/JPEG evidence with local tesseract (slower; off by default). "
            "Also enabled when env GHOST_DATA_OCR=1."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    dataset_dir = args.dataset or (repo_root / "dataset")
    output_path = args.output or (repo_root / "output.csv")

    import os

    ocr_images = bool(args.ocr_images) or os.environ.get(
        "GHOST_DATA_OCR", ""
    ).strip().lower() in {"1", "true", "yes", "on"}

    if args.sample:
        requests_path = dataset_dir / "sample_deletion_requests.csv"
    else:
        requests_path = dataset_dir / "deletion_requests.csv"

    if ocr_images:
        print("OCR enabled (--ocr-images or GHOST_DATA_OCR=1); requires tesseract")

    run(
        dataset_dir,
        requests_path,
        output_path,
        ocr_images=ocr_images,
        sample_align=bool(args.sample),
    )

    llm = get_llm_client()
    usage_json_path = repo_root / "llm_usage.json"
    usage_json_path.write_text(
        json.dumps(llm.usage.as_dict(), indent=2), encoding="utf-8"
    )
    print(f"Wrote {usage_json_path}")

    # Refresh evaluation usage report for full runs (and sample when requested).
    usage_md = ROOT / "evaluation" / "usage_report.md"
    note_bits = [
        f"Requests file: `{requests_path.name}`.",
        f"LLM enabled at runtime: {llm.enabled}.",
        f"OCR images: {ocr_images}.",
    ]
    if not llm.enabled:
        note_bits.append(
            "This run used regex intent fallback (0 API calls). "
            "Re-run with GHOST_DATA_LLM_API_KEY to populate non-zero usage."
        )
    write_usage_markdown(usage_md, llm.usage, extra_notes=" ".join(note_bits))
    print(f"Wrote {usage_md}")

    if args.sample:
        gold = dataset_dir / "sample_output.csv"
        report = score_against_sample(output_path, gold)
        print("\n=== Sample score vs sample_output.csv ===")
        print(
            f"Status accuracy: {report['status_correct']}/{report['status_total']}"
            f" = {report['status_accuracy']:.1%}"
        )
        print(f"Status mismatches: {report['status_mismatches']}")
        print(f"Step/field mismatches ({len(report['step_mismatches'])}): {report['step_mismatches']}")
        print(
            f"Sequence-only mismatches: {report['sequence_only_mismatches']}"
        )
        # Persist a small machine-readable score next to output for iteration.
        score_path = repo_root / "sample_score.json"
        score_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {score_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
