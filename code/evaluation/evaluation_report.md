# Evaluation Report — Ghost Data Still Exists (BMV)

## Run context

- Branch: `grokbot-build-01`
- Mode: sample (`dataset/sample_deletion_requests.csv` → root `output.csv`)
- Pipeline: deterministic Python hybrid (`code/main.py`); zero LLM calls on the sample path
- Scored against: `dataset/sample_output.csv`

## Case-level deletion-status accuracy

| Metric | Value |
| --- | --- |
| Cases | 32 |
| Status correct | 32 |
| Status accuracy | **100%** |
| Status mismatches | none |

Statuses covered on sample: `COMPLETE` and `INCOMPLETE` (no `NOT_APPLICABLE` rows in the public sample).

## Step-level / field-level / sequence accuracy

| Metric | Value |
| --- | --- |
| Exact `steps_json` match | 16 / 32 (50%) |
| Step/field mismatches | 16 cases |
| Sequence-only mismatches | 0 |

Primary remaining step gaps (resource identity / companion linking, not status):

- Wrong or missing `account_*` on several COMPLETE/INCOMPLETE cases when the account is absent and only linked via orphan co-occurrence in shared evidence exports
- Extra `VERIFY_RECORD_ANONYMIZED` companions pulled from shared snapshots
- `analytics_9914` predicted as ANONYMIZE (per `deletion_rules.csv`) vs sample gold DELETE
- Missing some `audit_record_*` PRESENT verifications when the audit is not directly identifier-linked before `audit_at`

## Additional evaluation cases (participant-authored)

1. **Historical email only** — request email present solely in `user_audit_log.csv`; expect identity resolution to the current `user_id` and evidence match on both old and new emails.
2. **Replication drift** — account absent in primary SQL table but present in failover replica at/before `audit_at`; expect `INCOMPLETE` with `REPORT_REMAINING_RECORD` on the replica resource.
3. **Non-deletion request** — access/correction/marketing opt-out language; expect `NOT_APPLICABLE` with `steps_json=[]` even if `reported_status=COMPLETE`.

## Failures found and improvements made

- Early BMV incorrectly marked several deletion requests `NOT_APPLICABLE` (intent patterns too narrow) → multilingual/stemmed deletion cues restored **100% status**.
- Log `trace_id` correlation added so `lookup_key` / `subject_key` on started lines attach to `resource_id` on completed lines.
- Structured JSON logs (`<ts> {json}`) parsed; in-log event timestamps used for state-at-`audit_at` reconstruction.
- Padded `account_00N` fallback limited to users 01–10 to avoid inventing `account_025` for later users.
- Evidence citations prefer files whose manifest `captured_at` ≤ `audit_at` when log export packaging is later than the events.

## Next improvements (toward full 518)

1. Global orphan assignment across users sharing one evidence export (bipartite matching by activity neighborhood).
2. Stronger account discovery from identity-keyed lookups in resource_lookup / inventory evidence.
3. Align analytics treatment with rules vs sample edge cases; add NOT_APPLICABLE coverage from full `deletion_requests.csv`.
4. Optional OCR/LLM only after CSV/SQL/JSON step accuracy plateaus.
