# Usage Report — Sample BMV run

## Scope

Final sample run that produced root `output.csv` from `dataset/sample_deletion_requests.csv` (32 cases).

This BMV path is **deterministic Python only**. No model provider APIs were called.

## Model providers and names

| Provider | Model | Role |
| --- | --- | --- |
| none | n/a | No LLM/vision calls |

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

## Estimated cost

| Metric | Value |
| --- | --- |
| Total estimated cost | $0.00 |
| Average cost / request | $0.00 |

## Notes

- Runtime work is local CSV/SQL/JSON/log/PDF parsing under `dataset/`.
- When multimodal/LLM stages are added for ambiguous `request_text` or PNG evidence on the full 518-case run, this file will be updated with per-model call counts, tokens, and cost.
