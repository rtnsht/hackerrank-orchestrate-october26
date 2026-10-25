"""Deletion policy application and step planning."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from evidence import Observation


FINDING_OPS = [
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
]
REMEDIATION_OPS = [
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
]
VERIFICATION_OPS = [
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]

PHASE_OP_ORDER = {
    "FINDING": {op: i for i, op in enumerate(FINDING_OPS)},
    "REMEDIATION": {op: i for i, op in enumerate(REMEDIATION_OPS)},
    "VERIFICATION": {op: i for i, op in enumerate(VERIFICATION_OPS)},
}


@dataclass
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


class Policy:
    def __init__(self, dataset_dir: Path):
        self.rules: Dict[str, Rule] = {}
        path = dataset_dir / "deletion_rules.csv"
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rule = Rule(
                    rule_id=row["rule_id"],
                    data_category=row["data_category"],
                    condition=row["condition"],
                    required_action=row["required_action"],
                )
                self.rules[rule.data_category] = rule

    def required_action(self, category: str) -> str:
        # DATA_FLOW is not in deletion_rules; treat as disable-when-active
        if category == "DATA_FLOW":
            return "DISABLE_FLOW"
        rule = self.rules.get(category)
        if not rule:
            # fallback by family
            if category.endswith("_ATTACHMENT"):
                return "DELETE"
            return "DELETE"
        return rule.required_action


def classify_observation(obs: Observation, policy: Policy) -> Optional[Tuple[str, str, str]]:
    """
    Return (finding_operation, remediation_operation, verify_operation) when non-compliant.
    Return None when already compliant (no finding).
    For compliant retained/present records, callers handle verification separately.
    """
    action = policy.required_action(obs.category)
    state = obs.state.upper()

    if obs.category == "DATA_FLOW":
        if state == "ACTIVE":
            return (
                "REPORT_ACTIVE_DATA_FLOW",
                "DISABLE_DATA_FLOW",
                "VERIFY_DATA_FLOW_DISABLED",
            )
        return None

    if obs.category == "BACKUP_ARCHIVE":
        if state == "ACTIVE":
            return ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
        return None

    if action == "DELETE":
        if state in {"PRESENT", "EXPOSED", "ACTIVE"}:
            return ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
        return None

    if action == "REDACT":
        if state in {"EXPOSED", "PRESENT"}:
            # PRESENT with identifiers for tombstone/support counts as exposed PII
            return ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
        return None

    if action == "ANONYMIZE":
        if state in {"EXPOSED", "PRESENT"}:
            return ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
        return None

    if action == "RETAIN":
        # non-compliance only if absent when required present — handled elsewhere
        return None

    return None


def compliant_verification(obs: Observation, policy: Policy) -> Optional[Tuple[str, str]]:
    """Return (verify_operation, state) for a compliant observation worth verifying."""
    action = policy.required_action(obs.category)
    state = obs.state.upper()

    if obs.category == "DATA_FLOW" and state == "DISABLED":
        return ("VERIFY_DATA_FLOW_DISABLED", "DISABLED")
    if obs.category == "BACKUP_ARCHIVE" and state == "BLOCKED":
        return ("VERIFY_RESTORE_BLOCKED", "BLOCKED")
    if action == "DELETE" and state == "ABSENT":
        return ("VERIFY_RECORD_ABSENT", "ABSENT")
    if action == "REDACT" and state == "REDACTED":
        return ("VERIFY_RECORD_REDACTED", "REDACTED")
    if action == "ANONYMIZE" and state == "ANONYMIZED":
        return ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")
    if action == "RETAIN" and state == "PRESENT":
        return ("VERIFY_RECORD_PRESENT", "PRESENT")
    return None


def sort_steps(steps: List[dict]) -> List[dict]:
    phase_order = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

    def key(step: dict):
        phase = step["phase"]
        op = step["operation"]
        return (
            phase_order[phase],
            PHASE_OP_ORDER[phase].get(op, 99),
            step["system_id"],
            step["resource_id"],
        )

    ordered = sorted(steps, key=key)
    for i, step in enumerate(ordered, start=1):
        step["step_no"] = i
    return ordered
