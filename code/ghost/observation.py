"""Normalized evidence model shared by every loader.

Every piece of participant-visible evidence becomes one of two things:

* ``Observation`` - a statement about one resource at one moment (it exists, it is gone,
  it still exposes identifiers, a data flow is enabled, a backup can be restored ...).
* ``Scope`` - a complete listing of a collection at one moment. A linked resource that is
  missing from a scope that covers its system is proven absent at that moment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "object_storage": "SUPPORT_ATTACHMENT",
}

# Fields that hold direct identifiers. A retained record that still has any of these set
# "exposes PII" (subject references such as user_id are handled per category).
DIRECT_ID_FIELDS = ("name", "email", "phone", "email_hash")


@dataclass
class Observation:
    evidence_id: str
    available_at: str          # when the evidence item became available (file captured_at / activity time)
    observed_at: str           # when the statement about the resource was true
    system_id: str
    resource_id: str
    source: str                # loader-specific kind, e.g. "sql_inventory", "handover", "log_lookup"
    category: str | None = None
    present: bool | None = None      # True = record exists, False = proven absent, None = not stated
    exposed: bool | None = None      # True = direct identifiers still visible, False = none visible
    active: bool | None = None       # data flow enabled / backup restore allowed
    ids: dict = field(default_factory=dict)   # normalized direct identifiers: name/email/phone/email_hash/subject
    keys: set = field(default_factory=set)    # identifier tokens that link this resource to a subject
    copy: str = "primary"            # replica / region label for replication drift
    claim: bool = False              # True for workflow claims (activity results) - never proof of state
    note: str = ""

    def short(self) -> str:
        bits = [self.evidence_id, self.observed_at, self.system_id, self.resource_id, self.source]
        for k in ("present", "exposed", "active"):
            v = getattr(self, k)
            if v is not None:
                bits.append(f"{k}={v}")
        if self.copy != "primary":
            bits.append(f"copy={self.copy}")
        if self.category:
            bits.append(self.category)
        if self.claim:
            bits.append("CLAIM")
        if self.ids:
            bits.append(str({k: v for k, v in self.ids.items() if v}))
        return " ".join(str(b) for b in bits)


@dataclass
class Scope:
    evidence_id: str
    available_at: str
    observed_at: str
    system_id: str
    resource_ids: set
    source: str
    copy: str = "primary"
    collection: str = ""
