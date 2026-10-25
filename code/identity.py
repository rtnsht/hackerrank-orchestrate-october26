"""Identity resolution from user_identities.csv and user_audit_log.csv."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


def parse_ts(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass
class Identity:
    user_id: str
    name: Optional[str] = None
    emails: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)
    email_hashes: Set[str] = field(default_factory=set)
    updated_at: Optional[datetime] = None

    def all_tokens(self) -> Set[str]:
        tokens: Set[str] = {self.user_id.lower()}
        tokens.update(e.lower() for e in self.emails)
        tokens.update(p.lower() for p in self.phones if p)
        tokens.update(h.lower() for h in self.email_hashes if h)
        if self.name:
            tokens.add(self.name.lower())
        return tokens


class IdentityIndex:
    def __init__(self, dataset_dir: Path):
        self.by_user: Dict[str, Identity] = {}
        self.email_to_user: Dict[str, str] = {}
        self._load(dataset_dir)

    def _ensure(self, user_id: str) -> Identity:
        if user_id not in self.by_user:
            self.by_user[user_id] = Identity(user_id=user_id)
        return self.by_user[user_id]

    def _load(self, dataset_dir: Path) -> None:
        identities_path = dataset_dir / "user_identities.csv"
        with identities_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ident = self._ensure(row["user_id"])
                ident.name = row.get("name") or ident.name
                if row.get("email"):
                    email = row["email"].strip()
                    ident.emails.add(email)
                    self.email_to_user[email.lower()] = row["user_id"]
                if row.get("phone"):
                    ident.phones.add(row["phone"].strip())
                if row.get("email_hash"):
                    ident.email_hashes.add(row["email_hash"].strip())
                if row.get("updated_at"):
                    ident.updated_at = parse_ts(row["updated_at"])

        audit_path = dataset_dir / "user_audit_log.csv"
        with audit_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ident = self._ensure(row["user_id"])
                field_name = row["field_name"]
                for raw in (row.get("old_value"), row.get("new_value")):
                    if not raw:
                        continue
                    value = raw.strip()
                    if field_name == "email":
                        ident.emails.add(value)
                        self.email_to_user[value.lower()] = row["user_id"]
                    elif field_name == "phone":
                        ident.phones.add(value)
                    elif field_name == "email_hash":
                        ident.email_hashes.add(value)
                    elif field_name == "name":
                        ident.name = ident.name or value

    def resolve_email(self, email: str) -> Optional[Identity]:
        user_id = self.email_to_user.get(email.strip().lower())
        if not user_id:
            return None
        return self.by_user[user_id]

    def match_values(self, values: Iterable[str], identity: Identity) -> bool:
        tokens = identity.all_tokens()
        for value in values:
            if not value:
                continue
            if value.strip().lower() in tokens:
                return True
        return False
