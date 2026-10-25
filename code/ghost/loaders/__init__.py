"""Load every participant-visible evidence source into one EvidenceStore."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict

from ..observation import Observation, Scope
from ..util import CACHE_DIR, DATASET_DIR, norm_ts
from .common import record_obs, status_obs, truthy

KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s|]*)')
LOOKUP_KEYS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref")
SERVICE_ALIAS = {"profile_service": "account_service", "support_replica": "support_platform"}


class EvidenceStore:
    def __init__(self):
        self.manifest: dict[str, dict] = {}
        self.obs: list[Observation] = []
        self.scopes: list[Scope] = []
        self.activity: list[dict] = []
        self.errors: list[str] = []

    def add(self, o):
        if o is not None:
            self.obs.append(o)

    def by_resource(self) -> dict[str, list[Observation]]:
        out = defaultdict(list)
        for o in self.obs:
            out[o.resource_id].append(o)
        return out


# ---------------------------------------------------------------- JSON
def _load_json(store: EvidenceStore, fid: str, cap: str, path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if "snapshots" in data:  # records-assurance snapshot bundle
        for snap in data["snapshots"]:
            at = norm_ts(snap.get("captured_at")) or cap
            system = snap["data_store"]
            recs = snap.get("records", [])
            if snap.get("collection") == "operational_record_history":
                for r in recs:
                    store.add(status_obs(fid, cap, norm_ts(r.get("recorded_at")) or at, r["system"],
                                         r["resource_id"], r.get("record_type"), r.get("result"),
                                         r.get("lookup_value") or r.get("subject_reference"), "bundle_history"))
                continue
            store.scopes.append(Scope(fid, cap, at, system, {r.get("resource_id") for r in recs},
                                      "bundle", collection=snap.get("collection", "")))
            for r in recs:
                store.add(record_obs(fid, cap, at, system, r, "bundle"))
    elif "service_exports" in data:  # point-in-time service readback export
        for system, colls in data["service_exports"].items():
            for coll, recs in colls.items():
                for r in recs:
                    o = record_obs(fid, cap, norm_ts(r.get("observed_at")) or cap, system, r, "service_export")
                    if o is None:
                        continue
                    if "status_code" in r:
                        o.present = r["status_code"] == 200 and r.get("profile") is not None
                    if coll == "index_queries":
                        o.present = bool(r.get("hits", {}).get("total"))
                        o.keys |= {"e:" + str(r["query"]).lower()} if "@" in str(r.get("query")) else set()
                    store.add(o)
    elif "records" in data and data["records"] and "record_type" in data["records"][0]:
        for r in data["records"]:  # record-history style manifests / ledgers
            store.add(status_obs(fid, cap, norm_ts(r.get("recorded_at")) or cap, r["system"], r["resource_id"],
                                 r.get("record_type"), r.get("result"), r.get("subject_reference"), "record_history"))
    elif "records" in data:  # single collection capture (analytics report, export jobs)
        system = COLLECTION_SYSTEM.get(data.get("collection"), data.get("source"))
        at = norm_ts(data.get("captured_at")) or cap
        for r in data["records"]:
            o = record_obs(fid, cap, at, system, r, "collection")
            if o and "download_available" in r:
                o.present = bool(r["download_available"])
            store.add(o)
    else:
        store.errors.append(f"{fid}: unrecognized JSON layout")


COLLECTION_SYSTEM = {
    "customer_export_jobs": "export_service",
    "customer_analytics_reports": "analytics_platform",
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "support_conversations": "support_platform",
    "ticket_attachments": "support_platform",
}


# ---------------------------------------------------------------- SQL
INVENTORY_TABLES = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def _load_sql(store: EvidenceStore, fid: str, cap: str, path):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(path.read_text(encoding="utf-8"))
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    if "snapshot_scope" in tables:  # inventory snapshot: complete listing per covered system
        scope = {r["origin_system"]: norm_ts(r["captured_at"]) for r in con.execute("select * from snapshot_scope")}
        for table, system in INVENTORY_TABLES.items():
            if table not in tables or system not in scope:
                continue
            rows = [dict(r) for r in con.execute(f"select * from {table}")]
            store.scopes.append(Scope(fid, cap, scope[system], system, {r["resource_id"] for r in rows}, "sql_inventory"))
            for r in rows:
                store.add(record_obs(fid, cap, norm_ts(r.get("updated_at")) or scope[system], system, r, "sql_inventory"))
        return
    for table in tables:
        cols = [c[1] for c in con.execute(f"pragma table_info({table})")]
        copy = "primary"
        for suffix in ("_primary", "_failover_replica", "_read_replica", "_reporting_replica", "_replica_ap_south",
                       "_replica_eu_west"):
            if table.endswith(suffix):
                copy = suffix.lstrip("_")
        rows = [dict(r) for r in con.execute(f"select * from {table}")]
        if "record_json" in cols:  # generic multi-collection replica table
            by_sys = defaultdict(set)
            for r in rows:
                rec = json.loads(r["record_json"])
                by_sys[r["origin_system"]].add(r["resource_id"])
                store.add(record_obs(fid, cap, cap, r["origin_system"], rec, "sql_replica", copy=copy))
            for system, ids in by_sys.items():
                store.scopes.append(Scope(fid, cap, cap, system, ids, "sql_replica", copy=copy))
        else:
            system = "search_service" if "search" in table else "account_service"
            for r in rows:
                store.add(record_obs(fid, cap, cap, system, r, "sql_table", copy=copy))
            store.scopes.append(Scope(fid, cap, cap, system, {r["resource_id"] for r in rows}, "sql_table", copy=copy))


# ---------------------------------------------------------------- logs
def parse_log_line(line: str) -> dict | None:
    if len(line) < 21 or not line[:4].isdigit():
        return None
    ts, rest = line[:20], line[20:].lstrip(" |")
    d: dict = {}
    if rest.startswith("{"):
        d = json.loads(rest)
        d.pop("logger", None)
    else:
        m = re.match(r"\[(\w+)\] \[(\w+)\] \[([\w-]+)\] (\w+): (.*)", rest)
        if m:
            d = {"service": m[2], "trace_id": m[3], "event": m[4]}
            rest = m[5]
        m = re.match(r"(\w+) \| (\w+) \| ([\w-]+) \| (\w+) \| (.*)", rest)
        if m:
            d = {"service": m[2], "trace_id": m[3], "event": m[4]}
            rest = m[5]
        m = re.match(r"<\d+>1 \S+ (\w+) ([\w-]+) - - (.*)", rest)
        if m:
            d = {"service": m[1], "trace_id": m[2]}
            rest = m[3]
        for k, v in KV_RE.findall(rest):
            if v.startswith('"'):
                v = v[1:-1].replace('\\"', '"')
            d.setdefault(k, v)
        if "message" in d:
            for k, v in KV_RE.findall(d.pop("message")):
                d.setdefault(k, v[1:-1] if v.startswith('"') else v)
        if "function" in d:
            d["service"] = d.pop("function")
        if "requestId" in d:
            d["trace_id"] = d.pop("requestId")
    d = {k: (None if str(v).lower() in ("null", "") else v) for k, v in d.items()}
    d["ts"] = norm_ts(ts)
    d["service"] = SERVICE_ALIAS.get(d.get("service"), d.get("service"))
    return d


def _log_event_obs(fid: str, cap: str, d: dict, trace_keys: list) -> Observation | None:
    ev = d.get("event") or ""
    if d.get("event") == "record_lookup":
        return status_obs(fid, cap, d["ts"], d.get("system"), d.get("resource_id"), d.get("record_type"),
                          d.get("result"), d.get("subject_reference"), "log_record_lookup")
    if not d.get("resource_id") or ev.endswith("_started"):
        return None
    system = d.get("service")
    rec = dict(d)
    o = record_obs(fid, cap, d["ts"], system, rec, "log")
    if o is None:
        return None
    for k in trace_keys:
        from ..util import identifier_tokens
        o.keys |= identifier_tokens(k)
    if "rows" in d and d["rows"] is not None:
        o.present = int(d["rows"]) > 0
    if "http_status" in d and d["http_status"]:
        o.present = d["http_status"] == "200"
    if d.get("result") in ("hit", "miss"):
        o.present = d["result"] == "hit"
    if "available" in d:
        o.present = truthy(d["available"]) is not False
    if ev in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
        o.exposed = True if "direct_identifiers" not in d else truthy(d.get("direct_identifiers")) is not False
        if ev == "attachment_pii_scan":
            o.category = "SUPPORT_ATTACHMENT"
    if ev == "analytics_profile_read" and "direct_identifiers" in d:
        o.exposed = truthy(d["direct_identifiers"]) is True or o.exposed
    if ev == "restore_candidate_evaluated":
        o.active = truthy(d.get("eligible")) is True and truthy(d.get("deletion_block")) is not True
    if ev == "restore_authorization_completed":
        o.active = (d.get("decision") or "").lower() not in ("deny", "denied", "blocked")
    if ev in ("connector_heartbeat", "connector_config_loaded"):
        o.active = truthy(d.get("enabled")) is True and truthy(d.get("scheduler_registered")) is not False
    if ev == "contact_lookup_completed" and "email_opt_in" in d:
        o.category = "MARKETING_PROFILE"
        o.note = "email_opt_in=" + str(d["email_opt_in"])
    if ev == "deletion_marker_check":
        o.present = truthy(d.get("marker_found")) is not True
    if d.get("service") == "support_platform" and ev.startswith(("contact_query", "deletion_marker")):
        # legacy contact-index copy: no conversation context to retain, so the whole copy must go
        o.category = "SUPPORT_CONTACT_COPY"
        o.copy = "support_replica"
    if ev == "audit_record_read_completed" and "rows" not in d:
        o.present = True
    return o


def _load_log(store: EvidenceStore, fid: str, cap: str, path):
    lines = [parse_log_line(l.rstrip("\n")) for l in path.read_text(encoding="utf-8").splitlines()]
    lines = [d for d in lines if d]
    trace_keys = defaultdict(list)
    for d in lines:
        for k in LOOKUP_KEYS:
            if d.get(k) and d.get("trace_id"):
                trace_keys[d["trace_id"]].append(d[k])
    for d in lines:
        store.add(_log_event_obs(fid, cap, d, trace_keys.get(d.get("trace_id"), [])))


# ---------------------------------------------------------------- text / pdf / images
def _load_handover(store: EvidenceStore, fid: str, cap: str, path):
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == 6:
            ts, system, rid, rtype, result, subject = parts
            store.add(status_obs(fid, cap, norm_ts(ts) or cap, system, rid, rtype, result, subject, "handover"))


def _load_cached_extraction(store: EvidenceStore, fid: str, cap: str, cache: dict):
    for r in cache.get(fid, {}).get("records", []):
        o = record_obs(fid, cap, norm_ts(r.get("observed_at")) or cap, r["system_id"], r.get("fields", {}) | {
            "resource_id": r["resource_id"]}, r.get("source", "image"), category=r.get("category"))
        if o is None:
            continue
        for k in ("present", "exposed", "active"):
            if k in r:
                setattr(o, k, r[k])
        store.add(o)


# ---------------------------------------------------------------- activity
def _load_activity(store: EvidenceStore):
    with open(DATASET_DIR / "system_activity.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["event_time"] = norm_ts(row["event_time"])
            store.activity.append(row)


def load_all() -> EvidenceStore:
    store = EvidenceStore()
    cache_path = CACHE_DIR / "evidence_extractions.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    with open(DATASET_DIR / "evidence_files.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fid, cap = row["file_id"], norm_ts(row["captured_at"])
            row["captured_at"] = cap
            store.manifest[fid] = row
            path = DATASET_DIR / row["file_path"]
            ext = path.suffix.lower()
            try:
                if fid in cache:
                    _load_cached_extraction(store, fid, cap, cache)
                elif ext == ".json":
                    _load_json(store, fid, cap, path)
                elif ext == ".sql":
                    _load_sql(store, fid, cap, path)
                elif ext == ".log":
                    _load_log(store, fid, cap, path)
                elif ext == ".txt":
                    _load_handover(store, fid, cap, path)
                else:
                    store.errors.append(f"{fid}: no extraction for {path.name}")
            except Exception as exc:  # keep going; one bad file must not sink the run
                store.errors.append(f"{fid}: {type(exc).__name__}: {exc}")
    _load_activity(store)
    return store
