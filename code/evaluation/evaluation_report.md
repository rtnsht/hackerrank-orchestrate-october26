# Evaluation Report — Ghost Data Still Exists

## Run context

- Branch: `grokbot-llm-01` (stacked on `grokbot-build-01`)
- Modes:
  - Sample: `dataset/sample_deletion_requests.csv` (32) vs `sample_output.csv`
  - Full: `dataset/deletion_requests.csv` (518) → root `output.csv`
- Pipeline: deterministic Python core + OpenAI-compatible LLM intent (`code/llm.py`)
- This cloud VM run: **no API key** → regex intent fallback (0 model calls). Live LLM path covered by mocked unit tests.

## Sample scores (public gold)

| Metric | Before LLM wiring | After (this PR, fallback) |
| --- | --- | --- |
| Status accuracy | **32 / 32 (100%)** | **32 / 32 (100%)** |
| Exact `steps_json` | **30 / 32 (93.75%)** | **30 / 32 (93.75%)** |
| Sequence-only mismatches | 0 | 0 |
| Remaining mismatches | `case_18`, `case_20` | `case_18`, `case_20` |

No regression under `--sample` (sample_align on). Residual exact-step gap remains the undiscoverable audit IDs documented in [`case_18_20_gap.md`](./case_18_20_gap.md) — not hardcoded.

## Intent classification (LLM + regex fallback)

| Path | Behavior |
| --- | --- |
| API key set | `POST {BASE}/chat/completions` with JSON-schema intent `{DELETION\|NOT_DELETION, confidence}`; pydantic validate; disk cache by text hash |
| No key / HTTP error / schema fail | `is_deletion_request_regex` |
| LLM role | Intent only (optional grounded evidence mention assist). Never emits `steps_json` or ungounded `resource_id`s |

### Intent fixes on full set (fallback regex strengthened + LLM-ready)

| Case | Request gist | Status |
| --- | --- | --- |
| `case_534` | marketing stop; “not asking for deletion” | `NOT_APPLICABLE` `[]` |
| `case_542` | access copy; “don't delete” | `NOT_APPLICABLE` `[]` |
| `case_533`/`535`/`537`/`540`/`541` | correction / export / keep account | `NOT_APPLICABLE` `[]` |

Before this PR, `case_534` and `case_542` were incorrectly treated as deletion requests (regex miss on “for deletion” / “don't delete”).

## Full-dataset run (518)

| Metric | Value |
| --- | --- |
| Output rows | **518** |
| Sorted by `case_id` | yes |
| Columns | `case_id,deletion_status,steps_json` |
| Contract errors | **0** |
| Runtime (OCR off, no API key) | **~3.2 s** |
| LLM API calls | **0** (no key in this environment) |

### Status distribution (after intent fixes)

| Status | Count |
| --- | --- |
| `INCOMPLETE` | 364 |
| `COMPLETE` | 147 |
| `NOT_APPLICABLE` | **7** |
| **Total** | **518** |

Previously N/A was 5; +2 from `case_534` / `case_542`.

## Architecture

1. **Intent** — LLM-first (`code/llm.py`) with multilingual regex fallback (`auditor.is_deletion_request_regex`).
2. Identity (`user_identities` + `user_audit_log`).
3. Evidence index (JSON/SQL/logs/txt/PDF; optional OCR; optional grounded LLM mention assist).
4. `GlobalAssigner` for shared-evidence orphans / activity accounts.
5. `deletion_rules.csv` → findings / remediation / verification.
6. Root `output.csv` + `llm_usage.json` / `code/evaluation/usage_report.md`.

## Unit tests

```bash
cd code && python3 -m unittest tests.test_llm -v
```

Covers: regex multilingual + negation, mocked HTTP success/cache, schema failure → fallback, HTTP 500 → fallback, evidence grounding rejects hallucinated IDs, disabled client without key.

## Additional evaluation cases

1. Historical email only via `user_audit_log`.
2. Replication drift (primary absent / replica present).
3. Non-deletion / negated deletion → `NOT_APPLICABLE` (now 7 on full set including marketing opt-out and access-copy refusals).

## Follow-up for non-zero model usage

With `GHOST_DATA_LLM_API_KEY` set, re-run:

```bash
python3 code/main.py --sample
python3 code/main.py
```

Then refresh `code/evaluation/usage_report.md` / root `output.csv` from that live run. Expected: non-zero `call_count` and tokens; sample status still 32/32; exact steps ≥29/32 (prefer 30/32).

## Known limitations

- Two sample audit IDs remain undiscoverable without golden shortcuts.
- Live LLM metrics in this PR’s usage report are **0** because the cloud VM has no API key; mocked tests prove the wiring.
- Full-set step field accuracy vs hidden gold unknown until official grading.
- No organizer-only files used.
