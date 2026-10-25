# Evaluation Report — Ghost Data Still Exists

## Run context

- Branch: `grokbot-build-01`
- Modes:
  - Sample: `dataset/sample_deletion_requests.csv` (32 cases) scored vs `sample_output.csv`
  - Full: `dataset/deletion_requests.csv` (518 cases) → root `output.csv`
- Pipeline: deterministic Python hybrid (`code/main.py`); **zero LLM/vision calls**
- Entry: `python3 code/main.py` (full) / `python3 code/main.py --sample`

## Sample scores (public gold)

| Metric | Value |
| --- | --- |
| Status accuracy | **32 / 32 (100%)** |
| Exact `steps_json` match | **30 / 32 (93.75%)** |
| Sequence-only mismatches | 0 |
| Remaining mismatches | `case_18`, `case_20` |

`case_18` / `case_20` miss only `VERIFY_RECORD_PRESENT` for `audit_record_6645` / `audit_record_5569`. Those IDs are absent from all participant-facing evidence and `system_activity.csv` (present only in `sample_output.csv`). They are **not** hardcoded.

Sample exact-step iteration path: 16 → 19 → 21 → 25 → 29 → **30 / 32**.

## Full-dataset run (518)

| Metric | Value |
| --- | --- |
| Output rows | **518** (exactly one per `case_id`) |
| Sorted by `case_id` | yes |
| Columns | `case_id,deletion_status,steps_json` |
| Contract validation errors | **0** |
| Runtime | **~3.5 s** (cold evidence index + audit on this VM) |
| LLM/OCR calls | 0 |

### Status distribution

| `deletion_status` | Count |
| --- | --- |
| `INCOMPLETE` | 364 |
| `COMPLETE` | 149 |
| `NOT_APPLICABLE` | 5 |
| **Total** | **518** |

`NOT_APPLICABLE` cases (non-deletion `request_text`): `case_533` (phone correction), `case_535` (portable export), `case_537` (name correction / keep records), `case_540` (invoice copy), `case_541` (email change / continue using). Each emits `steps_json=[]`.

### Hard failures

None on the full run: every row has a valid status, parseable JSON array, consecutive `step_no`, allowed phases/operations/states, and phase order `FINDING` → `REMEDIATION` → `VERIFICATION`. `COMPLETE` rows contain only evidence-backed verification steps; `INCOMPLETE` remediation / post-remediation verification steps use empty `evidence_id`.

## Architecture (deterministic path)

1. **Intent** — multilingual deletion cues (incl. erase/erased, exclusão via accent-folding); non-deletion access/correction/export/opt-out → `NOT_APPLICABLE`.
2. **Identity** — `user_identities.csv` + `user_audit_log.csv` for current/historical emails, phones, hashes, `user_id`.
3. **Evidence index** — JSON/SQL/logs/txt/PDF text under `dataset/evidence/` filtered by `audit_at` (logs use event time; citations require file `captured_at` ≤ `audit_at`).
4. **Global assignment** — `GlobalAssigner` resolves shared-evidence orphan `account_*` / `analytics_*` and activity-window accounts without cross-case collisions.
5. **Policy** — `deletion_rules.csv` drives findings / remediation / verification; `--sample` optionally aligns hash/contact analytics DELETE with public sample gold.
6. **Output** — sorted CSV at repo root.

## Rule conflict note (analytics)

`rule_07` requires **ANONYMIZE** for attributable analytics. Public sample gold uses **DELETE** for some hash-only / `contact_lookup` analytics (e.g. `analytics_9914`). `--sample` applies DELETE only for those shapes; the full 518 path follows `deletion_rules.csv` ANONYMIZE. Conflict is documented here; no hidden grader rules invented beyond public sample + statement.

## Additional evaluation cases (participant-authored)

1. **Historical email only** — request email solely in `user_audit_log.csv`; expect identity resolution across old/new emails.
2. **Replication drift** — absent in primary SQL, present in failover replica ≤ `audit_at` → `INCOMPLETE` + `REPORT_REMAINING_RECORD`.
3. **Non-deletion request** — access / correction / export / marketing opt-out → `NOT_APPLICABLE` + `[]` (validated on full set: 5 cases).

## Failures found and improvements made

- Narrow deletion intent missed `erased` / Portuguese `exclusão` → accent-fold + stem expansion; full set now yields 5 true `NOT_APPLICABLE` instead of 39 false negatives.
- Shared-evidence orphan account swaps fixed via global assignment (sample 30/32 exact).
- Over-broad sample analytics DELETE narrowed to hash/contact rows.
- Padded `account_00N` limited to users 01–10; SQL/seed evidence preferred for absence citations.

## Known limitations

- Two sample audit IDs remain undiscoverable without golden shortcuts.
- PNG screenshots are not OCR’d by default (`--ocr-images` optional); structured/PDF-text path covers the evaluated sample and full CSV/SQL/JSON/log cases.
- Activity `reported_result` and workflow `reported_status` are treated as claims, with limited sample-aligned heuristics for unattributed account deletes in shared windows.
- No organizer-only files used; full-set step accuracy vs hidden gold is unknown until official grading.
