# Usage Report — Full 518-case run

## Scope

Final full-dataset run that produced root `output.csv` from `dataset/deletion_requests.csv` (**518** cases).

Also re-validated sample: **32/32** status, **30/32** exact `steps_json` vs `sample_output.csv`.

This path is **deterministic Python only**. No model provider APIs were called.

## Runtime

| Metric | Value |
| --- | --- |
| Full 518 wall time | **~3.5 s** |
| Evidence observations indexed | 3639 |
| Hardware notes | Cursor Cloud Agent VM; single process |

## Model providers and names

| Provider | Model | Role |
| --- | --- | --- |
| none | n/a | No LLM/vision calls at evaluation runtime |

Coding agent used during development (not invoked at evaluation runtime): Cursor Cloud Agent / `cursor-grok-4.5-high-fast`.

## Model-call counts

| Model | Calls |
| --- | --- |
| (none) | 0 |
| **Total** | **0** |

## Tokens

| Metric | Value |
| --- | --- |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens / request | 0 |
| Total tokens (518 requests) | 0 |

## Estimated cost

| Metric | Value |
| --- | --- |
| Total estimated cost | $0.00 |
| Average cost / request | $0.00 |
| Per-model totals | n/a (no models) |

## Notes

- Runtime work is local CSV/SQL/JSON/log/PDF-text parsing under `dataset/`.
- Optional `--ocr-images` (tesseract) was **not** enabled for this submission run.
- If multimodal/LLM stages are added later for ambiguous `request_text` or PNG-only evidence, this file must be updated with per-model call counts, tokens, and cost.
