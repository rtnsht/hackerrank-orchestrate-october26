"""Attribute resources to deletion cases.

Two grounded links connect a resource to a case:

1. Identifier links - any evidence row whose identifiers (current/historical email, email hash,
   phone, user_id) belong to the case subject.
2. Timestamp links - the deletion worker processes a request's resources in a burst around the
   moment the workflow reported its status. Resources that carry no identifiers at all
   (404 account lookups, already-anonymized analytics profiles) can only be tied to a case this way.

Timestamp links are ambiguous when several requests were reported at the same moment, so they are
resolved globally across every case: a resource identified as someone else's is never taken, a case
takes at most one resource per system from the burst, and remaining ties are resolved in a stable
order (event time order matched to case order).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .identity import IdentityGraph
from .loaders import EvidenceStore

WORKER = "deletion_worker"
# Where a worker event sits relative to the reported status, by workflow stage (minutes):
# submissions and completed policy actions precede the status report; final results and
# follow-up checks arrive after it; immediate confirmations bracket it closely.
STAGE_WINDOWS = [
    ("submitted primary profile", (-90, -30)),
    ("policy action completed", (-45, -1)),
    ("received the final result", (25, 130)),
    ("connector status check", (25, 130)),
    ("restore-access check", (25, 130)),
    ("reported the requested operation", (-20, 20)),
    ("stopped after the target system", (-20, 20)),
    ("completed some internal work", (-20, 20)),
]
DEFAULT_WINDOW = (-90, 130)


# The workflow's reported status is derived from its primary-profile submission result.
STATUS_OF_RESULT = {"SUCCESS": "COMPLETE", "PARTIAL": "PARTIALLY_COMPLETE", "FAILED": "NOT_STARTED"}


def stage_window(event: dict) -> tuple[timedelta, timedelta]:
    detail = event.get("details", "").lower()
    lo, hi = next((w for key, w in STAGE_WINDOWS if key in detail), DEFAULT_WINDOW)
    return timedelta(minutes=lo), timedelta(minutes=hi)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Attribution:
    def __init__(self, store: EvidenceStore, graph: IdentityGraph, cases: list[dict]):
        """cases: deletion-authorized requests (all known requests, so elimination is global)."""
        self.store, self.graph = store, graph
        self.token_owner: dict[str, set] = defaultdict(set)
        for s in graph.subjects.values():
            for t in s.tokens():
                self.token_owner[t].add(s.user_id)
        # resource -> users it is identifier-linked to; resource -> earliest/latest identifier evidence
        self.resource_users: dict[str, set] = defaultdict(set)
        self.resource_user_times: dict[tuple, list] = defaultdict(list)
        for o in store.obs:
            for k in o.keys:
                for uid in self.token_owner.get(k, ()):
                    self.resource_users[o.resource_id].add(uid)
                    self.resource_user_times[(o.resource_id, uid)].append(o.observed_at)
        self.user_resources: dict[str, set] = defaultdict(set)
        for rid, users in self.resource_users.items():
            for uid in users:
                self.user_resources[uid].add(rid)
        self.by_res = store.by_resource()
        self.system_of = {}
        for o in store.obs:
            self.system_of.setdefault(o.resource_id, o.system_id)
        self.case_user = {}
        for c in cases:
            subj = graph.resolve(c["email"])
            if subj:
                self.case_user[c["case_id"]] = subj.user_id
        self.cases = {c["case_id"]: c for c in cases if c["case_id"] in self.case_user}
        self.user_cases: dict[str, list] = defaultdict(list)
        for c in self.cases.values():
            self.user_cases[self.case_user[c["case_id"]]].append(c)
        self.assigned: dict[str, set] = defaultdict(set)   # case_id -> resource ids from the worker burst
        self.event_of: dict[str, dict] = {}
        self._assign()

    # ------------------------------------------------------------------
    def _candidates(self, event: dict) -> list[str]:
        t = _dt(event["event_time"])
        lo, hi = stage_window(event)
        out = []
        for cid, c in self.cases.items():
            rep = _dt(c["reported_at"])
            if rep + lo <= t <= rep + hi:
                out.append(cid)
        users = self.resource_users.get(event["resource_id"])
        if users:
            out = [cid for cid in out if self.case_user[cid] in users]
        if "submitted primary profile" in event.get("details", "").lower():
            want = STATUS_OF_RESULT.get(event["reported_result"])
            matching = [cid for cid in out if self.cases[cid]["reported_status"] == want]
            out = matching or out
        return out

    def _evidence_cost(self, rid: str, cid: str) -> float:
        """Hours between the case's audit and the closest observation of the resource made for it
        (inside the request -> audit window). Readbacks are taken just before the audit they serve."""
        c = self.cases[cid]
        audit = _dt(c["audit_at"])
        best = float("inf")
        for o in self.by_res.get(rid, ()):
            if c["requested_at"] <= o.observed_at <= c["audit_at"] and o.available_at <= c["audit_at"]:
                best = min(best, round((audit - _dt(o.observed_at)).total_seconds() / 3600))
        return best

    def _assign(self):
        events = {}
        for a in self.store.activity:
            if a["source_system"] != WORKER:
                continue
            events.setdefault(a["resource_id"], a)   # first worker event for the resource
        pending = {}
        for rid, ev in events.items():
            self.system_of.setdefault(rid, ev["target_system"])
            cands = [c for c in self._candidates(ev) if not self._has_system(c, self.system_of.get(rid))]
            if len(cands) == 1:
                self.assigned[cands[0]].add(rid)
                self.event_of[rid] = ev
            elif cands:
                pending[rid] = (ev, cands)
        # elimination: a case already holding a resource of this system from the burst (or by
        # identifiers) is not a candidate for another orphan of the same system
        changed = True
        while changed and pending:
            changed = False
            for rid, (ev, cands) in list(pending.items()):
                system = self.system_of.get(rid)
                left = [c for c in cands if not self._has_system(c, system)]
                if len(left) == 1:
                    self.assigned[left[0]].add(rid)
                    self.event_of[rid] = ev
                    del pending[rid]
                    changed = True
                elif left != cands:
                    pending[rid] = (ev, left)
                    changed = True
                if not left:
                    del pending[rid]
        # remaining ties: prefer pairs where the resource was observed inside the case's audit window,
        # then pair event time order with case order (stable and deterministic)
        # Only accounts are tie-broken: every request has exactly one primary profile, whereas a guessed
        # analytics/support orphan is more often wrong than right and would corrupt two cases.
        pairs = []
        for rid, (ev, cands) in pending.items():
            costs = {c: self._evidence_cost(rid, c) for c in cands}
            if self.system_of.get(rid) != "account_service":
                best = min(costs.values())
                if best == float("inf"):
                    continue  # no evidence ties this orphan to any of the requests
                cands = [c for c in cands if costs[c] == best]
            for rank, cid in enumerate(sorted(cands, key=lambda c: (len(c), c))):
                pairs.append((ev["event_time"], rank, rid, cid, ev))
        done = set()
        for _, _, rid, cid, ev in sorted(pairs, key=lambda x: x[:2]):
            if rid in done or self._has_system(cid, self.system_of.get(rid)):
                continue
            self.assigned[cid].add(rid)
            self.event_of[rid] = ev
            done.add(rid)

    def _has_system(self, cid: str, system: str) -> bool:
        uid = self.case_user[cid]
        for rid in self.assigned[cid]:
            if self.system_of.get(rid) == system:
                return True
        c = self.cases[cid]
        for rid in self.user_resources.get(uid, ()):
            if self.system_of.get(rid) == system and self._identifier_relevant(rid, uid, c):
                return True
        return False

    def _identifier_relevant(self, rid: str, uid: str, case: dict) -> bool:
        """An identifier observation belongs to the subject's most recent request made before it."""
        later = [c["requested_at"] for c in self.user_cases.get(uid, ()) if c["requested_at"] > case["requested_at"]]
        nxt = min(later) if later else None
        return any(t >= case["requested_at"] and (nxt is None or t < nxt)
                   for t in self.resource_user_times.get((rid, uid), []))

    # ------------------------------------------------------------------
    def resources_for(self, case: dict) -> set[str]:
        cid = case["case_id"]
        uid = self.case_user.get(cid)
        if uid is None:
            return set()
        out = set(self.assigned.get(cid, set()))
        for rid in self.user_resources.get(uid, ()):
            ev = self.event_of.get(rid)
            if ev is not None and rid not in out:
                continue  # its worker burst belongs to another request of the same subject
            if rid in out or self._identifier_relevant(rid, uid, case):
                out.add(rid)
        return out
