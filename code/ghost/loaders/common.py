"""Turn heterogeneous raw records into ``Observation`` objects."""
from __future__ import annotations

from ..observation import Observation, SYSTEM_CATEGORY
from ..util import EMAIL_RE, identifier_tokens, is_null, norm_email, norm_name, norm_phone

NAME_FIELDS = ("full_name", "display_name", "requester_name", "name", "customer_name")
EMAIL_FIELDS = ("email", "owner_email", "customer_email", "requester_email", "uploaded_by")
PHONE_FIELDS = ("phone", "owner_phone", "requester_phone", "customer_phone")
HASH_FIELDS = ("email_hash",)
SUBJECT_FIELDS = ("customer_reference", "subject_reference", "user_id", "subject_ref")
# Fields that only say how a record was looked up / correlated; they link but are not record content.
LINK_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "correlation_key",
               "matched_value", "subject_selector", "lookup_value", "query")
TEXT_FIELDS = ("message_text", "message_excerpt")


def truthy(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "enabled", "allowed", "active"):
        return True
    if s in ("0", "false", "no", "n", "disabled", "blocked", "stopped"):
        return False
    return None


def flatten(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, dict) and k in ("document", "profile"):
            for k2, v2 in v.items():
                out.setdefault(k2, v2)
        else:
            out[k] = v
    return out


def extract_ids(rec: dict) -> tuple[dict, set]:
    """Return (direct identifier dict, linking tokens) for a flattened record."""
    ids: dict = {"name": None, "email": None, "phone": None, "email_hash": None, "subject": None}
    keys: set = set()
    for f in NAME_FIELDS:
        v = rec.get(f)
        if not is_null(v) and isinstance(v, str) and "@" not in v:
            ids["name"] = v
            keys.add("n:" + norm_name(v))
    for f in EMAIL_FIELDS:
        v = rec.get(f)
        if not is_null(v) and isinstance(v, str) and EMAIL_RE.search(v):
            ids["email"] = norm_email(EMAIL_RE.search(v).group(0))
            keys |= identifier_tokens(v)
    for f in PHONE_FIELDS:
        v = rec.get(f)
        if not is_null(v):
            ids["phone"] = norm_phone(str(v))
            keys.add("p:" + norm_phone(str(v)))
    for f in HASH_FIELDS:
        v = rec.get(f)
        if not is_null(v):
            ids["email_hash"] = str(v).lower()
            keys |= identifier_tokens(v)
    for f in SUBJECT_FIELDS:
        v = rec.get(f)
        if is_null(v):
            continue
        toks = identifier_tokens(v)
        keys |= toks
        if any(t.startswith("u:") for t in toks):
            ids["subject"] = str(v).lower()
        elif any(t.startswith("e:") for t in toks):
            # some catalogs store the subject's email in a reference column
            ids["email"] = ids["email"] or norm_email(EMAIL_RE.search(str(v)).group(0))
        elif any(t.startswith("h:") for t in toks):
            ids["email_hash"] = ids["email_hash"] or str(v).lower()
    for f in TEXT_FIELDS:
        v = rec.get(f)
        if isinstance(v, str):
            for em in EMAIL_RE.findall(v):
                ids["email"] = ids["email"] or norm_email(em)
                keys.add("e:" + norm_email(em))
    for f in LINK_FIELDS:
        keys |= identifier_tokens(rec.get(f))
    return ids, keys


def record_obs(evidence_id: str, available_at: str, observed_at: str, system: str, rec: dict,
               source: str, copy: str = "primary", category: str | None = None) -> Observation | None:
    rec = flatten(rec)
    rid = rec.get("resource_id")
    if is_null(rid):
        return None
    ids, keys = extract_ids(rec)
    exposed = any(ids[k] for k in ("name", "email", "phone", "email_hash"))
    active = None
    for f in ("enabled", "restore_allowed"):
        if f in rec:
            active = truthy(rec[f])
    obs = Observation(
        evidence_id=evidence_id, available_at=available_at, observed_at=observed_at or available_at,
        system_id=system, resource_id=str(rid), source=source,
        category=category or SYSTEM_CATEGORY.get(system), present=True, exposed=exposed,
        active=active, ids=ids, keys=keys, copy=copy,
    )
    status = rec.get("account_status")
    if isinstance(status, str):
        obs.note = status
    if "download_available" in rec or "available" in rec:
        obs.note = f"download_available={truthy(rec.get('download_available', rec.get('available')))}"
    if "email_opt_in" in rec:
        obs.note = f"email_opt_in={rec['email_opt_in']}"
    if rec.get("storage_class") == "ticket_attachment" or "file_name" in rec and system == "support_platform":
        obs.category = "SUPPORT_ATTACHMENT"
    return obs


RESULT_STATE = {
    # result -> (present, exposed, active)
    "FOUND": (True, None, None),
    "NOT_FOUND": (False, None, None),
    "ABSENT": (False, None, None),
    "DELETED": (False, None, None),
    "IDENTIFIABLE": (True, True, None),
    "ANONYMIZED": (True, False, None),
    "REDACTED": (True, False, None),
    "ACTIVE": (True, None, True),
    "ENABLED": (True, None, True),
    "DISABLED": (True, None, False),
    "BLOCKED": (True, None, False),
}


def status_obs(evidence_id: str, available_at: str, observed_at: str, system: str, resource_id: str,
               record_type: str | None, result: str, subject: str | None, source: str) -> Observation | None:
    """Observation from a 'record lookup' style line: system/resource/type/result/subject."""
    state = RESULT_STATE.get((result or "").strip().upper())
    if state is None or is_null(resource_id):
        return None
    present, exposed, active = state
    keys = identifier_tokens(subject)
    ids = {"name": None, "email": None, "phone": None, "email_hash": None, "subject": None}
    for k in keys:
        if k.startswith("u:"):
            ids["subject"] = k[2:]
    return Observation(
        evidence_id=evidence_id, available_at=available_at, observed_at=observed_at or available_at,
        system_id=system, resource_id=resource_id, source=source,
        category=(record_type or SYSTEM_CATEGORY.get(system)), present=present, exposed=exposed,
        active=active, ids=ids, keys=keys, note=result,
    )
