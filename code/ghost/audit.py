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


def link_resources(store: EvidenceStore, subject: Subject, audit_at: str) -> set[str]:
    tokens = subject.tokens()
    linked = set()
    for o in store.obs:
        if visible(o, audit_at) and o.keys & tokens:
            linked.add(o.resource_id)
    return linked


def account_from_activity(store: EvidenceStore, req: dict, taken: set) -> str | None:
    """Fallback: the deletion worker's account_service submission closest to the reported time."""
    best, best_gap = None, None
    from datetime import datetime
    rep = datetime.fromisoformat(req["reported_at"].replace("Z", "+00:00"))
    for a in store.activity:
        if a["target_system"] != "account_service" or a["event_type"] != "DELETE_RECORD":
            continue
        if a["resource_id"] in taken:
            continue
        gap = abs((datetime.fromisoformat(a["event_time"].replace("Z", "+00:00")) - rep).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = a["resource_id"], gap
    return best


def resolve_state(store: EvidenceStore, by_res: dict, rid: str, audit_at: str) -> ResourceState | None:
    obs = [o for o in by_res.get(rid, []) if visible(o, audit_at) and not o.claim]
    if not obs:
        return None
    obs.sort(key=lambda o: (o.observed_at, o.available_at))
    system = obs[-1].system_id
    category = next((o.category for o in reversed(obs) if o.category), None)
    st = ResourceState(system, rid, category, history=obs)
    # latest observation wins; at equal time a present copy (replica drift) beats an absent one
    latest_t = obs[-1].observed_at
    for o in obs:
        if o.present is not None:
            st.present = o.present
            st.evidence_id, st.observed_at = o.evidence_id, o.observed_at
            if o.present is False:
                st.exposed = None
        if o.exposed is not None:
            st.exposed = o.exposed
        if o.active is not None:
            st.active = o.active
            st.evidence_id, st.observed_at = o.evidence_id, o.observed_at
    same = [o for o in obs if o.observed_at == latest_t]
    if any(o.present for o in same):
        st.present = True
    # complete listings taken after the last direct observation can prove absence
    for sc in store.scopes:
        if sc.system_id == system and sc.available_at <= audit_at and sc.observed_at <= audit_at \
                and sc.observed_at > st.observed_at and rid not in sc.resource_ids:
            st.present, st.exposed = False, None
            st.evidence_id, st.observed_at = sc.evidence_id, sc.observed_at
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
        if action == "RETAIN" and st.present is False:
            continue
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


def audit_case(req: dict, subject: Subject | None, store: EvidenceStore, by_res: dict, policy: Policy) -> CaseResult:
    audit_at = req["audit_at"]
    if subject is None:
        return CaseResult(req["case_id"], "COMPLETE", [], {"error": "subject not resolved"})
    linked = link_resources(store, subject, audit_at)
    states = [s for rid in sorted(linked) if (s := resolve_state(store, by_res, rid, audit_at))]
    if not any(s.system_id == "account_service" for s in states):
        acc = account_from_activity(store, req, set())
        if acc:
            states.append(ResourceState("account_service", acc, "USER_PROFILE", present=False))
    result = plan(req["case_id"], states, policy)
    result.debug = {"user_id": subject.user_id, "linked": sorted(linked),
                    "states": [(s.system_id, s.resource_id, s.category, s.present, s.exposed, s.active, s.evidence_id)
                               for s in states]}
    return result
