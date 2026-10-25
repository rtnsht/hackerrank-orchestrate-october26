"""deletion_rules.csv -> required treatment per data category."""
from __future__ import annotations

import csv

from .util import DATASET_DIR


class Policy:
    def __init__(self):
        self.rules: dict[str, dict] = {}
        with open(DATASET_DIR / "deletion_rules.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.rules[r["data_category"]] = r

    def action_for(self, st) -> str | None:
        """Return DELETE / REDACT / ANONYMIZE / RETAIN / DISABLE / BLOCK for one resource state."""
        cat = st.category
        if cat == "DATA_FLOW":
            return "DISABLE"
        if cat == "BACKUP_ARCHIVE":
            # rule_14: archives are retained, so the compliant state is "restore blocked"
            return "BLOCK"
        rule = self.rules.get(cat or "")
        return rule["required_action"] if rule else None
