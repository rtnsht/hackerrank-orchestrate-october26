# Usage Report — Full 518 + sample OCR polish

## Scope

- Full run: `dataset/deletion_requests.csv` → root `output.csv` (**518** rows), OCR **off**.
- Sample regression: **32/32** status, **30/32** exact `steps_json`.
- Optional OCR sample pass measured separately (not used for submission `output.csv`).

Deterministic Python only at evaluation runtime for the submitted full `output.csv`.

## Runtime

| Run | Wall time | Notes |
| --- | --- | --- |
| Full 518 (OCR off) | **~3.5 s** | submission path |
| Sample 32 (OCR off) | **~0.7 s** | default |
| Sample 32 (OCR on) | **~5.3 s** | `--ocr-images` / `GHOST_DATA_OCR=1` |
| Evidence observations (OCR off) | 3639 | |
| Evidence observations (OCR on) | ~3664 | +OCR rows |

## Model providers and names

| Provider | Model | Role |
| --- | --- | --- |
| none | n/a | No LLM/vision API calls |

Local OCR (optional): system `tesseract` CLI — not a cloud model; **0 tokens**.

Coding agent (development only): Cursor Cloud Agent / `cursor-grok-4.5-high-fast`.

## Model-call counts

| Model / tool | Calls (full submission run) |
| --- | --- |
| LLM APIs | **0** |
| tesseract OCR | **0** (OCR off) |
| **Total billable model calls** | **0** |

Optional OCR sample pass: 11 image files OCRed once during indexing (~5 s total); still **0** LLM tokens.

## Tokens

| Metric | Value |
| --- | --- |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens / request | 0 |
| Estimated total cost | **$0.00** |
| Estimated cost / request | **$0.00** |

## Notes

- Enable OCR: `python3 code/main.py --ocr-images` or `GHOST_DATA_OCR=1`.
- Requires `tesseract` on PATH (`tesseract-ocr` system package).
- OCR did not change any sample case outputs after line-local binding; did not recover `case_18`/`case_20` audits.
