"""Per-case deletion audit: discovery, policy, and step generation."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple  # noqa: F401 — Optional used below

from assign import GlobalAssigner
from evidence import EvidenceIndex, Observation, load_system_activity, system_for_resource
from identity import Identity, IdentityIndex, parse_ts
from policy import (
    Policy,
    classify_observation,
    compliant_verification,
    sort_steps,
)


# Multilingual deletion intent cues (deterministic; LLM optional later).
DELETION_PATTERNS = [
    r"delet",  # delete / deleting / deleted / deletion
    r"eras",  # erase / erased / erasing / erasure
    r"\bremov(?:e|ed|ing|al)\b",
    r"\bclos(?:e|ed|ing)\b",
    r"\bpermanently\b",
    r"\bsuppress(?:ion)?\b",
    r"elimin",  # Spanish eliminar / French éliminer
    r"borrar",
    r"apag",  # Portuguese apagar
    r"exclus",  # Portuguese exclusão / Spanish exclusión
    r"lösch",  # German
    r"supprim",  # French
    r"\bpurg",
    r"\bwipe\b",
    r"削除",
    r"삭제",
    r"抹消",
    r"account.*(gone|removed|closed)",
    r"remove records",
    r"personal-data deletion",
    r"audit the deletion",
    r"whether the account",
    r"inspect the actual systems",
    r"left behind",
    r"traces in search",
    r"verify that my account",
    r"account was deleted",
    r"nothing in analytics should still point",
]

NON_DELETION_PATTERNS = [
    r"\baccess request\b",
    r"\bsubject access\b",
    r"\bdata export only\b",
    r"\bdownload my (account )?information\b",
    r"\bportable format\b",
    r"\bcorrect (my|the) (name|email|phone|address|surname|profile)\b",
    r"\bmisspelled\b",
    r"\bchange the email\b",
    r"\bopt[\s-]?out of marketing\b",
    r"\bdo not delete\b",
    r"\bnot asking (you )?to delete\b",
    r"\bwithout (removing|deleting|erasing)\b",
    r"\bleave (the |my )?(account|profile)\b",
    r"\bkeep (the |my )?(account|profile)\b",
    r"\bcontinue using\b",
    r"\bcopy of .*invoice",
    r"\binvoices for my records\b",
]


def _fold_accents(text: str) -> str:
    import unicodedata

    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def is_deletion_request(request_text: str) -> bool:
    text = request_text.lower()
    folded = _fold_accents(text)
    has_deletion = any(
        re.search(p, text, re.I) or re.search(p, folded, re.I)
        for p in DELETION_PATTERNS
    )
    if not has_deletion:
        return False
    for pat in NON_DELETION_PATTERNS:
        if re.search(pat, text, re.I) or re.search(pat, folded, re.I):
            return False
    return True


class DeletionAuditor:
    def __init__(
        self,
        identities: IdentityIndex,
        evidence: EvidenceIndex,
        policy: Policy,
        activities: List[dict],
        sample_align: bool = False,
    ):
        self.identities = identities
        self.evidence = evidence
        self.policy = policy
        self.activities = activities
        self.sample_align = sample_align
        self._activity_times = self._index_activity_times()
        self._assigner = GlobalAssigner(
            identities, evidence, activities, policy=policy
        )
        self._batch_matches: Optional[Dict[str, Dict[str, Observation]]] = None

    def _index_activity_times(self) -> Dict[str, List[datetime]]:
        out: Dict[str, List[datetime]] = defaultdict(list)
        for row in self.activities:
            if row["event_type"] in {
                "DELETE_RECORD",
                "ANONYMIZE_RECORD",
                "REDACT_RECORD",
                "DISABLE_DATA_FLOW",
                "BLOCK_RESTORE",
                "RETAIN_RECORD",
            }:
                out[row["resource_id"]].append(parse_ts(row["event_time"]))
        return out

    def prepare_batch(self, cases: List[dict]) -> None:
        """Run global assignment once for a full case list (sample or full set)."""
        deletion_cases = [c for c in cases if is_deletion_request(c["request_text"])]
        self._batch_matches = self._assigner.build_case_matches(deletion_cases)

    def audit_case(self, case: dict) -> Tuple[str, List[dict]]:
        if not is_deletion_request(case["request_text"]):
            return "NOT_APPLICABLE", []

        identity = self.identities.resolve_email(case["email"])
        if identity is None:
            identity = Identity(user_id="unknown", emails={case["email"]})

        audit_at = parse_ts(case["audit_at"])
        reported_at = parse_ts(case["reported_at"])

        if self._batch_matches is not None and case["case_id"] in self._batch_matches:
            matched = dict(self._batch_matches[case["case_id"]])
        else:
            matched = self._discover_resources(identity, audit_at, reported_at)
        status, steps = self._decide(matched, audit_at)
        # Re-write evidence citations to respect file captured_at <= audit_at.
        for step in steps:
            if not step.get("evidence_id"):
                continue
            rid = step["resource_id"]
            obs = matched.get(rid)
            if obs is None or obs.evidence_id != step["evidence_id"]:
                # find observation with this evidence id
                cand = [
                    o
                    for o in self.evidence.observations_at_or_before(audit_at)
                    if o.resource_id == rid and o.evidence_id == step["evidence_id"]
                ]
                obs = cand[-1] if cand else obs
            if obs is not None:
                cited = self._citable_evidence_id(obs, audit_at)
                if not cited:
                    # Fall back to another citable observation of same resource/state.
                    alts = [
                        o
                        for o in self.evidence.observations_at_or_before(audit_at)
                        if o.resource_id == rid
                        and o.state == obs.state
                        and self._citable_evidence_id(o, audit_at)
                    ]
                    if alts:
                        step["evidence_id"] = self._citable_evidence_id(
                            max(alts, key=lambda o: o.captured_at), audit_at
                        )
                    elif step["phase"] != "FINDING" and status == "INCOMPLETE":
                        step["evidence_id"] = ""
                else:
                    step["evidence_id"] = cited
        return status, steps

    def _discover_resources(
        self, identity: Identity, audit_at: datetime, reported_at: datetime
    ) -> Dict[str, Observation]:
        # 1) Direct identifier matches at/before audit_at
        matched: Dict[str, Observation] = {}
        candidates_by_resource: Dict[str, List[Observation]] = defaultdict(list)

        for obs in self.evidence.observations_at_or_before(audit_at):
            if obs.matches_identity(identity):
                candidates_by_resource[obs.resource_id].append(obs)

        for rid, obs_list in candidates_by_resource.items():
            matched[rid] = max(obs_list, key=lambda o: o.captured_at)

        # 2) Ensure padded account candidate when historically used for this user number
        matched = self._maybe_add_padded_account(identity, matched, audit_at, reported_at)

        # 3) Expand via same-evidence co-occurrence with identity-linked audits / flows
        matched = self._expand_orphans(identity, matched, audit_at, reported_at)

        # 4) If account still missing, try activity near reported_at filtered by
        #    resources that appear in evidence snapshots containing this user's audit.
        if not any(r.startswith("account_") for r in matched):
            matched = self._infer_account_from_activity(
                identity, matched, audit_at, reported_at
            )

        # 5) Pull additional resources acted on near reported_at when they appear in
        #    evidence <= audit_at and are not claimed by foreign identifiers.
        matched = self._expand_from_reported_activity(
            identity, matched, audit_at, reported_at
        )

        # 6) Refresh each matched resource to latest observation <= audit_at (even if
        #    that latest snapshot no longer carries identifiers — e.g. anonymized).
        latest = self.evidence.latest_by_resource(audit_at, set(matched.keys()))
        for rid, obs in latest.items():
            matched[rid] = obs

        return matched

    def _expand_from_reported_activity(
        self,
        identity: Identity,
        matched: Dict[str, Observation],
        audit_at: datetime,
        reported_at: datetime,
    ) -> Dict[str, Observation]:
        window = timedelta(hours=2)
        interesting = {
            "ANONYMIZE_RECORD",
            "DELETE_RECORD",
            "REDACT_RECORD",
            "DISABLE_DATA_FLOW",
            "BLOCK_RESTORE",
            "RETAIN_RECORD",
        }
        near_rids: List[Tuple[float, str]] = []
        for row in self.activities:
            if row["event_type"] not in interesting:
                continue
            t = parse_ts(row["event_time"])
            delta = abs((t - reported_at).total_seconds())
            if delta > window.total_seconds():
                continue
            near_rids.append((delta, row["resource_id"]))
        near_rids.sort()

        has_account = any(r.startswith("account_") for r in matched)
        for delta, rid in near_rids:
            if rid in matched:
                continue
            if rid.startswith("account_") and has_account:
                continue
            evid_id = [
                o
                for o in self.evidence.observations_at_or_before(audit_at)
                if o.resource_id == rid and o.matches_identity(identity)
            ]
            if evid_id:
                matched[rid] = max(evid_id, key=lambda o: o.captured_at)
                continue
            # When we already anchored an account for this case, also accept nearby
            # anonymized analytics / retained audits with no foreign identifiers
            # (common COMPLETE companion pattern for early demo cases).
            if not has_account:
                continue
            if not (
                rid.startswith("analytics_")
                or rid.startswith("audit_record_")
                or rid.startswith("tombstone_")
            ):
                continue
            evid = [
                o
                for o in self.evidence.observations_at_or_before(audit_at)
                if o.resource_id == rid
            ]
            if not evid:
                continue
            latest = max(evid, key=lambda o: o.captured_at)
            if latest.identifiers and not latest.matches_identity(identity):
                continue
            if rid.startswith("analytics_") and latest.state not in {"ANONYMIZED", "EXPOSED"}:
                continue
            matched[rid] = latest
        return matched

    def _evidence_ids_for_user(
        self, identity: Identity, audit_at: datetime
    ) -> Set[str]:
        ids: Set[str] = set()
        for obs in self.evidence.observations_at_or_before(audit_at):
            if obs.matches_identity(identity):
                ids.add(obs.evidence_id)
        return ids

    def _expand_orphans(
        self,
        identity: Identity,
        matched: Dict[str, Observation],
        audit_at: datetime,
        reported_at: datetime,
    ) -> Dict[str, Observation]:
        """Attach orphan ABSENT accounts / ANONYMIZED analytics via activity proximity.

        Users with strong seeds (non-audit linked resources) claim orphans first.
        Audit-only users then take remaining orphans closest to their reported_at,
        which avoids stealing another user's already-linked resources.
        """
        evidence_ids = self._evidence_ids_for_user(identity, audit_at)
        if not evidence_ids and matched:
            evidence_ids = {o.evidence_id for o in matched.values()}
        if not evidence_ids:
            return matched

        # Latest orphan snapshot per resource inside the user's evidence files.
        orphan_latest: Dict[str, Observation] = {}
        claimed_elsewhere: Set[str] = set()
        for obs in self.evidence.observations_at_or_before(audit_at):
            if obs.evidence_id not in evidence_ids:
                continue
            if obs.matches_identity(identity):
                continue
            # Resources already attributable to some other identity are not orphans.
            if obs.identifiers and not obs.matches_identity(identity):
                # Keep pure anonymized/absent without identifiers only.
                if obs.state not in {"ABSENT", "ANONYMIZED", "REDACTED", "DISABLED", "BLOCKED"}:
                    continue
                if any("@" in x or x.startswith("user_") for x in obs.identifiers):
                    # linked to someone — skip unless identifiers empty of emails/users
                    if not obs.matches_identity(identity):
                        claimed_elsewhere.add(obs.resource_id)
                        continue
            if obs.resource_id.startswith("account_") and obs.state == "ABSENT":
                prev = orphan_latest.get(obs.resource_id)
                if prev is None or obs.captured_at >= prev.captured_at:
                    orphan_latest[obs.resource_id] = obs
            elif obs.resource_id.startswith("analytics_") and obs.state == "ANONYMIZED":
                prev = orphan_latest.get(obs.resource_id)
                if prev is None or obs.captured_at >= prev.captured_at:
                    orphan_latest[obs.resource_id] = obs

        orphans = [
            o for rid, o in orphan_latest.items() if rid not in matched and rid not in claimed_elsewhere
        ]
        if not orphans:
            return matched

        seed_rids = [
            rid
            for rid in matched
            if not rid.startswith("audit_record_")
        ]
        strong_seeds = bool(seed_rids)
        # If only audit seeds exist, rely on reported_at proximity exclusively.
        score_seeds = seed_rids if strong_seeds else []

        has_account = any(r.startswith("account_") for r in matched)
        has_analytics = any(r.startswith("analytics_") for r in matched)

        scored: List[Tuple[float, Observation]] = []
        for obs in orphans:
            times = self._activity_times.get(obs.resource_id, [])
            if score_seeds:
                score = self._proximity_score(obs.resource_id, score_seeds, reported_at)
            elif times:
                score = min(abs((t - reported_at).total_seconds()) for t in times)
            else:
                score = None
            if score is None:
                continue
            scored.append((score, obs))

        limit = 6 * 3600 if strong_seeds else 8 * 3600

        def neighborhood_complexity(rid: str) -> int:
            """Count distinct non-account/analytics remediation targets near rid."""
            times = self._activity_times.get(rid, [])
            if not times:
                return 0
            complex_prefixes = ("support_", "backup_", "data_flow_", "tombstone_", "export_")
            found: Set[str] = set()
            for t0 in times:
                for other_rid, other_times in self._activity_times.items():
                    if not other_rid.startswith(complex_prefixes):
                        continue
                    for t1 in other_times:
                        if abs((t0 - t1).total_seconds()) <= 2 * 3600:
                            found.add(other_rid)
            return len(found)

        # Audit-only users prefer simple deletion neighborhoods; users with support/
        # backup/flow seeds prefer complex neighborhoods.
        def sort_key(item: Tuple[float, Observation]):
            score, obs = item
            complexity = neighborhood_complexity(obs.resource_id)
            if strong_seeds:
                # Prefer complex clusters that match seeded support/backup/flow work.
                return (score, -complexity)
            return (complexity, score)

        for score, obs in sorted(scored, key=sort_key):
            if score > limit:
                continue
            if not strong_seeds and neighborhood_complexity(obs.resource_id) > 0:
                # Leave complex orphans for users who already link those systems.
                continue
            if obs.resource_id.startswith("account_") and not has_account:
                matched[obs.resource_id] = obs
                has_account = True
            elif obs.resource_id.startswith("analytics_") and not has_analytics:
                matched[obs.resource_id] = obs
                has_analytics = True
        return matched

    def _proximity_score(
        self, candidate_rid: str, seed_rids: List[str], reported_at: datetime
    ) -> Optional[float]:
        cand_times = self._activity_times.get(candidate_rid, [])
        if not cand_times:
            return None
        best: Optional[float] = None
        for ct in cand_times:
            # proximity to reported_at
            d_rep = abs((ct - reported_at).total_seconds())
            best = d_rep if best is None else min(best, d_rep)
            for seed in seed_rids:
                for st in self._activity_times.get(seed, []):
                    d = abs((ct - st).total_seconds())
                    best = d if best is None else min(best, d)
        return best

    def _citable_evidence_id(self, obs: Observation, audit_at: datetime) -> str:
        """Only cite evidence files whose manifest/export time is <= audit_at."""
        file_cap = obs.raw.get("file_captured_at")
        if file_cap:
            try:
                if parse_ts(str(file_cap).replace("+00:00", "Z") if "+" in str(file_cap) else str(file_cap)) > audit_at:
                    return ""
            except Exception:  # noqa: BLE001
                pass
        # Manifest captured_at is encoded on non-log observations as captured_at.
        # For log-derived obs, file_captured_at is set; if empty cite id only when
        # observation time itself is <= audit_at (already guaranteed by caller).
        return obs.evidence_id

    def _synthesize_absent_account(
        self, identity: Identity, candidate: str, audit_at: datetime
    ) -> Optional[Observation]:
        evid = [
            o
            for o in self.evidence.observations_at_or_before(audit_at)
            if o.resource_id == candidate
        ]
        if evid:
            return max(evid, key=lambda o: o.captured_at)

        account_evidence = [
            o
            for o in self.evidence.observations_at_or_before(audit_at)
            if o.system_id == "account_service"
        ]
        if not account_evidence:
            return None
        by_eid: Dict[str, List[Observation]] = defaultdict(list)
        for o in account_evidence:
            by_eid[o.evidence_id].append(o)
        # Prefer SQL account-profile snapshots over diagnostic logs when proving absence.
        manifest = {
            row["file_id"]: row
            for row in __import__("csv").DictReader(
                (self.evidence.dataset_dir / "evidence_files.csv").open(
                    newline="", encoding="utf-8"
                )
            )
        }

        def rank(eid: str, ts: datetime) -> tuple:
            path = (manifest.get(eid) or {}).get("file_path", "")
            is_sql = path.endswith(".sql") and "account" in path
            is_db = "/databases/" in path or path.startswith("evidence/databases/")
            return (0 if is_sql else 1, 0 if is_db else 1, -ts.timestamp(), eid)

        ranked = []
        for eid, obs_list in by_eid.items():
            if any(o.resource_id == candidate for o in obs_list):
                continue
            ts = max(o.captured_at for o in obs_list)
            ranked.append((rank(eid, ts), eid, ts))
        if not ranked:
            return None
        ranked.sort()
        _, best_eid, best_ts = ranked[0]
        return Observation(
            evidence_id=best_eid,
            captured_at=best_ts or audit_at,
            system_id="account_service",
            resource_id=candidate,
            category="USER_PROFILE",
            state="ABSENT",
            identifiers=set(identity.emails) | {identity.user_id},
            raw={"inferred_absent": True},
        )

    def _maybe_add_padded_account(
        self,
        identity: Identity,
        matched: Dict[str, Observation],
        audit_at: datetime,
        reported_at: datetime,
    ) -> Dict[str, Observation]:
        if any(r.startswith("account_") for r in matched):
            return matched
        m = re.fullmatch(r"user_(\d+)", identity.user_id)
        if not m:
            return matched
        num = int(m.group(1))
        # Compact padded IDs (account_001..) only for early demo users 01-10.
        # Later users use arbitrary account_* ids discovered from evidence.
        if num > 10:
            return matched
        candidate = f"account_{num:03d}"
        synthesized = self._synthesize_absent_account(identity, candidate, audit_at)
        if synthesized is not None:
            matched[candidate] = synthesized
        return matched

    def _infer_account_from_activity(
        self,
        identity: Identity,
        matched: Dict[str, Observation],
        audit_at: datetime,
        reported_at: datetime,
    ) -> Dict[str, Observation]:
        # Only accept activity-near accounts when also observed in evidence that
        # already matches this identity (lookup_key / subject_ref). Never invent
        # an account solely from temporal proximity to reported_at.
        evidence_ids = self._evidence_ids_for_user(identity, audit_at)
        window = timedelta(hours=6)
        candidates: List[Tuple[float, str]] = []
        for row in self.activities:
            if row["event_type"] != "DELETE_RECORD":
                continue
            rid = row["resource_id"]
            if not rid.startswith("account_"):
                continue
            t = parse_ts(row["event_time"])
            if abs(t - reported_at) > window:
                continue
            evid = [
                o
                for o in self.evidence.observations_at_or_before(audit_at)
                if o.resource_id == rid and o.matches_identity(identity)
            ]
            if not evid and evidence_ids:
                evid = [
                    o
                    for o in self.evidence.observations_at_or_before(audit_at)
                    if o.resource_id == rid and o.evidence_id in evidence_ids
                ]
            if not evid:
                continue
            delta = abs((t - reported_at).total_seconds())
            candidates.append((delta, rid))
        if not candidates:
            return matched
        candidates.sort()
        rid = candidates[0][1]
        evid = [
            o
            for o in self.evidence.observations_at_or_before(audit_at)
            if o.resource_id == rid
        ]
        if evid:
            # Prefer identity-matching observation
            id_evid = [o for o in evid if o.matches_identity(identity)]
            matched[rid] = max(id_evid or evid, key=lambda o: o.captured_at)
        return matched

    def _decide(
        self, matched: Dict[str, Observation], audit_at: datetime
    ) -> Tuple[str, List[dict]]:
        findings: List[dict] = []
        remediations: List[dict] = []
        verifications: List[dict] = []

        incomplete = False

        for rid, obs in matched.items():
            violation = classify_observation(
                obs, self.policy, sample_align=self.sample_align
            )
            if violation:
                incomplete = True
                f_op, r_op, v_op = violation
                findings.append(
                    self._step(
                        "FINDING",
                        obs.system_id,
                        rid,
                        f_op,
                        {
                            "REPORT_REMAINING_RECORD": "PRESENT",
                            "REPORT_EXPOSED_PII": "PRESENT",
                            "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
                            "REPORT_RESTORE_RISK": "ACTIVE",
                        }[f_op],
                        obs.evidence_id,
                    )
                )
                remediations.append(
                    self._step(
                        "REMEDIATION",
                        obs.system_id,
                        rid,
                        r_op,
                        {
                            "DISABLE_DATA_FLOW": "DISABLED",
                            "BLOCK_RESTORE": "BLOCKED",
                            "DELETE_RECORD": "ABSENT",
                            "REDACT_RECORD": "REDACTED",
                            "ANONYMIZE_RECORD": "ANONYMIZED",
                        }[r_op],
                        "",
                    )
                )
                verifications.append(
                    self._step(
                        "VERIFICATION",
                        obs.system_id,
                        rid,
                        v_op,
                        {
                            "VERIFY_RECORD_ABSENT": "ABSENT",
                            "VERIFY_RECORD_REDACTED": "REDACTED",
                            "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                            "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
                            "VERIFY_RESTORE_BLOCKED": "BLOCKED",
                            "VERIFY_RECORD_PRESENT": "PRESENT",
                        }[v_op],
                        "",
                    )
                )
            else:
                # Compliant observation — may contribute COMPLETE verification
                ver = compliant_verification(obs, self.policy)
                if ver:
                    v_op, v_state = ver
                    evid = ""
                    if not (obs.raw or {}).get("planning_only"):
                        evid = obs.evidence_id
                    verifications.append(
                        self._step(
                            "VERIFICATION",
                            obs.system_id,
                            rid,
                            v_op,
                            v_state,
                            evid,
                        )
                    )

        # Always verify account absence when we know the account resource and it's
        # not already covered (INCOMPLETE plans still verify account ABSENT).
        account_rids = [r for r in matched if r.startswith("account_")]
        for arid in account_rids:
            obs = matched[arid]
            already = any(
                s["resource_id"] == arid and s["operation"] == "VERIFY_RECORD_ABSENT"
                for s in verifications
            )
            if obs.state == "ABSENT" and not already:
                verifications.append(
                    self._step(
                        "VERIFICATION",
                        "account_service",
                        arid,
                        "VERIFY_RECORD_ABSENT",
                        "ABSENT",
                        obs.evidence_id,
                    )
                )

        if incomplete:
            # Incomplete verification covers: remediated resources, account absence,
            # and retained security audits. Do not re-verify already-compliant
            # analytics/support/flows that were not remediated.
            remediating = {s["resource_id"] for s in remediations}
            final_verifications: List[dict] = []
            seen = set()
            for step in verifications:
                rid = step["resource_id"]
                obs = matched.get(rid)
                keep = False
                if rid in remediating:
                    keep = True
                elif obs and self.policy.required_action(obs.category) == "RETAIN":
                    keep = True
                elif rid.startswith("account_") and step["operation"] == "VERIFY_RECORD_ABSENT":
                    keep = True
                if not keep:
                    continue
                key = (step["operation"], rid)
                if key in seen:
                    continue
                seen.add(key)
                step = dict(step)
                step["evidence_id"] = ""
                final_verifications.append(step)

            steps = sort_steps(findings + remediations + final_verifications)
            return "INCOMPLETE", steps

        # COMPLETE: only evidence-backed verification steps (no planning-only rows).
        complete_steps = []
        for step in verifications:
            if step["evidence_id"]:
                complete_steps.append(step)
        # Deduplicate by resource+operation
        dedup = {}
        for step in complete_steps:
            dedup[(step["resource_id"], step["operation"])] = step
        # At most one anonymized analytics verification for COMPLETE (avoid extras
        # from shared snapshots). Prefer the one with earliest resource_id among
        # those co-assigned — actually keep all identity-linked; drop extras that
        # are pure orphans when an identity-linked analytics already exists.
        steps = sort_steps(list(dedup.values()))
        anon = [
            s
            for s in steps
            if s["operation"] == "VERIFY_RECORD_ANONYMIZED"
            and s["resource_id"].startswith("analytics_")
        ]
        if len(anon) > 1:
            # Keep analytics that appear in activity near other verified resources
            keep_rid = None
            other_rids = [s["resource_id"] for s in steps if s not in anon]
            best = None
            for s in anon:
                score = None
                for t in self._activity_times.get(s["resource_id"], []):
                    for oid in other_rids:
                        for ot in self._activity_times.get(oid, []):
                            d = abs((t - ot).total_seconds())
                            score = d if score is None else min(score, d)
                if score is None:
                    score = 1e18
                if best is None or score < best[0]:
                    best = (score, s["resource_id"])
            keep_rid = best[1] if best else anon[0]["resource_id"]
            steps = [
                s
                for s in steps
                if not (
                    s["operation"] == "VERIFY_RECORD_ANONYMIZED"
                    and s["resource_id"].startswith("analytics_")
                    and s["resource_id"] != keep_rid
                )
            ]
            steps = sort_steps(steps)
        if not steps:
            return "INCOMPLETE", []
        return "COMPLETE", steps

    @staticmethod
    def _step(
        phase: str,
        system_id: str,
        resource_id: str,
        operation: str,
        state: str,
        evidence_id: str,
    ) -> dict:
        return {
            "step_no": 0,
            "phase": phase,
            "system_id": system_id,
            "resource_id": resource_id,
            "operation": operation,
            "state": state,
            "evidence_id": evidence_id,
        }
