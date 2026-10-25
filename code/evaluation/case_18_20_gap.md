# Negative proof: `case_18` / `case_20` audit gaps

Sample score remains **32/32 status**, **30/32 exact `steps_json`**.  
The only deltas vs `dataset/sample_output.csv` are:

| Case | Missing gold verification | Status (pred = gold) |
| --- | --- | --- |
| `case_18` | `VERIFY_RECORD_PRESENT` `audit_record_6645` `evidence_id=""` | `INCOMPLETE` |
| `case_20` | `VERIFY_RECORD_PRESENT` `audit_record_5569` `evidence_id=""` | `INCOMPLETE` |

All FINDING / REMEDIATION steps and every other VERIFICATION step already match.  
**No gold IDs were hardcoded. Pipeline unchanged after this probe.**

---

## 1. ID synthesis from case graph (rejected)

Checked whether `audit_record_<N>` shares numeric suffix with any same-case resource in gold (account / support / search / analytics / …).

- For **all 24** sample cases that include an audit, **overlap = false** (audit suffix never equals another resource suffix on that case).
- **Rule A** `support_N → audit_record_N` (same case): **0 / 11** support-bearing cases match gold; for `case_18` predicts `audit_record_5569` but gold wants `audit_record_6645`.
- **Rule C** `account_N → audit_record_N`: **0 / 32** exact matches; `case_18` → `5521`, `case_20` → `5037` (both wrong).
- **Rule B** (cohort): gold reuses `5569` as `support_5569` on **case_18** and `audit_record_5569` on **case_20**. That is a **cross-subject** collision, not a same-identity alias. Using another case’s support ticket as this case’s audit ID is a golden shortcut and is rejected.

**Result:** no consistent synthesis rule lifts 18/20 without inventing cross-case labels or regressing other sample cases.

---

## 2. Byte-wise / path-derived search (zero hits for exact IDs)

Scanned **103 files / ~3.98 MB** under `dataset/` as raw bytes for:

- `audit_record_6645` → **only** in `sample_output.csv`
- `audit_record_5569` → **only** in `sample_output.csv`

Bare `6645` / `5569` non-gold hits (unsafe):

| Token | Hits | Nature |
| --- | --- | --- |
| `6645` | `account_6645` in `system_activity.csv` + one old account log | Unrelated Feb-2026 account; not linked to `user_50` |
| `5569` | `support_5569` (case_18), hash substrings, `CS-5569` for `user_405`, request id noise | Not `audit_record_5569` for `user_48` |

Also checked:

- No sidecar `.txt` / metadata next to the 11 PNGs
- No zip/tar archives under `dataset/`
- Filenames / JSON keys / HTML comments: no `audit_record_6645|5569`

Prior OCR pass over all 11 PNGs (including `security_audit_explorer_20260905.png`) found only demo `user_03` / `audit_record_001`–`006`-style strings — not these IDs.

---

## 3. Retention / tombstone semantics (cannot invent `resource_id`)

- `deletion_rules.csv` `rule_12`: `SECURITY_AUDIT_LOG` → **RETAIN** when within retention.
- `problem_statement.md`: `VERIFY_RECORD_PRESENT` confirms a **policy-required retained record still exists**.
- Tombstone rules address `ACCOUNT_TOMBSTONE` redaction of direct identifiers, not minting audit resource IDs.

A general “always emit some retained audit verification for every deletion” still requires a **real** `resource_id` grounded in evidence. Emitting a placeholder without a discoverable ID cannot exact-match `audit_record_6645` / `5569` and would invent evidence. Not implemented.

---

## 4. Activity → audit bridging (no consistent transform)

Provenance of sample gold audits:

| Group | Count | In evidence with identity match? | In `system_activity` as that `audit_record_*`? |
| --- | --- | --- | --- |
| Other sample audits | 22 | **Yes** (obs ≥ 1) | Usually **no** near `reported_at` |
| `case_18` / `case_20` | 2 | **No** (obs = 0) | **No** (act = 0) |

Near `case_18`/`case_20` `reported_at=2026-09-09T11:00Z`, the only nearby `RETAIN_RECORD` is `audit_record_7343` — not gold for either case. Matching cases’ gold audits are typically **not** the RETAIN events in the same activity window (e.g. `case_15` gold `audit_record_1596` vs nearby retains `5358` / `383`).

**Result:** no empirically consistent activity_id / delete → audit_record transform that holds on matching cases and would recover 6645/5569.

---

## 5. Conclusion

`audit_record_6645` and `audit_record_5569` are **not present in participant-facing data**. They appear only in organizer sample gold (`sample_output.csv`).  

Leaving the pipeline as-is (30/32 exact) is correct. Hardcoding those IDs or copying sample steps would violate the challenge rules.
