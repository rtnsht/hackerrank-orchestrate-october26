# Evaluation Report: Ghost Data Still Exists

## How to reproduce

```bash
python3 code/main.py --cases sample        # predictions for the 32 public samples
python3 code/evaluation/main.py            # score vs dataset/sample_output.csv (add -v for per-case diffs)
python3 code/evaluation/main.py --custom   # participant-built evaluation cases
python3 code/main.py                       # full run -> ./output.csv (518 rows, contract-validated)
```

The scorer (`code/evaluation/main.py`) parses `steps_json` and compares the step tuple
`(phase, system_id, resource_id, operation, state, evidence_id)`:

* **Case-level status accuracy**: `deletion_status` matches.
* **Step-level precision / recall / F1**: multiset match of step tuples, so order is ignored.
* **Field-level accuracy**: each gold step is compared with the predicted step at the same position, field by field.
* **Sequence accuracy**: the whole ordered `steps_json` array is identical, so order matters.

## Final results on the 32 public samples

| Metric | Value |
|---|---|
| Case-level `deletion_status` accuracy | **100.0 %** (32/32) |
| Sequence accuracy (exact ordered steps) | **75.0 %** (24/32) |
| Step precision | 97.4 % |
| Step recall | 96.9 % |
| Step F1 | **97.1 %** |
| Field accuracy: phase | 98.4 % |
| Field accuracy: system_id | 94.3 % |
| Field accuracy: resource_id | 93.3 % |
| Field accuracy: operation | 94.3 % |
| Field accuracy: state | 94.3 % |
| Field accuracy: evidence_id | 97.9 % |

Full evaluation set (518 requests): every row passes the contract validator built into `code/main.py`:
518 rows, sorted, valid JSON, consecutive step numbers, allowed phase/operation/state triples, phase order,
empty evidence on future steps, and verification-only `COMPLETE` rows. Status split: 355 INCOMPLETE,
153 COMPLETE, 10 NOT_APPLICABLE. There are 3,279 steps in total.

## Participant-built evaluation cases

`code/evaluation/custom_cases.json` holds 4 cases. Each mutates one public sample, and the expected answer was
derived by hand from participant-visible evidence. Result: **4/4 pass**.

| Case | Mutation | What it tests | Expected | Result |
|---|---|---|---|---|
| `custom_01_negated_deletion_words` | case_05 text becomes *"Please send me a copy of the data you hold on this account. I still use it, so do not delete or change anything."* | Intent: deletion words inside an explicit non-deletion request | `NOT_APPLICABLE`, `[]` | pass |
| `custom_02_historical_email` | case_12 email becomes the subject's **previous** address `minjun.kim90@gmail.com` (audit_282) | Identity: resolve through `user_audit_log`. The analytics/marketing record `analytics_9914` is linked only by `correlation_key = sha256(old email)` | Same as gold case_12 | pass |
| `custom_03_uppercase_email` | case_14 email becomes `Y.TANAKA32@GMAIL.COM` | Identity: case-insensitive matching (evidence also stores upper-case emails) | Same as gold case_14 | pass |
| `custom_04_audit_before_readback` | case_21 `audit_at` moves from 12:00 to 09:00 on 2026-09-15 | Time cutoff: the 11:44 readback `evidence_92` did not exist yet and must not be cited. Only the post-request inventory `evidence_90` (2026-09-05) proves the profile absent | COMPLETE: `account_6440` ABSENT via `evidence_90`; audit record PRESENT and analytics ANONYMIZED with no citable evidence | pass |

## Iteration history: failures found and improvements made

| # | Change | Status acc. | Step F1 | Sequence |
|---|---|---|---|---|
| 0 | MVP: generic loaders, identity graph with sha256 of historical emails, latest observation at or before `audit_at`, rules-to-steps planner, keyword intent | 81.3 % | 0.794 | 18.8 % |
| 1 | Snapshot bundles listed only their own subjects but were treated as complete listings, which hid retained security records. Bundles became subject-scoped. Linking now uses all evidence, while state uses only evidence at or before `audit_at`. Fixed French `suppression` and "copy of my profile" intent cues | 87.5 % | 0.875 | 40.6 % |
| 2 | **Timestamp attribution.** Resources with no identifiers (404 account lookups, already-anonymized analytics) can only be tied to a request through the deletion-worker burst around `reported_at`. These are assigned globally across all 550 requests by elimination | 87.5 % | 0.898 | 50.0 % |
| 3 | Stage-specific windows: submissions come about 45–75 min before the report, policy actions about 29 min before, final results and checks 35–115 min after | 87.5 % | 0.901 | 53.1 % |
| 4 | Found that the account-submission result always matches `reported_status` (SUCCESS→COMPLETE, PARTIAL→PARTIALLY_COMPLETE, FAILED→NOT_STARTED) and used it as a constraint. Added a preference for resources observed close to the case's audit | 87.5 % | 0.910 | 56.3 % |
| 5 | A case takes at most one orphan per system, and this is checked before accepting even a single candidate, because some orphans belong to no request | 87.5 % | 0.914 | 62.5 % |
| 6 | **Per-copy state** (primary, read, failover, reporting and regional replicas, support replica). A replica copy seen after the request stays a ghost even after the primary is deleted. Support contact-index copies have no conversation context, so rule_05 does not allow redaction and they are deleted. Fixed the collection-to-system mapping for single-collection JSON | 96.9 % | 0.958 | 71.9 % |
| 7 | Screenshot and PDF extraction (cached Claude vision), data-catalog links with alias resolution (`delivery_confirmation_19.pdf` → `object_19`, `NST-2026-0718-1842` → `export_1842`, `user_17` → `tombstone_17`), and negation-aware intent (exactly the 10 non-deletion requests) | **100 %** | **0.971** | **75.0 %** |
| 8 | Hardening without changing sample scores: repeated requests by one subject split identifier evidence by request date; replica dumps are subject-scoped; listings prove absence only when taken after the request | 100 % | 0.971 | 75.0 % |

### Remaining sample failures (8/32), all diagnosed

* **Unrecoverable from participant data (3 cases).** `account_004` (case_04), `audit_record_6645` (case_18)
  and `audit_record_5569` (case_20) appear in no evidence file and no activity row, so no evidence-based
  method can recover them.
* **Ambiguous identifier-less analytics profiles (5 cases: 09, 20, 24, 25, 26).** Several requests reported
  at the same instant each get an "anonymize, policy action completed" event at the same offset, and the
  profiles carry no identifiers in any evidence. Elimination plus audit-proximity resolves most clusters.
  In the remaining ties a stable order is used, and orphans that fit no request are dropped rather than
  guessed.
* **Account tie (case_23).** Two account submissions with the same result in one burst, and neither has
  evidence. Accounts are always tie-broken because every request owns exactly one profile.

## Design decisions validated by the samples

* Reported statuses and activity results are treated only as **claims**. Proof comes from snapshots,
  lookups, readbacks, listings and screenshots.
* Absence is proven by a 404 / `rows=0` lookup, a NOT_FOUND line, or a complete listing taken after the
  request that omits the record.
* Retained categories (security audit logs, legal holds) always get `VERIFY_RECORD_PRESENT`. Archives get
  `VERIFY_RESTORE_BLOCKED`. Tombstones keep `subject_reference` and must have no name, email, phone or hash.
* Step order: phase, then the operation-table order, then `system_id`, then `resource_id`. Remediation
  disables flows and blocks restores before changing records.
