"""Identity graph: current snapshot + audit-log history -> every identifier a subject ever had."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field

from .util import DATASET_DIR, email_hash, norm_email, norm_name, norm_phone, norm_ts


@dataclass
class Subject:
    user_id: str
    name: str
    emails: list = field(default_factory=list)      # current first, then historical
    phones: list = field(default_factory=list)
    names: list = field(default_factory=list)
    hashes: set = field(default_factory=set)

    def tokens(self, include_names: bool = False) -> set:
        t = {"u:" + self.user_id}
        t |= {"e:" + e for e in self.emails}
        t |= {"h:" + h for h in self.hashes}
        t |= {"p:" + p for p in self.phones if p}
        if include_names:
            t |= {"n:" + norm_name(n) for n in self.names}
        return t


class IdentityGraph:
    def __init__(self):
        self.subjects: dict[str, Subject] = {}
        self.by_email: dict[str, str] = {}
        with open(DATASET_DIR / "user_identities.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                s = Subject(r["user_id"], r["name"], [norm_email(r["email"])], [norm_phone(r["phone"])],
                            [r["name"]], {r["email_hash"].lower(), email_hash(r["email"])})
                self.subjects[s.user_id] = s
        with open(DATASET_DIR / "user_audit_log.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                s = self.subjects.get(r["user_id"])
                if not s:
                    continue
                for v in (r["old_value"], r["new_value"]):
                    if not v:
                        continue
                    if r["field_name"] == "email":
                        e = norm_email(v)
                        if e not in s.emails:
                            s.emails.append(e)
                        s.hashes.add(email_hash(e))
                    elif r["field_name"] == "phone":
                        p = norm_phone(v)
                        if p not in s.phones:
                            s.phones.append(p)
                    elif r["field_name"] in ("name", "full_name"):
                        if v not in s.names:
                            s.names.append(v)
                    elif r["field_name"] == "email_hash":
                        s.hashes.add(v.lower())
        for s in self.subjects.values():
            for e in s.emails:
                self.by_email.setdefault(e, s.user_id)

    def resolve(self, email: str) -> Subject | None:
        uid = self.by_email.get(norm_email(email))
        return self.subjects.get(uid) if uid else None
