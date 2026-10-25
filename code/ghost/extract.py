"""Multimodal evidence extraction: screenshots and PDFs -> structured records via Claude vision.

Run once (``python3 code/main.py --extract``); results are cached in code/cache/evidence_extractions.json
and consumed by the loaders, so the audit itself never re-reads images.
"""
from __future__ import annotations

import csv
import json

from .llm import VISION_MODEL, image_block, pdf_block, structured_call
from .util import CACHE_DIR, DATASET_DIR

CACHE_PATH = CACHE_DIR / "evidence_extractions.json"
SYSTEMS = ["account_service", "search_service", "analytics_platform", "support_platform", "export_service",
           "integration_service", "backup_service", "security_service", "identity_service", "legal_service",
           "marketing_platform", "object_storage"]
CATEGORIES = ["USER_PROFILE", "SEARCH_INDEX_ENTRY", "ANALYTICS_PROFILE", "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT",
              "USER_EXPORT", "DATA_FLOW", "BACKUP_ARCHIVE", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD",
              "ACCOUNT_TOMBSTONE", "MARKETING_PROFILE", "CACHE_ENTRY", "EMAIL_DELIVERY_LOG"]
SYSTEM_PROMPT = (
    "You extract facts from internal business artifacts (admin-console screenshots, statements, registers) of an "
    "online shop for a data-deletion audit. Report every customer data record the artifact shows. Copy identifiers "
    "exactly as displayed (resource ids such as account_5762, user ids such as user_11, emails, phone numbers). "
    "When no resource id is shown, use the file name or document reference that identifies the record. "
    "role is 'detail' for the record a detail panel or the whole document focuses on and 'row' for other list rows. "
    "present: the record exists in the system shown. exposed: direct identifiers (name, email, phone, email hash) are "
    "visible on the record. active: for data flows, the connector is enabled/syncing; for backups, restore is not "
    "blocked. Use null for anything the artifact does not establish. Do not infer deletion verdicts."
)
FIELD_PROPS = {k: {"type": ["string", "null"]} for k in
               ("full_name", "email", "phone", "email_hash", "customer_reference", "subject_reference")}
RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["detail", "row"]},
        "system_id": {"type": "string", "enum": SYSTEMS},
        "resource_id": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "present": {"type": ["boolean", "null"]},
        "exposed": {"type": ["boolean", "null"]},
        "active": {"type": ["boolean", "null"]},
        "observed_at": {"type": ["string", "null"]},
        "fields": {"type": "object", "properties": FIELD_PROPS, "required": list(FIELD_PROPS),
                   "additionalProperties": False},
        "note": {"type": "string"},
    },
    "required": ["role", "system_id", "resource_id", "category", "present", "exposed", "active", "observed_at",
                 "fields", "note"],
    "additionalProperties": False,
}
SCHEMA = {"type": "object", "properties": {"records": {"type": "array", "items": RECORD_SCHEMA}},
          "required": ["records"], "additionalProperties": False}


def extract_all(force: bool = False) -> dict:
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    with open(DATASET_DIR / "evidence_files.csv", encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    changed = False
    for row in manifest:
        fid, path = row["file_id"], DATASET_DIR / row["file_path"]
        if path.suffix.lower() not in (".png", ".pdf") or (fid in cache and not force):
            continue
        block = image_block(path) if path.suffix.lower() == ".png" else pdf_block(path)
        prompt = (f"Artifact {fid} from {row['origin']}, captured {row['captured_at']}. "
                  "Extract the customer data records it shows.")
        result = structured_call(VISION_MODEL, "evidence_extraction", [block, {"type": "text", "text": prompt}],
                                 SCHEMA, SYSTEM_PROMPT, max_tokens=8000, effort="medium")
        if result is None:
            print(f"[extract] {fid}: no result (offline or API error); keeping cache as is")
            continue
        for r in result["records"]:
            r["fields"] = {k: v for k, v in r["fields"].items() if v is not None}
            for k in ("present", "exposed", "active", "observed_at"):
                if r.get(k) is None:
                    r.pop(k, None)
        cache[fid] = {"source": VISION_MODEL, "records": result["records"]}
        changed = True
        print(f"[extract] {fid}: {len(result['records'])} records")
    if changed:
        CACHE_PATH.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return cache
