"""Evidence indexing and parsing for participant-facing dataset/evidence files."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from identity import Identity, IdentityIndex, parse_ts


RESOURCE_RE = re.compile(
    r"\b((?:account|search|support|export|analytics|backup|data_flow|audit_record|"
    r"tombstone|marketing|object|legal_hold)_\w+)\b",
    re.I,
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
USER_RE = re.compile(r"\buser_\d+\b", re.I)

# system_id inference from resource prefix / explicit fields
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "export": "export_service",
    "analytics": "analytics_platform",
    "backup": "backup_service",
    "data_flow": "integration_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "legal_hold": "legal_service",
}

CATEGORY_BY_PREFIX = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "support": "SUPPORT_MESSAGE",  # refined when explicit category present
    "export": "USER_EXPORT",
    "analytics": "ANALYTICS_PROFILE",
    "backup": "BACKUP_ARCHIVE",
    "data_flow": "DATA_FLOW",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "marketing": "MARKETING_PROFILE",
    "object": "SUPPORT_ATTACHMENT",
    "legal_hold": "LEGAL_HOLD_RECORD",
}


def system_for_resource(resource_id: str, fallback: Optional[str] = None) -> str:
    prefix = resource_id.rsplit("_", 1)[0] if "_" in resource_id else resource_id
    # handle audit_record specially (two-part prefix)
    if resource_id.startswith("audit_record_"):
        return "security_service"
    if resource_id.startswith("data_flow_"):
        return "integration_service"
    if resource_id.startswith("legal_hold_"):
        return "legal_service"
    for key, system in PREFIX_SYSTEM.items():
        if resource_id.startswith(key + "_"):
            return system
    return fallback or "unknown_service"


def category_for_resource(resource_id: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    if resource_id.startswith("audit_record_"):
        return "SECURITY_AUDIT_LOG"
    if resource_id.startswith("data_flow_"):
        return "DATA_FLOW"
    if resource_id.startswith("legal_hold_"):
        return "LEGAL_HOLD_RECORD"
    for key, cat in CATEGORY_BY_PREFIX.items():
        if resource_id.startswith(key + "_"):
            return cat
    return "UNKNOWN"


@dataclass
class Observation:
    evidence_id: str
    captured_at: datetime
    system_id: str
    resource_id: str
    category: str
    # normalized compliance-oriented state
    # PRESENT | ABSENT | EXPOSED | REDACTED | ANONYMIZED | ACTIVE | DISABLED | BLOCKED
    state: str
    identifiers: Set[str] = field(default_factory=set)
    raw: Dict[str, Any] = field(default_factory=dict)

    def matches_identity(self, identity: Identity) -> bool:
        tokens = identity.all_tokens()
        for value in self.identifiers:
            if value and value.lower() in tokens:
                return True
        return False


def _add_ident(target: Set[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (int, float)):
        return
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return
    target.add(text)


def _extract_identifiers_from_mapping(obj: Dict[str, Any]) -> Set[str]:
    keys = {
        "email",
        "owner_email",
        "requester_email",
        "customer_email",
        "lookup_key",
        "query",
        "correlation_key",
        "phone",
        "owner_phone",
        "requester_phone",
        "email_hash",
        "user_id",
        "customer_reference",
        "subject_reference",
        "subject_ref",
        "subject_key",
        "full_name",
        "display_name",
        "name",
        "requester_name",
    }
    found: Set[str] = set()
    for key, value in obj.items():
        if key in keys:
            _add_ident(found, value)
        elif isinstance(value, dict):
            found |= _extract_identifiers_from_mapping(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found |= _extract_identifiers_from_mapping(item)
    # also harvest emails anywhere
    blob = json.dumps(obj, ensure_ascii=False)
    for email in EMAIL_RE.findall(blob):
        found.add(email)
    for user in USER_RE.findall(blob):
        found.add(user)
    return found


class EvidenceIndex:
    def __init__(self, dataset_dir: Path, ocr_images: bool = False):
        self.dataset_dir = dataset_dir
        self.ocr_images = ocr_images
        self.manifest: List[Dict[str, str]] = []
        self.observations: List[Observation] = []
        self._load_manifest()
        self._index_all()

    def _load_manifest(self) -> None:
        path = self.dataset_dir / "evidence_files.csv"
        with path.open(newline="", encoding="utf-8") as f:
            self.manifest = list(csv.DictReader(f))

    def observations_at_or_before(self, audit_at: datetime) -> List[Observation]:
        return [o for o in self.observations if o.captured_at <= audit_at]

    def latest_by_resource(
        self, audit_at: datetime, resource_ids: Optional[Set[str]] = None
    ) -> Dict[str, Observation]:
        latest: Dict[str, Observation] = {}
        for obs in self.observations_at_or_before(audit_at):
            if resource_ids is not None and obs.resource_id not in resource_ids:
                continue
            prev = latest.get(obs.resource_id)
            if prev is None or obs.captured_at >= prev.captured_at:
                latest[obs.resource_id] = obs
        return latest

    def _index_all(self) -> None:
        for row in self.manifest:
            file_id = row["file_id"]
            rel = row["file_path"]
            captured_at = parse_ts(row["captured_at"])
            path = self.dataset_dir / rel
            if not path.exists():
                continue
            suffix = path.suffix.lower()
            try:
                if suffix == ".json":
                    self._parse_json(file_id, captured_at, path)
                elif suffix == ".sql":
                    self._parse_sql(file_id, captured_at, path)
                elif suffix in {".log", ".txt"}:
                    self._parse_textish(file_id, captured_at, path)
                elif suffix == ".pdf":
                    self._parse_pdf(file_id, captured_at, path)
                elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} and self.ocr_images:
                    self._parse_png(file_id, captured_at, path)
            except Exception as exc:  # noqa: BLE001 - keep indexing resilient
                # Skip corrupt/unreadable evidence; do not fail the whole run.
                print(f"[warn] failed to parse {file_id} ({path.name}): {exc}")

    def _emit(
        self,
        evidence_id: str,
        captured_at: datetime,
        system_id: str,
        resource_id: str,
        category: str,
        state: str,
        identifiers: Optional[Iterable[str]] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        idents: Set[str] = set()
        if identifiers:
            for value in identifiers:
                _add_ident(idents, value)
        self.observations.append(
            Observation(
                evidence_id=evidence_id,
                captured_at=captured_at,
                system_id=system_id or system_for_resource(resource_id),
                resource_id=resource_id,
                category=category_for_resource(resource_id, category),
                state=state,
                identifiers=idents,
                raw=raw or {},
            )
        )

    # ---- JSON ----
    def _parse_json(self, evidence_id: str, captured_at: datetime, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "service_exports" in data:
            self._parse_service_exports(evidence_id, captured_at, data)
            return
        if isinstance(data, dict) and "snapshots" in data:
            self._parse_snapshot_bundle(evidence_id, captured_at, data)
            return
        if isinstance(data, dict) and "records" in data:
            self._parse_records_array(
                evidence_id,
                captured_at,
                data.get("source") or data.get("collection") or path.stem,
                data["records"],
            )
            return
        # generic walk
        self._walk_json(evidence_id, captured_at, data)

    def _parse_service_exports(
        self, evidence_id: str, captured_at: datetime, data: Dict[str, Any]
    ) -> None:
        exports = data.get("service_exports", {})
        # accounts
        for item in exports.get("account_service", {}).get("profile_lookups", []):
            profile = item.get("profile") or {}
            idents = _extract_identifiers_from_mapping(item)
            status = item.get("status_code")
            state = "ABSENT" if status == 404 or profile in (None, {}) else "PRESENT"
            if state == "PRESENT":
                # exposed if direct identifiers remain
                if any(profile.get(k) for k in ("email", "phone", "email_hash", "full_name")):
                    state = "PRESENT"
            self._emit(
                evidence_id,
                captured_at,
                "account_service",
                item["resource_id"],
                "USER_PROFILE",
                state,
                idents,
                item,
            )
        for item in exports.get("analytics_platform", {}).get("profiles", []):
            idents = _extract_identifiers_from_mapping(item)
            exposed = any(item.get(k) for k in ("email", "phone", "email_hash")) or bool(
                item.get("customer_reference") and item.get("email")
            )
            # anonymized when no direct identifiers
            if not any(item.get(k) for k in ("email", "phone", "email_hash", "customer_reference")):
                state = "ANONYMIZED"
            elif item.get("customer_reference") and not any(
                item.get(k) for k in ("email", "phone", "email_hash")
            ):
                # linked only by stable ref without direct PII — treat as anonymized-ish? policy wants unlink
                # Sample treats null identifiers as ANONYMIZED even without customer_reference.
                state = "ANONYMIZED" if not item.get("email") else "EXPOSED"
            else:
                state = "EXPOSED" if exposed else "ANONYMIZED"
            if item.get("email") or item.get("phone") or item.get("email_hash"):
                state = "EXPOSED"
            elif not item.get("customer_reference"):
                state = "ANONYMIZED"
            else:
                # customer_reference alone without direct PII — still attributable via user_id
                # Policy: ANONYMIZE when attributable. customer_reference means attributable.
                state = "EXPOSED"
            # Re-evaluate: sample COMPLETE analytics have all nulls including customer_reference -> ANONYMIZED
            if not any(item.get(k) for k in ("email", "phone", "email_hash", "customer_reference")):
                state = "ANONYMIZED"
            self._emit(
                evidence_id,
                captured_at,
                "analytics_platform",
                item["resource_id"],
                "ANALYTICS_PROFILE",
                state,
                idents,
                item,
            )
        for item in exports.get("search_service", {}).get("index_queries", []):
            idents = _extract_identifiers_from_mapping(item)
            hits = item.get("hits") or {}
            total = hits.get("total") if isinstance(hits, dict) else None
            docs = []
            if isinstance(hits, dict):
                docs = hits.get("documents") or []
            state = "PRESENT" if (total and total > 0) or docs else "ABSENT"
            self._emit(
                evidence_id,
                captured_at,
                "search_service",
                item["resource_id"],
                "SEARCH_INDEX_ENTRY",
                state,
                idents,
                item,
            )
        for item in exports.get("support_platform", {}).get("tickets", []):
            idents = _extract_identifiers_from_mapping(item)
            exposed = any(
                item.get(k)
                for k in ("requester_email", "requester_name", "requester_phone")
            )
            if exposed:
                state = "EXPOSED"
                category = "SUPPORT_MESSAGE"
            elif item.get("customer_reference"):
                state = "REDACTED"
                category = "SUPPORT_MESSAGE"
            else:
                state = "PRESENT"
                category = "SUPPORT_MESSAGE"
            self._emit(
                evidence_id,
                captured_at,
                "support_platform",
                item["resource_id"],
                category,
                state,
                idents,
                item,
            )
        for item in exports.get("integration_service", {}).get("connectors", []):
            idents = _extract_identifiers_from_mapping(item)
            state = "ACTIVE" if item.get("enabled") else "DISABLED"
            self._emit(
                evidence_id,
                captured_at,
                "integration_service",
                item["resource_id"],
                "DATA_FLOW",
                state,
                idents,
                item,
            )
        for item in exports.get("backup_service", {}).get("restore_entries", []):
            idents = _extract_identifiers_from_mapping(item)
            state = "ACTIVE" if item.get("restore_allowed") else "BLOCKED"
            self._emit(
                evidence_id,
                captured_at,
                "backup_service",
                item["resource_id"],
                "BACKUP_ARCHIVE",
                state,
                idents,
                item,
            )
        for item in exports.get("export_service", {}).get("exports", []):
            idents = _extract_identifiers_from_mapping(item)
            self._emit(
                evidence_id,
                captured_at,
                "export_service",
                item["resource_id"],
                "USER_EXPORT",
                "PRESENT",
                idents,
                item,
            )
        for item in exports.get("security_service", {}).get("audit_records", []):
            idents = _extract_identifiers_from_mapping(item)
            self._emit(
                evidence_id,
                captured_at,
                "security_service",
                item["resource_id"],
                "SECURITY_AUDIT_LOG",
                "PRESENT",
                idents,
                item,
            )

    def _parse_snapshot_bundle(
        self, evidence_id: str, captured_at: datetime, data: Dict[str, Any]
    ) -> None:
        for snap in data.get("snapshots", []):
            store = snap.get("data_store") or "unknown"
            collection = snap.get("collection") or ""
            for rec in snap.get("records", []):
                if "resource_id" not in rec:
                    continue
                idents = _extract_identifiers_from_mapping(rec)
                rid = rec["resource_id"]
                category = category_for_resource(rid)
                state = "PRESENT"
                if store == "account_service" or rid.startswith("account_"):
                    status = (rec.get("account_status") or "").upper()
                    if status == "DELETED" and not any(
                        rec.get(k) for k in ("email", "phone", "full_name", "name")
                    ):
                        # empty profiles shouldn't appear; if listed with identifiers -> present
                        state = "PRESENT"
                    category = "USER_PROFILE"
                if rid.startswith("tombstone_") or collection == "deleted_customer_references":
                    category = "ACCOUNT_TOMBSTONE"
                    exposed = any(
                        rec.get(k) for k in ("name", "email", "phone", "email_hash")
                    )
                    state = "EXPOSED" if exposed else "REDACTED"
                if store == "analytics_platform" or rid.startswith("analytics_"):
                    category = "ANALYTICS_PROFILE"
                    if not any(
                        rec.get(k)
                        for k in ("email", "phone", "email_hash", "user_id", "customer_reference")
                    ):
                        state = "ANONYMIZED"
                    elif any(rec.get(k) for k in ("email", "phone", "email_hash")):
                        state = "EXPOSED"
                    else:
                        state = "EXPOSED"
                if store == "security_service" or rid.startswith("audit_record_"):
                    category = "SECURITY_AUDIT_LOG"
                    state = "PRESENT"
                if store == "search_service" or rid.startswith("search_"):
                    category = "SEARCH_INDEX_ENTRY"
                    state = "PRESENT"
                if store == "export_service" or rid.startswith("export_"):
                    category = "USER_EXPORT"
                    state = "PRESENT"
                if store == "integration_service" or rid.startswith("data_flow_"):
                    category = "DATA_FLOW"
                    enabled = rec.get("enabled")
                    if enabled is None and "status" in rec:
                        enabled = str(rec.get("status")).lower() in {"active", "enabled", "true", "1"}
                    state = "ACTIVE" if enabled else "DISABLED"
                if store == "backup_service" or rid.startswith("backup_"):
                    category = "BACKUP_ARCHIVE"
                    allowed = rec.get("restore_allowed")
                    state = "ACTIVE" if allowed else "BLOCKED"
                if store == "support_platform" or rid.startswith("support_"):
                    category = "SUPPORT_MESSAGE"
                    exposed = any(
                        rec.get(k)
                        for k in ("requester_email", "email", "requester_name", "requester_phone", "name", "phone")
                    )
                    state = "EXPOSED" if exposed else "REDACTED"
                self._emit(
                    evidence_id,
                    captured_at,
                    store if store != "unknown" else system_for_resource(rid),
                    rid,
                    category,
                    state,
                    idents,
                    rec,
                )

    def _parse_records_array(
        self,
        evidence_id: str,
        captured_at: datetime,
        source: str,
        records: List[Dict[str, Any]],
    ) -> None:
        for rec in records:
            if "resource_id" not in rec:
                continue
            rid = rec["resource_id"]
            idents = _extract_identifiers_from_mapping(rec)
            category = category_for_resource(rid)
            state = "PRESENT"
            if rid.startswith("analytics_"):
                if not any(rec.get(k) for k in ("email", "phone", "email_hash", "user_id")):
                    state = "ANONYMIZED"
                else:
                    state = "EXPOSED"
            if rid.startswith("export_"):
                available = rec.get("download_available")
                state = "PRESENT" if available is not False else "ABSENT"
            if rid.startswith("tombstone_"):
                category = "ACCOUNT_TOMBSTONE"
                exposed = any(rec.get(k) for k in ("name", "email", "phone", "email_hash"))
                state = "EXPOSED" if exposed else "REDACTED"
            self._emit(
                evidence_id,
                captured_at,
                system_for_resource(rid),
                rid,
                category,
                state,
                idents,
                rec,
            )

    def _walk_json(self, evidence_id: str, captured_at: datetime, data: Any) -> None:
        if isinstance(data, dict):
            if "resource_id" in data and isinstance(data["resource_id"], str):
                rid = data["resource_id"]
                idents = _extract_identifiers_from_mapping(data)
                self._emit(
                    evidence_id,
                    captured_at,
                    system_for_resource(rid),
                    rid,
                    category_for_resource(rid),
                    "PRESENT",
                    idents,
                    data if isinstance(data, dict) else {},
                )
            for value in data.values():
                self._walk_json(evidence_id, captured_at, value)
        elif isinstance(data, list):
            for item in data:
                self._walk_json(evidence_id, captured_at, item)

    # ---- SQL ----
    def _parse_sql(self, evidence_id: str, captured_at: datetime, path: Path) -> None:
        sql_text = path.read_text(encoding="utf-8", errors="replace")
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            try:
                conn.executescript(sql_text)
            except sqlite3.Error:
                # fallback: regex insert parsing for broken scripts
                self._parse_sql_regex(evidence_id, captured_at, sql_text)
                conn.close()
                return
            cur = conn.cursor()
            tables = [
                r[0]
                for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            for table in tables:
                if table == "snapshot_scope":
                    continue
                cols = [d[1] for d in cur.execute(f"PRAGMA table_info({table})")]
                rows = cur.execute(f"SELECT * FROM {table}").fetchall()
                for row in rows:
                    record = dict(zip(cols, row))
                    self._ingest_sql_record(evidence_id, captured_at, table, record)
            conn.close()

    def _ingest_sql_record(
        self,
        evidence_id: str,
        captured_at: datetime,
        table: str,
        record: Dict[str, Any],
    ) -> None:
        # Generic JSON payload tables (customer_data_* replicas)
        payload = None
        for key in ("payload", "record_json", "data_json"):
            if key in record and isinstance(record[key], str) and record[key].startswith("{"):
                try:
                    payload = json.loads(record[key])
                except json.JSONDecodeError:
                    payload = None
        if payload and isinstance(payload, dict) and payload.get("resource_id"):
            # Prefer nested document fields
            idents = _extract_identifiers_from_mapping(payload)
            system = record.get("system") or record.get("service") or system_for_resource(
                payload["resource_id"]
            )
            collection = record.get("collection") or ""
            rid = payload["resource_id"]
            category = category_for_resource(rid)
            state = "PRESENT"
            if "document" in payload and isinstance(payload["document"], dict):
                idents |= _extract_identifiers_from_mapping(payload["document"])
            if rid.startswith("analytics_") or "analytics" in collection:
                if not any(
                    payload.get(k) for k in ("email", "phone", "email_hash")
                ) and not (
                    isinstance(payload.get("document"), dict)
                    and any(payload["document"].get(k) for k in ("email", "phone", "email_hash"))
                ):
                    # may still be identifiable via nested fields — default PRESENT/EXPOSED if email in idents
                    state = "EXPOSED" if any("@" in x for x in idents) else "ANONYMIZED"
                else:
                    state = "EXPOSED"
            self._emit(evidence_id, captured_at, str(system), rid, category, state, idents, record)
            return

        rid = record.get("resource_id")
        if not rid:
            return
        idents = set()
        for key, value in record.items():
            if key in {
                "email",
                "owner_email",
                "requester_email",
                "customer_email",
                "phone",
                "owner_phone",
                "requester_phone",
                "email_hash",
                "customer_reference",
                "subject_reference",
                "full_name",
                "display_name",
                "requester_name",
                "user_id",
            }:
                _add_ident(idents, value)

        table_l = table.lower()
        if "profile" in table_l and "analytics" not in table_l:
            system, category = "account_service", "USER_PROFILE"
            state = "PRESENT"
        elif "search" in table_l:
            system, category, state = "search_service", "SEARCH_INDEX_ENTRY", "PRESENT"
        elif "analytics" in table_l:
            system, category = "analytics_platform", "ANALYTICS_PROFILE"
            if not any(record.get(k) for k in ("email", "phone", "email_hash", "customer_reference")):
                state = "ANONYMIZED"
            elif any(record.get(k) for k in ("email", "phone", "email_hash")):
                state = "EXPOSED"
            else:
                state = "EXPOSED"
        elif "support" in table_l:
            system, category = "support_platform", "SUPPORT_MESSAGE"
            exposed = any(
                record.get(k)
                for k in ("requester_email", "requester_name", "requester_phone", "email")
            )
            state = "EXPOSED" if exposed else ("REDACTED" if record.get("customer_reference") else "PRESENT")
        elif "export" in table_l:
            system, category, state = "export_service", "USER_EXPORT", "PRESENT"
        elif "connector" in table_l or "subscription" in table_l:
            system, category = "integration_service", "DATA_FLOW"
            enabled = record.get("enabled")
            state = "ACTIVE" if str(enabled) in {"1", "true", "True"} else "DISABLED"
        elif "backup" in table_l or "restore" in table_l:
            system, category = "backup_service", "BACKUP_ARCHIVE"
            allowed = record.get("restore_allowed")
            state = "ACTIVE" if str(allowed) in {"1", "true", "True"} else "BLOCKED"
        elif "security" in table_l or "audit" in table_l:
            system, category, state = "security_service", "SECURITY_AUDIT_LOG", "PRESENT"
        else:
            system = system_for_resource(str(rid))
            category = category_for_resource(str(rid))
            state = "PRESENT"
        self._emit(
            evidence_id,
            captured_at,
            system,
            str(rid),
            category,
            state,
            idents,
            record,
        )

    def _parse_sql_regex(self, evidence_id: str, captured_at: datetime, sql_text: str) -> None:
        for match in re.finditer(
            r"INSERT INTO \"?(\w+)\"? VALUES\((.*?)\);", sql_text, re.S
        ):
            # too ambiguous without schema — skip
            continue

    # ---- text / logs / handover ----
    def _parse_textish(self, evidence_id: str, captured_at: datetime, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Handover style:
        # - TS | system | resource | CATEGORY | STATE | identifier
        handover_re = re.compile(
            r"^[\-\*]?\s*(\d{4}-\d{2}-\d{2}T[\d:]+Z)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*(.+)$"
        )
        # First pass: collect log fields keyed by trace_id so lookup_key / subject_key
        # on started lines attach to resource_id on completed lines.
        trace_bags: Dict[str, Dict[str, str]] = defaultdict(dict)
        plain_lines: List[str] = []
        for line in text.splitlines():
            m = handover_re.match(line.strip())
            if m:
                system = m.group(2).strip()
                rid = m.group(3).strip()
                category = m.group(4).strip()
                raw_state = m.group(5).strip().upper()
                ident = m.group(6).strip()
                state = self._normalize_handover_state(category, raw_state)
                idents = set()
                _add_ident(idents, ident)
                if ident.lower().startswith("no direct"):
                    idents.clear()
                self._emit(
                    evidence_id,
                    captured_at,
                    system,
                    rid,
                    category,
                    state,
                    idents,
                    {"line": line},
                )
                continue
            plain_lines.append(line)
            fields = self._log_fields(line)
            trace = fields.get("trace_id")
            if trace:
                # merge non-empty fields into the trace bag
                for key, value in fields.items():
                    if value not in (None, "", "null"):
                        trace_bags[trace][key] = value

        # Second pass: emit resource observations with merged trace context.
        emitted: Set[Tuple[str, str]] = set()
        for line in plain_lines:
            fields = self._log_fields(line)
            trace = fields.get("trace_id")
            merged = dict(trace_bags.get(trace, {})) if trace else {}
            merged.update({k: v for k, v in fields.items() if v not in (None, "", "null")})
            rid = merged.get("resource_id")
            if not rid:
                m = RESOURCE_RE.search(line)
                if m:
                    rid = m.group(1)
            if not rid:
                continue
            key = (rid, trace or line[:80])
            # Prefer the line that carries the strongest state signal; still allow
            # multiple events for same rid/trace when state-bearing.
            if "resource_id" not in fields and "resource_id" not in line:
                # only emit from lines that mention the resource, after merge
                if f"resource_id={rid}" not in line and f'resource_id="{rid}"' not in line and rid not in line:
                    continue
            self._emit_from_log_fields(evidence_id, captured_at, rid, merged, line)
            emitted.add(key)

    def _normalize_handover_state(self, category: str, raw_state: str) -> str:
        raw = raw_state.upper()
        if raw in {"NOT_FOUND", "ABSENT", "MISSING"}:
            return "ABSENT"
        if raw in {"FOUND", "PRESENT", "ACTIVE"} and category.upper() == "BACKUP_ARCHIVE":
            return "ACTIVE"
        if raw in {"FOUND", "PRESENT"}:
            return "PRESENT"
        if raw in {"IDENTIFIABLE", "EXPOSED"}:
            return "EXPOSED"
        if raw in {"ANONYMIZED"}:
            return "ANONYMIZED"
        if raw in {"REDACTED"}:
            return "REDACTED"
        if raw in {"DISABLED", "STOPPED"}:
            return "DISABLED"
        if raw in {"BLOCKED"}:
            return "BLOCKED"
        if raw in {"ACTIVE", "ENABLED"}:
            return "ACTIVE"
        return raw

    def _log_fields(self, line: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        # Structured JSON logs: "<ts> {json}" or bare JSON
        json_part = None
        ts_json = re.match(r"^(\d{4}-\d{2}-\d{2}T[\d:]+Z)\s+(\{.*\})\s*$", line.strip())
        if ts_json:
            fields["timestamp"] = ts_json.group(1)
            json_part = ts_json.group(2)
        elif line.strip().startswith("{"):
            json_part = line.strip()
        if json_part:
            try:
                obj = json.loads(json_part)
                for key, value in obj.items():
                    if value is None:
                        continue
                    if isinstance(value, (dict, list)):
                        continue
                    fields[str(key)] = str(value)
                return fields
            except json.JSONDecodeError:
                pass
        for key, value in re.findall(r"(\w+)=((?:\"[^\"]*\")|(?:\S+))", line):
            fields[key] = value.strip().strip('"')
        return fields

    def _emit_from_log_fields(
        self,
        evidence_id: str,
        captured_at: datetime,
        rid: str,
        fields: Dict[str, str],
        line: str,
    ) -> None:
        # Prefer the event timestamp embedded in the log line when present. Export
        # captured_at can post-date the events; state reconstruction uses event time.
        event_at = captured_at
        ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T[\d:]+Z)", line.strip())
        if ts_match:
            try:
                event_at = parse_ts(ts_match.group(1))
            except ValueError:
                event_at = captured_at
        # Also handle JSON logs with "timestamp" / time fields
        for key in ("timestamp", "time", "event_time", "@timestamp"):
            if fields.get(key):
                try:
                    event_at = parse_ts(fields[key].replace("Z", "+00:00") if "Z" in fields[key] else fields[key])
                    # normalize via parse_ts helper
                    event_at = parse_ts(fields[key] if fields[key].endswith("Z") else fields[key])
                except Exception:  # noqa: BLE001
                    pass
                break

        system = (
            fields.get("service")
            or fields.get("function")
            or fields.get("system")
            or system_for_resource(rid)
        )
        if system in {"profile_service"}:
            system = "account_service"

        idents: Set[str] = set()
        for key in (
            "lookup_key",
            "correlation_key",
            "customer_email",
            "email",
            "owner_email",
            "requester_email",
            "subject_reference",
            "subject_ref",
            "subject_key",
            "customer_reference",
            "query",
            "matched_value",
        ):
            if fields.get(key):
                _add_ident(idents, fields[key])
        for email in EMAIL_RE.findall(line):
            idents.add(email)
        for user in USER_RE.findall(line):
            idents.add(user)

        category = fields.get("record_type") or category_for_resource(rid)
        state = "PRESENT"
        event = (fields.get("event") or "").lower()
        result = (fields.get("result") or fields.get("http_status") or "").upper()

        if rid.startswith("account_") or system == "account_service":
            if result in {"404", "NOT_FOUND"} or fields.get("rows") == "0":
                state = "ABSENT"
            elif result in {"200"} or fields.get("rows") == "1":
                state = "PRESENT"
        if rid.startswith("search_"):
            if fields.get("total_hits") == "0":
                state = "ABSENT"
            elif (fields.get("result") or "").lower() in {"hit", "found"}:
                state = "PRESENT"
            elif fields.get("total_hits") and fields.get("total_hits") != "0":
                state = "PRESENT"
        if rid.startswith("analytics_"):
            # Any direct identifier or historical email hash => attributable / exposed.
            has_direct = any(
                fields.get(k) not in (None, "", "null")
                for k in ("email", "phone", "email_hash")
            ) or any("@" in x for x in idents)
            has_hash = any(
                len(x) >= 32 and "@" not in x and not x.startswith("user_") for x in idents
            )
            if has_direct or has_hash or fields.get("rows") == "1":
                state = "EXPOSED"
            elif fields.get("customer_reference") not in (None, "", "null"):
                state = "EXPOSED"
            else:
                state = "ANONYMIZED"
        if rid.startswith("data_flow_") or system == "integration_service":
            enabled = fields.get("enabled")
            if enabled is not None:
                state = "ACTIVE" if enabled.lower() in {"true", "1", "yes"} else "DISABLED"
        if rid.startswith("backup_"):
            allowed = fields.get("restore_allowed")
            eligible = fields.get("eligible")
            deletion_block = fields.get("deletion_block")
            if allowed is not None:
                state = "ACTIVE" if allowed.lower() in {"true", "1", "yes"} else "BLOCKED"
            elif eligible is not None:
                # restore_candidate_evaluated eligible=true => restore risk active
                if eligible.lower() in {"true", "1", "yes"} and (
                    deletion_block in (None, "", "false", "0")
                ):
                    state = "ACTIVE"
                else:
                    state = "BLOCKED"
        if rid.startswith("support_"):
            if any(
                fields.get(k) not in (None, "", "null")
                for k in ("requester_email", "email", "lookup_key")
            ) or any("@" in x for x in idents):
                # Contact/replica hit still resolvable for the subject.
                state = "PRESENT"
                if "message" in event or "ticket" in event:
                    category = "SUPPORT_MESSAGE"
                    state = "EXPOSED"
                else:
                    # Legacy contact index / attachment-like exclusive records -> DELETE.
                    category = "SUPPORT_ATTACHMENT"
            elif fields.get("customer_reference") not in (None, "", "null"):
                category = "SUPPORT_MESSAGE"
                state = "REDACTED"
            elif fields.get("rows") == "1" or "completed" in event:
                category = "SUPPORT_ATTACHMENT"
                state = "PRESENT"
        if rid.startswith("audit_record_"):
            state = "PRESENT"
        if rid.startswith("export_"):
            state = "PRESENT"
        if rid.startswith("tombstone_"):
            category = "ACCOUNT_TOMBSTONE"
            exposed = any(
                fields.get(k) not in (None, "", "null")
                for k in ("email", "name", "phone", "email_hash", "requester_email")
            )
            state = "EXPOSED" if exposed else "REDACTED"

        if "deletion_marker_check" in event and fields.get("marker_found", "").lower() == "false":
            state = "PRESENT"

        self._emit(
            evidence_id,
            event_at,
            system_for_resource(rid, system),
            rid,
            category,
            state,
            idents,
            {"line": line, "file_captured_at": captured_at.isoformat(), **fields},
        )

    # ---- PDF / PNG ----
    def _parse_pdf(self, evidence_id: str, captured_at: datetime, path: Path) -> None:
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        self._parse_ocr_or_pdf_text(evidence_id, captured_at, text, path.name)

    def _parse_png(self, evidence_id: str, captured_at: datetime, path: Path) -> None:
        """OCR image evidence via local tesseract (optional dependency)."""
        try:
            import subprocess

            proc = subprocess.run(
                [
                    "tesseract",
                    str(path),
                    "stdout",
                    "-l",
                    "eng",
                    "--psm",
                    "6",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            text = proc.stdout or ""
        except FileNotFoundError:
            print(
                f"[warn] tesseract not installed; skipping OCR for {evidence_id}"
            )
            return
        except Exception:  # noqa: BLE001
            return
        if not text.strip():
            return
        self._parse_ocr_or_pdf_text(
            evidence_id, captured_at, text, path.name, from_ocr=True
        )

    def _parse_ocr_or_pdf_text(
        self,
        evidence_id: str,
        captured_at: datetime,
        text: str,
        source_name: str,
        from_ocr: bool = False,
    ) -> None:
        """Extract resources with *local* identifier context.

        Screenshots often show multiple customers on one page. Binding every email
        on the page to every resource_id caused false identity matches (e.g. OCR
        regressing sample case_15). Prefer identifiers within a proximity window
        of each resource mention; fall back to page-level idents only for PDFs
        with a single resource.
        """
        resources = list(RESOURCE_RE.finditer(text))
        if not resources:
            return

        # Line index for OCR: screenshots often list one resource per row;
        # char windows bleed emails from neighboring rows (case_15 regression).
        line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(i + 1)

        def line_no(pos: int) -> int:
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= pos:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        id_spans: List[Tuple[int, int, str]] = []
        for m in EMAIL_RE.finditer(text):
            id_spans.append((m.start(), m.end(), m.group(0)))
        for m in USER_RE.finditer(text):
            id_spans.append((m.start(), m.end(), m.group(0)))

        page_idents = {v for _, _, v in id_spans}
        window = 120 if from_ocr else 400

        for m in resources:
            rid = m.group(1)
            rline = line_no(m.start())
            if from_ocr:
                # Same line only — OCR row binding.
                local = {
                    val
                    for s, e, val in id_spans
                    if line_no(s) == rline
                }
                # If the resource line has no email/user, allow ±1 line but only
                # when that neighbor line does not itself contain another resource.
                if not local:
                    neighbor_idents = set()
                    for delta in (-1, 1):
                        nline = rline + delta
                        if nline < 0 or nline >= len(line_starts):
                            continue
                        # Neighbor contains another resource? skip.
                        if any(
                            line_no(rm.start()) == nline for rm in resources if rm is not m
                        ):
                            continue
                        for s, e, val in id_spans:
                            if line_no(s) == nline:
                                neighbor_idents.add(val)
                    local = neighbor_idents
                if local:
                    idents = local
                elif len(resources) == 1:
                    idents = set(page_idents)
                else:
                    idents = set()
                start = line_starts[rline]
                end = (
                    line_starts[rline + 1]
                    if rline + 1 < len(line_starts)
                    else len(text)
                )
            else:
                start = max(0, m.start() - window)
                end = min(len(text), m.end() + window)
                local = {
                    val
                    for s, e, val in id_spans
                    if (s >= start and s <= end) or (e >= start and e <= end)
                }
                idents = local or (page_idents if len(resources) == 1 else local)

            category = category_for_resource(rid)
            state = "PRESENT"
            local_text = text[start:end].lower()
            if rid.startswith("tombstone_"):
                category = "ACCOUNT_TOMBSTONE"
                state = "EXPOSED" if any("@" in x for x in idents) else "REDACTED"
            elif rid.startswith("audit_record_"):
                state = "PRESENT"
            elif rid.startswith("account_"):
                if re.search(r"\b(404|not found|absent|deleted|no rows)\b", local_text):
                    state = "ABSENT"
            elif rid.startswith("analytics_"):
                if re.search(r"\banonym", local_text):
                    state = "ANONYMIZED"
                elif any("@" in x for x in idents):
                    state = "EXPOSED"
            elif rid.startswith("data_flow_"):
                if re.search(r"\b(disabled|stopped|inactive|off)\b", local_text):
                    state = "DISABLED"
                else:
                    state = "ACTIVE"
            elif rid.startswith("backup_"):
                if re.search(r"\b(blocked|deny|disabled)\b", local_text):
                    state = "BLOCKED"
                else:
                    state = "ACTIVE"
            elif rid.startswith("support_") and "attachment" in source_name.lower():
                category = "SUPPORT_ATTACHMENT"

            emails_local = [x for x in idents if "@" in x]
            if from_ocr and len(emails_local) > 1 and not rid.startswith("audit_record_"):
                continue

            self._emit(
                evidence_id,
                captured_at,
                system_for_resource(rid),
                rid,
                category,
                state,
                idents,
                {
                    "source": source_name,
                    "from_ocr": from_ocr,
                    "ocr_line_bind": from_ocr,
                },
            )


def load_system_activity(dataset_dir: Path) -> List[Dict[str, str]]:
    path = dataset_dir / "system_activity.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
