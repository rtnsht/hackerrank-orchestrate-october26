"""Per-case audit: link resources to the subject, reconstruct state at audit_at, plan steps."""
from __future__ import annotations

from dataclasses import dataclass, field

from .identity import Subject
from .loaders import EvidenceStore
from .observation import Observation
from .policy import Policy

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
# action -> (finding op when violated, remediation op, verification op)
ACTION_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


@dataclass
class ResourceState:
    system_id: str
    resource_id: str
    category: str | None
    present: bool | None = None
    exposed: bool | None = None
    active: bool | None = None
    evidence_id: str = ""
    observed_at: str = ""
    history: list = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    debug: dict = field(default_factory=dict)


def visible(o, audit_at: str) -> bool:
    return o.available_at <= audit_at and o.observed_at <= audit_at


def subject_files(store: EvidenceStore, subject: Subject) -> set[str]:
    """Evidence files that mention the subject (subject-scoped captures can prove absence for them)."""
    tokens = subject.tokens()
    return {o.evidence_id for o in store.obs if o.keys & tokens}


SYSTEM_FROM_ID = {"account": "account_service", "search": "search_service", "analytics": "analytics_platform",
                  "support": "support_platform", "export": "export_service", "data_flow": "integration_service",
                  "backup": "backup_service", "audit_record": "security_service", "tombstone": "identity_service",
                  "legal_hold": "legal_service", "marketing": "marketing_platform"}


# Complete listings: dedicated per-system tables and inventory snapshots. Snapshot bundles and generic
# multi-collection replica dumps are captures taken for particular subjects.
SUBJECT_SCOPED = {"bundle", "sql_replica"}


def _copy_state(obs: list[Observation], scopes: list, rid: str, audit_at: str, files: set, check_scopes: bool,
                requested_at: str = ""):
    """Latest state of one physical copy: dict(present, exposed, active, evidence_id, observed_at)."""
    st = {"present": None, "exposed": None, "active": None, "evidence_id": "", "observed_at": "",
          "exposed_ev": "", "active_ev": ""}
    for o in obs:
        if o.present is not None:
            st["present"], st["evidence_id"], st["observed_at"] = o.present, o.evidence_id, o.observed_at
            if o.present is False:
                st["exposed"] = None
        if o.exposed is not None:
            st["exposed"], st["exposed_ev"] = o.exposed, o.evidence_id
            if st["present"] is not False:
                st["evidence_id"], st["observed_at"] = o.evidence_id, o.observed_at
        if o.active is not None:
            st["active"], st["active_ev"] = o.active, o.evidence_id
    if check_scopes:
        for sc in scopes:
            if sc.source in SUBJECT_SCOPED and sc.evidence_id not in files:
                continue  # mixed-collection captures only list the subjects they were taken for
            # only a listing taken after the request (a post-deletion check) can prove absence
            if sc.available_at <= audit_at and sc.observed_at <= audit_at and sc.observed_at > st["observed_at"] \
                    and sc.observed_at >= requested_at and rid not in sc.resource_ids:
                st.update(present=False, exposed=None, evidence_id=sc.evidence_id, observed_at=sc.observed_at)
    return st


def resolve_state(store: EvidenceStore, by_res: dict, rid: str, req: dict, files: set,
                  system_hint: str | None = None) -> ResourceState:
    audit_at, requested_at = req["audit_at"], req["requested_at"]
    obs = sorted((o for o in by_res.get(rid, []) if not o.claim and visible(o, audit_at)),
                 key=lambda o: (o.observed_at, o.available_at))
    any_obs = by_res.get(rid, [])
    system = obs[-1].system_id if obs else any_obs[0].system_id if any_obs else system_hint
    category = next((o.category for o in reversed(obs or any_obs) if o.category), None)
    if category is None:
        from .observation import SYSTEM_CATEGORY
        category = SYSTEM_CATEGORY.get(system)
    st = ResourceState(system, rid, category, history=obs)
    check_scopes = category not in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD")
    by_copy: dict[str, list] = {"primary": []}
    for o in obs:
        # a replica copy only matters if it was seen after the deletion request (a ghost copy)
        if o.copy != "primary" and o.observed_at < requested_at:
            continue
        by_copy.setdefault(o.copy, []).append(o)
    copies = {}
    for copy, cobs in by_copy.items():
        scopes = sorted((sc for sc in store.scopes if sc.system_id == system and sc.copy == copy),
                        key=lambda x: x.observed_at)
        copies[copy] = _copy_state(cobs, scopes, rid, audit_at, files, check_scopes, requested_at)
    present = [c for c in copies.values() if c["present"]]
    exposed = [c for c in present if c["exposed"]]
    if present:
        best = max(exposed or present, key=lambda c: c["observed_at"])
        st.present, st.exposed = True, bool(exposed) if any(c["exposed"] is not None for c in present) else None
        st.evidence_id, st.observed_at = best["evidence_id"], best["observed_at"]
    else:
        absent = [c for c in copies.values() if c["present"] is False]
        if absent:
            best = max(absent, key=lambda c: c["observed_at"])
            st.present, st.evidence_id, st.observed_at = False, best["evidence_id"], best["observed_at"]
        else:
            known = [c for c in copies.values() if c["exposed"] is not None]
            if known:
                best = max(known, key=lambda c: c["observed_at"])
                st.exposed, st.evidence_id = best["exposed"], best["exposed_ev"] or best["evidence_id"]
    active = [o for o in obs if o.active is not None]
    if active:
        st.active, st.evidence_id = active[-1].active, active[-1].evidence_id
    return st


def plan(case_id: str, states: list[ResourceState], policy: Policy) -> CaseResult:
    findings, remediation, verification = [], [], []
    for st in states:
        action = policy.action_for(st)
        if action is None:
            continue
        finding_op, fix_op, verify_op = ACTION_OPS[action]
        violated = False
        if action == "DELETE":
            violated = st.present is True
        elif action in ("REDACT", "ANONYMIZE"):
            violated = st.present is not False and st.exposed is True
        elif action in ("DISABLE", "BLOCK"):
            violated = st.active is True
        if violated:
            findings.append((finding_op, st.system_id, st.resource_id, st.evidence_id))
            remediation.append((fix_op, st.system_id, st.resource_id, ""))
        verification.append((verify_op, st.system_id, st.resource_id, st.evidence_id))
    incomplete = bool(findings)
    if incomplete:
        verification = [(op, s, r, "") for op, s, r, _ in verification]
    steps = []
    for phase, items, order in (("FINDING", findings, FINDING_ORDER), ("REMEDIATION", remediation, REMEDIATION_ORDER),
                                ("VERIFICATION", verification, VERIFICATION_ORDER)):
        for op, system, rid, ev in sorted(items, key=lambda x: (order.index(x[0]), x[1], x[2])):
            steps.append({"step_no": len(steps) + 1, "phase": phase, "system_id": system, "resource_id": rid,
                          "operation": op, "state": STATE_OF[op], "evidence_id": ev})
    return CaseResult(case_id, "INCOMPLETE" if incomplete else "COMPLETE", steps)


def audit_case(req: dict, subject: Subject | None, store: EvidenceStore, by_res: dict, policy: Policy,
               attribution) -> CaseResult:
    if subject is None:
        return CaseResult(req["case_id"], "COMPLETE", [], {"error": "subject not resolved"})
    resources = attribution.resources_for(req)
    files = subject_files(store, subject)
    states = []
    for rid in sorted(resources):
        hint = next((v for k, v in SYSTEM_FROM_ID.items() if rid.startswith(k + "_")), None)
        ev = attribution.event_of.get(rid)
        states.append(resolve_state(store, by_res, rid, req, files, ev["target_system"] if ev else hint))
    result = plan(req["case_id"], states, policy)
    result.debug = {"user_id": subject.user_id, "resources": sorted(resources),
                    "states": [(s.system_id, s.resource_id, s.category, s.present, s.exposed, s.active, s.evidence_id)
                               for s in states]}
    return result
