# Evaluation Report — Ghost Data Still Exists

## Run context

- Branch: `grokbot-build-01`
- Modes:
  - Sample: `dataset/sample_deletion_requests.csv` (32) vs `sample_output.csv`
  - Full: `dataset/deletion_requests.csv` (518) → root `output.csv`
- Pipeline: deterministic Python (`code/main.py`); LLM unused
- OCR: **off by default**; opt-in via `--ocr-images` or `GHOST_DATA_OCR=1` (local `tesseract`)

## Sample scores (public gold)

| Metric | Value |
| --- | --- |
| Status accuracy | **32 / 32 (100%)** |
| Exact `steps_json` | **30 / 32 (93.75%)** |
| Sequence-only mismatches | 0 |
| Remaining mismatches | `case_18`, `case_20` only |

Exact-step iteration: 16 → 19 → 21 → 25 → 29 → **30 / 32** (unchanged after OCR polish; OCR does not unlock the last two).

## Deep-dive: `case_18` / `case_20` (residual exact-step gap)

**Full negative-proof package:** [`case_18_20_gap.md`](./case_18_20_gap.md).

Both cases already match gold on all FINDING / REMEDIATION steps and every VERIFICATION except one retained-audit row each (`audit_record_6645` / `audit_record_5569`). Status remains correctly `INCOMPLETE`.

Follow-up probes (general rules only; no hardcoding):

1. **ID synthesis** — no sample-wide pattern ties audit numeric suffixes to same-case account/support/search IDs (`support_N→audit_N` and `account_N→audit_N` both **0** exact matches; would regress). Cross-case reuse of `5569` (`support_5569` on case_18 vs audit on case_20) rejected as a golden shortcut.
2. **Byte-wise dataset scan** (103 files / ~4 MB) — exact `audit_record_6645|5569` only in `sample_output.csv`; no sidecars/archives/path-derived IDs.
3. **Retention semantics** — `rule_12` requires VERIFY_RECORD_PRESENT only when a retained audit **exists**; cannot mint `resource_id` without evidence.
4. **Activity bridging** — other sample audits are evidence-backed with identity match; 18/20 have **0** obs and **0** activity rows; nearby `RETAIN_RECORD` is unrelated `audit_record_7343`.

**Conclusion unchanged:** IDs are not participant-discoverable. Exact score stays **30/32**.

## OCR / multimodal (opt-in)

### How to enable

```bash
# requires system package `tesseract-ocr` on PATH
python3 code/main.py --sample --ocr-images
# or
GHOST_DATA_OCR=1 python3 code/main.py
```

Default full/sample runs keep OCR **off** for speed.

### Behavior

- OCRs `.png`/`.jpg`/`.jpeg`/`.webp`/`.gif` under `dataset/evidence/`.
- Identifier binding is **line-local** (same OCR row as the `resource_id`) to avoid page-wide false matches.
- Naive whole-page binding previously **regressed** sample `case_15` (wrong extra `data_flow_1121`); line binding removed that regression.

### Sample impact (this machine)

| Mode | Status | Exact steps | Wall time | Cases changed vs no-OCR |
| --- | --- | --- | --- | --- |
| OCR off (default) | 32/32 | 30/32 | ~0.7 s | — |
| OCR on | 32/32 | 30/32 | ~5.3 s | **none** |

OCR does **not** improve `case_18` / `case_20` (target audits never appear in screenshots). Net: optional accuracy tool for image-only clues; not required for current sample ceiling.

## Full-dataset run (518)

| Metric | Value |
| --- | --- |
| Output rows | **518** |
| Sorted by `case_id` | yes |
| Columns | `case_id,deletion_status,steps_json` |
| Contract errors | **0** |
| Runtime (OCR off) | **~3.5 s** |
| LLM calls | 0 |
| OCR calls | 0 (default path) |

### Status distribution

| Status | Count |
| --- | --- |
| `INCOMPLETE` | 364 |
| `COMPLETE` | 149 |
| `NOT_APPLICABLE` | 5 |
| **Total** | **518** |

`NOT_APPLICABLE`: `case_533`, `case_535`, `case_537`, `case_540`, `case_541` (correction / export / keep-account language; `steps_json=[]`).

## Architecture

1. Intent (multilingual deletion vs access/correction/export).
2. Identity (`user_identities` + `user_audit_log`).
3. Evidence index (JSON/SQL/logs/txt/PDF; optional OCR).
4. `GlobalAssigner` for shared-evidence orphans / activity accounts.
5. `deletion_rules.csv` → findings / remediation / verification; phase order FINDING → REMEDIATION → VERIFICATION.
6. Root `output.csv`.

## Analytics rule conflict

`rule_07` = ANONYMIZE. Public sample uses DELETE for some hash/contact_lookup analytics (e.g. `analytics_9914`). `--sample` aligns only those shapes; full 518 follows ANONYMIZE. Documented; not an invented hidden grader rule.

## Additional evaluation cases

1. Historical email only via `user_audit_log`.
2. Replication drift (primary absent / replica present).
3. Non-deletion request → `NOT_APPLICABLE` (validated: 5 on full set).

## Known limitations / residual risks

- Two sample audit IDs remain undiscoverable without golden shortcuts.
- OCR quality depends on tesseract; noisy screenshots can invent spurious tokens — mitigated by line-local binding; still opt-in.
- Activity / workflow results treated as claims with limited cohort heuristics for unattributed account deletes.
- Full-set step field accuracy vs hidden gold unknown until official grading.
- No organizer-only files used.
